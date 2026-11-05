#!/usr/bin/env python3
"""退市股因子覆盖探针（A2，只读）。

回答两个问题：
1. 各因子库在退市股的**在市区间**里到底有没有行（按 symbol 抽样检查分区）；
2. 库里的列有多少是「OHLCV 可派生」、多少必须依赖上游/基本面/微观结构。

**只读**：不写因子数据；只有带 ``--write`` 时才落
``<root>/factor_research/delisted_coverage.json`` 报告。

用法：
  python backend/scripts/delisted_factor_coverage.py --sample 20 --write
  python backend/scripts/delisted_factor_coverage.py --libs factor_defs,cand_factors
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

PROJECT_ROOT = Path(__file__).resolve().parents[2]
log = logging.getLogger("delisted_factor_coverage")

DEFAULT_RAW_REL = "factor_research/selection_v2_20261021/delisted_raw"
DEFAULT_LIBS = (
    "factor_defs",
    "cand_factors",
    "factor1300",
    "gap_mined",
    "l1_batch1_factors",
    "rd_mined",
    "alpha360",
    "jq110",
    "tdxgs",
    "l1_factors",
    "l2_factors",
    "l1_l2_factors",
    "factor_dictionary",
)

#: 简单 OHLCV 派生族（补齐成本低）；alpha101/gtja/a158 属于「可派生但要复算公式」。
OHLCV_PREFIXES = (
    "mom_",
    "ret_",
    "vol_",
    "liq_",
    "turn_",
    "amt_",
    "mfi_",
    "obv_",
    "tech_",
    "vwap",
    "close_",
    "open_",
    "high_",
    "low_",
    "range_",
    "gap_",
    "parkinson_",
    "gk_",
    "rv_",
)
OHLCV_EXACT = {"open", "high", "low", "close", "volume", "amount", "vwap", "turnover"}
NON_DERIVABLE_PREFIXES = (
    "fun_",
    "ind_",
    "chip_",
    "concept_",
    "micro_",
    "flow_",
    "ca_",
    "sb_",
)
FORMULA_PREFIXES = ("a101_", "gtja_", "a158_", "alpha101_", "alpha191_", "alpha158_")

_SYMBOL_FILE_RE = re.compile(r"^(\d{6}\.(?:SZ|SH|BJ))_(qfq|raw)\.parquet$", re.I)


@dataclass
class SymbolSpan:
    symbol: str
    first: date
    last: date


@dataclass
class LibraryReport:
    library: str
    partitions: int = 0
    latest: str | None = None
    sampled: list[str] = field(default_factory=list)
    hits: list[str] = field(default_factory=list)
    misses: list[str] = field(default_factory=list)
    columns: dict[str, int] = field(default_factory=dict)
    error: str | None = None

    @property
    def coverage(self) -> float | None:
        total = len(self.sampled)
        return round(len(self.hits) / total, 4) if total else None

    def to_dict(self) -> dict:
        return {
            "library": self.library,
            "partitions": self.partitions,
            "latest": self.latest,
            "sampled": self.sampled,
            "hits": self.hits,
            "misses": self.misses,
            "coverage": self.coverage,
            "columns": self.columns,
            "error": self.error,
        }


def _parse_day(value) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return pd.Timestamp(value).date()
    except Exception:  # noqa: BLE001
        return None


def load_delisted_spans(raw_dir: str | Path) -> dict[str, SymbolSpan]:
    """从退市原始缓存（{symbol}_qfq.parquet）读 symbol 的在市区间。"""
    raw_dir = Path(raw_dir)
    spans: dict[str, SymbolSpan] = {}
    if not raw_dir.is_dir():
        return spans
    for path in sorted(raw_dir.glob("*_qfq.parquet")):
        match = _SYMBOL_FILE_RE.match(path.name)
        if not match:
            continue
        symbol = match.group(1).upper()
        try:
            frame = pd.read_parquet(path, columns=["time"])
        except Exception as exc:  # noqa: BLE001
            log.warning("读退市缓存失败 %s: %s", path.name, exc)
            continue
        if frame.empty or "time" not in frame.columns:
            continue
        days = pd.to_datetime(frame["time"], errors="coerce").dropna()
        if days.empty:
            continue
        spans[symbol] = SymbolSpan(
            symbol=symbol,
            first=days.min().date(),
            last=days.max().date(),
        )
    return spans


def _partition_dates(lib_root: Path) -> list[str]:
    if not lib_root.is_dir():
        return []
    out = []
    for part in lib_root.glob("dt=*"):
        if part.is_dir() and (part / "data.parquet").is_file():
            digits = part.name[3:]
            if len(digits) == 8 and digits.isdigit():
                out.append(digits)
    return sorted(set(out))


def _partition_path(lib_root: Path, dt: str) -> Path:
    return lib_root / f"dt={dt}" / "data.parquet"


def _symbol_forms(symbol: str) -> set[str]:
    s = symbol.strip().upper()
    if "." in s:
        bare = s.split(".", 1)[0]
        suffix = s
        prefix = s.split(".", 1)[1] + bare
        return {s, bare, suffix, prefix}
    return {s}


def _partition_symbols(path: Path) -> set[str]:
    try:
        frame = pd.read_parquet(path, columns=["symbol"])
    except Exception:  # noqa: BLE001
        try:
            frame = pq.read_table(path, columns=["symbol"]).to_pandas()
        except Exception:  # noqa: BLE001
            return set()
    out: set[str] = set()
    for value in frame["symbol"].astype(str):
        out |= _symbol_forms(value)
    return out


def _classify_columns(columns: list[str]) -> dict[str, int]:
    counts = {"ohlcv_derived": 0, "formula_derived": 0, "non_derivable": 0, "other": 0}
    for column in columns:
        name = str(column)
        if name in OHLCV_EXACT or name.startswith(OHLCV_PREFIXES):
            counts["ohlcv_derived"] += 1
        elif name.startswith(FORMULA_PREFIXES):
            counts["formula_derived"] += 1
        elif name.startswith(NON_DERIVABLE_PREFIXES):
            counts["non_derivable"] += 1
        else:
            counts["other"] += 1
    return counts


def _sample_dates(span: SymbolSpan, parts: list[str], k: int = 3) -> list[str]:
    """在 symbol 的在市区间里挑 k 个检查日，映射到库中最近的已有分区。"""
    if not parts:
        return []
    first_day = span.first
    mid_day = first_day + (span.last - first_day) / 2
    candidates = [first_day, mid_day, span.last]
    targets = []
    for cand in candidates:
        target = cand.strftime("%Y%m%d")
        eligible = [p for p in parts if p <= target]
        if eligible:
            targets.append(eligible[-1])
    return sorted(set(targets))[:k]


def check_library(
    root: Path,
    library: str,
    spans: dict[str, SymbolSpan],
    *,
    sample: int = 20,
    seed: int = 0,
) -> LibraryReport:
    lib_root = root / "6_ml_datasets" / library
    report = LibraryReport(library=library)
    parts = _partition_dates(lib_root)
    report.partitions = len(parts)
    report.latest = parts[-1] if parts else None
    if not parts:
        report.error = "no_dt_partitions"
        return report

    latest_columns = []
    try:
        latest_columns = [
            str(c)
            for c in pq.ParquetFile(
                _partition_path(lib_root, parts[-1])
            ).schema_arrow.names
        ]
    except Exception as exc:  # noqa: BLE001
        report.error = f"schema_unreadable: {exc}"
    report.columns = _classify_columns(latest_columns)

    symbols = sorted(spans)
    if sample > 0 and len(symbols) > sample:
        # 确定性抽样：等距取，避免每次报告不同
        step = len(symbols) / sample
        symbols = [symbols[int(i * step)] for i in range(sample)]
    report.sampled = symbols

    for symbol in symbols:
        span = spans[symbol]
        dates = _sample_dates(span, parts)
        hit = False
        for dt in dates:
            path = _partition_path(lib_root, dt)
            if not path.is_file():
                continue
            if _symbol_forms(symbol) & _partition_symbols(path):
                hit = True
                break
        (report.hits if hit else report.misses).append(symbol)
    return report


def build_report(
    root: str | Path,
    *,
    raw_dir: str | Path | None = None,
    libraries: list[str] | None = None,
    sample: int = 20,
) -> dict:
    root = Path(root)
    raw = Path(raw_dir) if raw_dir is not None else root / DEFAULT_RAW_REL
    spans = load_delisted_spans(raw)
    libs = list(libraries or DEFAULT_LIBS)
    reports = [check_library(root, lib, spans, sample=sample) for lib in libs]
    return {
        "root": str(root),
        "raw_dir": str(raw),
        "delisted_symbols": len(spans),
        "sample_per_library": sample,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "libraries": [r.to_dict() for r in reports],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="退市股因子覆盖探针（只读）")
    parser.add_argument(
        "--root",
        default=os.getenv(
            "QM_QUANTDB_DATA_DIR", str(PROJECT_ROOT / "data" / "quantdb")
        ),
    )
    parser.add_argument("--raw", default=None, help="退市原始缓存目录")
    parser.add_argument("--libs", default=",".join(DEFAULT_LIBS))
    parser.add_argument("--sample", type=int, default=20)
    parser.add_argument("--write", action="store_true", help="写 report JSON")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )
    report = build_report(
        args.root,
        raw_dir=args.raw,
        libraries=[s.strip() for s in args.libs.split(",") if s.strip()],
        sample=args.sample,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.write:
        out = Path(args.root) / "factor_research" / "delisted_coverage.json"
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
