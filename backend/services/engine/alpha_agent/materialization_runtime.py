"""Chunked factor evaluation for private materialization.

Statistical factors keep the frozen research panel inside its window. Dates
before that freeze and after it are computed from backward-adjusted prices and
do not restate the frozen window. Functional and Factor-class jobs read exact
QuantDB partitions in sandbox-sized chunks and never hand the sandbox a
post-window row.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from backend.services.engine.alpha_agent.materialization_window import (
    chunk_output_bounds,
)


def _output_frame(series, start: str, end: str):
    import pandas as pd

    from backend.shared.stock_utils import StockCodeUtil

    if series is None or len(series) == 0:
        return pd.DataFrame(columns=["symbol", "trade_date", "value"])
    dates_all = pd.to_datetime(series.index.get_level_values(1), errors="coerce")
    keep = (dates_all >= pd.Timestamp(start)) & (dates_all <= pd.Timestamp(end))
    kept = series.iloc[keep]
    if len(kept) == 0:
        return pd.DataFrame(columns=["symbol", "trade_date", "value"])
    symbols = kept.index.get_level_values(0).map(StockCodeUtil.to_suffix)
    dates = pd.to_datetime(kept.index.get_level_values(1), errors="coerce")
    return pd.DataFrame(
        {
            "symbol": symbols,
            "trade_date": dates.strftime("%Y-%m-%d"),
            "value": pd.to_numeric(kept.to_numpy(), errors="coerce"),
        }
    )


def _compute_panel_series(
    panel,
    candidate,
    *,
    should_cancel: Callable[[], bool] | None,
    progress: Callable[[dict[str, Any]], None] | None,
):
    import pandas as pd

    from backend.services.engine.alpha_agent.factor_job_executor import (
        FactorMaterializationCancelled,
    )

    pieces = []
    total_symbols = int(panel.index.get_level_values(0).nunique())
    for index, (_symbol, group) in enumerate(
        panel.groupby(level=0, sort=False), start=1
    ):
        if should_cancel is not None and should_cancel():
            raise FactorMaterializationCancelled("Factor materialization was cancelled")
        values = candidate.compute(group)
        if not isinstance(values, pd.Series) or len(values) != len(group):
            raise ValueError("Trusted statistical candidate returned invalid values")
        pieces.append(pd.Series(values.to_numpy(), index=group.index, name="factor"))
        if progress is not None and (index % 25 == 0 or index == total_symbols):
            progress(
                {
                    "stage": "factor_eval",
                    "progress_pct": 35 + int(2 * index / max(total_symbols, 1)),
                }
            )
    if not pieces:
        return None
    return pd.concat(pieces).sort_index()


def _load_frozen_panel(
    request: dict,
    input_columns: list[str],
    *,
    tenant_id: str,
    user_id: str,
    panel_limit: str,
):
    import pandas as pd

    from backend.shared.owner_path import user_scoped_dir
    from backend.services.engine.alpha_agent.launcher import AlphaAgentLauncher
    from backend.services.engine.alpha_agent.statistical.worker import digest_file

    task_id = request["statistical_task_id"]
    expected_hash = request.get("statistical_panel_sha256")
    if not isinstance(expected_hash, str) or len(expected_hash) != 64:
        raise ValueError("Frozen statistical panel fingerprint is invalid")
    launcher = AlphaAgentLauncher()
    task_dir = launcher.task_dir(task_id, tenant_id=tenant_id, user_id=user_id)
    owner_dir = user_scoped_dir(launcher._log_dir, tenant_id, user_id)
    if task_dir.is_symlink() or not task_dir.resolve().is_relative_to(
        owner_dir.resolve()
    ):
        raise ValueError("Frozen statistical panel escaped its owner scope")
    panel_path = task_dir / "statistical_panel.parquet"
    if digest_file(panel_path, 2 * 1024**3) != expected_hash:
        raise ValueError("Frozen statistical panel hash does not match")
    panel = pd.read_parquet(
        panel_path, columns=[*input_columns, "trade_date", "symbol"]
    )
    if isinstance(panel.index, pd.MultiIndex) and {"trade_date", "symbol"}.issubset(
        panel.index.names
    ):
        panel = panel.reset_index()
    if not {"trade_date", "symbol", *input_columns}.issubset(panel.columns):
        raise ValueError("Frozen statistical panel schema is invalid")
    if panel.empty or len(panel) > 2_000_000:
        raise RuntimeError("Frozen statistical panel is empty or oversized")
    panel["trade_date"] = pd.to_datetime(panel["trade_date"], errors="coerce")
    if panel[["symbol", "trade_date"]].isna().any().any():
        raise ValueError("Frozen statistical panel has missing symbol/date keys")
    if panel.duplicated(["symbol", "trade_date"]).any():
        raise ValueError("Frozen statistical panel has duplicate symbol/date keys")
    symbols = sorted(panel["symbol"].astype(str).unique())
    limit = pd.Timestamp(panel_limit)
    panel = panel[panel["trade_date"] <= limit]
    return panel, symbols


def _prepare_frozen_index(panel, candidate, start: str):
    import pandas as pd

    calendar = pd.DatetimeIndex(panel["trade_date"].drop_duplicates()).sort_values()
    start_position = int(calendar.searchsorted(pd.Timestamp(start), side="left"))
    if start_position < candidate.min_history:
        raise ValueError("Frozen statistical panel lacks factor warmup history")
    fetch_start = calendar[start_position - candidate.min_history]
    indexed = (
        panel[panel["trade_date"] >= fetch_start]
        .set_index(["symbol", "trade_date"])
        .sort_index()
    )
    expected = [
        value.strftime("%Y-%m-%d")
        for value in calendar
        if pd.Timestamp(start) <= value <= panel["trade_date"].max()
    ]
    return indexed, expected


def _hfq_extension_frame(
    symbols: list[str],
    candidate,
    input_columns: list[str],
    *,
    start: str,
    end: str,
    should_cancel,
    progress,
    keep_after: str | None = None,
    keep_before: str | None = None,
):
    """Compute one backward-adjusted span outside the frozen research window."""
    import pandas as pd

    from backend.services.engine.alpha_agent.materialization_window import (
        materialization_date_chunks,
    )
    from backend.services.engine.alpha_agent.quantdb_price_panel import (
        load_factor_price_panel,
    )

    if start > end:
        return None, []
    frames = []
    trading_dates: list[str] = []
    for chunk_start, chunk_end in materialization_date_chunks(
        start, end, max(len(symbols), 1), candidate.min_history
    ):
        panel = load_factor_price_panel(
            symbols,
            chunk_start,
            chunk_end,
            warmup_bars=candidate.min_history,
            adjust="hfq",
        )
        fields = [f"${column}" for column in input_columns]
        renamed = panel[fields].rename(
            columns=dict(zip(fields, input_columns, strict=True))
        )
        series = _compute_panel_series(
            renamed, candidate, should_cancel=should_cancel, progress=progress
        )
        frame = _output_frame(series, chunk_start, chunk_end)
        if len(frame) and keep_after is not None:
            frame = frame[frame["trade_date"] > keep_after]
        if len(frame) and keep_before is not None:
            frame = frame[frame["trade_date"] < keep_before]
        if len(frame):
            frames.append(frame)
        trading_dates.extend(
            day
            for day in (panel.attrs.get("trading_dates") or [])
            if chunk_start <= day <= chunk_end
            and (keep_after is None or day > keep_after)
            and (keep_before is None or day < keep_before)
        )
    if not frames:
        return None, trading_dates
    return pd.concat(frames, ignore_index=True), trading_dates


def _append_backward_extension(
    symbols: list[str],
    candidate,
    input_columns: list[str],
    *,
    freeze_end: str,
    end: str,
    should_cancel,
    progress,
):
    from datetime import date, timedelta

    ext_start = (date.fromisoformat(freeze_end) + timedelta(days=1)).isoformat()
    return _hfq_extension_frame(
        symbols,
        candidate,
        input_columns,
        start=ext_start,
        end=end,
        should_cancel=should_cancel,
        progress=progress,
        keep_after=freeze_end,
    )


def _prepend_backward_extension(
    symbols: list[str],
    candidate,
    input_columns: list[str],
    *,
    start: str,
    freeze_start: str,
    should_cancel,
    progress,
):
    from datetime import date, timedelta

    prefix_end = (date.fromisoformat(freeze_start) - timedelta(days=1)).isoformat()
    return _hfq_extension_frame(
        symbols,
        candidate,
        input_columns,
        start=start,
        end=prefix_end,
        should_cancel=should_cancel,
        progress=progress,
        keep_before=freeze_start,
    )


def evaluate_statistical_materialization(
    request: dict,
    candidate,
    input_columns: list[str],
    *,
    tenant_id: str,
    user_id: str,
    start: str,
    end: str,
    should_cancel: Callable[[], bool] | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
):
    """Research-window rows come from the hashed frozen panel.

    Dates before ``statistical_freeze_start`` and after ``statistical_freeze_end``
    are filled from backward-adjusted prices. Those rows do not restate the
    frozen research window, which is what model training needs from 2018 on.
    """
    import pandas as pd

    freeze_start = request.get("statistical_freeze_start")
    if freeze_start is not None and not isinstance(freeze_start, str):
        raise ValueError("Statistical freeze start is invalid")
    freeze_end = request.get("statistical_freeze_end") or end
    if not isinstance(freeze_end, str):
        raise ValueError("Statistical freeze end is invalid")
    panel_limit = freeze_end if freeze_end < end else end
    frozen_output_start = start
    if isinstance(freeze_start, str) and start < freeze_start:
        frozen_output_start = freeze_start
    use_frozen_rows = pd.Timestamp(frozen_output_start) <= pd.Timestamp(panel_limit)
    evidence: dict[str, Any] = {
        "price_source": "frozen_statistical_panel",
        "freeze_start": freeze_start if isinstance(freeze_start, str) else None,
        "freeze_end": freeze_end,
        "extension_adjust": None,
    }
    panel, symbols = _load_frozen_panel(
        request,
        input_columns,
        tenant_id=tenant_id,
        user_id=user_id,
        panel_limit=panel_limit if use_frozen_rows else freeze_end,
    )
    expected: list[str] = []
    frames = []
    if isinstance(freeze_start, str) and start < freeze_start:
        prefix, prefix_dates = _prepend_backward_extension(
            symbols,
            candidate,
            input_columns,
            start=start,
            freeze_start=freeze_start,
            should_cancel=should_cancel,
            progress=progress,
        )
        evidence["extension_adjust"] = "hfq"
        expected.extend(prefix_dates)
        if prefix is not None and len(prefix):
            frames.append(prefix)
    if use_frozen_rows:
        if panel.empty:
            raise ValueError("Frozen statistical panel has no rows inside the request")
        indexed, frozen_expected = _prepare_frozen_index(
            panel, candidate, frozen_output_start
        )
        series = _compute_panel_series(
            indexed, candidate, should_cancel=should_cancel, progress=progress
        )
        frames.append(_output_frame(series, frozen_output_start, panel_limit))
        expected.extend(frozen_expected)
    if end > freeze_end:
        extension, ext_dates = _append_backward_extension(
            symbols,
            candidate,
            input_columns,
            freeze_end=freeze_end,
            end=end,
            should_cancel=should_cancel,
            progress=progress,
        )
        evidence["extension_adjust"] = "hfq"
        expected.extend(ext_dates)
        if extension is not None and len(extension):
            frames.append(extension)
    if not frames:
        output = pd.DataFrame(columns=["symbol", "trade_date", "value"])
    else:
        output = pd.concat(frames, ignore_index=True)
    return output, evidence, expected


def _chunk_factor_frame(
    frame, start: str, end: str, warmup: int, *, date_level: int = 1
):
    import numpy as np
    import pandas as pd

    level_dates = pd.to_datetime(frame.index.get_level_values(date_level))
    dates = pd.DatetimeIndex(level_dates.unique()).sort_values()
    output_dates = dates[(dates >= pd.Timestamp(start)) & (dates <= pd.Timestamp(end))]
    symbol_level = 0 if date_level == 1 else 1
    n_symbols = max(int(frame.index.get_level_values(symbol_level).nunique()), 1)
    for start_i, end_i in chunk_output_bounds(len(output_dates), n_symbols, warmup):
        chunk_dates = output_dates[start_i:end_i]
        position = int(dates.searchsorted(chunk_dates[0]))
        window_lo = dates[max(0, position - warmup)]
        window_hi = chunk_dates[-1]
        mask = np.asarray((level_dates >= window_lo) & (level_dates <= window_hi))
        sl = frame.iloc[np.flatnonzero(mask)]
        yield sl, chunk_dates


def run_factor_class_in_chunks(
    frame,
    factor_code: str,
    *,
    start: str,
    end: str,
    warmup: int,
    tenant_id: str,
    user_id: str,
    datasets,
    lease,
    budget,
    lifecycle,
    should_cancel: Callable[[], bool] | None = None,
):
    """Evaluate a Factor class without pickling the whole history at once."""
    import numpy as np
    import pandas as pd

    from backend.services.engine.alpha_agent.factor_job_executor import (
        FactorMaterializationCancelled,
    )
    from backend.services.engine.strategy_lab.runner.container_runner import (
        run_factor_series,
    )
    from backend.shared.stock_utils import StockCodeUtil

    frames = []
    level_name_dates = 1
    for sl, chunk_dates in _chunk_factor_frame(frame, start, end, warmup):
        if should_cancel is not None and should_cancel():
            raise FactorMaterializationCancelled("Factor materialization was cancelled")
        if sl.empty:
            continue
        values = run_factor_series(
            factor_code,
            sl,
            tenant_id=tenant_id,
            user_id=user_id,
            datasets=datasets,
            lease=lease,
            budget=budget,
            lifecycle=lifecycle,
        )
        array = np.asarray(values, dtype=float)
        if len(array) != len(sl):
            raise ValueError("Sandbox factor series shape does not match its chunk")
        sl_dates = pd.to_datetime(sl.index.get_level_values(level_name_dates))
        keep = np.asarray((sl_dates >= chunk_dates[0]) & (sl_dates <= chunk_dates[-1]))
        kept = sl.iloc[np.flatnonzero(keep)]
        kept_values = array[keep]
        symbols = kept.index.get_level_values(0).map(StockCodeUtil.to_suffix)
        dates = pd.to_datetime(kept.index.get_level_values(1), errors="coerce")
        frames.append(
            pd.DataFrame(
                {
                    "symbol": symbols,
                    "trade_date": dates.strftime("%Y-%m-%d"),
                    "value": pd.to_numeric(kept_values, errors="coerce"),
                }
            )
        )
    if not frames:
        return pd.DataFrame(columns=["symbol", "trade_date", "value"])
    return pd.concat(frames, ignore_index=True)


def _referenced_catalog_features(code: str) -> list[str]:
    import ast
    import re

    from backend.services.engine.alpha_agent.factor_codegen import feature_catalog

    names: set[str] = set()
    try:
        tree = ast.parse(code)
    except SyntaxError:
        tree = None
    if tree is not None:
        names.update(
            node.value[1:]
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.startswith("$")
            and len(node.value) > 1
        )
    names.update(re.findall(r"\$([A-Za-z_][A-Za-z0-9_]*)", code))
    catalog = feature_catalog()
    return [name for name in sorted(names) if name in catalog]


def evaluate_functional_materialization(
    factor_code: str,
    instruments: list[str],
    *,
    start: str,
    end: str,
    tenant_id: str,
    user_id: str,
    datasets,
    lease,
    budget,
    lifecycle,
    should_cancel: Callable[[], bool] | None = None,
):
    """Read exact price partitions and hide every row after ``end``."""
    import numpy as np
    import pandas as pd

    from backend.services.engine.alpha_agent.factor_job_executor import (
        FactorMaterializationCancelled,
    )
    from backend.services.engine.alpha_agent.factor_panel_inputs import enrich_panel
    from backend.services.engine.alpha_agent.materialization_window import (
        infer_functional_warmup,
        materialization_date_chunks,
    )
    from backend.services.engine.alpha_agent.quantdb_price_panel import (
        load_factor_price_panel,
    )
    from backend.services.engine.strategy_lab.runner.container_runner import (
        run_functional_factor_values,
    )

    warmup = infer_functional_warmup(factor_code)
    features = _referenced_catalog_features(factor_code)
    pieces = []
    expected: list[str] = []
    import os
    import tempfile

    saw_rows = False
    for chunk_start, chunk_end in materialization_date_chunks(
        start, end, max(len(instruments), 1), warmup
    ):
        frame = load_factor_price_panel(
            instruments, chunk_start, chunk_end, warmup_bars=warmup
        )
        expected.extend(
            day
            for day in (frame.attrs.get("trading_dates") or [])
            if chunk_start <= day <= chunk_end and day <= end
        )
        level1 = pd.to_datetime(frame.index.get_level_values(1))
        frame = frame.iloc[np.flatnonzero(np.asarray(level1 <= pd.Timestamp(end)))]
        if frame.empty:
            continue
        saw_rows = True
        if features:
            frame = enrich_panel(frame, {"features": features, "warmup_bars": warmup})
            level1 = pd.to_datetime(frame.index.get_level_values(1))
            frame = frame.iloc[np.flatnonzero(np.asarray(level1 <= pd.Timestamp(end)))]
            if frame.empty:
                continue
        ordered = frame.reorder_levels([1, 0]).sort_index()
        for sl, chunk_dates in _chunk_factor_frame(
            ordered, chunk_start, chunk_end, warmup, date_level=0
        ):
            if should_cancel is not None and should_cancel():
                raise FactorMaterializationCancelled(
                    "Factor materialization was cancelled"
                )
            if sl.empty:
                continue
            # The worker treats level 0 as the date. Chunks are already in that order.
            fd, path = tempfile.mkstemp(suffix=".parquet")
            os.close(fd)
            try:
                sl.to_parquet(path, index=True)
                part = run_functional_factor_values(
                    factor_code,
                    path,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    datasets=datasets,
                    start_date=chunk_dates[0].strftime("%Y-%m-%d"),
                    end_date=chunk_dates[-1].strftime("%Y-%m-%d"),
                    universe_symbols=None,
                    lease=lease,
                    budget=budget,
                    lifecycle=lifecycle,
                )
            finally:
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass
            if part is not None and len(part):
                pieces.append(part)
    if not saw_rows:
        raise RuntimeError("QuantDB price input is empty for this window")
    if not pieces:
        output = pd.DataFrame(columns=["symbol", "trade_date", "value"])
    else:
        output = pd.concat(pieces, ignore_index=True)
    evidence = {
        "price_source": "quantdb_daily_forward",
        "scope": "exact_partitions",
        "legacy_panel_cache": False,
        "warmup_bars": warmup,
    }
    return output, evidence, expected
