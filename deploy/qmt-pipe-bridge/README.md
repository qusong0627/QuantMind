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
3=心跳正常但账户快照停摆（只告警，不自动重启——交易时段重启会打断在途 RPC）；
4=存在 inflight/unknown 订单意图（processing 或 inflight hash），**只告警不重启**，
等人工核对柜台后用 `bridge_ops.py resolve` 归档。

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

### 2026-11-16 审计后的 P0（订单意图 / 快照 / 止损）

这一批是审计确认的 High，和上面的「防误重启」不是同一层：

- **订单意图持久化**：`rpc_worker` 用 `BLMOVE` 把队首原子搬到
  `bigqmt:rpc:processing:{account}`，再写 `qm:qmt:pipe:inflight:{account}`；
  进程在两步之间崩溃不会出现「队列没有了、Redis 也没记录」。重启时
  `recover_processing()` 把订单标 `unknown` 并保留，绝不重放。
- **未知结果不可重试**：发送超时/回写失败 → `ORDER_RESULT_UNKNOWN` +
  `retriable=false`；同 remark 重试直接返回该结果，不再发第二笔。
- **看门狗看到在途单**：`bridge_health.py` 退出码 4；`pipe_watchdog.ps1` 与
  `qmt_strategy_watchdog.py` 同时检查 queue + processing + inflight，任一有单
  就 defer 重启。`pipe_watchdog.ps1` 改为按 `data/pipe_bridge.pid` 精确杀，
  PID 文件缺失时用 17720 监听者 PID，不再按命令行子串扫杀。
- **快照分组件计时**：`positions_worker` 的 `get_asset` 失败不再刷新资产时间；
  payload 带 `asset_observed_at` / `positions_observed_at` /
  `asset_status` / `positions_status` / `stale_reasons`。资产阈值独立
  `positions.asset_stale_s`（默认 120s），持仓沿用 `source_http.stale_after_s`。
- **回报先落再记 seen**：桥 `events_worker` 先 `XADD` 成功再记 seen；后端
  `QmtExecPoller` 在 `db.commit()` 成功后才更新 `_seen_orders/_seen_trades`。
- **止损唯一所有者**：桥自带 `StopLossDaemon` 默认关闭，且只有
  `sltp_daemon.owner=bridge` 才会启动；QuantMind `sltp_executor` 是唯一卖出者。
  卖出失败（无仓/超时/拒单/异常）不再无条件 `enabled=false`，会保留规则并退避。
- **运维工具**：`python bridge_ops.py list` 查看 inflight/processing；
  人工核对柜台后 `python bridge_ops.py resolve --request-id <id> --outcome filled`。
  `resolve` **只归档，不重发、不改订单账本**。

### 2026-11-16 上线清单（Windows，建议收盘后）

1. 备份现有 `pipe_redis_bridge.py` / `bridge_gates.py` / `qmt_strategy_watchdog.py` /
   `bridge_config.json`。
2. 复制本仓 `deploy/qmt-pipe-bridge/` 下：
   `pipe_redis_bridge.py`、`bridge_gates.py`、`bridge_account_state.py`、
   `bridge_order_intent.py`、`bridge_stability.py`、`bridge_health.py`、`bridge_ops.py`、
   `qmt_strategy_watchdog.py` 到 `qmt-tools\`（覆盖）；**整个
   `qmt_today_cache\` 目录**复制到 `qmt-tools\qmt-external-bridge\`（V1 今日行情缓存）；
   `pipe_watchdog.ps1` 到 `qmt-tools\`（覆盖）；`bridge_config.example.json`
   只作合并参考，**不要覆盖**你的 `bridge_config.json`。
3. 把 **`rpc` / `startup` / `budget` / `tap` / `sweep` / `today_cache` / `watchdog`**
   这几段按新 example 合并进 `bridge_config.json`（尤其
   `watchdog.prefer_bridge_restart=true`、`rpc.trade_min_interval_s=3`、
   `rpc.internal_trade_min_interval_s=2`、`startup.grace_s=8`、`startup.step_s=3`）。
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

## 2026-11-27 稳定性加固（自适应调速 / 交易只读缓存 / 重启风暴护栏）

「QMT 在线·账户滞后」的根因是 adjust 主线程被 `get_trade_detail_data` 挂死，
而容器轮询与桥内轮询仍在按固定速率加压。本次加固（详见
`docs/QMT桥稳定性加固_机构级细案_20261127.md`）：

- **自适应 Governor**（`bridge_stability.py`）：交易查询 EWMA 延迟 > `health.slow_ms`
  （默认 1200ms）即把间隔翻倍，失败再翻倍（封顶 `health.max_multiplier=8`），
  连续 `health.recover_ok=3` 次健康才逐级减半；`qm:qmt:pipe:metrics.governor_multiplier`
  可直接看到当前倍率。
- **交易只读短缓存**：`get_asset/get_positions` 2s、`query_orders/query_trades` 1s；
  订单/撤单方法永不缓存，且成功后立即 `invalidate`。指标 `trade_cache_hits/misses`。
- **重启风暴护栏**：`qmt_strategy_watchdog.py` 对 `bridge_restart_ts` 做 50 条/24h
  裁剪 + 指数退避（60s→…→30min）+ 30 分钟 ≥4 次判风暴并**停止自动重启**，
  告警要求人工在 QMT 编辑器停止→运行策略。
- 新增配置键见 `bridge_config.example.json`：`rpc.trade_read_cache_*`、
  `health.slow_ms/very_slow_ms/ewma_alpha/max_multiplier/recover_ok`、
  `watchdog.bridge_restart_backoff_*/storm_*/stamp_keep`。旧配置不写也按默认值运行。

客户端侧（QuantMind 后端）同步加固：QMT 非当前交易券商时执行轮询自动空转
（有未平 QMT 单仍低频对账）；桥不健康时指数退避 5s→120s；账户同步对陈旧/空账户
分级退避；`qmt_pipe_guard` 读取 `qm:qmt:strategy:watchdog`，对「主线程卡死 >
180s / 重启风暴」告警；**推送按相关性门控**（QMT 是当前券商或有未平单才推），
Tap 未装配默认只作为状态展示（`QM_QMT_ALERT_TAP_DISABLED=true` 才告警）。

## 2026-11-28 今日行情缓存 V1+V2+V3（快照 / 1m / 热集 tick / 3s 全市场）

- 新增 `qmt_today_cache/`：内存最新快照、1m 聚合、热集 tick 环、全市场 3s 快照、Parquet part/manifest、异步写线程；
- 桥的 `get_full_tick` 命中内存直接回包；新增只读方法 `cache_today_1m`（列式，单次 ≤600 只）、
  `cache_hot_ticks`（列式，单次 ≤800 只，带 `stale/age_ms/gap`）、`cache_status`；
  `get_market_data_ex` / 订单/撤单**原样透传**；
- 落盘：`type=bar1m`（30 天）、`type=tick_hot`（10 天）、`type=snapshot3s`（全市场 3s，5 天）；
- **V3 来源键**：QMT 写 `market:snapshot:qmt:{后缀}`，TDX 写 `market:snapshot:tdx:{前缀小写}`；
  canonical `market:snapshot` 保持不变；后端 `unified_quote_cache` 合并并可选写 `market:snapshot:merged:{后缀}`；
- 默认 `today_cache.enabled=true`；`false` 时完全透传，行为与旧版一致；`snapshot3s_enabled` 可单独关；
- 落盘目录 `qmt-tools/data/cache/qmt/date=YYYYMMDD/type=.../hour=HH/part-*.parquet`；
- **pyarrow 预检**（写在桥的 Python 环境里）：
  ```powershell
  "C:\Program Files\Python312\python.exe" -c "import pyarrow; print(pyarrow.__version__)"
  ```
  没有 pyarrow 时自动降级 `.jsonl.gz` spool（状态 `cache_degraded=1`），桥照常运行；
- 状态：`qm:qmt:pipe:metrics` 的 `cache_*` 字段（含 `cache_hot_*`、`cache_snapshot3s_*`）；或 RPC `cache_status`；
- 后端 `qmt_cache_parquet.py` 用 DuckDB 读 `/mnt/tdx-shared/bridge-windows/qmt-tools/data/cache/qmt`（可用 `QM_QMT_CACHE_DIR` 覆盖）；
- 回滚：`bridge_config.json` 里 `today_cache.enabled=false` 重启桥，或还原 `.bak` 文件。

## 2026-11-27 稳定启动/重启（不残留旧进程）

新增文件：
- `start_pipe_bridge.ps1`：稳定启动器
  - 全局互斥锁，串行化并发启动；
  - 只杀**可证实是桥**的进程：PID 文件 PID、17719/17720 端口 owner、命令行含 `pipe_redis_bridge`；
  - 端口被其他程序占用时 **拒绝启动**（不会误杀 TQ/文件桥）；
  - 通过 `Win32_Process.Create` 分离启动，**不随 SSH/RDP 会话断开而死**；
  - 启动后等待 17720 监听，30 秒不通返回非 0；
  - 日志 `logs/start_pipe_bridge.log`。
- `start_pipe_bridge.cmd`：包装器，调用上面的 ps1（双击/命令行均可）；
- `start_pipe_bridge_raw.cmd`：原来的裸 python 启动命令（由 ps1 调用，不要直接双击）；
- `pipe_watchdog.ps1`：看门狗改为调用 ps1 启动；
- `register_start_pipe_bridge.ps1`：注册计划任务 `QMTTools-StartPipeBridge`（SYSTEM、最高权限、IgnoreNew）。

推荐启动方式（任意 SSH/RDP 会话都能用，进程会留在后台）：
```powershell
Start-ScheduledTask -TaskName QMTTools-StartPipeBridge
# 或
cmd /c C:\new_tdx_mock\PYPlugins\bridge-windows\qmt-tools\start_pipe_bridge.cmd
```
**一键启动整个交易台**（TDX 5 项 + QMT 桥）：
```
C:\new_tdx_mock\PYPlugins\bridge-windows\启动交易台.cmd
# ASCII 别名：start_trading_desk.cmd
```
- QMT 已在健康运行时不重启（`NOOP`）；`-Restart` 同时强制重启 TDX 与 QMT；
- 自动化用 `-NoPause -NoUi`；一键启动会调用 `qmt-tools\cache_quality_check.py` 质检缓存与关键参数，
  跳过用 `-NoCacheCheck`；最终打印 `TDX rc / QMT rc / CACHE rc`；
- 该包装器调用 `scripts\launcher.ps1`（TDX）后，再调用本目录的 `start_pipe_bridge.ps1`（QMT）。
**不要**在 SSH 里用 `Start-Process` 直接拉 `pipe_redis_bridge.py`：会话断开时子进程会被一并杀掉（本次部署实测）。

验证：
```powershell
Get-NetTCPConnection -LocalPort 17719,17720 -State Listen | Select LocalPort,OwningProcess
Get-Content C:\new_tdx_mock\PYPlugins\bridge-windows\qmt-tools\logs\start_pipe_bridge.log -Tail 20
```

回滚：还原 `start_pipe_bridge.cmd.bak-20261127-1814` / `pipe_watchdog.ps1.bak-20261127-1814`；`QMTTools-StartPipeBridge` 任务可保留或删除。

## 接口

伴生桥只监听 `127.0.0.1:17720`，提供 `/api/v1/qmt/call` 与 `/health`；
外部统一经 8550 桥的 `source=qmt` 访问。桥侧接口、字段映射、口径对账与排错见
`tools/bridge-windows/docs/QMT渠道接入说明_20261028.md`。

## 不提交的内容

`logs/`、`data/`、`bridge_config.json`、真实账号/Redis 密码、TQRelay token、
QMT 策略运行产生的任何文件。仓库里的 `bridge_config.example.json` 只有占位符。
