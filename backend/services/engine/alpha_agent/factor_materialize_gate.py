"""Owner-library value-level duplicate gate for private rd_mined materialization.

Adapted from frozen AutoAlpha 4aaeda8b0e58477085762c1ac2e7faa7bdab096c
(backend/scripts/rd_mined_materialize.py, the rejected_duplicate evidence chain).
The correlation kernel reuses ``statistical.incremental.mean_rho_by_column`` -
the same single-source metric the frozen gate uses - instead of a second
implementation. The single-hit replacement verdict (T-FM-02) is ported from
``_replacement_decision``: a hit on exactly one active column is superseded
only when ``|new IC| >= max(0.10, 1.3 * |incumbent IC|)``, both ICs measured on
the same window with the same forward-return construction
(``statistical.incremental.mean_abs_rank_ic``). Anything else at or above the
threshold is rejected outright and the outcome is recorded for manual review.
Re-materializing the same factor is not self-rejected because its own previous
columns are excluded from the controls.

Controls are the owner's private rd_mined generations plus the shared factory
l1_factors partition set; both are read for sampled days only. A missing or
unreadable control set never blocks materialization - the gate degrades
visibly (corr_unverified) instead of inventing a zero.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: 近端窗口/采样日/口径下限与拒绝阈值，全部来自冻结来源常量。
RECENT_WINDOW_DAYS = 120
SAMPLE_DAYS = 60
MIN_PAIRS_PER_DAY = 20
MIN_SAMPLE_DAYS = 5
CORR_REJECT = 0.5

#: 替换判据常量，逐字对应冻结来源 ``_REPLACE_IC_FLOOR`` / ``_REPLACE_IC_RATIO``。
REPLACE_IC_FLOOR = 0.10
REPLACE_IC_RATIO = 1.3

_FACTORY_KEY_COLUMNS = {
    "symbol",
    "date",
    "dt",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
}


def _day_keys(values) -> list[str]:
    import pandas as pd

    normalized = pd.to_datetime(values, errors="coerce")
    return sorted(
        {day.strftime("%Y-%m-%d") for day in normalized.dropna().unique().tolist()}
    )


def pick_sample_days(trade_dates, *, window: int = RECENT_WINDOW_DAYS, n: int = SAMPLE_DAYS):
    """近端 window 个交易日里均匀取 n 天（与冻结来源 _pick_sample_days 同口径）。"""
    import numpy as np

    seq = _day_keys(trade_dates)
    if n <= 0 or len(seq) <= n:
        return seq[-window:] if window > 0 and len(seq) > window else seq
    recent = seq[-window:] if window > 0 and len(seq) > window else seq
    if len(recent) <= n:
        return recent
    idx = np.linspace(0, len(recent) - 1, n).round().astype(int)
    return [recent[i] for i in sorted(set(idx.tolist()))]


def _partition_name(day_key: str) -> str:
    return f"dt={day_key.replace('-', '')}"


def _indexed_column(frame, column: str):
    """(trade_date, symbol) 双层索引的单列 Series，symbol 归一后缀式。"""
    import pandas as pd

    from backend.shared.stock_utils import StockCodeUtil

    dates = pd.to_datetime(frame["trade_date"], errors="coerce")
    symbols = frame["symbol"].map(lambda value: StockCodeUtil.to_suffix(str(value)))
    keep = dates.notna() & symbols.ne("")
    series = pd.Series(
        pd.to_numeric(frame.loc[keep, column], errors="coerce").to_numpy(),
        index=pd.MultiIndex.from_arrays(
            [dates[keep].dt.strftime("%Y-%m-%d"), symbols[keep]],
            names=["trade_date", "symbol"],
        ),
        name=column,
    )
    return series[~series.index.duplicated(keep="last")]


def _read_owner_generation_column(tenant_id, user_id, mapping, sample_days) -> Any:
    """Read one owner rd_mined generation's sampled partitions as a Series."""
    from backend.shared.feature_source import user_factor_dataset_dir
    from backend.shared.owner_path import owner_segment

    generation_id, feature_key = mapping["generation_id"], mapping["feature_key"]
    root = user_factor_dataset_dir(
        tenant_id,
        user_id,
        "rd_mined",
        generation_id=owner_segment(generation_id),
    )
    frames = []
    for day in sample_days:
        path = root / _partition_name(day) / "data.parquet"
        if path.is_symlink() or not path.is_file():
            continue
        import pandas as pd

        frames.append(
            pd.read_parquet(
                path, columns=["symbol", "trade_date", feature_key], engine="pyarrow"
            )
        )
    if not frames:
        return None
    import pandas as pd

    return _indexed_column(pd.concat(frames, ignore_index=True), feature_key)


def _read_factory_l1_column_names(sample_days) -> tuple[list[tuple[str, Any]], int]:
    """Shared factory l1_factors sampled partitions as indexed column Series."""
    from backend.shared.feature_source import factor_source_dir

    root = factor_source_dir("l1_factors", market="CN")
    frames = []
    for day in sample_days:
        path = root / _partition_name(day) / "data.parquet"
        if path.is_symlink() or not path.is_file():
            continue
        import pandas as pd

        frames.append(pd.read_parquet(path, engine="pyarrow"))
    if not frames:
        return [], 0
    import pandas as pd

    frame = pd.concat(frames, ignore_index=True)
    date_column = next(
        (name for name in ("trade_date", "date", "dt") if name in frame.columns), None
    )
    if date_column is None or "symbol" not in frame.columns:
        return [], 0
    frame = frame.rename(columns={date_column: "trade_date"})
    columns = [
        column
        for column in frame.columns
        if column not in _FACTORY_KEY_COLUMNS
    ]
    series: list[tuple[str, Any]] = []
    for column in columns:
        series.append((column, _indexed_column(frame, column)))
    return series, len(columns)


def _read_factory_close_range(first_day: str, last_day: str, *, trailing: int = 5):
    """Contiguous close history over [first_day, last_day + trailing] for fwd returns.

    Forward returns need ``close[t+1]`` on the *next trading day*, so the gate
    cannot reuse the sampled-day slices: it loads the contiguous range instead.
    Any read failure returns ``None`` and the replacement verdict fails closed
    (duplicate → reject), never inventing an IC.
    """
    from backend.shared.feature_source import factor_source_dir

    import os

    try:
        begin = int(first_day.replace("-", ""))
        end = int(last_day.replace("-", "")) + max(0, int(trailing))
    except (TypeError, ValueError):
        return None
    root = factor_source_dir("l1_factors", market="CN")
    frames = []
    try:
        names = sorted(
            name[3:]
            for name in os.listdir(root)
            if name.startswith("dt=") and len(name) == 11 and name[3:].isdigit()
        )
    except OSError:
        return None
    for day_key in names:
        if not day_key.isdigit() or not begin <= int(day_key) <= end:
            continue
        path = root / f"dt={day_key}" / "data.parquet"
        if path.is_symlink() or not path.is_file():
            continue
        import pandas as pd

        try:
            frames.append(
                pd.read_parquet(
                    path, columns=["symbol", "trade_date", "close"], engine="pyarrow"
                )
            )
        except Exception:  # noqa: BLE001 - a single bad partition must not poison fwd
            continue
    if not frames:
        return None
    import pandas as pd

    frame = pd.concat(frames, ignore_index=True)
    if "symbol" not in frame.columns or "close" not in frame.columns:
        return None
    date_column = next(
        (name for name in ("trade_date", "date", "dt") if name in frame.columns), None
    )
    if date_column is None:
        return None
    frame = frame.rename(columns={date_column: "trade_date"})
    series = _indexed_column(frame[["symbol", "trade_date", "close"]], "close")
    return series if not series.empty else None


def _replacement_decision(
    *,
    hit_columns: list,
    owners: dict,
    active: dict,
    new_ic,
    incumbent_ic,
    ic_floor: float = REPLACE_IC_FLOOR,
    ic_ratio: float = REPLACE_IC_RATIO,
) -> tuple[bool, str]:
    """Single-hit replacement verdict (T-FM-02), ported from the frozen source.

    Replace only when the hit is exactly one column, that column belongs to an
    active (published, non-archived) owner generation, and the new column's
    ``|IC| >= max(ic_floor, ic_ratio * |incumbent IC|)`` on the same window and
    forward-return construction. Every other case fails closed to reject.
    """
    if len(hit_columns) != 1:
        return False, "not_single_hit"
    column = str(hit_columns[0])
    incumbent_id = str(owners.get(column) or "")
    if not incumbent_id:
        return False, "not_owned"
    if not active.get(column, False):
        return False, "incumbent_not_active"
    if new_ic is None:
        return False, "new_ic_missing"
    if incumbent_ic is None:
        return False, "incumbent_ic_missing"
    if abs(new_ic) < ic_floor:
        return False, "ic_below_floor"
    if abs(new_ic) < ic_ratio * abs(incumbent_ic):
        return False, "ratio_below"
    return True, "ok"


def evaluate_library_duplicate(
    output,
    *,
    tenant_id: str,
    user_id: str,
    factor_id: str,
    feature_key: str,
    control_mappings: list[dict],
) -> dict[str, Any]:
    """值级查重裁决：命中阈值返回 rejected_duplicate（单命中且更优则 replaced），
    否则 materialized。

    ``control_mappings`` 是 claim 侧按双维属主预先读取的本人 rd_mined 列清单
    （generation_id/feature_key/factor_id/generation_status/factor_archived，
    已排除本因子自身代次）；文件与相关性计算都在本函数内完成。任何对照读取
    失败只缩小对照集，不阻断物化。替换 IC 需要的远期收益来自工厂 l1 的 close
    连续区间，读不到时裁决 fail-closed（拒入）。
    """
    import pandas as pd

    values = _indexed_column(output, feature_key)
    if values.empty:
        raise ValueError("Factor materialization produced no indexable values")
    sample_days = pick_sample_days(values.index.get_level_values(0))
    columns: dict[str, Any] = {}
    opponent_by_column: dict[str, dict[str, Any]] = {}
    owners: dict[str, str | None] = {}
    active: dict[str, bool] = {}
    generation_by_column: dict[str, str] = {}
    user_columns = 0
    for mapping in control_mappings:
        try:
            series = _read_owner_generation_column(
                tenant_id, user_id, mapping, sample_days
            )
        except Exception as exc:  # noqa: BLE001 - 对照缺失不阻断物化
            logger.warning(
                "Owner rd_mined control %s unreadable: %s",
                mapping.get("generation_id"),
                type(exc).__name__,
            )
            continue
        if series is None or series.empty:
            continue
        columns[mapping["feature_key"]] = series
        opponent_by_column[mapping["feature_key"]] = {
            "kind": "user_rd_mined",
            "factor_id": mapping.get("factor_id"),
        }
        owners[mapping["feature_key"]] = mapping.get("factor_id")
        active[mapping["feature_key"]] = (
            mapping.get("generation_status") == "published"
            and not mapping.get("factor_archived")
        )
        if isinstance(mapping.get("generation_id"), str):
            generation_by_column[mapping["feature_key"]] = mapping["generation_id"]
        user_columns += 1
    factory_columns = 0
    factory_skipped = False
    try:
        factory_series, factory_columns = _read_factory_l1_column_names(sample_days)
    except Exception as exc:  # noqa: BLE001 - 对照缺失不阻断物化
        logger.warning("Factory l1_factors controls unavailable: %s", type(exc).__name__)
        factory_skipped = True
        factory_series = []
    for column, series in factory_series:
        if series.empty or column in columns:
            continue
        columns[column] = series
        opponent_by_column[column] = {"kind": "factory_l1", "factor_id": None}
    corr_unverified = not columns
    corr: float | None = None
    corr_against: str | None = None
    opponent: dict[str, Any] | None = None
    if columns:
        controls = pd.DataFrame(dict(sorted(columns.items())))
        controls = controls.loc[:, ~controls.columns.duplicated()]
        from backend.services.engine.alpha_agent.statistical.incremental import (
            mean_rho_by_column,
        )

        rho_series = mean_rho_by_column(
            values,
            controls,
            sample_days=set(sample_days),
            exclude=(feature_key,),
            min_pairs=MIN_PAIRS_PER_DAY,
            min_sample_days=MIN_SAMPLE_DAYS,
        )
        abs_rho = rho_series.abs().dropna()
        if abs_rho.empty:
            corr_unverified = True
        else:
            corr = float(abs_rho.max())
            corr_against = str(abs_rho.idxmax())
            opponent = opponent_by_column.get(corr_against) or {
                "kind": "unknown",
                "factor_id": None,
            }
    evidence = {
        "sample_days": len(sample_days),
        "control_columns": len(columns),
        "user_columns": user_columns,
        "factory_columns": factory_columns,
        "factory_skipped": factory_skipped,
        "corr_unverified": bool(corr_unverified),
        "min_pairs_per_day": MIN_PAIRS_PER_DAY,
        "min_sample_days": MIN_SAMPLE_DAYS,
        "corr_threshold": CORR_REJECT,
        "kernel": "statistical.incremental.mean_rho_by_column",
        "ic_kernel": "statistical.incremental.mean_abs_rank_ic",
        "ic_floor": REPLACE_IC_FLOOR,
        "ic_ratio": REPLACE_IC_RATIO,
        "replacement_migrated": True,
        "opponent": opponent,
    }
    if corr is not None and corr >= CORR_REJECT:
        hit_columns = [str(c) for c in abs_rho[abs_rho >= CORR_REJECT].index]
        new_ic: float | None = None
        incumbent_ic: float | None = None
        if len(hit_columns) == 1 and sample_days:
            from backend.services.engine.alpha_agent.statistical.incremental import (
                forward_return_series,
                mean_abs_rank_ic,
            )

            try:
                close = _read_factory_close_range(sample_days[0], sample_days[-1])
            except Exception:  # noqa: BLE001 - 远期收益不可得时裁决 fail-closed
                close = None
                logger.warning("Replacement close history unreadable")
            controls_fwd = (
                forward_return_series(pd.DataFrame({"close": close}))
                if close is not None and not close.empty
                else None
            )
            if controls_fwd is not None:
                window = set(sample_days)
                new_ic, _ = mean_abs_rank_ic(values, controls_fwd, sample_days=window)
                if hit_columns[0] in controls.columns:
                    incumbent_ic, _ = mean_abs_rank_ic(
                        controls[hit_columns[0]], controls_fwd, sample_days=window
                    )
        replace, reason = _replacement_decision(
            hit_columns=hit_columns,
            owners=owners,
            active=active,
            new_ic=new_ic,
            incumbent_ic=incumbent_ic,
        )
        evidence["replacement"] = {
            "decision": reason,
            "hits": hit_columns,
            "new_ic": new_ic,
            "incumbent_ic": incumbent_ic,
        }
        if replace:
            incumbent_column = hit_columns[0]
            evidence["replacement"]["replaced_generation_id"] = generation_by_column.get(
                incumbent_column
            )
            evidence["replacement"]["replaced_factor_id"] = owners.get(incumbent_column)
            return {
                "status": "replaced",
                "corr": corr,
                "corr_against": corr_against,
                "new_ic": new_ic,
                "incumbent_ic": incumbent_ic,
                "replaced_generation_id": generation_by_column.get(incumbent_column),
                "replaced_factor_id": owners.get(incumbent_column),
                "evidence": evidence,
            }
        return {
            "status": "rejected_duplicate",
            "corr": corr,
            "corr_against": corr_against,
            "evidence": evidence,
        }
    return {
        "status": "materialized",
        "corr": corr,
        "corr_against": corr_against if corr is not None else None,
        "evidence": evidence,
    }
