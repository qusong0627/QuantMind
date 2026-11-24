"""标的代码 → 业务市场统一推断（CN / HK / US / FUTURES / CRYPTO / CUSTOM）。

与模拟引擎 ``market_rules.infer_market`` 同一套号段口径，但保持轻量：
trade / simulation / api 各层按市场过滤数据时都从这里取，避免各处手写
``.HK`` / ``.SH`` 判断出现漂移（例如把美股 ``BRK.B`` 当成 A 股）。

两种用法：
- Python 侧：``market_of_symbol(symbol)``
- SQL 侧：``symbol_market_clause(SimTrade.symbol, "HK")``
"""

from __future__ import annotations

import re

APP_MARKETS = ("CN", "HK", "US", "FUTURES", "CRYPTO", "CUSTOM")

# 存储口径为后缀式（AGENTS：600036.SH / 00700.HK / BTCUSDT）
_HK_RE = re.compile(r"^\d{1,5}\.HK$", re.IGNORECASE)
_CN_SUFFIX_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$", re.IGNORECASE)
_CN_NUMERIC_RE = re.compile(r"^\d{6}$")
_FUTURES_RE = re.compile(r"\.(CN|FUT)$", re.IGNORECASE)
_CRYPTO_RE = re.compile(r"^[A-Z0-9]+USDT$", re.IGNORECASE)
_US_TICKER_RE = re.compile(r"^[A-Z]{1,6}(\.[A-Z]{1,2})?$", re.IGNORECASE)


def normalize_market(value: object) -> str:
    """归一业务市场；A/A_SHARE/空 → CN，未知值原样大写（不合法时调用方兜底）。"""
    market = str(value or "CN").strip().upper()
    if market in {"", "A", "A_SHARE"}:
        return "CN"
    return market


def market_of_symbol(symbol: object) -> str:
    """由标的推断业务市场。空值/无法识别按 CN（与模拟引擎一致）。"""
    text = str(symbol or "").strip()
    if not text:
        return "CN"
    if _HK_RE.fullmatch(text):
        return "HK"
    if _CN_SUFFIX_RE.fullmatch(text) or _CN_NUMERIC_RE.fullmatch(text):
        return "CN"
    if _FUTURES_RE.search(text):
        return "FUTURES"
    if "(T+D)" in text.upper() or re.fullmatch(
        r"[A-Z]{2}\d{2}\.\d{2}", text, re.IGNORECASE
    ):
        return "FUTURES"
    if _CRYPTO_RE.fullmatch(text):
        return "CRYPTO"
    if _US_TICKER_RE.fullmatch(text):
        return "US"
    return "CN"


def symbol_market_clause(column, market: object):
    """构造 SQLAlchemy 过滤条件：``column``（symbol 列）属于指定市场。

    按存储口径的代码后缀判断，不依赖各表额外的市场列。
    """
    from sqlalchemy import false, func, not_, or_

    col = func.upper(column)
    cn = or_(
        col.like("%.SH"),
        col.like("%.SZ"),
        col.like("%.BJ"),
        _six_digit_clause(col),
    )
    hk = col.like("%.HK")
    futures = or_(col.like("%.CN"), col.like("%.FUT"))
    crypto = col.like("%USDT")

    normalized = normalize_market(market)
    if normalized == "CN":
        return cn
    if normalized == "HK":
        return hk
    if normalized == "FUTURES":
        return futures
    if normalized == "CRYPTO":
        return crypto
    if normalized == "US":
        return not_(or_(cn, hk, futures, crypto))
    # CUSTOM 没有公开行情/成交，按无数据处理，避免误把其它市场算进来
    return false()


def _six_digit_clause(col):
    """兼容历史裸 6 位 A 股代码（新写入必须后缀式）。"""
    from sqlalchemy import and_, func, not_

    return and_(func.length(col) == 6, not_(col.like("%.%")), col.op("~")("^[0-9]+$"))
