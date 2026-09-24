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
from datetime import time as _time_cls
from enum import Enum

from backend.shared.stock_utils import StockCodeUtil

# T-P2-07（2026-07-06 新规）：盘后固定价格交易扩至全部 A 股与 ETF——
# 15:05–15:30 按当日收盘价、时间优先撮合；买价<收盘/卖价>收盘为无效申报。
AFTER_HOURS_FIXED_START = _time_cls(15, 5)
AFTER_HOURS_FIXED_END = _time_cls(15, 30)

SESSION_CONTINUOUS = "continuous"
SESSION_AFTER_HOURS_FIXED = "after_hours_fixed"


def is_after_hours_fixed_session(ts) -> bool:
    """是否处于盘后固定价格交易时段（纯函数；ts 接受 datetime/time）。

    T-P2-07：这是盘后时段判定的**唯一谓词**（风控 L0 时段校验 / 撮合会话推导 /
    调度会话门三处共用），15:05:00–15:30:00 含边界。
    """
    try:
        t = ts.time() if hasattr(ts, "time") and not isinstance(ts, _time_cls) else ts
    except Exception:  # noqa: BLE001
        return False
    return AFTER_HOURS_FIXED_START <= t <= AFTER_HOURS_FIXED_END


def session_for_time(ts) -> str:
    """由时刻解析撮合会话（会话解析唯一入口）。

    15:05–15:30 → after_hours_fixed（收盘价成交）；其余 → continuous。
    调用方负责传入正确时区（上海）的时刻；replay/回测不按墙钟调用本函数。
    """
    return (
        SESSION_AFTER_HOURS_FIXED
        if is_after_hours_fixed_session(ts)
        else SESSION_CONTINUOUS
    )


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


def side_text(side: object) -> str:
    """买卖方向归一为 ``"buy"`` / ``"sell"``（吃字符串也吃枚举）。

    ``str(OrderSide.SELL)`` 是 ``"OrderSide.SELL"`` **不是** ``"sell"``（``str``
    混入的 Enum 在 3.10 仍用 ``Enum.__str__``）。按 ``str(side).lower()`` 判卖方，
    传枚举时印花税**静默归零**——实测 ``compute_fee_breakdown(1000, 10, OrderSide.SELL)``
    得 ``(5.0, 0.0, 0.1)``，而传 ``"sell"`` 得 ``(5.0, 5.0, 0.1)``：一笔卖出少收
    万 5。本仓有**三个** OrderSide 枚举（simulation / trade_shared / backtest_engine），
    故按 ``.value`` 解包（与 ``normalize_market`` 同一手法），不按类型嗅探。
    """
    raw = getattr(side, "value", side)
    return str(raw or "").strip().lower()


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
    # **真单/评估侧**的券商佣金假设；None = 与 commission_rate 同。
    # 与 commission_rate 分开是因为两者语义不同：commission_rate 是**撮合/回测的
    # 计划口径**（CN 万3，刻意取保守值），本字段是**券商实收的估计**（CN 万2.5，
    # 与 CnExchange / trading_cost.CostModel / 前端默认同值）。真单成交只发生一次，
    # 记进去的费用必须按后者估——见 compute_real_order_fee。平价网与来源见
    # test_rule_parity.test_fee_parity_real_order_uses_the_broker_assumption。
    broker_commission_rate: float | None = None

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
        stamp = (
            round(gross * stamp_rate, 2) if side_text(side) == "sell" else 0.0
        )
        transfer = round(gross * transfer_rate, 2)
        return commission, stamp, transfer

    def compute_commission(self, quantity: float, price: float, side: str) -> float:
        """按市场规则计算单笔费用合计（佣金 + 印花税 + 过户费；向后兼容）。"""
        commission, stamp, transfer = self.compute_fee_breakdown(quantity, price, side)
        return round(commission + stamp + transfer, 2)

    @property
    def effective_broker_commission_rate(self) -> float:
        """真单/评估口径的佣金率（未单独配置的市场与撮合默认同值）。"""
        if self.broker_commission_rate is None:
            return self.commission_rate
        return float(self.broker_commission_rate)

    def compute_real_order_breakdown(
        self, quantity: float, price: float, side: str
    ) -> tuple[float, float, float]:
        """**真单/评估口径**的费用分项：(佣金, 印花税, 过户费)。

        与 ``compute_fee_breakdown`` 同实现、只差佣金率——真单记的是**券商实收的
        估计**，撮合/回测的计划费率（CN 万3）比它保守，用它记会系统性高估真单成本
        （每 10 万成交差 5 元，且只有真单那一侧错）。法定费率（印花税/过户费）两侧
        相同，不参与这处差异。
        """
        return self.compute_fee_breakdown(
            quantity, price, side, commission_rate=self.effective_broker_commission_rate
        )

    def compute_real_order_fee(self, quantity: float, price: float, side: str) -> float:
        """真单/评估口径的单笔费用合计（佣金 + 印花税 + 过户费）。"""
        commission, stamp, transfer = self.compute_real_order_breakdown(
            quantity, price, side
        )
        return round(commission + stamp + transfer, 2)


# A 股**券商实收**佣金假设（万2.5）。与撮合默认 ``commission_rate``（万3）**刻意不同**：
# 万3 是计划口径的保守值（回测/模拟盘宁可多算成本），万2.5 是券商成本假设
# （``CnExchange`` 主回测引擎 / ``inference.trading_cost.CostModel`` / 前端
# ``config/backtest.ts`` 的「默认券商佣金」三处同值，见 ``docs/回测费用配置说明.md``
# 的「真实A股费用结构」）。**同值不代表同源**，故这里立一个具名常量并由
# ``test_rule_parity.test_fee_parity_real_order_uses_the_broker_assumption``
# 把三方钉住——改任一处而不改其余会让该用例转红，而不是让真单成本静默漂移。
CN_BROKER_COMMISSION_RATE: float = 0.00025


CN_RULES = MarketTradingRules(
    market=Market.CN,
    currency="CNY",
    t_plus_1=True,
    lot_size=100,
    commission_rate=0.0003,
    commission_min=5.0,
    stamp_duty_rate=0.0005,
    # 过户费（沪深双向 0.001%）——T-P2-02 收敛时漏配致全链路静默归零，
    # 回归由 test_ashare_matcher 抓出（2026-09-16 T-P2-07 批次修复）
    transfer_fee_rate=0.00001,
    has_price_limit=True,
    broker_commission_rate=CN_BROKER_COMMISSION_RATE,
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


# ── 单笔申报数量上限（2026-07-06 新规口径；官方规则原文核实 2026-09-16）──
# 主板/创业板：限价 ≤30 万股、市价 ≤15 万股；科创板：限价 ≤10 万股、市价 ≤5 万股；
# 盘后固定价格（全 A 股/ETF）：≤100 万股。非 CN 不设上限。
_CAP_CONTINUOUS_LIMIT = 300_000
_CAP_CONTINUOUS_MARKET = 150_000
_CAP_STAR_LIMIT = 100_000
_CAP_STAR_MARKET = 50_000
_CAP_AFTER_HOURS = 1_000_000


def order_quantity_cap(
    symbol: str,
    *,
    order_type: str | None = None,
    session: str | None = None,
    market: Market | str | None = None,
) -> int | None:
    """单笔申报数量上限（唯一实现）；非 CN 返回 None（不设限）。

    - 盘后固定价格会话：统一 100 万股（不分板块/类型）；
    - 科创板（688/689）：限价 10 万 / 市价 5 万；
    - 主板/创业板：限价 30 万 / 市价 15 万；
    - order_type 缺失（校验点拿不到类型）→ 取该板块**最宽松档（限价档）**，
      宁可不误拒（类型在别的边界另行收敛）。
    """
    if isinstance(market, Market):
        mkt = market
    elif market is None:
        mkt = infer_market(symbol)  # 未传市场 → 按标的推断（HK/US 不设限）
    else:
        mkt = normalize_market(market)
    if mkt != Market.CN:
        return None
    if str(session or "").lower() == SESSION_AFTER_HOURS_FIXED:
        return _CAP_AFTER_HOURS
    ot = str(order_type or "").lower()
    if is_star_market(symbol):
        return _CAP_STAR_MARKET if ot == "market" else _CAP_STAR_LIMIT
    return _CAP_CONTINUOUS_MARKET if ot == "market" else _CAP_CONTINUOUS_LIMIT


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
