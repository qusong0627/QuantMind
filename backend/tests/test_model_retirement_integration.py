"""§7 退役清退真库链路（P4，I 类）：夹具归档行 → sweep（预演/执行/幂等）→ 断言 → 清理。

覆盖验收口径：
- 预演不动盘、执行删产物但 **DB 行保留**（墓碑 ``artifacts_purged`` + 审计行）；
- 保留 N=3 按 ``(tenant,user,market)`` 分组生效（HK 单行不被 CN 挤掉）；
- 活跃 rollout（challenger 或 champion 列）引用 → 跳过；``promoted`` 非活跃 → 不拦；
- 产物已不在盘（``not_found``）照常写墓碑（幂等收敛，不重复报错）。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.integration

_TENANT_PREFIX = "t-retire-"
_USER = "u-retire"


def _meta(market: str) -> str:
    return json.dumps({"market": market}, ensure_ascii=False)


@pytest.mark.asyncio
async def test_sweep_retirement_end_to_end(tmp_path):
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import close_database, get_session
    from backend.shared.model_retirement import AUDIT_ACTION_PURGE, sweep_retirement

    tenant = f"{_TENANT_PREFIX}{uuid.uuid4().hex[:8]}"
    root = tmp_path / "models" / "users"
    root.mkdir(parents=True)
    now = datetime.now(timezone.utc)

    # (model_id, 归档天数, market, 在盘上否)；keep_n=3 → CN 组保留最新的 5/10/30 天三行
    seeds = [
        ("mdl_a_old", 40, "CN", True),          # 超保留 + 满冷却 → 清退
        ("mdl_b_mid", 35, "CN", True),          # 超保留 + 满冷却 → 清退
        ("mdl_c_30d", 30, "CN", True),          # 恰在保留位（第 3 新）→ 留
        ("mdl_d_10d", 10, "CN", True),          # 留
        ("mdl_e_5d", 5, "CN", True),            # 留
        ("mdl_hk_old", 40, "HK", True),         # HK 组仅 1 行 → 留（分组隔离）
        ("mdl_f_blocked_roll", 40, "CN", True),  # 活跃 rollout（challenger）→ 跳过
        ("mdl_g_blocked_champ", 40, "CN", True),  # 活跃 rollout（champion）→ 跳过
        ("mdl_h_notfound", 40, "CN", False),    # 超保留但产物已不在盘 → 照常墓碑
    ]
    updated_at_by = {mid: now - timedelta(days=days) for mid, days, _m, _o in seeds}

    async def _cleanup():
        async with get_session() as session:
            await session.execute(
                text("DELETE FROM user_audit_logs WHERE tenant_id = :t AND user_id = :u"),
                {"t": tenant, "u": _USER},
            )
            await session.execute(
                text("DELETE FROM qm_model_rollouts WHERE tenant_id = :t"), {"t": tenant}
            )
            await session.execute(
                text("DELETE FROM qm_user_models WHERE tenant_id = :t"), {"t": tenant}
            )

    try:
        await _cleanup()
        async with get_session() as session:
            for mid, days, market, on_disk in seeds:
                if on_disk:
                    d = root / mid
                    d.mkdir()
                    (d / "model.pkl").write_bytes(b"x" * 100)
                await session.execute(
                    text(
                        "INSERT INTO qm_user_models (tenant_id, user_id, model_id, status, "
                        "storage_path, metadata_json, is_default, created_at, updated_at) "
                        "VALUES (:t, :u, :m, 'archived', :sp, CAST(:meta AS JSONB), FALSE, "
                        ":created, :updated)"
                    ),
                    {
                        "t": tenant, "u": _USER, "m": mid,
                        "sp": str(root / mid),
                        "meta": _meta(market),
                        "created": now - timedelta(days=days),
                        "updated": updated_at_by[mid],
                    },
                )
            rollouts = [
                # 活跃（observing）：challenger 被引用 → 拦
                ("r1", "mdl_other_x", "mdl_f_blocked_roll", "observing"),
                # 活跃（replay_eval）：champion 被引用 → 拦（UNION 另一支）
                ("r2", "mdl_g_blocked_champ", "mdl_other_y", "replay_eval"),
                # 非活跃（promoted）：不拦（清退照走）
                ("r3", "mdl_a_old", "mdl_other_z", "promoted"),
            ]
            for rid, champ, chal, stage in rollouts:
                await session.execute(
                    text(
                        "INSERT INTO qm_model_rollouts (rollout_id, tenant_id, user_id, market, "
                        "champion_model_id, challenger_model_id, stage, notes) "
                        "VALUES (:rid, :t, :u, 'CN', :c, :ch, :s, 'retirement fixture')"
                    ),
                    {"rid": f"{tenant}-{rid}", "t": tenant, "u": _USER,
                     "c": champ, "ch": chal, "s": stage},
                )

        # ① 预演：计划正确、盘上不动
        s1 = await sweep_retirement(dry_run=True, tenant_id=tenant, models_root=root)
        assert [e["model_id"] for e in s1["purge"]] == [
            "mdl_a_old", "mdl_h_notfound", "mdl_b_mid",
        ]  # 最老先清；同刻平手按 model_id 升序
        assert sorted(e["model_id"] for e in s1["kept"]) == [
            "mdl_c_30d", "mdl_d_10d", "mdl_e_5d", "mdl_hk_old",
        ]
        skip_reason = {e["model_id"]: e["reason"] for e in s1["skipped"]}
        assert skip_reason["mdl_f_blocked_roll"] == "active_rollout_reference"
        assert skip_reason["mdl_g_blocked_champ"] == "active_rollout_reference"
        assert (root / "mdl_a_old").exists()  # 预演不删盘

        # ② 执行：产物删除 + 墓碑 + 审计；阻塞/保留者原样
        s2 = await sweep_retirement(dry_run=False, tenant_id=tenant, models_root=root)
        assert s2["errors"] == []
        assert all(e.get("purged") for e in s2["purge"])
        assert not (root / "mdl_a_old").exists()
        assert not (root / "mdl_b_mid").exists()
        assert (root / "mdl_f_blocked_roll").exists()
        assert (root / "mdl_hk_old").exists()

        async with get_session(read_only=True) as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT model_id, updated_at, metadata_json FROM qm_user_models "
                            "WHERE tenant_id = :t"
                        ),
                        {"t": tenant},
                    )
                )
                .mappings()
                .all()
            )
        by = {r["model_id"]: r for r in rows}
        for mid in ("mdl_a_old", "mdl_b_mid", "mdl_h_notfound"):
            meta = by[mid]["metadata_json"]
            assert meta.get("artifacts_purged") is True
            assert meta.get("artifacts_purged_at")
            assert "artifacts_purge_policy" in meta
            # updated_at 是归档时间戳代理，清退不得刷新（否则冷却期重计）
            assert by[mid]["updated_at"] == updated_at_by[mid]
        assert by["mdl_e_5d"]["metadata_json"].get("artifacts_purged") is None

        async with get_session(read_only=True) as session:
            audits = (
                (
                    await session.execute(
                        text(
                            "SELECT resource_id, description FROM user_audit_logs "
                            "WHERE tenant_id = :t AND action = :a"
                        ),
                        {"t": tenant, "a": AUDIT_ACTION_PURGE},
                    )
                )
                .mappings()
                .all()
            )
        assert {a["resource_id"] for a in audits} == {
            "mdl_a_old", "mdl_b_mid", "mdl_h_notfound",
        }
        assert any("墓碑" in a["description"] for a in audits)

        # ③ 幂等：再跑一轮零清退；已清退行带 already_purged 原因
        s3 = await sweep_retirement(dry_run=True, tenant_id=tenant, models_root=root)
        assert s3["purge"] == []
        skip_reason3 = {e["model_id"]: e["reason"] for e in s3["skipped"]}
        assert skip_reason3["mdl_a_old"] == "already_purged"
        assert skip_reason3["mdl_f_blocked_roll"] == "active_rollout_reference"
    finally:
        try:
            await _cleanup()
        finally:
            await close_database()
