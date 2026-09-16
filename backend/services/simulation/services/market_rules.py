"""模拟盘多市场交易规则。

每个市场的交易规则差异集中在这里表达：
- 回转交易：CN T+1（当日买入锁到次日），其余 T+0
- 最小交易单位：CN 主板/创业板/北交所 100 股，科创板（688/689）200 股；
  HK 按每手股数（board lot，缺省 1 表示按标的元数据，未接入时退化为 1 股）；
  US/期货/加密 1
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

import os
import re
from dataclasses import dataclass
from enum import Enum

from backend.shared.stock_utils import StockCodeUtil


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
    # 最小买入单位（股/张/枚）。CN 市场默认 100，科创板见 lot_size_for_symbol；
    # 其余市场 1。
    lot_size: int
    # 比例佣金（双向）
    commission_rate: float
    # 单笔最低佣金
    commission_min: float
    # 印花税率（卖出单边计提；0 表示无）
    stamp_duty_rate: float
    # 过户费率（双向；A股 0.001%，其余 0）——T-P2-02 费用单实现补齐分项
    transfer_fee_rate: float = 0.0
    # 是否存在涨跌停限制（False 时行情层 limit_up/down 恒为 False）
    has_price_limit: bool = True

    def compute_fee_breakdown(
        self,
        quantity: float,
        price: float,
        side: str,
        *,
        commission_rate: float | None = None,
        commission_min: float | None = None,
        stamp_duty_rate: float | None = None,
        transfer_fee_rate: float | None = None,
    ) -> tuple[float, float, float]:
        """**费用分项唯一实现**（T-P2-02）：(佣金, 印花税, 过户费)，各项 round(2)。

        默认值来自本市场规则；显式入参仅作覆盖（env/前端 settings 的可配置语义）。
        """
        gross = abs(float(quantity) * float(price))
        if gross <= 0:
            return 0.0, 0.0, 0.0
        rate = self.commission_rate if commission_rate is None else float(commission_rate)
        min_fee = self.commission_min if commission_min is None else float(commission_min)
        stamp_rate = (
            self.stamp_duty_rate if stamp_duty_rate is None else float(stamp_duty_rate)
        )
        transfer_rate = (
            self.transfer_fee_rate if transfer_fee_rate is None else float(transfer_fee_rate)
        )
        commission = round(max(gross * rate, min_fee), 2)
        stamp = round(gross * stamp_rate, 2) if str(side).lower() == "sell" else 0.0
        transfer = round(gross * transfer_rate, 2)
        return commission, stamp, transfer

    def compute_commission(self, quantity: float, price: float, side: str) -> float:
        """按市场规则计算单笔费用合计（佣金 + 印花税 + 过户费；向后兼容）。"""
        commission, stamp, transfer = self.compute_fee_breakdown(quantity, price, side)
        return round(commission + stamp + transfer, 2)


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


def _pure_code(symbol: str) -> str:
    pure = StockCodeUtil.to_suffix(symbol) or symbol
    pure = str(pure).upper()
    for prefix in ("SH", "SZ", "BJ"):
        if pure.startswith(prefix):
            pure = pure[len(prefix) :]
            break
    return pure.split(".")[0]


def is_star_market(symbol: str) -> bool:
    """科创板（688/689）：申报数量语义与主板不同（200 股起、1 股递增）。"""
    return _pure_code(symbol).startswith(("688", "689"))


def normalize_order_quantity(
    quantity: float, symbol: str, market: Market | str | None = None
) -> int:
    """**申报数量归一唯一实现**（T-P2-02）。

    规则（含 2026-07-06 交易新规口径）：
    - 科创板（688/689）：单笔申报 ≥200 股，超过部分**以 1 股为单位递增**（201 股合法）；
    - 其余 CN（主板/创业板/北交所）：按 100（或市场配置）整数倍**向下取整**；
    - 非 CN：原样取整。
    返回 0 表示低于最小申报数量，调用方应拒单。
    """
    qty = int(float(quantity or 0))
    if qty <= 0:
        return 0
    mkt = market if isinstance(market, Market) else normalize_market(market)
    if mkt != Market.CN:
        return qty
    if is_star_market(symbol):
        min_qty = max(200, int(lot_size_for_symbol(symbol, Market.CN)))
        return qty if qty >= min_qty else 0
    lot = max(1, int(lot_size_for_symbol(symbol, Market.CN)))
    return (qty // lot) * lot


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

# ── 同一套判据的 SQL 版（Postgres `~*` 大小写不敏感正则）────────────────────
# 存在意义：sim_trades / trades 等表没有 market 列，市场只隐含在 symbol 形态里，
# 按市场过滤时必须在 SQL 侧用与 infer_market 完全一致的判据，
# 否则「列表过滤」与「引擎推断」会给出两套口径（历史教训：SHOP/SHW 被当成上交所）。
# 港股只认 `.HK` 后缀式：模拟盘落库前经 signal_loader._to_market_symbol 归一为 0001.HK，
# 裸 4-5 位数字在 infer_market 里会落到 CN 兜底，SQL 侧同样不接（两边必须一致）。
_SQL_PATTERNS: dict[Market, str] = {
    Market.HK: r"\d{1,5}\.HK",
    Market.CN: r"(\d{6}\.(SH|SZ|BJ)|(SH|SZ|BJ)\d{6}|\d{6})",
    Market.FUTURES: r"(.*\.(CN|FUT)|.*\(T\+D\)|[A-Z]{2}\d{2}\.\d{2})",
    Market.CRYPTO: r"[A-Z0-9]+USDT",
    Market.US: r"[A-Z]{1,6}(\.[A-Z]{1,2})?",
}


def market_symbol_sql_regex(market: Market | str | None) -> str | None:
    """市场 → symbol 形态 SQL 正则（供 `symbol ~* :pattern` 使用）。

    **不传市场 / 不认识的市场一律返回 None（调用方按「不过滤」处理）**——
    不能像 normalize_market 那样把空值兜底成 CN，否则「不传 market」会静默变成
    「只看 A 股」，历史调用方（不带市场参数的分页列表）会突然少数据。
    """
    if isinstance(market, Market):
        key: Market | None = market
    else:
        text = str(market or "").upper().strip()
        if not text:
            key = None
        elif text in {"A", "A_SHARE", "SSE"}:
            key = Market.CN
        else:
            try:
                key = Market(text)
            except ValueError:
                key = None
    pattern = _SQL_PATTERNS.get(key) if key else None
    if not pattern:
        return None
    # 全匹配锚定：避免子串误伤（US 的 AAPL 不该匹配到 "XXAAPLXX"）
    return f"^({pattern})$"


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


def _cn_numeric_code(symbol: str) -> str:
    """取出 A 股 6 位数字代码（兼容 SH688001 / 688001.SH / 688001）。"""
    suffix = StockCodeUtil.to_suffix(str(symbol or "").strip())
    code = suffix.split(".", 1)[0] if suffix else ""
    if len(code) == 6 and code.isdigit():
        return code
    raw = str(symbol or "").upper().strip()
    for pfx in ("SH", "SZ", "BJ"):
        if raw.startswith(pfx):
            raw = raw[len(pfx) :]
            break
    raw = raw.split(".", 1)[0]
    return raw if len(raw) == 6 and raw.isdigit() else ""


def lot_size_for_symbol(symbol: str, market: Market | str | None = None) -> int:
    """按标的返回买入整手。科创板 688/689 为 200，其余 A 股 100。"""
    inferred = infer_market(symbol) if market is None else normalize_market(market)
    if inferred is not Market.CN:
        return max(1, int(RULES_BY_MARKET[inferred].lot_size))

    code = _cn_numeric_code(symbol)
    if code.startswith(("688", "689")):
        return max(1, int(os.getenv("MIN_LOT_STAR_BOARD", "200")))
    return max(1, int(os.getenv("MIN_LOT_MAIN_BOARD", "100")))


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
