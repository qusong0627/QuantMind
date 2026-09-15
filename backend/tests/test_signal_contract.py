"""T-P1-01 测试：Signal 契约列 + 截面分位口径。

覆盖：
1. compute_rank_pct 纯函数（升/降序、并列、单值、空、NaN/非有限值）；
2. 与 PG percent_rank() 口径一致性（VALUES 只读对照，DB 不可用则跳过）；
3. 两个写入端 SQL 含契约列（源断言，防重构漏写）；
4. 迁移 ALTER 幂等形态（IF NOT EXISTS）与 db_init.sql 同步。
"""

import os
from pathlib import Path

import pytest

from backend.shared.signal_contract import (
    ALTER_TEMPLATE,
    CONTRACT_COLUMNS,
    compute_rank_pct,
    normalize_market,
)

_BACKEND = Path(__file__).resolve().parents[1]


# --- 纯函数 ---------------------------------------------------------------


def test_rank_pct_ascending_distinct():
    assert compute_rank_pct([1.0, 2.0, 3.0, 4.0]) == [0.0, 1 / 3, 2 / 3, 1.0]


def test_rank_pct_order_independent():
    # 输入乱序，结果按位置返回
    pcts = compute_rank_pct([30.0, 10.0, 20.0])
    assert pcts == [1.0, 0.0, 0.5]


def test_rank_pct_ties_take_min_rank():
    # 并列取最小名次（与 PG rank()/percent_rank() 一致）
    pcts = compute_rank_pct([10.0, 20.0, 20.0, 30.0])
    assert pcts == [0.0, 1 / 3, 1 / 3, 1.0]


def test_rank_pct_single_value_is_zero():
    assert compute_rank_pct([5.0]) == [0.0]


def test_rank_pct_empty():
    assert compute_rank_pct([]) == []


def test_rank_pct_non_finite_returns_none():
    """非有限值（None/NaN）位置返回 None；有限值按 n-1 分母计算（n 计全部输入）。

    注：混合非有限值时分母口径由实现显式定义（非 PG NULL 语义的直接映射）——
    生产路径（fusion_score NOT NULL 且有限）不受影响。
    """
    pcts = compute_rank_pct([1.0, float("nan"), 3.0, None])
    assert pcts[0] == 0.0
    assert pcts[1] is None and pcts[3] is None
    assert pcts[2] == pytest.approx(1 / 3)


def test_normalize_market():
    assert normalize_market(None) == "CN"
    assert normalize_market("") == "CN"
    assert normalize_market("A") == "CN"
    assert normalize_market("hk") == "HK"
    assert normalize_market("FUTURES") == "FUTURES"


# --- PG 口径一致性（只读，DB 不可用则跳过）----------------------------------


def test_rank_pct_matches_pg_percent_rank():
    try:
        from sqlalchemy import create_engine, text

        db_url = os.environ.get("DATABASE_URL", "")
        if "+asyncpg" in db_url:
            db_url = db_url.replace("+asyncpg", "+psycopg2")
        if not db_url.startswith("postgresql"):
            pytest.skip("无 DATABASE_URL，跳过 PG 口径对照")
        engine = create_engine(db_url, pool_pre_ping=True, future=True)
        values = [10.0, 20.0, 20.0, 35.5, 7.0]
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT v, percent_rank() OVER (ORDER BY v) AS pct "
                    "FROM unnest(CAST(:arr AS double precision[])) AS t(v)"
                ),
                {"arr": values},
            ).all()
    except Exception as exc:  # 连接失败等 → 跳过（容器外无 DB）
        pytest.skip(f"PG 不可用: {exc}")

    pg_by_value = {float(r.v): float(r.pct) for r in rows}
    py = compute_rank_pct(values)
    for value, pct in zip(values, py, strict=True):
        assert pct == pytest.approx(pg_by_value[value]), f"值与 PG percent_rank 不一致: {value}"


# --- 写入端与迁移源断言 ------------------------------------------------------


def test_main_writer_includes_contract_columns():
    src = (_BACKEND / "services/engine/inference/script_runner.py").read_text(encoding="utf-8")
    assert "market, rank_pct, source, signal_ts" in src
    assert ":market, :rank_pct, :source" in src
    assert "ensure_signal_contract_columns(db)" in src
    assert "compute_rank_pct(scores)" in src


def test_realtime_writer_includes_contract_columns():
    src = (_BACKEND / "services/engine/routers/realtime_contract.py").read_text(encoding="utf-8")
    assert "rank_pct" in src and "SOURCE_REALTIME" in src
    assert "ensure_signal_contract_columns_async()" in src
    assert '"market": normalize_market(item.market or item.universe_tag)' in src


def test_migration_is_idempotent_and_db_init_in_sync():
    assert "ADD COLUMN IF NOT EXISTS" in ALTER_TEMPLATE
    names = {name for name, _ in CONTRACT_COLUMNS}
    assert names == {"market", "rank_pct", "source", "signal_ts"}
    ddl = (_BACKEND / "shared/db_init.sql").read_text(encoding="utf-8")
    for name in names:
        assert name in ddl, f"db_init.sql 缺少契约列 {name}"
