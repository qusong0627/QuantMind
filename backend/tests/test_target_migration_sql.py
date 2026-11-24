"""目标注册表迁移 SQL：静态门 + 可选真库集成测试。

静态门随单测常跑；真库用例需要 `QM_TEST_PG_DSN`（例如
`postgresql://postgres:postgres@127.0.0.1:55432/postgres`），未设置时 skip。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SQL_PATH = ROOT / "backend" / "shared" / "db_migrations" / "upgrade_v1.1.9.sql"


def _sql_text() -> str:
    return SQL_PATH.read_text(encoding="utf-8")


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    return re.sub(r"--[^\n]*", " ", text)


def test_migration_file_exists_and_has_required_objects() -> None:
    text = _sql_text()
    assert "CREATE TABLE IF NOT EXISTS qm_label_targets" in text
    assert "CREATE TABLE IF NOT EXISTS qm_label_target_audit" in text
    assert "CREATE OR REPLACE FUNCTION qm_label_targets_guard" in text
    assert "CREATE TRIGGER trg_qm_label_targets_guard" in text
    assert "BEFORE UPDATE OR DELETE" in text
    assert "status IN ('draft', 'active', 'deprecated')" in text
    assert "spec_hash ~ '^[0-9a-f]{64}$'" in text


def test_migration_is_non_destructive_for_startup_runner() -> None:
    """must pass backend/main_oss.py::_is_destructive_sql（自动迁移硬约束）。"""
    cleaned = _strip_comments(_sql_text())
    forbidden = re.compile(
        r"\b(?:DROP\s+(?:TABLE|SCHEMA|DATABASE)|TRUNCATE|DELETE\s+FROM|DROP\s+COLUMN)\b",
        re.IGNORECASE,
    )
    assert not forbidden.search(cleaned), forbidden.search(cleaned).group(0)


def test_migration_is_idempotent_shape() -> None:
    text = _sql_text()
    # 表/索引全部 IF NOT EXISTS；触发器先 DROP IF EXISTS 再建
    assert text.count("CREATE TABLE IF NOT EXISTS") == 2
    assert text.count("CREATE INDEX IF NOT EXISTS") == 2
    assert "DROP TRIGGER IF EXISTS trg_qm_label_targets_guard" in text


def test_immutability_guard_covers_identity_and_spec() -> None:
    text = _sql_text()
    for col in ("id", "version", "spec", "spec_hash", "market", "family", "mode"):
        assert f"NEW.{col}" in text, col
    assert "qm_label_targets is append-only" in text
    assert "invalid target status transition" in text


def test_upgrade_runner_includes_backend_migrations_dir() -> None:
    """新迁移随 backend 包发布（不依赖 data/ 挂载）；启动扫描必须覆盖。"""
    from backend.main_oss import _upgrade_sql_files

    paths = _upgrade_sql_files()
    assert any(
        p.replace("\\", "/").endswith(
            "backend/shared/db_migrations/upgrade_v1.1.9.sql"
        )
        for p in paths
    )


def test_upgrade_runner_dedupes_same_filename(tmp_path, monkeypatch) -> None:
    """同名脚本只执行一份（env 目录优先，避免双跑/双维护）。"""
    from backend.main_oss import _upgrade_sql_files

    (tmp_path / "upgrade_v1.1.9.sql").write_text(
        "CREATE TABLE IF NOT EXISTS unit_dedupe (id INT);", encoding="utf-8"
    )
    monkeypatch.setenv("QM_UPGRADE_SQL_DIR", str(tmp_path))
    paths = _upgrade_sql_files()
    names = [Path(p).name for p in paths]
    assert names.count("upgrade_v1.1.9.sql") == 1
    assert str(tmp_path / "upgrade_v1.1.9.sql") in paths


# ── 真库集成（可选）──────────────────────────────────────────────────────────


@pytest.mark.integration
def test_migration_twice_and_trigger_enforces_immutability() -> None:
    dsn = os.getenv("QM_TEST_PG_DSN")
    if not dsn:
        pytest.skip("QM_TEST_PG_DSN not set")
    psycopg2 = pytest.importorskip("psycopg2")
    sql = _sql_text()
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(sql)  # 第一次
            cur.execute(sql)  # 第二次（幂等）
            cur.execute(
                """
                INSERT INTO qm_label_targets
                    (id, version, market, family, mode, spec, spec_hash,
                     status, created_by, activated_by, activated_at)
                VALUES
                    ('it_target', 1, 'CN', 'forward_return', 'regression',
                     '{"target_id":"it_target","version":1}'::jsonb,
                     repeat('a', 64), 'active', 'it', 'it', now())
                """
            )
            # 改 spec → 必须被触发器拒绝
            with pytest.raises(Exception) as exc:
                cur.execute(
                    "UPDATE qm_label_targets SET spec='{}'::jsonb "
                    "WHERE id='it_target' AND version=1"
                )
            assert "immutable" in str(exc.value)
            # 非法状态跳转 → 拒绝
            with pytest.raises(Exception) as exc2:
                cur.execute(
                    "UPDATE qm_label_targets SET status='draft' "
                    "WHERE id='it_target' AND version=1"
                )
            assert "status transition" in str(exc2.value)
            # DELETE → 拒绝
            with pytest.raises(Exception) as exc3:
                cur.execute(
                    "DELETE FROM qm_label_targets WHERE id='it_target' AND version=1"
                )
            assert "append-only" in str(exc3.value)
    finally:
        # 清理测试行（触发器禁止 DELETE，先临时禁用触发器再删）
        with conn.cursor() as cur:
            cur.execute(
                "ALTER TABLE qm_label_targets DISABLE TRIGGER trg_qm_label_targets_guard"
            )
            cur.execute(
                "DELETE FROM qm_label_targets WHERE id='it_target' AND version=1"
            )
            cur.execute(
                "ALTER TABLE qm_label_targets ENABLE TRIGGER trg_qm_label_targets_guard"
            )
        conn.close()
