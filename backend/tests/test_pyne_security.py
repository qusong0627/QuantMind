"""request.security 数据面：周期解析 / 同标的识别 / 聚合 / 市场支持。

不写 `.ohlcv`（需要 pynecore writer）；聚合与解析都是纯函数，容器内可直接跑。

运行（容器）：python3 -m pytest backend/tests/test_pyne_security.py -q
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.services.agent_arena import pyne_security as ps  # noqa: E402


class TestNormalize:
    def test_daily_aliases(self):
        assert ps.normalize_tf("D") == "1D"
        assert ps.normalize_tf("1D") == "1D"

    def test_week_month(self):
        assert ps.normalize_tf("W") == "1W"
        assert ps.normalize_tf("M") == "1M"
        assert ps.normalize_tf("3M") == "3M"

    def test_minutes(self):
        assert ps.normalize_tf("60") == "60"
        assert ps.normalize_tf("05") == "5"


class TestRequested:
    def test_literals_and_chart_period(self):
        src = """
        request.security(syminfo.tickerid, 'D', close)
        request.security(syminfo.ticker, 'W', close)
        request.security(syminfo.tickerid, timeframe.period, close)
        """
        wanted, unresolved = ps.requested_timeframes(src, "1D")
        assert wanted == {"D": "1D", "W": "1W", "1D": "1D"}
        assert unresolved == []

    def test_input_timeframe_variable(self):
        src = """
        rsi_tf = input.timeframe('60', 'RSI TF')
        request.security(syminfo.tickerid, rsi_tf, close)
        """
        wanted, unresolved = ps.requested_timeframes(src, "1D")
        assert wanted == {"60": "60"}
        assert unresolved == []

    def test_symbol_alias(self):
        src = """
        _ticker = syminfo.tickerid
        request.security(_ticker, 'D', close)
        """
        wanted, unresolved = ps.requested_timeframes(src, "1D")
        assert wanted == {"D": "1D"}
        assert unresolved == []

    def test_function_parameter_alias(self):
        src = """
        def f_securitys(_ticker, _source):
            return request.security(_ticker, 'D', _source)
        x = f_securitys(syminfo.tickerid, close)
        """
        wanted, unresolved = ps.requested_timeframes(src, "1D")
        assert wanted == {"D": "1D"}
        assert unresolved == []

    def test_function_parameter_timeframe_is_unquoted(self):
        src = """
        def f_security(_sym, _res, _src):
            return request.security(_sym, _res, _src)
        a = f_security(syminfo.tickerid, 'D', high)
        b = f_security(syminfo.tickerid, '1D', close)
        """
        wanted, unresolved = ps.requested_timeframes(src, "1D")
        assert wanted == {"D": "1D", "1D": "1D"}
        assert unresolved == []

    def test_dynamic_unresolved(self):
        src = "request.security(syminfo.tickerid, dynamic_tf, close)"
        wanted, unresolved = ps.requested_timeframes(src, "1D")
        assert wanted == {}
        assert unresolved == ["dynamic_tf"]

    def test_other_symbol_unresolved(self):
        src = "request.security('AAPL', 'D', close)"
        wanted, unresolved = ps.requested_timeframes(src, "1D")
        assert wanted == {}
        assert unresolved[0].startswith("symbol=")

    def test_heikin_ashi_wrapper_counts_as_same_symbol(self):
        src = "request.security(ticker.heikinashi(syminfo.tickerid), 'D', close)"
        wanted, unresolved = ps.requested_timeframes(src, "1D")
        assert wanted == {"D": "1D"}
        assert unresolved == []


class TestMarkets:
    def test_daily_all_markets(self):
        assert set(ps.supported_markets({"1D", "1W", "1M"})) == {
            "cn",
            "hk",
            "us",
            "futures",
            "crypto",
        }

    def test_minutes_60_cn_only_plus_crypto(self):
        assert set(ps.supported_markets({"1D", "60"})) == {"cn", "crypto"}

    def test_240_crypto_only(self):
        assert ps.supported_markets({"240"}) == ["crypto"]

    def test_empty_means_all(self):
        assert len(ps.supported_markets(set())) == 5


class TestAggregate:
    def test_weekly_ohlc(self):
        bars = [
            {
                "date": "2024-01-01",
                "open": 10,
                "high": 12,
                "low": 9,
                "close": 11,
                "volume": 100,
            },
            {
                "date": "2024-01-02",
                "open": 11,
                "high": 15,
                "low": 10,
                "close": 14,
                "volume": 200,
            },
            {
                "date": "2024-01-08",
                "open": 14,
                "high": 16,
                "low": 13,
                "close": 15,
                "volume": 300,
            },
        ]
        out = ps.aggregate_calendar(bars, "1W")
        assert len(out) == 2
        assert out[0]["open"] == 10 and out[0]["high"] == 15
        assert out[0]["low"] == 9 and out[0]["close"] == 14
        assert out[0]["volume"] == 300
        assert out[0]["date"] == "2024-01-02"

    def test_monthly(self):
        bars = [
            {
                "date": "2024-01-31",
                "open": 1,
                "high": 2,
                "low": 1,
                "close": 2,
                "volume": 1,
            },
            {
                "date": "2024-02-01",
                "open": 2,
                "high": 3,
                "low": 2,
                "close": 3,
                "volume": 1,
            },
        ]
        out = ps.aggregate_calendar(bars, "1M")
        assert [b["date"][:7] for b in out] == ["2024-01", "2024-02"]

    def test_minute_bucket(self):
        bars = [
            {
                "date": "2024-01-02 09:30:00",
                "open": 1,
                "high": 2,
                "low": 1,
                "close": 1.5,
                "volume": 10,
            },
            {
                "date": "2024-01-02 09:35:00",
                "open": 1.5,
                "high": 3,
                "low": 1,
                "close": 2,
                "volume": 20,
            },
            {
                "date": "2024-01-02 09:45:00",
                "open": 2,
                "high": 4,
                "low": 2,
                "close": 3,
                "volume": 30,
            },
        ]
        out = ps.aggregate_minutes(bars, 15)
        assert len(out) == 2
        assert out[0]["open"] == 1 and out[0]["high"] == 3
        assert out[0]["volume"] == 30
