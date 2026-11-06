"""合并导出：把多个已物化因子的分区 parquet 拼成一张宽表。

- 数据源：各因子最新一次 `materialized` 物化的 generation 目录
  （`factor_generations/{gen}/6_ml_datasets/rd_mined/dt=*/data.parquet`），
  归档 generation 的文件仍在盘上，可照常读取。
- 产物：用户目录 `exports/factor_export_{export_id}.parquet`（单文件，
  配额台账走 `pipeline_artifacts`），供下载与审计留痕。
- 训练仍直接读 `rd_mined` 源列（`user_factor_columns`），导出文件不进入
  训练特征发现口径，避免污染查重基线。

失败一律 fail-closed：缺物化版本、目录丢失、无交集行数都抛错不落半成品。
"""

from __future__ import annotations

import io
import re
import uuid
from pathlib import Path
from typing import Any

from backend.shared.feature_source import quantcustom_root, user_factor_dataset_dir
from backend.shared.owner_path import owner_segment, user_scoped_dir

_EXPORT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_MAX_FACTORS = 12
_COLUMN_RE = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)


class ExportPreconditionError(ValueError):
    """导出前置条件不满足（缺物化版本/目录丢失/无交集），调用方转 422。"""


def sanitize_export_column(name: str | None, fallback: str) -> str:
    """展示名 → parquet 安全列名：只留中英数下划线，超长截断，空时回退。"""
    text = re.sub(_COLUMN_RE, "_", str(name or "").strip()).strip("_")
    if not text:
        text = re.sub(_COLUMN_RE, "_", str(fallback or "factor").strip()).strip("_") or "factor"
    if len(text) > 64:
        text = text[:64].rstrip("_") or "factor"
    if text[0].isdigit():
        text = f"f_{text}"
    return text


def _unique_columns(names: list[str]) -> list[str]:
    """重名列加 _2/_3 后缀保唯一（保序）。"""
    seen: dict[str, int] = {}
    out: list[str] = []
    for name in names:
        count = seen.get(name, 0)
        if count == 0 and name not in seen:
            seen[name] = 1
            out.append(name)
            continue
        index = count + 1
        while f"{name}_{index}" in seen:
            index += 1
        candidate = f"{name}_{index}"
        seen[name] = index
        seen[candidate] = 1
        out.append(candidate)
    return out


def export_file_path(tenant_id: str, user_id: str, export_id: str) -> Path:
    """用户目录下的导出文件绝对路径；export_id 非法直接抛错（防穿越）。"""
    if not _EXPORT_ID_RE.match(export_id or ""):
        raise ExportPreconditionError("Invalid export ID")
    root = user_scoped_dir(quantcustom_root() / "users", tenant_id, user_id)
    return root / "exports" / f"factor_export_{export_id}.parquet"


async def _latest_materialized(
    tenant_id: str, user_id: str, factor_id: str
) -> dict[str, Any] | None:
    """该因子最新一次成功物化的 generation 与 feature_key；无则 None。"""
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=True) as session:
        row = (
            await session.execute(
                text(
                    "SELECT generation_id,feature_key FROM alpha_factor_materializations "
                    "WHERE tenant_id=:tenant AND user_id=:user AND factor_id=:factor "
                    "AND status='materialized' ORDER BY created_at DESC LIMIT 1"
                ),
                {"tenant": tenant_id, "user": user_id, "factor": factor_id},
            )
        ).mappings().first()
    if not row:
        return None
    return {"generation_id": str(row["generation_id"]), "feature_key": str(row["feature_key"])}


def _read_generation_frame(
    tenant_id: str,
    user_id: str,
    generation_id: str,
    feature_key: str,
    *,
    start: str | None,
    end: str | None,
) -> Any | None:
    """读一个 generation 的全部分区 → (symbol, trade_date, value)；目录丢失回 None。"""
    import pandas as pd

    root = user_factor_dataset_dir(
        tenant_id, user_id, dataset="rd_mined", generation_id=generation_id
    )
    if not root.is_dir():
        return None
    frames = []
    for part in sorted(root.glob("dt=*/data.parquet")):
        day = part.parent.name[3:] if part.parent.name.startswith("dt=") else ""
        if start and day and day.replace("-", "") < start.replace("-", ""):
            continue
        if end and day and day.replace("-", "") > end.replace("-", ""):
            continue
        try:
            piece = pd.read_parquet(part, columns=["symbol", "trade_date", feature_key])
        except Exception:
            continue
        frames.append(piece)
    if not frames:
        return None
    merged = pd.concat(frames, ignore_index=True)
    merged = merged.dropna(subset=["symbol", "trade_date"])
    if start:
        merged = merged[merged["trade_date"] >= start]
    if end:
        merged = merged[merged["trade_date"] <= end]
    merged["value"] = pd.to_numeric(merged[feature_key], errors="coerce")
    return merged[["symbol", "trade_date", "value"]]


async def export_factors(
    *,
    tenant_id: str,
    user_id: str,
    factor_ids: list[str],
    factor_names: dict[str, str],
    start: str | None = None,
    end: str | None = None,
    export_name: str = "",
) -> dict[str, Any]:
    """合并导出主流程：校验 → 逐因子读盘 → 外连接 → 单文件落盘。"""
    import pandas as pd

    from backend.shared.storage_quota_ledger import write_quota_managed_local_file

    cleaned = [str(factor_id).strip() for factor_id in factor_ids or []]
    cleaned = [factor_id for factor_id in cleaned if factor_id]
    if not 1 <= len(cleaned) <= _MAX_FACTORS:
        raise ExportPreconditionError(f"一次最多导出 {_MAX_FACTORS} 个因子")
    if len(set(cleaned)) != len(cleaned):
        raise ExportPreconditionError("因子 ID 必须互不相同")

    missing: list[str] = []
    columns: list[dict[str, str]] = []
    frames: list[Any] = []
    for factor_id in cleaned:
        resolved = await _latest_materialized(tenant_id, user_id, factor_id)
        if resolved is None:
            missing.append(factor_id)
            continue
        frame = _read_generation_frame(
            tenant_id,
            user_id,
            resolved["generation_id"],
            resolved["feature_key"],
            start=start,
            end=end,
        )
        if frame is None or len(frame) == 0:
            missing.append(factor_id)
            continue
        columns.append(
            {
                "factor_id": factor_id,
                "generation_id": resolved["generation_id"],
                "feature_key": resolved["feature_key"],
            }
        )
        frames.append(frame)
    if missing:
        raise ExportPreconditionError(
            "以下因子缺少可用的物化版本，请先物化：" + "、".join(missing[:8])
        )

    display = _unique_columns(
        [
            sanitize_export_column(factor_names.get(item["factor_id"]), item["factor_id"][:12])
            for item in columns
        ]
    )
    wide = None
    for item, column, frame in zip(columns, display, frames, strict=True):
        renamed = frame.rename(columns={"value": column}).drop_duplicates(
            subset=["symbol", "trade_date"]
        )
        wide = renamed if wide is None else wide.merge(renamed, on=["symbol", "trade_date"], how="outer")
        item["column"] = column
    assert wide is not None
    wide = wide.sort_values(["symbol", "trade_date"]).reset_index(drop=True)
    if len(wide) == 0:
        raise ExportPreconditionError("所选日期区间内无交集行")

    buffer = io.BytesIO()
    wide.to_parquet(buffer, index=False, engine="pyarrow")
    content = buffer.getvalue()
    export_id = uuid.uuid4().hex
    path = write_quota_managed_local_file(
        root=quantcustom_root() / "users",
        tenant_id=tenant_id,
        user_id=user_id,
        relative_path=Path("exports") / f"factor_export_{export_id}.parquet",
        content=content,
        resource_namespace="pipeline_artifacts",
    )
    return {
        "export_id": export_id,
        "name": (export_name or "")[:128],
        "columns": columns,
        "row_count": int(len(wide)),
        "column_count": int(len(display)),
        "byte_count": int(path.stat().st_size),
        "date_min": str(wide["trade_date"].min()),
        "date_max": str(wide["trade_date"].max()),
        "symbol_count": int(wide["symbol"].nunique()),
        "download_path": f"/api/v1/alpha-agent/factors/exports/{export_id}/download",
    }
