"""配额告警 doc_alerts（T-FM-14）—— 余量告急发一次，且绝不弄断主链。

钉住四条：

1. **非告急不发**（warning=False / status=None）——连去重锁都不碰；
2. **按配额日去重**：同一天第二次结算拿到锁即跳过，不随结算刷屏；
3. **发送失败释放锁**（下个结算点重试）并返回 False，不上抛；
4. **去重锁故障只记日志**（宁可漏发，不许把 Redis 抖动变成解析链异常）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.engine.alpha_agent.doc_alerts import (  # noqa: E402
    ALERT_LOCK_TTL_S,
    maybe_alert_quota_low,
)
from backend.services.engine.alpha_agent.doc_quota import QuotaStatus  # noqa: E402


def mk_status(*, warning: bool) -> QuotaStatus:
    return QuotaStatus(
        day="20261009",
        user_id="u-1",
        user_used=30,
        user_limit=200,
        platform_used=950,
        platform_budget=1000,
        user_remaining=170,
        platform_remaining=50,
        exhausted=False,
        warning=warning,
    )


class FakeLockQuota:
    """SET NX EX 替身：第一次授予，之后拿不到（模拟同日重复结算）。"""

    def __init__(self, *, grant: bool = True, raise_on_lock: bool = False) -> None:
        self.locks: list[tuple[str, int]] = []
        self.unlocks: list[str] = []
        self.grant = grant
        self.raise_on_lock = raise_on_lock

    def try_lock(self, name, *, ttl_s):
        if self.raise_on_lock:
            raise RuntimeError("redis down")
        self.locks.append((name, ttl_s))
        if self.grant:
            self.grant = False  # 同配额日第二次不再授予
            return True
        return False

    def unlock(self, name):
        self.unlocks.append(name)


def mk_publisher(calls: list, *, error: Exception | None = None):
    def send(**kwargs):
        calls.append(kwargs)
        if error is not None:
            raise error
        return (2, 2)

    return send


def test_not_warning_no_alert_and_lock_untouched() -> None:
    quota, calls = FakeLockQuota(), []
    assert (
        maybe_alert_quota_low(
            mk_status(warning=False), quota=quota, publisher=mk_publisher(calls)
        )
        is False
    )
    assert quota.locks == [] and calls == []


def test_status_none_no_alert() -> None:
    assert maybe_alert_quota_low(None) is False


def test_warning_alerts_once_per_quota_day() -> None:
    quota, calls = FakeLockQuota(), []
    pub = mk_publisher(calls)
    st = mk_status(warning=True)

    assert maybe_alert_quota_low(st, quota=quota, publisher=pub) is True
    assert quota.locks == [("doc_quota_alert:20261009", ALERT_LOCK_TTL_S)]
    assert len(calls) == 1
    kw = calls[0]
    assert kw["type"] == "system" and kw["level"] == "warning"
    for num in ("950", "1000", "50"):
        assert num in kw["content"], f"告警正文要带平台账目数字（缺 {num}）"

    # 同日第二次结算：锁已占 → 不再发
    assert maybe_alert_quota_low(st, quota=quota, publisher=pub) is False
    assert len(calls) == 1


def test_publish_failure_releases_lock_for_retry() -> None:
    quota, calls = FakeLockQuota(), []
    pub = mk_publisher(calls, error=RuntimeError("db down"))

    assert (
        maybe_alert_quota_low(mk_status(warning=True), quota=quota, publisher=pub)
        is False
    )
    assert quota.unlocks == ["doc_quota_alert:20261009"], "发送失败要放锁供重试"


def test_lock_trouble_is_swallowed() -> None:
    quota = FakeLockQuota(raise_on_lock=True)
    calls = []
    assert (
        maybe_alert_quota_low(
            mk_status(warning=True), quota=quota, publisher=mk_publisher(calls)
        )
        is False
    )
    assert calls == [], "取锁都失败了不许再发"
