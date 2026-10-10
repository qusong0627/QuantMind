"""T7-2 字母目标清理脚本的谓词与分类单测（不触 DB）。

钉住与 T4-2 事件层拒收口径（``news_intel.normalize_targets`` 的 1-2 位裸字母
分支）一致的三条语义：全字母目标=删除、混合=剔除字母且保序、字母 symbol=修为
首个存活目标；以及漂移护栏（候选超限即中止）。
"""

from __future__ import annotations

import pytest

from backend.scripts.cleanup_letter_targets import (
    RE_LETTER_TARGET,
    _CANDIDATE_SQL,
    assert_capped,
    classify_letter_row,
    is_letter_target,
)


@pytest.mark.unit
def test_letter_regex_hits_letter_codes_not_real_symbols():
    for token in ("GS", "BA", "IP", "IT", "A", "li"):
        assert is_letter_target(token), token
    for token in ("AAPL", "NVDA", "1113.HK", "600036.SH", "*", "", "NDAQ"):
        assert not is_letter_target(token), token


@pytest.mark.unit
def test_classify_all_letter_targets_deletes():
    assert classify_letter_row("BA", ["BA"]) == {"action": "delete"}
    assert classify_letter_row("GS", ["IP", "PG"]) == {"action": "delete"}


@pytest.mark.unit
def test_classify_mixed_scrubs_letters_preserving_order():
    verdict = classify_letter_row("NVDA", ["NVDA", "MSFT", "ON", "AMAT", "MU"])
    assert verdict == {
        "action": "scrub",
        "new_symbol": "NVDA",
        "new_targets": ["NVDA", "MSFT", "AMAT"],
    }


@pytest.mark.unit
def test_classify_letter_symbol_is_repaired_to_first_survivor():
    verdict = classify_letter_row("GS", ["GS", "JPM", "NVDA"])
    assert verdict == {
        "action": "scrub",
        "new_symbol": "JPM",
        "new_targets": ["JPM", "NVDA"],
    }


@pytest.mark.unit
def test_classify_letter_symbol_without_letter_targets_still_repairs():
    # 纵深防御臂：symbol 漂移为字母但目标集干净，仍须修复 symbol
    verdict = classify_letter_row("BA", ["AAPL"])
    assert verdict == {"action": "scrub", "new_symbol": "AAPL", "new_targets": ["AAPL"]}


@pytest.mark.unit
def test_classify_empty_targets_with_letter_symbol_deletes():
    assert classify_letter_row("BA", []) == {"action": "delete"}


@pytest.mark.unit
def test_classify_clean_row_returns_none():
    assert classify_letter_row("600036.SH", ["600036.SH"]) is None
    assert classify_letter_row("*", []) is None


@pytest.mark.unit
def test_cap_guard_aborts_only_when_exceeded():
    assert_capped(1804, 2500, "sentinel_alerts(delete)")  # 预期量：放行
    with pytest.raises(RuntimeError, match="超上限"):
        assert_capped(2501, 2500, "sentinel_alerts(delete)")


@pytest.mark.unit
def test_candidate_sql_is_parameterized():
    sql = str(_CANDIDATE_SQL)
    # 正则不得硬编码进 SQL 文本（裸 % 经 text() 会与占位符语义混淆）
    assert ":letter_re" in sql
    assert RE_LETTER_TARGET not in sql
