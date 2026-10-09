"""MinerU 页数配额守卫（T-FM-12）—— 平台账号级共享额度的记账与双闸。

MinerU 免费额度是**账号级 1000 页/日**，一次烧穿全平台当天都不能解析。本模块
是所有页数记账与频控的**唯一入口**，四条纪律（2026-10-09 安全审查 C1/H1/M1
修复后定稿）：

- ``reserve`` **先扣后交（原子）**：提交解析前按保守预估值预留。旧实现
  「先读后判」的 check-then-act 在并发下就是真超支（N 个请求都读到
  remaining>0 全放行）；现在先 INC 再判，超限即回滚——单次 INCRBY 是原子的，
  并发只会瞬态多计、不会真的超放。
- ``settle`` **页数已知后结算**：实际页数与预留的差额多退少补。预留值存
  在每文档键里（``day:pages``），结算按**预留当天**的键找账——跨午夜完成的
  解析不会把账记错日子。取预留用 GET+DEL 且认 DEL 的返回值：并发的重复
  结算只有拿到键的那个生效（幂等）。
- ``release`` **失败全额退**：MinerU 没交付产物就不计用户的账（对用户保守；
  平台侧多花的部分由预留期的保守值兜底）。
- ``check_rate`` / ``try_lock``：上传与整理端点的**按用户限流**与**单文档
  在途去重**（安全审查 M1）——整理一次最多烧 9 次 LLM 调用，同一份文档
  的并发重复整理必须被 409 挡住。

预留按**保守预估**计（非 PDF 数不出页数 = 单文件上限，图片 = 1 页）：
宁可多留不许多放，多留的在结算时退回。日界按**北京时间**换键（平台按
中国时区切日）：TTL 只是垃圾回收，跨日重置靠键里的日期。

Redis 客户端惰性构建（env REDIS_HOST/PORT/DB，与 engine 其它模块同款），
也可注入替身（测试）。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from backend.services.engine.alpha_agent.mineru_client import MineruQuotaError

logger = logging.getLogger(__name__)

ENV_USER_DAILY_PAGES = "MINERU_USER_DAILY_PAGES"
ENV_DAILY_BUDGET = "MINERU_DAILY_PAGE_BUDGET"
ENV_UPLOAD_PER_HOUR = "DOC_UPLOAD_RATE_PER_HOUR"
ENV_ORGANIZE_PER_HOUR = "DOC_ORGANIZE_RATE_PER_HOUR"

DEFAULT_USER_DAILY_PAGES = 200
DEFAULT_DAILY_BUDGET = 1000
DEFAULT_UPLOAD_PER_HOUR = 30
DEFAULT_ORGANIZE_PER_HOUR = 60

KEY_PREFIX = "qm:docmining:pages"
KEY_RESERVE_PREFIX = "qm:docmining:reserve"
KEY_RATE_PREFIX = "qm:docmining:rate"
KEY_LOCK_PREFIX = "qm:docmining:lock"

TTL_S = 48 * 3600  # 只为回收：真正的跨日重置靠键里的日期
TTL_RESERVE_S = 24 * 3600  # 预留键：解析超时上限 2h + 重试，24h 足够兜住
RATE_WINDOW_S = 3600

CST = timezone(timedelta(hours=8))
_WARN_FRACTION = 0.10  # 平台余量低于预算 10% → 预警


def _read_int_env(name: str, default: int) -> int:
    """脏 env 回落默认值：import/构造期绝不炸（配额守卫炸了就是全链 500）。"""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r 不是整数，回落默认 %d", name, raw, default)
        return default
    return value if value > 0 else default


def quota_day(now: datetime | None = None) -> str:
    """配额日（北京时间 YYYYMMDD）。"""
    moment = now or datetime.now(tz=timezone.utc)
    return moment.astimezone(CST).strftime("%Y%m%d")


@dataclass(frozen=True)
class QuotaStatus:
    day: str
    user_id: str
    user_used: int
    user_limit: int
    platform_used: int
    platform_budget: int
    user_remaining: int
    platform_remaining: int
    exhausted: bool
    warning: bool


class QuotaExceeded(MineruQuotaError):
    """守卫拦下（非 MinerU 返回，是我们自己的闸）。scope 说明是哪一侧不足。"""

    def __init__(self, message: str, *, scope: str, status: QuotaStatus) -> None:
        super().__init__(message, code=None, retryable=False)
        self.scope = scope
        self.status = status


class RateLimited(Exception):
    """按用户频控拦下（上传/整理端点）。scope=动作名，limit=窗口内上限。"""

    def __init__(self, message: str, *, scope: str, limit: int, window_s: int) -> None:
        super().__init__(message)
        self.scope = scope
        self.limit = limit
        self.window_s = window_s


def _default_redis_client():
    import redis as redis_lib

    return redis_lib.Redis(
        host=os.getenv("REDIS_HOST") or "redis",
        port=int(os.getenv("REDIS_PORT", "6379")),
        db=int(os.getenv("REDIS_DB", "0")),
        password=os.getenv("REDIS_PASSWORD") or None,
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=5,
    )


class DocQuota:
    def __init__(
        self,
        *,
        redis_client=None,
        user_daily_pages: int | None = None,
        daily_budget: int | None = None,
        upload_per_hour: int | None = None,
        organize_per_hour: int | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._redis = redis_client
        self.user_limit = (
            user_daily_pages
            if user_daily_pages is not None
            else _read_int_env(ENV_USER_DAILY_PAGES, DEFAULT_USER_DAILY_PAGES)
        )
        self.budget = (
            daily_budget
            if daily_budget is not None
            else _read_int_env(ENV_DAILY_BUDGET, DEFAULT_DAILY_BUDGET)
        )
        self.upload_per_hour = (
            upload_per_hour
            if upload_per_hour is not None
            else _read_int_env(ENV_UPLOAD_PER_HOUR, DEFAULT_UPLOAD_PER_HOUR)
        )
        self.organize_per_hour = (
            organize_per_hour
            if organize_per_hour is not None
            else _read_int_env(ENV_ORGANIZE_PER_HOUR, DEFAULT_ORGANIZE_PER_HOUR)
        )
        self._now = now or (lambda: datetime.now(tz=timezone.utc))

    # -- 内部 ----------------------------------------------------------

    def _client(self):
        if self._redis is None:
            self._redis = _default_redis_client()
        return self._redis

    def _keys(self, day: str, user_id: str) -> tuple[str, str]:
        return f"{KEY_PREFIX}:{day}", f"{KEY_PREFIX}:{day}:{user_id}"

    def _reserve_key(self, doc_id: str) -> str:
        return f"{KEY_RESERVE_PREFIX}:{doc_id}"

    @staticmethod
    def _read_used(client, key: str) -> int:
        raw = client.get(key)
        if raw is None:
            return 0
        try:
            return int(raw)
        except (TypeError, ValueError):
            logger.warning("配额键 %s 含非数字值 %r，按 0 处理", key, raw)
            return 0

    def _take_reservation(self, doc_id: str) -> tuple[str, int] | None:
        """取走并清除预留（GET+DEL，认 DEL 返回值 → 并发下只有一方生效）。"""
        client = self._client()
        rkey = self._reserve_key(doc_id)
        raw = client.get(rkey)
        if raw is None:
            return None
        if not client.delete(rkey):
            return None  # 并发的结算/释放已经把它拿走了：本次不再动账
        day, _, pages = str(raw).partition(":")
        try:
            return (day or quota_day(self._now()), max(0, int(pages or 0)))
        except ValueError:
            logger.warning("预留键 %s 含非法值 %r，忽略预留部分", rkey, raw)
            return (day or quota_day(self._now()), 0)

    def _bump(self, day: str, user_id: str, delta: int) -> None:
        if not delta:
            return
        pkey, ukey = self._keys(day, user_id)
        client = self._client()
        client.incrby(pkey, delta)
        client.expire(pkey, TTL_S)
        client.incrby(ukey, delta)
        client.expire(ukey, TTL_S)

    # -- 公开面 --------------------------------------------------------

    def status(self, user_id: str) -> QuotaStatus:
        day = quota_day(self._now())
        pkey, ukey = self._keys(day, user_id)
        client = self._client()
        platform_used = self._read_used(client, pkey)
        user_used = self._read_used(client, ukey)
        user_remaining = max(0, self.user_limit - user_used)
        platform_remaining = max(0, self.budget - platform_used)
        return QuotaStatus(
            day=day,
            user_id=user_id,
            user_used=user_used,
            user_limit=self.user_limit,
            platform_used=platform_used,
            platform_budget=self.budget,
            user_remaining=user_remaining,
            platform_remaining=platform_remaining,
            exhausted=user_remaining <= 0 or platform_remaining <= 0,
            warning=platform_remaining < self.budget * _WARN_FRACTION,
        )

    def reserve(self, user_id: str, pages: int, *, doc_id: str) -> QuotaStatus:
        """原子预留（先 INCR 后判，超限即回滚）。不足即抛 :class:`QuotaExceeded`。

        返回的是**已含预留**的状态（展示面保守：在途的也算已用）。
        """
        pages = max(1, int(pages or 0))
        day = quota_day(self._now())
        pkey, ukey = self._keys(day, user_id)
        client = self._client()

        u_after = int(client.incrby(ukey, pages))
        client.expire(ukey, TTL_S)
        if u_after > self.user_limit:
            client.decrby(ukey, pages)
            st = self.status(user_id)
            if st.user_remaining <= 0:
                message = f"您今日文档解析页数已用尽（{st.user_used}/{st.user_limit} 页，北京时间 {st.day} 重置）"
            else:
                message = (
                    f"本次需要 {pages} 页，超过您今日剩余额度 {st.user_remaining} 页"
                )
            raise QuotaExceeded(message, scope="user", status=st)

        p_after = int(client.incrby(pkey, pages))
        client.expire(pkey, TTL_S)
        if p_after > self.budget:
            client.decrby(pkey, pages)
            client.decrby(ukey, pages)
            st = self.status(user_id)
            if st.platform_remaining <= 0:
                message = f"平台今日解析额度已用尽（{st.platform_used}/{st.platform_budget} 页），请明日再试"
            else:
                message = f"本次需要 {pages} 页，超过平台今日剩余额度 {st.platform_remaining} 页"
            raise QuotaExceeded(message, scope="platform", status=st)

        client.set(self._reserve_key(doc_id), f"{day}:{pages}", ex=TTL_RESERVE_S)
        return self.status(user_id)

    def settle(self, doc_id: str, user_id: str, pages: int) -> QuotaStatus:
        """页数已知后结算：把预留替换成实际值（多退少补）。

        预留被取走/过期（重复结算、复用路径）时：实际页数 > 0 仍如实补记
        （账必须是真的），0 则纯 no-op。
        """
        pages = max(0, int(pages or 0))
        held = self._take_reservation(doc_id)
        if held is None:
            if pages:
                self._bump(quota_day(self._now()), user_id, pages)
            return self.status(user_id)
        day, reserved = held
        self._bump(day, user_id, pages - reserved)
        return self.status(user_id)

    def release(self, doc_id: str, user_id: str) -> QuotaStatus:
        """失败/删除：全额退回预留（从未预留过 = no-op）。"""
        held = self._take_reservation(doc_id)
        if held is not None:
            day, reserved = held
            self._bump(day, user_id, -reserved)
        return self.status(user_id)

    def check_rate(
        self, user_id: str, action: str, *, limit: int | None = None
    ) -> None:
        """按用户固定窗口（北京时间整点小时桶）频控；超限抛 :class:`RateLimited`。"""
        limits = {
            "upload": self.upload_per_hour,
            "organize": self.organize_per_hour,
        }
        effective = limit if limit is not None else limits.get(action, 0)
        if effective <= 0:
            return  # 显式关闭（<=0）＝不限流；默认值都在构造期保证 >0
        bucket = self._now().astimezone(CST).strftime("%Y%m%d%H")
        key = f"{KEY_RATE_PREFIX}:{action}:{user_id}:{bucket}"
        client = self._client()
        count = int(client.incr(key))
        if count == 1:
            client.expire(key, RATE_WINDOW_S * 2)
        if count > effective:
            raise RateLimited(
                f"操作过于频繁（每小时上限 {effective} 次），请稍后再试",
                scope=action,
                limit=effective,
                window_s=RATE_WINDOW_S,
            )

    def try_lock(self, name: str, *, ttl_s: int) -> bool:
        """在途去重锁（SET NX EX）。拿不到说明同名操作正在进行。"""
        return bool(
            self._client().set(f"{KEY_LOCK_PREFIX}:{name}", "1", nx=True, ex=ttl_s)
        )

    def unlock(self, name: str) -> None:
        self._client().delete(f"{KEY_LOCK_PREFIX}:{name}")


_quota: DocQuota | None = None


def get_doc_quota() -> DocQuota:
    global _quota
    if _quota is None:
        _quota = DocQuota()
    return _quota
