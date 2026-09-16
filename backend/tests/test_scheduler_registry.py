"""T-P1-06 测试：调度注册表 + 心跳协议 + 控制台分发表。

覆盖：
1. 注册表完整性（键唯一/字段齐全/心跳 TTL 与周期的合理关系/可重跑项都在分发表）；
2. switch_enabled 纯函数；classify_scheduler_status 纯函数（stale=fail / missing=warn / off 不算）；
3. heartbeat() best-effort（假 redis 成功写入；坏 redis 不抛出）；
4. 接线源断言：全部任务写入心跳（worker + celery；T-P2-06 后 5+2 worker + 6 celery），漏一个即测试红；
5. 体检 C07 使用注册表；schedule_ctl 分发表覆盖声明了 rerun 的任务。
"""

from pathlib import Path

import pytest

from backend.shared.scheduler_registry import (
    JOBS,
    JOBS_BY_KEY,
    classify_scheduler_status,
    heartbeat,
    heartbeat_key,
    switch_enabled,
)

_BACKEND = Path(__file__).resolve().parents[1]

_HEARTBEAT_WIRED = {
    "equity_settle": "services/simulation/services/equity_settlement_worker.py",
    "sim_eod": "services/simulation/services/eod_service.py",
    "pending_order": "services/simulation/services/pending_order_worker.py",
    "t1_unlock": "services/simulation/services/simulation_t1_unlock_task.py",
    "corp_action": "services/simulation/services/simulation_corporate_action_task.py",
    # T-P2-06 新增（对账/影子对照）
    "dual_book": "services/trade/services/dual_book_reconciliation_task.py",
    "mirror_shadow": "services/trade/services/shadow_compare_service.py",
    # T-P4-05b 新增（EOD 五卡评分）
    "eval_scores": "services/trade/services/eval_scores_service.py",
    # T-P4-06 新增（月度体检复检）
    "health_recheck": "services/trade/services/health_recheck_service.py",
}


def test_registry_integrity():
    keys = [j.key for j in JOBS]
    assert len(keys) == len(set(keys)), "任务 key 必须唯一"
    for j in JOBS:
        assert j.kind in {"worker", "celery_beat"}
        assert j.owner in {"trade", "celery"}
        assert j.schedule
        if j.heartbeat_ttl is not None:
            assert j.heartbeat_ttl >= 60, f"{j.key} 心跳 TTL 过小"
        if j.rerun:
            assert "schedule_ctl.py run" in j.rerun, f"{j.key} 重跑命令须走控制台"


def test_switch_enabled_pure():
    spec = JOBS_BY_KEY["sim_eod"]
    assert switch_enabled(spec, env={}) is True  # 默认开
    assert switch_enabled(spec, env={"SIM_EOD_WORKER_ENABLED": "false"}) is False
    assert switch_enabled(spec, env={"SIM_EOD_WORKER_ENABLED": "1"}) is True
    always = JOBS_BY_KEY["t1_unlock"]
    assert switch_enabled(always, env={"ANY": "off"}) is True  # 常开任务


def test_classify_scheduler_status_pure():
    ok_entries = [
        {"key": "a", "name": "A", "enabled": True, "state": "ok", "age": 10, "ttl": 60},
        {"key": "b", "name": "B", "enabled": False, "state": "off", "age": None, "ttl": 60},
    ]
    level, detail, metrics = classify_scheduler_status(ok_entries)
    assert level == "ok" and metrics["off"] == ["b"]

    stale_entries = [
        {"key": "a", "name": "A", "enabled": True, "state": "stale", "age": 999, "ttl": 60}
    ]
    level, detail, metrics = classify_scheduler_status(stale_entries)
    assert level == "fail" and "停摆" in detail and metrics["stale"] == ["a"]

    missing_entries = [
        {"key": "a", "name": "A", "enabled": True, "state": "missing", "age": None, "ttl": 60}
    ]
    level, detail, _ = classify_scheduler_status(missing_entries)
    assert level == "warn" and "无心跳记录" in detail


class _FakeRedis:
    def __init__(self, explode: bool = False):
        self.store: dict = {}
        self.explode = explode

    def set(self, key, value, ex=None):
        if self.explode:
            raise RuntimeError("redis down")
        self.store[key] = (value, ex)


def test_heartbeat_ok_and_best_effort():
    client = _FakeRedis()
    assert heartbeat("equity_settle", redis_client=client) is True
    key = heartbeat_key("equity_settle")
    assert key in client.store and client.store[key][1] == 150

    bad = _FakeRedis(explode=True)
    assert heartbeat("equity_settle", redis_client=bad) is False  # 不抛出

    assert heartbeat("nonexistent-job", redis_client=client) is False


def test_all_jobs_heartbeat_wired_in_source():
    """各任务的心跳接线源断言——漏接一个即红（防静默漏项）。"""
    celery_src = (_BACKEND / "services/engine/tasks/celery_tasks.py").read_text(
        encoding="utf-8"
    )
    for job_key, rel in _HEARTBEAT_WIRED.items():
        src = (_BACKEND / rel).read_text(encoding="utf-8")
        assert f'_sched_heartbeat("{job_key}")' in src, f"{job_key} 未接心跳（{rel}）"
    for job_key in (
        "auto_inference",
        "news_enrich",
        "news_matcher",
        "strategy_lab_scan",
        "backfill_quality",
        "market_sync_dispatch",
    ):
        assert f'_sched_heartbeat("{job_key}")' in celery_src, f"{job_key} 未接心跳"

    wired = set(_HEARTBEAT_WIRED) | {
        "auto_inference",
        "news_enrich",
        "news_matcher",
        "strategy_lab_scan",
        "backfill_quality",
        "market_sync_dispatch",
    }
    registry = {j.key for j in JOBS if j.heartbeat_ttl is not None}
    assert registry == wired, f"注册表与接线不一致: {registry ^ wired}"


def test_health_c07_uses_registry():
    src = (_BACKEND / "scripts/diagnose/health.py").read_text(encoding="utf-8")
    assert "from backend.shared.scheduler_registry import" in src
    assert "classify_scheduler_status" in src
    assert "heartbeat_key(spec.key)" in src


def test_schedule_ctl_dispatch_covers_rerun_declared_jobs():
    from backend.scripts import schedule_ctl

    declared = {j.key for j in JOBS if j.rerun}
    assert declared <= set(schedule_ctl._RERUN_DISPATCH), "声明了 rerun 的任务必须在分发表中"
    assert set(schedule_ctl._RERUN_DISPATCH) == {
        "sim_eod",
        "auto_inference",
        "dual_book",
        "mirror_shadow",
        "market_sync_dispatch",
        "eval_scores",
        "health_recheck",
    }

    # 未知任务 → 退出码 2（纯函数路径，不触发真实执行）
    assert schedule_ctl.cmd_run("nonexistent", None, False) == 2
    # 不支持重跑的任务 → 退出码 2
    assert schedule_ctl.cmd_run("equity_settle", None, False) == 2
