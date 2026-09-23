"""
free-stockdb 数据源适配器
========================

封装 free-stockdb HTTP API (http://host:7899)，提供：
- 日线 K 线（前复权/后复权/不复权），支持范围查询
- 申万行业 + 概念板块映射（双向：股票→板块 / 板块→股票）
- 复权因子（带缓存）
- 分钟 K 线（1/5/15/30/60 min）
- 股票列表（含退市）
- 字段投影（减少传输量）

free-stockdb 是开源本地量化数据引擎，数据以 LevelDB + Zstd 存储，
通过 HTTP API (cmd=get/keys/vals/len) 或 Python SDK (stockdb.pyd) 访问。

API Key 格式：
  日k:{code}:{date}       日线 K 线
  分钟k:{code}:{date}     分钟 K 线
  复权:{code}:{date}      复权因子
  板块:*                  板块数据
  股票代码                全市场代码分组
  退市:*                  退市股票

范围查询语法：{start}<{end}  或  {start}<N (到最新)
字段投影：vals(...).get("close,open") 通过 fields 参数

配置：环境变量 FREE_STOCKDB_HOST（默认 127.0.0.1:7899）
"""

from __future__ import annotations

import logging
import os
from datetime import date
from typing import Any, Optional
from urllib.parse import quote

import pandas as pd
import requests

from backend.services.engine.data_platform.base import (
    DataUnavailable,
    InvalidFieldRequest,
    OfflineDataSourceAdapter,
)

logger = logging.getLogger(__name__)

_DEFAULT_HOST = os.getenv("FREE_STOCKDB_HOST", "127.0.0.1:7899")


def _api_url(
    cmd: str,
    t: str,
    *,
    limit: int | None = None,
    fields: str | None = None,
) -> str:
    base = f"http://{_DEFAULT_HOST}"
    params = f"cmd={cmd}&t={quote(t)}"
    if limit is not None:
        params += f"&limit={limit}"
    if fields:
        params += f"&fields={quote(fields)}"
    return f"{base}/?{params}"


def _api_get(
    cmd: str,
    t: str,
    *,
    limit: int | None = None,
    fields: str | None = None,
    timeout: int = 30,
) -> Any:
    url = _api_url(cmd, t, limit=limit, fields=fields)
    resp = requests.get(url, timeout=timeout)
    if resp.status_code != 200:
        raise DataUnavailable(f"free-stockdb HTTP {resp.status_code}: {url}")
    return resp.json()


def _date_to_int(d: date) -> int:
    return int(d.strftime("%Y%m%d"))


def _to_fsd_code(symbol: str) -> str:
    """SH600036 / 600036.SH -> 600519 纯数字格式（free-stockdb 使用纯数字代码）"""
    s = symbol.strip().upper()
    if "." in s:
        return s.split(".", 1)[0]
    if s.startswith("SH") or s.startswith("SZ") or s.startswith("BJ"):
        return s[2:]
    return s


def _from_fsd_code(code: str) -> str:
    """600519 -> SH600519 / 000001 -> SZ000001 (内部前缀格式)"""
    c = code.strip()
    if c.startswith("6") or c.startswith("9"):
        return f"SH{c}"
    if c.startswith("0") or c.startswith("3") or c.startswith("2"):
        return f"SZ{c}"
    if c.startswith("4") or c.startswith("8"):
        return f"BJ{c}"
    return c


class FreeStockDBAdapter(OfflineDataSourceAdapter):
    """free-stockdb HTTP API 适配器 — 开源本地量化数据引擎。"""

    name = "freestockdb"
    markets = ["A"]
    fields = {
        "daily_kline",
        "minute_kline",
        "adj_factor",
        "sector",
        "stock_list",
        "delisted",
    }

    def __init__(self) -> None:
        self._adj_cache: dict[str, pd.DataFrame] = {}

    def fetch_daily(
        self,
        symbol: str,
        start: date,
        end: date,
        *,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        fsd_code = _to_fsd_code(symbol)
        start_int = _date_to_int(start)
        end_int = _date_to_int(end) if end else None

        # 使用范围查询语法，避免全量拉取
        if end_int:
            t = f"日k:{fsd_code}:{start_int}<{end_int}"
        else:
            t = f"日k:{fsd_code}:{start_int}<N"

        try:
            raw = _api_get("vals", t)
        except Exception as exc:
            raise DataUnavailable(f"free-stockdb 日k failed for {symbol}: {exc}") from exc

        if not raw:
            raise DataUnavailable(f"free-stockdb empty for {symbol}")

        df = pd.DataFrame(raw)
        df["trade_date"] = pd.to_datetime(df["date"].astype(str), format="%Y%m%d").dt.date

        if adjust == "qfq":
            df = self._apply_qfq(df, fsd_code)
        elif adjust == "hfq":
            df = self._apply_hfq(df, fsd_code)

        df["symbol"] = _from_fsd_code(fsd_code)
        df["adj_factor"] = df.get("cum", 1.0)
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
        if field == "adj_factor":
            return self._fetch_adj_factor(_to_fsd_code(symbol))
        if field == "sector":
            direction = kwargs.get("direction", "stock_to_sector")
            if direction == "sector_to_stock":
                return self._fetch_sector_stocks(symbol)
            return self._fetch_sectors_for_stock(_to_fsd_code(symbol))
        if field == "stock_list":
            return self._fetch_stock_list()
        if field == "delisted":
            return self._fetch_delisted()
        if field == "minute_kline":
            freq = kwargs.get("frequency", "1m")
            return self._fetch_minute(symbol, freq, start, end)
        raise InvalidFieldRequest(f"free-stockdb: field={field} not implemented")

    def fetch_meta(self, market: str) -> pd.DataFrame:
        return self._fetch_stock_list()

    # ---- 复权因子（带缓存） ----
    def _fetch_adj_factor(self, fsd_code: str) -> pd.DataFrame:
        if fsd_code in self._adj_cache:
            return self._adj_cache[fsd_code]

        t = f"复权:{fsd_code}:*"
        raw = _api_get("keys", t, limit=500)
        if not raw:
            raise DataUnavailable(f"free-stockdb adj_factor empty for {fsd_code}")

        records = []
        for key in raw:
            parts = key.split(":")
            if len(parts) >= 3:
                dt = parts[2]
                data = _api_get("get", key)
                if data and isinstance(data, dict):
                    records.append({
                        "date": dt,
                        "div": data.get("div", 0),
                        "give": data.get("give", 0),
                        "trans": data.get("trans", 0),
                        "mult": data.get("mult", 1),
                        "cum": data.get("cum", 1),
                    })

        df = pd.DataFrame(records)
        df["trade_date"] = pd.to_datetime(df["date"], format="%Y%m%d", errors="coerce").dt.date
        df["symbol"] = _from_fsd_code(fsd_code)
        df["source"] = self.name
        self._adj_cache[fsd_code] = df
        return df

    # ---- 前复权 / 后复权 ----
    def _apply_qfq(self, df: pd.DataFrame, fsd_code: str) -> pd.DataFrame:
        """前复权：用 cum 因子调整 OHLCV。价格乘以因子，成交量除以因子。"""
        adj_df = self._fetch_adj_factor(fsd_code)
        if adj_df.empty:
            return df
        latest_cum = adj_df["cum"].max()
        for col in ("open", "high", "low", "close", "pre_close"):
            if col in df.columns:
                factor = df["trade_date"].map(
                    lambda d: self._find_cum(adj_df, d, latest_cum)
                )
                df[col] = df[col] * factor
        if "volume" in df.columns:
            vol_factor = df["trade_date"].map(
                lambda d: self._find_cum(adj_df, d, latest_cum)
            )
            df["volume"] = df["volume"] / vol_factor.replace(0, 1.0)
        return df

    def _apply_hfq(self, df: pd.DataFrame, fsd_code: str) -> pd.DataFrame:
        """后复权：价格乘以 cum 因子，成交量除以 cum 因子。"""
        adj_df = self._fetch_adj_factor(fsd_code)
        if adj_df.empty:
            return df
        for col in ("open", "high", "low", "close", "pre_close"):
            if col in df.columns:
                factor = df["trade_date"].map(
                    lambda d: self._find_cum_raw(adj_df, d)
                )
                df[col] = df[col] * factor
        if "volume" in df.columns:
            vol_factor = df["trade_date"].map(
                lambda d: self._find_cum_raw(adj_df, d)
            )
            df["volume"] = df["volume"] / vol_factor.replace(0, 1.0)
        return df

    def _find_cum(self, adj_df: pd.DataFrame, d: date, latest_cum: float) -> float:
        """前复权因子 = latest_cum / cum_on_date。"""
        match = adj_df[adj_df["trade_date"] <= d]
        if match.empty:
            return latest_cum
        cum = match.iloc[-1]["cum"]
        return latest_cum / cum if cum > 0 else 1.0

    def _find_cum_raw(self, adj_df: pd.DataFrame, d: date) -> float:
        """后复权因子 = cum_on_date。"""
        match = adj_df[adj_df["trade_date"] <= d]
        if match.empty:
            return 1.0
        return match.iloc[-1]["cum"]

    # ---- 板块（双向映射） ----
    def _fetch_sectors_for_stock(self, fsd_code: str) -> pd.DataFrame:
        """股票 → 所属板块（申万一级/二级/三级 + 概念）。"""
        records = []
        for level, category in [(1, "申万一级"), (2, "申万二级"), (3, "申万三级")]:
            t = f"板块:{fsd_code}:{level}"
            try:
                raw = _api_get("vals", t, limit=100)
            except DataUnavailable:
                continue
            if raw:
                items = raw if isinstance(raw, list) else [raw]
                for item in items:
                    if isinstance(item, dict):
                        records.append({
                            "symbol": _from_fsd_code(fsd_code),
                            "sector_code": item.get("code", ""),
                            "sector_name": item.get("name", ""),
                            "sector_level": category,
                            "sector_type": item.get("type", ""),
                            "source": self.name,
                        })

        # 概念板块
        t = f"板块:{fsd_code}:0"
        try:
            raw = _api_get("vals", t, limit=500)
        except DataUnavailable:
            raw = None
        if raw:
            items = raw if isinstance(raw, list) else [raw]
            for item in items:
                if isinstance(item, dict):
                    records.append({
                        "symbol": _from_fsd_code(fsd_code),
                        "sector_code": item.get("code", ""),
                        "sector_name": item.get("name", ""),
                        "sector_level": "概念板块",
                        "sector_type": item.get("type", ""),
                        "source": self.name,
                    })

        if not records:
            raise DataUnavailable(f"free-stockdb sectors empty for {fsd_code}")
        return pd.DataFrame(records)

    def _fetch_sector_stocks(self, sector_name: str) -> pd.DataFrame:
        """板块 → 成分股列表。sector_name 可以是板块名称或代码。"""
        t = f"板块:{sector_name}:symbols"
        try:
            raw = _api_get("vals", t, limit=5000)
        except DataUnavailable:
            # 尝试模糊匹配
            t = f"板块:*{sector_name}*:symbols"
            try:
                raw = _api_get("vals", t, limit=5000)
            except DataUnavailable as exc:
                raise DataUnavailable(f"free-stockdb sector_stocks empty for {sector_name}: {exc}") from exc

        if not raw:
            raise DataUnavailable(f"free-stockdb sector_stocks empty for {sector_name}")

        codes = raw if isinstance(raw, list) else [raw]
        df = pd.DataFrame([{"symbol": _from_fsd_code(str(c)), "sector": sector_name} for c in codes])
        df["source"] = self.name
        return df

    # ---- 股票列表（使用 股票代码 key） ----
    def _fetch_stock_list(self) -> pd.DataFrame:
        t = "股票代码"
        try:
            raw = _api_get("get", t)
        except DataUnavailable:
            raise DataUnavailable("free-stockdb stock_list: 股票代码 key not found")

        if not raw or not isinstance(raw, dict):
            raise DataUnavailable("free-stockdb stock_list: 股票代码 returned empty")

        records = []
        for prefix, codes in raw.items():
            if not isinstance(codes, list):
                continue
            for code in codes:
                records.append({
                    "symbol": _from_fsd_code(str(code)),
                    "code": str(code),
                    "market": "A",
                    "prefix": prefix,
                })

        df = pd.DataFrame(records)
        df["source"] = self.name
        return df

    # ---- 退市股票 ----
    def _fetch_delisted(self) -> pd.DataFrame:
        t = "退市*"
        try:
            raw = _api_get("vals", t, limit=5000)
        except DataUnavailable:
            raise DataUnavailable("free-stockdb delisted empty")

        if not raw:
            raise DataUnavailable("free-stockdb delisted empty")

        codes = raw if isinstance(raw, list) else [raw]
        df = pd.DataFrame([{"symbol": _from_fsd_code(str(c)), "code": str(c), "status": "delisted"} for c in codes])
        df["market"] = "A"
        df["source"] = self.name
        return df

    # ---- 分钟线 ----
    def _fetch_minute(
        self,
        symbol: str,
        frequency: str,
        start: date | None = None,
        end: date | None = None,
    ) -> pd.DataFrame:
        fsd_code = _to_fsd_code(symbol)

        # 使用范围查询减少传输量
        if start and end:
            start_int = _date_to_int(start) * 1000000
            end_int = _date_to_int(end) * 235959
            t = f"分钟k:{fsd_code}:{start_int}<{end_int}"
        elif start:
            start_int = _date_to_int(start) * 1000000
            t = f"分钟k:{fsd_code}:{start_int}<N"
        else:
            t = f"分钟k:{fsd_code}:*"

        limit = 50000
        raw = _api_get("vals", t, limit=limit)
        if not raw:
            raise DataUnavailable(f"free-stockdb 分钟k empty for {symbol}")

        df = pd.DataFrame(raw)
        if "date" in df.columns:
            df["trade_date"] = pd.to_datetime(
                df["date"].astype(str), format="%Y%m%d%H%M%S", errors="coerce"
            ).dt.date

        # 按频率过滤（free-stockdb 分钟线 key 不区分频率，需客户端过滤）
        freq_minutes = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60}
        target_min = freq_minutes.get(frequency)
        if target_min and "date" in df.columns:
            dt_series = pd.to_datetime(df["date"].astype(str), format="%Y%m%d%H%M%S", errors="coerce")
            minute_vals = dt_series.dt.minute
            if target_min == 60:
                df = df[minute_vals == 0]
            else:
                df = df[minute_vals % target_min == 0]

        df["symbol"] = _from_fsd_code(fsd_code)
        df["frequency"] = frequency
        df["source"] = self.name
        return df


def register() -> bool:
    """运行时按需调用；返回是否成功注册。"""
    try:
        resp = requests.get(f"http://{_DEFAULT_HOST}/?cmd=len&t=日k:600519:*", timeout=5)
        if resp.status_code != 200:
            logger.info("free-stockdb 不可达，跳过注册")
            return False
    except Exception:
        logger.info("free-stockdb 连接失败，跳过 FreeStockDBAdapter 注册")
        return False

    from backend.services.engine.data_platform.registry import get_registry
    get_registry().register(FreeStockDBAdapter, name=FreeStockDBAdapter.name)
    logger.info("FreeStockDBAdapter 注册成功 (host=%s)", _DEFAULT_HOST)
    return True
