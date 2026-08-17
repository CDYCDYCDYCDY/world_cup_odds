#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从 TheStatsAPI 拉取 2026 世界杯 104 场完整赛程骨架。

数据来源：https://www.thestatsapi.com/world-cup/data/fixtures.json
免费、CORS 可用、无需 key，可商用（需注明出处）。

输出：wc_schedule.csv
  - match_number: 官方比赛编号 1-104
  - kickoff_utc: UTC 开赛时间
  - kickoff_beijing: 北京时间开赛
  - stage: group-stage / round-of-32 / round-of-16 / quarter-finals / semi-finals / third-place / final
  - competition_phase: group / knockout
  - group_code: A-L（小组赛），淘汰赛留空
  - home_team / away_team: 队名或淘汰赛占位符
  - stadium / host_city: 球场与主办城市

用法：
  python3 fetch_schedule.py                # 拉取并覆盖 wc_schedule.csv
  python3 fetch_schedule.py --out-dir .    # 指定输出目录
"""

import argparse
import csv
import os
import sys
import time
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    print("缺少 requests，请先安装: pip install requests", file=sys.stderr)
    raise

FIXTURES_URL = "https://www.thestatsapi.com/world-cup/data/fixtures.json"
REQUEST_TIMEOUT = 30

KNOCKOUT_STAGES = {
    "round-of-32", "round-of-16", "quarter-finals",
    "semi-finals", "third-place", "final",
}

STAGE_ORDER = {
    "group-stage": 1,
    "round-of-32": 2,
    "round-of-16": 3,
    "quarter-finals": 4,
    "semi-finals": 5,
    "third-place": 6,
    "final": 7,
}


def fetch_fixtures():
    resp = requests.get(FIXTURES_URL, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    return data.get("fixtures", [])


def build_schedule_rows(fixtures):
    rows = []
    for f in fixtures:
        stage = f.get("stage", "")
        kickoff_utc = f.get("kickoffUtc", "")
        kickoff_dt = None
        if kickoff_utc:
            try:
                kickoff_dt = datetime.fromisoformat(kickoff_utc.replace("Z", "+00:00"))
            except ValueError:
                pass

        row = {
            "match_number": f.get("matchNumber"),
            "date": f.get("date", ""),
            "kickoff_utc": kickoff_utc,
            "kickoff_beijing": (
                datetime.fromtimestamp(
                    int(kickoff_dt.timestamp()) + 8 * 3600, tz=timezone.utc
                ).strftime("%Y-%m-%d %H:%M")
                if kickoff_dt else ""
            ),
            "stage": stage,
            "stage_order": STAGE_ORDER.get(stage, 99),
            "competition_phase": "knockout" if stage in KNOCKOUT_STAGES else "group",
            "group_code": f.get("group") or "",
            "home_team": f.get("homeTeam", ""),
            "away_team": f.get("awayTeam", ""),
            "stadium": f.get("stadium", ""),
            "host_city": f.get("hostCity", ""),
        }
        rows.append(row)
    rows.sort(key=lambda r: r["match_number"])
    return rows


def write_csv(out_dir, rows):
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "wc_schedule.csv")
    fields = [
        "match_number", "date", "kickoff_utc", "kickoff_beijing",
        "stage", "stage_order", "competition_phase", "group_code",
        "home_team", "away_team", "stadium", "host_city",
    ]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    print(f"赛程已写入: {path}（{len(rows)} 场）")
    return path


def main():
    ap = argparse.ArgumentParser(description="拉取 2026 世界杯 104 场赛程骨架")
    ap.add_argument("--out-dir", default=os.path.dirname(os.path.abspath(__file__)))
    args = ap.parse_args()

    print("正在拉取赛程...")
    fixtures = fetch_fixtures()
    print(f"  获取 {len(fixtures)} 场赛程")

    rows = build_schedule_rows(fixtures)
    write_csv(args.out_dir, rows)

    stage_dist = {}
    for r in rows:
        stage_dist[r["stage"]] = stage_dist.get(r["stage"], 0) + 1
    print(f"  阶段分布: {stage_dist}")


if __name__ == "__main__":
    main()
