"""
策略模板市场过滤单元测试

测试 template_applies_to_market 的市场匹配语义：
- A股视图（market=CN/A）：无 markets 标记的历史模板 + 显式 a_share，不含纯港股
- HK/US/CRYPTO 视图：只含各自显式标记的模板
- 未知 / 缺省市场：不过滤（保持向后兼容返回全部）
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.services.engine.qlib_app.services.strategy_templates import (  # noqa: E402
    StrategyTemplate,
    template_applies_to_market,
)


def _make_template(template_id: str, markets: list[str]) -> StrategyTemplate:
    """构造一个带市场标记的模板对象（无需落盘）。"""
    return StrategyTemplate(
        id=template_id,
        name=template_id,
        description="",
        category="basic",
        difficulty="beginner",
        code="STRATEGY_CONFIG = {}",
        params=[],
        markets=markets,
    )


class TestTemplateMarketFilter:
    def test_legacy_template_without_markets_only_in_cn_view(self):
        """历史 A 股模板（markets 为空）只出现在 CN/A 视图，不得混入 HK 等视图。"""
        legacy = _make_template("standard_topk", [])
        assert template_applies_to_market(legacy, "CN") is True
        assert template_applies_to_market(legacy, "A") is True
        assert template_applies_to_market(legacy, "HK") is False
        assert template_applies_to_market(legacy, "US") is False
        assert template_applies_to_market(legacy, "CRYPTO") is False

    def test_hk_template_excluded_from_cn_view(self):
        """纯港股模板不得出现在 A 股视图。"""
        hk = _make_template("hk_standard_topk", ["hong_kong"])
        assert template_applies_to_market(hk, "CN") is False
        assert template_applies_to_market(hk, "A") is False
        assert template_applies_to_market(hk, "HK") is True

    def test_explicit_a_share_template_in_cn_view(self):
        """显式标记 a_share 的模板出现在 CN/A 视图（大小写不敏感）。"""
        t = _make_template("cn_momentum", ["A_SHARE"])
        assert template_applies_to_market(t, "cn") is True
        assert template_applies_to_market(t, "HK") is False

    def test_multi_market_template_shared_between_views(self):
        """同时标记 a_share + hong_kong 的模板两个视图都应可见。"""
        shared = _make_template("ls_topk", ["a_share", "hong_kong"])
        assert template_applies_to_market(shared, "CN") is True
        assert template_applies_to_market(shared, "HK") is True
        assert template_applies_to_market(shared, "US") is False

    def test_us_and_crypto_views(self):
        """US/CRYPTO 视图按各自标记过滤。"""
        us = _make_template("us_topk", ["us_stock"])
        crypto = _make_template("btc_momentum", ["crypto"])
        assert template_applies_to_market(us, "US") is True
        assert template_applies_to_market(us, "CN") is False
        assert template_applies_to_market(crypto, "CRYPTO") is True
        assert template_applies_to_market(crypto, "US") is False

    def test_market_alias_normalization(self):
        """HK/US 的别名与主键等价。"""
        hk = _make_template("hk_topk", ["hong_kong"])
        us = _make_template("us_topk", ["us_stock"])
        assert template_applies_to_market(hk, "hk") is True
        assert template_applies_to_market(hk, "HONG_KONG") is True
        assert template_applies_to_market(us, "us_stock") is True
        assert template_applies_to_market(us, "US") is True

    def test_unknown_or_missing_market_returns_all(self):
        """缺省 / 未知市场不过滤（旧行为：返回全部模板）。"""
        hk = _make_template("hk_topk", ["hong_kong"])
        legacy = _make_template("standard_topk", [])
        assert template_applies_to_market(hk, None) is True
        assert template_applies_to_market(legacy, None) is True
        assert template_applies_to_market(hk, "FUZZY_MARKET") is True
