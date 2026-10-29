"""散户视角日频序列：散户（中单+小单）净额、广度、追涨相关。

核心反向量：``retail_chase_corr`` = 当日个股「散户净额/成交额」与「过去 5 日涨跌幅」
的截面相关。高 = 散户在追近期赢家（情绪过热，未来偏弱）；低 = 散户不追/卖涨
（情绪冷，未来偏强）。这是 2022-2026 全史验证里唯一稳定的反向散户指标；
散户净流入/广度本身更像同步动量，不单独做反向。

L2 金额按分区混存（元/万元），按日判定单位后统一为元。
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


def load_retail_force(
    data_dir: str | Path | None = None,
    days: int = 1600,
) -> pd.DataFrame:
    """返回近 ``days`` 个自然日的散户/主力日频序列（索引为交易日）。"""
    if duckdb is None:
        return pd.DataFrame()
    root = resolve_data_dir(data_dir)
    glob = str(root / L2_GLOB)
    cutoff = (datetime.now() - timedelta(days=max(days, 60))).strftime("%Y%m%d")
    con = duckdb.connect()
    try:
        df = con.execute(
            """
            WITH l2 AS (
              SELECT dt, symbol, close, amount,
                     coalesce(flow_medium_net,0)+coalesce(flow_small_net,0) AS retail_raw,
                     coalesce(flow_super_net,0)+coalesce(flow_large_net,0) AS main_raw,
                     flow_net_amount, flow_net_ratio
              FROM read_parquet(?)
              WHERE dt >= ?
            ),
            scaled AS (
              SELECT dt,
                     CASE WHEN coalesce(median(abs(flow_net_amount/nullif(amount,0)-flow_net_ratio)),1e9)
                               < coalesce(median(abs(flow_net_amount/nullif(amount*1e4,0)-flow_net_ratio)),1e9)
                          THEN 1e4 ELSE 1.0 END AS scale
              FROM l2 GROUP BY dt
            ),
            rows AS (
              SELECT l.dt,
                     l.close,
                     l.amount*1e4 AS amt_yuan,
                     l.retail_raw*s.scale AS retail_yuan,
                     l.main_raw*s.scale AS main_yuan,
                     l.close / lag(l.close,5) OVER (PARTITION BY l.symbol ORDER BY l.dt) - 1 AS r5
              FROM l2 l JOIN scaled s USING(dt)
            )
            SELECT dt,
                   sum(retail_yuan)/nullif(sum(amt_yuan),0) AS retail_ratio,
                   avg(CASE WHEN retail_yuan > 0 THEN 1.0 ELSE 0.0 END) AS retail_breadth,
                   sum(CASE WHEN retail_yuan > 0 THEN amt_yuan ELSE 0 END)/nullif(sum(amt_yuan),0) AS retail_breadth_amt,
                   sum(main_yuan)/nullif(sum(amt_yuan),0) AS main_ratio,
                   avg(CASE WHEN main_yuan > 0 THEN 1.0 ELSE 0.0 END) AS main_breadth,
                   sum(retail_yuan)/1e8 AS retail_net_yi,
                   corr(retail_yuan/nullif(amt_yuan,0), r5) AS retail_chase_corr,
                   sum(CASE WHEN r5 >= 0.05 THEN retail_yuan ELSE 0 END)/nullif(sum(amt_yuan),0) AS retail_chase,
                   sum(CASE WHEN r5 <= -0.05 THEN retail_yuan ELSE 0 END)/nullif(sum(amt_yuan),0) AS retail_panic
            FROM rows GROUP BY dt ORDER BY dt
            """,
            [glob, cutoff],
        ).df()
    finally:
        con.close()
    if df.empty:
        return pd.DataFrame()
    df["dt"] = pd.to_datetime(df["dt"].astype(str))
    df = df.set_index("dt").sort_index()
    df["divergence"] = df["main_ratio"] - df["retail_ratio"]
    for c in df.columns:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df[
        [
            "retail_ratio",
            "retail_breadth",
            "retail_breadth_amt",
            "main_ratio",
            "main_breadth",
            "retail_net_yi",
            "retail_chase_corr",
            "retail_chase",
            "retail_panic",
            "divergence",
        ]
    ]


def latest_retail_block(df: pd.DataFrame) -> dict:
    if df.empty:
        return {}
    row = df.iloc[-1]
    out = {"date": df.index[-1].strftime("%Y-%m-%d")}
    for c in df.columns:
        out[c] = None if not np.isfinite(row[c]) else round(float(row[c]), 4)
    return out
