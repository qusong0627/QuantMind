---
name: backtest-center
description: "回测中心 — 快速回测、专家模式、回测历史、策略对比、参数优化、策略管理、高级分析。在 QuantBot / Claude Code 中运行 Qlib 回测、对比策略、优化参数、分析回测结果、管理策略时使用。触发词：回测、回测中心、运行回测、策略对比、参数优化、回测历史、专家模式、高级分析、模型回测、推理回测"
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

# 回测中心技能

QuantMind 回测中心的完整操作指南。覆盖 7 大功能：快速回测、专家模式、回测历史、策略对比、参数优化、策略管理、高级分析。

## 架构

回测走 **engine 服务**（8001）的 Qlib 引擎，API 网关（8000）代理。核心路径 `/api/v1/qlib/*`。

## 认证

```bash
BASE=http://127.0.0.1:8000
TOKEN=$(curl -s -X POST $BASE/api/v1/auth/login -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"<管理员口令>","tenant_id":"default"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))")
AUTH="Authorization: Bearer $TOKEN"
CT="Content-Type: application/json"
```

## 1. 快速回测（单次 Qlib 回测）

### 1.0 向量化极速回测（新）
`QlibBacktestRequest.use_vectorized: bool`（默认 false）触发**向量化极速引擎**（纯 pandas 矩阵运算，全市场近 1 年从 500s+ 降到秒级~分钟级）。
```bash
curl -s -X POST -H "$AUTH" -H "$CT" "$BASE/api/v1/qlib/backtest" \
  -d '{
    "strategy_type": "CustomStrategy",
    "strategy_content": "STRATEGY_CONFIG = {...}",
    "model_id": "mdl_cn_xxx",
    "start_date": "2025-01-01",
    "end_date": "2025-12-31",
    "universe": "csi300",
    "initial_capital": 1000000,
    "benchmark": "000300.SH",
    "use_vectorized": true,
    "strategy_params": {"signal": "<PRED>", "topk": 50},
    "qlib_provider_uri": "db/qlib_data",
    "qlib_region": "cn"
  }'
```
**安全门**：`use_vectorized=true` 时系统自动检测策略是否"向量化安全"（纯 TopK 全换 + 无加权/无止损/无 pool_file/无自定义类）。不安全策略自动退回 step 模式保语义。

### 1.1 提交回测
```bash
curl -s -X POST -H "$AUTH" -H "$CT" "$BASE/api/v1/qlib/backtest" \
  -d '{
    "strategy_id": "strategy_xxx",
    "start_date": "2024-01-01",
    "end_date": "2024-12-31",
    "initial_capital": 1000000,
    "benchmark": "000300.SH"
  }'
```
**返回**：`backtest_id` + 初始结果。后续用 backtest_id 查结果/日志/分析。

### 1.2 模型滚动回测（管理端）
```bash
# 可用回测交易日
curl -s -H "$AUTH" "$BASE/api/v1/admin/models/backtest/trading-dates?start=2025-01-01&end=2025-12-31"

# 可用于回测的模型列表
curl -s -H "$AUTH" "$BASE/api/v1/admin/models/list-for-backtest"

# 启动模型滚动回测
curl -s -X POST -H "$AUTH" -H "$CT" "$BASE/api/v1/admin/models/backtest" \
  -d '{"model_id":"mdl_xxx","start":"2025-01-01","end":"2025-12-31"}'
# ⚠️ 多周期对比回测已下线：/api/v1/admin/models/backtest/multi-horizon 路由不存在（2026-09 清理）
```

### 1.3 推理回测（选股策略事件驱动）
```bash
curl -s -X POST -H "$AUTH" -H "$CT" "$BASE/api/v1/admin/models/inference-backtest" \
  -d '{
    "model_id":"mdl_xxx",
    "start_date":"2025-01-01",
    "end_date":"2025-12-31",
    "signal_mode":"stored",
    "strategy":{"top_k":20,"side":"long"}
  }'
```
**signal_mode**：`stored`（用已存信号）/ `realtime`（实时生成）

## 2. 专家模式（云端策略开发与回测）

### 2.1 策略管理
```bash
# 策略列表
curl -s -H "$AUTH" "$BASE/api/v1/strategies"

# 策略模板
curl -s -H "$AUTH" "$BASE/api/v1/strategies/templates"

# 创建策略
curl -s -X POST -H "$AUTH" -H "$CT" "$BASE/api/v1/strategies" \
  -d '{"name":"我的策略","description":"动量策略","strategy_type":"TopkDropoutStrategy","params":{"topk":20}}'

# 激活策略
curl -s -X POST -H "$AUTH" "$BASE/api/v1/strategies/{strategy_id}/activate"
```

## 3. 回测结果 / 历史

```bash
# 回测结果（含净值/回撤/交易/指标）
curl -s -H "$AUTH" "$BASE/api/v1/qlib/results/{backtest_id}"
# 回测成交明细
curl -s -H "$AUTH" "$BASE/api/v1/qlib/results/{backtest_id}/trades"
# 回测状态（轮询）
curl -s -H "$AUTH" "$BASE/api/v1/qlib/results/{backtest_id}/status"
# 删除回测记录
curl -s -X DELETE -H "$AUTH" "$BASE/api/v1/qlib/results/{backtest_id}"

# 我的回测历史
curl -s -H "$AUTH" "$BASE/api/v1/qlib/history/me"
# 模型滚动回测历史（管理端）
curl -s -H "$AUTH" "$BASE/api/v1/admin/models/backtest/history/{model_id}?limit=20"
curl -s -H "$AUTH" "$BASE/api/v1/admin/models/backtest/history/{model_id}/{run_id}"
curl -s -X DELETE -H "$AUTH" "$BASE/api/v1/admin/models/backtest/history/{model_id}/{run_id}"
```

## 4. 策略对比

### 4.1 对比两个回测结果
```bash
curl -s -H "$AUTH" "$BASE/api/v1/qlib/compare/{id1}/{id2}"
```

### 4.2 多模型对比

多周期对比回测（`/api/v1/admin/models/backtest/multi-horizon`）已于 2026-09 下线；多策略/多模型对比改用 `compare`（结果级）或在训练侧按单周期分别训练模型再各自回测。

## 5. 参数优化（遗传算法）

```bash
# 提交参数优化（默认算法）
curl -s -X POST -H "$AUTH" -H "$CT" "$BASE/api/v1/qlib/optimize" \
  -d '{
    "strategy_id": "strategy_xxx",
    "start_date": "2024-01-01",
    "end_date": "2024-12-31",
    "param_ranges": {
      "topk": [5, 50],
      "n_drop": [1, 10],
      "rebalance_period": [5, 30]
    },
    "generations": 10,
    "population_size": 20
  }'
# 返回 optimization_id

# 遗传算法优化（专门入口）
curl -s -X POST -H "$AUTH" -H "$CT" "$BASE/api/v1/qlib/optimize/genetic" \
  -d '{"strategy_id":"strategy_xxx","start_date":"2024-01-01","end_date":"2024-12-31","param_ranges":{"topk":[5,50]},"generations":10,"population_size":20}'

# 查询优化结果
curl -s -H "$AUTH" "$BASE/api/v1/qlib/optimization/{optimization_id}"
# 优化历史
curl -s -H "$AUTH" "$BASE/api/v1/qlib/optimization/history"
```

## 6. 高级分析（深度性能分析）

> 高级分析端点自带 `/api/v1/analysis` 前缀。

### 6.1 基础风险
```bash
curl -s -X POST -H "$AUTH" -H "$CT" "$BASE/api/v1/analysis/basic-risk" \
  -d '{"backtest_id":"xxx"}'
```

### 6.2 绩效归因
```bash
curl -s -X POST -H "$AUTH" -H "$CT" "$BASE/api/v1/analysis/performance" \
  -d '{"backtest_id":"xxx"}'
```

### 6.3 交易统计
```bash
curl -s -X POST -H "$AUTH" -H "$CT" "$BASE/api/v1/analysis/trade-stats" \
  -d '{"backtest_id":"xxx"}'
```

### 6.4 基准对比
```bash
curl -s -X POST -H "$AUTH" -H "$CT" "$BASE/api/v1/analysis/benchmark" \
  -d '{"backtest_id":"xxx"}'
```

### 6.5 持仓分析
```bash
curl -s -X POST -H "$AUTH" -H "$CT" "$BASE/api/v1/analysis/position" \
  -d '{"backtest_id":"xxx"}'
```

### 6.6 因子分析
```bash
curl -s -X POST -H "$AUTH" -H "$CT" "$BASE/api/v1/analysis/factor-analysis" \
  -d '{"backtest_id":"xxx"}'
```

### 6.7 风格归因
```bash
curl -s -X POST -H "$AUTH" -H "$CT" "$BASE/api/v1/analysis/style-attribution" \
  -d '{"backtest_id":"xxx"}'
```

### 6.8 风险指标与告警
```bash
# 风险指标（回撤/夏普/波动等）
curl -s -H "$AUTH" "$BASE/api/v1/qlib/risk/{backtest_id}/metrics"
# 风险告警
curl -s -H "$AUTH" "$BASE/api/v1/qlib/risk/{backtest_id}/alerts"
# 风险配置
curl -s -X POST -H "$AUTH" -H "$CT" "$BASE/api/v1/qlib/risk/{backtest_id}/config" \
  -d '{"max_drawdown":0.15,"var_confidence":0.95}'
```

### 6.9 回测日志
```bash
curl -s -H "$AUTH" "$BASE/api/v1/qlib/logs/{backtest_id}"
```

## 7. 报告导出

```bash
# CSV / PDF / Excel 报告
curl -s -H "$AUTH" "$BASE/api/v1/qlib/export/{backtest_id}/csv" -o backtest_report.csv
curl -s -H "$AUTH" "$BASE/api/v1/qlib/export/{backtest_id}/pdf" -o backtest_report.pdf
curl -s -H "$AUTH" "$BASE/api/v1/qlib/export/{backtest_id}/excel" -o backtest_report.xlsx
```

## 8. 实战流程（推荐）

当用户要求"回测策略/模型"时：
1. **确认策略**：`/strategies` 或 `/admin/models/list-for-backtest` 选回测对象
2. **确认日期**：`/admin/models/backtest/trading-dates` 选区间
3. **运行回测**：`/admin/models/backtest` 或 `/qlib/backtest`
4. **查日志**：`/qlib/logs/{id}` 确认完成
5. **深度分析**：`/qlib/analysis/*` + `/qlib/risk/{id}/metrics`
6. **对比**：多策略用 compare（结果级对比）
7. **导出**：PDF / Excel 报告
8. **参数调优**：`/qlib/optimize` 遗传算法搜索最优参数

## 9. 相关技能

- **[[ai-ide-strategy-writing]]** — AI-IDE 写策略（自然语言生成 Qlib 策略代码）
- **[[simulation-trading]]** — 模拟交易（下单/持仓/成交）
- **[[smart-strategy-stock-picking]]** — 条件选股（生成股票池）
- **[[quantmind-operations]]** — 模型训练/推理

## 10. 常见问题

| 现象 | 处理 |
|---|---|
| 回测无结果 | 确认日期区间有交易日数据，查 `/qlib/logs/{id}` |
| 策略列表空 | 先创建策略或从模板同步 `/strategies/templates` |
| 参数优化慢 | 减少 generations/population_size |
| 报告导出失败 | 确认 backtest_id 存在且有完整结果 |
