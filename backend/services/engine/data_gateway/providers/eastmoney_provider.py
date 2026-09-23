"""
东方财富数据源
移植自 OPENBB-CN，提供实时行情、历史 K 线、资金流向等功能
"""

import logging
import os
import time
from typing import Any, Dict, List, Optional
import pandas as pd
import httpx
from datetime import datetime

logger = logging.getLogger(__name__)

BASE_URL = "https://push2.eastmoney.com"


class EastMoneyProvider:
    """东方财富数据源 Provider"""

    name = "eastmoney"

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.eastmoney.com",
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
    }

    def __init__(self):
        self._client = None

    def _get_client(self) -> httpx.Client:
        if self._client is None or self._client.is_closed:
            self._client = httpx.Client(
                timeout=httpx.Timeout(connect=10.0, read=15.0, write=10.0, pool=10.0),
                headers=self.HEADERS,
                follow_redirects=True,
                http2=False,
            )
        return self._client

    def _get(self, url, params, timeout=15):
        """发送 GET 请求，带指数退避重试"""
        client = self._get_client()
        last_err = None
        for attempt in range(4):
            try:
                resp = client.get(url, params=params, timeout=timeout)
                resp.raise_for_status()
                return resp
            except Exception as e:
                last_err = e
                if attempt < 3:
                    wait = 0.5 * (2 ** attempt)  # 0.5, 1, 2 seconds
                    logger.debug("Retry %d for %s after %.1fs: %s", attempt + 1, url, wait, e)
                    time.sleep(wait)
                    # Reset client on connection errors
                    try:
                        self._client.close()
                    except Exception:
                        pass
                    self._client = None
                else:
                    raise

    def get_historical(
        self,
        symbol: str,
        start_date: str | None = None,
        end_date: str | None = None,
        period: str = "daily",
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        """获取历史行情"""
        secid = self._symbol_to_secid(symbol)

        period_map = {
            "daily": "101", "weekly": "102", "monthly": "103",
            "1min": "1", "5min": "5", "15": "15", "30min": "30", "60min": "60",
        }
        adj_map = {"qfq": "2", "hfq": "1", "none": "0"}

        params = {
            "secid": secid,
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": period_map.get(period, "101"),
            "fqt": adj_map.get(adjust, "2"),
            "beg": (start_date or "").replace("-", ""),
            "end": (end_date or "20500101").replace("-", ""),
            "lmt": 1000000,
        }

        resp = self._get(f"{BASE_URL}/api/qt/stock/kline/get", params=params, timeout=15)
        data = resp.json()

        if not data.get("data") or not data["data"].get("klines"):
            return pd.DataFrame()

        records = []
        for kl in data["data"]["klines"]:
            p = kl.split(",")
            records.append({
                "trade_date": p[0],
                "open": float(p[1]),
                "close": float(p[2]),
                "high": float(p[3]),
                "low": float(p[4]),
                "volume": float(p[5]),
                "amount": float(p[6]) if len(p) > 6 else 0,
            })
        return pd.DataFrame(records)

    def get_realtime_quote(self, symbol: str) -> dict[str, Any]:
        """获取实时行情"""
        secid = self._symbol_to_secid(symbol)

        params = {
            "secid": secid,
            "fields": "f43,f44,f45,f46,f47,f48,f50,f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f170,f168,f169",
            "ut": "fa5fd1943c7b386f172d6893dbbd5d51",
        }

        resp = self._get(f"{BASE_URL}/api/qt/stock/get", params=params, timeout=10)
        data = resp.json()

        if not data.get("data"):
            return {}

        d = data["data"]
        return {
            "symbol": symbol,
            "name": d.get("f58", ""),
            "price": d.get("f43", 0) / 100,
            "open": d.get("f46", 0) / 100,
            "high": d.get("f44", 0) / 100,
            "low": d.get("f45", 0) / 100,
            "volume": d.get("f47", 0),
            "amount": d.get("f48", 0),
            "pct_change": d.get("f170", 0) / 100,
            "change": d.get("f169", 0) / 100,
            "turnover_rate": d.get("f168", 0) / 100,
            "timestamp": datetime.now().isoformat(),
        }

    def search(self, keyword: str, limit: int = 20) -> list[dict[str, str]]:
        """搜索股票"""
        params = {
            "input": keyword,
            "type": "14",
            "token": os.getenv("EASTMONEY_SEARCH_TOKEN", ""),
            "count": limit,
        }

        resp = self._get(
            "https://searchapi.eastmoney.com/api/suggest/get", params=params, timeout=10
        )
        data = resp.json()

        if not data.get("QuotationCodeTable") or not data["QuotationCodeTable"].get("Data"):
            return []

        return [
            {
                "symbol": item.get("Code", ""),
                "name": item.get("Name", ""),
                "market": item.get("MktNum", ""),
            }
            for item in data["QuotationCodeTable"]["Data"]
        ]

    def get_fund_flow(self, symbol: str, limit: int = 60) -> pd.DataFrame:
        """获取资金流向"""
        secid = self._symbol_to_secid(symbol)
        params = {
            "secid": secid,
            "fields1": "f1,f2,f3,f7,f8,f10,f12",
            "klt": "101",
            "lmt": limit,
            "ut": "7eea3edcaed734bea9cbfc24409ed989",
        }

        resp = self._get(
            "https://push2.eastmoney.com/api/qt/stock/fflow/get", params=params, timeout=10
        )
        data = resp.json()

        if not data.get("data") or not data["data"].get("klines"):
            return pd.DataFrame()

        records = []
        for kl in data["data"]["klines"]:
            p = kl.split(",")
            records.append({
                "date": p[1],
                "pct_change": float(p[2]) if p[2] else 0,
                "net_inflow": float(p[3]) if p[3] else 0,
                "main_net_inflow": float(p[4]) if p[4] else 0,
                "retail_net_inflow": float(p[5]) if p[5] else 0,
            })
        return pd.DataFrame(records)

    def _symbol_to_secid(self, symbol: str) -> str:
        """股票代码 -> 东方财富 secid 格式 (如 1.600519)"""
        symbol = symbol.strip().upper()

        # 已有后缀
        for suffix in [".SH", ".SZ", ".BJ"]:
            if suffix in symbol:
                code = symbol.replace(suffix, "")
                market = {"SH": "1", "SZ": "0", "BJ": "0"}[suffix[1:]]
                return f"{market}.{code}"

        # 纯数字
        code = symbol.replace(".", "")
        if code.startswith("6") or code.startswith("688"):
            return f"1.{code}"
        elif code.startswith(("0", "3", "4", "8")):
            return f"0.{code}"
        return f"1.{code}"

    def __del__(self):
        try:
            if self._client and not self._client.is_closed:
                self._client.close()
        except Exception:
            pass
