"""``StockCodeUtil.classify_board`` 测试（手动任务台账/推理分组共用的上市板口径）。

为什么值得单独测：这个分类同时服务两处 —— UI 台账上的「科创板 / 创业板」标签，
以及 ``position_signal.compute_position_scores`` 的板内百分位分组。两处口径必须
一模一样，否则「界面上写着创业板、分组却按深主板算」。本用例即该口径的唯一台账。

边界确权：非 A 股代码（港股/美股/空）**不许猜市场**，一律「其他」；9 开头（沪市
B 股）沿用原 position_signal 口径归北交所组，改它属于评分口径变更。
"""

from backend.shared.stock_utils import StockCodeUtil

# Arrange 用的三种层口径写法：后缀式（QuantDB/Qlib 层）、前缀式（PG/前端层）、裸码
SUFFIX_CASES = [
    ("688217.SH", "科创板"),
    ("300750.SZ", "创业板"),
    ("301001.SZ", "创业板"),
    ("002552.SZ", "中小板"),
    ("003816.SZ", "中小板"),
    ("000001.SZ", "深主板"),
    ("001979.SZ", "深主板"),
    ("600036.SH", "沪主板"),
    ("601398.SH", "沪主板"),
    ("430047.BJ", "北交所"),
    ("833171.BJ", "北交所"),
    ("870508.BJ", "北交所"),
]


class TestClassifyBoard:
    def test_suffix_codes(self):
        for code, expected in SUFFIX_CASES:
            assert StockCodeUtil.classify_board(code) == expected, code

    def test_prefix_and_bare_codes_same_verdict(self):
        """跨层写法必须同判：前端给前缀式、推理侧给裸码，结果不能分叉。"""
        for code, expected in SUFFIX_CASES:
            suffix, market = code.split(".")
            prefix = f"{market}{suffix}"
            assert StockCodeUtil.classify_board(prefix) == expected, prefix
            assert StockCodeUtil.classify_board(suffix) == expected, suffix

    def test_non_a_share_is_other_never_guessed(self):
        """港股/美股/垃圾输入 → 「其他」：不许按首位数字硬套 A 股板别。"""
        assert StockCodeUtil.classify_board("00700.HK") == "其他"
        assert StockCodeUtil.classify_board("0700.HK") == "其他"
        assert StockCodeUtil.classify_board("AAPL") == "其他"
        assert StockCodeUtil.classify_board("BRK.B") == "其他"
        assert StockCodeUtil.classify_board("") == "其他"
        assert StockCodeUtil.classify_board("12345") == "其他"

    def test_b_share_grouping_is_inherited_not_reclassified(self):
        """9 开头（沪 B）沿用北交所分组 —— 锁定现状，防有人「顺手修正」挪动评分分组。"""
        assert StockCodeUtil.classify_board("900901") == "北交所"
        assert StockCodeUtil.classify_board("900901.SH") == "北交所"

    def test_matches_position_signal_forwarder(self):
        """推理侧转发必须与本体逐例一致（两处口径分叉就是事故）。"""
        from backend.services.engine.inference.position_signal import _classify_board

        probes = [code for code, _ in SUFFIX_CASES] + [
            "00700.HK",
            "AAPL",
            "",
            "900901",
            "688217",
            "SH600036",
        ]
        for probe in probes:
            assert _classify_board(probe) == StockCodeUtil.classify_board(probe), probe
