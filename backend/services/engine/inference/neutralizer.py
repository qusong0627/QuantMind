import logging
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

try:
    import duckdb

    _DUCKDB_AVAILABLE = True
except ImportError:
    _DUCKDB_AVAILABLE = False

logger = logging.getLogger(__name__)


class Neutralizer:
    """
    Production-grade neutralization engine for real-time inference.
    Supports Industry and Size (Market Cap) neutralization using cross-sectional regression.
    """

    def __init__(self, db_path: str = "db/official_factors.duckdb"):
        self.db_path = db_path
        self._industry_cache: dict[str, str] = {}
        self._mkt_cap_cache: dict[str, float] = {}
        self._last_update_date: str | None = None

    def refresh_metadata(self, date_str: str):
        """Fetch latest industry and market cap data from DB for the given date."""
        if self._last_update_date == date_str:
            return

        if not _DUCKDB_AVAILABLE:
            logger.warning("duckdb not installed; neutralization metadata unavailable, skipping neutralization")
            return

        if not Path(self.db_path).exists():
            logger.warning(f"Official factors duckdb file not found at {self.db_path}; skipping neutralization metadata refresh")
            return

        try:
            conn = duckdb.connect(self.db_path, read_only=True)
            # ASOF join to get latest available metadata
            sql = f"""
            WITH shrs AS (
                SELECT stkcd, date, a_circulated_share FROM stk_shares_raw WHERE date <= '{date_str}'
            ),
            price AS (
                SELECT stkcd, date, Clsprc FROM TRD_Dalyr WHERE date <= '{date_str}'
            ),
            ind AS (
                SELECT stkcd, date, Nindnme as industry FROM TRD_Co WHERE date <= '{date_str}'
            )
            SELECT 
                p.stkcd, p.Clsprc * s.a_circulated_share as mkt_cap, i.industry
            FROM price p
            ASOF LEFT JOIN shrs s ON p.stkcd = s.stkcd AND p.date >= s.date
            ASOF LEFT JOIN ind i ON p.stkcd = i.stkcd AND p.date >= i.date
            WHERE p.date = (SELECT MAX(date) FROM TRD_Dalyr WHERE date <= '{date_str}')
            """
            df = conn.execute(sql).fetchdf()
            conn.close()

            if not df.empty:
                df["log_cap"] = np.log(df["mkt_cap"] + 1e-6)
                self._industry_cache = df.set_index("stkcd")["industry"].to_dict()
                self._mkt_cap_cache = df.set_index("stkcd")["log_cap"].to_dict()
                self._last_update_date = date_str
                logger.info(f"Refreshed neutralization metadata for {date_str}, records: {len(df)}")
        except Exception as e:
            logger.error(f"Failed to refresh neutralization metadata: {e}")

    def neutralize(self, df: pd.DataFrame, feature_cols: list[str]) -> pd.DataFrame:
        """
        Perform strict cross-sectional neutralization on input features.
        df index should be instruments (stkcd).
        """
        if df.empty or not feature_cols:
            return df

        # 元数据不可用（official_factors.duckdb 缺失或刷新失败）时硬失败：
        # 声明 is_neutralized=true 的模型若静默拿到未中性化特征（或全量 dropna 后
        # 的空特征），会产出看似成功却错误的预测——宁可在调用方报错。
        if self._last_update_date is None:
            raise RuntimeError(
                "中性化元数据不可用（official_factors.duckdb 缺失或刷新失败），"
                f"拒绝静默返回未中性化特征: db_path={self.db_path}"
            )

        # Enrich DF with metadata from cache
        df = df.copy()
        df["industry"] = df.index.map(self._industry_cache)
        df["log_cap"] = df.index.map(self._mkt_cap_cache)

        # Drop rows missing control variables (cannot neutralize without them)
        initial_len = len(df)
        df = df.dropna(subset=["industry", "log_cap"])
        if len(df) < initial_len:
            logger.warning(f"Dropped {initial_len - len(df)} stocks due to missing neutralization metadata")

        if df.empty or len(df) < 10:
            return df

        # Prepare X: Constant + LogCap + Industry Dummies
        ind_dummies = pd.get_dummies(df["industry"], drop_first=True)
        X = np.column_stack([np.ones(len(df)), df["log_cap"].values, ind_dummies.values.astype(float)])

        # Batch regression for speed
        Y = df[feature_cols].fillna(0.0).values
        # Residuals = Y - X * (X^T * X)^-1 * X^T * Y
        try:
            beta, _, _, _ = np.linalg.lstsq(X, Y, rcond=None)
            neutral_features = Y - X.dot(beta)
            df[feature_cols] = neutral_features
        except Exception as e:
            logger.error(f"Neutralization regression failed: {e}")

        return df.drop(columns=["industry", "log_cap"])
