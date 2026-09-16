"""QuantBC 数据中枢 — 区块链/加密货币本地 parquet 读取的单一入口。

复用 QuantDBDataHub 的查询基础设施（DuckDB 连接管理、视图挂载、K线/列
标准化），仅替换数据目录与视图命名空间（qbc_*），避免与其他市场视图串扰。

数据目录：环境变量 QM_QUANTBC_DATA_DIR，默认 data/quantbc/。
目录结构与 QuantDB 对齐（日线 / 指数 / 估值 / 财务 / 标的池）。
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

_QUANTBC_DATA_DIR_ENV = "QM_QUANTBC_DATA_DIR"
_QUANTBC_DEFAULT_DATA_DIRS = [
    "/data/quantbc",  # Docker 容器内（挂载点）
    str(Path(__file__).resolve().parents[4] / "data" / "quantbc"),  # 项目根/data/quantbc
]


def _crypto_enabled() -> bool:
    """生产环境通过 ENABLE_CRYPTO=false 屏蔽区块链（默认开启，与 market_adapters 一致）。"""
    raw = os.getenv("ENABLE_CRYPTO", "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return True


def _resolve_quantbc_data_dir() -> Path:
    # Docker 层屏蔽：ENABLE_CRYPTO=false 时返回不存在路径，quantbc 不可用
    if not _crypto_enabled():
        return Path(_QUANTBC_DATA_DIR_ENV)  # 非目录，不可用
    env_val = os.getenv(_QUANTBC_DATA_DIR_ENV, "").strip()
    if env_val:
        p = Path(env_val)
        if p.is_dir():
            return p
        logger.warning("QM_QUANTBC_DATA_DIR=%s 不存在，尝试默认路径", env_val)
    for d in _QUANTBC_DEFAULT_DATA_DIRS:
        p = Path(d)
        if p.is_dir():
            return p
    return Path(_QUANTBC_DEFAULT_DATA_DIRS[-1])


class QuantBCDataHub(QuantDBDataHub):
    """区块链/加密货币本地 parquet 数据中枢。视图命名空间 qbc_*。"""

    _instance: QuantBCDataHub | None = None
    _instance_lock = threading.Lock()

    def __init__(self, data_dir: str | Path | None = None) -> None:
        super().__init__(data_dir=data_dir or _resolve_quantbc_data_dir())

    @classmethod
    def get_instance(cls) -> QuantBCDataHub:
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def _mount_views(self, conn, force: bool = False) -> None:
        """用 qbc_* 前缀挂载分区视图，避免与其他市场视图冲突。

        force=True 时忽略已挂载标记重建（基类 query() 在 Catalog 报错后的
        重试路径就带 force=True，签名不一致会抛 TypeError 盖掉原始错误）。
        """
        conn_id = id(conn)
        if not force and conn_id in self._views_mounted_per_conn:
            return
        dd = self._data_dir
        partitioned_views = {
            "qbc_daily_forward": "1_kline_data/daily_forward",
            "qbc_index_daily": "1_kline_data/index_daily",
            "qbc_valuation": "5_technical_derived/valuation",
            "qbc_features_daily": "6_ml_datasets/features_daily",
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

    # ---- 区块链查询（视图名带 qbc_ 前缀） ----
    def fetch_daily_kline(self, symbol: str, start, end, *, adjust: str = "qfq"):
        """区块链日线。symbol 为币种交易对（BTCUSDT）。"""
        view_name = "qbc_daily_forward"
        if not self._view_exists(view_name):
            return self._read_daily_kline_from_files(symbol, start, end, adjust="qfq")
        conn = self._get_duck_conn()
        conditions = [f"symbol = '{symbol}'"] + _dt_conditions(start, end)
        where = " AND ".join(conditions)
        df = conn.execute(f"SELECT * FROM {view_name} WHERE {where} ORDER BY dt").fetchdf()
        return self._normalize_kline(df)

    def fetch_valuation(self, symbol: str | None = None, start=None, end=None):
        """区块链估值快照（按日落盘）。"""
        if not self._view_exists("qbc_valuation"):
            return self._empty_df()
        conn = self._get_duck_conn()
        conditions = []
        if symbol:
            conditions.append(f"symbol = '{symbol}'")
        conditions.extend(_dt_conditions(start, end))
        where = " AND ".join(conditions) if conditions else "1=1"
        df = conn.execute(f"SELECT * FROM qbc_valuation WHERE {where} ORDER BY dt").fetchdf()
        return self._normalize_columns(df)

    def fetch_stock_list(self):
        """区块链标的池（instrument_detail.parquet）。"""
        import pandas as pd

        detail_dir = self._data_dir / "2_base_sector" / "instrument_detail"
        file_path = detail_dir / "instrument_list.parquet"
        if not file_path.exists():
            file_path = detail_dir / "instrument_detail.parquet"
        if not file_path.exists():
            return pd.DataFrame()
        return pd.read_parquet(file_path)

    # ---- 通用数据段读取（分红/财务/评级/持仓等） ----
    DATASET_DIRS = {
        "sector": "2_base_sector/sector",
        "f10": "2_base_sector/f10",
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
