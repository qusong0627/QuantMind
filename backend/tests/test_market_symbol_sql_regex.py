"""市场 → symbol 形态过滤：SQL 判据与 Python 判据必须一致（无 DB 依赖）。

背景：`sim_trades` / `sim_orders` 等表没有 market 列，按市场过滤只能看 symbol 形态。
后端有**两套**判据：
- Python：`market_rules.infer_market(symbol)`（引擎撮合、信号路由在用）
- SQL：`market_rules.market_symbol_sql_regex(market)`（列表/统计接口过滤在用）

两套判据一旦漂移，就会出现「列表过滤说这是港股、引擎说这是 A 股」这类静默错配。
本测试用同一批样例把两者钉在一起；改动任一侧判据时这里会先红。

同时覆盖两条**向后兼容**约定：
- 不传市场 / 不认识的市场 → 返回 None（调用方不过滤），不能像 normalize_market 那样兜底成 CN，
  否则「不带 market 参数」会静默变成「只看 A 股」，历史调用方会突然少数据。
"""

import re

import pytest

from backend.services.simulation.services.market_rules import (
    Market,
    infer_market,
    market_symbol_sql_regex,
)

# (symbol, 期望市场) —— 与前端 utils/marketInfer 的样例保持同一组
SAMPLES: list[tuple[str, Market]] = [
    ("600036.SH", Market.CN),
    ("000001.SZ", Market.CN),
    ("BJ430047", Market.CN),
    ("600036", Market.CN),
    ("00700.HK", Market.HK),
    ("0700.HK", Market.HK),
    ("AAPL", Market.US),
    ("BRK.B", Market.US),
    ("SHOP", Market.US),
    ("RB0.CN", Market.FUTURES),
    ("RB2601.CN", Market.FUTURES),
    ("CL.FUT", Market.FUTURES),
    ("Au99.99", Market.FUTURES),
    ("AG(T+D)", Market.FUTURES),
    ("BTCUSDT", Market.CRYPTO),
    ("ETHUSDT", Market.CRYPTO),
]


@pytest.mark.parametrize("symbol,expected", SAMPLES)
def test_infer_market_样例(symbol, expected):
    assert infer_market(symbol) == expected


@pytest.mark.parametrize("symbol,expected", SAMPLES)
def test_sql_判据与_python_判据一致(symbol, expected):
    """每个市场自己的 SQL 正则必须只匹配属于它的样例，且不漏判。"""
    for market in Market:
        pattern = market_symbol_sql_regex(market)
        assert pattern, f"{market} 缺少 SQL 判据"
        matched = bool(re.fullmatch(pattern, symbol, re.IGNORECASE))
        assert matched == (market == expected), (
            f"{symbol} 在 {market} 判据下 matched={matched}，期望 {'匹配' if market == expected else '不匹配'}"
        )


@pytest.mark.parametrize("value", [None, "", "   ", "XX", "unknown"])
def test_未指定或不认识的市场不过滤(value):
    """None / 空串 / 未知市场码 → 返回 None（调用方按不过滤处理，保持历史行为）。"""
    assert market_symbol_sql_regex(value) is None


@pytest.mark.parametrize(
    "raw,expected",
    [("A", Market.CN), ("a_share", Market.CN), ("cn", Market.CN), (Market.HK, Market.HK)],
)
def test_别名与枚举入参(raw, expected):
    assert market_symbol_sql_regex(raw) == market_symbol_sql_regex(expected)


def test_裸数字不属于任何SQL判据_由落库归一保证():
    """裸 4-5 位数字（如 0700）不是受支持的落库形态。

    港股在 signal_loader._to_market_symbol 里归一为 0700.HK，A 股是 6 位数字，
    所以 SQL 判据对裸数字一律不匹配；Python 的 infer_market 对它走 CN 兜底。
    这条差异是有意的（兜底 ≠ 判定），写成测试钉住，避免以后误改判据。
    """
    for market in Market:
        assert not re.fullmatch(market_symbol_sql_regex(market), "0700", re.IGNORECASE)
    assert infer_market("0700") == Market.CN


def test_判据互斥_没有符号同时属于两个市场():
    """同一 symbol 不能被两个市场同时判中（否则过滤会重复计数）。"""
    for symbol, _ in SAMPLES:
        hits = [
            market
            for market in Market
            if re.fullmatch(market_symbol_sql_regex(market), symbol, re.IGNORECASE)
        ]
        assert len(hits) == 1, f"{symbol} 被判中 {hits}"
