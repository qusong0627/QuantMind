"""因子分类器测试（池总览「因子分类」口径的单源守卫）。

分类两段（优先级从高到低）：
1. ``metadata.description`` 的 ``[类别]`` 前缀 → 规则引擎归一到大类；
2. 无前缀/无描述 → 因子名关键词兜底；再不行 → ``other``（如实显示，不猜）。

金样 ``fixtures/factorCategoryGolden.json`` 冻结现有全量标签/名字的分类结果
（148 个标签前缀 + 19 个无描述因子名）：改规则必须显式同步金样，防止
「调整一个关键词导致某个类整片搬家」悄悄发生。
"""

from __future__ import annotations

import json
from pathlib import Path

from backend.services.engine.mining_plugins.factor_classify import (
    CANONICAL_CLASSES,
    classify_by_name,
    classify_factor,
    classify_raw_label,
)

GOLDEN = json.loads(
    (Path(__file__).parent / "fixtures" / "factorCategoryGolden.json").read_text("utf-8")
)


class TestGolden:
    """金样冻结：现有 148 个描述前缀与 19 个无描述因子名的分类结果。"""

    def test_all_golden_labels_reproduced(self):
        mismatches = {
            label: (cls, classify_raw_label(label))
            for label, cls in GOLDEN["labels"].items()
            if classify_raw_label(label) != cls
        }
        assert not mismatches, f"规则与金样不符（label: 金样/当前）: {mismatches}"

    def test_all_golden_names_reproduced(self):
        mismatches = {
            name: (cls, classify_by_name(name))
            for name, cls in GOLDEN["names"].items()
            if classify_by_name(name) != cls
        }
        assert not mismatches, f"规则与金样不符（name: 金样/当前）: {mismatches}"

    def test_golden_file_not_truncated(self):
        """下限守卫：复现测试游走于金样**现有条目**之上，金样被截断时它们会空转通过，
        这条下限兜住这一点。注意：新挖掘标签允许先落 ``other``（不猜），**不**在此告警；
        只有为新标签立规则时才需要把它写进金样（那一步由复现测试把关）。"""
        assert len(GOLDEN["labels"]) >= 148
        assert len(GOLDEN["names"]) >= 19


class TestTrickyOverlaps:
    """重叠词优先级（一个词同时出现在两个大类里时谁赢）。"""

    def test_interaction_beats_reversal_and_volume(self):
        # 「动量反转与量能交互因子」含 反转/量能，但本体是交互因子
        assert classify_raw_label("动量反转与量能交互因子") == "interaction"

    def test_overnight_gap_reversal_is_reversal(self):
        # 隔夜跳空反转族：经济含义是反转，隔夜只是形成机制
        assert classify_raw_label("隔夜跳空反转因子-中期种子") == "reversal"
        assert classify_raw_label("隔夜跳空反转因子") == "reversal"

    def test_plain_overnight_is_overnight(self):
        assert classify_raw_label("隔夜信息因子") == "overnight"
        assert classify_raw_label("跳空因子") == "overnight"

    def test_modifier_stripped_head_noun_wins(self):
        # 「X 调整 Y」按 Y 归类：波动率调整动量 → 动量；流动性调整波动 → 波动
        assert classify_raw_label("波动率调整动量因子") == "momentum"
        assert classify_raw_label("流动性调整动量因子") == "momentum"
        assert classify_raw_label("流动性调整反转因子") == "reversal"
        assert classify_raw_label("流动性调整波动因子") == "volatility"

    def test_price_volume_beats_volume_and_momentum(self):
        assert classify_raw_label("量价因子") == "price_volume"
        assert classify_raw_label("量价背离持续度因子") == "price_volume"
        assert classify_raw_label("个股量价时序相关系数因子") == "price_volume"

    def test_divergence_after_price_volume_goes_moneyflow(self):
        # 量价背离* 归量价；剥离余下的净流入-换手率背离归资金流
        assert classify_raw_label("背离残差因子") == "moneyflow"
        assert classify_raw_label("横截面净流入-换手率背离因子（5日窗口）") == "moneyflow"

    def test_mean_reversion_is_reversal(self):
        assert classify_raw_label("均值回归因子") == "reversal"

    def test_technical_oscillator_is_momentum(self):
        assert classify_raw_label("技术指标因子") == "momentum"
        assert classify_raw_label("超买超卖因子") == "momentum"
        assert classify_raw_label("相对强弱指标") == "momentum"

    def test_concept_heat_is_sentiment(self):
        # 概念热度整族归情绪：即使含「趋势」「相对位置」也不能被动量/位置抢走
        assert classify_raw_label("概念热度趋势因子") == "sentiment"
        assert classify_raw_label("概念热度相对位置因子") == "sentiment"
        assert classify_raw_label("市场情绪因子") == "sentiment"

    def test_valuation_and_return_dist(self):
        assert classify_raw_label("估值因子") == "valuation"
        assert classify_raw_label("收益分布偏度因子") == "return_dist"
        assert classify_raw_label("收益率因子") == "return_dist"

    def test_english_labels(self):
        assert classify_raw_label("Momentum Factor") == "momentum"
        assert classify_raw_label("Liquidity Factor") == "liquidity"
        assert classify_raw_label("Position Factor") == "position"
        assert classify_raw_label("Risk Factor") == "volatility"
        assert classify_raw_label("Volume Factor") == "volume"


class TestNameFallback:
    """无描述时的因子名兜底。"""

    def test_known_names(self):
        assert classify_by_name("volatility_14d") == "volatility"
        assert classify_by_name("volume_zscore_20d") == "volume"
        assert classify_by_name("atr_14") == "volatility"
        assert classify_by_name("rsi_14") == "momentum"
        assert classify_by_name("mfi_14") == "moneyflow"
        assert classify_by_name("overnight_gap") == "overnight"
        assert classify_by_name("momentum_5d") == "momentum"
        assert classify_by_name("intraday_return_1d") == "position"
        assert classify_by_name("avg_intraday_amplitude_10d") == "volatility"
        assert classify_by_name("return_1d") == "return_dist"

    def test_amplitude_beats_intraday(self):
        # 「intraday amplitude」是波动，不是日内位置
        assert classify_by_name("avg_intraday_amplitude_10d") == "volatility"

    def test_volume_not_hijacked_by_volatility(self):
        # "volume_*" 不能命中波动率规则（vol_ 前缀）
        assert classify_by_name("volume_ratio_5d") == "volume"
        assert classify_by_name("volume_stability_10d") == "volume"

    def test_return_not_hijacked_by_liquidity(self):
        # "return_*" 里含 "turn" 子串，不能被换手率规则（\bturn）吞掉
        assert classify_by_name("return_1d") == "return_dist"
        assert classify_by_name("return_skewness_20d") == "return_dist"
        assert classify_by_name("intraday_return_1d") == "position"

    def test_unknown_name_is_other(self):
        assert classify_by_name("xyzzy_magic_42") == "other"


class TestClassifyFactor:
    """入口函数：描述前缀优先，名字兜底，都没有给 other。"""

    def test_description_prefix_wins_over_name(self):
        # 名字像波动率，但描述前缀说日内波动 → volatility 也是波动类；
        # 换一对更明确的：名字像动量（momentum_5d），描述说反转
        cls = classify_factor(
            factor_name="momentum_5d",
            description="[反转因子] 5日反转",
        )
        assert cls.category_id == "reversal"
        assert cls.raw_label == "反转因子"
        assert cls.category_label == CANONICAL_CLASSES["reversal"]

    def test_description_without_bracket_falls_back_to_name(self):
        cls = classify_factor(factor_name="rsi_14", description="相对强弱指标 RSI")
        assert cls.category_id == "momentum"
        assert cls.raw_label is None

    def test_no_description_falls_back_to_name(self):
        cls = classify_factor(factor_name="atr_14", description=None)
        assert cls.category_id == "volatility"
        assert cls.raw_label is None

    def test_nothing_matches_is_other(self):
        cls = classify_factor(factor_name="zzz_unknown", description="")
        assert cls.category_id == "other"
        assert cls.category_label == CANONICAL_CLASSES["other"]
        assert cls.raw_label is None


class TestCanonicalClasses:
    def test_every_rule_target_is_declared(self):
        """规则只能打到声明过的大类里。"""
        import backend.services.engine.mining_plugins.factor_classify as fc

        declared = set(CANONICAL_CLASSES)
        for pattern, cls in fc._LABEL_RULES:
            assert cls in declared, f"{pattern} → {cls} 未在 CANONICAL_CLASSES 声明"
        for pattern, cls in fc._NAME_RULES:
            assert cls in declared, f"{pattern} → {cls} 未在 CANONICAL_CLASSES 声明"

    def test_labels_are_chinese(self):
        for cls, label in CANONICAL_CLASSES.items():
            assert label and not label.isascii(), f"{cls} 缺中文名"
