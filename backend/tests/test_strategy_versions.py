"""T-FE-10 测试：策略版本历史（快照契约 + 同事务写入 + 幂等 + /versions 端点）。

真库 E2E：建策略（v1）→ 改参（v2）→ 无变化保存（不增版本）→ 端点读取（含内容开关）→ 清理。
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException

_BACKEND = Path(__file__).resolve().parents[1]


@pytest.mark.unit
def test_version_contract_safety_discipline():
    src = (_BACKEND / "shared/strategy_version_contract.py").read_text(encoding="utf-8")
    # 安全三纪律（P1 铁律）：预检零 DDL 快路径 + lock_timeout + 失败不阻断
    assert "to_regclass" in src
    assert "lock_timeout" in src
    assert "不阻断" in src
    assert "ON CONFLICT (strategy_id, version) DO NOTHING" in src  # 幂等

    storage_src = (_BACKEND / "shared/strategy_storage.py").read_text(encoding="utf-8")
    # 两条路径都接线（UPDATE 内容变更 + INSERT 首版）
    assert storage_src.count("record_strategy_version(") == 2


@pytest.mark.asyncio
async def test_strategy_versions_real_db_flow():
    try:
        from sqlalchemy import text

        from backend.shared.database_manager_v2 import get_session
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"依赖不可用: {exc}")
    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(text("SELECT 1"))
    except Exception:
        from backend.shared.database_manager_v2 import close_database

        await close_database()
        try:
            async with get_session(read_only=True) as probe:
                await probe.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"DB 连接抖动: {exc}")

    from backend.shared.strategy_storage import get_strategy_storage_service

    svc = get_strategy_storage_service()
    user_id = "1"
    name = f"pytest版本历史_{uuid.uuid4().hex[:6]}"
    code_v1 = "def handle_data(ctx):\n    return 'v1'\n"
    code_v2 = "def handle_data(ctx):\n    return 'v2'\n"
    strategy_id: str | None = None

    def _query_versions() -> list[dict]:
        from backend.shared.strategy_storage import _ensure_int_user_id, get_db

        with get_db() as session:
            rows = session.execute(
                text(
                    "SELECT version, code, parameters FROM strategy_versions "
                    "WHERE strategy_id = :sid AND user_id = :uid ORDER BY version"
                ),
                {"sid": int(strategy_id), "uid": _ensure_int_user_id(user_id)},
            ).mappings().all()
        return [dict(r) for r in rows]

    try:
        created = await svc.save(
            user_id=user_id,
            name=name,
            code=code_v1,
            metadata={"parameters": {"topk": 5}, "strategy_type": "CUSTOM"},
        )
        strategy_id = str(created.get("id") if isinstance(created, dict) else created)
        assert strategy_id.isdigit()

        versions = await asyncio.to_thread(_query_versions)
        assert len(versions) == 1 and versions[0]["version"] == 1
        assert "v1" in versions[0]["code"]
        assert versions[0]["parameters"].get("topk") == 5

        # 改参 + 改码 → v2（同事务快照，含最终生效参数）
        await svc.save(
            user_id=user_id,
            name=name,
            code=code_v2,
            metadata={"parameters": {"topk": 10}, "strategy_type": "CUSTOM"},
            strategy_id=strategy_id,
        )
        versions = await asyncio.to_thread(_query_versions)
        assert len(versions) == 2 and versions[1]["version"] == 2
        assert "v2" in versions[1]["code"] and versions[1]["parameters"].get("topk") == 10
        # v1 快照不被覆写（历史留档）
        assert "v1" in versions[0]["code"] and versions[0]["parameters"].get("topk") == 5

        # 内容无变化 → 不升版不增快照（幂等）
        await svc.save(
            user_id=user_id,
            name=name,
            code=code_v2,
            metadata={"parameters": {"topk": 10}, "strategy_type": "CUSTOM"},
            strategy_id=strategy_id,
        )
        versions = await asyncio.to_thread(_query_versions)
        assert len(versions) == 2, "无变化保存不应新增版本快照"

        # 端点：默认带内容、降序；include_content=false 时无 code 字段
        from backend.services.engine.qlib_app.api import user_strategies as us

        monkeypatched = False
        original = us._get_user_id
        us._get_user_id = lambda request: user_id  # type: ignore[assignment]
        monkeypatched = True
        try:
            resp = await us.list_strategy_versions(strategy_id, request=None, limit=10, include_content=True)
            got = resp["data"]["versions"]
            assert [v["version"] for v in got] == [2, 1]
            assert got[0]["code"] and got[0]["parameters"].get("topk") == 10

            resp_meta = await us.list_strategy_versions(strategy_id, request=None, limit=10, include_content=False)
            assert all("code" not in v for v in resp_meta["data"]["versions"])

            with pytest.raises(HTTPException) as exc:
                await us.list_strategy_versions("abc", request=None, limit=10, include_content=True)
            assert exc.value.status_code == 400
        finally:
            if monkeypatched:
                us._get_user_id = original  # type: ignore[assignment]
    finally:
        from backend.shared.strategy_storage import get_db

        def _cleanup() -> None:
            with get_db() as session:
                if strategy_id:
                    session.execute(
                        text("DELETE FROM strategy_versions WHERE strategy_id = :sid"),
                        {"sid": int(strategy_id)},
                    )
                    session.execute(
                        text("DELETE FROM strategies WHERE id = :sid"), {"sid": int(strategy_id)}
                    )
                else:
                    session.execute(
                        text("DELETE FROM strategies WHERE name = :n"), {"n": name}
                    )

        await asyncio.to_thread(_cleanup)
        from backend.shared.database_manager_v2 import close_database

        await close_database()
