#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
为 wc_odds.csv 补充比赛因子字段，生成 wc_odds_factors.csv。

输入：
  - wc_odds.csv       赔率与赛果（由 fetch_wc_odds.py 生成）
  - wc_schedule.csv   104 场赛程骨架（由 fetch_schedule.py 生成）
  - teams.csv         48 队标准化映射（国家、联合会、大洲、FIFA 排名、东道主）

输出：
  - wc_odds_factors.csv  含全部原始字段 + 比赛因子的完整表

新增因子分三类：
  1. 静态比赛因子：国家、洲别、联合会、阶段、小组、赛程轮次、东道主
  2. 赛前市场因子：热门方向、概率差、市场熵、流动性汇总
  3. 赛前球队状态：休息天数、累计参赛场数（仅用锚点前已结束的比赛）

所有赛前状态因子严格按时间顺序计算，不使用未来赛果。

用法：
  python3 build_factors.py                         # 默认读同目录
  python3 build_factors.py --out-dir /path/to/dir
"""

import argparse
import csv
import math
import os
from collections import defaultdict
from datetime import datetime, timezone


def load_csv(path):
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def parse_iso_utc(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_kickoff(row):
    """从 odds 行解析开赛时间（UTC datetime）。"""
    return parse_iso_utc(row.get("kickoff_utc"))


def build_team_lookup(teams_rows):
    """polymarket_name -> team info dict"""
    lookup = {}
    for t in teams_rows:
        lookup[t["polymarket_name"]] = t
    return lookup


def build_schedule_lookup(schedule_rows):
    """(home_team, away_team, kickoff_date) -> schedule row
    淘汰赛占位符无法直接匹配，按 kickoff_utc 精确匹配。"""
    by_kickoff = {}
    for s in schedule_rows:
        dt = parse_iso_utc(s.get("kickoff_utc"))
        if dt:
            by_kickoff[dt] = s
    return by_kickoff


def match_schedule(odds_row, schedule_by_kickoff, schedule_rows):
    """尝试将 odds 行匹配到赛程骨架中的一场。
    优先按 kickoff_utc 精确匹配；若失败则按队名 + 日期模糊匹配。"""
    kickoff = parse_kickoff(odds_row)
    if kickoff and kickoff in schedule_by_kickoff:
        return schedule_by_kickoff[kickoff]

    # 兜底：按队名匹配（处理时区或时间细微差异）
    home = odds_row.get("home_team", "")
    away = odds_row.get("away_team", "")
    for s in schedule_rows:
        if s.get("home_team") == home and s.get("away_team") == away:
            return s
    return None


def compute_market_factors(row):
    """从 VWAP 价格计算赛前市场因子。"""
    sides = ("home", "draw", "away")
    prices = {}
    probs = {}
    for s in sides:
        p = row.get(f"{s}_price", "")
        pn = row.get(f"{s}_prob_norm", "")
        prices[s] = float(p) if p else None
        probs[s] = float(pn) if pn else None

    result = {
        "favorite_side": "",
        "favorite_prob_norm": "",
        "underdog_prob_norm": "",
        "home_away_prob_diff": "",
        "top2_prob_gap": "",
        "market_entropy": "",
        "total_trade_count": "",
        "total_trade_volume": "",
        "quote_quality_min": "",
    }

    if None in probs.values():
        return result

    # 热门方向
    sorted_sides = sorted(sides, key=lambda s: probs[s], reverse=True)
    result["favorite_side"] = sorted_sides[0]
    result["favorite_prob_norm"] = round(probs[sorted_sides[0]], 4)
    result["underdog_prob_norm"] = round(probs[sorted_sides[-1]], 4)
    result["home_away_prob_diff"] = round(probs["home"] - probs["away"], 4)
    result["top2_prob_gap"] = round(probs[sorted_sides[0]] - probs[sorted_sides[1]], 4)

    # 市场熵（基于归一化概率）
    entropy = 0.0
    for s in sides:
        if probs[s] > 0:
            entropy -= probs[s] * math.log2(probs[s])
    result["market_entropy"] = round(entropy, 4)

    # 流动性汇总
    trade_counts = []
    trade_volumes = []
    qualities = []
    for s in sides:
        tc = row.get(f"{s}_trade_count", "")
        tv = row.get(f"{s}_trade_volume", "")
        q = row.get(f"{s}_price_quality", "")
        trade_counts.append(int(tc) if tc else 0)
        trade_volumes.append(float(tv) if tv else 0.0)
        qualities.append(q if q else "missing")

    result["total_trade_count"] = sum(trade_counts)
    result["total_trade_volume"] = round(sum(trade_volumes), 4)

    quality_order = {"high": 0, "medium": 1, "low": 2, "partial": 3, "missing": 4}
    result["quote_quality_min"] = min(qualities, key=lambda q: quality_order.get(q, 99))

    return result


def compute_rest_days(matches_sorted, team_name, current_kickoff):
    """计算某队在当前比赛前距上一场的休息天数。
    matches_sorted: 按开赛时间排序的全部比赛列表。
    只使用 current_kickoff 之前的比赛。"""
    current_ts = current_kickoff.timestamp()
    last_kickoff = None
    for m in matches_sorted:
        mk = parse_kickoff(m)
        if mk is None:
            continue
        if mk.timestamp() >= current_ts:
            break
        if m.get("home_team") == team_name or m.get("away_team") == team_name:
            last_kickoff = mk

    if last_kickoff is None:
        return ""

    diff_seconds = current_ts - last_kickoff.timestamp()
    return round(diff_seconds / 86400.0, 2)


def compute_prior_matches(matches_sorted, team_name, current_kickoff):
    """计算某队在当前比赛前已参赛场数。"""
    current_ts = current_kickoff.timestamp()
    count = 0
    for m in matches_sorted:
        mk = parse_kickoff(m)
        if mk is None:
            continue
        if mk.timestamp() >= current_ts:
            break
        if m.get("home_team") == team_name or m.get("away_team") == team_name:
            count += 1
    return count


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="为赔率数据补充比赛因子")
    ap.add_argument("--odds-csv", default=os.path.join(here, "wc_odds.csv"))
    ap.add_argument("--schedule-csv", default=os.path.join(here, "wc_schedule.csv"))
    ap.add_argument("--teams-csv", default=os.path.join(here, "teams.csv"))
    ap.add_argument("--out-dir", default=here)
    args = ap.parse_args()

    odds_rows = load_csv(args.odds_csv)
    schedule_rows = load_csv(args.schedule_csv)
    teams_rows = load_csv(args.teams_csv)

    team_lookup = build_team_lookup(teams_rows)
    schedule_by_kickoff = build_schedule_lookup(schedule_rows)

    # 按开赛时间排序，用于计算休息天数和累计场数
    odds_sorted = sorted(odds_rows, key=lambda r: r.get("kickoff_utc", ""))

    enriched = []
    matched = 0
    unmatched = 0

    for row in odds_sorted:
        kickoff = parse_kickoff(row)
        sched = match_schedule(row, schedule_by_kickoff, schedule_rows)

        enriched_row = dict(row)

        # ---- 静态比赛因子 ----
        home_name = row.get("home_team", "")
        away_name = row.get("away_team", "")
        home_info = team_lookup.get(home_name, {})
        away_info = team_lookup.get(away_name, {})

        enriched_row["home_country_code"] = home_info.get("country_code", "")
        enriched_row["home_country_zh"] = home_info.get("country_zh", "")
        enriched_row["home_confederation"] = home_info.get("confederation", "")
        enriched_row["home_continent"] = home_info.get("continent", "")
        enriched_row["home_fifa_ranking"] = home_info.get("fifa_ranking", "")
        enriched_row["home_is_host"] = home_info.get("is_host", "")

        enriched_row["away_country_code"] = away_info.get("country_code", "")
        enriched_row["away_country_zh"] = away_info.get("country_zh", "")
        enriched_row["away_confederation"] = away_info.get("confederation", "")
        enriched_row["away_continent"] = away_info.get("continent", "")
        enriched_row["away_fifa_ranking"] = away_info.get("fifa_ranking", "")
        enriched_row["away_is_host"] = away_info.get("is_host", "")

        enriched_row["same_confederation"] = (
            "Y" if home_info.get("confederation") and
                  home_info.get("confederation") == away_info.get("confederation")
            else "N"
        )
        enriched_row["same_continent"] = (
            "Y" if home_info.get("continent") and
                  home_info.get("continent") == away_info.get("continent")
            else "N"
        )
        enriched_row["fifa_ranking_diff"] = (
            str(int(home_info["fifa_ranking"]) - int(away_info["fifa_ranking"]))
            if home_info.get("fifa_ranking") and away_info.get("fifa_ranking")
            else ""
        )

        # 赛程阶段
        if sched:
            matched += 1
            enriched_row["match_number"] = sched.get("match_number", "")
            enriched_row["stage"] = sched.get("stage", "")
            enriched_row["stage_order"] = sched.get("stage_order", "")
            enriched_row["competition_phase"] = sched.get("competition_phase", "")
            enriched_row["group_code"] = sched.get("group_code", "")
            enriched_row["stadium"] = sched.get("stadium", "")
            enriched_row["host_city"] = sched.get("host_city", "")
            enriched_row["is_elimination_match"] = (
                "Y" if sched.get("competition_phase") == "knockout" else "N"
            )
            # 小组赛轮次：同组内按日期排序
            group_code = sched.get("group_code", "")
            if group_code:
                group_matches = [
                    s for s in schedule_rows
                    if s.get("group_code") == group_code and s.get("stage") == "group-stage"
                ]
                group_matches.sort(key=lambda s: s.get("kickoff_utc", ""))
                matchday = 0
                for i, gm in enumerate(group_matches):
                    if gm.get("match_number") == sched.get("match_number"):
                        matchday = (i // 2) + 1
                        break
                enriched_row["group_matchday"] = matchday
            else:
                enriched_row["group_matchday"] = ""
        else:
            unmatched += 1
            enriched_row["match_number"] = ""
            enriched_row["stage"] = ""
            enriched_row["stage_order"] = ""
            enriched_row["competition_phase"] = ""
            enriched_row["group_code"] = ""
            enriched_row["stadium"] = ""
            enriched_row["host_city"] = ""
            enriched_row["is_elimination_match"] = ""
            enriched_row["group_matchday"] = ""

        # ---- 赛前市场因子 ----
        market = compute_market_factors(row)
        enriched_row.update(market)

        # ---- 赛前球队状态因子 ----
        if kickoff:
            enriched_row["home_rest_days"] = compute_rest_days(odds_sorted, home_name, kickoff)
            enriched_row["away_rest_days"] = compute_rest_days(odds_sorted, away_name, kickoff)
            enriched_row["home_matches_played_prior"] = compute_prior_matches(odds_sorted, home_name, kickoff)
            enriched_row["away_matches_played_prior"] = compute_prior_matches(odds_sorted, away_name, kickoff)
        else:
            enriched_row["home_rest_days"] = ""
            enriched_row["away_rest_days"] = ""
            enriched_row["home_matches_played_prior"] = ""
            enriched_row["away_matches_played_prior"] = ""

        if enriched_row.get("home_rest_days") and enriched_row.get("away_rest_days"):
            enriched_row["rest_days_diff"] = round(
                float(enriched_row["home_rest_days"]) - float(enriched_row["away_rest_days"]), 2
            )
        else:
            enriched_row["rest_days_diff"] = ""

        enriched.append(enriched_row)

    # 写出
    out_path = os.path.join(args.out_dir, "wc_odds_factors.csv")
    if enriched:
        fields = list(enriched[0].keys())
        with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(enriched)

    print(f"完成: {len(enriched)} 场，赛程匹配 {matched}，未匹配 {unmatched}")
    print(f"输出: {out_path}")

    # 因子分布快照
    if enriched:
        from collections import Counter
        phases = Counter(r.get("competition_phase") for r in enriched)
        stages = Counter(r.get("stage") for r in enriched)
        confs = Counter(r.get("same_confederation") for r in enriched)
        favs = Counter(r.get("favorite_side") for r in enriched)
        print(f"  阶段: {dict(phases)}")
        print(f"  stage: {dict(stages)}")
        print(f"  同联合会: {dict(confs)}")
        print(f"  热门方向: {dict(favs)}")


if __name__ == "__main__":
    main()
