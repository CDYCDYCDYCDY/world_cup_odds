#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
拉取整届 FIFA 世界杯所有【已开赛】场次，在【开赛前 1 小时】锚点前 3 分钟内，
Polymarket 上【胜 / 平 / 负】三个市场的成交量加权平均价格（1X2）。

数据来源（Polymarket 官方公开 API，无需 key，非爬虫）：
  1) Gamma API   https://gamma-api.polymarket.com/events
     - 每场比赛是一个 event，含 3 个 market：主胜 / 平局 / 客胜
     - 每个 market: outcomes=["Yes","No"], clobTokenIds[0]=Yes token
  2) Data API    https://data-api.polymarket.com/trades
     - 按 conditionId 获取已成交记录，筛选各方向的 Yes token
     - 以“开赛前 1 小时”为锚点，只使用锚点【之前】的成交，避免前视偏差
     - 默认聚合窗口为 [锚点 - 3 分钟, 锚点]，按成交量加权平均价（VWAP）取价

赔率换算：
  Yes 价格 p ≈ 市场对该结果发生的隐含概率
  公平十进制赔率 = 1 / p
  另给出归一化概率：把主胜/平/客胜三个 VWAP 归一化（和=1）

用法：
  python3 fetch_wc_odds.py                # 真实拉取全部
  python3 fetch_wc_odds.py --dry-run      # 只打印请求，不联网
  python3 fetch_wc_odds.py --limit 5      # 只处理前 5 场（调试）

输出（默认脚本同目录）：
  wc_odds.csv     结构化表格（含胜平负三路与取价质量字段）
  wc_odds.json    完整原始+计算结果
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
PRE_MATCH_SECONDS = 3600          # 开赛前 1 小时锚点
VWAP_WINDOW_SECONDS = 180         # 只取锚点前 3 分钟成交
FALLBACK_WINDOW_SECONDS = 600     # 3 分钟样本不足时，扩展至锚点前 10 分钟
HOUR_WINDOW_SECONDS = 3600        # 10 分钟样本不足时，扩展至锚点前 1 小时
MIN_VWAP_TRADES = 3
REQUEST_TIMEOUT = 30
PAGE_LIMIT = 100
SLEEP_BETWEEN = 0.15


class Client:
    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "wc-odds/1.0"})

    def get(self, url, params=None):
        if self.dry_run:
            full = url
            if params:
                qs = "&".join(f"{k}={v}" for k, v in params.items())
                full = f"{url}?{qs}"
            print(f"[dry-run] GET {full}")
            return None

        # Data API 批量调用时可能出现短暂限流；有限重试比静默丢失报价更可审计。
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
# events 遍历
# ---------------------------------------------------------------------------
def fetch_all_wc_events(client):
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
    return list(dedup.values())


def _parse_json_field(val):
    if val is None:
        return None
    if isinstance(val, (list, dict)):
        return val
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return None


def _yes_no_tokens(market):
    """返回 (yes_token_id, no_token_id)"""
    outcomes = _parse_json_field(market.get("outcomes"))
    token_ids = _parse_json_field(market.get("clobTokenIds"))
    if outcomes and token_ids and len(outcomes) == len(token_ids):
        yes_tok = no_tok = None
        for oc, tk in zip(outcomes, token_ids):
            ol = str(oc).lower()
            if ol == "yes":
                yes_tok = tk
            elif ol == "no":
                no_tok = tk
        if yes_tok or no_tok:
            return yes_tok, no_tok
    # 兜底：outcomes 无 Yes/No 字样时，按顺序假定 [yes, no]
    if token_ids and len(token_ids) >= 2:
        return token_ids[0], token_ids[1]
    return (token_ids[0] if token_ids else None, None)


def classify_markets(event):
    """
    把 event 的 3 个 market 分类为 home / draw / away。
    识别规则：
      - draw:  groupItemTitle 含 "draw" 或 slug 以 -draw 结尾 或 question 含 "draw"
      - home/away: 通过 event.title "A vs. B" 匹配 groupItemTitle
    返回 dict: {"home": {...}|None, "draw": {...}|None, "away": {...}|None,
               "home_team":str, "away_team":str}
    """
    title = event.get("title", "") or ""
    home_team = away_team = ""
    # 解析 "Spain vs. Argentina"
    for sep in (" vs. ", " vs ", " VS "):
        if sep in title:
            parts = title.split(sep, 1)
            home_team = parts[0].strip()
            away_team = parts[1].strip().rstrip(".")
            break

    result = {"home": None, "draw": None, "away": None,
              "home_team": home_team, "away_team": away_team}

    markets = event.get("markets", []) or []

    def _info(m):
        # 解析结算价：已结束比赛 outcomePrices=["1","0"] 表示该市场(Yes)命中
        outcome_prices = _parse_json_field(m.get("outcomePrices"))
        settled_yes = None
        if outcome_prices:
            try:
                settled_yes = int(round(float(outcome_prices[0])))  # 1=命中, 0=未中
            except (ValueError, TypeError):
                settled_yes = None
        yes_tok, no_tok = _yes_no_tokens(m)
        return {
            "market_slug": m.get("slug"),
            "conditionId": m.get("conditionId"),
            "groupItemTitle": m.get("groupItemTitle"),
            "question": m.get("question"),
            "yes_token_id": yes_tok,
            "no_token_id": no_tok,
            "settled_yes": settled_yes,               # 结算结果：1命中/0未中/None未结算
            "uma_status": m.get("umaResolutionStatus"),
        }

    def _is_draw(m):
        # 平局判定：slug 以 -draw 结尾（最可靠），或 groupItemTitle/question 含 "draw"
        # 注意 groupItemTitle 可能是 "Draw (A vs. B)"，其中含客队名，故优先看 slug
        slug = (m.get("slug") or "")
        git = (m.get("groupItemTitle") or "").lower()
        q = (m.get("question") or "").lower()
        return slug.endswith("-draw") or git.startswith("draw") or "end in a draw" in q

    draw_markets = [m for m in markets if _is_draw(m)]
    win_markets = [m for m in markets if not _is_draw(m)]

    if draw_markets:
        result["draw"] = _info(draw_markets[0])

    # 主/客：先按 question "Will <team> win" 匹配，再按 slug 后缀兜底
    for m in win_markets:
        q = (m.get("question") or "").lower()
        slug = (m.get("slug") or "").lower()
        matched = False
        if home_team and (f"will {home_team.lower()} win" in q):
            result["home"] = _info(m); matched = True
        elif away_team and (f"will {away_team.lower()} win" in q):
            result["away"] = _info(m); matched = True
        if matched:
            continue
        # 兜底：question 里出现哪个队名
        if home_team and home_team.lower() in q and result["home"] is None:
            result["home"] = _info(m)
        elif away_team and away_team.lower() in q and result["away"] is None:
            result["away"] = _info(m)

    # 最后兜底：仍有空缺则按顺序填入剩余 win_markets
    if win_markets and (result["home"] is None or result["away"] is None):
        used = {result["home"]["conditionId"] if result["home"] else None,
                result["away"]["conditionId"] if result["away"] else None}
        leftover = [m for m in win_markets if m.get("conditionId") not in used]
        for m in leftover:
            if result["home"] is None:
                result["home"] = _info(m)
            elif result["away"] is None:
                result["away"] = _info(m)
    return result


# ---------------------------------------------------------------------------
# 时间与价格
# ---------------------------------------------------------------------------
def parse_iso(ts):
    if not ts:
        return None
    ts = ts.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def get_kickoff_epoch(event):
    """开赛时间。优先 gameStartTime/startTime，回退 endDate。"""
    for key in ("gameStartTime", "startTime"):
        dt = parse_iso(event.get(key))
        if dt:
            return int(dt.timestamp())
    dt = parse_iso(event.get("endDate"))
    return int(dt.timestamp()) if dt else None


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


def _vwap_meta(trades, target, window_seconds, method, quality):
    """把已筛选的成交记录聚合为 VWAP，并保留可审计的取价元数据。"""
    window_start = target - window_seconds
    selected = [
        trade for trade in trades
        if window_start <= trade.get("timestamp", 0) <= target
        and _trade_price(trade) is not None
    ]
    if not selected:
        return None

    volume = sum(_trade_size(trade) for trade in selected)
    if volume > 0:
        price = sum(_trade_price(trade) * _trade_size(trade) for trade in selected) / volume
    else:
        price = sum(_trade_price(trade) for trade in selected) / len(selected)

    latest = max(selected, key=lambda trade: trade["timestamp"])
    return {
        "price": round(price, 8),
        "point_epoch": latest["timestamp"],
        "window_start_epoch": window_start,
        "window_end_epoch": target,
        "trade_count": len(selected),
        "trade_volume": round(volume, 8),
        "price_age_seconds": target - latest["timestamp"],
        "method": method,
        "quality": quality,
        "trades_truncated": False,
    }


def _empty_quote(target):
    return {
        "price": None,
        "point_epoch": None,
        "window_start_epoch": None,
        "window_end_epoch": target,
        "trade_count": 0,
        "trade_volume": 0.0,
        "price_age_seconds": None,
        "method": "unavailable",
        "quality": "missing",
        "trades_truncated": False,
    }


def fetch_pre_match_vwap(client, market_info, kickoff_epoch):
    """取开赛前 1 小时锚点【之前】成交的 yes/no 两个 token 的 VWAP；绝不使用锚点后的成交。
    返回 {"yes": meta, "no": meta}。"""
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

        truncated = len(data) >= 10000
        # yes/no 独立降级：各自在能满足 ≥3 笔的最小窗口定档，互不拖累
        if yes_tok and not yes_final:
            yes_trades = [t for t in data if t.get("asset") == yes_tok and t.get("timestamp", 0) <= target]
            yes_meta = _vwap_meta(yes_trades, target, window_seconds, method, quality)
            if yes_meta:
                yes_meta["trades_truncated"] = truncated
                if truncated:
                    yes_meta["quality"] = "partial"
                if yes_meta["trade_count"] >= MIN_VWAP_TRADES:
                    yes_final = yes_meta
        if no_tok and not no_final:
            no_trades = [t for t in data if t.get("asset") == no_tok and t.get("timestamp", 0) <= target]
            no_meta = _vwap_meta(no_trades, target, window_seconds, method, quality)
            if no_meta:
                no_meta["trades_truncated"] = truncated
                if truncated:
                    no_meta["quality"] = "partial"
                if no_meta["trade_count"] >= MIN_VWAP_TRADES:
                    no_final = no_meta

    # 24h 兜底：仅对仍未确定的路取锚点前最后一笔，不伪装成 VWAP。
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
            truncated = len(data) >= 10000
            for side, tok in need_24h:
                side_trades = [t for t in data if t.get("asset") == tok and t.get("timestamp", 0) <= target and _trade_price(t) is not None]
                if not side_trades:
                    continue
                latest = max(side_trades, key=lambda t: t["timestamp"])
                meta = {
                    "price": round(_trade_price(latest), 8),
                    "point_epoch": latest["timestamp"],
                    "window_start_epoch": target - 86400,
                    "window_end_epoch": target,
                    "trade_count": 1,
                    "trade_volume": round(_trade_size(latest), 8),
                    "price_age_seconds": target - latest["timestamp"],
                    "method": "data_api_last_trade_24h_pre_anchor",
                    "quality": "low",
                    "trades_truncated": truncated,
                }
                if side == "yes":
                    yes_final = meta
                else:
                    no_final = meta

    return {"yes": yes_final or _empty_quote(target), "no": no_final or _empty_quote(target)}


def _vwap_of(client, market_info, kickoff):
    return fetch_pre_match_vwap(client, market_info, kickoff)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="拉取世界杯已开赛场次开赛前1小时锚点前3分钟VWAP胜平负赔率")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out-dir", default=os.path.dirname(os.path.abspath(__file__)))
    args = ap.parse_args()

    client = Client(dry_run=args.dry_run)
    now_epoch = int(time.time())

    print(f"[1/3] 拉取世界杯 events (tags={WC_TAG_SLUGS}) ...")
    events = fetch_all_wc_events(client)
    if args.dry_run:
        print("[dry-run] events 请求已演示；每场将对主胜/平/客胜市场各请求一次 Data API /trades，聚合开赛前 1 小时锚点前 3 分钟的成交。")
        print("\n[dry-run] 结束。")
        return
    print(f"    共获取 {len(events)} 个 events")

    rows, records = [], []
    processed = 0
    for ev in events:
        kickoff = get_kickoff_epoch(ev)
        if kickoff is None or kickoff > now_epoch:
            continue  # 只要已开赛
        # 只处理真正的 1X2 比赛盘：event 必须含平局市场，且 title 不是 Player Props 等衍生盘
        title = ev.get("title", "") or ""
        if "Player Props" in title or " - " in title:
            continue
        mk = classify_markets(ev)
        if mk["draw"] is None:
            continue  # 无平局市场 = 非 1X2 比赛盘，跳过

        target = kickoff - PRE_MATCH_SECONDS
        quote_home = _vwap_of(client, mk["home"], kickoff)
        quote_draw = _vwap_of(client, mk["draw"], kickoff)
        quote_away = _vwap_of(client, mk["away"], kickoff)
        quotes = {"home": quote_home, "draw": quote_draw, "away": quote_away}
        p_home = quote_home["yes"]["price"]
        p_draw = quote_draw["yes"]["price"]
        p_away = quote_away["yes"]["price"]
        p_home_no = quote_home["no"]["price"]
        p_draw_no = quote_draw["no"]["price"]
        p_away_no = quote_away["no"]["price"]

        # 归一化概率：仅当三路都有价；对各方向 VWAP 的和做归一化。
        norm = {"home": None, "draw": None, "away": None}
        if None not in (p_home, p_draw, p_away):
            s = p_home + p_draw + p_away
            if s > 0:
                norm = {
                    "home": round(p_home / s, 4),
                    "draw": round(p_draw / s, 4),
                    "away": round(p_away / s, 4),
                }

        def odds(p):
            return round(1.0 / p, 4) if p and p > 0 else ""

        # ---- 常规时间(90分钟+补时)结果 ----
        # Polymarket 的胜平负市场按"前90分钟+补时"结算，settled_yes==1 即命中
        result_regular = ""
        for key in ("home", "draw", "away"):
            mi = mk.get(key)
            if mi and mi.get("settled_yes") == 1:
                result_regular = key
                break
        final_score = ev.get("score", "")        # 常规时间最终比分，如 "3-1"
        match_ended = ev.get("ended", False)

        # 结果是否已结算：三个市场都 resolved 才算稳
        settled = all(
            (mk.get(k) and mk[k].get("uma_status") == "resolved")
            for k in ("home", "draw", "away")
        )

        row = {
            "event_id": ev.get("id"),
            "title": ev.get("title"),
            "home_team": mk["home_team"],
            "away_team": mk["away_team"],
            "event_slug": ev.get("slug"),
            "kickoff_utc": datetime.fromtimestamp(kickoff, tz=timezone.utc).isoformat(),
            "kickoff_beijing": datetime.fromtimestamp(kickoff + 8 * 3600, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "target_pre1h_utc": datetime.fromtimestamp(target, tz=timezone.utc).isoformat(),
            # 原始时间字段（透明化，便于核对开赛时间来源）
            "raw_endDate": ev.get("endDate", ""),
            "raw_startDate": ev.get("startDate", ""),
            # 胜平负 Yes/No 价格：开赛前 1 小时锚点前成交的 VWAP（≈隐含概率）
            "home_price": p_home if p_home is not None else "",
            "draw_price": p_draw if p_draw is not None else "",
            "away_price": p_away if p_away is not None else "",
            "home_no_price": p_home_no if p_home_no is not None else "",
            "draw_no_price": p_draw_no if p_draw_no is not None else "",
            "away_no_price": p_away_no if p_away_no is not None else "",
            # 取价审计信息（yes 路完整，no 路精简）
            "home_price_method": quote_home["yes"]["method"],
            "draw_price_method": quote_draw["yes"]["method"],
            "away_price_method": quote_away["yes"]["method"],
            "home_price_quality": quote_home["yes"]["quality"],
            "draw_price_quality": quote_draw["yes"]["quality"],
            "away_price_quality": quote_away["yes"]["quality"],
            "home_trade_count": quote_home["yes"]["trade_count"],
            "draw_trade_count": quote_draw["yes"]["trade_count"],
            "away_trade_count": quote_away["yes"]["trade_count"],
            "home_trade_volume": quote_home["yes"]["trade_volume"],
            "draw_trade_volume": quote_draw["yes"]["trade_volume"],
            "away_trade_volume": quote_away["yes"]["trade_volume"],
            "home_price_age_seconds": quote_home["yes"]["price_age_seconds"] if quote_home["yes"]["price_age_seconds"] is not None else "",
            "draw_price_age_seconds": quote_draw["yes"]["price_age_seconds"] if quote_draw["yes"]["price_age_seconds"] is not None else "",
            "away_price_age_seconds": quote_away["yes"]["price_age_seconds"] if quote_away["yes"]["price_age_seconds"] is not None else "",
            "home_price_point_utc": datetime.fromtimestamp(quote_home["yes"]["point_epoch"], tz=timezone.utc).isoformat() if quote_home["yes"]["point_epoch"] else "",
            "draw_price_point_utc": datetime.fromtimestamp(quote_draw["yes"]["point_epoch"], tz=timezone.utc).isoformat() if quote_draw["yes"]["point_epoch"] else "",
            "away_price_point_utc": datetime.fromtimestamp(quote_away["yes"]["point_epoch"], tz=timezone.utc).isoformat() if quote_away["yes"]["point_epoch"] else "",
            "home_window_start_utc": datetime.fromtimestamp(quote_home["yes"]["window_start_epoch"], tz=timezone.utc).isoformat() if quote_home["yes"]["window_start_epoch"] else "",
            "draw_window_start_utc": datetime.fromtimestamp(quote_draw["yes"]["window_start_epoch"], tz=timezone.utc).isoformat() if quote_draw["yes"]["window_start_epoch"] else "",
            "away_window_start_utc": datetime.fromtimestamp(quote_away["yes"]["window_start_epoch"], tz=timezone.utc).isoformat() if quote_away["yes"]["window_start_epoch"] else "",
            # no 路审计（method/window/point 与 yes 共用同一 market 同一窗口）
            "home_no_quality": quote_home["no"]["quality"],
            "draw_no_quality": quote_draw["no"]["quality"],
            "away_no_quality": quote_away["no"]["quality"],
            "home_no_trade_count": quote_home["no"]["trade_count"],
            "draw_no_trade_count": quote_draw["no"]["trade_count"],
            "away_no_trade_count": quote_away["no"]["trade_count"],
            "home_no_price_age": quote_home["no"]["price_age_seconds"] if quote_home["no"]["price_age_seconds"] is not None else "",
            "draw_no_price_age": quote_draw["no"]["price_age_seconds"] if quote_draw["no"]["price_age_seconds"] is not None else "",
            "away_no_price_age": quote_away["no"]["price_age_seconds"] if quote_away["no"]["price_age_seconds"] is not None else "",
            # 换算公平赔率 = 1/price
            "home_odds": odds(p_home),
            "draw_odds": odds(p_draw),
            "away_odds": odds(p_away),
            # 归一化概率（去抽水，三路和=1）
            "home_prob_norm": norm["home"] if norm["home"] is not None else "",
            "draw_prob_norm": norm["draw"] if norm["draw"] is not None else "",
            "away_prob_norm": norm["away"] if norm["away"] is not None else "",
            # ---- 常规时间赛果（Polymarket 按90分钟+补时结算）----
            "final_score": final_score,                    # 常规时间最终比分 如 "3-1"
            "result_regular": result_regular,              # home / draw / away
            "match_ended": "Y" if match_ended else "",
            "settled": "Y" if settled else "",
        }
        rows.append(row)
        records.append({
            "event": {k: ev.get(k) for k in
                      ("id", "title", "slug", "startDate", "endDate",
                       "score", "ended", "period", "finishedTimestamp")},
            "kickoff_epoch": kickoff,
            "target_pre1h_epoch": target,
            "markets": mk,
            "prices": quotes,
            "normalized_prob": norm,
            "result": {
                "final_score": final_score,
                "result_regular": result_regular,
                "match_ended": match_ended,
                "settled": settled,
            },
        })
        processed += 1
        if args.limit and processed >= args.limit:
            break

    _write(args.out_dir, rows, records)
    n_full = sum(1 for r in rows if r["home_price"] != "" and r["draw_price"] != "" and r["away_price"] != "")
    print(f"[3/3] 完成：已开赛场次 {len(rows)} 场，其中三路齐全 {n_full} 场")


def _write(out_dir, rows, records):
    os.makedirs(out_dir, exist_ok=True)
    csv_path = os.path.join(out_dir, "wc_odds.csv")
    json_path = os.path.join(out_dir, "wc_odds.json")
    fields = [
        "event_id", "title", "home_team", "away_team", "event_slug",
        "kickoff_utc", "kickoff_beijing", "target_pre1h_utc",
        "raw_endDate", "raw_startDate",
        "home_price", "draw_price", "away_price",
        "home_no_price", "draw_no_price", "away_no_price",
        "home_price_method", "draw_price_method", "away_price_method",
        "home_price_quality", "draw_price_quality", "away_price_quality",
        "home_trade_count", "draw_trade_count", "away_trade_count",
        "home_trade_volume", "draw_trade_volume", "away_trade_volume",
        "home_price_age_seconds", "draw_price_age_seconds", "away_price_age_seconds",
        "home_price_point_utc", "draw_price_point_utc", "away_price_point_utc",
        "home_window_start_utc", "draw_window_start_utc", "away_window_start_utc",
        "home_no_quality", "draw_no_quality", "away_no_quality",
        "home_no_trade_count", "draw_no_trade_count", "away_no_trade_count",
        "home_no_price_age", "draw_no_price_age", "away_no_price_age",
        "home_odds", "draw_odds", "away_odds",
        "home_prob_norm", "draw_prob_norm", "away_prob_norm",
        "final_score", "result_regular", "match_ended", "settled",
    ]
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
