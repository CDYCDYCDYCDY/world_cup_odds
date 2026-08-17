#!/usr/bin/env python3
"""Convert wc_odds_factors.csv → data.js for the web app."""
import csv, json, os

csv_path = os.path.join(os.path.dirname(__file__), '..', 'wc_odds_factors.csv')
rows = []

with open(csv_path, encoding='utf-8-sig') as f:
    reader = csv.DictReader(f)
    for r in reader:
        if r.get('settled') != 'Y':
            continue
        if r.get('result_regular') not in ('home', 'draw', 'away'):
            continue
        try:
            ho = float(r['home_odds'])
            do = float(r['draw_odds'])
            ao = float(r['away_odds'])
        except (ValueError, KeyError):
            continue

        matchday_raw = r.get('group_matchday', '') or ''
        try:
            matchday = int(matchday_raw)
        except ValueError:
            matchday = 0

        rows.append({
            'n': int(r.get('match_number', 0) or 0),
            'title': r.get('title', ''),
            'home': r.get('home_team', ''),
            'away': r.get('away_team', ''),
            'homeZh': r.get('home_country_zh', r.get('home_team', '')),
            'awayZh': r.get('away_country_zh', r.get('away_team', '')),
            'stage': r.get('stage', ''),
            'stageOrder': int(r.get('stage_order', 0) or 0),
            'matchday': matchday,
            'group': r.get('group_code', ''),
            'homeOdds': round(ho, 4),
            'drawOdds': round(do, 4),
            'awayOdds': round(ao, 4),
            'result': r.get('result_regular', ''),
            'score': r.get('final_score', ''),
            'homeRank': int(r.get('home_fifa_ranking', 0) or 0),
            'awayRank': int(r.get('away_fifa_ranking', 0) or 0),
            'rankDiff': int(r.get('fifa_ranking_diff', 0) or 0),
            'favorite': r.get('favorite_side', ''),
            'kickoff': r.get('kickoff_beijing', ''),
        })

rows.sort(key=lambda x: x['n'])

out_path = os.path.join(os.path.dirname(__file__), 'data.js')
with open(out_path, 'w', encoding='utf-8') as f:
    f.write('window.MATCH_DATA = ')
    json.dump(rows, f, ensure_ascii=False, indent=1)
    f.write(';\n')

print(f'Generated {out_path} with {len(rows)} matches')
