"""data_fingerprint —— 训练可复现三件套的数据面（P1 · 设计文档 §4.5）。

写进 metadata.json，回答审计三问：

1. 这次训练**实际**吃到哪一段数据——train/valid/test 的实况区间与行数
   （从 split_frames 读，不抄 cfg 期望值；数据滞后时两者不一致，审计看的
   正是这个差）；
2. 源头数据截止到哪天——``prices_max_date``（价格/因子面，直读 QuantDB 时
   取分区 manifest 的 max_date，快路径 <0.2s）与 ``labels_max_date``
   （标签面 = 有标签行的最新交易日，天然比价格面早一个 horizon）；
3. 取数落盘面——因子源 parquet 的 ``文件数 / mtime 摘要 / 清单 sha1``。
   文件内容动辄 GB 级不做内容哈希；``相对路径:size:mtime`` 清单的 sha1
   足以察觉增删改。

纪律：**全部 best-effort**。指纹构建发生在训练收尾段，任何一项取不到就记
``None``/``error`` 降级字段，绝不允许把已训完的 run 炸成失败——G0 晋升闸门
（§4.5：缺三件套不允许晋升）靠的是字段**在不在**，而不是构建期抛错。
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

_SPLIT_NAMES = ("train", "valid", "test")
_LABEL_COLUMN = "label"
_DATE_COLUMN = "trade_date"

#: 直读源探测函数签名：(factor_source, quantdb_dir, market) -> dict | None
SourceProbe = Callable[[str, "str | None", str], "dict[str, Any] | None"]


def _iso_date(value: Any) -> str:
    """任意日期型 → ``YYYY-MM-DD`` 字符串；取不到一律空串（不抛）。"""
    if value is None:
        return ""
    try:
        ts = pd.Timestamp(value)
    except Exception:
        return ""
    if ts is pd.NaT or pd.isna(ts):
        return ""
    return str(ts.date())


def _frame_summary(frame: Any) -> dict[str, Any]:
    """单段切分帧实况：区间 / 行数 / 有标签行数（空帧返回零值，不抛）。"""
    out: dict[str, Any] = {"start": "", "end": "", "rows": 0, "label_rows": 0}
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return out
    out["rows"] = int(len(frame))
    if _DATE_COLUMN in frame.columns:
        dates = pd.to_datetime(frame[_DATE_COLUMN], errors="coerce").dropna()
        if len(dates):
            out["start"] = _iso_date(dates.min())
            out["end"] = _iso_date(dates.max())
    if _LABEL_COLUMN in frame.columns:
        out["label_rows"] = int(frame[_LABEL_COLUMN].notna().sum())
    return out


def _labels_max_date(split_frames: dict[str, pd.DataFrame]) -> str:
    """全段并集中**有标签行**的最新交易日。"""
    best = ""
    for frame in split_frames.values():
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            continue
        if _DATE_COLUMN not in frame.columns:
            continue
        dates = pd.to_datetime(frame[_DATE_COLUMN], errors="coerce")
        if _LABEL_COLUMN in frame.columns:
            dates = dates[frame[_LABEL_COLUMN].notna()]
        dates = dates.dropna()
        if len(dates):
            candidate = _iso_date(dates.max())
            if candidate and candidate > best:
                best = candidate
    return best


def _quantdb_source_probe(
    factor_source: str, quantdb_dir: str | None, market: str
) -> dict[str, Any] | None:
    """直读 QuantDB 模式：与 ``data.loading`` 同一 reader 同一 ``describe``。

    ``describe`` 的 min/max 走分区目录名快路径（<0.2s），不做全表扫描。
    这里 import 放函数内：本模块在测试环境（无 backend 路径）也要能独立导入。
    """
    from backend.services.engine.data_platform.quantdb_factor_reader import (
        QuantDBFactorReader,
    )

    reader = QuantDBFactorReader(quantdb_dir or None, market=market)
    status = reader.describe(factor_source)
    return {
        "path": str(getattr(status, "path", "") or ""),
        "min_date": _iso_date(getattr(status, "min_date", None)),
        "max_date": _iso_date(getattr(status, "max_date", None)),
        "schema_hash": str(getattr(status, "schema_hash", "") or ""),
        "files": int(getattr(status, "files", 0) or 0),
    }


def _manifest_summary(root: Any) -> dict[str, Any]:
    """parquet 落盘面摘要：文件数 / 最新 mtime / ``rel:size:mtime`` 清单 sha1。"""
    out: dict[str, Any] = {"root": str(root or ""), "files": 0, "max_mtime": "", "sha1": ""}
    if not root:
        return out
    base = Path(str(root))
    try:
        if not base.is_dir():
            out["error"] = "root_not_found"
            return out
        rows: list[str] = []
        max_mtime = 0.0
        for path in base.rglob("*.parquet"):
            try:
                stat = path.stat()
            except OSError:
                continue
            rel = path.relative_to(base).as_posix()
            rows.append(f"{rel}\0{stat.st_size}\0{int(stat.st_mtime)}")
            if stat.st_mtime > max_mtime:
                max_mtime = stat.st_mtime
        rows.sort()
        out["files"] = len(rows)
        if max_mtime:
            out["max_mtime"] = datetime.fromtimestamp(
                max_mtime, tz=timezone.utc
            ).isoformat()
        if rows:
            out["sha1"] = hashlib.sha1("\n".join(rows).encode("utf-8")).hexdigest()
    except Exception as exc:  # noqa: BLE001 —— 摘要失败只降级
        logger.warning("[Fingerprint] manifest 摘要失败: %s", exc)
        out["error"] = str(exc)
    return out


def build_data_fingerprint(
    *,
    cfg: dict[str, Any],
    split_frames: dict[str, pd.DataFrame] | None,
    market: str = "CN",
    source_probe: SourceProbe | None = None,
) -> dict[str, Any]:
    """构建 data_fingerprint（见模块 docstring）。永不抛出。"""
    computed_at = datetime.now(timezone.utc).isoformat()
    try:
        data_cfg = (cfg or {}).get("data") or {}
        factor_source = str(data_cfg.get("factor_source") or "").strip()
        quantdb_dir = str(data_cfg.get("quantdb_dir") or "").strip() or None
        market_code = str(market or "CN").upper()

        frames = split_frames if isinstance(split_frames, dict) else {}
        splits = {name: _frame_summary(frames.get(name)) for name in _SPLIT_NAMES}
        labels_max = _labels_max_date(frames)

        source: dict[str, Any] | None = None
        error = ""
        if factor_source:
            probe = source_probe or _quantdb_source_probe
            try:
                source = probe(factor_source, quantdb_dir, market_code) or None
            except Exception as exc:  # noqa: BLE001 —— 探测失败只降级
                error = f"source_probe_failed: {exc}"
                logger.warning("[Fingerprint] QuantDB 源探测失败（指纹降级）: %s", exc)

        prices_max = _iso_date(source.get("max_date")) if source else ""
        prices_basis = "quantdb_source"
        if not prices_max:
            # 降级口径：切分帧实况的最大交易日（旧 parquet 路径 / 探测失败）
            prices_max = max((s["end"] for s in splits.values() if s["end"]), default="")
            prices_basis = "split_frames"

        return {
            "factor_source": factor_source or None,
            "catalog_version": str(
                data_cfg.get("factor_catalog_version") or ""
            ).strip()
            or None,
            "schema_hash": str(data_cfg.get("factor_schema_hash") or "").strip()
            or (str(source.get("schema_hash") or "") if source else "")
            or None,
            "splits": splits,
            "prices_max_date": prices_max,
            "prices_max_date_basis": prices_basis,
            "labels_max_date": labels_max,
            "source": source,
            "manifest": _manifest_summary(source.get("path") if source else None),
            "computed_at": computed_at,
            "error": error,
        }
    except Exception as exc:  # noqa: BLE001 —— 指纹永不炸训练
        logger.warning("[Fingerprint] 构建失败（指纹降级为空壳）: %s", exc)
        return {"error": f"fingerprint_failed: {exc}", "computed_at": computed_at}
