# QMT 真单链路完善计划（实盘压测问题闭环）

> 背景：2026-09-10 ~ 09-11 对 40327478 真账户做了买卖压测（含止盈止损、未成交、涨跌停、
> 部分成交场景）。本文是发现问题的闭环计划，所有结论都有实测或代码证据。
> 相关模块：`backend/services/live_trading/services/`（qmt_exec_*、real_mirror_service、
> internal_strategy_dispatcher、trading_engine）、`backend/services/trade/services/order_timeout_scanner.py`。

---

## 0. 摘要

| 优先级 | 问题 | 状态 |
|---|---|---|
| P0-1 | QMT 通道**没有止盈止损执行链路**（触发只存在于 TDX 桥/模拟盘） | Phase 1 解决 |
| P0-2 | 镜像 2% 偏离闸门**静默跳过**且上层显示 success；急跌日止损信号全灭 | Phase 2.1/2.2 |
| P0-3 | 撤单竞态返回假成功（柜台已全成/撤销被拒 -1，本地仍报"撤单已发送"） | Phase 4.2 |
| P1-1 | 镜像真单限价 = 昨收×(1±2%)，与盘口脱钩 | Phase 2.3 |
| P1-2 | 未成交/部分成交无 TTL / 追价 / 余量处理 | Phase 3.2 |
| P1-3 | `mirror:` 标记存 remarks，被成交回报覆盖 → 超时扫描器托管保护失效 | Phase 2.4 |
| P1-4 | 无整手/板块校验（碎股 251150、科创 200 起） | Phase 2.5 |
| P1-5 | SIM 一次全额记账，真单可能部分/不成交，双轨分歧无对账 | Phase 3.1 |
| P2-1 | 超范围价柜台不拒 → 无价格保护（程序 bug 会直接市价成交） | Phase 4.1 |
| P2-2 | 对废单/已成交单撤单仍返回 True；镜像 skip 无通知 | Phase 4.2/4.3 |

**实测获得的确定性能力**（执行器直接采用）：

- 卖出保护价（跌停价）报单 = 扫单成交：卖 603359.SH @2.21（跌停）→ 成交 2.34。
- 柜台容忍超范围价：卖 300091.SZ @1.80（低于跌停 1.97）→ 成交 2.40（买超涨停同理）。
- 市价委托可用：`order_type="MARKET"`（映射最新价委托）→ 成交 4.10。
- 涨跌停价可从桥直接取：`get_instrument_detail {"code": ...}` → `UpStopPrice/DownStopPrice/PreClose`（主板/ST/科创/北交所六只票验证）。
- 本地真卖链路无风控拦截（门槛在柜台 `251005 可用不足`）；卖出落库 + poller 2s 回收正常。

---

## 1. Phase 1 — QMT 止损/强平执行器（核心）

### 1.1 目标语义

用户语义："跌破止损/到了止盈，**一定要卖掉**；跌停封死卖不掉要让我知道。"

- 触发即执行，一次性（当日不重复），不做"只提醒"。
- 卖出用**保护价**（跌停价）报单：既保证只要盘口有买盘就成交，又不会因为报低价而卖在低价
  （实测：限价 2.21 成交 2.34，成交价永远是盘口最优价）。
- 跌停封死（无买盘）时：挂跌停价排队 + 超时通知"剩余未成交，原因是流动性枯竭"。
- 不做自动追价：保护价已是当日最优可报价格，撤单重挂只会丢失队列优先级（时间优先）。
  这是与"追价"直觉相反的**有意决策**，见 §6。

### 1.2 架构与数据流

```
QMT 桥 get_full_tick (轮询 3s, 批量)
        │
        ▼
sltp_executor  ── 规则表(Redis) ──► check_sltp_trigger(price, entry, cfg)   [复用 tdx_quote_feed 纯函数]
        │ 触发（armed → triggered）
        ▼
柜台持仓快照 get_positions ── 数量（can_use_volume 全量，碎股允许）
        │
        ▼
保护价：get_instrument_detail.DownStopPrice（缓存当日）或 MARKET
        │
        ▼
dispatch_internal_strategy_order(trading_mode=REAL, remarks="sltp:<rule>")
        │  落 orders 表 ── qmt_exec_poller(2s) 回收状态/成交
        ▼
executor 监控本地订单行：终态→通知结果；超时未成交→告警（剩余量+原因推测）
```

**为什么走内部真单链路**（而非直连客户端）：落 orders 表、poller 自动对账、UI 可见；
与 `qmt_flatten_positions.py` 同一模式（已验证可行）。绕过的是**镜像闸门**——REAL 分支
根本不经过 `real_mirror_service`，所以 2% 偏离闸门天然不适用。

### 1.3 关键设计决策

| 决策 | 依据 |
|---|---|
| 触发规则复用 `check_sltp_trigger` | 与 TDX 桥 `stop_loss_daemon` 同口径，避免两套语义 |
| 保护价取桥的 `DownStopPrice`，不自己算 | 板块/ST 涨跌幅规则复杂（实测：*ST 主板 ±10%、*ST 创业板 ±20%、北交所 ±30%），桥给权威值 |
| 数量默认取 `can_use_volume` 全量 | 全量卖出允许碎股（不足 100 一次性卖出）；部分卖出时整手对齐 |
| 部分卖出整手规则 | SH/SZ/BJ 整 100；科创（688/689）部分卖出 ≥200；不足则降级为全量卖出 |
| entry_price 默认取柜台 `open_price` | 用户不必手工录入成本；规则可覆盖 |
| trailing 最高价由 executor 维护（Redis，只升不降） | 与 `check_sltp_trigger` 的 `highest_price` 约定一致 |
| 触发一次/日 + 状态机落 Redis | 防重复下单；`POST /reset` 或改配置重新武装 |
| 全部动作发通知 | 触发/提交/成交/部分/超时/失败，六类事件 |

### 1.4 交付物清单

| 文件 | 内容 |
|---|---|
| `backend/services/live_trading/services/sltp_executor.py` | 新服务：配置读写、规则状态机、触发循环、下单、跟踪、通知 |
| `backend/services/live_trading/services/qmt_exec_client.py` | 新增 `get_full_tick(codes)`、`get_instrument_detail(code)` 两个公开方法（封装已验证的 RPC） |
| `backend/services/trade/routers/qmt_sltp.py` | `GET/PUT /api/v1/qmt-sltp/config`、`GET /status`、`POST /reset` |
| `backend/services/trade/main.py` | 注册路由 + 启动 `run_qmt_sltp_executor_task()`（与 poller/scanner 同处，失败不影响启动） |
| `backend/scripts/qmt_sltp_ctl.py` | 运维 CLI：`--arm/--list/--enable/--disable/--reset/--status` |
| `backend/tests/test_qmt_sltp_executor.py` | 单元测试（见 §1.6） |

Redis 键：

- `qmt:sltp:executor:config` — 配置（含 rules、开关、轮询间隔、保护价模式）
- `qmt:sltp:executor:state` — 每条规则状态：`armed/triggered/submitted/filled/partial/failed`、
  `highest_price`、`order_id`、时间戳

配置示例：

```json
{
  "enabled": false,
  "user_id": "1",
  "poll_interval_sec": 3,
  "protect_price_mode": "limit_floor",
  "pending_alert_sec": 300,
  "remainder_policy": "alert_only",
  "close_reminder_sec": 300,
  "rules": [
    {"symbol": "600036.SH", "entry_price": null, "quantity": null,
     "stop_loss_pct": 0.05, "take_profit_pct": 0.10, "trailing_stop_pct": null}
  ]
}
```

> 旧键名 `unfilled_alert_sec` 仍兼容（`merge_config` 自动映射到 `pending_alert_sec`）。

### 1.5 状态机

```
armed ──触发──► triggered ──下单成功──► submitted ──部成/全成──► partial/filled (通知)
  ▲                │                        │
  │                └─下单失败──► failed ◄────┴─超时未成交──► 通知(挂队中) ──终态─► partial/filled/cancelled
  └────────────────── POST /reset / 改规则 ──────────────────┘
```

### 1.6 测试与验收

单元测试（pytest，mock client / Redis / dispatcher）：

1. 触发：止损/止盈/移动止损三种各 ≥2 例（含 `highest_price` 只升不降）。
2. 数量：SH 全量碎股、部分卖出整手对齐、科创 200 规则、`can_use=0`（T+1）跳过。
3. 保护价：涨跌停可取 → 用跌停价；取不到 → fail-closed 跳过并告警；`market` 模式用 MARKET。
4. 状态机：一次/日不重复触发；reset 后可再触发；下单失败落 failed。
5. 跟踪：超时未成交只通知一次；终态通知含已成交/剩余量。
6. 开关：`enabled=false` 时循环空转不下单。

实盘验收（test14 容器 + 真账户小额 100 股）：

- A. 正常止损：设 entry=现价×1.03、stop=2% → 触发后以跌停价报单 → 柜台成交价=盘口买一。
- B. T+1 锁定标的：`can_use=0` → 跳过并告警。
- C. 挂队场景：挂单价格远离盘口（模拟跌停封死）→ 超时通知"剩余未成交"。
- D. 全链路回归：orders 行 remarks=`sltp:*`、poller 回收成交、position 变化与柜台一致。

---

## 2. Phase 2 — 镜像链路修复

### 2.1 跳过可见化（P0-2）
`mirror_virtual_fill` 的返回值目前被 `internal_strategy_dispatcher.py:281` 丢弃。改为把
mirror 结果（`submitted/skipped(reason)/queued/failed`）**上抛到 dispatch 返回值**，并对
`skipped/failed` 发通知。验收：急跌日模拟卖出后，前端/通知能看到"真单未下：price_drift"。

### 2.2 强平类订单 bypass 价格闸门（P0-2）
给镜像 payload 增加 `bypass_price_gate` 来源标记（sltp/强平/人工平仓来源），
`_submit_payload` 对该标记只做"参考价存在 + 非零"校验，不做 2% 偏离跳过。
验收：偏移 8% 的模拟止损单能下出真单。

### 2.3 真单限价改用实时盘口（P1-1）
现状：`limit = ref(昨收)×(1±2%)`（实测 D2：40.74 = 41.57×0.98）。
改为：`live = get_full_tick` 实时价；`limit = live×(1−滑点)`（卖）/`live×(1+滑点)`（买）；
昨收只用于 **sanity 上界**（如偏离 >15% 视为异常数据 → fail-closed）。
**开放问题**：需先确认虚拟成交价来源（实时盘口还是 EOD 数据）。若 SIM 用 EOD 价，
则镜像本质是"次日跟随"，本项需与用户重新确认语义后再动。

### 2.4 `mirror:` 标记加固（P1-3）
现状：标记存 `orders.remarks`，成交回报（`order.remarks = msg`）会覆盖它 →
`order_timeout_scanner._not_broker_managed_clause()` 失效 → 若订单仍 `submitted`，
30 分钟后被本地误判 EXPIRED（柜台还挂着）。当前柜台 `status_msg` 多为空所以未爆雷。
改法（二选一）：① 匹配改用 `client_order_id` 前缀（`mir-%`，不可被覆盖）；
② 新增独立列/Redis 标记。推荐 ①，改动最小、无迁移。

### 2.5 整手/板块校验前置（P1-4）
在 dispatcher/broker 提交前按板块校验数量（SH/SZ 100 整、科创买入 200 起/部分卖 200 起、
北交所 100 起 1 递增、全量卖出豁免碎股），失败即 `rejected` 并带人类可读原因，
不再依赖柜台 `251150`。与 Phase 1 的执行器共用同一校验函数。

---

## 3. Phase 3 — 对账与余量策略

### 3.1 双轨对账（P1-5）
每日收盘后对比：SIM 台账成交 vs 真单成交 vs 柜台持仓变化，输出差异报表（差异 > 阈值发通知）。
数据源：`sim_orders/sim_trades`、`orders/trades`、`get_positions`。

### 3.2 未成交/部分成交余量策略（P1-2）
为真单增加可配置生命周期（默认关闭，避免影响现有单）：

- `pending_alert_sec`（默认 300）：未成交 → 通知；
- `remainder_policy`：`alert_only`（默认）/ `cancel` / `requote_at_protect_price`
  （仅在价格偏离保护价时重挂，避免无意义丢失队列优先级）；
- 收盘前 5 分钟提示"在途单将随日终自动失效"。

### 3.3 收盘清理核对
日终核对：柜台委托全部终结（A 股当日有效）、本地无 `submitted` 残留（与现有 30 分钟
扫描器互补：扫描器管"本地判死"，本项管"柜台到底还有没有单"）。

---

## 4. Phase 4 — 风控与可观测性加固

### 4.1 价格保护（P2-1）
提交前校验 `|报价 − 实时盘口|` 超过阈值（如 10%）→ 拒绝并告警（防程序 bug 打出
离谱价格被当作市价单执行）。**sltp/强平来源豁免**（保护价本来就是跌停价）。

### 4.2 撤单结果如实上报（P0-3 / P2-2）
`trading_engine.cancel_order_execution`：broker 返回 False 时不再无条件 `return True`；
按原因返回（`already_filled` / `counter_rejected` / `accepted_waiting_report`）。
`QmtExecBroker.cancel_order` 对"柜台已终态"的 -1 返回明确原因码。

### 4.3 通知全覆盖（P2-2）
以下事件必须发通知：镜像 skip（含原因）、下单失败、部分成交、超时未成交、撤单失败、
EXPIRED 标记。统一走 `backend/shared/notification_publisher`。

---

## 5. 排期

| 阶段 | 内容 | 预估 |
|---|---|---|
| Phase 1 | 止损执行器（含测试 + CLI + 实盘小额验收） | 1.5–2 天 |
| Phase 2 | 镜像链路 5 项修复 | 1 天 |
| Phase 3 | 对账 + 余量策略 | 1–1.5 天 |
| Phase 4 | 风控与可观测性 | 0.5 天 |

依赖关系：Phase 1 独立可先行；Phase 2.2（bypass 标记）与 Phase 1 互补但互不阻塞。

---

## 6. 明确不做（决策记录）

1. **不做自动追价/撤单重挂（默认）**：卖出保护价 = 当日最低可报价格，已是最激进报价；
   再撤再挂只会把队列优先级拱手让人（时间优先）。只有"报价偏离保护价"时才有重挂意义。
2. **不做市价单兜底跌停封死**：无买盘时市价单同样不成交，且柜台市价类型有额外风险
   （无价格保护）。跌停封死 = 只能排队，系统职责是**如实告警**，不是假装能卖。
3. **不绕过本地可用量校验**：真实可用量以柜台为准（`can_use_volume`），本地台账不作为
   拒绝依据（实测：本地链路不拦，柜台 `251005` 把关）。
4. **不自动平掉全部持仓**：执行器只作用于显式配置的规则标的，不做"全账户止损"。

---

## 7. 复现证据索引（本次压测）

| 结论 | 证据 |
|---|---|
| 保护价成交 | T18-B1 卖 603359.SH @2.21 → 成交 2.34 |
| 超范围价被接受 | T18-B2 卖 300091.SZ @1.80（跌停 1.97）→ 成交 2.40 |
| 市价委托可用 | T18-B3 卖 600289.SH MARKET → 成交 4.10 |
| 涨停价买入 | T18-B4 买 600289.SH @4.86（涨停 4.66）→ 成交 4.12 |
| 镜像静默跳过 | T19-D1 688596.SH 偏离 8.05% → skip，dispatch 返回 success |
| 真单限价=昨收×0.98 | T19-D2 600036.SH 真单价 40.74 |
| 撤单假成功 | T16 全成后撤单被拒 -1，`cancel_order_execution` 返回 True |
| 部分成交/分批回报 | T15/T17 6 笔 500 股明细 vs 柜台一次性 FILLED |
| mirror 标记被覆盖 | orders 表：成交的镜像单 remarks='成交回报' |
| 涨跌停价可取 | `get_instrument_detail` 六只票（主板/ST/科创/北交所） |

---

## 8. 实施与验证状态（2026-09-11）

P1–P4 全部落地，代码位于 `next` 分支。

| 阶段 | 状态 | 关键落点 |
|---|---|---|
| Phase 1 止损执行器 | ✅ 已实现 | `sltp_executor.py`（触发一次/日、保护价报单、`can_use_volume` 全量、涨跌停取桥权威值、六类通知）、`routers/qmt_sltp.py`、`scripts/qmt_sltp_ctl.py` |
| Phase 2.1/2.2 跳过可见化+强平 bypass | ✅ | `record_skip/load_skips`（Redis 哈希，7 天 TTL）+ `_is_forced_exit` 前缀（`sltp:`/`flatten:`/`forced-exit:`）→ `bypass_price_gate` |
| Phase 2.3 实时盘口限价 | ✅ | bypass 路径限价改用盘口价，昨收仅做 20% sanity 上界（`_SANITY_MAX_DRIFT`） |
| Phase 2.4 mirror 标记加固 | ✅ | `order_timeout_scanner.is_broker_managed`（remarks 前缀 + `mir-` cid 双口径，remark 被成交回报覆盖仍可识别） |
| Phase 2.5 整手预检前置 | ✅ | `internal_strategy_dispatcher._sell_lot_violation` + `_fetch_latest_real_account_snapshot`（拿不准放行，柜台 251150 仍兜底） |
| Phase 3.1 双轨对账 | ✅ | `dual_book_reconciliation_task.py`，每日 15:10（`MIRROR_RECONCILE_*`），skip 事件解释缺口的判定为 explained |
| Phase 3.2 余量策略 | ✅ | `pending_alert_sec` / `remainder_policy`（`alert_only`/`cancel`/`requote_at_protect_price`）/ `close_reminder_sec`；旧键 `unfilled_alert_sec` 兼容 |
| Phase 3.3 收盘核对 | ✅ | `close_cleanup_audit_task.py`，每日 15:05（`CLOSE_AUDIT_*`），柜台未终结 vs 本地残留（`no_exchange_order_id`/`counter_order_missing`/`counter_terminal`） |
| Phase 4.1 价格保护 | ✅ | `check_price_protection_band`（含 2% 容差，`sltp-`/`flat-`/`flatten-`/`mir-` 豁免），取不到合约详情时放行 |
| Phase 4.2/4.3 撤单如实上报+通知 | ✅ | `cancel_order_verbose` 原因码 → `_CANCEL_FAILURE_REMARKS` → 备注 + 警告通知；镜像 skip 通知（30 分钟/标的/原因节流） |

**验证**：QMT/SLTP 相关回归 302 passed / 1 skipped（容器内 pytest）；
改动文件 `ruff check` 全绿（仅剩存量问题）。trade 服务重启后三条新任务已按默认
安全值运行：`[SltpExec]`（`enabled=false` 空转）、`[Reconcile]` 15:10、
`[CloseAudit]` 15:05。

**已知后续项**：`sltp_executor.py` 905 行略超 800 行指引（触发/执行/监控/运行时装配
同文件），后续可按"核心执行 vs 运行时装配"拆分。

---

## 9. 代码审查修复（2026-09-11，第二轮）

P1–P4 落地后做了一轮独立代码审查（结论：WARNING——启用执行器前先修 HIGH 1/2）。
全部发现已修复并补测，改动仍在 `next`。

| 级别 | 问题 | 修法 | 落点 |
|---|---|---|---|
| HIGH 1 | 超时扫描器的托管保护漏了 `sltp-`/`flat-`/`flatten-` 前缀；备注模式列表被 `dict.fromkeys` 前的写法吞掉「通达信桥委托」包含式 | 补齐 cid 前缀；前缀式/包含式合并去重 | `order_timeout_scanner.py` |
| HIGH 2 | 触发后先落 `triggered` 再下单：崩溃会留「触发未落单」状态，当日不再重试也不告警（真单漏卖） | ①委托号改为**当日固定** `sltp-{symbol}-{yyyymmdd}-g{代数}`，崩溃重试同号被 dispatcher 幂等去重（不重复下单）；②`triggered` 且无委托号视为可重试；③收盘后仍无委托号的告警一次（`_notify_stranded_triggers`） | `sltp_executor.py` |
| MEDIUM 3 | `PUT /enabled`、CLI `--enable` 把「读配置失败」当空配置写回 → Redis 抖动时抹掉规则表 | 新增 `set_enabled`（读失败抛错 → 503）；CLI `_load` 改严格读；`--status/--evaluate` 顶层捕获后友好退出 | `sltp_executor.py` / `routers/qmt_sltp.py` / `qmt_sltp_ctl.py` |
| MEDIUM 4 | 卖单整手预检拿隔日快照可能误拦合法全量卖出 | 只取**当日**快照（`snapshot_date`）；数量 ≥ 可用量×99% 视为全量；非 A 股（HK/US）不做预检；拿不准放行 | `internal_strategy_dispatcher.py` |
| MEDIUM 5 | 规则 `side=BUY` 可写入，触发会变成「越止越买」 | 路由校验只允许 SELL；`normalize_rule` 对存量脏配置强制 SELL 并告警 | `routers/qmt_sltp.py` / `sltp_executor.py` |
| MEDIUM 6 | 收盘核对在 QMT 通道未配置时每天都报假警；本地残留把桥单也算进去（假 `counter_order_missing`） | 通道未配置直接跳过（且不写 done 标记，当日启用后可补跑）；本地单按 QMT 通道 cid 前缀过滤 | `close_cleanup_audit_task.py` |
| MEDIUM 7 | 本地订单查询失败被吞 → 报表静默变绿 | 查询异常上抛并记入 `report["errors"]`（→ `ok=False` + 通知） | `close_cleanup_audit_task.py` |
| LOW 8 | 备注模式列表可能被去重逻辑吃掉包含式匹配 | 同 HIGH 1 | `order_timeout_scanner.py` |
| LOW 9 | 行情长期缺失无感知 | 连续 `_TICK_MISS_ALERT_THRESHOLD`(10) 轮缺 tick 告警一次（恢复后计数归零，可再次告警） | `sltp_executor.py` |
| LOW 10 | 派发闭包写死 `tenant_id="default"` | `_build_default_deps(redis, tenant_id=...)` 跟随配置 | `sltp_executor.py` |
| LOW 11 | 设置页把止损止盈关掉后，其阈值仍作为规则缺省值 | `trigger_config` 忽略 `enabled=False` 的回落配置 | `sltp_executor.py` |
| LOW 12 | 执行器每轮整份回写状态，冲掉并发的 `POST /reset` / CLI `--rm` | `save_state(dirty=…, removed=…)` 只覆盖本轮改动过的规则（读回合并）；reset/初始化仍整份写 | `sltp_executor.py` |

**新增测试**（`test_qmt_sltp_executor.py` 新增 15 例 + 两个既有测试文件各补 2 例）：
幂等委托号/崩溃重试复用同号/reset 后换新号、收盘后滞留触发告警一次、状态合并写、
`set_enabled` 读失败不写回、tick 缺失告警、回落配置 enabled 门控、side 强制 SELL、
路由拒 BUY、快照当日过滤/非 A 股豁免/全量容差、QMT 通道过滤、通道未配置跳过核对。

**回归**：容器内 320 passed（QMT/镜像/执行器/对账/扫描器/派发 + 新用例）；改动文件
`ruff check` 全绿（仅剩 `internal_strategy_dispatcher.py` 7 处存量 B904）。
trade 服务已重启加载新代码，三条任务启动正常，执行器 `enabled=false` 空转。

**实盘验证（只读）**：`--status` 配置与状态无损；`--enable` → `--status` → `--disable`
往返正常且规则表保留；`mirror:enabled=0`、无 kill switch（安全开关保持关闭）。
