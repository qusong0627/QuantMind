"""模拟盘多市场交易规则。

每个市场的交易规则差异集中在这里表达：
- 回转交易：CN T+1（当日买入锁到次日），其余 T+0
- 最小交易单位：CN 100 股整手；HK 按每手股数（board lot，缺省 1 表示
  按标的元数据，未接入时退化为 1 股）；US/期货/加密 1
- 涨跌停：仅 CN 有（±10%/创业板科创板 ±20%/北交所 ±30%，见 local_market_data）
- 费用：比例佣金 + 最低佣金 + 印花税（CN 卖出 0.05%、HK 双边 0.1% 均以
  seller 单边口径简化）
- 币种：账户展示用；模拟盘金额仍以账户 base_currency 计价

symbol → 市场推断规则（infer_market）：
  0001.HK            → HK
  600036.SH / 000001 → CN
  RB0.CN / CL.FUT / Au99.99 → FUTURES
  BTCUSDT / ETHUSDT  → CRYPTO
  AAPL               → US
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Market(str, Enum):
    CN = "CN"
    HK = "HK"
    US = "US"
    FUTURES = "FUTURES"
    CRYPTO = "CRYPTO"


_MARKET_CURRENCIES: dict[Market, str] = {
    Market.CN: "CNY",
    Market.HK: "HKD",
    Market.US: "USD",
    Market.FUTURES: "CNY",
    Market.CRYPTO: "USDT",
}

# 老虎/富途/IB 等券商的 broker_id
SUPPORTED_BROKERS: dict[Market, tuple[str, ...]] = {
    Market.CN: ("qmt", "tdx"),
    Market.HK: ("futu", "tiger", "ib"),
    Market.US: ("tiger", "ib", "futu"),
    Market.FUTURES: ("ib",),
    Market.CRYPTO: (),
}


@dataclass(frozen=True)
class MarketTradingRules:
    """单个市场的模拟撮合规则。"""

    market: Market
    currency: str
    # 买入是否锁定至次日可卖（T+1）
    t_plus_1: bool
    # 最小买入单位（股/张/枚）。CN=100 整手；其余市场 1。
    lot_size: int
    # 比例佣金（双向）
    commission_rate: float
    # 单笔最低佣金
    commission_min: float
    # 印花税率（卖出单边计提；0 表示无）
    stamp_duty_rate: float
    # 是否存在涨跌停限制（False 时行情层 limit_up/down 恒为 False）
    has_price_limit: bool

    def compute_commission(self, quantity: float, price: float, side: str) -> float:
        """按市场规则计算单笔费用（佣金 + 印花税）。"""
        gross = abs(float(quantity) * float(price))
        if gross <= 0:
            return 0.0
        fee = max(gross * self.commission_rate, self.commission_min)
        if side.lower() == "sell":
            fee += gross * self.stamp_duty_rate
        return round(fee, 2)


CN_RULES = MarketTradingRules(
    market=Market.CN,
    currency="CNY",
    t_plus_1=True,
    lot_size=100,
    commission_rate=0.0003,
    commission_min=5.0,
    stamp_duty_rate=0.0005,
    has_price_limit=True,
)
HK_RULES = MarketTradingRules(
    market=Market.HK,
    currency="HKD",
    t_plus_1=False,
    lot_size=1,
    commission_rate=0.0003,
    commission_min=3.0,
    stamp_duty_rate=0.001,
    has_price_limit=False,
)
US_RULES = MarketTradingRules(
    market=Market.US,
    currency="USD",
    t_plus_1=False,
    lot_size=1,
    commission_rate=0.0,
    commission_min=0.0,
    stamp_duty_rate=0.0,
    has_price_limit=False,
)
FUTURES_RULES = MarketTradingRules(
    market=Market.FUTURES,
    currency="CNY",
    t_plus_1=False,
    lot_size=1,
    commission_rate=0.0001,
    commission_min=0.0,
    stamp_duty_rate=0.0,
    has_price_limit=False,
)
CRYPTO_RULES = MarketTradingRules(
    market=Market.CRYPTO,
    currency="USDT",
    t_plus_1=False,
    lot_size=1,
    commission_rate=0.001,
    commission_min=0.0,
    stamp_duty_rate=0.0,
    has_price_limit=False,
)

RULES_BY_MARKET: dict[Market, MarketTradingRules] = {
    Market.CN: CN_RULES,
    Market.HK: HK_RULES,
    Market.US: US_RULES,
    Market.FUTURES: FUTURES_RULES,
    Market.CRYPTO: CRYPTO_RULES,
}


def rules_for(market: Market | str | None) -> MarketTradingRules:
    market = normalize_market(market)
    return RULES_BY_MARKET[market]


def normalize_market(market: Market | str | None) -> Market:
    if isinstance(market, Market):
        return market
    text = str(market or "").upper().strip()
    if text in {"", "CN", "A", "A_SHARE", "SSE"}:
        return Market.CN
    try:
        return Market(text)
    except ValueError:
        return Market.CN


_HK_RE = re.compile(r"^\d{1,5}\.HK$", re.IGNORECASE)
_CN_SUFFIX_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$", re.IGNORECASE)
_CN_NUMERIC_RE = re.compile(r"^\d{6}$")
_FUTURES_RE = re.compile(r"\.(CN|FUT)$", re.IGNORECASE)
_CRYPTO_RE = re.compile(r"^[A-Z0-9]+USDT$", re.IGNORECASE)
_US_TICKER_RE = re.compile(r"^[A-Z]{1,6}(\.[A-Z]{1,2})?$", re.IGNORECASE)


def infer_market(symbol: str) -> Market:
    """由标的代码推断所属市场（模拟引擎用信号代码选行情源/规则）。"""
    text = str(symbol or "").strip()
    if not text:
        return Market.CN
    if _HK_RE.fullmatch(text):
        return Market.HK
    if _CN_SUFFIX_RE.fullmatch(text) or _CN_NUMERIC_RE.fullmatch(text):
        return Market.CN
    if _FUTURES_RE.search(text):
        return Market.FUTURES
    # 上金所品种（Au99.99 / AG(T+D)）归期货
    if "(T+D)" in text.upper() or re.fullmatch(r"[A-Z]{2}\d{2}\.\d{2}", text, re.IGNORECASE):
        return Market.FUTURES
    if _CRYPTO_RE.fullmatch(text):
        return Market.CRYPTO
    if _US_TICKER_RE.fullmatch(text):
        return Market.US
    return Market.CN


def infer_market_from_symbols(
    symbols: list[str], *, market_hint: Market | str | None = None
) -> Market:
    """从一批信号标的推断共同市场（同一策略的信号来自同一模型/市场）。

    提供 market_hint（激活策略的 parameters.market）时直接使用——
    港股信号 symbol 为裸数字（DB 契约），无法靠众数推断；其余走逐个推断取众数。
    """
    if market_hint is not None:
        return normalize_market(market_hint)
    if not symbols:
        return Market.CN
    counts: dict[Market, int] = {}
    for sym in symbols:
        mkt = infer_market(sym)
        counts[mkt] = counts.get(mkt, 0) + 1
    return max(counts.items(), key=lambda kv: kv[1])[0]
