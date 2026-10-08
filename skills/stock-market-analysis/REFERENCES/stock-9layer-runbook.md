# 个股 9 层深分端到端操作手册（Runbook）

> 把 [quantdb-full-analysis-design.md](./quantdb-full-analysis-design.md) 的框架从"设计原则"落到"一次能跑完的流水线"。
> 每一步都有**命令 + 判读规则 + 输出模板**。两个校准案例：法拉电子 600563（缩量阴跌/L2 偏空/无消息）与振华科技 000733（估值冰点/基本面恶化/行业拥挤/资金活跃非知情），见 §7。

---

## 0. 输入与前置（30 秒）

```bash
# ① 股票名 → 代码（用户只说"振华科技"时）
docker exec quantmind-db psql -U quantmind -d quantmind -t -c \
  "SELECT symbol,name FROM stocks WHERE name LIKE '%振华科技%';"

# ② 确认最新数据分区（日更节奏：当日 L2 晚间落盘；个别标的日K可能缺最新一天）
ls data/quantdb/6_ml_datasets/l2_factors/ | tail -1          # L2 最新
ls data/quantdb/1_kline_data/daily_forward/ | tail -1        # 日K 最新
```

---

## 1. 一键取数（3 分钟拿到 8/9 层原始证据）

```bash
# parquet 层：行情/财务/估值/两融/订单微结构截面（自动探测 host/容器路径）
python3 scripts/stock_9layer_fetch.py 000733.SZ --json       # 宿主机
docker exec quantmind python3 /app/scripts/stock_9layer_fetch.py 000733.SZ --json  # 容器

# PG 层（脚本不管，需补）：
# L6 模型信号
docker exec quantmind-db psql -U quantmind -d quantmind -c \
 "SELECT trade_date, model_version, light_score, tft_score, fusion_score, signal_side FROM engine_signal_scores WHERE symbol='000733' ORDER BY trade_date DESC LIMIT 15;"
# L7 新闻富集（个股 ticker 命中）
docker exec quantmind-db psql -U quantmind -d quantmind -c \
 "SELECT huntly_page_id, sentiment_label, sentiment_score, substr(title,1,60) FROM news_article_enrichment WHERE tickers::text LIKE '%000733%' ORDER BY huntly_page_id DESC LIMIT 20;"
# L0 大盘（上证 vs MA20，脚本暂无 → 手动 python 或 /selection/daily）
```

---

## 2. 九层判读说明书（每层：看什么 → 怎么算 → 怎么下结论）

### L0 市场环境
- 上证综指收盘 vs MA20：`MA20 之上 = 非空仓市`；若当日全市场系统性大跌（双创 -6%），个股任何跌幅都先归因大盘。
- **坑**：`index_daily` 的关为不复权口径；个股与指数单位需分开看（个股 volume=股、指数=手）。

### L1 估值（三维对照）
- 算 `pe_ttm / pb / ps_ttm / dividend_rate` 近 3 年**截面分位**（脚本 `fetch_valuation` 自动输出）。
- 判读模板：`PB {x} = {n}% 分位 / PS {n}%`。
- **关键矛盾识别**：PB 冰点 + PE 中位 = **利润下滑抵消股价下跌**，"便宜"≠错杀（振华 PB 3% 但盈利 Q1 -72.8%）。PB 冰点给"向下有限"的底，PE 中位/高位给"盈利未止跌"的警告。

### L2 财务（三表联动）
- 利润质量：`经营现金流 / 净利` 连续 2 期 <1 = 红牌；Q1 现金流转负需备注季节性。
- 应收预警：`应收账款 / 全年营收 > 0.8` = 回款风险（振华 51.6亿/57.5亿 ≈ 0.9）。
- 股东结构：股东户数**持续上升 = 筹码分散/派发**。
- 分红趋势：每10股分红从 11 缩到 3.8 = 盈利恶化直接证据。
- **坑**：`inc_net_profit_rate` 是**归母净利同比增速**不是利润率（Q1 -72.8% = 利润同比腰斩）；毛利率看 `sales_gross_profit`（%）。

### L3 技术（量价 → 支撑压力）
- MA 排列：价格 vs MA5/10/20（跌破 MA20 + 创阶段新低 = 转空）。
- 关键价位：近 20 日高低点 + 大阴线 low（08-19 低 = 短线纪律位）、上方 MA 密集区 = 压力。
- 量价质量：**放量下跌后缩量反弹 = 反弹质量差**（振华 08-20 反弹未放量）。
- **坑**：`daily_forward` 个别标的缺最新一天分区行 → 用 `valuation` 的 close 补（同文件时间戳最新行）。

### L4 资金筹码（两融 + 大单交叉）
- 两融：`finance_balance`（万元）、`finance_net`（净买入）。**暴跌日两融逆势净买入 = 杠杆抄底行为**（振华 08-19 +2807 万）。
- 交叉：L4b `flow_super_net`（超大单净额，元）方向是否与两融一致。
- 经典背离：股价涨 + 大单净流出 + 户数升 = 派发。

### L4b 订单微结构（截面分位——本框架最独特的层）
`stock_9layer_fetch.py` 已算好 23 因子 **vs 全市场当日截面分位**并标注 IC 方向：
- **负面组合**：负 IC 因子（vol_persistence / toxicity_persistence / flow_buy / flow_sell / order_arrival / trade_arrival / tick_density / realized_jump / rrv / jump_count / close_squeeze）≥5 项 ≥70% 分位 + 正 IC 因子（vpin_vol_ratio / order_duration / cancel_lifetime / trade_interval_mean / vpin_50 / vpin_ma_20 / informed_ratio）多数 ≤40% → 持续偏空。
- **正面组合**：正 IC 因子 ≥70% + 负 IC 因子 ≤40% → 持续偏多。
- **判别注意**：`vpin_vol_ratio` 单因子 92% 高位 ≠ 反转确认——必须看 `informed_ratio`（振华 24.6% 极低 = 非知情资金）与 `hurst`（0.97 = 单边惯性巨大，双刃剑）。**资金活跃 ≠ 主力资金**。
- 边界条件：`vol_realized_jump` = 当日有无脉冲下杀（0=无，>0.5=有）；`rrv` 低位往往配合低波动 = 利多（负 IC 低位）。

### L5 行业概念
- 行业强势信号：`ind_ret_20`（行业 20 日涨幅）、`ind_breadth_up_20`（宽度）、`ind_relative_pe`（<1 折价）。
- **拥挤红线**：`concept_crowding_max` ≥ 0.9 **或** `ind_crowding_20` ≥ 0.75 = 情绪顶点区 → 追高禁止，已有仓位逢高兑现（振华 0.998 满格）。
- 组合定调：行业强+个股强=共振（最理想）；行业强+个股弱=掉队；行业强+拥挤高=过热。

### L6 模型信号
- 多模型逐拉：每个 trade_date 多行可能是不同模型，也可能是同模型不同批次，必须核对模型标识与 run_id。仅同模型同口径比较序列趋势，未知模型记录不能合成连续趋势。
- 判读：全 HOLD + 个别峰值回落 = 模型"短暂提示后重回中性"（振华 08-19 +0.335 → 归零）。
- **坑**：`fusion_score` 绝对值小不等于无意义——看变化方向与是否跨 HOLD→BUY/SELL。

### L7 新闻（三步纵深，禁止"拉几条就贴"）
- **① 直接消息**：Huntly 库窗口 `[T-2,T]` 标题+正文搜 `{名称}/{代码}`，命中→定性（来源白/黑名单、时段黄金、多篇），零命中→**明写"个股无直接催化"**。
- **② 行业归类**：搜行业词（军工/MLCC/钽/超级电容等）→ 分利好共振源 vs 利空分化源，写"板块级/间接"。
- **③ 规律对照 + 新闻×L2 交叉**：
  | 直接消息 × L2 | 定调 |
  |---|---|
  | 直接利空 + L4b 负IC高位 | 强利空，回避/做空候选 |
  | 无消息 + L4b 缩量阴跌 | 左侧末段，勿接飞刀 |
  | 直接利好 + L4b 正IC扩散 | 高置信做多候选 |
  | 有利好但 L4b 正IC低位 | 假利好，不追 |
  | 无消息 + L4b vpin_vol_ratio 恢复 | 反转前兆，等第二信号 |
- 新闻库访问：`docker cp quantmind-huntly:/data/db.sqlite /tmp/h.sqlite` + sqlite3（SQL 模板见 news-sentiment-research）。

---

## 3. 跨层合成（多空证据对照表）

报告中必须有一张「多空证据对照」表（见报告模板 §11），一行一条证据、带数值。合成优先级规则：
1. **五个红线任一命中 → 降级**：concept_crowding ≥0.9；应收/营收 ≥0.8；Q1 净利同比 < -50%；L4b 负 IC 组合；无消息 + 缩量而 L3 破位。
2. 估值冰点（PB<P10）能**托底但不提供方向**——方向由 L3+L4b+L7 三者合流决定。
3. 模型永远只作"印证"，不作"驱动"（它反映已知基本面）。

---

## 4. 报告输出与落盘（必做，只发 /tmp = 未交付）

```bash
# ① 按模板写 MD（结构见 §5）→ /tmp/{name}_{code}_report.md
# ② 容器内转 PDF
docker cp /tmp/{name}_{code}_report.md quantmind:/tmp/report.md
docker exec quantmind bash -c 'mkdir -p "/data/reports/stock_reports/A股市场/{股票名}" && \
  python3 /app/backend/scripts/md_to_pdf_report.py /tmp/report.md \
    "/data/reports/stock_reports/A股市场/{股票名}/{股票名}{code}_{YYYYMMDD}_深度学习分析报告.pdf" && \
  cp /tmp/report.md "/data/reports/stock_reports/A股市场/{股票名}/{股票名}{code}_{YYYYMMDD}_深度学习分析报告.md"'
# ③ 宿主机可见（挂载 ./db:/app/db 自动同步），前端报告列表可查
```

---

## 5. 报告模板（研报级，14 节固定）

1. **投资要点**（3-5 条一句话，多空都写）
2. **核心结论表**（9 层一行一读 + 综合评级）
3. **公司概况**（代码/市值/行业/近期事件背景）
4. **财务分析**（最近 8 期三表 + 利润质量/应收/户数/分红）
5. **估值分析**（当前值 + 3 年分位 + 行业相对）
6. **技术分析**（均线排列 / 关键价位 / 量价质量）
7. **资金面与筹码**（两融 + 大单交叉 + 派发结构）
8. **订单微结构（L4b）**（正/负 IC 两组分位表 + L4b 一句话结论）
9. **行业与概念**（强势/宽度/拥挤度）
10. **AI 模型信号**（多模型共识 + 趋势）
11. **新闻面结论（L7 三步纵深）**（直接 0/命中 → 行业归类 → 21 条规律对照 → 明确结论）
12. **多空证据对照**（逐条带数值）
13. **风险提示**（veto 项 + 各层风险）
14. **操作建议**（分持仓/空仓 + 触发观察清单）

---

## 6. 操作建议模板（含触发清单）

- **持仓者**：跌破 {大阴线 low} 离场；{压力价} 无量冲不过 = 弱反弹逢高兑现。
- **空仓者**：不追高（尤其 L5 拥挤红线命中时）。等信号组合——
  - 反转确认：L4b informed_ratio 升 ≥40% + 放量收复 {MA20 压力}
  - 拥挤回落：concept_crowding_max 降 <0.9
  - 基本面拐点：最新一季净利同比收窄（≥-30%）/ 应收占比回落
  - 催化剂：白名单来源直接利好 + 多篇共振
- 每个"操作建议"必须给出**具体价位/阈值**，禁止"逢低关注"式空话。

---

## 7. 双案例校准（同一框架，两种结论）

| 维度 | 法拉电子 600563（2026-08-20） | 振华科技 000733（2026-08-20） |
|---|---|---|
| 量价 | 放量跌→缩量阴跌 | 大阴线→缩量反弹 |
| L1 估值 | 中高位 | **PB 3% 冰点 / PE 54%** |
| L2 财务 | — | **Q1 净利 -72.8%、现金流转负、应收≈年营收** |
| L3 技术 | 跌破 08-19 低点后续创新低 | 跌破 MA20、创阶段新低 |
| L4 | 大单 08-19 出逃后停手 | 两融逆势净买 +2807 万 |
| L4b | **负 IC 5 项 72-82% 高位**（偏空）| vpin_vol_ratio 92% 但 informed 24.6%（活跃非知情）|
| L5 | 中性 | **概念拥挤 0.998（满格）** |
| L6 | HOLD | HOLD（一模型峰值落回）|
| L7 | 0 直接消息，行业中性 | 0 直接消息，军工/MSLC 多空交织 |
| **结论** | **无消息驱动的缩量阴跌左侧末段，勿接飞刀** | **估值托底 + 基本面恶化作空 + 拥挤反转风险，中性偏谨慎，右侧未至** |

> 两个案例教你两件事：
> 1. **L4b 是独立证据维度**——没有它，法拉电子会把 VPIN 当利空、把资金搁置误判；有了它，负面组合一眼可见。
> 2. **相同 L7（0 直接消息）≠ 相同结论**——结论由「估值×基本面×L4b×拥挤」四者合流决定，消息面只是五红线之一。

---

## 8. 红线清单（违反任一条 = 报告作废）

1. **禁止**用行业新闻冒充个股消息（L7 未命中必须明写"无直接催化"）。
2. **禁止**编造数值/新闻；API 拿不到就标注 `[数据缺失]`。
3. **禁止**在没做单位核对前引用数值——先查 [[quantdb-fields]]（volume=股/amount=万元、估值 close 不复权等）。
4. **禁止**把 VPIN 高位当利空（正 IC）；禁止把 `inc_net_profit_rate`（增速）当利润率。
5. **禁止**在 L5 拥挤度 ≥0.9 时给"追高买入"建议。
6. 落盘才算交付——`data/reports/stock_reports/...` 缺 MD 或 PDF = 未完成。

---

*本 runbook 由 QuantMind 9 层深分管线沉淀（2026-08-21，依据两实证报告：L2 微观结构因子系统化分析 + 新闻情绪深度研究 v2）。仅供学习研究，不构成投资建议。*
