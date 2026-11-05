"""
QuantDB Remote SDK 数据源适配器
===============================

封装 quantdb-sdk (pip install quantdb-sdk)，提供远程 API 实时查询：
- 日线 K 线（前复权/后复权/不复权）
- Tick 逐笔
- 股票列表
- 交易日历
- 财务报表 / 估值 / 315 维 AI 因子（通过 query_local / load_as_df）

QuantDB 是付费 CDN 数据源，通过 API Key 认证，流量配额制。
数据以 Parquet 格式经 CDN 两跳 302 分发，SDK 返回 DataFrame。

注意：本地 parquet 数据读取请使用 quantdb_local_adapter (QuantDBLocalAdapter)，
本适配器仅用于远程 API 实时查询（如当日数据补全、特定 symbol 查询等）。
"""

from __future__ import annotations

import logging
import os
from datetime import date
from typing import Any, Optional

import pandas as pd

from backend.services.engine.data_platform.base import (
    DataUnavailable,
    InvalidFieldRequest,
    OfflineDataSourceAdapter,
)

logger = logging.getLogger(__name__)

try:
    from quantdb_sdk import QuantDBClient  # type: ignore
    _QDB_AVAILABLE = True
except ImportError:
    QuantDBClient = None  # type: ignore
    _QDB_AVAILABLE = False


def _get_client() -> QuantDBClient:
    if not _QDB_AVAILABLE:
        raise DataUnavailable("quantdb-sdk 未安装，请运行 pip install quantdb-sdk")
    from backend.shared.quantdb_client import get_client

    try:
        # 与同步客户端共用超时/重试/Key 读取；避免 adapter 用 SDK 默认 (5,60)/2
        return get_client("adapter")
    except RuntimeError as exc:
        raise DataUnavailable(str(exc)) from exc


def _to_qdb_symbol(symbol: str) -> str:
    """内部格式 SH600036 -> 600036.SH (QuantDB 使用 Suffix 格式)

    支持输入: 600036.SH, SH600036, 600036
    BJ 前缀: BJ873169 -> 873169.BJ
    """
    s = symbol.strip().upper()
    if "." in s:
        return s  # 已经是 600036.SH 格式
    if s.startswith("SH") or s.startswith("SZ") or s.startswith("BJ"):
        return f"{s[2:]}.{s[:2]}"
    # 纯数字：根据规则自动识别
    if s.isdigit():
        if s.startswith("6") or s.startswith("9"):
            return f"{s}.SH"
        if s.startswith("0") or s.startswith("3") or s.startswith("2"):
            return f"{s}.SZ"
        if s.startswith("4") or s.startswith("8"):
            return f"{s}.BJ"
    return s


class QuantDBAdapter(OfflineDataSourceAdapter):
    """QuantDB SDK 适配器 — 付费高质量数据源。"""

    name = "quantdb"
    markets = ["A"]
    fields = {
        "daily_kline",
        "tick",
        "stock_list",
        "calendar",
        "financial_report",
        "valuation",
        "ai_factors",
    }

    def __init__(self) -> None:
        self._client: QuantDBClient | None = None

    @property
    def client(self) -> QuantDBClient:
        if self._client is None:
            self._client = _get_client()
        return self._client

    # ---- 必选 ----
    def fetch_daily(
        self,
        symbol: str,
        start: date,
        end: date,
        *,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        adj_map = {"qfq": "forward", "hfq": "backward", "none": "unadjusted"}
        adj_type = adj_map.get(adjust, "forward")
        try:
            df = self.client.query_kline(
                _to_qdb_symbol(symbol),
                adj_type=adj_type,
                start_date=start.isoformat(),
                end_date=end.isoformat(),
            )
        except Exception as exc:
            raise DataUnavailable(f"QuantDB query_kline failed: {exc}") from exc
        if df is None or df.empty:
            raise DataUnavailable(f"QuantDB empty for {symbol} {start}~{end}")
        df["symbol"] = symbol.upper()
        df["source"] = self.name
        return df

    def fetch_meta(self, market: str) -> pd.DataFrame:
        if market.upper() != "A":
            raise InvalidFieldRequest(f"QuantDB 不支持 market={market}")
        try:
            df = self.client.query_stock_list(limit=10000)
        except Exception as exc:
            raise DataUnavailable(f"QuantDB query_stock_list failed: {exc}") from exc
        if df is None or df.empty:
            raise DataUnavailable("QuantDB stock_list empty")
        df["market"] = "A"
        df["source"] = self.name
        return df

    # ---- 可选 ----
    def fetch_tick(self, symbol: str, trade_date: date) -> pd.DataFrame:
        try:
            df = self.client.query_tick(
                _to_qdb_symbol(symbol),
                trade_date=trade_date.strftime("%Y%m%d"),
            )
        except Exception as exc:
            raise DataUnavailable(f"QuantDB query_tick failed: {exc}") from exc
        if df is None or df.empty:
            raise DataUnavailable(f"QuantDB tick empty for {symbol} {trade_date}")
        df["symbol"] = symbol.upper()
        df["source"] = self.name
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
        if field == "calendar":
            return self._fetch_calendar(start, end)
        if field == "financial_report":
            return self._fetch_financial(symbol, start, end)
        if field == "valuation":
            return self._fetch_valuation(symbol, start, end)
        if field == "ai_factors":
            return self._fetch_ai_factors(symbol, kwargs.get("sub_category", "l1_l2_factors"))
        raise InvalidFieldRequest(f"QuantDB: field={field} not implemented")

    def _fetch_calendar(
        self, start: date | None, end: date | None
    ) -> pd.DataFrame:
        s = start or date.today().replace(year=date.today().year - 1)
        e = end or date.today()
        try:
            df = self.client.query_calendar(
                start_date=s.isoformat(), end_date=e.isoformat()
            )
        except Exception as exc:
            raise DataUnavailable(f"QuantDB calendar failed: {exc}") from exc
        if df is None or df.empty:
            raise DataUnavailable("QuantDB calendar empty")
        df["source"] = self.name
        return df

    def _fetch_financial(
        self, symbol: str, start: date | None, end: date | None
    ) -> pd.DataFrame:
        try:
            df = self.client.load_as_df(
                category_id="3", sub_category="income", symbol=_to_qdb_symbol(symbol)
            )
        except Exception as exc:
            raise DataUnavailable(f"QuantDB financial failed: {exc}") from exc
        if df is None or df.empty:
            raise DataUnavailable(f"QuantDB financial empty for {symbol}")
        df["symbol"] = symbol.upper()
        df["source"] = self.name
        return df

    def _fetch_valuation(
        self, symbol: str, start: date | None, end: date | None
    ) -> pd.DataFrame:
        try:
            df = self.client.load_as_df(
                category_id="5", sub_category="valuation", symbol=_to_qdb_symbol(symbol)
            )
        except Exception as exc:
            raise DataUnavailable(f"QuantDB valuation failed: {exc}") from exc
        if df is None or df.empty:
            raise DataUnavailable(f"QuantDB valuation empty for {symbol}")
        df["symbol"] = symbol.upper()
        df["source"] = self.name
        return df

    def _fetch_ai_factors(
        self, symbol: str, sub_category: str = "l1_l2_factors"
    ) -> pd.DataFrame:
        try:
            df = self.client.load_as_df(
                category_id="6", sub_category=sub_category, symbol=_to_qdb_symbol(symbol)
            )
        except Exception as exc:
            raise DataUnavailable(f"QuantDB ai_factors failed: {exc}") from exc
        if df is None or df.empty:
            raise DataUnavailable(f"QuantDB ai_factors empty for {symbol}")
        df["symbol"] = symbol.upper()
        df["source"] = self.name
        return df


# ---------------------------------------------------------------------------
# SDK 管理辅助函数（供 admin 路由调用）
# ---------------------------------------------------------------------------

def get_sdk_info() -> dict[str, Any]:
    """返回 QuantDB SDK 状态信息。"""
    from backend.shared.runtime_secrets import get_secret

    info: dict[str, Any] = {
        "installed": _QDB_AVAILABLE,
        "api_key_configured": bool(get_secret("QUANTDB_API_KEY")),
    }
    if _QDB_AVAILABLE:
        info["version"] = getattr(
            __import__("quantdb_sdk"), "__version__", "unknown"
        )
    if info["api_key_configured"] and _QDB_AVAILABLE:
        try:
            client = _get_client()
            me = client.get_me()
            usage = client.get_usage()
            info["account"] = me
            info["usage"] = usage
            info["connected"] = True
        except Exception as exc:
            info["connected"] = False
            info["error"] = str(exc)
    else:
        info["connected"] = False
    return info


def register() -> bool:
    """运行时按需调用；返回是否成功注册。"""
    if not _QDB_AVAILABLE:
        logger.info("quantdb-sdk 未安装，跳过 QuantDBAdapter 注册")
        return False
    from backend.shared.runtime_secrets import get_secret

    if not get_secret("QUANTDB_API_KEY"):
        logger.info("QUANTDB_API_KEY 未配置，跳过 QuantDBAdapter 注册")
        return False
    from backend.services.engine.data_platform.registry import get_registry
    get_registry().register(QuantDBAdapter, name=QuantDBAdapter.name)
    return True
