"""证券代码标准化和委托数量处理。"""

import re


_DIGIT_CODE_RE = re.compile(r"^\d{6}$")

# Tokens that name a whole exchange rather than one instrument.
#
# Stock markets are what get_markets() reports, so they stay their own set.
# Futures exchanges are listed separately because a futures exchange carries
# only futures: the token already says what kind of instrument it holds, so
# nothing has to be narrowed or asked of the caller there (issues #95, #104).
STOCK_MARKET_CODES = frozenset({"SH", "SZ", "BJ", "HK"})
FUTURES_MARKET_CODES = frozenset({"IF", "SF", "DF", "ZF", "INE", "GF"})
EXCHANGE_TOKENS = STOCK_MARKET_CODES | FUTURES_MARKET_CODES


# QMT ContextInfo uses market-specific suffixes for all instrument types.
_QMT_SUFFIXES = (
    ".SH", ".SZ", ".BJ", ".HK",            # Stock markets
    ".HGT", ".SGT",                        # 港股通（沪/深）
    ".SHO", ".SZO",                        # Options markets (8-digit codes)
    ".SF", ".DF", ".IF", ".ZF", ".INE", ".GF",    # Futures markets
)
# Futures symbols are case-sensitive: each exchange has its own convention, and
# the two are not interchangeable -- "rb2401.SF" (SHFE, lower) is a different
# string to QMT than "RB2401.SF", while CZCE writes "AP401.ZF" upper. Uppercasing
# a lowercase futures symbol therefore produces a code QMT does not recognise,
# which surfaces as an empty quote rather than an error (issue #95).
#
# #68 fixed this for POSITION rows by bypassing this function; every other path
# -- orders, quotes, the full-tick cache, the risk guard -- still came through
# here and got uppercased.
_CASE_SENSITIVE_SUFFIXES = frozenset({".SF", ".DF", ".IF", ".ZF", ".INE", ".GF"})


# 可转债 / 可交换债的代码段。两件事都要靠它：
#   1. 裸 6 位码推断市场 —— 沪市转债 110/111/113 是 "1" 开头，落不进 ("5","6")
#      那条规则，会被判到深市；
#   2. 最小申报量 —— 债券论「张」，10 张起，不是 100。
#
#   SH  110/111/113 可转债、118 科创转债、132 可交换债
#   SZ  12x 可转债与可交换债
_BOND_SH_PREFIXES = ("110", "111", "113", "118", "132")
_BOND_SZ_PREFIXES = ("12",)
BOND_LOT = 10          # 债券最小申报数量（张）
DEFAULT_LOT = 100      # 股票/基金最小申报数量（股/份）
STAR_LOT = 200         # 科创板（688/689）


def is_bond_code(stock_code):
    """是不是可转债/可交换债。只看代码段，不需要联网。"""
    text = str(stock_code or "").strip().upper()
    if not text:
        return False
    if "." in text:
        pure, _, suffix = text.partition(".")
    elif text[:2] in ("SH", "SZ") and text[2:].isdigit():
        pure, suffix = text[2:], text[:2]
    else:
        pure, suffix = text, ""
    if not pure.isdigit():
        return False
    if suffix == "SH":
        return pure.startswith(_BOND_SH_PREFIXES)
    if suffix == "SZ":
        return pure.startswith(_BOND_SZ_PREFIXES)
    if suffix:
        return False
    # 裸码：两个市场的债券段互不重叠，直接一起判
    return pure.startswith(_BOND_SH_PREFIXES) or pure.startswith(_BOND_SZ_PREFIXES)


def normalize_stock_code(code):
    raw = str(code or "").strip()
    if not raw:
        return ""
    text = raw.upper()
    # If the code already has a recognized suffix, pass it through unchanged —
    # these are native ContextInfo codes that need no normalization.
    for suffix in _QMT_SUFFIXES:
        if text.endswith(suffix):
            prefix = text[:-len(suffix)]
            if prefix and (prefix.isdigit() or prefix[0].isalpha()):
                if suffix in _CASE_SENSITIVE_SUFFIXES:
                    # Normalise the suffix, keep the caller's symbol verbatim.
                    return raw[:-len(suffix)] + suffix
                return text
    # Original logic for 6-digit stock codes
    if text.startswith("SH") and _DIGIT_CODE_RE.match(text[2:]):
        return f"{text[2:]}.SH"
    if text.startswith("SZ") and _DIGIT_CODE_RE.match(text[2:]):
        return f"{text[2:]}.SZ"
    if text.endswith(".SH") or text.endswith(".SZ"):
        prefix = text[:6]
        if _DIGIT_CODE_RE.match(prefix):
            return text
    if _DIGIT_CODE_RE.match(text):
        # 沪市转债 110/111/113/118/132 是 "1" 开头，不加这一条会被判到深市
        if text.startswith(_BOND_SH_PREFIXES) or text.startswith(("5", "6")):
            market = "SH"
        else:
            market = "SZ"
        return f"{text}.{market}"
    raise ValueError(f"invalid stock code: {code}")


def min_lot(stock_code):
    """最小申报数量。

    债券论「张」，10 张起 —— 之前一律按 100 算，于是 round_buy_volume 会把
    一手转债 (10 张) 变成 `(10 // 100) * 100 == 0`，单子直接废掉。
    """
    normalized = normalize_stock_code(stock_code)
    if is_bond_code(normalized):
        return BOND_LOT
    pure = normalized.split(".")[0]
    return STAR_LOT if pure.startswith("688") else DEFAULT_LOT


def round_buy_volume(stock_code, amount):
    lot = min_lot(stock_code)
    value = int(amount or 0)
    if value <= 0:
        return 0
    return (value // lot) * lot


def round_sell_volume(stock_code, amount, sell_all=False):
    value = int(amount or 0)
    if value <= 0:
        return 0
    if sell_all:
        return value
    lot = min_lot(stock_code)
    return (value // lot) * lot
