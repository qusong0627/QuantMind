"""程序化交易报告义务模块测试：阈值单位、待填报文本、只告警不拦截。"""

from __future__ import annotations

import logging

import pytest

from backend.shared.programmatic_trading_disclosure import (
    HFT_ORDERS_PER_DAY,
    HFT_ORDERS_PER_MINUTE,
    HFT_ORDERS_PER_SECOND,
    SOFTWARE_DEVELOPER,
    SOFTWARE_NAME,
    disclosure_lines,
    disclosure_text,
    is_high_frequency_order_rate,
    log_high_frequency_warning,
    software_version,
)


class TestThresholdUnits:
    """单位换算是最容易错的地方：法规按「每秒」，风控配置按「每分钟」。"""

    def test_每分阈值等于每秒阈值乘六十(self):
        assert HFT_ORDERS_PER_MINUTE == HFT_ORDERS_PER_SECOND * 60

    def test_每秒三百笔(self):
        assert HFT_ORDERS_PER_SECOND == 300

    def test_每分钟一万八(self):
        assert HFT_ORDERS_PER_MINUTE == 18000

    def test_单日两万笔(self):
        assert HFT_ORDERS_PER_DAY == 20000


class TestHighFrequencyClassification:
    """边界必须精确：差一笔就不算，能判定为「已触及」才是护栏的意义。"""

    @pytest.mark.parametrize("per_minute", [17999, 17999.9, 60, 1000, 0])
    def test_未达阈值(self, per_minute):
        assert is_high_frequency_order_rate(per_minute) is False

    @pytest.mark.parametrize("per_minute", [18000, 18001, 60000])
    def test_达到或超过阈值(self, per_minute):
        assert is_high_frequency_order_rate(per_minute) is True

    def test_默认风控配置远在阈值之下(self):
        # l3.order_frequency 默认 60 笔/分 = 1 笔/秒
        assert is_high_frequency_order_rate(60) is False

    @pytest.mark.parametrize("value", [None, "", "abc", True, False])
    def test_取不到值不判为高频(self, value):
        """配置缺失/类型不对是另一回事，不能报成「你可能被认定为高频」。"""
        assert is_high_frequency_order_rate(value) is False

    def test_字符串数字按数值判定(self):
        assert is_high_frequency_order_rate("18000") is True
        assert is_high_frequency_order_rate("100") is False

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_非有限值不判为高频(self, value):
        assert is_high_frequency_order_rate(value) is False


class TestDisclosureText:
    def test_三行必填项齐备(self):
        lines = disclosure_lines()
        assert len(lines) == 3
        assert lines[0] == f"交易软件名称：{SOFTWARE_NAME}"
        assert lines[1].startswith("交易软件版本号：")
        assert lines[2] == f"开发者（供应商）名称：{SOFTWARE_DEVELOPER}"

    def test_文本块是三行拼接(self):
        assert disclosure_text() == "\n".join(disclosure_lines())

    def test_版本号取自版本单一事实源(self):
        assert f"交易软件版本号：{software_version()}" in disclosure_text()

    def test_版本号非空_不能是空串(self):
        """空版本号填进券商报告表等于没填，留空要看得见。"""
        assert software_version().strip() != ""

    @pytest.mark.parametrize("raw", [None, "", "   ", "\t\n"])
    def test_开发者留空时回落到默认值(self, raw):
        from backend.shared.programmatic_trading_disclosure import (
            SOFTWARE_DEVELOPER as default_name,
            resolve_developer,
        )

        assert resolve_developer(raw) == default_name
        assert resolve_developer(raw).strip() != ""

    def test_开发者有值时原样取用并去空白(self):
        from backend.shared.programmatic_trading_disclosure import resolve_developer

        assert resolve_developer("  Acme 分发版  ") == "Acme 分发版"


class TestWarningOnlyNotBlocking:
    """护栏只告警：被认定为高频交易不违法，拦下来才是越权。"""

    def test_未达阈值不告警(self, caplog):
        with caplog.at_level(logging.WARNING):
            warned = log_high_frequency_warning(60, source="test")
        assert warned is False
        assert caplog.records == []

    def test_达阈值告警且带够上下文(self, caplog):
        with caplog.at_level(logging.WARNING):
            warned = log_high_frequency_warning(20000, source="redis:qm:risk:config")
        assert warned is True
        assert len(caplog.records) == 1
        message = caplog.records[0].getMessage()
        # 告警必须让人知道「多少」「哪个来源」「要额外做什么」
        assert "20000" in message
        assert "18000" in message
        assert "300" in message
        assert "redis:qm:risk:config" in message
        assert "高频交易" in message

    def test_告警措辞说明本工具不代为报告(self, caplog):
        with caplog.at_level(logging.WARNING):
            log_high_frequency_warning(18000, source="test")
        message = caplog.records[0].getMessage()
        assert "不代为报告" in message
