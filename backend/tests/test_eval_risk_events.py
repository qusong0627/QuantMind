"""账户卡「风控事件」维的取数与评分（`scripts/eval/risk_events.py`，设计 §2.5）。

实盘数据把这维的两处坑做实了：
- `risk_events.status` 的真实取值是 ``skipped_no_quote`` / ``no_targets``（全库 8028 条），
  而评分代码按 ``failed``/``skipped``/``alert`` 精确取键——**一个都对不上**，
  于是「罚分」恒为 0，风控维变成静默满分；
- ``skipped_no_quote`` 的真实语义是「有持仓但取不到行情 → 止损/止盈**判不了**」
  （`risk_trigger_eval.py`），这是风控**盲区**，不能当普通跳过忽略。
"""

from __future__ import annotations

import pytest

from backend.scripts.eval.risk_events import (
    BLIND_RED_LINE,
    db_user_id_keys,
    score_risk_events,
    summarize_statuses,
)


# ── 状态词表 ─────────────────────────────────────────────────────────


@pytest.mark.unit
def test_classify_maps_the_real_status_vocabulary():
    """库里的真实取值必须落在正确的类上（不是按名字猜）。"""
    from backend.scripts.eval.risk_events import classify_status

    assert classify_status("skipped_no_quote") == "blind"
    assert classify_status("no_targets") == "no_targets"
    assert classify_status("failed") == "failed"
    assert classify_status("failed_execute") == "failed"
    assert classify_status("order_error") == "failed"
    assert classify_status("alert") == "alert"
    assert classify_status("filled") == "filled"
    assert classify_status("applied") == "filled"
    assert classify_status("some_new_status") == "other"


@pytest.mark.unit
def test_summarize_statuses_reports_unknown_instead_of_dropping():
    """没见过的状态要单列出来（新增状态静默归零 = 罚分永远不触发）。"""
    summary = summarize_statuses([("skipped_no_quote", 10), ("weird_new_status", 3)])

    assert summary["blind"] == 10
    assert summary["unknown_statuses"] == {"weird_new_status": 3}
    assert summary["n_events"] == 13


@pytest.mark.unit
def test_summarize_statuses_computes_blind_ratio():
    summary = summarize_statuses([("skipped_no_quote", 9), ("filled", 1)])

    assert summary["blind_ratio"] == pytest.approx(0.9)
    assert summary["raw_statuses"] == {"skipped_no_quote": 9, "filled": 1}


@pytest.mark.unit
def test_summarize_statuses_handles_empty_input():
    summary = summarize_statuses([])

    assert summary["n_events"] == 0
    assert summary["blind_ratio"] == 0.0


# ── user_id 键形 ─────────────────────────────────────────────────────


@pytest.mark.unit
def test_db_user_id_keys_collapses_admin_family_to_canonical():
    """管理员族（10000001/00000001/1/0/admin）是同一个账户：查库要一次读全。"""
    for raw in ("10000001", "00000001", "1", "0", "admin", ""):
        assert db_user_id_keys(raw) == [10000001, 1, 0], raw


@pytest.mark.unit
def test_db_user_id_keys_keeps_other_users_out_of_the_admin_family():
    """其它数字用户的键形各自独立，绝不掺进管理员族。"""
    assert db_user_id_keys("42") == [42]
    assert db_user_id_keys("00000042") == [42]


# ── 评分 ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_score_is_insufficient_when_risk_machinery_was_never_on():
    dim = score_risk_events({"available": False, "window_days": 7})

    assert dim.score is None
    assert dim.detail["insufficient"] is True
    assert "风控机制未启用" in str(dim.detail["note"])


@pytest.mark.unit
def test_blind_ratio_drives_score_and_red_line():
    """全库真实形态：7992 次无行情跳过 + 36 次无目标 → 风控基本是瞎的。"""
    summary = {
        "available": True,
        "window_days": 7,
        **summarize_statuses([("skipped_no_quote", 7992), ("no_targets", 36)]),
    }

    dim = score_risk_events(summary)

    assert dim.score is not None and dim.score < 40.0
    assert dim.red_line_failed is True
    assert "无行情" in str(dim.detail["red_line"])
    assert dim.detail["blind_ratio"] >= BLIND_RED_LINE
    assert dim.detail["raw_statuses"]["skipped_no_quote"] == 7992


@pytest.mark.unit
def test_healthy_risk_engine_scores_high_without_red_line():
    summary = {
        "available": True,
        "window_days": 7,
        **summarize_statuses([("filled", 40), ("alert", 2), ("no_targets", 5)]),
        "rejected_orders": 0,
    }

    dim = score_risk_events(summary)

    assert dim.score is not None and dim.score >= 85.0
    assert dim.red_line_failed is False


@pytest.mark.unit
def test_failed_red_line_still_fires_on_execution_failures():
    """执行失败红线（failed ≥3）在真实词表下必须真的能触发。"""
    summary = {
        "available": True,
        "window_days": 7,
        **summarize_statuses([("filled", 10), ("failed", 3)]),
    }

    dim = score_risk_events(summary)

    assert dim.red_line_failed is True
    assert "失败" in str(dim.detail["red_line"])
    assert dim.detail["failed"] == 3


@pytest.mark.unit
def test_account_with_zero_window_events_is_insufficient_not_perfect():
    """租户有 8028 条历史但本账户窗口内 0 条 → 缺省，不许静默 100 分。

    实测 user=42 就是这个形态：`available` 是租户级判定，会误放行。
    """
    summary = {
        "available": True,
        "window_days": 7,
        "tenant_events_all_time": 8028,
        **summarize_statuses([]),
    }

    dim = score_risk_events(summary)

    assert dim.score is None
    assert dim.detail["insufficient"] is True
    assert "无记录不等于无事件" in str(dim.detail["note"])
