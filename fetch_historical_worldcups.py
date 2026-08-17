#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拉取并标准化 2002、2006、2010、2014、2018、2022 世界杯逐场赛果。

数据源：openfootball/worldcup.json（Public Domain，无需 API key）
重点口径：score.ft 为 90 分钟 + 补时的常规时间比分；淘汰赛平局按 ft 判断，
不把加时赛或点球大战结果混入常规时间赛果。

输出：
  historical_worldcups_matches.csv  六届共 384 场逐场数据与基础因子
  historical_worldcups_summary.csv  各届、各阶段、各小组轮次的平局率摘要
"""

import argparse
import csv
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

YEARS = (2002, 2006, 2010, 2014, 2018, 2022)
URL = "https://raw.githubusercontent.com/openfootball/worldcup.json/master/{year}/worldcup.json"
HOST_CODES = {2002: {"KOR", "JPN"}, 2006: {"GER"}, 2010: {"RSA"}, 2014: {"BRA"}, 2018: {"RUS"}, 2022: {"QAT"}}
DEFAULT_UTC_OFFSET = {2002: 9, 2006: 2, 2010: 2, 2014: -3, 2018: 3, 2022: 3}

TEAM_META = {
    "Algeria": ("ALG", "CAF", "Africa"), "Angola": ("ANG", "CAF", "Africa"),
    "Argentina": ("ARG", "CONMEBOL", "South America"), "Australia": ("AUS", "AFC", "Oceania"),
    "Belgium": ("BEL", "UEFA", "Europe"), "Bosnia-Herzegovina": ("BIH", "UEFA", "Europe"),
    "Bosnia and Herzegovina": ("BIH", "UEFA", "Europe"),     "Brazil": ("BRA", "CONMEBOL", "South America"),
    "Cameroon": ("CMR", "CAF", "Africa"), "Canada": ("CAN", "CONCACAF", "North America"),
    "China": ("CHN", "AFC", "Asia"), "Czech Republic": ("CZE", "UEFA", "Europe"),
    "Chile": ("CHI", "CONMEBOL", "South America"), "Colombia": ("COL", "CONMEBOL", "South America"),
    "Costa Rica": ("CRC", "CONCACAF", "North America"), "Croatia": ("CRO", "UEFA", "Europe"),
    "Côte d'Ivoire": ("CIV", "CAF", "Africa"), "Denmark": ("DEN", "UEFA", "Europe"),
    "Ecuador": ("ECU", "CONMEBOL", "South America"), "Egypt": ("EGY", "CAF", "Africa"),
    "England": ("ENG", "UEFA", "Europe"), "France": ("FRA", "UEFA", "Europe"),
    "Germany": ("GER", "UEFA", "Europe"), "Ghana": ("GHA", "CAF", "Africa"),
    "Greece": ("GRE", "UEFA", "Europe"), "Honduras": ("HON", "CONCACAF", "North America"),
    "Iceland": ("ISL", "UEFA", "Europe"), "Iran": ("IRN", "AFC", "Asia"),
    "IR Iran": ("IRN", "AFC", "Asia"), "Ireland": ("IRL", "UEFA", "Europe"), "Italy": ("ITA", "UEFA", "Europe"),
    "Japan": ("JPN", "AFC", "Asia"), "Mexico": ("MEX", "CONCACAF", "North America"),
    "Morocco": ("MAR", "CAF", "Africa"), "Netherlands": ("NED", "UEFA", "Europe"),
    "New Zealand": ("NZL", "OFC", "Oceania"), "Nigeria": ("NGA", "CAF", "Africa"),
    "North Korea": ("PRK", "AFC", "Asia"), "Panama": ("PAN", "CONCACAF", "North America"),
    "Paraguay": ("PAR", "CONMEBOL", "South America"), "Peru": ("PER", "CONMEBOL", "South America"),
    "Poland": ("POL", "UEFA", "Europe"), "Portugal": ("POR", "UEFA", "Europe"),
    "Qatar": ("QAT", "AFC", "Asia"), "Russia": ("RUS", "UEFA", "Europe"),
    "Saudi Arabia": ("KSA", "AFC", "Asia"), "Senegal": ("SEN", "CAF", "Africa"),
    "Serbia": ("SRB", "UEFA", "Europe"), "Serbia and Montenegro": ("SCG", "UEFA", "Europe"), "Slovakia": ("SVK", "UEFA", "Europe"),
    "Slovenia": ("SVN", "UEFA", "Europe"), "South Africa": ("RSA", "CAF", "Africa"),
    "South Korea": ("KOR", "AFC", "Asia"), "Spain": ("ESP", "UEFA", "Europe"),
    "Sweden": ("SWE", "UEFA", "Europe"), "Switzerland": ("SUI", "UEFA", "Europe"),
    "Togo": ("TOG", "CAF", "Africa"), "Trinidad and Tobago": ("TRI", "CONCACAF", "North America"), "Tunisia": ("TUN", "CAF", "Africa"),
    "Turkey": ("TUR", "UEFA", "Europe"), "Ukraine": ("UKR", "UEFA", "Europe"),
    "United States": ("USA", "CONCACAF", "North America"), "USA": ("USA", "CONCACAF", "North America"),
    "Uruguay": ("URU", "CONMEBOL", "South America"), "Wales": ("WAL", "UEFA", "Europe"),
}

STAGE_MAP = {
    "Round of 16": "round_of_16",
    "Quarterfinals": "quarter_final", "Quarter-finals": "quarter_final",
    "Semifinals": "semi_final", "Semi-finals": "semi_final",
    "Third-place play-off": "third_place", "Match for third place": "third_place",
    "Final": "final",
}


def fetch_year(year):
    response = requests.get(URL.format(year=year), timeout=30)
    response.raise_for_status()
    return response.json().get("matches", [])


def infer_group_round(matches):
    """在每个小组内按球队已出场次数推断第 1/2/3 轮。"""
    by_group = defaultdict(list)
    for index, match in enumerate(matches):
        if match.get("group"):
            by_group[match["group"]].append((index, match))

    round_by_index = {}
    for items in by_group.values():
        items.sort(key=lambda x: (x[1].get("date", ""), x[1].get("time", ""), x[0]))
        played = defaultdict(int)
        for index, match in items:
            team_round = max(played[match["team1"]], played[match["team2"]]) + 1
            round_by_index[index] = team_round
            played[match["team1"]] += 1
            played[match["team2"]] += 1
    return round_by_index


def meta(team):
    return TEAM_META.get(team, ("", "", ""))


def kickoff_utc(year, date_text, time_text):
    """把数据源中的当地时间和可选 UTC 偏移转换成 UTC ISO 字符串。"""
    if not date_text or not time_text:
        return ""
    time_part = time_text[:5]
    offset = DEFAULT_UTC_OFFSET[year]
    match = re.search(r"UTC([+-]\d+)", time_text)
    if match:
        offset = int(match.group(1))
    try:
        local_dt = datetime.strptime(f"{date_text} {time_part}", "%Y-%m-%d %H:%M")
        utc_dt = local_dt.replace(tzinfo=timezone(timedelta(hours=offset))).astimezone(timezone.utc)
        return utc_dt.isoformat()
    except ValueError:
        return ""


def add_prior_state(rows):
    """严格按开赛时间添加赛前球队状态，不使用当前场或未来比赛。"""
    rows.sort(key=lambda r: (r["kickoff_utc"], r["match_index"]))
    last_kickoff = {}
    matches_played = defaultdict(int)
    group_points = defaultdict(int)
    group_goal_diff = defaultdict(int)

    for row in rows:
        current = datetime.fromisoformat(row["kickoff_utc"]) if row["kickoff_utc"] else None
        home = row["home_team"]
        away = row["away_team"]

        for side, team in (("home", home), ("away", away)):
            previous = last_kickoff.get(team)
            row[f"{side}_rest_days"] = round((current - previous).total_seconds() / 86400, 2) if current and previous else ""
            row[f"{side}_matches_played_prior"] = matches_played[team]
            row[f"{side}_group_points_prior"] = group_points[team] if row["competition_phase"] == "group" else ""
            row[f"{side}_group_goal_diff_prior"] = group_goal_diff[team] if row["competition_phase"] == "group" else ""

        if row["home_rest_days"] != "" and row["away_rest_days"] != "":
            row["rest_days_diff"] = round(row["home_rest_days"] - row["away_rest_days"], 2)
        else:
            row["rest_days_diff"] = ""

        if current:
            last_kickoff[home] = current
            last_kickoff[away] = current
        matches_played[home] += 1
        matches_played[away] += 1

        if row["competition_phase"] == "group":
            home_goals = row["score_regular_home"]
            away_goals = row["score_regular_away"]
            group_goal_diff[home] += home_goals - away_goals
            group_goal_diff[away] += away_goals - home_goals
            if home_goals == away_goals:
                group_points[home] += 1
                group_points[away] += 1
            elif home_goals > away_goals:
                group_points[home] += 3
            else:
                group_points[away] += 3
    return rows


def build_rows(year, matches):
    group_rounds = infer_group_round(matches)
    rows = []
    for index, match in enumerate(matches):
        score = match.get("score", {})
        ft = score.get("ft")
        if not ft or len(ft) != 2:
            continue
        is_group = bool(match.get("group"))
        stage = "group_stage" if is_group else STAGE_MAP.get(match.get("round"), "knockout_other")
        team1 = match["team1"]
        team2 = match["team2"]
        c1, conf1, cont1 = meta(team1)
        c2, conf2, cont2 = meta(team2)
        rows.append({
            "year": year,
            "match_index": index + 1,
            "date": match.get("date", ""),
            "time": match.get("time", ""),
            "kickoff_utc": kickoff_utc(year, match.get("date", ""), match.get("time", "")),
            "stage": stage,
            "competition_phase": "group" if is_group else "knockout",
            "group_code": (match.get("group") or "").replace("Group ", ""),
            "group_matchday": group_rounds.get(index, "") if is_group else "",
            "home_team": team1,
            "away_team": team2,
            "home_country_code": c1,
            "away_country_code": c2,
            "home_is_host": "Y" if c1 in HOST_CODES[year] else "N",
            "away_is_host": "Y" if c2 in HOST_CODES[year] else "N",
            "home_confederation": conf1,
            "away_confederation": conf2,
            "home_continent": cont1,
            "away_continent": cont2,
            "same_confederation": "Y" if conf1 and conf1 == conf2 else "N",
            "same_continent": "Y" if cont1 and cont1 == cont2 else "N",
            "score_regular_home": ft[0],
            "score_regular_away": ft[1],
            "score_regular": f"{ft[0]}-{ft[1]}",
            "result_regular": "draw" if ft[0] == ft[1] else ("home" if ft[0] > ft[1] else "away"),
            "is_draw_regular": "Y" if ft[0] == ft[1] else "N",
            "went_extra_time": "Y" if "et" in score else "N",
            "went_penalties": "Y" if "p" in score else "N",
            "score_extra_time": "-".join(map(str, score.get("et", []))) if score.get("et") else "",
            "score_penalties": "-".join(map(str, score.get("p", []))) if score.get("p") else "",
            "ground": match.get("ground", ""),
            "source": "openfootball/worldcup.json",
        })
    return add_prior_state(rows)


def summarize(rows):
    summary = []
    for year in YEARS:
        yr = [r for r in rows if r["year"] == year]
        buckets = [
            ("all", yr),
            ("group_all", [r for r in yr if r["competition_phase"] == "group"]),
            ("group_md1", [r for r in yr if r["group_matchday"] == 1]),
            ("group_md2", [r for r in yr if r["group_matchday"] == 2]),
            ("group_md3", [r for r in yr if r["group_matchday"] == 3]),
            ("knockout_all", [r for r in yr if r["competition_phase"] == "knockout"]),
        ]
        for segment, sample in buckets:
            draws = sum(r["is_draw_regular"] == "Y" for r in sample)
            summary.append({
                "year": year,
                "segment": segment,
                "matches": len(sample),
                "draws_regular": draws,
                "draw_rate_pct": round(draws / len(sample) * 100, 2) if sample else "",
            })
    return summary


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(description="拉取历届世界杯常规时间赛果与基础因子")
    parser.add_argument("--out-dir", default=os.path.join(here, "historical"))
    args = parser.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    all_rows = []
    for year in YEARS:
        matches = fetch_year(year)
        rows = build_rows(year, matches)
        all_rows.extend(rows)
        print(f"{year}: 原始 {len(matches)} 场，标准化 {len(rows)} 场")

    summary = summarize(all_rows)
    matches_path = os.path.join(args.out_dir, "historical_worldcups_matches.csv")
    summary_path = os.path.join(args.out_dir, "historical_worldcups_summary.csv")
    write_csv(matches_path, all_rows)
    write_csv(summary_path, summary)
    print(f"逐场数据 -> {matches_path}")
    print(f"摘要数据 -> {summary_path}")


if __name__ == "__main__":
    main()
