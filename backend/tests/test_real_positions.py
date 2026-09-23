"""实盘持仓并集口径的单测（纯函数层 + 券商选定键读法）。

盯四件事：多券商两源都要并进来（不是取最新一条）、停更源不并入、
同票两源都报时按活跃券商取量、以及 **``broker:selected:CN`` 从交易库的哪条路读**。
前三条错了都不会报错——只会让自选里少几只票、或让哨兵对着早已卖出的持仓发提醒；
最后一条错了更安静：**永远回退 env 默认**，页面上切了券商而订单路由与持仓/风控
分歧在两座真实账户之间（历史实现正是如此，见 ``active_broker_type`` 的注释）。
"""

from __future__ import annotations

import inspect
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.shared import real_positions as rp
from backend.shared.real_positions import (
    BrokerSelectionUnreadable,
    active_broker_type,
    broker_for_snapshot_source,
    merge_real_sources,
    selected_broker_is_explicit,
    snapshot_source_for_broker,
)
from backend.services.trade_shared.trade_config import settings

BASE = datetime(2026, 9, 20, 6, 0, tzinfo=timezone.utc)


def _payload(*positions: dict) -> str:
    """实盘快照 payload：实测是**双层编码**（JSON 字符串套 JSON），这里如实模拟。"""
    return json.dumps({"positions": list(positions)})


def _pos(symbol: str, volume: float, **extra) -> dict:
    return {"symbol": symbol, "volume": volume, "available_volume": volume, **extra}


class TestMergeRealSources:
    def test_unions_disjoint_brokers(self):
        # 实测 qmt_exec 50 只 / tdx_bridge 8 只互不相交：取最新一条等于掷硬币
        rows = [
            ("qmt_exec", BASE, _payload(_pos("600036.SH", 200))),
            (
                "tdx_bridge",
                BASE - timedelta(seconds=10),
                _payload(_pos("000001.SZ", 500)),
            ),
        ]
        out, meta = merge_real_sources(rows)

        assert set(out) == {"SH600036", "SZ000001"}
        assert out["SH600036"]["source"] == "qmt_exec"
        assert out["SZ000001"]["sources"] == ["tdx_bridge"]
        assert meta["sources"]["qmt_exec"]["stale"] is False

    def test_stale_source_is_reported_but_not_merged(self):
        # 停更源会把早已卖出的持仓一直留在表上 → 假持仓，比漏持仓更危险
        rows = [
            ("qmt_exec", BASE, _payload(_pos("600036.SH", 200))),
            ("tdx_bridge", BASE - timedelta(hours=3), _payload(_pos("000001.SZ", 500))),
        ]
        out, meta = merge_real_sources(rows)

        assert set(out) == {"SH600036"}
        assert meta["sources"]["tdx_bridge"]["stale"] is True
        assert meta["sources"]["tdx_bridge"]["positions"] == 1  # 有仓位但不并入，如实报

    def test_active_broker_wins_when_both_report_same_symbol(self):
        rows = [
            ("qmt_exec", BASE, _payload(_pos("600036.SH", 200))),
            ("tdx_bridge", BASE, _payload(_pos("600036.SH", 900))),
        ]
        out, _ = merge_real_sources(rows, active_source="tdx")

        assert out["SH600036"]["volume"] == 900
        assert out["SH600036"]["source"] == "tdx_bridge"
        assert out["SH600036"]["sources"] == ["qmt_exec", "tdx_bridge"]

    def test_larger_volume_wins_without_active_preference(self):
        rows = [
            ("qmt_exec", BASE, _payload(_pos("600036.SH", 200))),
            ("tdx_bridge", BASE, _payload(_pos("600036.SH", 900))),
        ]
        out, meta = merge_real_sources(rows, active_source="tiger")

        assert out["SH600036"]["volume"] == 900
        assert meta["active_broker"] is None  # 没有映射的券商 = 没有偏好，不假报

    def test_non_a_share_rows_are_dropped(self):
        rows = [("qmt_exec", BASE, _payload(_pos("AAPL", 10), _pos("600036.SH", 200)))]
        out, meta = merge_real_sources(rows)

        assert set(out) == {"SH600036"}
        assert meta["sources"]["qmt_exec"]["positions"] == 2  # 原始条数照实报

    def test_undecodable_payload_is_not_silently_empty(self):
        rows = [("qmt_exec", BASE, "{not json")]
        out, meta = merge_real_sources(rows)

        assert out == {}
        assert meta["sources"]["qmt_exec"]["positions"] == 0

    def test_empty_rows_report_no_snapshot(self):
        out, meta = merge_real_sources([])

        assert out == {}
        assert meta == {"sources": {}, "snapshot_at": None, "active_broker": None}


class TestSnapshotSourceForBroker:
    def test_unknown_broker_has_no_snapshot_source(self):
        assert snapshot_source_for_broker("tiger") is None
        assert snapshot_source_for_broker(None) is None

    def test_known_brokers_map_to_their_snapshot_source(self):
        # 这条映射是风控选源的唯一出处：接错账户比不接更危险
        assert snapshot_source_for_broker("tdx") == "tdx_bridge"
        assert snapshot_source_for_broker("qmt_exec") == "qmt_exec"

    def test_reverse_map_is_available_for_per_source_views(self):
        # 「按源看」的视图要能反查出可选券商；猜映射就会切错券商
        assert broker_for_snapshot_source("qmt_exec") == "qmt_exec"
        assert broker_for_snapshot_source(" TDX_BRIDGE ") == "tdx"  # 大小写/空白都要认
        assert broker_for_snapshot_source("manual") is None  # 非实盘源不可选为交易券商

    def test_equal_volume_without_active_preference_keeps_first_row(self):
        """量相同且无活跃偏好：先到的那行留下，但**两个出处都要记上**（一键卖出按出处路由）。"""
        rows = [
            ("qmt_exec", BASE, _payload(_pos("600036.SH", 200))),
            ("tdx_bridge", BASE, _payload(_pos("600036.SH", 200))),
        ]
        out, _ = merge_real_sources(rows, active_source="tiger")

        assert out["SH600036"]["volume"] == 200
        assert out["SH600036"]["source"] == "qmt_exec"
        assert out["SH600036"]["sources"] == ["qmt_exec", "tdx_bridge"]


# ══ 券商选定键的读法（H2）══════════════════════════════════════════
class FakeRawRedis:
    """``RedisClient.client``（原生 redis-py 形态）的替身：只认 ``get``。"""

    def __init__(self, value=None, *, raises: Exception | None = None) -> None:
        self.value = value
        self.raises = raises
        self.keys: list[str] = []

    def get(self, key):
        self.keys.append(key)
        if self.raises is not None:
            raise self.raises
        return self.value


@pytest.fixture(autouse=True)
def _broker_redis_state(monkeypatch):
    """模块级连接单例与告警冷却都是**跨调用**状态：每条用例前后必须复位。

    不复位的话，一条用例失败留下的「30s 内不再重连」会让下一条用例凭空看不到
    Redis 调用，红灯指向错的地方。
    """
    monkeypatch.setattr(rp, "_broker_redis", None, raising=False)
    monkeypatch.setattr(rp, "_broker_redis_last_try", 0.0, raising=False)
    monkeypatch.setattr(rp, "_fallback_warned_at", 0.0, raising=False)


class _BrokenSettings:
    """settings 属性读取就抛（配置模块半初始化/属性缺失的真实形态）。"""

    @property
    def REAL_BROKER_TYPE(self) -> str:
        raise RuntimeError("settings 未就绪")


def _pin(raw_redis: FakeRawRedis) -> FakeRawRedis:
    """把模块单例换成「已连上」的替身（``.client`` 非空即视为连上）。"""
    rp._broker_redis = SimpleNamespace(client=raw_redis)  # noqa: SLF001 - 测的就是这层
    return raw_redis


class TestActiveBrokerType:
    def test_pinned_broker_wins_over_settings_fallback(self, monkeypatch):
        """页面选定值必须压过 env 默认——这就是 H2 的全部要点。"""
        monkeypatch.setattr(settings, "REAL_BROKER_TYPE", "tdx", raising=False)
        raw = _pin(FakeRawRedis(b"qmt_exec"))

        assert active_broker_type() == "qmt_exec"
        assert raw.keys == ["broker:selected:CN"]

    def test_text_value_also_decodes(self):
        # 哨兵/单机两种形态在 decode_responses 上不一致，两种都要认
        _pin(FakeRawRedis("qmt_exec"))

        assert active_broker_type() == "qmt_exec"

    def test_absent_key_falls_back_without_warning(self, monkeypatch, caplog):
        """键不存在 = 没人显式选过 ⇒ env 兜底是**设计语义**，不该告警。"""
        monkeypatch.setattr(settings, "REAL_BROKER_TYPE", "qmt_exec", raising=False)
        _pin(FakeRawRedis(None))

        with caplog.at_level("WARNING"):
            assert active_broker_type() == "qmt_exec"
        assert caplog.records == []

    def test_empty_value_is_treated_as_absent(self, monkeypatch):
        monkeypatch.setattr(settings, "REAL_BROKER_TYPE", "qmt_exec", raising=False)
        _pin(FakeRawRedis(b""))

        assert active_broker_type() == "qmt_exec"

    def test_read_failure_falls_back_loudly(self, monkeypatch, caplog):
        """容忍方（列表/并集）不许被读失败打断，但**必须留痕**——静默回退就是原病。"""
        monkeypatch.setattr(settings, "REAL_BROKER_TYPE", "tdx", raising=False)
        _pin(FakeRawRedis(raises=RuntimeError("connection reset")))

        with caplog.at_level("WARNING"):
            assert active_broker_type() == "tdx"
        assert any("broker:selected:CN" in r.getMessage() for r in caplog.records)

    def test_read_failure_strict_raises(self, monkeypatch):
        """严格模式：读不到 ≠ 没设置。调用点（决策轮取资金面）要据此 abort。"""
        monkeypatch.setattr(settings, "REAL_BROKER_TYPE", "tdx", raising=False)
        _pin(FakeRawRedis(raises=RuntimeError("connection reset")))

        with pytest.raises(BrokerSelectionUnreadable) as ei:
            active_broker_type(strict=True)
        assert "connection reset" in str(ei.value)

    def test_absent_key_strict_still_falls_back(self, monkeypatch):
        """严格模式只对**读失败**严格：键不存在时 settings 兜底照旧（两回事）。"""
        monkeypatch.setattr(settings, "REAL_BROKER_TYPE", "qmt_exec", raising=False)
        _pin(FakeRawRedis(None))

        assert active_broker_type(strict=True) == "qmt_exec"

    def test_unconnected_client_is_a_read_failure(self, monkeypatch):
        """Redis 连不上（``.client`` 为 None）在严格模式下同样是不做，不是掷硬币。"""
        monkeypatch.setattr(settings, "REAL_BROKER_TYPE", "tdx", raising=False)
        rp._broker_redis = SimpleNamespace(client=None)  # noqa: SLF001
        rp._broker_redis_last_try = time.monotonic()  # noqa: SLF001 - 冷却期内不再重连

        assert active_broker_type() == "tdx"  # 非严格：照旧兜底
        with pytest.raises(BrokerSelectionUnreadable):
            active_broker_type(strict=True)

    def test_connect_failure_is_cooled_down(self, monkeypatch):
        """连不上不许每次调用都重连：热路径上那是把「读不到券商」升级成「下单卡死」。"""
        attempts: list[int] = []

        class FailingClient:
            def __init__(self) -> None:
                self.client = None

            def connect(self):
                attempts.append(
                    1
                )  # 模拟「如实连接失败」：connect 自己把 client 置 None

        monkeypatch.setattr(
            "backend.services.trade_shared.redis_client.RedisClient", FailingClient
        )
        monkeypatch.setattr(settings, "REAL_BROKER_TYPE", "tdx", raising=False)

        assert active_broker_type() == "tdx"
        assert len(attempts) == 1
        assert active_broker_type() == "tdx"  # 冷却期内：不再尝试
        assert len(attempts) == 1
        rp._broker_redis_last_try = time.monotonic() - rp._BROKER_REDIS_RETRY_S - 1  # noqa: SLF001
        assert active_broker_type() == "tdx"
        assert len(attempts) == 2  # 冷却过后允许再试一次

    def test_unreadable_settings_non_strict_returns_none(self, monkeypatch):
        """键与 settings 都读不到：非严格调用方拿到 None（= 不知道），不编一个券商名。"""
        _pin(FakeRawRedis(None))
        monkeypatch.setattr(
            "backend.services.trade_shared.trade_config.settings", _BrokenSettings()
        )

        assert active_broker_type() is None

    def test_unreadable_settings_strict_raises(self, monkeypatch):
        _pin(FakeRawRedis(None))
        monkeypatch.setattr(
            "backend.services.trade_shared.trade_config.settings", _BrokenSettings()
        )

        with pytest.raises(BrokerSelectionUnreadable) as ei:
            active_broker_type(strict=True)
        assert "settings.REAL_BROKER_TYPE" in str(ei.value)

    def test_selected_broker_is_explicit(self):
        """页面要说清「你选的」还是「env 兜底」——读不到算未显式设置，但不抛。"""
        _pin(FakeRawRedis(b"qmt_exec"))
        assert selected_broker_is_explicit() is True

    def test_selected_broker_not_explicit_when_absent(self):
        _pin(FakeRawRedis(None))
        assert selected_broker_is_explicit() is False

    def test_selected_broker_not_explicit_when_read_fails(self):
        _pin(FakeRawRedis(raises=RuntimeError("boom")))
        assert selected_broker_is_explicit() is False


class TestBrokerSelectionReadPath:
    """源码守卫：这条读法曾经**永远失败且无人察觉**，钉住「别再换回哨兵客户端」。"""

    SRC = Path(inspect.getsourcefile(rp) or "").read_text(encoding="utf-8")

    def test_reader_uses_the_trade_client_not_the_sentinel_client(self):
        # 哨兵客户端没有 .client 属性：读它会每次 AttributeError → 吞成 debug → 永远兜底
        assert "RedisSentinelClient" not in self.SRC
        assert "get_redis_sentinel_client" not in self.SRC
        assert "backend.services.trade_shared.redis_client" in self.SRC

    def test_reader_and_writer_share_one_client_class(self):
        """读侧与写侧必须同库同实现：换一个客户端就等于换一个库（键在 db2、读在 db0）。"""
        from backend.services.trade.routers.broker_config import _SELECTED_KEY

        assert _SELECTED_KEY.format(market="CN") == rp._SELECTED_BROKER_KEY  # noqa: SLF001
        assert settings.REDIS_DB == int(
            __import__("os").getenv("REDIS_DB_TRADE", "2")
        )  # RedisClient 的库 = 交易库
