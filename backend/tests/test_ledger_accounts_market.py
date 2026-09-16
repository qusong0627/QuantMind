"""市场化账户（account_id 带市场段，T-P1-04 收口）测试。

覆盖：
1. 纯函数：id 生成/反解（CN 无后缀、枚举解包回归守卫、非法段兜底）；
2. 真库 E2E：双市场 record_trade → 两账户行两 id + 唯一索引可插第三市场；
   load_projection(market=) 现金/持仓双隔离；
3. reset 收窄：按市场删台账行（其它市场保留）；
4. 回填脚本：legacy 行 → 清单 → 补后缀/补建账户行 → 幂等；
5. 源守卫：唯一实现委托、无手写 id 残留、企业行为按市场回写。
"""

from __future__ import annotations

import importlib.util
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND = Path(__file__).resolve().parents[1]
_TEST_TENANT_PREFIX = "t-mktacct"


# ── 纯函数 ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_ledger_account_id_contract():
    from backend.services.simulation.services.market_rules import Market
    from backend.shared.simulation_account_keys import (
        ledger_account_id,
        market_from_ledger_account_id,
        normalize_market,
    )

    # CN（含空/枚举）无后缀——存量行零迁移
    assert ledger_account_id("default", 1) == "sim:default:1"
    assert ledger_account_id("default", "1", "CN") == "sim:default:1"
    assert ledger_account_id("default", "1", None) == "sim:default:1"
    # 枚举解包回归守卫（str(Market.CN) 是 "Market.CN" 而非 "CN"——曾致错后缀）
    assert normalize_market(Market.CN) == "CN"
    assert ledger_account_id("default", "1", Market.HK) == "sim:default:1:HK"
    # 非 CN 带后缀 + 大小写归一
    assert ledger_account_id("default", "1", "hk") == "sim:default:1:HK"
    assert ledger_account_id("default", "1", "FUTURES") == "sim:default:1:FUTURES"
    # 反解（含 legacy 三段→CN 与非法串兜底）
    assert market_from_ledger_account_id("sim:default:1") == "CN"
    assert market_from_ledger_account_id("sim:default:1:HK") == "HK"
    assert market_from_ledger_account_id("default:1") == "CN"
    assert market_from_ledger_account_id(None) == "CN"


# ── 真库夹具 ────────────────────────────────────────────────────────


def _order(tenant: str, user: str, symbol: str, side: str = "buy"):
    return SimpleNamespace(tenant_id=tenant, user_id=user, symbol=symbol, side=side)


def _trade(tag: str, *, qty: float = 100.0, price: float = 40.0, side: str = "buy"):
    return SimpleNamespace(
        side=side,
        trade_id=f"{_TEST_TENANT_PREFIX}-{tag}",
        quantity=qty,
        price=price,
        total_fee=5.0,
        trade_value=qty * price,
        commission=5.0,
        stamp_duty=0.0,
        transfer_fee=0.0,
        executed_at=datetime.now(timezone.utc),
    )


async def _ensure_db_pool():
    from sqlalchemy import text as _t

    from backend.shared.database_manager_v2 import close_database, get_session

    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(_t("SELECT 1"))
        return
    except Exception:  # noqa: BLE001
        await close_database()
    async with get_session(read_only=True) as probe:
        await probe.execute(_t("SELECT 1"))


async def _cleanup_tenant(tenant: str):
    from sqlalchemy import text as _t

    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=False) as session:
        for table in (
            "simulation_cash_ledger",
            "simulation_position_lots",
            "simulation_accounts",
            "simulation_orders",
        ):
            await session.execute(
                _t(f"DELETE FROM {table} WHERE tenant_id=:t"), {"t": tenant}
            )
        await session.commit()


# ── 真库 E2E：双市场账户 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_market_accounts_e2e_real_db():
    await _ensure_db_pool()
    from sqlalchemy import text as sa_text

    from backend.services.simulation.services.ledger_service import (
        SimulationLedgerService,
    )
    from backend.services.simulation.services.projection_service import (
        SimulationProjectionService,
    )
    from backend.shared.database_manager_v2 import close_database, get_session
    from backend.shared.ledger_contract import ensure_accounts_market_contract_async
    from backend.shared.simulation_account_keys import ledger_account_id

    assert await ensure_accounts_market_contract_async() is True
    assert await ensure_accounts_market_contract_async() is True  # 幂等

    tenant = f"{_TEST_TENANT_PREFIX}-{uuid.uuid4().hex[:6]}"
    user = "77"
    try:
        # 契约实形：列 + 新唯一索引存在、旧索引已移除
        async with get_session(read_only=True) as session:
            col = (
                await session.execute(
                    sa_text(
                        "SELECT 1 FROM information_schema.columns WHERE "
                        "table_name='simulation_accounts' AND column_name='market'"
                    )
                )
            ).fetchone()
            new_idx = (
                await session.execute(
                    sa_text(
                        "SELECT 1 FROM pg_indexes WHERE indexname="
                        "'idx_simulation_accounts_tenant_user_market'"
                    )
                )
            ).fetchone()
            old_idx = (
                await session.execute(
                    sa_text(
                        "SELECT 1 FROM pg_indexes WHERE indexname="
                        "'idx_simulation_accounts_tenant_user'"
                    )
                )
            ).fetchone()
        assert col is not None and new_idx is not None and old_idx is None

        # 双市场 record_trade（同用户 CN + HK 各一笔）
        async with get_session(read_only=False) as session:
            ledger = SimulationLedgerService(session)
            await ledger.record_trade(
                order=_order(tenant, user, "600036.SH"),
                trade=_trade("cn", qty=100.0, price=40.0),
                account_snapshot={
                    "cash": 100_000.0,
                    "total_asset": 100_000.0,
                    "initial_equity": 100_000.0,
                },
                market="CN",
            )
            await ledger.record_trade(
                order=_order(tenant, user, "0700.HK"),
                trade=_trade("hk", qty=200.0, price=10.0),
                account_snapshot={
                    "cash": 500_000.0,
                    "total_asset": 500_000.0,
                    "initial_equity": 500_000.0,
                },
                market="HK",
            )
            await session.commit()

        cn_id = ledger_account_id(tenant, user, "CN")
        hk_id = ledger_account_id(tenant, user, "HK")
        async with get_session(read_only=True) as session:
            rows = (
                await session.execute(
                    sa_text(
                        "SELECT account_id, market, cash FROM simulation_accounts "
                        "WHERE tenant_id=:t ORDER BY account_id"
                    ),
                    {"t": tenant},
                )
            ).fetchall()
        by_id = {str(r[0]): (str(r[1]), float(r[2])) for r in rows}
        assert set(by_id) == {cn_id, hk_id}, by_id
        # 账户行 cash = 市场快照 + 本笔成交效应（buy：-gross-fee）——两市场各自独立
        assert by_id[cn_id] == ("CN", 100_000.0 - 4000.0 - 5.0)
        assert by_id[hk_id] == ("HK", 500_000.0 - 2000.0 - 5.0)

        # 持仓/流水按市场落对应账户 id
        async with get_session(read_only=True) as session:
            lot_rows = (
                await session.execute(
                    sa_text(
                        "SELECT DISTINCT account_id, market FROM simulation_position_lots "
                        "WHERE tenant_id=:t"
                    ),
                    {"t": tenant},
                )
            ).fetchall()
        assert {(str(r[0]), str(r[1])) for r in lot_rows} == {(cn_id, "CN"), (hk_id, "HK")}

        # load_projection(market=HK)：现金取 HK 行、持仓只含 HK（双隔离）
        async def _hk_price_loader(_symbol: str) -> float:  # 契约：async loader
            return 10.0

        async with get_session(read_only=True) as session:
            svc = SimulationProjectionService(session)
            snap_hk = await svc.load_projection(
                tenant_id=tenant,
                user_id=user,
                latest_price_loader=_hk_price_loader,
                market="HK",
            )
        assert snap_hk.account is not None
        assert str(snap_hk.account.account_id) == hk_id
        # 账户行 cash = HK 市场快照扣本笔（与上方 DB 断言同源）
        assert float(snap_hk.account.cash) == 500_000.0 - 2000.0 - 5.0
        # 持仓严格隔离：HK 投影只含 HK 标的，CN 批次不得串入
        assert set(snap_hk.positions) == {"0700.HK"}, snap_hk.positions
        # 第三市场可插（唯一索引不阻塞；CN 无后缀约定下三行共存）
        async with get_session(read_only=False) as session:
            ledger = SimulationLedgerService(session)
            await ledger.record_trade(
                order=_order(tenant, user, "AAPL"),
                trade=_trade("us", qty=50.0, price=100.0),
                account_snapshot={
                    "cash": 300_000.0,
                    "total_asset": 300_000.0,
                    "initial_equity": 300_000.0,
                },
                market="US",
            )
            await session.commit()
        async with get_session(read_only=True) as session:
            total = (
                await session.execute(
                    sa_text(
                        "SELECT count(*) FROM simulation_accounts WHERE tenant_id=:t"
                    ),
                    {"t": tenant},
                )
            ).scalar_one()
        assert int(total) == 3, "三市场账户行应共存"
    finally:
        await _cleanup_tenant(tenant)
        await close_database()


# ── reset 收窄（共享实现）────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reset_delete_scoped_by_market_real_db():
    await _ensure_db_pool()
    from sqlalchemy import text as sa_text

    from backend.services.simulation.services.ledger_service import (
        SimulationLedgerService,
    )
    from backend.shared.database_manager_v2 import close_database, get_session
    from backend.shared.ledger_reset import delete_user_ledger_rows

    tenant = f"{_TEST_TENANT_PREFIX}-rst-{uuid.uuid4().hex[:6]}"
    user = "88"
    try:
        async with get_session(read_only=False) as session:
            ledger = SimulationLedgerService(session)
            for market, cash in (("CN", 100_000.0), ("HK", 200_000.0)):
                await ledger.record_trade(
                    order=_order(tenant, user, f"{market}-SYM"),
                    trade=_trade(f"rst-{market}", qty=100.0, price=10.0),
                    account_snapshot={
                        "cash": cash,
                        "total_asset": cash,
                        "initial_equity": cash,
                    },
                    market=market,
                )
            await session.commit()

        async with get_session(read_only=False) as session:
            counts = await delete_user_ledger_rows(
                session, tenant_id=tenant, user_id_variants=[user], market="HK"
            )
            await session.commit()
        assert counts.get("simulation_accounts", 0) >= 1, counts

        async with get_session(read_only=True) as session:
            remain = (
                await session.execute(
                    sa_text(
                        "SELECT DISTINCT COALESCE(market,'CN') FROM simulation_position_lots "
                        "WHERE tenant_id=:t"
                    ),
                    {"t": tenant},
                )
            ).fetchall()
            accounts = (
                await session.execute(
                    sa_text(
                        "SELECT DISTINCT market FROM simulation_accounts WHERE tenant_id=:t"
                    ),
                    {"t": tenant},
                )
            ).fetchall()
        assert {str(r[0]) for r in remain} == {"CN"}, "HK 批次应删、CN 保留"
        assert {str(r[0]) for r in accounts} == {"CN"}, "HK 账户行应删、CN 保留"
    finally:
        await _cleanup_tenant(tenant)
        await close_database()


# ── 回填脚本（真库夹具）───────────────────────────────────────────────


def _load_backfill_module():
    path = _BACKEND / "scripts" / "backfill_ledger_account_market.py"
    spec = importlib.util.spec_from_file_location("backfill_ledger_account_market", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_backfill_legacy_rows_real_db():
    await _ensure_db_pool()
    from sqlalchemy import text as sa_text

    from backend.shared.database_manager_v2 import close_database, get_session

    mod = _load_backfill_module()
    tenant = f"{_TEST_TENANT_PREFIX}-bf-{uuid.uuid4().hex[:6]}"
    user = "99"
    legacy_id = f"sim:{tenant}:{user}"  # 合并形态（无市场段）
    try:
        # 造 legacy：HK 流水与批次挂了合并 id；无 HK 账户行
        async with get_session(read_only=False) as session:
            await session.execute(
                sa_text(
                    "INSERT INTO simulation_cash_ledger "
                    "(account_id, tenant_id, user_id, event_type, ref_type, amount, "
                    " balance_after, occurred_at, currency, market) "
                    "VALUES (:a, :t, :u, 'trade', 'fill', -1000, 12345.67, now(), 'HKD', 'HK')"
                ),
                {"a": legacy_id, "t": tenant, "u": user},
            )
            await session.execute(
                sa_text(
                    "INSERT INTO simulation_position_lots "
                    "(account_id, tenant_id, user_id, market, symbol, position_side, open_date, "
                    " quantity_open, quantity_remaining, cost_price, cost_amount, status) "
                    "VALUES (:a, :t, :u, 'HK', '0700.HK', 'long', now(), 100, 100, 10, 1000, 'open')"
                ),
                {"a": legacy_id, "t": tenant, "u": user},
            )
            await session.commit()

        async with get_session(read_only=True) as session:
            suffix_plan = await mod._scan_suffix_plan(session)
            gap_plan = await mod._scan_account_gap_plan(session)
        my_suffix = [p for p in suffix_plan if p["tenant_id"] == tenant]
        my_gap = [p for p in gap_plan if p["tenant_id"] == tenant]
        assert len(my_suffix) == 2 and my_suffix[0]["new"].endswith(":HK"), my_suffix
        assert len(my_gap) == 1 and my_gap[0]["cash"] == pytest.approx(12345.67), my_gap

        # apply：补后缀 + 补建账户行（cash=末笔 balance_after、initial_equity=0 诚实缺省）
        assert await mod.run(apply=True) == 0
        async with get_session(read_only=True) as session:
            ids = (
                await session.execute(
                    sa_text(
                        "SELECT DISTINCT account_id FROM simulation_position_lots "
                        "WHERE tenant_id=:t"
                    ),
                    {"t": tenant},
                )
            ).fetchall()
            acct = (
                await session.execute(
                    sa_text(
                        "SELECT market, cash, initial_equity FROM simulation_accounts "
                        "WHERE tenant_id=:t"
                    ),
                    {"t": tenant},
                )
            ).fetchone()
        assert {str(r[0]) for r in ids} == {f"{legacy_id}:HK"}, ids
        assert acct is not None and str(acct[0]) == "HK"
        assert float(acct[1]) == pytest.approx(12345.67)
        assert float(acct[2]) == 0.0  # 不猜测初始资金

        # 幂等：重扫本租户应为空清单
        async with get_session(read_only=True) as session:
            again = [
                p for p in await mod._scan_suffix_plan(session) if p["tenant_id"] == tenant
            ]
            again_gap = [
                p for p in await mod._scan_account_gap_plan(session) if p["tenant_id"] == tenant
            ]
        assert again == [] and again_gap == []
    finally:
        await _cleanup_tenant(tenant)
        await close_database()


# ── 源守卫 ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_market_accounts_wiring_source_guards():
    ledger_src = (
        _BACKEND / "services/simulation/services/ledger_service.py"
    ).read_text(encoding="utf-8")
    assert "ledger_account_id" in ledger_src
    assert "ensure_accounts_market_contract_async" in ledger_src
    assert "market=market_n" in ledger_src  # 账户行按市场落

    proj_src = (
        _BACKEND / "services/simulation/services/projection_service.py"
    ).read_text(encoding="utf-8")
    assert "ledger_account_id" in proj_src
    assert "self.build_account_id(tenant_id, user_id, market)" in proj_src  # 按市场取账户行
    assert "async def get_available_quantity" not in proj_src  # 死代码已清

    worker_src = (
        _BACKEND / "services/simulation/services/equity_settlement_worker.py"
    ).read_text(encoding="utf-8")
    assert 'f"sim:{tenant_id}:{user_id}"' not in worker_src  # 手写 id 已归位
    assert "ledger_account_id(tenant_id, user_id, market)" in worker_src

    ca_src = (
        _BACKEND / "services/simulation/services/corporate_action_service.py"
    ).read_text(encoding="utf-8")
    assert "market_from_ledger_account_id" in ca_src
    assert "account_key(tenant_id, user_id, market)" in ca_src

    reset_src = (
        _BACKEND / "services/simulation/routers/simulation.py"
    ).read_text(encoding="utf-8")
    assert "delete_user_ledger_rows" in reset_src

    keys_src = (_BACKEND / "shared/simulation_account_keys.py").read_text(encoding="utf-8")
    assert "getattr(market, \"value\", market)" in keys_src  # 枚举解包回归守卫
