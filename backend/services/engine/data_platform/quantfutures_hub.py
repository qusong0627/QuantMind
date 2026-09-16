"""QuantFutures 数据中枢 — 期货/贵金属本地 parquet 读取的单一入口。

复用 QuantDBDataHub 的查询基础设施（DuckDB 连接管理、视图挂载、K线/列
标准化），仅替换数据目录与视图命名空间（qfut_*），避免与其他市场视图串扰。

数据目录：环境变量 QM_QUANTFUTURES_DATA_DIR，默认 data/quantfutures/。
目录结构与 QuantDB 对齐（日线 / 实时快照）。

数据段:
  1_kline_data/daily_forward         — 期货/贵金属日K（国际 CL.FUT / 国内 RB0.CN / 上金所 Au99.99）
  2_base_sector/futures_realtime     — 实时行情快照（国际/国内期货）
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from backend.services.engine.data_platform.quantdb_hub import (
    QuantDBDataHub,
    _dt_conditions,
)

logger = logging.getLogger(__name__)

_QUANTFUTURES_DATA_DIR_ENV = "QM_QUANTFUTURES_DATA_DIR"
_QUANTFUTURES_DEFAULT_DATA_DIRS = [
    "/data/quantfutures",  # Docker 容器内（挂载点）
    str(Path(__file__).resolve().parents[4] / "data" / "quantfutures"),  # 项目根/data/quantfutures
]


def _resolve_quantfutures_data_dir() -> Path:
    env_val = os.getenv(_QUANTFUTURES_DATA_DIR_ENV, "").strip()
    if env_val:
        p = Path(env_val)
        if p.is_dir():
            return p
        logger.warning("QM_QUANTFUTURES_DATA_DIR=%s 不存在，尝试默认路径", env_val)
    for d in _QUANTFUTURES_DEFAULT_DATA_DIRS:
        p = Path(d)
        if p.is_dir():
            return p
    return Path(_QUANTFUTURES_DEFAULT_DATA_DIRS[-1])


class QuantFuturesDataHub(QuantDBDataHub):
    """期货/贵金属本地 parquet 数据中枢。视图命名空间 qfut_*。"""

    _instance: QuantFuturesDataHub | None = None
    _instance_lock = threading.Lock()

    def __init__(self, data_dir: str | Path | None = None) -> None:
        super().__init__(data_dir=data_dir or _resolve_quantfutures_data_dir())

    @classmethod
    def get_instance(cls) -> QuantFuturesDataHub:
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def _mount_views(self, conn, force: bool = False) -> None:
        """用 qfut_* 前缀挂载分区视图，避免与其他市场视图冲突。

        force=True 时忽略已挂载标记重建（基类 query() 在 Catalog 报错后的
        重试路径就带 force=True，签名不一致会抛 TypeError 盖掉原始错误）。
        """
        conn_id = id(conn)
        if not force and conn_id in self._views_mounted_per_conn:
            return
        dd = self._data_dir
        partitioned_views = {
            "qfut_daily_forward": "1_kline_data/daily_forward",
        }
        for view_name, rel_path in partitioned_views.items():
            full_path = dd / rel_path
            if not full_path.exists():
                continue
            parquet_glob = str(full_path / "**" / "*.parquet")
            try:
                conn.execute(
                    f"CREATE VIEW IF NOT EXISTS {view_name} AS "
                    f"SELECT * FROM read_parquet('{parquet_glob}', hive_partitioning=1, union_by_name=true)"
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("创建 DuckDB 视图 %s 失败: %s", view_name, exc)
        self._views_mounted_per_conn.add(conn_id)

    # ---- 期货查询（视图名带 qfut_ 前缀） ----
    def fetch_daily_kline(self, symbol: str, start, end, *, adjust: str = "qfq"):
        """期货日K。symbol 如 CL.FUT（国际）、RB0.CN（国内主力）、Au99.99（上金所）。"""
        view_name = "qfut_daily_forward"
        if not self._view_exists(view_name):
            return self._read_daily_kline_from_files(symbol, start, end, adjust="qfq")
        conn = self._get_duck_conn()
        conditions = [f"symbol = '{symbol}'"] + _dt_conditions(start, end)
        where = " AND ".join(conditions)
        df = conn.execute(f"SELECT * FROM {view_name} WHERE {where} ORDER BY dt").fetchdf()
        return self._normalize_kline(df)

    def fetch_daily_kline_batch(
        self,
        symbols: list[str],
        start,
        end,
        *,
        adjust: str = "qfq",
    ):
        """批量读期货日K。覆盖父类实现：期货视图名带 qfut_ 前缀（qfut_daily_forward）。"""
        import pandas as _pd

        view_name = "qfut_daily_forward"
        if not symbols or not self._view_exists(view_name):
            return _pd.DataFrame()
        conn = self._get_duck_conn()
        conditions = _dt_conditions(start, end)
        placeholders = ", ".join("?" for _ in symbols)
        conditions.append(f"symbol IN ({placeholders})")
        where = " AND ".join(conditions)
        df = conn.execute(
            f"SELECT * FROM {view_name} WHERE {where} ORDER BY symbol, dt",
            list(symbols),
        ).fetchdf()
        if df.empty:
            return df
        return self._normalize_kline(df)

    def fetch_realtime(self, symbol: str | None = None):
        """实时行情快照（2_base_sector/futures_realtime/*.parquet）。"""
        import pandas as pd

        d = self._data_dir / "2_base_sector" / "futures_realtime"
        if not d.is_dir():
            return pd.DataFrame()
        if symbol:
            f = d / f"{symbol}.parquet"
            if not f.exists():
                return pd.DataFrame()
            return pd.read_parquet(f)
        files = sorted(d.glob("*.parquet"))
        if not files:
            return pd.DataFrame()
        return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)

    # ---- 通用数据段读取 ----
    DATASET_DIRS = {
        "futures_realtime": "2_base_sector/futures_realtime",
        "sector": "2_base_sector/sector",
        "f10": "2_base_sector/f10",
        "warehouse_receipts": "2_base_sector/warehouse_receipts",
        "member_positions": "2_base_sector/member_positions",
        "contracts_daily": "2_base_sector/contracts_daily",
        "cftc": "2_base_sector/cftc",
        "fx_daily": "2_base_sector/fx_daily",
    }

    def fetch_dataset(self, dataset: str, symbol: str | None = None):
        """读取任一数据段（标的级 parquet）。"""
        import pandas as pd

        rel_dir = self.DATASET_DIRS.get(dataset)
        if rel_dir is None:
            return pd.DataFrame()
        d = self._data_dir / rel_dir
        if not d.is_dir():
            return pd.DataFrame()
        if symbol:
            file_path = d / f"{symbol}.parquet"
            if not file_path.exists():
                return pd.DataFrame()
            df = pd.read_parquet(file_path)
        else:
            files = sorted(d.glob("*.parquet"))
            if not files:
                return pd.DataFrame()
            df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
        return df

    @staticmethod
    def _empty_df():
        import pandas as _pd

        return _pd.DataFrame()
