# 批次 3 设计：对外数据面（`/api/ext/v1/data/*`）

外部节点（Windows 上的交易系统 + 智能体）要把本平台的数据同步到本地。
本文件是**实现前**的契约设计，先定死语义再写码；实现完成后，稳定的部分
并入 `README.md`。

> **实现已落地。** 本文件保留设计时的推理，并把实现推翻了初稿的地方**就地改掉
> 并说明为什么**（不是把初稿悄悄擦掉）——初稿错在哪，比最终长什么样更有用。
> 四处与初稿不同，都用「第一版…」点名了：
> §3.2 `next_since` 的次一日、§3.4 错误分类不再同形、§4.2 游标参数的绑定方式、
> §6 本批不做 429。§9 是原「待调研补全」四项的结论。

数据面的目标不是「把库暴露出去」，而是让外部节点能安全地维持一份
**本地镜像**（边缘 DuckDB / parquet），并在数据变化时增量跟进。

---

## 0. 一句话契约

> 每一份数据要么以**不可变分区文件**的形式给出（可断点续传、可校验），
> 要么以**行级增量游标**的形式给出（至少一次、幂等可重放）。
> **没有**「恰好一次」——HTTP 上不引入应答机制就做不到，不假装能做。

---

## 1. 两类数据，两种传输

| | 分区型（file-backed） | 表型（row-backed） |
|---|---|---|
| 例子 | QuantDB A股行情/因子 parquet、特征快照 | 推理结果、新闻 |
| 写入形态 | 按 (数据集, 日期) 落盘，**整体重写** | 追加/更新行 |
| 增量单位 | **分区**（一天一个文件） | **行** |
| 传输 | Parquet over HTTP + Range + ETag | JSON 行页 + 游标 |
| 变更检测 | 分区 `etag`（size+mtime 派生） | `(updated_at, id)` 元组比较 |
| 幂等性 | 重下一遍无害 | 重放一遍无害 |

**判断依据**：能不能「重写整个分区」——能，就是分区型；只能改其中几行，
就是表型。分区型的天然优势是**可以只下一个文件就得到一致快照**；
表型必须靠游标，且必须接受「可能重复」。

---

## 2. 不可变假设与它的例外

分区型数据**默认不可变**，但这不是事实，是假设——本仓有过真实反例：

* 派生产物会**重算**（复权口径修正后整段重写，看 mtime 才知道）；
* 同步 manifest 的 etag 会**漂移**，导致「看起来变了其实没变」（假重下）。

所以：

1. 每个分区都带 `etag`，由**文件 size + mtime** 派生（不读内容，代价 O(1)）。
   消费者按 etag 比对，不按日期猜。
2. 消费者端**必须**容忍「etag 变了但内容一样」——重下一遍是正确行为，不是浪费。
3. 服务端**不**承诺分区永不重写，只承诺：**重写后 etag 一定变**。

> 由此推出一条硬规则：**游标必须「至少一次」，绝不承诺「恰好一次」。**
> 任何依赖「这条我收过了就不会再来」的消费者写法都是错的，文档里要写死。

**本仓已经有这个坑的真实事故**（`backend/scripts/quantdb_daily_sync.py:192-208`，
2026-09-17）：云侧 manifest 的 `sha256` 字段整列消失（0/2603 条），
旧逻辑 `actual == expected("")` **恒假** → 4 个「全量重写」数据集每轮把 **2600 个分区
全部判为待重下** → 下载风暴打满、celery 超时被杀 → **后续所有数据集集体停更**。
（受影响的正是 `daily_forward / daily_backward / daily_unadjusted / index_daily`
这四个按复权基准整段重算的数据集——与本设计 §2 开头说的「例外」是同一批。）

提炼成两条要写进实现里的规则：

1. **「期望值缺失」必须与「值不匹配」分成两态**。etag/哈希拿不到时（字段没了、
   文件刚被轮转）走**保守重下**，绝不能因为 `None != x` 就判成「变了」并触发无界下载。
2. **判断错误的代价要隔离、要限流**。上面那次是「一个数据集判断错 → 掐死整条流水线」。
   对外数据面的对端我们控制不了，所以分页/限流是必需项，不是优化项。

---

## 3. 分区型：清单 + 按需取文件

### 3.1 可用性索引（先问再做）

```
GET /api/ext/v1/data/datasets
→ { server_time, datasets: [ { name, kind, market, grain, description,
                               available, as_of, freshness,
                               partition_count, first_partition, last_partition,
                               bytes, etag } ] }
```

外部节点接入的第一件事就是调它：**本部署有哪些数据、覆盖到哪天、是不是新鲜**。
`available=false` 的数据集（本机没挂这个市场的盘 / 那张表还是空的）如实报告，
不让对方去试。

* `bytes` / `etag` 只有单文件型给（整份就是一个文件，比对有意义）；
  分区型的是 `null`，逐分区的那一份在 §3.2。
* 文件扫描与查表**并发**执行（`asyncio.gather` + 线程池）：两者互不依赖，
  而查表那侧有一张 68 万行的表。
* 查表失败时表型那几项**如实报 `available=false`，不抛 500**：这是节点接入时
  问的第一件事，库暂时读不到不该让它整个接不上。失败原因进服务端日志——
  `available=false` 同时意味着「没有数据」和「读不到」，这个区分不在字段里。

### 3.2 分区清单（增量判断的锚点）

```
GET /api/ext/v1/data/datasets/{name}/partitions?since=YYYY-MM-DD&until=YYYY-MM-DD&limit=1000
→ { server_time, dataset, as_of, freshness,
    partitions: [ { partition: "2026-09-22", bytes, etag, mtime } ],
    truncated: false, next_since: null }
```

* `as_of` = 该数据集**最新分区**的日期（不是服务器当前时间）——消费者真正关心的是
  「我的镜像新不新」，不是「你几点回我」。注意它是**过滤后**最新的那个。
* `since` / `until` 都是**闭区间**，不传 = 全量清单。清单只需分区元信息，
  不读数据内容，所以可以很大；超过 `limit`（默认 1000，上限 5000）时返回
  `truncated=true` + `next_since`。
* **`next_since` 是最后一个已返回分区的「次一日」，不是它本身。**
  第一版返回的是 `page[-1]`（「下一页从最后一个分区继续」，听起来天经地义），
  但 `since` 是闭区间 → 第二页把边界那天**又发了一遍**。重复比遗漏温和，
  但同样是错，而消费者按幂等语义写入时根本看不出来。加一天在这里是**精确的日期
  算术**（入参出参都是 ISO 日期），不是「下一个交易日」那种需要日历的推算。
* 逐分区的 `stat`（拿 size/mtime/etag）只发生在**分页之后的那一页**上。
  枚举本身只读目录名：`l2_factors` 的历史分区有两千多个，逐分区 `stat` 会把一个
  JSON 端点变成几万次系统调用。
* **不提供**「只回变化的分区」这种服务端状态化接口：服务端不知道消费者手里有什么。
  比对交给消费者（它本来就有自己那份清单）。这比维护 per-consumer 水位更简单也更诚实。

### 3.3 取分区内容

```
GET /api/ext/v1/data/datasets/{name}/partitions/{partition}/file
```

* `Accept-Ranges: bytes`，支持 `Range` → 断点续传；
* `ETag` 用 3.2 里那个 etag，支持 `If-None-Match` → `304`，让消费者**先问再下**；
* `Content-Type: application/vnd.apache.parquet`（拿不到就 `application/octet-stream`）。

**实测（容器内 starlette 1.6.0）**——哪些是白送的、哪些必须自己写：

| 能力 | 谁提供 |
|---|---|
| `Range` → `206` / 不可满足 → `416` / 畸形 → `400` / 多区间 multipart / `HEAD` | **`FileResponse` 自带**（`chunk_size=64KB`、`max_ranges=100`、`accept-ranges: bytes` 默认开） |
| `ETag` 生成 | 自带，但算法是 `md5(f"{st_mtime}-{st_size}")` ——**恰好等于本设计的 etag 定义**，所以直接用它的值，别再造一个 |
| `If-Range` | 自带，但是**裸字符串相等**（不解析日期、不分强弱 ETag）；客户端要断点续传就得原样回传我们给的 ETag |
| **`If-None-Match` / `304`** | **完全没有** —— starlette 里搜不到这个头，FileResponse 没有任何 304 分支。**必须端点自己实现**：比对请求头与自算 etag，命中就 `Response(304)` |

另外三处细节：

1. 传 `stat_result=` 可以省掉 FileResponse 内部那次 `os.stat`（也避开「stat 完文件被删」的竞态）。
2. `If-None-Match` 那条路径**不要**依赖 `FileResponse`，要在进它之前就短路掉。
3. `FileResponse` 的 ETag 是**带引号**的强校验器（`'"abc…"'`）。清单端点里那个
   `etag_of()` 是**逐字复刻**它（连引号都保留、连 `str(float)` 的写法都照抄），
   不是「按公式另算一个」——两者必须逐字节相同。
   `test_external_api_datasets.py::test_etag_matches_starlette_file_response_exactly`
   走一次**真实请求**比对线路上发出的头来钉这件事（构造完的 `FileResponse`
   还没设 stat 头——那是在 `__call__` 里做的，直接读 `response.headers` 会 KeyError）。
4. `_not_modified` 做的是 RFC 7232 的**弱比较**：拆逗号列表、认 `*`、剥 `W/` 前缀。
   只做字符串相等的话，一个标准客户端发 `W/"abc"` 就会被判成「变过了」，
   然后每轮全量重下——而这没有任何报错，只是慢。

### 3.4 路径安全（这是本面最大的风险点）

`{name}` 与 `{partition}` 都是**用户输入**，会拼进文件路径或 SQL 表名。四层叠着来：

1. `{name}` **只能**从数据集注册表里查（字典白名单），查不到即 404 `dataset_not_found`
   —— 绝不拼接到路径上。`..` 这类名字在这里根本不是一条分支：查表查不到。
2. `{partition}` 必须匹配 `^\d{4}-\d{2}-\d{2}$`**并且**过 `date.fromisoformat`。
   两层都要：正则挡形状，`fromisoformat` 挡 `2026-13-45` 这种形状对、值越界的。
   **不做到值级的容错**（`2026-9-2` 一律拒）——容错就是猜，猜错就是读错分区。
3. 拼出的路径经 `os.path.realpath` 后**必须**仍在该数据集根之下（`os.path.commonpath`
   判定，不是 `startswith` —— 后者会被 `root=/a/b` vs `/a/bc/x` 绕过）。
4. **闸门那一层在最前面**：`live_trading_gate._ALLOWED_EXT_PATTERNS` 按**逐段写死**
   的正则（参数位用 `[a-z0-9_]+`）放行，`fullmatch` 两端锚死。形状不对的路径
   **根本走不到 handler**（403，实盘关闭的部署上）。

四条里任何一条单独都能挡住，但四条一起才是纵深——本仓的教训是
「一份清单手抄八遍、八遍都漏了一项」，所以这里从第一天就只写一份。

**「路径非法」与「分区不存在」不对外同形**（这里推翻了设计初稿）：

| 情形 | 码 | detail | 为什么 |
|---|---|---|---|
| 分区名不合格式 | 400 | `invalid_partition` | 规则是**公开且无状态**的，不构成探测 oracle；报 404 会让调用方去查「是不是这天没数据」 |
| 格式对、分区不存在 | 404 | `partition_not_found` | 这是数据问题 |
| 数据集在注册表里、本机没挂盘 | 503 | `dataset_unavailable` | 这是**服务端**状态；报 404 会让运维去查路由注册，方向反了 |

初稿写的是「404 与路径非法同形，不给探测 oracle」。实现时发现那条
推理只在「非法路径能透露出目录结构」时成立——而这里路径**压根没被拼出来**
（第 1、2 层就拦掉了），4xx 只说明「你请求里那个字符串不对」。
把三种情形压成同一个码的代价是真实的：对接方无法区分「改请求」和「等数据」。

---

## 4. 表型：行级增量游标

```
GET /api/ext/v1/data/{dataset}/changes?cursor=<opaque>&limit=500
→ { server_time, dataset, as_of, freshness,
    items: [ ... ],
    next_cursor: "qmc1....", has_more: false,
    full_sync_recommended_after: "2026-10-23T00:00:00Z" }
```

### 4.1 游标的形状

`qmc1.<base64url(json)>` —— 带版本前缀（`qmc1`），载荷自描述：

```json
{ "d": "news_enrichment", "t": "2026-09-23T10:00:00.123456Z", "k": ["918273"], "v": 1 }
```

* `d` = 数据集名。**用在别的数据集上直接 400**——静默接受等于让消费者拿 A 的水位
  去读 B，结果是「少了几天数据但没有任何报错」。
* `v` = 游标格式版本。改格式时旧游标一律 400 并提示全量重来，不猜。
* `t` 是**带时区的 ISO-8601 字符串**（微秒逐位无损），`k` 是兜底键的**字符串**列表。
  **载荷里没有数字**：JSON 的 double 存不下 64 位整数键（`huntly_page_id` 是 bigint），
  跨语言也不保证 round-trip。字符串在线上无损，还原成原生的活干在绑定前（见 §4.2）。
* **不签名**：游标不是安全边界（它是消费者自己提供、自己承担的定位信息），
  伪造游标最多让自己拿到错的数据，拿不到权限外的数据。给它签名只会让人以为它是凭证。

### 4.2 排序与比较（最容易出错的地方，其中一条是实测出来的）

* 排序键 = `(游标列, 兜底键…)`，**元组序**，且查询与比较必须用**同一个元组**：
  `WHERE (enriched_at, huntly_page_id) > (:t, :k0) ORDER BY enriched_at, huntly_page_id`。
  只按时间戳翻页、不用兜底键，会在**同一时间戳有多行**时漏行（一次 upsert 写几千行
  落在同一个 `NOW()` 上是常态）。
* 边界**严格大于** `>`，不是 `>=`：`>=` 会让消费者卡在同一行上无限循环。
* **首页不带比较谓词**，而不是「给一个足够早的哨兵值」。第一版用的是哨兵
  （text 键给 `""`），那对文本键**必然有漏洞**：排序上小于任何非空串的那个值
  就是空串本身，于是一行键为空串的记录从第一页起就永远被跳过，且没有任何报错。
* **`CAST` 救不了字符串参数**（这条是实测出来的，不是推理）：

  ```sql
  -- 第一版：参数直接绑线上那串字符串，指望数据库转
  WHERE (updated_at, run_id) > (CAST(:t AS timestamptz), :k0)
  ```

  asyncpg 从预备语句拿到参数 OID 后**在 SQL 层之下**就拒了，`CAST` 根本轮不到：

  ```
  invalid input for query argument $1: '1970-01-01T00:00:00Z'
  (expected a datetime.date or datetime.datetime instance, got 'str')
  ```

  所以注册表里每个兜底键列都带一个**还原函数**（`_KEY_CASTS`：`text→str`、
  `bigint→int`、`timestamptz→aware datetime`…），绑定前把游标里的字符串还原成
  原生 Python 值，SQL 里**一个 `CAST` 都不留**。留一个就会让后来的人以为
  「反正数据库会转」。类型声明错了（拿 `text` 声明一个 bigint 列）在这里是
  **当场报错**，不是一个静默的错位置——这正是要的失败方向。
* 游标列是 NULL 的行（`engine_feature_runs.updated_at` 在 schema 上可空）
  **不可能被任何元组比较选中**，它们对游标接口永久不可见。这个前提**显式写进 SQL**
  （`cursor_column IS NOT NULL`），不靠三值逻辑的副产品——写出来它才是一个
  能被读到、能被质疑的事实。

### 4.3 三条必须写进文档的诚实声明

1. **回填行可能永久不可见**。若某行以**早于已发出游标**的时间戳写入
   （补数据、时钟回拨、批量重算），它落在游标之前，增量永远看不到它。
   这不是 bug 是这类游标的固有性质。对策：`full_sync_recommended_after`
   告诉消费者「最迟什么时候应该全量重来一次」（每次响应现算 = 服务器当前时间
   + 30 天，**不是写死的常量**——消费者可能几个月才来一次，一个写死的日期
   到那时早就过期了，而它看起来仍然像个正常值）。
2. **删除不可见**。游标只报「有/变了」，不报「没了」。需要处理删除的消费者
   靠自己那份清单与服务端清单做差集（分区型已经天然支持这一点，表型需全量兜底）。
3. **至少一次**。消费者必须幂等（按主键 upsert），不能假设不重复。

### 4.3.1 整行外发与「像凭据的列」

响应是**整行原样返回**（镜像语义：消费者要的就是那一行，而不是我们替它挑的几列）。
代价是**将来新增的列会自动外发**。一层网兜住最坏的那种：列名命中
`password|secret|token|credential|private_key|api_key` 的列**从载荷里剔除**，
并打一条 WARNING——静默丢弃比泄漏好，但不该静默：那是一行需要人来看的代码错误。

写出去还要能是**合法 JSON**（这层是前面漏了会直接炸客户端的）：

| 值 | 出去是什么 | 为什么 |
|---|---|---|
| `datetime` | ISO-8601 UTC 带 `Z` | 全仓唯一口径（`utc_datetime`），不是 naive 本地时间、不是 epoch 数字 |
| `date` | `YYYY-MM-DD` | **必须在 `datetime` 之后判**——`datetime` 是 `date` 的子类，顺序反了会把时间戳当日期 |
| `NaN` / `±Inf` | `null` | JSON 里根本没有 NaN 的表达，原样写出去就是一份**非法 JSON**，客户端解析直接失败。`news_article_enrichment` 的 sentiment 列是 `real`，能存 NaN |
| `Decimal` / `UUID` / `bytes` | float / str / base64 | 依次兜底 |

### 4.4 `as_of` 的语义

表型的 `as_of` = 该数据集**最新一行的 `updated_at`**，不是服务器时间。
`server_time` 单独给。两者混用会让消费者把「你回得很快」当成「数据很新」。

---

## 5. 每个响应都带的三件套

| 字段 | 含义 | 唯一出处 |
|---|---|---|
| `server_time` | 服务器 Unix 秒（float） | 各端点直接取 |
| `as_of` | **数据自己**的时间（最新分区日期 / 最新行时间戳） | 数据集定义 |
| `freshness` | `fresh` / `stale` / `unavailable` | `backend/shared/freshness.py`（唯一谓词，不在本面重写） |

**复用方式要说清**（`backend/shared/freshness.py:46,68,108`）：分级逻辑是纯函数
（`classify_age` / `FreshnessPolicy`），但阈值入口 `quote_policy()` 读的是
**行情口径**的 env（`QM_QUOTE_FRESH_WITHIN_S` 默认 60s / `..._STALE_WITHIN_S` 默认 300s）。
日频数据集一天一个分区，套 60s/300s 会永远显示 `stale`，那不是「不新鲜」是**口径错误**。
所以：**用同一套谓词，自建 policy**（`FreshnessPolicy(fresh_within_s=…, stale_within_s=…)`，
按数据集的产出节奏定，如日频用「N 天」量级）。**不要**为数据面新造一组
`QM_*_FRESH_*` env —— `test_freshness_latency.py` 是**源守卫测试**，
专门禁止阈值 env 再次散落（`freshness.py` 模块 docstring 明说历史散点已收敛、防回潮）。

`as_of` 缺失时（空数据集）为 `null`，**不是** `0` 也不是当前时间——
把「没有数据」显示成「数据是现在的」是最坏的一种错。

---

## 6. 错误分类

| 状态码 | detail | 含义 | 客户端该做什么 |
|---|---|---|---|
| 400 | `invalid_cursor` | 游标格式错/版本不认/不属于该数据集 | **丢弃游标，全量重来**（不是修请求） |
| 400 | `invalid_partition` / `invalid_since` / `invalid_until` | 日期写法不合格式 | 修请求 |
| 404 | `dataset_not_found` | 名字不在注册表里 | 先查 `/datasets` |
| 404 | `partition_not_found` | 格式对但那天没有文件 | 正常（非交易日），跳过 |
| 503 | `dataset_unavailable` | 数据集存在但本机没有数据 / 读库失败 | 别重试，走 `/datasets` 看 `available` |

**401 只用于身份问题**（沿用批次 2 的既有语义），**403 只用于 `ENABLE_REAL_TRADING`**
——数据面不因实盘开关关闭而不可用（它不碰交易），这一点由
`live_trading_gate` 的**逐端点登记**保证：不带参数的进 `_ALLOWED_EXT_ENDPOINTS`
（精确相等），带参数的进 `_ALLOWED_EXT_PATTERNS`（`fullmatch`）。两张表都是
**放行侧**，没登记一律拒绝。测试
（`test_external_api_data.py::test_data_plane_is_not_blocked_by_the_real_trading_gate`）
断言的是「不是 403」而不是「是 200」——闸门放不放行与端点答什么，是两件事。

**本批不做 429**（读面限流）。设计初稿列了它，实现时判定：数据面的对端是我们
自己部署的节点，不是匿名流量；真正需要限流的是**匿名可达**的握手端点（那里有
两层固定窗口计数，见 `router.py`）。给已鉴权的读面加限流需要先有 per-键的配额语义，
而那属于控制面（批次 4）。这里如实记为「没做」，不假装有。

**读库失败报 503 而不是 400/404**：那是**服务端**的问题，报 4xx 会把对接方
引去查自己的请求，方向反了（`test_read_failure_is_503_not_400`）。

> 404 与 503 分开是刻意的：「没有这个数据集」和「有这个数据集但这台机器上没有数据」
> 是两件事，混在一起会让运维去查错方向。

---

## 7. 不做的事（YAGNI）

* **不做服务端 per-consumer 水位**：消费者自带清单/游标，服务端无状态。
  代价是清单可能较大，收益是没有「服务端记错了消费者进度」这类不可调试的故障。
* **不做 WebSocket 变更推送**：那是流面（批次 4）。数据面只做拉取。
* **不做服务端 DuckDB 查询接口**（把 SQL 传上来跑）：这是任意代码执行面，
  且与本面的定位（同步镜像）无关。边缘要查询，把数据拉下去用本地 DuckDB 查。
* **不做「恰好一次」**：见 §0。

---

## 8. 运行形态约束（实测）

`backend/main_oss.py:123` —— all 模式下 **api 服务是单 worker 单事件循环**
（`API_WORKERS` 可覆盖，但默认 1，且注释说明多 worker 会踩嵌套 spawn）。
这决定了三件事：

1. **JSON 端点里禁止读 parquet 内容。** 一个事件循环被一次大文件读取占住，
   全站（含用户前端、含实盘相关只读端点）一起卡。清单/可用性索引只允许
   `os.stat` 级操作——这也是 §3.2「清单不读内容」的真实理由，不是优化洁癖。
2. **文件传输必须走异步流式**（`FileResponse` 在 anyio 线程池里分块读），
   绝不在 handler 里 `open().read()`。一条慢链路下载不能冻住整个网关。
3. **内存缓存只能当纯 memoization 用**。单进程下它是安全的，但不能把
   **正确性**建立在「别的请求写过这份缓存」上——谁哪天把 `API_WORKERS` 调成 4，
   缓存就分片，而那种错是间歇性的、极难查。缓存只允许「命中就快、不命中就对」。

落到实现上的具体形态：目录枚举（`scandir`/`stat`）与逐分区 `stat` 全部走
`run_in_threadpool`；文件传输走 `FileResponse`；JSON 端点里没有任何一次
读 parquet 内容的调用。三件事都在模块 docstring 里写明了理由。

---

## 9. 调研结论（原「待补全」四项）

- [x] **数据集注册表放在哪个模块** → `backend/services/api/routers/external/datasets.py`，
      三类（`PARTITION_DATASETS` / `BLOB_DATASETS` / `ROW_DATASETS`）共 19 条。
      **路径字段不在那里定义**：`rel_dir` 一律从上游 `backend/shared/quantdb_datasets.py`
      取（那是既有的名字→路径事实源，27 条含 `layout`），本模块只登记
      **对外暴露的子集**。测试钉住「登记的名字在上游存在且 `layout` 相符」。
      本仓在这件事上吃过的亏是「一份清单手抄八遍、八遍都漏了一项」。
- [x] **各数据集的真实根目录与分区布局** → `quantdb_paths.resolve_quantdb_dir()`
      是唯一事实源，分区布局是 `{rel_dir}/dt=YYYYMMDD/data.parquet`。
      **上游声明本身也可能错**：`tick_data` 声明 `layout="partition"`，盘面实际是
      平铺的 `{SYMBOL}_{YYYYMMDD}.parquet`（一天几千个文件）。按声明取会**静默查空**，
      所以这一版不收它。因此测试分两层：声明层对齐（自动）+ 盘面核对（人工，
      `ls` 确认）。
- [x] **表型数据集的真实列名与可用游标列**（实测，见表格）。四条共同的踩坑点：
      游标列必须是**每次写入都会刷新**的列；兜底键必须是**每行不变**的列；
      `engine_feature_runs.updated_at` 在 schema 上**可空**（当前 0 行 NULL，
      但那是数据现状不是约束）；有租户列的表必须按凭据过滤。

      | name | 表 | 游标列 | 兜底键 | 租户过滤 | 现状 |
      |---|---|---|---|---|---|
      | `model_inference_runs` | `qm_model_inference_runs` | `updated_at` | `run_id` (text) | ✅ | — |
      | `model_inference_batches` | `qm_model_inference_batches` | `updated_at` | `batch_id` (text) | ✅ | — |
      | `feature_runs` | `engine_feature_runs` | `updated_at` | `run_id` (text) | ✅ | 游标列 schema 可空 |
      | `news_enrichment` | `news_article_enrichment` | `enriched_at` | `huntly_page_id` (bigint) | ❌ 表里没有租户列 | 68.9 万行 |

      **刻意排除 `engine_signal_scores`**（全仓最有价值的表之一）：它没有可用的
      游标列——`created_at` 覆盖全部行却不随 upsert 变化，`signal_ts` 只有 0.6% 的
      行有值。任何时间戳游标都会**漏掉更新**且不报错。要么先补列（1400 万行），要么不做。
- [x] **Range/ETag 在容器内 starlette 版本上的实测行为** → 见 §3.3 的表
      （starlette 1.6.0）。关键结论：Range/206/416/多区间/HEAD 全白送；
      `If-None-Match`/304 **完全没有**，必须端点自己写。

### 9.1 索引：读出来是 Seq Scan，需要补

实测（`news_article_enrichment`，68.9 万行）：

| 查询 | 计划 | 耗时 |
|---|---|---|
| `MAX(enriched_at)` | `Seq Scan` | 55–56 ms |
| 首页翻页 | `Seq Scan + Sort` | 76–151 ms |

四个表都是这样。按 `MAX_ROW_PAGE=5000` 算，`news_enrichment` 全量首镜像 ≈ 1379 页
——每页都全表扫一遍。索引在 `data/upgrade_v1.1.2.sql`（启动时自动应用，
`main_oss._ensure_upgrade_scripts` 会 glob `upgrade_*.sql`）：

```
news_article_enrichment (enriched_at, huntly_page_id)
qm_model_inference_runs  (tenant_id, user_id, updated_at, run_id)
qm_model_inference_batches (tenant_id, user_id, updated_at, batch_id)
engine_feature_runs      (updated_at, run_id)
```

租户表的列序是**等值列在前、游标列在后**：这样 `WHERE tenant_id=? AND user_id=?`
之后剩下的后缀本身就已经是游标序，**不需要再排序**。

> ⚠️ **索引加完之后的计划与耗时没有实测**（本地是共享库，建索引属于变更共享资源，
> 没有在无人监督的情况下执行）。上表里「加索引前」的数字是实测的；索引文件已就位，
> 下次部署重启时会生效。要验证的话：`EXPLAIN ANALYZE` 上面两条查询，
> 期望看到 `Index Only Scan` 且耗时降到个位毫秒。
