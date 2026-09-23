"""风控接入测试（T-RC-02）：上下文构建 / 影子-强制-故障三态 / 唯一入口接线 / 全撤。

口径：影子=判定留痕不拦单；强制=REJECT/HALT 拒单；配置不可读/判定异常=fail-closed 拒单。
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from backend.services.trade.services import risk_gate_service as rgs


class FakeRedis:
    """最小 Redis 假体（记录 xadd/hincrby/hset；支持 pipeline 链）。"""

    def __init__(
        self,
        config: dict | None = None,
        *,
        fail_hgetall: bool = False,
        tier: dict | None = None,
    ):
        self.config = {**(config or {})}
        self.tier = dict(tier) if tier else None
        self.fail_hgetall = fail_hgetall
        self.xadds: list[tuple] = []
        self.hincr: dict[str, int] = {}
        self.hset_calls: list[dict] = []

    def hgetall(self, key):
        if self.fail_hgetall:
            raise ConnectionError("redis down")
        if key == rgs.CONFIG_KEY:
            return dict(self.config)
        from backend.shared.risk.tiers import TIER_KEY

        if key == TIER_KEY:
            return dict(self.tier or {})
        return {}

    def hset(self, key, mapping=None, **kw):
        self.hset_calls.append(dict(mapping or {}))
        self.config.update(mapping or {})

    def hincrby(self, key, field, n=1):
        self.hincr[field] = self.hincr.get(field, 0) + int(n)

    def xadd(self, key, fields, maxlen=None, approximate=None):
        self.xadds.append((key, dict(fields)))

    def expire(self, key, ttl):
        pass

    def pipeline(self, transaction=False):
        return self

    def execute(self):
        return []


def _req(**over) -> SimpleNamespace:
    base = {
        "tenant_id": "default",
        "user_id": 1,
        "symbol": "600036.SH",
        "side": "buy",
        "quantity": 100,
        "order_type": "limit",
        "price": 40.0,
        "source": "manual",
        "client_order_id": "",
        "strategy_id": None,
        "portfolio_id": 0,
        "remarks": None,
        "position_side": "long",
        "is_margin_trade": False,
        "bar": None,
        "run_id": "",
        "mirror": False,
        "mirror_source": "",
        "strict_market": None,
    }
    base.update(over)
    return SimpleNamespace(**base)


def _cfg(**over) -> dict:
    import json

    base = {
        "enabled": "true",
        "shadow": "true",
        "version": "1",
        "rules": json.dumps(rgs.DEFAULT_RULES, ensure_ascii=False),
    }
    base.update(over)
    return base


# ── 上下文构建 ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_context_fields(monkeypatch):
    monkeypatch.setattr(
        rgs,
        "_quote_snapshot",
        lambda sym: {
            "Now": "40.60",
            "timestamp": "9999999999",
        },  # 未来戳 → age 为负，仅验字段
    )
    monkeypatch.setattr(
        "backend.services.live_trading.services.real_mirror_service.kill_switch_on",
        lambda redis: False,
    )

    class _Mgr:
        def __init__(self, redis):
            pass

        async def get_account(self, uid, tenant, market="CN"):
            return {
                "cash": 12345.0,
                "total_asset": 100000.0,
                "positions": {
                    "SH600036": {"available_volume": 300, "market_value": 12180.0}
                },
            }

    monkeypatch.setattr(
        "backend.services.trade_shared.simulation_manager.SimulationAccountManager",
        _Mgr,
    )
    ctx = await rgs.build_context(
        _req(order_type="market", price=None, remarks="sltp:600036"),
        db=None,
        redis=FakeRedis(),
    )
    assert ctx.side == "BUY" and ctx.order_type == "MARKET"
    assert ctx.forced_exit is True  # sltp: 前缀识别
    assert ctx.amount == pytest.approx(40.60 * 100)  # 市价单按最新价估额
    assert ctx.available_cash == 12345.0
    assert ctx.sellable_volume == 300  # 前缀式持仓键容错命中
    assert ctx.position_pct == pytest.approx(12180.0 / 100000.0)
    assert ctx.last_price == pytest.approx(40.60)
    assert ctx.kill_switch is False

    # 持仓键为后缀式也要能命中
    class _Mgr2(_Mgr):
        async def get_account(self, uid, tenant, market="CN"):
            return {
                "cash": 1.0,
                "total_asset": 2.0,
                "positions": {"600036.SH": {"available_volume": 7}},
            }

    monkeypatch.setattr(
        "backend.services.trade_shared.simulation_manager.SimulationAccountManager",
        _Mgr2,
    )
    ctx2 = await rgs.build_context(_req(), db=None, redis=FakeRedis())
    assert ctx2.sellable_volume == 7


# ── 持仓合计市值（l1.leverage_cap 的分子）─────────────────────────────


def _fake_db(row, calls: list | None = None):
    """`await db.execute(...)` → 带 fetchone() 的结果（真账户快照查询）。

    `calls` 非空时记录每次 (SQL 文本, 参数)：查询的**作用域**（限定哪座账户、哪个 user）
    不体现在返回值里，只能这样验——那正是「拿错账户的权益判杠杆」这类缺陷的藏身处。
    """

    class _Result:
        def fetchone(self):
            return row

    class _DB:
        async def execute(self, stmt, params=None):
            if calls is not None:
                calls.append((str(stmt), dict(params or {})))
            return _Result()

    return _DB()


def _fake_db_counts(
    *,
    snapshot=None,
    counts=None,
    opened=None,
    ledger=None,
    calls: list | None = None,
):
    """按查询种类分发的假库：快照 / 频率计数 / 当日已开仓标的 / 日度台账。

    四类查询都由 ``await db.execute(...)`` 发出，只能靠 SQL 形状分辨：
    台账含 ``real_account_ledger_daily_snapshots``、已开仓列表含 ``DISTINCT``、
    计数含 ``count(``、快照查询三者都不含（**台账那条必须先判**，否则它会掉进
    快照分支，把快照行当成日初基线读出去）。
    传 ``RuntimeError`` 实例表示"这条查询抛错"（测降级分支）。

    ``calls`` 记录每次 (SQL 文本, 参数)——作用域（哪座账户、哪个交易日）不在返回值里，
    只能这样验。
    """

    class _Result:
        def __init__(self, rows):
            self._rows = rows

        def fetchone(self):
            return self._rows[0] if self._rows else None

        def fetchall(self):
            return self._rows

        def scalar(self):
            # sim 分支的三条计数查询走 .scalar()（只取一行一列）；REAL 分支是
            # 一条 count(*) FILTER 三列的行 → fetchone()。两种形状共用一个假库。
            row = self._rows[0] if self._rows else None
            if row is None:
                return None
            return row[0] if isinstance(row, tuple) else row

    class _DB:
        async def execute(self, stmt, params=None):
            sql = str(stmt)
            if calls is not None:
                calls.append((sql, dict(params or {})))
            if "real_account_ledger_daily_snapshots" in sql:
                if isinstance(ledger, Exception):
                    raise ledger
                return _Result([] if ledger is None else [ledger])
            if "DISTINCT" in sql.upper():
                if isinstance(opened, Exception):
                    raise opened
                return _Result(list(opened or []))
            if "count(" in sql:
                if isinstance(counts, Exception):
                    raise counts
                return _Result([] if counts is None else [counts])
            if isinstance(snapshot, Exception):
                raise snapshot
            return _Result([] if snapshot is None else [snapshot])

    return _DB()


def _snap_row(
    *,
    cash=100000.0,
    total_asset=100000.0,
    market_value=0.0,
    payload=None,
    snapshot_at=None,
    source="tdx_bridge",
    snapshot_date=None,
    account_id="tdx-default-10000001",
):
    """``real_account_snapshots`` 一行的 8 列投影，**顺序即契约**（与 SELECT 一致）：

    ``cash, total_asset, market_value, payload_json, snapshot_at, source,
    snapshot_date, account_id``。
    改 SELECT 的列序必须同步改这里，否则测试会用错位的字段通过。

    ``snapshot_date`` 缺省 None = "日期不可得" → 当日盈亏留 None（不猜）。
    要测「快照是今天的」必须显式传 ``_today_cst()``。
    """
    return (
        cash,
        total_asset,
        market_value,
        {"positions": []} if payload is None else payload,
        snapshot_at,
        source,
        snapshot_date,
        account_id,
    )


def _today_cst():
    """当天（上海口径）——与 build_context 里 `day_start_cst` 同一口径。"""
    from datetime import datetime

    return datetime.now(tz=rgs.CST).date()


def _ledger_row(*, day_open_equity=100000.0, snapshot_date=None):
    """``real_account_ledger_daily_snapshots`` 查询的一行：``day_open_equity, snapshot_date``。"""
    return (
        day_open_equity,
        snapshot_date if snapshot_date is not None else _today_cst(),
    )


def _patch_ctx_deps(monkeypatch):
    monkeypatch.setattr(
        rgs, "_quote_snapshot", lambda sym: {"Now": "40.00", "timestamp": "9999999999"}
    )
    monkeypatch.setattr(
        "backend.services.live_trading.services.real_mirror_service.kill_switch_on",
        lambda redis: False,
    )


@pytest.mark.unit
def test_join_checked_marks_truncation():
    """超长 ``checked`` 截断时**必须**留标记：把截断的清单当完整清单读，会把"没跑过"
    误读成"跑过且放行"——恰好是本字段存在的意义（影子报告靠它统计覆盖率）。
    旧实现 ``[:600]`` 还会截在 id 中间，产出半个 id，两种误读都指向"看起来跑过"。
    """
    ids = tuple(f"l1.rule_{i:02d}" for i in range(80))  # 远超 600 字符
    out = rgs._join_checked(ids)
    assert len(out) <= rgs._CHECKED_MAXLEN
    head, suffix = out.rsplit(",+", 1)  # 截断标记形如 ",+37more"
    assert suffix.endswith("more")
    listed = head.split(",")
    assert len(listed) + int(suffix.removesuffix("more")) == len(ids)
    assert all(x in ids for x in listed)  # 不留半个 id
    # 不超长时不加标记（今日 15 条规则 ≈ 200 字符，走不到截断分支）
    assert rgs._join_checked(("l1.a", "l1.b")) == "l1.a,l1.b"


@pytest.mark.unit
def test_sum_position_value_skips_zero_volume_rows():
    rows = [
        {"volume": 200, "market_value": 8000.0},
        {"volume": 0, "market_value": 0.0},  # 已清仓幻影行（P0.4 那 4 个标的如此）
        {"volume": 0, "market_value": 999.0},  # 零量但残留市值：计入会虚增分子→误拒买单
    ]
    assert rgs._sum_position_value(rows) == pytest.approx(8000.0)


@pytest.mark.unit
def test_sum_position_value_is_gross_exposure():
    # 空头计入总敞口、不抵扣多头。两种空头记法都要按住：
    # sim 是 side=short + 正市值；若某数据源用**负市值**记，净额会把多头抵掉。
    rows = [
        {"volume": 100, "market_value": 4000.0},
        {"volume": 100, "market_value": 2000.0, "side": "short"},
        {"volume": 100, "market_value": -1500.0},
    ]
    assert rgs._sum_position_value(rows) == pytest.approx(7500.0)


@pytest.mark.unit
def test_sum_position_value_missing_market_value_is_unknown():
    # 有一行有量却缺市值 → 整个合计不可信（交规则 fail-closed），而不是静默少算
    assert (
        rgs._sum_position_value(
            [{"volume": 100, "market_value": 4000.0}, {"volume": 100}]
        )
        is None
    )
    # 零量行缺市值无所谓——它本就不计入
    assert rgs._sum_position_value(
        [{"volume": 100, "market_value": 4000.0}, {"volume": 0}]
    ) == pytest.approx(4000.0)


@pytest.mark.unit
def test_sum_position_value_empty_vs_unknown():
    # 「没有持仓」与「结构不可得」是两件事：前者 0.0（可信），后者 None（fail-closed）
    assert rgs._sum_position_value([]) == 0.0
    assert rgs._sum_position_value({}) == 0.0
    assert rgs._sum_position_value(None) is None
    assert rgs._sum_position_value([None, {"volume": 0, "market_value": 1.0}]) == 0.0


@pytest.mark.unit
def test_sum_position_value_rejects_unknown_shapes():
    """形态不认识 → None，**绝不**静默算成 0.0（那是"清仓"的样子）。

    三形态都会落进"迭代下去全是非 dict → 一行都没计入 → 合计 0.0"的陷阱：
    双层编码的 JSON 字符串（payload 被 json.dumps 了两次，真实发生过）、
    字符串本身（迭代字符串得到的是**一个个字符**）、值不是 dict 的映射。
    0.0 的语义是"确实空仓"，用它顶替"读不懂"= 唯一的总敞口闸静默关闭。
    """
    assert rgs._sum_position_value('[{"volume": 100, "market_value": 1.0}]') is None
    assert rgs._sum_position_value("[]") is None
    assert rgs._sum_position_value(0) is None
    assert rgs._sum_position_value({"SH600036": "oops"}) is None
    assert (
        rgs._sum_position_value([["nested"], {"volume": 1, "market_value": 2.0}])
        is None
    )
    # 而"真的空"仍然是 0.0 —— 守卫不能把正常状态也拒了
    assert rgs._sum_position_value({"SH600036": None}) == 0.0


@pytest.mark.unit
def test_sum_position_value_accepts_sim_mapping_shape():
    # 模拟账户是 {code: pos} 映射（真账户是 list[dict]）
    assert rgs._sum_position_value(
        {"SH600036": {"volume": 300, "market_value": 12180.0}}
    ) == pytest.approx(12180.0)


@pytest.mark.asyncio
async def test_build_context_real_sums_positions_and_skips_phantoms(monkeypatch):
    """真账户分支：合计 = 有量持仓之和（零量幻影行不计），字段取自同一份快照。"""
    _patch_ctx_deps(monkeypatch)
    payload = {
        "broker_type": "tdx",
        "source": "tdx_bridge",
        "positions": [
            {
                "symbol": "600036.SH",
                "volume": 200,
                "available_volume": 0,
                "market_value": 8720.0,
            },
            {
                "symbol": "300687.SZ",
                "volume": 300,
                "available_volume": 300,
                "market_value": 4500.0,
            },
            {
                "symbol": "002518.SZ",
                "volume": 0,
                "available_volume": 0,
                "market_value": 0.0,
            },
            {
                "symbol": "603678.SH",
                "volume": 0,
                "available_volume": 0,
                "market_value": 0.0,
            },
        ],
    }
    ctx = await rgs.build_context(
        _req(trading_mode="REAL"),
        db=_fake_db(
            # PG numeric 常回字符串；列值刻意取小（13180 < Σ13220）→ 走"取大"的逐行一侧
            _snap_row(
                cash="867602.42",
                total_asset="919873.42",
                market_value="13180.00",
                payload=payload,
            )
        ),
        redis=FakeRedis(),
    )
    assert ctx.available_cash == pytest.approx(867602.42)
    assert ctx.total_assets == pytest.approx(919873.42)
    assert ctx.total_position_value == pytest.approx(13220.0)
    assert ctx.position_pct == pytest.approx(8720.0 / 919873.42)
    assert ctx.account_source == "tdx_bridge"


@pytest.mark.asyncio
async def test_build_context_real_takes_larger_of_column_and_rows(monkeypatch):
    """分子两口径取**较大者**（偏严）：列 > 逐行时用列。

    实测两座真账户：qmt 两口径逐分相同；tdx 逐行口径大 312–521 元（行市值由 QuantDB
    收盘价回填、非券商 mark）。取大是不让任何一侧的少算变成静默放松。
    """
    _patch_ctx_deps(monkeypatch)
    payload = {
        "positions": [
            {"symbol": "600036.SH", "volume": 200, "market_value": 8720.0},
        ]
    }
    ctx = await rgs.build_context(
        _req(trading_mode="REAL"),
        db=_fake_db(_snap_row(market_value=9100.0, payload=payload)),
        redis=FakeRedis(),
    )
    assert ctx.total_position_value == pytest.approx(9100.0)


@pytest.mark.asyncio
async def test_build_context_real_all_cleared_is_zero_not_unknown(monkeypatch):
    """全部清仓（只剩 0 量历史行）是**正常状态**：合计 0.0，不是"未知"。

    按"有没有 0 量以上的行"判，会把刚清仓的账户误判成快照故障 → fail-closed 拦死买单。
    """
    _patch_ctx_deps(monkeypatch)
    payload = {"positions": [{"symbol": "600036.SH", "volume": 0, "market_value": 0.0}]}
    ctx = await rgs.build_context(
        _req(trading_mode="REAL"),
        db=_fake_db(_snap_row(cash="100000", total_asset="100000", payload=payload)),
        redis=FakeRedis(),
    )
    assert ctx.total_position_value == 0.0


@pytest.mark.asyncio
async def test_build_context_real_queries_yesterday_scope_for_opened_today(monkeypatch):
    """`opened_today` 的查询**作用域**必须落在本租户本账户本交易日：
    漏了 user 维度 → 别人的当日买入算成你的新开仓（凭空拦单）；
    漏了 trading_mode → 模拟盘的单子挤占真单额度（两个盘口互相污染）。
    """
    _patch_ctx_deps(monkeypatch)
    calls: list = []
    ctx = await rgs.build_context(
        _req(trading_mode="REAL", user_id=10000001),
        db=_fake_db_counts(
            snapshot=_snap_row(),
            counts=(0, 0, 0),
            opened=[("600036.SH",), ("000001.SZ",)],
            calls=calls,
        ),
        redis=FakeRedis(),
        need_counts=True,
    )
    assert ctx.opened_today == ("600036.SH", "000001.SZ")

    sql, params = next(c for c in calls if "DISTINCT symbol" in c[0])
    assert "tenant_id = :t" in sql and "user_id = :u" in sql
    assert "trading_mode::text = 'REAL'" in sql
    assert "side::text = 'buy'" in sql
    assert "filled_quantity > 0" in sql  # 判据是"有成交量"，不是状态标签
    # **时间谓词必须在**：少了它=全历史都算"今天"，每个新标的都撞上限
    assert "created_at >= :day" in sql and "filled_at >= :day_cst" in sql
    # 两个口径的当日零点都进查询：orders 的两列时钟口径不一（见 build_context 注释）
    assert params["day"] < params["day_cst"]
    assert params["u"] == "10000001"


@pytest.mark.asyncio
async def test_build_context_real_opened_today_failure_is_none_not_empty(monkeypatch):
    """查询失败 → **None**（不可得），绝不是 ()（"今天还没买过"）。

    预置成空集合等于在最需要这条闸的时候把它整个关掉：新开仓上限看着在跑、
    实际每个新标的都放行。None 才让规则走 fail-closed 拒新开仓。
    """
    _patch_ctx_deps(monkeypatch)
    ctx = await rgs.build_context(
        _req(trading_mode="REAL"),
        db=_fake_db_counts(
            snapshot=_snap_row(), counts=(0, 0, 0), opened=RuntimeError("pg down")
        ),
        redis=FakeRedis(),
        need_counts=True,
    )
    assert ctx.opened_today is None
    # 其余字段照常（单条查询的故障不该拖垮整份上下文）
    assert ctx.total_assets is not None


@pytest.mark.asyncio
async def test_build_context_sim_opened_today_prefix_scope(monkeypatch):
    """模拟盘分支：同一口径（有成交量的买单）+ 当日零点，代码保持库内前缀式。"""
    _patch_ctx_deps(monkeypatch)
    calls: list = []
    ctx = await rgs.build_context(
        _req(),  # 无 trading_mode → sim 分支
        db=_fake_db_counts(
            snapshot=None,
            counts=(0, 0, 0),
            opened=[("SH600036",), ("SZ000001",)],
            calls=calls,
        ),
        redis=FakeRedis(),
        need_counts=True,
    )
    assert ctx.opened_today == ("SH600036", "SZ000001")
    sel, _ = next(c for c in calls if "DISTINCT" in c[0].upper())
    assert "sim_orders" in sel
    # 漏了任一列都会悄悄放大计数：不筛方向=卖出也算新开仓；不筛成交量=挂单就占额度
    assert "sim_orders.side" in sel and "filled_quantity" in sel


@pytest.mark.asyncio
async def test_build_context_skips_opened_query_without_need_counts(monkeypatch):
    """没启用相关规则时**不发**这条查询（每种 need_counts=False 的路径都少两次
    库往返）；代价是此时 opened_today 恒为 None —— 规则未被启用，它读不到也无妨。
    """
    _patch_ctx_deps(monkeypatch)
    calls: list = []
    ctx = await rgs.build_context(
        _req(trading_mode="REAL"),
        db=_fake_db_counts(snapshot=_snap_row(), counts=None, opened=None, calls=calls),
        redis=FakeRedis(),
        need_counts=False,
    )
    assert ctx.opened_today is None
    assert not any("DISTINCT symbol" in c[0] for c in calls)


@pytest.mark.asyncio
async def test_build_context_real_empty_positions_is_flat(monkeypatch):
    _patch_ctx_deps(monkeypatch)
    ctx = await rgs.build_context(
        _req(trading_mode="REAL"),
        db=_fake_db(_snap_row(payload={"positions": []})),
        redis=FakeRedis(),
    )
    assert ctx.total_position_value == 0.0


@pytest.mark.asyncio
async def test_build_context_real_missing_positions_key_falls_back_to_column(
    monkeypatch,
):
    """`positions` 键缺失（结构不认识）时**不再**直接判 None：用券商自报的账户级列值。

    列值与分母 ``total_asset`` 同源同一次回报，是权威总额——用它比 fail-closed 更准，
    也不构成"静默"（少算才静默；这只是换了个更可靠的口径）。
    """
    _patch_ctx_deps(monkeypatch)
    ctx = await rgs.build_context(
        _req(trading_mode="REAL"),
        db=_fake_db(_snap_row(market_value="52584.00", payload={"broker_type": "tdx"})),
        redis=FakeRedis(),
    )
    assert ctx.total_position_value == pytest.approx(52584.0)
    assert ctx.available_cash == pytest.approx(100000.0)  # 其余字段照常解析


@pytest.mark.asyncio
async def test_build_context_real_no_rows_basis_is_unknown(monkeypatch):
    """两口径都不可得（键缺失 **且** 列值不可用）→ None，交规则 fail-closed 拒买。"""
    _patch_ctx_deps(monkeypatch)
    ctx = await rgs.build_context(
        _req(trading_mode="REAL"),
        db=_fake_db(
            _snap_row(market_value=None, payload={"broker_type": "tdx"}),
        ),
        redis=FakeRedis(),
    )
    assert ctx.total_position_value is None


@pytest.mark.asyncio
async def test_build_context_real_row_without_market_value_falls_back_to_column(
    monkeypatch,
):
    """某行有量无市值 → 逐行口径不可信（None）；列值在则用它，两口径都缺才 None。"""
    _patch_ctx_deps(monkeypatch)
    payload = {
        "positions": [
            {"symbol": "600036.SH", "volume": 200, "market_value": 8720.0},
            {"symbol": "300687.SZ", "volume": 300},  # 有量无市值 → 逐行合计不可信
        ]
    }
    ctx = await rgs.build_context(
        _req(trading_mode="REAL"),
        db=_fake_db(_snap_row(total_asset=2.0, market_value=9000.0, payload=payload)),
        redis=FakeRedis(),
    )
    assert ctx.total_position_value == pytest.approx(9000.0)

    ctx2 = await rgs.build_context(
        _req(trading_mode="REAL"),
        db=_fake_db(_snap_row(total_asset=2.0, market_value=None, payload=payload)),
        redis=FakeRedis(),
    )
    assert ctx2.total_position_value is None


@pytest.mark.asyncio
async def test_build_context_real_scopes_to_user_and_source(monkeypatch):
    """查询必须**限定到具体账户**：user_id（含历史别名）+ 券商 source。

    同一 (tenant,user) 下 tdx_bridge 与 qmt_exec 是两座**互不相交**的真账户（实测规模差
    ~25 倍、持仓不重叠）。不限定 source 的"取最新一行"等于在两座账户间掷硬币——拿 A 的
    权益判 B 的杠杆，比不判更危险。user_id 要能命中历史别名（00000001/1/0/admin），
    否则本闸对着空账户 fail-closed，把全部真单拦死。
    """
    _patch_ctx_deps(monkeypatch)
    calls: list = []
    await rgs.build_context(
        _req(trading_mode="REAL", user_id=10000001, source="tdx_l2"),
        db=_fake_db(_snap_row(), calls=calls),
        redis=FakeRedis(),
    )
    sql, params = calls[-1]
    assert "user_id IN" in sql and "source = :s" in sql
    assert params["t"] == "default"
    assert "10000001" in params["u"] and "00000001" in params["u"]
    assert params["s"] == "tdx_bridge"

    # 订单来自 qmt 臂 → 读 qmt 那座账户
    calls.clear()
    await rgs.build_context(
        _req(trading_mode="REAL", source="qmt_sltp"),
        db=_fake_db(_snap_row(source="qmt_exec"), calls=calls),
        redis=FakeRedis(),
    )
    assert calls[-1][1]["s"] == "qmt_exec"


@pytest.mark.asyncio
async def test_build_context_real_manual_order_uses_active_broker(monkeypatch):
    """来源看不出券商（manual/desk/copilot）→ 退回平台活跃券商解析（与账户页同一只账户）。"""
    _patch_ctx_deps(monkeypatch)
    monkeypatch.setattr(
        "backend.shared.real_positions.active_broker_type", lambda: "qmt_exec"
    )
    calls: list = []
    await rgs.build_context(
        _req(trading_mode="REAL", source="manual"),
        db=_fake_db(_snap_row(source="qmt_exec"), calls=calls),
        redis=FakeRedis(),
    )
    assert calls[-1][1]["s"] == "qmt_exec"


@pytest.mark.asyncio
async def test_build_context_real_carries_account_age_and_source(monkeypatch):
    """快照时点 → ``account_age_s``（naive UTC 口径），并带上来源名。

    该列是 naive UTC（写入侧惯例）：按容器会话时区（Asia/Shanghai）算会凭空多 8 小时。
    时点不可得时必须是 None —— **不是 0**（0 的语义是"刚更新"，会把不可得伪装成新鲜）。
    """
    _patch_ctx_deps(monkeypatch)
    from datetime import timedelta

    from backend.shared.utc_datetime import utc_now

    snap_at = (utc_now() - timedelta(seconds=125)).replace(tzinfo=None)  # 库里是 naive
    ctx = await rgs.build_context(
        _req(trading_mode="REAL"),
        db=_fake_db(_snap_row(snapshot_at=snap_at, source="qmt_exec")),
        redis=FakeRedis(),
    )
    assert ctx.account_age_s == pytest.approx(125.0, abs=5.0)
    assert ctx.account_source == "qmt_exec"

    ctx2 = await rgs.build_context(
        _req(trading_mode="REAL"),
        db=_fake_db(_snap_row(snapshot_at=None)),
        redis=FakeRedis(),
    )
    assert ctx2.account_age_s is None


@pytest.mark.asyncio
async def test_build_context_sim_sums_positions(monkeypatch):
    _patch_ctx_deps(monkeypatch)

    class _Mgr:
        def __init__(self, redis):
            pass

        async def get_account(self, uid, tenant, market="CN"):
            return {
                "cash": 2241.35,
                "total_asset": 1065393.35,
                "positions": {
                    "SH600036": {
                        "volume": 200,
                        "available_volume": 0,
                        "market_value": 8720.0,
                    },
                    "SZ300251": {
                        "volume": 0,
                        "available_volume": 0,
                        "market_value": 999.0,
                    },  # 零量：不计入
                },
            }

    monkeypatch.setattr(
        "backend.services.trade_shared.simulation_manager.SimulationAccountManager",
        _Mgr,
    )
    ctx = await rgs.build_context(_req(), db=None, redis=FakeRedis())
    assert ctx.total_position_value == pytest.approx(8720.0)
    # 模拟账户是当场读的账本（不落快照表）：新鲜由构造保证，年龄 0.0 而非 None
    assert ctx.account_age_s == 0.0
    assert ctx.account_source == "sim"


@pytest.mark.asyncio
async def test_build_context_sim_takes_larger_of_account_and_rows(monkeypatch):
    """模拟分支与真账户分支**同口径**：账户自报 market_value × 逐行重建取较大者。"""
    _patch_ctx_deps(monkeypatch)

    class _Mgr:
        def __init__(self, redis):
            pass

        async def get_account(self, uid, tenant, market="CN"):
            return {
                "cash": 1.0,
                "total_asset": 100000.0,
                "market_value": 9000.0,  # 账户自报 > 逐行 8720
                "positions": {
                    "SH600036": {"volume": 200, "market_value": 8720.0},
                },
            }

    monkeypatch.setattr(
        "backend.services.trade_shared.simulation_manager.SimulationAccountManager",
        _Mgr,
    )
    ctx = await rgs.build_context(_req(), db=None, redis=FakeRedis())
    assert ctx.total_position_value == pytest.approx(9000.0)


@pytest.mark.asyncio
async def test_build_context_sim_missing_positions_key_is_unknown(monkeypatch):
    """账户结构异常（无 positions 键）→ 未知，与真账户分支同口径。"""
    _patch_ctx_deps(monkeypatch)

    class _Mgr:
        def __init__(self, redis):
            pass

        async def get_account(self, uid, tenant, market="CN"):
            return {"cash": 1000.0, "total_asset": 1000.0}

    monkeypatch.setattr(
        "backend.services.trade_shared.simulation_manager.SimulationAccountManager",
        _Mgr,
    )
    ctx = await rgs.build_context(_req(), db=None, redis=FakeRedis())
    assert ctx.total_position_value is None


# ── 三态：未配置 / 影子 / 强制 / 故障 ────────────────────────────────


@pytest.mark.asyncio
async def test_new_buys_rule_in_config_triggers_count_query(monkeypatch):
    """启用了新开仓上限的配置必须把 `need_counts` 打开——否则 `opened_today` 恒为
    None，规则**每一笔新开仓都 fail-closed 拒**（不是"没过线"，是"全拦"）。

    这是配置→上下文接线上的一个死结：两处都各自看起来对（规则写对了、查询也写对了），
    中间那个布尔量没跟上就整条链失效，而且失效形态是"拦截率高得离谱"而非报错。
    """
    redis = FakeRedis(
        config=_cfg(rules=json.dumps({"l1.new_buys_per_day": {"max_new_buys": 3}}))
    )
    seen: dict = {}

    async def _ctx(req, *, db, redis, need_counts=False, need_daily_pnl=False):
        from backend.shared.risk import RiskContext

        seen["need_counts"] = need_counts
        return RiskContext(market="CN", symbol="600036.SH", side="BUY", quantity=100)

    monkeypatch.setattr(rgs, "build_context", _ctx)
    await rgs.check_order(_req(), db=None, redis=redis)

    assert seen.get("need_counts") is True


@pytest.mark.asyncio
async def test_check_order_unconfigured_passes():
    redis = FakeRedis(config={})
    check = await rgs.check_order(_req(), db=None, redis=redis)
    assert check.passed and not check.enforced
    assert redis.xadds and redis.xadds[-1][1]["verdict"] == "disabled"


@pytest.mark.asyncio
async def test_check_order_shadow_records_but_passes():
    redis = FakeRedis(config=_cfg())

    async def _ctx(req, *, db, redis, need_counts=False, need_daily_pnl=False):
        from backend.shared.risk import RiskContext

        return RiskContext(
            market="CN",
            symbol="600036.SH",
            side="BUY",
            quantity=100,
            now_ts=0.0,
            kill_switch=True,
        )  # 急停 → HALT 判定

    import backend.services.trade.services.risk_gate_service as mod

    monkey = pytest.MonkeyPatch()
    monkey.setattr(mod, "build_context", _ctx)
    try:
        check = await rgs.check_order(_req(), db=None, redis=redis)
    finally:
        monkey.undo()
    assert check.passed and not check.enforced  # 影子不拦
    rec = redis.xadds[-1][1]
    assert rec["verdict"] == "halt" and rec["enforced"] == "false"
    assert redis.hincr.get("halted") == 1


@pytest.mark.asyncio
async def test_decision_record_carries_checked_rules(monkeypatch):
    """留痕要写明「这单实际跑过哪些规则」，不能只记拦下来的那几条。

    影子期的核心问题是「`l1.leverage_cap` 到底评估了多少单」。只记 decisions（拦下来的）
    时，一条**从没触发过**的规则与一条**根本没加载**的规则在证据流里长得一模一样，
    据此算「今天评估 N 单」会得到 0，而真相是「每单都评估了、只是没越线」。
    """

    # Arrange
    redis = FakeRedis(config=_cfg())

    async def _ctx(req, *, db, redis, need_counts=False, need_daily_pnl=False):
        from backend.shared.risk import RiskContext

        return RiskContext(
            market="CN",
            symbol="600036.SH",
            side="BUY",
            quantity=100,
            now_ts=0.0,
            kill_switch=True,
        )

    import backend.services.trade.services.risk_gate_service as mod

    monkeypatch.setattr(mod, "build_context", _ctx)

    # Act
    await rgs.check_order(_req(), db=None, redis=redis)

    # Assert：配置里的 13 条 + 2 条 always_on（急停/时段）全部在册
    checked = redis.xadds[-1][1].get("checked", "").split(",")
    assert len(checked) == len(
        set(rgs.DEFAULT_RULES) | {"l0.kill_switch", "l0.session"}
    )
    assert "l1.leverage_cap" in checked
    assert "l0.session" in checked


@pytest.mark.asyncio
async def test_check_order_enforce_rejects(monkeypatch):
    redis = FakeRedis(config=_cfg(shadow="false"))

    async def _ctx(req, *, db, redis, need_counts=False, need_daily_pnl=False):
        from backend.shared.risk import RiskContext

        return RiskContext(
            market="CN",
            symbol="600036.SH",
            side="BUY",
            quantity=100,
            now_ts=0.0,
            kill_switch=True,
        )

    import backend.services.trade.services.risk_gate_service as mod

    monkeypatch.setattr(mod, "build_context", _ctx)
    check = await rgs.check_order(_req(), db=None, redis=redis)
    assert not check.passed and check.enforced
    assert check.rule_id == "l0.kill_switch"
    assert redis.xadds[-1][1]["enforced"] == "true"


@pytest.mark.asyncio
async def test_check_order_fail_closed_paths(monkeypatch):
    # ① 配置不可读 → 拒
    check = await rgs.check_order(_req(), db=None, redis=FakeRedis(fail_hgetall=True))
    assert not check.passed and check.rule_id == "l0.config"

    # ② 判定异常 → 拒
    import backend.services.trade.services.risk_gate_service as mod

    monkeypatch.setattr(
        mod,
        "build_context",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    check2 = await rgs.check_order(_req(), db=None, redis=FakeRedis(config=_cfg()))
    assert not check2.passed and check2.rule_id == "l0.evaluate"


# ── 唯一入口接线 ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_submit_order_blocked_by_gate(monkeypatch):
    from backend.services.simulation.services import order_router as orouter

    called = {"submitted": False}

    async def _fake_immediate(db, manager, req):
        called["submitted"] = True
        return orouter.RouterOutcome(success=True)

    async def _fake_check(req, *, db, redis):
        return rgs.RiskCheck(
            passed=False, enforced=True, rule_id="l3.lot_size", reason="买入数量非整手"
        )

    monkeypatch.setattr(orouter, "_submit_immediate", _fake_immediate)
    monkeypatch.setattr(rgs, "check_order", _fake_check)
    # submit_order 内部按名导入 check_order —— 直接打补丁到其导入源
    import backend.services.trade.services.risk_gate_service as mod

    monkeypatch.setattr(mod, "check_order", _fake_check)

    out = await orouter.submit_order(None, FakeRedis(), _req())
    assert (
        not out.success and "风控拒单" in out.message and "l3.lot_size" in out.message
    )
    assert called["submitted"] is False  # 拒单在链前，无任何建单副作用


@pytest.mark.asyncio
async def test_submit_order_passes_through_when_gate_ok(monkeypatch):
    from backend.services.simulation.services import order_router as orouter

    async def _fake_immediate(db, manager, req):
        return orouter.RouterOutcome(success=True, order_id="o1")

    monkeypatch.setattr(orouter, "_submit_immediate", _fake_immediate)
    import backend.services.trade.services.risk_gate_service as mod

    monkeypatch.setattr(mod, "check_order", lambda req, *, db, redis: _ok())

    async def _ok():
        return rgs.RiskCheck(passed=True)

    out = await orouter.submit_order(None, FakeRedis(), _req())
    assert out.success and out.order_id == "o1"


# ── 全撤 cancel_all ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_all_counts_and_isolates_failures(monkeypatch):
    from backend.services.simulation.services import order_service as osvc
    from backend.services.simulation.services import order_router as orouter

    class _Order:
        def __init__(self, oid):
            self.order_id = oid

    class _Svc:
        def __init__(self, db):
            pass

        async def list_orders(self, tenant_id, user_id, *, status=None, limit=50):
            return (
                [_Order("o1"), _Order("o2")] if status == "pending" else [_Order("o3")]
            )

        async def cancel_order(self, order, reason=None):
            if order.order_id == "o2":
                raise ValueError("Cannot cancel order in status: filled")
            return order

    monkeypatch.setattr(osvc, "SimOrderService", _Svc)
    result = await orouter.cancel_all(None, FakeRedis(), tenant_id="default", user_id=1)
    assert result["cancelled"] == 2 and result["failed"] == 1
    assert result["errors"][0]["order_id"] == "o2"


@pytest.mark.unit
def test_single_chokepoint_source_guard():
    """G：下单唯一入口必须恰好一处调用风控卡点（防旁路回潮）。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    src = (root / "backend/services/simulation/services/order_router.py").read_text(
        encoding="utf-8"
    )
    assert src.count("_risk_check(req, db=db, redis=redis)") == 1
    assert "risk_gate_service" in src


# ── T-RC-02b：直连路径（TDX 滚动/L2）接线 + 盘后入队语义（影子实测修复）──────


class _FakeSessionCtx:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *a):
        return False


@pytest.mark.asyncio
async def test_check_direct_order_builds_real_req_and_shadow_pass(monkeypatch):
    captured = {}

    async def _fake_check(req, *, db, redis):
        captured["req"] = req
        return rgs.RiskCheck(passed=True, enforced=False, version=1)

    monkeypatch.setattr(rgs, "check_order", _fake_check)
    monkeypatch.setattr(
        "backend.shared.database_manager_v2.get_session",
        lambda read_only=True: _FakeSessionCtx(),
    )
    check = await rgs.check_direct_order(
        tenant_id="default",
        user_id="10000001",
        symbol="600036.SH",
        side="sell",
        quantity=100,
        price=None,
        order_type="market",
        source="tdx_rolling",
        remarks="rolling_x_600036.SH_sell",
        redis_client=FakeRedis(),
    )
    assert check.passed
    req = captured["req"]
    assert isinstance(req, rgs.DirectOrderReq)
    assert req.trading_mode == "REAL" and req.source == "tdx_rolling"
    assert req.user_id == 10000001 and req.side == "sell"
    assert req.remarks == "rolling_x_600036.SH_sell"


@pytest.mark.unit
def test_l0_session_queued_intent_downgrades():
    from datetime import datetime

    from backend.shared.risk import RiskContext
    from backend.shared.risk.builtin_rules import l0_session

    ts = datetime(2026, 9, 18, 15, 34, tzinfo=rgs.CST).timestamp()  # 交易日盘后
    queued = l0_session(RiskContext(side="BUY", queued_intent=True, now_ts=ts), {})
    assert queued is not None and queued.action == "WARN"
    plain = l0_session(RiskContext(side="BUY", queued_intent=False, now_ts=ts), {})
    assert plain is not None and plain.action == "REJECT"


@pytest.mark.unit
def test_l3_stale_quote_queued_intent_downgrades():
    from backend.shared.risk import RiskContext
    from backend.shared.risk.builtin_rules import l3_stale_quote

    # 时刻不可得 + 有市场价 + 入队 → 告警（注意用 last_price 而非委托限价 price）
    queued = l3_stale_quote(
        RiskContext(
            quote_age_s=None,
            last_price=7.5,
            price=None,
            queued_intent=True,
            price_source="fallback_close",
        ),
        {},
    )
    assert queued is not None and queued.action == "WARN"
    # 陈旧但已知 age（现场实测：盘后快照 age≈87min）+ 入队 → 同样降级告警
    stale_queued = l3_stale_quote(
        RiskContext(
            quote_age_s=5220.0,
            last_price=7.57,
            queued_intent=True,
            price_source="snapshot",
        ),
        {},
    )
    assert stale_queued is not None and stale_queued.action == "WARN"
    # 非入队 + 陈旧 → 拒
    strict = l3_stale_quote(
        RiskContext(quote_age_s=5220.0, last_price=7.5, queued_intent=False), {}
    )
    assert strict is not None and strict.action == "REJECT"
    none_price = l3_stale_quote(
        RiskContext(quote_age_s=None, last_price=None, queued_intent=True), {}
    )
    assert none_price is not None and none_price.action == "REJECT"


@pytest.mark.asyncio
async def test_build_context_after_hours_fallback_price_and_queued(monkeypatch):
    """盘后入队：快照缺失 → 最近收盘兜底价 + queued_intent（时段/时效校验延后）。"""
    from datetime import datetime as _dt

    class _FakeDT(_dt):
        @classmethod
        def now(cls, tz=None):
            base = _dt(2026, 9, 18, 15, 34, tzinfo=rgs.CST)
            return base.astimezone(tz) if tz else base.replace(tzinfo=None)

    monkeypatch.setattr(rgs, "datetime", _FakeDT)
    monkeypatch.setattr(rgs, "_quote_snapshot", lambda sym: {})  # 盘后快照缺失
    monkeypatch.setattr(rgs, "_last_close_fallback", lambda sym: 7.57)
    monkeypatch.setattr(
        "backend.services.live_trading.services.real_mirror_service.kill_switch_on",
        lambda redis: False,
    )

    class _Mgr:
        def __init__(self, redis):
            pass

        async def get_account(self, uid, tenant, market="CN"):
            return None

    monkeypatch.setattr(
        "backend.services.trade_shared.simulation_manager.SimulationAccountManager",
        _Mgr,
    )
    ctx = await rgs.build_context(
        _req(order_type="market", price=None, source="co_pilot"),
        db=None,
        redis=FakeRedis(),
    )
    assert ctx.queued_intent is True
    assert ctx.price_source == "fallback_close"
    assert ctx.last_price == pytest.approx(7.57)
    assert ctx.amount == pytest.approx(7.57 * 100)
    assert ctx.quote_age_s is None


@pytest.mark.unit
def test_direct_paths_source_guard():
    """G：TDX 直连下单路径必须过闸（滚动/L2 共用 place_rolling_orders + L2 重挂各一处）。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    rolling = (
        root / "backend/services/live_trading/services/tdx_rolling_trade_service.py"
    ).read_text(encoding="utf-8")
    l2 = (root / "backend/services/live_trading/services/tdx_l2_realtime.py").read_text(
        encoding="utf-8"
    )
    assert "check_direct_order" in rolling
    assert "check_direct_order" in l2


# ── 预检（推送确认面板逐笔跑的那条路）────────────────────────────────


@pytest.mark.asyncio
async def test_preflight_returns_full_verdict_without_any_trace(monkeypatch):
    """预检：判定全貌照给，**留痕一条不写**。

    这是本次推送功能的关键不变式。`check_order` 每次调用都 `hincrby evaluated`，
    若预检复用它，一次「选 10 只点推送」就等于往当日 metrics 灌 10 次判定 ——
    影子报告会显示「今天拦了 10 单」，而那 10 单**一次都没发出去**。
    """
    # Arrange：急停触发 HALT
    redis = FakeRedis(config=_cfg())

    async def _ctx(req, *, db, redis, need_counts=False, need_daily_pnl=False):
        from backend.shared.risk import RiskContext

        return RiskContext(
            market="CN",
            symbol="600036.SH",
            side="BUY",
            quantity=100,
            now_ts=0.0,
            kill_switch=True,
        )

    import backend.services.trade.services.risk_gate_service as mod

    monkeypatch.setattr(mod, "build_context", _ctx)

    # Act
    verdict = await rgs.preflight_order(_req(), db=None, redis=redis)

    # Assert：裁定可见。**要的是 decisions 全表而不是一条主因** —— 同一笔单会同时踩中
    # 多条（急停 + 时段 + 陈旧行情），确认面板要按规则前缀分组呈现环境闸门与标的级原因。
    assert verdict.verdict == "halt"
    assert verdict.shadow is True  # 影子期：会拦但不拦
    assert verdict.passed is True
    rule_ids = [d["rule_id"] for d in verdict.decisions]
    assert "l0.kill_switch" in rule_ids and len(rule_ids) > 1
    assert all(
        {"rule_id", "level", "action", "reason", "evidence"} <= set(d)
        for d in verdict.decisions
    )
    # 但一条留痕都没有（决策流 + 计数双向为空）
    assert redis.xadds == []
    assert redis.hincr == {}


@pytest.mark.asyncio
async def test_preflight_matches_check_order_verdict(monkeypatch):
    """同数据下预检与真实判定的裁定必须逐字一致 —— 否则「预检说能过、下单被拒」。"""

    # Arrange
    def _ctx_factory():
        async def _ctx(req, *, db, redis, need_counts=False, need_daily_pnl=False):
            from backend.shared.risk import RiskContext

            return RiskContext(
                market="CN",
                symbol="600036.SH",
                side="BUY",
                quantity=100,
                now_ts=0.0,
                kill_switch=True,
            )

        return _ctx

    import backend.services.trade.services.risk_gate_service as mod

    monkeypatch.setattr(mod, "build_context", _ctx_factory())

    # Act：强制模式（会真拒），两条路各跑一次
    pre_redis = FakeRedis(config=_cfg(shadow="false"))
    pre = await rgs.preflight_order(_req(), db=None, redis=pre_redis)
    real_redis = FakeRedis(config=_cfg(shadow="false"))
    real = await rgs.check_order(_req(), db=None, redis=real_redis)

    # Assert
    assert pre.passed is False and real.passed is False
    assert pre.rule_id == real.rule_id == "l0.kill_switch"
    assert pre.reason == real.reason
    # 真实那条留痕、预检那条不留 —— 差值恰好是一次
    assert real_redis.hincr.get("halted") == 1
    assert pre_redis.hincr == {}


@pytest.mark.asyncio
async def test_preflight_fail_closed_without_trace():
    """配置不可读时预检同样 fail-closed，且不写 errors 计数。"""
    # Arrange
    redis = FakeRedis(fail_hgetall=True)

    # Act
    verdict = await rgs.preflight_order(_req(), db=None, redis=redis)

    # Assert
    assert verdict.passed is False and verdict.rule_id == "l0.config"
    assert redis.xadds == [] and redis.hincr == {}


@pytest.mark.asyncio
async def test_preflight_disabled_gate_passes_quietly():
    """风控未启用：预检放行（与 check_order 的 disabled 分支同判），且不留痕。"""
    # Arrange
    redis = FakeRedis(config={})

    # Act
    verdict = await rgs.preflight_order(_req(), db=None, redis=redis)

    # Assert
    assert verdict.passed is True and verdict.verdict == "disabled"
    assert redis.xadds == [] and redis.hincr == {}


# ── 高频交易阈值护栏（程序化交易报告义务）──────────────────────────────
#
# 撞线不违法，所以**只告警不拦截**：配置照常加载、照常生效。这一组测的就是
# 「加载不被改坏」+「该响的时候响」，两者缺一不可——只测告警会漏掉前者。


def test_order_rate_reaching_hft_warns_but_still_loads(caplog):
    """max_per_minute ≥18000 时告警，但配置原样生效（不改值、不拒绝加载）。"""
    # Arrange
    rules = json.dumps({"l3.order_frequency": {"max_per_minute": 18000}})
    redis = FakeRedis(config={"enabled": "true", "rules": rules, "version": "3"})

    # Act
    with caplog.at_level(logging.WARNING):
        cfg = rgs.load_config(redis)

    # Assert
    assert [r.getMessage() for r in caplog.records if "高频交易" in r.getMessage()]
    assert cfg is not None and cfg.enabled is True and cfg.version == 3
    assert cfg.rules["l3.order_frequency"]["max_per_minute"] == 18000


def test_order_rate_below_hft_is_quiet(caplog):
    """默认 60 笔/分（=1 笔/秒）不该刷告警——天天响的告警等于没有告警。"""
    # Arrange
    rules = json.dumps({"l3.order_frequency": {"max_per_minute": 60}})
    redis = FakeRedis(config={"enabled": "true", "rules": rules})

    # Act
    with caplog.at_level(logging.WARNING):
        rgs.load_config(redis)

    # Assert
    assert not [r for r in caplog.records if "高频交易" in r.getMessage()]


def test_order_rate_rule_absent_is_quiet(caplog):
    """规则没配 order_frequency 时不能报「你可能被认定为高频」。"""
    # Arrange
    redis = FakeRedis(
        config={"enabled": "true", "rules": json.dumps({"l1.available_cash": {}})}
    )

    # Act
    with caplog.at_level(logging.WARNING):
        rgs.load_config(redis)

    # Assert
    assert not [r for r in caplog.records if "高频交易" in r.getMessage()]


# ── 配置与代码版本错位探针（未注册规则）────────────────────────────────
#
# `RiskGateCore.evaluate` 按**注册表**遍历（`for spec in self._specs`），配置里多出来的
# 条目会被静默跳过：往 Redis 写了新规则的配置、而服务进程还跑着旧代码时，闸门看着
# "已启用"、实则一次都不执行，且留痕看不出差别（跳过不产生 decision，"没拦过"与
# "根本没跑过"长得一模一样）。下面这组就是这个静默失效形态的守卫。


@pytest.fixture
def _reset_unknown_rule_memo(monkeypatch):
    """模块级去重状态隔离（它是进程全局，会跨用例残留）。"""
    monkeypatch.setattr(rgs, "_unknown_rules_warned", frozenset())


@pytest.fixture
def _reset_missing_rule_memo(monkeypatch):
    """同上，反向探针的去重状态（两个 memo 必须各自隔离，否则互相顶掉告警）。"""
    monkeypatch.setattr(rgs, "_missing_rules_warned", frozenset())
    monkeypatch.setattr(rgs, "_unknown_rules_warned", frozenset())


@pytest.mark.unit
def test_unknown_rule_in_config_warns_as_error(caplog, _reset_unknown_rule_memo):
    # Arrange
    rules = json.dumps({"l9.not_implemented": {}, "l1.available_cash": {}})
    redis = FakeRedis(config={"enabled": "true", "rules": rules})

    # Act
    with caplog.at_level(logging.ERROR):
        cfg = rgs.load_config(redis)

    # Assert
    errs = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("未注册规则" in m and "l9.not_implemented" in m for m in errs)
    # 只告警、不拒加载：配置本身没错，错的是进程版本（拒绝加载 = 把风控整个关掉）
    assert cfg is not None and cfg.enabled is True
    assert "l9.not_implemented" in cfg.rules


@pytest.mark.unit
def test_unknown_rule_warning_is_deduped(caplog, _reset_unknown_rule_memo):
    """`load_config` 每单读一次；同一条错位配置不能按单刷屏——刷屏的日志等于没有日志。"""
    # Arrange
    redis = FakeRedis(
        config={"enabled": "true", "rules": json.dumps({"l9.not_implemented": {}})}
    )

    # Act
    with caplog.at_level(logging.ERROR):
        rgs.load_config(redis)
        rgs.load_config(redis)
        rgs.load_config(redis)

    # Assert
    assert len([r for r in caplog.records if "未注册规则" in r.getMessage()]) == 1


@pytest.mark.unit
def test_unknown_rule_warning_fires_again_after_config_fixed(
    caplog, _reset_unknown_rule_memo
):
    """去重按「当前集合」而非「响过没有」：修好再坏一次必须重新响。"""
    # Arrange
    bad = FakeRedis(config={"enabled": "true", "rules": json.dumps({"l9.x": {}})})
    good = FakeRedis(
        config={"enabled": "true", "rules": json.dumps({"l1.available_cash": {}})}
    )

    # Act
    with caplog.at_level(logging.ERROR):
        rgs.load_config(bad)
        rgs.load_config(good)
        rgs.load_config(bad)

    # Assert
    assert len([r for r in caplog.records if "未注册规则" in r.getMessage()]) == 2


@pytest.mark.unit
def test_missing_rule_in_config_warns(caplog, _reset_missing_rule_memo):
    """**反向错位**：代码已内置、配置里没有的规则 → 告警（`_warn_if_unknown_rules` 的镜像）。

    新增一条规则、代码上线、却没人往 ``qm:risk:config.rules`` 补条目时，新规则一次都
    不会执行（引擎只跑配置列出的，always_on 除外）；而"没拦过"与"根本没跑过"在留痕里
    依旧一模一样。与未知规则同为 WARNING/ERROR 级别的"版本错位"，必须能响。
    """
    # Arrange：只有两条规则的老配置（模拟"代码比配置新"）
    rules = json.dumps({"l0.clock_drift": {}, "l1.available_cash": {}})
    redis = FakeRedis(config={"enabled": "true", "rules": rules})

    # Act
    with caplog.at_level(logging.WARNING):
        cfg = rgs.load_config(redis)

    # Assert：缺的规则要逐条点名（含总杠杆闸）
    warns = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    hit = [m for m in warns if "缺少代码已内置的规则" in m]
    assert hit, f"没有告警：{warns}"
    assert "l1.leverage_cap" in hit[0] and "l3.lot_size" in hit[0]
    # 只告警、不拒加载、不改配置
    assert cfg is not None and cfg.enabled is True
    assert "l1.leverage_cap" not in cfg.rules


@pytest.mark.unit
def test_missing_rule_warning_is_deduped_and_refires(caplog, _reset_missing_rule_memo):
    """每单读一次配置 → 同一份错位只响一次；修好再坏必须重新响（与未知规则同款语义）。"""
    old = FakeRedis(
        config={"enabled": "true", "rules": json.dumps({"l1.available_cash": {}})}
    )
    fixed = FakeRedis(
        config={"enabled": "true", "rules": json.dumps(rgs.DEFAULT_RULES)}
    )

    with caplog.at_level(logging.WARNING):
        rgs.load_config(old)
        rgs.load_config(old)
        rgs.load_config(old)
        n_after_three = len(
            [r for r in caplog.records if "缺少代码已内置的规则" in r.getMessage()]
        )
        rgs.load_config(fixed)  # 补齐 → 静默
        n_after_fix = len(
            [r for r in caplog.records if "缺少代码已内置的规则" in r.getMessage()]
        )
        rgs.load_config(old)  # 又缺 → 重新响
        n_after_relapse = len(
            [r for r in caplog.records if "缺少代码已内置的规则" in r.getMessage()]
        )

    assert n_after_three == 1  # 三单只响一次（刷屏的日志等于没有日志）
    assert n_after_fix == 1  # 补齐后不再响
    assert n_after_relapse == 2  # 再缺一次重新响


@pytest.mark.unit
def test_missing_rule_warning_silent_on_empty_config(caplog, _reset_missing_rule_memo):
    """**未启用整层**是有意为之（不是错位）→ 不告警。否则关掉风控的部署会被刷屏。"""
    with caplog.at_level(logging.WARNING):
        rgs.load_config(FakeRedis(config={"enabled": "false", "rules": "{}"}))
    assert not [r for r in caplog.records if "缺少代码已内置的规则" in r.getMessage()]


@pytest.mark.unit
def test_shipped_default_rules_are_all_registered(caplog, _reset_unknown_rule_memo):
    """出厂 DEFAULT_RULES 必须逐条在注册表里：否则首次配置化时就埋了一颗
    「看着配了 13 条、实际只跑 12 条」的雷——正是本探针要防的形态。"""
    # Arrange
    rules = json.dumps(rgs.DEFAULT_RULES, ensure_ascii=False)
    redis = FakeRedis(config={"enabled": "true", "rules": rules})

    # Act
    with caplog.at_level(logging.ERROR):
        rgs.load_config(redis)

    # Assert
    assert not [r for r in caplog.records if "未注册规则" in r.getMessage()]


# ── 档位层接入（`qm:risk:tier` → 规则参数视图）─────────────────────────
#
# 档位层是"全账户买入侧参数的动态上限"，与配置取更严者合入。接入点必须满足：
# ① 未配置档位 = 不改任何行为（不是"坏了"）；② 只收紧；③ 档位失效（过期/读不到）
# 会**收紧**买入侧并告警，而不是静默按更松的参数跑；④ 判定留痕带档位坐标。


@pytest.fixture
def _reset_tier_memo(monkeypatch):
    monkeypatch.setattr(rgs, "_tier_warned", frozenset())
    monkeypatch.setattr(rgs, "_missing_rules_warned", frozenset())
    monkeypatch.setattr(rgs, "_unknown_rules_warned", frozenset())


def _tier_doc(level: str, date_str: str, **budget_over) -> dict:
    from backend.shared.risk import tiers as T

    budget = {k: T.LEVELS[level][k] for k in T.LIMIT_KEYS}
    budget.update(budget_over)
    return {
        "date": date_str,
        "level": level,
        "budget": json.dumps(budget, ensure_ascii=False),
    }


@pytest.mark.unit
def test_absent_tier_leaves_config_untouched(_reset_tier_memo):
    """**没配过档位 ≠ 档位坏了**：全新部署不该被静默套上防守档。"""
    # Arrange
    redis = FakeRedis(config=_cfg())

    # Act
    cfg = rgs.load_config(redis)

    # Assert
    assert cfg is not None
    assert cfg.rules["l1.position_cap"]["max_pct"] == 0.15  # DEFAULT_RULES 原值
    assert cfg.tier_level == "" and cfg.tier_source == "absent"
    assert cfg.tier_applied == {}


@pytest.mark.unit
def test_tier_tightens_rule_params(_reset_tier_memo):
    # Arrange：配置故意配得比档位松（1.5 / 25%），档位把它们压回防守档
    from datetime import datetime

    from backend.shared.risk.tiers import CST

    today = datetime.now(tz=CST).strftime("%Y-%m-%d")
    rules = {
        **rgs.DEFAULT_RULES,
        "l1.leverage_cap": {"max_leverage": 1.5},
        "l1.position_cap": {"max_pct": 0.25},
    }
    redis = FakeRedis(
        config=_cfg(rules=json.dumps(rules)), tier=_tier_doc("defensive", today)
    )

    # Act
    cfg = rgs.load_config(redis)

    # Assert
    assert cfg is not None
    assert cfg.rules["l1.leverage_cap"]["max_leverage"] == 1.0
    assert cfg.rules["l1.position_cap"]["max_pct"] == 0.15
    assert cfg.tier_level == "defensive" and cfg.tier_source == "doc"
    # 四个买入侧键全被档位接管（批次 B 起 per_stock_pct / max_new_buys 也有消费者）
    assert cfg.tier_applied == {
        "l1.leverage_cap": {"max_leverage": 1.0},
        "l1.position_cap": {"max_pct": 0.15},
        "l1.per_order_pct": {"max_pct": 0.10},
        "l1.new_buys_per_day": {"max_new_buys": 1},
    }


@pytest.mark.unit
def test_tier_applied_records_only_actual_changes(_reset_tier_memo):
    """档位值与配置相同时 `tier_applied` 为空——审计字段只说**真发生的事**，
    否则留痕会声称一次并不存在的收紧。"""
    # Arrange：档位文档的四个买入侧键**逐个取 DEFAULT_RULES 的值**——这正是用例
    # 要构造的局面（档位与配置一致）。不能再用防守档的默认数值：批次 B 起
    # DEFAULT_RULES 的 per_order_pct=0.15 / new_buys=3 比防守档（0.10 / 1）松，
    # 那样 applied 恒非空，本用例就测不到"零变更"这条分支了。
    from datetime import datetime

    from backend.shared.risk.tiers import CST

    today = datetime.now(tz=CST).strftime("%Y-%m-%d")
    redis = FakeRedis(
        config=_cfg(),
        tier=_tier_doc(
            "defensive",
            today,
            leverage_max=1.0,
            per_stock_pos_pct=0.15,
            per_stock_pct=0.15,
            max_new_buys=3,
        ),
    )

    # Act
    cfg = rgs.load_config(redis)

    # Assert
    assert cfg is not None and cfg.tier_level == "defensive"
    assert cfg.tier_applied == {}


@pytest.mark.unit
def test_tier_never_loosens_stricter_config(_reset_tier_memo):
    """配置比档位更严时配置赢——档位是上限，不是赋值（放宽须显式改配置）。"""
    # Arrange
    from datetime import datetime

    from backend.shared.risk.tiers import CST

    today = datetime.now(tz=CST).strftime("%Y-%m-%d")
    rules = {**rgs.DEFAULT_RULES, "l1.position_cap": {"max_pct": 0.05}}
    redis = FakeRedis(
        config=_cfg(rules=json.dumps(rules)), tier=_tier_doc("calm", today)
    )

    # Act
    cfg = rgs.load_config(redis)

    # Assert
    assert cfg is not None
    assert cfg.rules["l1.position_cap"]["max_pct"] == 0.05
    assert cfg.rules["l1.leverage_cap"]["max_leverage"] == 1.0  # 档位 1.5 不许放大


@pytest.mark.unit
def test_stale_tier_tightens_buy_side_and_warns(caplog, _reset_tier_memo):
    """定档任务挂掉（档位停在昨天）→ 买入侧回退防守 + 告警，**不静默按旧档跑**。"""
    # Arrange
    redis = FakeRedis(config=_cfg(), tier=_tier_doc("calm", "2026-09-01"))

    # Act
    with caplog.at_level(logging.WARNING):
        cfg = rgs.load_config(redis)

    # Assert
    assert cfg is not None
    assert cfg.rules["l1.position_cap"]["max_pct"] == 0.15  # FALLBACK（买入侧收紧）
    assert cfg.tier_source == "stale"
    assert any("档位不可信" in r.getMessage() for r in caplog.records)


@pytest.mark.unit
def test_tier_read_failure_does_not_break_config(caplog, _reset_tier_memo):
    """Redis 抖动不该让闸门停摆：档位读不到 → 只回退买入侧，配置照常返回。"""

    # Arrange
    class _TierBoom(FakeRedis):
        def hgetall(self, key):
            from backend.shared.risk.tiers import TIER_KEY

            if key == TIER_KEY:
                raise ConnectionError("redis down")
            return super().hgetall(key)

    redis = _TierBoom(config=_cfg())

    # Act
    with caplog.at_level(logging.WARNING):
        cfg = rgs.load_config(redis)

    # Assert
    assert cfg is not None and cfg.enabled is True
    assert cfg.tier_source == "fallback"
    assert cfg.rules["l1.position_cap"]["max_pct"] == 0.15
    assert any("档位不可信" in r.getMessage() for r in caplog.records)


@pytest.mark.unit
def test_tier_problem_warning_is_deduped(caplog, _reset_tier_memo):
    """`load_config` 每单读一次 → 同一条档位异常不能按单刷屏。"""
    # Arrange
    redis = FakeRedis(config=_cfg(), tier=_tier_doc("calm", "2026-09-01"))

    # Act
    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            rgs.load_config(redis)

    # Assert
    assert len([r for r in caplog.records if "档位不可信" in r.getMessage()]) == 1


@pytest.mark.unit
def test_tier_absent_is_silent(caplog, _reset_tier_memo):
    """未配置档位不是异常 → 不告警（否则每个未启用档位的部署都被刷屏）。"""
    with caplog.at_level(logging.WARNING):
        rgs.load_config(FakeRedis(config=_cfg()))
    assert not [r for r in caplog.records if "RiskTier" in r.getMessage()]


@pytest.mark.unit
def test_missing_rule_warning_exempts_tier_enabled_rules(caplog, _reset_tier_memo):
    """档位启用的规则不算"配置漏配"（那是有意的权威行为）——否则告警永远消不掉。"""
    # Arrange：配置里缺两条——l1.position_cap（档位会带上来）与 l3.cancel_ratio（档位不管）
    from datetime import datetime

    from backend.shared.risk.tiers import CST

    today = datetime.now(tz=CST).strftime("%Y-%m-%d")
    rules = {
        k: v
        for k, v in rgs.DEFAULT_RULES.items()
        if k not in ("l1.position_cap", "l3.cancel_ratio")
    }
    redis = FakeRedis(
        config=_cfg(rules=json.dumps(rules)), tier=_tier_doc("defensive", today)
    )

    # Act
    with caplog.at_level(logging.WARNING):
        cfg = rgs.load_config(redis)

    # Assert：真漏配的照报，档位带上来的不报
    assert cfg is not None
    assert cfg.rules["l1.position_cap"]["max_pct"] == 0.15  # 档位带上来了
    msgs = [
        r.getMessage()
        for r in caplog.records
        if "缺少代码已内置的规则" in r.getMessage()
    ]
    assert msgs and any("l3.cancel_ratio" in m for m in msgs)
    assert all("l1.position_cap" not in m for m in msgs)


@pytest.mark.asyncio
async def test_decision_record_carries_tier(monkeypatch, _reset_tier_memo):
    """留痕带档位坐标：事后能回答"这一单是在哪个档位下判的、档位改了什么"。"""
    # Arrange
    from datetime import datetime

    from backend.shared.risk.tiers import CST

    today = datetime.now(tz=CST).strftime("%Y-%m-%d")
    rules = {
        **rgs.DEFAULT_RULES,
        "l1.leverage_cap": {"max_leverage": 1.5},
        "l1.position_cap": {"max_pct": 0.25},
    }
    redis = FakeRedis(
        config=_cfg(rules=json.dumps(rules)), tier=_tier_doc("defensive", today)
    )

    async def _ctx(req, *, db, redis, need_counts=False, need_daily_pnl=False):
        from backend.shared.risk import RiskContext

        return RiskContext(market="CN", symbol="600036.SH", side="BUY", quantity=100)

    monkeypatch.setattr(rgs, "build_context", _ctx)

    # Act
    await rgs.check_order(_req(), db=None, redis=redis)

    # Assert：留痕带档位坐标 + 指标按档位分桶（影子报告要能按档位拆）
    fields = redis.xadds[-1][1]
    assert fields.get("tier") == "defensive/doc"
    assert json.loads(fields.get("tier_applied") or "{}") == {
        "l1.leverage_cap": {"max_leverage": 1.0},
        "l1.position_cap": {"max_pct": 0.15},
        "l1.per_order_pct": {"max_pct": 0.10},
        "l1.new_buys_per_day": {"max_new_buys": 1},
    }
    assert redis.hincr.get("tier:defensive") == 1


@pytest.mark.asyncio
async def test_decision_record_marks_untrusted_tier(monkeypatch, _reset_tier_memo):
    """档位坏了必须留痕且分桶——判据是 `source` 不是 `level`。

    结构坏掉的档位没有档位名（level=""）；若按 level 判，这一单会写成和"没配过
    档位"一模一样，事后无法回答"那天有多少单是在档位不可信的情况下判的"。
    """
    # Arrange：结构坏（budget 不是 JSON）→ fallback，level 为空
    redis = FakeRedis(
        config=_cfg(), tier={"date": "2026-09-23", "budget": "{不是 JSON"}
    )

    async def _ctx(req, *, db, redis, need_counts=False, need_daily_pnl=False):
        from backend.shared.risk import RiskContext

        return RiskContext(market="CN", symbol="600036.SH", side="BUY", quantity=100)

    monkeypatch.setattr(rgs, "build_context", _ctx)

    # Act
    await rgs.check_order(_req(), db=None, redis=redis)

    # Assert：一律用 .get——字段缺失**就是**这里要断言的失败，写成 [] 会把失败
    # 变成 KeyError，探针无法区分"守卫生效"与"测试自己炸了"
    fields = redis.xadds[-1][1]
    assert fields.get("tier") == "-/fallback"
    assert redis.hincr.get("tier:unknown") == 1
    assert redis.hincr.get("tier_source:fallback") == 1


@pytest.mark.asyncio
async def test_decision_record_omits_tier_when_absent(monkeypatch, _reset_tier_memo):
    """档位未配置 → 留痕**不写** tier 字段（absent 与"配了没生效"必须可区分）。"""
    # Arrange
    redis = FakeRedis(config=_cfg())

    async def _ctx(req, *, db, redis, need_counts=False, need_daily_pnl=False):
        from backend.shared.risk import RiskContext

        return RiskContext(market="CN", symbol="600036.SH", side="BUY", quantity=100)

    monkeypatch.setattr(rgs, "build_context", _ctx)

    # Act
    await rgs.check_order(_req(), db=None, redis=redis)

    # Assert
    assert "tier" not in redis.xadds[-1][1]


# ── 当日盈亏（l1.daily_loss_limit 的 ctx.daily_pnl_pct 生产者）─────────
#
# 这条规则的判定逻辑早已写好并有单测（test_risk_engine_core），但**生产者缺席**：
# build_context 从不填 daily_pnl_pct → 生产环境该规则恒不触发（`is None` 直接放行）。
# 下面这组测的是"喂进去的数字"，不是规则本身。


@pytest.mark.asyncio
async def test_build_context_real_daily_pnl_from_ledger_baseline(monkeypatch):
    """真账户：分子=本行快照权益，分母=日度台账 day_open_equity（与账户页同源）。"""
    _patch_ctx_deps(monkeypatch)
    calls: list = []
    ctx = await rgs.build_context(
        _req(trading_mode="REAL", user_id=10000001),
        db=_fake_db_counts(
            snapshot=_snap_row(
                cash="20000",
                total_asset="96000",
                snapshot_date=_today_cst(),
                account_id="tdx-default-10000001",
            ),
            ledger=_ledger_row(day_open_equity=100000.0),
            calls=calls,
        ),
        redis=FakeRedis(),
        need_daily_pnl=True,
    )

    # Assert：-4%（96,000 vs 日初 100,000）
    assert ctx.daily_pnl_pct == pytest.approx(-4.0)
    sql, params = next(c for c in calls if "real_account_ledger" in c[0])
    # 作用域三件套缺一不可：少 tenant/user 别名空间会读到别人的台账；
    # 少 snapshot_date 会读到**别的交易日**的日初基线（把昨天的亏损算进今天）。
    assert "tenant_id = :t" in sql and "account_id = :a" in sql
    assert "snapshot_date = :d" in sql
    # 账户键必须与那一行快照自身读出来的 account_id 一致——(tenant,user) 下
    # tdx/qmt 是两座互不相交的真账户，按 user 找台账等于在两座之间掷硬币。
    assert params["a"] == "tdx-default-10000001"
    assert params["d"] == _today_cst()


@pytest.mark.asyncio
async def test_build_context_real_daily_pnl_skips_stale_snapshot(monkeypatch):
    """最新快照不是**今天**的 → 不算当日盈亏，**也不发**台账查询。

    桥断三天时最新快照仍是三天前那行；照算会得出"三天前的当日盈亏"——拿它判今天，
    是在凭历史的亏损拒今天的买单（或凭历史的盈利放行今天的亏损）。
    """
    from datetime import timedelta

    _patch_ctx_deps(monkeypatch)
    calls: list = []
    ctx = await rgs.build_context(
        _req(trading_mode="REAL"),
        db=_fake_db_counts(
            snapshot=_snap_row(
                total_asset="96000", snapshot_date=_today_cst() - timedelta(days=1)
            ),
            ledger=_ledger_row(day_open_equity=100000.0),
            calls=calls,
        ),
        redis=FakeRedis(),
        need_daily_pnl=True,
    )

    assert ctx.daily_pnl_pct is None
    assert not any("real_account_ledger" in c[0] for c in calls)
    # 其余字段照常（陈旧只影响这一项，别的规则有自己的时效判据）
    assert ctx.total_assets == pytest.approx(96000.0)


@pytest.mark.asyncio
async def test_build_context_real_daily_pnl_missing_ledger_row_is_none(monkeypatch):
    """台账无当日行 → None（规则放行），**不是 0.0**（"当日打平"的假事实）。"""
    _patch_ctx_deps(monkeypatch)
    ctx = await rgs.build_context(
        _req(trading_mode="REAL"),
        db=_fake_db_counts(
            snapshot=_snap_row(total_asset="96000", snapshot_date=_today_cst()),
            ledger=None,
        ),
        redis=FakeRedis(),
        need_daily_pnl=True,
    )

    assert ctx.daily_pnl_pct is None


@pytest.mark.asyncio
async def test_build_context_real_daily_pnl_zero_baseline_is_none(monkeypatch):
    """台账行在但日初权益为 0（老行/坏行）→ None，不拿 0 当分母。"""
    _patch_ctx_deps(monkeypatch)
    ctx = await rgs.build_context(
        _req(trading_mode="REAL"),
        db=_fake_db_counts(
            snapshot=_snap_row(total_asset="96000", snapshot_date=_today_cst()),
            ledger=_ledger_row(day_open_equity=0.0),
        ),
        redis=FakeRedis(),
        need_daily_pnl=True,
    )

    assert ctx.daily_pnl_pct is None


@pytest.mark.asyncio
async def test_build_context_real_daily_pnl_query_failure_keeps_rest(monkeypatch):
    """台账查询抛错 → 该项 None，**不拖垮**整份上下文（其余规则照判）。"""
    _patch_ctx_deps(monkeypatch)
    ctx = await rgs.build_context(
        _req(trading_mode="REAL"),
        db=_fake_db_counts(
            snapshot=_snap_row(total_asset="96000", snapshot_date=_today_cst()),
            ledger=RuntimeError("pg down"),
        ),
        redis=FakeRedis(),
        need_daily_pnl=True,
    )

    assert ctx.daily_pnl_pct is None
    assert ctx.total_assets == pytest.approx(96000.0)
    assert ctx.available_cash == pytest.approx(100000.0)


@pytest.mark.asyncio
async def test_build_context_real_daily_pnl_skipped_without_flag(monkeypatch):
    """规则未启用时**不发**这条查询（每单少一次库往返）。"""
    _patch_ctx_deps(monkeypatch)
    calls: list = []
    ctx = await rgs.build_context(
        _req(trading_mode="REAL"),
        db=_fake_db_counts(
            snapshot=_snap_row(total_asset="96000", snapshot_date=_today_cst()),
            ledger=_ledger_row(day_open_equity=100000.0),
            calls=calls,
        ),
        redis=FakeRedis(),
        need_daily_pnl=False,
    )

    assert ctx.daily_pnl_pct is None
    assert not any("real_account_ledger" in c[0] for c in calls)


@pytest.mark.asyncio
async def test_real_daily_pnl_reaches_engine_reject(monkeypatch):
    """生产者→消费者接线：-4% 的上下文喂进真引擎必须**真拒单**。

    这是本组测试存在的理由——判定逻辑早就对了，缺的只是没人喂数字。
    """
    _patch_ctx_deps(monkeypatch)
    ctx = await rgs.build_context(
        _req(trading_mode="REAL"),
        db=_fake_db_counts(
            snapshot=_snap_row(total_asset="96000", snapshot_date=_today_cst()),
            ledger=_ledger_row(day_open_equity=100000.0),
        ),
        redis=FakeRedis(),
        need_daily_pnl=True,
    )

    verdict = rgs._CORE.evaluate(
        ctx, {"l1.daily_loss_limit": {"max_loss_pct": 3.0}}, version=1
    )
    # 取 id 而非 decisions[0]：`l0.session` 等 always_on 规则照常参战（本用例的
    # 判定时刻就是墙上时钟），按下标取会在别的时段红/绿翻转。
    from backend.shared.risk.contracts import ACTION_REJECT

    hits = [
        d
        for d in verdict.decisions
        if d.rule_id == "l1.daily_loss_limit" and d.action == ACTION_REJECT
    ]
    assert hits, "l1.daily_loss_limit 没参与判定/没拒单——生产者仍是断的"
    assert not verdict.passed
    # 证据里带的是**算出来的**百分数（不是 None / 0）
    assert hits[0].evidence["daily_pnl_pct"] == pytest.approx(-4.0)


class _MgrWithSettings:
    """模拟账户假体：账户 + 设置（初始资金）。"""

    account: dict | None = None
    settings: dict | None = None

    def __init__(self, redis):
        pass

    async def get_account(self, uid, tenant, market="CN"):
        return type(self).account

    async def get_settings(self, user_id, tenant_id="default", **kw):
        return {"initial_cash": 1_000_000.0, **(type(self).settings or {})}


def _patch_sim_fund_baselines(monkeypatch, baselines, seen: dict | None = None):
    """把基金快照服务的日初基线换成给定值（``baselines`` 传异常实例=抛错）。"""
    from backend.services.simulation.services import fund_snapshot_service as fss

    async def _fake(
        cls, *, tenant_id, user_id, initial_capital, as_of=None, market="ALL"
    ):
        if seen is not None:
            seen.update(
                tenant_id=tenant_id,
                user_id=user_id,
                initial_capital=initial_capital,
                market=market,
            )
        if isinstance(baselines, Exception):
            raise baselines
        return baselines

    monkeypatch.setattr(
        fss.SimulationFundSnapshotService, "get_baselines", classmethod(_fake)
    )


@pytest.mark.asyncio
async def test_build_context_sim_daily_pnl_uses_fund_baselines(monkeypatch):
    """模拟盘：日初基线走模拟基金快照服务（与账户页 today_pnl 同一口径）。"""
    _patch_ctx_deps(monkeypatch)
    from decimal import Decimal

    from backend.services.trade_shared import simulation_manager as sm

    _MgrWithSettings.account = {
        "cash": 20000.0,
        "total_asset": 1_020_000.0,
        "positions": {},
    }
    _MgrWithSettings.settings = {"initial_cash": 1_000_000.0}
    monkeypatch.setattr(sm, "SimulationAccountManager", _MgrWithSettings)
    seen: dict = {}
    _patch_sim_fund_baselines(
        monkeypatch,
        {
            "day_open_equity": Decimal("1000000"),
            "month_open_equity": Decimal("1000000"),
        },
        seen=seen,
    )

    ctx = await rgs.build_context(
        _req(),  # 无 trading_mode → sim 分支
        db=_fake_db_counts(),
        redis=FakeRedis(),
        need_daily_pnl=True,
    )

    assert ctx.daily_pnl_pct == pytest.approx(2.0)  # 1,020,000 vs 日初 1,000,000
    # 基线必须按**本市场**取（settings 只有一份、无市场维度）；初始资金按 settings 种子
    assert seen["market"] == "CN"
    assert float(seen["initial_capital"]) == pytest.approx(1_000_000.0)
    assert seen["user_id"] == "1"


@pytest.mark.asyncio
async def test_build_context_sim_daily_pnl_failure_is_none(monkeypatch):
    """基线服务抛错 → None（规则放行），其余账户字段照旧可用。"""
    _patch_ctx_deps(monkeypatch)
    from backend.services.trade_shared import simulation_manager as sm

    _MgrWithSettings.account = {
        "cash": 20000.0,
        "total_asset": 1_020_000.0,
        "positions": {},
    }
    _MgrWithSettings.settings = {"initial_cash": 1_000_000.0}
    monkeypatch.setattr(sm, "SimulationAccountManager", _MgrWithSettings)
    _patch_sim_fund_baselines(monkeypatch, RuntimeError("pg down"))

    ctx = await rgs.build_context(
        _req(),
        db=_fake_db_counts(),
        redis=FakeRedis(),
        need_daily_pnl=True,
    )

    assert ctx.daily_pnl_pct is None
    assert ctx.total_assets == pytest.approx(1_020_000.0)
