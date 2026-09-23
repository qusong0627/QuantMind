"""`shared/symbol_policy.py` 的不变量：名称类禁买判据的**唯一出处**。

这组测试看着很琐碎（就是几个字符串），但它钉的是一件真事：同一句「名称里有 ST
就别买」在本仓曾经有两份**不一样**的实现（一份子串命中、一份前缀表，且都没看
退市）。判定分裂的后果不是"多拦一只"，而是**两处对同一只票给出相反结论**。
故这里逐条钉死形态，谁要改判据，先来这里改期望。
"""

from __future__ import annotations

import pytest

from backend.shared.symbol_policy import (
    DELIST_MARK,
    ST_PREFIXES,
    is_risky_name,
)


class TestRiskyNames:
    @pytest.mark.parametrize(
        "name",
        ["ST海航", "*ST三圣", "SST前锋", "S*ST生化", "ST 三圣", "  *ST 三圣"],
    )
    def test_st_forms_are_risky(self, name: str) -> None:
        """四种前缀 + 中间带空格的形态（真实源里出现过 `ST 三圣`）。"""
        assert is_risky_name(name)

    @pytest.mark.parametrize("name", ["退市海润", "海润退"])
    def test_delisting_forms_are_risky(self, name: str) -> None:
        """退市整理期两种写法都判（A 股简称里「退」基本只出现在这一类）。"""
        assert is_risky_name(name)

    @pytest.mark.parametrize("name", ["招商银行", "贵州茅台", "宁德时代", ""])
    def test_normal_names_are_not_risky(self, name: str) -> None:
        assert not is_risky_name(name)

    def test_missing_name_fails_open(self) -> None:
        """取不到名称 → 放行（宁可漏拦一只，也不能停掉当天所有买入）。"""
        assert not is_risky_name(None)
        assert not is_risky_name("   ")

    def test_substring_st_is_not_enough(self) -> None:
        """**刻意不看子串**：旧实现用 `"ST" in name.upper()`，会把任何含 ST 的
        名字判成风险股；本判据只认前缀。"""
        assert not is_risky_name("华STAR科技")

    def test_lowercase_input_is_normalised(self) -> None:
        assert is_risky_name("st海航")

    def test_full_width_space_is_stripped(self) -> None:
        assert is_risky_name("ST　三圣")


def test_prefix_table_covers_the_marked_forms() -> None:
    """前缀表本身是数据，改它要有人看见（`symbol_policy` 的唯二常量之一）。"""
    assert set(ST_PREFIXES) == {"ST", "*ST", "SST", "S*ST"}
    assert DELIST_MARK == "退"
