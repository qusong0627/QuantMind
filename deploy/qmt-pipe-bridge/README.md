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

**启动错峰**：``startup.grace_s``/``startup.step_s``（默认 8s + 3s/步）——
五个 worker 零延迟同时开跑时，QMT 单线程交易 API 在启动瞬间被打爆
（2026-11-06 三次实测：桥一启动，策略 40-60s 内必挂）。错峰后同一时刻只有
一个 worker 热身，rpc_worker 也延后消费队列，避免容器积压在启动窗口灌进来。
桥启动时 ``strategy_state`` 先置 **unresponsive**，首次 ``get_asset`` 探针成功
才放行交易查询——启动窗口内到达的交易类只读请求按判死处理（订单/行情不受
影响），不会灌进还没热身的策略。

**交易类 RPC 全局限速（令牌桶，双桶）**：容器请求桶 `rpc.trade_min_interval_s`
（默认 2s，容量 `trade_burst`=1）；桥内部轮询桶 `rpc.internal_trade_min_interval_s`
（默认 2s，容量 `internal_trade_burst`=4，positions 一轮要连发 asset+positions
两条）。两桶互不抢占——单桶会被桥自身轮询抢光令牌、把 SLTP/委托轮询饿死。
超速的容器请求回 ``TRADE_RPC_THROTTLED`` 快速失败，桥自身轮询按 ``skipped``
跳过；订单/撤单/行情不受限制。依据：2026-11-06 实测「桥停掉的 35 秒里账户
通道自己恢复，恢复流量后一分钟内又挂」——``get_trade_detail_data`` 连续查询
会长时间阻塞、空载可自愈，速率是可控变量。

## 为什么「经常要重启」与 2026-11-16 修复

QMT 的交易查询（`get_asset` / `get_positions` / `query_orders` / `query_trades`）
**只能在策略 adjust 主线程执行**，`get_trade_detail_data` 一旦阻塞，主线程就不再
tick；而 `ping` 与行情方法跑在后台 listener 线程，仍然正常——这就是「QMT 在线、
账户/委托查询全死」的假活。轻量恢复顺序必须是：

1. **清积压**：策略判死后容器仍在入队，重启 QMT 前先清交易类只读请求
   （订单/撤单永不删）：
   ```bash
   python scripts/qmt_rpc_queue_hygiene.py --account <资金账号>
   python scripts/qmt_rpc_queue_hygiene.py --account <资金账号> --trim \
     --drop-methods query_stock_orders,query_stock_asset,subscribe_whole_quote
   ```
2. **重启伴生桥**（断管道解锁主线程，不碰策略运行态 → Tap 不丢）；
3. 账户仍不恢复 → 在 QMT 策略编辑器里**停止 → 运行**（重新执行 init，装回 Tap）；
4. 最后才重启整个 QMT 客户端。

**2026-11-16 新增**：

- `pipe_redis_bridge.py` 在 `strategy_state=unresponsive` 期间**自动清理**队列里
  已积压的交易类只读请求（`bridge_gates.split_backlog`），并立即回
  `STRATEGY_UNHEALTHY` 失败回复；订单/撤单、行情类、解码失败条目一律保留。
  指标新增 `qm:qmt:pipe:metrics.purged_backlog`。这修的是「重启后积压回灌，
  存活 12h → 6min → 3min」那条事故链。
- `ping` 新增 `quote_push_ready`：false 表示全推行情订阅服务未装配（策略文件/
  配置旧，或 `quote_push.enabled=false`），此时 `subscribe_whole_quote` 会报
  `whole-quote push subscription is not configured on this server`。看门狗状态
  `qm:qmt:strategy:watchdog.quote_push_ready` 同步可见。修法是同步最新
  `bigqmt_signal_trader_strategy.py` 后在 QMT 编辑器停止/运行一次策略。
- `bridge_config.example.json` 已补齐 `rpc` 令牌桶、`startup` 错峰、
  `budget` 让路闸、`tap`/`sweep`、`watchdog.prefer_bridge_restart` 等键。
  **Windows 上的 `bridge_config.json` 若是旧版，按新 example 合并**；尤其是
  `rpc.internal_trade_min_interval_s=2` / `trade_min_interval_s=3` /
  `startup.grace_s=8` / `step_s=3` 与 `watchdog.prefer_bridge_restart=true`。

### 2026-11-16 上线清单（Windows，建议收盘后）

1. 备份现有 `pipe_redis_bridge.py` / `bridge_gates.py` / `qmt_strategy_watchdog.py` /
   `bridge_config.json`。
2. 复制本仓 `deploy/qmt-pipe-bridge/` 下：
   `pipe_redis_bridge.py`、`bridge_gates.py`、`qmt_strategy_watchdog.py` 到
   `qmt-tools\`（覆盖）；`bridge_config.example.json` 只作合并参考，**不要覆盖**
   你的 `bridge_config.json`。
3. 把 **`rpc` / `startup` / `budget` / `tap` / `sweep` / `watchdog`** 这几段按新
   example 合并进 `bridge_config.json`（尤其 `watchdog.prefer_bridge_restart=true`、
   `rpc.trade_min_interval_s=3`、`rpc.internal_trade_min_interval_s=2`、
   `startup.grace_s=8`、`startup.step_s=3`）。
4. 复制本仓 `deploy/qmt-bridge-kit/bigqmt_signal_trader/redis_rpc.py` 到 QMT
   python 目录下的 `bigqmt_signal_trader\`（覆盖）；如果 `ping` 的
   `quote_push_ready` 仍为 false，再把 `bigqmt_signal_trader_strategy.py` /
   `qmt_quote_tap.py` 一起覆盖，并在 QMT 编辑器里**停止 → 运行**策略一次
   （`reload_deployment` 不刷新顶层策略文件）。
5. 重启伴生桥（`start_pipe_bridge.cmd` 或看门狗自动拉起），**不需要重启 QMT
   客户端**；确认 `qm:qmt:pipe:metrics.strategy_state=ok`、`queue_len=0`。
6. 验证：
   ```bash
   # 容器内
   docker exec -w /app quantmind python -c "import asyncio,json; from backend.services.live_trading.services.tdx_push_service import tdx_pusher as p; print(asyncio.run(p.tdx_call('ping',{},source='qmt')))"
   # 期望 quote_push_ready: True；同时 qm:qmt:pipe:metrics 出现 purged_backlog
   ```
7. 若 `quote_push_ready=false`：先查 QMT python 目录
   `bigqmt_signal_trader_local_config.py` 里 `quote_push.enabled` 是否被显式关掉；
   否则就是策略文件旧，按第 4 步停/运行一次。

## 接口

伴生桥只监听 `127.0.0.1:17720`，提供 `/api/v1/qmt/call` 与 `/health`；
外部统一经 8550 桥的 `source=qmt` 访问。桥侧接口、字段映射、口径对账与排错见
`tools/bridge-windows/docs/QMT渠道接入说明_20261028.md`。

## 不提交的内容

`logs/`、`data/`、`bridge_config.json`、真实账号/Redis 密码、TQRelay token、
QMT 策略运行产生的任何文件。仓库里的 `bridge_config.example.json` 只有占位符。
