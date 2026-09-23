"""对外握手节流（`router._check_throttle` / `router._bump`）。

为什么单独一个文件
------------------
握手端点是**匿名可达**的，且每次尝试要跑一次 bcrypt（cost-12 ≈ 0.23s 同步 CPU）。
没有节流，它既是暴力破解入口，也是一个廉价的拒绝服务点。
但此前 `test_external_api_handshake.py` 里的 5 条测试都把 `_check_throttle`
monkeypatch 成 no-op（为了让它们专注别的语义）——于是**429 分支、Retry-After、
fail-open、TTL 自愈全都没有覆盖**，而那正是后来被重写过的代码。
本文件把那一块补上。

测试全部走 `_get_redis` 的接缝（monkeypatch），不连真 Redis。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from backend.services.api.routers.external import router as ext_router


class _FakeRedis:
    """够用的 Redis 替身：计数、TTL、以及「EXPIRE 失败过」的状态。"""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.ttls: dict[str, int] = {}
        self.expire_calls: list[tuple[str, int]] = []
        self.fail_on: str | None = None

    async def incr(self, key: str) -> int:
        if self.fail_on == "incr":
            raise ConnectionError("redis 挂了")
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, key: str, window: int) -> bool:
        self.expire_calls.append((key, window))
        self.ttls[key] = window
        return True

    async def ttl(self, key: str) -> int:
        if self.fail_on == "ttl":
            raise ConnectionError("redis 挂了")
        return self.ttls.get(key, -1)  # -1 = 键存在但没有过期时间


def _request(
    host: str = "203.0.113.7", headers: dict[str, str] | None = None
) -> Request:
    raw = [
        (k.lower().encode("latin-1"), v.encode("latin-1"))
        for k, v in (headers or {}).items()
    ]
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/ext/v1/auth/session",
            "query_string": b"",
            "headers": raw,
            "client": (host, 4242),
        }
    )


@pytest.fixture()
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    r = _FakeRedis()

    async def _get() -> Any:
        return r

    monkeypatch.setattr(ext_router, "_get_redis", _get)
    return r


# ---------------------------------------------------------------------------
# 正常路径与两个桶
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_under_threshold_passes(fake_redis: _FakeRedis) -> None:
    for _ in range(ext_router.HANDSHAKE_MAX_ATTEMPTS):
        await ext_router._check_throttle(_request(), "ak-under-test")


@pytest.mark.asyncio
async def test_credential_bucket_trips_after_max_attempts(
    fake_redis: _FakeRedis,
) -> None:
    """同一个 (对端, 凭据) 超过 `HANDSHAKE_MAX_ATTEMPTS` 次 → 429 + Retry-After。"""
    req = _request()
    with pytest.raises(HTTPException) as exc:
        for _ in range(ext_router.HANDSHAKE_MAX_ATTEMPTS + 1):
            await ext_router._check_throttle(req, "ak-brute-forced")

    assert exc.value.status_code == 429
    assert exc.value.detail == "too_many_attempts"
    # RFC 6585：429 应带 Retry-After，客户端才知道什么时候回来
    assert exc.value.headers["Retry-After"] == str(ext_router.HANDSHAKE_WINDOW_SECONDS)


@pytest.mark.asyncio
async def test_peer_bucket_is_credential_independent(fake_redis: _FakeRedis) -> None:
    """**核心断言**：同一对端**换着 access_key** 刷也要被拦住。

    凭据维度是攻击者控制的——`access_key` 是请求体里的自由字段，每换一个随机值
    就是一个新桶、count=1，永远超不过阈值。只挂凭据维度等于没限流，
    攻击者可以无限次触发 bcrypt。对端维度他换不掉（`request.client.host`
    是 TCP 对端，不可伪造）。
    """
    req = _request(host="198.51.100.9")
    with pytest.raises(HTTPException) as exc:
        for i in range(ext_router.HANDSHAKE_MAX_ATTEMPTS_PER_PEER + 1):
            # 每次都换一个「凭据」，凭据桶永远 count=1
            await ext_router._check_throttle(req, f"ak-random-{i}")

    assert exc.value.status_code == 429


@pytest.mark.asyncio
async def test_peer_bucket_checked_before_credential_bucket(
    fake_redis: _FakeRedis,
) -> None:
    """对端桶必须**先**判——否则一个已被对端维度拦下的来源，还要先烧一次
    凭据桶的键空间。（顺序也是实现的一部分，钉住它。）"""
    req = _request(host="198.51.100.10")
    fake_redis.counts[ext_router._peer_throttle_key(req)] = (
        ext_router.HANDSHAKE_MAX_ATTEMPTS_PER_PEER
    )
    with pytest.raises(HTTPException):
        await ext_router._check_throttle(req, "ak-whatever")
    assert ext_router._throttle_key(req, "ak-whatever") not in fake_redis.counts


@pytest.mark.asyncio
async def test_different_peers_do_not_share_a_bucket(fake_redis: _FakeRedis) -> None:
    """一个节点刷爆不该连累另一个节点。"""
    for _ in range(ext_router.HANDSHAKE_MAX_ATTEMPTS + 1):
        try:
            await ext_router._check_throttle(_request(host="203.0.113.1"), "ak-a")
        except HTTPException:
            pass
    # 另一个对端、另一个凭据：不受影响
    await ext_router._check_throttle(_request(host="203.0.113.2"), "ak-b")


# ---------------------------------------------------------------------------
# 键的构造：不可伪造 + 不落明文
# ---------------------------------------------------------------------------


def test_throttle_key_ignores_client_controlled_headers() -> None:
    """**安全属性**：节流键只取 TCP 对端，不读 X-Forwarded-For / X-Real-IP。

    第一版读过那两个头，那是个洞：它们是客户端可控的，攻击者每试一次换一个值
    就换一个桶，节流直接失效。
    """
    a = ext_router._throttle_key(
        _request(host="203.0.113.5", headers={"X-Forwarded-For": "1.2.3.4"}), "ak-x"
    )
    b = ext_router._throttle_key(
        _request(host="203.0.113.5", headers={"X-Forwarded-For": "9.9.9.9"}), "ak-x"
    )
    assert a == b, "X-Forwarded-For 影响了节流键——客户端可换桶，节流失效"


def test_throttle_key_carries_no_plaintext_credential() -> None:
    """明文 access_key 不得进 Redis 键名（MONITOR / 慢日志 / 备份都会带出去）。"""
    secret_looking = "ak_SUPER_SECRET_VALUE_1234567890"
    key = ext_router._throttle_key(_request(), secret_looking)
    assert secret_looking not in key
    assert ext_router._fingerprint(secret_looking) in key


def test_peer_key_is_stable_and_credential_free() -> None:
    assert ext_router._peer_throttle_key(_request(host="h1")) == (
        ext_router._peer_throttle_key(_request(host="h1"))
    )
    assert ext_router._peer_throttle_key(_request(host="h1")) != (
        ext_router._peer_throttle_key(_request(host="h2"))
    )


# ---------------------------------------------------------------------------
# TTL 自愈（`_bump`）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bump_sets_ttl_on_first_hit(fake_redis: _FakeRedis) -> None:
    await ext_router._bump(fake_redis, "k", 300)
    assert fake_redis.ttls["k"] == 300


@pytest.mark.asyncio
async def test_bump_heals_a_key_that_lost_its_ttl(fake_redis: _FakeRedis) -> None:
    """**核心断言**：已存在但没有 TTL 的键要补一次 EXPIRE。

    否则：`INCR` 成功、`EXPIRE` 失败（连接被重置/超时）→ 这个键**永不过期**，
    计数一直 ≥ 阈值 → 该 (对端, 凭据) 被**永久 429**，且没有自愈路径，
    只能人工 DEL。本仓有过 Redis 抖动的真实事故，不是理论场景。
    """
    fake_redis.counts["k"] = 5  # 已存在
    fake_redis.ttls.pop("k", None)  # 但没有 TTL
    await ext_router._bump(fake_redis, "k", 300)
    assert fake_redis.ttls["k"] == 300, "键仍然没有 TTL——会被永久卡住"


@pytest.mark.asyncio
async def test_bump_does_not_reset_a_healthy_ttl(fake_redis: _FakeRedis) -> None:
    """正常的键不该被反复续期（那会让窗口永远不过去，等于永久封禁）。"""
    fake_redis.counts["k"] = 5
    fake_redis.ttls["k"] = 120
    await ext_router._bump(fake_redis, "k", 300)
    assert fake_redis.ttls["k"] == 120
    assert fake_redis.expire_calls == []


# ---------------------------------------------------------------------------
# fail-open：Redis 不可用时不阻断对外面
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redis_unavailable_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Redis 连不上 → 放行并告警，不是「握手全部挂死」。

    刻意选 fail-open：握手是外部系统接入的唯一入口，Redis 抖一下不该让整个
    对外面停摆。代价是 Redis 挂掉期间节流失效——届时 bcrypt 的计算成本
    本身就是最后一道限速。
    """

    async def _none() -> Any:
        return None

    monkeypatch.setattr(ext_router, "_get_redis", _none)
    for _ in range(ext_router.HANDSHAKE_MAX_ATTEMPTS_PER_PEER + 5):
        await ext_router._check_throttle(_request(), "ak-x")


@pytest.mark.asyncio
async def test_redis_error_fails_open(fake_redis: _FakeRedis) -> None:
    """Redis 抛异常（超时/连接重置）也放行——不能把 Redis 故障升级成 500。"""
    fake_redis.fail_on = "incr"
    for _ in range(ext_router.HANDSHAKE_MAX_ATTEMPTS_PER_PEER + 5):
        await ext_router._check_throttle(_request(), "ak-x")


@pytest.mark.asyncio
async def test_ttl_error_also_fails_open(fake_redis: _FakeRedis) -> None:
    """`_bump` 的 TTL 自愈那一步炸了也要放行，不能穿成 500。"""
    fake_redis.fail_on = "ttl"
    await ext_router._check_throttle(_request(), "ak-x")


@pytest.mark.asyncio
async def test_throttle_exception_is_not_swallowed_into_success(
    fake_redis: _FakeRedis,
) -> None:
    """反向保险：真正超限时必须抛出去。

    fail-open 的 `except Exception` 很容易写宽——把 `HTTPException` 也吞掉的话，
    节流就**完全失效**而测试仍全绿。`_check_throttle` 里先 `except HTTPException: raise`
    就是为了这个。
    """
    fake_redis.counts[ext_router._peer_throttle_key(_request())] = (
        ext_router.HANDSHAKE_MAX_ATTEMPTS_PER_PEER + 1
    )
    with pytest.raises(HTTPException) as exc:
        await ext_router._check_throttle(_request(), "ak-x")
    assert exc.value.status_code == 429
