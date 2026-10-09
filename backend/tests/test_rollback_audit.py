"""P2 晋升/回滚/拒绝审计演练（真实库，设计 §5.3/§5.4）。

不造重放证据（那需要真 model 产物目录，属 P2 验收演练的活）——本测试锻的是
**事务与审计骨架**：

- ``create_rollout`` 的 champion 取自**数据库当前默认**（不是入参）；环境不合法
  （挑战者不存在/状态不对/无默认）显式拒绝；
- 非 ``gate_passed`` 不许晋升；owner 不匹配按不存在处理（不泄露他人台账）；
- 晋升 = 单事务「市场级默认切换 + prior_default 落账 + decided_at/by」，
  且**只切本市场**——HK 冠军不被 CN 晋升顶掉（§5.5 索引行为在此再验一次）；
- 回滚 = 默认切回备任 + 理由必填（空白理由进服务层就拦）+ 关 settings 行；
  备任链缺失（prior 为 NULL）诚实拒绝，不猜；
- 拒绝 = 终态 + 关 settings 行。

另附 §5.4 archive 回退修复的实证：归档当前默认后，**备任链优先**（台账里该
模型晋升时记录的前任），其次才是「同市场最近更新 ready」；且回退只在**本市场**
找继任——更晚更新的其它市场模型不许被扶上马。

测试行随用随建随删，租户前缀 ``unittest_rollout_``；``close_database`` 兜底
（连接池与事件循环绑定——不关掉会连累同文件下一个真库测试静默 skip）。
"""

from __future__ import annotations

import uuid

import pytest

from backend.shared import model_rollout_store as store
from backend.shared.model_registry import model_registry_service
from backend.services.engine.services.model_inference_persistence import (
    model_inference_persistence,
)
from backend.services.engine.services.model_rollout_service import (
    RolloutConflict,
    RolloutInvalid,
    RolloutNotFound,
    model_rollout_service,
)


async def _db_available() -> bool:
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


async def _insert_model(
    tenant: str, user: str, model_id: str, *, market: str, is_default: bool
) -> None:
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    async with get_session() as session:
        await session.execute(
            text(
                """
                INSERT INTO qm_user_models (
                    tenant_id, user_id, model_id, status, is_default, metadata_json
                ) VALUES (
                    :tenant, :user, :model_id, 'ready', :is_default,
                    CAST(:metadata AS JSONB)
                )
                """
            ),
            {
                "tenant": tenant,
                "user": user,
                "model_id": model_id,
                "is_default": is_default,
                "metadata": f'{{"market": "{market}"}}',
            },
        )


async def _defaults_of(tenant: str) -> dict[str, set[str]]:
    """→ {market: {默认模型 id}}（当前库内真实状态）。"""
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=True) as session:
        rows = (
            (
                await session.execute(
                    text(
                        """
                        SELECT model_id,
                               qm_market_of(metadata_json) AS market
                        FROM qm_user_models
                        WHERE tenant_id = :tenant AND is_default = TRUE
                        """
                    ),
                    {"tenant": tenant},
                )
            )
            .mappings()
            .all()
        )
    out: dict[str, set[str]] = {}
    for row in rows:
        out.setdefault(str(row["market"]), set()).add(str(row["model_id"]))
    return out


async def _settings_enabled(tenant: str, user: str, model_id: str) -> bool | None:
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=True) as session:
        value = (
            await session.execute(
                text(
                    """
                    SELECT enabled FROM qm_model_inference_settings
                    WHERE tenant_id = :tenant AND user_id = :user AND model_id = :model_id
                    """
                ),
                {"tenant": tenant, "user": user, "model_id": model_id},
            )
        ).scalar()
    return None if value is None else bool(value)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_promote_rollback_reject_audit_drill():
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import close_database, get_session

    if not await _db_available():
        pytest.skip("DB 不可用")

    await store.ensure_tables()
    await model_inference_persistence.ensure_tables()

    unique = uuid.uuid4().hex[:6]
    tenant = f"unittest_rollout_{unique}"
    user = "u"
    owner = (tenant, user)
    champ_cn = f"mdl_cn_{unique}_champ"
    chal_cn = f"mdl_cn_{unique}_chal"
    champ_hk = f"mdl_hk_{unique}_champ"

    async def _cleanup() -> None:
        async with get_session() as session:
            for table in (
                "qm_model_rollouts",
                "qm_user_models",
                "qm_model_inference_settings",
            ):
                await session.execute(
                    text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant}
                )

    await _cleanup()
    try:
        await _insert_model(tenant, user, champ_cn, market="CN", is_default=True)
        await _insert_model(tenant, user, chal_cn, market="CN", is_default=False)
        await _insert_model(tenant, user, champ_hk, market="HK", is_default=True)

        # 1) 创建：champion 取自库内默认（不是入参）；环境不合法显式拒绝
        with pytest.raises(RolloutInvalid, match="不存在"):
            await model_rollout_service.create_rollout(
                tenant_id=tenant, user_id=user, market="CN",
                challenger_model_id=f"mdl_cn_{unique}_ghost",
            )
        with pytest.raises(RolloutInvalid, match="就是当前默认"):
            await model_rollout_service.create_rollout(
                tenant_id=tenant, user_id=user, market="CN",
                challenger_model_id=champ_cn,
            )
        rollout = await model_rollout_service.create_rollout(
            tenant_id=tenant, user_id=user, market="CN", challenger_model_id=chal_cn
        )
        rid = rollout["rollout_id"]
        assert rollout["champion_model_id"] == champ_cn
        assert rollout["stage"] == store.STAGE_REPLAY_EVAL

        # 2) 活跃重复 → 409 类冲突（部分唯一索引）
        with pytest.raises(RolloutConflict, match="活跃 rollout"):
            await model_rollout_service.create_rollout(
                tenant_id=tenant, user_id=user, market="CN", challenger_model_id=chal_cn
            )

        # 3) 非 gate_passed 不许晋升；owner 不匹配按不存在
        with pytest.raises(RolloutConflict, match="不可晋升"):
            await model_rollout_service.promote(
                rid, decided_by=user, notes="太早", owner=owner
            )
        with pytest.raises(RolloutNotFound):
            await model_rollout_service.promote(
                rid, decided_by=user, notes="越权", owner=("other_tenant", user)
            )

        # 4) 证据组装冒烟：假 model_id 无产物目录 → 回放降级为 warning，
        #    G0 三件套缺 → 不过；阶段保持 replay_eval（观察 0/20）
        evaluated = await model_rollout_service.evaluate(rid, owner=owner)
        assert evaluated["rollout"]["stage"] == store.STAGE_REPLAY_EVAL
        gates = {
            g["gate"]: g
            for g in evaluated["evaluation"]["gates"]
            if isinstance(g, dict)
        }
        assert gates["G0"]["status"] == "fail"
        assert evaluated["warnings"], "回放不可用必须留 warning，不许装成评估过"
        # G0 不过 → 不许进观察期
        with pytest.raises(RolloutConflict, match="G0"):
            await model_rollout_service.start_observation(rid, owner=owner)

        # 5) 演进到 gate_passed（观察证据属重放演练，此处直接置阶段），晋升
        await store.transition(
            rid, to_stage=store.STAGE_GATE_PASSED, from_stages=[store.STAGE_REPLAY_EVAL]
        )
        with pytest.raises(RolloutInvalid, match="理由"):
            await model_rollout_service.promote(rid, decided_by=user, notes="  ", owner=owner)
        promoted = await model_rollout_service.promote(
            rid, decided_by=user, notes="观察期 IC 稳定优于冠军", owner=owner
        )
        row = promoted["rollout"]
        assert row["stage"] == store.STAGE_PROMOTED
        assert row["prior_default_model_id"] == champ_cn
        assert row["decided_by"] == user and row["decided_at"]
        defaults = await _defaults_of(tenant)
        assert defaults.get("CN") == {chal_cn}
        assert defaults.get("HK") == {champ_hk}, "CN 晋升不许动 HK 默认（§5.5）"

        # 6) 回滚：理由必填；切回备任；关 settings 行
        await model_inference_persistence.update_settings(
            tenant_id=tenant, user_id=user, model_id=chal_cn, enabled=True
        )
        with pytest.raises(RolloutInvalid, match="理由"):
            await model_rollout_service.rollback(
                rid, decided_by=user, notes="", owner=owner
            )
        rolled = await model_rollout_service.rollback(
            rid, decided_by=user, notes="前向 IC 转负，回退冠军", owner=owner
        )
        assert rolled["rollout"]["stage"] == store.STAGE_ROLLED_BACK
        assert rolled["restored_model_id"] == champ_cn
        defaults = await _defaults_of(tenant)
        assert defaults.get("CN") == {champ_cn}
        assert defaults.get("HK") == {champ_hk}, "回滚同样只动本市场"
        assert await _settings_enabled(tenant, user, chal_cn) is False

        # 7) 拒绝路径：重新建单 → 开 settings → 拒绝（理由必填，关行）
        second = await model_rollout_service.create_rollout(
            tenant_id=tenant, user_id=user, market="CN", challenger_model_id=chal_cn
        )
        rid2 = second["rollout_id"]
        await model_inference_persistence.update_settings(
            tenant_id=tenant, user_id=user, model_id=chal_cn, enabled=True
        )
        with pytest.raises(RolloutInvalid, match="理由"):
            await model_rollout_service.reject(
                rid2, decided_by=user, notes=" ", owner=owner
            )
        rejected = await model_rollout_service.reject(
            rid2, decided_by=user, notes="回放配对不显著", owner=owner
        )
        assert rejected["rollout"]["stage"] == store.STAGE_REJECTED
        assert rejected["rollout"]["decided_at"]
        assert await _settings_enabled(tenant, user, chal_cn) is False

        # 8) 备任链缺失：直接伪造 promoted（prior=NULL）→ 回滚诚实拒绝
        third = await model_rollout_service.create_rollout(
            tenant_id=tenant, user_id=user, market="CN", challenger_model_id=chal_cn
        )
        rid3 = third["rollout_id"]
        await store.transition(
            rid3, to_stage=store.STAGE_GATE_PASSED, from_stages=[store.STAGE_REPLAY_EVAL]
        )
        await store.transition(
            rid3,
            to_stage=store.STAGE_PROMOTED,
            from_stages=[store.STAGE_GATE_PASSED],
            decided=True,
            decided_by="forged",
        )
        with pytest.raises(RolloutInvalid, match="备任链"):
            await model_rollout_service.rollback(
                rid3, decided_by=user, notes="x", owner=owner
            )
    finally:
        try:
            await _cleanup()
        finally:
            await close_database()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_archive_prefers_prior_default_from_ledger():
    """§5.4：archive 回退 = 备任链优先 → 同市场最近 ready；跨市场不越权。"""
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import close_database, get_session

    if not await _db_available():
        pytest.skip("DB 不可用")

    await store.ensure_tables()

    unique = uuid.uuid4().hex[:6]
    tenant = f"unittest_rollout_{unique}"
    user = "u"

    async def _cleanup() -> None:
        async with get_session() as session:
            for table in ("qm_model_rollouts", "qm_user_models"):
                await session.execute(
                    text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant}
                )

    await _cleanup()
    try:
        # ── 场景①：有备任链——prior 必须胜过「更晚更新的同市场 ready」与
        #     「更晚更新的其它市场 ready」
        prior_cn = f"mdl_cn_{unique}_prior"
        mid_cn = f"mdl_cn_{unique}_mid"
        fresh_cn = f"mdl_cn_{unique}_fresh"
        decoy_hk = f"mdl_hk_{unique}_decoy"
        champ_hk = f"mdl_hk_{unique}_champ"
        await _insert_model(tenant, user, prior_cn, market="CN", is_default=False)
        await _insert_model(tenant, user, mid_cn, market="CN", is_default=True)
        await _insert_model(tenant, user, fresh_cn, market="CN", is_default=False)
        await _insert_model(tenant, user, champ_hk, market="HK", is_default=True)
        await _insert_model(tenant, user, decoy_hk, market="HK", is_default=False)

        # 台账：mid_cn 经晋升上台，前任 prior_cn（§5.4 的备任链来源）
        row = await store.insert_rollout(
            tenant_id=tenant,
            user_id=user,
            market="CN",
            champion_model_id=prior_cn,
            challenger_model_id=mid_cn,
        )
        assert row is not None
        await store.transition(
            row["rollout_id"],
            to_stage=store.STAGE_PROMOTED,
            from_stages=[store.STAGE_REPLAY_EVAL],
            prior_default_model_id=prior_cn,
            decided_by="tester",
            decided=True,
        )

        archived = await model_registry_service.archive_model(
            tenant_id=tenant, user_id=user, model_id=mid_cn
        )
        assert archived["status"] == "archived"
        defaults = await _defaults_of(tenant)
        assert defaults.get("CN") == {prior_cn}, (
            "备任链优先于「最近更新 ready」——fresh/decoy 都不该被扶上马"
        )
        assert defaults.get("HK") == {champ_hk}, "归档 CN 不许动 HK 默认"

        # ── 场景②：无备任链——回退到同市场最近 ready；更新的 US 诱饵不许上位
        # 默认位先从 prior_cn 挪走（同市场唯一默认——索引行为顺带再验一次）
        async with get_session() as session:
            await session.execute(
                text(
                    "UPDATE qm_user_models SET is_default = FALSE "
                    "WHERE tenant_id = :t AND model_id = :m"
                ),
                {"t": tenant, "m": prior_cn},
            )
        mid2_cn = f"mdl_cn_{unique}_mid2"
        fresh2_cn = f"mdl_cn_{unique}_fresh2"
        decoy_us = f"mdl_us_{unique}_decoy"
        await _insert_model(tenant, user, mid2_cn, market="CN", is_default=True)
        await _insert_model(tenant, user, fresh2_cn, market="CN", is_default=False)
        await _insert_model(tenant, user, decoy_us, market="US", is_default=False)  # 最新

        await model_registry_service.archive_model(
            tenant_id=tenant, user_id=user, model_id=mid2_cn
        )
        defaults = await _defaults_of(tenant)
        assert defaults.get("CN") == {fresh2_cn}, "无备任链 → 同市场最近 ready"
        assert "US" not in defaults, "跨市场诱饵（updated_at 更新）不许被扶上马"
        assert defaults.get("HK") == {champ_hk}
    finally:
        try:
            await _cleanup()
        finally:
            await close_database()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_custom_market_unknown_value_regression():
    """P2 验收③补钉：未知 market 值（'CUSTOM'）按 CN 口径全链一致（v1.1.4 修复）。

    旧实现（裸 COALESCE 谓词，v1.1.3）把 'CUSTOM' 漏在市场之外：晋升时「清本市场
    旧默认」更新不到它，索引又把 'CUSTOM' 当独立市场 —— CN 出现两个默认并存，
    回滚/应急恢复再撞旧全局唯一索引直接报错（本机 2026-10-08 演练实测）。本测试
    用真实库复现该形态，锁死三件事：

    ① 索引把 'CUSTOM' 与 'CN' 归为同一市场（第二个默认位直接违反唯一约束——
       此断言同时强制 v1.1.4 迁移已生效，旧索引下不会 raise）；
    ② 服务侧晋升清位谓词命中未知值行：CUSTOM 挑战者晋升后是 CN 唯一默认；
    ③ 回滚完整回到冠军（备任链 + 清位都命中同市场）。
    """
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    from backend.shared.database_manager_v2 import close_database, get_session

    if not await _db_available():
        pytest.skip("DB 不可用")

    await store.ensure_tables()
    await model_inference_persistence.ensure_tables()

    unique = uuid.uuid4().hex[:6]
    tenant = f"unittest_rollout_{unique}"
    user = "u"
    owner = (tenant, user)
    champ_cn = f"mdl_cn_{unique}_champ"
    chal_custom = f"mdl_custom_{unique}_chal"

    async def _cleanup() -> None:
        async with get_session() as session:
            for table in (
                "qm_model_rollouts",
                "qm_user_models",
                "qm_model_inference_settings",
            ):
                await session.execute(
                    text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant}
                )

    await _cleanup()
    try:
        await _insert_model(tenant, user, champ_cn, market="CN", is_default=True)
        await _insert_model(tenant, user, chal_custom, market="CUSTOM", is_default=False)

        # ① 索引口径：再置一个「异形 CN」默认必违反唯一约束
        with pytest.raises(IntegrityError):
            async with get_session() as session:
                await session.execute(
                    text(
                        "UPDATE qm_user_models SET is_default = TRUE "
                        "WHERE tenant_id = :t AND model_id = :m"
                    ),
                    {"t": tenant, "m": chal_custom},
                )

        # ② 晋升：champion 解析到 champ_cn；清位谓词命中未知值行
        rollout = await model_rollout_service.create_rollout(
            tenant_id=tenant,
            user_id=user,
            market="CN",
            challenger_model_id=chal_custom,
        )
        rid = rollout["rollout_id"]
        assert rollout["champion_model_id"] == champ_cn
        await store.transition(
            rid, to_stage=store.STAGE_GATE_PASSED, from_stages=[store.STAGE_REPLAY_EVAL]
        )
        promoted = await model_rollout_service.promote(
            rid, decided_by="unittest", notes="CUSTOM 归 CN 回归", owner=owner
        )
        assert promoted["rollout"]["prior_default_model_id"] == champ_cn
        defaults = await _defaults_of(tenant)
        assert defaults.get("CN") == {chal_custom}, (
            "CUSTOM 挑战者晋升后必须是 CN 唯一默认（旧谓词漏清冠军 → 双默认并存）"
        )

        # ③ 回滚：完整回到冠军
        rolled = await model_rollout_service.rollback(
            rid, decided_by="unittest", notes="回归验证回滚", owner=owner
        )
        assert rolled["restored_model_id"] == champ_cn
        assert (await _defaults_of(tenant)).get("CN") == {champ_cn}
    finally:
        try:
            await _cleanup()
        finally:
            await close_database()
