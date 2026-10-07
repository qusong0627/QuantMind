"""
Phase 0 · QuantDB 数据口径适配层。

当前 QuantDB 本地 parquet（D:\\quant_data）各数据源列名不完全统一：
  - daily_unadjusted / index_daily / valuation : 小写 ``symbol`` + ``time``
  - technical_indicators                        : 部分分区大写 ``Symbol``，最新分区小写 ``symbol``
  - l2_factors                                  : 小写 ``symbol`` + ``date`` + ``flow_*`` 资金流（partition 注入 ``dt``）
  - sector_concept/sector_members.parquet       : ``SectorCode/ SectorName/ SectorType/ Symbol``

本层把上层（snapshot 计算 / 实时 feed）需要的列统一归一：
  - 标的 ID 一律小写后缀（``000001.SZ``）
  - 交易日一律为 partition 注入的 ``dt``（``YYYYMMDD``）
  - 提供 DuckDB 规范视图：qdb_daily_unadjusted / qdb_index_daily / qdb_l2_factors / qdb_technical_indicators

这样上层 SQL 无需感知分区间的大小写差异，跨分区读也安全（用 COALESCE 合并 Symbol/symbol）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import duckdb
import pandas as pd

# 默认数据根：环境变量优先，其次 D:\\quant_data
DEFAULT_DATA_DIR = Path(os.getenv("QM_QUANTDB_DATA_DIR", r"D:\quant_data"))

# 每个数据源 → 相对目录（compute 与 feed 共用）
DATASET_DIRS = {
    "daily_unadjusted": "1_kline_data/daily_unadjusted",
    "index_daily": "1_kline_data/index_daily",
    "technical_indicators": "5_technical_derived/technical_indicators",
    "l2_factors": "6_ml_datasets/l2_factors",
    "valuation": "5_technical_derived/valuation",
    "sector_members": "2_base_sector/sector_concept/sector_members.parquet",
    "instrument_list": "2_base_sector/instrument_detail/instrument_list.parquet",
}

# l2_factors 资金流必要列（供上层直接选取，缺失时降级）
FLOW_COLUMNS = [
    "symbol", "dt", "close", "amount",
    "flow_net_amount", "flow_buy_amount", "flow_sell_amount", "flow_net_ratio",
    "flow_super_net", "flow_large_net", "flow_medium_net", "flow_small_net",
    "flow_large_ratio", "flow_medium_ratio", "flow_small_ratio",
    "flow_money_flow_index",
]


def _partition_glob(data_dir: Path, rel: str) -> str:
    """分区目录 glob：{dir}/dt=*/*.parquet（posix 分隔，DuckDB 用）。"""
    return (str(data_dir / rel / "dt=*" / "*.parquet").replace("\\", "/"))


def _available(data_dir: Path, rel: str) -> bool:
    d = data_dir / rel
    if not d.is_dir():
        return False
    return any(p.is_dir() and p.name.startswith("dt=") for p in d.iterdir())


def _parquet_columns(con: duckdb.DuckDBPyConnection, path_glob: str) -> set[str]:
    """探测 parquet（含 dt=* 分区 glob）实际列名；失败返回空集合。"""
    try:
        cols = con.execute(
            f"SELECT * FROM read_parquet('{path_glob}',"
            " hive_partitioning=1, union_by_name=true) LIMIT 0"
        ).description
        return {c[0] for c in cols} if cols else set()
    except Exception:  # noqa: BLE001
        return set()


def create_views(con: duckdb.DuckDBPyConnection, data_dir: Path) -> duckdb.DuckDBPyConnection:
    """在给定 DuckDB 连接上挂好一套规范的视图（幂等）。"""
    # 日线不复权（小写 symbol + time）。
    # 注意：`vol_in_stock` 是 `volume` 的旧别名（见 quantdb_hub._VOLUME_ALIASES），
    # 魔搭等精简 schema 只给 `volume`，缺失时用 volume 兜底，避免 Binder Error。
    if _available(data_dir, DATASET_DIRS["daily_unadjusted"]):
        glob = _partition_glob(data_dir, DATASET_DIRS["daily_unadjusted"])
        cols = _parquet_columns(con, glob)
        vol_expr = "vol_in_stock" if "vol_in_stock" in cols else "volume AS vol_in_stock"
        con.execute(f"""
            CREATE VIEW IF NOT EXISTS qdb_daily_unadjusted AS
            SELECT symbol, dt, time, open, high, low, close, volume, amount, {vol_expr}
            FROM read_parquet('{glob}', hive_partitioning=1, union_by_name=true)
        """)
    # 指数日线
    if _available(data_dir, DATASET_DIRS["index_daily"]):
        con.execute(f"""
            CREATE VIEW IF NOT EXISTS qdb_index_daily AS
            SELECT symbol, dt, time, open, high, low, close, volume, amount, Category
            FROM read_parquet('{_partition_glob(data_dir, DATASET_DIRS["index_daily"])}',
                              hive_partitioning=1, union_by_name=true)
        """)
    # 技术指标：跨分区合并 Symbol/symbol 大小写差异
    if _available(data_dir, DATASET_DIRS["technical_indicators"]):
        con.execute(f"""
            CREATE VIEW IF NOT EXISTS qdb_technical_indicators AS
            SELECT COALESCE(symbol, "Symbol") AS symbol,
                   dt, time, close, pct_change,
                   return_1d, return_3d, return_5d, return_10d, return_20d, return_60d
            FROM read_parquet('{_partition_glob(data_dir, DATASET_DIRS["technical_indicators"])}',
                              hive_partitioning=1, union_by_name=true)
        """)
    # L2 资金流：只投影必要列（symbol + 资金流字段）
    if _available(data_dir, DATASET_DIRS["l2_factors"]):
        present_cols = _l2_available_flow_cols(con, data_dir)
        select_cols = [c for c in FLOW_COLUMNS if c in present_cols] or ["symbol", "dt", "close"]
        con.execute(f"""
            CREATE VIEW IF NOT EXISTS qdb_l2_factors AS
            SELECT {", ".join(select_cols)}
            FROM read_parquet('{_partition_glob(data_dir, DATASET_DIRS["l2_factors"])}',
                              hive_partitioning=1, union_by_name=true)
        """)
    # 估值（板块总市值用）——按需读，供 compute 引用
    if _available(data_dir, DATASET_DIRS["valuation"]):
        con.execute(f"""
            CREATE VIEW IF NOT EXISTS qdb_valuation AS
            SELECT symbol, dt, total_mv
            FROM read_parquet('{_partition_glob(data_dir, DATASET_DIRS["valuation"])}',
                              hive_partitioning=1, union_by_name=true)
        """)
    return con


def _l2_available_flow_cols(con: duckdb.DuckDBPyConnection, data_dir: Path) -> set[str]:
    """探测最新 l2 分区实际有哪些列（避免引用不存在的 flow_* 报错）。"""
    try:
        files = sorted((data_dir / DATASET_DIRS["l2_factors"]).glob("dt=*/data.parquet"))
        if not files:
            return set()
        rel = str(files[-1]).replace("\\", "/")
        cols = con.execute(f"SELECT * FROM read_parquet('{rel}') LIMIT 0").description
        return {c[0] for c in cols} if cols else set()
    except Exception:
        return set()


def get_conn(data_dir: Optional[Path] = None) -> duckdb.DuckDBPyConnection:
    """创建内存 DuckDB 连接并把规范视图挂载好（Phase 0 入口）。"""
    data_dir = Path(data_dir) if data_dir else DEFAULT_DATA_DIR
    con = duckdb.connect(":memory:")
    return create_views(con, data_dir)


def q(con: duckdb.DuckDBPyConnection, sql: str) -> pd.DataFrame:
    """执行查询，失败返回空表（与上层 _q 语义一致）。"""
    try:
        return con.execute(sql).fetchdf()
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] query failed: {exc}\n  sql={sql[:200]}", file=__import__("sys").stderr)
        return pd.DataFrame()


if __name__ == "__main__":
    con = get_conn()
    print("挂载视图完成；可用表:", [r[0] for r in con.execute("SHOW TABLES").fetchall()])