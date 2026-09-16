"""P6 验收报告工具（T-P6-02/04/05/07 汇总仪器）纯函数测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

_CST = timezone(timedelta(hours=8))


@pytest.mark.unit
def test_is_trading_now_sessions():
    from backend.scripts.p6_acceptance_report import is_trading_now

    def t(day: str, hhmm: str) -> datetime:
        return datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M").replace(tzinfo=_CST)

    assert is_trading_now(t("2026-09-17", "10:00")) is True   # 周四上午
    assert is_trading_now(t("2026-09-17", "13:30")) is True   # 下午
    assert is_trading_now(t("2026-09-17", "12:00")) is False  # 午休
    assert is_trading_now(t("2026-09-17", "09:29")) is False
    assert is_trading_now(t("2026-09-17", "15:01")) is False
    assert is_trading_now(t("2026-09-19", "10:00")) is False  # 周六


@pytest.mark.unit
def test_interval_p95():
    from backend.scripts.p6_acceptance_report import interval_p95

    assert interval_p95([1.0, 2.0]) is None  # 样本不足
    # 3s 节拍 100 帧 + 一个 60s 缺口 → P95 仍在 3s 档
    scores = [1000.0 + 3 * i for i in range(60)] + [1180.0 + 3 * i for i in range(60)]
    p95 = interval_p95(scores)
    assert p95 == pytest.approx(3.0, abs=0.01)
    # 半数是 60s 缺口 → P95 抬升
    scores = [0, 3, 6, 66, 126, 186, 246, 306]
    assert interval_p95(scores) >= 60.0


@pytest.mark.unit
def test_acceptance_grades():
    from backend.scripts.p6_acceptance_report import (
        grade_cadence,
        grade_landing_rate,
        grade_latency,
        grade_shards,
    )

    assert grade_landing_rate(0.95, trading=True)[0] == "OK"
    assert grade_landing_rate(0.5, trading=True)[0] == "FAIL"
    assert grade_landing_rate(0.5, trading=False)[0] == "N/A"  # 闭市不假绿也不误杀
    assert grade_latency({"samples": 100, "p95_ms": 800}, trading=True)[0] == "OK"
    assert grade_latency({"samples": 100, "p95_ms": 2500}, trading=True)[0] == "FAIL"
    assert grade_latency(None, trading=False)[0] == "N/A"
    assert grade_latency(None, trading=True)[0] == "WARN"
    assert grade_cadence(5.0, trading=True)[0] == "OK"
    assert grade_cadence(300.0, trading=True)[0] == "FAIL"
    assert grade_cadence(None, trading=False)[0] == "N/A"
    assert grade_shards({"worker": "up", "shards_up": "6/6"})[0] == "OK"
    assert grade_shards({"worker": "degraded", "shards_up": "5/6"})[0] == "WARN"
    assert grade_shards({"worker": "down", "shards_up": "0/6"})[0] == "FAIL"
    assert grade_shards(None)[0] == "FAIL"
