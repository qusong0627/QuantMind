# QMT 管道桥（策略入口 + 伴生桥）

QMT 的**只读数据面**：QMT 策略 ↔ 命名管道 ↔ 伴生桥 ↔ Redis ↔ 通达信桥 8550。
策略进程零网络、零文件 IO；对外只由 8550 桥提供 `source=qmt`。

## 文件放哪

| 文件 | 目标位置 |
|---|---|
| `deploy/qmt-bridge-kit/QMT_BRIDGE_V2.py` | QMT 的 python 目录（如 `C:\xmt\python`） |
| `deploy/qmt-bridge-kit/qmt_quote_tap.py` | 同上 |
| `deploy/qmt-pipe-bridge/*` | Windows 交易机任意目录（如 `...\bridge-windows\qmt-tools`） |

依赖：`deploy/qmt-bridge-kit/` 里的
`bigqmt_signal_trader_redis_rpc_runtime.py` / `bigqmt_signal_trader_strategy.py`
（与本仓库同版本）需对 QMT 的 venv 可见；默认找
`C:\xmt\venv\Lib\site-packages`，可用环境变量 `QMT_VENV_SITE_PACKAGES` 覆盖。

## 配置

1. 复制 `bridge_config.example.json` 为 `bridge_config.json`。
2. 填 `account_id`（资金账号）与 Redis 地址；**账号不要提交 git**。
3. QMT 策略编辑器加载 `QMT_BRIDGE_V2.py`；把其中 `ACCOUNT_ID = ''` 填成同一账号。
   策略使用 `transport=pipe`、`redis_enabled=False`，不发起任何网络连接。

## 交易写入（默认关）

`bridge_config.json` 的 `source_http.allow_orders` 默认 `false`。即使打开，
桥侧 `qmt_orders.enabled` 与 QMT 策略 `ALLOW_ORDER_METHODS=True` 也必须同时打开
（三道闸），QMT 才能收到下单/撤单。写入面仍经过 `risk_gate`：ST/退名称黑名单、
单笔金额上限（默认 20 万）、价格偏离（默认 5%）、每分钟笔数（默认 30）；
`client_order_id` 作为 24h 幂等键，重复提交返回首次结果。

## 启动与看护

```powershell
# 启动伴生桥（脱离 SSH）
powershell -NoProfile -ExecutionPolicy Bypass -File svc_start_pipe_bridge.ps1

# 注册看门狗（每分钟；心跳故障才重启，数据停摆只记 DATA_STALE）
powershell -NoProfile -ExecutionPolicy Bypass -File register_pipe_watchdog.ps1
```

`bridge_health.py` 退出码：0=正常；1=心跳缺失/过期；2=读不到心跳；
3=心跳正常但账户快照停摆（只告警，不自动重启——交易时段重启会打断在途 RPC）。

### 策略看门狗（交易查询通道卡死 → 自救/告警）

`qmt_strategy_watchdog.py` 每分钟探测一次：`ping`（后台 listener 线程）与
`get_asset`（adjust 主线程）。ping 通但 `get_asset` 连续两次超时 → 发
`reload_deployment`（主线程还活着时这是最后的自愈机会）；reload 也超时 →
判定主线程死锁，写 Redis `qm:qmt:strategy:watchdog` 并 CRITICAL 告警，提示人工
在 QMT 策略编辑器里重启策略（GUI 是唯一恢复手段）。

为什么需要它：trade-context RPC（`get_asset` / `get_positions` / `query_orders` …）
必须走 adjust 主线程（`get_trade_detail_data` 在后台线程返回空），主线程一旦被
QMT 的 C++ 调用挂住，这些查询全部超时，而 `ping` 与行情方法仍在后台线程 25ms
正常应答——这就是「QMT 在线·账户滞后」的假活。2026-11-06 两次实测：
09:37:46 与 10:14:23，策略日志在 `adjust cadence` 之后同时断掉。

保护：队列里有订单/撤单 RPC 时不 reload；冷却 10 分钟；每日最多 3 次；
只在交易时段自动 reload；状态全部落在 `qm:qmt:strategy:watchdog`（TTL 1 天）。

部署（文件放到 `qmt-tools\` 与 `qmt-tools\qmt-external-bridge\`）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File register_strategy_watchdog.ps1
```

### 账户通道熔断（积压不回灌）

2026-11-06 事故链：QMT 策略主线程被交易类查询挂死后，容器轮询继续入队，伴生桥
每 15s 才超时放掉一条，队列越积越多；**重启 QMT 后这些积压立刻被灌进新策略**
——三次重启的存活时间 12h → 6min → 3min。现在伴生桥每 30s 用 `get_asset` 探活
（`ping` 与行情跑在后台 listener 线程，主线程挂死时仍然正常，不能判健康）：

* 连续 2 次探活超时 → `strategy_state=unresponsive`；
* 此后**交易类只读请求本地立即失败**（方法集合见 `bridge_gates.py`，与 QMT 侧
  `LISTENER_DEFERRED_METHODS` 同源，含 `query_stock_*` 线名），不转发、不占管道、
  重启后不回灌；
* **订单/撤单永远放行**；行情类（`get_full_tick` 等）不受影响；
* 探针一成功立即回到 `ok`。状态经 `/health` 的 `strategy_state` 暴露。

配套：轮询间隔已收紧（positions 10s / orders 5s / trades 15s），降低主线程压力。

## 接口

伴生桥只监听 `127.0.0.1:17720`，提供 `/api/v1/qmt/call` 与 `/health`；
外部统一经 8550 桥的 `source=qmt` 访问。桥侧接口、字段映射、口径对账与排错见
`tools/bridge-windows/docs/QMT渠道接入说明_20261028.md`。

## 不提交的内容

`logs/`、`data/`、`bridge_config.json`、真实账号/Redis 密码、TQRelay token、
QMT 策略运行产生的任何文件。仓库里的 `bridge_config.example.json` 只有占位符。
