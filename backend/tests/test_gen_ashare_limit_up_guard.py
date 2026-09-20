"""`as43_limit_up_guard` 生成体的涨停阈值测试。

该模板的 `LimitUpGuardStrategy` 以**字符串形式**存在生成器里
（`scripts/gen_ashare_strategy_templates.py` 的 `_LIMIT_UP_BODY`），再落到
`strategy_templates/as43_limit_up_guard.py`。所以这里直接 exec 那段字符串 ——
测的是**将要/已经生成出去的那份代码**，不是生成器自身的某个辅助函数。

旧实现 `_limit_threshold(symbol)` 按前缀返回 0.095/0.195/0.295：不看日期
（2020-08-24 前的创业板是 10% 板却套 19.5% 的线 → 那几年真涨停从不计数，
「涨停规避」在最需要它的年份静默失效）、`("4","8","9")` 兜底把沪市 900xxx
的 B 股当北交所、且没有 5% 的 ST 档。
"""

from __future__ import annotations

import os
import sys

import pandas as pd
import pytest

project_root = os.path.join(os.path.dirname(__file__), "../../")
sys.path.append(project_root)


#: 主板 / 宽板 / 北交所，各扣 0.5pp 取整余量。
_MAIN = 0.095  # fidelity: allow-limit-threshold — 期望值，钉住既有口径
_WIDE = 0.195  # fidelity: allow-limit-threshold — 期望值，钉住既有口径
_BSE = 0.295  # fidelity: allow-limit-threshold — 期望值，钉住既有口径

_REFORM_BEFORE = pd.Timestamp("2019-06-03")
_REFORM_AFTER = pd.Timestamp("2024-01-02")


@pytest.fixture(scope="module")
def limit_threshold():
    """exec 生成体，取出 `LimitUpGuardStrategy._limit_threshold`。"""
    from scripts.gen_ashare_strategy_templates import _LIMIT_UP_BODY

    class _StubRecordingStrategy:  # 生成体的唯一外部依赖
        pass

    ns: dict = {
        "pd": pd,
        "RedisRecordingStrategy": _StubRecordingStrategy,
        "__name__": "generated_limit_up_guard",
    }
    exec(compile(_LIMIT_UP_BODY, "<_LIMIT_UP_BODY>", "exec"), ns)  # noqa: S102
    return ns["LimitUpGuardStrategy"]._limit_threshold


def test_threshold_main_board(limit_threshold):
    assert limit_threshold("sz000001", _REFORM_AFTER) == _MAIN
    assert limit_threshold("sh600036", _REFORM_AFTER) == _MAIN


def test_threshold_wide_boards(limit_threshold):
    assert limit_threshold("sz300750", _REFORM_AFTER) == _WIDE
    assert limit_threshold("sh688981", _REFORM_AFTER) == _WIDE


def test_threshold_pre_reform_chinext_is_main_board(limit_threshold):
    """2020-08-24 前创业板是 10% 板 —— 旧实现在这里给 19.5%。"""
    assert limit_threshold("sz300750", _REFORM_BEFORE) == _MAIN


def test_threshold_sh_b_share_is_not_bse(limit_threshold):
    """沪市 900xxx 是 B 股（10% 板），旧实现的 `("4","8","9")` 兜底把它当北交所。

    这类票一旦被套上 30% 的线，永远判不出涨停 —— 涨停规避对它们完全失效。
    """
    assert limit_threshold("sh900901", _REFORM_AFTER) == _MAIN


def test_threshold_bse(limit_threshold):
    assert limit_threshold("bj430047", _REFORM_AFTER) == _BSE
    assert limit_threshold("bj830799", _REFORM_AFTER) == _BSE


def test_threshold_reads_ref_date(limit_threshold):
    """同一代码换日期必须换阈值 —— ref_date 被吞掉的话，上面「板别」用例
    仍会全绿，改革分界线却被静默忽略。"""
    assert limit_threshold("sz300750", _REFORM_BEFORE) < limit_threshold(
        "sz300750", _REFORM_AFTER
    )


def test_threshold_covers_302_prefix(limit_threshold):
    """302 前缀在旧表里同样缺失，会被当成主板。"""
    assert limit_threshold("sz302132", _REFORM_AFTER) == _WIDE


def test_threshold_falls_back_to_the_strictest_board():
    """权威实现不可用时必须按**最严**的主板线兜底，而不是放行。

    兜底方向比数值更重要：退回 0.095 只会让宽板票被过度剔除（少交易），
    退回 0.295 则会把宽板的真涨停当普通日放进买入清单（假收益）。
    """
    from scripts.gen_ashare_strategy_templates import _LIMIT_UP_BODY

    class _StubRecordingStrategy:
        pass

    ns: dict = {
        "pd": pd,
        "RedisRecordingStrategy": _StubRecordingStrategy,
        "__name__": "generated_limit_up_guard_fallback",
    }
    exec(compile(_LIMIT_UP_BODY, "<_LIMIT_UP_BODY>", "exec"), ns)  # noqa: S102
    cls = ns["LimitUpGuardStrategy"]

    # 传一个 limit_pct 无法解析的日期 → 走 except 分支
    assert cls._limit_threshold("sz300750", "not-a-date") == _MAIN


def test_generated_body_has_no_prefix_table():
    """结构护栏：生成体里不得再出现按前缀复述阈值的写法。

    否则「收敛到权威口径」会被下一次改动悄悄回退，而所有数值型用例仍可能通过。
    """
    from scripts.gen_ashare_strategy_templates import _LIMIT_UP_BODY

    assert "startswith" not in _LIMIT_UP_BODY
    assert "limit_pct" in _LIMIT_UP_BODY
