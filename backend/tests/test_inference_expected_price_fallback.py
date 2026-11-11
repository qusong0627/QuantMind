"""盘前推理 ``expected_price`` 兜底：实时快照缺失时回落到上一交易日收盘价。

背景（2026-11-11 定位）：自动推理固定在交易日 08:00 CST，而 TDX 热集行情快照
09:26 才开始写入、``SNAPSHOT_TTL=300s``。所以盘前批次取价必然为 0——
**键形与 db 都已正确，缺的是「那一刻没有快照」**。铁证：2026-11-06 盘中
10:14–10:18 的五次推理 ``expected_price`` 命中 633/633、619/619、620/620
（100%），而所有盘前批次全 NULL。

本组用例钉住四条不变量：

1. 有实时价 → 用实时价，来源 ``live``；
2. 无实时价 → 用 QuantDB **上一交易日**收盘价，来源 ``prev_close``；
3. 两者都取不到 → 该标的不进表（调用方写 NULL，绝不伪造 0）；
4. 兜底日期**严格早于**预测日——盘后补跑时不得把当日收盘冒充「昨收」。

全部使用假行情对象与假 QuantDB，不连 Redis、不读磁盘。
"""

from __future__ import annotations

from datetime import date

import pytest

from backend.services.engine.inference.script_runner import InferenceScriptRunner
from backend.shared.signal_contract import CONTRACT_COLUMNS


class _Bar:
    def __init__(self, close: float) -> None:
        self.close = close


class _FakeMarketData:
    """最小 QuantDB 日线替身：``latest_trade_date`` + ``get_bar``。"""

    def __init__(self, bars: dict[tuple[str, date], float], latest: date | None) -> None:
        self._bars = bars
        self._latest = latest
        self.calls: list[date | None] = []

    def latest_trade_date(self, on_or_before: date | None = None) -> date | None:
        self.calls.append(on_or_before)
        if self._latest is None:
            return None
        if on_or_before is not None and self._latest > on_or_before:
            return None
        return self._latest

    def get_bar(self, symbol: str, trade_date: date):
        close = self._bars.get((symbol, trade_date))
        return _Bar(close) if close is not None else None


@pytest.mark.unit
def test_live_price_wins_over_prev_close():
    """实时价存在时不使用兜底，来源标 live。"""
    prices, sources = InferenceScriptRunner._resolve_expected_prices(
        {"600036": 42.77},
        {"600036": 41.00},
    )

    assert prices == {"600036": 42.77}
    assert sources == {"600036": "live"}


@pytest.mark.unit
def test_missing_live_falls_back_to_prev_close():
    """实时价缺失的标的用昨收兜底，来源标 prev_close。"""
    prices, sources = InferenceScriptRunner._resolve_expected_prices(
        {"600036": 42.77},
        {"600036": 41.00, "000001": 11.66},
    )

    assert prices == {"600036": 42.77, "000001": 11.66}
    assert sources == {"600036": "live", "000001": "prev_close"}


@pytest.mark.unit
def test_symbol_without_any_price_is_absent_not_zero():
    """两套来源都没有 → 不进表（调用方写 NULL），不得伪造 0.0。"""
    prices, sources = InferenceScriptRunner._resolve_expected_prices({}, {})

    assert prices == {}
    assert sources == {}


@pytest.mark.unit
def test_prev_close_skipped_when_no_trade_date_before_prediction_day():
    """盘后补跑：库里最新交易日 == 预测日时，不得把当日收盘当「昨收」。"""
    prediction_day = date(2026, 11, 11)
    market_data = _FakeMarketData({}, latest=prediction_day)

    out = InferenceScriptRunner._fetch_prev_close_prices(
        ["600036"], prediction_day, market_data=market_data
    )

    assert out == {}
    # 查询上界必须是预测日的前一天
    assert market_data.calls == [date(2026, 11, 10)]


@pytest.mark.unit
def test_prev_close_reads_last_available_trade_date():
    """正常盘前：取上一交易日收盘价，多种符号形态都要能落到 suffix。"""
    prev_day = date(2026, 11, 10)
    market_data = _FakeMarketData(
        {("600036.SH", prev_day): 41.00, ("000001.SZ", prev_day): 11.66},
        latest=prev_day,
    )

    out = InferenceScriptRunner._fetch_prev_close_prices(
        ["600036", "000001", "300750"],
        date(2026, 11, 11),
        market_data=market_data,
    )

    assert out == {"600036": 41.0, "000001": 11.66}


@pytest.mark.unit
def test_prev_close_drops_nonpositive_close():
    """0 价 / 负价不进表——诚实缺失，不是 0。"""
    prev_day = date(2026, 11, 10)
    market_data = _FakeMarketData(
        {("600036.SH", prev_day): 0.0, ("000001.SZ", prev_day): -1.0},
        latest=prev_day,
    )

    out = InferenceScriptRunner._fetch_prev_close_prices(
        ["600036", "000001"], date(2026, 11, 11), market_data=market_data
    )

    assert out == {}


@pytest.mark.unit
def test_signal_contract_carries_price_source_column():
    """来源列必须进契约自愈集合，否则老库写入会整条失败。"""
    names = {name for name, _ in CONTRACT_COLUMNS}

    assert "expected_price_source" in names
