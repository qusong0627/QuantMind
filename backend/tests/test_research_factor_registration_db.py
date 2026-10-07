"""因子研究 → 训练目录注册：落库契约（真库，事务内自回滚，零残留）。

单元测试（``test_research_factor_registration.py``）守的是「code → 映射行」的
解析正确性；本文件守的是只有打到真库才暴露得出来的那几条**机构级**性质：

1. **幂等**：同一批因子注册两次不产生重复行（靠 ``ON CONFLICT`` 而非
   SELECT-then-INSERT——后者在并发下会撞唯一索引或写出两行）；
2. **只写草稿**：已发布版本拒绝写入。这是发布闸门的完整性——注册若能顺手改
   线上训练口径，发布这一步就形同虚设；
3. **跨库拒绝**：钉住某草稿时，来自别的来源库的因子必须整体拒绝，而不是把
   列名硬塞进去（错位的映射训练时读出来是 NaN，没有任何一层会报错）；
4. **fail-closed**：来源库的列存在性无法验证时**一律不写**。放行会让错误的
   映射推迟到训练时才炸，或者更糟——静默读到常量；
5. **审计与副作用同事务**：审计行必须和映射行一起落地（``AuditLogService``
   会在 ``log_action`` 内部自行 commit，用它会把这个事务提前提交、破坏原子性，
   所以这里是直接 INSERT，不调那个服务）；
6. **并发建草稿**：两个管理员同时注册同一来源，只能产出**一份**草稿。

真库用例全程在单个事务内执行并在 finally 里 ``rollback()``——净残留为零，
不会碰任何已有草稿。并发用例无法单事务完成，改用唯一版本名标记 + 显式清理。

事件循环：pytest-asyncio 给**每个用例开一个新 loop**，而 ``DatabaseManager``
把 engine/连接池缓存在实例上（连接绑死在创建它的那个 loop）。用例结束时若不
释放，下个用例的 ``pool_pre_ping`` 会拿到上一轮的连接，抛
``attached to a different loop``——表现为**一过一个挂**的交替失败。仓库既有
做法（``test_agent_ledger_store.py``）是每个碰库用例收尾 ``close_database()``
把池 dispose 掉，这里保持一致。

DB 不可用时 skip。
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid

import pytest
from sqlalchemy import text

from backend.services.api.routers.admin import quantdb_factor_catalog as qfc
from backend.services.api.routers.admin import research_factor_registration as rfr
from backend.services.engine.data_platform.quantdb_factor_reader import (
    EXCLUDED_FROM_TRAINING,
)
from backend.services.engine.factor_research import store

# 用 CN 的静态源 l1_factors 做靶：它有已刷新字段，且当前无存量草稿。
_TARGET_LIB = "l1_factors"


async def _probe() -> None:
    from backend.shared.database_manager_v2 import get_session

    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"DB 不可用: {exc}")


async def _real_codes(limit: int = 3) -> list[str]:
    """从真实快照里取若干「来源库 = _TARGET_LIB 且已发现」的因子代码。"""
    from backend.shared.database_manager_v2 import get_session

    meta = store.factors_meta("private") or {}
    wanted = [
        str(f["code"])
        for f in (meta.get("factors") or [])
        if str(f.get("l2") or "") == _TARGET_LIB
    ]
    async with get_session(read_only=True) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT column_name FROM qm_quantdb_factor_field "
                    "WHERE market = 'CN' AND dataset_id = :d AND is_present"
                ),
                {"d": _TARGET_LIB},
            )
        ).scalars().all()
    known = {str(r) for r in rows}
    usable = [c for c in wanted if c in known]
    if len(usable) < limit:
        pytest.skip(f"CN/{_TARGET_LIB} 已发现字段不足以支撑用例（{len(usable)}）")
    return usable[:limit]


async def _scalar(session, sql: str, **params) -> int:
    return int((await session.execute(text(sql), params)).scalar_one())


async def _mapping_count(session, version_id: str) -> int:
    return await _scalar(
        session,
        "SELECT count(*) FROM qm_training_factor_mapping WHERE version_id = :v",
        v=version_id,
    )


async def _mapping_count_for_codes(session, version_id: str, codes: list[str]) -> int:
    """只数**本次请求涉及**的那些行。

    草稿会从线上那份播种（见 ``test_registration_draft_is_seeded_from_published``），
    所以「草稿总行数」里必然混着与本次注册无关的存量映射——拿总数当断言靶会
    恒假。要断言「没写进去」，就只数被请求的那几个 key/列。
    """
    return await _scalar(
        session,
        "SELECT count(*) FROM qm_training_factor_mapping "
        "WHERE version_id = :v AND (feature_key = ANY(:c) OR source_column = ANY(:c))",
        v=version_id,
        c=list(codes),
    )


async def _draft_ids(session, source_dataset: str) -> set[str]:
    rows = (
        await session.execute(
            text(
                "SELECT version_id FROM qm_training_factor_catalog_version "
                "WHERE market = 'CN' AND source_dataset = :d AND status = 'draft'"
            ),
            {"d": source_dataset},
        )
    ).scalars().all()
    return {str(r) for r in rows}


async def _mapping_keys(session, version_ids: set[str]) -> set[str]:
    """这些版本里**已经存在**的 feature_key。

    用途：把「本次注册请求涉及的 key」和「本次真正新建的行」分开。注册是 upsert
    （``ON CONFLICT DO UPDATE``），key 已存在时不新建行——清理若按请求 key 删，
    删掉的就是存量行。
    """
    if not version_ids:
        return set()
    rows = (
        await session.execute(
            text(
                "SELECT feature_key FROM qm_training_factor_mapping "
                "WHERE version_id = ANY(:ids)"
            ),
            {"ids": list(version_ids)},
        )
    ).scalars().all()
    return {str(r) for r in rows}


async def _audit_rows(session, user_id: str) -> list[dict]:
    rows = (
        await session.execute(
            text(
                "SELECT action, resource, success, description, resource_id "
                "FROM user_audit_logs WHERE user_id = :u AND action = :a "
                "ORDER BY id DESC"
            ),
            {"u": user_id, "a": rfr.AUDIT_ACTION},
        )
    ).mappings().all()
    return [dict(r) for r in rows]


class _Sandbox:
    """单事务沙箱：跑完无条件 rollback，净残留为零。"""

    def __init__(self, session):
        self.session = session

    async def count_mappings(self, version_id: str) -> int:
        return await _mapping_count(self.session, version_id)

    async def count_mappings_for_codes(self, version_id: str, codes: list[str]) -> int:
        return await _mapping_count_for_codes(self.session, version_id, codes)

    async def audit_rows(self, user_id: str) -> list[dict]:
        return await _audit_rows(self.session, user_id)


@contextlib.asynccontextmanager
async def _sandbox():
    """单事务沙箱，收尾 dispose 连接池（见模块 docstring 的事件循环说明）。"""
    from backend.shared.database_manager_v2 import close_database, get_session

    await _probe()
    try:
        # DDL 独立事务——与端点同形状（见 `register_from_research` 的死锁说明）。
        async with get_session() as ddl:
            await qfc._ensure_schema(ddl)
        async with get_session() as session:
            try:
                yield _Sandbox(session)
            finally:
                await session.rollback()
    finally:
        await close_database()


# ---------------------------------------------------------------------------
# 幂等
# ---------------------------------------------------------------------------
@pytest.mark.integration
@pytest.mark.database
@pytest.mark.asyncio
async def test_repeated_registration_is_idempotent():
    """同一批因子注册两次：行数不变、不报错、两次都报「已注册」。

    断言按**本次注册涉及的那些行**计数，不按草稿总行数：草稿会从线上播种
    （见 ``test_registration_draft_is_seeded_from_published``），总数里必然
    混着与本次无关的存量映射。
    """
    async with _sandbox() as sandbox:
        codes = await _real_codes(3)
        user = f"t-{uuid.uuid4().hex[:10]}"

        first = await rfr.register_research_factors(
            sandbox.session, market="CN", dataset="private", codes=codes,
            user_id=user, version_name=f"t-{uuid.uuid4().hex[:8]}",
        )
        version_id = first["versions"][_TARGET_LIB]
        assert len(first["registered"]) == 3
        assert await sandbox.count_mappings_for_codes(version_id, codes) == 3
        after_first = await sandbox.count_mappings(version_id)

        second = await rfr.register_research_factors(
            sandbox.session, market="CN", dataset="private", codes=codes,
            user_id=user, version_id=version_id,
        )

        assert len(second["registered"]) == 3
        assert second["versions"][_TARGET_LIB] == version_id
        assert await sandbox.count_mappings(version_id) == after_first, "重复注册产生了重复行"


# ---------------------------------------------------------------------------
# 发布闸门
# ---------------------------------------------------------------------------
@pytest.mark.integration
@pytest.mark.database
@pytest.mark.asyncio
async def test_published_version_refuses_registration():
    """已发布版本必须拒写——否则注册等于绕过发布闸门改线上口径。"""
    async with _sandbox() as sandbox:
        codes = await _real_codes(1)
        user = f"t-{uuid.uuid4().hex[:10]}"

        created = await rfr.register_research_factors(
            sandbox.session, market="CN", dataset="private", codes=codes,
            user_id=user, version_name=f"t-{uuid.uuid4().hex[:8]}",
        )
        version_id = created["versions"][_TARGET_LIB]
        await sandbox.session.execute(
            text(
                "UPDATE qm_training_factor_catalog_version SET status='published' "
                "WHERE version_id = :v"
            ),
            {"v": version_id},
        )

        with pytest.raises(rfr.VersionNotDraft):
            await rfr.register_research_factors(
                sandbox.session, market="CN", dataset="private", codes=codes,
                user_id=user, version_id=version_id,
            )


# ---------------------------------------------------------------------------
# 跨库拒绝
# ---------------------------------------------------------------------------
@pytest.mark.integration
@pytest.mark.database
@pytest.mark.asyncio
async def test_cross_library_batch_refuses_pinned_draft():
    """钉住 l1_factors 草稿时，混入别库的因子必须整体拒绝，不能硬塞。"""
    async with _sandbox() as sandbox:
        meta = store.factors_meta("private") or {}
        other = next(
            (
                str(f["code"])
                for f in (meta.get("factors") or [])
                if str(f.get("l2") or "") not in ("", _TARGET_LIB)
                and str(f.get("l2") or "") not in EXCLUDED_FROM_TRAINING
            ),
            None,
        )
        if not other:
            pytest.skip("快照里找不到第二个可注册来源库")

        codes = await _real_codes(1)
        user = f"t-{uuid.uuid4().hex[:10]}"
        created = await rfr.register_research_factors(
            sandbox.session, market="CN", dataset="private", codes=codes,
            user_id=user, version_name=f"t-{uuid.uuid4().hex[:8]}",
        )

        with pytest.raises(rfr.SourceMismatch):
            await rfr.register_research_factors(
                sandbox.session, market="CN", dataset="private", codes=[*codes, other],
                user_id=user, version_id=created["versions"][_TARGET_LIB],
            )


# ---------------------------------------------------------------------------
# fail-closed
# ---------------------------------------------------------------------------
@pytest.mark.integration
@pytest.mark.database
@pytest.mark.asyncio
async def test_unknown_columns_are_refused_and_not_written(monkeypatch):
    """来源库字段未知时一律不写——放行会让错映射推迟到训练时才炸。

    草稿由用例自建并**显式钉住**（``create_catalog_draft`` 建的是空壳，不播种）：
    若不钉，注册会新建一份**从线上播种过**的草稿，而靶因子本来就都在线上，
    于是「草稿里有没有这几行」再也证明不了「注册写没写」。
    """
    async with _sandbox() as sandbox:
        codes = await _real_codes(2)
        user = f"t-{uuid.uuid4().hex[:10]}"
        tag = f"t-{uuid.uuid4().hex[:8]}"
        version_id = await qfc.create_catalog_draft(
            sandbox.session, _TARGET_LIB, tag, "CN", created_by=tag
        )

        async def _none(*_a, **_kw):
            return set()

        monkeypatch.setattr(rfr, "_discovered_fields", _none)

        out = await rfr.register_research_factors(
            sandbox.session, market="CN", dataset="private", codes=codes,
            user_id=user, version_id=version_id,
        )

        assert out["registered"] == []
        assert len(out["skipped"]) == 2
        assert await sandbox.count_mappings_for_codes(version_id, codes) == 0, (
            "fail-closed 之下仍写了行"
        )


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------
@pytest.mark.integration
@pytest.mark.database
@pytest.mark.asyncio
async def test_audit_row_lands_with_the_change():
    """审计行与映射行同事务落地，且带上版本号可回溯。"""
    async with _sandbox() as sandbox:
        codes = await _real_codes(2)
        user = f"t-{uuid.uuid4().hex[:10]}"

        out = await rfr.register_research_factors(
            sandbox.session, market="CN", dataset="private", codes=codes,
            user_id=user, version_name=f"t-{uuid.uuid4().hex[:8]}",
        )
        rows = await sandbox.audit_rows(user)

        assert len(rows) == 1, "一次注册应恰好留一条审计"
        assert rows[0]["success"] is True
        assert rows[0]["resource"] == rfr.AUDIT_RESOURCE
        assert out["versions"][_TARGET_LIB] in str(rows[0]["resource_id"])


@pytest.mark.integration
@pytest.mark.database
@pytest.mark.asyncio
async def test_all_skipped_still_leaves_an_audit_trail():
    """一个都没注册也要留痕——管理员动作必须可回溯。"""
    async with _sandbox() as sandbox:
        user = f"t-{uuid.uuid4().hex[:10]}"

        out = await rfr.register_research_factors(
            sandbox.session, market="CN", dataset="private",
            codes=["__definitely_not_a_factor__"], user_id=user,
        )
        rows = await sandbox.audit_rows(user)

        assert out["registered"] == []
        assert len(rows) == 1
        assert rows[0]["success"] is False


# ---------------------------------------------------------------------------
# 上限
# ---------------------------------------------------------------------------
@pytest.mark.integration
@pytest.mark.database
@pytest.mark.asyncio
async def test_over_max_codes_is_rejected_before_any_write():
    """超上限在写库前就拒绝：既不产生审计行，也不产生草稿版本。"""
    async with _sandbox() as sandbox:
        user = f"t-{uuid.uuid4().hex[:10]}"
        too_many = [f"c{i}" for i in range(rfr.MAX_CODES + 1)]
        before = await _scalar(
            sandbox.session, "SELECT count(*) FROM qm_training_factor_catalog_version"
        )

        with pytest.raises(rfr.TooManyCodes):
            await rfr.register_research_factors(
                sandbox.session, market="CN", dataset="private", codes=too_many,
                user_id=user,
            )

        assert await sandbox.audit_rows(user) == []
        after = await _scalar(
            sandbox.session, "SELECT count(*) FROM qm_training_factor_catalog_version"
        )
        assert after == before, "上限校验跑在找/建草稿之后了——拒绝请求却留下了草稿"


@pytest.mark.integration
@pytest.mark.database
@pytest.mark.asyncio
async def test_publish_landing_mid_flight_is_caught(monkeypatch):
    """发布闸门 TOCTOU 回归：状态检查与写行之间被 publish，必须拒写。

    开头那次 `status == 'draft'` 只查一次；而映射行的外键检查在 version 行上
    只取 `FOR KEY SHARE`，与 publish 的 `UPDATE`（`FOR NO KEY UPDATE`）**不冲突**
    —— 行锁天然挡不住这个窗口。不补那次「取行锁复核 draft」，映射会被静默写进
    已发布版本（绕过发布闸门、改掉线上训练口径），而响应仍报成功。

    这里把发布精确插在 `_lock_draft_scope` 返回之后（第一条 INSERT 之前），
    也就是真实的竞态窗口位置。跨事务，按 tag 清理。

    **刻意走裸 UPDATE 而不是 `publish_catalog_version`**：后者会把「同源同市场的
    旧版本自动归档」，那会动到线上已发布的 CN 目录版本。本用例要验的是我们这次
    复核，不是 publish 的语义；锁行为与真实发布一致（都是 version 行上的 UPDATE）。
    """
    from backend.shared.database_manager_v2 import close_database, get_session

    await _probe()
    codes = await _real_codes(2)
    tag = f"t-toctou-{uuid.uuid4().hex[:10]}"
    vid: str | None = None

    async with get_session() as ddl:
        await qfc._ensure_schema(ddl)
    async with get_session() as setup:  # 草稿必须先提交，才可能被另一连接发布
        vid = await qfc.create_catalog_draft(setup, _TARGET_LIB, tag, "CN", created_by=tag)

    original_lock = rfr._lock_draft_scope

    async def _lock_then_publish(session, *, market: str, source_dataset: str) -> None:
        await original_lock(session, market=market, source_dataset=source_dataset)
        async with get_session() as publisher:  # 独立连接，模拟另一个管理员发布
            await publisher.execute(
                text(
                    "UPDATE qm_training_factor_catalog_version SET status = 'published' "
                    "WHERE version_id = :v"
                ),
                {"v": vid},
            )

    monkeypatch.setattr(rfr, "_lock_draft_scope", _lock_then_publish)

    try:
        with pytest.raises(rfr.VersionNotDraft):
            async with get_session() as session:
                await rfr.register_research_factors(
                    session, market="CN", dataset="private", codes=codes,
                    user_id=tag, version_id=vid,
                )

        async with get_session() as check:
            written = (
                await check.execute(
                    text(
                        "SELECT count(*) FROM qm_training_factor_mapping "
                        "WHERE version_id = :v"
                    ),
                    {"v": vid},
                )
            ).scalar_one()
        assert written == 0, "映射被写进了已发布版本——发布闸门被绕过"
    finally:
        try:
            async with get_session() as session:
                await session.execute(
                    text("DELETE FROM qm_training_factor_mapping WHERE version_id = :v"),
                    {"v": vid},
                )
                await session.execute(
                    text("DELETE FROM qm_training_factor_catalog_version WHERE version_id = :v"),
                    {"v": vid},
                )
                await session.execute(
                    text("DELETE FROM user_audit_logs WHERE user_id = :u"), {"u": tag}
                )
        finally:
            await close_database()


@pytest.mark.integration
@pytest.mark.database
@pytest.mark.asyncio
async def test_audit_rolls_back_with_the_mappings():
    """审计行与映射行必须同生共死（MED-3 回归）。

    模块 docstring 写明「不使用 ``AuditLogService``」——那个服务在 ``log_action``
    内部自行 ``commit()``，会把这个事务提前提交。若有人图省事换回它，映射行照样
    随事务回滚，而审计行**已经被提交掉、留在库里**：一次并没落地的注册在审计里
    看着像成功了，还指向一个从未真正存在过的版本号（审计的价值全在于可信，
    这比没有审计更糟）。

    这条只有在真库上、且**跨越一次回滚**才验得出来：同事务内两者都可见（先断言
    这一点，否则测的只是「什么都没写」），回滚后再开一个新会话查，两者都必须
    不见——用 `_sandbox` 的单会话看不见回滚后的状态，所以这里自管事务。

    跨事务，按 tag 清理。**草稿必须由用例自己建、并显式钉住**（见下）。
    """
    from backend.shared.database_manager_v2 import close_database, get_session

    await _probe()
    codes = await _real_codes(2)
    tag = f"t-audit-{uuid.uuid4().hex[:10]}"

    async with get_session() as ddl:
        await qfc._ensure_schema(ddl)
    # 用例自有的空草稿，先提交——注册显式钉在它上面。
    # **不钉不行**：`_draft_version_id` 会复用 (market, source) 下**最新的存量草稿**，
    # 而 finally 的清理按 version_id 删。2026-10-07 线上实证：这条用例抓到管理员
    # 真实草稿（CN/l1_factors，20 行），断言 `count == len(codes)` 失败后 finally
    # 照常执行，把那份**生产草稿连同 20 条映射一起删了**——一次测试跑批毁掉真实
    # 数据。断言失败不能阻止 finally，所以防线只能在「只碰自己建的东西」上。
    async with get_session() as setup:
        version_id = await qfc.create_catalog_draft(
            setup, _TARGET_LIB, tag, "CN", created_by=tag
        )

    try:
        async with get_session() as session:
            out = await rfr.register_research_factors(
                session, market="CN", dataset="private", codes=codes,
                user_id=tag, version_id=version_id,
            )
            assert out["versions"][_TARGET_LIB] == version_id

            # 回滚前：两者都在本事务内可见（前置断言，防止用例退化成空测）
            assert await _mapping_count(session, version_id) == len(codes)
            assert len(await _audit_rows(session, tag)) == 1

            await session.rollback()  # 模拟端点中途失败

        async with get_session() as after:
            assert await _mapping_count(after, version_id) == 0, "映射行没随回滚消失"
            assert await _audit_rows(after, tag) == [], (
                "审计行被独立提交了——一次并未落地的注册在审计里看着像成功了"
            )
    finally:
        try:
            async with get_session() as session:
                if version_id:
                    await session.execute(
                        text("DELETE FROM qm_training_factor_mapping WHERE version_id = :v"),
                        {"v": version_id},
                    )
                await session.execute(
                    text(
                        "DELETE FROM qm_training_factor_catalog_version "
                        "WHERE version_id = :v OR version_name = :n"
                    ),
                    {"v": version_id, "n": tag},
                )
                await session.execute(
                    text("DELETE FROM user_audit_logs WHERE user_id = :u"), {"u": tag}
                )
        finally:
            await close_database()


# ---------------------------------------------------------------------------
# 播种：发布不得缩口径
# ---------------------------------------------------------------------------
async def _enabled_keys(session, version_id: str) -> set[str]:
    rows = (
        await session.execute(
            text(
                "SELECT feature_key FROM qm_training_factor_mapping "
                "WHERE version_id = :v AND enabled"
            ),
            {"v": version_id},
        )
    ).scalars().all()
    return {str(r) for r in rows}


async def _drop_drafts(session, source_dataset: str) -> None:
    """沙箱内清掉某来源库的草稿，逼注册走「新建」分支（回滚后原样还在）。"""
    await session.execute(
        text(
            "DELETE FROM qm_training_factor_mapping WHERE version_id IN ("
            "  SELECT version_id FROM qm_training_factor_catalog_version"
            "  WHERE market = 'CN' AND source_dataset = :d AND status = 'draft')"
        ),
        {"d": source_dataset},
    )
    await session.execute(
        text(
            "DELETE FROM qm_training_factor_catalog_version "
            "WHERE market = 'CN' AND source_dataset = :d AND status = 'draft'"
        ),
        {"d": source_dataset},
    )


@pytest.mark.integration
@pytest.mark.database
@pytest.mark.asyncio
async def test_registration_draft_is_seeded_from_published():
    """注册新建的草稿必须**先从线上抄一份**——否则「发布」会把线上口径砍掉。

    2026-10-07 线上实证：``factor_defs`` 线上 1336 个启用特征，而注册建出的草稿
    里只有刚注册的那 2 个。因为「发布」的语义是**替换**（旧版转 ``archived``、
    草稿原样扶正，``publish_catalog_version``），中间**没有合并**这一步：线上
    不是变成 1336+2，而是变成 2。管理员点「注册」的意图显然是「把这些因子加进
    训练口径」，不是「把训练口径换成这几个」。

    钉住的性质：**新建草稿的启用集 ⊇ 线上那份的启用集**。管理者手工关闭过的
    特征也要照抄（它们是「线上现状」的一部分），所以这里比的是 enabled 侧。
    """
    async with _sandbox() as sandbox:
        session = sandbox.session
        published_id = (
            await session.execute(
                text(
                    "SELECT version_id FROM qm_training_factor_catalog_version "
                    "WHERE market = 'CN' AND source_dataset = :d AND status = 'published'"
                ),
                {"d": _TARGET_LIB},
            )
        ).scalars().first()
        if not published_id:
            pytest.skip(f"CN/{_TARGET_LIB} 没有已发布版本，无从播种")
        online = await _enabled_keys(session, str(published_id))
        assert online, "线上版本没有启用特征，用例失去意义"

        await _drop_drafts(session, _TARGET_LIB)  # 沙箱内，回滚后草稿原样还在

        codes = await _real_codes(2)
        out = await rfr.register_research_factors(
            session, market="CN", dataset="private", codes=codes,
            user_id=f"t-{uuid.uuid4().hex[:10]}",
            version_name=f"t-{uuid.uuid4().hex[:8]}",
        )
        draft_id = out["versions"][_TARGET_LIB]
        assert draft_id != str(published_id), "注册写进了线上版本"
        draft_enabled = await _enabled_keys(session, draft_id)

        missing = online - draft_enabled
        assert not missing, (
            f"新建草稿丢了线上 {len(missing)} 个启用特征"
            f"（例：{sorted(missing)[:5]}）——发布这份草稿会把线上口径缩掉"
        )
        registered = {str(r["feature_key"]) for r in out["registered"]}
        assert registered, "本次一个都没注册上，用例失去意义"
        assert registered <= draft_enabled, "本次注册的因子没进草稿"


@pytest.mark.integration
@pytest.mark.database
@pytest.mark.asyncio
async def test_registration_without_published_degrades_to_blank_draft():
    """没有线上版本可抄时退化为空草稿——不许因此报错或凭空造行。

    播种逻辑必须「找不到就跳过」，而不是假定线上那份一定存在：来源库第一次
    注册时就是这种状态（本省 CN 当前没有这样的库，所以用例自己把已发布版本
    删掉来构造——沙箱内，回滚后原样还在）。
    """
    async with _sandbox() as sandbox:
        session = sandbox.session
        await session.execute(
            text(
                "DELETE FROM qm_training_factor_mapping WHERE version_id IN ("
                "  SELECT version_id FROM qm_training_factor_catalog_version"
                "  WHERE market = 'CN' AND source_dataset = :d AND status = 'published')"
            ),
            {"d": _TARGET_LIB},
        )
        await session.execute(
            text(
                "DELETE FROM qm_training_factor_catalog_version "
                "WHERE market = 'CN' AND source_dataset = :d AND status = 'published'"
            ),
            {"d": _TARGET_LIB},
        )
        await _drop_drafts(session, _TARGET_LIB)

        codes = await _real_codes(2)
        out = await rfr.register_research_factors(
            session, market="CN", dataset="private", codes=codes,
            user_id=f"t-{uuid.uuid4().hex[:10]}",
            version_name=f"t-{uuid.uuid4().hex[:8]}",
        )
        draft_id = out["versions"][_TARGET_LIB]

        assert await _mapping_count(session, draft_id) == len(codes), (
            "没有线上版本可抄时，草稿里应当只有本次注册的行"
        )


# ---------------------------------------------------------------------------
# 并发
# ---------------------------------------------------------------------------
@pytest.mark.integration
@pytest.mark.database
@pytest.mark.asyncio
async def test_concurrent_registration_creates_a_single_draft():
    """两个并发注册只能产出一份草稿（advisory lock 串行化「找/建」）。

    同时是**死锁回归**：曾把 `_ensure_schema` 和注册放在同一事务里，两个并发请求
    对「version 表 DDL 锁」与「mapping 表 DML 锁」形成 ABBA 环，PostgreSQL 判定
    死锁杀掉其一（线上表现：两个管理员同时点注册，一个拿 500）。DDL 现已在独立
    事务中先落地，这里按同形状复现——若回归，本用例会以 DeadlockDetectedError 挂掉。

    本用例跨事务，无法靠回滚清理——用唯一版本名标记，finally 里删净。

    **前置条件必须在任何写入之前查、并立即 skip**：`_draft_version_id` 只要
    (market, source_dataset) 已经有草稿就直接复用，并**忽略**传入的 `version_name`
    ——`va == vb` 会退化成恒真（测不出 advisory lock 被摘掉）。存量草稿是生产状态的
    常态，所以本用例在真库上通常**跑不起来**，那就当场跳过，一行都不许写。

    **绝不允许「先写进去、再在 finally 里擦掉」**：擦除会默认
    「本次注册的 key == 本次新建的行」，而注册是 upsert（``ON CONFLICT DO UPDATE``）
    ——key 已存在时不新建任何行，照 key 删就是删存量行。2026-10-07 线上实证：
    本条用例借用 CN/l1_factors 真实草稿，注册的 2 个 key（`amt_close_pos`、
    `amt_high_days_10`）早已由线上播种存在，finally 按 key 删，把 2 条生产映射
    删了；而且 skip 是**异常**，`finally` 照样执行——「跳过」与「删数据」同时发生。

    **清理只许碰自己建的东西**：探到无存量草稿才继续，此时 `created` 里的 id 必然
    都是本用例自建的，整份删安全。`borrowed` 分支只作纵深防御（探查到注册之间有
    极小竞态窗口），且只删**本次确实新建的行**（``ours`` = 注册得到的 key −
    注册前该草稿已有的 key）。
    """
    from backend.shared.database_manager_v2 import close_database, get_session

    await _probe()
    codes = await _real_codes(2)
    tag = f"t-conc-{uuid.uuid4().hex[:10]}"
    created: set[str] = set()
    ours: list[str] = []
    # 先赋默认值：下面 DDL/探查若抛错，finally 仍会引用它们。
    preexisting: set[str] = set()
    preexisting_keys: set[str] = set()

    async def _run(user_suffix: str):
        async with get_session() as session:
            return await rfr.register_research_factors(
                session, market="CN", dataset="private", codes=codes,
                user_id=f"{tag}-{user_suffix}", version_name=tag,
            )

    try:
        async with get_session() as ddl:  # 与端点一致：DDL 先独立提交
            await qfc._ensure_schema(ddl)
        async with get_session() as probe:
            preexisting = await _draft_ids(probe, _TARGET_LIB)
            preexisting_keys = await _mapping_keys(probe, preexisting)

        if preexisting:  # 写入之前就跳——见 docstring
            pytest.skip(
                f"CN/{_TARGET_LIB} 已有存量草稿（{len(preexisting)} 份），本用例无"
                "回归能力；且写入后再清理会误删存量行。请在无草稿环境跑。"
            )

        a, b = await asyncio.gather(_run("a"), _run("b"))
        va = a["versions"][_TARGET_LIB]
        vb = b["versions"][_TARGET_LIB]
        created.update({va, vb})
        # 只认「注册前不存在的 key」＝本次真正新建的行
        ours.extend(
            str(r["feature_key"])
            for r in a["registered"]
            if str(r["feature_key"]) not in preexisting_keys
        )

        assert va == vb, f"并发注册产出了两份草稿：{va} / {vb}"
    finally:
        try:
            async with get_session() as session:
                borrowed = created & preexisting
                owned = created - preexisting
                if owned:
                    await session.execute(
                        text(
                            "DELETE FROM qm_training_factor_mapping "
                            "WHERE version_id = ANY(:ids)"
                        ),
                        {"ids": list(owned)},
                    )
                    await session.execute(
                        text(
                            "DELETE FROM qm_training_factor_catalog_version "
                            "WHERE version_id = ANY(:ids)"
                        ),
                        {"ids": list(owned)},
                    )
                if borrowed and ours:  # 借用的草稿：只删本次**新建**的那几行
                    await session.execute(
                        text(
                            "DELETE FROM qm_training_factor_mapping "
                            "WHERE version_id = ANY(:ids) AND feature_key = ANY(:keys)"
                        ),
                        {"ids": list(borrowed), "keys": ours},
                    )
                await session.execute(
                    text("DELETE FROM user_audit_logs WHERE user_id LIKE :p"),
                    {"p": f"{tag}%"},
                )
        finally:
            await close_database()
