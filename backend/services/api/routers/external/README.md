# 对外 API（`/api/ext/v1`）

给**机器**用的接口：外部节点（Windows 上的交易系统 + 智能体、将来的 MCP server）
通过它读取本平台的数据、投递任务、接收交易信号。

与 `/api/v1`（给人用的 Web 前端）是**两套并存**的东西，不是替代关系：

| | `/api/v1/*` | `/api/ext/v1/*` |
|---|---|---|
| 调用方 | 浏览器 / Electron | 外部节点、智能体 |
| 身份 | 用户 JWT（`sub` = 人） | 长期凭据 → 短期会话令牌 |
| 契约 | 随前端一起改 | **被测试钉死**，改字段要先改测试 |
| 失败方向 | — | 未登记即拒绝（见 `live_trading_gate`） |

服务地址：`api` 服务，容器内 8000 端口。

---

## 1. 配置（部署侧，做一次）

签名密钥 `EXTERNAL_API_SECRET`。**未配置 = 整个对外 API 不可用**，不回落默认值
——C1 事故的成因就是 compose 里 `${INTERNAL_CALL_SECRET:-changeme-internal-secret}`
这种公开回退值被烘进镜像。`changeme` / `secret` / `test` 之类的字面量也一律
按未配置处理。

两条路都能走，**区别只在生效速度**：

| 写在哪 | 生效方式 | 适用 |
|---|---|---|
| `config/runtime.env`（挂载卷） | **热生效**，内核每次调用实时重读 | 推荐 |
| 宿主 `.env` | 需 `docker compose up -d` **重建容器** | 首次部署 |

> ⚠️ **`docker compose restart` 不算数**。Compose 在**渲染配置时**读 `.env`，
> 改完 `.env` 只重启进程的话，容器里还是旧值（甚至仍然为空），而对外 API
> 会继续 503 / 401 —— 排查时很容易反过去怀疑客户端凭据。改 `.env` 之后
> 必须是 `up -d`（重建）。写 `config/runtime.env` 没有这个坑。

生成：`openssl rand -hex 32`。

密钥是**每次调用实时读**的，轮换后免重启生效——但换掉它会**立刻作废所有已签发
令牌**，外部节点会收到 401 并需要重新握手（客户端要按这个语义写重试）。

---

## 2. 签发访问凭据

对外凭据就是平台原有的 `api_keys`（bcrypt 存哈希），**不是另起一套**。
在「设置中心」签发，或直接调：

```http
POST /api/v1/api-keys          # 需要用户 JWT；secret_key 只在创建时返回一次
{ "name": "win-agent-node", "permissions": ["trade.read"] }
→ 201 { "access_key": "ak_...", "secret_key": "sk_...", ... }
```

相关端点（均在 `/api/v1/api-keys` 下）：`GET ""` 列表、`PUT /{access_key}` 改
名称/权限/启停、`DELETE /{access_key}` 吊销、`POST /{access_key}/rotate-secret`
换 secret、`POST /init` 幂等建默认 Key。

`expires_at` 可留给空（= 永不过期）。带有效期的凭据同样受支持——**注意**：
2026-09-23 之前贸易侧有一份判定把 aware 与 naive 的 datetime 直接比较，只要
签发一枚带有效期的凭据，那条路径就抛 `TypeError` 变 500。现在四处判定收敛到了
`backend/shared/api_key_checks.py` 的唯一实现，已修。

**吊销即时生效**：每个请求都查一次库核验凭据，所以停用/吊销一枚凭据后，
它已签发的、尚未过期的令牌**立刻**失效（这是刻意用车库查询换来的性质——
纯自包含令牌在过期前吊销不掉）。

---

## 3. 握手：换会话令牌

```http
POST /api/ext/v1/auth/session          # 匿名可达（它就是去换令牌的）
{ "access_key": "ak_...", "secret_key": "sk_..." }
```

成功：

```json
{
  "token": "qmx1.<b64url(payload)>.<b64url(hmac)>",
  "token_type": "Bearer",
  "expires_at": 1758600000,
  "ttl_seconds": 3600,
  "renew_after": 1758598800,
  "user_id": "1",
  "tenant_id": "default",
  "permissions": ["trade.read"]
}
```

后续请求一律 `Authorization: Bearer <token>`。

* `token_type` 是 RFC 6749 §5.1 的字段名（值 `Bearer`）。
* **按 `renew_after` 续期**（约为 TTL 的 2/3），不要等 401 才换。
* 令牌形如 `qmx1.…`，与用户 JWT 密钥不同、格式不同，**互不认**——
  不要试图把它当用户 JWT 用（或反过来）。

失败：所有认证失败都是**同一个 401**（`detail: external_auth_failed`，
带 `WWW-Authenticate: Bearer`）。凭据不存在 / 密钥不匹配 / 令牌过期 / 签名错，
对外不可区分，且耗时被拉平（不存在的凭据也跑一次 bcrypt）——这是**反枚举**，
不是实现偷懒，客户端不要试图从 detail 里区分原因。

另有两种**不是你的问题**的失败，按状态码区分：

| 状态码 | detail | 含义 | 该做什么 |
|---|---|---|---|
| 401 | `external_auth_failed` | 凭据/令牌不对 | 检查凭据或重新握手 |
| 403 | `real_trading_disabled` | 本部署未开实盘 | 别重试，去问部署方 |
| 429 | `too_many_attempts` | 握手太频繁（带 `Retry-After`） | 退避重试 |
| 503 | `external_api_not_configured` | 服务端**没配密钥** | 别重试，去问部署方 |
| 503 | `auth_backend_unavailable` | 服务端**数据库坏了** | 退避重试，别查自己凭据 |

> 503 那两条是刻意与 401 分开的：把「我们的库挂了」报成「你的凭据不对」
> 会把运维引向客户端，方向反了。

握手有节流：**30 次/5 分钟**（按对端 IP + 凭据指纹）与 **60 次/5 分钟**
（按对端 IP，与凭据无关——挡「换着 access_key 刷」）。正常节点一小时才握一次，
额度绰绰有余。Redis 不可用时**放行**（fail-open）：握手是对外接入的唯一入口，
Redis 抖一下不该让整个面对外停摆。

---

## 4. 先问再做：`/capabilities`

```http
GET /api/ext/v1/capabilities           # 需要 Bearer
```

```json
{
  "api_version": "v1",
  "server_time": 1758596400.123,
  "principal": { "access_key": "ak_...", "user_id": "1", "tenant_id": "default",
                 "permissions": ["trade.read"], "session_expires_at": 1758600000 },
  "trading":  { "real_trading_enabled": false, "note": "本部署未启用实盘…" },
  "planes":   [ { "plane": "control", "description": "策略清单、模型清单（只读）",
                  "transport": "json-rest", "available": true }, … ]
}
```

**新节点接入的第一件事就是调它**：实盘开关是部署级配置，外部节点猜不到。
与其去试 `/orders` 收一个 403，不如在这儿读 `trading.real_trading_enabled`。

* `server_time` 是服务器 Unix 秒（`float`），用于对齐时钟、判断数据新鲜度。
* `planes[].available` 如实反映**当前实现进度**：`false` = 这条路还没通，
  别去试。五个面见下节。
* 响应 schema 是**有类型的具名模型**（`CapabilitiesResponse`），
  字段集合被 `test_external_api_contract.py` 钉死——它是给机器生成客户端用的
  说明书，会随契约变更而不是随实现漂移。

---

## 5. 错误的形状与请求追踪（所有面通用）

失败响应有**两种**形状，对接时都按机器可读的那个字段取：

```jsonc
// 普通 HTTPException（各面的 4xx/5xx 绝大多数）
{ "detail": "invalid_cursor" }

// 闸门拒绝（实盘关闭时的 403）——多一层给人和给日志的信息
{ "detail": "real_trading_disabled", "success": false,
  "message": "本部署未启用实盘交易（ENABLE_REAL_TRADING=false）…",
  "error": { "code": "real_trading_disabled", "message": "real trading is disabled on this deployment" } }
```

**判定逻辑请只读 `detail`**：它在两种形状里是同一个字符串，也是唯一稳定的
机器可读码（`error.code` 与它同值，是给打印日志的人看的）。**不要按 HTTP 状态码
或 message 文本分支**——message 是中文、会改。

每个响应都带 `X-Request-ID`：你传 `X-Request-ID` 就沿用你的，不传就服务端生成
一个 UUID。**报故障时带上它**，服务端日志按它串得起一条请求。

> 两种形状并存是现状，不是设计。统一信封（`{success, data, error}`）是控制面
> 批次的事——那要一次改掉已经发出的所有 4xx，现在做会让批次 2 的契约测试
> 与外部节点同时失效。这里如实写清楚，好过写一个「统一信封」的漂亮话。

---

## 6. 五个面与当前进度

| 面 | 内容 | 传输 | 状态 |
|---|---|---|---|
| control | 策略 / 模型清单（**只读**） | JSON REST | **已实现** |
| task | 训练、回测、因子演化、数据同步、TradingAgents 分析 | `202 + ref` + **轮询** | **已实现** |
| data | QuantDB、特征快照、推理结果、新闻富化 | 游标增量 + Parquet over HTTP | **已实现** |
| stream | 实时行情、情报总线、信号 | WebSocket | **不提供**（理由见 6.4） |
| trading | 模拟盘：账户 / 持仓 / 委托 / 成交 | REST + 幂等键 | **已实现（仅模拟盘）** |

`available=false` 的面一律别去试（现在会 403 或 404）。以 `/capabilities` 的
实时返回为准——这张表是给人看的。

⚠️ **本表曾经写错过。** 它长期写着 task 面走 `202 + task_id` + **SSE**，
而 SSE 一天都没有实现过，最后也没做（理由见 6.3）。**契约文档里一句没兑现的
传输方式，比缺一句更坏**：对接方会照着它去写一个订阅端，然后才发现对面根本
不发事件。改这张表时请把「打算怎么传」和「实际怎么传」分开写。

### 6.1 数据面（`/api/ext/v1/data/*`）

五个端点：可用性索引、分区清单、取分区文件、取单文件数据集、行级增量。
**语义与实测结论写在 [`DESIGN-data-plane.md`](./DESIGN-data-plane.md)**，
这里只放对接方最需要的三件事：

1. **每个响应都有 `as_of`（数据自己的时间），它不是 `server_time`。**
   `as_of` 为空（`null`）表示这个数据集没有数据——**不是 0、也不是现在**。
   把「没有数据」显示成「数据是现在的」是最坏的一种错。
2. **行级增量是「至少一次」。** 同一行可能来两次（边界重叠、重试），客户端必须
   按主键幂等写入。另外：以**早于你手里水位**的时间戳写进来的行（补数据、
   批量重算）增量**永远看不到**，删除也**看不见**——所以响应里有
   `full_sync_recommended_after`，到点做一次全量兜底。
3. **先问再下。** 分区清单里给的 `etag` 与文件端点返回的 `ETag` 是
   **同一个字符串**，带上 `If-None-Match` 就能只拿 304。别按日期猜「变没变」。

### 6.2 控制面（`/api/ext/v1/control/*`）—— **只读**

两个端点：`GET /control/strategies`（`market` / `search` / `include_templates` /
`limit`）、`GET /control/models`（`status` / `limit`）。

它存在的唯一理由是**任务面要填 id**：下发训练要 `model_id`，下发回测要
`strategy_id`，而下发之前那个节点并不知道 id 是什么。没有这个面，机器接口的
第一次调用就得靠人把 id 抄进配置文件——那正是机器接口要消灭的东西。

* **没有写口子**（建/改/删）。理由不是没做，是权限模型还没到位：
  `api_keys.permissions` 现在只发了 `trade.read` / `trade.write` 两个码，
  给控制面开写等于让一枚**按设计只给交易用**的凭据能改策略。
* **没有 `/control/markets`**（「本部署有哪些市场」）。那是第二份事实源：
  数据面已经逐数据集给出 `available` / `as_of` / `freshness`，再挂一个市场
  开关端点，两者会在某次配置变更后互相矛盾，而调用方无从判断信谁。
  要判断某个市场能不能用，去读它的数据集。
* 列表**失败不吞成空数组**：`strategies: []` 在调用方眼里是「你没有策略」，
  而真实情况是查询坏了——503 才是诚实的答案。

### 6.3 任务面（`/api/ext/v1/task/*`）：`202 + ref` + **轮询**

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/task/kinds` | 五类任务的自描述（含各自的参数 schema 与 market 取值） |
| POST | `/task/{kind}` | 投递，**202**，返回 `ref` 与 `pollable` |
| GET | `/task/{kind}/{ref}` | 轮询。`ref` 的形状**由 kind 决定** |

`kind` ∈ `training` / `backtest` / `alpha_evolve` / `trading_agents` / `data_sync`。

**为什么要轮询、为什么当初写的 SSE 没做**：上游五个 kind 是**五个不同的
服务、三种不同的任务模型**（Celery `AsyncResult`、进程内线程 + 进度字典、
直接返回）。要让它们吐出统一的事件流，得在中间再造一层状态总线；而对外节点
本来就是一个「按自己节奏做事」的程序，轮询是它能直接写对的形状。**跨公网
RTT 下 SSE 相对轮询没有优势**——省下的那点延迟被一次重连抵消掉。

状态词表**归一**成六个：`queued` / `running` / `succeeded` / `failed` /
`cancelled` / `unknown`。

* **`unknown` 不是 `failed`。** 上游给了没见过的状态时如实报 `unknown`
  （同时 `upstream_status` 里带原始值），**不许猜**一个终态——
  猜错的代价是调用方要么以为失败去重跑（浪费一次训练），要么以为成功去用
  一个不存在的产物。归一不等于抹平：`upstream_status` 永远保留原值。
* `progress_pct` 为 **null ≠ 0**：`null` = 上游这类任务不报百分比，
  `0` = 确实还没开始动。两者对「要不要继续等」的结论不同。
* **没有 cancel**。五个 kind 有三种取消形状，其中回测的取消键是 Celery
  `task_id` 而它的轮询键是 `backtest_id`、两者之间没有上游映射端点；
  TradingAgents 的取消是个 best-effort 线程、取消完连状态都不回。
  **一个悄悄不生效的 cancel 比没有 cancel 更坏**，所以宁可不提供。
* `data_sync` 是**唯一** `pollable=false` 的 kind：上游那个端点
  **不返回任务 id**（`send_task` 的 `AsyncResult` 被丢弃了），且它要求
  `enabled=true`，而那默认是关的。所以这里返回 `ref: null` + 一句 `note`，
  并把上游的 400 原样透传。**本面不代写那份配置**——那会造出一条绕过前端
  「同步调度」开关的机器路径，与平台的设计（是否启用一律以用户在前端的
  配置为准）直接冲突。要确认「数据到没到」，去看数据面的 `as_of`。

> ⚠️ **已知缺口：任务投递没有独立节流。** 现在只有**握手**有节流（见 §3），
> 投递本身没有。也就是说一枚有效凭据可以连续投递训练/因子演化。
> 上游各自有并发上限，但那是资源保护，不是配额。补齐前请把对外凭据
> 只发给可信节点——**这一点必须让催接口的人也看到**。

### 6.4 为什么没有 stream 面

不是排期问题，是**在这一层还没有答案**：

1. **传输的收益算不过来。** 外部节点跨公网，一次 WS 断线重连的开销
   （握手 + 补历史）常大于这段时间里轮询能拿到的东西；而行情要的是
   **新鲜度**，不是**推送**——`/data` 面每行都带 `as_of`，节点按 `as_of`
   判断新不新，比订阅一个会静默停滞的流更好排查。
2. **收窄维度没定。** 现成的两份流（`market:snapshot` / `market:series`、
   `intel:events`）都是**全市场、全租户**级别的数据，量大且未按租户切分。
   而对外凭据的授权单位是 `api_keys.permissions` 里那几个字符串——
   「这条流该按什么收窄（标的？市场？策略？）」没有一个能从凭据推出来的答案。
   先做推送再补收窄，等于先造一个**所有外部节点都能看到全市场**的通道。

要开这个面，先回答 §2 那个问题（收窄维度是什么），再谈传输。

### 6.5 交易面（`/api/ext/v1/trading/*`）—— **只有模拟盘**

| 方法 | 路径 | 权限码 | 幂等键 |
|---|---|---|---|
| GET | `/trading/sim/account?market=CN` | `trade.read` | — |
| GET | `/trading/sim/orders` | `trade.read` | — |
| GET | `/trading/sim/orders/{order_id}` | `trade.read` | — |
| GET | `/trading/sim/trades` | `trade.read` | — |
| POST | `/trading/sim/orders` | `trade.write` | **必需** |
| POST | `/trading/sim/orders/{order_id}/cancel` | `trade.write` | — |

**实盘不在这一批里，是有意的。** 真实下单链路 `POST /api/v1/orders`
**没有任何幂等键**（实测：重复的 `client_order_id` 撞唯一索引 → `IntegrityError`
→ 500）。把「重试一下就多下一单 / 或者直接 500」的端点交给一个跨公网、按自身
节奏重试的节点，等于交付一个已知的双下单与资金错账面。模拟盘有幂等
（`sim_orders.client_order_id` 的部分唯一索引），实盘没有——所以模拟盘先开。

**幂等键必需**（`Idempotency-Key` 头，或报文里的 `client_order_id`），缺了是
400 `idempotency_key_required`；两处都给且不一致是 400
`idempotency_key_conflict`（放行的话，调用方下次重试可能只带其中一个，
同一笔意图拿到两个键、下出两张单——正是幂等要防的那件事）。

> ⚠️ **判定「是不是重放」请比 `order_id`，不要比状态码。** 同键重放，上游返回
> **已有的那张单**，状态码**同样是 201**。超时/断线后原样重试是安全的，
> 但「201 还是 409」区分不出新旧单。

* `trading_mode` **由服务端钉死**为 `SIMULATION`，且报文模型里根本没有这个
  字段（`extra="forbid"`）——「走对外接口下实盘单」不是「被检查后拒绝」，
  而是**表达不出来**。
* `order_id` 是 **UUID**（`sim_orders.order_id`），不是列表里那个自增 `id`。
  自增 id 是跨租户唯一的实现细节，不对外暴露。
* **对账以 `/trades` 为准**，不要拿委托列表推算：委托是意图，成交才是事实。
* 权限码的来历与边界见 `permissions.py`——**当前只有这个面挂了权限码**。
  `permissions` 为空的凭据在这个面上一律 403（空 = 没有任何被关的权限，
  不是「全部」）。
* `/trading/sim/...` 这一层路径是为了**将来能按前缀关掉实盘侧而不误伤模拟盘**
  （`_BLOCKED_EXT_PREFIXES` 是前缀语义）。见 §7。

---

## 7. 与实盘闸门的关系（重要，且反直觉）

`live_trading_gate` 的默认策略是「新路由默认落在拒绝侧」，但**对外命名空间
自成一域**，用的是「登记过才放行、未登记即拒绝」。

放行侧有**两张表**，按端点形状分工：

* `_ALLOWED_EXT_ENDPOINTS` —— **精确相等**。登记 `/auth/session` **不会**连带
  放行 `/auth/session/anything`；登记 `/data/datasets` 也不会连带放行
  `/data/datasets/{name}/partitions`。
* `_ALLOWED_EXT_PATTERNS` —— 带路径参数的端点（数据面几乎每个都是）。
  正则**逐段写死**：参数位是窄字符类 `[a-z0-9_]+`，其余每段都是字面量，
  `fullmatch` 两端锚死。**不写成整段前缀放行**（`/api/ext/v1/data/`）：
  那正是上面那条精确登记想堵的洞（将来加 `/data/orders` 会被顺带放行）。

已登记的端点（数据面 + 控制面 + 任务面 + **模拟盘**）**在实盘关闭的部署上照样
可用**——`ENABLE_REAL_TRADING=false` 的部署一样能建连、握手、读能力文档、
拉数据、投任务、下**模拟**单。

`_BLOCKED_EXT_PREFIXES` 此刻是**空的**，而且不许填成
`("/api/ext/v1/trading",)`：模拟盘端点长在 `/trading/sim/…` 之下，按前缀拦
会**连带打死模拟盘**——而 OSS 默认 `ENABLE_REAL_TRADING=false`，那是
**每一个** OSS 部署。将来实盘侧对外端点落地时，登记的前缀必须是
`/trading/live`（或它实际用的那一段），且
`test_external_api_gate_coverage.py::test_blocked_prefix_never_covers_simulation`
会先一步拦下写错的版本。

---

## 8. 给改这个目录的人

* **加端点必须去 `live_trading_gate` 的放行表登记一行**（无参数进
  `_ALLOWED_EXT_ENDPOINTS`，带参数进 `_ALLOWED_EXT_PATTERNS`），否则实盘关闭的
  部署上它一律 403。`test_external_api_gate_coverage.py` 会强制：它从 router
  实际枚举路由，逐条拿**具体取值**跑一遍闸门判定。
* **响应模型必须是有类型的具名 `BaseModel`**，不要返回裸 `dict[str, Any]`：
  在 OpenAPI 里那会退化成一团 `additionalProperties`，外部节点生成的客户端
  拿不到任何字段信息，字段改名要等对面运行时报错才发现。
* 鉴权用 `HTTPBearer` 依赖（`Depends(require_external_principal)`），
  **不要用 `Header()`**：后者在 OpenAPI 里 `security` 是 `null`，
  自动生成的客户端会默认不带 `Authorization`。
* 日志与 Redis 键名一律用 `access_key_fp()` 指纹，**不要落明文**——
  `access_key` 是请求体里的自由字符串（可含换行/ANSI），明文进日志等于给
  匿名者一个伪造日志行、污染终端回显的通道。
* 判定「这枚 key 还能用吗」请调 `backend/shared/api_key_checks.py`，
  不要就地再写一遍（这份逻辑曾在仓库里被手写四遍，其中一份漂成了 500）。
* **数据面的路径一律不许自己拼。** 数据集名只能从 `datasets.py` 的注册表查，
  分区名只能过 `normalize_partition`，拼出来的路径必须过 `resolve_under_root`
  （`commonpath`，不是 `startswith`）。
* **JSON 端点里不许读 parquet 内容**（api 服务是单 worker 单事件循环），
  目录枚举走 `run_in_threadpool`，文件传输走 `FileResponse`。
  理由写在 `data.py` 模块 docstring 里。
* **上游状态一律经 `task._STATUS_MAP` 归一，不要在调用点自己判。**
  判不出来就是 `unknown`，并且**把原始值放进 `upstream_status` 一起返回**。
  归一不等于抹平：本仓已经在别处因为「把取不到显示成 0 / 把不认识猜成失败」
  吃过亏，对外契约里这类错误的代价由对面的自动化承担。
* **给任务面加 kind 时，`/task/kinds` 与 `test_external_api_task.py` 要一起改。**
  kind 的参数 schema 是**对外说明书**（`extra="allow"` 透传给上游是有意的，
  训练参数的校验在上游做第二遍，这里不复刻），声明与实现对不上时
  外部节点只会在运行时才发现。
* **`fetch_json(admin=True)` 只许出现在确实打向管理员端点的调用点**，
  且必须是硬编码字面量，不许由请求字段派生。理由写在 `upstream.py`
  模块 docstring 的「例外」一节——那是全仓唯一一处对外提权。

## 9. 测试

```bash
docker exec quantmind python -m pytest backend/tests/ -q \
  -k "external_api or api_key_checks or qmt_agent_auth_expiry or env_example_reachability"
```

| 文件 | 盯住什么 |
|---|---|
| `test_external_api_auth.py` | 令牌往返/防篡改/撤销即失效；失败方向统一 |
| `test_external_api_contract.py` | OpenAPI schema 即对外契约（字段集合钉死） |
| `test_external_api_handshake.py` | 握手端点行为、bcrypt 计时拉平、**面的可用性与路由是否对得上** |
| `test_external_api_throttle.py` | 两层节流桶、TTL 自愈、fail-open |
| `test_external_api_gate_coverage.py` | 新端点有没有登记进闸门放行表；**路径集合的唯一出处** |
| `test_external_api_datasets.py` | 注册表不变量（名字在上游存在、`layout` 相符）、路径越界、etag 与 starlette 逐字一致 |
| `test_external_api_data.py` | 数据面端点行为：304/Range/分页不漏不重/游标原生类型绑定/租户过滤 |
| `test_external_api_task.py` | 状态词表（`unknown` 不许被猜成终态）、进度字段那些坑、市场翻译表、`data_sync` 的 `ref=null` 是诚实的 |
| `test_external_api_permissions.py` | 权限码语义（空列表 = 无权限、精确相等）、**受关端点集合是枚举出来的** |
| `test_external_api_trading.py` | 提权边界（默认不带 admin）、幂等键三态、账户信封两条形状、`data` 缺失必须 502 而不是空账户 |
| `test_api_key_checks.py` | 凭据可用性判定的唯一实现 |
| `test_qmt_agent_auth_expiry.py` | aware/naive 比较那个 500 的回归 |
| `test_env_example_reachability.py` | `.env.example` 里的键 compose 是否真的转发 |
