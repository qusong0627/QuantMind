#!/usr/bin/env python3
"""财务因子 PIT 审计探针（A3）。

对 V5 宽表里**可映射**的财务因子（fun_roe / fun_np_growth）做公告日对齐审计：
- ``pit_asof``：as-of 日之前**已公告**的最新报告值（公告日 + 1 交易日近似）；
- ``report_asof``：as-of 日之前**报告期**最新的值（忽略公告日，等于偷看未来）；
- 分别与 as-of 日的 V5 因子做截面 Spearman 相关。

若 V5 因子与 report_asof 的相关性显著高于与 pit_asof 的相关性（默认 Δ>0.10），
标记 ``pit_pending``——它更像按报告期对齐（含前视），而不是按公告日对齐。

这是**季度快照探针**，不是全量逐日审计；结论标注为筛选建议，需人工复核。
输出默认只打印；``--write`` 落 ``factor_research/pit_audit.json``。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

PROJECT_ROOT = Path(__file__).resolve().parents[2]
log = logging.getLogger("audit_factor_pit")

V5_REL = "6_ml_datasets/factor_dictionary"
#: V5 列 ↔ PIT 面板列（只审计有直接映射的；pe/pb/ep/bp 等需要额外公式，暂不猜）
MAPPINGS = {
    "fun_roe": "roe",
    "fun_np_growth": "profit_growth",
}


@dataclass
class FactorAudit:
    v5_column: str
    pit_column: str
    snapshots: int = 0
    corr_pit_sum: float = 0.0
    corr_report_sum: float = 0.0
    valid_snapshots: int = 0
    suspicious: list[str] = field(default_factory=list)

    @property
    def avg_corr_pit(self) -> float | None:
        return (
            round(self.corr_pit_sum / self.valid_snapshots, 4)
            if self.valid_snapshots
            else None
        )

    @property
    def avg_corr_report(self) -> float | None:
        return (
            round(self.corr_report_sum / self.valid_snapshots, 4)
            if self.valid_snapshots
            else None
        )

    def status(self, threshold: float) -> str:
        if self.valid_snapshots < 2:
            return "insufficient"
        delta = (self.avg_corr_report or 0.0) - (self.avg_corr_pit or 0.0)
        return "pit_pending" if delta > threshold else "pit_ok"

    def to_dict(self, threshold: float) -> dict:
        return {
            "v5_column": self.v5_column,
            "pit_column": self.pit_column,
            "snapshots": self.snapshots,
            "valid_snapshots": self.valid_snapshots,
            "avg_corr_pit": self.avg_corr_pit,
            "avg_corr_report": self.avg_corr_report,
            "delta": (
                round((self.avg_corr_report or 0.0) - (self.avg_corr_pit or 0.0), 4)
                if self.valid_snapshots
                else None
            ),
            "status": self.status(threshold),
            "suspicious_snapshots": self.suspicious,
        }


def _latest_by(
    symbol_frame: pd.DataFrame, date_col: str, value_cols: list[str], asof: pd.Timestamp
) -> pd.DataFrame:
    """每个 symbol 取 date_col <= asof 的最后一行（输入已按日期升序）。"""
    subset = symbol_frame[symbol_frame[date_col] <= asof]
    if subset.empty:
        return pd.DataFrame(columns=["symbol", *value_cols])
    return (
        subset.sort_values(date_col)
        .groupby("symbol", as_index=False)
        .tail(1)[["symbol", *value_cols]]
    )


def _spearman(left: pd.Series, right: pd.Series) -> float | None:
    valid = left.notna() & right.notna()
    if int(valid.sum()) < 10:
        return None
    try:
        from scipy.stats import spearmanr

        rho, _ = spearmanr(left[valid], right[valid])
    except Exception:  # noqa: BLE001 - 无 scipy 时用秩相关兜底
        rho = left[valid].rank().corr(right[valid].rank())
    return float(rho) if pd.notna(rho) else None


def audit_snapshots(
    pit: pd.DataFrame,
    snapshots: list[tuple[pd.Timestamp, pd.DataFrame]],
    *,
    mappings: dict[str, str] | None = None,
    threshold: float = 0.10,
) -> list[dict]:
    """纯函数审计：pit 为带 symbol/report_date/ann_date + 指标列的窄表。

    snapshots = [(asof, v5_frame)]，v5_frame 需含 symbol + V5 因子列。
    """
    mappings = mappings or MAPPINGS
    pit = pit.dropna(subset=["symbol", "report_date", "ann_date"]).copy()
    pit["report_date"] = pd.to_datetime(pit["report_date"])
    pit["ann_date"] = pd.to_datetime(pit["ann_date"])

    audits = {
        column: FactorAudit(v5_column=column, pit_column=pit_col)
        for column, pit_col in mappings.items()
    }

    for asof, frame in snapshots:
        if frame is None or frame.empty:
            continue
        frame = frame.copy()
        frame["symbol"] = frame["symbol"].astype(str).str.upper()
        for v5_col, pit_col in mappings.items():
            audit = audits[v5_col]
            audit.snapshots += 1
            if v5_col not in frame.columns or pit_col not in pit.columns:
                continue
            pit_asof = _latest_by(pit, "ann_date", [pit_col], asof).rename(
                columns={pit_col: "_pit"}
            )
            report_asof = _latest_by(pit, "report_date", [pit_col], asof).rename(
                columns={pit_col: "_report"}
            )
            merged = (
                frame[["symbol", v5_col]]
                .merge(pit_asof, on="symbol", how="inner")
                .merge(report_asof, on="symbol", how="inner")
            )
            if len(merged) < 10:
                continue
            corr_pit = _spearman(merged[v5_col], merged["_pit"])
            corr_report = _spearman(merged[v5_col], merged["_report"])
            if corr_pit is None or corr_report is None:
                continue
            audit.valid_snapshots += 1
            audit.corr_pit_sum += corr_pit
            audit.corr_report_sum += corr_report
            if corr_report - corr_pit > threshold:
                audit.suspicious.append(asof.strftime("%Y-%m-%d"))

    return [a.to_dict(threshold) for a in audits.values()]


def _load_v5_snapshot(root: Path, asof: pd.Timestamp) -> pd.DataFrame | None:
    base = root / V5_REL
    if not base.is_dir():
        return None
    target = asof.strftime("%Y%m%d")
    parts = sorted(
        p.name[3:]
        for p in base.glob("dt=*")
        if p.is_dir() and (p / "data.parquet").is_file() and p.name[3:] <= target
    )
    if not parts:
        return None
    path = base / f"dt={parts[-1]}" / "data.parquet"
    columns = ["symbol", *MAPPINGS.keys()]
    try:
        available = set(pq.ParquetFile(path).schema_arrow.names)
        use = [c for c in columns if c in available]
        if "symbol" not in use:
            return None
        return pd.read_parquet(path, columns=use)
    except Exception as exc:  # noqa: BLE001
        log.warning("读 V5 快照失败 %s: %s", path, exc)
        return None


def build_report(
    root: str | Path,
    *,
    start: date,
    end: date,
    threshold: float = 0.10,
) -> dict:
    from backend.scripts.financial_pit_panel import (
        build_quarterly,
        load_pit,
        quarter_asofs,
    )

    root = Path(root)
    pit = load_pit()
    asofs = [d for d in quarter_asofs() if start <= d.date() <= end]
    snapshots: list[tuple[pd.Timestamp, pd.DataFrame]] = []
    for asof in asofs:
        frame = _load_v5_snapshot(root, asof)
        if frame is not None and not frame.empty:
            snapshots.append((asof, frame))
    # build_quarterly 的 as-of 面板用于报告/人工核对；审计本身用纯函数逐快照算
    quarterly = (
        build_quarterly(pit, pd.DatetimeIndex(asofs)) if asofs else pd.DataFrame()
    )
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "threshold": threshold,
        "asofs": len(asofs),
        "snapshots_with_v5": len(snapshots),
        "quarterly_rows": int(len(quarterly)),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "factors": audit_snapshots(pit, snapshots, threshold=threshold),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="财务因子 PIT 审计（季度快照）")
    parser.add_argument(
        "--root",
        default=os.getenv(
            "QM_QUANTDB_DATA_DIR", str(PROJECT_ROOT / "data" / "quantdb")
        ),
    )
    parser.add_argument("--start", default="2024-01-01")
    parser.add_argument("--end", default="2025-12-31")
    parser.add_argument("--threshold", type=float, default=0.10)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )

    report = build_report(
        args.root,
        start=date.fromisoformat(args.start),
        end=date.fromisoformat(args.end),
        threshold=args.threshold,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.write:
        out = Path(args.root) / "factor_research" / "pit_audit.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, out)
        log.info("报告已写入 %s", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
