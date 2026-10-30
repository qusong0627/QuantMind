"""行情缓存优先读取：映射/新鲜度/回落语义。

2026-10-30 P3：桥把全市场快照写进 Redis 后，消费方优先读缓存，
只有缺失或陈旧的标的才回落到 QMT 管道（保护单线程管道与订单延迟）。
"""

from __future__ import annotations

import pytest

from backend.services.live_trading.services import quote_cache_reader as mod

NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def _enable_cache_for_this_module(monkeypatch):
    # conftest 全局关缓存；本模块专测缓存路径，显式打开。
    monkeypatch.setenv("QM_QUOTE_CACHE", "on")


def test_to_tick_maps_snapshot_fields() -> None:
    tick = mod.to_tick(
        {
            "last": "40.5",
            "pre_close": "39.0",
            "bid1_p": "40.4",
            "ask1_p": "40.6",
            "ts": "1799999999.5",
            "source": "qmt_big",
        }
    )
    assert tick["lastPrice"] == 40.5
    assert tick["lastClose"] == 39.0
    assert tick["bidPrice"][0] == 40.4
    assert tick["askPrice"][0] == 40.6
    assert tick["_source"] == "qmt_big"


def test_to_tick_falls_back_to_price_field() -> None:
    assert mod.to_tick({"price": "11.2"})["lastPrice"] == 11.2


def test_is_fresh_requires_price_and_recent_ts() -> None:
    assert mod.is_fresh({"lastPrice": 1.0, "_ts": NOW - 1}, NOW, 5.0) is True
    assert mod.is_fresh({"lastPrice": 1.0, "_ts": NOW - 30}, NOW, 5.0) is False
    assert mod.is_fresh({"lastPrice": 0, "_ts": NOW - 1}, NOW, 5.0) is False
    assert mod.is_fresh({"lastPrice": 1.0}, NOW, 5.0) is False


def test_to_suffix_normalizes_forms() -> None:
    assert mod._to_suffix("600036.SH") == "600036.SH"
    assert mod._to_suffix("SH600036") == "600036.SH"
    assert mod._to_suffix("") == ""


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def get_full_tick(self, codes):
        self.calls.append(list(codes))
        return {code: {"lastPrice": 9.9} for code in codes}


@pytest.mark.asyncio
async def test_get_ticks_cache_hit_and_stale_fallback(monkeypatch) -> None:
    async def _rows(codes):
        return {
            "600036.SH": {"last": "40", "pre_close": "39", "ts": str(NOW - 1)},
            "000001.SZ": {"last": "11", "ts": str(NOW - 100)},
        }

    monkeypatch.setattr(mod, "read_snapshots", _rows)
    client = _FakeClient()
    ticks = await mod.get_ticks(
        client, ["SH600036", "000001.SZ", "600519.SH"], now=NOW, max_age_s=5.0
    )

    # 返回键保持调用方原始形态（前缀式入参 → 前缀式键）
    assert ticks["SH600036"]["lastPrice"] == 40.0
    assert ticks["SH600036"]["lastClose"] == 39.0
    # 陈旧 + 缺失的标的才走管道
    assert client.calls == [["000001.SZ", "600519.SH"]]
    assert ticks["000001.SZ"]["lastPrice"] == 9.9
    assert ticks["600519.SH"]["lastPrice"] == 9.9


@pytest.mark.asyncio
async def test_get_ticks_empty_input_returns_empty() -> None:
    client = _FakeClient()
    assert await mod.get_ticks(client, []) == {}
    assert client.calls == []


@pytest.mark.asyncio
async def test_get_ticks_preserves_suffix_input_form(monkeypatch) -> None:
    async def _rows(codes):
        return {"600036.SH": {"last": "40", "ts": str(NOW - 1)}}

    monkeypatch.setattr(mod, "read_snapshots", _rows)
    client = _FakeClient()
    ticks = await mod.get_ticks(client, ["600036.SH"], now=NOW, max_age_s=5.0)
    assert "600036.SH" in ticks and client.calls == []
