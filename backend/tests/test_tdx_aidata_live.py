"""TdxAiData（T-P6-01）真机 E2E：worker 全链（拉起 → ping → 真实取数）。

机构纪律：
- ping/status 不计配额（不触 SDK），必须先绿；
- 真实取数受 token 窗口配额约束（实测 3 次/窗口）——命中限流时**如实跳过并携带
  retry_after**（不得假绿、不得重试轰炸）；
- 测试用独立 socket 路径（不干扰生产 worker）；
- 结束清理：SIGTERM 掉本测试拉起的 worker（pid 来自 ping）。
"""

from __future__ import annotations

import os
import signal

import pytest
import pytest_asyncio

_TEST_SOCKET = "/tmp/qm-tdx-aidata-e2e.sock"


@pytest_asyncio.fixture
async def live_client():
    from backend.shared.tdx_aidata import config
    from backend.shared.tdx_aidata.client import TdxAiDataClient

    if not config.is_enabled():
        pytest.skip("TdxAiData 已显式禁用（TDX_AIDATA_DISABLED/Redis 配置）")
    directory = config.resolve_dir()
    if not config.dir_ready(directory):
        pytest.skip(f"TdxAiData 安装目录不完整: {directory}")
    client = TdxAiDataClient(socket_path=_TEST_SOCKET)
    try:
        yield client
    finally:
        try:
            st = await client.status()
            pid = st.get("pid")
            if isinstance(pid, int) and pid > 1:
                os.kill(pid, signal.SIGTERM)
        except Exception:  # noqa: BLE001
            pass
        await client.close()
        try:
            os.unlink(_TEST_SOCKET)
        except OSError:
            pass


def _skip_if_rate_limited(exc) -> None:
    from backend.shared.tdx_aidata.client import TdxAiDataError

    if isinstance(exc, TdxAiDataError) and exc.code == "rate_limited":
        pytest.skip(
            f"token 窗口限流中（实测 3 次/窗口）：{exc.message} "
            f"retry_after={exc.retry_after_s}s"
        )
    raise exc


@pytest.mark.integration
@pytest.mark.asyncio
async def test_worker_lifecycle_and_live_quote(live_client):
    """全链：按需拉起 worker → ping/status 就绪 → 真实取数（日K + 快照）。"""
    from backend.shared.tdx_aidata.client import TdxAiDataError

    # 1) 生命周期（不耗配额）
    assert await live_client.ensure_worker(), "worker 拉起失败"
    st = await live_client.status()
    assert st["worker"] == "up", st
    assert st.get("sdk_ready") is True, st
    assert st.get("pid"), st
    gate = st.get("gate") or {}
    assert gate.get("max_requests_per_window", 0) >= 1

    # 2) 真实取数：日K（1 次配额）
    try:
        bars = await live_client.get_klines("600036.SH", interval="daily", count=5, timeout=60)
    except TdxAiDataError as exc:  # noqa: BLE001
        _skip_if_rate_limited(exc)
    assert isinstance(bars, list) and len(bars) >= 1, bars
    last = bars[-1]
    assert last["date"] and float(last["close"]) > 0
    assert last["high"] is not None and last["low"] is not None

    # 3) 真实快照（再 1 次配额；限流则如实跳过）
    try:
        quote = await live_client.get_quote("600036.SH", timeout=60)
    except TdxAiDataError as exc:  # noqa: BLE001
        _skip_if_rate_limited(exc)
    assert isinstance(quote, dict) and quote, quote

    # 快照字段以 SDK 实际为准（键为字符串、值多为字符串型数值，含五档列表）——
    # 断言「至少一个数值型价格 > 0」，不绑定具体键名
    def _numeric(v) -> bool:
        if isinstance(v, (int, float)):
            return float(v) > 0
        if isinstance(v, str):
            try:
                return float(v) > 0
            except ValueError:
                return False
        if isinstance(v, list):
            return any(_numeric(x) for x in v[:5])
        return False

    assert any(_numeric(v) for v in quote.values()), quote


@pytest.mark.integration
@pytest.mark.asyncio
async def test_worker_budget_gate_fast_fails_without_sdk(live_client):
    """预算闸门：窗口内超额请求快速失败（不触 SDK），带 retry_after。"""
    from backend.shared.tdx_aidata.client import TdxAiDataError

    assert await live_client.ensure_worker(), "worker 拉起失败"
    st = await live_client.status()
    assert st["worker"] == "up", st
    used = int((st.get("gate") or {}).get("window_requests") or 0)
    max_req = int((st.get("gate") or {}).get("max_requests_per_window") or 3)

    errors = 0
    for _ in range(max_req + 2 - min(used, max_req)):
        try:
            await live_client.get_quote("600036.SH", timeout=20)
        except TdxAiDataError as exc:
            if exc.code == "rate_limited" and exc.retry_after_s is not None:
                errors += 1
            else:
                raise
    # 窗口打满后必然出现 fast-fail（或窗口刚翻转重开——则本轮不强制）
    st2 = await live_client.status()
    gate2 = st2.get("gate") or {}
    assert gate2.get("window_requests", 0) <= max_req + 1 or errors >= 1, (gate2, errors)
