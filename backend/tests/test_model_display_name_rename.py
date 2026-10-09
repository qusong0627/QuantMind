"""用户模型展示名重命名测试。

背景（2026-10-09 用户诉求）：广场导入的模型 model_id 是机器名（如
`mdl_cn_hub_T_10_LightGBM_d899b0_0792`），模型一多「不知道是干啥的」。
需要能改展示名——模型管理列表 / 详情 / 滚动训练派生描述都要跟着变。

口径：
  - 双写：DB `qm_user_models.metadata_json` 与磁盘 `<model_dir>/metadata.json`
    的 `display_name` 与 `model_name` 同步更新（前端列表读 DB 那份；滚动训练
    派生描述 `model_recipe.py` 读磁盘那份的 `model_name`），其余键（hub_name、
    market 等）原样保留；
  - **model_id 不动**（滚动训练校验器仅允许 ASCII，改 ID 会动摇台账引用）；
  - 系统模型（metadata.readonly）拒绝；
  - 幂等：改回同名 = 空操作（不写盘、不落库、updated_at 不动）；
  - 归属：不存在 / 他人模型 → None（路由层 404）。

注意：异步测试全程单协程（一段测试一个 `@pytest.mark.asyncio`），不可在
同一测试里多次 `asyncio.run`——全局连接池会把连接跨已关闭 loop 复用。

运行（容器内）：
    python3 -m pytest backend/tests/test_model_display_name_rename.py -q
"""

from __future__ import annotations

import json
import shutil
import time
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

TENANT = "t-rename-display"
USER = "1"

_OLD_NAME = "旧展示名"
_OLD_META = {
    "display_name": _OLD_NAME,
    "model_name": _OLD_NAME,
    "hub_name": "广场公开名",
    "market": "CN",
    "feature_count": 3,
    "model_type": "LightGBM",
}


async def _make_model(*, tenant: str = TENANT, user: str = USER, readonly: bool = False,
                      with_disk: bool = True, status: str = "ready") -> dict:
    """建一条真实测试模型：user_models_root 下真实目录 + DB 行。

    测试写入真实 PG（t- 前缀租户，消费侧按 strpos 排除），teardown 全清。
    """
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session
    from backend.shared.model_registry import model_registry_service

    model_id = f"mdl_t_rename_{uuid.uuid4().hex[:8]}"
    meta = dict(_OLD_META)
    if readonly:
        meta["readonly"] = True
    root = Path(model_registry_service.user_models_root)
    model_dir = root / tenant / user / model_id

    if with_disk:
        model_dir.mkdir(parents=True, exist_ok=True)
        (model_dir / "metadata.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    async with get_session() as session:
        await session.execute(
            text(
                """
                INSERT INTO qm_user_models (
                    tenant_id, user_id, model_id, status, storage_path,
                    metadata_json, is_default
                ) VALUES (
                    :tenant, :user, :model_id, :status, :storage_path,
                    CAST(:metadata AS JSONB), FALSE
                )
                """
            ),
            {
                "tenant": tenant,
                "user": user,
                "model_id": model_id,
                "status": status,
                "storage_path": str(model_dir),
                "metadata": json.dumps(meta, ensure_ascii=False),
            },
        )
    return {
        "tenant": tenant,
        "user": user,
        "model_id": model_id,
        "storage_path": model_dir,
        "meta": meta,
    }


async def _cleanup(env: dict) -> None:
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    async with get_session() as session:
        await session.execute(
            text(
                """
                DELETE FROM qm_user_models
                WHERE tenant_id = :t AND user_id = :u AND model_id = :m
                """
            ),
            {"t": env["tenant"], "u": env["user"], "m": env["model_id"]},
        )
    path = Path(env["storage_path"])
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
        parent = path.parent
        try:
            if parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            pass


async def _teardown(env: dict) -> None:
    """清行清目录 + 关连接池：池与事件循环绑定，不关会连累同文件下一个真库测试。"""
    from backend.shared.database_manager_v2 import close_database

    try:
        await _cleanup(env)
    finally:
        await close_database()


async def _db_meta(env: dict) -> dict:
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=True) as session:
        row = (
            (
                await session.execute(
                    text(
                        """
                        SELECT metadata_json FROM qm_user_models
                        WHERE tenant_id = :t AND user_id = :u AND model_id = :m
                        """
                    ),
                    {"t": env["tenant"], "u": env["user"], "m": env["model_id"]},
                )
            )
            .mappings()
            .first()
        )
    return dict(row["metadata_json"]) if row else {}


def _disk_meta(env: dict) -> dict:
    p = Path(env["storage_path"]) / "metadata.json"
    return json.loads(p.read_text(encoding="utf-8"))


async def _rename(env: dict, name: str, *, user: str | None = None):
    from backend.shared.model_registry import model_registry_service

    return await model_registry_service.update_display_name(
        tenant_id=env["tenant"],
        user_id=user if user is not None else env["user"],
        model_id=env["model_id"],
        display_name=name,
    )


# ───────────────────────── 服务层：双写 ─────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rename_updates_db_and_disk_keeps_other_keys():
    env = await _make_model()
    try:
        got = await _rename(env, "沪深300增强-低波")

        assert got is not None
        # 返回记录即读回后的 DB 口径
        assert got["metadata_json"]["display_name"] == "沪深300增强-低波"
        assert got["metadata_json"]["model_name"] == "沪深300增强-低波"

        # DB 侧回读
        db = await _db_meta(env)
        assert db["display_name"] == "沪深300增强-低波"
        assert db["model_name"] == "沪深300增强-低波"
        # 其余键原样保留
        assert db["hub_name"] == "广场公开名"
        assert db["market"] == "CN"
        assert db["feature_count"] == 3

        # 磁盘侧回读（滚动训练派生描述读这份 model_name）
        disk = _disk_meta(env)
        assert disk["display_name"] == "沪深300增强-低波"
        assert disk["model_name"] == "沪深300增强-低波"
        assert disk["hub_name"] == "广场公开名"
    finally:
        await _teardown(env)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rename_strips_whitespace():
    env = await _make_model()
    try:
        got = await _rename(env, "  低波红利  ")
        assert got["metadata_json"]["display_name"] == "低波红利"
        assert _disk_meta(env)["model_name"] == "低波红利"
    finally:
        await _teardown(env)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rename_idempotent_same_name_is_noop():
    env = await _make_model()
    try:
        first = await _rename(env, "低波红利")
        meta_path = Path(env["storage_path"]) / "metadata.json"
        before_bytes = meta_path.read_bytes()
        before_updated_at = first["updated_at"]

        time.sleep(0.05)
        second = await _rename(env, "低波红利")

        assert second is not None
        assert meta_path.read_bytes() == before_bytes
        assert second["updated_at"] == before_updated_at  # 未落库
    finally:
        await _teardown(env)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rename_rejects_invalid_names():
    env = await _make_model()
    try:
        for bad in ("", "   ", "\n", "a\nb", "x" * 81, "nul\x00byte"):
            with pytest.raises(ValueError):
                await _rename(env, bad)
        # 未写入：原名不变
        assert (await _db_meta(env))["display_name"] == _OLD_NAME
        assert _disk_meta(env)["display_name"] == _OLD_NAME
    finally:
        await _teardown(env)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rename_rejects_system_model():
    env = await _make_model(readonly=True)
    try:
        with pytest.raises(ValueError):
            await _rename(env, "不许改")
    finally:
        await _teardown(env)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rename_missing_or_foreign_model_returns_none():
    env = await _make_model()
    try:
        from backend.shared.model_registry import model_registry_service

        # 不存在的模型
        gone = await model_registry_service.update_display_name(
            tenant_id=TENANT, user_id=USER,
            model_id="mdl_t_rename_doesnotexist", display_name="x",
        )
        assert gone is None
        # 他人（同租户不同用户）不可见
        foreign = await _rename(env, "越权", user="999")
        assert foreign is None
        assert (await _db_meta(env))["display_name"] == _OLD_NAME
    finally:
        await _teardown(env)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rename_missing_disk_dir_still_updates_db():
    env = await _make_model()
    try:
        shutil.rmtree(env["storage_path"])  # 磁盘目录丢失（模型资产已坏）
        got = await _rename(env, "还能改名")
        assert got is not None
        assert (await _db_meta(env))["display_name"] == "还能改名"
    finally:
        await _teardown(env)


# ───────────────────────── 路由层：状态码映射 ─────────────────────────


_USER = {"tenant_id": "default", "user_id": "10000001", "sub": "10000001"}


def _client(monkeypatch, fake):
    from backend.services.api.routers import model_training as mt
    from backend.services.api.user_app.middleware.auth import get_current_user

    monkeypatch.setattr(mt.model_registry_service, "update_display_name", fake, raising=True)

    app = FastAPI()
    app.include_router(mt.router, prefix="/api/v1/models")
    app.dependency_overrides[get_current_user] = lambda: dict(_USER)
    return TestClient(app)


def test_route_rename_happy_path_passes_scope(monkeypatch):
    calls = {}

    async def _fake(*, tenant_id, user_id, model_id, display_name):
        calls.update(
            tenant_id=tenant_id, user_id=user_id,
            model_id=model_id, display_name=display_name,
        )
        return {"model_id": model_id, "metadata_json": {"display_name": display_name}}

    client = _client(monkeypatch, _fake)
    resp = client.patch(
        "/api/v1/models/mdl_abc/display-name", json={"display_name": "新名"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["metadata_json"]["display_name"] == "新名"
    assert calls == {
        "tenant_id": "default",
        "user_id": "10000001",
        "model_id": "mdl_abc",
        "display_name": "新名",
    }


def test_route_rename_not_found(monkeypatch):
    async def _fake(**_):
        return None

    client = _client(monkeypatch, _fake)
    resp = client.patch(
        "/api/v1/models/mdl_abc/display-name", json={"display_name": "新名"}
    )
    assert resp.status_code == 404


def test_route_rename_invalid_name_maps_422(monkeypatch):
    async def _fake(**_):
        raise ValueError("展示名不能为空")

    client = _client(monkeypatch, _fake)
    resp = client.patch(
        "/api/v1/models/mdl_abc/display-name", json={"display_name": "x"}
    )
    assert resp.status_code == 422
    assert "展示名" in resp.text


def test_route_rename_empty_body_rejected(monkeypatch):
    async def _fake(**_):
        raise AssertionError("不应到达服务层")

    client = _client(monkeypatch, _fake)
    resp = client.patch(
        "/api/v1/models/mdl_abc/display-name", json={"display_name": ""}
    )
    assert resp.status_code == 422  # pydantic min_length 拦截
