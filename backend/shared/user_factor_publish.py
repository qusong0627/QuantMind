"""用户因子训练数据集自动发布（企业版 generation 语义的 OSS 下放）。

企业版（``alpha_user_factor_generations`` + ``alpha_user_factor_dataset_active``）：
因子物化任务完成时在同一事务内把 generation 置为 ``published``、登记特征映射、
切换 active 指针并归档旧版；失败任务不切换指针，训练/推理只读 active 指向的
``published`` 版本。

OSS 为单容器部署，无租户隔离与因子任务队列，故做如下简化（语义不变）：

- 按 ``dataset_key`` 单流推进版本，不分 tenant/user；
- generation 是逻辑版本标记（parquet 仍累积写
  ``quantcustom/6_ml_datasets/l1_factors``，见 ``merge_factor_into_source``），
  不做物理目录拷贝；指针即训练可用的版本真相；
- 校验门禁与企业版 ``factor_jobs.py`` 同口径：非法结果抛 ``ValueError``，
  指针不动；显式失败记 ``failed``，指针不动。

调用方（``admin/alpha_factor_pipeline.py``）流程：
``create_building_generation`` → 离线计算 + parquet 合并 →
``publish_completed_generation``（内含字段注册表 upsert）；
任一步失败 → ``mark_generation_failed``。

数据初始化（魔搭全量拉取 / 本地扫描）完成后调
``publish_baseline_after_data_init_sync`` 做一次基线发布：
把 ``quantcustom`` 现有用户因子列按列建代次并切指针；目录为空或与
active 指针状态一致时直接跳过（幂等，可重复点初始化）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

logger = logging.getLogger(__name__)

DATASET_KEYS = ("l1_factors", "rd_mined")
DEFAULT_DATASET_KEY = "l1_factors"
GENERATION_STATUSES = ("building", "published", "failed", "archived")

# 基线发布来源标记（写进 manifest，区别于 promote 触发的代次）。
BASELINE_SOURCE = "data-init"

# 用户因子目录里不算因子的键列（与 merge/训练侧口径对齐）。
_BASELINE_KEY_COLUMNS = frozenset(
    {
        "symbol",
        "date",
        "dt",
        "trade_date",
        "datetime",
        "time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "release_id",
        "published_at",
    }
)

_FEATURE_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")
_GENERATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def new_generation_id() -> str:
    """生成 16 位 hex 代次 ID（满足库约束，可直接作目录名）。"""
    return uuid.uuid4().hex[:16]


def expression_sha256(expression: str) -> str:
    return hashlib.sha256(str(expression or "").encode("utf-8")).hexdigest()


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def utc_now_dt() -> datetime:
    """DB 绑定用：必须传 aware UTC datetime 实例。

    asyncpg 在参数绑定阶段就按列类型校验，`TIMESTAMPTZ` 列传 `str`
    直接报 `DataError`，SQL 里加 `CAST(:x AS TIMESTAMPTZ)` 也救不了
    （psycopg2 会隐式转，所以单测/sqlite 下不爆）。调用方返回给
    JSON 的仍用 `utcnow_iso()`，写库一律用本函数。
    """
    return datetime.now(timezone.utc)


def as_date(value: object) -> date | None:
    """归一化 `DATE` 列绑定值：接受 `date/datetime/YYYY-MM-DD/YYYYMMDD`。

    返回 `date` 实例或 None；空串/None → None；非法 → ValueError。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s:
        return None
    # 兼容 ISO datetime（"2024-01-02T00:00:00+00:00" / "2024-01-02 00:00:00"）
    if "T" in s:
        s = s.split("T", 1)[0].strip()
    elif " " in s and "-" in s:
        s = s.split(" ", 1)[0].strip()
    if len(s) == 8 and s.isdigit():
        return date(int(s[0:4]), int(s[4:6]), int(s[6:8]))
    try:
        return date.fromisoformat(s)
    except ValueError:
        for fmt in ("%Y%m%d", "%Y/%m/%d", "%Y.%m.%d"):
            try:
                return datetime.strptime(s, fmt).date()
            except ValueError:
                continue
        raise ValueError(f"非法日期：{value!r}") from None


def validate_dataset_key(value: object) -> str:
    dataset = str(value or "").strip()
    if dataset not in DATASET_KEYS:
        raise ValueError(f"非法 dataset_key：{dataset!r}，仅支持 {list(DATASET_KEYS)}")
    return dataset


def validate_feature_key(value: object) -> str:
    feature = str(value or "").strip()
    if not _FEATURE_KEY_RE.match(feature):
        raise ValueError(f"非法 feature_key：{feature!r}")
    return feature


def validate_generation_id(value: object) -> str:
    generation_id = str(value or "").strip()
    if not _GENERATION_ID_RE.match(generation_id):
        raise ValueError(f"非法 generation_id：{generation_id!r}")
    return generation_id


async def ensure_user_factor_schema(session: Any) -> None:
    """幂等建表（PG 与 sqlite 通用；时间戳一律由调用方显式传入）。"""
    await session.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS qm_user_factor_generations (
                generation_id TEXT PRIMARY KEY,
                dataset_key TEXT NOT NULL DEFAULT 'l1_factors',
                status TEXT NOT NULL DEFAULT 'building',
                feature_key TEXT NOT NULL,
                factor_name TEXT NOT NULL DEFAULT '',
                expression_sha256 TEXT NOT NULL DEFAULT '',
                manifest_json TEXT NOT NULL DEFAULT '{}',
                row_count INTEGER NOT NULL DEFAULT 0,
                byte_count INTEGER NOT NULL DEFAULT 0,
                error_message TEXT NOT NULL DEFAULT '',
                created_by TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ NOT NULL,
                published_at TIMESTAMPTZ,
                CHECK (dataset_key IN ('l1_factors', 'rd_mined')),
                CHECK (status IN ('building', 'published', 'failed', 'archived'))
            );
            """
        )
    )
    await session.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS qm_user_factor_dataset_active (
                dataset_key TEXT PRIMARY KEY,
                generation_id TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL,
                CHECK (dataset_key IN ('l1_factors', 'rd_mined'))
            );
            """
        )
    )
    await session.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_qm_user_factor_generations_ds_status "
            "ON qm_user_factor_generations (dataset_key, status, created_at DESC)"
        )
    )


async def create_building_generation(
    session: Any,
    *,
    generation_id: str,
    dataset_key: str = DEFAULT_DATASET_KEY,
    feature_key: str,
    factor_name: str = "",
    expression: str = "",
    created_by: str = "",
) -> dict[str, Any]:
    """登记一个 ``building`` 代次（物化开始前调用）。"""
    generation_id = validate_generation_id(generation_id)
    dataset = validate_dataset_key(dataset_key)
    feature = validate_feature_key(feature_key)
    now_dt = utc_now_dt()
    await session.execute(
        text(
            """
            INSERT INTO qm_user_factor_generations
              (generation_id, dataset_key, status, feature_key, factor_name,
               expression_sha256, created_by, created_at)
            VALUES (:gid, :dataset, 'building', :feature, :name, :sha, :by, :now)
            """
        ),
        {
            "gid": generation_id,
            "dataset": dataset,
            "feature": feature,
            "name": str(factor_name or "")[:256],
            "sha": expression_sha256(expression),
            "by": str(created_by or "")[:128],
            "now": now_dt,
        },
    )
    return {
        "generation_id": generation_id,
        "dataset_key": dataset,
        "status": "building",
        "feature_key": feature,
        "created_at": now_dt.isoformat(),
    }


def _validate_completed_payload(
    *, row_count: object, byte_count: object, expression_sha: str
) -> tuple[int, int]:
    """企业版 finish() 门禁的 OSS 等价：非法结果直接拒收（抛错，不落库）。"""
    if isinstance(row_count, bool) or not isinstance(row_count, int) or row_count <= 0:
        raise ValueError(f"发布结果非法：row_count 必须为正整数，实得 {row_count!r}")
    if (
        isinstance(byte_count, bool)
        or not isinstance(byte_count, int)
        or byte_count <= 0
    ):
        raise ValueError(f"发布结果非法：byte_count 必须为正整数，实得 {byte_count!r}")
    if expression_sha and not _SHA256_RE.match(str(expression_sha)):
        raise ValueError("发布结果非法：expression_sha256 不是 64 位 hex")
    return int(row_count), int(byte_count)


async def publish_completed_generation(
    session: Any,
    *,
    generation_id: str,
    dataset_key: str = DEFAULT_DATASET_KEY,
    row_count: int,
    byte_count: int,
    manifest_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把 ``building`` 代次原子发布：置 published → 切换指针 → 归档旧版。

    三条写语句与调用方会话同事务；任一失败整体回滚，指针不动。
    ``manifest_extra`` 只收 JSON 可序列化字段。
    """
    generation_id = validate_generation_id(generation_id)
    dataset = validate_dataset_key(dataset_key)
    row = (
        (
            await session.execute(
                text(
                    "SELECT feature_key, factor_name, expression_sha256 "
                    "FROM qm_user_factor_generations "
                    "WHERE generation_id = :gid AND dataset_key = :dataset"
                ),
                {"gid": generation_id, "dataset": dataset},
            )
        )
        .mappings()
        .first()
    )
    if not row:
        raise ValueError(f"代次不存在：{generation_id!r} / {dataset!r}")
    rows, byte_count = _validate_completed_payload(
        row_count=row_count,
        byte_count=byte_count,
        expression_sha=str(row["expression_sha256"] or ""),
    )
    manifest = {
        "dataset_key": dataset,
        "feature_key": str(row["feature_key"]),
        "factor_name": str(row["factor_name"] or ""),
        "expression_sha256": str(row["expression_sha256"] or ""),
        "row_count": rows,
        "byte_count": byte_count,
    }
    for key, value in dict(manifest_extra or {}).items():
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", str(key)):
            manifest[str(key)] = value
    manifest_json = json.dumps(
        manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    now_dt = utc_now_dt()
    updated = await session.execute(
        text(
            """
            UPDATE qm_user_factor_generations
               SET status = 'published', manifest_json = :manifest,
                   row_count = :rows, byte_count = :bytes, published_at = :now
             WHERE generation_id = :gid AND dataset_key = :dataset
               AND status = 'building'
            """
        ),
        {
            "manifest": manifest_json,
            "rows": rows,
            "bytes": byte_count,
            "now": now_dt,
            "gid": generation_id,
            "dataset": dataset,
        },
    )
    if updated.rowcount != 1:
        raise RuntimeError(f"代次已不在 building 状态，拒绝发布：{generation_id!r}")
    await session.execute(
        text(
            """
            INSERT INTO qm_user_factor_dataset_active
              (dataset_key, generation_id, updated_at)
            VALUES (:dataset, :gid, :now)
            ON CONFLICT (dataset_key) DO UPDATE SET
              generation_id = EXCLUDED.generation_id,
              updated_at = EXCLUDED.updated_at
            """
        ),
        {"dataset": dataset, "gid": generation_id, "now": now_dt},
    )
    await session.execute(
        text(
            """
            UPDATE qm_user_factor_generations SET status = 'archived'
             WHERE dataset_key = :dataset AND status = 'published'
               AND generation_id <> :gid
            """
        ),
        {"dataset": dataset, "gid": generation_id},
    )
    return {
        "generation_id": generation_id,
        "dataset_key": dataset,
        "status": "published",
        "manifest": manifest,
        "published_at": now_dt.isoformat(),
    }


async def mark_generation_failed(
    session: Any, generation_id: str, error: str = ""
) -> bool:
    """把 ``building`` 代次记为失败；指针不动。返回是否实际标记成功。"""
    generation_id = validate_generation_id(generation_id)
    updated = await session.execute(
        text(
            """
            UPDATE qm_user_factor_generations
               SET status = 'failed', error_message = :error
             WHERE generation_id = :gid AND status = 'building'
            """
        ),
        {"gid": generation_id, "error": str(error or "")[:2000]},
    )
    return (updated.rowcount or 0) == 1


async def get_active_generation(
    session: Any, dataset_key: str = DEFAULT_DATASET_KEY
) -> dict[str, Any] | None:
    """读 active 指针指向的 ``published`` 代次（训练侧唯一可信版本）。"""
    dataset = validate_dataset_key(dataset_key)
    row = (
        (
            await session.execute(
                text(
                    """
            SELECT active.generation_id, generation.status,
                   generation.feature_key, generation.factor_name,
                   generation.expression_sha256, generation.manifest_json,
                   generation.row_count, generation.byte_count,
                   generation.published_at
              FROM qm_user_factor_dataset_active AS active
              JOIN qm_user_factor_generations AS generation
                ON generation.generation_id = active.generation_id
               AND generation.dataset_key = active.dataset_key
             WHERE active.dataset_key = :dataset
               AND generation.status = 'published'
            """
                ),
                {"dataset": dataset},
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


async def list_generations(
    session: Any,
    dataset_key: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """按创建时间倒序列出代次（含 building/failed/archived，便于排障）。"""
    dataset = validate_dataset_key(dataset_key) if dataset_key else None
    rows = (
        (
            await session.execute(
                text(
                    "SELECT generation_id, dataset_key, status, feature_key, factor_name,"
                    " row_count, byte_count, created_by, created_at, published_at"
                    " FROM qm_user_factor_generations"
                    + (" WHERE dataset_key = :dataset" if dataset else "")
                    + " ORDER BY created_at DESC LIMIT :limit"
                ),
                {"dataset": dataset, "limit": max(1, min(int(limit or 50), 200))},
            )
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


async def upsert_discovered_field(
    session: Any,
    *,
    column: str,
    market: str = "CUSTOM",
    dataset_id: str = DEFAULT_DATASET_KEY,
    min_date: str | date | datetime | None = None,
    max_date: str | date | datetime | None = None,
) -> None:
    """发布成功后自动登记字段发现表，省掉一次手动 ``/sources/refresh``。

    只做单列 upsert（``is_present=TRUE``），不碰其它列；训练特征目录的
    草稿/发布仍由管理员在 ``quantdb_factor_catalog`` 侧决策。

    asyncpg 按列类型绑定：``min_date/max_date`` 必须传 `date` 实例、
    ``discovered_at`` 必须传 aware `datetime`，传 `str` 直接 DataError。
    为兼容旧调用，本函数接受 `str` 并内部归一化。
    """
    feature = validate_feature_key(column)
    await session.execute(
        text(
            """
            INSERT INTO qm_quantdb_factor_field
              (market, dataset_id, column_name, data_type, schema_hash,
               min_date, max_date, is_present, discovered_at)
            VALUES (:market, :dataset, :column, 'double', '', :min_date,
                    :max_date, TRUE, :now)
            ON CONFLICT (market, dataset_id, column_name) DO UPDATE SET
              is_present = TRUE, discovered_at = EXCLUDED.discovered_at,
              min_date = COALESCE(EXCLUDED.min_date, qm_quantdb_factor_field.min_date),
              max_date = COALESCE(EXCLUDED.max_date, qm_quantdb_factor_field.max_date)
            """
        ),
        {
            "market": str(market or "CUSTOM").upper(),
            "dataset": str(dataset_id or DEFAULT_DATASET_KEY),
            "column": feature,
            "min_date": as_date(min_date),
            "max_date": as_date(max_date),
            "now": utc_now_dt(),
        },
    )


def describe_custom_user_dataset_state(
    dataset_key: str = DEFAULT_DATASET_KEY,
) -> dict[str, Any] | None:
    """扫描用户因子目录现状（纯本地 IO，无 DB）。

    返回 ``{columns, partitions, fingerprint, min_date, max_date,
    non_null, latest_rows}``；目录为空或无因子列时返回 None。
    ``fingerprint`` 是「列集合 + 分区集合 + 总字节」的 sha256，
    供基线发布做幂等跳过判断。
    """
    from backend.shared.feature_source import user_dataset_dir

    dataset = validate_dataset_key(dataset_key)
    root = Path(user_dataset_dir(dataset))
    parts = sorted(p for p in root.glob("dt=*/data.parquet") if p.is_file())
    if not parts:
        return None
    try:
        import pyarrow.parquet as pq
    except ImportError:
        logger.warning("pyarrow 不可用，跳过用户因子目录扫描")
        return None
    columns: set[str] = set()
    for part in parts:
        try:
            columns |= set(pq.ParquetFile(str(part)).schema_arrow.names)
        except Exception:  # noqa: BLE001
            logger.warning("跳过不可读分区: %s", part)
    feature_columns = sorted(c for c in columns if c not in _BASELINE_KEY_COLUMNS)
    if not feature_columns:
        return None
    dts: list[str] = []
    total_bytes = 0
    for part in parts:
        try:
            dts.append(part.parent.name.split("=", 1)[1])
        except IndexError:  # noqa: BLE001
            continue
        try:
            total_bytes += part.stat().st_size
        except OSError:  # noqa: BLE001
            continue
    if not dts:
        return None
    dts = sorted(dts)
    fingerprint = hashlib.sha256(
        f"{','.join(feature_columns)}|{','.join(dts)}|{total_bytes}".encode()
    ).hexdigest()
    non_null: dict[str, int] = {}
    latest_rows = 0
    try:
        import pandas as pd

        latest = pd.read_parquet(parts[-1], engine="pyarrow")
        latest_rows = int(len(latest))
        for column in feature_columns:
            if column in latest.columns:
                non_null[column] = int(latest[column].notna().sum())
            else:
                non_null[column] = 0
    except Exception:  # noqa: BLE001
        logger.warning("读取最新分区失败，仅登记列与分区: %s", parts[-1])
    return {
        "dataset_key": dataset,
        "root": str(root),
        "columns": feature_columns,
        "partitions": dts,
        "partition_count": len(dts),
        "total_bytes": total_bytes,
        "fingerprint": fingerprint,
        "min_date": dts[0],
        "max_date": dts[-1],
        "non_null": non_null,
        "latest_rows": latest_rows,
    }


def _manifest_fingerprint(manifest_json: object) -> str | None:
    try:
        manifest = json.loads(str(manifest_json or "{}"))
    except (ValueError, TypeError):
        return None
    fingerprint = manifest.get("fingerprint") if isinstance(manifest, dict) else None
    return str(fingerprint) if fingerprint else None


async def _latest_feature_fingerprint(
    session: Any, dataset_key: str, feature_key: str
) -> str | None:
    """该列最近一个 published 代次的指纹（无则 None）。"""
    row = (
        (
            await session.execute(
                text(
                    "SELECT manifest_json FROM qm_user_factor_generations "
                    "WHERE dataset_key = :dataset AND feature_key = :feature "
                    "AND status = 'published' "
                    "ORDER BY published_at DESC LIMIT 1"
                ),
                {"dataset": dataset_key, "feature": feature_key},
            )
        )
        .mappings()
        .first()
    )
    return _manifest_fingerprint(row["manifest_json"]) if row else None


def dataset_state_fingerprint(state: dict[str, Any] | None) -> str | None:
    """基线/publish 两侧统一的指纹读取（含 promote 时写入的指纹）。"""
    if not state:
        return None
    fingerprint = state.get("fingerprint")
    return str(fingerprint) if fingerprint else None


async def publish_data_init_baseline(
    session: Any,
    *,
    dataset_key: str = DEFAULT_DATASET_KEY,
    created_by: str = "data-init",
) -> dict[str, Any]:
    """数据初始化完成后的基线发布：按列建代次并切指针。

    - 目录为空 / 无因子列 → 跳过（不建代次，指针不动）；
    - 当前目录指纹与 active 代次一致 → 跳过（幂等，重复点初始化不刷版本）；
    - 单列失败 → 记 ``failed`` 后继续下一列，不中断、不回滚已发布列。
    返回 ``{action, published, skipped_unchanged, skipped_empty, failed, ...}``。
    """
    dataset = validate_dataset_key(dataset_key)
    state = await asyncio.to_thread(describe_custom_user_dataset_state, dataset)
    if not state:
        return {
            "action": "skipped",
            "reason": "empty",
            "detail": "用户因子目录为空或无因子列，未发布",
        }
    fingerprint = str(state["fingerprint"])
    active = await get_active_generation(session, dataset)
    if active and _manifest_fingerprint(active.get("manifest_json")) == fingerprint:
        return {
            "action": "skipped",
            "reason": "up-to-date",
            "detail": "目录状态与 active 代次一致，未发布",
            "fingerprint": fingerprint,
            "active_generation_id": active.get("generation_id"),
        }
    published: list[str] = []
    skipped_unchanged: list[str] = []
    skipped_empty: list[str] = []
    failed: list[dict[str, str]] = []
    non_null = state.get("non_null") or {}
    for column in state["columns"]:
        rows = int(non_null.get(column, 0))
        if rows <= 0:
            skipped_empty.append(column)
            continue
        if await _latest_feature_fingerprint(session, dataset, column) == fingerprint:
            skipped_unchanged.append(column)
            continue
        generation_id = new_generation_id()
        try:
            await create_building_generation(
                session,
                generation_id=generation_id,
                dataset_key=dataset,
                feature_key=column,
                factor_name=column,
                expression="",
                created_by=str(created_by or "data-init")[:128],
            )
            await publish_completed_generation(
                session,
                generation_id=generation_id,
                dataset_key=dataset,
                row_count=rows,
                byte_count=max(1, rows * 8),
                manifest_extra={
                    "source": BASELINE_SOURCE,
                    "fingerprint": fingerprint,
                    "partitions": state["partition_count"],
                    "min_date": state["min_date"],
                    "max_date": state["max_date"],
                },
            )
            await upsert_discovered_field(
                session,
                column=column,
                market="CUSTOM",
                dataset_id=dataset,
                min_date=state["min_date"],
                max_date=state["max_date"],
            )
            published.append(column)
        except Exception as exc:  # noqa: BLE001
            try:
                await mark_generation_failed(session, generation_id, error=str(exc))
            except Exception:  # noqa: BLE001
                logger.warning("基线发布标记失败状态异常: %s", generation_id)
            failed.append({"column": column, "error": str(exc)[:300]})
    return {
        "action": "published" if published else "skipped",
        "reason": "baseline",
        "published": published,
        "skipped_unchanged": skipped_unchanged,
        "skipped_empty": skipped_empty,
        "failed": failed,
        "fingerprint": fingerprint,
        "min_date": state["min_date"],
        "max_date": state["max_date"],
        "partition_count": state["partition_count"],
    }


def publish_baseline_after_data_init_sync(
    *,
    dataset_key: str = DEFAULT_DATASET_KEY,
    created_by: str = "data-init",
) -> dict[str, Any]:
    """同步包装，供初始化后台线程调用；永不抛错（失败只记日志+返回）。"""
    try:
        from backend.shared.database_manager_v2 import get_session
    except ImportError as exc:
        logger.warning("基线发布跳过（DB 模块不可用）: %s", exc)
        return {"action": "skipped", "reason": "no-db", "detail": str(exc)[:200]}

    async def _main() -> dict[str, Any]:
        async with get_session() as session:
            await ensure_user_factor_schema(session)
            return await publish_data_init_baseline(
                session, dataset_key=dataset_key, created_by=created_by
            )

    try:
        return asyncio.run(_main())
    except Exception as exc:  # noqa: BLE001
        logger.warning("数据初始化后基线发布失败（不影响初始化结果）: %s", exc)
        return {"action": "skipped", "reason": "error", "detail": str(exc)[:300]}
