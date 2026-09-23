---
name: trading-agents
description: "投研分析（TradingAgents）— 7 AI分析师 → 质量门控 → 多空辩论 → 风控评估 → 最终决策，多 Agent 研究报告生成。在 QuantBot / Claude Code 中生成股票投研报告、深度研报、AI 分析师分析时使用。触发词：投研分析、研究报告、生成报告、深度研报、TradingAgents、分析师、多Agent分析、股票研究报告"
---

# 投研分析（TradingAgents）技能

多 Agent 投研分析管线：**7 个 AI 分析师 → 质量门控 → 多空辩论 → 交易决策 → 风控评估 → 最终决策**。支持 **A股/港股/美股/区块链/期货** 五市场，本地 QuantDB 数据驱动。

## 一、完整流程（LangGraph 状态图）

```
START
 → 7 个分析师串行（每个: 分析师 → tools工具 → 清理 → 下一个）
    ① Market Analyst     技术分析（选 ≤8 个技术指标）📊
    ② Social Media       情绪分析（社交情绪）💬
    ③ News Analyst       新闻舆情 📰
    ④ Fundamentals       基本面分析 📋
    ⑤ Policy Analyst     政策分析（A股特有）🏛️
    ⑥ Hot Money          游资追踪（资金流/量异常）🔥
    ⑦ Lockup Watcher     解禁监控（解禁/减持）🔒
 → Quality Gate          质量门控（硬检查报告出 grade/detail）✅
 → Bull ↔ Bear Researcher 多空辩论（轮次=2×max_debate_rounds）⚔️
 → Research Manager      研究经理 → 结构化投资计划
 → Trader                交易员 → 交易投资计划 💹
 → Aggressive ↔ Conservative ↔ Neutral 风控三辩（轮次=3×max_risk_discuss_rounds）🛡️
 → Portfolio Manager     组合经理 → 最终决策（5 档评级）👔
 → END
```

### 每阶段报告字段（progress 返回的 stage_reports）

| # | 阶段 id | 名称 | report_key | 内容 |
|---|---|---|---|---|
| 1 | market | 技术分析 | `market_report` | 技术指标分析（MA/RSI/KDJ/MACD 等） |
| 2 | social | 情绪分析 | `sentiment_report` | 市场情绪 |
| 3 | news | 新闻舆情 | `news_report` | 新闻事件 |
| 4 | fundamentals | 基本面 | `fundamentals_report` | 财务/估值 |
| 5 | policy | 政策分析 | `policy_report` | 政策信号 |
| 6 | hot_money | 游资追踪 | `hot_money_report` | 资金流/量异常 |
| 7 | lockup | 解禁监控 | `lockup_report` | 解禁/减持 |
| 8 | quality_gate | 质量门控 | `data_quality_summary` | 数据质量检查 |
| 9 | debate | 多空辩论 | `investment_debate_state.judge_decision` | 多空裁判结论 |
| 10 | trader | 交易决策 | `trader_investment_plan` | 交易计划 |
| 11 | risk | 风控评估 | `risk_debate_state.judge_decision` | 风控裁判结论 |
| 12 | pm | 最终决策 | `final_trade_decision` | **最终评级** |

### 5 档最终评级

`process_signal()` 用 `parse_rating()` 从组合经理决策中确定性提取（不额外调 LLM）：
**Buy（买入）→ Overweight（增持）→ Hold（持有）→ Underweight（减持）→ Sell（卖出）**

### 关键配置（默认）

| 配置 | 默认值 | 说明 |
|---|---|---|
| `max_debate_rounds` | 1 | 多空辩论轮数（=2 次往返） |
| `max_risk_discuss_rounds` | 1 | 风控辩论轮数（=3 次往返） |
| `llm_provider` | minimax | LLM 供应商 |
| `deep_think_llm` | MiniMax-M2.7 | 研究经理/组合经理用（深度思考） |
| `quick_think_llm` | MiniMax-M2.7-highspeed | 分析师用（快速） |
| `data_vendors` | quantmind_local,<fallback> | 本地 QuantDB parquet 优先 |

## 认证

```bash
BASE=http://127.0.0.1:8000
TOKEN=$(curl -s -X POST $BASE/api/v1/auth/login -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"<管理员口令>","tenant_id":"default"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))")
AUTH="Authorization: Bearer $TOKEN"
CT="Content-Type: application/json"
```

## 1. 启动投研分析（核心）

```bash
curl -s -X POST -H "$AUTH" -H "$CT" "$BASE/api/v1/trading-agents/analyze" \
  -d '{
    "ticker": "300750",
    "trade_date": "2026-08-14",
    "market": "CN",
    "llm_provider": "minimax",
    "deep_think_llm": "MiniMax-M2.7",
    "quick_think_llm": "MiniMax-M2.7-highspeed"
  }'
# 返回: {"code":200, "data":{"analysis_id":"xxxx","ticker":"300750","trade_date":"...","market":"CN","message":"分析已启动"}}
```

**参数**：
| 字段 | 说明 |
|---|---|
| `ticker` | **必填** 标的代码（纯代码，如 300750 / 600519，不带后缀） |
| `trade_date` | 分析日期 YYYY-MM-DD（默认今天） |
| `market` | CN（默认）/ US / HK / CRYPTO / FUTURES |
| `llm_provider` | LLM 供应商（默认 minimax） |
| `deep_think_llm` | 深度思考模型（研究经理/组合经理） |
| `quick_think_llm` | 快速思考模型（分析师） |

**数据来源**：`data_vendors` 用 `quantmind_local` 读**本地 QuantDB 股票数据库**（信息流丰富：K线/财务/估值/技术指标/315维AI因子），各市场网络回退（CN:a_stock / HK:hk_stock / US:us_stock / CRYPTO:crypto / FUTURES:futures）。

**QuantDB 本地数据（7 个分析师共用）**：
| 数据 | 用途 | 覆盖 |
|---|---|---|
| `daily_forward` 日线 | 技术分析（MA/RSI/KDJ/MACD） | 2016~今 |
| `instrument_detail` | 个股详情/行业/上市天数 | 152列 |
| `valuation` | 估值（PE/PB/市值） | 基本面 |
| `l1_factors` / `l2_factors` | 315 维 AI 因子 | 动量/波动/资金流/筹码 |
| `market_sentiment` | 情绪 | 技术+情绪 |
| `trading_calendar` | 交易日历 | — |

**7 个分析师各自读取**：
- 技术分析 ← K线 + technical_indicators
- 基本面 ← valuation + instrument_detail + 财务
- 游资追踪 ← fundFlow 资金流 + 成交量异常
- 情绪分析 ← market_sentiment
- 政策/解禁 ← instrument_detail（上市/解禁信息）

> 数据均来自 `data/quantdb/`（A股）及各市场 parquet，由 `quantmind_local` vendor 读取。信息流不足时用 `data_vendors` 配置的网络回退补全。

## 1.1 新闻数据源（RSS）

「新闻舆情」分析师通过 **RSSHub**（平台自带 `quantmind-rsshub` 容器）拉取金融新闻，按公司名 + 行业关键词过滤：

| RSS 源 | 说明 |
|---|---|
| 华尔街见闻（最新/快讯） | 最快资讯 |
| 东方财富（策略报告） | 广泛 |
| 36氪快讯 | 科技/财经 |
| 财新网 | 权威 |
| 第一财经 | 简报 |

**新闻分析要求**（news_analyst prompt 内置）：
- 区分**利好/利空/中性**消息，评估影响程度和持续时间
- 报告末尾附 Markdown 表格汇总关键新闻事件及影响评级
- **必采清单**：个股新闻条数/时间范围、宏观新闻条数、关键事件时间线（≥3 个）、利好/利空/中性分类统计、风险事件清单
- **无新闻时**：标注 `[数据缺失: xxx]`（如"个股新闻 0 条"），**并提醒用户补充该股相关新闻源**

**常见检查**：
```bash
# 确认 RSSHub 容器在跑（新闻源依赖它）
docker ps | grep rsshub
# 确认新闻源已配置（后台 RSS 管理）
curl -s -H "$AUTH" "$BASE/api/v1/news/sources"
```

**若新闻经常为空**（建议）：
1. 在 Huntly（`quantmind-huntly:8090`）或后台「RSS 管理」添加更多股票相关源（如财联社、证券时报、新浪财经个股页）
2. 确认 RSSHub 路由可用（`curl http://quantmind-rsshub:1200/wallstreetcn/news/quick`）
3. 平台新闻接口 `/api/v1/news/articles?tickers=xxx` 可交叉验证（见 [[quantmind-operations]] 第 7 节）

## 2. 轮询分析进度（12 阶段）

```bash
curl -s -H "$AUTH" "$BASE/api/v1/trading-agents/progress/{analysis_id}"
# 返回: {"code":200, "data":{
#   "ticker","trade_date","market","is_running","is_complete","error",
#   "current_stage","completed_stages","stage_reports",
#   "signal","stats":{"llm_calls","tool_calls","tokens_in","tokens_out"},"elapsed"}}
```

## 3. 获取研究报告

```bash
# 分析报告（结构化 JSON）
curl -s -H "$AUTH" "$BASE/api/v1/trading-agents/report/{analysis_id}"
# 进行中: {"code":202, "data":{"message":"分析仍在进行中","progress":{...}}}
# 完成:   {"code":200, "data":{
#   "ticker","trade_date","signal","final_state",
#   "stage_reports",  # 12 阶段报告（见上文表格）
#   "stats":{"llm_calls","tool_calls","tokens_in","tokens_out"},"elapsed"}}
```

**报告结构**：
- `signal` — 最终评级（Buy/Overweight/Hold/Underweight/Sell）
- `final_state` — 最终决策状态（含组合经理完整输出）
- `stage_reports` — 各阶段报告（key=阶段 id，value=报告文本）
- `stats` — LLM 调用次数、工具调用、token 消耗

## 3.1 生成 Markdown 报告（标题含股票名/日期/分析时间）

拉取报告后，整理成带标题（股票名称 + 日期 + 分析时间）的 md 文件：

```bash
# 1. 下载报告 JSON
curl -s -H "$AUTH" "$BASE/api/v1/trading-agents/report/{analysis_id}" -o /tmp/ta_report.json

# 2. 转 Markdown（标题 = 股票名 + 交易日期 + 分析时间）
# 输出文件放到 {结果目录}/{市场名}/{股票名}/ 下，
# 命名 {股票名}{代码}_{trade_date}_投研分析报告.md（管线自动导出 report_exporter.py 已按此规格落盘）
python3 <<'EOF'
import json, datetime
from pathlib import Path

d = json.load(open('/tmp/ta_report.json'))['data']
ticker = d['ticker']
trade_date = d['trade_date']
now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
signal = d.get('signal', 'Hold')
stage = d.get('stage_reports', {})
elapsed = d.get('elapsed', 0)

# 阶段 id → 中文名映射
STAGE_NAMES = {
    'market':'技术分析','social':'情绪分析','news':'新闻舆情','fundamentals':'基本面',
    'policy':'政策分析','hot_money':'游资追踪','lockup':'解禁监控',
    'quality_gate':'质量门控','debate':'多空辩论','trader':'交易决策',
    'risk':'风控评估','pm':'最终决策',
}

lines = []
lines.append(f'# {ticker} 投研分析报告')
lines.append('')
lines.append(f'> **交易日期**: {trade_date}　|　**分析时间**: {now}　|　**耗时**: {elapsed:.0f}s')
lines.append(f'> **最终评级**: **{signal}**')
lines.append('')
lines.append('---')
lines.append('')
for sid, name in STAGE_NAMES.items():
    text = (stage.get(sid) or '').strip()
    if text:
        lines.append(f'## {name}')
        lines.append('')
        lines.append(text)
        lines.append('')

md = '\n'.join(lines)
out = Path(f'/tmp/ta_report_{ticker}_{trade_date}.md')
out.write_text(md, encoding='utf-8')
print(f'已生成: {out}')
print(f'标题: {ticker} 投研分析报告 | {trade_date} | {now} | 评级 {signal}')
EOF
```

**生成结果**：`/tmp/ta_report_{ticker}_{trade_date}.md`，标题格式 `股票名 投研分析报告`，副标题含交易日期、分析时间、耗时、最终评级，正文按 12 阶段分节展示各分析师结论。

## 3.2 转 PDF（中文排版）

用 `backend/scripts/md_to_pdf_report.py` 把 md 报告转成带样式的中文 PDF。**首选文泉驿 WQY 字体（TrueType 内嵌）**：正文用 MicroHei、`**粗体**` 用 ZenHei（addMapping 使 `<b>` 真正加粗）；缺字体时回退 NotoSansCJK → arphic/uming → STSong-Light CID。支持表格/标题/引用/列表/代码块：

```bash
# 在容器内执行（脚本随 backend bind mount 可见）
# 新目录结构：{市场名}/{股票名}/，文件 {股票名}{代码}_{date}_投研分析报告.{md,pdf}
docker exec quantmind python /app/backend/scripts/md_to_pdf_report.py \
  /data/reports/trading_agents/A股市场/比亚迪/比亚迪002594_2026-08-14_投研分析报告.md \
  /data/reports/trading_agents/A股市场/比亚迪/比亚迪002594_2026-08-14_投研分析报告.pdf

# 验证字体真正内嵌（WQY 字体子集化后带 AAAAAA+ 前缀，
# 必须用 BaseFont 正则解析，不能直接 grep 'WQY' 字节串）
docker exec quantmind python -c "
import re
data = open('/data/reports/trading_agents/A股市场/比亚迪/比亚迪002594_2026-08-14_投研分析报告.pdf','rb').read()
bf = sorted(set(m.group(1).decode() for m in re.finditer(rb'/BaseFont\s*/([A-Za-z0-9+_.-]+)', data)))
print('字体:', bf)
print('粗体 OK:', any('ZenHei' in f or 'CJK-Bold' in f for f in bf))
print('正文 OK:', any('MicroHei' in f or 'CJK' in f for f in bf))
"

# 复制到宿主机
docker cp quantmind:/data/reports/trading_agents/A股市场/比亚迪/比亚迪002594_2026-08-14_投研分析报告.pdf .

# 宿主机直跑（需 reportlab，字体路径见下方说明）
# python backend/scripts/md_to_pdf_report.py input.md output.pdf
```

**PDF 特性**：
- A4 页面，中文正常渲染（文泉驿 MicroHei 内嵌，任何 PDF 渲染器含浏览器 pdfjs 均可显示）
- **粗体真实加粗**（ZenHei 独立字重，而非伪粗体）
- 标题分级（h1 居中大标题、h2 蓝色小节）
- 表格带表头深蓝底 + 隔行浅色 + 网格线
- 引用块/列表/代码块/分隔线样式

**字体路径**：脚本依次探测 `docker/training/fonts/WQYMicroHei.ttf`（+ `WQYZenHei.ttf`，按脚本位置相对项目根解析）→ `NotoSansCJK.ttf` → arphic/uming → NotoSerifCJK → STSong-Light。宿主机直跑时把 WQY ttf 放到 `docker/training/fonts/` 或系统字体目录，否则回退 STSong-Light（CID 字体不内嵌，某些渲染器中文会缺失）。

**输出文件约定**：`{股票名}{ticker}_{trade_date}_投研分析报告.{md,pdf}`（股票名查不到时省略），存 `db/trading_agents_results/{市场名}/{股票名}/` 两级目录。

## 4. 分析历史 / 停止 / 配置 / 下载

```bash
# 历史分析记录（读 qm_trading_agents_history 表）
curl -s -H "$AUTH" "$BASE/api/v1/trading-agents/history"

# 停止正在运行的分析
curl -s -X POST -H "$AUTH" -H "$CT" "$BASE/api/v1/trading-agents/stop" \
  -d '{"analysis_id":"xxx"}'

# 查看管线配置（模型/市场/供应商）
curl -s -H "$AUTH" "$BASE/api/v1/trading-agents/config"

# 下载分析报告（JSON 文件）
curl -s -H "$AUTH" "$BASE/api/v1/trading-agents/download/{analysis_id}" -o report.json
```

## 5. 数据库表（qm_trading_agents_history）

分析结果持久化到 PostgreSQL：

| 列 | 类型 | 说明 |
|---|---|---|
| analysis_id | text | 分析 ID（主键） |
| ticker | text | 标的代码 |
| trade_date | text | 分析日期 |
| signal | text | 最终评级（Buy/Hold/Sell 等） |
| llm_provider / deep_think_llm / quick_think_llm | text | LLM 配置 |
| **stage_reports** | jsonb | 12 阶段报告（key=阶段 id） |
| **final_state** | jsonb | 最终决策完整状态 |
| **stats** | jsonb | LLM 调用/token 统计 + market |
| elapsed_seconds | double | 耗时 |
| error | text | 错误信息 |
| created_at / updated_at | timestamp | 时间戳 |

## 6. 实战流程（推荐）

当用户要求"生成投研报告 / 分析某只股票"时：
1. **选标的**：确认 ticker（纯代码，如 300750）+ market
2. **启动分析**：`/trading-agents/analyze` POST（默认 minimax LLM）
3. **轮询进度**：`/progress/{analysis_id}` 每 10s 查一次（12 阶段通常 1-3 分钟）
4. **取报告**：`/report/{analysis_id}` 拿 signal + stage_reports（code=200 才算完成，202 为进行中）
5. **解读各阶段**：按 stage_reports 的 12 个 key 逐个看分析师结论
6. **下载留档**：`/download/{analysis_id}` 存 JSON
7. **交叉验证**：结合 [[stock-market-analysis]] 量化因子 + [[quantdb-sdk]] 数据

## 7. 相关技能

- **[[stock-market-analysis]]** — 量化因子深度分析（371 字段 + 风险评分）
- **[[batch-inference-analysis]]** — 模型推理信号选股
- **[[quantmind-operations]]** — RSS 新闻、模型推理
- **[[quantdb-sdk]]** — QuantDB 数据查询

## 8. 常见问题

| 现象 | 处理 |
|---|---|
| analyze 超时 | 减小分析范围，先看 `/config` 确认 LLM 配置 |
| 进度卡住 | 检查引擎服务健康 + LLM Key，用 `/stop` 停止后重试 |
| report 返回 202 | 分析仍在进行中，继续轮询 progress |
| 报告为空 | 确认 analysis_id 状态，等 code=200 |
| LLM 供应商报错 | 检查 minimax/OpenAI Key 配置，换 llm_provider |
| 本地数据缺失 | 该市场未同步，先跑对应 `*_daily_sync.py` 或数据管理页同步 |
| 想看历史报告 | `/history`（读 qm_trading_agents_history，stage_reports 保留全部阶段） |
| 股票报告页找不到报告 | 目录名必须用 **`A股市场`（无空格）**，与后端 `report_exporter.py::_MARKET_NAMES` 一致；新结构为「市场文件夹/股票名文件夹/{股票名}{代码}_{日期}_投研分析报告.{md,pdf}」 |
