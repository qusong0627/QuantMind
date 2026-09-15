"""T-P1-04 测试：Ledger 契约（市场维度）+ 成交必落账 E2E。

覆盖：
1. normalize_ledger_market 纯函数；模型字段存在；
2. 自愈迁移安全化三纪律（同 signal/order 契约）源断言；
3. 写入/读取接线源断言（record_trade 市场参数、投影按市场过滤、重建/ EOD/融券透传、
   apply_filled 落市场、引擎幂等去重）；
4. 体检 C05b 覆盖率判定纯函数；
5. **成交必落账 E2E（集成，真库、COMMIT 后校验再清理）**：record_trade 一次买入 →
   simulation_accounts / simulation_position_lots / simulation_cash_ledger 三表齐落，
   且 lot/流水带 market；随后按测试租户清理。
"""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.shared.ledger_contract import (
    LEDGER_TABLE_COLUMNS,
    ensure_ledger_contract_columns_async,
    normalize_ledger_market,
)

_BACKEND = Path(__file__).resolve().parents[1]


def test_normalize_ledger_market():
    assert normalize_ledger_market(None) == "CN"
    assert normalize_ledger_market("") == "CN"
    assert normalize_ledger_market("hk") == "HK"


def test_models_have_market():
    from backend.services.simulation.models.cash_ledger import SimulationCashLedger
    from backend.services.simulation.models.position_lot import SimulationPositionLot

    assert hasattr(SimulationPositionLot, "market")
    assert hasattr(SimulationCashLedger, "market")


def test_migration_is_lock_safe():
    src = (_BACKEND / "shared/ledger_contract.py").read_text(encoding="utf-8")
    assert "information_schema.columns" in src
    assert "lock_timeout" in src
    assert "不阻断业务" in src
    assert set(LEDGER_TABLE_COLUMNS) == {
        "simulation_position_lots",
        "simulation_cash_ledger",
    }


def test_writers_and_readers_wired():
    ledger_src = (_BACKEND / "services/simulation/services/ledger_service.py").read_text(
        encoding="utf-8"
    )
    assert "market: str | None = None" in ledger_src
    assert "market=market" in ledger_src  # lot / cash 落市场
    assert 'coalesce(SimulationPositionLot.market, "CN") == market' in ledger_src

    proj_src = (_BACKEND / "services/simulation/services/projection_service.py").read_text(
        encoding="utf-8"
    )
    assert "market: str | None = None" in proj_src
    assert 'coalesce(SimulationPositionLot.market, "CN")' in proj_src

    rebuild_src = (
        _BACKEND / "services/trade_shared/simulation_manager.py"
    ).read_text(encoding="utf-8")
    assert "market=market," in rebuild_src  # 重建透传市场

    exec_src = (_BACKEND / "services/simulation/services/execution_engine.py").read_text(
        encoding="utf-8"
    )
    assert "ensure_ledger_contract_columns_async()" in exec_src
    assert 'market=str(getattr(result, "market", None) or "CN")' in exec_src

    engine_src = (_BACKEND / "services/simulation/engine.py").read_text(encoding="utf-8")
    assert "RULE:SIM-DEDUP" in engine_src  # 引擎重跑幂等去重


def test_health_coverage_classifier():
    from backend.scripts.diagnose.health import classify_ledger_coverage

    assert classify_ledger_coverage(0, 0).level == "ok"
    assert classify_ledger_coverage(5, 5).level == "ok"
    r = classify_ledger_coverage(5, 3)
    assert r.level == "warn" and "2 笔" in r.detail


# --- 成交必落账 E2E（集成；真库 COMMIT 后校验再清理）-------------------------

_TEST_TENANT = "t-p1-04-test"
_TEST_USER = "t-ledger"


def _fake_order():
    return SimpleNamespace(
        tenant_id=_TEST_TENANT,
        user_id=_TEST_USER,
        symbol="600036.SH",
        side="buy",  # 兼容 str 与枚举（代码取 getattr(side,'value',side)）
        trade_action=None,
        position_side="long",
    )


def _fake_trade():
    from datetime import datetime, timezone

    return SimpleNamespace(
        trade_id="t-p1-04-fill-1",
        quantity=100.0,
        price=40.0,
        total_fee=5.0,
        trade_value=4000.0,
        commission=5.0,
        stamp_duty=0.0,
        transfer_fee=0.0,
        executed_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_record_trade_writes_all_ledger_tables():
    """成交必落账：一次买入 → 账户/批次/流水三表齐落且带市场；随后清理。"""
    try:
        from sqlalchemy import text

        from backend.services.simulation.services.ledger_service import (
            SimulationLedgerService,
        )
        from backend.shared.database_manager_v2 import get_session
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"依赖不可用: {exc}")

    try:
        async with get_session(read_only=False) as session:
            await ensure_ledger_contract_columns_async()
            svc = SimulationLedgerService(session)
            await svc.record_trade(
                order=_fake_order(),
                trade=_fake_trade(),
                account_snapshot={"cash": 100000.0, "total_asset": 100000.0, "initial_equity": 100000.0},
                market="CN",
            )
            await session.commit()

        async with get_session(read_only=True) as session:
            accounts = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM simulation_accounts WHERE tenant_id=:t"
                    ),
                    {"t": _TEST_TENANT},
                )
            ).scalar_one()
            lots = (
                await session.execute(
                    text(
                        "SELECT count(*), max(market) FROM simulation_position_lots "
                        "WHERE tenant_id=:t AND symbol='600036.SH'"
                    ),
                    {"t": _TEST_TENANT},
                )
            ).one()
            cash = (
                await session.execute(
                    text(
                        "SELECT count(*), max(market), count(*) FILTER (WHERE ref_id='t-p1-04-fill-1') "
                        "FROM simulation_cash_ledger WHERE tenant_id=:t"
                    ),
                    {"t": _TEST_TENANT},
                )
            ).one()
        assert int(accounts) == 1, "账户行应创建"
        assert int(lots[0]) >= 1 and lots[1] == "CN", "持仓批次应落库且带市场"
        assert int(cash[0]) >= 1 and cash[1] == "CN", "现金流水应落库且带市场"
        assert int(cash[2]) >= 1, "流水 ref_id 应关联成交"
    finally:
        # 清理测试租户数据（三表）
        try:
            from sqlalchemy import text as _text

            from backend.shared.database_manager_v2 import get_session as _gs

            async with _gs(read_only=False) as session:
                for table in (
                    "simulation_cash_ledger",
                    "simulation_position_lots",
                    "simulation_accounts",
                ):
                    await session.execute(
                        _text(f"DELETE FROM {table} WHERE tenant_id=:t"),
                        {"t": _TEST_TENANT},
                    )
                await session.commit()
        except Exception:  # noqa: BLE001
            pass
