"""T-P6-10 过载治理测试：分级阈值/滞环/回切/异常输入 + 服务降级路径接线。

覆盖：
1. U：p95 分级（0.8× 降载线 → L1；1.2× 重度线 → L2）需连续 3 周期；
2. U：回切滞环（<0.5× 连续 10 周期才回切）；中间带不动级别；
3. D：样本不足不判定；NaN/负数被忽略；
4. I：服务 build_cycle(degrade_level=1) 跳过覆盖且载荷/账本如实标注（绝不静默）。
"""

from __future__ import annotations

import math

import numpy as np
import pytest


def _gov(cadence=10.0, now_fn=None):
    from backend.shared.load_governor import LoadGovernor

    return LoadGovernor(base_cadence_s=cadence, now_fn=now_fn)


@pytest.mark.unit
def test_governor_grading_and_streaks():
    g = _gov(cadence=10.0)  # 预算 10000ms
    # 样本不足（<window//2=10）不判定
    for _ in range(9):
        assert g.record(9000) == 0
    # 连续 3 个超 0.8×（>8000）→ L1
    for _ in range(3):
        g.record(9000)
    assert g.level == 1 and g.skip_optional() is True
    # L1→L2 需超过重度线 1.2×（>12000）连续 3 个
    for _ in range(2):
        g.record(13000)
    assert g.level == 1, "streak 未满不升级"
    g.record(13000)
    assert g.level == 2
    assert g.effective_cadence_s() == pytest.approx(20.0)  # 放慢 ×2
    snap = g.snapshot()
    assert snap["degraded"] is True and snap["degradations"] == 2


@pytest.mark.unit
def test_governor_recovery_hysteresis():
    g = _gov(cadence=10.0)
    for _ in range(12):
        g.record(9000)
    assert g.level == 1
    # 中间带（0.5×~升级线）：不动级别（滞环）
    for _ in range(5):
        g.record(7000)
    assert g.level == 1
    # 持续低载直到回切（窗清空 + 连续低载 streak 满足）
    steps = 0
    while g.level > 0 and steps < 120:
        g.record(4000)
        steps += 1
    assert g.level == 0, f"持续低载未回切（steps={steps}, p95={g.counters['p95_ms']}）"
    assert g.snapshot()["recoveries"] >= 1


@pytest.mark.unit
def test_governor_ignores_bad_samples():
    g = _gov(cadence=10.0)
    n0 = g.counters["cycles"]
    g.record(float("nan"))
    g.record(-5)
    g.record("bad")  # type: ignore[arg-type]
    assert g.counters["cycles"] == n0  # 非法样本不入窗
    assert g.level == 0


@pytest.mark.integration
def test_service_degrade_skips_override(tmp_path):
    """降级路径：L1 跳过 live 覆盖；载荷与账本如实标注 degraded_level。"""
    from backend.services.engine.inference.realtime_service import (
        RealtimeInferConfig,
        RealtimeInferenceService,
    )
    from backend.tests.test_realtime_inference import _fake_engine_inputs, _make_model_dir

    model_dir = _make_model_dir(tmp_path)
    hot, snaps, bundle = _fake_engine_inputs()
    ledger: list[dict] = []
    svc = RealtimeInferenceService(
        config_loader=lambda: RealtimeInferConfig(
            enabled=True, model_dir=str(model_dir), cadence_s=3,
            override_whitelist=("mom_ret_1d",),
        ),
        hot_set_fetcher=lambda: hot,
        snapshot_fetcher=lambda syms: snaps,
        baseline_loader=lambda syms, day: bundle,
        ledger_sink=ledger.append,
    )
    normal = svc.build_cycle(0)
    assert normal["overridden_cells"] > 0
    degraded = svc.build_cycle(1)
    assert degraded["overridden_cells"] == 0, "L1 必须跳过覆盖"
    assert degraded["degraded_level"] == 1
    assert degraded["quality"]["degraded_level"] == 1
    assert ledger[-1]["degraded_level"] == 1
    assert degraded["scores"], "降级只减计算不减数据（快照/发布保留）"
    st = svc.status()
    assert st["resources"]["rss_mb"] is not None
