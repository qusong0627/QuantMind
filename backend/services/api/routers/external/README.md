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
  "planes":   [ { "plane": "control", "description": "策略/模型/账户/能力",
                  "transport": "json-rest", "available": false }, … ]
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
| control | 策略 / 模型 / 账户 / 能力 | JSON REST | 规划中 |
| task | 训练、因子演化、回测、数据同步、TradingAgents 分析 | `202 + task_id` + SSE | 规划中 |
| data | QuantDB、特征快照、推理结果、新闻富化 | 游标增量 + Parquet over HTTP | **已实现（本批）** |
| stream | 实时行情、情报总线、信号 | WebSocket | 规划中 |
| trading | 订单 / 持仓 / 风控 | REST + 幂等键 | 规划中 |

`available=false` 的面一律别去试（现在会 403 或 404）。

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

已登记的端点（含整个数据面）**在实盘关闭的部署上照样可用**——数据面不碰交易。
所以 `ENABLE_REAL_TRADING=false` 的部署一样能建连、握手、读能力文档、拉数据。

将来对外交易面端点在 `_BLOCKED_EXT_PREFIXES` 登记后，才会在实盘关闭时返 403
（`detail: real_trading_disabled`）。

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
| `test_api_key_checks.py` | 凭据可用性判定的唯一实现 |
| `test_qmt_agent_auth_expiry.py` | aware/naive 比较那个 500 的回归 |
| `test_env_example_reachability.py` | `.env.example` 里的键 compose 是否真的转发 |
