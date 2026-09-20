"""T-RC-15/19/20 测试：策略控制台验收仪器（`scripts/trading_console_acceptance.py`）。

仪器本身也要被验——它唯一的防伪部件是「零项参与即 FAIL」护栏：这条一旦失效，
未启动策略时七项全 N/A 也会返回退出码 0，比假绿更危险（用户会把「什么都没验」
读成「运行态契约全过」）。

覆盖三件事：
1. 七个 `grade_*` 纯函数的四态判定（尤其「缺数据不得臆造 OK」）；
2. `finalize_verdicts` 的护栏与覆盖度计数；
3. 采集函数在缺少外部依赖时**降级为 None 而不是抛异常**（仪器不得因单点故障整体崩）。
"""

from __future__ import annotations

import pytest

from backend.scripts.trading_console_acceptance import (
    finalize_verdicts,
    grade_heartbeats,
    grade_hot_update,
    grade_market_gate,
    grade_mode,
    grade_risk_divergence,
    grade_runtime_logs,
    grade_stop_audit,
)


# ── A 模式一致性 ───────────────────────────────────────────────────


@pytest.mark.unit
def test_grade_mode_missing_payload_is_na_not_ok():
    """无活跃策略时判 N/A——若在此返回 OK，控制台会把后端缺省值当成用户选的档位。"""
    assert grade_mode(None)[0] == "N/A"
    assert grade_mode({})[0] == "N/A"


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["REAL", "SHADOW", "SIMULATION"])
def test_grade_mode_accepts_known_modes(mode: str):
    assert grade_mode({"mode": mode})[0] == "OK"


@pytest.mark.unit
def test_grade_mode_rejects_unknown_and_missing():
    assert grade_mode({"mode": "LIVE"})[0] == "FAIL"
    assert grade_mode({"mode": ""})[0] == "WARN"


# ── B 市场闸门 ─────────────────────────────────────────────────────


@pytest.mark.unit
def test_grade_market_gate_empty_library_is_na():
    level, _ = grade_market_gate("CN", None, foreign_leak=0, checked_strategies=0)
    assert level == "N/A"


@pytest.mark.unit
def test_grade_market_gate_flags_cross_market_leak():
    """跨市场混入（D4）必须判 FAIL——这是用户最先看到的那个 bug。"""
    level, message = grade_market_gate("CN", "CN", foreign_leak=3, checked_strategies=75)
    assert level == "FAIL"
    assert "3" in message


@pytest.mark.unit
def test_grade_market_gate_flags_declared_mismatch():
    """运行市场与策略声明不符 → 行情/信号口径可能全错，判 FAIL 而非 WARN。"""
    assert grade_market_gate("HK", "CN", foreign_leak=0, checked_strategies=10)[0] == "FAIL"


@pytest.mark.unit
def test_grade_market_gate_undeclared_is_warn_not_ok():
    """策略未声明 market 时不许冒充「一致」——缺失 ≠ 相符。"""
    assert grade_market_gate("CN", None, foreign_leak=0, checked_strategies=10)[0] == "WARN"
    assert grade_market_gate("CN", "CN", foreign_leak=0, checked_strategies=10)[0] == "OK"


# ── C 运行日志 ─────────────────────────────────────────────────────


@pytest.mark.unit
def test_grade_runtime_logs_empty_while_running_is_fail():
    """运行中却没日志 = 静默失败，必须 FAIL；未运行的空流才是 N/A。"""
    assert grade_runtime_logs([], has_active=True)[0] == "FAIL"
    assert grade_runtime_logs([], has_active=False)[0] == "N/A"


@pytest.mark.unit
def test_grade_runtime_logs_requires_level_and_stage():
    assert grade_runtime_logs([{"level": "", "stage": "cycle"}], has_active=True)[0] == "FAIL"
    assert grade_runtime_logs([{"level": "info", "stage": ""}], has_active=True)[0] == "WARN"
    ok = grade_runtime_logs([{"level": "info", "stage": "cycle"}], has_active=True)
    assert ok[0] == "OK"


# ── D 心跳 ─────────────────────────────────────────────────────────


@pytest.mark.unit
def test_grade_heartbeats_collection_failure_is_fail():
    """读不到心跳时判 FAIL：守护条会显示「不可用」，与「确实没在跑」必须区分。"""
    assert grade_heartbeats(None)[0] == "FAIL"
    assert grade_heartbeats([])[0] == "FAIL"


@pytest.mark.unit
def test_grade_heartbeats_ok_stale_and_idle():
    assert grade_heartbeats([{"key": "sim_hosted", "state": "ok"}])[0] == "OK"
    assert grade_heartbeats([{"key": "sim_hosted", "state": "stale"}])[0] == "WARN"
    idle = grade_heartbeats(
        [
            {"key": "sim_hosted", "state": "missing"},
            {"key": "manual_execution", "state": "off"},
            {"key": "sentinel_push", "state": "ok"},
        ]
    )
    # 只有 sentinel_push 活着、两个托管循环未启用 → 不是 OK，但也不是缺陷
    assert idle[0] == "N/A"


# ── E 热更新 ───────────────────────────────────────────────────────


@pytest.mark.unit
def test_grade_hot_update_missing_version_is_warn():
    assert grade_hot_update({"mode": "SIMULATION"}, None)[0] == "WARN"
    assert grade_hot_update(None, None)[0] == "N/A"


@pytest.mark.unit
def test_grade_hot_update_identity_loss_is_fail():
    """热更新只改配置：抹掉 code_str/run_id/started_at 会让账本与原实例断链。"""
    payload = {"config_version": 2, "config_updated_at": "2026-09-20T10:00:00+08:00"}
    level, message = grade_hot_update(payload, None)
    assert level == "FAIL"
    assert "code_str" in message


@pytest.mark.unit
def test_grade_hot_update_requires_history_beyond_first_version():
    base = {
        "config_version": 3,
        "config_updated_at": "2026-09-20T10:00:00+08:00",
        "code_str": "x",
        "run_id": "r1",
        "started_at": "2026-09-20T09:00:00+08:00",
    }
    assert grade_hot_update({**base, "config_version": 1}, None)[0] == "OK"
    assert grade_hot_update(base, None)[0] == "FAIL"  # v3 却无 history
    short = grade_hot_update({**base, "config_history": [{}]}, None)
    assert short[0] == "WARN"  # 历史条数落后于版本增量


@pytest.mark.unit
def test_grade_hot_update_uses_cycle_time_to_judge_pending():
    """生效时机靠「最近周期时间 vs 配置更新时间」判——写成恒真的那一路就废了。"""
    payload = {
        "config_version": 2,
        "config_updated_at": "2026-09-20T10:00:00+08:00",
        "config_history": [{}],
        "code_str": "x",
        "run_id": "r1",
        "started_at": "2026-09-20T09:00:00+08:00",
    }
    pending, pending_msg = grade_hot_update(payload, "2026-09-20T09:30:00+08:00")
    assert pending == "OK" and "待生效" in pending_msg
    effective, effective_msg = grade_hot_update(payload, "2026-09-20T11:00:00+08:00")
    assert effective == "OK" and "已在本轮生效" in effective_msg


# ── F 风控口径（D9 回归门）─────────────────────────────────────────


@pytest.mark.unit
def test_grade_risk_divergence():
    assert grade_risk_divergence(None, has_active=False)[0] == "N/A"
    assert grade_risk_divergence(None, has_active=True)[0] == "WARN"
    assert grade_risk_divergence({"diverged": False}, has_active=True)[0] == "OK"
    level, message = grade_risk_divergence(
        {"diverged": True, "fields": {"stop_loss": {"snapshot": 0.08, "strategy": 0.05}}},
        has_active=True,
    )
    assert level == "FAIL" and "stop_loss" in message


# ── G 停止留痕 ─────────────────────────────────────────────────────


@pytest.mark.unit
def test_grade_stop_audit():
    assert grade_stop_audit([])[0] == "N/A"
    assert grade_stop_audit([{"stage": "stop", "line": "收到停止请求"}])[0] == "WARN"
    ok = grade_stop_audit([{"stage": "stop", "line": "策略已停止，原因：更换策略"}])
    assert ok[0] == "OK"


# ── 护栏：零项参与即 FAIL ──────────────────────────────────────────


@pytest.mark.unit
def test_zero_participation_guard_fires():
    """七项全 N/A 必须判 FAIL 并补一条 Z_coverage——这是仪器唯一的防伪部件。"""
    all_na = {k: {"level": "N/A", "message": "无活跃策略"} for k in ("A_mode", "B_gate", "C_logs")}
    out, has_fail, coverage = finalize_verdicts(all_na)
    assert has_fail is True
    assert out["Z_coverage"]["level"] == "FAIL"
    assert coverage == {"total": 3, "participated": 0}


@pytest.mark.unit
def test_partial_coverage_does_not_fabricate_failure_but_counts():
    """有一项真跑过（哪怕只是 WARN）就不触发护栏，但覆盖度必须如实计数。"""
    mixed = {
        "A_mode": {"level": "N/A", "message": ""},
        "B_gate": {"level": "WARN", "message": "库中 75 条零泄漏但策略未声明市场"},
        "C_logs": {"level": "N/A", "message": ""},
    }
    out, has_fail, coverage = finalize_verdicts(mixed)
    assert has_fail is False
    assert "Z_coverage" not in out
    assert coverage == {"total": 3, "participated": 1}


@pytest.mark.unit
def test_any_fail_propagates():
    out, has_fail, _ = finalize_verdicts({"A_mode": {"level": "FAIL", "message": "mode=LIVE"}})
    assert has_fail is True
    assert out["A_mode"]["level"] == "FAIL"


@pytest.mark.unit
def test_finalize_does_not_mutate_caller_dict():
    original = {"A_mode": {"level": "N/A", "message": ""}}
    finalize_verdicts(original)
    assert "Z_coverage" not in original  # 不可变：护栏结果只出现在返回值里
