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

import pytest

from backend.services.live_trading.services import sltp_executor as ex
from backend.services.live_trading.services.lot_rules import (
    align_sell_quantity,
    describe_violation,
    is_full_position_sell,
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
        state: dict | None = None,
    ) -> None:
        self.redis = FakeRedis(cfg=ex.merge_config(cfg), state=state)
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
    # 夹具默认跟随**生产缺省**（aggressive）；要测遗留 limit_floor 口径的用例显式传入。
    base = {
        "enabled": True,
        "user_id": "1",
        "protect_price_mode": ex.DEFAULT_PROTECT_MODE,
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

    def test_is_full_position_sell_contract(self) -> None:
        """整仓断言的判据（push_plan 手填卖单 / 止损 / 减仓三处同源）：

        * 取不到可用持仓（``None``）或可用为 0（T+1 锁定）→ ``False``：
          **不凭想象**替调用方放行一张碎股卖单；
        * 买入无「整仓」概念 → ``False``；
        * 大小写不敏感（调用点传 ``sell`` 与 ``SELL`` 两种写法）。
        """
        assert is_full_position_sell("SELL", 246, 246) is True
        assert is_full_position_sell("sell", 246.0, 246.0) is True
        # 手量 ≥ 可用（多出的部分由柜台拦，本谓词只管「是不是整仓」）
        assert is_full_position_sell("SELL", 300, 246) is True
        assert is_full_position_sell("SELL", 200, 246) is False  # 部分卖出
        assert is_full_position_sell("SELL", 246, None) is False  # 没取到持仓
        assert is_full_position_sell("SELL", 246, 0) is False  # 可用为 0
        assert is_full_position_sell("BUY", 246, 246) is False
        assert is_full_position_sell("", 246, 246) is False

    def test_liquidating_trigger_marks_the_order_as_a_full_position_sell(self) -> None:
        """整仓清掉的触发单带 ``full_position_sell``（评审 M4）。

        数量来自柜台**实时**可用量，而派发层整手预检只看**当日快照**：快照比实时大
        （当天已有成交）时，合法的碎股全清会被判 ``lot_blocked`` ⇒ **该止损的时候
        止损单发不出去**（委托行已落库，每轮重试都被拒）。断言订单报文。
        """
        h = Harness(
            cfg=_cfg([_rule()]),  # 未给 quantity/reduce_pct ⇒ 整仓卖出
            ticks={"600036.SH": {"lastPrice": 94.9}},
            positions=[_position(can_use=246)],
        )
        h.cycle()
        assert h.dispatched[0]["quantity"] == pytest.approx(246.0)
        assert h.dispatched[0]["full_position_sell"] is True

    def test_partial_reduce_does_not_mark_full_position_sell(self) -> None:
        h = Harness(
            cfg=_cfg([_rule(reduce_pct=0.33)]),
            ticks={"600036.SH": {"lastPrice": 94.9}},
            positions=[_position(can_use=1000)],
        )
        h.cycle()
        order = h.dispatched[0]
        assert order["quantity"] == pytest.approx(300.0)  # 1000 × 0.33 = 330 → 整手 300
        assert order["full_position_sell"] is False

    def test_partial_sell_aligned_to_lot(self) -> None:
        qty, note = align_sell_quantity("600036.SH", 246, 1000)
        assert qty == 200
        assert "整手" in note

    def test_star_partial_under_200_lifts_to_min_not_liquidates(self) -> None:
        """2026-09-23 更正：旧实现「不足 200 股 → 全量卖出」是**超卖**。

        科创板最小申报量 200 股本身合法（1 股递增只约束 200 以上），意图 100 股时
        抬到 200 即可；旧实现会卖掉全部 1000 股（10 倍于意图）。
        """
        qty, note = align_sell_quantity("688596.SH", 100, 1000)
        assert qty == 200
        assert "最小申报量" in note

    def test_star_partial_over_200_keeps_quantity(self) -> None:
        qty, _ = align_sell_quantity("688596.SH", 300, 1000)
        assert qty == 300

    def test_bj_min_lot_100(self) -> None:
        qty, note = align_sell_quantity("920950.BJ", 30, 1000)
        assert qty == 100
        assert "最小申报量" in note

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
    def test_aggressive_prices_at_live_minus_one_pct(self) -> None:
        """默认口径：``max(跌停价, 现价 × 0.99)`` —— 报得出去、也成交得了。"""
        h = Harness(
            cfg=_cfg([_rule()]),
            ticks={"600036.SH": {"lastPrice": 90.0}},
            details={"600036.SH": {"DownStopPrice": 85.5, "UpStopPrice": 104.5}},
            positions=[_position()],
        )
        h.cycle()
        order = h.dispatched[0]
        assert order["order_type"] == "LIMIT"
        # 90.0 × 0.99 = 89.1，高于跌停价 85.5 → 取 89.1（而非旧实现的 85.5）
        assert order["price"] == 89.1

    def test_aggressive_clamps_to_floor_near_limit_down(self) -> None:
        """近跌停时现价 × 0.99 会跌破跌停价 → 夹到跌停价（此时报跌停价合法）。"""
        h = Harness(
            cfg=_cfg([_rule()]),
            ticks={"600036.SH": {"lastPrice": 86.0}},
            details={"600036.SH": {"DownStopPrice": 85.5}},
            positions=[_position()],
        )
        h.cycle()
        # 86.0 × 0.99 = 85.14 < 85.5 → 夹到 85.5
        assert h.dispatched[0]["price"] == 85.5

    def test_legacy_limit_floor_still_available(self) -> None:
        """``limit_floor`` 保留（封板排队场景）：显式选了就按跌停价报。"""
        h = Harness(
            cfg=_cfg([_rule()], protect_price_mode="limit_floor"),
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
        assert "跌停价下限" in st["skip_reason"]
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
        assert ex.resolve_protect_price("limit_floor", {"DownStopPrice": 9.5}, 10.0) == (  # fidelity: allow-limit-threshold — 非阈值：券商回报的跌停保护价（DownStopPrice）夹具
            "LIMIT",
            9.5,  # fidelity: allow-limit-threshold — 非阈值：断言保护价原样透传（值即夹具）
            "跌停保护价 9.50（遗留口径）",  # fidelity: allow-limit-threshold — 非阈值：文案里的保护价
        )
        assert ex.resolve_protect_price("limit_floor", {}, 10.0)[0] is None
        assert ex.resolve_protect_price("limit_floor", {"DownStopPrice": 0}, 10.0)[0] is None

    def test_resolve_protect_price_aggressive_never_quotes_floor(self) -> None:
        """2026-09-21 002074 回归：市价 26.26 报跌停价 23.53 → 42 笔真单全废。

        缺省/非法 mode 一律回落 ``aggressive``（**绝不**静默沿用遗留口径）。
        """
        for mode in ("", None, "typo", "AGGRESSIVE"):
            got = ex.resolve_protect_price(mode, {"DownStopPrice": 23.53}, 26.26)  # fidelity: allow-limit-threshold — 非阈值：券商回报的跌停保护价（DownStopPrice）夹具
            assert got[:2] == ("LIMIT", 26.00), f"mode={mode!r} → {got}"
            assert got[1] > 23.53, "报价绝不可等于跌停价（越界申报 → 废单）"
        # 近跌停 → 夹到跌停价
        assert ex.resolve_protect_price(
            "aggressive", {"DownStopPrice": 23.53}, 23.60  # fidelity: allow-limit-threshold — 非阈值：同上夹具
        )[:2] == ("LIMIT", 23.53)  # fidelity: allow-limit-threshold — 非阈值：同上夹具
        # 现价非法 → fail-closed，**绝不**退回跌停价
        for bad in (0, 0.0, float("nan"), float("inf")):
            assert ex.resolve_protect_price(
                "aggressive", {"DownStopPrice": 23.53}, bad  # fidelity: allow-limit-threshold — 非阈值：同上夹具
            )[0] is None, f"现价 {bad!r} 应 fail-closed"

    def test_default_config_mode_is_aggressive(self) -> None:
        """生产缺省必须是 aggressive —— 改回 limit_floor 等于重新引入那 42 笔废单。"""
        assert ex.DEFAULT_CONFIG["protect_price_mode"] == "aggressive"
        assert ex.DEFAULT_PROTECT_MODE == "aggressive"
        assert set(ex.VALID_PROTECT_MODES) == {"aggressive", "limit_floor", "market"}


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

    def test_dispatch_failure_reads_nested_production_envelope(self) -> None:
        """拒因藏在**嵌套** ``result.message`` 里（派发层的真实形状）也要取到。

        真派发层拒单时的信封是
        ``{"status": "failed", "execution": "direct", "result": {...}}`` —— 顶层
        **没有** message。上面那条用例喂的是顶层带 message 的假信封，于是「线上告警
        把整个信封 ``str()`` 成 Python 字典」这件事测不出来（夹具形状≠生产形状，
        与 2026-09-24 引擎假成功同一类盲区）。
        """
        h = Harness(
            cfg=_cfg([_rule()]),
            ticks={"600036.SH": {"lastPrice": 90.0}},
            positions=[_position()],
            dispatch_result={
                "status": "failed",
                "execution": "direct",
                "order_id": "OID-9",
                "result": {
                    "success": False,
                    "status": "rejected",
                    "message": "Broker拒绝: 废单：委托价格超出涨跌幅限制",
                },
            },
        )
        summary = h.cycle()
        assert summary["failed"] == 1
        st = h.state()["rules"]["600036.SH"]
        assert st["status"] == ex.ST_FAILED
        # 断言**等于**拒因而不是「含有」：信封的 ``str()`` 兜底也含「废单」二字
        # （字典 repr 里有嵌套原文），用 ``in`` 会被退化路径满足 —— 那就等于没测。
        assert st["failure"] == "Broker拒绝: 废单：委托价格超出涨跌幅限制", (
            f"用户告警里会是一段 Python 字典: {st['failure']}"
        )

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
            cfg=_cfg(
                [_rule()],
                remainder_policy="requote_at_protect_price",
                protect_price_mode="limit_floor",
            ),
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
            cfg=_cfg(
                [_rule()],
                remainder_policy="requote_at_protect_price",
                protect_price_mode="limit_floor",
            ),
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
            cfg=_cfg(
                [_rule()],
                remainder_policy="requote_at_protect_price",
                protect_price_mode="limit_floor",
            ),
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

    def test_requote_aggressive_uses_live_price_not_floor(self) -> None:
        """``aggressive`` 口径下的重挂：基准取**当前市价**（``st["last_price"]``）。

        委托挂在保护价上、市价已走开 → 重挂到 ``现价 × 0.99``，而不是跌停价
        （报跌停价在非封板场景属越界申报，正是 2026-09-21 那 42 笔废单的报价）。
        """
        detail = {"DownStopPrice": 88.0}
        h = Harness(
            cfg=_cfg([_rule()], remainder_policy="requote_at_protect_price"),
            ticks={"600036.SH": {"lastPrice": 90.0}},
            positions=[_position()],
            details={"600036.SH": detail},
        )
        order_id = self._submit_pending(h)
        # 市价走低到 84 → 现价×0.99 = 83.16 > 跌停价 80 → 重挂到 83.16
        detail["DownStopPrice"] = 80.0
        h.client.ticks["600036.SH"]["lastPrice"] = 84.0
        h.now += 200
        h.cycle()
        assert h.cancelled == [order_id], "委托价 88 已偏离 83.16 → 应撤挂"
        requote = h.dispatched[1]
        assert requote["price"] == 83.16, f"应报现价−1%，实际 {requote['price']}"
        assert requote["price"] > detail["DownStopPrice"], "绝不可重挂到跌停价"


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


# --------------------------------------------------------------------------
# 9. 崩溃重试与幂等委托号（review HIGH 2）
# --------------------------------------------------------------------------
def _stuck_state(reason: str = "止损触发 现价90.00 ≤ 95.00") -> dict:
    """模拟「已落 triggered、进程在落单前/写回前中断」的 Redis 状态。"""
    return {
        "date": DAY,
        "rules": {"600036.SH": {"status": ex.ST_TRIGGERED, "reason": reason}},
    }


class TestIdempotentRetry:
    def test_client_order_id_is_rule_day_generation_stable(self) -> None:
        a = ex.rule_client_order_id("600036.SH", 1_000_000.0, 1)
        b = ex.rule_client_order_id("SH600036", 1_000_000.0 + 1, 1)  # 前缀式 + 数秒后
        c = ex.rule_client_order_id("600036.SH", 1_000_000.0, 2)
        assert a == b  # 同一标的同一天同一代 → 同号（重试可被幂等去重）
        assert a != c  # 重新武装后的下一代 → 新号（能下出新单）
        assert a.startswith("sltp-600036.SH-") and a.endswith("-g1")

    def test_is_retryable_predicate(self) -> None:
        assert ex.is_retryable({"status": ex.ST_ARMED})
        assert ex.is_retryable({})
        assert ex.is_retryable({"status": ex.ST_TRIGGERED})  # 触发未落单 → 可重试
        assert not ex.is_retryable({"status": ex.ST_TRIGGERED, "order_id": "OID-1"})
        assert not ex.is_retryable({"status": ex.ST_SUBMITTED, "order_id": "OID-1"})
        assert not ex.is_retryable({"status": ex.ST_FILLED})

    def test_triggered_without_order_is_retried(self) -> None:
        h = Harness(
            cfg=_cfg([_rule()]),
            ticks={"600036.SH": {"lastPrice": 90.0}},
            positions=[_position()],
            state=_stuck_state(),
        )
        summary = h.cycle()
        assert summary["triggered"] == 1
        assert len(h.dispatched) == 1
        assert h.state()["rules"]["600036.SH"]["status"] == ex.ST_SUBMITTED

    def test_triggered_with_order_is_not_retried(self) -> None:
        state = _stuck_state()
        state["rules"]["600036.SH"]["order_id"] = "OID-1"
        h = Harness(
            cfg=_cfg([_rule()]),
            ticks={"600036.SH": {"lastPrice": 90.0}},
            positions=[_position()],
            state=state,
        )
        h.cycle()
        assert h.dispatched == []

    def test_crash_retry_reuses_client_order_id(self) -> None:
        """下单成功但状态没写回（崩溃）→ 重试必须复用同一委托号，交调度器去重。"""
        first = Harness(
            cfg=_cfg([_rule()]), ticks={"600036.SH": {"lastPrice": 90.0}}, positions=[_position()]
        )
        first.cycle()
        cid_first = first.dispatched[0]["client_order_id"]

        retry = Harness(
            cfg=_cfg([_rule()]),
            ticks={"600036.SH": {"lastPrice": 90.0}},
            positions=[_position()],
            state=_stuck_state(),
        )
        retry.cycle()
        assert retry.dispatched[0]["client_order_id"] == cid_first

    def test_reset_then_retrigger_uses_new_client_order_id(self) -> None:
        h = Harness(
            cfg=_cfg([_rule()]), ticks={"600036.SH": {"lastPrice": 90.0}}, positions=[_position()]
        )
        h.cycle()
        h.reset()
        h.cycle()
        assert len(h.dispatched) == 2
        assert h.dispatched[0]["client_order_id"].endswith("-g1")
        assert h.dispatched[1]["client_order_id"].endswith("-g2")

    def test_stranded_trigger_notified_after_close_once(self) -> None:
        h = Harness(
            cfg=_cfg([_rule()]),
            ticks={"600036.SH": {"lastPrice": 90.0}},
            positions=[_position()],
            state=_stuck_state(),
        )
        with (
            patch.object(ex, "is_trading_time", return_value=False),
            patch.object(ex, "trade_date_str", return_value=DAY),
        ):
            asyncio.run(ex.run_sltp_cycle(h.deps()))
            asyncio.run(ex.run_sltp_cycle(h.deps()))
        alerts = [n for n in h.notices if "触发未能下单" in n["title"]]
        assert len(alerts) == 1
        assert alerts[0]["level"] == "error"
        assert h.dispatched == []  # 收盘后不补单，只告警


# --------------------------------------------------------------------------
# 9b. 保护性重试（P4 附-①）：分类先于重试
# --------------------------------------------------------------------------
class TestProtectiveRetry:
    """「失败就重试」是错的 —— sltp「一次触发当日只执行一次」本身是 2026-09-21
    事故（002074：42 笔越界价废单、0 成交；触发→废单→重新武装→再触发每 2 分钟
    一轮）之后的刻意设计。边界：**暂时性**失败且保护条件仍成立才换代重试；
    价格类废单与状态未知（派发异常/超时）一次都不重试。

    设计原文见 docs/local/quant-trader-migration-plan.md「P4 附-c」。
    """

    #: 暂时性失败的真实形状：派发没抛，引擎走到了券商，券商侧通道断（终态 REJECTED）
    TRANSIENT = {
        "status": "failed",
        "execution": "direct",
        "order_id": "OID-1",
        "message": "执行异常: 柜台连接中断",
    }

    def _harness(self, dispatch_result: dict) -> Harness:
        return Harness(
            cfg=_cfg([_rule()]),
            ticks={"600036.SH": {"lastPrice": 90.0}},
            positions=[_position()],
            dispatch_result=dispatch_result,
        )

    def test_transient_failure_retries_next_cycle_with_new_generation(self) -> None:
        """①暂时性失败 + 条件仍成立 → 下一轮再发，且**代次 +1**（新委托号）。

        同号重发会撞派发层幂等 → duplicate_skipped，而幂等命中不是「又下了一单」
        —— 单永远出不去、状态还停在已提交（P2.6 教训）。
        """
        h = self._harness(dict(self.TRANSIENT))
        s1 = h.cycle()
        st = h.state()["rules"]["600036.SH"]
        assert s1["retry_scheduled"] == 1
        assert st["status"] == ex.ST_TRIGGERED, "暂时性失败必须保持可重试，不能进当日终态"
        assert not st.get("order_id"), "失败的那张单不是我们的在途单，不得记进状态"
        assert st["retry_attempts"] == 1

        h.now += ex._RETRY_COOLDOWN_SEC + 1  # 越过冷却窗（轮询 3s，没有它 9s 烧光预算）
        h.dispatch_result = {"status": "success", "order_id": "OID-2"}
        s2 = h.cycle()
        assert s2["submitted"] == 1
        assert len(h.dispatched) == 2, "条件仍成立（现价 90 ≤ 防守位 95）却不再重试"
        cids = [d["client_order_id"] for d in h.dispatched]
        assert cids[0].endswith("-g1") and cids[1].endswith("-g2"), cids
        assert h.state()["rules"]["600036.SH"]["status"] == ex.ST_SUBMITTED

    def test_retry_waits_while_condition_no_longer_holds(self) -> None:
        """重试是「条件仍成立」的函数，不是定时重放：价格回到防守位上方就不发。

        等于用户裁决第三条（不重放陈旧意图）：行情已变，重放旧触发=按过期判断下单。
        """
        h = self._harness(dict(self.TRANSIENT))
        h.cycle()
        h.now += ex._RETRY_COOLDOWN_SEC + 1  # 越过冷却窗：本轮不发只能是条件的原因
        h.client.ticks["600036.SH"] = {"lastPrice": 99.0}  # 防守位 95，条件不再成立
        h.cycle()
        assert len(h.dispatched) == 1, "条件已不成立还重发 = 按过期判断卖股票"
        st = h.state()["rules"]["600036.SH"]
        # 仍在「待重试」态（而不是当日终态）：价格再跌破时当日仍能补上这笔保护，
        # 收盘仍未落单则由 _notify_stranded_triggers 收口告警。
        assert st["status"] == ex.ST_TRIGGERED

    def test_retry_respects_cooldown_between_attempts(self) -> None:
        """冷却窗内不发单、也不记尝试：轮询周期 3s，没有冷却挡着，3 次换代会在
        9 秒内烧光当日重试预算 —— 一次十秒级通道抖动之后就再也补不上保护。"""
        h = self._harness(dict(self.TRANSIENT))
        h.cycle()  # 首发失败 → retry_attempts=1、last_failure_at=now
        h.now += 1.0  # 下一拍轮询
        h.cycle()
        assert len(h.dispatched) == 1, "冷却期内重发 = 3 秒一轮的静默重试"
        assert h.state()["rules"]["600036.SH"]["retry_attempts"] == 1
        h.now += ex._RETRY_COOLDOWN_SEC
        h.cycle()
        assert len(h.dispatched) == 2, "冷却过后条件仍成立 → 必须补上这笔保护"

    def test_retry_cap_escalates_exactly_once_and_stops(self) -> None:
        """②到上限（3 次/标的/日）→ 当日终态 + 一条**升级**告警，之后不再发。

        上限是防「无限静默重试」的那道网（隔壁 replay_deferred 正是栽在这里）。
        """
        h = self._harness(dict(self.TRANSIENT))
        for _ in range(4):  # 首发 1 + 重试 3
            h.cycle()
            h.now += ex._RETRY_COOLDOWN_SEC + 1
        assert len(h.dispatched) == 4
        st = h.state()["rules"]["600036.SH"]
        assert st["status"] == ex.ST_FAILED
        assert st["retry_attempts"] == 3

        escalations = [
            n for n in h.notices if n["level"] == "error" and "重试" in n["title"]
        ]
        assert len(escalations) == 1, f"升级告警必须恰好一条: {[n['title'] for n in h.notices]}"
        assert "人工" in escalations[0]["content"]

        h.cycle()  # 终态后当日不再发
        assert len(h.dispatched) == 4

    def test_price_reject_is_never_retried(self) -> None:
        """③价格类废单不重发同单：2026-09-21 那 42 笔循环的回归线。

        触发→废单→重新武装→再触发，每 2 分钟一轮就是这么来的。价格可修时交人工
        改价（或下一个交易日的重新武装），执行器不做「同参数再试一次」。
        """
        h = self._harness(
            {
                "status": "failed",
                "execution": "direct",
                "order_id": "OID-9",
                "result": {
                    "success": False,
                    "status": "rejected",
                    "message": "Broker拒绝: 废单：委托价格超出涨跌幅限制",
                },
            }
        )
        for _ in range(3):
            h.cycle()
        assert len(h.dispatched) == 1, "越界价废单被自动重试 = 重演 2026-09-21 循环"
        st = h.state()["rules"]["600036.SH"]
        assert st["status"] == ex.ST_FAILED
        assert st["failure"] == "Broker拒绝: 废单：委托价格超出涨跌幅限制"
        assert not any("重试" in n["title"] for n in h.notices)

    def test_dispatch_exception_unknown_state_is_never_retried(self) -> None:
        """④状态未知绝不重试（双卖防线）：派发层自己抛异常时没有 order_id，
        无法证明单没到柜台 —— 重发可能变成第二张卖单。

        （真·超时的形态更好：引擎把 `[BRIDGE_ACK_TIMEOUT_PENDING_REVIEW]` 单当
        submitted 报成功，执行器根本走不到失败分支；这条覆盖的是派发契约破损时
        的兜底分类。）
        """
        h = self._harness({"status": "error", "message": "TimeoutError: 桥回执超时"})
        for _ in range(3):
            h.cycle()
        assert len(h.dispatched) == 1
        assert h.state()["rules"]["600036.SH"]["status"] == ex.ST_FAILED

    def test_timeout_pending_review_stays_submitted_not_retried(self) -> None:
        """④（真超时形态）引擎「已提交待核查」→ 记在途、绝不重发。"""
        h = self._harness(
            {
                "status": "success",
                "order_id": "OID-1",
                "result": {"status": "submitted", "message": "订单待核查（桥回执超时）"},
            }
        )
        for _ in range(3):
            h.cycle()
        assert len(h.dispatched) == 1
        st = h.state()["rules"]["600036.SH"]
        assert st["status"] == ex.ST_SUBMITTED and st["order_id"] == "OID-1"

    def test_duplicate_skipped_follows_the_order_without_burning_budget(self) -> None:
        """⑤幂等命中：不烧重试预算、也不当作一笔新的提交 —— 由该委托号的真实
        终态收口（崩溃重试路径原本就该是同一个号）。

        与 P2.6 减仓腿的口径区别：那边的代次可能与**此前被拒**的行撞号，故命中
        即作废换号；这里代次是 (标的,日,代) 的确定函数，命中只会是自己此前那一张。
        """
        h = self._harness(
            {"status": "success", "execution": "duplicate_skipped", "order_id": "OID-7"}
        )
        h.cycle()
        h.cycle()
        assert len(h.dispatched) == 1
        st = h.state()["rules"]["600036.SH"]
        assert st["status"] == ex.ST_SUBMITTED and st["order_id"] == "OID-7"
        assert not st.get("retry_attempts"), "幂等命中不是一次重试尝试"


# --------------------------------------------------------------------------
# 10. 状态回写与并发（review LOW 12 / MEDIUM 3）
# --------------------------------------------------------------------------
class TestStateWriteMerge:
    def test_dirty_save_preserves_concurrent_writes(self) -> None:
        redis = FakeRedis(state={"date": DAY, "rules": {"600036.SH": {"status": ex.ST_ARMED}}})
        state = ex.load_state(redis, DAY)
        before = {symbol: dict(item) for symbol, item in state["rules"].items()}
        # 执行器本轮只改了 600036
        state["rules"]["600036.SH"]["last_price"] = 9.9  # fidelity: allow-limit-threshold — 非阈值：last_price 夹具
        # 并发：CLI 新武装了 300750、并删掉了别的规则
        stored = redis.store[ex.STATE_KEY]["rules"]
        stored["300750.SZ"] = {"status": ex.ST_ARMED}

        dirty, removed = ex.diff_state(before, state["rules"])
        ex.save_state(redis, state, dirty=dirty, removed=removed)

        after = redis.store[ex.STATE_KEY]["rules"]
        assert after["600036.SH"]["last_price"] == 9.9  # fidelity: allow-limit-threshold — 非阈值：断言 last_price 合并结果（值即夹具）
        assert after["300750.SZ"] == {"status": ex.ST_ARMED}  # 并发新增没被冲掉

    def test_full_save_still_overwrites(self) -> None:
        """reset/初始化路径不受合并逻辑影响（整份覆盖）。"""
        redis = FakeRedis(state={"date": DAY, "rules": {"600036.SH": {"status": ex.ST_ARMED}}})
        ex.save_state(redis, {"date": DAY, "rules": {"000001.SZ": {"status": ex.ST_ARMED}}})
        assert list(redis.store[ex.STATE_KEY]["rules"]) == ["000001.SZ"]

    def test_set_enabled_raises_without_writing_on_read_failure(self) -> None:
        class BrokenRedis:
            def __init__(self) -> None:
                self.writes: list = []

            def get(self, key):
                raise RuntimeError("redis down")

            def set(self, key, value):
                self.writes.append((key, value))

        redis = BrokenRedis()
        with pytest.raises(RuntimeError):
            ex.set_enabled(redis, True)
        assert redis.writes == []  # 读失败绝不写回（否则规则表被整份抹掉）

    def test_set_enabled_keeps_rules(self) -> None:
        redis = FakeRedis(
            cfg={"enabled": False, "rules": [{"symbol": "600036.SH", "stop_loss_pct": 0.05}]}
        )
        saved = ex.set_enabled(redis, True)
        assert saved["enabled"] is True
        assert [r["symbol"] for r in saved["rules"]] == ["600036.SH"]


# --------------------------------------------------------------------------
# 11. 其他加固（review LOW 9 / LOW 11 / MEDIUM 5）
# --------------------------------------------------------------------------
class TestHardening:
    def test_tick_miss_alerted_once_at_threshold(self) -> None:
        h = Harness(cfg=_cfg([_rule()]), ticks={}, positions=[_position()])
        for _ in range(ex._TICK_MISS_ALERT_THRESHOLD - 1):
            h.cycle()
        assert [n for n in h.notices if "行情缺失" in n["title"]] == []
        h.cycle()
        h.cycle()  # 超过阈值不重复告警
        assert len([n for n in h.notices if "行情缺失" in n["title"]]) == 1

    def test_disabled_fallback_thresholds_ignored(self) -> None:
        rule = ex.normalize_rule({"symbol": "600036.SH"})
        merged = ex.trigger_config(rule, {"stop_loss_pct": 0.05, "enabled": False})
        assert merged["stop_loss_pct"] is None
        # enabled=True / 未声明时照常回落
        assert ex.trigger_config(rule, {"stop_loss_pct": 0.05})["stop_loss_pct"] == 0.05
        assert (
            ex.trigger_config(rule, {"stop_loss_pct": 0.05, "enabled": True})["stop_loss_pct"]
            == 0.05
        )

    def test_normalize_rule_forces_sell_side(self) -> None:
        assert ex.normalize_rule({"symbol": "600036.SH", "side": "buy"})["side"] == "SELL"
        assert ex.normalize_rule({"symbol": "600036.SH", "side": "SELL"})["side"] == "SELL"

    def test_router_rejects_buy_side(self) -> None:
        from pydantic import ValidationError

        from backend.services.trade.routers.qmt_sltp import SltpRule

        with pytest.raises(ValidationError):
            SltpRule(symbol="600036.SH", side="BUY")
        assert SltpRule(symbol="600036.SH", side="sell").side == "SELL"
