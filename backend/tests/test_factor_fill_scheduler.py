"""因子数据集定时填充调度器的回归测试。

锁两层行为：
1. 派发纪律与市场同步调度同款——**标记在派发成功之后写**（先标记再派发会在
   broker 抖动时留下「今天跑过了」的假记录，当天永不重试）。
2. 「落后才建」判据——因子集追平来源数据必须空跑不重算；来源缺失不得触发
   建造；单数据集失败不拖垮其余数据集。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from backend.services.engine.tasks import factor_fill_scheduler as sched


class _FakeCelery:
    """只记下发过什么，不碰 broker。"""

    def __init__(self, fail_for: set[str] | None = None) -> None:
        self.sent: list[str] = []
        self.fail_for = fail_for or set()

    def send_task(self, name: str, args: list[Any], queue: str) -> None:
        market = args[0]
        if market in self.fail_for:
            raise ConnectionError("broker down")
        self.sent.append(market)


@pytest.fixture()
def dispatch_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """把调度器的配置与标记替换成可控对象（time 取当前真实分钟必然到点）。"""
    marks: set[tuple[str, str]] = set()
    state: dict[str, Any] = {"marks": marks}

    def _fake_get_schedule(_market: str) -> dict[str, Any]:
        return {
            "enabled": True,
            "time": datetime.now().strftime("%H:%M"),
            "datasets": [],
        }

    monkeypatch.setattr(sched, "get_schedule", _fake_get_schedule)
    monkeypatch.setattr(
        sched, "_last_run_today", lambda market, date_str: (market, date_str) in marks
    )
    monkeypatch.setattr(
        sched, "_mark_run", lambda market, date_str: marks.add((market, date_str))
    )
    return state


def _install_celery(monkeypatch: pytest.MonkeyPatch, fake: _FakeCelery) -> None:
    import backend.services.engine.qlib_app.celery_config as cfg

    monkeypatch.setattr(cfg, "celery_app", fake, raising=True)


def test_dispatch_marks_run_after_successful_send(
    monkeypatch: pytest.MonkeyPatch, dispatch_env: dict[str, Any]
) -> None:
    fake = _FakeCelery()
    _install_celery(monkeypatch, fake)
    monkeypatch.setattr(sched, "MARKETS", {"HK": "QuantHK 港股"})

    result = sched.dispatch_due_factor_fills()

    assert fake.sent == ["HK"]
    assert result["dispatched"] == ["HK"]
    assert {m for m, _d in dispatch_env["marks"]} == {"HK"}, "派发成功后必须写标记"


def test_dispatch_failure_leaves_no_marker_so_next_minute_retries(
    monkeypatch: pytest.MonkeyPatch, dispatch_env: dict[str, Any]
) -> None:
    """核心回归：派发抛错时，绝不能留下「今天已跑」的假记录。"""
    fake = _FakeCelery(fail_for={"HK"})
    _install_celery(monkeypatch, fake)
    monkeypatch.setattr(sched, "MARKETS", {"HK": "QuantHK 港股"})

    result = sched.dispatch_due_factor_fills()

    assert result["dispatched"] == []
    assert dispatch_env["marks"] == set(), (
        "派发失败不得写 last_run 标记，否则当天永不重试"
    )


def test_marker_prevents_second_dispatch_same_day(
    monkeypatch: pytest.MonkeyPatch, dispatch_env: dict[str, Any]
) -> None:
    fake = _FakeCelery()
    _install_celery(monkeypatch, fake)
    monkeypatch.setattr(sched, "MARKETS", {"HK": "QuantHK 港股"})

    sched.dispatch_due_factor_fills()
    second = sched.dispatch_due_factor_fills()

    assert second["dispatched"] == []
    assert fake.sent == ["HK"], "同日不得重复派发"


def test_normalize_filters_unknown_dataset_names() -> None:
    cfg = sched._normalize(
        {"enabled": True, "time": "07:30", "datasets": ["l1_factors", "bogus"]}, "US"
    )
    assert cfg["datasets"] == ["l1_factors"]


# ---------------------------------------------------------------------------
# run_factor_fill：「落后才建」判据
# ---------------------------------------------------------------------------


def _fill_env(
    monkeypatch: pytest.MonkeyPatch, latest: dict[str, str | None]
) -> tuple[list[Any], dict[str, Any]]:
    """latest: {相对目录: 最新分区}; 返回 (builder 调用记录, 落盘的状态对象)。"""
    calls: list[Any] = []
    saved: dict[str, Any] = {}

    monkeypatch.setattr(sched, "_data_dir", lambda market: Path("/nonexistent"))
    monkeypatch.setattr(
        sched, "_latest_partition_dt", lambda root, rel: latest.get(rel)
    )
    monkeypatch.setattr(
        sched,
        "_save_status",
        lambda market, result: saved.update({"market": market, "result": result}),
    )
    return calls, saved


_US_L1 = {
    "US": {"l1_factors": ("6_ml_datasets/l1_factors", "1_kline_data/daily_forward")}
}


def test_run_skips_when_up_to_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    latest = {
        "6_ml_datasets/l1_factors": "20261007",
        "1_kline_data/daily_forward": "20261007",
    }
    calls, saved = _fill_env(monkeypatch, latest)
    monkeypatch.setattr(sched, "_fill_dataset", lambda market, name: calls.append(name))
    monkeypatch.setattr(sched, "FACTOR_DATASETS", _US_L1)

    result = sched.run_factor_fill("US", {"datasets": []})

    assert calls == [], "因子集追平来源时不得重算"
    assert result["datasets"]["l1_factors"]["status"] == "up_to_date"
    assert result["status"] == "ok"
    assert saved["market"] == "US", "每次运行都必须落盘状态供前端展示"


def test_run_fills_when_behind(monkeypatch: pytest.MonkeyPatch) -> None:
    latest = {
        "6_ml_datasets/l1_factors": "20260917",
        "1_kline_data/daily_forward": "20261007",
    }
    calls, _ = _fill_env(monkeypatch, latest)

    def _fake_fill(market: str, name: str) -> dict[str, Any]:
        calls.append((market, name))
        latest["6_ml_datasets/l1_factors"] = "20261007"  # 补建后追平来源
        return {"partitions_written": 14}

    monkeypatch.setattr(sched, "_fill_dataset", _fake_fill)
    monkeypatch.setattr(sched, "FACTOR_DATASETS", _US_L1)

    result = sched.run_factor_fill("US", {"datasets": ["l1_factors"]})

    assert calls == [("US", "l1_factors")]
    ds = result["datasets"]["l1_factors"]
    assert ds["status"] == "filled"
    assert ds["latest"] == "20261007"
    assert ds["source_latest"] == "20261007"


def test_run_source_missing_does_not_build(monkeypatch: pytest.MonkeyPatch) -> None:
    latest: dict[str, str | None] = {
        "6_ml_datasets/l1_factors": "20260917",
        "1_kline_data/daily_forward": None,
    }
    calls, _ = _fill_env(monkeypatch, latest)
    monkeypatch.setattr(sched, "_fill_dataset", lambda market, name: calls.append(name))
    monkeypatch.setattr(sched, "FACTOR_DATASETS", _US_L1)

    result = sched.run_factor_fill("US", {"datasets": []})

    assert calls == [], "来源缺失时不得触发建造"
    assert result["datasets"]["l1_factors"]["status"] == "source_missing"


def test_run_one_dataset_error_does_not_block_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    latest = {
        "6_ml_datasets/l1_factors": "20260917",
        "1_kline_data/daily_forward": "20261007",
        "6_ml_datasets/ccass_factors": "20261001",
        "2_base_sector/ccass_top50": "20261007",
    }
    calls, _ = _fill_env(monkeypatch, latest)

    def _fake_fill(market: str, name: str) -> dict[str, Any]:
        calls.append(name)
        if name == "l1_factors":
            raise RuntimeError("boom")
        latest["6_ml_datasets/ccass_factors"] = "20261007"
        return {"status": "ok"}

    monkeypatch.setattr(sched, "_fill_dataset", _fake_fill)
    monkeypatch.setattr(
        sched,
        "FACTOR_DATASETS",
        {
            "HK": {
                "l1_factors": (
                    "6_ml_datasets/l1_factors",
                    "1_kline_data/daily_forward",
                ),
                "ccass_factors": (
                    "6_ml_datasets/ccass_factors",
                    "2_base_sector/ccass_top50",
                ),
            }
        },
    )

    result = sched.run_factor_fill("HK", {"datasets": []})

    assert calls == ["l1_factors", "ccass_factors"], "前一个数据集失败不拖垮后一个"
    assert result["datasets"]["l1_factors"]["status"] == "error"
    assert result["datasets"]["ccass_factors"]["status"] == "filled"
    assert result["status"] == "partial"
