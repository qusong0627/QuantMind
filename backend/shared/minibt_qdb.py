# -*- coding: utf-8 -*-
"""QuantDB → minibt KLine 数据适配器。

在 ai-ide 的 minibt 运行时容器内使用(backend/ 卷随容器挂载于 /app/backend,
PYTHONPATH=/app),也可在引擎/本地直接调用。只依赖 duckdb/pandas 与
backend.shared 的纯工具模块,不引入 fastapi/qlib。

口径说明:
- daily_forward 为前复权价格;amount/volume 未复权(单位: 元、股)
- minibt get_kline 对缺失的 price_tick/volume_multiple 自动补 0.01/1.0,
  恰为 A 股口径,此处不预填
"""
from __future__ import annotations

import os
from datetime import date, datetime

import pandas as pd

from backend.shared.quantdb_datasets import get_dataset_spec

_DEFAULT_DATA_DIR = "/data/quantdb"
_DAILY_COLUMNS = ["datetime", "open", "high", "low", "close", "volume", "amount"]


def _to_date_str(value: str | date | datetime | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text[:10]


def load_daily(
    symbol: str,
    start: str | date | datetime | None = None,
    end: str | date | datetime | None = None,
    *,
    dataset: str = "daily_forward",
    data_dir: str | None = None,
) -> pd.DataFrame:
    """读取 QuantDB 日线 parquet,输出 minibt get_kline 所需的 DataFrame。

    Args:
        symbol: 股票代码,'600036' / 'SH600036' / '600036.SH' 均可(内部归一为后缀式)
        start/end: 起止日期,'YYYYMMDD' 或 'YYYY-MM-DD';None 表示不限制
        dataset: QuantDB 数据集名,默认 daily_forward(前复权);
            传 daily_unadjusted/daily_backward 可切换复权口径
        data_dir: 显式数据根目录;默认环境变量 QM_QUANTDB_DATA_DIR(容器内 /data/quantdb)

    Returns:
        DataFrame[datetime, open, high, low, close, volume, amount],按时间升序。
        空结果返回零行 DataFrame(列齐全),由调用方决定报错或放行。
    """
    from backend.shared.stock_utils import StockCodeUtil

    suffix_code = StockCodeUtil.to_suffix(symbol)
    if not suffix_code:
        raise ValueError(f"无法识别的股票代码: {symbol!r}")

    root = data_dir or os.getenv("QM_QUANTDB_DATA_DIR", _DEFAULT_DATA_DIR)
    rel_dir = get_dataset_spec(dataset).rel_dir
    glob_path = os.path.join(root, rel_dir, "dt=*", "data.parquet")
    if not os.path.isdir(os.path.join(root, rel_dir)):
        raise FileNotFoundError(
            f"QuantDB 数据目录不存在: {os.path.join(root, rel_dir)} "
            f"(检查 QM_QUANTDB_DATA_DIR={root!r} 与 data 卷挂载)"
        )

    import duckdb

    start_str = _to_date_str(start)
    end_str = _to_date_str(end)
    dt_bounds = ""
    params: list[str] = [suffix_code]
    if start_str:
        dt_bounds += " AND dt >= ?"
        params.append(start_str.replace("-", ""))
    if end_str:
        dt_bounds += " AND dt <= ?"
        params.append(end_str.replace("-", ""))

    # hive_partitioning 暴露 dt 分区列,dt 谓词触发分区裁剪
    query = f"""
        SELECT time, open, high, low, close, volume, amount
        FROM read_parquet('{glob_path}', hive_partitioning=true)
        WHERE symbol = ?{dt_bounds}
        ORDER BY time
    """
    con = duckdb.connect()
    try:
        df = con.execute(query, params).df()
    finally:
        con.close()

    if df.empty:
        empty = pd.DataFrame(columns=_DAILY_COLUMNS)
        empty["datetime"] = pd.to_datetime(empty["datetime"])
        return empty

    df["datetime"] = pd.to_datetime(df.pop("time"))
    return df[_DAILY_COLUMNS].reset_index(drop=True)
