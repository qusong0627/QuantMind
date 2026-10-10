"""自成交/重复单窗口生产者与消费者（P2-1 定档：`l3.self_trade` / `l3.duplicate_fingerprint`）。

这两条规则的输入此前**没有任何生产者**（登记表 spec 自注「空窗口 = 不判」）——
本文件钉住 `build_context` 的窗口喂数语义与规则的消费语义：

- **指纹单源**：入库行与入参两侧都过 `order_fingerprint` 这一个函数——跨代码形态
  （600036.SH / SH600036）、大小写、数量价格写法必须撞指纹；形态差异（qty/price/
  side/order_type 任一不同）不撞。写两遍口径必然漂移成"同参数两次下单指纹不同"。
- **窗口切分**：自成交面 = 今日的活单（pending/submitted/partially_filled，不论多久）；
  重复面 = 最近 5 分钟内已落账的委托（含已成）；撤单两面都不进。
- **读失败 = 空窗口**（fail-open 方向）：这两条是存在性判据，读失败造不出反向单只是
  漏报，fail-closed（按存在处理）会拒掉一切双向策略。
- **消费面归一**：规则内两侧都过 `to_prefix`（REAL orders 后缀式、sim_orders 前缀式、
  入参随调用方——不归一的后果是静默不触发）。

时间一律冻结（周一 10:30 CST）——不钉时间的话"CST 当日零点"的边界断言随运行时刻飘。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend.services.trade.services import risk_gate_service as rgs
from backend.tests.test_risk_gate_wiring import FakeRedis, _cfg, _req, _snap_row

#: 周一 2026-10-12 10:30 CST = 02:30 UTC：CST 当日零点 = 周日 16:00 UTC。
_FROZEN_UTC = datetime(2026, 10, 12, 2, 30, tzinfo=timezone.utc)
_DAY_START_NAIVE = datetime(2026, 10, 11, 16, 0)  # CST 周一零点（naive UTC）
_DAY_START_AWARE = _FROZEN_UTC.astimezone(timezone(timedelta(hours=8))).replace(
    hour=0, minute=0, second=0, microsecond=0
)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return _FROZEN_UTC if tz is None else _FROZEN_UTC.astimezone(tz)


def _freeze(monkeypatch):
    monkeypatch.setattr(rgs, "datetime", _FrozenDatetime)
    monkeypatch.setattr(
        rgs, "_quote_snapshot", lambda sym: {"Now": "40.00", "timestamp": "9999999999"}
    )
    monkeypatch.setattr(
        "backend.services.live_trading.services.real_mirror_service.kill_switch_on",
        lambda redis: False,
    )


class _WindowDB:
    """按查询形状分发的假库：快照行（兜底）/ 窗口行；其余形状一律炸。

    未预期形状直接 AssertionError——计数/已开仓/台账查询都不该在 `need_counts=False`
    / `need_daily_pnl=False` 下发出；静默给空会让"没发查询"与"发了但没数据"长得一样。
    """

    class _Result:
        def __init__(self, rows):
            self._rows = rows

        def fetchone(self):
            return self._rows[0] if self._rows else None

        def fetchall(self):
            return list(self._rows)

    def __init__(self, *, snapshot=None, window_rows=(), window_error=None):
        self.snapshot = snapshot
        self.window_rows = list(window_rows)
        self.window_error = window_error
        self.sqls: list[str] = []

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        self.sqls.append(sql)
        if "count(" in sql or "DISTINCT" in sql.upper() or "ledger" in sql:
            raise AssertionError(
                f"未预期查询（need_counts/need_daily_pnl 应为 False）: {sql[:120]}"
            )
        if "FROM orders" in sql or "sim_orders" in sql:
            if self.window_error is not None:
                raise self.window_error
            return self._Result(self.window_rows)
        return self._Result([] if self.snapshot is None else [self.snapshot])


def _real_row(
    symbol, side, status, *, created, qty=100.0, price=40.0, order_type="limit"
):
    return (symbol, side, status, qty, price, order_type, created)


# ── order_fingerprint：单源归一 ─────────────────────────────────────


@pytest.mark.unit
def test_fingerprint_same_params_across_code_forms_and_case():
    a = rgs.order_fingerprint("600036.SH", "BUY", 100, 40.0, "LIMIT")
    b = rgs.order_fingerprint("SH600036", "buy", 100.0, 40.00, "limit")
    assert a == b


@pytest.mark.unit
def test_fingerprint_distinguishes_real_param_differences():
    base = rgs.order_fingerprint("600036.SH", "BUY", 100, 40.0, "limit")
    assert base != rgs.order_fingerprint("600036.SH", "SELL", 100, 40.0, "limit")
    assert base != rgs.order_fingerprint("600036.SH", "BUY", 200, 40.0, "limit")
    assert base != rgs.order_fingerprint("600036.SH", "BUY", 100, 40.01, "limit")
    # 市价单（price=None）与同写法的 0 同归一段；与限价单不撞（order_type 段不同）
    assert rgs.order_fingerprint("600036.SH", "BUY", 100, None, "market") == (
        rgs.order_fingerprint("600036.SH", "BUY", 100, 0, "market")
    )
    assert rgs.order_fingerprint("600036.SH", "BUY", 100, None, "market") != (
        rgs.order_fingerprint("600036.SH", "BUY", 100, None, "limit")
    )


# ── build_context：REAL 窗口切分 ────────────────────────────────────


@pytest.mark.asyncio
async def test_real_windows_split_live_vs_took_vs_cancelled(monkeypatch):
    _freeze(monkeypatch)
    live_old = _real_row(
        "600036.SH", "sell", "submitted", created=datetime(2026, 10, 11, 20, 0)
    )  # 今日 04:00 CST 挂出、仍未完——活单面进、重复面不进
    partial_recent = _real_row(
        "300687.SZ",
        "buy",
        "partially_filled",
        created=datetime(2026, 10, 12, 2, 29, 30),
    )  # 两面都进
    filled_recent = _real_row(
        "002518.SZ", "buy", "filled", created=datetime(2026, 10, 12, 2, 29, 0)
    )  # 只进重复面
    cancelled_recent = _real_row(
        "601988.SH", "buy", "cancelled", created=datetime(2026, 10, 12, 2, 29, 10)
    )  # 两面都不进
    filled_old = _real_row(
        "600000.SH", "buy", "filled", created=datetime(2026, 10, 11, 18, 0)
    )  # 今日但超窗——不进重复面
    yesterday_live = _real_row(
        "600519.SH", "buy", "submitted", created=datetime(2026, 10, 11, 12, 0)
    )  # 隔日活单（对账滞后残留）——不进活单面
    db = _WindowDB(
        snapshot=_snap_row(),
        window_rows=[
            live_old,
            partial_recent,
            filled_recent,
            cancelled_recent,
            filled_old,
            yesterday_live,
        ],
    )

    ctx = await rgs.build_context(
        _req(trading_mode="REAL"), db=db, redis=FakeRedis(), need_windows=True
    )

    sides = ctx.recent_symbol_sides
    assert ("600036.SH", "sell") in sides  # 活单不论多久（今日）
    assert ("300687.SZ", "buy") in sides
    assert all(
        sym not in {"002518.SZ", "601988.SH", "600000.SH", "600519.SH"}
        for sym, _ in sides
    )
    assert len(sides) == 2
    fps = ctx.recent_fingerprints
    assert rgs.order_fingerprint("300687.SZ", "buy", 100.0, 40.0, "limit") in fps
    assert rgs.order_fingerprint("002518.SZ", "buy", 100.0, 40.0, "limit") in fps
    assert len(fps) == 2  # filled_old / cancelled / yesterday 都不进
    # 本单自己的指纹总在（消费侧两侧同一实现）
    assert ctx.fingerprint == rgs.order_fingerprint(
        "600036.SH", "BUY", 100, 40.0, "LIMIT"
    )


@pytest.mark.asyncio
async def test_real_window_query_scopes_account_and_excludes_terminal_statuses(
    monkeypatch,
):
    """查询作用域（哪座账户）与状态清单只体现在 SQL 里——返回值验不到，必须验 SQL。"""
    _freeze(monkeypatch)
    db = _WindowDB(snapshot=_snap_row(), window_rows=[])
    await rgs.build_context(
        _req(trading_mode="REAL"), db=db, redis=FakeRedis(), need_windows=True
    )
    window_sql = next(s for s in db.sqls if "FROM orders" in s)
    assert "tenant_id = :t" in window_sql and "user_id = :u" in window_sql
    assert "trading_mode::text = 'REAL'" in window_sql
    assert "'pending','submitted','partially_filled','filled'" in window_sql
    assert "cancelled" not in window_sql and "rejected" not in window_sql


@pytest.mark.asyncio
async def test_window_read_failure_keeps_empty_fail_open(monkeypatch):
    """窗口查询失败 = 空窗口（不判），且不炸其余字段——存在性判据的 fail-open 方向。"""
    _freeze(monkeypatch)
    db = _WindowDB(
        snapshot=_snap_row(cash=12345.0), window_error=RuntimeError("db down")
    )
    ctx = await rgs.build_context(
        _req(trading_mode="REAL"), db=db, redis=FakeRedis(), need_windows=True
    )
    assert ctx.recent_symbol_sides == ()
    assert ctx.recent_fingerprints == ()
    assert ctx.available_cash == pytest.approx(12345.0)  # 其余字段照常
    assert ctx.fingerprint  # 指纹是本单属性，不依赖窗口查询


@pytest.mark.asyncio
async def test_no_window_query_when_not_needed(monkeypatch):
    _freeze(monkeypatch)
    db = _WindowDB(snapshot=_snap_row())
    ctx = await rgs.build_context(_req(trading_mode="REAL"), db=db, redis=FakeRedis())
    assert not any("FROM orders" in s for s in db.sqls)
    assert ctx.recent_symbol_sides == () and ctx.recent_fingerprints == ()


# ── build_context：sim 窗口（前缀式代码原样喂入）────────────────────


@pytest.mark.asyncio
async def test_sim_windows_feed_rows_as_stored(monkeypatch):
    _freeze(monkeypatch)

    class _Mgr:
        def __init__(self, redis):
            pass

        async def get_account(self, uid, tenant, market="CN"):
            return {"cash": 50000.0, "total_asset": 80000.0, "positions": {}}

    monkeypatch.setattr(
        "backend.services.trade_shared.simulation_manager.SimulationAccountManager",
        _Mgr,
    )

    def _row(symbol, side, status, created, qty=100.0, price=40.0, order_type="limit"):
        return (
            symbol,
            SimpleNamespace(value=side),
            SimpleNamespace(value=status),
            qty,
            price,
            SimpleNamespace(value=order_type),
            created,
        )

    db = _WindowDB(
        window_rows=[
            _row(
                "SH600036", "sell", "submitted", _DAY_START_AWARE + timedelta(hours=3)
            ),
            _row("SZ300687", "buy", "filled", _FROZEN_UTC - timedelta(seconds=30)),
        ]
    )
    ctx = await rgs.build_context(_req(), db=db, redis=FakeRedis(), need_windows=True)
    assert ("SH600036", "sell") in ctx.recent_symbol_sides  # 前缀式原样喂入
    assert rgs.order_fingerprint("SZ300687", "buy", 100.0, 40.0, "limit") in (
        ctx.recent_fingerprints
    )
    assert len(ctx.recent_symbol_sides) == 1  # 已成单不进活单面


# ── 消费面：归一命中与空码守卫 ──────────────────────────────────────


@pytest.mark.unit
def test_self_trade_rule_matches_across_symbol_forms():
    from backend.shared.risk import RiskContext
    from backend.shared.risk.builtin_rules import l3_self_trade
    from backend.shared.risk.contracts import ACTION_REJECT

    ctx = RiskContext(
        market="CN",
        symbol="SH600036",  # 入参前缀式
        side="BUY",
        quantity=100,
        recent_symbol_sides=(("600036.SH", "SELL"),),  # 窗口后缀式——归一后必须撞上
        now_ts=_FROZEN_UTC.timestamp(),
    )
    d = l3_self_trade(ctx, {})
    assert d is not None and d.action == ACTION_REJECT
    # 同向不判、空代码不判
    same_side = RiskContext(
        symbol="SH600036",
        side="BUY",
        quantity=100,
        recent_symbol_sides=(("600036.SH", "BUY"),),
    )
    assert l3_self_trade(same_side, {}) is None
    empty = RiskContext(
        symbol="",
        side="BUY",
        quantity=100,
        recent_symbol_sides=(("600036.SH", "SELL"),),
    )
    assert l3_self_trade(empty, {}) is None


# ── 旗标接线：evaluate_order 按启用规则发窗口查询 ────────────────────


@pytest.mark.asyncio
async def test_evaluate_order_sets_need_windows_from_enabled_rules(monkeypatch):
    seen: list[bool] = []

    async def _b(
        req, *, db, redis, need_counts=False, need_daily_pnl=False, need_windows=False
    ):
        from backend.shared.risk import RiskContext

        seen.append(need_windows)
        return RiskContext(
            market="CN",
            symbol="600036.SH",
            side="BUY",
            quantity=100,
            price=40.0,
            now_ts=_FROZEN_UTC.timestamp(),
            kill_switch=False,
        )

    monkeypatch.setattr(rgs, "build_context", _b)

    with_rule = FakeRedis(
        config=_cfg(rules=json.dumps({"l3.self_trade": {}}, ensure_ascii=False))
    )
    await rgs.evaluate_order(_req(), db=None, redis=with_rule, record=True)
    without = FakeRedis(config=_cfg(rules=json.dumps({"l6.book_invalid": {}})))
    await rgs.evaluate_order(_req(), db=None, redis=without, record=True)
    assert seen == [True, False]
