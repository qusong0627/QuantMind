"""MinerU 页数配额守卫（T-FM-12；2026-10-09 安全审查 H1/M1 改版）。

MinerU 的额度是**平台账号级**共享资源（1000 页/日），烧穿一次全平台当天都
解析不了。这里锁五件事：

1. **先预留后提交（原子）**：reserve 先 INCR 再判，超限即回滚——旧实现
   「先读后判」的 check-then-act 在并发下 N 个请求都能通过，真实页数延迟
   入账就能一天打穿平台预算。
2. **结算多退少补**：settle 把保守预留替换成 MinerU 实际报告的页数；预留键
   GET+DEL 一次性取走（并发重复结算只有一方生效）。
3. **失败全额退**：release 退回预留——MinerU 没交付产物就不记用户的账。
4. **跨日换键**：结算按**预留当天**的键找账（跨午夜完成不记错日子）。
5. **频控与在途锁**（M1）：上传/整理按用户小时限流；单文档整理去重。

Redis 用内存替身（StubRedis），不碰真 Redis。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.engine.alpha_agent.doc_quota import (  # noqa: E402
    DEFAULT_DAILY_BUDGET,
    DEFAULT_USER_DAILY_PAGES,
    DocQuota,
    QuotaExceeded,
    RateLimited,
    quota_day,
)

CST = timezone(timedelta(hours=8))


class StubRedis:
    """内存版 INCRBY/DECRBY/EXPIRE/GET/SET NX/DELETE——够配额模块用，也看得见每次调用。"""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.expires: dict[str, int] = {}
        self.calls: list[tuple] = []

    def incr(self, key: str) -> int:
        self.calls.append(("incr", key))
        value = int(self.data.get(key, "0")) + 1
        self.data[key] = str(value)
        return value

    def incrby(self, key: str, amount: int) -> int:
        self.calls.append(("incrby", key, amount))
        value = int(self.data.get(key, "0")) + amount
        self.data[key] = str(value)
        return value

    def decrby(self, key: str, amount: int) -> int:
        self.calls.append(("decrby", key, amount))
        value = int(self.data.get(key, "0")) - amount
        self.data[key] = str(value)
        return value

    def expire(self, key: str, ttl: int) -> bool:
        self.calls.append(("expire", key, ttl))
        self.expires[key] = ttl
        return True

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def set(self, key: str, value, *, nx: bool = False, ex: int | None = None):
        self.calls.append(("set", key, value, nx, ex))
        if nx and key in self.data:
            return None
        self.data[key] = str(value)
        if ex is not None:
            self.expires[key] = ex
        return True

    def delete(self, key: str) -> int:
        self.calls.append(("delete", key))
        if key in self.data:
            del self.data[key]
            return 1
        return 0


def mk_quota(
    redis: StubRedis | None = None,
    *,
    now: datetime | None = None,
    user_limit: int = 200,
    budget: int = 1000,
    upload_per_hour: int = 30,
    organize_per_hour: int = 60,
) -> DocQuota:
    r = redis or StubRedis()
    q = DocQuota(
        redis_client=r,
        user_daily_pages=user_limit,
        daily_budget=budget,
        upload_per_hour=upload_per_hour,
        organize_per_hour=organize_per_hour,
        now=(lambda: now) if now else None,
    )
    return q


FIXED = datetime(2026, 10, 9, 15, 0, tzinfo=CST)
DAY = "20261009"
UKEY = f"qm:docmining:pages:{DAY}:u1"
PKEY = f"qm:docmining:pages:{DAY}"


# ── 日界 ────────────────────────────────────────────────────────────


def test_quota_day_uses_beijing_midnight() -> None:
    """北京时间 00:00 换键：UTC 23:30（=北京次日 07:30）必须算北京这天。"""
    utc_late = datetime(2026, 10, 8, 23, 30, tzinfo=timezone.utc)  # 北京 10-09 07:30
    assert quota_day(utc_late) == "20261009"

    beijing_2359 = datetime(2026, 10, 9, 23, 59, tzinfo=CST)
    assert quota_day(beijing_2359) == "20261009"

    beijing_0001 = datetime(2026, 10, 10, 0, 1, tzinfo=CST)
    assert quota_day(beijing_0001) == "20261010"


def test_cross_day_resets_accounting() -> None:
    """跨日 = 换键：昨天的用量绝不带到今天。"""
    today = FIXED
    q = mk_quota(now=today)
    q.settle("seed", "u1", 150)
    assert q.status("u1").user_used == 150

    q2 = mk_quota(redis=q._redis, now=today + timedelta(days=1))
    st = q2.status("u1")
    assert st.user_used == 0
    assert st.platform_used == 0
    assert st.day != q.status("u1").day


# ── 原子预留（H1） ──────────────────────────────────────────────────


def test_reserve_writes_both_scopes_and_reservation_key() -> None:
    r = StubRedis()
    q = mk_quota(r, now=FIXED)
    st = q.reserve("u1", 150, doc_id="d1")
    assert r.data[UKEY] == "150" and r.data[PKEY] == "150"
    assert r.data["qm:docmining:reserve:d1"] == f"{DAY}:150", "预留值记原始配额日"
    assert r.expires["qm:docmining:reserve:d1"] > 0
    assert st.user_remaining == 50 and st.platform_remaining == 850


def test_reserve_user_limit_rolls_back_completely() -> None:
    """拦下时连同 INCR 一起回滚：不许留半笔账（旧实现「先写后回滚」的真实超支）。"""
    r = StubRedis()
    q = mk_quota(r, now=FIXED, user_limit=200)
    q.settle("seed", "u1", 150)

    with pytest.raises(QuotaExceeded) as ei:
        q.reserve("u1", 100, doc_id="d1")
    assert ei.value.scope == "user"
    assert "剩余额度" in str(ei.value) and "50" in str(ei.value)
    assert r.data[UKEY] == "150", "拦下后用户账必须回到 150（不许 250）"
    assert r.data[PKEY] == "150"
    assert "qm:docmining:reserve:d1" not in r.data


def test_reserve_platform_limit_rolls_back_both_sides() -> None:
    r = StubRedis()
    q = mk_quota(r, now=FIXED, user_limit=200, budget=300)
    q.settle("seed", "other-user", 250)  # 平台预算被吃到大半

    with pytest.raises(QuotaExceeded) as ei:
        q.reserve("u1", 100, doc_id="d1")
    assert ei.value.scope == "platform"
    assert r.data[PKEY] == "250", "平台侧回滚"
    assert r.data.get(UKEY, "0") == "0", "用户侧也要回滚（先加后撤）"
    assert "qm:docmining:reserve:d1" not in r.data


def test_reserve_concurrent_second_one_is_blocked() -> None:
    """并发窗口：两个 150 页请求打 200 页的用户限，第二个必须被原子回滚拦下。"""
    r = StubRedis()
    q = mk_quota(r, now=FIXED, user_limit=200)
    q.reserve("u1", 150, doc_id="d1")

    with pytest.raises(QuotaExceeded):
        q.reserve("u1", 150, doc_id="d2")
    assert r.data[UKEY] == "150", "被拦下的并发请求不许把账面顶到 300"
    assert "qm:docmining:reserve:d2" not in r.data


def test_reserve_zero_pages_reserves_one() -> None:
    """pages<1 一律按 1 预留：零预留等于零闸。"""
    r = StubRedis()
    q = mk_quota(r, now=FIXED)
    q.reserve("u1", 0, doc_id="d1")
    assert r.data[UKEY] == "1"


def test_reserve_exhausted_message_says_exhausted() -> None:
    q = mk_quota(now=FIXED, user_limit=10)
    q.settle("seed", "u1", 10)
    with pytest.raises(QuotaExceeded) as ei:
        q.reserve("u1", 1, doc_id="d1")
    assert "已用尽" in str(ei.value)


# ── 结算（多退少补） ────────────────────────────────────────────────


def test_settle_replaces_reservation_with_actual() -> None:
    r = StubRedis()
    q = mk_quota(r, now=FIXED)
    q.reserve("u1", 200, doc_id="d1")  # 保守预留（数不出页数的 PDF）
    st = q.settle("d1", "u1", 3)  # MinerU 实际 3 页
    assert st.user_used == 3, "多留的 197 页必须退回去"
    assert r.data[PKEY] == "3"
    assert "qm:docmining:reserve:d1" not in r.data, "预留键一次性取走"


def test_settle_over_report_still_recorded() -> None:
    """实际大于预留（预留时数错页）也如实记：MinerU 真扣了，账必须是真的。"""
    q = mk_quota(now=FIXED, user_limit=10)
    q.reserve("u1", 5, doc_id="d1")
    st = q.settle("d1", "u1", 12)
    assert st.user_used == 12
    assert st.user_remaining == 0, "余量钳到 0，不显示负数"


def test_settle_double_call_only_bumps_once_per_reservation() -> None:
    """预留键只被取走一次：重复释放不再动账（删除与轮询竞态的兜底）。"""
    r = StubRedis()
    q = mk_quota(r, now=FIXED)
    q.reserve("u1", 200, doc_id="d1")
    q.settle("d1", "u1", 3)
    q.release("d1", "u1")  # 删除端点的“再退一次”是 no-op
    assert r.data[UKEY] == "3"


def test_settle_without_reservation_records_actual() -> None:
    """没预留过（重启前在途行/预留过期）仍如实补记实际页数。"""
    r = StubRedis()
    q = mk_quota(r, now=FIXED)
    q.settle("d-legacy", "u1", 42)
    assert r.data[UKEY] == "42"


def test_settle_zero_without_reservation_is_noop() -> None:
    r = StubRedis()
    q = mk_quota(r, now=FIXED)
    q.settle("d-legacy", "u1", 0)
    assert r.data == {}, "没消耗就不该产生键（凭空造键=凭空造账）"


def test_settle_after_midnight_books_against_reservation_day() -> None:
    """跨午夜结算：按**预留当天**的键找账，不记到新的一天。"""
    r = StubRedis()
    day1 = mk_quota(r, now=FIXED)
    day1.reserve("u1", 200, doc_id="d1")

    next_fixed = FIXED + timedelta(days=1)
    day2 = mk_quota(redis=r, now=next_fixed)
    day2.settle("d1", "u1", 5)

    day2_key = f"qm:docmining:pages:{quota_day(next_fixed)}:u1"
    assert r.data[UKEY] == "5", "差额记回预留当天（200 → 5）"
    assert day2_key not in r.data, "今天不许凭空多出记账"


def test_settle_missing_value_tolerates_garbage_pages() -> None:
    """预留键页数段损坏：预留按 0 处理，实际照记——坏键不许吞账。"""
    r = StubRedis()
    q = mk_quota(r, now=FIXED)
    r.data["qm:docmining:reserve:d1"] = f"{DAY}:not-a-number"
    st = q.settle("d1", "u1", 7)
    assert st.user_used == 7
    assert "qm:docmining:reserve:d1" not in r.data


# ── 释放（失败/删除全额退） ─────────────────────────────────────────


def test_release_refunds_reservation() -> None:
    r = StubRedis()
    q = mk_quota(r, now=FIXED)
    q.reserve("u1", 200, doc_id="d1")
    st = q.release("d1", "u1")
    assert st.user_used == 0 and st.platform_used == 0
    assert r.data.get(UKEY, "0") == "0"


def test_release_without_reservation_is_noop() -> None:
    r = StubRedis()
    q = mk_quota(r, now=FIXED)
    q.release("d-none", "u1")
    assert r.data == {}


# ── 频控与在途锁（M1） ──────────────────────────────────────────────


def test_check_rate_blocks_beyond_hourly_limit() -> None:
    q = mk_quota(now=FIXED, upload_per_hour=2)
    q.check_rate("u1", "upload")
    q.check_rate("u1", "upload")
    with pytest.raises(RateLimited) as ei:
        q.check_rate("u1", "upload")
    assert ei.value.scope == "upload" and ei.value.limit == 2


def test_check_rate_resets_with_new_hour_bucket() -> None:
    q = mk_quota(now=FIXED, upload_per_hour=1)
    q.check_rate("u1", "upload")
    with pytest.raises(RateLimited):
        q.check_rate("u1", "upload")
    q2 = mk_quota(redis=q._redis, now=FIXED + timedelta(hours=1), upload_per_hour=1)
    q2.check_rate("u1", "upload")  # 新小时桶：不拦


def test_check_rate_zero_limit_disables() -> None:
    q = mk_quota(now=FIXED, organize_per_hour=0)
    for _ in range(5):
        q.check_rate("u1", "organize")  # 显式关闭＝不限流


def test_try_lock_dedupes_and_unlock_frees() -> None:
    q = mk_quota(now=FIXED)
    token = q.try_lock("organize:d1", ttl_s=60)
    assert isinstance(token, str) and token, "拿到锁要返回令牌（供 CAD 释放）"
    assert q.try_lock("organize:d1", ttl_s=60) is None, "在途同名锁拿不到"
    q.unlock("organize:d1", token)
    assert q.try_lock("organize:d1", ttl_s=60)


def test_unlock_with_stale_token_does_not_delete_new_lock() -> None:
    """ABA 防护：锁过期后被后来者抢走，前持有者的旧令牌不许误删新锁。"""
    q = mk_quota(now=FIXED)
    stale = q.try_lock("organize:d1", ttl_s=60)
    q.unlock("organize:d1", stale)  # 正常释放
    fresh = q.try_lock("organize:d1", ttl_s=60)
    assert fresh and fresh != stale
    q.unlock("organize:d1", stale)  # 旧令牌：值不符，不动
    assert q.try_lock("organize:d1", ttl_s=60) is None, "后来者的锁必须还在"


def test_unlock_decodes_bytes_lock_value() -> None:
    """真 Redis 默认返回 bytes：令牌比对要先解码，否则 CAD 永远不命中。"""
    r = StubRedis()
    q = mk_quota(r, now=FIXED)
    token = q.try_lock("organize:d1", ttl_s=60)
    key = "qm:docmining:lock:organize:d1"
    r.data[key] = token.encode()
    q.unlock("organize:d1", token)
    assert key not in r.data


def test_unlock_without_token_is_unconditional() -> None:
    """遗留调用方（不带令牌）：无条件删除，兼容旧行为。"""
    q = mk_quota(now=FIXED)
    assert q.try_lock("organize:d1", ttl_s=60)
    q.unlock("organize:d1")
    assert q.try_lock("organize:d1", ttl_s=60)


# ── 预留转已用（commit） ────────────────────────────────────────────


def test_commit_reservation_keeps_pages_on_books() -> None:
    """页数不可知但已计费（MinerU 已拿到文件）：预留转已用，不许全退。"""
    r = StubRedis()
    q = mk_quota(r, now=FIXED)
    q.reserve("u1", 200, doc_id="d1")
    q.commit_reservation("d1")
    assert r.data[UKEY] == "200", "预留额留在账上（费用已发生）"
    assert "qm:docmining:reserve:d1" not in r.data, "预留键取走，防后续 release 全退"
    q.release("d1", "u1")  # 幂等：键已不在，不再动账
    assert r.data[UKEY] == "200"


def test_commit_reservation_without_reservation_is_noop() -> None:
    r = StubRedis()
    q = mk_quota(r, now=FIXED)
    q.commit_reservation("d-none")
    assert r.data == {}, "没预留过就没有账可转"


# ── 状态与预警 ──────────────────────────────────────────────────────


def test_status_shape_and_warning() -> None:
    q = mk_quota(now=FIXED, budget=1000)
    q.settle("seed", "u1", 950)
    st = q.status("u1")
    assert st.day == DAY
    assert st.user_limit == 200 and st.platform_budget == 1000
    assert st.exhausted is True, "用户余量 0（200-950 钳 0）→ 已耗尽"
    assert st.warning is True

    q2 = mk_quota(now=FIXED)
    st2 = q2.status("u1")
    assert st2.exhausted is False
    assert st2.warning is False


def test_status_tolerates_non_numeric_redis_value() -> None:
    """Redis 里被别的写者塞了非数字：视为 0 而不是把整个端点炸掉。"""
    r = StubRedis()
    q = mk_quota(r, now=FIXED)
    r.data[UKEY] = "garbage"
    st = q.status("u1")
    assert st.user_used == 0


def test_env_defaults_are_read_at_construction() -> None:
    q = mk_quota(now=FIXED)
    assert q.user_limit == DEFAULT_USER_DAILY_PAGES == 200
    assert q.budget == DEFAULT_DAILY_BUDGET == 1000


def test_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("MINERU_USER_DAILY_PAGES", "50")
    monkeypatch.setenv("MINERU_DAILY_PAGE_BUDGET", "500")
    q = DocQuota(redis_client=StubRedis(), now=lambda: FIXED)
    assert q.user_limit == 50
    assert q.budget == 500

    monkeypatch.setenv("MINERU_USER_DAILY_PAGES", "not-a-number")
    q2 = DocQuota(redis_client=StubRedis(), now=lambda: FIXED)
    assert q2.user_limit == 200, "脏 env 回落默认值，不许 import 期炸"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
