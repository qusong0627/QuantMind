"""T7-2 清理脚本的守护与谓词单测（不触 DB/Redis）。

重点不是覆盖删除路径（那是运维执行面，靠 dry-run 报告人工核验），而是钉住
**误删防线**：受保护真行守护、候选上限守护、测试消费组前缀过滤，以及夹具词汇
正则不得误伤真行标题/账户。
"""

from __future__ import annotations

import re

import pytest

from backend.scripts.cleanup_test_residue import (
    DATA_JUMP_FIXTURE_METRICS,
    PROTECTED_NOTES,
    PROTECTED_NOTIFICATION_IDS,
    RE_SYMBOL_VOCAB,
    RE_UID_VOCAB,
    ROUND_STALL_PREFIX,
    _NOTIFICATION_SQL,
    assert_capped,
    assert_protected_absent,
    is_test_group,
)

_FIXTURE_TITLES = (
    "T13C1A 日线跳变",
    "[critical] T1DCAD 异常放量",
    "账户 90003abf 撤单率异常",
    "模型 mdl_it_train_20261008010203_1dcad6abcd_e IC 异常",
)
_REAL_TITLES = (
    "600036.SH 日线跳变",
    "招商银行(600036.SH) 出现重大利空",
    "决策轮 11:05「glm-5.3-flash」 模型未出决策",
    "决策轮整天没跑：2026-10-08 一条轮次都没有",
    "账户 10000001 撤单率异常",
)


@pytest.mark.unit
def test_symbol_vocab_regex_hits_fixtures_not_real():
    for title in _FIXTURE_TITLES[:2]:
        assert re.search(RE_SYMBOL_VOCAB, title), title
    for title in _REAL_TITLES:
        assert not re.search(RE_SYMBOL_VOCAB, title), title


@pytest.mark.unit
def test_uid_vocab_regex_hits_fixtures_not_real():
    assert re.search(RE_UID_VOCAB, "账户 90003abf 撤单率异常")
    for title in ("账户 10000001 撤单率异常", "决策轮整天没跑：2026-10-08 一条轮次都没有"):
        assert not re.search(RE_UID_VOCAB, title), title


@pytest.mark.unit
def test_protected_guard_aborts_on_real_row_in_candidates():
    assert_protected_absent([1, 2, 3])  # 无交集：放行
    for pid in PROTECTED_NOTIFICATION_IDS:
        with pytest.raises(RuntimeError, match="受保护真行"):
            assert_protected_absent([100, pid])


@pytest.mark.unit
def test_protected_ids_have_documented_rationale():
    assert set(PROTECTED_NOTES) == set(PROTECTED_NOTIFICATION_IDS)
    assert 6332 in PROTECTED_NOTIFICATION_IDS, "整天没跑行内容属实，必须保留"


@pytest.mark.unit
def test_cap_guard_aborts_only_when_exceeded():
    assert_capped(362, 500, "notifications")  # 预期量：放行
    with pytest.raises(RuntimeError, match="超上限"):
        assert_capped(501, 500, "notifications")


@pytest.mark.unit
def test_group_filter_selects_only_test_prefixed_groups():
    assert is_test_group("sentinel-itest-06ccf4b7")
    assert is_test_group("regime-test-0a24387c")
    for name in ("intel:ws", "sentinel", "realtime_regime", "regime", ""):
        assert not is_test_group(name), name


@pytest.mark.unit
def test_delete_sql_keeps_stall_row_guard_and_fingerprint():
    sql = str(_NOTIFICATION_SQL)
    # 6332（整天没跑）排除臂必须留在 SQL 里——漂移掉它就等于把真行拖进删除面
    assert ":stall_prefix" in sql
    assert ROUND_STALL_PREFIX.endswith("%"), "前缀参数须为 LIKE 模式"
    assert DATA_JUMP_FIXTURE_METRICS == {"close": 20.0, "prev_close": 11.0}
