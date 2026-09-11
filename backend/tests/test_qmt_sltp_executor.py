"""QMT 止盈/止损执行器单测（fake 客户端/Redis/派发，无真机、无真库）。

覆盖计划 §1.6 六组：
1. 触发（止损/止盈/移动止损 + highest 只升不降）
2. 数量（全量碎股/部分整手/科创 200/北交所 100/可用 0 跳过）
3. 保护价（跌停价 / fail-closed / market 模式）
4. 状态机（一次/日、reset 后重新武装、下单失败落 failed）
5. 跟踪（超时只通知一次、终态通知含成交/剩余量）
6. 开关（enabled=false 空转不下单）
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from backend.services.live_trading.services import sltp_executor as ex
from backend.services.live_trading.services.lot_rules import (
    align_sell_quantity,
    describe_violation,
    resolve_board,
)

DAY = "20260911"


# --------------------------------------------------------------------------
# 替身
# --------------------------------------------------------------------------
class FakeRedis:
    def __init__(self, cfg: dict | None = None, state: dict | None = None) -> None:
        self.store: dict = {}
        if cfg is not None:
            self.store[ex.CONFIG_KEY] = cfg
        if state is not None:
            self.store[ex.STATE_KEY] = state

    def get(self, key: str):
        return self.store.get(key)

    def set(self, key: str, value) -> None:
        self.store[key] = value


class FakeClient:
    def __init__(
        self,
        *,
        ticks: dict | None = None,
        details: dict | None = None,
        positions: list | None = None,
        configured: bool = True,
        default_detail: dict | None = None,
    ) -> None:
        self.configured = configured
        self.ticks = ticks or {}
        self.details = details or {}
        self.positions = positions or []
        self.default_detail = default_detail if default_detail is not None else {"DownStopPrice": 88.0}
        self.detail_calls: list[str] = []
        self.tick_calls: list[list[str]] = []

    async def get_full_tick(self, codes: list[str]) -> dict:
        self.tick_calls.append(list(codes))
        return {code: self.ticks[code] for code in codes if code in self.ticks}

    async def get_instrument_detail(self, code: str) -> dict:
        self.detail_calls.append(code)
        detail = self.details.get(code)
        return dict(detail if detail is not None else self.default_detail)

    async def get_positions(self) -> list:
        return list(self.positions)


class Harness:
    def __init__(
        self,
        *,
        cfg: dict,
        ticks: dict | None = None,
        details: dict | None = None,
        positions: list | None = None,
        dispatch_result: dict | None = None,
        fallback: dict | None = None,
        default_detail: dict | None = None,
    ) -> None:
        self.redis = FakeRedis(cfg=ex.merge_config(cfg))
        self.client = FakeClient(
            ticks=ticks, details=details, positions=positions, default_detail=default_detail
        )
        self.dispatched: list[dict] = []
        self.notices: list[dict] = []
        self.orders: dict[str, dict] = {}
        self.dispatch_result = dispatch_result or {"status": "success", "order_id": "OID-1"}
        self.fallback = fallback or {"stop_loss_pct": None, "take_profit_pct": None, "trailing_stop_pct": None}
        self.now = 1_000_000.0
        self.cancelled: list[str] = []
        self.cancel_result = True
        self.cancel_hook = None

    def deps(self) -> ex.SltpDeps:
        async def dispatch(order_data: dict, user_id: str) -> dict:
            self.dispatched.append(order_data)
            return dict(self.dispatch_result)

        async def notify(user_id: str, title: str, content: str, level: str = "info", tenant_id: str = "default") -> None:
            self.notices.append(
                {"user_id": user_id, "title": title, "content": content, "level": level}
            )

        async def order_reader(order_id: str):
            return self.orders.get(order_id)

        async def cancel_order(order_id: str) -> bool:
            if self.cancel_hook is not None:
                self.cancel_hook(order_id)
            self.cancelled.append(order_id)
            return self.cancel_result

        return ex.SltpDeps(
            client=self.client,
            redis=self.redis,
            dispatch=dispatch,
            notify=notify,
            order_reader=order_reader,
            now=lambda: self.now,
            fallback_config=lambda tenant, user: self.fallback,
            cancel_order=cancel_order,
        )

    def cycle(self) -> dict:
        with (
            patch.object(ex, "is_trading_time", return_value=True),
            patch.object(ex, "trade_date_str", return_value=DAY),
        ):
            return asyncio.run(ex.run_sltp_cycle(self.deps()))

    def state(self) -> dict:
        with patch.object(ex, "trade_date_str", return_value=DAY):
            return ex.load_state(self.redis)

    def reset(self, symbols: list[str] | None = None) -> dict:
        with patch.object(ex, "trade_date_str", return_value=DAY):
            return ex.reset_rules(self.redis, symbols)


def _cfg(rules: list[dict], **over) -> dict:
    base = {
        "enabled": True,
        "user_id": "1",
        "protect_price_mode": "limit_floor",
        "pending_alert_sec": 180,
        "rules": rules,
    }
    base.update(over)
    return base


def _rule(symbol: str = "600036.SH", **over) -> dict:
    rule = {"symbol": symbol, "entry_price": 100.0, "stop_loss_pct": 0.05}
    rule.update(over)
    return rule


def _position(symbol: str = "600036.SH", can_use: float = 1000, cost: float = 100.0) -> dict:
    return {"stock_code": symbol, "symbol": symbol, "can_use_volume": can_use, "open_price": cost, "avg_price": cost}


# --------------------------------------------------------------------------
# 1. 触发
# --------------------------------------------------------------------------
class TestTrigger:
    def test_stop_loss_triggers_and_dispatches(self) -> None:
        h = Harness(cfg=_cfg([_rule()]), ticks={"600036.SH": {"lastPrice": 94.9}}, positions=[_position()])
        summary = h.cycle()
        assert summary["triggered"] == 1
        assert summary["submitted"] == 1
        assert len(h.dispatched) == 1
        order = h.dispatched[0]
        assert order["side"] == "SELL"
        assert order["trading_mode"] == "REAL"
        assert order["remarks"].startswith("sltp:")
        assert h.state()["rules"]["600036.SH"]["status"] == ex.ST_SUBMITTED

    def test_no_trigger_above_stop_line(self) -> None:
        h = Harness(cfg=_cfg([_rule()]), ticks={"600036.SH": {"lastPrice": 95.1}}, positions=[_position()])
        summary = h.cycle()
        assert summary["triggered"] == 0
        assert h.dispatched == []
        assert h.state()["rules"]["600036.SH"]["status"] == ex.ST_ARMED

    def test_take_profit_triggers(self) -> None:
        h = Harness(
            cfg=_cfg([_rule(stop_loss_pct=None, take_profit_pct=0.10)]),
            ticks={"600036.SH": {"lastPrice": 111.0}},
            positions=[_position()],
        )
        h.cycle()
        assert len(h.dispatched) == 1
        assert "止盈" in h.state()["rules"]["600036.SH"]["reason"]

    def test_trailing_uses_highest_price_monotonic(self) -> None:
        h = Harness(
            cfg=_cfg([_rule(stop_loss_pct=None, trailing_stop_pct=0.05)]),
            positions=[_position()],
        )
        # 第一轮冲高到 110：最高价跟涨，未触发（110 > 110×0.95）
        h.client.ticks = {"600036.SH": {"lastPrice": 110.0}}
        h.cycle()
        assert h.dispatched == []
        assert h.state()["rules"]["600036.SH"]["highest_price"] == 110.0
        # 第二轮回落到 103：触发（103 ≤ 104.5），最高价不回落
        h.client.ticks = {"600036.SH": {"lastPrice": 103.0}}
        h.cycle()
        assert len(h.dispatched) == 1
        assert h.state()["rules"]["600036.SH"]["highest_price"] == 110.0
        assert "移动止损" in h.state()["rules"]["600036.SH"]["reason"]

    def test_tick_missing_increments_miss_counter(self) -> None:
        h = Harness(cfg=_cfg([_rule()]), ticks={}, positions=[_position()])
        h.cycle()
        assert h.state()["rules"]["600036.SH"]["misses"] == 1
        assert h.dispatched == []


# --------------------------------------------------------------------------
# 2. 数量（含 lot_rules 纯函数）
# --------------------------------------------------------------------------
class TestQuantity:
    def test_board_resolution(self) -> None:
        assert resolve_board("600036.SH") == "MAIN"
        assert resolve_board("SH600036") == "MAIN"
        assert resolve_board("300750.SZ") == "GEM"
        assert resolve_board("688596.SH") == "STAR"
        assert resolve_board("920950.BJ") == "BJ"
        assert resolve_board("835185.BJ") == "BJ"

    def test_full_position_sell_allows_odd_lot(self) -> None:
        qty, note = align_sell_quantity("600036.SH", 0, 246)
        assert (qty, note) == (246, "")

    def test_partial_sell_aligned_to_lot(self) -> None:
        qty, note = align_sell_quantity("600036.SH", 246, 1000)
        assert qty == 200
        assert "整手" in note

    def test_star_partial_under_200_sells_all(self) -> None:
        qty, note = align_sell_quantity("688596.SH", 100, 1000)
        assert qty == 1000
        assert "全量" in note

    def test_star_partial_over_200_keeps_quantity(self) -> None:
        qty, _ = align_sell_quantity("688596.SH", 300, 1000)
        assert qty == 300

    def test_bj_min_lot_100(self) -> None:
        qty, note = align_sell_quantity("920950.BJ", 30, 1000)
        assert qty == 100
        assert "最少 100" in note

    def test_can_use_zero_skips_with_notice(self) -> None:
        h = Harness(
            cfg=_cfg([_rule()]),
            ticks={"600036.SH": {"lastPrice": 90.0}},
            positions=[_position(can_use=0)],
        )
        h.cycle()
        assert h.dispatched == []
        st = h.state()["rules"]["600036.SH"]
        assert st["status"] == ex.ST_SKIPPED
        assert "T+1" in st["skip_reason"]
        assert any("触发未卖出" in n["title"] for n in h.notices)

    def test_describe_violation_covers_boards(self) -> None:
        assert describe_violation("600036.SH", "SELL", 246) is not None
        assert describe_violation("600036.SH", "SELL", 246, full_position_sell=True) is None
        assert describe_violation("688596.SH", "SELL", 100) is not None
        assert describe_violation("600036.SH", "BUY", 50) is not None


# --------------------------------------------------------------------------
# 3. 保护价
# --------------------------------------------------------------------------
class TestProtectPrice:
    def test_uses_down_stop_price(self) -> None:
        h = Harness(
            cfg=_cfg([_rule()]),
            ticks={"600036.SH": {"lastPrice": 90.0}},
            details={"600036.SH": {"DownStopPrice": 85.5, "UpStopPrice": 104.5}},
            positions=[_position()],
        )
        h.cycle()
        order = h.dispatched[0]
        assert order["order_type"] == "LIMIT"
        assert order["price"] == 85.5

    def test_missing_down_stop_fails_closed(self) -> None:
        h = Harness(
            cfg=_cfg([_rule()]),
            ticks={"600036.SH": {"lastPrice": 90.0}},
            details={},
            positions=[_position()],
            default_detail={},
        )
        summary = h.cycle()
        assert h.dispatched == []
        assert summary["failed"] == 1
        st = h.state()["rules"]["600036.SH"]
        assert st["status"] == ex.ST_FAILED
        assert "DownStopPrice" in st["skip_reason"]
        assert h.notices and h.notices[-1]["level"] == "error"

    def test_market_mode_skips_detail_lookup(self) -> None:
        h = Harness(
            cfg=_cfg([_rule()], protect_price_mode="market"),
            ticks={"600036.SH": {"lastPrice": 90.0}},
            positions=[_position()],
        )
        h.cycle()
        assert h.client.detail_calls == []
        assert h.dispatched[0]["order_type"] == "MARKET"

    def test_resolve_protect_price_pure(self) -> None:
        assert ex.resolve_protect_price("market", None, 10.0)[0] == "MARKET"
        assert ex.resolve_protect_price("limit_floor", {"DownStopPrice": 9.5}, 10.0) == (
            "LIMIT",
            9.5,
            "跌停保护价 9.50",
        )
        assert ex.resolve_protect_price("limit_floor", {}, 10.0)[0] is None
        assert ex.resolve_protect_price("limit_floor", {"DownStopPrice": 0}, 10.0)[0] is None


# --------------------------------------------------------------------------
# 4. 状态机
# --------------------------------------------------------------------------
class TestStateMachine:
    def test_once_per_day(self) -> None:
        h = Harness(cfg=_cfg([_rule()]), ticks={"600036.SH": {"lastPrice": 90.0}}, positions=[_position()])
        h.cycle()
        h.cycle()
        h.cycle()
        assert len(h.dispatched) == 1

    def test_reset_rearms(self) -> None:
        h = Harness(cfg=_cfg([_rule()]), ticks={"600036.SH": {"lastPrice": 90.0}}, positions=[_position()])
        h.cycle()
        h.reset()
        h.cycle()
        assert len(h.dispatched) == 2

    def test_state_resets_on_new_day(self) -> None:
        h = Harness(cfg=_cfg([_rule()]), ticks={"600036.SH": {"lastPrice": 90.0}}, positions=[_position()])
        h.cycle()
        with (
            patch.object(ex, "is_trading_time", return_value=True),
            patch.object(ex, "trade_date_str", return_value="2026-09-12"),
        ):
            asyncio.run(ex.run_sltp_cycle(h.deps()))
        assert len(h.dispatched) == 2

    def test_dispatch_failure_marks_failed(self) -> None:
        h = Harness(
            cfg=_cfg([_rule()]),
            ticks={"600036.SH": {"lastPrice": 90.0}},
            positions=[_position()],
            dispatch_result={"status": "failed", "message": "柜台拒绝"},
        )
        summary = h.cycle()
        assert summary["failed"] == 1
        st = h.state()["rules"]["600036.SH"]
        assert st["status"] == ex.ST_FAILED
        assert "柜台拒绝" in st["failure"]
        assert any(n["level"] == "error" for n in h.notices)

    def test_entry_price_from_position_cost(self) -> None:
        h = Harness(
            cfg=_cfg([_rule(entry_price=None)]),
            ticks={"600036.SH": {"lastPrice": 90.0}},
            positions=[_position(cost=95.0)],
        )
        h.cycle()
        # 95×0.95=90.25，现价 90 ≤ 90.25 → 触发
        assert len(h.dispatched) == 1
        assert h.state()["rules"]["600036.SH"]["entry_price"] == 95.0

    def test_missing_entry_notifies_and_skips(self) -> None:
        h = Harness(
            cfg=_cfg([_rule(entry_price=None)]),
            ticks={"600036.SH": {"lastPrice": 90.0}},
            positions=[],
        )
        h.cycle()
        assert h.dispatched == []
        assert any("缺少成本价" in n["title"] for n in h.notices)


# --------------------------------------------------------------------------
# 5. 跟踪
# --------------------------------------------------------------------------
class TestTracking:
    def _submit(self, h: Harness) -> None:
        h.cycle()
        assert len(h.dispatched) == 1

    def test_unfilled_alerts_once(self) -> None:
        h = Harness(cfg=_cfg([_rule()]), ticks={"600036.SH": {"lastPrice": 90.0}}, positions=[_position()])
        self._submit(h)
        order_id = h.state()["rules"]["600036.SH"]["order_id"]
        h.orders[order_id] = {"status": "submitted", "filled_quantity": 0, "average_price": 0}
        h.now += 200
        h.cycle()
        h.cycle()
        alerts = [n for n in h.notices if "未成交" in n["title"]]
        assert len(alerts) == 1
        assert "180" in alerts[0]["content"] or "已挂" in alerts[0]["content"]

    def test_terminal_notify_reports_filled_and_remaining(self) -> None:
        h = Harness(cfg=_cfg([_rule()]), ticks={"600036.SH": {"lastPrice": 90.0}}, positions=[_position(can_use=200)])
        self._submit(h)
        order_id = h.state()["rules"]["600036.SH"]["order_id"]
        h.orders[order_id] = {"status": "cancelled", "filled_quantity": 100, "average_price": 89.5}
        h.now += 30
        h.cycle()
        st = h.state()["rules"]["600036.SH"]
        assert st["status"] == ex.ST_CANCELLED
        terminal = [n for n in h.notices if "撤销" in n["title"]]
        assert terminal and "剩余 100" in terminal[0]["content"]
        # 已终态不再重复通知
        h.notices.clear()
        h.cycle()
        assert [n for n in h.notices if "撤销" in n["title"]] == []

    def test_filled_state_reported(self) -> None:
        h = Harness(cfg=_cfg([_rule()]), ticks={"600036.SH": {"lastPrice": 90.0}}, positions=[_position(can_use=100)])
        self._submit(h)
        order_id = h.state()["rules"]["600036.SH"]["order_id"]
        h.orders[order_id] = {"status": "filled", "filled_quantity": 100, "average_price": 90.1}
        h.cycle()
        st = h.state()["rules"]["600036.SH"]
        assert st["status"] == ex.ST_FILLED
        assert any("全部成交" in n["title"] for n in h.notices)


# --------------------------------------------------------------------------
# 6. 开关
# --------------------------------------------------------------------------
class TestSwitch:
    def test_disabled_does_nothing(self) -> None:
        h = Harness(
            cfg=_cfg([_rule()], enabled=False),
            ticks={"600036.SH": {"lastPrice": 80.0}},
            positions=[_position()],
        )
        summary = h.cycle()
        assert summary["enabled"] is False
        assert h.client.tick_calls == []
        assert h.dispatched == []

    def test_empty_rules_no_tick_call(self) -> None:
        h = Harness(cfg=_cfg([]), positions=[_position()])
        h.cycle()
        assert h.client.tick_calls == []

    def test_outside_trading_hours_no_dispatch_but_monitor_runs(self) -> None:
        h = Harness(cfg=_cfg([_rule()]), ticks={"600036.SH": {"lastPrice": 80.0}}, positions=[_position()])
        h.cycle()  # 提交
        order_id = h.state()["rules"]["600036.SH"]["order_id"]
        h.orders[order_id] = {"status": "filled", "filled_quantity": 1000, "average_price": 88.0}
        h.notices.clear()
        with (
            patch.object(ex, "is_trading_time", return_value=False),
            patch.object(ex, "trade_date_str", return_value=DAY),
        ):
            asyncio.run(ex.run_sltp_cycle(h.deps()))
        assert any("全部成交" in n["title"] for n in h.notices)


# --------------------------------------------------------------------------
# 配置归一化
# --------------------------------------------------------------------------
class TestConfig:
    def test_normalize_symbol_and_rules(self) -> None:
        cfg = ex.merge_config(
            {
                "enabled": True,
                "rules": [
                    {"symbol": "SH600036", "stop_loss_pct": "0.05"},
                    {"symbol": "", "stop_loss_pct": 0.1},
                    "not-a-dict",
                ],
            }
        )
        assert [r["symbol"] for r in cfg["rules"]] == ["600036.SH"]
        assert cfg["rules"][0]["stop_loss_pct"] == 0.05

    def test_rule_overrides_fallback(self) -> None:
        rule = ex.normalize_rule({"symbol": "600036.SH", "stop_loss_pct": 0.03})
        merged = ex.trigger_config(rule, {"stop_loss_pct": 0.08, "take_profit_pct": 0.2})
        assert merged["stop_loss_pct"] == 0.03
        assert merged["take_profit_pct"] == 0.2

    def test_highest_price_only_rises(self) -> None:
        assert ex.update_highest_price(10.0, 9.0) == 10.0
        assert ex.update_highest_price(10.0, 11.0) == 11.0
        assert ex.update_highest_price(None, 11.0) == 11.0

    def test_legacy_alert_key_alias(self) -> None:
        cfg = ex.merge_config({"unfilled_alert_sec": 90})
        assert cfg["pending_alert_sec"] == 90

    def test_invalid_remainder_policy_falls_back(self) -> None:
        cfg = ex.merge_config({"remainder_policy": "bogus"})
        assert cfg["remainder_policy"] == "alert_only"
        assert ex.merge_config({"remainder_policy": "CANCEL"})["remainder_policy"] == "cancel"

    def test_seconds_to_close(self) -> None:
        from datetime import datetime

        before = datetime(2026, 9, 11, 14, 55, 0, tzinfo=ex.TZ)
        assert ex.seconds_to_close(before.timestamp()) == 300.0
        after = datetime(2026, 9, 11, 15, 30, 0, tzinfo=ex.TZ)
        assert ex.seconds_to_close(after.timestamp()) < 0


# --------------------------------------------------------------------------
# 7. 未成交余量策略（Phase 3.2）
# --------------------------------------------------------------------------
class TestRemainderPolicy:
    def _submit_pending(self, h: Harness) -> str:
        h.cycle()
        assert len(h.dispatched) == 1
        order_id = h.state()["rules"]["600036.SH"]["order_id"]
        h.orders[order_id] = {"status": "submitted", "filled_quantity": 0, "average_price": 0}
        return order_id

    def test_alert_only_never_cancels(self) -> None:
        h = Harness(cfg=_cfg([_rule()]), ticks={"600036.SH": {"lastPrice": 90.0}}, positions=[_position()])
        self._submit_pending(h)
        h.now += 200
        h.cycle()
        assert h.cancelled == []
        assert h.state()["rules"]["600036.SH"]["remainder_applied"] is True

    def test_cancel_policy_cancels_remaining(self) -> None:
        h = Harness(
            cfg=_cfg([_rule()], remainder_policy="cancel"),
            ticks={"600036.SH": {"lastPrice": 90.0}},
            positions=[_position()],
        )
        order_id = self._submit_pending(h)
        h.now += 200
        h.cycle()
        assert h.cancelled == [order_id]
        st = h.state()["rules"]["600036.SH"]
        assert "已撤单" in st["remainder_note"]
        assert any("余量已撤单" in n["title"] for n in h.notices)
        # 只撤一次
        h.cycle()
        assert h.cancelled == [order_id]

    def test_cancel_rejected_keeps_state(self) -> None:
        h = Harness(
            cfg=_cfg([_rule()], remainder_policy="cancel"),
            ticks={"600036.SH": {"lastPrice": 90.0}},
            positions=[_position()],
        )
        h.cancel_result = False
        self._submit_pending(h)
        h.now += 200
        h.cycle()
        st = h.state()["rules"]["600036.SH"]
        assert h.cancelled  # 尝试过
        assert "未受理" in st["remainder_note"]
        assert not any("余量已撤单" in n["title"] for n in h.notices)

    def test_requote_same_price_keeps_queue(self) -> None:
        h = Harness(
            cfg=_cfg([_rule()], remainder_policy="requote_at_protect_price"),
            ticks={"600036.SH": {"lastPrice": 90.0}},
            positions=[_position()],
        )
        self._submit_pending(h)
        h.now += 200
        h.cycle()  # 默认 DownStopPrice=88 == 委托价，不重挂
        assert h.cancelled == []
        assert "保持排队" in h.state()["rules"]["600036.SH"]["remainder_note"]

    def test_requote_when_price_deviates(self) -> None:
        detail = {"DownStopPrice": 88.0}
        h = Harness(
            cfg=_cfg([_rule()], remainder_policy="requote_at_protect_price"),
            ticks={"600036.SH": {"lastPrice": 90.0}},
            positions=[_position()],
            details={"600036.SH": detail},
        )
        order_id = self._submit_pending(h)
        # 模拟当日跌停价下移（委托价偏离保护价）
        detail["DownStopPrice"] = 80.0
        h.now += 200
        h.cycle()
        assert h.cancelled == [order_id]
        assert len(h.dispatched) == 2
        requote = h.dispatched[1]
        assert requote["price"] == 80.0
        assert requote["client_order_id"].endswith("-r1")
        assert requote["remarks"].startswith("sltp:requote")
        st = h.state()["rules"]["600036.SH"]
        assert st["order_price"] == 80.0
        assert st["requote_count"] == 1
        assert st["status"] == ex.ST_SUBMITTED
        # 重挂后不再重复撤挂
        h.orders[requote["client_order_id"]] = None
        h.cycle()
        assert h.cancelled == [order_id]

    def test_requote_dispatch_failure_notifies(self) -> None:
        detail = {"DownStopPrice": 88.0}
        h = Harness(
            cfg=_cfg([_rule()], remainder_policy="requote_at_protect_price"),
            ticks={"600036.SH": {"lastPrice": 90.0}},
            positions=[_position()],
            details={"600036.SH": detail},
        )
        self._submit_pending(h)
        detail["DownStopPrice"] = 80.0
        h.dispatch_result = {"status": "error", "message": "柜台拒单"}
        h.now += 200
        h.cycle()
        assert any("余量重挂失败" in n["title"] for n in h.notices)


# --------------------------------------------------------------------------
# 8. 收盘前提醒（Phase 3.2）
# --------------------------------------------------------------------------
class TestCloseReminder:
    @staticmethod
    def _at(hour: int, minute: int) -> float:
        from datetime import datetime

        return datetime(2026, 9, 11, hour, minute, 0, tzinfo=ex.TZ).timestamp()

    def test_close_reminder_once(self) -> None:
        h = Harness(
            cfg=_cfg([_rule()], close_reminder_sec=300, pending_alert_sec=99999),
            ticks={"600036.SH": {"lastPrice": 90.0}},
            positions=[_position()],
        )
        h.cycle()
        order_id = h.state()["rules"]["600036.SH"]["order_id"]
        h.orders[order_id] = {"status": "submitted", "filled_quantity": 0, "average_price": 0}
        h.now = self._at(14, 56)
        h.cycle()
        h.cycle()
        reminders = [n for n in h.notices if "临近收盘" in n["title"]]
        assert len(reminders) == 1
        assert "当日有效" in reminders[0]["content"]

    def test_no_reminder_outside_window(self) -> None:
        h = Harness(
            cfg=_cfg([_rule()], close_reminder_sec=300, pending_alert_sec=99999),
            ticks={"600036.SH": {"lastPrice": 90.0}},
            positions=[_position()],
        )
        h.cycle()
        order_id = h.state()["rules"]["600036.SH"]["order_id"]
        h.orders[order_id] = {"status": "submitted", "filled_quantity": 0, "average_price": 0}
        h.now = self._at(14, 40)
        h.cycle()
        assert [n for n in h.notices if "临近收盘" in n["title"]] == []
