"""Public QuantDB enrichment for the sandbox's explicit full-panel contract."""

from __future__ import annotations


def panel_fetch_start(start: str, bars: int, provider) -> str:
    import pandas as pd

    if not bars:
        return start
    calendar = pd.DatetimeIndex(
        provider.calendar(start_time="1900-01-01", end_time=start, freq="day")
    )
    if calendar.empty:
        raise ValueError("Qlib warmup calendar is unavailable")
    position = calendar.searchsorted(pd.Timestamp(start), side="left")
    return pd.Timestamp(calendar[max(0, position - bars)]).strftime("%Y-%m-%d")


def enrich_panel(frame, contract: dict):
    import pandas as pd
    from backend.shared.stock_utils import StockCodeUtil
    from backend.services.engine.alpha_agent.factor_codegen import feature_catalog
    from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub
    from backend.services.engine.rd_agent.mining_datasets import DATASET_REL_PATHS
    from backend.services.engine.rd_agent.rd_loop_wrapper import RDLoopWrapper

    if len(frame) > 1_000_000:
        raise ValueError("Full-panel input exceeds 1,000,000 rows")
    if not contract["features"]:
        return frame
    catalog = feature_catalog()
    dates = sorted(
        pd.DatetimeIndex(frame.index.get_level_values(1)).strftime("%Y%m%d").unique()
    )
    if len(dates) > 3000:
        raise ValueError("Full-panel input exceeds 3,000 trading dates")
    symbols = sorted(
        {StockCodeUtil.to_suffix(value) for value in frame.index.get_level_values(0)}
    )
    hub = QuantDBDataHub(RDLoopWrapper._resolve_quantdb_dir())
    frame = frame.copy()
    for dataset in DATASET_REL_PATHS:
        selected = {
            catalog[name][1]: name
            for name in contract["features"]
            if catalog[name][0] == dataset
        }
        if not selected:
            continue
        rel = DATASET_REL_PATHS[dataset]
        latest = hub.latest_partition_date(rel)
        available = hub.partition_columns(rel, latest) if latest else set()
        if not {"symbol", *selected}.issubset(available):
            raise ValueError(f"Required {dataset} input columns are unavailable")
        # Identifiers come exclusively from the canonical, server-owned map.
        columns = ",".join(['"symbol"', *['"' + name + '"' for name in selected]])
        raw = hub._read_partitioned(
            rel,
            dates,
            cols=columns,
            where_sql="symbol IN (" + ",".join("?" for _ in symbols) + ")",
            bind=symbols,
        )
        if raw.empty or not {"symbol", "dt", *selected}.issubset(raw.columns):
            raise ValueError(f"Required {dataset} input data is unavailable")
        raw["instrument"] = raw["symbol"].map(StockCodeUtil.to_qlib)
        raw["datetime"] = pd.to_datetime(
            raw["dt"].astype(str), format="%Y%m%d", errors="raise"
        )
        raw = raw.set_index(["instrument", "datetime"])
        if raw.index.has_duplicates:
            raise ValueError("Public feature input has duplicate symbol/date rows")
        for column, alias in selected.items():
            frame["$" + alias] = pd.to_numeric(raw[column], errors="coerce").reindex(
                frame.index
            )
            if frame["$" + alias].notna().sum() == 0:
                raise ValueError(f"Required feature {alias} has no observations")
    return frame


def input_evidence(frame, contract: dict) -> dict:
    import hashlib
    import json
    import pandas as pd

    digest = hashlib.sha256(json.dumps(list(frame.columns)).encode())
    digest.update(pd.util.hash_pandas_object(frame, index=True).values.tobytes())
    return {
        "contract": contract,
        "rows_including_warmup": len(frame),
        "pandas_version": pd.__version__,
        "panel_sha256": digest.hexdigest(),
    }
