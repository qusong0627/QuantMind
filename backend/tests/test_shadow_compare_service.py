"""T-P2-06 服务层测试：影子对照采集/日报/接线 + 真库 E2E（测试租户、用后清理）。

覆盖：
1. 纯函数：parse_shadow_time / uid_forms（归一优先）/ cst_day_window；
2. 接线源断言：trade/main 注册 worker、调度注册表 JobSpec、schedule_ctl 分派、
   dual_book 心跳；
3. **E2E（真库）**：造 1 组「模拟成交 + 镜像真单」配对 + 两侧各 3 日净值 →
   collect_day_pairs 配对正确 → run_shadow_compare 日报数值正确（价格偏差 bps/
   成交率/跟踪误差）并落 Redis；随后按测试租户清理。
"""

from __future__ import annotations

import asyncio  # noqa: F401  (风格统一：异步用例直接 await)
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.services.trade.services.shadow_compare_service import (
    cst_day_window,
    parse_shadow_time,
    uid_forms,
)
from backend.services.live_trading.services.trading_session import TZ

_BACKEND = Path(__file__).resolve().parents[1]

_TEST_TENANT = "_t_p206"
_TEST_UID = "620601"  # 6 位：模拟侧归一整型、实盘侧补零 —— 正是双键形场景
_REAL_UID_PADDED = "00620601"
_TEST_ACCOUNT_ID = f"qmt-{_TEST_TENANT}-{_REAL_UID_PADDED}"


class _FakeRedisClient:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def set(self, key, value, ex=None, nx=False):
        self.store[str(key)] = value
        return True

    def get(self, key):
        return self.store.get(str(key))

    def exists(self, key):
        return str(key) in self.store


def _fake_redis() -> SimpleNamespace:
    return SimpleNamespace(client=_FakeRedisClient())


# ── 纯函数 ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_parse_shadow_time_parsing_and_fallback():
    assert parse_shadow_time("15:15") == (15, 15)
    assert parse_shadow_time("09:05") == (9, 5)
    assert parse_shadow_time("bad") == (15, 15)
    assert parse_shadow_time("25:00") == (15, 15)


@pytest.mark.unit
def test_uid_forms_prefers_normalized_int():
    assert uid_forms("00000001") == ["1", "00000001"]
    assert uid_forms("1") == ["1", "00000001"]
    assert uid_forms("admin") == ["admin"]
    assert uid_forms("") == []


@pytest.mark.unit
def test_cst_day_window_is_shanghai_day():
    start, end = cst_day_window("20260916")
    assert start.tzinfo is not None
    assert (start.hour, start.minute) == (0, 0)
    assert end - start == timedelta(days=1)


@pytest.mark.unit
def test_raw_client_accepts_wrapper_and_native():
    """客户端形态兼容（T-P2-06 实测陷阱：CLI 原生客户端无 .client 曾致落盘静默 no-op）。"""
    from backend.services.trade.services.shadow_compare_service import _raw_client

    native = _FakeRedisClient()
    assert _raw_client(SimpleNamespace(client=native)) is native
    assert _raw_client(native) is native
    assert _raw_client(None) is None
    assert _raw_client(object()) is None


@pytest.mark.unit
def test_report_save_and_load_with_native_client():
    """原生客户端（无 .client 包装）下 save→load 闭环——不得静默丢报告。"""
    from backend.services.trade.services.shadow_compare_service import (
        _save_report,
        load_latest_report,
    )
    from backend.shared.shadow_compare import build_shadow_report
    from backend.services.live_trading.services.trading_session import TZ
    from datetime import datetime

    native = _FakeRedisClient()
    date_str = datetime.now(TZ).strftime("%Y%m%d")
    report = build_shadow_report(
        date_str=date_str,
        pairing={
            "pairs": [],
            "sim_only": [],
            "real_only": [],
            "symbol_side_mismatch": 0,
        },
        configured_bps=5.0,
    )
    _save_report(native, report)
    loaded = load_latest_report(native)
    assert loaded is not None
    assert loaded["date"] == date_str
    assert loaded["stale"] is False


@pytest.mark.unit
def test_reconcile_outcome_classification():
    """T-P2-06：对账三分类（台账空显式可见 / 干净留痕 / 差异）。"""
    from backend.services.simulation.services.reconcile_service import (
        classify_reconcile_outcome,
    )

    assert classify_reconcile_outcome(False, 0) == "ledger_empty"
    assert classify_reconcile_outcome(False, 3) == "ledger_empty"
    assert classify_reconcile_outcome(True, 0) == "clean"
    assert classify_reconcile_outcome(True, 2) == "diff"


# ── 接线源断言 ──────────────────────────────────────────────────────


@pytest.mark.unit
def test_trade_main_registers_shadow_worker():
    src = (_BACKEND / "services/trade/main.py").read_text(encoding="utf-8")
    assert "run_shadow_compare_worker" in src
    assert "shadow_compare_task" in src
    assert "shadow_compare_task," in src  # 进取消清单
    assert src.index("shadow_compare_task = None") < src.index(
        "shadow_compare_task = asyncio.create_task"
    )


@pytest.mark.unit
def test_scheduler_registry_has_shadow_and_dual_book():
    from backend.shared.scheduler_registry import JOBS_BY_KEY

    shadow = JOBS_BY_KEY["mirror_shadow"]
    assert shadow.heartbeat_ttl and shadow.switch_env == "MIRROR_SHADOW_ENABLED"
    assert shadow.rerun
    dual = JOBS_BY_KEY["dual_book"]
    assert dual.heartbeat_ttl and dual.switch_env == "MIRROR_RECONCILE_ENABLED"


@pytest.mark.unit
def test_schedule_ctl_dispatch_includes_new_tasks():
    src = (_BACKEND / "scripts/schedule_ctl.py").read_text(encoding="utf-8")
    assert '"dual_book": _run_dual_book' in src
    assert '"mirror_shadow": _run_shadow_compare' in src


@pytest.mark.unit
def test_dual_book_loop_writes_heartbeat():
    src = (
        _BACKEND / "services/trade/services/dual_book_reconciliation_task.py"
    ).read_text(encoding="utf-8")
    assert '_sched_heartbeat("dual_book")' in src


# ── E2E（真库） ─────────────────────────────────────────────────────


async def _cleanup() -> None:
    try:
        from sqlalchemy import text

        from backend.shared.database_manager_v2 import get_session

        async with get_session(read_only=False) as session:
            for table in (
                "sim_trades",
                "sim_orders",
                "orders",
                "simulation_fund_snapshots",
                "real_account_ledger_daily_snapshots",
                "simulation_reconcile_reports",
            ):
                await session.execute(
                    text(f"DELETE FROM {table} WHERE tenant_id=:t"),
                    {"t": _TEST_TENANT},
                )
            await session.commit()
    except Exception:  # noqa: BLE001
        pass


async def _seed_fixture() -> dict:
    """造 1 组配对 + 两侧 3 日净值；返回标识。"""
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    order_id = uuid4()
    trade_id = uuid4()
    real_order_id = uuid4()
    sim_cid = "sim-run-t206-600036.SH-buy"
    today = datetime.now(TZ).date()
    days = [today - timedelta(days=2), today - timedelta(days=1), today]

    async with get_session(read_only=False) as session:
        await session.execute(
            text(
                "INSERT INTO sim_orders (order_id, tenant_id, user_id, portfolio_id, "
                "symbol, side, order_type, trading_mode, status, quantity, "
                "filled_quantity, average_price, client_order_id, remarks) VALUES "
                "(:oid, :t, :u, 0, '600036.SH', 'buy', 'limit', 'SIMULATION', 'filled', "
                "100, 100, 10.0, :cid, :rmk)"
            ),
            {
                "oid": order_id,
                "t": _TEST_TENANT,
                "u": int(_TEST_UID),  # sim_orders.user_id 为整型
                "cid": sim_cid,
                "rmk": f"client_order_id={sim_cid} 测试",
            },
        )
        # sim_trades.executed_at：T-P1-04 后的 aware-UTC 语义（真实瞬时）
        await session.execute(
            text(
                "INSERT INTO sim_trades (trade_id, order_id, tenant_id, user_id, "
                "portfolio_id, symbol, side, trading_mode, quantity, price, "
                "trade_value, commission, stamp_duty, transfer_fee, total_fee, "
                "executed_at) VALUES "
                "(:tid, :oid, :t, :u, 0, '600036.SH', 'buy', 'SIMULATION', 100, 10.0, "
                "1000, 5.0, 0, 0.01, 5.01, :ts)"
            ),
            {
                "tid": trade_id,
                "oid": order_id,
                "t": _TEST_TENANT,
                "u": int(_TEST_UID),
                "ts": datetime.now(tz=TZ),
            },
        )
        await session.execute(
            text(
                "INSERT INTO orders (order_id, tenant_id, user_id, portfolio_id, "
                "symbol, side, position_side, is_margin_trade, order_type, "
                "trading_mode, status, quantity, filled_quantity, average_price, "
                "price, commission, client_order_id, exchange_order_id, remarks) VALUES "
                "(:oid, :t, :u, 0, '600036.SH', 'buy', 'LONG', false, 'limit', "
                "'REAL', 'filled', 100, 100, 10.06, 10.06, 6.0, :cid, 'EX-1', '')"
            ),
            {
                "oid": real_order_id,
                "t": _TEST_TENANT,
                "u": _TEST_UID,
                "cid": f"mir-{sim_cid}",
            },
        )
        for i, day in enumerate(days):
            await session.execute(
                text(
                    "INSERT INTO simulation_fund_snapshots (tenant_id, user_id, "
                    "snapshot_date, total_asset) VALUES (:t, :u, :d, :eq)"
                ),
                {
                    "t": _TEST_TENANT,
                    "u": _TEST_UID,  # 模拟侧归一整型键形
                    "d": day,
                    "eq": 1000000.0 + i * 1000.0,
                },
            )
            await session.execute(
                text(
                    "INSERT INTO real_account_ledger_daily_snapshots "
                    "(tenant_id, user_id, account_id, snapshot_date, total_asset) "
                    "VALUES (:t, :u, :a, :d, :eq)"
                ),
                {
                    "t": _TEST_TENANT,
                    "u": _REAL_UID_PADDED,  # 实盘侧补零键形（归一连接正是本契约）
                    "a": _TEST_ACCOUNT_ID,
                    "d": day,
                    "eq": 1000000.0 + i * 500.0,
                },
            )
        await session.commit()

    return {
        "order_id": str(order_id),
        "sim_cid": sim_cid,
        "date_str": today.strftime("%Y%m%d"),
    }


@pytest.mark.asyncio
async def test_shadow_compare_e2e_pair_and_report():
    """真库 E2E：配对 → 日报数值（60bps 价格偏差 / 成交率 1 / 跟踪误差可算）→ 落 Redis。"""
    try:
        from backend.shared.database_manager_v2 import get_session  # noqa: F401
        from sqlalchemy import text  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"依赖不可用: {exc}")

    await _cleanup()
    try:
        fixture = await _seed_fixture()

        from backend.services.trade.services.shadow_compare_service import (
            collect_day_pairs,
            run_shadow_compare,
        )

        pairing = await collect_day_pairs(fixture["date_str"], tenant_id=_TEST_TENANT)
        assert len(pairing["pairs"]) == 1, pairing
        pair = pairing["pairs"][0]
        assert pair["sim_cid"] == fixture["sim_cid"]
        assert pair["real_price"] == pytest.approx(10.06)
        assert pair["sim_price"] == pytest.approx(10.0)
        assert pairing["sim_only"] == [] and pairing["real_only"] == []

        redis = _fake_redis()
        report = await run_shadow_compare(
            redis, fixture["date_str"], tenant_id=_TEST_TENANT
        )
        assert report["coverage"]["matched"] == 1
        assert report["coverage"]["symbol_side_mismatch"] == 0
        # 买入成交价偏差 = (10.06-10.0)/10 = 60 bps
        assert report["price_deviation"]["n"] == 1
        assert report["price_deviation"]["mean_bps"] == pytest.approx(60.0, abs=0.01)
        assert report["fill"]["fill_rate"] == pytest.approx(1.0)
        te = report["tracking_error"][_TEST_UID]
        assert te["sufficient"] is True
        assert te["n_returns"] == 2
        assert te["mean_diff_bps"] is not None

        saved = json.loads(redis.client.get(f"mirror:shadow:{fixture['date_str']}"))
        assert saved["coverage"]["matched"] == 1
        assert saved["ok"] is True
    finally:
        await _cleanup()


@pytest.mark.asyncio
async def test_shadow_compare_e2e_empty_day_writes_evidence():
    """无任何配对时也要落"跑过且为空"的证据（与对账 clean 行同一原则）。"""
    try:
        from backend.shared.database_manager_v2 import get_session  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"依赖不可用: {exc}")

    await _cleanup()
    try:
        from backend.services.trade.services.shadow_compare_service import (
            run_shadow_compare,
        )

        redis = _fake_redis()
        report = await run_shadow_compare(redis, "20260101", tenant_id=_TEST_TENANT)
        assert report["coverage"]["matched"] == 0
        assert report["ok"] is True
        assert report["tracking_error"]["sufficient"] is False
        assert redis.client.get("mirror:shadow:20260101") is not None
    finally:
        await _cleanup()


@pytest.mark.asyncio
async def test_write_clean_row_e2e_and_dedup():
    """T-P2-06：对账零差异证据行真库落盘 + 进程内当日去重（C06 消费口径 diff=0）。"""
    try:
        from sqlalchemy import text

        from backend.shared.database_manager_v2 import get_session
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"依赖不可用: {exc}")

    import backend.services.simulation.services.reconcile_service as recon

    await _cleanup()
    recon._clean_marked.clear()
    try:
        await recon._write_clean_row(
            tenant_id=_TEST_TENANT, user_id=_TEST_UID, market="CN"
        )
        await recon._write_clean_row(
            tenant_id=_TEST_TENANT, user_id=_TEST_UID, market="CN"
        )  # 去重：不写第二行
        async with get_session(read_only=True) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT count(*) AS c FROM simulation_reconcile_reports "
                        "WHERE tenant_id=:t"
                    ),
                    {"t": _TEST_TENANT},
                )
            ).one()
        assert int(row.c) == 1
        async with get_session(read_only=True) as session:
            detail = (
                await session.execute(
                    text(
                        "SELECT field, diff, market FROM simulation_reconcile_reports "
                        "WHERE tenant_id=:t LIMIT 1"
                    ),
                    {"t": _TEST_TENANT},
                )
            ).one()
        assert detail.field == "clean"
        assert float(detail.diff) == 0.0
        assert detail.market == "CN"
    finally:
        recon._clean_marked.clear()
        await _cleanup()
