"""真单镜像 ``real_mirror_service`` 单测：风控闸门 + 限额 + 幂等 + 熔断 + 队列。

无真机/真库依赖：QMT RPC、Redis、DB 全部注入假体，验证的是**真钱路径的判定
逻辑**（闸门顺序、拒绝原因、失败回滚、连续拒单熔断），不是第三方库行为。
异步用例统一用 ``asyncio.run``（容器内无 pytest-asyncio）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from backend.services.live_trading.services import real_mirror_service as m


class FakeRedisClient:
    """内存版 Redis（只实现镜像服务用到的命令）。"""

    def __init__(
        self,
        *,
        strings: dict[str, str] | None = None,
        sets: dict[str, set[str]] | None = None,
    ):
        self.strings: dict[str, str] = dict(strings or {})
        self.sets: dict[str, set[str]] = {k: set(v) for k, v in (sets or {}).items()}
        self.lists: dict[str, list[str]] = {}

    # -- string --
    def get(self, key: str) -> str | None:
        return self.strings.get(key)

    def set(self, key: str, value: Any) -> bool:
        self.strings[key] = str(value)
        return True

    def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            removed += 1 if self.strings.pop(key, None) is not None else 0
            removed += 1 if self.sets.pop(key, None) is not None else 0
        return removed

    def incr(self, key: str) -> int:
        value = int(float(self.strings.get(key) or 0)) + 1
        self.strings[key] = str(value)
        return value

    def decr(self, key: str) -> int:
        value = int(float(self.strings.get(key) or 0)) - 1
        self.strings[key] = str(value)
        return value

    def incrbyfloat(self, key: str, amount: float) -> float:
        value = float(self.strings.get(key) or 0) + float(amount)
        self.strings[key] = str(value)
        return value

    def expire(self, key: str, seconds: int) -> bool:
        return True

    # -- set --
    def sadd(self, key: str, *values: str) -> int:
        target = self.sets.setdefault(key, set())
        before = len(target)
        target.update(str(v) for v in values)
        return len(target) - before

    def srem(self, key: str, *values: str) -> int:
        target = self.sets.get(key) or set()
        removed = 0
        for value in values:
            removed += 1 if value in target else 0
            target.discard(str(value))
        return removed

    def scard(self, key: str) -> int:
        return len(self.sets.get(key) or set())

    def sismember(self, key: str, value: str) -> bool:
        return str(value) in (self.sets.get(key) or set())

    def smembers(self, key: str) -> set[str]:
        return set(self.sets.get(key) or set())

    # -- list --
    def rpush(self, key: str, *values: str) -> int:
        target = self.lists.setdefault(key, [])
        target.extend(str(v) for v in values)
        return len(target)

    def lpop(self, key: str) -> str | None:
        target = self.lists.get(key) or []
        return target.pop(0) if target else None

    def llen(self, key: str) -> int:
        return len(self.lists.get(key) or [])

    def eval(self, script: str, numkeys: int, *args: Any) -> list[Any]:
        """按 ``_RESERVE_LUA`` 的语义做最小模拟（真 Lua 已在真实 Redis 验证）。

        返回值统一为 bytes，与真实 redis-py 一致 —— 服务端必须能正确解码。
        """
        keys = args[:numkeys]
        argv = args[numkeys:]
        value, symbol = float(argv[0]), str(argv[1])
        max_order, max_daily, max_orders, max_symbols = (
            float(argv[2]),
            float(argv[3]),
            float(argv[4]),
            float(argv[5]),
        )
        daily_value = float(self.strings.get(keys[0]) or 0)
        daily_orders = float(self.strings.get(keys[1]) or 0)
        symbols = self.sets.get(keys[2]) or set()
        new_flag = 0 if symbol in symbols else 1

        def reject(reason: str) -> list[bytes]:
            return [
                b"0",
                reason.encode(),
                str(daily_value).encode(),
                str(daily_orders).encode(),
                str(len(symbols)).encode(),
                str(new_flag).encode(),
            ]

        if value > max_order:
            return reject("max_order_value")
        if daily_value + value > max_daily:
            return reject("max_daily_value")
        if daily_orders + 1 > max_orders:
            return reject("max_daily_orders")
        if new_flag == 1 and len(symbols) + 1 > max_symbols:
            return reject("max_daily_symbols")
        self.incrbyfloat(keys[0], value)
        self.incr(keys[1])
        self.sadd(keys[2], symbol)
        return [
            b"1",
            b"ok",
            str(daily_value + value).encode(),
            str(daily_orders + 1).encode(),
            str(len(symbols) + new_flag).encode(),
            str(new_flag).encode(),
        ]


def _redis(**kwargs: Any) -> Any:
    return SimpleNamespace(client=FakeRedisClient(**kwargs))


def _cfg(**over: Any) -> m.MirrorConfig:
    base: dict[str, Any] = {"enabled": True, "markets": frozenset({"CN"})}
    base.update(over)
    return m.MirrorConfig(**base)


def _payload(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "client_order_id": "mir-cid-1",
        "tenant_id": "default",
        "user_id": "1",
        "strategy_id": "42",
        "symbol": "SH600519",
        "side": "BUY",
        "quantity": 100.0,
        "price": 10.0,
        "market": "CN",
        "source": "test",
    }
    base.update(over)
    return base


def _mirror(**over: Any) -> dict[str, Any]:
    """调 ``mirror_virtual_fill``（带全套外部依赖假体）。"""
    kw: dict[str, Any] = {
        "db": object(),
        "redis": _redis(),
        "tenant_id": "default",
        "user_id": "1",
        "symbol": "SH600519",
        "side": "BUY",
        "quantity": 100.0,
        "price": 10.0,
        "client_order_id": "cid-1",
        "source": "test",
    }
    kw.update(over)
    return asyncio.run(m.mirror_virtual_fill(**kw))


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
class TestConfig:
    def test_env_baseline_defaults(self, monkeypatch) -> None:
        for name in (
            "SIMULATION_MIRROR_TO_REAL",
            "MIRROR_ENABLED",
            "MIRROR_MAX_ORDER_VALUE",
            "MIRROR_MAX_DAILY_VALUE",
        ):
            monkeypatch.delenv(name, raising=False)
        cfg = m.load_config(None)
        assert cfg.enabled is False
        assert cfg.max_order_value == 10000.0
        assert cfg.max_daily_value == 50000.0
        assert cfg.max_daily_symbols == 5

    def test_env_enables_mirror(self, monkeypatch) -> None:
        monkeypatch.setenv("SIMULATION_MIRROR_TO_REAL", "true")
        assert m.load_config(None).enabled is True

    def test_redis_override_wins(self, monkeypatch) -> None:
        monkeypatch.setenv("MIRROR_MAX_ORDER_VALUE", "10000")
        redis = _redis(
            strings={"mirror:config": '{"max_order_value": 3000, "enabled": true}'}
        )
        cfg = m.load_config(redis)
        assert cfg.max_order_value == 3000.0
        assert cfg.enabled is True

    def test_invalid_override_ignored(self) -> None:
        redis = _redis(strings={"mirror:config": '{"max_order_value": "abc"}'})
        assert m.load_config(redis).max_order_value == 10000.0

    def test_broken_json_falls_back_to_env(self) -> None:
        redis = _redis(strings={"mirror:config": "{not json"})
        assert m.load_config(redis).max_order_value == 10000.0


# --------------------------------------------------------------------------
# 开关 / 白黑名单
# --------------------------------------------------------------------------
class TestGates:
    def test_kill_switch_blocks(self) -> None:
        result = _mirror(
            redis=_redis(strings={"mirror:kill": "1", "mirror:enabled": "1"})
        )
        assert result["status"] == "skipped"
        assert result["reason"] == "mirror_disabled"

    def test_kill_switch_read_failure_fails_closed(self) -> None:
        broken = SimpleNamespace(client=SimpleNamespace(get=lambda key: 1 / 0))
        assert m.kill_switch_on(broken) is True
        result = _mirror(redis=broken)
        assert result["status"] == "skipped"

    def test_disabled_blocks(self) -> None:
        result = _mirror(redis=_redis(strings={"mirror:enabled": "0"}))
        assert result["reason"] == "mirror_disabled"

    def test_whitelist_empty_blocks(self) -> None:
        result = _mirror(redis=_redis(strings={"mirror:enabled": "1"}))
        assert result["reason"] == "whitelist"

    def test_whitelist_tenant_user_strategy_match(self) -> None:
        redis = _redis(
            strings={"mirror:enabled": "1"},
            sets={"mirror:whitelist": {"default:1:42"}},
        )
        with (
            patch.object(m, "_real_trading_ready", return_value=(True, "")),
            patch.object(m, "is_trading_time", return_value=False),
        ):
            result = _mirror(redis=redis, strategy_id="42")
        assert result["status"] == "queued"

    def test_whitelist_wrong_strategy_blocks(self) -> None:
        redis = _redis(
            strings={"mirror:enabled": "1"},
            sets={"mirror:whitelist": {"default:1:99"}},
        )
        assert _mirror(redis=redis, strategy_id="42")["reason"] == "whitelist"

    def test_blacklist_blocks(self) -> None:
        redis = _redis(
            strings={"mirror:enabled": "1"},
            sets={
                "mirror:whitelist": {"*"},
                "mirror:blacklist": {"SH600519"},
            },
        )
        assert _mirror(redis=redis)["reason"] == "blacklist"

    def test_market_not_supported(self) -> None:
        redis = _redis(
            strings={"mirror:enabled": "1"}, sets={"mirror:whitelist": {"*"}}
        )
        assert _mirror(redis=redis, market="US")["reason"] == "market_not_supported:US"

    def test_broker_not_ready(self) -> None:
        redis = _redis(
            strings={"mirror:enabled": "1"}, sets={"mirror:whitelist": {"*"}}
        )
        with patch.object(
            m, "_real_trading_ready", return_value=(False, "broker_not_qmt_exec:tdx")
        ):
            result = _mirror(redis=redis)
        assert result["reason"] == "broker_not_qmt_exec:tdx"

    def test_invalid_side_and_quantity(self) -> None:
        assert _mirror(side="HOLD")["reason"] == "invalid_side"
        assert _mirror(quantity=0)["reason"] == "invalid_quantity_or_price"
        assert _mirror(price=0)["reason"] == "invalid_quantity_or_price"


# --------------------------------------------------------------------------
# 交易时段 / 队列
# --------------------------------------------------------------------------
class TestQueue:
    def _open_redis(self) -> Any:
        return _redis(strings={"mirror:enabled": "1"}, sets={"mirror:whitelist": {"*"}})

    def test_outside_hours_queues(self) -> None:
        redis = self._open_redis()
        with (
            patch.object(m, "_real_trading_ready", return_value=(True, "")),
            patch.object(m, "is_trading_time", return_value=False),
        ):
            result = _mirror(redis=redis)
        assert result["status"] == "queued"
        assert redis.client.lists["mirror:queue"]

    def test_queue_is_idempotent(self) -> None:
        redis = self._open_redis()
        with (
            patch.object(m, "_real_trading_ready", return_value=(True, "")),
            patch.object(m, "is_trading_time", return_value=False),
        ):
            first = _mirror(redis=redis)
            second = _mirror(redis=redis)
        assert first["status"] == "queued"
        assert second["reason"] == "queue_duplicate"
        assert len(redis.client.lists["mirror:queue"]) == 1

    def test_queue_disabled_skips(self) -> None:
        redis = self._open_redis()
        with (
            patch.object(m, "_real_trading_ready", return_value=(True, "")),
            patch.object(m, "is_trading_time", return_value=False),
            patch.object(
                m, "load_config", return_value=_cfg(queue_outside_hours=False)
            ),
        ):
            result = _mirror(redis=redis)
        assert result["reason"] == "outside_trading_hours"

    def test_drain_skips_outside_hours(self) -> None:
        redis = self._open_redis()
        with patch.object(m, "is_trading_time", return_value=False):
            result = asyncio.run(m.drain_mirror_queue(redis))
        assert result["status"] == "outside_trading_hours"

    def test_drain_requeues_on_session_boundary(self) -> None:
        redis = self._open_redis()
        redis.client.rpush("mirror:queue", '{"client_order_id": "mir-x"}')
        with (
            patch.object(m, "is_trading_time", return_value=True),
            patch.object(m, "_queued_entry_blocked", return_value=""),
            patch.object(
                m,
                "_submit_payload",
                AsyncMock(return_value={"status": "queued", "reason": "lunch"}),
            ),
        ):
            result = asyncio.run(m.drain_mirror_queue(redis, db=object()))
        assert result["requeued"] == 1
        assert redis.client.lists["mirror:queue"]

    def test_drain_drops_entry_failing_recheck(self) -> None:
        """入队后名单/通道可能被改：补交前复核不过的条目直接丢弃，不提交。"""
        redis = self._open_redis()
        redis.client.rpush("mirror:queue", '{"client_order_id": "mir-x"}')
        submit = AsyncMock(return_value={"status": "submitted"})
        with (
            patch.object(m, "is_trading_time", return_value=True),
            patch.object(m, "_queued_entry_blocked", return_value="whitelist"),
            patch.object(m, "_submit_payload", submit),
        ):
            result = asyncio.run(m.drain_mirror_queue(redis, db=object()))
        assert result["dropped"] == 1
        assert result["submitted"] == 0
        submit.assert_not_awaited()
        assert not redis.client.lists.get("mirror:queue")


# --------------------------------------------------------------------------
# 提交链路
# --------------------------------------------------------------------------
class TestSubmit:
    def _submit(self, redis: Any, **over: Any) -> dict[str, Any]:
        with (
            patch.object(m, "_reference_price", AsyncMock(return_value=10.0)),
            patch.object(
                m,
                "_account_snapshot",
                AsyncMock(
                    return_value={
                        "cash": 1_000_000.0,
                        "total_asset": 1_000_000.0,
                        "available_volume": {"SH600519": 1000.0},
                    }
                ),
            ),
        ):
            return asyncio.run(
                m._submit_payload(
                    db=object(), redis=redis, cfg=_cfg(), payload=_payload(**over)
                )
            )

    def _open_redis(self) -> Any:
        return _redis(sets={"mirror:whitelist": {"*"}})

    def test_price_drift_skipped(self) -> None:
        with patch.object(m, "_reference_price", AsyncMock(return_value=12.0)):
            result = asyncio.run(
                m._submit_payload(
                    db=object(),
                    redis=self._open_redis(),
                    cfg=_cfg(),
                    payload=_payload(price=10.0),
                )
            )
        assert result["reason"] == "price_drift"

    def test_insufficient_cash(self) -> None:
        redis = self._open_redis()
        with (
            patch.object(m, "_reference_price", AsyncMock(return_value=10.0)),
            patch.object(
                m,
                "_account_snapshot",
                AsyncMock(return_value={"cash": 100.0, "available_volume": {}}),
            ),
        ):
            result = asyncio.run(
                m._submit_payload(
                    db=object(), redis=redis, cfg=_cfg(), payload=_payload()
                )
            )
        assert result["reason"] == "insufficient_cash"

    def test_insufficient_position(self) -> None:
        redis = self._open_redis()
        with (
            patch.object(m, "_reference_price", AsyncMock(return_value=10.0)),
            patch.object(
                m,
                "_account_snapshot",
                AsyncMock(
                    return_value={"cash": 1e6, "available_volume": {"SH600519": 1.0}}
                ),
            ),
        ):
            result = asyncio.run(
                m._submit_payload(
                    db=object(),
                    redis=redis,
                    cfg=_cfg(),
                    payload=_payload(side="SELL", quantity=100.0),
                )
            )
        assert result["reason"] == "insufficient_position"

    def test_account_unavailable(self) -> None:
        from backend.services.live_trading.services.qmt_exec_client import QmtExecError

        redis = self._open_redis()
        with (
            patch.object(m, "_reference_price", AsyncMock(return_value=10.0)),
            patch.object(
                m,
                "_account_snapshot",
                AsyncMock(side_effect=QmtExecError("桥未连接", code="NOT_CONNECTED")),
            ),
        ):
            result = asyncio.run(
                m._submit_payload(
                    db=object(), redis=redis, cfg=_cfg(), payload=_payload()
                )
            )
        assert result["reason"] == "account_unavailable:NOT_CONNECTED"

    def test_quota_reject(self) -> None:
        redis = self._open_redis()
        with patch.object(
            m,
            "_reserve_quota",
            return_value=(False, "max_daily_value", {"daily_value": 50000.0}),
        ):
            result = self._submit(redis)
        assert result["status"] == "skipped"
        assert result["reason"] == "max_daily_value"

    def test_submit_success_maps_order_params(self) -> None:
        redis = self._open_redis()
        dispatch = AsyncMock(return_value={"status": "success", "order_id": "1001"})
        notify = SimpleNamespace(calls=[])

        def fake_notify(**kwargs: Any) -> None:
            notify.calls.append(kwargs)

        with (
            patch(
                "backend.services.live_trading.services.internal_strategy_dispatcher"
                ".dispatch_internal_strategy_order",
                dispatch,
            ),
            patch.object(m, "notify", side_effect=fake_notify),
        ):
            result = self._submit(redis)

        assert result["status"] == "submitted"
        assert result["order_id"] == "1001"
        # 买入限价 = 参考价 × (1 + 滑点) 并保留两位
        assert result["limit_price"] == 10.2
        assert result["order_value"] == 1020.0
        kwargs = dispatch.await_args.kwargs
        assert kwargs["order_data"]["trading_mode"] == "REAL"
        assert kwargs["order_data"]["client_order_id"] == "mir-cid-1"
        assert kwargs["order_data"]["order_type"] == "LIMIT"
        assert kwargs["order_data"]["side"] == "BUY"
        assert kwargs["tenant_id"] == "default"
        assert notify.calls, "提交成功应推送通知"

    def test_submit_failure_releases_quota(self) -> None:
        redis = self._open_redis()
        release = SimpleNamespace(calls=[])

        def fake_release(*args: Any, **kwargs: Any) -> None:
            release.calls.append(kwargs)

        with (
            patch(
                "backend.services.live_trading.services.internal_strategy_dispatcher"
                ".dispatch_internal_strategy_order",
                AsyncMock(return_value={"status": "rejected", "detail": "风控拒单"}),
            ),
            patch.object(m, "_release_quota", side_effect=fake_release),
            patch.object(m, "_record_reject") as record_reject,
        ):
            result = self._submit(redis)

        assert result["status"] == "failed"
        assert result["reason"] == "rejected"
        assert release.calls and release.calls[0]["was_new_symbol"] is True
        record_reject.assert_called_once()

    def test_submit_exception_releases_quota(self) -> None:
        redis = self._open_redis()
        with (
            patch(
                "backend.services.live_trading.services.internal_strategy_dispatcher"
                ".dispatch_internal_strategy_order",
                AsyncMock(side_effect=RuntimeError("boom")),
            ),
            patch.object(m, "_release_quota") as release,
            patch.object(m, "_record_reject") as record_reject,
        ):
            result = self._submit(redis)
        assert result["status"] == "failed"
        release.assert_called_once()
        record_reject.assert_called_once()


# --------------------------------------------------------------------------
# 限额 / 熔断 / 幂等键
# --------------------------------------------------------------------------
class TestQuotaAndBreaker:
    def test_reserve_quota_parses_bytes_result(self) -> None:
        redis = _redis()
        ok, reason, snapshot = m._reserve_quota(
            redis, _cfg(), symbol="SH600519", value=9000.0
        )
        assert ok is True and reason == "ok"
        assert snapshot["daily_value"] == 9000.0
        assert snapshot["new_symbol"] == 1.0

    def test_reserve_quota_rejects_over_limit(self) -> None:
        redis = _redis()
        ok, reason, _ = m._reserve_quota(
            redis, _cfg(), symbol="SH600519", value=20000.0
        )
        assert ok is False and reason == "max_order_value"

    def test_reserve_quota_redis_unavailable(self) -> None:
        ok, reason, _ = m._reserve_quota(None, _cfg(), symbol="SH600519", value=1.0)
        assert ok is False and reason == "redis_unavailable"

    def test_daily_symbols_limit(self) -> None:
        redis = _redis()
        cfg = _cfg(max_daily_symbols=1)
        assert m._reserve_quota(redis, cfg, symbol="SH600519", value=1000.0)[0] is True
        ok, reason, _ = m._reserve_quota(redis, cfg, symbol="SH600036", value=1000.0)
        assert ok is False and reason == "max_daily_symbols"

    def test_release_quota_rolls_back(self) -> None:
        redis = _redis()
        m._reserve_quota(redis, _cfg(), symbol="SH600519", value=9000.0)
        m._release_quota(
            redis, _cfg(), symbol="SH600519", value=9000.0, was_new_symbol=True
        )
        assert float(redis.client.strings[m._daily_key("value")]) == 0.0
        assert redis.client.scard(m._daily_key("symbols")) == 0

    def test_circuit_breaker_sets_kill_switch(self) -> None:
        redis = _redis()
        cfg = _cfg(max_consecutive_rejects=3)
        with patch.object(m, "notify") as notify:
            for _ in range(3):
                m._record_reject(redis, cfg, reason="rejected")
        assert redis.client.strings["mirror:kill"] == "1"
        assert notify.called

    def test_circuit_breaker_disabled_when_zero(self) -> None:
        redis = _redis()
        with patch.object(m, "notify"):
            for _ in range(5):
                m._record_reject(redis, _cfg(max_consecutive_rejects=0), reason="x")
        assert "mirror:kill" not in redis.client.strings

    def test_success_clears_reject_counter(self) -> None:
        redis = _redis(strings={"mirror:rejects": "2"})
        m._record_success(redis)
        assert "mirror:rejects" not in redis.client.strings

    def test_client_order_id_prefix_and_truncation(self) -> None:
        assert m.build_mirror_client_order_id(client_order_id="cid-1") == "mir-cid-1"
        long_cid = "x" * 200
        built = m.build_mirror_client_order_id(client_order_id=long_cid)
        assert built.startswith("mir-")
        assert len(built) == m._MAX_CLIENT_ORDER_ID_LEN

    def test_client_order_id_falls_back_to_sim_order(self) -> None:
        assert (
            m.build_mirror_client_order_id(sim_order_id="9001", symbol="SH600519")
            == "mir-9001"
        )
        assert (
            m.build_mirror_client_order_id(
                run_id="sim_20260909", symbol="SH600519", side="SELL"
            )
            == "mir-sim_20260909-SH600519-SELL"
        )

    def test_set_and_clear_kill_switch(self) -> None:
        redis = _redis()
        m.set_kill_switch(redis, True)
        assert redis.client.strings["mirror:kill"] == "1"
        m.set_kill_switch(redis, False)
        assert "mirror:kill" not in redis.client.strings


class TestRealTradingGate:
    def test_disabled_by_default(self) -> None:
        redis = _redis()
        with patch(
            "backend.services.trade_shared.trade_config.settings.ENABLE_REAL_TRADING",
            False,
            create=True,
        ):
            ready, reason = m._real_trading_ready(redis, "CN")
        assert ready is False
        assert reason == "real_trading_disabled"

    def test_requires_qmt_exec_selected(self) -> None:
        redis = _redis(strings={"broker:selected:CN": "tdx"})
        with patch(
            "backend.services.trade_shared.trade_config.settings.ENABLE_REAL_TRADING",
            True,
            create=True,
        ):
            ready, reason = m._real_trading_ready(redis, "CN")
        assert ready is False
        assert reason == "broker_not_qmt_exec:tdx"

    def test_ready_when_enabled_and_selected(self) -> None:
        redis = _redis(strings={"broker:selected:CN": "qmt_exec"})
        with patch(
            "backend.services.trade_shared.trade_config.settings.ENABLE_REAL_TRADING",
            True,
            create=True,
        ):
            ready, reason = m._real_trading_ready(redis, "CN")
        assert ready is True and reason == ""
