"""``qmt_mirror`` 控制面/对账路由单测（假 Redis + 假 DB Session）。

路由本身不碰 Redis 键，全部委托 ``real_mirror_service``，所以这里验证的是
**参数校验、状态回显、审计不炸、对账口径（滑点/费用/未镜像归因）**。
异步用例统一 ``asyncio.run``（容器内无 pytest-asyncio）。
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from backend.services.live_trading.services import real_mirror_service as mirror
from backend.services.trade.routers import qmt_mirror as mod
from backend.tests.test_qmt_exec_mirror import FakeRedisClient


def _redis(**kwargs: Any) -> Any:
    return SimpleNamespace(client=FakeRedisClient(**kwargs))


def _auth() -> Any:
    return SimpleNamespace(tenant_id="default", user_id="1")


class FakeResult:
    def __init__(self, rows: list[Any]):
        self._rows = rows

    def scalars(self) -> FakeResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


class FakeSession:
    """按调用顺序吐出预置结果集（对账端点固定两次 execute）。"""

    def __init__(self, batches: list[list[Any]]):
        self.batches = list(batches)
        self.statements: list[Any] = []

    async def execute(self, stmt: Any) -> FakeResult:
        self.statements.append(stmt)
        return FakeResult(self.batches.pop(0) if self.batches else [])


class TestStatusAndSwitches:
    def test_status_returns_snapshot(self) -> None:
        redis = _redis()
        data = asyncio.run(mod.get_mirror_status(redis=redis, auth=_auth()))
        assert set(data) >= {
            "enabled",
            "env_enabled",
            "kill_switch",
            "whitelist",
            "blacklist",
            "config",
            "quota",
            "queue_length",
            "blocked_reason",
        }
        assert data["enabled"] is False
        assert data["blocked_reason"]

    def test_status_redis_unavailable_raises_503(self) -> None:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(mod.get_mirror_status(redis=None, auth=_auth()))
        assert exc.value.status_code == 503

    def test_enable_and_disable_hot_switch(self) -> None:
        redis = _redis()
        payload = mod.MirrorEnabledUpdate(enabled=True)
        data = asyncio.run(mod.set_mirror_enabled(payload, redis=redis, auth=_auth()))
        assert redis.client.strings[mirror._ENABLED_KEY] == "1"
        assert data["enabled"] is True

        off = mod.MirrorEnabledUpdate(enabled=False)
        data = asyncio.run(mod.set_mirror_enabled(off, redis=redis, auth=_auth()))
        # 显式写 "0"（删键会回落到 env 基线，env=true 时「关闭」等于没关）
        assert redis.client.strings[mirror._ENABLED_KEY] == "0"
        assert data["enabled"] is False

    def test_kill_switch_blocks_even_when_enabled(self) -> None:
        redis = _redis(strings={mirror._ENABLED_KEY: "1"})
        data = asyncio.run(
            mod.set_mirror_kill(
                mod.MirrorKillUpdate(on=True), redis=redis, auth=_auth()
            )
        )
        assert data["kill_switch"] is True
        assert data["enabled"] is False
        assert data["blocked_reason"] == "kill_switch"

        data = asyncio.run(
            mod.set_mirror_kill(
                mod.MirrorKillUpdate(on=False), redis=redis, auth=_auth()
            )
        )
        assert data["kill_switch"] is False


class TestConfigAndLists:
    def test_config_merges_and_normalizes_markets(self) -> None:
        redis = _redis(strings={mirror._CONFIG_KEY: '{"max_daily_value": 20000}'})
        payload = mod.MirrorConfigUpdate(max_order_value=5000, markets=["cn"])
        data = asyncio.run(mod.update_mirror_config(payload, redis=redis, auth=_auth()))
        assert data["config"]["max_order_value"] == 5000
        assert data["config"]["max_daily_value"] == 20000  # 未覆盖项保持
        assert data["config"]["markets"] == ["CN"]
        assert data["config_overrides"]["max_daily_value"] == 20000

    def test_config_empty_payload_rejected(self) -> None:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                mod.update_mirror_config(
                    mod.MirrorConfigUpdate(), redis=_redis(), auth=_auth()
                )
            )
        assert exc.value.status_code == 400

    def test_lists_replace_whitelist_and_blacklist(self) -> None:
        redis = _redis(
            sets={mirror._WHITELIST_KEY: {"old"}, mirror._BLACKLIST_KEY: {"SH600000"}}
        )
        payload = mod.MirrorListsUpdate(
            whitelist=["default:1:42", "default"], blacklist=[]
        )
        data = asyncio.run(mod.update_mirror_lists(payload, redis=redis, auth=_auth()))
        assert data["whitelist"] == ["default", "default:1:42"]
        assert data["blacklist"] == []

    def test_lists_empty_payload_rejected(self) -> None:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                mod.update_mirror_lists(
                    mod.MirrorListsUpdate(), redis=_redis(), auth=_auth()
                )
            )
        assert exc.value.status_code == 400

    def test_drain_delegates(self) -> None:
        redis = _redis()
        with patch.object(
            mirror, "drain_mirror_queue", AsyncMock(return_value={"drained": 2})
        ) as drain:
            result = asyncio.run(
                mod.drain_mirror_queue_endpoint(limit=5, redis=redis, auth=_auth())
            )
        assert result == {"drained": 2}
        assert drain.await_args.kwargs["limit"] == 5

    def test_config_limits_bounded(self) -> None:
        """限额有上限，防手滑填出天文数字后一路下单。"""
        with pytest.raises(ValidationError):
            mod.MirrorConfigUpdate(max_order_value=mod.MAX_ORDER_VALUE_LIMIT + 1)
        with pytest.raises(ValidationError):
            mod.MirrorConfigUpdate(max_daily_value=mod.MAX_DAILY_VALUE_LIMIT + 1)
        with pytest.raises(ValidationError):
            mod.MirrorConfigUpdate(max_slippage_pct=0.5)
        assert mod.MirrorConfigUpdate(max_order_value=1.0).max_order_value == 1.0

    def test_lists_clean_and_bounded(self) -> None:
        data = mod.MirrorListsUpdate(whitelist=[" default:1 ", "", "  "], blacklist=None)
        assert data.whitelist == ["default:1"]
        with pytest.raises(ValidationError):
            mod.MirrorListsUpdate(blacklist=["S" * (mod.MAX_LIST_ITEM_LEN + 1)])
        with pytest.raises(ValidationError):
            mod.MirrorListsUpdate(
                blacklist=[f"S{i}" for i in range(mod.MAX_LIST_ITEMS + 1)]
            )


def _sim(**over: Any) -> Any:
    base: dict[str, Any] = {
        "remarks": "client_order_id=cid-1",
        "symbol": "SH600519",
        "side": "BUY",
        "quantity": 100.0,
        "average_price": 10.0,
        "total_fee": 5.0,
        "status": "filled",
    }
    base.update(over)
    return SimpleNamespace(**base)


def _real(**over: Any) -> Any:
    base: dict[str, Any] = {
        "client_order_id": "mir-cid-1",
        "order_id": "QMT-1",
        "exchange_order_id": "1001",
        "average_price": 10.2,
        "commission": 6.0,
        "status": "FILLED",
        "filled_quantity": 100.0,
        "price": 10.2,
        "remarks": "",
    }
    base.update(over)
    return SimpleNamespace(**base)


class TestReconcile:
    def test_matched_order_reports_slippage_and_fee(self) -> None:
        session = FakeSession([[_sim()], [_real()]])
        data = asyncio.run(
            mod.reconcile_mirror_orders(
                date="2026-09-09", limit=100, db=session, auth=_auth()
            )
        )
        item = data["items"][0]
        assert item["mirrored"] is True
        assert item["real_order_id"] == "QMT-1"
        assert item["slippage"] == pytest.approx(0.2)
        assert item["slippage_pct"] == pytest.approx(0.02)
        assert item["fee_diff"] == pytest.approx(1.0)
        summary = data["summary"]
        assert summary["virtual_orders"] == 1
        assert summary["mirrored"] == 1
        assert summary["filled"] == 1
        assert summary["avg_abs_slippage"] == pytest.approx(0.2)
        assert summary["fee_diff_total"] == pytest.approx(1.0)

    def test_unmirrored_order_explains_why(self) -> None:
        session = FakeSession([[_sim(remarks="client_order_id=cid-9")], []])
        data = asyncio.run(
            mod.reconcile_mirror_orders(
                date="2026-09-09", limit=100, db=session, auth=_auth()
            )
        )
        item = data["items"][0]
        assert item["mirrored"] is False
        assert "未找到真单" in item["note"]
        assert data["summary"]["mirrored"] == 0

    def test_rejected_real_order_counted(self) -> None:
        session = FakeSession(
            [
                [_sim()],
                [
                    _real(
                        status="REJECTED",
                        average_price=0.0,
                        price=0.0,
                        remarks="资金不足",
                    )
                ],
            ]
        )
        data = asyncio.run(
            mod.reconcile_mirror_orders(
                date="2026-09-09", limit=100, db=session, auth=_auth()
            )
        )
        assert data["summary"]["rejected_or_cancelled"] == 1
        assert data["items"][0]["real_message"] == "资金不足"
        assert "slippage" not in data["items"][0]

    def test_bad_date_rejected(self) -> None:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                mod.reconcile_mirror_orders(
                    date="2026/09/09", limit=10, db=FakeSession([]), auth=_auth()
                )
            )
        assert exc.value.status_code == 400

    def test_no_sim_orders_skips_real_lookup(self) -> None:
        session = FakeSession([[]])
        data = asyncio.run(
            mod.reconcile_mirror_orders(
                date="2026-09-09", limit=10, db=session, auth=_auth()
            )
        )
        assert data["items"] == []
        assert len(session.statements) == 1


class TestSimCreatedWindow:
    """虚拟单落库时间比真实时刻早 UTC+8h（naive utcnow 写 timestamptz），窗口须回移。"""

    def test_window_shifted_by_session_offset(self) -> None:
        target = date(2026, 9, 9)
        start, end = mod._sim_created_window(target)
        offset = datetime.now(mod.TZ).utcoffset() or timedelta(0)
        expected_start = datetime.combine(
            target, time.min, tzinfo=mod.TZ
        ).astimezone(timezone.utc) - offset
        assert start == expected_start
        assert end - start == timedelta(days=1)
        # 上海日 00:00 的真实瞬时被回移了 8 小时
        assert start.hour == 8  # 2026-09-08 08:00Z

    def test_window_covers_stored_values(self) -> None:
        """实测样本：8/25 12:21（上海）的单落库为 8/24 20:21Z，必须落在 8/25 的窗口内。"""
        start, end = mod._sim_created_window(date(2026, 8, 25))
        stored = datetime(2026, 8, 24, 20, 21, 19, tzinfo=timezone.utc)
        assert start <= stored < end
