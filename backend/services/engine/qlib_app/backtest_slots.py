"""回测名额判断。不依赖 Celery，供入队前和单测共用。"""
# OSS port: 与企业版逐字一致。

from __future__ import annotations


def decide_backtest_admission(
    user_running: int,
    global_running: int,
    *,
    per_user: int,
    global_slots: int,
) -> tuple[bool, int, int, str]:
    """返回 (allowed, running, limit, scope)。scope 为 user 或 global。"""
    if per_user > 0 and user_running >= per_user:
        return False, user_running, per_user, "user"
    if global_slots > 0 and global_running >= global_slots:
        return False, global_running, global_slots, "global"
    return True, user_running, per_user, ""
