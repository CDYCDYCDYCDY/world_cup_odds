#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
弱队受让1.5球策略回测
=====================

策略逻辑：
  每场比赛比较两队 FIFA 排名（数字越大 = 排名越低 = 越弱）。
  买入"排名更低的队（弱队）受让 1.5 球后获胜"：
    - 弱队是主队（home_rank > away_rank）→ 买"客让1.5"盘的 no
      （客队让1.5不成立 = 主队 +1.5 后胜）
    - 弱队是客队（away_rank > home_rank）→ 买"主让1.5"盘的 no
      （主队让1.5不成立 = 客队 +1.5 后胜）
  命中条件：对应让球盘 settled_yes = 0（让球不成立 = 弱队受让后胜）

结算模型（与 backtest/backtest.py 一致）：
  每场固定投入 STAKE 买入 no_token。
  价格 p = no_price（锚点前 VWAP），赔率 odds = 1/p
  命中：净收益 = STAKE × (odds - 1) = STAKE × (1/p - 1)
  未中：净收益 = -STAKE
  ROI = 总净收益 / 总投入

数据来源（只读，不修改）：
  ../../wc_odds_factors.csv   — FIFA 排名、阶段、轮次等因子
  ../../wc_handicap_odds.csv  — 让球盘 yes/no 价格与结算结果

样本筛选：
  - 排名不同（排名相同的场次跳过）
  - no_price 有值
  - settled_yes 有值（已结算）
  - no_quality 为 high/medium/fair（默认排除 low/missing，与 1X2 回测口径一致）
  - 可用 --allow-low-quality 放开限制

用法：
  python3 backtest.py                         # 默认 stake=100
  python3 backtest.py --stake 60
  python3 backtest.py --allow-low-quality     # 纳入低流动性报价

输出（脚本同目录）：
  backtest_summary.csv   策略汇总
  backtest_detail.csv    逐场明细
"""

import argparse
import csv
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
FACTORS_CSV = os.path.join(PROJECT_ROOT, "wc_odds_factors.csv")
HANDICAP_CSV = os.path.join(PROJECT_ROOT, "wc_handicap_odds.csv")

STAKE_DEFAULT = 100
# 默认可用的质量档位（排除 low 和 missing）
DEFAULT_QUALITY = {"high", "medium", "fair"}


def load_factors(csv_path):
    """读取因子表，返回 event_id -> row 的字典"""
    rows = {}
    with open(csv_path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rows[r["event_id"]] = r
    return rows


def load_handicap(csv_path):
    """读取让球盘表，返回 event_id -> row 的字典"""
    rows = {}
    with open(csv_path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rows[r["event_id"]] = r
    return rows


def determine_bet(factors_row):
    """
    根据 FIFA 排名确定下注方向。
    返回 (spread_key, team_label) 或 None（排名相同）。
      spread_key: "home_15" 或 "away_15"（要买 no 的那个让球盘）
      team_label: 弱队名称（用于明细展示）
    """
    try:
        home_rank = int(factors_row.get("home_fifa_ranking", ""))
        away_rank = int(factors_row.get("away_fifa_ranking", ""))
    except (ValueError, TypeError):
        return None

    if home_rank == away_rank:
        return None  # 排名相同，跳过

    home_team = factors_row.get("home_team", "")
    away_team = factors_row.get("away_team", "")

    if home_rank > away_rank:
        # 主队排名更低（更弱）→ 买"客让1.5"盘的 no
        return ("away_15", home_team)
    else:
        # 客队排名更低（更弱）→ 买"主让1.5"盘的 no
        return ("home_15", away_team)


def run_backtest(factors, handicap, stake, allow_low_quality):
    """执行回测，返回 (detail_rows, summary)"""
    quality_set = DEFAULT_QUALITY if not allow_low_quality else {"high", "medium", "fair", "low"}

    detail = []
    skipped_same_rank = 0
    skipped_no_price = 0
    skipped_unsettled = 0
    skipped_quality = 0

    for eid, fr in factors.items():
        hr = handicap.get(eid)
        if not hr:
            continue

        bet = determine_bet(fr)
        if bet is None:
            skipped_same_rank += 1
            continue

        spread_key, underdog_team = bet

        # 读取 no 价格、质量、结算
        no_price_str = hr.get(f"{spread_key}_no_price", "")
        no_quality = hr.get(f"{spread_key}_no_quality", "")
        settled_str = hr.get(f"{spread_key}_settled_yes", "")

        # 筛选：价格有值
        if not no_price_str:
            skipped_no_price += 1
            continue
        try:
            no_price = float(no_price_str)
        except ValueError:
            skipped_no_price += 1
            continue

        # 筛选：已结算
        if settled_str == "" or settled_str is None:
            skipped_unsettled += 1
            continue
        settled_yes = settled_str

        # 筛选：质量
        if no_quality not in quality_set:
            skipped_quality += 1
            continue

        # 结算：settled_yes=0 → 让球不成立 → no方胜 → 命中
        hit = (settled_yes == "0" or settled_yes == 0)
        odds = 1.0 / no_price if no_price > 0 else 0
        if hit:
            net = stake * (odds - 1)
        else:
            net = -stake

        # 排名差距
        try:
            home_rank = int(fr.get("home_fifa_ranking", 0))
            away_rank = int(fr.get("away_fifa_ranking", 0))
            rank_diff = abs(home_rank - away_rank)
        except (ValueError, TypeError):
            rank_diff = 0

        # 累计净收益
        detail.append({
            "event_id": eid,
            "title": fr.get("title", ""),
            "home_team": fr.get("home_team", ""),
            "away_team": fr.get("away_team", ""),
            "underdog": underdog_team,
            "home_fifa_ranking": fr.get("home_fifa_ranking", ""),
            "away_fifa_ranking": fr.get("away_fifa_ranking", ""),
            "rank_diff": rank_diff,
            "spread_key": spread_key,
            "spread_label": hr.get(f"{spread_key}_label", ""),
            "bet_side": "no",
            "no_price": round(no_price, 8),
            "odds": round(odds, 4),
            "no_quality": no_quality,
            "settled_yes": settled_yes,
            "hit": "Y" if hit else "N",
            "stake": stake,
            "net": round(net, 2),
            "kickoff_beijing": fr.get("kickoff_beijing", ""),
            "final_score": fr.get("final_score", ""),
            "result_regular": fr.get("result_regular", ""),
            "competition_phase": fr.get("competition_phase", ""),
            "group_matchday": fr.get("group_matchday", ""),
            "stage": fr.get("stage", ""),
        })

    # 按开赛时间排序
    detail.sort(key=lambda r: r.get("kickoff_beijing", ""))

    # 累计净收益
    cum = 0
    for r in detail:
        cum += r["net"]
        r["cum_net"] = round(cum, 2)

    # 汇总
    n = len(detail)
    hits = sum(1 for r in detail if r["hit"] == "Y")
    total_stake = n * stake
    total_net = sum(r["net"] for r in detail)
    roi = (total_net / total_stake * 100) if total_stake > 0 else 0

    # 最大回撤
    peak = 0
    mdd = 0
    cum2 = 0
    for r in detail:
        cum2 += r["net"]
        peak = max(peak, cum2)
        mdd = min(mdd, cum2 - peak)

    # 收益集中度
    nets_sorted = sorted([r["net"] for r in detail], reverse=True)
    total_net_val = sum(nets_sorted)
    top3 = sum(nets_sorted[:3]) if len(nets_sorted) >= 3 else sum(nets_sorted)

    summary = {
        "strategy": "underdog_h15_no (弱队受让1.5球后胜)",
        "matches_bet": n,
        "hits": hits,
        "hit_rate": round(hits / n * 100, 2) if n > 0 else 0,
        "total_stake": total_stake,
        "total_net": round(total_net, 2),
        "roi_pct": round(roi, 2),
        "max_drawdown": round(abs(mdd), 2),
        "top3_contribution": round(top3, 2),
        "top3_share": round(top3 / total_net_val * 100, 1) if total_net_val != 0 else 0,
        "skipped_same_rank": skipped_same_rank,
        "skipped_no_price": skipped_no_price,
        "skipped_unsettled": skipped_unsettled,
        "skipped_quality": skipped_quality,
    }

    return detail, summary


def write_output(detail, summary, out_dir, stake, allow_low_quality):
    os.makedirs(out_dir, exist_ok=True)

    # detail
    detail_path = os.path.join(out_dir, "backtest_detail.csv")
    detail_fields = [
        "event_id", "title", "home_team", "away_team", "underdog",
        "home_fifa_ranking", "away_fifa_ranking", "rank_diff",
        "spread_key", "spread_label", "bet_side",
        "no_price", "odds", "no_quality", "settled_yes", "hit",
        "stake", "net", "cum_net",
        "kickoff_beijing", "final_score", "result_regular",
        "competition_phase", "group_matchday", "stage",
    ]
    with open(detail_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=detail_fields)
        w.writeheader()
        w.writerows(detail)

    # summary
    summary_path = os.path.join(out_dir, "backtest_summary.csv")
    summary_fields = [
        "strategy", "matches_bet", "hits", "hit_rate",
        "total_stake", "total_net", "roi_pct",
        "max_drawdown", "top3_contribution", "top3_share",
        "skipped_same_rank", "skipped_no_price",
        "skipped_unsettled", "skipped_quality",
    ]
    with open(summary_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=summary_fields)
        w.writeheader()
        w.writerow(summary)

    return detail_path, summary_path


def main():
    ap = argparse.ArgumentParser(description="弱队受让1.5球策略回测")
    ap.add_argument("--stake", type=int, default=STAKE_DEFAULT)
    ap.add_argument("--allow-low-quality", action="store_true")
    ap.add_argument("--out-dir", default=HERE)
    args = ap.parse_args()

    print(f"[1/3] 读取数据（只读）...")
    print(f"  因子表: {FACTORS_CSV}")
    print(f"  让球盘: {HANDICAP_CSV}")
    factors = load_factors(FACTORS_CSV)
    handicap = load_handicap(HANDICAP_CSV)
    print(f"  因子表 {len(factors)} 场, 让球盘 {len(handicap)} 场")

    print(f"\n[2/3] 回测（stake={args.stake}, allow_low={args.allow_low_quality}）...")
    detail, summary = run_backtest(factors, handicap, args.stake, args.allow_low_quality)

    print(f"\n[3/3] 输出结果...")
    detail_path, summary_path = write_output(detail, summary, args.out_dir, args.stake, args.allow_low_quality)
    print(f"  明细 -> {detail_path}")
    print(f"  汇总 -> {summary_path}")

    # 控制台打印汇总
    s = summary
    print(f"\n{'='*60}")
    print(f"策略: {s['strategy']}")
    print(f"下注场次: {s['matches_bet']}  命中: {s['hits']} ({s['hit_rate']}%)")
    print(f"总投入: {s['total_stake']}  净收益: {s['total_net']}  ROI: {s['roi_pct']}%")
    print(f"最大回撤: {s['max_drawdown']}")
    print(f"Top3贡献: {s['top3_contribution']} (占比 {s['top3_share']}%)")
    print(f"跳过: 排名相同={s['skipped_same_rank']} 无价={s['skipped_no_price']} 未结算={s['skipped_unsettled']} 质量不符={s['skipped_quality']}")

    # 分阶段
    if detail:
        from collections import defaultdict
        by_phase = defaultdict(lambda: [0, 0, 0])  # [n, hits, net]
        for r in detail:
            ph = r.get("competition_phase", "")
            by_phase[ph][0] += 1
            if r["hit"] == "Y":
                by_phase[ph][1] += 1
            by_phase[ph][2] += r["net"]
        print(f"\n--- 分阶段 ---")
        for ph, (n, h, net) in sorted(by_phase.items()):
            roi = net / (n * args.stake) * 100 if n > 0 else 0
            print(f"  {ph or '未知':<12} {n}场 命中{h}({h/n*100:.1f}%) 净收益{net:.0f} ROI {roi:.1f}%")

        # 按排名差距分档
        by_diff = {"0-10": [0,0,0], "11-30": [0,0,0], "31-60": [0,0,0], "60+": [0,0,0]}
        for r in detail:
            d = r["rank_diff"]
            if d <= 10: k = "0-10"
            elif d <= 30: k = "11-30"
            elif d <= 60: k = "31-60"
            else: k = "60+"
            by_diff[k][0] += 1
            if r["hit"] == "Y": by_diff[k][1] += 1
            by_diff[k][2] += r["net"]
        print(f"\n--- 按排名差距 ---")
        for k, (n, h, net) in by_diff.items():
            if n == 0: continue
            roi = net / (n * args.stake) * 100
            print(f"  差距{k:<6} {n}场 命中{h}({h/n*100:.1f}%) 净收益{net:.0f} ROI {roi:.1f}%")


if __name__ == "__main__":
    main()
