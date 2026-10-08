---
name: anomaly-engine-ops
description: "识别引擎（T-P6-14 异动/异常检测常驻服务）运维口径 — 四族检测（市场量价/账户/数据/模型）→ 三类动作（总线告警/落表台账/risk lock 否决）；subject 分族规则（只有市场/数据族的 subject 是证券代码）；市场族只在连续竞价时段取数（盘外/假期在报异动是缺陷特征）；「模型 IC 告警没收到」「告警里 symbol 是 *」「台账里某类告警一条都没有」「盘外/假期一堆异动告警」「涨跌停类一条都没报过」的唯一口径。触发词：识别引擎、异动检测、异常告警、模型 IC 骤降、IC 异常、告警没收到、qm_market_anomalies、sentinel_alerts、risk lock、no_targets、告警落表失败、盘外异动、节假日告警、涨跌停没告警"
---

> ## ⚙️ 运行环境契约（先于本文其余内容执行）
>
> 1. **重依赖/容器内命令**：本文的 `docker exec -w /app quantmind python3 -c ...` 与 `psql` 都实测可跑；
>    QuantBot（dsh）没有 backend 包与 redis 库，**读 Redis / 查 PG 必须在 quantmind 容器内**执行。
>    例外：**HTTP 状态端点 dsh 可以直接 curl**（`http://quantmind:8001` + 内部调用头，见 §3）。
> 2. **只读取证**：Redis 只有 scan/hgetall/smembers，PG 只 SELECT。**不要自铸 admin token**；
>    HTTP 状态端点需内部调用头或已登录会话（见 §3）。
> 3. 数据库：`docker exec -i quantmind-db psql -U quantmind -d quantmind -c "<SQL>"`。
> 4. 改 `backend/**` 后必须 `docker restart quantmind`（仓库 backend/ 是 bind mount，不用重建镜像；Python 不热重载）。
> 5. 本文 `~/.claude` 路径仅适用于本地维护者，**QuantBot 不要执行**。

# 识别引擎运维（异动/异常检测，T-P6-14）

`backend/services/engine/anomaly_engine.py`：engine 服务内常驻循环，**门控默认关**，
生产 `qm:engine:anomaly:config.enabled=true`。四族取数 → 纯函数检测（`anomaly_detectors.py`）
→ 每轮对每条 detection 走三类动作（**逐条 try/except，单条失败不连锁**）：

```
市场量价（热集快照+量比） ┐
账户（模拟账户持仓/撤单率）├→ Detection(kind, subject, severity, targets…)
数据（日线跳变/缺口/零成交）│      ├─① 告警：intel 总线（type=anomaly）+ 台账 qm_market_anomalies
模型（model_ic_monitor IC） ┘      ├─② 否决：critical 时写 risk lock（fail-closed）+ risk_events 审计
                                   └─③ 降仓：默认关（reduce_enabled），只记审计建议
```

节奏：主循环 `cadence_s=60`；数据族 `data_every_s=1800`、模型族 `model_every_s=3600`
**低频但首轮必跑**（重启后 ~10s 就会跑一次模型 IC → 想立刻验证重启是最快的办法）。
**市场族只在连续竞价时段取数**（工作日 09:30–11:30 / 13:00–15:00，盘外计 `skipped_market_closed`，
见 §4.2）——盘外调接口试引擎，看不到市场族的任何动静是**对的**。
去重冷却 `qm:anomaly:last_fired:{kind}:{subject}:{severity}`（TTL 1800s）——**冷却窗内的告警不会重复落表/上总线**，
排查「怎么没新行」先看这个键。

## 1. subject 分族规则（本文最重要的一张表）

`Detection.subject` 的语义按 kind 分族；**只有市场/数据族的 subject 是证券代码**，
账户族是 user_id、模型族是 model_id（实测 60~68 字符）。三个落点各自的形状：

| 族 | kind | subject | ① 台账 `instrument` | ① 总线 `targets` | ② risk lock |
|---|---|---|---|---|---|
| 市场 | price_surge / price_limit_up / price_limit_down / volume_surge | 证券代码 | **写** | `[代码]` | 锁该标的持有人 |
| 数据 | data_jump / data_gap / data_zero_volume | 证券代码 | **写** | `[代码]` | 锁该标的持有人 |
| 账户 | account_cancel_ratio / account_concentration | user_id | NULL | `[]` | 账户锁 |
| 模型 | model_ic_drop | model_id | NULL | `[]` | 无（no_targets 审计） |

非 symbol 族的完整 subject 走 **`details.subject`（台账）/ `payload.subject`（总线）**——
两个槽都是 symbol 形状的硬约束（列宽 / 路由键长度），塞不进就把整条告警丢掉，所以宁可空着。
判据单源在 `anomaly_engine.SYMBOL_SUBJECT_KINDS`（白名单）+ `NON_SYMBOL_KIND_PREFIXES`，
新增 kind 必须显式归类，测试 `test_every_engine_kind_is_classified` 会拦住漏网。

**下游**：`sentinel_alert_service` 消费总线 → `sentinel_alerts.symbol = targets[0] 或 "*"`。
所以模型/账户类告警在哨兵表里 **symbol=`*`**（这是对的，不是 bug）。

## 2. 体检（只读，按需取用）

```bash
# ① 引擎活着吗（最近一轮时间 + 计数）——用 Redis 直读，无需鉴权
docker exec -w /app quantmind python3 -c "
import os, json, redis
c = redis.Redis(host=os.getenv('REDIS_HOST') or 'redis', port=int(os.getenv('REDIS_PORT','6379')),
                password=os.getenv('REDIS_PASSWORD') or None, db=0, decode_responses=True)
st = c.hgetall('qm:anomaly:status')
print(st.get('last_build_at'))
print(json.dumps(json.loads(st.get('counters') or '{}'), ensure_ascii=False, indent=1))
"
# ② 台账落表（近 N 分钟）
docker exec -i quantmind-db psql -U quantmind -d quantmind -c \
  "SELECT anomaly_type, instrument, left(details->>'subject',64) AS subject, severity, created_at
   FROM qm_market_anomalies WHERE created_at > NOW() - INTERVAL '30 minutes' ORDER BY created_at DESC;"
# ③ 总线事件（model 事件在 targets=[] 上，subject 在 payload 里）
docker exec -w /app quantmind python3 -c "
import os, json, redis
c = redis.Redis(host=os.getenv('REDIS_HOST') or 'redis', port=int(os.getenv('REDIS_PORT','6379')),
                password=os.getenv('REDIS_PASSWORD') or None, db=0, decode_responses=True)
for eid, f in c.xrevrange('intel:events', count=100):
    ev = json.loads(f.get('data') or '{}')
    ev = ev.get('event') if isinstance(ev.get('event'), dict) else ev
    if ev.get('source') == 'anomaly_engine':
        print(ev.get('level'), (ev.get('payload') or {}).get('kind'), ev.get('targets'),
              (ev.get('payload') or {}).get('subject'))
"
# ④ 冷却键（30 分钟窗；「怎么没新告警」十有八九是它）
docker exec -w /app quantmind python3 -c "
import os, redis
c = redis.Redis(host=os.getenv('REDIS_HOST') or 'redis', port=int(os.getenv('REDIS_PORT','6379')),
                password=os.getenv('REDIS_PASSWORD') or None, db=0, decode_responses=True)
for k in sorted(c.scan_iter(match='qm:anomaly:last_fired:*', count=500)):
    print(c.ttl(k), k)
"
```

`counters` 读法（常用键）：

| 键 | 含义 |
|---|---|
| `detections` / `deduped` | 本轮检出 / 被 30min 冷却压掉 |
| `skipped_market` | 取到的行情条目里**缺 `price`**（不可评测）而被跳过的条数——正常，非故障 |
| `skipped_market_closed` | **盘外闸拦下市场族取数**（见 §4.2）。盘外每分钟 +1（cadence 60s），15:00→次日 09:30 累计 ~1100 属正常；内存计数，重启归零 |
| `errors` + `last_error` | 会把 `deny no_targets`（模型/非持仓标的无持有人，**正常**）也算进去 |

所以 `errors` 上涨 + `last_error=deny no_targets: model_ic_drop:...` 不是故障；`skipped_market_closed`
上涨更不是故障（那是闸在干活）。真故障看日志里 `[anomaly] record:` / `[anomaly] publish:` 的行。

**判「引擎是否在正常干活」的顺序**：先看 `last_build_at` 是不是一分钟内（循环在转）→ 盘中
`skipped_market_closed` 应为 **0** 且在涨；盘外应为 **+1/分钟**、`detections` 不动
——**盘外还出市场族告警就是缺陷复发**（§4.2）。`detections` 盘中也可能长时间为 0：异动本就稀疏。

## 3. 状态端点（带内部调用头；**不要自铸 token**）

`GET /api/v1/engine/realtime/anomaly/status`（engine 服务 :8001，前缀 `/api/v1/engine`）。
裸 curl 返回 **401 `Authentication required`**；加内部调用头即可（**实测 2026-10-08 200**，从 dsh 与
quantmind 容器内都通）：

```bash
# dsh（QuantBot）里：引擎服务在 quantmind:8001。密钥经 stdin 配置传入，**不进 argv**
# （-H "$SECRET" 会把它落到 ps 与 shell 历史里；该头等价 admin，见下）：
printf 'header = "X-Internal-Call: %s"\n' "$INTERNAL_CALL_SECRET" \
  | curl -s --config - http://quantmind:8001/api/v1/engine/realtime/anomaly/status

# 宿主/维护者：在容器内跑，密钥不出容器（$INTERNAL_CALL_SECRET 由容器环境展开，勿打印）
docker exec quantmind sh -lc 'curl -s -H "X-Internal-Call: $INTERNAL_CALL_SECRET" http://127.0.0.1:8001/api/v1/engine/realtime/anomaly/status'
```

> 该头**等价 admin**（`shared/auth.py` 走内部调用分支，角色由客户端自选的 `X-User-Id` 决定），
> 所以只在本机/容器内使用，别写进脚本、别贴进聊天、别自铸 token（见 §3 标题）。

返回 `{"ok":true,"data":{"enabled",…,"counters":{…},"recent_…"}}`——与 §2 的 Redis `qm:anomaly:status`
同源（端点读的就是它），**计数读法见 §2 表**。只读接口，不需要 `X-User-Id`（实测不带也 200）。
裸 curl 的 401 只说明没带头，**不代表端点不存在**；Redis 直读是等价的兜底。

## 4. 已修的坑（2026-10-08，全部带回归）

### 4.1 非 symbol 族 subject 塞进 symbol 形状的槽（同一根因三处落点）

| # | 落点 | 症状（修前实测） | 根因 | 修复 |
|---|---|---|---|---|
| 1 | 台账 `qm_market_anomalies.instrument`（varchar(16)） | 每次模型 IC 告警 `StringDataRightTruncation` → **整行丢失**；表里 2829 行**全是 price_surge**、model_ic_drop 0 行（自引擎上线起），而 risk_events 审计有 979 行 | 判据「非 account_ 即写 subject」，而模型 id 60~68 字符 | `SYMBOL_SUBJECT_KINDS` 白名单 + `details.subject` |
| 2 | 总线 `targets`（契约 `MAX_TARGET_LEN=24`） | `publish_event` 抛 `IntelEventError: target 超长` → **事件一条都没上过总线**（只留 WARNING） | 同上；`targets` 是证券级路由键 | 非 symbol 族发 `[]`（消费端按 `*` 兜底）+ `payload.subject` |
| 3 | 否决路径 | 拿模型 id 调 `_symbol_holders()` 全量扫 `simulation:account:*`，必然空手 | 判据「非账户即标的」 | 只对 symbol 族扫持有人；`_audit` 的 symbol 列 32 字符截断，完整 subject **补进 message** |

**教训（通用）**：告警/台账这类「多维标识」的载荷，别把不同族的标识塞进同一个形状的槽——
列宽与契约长度是硬约束，塞不进就是**静默丢整条**（逐条 try/except 会把异常降成一条 WARNING）。
判据要用**白名单**并留 fail-safe 默认（未知 kind 默认不写），不是黑名单。

**为什么测试没拦住（同族第四个坑）**：`test_anomaly_integration.py` 的夹具模型 id 是
`f"itest-{tag}"`（12 字符，塞得进 varchar(16)），断言「四类必须落表」**假绿**了一整个月。
夹具标识一律取**生产长度**（模型 id 60+ 字符）；同类还有一条：夹具原用真实代码 600036.SH，
`data_jump` 是 critical → deny 会给**持有该代码的真实模拟账户**写标的锁
（TTL 到当日 23:59+4h），每跑一次测试锁掉它们一天买入——夹具必须用虚构代码，
清理必须按**自造 subject 钉死**（别按「近 5 分钟」清场，那会删真实引擎刚落的行）。

回归测试（§4.1 + §4.2 一起）：
```bash
docker exec -w /app quantmind python -m pytest \
  backend/tests/test_anomaly_record_instrument.py \
  backend/tests/test_anomaly_market_session.py \
  backend/tests/test_anomaly_integration.py -q
```

### 4.2 市场族盘外取数：冻结快照被当实时异动报

**症状**：台账里盘外一堆 `price_surge`（半夜 00:00、早上 08:1x、晚上 20:2x、23:5x 都在报），
假期照报。**判据**：市场族告警的 `created_at` 落在连续竞价时段外 ⇒ 缺陷复发。

**根因**：市场族原本**任何时刻**都取数评测，而行情源在盘外仍留有「PreClose 已翻篇、Now 还是上一根」
的冻结快照——拿它评量价异动 = 把昨天的涨跌当今天实时报。实证：国庆假期 10-01~10-06 共 **287 条
`price_surge` 全部落在时段外**（时段内 0 条）；09-25 的 48 条全在 08:11–08:17 盘前。

**修复**：`in_market_session()` 闸——市场族**只在工作日 09:30–11:30 / 13:00–15:00（Asia/Shanghai）取数**，
盘外只计 `skipped_market_closed`（**取数都不取**，不是取到不算）。数据/账户/模型族**不受影响**：
日线跳变、IC 骤降本就该盘后出值。

- 时钟走 `now_fn` 注入点（与节流/冷却同一时钟），测试可注入任意时点；
- 时段常量与 `shared/market_sessions.py` 的 CN 表由 `test_session_windows_match_shared_cn_table` 钉住防漂移
  （平台时段表不止一份历史，改共享表忘改引擎 → 这条红）；
- **不判节假日**（日历在调度器那边，不引入新依赖）：节假日只会白跑取数，不会假报（时段内实测 0 条）。

### 4.3 教训：改「取数入口」要连测试时钟一起改

时段闸一加，**所有把引擎时钟钉在夜间的测试都会假绿/假红**：单测夹具原用 `now_fn=lambda: 1000.0`
（= 08:16 CST，盘外），集成夹具用墙钟（CI 夜里跑就红）。三处夹具一并钉到**当天的 10:30**。
写带时段/日历判定的测试，第一件事就是把时钟变成**注入参数**，别让测试语义随时钟漂移。

## 5. 能力边界（不是 bug，别去改引擎代码）

| 现象 | 真相 |
|---|---|
| `price_limit_up` / `price_limit_down` **一条都没报过** | 桥的字段契约只有 `Now/PreClose/Open/High/Low/Volume/Amount` + 五档（见 `tdx_hot_set_feed._L05_PRICE_MAP`）——**没有涨跌停/封单字段**，写侧如实 None 不假填。引擎读的 `LimitUp/LimitDown` 是**订阅写侧契约**（`tdx_aidata.collector.frame_to_redis` 写的 camel 键，键名大小写敏感）。纯桥源部署下这两类恒 0——数据源能力边界。**别把键名改小写，也别拿昨收自造涨跌停价** |
| `volume_surge` 常年 0 | 量比 = 实时 `now_volume` 对均量基线（`_volume_baselines`，实测有真实值）按 `trading_elapsed_fraction` 折算，要撞 `volume_ratio_min`（生产 3.0）才报；盘外无快照这一条已被 §4.2 闸挡住 |
| 模型/账户族告警的 `instrument` 是空、哨兵表 `symbol='*'` | §1 表，**设计如此** |

## 6. 常见排查

| 症状 | 先查 | 结论 |
|---|---|---|
| 「模型 IC 骤降没收到告警」 | §2 ①②④ | ①冷却键在窗内（30min）；②看日志是否有 `record:`/`publish:` 失败行 |
| 告警里 `symbol` 是 `*` | §1 表 | 模型/账户族**正常**（非证券代码不占 symbol 槽） |
| 台账某类告警**一条都没有** | 该 kind 的 subject 长度 vs 落点形状 | 形状不匹配会静默丢整条：`instrument` varchar(16)、总线 target ≤24 |
| `errors` 计数涨、`last_error=deny no_targets` | — | 正常（无持有人可锁）；只有 `record:`/`publish:` 失败才是故障 |
| 重启后想立刻看模型告警 | — | 模型族首轮必跑（~10s）；冷却键会压掉 30 分钟内的重复，验证时可删该 subject 的键 |
| 盘外/半夜/假期在报市场异动 | 该行 `created_at` 的时段 | 时段外 ⇒ §4.2 缺陷复发（闸没生效/被绕过），先看 `skipped_market_closed` 有没有在涨 |
| 盘外调接口看不到市场族动静 | `skipped_market_closed` | **正常**，闸在干活；盘中再来验 |
| 涨跌停类告警一条都没有 | §5 | 桥源不给涨跌停字段 → 能力边界，不是漏报 |
| 「识别引擎没跑」 | `qm:engine:anomaly:config.enabled` | 门控默认关，生产为 `true`；改配置只动 Redis 键 |
| **计数像刚重启过**（`cycles` 很小、盘外 `skipped_market_closed=0`） | 这台机器上最近有没有人跑过 pytest | 2026-10-08 前，测试用默认 `status_writer` 会把生产镜像 `qm:anomaly:status` 覆盖成测试计数（同日发现实时推理镜像 `qm:realtime:infer:status` 同病），已修：引擎加注入缝 + 测试注入空实现。**再遇到先查测试来源，别急着重启容器或改闸**——真身下一轮会盖回，重启反而清掉现场 |

## 相关技能

- 下游消费：`copilot-advice`（副驾驶上下文里的近 24h 告警）
- 风控锁：`simulation-trading`（账户/标的锁影响模拟撮合）
- 平台运维：`quantmind-operations`
