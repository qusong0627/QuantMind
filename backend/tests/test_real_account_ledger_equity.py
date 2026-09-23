"""真账户日度台账「权益一致性归一」测试（2026-09-23 修复配套）。

背景（实况）：tdx 桥 2026-09-03/04 把 ``total_asset`` 字段读错——北京 16:34 起
一步掉 161,058（−17.5%）而**同行** cash/mv 一分钟没动，次日恢复；该值进了日度台账，
污染账户图与风控档位回撤输入。写侧规则：现金账户恒等式「总资产 ≥ 现金+市值」，
反方向（冻结/在途把整体抬高）合法、不动。

覆盖：纯函数边界（阈值两侧 / 反方向 / 单分量）+ 写侧接线（upsert 真落归一值并留痕）
+ 存量修复脚本 plan_repairs（含幂等回灌）。
"""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

import pytest

from backend.services.trade.services.real_account_ledger_service import (
    EQUITY_UNDERREPORT_TOL_MIN,
    derive_equity_returns,
    normalize_equity,
    resolve_daily_pnl_pct,
    upsert_real_account_daily_ledger,
)
from backend.scripts.repair_ledger_equity import plan_repairs

# ── 纯函数：normalize_equity ──────────────────────────────────────────


def test_underreport_rebuilds_equity_and_keeps_raw_evidence():
    """实况数字：total=759,400 < cash+mv=920,458 ⇒ 重建并留痕原值。"""
    # Arrange
    total, cash, mv = 759_400.0, 714_812.0, 205_646.0

    # Act
    equity, evidence = normalize_equity(total, cash, mv)

    # Assert
    assert equity == 920_458.0
    assert evidence is not None
    assert evidence["rule"] == "equity_underreport"
    assert evidence["raw_total_asset"] == total
    assert evidence["gap"] == 161_058.0


def test_consistent_row_keeps_reported_total():
    # Arrange：正常行 total 就是 cash+mv（宽容差内）
    # Act
    equity, evidence = normalize_equity(920_458.0, 714_812.0, 205_646.0)

    # Assert
    assert equity == 920_458.0
    assert evidence is None


def test_small_gap_within_relative_tolerance_kept():
    """差额 0.9%（< 1%）视为报价/舍入噪声，不动。"""
    # Arrange：重建 100,000，上报 99,100 ⇒ gap 900 < max(100, 1,000)
    # Act
    equity, evidence = normalize_equity(99_100.0, 60_000.0, 40_000.0)

    # Assert
    assert equity == 99_100.0
    assert evidence is None


def test_small_gap_within_absolute_floor_kept():
    """小账户：gap 的 1% 只有几元时由绝对下限 100 元兜底。"""
    # Arrange：重建 5,000，gap 50 < max(100, 50)
    # Act
    equity, evidence = normalize_equity(4_950.0, 3_000.0, 2_000.0)

    # Assert
    assert equity == 4_950.0
    assert evidence is None
    assert EQUITY_UNDERREPORT_TOL_MIN == 100.0


def test_gap_just_above_tolerance_is_normalized():
    """差额 1.1%（> 1%）触发归一。"""
    # Arrange：重建 100,000，上报 98,900 ⇒ gap 1,100 > max(100, 1,000)
    # Act
    equity, evidence = normalize_equity(98_900.0, 60_000.0, 40_000.0)

    # Assert
    assert equity == 100_000.0
    assert evidence is not None


def test_over_report_kept_because_frozen_funds_are_legal():
    """反方向不动：total > cash+mv 可能是冻结/在途资金，属合法状态。"""
    # Arrange：冻结 100,000，total 1,000,000 > cash+mv 900,000
    # Act
    equity, evidence = normalize_equity(1_000_000.0, 500_000.0, 400_000.0)

    # Assert
    assert equity == 1_000_000.0
    assert evidence is None


@pytest.mark.parametrize(
    ("total", "cash", "mv"),
    [
        (500_000.0, 0.0, 480_000.0),  # 现金缺失（单分量，差额方向为负）
        (500_000.0, 500_000.0, 0.0),  # 市值缺失（单分量，恰相等）
        (500_000.0, 0.0, 0.0),  # 双分量缺失
        # 判别性输入：单分量高于上报总资产。缺分量时无法区分「真的是 0」与
        # 「字段没读到」（如桥未报现金 + QuantDB 价格回填的市值），重建会凭空
        # 抬高权益——守卫必须拦住，保留上报值。
        (700_000.0, 0.0, 800_000.0),
        (700_000.0, 800_000.0, 0.0),
    ],
)
def test_single_or_missing_component_keeps_reported_total(total, cash, mv):
    """两分量必须都 > 0 才核验；单分量无法判读，一律保留上报值。"""
    # Act
    equity, evidence = normalize_equity(total, cash, mv)

    # Assert
    assert equity == total
    assert evidence is None


def test_none_inputs_do_not_crash():
    # Act
    equity, evidence = normalize_equity(None, None, None)

    # Assert
    assert equity == 0.0
    assert evidence is None


# ── 纯函数：derive_equity_returns ─────────────────────────────────────


def test_derive_returns_matches_writer_formulas():
    # Arrange：日开 100 → 收 120；月开 110；初始 100
    # Act
    derived = derive_equity_returns(
        total_asset=120.0,
        day_open_equity=100.0,
        month_open_equity=110.0,
        initial_equity=100.0,
        today_pnl=-999.0,
        total_pnl=-999.0,
    )

    # Assert
    assert derived["daily_return_pct"] == pytest.approx(20.0)
    assert derived["total_return_pct"] == pytest.approx(20.0)
    assert derived["monthly_pnl_raw"] == pytest.approx(10.0)


def test_derive_returns_zero_denominators_fall_back_to_raw_pnl():
    # Act
    derived = derive_equity_returns(
        total_asset=100.0,
        day_open_equity=0.0,
        month_open_equity=0.0,
        initial_equity=0.0,
        today_pnl=5.0,
        total_pnl=7.0,
    )

    # Assert
    assert derived["daily_return_pct"] == 0.0
    assert derived["total_return_pct"] == 0.0
    assert derived["monthly_pnl_raw"] == 7.0


# ── 纯函数：resolve_daily_pnl_pct（风控 l1.daily_loss_limit 的输入口径）──


def test_resolve_daily_pnl_pct_signs_and_zero():
    """负=亏、0=当日打平——符号即语义，风控按 `<= -限额` 判。"""
    # Arrange / Act / Assert
    assert resolve_daily_pnl_pct(total_asset=96.0, day_open_equity=100.0) == (
        pytest.approx(-4.0)
    )
    assert resolve_daily_pnl_pct(total_asset=100.0, day_open_equity=100.0) == (
        pytest.approx(0.0)
    )
    assert resolve_daily_pnl_pct(total_asset=103.0, day_open_equity=100.0) == (
        pytest.approx(3.0)
    )


def test_resolve_daily_pnl_pct_unknown_baseline_is_none_not_zero():
    """基线不可得 → **None**，绝不是 0.0。

    0.0 的语义是"当日打平"（规则据此放行是**对的**），拿它顶替"算不出来"会让
    风控证据里留下一个假事实——与展示面「缺失一律 —」是同一条原则。风控分支靠
    None 走"没有依据就不判"，靠 0.0 就会声称"今天没亏"。
    """
    assert resolve_daily_pnl_pct(total_asset=100.0, day_open_equity=0.0) is None
    assert resolve_daily_pnl_pct(total_asset=100.0, day_open_equity=None) is None
    assert resolve_daily_pnl_pct(total_asset=None, day_open_equity=100.0) is None
    assert resolve_daily_pnl_pct(total_asset="oops", day_open_equity=100.0) is None


def test_resolve_daily_pnl_pct_is_the_formula_derive_returns_uses():
    """与台账派生列**同一实现**：账户页显示的当日收益率与风控判定必须逐位同源。

    两边各写一份的话，"页面看着亏 4%、风控认为亏 3.9%"这类分歧没有任何一处会报错。
    """
    derived = derive_equity_returns(
        total_asset=96.0,
        day_open_equity=100.0,
        month_open_equity=100.0,
        initial_equity=100.0,
        today_pnl=-999.0,  # 干扰项：日开可得时一律以 (总资产-日开)/日开 为准
        total_pnl=-999.0,
    )
    assert derived["daily_return_pct"] == resolve_daily_pnl_pct(
        total_asset=96.0, day_open_equity=100.0
    )


# ── 写侧接线：upsert 真落归一值 ──────────────────────────────────────


class _FakeResult:
    def __init__(self, row=None):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _FakeSession:
    """只实现 upsert 需要的 execute：记录语句；select 返回既有行（默认 None）。"""

    def __init__(self, existing=None):
        self.existing = existing
        self.statements: list = []

    async def execute(self, stmt, *args, **kwargs):
        self.statements.append(stmt)
        return _FakeResult(self.existing)


def _insert_params(session: _FakeSession) -> dict:
    from sqlalchemy.dialects import postgresql

    return session.statements[-1].compile(dialect=postgresql.dialect()).params


async def _upsert(db, **overrides):
    base = {
        "tenant_id": "default",
        "user_id": "00000001",
        "account_id": "tdx-default-00000001",
        "snapshot_at": datetime(2026, 9, 3, 15, 59, 0),
        "snapshot_date": date(2026, 9, 3),
        "total_asset": 920_458.0,
        "cash": 714_812.0,
        "market_value": 205_646.0,
        "initial_equity": 900_000.0,
        "day_open_equity": 900_000.0,
        "month_open_equity": 910_000.0,
        "today_pnl": 1_234.0,
        "total_pnl": 5_678.0,
        "floating_pnl": 0.0,
        "position_count": 3,
        "source": "tdx_bridge",
        "payload_json": {"positions": []},
    }
    base.update(overrides)
    await upsert_real_account_daily_ledger(db, **base)


@pytest.mark.asyncio
async def test_upsert_normal_row_passes_total_through_untouched():
    # Arrange
    db = _FakeSession()

    # Act
    await _upsert(db)

    # Assert
    params = _insert_params(db)
    assert params["total_asset"] == 920_458.0
    assert "equity_normalized" not in (params["payload_json"] or {})


@pytest.mark.asyncio
async def test_upsert_phantom_row_stores_rebuilt_equity_with_evidence():
    """被读错的 total 不得进台账：落重建值 + payload 留痕原值 + 派生列按重建值重算。"""
    # Arrange：上报 759,400（错），cash+mv = 920,458
    db = _FakeSession()

    # Act
    await _upsert(db, total_asset=759_400.0)

    # Assert
    params = _insert_params(db)
    assert params["total_asset"] == 920_458.0
    mark = params["payload_json"]["equity_normalized"]
    assert mark["raw_total_asset"] == 759_400.0
    assert mark["gap"] == 161_058.0
    assert mark["normalized_at"].startswith("2026-09-03")
    # 日收益按重建值：(920458-900000)/900000*100
    assert params["daily_return_pct"] == pytest.approx(
        (920_458.0 - 900_000.0) / 900_000.0 * 100
    )


@pytest.mark.asyncio
async def test_upsert_stale_row_skips_before_normalization():
    """过期写入（既有行更新）在归一路径之前就返回——不插行、不改值。"""
    # Arrange
    db = _FakeSession(
        existing=SimpleNamespace(last_snapshot_at=datetime(2026, 9, 3, 23, 59, 0))
    )

    # Act
    await _upsert(db, snapshot_at=datetime(2026, 9, 3, 23, 0, 0))

    # Assert：只发生了一次 select，没有 insert
    assert len(db.statements) == 1


# ── 存量修复：plan_repairs ───────────────────────────────────────────
# 实况数字（2026-09-03/04/07，tdx 账户）：坏值 759,399.59 与 856,948.02 沿 day_open
# 逐字拷贝传播；重建值 920,457.59 / 918,040.02。


def _ledger_row(**overrides):
    row = {
        "id": 1,
        "tenant_id": "default",
        "user_id": "10000001",
        "account_id": "tdx-default-00000001",
        "snapshot_date": date(2026, 9, 3),
        "total_asset": 759_399.59,
        "cash": 714_812.00,
        "market_value": 205_645.59,
        "day_open_equity": 922_922.24,
        "month_open_equity": 940_305.93,
        "initial_equity": 997_989.14,
        "today_pnl_raw": -163_522.65,
        "total_pnl_raw": -238_589.55,
        "daily_return_pct": -17.72,
        "source": "tdx_bridge",
        "payload_json": {"positions": []},
    }
    row.update(overrides)
    return row


def test_plan_repairs_targets_only_inconsistent_rows():
    # Arrange：一行被读错 + 一行正常
    rows = [
        _ledger_row(),
        _ledger_row(
            id=2,
            snapshot_date=date(2026, 9, 8),
            total_asset=921_081.64,
            cash=862_621.63,
            market_value=58_460.01,
            day_open_equity=919_715.02,
            daily_return_pct=0.15,
        ),
    ]

    # Act
    plans = plan_repairs(rows)

    # Assert
    assert len(plans) == 1
    plan = plans[0]
    assert plan["id"] == 1
    assert plan["total_new"] == 920_457.59
    assert ("total_asset", 759_399.59) in plan["guard"]
    assert plan["updates"]["total_asset"] == 920_457.59
    assert plan["daily_return_new"] == pytest.approx(
        (920_457.59 - 922_922.24) / 922_922.24 * 100
    )
    assert plan["payload_json"]["equity_normalized"]["raw_total_asset"] == 759_399.59
    assert (
        plan["payload_json"]["equity_normalized"]["repaired_by"]
        == "repair_ledger_equity.py"
    )


def test_plan_repairs_follows_day_open_chain_from_bad_total():
    """09-04 的 day_open=09-03 坏值、09-07 的 day_open=09-04 坏值 ⇒ 一并重锚并重算。"""
    # Arrange
    rows = [
        _ledger_row(),
        _ledger_row(
            id=2,
            snapshot_date=date(2026, 9, 4),
            total_asset=856_948.02,
            cash=839_992.01,
            market_value=78_048.01,
            day_open_equity=759_399.59,  # ← 09-03 的坏值
            today_pnl_raw=97_548.43,
            daily_return_pct=12.85,
        ),
        _ledger_row(
            id=3,
            snapshot_date=date(2026, 9, 7),
            total_asset=919_715.02,
            cash=839_992.01,
            market_value=79_723.01,  # total == cash+mv（自身无病）
            day_open_equity=856_948.02,  # ← 09-04 的坏值
            today_pnl_raw=62_767.00,
            daily_return_pct=7.32,
        ),
    ]

    # Act
    plans = plan_repairs(rows)
    by_id = {p["id"]: p for p in plans}

    # Assert
    assert set(by_id) == {1, 2, 3}
    # 09-04：total 与 day_open 双双换锚，派生盈亏同口径重算
    assert by_id[2]["updates"]["total_asset"] == 918_040.02
    assert by_id[2]["updates"]["day_open_equity"] == 920_457.59
    assert by_id[2]["updates"]["today_pnl_raw"] == pytest.approx(
        918_040.02 - 920_457.59
    )
    # 09-07：total 无病（不得动），只重锚 day_open
    assert "total_asset" not in by_id[3]["updates"]
    assert by_id[3]["updates"]["day_open_equity"] == 918_040.02
    assert by_id[3]["updates"]["today_pnl_raw"] == pytest.approx(
        919_715.02 - 918_040.02
    )
    assert by_id[3]["daily_return_new"] == pytest.approx(
        (919_715.02 - 918_040.02) / 918_040.02 * 100
    )
    assert by_id[3]["payload_json"]["equity_anchor_repaired"]["raw"] == 856_948.02


def test_plan_repairs_day_open_chain_is_account_scoped():
    """别账户出现同数字的 day_open 不误命中（坏值表按 account_id 隔离）。"""
    # Arrange：tdx 账户造出坏值 856,948.02；qmt 账户 day_open 恰好同数字、自身一致
    rows = [
        _ledger_row(),
        _ledger_row(
            id=2,
            snapshot_date=date(2026, 9, 4),
            total_asset=856_948.02,
            cash=839_992.01,
            market_value=78_048.01,
            day_open_equity=759_399.59,
        ),
        _ledger_row(
            id=9,
            account_id="qmt-default-00000001",
            snapshot_date=date(2026, 9, 7),
            total_asset=856_948.02,
            cash=800_000.00,
            market_value=56_948.02,  # 自身一致
            day_open_equity=856_948.02,
        ),
    ]

    # Act
    plans = plan_repairs(rows)

    # Assert：坏值 856,948.02 只在 tdx 账户内命中（id=2 是它自己的 total），qmt 行不动
    assert [p["id"] for p in plans] == [1, 2]


def test_plan_repairs_day_open_chain_is_user_scoped():
    """同 account_id 存在于多个 user_id 空间（实测库中存在）：跨空间不得互相重锚。"""
    # Arrange：user 10000001 造出坏值 856,948.02；user 00000001 同 account_id 的
    # 行 day_open 恰好同数字且自身一致（不得被别空间的坏值表命中）
    rows = [
        _ledger_row(),
        _ledger_row(
            id=2,
            snapshot_date=date(2026, 9, 4),
            total_asset=856_948.02,
            cash=839_992.01,
            market_value=78_048.01,
            day_open_equity=759_399.59,
        ),
        _ledger_row(
            id=9,
            user_id="00000001",
            snapshot_date=date(2026, 9, 7),
            total_asset=856_948.02,
            cash=800_000.00,
            market_value=56_948.02,  # 自身一致
            day_open_equity=856_948.02,
        ),
    ]

    # Act
    plans = plan_repairs(rows)

    # Assert：id=9 不属于坏值表所在空间，不重锚
    assert [p["id"] for p in plans] == [1, 2]


def test_build_update_statement_carries_compare_and_set_guard():
    """UPDATE 必须带 compare-and-set 守卫（只改扫描时所见原值仍成立的行）。"""
    from backend.scripts.repair_ledger_equity import build_update_statement
    from backend.services.trade_shared.models.real_account_ledger import (
        RealAccountLedgerDailySnapshot as Ledger,
    )

    # Arrange：id=1 直接命中；id=2 染坏值 856,948.02；id=3 只重锚 day_open
    rows = [
        _ledger_row(),
        _ledger_row(
            id=2,
            snapshot_date=date(2026, 9, 4),
            total_asset=856_948.02,
            cash=839_992.01,
            market_value=78_048.01,
            day_open_equity=759_399.59,
        ),
        _ledger_row(
            id=3,
            snapshot_date=date(2026, 9, 7),
            total_asset=919_715.02,
            cash=839_992.01,
            market_value=79_723.01,
            day_open_equity=856_948.02,
        ),
    ]
    plans = {p["id"]: p for p in plan_repairs(rows)}

    # Act
    stmt_total = build_update_statement(plans[1], Ledger)
    stmt_anchor = build_update_statement(plans[3], Ledger)

    # Assert
    where_total = str(
        stmt_total.whereclause.compile(compile_kwargs={"literal_binds": True})
    )
    assert "total_asset = 759399.59" in where_total
    assert stmt_total.compile().params["total_asset"] == 920_457.59

    where_anchor = str(
        stmt_anchor.whereclause.compile(compile_kwargs={"literal_binds": True})
    )
    assert "day_open_equity = 856948.02" in where_anchor
    assert "total_asset =" not in where_anchor  # 自身无病的行不许被当 total 修复
    assert stmt_anchor.compile().params["day_open_equity"] == 918_040.02


def test_plan_repairs_leaves_reported_pnl_untouched_for_other_sources():
    """非本方派生的 source（如回填）只换锚，不改 *_raw 盈亏。"""
    # Arrange：09-04 制造坏值锚 856,948.02，再让回填行引用它
    rows = [
        _ledger_row(),
        _ledger_row(
            id=2,
            snapshot_date=date(2026, 9, 4),
            total_asset=856_948.02,
            cash=839_992.01,
            market_value=78_048.01,
            day_open_equity=759_399.59,
        ),
        _ledger_row(
            id=4,
            snapshot_date=date(2026, 9, 8),
            total_asset=918_040.02,
            cash=839_992.01,
            market_value=78_048.01,
            day_open_equity=856_948.02,  # ← 09-04 坏值
            source="qmt_bridge_backfill",
            today_pnl_raw=0.0,
        ),
    ]

    # Act
    plans = plan_repairs(rows)
    by_id = {p["id"]: p for p in plans}

    # Assert
    assert by_id[4]["updates"]["day_open_equity"] == 918_040.02
    assert "today_pnl_raw" not in by_id[4]["updates"]
    assert "total_pnl_raw" not in by_id[4]["updates"]


def test_plan_repairs_is_idempotent_when_fed_back_its_own_output():
    """回灌：修复后的行再扫一遍不再命中（幂等，可反复执行）。"""
    # Arrange：含传播链的三行
    rows = [
        _ledger_row(),
        _ledger_row(
            id=2,
            snapshot_date=date(2026, 9, 4),
            total_asset=856_948.02,
            cash=839_992.01,
            market_value=78_048.01,
            day_open_equity=759_399.59,
        ),
        _ledger_row(
            id=3,
            snapshot_date=date(2026, 9, 7),
            total_asset=919_715.02,
            cash=839_992.01,
            market_value=79_723.01,
            day_open_equity=856_948.02,
        ),
    ]

    # Act
    plans = plan_repairs(rows)
    assert len(plans) == 3
    repaired_rows = []
    for row, plan in zip(rows, plans, strict=True):
        repaired = dict(row)
        repaired.update(plan["updates"])
        repaired["payload_json"] = plan["payload_json"]
        repaired_rows.append(repaired)

    # Assert
    assert plan_repairs(repaired_rows) == []


# ── 写侧接线：锚值取数必须先归一 ─────────────────────────────────────


def _flat(src: str) -> str:
    """去空白后再断言——源码守卫不该被换行重排（ruff format）打红。"""
    import re

    return re.sub(r"\s+", "", src)


def test_tdx_writer_normalizes_day_open_anchors_at_source():
    """day_open/month_open/initial 的锚取自快照原始 total，必须先过归一。"""
    import inspect

    from backend.services.live_trading.services import tdx_push_service as mod

    src = _flat(inspect.getsource(mod))
    assert _flat("normalize_equity(prev_row[0], prev_row[1], prev_row[2])") in src
    assert _flat("normalize_equity(row[0], row[1], row[2])") in src


def test_backfill_normalizes_before_inferring_day_open():
    """回填链路的 day_open 由 total 推得，归一必须在推导之前。"""
    import inspect

    from backend.services.trade.services import real_account_ledger_service as mod

    src = _flat(inspect.getsource(mod.backfill_daily_ledgers_from_snapshots))
    normalize_call = _flat("normalize_equity(total_asset, cash, market_value)")
    assert normalize_call in src
    assert src.index(normalize_call) < src.index(
        _flat("inferred_day_open = total_asset - today_pnl_raw")
    )
