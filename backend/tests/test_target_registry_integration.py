"""目标注册表真库生命周期（integration，默认 skip）。

运行条件（测试容器/CI）：
  QM_TEST_PG_DSN=postgresql://postgres:<password>@127.0.0.1:55432/postgres
  DB_HOST=127.0.0.1 DB_PORT=55432 DB_NAME=postgres DB_USER=postgres
  DB_PASSWORD=<password>

覆盖：迁移两次、内置种子幂等、draft→active→deprecated、版本不可变、
市场校验、deprecated 解析策略、审计落库、清理。
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from backend.shared.training.target_registry import (
    TargetRegistryError,
    activate,
    create_draft,
    deprecate,
    ensure_builtin_targets,
    get_target,
    list_targets,
    resolve_for_training,
)
from backend.shared.training.target_spec import spec_from_dict

ROOT = Path(__file__).resolve().parents[2]
SQL_PATH = ROOT / "backend" / "shared" / "db_migrations" / "upgrade_v1.1.9.sql"


def _spec(target_id: str, *, horizon: int = 5) -> dict:
    return {
        "target_id": target_id,
        "version": 1,
        "market": "CN",
        "family": "forward_return",
        "mode": "regression",
        "horizon_days": horizon,
        "entry": {"lag_days": 1, "price": "open"},
        "exit": {"price": "close"},
        "transform": {"type": "rank", "top_pct": 0.2, "drop_extreme_pct": 0.0},
        "description": "integration test target",
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_target_registry_lifecycle_real_db() -> None:
    dsn = os.getenv("QM_TEST_PG_DSN")
    if not dsn:
        pytest.skip("QM_TEST_PG_DSN not set")
    psycopg2 = pytest.importorskip("psycopg2")

    # 1) 迁移两次（幂等），并确保 app 连接可用
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(SQL_PATH.read_text(encoding="utf-8"))
            cur.execute(SQL_PATH.read_text(encoding="utf-8"))
    finally:
        conn.close()

    from backend.shared.database_manager_v2 import get_session

    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"app DB 不可用: {exc}")

    target_id = f"it_ret_5d_open_rank_{uuid.uuid4().hex[:6]}"
    spec = spec_from_dict(_spec(target_id))

    async def _cleanup() -> None:
        async with get_session() as session:
            await session.execute(
                text("DELETE FROM qm_label_target_audit WHERE target_id=:id"),
                {"id": target_id},
            )
            await session.execute(
                text(
                    "ALTER TABLE qm_label_targets "
                    "DISABLE TRIGGER trg_qm_label_targets_guard"
                )
            )
            await session.execute(
                text("DELETE FROM qm_label_targets WHERE id=:id"),
                {"id": target_id},
            )
            await session.execute(
                text(
                    "ALTER TABLE qm_label_targets "
                    "ENABLE TRIGGER trg_qm_label_targets_guard"
                )
            )
            await session.commit()

    try:
        async with get_session() as session:
            await ensure_builtin_targets(session, force=True)
            await session.commit()

        items = await list_targets(market="CN", status="active")
        builtin_ids = {item["id"] for item in items}
        assert {"cn_ret_5d_open_rank", "cn_updown_5d_open"} <= builtin_ids

        created = await create_draft(spec, actor="it", note="lifecycle")
        assert created["status"] == "draft"
        assert created["spec_hash"] == spec.spec_hash()

        # 幂等：同内容重复创建不报错
        again = await create_draft(spec, actor="it")
        assert again["spec_hash"] == created["spec_hash"]

        # 版本不可变：同 version 不同口径 → 409
        with pytest.raises(TargetRegistryError) as exc:
            await create_draft(
                spec_from_dict(_spec(target_id, horizon=10)), actor="it"
            )
        assert exc.value.code == "target_version_immutable"
        assert exc.value.http_status == 409

        # draft 默认不能用于训练
        with pytest.raises(TargetRegistryError) as exc:
            await resolve_for_training(
                {"id": target_id, "version": 1}, market="CN"
            )
        assert exc.value.code == "target_not_active"

        activated = await activate(target_id, 1, actor="it")
        assert activated["status"] == "active"

        resolved, status = await resolve_for_training(
            {"id": target_id, "version": 1}, market="CN"
        )
        assert status == "active"
        assert resolved.spec_hash() == spec.spec_hash()

        # 市场不符
        with pytest.raises(TargetRegistryError) as exc:
            await resolve_for_training(
                {"id": target_id, "version": 1}, market="HK"
            )
        assert exc.value.code == "target_market_mismatch"

        deprecated = await deprecate(target_id, 1, actor="it")
        assert deprecated["status"] == "deprecated"

        with pytest.raises(TargetRegistryError) as exc:
            await resolve_for_training(
                {"id": target_id, "version": 1}, market="CN"
            )
        assert exc.value.code == "target_deprecated"

        # 同 hash 的历史重训显式放行
        resolved2, status2 = await resolve_for_training(
            {"id": target_id, "version": 1}, market="CN", allow_deprecated=True
        )
        assert status2 == "deprecated"
        assert resolved2.spec_hash() == spec.spec_hash()

        # 审计：create + activate + deprecate
        async with get_session() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT action FROM qm_label_target_audit "
                        "WHERE target_id=:id ORDER BY audit_id"
                    ),
                    {"id": target_id},
                )
            ).scalars().all()
        assert {"create", "activate", "deprecate"} <= set(rows)

        # 行内容不可与 spec 漂移
        fetched = await get_target(target_id, 1)
        assert fetched["spec"]["horizon_days"] == 5
    finally:
        await _cleanup()
