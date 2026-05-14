import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from .data_source import DataSourceAdapter

logger = logging.getLogger(__name__)


class IFindDataSource(DataSourceAdapter):
    """
    同花顺 iFind 数据源
    使用 HTTP API 获取实时行情
    """

    # Verified Endpoint
    BASE_URL = "https://quantapi.51ifind.com/api/v1/real_time_quotation"

    def __init__(self):
        self.access_token = os.getenv("IFIND_ACCESS_TOKEN")
        self.refresh_token = os.getenv("IFIND_REFRESH_TOKEN")
        # Assumption, verifying dynamically might be needed
        self.token_refresh_url = "https://quantapi.51ifind.com/api/v1/refresh_token"
        # Actually refresh logic is separate, for now assume token is valid or refreshed externally
        # But for robustness, we can try to reload from env if 401

    async def fetch_quote(self, symbol: str) -> dict[str, Any] | None:
        """
        获取实时行情

        Args:
            symbol: 股票代码 (e.g. "SZ000001", "SH600000")
                    iFind format is usually just the code with suffix.
                    Our internal symbol might be "SZ000001".
                    Need to normalize.
        """
        try:
            if not self.access_token:
                logger.error("IFIND_ACCESS_TOKEN not set")
                return None

            # Normalize symbol to iFind format (e.g., 000001.SZ)
            ifind_symbol = self._format_symbol(symbol)

            headers = {
                "Content-Type": "application/json",
                "access_token": self.access_token,
            }

            payload = {
                "codes": ifind_symbol,
                "indicators": "latestVolume,latest_price,open,high,low,preClose,amount,bid1,bid1_volume,bid2,bid2_volume,bid3,bid3_volume,bid4,bid4_volume,bid5,bid5_volume,ask1,ask1_volume,ask2,ask2_volume,ask3,ask3_volume,ask4,ask4_volume,ask5,ask5_volume",
            }

            # Use a slightly longer timeout for external API
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(self.BASE_URL, headers=headers, json=payload)

                if response.status_code == 200:
                    data = response.json()
                    error_code = data.get("errorcode")

                    if error_code == 0:
                        return self._parse_quote(data, symbol)

                    # Check for token expiration error codes (broad check for now)
                    # iFind often uses non-zero error codes for auth issues
                    # If we have a refresh token, try to refresh and retry once
                    elif self.refresh_token and "token" in str(data.get("errmsg", "")).lower():
                        logger.warning(f"Token expired or invalid: {data.get('errmsg')}. Refreshing...")
                        if await self._refresh_access_token(client):
                            # Retry with new token
                            headers["access_token"] = self.access_token
                            response = await client.post(self.BASE_URL, headers=headers, json=payload)
                            if response.status_code == 200 and response.json().get("errorcode") == 0:
                                return self._parse_quote(response.json(), symbol)

                    logger.error(f"iFind API error: {data.get('errmsg', 'Unknown error')}")
                    return None

                elif response.status_code == 401:
                    logger.warning("401 Unauthorized. Refreshing token...")
                    if await self._refresh_access_token(client):
                        headers["access_token"] = self.access_token
                        response = await client.post(self.BASE_URL, headers=headers, json=payload)
                        if response.status_code == 200:
                            return self._parse_quote(response.json(), symbol)

                    logger.error("Failed to refresh token or retry failed")
                    return None
                else:
                    logger.error(f"iFind API HTTP error: {response.status_code} - {response.text}")
                    return None

        except Exception as e:
            logger.error(f"Error fetching quote from iFind: {e}")
            return None

    async def _refresh_access_token(self, client: httpx.AsyncClient) -> bool:
        """
        Refresh Access Token using Refresh Token
        """
        try:
            if not self.refresh_token:
                logger.error("No refresh token available")
                return False

            # Endpoints to try
            endpoints = [
                "https://quantapi.51ifind.com/api/v1/get_access_token",
                "https://quantapi.10jqka.com.cn/api/v1/get_access_token",
            ]

            # Clean token if needed (remove '==' prefix if present, logic from script)
            clean_token = self.refresh_token.lstrip("=").strip()

            headers = {
                "Content-Type": "application/json",
                "refresh_token": clean_token,  # Try this key first
            }

            for url in endpoints:
                try:
                    response = await client.post(url, headers=headers)
                    if response.status_code == 200:
                        data = response.json()
                        new_token = None

                        if "access_token" in data:
                            new_token = data["access_token"]
                        elif "data" in data and "access_token" in data["data"]:
                            new_token = data["data"]["access_token"]

                        if new_token:
                            self.access_token = new_token
                            logger.info("Successfully refreshed access token")
                            # Ideally update .env here or notify system, but for now in-memory is fine
                            return True
                except Exception as e:
                    logger.warning(f"Failed to refresh from {url}: {e}")

            logger.error("All refresh attempts failed")
            return False

        except Exception as e:
            logger.error(f"Error refreshing token: {e}")
            return False

    def _format_symbol(self, symbol: str) -> str:
        """
        将内部 symbol 转换为 iFind 格式
        Input: "SZ000001", "SH600000"
        Output: "000001.SZ", "600000.SH"
        """
        s = symbol.lower()
        if s.startswith("sz"):
            return f"{s[2:]}.SZ"
        elif s.startswith("sh"):
            return f"{s[2:]}.SH"
        elif s.endswith(".sz") or s.endswith(".sh") or s.endswith(".bj"):
            return symbol.upper()

        # Guess based on first digit
        if s.startswith("6"):
            return f"{s}.SH"
        else:
            return f"{s}.SZ"

    def _parse_quote(self, data: dict[str, Any], original_symbol: str) -> dict[str, Any] | None:
        """
        解析 iFind 返回的 JSON 数据

        Example Structure:
        {
            "tables": [
                {
                    "thscode": "000001.SZ",
                    "time": ["2026-02-06 16:00:54"],
                    "table": {
                        "latestVolume": [598846.0],
                        "latest_price": [11.05],
                        ...
                    }
                }
            ]
        }
        """
        try:
            tables = data.get("tables", [])
            if not tables:
                return None

            # Assume single stock query, take first item
            item = tables[0]
            table_data = item.get("table", {})

            def get_val(key, default=0.0):
                vals = table_data.get(key, [])
                if vals and len(vals) > 0:
                    return vals[0]
                return default

            current_price = get_val("latest_price")
            # verify case sensitivity or mapping
            pre_close = get_val("preClose")

            # If preClose is missing/0, use open or calculate?
            # Better to be safe.

            change = current_price - pre_close if pre_close else 0.0
            change_percent = (change / pre_close * 100) if pre_close > 0 else 0.0

            quote = {
                "symbol": original_symbol,
                "timestamp": datetime.now(timezone.utc),  # Or parse item['time'][0]
                "current_price": current_price,
                "open_price": get_val("open"),
                "high_price": get_val("high"),
                "low_price": get_val("low"),
                "pre_close": pre_close,
                "volume": int(get_val("latestVolume")),
                "amount": get_val("amount"),
                "change": change,
                "change_percent": round(change_percent, 2),
                "data_source": "ifind",
                # Bid 1-5
                "bid1_price": get_val("bid1"),
                "bid1_volume": int(get_val("bid1_volume")),
                "bid2_price": get_val("bid2"),
                "bid2_volume": int(get_val("bid2_volume")),
                "bid3_price": get_val("bid3"),
                "bid3_volume": int(get_val("bid3_volume")),
                "bid4_price": get_val("bid4"),
                "bid4_volume": int(get_val("bid4_volume")),
                "bid5_price": get_val("bid5"),
                "bid5_volume": int(get_val("bid5_volume")),
                # Ask 1-5
                "ask1_price": get_val("ask1"),
                "ask1_volume": int(get_val("ask1_volume")),
                "ask2_price": get_val("ask2"),
                "ask2_volume": int(get_val("ask2_volume")),
                "ask3_price": get_val("ask3"),
                "ask3_volume": int(get_val("ask3_volume")),
                "ask4_price": get_val("ask4"),
                "ask4_volume": int(get_val("ask4_volume")),
                "ask5_price": get_val("ask5"),
                "ask5_volume": int(get_val("ask5_volume")),
            }
            return quote

        except Exception as e:
            logger.error(f"Error parsing iFind data: {e}")
            return None

    # Implement other abstract methods with empty default
    async def fetch_kline(
        self,
        symbol: str,
        interval: str,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return []

    async def fetch_symbols(self, market: str | None = None) -> list[dict[str, Any]]:
        return []
