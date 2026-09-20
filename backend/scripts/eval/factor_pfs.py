"""因子 PFS 侧车：给评估中心的「质量闸门」维供数（设计 §2.1）。

**公式不在本模块**：PFS（扰动保真度）唯一实现是训练侧
``docker/training/data/factor_quality.py``，本模块经 ``backend.shared.factor_quality``
按路径加载后调用。这里只做三件事：

1. **取数**：`6_ml_datasets/<dataset>` 是 ``dt=YYYYMMDD/data.parquet`` 分区表（实测
   alpha_library 431 列 / 5568 行/日 / 2000+ 分区），所以必须
   ① 只读抽样出来的日子、② 只读要评的那几列——整表读一次几十 GB；
2. **抽样**：与训练侧 ``_sample_positions`` **同一条规则**（等距 + 固定 seed 由调用方给），
   否则同一因子在训练期与评估期会得出两个 PFS；
3. **缓存**：结果写 ``<dataset>/report/pfs_cache.json``，带 ``公式版本 + 面板日签名``，
   面板新增交易日或公式改动即失效重算（不吃旧数）。

读不到就是读不到：分区缺失、训练侧模块未挂载、有效日不足，一律返回 **note 说明原因**，
调用方据此把「质量闸门」维标成缺省——**不编 0 分**（0 分等于说「验过了，很差」）。
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import numpy as np

from backend.shared.eval_scoring import DimensionScore, score_from_thresholds

logger = logging.getLogger(__name__)

PFS_CACHE_NAME = "pfs_cache.json"
# 公式/抽样口径变更必须 +1：旧缓存会被整体作废（宁可重算，不可混口径）
PFS_FORMULA_VERSION = 1
# 设计 §2.1「PFS 不达标」为红线；文档未给数值，按既有换算表的 40 分锚点取 0.5
PFS_RED_LINE_MIN = 0.5
PFS_MAX_DAYS = 120  # 与训练侧 MAX_PFS_DAYS 同值（抽样天数上限）
QUALITY_GATE_WEIGHT = 20.0

_PFS_THRESHOLDS = [(0.0, 0.0), (0.5, 40.0), (0.7, 70.0), (0.9, 100.0)]
_DH_THRESHOLDS = [(0.0, 0.0), (0.01, 40.0), (0.05, 80.0), (0.1, 100.0)]


# ── 分区与抽样 ────────────────────────────────────────────────────────


def panel_days(root: Path | str) -> list[str]:
    """数据集分区日（``dt=YYYYMMDD`` 目录名，升序）；无分区返回空表。"""
    base = Path(root)
    if not base.is_dir():
        return []
    return sorted(p.name[3:] for p in base.glob("dt=*") if p.is_dir())


def sample_day_dirs(days: list[str], *, max_days: int = PFS_MAX_DAYS) -> list[str]:
    """等距抽样的分区日（**与训练侧 ``_sample_positions`` 同一规则**，保持时间序）。

    按位置抽样而非按日期值：pandas 3 的 datetime 索引 tolist() 是整数纳秒，
    按值建集合做成员测试会跨版本静默失配（训练侧注释已记此坑）。
    """
    n = len(days)
    if max_days <= 0 or n <= max_days:
        return list(days)
    idx = np.linspace(0, n - 1, num=int(max_days)).round().astype(int)
    return [days[i] for i in sorted(set(idx.tolist()))]


def pfs_cache_signature(*, dataset: str, days: list[str]) -> str:
    """缓存签名：公式版本 + 数据集 + 抽样日序列（面板一变即失效）。"""
    payload = json.dumps(
        {"v": PFS_FORMULA_VERSION, "dataset": dataset, "days": list(days)},
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ── 取数（只读抽样日 + 列投影） ────────────────────────────────────────


def _available_columns(parquet_path: Path) -> set[str]:
    import pyarrow.parquet as pq

    return set(pq.ParquetFile(str(parquet_path)).schema_arrow.names)


def load_factor_panel(
    root: Path | str, factors: list[str], *, days: list[str]
) -> Any:
    """抽样日的因子面板 → DataFrame(``trade_date`` + 实际存在的因子列)。

    面板里没有的因子**不造列、不填 0**（缺列与零值在 PFS 里是两回事）。
    """
    import pandas as pd

    base = Path(root)
    present = [d for d in days if (base / f"dt={d}" / "data.parquet").is_file()]
    if not present:
        raise FileNotFoundError(f"因子面板无可用分区（dt=* 下没有 data.parquet）：{base}")

    cols = _available_columns(base / f"dt={present[0]}" / "data.parquet")
    wanted = [f for f in factors if f in cols]
    if not wanted:
        raise FileNotFoundError(
            f"因子面板里找不到待评因子列（首个分区 {len(cols)} 列）：{factors[:5]}"
        )

    import pyarrow.parquet as pq

    frames = []
    for day in present:
        table = pq.read_table(base / f"dt={day}" / "data.parquet", columns=wanted)
        chunk = table.to_pandas()
        chunk["trade_date"] = day
        frames.append(chunk)
    panel = pd.concat(frames, ignore_index=True)
    return panel[["trade_date", *wanted]]


# ── 侧车缓存 ──────────────────────────────────────────────────────────


def read_pfs_cache(report_dir: Path | str, *, signature: str) -> dict[str, Any] | None:
    """读侧车；版本不符 / 签名不符 / 文件损坏 → None（都要重算，且留日志）。"""
    path = Path(report_dir) / PFS_CACHE_NAME
    if not path.is_file():
        return None
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("PFS 缓存不可读（按未命中处理）%s: %s", path, exc)
        return None
    if not isinstance(body, dict):
        return None
    if int(body.get("version") or 0) != PFS_FORMULA_VERSION:
        logger.info("PFS 缓存版本过期（%s≠%s），重算", body.get("version"), PFS_FORMULA_VERSION)
        return None
    if body.get("signature") != signature:
        logger.info("PFS 缓存签名不符（面板已变），重算")
        return None
    pfs = body.get("pfs")
    return pfs if isinstance(pfs, dict) else None


def write_pfs_cache(
    report_dir: Path | str, values: dict[str, Any], *, signature: str
) -> None:
    """原子写侧车；写不进去只影响性能，但必须留日志（不静默）。"""
    path = Path(report_dir) / PFS_CACHE_NAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(
                {"version": PFS_FORMULA_VERSION, "signature": signature, "pfs": values},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError as exc:
        logger.warning("PFS 缓存写入失败（不影响评分）%s: %s", path, exc)


# ── 评分接线（质量闸门维） ────────────────────────────────────────────


def _insufficient_quality_gate(note: str) -> DimensionScore:
    return DimensionScore(
        "quality_gate",
        "质量闸门",
        QUALITY_GATE_WEIGHT,
        None,
        False,
        {"insufficient": True, "note": note},
    )


def quality_gate_dim(
    record: dict[str, Any] | None, *, note: str | None = None
) -> DimensionScore:
    """质量闸门维：PFS（扰动保真）/ DH（多样性增益）→ 分数 + 「PFS 不达标」红线。

    ``record`` 为 ``compute_pfs`` 的单因子结果（``{"pfs","pfs_gauss","pfs_t","n_days"}``）
    或带 ``dh`` 的字典；为 None 或缺值时**如实缺省**，不按 0 分算。
    """
    if not record:
        return _insufficient_quality_gate(note or "PFS/DH 未算（无可评数据）")

    pfs_raw = record.get("pfs")
    dh = record.get("dh") if "dh" in record else record.get("diversity_gain")
    pfs = None if pfs_raw is None else float(pfs_raw)
    if pfs is None and dh is None:
        return _insufficient_quality_gate(
            note
            or (
                f"PFS/DH 均不可用：仅 {record.get('n_days')} 个有效交易日，"
                "不足训练侧 MIN_DAYS，扰动保真无法判定（不按 0 分算）"
            )
        )

    parts: list[float] = []
    if pfs is not None:
        parts.append(score_from_thresholds(pfs, _PFS_THRESHOLDS) or 0.0)
    if dh is not None:
        parts.append(score_from_thresholds(float(dh), _DH_THRESHOLDS) or 0.0)
    red = bool(pfs is not None and pfs < PFS_RED_LINE_MIN)
    detail: dict[str, Any] = {
        "pfs": record if pfs is not None else None,
        "dh": dh,
        "red_line_min": PFS_RED_LINE_MIN,
    }
    if red:
        detail["red_line"] = (
            f"PFS 不达标（{pfs:.3f} < {PFS_RED_LINE_MIN}）："
            "因子排名经扰动即失稳，实盘换个数据源/复权口径就换一批票"
        )
    if record.get("n_days") is not None:
        detail["n_days"] = record.get("n_days")
    return DimensionScore(
        "quality_gate",
        "质量闸门",
        QUALITY_GATE_WEIGHT,
        round(float(np.mean(parts)), 2),
        red,
        detail,
    )


# ── 编排：抽样 → 取数 → 调训练侧实现 → 缓存 ────────────────────────────


def compute_pfs_for(
    dataset: str,
    factors: list[str],
    *,
    root: Path | str | None = None,
    max_days: int = PFS_MAX_DAYS,
    refresh: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """→ (``{factor: pfs 记录}``, 证据/降级说明)。缓存命中不读面板。"""
    from backend.services.engine.factor_report.datasets import dataset_dir
    from backend.shared.factor_quality import load_factor_quality

    base = Path(root) if root is not None else dataset_dir(dataset)
    report_dir = base / "report"
    evidence: dict[str, Any] = {
        "dataset": dataset,
        "root": str(base),
        "formula_version": PFS_FORMULA_VERSION,
        "max_days": max_days,
        "factors_requested": len(factors),
    }
    days = panel_days(base)
    if not days:
        evidence["note"] = f"因子面板无 dt=* 分区：{base}"
        return {}, evidence
    picked = sample_day_dirs(days, max_days=max_days)
    signature = pfs_cache_signature(dataset=dataset, days=picked)
    evidence["n_days_total"] = len(days)
    evidence["n_days_sampled"] = len(picked)
    evidence["signature"] = signature

    wanted = [f for f in factors if f]
    cached = None if refresh else read_pfs_cache(report_dir, signature=signature)
    have = {f: cached[f] for f in wanted if cached and f in cached}
    missing = [f for f in wanted if f not in have]
    if not missing:
        evidence["cache"] = "hit"
        evidence["n_factors"] = len(have)
        return have, evidence
    evidence["cache"] = "miss" if cached is None else "partial"

    module = load_factor_quality()
    if module is None:
        evidence["note"] = "训练侧 factor_quality 未挂载（docker/training），PFS 无法计算"
        return have, evidence
    try:
        panel = load_factor_panel(base, missing, days=picked)
    except FileNotFoundError as exc:
        evidence["note"] = str(exc)
        return have, evidence

    computed = module.compute_pfs(panel, [c for c in panel.columns if c != "trade_date"])
    merged = {**have, **{str(k): v for k, v in computed.items()}}
    write_pfs_cache(report_dir, merged, signature=signature)
    evidence["n_factors"] = len(merged)
    evidence["n_rows"] = int(panel.shape[0])
    return merged, evidence
