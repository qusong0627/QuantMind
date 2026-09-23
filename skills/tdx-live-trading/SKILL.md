---
name: tdx-live-trading
description: "TDX 通达信实盘交易 + 模拟/实盘全链路实时监控。覆盖：实时推理(L2)、策略选择、自动买卖(下单/挂单/卖出/撤单)、交易记录、持仓查询、桥健康/链路状态监控。用户说「实时推理」「自动买卖」「实盘下单」「挂单」「撤单」「卖出」「交易记录」「持仓」「实时监控」「链路状态」「TDX」「通达信」「监控交易」「今天交易怎么样」时使用：跑状态快照 → 判定异常 → 按需配置/下单/复核。触发词：实时推理、自动买卖、实盘、挂单、撤单、交易记录、持仓、实时监控、链路状态、TDX、通达信"
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

# tdx-live-trading — TDX 实盘链路 + 模拟/实盘全链路实时监控

QuantMind 交易执行链路的**操作 + 监控手册**：从模型推理信号到通达信真实下单、再到交易记录与持仓的完整闭环，以及供 AI 周期性巡检的一键状态快照。

**定位**：`[[simulation-trading]]` 是模拟盘操作（下单/持仓/账户），本技能是**实盘（通达信桥）操作 + 模拟/实盘全链路监控**——链路任何一环出问题，先跑状态快照定位，再按手册处置。部署/重启见 `[[quantmind-operations]]`。

## ⚠️ 铁律（先读，最高优先级）

| 陷阱 | 正确口径 |
|---|---|
| **股票代码用后缀格式** | 桥下单/持仓/拉委托一律 `600206.SH`（后缀）；`SH600206`（前缀）会 `codestr error` 被拒 |
| **T+1 冻结** | 当日买入 `available_volume=0`，卖出会 `可用持仓不足: 0 < 100` —— 不是 bug |
| **盘后委托=废单** | 收盘后挂单交易所会拒（终态 rejected, filled=0）；对废单撤单返回"撤单提交失败"——正常，无单可撤 |
| **桥是成交权威** | 委托状态以桥 `pull_orders` 为准（30s 同步 UPSERT 到 PG）；本地超时扫描器**已排除**桥委托（remarks 含「通达信桥委托」），不会误标过期 |
| **桥当日委托列表有上限** | 太旧的委托会滚出桥列表→sync 看不到→PG 里费用/状态不再刷新；已成交旧单费用需一次性回填（见 §9） |
| **RedisClient 静默 no-op** | `get/set` 在 client 为 None 时静默返回 —— 独立脚本必须先 `r.connect()`，否则配置"保存成功"实际没写 |
| **L2 分数中性=无生产推理** | 无当天推理时 `load_latest_scores` 为空 → 融合回退中性 50 → 不触发买卖（正确的稳定性行为，不是故障） |
| **A股红涨绿跌** | 前端/报告里红色=涨/买入，绿色=跌/卖出 |
| **桥运行时配置在 Redis** | 真实生效地址一律取 `trade:tdx_config:runtime` 的 `bridge_url`（桥换过机，env 与文档里的字面量都可能是过期 IP —— 本表**不记录具体地址**，避免又一处会腐烂的常量） |
| **实盘包跑源码、不跑 exe** | 仓库里的 `tools/bridge-windows/dist/TDXBridge.exe` 是 `.gitignore` 白名单内的历史产物，早于 BSFlag 买卖方向修复；`deploy/live-win/build_live_pack.sh` 从源码注入 `<包根>/bridge/tdx` |

## 链路架构

```
模型推理(engine) ──> engine_signal_scores (PG)
       │
       ▼
trade 服务 (8002) 两个自动循环
  ├─ L2 实时推理循环  tdx_l2_realtime  (配置 Redis tdx:l2:config, db2)
  │     打分(47标的) → 分数>65买入 / <45卖出 → 桥下单
  └─ 滚动买卖循环     tdx_rolling_trade (配置 tdx:rolling_config:{tenant}:{user})
        融合分>2.2买 / ≤2.2卖 / 上证MA20 只卖不买 → 桥下单
       │
       ▼
Windows 桥 bridge-windows (8550, token 认证)  ←─ /home/zbox/projects/quantmind/tools/bridge-windows
       │  http://127.0.0.1:17709  (TQ 常驻策略, 在通达信客户端内)
       ▼
通达信客户端 TdxW.exe (完整 Windows 客户端 = tdx-datatest) ──> 券商服务器
```

**依赖边界**：桥/客户端只影响 **真实下单、持仓同步、L2 实时行情**；行情日线、推理、信号、回测全走 QuantDB 本地 parquet，**桥掉线不影响**。

## 1. 实时监控（AI 巡检核心）

### 一键状态快照

```bash
# 宿主机执行（容器内跑，输出 ①桥健康 ②L2循环 ③滚动策略 ④今日委托 ⑤实盘持仓 ⑥模拟盘）
docker exec -i -w /app quantmind python - < skills/tdx-live-trading/scripts/tdx_live_status.py
```

### 异常判定表（AI 巡检时对照）

| 快照异常 | 判定 | 处置 |
|---|---|---|
| ① `tdx_connected: false` | 客户端掉线 | 下单/持仓同步/L2 行情全停；日线/推理/回测不受影响；需 Windows 机器人工重启通达信并登录（平台无法远程拉起） |
| ② enabled=True 但全中性 | 今天无生产推理 | 正常，不触发买卖；有推理后自然恢复 |
| ② enabled=False | 自动买卖关闭 | 用户侧关闭；需要时设置页开启 |
| ④ 在途委托收盘后滞留 | 盘后废单未回报 | 盯次日桥回报；连续滞留才告警 |
| ④ 大批 rejected | 价格错误/可用不足 | 检查价格与 T+1 可用；桥拒绝信息会带真实价（`价格错误[600206,0.010(47.27)]`） |
| ④ 累计费用 ¥0 | 旧单未回填 | 见 §9 回填命令 |
| ⑤ 持仓数对不上 | 桥返回清仓残留 | `total_volume=0` 需过滤（已修复，只统计 >0） |
| 桥 HTTP 无响应 | 桥进程挂了 | Windows 看门狗应自愈；查 bridge.log |

**巡检节奏建议**：交易日 9:25–15:05 每 5–10 分钟一次；非交易时段每 30 分钟一次（主要盯 ①桥在线 + ④无异常残留）。

## 2. 实时推理（L2 自动循环）

配置存 Redis db2 `tdx:l2:config`（前端设置页可改，即时生效）：

```json
{"enabled": false, "buy_trigger": 65.0, "sell_trigger": 45.0,
 "interval_sec": 30.0, "cooldown_min": 5.0,
 "daily_weight": 0.6, "signal_weight": 0.4, "pool_size": 12}
```

- 打分 = 日线信号×0.6 + 实时推理信号×0.4 融合（50 为中性）
- 分数 > buy_trigger 买 / < sell_trigger 卖；每只 cooldown 内不重复触发
- 循环状态 Redis `tdx:l2:realtime:status`，每只分数 `tdx:l2:score:{symbol}`
- **开关**：前端设置页，或
  ```bash
  docker exec quantmind python -c "
  import asyncio
  from backend.services.trade_shared.redis_client import get_redis
  async def m():
      r = RedisClient(); r.connect()
      c = r.get('tdx:l2:config') or {}
      c['enabled'] = True
      r.set('tdx:l2:config', c)
  asyncio.run(m())"
  ```

## 3. 策略选择（滚动买卖）

配置 Redis `tdx:rolling_config:default:{user_id}`（user_id 常见 `00000000`/`00000001`）：

```json
{"execute_mode": "off", "score_threshold": 2.2, "fixed_buy_amount": 10000, "auto_place": true}
```

- `execute_mode`: `off`=仅信号提示 / `tdx`=桥实盘 / `paper`=模拟盘（会员门控）
- 规则：融合分 > 阈值 买入；≤ 阈值 卖出；**上证指数跌破 MA20 只卖不买**
- 前端入口：交易 → 滚动买卖设置；API：`GET/POST /api/v1/trade/tdx/rolling-config`、`POST /api/v1/trade/tdx/rolling-signals`

## 4. 下单 / 挂单 / 卖出 / 撤单

桥 API（地址与 token 都取 Redis `trade:tdx_config:runtime` 的 `bridge_url` / `bridge_token`，需 `Authorization: Bearer <token>`）：

| 操作 | 路径 | 要点 |
|---|---|---|
| 下单/挂单 | `POST /api/v1/plans/execute` | 代码后缀格式；限价单=挂单；`order_price` 需在涨跌停内 |
| 撤单 | `POST /api/v1/orders/cancel` | 对已废单会失败（正常）；成功后必须 pull 复核终态 |
| 复核 | `POST /api/v1/orders/query` | 返回当日委托+状态；桥是权威 |
| 持仓 | `POST /api/v1/account/query` | 过滤 total_volume=0 残留 |

桥状态码：`0=REJECTED 1=SUBMITTED 2=PARTIAL_FILL 3=FILLED 4=PARTIAL_CANCELLED 5=CANCELLED`。
桥字符串 → PG：`filled→filled, partial_fill/partially_filled→partially_filled, partial_cancelled/cancelled→cancelled, rejected→rejected`，其余→submitted。

**正确下单姿势（API 层）**：先 `pull_positions` 确认可用 > 0（T+1）→ 下单 → `pull_order_status(wtbh)` 轮询 → 终态后 `pull_today_orders` 复核。

## 5. 交易记录

- 列表：`GET /api/v1/orders/`（OrderResponse，含 commission；前端交易记录页每 5s 轮询）
- 状态统计口径：已成交=filled(+partial) / 委托中=submitted,open,partial / 撤单=cancelled / 拒绝=rejected,expired
- 同步：桥当日委托每 30s UPSERT 进 PG orders 表（remarks=`通达信桥委托`，exchange_order_id 为桥委托号）
- **手续费估算**（桥无费用字段，按标准费率写入 `orders.commission`）：
  `佣金 万2.5（最低 5 元/笔，双边） + 印花税 万5（仅卖出） + 过户费 万0.1（双边）`
  实现：`backend/services/trade/services/tdx_push_service.py::estimate_order_fee`
- **旧单回填**（桥列表滚出后 sync 刷不到时）：
  ```bash
  docker exec quantmind python -c "
  import asyncio
  from sqlalchemy import text
  from backend.shared.database_manager_v2 import get_session
  from backend.services.trade.services.tdx_push_service import estimate_order_fee
  async def m():
      async with get_session() as s:
          rows = (await s.execute(text(\"\"\"SELECT order_id, side, filled_value FROM orders
              WHERE remarks='通达信桥委托' AND filled_value>0\"\"\"))).fetchall()
          for r in rows:
              fee = estimate_order_fee(float(r[2]), str(r[1]).lower())
              await s.execute(text('UPDATE orders SET commission=:c WHERE order_id=:id'),
                              {'c': fee, 'id': r[0]})
          await s.commit()
  asyncio.run(m())"
  ```

## 6. 持仓

- 权威来源：桥 `POST /api/v1/account/query`（或 `pull_positions`）
- PG 侧：`real_account_snapshot_overview_v` 视图（账户面板数据）；订单侧 `orders` 表
- 清仓残留（volume=0）必须过滤；当日买入可用=0 属 T+1 正常
- 仓位显示：持仓数/总仓位来自最近一次成功同步（桥掉线时停留旧值，不报错）

## 7. 运维手册

### 热加载后端改动

```bash
# 找到 trade worker (8002=0x1F42) 并杀掉，看门狗自动 respawn 新代码
docker exec quantmind sh -c "ps aux | grep spawn_main | grep -v grep"  # 看 PID
docker exec quantmind kill <trade-PID>   # 通过 0x1F42 端口归属确认
```

### 桥健康 / 运行时配置

```bash
docker exec quantmind python -c "
from backend.services.trade_shared.redis_client import get_redis
r = RedisClient(); r.connect()
print(r.get('trade:tdx_config:runtime'))   # 真实生效的 bridge_url/token
"
```

### 测试（不实盘也能验链路）

- 单元：`backend/services/tests/test_tdx_push_service.py`（UPSERT/状态映射/费用）、`test_tdx_rolling_trade.py`（桥交互回归，直接测真实方法）、`test_order_timeout_scanner.py`
- 实测（盘后安全做法）：`0.01` 价格下单 → 交易所 `价格错误[code,0.010(现价)]` 拒单 = 链路通；挂有效价限价单 → 盘后变废单（rejected, filled=0）→ 验证撤单接口返回"提交失败"（无单可撤，正常）
- **撤单完整闭环**（撤真在途单）只能在交易时段验证

## 8. 常见问题速查

| 现象 | 原因 | 处理 |
|---|---|---|
| 交易记录全 rejected/expired | 旧版本 sync 只 INSERT 不 UPDATE + 超时扫描误标 | 已修复（UPSERT+排除桥委托）；确认版本含 `ab48fbb2` |
| 持仓数偏大 | 桥清仓残留未过滤 | 已修复（volume>0 过滤）；版本含 `ab48fbb2` |
| 下单报 `codestr error` | 用了前缀格式 | 改用后缀 `600206.SH` |
| 撤单失败 | 单已废/已成交 | pull 复核终态，以桥为准 |
| 配置保存了但没生效 | RedisClient 未 connect | 独立脚本先 `r.connect()` |

## 相关技能

- `[[simulation-trading]]` — 模拟盘下单/持仓/账户（本技能 §6 ⑥）
- `[[quantmind-operations]]` — 部署/重启/日志
- `[[quantmind-deploy]]` — 版本升级
