# QuantDB 全量数据 · 单股研报级分析框架设计

> 设计目标：给定任意 A 股，把 QuantDB 能提供的**全部**证据维度组织成一份可复现、可证伪、媲美券商研报的深度分析。
> 原则：**每一层都有数据端点 + 具体字段 + 明确的分析问题 + 写死的计算公式**，拒绝"用大而全的数据做泛泛之谈"。
> 单位/口径错误 = 结论作废：所有计算前先对照 [[quantdb-fields]] 技能。
>
> **⚡ 执行版（先读这个）**：本文件是"设计原则"。要**跑起来**请用 [stock-9layer-runbook.md](./stock-9layer-runbook.md)——
> 它含一键取数脚本 `scripts/stock_9layer_fetch.py`、每层判读模板、跨层合成规则、报告落盘命令、双案例校准与红线清单。

## 数据 → 问题映射（9 层金字塔，L4 拆为资金筹码/订单微结构，L7 用七维新闻方法论）

| 层 | 数据来源（端点/parquet） | 回答的核心问题 |
|---|---|---|
| L0 市场环境 | `/selection/daily` market_state、`index_daily` 大盘 MA20 | 现在是牛还是熊？仓位该多少？ |
| L1 估值 | `valuation`（pe_ttm/pb/ps/dividend_rate）+ 历史分位、l1 `ind_relative_pe` | 贵不贵？相对行业、相对自身历史处在什么位置？ |
| L2 财务 | `income`/`balance`/`cashflow`/`pershare_index`/`dividend_factors`/`holder_num` | 赚钱吗？真金白银还是纸面利润？资产负债表健康吗？ |
| L3 技术 | `technical_indicators`（37 列全量）+ `daily_forward` K 线 | 趋势、动能、支撑压力、量价是否配合？ |
| L4 资金筹码 | `l1_l2_factors` flow_*/chip_*（见 l1_l2 合并宽表）、`margin_trading` 融资融券 | 谁在买谁在卖？主力还是散户？筹码松动还是集中？ |
| **L4b 订单微结构** | `6_ml_datasets/l1_l2_factors`（micro_*/vol_*/flow_* 列，见 l1_l2 合并宽表）按《L2 微观结构因子系统化分析报告》框架 | 盘里的钱在怎么动？订单流毒性、知情交易、资金韧性处于全市场什么分位？（T+5/T+10 持续信号） |
| L5 行业概念 | `l1_factors` ind_*/concept_*、`sector_concept`、`index_weights` | 所处行业强不强？概念热不热？轮动到了没有？ |
| L6 模型信号 | `/models/inference/stock/{symbol}/history`（按 model_id 分模型） | 模型怎么看？多模型是否一致？趋势是改善还是恶化？ |
| L7 新闻舆情 | `/news/articles` + Huntly 库全文 → **七维规律三步纵深**（直接→相关→规律对照） | 消息面是催化剂还是风险？与量价/L4b/模型信号互相印证？ |

## 分析深度规范（每层的具体做法 + 计算公式）

### L0 市场环境（先于一切）
- `/selection/daily` 的 `market_state`：牛/熊 + 建议仓位（熊市空仓正常，报告必须说明）
- `index_daily` 上证综指（000001.SH）收盘 vs MA20：`MA20 之上 + 仓位>0` 才支持做多；**个股再强也受大盘拖累**
- `meta.total_signals`、强行业数 → 市场宽度

### L1 估值（三维对照，缺一不可）
1. **绝对水平**：pe_ttm / pb / ps_ttm 当前值，负 PE 或无盈利 → 用 PB/PS
2. **行业相对**：`ind_relative_pe`（l1 因子，<1 = 相对行业折价）
3. **历史分位**：拉 valuation 近 3-5 年序列，算当前 PE 的历史百分位（**估值贵不贵只看当前值没有意义**）
- 输出一句话：`PE 23.1x = 近 5 年 18% 分位，行业相对 0.72 → 估值不构成风险`

**⚠️ 口径陷阱（写死）**：
- valuation.dividend_rate **20260814 起切换为百分数口径**（0.148 = 0.148%），此前是小数口径（0.0148）。跨切换日的历史分位/趋势必须先按日期 ×10 归一，否则股息率差 10 倍
- 股息率自算公式（与 valuation 互验）：`dividend_factors.interest ÷ 10 ÷ 不复权close × 100`
- valuation 的 close 是**不复权**、total_mv/float_mv 单位是**元**（报告换算亿元 ÷1e8）

### L2 财务（三表联动 + 每股指标，防"纸面利润"）
- **利润质量**：`cf.net_cash_flows_oper_act / inc.net_profit_incl_min_int_inc`，连续 2 期 < 1 → 红牌
- **资产负债表**：`(account_receivable + inventories)` 增速 vs `revenue` 增速（应收暴增 = 压货）；`goodwill / tot_shrhldr_eqy_excl_min_int > 30%` → 减值风险
- **股东行为**：`holder_num.shareholder` 趋势（户数降 = 筹码集中，升 = 派发）
- **ROE 拆解**：`equity_roe`（%）= 净利率 × 周转 × 杠杆，看哪个驱动
- **成长性**：单季同比 = 本期 / 去年同期 − 1（m_timetag 找去年同季行）
- **分红**：`dividend_factors.interest` = **每10股派息（元）**，每股股息 = interest/10；连续分红年数看 `time` 序列
- 财务数据是季频（单位=元）：看最近 8 个季度趋势，不是单期；报告表格统一换算为**亿元**

### L3 技术（分层递进，不堆指标）
- **趋势层**：MA5/10/20/60 排列（多头/空头/粘合），价格在均线系统的位置
- **动能层**：MACD（dif/dea/hist 方向+柱状收敛还是放大）、RSI 6/14（超买超卖+背离）、KDJ
- **波动层**：`vol_atr_14`（元）、`vol_std_60`（**%**，technical 口径=4.06 即 4.06%；l1 同名字段是小数 0.0406，差 100 倍）、`beta_20`（beta 高 = 大盘放大镜，L0 结论要加重）
- **量价层**：`vol_to_ma5/20`（量比）、`volume_trend_3d`（放量上涨 vs 缩量反弹）
- **关键价位**：K 线近 20/60 日高低点（/research/kline 是**不复权**价；technical_indicators 的 close/ma 是**后复权**口径，两套价格不能直接对比，除权前后要换算）
- 输出：趋势方向 + 动能状态 + 关键价位（支撑/压力）

### L4 资金筹码（多口径交叉，防单一口径误导）
- **分单口径**：`flowSuperNet`（超大单）/`flowLargeNet`（大单）/`flowMediumNet`/`flowSmallNet`（散户）——三口径同向才可信
- **全口径**：`flowNetAmount` + `flowNetRatio`（净流入占成交比）
- **持续性**：`flowConsistency`、5 日/20 日累计净流入方向
- **筹码**：`chipProfitRatio20/60`（获利盘比例）、`chipConcentration20`（集中度）、`chipPeakDistance`（离成本峰距离）、`chipCost90Width`（成本区间宽度）、`chipProfitDelta5`（5 日获利盘变化——散户接盘还是主力吸筹）
- **融资融券**：margin_trading `finance_balance`（融资余额，**万元**）、`finance_net`（融资净买入，万元）、`slo_net`（融券净卖出，股）——杠杆资金态度
- 经典背离：**股价涨 + 大单净流出 + 获利盘快速上升 = 散户接盘主力派发**

### L4b 订单微结构（L2 截面框架，2026-08 新增层）

> 依据《L2 微观结构因子系统化分析报告》（875 交易日 × 5202 标的全量验证，211 因子）。**注：l2 独立目录已下线，读 l1_l2 合并宽表（见 l1_l2 合并宽表）。** 若某天读数全 NaN 才是厂商停更，要用前先 `ls data/quantdb/6_ml_datasets/l1_l2_factors/ | tail` 确认最新日期。

**三条铁律（IC 方向，判读前提）**：
1. **VPIN 族是正 IC 因子**（`micro_vpin_vol_ratio` ICIR +0.562 居首）：VPIN 上升 = 知情资金在场 = **偏多**，绝不是"毒性利空"。
2. **最强负向因子**：`vol_persistence`（IC **-0.677**）、`micro_toxicity_persistence`（IC **-0.503**）、`flow_buy/sell_amount`（-0.52）、`vol_realized_jump`（-0.459）——**这些上升才构成利空**。
3. **L2 是 T+5/T+10 持续信号，不是 T+1 超短线**（信号 T+1→T+10 单调增强，T+10 比 T+3 强 22%~53%）——判个股看**截面状态分位**，而非单日边际。

**标准动作：算截面分位（个股 vs 全市场当日 5202 标的）**
```python
import duckdb, pandas as pd
dt = 'YYYYMMDD'  # 最新分区
mkt = duckdb.connect().execute(f"SELECT * FROM read_parquet('data/quantdb/6_ml_datasets/l1_l2_factors/dt={dt}/data.parquet')").df()
stock = mkt[mkt['symbol']=='600563.SH'].iloc[0]
for f in L2_REPORT_FACTORS:               # 报告 26 个确认因子
    pct = (mkt[f].astype(float) < float(stock[f])).mean()   # 上分位
    # 负IC因子 pct≥70%→利空；正IC因子 pct≥70%→利多
```

**判读规则**（报告实证阈值）：
- **负面组合**：负 IC 因子 ≥5 项高位（≥70% 分位）+ 正 IC 因子多数低位（≤40%）→ 持续性偏空
- **正面组合**：正 IC 因子 ≥70% + 负 IC 因子 ≤40% → 持续性偏多
- **中性**：两族均居中；须与其他层（量价/新闻/模型）交叉定调
- **参考维度**：`micro_vpin_hurst`（订单流单边惯性）、`vol_realized_jump`（脉冲下杀）、`vpin_vol_ratio` 方向变化（领先反转的前兆——法拉电子案例中它是唯一低位正 IC 反弹项）

**关键因子速查**：正 IC＝vpin_vol_ratio / vpin_amount_ratio / vpin_50 / vpin_ma_20 / order_duration_p90 / cancel_lifetime / trade_interval_mean；负 IC＝vol_persistence / toxicity_persistence / flow_buy_amount / flow_sell_amount / order_arrival_rate / tick_density / realized_jump / realized_rrv / jump_count_* / trade_arrival_rate。

### L5 行业概念（个股强 ≠ 行业强）
- `ind_strength_20/60`（行业动量强度）、`ind_rotation_speed_20`（轮动速度）、`ind_crowding_20`（拥挤度——太热要警惕）
- `ind_breadth_up_20`（行业上涨家数占比，宽度）、`ind_netflow_rank_20`（行业资金流排名）
- 概念：`concept_hot_score`（热度）、`concept_momentum_top3`（动量）、`concept_leader_score`（龙头分）、`concept_crowding_max`（拥挤）
- 结论要区分：**行业强 + 个股强**（共振，最理想）/ 行业弱 + 个股强（逆势，持续性存疑）/ 行业强 + 个股弱（掉队）
- `index_weights`：该股在沪深300/中证500 的权重变化（被动资金流入流出）

### L6 模型信号（多模型，不只看一个）
- **用户模型**：`/api/v1/models`（items[]，`id` 字段 + `is_default`）；**系统模型**：`/api/v1/inference/models`（engine 直连，`model_id` 字段）——两端命名不同，拉历史时注意
- **全部模型逐拉**：用 `model_id` 参数逐个拉 `history`——不同模型是独立视角（不同训练期/周期 T3/T10/T15/融合），比较：
  1. **共识度**：多模型同方向 = 高置信；分歧 = 报告单独说明
  2. **趋势**：每个模型序列自身是上升/回落/横盘（分数绝对值小不代表无意义，看**变化方向**）
  3. **极值**：分数处于该模型历史序列的什么位置（z-score/分位）
- `signal_side`（BUY/HOLD/SELL）是模型当时的操作信号，与 fusion_score 并列引用
- **模型与量价/资金背离时**：这是最值钱的信号（如基本面好但模型 4 个月 SELL = 边际改善未被确认）
- 排名 `score_rank` 是该股在当天批次内的截面排名（越小越靠前）

### L7 新闻舆情（催化剂 vs 印证，七维规律三步纵深）

> 方法论底座：[[news-sentiment-research]] 技能 + `docs/news_sentiment_deep_report.md`（21 条实证规律）。**个股新闻分析的规范动作是「三步纵深」，不是"拉几条新闻贴上去"。**

**三步纵深（每一步都必须做）**：

**① 直接消息判定** —— 扫 `[T-2, T]` 窗口内**标题+正文**命中 `{股票名}/{code}`：
- 命中 → 定性：来源（白名单财联社/同花顺？黑名单南华/彭博？）、时段（黄金 19-22 点？凌晨噪声）、多篇数（≥2 同向 → 信号 ×2.2）、当天涨跌（首日动量双确认：利好+涨/利空+跌 才有效）
- 零命中 → 明确写「个股近期无直接催化」，**禁止用行业新闻冒充个股消息**

**② 相关行业归类** —— 标题含行业词（超级电容/薄膜电容/电力设备等）的新闻按板块定性：
- 利好共振源（同行量产量/业绩预增）vs 利空分化源（同行亏损/行业竞争叙事）
- 用 `industries`/`event_tags` 区分板块级利好和个股级利好，写清"间接、板块级"

**③ 规律对照打分** —— 21 条规律逐条对位，产出**明确的新闻面结论**（永远不许含混）：

| 规律 | 单股化判据 |
|------|-----------|
| 财联社利空金矿 / 同花顺利好金矿 | 有白名单来源消息 → 信号加权重 |
| 黄金时段 19-22 点 | 晚闻消息质量高；凌晨 1-5 点直接降权 |
| 多篇≥2 同向 → ×2.2 | 同日 ≥2 篇同向 = 强信号 |
| 首日动量双确认 | 消息日同向涨跌才确认 |
| 利好→利空反转 | 持有中反向新闻 → 离场信号 |
| 板块规则 | 深主板利空最深 / 创业板禁做空 / 沪主板利好 T+5 +0.49% |
| 强利空分值 | score ≤ -0.6 → 真暴雷（-2.39%），权重提高 |

**API 用法**：
- `tickers={code}` 个股 + `industries={行业}` 行业（个股没新闻不代表行业没新闻）
- `sentiment=bullish|bearish` + `strong_only=true`（\|score\|>=0.5）快速定位最强多空
- `event_tags` 事件标签（并购/财报/解禁/政策）——事件型新闻必须找**后续数据印证**（公告增持 → 查资金流是否真流入）
- **无新闻处理**：明确标注 `[数据缺失]` 并提醒加 RSS 源；**禁止编造新闻**
- 默认价值排序：**政策 > 公司重大事件 > 行业动态 > 分析师观点 > 市场情绪文**

## 跨层印证矩阵（设计的灵魂：不孤立看任何一层）

| 组合 | 印证逻辑 | 典型结论 |
|---|---|---|
| L6 模型↑ + L4 大单流入 + L3 突破 | 三重共振 | 高置信做多信号 |
| L2 财务好 + L6 模型持续 SELL + L4 流出 | 好公司≠好买点 | 等待确认，不追 |
| L1 估值低 + L5 行业弱 | 价值陷阱风险 | 低有低的理由 |
| L3 放量长阴 + L4 大单流出 + L7 有利空新闻 | 事件驱动下跌 | 看承接力 |
| L4 获利盘↑ + 大单流出 + 股价涨 | 派发结构 | 警惕 |
| L5 概念拥挤 + L3 RSI 超买 | 情绪顶点风险 | 减仓区 |
| L2 现金流/净利 <1 连续2期 + 应收暴增 | 纸面利润 | 下调基本面评级 |
| **L7 无消息 + L4b 负IC家族高位（toxicity↑/jump↑）+ L3 缩量阴跌** | 资金惯性下行、无催化剂 | **左侧末段，仅反弹博弈，勿接飞刀** |
| **L7 直接利空 + L4b 负IC高位 + L4 大单流出** | 事件驱动下杀盘面确认 | 强利空，回避/做空候选 |
| **L7 直接利好 + L4b 正IC族扩散（vpin_vol_ratio↑/informed↑）** | 消息+知情资金共振 | 高置信做多候选 |
| **L7 有利好但 L4b 正IC族低位（无资金跟进）** | 利好不认、假利好 | 一日游，不追 |
| **L4b vpin_vol_ratio 率先恢复 + L7 消息面静默** | 知情交易回归、反转前兆 | 观察确认，等第二信号 |

## 报告结论结构（研报模板，与 SKILL.md 第 7 节一致）

1. **投资要点**：3-5 条一句话要点（多空都写）
2. **核心结论表**：市场/基本面/估值/技术/资金/综合评级六维
3. **公司概况**：行业/市值/两融标的
4. **财务分析**：最近 8 期三表 + 趋势解读
5. **估值分析**：当前值 + 历史分位 + 行业相对
6. **技术分析**：分层递进 + 关键价位
7. **资金面与筹码**：多口径交叉 + 融资融券
8. **订单微结构（L4b）**：截面分位表（负 IC 家族/正 IC 家族）+ L4b 结论一句话
9. **行业与概念**：共振/逆势/掉队
10. **AI 模型信号**：多模型共识度 + 趋势
11. **新闻面结论（L7）**：三步纵深输出——直接消息（0 条/命中）/ 相关行业归类 / 规律对照判定
12. **多空证据对照**：逐层列证据（每条带数值）
13. **风险提示**：风险评分卡 veto 项 + 各层风险点
14. **操作建议**：分持仓状态 + 触发条件（价位/信号阈值/L4b 反转前兆）

**报告落盘（必做）**：`data/reports/stock_reports/{市场名}/{股票名}/{股票名}{代码}_{日期}_投研分析报告.{md,pdf}`；宿主机目录 owner 是容器内 root → 必须 `docker cp` 到容器 `/app/db/...`，别在宿主机直接写（EACCES）。md→pdf 用容器内 `backend/scripts/md_to_pdf_report.py`。只留 /tmp = 未交付。

## 落地端点速查

```bash
# L0: 市场状态（market_state 含牛熊+仓位建议）
curl -s -H "$AUTH" "$BASE/api/v1/selection/daily"
# L0: 大盘 MA20（000001.SH 上证综指）
curl -s -H "$AUTH" "$BASE/api/v1/market/index-kline?symbol=000001.SH&days=60"
# L1-L5: 371 维特征一次拿全（valuation/technical/l1/l2 聚合；API 已换算：市值→亿元、flow→百万元）
curl -s -X POST -H "$AUTH" -H "$CT" "$BASE/api/v1/research/symbols/features" -d '{"symbols":["600519.SH"]}'
# L1 补充: 估值历史分位（parquet 直读，valuation Hive 分区）
# L2: 财务三表 + 每股指标 + 分红 + 股东户数（parquet 直读，3_financial_data/ 按 symbol 平铺）
# L4 补充: 融资融券（parquet 直读，2_base_sector/margin_trading/ Hive 分区，finance_*=万元）
# L6: 模型信号（用户模型 / 系统模型 / 默认模型）
curl -s -H "$AUTH" "$BASE/api/v1/models"                       # 用户模型列表 items[].id
curl -s -H "$AUTH" "$BASE/api/v1/models/default"               # 默认模型
curl -s -H "$AUTH" "$BASE/api/v1/models/inference/stock/600519.SH/history?days=180"
curl -s -H "$AUTH" "$BASE/api/v1/models/inference/stock/600519.SH/history?days=180&model_id=xxx"
# L4b: 订单微结构截面分位（容器内 python + duckdb，需 pandas；先 ls 分区确认最新日期）
#   docker exec quantmind ls /data/quantdb/6_ml_datasets/l1_l2_factors/ | tail -1
#   docker exec quantmind python3 - <<'PY'
#   import duckdb, pandas as pd
#   mkt = duckdb.connect().execute("SELECT * FROM read_parquet('/data/quantdb/6_ml_datasets/l1_l2_factors/dt=最新/data.parquet')").df()
#   pct = (mkt[f].astype(float) < float(mkt[mkt.symbol=='xxx.SH'].iloc[0][f])).mean()  # 逐因子算截面分位
#   # 负IC族≥70%高位 / 正IC族≤40%低位 = 偏空；反之为偏多
# L7: 新闻三步纵深（v3 规律对照，方法论见 news_sentiment_deep_report §13）
curl -s -H "$AUTH" "$BASE/api/v1/news/articles?tickers=600519&industries=白酒&limit=30&strong_only=true"
curl -s -H "$AUTH" "$BASE/api/v1/news/articles?tickers=600519&sort=sentiment_bullish&limit=10"
```

## 复杂度分级（智能体按需选择深度）

| 级别 | 用时 | 适用场景 |
|---|---|---|
| 快速体检 | 1-2 min | 用户只问"怎么样"：L0+L1+L3+L6 默认模型 + 风险卡 |
| 标准分析 | 3-5 min | 日常投研：全部 9 层 + 财务 4 期 + L6 默认+融合 + **L4b 截面分位 + L7 三步纵深简版** |
| 深度尽调 | 10+ min | 用户明确要"全方位"：全部 9 层 + L6 全模型逐拉 + 财务三表 8 期 + 5 年估值分位 + 融资融券 30 日 + 股东户数趋势 + 分红历史 + **L4b 全 26 因子截面 + L7 三步纵深全量（21 条规律对照）** + 情景推演 |
