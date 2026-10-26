"""runner 专用只读 DB 账号（T-P0-03 遗留收口）。

背景：AI-IDE 用户代码容器（runner）此前透传**主库凭据**（DB_USER/DB_PASSWORD +
含主凭据的 DATABASE_URL）——用户代码等价于持有全权 DB 面。本模块提供最小权限
账号 `qm_runner_ro` 的幂等供给与凭据派生：

- 权限：LOGIN / NOSUPERUSER / NOCREATEDB / NOCREATEROLE / NOINHERIT +
  CONNECT（本库）+ USAGE（public）+ **SELECT ON ALL TABLES** + DEFAULT PRIVILEGES
  （后续新表自动只读）——写操作一律 permission denied（fail-loud，不再静默越权）；
- 口令：`RUNNER_DB_PASSWORD` 显式覆盖优先；否则由主库口令派生
  `sha256(f"{DB_PASSWORD}:qm-runner-ro:v1")[:24]`（确定性、无新增配置；主口令轮换后
  每次供给会同步 ALTER ROLE，不会失联）；
- 幂等：进程内缓存 + 服务端 DO 块（存在则 ALTER、不存在则 CREATE），可重复调用；
- 失败不阻断：供给失败返回 None（调用方按"退回主凭据并显式告警"处理，记录在案），
  体检 C11 可独立观测角色与权限面。

标识符安全：角色名经白名单正则校验后用于 %I 位；口令经单引号转义后用于 %L 位
（不拼接未经校验的外部串）。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re

logger = logging.getLogger(__name__)

DEFAULT_RUNNER_DB_USER = "qm_runner_ro"
# 模块型回测 runner 专用写角色：只允许写回测历史/体检两张表，
# 普通 AI-IDE 用户代码仍用 qm_runner_ro（SELECT-only）。
DEFAULT_BACKTEST_DB_USER = "qm_backtest_rw"
_ROLE_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

_ensured = False
_backtest_ensured = False


def _master_db_password() -> str:
    master = str(os.getenv("DB_PASSWORD", "")).strip()
    if not master:
        master = _password_from_database_url() or ""
    return master


def runner_db_role_name() -> str:
    name = str(os.getenv("RUNNER_DB_USER", "")).strip().lower() or DEFAULT_RUNNER_DB_USER
    if not _ROLE_RE.match(name):
        logger.warning("[RunnerDB] RUNNER_DB_USER 非法（回退默认）: %r", name)
        return DEFAULT_RUNNER_DB_USER
    return name


def runner_db_password() -> str:
    """派生/覆盖 runner 只读口令（确定性：主口令不换则不变）。"""
    explicit = str(os.getenv("RUNNER_DB_PASSWORD", "")).strip()
    if explicit:
        return explicit
    seed = f"{_master_db_password()}:qm-runner-ro:v1"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


def backtest_db_role_name() -> str:
    name = (
        str(os.getenv("BACKTEST_DB_USER", "")).strip().lower()
        or DEFAULT_BACKTEST_DB_USER
    )
    if not _ROLE_RE.match(name):
        logger.warning("[RunnerDB] BACKTEST_DB_USER 非法（回退默认）: %r", name)
        return DEFAULT_BACKTEST_DB_USER
    return name


def backtest_db_password() -> str:
    """派生/覆盖模块回测写角色口令（确定性：主口令不换则不变）。"""
    explicit = str(os.getenv("BACKTEST_DB_PASSWORD", "")).strip()
    if explicit:
        return explicit
    seed = f"{_master_db_password()}:qm-backtest-rw:v1"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


def _password_from_database_url() -> str | None:
    raw = str(os.getenv("DATABASE_URL", "")).strip()
    if not raw or "://" not in raw:
        return None
    try:
        userinfo = raw.split("://", 1)[1].split("@", 1)[0]
        if ":" in userinfo:
            from urllib.parse import unquote

            return unquote(userinfo.split(":", 1)[1])
    except Exception:  # noqa: BLE001
        return None
    return None


def _sql_literal(value: str) -> str:
    """PG 单引号字面量转义（用于 DO 块内层 %L 参数）。"""
    return "'" + str(value).replace("'", "''") + "'"


async def ensure_runner_db_role_async() -> bool:
    """幂等供给只读角色（进程内缓存）。就绪/新建成 True；失败 False（只告警）。"""
    global _ensured
    if _ensured:
        return True
    from sqlalchemy import text as sa_text

    from backend.shared.database_manager_v2 import get_session

    role = runner_db_role_name()
    if not _ROLE_RE.match(role):  # 双保险（runner_db_role_name 已归一）
        return False
    pw_literal = _sql_literal(runner_db_password())
    role_literal = _sql_literal(role)
    ddl = f"""
DO $$
DECLARE
  r text := {role_literal};
  pw text := {pw_literal};
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
    EXECUTE format('ALTER ROLE %I WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD %L', r, pw);
  ELSE
    EXECUTE format('CREATE ROLE %I WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD %L', r, pw);
  END IF;
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), r);
  EXECUTE format('GRANT USAGE ON SCHEMA public TO %I', r);
  EXECUTE format('GRANT SELECT ON ALL TABLES IN SCHEMA public TO %I', r);
  EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO %I', r);
END $$;
"""
    try:
        async with get_session(read_only=False) as session:
            await session.execute(sa_text("SET LOCAL lock_timeout = '3s'"))
            await session.execute(sa_text(ddl))
            await session.commit()
        _ensured = True
        logger.info(
            "[RunnerDB] 只读角色就绪：%s（SELECT-only，runner 容器将使用该凭据）", role
        )
        return True
    except Exception as exc:  # noqa: BLE001 - 供给失败不阻断（调用方退回主凭据并告警）
        logger.warning("[RunnerDB] 只读角色供给失败（不影响主流程）: %s", exc)
        return False


async def get_runner_db_env_overrides_async() -> dict[str, str] | None:
    """供给只读角色并返回 runner 环境覆盖 {DB_USER, DB_PASSWORD}；失败返回 None。"""
    if not await ensure_runner_db_role_async():
        return None
    return {
        "DB_USER": runner_db_role_name(),
        "DB_PASSWORD": runner_db_password(),
    }


async def ensure_backtest_db_role_async() -> bool:
    """幂等供给**模块回测专用写角色**（只写 qlib_backtest_runs / eval_scores）。

    背景（2026-10-26 实测）：Qlib 模块型策略在 runner 容器内走
    ``QlibBacktestService.run_backtest``，会 INSERT/UPDATE/DELETE
    ``qlib_backtest_runs``（并在体检时 upsert ``eval_scores``）；而 runner
    统一使用 SELECT-only 的 ``qm_runner_ro`` → ``permission denied``、回测直接
    退出（ExitCode=2）。本角色只给这两张表的写权限，普通 AI-IDE 用户代码仍走
    只读角色，不扩大其 DB 面。
    """
    global _backtest_ensured
    if _backtest_ensured:
        return True
    from sqlalchemy import text as sa_text

    from backend.shared.database_manager_v2 import get_session

    role = backtest_db_role_name()
    if not _ROLE_RE.match(role):
        return False
    pw_literal = _sql_literal(backtest_db_password())
    role_literal = _sql_literal(role)
    ddl = f"""
DO $$
DECLARE
  r text := {role_literal};
  pw text := {pw_literal};
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
    EXECUTE format('ALTER ROLE %I WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD %L', r, pw);
  ELSE
    EXECUTE format('CREATE ROLE %I WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD %L', r, pw);
  END IF;
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), r);
  EXECUTE format('GRANT USAGE ON SCHEMA public TO %I', r);
  EXECUTE format('GRANT SELECT ON ALL TABLES IN SCHEMA public TO %I', r);
  EXECUTE format('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO %I', r);
  EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO %I', r);
  IF to_regclass('public.qlib_backtest_runs') IS NOT NULL THEN
    EXECUTE format('GRANT INSERT, UPDATE, DELETE ON TABLE public.qlib_backtest_runs TO %I', r);
  END IF;
  IF to_regclass('public.eval_scores') IS NOT NULL THEN
    EXECUTE format('GRANT INSERT, UPDATE ON TABLE public.eval_scores TO %I', r);
  END IF;
  IF to_regclass('public.eval_scores_id_seq') IS NOT NULL THEN
    EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE public.eval_scores_id_seq TO %I', r);
  END IF;
END $$;
"""
    try:
        async with get_session(read_only=False) as session:
            await session.execute(sa_text("SET LOCAL lock_timeout = '3s'"))
            await session.execute(sa_text(ddl))
            await session.commit()
        _backtest_ensured = True
        logger.info(
            "[RunnerDB] 模块回测写角色就绪：%s（仅 qlib_backtest_runs/eval_scores 写）",
            role,
        )
        return True
    except Exception as exc:  # noqa: BLE001 - 供给失败由调用方显式告警
        logger.warning("[RunnerDB] 模块回测写角色供给失败: %s", exc)
        return False


async def get_backtest_db_env_overrides_async() -> dict[str, str] | None:
    """模块回测 runner 的 DB 覆盖；角色不可用返回 None（调用方显式告警）。"""
    if not await ensure_backtest_db_role_async():
        return None
    return {
        "DB_USER": backtest_db_role_name(),
        "DB_PASSWORD": backtest_db_password(),
    }


def rewrite_database_url_for_runner(url: str, user: str, password: str) -> str | None:
    """把 DATABASE_URL 的认证段替换为 runner 只读凭据；无法解析返回 None。

    保留原 scheme（含 +asyncpg 驱动段）/host/port/db 与查询串——只换 user:pass，
    避免"换了 DB_* 变量但 DATABASE_URL 仍带主凭据"的双口径泄漏。
    """
    from urllib.parse import quote, urlsplit, urlunsplit

    raw = str(url or "").strip()
    if not raw or "://" not in raw:
        return None
    try:
        parts = urlsplit(raw)
        if not parts.hostname:
            return None
        new_netloc = f"{quote(user, safe='')}:{quote(password, safe='')}@{parts.hostname}"
        if parts.port:
            new_netloc += f":{parts.port}"
        return urlunsplit((parts.scheme, new_netloc, parts.path, parts.query, parts.fragment))
    except Exception:  # noqa: BLE001
        return None
