# 世界杯 1X2 赔率、赛果与策略回测数据

拉取整届 FIFA 世界杯所有已开赛的 1X2 比赛盘（主胜 / 平局 / 客胜），以**开赛前 1 小时**为锚点，计算锚点**之前 3 分钟**内已成交单的成交量加权平均价（VWAP），并附带常规时间赛果，供策略回测使用。

> 数据仅用于市场数据与策略实验，不构成投注建议。

## 一、数据来源

| 用途 | 接口 | 说明 |
|---|---|---|
| 比赛、市场、结算与赛果 | `GET https://gamma-api.polymarket.com/events` | 通过世界杯标签获取比赛事件与三路市场信息 |
| 历史成交 | `GET https://data-api.polymarket.com/trades` | 按 market 的 `conditionId` 获取成交记录，再按 Yes token 筛选方向 |

每场比赛包含主胜、平局、客胜三个 Yes/No 市场；本项目只使用各方向 **Yes token** 的已成交记录。

## 二、赔率取价口径

### 1. 时间锚点

```text
锚点 = 开赛时间 - 1 小时
正式取价窗口 = [锚点 - 3 分钟, 锚点]
```

例如比赛在北京时间 03:00 开始：

```text
锚点：北京时间 02:00
聚合窗口：北京时间 01:57:00 至 02:00:00
```

**不会使用锚点后的任何成交。** 这样可以避免把开赛前 1 小时之后才出现的信息混入回测，造成前视偏差。

### 2. 三路价格与赔率

主胜、平局、客胜分别独立计算：

```text
VWAP = Σ(成交价 × 成交量) / Σ成交量
十进制赔率 = 1 / VWAP
归一化概率 = 某方向 VWAP / 三路 VWAP 之和
```

VWAP 比单笔成交价稳定：大额成交权重更高，不会把一笔极小成交与大额成交等权处理。

### 3. 流动性不足时的处理

| 优先级 | 取价方式 | 质量标记 |
|---|---|---|
| 1 | 锚点前 3 分钟内至少 3 笔成交的 VWAP | `high` |
| 2 | 锚点前 10 分钟内至少 3 笔成交的 VWAP | `medium` |
| 3 | 锚点前 24 小时内最后一笔成交 | `low` |
| 4 | 无可用成交 | `missing`，价格留空 |

所有兜底都限制在锚点之前；不会为了补齐而取锚点之后或赛后的价格。

## 三、赛果口径

Polymarket 的 1X2 市场按**常规时间（90 分钟 + 补时）**结算：

- `event.score`：事件返回的原始比分，淘汰赛中可能与常规时间口径不同，不能单独作为 1X2 赛果依据
- `outcomePrices[0]`：该方向 Yes token 的结算价，`1` 表示命中
- `umaResolutionStatus == "resolved"`：三路均已链上结算时，`settled = Y`

`result_regular` 由三路 Yes token 的结算结果判定，是常规时间 1X2 的权威字段；它不包含加时赛或点球大战的晋级结果。

## 四、输出文件

### `wc_odds.csv`

| 分组 | 字段 | 含义 |
|---|---|---|
| 标识 | `event_id` / `title` / `home_team` / `away_team` / `event_slug` | 比赛标识 |
| 时间 | `kickoff_utc` / `kickoff_beijing` / `target_pre1h_utc` | 开赛时间与赛前一小时锚点 |
| 原始时间 | `raw_endDate` / `raw_startDate` | 用于核对比赛事件时间字段 |
| 原始价格 | `home_price` / `draw_price` / `away_price` | 三路锚点前窗口 VWAP，约等于隐含概率 |
| 取价方法 | `*_price_method` / `*_price_quality` | 使用的聚合或兜底方法及质量等级 |
| 流动性 | `*_trade_count` / `*_trade_volume` | 聚合窗口内成交笔数和累计成交量 |
| 审计 | `*_price_age_seconds` / `*_price_point_utc` / `*_window_start_utc` | 最后一笔成交距锚点时间、成交时间与窗口起点 |
| 赔率 | `home_odds` / `draw_odds` / `away_odds` | 十进制赔率，`1 / price` |
| 概率 | `home_prob_norm` / `draw_prob_norm` / `away_prob_norm` | 三路归一化后的概率 |
| 赛果 | `final_score` / `result_regular` / `match_ended` / `settled` | 常规时间赛果与结算状态 |

### `wc_odds.json`

保留每场原始 event 片段、三路市场标识、每个方向的 VWAP 元数据、归一化概率与赛果，便于复核或二次分析。

## 五、使用方式

```bash
pip install requests

python3 fetch_wc_odds.py             # 重建全部已开赛比赛数据
python3 fetch_wc_odds.py --dry-run   # 仅展示请求
python3 fetch_wc_odds.py --limit 5   # 只处理前 5 场，用于调试
python3 fetch_wc_odds.py --out-dir . # 指定输出目录

python3 fetch_schedule.py            # 拉取 104 场赛程骨架（含阶段、小组）
python3 build_factors.py             # 合并赔率+赛程+球队，生成因子表
python3 backtest/backtest.py         # 用最新数据重跑 7 个基准策略
```

## 六、比赛因子

在赔率数据之上，项目通过 `build_factors.py` 补充三类赛前因子，输出到 `wc_odds_factors.csv`，用于按条件筛选回测。

### 1. 静态比赛因子

| 字段 | 含义 |
|---|---|
| `match_number` / `stage` / `stage_order` / `competition_phase` | 官方比赛编号、阶段（group-stage 至 final） |
| `group_code` / `group_matchday` | 小组赛分组（A–L）与轮次（1–3） |
| `is_elimination_match` | 是否单场淘汰赛 |
| `stadium` / `host_city` | 球场与主办城市 |
| `home_country_code` / `home_country_zh` | 主队 ISO/FIFA 代码与中文国名 |
| `home_confederation` / `home_continent` | 主队所属足球联合会（UEFA/AFC/CAF/CONCACAF/CONMEBOL/OFC）与地理大洲 |
| `home_fifa_ranking` / `home_is_host` | 主队 FIFA 排名与是否东道主 |
| `away_*` | 对应客队字段 |
| `same_confederation` / `same_continent` | 双方是否同联合会 / 同大洲 |
| `fifa_ranking_diff` | 主队排名减客队排名（正值表示主队排名数值更大、名次更靠后） |

### 2. 赛前市场因子

全部由赛前一小时锚点前 VWAP 计算，不含赛后信息。

| 字段 | 含义 |
|---|---|
| `favorite_side` / `favorite_prob_norm` | 市场热门方向与归一化概率 |
| `underdog_prob_norm` | 冷门方向归一化概率 |
| `home_away_prob_diff` | 主胜概率减客胜概率 |
| `top2_prob_gap` | 第一热门与第二热门的概率差 |
| `market_entropy` | 三路概率的信息熵，衡量市场分歧程度 |
| `total_trade_count` / `total_trade_volume` | 三路累计成交笔数与成交量 |
| `quote_quality_min` | 三路中最低报价质量（high / medium / low / missing） |

### 3. 赛前球队状态因子

按时间顺序逐场计算，只使用当前比赛开赛前已结束的比赛。

| 字段 | 含义 |
|---|---|
| `home_rest_days` / `away_rest_days` | 距各自上一场比赛的休息天数 |
| `rest_days_diff` | 主队休息天数减客队 |
| `home_matches_played_prior` / `away_matches_played_prior` | 本届赛事本场前已参赛场数 |

### 数据来源

| 数据 | 来源 | 更新频率 |
|---|---|---|
| 104 场赛程骨架 | TheStatsAPI `fixtures.json`（免费、CORS 可用） | 一次性 |
| 球队映射（国家、联合会、FIFA 排名） | Wikipedia + `teams.csv` 本地映射 | 一次性 |
| 赔率与赛果 | Polymarket 公开 API | 按需手动重建 |

## 七、历届世界杯平局趋势

项目补充了 2010、2014、2018、2022 四届世界杯逐场赛果，并与 2026 年数据统一为“90 分钟 + 补时”的常规时间口径。

- 历史数据源：`openfootball/worldcup.json`（Public Domain，无需 key）
- 历史样本：4 届 × 64 场，共 256 场
- 比分口径：`score.ft`；加时赛 `score.et`、点球大战 `score.p` 单独保存
- 基础因子：阶段、小组轮次、双方联合会/大洲、东道主、休息天数、赛前累计场数、小组赛前积分和净胜球
- 历史 Polymarket 逐场 1X2 赔率未找到完整可靠归档，因此不做历史赔率收益回测

```bash
python3 fetch_historical_worldcups.py # 重建 2010-2022 历史逐场数据
python3 analyze_worldcup_draws.py      # 合并 2026 并生成趋势报告
```

核心描述性结果：2026 小组赛平局率 27.78%，高于 2010-2022 合并样本的 21.88%；淘汰赛常规时间平局率 28.12%，低于历史合并样本的 34.38%。但两项差异均未达到 5% 统计显著性，不能据此断言长期结构已经改变。

## 八、方法检查与已知边界

1. **已结算市场的历史价格来源**：原 CLOB `prices-history` 接口在市场结算后可能返回空历史，因此项目统一以 Data API 的已成交记录重建历史价格。
2. **时间一致性**：取价窗口固定相对于“开赛前 1 小时”锚点，而不是相对于开赛时间前 3 分钟。
3. **无前视偏差**：所有回测价格只使用锚点及之前的成交；对称窗口或锚点后价格仅能用于描述性分析，不可用于回测。
4. **市场价格不等于真实可成交赔率**：VWAP 来自历史成交，忽略了下单时的盘口深度、滑点、手续费与资金限制；回测收益不代表实际可实现收益。
5. **归一化不等于去除全部交易成本**：三路归一化只将概率和调整为 1，无法还原真实交易费用或买卖价差。
6. **赛果范围**：仅覆盖常规时间 1X2；淘汰赛的加时赛和点球大战不在 `result_regular` 中。
7. **样本独立性有限**：单届世界杯比赛数有限，策略的正负 ROI 可能主要由偶然性驱动，需在更多赛事或滚动样本中验证。

## 九、最近一次数据扫描（2026-07-20）

- 全届 1X2 比赛：**104 场**；全部已结束、已结算，三路价格均完整。
- 已新增北京时间 **2026-07-20 03:00** 的决赛 `Spain vs. Argentina`：Polymarket 1X2 按常规时间结算为平局；事件原始比分 `1-0` 是包含加时后的总比分。
- 决赛赛前一小时锚点价格：西班牙胜 `0.4274`、平局 `0.3200`、阿根廷胜 `0.2625`；三路均为 `high` 质量。
- 三路报价以 3 分钟 VWAP 为主；6 个方向因流动性较低降级为 10 分钟 VWAP 或最后一笔赛前成交。
- 默认回测排除两场含 `low` 质量方向的比赛，因此当前可回测样本为 **102 场**。
- 已检查所有 312 个方向的成交时间，**未发现任何锚点后的成交进入取价窗口**。

## 十、手动重建

本项目不做自动更新，比赛结束后按需手动重跑即可：

```bash
python3 fetch_wc_odds.py     # 拉取最新已结束比赛的赔率
python3 build_factors.py     # 重建因子表
python3 backtest/backtest.py # 重跑回测
```

## 十一、目录结构

```text
world_cup_odds/
├── fetch_wc_odds.py          # 数据拉取与锚点前 VWAP 聚合脚本
├── fetch_schedule.py         # 拉取 104 场赛程骨架（TheStatsAPI）
├── build_factors.py          # 合并赔率+赛程+球队，生成因子表
├── fetch_historical_worldcups.py # 拉取并标准化 2010-2022 世界杯赛果
├── analyze_worldcup_draws.py # 合并五届数据并生成平局趋势报告
├── teams.csv                 # 48 队标准化映射（国家、联合会、FIFA 排名）
├── wc_odds.csv               # 结构化赔率、赛果和流动性质量表
├── wc_odds.json              # 原始市场与聚合明细
├── wc_odds_factors.csv       # 含全部因子的完整表（82 字段）
├── wc_schedule.csv           # 104 场赛程骨架
├── README.md                 # 项目方法、使用方式与边界
├── historical/
│   ├── historical_worldcups_matches.csv # 2010-2022 四届逐场数据
│   ├── historical_worldcups_summary.csv # 四届分阶段平局摘要
│   ├── worldcup_draw_trend_summary.csv  # 2010-2026 趋势汇总
│   ├── worldcup_draw_factor_summary.csv # 分因子平局率汇总
│   ├── worldcup_draw_stat_tests.json     # 2026 与历史合并样本的两比例 z 检验
│   └── worldcup_draw_trend_report.html  # 可视化报告
└── backtest/
    ├── backtest.py           # 7 个基准策略回测脚本
    ├── backtest_summary.csv  # 策略汇总
    ├── backtest_detail.csv   # 逐笔下注明细
    └── README.md             # 回测说明
```