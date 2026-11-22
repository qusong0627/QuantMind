"""Pyne decorator 覆盖：口径、字符串安全、原文件不变。

运行（容器）：python3 -m pytest backend/tests/test_pyne_overrides.py -q
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.services.agent_arena import pyne_overrides as po  # noqa: E402

SINGLE = """@script.strategy('MACD ReLoaded', 'MACDReSt', overlay=True, margin_long=99,
                 initial_capital=1000000, default_qty_type=strategy.fixed,
                 default_qty_value=1, close_by_id=True)
def main():
    pass
"""

MULTI = """@script.strategy(
    title='v.5 - 3 EMA + RSI with Trail Stop',
    shorttitle='v.5',
    overlay=True,
    pyramiding=0,
    currency=currency.USD,
    default_qty_type=strategy.percent_of_equity,
    initial_capital=1000,
    default_qty_value=20,
    commission_type=strategy.commission.percent,
    commission_value=0.05,
    margin_long=0,
    close_by_id=True,
)
def main():
    pass
"""

STRINGY = """@script.strategy('Title, with comma (and paren)', overlay=True,
                 commission_value=0.1, initial_capital=1000)
def main():
    pass
"""


class TestPreset:
    def test_cn_uses_platform_template(self):
        p = po.preset_for_market("cn")
        assert p["initial_capital"] == "100000"
        assert p["default_qty_type"] == "strategy.percent_of_equity"
        assert p["default_qty_value"] == "95"
        assert p["commission_type"] == "strategy.commission.percent"
        assert p["commission_value"] == "0.05"
        assert p["slippage"] == "1"
        assert p["currency"] == "currency.CNY"

    def test_hk_carries_stamp_duty(self):
        p = po.preset_for_market("hk")
        assert p["commission_value"] == "0.15"
        assert p["currency"] == "currency.HKD"

    def test_us_currency(self):
        p = po.preset_for_market("us")
        assert p["commission_value"] == "0.05"
        assert p["currency"] == "currency.USD"

    def test_returns_fresh_dict(self):
        a = po.preset_for_market("cn")
        a["commission_value"] = "9"
        assert po.preset_for_market("cn")["commission_value"] == "0.05"


class TestApplySettings:
    def test_replaces_existing_kwargs(self):
        out = po.apply_strategy_settings(SINGLE, po.preset_for_market("cn"))
        assert "initial_capital=100000," in out
        assert "default_qty_type=strategy.percent_of_equity" in out
        assert "default_qty_value=95" in out
        assert "margin_long=0" in out
        # 非口径参数原样保留
        assert "close_by_id=True" in out
        assert "'MACD ReLoaded'" in out
        assert "'MACDReSt'" in out

    def test_injects_missing_kwargs(self):
        out = po.apply_strategy_settings(
            "@script.strategy('T', overlay=True)\ndef main():\n    pass\n",
            po.preset_for_market("cn"),
        )
        assert "commission_value=0.05" in out
        assert "slippage=1" in out
        # currency 不凭空注入：源码没 import currency 时注入会 NameError
        assert "currency=" not in out
        assert out.count("@script.strategy(") == 1

    def test_currency_replaced_only_when_present(self):
        out = po.apply_strategy_settings(MULTI, po.preset_for_market("cn"))
        assert "currency=currency.CNY" in out
        assert "currency.USD" not in out

    def test_string_currency_stays_a_string(self):
        src = (
            "@script.strategy('T', overlay=True, currency='USD',\n"
            "                 initial_capital=1000)\ndef main():\n    pass\n"
        )
        out = po.apply_strategy_settings(src, po.preset_for_market("cn"))
        assert "currency='CNY'" in out
        assert "currency.CNY" not in out

    def test_multiline_decorator(self):
        out = po.apply_strategy_settings(MULTI, po.preset_for_market("us"))
        assert "currency=currency.USD" in out
        assert "initial_capital=100000" in out
        assert "commission_value=0.05" in out
        assert "pyramiding=0" in out
        assert "shorttitle='v.5'" in out

    def test_comma_inside_string_is_not_a_separator(self):
        out = po.apply_strategy_settings(STRINGY, po.preset_for_market("cn"))
        assert "'Title, with comma (and paren)'" in out
        assert "initial_capital=100000" in out

    def test_no_decorator_is_noop(self):
        src = "def main():\n    pass\n"
        assert po.apply_strategy_settings(src, po.preset_for_market("cn")) == src

    def test_preserves_multiline_decorator_content(self):
        out = po.apply_strategy_settings(MULTI, po.preset_for_market("cn"))
        assert "title='v.5 - 3 EMA + RSI with Trail Stop'" in out
        assert "shorttitle='v.5'" in out
        assert out.rstrip().endswith("pass")


class TestMaterialize:
    def test_original_file_unchanged(self, tmp_path):
        src = tmp_path / "candidate.py"
        src.write_text(SINGLE, encoding="utf-8")
        before = src.read_text(encoding="utf-8")

        out = po.materialize_script(src, po.preset_for_market("hk"), tmp_path / "run")
        assert out.is_file()
        assert out.read_text(encoding="utf-8") != before
        assert src.read_text(encoding="utf-8") == before

    def test_none_settings_copies_verbatim(self, tmp_path):
        src = tmp_path / "candidate.py"
        src.write_text(SINGLE, encoding="utf-8")
        out = po.materialize_script(src, None, tmp_path / "run")
        assert out.read_text(encoding="utf-8") == SINGLE

    def test_out_dir_created(self, tmp_path):
        src = tmp_path / "candidate.py"
        src.write_text(SINGLE, encoding="utf-8")
        out = po.materialize_script(src, {}, tmp_path / "a" / "b")
        assert out.parent.is_dir()


@pytest.mark.parametrize("market", ["cn", "hk", "us", "futures", "crypto"])
def test_every_market_has_a_preset(market):
    p = po.preset_for_market(market)
    assert p["commission_value"]
    assert p["currency"].startswith("currency.")
