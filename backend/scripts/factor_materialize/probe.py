"""尾窗物化的证伪探针：重叠日复算比对 + 必需列覆盖率。

两类检查，边界不同：

1. **重叠日比对**（producer 多写前一个交易日，不并入正式库）：把「有限预热
   窗口」复算出来的前一交易日值，与正式库里「全历史计算」的存量值逐列比对。
   预热不足的典型症状是必需列整列变空（长窗 rolling 的 min_periods 不满足），
   其次是覆盖塌陷。**数值级残差不是硬判据**：qfq（``daily_forward``）历史每晚
   按最新复权因子整段重写，价格派生的比值/相关类因子在重叠日上的相对误差会被
   近零分母放大（2026-10-20 实测 share_rel_gt 最高 0.66，但秩相关全部 ≥0.985）。
   结构真的坏了（公式语义变更、单位切换、取错列）秩相关才会掉下来，故硬闸用
   **秩相关下限 + 覆盖比例下限**；相对误差只作信息上报。
2. **必需列覆盖**：合并后逐目标日检查契约列至少有一个非空值（结构性全空
   列已在 ``contract.KNOWN_EMPTY_COLUMNS`` 白名单剔除）。

所有函数只读，不做任何写盘。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

LOG = logging.getLogger("factor_materialize.probe")

#: 相对误差的信息口径（**不再作硬闸**，见模块 docstring）
DRIFT_REL_TOL = 0.01
#: 数值漂移的硬判据：重叠日共同非空值上的秩相关下限。qfq 每晚重算后实测
#: 0.985~0.9999；语义/单位/取列错误的秩相关会明显掉下来。
DRIFT_MIN_RANK_CORR = 0.9
#: 覆盖比例硬判据：staged 非空值数 / 正式库非空值数。预热不足的次生症状是
#: 长窗列只剩尾部少数值；整列空由 ``required_empty`` 单独判。
DRIFT_MIN_COVERAGE_RATIO = 0.5
#: 行集合判据：staged 缺失的正式库标的占比
UNIVERSE_MISSING_LIMIT = 0.05
#: 漂移比对至少需要的共同非空值数量（少了只报信息，不设闸）
DRIFT_MIN_COMMON = 100
#: 阈值计数型因子的漂移豁免名单：这类列的值 = 「240 日内收益超 ±kσ 的天数」/ 窗长，
#: 取值是 1/N 的整数倍。qfq 基准每晚整段重写，个别交易日会在阈值边界翻转，整列按
#: 1/N 跳变，秩相关必然跌破下限——这不是算错，而是两条都合法的序列（不同复权基准）
#: 本就不该逐值对齐。2026-10-29 实测：f13_0654 在重叠日 20261013 上秩相关 0.847、
#: 65.3% 的值变化，而同批长窗列 f13_0645 / f13_0261 与正式库 diff=0.0。
#: 不豁免的后果是长窗遍**每晚**判失败 → 长窗列永远合并不进去（2026-10-14 起
#: f13_0645 / f13_0261 全空，正是 300 因子模型在用的两列）。名单里的列降级为
#: informational，仍然可见，只是不再阻断合并。
DRIFT_EXEMPT_COLUMNS = frozenset({"f13_0654"})


def _read(path: Path) -> pd.DataFrame | None:
    if not path.is_file():
        return None
    return pd.read_parquet(path)


def nonnull_counts(path: Path, columns: list[str]) -> dict[str, int]:
    """分区内各列非空计数；分区缺列记 -1（结构性缺失，单独判）。"""
    import pyarrow.parquet as pq

    if not path.is_file():
        return dict.fromkeys(columns, -2)
    present = set(pq.ParquetFile(path).schema_arrow.names)
    wanted = [column for column in columns if column in present]
    missing = {column: -1 for column in columns if column not in present}
    if not wanted:
        return missing
    frame = pd.read_parquet(path, columns=wanted)
    counts = {column: int(frame[column].notna().sum()) for column in wanted}
    counts.update(missing)
    return counts


def emptiness_regressions(
    root: Path,
    dates: list[str],
    required: list[str],
) -> dict[str, dict[str, list[str]]]:
    """必需列「前一日有值、当日全空」的日期（含当日缺列）。"""
    import pyarrow.parquet as pq

    ordered = sorted(dates)
    issues: dict[str, dict[str, list[str]]] = {}
    previous_nonnull: dict[str, int] | None = None
    for dt in ordered:
        path = Path(root) / f"dt={dt}" / "data.parquet"
        if not path.is_file():
            previous_nonnull = None
            continue
        present = set(pq.ParquetFile(path).schema_arrow.names)
        absent = [column for column in required if column not in present]
        wanted = [column for column in required if column in present]
        if wanted:
            frame = pd.read_parquet(path, columns=wanted)
            current = {column: int(frame[column].notna().sum()) for column in wanted}
        else:
            current = {}
        current.update(dict.fromkeys(absent, -1))
        if previous_nonnull is not None:
            dropped = [
                column
                for column, count in current.items()
                # -1 = 当日整列缺列：同样算「有值变无」，否则 schema 掉列
                # 永远不会进回补窗口（-2 只用于分区缺失，走不到这里）
                if count <= 0 and previous_nonnull.get(column, 0) > 0
            ]
            if dropped:
                issues[dt] = {
                    "all_empty": sorted(dropped),
                    "absent": sorted(absent),
                }
        previous_nonnull = {column: max(count, 0) for column, count in current.items()}
    return issues


def repair_dates(
    root: Path,
    trading_days: list[str],
    *,
    target: str,
    required: list[str],
    window: int = 5,
) -> list[str]:
    """近 ``window`` 个交易日里，必需列相对前一日「有值变全空」的日期。

    这些日期要重新物化（上游数据晚间才到齐的列，次日回补）。
    """
    # 多取一天做「前值」：窗口首日也需要前一交易日作对照
    recent = [dt for dt in trading_days if dt <= target][-(window + 1) :]
    if not recent:
        return []
    issues = emptiness_regressions(Path(root), recent, required)
    return sorted(issues)


def compare_partitions(
    official: Path,
    staged: Path,
    *,
    required: list[str],
    rel_tol: float = DRIFT_REL_TOL,
    min_common: int = DRIFT_MIN_COMMON,
    min_rank_corr: float = DRIFT_MIN_RANK_CORR,
    min_coverage_ratio: float = DRIFT_MIN_COVERAGE_RATIO,
    universe_missing_limit: float = UNIVERSE_MISSING_LIMIT,
) -> dict[str, object]:
    """同一天「正式库 vs staging 复算」逐列比对，返回问题清单与统计。

    硬性问题（``issues``）：
    - ``required_empty``：必需列正式库非空、staging 整列空；
    - ``required_absent``：staging 缺必需列；
    - ``required_coverage_drop``：staged 非空值不足正式库的一半（预热不足）；
    - ``required_drift``：必需列共同非空值上的秩相关跌破 ``min_rank_corr``；
    - ``universe``：staging 相比正式库缺标的超限。

    相对误差（``share_rel_gt``）只进 ``informational``：qfq 每晚重算会让它
    合法地大幅波动，拿它做硬闸会天天误报。
    """
    official_frame = _read(Path(official))
    staged_frame = _read(Path(staged))
    if official_frame is None or staged_frame is None:
        return {
            "ok": False,
            "error": "分区缺失",
            "official": official_frame is not None,
            "staged": staged_frame is not None,
        }
    if "symbol" not in official_frame.columns or "symbol" not in staged_frame.columns:
        return {"ok": False, "error": "分区缺 symbol 列"}
    official_frame = official_frame.drop_duplicates("symbol").set_index("symbol")
    staged_frame = staged_frame.drop_duplicates("symbol").set_index("symbol")

    required_set = set(required)
    issues: list[dict[str, object]] = []
    informational: list[dict[str, object]] = []

    official_symbols = set(official_frame.index)
    staged_symbols = set(staged_frame.index)
    missing_symbols = official_symbols - staged_symbols
    missing_share = len(missing_symbols) / max(len(official_symbols), 1)
    if missing_share > universe_missing_limit:
        issues.append(
            {
                "kind": "universe",
                "missing_symbols": len(missing_symbols),
                "official_symbols": len(official_symbols),
                "share": round(missing_share, 4),
            }
        )

    common_index = official_frame.index.intersection(staged_frame.index)
    common_columns = [
        column
        for column in official_frame.columns
        if column in staged_frame.columns and column != "symbol"
    ]
    absent_required = sorted(
        column
        for column in required_set
        if column in official_frame.columns and column not in staged_frame.columns
    )
    if absent_required:
        issues.append({"kind": "required_absent", "columns": absent_required[:20]})

    for column in common_columns:
        left = official_frame.loc[common_index, column]
        right = staged_frame.loc[common_index, column]
        left_nonnull = int(left.notna().sum())
        right_nonnull = int(right.notna().sum())
        if left_nonnull == 0 and right_nonnull == 0:
            continue
        both = left.notna() & right.notna()
        common = int(both.sum())
        max_abs = 0.0
        share_rel_gt = 0.0
        rank_corr: float | None = None
        # 元数据列（time/date）是 timestamp，非数值列只做空值检查：
        # 直接 astype("float64") 会抛 TypeError，曾让每个真实库的重叠探针必崩。
        numeric = pd.api.types.is_numeric_dtype(left) and pd.api.types.is_numeric_dtype(
            right
        )
        if common and numeric:
            left_values = left[both].astype("float64")
            right_values = right[both].astype("float64")
            diff = (left_values - right_values).abs()
            max_abs = float(diff.max())
            scale = left_values.abs().clip(lower=1e-12)
            share_rel_gt = float((diff / scale > rel_tol).mean())
            # 常数序列（全 0 的 IF 分支等）秩相关是 NaN，不能当结构损坏
            if common >= min_common and left_values.nunique() > 1:
                corr = left_values.rank().corr(right_values.rank())
                if pd.notna(corr):
                    rank_corr = float(corr)
        elif common:
            common = 0  # 非数值列不参与漂移判据
        is_required = column in required_set
        coverage_ratio = right_nonnull / left_nonnull if left_nonnull > 0 else 1.0
        entry = {
            "column": column,
            "required": is_required,
            "official_nonnull": left_nonnull,
            "staged_nonnull": right_nonnull,
            "coverage_ratio": round(coverage_ratio, 4),
            "common": common,
            "max_abs_diff": max_abs,
            "share_rel_gt": round(share_rel_gt, 4),
            "rank_corr": None if rank_corr is None else round(rank_corr, 4),
        }
        if is_required and left_nonnull > 0 and right_nonnull == 0:
            issues.append({"kind": "required_empty", **entry})
        elif is_required and left_nonnull > 0 and coverage_ratio < min_coverage_ratio:
            issues.append({"kind": "required_coverage_drop", **entry})
        elif is_required and rank_corr is not None and rank_corr < min_rank_corr:
            if column in DRIFT_EXEMPT_COLUMNS:
                # 阈值计数型：基准漂移导致的边界翻转，不阻断合并（见常量注释）
                informational.append({"kind": "required_drift_exempt", **entry})
            else:
                issues.append({"kind": "required_drift", **entry})
        elif not is_required and right_nonnull == 0 and left_nonnull > 0:
            informational.append({"kind": "column_empty", **entry})
        elif is_required and (share_rel_gt > 0 or coverage_ratio < 1.0):
            informational.append({"kind": "required_numeric_drift", **entry})
        elif max_abs > 0:
            informational.append({"kind": "diff", **entry})
    informational.sort(key=lambda item: item["share_rel_gt"], reverse=True)
    return {
        "ok": not issues,
        "official_rows": len(official_frame),
        "staged_rows": len(staged_frame),
        "common_symbols": len(common_index),
        "missing_symbols": len(missing_symbols),
        "issues": issues,
        "informational": informational[:20],
        "compared_columns": len(common_columns),
    }


def coverage(
    root: Path,
    dates: list[str],
    required: list[str],
) -> dict[str, object]:
    """必需列覆盖：每个目标日至少一个非空值；返回缺口明细。"""
    gaps: dict[str, dict[str, object]] = {}
    for dt in dates:
        counts = nonnull_counts(Path(root) / f"dt={dt}" / "data.parquet", required)
        missing = bool(counts) and all(count == -2 for count in counts.values())
        absent = sorted(column for column, count in counts.items() if count == -1)
        empty = sorted(column for column, count in counts.items() if count == 0)
        if missing or absent or empty:
            gaps[dt] = {
                "partition": "" if missing else "present",
                "absent": absent,
                "all_empty": empty,
            }
    return {"ok": not gaps, "gaps": gaps, "dates": list(dates)}
