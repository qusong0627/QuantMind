# 大 QMT 真单镜像 — 部署与上线手册

> 模拟盘虚拟成交 → 同一笔委托按限额同步到大 QMT 真实账户，并回收真实成交/持仓。
> **双轨并行**：虚拟账本行为不变，真单独立记账，可逐笔对账（滑点/费用/拒单）。

---

## 一、这套东西是什么

| 层 | 组件 | 位置 |
|----|------|------|
| 传输 | big-convert RPC（Redis 通道） | QMT 内置 Python 跑服务端；Linux 侧是客户端 |
| 适配 | `QmtExecBroker` | `backend/services/live_trading/services/broker_client.py` |
| 客户端 | `QmtExecClient` | `backend/services/live_trading/services/qmt_exec_client.py` |
| 回收 | `qmt_exec_poller`（2s 轮询，日切重置、成交去重） | `.../services/qmt_exec_poller.py` |
| 账户 | `run_qmt_account_sync_task`（30s 快照 → `real_account_snapshots`） | `.../services/qmt_account_sync_task.py` |
| 镜像 | `real_mirror_service`（开关/限额/白黑名单/熔断/队列） | `.../services/real_mirror_service.py` |
| 控制面 | `qmt_mirror` 路由（状态/开关/急停/限额/名单/补交/对账） | `backend/services/trade/routers/qmt_mirror.py` |
| 前端 | 「模拟交易设置 → 大 QMT 真单镜像」卡片 | `electron/src/pages/trading/components/QmtMirrorCard.tsx` |

```
┌─ QuantMind 容器（Linux） ─────────────────────────────────────┐
│ 模拟盘 / 托管任务 → 虚拟成交（不变）                            │
│        └─ real_mirror_service（默认关，白名单点名才镜像）        │
│              └─ dispatch_internal_strategy_order（REAL 尾段）   │
│                    └─ QmtExecBroker ──► RPC                     │
│ qmt_exec_poller ── 轮询委托/成交 ──► orders / order_history      │
│ qmt_account_sync_task ──► real_account_snapshots                │
└───────────────────────────┬────────────────────────────────────┘
                            │ Redis（专用实例，密码 + 防火墙白名单）
┌───────────────────────────▼──── Windows（QMT 机器）────────────┐
│ QMT 内置 Python：big-convert RPC 服务端（常驻 + 看门狗）          │
│   入口 BIGQMT_REDIS_DRYRUN.py；rpc_allow_order_methods=True      │
└─────────────────────────────────────────────────────────────────┘
```

**为什么不是外接 xtquant**：miniQMT/xtquant 外接通道已按监管下线（commit `b5e38d51`），
大 QMT 上走「内置执行端」才是官方合规通道。

---

## 二、前置条件

| 条件 | 说明 |
|------|------|
| 大 QMT（投研版） | Windows 机器，QMT 已安装并登录，账号有交易权限 |
| QMT 内置 Python | 3.6 解释器（big-convert 服务端跑在这里） |
| 专用 Redis | 建议独立实例（默认 6380 + 密码），只监听局域网；**不要复用业务 Redis** |
| 网络 | Windows 与 QuantMind 主机同一局域网互通（Redis 端口放行给 QuantMind 主机 IP） |
| Linux 侧 | `pip install "xtquant-big-convert[redis]"`（`requirements.txt` 已固定 0.3.31） |

---

## 三、Windows 侧部署（QMT 机器）

1. **探测环境**（只读，绝不下单）
   ```bat
   :: 用 QMT 内置 Python 跑
   D:\国金QMT交易端\bin.x64\python.exe probe_qmt_env.py --account <资金账号>
   ```
   把生成的 `qmt_probe_report.json` 发回。结论决定走哪条分支：
   - 分支 A：`xtquant.xttrader` 可用 → 可另议外接方案（本手册不覆盖）
   - 分支 B（预期）：`xtquant-big-convert` 可导入 → 按下面继续

2. **在 QMT 内置 Python 安装 big-convert**
   ```bat
   D:\...\bin.x64\python.exe -m pip install "xtquant-big-convert[redis]"
   ```
   装完用这条命令定位 4 个服务端文件的所在目录（就是 pip 的 site-packages）：
   ```bat
   D:\...\bin.x64\python.exe -c "import bigqmt_signal_trader_strategy as m, os; print(os.path.dirname(m.__file__))"
   ```

3. **就位服务端文件**（拷到 QMT 的 `python` 目录，如 `D:\国金证券QMT交易端\python\`）：
   `bigqmt_signal_trader/`（整个包）、`bigqmt_signal_trader_strategy.py`、
   `bigqmt_signal_trader_redis_rpc_runtime.py`、`BIGQMT_REDIS_DRYRUN.py`。

4. **写 QMT 端私有配置**：在 QMT 的 `python` 目录新建 `bigqmt_signal_trader_local_config.py`
   （含账号密码，**不要提交 git**）：
   ```python
   # coding: utf-8
   BIGQMT_ACCOUNT_ID = "资金账号"          # 必须与页面/环境变量里的 QMT_EXEC_ACCOUNT_ID 一致
   BIGQMT_REDIS_CONFIG = {
       "host": "Redis地址", "port": 6380, "db": 0, "password": "Redis密码",
       "rpc_allow_order_methods": False,  # ★ 下单开关：先 False 验只读链路，确认风控后改 True
       "rpc_process_in_listener": True,
       "rpc_listener_methods": ("*",),
       "rpc_background_threads": True,    # redis 传输用 True；换 zmq/pipe 必须改 False
       "schedule_adjust": True,
       "schedule_adjust_interval": "100nMilliSecond",
   }
   ```

5. **在 QMT 策略编辑器里加载运行 `BIGQMT_REDIS_DRYRUN.py`**（只加载这一个文件，它自己 import 其余模块）。
   QMT 需处于**实盘模式**。启动成功时 QMT 输出面板会打印：
   ```
   [bigqmt_shell] local redis config loaded keys=[...]
   [bigqmt_shell] local account config loaded=True
   [bigqmt_rpc] started channel=bigqmt:rpc:req:你的账号
   [bigqmt_signal_trader] init ok
   ```
   > ⚠️ **不要**用 `python.exe BIGQMT_REDIS_DRYRUN.py` 当普通脚本跑，也**不要**用「独立 Python 进程」
   > 方式：那样 QMT 不会注入 `passorder`/`get_trade_detail_data`，`init()` 不会被调用，
   > 服务端看起来"跑起来了"其实什么都没监听（面板日志以 `finished` 结尾）。
   > QMT 重启后要在策略编辑器里重新运行该策略。
   > 若券商沙箱拦截 `import redis`，改用自包含的 `bigqmt_no_redis/`（ZMQ 传输，配置里加 `"transport": "zmq"` 且 `rpc_background_threads=False`）。

6. **放行防火墙**：Redis 端口只对 QuantMind 主机 IP 开放。

7. **服务端排错日志**：QMT 的 `python` 目录下 `logs/bigqmt_*.log`（保留 7 天）。

---

## 四、Linux 侧配置

### 4.1 页面配置（推荐）

「模拟交易设置 → 券商实盘接入」选择 **大 QMT(执行端)**，填写：

| 字段 | 说明 |
|------|------|
| `enabled` | `true` 启用执行端 |
| `account_id` | QMT 资金账号 |
| `account_type` | `STOCK` 普通 / `CREDIT` 信用 |
| `strategy_name` | 默认 `quantmind`（用于识别本系统的委托） |
| `redis_host/port/db/password` | QMT 那台机器的 Redis 地址与密码 |

保存后点「**测试连接**」，应返回真实资金与持仓数量。再在「券商实盘接入」顶部把
A 股通道选为「大 QMT(执行端)」（写入 `broker:selected:CN=qmt_exec`）。

> 页面配置写入 `broker:config:qmt_exec`（Trade Redis），优先级高于 env；改完立即生效
> （客户端会清缓存，下一次轮询重读）。

### 4.2 env 兜底（可选）

见 `docker-compose.yml` 的 `QMT_EXEC_*` 段；页面未填时按 env 走。

---

## 五、上线步骤（建议顺序）

1. **只读链路**：完成 §4.1 的「测试连接」；页面显示通道就绪。
2. **开户镜像前先确认风控基线**：`ENABLE_REAL_TRADING=true` 且 `broker:selected:CN=qmt_exec`
   （缺一不可，否则镜像只记日志不下单）。
3. **打开镜像卡**：设置页「大 QMT 真单镜像」→ 设置限额（首期建议：单笔 ≤1 万、
   单日 ≤5 万、最多 5 只、单日 ≤20 笔）→ 保存。
4. **填白名单**：`tenant` 或 `tenant:user` 或 `tenant:user:strategy`。
   **空名单 = 不下任何单**，这是有意设计（宁可不下，不可乱下）。
5. **打开镜像开关**（热开关，无需重启）。
6. **小额验证**：让白名单内策略产生一笔最小单（100 股），确认：
   - 大 QMT 里出现同向同量委托，成交后 `orders`/`order_history` 落库；
   - 对账卡当天能看到 虚拟价 / 真实成交价 / 滑点 / 费用差；
   - 虚拟账本金额不变（双轨）。
7. **异常演练**（择机）：急停 → 拒单熔断 → QMT 重启 → Redis 断连 → 重复回报。

---

## 六、运维与风控

| 能力 | 位置 | 说明 |
|------|------|------|
| 热开关 | `mirror:enabled` | 页面开关，随时停/开 |
| 急停 | `mirror:kill` | 优先级最高；**读取失败时按「已急停」处理**（fail-closed） |
| 白名单 | `mirror:whitelist` | `*` / tenant / tenant:user / tenant:user:strategy |
| 黑名单 | `mirror:blacklist` | 按标的前缀式（如 `SH600519`）排除 |
| 限额 | `mirror:config` | 单笔/单日金额、单日笔数、单日标的数、滑点上限 |
| 日额度 | `mirror:daily:{date}:*` | Lua 原子预占/回滚，跨日自动失效（TTL 3 天） |
| 熔断 | `mirror:rejects` | 连续拒单 ≥ 阈值 → 自动急停 + 通知 |
| 快照新鲜度 | env | 交易时段内账户快照 >5 分钟未落库 → 站内通知（1 小时冷却；`QMT_SYNC_STALE_ALERT_SECONDS`/`_COOLDOWN_SECONDS` 可调） |
| 非交易时段 | `mirror:queue` | 入队，开盘由 drainer 补交（队列 TTL 7 天） |
| 对账 | `GET /api/v1/qmt-mirror/reconcile` | 虚拟 vs 真单逐笔，含滑点/费用差/拒单原因 |

**限额校验顺序**（任一不满足即拒绝并记录原因）：
黑名单 → 市场 → 白名单 → 急停 → 开关 → 通道就绪 → 价格漂移（滑点上限）→
资金/持仓 → 单笔 → 单日金额 → 单日笔数 → 单日标的数。

---

## 七、回滚

1. 页面**急停**（或清空白名单）→ 立刻停发新单；
2. 关闭镜像开关 → 退回纯模拟；
3. `broker:selected:CN` 改回 `tdx` 或清空 → 实盘通道切走；
4. QMT 侧服务端停掉不影响模拟盘（镜像只是旁路）。

---

## 八、故障排查

| 现象 | 排查 |
|------|------|
| 页面「通道未就绪」 | `ENABLE_REAL_TRADING` 是否为 true；A 股券商是否已选 `qmt_exec` |
| 测试连接报 `NOT_CONNECTED` | QMT 机器上的服务端没跑 / Redis 地址密码不对 / 防火墙未放行 |
| 测试连接报 `ORDER_DISABLED` | 服务端 `rpc_allow_order_methods` 未开 |
| 服务端面板日志以 `finished` 结尾、没有 `[bigqmt_rpc] started` | 用普通脚本/独立进程方式跑了入口 → 必须在 QMT **策略编辑器**里加载运行 `BIGQMT_REDIS_DRYRUN.py` |
| 委托已下但状态不动 | poller 未跑（`trade` 服务日志 `qmt-exec-poller`）；或 QMT 未推送委托（重启服务端） |
| 镜像单被跳过 | 对账卡「未找到真单」+ 原因；看 `blocked_reason` 与日志 `[Mirror]` |
| 连续拒单熔断 | 看 `mirror:rejects` 与通知；处理后在页面「解除急停」 |
| 收到「账户快照超时未更新」 | QMT 是否登录 / 服务端是否在跑 / Redis 通道；看日志 `[QmtSync]` |
| 非交易时段下单 | 正常入队，开盘后 drainer 补交；也可页面「立即补交」 |

日志关键词：`[Mirror]`、`[QmtExec]`、`[QmtExecPoller]`、`[MirrorAPI]`（trade 服务 `logs/`）。

---

## 九、安全与合规

- Redis 必须**独立实例 + 密码 + 局域网白名单**，不要暴露公网。
- `account_id`/密码等敏感字段在页面上**只写不回读**。
- 所有控制面写操作记审计日志（用户/租户/前后值）。
- 程序化交易需按券商要求完成**报备**；单账户申报频率远低于高频线。
- 首期务必保持小额限额；确认对账无误后再逐步放宽。

> ⚠️ 本项目仅供学习研究与技术演示，不构成投资建议。真单交易风险自负。
