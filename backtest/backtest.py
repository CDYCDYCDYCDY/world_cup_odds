#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
世界杯 1X2 策略回测
====================

数据来源：../wc_odds.csv （由 fetch_wc_odds.py 生成）
  - 赔率：以“开赛前 1 小时”为锚点、只聚合锚点前 3 分钟成交的 VWAP；十进制赔率 = 1 / VWAP
  - 赛果：Polymarket 常规时间(90分钟+补时)结算结果 result_regular ∈ {home,draw,away}

结算模型（十进制赔率）：
  每场按固定金额 STAKE 下注某一路。
  命中：回收 = STAKE × odds（含本金），净收益 = STAKE × (odds - 1)
  未中：回收 = 0，净收益 = -STAKE
  收益率 ROI = 总净收益 / 总投入

样本筛选（"可回测集"）：
  - 三路赔率齐全（home_odds/draw_odds/away_odds 均有值）
  - 三路报价质量均为 high 或 medium（默认不使用低流动性兜底价）
  - 已结算（settled == "Y"）且有 result_regular
  说明：赔率是市场历史成交形成的隐含十进制赔率，非某家博彩公司可直接成交的挂盘；
        回测不计手续费、盘口深度、滑点与资金限制。

回测的策略：
  S1  永远买平局(draw)
  S2  永远买主胜(home)
  S3  永远买客胜(away)
  S4  三路各买 1/3（分散，等额）
  S5  只买"平局赔率≥阈值"的场次买平（价值票，默认阈值 5.0）
  S6  买赔率最低的一路（跟随市场热门/最可能结果）
  S7  买赔率最高的一路（专抄冷门）

用法：
  python3 backtest.py                 # 默认 stake=100，读 ../wc_odds.csv
  python3 backtest.py --stake 60
  python3 backtest.py --csv /path/to/wc_odds.csv --value-threshold 5
  python3 backtest.py --allow-low-quality  # 显式纳入低流动性兜底报价
输出：
  backtest_summary.csv   各策略汇总
  backtest_detail.csv    每场每策略逐笔明细
  控制台打印汇总
"""

import argparse
import csv
import os


def load_rows(csv_path, allow_low_quality=False):
    """读取并筛选可回测样本；默认排除低流动性兜底报价。"""
    rows = []
    with open(csv_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for r in reader:
            # 必须三路赔率齐全 + 已结算 + 有赛果
            try:
                ho = float(r["home_odds"]) if r["home_odds"] else None
                do = float(r["draw_odds"]) if r["draw_odds"] else None
                ao = float(r["away_odds"]) if r["away_odds"] else None
            except ValueError:
                continue
            if None in (ho, do, ao):
                continue
            qualities = {
                side: (r.get(f"{side}_price_quality") or "high")
                for side in ("home", "draw", "away")
            }
            if not allow_low_quality and any(q not in ("high", "medium") for q in qualities.values()):
                continue
            if r.get("settled") != "Y":
                continue
            res = r.get("result_regular", "")
            if res not in ("home", "draw", "away"):
                continue
            rows.append({
                "title": r["title"],
                "kickoff_beijing": r["kickoff_beijing"],
                "odds": {"home": ho, "draw": do, "away": ao},
                "price_quality": qualities,
                "result": res,
            })
    return rows


def settle(stake, odds, hit):
    """返回该笔净收益。"""
    return stake * (odds - 1) if hit else -stake


# ---- 各策略：给定一场，返回 [(bet_side, stake_fraction), ...] ----
def bets_fixed(side):
    def _f(row, stake, threshold):
        return [(side, stake)]
    return _f


def bets_spread(row, stake, threshold):
    each = stake / 3.0
    return [("home", each), ("draw", each), ("away", each)]


def bets_value_draw(row, stake, threshold):
    # 仅当平局赔率 >= 阈值才买平，否则不下注
    if row["odds"]["draw"] >= threshold:
        return [("draw", stake)]
    return []


def bets_lowest(row, stake, threshold):
    side = min(row["odds"], key=row["odds"].get)  # 赔率最低=最热门
    return [(side, stake)]


def bets_highest(row, stake, threshold):
    side = max(row["odds"], key=row["odds"].get)  # 赔率最高=最冷门
    return [(side, stake)]


STRATEGIES = [
    ("S1_always_draw", "永远买平局", bets_fixed("draw")),
    ("S2_always_home", "永远买主胜", bets_fixed("home")),
    ("S3_always_away", "永远买客胜", bets_fixed("away")),
    ("S4_spread_3way", "三路各买1/3", bets_spread),
    ("S5_value_draw", "平局赔率≥阈值才买平", bets_value_draw),
    ("S6_favorite", "买赔率最低一路(热门)", bets_lowest),
    ("S7_underdog", "买赔率最高一路(冷门)", bets_highest),
]


def run(rows, stake, threshold):
    summary = []
    detail = []
    for key, name, fn in STRATEGIES:
        total_stake = 0.0
        total_return = 0.0  # 含本金回收
        n_bet_matches = 0   # 实际下注的场次数
        n_hit = 0           # 命中笔数
        n_legs = 0          # 下注笔数
        for row in rows:
            bets = fn(row, stake, threshold)
            if not bets:
                continue
            n_bet_matches += 1
            match_hit = False
            for side, s in bets:
                n_legs += 1
                total_stake += s
                hit = (row["result"] == side)
                if hit:
                    n_hit += 1
                    match_hit = True
                    total_return += s * row["odds"][side]  # 回收含本金
                detail.append({
                    "strategy": key,
                    "title": row["title"],
                    "kickoff_beijing": row["kickoff_beijing"],
                    "bet_side": side,
                    "price_quality": row["price_quality"][side],
                    "stake": round(s, 2),
                    "odds": row["odds"][side],
                    "result": row["result"],
                    "hit": "Y" if hit else "N",
                    "net": round(settle(s, row["odds"][side], hit), 2),
                })
        net = total_return - total_stake
        roi = (net / total_stake * 100) if total_stake else 0.0
        hit_rate = (n_hit / n_legs * 100) if n_legs else 0.0
        summary.append({
            "strategy": key,
            "desc": name,
            "matches_bet": n_bet_matches,
            "legs": n_legs,
            "hits": n_hit,
            "hit_rate_%": round(hit_rate, 1),
            "total_stake": round(total_stake, 2),
            "total_return": round(total_return, 2),
            "net_profit": round(net, 2),
            "roi_%": round(roi, 2),
        })
    return summary, detail


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=os.path.join(here, "..", "wc_odds.csv"))
    ap.add_argument("--stake", type=float, default=100.0, help="每场每路下注额")
    ap.add_argument("--value-threshold", type=float, default=5.0,
                    help="S5 平局价值票的赔率阈值")
    ap.add_argument("--allow-low-quality", action="store_true",
                    help="纳入 low / partial 等低流动性兜底报价")
    ap.add_argument("--out-dir", default=here)
    args = ap.parse_args()

    rows = load_rows(args.csv, allow_low_quality=args.allow_low_quality)
    print(f"可回测样本：{len(rows)} 场（三路赔率齐全 + 已结算 + 有常规时间赛果）")
    dist = {"home": 0, "draw": 0, "away": 0}
    for r in rows:
        dist[r["result"]] += 1
    print(f"赛果分布：主胜 {dist['home']} / 平局 {dist['draw']} / 客胜 {dist['away']}")
    print(f"每笔下注额 stake = {args.stake}，S5平局阈值 = {args.value_threshold}\n")

    summary, detail = run(rows, args.stake, args.value_threshold)

    # 打印
    print(f"{'策略':<18}{'说明':<22}{'下注场':>6}{'命中率%':>8}{'投入':>10}{'净收益':>12}{'ROI%':>9}")
    print("-" * 92)
    for s in summary:
        print(f"{s['strategy']:<18}{s['desc']:<22}{s['matches_bet']:>6}"
              f"{s['hit_rate_%']:>8}{s['total_stake']:>10}"
              f"{s['net_profit']:>12}{s['roi_%']:>9}")

    # 写文件
    os.makedirs(args.out_dir, exist_ok=True)
    sum_path = os.path.join(args.out_dir, "backtest_summary.csv")
    det_path = os.path.join(args.out_dir, "backtest_detail.csv")
    with open(sum_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(summary[0].keys()))
        w.writeheader()
        w.writerows(summary)
    with open(det_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(detail[0].keys()))
        w.writeheader()
        w.writerows(detail)
    print(f"\n汇总 -> {sum_path}")
    print(f"明细 -> {det_path}")


if __name__ == "__main__":
    main()
