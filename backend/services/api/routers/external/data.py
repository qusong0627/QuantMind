"""对外数据面（`/api/ext/v1/data/*`）。

这一面要解决的问题不是「把库暴露出去」，而是让外部节点（Windows 上的交易
系统 + 智能体）能安全地维持一份**本地镜像**，并在数据变化时增量跟进。

两类传输
--------
* **按分区取文件**：`dt=YYYYMMDD/data.parquet` 一个交易日一个文件，
  支持 `Range` 断点续传与 `ETag`/`304` 先问再下（清单里给的 etag 与文件端点
  返回的 ETag 是**同一个字符串**，见 `datasets.etag_of`）。
* **按游标取行**：数据库表按 `(updated_at, 兜底键)` 元组增量翻页。
  **至少一次**，绝不承诺「恰好一次」——HTTP 上不引入应答机制就做不到，
  不假装能做（客户端必须幂等）。

三条必须写进对接文档的诚实声明（设计文档 §4.3）
------------------------------------------------
1. **以早于已发出水位的时间戳写入的行，增量永远看不到**（补数据、时钟回拨、
   批量重算）。这是这类游标的固有性质，不是缺陷。对策是响应里的
   `full_sync_recommended_after`：消费者定期全量兜底一次。
2. **删除不可见**。游标只报「有/变了」，不报「没了」。
3. **会重复**。同一行可能被发出两次（边界重叠、重试）。

运行形态约束（决定了本模块的写法）
----------------------------------
`main_oss.py` 的 all 模式下 **api 服务是单 worker 单事件循环**。所以：

* JSON 端点里**不读 parquet 内容**——一次大文件读取会把全站（含用户前端、
  含实盘只读端点）一起卡住。清单只做目录枚举，不做「顺手读一下行数」。
* 目录枚举这类阻塞系统调用走 `run_in_threadpool`，不占事件循环。
* 文件传输一律交给 `FileResponse`（内部按 64KB 分块、在线程池里读），
  绝不在 handler 里 `open().read()`。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import text as sql_text
from starlette.concurrency import run_in_threadpool

from backend.services.api.routers.external.auth import (
    ExternalPrincipal,
    require_external_principal,
)
from backend.services.api.routers.external.cursor import (
    InvalidCursor,
    decode_cursor,
    encode_cursor,
)
from backend.services.api.routers.external.datasets import (
    BLOB_DATASETS,
    FULL_SYNC_RECOMMENDED_AFTER_DAYS,
    PARTITION_DATASETS,
    ROW_DATASETS,
    RowDataset,
    blob_file,
    dataset_dir,
    etag_of,
    list_partitions,
    normalize_partition,
    partition_date_ts,
    partition_file,
    stat_or_none,
)
from backend.shared.freshness import Level
from backend.shared.utc_datetime import UTC, to_utc_iso

logger = logging.getLogger(__name__)

router = APIRouter(tags=["External Data"])

#: parquet 的媒体类型。取不到时退 `application/octet-stream`。
PARQUET_MEDIA_TYPE = "application/vnd.apache.parquet"

#: 清单分页上限。分区清单的每一项要 `stat` 一个文件，所以它必须能分页——
#: `l2_factors` 有两千多个分区，一次全列出来就是两千多次系统调用。
MAX_PARTITION_PAGE = 5000
DEFAULT_PARTITION_PAGE = 1000

#: 行页上限。**有界**是刻意的：`news_enrichment` 有 68 万行，一次全量会把
#: 单 worker 的事件循环和内存一起压住；消费者本来就该按页推进游标。
MAX_ROW_PAGE = 5000
DEFAULT_ROW_PAGE = 500

#: 兜底键/游标里的组件在日志与错误信息里的显示上限。
_MAX_ECHO = 64

#: 看起来像凭据的列名。`SELECT *` 是镜像语义想要的（消费者要的就是整行），
#: 但代价是**将来新增的列会自动外发**。这层网兜住最坏的那种：
#: 哪天有人往这些表里加了 `api_token` 之类的列，它不会被静默发出去。
_SENSITIVE_COLUMN_RE = re.compile(
    r"password|passwd|secret|token|credential|private_key|api_key", re.IGNORECASE
)


# ---------------------------------------------------------------------------
# 响应模型
# ---------------------------------------------------------------------------
#
# 全部是**有类型的具名模型**：对外接口的 schema 就是给机器生成客户端用的
# 说明书，裸 `dict[str, Any]` 在 OpenAPI 里会退化成一团 `additionalProperties`
# ——字段改名要等对面运行时报错才发现（见 README「给改这个目录的人」）。


class DatasetEntry(BaseModel):
    """可用性索引里的一项。这是外部节点接入后问的第一件事。"""

    name: str
    kind: Literal["partition", "blob", "row"]
    market: str
    grain: str | None = Field(None, description="分区型数据集的粒度，如 1d")
    description: str
    available: bool = Field(
        ..., description="False = 本部署没有这份数据（未挂盘/未产出），别去试"
    )
    as_of: str | None = Field(
        None,
        description=(
            "**数据自己**的时间（最新分区日 / 最新行时间戳），ISO-8601 UTC。"
            "不是服务器时间——那是 server_time。空数据集为 null，不是 0、也不是现在。"
        ),
    )
    freshness: Level
    partition_count: int | None = None
    first_partition: str | None = None
    last_partition: str | None = None
    bytes: int | None = None
    etag: str | None = None


class DatasetsResponse(BaseModel):
    server_time: float = Field(..., description="服务器 Unix 秒")
    datasets: list[DatasetEntry]


class PartitionEntry(BaseModel):
    partition: str = Field(..., description="分区日期 YYYY-MM-DD")
    bytes: int
    etag: str = Field(
        ...,
        description=(
            "与文件端点返回的 ETag **逐字相同**。带上 `If-None-Match` 可以先问再下。"
        ),
    )
    mtime: str = Field(..., description="文件修改时间（ISO-8601 UTC）")


class PartitionsResponse(BaseModel):
    server_time: float
    dataset: str
    as_of: str | None
    freshness: Level
    partitions: list[PartitionEntry]
    truncated: bool = Field(
        False, description="True = 还有更多分区，用 next_since 继续拉"
    )
    next_since: str | None = Field(
        None,
        description=(
            "下一页的 `since` 取值（`since` 是闭区间，所以它是最后一个已返回"
            "分区的**次一日**，不是它本身）。truncated=false 时为 null。"
        ),
    )


class ChangesResponse(BaseModel):
    server_time: float
    dataset: str
    as_of: str | None = Field(None, description="该数据集最新一行的游标列取值")
    freshness: Level
    items: list[dict[str, Any]] = Field(
        ...,
        description=(
            "整行原样返回（镜像语义）。**列集合会随表结构变化**——"
            "这是有意的，消费者应按列名取用而不是按位置。"
        ),
    )
    next_cursor: str | None = Field(
        None,
        description=(
            "下一页起点。**不透明**：原样回传，不要解析、不要自己拼。"
            "本页为空时返回传入的游标（或 null）。"
        ),
    )
    has_more: bool
    full_sync_recommended_after: str = Field(
        ...,
        description=(
            "建议在这个时刻之前做一次全量兜底。游标看不到「以更早时间戳写入的行」"
            "与「删除」，只有定期全量能收敛。"
        ),
    )


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _jsonable_value(value: Any) -> Any:
    """把数据库值收成能安全出 JSON 的形状。

    * `datetime` 走全仓唯一口径（ISO-8601、UTC、`Z` 结尾，见 `utc_datetime`）
      ——**不是** naive 本地时间，也不是 epoch 数字；
    * `date` 单独判（`datetime` 是 `date` 的子类，顺序反了会把日期当时间戳）；
    * 非有限浮点转 `null`：JSON 里根本没有 NaN/Infinity 的表达，写出去就是
      一份**非法 JSON**，客户端解析直接失败。转 null 是唯一不骗人的选项。
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else None
    if isinstance(value, datetime):
        return to_utc_iso(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode()
    if isinstance(value, (list, tuple)):
        return [_jsonable_value(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _jsonable_value(v) for k, v in value.items()}
    return str(value)


def _jsonable_row(row: Any) -> dict[str, Any]:
    """一行 → 对外字典。带一层「像凭据的列不外发」的网（见 `_SENSITIVE_COLUMN_RE`）。"""
    out: dict[str, Any] = {}
    for column, value in row._mapping.items():
        if _SENSITIVE_COLUMN_RE.search(column):
            # 静默丢弃比泄漏好，但**不静默**：这是一行代码错误，要有人来修。
            logger.warning(
                "[ExtData] 列 %s 命中凭据特征，已从对外载荷中剔除；"
                "请确认该列是否本就不该出现在镜像里（或改名）",
                column,
            )
            continue
        out[column] = _jsonable_value(value)
    return out


def _not_modified(request: Request, etag: str) -> bool:
    """`If-None-Match` 判定。

    **starlette 1.6.0 的 `FileResponse` 完全没有 304 分支**（实测：模块里搜不到
    `if-none-match`），所以这一段必须自己写。少了它，消费者「先问再下」就只能
    靠整份下载来回答「变了吗」。

    按 RFC 7232 做**弱比较**：`If-None-Match` 允许逗号分隔的列表、允许 `*`、
    允许 `W/"…"` 弱校验器。三者都要认——只做字符串相等的话，一个标准客户端
    发 `W/"abc"` 就会被判成「变过了」，然后每次全量重下（而这没有任何报错）。
    """
    header = request.headers.get("if-none-match")
    if not header:
        return False
    for token in header.split(","):
        candidate = token.strip()
        if candidate == "*":
            return True
        if candidate.startswith(("W/", "w/")):
            candidate = candidate[2:]
        if candidate == etag:
            return True
    return False


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _unavailable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)


def _next_since(partition: str) -> str | None:
    """分页的下一页起点：最后一个已返回分区的**次一日**。

    ⚠️ **不能直接返回 `page[-1]`。** 第一版就是这么写的，而且看起来天经地义
    （「下一页从最后一个分区继续」）。但 `since` 是**闭区间**——消费者说
    「从 09-21 起」就是要含 09-21——于是第二页从同一个值开始，把边界那天
    **又发了一遍**。重复比遗漏温和，但同样是错，而消费者按不可变语义写入时
    根本看不出来。

    加一天在这里是**精确算术**，不是「下一个交易日」那种需要日历的推算：
    入参出参都是 ISO 日期。`page` 升序且目录名唯一，所以 `parts[limit:]`
    每一项都严格大于 `page[-1]`，用「次一日」过滤既不漏也不重。
    """
    try:
        return (date.fromisoformat(partition) + timedelta(days=1)).isoformat()
    except OverflowError:  # pragma: no cover - 9999-12-31
        return None


def _full_sync_deadline() -> str:
    """「最迟什么时候该做一次全量兜底」。

    每次响应现算，不是常量：消费者可能几个月才来一次，一个写死的日期到那时
    早就过期了，而它看起来仍然像个正常值。
    """
    return to_utc_iso(
        datetime.fromtimestamp(
            time.time() + FULL_SYNC_RECOMMENDED_AFTER_DAYS * 86400.0, tz=UTC
        )
    )


# ---------------------------------------------------------------------------
# 可用性索引
# ---------------------------------------------------------------------------


def _scan_file_datasets() -> list[DatasetEntry]:
    """分区型 + 单文件型的可用性（**只读目录名与 stat**，不读内容）。

    在线程池里跑：`scandir` 与 `stat` 是阻塞系统调用，12 个数据集加起来是
    十几次目录遍历；放在事件循环上就是「外部节点接入时全站卡一下」。
    """
    entries: list[DatasetEntry] = []

    for ds in PARTITION_DATASETS:
        parts = list_partitions(ds)
        newest = parts[-1] if parts else None
        entries.append(
            DatasetEntry(
                name=ds.name,
                kind="partition",
                market=ds.market,
                grain=ds.grain,
                description=ds.description,
                available=bool(parts),
                as_of=to_utc_iso(
                    datetime.fromtimestamp(partition_date_ts(newest), tz=UTC)
                )
                if newest
                else None,
                freshness=ds.policy().classify_ts(
                    partition_date_ts(newest) if newest else None, time.time()
                ),
                partition_count=len(parts),
                first_partition=parts[0] if parts else None,
                last_partition=newest,
            )
        )

    for ds in BLOB_DATASETS:
        path = blob_file(ds)
        stat_result = stat_or_none(path) if path is not None else None
        if stat_result is None:
            entries.append(
                DatasetEntry(
                    name=ds.name,
                    kind="blob",
                    market=ds.market,
                    grain=None,
                    description=ds.description,
                    available=False,
                    as_of=None,
                    freshness="unavailable",
                )
            )
            continue
        entries.append(
            DatasetEntry(
                name=ds.name,
                kind="blob",
                market=ds.market,
                grain=None,
                description=ds.description,
                available=True,
                as_of=to_utc_iso(datetime.fromtimestamp(stat_result.st_mtime, tz=UTC)),
                freshness=ds.policy().classify_ts(stat_result.st_mtime, time.time()),
                bytes=stat_result.st_size,
                etag=etag_of(stat_result),
            )
        )

    return entries


async def _scan_row_datasets() -> list[DatasetEntry]:
    """表型数据集的可用性（每张表一次 `MAX(游标列)`）。

    查库失败时把这几项**如实报成不可用**，不抛 500：`/datasets` 是节点接入时
    问的第一件事，库暂时读不到不该让它整个接不上；其余数据集的信息仍然有用。
    失败原因进日志（`available=false` 同时意味着「没有数据」和「读不到」，
    两者的区分在服务端日志里，不在这个字段里）。
    """
    entries: list[DatasetEntry] = []
    try:
        from backend.shared.database_manager_v2 import get_session

        async with get_session(read_only=True) as session:
            for ds in ROW_DATASETS:
                # 表名/列名来自注册表常量，且 `RowDataset.__post_init__` 已把
                # 它们钉成纯标识符（见该处注释）。
                newest = (
                    await session.execute(
                        sql_text(f"SELECT MAX({ds.cursor_column}) FROM {ds.table}")
                    )
                ).scalar()
                newest_ts = newest.timestamp() if newest is not None else None
                entries.append(
                    DatasetEntry(
                        name=ds.name,
                        kind="row",
                        market="CN",
                        grain=None,
                        description=ds.description,
                        available=newest is not None,
                        as_of=to_utc_iso(newest) if newest is not None else None,
                        freshness=ds.policy().classify_ts(newest_ts, time.time()),
                    )
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ExtData] 表型数据集可用性查询失败：%s", exc)
        return [
            DatasetEntry(
                name=ds.name,
                kind="row",
                market="CN",
                grain=None,
                description=ds.description,
                available=False,
                as_of=None,
                freshness="unavailable",
            )
            for ds in ROW_DATASETS
        ]
    return entries


@router.get("/datasets", response_model=DatasetsResponse)
async def list_datasets(
    principal: ExternalPrincipal = Depends(require_external_principal),
) -> DatasetsResponse:
    """这个部署有哪些数据、覆盖到哪天、是不是新鲜。

    **先问再做**：外部节点接入的第一件事就是调它。`available=false` 的数据集
    （本机没挂这个市场的盘 / 这张表还是空的）如实报告，不让对方去试。

    文件扫描与查库**并发**执行：两者互不依赖，而查库那一侧有一张 68 万行的表。
    """
    file_entries, row_entries = await asyncio.gather(
        run_in_threadpool(_scan_file_datasets),
        _scan_row_datasets(),
    )
    return DatasetsResponse(
        server_time=time.time(),
        datasets=[*file_entries, *row_entries],
    )


# ---------------------------------------------------------------------------
# 分区清单
# ---------------------------------------------------------------------------


def _stat_partitions(ds: Any, partitions: list[str]) -> list[PartitionEntry]:
    """给每个分区 `stat` 一次——`etag`/字节数/时间都从这一次拿到。"""
    out: list[PartitionEntry] = []
    for part in partitions:
        path = partition_file(ds, part)
        stat_result = stat_or_none(path) if path is not None else None
        if stat_result is None:
            # 目录在、文件没了（正在重写）。跳过它比给一个空 etag 好：
            # 消费者下一轮会再问一次，届时要么出现要么继续缺席。
            continue
        out.append(
            PartitionEntry(
                partition=part,
                bytes=stat_result.st_size,
                etag=etag_of(stat_result),
                mtime=to_utc_iso(datetime.fromtimestamp(stat_result.st_mtime, tz=UTC)),
            )
        )
    return out


@router.get("/datasets/{name}/partitions", response_model=PartitionsResponse)
async def list_dataset_partitions(
    name: str,
    since: str | None = Query(None, description="起始分区（含），YYYY-MM-DD"),
    until: str | None = Query(None, description="结束分区（含），YYYY-MM-DD"),
    limit: int = Query(DEFAULT_PARTITION_PAGE, ge=1, le=MAX_PARTITION_PAGE),
    principal: ExternalPrincipal = Depends(require_external_principal),
) -> PartitionsResponse:
    """分区清单：增量判断的锚点。

    **服务端不记得消费者手里有什么**，所以没有「只回变化的分区」这种接口——
    比对交给消费者（它本来就有自己那份清单）。这比维护 per-consumer 水位更简单，
    也更诚实：不会出现「服务端记错了消费者进度」这类没法调试的故障。

    `as_of` 是**该数据集最新分区的日期**，不是服务器当前时间：消费者真正关心
    的是「我的镜像新不新」，不是「你几点回我」。
    """
    ds = next((d for d in PARTITION_DATASETS if d.name == name), None)
    if ds is None:
        raise _not_found("dataset_not_found")

    for label, value in (("since", since), ("until", until)):
        if value is not None and normalize_partition(value) is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"invalid_{label}",
            )

    parts = await run_in_threadpool(list_partitions, ds, since=since, until=until)
    page = parts[:limit]
    truncated = len(parts) > limit
    successor = _next_since(page[-1]) if page else None
    if truncated and successor is None:
        # 已到可表示的最大日期，后面不可能再有分区。如实说「没有了」，
        # 而不是给一个 null 让消费者从头把第一页再拉一遍。
        truncated = False
    entries = await run_in_threadpool(_stat_partitions, ds, page)

    newest = parts[-1] if parts else None
    return PartitionsResponse(
        server_time=time.time(),
        dataset=ds.name,
        as_of=to_utc_iso(datetime.fromtimestamp(partition_date_ts(newest), tz=UTC))
        if newest
        else None,
        freshness=ds.policy().classify_ts(
            partition_date_ts(newest) if newest else None, time.time()
        ),
        partitions=entries,
        truncated=truncated,
        # 「下一页的 since」= 最后一个已返回分区的**次一日**（见 `_next_since`）。
        # 返回 page[-1] 会让边界那天在第二页里再出现一次。
        next_since=successor if truncated else None,
    )


# ---------------------------------------------------------------------------
# 取文件
# ---------------------------------------------------------------------------


def _serve_file(request: Request, path: Any, stat_result: Any, etag: str) -> Response:
    """统一的文件响应：304 短路 + `FileResponse`（Range/206/416 由它负责）。

    * `If-None-Match` 必须在进 `FileResponse` **之前**短路：它没有 304 分支。
    * 传 `stat_result=` 既省掉它内部那次 `os.stat`，也避开「stat 完文件被删」
      的竞态——两次 stat 之间文件可能被整段重写。
    * `Range` 断点续传、不可满足区间 `416`、多区间 multipart、`HEAD`
      全部由 `FileResponse` 白送（实测 starlette 1.6.0，`chunk_size=64KB`、
      `max_ranges=100`、`accept-ranges: bytes` 默认开）。
    """
    if _not_modified(request, etag):
        return Response(
            status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag}
        )
    return FileResponse(
        path,
        media_type=PARQUET_MEDIA_TYPE,
        stat_result=stat_result,
        headers={"ETag": etag},
    )


@router.get("/datasets/{name}/partitions/{partition}/file")
async def get_partition_file(
    name: str,
    partition: str,
    request: Request,
    principal: ExternalPrincipal = Depends(require_external_principal),
) -> Response:
    """取某个分区的 parquet。

    路径安全是这一面最大的风险点（`{name}`/`{partition}` 都是用户输入、都会
    参与拼路径），所以四层叠着来：

    1. `{name}` **只能**从注册表查到，查不到即 404 ——绝不拼接到路径上；
    2. `{partition}` 必须是 `YYYY-MM-DD`（正则挡形状、`date.fromisoformat`
       挡 `2026-13-45` 这种形状对但值越界的）；
    3. 拼出的路径经 `realpath` 后必须仍在 QuantDB 根之下（`commonpath` 判定，
       不是 `startswith`——后者被 `root=/a/b` vs `/a/bc` 绕过）；
    4. 「分区不存在」与「路径非法」对外同形（都是 404），不给探测 oracle。
    """
    ds = next((d for d in PARTITION_DATASETS if d.name == name), None)
    if ds is None:
        raise _not_found("dataset_not_found")

    if normalize_partition(partition) is None:
        # 格式错是**客户端错误**（400）：规则是公开的、无状态的，不构成 oracle。
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_partition"
        )

    # 「本机没挂这个市场的盘」= 503，与「这个分区没有」= 404 分开，让运维查对方向。
    # 这里只判**目录在不在**，不枚举分区：下每个文件都扫一遍整个数据集目录
    # （`daily_forward` 有两千六百多个分区）是白花的。
    directory = dataset_dir(ds.rel_dir)
    if directory is None or not directory.is_dir():
        raise _unavailable("dataset_unavailable")

    path = partition_file(ds, partition)
    stat_result = stat_or_none(path) if path is not None else None
    if stat_result is None:
        raise _not_found("partition_not_found")

    return _serve_file(request, path, stat_result, etag_of(stat_result))


@router.get("/datasets/{name}/blob")
async def get_blob_file(
    name: str,
    request: Request,
    principal: ExternalPrincipal = Depends(require_external_principal),
) -> Response:
    """取单文件数据集的整份内容（交易日历、个股详情、板块成分）。

    这类文件是**原地重写**的（不是按日追加），所以消费者只有两条路：
    比对 `etag` 决定要不要重下，或者带 `Range` 断点续传。两者都由本端点支持。
    """
    ds = next((d for d in BLOB_DATASETS if d.name == name), None)
    if ds is None:
        raise _not_found("dataset_not_found")

    path = blob_file(ds)
    stat_result = stat_or_none(path) if path is not None else None
    if stat_result is None:
        raise _unavailable("dataset_unavailable")

    return _serve_file(request, path, stat_result, etag_of(stat_result))


# ---------------------------------------------------------------------------
# 行级增量
# ---------------------------------------------------------------------------


def _build_changes_sql(ds: RowDataset, *, scoped: bool, paged: bool) -> str:
    """拼出翻页查询。

    四个必须一起出现的细节（少一个就会静默出错）：

    * 排序键与比较键是**同一个元组** `(游标列, 兜底键…)`。只按时间戳翻页、
      不用兜底键，会在同一时间戳有多行时漏行——一次 upsert 写几千行在同一个
      `NOW()` 上是常态。
    * 边界是**严格大于** `>`，不是 `>=`：`>=` 会让消费者卡在同一行上无限循环。
    * **没有 `CAST`**：参数由 Python 值定型（`datetime`/`int`/`str`/`date`），
      与列类型自然对齐。线上那串字符串只活在游标里，绑定前已还原
      （见 `datasets._KEY_CASTS`）。
    * 首页**不带比较谓词**，而不是「给一个足够早的哨兵值」。哨兵值对文本键
      必然有漏洞——排序上小于任何非空串的那个值是空串本身，于是一行键为
      空串的记录从第一页起就永远被跳过，且没有任何报错。

    `cursor_column IS NOT NULL` 是**显式写出来的**，不靠三值逻辑的副产品：
    游标列是 NULL 的行（`engine_feature_runs.updated_at` 在 schema 上可空，
    当前 0 行）不可能被任何元组比较选中，它们**对游标接口永久不可见**。
    写出来是为了让这个事实出现在 SQL 里，而不是藏在「恰好 NULL 比较为假」里。
    """
    keys = ", ".join(ds.key_columns)
    where = [f"{ds.cursor_column} IS NOT NULL"]
    if paged:
        args = ", ".join([":t", *(f":k{i}" for i in range(len(ds.key_columns)))])
        where.append(f"({ds.cursor_column}, {keys}) > ({args})")
    if scoped:
        where.append("tenant_id = :tenant AND user_id = :user_id")
    return "\n".join(
        [
            f"SELECT * FROM {ds.table}",
            "WHERE " + " AND ".join(where),
            f"ORDER BY {ds.cursor_column}, {keys}",
            "LIMIT :limit",
        ]
    )


@router.get("/{dataset}/changes", response_model=ChangesResponse)
async def dataset_changes(
    dataset: str,
    cursor: str | None = Query(
        None,
        description=(
            "上一页返回的 next_cursor（不透明，原样回传）。"
            "**首次不带** = 从头开始按序拉全量。"
        ),
    ),
    limit: int = Query(DEFAULT_ROW_PAGE, ge=1, le=MAX_ROW_PAGE),
    principal: ExternalPrincipal = Depends(require_external_principal),
) -> ChangesResponse:
    """按 `(游标列, 兜底键)` 元组增量取行。

    **至少一次**：同一行可能被发出两次（边界重叠、客户端重试）。消费者必须
    按主键幂等写入，不能假设不重复。
    """
    ds = next((d for d in ROW_DATASETS if d.name == dataset), None)
    if ds is None:
        raise _not_found("dataset_not_found")

    position = None
    if cursor:
        try:
            position = decode_cursor(cursor, dataset=ds.name)
            key_values = ds.coerce_key(position.key)
        except (InvalidCursor, ValueError) as exc:
            # 400 而不是 404/500：游标是**客户端自己**给的定位信息，坏了就该
            # 由它丢弃重来。日志里带原因（不含游标原文，见 cursor 模块）。
            #
            # 兜底键组件数不符也走这里：那说明这个游标不是本数据集**当前**
            # 格式产出的（比如数据集换了兜底键）。宁可让它全量重来，不能猜。
            logger.info("[ExtData] 游标被拒 dataset=%s：%s", ds.name, exc)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_cursor"
            ) from exc
    else:
        key_values = ()

    from backend.shared.database_manager_v2 import get_session

    sql = _build_changes_sql(ds, scoped=ds.tenant_scoped, paged=position is not None)
    params: dict[str, Any] = {"limit": limit + 1}
    if position is not None:
        params["t"] = position.updated_at
        for i, value in enumerate(key_values):
            params[f"k{i}"] = value
    if ds.tenant_scoped:
        params["tenant"] = principal.tenant_id
        params["user_id"] = principal.user_id

    try:
        async with get_session(read_only=True) as session:
            result = await session.execute(sql_text(sql), params)
            rows = result.fetchall()
            newest = (
                await session.execute(
                    sql_text(f"SELECT MAX({ds.cursor_column}) FROM {ds.table}")
                )
            ).scalar()
    except Exception as exc:  # noqa: BLE001
        # 读库失败是**服务端**的问题，不是调用方的问题 —— 503 并说明可重试，
        # 不要报成 400/404 把对接方引去查自己的请求。
        logger.warning("[ExtData] 数据面查询失败 dataset=%s：%s", ds.name, exc)
        raise _unavailable("dataset_unavailable") from exc

    has_more = len(rows) > limit
    page = rows[:limit]
    items = [_jsonable_row(row) for row in page]

    next_cursor = cursor
    if page:
        last = page[-1]._mapping
        next_cursor = encode_cursor(
            ds.name,
            updated_at=last[ds.cursor_column],
            key=tuple(str(last[column]) for column in ds.key_columns),
        )

    newest_ts = newest.timestamp() if newest is not None else None
    return ChangesResponse(
        server_time=time.time(),
        dataset=ds.name,
        as_of=to_utc_iso(newest) if newest is not None else None,
        freshness=ds.policy().classify_ts(newest_ts, time.time()),
        items=items,
        next_cursor=next_cursor,
        has_more=has_more,
        full_sync_recommended_after=_full_sync_deadline(),
    )


__all__ = ["router", "DEFAULT_ROW_PAGE", "MAX_ROW_PAGE", "PARQUET_MEDIA_TYPE"]
