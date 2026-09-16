"""T-P0-03 遗留收口测试：runner 专用只读 DB 账号。

覆盖：
1. 纯函数：口令派生确定性/显式覆盖、角色名白名单、DATABASE_URL 认证段重写、字面量转义；
2. 真库 E2E：角色供给（幂等）→ 以只读角色连接 → SELECT 通过、**写操作全部被拒**
   （INSERT/UPDATE/CREATE TABLE），角色属性（LOGIN 且非 SUPERUSER/NOCREATEDB）；
3. executor 接线：只读凭据替换 DB_USER/DB_PASSWORD + DATABASE_URL 同源重写；
   供给失败回落主凭据（显式告警路径）；启动点使用统一入口（源守卫）；
4. 体检 C11 判定矩阵。
"""

from __future__ import annotations

from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]


# ── 纯函数 ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_runner_password_derivation(monkeypatch):
    from backend.shared.runner_db_account import (
        runner_db_password,
        runner_db_role_name,
    )

    monkeypatch.setenv("DB_PASSWORD", "master-secret")
    monkeypatch.delenv("RUNNER_DB_PASSWORD", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    pw1 = runner_db_password()
    assert pw1 == runner_db_password()  # 确定性
    assert len(pw1) == 24 and pw1 != "master-secret"

    monkeypatch.setenv("DB_PASSWORD", "other-secret")
    assert runner_db_password() != pw1  # 主口令轮换 → 派生随动（供给时 ALTER 同步）

    monkeypatch.setenv("RUNNER_DB_PASSWORD", "explicit-pw")
    assert runner_db_password() == "explicit-pw"

    monkeypatch.delenv("DB_PASSWORD", raising=False)
    monkeypatch.delenv("RUNNER_DB_PASSWORD", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://quantmind:url-pw@db:5432/qm")
    pw_from_url = runner_db_password()
    monkeypatch.setenv("DB_PASSWORD", "url-pw")
    assert runner_db_password() == pw_from_url  # URL 口令段兜底与显式同源

    # 角色名白名单：非法值回退默认
    monkeypatch.setenv("RUNNER_DB_USER", "Bad-Name; DROP")
    assert runner_db_role_name() == "qm_runner_ro"
    monkeypatch.setenv("RUNNER_DB_USER", "custom_ro")
    assert runner_db_role_name() == "custom_ro"


@pytest.mark.unit
def test_rewrite_database_url_for_runner():
    from backend.shared.runner_db_account import rewrite_database_url_for_runner

    out = rewrite_database_url_for_runner(
        "postgresql+asyncpg://quantmind:masterpw@db:5432/quantmind",
        "qm_runner_ro",
        "runnerpw",
    )
    assert out == "postgresql+asyncpg://qm_runner_ro:runnerpw@db:5432/quantmind"
    # 查询串/库名保留
    out2 = rewrite_database_url_for_runner(
        "postgresql://u:p@h:5433/qm?sslmode=require", "ro", "p@ss word"
    )
    assert out2 is not None and "ro:p%40ss%20word@h:5433" in out2 and "sslmode=require" in out2
    # 不可解析 → None（调用方移除 DATABASE_URL，宁可不带主凭据下沉）
    assert rewrite_database_url_for_runner("", "r", "p") is None
    assert rewrite_database_url_for_runner("not-a-url", "r", "p") is None


@pytest.mark.unit
def test_sql_literal_escaping():
    from backend.shared.runner_db_account import _sql_literal

    assert _sql_literal("abc") == "'abc'"
    assert _sql_literal("a'b") == "'a''b'"


# ── 真库 E2E：供给 + 最小权限实证 ───────────────────────────────────


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


@pytest.mark.asyncio
async def test_runner_ro_role_e2e_real_db(monkeypatch):
    """真库 E2E：供给幂等 → 只读连接 SELECT 通、INSERT/UPDATE/CREATE 全拒 → 角色属性合规。"""
    await _ensure_db_pool()
    from urllib.parse import urlsplit

    from sqlalchemy import text as sa_text
    from sqlalchemy.ext.asyncio import create_async_engine

    from backend.shared.database_manager_v2 import close_database, get_session
    from backend.shared.runner_db_account import (
        ensure_runner_db_role_async,
        runner_db_password,
        runner_db_role_name,
    )

    assert await ensure_runner_db_role_async() is True
    assert await ensure_runner_db_role_async() is True  # 幂等

    role = runner_db_role_name()
    # 角色属性：LOGIN 且非 SUPERUSER/NOCREATEDB/NOCREATEROLE/无继承
    async with get_session(read_only=True) as session:
        row = (
            await session.execute(
                sa_text(
                    "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolinherit "
                    "FROM pg_roles WHERE rolname = :r"
                ),
                {"r": role},
            )
        ).fetchone()
    assert row is not None, "只读角色不存在"
    assert row[0] is True and row[1] is False and row[2] is False and row[3] is False
    assert row[4] is False  # NOINHERIT

    # 以只读角色连接（宿主机/容器内 DATABASE_URL 的主机端口库名保持一致）
    import os

    parts = urlsplit(str(os.getenv("DATABASE_URL", "postgresql+asyncpg://x@db:5432/quantmind")))
    host = parts.hostname or "db"
    port = parts.port or 5432
    dbname = (parts.path or "/quantmind").lstrip("/") or "quantmind"
    dsn = f"postgresql+asyncpg://{role}:{runner_db_password()}@{host}:{port}/{dbname}"
    ro_engine = create_async_engine(dsn, pool_pre_ping=False)
    try:
        async with ro_engine.connect() as conn:
            assert (await conn.execute(sa_text("SELECT 1"))).scalar_one() == 1
            # SELECT 面：核心表可读
            await conn.execute(
                sa_text("SELECT count(*) FROM simulation_fund_snapshots")
            )
            # 写面：全部必须被拒（permission denied）
            for sql, params in (
                (
                    "INSERT INTO simulation_fund_snapshots "
                    "(tenant_id, user_id, snapshot_date, market, total_asset) "
                    "VALUES ('x','x', CURRENT_DATE, 'CN', 0)",
                    {},
                ),
                (
                    "UPDATE simulation_fund_snapshots SET total_asset = 0 "
                    "WHERE tenant_id = 'RO-E2E-NOPE'",
                    {},
                ),
                ("CREATE TABLE _ro_e2e_probe (id int)", {}),
            ):
                with pytest.raises(Exception) as exc:  # noqa: PT011 - asyncpg 异常族
                    await conn.execute(sa_text(sql), params)
                assert "permission denied" in str(exc.value).lower(), (sql, exc.value)
                await conn.rollback()  # 事务被拒后必须回滚，否则后续语句只见 aborted 态
    finally:
        await ro_engine.dispose()
        await close_database()


# ── executor 接线 ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_executor_env_uses_read_only_credentials(monkeypatch):
    import backend.shared.runner_db_account as rdb
    from backend.services.engine.routers.ai_ide import executor

    monkeypatch.setenv("DB_USER", "quantmind")
    monkeypatch.setenv("DB_PASSWORD", "masterpw")
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+asyncpg://quantmind:masterpw@db:5432/quantmind"
    )

    async def _fake_overrides():
        return {"DB_USER": "qm_runner_ro", "DB_PASSWORD": "runner-pw"}

    monkeypatch.setattr(rdb, "get_runner_db_env_overrides_async", _fake_overrides)
    env = await executor._runner_environment_with_least_privilege("1", {})
    assert env["DB_USER"] == "qm_runner_ro"
    assert env["DB_PASSWORD"] == "runner-pw"
    assert "qm_runner_ro:runner-pw@" in env["DATABASE_URL"]
    assert "masterpw" not in env["DATABASE_URL"]


@pytest.mark.asyncio
async def test_executor_env_falls_back_with_loud_warning(monkeypatch):
    import backend.shared.runner_db_account as rdb
    from backend.services.engine.routers.ai_ide import executor

    monkeypatch.setenv("DB_USER", "quantmind")
    monkeypatch.setenv("DB_PASSWORD", "masterpw")
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+asyncpg://quantmind:masterpw@db:5432/quantmind"
    )

    async def _no_ro():
        return None

    monkeypatch.setattr(rdb, "get_runner_db_env_overrides_async", _no_ro)
    env = await executor._runner_environment_with_least_privilege("1", {})
    # 回落主凭据（记录在案的降级路径）——行为与历史一致，但有 ERROR 级告警
    assert env["DB_USER"] == "quantmind"
    assert "masterpw" in env["DATABASE_URL"]


@pytest.mark.unit
def test_runner_ro_wiring_source_guards():
    executor_src = (
        _BACKEND / "services/engine/routers/ai_ide/executor.py"
    ).read_text(encoding="utf-8")
    # 启动点必须走统一入口（禁止裸调同步构造器丢最小权限逻辑）
    assert "runner_env = await _runner_environment_with_least_privilege(" in executor_src
    assert "environment=runner_env," in executor_src
    assert "回落为**主库凭据**" in executor_src  # 降级告警不可静默
    assert "rewrite_database_url_for_runner" in executor_src

    module_src = (_BACKEND / "shared/runner_db_account.py").read_text(encoding="utf-8")
    assert "NOSUPERUSER" in module_src and "NOINHERIT" in module_src
    assert "GRANT SELECT ON ALL TABLES" in module_src
    assert "ALTER DEFAULT PRIVILEGES" in module_src
    assert "lock_timeout" in module_src

    health_src = (_BACKEND / "scripts/diagnose/health.py").read_text(encoding="utf-8")
    assert '("C11", "runner 只读 DB", check_c11_runner_db_role)' in health_src


@pytest.mark.unit
def test_c11_classification_matrix():
    from backend.scripts.diagnose.health import classify_runner_db_privileges

    assert classify_runner_db_privileges(False, False, False)[0] == "fail"
    assert classify_runner_db_privileges(True, True, True)[0] == "fail"  # 可写=过宽
    assert classify_runner_db_privileges(True, False, False)[0] == "warn"  # 读不了
    level, detail = classify_runner_db_privileges(True, True, False)
    assert level == "ok" and "SELECT-only" in detail
