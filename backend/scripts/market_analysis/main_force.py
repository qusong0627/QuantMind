"""主力视角日频序列：L2 超大单+大单净额、主力广度、成交额加权广度。

QuantDB l2_factors 的 flow 金额按分区混存（元/万元），本模块按日判定单位后
统一为元；``amount`` 恒为万元。输出仅用于大盘情绪面板的「主力视角」曲线，
不参与评分/预测；所有指标当下可得，无前视。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

try:  # DuckDB 可能只在分析容器内安装
    import duckdb
except Exception:  # pragma: no cover
    duckdb = None  # type: ignore[assignment]

L2_GLOB = "6_ml_datasets/l2_factors/dt=*/*.parquet"


def resolve_data_dir(data_dir: str | Path | None = None) -> Path:
    if data_dir:
        return Path(data_dir)
    return Path(os.getenv("QM_QUANTDB_DATA_DIR", "data/quantdb"))


def load_main_force(
    data_dir: str | Path | None = None,
    days: int = 400,
) -> pd.DataFrame:
    """返回近 ``days`` 个自然日的主力视角日频序列（索引为交易日）。"""
    if duckdb is None:
        return pd.DataFrame()
    root = resolve_data_dir(data_dir)
    glob = str(root / L2_GLOB)
    cutoff = (datetime.now() - timedelta(days=max(days, 30))).strftime("%Y%m%d")
    con = duckdb.connect()
    try:
        df = con.execute(
            """
            WITH base AS (
              SELECT dt,
                     sum(coalesce(flow_super_net,0)+coalesce(flow_large_net,0)) AS main_raw,
                     sum(amount) AS amt_wan,
                     avg(CASE WHEN coalesce(flow_super_net,0)+coalesce(flow_large_net,0) > 0
                              THEN 1.0 ELSE 0.0 END) AS main_breadth,
                     sum(CASE WHEN coalesce(flow_super_net,0)+coalesce(flow_large_net,0) > 0
                              THEN amount ELSE 0 END) / nullif(sum(amount),0) AS main_breadth_amt,
                     median(abs(flow_net_amount / nullif(amount,0) - flow_net_ratio)) AS err_wan,
                     median(abs(flow_net_amount / nullif(amount*1e4,0) - flow_net_ratio)) AS err_yuan
              FROM read_parquet(?)
              WHERE dt >= ?
              GROUP BY dt
            )
            SELECT dt, main_raw, amt_wan, main_breadth, main_breadth_amt, err_wan, err_yuan,
                   CASE WHEN coalesce(err_wan, 1e9) < coalesce(err_yuan, 1e9)
                        THEN 1e4 ELSE 1.0 END AS scale
            FROM base ORDER BY dt
            """,
            [glob, cutoff],
        ).df()
    finally:
        con.close()
    if df.empty:
        return pd.DataFrame()
    df["dt"] = pd.to_datetime(df["dt"].astype(str))
    df = df.set_index("dt").sort_index()
    df["main_net_yuan"] = pd.to_numeric(df["main_raw"], errors="coerce") * df["scale"]
    df["amt_yuan"] = pd.to_numeric(df["amt_wan"], errors="coerce") * 1e4
    df["main_net_ratio"] = df["main_net_yuan"] / df["amt_yuan"].where(df["amt_yuan"] > 0)
    df["main_net_yi"] = df["main_net_yuan"] / 1e8
    df["turnover_yi"] = df["amt_yuan"] / 1e8
    mean20 = df["main_net_ratio"].rolling(20, min_periods=10).mean()
    std20 = df["main_net_ratio"].rolling(20, min_periods=10).std()
    df["main_net_z20"] = (df["main_net_ratio"] - mean20) / std20.where(std20 > 0)
    for c in ("main_breadth", "main_breadth_amt", "main_net_ratio", "main_net_z20"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df[
        [
            "main_net_yi",
            "turnover_yi",
            "main_net_ratio",
            "main_net_z20",
            "main_breadth",
            "main_breadth_amt",
        ]
    ]


def latest_block(df: pd.DataFrame) -> dict:
    if df.empty:
        return {}
    row = df.iloc[-1]
    return {
        "date": df.index[-1].strftime("%Y-%m-%d"),
        "main_net_yi": None if not np.isfinite(row["main_net_yi"]) else round(float(row["main_net_yi"]), 2),
        "main_net_ratio": None if not np.isfinite(row["main_net_ratio"]) else round(float(row["main_net_ratio"]), 4),
        "main_net_z20": None if not np.isfinite(row["main_net_z20"]) else round(float(row["main_net_z20"]), 3),
        "main_breadth": None if not np.isfinite(row["main_breadth"]) else round(float(row["main_breadth"]), 4),
        "main_breadth_amt": None if not np.isfinite(row["main_breadth_amt"]) else round(float(row["main_breadth_amt"]), 4),
    }
