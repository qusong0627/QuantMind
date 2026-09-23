---
name: smart-strategy-stock-picking
description: "智能策略选股 — 基于 QuantDB 数据的条件选股。在 QuantBot / Claude Code 中按自然语言或条件筛选股票、构建股票池、生成策略时使用。触发词：选股、筛选股票、股票池、条件选股、智能策略、按条件选股、自然语言选股、帮我选出"
---

> ## ⚙️ 运行环境契约（最高优先级，先于本文其余内容执行）
>
> 本技能可能运行在 **QuantBot（QwenPaw 容器）** 或**宿主机/本地 Claude Code**。执行前先探测环境（`which docker`、API 连通性），并遵守以下映射规则：
>
> 1. **后端 API 地址**：QwenPaw / 容器网络内一律用 `http://quantmind:8000`（`quantmind` 是 docker 网络别名）；仅宿主机调试用 `http://127.0.0.1:8000`。正文中出现的 `127.0.0.1:8000`、`localhost:800x`，在 QwenPaw 环境下自动替换为 `http://quantmind:8000`。
> 2. **取数脚本执行**：凡 import 了 `pandas / duckdb / psycopg2 / numpy / sqlalchemy` 等重依赖或 `backend` 包的脚本，**必须在 quantmind 容器内执行**（QwenPaw 本地 venv 无这些依赖）：
>    ```bash
>    docker cp <脚本路径> quantmind:/tmp/<脚本名> && docker exec -w /app quantmind python3 /tmp/<脚本名> <参数>
>    ```
>    脚本源三选一：宿主机 repo `skills/<name>/scripts/`、QwenPaw 工作区 `/app/working/workspaces/default/skills/<name>/scripts/`、挂载目录 `/quantmind/skills/<name>/scripts/`。纯标准库脚本（无重依赖）可在 QwenPaw 本地直接跑。
> 3. **报告落盘**：股票报告页可见的 MD/PDF 报告，直接写 `/data/reports/trading_agents/{市场或类别}/{股票名}/`（QwenPaw 对 `/app/db` 有写权限，**直接写文件，不要 docker cp**）；过程数据 facts 写 `/data/reports/<类别>/`（`/data` 可写）。
> 4. **MD → PDF 转换（按优先级降级）**：
>    ① `docker exec -w /app quantmind python3 backend/scripts/md_to_pdf_report.py <输入.md> <输出.pdf>`（研报级排版，首选）；
>    ② docker 不可用时，**改用 QwenPaw 内置 `pdf` 技能**把 MD 转成 PDF；
>    ③ 两者都不可用则只交付 MD，并明确告知用户 PDF 未能生成及原因。
> 5. 本文中的 `~/.claude`、`cp -r ... ~/.claude/skills` 等说明仅适用于本地 Claude Code 维护者，**QuantBot 不要执行**。

# 智能策略选股技能

基于 QuantDB 数据的条件选股。支持自然语言、结构化条件、DSL 三种方式，选出符合要求的股票池并附带量化指标。

## 数据基础

选股完全基于 **QuantDB** 本地 parquet。字段映射以后端字典为准：`backend/services/engine/ai_strategy/steps/step1_stock_selection.py`（DSL 字段 → QuantDB 表/列），覆盖以下数据源：

| QuantDB 数据源 | 字段 | 覆盖 |
|---|---|---|
| `l1_factors` | 66 | 动量/流动性/概念热度/资金流 |
| `technical_indicators` | 24 | 均线/RSI/KDJ/MACD/波动率 |
| `financial` | 20 | 财务指标 |
| `valuation` | 15 | PE/PB/市值/ROE |
| `sentiment` | 14 | 市场情绪 |
| `margin` | 10 | 融资融券 |
| `stock_list` | 6 | 行业/ST/上市天数 |
| `daily`/`turnover` | 3 | 量价 |

## 认证

```bash
BASE=http://127.0.0.1:8000
TOKEN=$(curl -s -X POST $BASE/api/v1/auth/login -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"<管理员口令>","tenant_id":"default"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))")
AUTH="Authorization: Bearer $TOKEN"
CT="Content-Type: application/json"
```

## 1. 常用选股因子（前 30 高频字段）

| 字段 | 含义 | 单位 |
|---|---|---|
| `market_cap` / `total_mv` | 总市值 | 亿 |
| `float_mv` | 流通市值 | 亿 |
| `pe` / `pe_ttm` | 市盈率 | — |
| `pb` | 市净率 | — |
| `roe` | 净资产收益率 | % |
| `close` | 收盘价 | 元 |
| `pct_change` | 当日涨跌幅 | % |
| `turnover_rate` | 换手率 | % |
| `ma5` / `ma10` / `ma20` / `ma60` | 均线 | 元 |
| `ma_gap_5` / `ma_gap_20` | 均线偏离度 | % |
| `rsi_6` / `rsi_14` | RSI | — |
| `kdj_k` / `kdj_d` / `kdj_j` | KDJ | — |
| `macd_dif` / `macd_dea` / `macd_hist` | MACD | — |
| `return_1d` / `return_3d` / `return_5d` / `return_20d` / `return_60d` | 收益率 | % |
| `vol_std_5` / `vol_std_20` / `vol_std_60` | 波动率 | % |
| `vol_atr_14` | 14日ATR | — |
| `beta_20` | 20日Beta | — |
| `volume_ratio_5` / `volume_ratio_20` | 量比 | — |
| `main_flow` | 主力资金净流入 | 元 |
| `flow_net_amount` | 资金净流入总额 | 元 |
| `inst_ownership` | 机构持仓 | % |
| `concept_ai` / `concept_chip` 等 | 概念热度 | — |
| `industry` | 行业 | — |
| `is_st` | 是否ST | — |
| `listed_days` | 上市天数 | 天 |

完整字段清单与映射以**后端字段字典为准**（`backend/services/engine/ai_strategy/steps/step1_stock_selection.py` + `api/schemas/stock_pool.py`）。⚠️ 前端 `electron/src/features/strategy-wizard/factors/dictionary.ts` 已移除，勿再引用。

## 2. 方式一：自然语言解析（推荐给用户用）

```bash
# 解析自然语言为 DSL（内部：可先用此步确认字段能否被识别）
curl -s -X POST "$BASE/api/v1/strategy/parse-text" -H "$AUTH" -H "$CT" \
  -d '{"text":"市值大于500亿且ROE大于15%的沪深300成分股，剔除ST"}' \
  -o /tmp/pt.json -w "HTTP %{http_code}\n"
cat /tmp/pt.json | python3 -m json.tool --no-ensure-ascii | head -30
```

## 3. 方式二：结构化条件解析（最可控）

```bash
curl -s -X POST "$BASE/api/v1/strategy/parse-conditions" -H "$AUTH" -H "$CT" \
  -d '{"conditions":{"type":"numeric","factor":"pe","operator":"<","threshold":15}}' \
  -o /tmp/pc.json -w "HTTP %{http_code}\n"
cat /tmp/pc.json | python3 -m json.tool --no-ensure-ascii
```
**条件结构**：
- 数值条件：`{"type":"numeric","factor":"pe","operator":"<|<=|>|>=|=","threshold":15}`
- 趋势条件：`{"type":"trend","factor":"ma5","window":5,"direction":"above|below"}`
- 复合条件：`{"type":"composite","op":"and|or","children":[...]}`（可嵌套）

**返回**：`dsl`（如 `SELECT symbol WHERE pe < 15`）+ `mapping` + `quantdb_filters`（如 `[{field:"pe_ttm", operator:"<", value:15, table:"quantdb_valuation"}]`）

## 4. 方式三：DSL 直接查询（执行选股）

```bash
curl -s -X POST "$BASE/api/v1/strategy/query-pool" -H "$AUTH" -H "$CT" \
  -d '{"dsl":"SELECT symbol WHERE pe < 15 AND roe > 10","market":"CN","exchange":"SH"}'
```
**参数**：
- `dsl`：`SELECT symbol WHERE 条件 [AND/OR 条件...]`（字段用上面的因子名）
- `market`：CN / HK / US / CRYPTO
- `exchange`：SH / SZ / BJ（仅 A 股）

**返回**：`items`（每只股票 symbol/name/metrics，metrics 含市值/PE/ROE 等）+ `summary`（matchRate/totalCandidates/universeTotal/asOf）

## 5. 实战示例（可直接复用）

### 5.1 低估值蓝筹（PE<15 且 市值>500亿）
```bash
curl -s -X POST "$BASE/api/v1/strategy/query-pool" -H "$AUTH" -H "$CT" \
  -d '{"dsl":"SELECT symbol WHERE pe < 15 AND market_cap > 500 AND roe > 10","market":"CN"}'
```

### 5.2 动量强势（20日收益>10% 且 RSI>60）
```bash
curl -s -X POST "$BASE/api/v1/strategy/query-pool" -H "$AUTH" -H "$CT" \
  -d '{"dsl":"SELECT symbol WHERE return_20d > 10 AND rsi_14 > 60 AND turnover_rate < 20","market":"CN"}'
```

### 5.3 高波动小盘（波动大 + 市值小）
```bash
curl -s -X POST "$BASE/api/v1/strategy/query-pool" -H "$AUTH" -H "$CT" \
  -d '{"dsl":"SELECT symbol WHERE vol_std_20 > 5 AND market_cap < 100 AND pct_change > 0","market":"CN"}'
```

### 5.4 资金流入 + 概念热门（AI/半导体）
```bash
curl -s -X POST "$BASE/api/v1/strategy/query-pool" -H "$AUTH" -H "$CT" \
  -d '{"dsl":"SELECT symbol WHERE main_flow > 10000000 AND concept_ai > 0.5","market":"CN"}'
```

### 5.5 剔除 ST + 特定行业 + 换手活跃
```bash
curl -s -X POST "$BASE/api/v1/strategy/query-pool" -H "$AUTH" -H "$CT" \
  -d '{"dsl":"SELECT symbol WHERE is_st = false AND industry = 半导体 AND turnover_rate BETWEEN 3 AND 15","market":"CN"}'
```

## 6. 分析建议

选出的股票池可结合其他技能深入分析：
- **查新闻**：`/news/articles` 带 tickers 看利好/利空 → [[quantmind-operations]] 第 7 节
- **查推理分数**：`/models/inference/stock/{symbol}/history` 看模型评分
- **挖新因子**：[[rd-agent-factor-mining]] 补充更多筛选维度
- **训练模型**：选出的池子可喂给模型训练 → [[quantmind-operations]] 第 1 节

## 7. 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 422 字段不匹配 | DSL 用了映射表外字段 | 用上面的常用因子名，或查后端字典 `step1_stock_selection.py` |
| 选股结果为空 | 条件过严 | 放宽阈值，或去掉 AND 条件 |
| summary.matchRate 极低 | 条件偏窄 | 检查 totalCandidates 是否正常 |
| 想要全市场 | dsl 用 `SELECT symbol WHERE true` | 返回全部候选 |
