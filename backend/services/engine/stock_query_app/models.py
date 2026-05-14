#!/usr/bin/env python3
"""
股票查询系统数据模型
定义股票相关的数据结构和模型类
"""

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Dict, List, Optional


class MarketType(Enum):
    """市场类型枚举"""

    SZ = "SZ"  # 深圳证券交易所
    SH = "SH"  # 上海证券交易所
    BJ = "BJ"  # 北京证券交易所
    HK = "HK"  # 香港证券交易所
    US = "US"  # 美国证券交易所


class TradeStatus(Enum):
    """交易状态枚举"""

    TRADING = "trading"  # 正常交易
    SUSPENDED = "suspended"  # 停牌
    DELISTED = "delisted"  # 退市
    PRE_MARKET = "pre_market"  # 盘前
    AFTER_MARKET = "after_market"  # 盘后
    CLOSED = "closed"  # 休市


class AdjustType(Enum):
    """复权类型枚举"""

    NONE = "none"  # 不复权
    FORWARD = "qfq"  # 前复权
    BACKWARD = "hfq"  # 后复权


class DataFrequency(Enum):
    """数据频率枚举"""

    MINUTE_1 = "1m"  # 1分钟
    MINUTE_5 = "5m"  # 5分钟
    MINUTE_15 = "15m"  # 15分钟
    MINUTE_30 = "30m"  # 30分钟
    HOUR_1 = "1h"  # 1小时
    DAILY = "D"  # 日线
    WEEKLY = "W"  # 周线
    MONTHLY = "M"  # 月线


@dataclass
class StockInfo:
    """股票基础信息"""

    code: str  # 股票代码 (SZ000001)
    name: str  # 股票名称
    market: MarketType  # 市场类型
    industry: str | None = None  # 所属行业
    sector: str | None = None  # 所属板块
    list_date: date | None = None  # 上市日期
    total_shares: float | None = None  # 总股本(万股)
    float_shares: float | None = None  # 流通股本(万股)
    pe_ttm: float | None = None  # 市盈率TTM
    pb: float | None = None  # 市净率
    ps_ttm: float | None = None  # 市销率TTM
    pcf_ncf_ttm: float | None = None  # 市现率TTM
    roe: float | None = None  # 净资产收益率
    net_profit_margin: float | None = None  # 净利率
    gross_profit_margin: float | None = None  # 毛利率
    is_st: bool = False  # 是否为ST股
    turnover_rate: float | None = None  # 换手率
    status: TradeStatus = TradeStatus.TRADING  # 交易状态
    company_name: str | None = None  # 公司全称
    exchange: str | None = None  # 交易所
    currency: str = "CNY"  # 交易货币
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict[str, Any]:
        """转换为字典格式"""
        return {
            "code": self.code,
            "name": self.name,
            "market": self.market.value,
            "industry": self.industry,
            "sector": self.sector,
            "list_date": self.list_date.isoformat() if self.list_date else None,
            "total_shares": self.total_shares,
            "float_shares": self.float_shares,
            "pe_ttm": self.pe_ttm,
            "pb": self.pb,
            "turnover_rate": self.turnover_rate,
            "status": self.status.value,
            "company_name": self.company_name,
            "exchange": self.exchange,
            "currency": self.currency,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StockInfo":
        """从字典创建实例"""
        return cls(
            code=data["code"],
            name=data["name"],
            market=MarketType(data["market"]),
            industry=data.get("industry"),
            sector=data.get("sector"),
            list_date=(date.fromisoformat(data["list_date"]) if data.get("list_date") else None),
            total_shares=data.get("total_shares"),
            float_shares=data.get("float_shares"),
            pe_ttm=data.get("pe_ttm"),
            pb=data.get("pb"),
            ps_ttm=data.get("ps_ttm"),
            pcf_ncf_ttm=data.get("pcf_ncf_ttm"),
            roe=data.get("roe"),
            net_profit_margin=data.get("net_profit_margin"),
            gross_profit_margin=data.get("gross_profit_margin"),
            is_st=data.get("is_st", False),
            turnover_rate=data.get("turnover_rate"),
            status=TradeStatus(data.get("status", "trading")),
            company_name=data.get("company_name"),
            exchange=data.get("exchange"),
            currency=data.get("currency", "CNY"),
            created_at=datetime.fromisoformat(data.get("created_at", datetime.now().isoformat())),
            updated_at=datetime.fromisoformat(data.get("updated_at", datetime.now().isoformat())),
        )


@dataclass
class RealtimeQuote:
    """实时行情数据"""

    code: str  # 股票代码
    timestamp: datetime  # 时间戳
    latest: float | None = None  # 最新价
    open: float | None = None  # 开盘价
    high: float | None = None  # 最高价
    low: float | None = None  # 最低价
    pre_close: float | None = None  # 昨收价
    volume: int | None = None  # 成交量(股)
    amount: float | None = None  # 成交额(元)
    chg: float | None = None  # 涨跌额
    chg_pct: float | None = None  # 涨跌幅(%)
    turnover_ratio: float | None = None  # 换手率(%)
    pe_ttm: float | None = None  # 市盈率TTM
    pb: float | None = None  # 市净率
    market_cap: float | None = None  # 总市值(万元)
    float_market_cap: float | None = None  # 流通市值(万元)
    vol_ratio: float | None = None  # 量比
    bid1: float | None = None  # 买一价
    ask1: float | None = None  # 卖一价
    bid1_size: int | None = None  # 买一量
    ask1_size: int | None = None  # 卖一量

    def to_dict(self) -> dict[str, Any]:
        """转换为字典格式"""
        return {
            "code": self.code,
            "timestamp": self.timestamp.isoformat(),
            "latest": self.latest,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "pre_close": self.pre_close,
            "volume": self.volume,
            "amount": self.amount,
            "chg": self.chg,
            "chg_pct": self.chg_pct,
            "turnover_ratio": self.turnover_ratio,
            "pe_ttm": self.pe_ttm,
            "pb": self.pb,
            "market_cap": self.market_cap,
            "float_market_cap": self.float_market_cap,
            "vol_ratio": self.vol_ratio,
            "bid1": self.bid1,
            "ask1": self.ask1,
            "bid1_size": self.bid1_size,
            "ask1_size": self.ask1_size,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RealtimeQuote":
        """从字典创建实例"""
        return cls(
            code=data["code"],
            timestamp=datetime.fromisoformat(data["timestamp"]),
            latest=data.get("latest"),
            open=data.get("open"),
            high=data.get("high"),
            low=data.get("low"),
            pre_close=data.get("pre_close"),
            volume=data.get("volume"),
            amount=data.get("amount"),
            chg=data.get("chg"),
            chg_pct=data.get("chg_pct"),
            turnover_ratio=data.get("turnover_ratio"),
            pe_ttm=data.get("pe_ttm"),
            pb=data.get("pb"),
            market_cap=data.get("market_cap"),
            float_market_cap=data.get("float_market_cap"),
            vol_ratio=data.get("vol_ratio"),
            bid1=data.get("bid1"),
            ask1=data.get("ask1"),
            bid1_size=data.get("bid1_size"),
            ask1_size=data.get("ask1_size"),
        )


@dataclass
class HistoricalQuote:
    """历史行情数据"""

    code: str  # 股票代码
    trade_date: date  # 交易日期
    open: float | None = None  # 开盘价
    high: float | None = None  # 最高价
    low: float | None = None  # 最低价
    close: float | None = None  # 收盘价
    volume: int | None = None  # 成交量(股)
    amount: float | None = None  # 成交额(元)
    adj_close: float | None = None  # 复权收盘价
    chg: float | None = None  # 涨跌额
    chg_pct: float | None = None  # 涨跌幅(%)
    turnover_ratio: float | None = None  # 换手率(%)
    pe_ttm: float | None = None  # 市盈率TTM
    pb: float | None = None  # 市净率

    def to_dict(self) -> dict[str, Any]:
        """转换为字典格式"""
        return {
            "code": self.code,
            "trade_date": self.trade_date.isoformat(),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "amount": self.amount,
            "adj_close": self.adj_close,
            "chg": self.chg,
            "chg_pct": self.chg_pct,
            "turnover_ratio": self.turnover_ratio,
            "pe_ttm": self.pe_ttm,
            "pb": self.pb,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HistoricalQuote":
        """从字典创建实例"""
        return cls(
            code=data["code"],
            trade_date=date.fromisoformat(data["trade_date"]),
            open=data.get("open"),
            high=data.get("high"),
            low=data.get("low"),
            close=data.get("close"),
            volume=data.get("volume"),
            amount=data.get("amount"),
            adj_close=data.get("adj_close"),
            chg=data.get("chg"),
            chg_pct=data.get("chg_pct"),
            turnover_ratio=data.get("turnover_ratio"),
            pe_ttm=data.get("pe_ttm"),
            pb=data.get("pb"),
        )


@dataclass
class TechnicalIndicator:
    """技术指标数据"""

    code: str  # 股票代码
    trade_date: date  # 交易日期
    indicator_name: str  # 指标名称
    values: dict[str, float]  # 指标值字典

    def to_dict(self) -> dict[str, Any]:
        """转换为字典格式"""
        return {
            "code": self.code,
            "trade_date": self.trade_date.isoformat(),
            "indicator_name": self.indicator_name,
            "values": self.values,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TechnicalIndicator":
        """从字典创建实例"""
        return cls(
            code=data["code"],
            trade_date=date.fromisoformat(data["trade_date"]),
            indicator_name=data["indicator_name"],
            values=data["values"],
        )


@dataclass
class QueryRequest:
    """查询请求基类"""

    codes: list[str]  # 股票代码列表
    start_date: date | None = None  # 开始日期
    end_date: date | None = None  # 结束日期
    limit: int = 100  # 返回数量限制

    def to_dict(self) -> dict[str, Any]:
        """转换为字典格式"""
        return {
            "codes": self.codes,
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "end_date": self.end_date.isoformat() if self.end_date else None,
            "limit": self.limit,
        }


@dataclass
class RealtimeQueryRequest(QueryRequest):
    """实时行情查询请求"""

    indicators: list[str] = field(
        default_factory=lambda: [
            "latest",
            "open",
            "high",
            "low",
            "pre_close",
            "volume",
            "amount",
            "chg",
            "chg_pct",
            "turnover_ratio",
            "pe_ttm",
            "pb",
        ]
    )


@dataclass
class HistoricalQueryRequest(QueryRequest):
    """历史数据查询请求"""

    frequency: DataFrequency = DataFrequency.DAILY  # 数据频率
    adjust_type: AdjustType = AdjustType.FORWARD  # 复权类型
    indicators: list[str] = field(
        default_factory=lambda: [
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount",
            "chg_pct",
        ]
    )


@dataclass
class TechnicalIndicatorRequest(QueryRequest):
    """技术指标查询请求"""

    indicators: list[str] = field(default_factory=lambda: ["MA5", "MA10", "MA20"])
    frequency: DataFrequency = DataFrequency.DAILY
    parameters: dict[str, Any] = field(default_factory=dict)  # 指标参数


@dataclass
class SearchRequest:
    """股票搜索请求"""

    keyword: str  # 搜索关键词
    search_type: str = "all"  # 搜索类型: code, name, pinyin, industry, all
    market: MarketType | None = None  # 市场限制
    limit: int = 20  # 返回数量限制
    offset: int = 0  # 分页偏移量

    def to_dict(self) -> dict[str, Any]:
        """转换为字典格式"""
        return {
            "keyword": self.keyword,
            "search_type": self.search_type,
            "market": self.market.value if self.market else None,
            "limit": self.limit,
            "offset": self.offset,
        }


@dataclass
class QueryResponse:
    """查询响应基类"""

    success: bool  # 是否成功
    message: str = ""  # 响应消息
    data: Any = None  # 响应数据
    total: int = 0  # 总数量
    timestamp: datetime = field(default_factory=datetime.now)  # 响应时间戳

    def to_dict(self) -> dict[str, Any]:
        """转换为字典格式"""
        return {
            "success": self.success,
            "message": self.message,
            "data": self.data,
            "total": self.total,
            "timestamp": self.timestamp.isoformat(),
        }

    def to_json(self) -> str:
        """转换为JSON字符串"""
        return json.dumps(self.to_dict(), ensure_ascii=False, default=str)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "QueryResponse":
        """从字典创建实例"""
        return cls(
            success=data["success"],
            message=data.get("message", ""),
            data=data.get("data"),
            total=data.get("total", 0),
            timestamp=datetime.fromisoformat(data.get("timestamp", datetime.now().isoformat())),
        )


# 常用指标映射
INDICATOR_MAPPING = {
    # 实时行情指标
    "latest": "最新价",
    "open": "开盘价",
    "high": "最高价",
    "low": "最低价",
    "pre_close": "昨收价",
    "volume": "成交量",
    "amount": "成交额",
    "chg": "涨跌额",
    "chg_pct": "涨跌幅",
    "turnover_ratio": "换手率",
    "pe_ttm": "市盈率TTM",
    "pb": "市净率",
    "market_cap": "总市值",
    "float_market_cap": "流通市值",
    # 技术指标
    "MA5": "5日均线",
    "MA10": "10日均线",
    "MA20": "20日均线",
    "MA60": "60日均线",
    "MACD": "MACD",
    "RSI": "RSI",
    "KDJ": "KDJ",
    "BOLL": "布林带",
    "WR": "威廉指标",
    "CCI": "CCI",
    "OBV": "OBV",
    "VOL": "成交量",
}


# 市场代码映射
MARKET_CODE_MAPPING = {
    "SZ": "深圳证券交易所",
    "SH": "上海证券交易所",
    "BJ": "北京证券交易所",
    "HK": "香港证券交易所",
    "US": "美国证券交易所",
}


def parse_stock_code(code: str) -> tuple[str, MarketType]:
    """解析股票代码，返回代码和市场类型

    Args:
        code: 股票代码，如 '000001.SZ', '000001', 'SH600000' 或 'sz000001'

    Returns:
        tuple: (纯代码, 市场类型)
    """
    cleaned = str(code or "").strip().upper()
    if not cleaned:
        return "", MarketType.SZ

    # 1. 处理后缀格式 (000001.SZ)
    if "." in cleaned:
        parts = cleaned.split(".", 1)
        stock_code = parts[0]
        market_suffix = parts[1]
        try:
            market = MarketType(market_suffix)
        except ValueError:
            market = MarketType.SZ  # 默认深圳
        return stock_code, market

    # 2. 处理前缀格式 (SH600000, SZ000001)
    if cleaned.startswith("SH") and len(cleaned) > 2 and cleaned[2:].isdigit():
        return cleaned[2:], MarketType.SH
    if cleaned.startswith("SZ") and len(cleaned) > 2 and cleaned[2:].isdigit():
        return cleaned[2:], MarketType.SZ
    if cleaned.startswith("BJ") and len(cleaned) > 2 and cleaned[2:].isdigit():
        return cleaned[2:], MarketType.BJ

    # 3. 处理纯数字格式 (600000, 000001)，根据前缀推断
    stock_code = cleaned
    if cleaned.startswith(("000", "001", "002", "003", "300")):
        market = MarketType.SZ
    elif cleaned.startswith(("600", "601", "603", "605", "688")):
        market = MarketType.SH
    elif cleaned.startswith(("4", "8", "9")):
        market = MarketType.BJ
    else:
        market = MarketType.SZ  # 默认深圳

    return stock_code, market


def format_stock_code(code: str, market: MarketType) -> str:
    """格式化股票代码

    Args:
        code: 纯股票代码
        market: 市场类型

    Returns:
        str: 格式化后的代码，如 '000001.SZ'
    """
    return f"{market.value}{code}"
