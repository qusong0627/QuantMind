---
name: qmt-bridge-ops
description: "大 QMT 桥（Windows big-convert RPC）运维——桥在线状态体检（bridge_ok/position_events/RPC 队列）、Redis 容器重启后的离线恢复三步（清 RPC 队列→Windows 侧 QMT 重载策略入口→验证）、RPC 队列卫生工具（订单类硬护栏）、备源行情席（source=qmt_big）席位语义与状态键、真单执行/账户同步链路排查。用户问「QMT 桥」「桥离线」「bridge_ok」「备源席」「备源行情」「RPC 队列/积压」「position_events」「重载策略」「QMT 连不上」时使用。触发词：QMT桥、大QMT桥、bridge_ok、桥离线、备源席、备源行情、qmt_big、RPC队列、积压、position_events、重载策略、BIGQMT_REDIS_DRYRUN"
---

> ## ⚙️ 运行环境契约（最高优先级）
>
> 1. **命令在 quantmind 容器内执行**：`docker exec -w /app/backend -e PYTHONPATH=/app quantmind ...`；
>    桥 Redis = 页面配置 `broker:config:qmt_exec`（trade Redis db2）指向的实例，**现网=本机 redis db5**。
> 2. **桥跑在 Windows 那台装着大 QMT 的机器上**：入口 `BIGQMT_REDIS_DRYRUN.py` **必须在 QMT 策略编辑器里
>    加载运行**（普通 python.exe 跑不注入 passorder/账户上下文，服务端不会起）。
>    **本容器/本机没有重启桥的能力——离线恢复的最后一步必须在那台 Windows 上做。**
> 3. **交易时段严禁重建/重启 Redis 容器**（会断桥且桥不自愈，2026-09-17 实盘事故：09:52 断到收盘）。
> 4. **订单类 RPC 请求永不删除**：任何队列清理只允许删只读/订阅方法（工具已内置硬护栏；
>    发现 `passorder/submit_order/cancel_*` 一律人工核对）。

# qmt-bridge-ops — 大 QMT 桥运维

## 1. 拓扑与键（30 秒）

- Windows：QMT 策略编辑器加载 `deploy/qmt-bridge-kit/BIGQMT_REDIS_DRYRUN.py`（Redis 传输，
  RPC runtime 自带全推行情订阅服务 `QuoteSubscriptionManager`，默认常开）。
- 容器侧：`qmt_exec_client.py`（下单/查询）、`qmt_quote_backup.py`（**备源行情席**）、
  `qmt_account_sync_task`（账户快照）、`qmt_exec_poller`（委托回报轮询）。

| 键（db5） | 含义 |
|---|---|
| `bigqmt:rpc:queue:{account}` | 请求队列（载荷 `b64s:`=base64+数字替换，kit `decode_rpc_request_payload` 解码） |
| `bigqmt:rpc:resp[q]:{account}:{request_id}` | 单次调用响应 |
| `bigqmt:position_events:{account}` | **Stream，桥每 ~3s 推持仓快照**——桥在线性的最灵敏指标 |
| `broker:config:qmt_exec`（trade Redis db2） | 页面配置：账号/桥 Redis 地址（host=redis port=6379 db=5） |
| `qm:qmt:quote:backup:status`（本机 db0） | 备源席状态：`bridge_ok` / `subscribed` / `written` / `skipped_fresh` / `last_error` |
| `qm:qmt:quote:backup:config`（本机 db0） | 备源席开关（`enabled`/`stale_after_s`） |

## 2. 体检（一条命令）

```bash
bash skills/qmt-bridge-ops/scripts/qmt_bridge_status.sh          # 桥/备源/事件流/队列一体报
```

判读：`bridge_ok=False` + `position_events` 末条陈旧 → **桥离线**（走 §3）；
事件流在动但 `subscribed=0` → 备源席未建立（查热集/退避窗口）。

## 3. 离线恢复三步（runbook，手册 §8.1 同源）

```bash
# ① 清积压（桥离线期间各调用方仍入队；恢复瞬间会一次性重放→重复建立服务端订阅引用）
docker exec -w /app/backend -e PYTHONPATH=/app quantmind \
  python scripts/qmt_rpc_queue_hygiene.py --account <资金账号>                 # dry-run 体检
docker exec -w /app/backend -e PYTHONPATH=/app quantmind \
  python scripts/qmt_rpc_queue_hygiene.py --account <资金账号> --trim \
  --drop-methods query_stock_orders,query_stock_asset,subscribe_whole_quote   # 显式白名单才删
# ② Windows：QMT 策略编辑器 → 重新加载运行 BIGQMT_REDIS_DRYRUN.py
# ③ 验证：
docker exec quantmind-redis redis-cli -n 0 hget qm:qmt:quote:backup:status bridge_ok   # → True
docker exec quantmind-redis redis-cli -n 5 xrevrange bigqmt:position_events:<账号> + - COUNT 1
```

备源席自身的退避重试（60s→300s）会在桥回线后自动重订（`test_bridge_recovers_after_outage` 锁定），
**无需重启容器**；恢复后确认 `written` 增长、`market:snapshot:*` 出现 `source=qmt_big`。

## 4. 备源行情席语义（T-P6-02）

- **standby 席位**：仅当标准键缺失或主源（TdxAiData 订阅）**陈旧超过 `stale_after_s`(默认 150s)**
  才写，写入 `source=qmt_big`（避免双源交错抖动）；主源新鲜时计数走 `skipped_fresh`。
  **阈值必须 > 桥热集轮转一圈（529 只 × `TDX_HOTSET_PACING_S` ≈ 95s，实测 ~102s）**——
  设小了（曾为 30s）桥与备源会轮流接管同一批键，持仓监控上表现为来源标签与现价来回跳。
- 量纲：QMT `volume=手`、`amount=元` 原样透传（消费方按 `source` 区分口径）。
- 推送载荷是 **msgpack 二进制**——通道客户端必须 `decode_responses=False`。
- 桥离线时**如实记 `last_error` 并指数退避，绝不假装有数据**。

## 5. 故障排查

| 现象 | 处置 |
|---|---|
| `bridge_ok=false` + `redis rpc timeout: subscribe_whole_quote` | 桥离线（§3）；确认 Windows 机器开机/QMT 登录/策略在跑 |
| 队列持续增长 | 桥没在消费（同 §3）；先 dry-run 体检再决定清 |
| `QMT 执行端未启用（QMT_EXEC_ENABLED=false）` | 这是**控制面开关**，与桥在线性无关——探活用 `position_events`/队列消费，不要用 exec 客户端 |
| 账户快照超时告警 | QMT 登录/桥/Redis 链路；`[QmtSync]` 日志 |
| 委托状态不动 | poller 未跑或 QMT 未推；重启服务端后回捞 |
| 非交易时段下单 | 正常入队，开盘 drainer 补交；页面可「立即补交」 |

## 6. 相关

- 真单镜像/止损执行器/对账：`docs/大QMT真单镜像_部署与上线手册.md`（§8.1=恢复三步）；
- 交易能力 CLI：用户级 `qmt-trader` 技能；备源行情消费侧：`realtime-quotes-tdx`（通达信桥，注意区分——那是另一台 Windows 上的 tqcenter HTTP 桥，本技能讲的是大 QMT 的 Redis RPC 桥）。
