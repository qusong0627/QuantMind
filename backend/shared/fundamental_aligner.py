import logging
from typing import Any

import pandas as pd

from backend.shared.stock_utils import StockCodeUtil

logger = logging.getLogger(__name__)


class FundamentalAligner:
    """
    统一基本面对齐器 (Unified Fundamental Aligner)

    从 QuantDB ``features_daily`` 按交易日读取特征，确保训练、回测和实盘筛选
    使用同一份宽表。数据读取统一走 ``quantdb_hub.QuantDBDataHub``，不再依赖
    ``fundamental_aligned.parquet``。
    """

    # 回测在某交易日 dt 上选股时，向前最多回看多少个交易日取特征（dt 本身无
    # 分区时取最近一个有数据的日期，避免回测早期因分区缺失而清空组合）。
    LOOKBACK_DAYS = 250

    def __init__(self) -> None:
        self._feature_snapshot_cache: dict[tuple[Any, ...], pd.DataFrame] = {}

    @staticmethod
    def _normalize_instrument(symbol: Any) -> str:
        return StockCodeUtil.to_prefix(str(symbol or ""))

    @staticmethod
    def _base_col(key: str) -> str:
        """约束键（无 f_ 前缀）→ 基础列名：剥掉 _min/_max/_in/_not 后缀。"""
        for suffix, length in (("_max", 4), ("_min", 4), ("_in", 3), ("_not", 4)):
            if key.endswith(suffix):
                return key[:-length]
        return key

    @staticmethod
    def _resolve_op(key: str) -> tuple[str, str]:
        """约束键 → (基础列名, 比较操作)。"""
        op = "eq"
        if key.endswith("_max"):
            return key[:-4], "le"
        if key.endswith("_min"):
            return key[:-4], "ge"
        if key.endswith("_in"):
            return key[:-3], "in"
        if key.endswith("_not"):
            return key[:-4], "ne"
        return key, op

    def _load_features_daily_snapshot(
        self,
        current_date: Any,
        symbols: list[str],
        needed_columns: list[str],
    ) -> pd.DataFrame:
        """经 quantdb_hub 读取交易日快照，symbol 归一化为前缀式。"""
        from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

        hub = QuantDBDataHub.get_instance()
        if not hub.available:
            return pd.DataFrame()

        dt = pd.to_datetime(current_date).normalize()
        dt_int = int(dt.strftime("%Y%m%d"))
        # features_daily.symbol 为后缀式，输入 instruments 为前缀式，先归一化。
        suffix_symbols = [
            StockCodeUtil.to_suffix(str(s)) for s in symbols if str(s or "").strip()
        ]
        suffix_symbols = [s for s in suffix_symbols if s]
        if not suffix_symbols:
            return pd.DataFrame()

        cache_key = (
            dt_int,
            frozenset(suffix_symbols),
            frozenset(needed_columns),
        )
        if cache_key in self._feature_snapshot_cache:
            return self._feature_snapshot_cache[cache_key]

        try:
            df = hub.fetch_latest_rows(
                "qdb_features_daily",
                suffix_symbols,
                dt=dt_int,
                lookback=self.LOOKBACK_DAYS,
                columns=needed_columns,
            )
        except Exception as exc:
            logger.warning("FundamentalAligner: 读取 features_daily 失败 (%s): %s", dt.date(), exc)
            return pd.DataFrame()

        if df.empty or "symbol" not in df.columns:
            return pd.DataFrame()

        df = df.copy()
        df["symbol"] = df["symbol"].map(
            lambda value: StockCodeUtil.to_prefix(str(value or ""))
        )
        df = df[df["symbol"] != ""]
        df = df.drop_duplicates(subset="symbol", keep="last").set_index("symbol")

        # 只缓存有限个查询，长期回测不会无限占用 worker 内存。
        if len(self._feature_snapshot_cache) >= 16:
            self._feature_snapshot_cache.clear()
        self._feature_snapshot_cache[cache_key] = df
        logger.debug(
            "FundamentalAligner: 使用 features_daily %s 快照，symbols=%s 字段=%s",
            dt.date(),
            len(df),
            needed_columns,
        )
        return df

    def filter_instruments(
        self,
        current_date: Any,
        instruments: list[str],
        constraints: dict[str, Any] | None = None,
    ) -> list[str]:
        if not constraints:
            return instruments

        needed_columns = sorted(
            {
                self._base_col(key)
                for key, target_val in constraints.items()
                if target_val is not None
            }
        )
        snapshot = self._load_features_daily_snapshot(
            current_date, instruments, needed_columns
        )
        if snapshot.empty:
            # 数据缺失时保持历史行为：不因数据源暂不可用而清空组合。
            return instruments

        mask = pd.Series(True, index=snapshot.index)
        for key, target_val in constraints.items():
            if target_val is None:
                continue

            col, op = self._resolve_op(key)

            if col not in snapshot.columns:
                logger.warning(
                    "FundamentalAligner: 约束列 %s 在 features_daily 中不存在，"
                    "已跳过（静默失效）",
                    col,
                )
                continue

            col_data = snapshot[col]
            if op == "le":
                mask &= col_data <= float(target_val)
            elif op == "ge":
                mask &= col_data >= float(target_val)
            elif op == "ne":
                mask &= col_data != target_val
            elif op == "in":
                if isinstance(target_val, (list, set, tuple)):
                    mask &= col_data.isin(target_val)
                else:
                    mask &= col_data == target_val
            else:
                mask &= col_data == target_val

        valid_symbols = set(snapshot[mask].index)
        return [
            symbol
            for symbol in instruments
            if self._normalize_instrument(symbol) in valid_symbols
        ]


fundamental_aligner = FundamentalAligner()
