"""T-P0-07 体检脚本单测：纯判定函数 + 假上下文驱动的代表性检查。

假上下文（FakeCtx）只实现 query / redis_get / redis_scan 三个注入点，
与 HealthContext 鸭子类型一致——检查函数因此可在不连 DB/Redis 下单测。
"""

import pytest

from backend.scripts.diagnose.health import (
    classify_account_key_forms,
    classify_cid_duplicates,
    classify_ledger_writes,
    classify_local_market_data,
    classify_signal_distribution,
    classify_snapshot_consistency,
    check_c03_account_key_consistency,
    check_c04_snapshot_consistency,
    check_c05_ledger_writes,
    check_c12_local_market_data,
    exit_code,
    summarize,
)


class FakeCtx:
    """注入式上下文假实现：按 SQL 子串给行，按 key 给值。"""

    def __init__(self, rows_by_sql=None, keys=None, values=None):
        self._rows = rows_by_sql or {}
        self._keys = keys or []
        self._values = values or {}

    def query(self, sql, **params):
        for needle, rows in self._rows.items():
            if needle in sql:
                return rows
        return []

    def redis_get(self, key, db):
        return self._values.get(key)

    def redis_scan(self, pattern, db):
        return list(self._keys)


# --- 纯判定 ---------------------------------------------------------------


def test_signal_distribution_empty_is_fail():
    assert classify_signal_distribution({}).level == "fail"


def test_signal_distribution_all_hold_is_fail():
    r = classify_signal_distribution({"HOLD": 5190})
    assert r.level == "fail"
    assert "全 HOLD" in r.detail
    assert "SIGNAL-GATE" in r.suggestion


def test_signal_distribution_collapse_is_warn():
    r = classify_signal_distribution({"BUY": 5000, "SELL": 100, "HOLD": 90})
    assert r.level == "warn"


def test_signal_distribution_normal_is_ok():
    r = classify_signal_distribution({"BUY": 1040, "SELL": 843, "HOLD": 3307})
    assert r.level == "ok"


def test_account_key_forms_single_ok_multi_fail():
    assert classify_account_key_forms({"1"}, "1").level == "ok"
    r = classify_account_key_forms({"1", "00000001"}, "1")
    assert r.level == "fail"
    assert "00000001" in r.detail


def test_snapshot_consistency_within_tolerance_ok():
    assert classify_snapshot_consistency(100.0, 100.5).level == "ok"


def test_snapshot_consistency_mismatch_fail():
    assert classify_snapshot_consistency(1_008_379.58, 2_008_379.58).level == "fail"


def test_ledger_writes_classification():
    assert classify_ledger_writes(0, 0).level == "ok"
    r = classify_ledger_writes(0, 3)
    assert r.level == "warn" and "T-P1-04" in r.suggestion
    assert classify_ledger_writes(5, 3).level == "ok"


def test_cid_duplicates_scan():
    """T-P2-06：幂等键重复扫描（无重复 ok；有重复 fail 且点名）。"""
    ok = classify_cid_duplicates([])
    assert ok.level == "ok" and ok.metrics == {"cid_dup_groups": 0}
    dup = classify_cid_duplicates(
        [
            {"tenant_id": "default", "user_id": 1, "client_order_id": "sim-r-600036.SH-buy"},
            {"tenant_id": "default", "user_id": 1, "client_order_id": "sim-r2-000001.SZ-sell"},
        ]
    )
    assert dup.level == "fail"
    assert "2 组" in dup.detail
    assert "sim_orders" in dup.detail  # 默认走模拟台账，消息与历史一致
    assert "sim-r-600036.SH-buy" in dup.detail


def test_cid_duplicates_scan_covers_real_orders_table():
    """P2.7-⑧：实盘台账（orders）同口径扫描 —— 重复既是幂等失守，也是迁移停手的原因。

    只测判定函数的 ``table`` 参数会让「C05 真的扫了 orders」漏掉，故连查询一起钉：
    ``check_c05`` 里必须出现 orders 的重复扫描 SQL。
    """
    dup = classify_cid_duplicates(
        [
            {
                "tenant_id": "default",
                "user_id": 10000001,
                "client_order_id": "lld-r-600036.SH-sell",
            }
        ],
        table="orders",
    )
    assert dup.level == "fail"
    assert dup.detail.startswith("orders 幂等键重复")

    import inspect

    from backend.scripts.diagnose import health

    src = inspect.getsource(health.check_c05_ledger_writes)
    assert "FROM orders " in src and "HAVING count(*) > 1" in src
    assert 'classify_cid_duplicates(real_dup_rows or [], table="orders")' in src


def test_summary_and_exit_code():
    results = [
        classify_signal_distribution({"BUY": 1, "SELL": 1}),
        classify_ledger_writes(0, 1),
        classify_signal_distribution({}),
    ]
    counts = summarize(results)
    assert counts == {"ok": 1, "warn": 1, "fail": 1}
    assert exit_code(results) == 1
    assert exit_code([classify_ledger_writes(1, 0)]) == 0


# --- 假上下文驱动的检查 -----------------------------------------------------


@pytest.mark.asyncio
async def test_c03_detects_dual_key_forms():
    ctx = FakeCtx(
        keys=[
            "simulation:account:default:1",
            "simulation:account:default:00000001",
            "simulation:settings:default:1",  # 非账户键应被忽略
        ]
    )
    r = await check_c03_account_key_consistency(ctx)
    assert r.level == "fail"


@pytest.mark.asyncio
async def test_c03_single_form_ok():
    ctx = FakeCtx(keys=["simulation:account:default:1", "simulation:account:default:1:FUTURES"])
    r = await check_c03_account_key_consistency(ctx)
    assert r.level == "ok"


@pytest.mark.asyncio
async def test_c04_multimarket_sum_matches_snapshot():
    """集成口径：CN + FUTURES 两账户求和应与用户级快照一致（修 +100 万后）。"""
    import json

    ctx = FakeCtx(
        keys=["simulation:account:default:1", "simulation:account:default:1:FUTURES"],
        values={
            "simulation:account:default:1": json.dumps({"total_asset": 1_008_379.58}),
            "simulation:account:default:1:FUTURES": json.dumps({"total_asset": 1_000_000.0}),
        },
        rows_by_sql={
            "simulation_fund_snapshots": [
                {"tenant_id": "default", "user_id": "1", "total_asset": 2_008_379.58}
            ]
        },
    )
    r = await check_c04_snapshot_consistency(ctx)
    assert r.level == "ok"


@pytest.mark.asyncio
async def test_c04_mismatch_detected():
    import json

    ctx = FakeCtx(
        keys=["simulation:account:default:1"],
        values={"simulation:account:default:1": json.dumps({"total_asset": 100.0})},
        rows_by_sql={
            "simulation_fund_snapshots": [
                {"tenant_id": "default", "user_id": "1", "total_asset": 999999.0}
            ]
        },
    )
    r = await check_c04_snapshot_consistency(ctx)
    assert r.level == "fail"


@pytest.mark.asyncio
async def test_c05_warns_when_ledger_empty_but_positions_exist():
    import json

    ctx = FakeCtx(
        keys=["simulation:account:default:1"],
        values={
            "simulation:account:default:1": json.dumps(
                {"positions": {"600036.SH": {"volume": 100}}}
            )
        },
        rows_by_sql={"sim_trades": [{"trades": 0}]},
    )
    r = await check_c05_ledger_writes(ctx)
    assert r.level == "warn"


# --- C12 本地行情数据 ------------------------------------------------------


def test_c12_all_markets_available_is_ok():
    r = classify_local_market_data({"CN": "2026-09-16", "HK": "2026-09-16"}, {})
    assert r.level == "ok"
    assert "2026-09-16" in r.detail


def test_c12_partial_missing_is_warn():
    r = classify_local_market_data(
        {"CN": "2026-09-16"}, {"HK": "HK 日线数据集目录不存在: /data/quanthk/..."}
    )
    assert r.level == "warn"
    assert "HK 日线数据集目录不存在" in r.detail
    assert "CN" in r.detail


def test_c12_nothing_available_is_fail():
    r = classify_local_market_data(
        {}, {"CN": "CN 日线数据集目录不存在: /data/quantdb/1_kline_data/daily_unadjusted"}
    )
    assert r.level == "fail"
    assert "模拟盘撮合" in r.suggestion


@pytest.mark.asyncio
async def test_c12_check_reports_missing_market_with_reason(monkeypatch):
    from datetime import date

    import backend.services.engine.data_platform.quantbc_hub as quantbc_hub
    import backend.services.simulation.services.local_market_data as local_market_data
    from backend.services.simulation.services.market_rules import Market

    class _FakeMarketData:
        def __init__(self, latest, reason=None):
            self._latest = latest
            self._reason = reason

        def latest_trade_date(self):
            return self._latest

        def data_unavailable_reason(self):
            return self._reason

    def _fake_get(market=None):
        if market is Market.CN:
            return _FakeMarketData(date(2026, 9, 16))
        return _FakeMarketData(None, f"{market.value} 日线数据集目录不存在: /data/x")

    monkeypatch.setattr(quantbc_hub, "_crypto_enabled", lambda: False)
    monkeypatch.setattr(local_market_data, "get_local_market_data", _fake_get)

    r = await check_c12_local_market_data(FakeCtx())

    assert r.level == "warn"
    assert "CN" in r.detail and "HK" in r.detail
