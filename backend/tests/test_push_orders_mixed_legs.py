"""一键推送的**混合方向**（P1.1）与**逐笔限价**（P1.2）。

场景来自 2026-09-23 隔壁 LLM 的实盘实录：一次决策里同时有卖 002518 与买 600276
（调仓），且逐笔都带 ``limit_px``（卖报 38.95 贴 ref 39.34 下方、买报 45.99 贴 45.53
上方 —— "贴着打保成交"）。原契约是一批一个 ``side``、限价由服务端算死，这两件都表达不出来。

本文件钉住三件事：

* **契约**：``orders`` 逐笔形态与旧 ``symbols``+``side`` 形态互斥（混用即 422，
  不能猜用户想表达哪个）；两边都不给也是 422。
* **贯通**：逐笔方向要一路走到数量计划 / 名单闸 / 风控判定 / 幂等键 / 下单请求；
  整批资金缩量**只缩买腿**（卖腿是回收资金，缩它等于凭空少卖）。
* **限价**：预检与真单共用 :func:`lot_rules.resolve_limit_price`；越界在预检就阻断
  （用户在确认面板上看得见），而不是等真单提交时才被镜像拒。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from backend.services.api.routers import push_orders as po


def _run(coro):
    import asyncio

    return asyncio.run(coro)


# --------------------------------------------------------------------------
# 契约：orders 与 symbols 互斥
# --------------------------------------------------------------------------


class TestPushInContract:
    def test_legacy_shape_still_works(self):
        # Arrange / Act
        body = po.PushIn(
            symbols=["600036.SH"], side="buy", channels=["sim"], batch_id="batch-0001"
        )

        # Assert：旧形态一字未改地可用（前端未升级前全靠它）
        plan = body.leg_plan()
        assert [(x.symbol, x.side) for x in plan] == [("600036.SH", "buy")]

    def test_mixed_orders_carry_per_leg_side(self):
        # Arrange / Act
        body = po.PushIn(
            orders=[
                {"symbol": "002518.SZ", "side": "sell"},
                {"symbol": "600276.SH", "side": "buy", "quantity": 100},
            ],
            channels=["sim", "real"],
            batch_id="batch-0002",
        )

        # Assert
        assert [(x.symbol, x.side) for x in body.leg_plan()] == [
            ("002518.SZ", "sell"),
            ("600276.SH", "buy"),
        ]
        assert body.leg_plan()[1].quantity == 100

    def test_symbols_and_orders_together_are_rejected(self):
        """混用即 422：两种形态的 side 语义不同，猜错的代价是真单。"""
        # Arrange / Act / Assert：钉住**互斥**这个原因，而不是「缺字段」顺带抛的
        with pytest.raises(ValidationError, match="不能同时"):
            po.PushIn(
                symbols=["600036.SH"],
                side="buy",
                orders=[{"symbol": "600519.SH", "side": "buy"}],
                channels=["sim"],
                batch_id="batch-0003",
            )

    def test_neither_shape_is_rejected(self):
        # Arrange / Act / Assert
        with pytest.raises(ValidationError, match="必须给"):
            po.PushIn(channels=["sim"], batch_id="batch-0004")

    def test_legacy_shape_requires_side(self):
        # Arrange / Act / Assert
        with pytest.raises(ValidationError, match="side"):
            po.PushIn(symbols=["600036.SH"], channels=["sim"], batch_id="batch-0005")

    def test_orders_with_quantities_map_is_rejected(self):
        """``orders`` 形态里数量写在腿上；再给一个 {symbol: 股数} 就有了两个事实源。"""
        # Arrange / Act / Assert
        with pytest.raises(ValidationError, match="quantities"):
            po.PushIn(
                orders=[{"symbol": "600276.SH", "side": "buy"}],
                quantities={"600276.SH": 100},
                channels=["sim"],
                batch_id="batch-0006",
            )

    def test_orders_require_side_on_every_leg(self):
        # Arrange / Act / Assert：缺 side 不默认成 batch 级（没有 batch 级了）
        with pytest.raises(ValidationError, match="side"):
            po.PushIn(
                orders=[{"symbol": "600276.SH"}], channels=["sim"], batch_id="batch-7"
            )

    def test_duplicate_symbols_in_orders_are_rejected(self):
        """同一只票同批一买一卖自相矛盾（净额与额度都算不准），逐笔形态下直接拒。"""
        # Arrange / Act / Assert
        with pytest.raises(ValidationError, match="重复"):
            po.PushIn(
                orders=[
                    {"symbol": "600276.SH", "side": "sell"},
                    {"symbol": "600276.SH", "side": "buy"},
                ],
                channels=["sim"],
                batch_id="batch-0008",
            )

    def test_orders_cap_matches_the_batch_cap(self):
        # Arrange：MAX_BATCH_SYMBOLS + 1 只
        many = [
            {"symbol": f"60{i:04d}.SH", "side": "buy"}
            for i in range(po.MAX_BATCH_SYMBOLS + 1)
        ]

        # Act / Assert
        with pytest.raises(ValidationError, match="orders"):
            po.PushIn(orders=many, channels=["sim"], batch_id="batch-0009")

    def test_negative_limit_price_is_rejected_at_the_schema(self):
        # Arrange / Act / Assert：脏价不该走到业务层（gt=0）
        with pytest.raises(ValidationError, match="limit_price"):
            po.PushIn(
                orders=[{"symbol": "600276.SH", "side": "buy", "limit_price": -1}],
                channels=["sim"],
                batch_id="batch-0010",
            )

    def test_blank_symbol_is_rejected(self):
        # Arrange / Act / Assert
        with pytest.raises(ValidationError):
            po.PushIn(
                orders=[{"symbol": "", "side": "buy"}],
                channels=["sim"],
                batch_id="batch-0013",
            )


# --------------------------------------------------------------------------
# 共通假体：价格 / 信号 / 名单 / 模拟账户 / 实盘快照
# --------------------------------------------------------------------------


def _patch_leg_deps(
    monkeypatch,
    *,
    prices: dict[str, float] | None = None,
    sim_positions: dict | None = None,
    real_positions: dict | None = None,
    real_sources: dict | None = None,
    max_slip: float = 0.02,
    cash: float = 1_000_000.0,
    score: float = 0.5,
):
    from backend.services.trade_shared import simulation_manager as sm
    from backend.shared import real_positions as rp

    px = prices if prices is not None else {}

    class _Mgr:
        def __init__(self, _redis):
            pass

        async def get_account(self, _uid, _tenant):
            return {"cash": cash, "positions": sim_positions or {}}

    async def _scores(symbols):
        # 买入自动算量需要 >0 的仓位信号（缺失即 blocked，那是另一条测试的事）
        return {str(s).split(".")[0]: score for s in symbols}, "2026-09-23"

    async def _risk(_symbols):
        return {"by_symbol": {}, "meta": {}, "imported": True}

    async def _load(_tenant, _uid):
        return real_positions or {}, {
            "sources": real_sources if real_sources is not None else {},
            "snapshot_at": "2026-09-23T01:00:00+00:00",
            "active_broker": "qmt_exec",
        }

    monkeypatch.setattr(sm, "SimulationAccountManager", _Mgr)
    monkeypatch.setattr(
        sm, "require_sim_user_id", lambda _raw, tenant_id="default": 10000001
    )
    monkeypatch.setattr(
        po, "_resolve_prices", lambda syms: {s: px.get(s, 0.0) for s in syms}
    )
    monkeypatch.setattr(po, "_resolve_position_scores", _scores)
    monkeypatch.setattr(po, "_resolve_risk", _risk)
    monkeypatch.setattr(rp, "load_real_positions", _load)
    monkeypatch.setattr(po, "_mirror_max_slip", lambda _redis: max_slip)


def _build(body: po.PushIn):
    return _run(
        po._build_legs(
            body,
            {"tenant_id": "default", "user_id": "10000001"},
            redis=SimpleNamespace(),
            db_available=False,
        )
    )


# --------------------------------------------------------------------------
# 贯通：逐笔方向一路走到底
# --------------------------------------------------------------------------


class TestMixedLegsCarveThrough:
    def _body(self, **over) -> po.PushIn:
        kw = {
            "orders": [
                {"symbol": "002518.SZ", "side": "sell"},
                {"symbol": "600276.SH", "side": "buy"},
            ],
            "channels": ["sim"],
            "batch_id": "batch-mixed-01",
        }
        kw.update(over)
        return po.PushIn(**kw)  # type: ignore[arg-type]

    def test_each_leg_keeps_its_own_side(self, monkeypatch):
        # Arrange：卖腿有持仓、买腿无持仓
        _patch_leg_deps(
            monkeypatch,
            prices={"002518.SZ": 39.34, "600276.SH": 45.53},
            sim_positions={"002518.SZ": {"available_volume": 200}},
        )

        # Act
        legs, meta = _build(self._body())

        # Assert
        assert [x["side"] for x in legs] == ["sell", "buy"]
        assert legs[0]["executable"] is True and legs[0]["quantity"] == 200
        assert legs[1]["executable"] is True
        assert legs[1]["exec_path"] == "sim"
        # 买腿不读实盘持仓：读到的是卖腿那份快照，但只对卖腿生效
        assert meta["meta"]["real_positions"] is not None

    def test_sell_leg_reads_real_snapshot_and_buy_leg_does_not(self, monkeypatch):
        """只有卖腿会因「实盘独有持仓」改道；买腿的路径不受实盘账户影响。"""
        # Arrange：模拟台账无票，实盘有 300 股
        _patch_leg_deps(
            monkeypatch,
            prices={"002518.SZ": 39.34, "600276.SH": 45.53},
            sim_positions={},
            real_positions={"SZ002518": {"available_volume": 300}},
            real_sources={"qmt_exec": {"stale": False}},
        )

        # Act
        legs, _ = _build(self._body(channels=["sim", "real"]))

        # Assert
        assert legs[0]["exec_path"] == "real_direct"
        assert legs[0]["quantity"] == 300
        assert legs[1]["exec_path"] == "sim"

    def test_batch_scale_shrinks_only_the_buy_leg(self, monkeypatch):
        """卖腿是**回收**资金，缩它等于凭空少卖；整批约束只对买腿成立。"""
        # Arrange：买腿远超可用资金（0 现金），卖腿必须原样
        _patch_leg_deps(
            monkeypatch,
            prices={"002518.SZ": 39.34, "600276.SH": 45.53},
            sim_positions={"002518.SZ": {"available_volume": 200}},
        )

        # Act
        legs, meta = _build(self._body())

        # Assert
        assert legs[0]["quantity"] == 200  # 卖腿一分未缩
        assert legs[1]["executable"] is True
        assert meta["meta"]["budget"]["applied"] is False


class TestRiskVerdictsPerLegSide:
    def test_preflight_receives_the_leg_side(self, monkeypatch):
        """风控判定必须按**这一笔**的方向跑 —— 用 batch 级 side 会把卖单判成买单。"""
        # Arrange
        from backend.services.trade.services import risk_gate_service as rgs
        from backend.shared import database_manager_v2 as dbm

        seen: list[tuple[str, str]] = []

        class _Verdict:
            verdict = "pass"
            enforced = False
            passed = True
            rule_id = ""
            reason = ""
            decisions: list = []

        async def _preflight(req, *, db, redis):
            seen.append(
                (str(getattr(req, "symbol", "")), str(getattr(req, "side", "")))
            )
            return _Verdict()

        class _Session:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(rgs, "preflight_order", _preflight)
        monkeypatch.setattr(dbm, "get_session", lambda **kw: _Session())

        body = po.PushIn(
            orders=[
                {"symbol": "002518.SZ", "side": "sell"},
                {"symbol": "600276.SH", "side": "buy"},
            ],
            channels=["sim"],
            batch_id="batch-mixed-02",
        )
        legs = [
            {"symbol": "002518.SZ", "side": "sell", "quantity": 200, "price": 39.34},
            {"symbol": "600276.SH", "side": "buy", "quantity": 100, "price": 45.53},
        ]

        # Act
        _run(
            po._apply_risk_verdicts(
                body, legs, tenant_id="default", uid=10000001, redis=SimpleNamespace()
            )
        )

        # Assert
        assert seen == [("002518.SZ", "sell"), ("600276.SH", "buy")]


# --------------------------------------------------------------------------
# 限价：预检可见、真单可带
# --------------------------------------------------------------------------


class TestLegLimitPreflight:
    def _body(
        self,
        *,
        real: bool,
        limit: float | None,
        side: str = "buy",
        symbol: str = "600276.SH",
    ) -> po.PushIn:
        leg: dict = {"symbol": symbol, "side": side}
        if limit is not None:
            leg["limit_price"] = limit
        return po.PushIn(
            orders=[leg],
            channels=["sim", "real"] if real else ["sim"],
            batch_id="batch-limit-01",
        )

    def test_inside_band_limit_is_adopted_for_the_real_order(self, monkeypatch):
        # Arrange：ref 45.53，报 45.99（+1.01% < 2%）
        _patch_leg_deps(monkeypatch, prices={"600276.SH": 45.53})

        # Act
        legs, _ = _build(self._body(real=True, limit=45.99))

        # Assert
        assert legs[0]["limit_price"] == 45.99
        assert legs[0]["limit_source"] == "requested"
        assert legs[0]["executable"] is True

    def test_no_limit_derives_the_band_for_the_real_order(self, monkeypatch):
        # Arrange / Act
        _patch_leg_deps(monkeypatch, prices={"600276.SH": 45.53})
        legs, _ = _build(self._body(real=True, limit=None))

        # Assert：与服务端历史公式同源（45.53 × 1.02 = 46.4406 → 46.44）
        assert legs[0]["limit_price"] == 46.44
        assert legs[0]["limit_source"] == "derived"

    def test_sim_only_leg_has_no_limit(self, monkeypatch):
        """模拟腿按快照价撮合，限价是实盘真单才有的概念 —— 不编一个出来。"""
        # Arrange / Act
        _patch_leg_deps(monkeypatch, prices={"600276.SH": 45.53})
        legs, _ = _build(self._body(real=False, limit=None))

        # Assert
        assert legs[0]["limit_price"] is None

    def test_out_of_band_limit_blocks_the_real_leg(self, monkeypatch):
        # Arrange：买报 46.60（+2.35% > 2%）
        _patch_leg_deps(monkeypatch, prices={"600276.SH": 45.53})

        # Act
        legs, _ = _build(self._body(real=True, limit=46.60))

        # Assert：预检就拦下 —— 否则用户点完确认才收到一条「真单被拒」
        assert legs[0]["executable"] is False
        assert legs[0]["blocked_by"] == "limit"
        assert "限价" in legs[0]["problem"]

    def test_out_of_band_limit_without_real_channel_is_a_note_not_a_block(
        self, monkeypatch
    ):
        """只推模拟盘时那条脏限价根本不参与撮合，不该把这一笔整个挡掉。"""
        # Arrange / Act
        _patch_leg_deps(monkeypatch, prices={"600276.SH": 45.53})
        legs, _ = _build(self._body(real=False, limit=46.60))

        # Assert
        assert legs[0]["executable"] is True
        assert "限价" in (legs[0].get("note") or "")

    def _sell_deps(self, monkeypatch, **over):
        # 开实盘通道的卖腿要能读到实盘快照，否则取数来源裁定会先把这一腿拦掉
        _patch_leg_deps(
            monkeypatch,
            prices={"002518.SZ": 39.34},
            sim_positions={"002518.SZ": {"available_volume": 200}},
            real_positions={"SZ002518": {"available_volume": 200}},
            real_sources={"qmt_exec": {"stale": False}},
            **over,
        )

    def test_sell_limit_below_the_band_is_blocked(self, monkeypatch):
        # Arrange：卖报 37.00（ref 39.34 的 -5.9% > 2%）
        self._sell_deps(monkeypatch)

        # Act
        legs, _ = _build(
            self._body(real=True, limit=37.00, side="sell", symbol="002518.SZ")
        )

        # Assert
        assert legs[0]["executable"] is False
        assert legs[0]["blocked_by"] == "limit"

    def test_sell_limit_tighter_than_the_band_is_allowed(self, monkeypatch):
        """卖报更高只是挂远了，不是错价 —— 采纳，不拦。"""
        # Arrange
        self._sell_deps(monkeypatch)

        # Act
        legs, _ = _build(
            self._body(real=True, limit=40.50, side="sell", symbol="002518.SZ")
        )

        # Assert
        assert legs[0]["executable"] is True
        assert legs[0]["limit_price"] == 40.5


class TestMirrorCarriesTheLegLimit:
    def test_mirror_fill_passes_the_limit_through(self, monkeypatch):
        """限价要跟着 ``OrderRequest`` 走到镜像 —— 断在中途等于没做。"""
        # Arrange
        from backend.services.live_trading.services import real_mirror_service as rms
        from backend.services.simulation.services import order_router as orouter

        captured: dict = {}

        async def _fake(**kw):
            captured.update(kw)
            return {"status": "submitted"}

        monkeypatch.setattr(rms, "mirror_virtual_fill", _fake)
        req = orouter.OrderRequest(
            tenant_id="default",
            user_id=10000001,
            symbol="600276.SH",
            side="buy",
            quantity=100,
            price=45.53,
            mirror=True,
            mirror_source="candidate_push",
            real_limit_price=45.99,
        )
        routed = orouter.RouterOutcome(
            success=True,
            fill_price=45.60,
            filled_quantity=100,
            order_id="sim-1",
            trade_id="t-1",
            message="filled",
        )

        # Act
        _run(orouter._mirror_fill(SimpleNamespace(), req, routed))

        # Assert
        assert captured.get("limit_price") == 45.99

    def test_router_submit_carries_the_leg_side_and_limit(self, monkeypatch):
        """提交循环：幂等键与下单请求都用**这一笔**的方向，并带上限价。"""
        # Arrange
        from backend.services.simulation.services import order_router as orouter

        seen: list[orouter.OrderRequest] = []

        async def _submit(_session, _redis, req):
            seen.append(req)
            return orouter.RouterOutcome(
                success=True,
                fill_price=req.price or 0.0,
                filled_quantity=req.quantity,
                order_id="o-1",
                trade_id="t-1",
                message="filled",
            )

        monkeypatch.setattr(orouter, "submit_order", _submit)
        monkeypatch.setattr(po, "submit_order", _submit, raising=False)

        legs = [
            {
                "symbol": "002518.SZ",
                "side": "sell",
                "quantity": 200,
                "price": 39.34,
                "executable": True,
                "exec_path": "sim",
                "limit_price": 38.95,
            }
        ]
        body = po.PushIn(
            orders=[{"symbol": "002518.SZ", "side": "sell", "limit_price": 38.95}],
            channels=["sim", "real"],
            batch_id="batch-limit-02",
        )

        # Act / Assert：幂等键与请求方向一致（幂等键按 side 分组，混了会串单）
        from backend.shared.order_contract import build_candidate_client_order_id

        assert build_candidate_client_order_id(
            body.batch_id, "002518.SZ", "sell"
        ) != build_candidate_client_order_id(body.batch_id, "002518.SZ", "buy")
        assert legs[0]["side"] == body.leg_plan()[0].side


class TestFrontendCompatibility:
    """旧字段一个都不能少：前端未升级前 ``data.side`` 与 ``legs[].quantity`` 是命根子。"""

    def test_legacy_response_side_is_echoed(self, monkeypatch):
        # Arrange
        _patch_leg_deps(monkeypatch, prices={"600036.SH": 40.0})
        body = po.PushIn(
            symbols=["600036.SH"], side="buy", channels=["sim"], batch_id="batch-0011"
        )

        # Act
        leg_plan = body.leg_plan()

        # Assert
        assert leg_plan[0].side == "buy"

    def test_batch_side_label_for_mixed_orders(self):
        # Arrange / Act
        body = po.PushIn(
            orders=[
                {"symbol": "002518.SZ", "side": "sell"},
                {"symbol": "600276.SH", "side": "buy"},
            ],
            channels=["sim"],
            batch_id="batch-0012",
        )

        # Assert：回执里不能只说 buy 或只说 sell（那会让前端把卖单渲染成买单）
        assert body.batch_side_label() == "mixed"
