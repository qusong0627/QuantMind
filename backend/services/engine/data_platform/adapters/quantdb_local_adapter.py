"""
QuantDB 本地 Parquet 适配器
===========================

基于本地 data/quantdb/ 目录的 parquet 文件提供 A 股数据，
是 QuantMind A 股数据的优先数据源。

与 quantdb_adapter（远程 SDK）的区别：
- 无需 API Key，无网络调用
- 数据来自本地 parquet 文件，读取延迟低
- 覆盖所有 QuantDB 数据类型（K线、财务、估值、因子等）
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Optional

import pandas as pd

from backend.services.engine.data_platform.base import (
    DataUnavailable,
    InvalidFieldRequest,
    OfflineDataSourceAdapter,
)
from backend.services.engine.data_platform.models import OHLCV_COLUMNS
from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub
from backend.shared.stock_utils import StockCodeUtil

logger = logging.getLogger(__name__)


class QuantDBLocalAdapter(OfflineDataSourceAdapter):
    """QuantDB 本地 Parquet 适配器 — A 股优先数据源。

    通过 QuantDBDataHub 读取本地 parquet 文件，无需 API Key，
    覆盖日线/分钟K线、财务报表、估值、技术指标、因子等全部 A 股数据。
    """

    name = "quantdb_local"
    markets = ["A"]
    fields = {
        "daily_kline",
        "minute_kline",
        "stock_list",
        "calendar",
        "financial_report",
        "valuation",
        "ai_factors",
        "adj_factor",
        "sector",
        "market_sentiment",
        "technical_indicators",
        "margin_trading",
        "dividend",
        "index_kline",
        "shareholder_count",
        "share_change",
        "dupont",
        "growth",
        "operation",
    }

    def __init__(self) -> None:
        self._hub = QuantDBDataHub()

    @property
    def hub(self) -> QuantDBDataHub:
        return self._hub

    # ---- 必选接口 ----
    def fetch_daily(
        self,
        symbol: str,
        start: date,
        end: date,
        *,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        if not self._hub.available:
            raise DataUnavailable("QuantDB 本地数据不可用")

        qdb_symbol = self._to_qdb_symbol(symbol)
        df = self._hub.fetch_daily_kline(qdb_symbol, start, end, adjust=adjust)
        if df is None or df.empty:
            raise DataUnavailable(
                f"QuantDB 本地无日线数据: {qdb_symbol} {start}~{end}"
            )

        df = self._standardize_ohlcv(df, symbol)
        return df

    def fetch_meta(self, market: str) -> pd.DataFrame:
        if market.upper() != "A":
            raise InvalidFieldRequest(f"QuantDB 本地不支持 market={market}")
        if not self._hub.available:
            raise DataUnavailable("QuantDB 本地数据不可用")

        df = self._hub.fetch_stock_list()
        if df is None or df.empty:
            raise DataUnavailable("QuantDB 本地无股票列表数据")

        # 映射 instrument_detail 列到 SYMBOL_META_COLUMNS
        result = pd.DataFrame()
        col_map = {
            "Symbol": "symbol",
            "Name": "name",
            "ErrorId": "code",
        }
        for src_col, dst_col in col_map.items():
            if src_col in df.columns:
                result[dst_col] = df[src_col]

        # 推断 exchange
        if "symbol" in result.columns:
            result["exchange"] = result["symbol"].apply(self._infer_exchange)
            result["market"] = "A"
            result["is_active"] = True

        # 行业
        if "rs_hyname" in df.columns:
            result["industry"] = df["rs_hyname"]
        if "tdx_dyname" in df.columns:
            result["sector"] = df["tdx_dyname"]

        result["source"] = self.name
        return result

    # ---- 可选接口 ----
    def fetch_minute(
        self,
        symbol: str,
        start: date,
        end: date,
        *,
        freq: str = "1min",
    ) -> pd.DataFrame:
        if not self._hub.available:
            raise DataUnavailable("QuantDB 本地数据不可用")

        qdb_symbol = self._to_qdb_symbol(symbol)
        df = self._hub.fetch_minute_kline(qdb_symbol, start, end, freq=freq)
        if df is None or df.empty:
            raise DataUnavailable(
                f"QuantDB 本地无分钟线数据: {qdb_symbol} {start}~{end} freq={freq}"
            )

        df = self._standardize_ohlcv(df, symbol)
        return df

    def fetch_field(
        self,
        field: str,
        symbol: str,
        *,
        start: date | None = None,
        end: date | None = None,
        **kwargs: Any,
    ) -> pd.DataFrame:
        if not self._hub.available:
            raise DataUnavailable("QuantDB 本地数据不可用")

        qdb_symbol = self._to_qdb_symbol(symbol)

        dispatch = {
            "calendar": self._fetch_calendar,
            "financial_report": self._fetch_financial,
            "valuation": self._fetch_valuation,
            "ai_factors": self._fetch_ai_factors,
            "stock_list": self._fetch_stock_list,
            "sector": self._fetch_sector,
            "market_sentiment": self._fetch_market_sentiment,
            "technical_indicators": self._fetch_technical_indicators,
            "adj_factor": self._fetch_adj_factor,
            "margin_trading": self._fetch_margin_trading,
            "dividend": self._fetch_dividend,
            "index_kline": self._fetch_index_kline,
            "shareholder_count": self._fetch_shareholder_count,
            "share_change": self._fetch_share_change,
            "dupont": self._fetch_dupont,
            "growth": self._fetch_growth,
            "operation": self._fetch_operation,
        }

        handler = dispatch.get(field)
        if handler is None:
            raise InvalidFieldRequest(
                f"QuantDB 本地不支持 field={field}"
            )

        return handler(qdb_symbol, start, end, **kwargs)

    # ---- 字段实现 ----
    def _fetch_calendar(
        self, symbol: str, start: date | None, end: date | None, **kwargs: Any
    ) -> pd.DataFrame:
        df = self._hub.fetch_calendar(start=start, end=end)
        if df is None or df.empty:
            raise DataUnavailable("QuantDB 本地无交易日历数据")
        df["source"] = self.name
        return df

    def _fetch_financial(
        self, symbol: str, start: date | None, end: date | None, **kwargs: Any
    ) -> pd.DataFrame:
        statement_type = kwargs.get("statement_type", "income")
        df = self._hub.fetch_financial(symbol, statement_type=statement_type, start=start, end=end)
        if df is None or df.empty:
            raise DataUnavailable(f"QuantDB 本地无财务数据: {symbol} {statement_type}")
        df["source"] = self.name
        return df

    def _fetch_valuation(
        self, symbol: str, start: date | None, end: date | None, **kwargs: Any
    ) -> pd.DataFrame:
        df = self._hub.fetch_valuation(symbol=symbol, start=start, end=end)
        if df is None or df.empty:
            raise DataUnavailable(f"QuantDB 本地无估值数据: {symbol}")
        df["source"] = self.name
        return df

    def _fetch_ai_factors(
        self, symbol: str, start: date | None, end: date | None, **kwargs: Any
    ) -> pd.DataFrame:
        sub = kwargs.get("sub_category", "l1_l2_factors")
        if sub == "l1_factors":
            df = self._hub.fetch_l1_factors(start=start, end=end)
        elif sub in ("l1_l2_factors", "l2_factors"):
            # l2_factors 目录已下线并入 l1_l2 合并宽表（列超集），旧参数名兼容指向新表
            df = self._hub.fetch_l1_l2_factors(start=start, end=end)
        elif sub == "features_daily":
            df = self._hub.fetch_features_daily(symbol=symbol, start=start, end=end)
        else:
            df = self._hub.fetch_features_daily(symbol=symbol, start=start, end=end)
        if df is None or df.empty:
            raise DataUnavailable(f"QuantDB 本地无因子数据: {symbol} {sub}")
        # L1/L2 因子可能没有 symbol 列的过滤，需要按 symbol 过滤
        if "symbol" in df.columns and symbol:
            df = df[df["symbol"] == symbol]
        df["source"] = self.name
        return df

    def _fetch_stock_list(
        self, symbol: str, start: date | None, end: date | None, **kwargs: Any
    ) -> pd.DataFrame:
        return self.fetch_meta("A")

    def _fetch_sector(
        self, symbol: str, start: date | None, end: date | None, **kwargs: Any
    ) -> pd.DataFrame:
        df = self._hub.fetch_sector_members(sector_name=kwargs.get("sector_name"))
        if df is None or df.empty:
            raise DataUnavailable("QuantDB 本地无板块数据")
        df["source"] = self.name
        return df

    def _fetch_market_sentiment(
        self, symbol: str, start: date | None, end: date | None, **kwargs: Any
    ) -> pd.DataFrame:
        df = self._hub.fetch_market_sentiment(symbol=symbol, start=start, end=end)
        if df is None or df.empty:
            raise DataUnavailable(f"QuantDB 本地无情绪数据: {symbol}")
        df["source"] = self.name
        return df

    def _fetch_technical_indicators(
        self, symbol: str, start: date | None, end: date | None, **kwargs: Any
    ) -> pd.DataFrame:
        df = self._hub.fetch_technical_indicators(symbol=symbol, start=start, end=end)
        if df is None or df.empty:
            raise DataUnavailable(f"QuantDB 本地无技术指标数据: {symbol}")
        df["source"] = self.name
        return df

    def _fetch_adj_factor(
        self, symbol: str, start: date | None, end: date | None, **kwargs: Any
    ) -> pd.DataFrame:
        """从前后复权价格比计算复权因子。"""
        if not start or not end:
            start = start or date(2016, 1, 4)
            end = end or date.today()

        # 读取不复权和前复权数据
        df_unadj = self._hub.fetch_daily_kline(symbol, start, end, adjust="none")
        df_qfq = self._hub.fetch_daily_kline(symbol, start, end, adjust="qfq")

        if df_unadj.empty or df_qfq.empty:
            raise DataUnavailable(f"QuantDB 本地无复权因子数据: {symbol}")

        # 合并计算 adj_factor = qfq_close / unadj_close
        df = df_unadj[["symbol", "trade_date", "close"]].rename(columns={"close": "unadj_close"})
        df["qfq_close"] = df_qfq["close"].values[:len(df)]
        df["adj_factor"] = df["qfq_close"] / df["unadj_close"].replace(0, float("nan"))
        df = df.drop(columns=["unadj_close", "qfq_close"])
        df["source"] = self.name
        return df

    def _fetch_margin_trading(
        self, symbol: str, start: date | None, end: date | None, **kwargs: Any
    ) -> pd.DataFrame:
        df = self._hub.fetch_margin_trading(symbol=symbol, start=start, end=end)
        if df is None or df.empty:
            raise DataUnavailable(f"QuantDB 本地无融资融券数据: {symbol}")
        df["source"] = self.name
        return df

    def _fetch_dividend(
        self, symbol: str, start: date | None, end: date | None, **kwargs: Any
    ) -> pd.DataFrame:
        df = self._hub.fetch_dividend_factors(symbol)
        if df is None or df.empty:
            raise DataUnavailable(f"QuantDB 本地无分红数据: {symbol}")
        df["source"] = self.name
        return df

    def _fetch_index_kline(
        self, symbol: str, start: date | None, end: date | None, **kwargs: Any
    ) -> pd.DataFrame:
        if not start or not end:
            start = start or date(2016, 1, 4)
            end = end or date.today()
        df = self._hub.fetch_index_kline(symbol, start, end)
        if df is None or df.empty:
            raise DataUnavailable(f"QuantDB 本地无指数K线数据: {symbol}")
        df["source"] = self.name
        return df

    def _fetch_shareholder_count(
        self, symbol: str, start: date | None, end: date | None, **kwargs: Any
    ) -> pd.DataFrame:
        df = self._hub.fetch_financial(
            symbol, statement_type="holder_num", start=start, end=end
        )
        if df is None or df.empty:
            raise DataUnavailable(f"QuantDB 本地无股东户数数据: {symbol}")
        df["source"] = self.name
        return df

    def _fetch_share_change(
        self, symbol: str, start: date | None, end: date | None, **kwargs: Any
    ) -> pd.DataFrame:
        df = self._hub.fetch_financial(
            symbol, statement_type="capital", start=start, end=end
        )
        if df is None or df.empty:
            raise DataUnavailable(f"QuantDB 本地无股本变动数据: {symbol}")
        df["source"] = self.name
        return df

    # pershare_index 一张表覆盖杜邦/成长/运营三类指标，按列前缀切分
    _DUPONT_COLS = ("du_return_on_equity", "du_profit_rate", "du_profit",
                    "equity_roe", "net_roe", "total_roe")
    _GROWTH_COLS = ("inc_revenue_rate", "inc_net_profit_rate", "inc_revenue",
                    "inc_gross_profit", "inc_profit_before_tax", "inc_net_profit",
                    "inc_total_revenue_annual",
                    "inc_net_profit_to_shareholders_annual",
                    "adjusted_net_profit_rate", "adjusted_profit_to_profit_annual")
    _OPERATION_COLS = ("inventory_turnover", "gear_ratio", "actual_tax_rate",
                       "sales_gross_profit", "sales_cash_flow",
                       "pre_pay_operate_income", "gross_profit", "net_profit")

    def _fetch_pershare_subset(
        self,
        symbol: str,
        start: date | None,
        end: date | None,
        cols: tuple[str, ...],
        label: str,
    ) -> pd.DataFrame:
        df = self._hub.fetch_financial(
            symbol, statement_type="pershare_index", start=start, end=end
        )
        if df is None or df.empty:
            raise DataUnavailable(f"QuantDB 本地无{label}数据: {symbol}")

        keep = ["symbol", "m_timetag", "m_anntime", "m_quarter"]
        selected = [c for c in keep if c in df.columns]
        selected += [c for c in cols if c in df.columns]
        if not any(c in df.columns for c in cols):
            raise DataUnavailable(f"QuantDB 本地{label}字段缺失: {symbol}")

        result = df[selected].copy()
        result["source"] = self.name
        return result

    def _fetch_dupont(
        self, symbol: str, start: date | None, end: date | None, **kwargs: Any
    ) -> pd.DataFrame:
        return self._fetch_pershare_subset(
            symbol, start, end, self._DUPONT_COLS, "杜邦分析"
        )

    def _fetch_growth(
        self, symbol: str, start: date | None, end: date | None, **kwargs: Any
    ) -> pd.DataFrame:
        return self._fetch_pershare_subset(
            symbol, start, end, self._GROWTH_COLS, "成长能力"
        )

    def _fetch_operation(
        self, symbol: str, start: date | None, end: date | None, **kwargs: Any
    ) -> pd.DataFrame:
        return self._fetch_pershare_subset(
            symbol, start, end, self._OPERATION_COLS, "运营能力"
        )

    # ---- 内部工具 ----
    def _standardize_ohlcv(self, df: pd.DataFrame, symbol: str) -> pd.DataFrame:
        """标准化 OHLCV 输出为 OHLCV_COLUMNS 规范。"""
        # 确保核心列存在
        for col in OHLCV_COLUMNS:
            if col not in df.columns:
                if col == "adj_factor":
                    df["adj_factor"] = 1.0
                elif col == "source":
                    df["source"] = self.name
                elif col == "symbol":
                    df["symbol"] = symbol.upper()

        # 按 OHLCV_COLUMNS 顺序排列（保留额外列）
        ordered = [c for c in OHLCV_COLUMNS if c in df.columns]
        extra = [c for c in df.columns if c not in ordered]
        df = df[ordered + extra]

        return df

    @staticmethod
    def _to_qdb_symbol(symbol: str) -> str:
        """内部格式 -> QuantDB suffix 格式 600036.SH（委托中枢，号段以中枢为准）。"""
        return StockCodeUtil.to_suffix(symbol)

    @staticmethod
    def _infer_exchange(symbol: str) -> str:
        """从 symbol 推断交易所（先经中枢归一，前后缀双收）。"""
        from backend.shared.stock_utils import StockCodeUtil

        norm = StockCodeUtil.to_suffix(symbol or "")
        if not norm or "." not in norm:
            return "unknown"
        exchange = norm.split(".")[-1].upper()
        return {"SH": "SSE", "SZ": "SZSE", "BJ": "BSE"}.get(exchange, exchange)


def register() -> bool:
    """注册适配器到 SourceRegistry。"""
    from backend.services.engine.data_platform.registry import get_registry

    hub = QuantDBDataHub()
    if not hub.available:
        logger.info("QuantDB 本地数据目录不存在，跳过 QuantDBLocalAdapter 注册")
        return False

    get_registry().register(QuantDBLocalAdapter, name=QuantDBLocalAdapter.name)
    logger.info(
        "QuantDBLocalAdapter 已注册 (data_dir=%s)", hub.data_dir
    )
    return True
