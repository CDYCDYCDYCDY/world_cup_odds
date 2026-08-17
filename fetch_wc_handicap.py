#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
拉取整届 FIFA 世界杯所有【已开赛】场次的【让球胜负（Spread）】赔率。

让球盘位于 Polymarket 每场比赛的 "More Markets" event 下，共 4 种：
  - 主让1.5  (home -1.5)  slug 含 -spread-home-1pt5
  - 客让1.5  (away -1.5，即主受让1.5)  slug 含 -spread-away-1pt5
  - 主让2.5  (home -2.5)  slug 含 -spread-home-2pt5
  - 客让2.5  (away -2.5，即主受让2.5)  slug 含 -spread-away-2pt5

每个让球盘 market 的 outcomes=[队伍名, 对手名]（非 Yes/No）：
  - clobTokenIds[0] = 让球成立方（yes，该队覆盖让球）
  - clobTokenIds[1] = 未成立方（no，该队未覆盖）

取价方法与 fetch_wc_odds.py 完全对齐：
  - 锚点 = 开赛前 1 小时（kickoff - 3600s）
  - 窗口 = 锚点前 3 分钟 VWAP（data_api_vwap_3m_pre_anchor）
  - 降级 = 锚点前 10 分钟 VWAP → 24h 内最后一笔赛前成交
  - 绝不使用锚点之后的成交（禁止前视偏差）

匹配方式：
  - 从 wc_odds.csv 读取 1X2 场次（event_id / title / event_slug / kickoff）
  - 用 Gamma API 拉所有 "More Markets" events
  - 先按 title 精确匹配（base = more_markets_title 去 " - More Markets"），
    再按 event_slug 前缀兜底（more_markets_slug.startswith(event_slug)）

用法：
  python3 fetch_wc_handicap.py                # 真实拉取全部
  python3 fetch_wc_handicap.py --dry-run      # 只打印请求
  python3 fetch_wc_handicap.py --limit 5      # 只处理前 5 场

输出（脚本同目录）：
  wc_handicap_odds.csv     结构化表格（4 种让球 × yes/no 价格 + 审计字段）
  wc_handicap_odds.json    完整原始+计算结果
"""

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    print("缺少 requests，请先安装: pip install requests", file=sys.stderr)
    raise

GAMMA_BASE = "https://gamma-api.polymarket.com"
DATA_BASE = "https://data-api.polymarket.com"
WC_TAG_SLUGS = ["soccer-fifwc", "fifa-world-cup"]
PRE_MATCH_SECONDS = 3600          # 开赛前 1 小时锚点（与 1X2 对齐）
VWAP_WINDOW_SECONDS = 180         # 锚点前 3 分钟
FALLBACK_WINDOW_SECONDS = 600     # 3 分钟不足时扩展至 10 分钟
HOUR_WINDOW_SECONDS = 3600        # 10 分钟不足时扩展至锚点前 1 小时
MIN_VWAP_TRADES = 3
REQUEST_TIMEOUT = 30
PAGE_LIMIT = 100
SLEEP_BETWEEN = 0.15

# 4 种让球盘的 slug 模式与中文名
SPREAD_DEFS = [
    ("home_-1.5", "spread-home-1pt5", "主让1.5"),
    ("away_-1.5", "spread-away-1pt5", "客让1.5(主受让1.5)"),
    ("home_-2.5", "spread-home-2pt5", "主让2.5"),
    ("away_-2.5", "spread-away-2pt5", "客让2.5(主受让2.5)"),
]


class Client:
    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "wc-handicap/1.0"})

    def get(self, url, params=None):
        if self.dry_run:
            full = url
            if params:
                qs = "&".join(f"{k}={v}" for k, v in params.items())
                full = f"{url}?{qs}"
            print(f"[dry-run] GET {full}")
            return None
        for attempt in range(3):
            try:
                resp = self.session.get(url, params=params, timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()
                time.sleep(SLEEP_BETWEEN)
                return resp.json()
            except requests.RequestException:
                if attempt == 2:
                    raise
                time.sleep(1.0 * (attempt + 1))


# ---------------------------------------------------------------------------
# 拉取所有 "More Markets" events
# ---------------------------------------------------------------------------
def fetch_more_markets_events(client):
    events = []
    for tag_slug in WC_TAG_SLUGS:
        for closed_flag in ("false", "true"):
            offset = 0
            while True:
                params = {
                    "tag_slug": tag_slug,
                    "limit": PAGE_LIMIT,
                    "offset": offset,
                    "closed": closed_flag,
                    "archived": "false",
                }
                data = client.get(f"{GAMMA_BASE}/events", params=params)
                if client.dry_run or not data:
                    break
                events.extend(data)
                if len(data) < PAGE_LIMIT:
                    break
                offset += PAGE_LIMIT
            if client.dry_run:
                break
    dedup = {}
    for ev in events:
        dedup[ev.get("id")] = ev
    # 只保留 "More Markets" events
    return [ev for ev in dedup.values() if " - More Markets" in (ev.get("title") or "")]


def _parse_json_field(val):
    if val is None:
        return None
    if isinstance(val, (list, dict)):
        return val
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return None


def classify_spread_markets(event):
    """
    从 More Markets event 里提取 4 个让球盘 market。
    返回 dict: { spread_key: {conditionId, yes_token, no_token, settled_yes, ...}|None }
    spread_key: home_-1.5 / away_-1.5 / home_-2.5 / away_-2.5
    """
    markets = event.get("markets", []) or []
    result = {}
    for spread_key, slug_frag, _zh in SPREAD_DEFS:
        matched = None
        for m in markets:
            slug = (m.get("slug") or "")
            if slug_frag in slug:
                tokens = _parse_json_field(m.get("clobTokenIds")) or []
                outcomes = _parse_json_field(m.get("outcomes")) or []
                outcome_prices = _parse_json_field(m.get("outcomePrices"))
                settled_yes = None
                if outcome_prices:
                    try:
                        settled_yes = int(round(float(outcome_prices[0])))
                    except (ValueError, TypeError):
                        settled_yes = None
                matched = {
                    "market_slug": slug,
                    "conditionId": m.get("conditionId"),
                    "groupItemTitle": m.get("groupItemTitle"),
                    "question": m.get("question"),
                    "outcomes": outcomes,
                    "yes_token_id": tokens[0] if len(tokens) > 0 else None,
                    "no_token_id": tokens[1] if len(tokens) > 1 else None,
                    "settled_yes": settled_yes,
                    "uma_status": m.get("umaResolutionStatus"),
                }
                break
        result[spread_key] = matched
    return result


# ---------------------------------------------------------------------------
# 匹配 1X2 场次 <-> More Markets event
# ---------------------------------------------------------------------------
def build_more_markets_lookup(more_events):
    """title(base) -> event,  以及 slug 前缀 -> event 的两个索引"""
    by_title = {}
    by_slug_prefix = {}
    for ev in more_events:
        base_title = (ev.get("title") or "").replace(" - More Markets", "").strip()
        by_title[base_title] = ev
        slug = ev.get("slug") or ""
        # More Markets slug 如 "fifwc-alg-aut-2026-06-27-more-markets"
        # 前缀 = 去 "-more-markets" 后缀
        if slug.endswith("-more-markets"):
            prefix = slug[:-len("-more-markets")]
            by_slug_prefix[prefix] = ev
        else:
            by_slug_prefix[slug] = ev
    return by_title, by_slug_prefix


def match_more_markets(odds_row, by_title, by_slug_prefix):
    """返回匹配的 More Markets event 或 None"""
    title = odds_row.get("title", "")
    # 1. title 精确匹配
    if title in by_title:
        return by_title[title]
    # 2. event_slug 前缀匹配
    ev_slug = odds_row.get("event_slug", "")
    if ev_slug:
        # 1X2 event_slug 可能就是 More Markets 的前缀
        if ev_slug in by_slug_prefix:
            return by_slug_prefix[ev_slug]
        # 也可能 1X2 slug 本身带后缀，取公共前缀试
        for prefix, ev in by_slug_prefix.items():
            if ev_slug.startswith(prefix) or prefix.startswith(ev_slug):
                return ev
    # 3. title 模糊匹配（去空格/大小写）
    title_norm = title.replace(" ", "").lower()
    for bt, ev in by_title.items():
        if bt.replace(" ", "").lower() == title_norm:
            return ev
    return None


# ---------------------------------------------------------------------------
# 取价：同时取 yes / no 两个 token 的 VWAP
# ---------------------------------------------------------------------------
def _trade_price(trade):
    try:
        return float(trade.get("price"))
    except (TypeError, ValueError):
        return None


def _trade_size(trade):
    try:
        return float(trade.get("size"))
    except (TypeError, ValueError):
        return 0.0


def _vwap_of_token(trades, token_id, target, window_seconds, method, quality):
    """对指定 token 的成交算 VWAP"""
    window_start = target - window_seconds
    selected = [
        t for t in trades
        if t.get("asset") == token_id
        and window_start <= t.get("timestamp", 0) <= target
        and _trade_price(t) is not None
    ]
    if not selected:
        return None
    volume = sum(_trade_size(t) for t in selected)
    if volume > 0:
        price = sum(_trade_price(t) * _trade_size(t) for t in selected) / volume
    else:
        price = sum(_trade_price(t) for t in selected) / len(selected)
    latest = max(selected, key=lambda t: t["timestamp"])
    return {
        "price": round(price, 8),
        "point_epoch": latest["timestamp"],
        "trade_count": len(selected),
        "trade_volume": round(volume, 8),
        "price_age_seconds": target - latest["timestamp"],
        "method": method,
        "quality": quality,
    }


def _empty_quote(target, method="unavailable", quality="missing"):
    return {
        "price": None,
        "point_epoch": None,
        "trade_count": 0,
        "trade_volume": 0.0,
        "price_age_seconds": None,
        "method": method,
        "quality": quality,
    }


def fetch_spread_vwap(client, market_info, kickoff_epoch):
    """
    取让球盘 market 的 yes/no 两个 token 在锚点前3分钟VWAP。
    一次 Data API 请求按 conditionId 拿该 market 全部成交（两 token 都在），分别算 VWAP。
    """
    target = kickoff_epoch - PRE_MATCH_SECONDS
    if not market_info or not market_info.get("conditionId"):
        return {"yes": _empty_quote(target), "no": _empty_quote(target)}

    yes_tok = market_info.get("yes_token_id")
    no_tok = market_info.get("no_token_id")

    yes_final = None
    no_final = None
    for window_seconds, method, quality in (
        (VWAP_WINDOW_SECONDS, "data_api_vwap_3m_pre_anchor", "high"),
        (FALLBACK_WINDOW_SECONDS, "data_api_vwap_10m_pre_anchor", "medium"),
        (HOUR_WINDOW_SECONDS, "data_api_vwap_1h_pre_anchor", "fair"),
    ):
        if yes_final and no_final:
            break
        params = {
            "market": market_info["conditionId"],
            "start": target - window_seconds,
            "end": target,
            "limit": 10000,
            "offset": 0,
        }
        data = client.get(f"{DATA_BASE}/trades", params=params)
        if client.dry_run:
            return {"yes": _empty_quote(target), "no": _empty_quote(target)}
        if not isinstance(data, list):
            continue

        # yes/no 独立降级：各自在能满足 ≥3 笔的最小窗口定档，互不拖累
        if yes_tok and not yes_final:
            yes_meta = _vwap_of_token(data, yes_tok, target, window_seconds, method, quality)
            if yes_meta and yes_meta["trade_count"] >= MIN_VWAP_TRADES:
                yes_final = yes_meta
        if no_tok and not no_final:
            no_meta = _vwap_of_token(data, no_tok, target, window_seconds, method, quality)
            if no_meta and no_meta["trade_count"] >= MIN_VWAP_TRADES:
                no_final = no_meta

    # 24h 兜底：仅对仍未确定的路取锚点前最后一笔
    need_24h = []
    if yes_final is None and yes_tok:
        need_24h.append(("yes", yes_tok))
    if no_final is None and no_tok:
        need_24h.append(("no", no_tok))
    if need_24h:
        params = {
            "market": market_info["conditionId"],
            "start": target - 86400,
            "end": target,
            "limit": 10000,
            "offset": 0,
        }
        data = client.get(f"{DATA_BASE}/trades", params=params)
        if not client.dry_run and isinstance(data, list):
            for side, tok in need_24h:
                side_trades = [t for t in data if t.get("asset") == tok and t.get("timestamp", 0) <= target and _trade_price(t) is not None]
                if not side_trades:
                    continue
                latest = max(side_trades, key=lambda t: t["timestamp"])
                meta = {
                    "price": round(_trade_price(latest), 8),
                    "point_epoch": latest["timestamp"],
                    "trade_count": 1,
                    "trade_volume": round(_trade_size(latest), 8),
                    "price_age_seconds": target - latest["timestamp"],
                    "method": "data_api_last_trade_24h_pre_anchor",
                    "quality": "low",
                }
                if side == "yes":
                    yes_final = meta
                else:
                    no_final = meta

    return {
        "yes": yes_final or _empty_quote(target),
        "no": no_final or _empty_quote(target),
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="拉取世界杯让球胜负(Spread)赔率，取价方式与1X2对齐")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out-dir", default=os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--odds-csv", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "wc_odds.csv"))
    args = ap.parse_args()

    client = Client(dry_run=args.dry_run)

    # 1. 读 1X2 场次
    print("[1/4] 读取 1X2 场次 (wc_odds.csv) ...")
    odds_rows = list(csv.DictReader(open(args.odds_csv, encoding="utf-8-sig")))
    print(f"    共 {len(odds_rows)} 场 1X2 场次")

    # 2. 拉 More Markets events
    print("[2/4] 拉取 More Markets events ...")
    more_events = fetch_more_markets_events(client)
    if args.dry_run:
        print("[dry-run] More Markets 请求已演示。")
        return
    print(f"    共 {len(more_events)} 个 More Markets events")
    by_title, by_slug_prefix = build_more_markets_lookup(more_events)

    # 3. 逐场取价
    print("[3/4] 逐场拉取让球盘 VWAP ...")
    rows, records = [], []
    matched_count = 0
    for odds_row in odds_rows:
        more_ev = match_more_markets(odds_row, by_title, by_slug_prefix)
        if not more_ev:
            records.append({"event_id": odds_row.get("event_id"), "title": odds_row.get("title"),
                            "error": "no_more_markets_event_matched"})
            continue
        matched_count += 1

        spreads = classify_spread_markets(more_ev)
        kickoff_epoch = int(datetime.fromisoformat(odds_row["kickoff_utc"].replace("Z", "+00:00")).timestamp())
        target = kickoff_epoch - PRE_MATCH_SECONDS

        quotes = {}
        for spread_key, _frag, _zh in SPREAD_DEFS:
            mi = spreads.get(spread_key)
            q = fetch_spread_vwap(client, mi, kickoff_epoch)
            quotes[spread_key] = q

        # 组装行
        row = {
            "event_id": odds_row.get("event_id"),
            "title": odds_row.get("title"),
            "home_team": odds_row.get("home_team"),
            "away_team": odds_row.get("away_team"),
            "event_slug": odds_row.get("event_slug"),
            "kickoff_utc": odds_row.get("kickoff_utc"),
            "kickoff_beijing": odds_row.get("kickoff_beijing"),
            "target_pre1h_utc": odds_row.get("target_pre1h_utc"),
            "final_score": odds_row.get("final_score"),
            "result_regular": odds_row.get("result_regular"),
        }
        for spread_key, _frag, zh in SPREAD_DEFS:
            q = quotes[spread_key]
            mi = spreads.get(spread_key) or {}
            prefix = spread_key.replace("-", "").replace(".", "")  # home_15, away_15, home_25, away_25
            row[f"{prefix}_yes_price"] = q["yes"]["price"] if q["yes"]["price"] is not None else ""
            row[f"{prefix}_no_price"] = q["no"]["price"] if q["no"]["price"] is not None else ""
            row[f"{prefix}_yes_quality"] = q["yes"]["quality"]
            row[f"{prefix}_no_quality"] = q["no"]["quality"]
            row[f"{prefix}_yes_trade_count"] = q["yes"]["trade_count"]
            row[f"{prefix}_no_trade_count"] = q["no"]["trade_count"]
            row[f"{prefix}_yes_price_age"] = q["yes"]["price_age_seconds"] if q["yes"]["price_age_seconds"] is not None else ""
            row[f"{prefix}_no_price_age"] = q["no"]["price_age_seconds"] if q["no"]["price_age_seconds"] is not None else ""
            row[f"{prefix}_method"] = q["yes"]["method"]  # yes/no 同一 market 同一窗口，method 一致
            row[f"{prefix}_settled_yes"] = mi.get("settled_yes", "")  # 1=让球成立, 0=未成立
            row[f"{prefix}_label"] = zh
        rows.append(row)
        records.append({"event_id": odds_row.get("event_id"), "title": odds_row.get("title"),
                        "more_markets_event_id": more_ev.get("id"),
                        "kickoff_epoch": kickoff_epoch, "target_pre1h_epoch": target,
                        "spreads": spreads, "quotes": quotes})

        if args.limit and matched_count >= args.limit:
            break

    print(f"    匹配 {matched_count}/{len(odds_rows)} 场，未匹配 {len(odds_rows)-matched_count} 场")

    # 4. 落盘
    _write(args.out_dir, rows, records)
    n_full = sum(1 for r in rows if all(r.get(f"{p}_yes_price") not in ("", None) for p in ["home_15","away_15","home_25","away_25"]))
    print(f"[4/4] 完成：{len(rows)} 场，其中 4 种让球盘 yes 价全有 {n_full} 场")


def _write(out_dir, rows, records):
    os.makedirs(out_dir, exist_ok=True)
    csv_path = os.path.join(out_dir, "wc_handicap_odds.csv")
    json_path = os.path.join(out_dir, "wc_handicap_odds.json")
    # 动态收集字段
    fields = [
        "event_id", "title", "home_team", "away_team", "event_slug",
        "kickoff_utc", "kickoff_beijing", "target_pre1h_utc",
        "final_score", "result_regular",
    ]
    for spread_key, _frag, zh in SPREAD_DEFS:
        prefix = spread_key.replace("-", "").replace(".", "")
        fields.extend([
            f"{prefix}_label",
            f"{prefix}_yes_price", f"{prefix}_no_price",
            f"{prefix}_yes_quality", f"{prefix}_no_quality",
            f"{prefix}_yes_trade_count", f"{prefix}_no_trade_count",
            f"{prefix}_yes_price_age", f"{prefix}_no_price_age",
            f"{prefix}_method", f"{prefix}_settled_yes",
        ])
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    print(f"    CSV  -> {csv_path}")
    print(f"    JSON -> {json_path}")


if __name__ == "__main__":
    main()
