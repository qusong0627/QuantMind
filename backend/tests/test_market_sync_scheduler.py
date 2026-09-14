"""市场定时同步调度器测试。

覆盖 _normalize 的市场建议时间预填（MARKET_SUGGESTED_TIMES）、未配置时保持
关闭、以及显式保存配置的覆盖行为，Redis 用桩对象替代。
"""

from __future__ import annotations

import pytest

from backend.services.engine.tasks.market_sync_scheduler import (
    DEFAULT_SCHEDULE,
    MARKET_SUGGESTED_TIMES,
    MARKETS,
    get_schedule,
    save_schedule,
)


class _StubRedis:
    """最小 Redis 桩：仅 get/set/exists，单测不依赖真实 Redis。"""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self._data.get(key)

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._data[key] = value

    def exists(self, key: str) -> bool:
        return key in self._data


@pytest.fixture()
def stub_redis(monkeypatch: pytest.MonkeyPatch) -> _StubRedis:
    stub = _StubRedis()
    monkeypatch.setattr(
        "backend.services.engine.tasks.market_sync_scheduler._redis", lambda: stub
    )
    return stub


def test_no_market_is_enabled_without_user_config(stub_redis):
    # Act：Redis 里没有任何配置时逐一读取所有市场
    got = {m: get_schedule(m) for m in MARKETS}

    # Assert：所有市场一律保持关闭，避免所有部署在同一固定时刻全量同步
    assert all(cfg["enabled"] is False for cfg in got.values()), got


def test_market_suggested_time_is_prefilled_without_enabling(stub_redis):
    # Act：Redis 里没有任何 HK 配置时读取
    cfg = get_schedule("HK")

    # Assert：建议时间只作预填，enabled 仍为 False；其余字段沿用全局默认
    assert cfg["enabled"] is False
    assert cfg["time"] == MARKET_SUGGESTED_TIMES["HK"]
    assert cfg["days"] == DEFAULT_SCHEDULE["days"]
    assert cfg["datasets"] == []


def test_suggested_times_are_staggered_and_after_midnight():
    # CUSTOM 是本地数据集重建（不请求上游），允许与 FUTURES 同时刻；
    # 其余市场均会请求上游数据源，必须错峰。
    upstream_times = [t for m, t in MARKET_SUGGESTED_TIMES.items() if m != "CUSTOM"]
    assert len(upstream_times) == len(set(upstream_times)), (
        "上游市场建议触发时间必须错开"
    )
    assert all("00:00" <= t <= "06:00" for t in MARKET_SUGGESTED_TIMES.values())


def test_custom_rebuild_is_suggested_after_ashare_sync():
    # Assert：自定义数据集重建建议在 A 股同步（01:00 建议 / 实配 00:55）之后，
    # 否则合并时源数据还没落盘
    assert MARKET_SUGGESTED_TIMES["CUSTOM"] == "03:00"
    assert MARKET_SUGGESTED_TIMES["CUSTOM"] > MARKET_SUGGESTED_TIMES["A"]


def test_custom_market_is_registered(stub_redis):
    # Assert：CUSTOM 在调度市场注册表内，且默认保持关闭（与其他市场一致）
    assert "CUSTOM" in MARKETS
    assert MARKETS["CUSTOM"]
    assert get_schedule("CUSTOM")["enabled"] is False


def test_run_market_sync_custom_dispatches_dataset_rebuild(monkeypatch):
    import backend.scripts.build_factor_custom_dataset as bfcd

    calls: list[dict] = []

    def fake_rebuild(**kwargs):
        calls.append(kwargs)
        return {"written": 2, "mode": "incremental"}

    monkeypatch.setattr(bfcd, "rebuild", fake_rebuild)

    from backend.services.engine.tasks.market_sync_scheduler import run_market_sync

    # Act
    result = run_market_sync("CUSTOM", {"enabled": True, "time": "03:00"})

    # Assert：走数据集重建而非上游同步，参数取默认（窗口起点 / 覆盖率）
    assert result["market"] == "CUSTOM"
    assert result["result"] == {"written": 2, "mode": "incremental"}
    assert len(calls) == 1
    assert calls[0]["start"] == bfcd.DEFAULT_START
    assert calls[0]["min_coverage"] == bfcd.DEFAULT_MIN_COVERAGE


def test_dispatch_fires_custom_once_per_day(stub_redis, monkeypatch):
    import sys
    import types
    from datetime import datetime as _dt

    import backend.services.engine.tasks.market_sync_scheduler as ms

    sent: list[tuple] = []
    fake_celery = types.SimpleNamespace(
        send_task=lambda name, args=None, queue=None: sent.append((name, args, queue))
    )
    monkeypatch.setitem(
        sys.modules,
        "backend.services.engine.qlib_app.celery_config",
        types.SimpleNamespace(celery_app=fake_celery),
    )

    class _FrozenDateTime(_dt):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 15, 3, 0, 0)

    monkeypatch.setattr(ms, "datetime", _FrozenDateTime)

    # Arrange：CUSTOM 配置为每天 03:00
    save_schedule("CUSTOM", {"enabled": True, "time": "03:00"})

    # Act：到点派发一次；同日再触发一次
    first = ms.dispatch_due_syncs()
    second = ms.dispatch_due_syncs()

    # Assert：只派发 CUSTOM，任务名/队列正确；同日不重复派发
    assert first["dispatched"] == ["CUSTOM"]
    assert second["dispatched"] == []
    assert len(sent) == 1
    name, args, queue = sent[0]
    # CUSTOM 走独立的数据集重建任务（本地作业，超时预算放宽）
    assert name == "engine.tasks.run_custom_dataset_rebuild"
    assert args[0] == "CUSTOM"
    assert queue == "qlib_backtest_srv"


def test_task_name_for_routes_custom_to_rebuild_task():
    from backend.services.engine.tasks.market_sync_scheduler import task_name_for

    # Assert：CUSTOM 走数据集重建任务；上游市场走常规同步任务
    assert task_name_for("CUSTOM") == "engine.tasks.run_custom_dataset_rebuild"
    assert task_name_for("A") == "engine.tasks.run_market_scheduled_sync"
    assert task_name_for("HK") == "engine.tasks.run_market_scheduled_sync"


def test_ashare_stays_disabled_without_config(stub_redis):
    # Act：A 股未配置
    cfg = get_schedule("A")

    # Assert：保持关闭，时间取 A 股建议值
    assert cfg["enabled"] is False
    assert cfg["time"] == MARKET_SUGGESTED_TIMES["A"]


def test_user_can_enable_market_explicitly(stub_redis):
    # Arrange：用户在前端显式开启港股定时
    save_schedule("HK", {"enabled": True, "time": "22:30"})

    # Act
    cfg = get_schedule("HK")

    # Assert：以用户保存的配置为准
    assert cfg["enabled"] is True
    assert cfg["time"] == "22:30"


def test_explicit_saved_config_disables_market(stub_redis):
    # Arrange：用户在前端显式关闭港股定时
    save_schedule("HK", {"enabled": False})

    # Act
    cfg = get_schedule("HK")

    # Assert：显式关闭生效；未覆盖字段沿用建议时间预填
    assert cfg["enabled"] is False
    assert cfg["time"] == MARKET_SUGGESTED_TIMES["HK"]


def test_save_and_get_roundtrip_keeps_fields_not_set_by_caller(stub_redis):
    # Arrange：只传 enabled/time 的部分配置
    saved = save_schedule("HK", {"enabled": True, "time": "22:30"})

    # Act
    loaded = get_schedule("HK")

    # Assert：保存与读回一致，调用方未传字段沿用默认值
    assert saved == loaded
    assert loaded["time"] == "22:30"
    assert loaded["days"] == DEFAULT_SCHEDULE["days"]


def test_invalid_time_in_stored_config_falls_back_to_global_default(stub_redis):
    # Arrange：绕过 API 层校验，直接把坏时间写进 Redis 配置
    save_schedule("HK", {"time": "25:00"})

    # Act
    cfg = get_schedule("HK")

    # Assert：非法 HH:MM 回退到全局默认时间而不是抛错
    assert cfg["time"] == "03:00"


def test_normalize_of_missing_config_for_unknown_market_uses_global_defaults():
    from backend.services.engine.tasks.market_sync_scheduler import _normalize

    # Act / Assert：未知 market 传入时仅应用全局默认，不抛错
    assert _normalize(None, "XX") == dict(DEFAULT_SCHEDULE)
