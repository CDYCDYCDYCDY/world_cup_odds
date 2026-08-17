#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""比较 2010-2026 世界杯常规时间平局率并生成 CSV 与 HTML 报告。"""

import csv
import json
import math
import os
from collections import defaultdict


def load_csv(path):
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def wilson(draws, matches, z=1.96):
    if matches == 0:
        return "", ""
    p = draws / matches
    denominator = 1 + z * z / matches
    center = (p + z * z / (2 * matches)) / denominator
    margin = z * math.sqrt(p * (1 - p) / matches + z * z / (4 * matches * matches)) / denominator
    return round((center - margin) * 100, 2), round((center + margin) * 100, 2)


def two_prop_test(x1, n1, x0, n0):
    p1 = x1 / n1
    p0 = x0 / n0
    pooled = (x1 + x0) / (n1 + n0)
    se = math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n0))
    z = (p1 - p0) / se if se else 0.0
    p_value = math.erfc(abs(z) / math.sqrt(2))
    return round((p1 - p0) * 100, 2), round(z, 3), round(p_value, 4)


def normalize_historical(rows):
    result = []
    for r in rows:
        result.append({
            "year": int(r["year"]),
            "competition_phase": r["competition_phase"],
            "stage": r["stage"],
            "group_matchday": r["group_matchday"],
            "is_draw_regular": r["is_draw_regular"],
            "same_confederation": r["same_confederation"],
            "same_continent": r["same_continent"],
            "host_involved": "Y" if r["home_is_host"] == "Y" or r["away_is_host"] == "Y" else "N",
            "confederation_pair": " × ".join(sorted([r["home_confederation"], r["away_confederation"]])),
        })
    return result


def normalize_2026(rows):
    result = []
    for r in rows:
        result.append({
            "year": 2026,
            "competition_phase": r["competition_phase"],
            "stage": r["stage"].replace("-", "_"),
            "group_matchday": r["group_matchday"],
            "is_draw_regular": "Y" if r["result_regular"] == "draw" else "N",
            "same_confederation": r["same_confederation"],
            "same_continent": r["same_continent"],
            "host_involved": "Y" if r["home_is_host"] == "Y" or r["away_is_host"] == "Y" else "N",
            "confederation_pair": " × ".join(sorted([r["home_confederation"], r["away_confederation"]])),
        })
    return result


def segments_for_year(rows, year):
    yr = [r for r in rows if r["year"] == year]
    return {
        "all": yr,
        "group_all": [r for r in yr if r["competition_phase"] == "group"],
        "group_md1": [r for r in yr if r["group_matchday"] == "1"],
        "group_md2": [r for r in yr if r["group_matchday"] == "2"],
        "group_md3": [r for r in yr if r["group_matchday"] == "3"],
        "knockout_all": [r for r in yr if r["competition_phase"] == "knockout"],
    }


def build_summary(rows):
    output = []
    for year in (2010, 2014, 2018, 2022, 2026):
        for segment, sample in segments_for_year(rows, year).items():
            draws = sum(r["is_draw_regular"] == "Y" for r in sample)
            low, high = wilson(draws, len(sample))
            output.append({
                "year": year,
                "segment": segment,
                "matches": len(sample),
                "draws_regular": draws,
                "draw_rate_pct": round(draws / len(sample) * 100, 2) if sample else "",
                "ci95_low_pct": low,
                "ci95_high_pct": high,
            })
    return output


def build_factor_summary(rows):
    output = []
    dimensions = {
        "competition_phase": lambda r: r["competition_phase"],
        "group_matchday": lambda r: r["group_matchday"] or "not_group",
        "same_confederation": lambda r: r["same_confederation"],
        "same_continent": lambda r: r["same_continent"],
        "host_involved": lambda r: r["host_involved"],
        "confederation_pair": lambda r: r["confederation_pair"],
    }
    for year in (2010, 2014, 2018, 2022, 2026):
        yr = [r for r in rows if r["year"] == year]
        for dimension, getter in dimensions.items():
            buckets = defaultdict(list)
            for r in yr:
                buckets[getter(r)].append(r)
            for value, sample in sorted(buckets.items()):
                draws = sum(r["is_draw_regular"] == "Y" for r in sample)
                output.append({
                    "year": year,
                    "dimension": dimension,
                    "value": value,
                    "matches": len(sample),
                    "draws_regular": draws,
                    "draw_rate_pct": round(draws / len(sample) * 100, 2),
                })
    return output


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def make_report(path, summary, tests, current_count, final_included):
    years = [2010, 2014, 2018, 2022, 2026]
    lookup = {(int(r["year"]), r["segment"]): r for r in summary}
    group_current = lookup[(2026, "group_all")]["draw_rate_pct"]
    md1_current = lookup[(2026, "group_md1")]["draw_rate_pct"]
    md3_current = lookup[(2026, "group_md3")]["draw_rate_pct"]
    knockout_current = lookup[(2026, "knockout_all")]["draw_rate_pct"]
    group_history = tests["group"]["history_draws"] / tests["group"]["history_matches"] * 100
    md1_history = tests["md1"]["history_draws"] / tests["md1"]["history_matches"] * 100
    knockout_history = tests["knockout"]["history_draws"] / tests["knockout"]["history_matches"] * 100
    coverage_text = "全届 104 场，决赛已计入" if final_included else f"截至当前共 {current_count} 场，决赛尚未计入"
    group_rates = [lookup[(y, "group_all")]["draw_rate_pct"] for y in years]
    knockout_rates = [lookup[(y, "knockout_all")]["draw_rate_pct"] for y in years]
    md1 = [lookup[(y, "group_md1")]["draw_rate_pct"] for y in years]
    md2 = [lookup[(y, "group_md2")]["draw_rate_pct"] for y in years]
    md3 = [lookup[(y, "group_md3")]["draw_rate_pct"] for y in years]

    rows_html = "".join(
        f"<tr><td>{y}</td><td>{lookup[(y,'group_all')]['draws_regular']}/{lookup[(y,'group_all')]['matches']}</td>"
        f"<td>{lookup[(y,'group_all')]['draw_rate_pct']:.2f}%</td>"
        f"<td>{lookup[(y,'group_md1')]['draw_rate_pct']:.2f}%</td>"
        f"<td>{lookup[(y,'group_md2')]['draw_rate_pct']:.2f}%</td>"
        f"<td>{lookup[(y,'group_md3')]['draw_rate_pct']:.2f}%</td>"
        f"<td>{lookup[(y,'knockout_all')]['draws_regular']}/{lookup[(y,'knockout_all')]['matches']}</td>"
        f"<td>{lookup[(y,'knockout_all')]['draw_rate_pct']:.2f}%</td></tr>"
        for y in years
    )

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>2010-2026 世界杯常规时间平局率趋势</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<style>
:root{{--red:#c83e3e;--green:#23845b;--blue:#315f9d;--ink:#1f2937;--muted:#64748b;--line:#e2e8f0;--bg:#f7f8fa;--card:#fff}}*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif;line-height:1.6}}main{{max-width:1080px;margin:auto;padding:32px 24px 56px}}h1{{font-size:28px;margin:0 0 6px}}h2{{font-size:20px;margin:32px 0 14px}}.sub{{color:var(--muted);font-size:14px}}.cards{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin:24px 0}}.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px}}.card b{{display:block;font-size:26px;margin:4px 0}}.up{{color:var(--red)}}.down{{color:var(--green)}}.note{{background:#fff7ed;border-left:4px solid #d97706;padding:14px 16px;border-radius:6px;margin:18px 0}}.chart{{height:360px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:8px}}table{{width:100%;border-collapse:collapse;background:var(--card);font-size:14px}}th,td{{padding:10px 12px;border-bottom:1px solid var(--line);text-align:right}}th:first-child,td:first-child{{text-align:left}}th{{background:#f1f5f9}}.conclusion{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px}}.source{{font-size:13px;color:var(--muted)}}@media(max-width:720px){{.cards{{grid-template-columns:1fr}}main{{padding:20px 12px}}.chart{{height:320px;overflow:hidden}}table{{font-size:12px}}th,td{{padding:8px 5px}}}}
</style></head><body><main>
<h1>2010-2026 世界杯常规时间平局率趋势</h1>
<div class="sub">只看 90 分钟 + 补时赛果；不把加时赛或点球大战算入常规时间胜负。2026 年{coverage_text}。</div>
<div class="cards">
<div class="card"><span>2026 小组赛平局率</span><b class="up">{group_current:.2f}%</b><small>历史四届合计 {group_history:.2f}%，高 {tests['group']['difference_pp']:.2f} 个百分点</small></div>
<div class="card"><span>2026 小组首轮平局率</span><b class="up">{md1_current:.2f}%</b><small>历史四届合计 {md1_history:.2f}%，高 {tests['md1']['difference_pp']:.2f} 个百分点</small></div>
<div class="card"><span>2026 淘汰赛平局率</span><b class="down">{knockout_current:.2f}%</b><small>历史四届合计 {knockout_history:.2f}%，差异 {tests['knockout']['difference_pp']:+.2f} 个百分点</small></div>
</div>
<div class="note"><b>结论先行：</b>2026 年的方向确实符合“小组赛平局更多、淘汰赛平局更少”的预设，但不能称为清晰的长期趋势。小组赛平局率是从 2014/2018 低位反弹，尚未超过 2010；淘汰赛则回到接近 2010 的水平。与 2010-2022 合并样本相比，两项差异的 p 值分别为 {tests['group']['p_value']:.4f} 和 {tests['knockout']['p_value']:.4f}，均未达到常用 5% 显著性标准。</div>
<h2>小组赛与淘汰赛平局率</h2><div id="trend" class="chart"></div>
<h2>小组赛第 1 / 2 / 3 轮</h2><div id="rounds" class="chart"></div>
<h2>精确数据</h2><div style="overflow:auto"><table><thead><tr><th>年份</th><th>小组平局</th><th>小组平局率</th><th>第1轮</th><th>第2轮</th><th>第3轮</th><th>淘汰赛平局</th><th>淘汰赛平局率</th></tr></thead><tbody>{rows_html}</tbody></table></div>
<h2>如何理解</h2><div class="conclusion"><ol>
<li><b>小组赛：</b>2010 为 29.17%，2014/2018 降至 18.75%，2022 回升至 20.83%，2026 为 {group_current:.2f}%。形状更像“先降后升”，而不是单调上升。</li>
<li><b>第一轮：</b>2026 的 {md1_current:.2f}% 很高，但与 2010 完全相同；它是高位重现，不是历史新高。</li>
<li><b>第三轮：</b>2026 为 {md3_current:.2f}%，明显高于 2022 的 6.25%，但并未超过 2010/2018 的 25.00%。</li>
<li><b>淘汰赛：</b>2026 为 {knockout_current:.2f}%，与 2010 的 25.00% 接近，因此也不是持续下降。</li>
<li><b>样本边界：</b>每届旧赛制小组轮次只有 16 场，2026 为 24 场；淘汰赛 2026 因扩军共 {lookup[(2026, 'knockout_all')]['matches']} 场。单届比例容易被数场比赛显著改变。</li>
</ol></div>
<h2>数据来源与口径</h2><p class="source">2010/2014/2018/2022：openfootball/worldcup.json，Public Domain；每届 64 场。2026：本项目 Polymarket 已结算 1X2 赛果，共 {current_count} 场。历史 Polymarket 完整逐场 1X2 赔率未找到可靠归档，因此本报告不做历史赔率或收益回测。所有淘汰赛均按 score.ft / result_regular 判断常规时间平局。</p>
<script>
const years={json.dumps(years)};
echarts.init(document.getElementById('trend')).setOption({{
 tooltip:{{trigger:'axis',valueFormatter:v=>v.toFixed(2)+'%'}},legend:{{data:['小组赛','淘汰赛']}},
 xAxis:{{type:'category',data:years}},yAxis:{{type:'value',name:'平局率',min:0,max:55,axisLabel:{{formatter:'{{value}}%'}}}},
 series:[
  {{name:'小组赛',type:'line',data:{json.dumps(group_rates)},symbol:'circle',symbolSize:8,lineStyle:{{width:3,color:'#c83e3e'}},itemStyle:{{color:'#c83e3e'}}}},
  {{name:'淘汰赛',type:'line',data:{json.dumps(knockout_rates)},symbol:'diamond',symbolSize:9,lineStyle:{{width:3,color:'#315f9d'}},itemStyle:{{color:'#315f9d'}}}}
 ]
}});
echarts.init(document.getElementById('rounds')).setOption({{
 tooltip:{{trigger:'axis',axisPointer:{{type:'shadow'}},valueFormatter:v=>v.toFixed(2)+'%'}},legend:{{data:['第1轮','第2轮','第3轮']}},
 xAxis:{{type:'category',data:years}},yAxis:{{type:'value',name:'平局率',min:0,max:45,axisLabel:{{formatter:'{{value}}%'}}}},
 series:[
  {{name:'第1轮',type:'bar',data:{json.dumps(md1)},itemStyle:{{color:'#c83e3e'}}}},
  {{name:'第2轮',type:'bar',data:{json.dumps(md2)},itemStyle:{{color:'#d9985f'}}}},
  {{name:'第3轮',type:'bar',data:{json.dumps(md3)},itemStyle:{{color:'#315f9d'}}}}
 ]
}});
window.addEventListener('resize',()=>{{echarts.getInstanceByDom(document.getElementById('trend')).resize();echarts.getInstanceByDom(document.getElementById('rounds')).resize();}});
</script></main></body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    hist_dir = os.path.join(here, "historical")
    historical = normalize_historical(load_csv(os.path.join(hist_dir, "historical_worldcups_matches.csv")))
    current = normalize_2026(load_csv(os.path.join(here, "wc_odds_factors.csv")))
    combined = historical + current

    summary = build_summary(combined)
    factor_summary = build_factor_summary(combined)
    write_csv(os.path.join(hist_dir, "worldcup_draw_trend_summary.csv"), summary)
    write_csv(os.path.join(hist_dir, "worldcup_draw_factor_summary.csv"), factor_summary)

    lookup = {(int(r["year"]), r["segment"]): r for r in summary}
    tests = {}
    comparison_segments = {
        "group": "group_all", "md1": "group_md1", "md2": "group_md2",
        "md3": "group_md3", "knockout": "knockout_all",
    }
    for name, segment in comparison_segments.items():
        current_row = lookup[(2026, segment)]
        history_rows = [lookup[(year, segment)] for year in (2010, 2014, 2018, 2022)]
        x0 = sum(int(r["draws_regular"]) for r in history_rows)
        n0 = sum(int(r["matches"]) for r in history_rows)
        diff, z, p_value = two_prop_test(int(current_row["draws_regular"]), int(current_row["matches"]), x0, n0)
        tests[name] = {"difference_pp": diff, "z": z, "p_value": p_value, "history_draws": x0, "history_matches": n0}

    with open(os.path.join(hist_dir, "worldcup_draw_stat_tests.json"), "w", encoding="utf-8") as f:
        json.dump(tests, f, ensure_ascii=False, indent=2)

    report_path = os.path.join(hist_dir, "worldcup_draw_trend_report.html")
    final_included = any(r["stage"] == "final" for r in current)
    make_report(report_path, summary, tests, len(current), final_included)
    print(f"趋势摘要 -> {os.path.join(hist_dir, 'worldcup_draw_trend_summary.csv')}")
    print(f"因子摘要 -> {os.path.join(hist_dir, 'worldcup_draw_factor_summary.csv')}")
    print(f"统计检验 -> {os.path.join(hist_dir, 'worldcup_draw_stat_tests.json')}")
    print(f"可视化报告 -> {report_path}")
    print(json.dumps(tests, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
