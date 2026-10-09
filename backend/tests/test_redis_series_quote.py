"""Redis 序列行情解析单测（纯函数 + 批量取价假客户端，不依赖网络）。"""

from __future__ import annotations

import json
import time

import pytest

from backend.shared.freshness import FreshnessPolicy
from backend.services.simulation.services import redis_series_quote as rq
from backend.services.simulation.services.redis_series_quote import (
    parse_series_member,
    recent_traded_shares,
    series_key_for,
)

_POLICY = FreshnessPolicy(fresh_within_s=60, stale_within_s=300)  # 与默认口径一致


def test_series_key_uses_prefix_format():
    assert series_key_for("600036.SH") == "market:series:SH600036"
    assert series_key_for("SH600036") == "market:series:SH600036"
    assert series_key_for("0700.HK") == "market:series:0700.HK"
    assert series_key_for("HK00700") == "market:series:HK00700"
    assert series_key_for("AAPL") == "market:series:AAPL"
    assert series_key_for("not-a-code-!!!") is None


def test_parse_fresh_tick():
    member = json.dumps(
        {"price": 40.9, "open": 40.5, "source": "remote_redis"},
        ensure_ascii=False,
    )
    tick = parse_series_member(member, 1_000_000.0, 1_000_100.0, _POLICY)
    assert tick is not None
    assert tick["price"] == 40.9
    assert tick["age_s"] == 100.0
    assert tick["open"] == 40.5
    assert tick["freshness"] == "stale"  # 100s：超 fresh(60) 未超 stale(300)，可用须标注


def test_parse_stale_or_bad_tick_returns_none():
    member = json.dumps({"price": 40.9})
    # 超龄（unavailable）
    assert parse_series_member(member, 1_000_000.0, 1_000_400.0, _POLICY) is None
    # 零价格
    assert parse_series_member(json.dumps({"price": 0}), 1_000_000.0, 1_000_100.0, _POLICY) is None
    # 非法 JSON
    assert parse_series_member("not-json", 1_000_000.0, 1_000_100.0, _POLICY) is None
    # 未来时间戳（超时钟偏斜容差）
    assert parse_series_member(member, 1_000_200.0, 1_000_100.0, _POLICY) is None
    # 未来戳在容差内（-2s）→ fresh 可用
    tick = parse_series_member(member, 1_000_102.0, 1_000_100.0, _POLICY)
    assert tick is not None and tick["freshness"] == "fresh"


# ── fetch_series_ticks：批量取价必须与单只取价同一答案（2026-10-09 事故）──────
#
# 事故：决策轮买单以「无法获取实时行情，模拟单拒绝成交」全拒。桥席按
# ~100s/只轮转写 market:series，而批量取价用 60s 硬窗裁剪——「stale 但策略判定
# 可用（≤300s）」的合法 tick 被整批漏掉；单只取价（zrevrange + 策略分级）却会
# 正常返回它。同一问题两个答案：批量说没有，单只说有。


class _FakePipeline:
    """最小 pipeline 假件：记录命令，execute 时对内存存储求值。"""

    def __init__(self, store: dict[str, list[tuple[str, float]]]):
        self._store = store
        self._cmds: list[tuple] = []

    def zrevrange(self, key, start, end, withscores=True):
        self._cmds.append(("latest", key))
        return self

    def zrangebyscore(self, key, mn, mx, withscores=True):
        self._cmds.append(("window", key, mn, mx))
        return self

    async def execute(self):
        out = []
        for cmd in self._cmds:
            rows = sorted(self._store.get(cmd[1], []), key=lambda item: item[1])
            if cmd[0] == "latest":
                out.append(rows[-1:] if rows else [])
            else:
                mn, mx = cmd[2], cmd[3]
                out.append([row for row in rows if mn <= row[1] <= mx])
        return out


class _FakeRedis:
    def __init__(self, store: dict[str, list[tuple[str, float]]]):
        self._store = store

    def pipeline(self, transaction=False):
        return _FakePipeline(self._store)

    async def zrevrange(self, key, start, end, withscores=True):
        rows = sorted(self._store.get(key, []), key=lambda item: item[1])
        return rows[-1:] if rows else []


def _member(price: float, volume: float, source: str = "tdx_bridge") -> str:
    return json.dumps({"price": price, "volume": volume, "source": source})


# ── recent_traded_shares：口径三缺陷（2026-10-09 决策轮买单全拒事故）────────────
#
# 当日实证（1445 轮重放）：SH600282 窗口差 65~800（手）被当股取整 → 容量 0 股 → 硬拒；
# SZ002438 最新成员同源仅 1 个、且跨源相减（qmt 36546 → tdx 36436）出负数被钳 0 →
# 同样硬拒。容量 0 是「无流动性」断言，None 才是「不可验证」——此区分是本案核心。


def _rows(*specs):
    """(volume, source, score) 序列 → window_rows 形状（score 被本函数忽略）。"""
    return [(json.dumps({"volume": v, "source": s}), float(t)) for v, s, t in specs]


def test_recent_volume_converts_hands_to_shares():
    rows = _rows((100.0, "tdx_bridge", 0), (130.0, "tdx_bridge", 30))
    assert recent_traded_shares(rows) == 3000.0  # 30 手 → 3000 股


def test_recent_volume_same_source_zero_or_negative_is_unverifiable():
    # 同源平值（TDX 快照量按批刷新，整分钟平值实测）→ 不可当「无成交」
    assert (
        recent_traded_shares(_rows((100.0, "tdx_bridge", 0), (100.0, "tdx_bridge", 30)))
        is None
    )
    # 同源倒挂（源侧重启/回滚）→ 不可验证，不能钳 0
    assert (
        recent_traded_shares(_rows((130.0, "tdx_bridge", 0), (90.0, "tdx_bridge", 30)))
        is None
    )


def test_recent_volume_last_source_needs_two_members():
    # 最新成员是 tdx、但同源只有 1 个 → 不可验证（跨源混合相减正是上一次事故）
    rows = _rows(
        (36546.0, "qmt_big", 0), (36715.0, "qmt_big", 10), (36436.0, "tdx_bridge", 30)
    )
    assert recent_traded_shares(rows) is None


def test_recent_volume_uses_last_source_domain_only():
    # 最新源 qmt_big 有 3 个成员：只在该域内求差，跨源成员不参与
    rows = _rows(
        (100.0, "qmt_big", 0),
        (500.0, "tdx_bridge", 10),  # 另一源，量纲跳动不得污染差值
        (130.0, "qmt_big", 20),
        (160.0, "qmt_big", 30),
    )
    assert recent_traded_shares(rows) == 6000.0  # (160 - 100) 手 → 股


def test_recent_volume_single_member_or_empty_is_none():
    assert recent_traded_shares(_rows((100.0, "tdx_bridge", 0))) is None
    assert recent_traded_shares([]) is None
    # 毒丸成员（非 JSON / volume 缺失）跳过，不炸
    assert recent_traded_shares([("not-json", 0.0), ("{}", 1.0)]) is None


@pytest.mark.asyncio
async def test_batch_fetch_cross_source_dip_is_none_not_zero(monkeypatch):
    """跨源倒挂的窗口必须落 None（放行小额），绝不能钳 0（硬拒）——2026-10-09 实证形态。"""
    now = time.time()
    store = {
        "market:series:SZ002438": [
            (_member(13.5, 36546.0, "qmt_big"), now - 50.0),
            (_member(13.5, 36715.0, "qmt_big"), now - 40.0),
            (_member(13.5, 36436.0, "tdx_bridge"), now - 20.0),
        ]
    }
    monkeypatch.setattr(rq, "_get_client", lambda: _FakeRedis(store))
    ticks = await rq.fetch_series_ticks(["SZ002438"], policy=_POLICY)
    assert ticks["SZ002438"]["recent_volume"] is None


@pytest.mark.asyncio
async def test_batch_fetch_keeps_stale_but_usable_tick(monkeypatch):
    # Arrange：最新 tick 95s 前（>60s 硬窗，≤300s 可用）
    now = time.time()
    store = {"market:series:SZ002438": [(_member(13.48, 31672.0), now - 95.0)]}
    monkeypatch.setattr(rq, "_get_client", lambda: _FakeRedis(store))

    # Act
    ticks = await rq.fetch_series_ticks(["SZ002438"], policy=_POLICY)

    # Assert：与单只取价同答案——stale 可用，如实标注
    assert "SZ002438" in ticks
    assert ticks["SZ002438"]["price"] == 13.48
    assert ticks["SZ002438"]["freshness"] == "stale"


@pytest.mark.asyncio
async def test_batch_fetch_drops_unavailable_and_reports_recent_volume(monkeypatch):
    # Arrange
    now = time.time()
    store = {
        # 最新 400s → unavailable，整只剔除
        "market:series:SH600282": [(_member(4.63, 423730.0), now - 400.0)],
        # 最新 10s（fresh）；60s 窗内 2 个成员 → 量差可算
        "market:series:SZ002664": [
            (_member(14.0, 100.0), now - 65.0),
            (_member(14.1, 130.0), now - 40.0),
            (_member(14.2, 160.0), now - 10.0),
        ],
        # 无键 → 缺就是缺
    }
    monkeypatch.setattr(rq, "_get_client", lambda: _FakeRedis(store))

    # Act
    ticks = await rq.fetch_series_ticks(["SH600282", "SZ002664", "SH600036"], policy=_POLICY)

    # Assert
    assert "SH600282" not in ticks  # 超龄不允许「凑合用」
    assert "SH600036" not in ticks
    assert ticks["SZ002664"]["price"] == 14.2
    # 160 - 130 = 30 手 → ×100 归一为股（下游日频核整手取整按股）
    assert ticks["SZ002664"]["recent_volume"] == 3000.0


@pytest.mark.asyncio
async def test_batch_and_single_path_agree_on_same_store(monkeypatch):
    """同一份数据两个入口（批量/单只）必须给同一个可用的答案。"""
    now = time.time()
    store = {"market:series:SH600036": [(_member(41.86, 646224.0), now - 90.0)]}
    monkeypatch.setattr(rq, "_get_client", lambda: _FakeRedis(store))

    batch = (await rq.fetch_series_ticks(["SH600036"], policy=_POLICY)).get("SH600036")
    single = await rq.fetch_series_tick("SH600036", policy=_POLICY)

    assert batch is not None and single is not None
    assert batch["price"] == single["price"] == 41.86
    assert batch["freshness"] == single["freshness"] == "stale"
