"""
AKShare 数据源
移植自 OPENBB-CN，提供 A 股实时行情、历史 K 线、搜索等功能
"""

import logging
from typing import Any, Dict, List, Optional
import pandas as pd
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)


def _is_st_name(name: Any) -> bool:
    """名称前缀判 ST。

    现货快照自带**当日**名称，属当日口径 —— 不是 ``instrument_detail``
    那种静态快照，因此不存在「拿未来 ST 状态回判历史」的前视偏差。
    """
    return str(name or "").replace(" ", "").upper().startswith(("ST", "*ST"))


def _count_limits(df: pd.DataFrame) -> tuple[int, int]:
    """全市场涨停/跌停家数 —— 口径唯一事实源 = ``market_breadth``。

    原实现按固定 ``>= 9.9`` 判定，是一张写死的主板线，两个方向都错：
    20%/30% 板（创业板/科创板/北交所）上任何 +9.9% 以上的普通阳线被计成
    涨停，「涨停 N 家」虚高；主板 ST 5% 板上真封死的票（+5.0%）则永远漏计。

    容差（沪深 0.5pp / 北交所 1pp）由 ``limit_up_down_counts`` 内部处理，
    此处不重复定义，避免同一指标出现第二个口径。
    """
    pct_col, code_col = df.get("涨跌幅"), df.get("代码")
    if pct_col is None or code_col is None:
        logger.warning("市场概览缺少 涨跌幅/代码 列，涨跌停家数降级为 0")
        return 0, 0

    name_col = df.get("名称")
    names = name_col.tolist() if name_col is not None else [""] * len(df)
    st_symbols = {
        str(c)
        for c, n in zip(code_col.tolist(), names, strict=True)
        if _is_st_name(n)
    }

    # 惰性导入：local_market_data 会拉起 QuantDB 行情栈，而本 provider 的多数
    # 方法（搜索、历史 K 线）并不需要它，放在模块顶层会拖慢整个 data-gateway 启动。
    from backend.shared.market_breadth import limit_up_down_counts

    return limit_up_down_counts(
        pd.to_numeric(pct_col, errors="coerce"),
        code_col.astype(str),
        trade_date=date.today(),
        st_symbols=st_symbols,
    )


class AkShareProvider:
    """AKShare 数据源 Provider"""

    name = "akshare"

    def __init__(self):
        self._client = None

    def _get_client(self):
        if self._client is None:
            import akshare as ak
            self._client = ak
        return self._client

    def get_historical(
        self,
        symbol: str,
        start_date: str | None = None,
        end_date: str | None = None,
        period: str = "daily",
        adjust: str = "qfq",
        days: int = 365,
    ) -> pd.DataFrame:
        """获取历史行情，stock_zh_a_hist 失败时回退到 stock_zh_a_daily"""
        ak = self._get_client()
        symbol = self._normalize_symbol(symbol)

        if end_date is None:
            end_date = datetime.now().strftime("%Y%m%d")
        if start_date is None:
            start_date = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")

        start_clean = start_date.replace("-", "")
        end_clean = end_date.replace("-", "")

        # Primary: stock_zh_a_hist (supports date range)
        try:
            df = ak.stock_zh_a_hist(
                symbol=symbol,
                start_date=start_clean,
                end_date=end_clean,
                period=period,
                adjust=adjust,
            )
            if not df.empty:
                return self._normalize_df(df)
        except Exception as e:
            logger.debug("stock_zh_a_hist failed for %s: %s", symbol, e)

        # Fallback: stock_zh_a_daily (always works from Docker, no date filter)
        try:
            prefix = self._symbol_to_prefix(symbol)
            df = ak.stock_zh_a_daily(symbol=prefix, adjust=adjust)
            if not df.empty:
                df = self._normalize_daily_df(df)
                # Filter by date range
                if "trade_date" in df.columns:
                    start_fmt = datetime.strptime(start_clean, "%Y%m%d").strftime("%Y-%m-%d")
                    end_fmt = datetime.strptime(end_clean, "%Y%m%d").strftime("%Y-%m-%d")
                    df = df[(df["trade_date"] >= start_fmt) & (df["trade_date"] <= end_fmt)]
                return df
        except Exception as e:
            logger.warning("stock_zh_a_daily also failed for %s: %s", symbol, e)

        return pd.DataFrame()

    def get_realtime_quote(self, symbol: str) -> dict[str, Any]:
        """获取实时行情，失败时用最近历史数据兜底"""
        ak = self._get_client()
        symbol = self._normalize_symbol(symbol)

        # Try stock_zh_a_spot_em (full market snapshot)
        try:
            df = ak.stock_zh_a_spot_em()
            row = df[df["代码"] == symbol]
            if not row.empty:
                r = row.iloc[0]
                return {
                    "symbol": symbol,
                    "name": r.get("名称", ""),
                    "price": float(r.get("最新价", 0) or 0),
                    "change": float(r.get("涨跌额", 0) or 0),
                    "pct_change": float(r.get("涨跌幅", 0) or 0),
                    "open": float(r.get("今开", 0) or 0),
                    "high": float(r.get("最高", 0) or 0),
                    "low": float(r.get("最低", 0) or 0),
                    "prev_close": float(r.get("昨收", 0) or 0),
                    "volume": float(r.get("成交量", 0) or 0),
                    "amount": float(r.get("成交额", 0) or 0),
                    "turnover_rate": float(r.get("换手率", 0) or 0),
                    "pe": float(r.get("市盈率-动态", 0) or 0),
                    "pb": float(r.get("市净率", 0) or 0),
                    "total_mv": float(r.get("总市值", 0) or 0),
                    "circ_mv": float(r.get("流通市值", 0) or 0),
                    "timestamp": datetime.now().isoformat(),
                }
        except Exception as e:
            logger.debug("stock_zh_a_spot_em failed: %s", e)

        # Fallback: use latest historical data
        try:
            df = self.get_historical(symbol, days=5)
            if not df.empty:
                latest = df.iloc[-1]
                return {
                    "symbol": symbol,
                    "name": "",
                    "price": float(latest.get("close", 0)),
                    "change": float(latest.get("change", 0)),
                    "pct_change": float(latest.get("pct_change", 0)),
                    "open": float(latest.get("open", 0)),
                    "high": float(latest.get("high", 0)),
                    "low": float(latest.get("low", 0)),
                    "prev_close": 0,
                    "volume": float(latest.get("volume", 0)),
                    "amount": float(latest.get("amount", 0)),
                    "turnover_rate": float(latest.get("turnover_rate", 0)),
                    "pe": 0, "pb": 0, "total_mv": 0, "circ_mv": 0,
                    "timestamp": latest.get("trade_date", datetime.now().isoformat()),
                    "source": "historical_fallback",
                }
        except Exception:
            pass

        return {}

    def search(self, keyword: str, limit: int = 20) -> list[dict[str, str]]:
        """搜索股票"""
        ak = self._get_client()
        df = ak.stock_info_a_code_name()
        mask = df["code"].str.contains(keyword, na=False) | df["name"].str.contains(
            keyword, na=False, case=False
        )
        df = df[mask].head(limit)
        return df.rename(columns={"code": "symbol"}).to_dict("records")

    def get_market_overview(self) -> dict[str, Any]:
        """获取市场概览（涨跌分布、成交额等）"""
        ak = self._get_client()
        df = ak.stock_zh_a_spot_em()

        total = len(df)
        up = len(df[df["涨跌幅"] > 0])
        down = len(df[df["涨跌幅"] < 0])
        flat = total - up - down
        limit_up, limit_down = _count_limits(df)
        total_amount = df["成交额"].sum()

        return {
            "total_stocks": total,
            "up": up,
            "down": down,
            "flat": flat,
            "limit_up": limit_up,
            "limit_down": limit_down,
            "total_amount": float(total_amount),
            "timestamp": datetime.now().isoformat(),
        }

    def get_index_realtime(self) -> list[dict[str, Any]]:
        """获取主要指数实时行情"""
        ak = self._get_client()
        try:
            df = ak.stock_zh_index_spot_em()
            target_indices = {
                "上证指数": "000001",
                "深证成指": "399001",
                "创业板指": "399006",
                "沪深300": "000300",
                "中证500": "000905",
                "中证1000": "000852",
            }
            result = []
            for name, code in target_indices.items():
                row = df[df["代码"] == code]
                if not row.empty:
                    r = row.iloc[0]
                    result.append({
                        "name": name,
                        "code": code,
                        "price": float(r.get("最新价", 0) or 0),
                        "pct_change": float(r.get("涨跌幅", 0) or 0),
                        "change": float(r.get("涨跌额", 0) or 0),
                        "volume": float(r.get("成交量", 0) or 0),
                        "amount": float(r.get("成交额", 0) or 0),
                    })
            return result
        except Exception as e:
            logger.warning("Failed to get index realtime: %s", e)
            return []

    def get_hot_sectors(self, limit: int = 10) -> list[dict[str, Any]]:
        """获取热门板块"""
        ak = self._get_client()
        try:
            df = ak.stock_board_industry_name_em()
            df = df.sort_values("涨跌幅", ascending=False).head(limit)
            return [
                {
                    "name": row["板块名称"],
                    "pct_change": float(row.get("涨跌幅", 0) or 0),
                    "turnover": float(row.get("换手率", 0) or 0),
                    "amount": float(row.get("总成交额", 0) or 0),
                    "leader": row.get("领涨股票", ""),
                    "leader_pct": float(row.get("领涨股票-涨跌幅", 0) or 0),
                }
                for _, row in df.iterrows()
            ]
        except Exception as e:
            logger.warning("Failed to get hot sectors: %s", e)
            return []

    def _normalize_symbol(self, symbol: str) -> str:
        """标准化股票代码为纯数字"""
        symbol = symbol.strip()
        for suffix in [".SH", ".SZ", ".BJ", ".sh", ".sz", ".bj"]:
            symbol = symbol.replace(suffix, "")
        return symbol

    def _symbol_to_prefix(self, symbol: str) -> str:
        """纯数字代码 -> 前缀格式 (sh600519)"""
        code = self._normalize_symbol(symbol)
        if code.startswith("6") or code.startswith("688") or code.startswith("9"):
            return f"sh{code}"
        elif code.startswith(("0", "3", "2")):
            return f"sz{code}"
        elif code.startswith(("4", "8")):
            return f"bj{code}"
        return f"sh{code}"

    def _normalize_df(self, df: pd.DataFrame) -> pd.DataFrame:
        """标准化 stock_zh_a_hist DataFrame 列名"""
        column_mapping = {
            "日期": "trade_date",
            "开盘": "open",
            "收盘": "close",
            "最高": "high",
            "最低": "low",
            "成交量": "volume",
            "成交额": "amount",
            "涨跌幅": "pct_change",
            "涨跌额": "change",
            "换手率": "turnover_rate",
        }
        df = df.rename(columns=column_mapping)
        if "trade_date" in df.columns:
            df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
        return df

    def _normalize_daily_df(self, df: pd.DataFrame) -> pd.DataFrame:
        """标准化 stock_zh_a_daily DataFrame 列名"""
        column_mapping = {
            "date": "trade_date",
            "open": "open",
            "close": "close",
            "high": "high",
            "low": "low",
            "volume": "volume",
            "amount": "amount",
        }
        df = df.rename(columns=column_mapping)
        if "trade_date" in df.columns:
            df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
        return df
