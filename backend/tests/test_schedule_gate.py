"""到点判据（审计 H4）的单测。

语义：`now >= 配置时刻` 即到点；迟到分钟/小时仍执行（当日去重由各调度器
的 last_run 日键负责，见各自派发测试）。反向守卫：早于配置时刻不得提前跑。
"""

from __future__ import annotations

from datetime import datetime

from backend.services.engine.tasks.schedule_gate import not_due_yet


def _at(hour: int, minute: int) -> datetime:
    return datetime(2026, 10, 10, hour, minute, 30)


def test_before_the_configured_minute_is_not_due() -> None:
    assert not_due_yet(_at(4, 29), "04:30") is True


def test_the_exact_minute_is_due() -> None:
    assert not_due_yet(_at(4, 30), "04:30") is False


def test_late_by_minutes_is_still_due() -> None:
    """H4 核心：worker 忙过 60s 迟到 5 分钟，仍要到点（旧 `==` 判据整天静默跳发）。"""
    assert not_due_yet(_at(4, 35), "04:30") is False


def test_late_by_hours_is_still_due_for_same_day_catch_up() -> None:
    """重启补齐：过点数小时仍是「今天没跑过」的当日首次到点。"""
    assert not_due_yet(_at(9, 10), "04:30") is False


def test_non_padded_time_is_compared_by_value_not_by_string() -> None:
    """非补零写法 "4:30" 是 strptime 合法且 _normalize 原样保留的配置：

    字符串比较下 ``"04:35" < "4:30"`` 为真（'0' < '4'），会把已过点的
    04:35 误判成「未到点」；元组比较按数值判断。
    """
    assert not_due_yet(_at(4, 29), "4:30") is True
    assert not_due_yet(_at(4, 35), "4:30") is False


def test_garbage_time_fails_closed() -> None:
    """解析失败宁可不跑：跑一个看不懂的时刻可能把凌晨任务按白天数据执行。"""
    assert not_due_yet(_at(4, 35), "") is True
    assert not_due_yet(_at(4, 35), "abc") is True
    assert not_due_yet(_at(4, 35), "04-30") is True
