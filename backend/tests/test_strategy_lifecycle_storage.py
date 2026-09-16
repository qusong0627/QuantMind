"""T-P3-01/T-P3-02 测试：策略存储状态机接线 + 版本/参数锁 + 删除守卫（真库 E2E）。

覆盖：
1. 接线源断言：启动/停止状态回写、启动门禁、update 端点 409、API 状态映射扩展；
2. 纯函数：API 层 _normalize_base_status 兼容新词表；
3. **真库 E2E**（user_id="1"，唯一名 + 用后清理）：
   - save 建 DRAFT v1 → get 返回 status/version；
   - 局部更新不抹 execution_config（补丁语义）；代码变化 → version+1；
   - 运行中（SIM）参数锁：无 expected_version 拒绝 / 版本不符冲突 / 正确版本放行；
   - update_lifecycle_status 合法迁移放行、非法迁移拒绝、同状态幂等；
   - delete：正常删除 True；不存在 False；运行中（SIM）拒绝。
"""

from __future__ import annotations

from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]

_NAME_PREFIX = "_tp301_e2e_"


def _unique_name(tag: str) -> str:
    from datetime import datetime

    return f"{_NAME_PREFIX}{tag}_{datetime.now().strftime('%H%M%S%f')}"


# ── 接线源断言 ──────────────────────────────────────────────────────


@pytest.mark.unit
def test_start_stop_wired_with_writeback_and_gate():
    src = (
        _BACKEND / "services/live_trading/routers/real_trading_lifecycle.py"
    ).read_text(encoding="utf-8")
    # 回写接线（此前 helper 已建、全仓无调用方）
    assert '_schedule_status_writeback(' in src
    assert 'lifecycle_status="SIM" if mode == "SIMULATION" else "LIVE"' in src
    assert 'lifecycle_status="VERIFIED"' in src
    # 启动门禁
    assert "can_start(detail.get(\"status\"), mode)" in src
    assert "from .real_trading_utils import (" in src
    assert "_schedule_status_writeback," in src


@pytest.mark.unit
def test_update_endpoint_plumbs_expected_version_and_conflict():
    src = (
        _BACKEND / "services/engine/qlib_app/api/user_strategies.py"
    ).read_text(encoding="utf-8")
    assert "expected_version: int | None = Field(" in src
    assert "expected_version=body.expected_version" in src
    assert "status_code=409" in src
    assert "StrategyLockedError" in src and "VersionConflictError" in src


@pytest.mark.unit
def test_api_base_status_mapping_covers_new_vocab():
    from backend.services.engine.qlib_app.api.user_strategies import (
        _normalize_base_status,
    )

    # 存量词表行为不变
    assert _normalize_base_status("ACTIVE") == "repository"
    assert _normalize_base_status("DRAFT") == "draft"
    assert _normalize_base_status("live_trading") == "live_trading"
    # T-P3-01 新词表 → 前端既有可见值
    assert _normalize_base_status("VERIFIED") == "repository"
    assert _normalize_base_status("SIM") == "live_trading"
    assert _normalize_base_status("LIVE") == "live_trading"
    assert _normalize_base_status(None) == "draft"


# ── 真库 E2E ────────────────────────────────────────────────────────


async def _cleanup() -> None:
    try:
        from sqlalchemy import text

        from backend.shared.database_manager_v2 import get_session

        async with get_session(read_only=False) as session:
            await session.execute(
                text("DELETE FROM strategies WHERE name LIKE :p"),
                {"p": f"{_NAME_PREFIX}%"},
            )
            await session.commit()
    except Exception:  # noqa: BLE001
        pass


async def _set_status_raw(strategy_id: str, status: str) -> None:
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=False) as session:
        await session.execute(
            text("UPDATE strategies SET status = :s WHERE id = :sid"),
            {"s": status, "sid": int(strategy_id)},
        )
        await session.commit()


@pytest.mark.asyncio
async def test_strategy_lifecycle_e2e_version_lock_and_delete():
    try:
        from sqlalchemy import text  # noqa: F401

        from backend.shared.database_manager_v2 import get_session  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"依赖不可用: {exc}")

    from backend.shared.strategy_lifecycle import (
        STATUS_SIM,
        StrategyLockedError,
        VersionConflictError,
    )
    from backend.shared.strategy_storage import get_strategy_storage_service

    await _cleanup()
    svc = get_strategy_storage_service()
    try:
        # ① 建：DRAFT v1，get 返回 status/version
        created = await svc.save(
            user_id="1",
            name=_unique_name("main"),
            code="print('v1')",
            metadata={
                "execution_config": {"max_buy_drop": -0.04, "stop_loss": -0.07},
                "parameters": {"topk": 5},
            },
        )
        sid = created["id"]
        got = await svc.get(sid, user_id="1")
        assert got["status"] == "DRAFT" and got["version"] == 1
        assert got["execution_config"]["stop_loss"] == -0.07

        # ② 局部更新（不带 execution_config）→ 不抹执行配置；代码变 → version+1
        await svc.save(
            user_id="1",
            name=_unique_name("main2"),
            code="print('v2')",
            metadata={"parameters": {"topk": 5}},  # description/config/exec 均未显式给
            strategy_id=sid,
        )
        got = await svc.get(sid, user_id="1")
        assert got["version"] == 2, "代码变化应递增版本"
        assert got["execution_config"]["stop_loss"] == -0.07, "补丁语义不得抹执行配置"

        # ③ 内容不变 → 不再递增
        await svc.save(
            user_id="1",
            name=got["name"],
            code="print('v2')",
            metadata={"parameters": {"topk": 5}},
            strategy_id=sid,
        )
        got = await svc.get(sid, user_id="1")
        assert got["version"] == 2, "无实质变化不应递增版本"

        # ④ 运行中参数锁：SIM 状态改代码必须显式升版本
        await _set_status_raw(sid, "SIM")
        with pytest.raises(StrategyLockedError):
            await svc.save(
                user_id="1",
                name=got["name"],
                code="print('v3')",
                metadata={"parameters": {"topk": 5}},
                strategy_id=sid,
            )
        with pytest.raises(VersionConflictError):
            await svc.save(
                user_id="1",
                name=got["name"],
                code="print('v3')",
                metadata={"parameters": {"topk": 5}},
                strategy_id=sid,
                expected_version=99,
            )
        await svc.save(
            user_id="1",
            name=got["name"],
            code="print('v3')",
            metadata={"parameters": {"topk": 5}},
            strategy_id=sid,
            expected_version=2,
        )
        got = await svc.get(sid, user_id="1")
        assert got["version"] == 3

        # ⑤ 迁移校验：SIM→DRAFT 非法；SIM→VERIFIED 合法
        assert svc.update_lifecycle_status(sid, "1", "DRAFT") is False
        assert svc.update_lifecycle_status(sid, "1", "VERIFIED") is True
        assert svc.update_lifecycle_status(sid, "1", "VERIFIED") is True  # 幂等

        # ⑥ 删除守卫：DRAFT 可删→True；再删→False；SIM 拒绝
        await _set_status_raw(sid, "DRAFT")
        assert await svc.delete(sid, user_id="1") is True
        assert await svc.delete(sid, user_id="1") is False

        created2 = await svc.save(
            user_id="1", name=_unique_name("guard"), code="print('x')"
        )
        sid2 = created2["id"]
        await _set_status_raw(sid2, "SIM")
        with pytest.raises(ValueError) as exc:
            await svc.delete(sid2, user_id="1")
        assert "运行" in str(exc.value)
        # 归档行可清理（旧实现经 get() 过滤归档 → 永远删不掉）
        await _set_status_raw(sid2, "ARCHIVED")
        assert await svc.delete(sid2, user_id="1") is True
    finally:
        # 清理所有测试残留
        await _cleanup()


@pytest.mark.asyncio
async def test_update_lifecycle_status_transition_matrix_e2e():
    """迁移矩阵（真库）：DRAFT→LIVE 拒绝 / DRAFT→VERIFIED 通过 / 合法链 VERIFIED→SIM→LIVE→VERIFIED。"""
    try:
        from backend.shared.database_manager_v2 import get_session  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"依赖不可用: {exc}")

    from backend.shared.strategy_storage import get_strategy_storage_service

    await _cleanup()
    svc = get_strategy_storage_service()
    try:
        created = await svc.save(
            user_id="1", name=_unique_name("matrix"), code="print('m')"
        )
        sid = created["id"]
        assert svc.update_lifecycle_status(sid, "1", "LIVE") is False  # 跨级拒绝
        assert svc.update_lifecycle_status(sid, "1", "VERIFIED") is True
        assert svc.update_lifecycle_status(sid, "1", "SIM") is True
        assert svc.update_lifecycle_status(sid, "1", "LIVE") is True
        assert svc.update_lifecycle_status(sid, "1", "SIM") is False  # 回退拒绝
        assert svc.update_lifecycle_status(sid, "1", "VERIFIED") is True
        got = await svc.get(sid, user_id="1")
        assert got["status"] == "VERIFIED"
    finally:
        await _cleanup()


@pytest.mark.unit
def test_writeback_helper_accepts_new_vocab():
    """回写通道的入参语义（下游 update_lifecycle_status 归一）。"""
    from backend.shared.strategy_lifecycle import normalize_status

    assert normalize_status("SIM") == "SIM"
    assert normalize_status("live_trading") == "LIVE"  # 兼容旧入参


@pytest.mark.asyncio
async def test_mark_as_verified_transitions_draft_to_verified():
    """T-P3-04：回测验证标记与状态机联动——DRAFT→VERIFIED（消除 is_verified/status 分裂），
    已运行状态不降级，目标不存在行数诚实。"""
    try:
        from backend.shared.database_manager_v2 import get_session  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"依赖不可用: {exc}")

    from backend.shared.strategy_storage import get_strategy_storage_service

    await _cleanup()
    svc = get_strategy_storage_service()
    try:
        created = await svc.save(
            user_id="1", name=_unique_name("verify"), code="print('v')"
        )
        sid = created["id"]
        got = await svc.get(sid, user_id="1")
        assert got["status"] == "DRAFT" and got["is_verified"] is False

        ok = await svc.mark_as_verified(sid, "1")
        assert ok is True
        got = await svc.get(sid, user_id="1")
        assert got["status"] == "VERIFIED", "DRAFT 应联动迁移 VERIFIED（T-P3-04）"
        assert got["is_verified"] is True
        # 幂等：重复标记不炸
        assert await svc.mark_as_verified(sid, "1") is True

        # 运行中（SIM）重复回测不得降级
        await _set_status_raw(sid, "SIM")
        assert await svc.mark_as_verified(sid, "1") is True
        got = await svc.get(sid, user_id="1")
        assert got["status"] == "SIM"

        # 不存在的目标 → False（此前无条件 True）
        assert await svc.mark_as_verified("999999999", "1") is False
    finally:
        await _cleanup()


@pytest.mark.unit
def test_ai_ide_executor_triggers_verified_on_success():
    """T-P3-04 接线源断言：AI-IDE 回测成功路径调用验证标记（数字 strategy_id 守卫）。"""
    src = (
        _BACKEND / "services/engine/routers/ai_ide/executor.py"
    ).read_text(encoding="utf-8")
    assert "_mark_strategy_verified_on_success(strategy_id)" in src
    assert "mark_as_verified" in src
    # 仅数字 strategy_id 触发（sys_ 模板/无策略代码不受影响）
    assert 'sid.isdigit()' in src
    # 失败不阻断回测结果
    assert "不阻断回测结果" in src
