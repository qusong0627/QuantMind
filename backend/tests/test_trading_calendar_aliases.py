"""交易日历的交易所惯用名别名回归。

背景：前端 `config/marketConfig.ts` 给各市场配的是惯用名（A 股 SSE / 港股 HKEX / 美股 NYSE），
而 `ALL_MARKETS` 的键是 exchange_calendars 的代码（SSE / XHKG / XNYS）。缺别名时
`get_market('NYSE')` 抛错 → `_get_xcal_calendar` 吞掉异常返回 None → 交易日判断
**静默退化成「只看周末」**，于是美股假日（独立日/感恩节）被当成交易日、港股假日同理。

断言挑的是**已知假日/已知交易日**而非「不抛错」：只判周末的实现同样能让 is_trading_day
返回布尔值，所以必须用真实日历才能区分。
"""

from __future__ import annotations

from datetime import date

import pytest

from backend.shared.trading_calendar import ALL_MARKETS, _get_xcal_calendar, calendar_service, get_market

# (市场入参, 日期, 期望是否交易日, 说明)
CASES = [
    ("NYSE", "2026-07-02", True, "独立日前一天，正常交易日"),
    ("NYSE", "2026-07-03", False, "美国独立日（观察日）"),
    ("NYSE", "2026-11-26", False, "感恩节"),
    ("XNYS", "2026-07-03", False, "规范代码与惯用名结果一致"),
    ("XHKG", "2026-07-01", False, "香港特别行政区成立纪念日"),
]


@pytest.mark.parametrize("alias", ["NYSE", "HKEX", "NASDAQ", "SSE", "SZSE"])
def test_alias_resolves_to_registered_market(alias: str):
    """惯用名必须落到 ALL_MARKETS 里真实存在的键上，否则等于没配"""
    assert get_market(alias).code in ALL_MARKETS


@pytest.mark.parametrize("market", ["NYSE", "HKEX", "XHKG"])
def test_alias_yields_exchange_calendar(market: str):
    """能取到 exchange_calendars 的日历对象 —— 取不到时上层会静默退化为工作日判断"""
    assert _get_xcal_calendar(market) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("market,day,expected,why", CASES)
async def test_is_trading_day_uses_exchange_calendar(market: str, day: str, expected: bool, why: str):
    got = await calendar_service.is_trading_day(
        market=market,
        trade_date=date.fromisoformat(day),
        tenant_id="default",
        user_id="00000001",
    )
    assert got is expected, f"{market} {day} 应为{'交易日' if expected else '休市'}（{why}）"
