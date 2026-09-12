"""QuantUS 数据中枢 — 美股本地 parquet 读取的单一入口。

复用 QuantDBDataHub 的查询基础设施（DuckDB 连接管理、视图挂载、K线/列
标准化），仅替换数据目录与视图命名空间（qus_*），避免 A 股/美股视图串扰。

数据目录：环境变量 QM_QUANTUS_DATA_DIR，默认 data/quantus/。
目录结构与 QuantDB 对齐（日线 / 指数 / 估值 / 财务 / 标的池）。
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    import pandas as pd

from backend.services.engine.data_platform.quantdb_hub import (
    QuantDBDataHub,
    _dt_conditions,
)

logger = logging.getLogger(__name__)

_QUANTUS_DATA_DIR_ENV = "QM_QUANTUS_DATA_DIR"
_QUANTUS_DEFAULT_DATA_DIRS = [
    "/data/quantus",  # Docker 容器内（挂载点）
    str(Path(__file__).resolve().parents[4] / "data" / "quantus"),  # 项目根/data/quantus
]


def _resolve_quantus_data_dir() -> Path:
    env_val = os.getenv(_QUANTUS_DATA_DIR_ENV, "").strip()
    if env_val:
        p = Path(env_val)
        if p.is_dir():
            return p
        logger.warning("QM_QUANTUS_DATA_DIR=%s 不存在，尝试默认路径", env_val)
    for d in _QUANTUS_DEFAULT_DATA_DIRS:
        p = Path(d)
        if p.is_dir():
            return p
    return Path(_QUANTUS_DEFAULT_DATA_DIRS[-1])


class QuantUSDataHub(QuantDBDataHub):
    """美股本地 parquet 数据中枢。视图命名空间 qus_*。"""

    _instance: QuantUSDataHub | None = None
    _instance_lock = threading.Lock()

    def __init__(self, data_dir: str | Path | None = None) -> None:
        super().__init__(data_dir=data_dir or _resolve_quantus_data_dir())

    @classmethod
    def get_instance(cls) -> QuantUSDataHub:
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def _mount_views(self, conn) -> None:
        """用 qus_* 前缀挂载分区视图，避免与 A 股 qdb_* 冲突。"""
        conn_id = id(conn)
        if conn_id in self._views_mounted_per_conn:
            return
        dd = self._data_dir
        partitioned_views = {
            "qus_daily_forward": "1_kline_data/daily_forward",
            "qus_index_daily": "1_kline_data/index_daily",
            "qus_valuation": "5_technical_derived/valuation",
            # L1 因子日频分区（模型训练直读数据集）
            "qus_l1_factors": "6_ml_datasets/l1_factors",
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

    # ---- 美股查询（视图名带 qus_ 前缀） ----
    def fetch_daily_kline(self, symbol: str, start, end, *, adjust: str = "qfq"):
        """美股日线。symbol 为原生 ticker（AAPL / MSFT）。"""
        view_name = "qus_daily_forward"
        if not self._view_exists(view_name):
            return self._read_daily_kline_from_files(symbol, start, end, adjust="qfq")
        conn = self._get_duck_conn()
        conditions = [f"symbol = '{symbol}'"] + _dt_conditions(start, end)
        where = " AND ".join(conditions)
        df = conn.execute(f"SELECT * FROM {view_name} WHERE {where} ORDER BY dt").fetchdf()
        return self._normalize_kline(df)

    def fetch_index_kline(self, symbol: str, start, end) -> pd.DataFrame:
        """美股指数日线（纳指/标普/道指）。symbol 如 IXIC.US。"""
        view_name = "qus_index_daily"
        if not self._view_exists(view_name):
            return self._empty_df()
        conn = self._get_duck_conn()
        conditions = [f"symbol = '{symbol}'"] + _dt_conditions(start, end)
        where = " AND ".join(conditions)
        df = conn.execute(f"SELECT * FROM {view_name} WHERE {where} ORDER BY dt").fetchdf()
        return self._normalize_kline(df)

    def fetch_valuation(self, symbol: str | None = None, start=None, end=None):
        """美股估值指标（yfinance info 快照按日落盘）。"""
        if not self._view_exists("qus_valuation"):
            return self._empty_df()
        conn = self._get_duck_conn()
        conditions = []
        if symbol:
            conditions.append(f"symbol = '{symbol}'")
        conditions.extend(_dt_conditions(start, end))
        where = " AND ".join(conditions) if conditions else "1=1"
        df = conn.execute(f"SELECT * FROM qus_valuation WHERE {where} ORDER BY dt").fetchdf()
        return self._normalize_columns(df)

    def fetch_stock_list(self):
        """美股标的池（security_master：symbol / cn_name / en_name）。

        历史上这里读 `2_base_sector/instrument_detail/`（instrument_list.parquet /
        instrument_detail.parquet），该目录从未落盘，方法恒返回空表。
        """
        import pandas as pd

        file_path = self._data_dir / "2_base_sector" / "security_master" / "data.parquet"
        if not file_path.exists():
            return pd.DataFrame()
        return pd.read_parquet(file_path)

    # ---- 通用数据段读取（分红/财务/评级/持仓/期权等） ----
    # 数据段 → 相对目录（与 global_market_sync 落盘一致）
    DATASET_DIRS = {
        "dividend": "3_financial_data/dividend",
        "splits": "3_financial_data/splits",
        "balance": "3_financial_data/balance",
        "income": "3_financial_data/income",
        "cashflow": "3_financial_data/cashflow",
        "sector": "2_base_sector/sector",
        "f10": "2_base_sector/f10",
        "recommendations": "4_analyst/recommendations",
        "upgrades_downgrades": "4_analyst/upgrades_downgrades",
        "earnings_history": "4_analyst/earnings_history",
        "earnings_dates": "4_analyst/earnings_dates",
        "earnings_estimate": "4_analyst/earnings_estimate",
        "revenue_estimate": "4_analyst/revenue_estimate",
        "growth_estimates": "4_analyst/growth_estimates",
        "analyst_price_targets": "4_analyst/analyst_price_targets",
        "major_holders": "4_analyst/major_holders",
        "mutual_fund_holders": "4_analyst/mutual_fund_holders",
        "calendar": "4_analyst/calendar",
        "insider_transactions": "4_analyst/insider_transactions",
        "options_chain": "4_options",
        # 标的池主表（symbol / cn_name / en_name），原 fetch_stock_list 读的
        # 2_base_sector/instrument_detail 从未落盘
        "security_master": "2_base_sector/security_master",
        # 由 quantus_extra_datasets.py 落盘（universe_YYYYMMDD.parquet），可能尚未生成
        "us_universe": "2_base_sector/us_universe",
    }

    def fetch_dataset(self, dataset: str, symbol: str | None = None):
        """读取任一数据段（标的级 parquet）。

        Args:
            dataset: 数据段名（dividend/income/balance/cashflow/sector/f10/recommendations/options_chain 等）
            symbol: 标的代码，为空返回该目录全部标的
        """
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
