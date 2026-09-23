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
    # P6 T-P6-06 新增（热集构建）
    "hot_set_builder": "services/live_trading/services/hot_set_builder.py",
    # T-RC-14 新增（策略控制台守护）：两条关键交易循环此前不在册，
    # 挂掉时 C07 无项可判 → 表现为「体检全绿但策略不再调仓」。
    "sim_hosted": "services/simulation/services/simulation_hosted_scheduler.py",
    "manual_execution": "services/live_trading/services/manual_execution_worker.py",
    # P6 期补登注册表时漏登记进本守卫（守卫因此长期为红——红灯的守卫等于没有守卫）
    "tdx_hot_set_feed": "services/live_trading/services/tdx_hot_set_feed.py",
    "sentinel_push": "services/trade/services/sentinel_alert_service.py",
    "sentinel_backfill": "services/trade/services/sentinel_backfill.py",
    "advice_backfill": "services/trade/services/advice_backfill.py",
    "advice_generator": "services/trade/services/advice_generator.py",
    "holding_sentinel": "services/trade/services/holding_sentinel.py",
    # P1.8 生产者（风险档位定档）：档位缺失时闸门按买入侧防守收紧，心跳是
    # "档位到底有没有人在定" 的唯一可观测信号，必须接线。
    "risk_tier": "services/trade/services/risk_tier_producer.py",
    # P2.8 决策轮（真钱生产者）：开关默认关，心跳是「到底有没有人在跑」的唯一信号
    # ——关着时 C07 记 off（不算故障），开了却没心跳就是故障。
    # 心跳写在**驱动层**（runner 的常驻循环里），编排层不含 worker。
    "decision_round": "services/trade/services/decision_round_runner.py",
}

#: 心跳用模块常量（``_sched_heartbeat(SCHEDULER_NAME)``）间接引用的任务：
#: 字面量断言扫不到，改判「常量声明与调用同文件共存」。
_HEARTBEAT_VIA_CONSTANT = {"holding_sentinel": "SCHEDULER_NAME"}


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
        const = _HEARTBEAT_VIA_CONSTANT.get(job_key)
        if const:
            assert f'{const} = "{job_key}"' in src, f"{job_key} 常量名不符（{rel}）"
            assert f"_sched_heartbeat({const})" in src, f"{job_key} 未接心跳（{rel}）"
        else:
            assert f'_sched_heartbeat("{job_key}")' in src, f"{job_key} 未接心跳（{rel}）"
    for job_key in (
        "auto_inference",
        "news_enrich",
        "news_matcher",
        "news_tag_rollup",
        "strategy_lab_scan",
        "backfill_quality",
        "market_sync_dispatch",
    ):
        assert f'_sched_heartbeat("{job_key}")' in celery_src, f"{job_key} 未接心跳"

    wired = set(_HEARTBEAT_WIRED) | {
        "auto_inference",
        "news_enrich",
        "news_matcher",
        "news_tag_rollup",
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
        # T-RC-14 补齐（此前注册表声明了 rerun 却缺分发，守卫长期为红）
        "sentinel_backfill",
        "advice_backfill",
        "advice_generator",
        # P1.8 生产者
        "risk_tier",
        # P2.8 决策轮
        "decision_round",
    }

    # 未知任务 → 退出码 2（纯函数路径，不触发真实执行）
    assert schedule_ctl.cmd_run("nonexistent", None, False) == 2
    # 不支持重跑的任务 → 退出码 2
    assert schedule_ctl.cmd_run("equity_settle", None, False) == 2


def test_force_notice_states_the_real_semantics_per_job():
    """控制台的 ``--force`` 提示必须与任务真实语义一致。

    旧文案是「保留参数（当前无带守卫的任务需要绕过）」——decision_round 落地后那句
    不再是事实：它的 ``--force`` 会抢占槽位**真的再下一批单**。出口含糊在真钱路径上
    等于误导（操作员会以为只是重算一遍）。逐任务断言，防文案回退。
    """
    from backend.scripts import schedule_ctl

    heavy = schedule_ctl._force_notice("decision_round")
    assert "抢占" in heavy and "新订单" in heavy, heavy
    light = schedule_ctl._force_notice("sim_eod")
    assert "新订单" not in light, light
