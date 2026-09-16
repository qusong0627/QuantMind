"""回归与单测：用户级资金快照多市场合并 + 初始资金按市场求和（P0-05）+ 调度日历兜底。

1. account_key 解析不再跳过非 CN 市场账户（跨市场合并入用户级快照）
2. resolve_account_seed：显式 initial_cash > 未交易启发式 > CN settings > 未知
   （2026-09-15 诊断事故：CN+FUTURES 双账户用户快照 total_pnl 曾虚增 +100 万）
3. SimulationScheduler._is_trading_day：日历不可用时回退周判断
"""

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from backend.services.simulation.scheduler import SimulationScheduler
from backend.services.simulation.services.fund_snapshot_service import (
    resolve_account_seed,
)
from backend.shared.simulation_account_keys import parse_account_key


def test_parse_account_key_accepts_all_markets():
    # CN（无后缀）与带市场后缀的键都应解析成功，交给 capture_all 合并
    assert parse_account_key("simulation:account:default:1") == ("default", "1", "CN")
    assert parse_account_key("simulation:account:default:1:CN") == ("default", "1", "CN")
    assert parse_account_key("simulation:account:default:1:HK") == ("default", "1", "HK")
    assert parse_account_key("simulation:account:default:1:US") == ("default", "1", "US")
    assert parse_account_key("simulation:account:default:1:FUTURES") == (
        "default",
        "1",
        "FUTURES",
    )
    # 非账户键返回 None
    assert parse_account_key("simulation:settings:default:1") is None
    assert parse_account_key("") is None


def _traded_account(cash="488965.58", total="1008379.58"):
    return {
        "cash": cash,
        "total_asset": total,
        "positions": {"300649.SZ": {"volume": 700, "market_value": 18186}},
    }


def test_seed_explicit_initial_cash_wins():
    acc = _traded_account()
    acc["initial_cash"] = 1000000
    assert resolve_account_seed(acc, "CN", Decimal("500000")) == Decimal("1000000")


def test_seed_untraded_account_uses_total_asset():
    # 未交易（无持仓、现金==总资产）：无盈亏，种子=当前总资产
    # —— 这是修"多市场种子虚增"的关键分支（期货账户从未成交）
    acc = {"cash": 1000000.0, "total_asset": 1000000.0, "positions": {}}
    assert resolve_account_seed(acc, "FUTURES", None) == Decimal("1000000.0")


def test_seed_traded_cn_falls_back_to_settings():
    acc = _traded_account()
    assert resolve_account_seed(acc, "CN", Decimal("1000000")) == Decimal("1000000")


def test_seed_traded_non_cn_unknown_returns_none():
    acc = _traded_account()
    assert resolve_account_seed(acc, "HK", Decimal("1000000")) is None


def test_seed_zero_volume_positions_count_as_untraded():
    acc = {
        "cash": 1000000.0,
        "total_asset": 1000000.0,
        "positions": {"600036.SH": {"volume": 0, "market_value": 0}},
    }
    assert resolve_account_seed(acc, "US", None) == Decimal("1000000.0")


def test_multi_market_seed_sum_fixes_phantom_profit():
    """复现 2026-09-15 事故：CN(已交易, settings 100 万) + FUTURES(未交易 100 万)。

    合并后 initial_capital 应为 200 万 → total_pnl ≈ +8379.58，
    而不是拿单份 settings 当初始导致的 +1008379.58 假收益。
    """
    cn = _traded_account()
    fut = {
        "cash": 1000000.0,
        "total_asset": 1000000.0,
        "positions": {},
        "market": "FUTURES",
    }
    seed_cn = resolve_account_seed(cn, "CN", Decimal("1000000"))
    seed_fut = resolve_account_seed(fut, "FUTURES", None)
    initial = seed_cn + seed_fut
    total_asset = Decimal("1008379.58") + Decimal("1000000.0")
    assert initial == Decimal("2000000")
    assert total_asset - initial == Decimal("8379.58")


def test_scheduler_trading_day_fallback():
    scheduler = SimulationScheduler.__new__(SimulationScheduler)  # 不走 __init__（避免连 Redis）
    sh = ZoneInfo("Asia/Shanghai")
    # 周六无论日历与否必非交易日（日历可用时同样返回 False）
    saturday = datetime(2026, 9, 12, 9, 35, tzinfo=sh)
    assert scheduler._is_trading_day(saturday) is False
    # 周一：日历可用时按 XSHG 判断（True）；不可用回退周判断（True）。均为 True。
    monday = datetime(2026, 9, 14, 9, 35, tzinfo=sh)
    assert scheduler._is_trading_day(monday) is True


def test_snapshots_daily_uses_normalized_user_id():
    """回归 #2（T-P0-04）：/snapshots/daily 必须用 require_sim_user_id 归一后的 ID。

    曾用原始 JWT sub（00000001）直读，而快照行由账户键解析而来（int 归一 → "1"），
    导致 admin 资金曲线读到另一个空账户的平线。
    """
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1]
        / "services"
        / "simulation"
        / "routers"
        / "simulation.py"
    ).read_text(encoding="utf-8")
    anchor = src.index("async def list_simulation_fund_snapshots")
    block = src[anchor : anchor + 900]
    assert "_require_user_id" in block, "snapshots/daily 未使用归一身份"
    assert "user_id=str(auth.user_id)" not in block, "snapshots/daily 仍在直读原始 sub"


# ── T-P1-07：市场维度 + 种子注入不计入 today_pnl ──────────────────────


def _baselines_case(**kw):
    from backend.services.simulation.services.fund_snapshot_service import (
        compute_market_baselines,
    )

    defaults = dict(
        today_totals={"CN": Decimal("1010000"), "FUTURES": Decimal("1000000")},
        seeds={"CN": Decimal("1000000"), "FUTURES": Decimal("1000000")},
        prev_day_totals={"CN": Decimal("1000000")},  # FUTURES 新开：无历史
        prev_month_totals={"CN": Decimal("980000")},
        is_month_start=False,
    )
    defaults.update(kw)
    return compute_market_baselines(**defaults)


def test_compute_market_baselines_new_market_seed_not_profit():
    """纯函数核心口径：新市场首日基线=其种子（注入不算盈利）；ALL=各市场基线之和。"""
    out = _baselines_case()
    assert out["CN"]["day_open_equity"] == Decimal("1000000")
    assert out["FUTURES"]["day_open_equity"] == Decimal("1000000")  # 种子，不是 0
    assert out["ALL"]["day_open_equity"] == Decimal("2000000")
    assert out["ALL"]["month_open_equity"] == Decimal("1980000")  # 980000 + FUTURES 种子


def test_compute_market_baselines_unknown_seed_uses_today_total():
    """种子未知（None）→ 基线=当日总资产（不声称任何盈亏，与"未知种子不参与求和"一致）。"""
    out = _baselines_case(
        today_totals={"CN": Decimal("1010000"), "HK": Decimal("500000")},
        seeds={"CN": Decimal("1000000"), "HK": None},
        prev_day_totals={"CN": Decimal("1000000")},
    )
    assert out["HK"]["day_open_equity"] == Decimal("500000")
    assert out["ALL"]["day_open_equity"] == Decimal("1500000")


def test_compute_market_baselines_month_start_and_mid_month():
    """月初（1 号）无上月快照 → 月初基线=日初；月中新建市场 → 月初基线=种子。"""
    on_first = _baselines_case(prev_month_totals={}, is_month_start=True)
    assert on_first["CN"]["month_open_equity"] == on_first["CN"]["day_open_equity"]
    mid_month = _baselines_case(prev_month_totals={"CN": Decimal("980000")})
    assert mid_month["FUTURES"]["month_open_equity"] == Decimal("1000000")


class _FakeRawRedis:
    def __init__(self, store: dict[str, str]):
        self.store = store

    def scan_iter(self, match: str = "*", count: int = 500):
        prefix = match.rstrip("*")
        for k in list(self.store.keys()):
            if k.startswith(prefix):
                yield k

    def get(self, key: str):
        return self.store.get(key)


class _FakeRedisWrapper:
    def __init__(self, store: dict[str, str]):
        self.client = _FakeRawRedis(store)


async def _ensure_db_pool():
    """跨事件循环池自愈（仓库既有纪律）：先探活，失败关池重试一次后放行。"""
    from sqlalchemy import text as _t

    from backend.shared.database_manager_v2 import close_database, get_session

    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(_t("SELECT 1"))
        return
    except Exception:  # noqa: BLE001 - 池绑定旧循环（asyncpg 陷阱）
        await close_database()
    async with get_session(read_only=True) as probe:
        await probe.execute(_t("SELECT 1"))


@pytest.mark.asyncio
async def test_capture_all_market_dimension_real_db():
    """真库 E2E：双市场账户（CN 有昨日快照 + FUTURES 当日新开）→ 三行（CN/FUTURES/ALL），
    FUTURES 种子进基线不计 today_pnl，ALL 不出现 +100 万假收益。"""
    import json as _json
    import uuid as _uuid
    from datetime import date

    from backend.shared.database_manager_v2 import get_session
    from backend.services.simulation.services.fund_snapshot_service import (
        SimulationFundSnapshotService,
    )
    from sqlalchemy import text as sa_text

    await _ensure_db_pool()
    user = f"FS{_uuid.uuid4().hex[:6]}"
    today = date(2026, 12, 8)  # 固定日避免与真实数据同日冲突
    yesterday = date(2026, 12, 7)
    store = {
        f"simulation:account:default:{user}:CN": _json.dumps(
            {"total_asset": 1010000, "cash": 900000, "market_value": 110000,
             "initial_cash": 1000000, "positions": {}}
        ),
        f"simulation:account:default:{user}:FUTURES": _json.dumps(
            {"total_asset": 1000000, "cash": 1000000, "market_value": 0,
             "initial_cash": 1000000, "positions": {}}
        ),
    }
    try:
        async with get_session(read_only=False) as session:
            await session.execute(
                sa_text(
                    "INSERT INTO simulation_fund_snapshots "
                    "(tenant_id, user_id, snapshot_date, market, total_asset, initial_capital) "
                    "VALUES ('default', :u, :d, 'CN', 1000000, 1000000)"
                ),
                {"u": user, "d": yesterday},
            )
        result = await SimulationFundSnapshotService.capture_all(
            _FakeRedisWrapper(store), snapshot_date=today
        )
        assert result.upserted_rows == 3  # CN + FUTURES + ALL

        async with get_session(read_only=True) as session:
            rows = (
                await session.execute(
                    sa_text(
                        "SELECT market, total_asset, initial_capital, today_pnl, total_pnl "
                        "FROM simulation_fund_snapshots "
                        "WHERE tenant_id='default' AND user_id=:u AND snapshot_date=:d"
                    ),
                    {"u": user, "d": today},
                )
            ).all()
        by = {str(r[0]): r for r in rows}
        assert set(by) == {"CN", "FUTURES", "ALL"}
        # CN：昨日 100 万 → 今日 101 万 = 真实 +1 万
        assert float(by["CN"][3]) == 10000.0  # [3]=today_pnl
        # FUTURES 新开：种子 100 万计入基线，today_pnl = 0（**不是 +100 万**）
        assert float(by["FUTURES"][3]) == 0.0
        # ALL：合计基线 = 100 万(昨日 CN) + 100 万(FUTURES 种子) → 仍只有真实 +1 万
        assert float(by["ALL"][3]) == 10000.0
        assert float(by["ALL"][1]) == 2010000.0  # [1]=total_asset
        assert float(by["ALL"][2]) == 2000000.0  # [2]=initial_capital（Σ 种子，P0-05 口径保持）
    finally:
        async with get_session(read_only=False) as session:
            await session.execute(
                sa_text(
                    "DELETE FROM simulation_fund_snapshots WHERE tenant_id='default' AND user_id=:u"
                ),
                {"u": user},
            )
        from backend.shared.database_manager_v2 import close_database

        await close_database()  # 用后关池（跨循环池纪律）


@pytest.mark.asyncio
async def test_get_baselines_market_aware_real_db():
    """真库：当天各市场行存在时，ALL 基线=各市场规则之和；单市场取该市场行。"""
    import uuid as _uuid
    from datetime import date

    from backend.shared.database_manager_v2 import get_session
    from backend.services.simulation.services.fund_snapshot_service import (
        SimulationFundSnapshotService,
    )
    from sqlalchemy import text as sa_text

    await _ensure_db_pool()
    user = f"FB{_uuid.uuid4().hex[:6]}"
    today = date(2026, 12, 9)
    yesterday = date(2026, 12, 8)
    try:
        async with get_session(read_only=False) as session:
            for market, d, total in (
                ("CN", yesterday, 1000000),
                ("CN", today, 1005000),
                ("HK", today, 2000000),  # HK 新开（无历史）：种子 200 万
                ("ALL", today, 3005000),
            ):
                seed = 1000000 if market == "CN" else 2000000 if market == "HK" else 3000000
                await session.execute(
                    sa_text(
                        "INSERT INTO simulation_fund_snapshots "
                        "(tenant_id, user_id, snapshot_date, market, total_asset, initial_capital) "
                        "VALUES ('default', :u, :d, :m, :t, :s)"
                    ),
                    {"u": user, "d": d, "m": market, "t": total, "s": seed},
                )
        base_all = await SimulationFundSnapshotService.get_baselines(
            "default", user, Decimal("3000000"), as_of=today
        )
        # ALL 基线 = CN 昨日 100 万 + HK 种子 200 万
        assert base_all["day_open_equity"] == Decimal("3000000")
        base_cn = await SimulationFundSnapshotService.get_baselines(
            "default", user, Decimal("1000000"), as_of=today, market="CN"
        )
        assert base_cn["day_open_equity"] == Decimal("1000000")
        base_hk = await SimulationFundSnapshotService.get_baselines(
            "default", user, Decimal("2000000"), as_of=today, market="HK"
        )
        assert base_hk["day_open_equity"] == Decimal("2000000")  # 无历史 → 传入种子
    finally:
        async with get_session(read_only=False) as session:
            await session.execute(
                sa_text(
                    "DELETE FROM simulation_fund_snapshots WHERE tenant_id='default' AND user_id=:u"
                ),
                {"u": user},
            )
        from backend.shared.database_manager_v2 import close_database

        await close_database()


@pytest.mark.asyncio
async def test_fund_snapshot_contract_real_db():
    """真库：契约就绪（列+新唯一索引），旧三列唯一约束已移除；幂等可重复调用。"""
    from backend.shared.fund_snapshot_contract import (
        ensure_fund_snapshot_contract_async,
    )

    await _ensure_db_pool()
    assert await ensure_fund_snapshot_contract_async() is True
    assert await ensure_fund_snapshot_contract_async() is True  # 幂等
    from backend.shared.database_manager_v2 import get_session
    from sqlalchemy import text as sa_text

    async with get_session(read_only=True) as session:
        col = (
            await session.execute(
                sa_text(
                    "SELECT 1 FROM information_schema.columns WHERE "
                    "table_name='simulation_fund_snapshots' AND column_name='market'"
                )
            )
        ).fetchone()
        assert col is not None
        idx = (
            await session.execute(
                sa_text(
                    "SELECT 1 FROM pg_indexes WHERE indexname='uq_sim_fund_snapshot_scope_date_market'"
                )
            )
        ).fetchone()
        assert idx is not None
        old = (
            await session.execute(
                sa_text(
                    "SELECT conname FROM pg_constraint WHERE "
                    "conrelid=CAST('simulation_fund_snapshots' AS regclass) AND contype='u' "
                    "AND pg_get_constraintdef(oid) NOT LIKE '%market%'"
                )
            )
        ).fetchone()
        assert old is None, f"旧唯一约束未移除: {old}"
    from backend.shared.database_manager_v2 import close_database

    await close_database()


@pytest.mark.unit
def test_fund_snapshot_contract_and_reader_source_guards():
    """接线源守卫：迁移安全化三纪律 + 关键读取方显式取市场行（防再混排）。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    contract = (root / "shared" / "fund_snapshot_contract.py").read_text(encoding="utf-8")
    assert "lock_timeout" in contract
    assert "information_schema" in contract  # 预检
    assert "不阻断" in contract  # 失败不抛出不阻断业务
    assert "DROP CONSTRAINT IF EXISTS" in contract and "CREATE UNIQUE INDEX IF NOT EXISTS" in contract

    desk = (root / "services/api/routers/desk.py").read_text(encoding="utf-8")
    assert "market = 'ALL'" in desk, "desk 盈亏卡未显式取合并行（会混排）"

    shadow = (root / "services/trade/services/shadow_compare_service.py").read_text(encoding="utf-8")
    assert "market = 'ALL'" in shadow

    user_strategies = (root / "services/engine/qlib_app/api/user_strategies.py").read_text(encoding="utf-8")
    assert "market = 'ALL'" in user_strategies

    health = (root / "scripts/diagnose/health.py").read_text(encoding="utf-8")
    assert "market = 'ALL'" in health

    account_card = (root / "scripts/eval/account_card.py").read_text(encoding="utf-8")
    assert "market = 'CN'" in account_card

    service = (
        root / "services/simulation/services/fund_snapshot_service.py"
    ).read_text(encoding="utf-8")
    assert "compute_market_baselines" in service
    assert "def _capture_all_legacy" in service  # 契约未就绪时的旧口径兜底保留
