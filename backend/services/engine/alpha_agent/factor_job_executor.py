"""Trusted execution adapter for owner-scoped factor backtest jobs."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any


class FactorMaterializationCancelled(RuntimeError):
    """The owner requested stop and no further materialization writes should run."""


def _validate_materialization_coverage(output, expected_dates) -> None:
    """Require at least one finite factor value on each expected trading date."""
    import numpy as np
    import pandas as pd

    if not expected_dates:
        raise ValueError("Materialization trading calendar is empty")
    values = pd.to_numeric(output["value"], errors="coerce")
    valid = np.isfinite(values.to_numpy(dtype=float, na_value=np.nan))
    observed = set(output.loc[valid, "trade_date"].dropna().astype(str))
    missing = sorted(set(expected_dates) - observed)
    if missing:
        raise ValueError("因子物化缺少有效交易日数据: " + ", ".join(missing[:5]))


def _request_date(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must use YYYY-MM-DD")
    return value


def _cost_rate(request: dict[str, Any]) -> tuple[float, dict[str, float]]:
    from backend.services.engine.inference.trading_cost import CostModel

    supplied = request.get("cost_model")
    if supplied is None:
        model = CostModel.resolve()
    elif isinstance(supplied, dict):
        expected = {
            "commission_rate",
            "min_commission",
            "stamp_duty",
            "transfer_fee",
            "slippage",
            "round_trip_cost",
            "round_trip_cost_sh",
        }
        if set(supplied) != expected or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value < 0
            for value in supplied.values()
        ):
            raise ValueError("Frozen factor cost model is invalid")
        model = CostModel.resolve(override=supplied)
        if (
            abs(float(supplied["round_trip_cost"]) - model.round_trip_cost()) > 1e-12
            or abs(
                float(supplied["round_trip_cost_sh"])
                - model.round_trip_cost(is_sh=True)
            )
            > 1e-12
        ):
            raise ValueError("Frozen factor cost model fingerprint is invalid")
    else:
        raise ValueError("Frozen factor cost model is invalid")
    snapshot = model.as_dict()
    return snapshot["round_trip_cost"], snapshot


def _date_first(series):
    import pandas as pd

    if not isinstance(series.index, pd.MultiIndex) or series.index.nlevels != 2:
        raise ValueError("Factor cost calculation requires a two-level panel index")
    return series.set_axis(series.index.reorder_levels([1, 0]))


def _net_long_returns(factors, returns, *, cost_rate: float):
    from backend.services.engine.alpha_agent.statistical.costs import (
        daily_turnover_long,
    )

    turnover = daily_turnover_long(_date_first(factors), min_names=10)
    return returns - turnover.reindex(returns.index).fillna(0.0) * cost_rate


def _daily_series_snapshot(
    daily_returns, *, cost_rate: float, max_points: int = 1500
) -> dict[str, Any]:
    """Serialize bounded daily net equity and drawdown curves for UI/reporting."""
    if len(daily_returns) == 0:
        return {
            "equity_curve": [],
            "drawdown_curve": [],
            "basis": "equal_weight_top_30pct_net_next_day_returns",
            "cost_rate": cost_rate,
        }
    import numpy as np

    equity = (1 + daily_returns.astype(float)).cumprod()
    drawdown = (equity.cummax() - equity) / equity.cummax()
    positions = np.arange(len(equity))
    if len(positions) > max_points:
        positions = np.unique(
            np.linspace(0, len(positions) - 1, max_points).astype(int)
        )

    def encode(series):
        output = []
        for position in positions:
            value = float(series.iloc[position])
            day = series.index[position]
            if not np.isfinite(value):
                continue
            day = day.date().isoformat() if hasattr(day, "date") else str(day)[:10]
            output.append({"date": day, "value": value})
        return output

    return {
        "equity_curve": encode(equity),
        "drawdown_curve": encode(drawdown),
        "basis": "equal_weight_top_30pct_net_next_day_returns",
        "cost_rate": cost_rate,
    }


def execute_factor_backtest(
    request: dict[str, Any],
    *,
    tenant_id: str,
    user_id: str,
    lease,
    budget,
    lifecycle: dict[str, Any],
    progress: Callable[[dict[str, Any]], None],
    panel_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run one immutable factor-code snapshot against public A-share data."""
    if not isinstance(request, dict):
        raise ValueError("Factor backtest request snapshot is invalid")
    factor_id = request.get("factor_id")
    factor_code = request.get("factor_code")
    factor_revision_no = request.get("factor_revision_no")
    universe = request.get("universe", "csi300")
    data_source = request.get("data_source", "qlib_bin")
    if not isinstance(factor_id, str) or not factor_id.strip():
        raise ValueError("Factor backtest requires a factor ID")
    if not isinstance(factor_code, str) or not factor_code.strip():
        raise ValueError("Factor backtest requires a code snapshot")
    if type(factor_revision_no) is not int or factor_revision_no < 1:
        raise ValueError("Factor backtest requires an immutable source revision")
    if len(factor_code.encode("utf-8")) > 256 * 1024:
        raise ValueError("Factor code snapshot exceeds 256 KiB")
    expected_code_hash = request.get("factor_code_sha256")
    if (
        not isinstance(expected_code_hash, str)
        or hashlib.sha256(factor_code.encode("utf-8")).hexdigest() != expected_code_hash
    ):
        raise ValueError("Factor code snapshot fingerprint does not match")
    if not isinstance(universe, str) or not universe.strip():
        raise ValueError("Factor backtest requires an A-share universe")
    if data_source not in {"qlib_bin", "panel"}:
        raise ValueError("Unsupported A-share factor backtest data source")

    start = _request_date(request.get("start_date"), "start_date")
    end = _request_date(request.get("end_date"), "end_date")
    if start and end and start > end:
        raise ValueError("start_date must not be after end_date")

    from backend.services.engine.routers import alpha_agent as alpha_router

    if start is None or end is None:
        default_start, default_end = alpha_router._default_backtest_window("a_share")
        start = start or default_start
        end = end or default_end
    if start > end:
        raise ValueError("start_date must not be after end_date")
    cost_rate, cost_model = _cost_rate(request)

    validation_context = request.get("validation_context")
    if validation_context is not None:
        if (
            not isinstance(validation_context, dict)
            or validation_context.get("kind") != "out_of_sample_validation"
            or validation_context.get("non_overlapping") is not True
        ):
            raise ValueError("Factor validation context is invalid")
        training_window = validation_context.get("training_window")
        validation_window = validation_context.get("validation_window")
        if not isinstance(training_window, dict) or not isinstance(
            validation_window, dict
        ):
            raise ValueError("Factor validation windows are invalid")
        training_end = _request_date(
            training_window.get("end_date"), "training end_date"
        )
        validation_start = _request_date(
            validation_window.get("start_date"), "validation start_date"
        )
        if (
            training_end is None
            or validation_start is None
            or validation_start <= training_end
            or validation_window.get("start_date") != start
            or validation_window.get("end_date") != end
        ):
            raise ValueError("Factor validation window does not match the request")

    is_factorminer = factor_code.startswith("fm:")
    from backend.services.engine.alpha_agent.factor_codegen import panel_contract
    from backend.services.engine.alpha_agent.factor_panel_inputs import (
        enrich_panel,
        input_evidence,
        panel_fetch_start,
    )

    contract = None if is_factorminer else panel_contract(factor_code)
    if is_factorminer:
        if data_source != "qlib_bin":
            raise ValueError("FactorMiner expressions require the Qlib data source")
        from backend.services.engine.alpha_agent.factorminer.dsl import (
            min_history,
            parse,
        )

        fm_warmup = min_history(parse(factor_code[3:]))
        kind = "factor_miner"
    else:
        fm_warmup = 0
        kind = alpha_router._detect_factor_kind(factor_code)
    if kind == "unknown":
        raise ValueError(
            "Factor code must define a Factor class or calculate_* function"
        )
    if kind == "functional":
        data_source = "panel"
        data_path = alpha_router._resolve_factor_panel_path_for_market(
            "a_share", factor_code=factor_code, datasets=request.get("datasets")
        )
        if not data_path:
            raise RuntimeError("A-share functional factor panel data is unavailable")
        progress({"stage": "factor_eval", "progress_pct": 45})
        from backend.services.engine.strategy_lab.runner.container_runner import (
            run_functional_factor_metrics,
        )

        execution = run_functional_factor_metrics(
            factor_code,
            data_path,
            tenant_id=tenant_id,
            user_id=user_id,
            datasets=request.get("datasets"),
            start_date=start,
            end_date=end,
            universe_symbols=request.get("universe_symbols"),
            lease=lease,
            budget=budget,
            lifecycle=lifecycle,
            include_series=True,
            cost_rate=cost_rate,
            series_max_points=request.get("_series_max_points", 750),
        )
        if isinstance(execution, dict) and isinstance(execution.get("metrics"), dict):
            metrics = execution["metrics"]
            series_snapshot = execution.get("series")
        else:
            metrics = execution
            series_snapshot = None
        result = {
            "factor_id": factor_id,
            "universe": universe,
            "data_source": data_source,
            "start_date": start,
            "end_date": end,
            "metrics": metrics,
            "cost_model": cost_model,
        }
        if isinstance(series_snapshot, dict):
            result["series"] = series_snapshot
        if validation_context is not None:
            result["validation"] = validation_context
        return result

    import numpy as np
    import pandas as pd
    import qlib
    from qlib.data import D

    from backend.shared.qlib_paths import resolve_qlib_provider_uri
    from backend.services.engine.strategy_lab.runner.container_runner import (
        run_factor_series,
    )

    provider_uri = resolve_qlib_provider_uri("CN")
    qlib.init(provider_uri=provider_uri, region="cn")
    frozen_symbols = request.get("universe_symbols")
    if frozen_symbols is not None:
        if (
            not isinstance(frozen_symbols, list)
            or not frozen_symbols
            or len(frozen_symbols) > 20_000
            or any(not isinstance(symbol, str) for symbol in frozen_symbols)
        ):
            raise ValueError("Frozen A-share universe snapshot is invalid")
        from backend.shared.stock_utils import StockCodeUtil

        normalized = [StockCodeUtil.to_qlib(symbol) for symbol in frozen_symbols]
        if len(normalized) != len(set(normalized)):
            raise ValueError("Frozen A-share universe contains duplicate symbols")
        instruments = normalized
    else:
        instruments = alpha_router._resolve_instruments_for_universe(
            "CN", universe, tenant_id=tenant_id, user_id=user_id
        )
    if not instruments:
        raise RuntimeError("A-share universe resolved to no instruments")
    progress({"stage": "load_data", "progress_pct": 15})
    fetch_start = start
    if contract is not None:
        fetch_start = panel_fetch_start(start, contract["warmup_bars"], D)
    elif is_factorminer and fm_warmup:
        import pandas as pd

        calendar_start = (
            pd.Timestamp(start) - pd.Timedelta(days=fm_warmup * 2 + 40)
        ).strftime("%Y-%m-%d")
        calendar = pd.DatetimeIndex(
            D.calendar(
                start_time=calendar_start,
                end_time=start,
                freq="day",
            )
        )
        if calendar.empty:
            raise RuntimeError("Qlib calendar is empty for FactorMiner warmup")
        position = calendar.searchsorted(pd.Timestamp(start), side="left")
        fetch_start = pd.Timestamp(calendar[max(0, position - fm_warmup)]).strftime(
            "%Y-%m-%d"
        )
    qlib_fields = ["$open", "$high", "$low", "$close", "$volume", "$factor"]
    if is_factorminer or contract is not None or "quantdb_data" in factor_code:
        qlib_fields.append("$amount")
    frame = D.features(
        instruments,
        qlib_fields,
        start_time=fetch_start,
        end_time=end,
        freq="day",
    )
    if frame is None or frame.empty:
        raise RuntimeError("Qlib returned no data for the requested A-share window")
    if contract is not None:
        frame = enrich_panel(frame, contract)
    evidence = input_evidence(frame, contract) if contract is not None else None
    progress({"stage": "factor_eval", "progress_pct": 45})
    values = run_factor_series(
        factor_code,
        frame,
        tenant_id=tenant_id,
        user_id=user_id,
        datasets=request.get("datasets"),
        lease=lease,
        budget=budget,
        lifecycle=lifecycle,
    )
    if (is_factorminer or contract is not None) and fetch_start != start:
        dates = pd.to_datetime(frame.index.get_level_values(1))
        in_window = (dates >= pd.Timestamp(start)) & (dates <= pd.Timestamp(end))
        frame = frame.iloc[in_window]
        values = values[in_window]
    factors = pd.Series(values, index=frame.index, name="factor")
    forward_returns = (
        frame["$close"]
        .groupby(level=0)
        .transform(lambda close: close.pct_change(fill_method=None).shift(-1))
    )
    common = factors.index.intersection(forward_returns.index)
    if len(common) < 100:
        raise RuntimeError(
            "Factor and next-day returns have fewer than 100 aligned rows"
        )
    factor_values = factors.loc[common]
    return_values = forward_returns.loc[common]
    valid = np.isfinite(factor_values.values) & np.isfinite(return_values.values)
    if int(valid.sum()) < 100:
        raise RuntimeError("Fewer than 100 finite factor/return observations remain")
    factor_values = factor_values.iloc[np.flatnonzero(valid)]
    return_values = return_values.iloc[np.flatnonzero(valid)]
    ic_mean, rank_ic_median, icir, observations = (
        alpha_router._vectorized_daily_spearman_ic(factor_values, return_values)
    )
    if observations <= 0:
        raise RuntimeError("Daily information coefficient could not be computed")

    pairs = pd.DataFrame({"factor": factor_values, "return": return_values})
    pairs["date"] = pairs.index.get_level_values(1)
    pairs["rank"] = pairs.groupby("date")["factor"].rank(pct=True)
    daily_counts = pairs.groupby("date").size()
    eligible_dates = daily_counts[daily_counts >= 10].index
    pairs = pairs[pairs["date"].isin(eligible_dates)]
    daily_long_gross = (
        pairs[pairs["rank"] >= 0.7].groupby("date")["return"].mean().dropna()
    )
    daily_long = _net_long_returns(
        pairs["factor"], daily_long_gross, cost_rate=cost_rate
    )
    annual_return = float(daily_long.mean() * 252) if len(daily_long) else None
    sharpe = None
    max_drawdown = None
    if len(daily_long) > 1:
        volatility = float(daily_long.std(ddof=1))
        sharpe = float(daily_long.mean() / (volatility + 1e-8) * np.sqrt(252))
        equity = (1 + daily_long).cumprod()
        drawdown = (equity.cummax() - equity) / equity.cummax()
        max_drawdown = float(drawdown.max()) if len(drawdown) else None

    from backend.services.engine.alpha_agent.statistical.costs import net_rank_ic

    factor_cost_series = _date_first(pairs["factor"])
    return_cost_series = _date_first(pairs["return"])
    net_rank_ic_mean, net_rank_ic_days = net_rank_ic(
        factor_cost_series,
        return_cost_series,
        cost_rate=cost_rate,
        min_names=10,
    )
    from backend.services.engine.alpha_agent.factor_job_series import (
        build_factor_series,
    )

    series_snapshot = build_factor_series(
        factor_cost_series,
        return_cost_series,
        cost_rate=cost_rate,
        max_points=request.get("_series_max_points", 750),
    )
    if panel_callback is not None and validation_context is None:
        try:
            panel_callback(
                {
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "factor_id": factor_id,
                    "market": "a_share",
                    "universe": universe,
                    "values": factor_cost_series,
                    "forward_returns": return_cost_series,
                    "factor_revision_no": factor_revision_no,
                }
            )
        except Exception:
            # Research panels are an optional derivative; a storage failure must
            # never turn a completed backtest into a failed job.
            pass
    result = {
        "factor_id": factor_id,
        "factor_revision_no": factor_revision_no,
        "panel_input_evidence": evidence,
        "universe": universe,
        "data_source": "qlib_bin",
        "start_date": start,
        "end_date": end,
        "metrics": {
            "ic_mean": ic_mean,
            "rank_ic_median": rank_ic_median,
            "icir": icir,
            "sharpe_ratio": sharpe,
            "annual_return": annual_return,
            "max_drawdown": max_drawdown,
            "observations": observations,
            "gross_annual_return": (
                float(daily_long_gross.mean() * 252) if len(daily_long_gross) else None
            ),
            "net_rank_ic_mean": net_rank_ic_mean,
            "net_rank_ic_days": net_rank_ic_days,
        },
        "series": series_snapshot,
        "cost_model": cost_model,
        "performance_basis": "equal_weight_top_30pct_net_next_day_returns",
    }
    if validation_context is not None:
        result["validation"] = validation_context
    return result


def execute_factor_materialization(
    request: dict[str, Any],
    *,
    tenant_id: str,
    user_id: str,
    lease,
    budget,
    lifecycle: dict[str, Any],
    progress: Callable[[dict[str, Any]], None],
    should_cancel: Callable[[], bool] | None = None,
    panel_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Compute a saved Factor class into one private, quota-managed generation."""
    if not isinstance(request, dict):
        raise ValueError("Factor materialization snapshot is invalid")
    source_snapshot = request.get("source_data_snapshot")
    if source_snapshot is not None:
        from backend.services.engine.alpha_agent.data_snapshot import (
            verify_rd_agent_data_snapshot,
        )

        verify_rd_agent_data_snapshot(
            source_snapshot,
            data_source="qlib_bin",
            datasets=request.get("datasets"),
        )
    factor_id = request.get("factor_id")
    factor_code = request.get("factor_code")
    generation_id = request.get("generation_id")
    expected_hash = request.get("factor_code_sha256")
    factor_revision_no = request.get("factor_revision_no")
    if (
        not isinstance(factor_id, str)
        or not factor_id.strip()
        or not isinstance(factor_code, str)
        or not factor_code.strip()
        or not isinstance(generation_id, str)
        or not generation_id.strip()
        or type(factor_revision_no) is not int
        or factor_revision_no < 1
        or request.get("factor_kind")
        not in {"factor_class", "functional", "qlib_expression"}
        or not isinstance(expected_hash, str)
        or hashlib.sha256(factor_code.encode("utf-8")).hexdigest() != expected_hash
    ):
        raise ValueError("Factor materialization snapshot fingerprint is invalid")
    import io

    import pandas as pd
    from backend.shared.feature_source import quantcustom_root
    from backend.shared.owner_path import owner_segment
    from backend.shared.qlib_paths import resolve_qlib_provider_uri
    from backend.shared.stock_utils import StockCodeUtil
    from backend.shared.storage_quota_ledger import write_quota_managed_local_file
    from backend.services.engine.routers import alpha_agent as alpha_router

    factor_kind = request["factor_kind"]
    candidate = None
    if factor_kind == "qlib_expression":
        from backend.services.engine.alpha_agent.statistical.materialization import (
            trusted_statistical_candidate,
        )

        candidate = trusted_statistical_candidate(
            {
                "algorithm": "statistical",
                "candidate_key": request.get("statistical_candidate_key"),
                "evidence": request.get("statistical_evidence"),
            },
            factor_code,
        )
        if candidate is None:
            raise ValueError("Statistical factor expression is not trusted")
    elif alpha_router._detect_factor_kind(factor_code) != factor_kind:
        raise ValueError("Factor materialization kind does not match its source code")

    universe = request.get("universe", "csi300")
    if not isinstance(universe, str) or not universe.strip():
        raise ValueError("Factor materialization requires a frozen universe")
    start = _request_date(request.get("start_date"), "start_date")
    end = _request_date(request.get("end_date"), "end_date")
    if start is None or end is None:
        from backend.services.engine.alpha_agent.materialization_window import (
            TRAINING_MATERIALIZATION_START,
        )

        _, default_end = alpha_router._default_backtest_window("a_share")
        start = start or TRAINING_MATERIALIZATION_START
        end = end or default_end
    if start > end:
        raise ValueError("start_date must not be after end_date")
    input_columns: list[str] = []
    input_evidence_snapshot = None
    expected_materialization_dates = None
    if request.get("serving_only") is True and (
        start != end
        or not isinstance(request.get("source_generation_id"), str)
        or not request["source_generation_id"].strip()
    ):
        raise ValueError("Serving requires one date and a frozen source generation")
    if candidate is not None:
        import re

        from backend.services.engine.alpha_agent.statistical.candidates import (
            REQUIRED_COLUMNS,
        )

        required_columns = set(REQUIRED_COLUMNS)
        input_columns = sorted(set(re.findall(r"\$([a-z][a-z0-9_]*)", factor_code)))
        if not input_columns or not set(input_columns).issubset(required_columns):
            raise ValueError("Trusted statistical factor uses unsupported input fields")

    statistical_task_id = request.get("statistical_task_id")
    use_statistical_panel = candidate is not None and isinstance(
        statistical_task_id, str
    )
    instruments: list[str] = []
    if not use_statistical_panel:
        import qlib

        # 先 init：下方 _resolve_instruments_for_universe 内部会调 D.instruments，
        # 未 init 直接抛 "Please run qlib.init() first"（回测路径顺序正确，仅此处反了）。
        # This runs in an engine worker thread. Qlib's multiprocessing backend
        # otherwise forks the live ASGI/DB process while it owns other locks.
        qlib.init(provider_uri=resolve_qlib_provider_uri("CN"), region="cn", kernels=1)
        frozen_symbols = request.get("universe_symbols")
        if frozen_symbols is not None:
            if (
                not isinstance(frozen_symbols, list)
                or not frozen_symbols
                or len(frozen_symbols) > 20_000
                or any(not isinstance(symbol, str) for symbol in frozen_symbols)
            ):
                raise ValueError("Frozen factor universe is invalid")
            instruments = [StockCodeUtil.to_qlib(symbol) for symbol in frozen_symbols]
            if len(instruments) != len(set(instruments)):
                raise ValueError("Frozen factor universe contains duplicate symbols")
        else:
            instruments = alpha_router._resolve_instruments_for_universe(
                "CN", universe, tenant_id=tenant_id, user_id=user_id
            )
            # Qlib's D.instruments(market=...) returns a filter specification dict
            # (for example {market, filter_pipe}), not the constituent symbols.
            if isinstance(instruments, dict):
                from qlib.data import D

                instruments = D.list_instruments(instruments, as_list=True)
        if not instruments:
            raise RuntimeError("Factor universe resolved to no instruments")
    progress({"stage": "load_data", "progress_pct": 10})
    if factor_kind == "functional":
        from backend.services.engine.alpha_agent.materialization_runtime import (
            evaluate_functional_materialization,
        )

        progress({"stage": "factor_eval", "progress_pct": 35})
        output, input_evidence_snapshot, expected_materialization_dates = (
            evaluate_functional_materialization(
                factor_code,
                instruments,
                start=start,
                end=end,
                tenant_id=tenant_id,
                user_id=user_id,
                datasets=request.get("datasets"),
                lease=lease,
                budget=budget,
                lifecycle=lifecycle,
                should_cancel=should_cancel,
            )
        )
    elif factor_kind == "qlib_expression":
        progress({"stage": "factor_eval", "progress_pct": 35})
        if use_statistical_panel:
            from backend.services.engine.alpha_agent.materialization_runtime import (
                evaluate_statistical_materialization,
            )

            output, input_evidence_snapshot, expected_materialization_dates = (
                evaluate_statistical_materialization(
                    request,
                    candidate,
                    input_columns,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    start=start,
                    end=end,
                    should_cancel=should_cancel,
                    progress=progress,
                )
            )
        else:
            qlib_fields = [f"${column}" for column in input_columns]
            if request.get("price_source") == "quantdb_daily_forward":
                from backend.services.engine.alpha_agent.quantdb_price_panel import (
                    load_factor_price_panel,
                )

                panel = load_factor_price_panel(
                    instruments, start, end, warmup_bars=candidate.min_history
                )
                expected_materialization_dates = panel.attrs.get("trading_dates")
            else:
                from qlib.data import D
                from backend.services.engine.alpha_agent.factor_panel_inputs import (
                    panel_fetch_start,
                )

                fetch_start = panel_fetch_start(start, candidate.min_history, D)
                panel = D.features(
                    instruments,
                    qlib_fields,
                    start_time=fetch_start,
                    end_time=end,
                    freq="day",
                )
                if panel is None or panel.empty:
                    raise RuntimeError(
                        "Qlib returned no data for statistical factor inputs"
                    )
                if source_snapshot is not None:
                    expected_materialization_dates = (
                        pd.DatetimeIndex(
                            D.calendar(start_time=start, end_time=end, freq="day")
                        )
                        .strftime("%Y-%m-%d")
                        .tolist()
                    )
            panel = panel[qlib_fields]
            panel = panel.rename(
                columns=dict(zip(qlib_fields, input_columns, strict=True))
            )
            pieces = []
            if request.get("price_source") == "quantdb_daily_forward":
                from backend.services.engine.alpha_agent.factor_panel_inputs import (
                    input_evidence,
                )

                input_evidence_snapshot = input_evidence(
                    panel,
                    {"price_source": "quantdb_daily_forward", "scope": "price_panel"},
                )
            total_symbols = int(panel.index.get_level_values(0).nunique())
            for index, (_symbol, group) in enumerate(
                panel.groupby(level=0, sort=False), start=1
            ):
                if should_cancel is not None and should_cancel():
                    raise FactorMaterializationCancelled(
                        "Factor materialization was cancelled"
                    )
                values = candidate.compute(group)
                if not isinstance(values, pd.Series) or len(values) != len(group):
                    raise ValueError(
                        "Trusted statistical candidate returned invalid values"
                    )
                pieces.append(
                    pd.Series(values.to_numpy(), index=group.index, name="factor")
                )
                if index % 25 == 0 or index == total_symbols:
                    progress(
                        {
                            "stage": "factor_eval",
                            "progress_pct": 35 + int(2 * index / max(total_symbols, 1)),
                        }
                    )
            series = pd.concat(pieces).sort_index()
            dates_all = pd.to_datetime(
                series.index.get_level_values(1), errors="coerce"
            )
            keep = (dates_all >= pd.Timestamp(start)) & (dates_all <= pd.Timestamp(end))
            series = series.iloc[keep]
            symbols = series.index.get_level_values(0).map(StockCodeUtil.to_suffix)
            dates = pd.to_datetime(series.index.get_level_values(1), errors="coerce")
            output = pd.DataFrame(
                {
                    "symbol": symbols,
                    "trade_date": dates.strftime("%Y-%m-%d"),
                    "value": pd.to_numeric(series.to_numpy(), errors="coerce"),
                }
            )
    else:
        from backend.services.engine.alpha_agent.factor_codegen import panel_contract
        from backend.services.engine.alpha_agent.factor_panel_inputs import (
            enrich_panel,
        )
        from backend.services.engine.alpha_agent.quantdb_price_panel import (
            load_factor_price_panel,
        )

        contract = panel_contract(factor_code)
        output = None
        if request.get("price_source") == "quantdb_daily_forward":
            if contract is None:
                raise ValueError(
                    "Factor 未声明完整面板输入和预热窗口；请先恢复为声明 WARMUP_BARS 的因子版本"
                )
            from backend.services.engine.alpha_agent.materialization_window import (
                materialization_date_chunks,
            )

            ranges = materialization_date_chunks(
                start, end, max(len(instruments), 1), contract["warmup_bars"]
            )
            if len(ranges) == 1:
                frame = load_factor_price_panel(
                    instruments,
                    start,
                    end,
                    warmup_bars=contract["warmup_bars"],
                )
                expected_materialization_dates = frame.attrs.get("trading_dates")
            else:
                from backend.services.engine.alpha_agent.factor_panel_inputs import (
                    input_evidence,
                )
                from backend.services.engine.alpha_agent.materialization_runtime import (
                    run_factor_class_in_chunks,
                )

                evidence_contract = {
                    "price_source": "quantdb_daily_forward",
                    "scope": "materialization_panel",
                    "external_reads_verified": False,
                }
                pieces = []
                expected_materialization_dates = []
                digest = hashlib.sha256()
                total_rows = 0
                pandas_version = None
                progress({"stage": "factor_eval", "progress_pct": 35})
                for chunk_start, chunk_end in ranges:
                    frame = load_factor_price_panel(
                        instruments,
                        chunk_start,
                        chunk_end,
                        warmup_bars=contract["warmup_bars"],
                    )
                    expected_materialization_dates.extend(
                        day
                        for day in (frame.attrs.get("trading_dates") or [])
                        if chunk_start <= day <= chunk_end
                    )
                    frame = enrich_panel(frame, contract)
                    evidence = input_evidence(frame, evidence_contract)
                    digest.update(bytes.fromhex(evidence["panel_sha256"]))
                    total_rows += int(evidence["rows_including_warmup"])
                    pandas_version = evidence["pandas_version"]
                    pieces.append(
                        run_factor_class_in_chunks(
                            frame,
                            factor_code,
                            start=chunk_start,
                            end=chunk_end,
                            warmup=contract["warmup_bars"],
                            tenant_id=tenant_id,
                            user_id=user_id,
                            datasets=request.get("datasets"),
                            lease=lease,
                            budget=budget,
                            lifecycle=lifecycle,
                            should_cancel=should_cancel,
                        )
                    )
                input_evidence_snapshot = {
                    "contract": evidence_contract,
                    "rows_including_warmup": total_rows,
                    "pandas_version": pandas_version,
                    "panel_sha256": digest.hexdigest(),
                    "load_chunks": len(ranges),
                }
                output = (
                    pd.concat(pieces, ignore_index=True)
                    if pieces
                    else pd.DataFrame(columns=["symbol", "trade_date", "value"])
                )
        else:
            from qlib.data import D
            from backend.services.engine.alpha_agent.factor_panel_inputs import (
                panel_fetch_start,
            )

            fetch_start = (
                panel_fetch_start(start, contract["warmup_bars"], D)
                if contract
                else start
            )
            fields = ["$open", "$high", "$low", "$close", "$volume", "$factor"]
            if contract is not None or "quantdb_data" in factor_code:
                fields.append("$amount")
            frame = D.features(
                instruments, fields, start_time=fetch_start, end_time=end, freq="day"
            )
            if frame is None or frame.empty:
                raise RuntimeError("Qlib returned no data for factor materialization")
            if source_snapshot is not None:
                expected_materialization_dates = (
                    pd.DatetimeIndex(
                        D.calendar(start_time=start, end_time=end, freq="day")
                    )
                    .strftime("%Y-%m-%d")
                    .tolist()
                )
        if output is None:
            if contract is not None:
                frame = enrich_panel(frame, contract)
            if request.get("price_source") == "quantdb_daily_forward":
                from backend.services.engine.alpha_agent.factor_panel_inputs import (
                    input_evidence,
                )

                input_evidence_snapshot = input_evidence(
                    frame,
                    {
                        "price_source": "quantdb_daily_forward",
                        "scope": "materialization_panel",
                        "external_reads_verified": False,
                    },
                )
            progress({"stage": "factor_eval", "progress_pct": 35})
            from backend.services.engine.alpha_agent.materialization_runtime import (
                run_factor_class_in_chunks,
            )

            output = run_factor_class_in_chunks(
                frame,
                factor_code,
                start=start,
                end=end,
                warmup=contract["warmup_bars"] if contract else 0,
                tenant_id=tenant_id,
                user_id=user_id,
                datasets=request.get("datasets"),
                lease=lease,
                budget=budget,
                lifecycle=lifecycle,
                should_cancel=should_cancel,
            )
    if expected_materialization_dates is not None:
        _validate_materialization_coverage(output, expected_materialization_dates)
    output = output.dropna(subset=["trade_date"]).replace(
        [float("inf"), float("-inf")], pd.NA
    )
    if output.duplicated(["symbol", "trade_date"]).any():
        raise ValueError("Materialized factor has duplicate symbol/date keys")
    feature_key = f"factor_{hashlib.sha256(factor_id.encode()).hexdigest()[:16]}"
    output = output.rename(columns={"value": feature_key})
    progress({"stage": "factor_calculated", "progress_pct": 38})
    generation = owner_segment(generation_id)
    users_root = quantcustom_root() / "users"
    relative_prefix = (
        Path(
            "factor_serving"
            if request.get("serving_only") is True
            else "factor_generations"
        )
        / generation
        / "6_ml_datasets"
        / "rd_mined"
    )
    byte_count = 0
    row_count = 0
    grouped = output.groupby("trade_date", sort=True)
    total = max(len(grouped), 1)
    written_partitions: list[tuple[Path, Path]] = []

    def discard_written_partitions() -> None:
        from backend.services.engine.alpha_agent.materialization_store import (
            release_partition,
        )

        for written_path, relative_path in reversed(written_partitions):
            release_partition(tenant_id, user_id, written_path, relative_path)

    committed = False
    try:
        for index, (trade_date, daily) in enumerate(grouped, start=1):
            if should_cancel is not None and should_cancel():
                raise FactorMaterializationCancelled(
                    "Factor materialization was cancelled"
                )
            partition = pd.DataFrame(
                {
                    "symbol": daily["symbol"].to_numpy(),
                    "trade_date": daily["trade_date"].to_numpy(),
                    feature_key: daily[feature_key].to_numpy(),
                }
            )
            buffer = io.BytesIO()
            partition.to_parquet(buffer, index=False, engine="pyarrow")
            content = buffer.getvalue()
            relative_path = (
                relative_prefix
                / f"dt={pd.Timestamp(trade_date):%Y%m%d}"
                / "data.parquet"
            )
            path = write_quota_managed_local_file(
                root=users_root,
                tenant_id=tenant_id,
                user_id=user_id,
                relative_path=relative_path,
                content=content,
                resource_namespace="user_factor_datasets",
            )
            written_partitions.append((path, relative_path))
            byte_count += path.stat().st_size
            row_count += len(partition)
            progress(
                {
                    "stage": "persist_partitions",
                    "progress_pct": 40 + int(55 * index / total),
                }
            )
        if should_cancel is not None and should_cancel():
            raise FactorMaterializationCancelled("Factor materialization was cancelled")
        if row_count <= 0:
            raise RuntimeError("Factor produced no persistable rows")
        result: dict[str, Any] = {
            "status": "materialized",
            "generation_id": generation,
            "dataset_key": "rd_mined",
            "factor_id": factor_id,
            "factor_revision_no": factor_revision_no,
            "factor_kind": factor_kind,
            "feature_key": feature_key,
            "factor_code_sha256": expected_hash,
            "row_count": row_count,
            "byte_count": byte_count,
            "partition_count": len(grouped),
            "universe": universe,
            "start_date": start,
            "end_date": end,
            "input_evidence": input_evidence_snapshot,
            "library_gate": {
                "corr": None,
                "corr_against": None,
                "evidence": {
                    "automatic_check": "skipped",
                    "deduplication": "manual",
                },
            },
        }
        if request.get("serving_only") is True:
            result["serving_snapshot"] = {
                "source_generation_id": request["source_generation_id"],
                "trade_date": start,
                "formula_sha256": expected_hash,
                "partition_sha256": hashlib.sha256(content).hexdigest(),
            }
        committed = True
        return result
    finally:
        if not committed:
            discard_written_partitions()


def execute_factor_batch(
    request: dict[str, Any],
    *,
    tenant_id: str,
    user_id: str,
    lease,
    budget,
    lifecycle: dict[str, Any],
    progress: Callable[[dict[str, Any]], None],
    panel_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run a frozen batch under one owner-scoped lease and cancellation boundary."""
    if not isinstance(request, dict):
        raise ValueError("Factor batch request snapshot is invalid")
    items = request.get("factor_items")
    factor_ids = request.get("factor_ids")
    batch_id = request.get("batch_id")
    if (
        not isinstance(items, list)
        or not 2 <= len(items) <= 20
        or not isinstance(factor_ids, list)
        or len(factor_ids) != len(items)
        or not isinstance(batch_id, str)
        or len(batch_id) != 32
        or any(
            not isinstance(factor_id, str) or not factor_id for factor_id in factor_ids
        )
        or len(set(factor_ids)) != len(factor_ids)
        or any(not isinstance(item, dict) for item in items)
        or [item.get("factor_id") for item in items] != factor_ids
    ):
        raise ValueError("Factor batch manifest is invalid")

    results: list[dict[str, Any]] = []
    total = len(items)
    for index, item in enumerate(items):
        lifecycle["create_started"] = False
        lifecycle["confirmed_stopped"] = False

        def report_factor(
            snapshot: dict[str, Any],
            *,
            factor_index: int = index,
            factor_item: dict[str, Any] = item,
        ) -> None:
            local_progress = snapshot.get("progress_pct", 0)
            local_progress = (
                float(local_progress)
                if isinstance(local_progress, (int, float))
                else 0.0
            )
            progress(
                {
                    **snapshot,
                    "stage": f"factor_{factor_index + 1}_of_{total}",
                    "factor_id": factor_item["factor_id"],
                    "factor_index": factor_index + 1,
                    "factor_count": total,
                    "progress_pct": min(
                        99,
                        int(((factor_index + local_progress / 100) / total) * 100),
                    ),
                }
            )

        result = execute_factor_backtest(
            {**item, "_series_max_points": 300},
            tenant_id=tenant_id,
            user_id=user_id,
            lease=lease,
            budget=budget,
            lifecycle=lifecycle,
            progress=report_factor,
            panel_callback=panel_callback,
        )
        results.append(result)
        lifecycle["create_started"] = False
        lifecycle["confirmed_stopped"] = True
        progress(
            {
                "stage": f"factor_{index + 1}_of_{total}_completed",
                "factor_id": item["factor_id"],
                "factor_index": index + 1,
                "factor_count": total,
                "progress_pct": int(((index + 1) / total) * 100),
            }
        )

    return {
        "batch_id": batch_id,
        "factor_ids": factor_ids,
        "factor_count": total,
        "results": results,
    }


def execute_factor_combo(
    request: dict[str, Any],
    *,
    tenant_id: str,
    user_id: str,
    progress: Callable[[dict[str, Any]], None],
    should_cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Optimize weights from exact, owner-scoped private panel versions."""
    from backend.services.engine.alpha_agent.combo_optimizer import (
        ComboConfig,
        optimize_combo,
    )
    from backend.services.engine.alpha_agent.factor_pool_panels import read_panel

    if (
        not isinstance(request, dict)
        or request.get("market") != "a_share"
        or not isinstance(request.get("universe"), str)
        or not request["universe"]
    ):
        raise ValueError("Factor combination request scope is invalid")
    factor_ids = request.get("factor_ids")
    items = request.get("factor_items")
    if (
        not isinstance(factor_ids, list)
        or not 2 <= len(factor_ids) <= 12
        or len(set(factor_ids)) != len(factor_ids)
        or not isinstance(items, list)
        or [item.get("factor_id") for item in items if isinstance(item, dict)]
        != factor_ids
    ):
        raise ValueError("Factor combination member snapshot is invalid")
    config_data = request.get("config")
    expected_config = {
        "max_days",
        "train_ratio",
        "popsize",
        "maxiter",
        "tolerance",
        "seed",
        "time_budget_seconds",
        "cost_rate",
    }
    if not isinstance(config_data, dict) or set(config_data) != expected_config:
        raise ValueError("Factor combination optimizer config is invalid")
    try:
        config = ComboConfig(**config_data)
    except (TypeError, ValueError) as exc:
        raise ValueError("Factor combination optimizer config is invalid") from exc

    frames = {}
    total = len(items)
    for index, item in enumerate(items):
        factor_id = item.get("factor_id")
        revision_no = item.get("factor_revision_no")
        panel_version = item.get("panel_version")
        if (
            type(revision_no) is not int
            or revision_no < 1
            or not isinstance(panel_version, str)
            or len(panel_version) != 64
            or any(char not in "0123456789abcdef" for char in panel_version)
        ):
            raise ValueError("Factor combination source version is invalid")
        frame = read_panel(
            tenant_id,
            user_id,
            "a_share",
            factor_id,
            expected_version=panel_version,
        )
        if frame is None or frame.empty or "forward_return" not in frame:
            raise ValueError(
                f"Factor {factor_id} panel is missing or lacks aligned forward returns"
            )
        frames[factor_id] = frame
        progress(
            {
                "stage": "load_private_panels",
                "factor_index": index + 1,
                "factor_count": total,
                "progress_pct": min(8, int(8 * (index + 1) / total)),
            }
        )

    progress({"stage": "validate_panel_intersection", "progress_pct": 10})
    result = optimize_combo(
        frames,
        factor_ids,
        config,
        should_cancel=should_cancel,
        progress=progress,
    )
    result["market"] = "a_share"
    result["universe"] = request["universe"]
    result["name"] = str(request.get("name") or "")[:128]
    result["factor_revisions"] = {
        item["factor_id"]: item["factor_revision_no"] for item in items
    }
    result["panel_versions"] = {
        item["factor_id"]: item["panel_version"] for item in items
    }
    return result
