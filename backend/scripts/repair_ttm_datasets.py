#!/usr/bin/env python3
"""TTM 损坏的多数据集修复（2026-09-17 夜扩面）。

背景：`repair_valuation_ttm.py` 只修 `5_technical_derived/valuation`；实测同一批坏值
（上游中报回归）已复制到 **`6_ml_datasets/features_daily`（net_profit_ttm/revenue_ttm/
pe_ttm/ps_ttm）与 `l1_factors`/`l1_l2_factors`（fun_pe/fun_ep）**——而训练/批量推理/
实时基线读的正是这三集（生产实时模型 48 特征含 fun_pe/fun_pb/fun_mv_rank）。
且各集损坏窗口不同（features_daily 的平安银行 2026-07 底即坏，valuation 已修过一批）。

修复口径（单一事实源 = income 重算）：
    np_ttm/rev_ttm = 最近连续 4 个单季值求和（PIT：m_anntime ≤ 分区日）——复用
                     repair_valuation_ttm 的重算核（唯一实现，不复制）
    features_daily : mv 取**本分区自带 total_mv（元）**；pe_ttm = mv/np_ttm；ps_ttm = mv/rev_ttm；
                     net_profit_ttm/revenue_ttm 亦按重算值修复
    l1 / l1_l2     : 本集 fun_total_mv 与 total_mv **非恒定比例（单位/口径不明，不信）** →
                     mv 取 valuation 同分区 total_mv（元）：fun_pe = mv/np_ttm；fun_ep = np_ttm/mv
逐格替换：仅 |源 − 重算| / |重算| > 25% 才替换（健康值原样保留；缺季报/新股保留原值）。
备份 `.bak-ttmrepair` + 原子写（tmp → replace），与 valuation 脚本同纪律。

用法（仓库根）:
    python3 backend/scripts/repair_ttm_datasets.py --survey-from 20260701     # 体检（只读）
    python3 backend/scripts/repair_ttm_datasets.py --apply-from <首个坏日>     # 修复（含备份）
    python3 backend/scripts/repair_ttm_datasets.py --validate 20260828        # 健康日（应≈0 格）
    python3 backend/scripts/repair_ttm_datasets.py --survey-from X --datasets features_daily
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.scripts.repair_valuation_ttm import (  # noqa: E402 — 重算核唯一实现
    _CELL_REL,
    _load_sources,
    _scan_symbols,
    _ttm_tables,
)
from backend.shared.quantdb_paths import resolve_quantdb_dir  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("repair-ttm-datasets")

VAL_DIR = ("5_technical_derived", "valuation")

# 数据集规格：mode 决定 mv 来源（self=本分区 total_mv；val_mv=valuation 分区 total_mv）
DATASETS: dict[str, dict] = {
    "features_daily": {
        "dir": ("6_ml_datasets", "features_daily"),
        "mode": "self",
        "cols": ("net_profit_ttm", "revenue_ttm", "pe_ttm", "ps_ttm"),
    },
    "l1_factors": {
        "dir": ("6_ml_datasets", "l1_factors"),
        "mode": "val_mv",
        "cols": ("fun_pe", "fun_ep"),
    },
    "l1_l2_factors": {
        "dir": ("6_ml_datasets", "l1_l2_factors"),
        "mode": "val_mv",
        "cols": ("fun_pe", "fun_ep"),
    },
}


def _schema(path: Path) -> set[str]:
    import pyarrow.parquet as pq

    try:
        return set(pq.ParquetFile(path).schema_arrow.names)
    except Exception:  # noqa: BLE001
        return set()


def _data_file(spec_dir: tuple[str, ...], dt: str) -> Path:
    return resolve_quantdb_dir().joinpath(*spec_dir, f"dt={dt}", "data.parquet")


def _partitions(spec_dir: tuple[str, ...], start: str | None) -> list[tuple[str, Path]]:
    root = resolve_quantdb_dir().joinpath(*spec_dir)
    out = []
    for p in sorted(root.glob("dt=*")):
        f = p / "data.parquet"
        dt = p.name[3:]
        if f.exists() and dt.isdigit() and (start is None or dt >= start):
            out.append((dt, f))
    return out


def _build_ttm(dt: str, symbols: list[str], inc: pd.DataFrame, spec: dict) -> pd.DataFrame:
    """重算表（含 mv）：symbol / np_ttm / rev_ttm / total_mv。"""
    ttm = _ttm_tables(pd.Timestamp(dt), symbols, inc)[["symbol", "np_ttm", "rev_ttm"]]
    if spec["mode"] == "self":
        mv_f = _data_file(spec["dir"], dt)
    else:
        mv_f = _data_file(VAL_DIR, dt)
    if mv_f.exists() and "total_mv" in _schema(mv_f):
        mv = pd.read_parquet(mv_f, columns=["symbol", "total_mv"])
        ttm = ttm.merge(mv, on="symbol", how="left")
    return ttm


def _fix_frame(part: pd.DataFrame, ttm: pd.DataFrame, spec: dict) -> tuple[pd.DataFrame, dict[str, int]]:
    """逐格修复：返回 (修复后的 part[spec.cols] 帧, 各列替换数)。"""
    m = part.merge(ttm, on="symbol", how="left", suffixes=("", "_ttm"))
    nan_col = pd.Series(np.nan, index=m.index)
    np_ttm = pd.to_numeric(m["np_ttm"], errors="coerce")
    rev_ttm = pd.to_numeric(m["rev_ttm"], errors="coerce")
    mv = pd.to_numeric(m["total_mv"], errors="coerce")
    calc_map = {
        "net_profit_ttm": np_ttm,
        "revenue_ttm": rev_ttm,
        "pe_ttm": mv / np_ttm.replace(0, np.nan),
        "ps_ttm": mv / rev_ttm.replace(0, np.nan),
        "fun_pe": mv / np_ttm.replace(0, np.nan),
        "fun_ep": np_ttm / mv.replace(0, np.nan),
    }
    fixed = pd.DataFrame({"symbol": m["symbol"]})
    counts: dict[str, int] = {}
    for col in spec["cols"]:
        calc = pd.to_numeric(calc_map[col], errors="coerce")
        old = pd.to_numeric(m.get(col, nan_col), errors="coerce")
        ok = calc.notna() & np.isfinite(calc) & (calc != 0)
        bad = ok & ~np.isclose(old, calc, rtol=_CELL_REL, equal_nan=False)
        fixed[col] = np.where(bad, calc, old)
        counts[col] = int(bad.sum())
    return fixed, counts


# ═══════════════════════════════════════════════════════════════════════


def _run(ds: str, start: str, end: str | None, *, apply: bool) -> dict[str, int]:
    spec = DATASETS[ds]
    parts = [(dt, f) for dt, f in _partitions(spec["dir"], start) if end is None or dt <= end]
    if not parts:
        log.warning("[%s] 无匹配分区", ds)
        return {}
    syms = _scan_symbols(parts)
    inc = _load_sources(syms)
    total: dict[str, int] = {}
    for dt, f in parts:
        cols = _schema(f)
        use_cols = [c for c in spec["cols"] if c in cols]
        if not use_cols:
            continue
        part = pd.read_parquet(f)[["symbol", *use_cols]]
        ttm = _build_ttm(dt, syms, inc, spec)
        fixed, counts = _fix_frame(part, ttm, spec)
        total_n = sum(counts.values())
        for k, v in counts.items():
            total[k] = total.get(k, 0) + v
        if not apply:
            log.info("[%s] %s 需替换 %s（合计 %d）", ds, dt, counts, total_n)
            continue
        if total_n == 0:
            continue
        full = pd.read_parquet(f)
        full = full.drop(columns=[c for c in use_cols if c in full.columns])
        full = full.merge(fixed, on="symbol", how="left")
        bak = f.with_name(f.name + ".bak-ttmrepair")
        if not bak.exists():
            bak.write_bytes(f.read_bytes())
        tmp = f.with_name(f.name + ".tmp")
        full.to_parquet(tmp, index=False)
        tmp.replace(f)
        log.info("[%s] %s 已修 %s（合计 %d）", ds, dt, counts, total_n)
    return total


def _validate(ds: str, dt: str) -> int:
    """健康日校验：>25% 偏差格数应 ≈0（公式与源口径一致的正面证据）。"""
    spec = DATASETS[ds]
    f = _data_file(spec["dir"], dt)
    if not f.exists():
        log.error("[%s] 分区不存在: %s", ds, f)
        return 2
    cols = _schema(f)
    use_cols = [c for c in spec["cols"] if c in cols]
    part = pd.read_parquet(f)[["symbol", *use_cols]]
    syms = part["symbol"].astype(str).tolist()
    inc = _load_sources(syms)
    ttm = _build_ttm(dt, syms, inc, spec)
    _fixed, counts = _fix_frame(part, ttm, spec)
    n = sum(counts.values())
    log.info("validate [%s] %s: 需替换 %s（合计 %d，应≈0）", ds, dt, counts, n)
    return 0 if n <= max(5, int(0.005 * len(part) * len(use_cols))) else 2


def main() -> int:
    ap = argparse.ArgumentParser(description="TTM 多数据集修复（features_daily/l1/l1_l2）")
    ap.add_argument("--survey-from", type=str, default="", help="体检起点（只读）")
    ap.add_argument("--apply-from", type=str, default="", help="修复起点")
    ap.add_argument("--end", type=str, default="", help="终点（含），缺省最新")
    ap.add_argument("--validate", type=str, default="", help="健康日校验（如 20260828）")
    ap.add_argument("--datasets", type=str, default=",".join(DATASETS), help="逗号分隔数据集名")
    args = ap.parse_args()
    names = [s.strip() for s in args.datasets.split(",") if s.strip() in DATASETS]

    if args.validate:
        return max(_validate(ds, args.validate) for ds in names)
    if args.survey_from:
        for ds in names:
            total = _run(ds, args.survey_from, args.end or None, apply=False)
            log.info("[%s] 体检合计（窗内需替换格数）: %s", ds, total)
        return 0
    if args.apply_from:
        for ds in names:
            total = _run(ds, args.apply_from, args.end or None, apply=True)
            log.info("[%s] 修复合计: %s", ds, total)
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
