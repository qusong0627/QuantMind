"""Engine 代理请求体上限（安全审查 C1）—— api 进程的内存闸。

代理转发前把请求体整包读进 api 进程内存，此前完全无上限：一个未认证的
大 POST（api 路由大多是 optional-user）就能把单容器里的 api/engine/trade/
stream 一起读爆。用例钉住：

- ``Content-Length`` 粗拦：声明值超上限在**读流之前** 413；非法值 400；
- 流式精验：缺头/chunked 或声明值撒谎时，累计字节数超限照样 413（声明值
  不可信）；
- 正常请求原样返回；无 Content-Length 的普通小请求不受影响（411 是文档
  上传端点自己的规矩，不是代理层的）；
- env 脏值回落默认 210MB，代理层绝不因配置炸。

替身只实现 handler 用到的 ``headers`` 与 ``stream()``。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.api.routers.engine_proxy import (  # noqa: E402
    DEFAULT_PROXY_MAX_BODY_MB,
    ENV_PROXY_MAX_BODY_MB,
    _max_proxy_body_bytes,
    _read_bounded_body,
)


class FakeRequest:
    def __init__(self, *, headers=None, chunks=()) -> None:
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self._chunks = list(chunks)

    async def stream(self):
        for chunk in self._chunks:
            yield chunk


def kb(n: int) -> bytes:
    return b"x" * n


# ── env 读取 ────────────────────────────────────────────────────────


def test_max_body_bytes_default_and_override(monkeypatch) -> None:
    monkeypatch.delenv(ENV_PROXY_MAX_BODY_MB, raising=False)
    assert _max_proxy_body_bytes() == DEFAULT_PROXY_MAX_BODY_MB * 1024 * 1024

    monkeypatch.setenv(ENV_PROXY_MAX_BODY_MB, "5")
    assert _max_proxy_body_bytes() == 5 * 1024 * 1024


def test_max_body_bytes_dirty_env_falls_back(monkeypatch) -> None:
    """脏 env 不许炸代理层（fallback 到默认值而不是 500）。"""
    for bad in ("banana", "-1", "0"):
        monkeypatch.setenv(ENV_PROXY_MAX_BODY_MB, bad)
        assert _max_proxy_body_bytes() == DEFAULT_PROXY_MAX_BODY_MB * 1024 * 1024


# ── 读取闸 ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_body_passes_small_body_without_content_length() -> None:
    req = FakeRequest(chunks=[b"hello", b" world"])
    assert await _read_bounded_body(req) == b"hello world"


@pytest.mark.asyncio
async def test_read_body_declared_over_limit_413_before_reading(monkeypatch) -> None:
    """声明值超上限：在读任何一字节之前就拒（省内存也省带宽）。"""
    monkeypatch.setenv(ENV_PROXY_MAX_BODY_MB, "1")
    read_calls = []

    class NoReadRequest(FakeRequest):
        async def stream(self):  # pragma: no cover - 不应被触到
            read_calls.append(1)
            yield b"x"

    req = NoReadRequest(headers={"content-length": str(2 * 1024 * 1024)})
    with pytest.raises(HTTPException) as ei:
        await _read_bounded_body(req)
    assert ei.value.status_code == 413
    assert read_calls == []


@pytest.mark.asyncio
async def test_read_body_lying_content_length_still_bounded(monkeypatch) -> None:
    """声明值撒谎（报小发大）：流式累计精验照样拦下——计数才是权威。"""
    monkeypatch.setenv(ENV_PROXY_MAX_BODY_MB, "1")
    req = FakeRequest(
        headers={"content-length": "10"},
        chunks=[kb(700_000), kb(700_000)],  # 合计 1.4MB > 1MB
    )
    with pytest.raises(HTTPException) as ei:
        await _read_bounded_body(req)
    assert ei.value.status_code == 413


@pytest.mark.asyncio
async def test_read_body_missing_length_over_limit_413(monkeypatch) -> None:
    """无 Content-Length（chunked）：没有粗筛可用，但流式精验独立成立。"""
    monkeypatch.setenv(ENV_PROXY_MAX_BODY_MB, "1")
    req = FakeRequest(chunks=[kb(600_000), kb(600_000)])
    with pytest.raises(HTTPException) as ei:
        await _read_bounded_body(req)
    assert ei.value.status_code == 413


@pytest.mark.asyncio
async def test_read_body_invalid_content_length_400() -> None:
    req = FakeRequest(headers={"content-length": "banana"}, chunks=[b"x"])
    with pytest.raises(HTTPException) as ei:
        await _read_bounded_body(req)
    assert ei.value.status_code == 400


@pytest.mark.asyncio
async def test_read_body_exactly_at_limit_passes(monkeypatch) -> None:
    """边界：恰好等于上限必须放行（上传 200MB + multipart 余量的真实用例）。"""
    monkeypatch.setenv(ENV_PROXY_MAX_BODY_MB, "1")
    req = FakeRequest(
        headers={"content-length": str(1024 * 1024)},
        chunks=[kb(1024 * 1024)],
    )
    assert len(await _read_bounded_body(req)) == 1024 * 1024


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
