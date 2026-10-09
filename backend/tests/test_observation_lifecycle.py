"""P2 rollout 台账（``qm_model_rollouts``）生命周期 + §5.5 多市场默认索引守卫。

- 纯测试部分：① store 的 ``_DDL_STATEMENTS`` 与 db_init.sql 镜像防漂移；
  ② §5.5 三份材料（db_init.sql / model_registry 启动自愈 / upgrade_v1.1.3.sql）
  的口径一致——新索引在、旧索引的 CREATE 不在（旧索引残留 = 多市场冠军
  再次互相顶掉；启动自愈重建旧索引 = 迁移被静默回滚）；
- 真库部分：① rollout 状态机全链路（含条件迁移的失配 no-op、终态后同挑战者
  可再建、回滚理由必填）；② 按市场部分唯一索引的实证——CN/HK 两个默认可并存，
  同市场第二个默认被唯一键拒绝（迁移的验收，不是纸上学）。测试行随用随建随删，
  租户前缀 unittest_。
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

import pytest

from backend.shared import model_rollout_store as store

ROOT = Path(__file__).resolve().parents[2]
DB_INIT = ROOT / "backend" / "shared" / "db_init.sql"
REGISTRY_SRC = ROOT / "backend" / "shared" / "model_registry.py"


def _resolve_upgrade_sql() -> Path:
    """与 main_oss._upgrade_sql_files 同序：容器 /data 优先（/app/data 符号链接在容器内无效）。"""
    candidates = [
        os.getenv("QM_UPGRADE_SQL_DIR", ""),
        os.getenv("QM_DATA_DIR", ""),
        "/data",
        str(ROOT / "data"),
    ]
    for directory in candidates:
        if not directory:
            continue
        candidate = Path(directory) / "upgrade_v1.1.3.sql"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("未找到 upgrade_v1.1.3.sql（查过 %s）" % candidates)


UPGRADE_SQL = _resolve_upgrade_sql()

_NEW_INDEX_CREATE = "CREATE UNIQUE INDEX IF NOT EXISTS uq_qm_user_models_default_per_market"
_OLD_INDEX_CREATE = "CREATE UNIQUE INDEX IF NOT EXISTS uq_qm_user_models_default_per_user"
_OLD_INDEX_DROP = "DROP INDEX IF EXISTS uq_qm_user_models_default_per_user"


def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip().rstrip(";").lower()


# ---------------------------------------------------------------------------
# 防漂移：store DDL ↔ db_init.sql
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_rollout_ddl_mirrored_in_db_init_and_non_destructive():
    db_init = _norm(DB_INIT.read_text(encoding="utf-8"))
    missing = [s for s in store._DDL_STATEMENTS if _norm(s) not in db_init]
    assert not missing, f"db_init.sql 缺少以下语句（两份 DDL 已漂移）: {missing}"
    for statement in store._DDL_STATEMENTS:
        lowered = statement.lower()
        for banned in ("drop ", "delete ", "truncate "):
            assert banned not in lowered


@pytest.mark.unit
def test_active_stage_predicate_matches_index_definition():
    """部分唯一索引谓词与 ``ACTIVE_STAGES`` 必须同集——否则并发保护有缺口。"""
    ddl = "\n".join(store._DDL_STATEMENTS)
    quoted = re.findall(r"stage IN \(([^)]+)\)", ddl)
    assert len(quoted) == 1
    stages_in_index = {token.strip().strip("'") for token in quoted[0].split(",")}
    assert stages_in_index == set(store.ACTIVE_STAGES)


# ---------------------------------------------------------------------------
# §5.5：多市场默认索引的口径一致
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_multimarket_index_migrated_everywhere():
    db_init = DB_INIT.read_text(encoding="utf-8")
    registry = REGISTRY_SRC.read_text(encoding="utf-8")
    upgrade = UPGRADE_SQL.read_text(encoding="utf-8")

    # 新索引在建库脚本与启动自愈里都在
    assert _NEW_INDEX_CREATE in db_init
    assert _NEW_INDEX_CREATE in registry
    # 旧索引的 CREATE 不许出现在任何一处（启动自愈重建 = 迁移被静默回滚）
    assert _OLD_INDEX_CREATE not in db_init
    assert _OLD_INDEX_CREATE not in registry
    # 迁移脚本：DROP 旧 + CREATE 新
    assert _OLD_INDEX_DROP in upgrade
    assert _NEW_INDEX_CREATE in upgrade


@pytest.mark.unit
def test_upgrade_sql_survives_startup_destructive_filter():
    """启动期自动迁移会整份跳过含破坏性语句的脚本（main_oss._is_destructive_sql）。

    迁移文件里每一条语句只能是 DROP INDEX / CREATE …——混入 DELETE 等会让
    整个迁移在启动时被静默跳过（一次静默的「没迁移」）。
    """
    lines = [
        line for line in UPGRADE_SQL.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("--")
    ]
    statements = [s.strip() for s in "\n".join(lines).split(";") if s.strip()]
    assert statements, "迁移脚本为空"
    for statement in statements:
        head = statement.split("(", 1)[0].upper()
        assert head.startswith("DROP INDEX") or head.startswith("CREATE"), statement


# ---------------------------------------------------------------------------
# 真库：rollout 状态机
# ---------------------------------------------------------------------------


async def _db_available() -> bool:
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rollout_lifecycle_real_db():
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import close_database, get_session

    if not await _db_available():
        pytest.skip("DB 不可用")

    await store.ensure_tables()

    unique = uuid.uuid4().hex[:6]
    tenant = f"unittest_rollout_{unique}"
    user = "u"
    market = "CN"
    champ = f"mdl_champ_{unique}"
    chal = f"mdl_chal_{unique}"

    async def _cleanup() -> None:
        async with get_session() as session:
            await session.execute(
                text("DELETE FROM qm_model_rollouts WHERE tenant_id = :t"),
                {"t": tenant},
            )

    await _cleanup()
    try:
        # 1) 创建 → replay_eval；同挑战者活跃重复 → None（部分唯一索引）
        created = await store.insert_rollout(
            tenant_id=tenant,
            user_id=user,
            market=market,
            champion_model_id=champ,
            challenger_model_id=chal,
            campaign_id="rc_unittest_x",
        )
        assert created is not None
        assert created["stage"] == store.STAGE_REPLAY_EVAL
        assert created["rollout_id"].startswith("ro_cn_")
        rid = created["rollout_id"]

        dup = await store.insert_rollout(
            tenant_id=tenant,
            user_id=user,
            market=market,
            champion_model_id=champ,
            challenger_model_id=chal,
        )
        assert dup is None
        active = await store.get_active_rollout(
            tenant_id=tenant, user_id=user, market=market, challenger_model_id=chal
        )
        assert active is not None and active["rollout_id"] == rid

        # 2) → observing（带证据）；COALESCE 语义：不传 notes 保留旧值
        observing = await store.transition(
            rid,
            to_stage=store.STAGE_OBSERVING,
            from_stages=[store.STAGE_REPLAY_EVAL],
            evidence={"paired": {"n_days": 163}},
            notes="进入观察",
        )
        assert observing is not None and observing["stage"] == store.STAGE_OBSERVING
        assert observing["evidence"]["paired"]["n_days"] == 163

        # 3) 失配迁移：from_stages 不含当前阶段 → None（不跳级、不覆盖）
        assert (
            await store.transition(
                rid,
                to_stage=store.STAGE_PROMOTED,
                from_stages=[store.STAGE_REPLAY_EVAL],
                decided_by="tester",
            )
            is None
        )

        # 4) → gate_passed
        passed = await store.transition(
            rid,
            to_stage=store.STAGE_GATE_PASSED,
            from_stages=[store.STAGE_OBSERVING],
            gate_result={"summary": {"verdict": "all_pass"}},
        )
        assert passed["stage"] == store.STAGE_GATE_PASSED
        assert passed["notes"] == "进入观察"  # 未传 notes → 保留
        assert passed["evidence"]["paired"]["n_days"] == 163  # 未传 evidence → 保留

        # 5) 晋升：decided 落 decided_at + 备任链
        promoted = await store.transition(
            rid,
            to_stage=store.STAGE_PROMOTED,
            from_stages=[store.STAGE_GATE_PASSED],
            prior_default_model_id=champ,
            decided_by="tester",
            decided=True,
        )
        assert promoted["stage"] == store.STAGE_PROMOTED
        assert promoted["decided_at"] is not None
        assert promoted["prior_default_model_id"] == champ

        latest = await store.latest_promotion_of(
            chal, tenant_id=tenant, user_id=user, market=market
        )
        assert latest is not None and latest["rollout_id"] == rid

        # 6) 回滚：理由必填（进 SQL 前就拦）
        with pytest.raises(ValueError, match="理由必填"):
            await store.transition(
                rid,
                to_stage=store.STAGE_ROLLED_BACK,
                from_stages=[store.STAGE_PROMOTED],
                decided=True,
                require_notes=True,
            )
        rolled = await store.transition(
            rid,
            to_stage=store.STAGE_ROLLED_BACK,
            from_stages=[store.STAGE_PROMOTED],
            notes="前向 IC 转负",
            decided_by="tester",
            decided=True,
            require_notes=True,
        )
        assert rolled["stage"] == store.STAGE_ROLLED_BACK
        assert rolled["notes"] == "前向 IC 转负"

        # 7) 终态后可再建（复评）：唯一槽已释放
        again = await store.insert_rollout(
            tenant_id=tenant,
            user_id=user,
            market=market,
            champion_model_id=champ,
            challenger_model_id=chal,
        )
        assert again is not None and again["rollout_id"] != rid

        # 8) 列表按租户/市场过滤可见两条
        rows = await store.list_rollouts(tenant_id=tenant, market=market, limit=10)
        assert {r["rollout_id"] for r in rows} == {rid, again["rollout_id"]}
    finally:
        try:
            await _cleanup()
        finally:
            # 连接池与事件循环绑定：不关掉，同文件下一个真库测试会拿着旧池
            # 在新 loop 上操作而静默 skip（close_database 纪律）。
            await close_database()


# ---------------------------------------------------------------------------
# 真库：§5.5 索引行为实证
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_per_market_default_index_behavior_real_db():
    """CN 与 HK 默认可并存；同市场第二个默认被拒（uq_..._per_market）。"""
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import close_database, get_session

    if not await _db_available():
        pytest.skip("DB 不可用")

    unique = uuid.uuid4().hex[:6]
    tenant = f"unittest_defaults_{unique}"
    user = "u"

    async def _cleanup() -> None:
        async with get_session() as session:
            await session.execute(
                text("DELETE FROM qm_user_models WHERE tenant_id = :t"),
                {"t": tenant},
            )

    async def _insert(model_id: str, market: str | None) -> None:
        metadata = f'{{"market": "{market}"}}' if market else None
        async with get_session() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO qm_user_models (
                        tenant_id, user_id, model_id, status, is_default, metadata_json
                    ) VALUES (
                        :tenant, :user, :model_id, 'ready', TRUE, CAST(:metadata AS JSONB)
                    )
                    """
                ),
                {
                    "tenant": tenant,
                    "user": user,
                    "model_id": model_id,
                    "metadata": metadata,
                },
            )

    await _cleanup()
    try:
        # 按市场并存：CN + HK 都 is_default=TRUE
        await _insert(f"mdl_cn_{unique}", "CN")
        await _insert(f"mdl_hk_{unique}", "HK")

        async with get_session(read_only=True) as session:
            count = (
                await session.execute(
                    text(
                        "SELECT COUNT(*) FROM qm_user_models "
                        "WHERE tenant_id = :t AND is_default = TRUE"
                    ),
                    {"t": tenant},
                )
            ).scalar()
        assert count == 2, "既有全局唯一索引残留（多市场冠军互相顶掉）"

        # 同市场第二个默认 → 唯一键拒绝
        with pytest.raises(Exception) as excinfo:
            await _insert(f"mdl_cn2_{unique}", "CN")
        assert "uq_qm_user_models_default_per_market" in str(excinfo.value)
    finally:
        try:
            await _cleanup()
        finally:
            await close_database()
