---
name: simulation-trading
description: "模拟交易 — 下单买卖、持仓管理、成交查询、账户状态、资金快照。在 QuantBot / Claude Code 中执行模拟交易、下单、查询持仓、查看账户、管理交易记录时使用。触发词：模拟交易、模拟下单、买入股票、卖出股票、查持仓、查账户、模拟账户、交易记录、下单、撤单"
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

# 模拟交易技能

QuantMind 模拟交易（纸面交易）完整操作：下单、持仓、成交、账户、资金快照、模拟盘启动。默认模拟资金 100 万，不影响实盘。

## 实盘 / 模拟盘切换

- **前端切换**：顶部 HeaderBar 右上角 **REAL/SIM 拨动开关**，切换实盘（通达信桥）与模拟盘。选择持久化到 `localStorage['qm:trading_mode_pref']`，默认**模拟盘**（`simulation`）。
- **后端分流**：账户按 `GET /api/v1/real-trading/status` 返回的 `mode` + 前端分流——`SIMULATION` 调 `/simulation/account`，`REAL` 调 `/account`。
- **启动参数**：`POST /api/v1/real-trading/start` 接收 `trading_mode=SIMULATION/REAL`；实盘需 `ENABLE_REAL_TRADING=true` 环境变量。
- **交易就绪检查**：`/api/v1/real-trading/trading-precheck` 返回信号就绪/交易权限（可交易/观察态/阻断）。

```bash
# 检查交易就绪（启动前必查）
curl -s -H "$AUTH" "$BASE/api/v1/real-trading/trading-precheck"
# 返回: {passed, items:[{key,passed,detail}], signal_readiness, trading_permission}
```

## 认证

```bash
BASE=http://127.0.0.1:8000
TOKEN=$(curl -s -X POST $BASE/api/v1/auth/login -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"<管理员口令>","tenant_id":"default"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))")
AUTH="Authorization: Bearer $TOKEN"
CT="Content-Type: application/json"
```

## 1. 账户状态

### 1.1 查看模拟账户
```bash
curl -s -H "$AUTH" "$BASE/api/v1/simulation/account"
# 返回: {cash, total_asset, market_value, positions, initial_equity, ...}
# 默认现金 100 万，positions 为当前持仓
```

### 1.2 模拟交易设置
```bash
curl -s -H "$AUTH" "$BASE/api/v1/simulation/settings"
```

### 1.3 重置模拟账户
```bash
curl -s -X POST -H "$AUTH" "$BASE/api/v1/simulation/reset"
```

### 1.4 资金快照
```bash
# 手动捕获资金快照
curl -s -X POST -H "$AUTH" "$BASE/api/v1/simulation/snapshots/capture"
# 每日资金快照（总资产/现金/市值序列）
curl -s -H "$AUTH" "$BASE/api/v1/simulation/snapshots/daily"
```

## 2. 下单（核心）

### 2.1 买入/卖出
```bash
curl -s -X POST -H "$AUTH" -H "$CT" "$BASE/api/v1/simulation/orders" \
  -d '{
    "symbol": "600519.SH",
    "side": "buy",            # buy / sell
    "order_type": "market",   # market / limit
    "quantity": 100,
    "price": null,            # 限价单必填，市价单可空
    "remarks": "AI策略买入"
  }'
```
**参数**：
| 字段 | 说明 |
|---|---|
| `symbol` | 股票代码（600519.SH / SH600519） |
| `side` | `buy` / `sell` |
| `order_type` | `market`（市价）/ `limit`（限价） |
| `quantity` | 数量（股） |
| `price` | 限价单必填，市价单可空 |
| `remarks` | 备注（可选） |

**返回**：`order_id` + 订单状态（pending/filled/...）

### 2.2 撤单
```bash
curl -s -X POST -H "$AUTH" -H "$CT" "$BASE/api/v1/simulation/orders/{order_id}/cancel" \
  -d '{"reason":"手动撤单"}'
```

## 3. 订单与成交查询

### 3.1 订单列表
```bash
curl -s -H "$AUTH" "$BASE/api/v1/simulation/orders"
# 返回: [{order_id, symbol, side, order_type, quantity, status, ...}]
```

### 3.2 单笔订单
```bash
curl -s -H "$AUTH" "$BASE/api/v1/simulation/orders/{order_id}"
```

### 3.3 成交记录
```bash
curl -s -H "$AUTH" "$BASE/api/v1/simulation/trades"
# 返回: [{trade_id, symbol, side, quantity, price, ...}]
```

### 3.4 单笔成交
```bash
curl -s -H "$AUTH" "$BASE/api/v1/simulation/trades/{trade_id}"
```

### 3.5 成交统计
```bash
curl -s -H "$AUTH" "$BASE/api/v1/simulation/trades/stats/summary"
# 返回: 盈亏/胜率/交易次数等统计
```

## 4. 模拟盘启动（real-trading）

模拟盘生命周期：策略绑定 → 预检查 → 启动 → 持仓跟踪 → 停止。

```bash
# 策略绑定到组合（策略信号 → 下单）
curl -s -X POST -H "$AUTH" -H "$CT" "$BASE/api/v1/portfolios/{portfolio_id}/bind-strategy" \
  -d '{"strategy_id":"strategy_xxx","model_id":"mdl_cn_xxx"}'

# 交易预检查（信号就绪 / 资金 / 风控）
curl -s -H "$AUTH" "$BASE/api/v1/real-trading/trading-precheck"

# 启动模拟盘
curl -s -X POST -H "$AUTH" -H "$CT" "$BASE/api/v1/real-trading/start" \
  -d '{"portfolio_id":"xxx","strategy_id":"strategy_xxx"}'
# 停止模拟盘
curl -s -X POST -H "$AUTH" -H "$CT" "$BASE/api/v1/real-trading/stop"

# 运行状态 / 日志 / 订单 / 历史
curl -s -H "$AUTH" "$BASE/api/v1/real-trading/status"
curl -s -H "$AUTH" "$BASE/api/v1/real-trading/logs"
curl -s -H "$AUTH" "$BASE/api/v1/real-trading/orders"
curl -s -H "$AUTH" "$BASE/api/v1/real-trading/history"
```

**组合（Portfolio）管理**：
```bash
# 创建/列表组合
curl -s -X POST -H "$AUTH" -H "$CT" "$BASE/api/v1/portfolios" -d '{"name":"我的组合","initial_capital":1000000}'
curl -s -H "$AUTH" "$BASE/api/v1/portfolios"
# 组合详情/分布/绩效
curl -s -H "$AUTH" "$BASE/api/v1/portfolios/{id}"
curl -s -H "$AUTH" "$BASE/api/v1/portfolios/{id}/distribution"
curl -s -H "$AUTH" "$BASE/api/v1/portfolios/{id}/performance"
# 结算/快照/同步
curl -s -X POST -H "$AUTH" "$BASE/api/v1/portfolios/{id}/settlement"
curl -s -X POST -H "$AUTH" "$BASE/api/v1/portfolios/{id}/snapshot"
curl -s -X POST -H "$AUTH" "$BASE/api/v1/portfolios/{id}/sync-status"
```

**持仓管理**：
```bash
# 组合持仓
curl -s -H "$AUTH" "$BASE/api/v1/portfolios/{id}/positions"
curl -s -X POST -H "$AUTH" -H "$CT" "$BASE/api/v1/portfolios/{id}/positions" -d '{"symbol":"600519.SH","quantity":100}'
# 调整/清仓/更新价格
curl -s -X POST -H "$AUTH" -H "$CT" "$BASE/api/v1/portfolios/{id}/adjust"
curl -s -X POST -H "$AUTH" -H "$CT" "$BASE/api/v1/portfolios/{id}/close"
curl -s -X PUT -H "$AUTH" -H "$CT" "$BASE/api/v1/positions/{position_id}/price" -d '{"price":1500.0}'
```

## 5. 实战流程（推荐）

当用户要求"模拟交易/买入/卖出"时：
1. **查账户**：`/simulation/account` 看现金和持仓
2. **确认标的**：查股票当前价（用 [[quantdb-sdk]] 或 K线）
3. **下单**：`/simulation/orders` POST（市价/限价）
4. **确认成交**：`/simulation/orders/{order_id}` 查状态
5. **查看持仓**：`/simulation/account` 看更新后的 positions
6. **统计**：`/simulation/trades/stats/summary` 看收益

当用户要求"启动模拟盘 / 跑实盘模拟"时：
1. **建组合**：`/portfolios` POST 创建
2. **绑定策略**：`/portfolios/{id}/bind-strategy`（strategy_id + model_id）
3. **预检查**：`/real-trading/trading-precheck` 确认信号就绪
4. **启动**：`/real-trading/start`
5. **监控**：`/real-trading/status` + `/simulation/account`
6. **停止**：`/real-trading/stop`

## 6. 相关技能

- **[[stock-market-analysis]]** — 分析选哪只股票（371 字段 + 风险评分）
- **[[smart-strategy-stock-picking]]** — 条件选股找标的
- **[[quantdb-sdk]]** — 查 K 线/估值确定买卖价
- **[[backtest-center]]** — 回测验证策略后再模拟执行
- **[[ai-ide-strategy-writing]]** — 写策略并执行回测

## 7. 常见问题

| 现象 | 处理 |
|---|---|
| 买入资金不足 | 查 account cash，减仓或调小 quantity |
| 卖出无持仓 | 查 account positions 确认持有 |
| 限价单未成交 | 价格远离现价，改市价或调价 |
| 想重置 | `/simulation/reset` 恢复 100 万初始资金 |
| 模拟盘启动失败 | 查 `/real-trading/trading-precheck` 看信号就绪状态 |
| 策略信号未生成 | 确认模型已推理（见 [[quantmind-operations]]） |
