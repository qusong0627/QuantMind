---
name: tdx-aidata-ops
description: "TdxAiData（通达信 AI 数据 SDK）通道运维与取数速查——状态体检（6 分片/帧节拍/热集/静默片）、订阅推送与请求两通道语义（订阅零请求配额、请求仅 3 次/窗口）、SDK API 方法表、故障模式与证据采集（盘中零数据帧、错误码 10/13、Token Insufficient）、平台 REST 与前端入口。用户问「TdxAiData」「订阅推送」「行情主源」「盘中有没有数据」「零帧」「配额/限流」「错误码10/13」「tdx 自检」「重启 tdx」时使用。触发词：TdxAiData、tdx-aidata、订阅推送、行情主源、数据帧、零帧、配额、Token Insufficient、错误码10、错误码13、自检、重启worker"
---

> ## ⚙️ 运行环境契约（最高优先级）
>
> 1. **所有命令在 quantmind 容器内执行**（SDK 目录 `/opt/tdx-aidata` 由 compose 挂载，宿主同名目录）：
>    `docker exec -w /app quantmind python -c "..."`；时区 CST。
> 2. **绝不直接 import SDK/DLL 写业务脚本**——`backend/shared/tdx_aidata/worker.py` 是**唯一 SDK 引入口**
>    （源守卫测试锁定）。取数一律走 `TdxAiDataClient`/`default_cluster()`（IPC socket），
>    原生命令行实验会污染/争抢 SDK 会话（裸 ctypes 驱动帧计数不可靠且会 segfault）。
> 3. **配额铁律**：请求类方法（K线/分时/分笔/快照）**全接口共享 3 次/冷却窗口**，第 4 次起
>    `错误码 13 Token Insufficient`。**先问用途再消耗**——一次诊断就烧掉 1/3 窗口，
>    别为"看一眼"浪费配额。
> 4. **见数据才报数据**：`frames_meta`（心跳）流动 ≠ 有行情。判"有数据"的唯一口径是
>    `frames_data`/`records`/`written` 增长；消费端另看 `market:snapshot` 新鲜度。

# tdx-aidata-ops — TdxAiData 通道运维

## 1. 架构 30 秒

- **worker 子进程**（`python -m backend.shared.tdx_aidata.worker`，每片一个 IPC socket
  `/tmp/qm-tdx-aidata.sock[.s{i}]`）持有原生 SDK；`client.py` 按需拉起 + 分片集群 `default_cluster()`。
- **分片**：SDK 单次 `subscribe` **≤100 只**（101 只整批拒绝且只打印不抛→静默零帧）；
  热集 >100 时按 `crc32 % N` 切片，分片数在 Redis `qm:market:tdx_aidata:config.shard_count`
  （env `QM_SUB_SHARDS` 兜底）。
- **热集**：`qm:hot_set:symbols` 在**部署本地 Redis**（`shared/hot_set_store.py`，2026-09-17
  从公共行情服迁回）；**行情快照/序列写远端**（`market:snapshot:{sh600036}` 小写 /
  `market:series:{SH600036}` 大写）。
- **预算闸门**：worker 内 3 次/窗口 + 冷却翻倍；超限快速失败（不触 SDK）。

## 2. 体检（一条命令）

```bash
bash skills/tdx-aidata-ops/scripts/tdx_status.sh          # 分片/帧/热集/静默片/本地+远端键
```

或直接（容器内）：

```bash
docker exec -w /app quantmind python -c "
import asyncio
from backend.shared.tdx_aidata.client import default_cluster
async def m():
    s = await default_cluster().subscription_status(timeout=10)
    for x in s['shards']:
        c = x.get('counters') or {}
        print('s%s sub=%s data=%s meta=%s records=%s written=%s err=%s' % (
            (x.get('shard') or {}).get('id'), x.get('subscribed'), c.get('frames_data'),
            c.get('frames_meta'), c.get('records'), c.get('written'), (c.get('last_error') or '')[:60]))
asyncio.run(m())"
```

判读：`data=0` 且 `meta` 在涨 = **只有心跳**（不是我们的问题，见 §4）；`silent` 片 =
超 `QM_HOT_SET_SILENCE_S`(120s) 无任何帧（重订/网络）。

## 3. 两条通道的语义与配额（2026-09-17 实测定案）

| 通道 | 调用 | 配额 | 老实话 |
|---|---|---|---|
| 订阅推送 | `tqs.subscribe`（worker 内） | **零请求配额** | 设计定位=盘中实时主源（五档全字段），**但 2026-09-17 全天实测盘中只推心跳帧、零数据行**——根因待 SDK 方确认，不得当"已验收" |
| 请求接口 | `get_quote/get_klines/get_minute_data/get_tick_data` | **3 次/窗口**（全接口共享） | 盘中实测：`错误码 10 [Invalid server response]`，随后窗口配额即报 `错误码 13`；只可做低频补充 |

错误码：`13`=Token Insufficient（配额）；`10`=服务端异常响应（`Invalid server response`）；
`2`=订阅批量拒绝（>100 只，仅打印不抛）；错误映射见 `shared/tdx_aidata/protocol.py::map_sdk_error`。

## 4. 已知故障模式（故障 ⇒ 处置）

| 现象 | 根因/处置 |
|---|---|
| 6 片 `data=0` 全天，meta 在涨 | **服务端不推数据帧**（2026-09-17 实盘，证据包 `data/p6_tdx_evidence_20260917/`）。排除项：分片/订阅规模/热集漂移（隔离探针同样零帧）。→ 找 SDK 方核对推送语义；不要在本机反复重订 |
| 某片 `subscribed=0` 或整片静默 | 先查该片 `frames_meta` 是否流动；`last_error` 有 `[错误码 2]` 佐证 >100 只；重订前确认 `tqs` 账本已清（worker 已内建每次重订前清 `_sub_codes`） |
| `hot_set read: Timeout connecting to server` | 历史遗留（热集在公共服时远端读超时）；热集本地化后应消失，若再现查本地 `redis` 连通与 `QM_HOT_SET_KEY` |
| `Token Insufficient`（自检/取数失败） | 窗口配额已满（3 次/窗口，全接口共享）——等冷却窗口；**别重试风暴** |
| worker 拉起失败/端口残留 | `client.restart()`（重读 ini/目录）；残留 socket 会被活体探针剔除后重拉（已修） |
| 改 Token/安装目录 | 前端「数据管理→A股→数据源设置」（写 `TdxAiData.ini` 的 `[Token]` 行，原子替换），或 `POST /api/v1/admin/data-platform/tdx-aidata/config` |

## 5. SDK API 速查（`tqServer.tqs.*`，官方接口=此 SDK，无公网 HTTP API）

aids.tdx.com.cn:7727 / aihs.tdx.com.cn:7709 为私有二进制协议（80/443 无响应，**没有 Web API**）；
SDK 内唯一 HTTP 是下载数据包：`www.tdx.com.cn/fastapi/api/quantload/downdata`（落 `data/`）。

| 类别 | 方法 |
|---|---|
| 行情 | `get_market_data`(K线) · `get_minute_data`/`get_today_minute_data`(分时) · `get_tick_data`(分笔) · `get_real_dats` · `get_exday_data`(L2 扩展) · `get_more_info` |
| 静态 | `get_stock_info` · `get_stock_list` · `get_block_from_stock` · `get_trade_calendar` · `get_zdt_data`(涨跌停) · `get_gb_info`(股本) · `get_zzgz_stocklist`(指数成分) · `get_cw_data`(财务) · `get_his_dats`(历史) · `get_kzz_info` · `get_relation` · `get_trackzs_etf_info` |
| 推送 | `subscribe` / `unsubscribe`（回调 `(data, datanum, datatype)` 返回 1 保活） |
| 通用 | `get_tdx_data(json)` · `get_pro_data(request)` |

## 6. 平台 REST 与前端

- `GET/POST /api/v1/admin/data-platform/tdx-aidata/config` —— 配置+worker 状态 / 保存（Token 写 ini）
- `POST .../tdx-aidata/selfcheck` —— 真取一次快照的连通自检（**消耗请求配额**）
- `POST .../tdx-aidata/restart` —— 重启 worker（重读 ini/目录；分片全量重启）
- 前端：「实时数据流 → 数据管理 → A股 → 数据源设置」（配额条+自检按钮）

## 7. 证据采集（报障/验收用）

隔离探针模式（不碰生产分片）：独立 worker（独立 socket + `QM_HOT_SET_KEY` 测试键 +
`QM_L05_ENABLED=0 QM_LATENCY_ENABLED=0`）→ 订阅 2–3 只活跃标 → 抓 `last_frame_preview`
与 counters。参考实现：`data/p6_tdx_evidence.py`（产出 counters/frames_raw/request_probe/
summary.md 证据包）；2026-09-17 案例见 `docs/P6实时轨_实施细案.md` §8。P6 全链验收：
`python backend/scripts/p6_acceptance_report.py`（A 分片 / B 落地率 / C 时延 / D 节拍 /
E L0.5 / F 推理 / G 资源）。
