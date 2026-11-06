"""主机看门狗的内存告急告警（2026-11-05 崩机事故后加）。

为什么值得测：这条告警只在**机器快要撑不住**时响，平时永远是静默的——反向守卫
（不告急时一个字都不许推）比正向更值钱，因为误报会让人把整条通道静音，而那正是
最需要它响的时刻。被测的三条纪律：

* 水位达标 → 不推（也不写冷却状态）；
* 水位告急 → 推一次，之后 ``MEM_ALERT_COOLDOWN_MIN`` 分钟内只留日志；
* 推送失败 → **不写冷却状态**，下一分钟重试（与心跳同一套「宁可重复、不可丢失」）。

跑法：``python3 -m pytest scripts/tests/test_host_watchdog_mem.py -q``
不读真实内存、不推 QQ、不碰 logs/。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import host_watchdog as hw

BJ = timezone(timedelta(hours=8))


@pytest.fixture()
def env(monkeypatch, tmp_path):
    """把水位、进程榜、状态文件、推送通道全部换成可控替身。"""
    state = {"avail": 25.0, "pushed": [], "fail_push": False}

    monkeypatch.setattr(hw, "mem_available_gb", lambda: state["avail"])
    monkeypatch.setattr(hw, "top_rss", lambda limit=5: ["4.6GB run_all.py"])
    monkeypatch.setattr(hw, "MEM_STATE", tmp_path / "host_mem_alert.json")
    monkeypatch.setattr(hw, "MEM_ALERT_GB", 8.0)
    monkeypatch.setattr(hw, "MEM_ALERT_COOLDOWN_MIN", 30)

    class _FakePush:
        @staticmethod
        def notify(title, content):  # noqa: ARG004 - 签名与真实 notify 对齐
            if state["fail_push"]:
                raise RuntimeError("QQ 通道不可用")
            state["pushed"].append(title)

    import sys

    monkeypatch.setitem(sys.modules, "push_notify", _FakePush)
    return state


def _now() -> datetime:
    return datetime(2026, 11, 6, 22, 10, tzinfo=BJ)


def test_silent_when_memory_is_fine(env) -> None:
    """水位正常 → 不推、不落冷却状态（否则真告急时要先等冷却）。"""
    assert hw.memory_pressure_alert(_now(), dry_run=False) is None
    assert env["pushed"] == []
    assert not hw.MEM_STATE.exists()


def test_alerts_once_when_memory_is_low(env) -> None:
    env["avail"] = 5.0

    body = hw.memory_pressure_alert(_now(), dry_run=False)

    assert body is not None
    assert "可用内存 5.0GB" in body
    assert env["pushed"] == ["🟠 主机内存告急"]
    assert hw.MEM_STATE.exists(), "推送成功必须落冷却时间"


def test_cooldown_suppresses_repeat_within_window(env, capsys) -> None:
    """同一次内存风暴里每分钟推一条 = 让人把通道静音。冷却期内只留日志。"""
    env["avail"] = 5.0
    hw.memory_pressure_alert(_now(), dry_run=False)

    second = hw.memory_pressure_alert(_now() + timedelta(minutes=5), dry_run=False)

    assert second is None
    assert env["pushed"] == ["🟠 主机内存告急"], "冷却期内不得重复推"
    assert "冷却中不重复推" in capsys.readouterr().out


def test_alert_resumes_after_cooldown(env) -> None:
    env["avail"] = 5.0
    hw.memory_pressure_alert(_now(), dry_run=False)

    again = hw.memory_pressure_alert(_now() + timedelta(minutes=31), dry_run=False)

    assert again is not None
    assert len(env["pushed"]) == 2


def test_push_failure_does_not_start_cooldown(env, capsys) -> None:
    """推不出去就不许进冷却——否则这条告警会静默地永远不再响。"""
    env["avail"] = 5.0
    env["fail_push"] = True

    assert hw.memory_pressure_alert(_now(), dry_run=False) is None
    assert not hw.MEM_STATE.exists(), "推送失败不得写冷却状态，下一分钟要能重试"
    assert "下分钟重试" in capsys.readouterr().err

    env["fail_push"] = False
    assert hw.memory_pressure_alert(_now(), dry_run=False) is not None


def test_dry_run_never_pushes_or_writes(env) -> None:
    env["avail"] = 5.0

    body = hw.memory_pressure_alert(_now(), dry_run=True)

    assert body is not None and "占用前 5" in body
    assert env["pushed"] == []
    assert not hw.MEM_STATE.exists()


def test_unreadable_meminfo_is_silent(env, monkeypatch) -> None:
    """读不到 /proc/meminfo 就不猜——宁可不报，也不能凭空喊内存告急。"""
    monkeypatch.setattr(hw, "mem_available_gb", lambda: None)
    assert hw.memory_pressure_alert(_now(), dry_run=False) is None
    assert env["pushed"] == []
