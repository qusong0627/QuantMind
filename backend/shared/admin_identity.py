"""管理员身份口径：username='admin'，user_id='10000001'。

历史原因（db_init.sql 曾 seed user_id='admin'，后又纠正为 '00000001'）：
``00000001`` 经 int() 会变成 1，和 admin JWT 落到的模拟账户 0 对不上。
规范 ID 必须是 8 位且不以 0 开头，int(user_id) 与字符串一致。

本模块提供幂等纠正：把字符型 user_id 列中的 'admin' / '00000001'
改为 '10000001'。整数列（strategies.user_id 等存 users.id）不受影响。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text

logger = logging.getLogger(__name__)

ADMIN_USERNAME = "admin"
ADMIN_USER_ID = "10000001"
LEGACY_ADMIN_USER_ID = "admin"
LEGACY_ADMIN_USER_IDS = frozenset({"admin", "00000001"})
# 模拟盘 Redis/PG 曾把 admin 写成 0，把 00000001 写成 1
OSS_ADMIN_SIM_ALIASES = frozenset({"0", "1", "00000001", "10000001", "admin"})


def is_admin_user_id(user_id: object) -> bool:
    from backend.shared.simulation_account_keys import is_admin_sim_user

    return is_admin_sim_user(user_id)


def normalize_admin_user_id(user_id: object) -> str:
    """旧 token / 历史键一律收到 10000001。"""
    if is_admin_user_id(user_id):
        return ADMIN_USER_ID
    return str(user_id or "").strip()


# 指向 users(user_id) 的 FK 约束名（live 库实测）。约束为即时检查：
# 子表先改则子侧校验失败，父表先改则父侧校验失败，故事务内先 drop、
# 改完再原名建回。子表当前均无 admin 行，但约束本身会拦父表更新。
_USER_ID_FKS: tuple[tuple[str, str], ...] = (
    ("user_roles", "user_roles_user_id_fkey"),
    ("identity_verifications", "identity_verifications_user_id_fkey"),
    ("notifications", "notifications_user_id_fkey"),
    ("password_reset_tokens", "password_reset_tokens_user_id_fkey"),
)


async def _drop_user_id_fks(session) -> None:
    """卸掉指向 users(user_id) 的 FK（即时检查，任一顺序直接改都会违约束）。"""
    from sqlalchemy import text as _text

    for table, conname in _USER_ID_FKS:
        try:
            await session.execute(
                _text(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {conname}")
            )
        except Exception as exc:
            logger.warning("卸 FK %s 跳过: %s", conname, str(exc)[:120])


async def _rebuild_user_id_fks(session) -> None:
    """原名建回 FK（与 db_init.sql 一致：plain REFERENCES，NO ACTION）。"""
    from sqlalchemy import text as _text

    for table, conname in _USER_ID_FKS:
        try:
            await session.execute(
                _text(
                    f"ALTER TABLE {table} ADD CONSTRAINT {conname} "
                    "FOREIGN KEY (user_id) REFERENCES users(user_id)"
                )
            )
        except Exception as exc:
            logger.warning("建回 FK %s 跳过: %s", conname, str(exc)[:120])


async def _char_user_id_tables(session) -> list[str]:
    """所有字符型 user_id 列的表名（users 除外，调用方自行追加到末尾）。"""
    from sqlalchemy import text as _text

    tables = (
        (
            await session.execute(
                _text(
                    "SELECT table_name FROM information_schema.columns "
                    "WHERE table_schema='public' AND column_name='user_id' "
                    "AND data_type IN ('character varying', 'character', 'text')"
                )
            )
        )
        .scalars()
        .all()
    )
    return [t for t in sorted(set(tables)) if t != "users"]


async def _sweep_one(session, old: str, new: str) -> dict[str, int]:
    """单映射 old→new：子表先行、users 收尾。调用方保证 FK 已卸、事务未提交。"""
    from sqlalchemy import text as _text

    updated: dict[str, int] = {}
    for table in await _char_user_id_tables(session) + ["users"]:
        try:
            async with session.begin_nested():
                n = (
                    await session.execute(
                        _text(f"UPDATE {table} SET user_id=:new WHERE user_id=:old"),
                        {"new": new, "old": old},
                    )
                ).rowcount or 0
        except Exception as exc:
            logger.warning("纠正 %s 跳过: %s", table, str(exc)[:120])
            continue
        if n:
            updated[table] = n
            logger.info("纠正 %s: %d 行 %s→%s", table, n, old, new)
    return updated


async def migrate_user_ids(
    plan: dict[str, str], dry_run: bool = False
) -> dict[str, Any]:
    """按 {old: new} 批量迁移 user_id（幂等，可重复执行）。

    单事务：卸 FK → 逐映射 sweep → 建回 FK。空 plan 直接返回。
    """
    from backend.shared.database_manager_v2 import get_session

    report: dict[str, Any] = {"updated": {}, "dry_run": dry_run}
    if not plan:
        return report
    async with get_session() as session:
        try:
            # 防锁等待升级为启动期死锁：拿不到 DDL 锁时 30s 快速失败并记日志，
            # 而不是无限等待（曾因调用方事务未提交、两连接互等，服务起不来）。
            await session.execute(text("SET LOCAL lock_timeout = '30s'"))
        except Exception:  # noqa: BLE001 - 设置失败不阻塞主流程
            pass
        if not dry_run:
            await _drop_user_id_fks(session)
        for old, new in plan.items():
            if old == new:
                continue
            for table, n in (await _sweep_one(session, old, new)).items():
                report["updated"][table] = report["updated"].get(table, 0) + n
        if not dry_run:
            await _rebuild_user_id_fks(session)
        if dry_run:
            await session.rollback()
    return report


async def fix_admin_user_id(dry_run: bool = False) -> dict[str, Any]:
    """纠正 admin 的 user_id 为 10000001（幂等，可重复执行）。

    同时收口历史 'admin' 与 '00000001'。
    """
    report = await migrate_user_ids(
        dict.fromkeys(LEGACY_ADMIN_USER_IDS, ADMIN_USER_ID),
        dry_run=dry_run,
    )
    report["users_fixed"] = bool(report["updated"].get("users"))
    return report


async def find_legacy_users() -> list[dict[str, Any]]:
    """找出 user_id 不符合 8 位数字规范的存量用户行。"""
    from sqlalchemy import text as _text

    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=True) as session:
        rows = (
            await session.execute(
                _text(
                    "SELECT id, user_id, username, tenant_id, is_admin FROM users "
                    "WHERE user_id !~ '^[0-9]{8}$' ORDER BY id"
                )
            )
        ).mappings().all()
        return [dict(r) for r in rows]


async def generate_user_id(session, taken: set[str] | None = None) -> str:
    """生成唯一的 8 位数字 user_id（与 auth_service._generate_user_id 同算法）。"""
    import uuid as _uuid

    from sqlalchemy import text as _text

    taken = taken or set()
    reserved = {ADMIN_USER_ID, *LEGACY_ADMIN_USER_IDS, "00000000"}
    for _ in range(50):
        # 10000000-99999999：8 位且不以 0 开头，避免 int(user_id) 丢掉前导零。
        candidate = str(_uuid.uuid4().int % 90_000_000 + 10_000_000)
        if candidate in taken or candidate in reserved:
            continue
        exists = (
            await session.execute(
                _text("SELECT 1 FROM users WHERE user_id=:uid"), {"uid": candidate}
            )
        ).scalar()
        if not exists:
            taken.add(candidate)
            return candidate
    raise ValueError("无法生成唯一的用户ID，请重试")


async def plan_legacy_migration() -> dict[str, str]:
    """为所有不规范 user_id 规划映射：admin 用户名→10000001，其余随机 8 位。

    10000001 被非 admin 占用时抛 ValueError 由调用方处理。
    """
    from sqlalchemy import text as _text

    from backend.shared.database_manager_v2 import get_session

    legacy = await find_legacy_users()
    if not legacy:
        return {}
    plan: dict[str, str] = {}
    async with get_session(read_only=True) as session:
        taken = {
            r for (r,) in (
                await session.execute(_text("SELECT user_id FROM users"))
            ).all()
        }
        for row in legacy:
            old = str(row["user_id"])
            if old in plan:
                continue
            if str(row.get("username") or "") == ADMIN_USERNAME:
                new = ADMIN_USER_ID
                if new in taken and new != old:
                    raise ValueError(
                        f"{ADMIN_USER_ID} 已被非 admin 用户占用，请先手工处理"
                    )
            else:
                new = await generate_user_id(session, taken)
            taken.add(new)
            taken.discard(old)
            plan[old] = new
    return plan


def needs_fix_sync() -> bool:
    """同步快检：users 表是否存在 user_id='admin' 的行（脚本预检用）。"""
    import os
    from urllib.parse import quote_plus

    from sqlalchemy import create_engine

    url = os.getenv("DATABASE_URL", "").strip()
    if "+asyncpg" in url:
        url = url.replace("+asyncpg", "+psycopg2")
    if not url.startswith("postgresql"):
        host = os.getenv("DB_HOST", "localhost")
        port = os.getenv("DB_PORT", "5432")
        user = os.getenv("DB_USER", "quantmind")
        password = os.getenv("DB_PASSWORD", "")
        dbname = os.getenv("DB_NAME", "quantmind")
        url = (
            f"postgresql+psycopg2://{user}:{quote_plus(password)}"
            f"@{host}:{port}/{dbname}"
        )
    engine = create_engine(url, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            n = conn.execute(
                text(
                    "SELECT count(*) FROM users "
                    "WHERE user_id IN ('admin', '00000001')"
                )
            ).scalar()
            return bool(n)
    finally:
        engine.dispose()
