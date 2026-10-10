"""模型管理「滚动训练」用户态端点测试（/api/v1/models/rolling/*）。

口径：
- 前端不得持有内部密钥 —— 本路由以 JWT（get_current_user）保护，无凭证 401；
- 派发必须带 ``current_user``（归属人=真实登录用户，不是 CUSTOM_USER 兜底）；
- 手动派发在提交前过内存守卫（低内存 409 拒发；dry_run 预览不过守卫）；
- 调度保存校验与内部端点**同口径**（共享 rolling_shared 实现）：窗口策略归
  配方（覆写 400）/ remote 未接线（400）/ 市场无配方（404）/ 配方市场错配（400）；
- 派生：只读注册表行 + 模型目录，产物是普通配方文件（load_recipe 可读）。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

import backend.services.engine.training.rolling_dispatch as rd
import backend.shared.model_registry as mreg
import backend.shared.rolling_campaigns as rc
import backend.shared.training.recipe_registry as rr
from backend.services.api.routers import model_rolling as mr
from backend.services.api.user_app.middleware.auth import get_current_user

_USER = {"tenant_id": "default", "user_id": "10000001", "sub": "10000001"}

MODEL_ID = "mdl_cn_hub_demo_abc123"
BASE = "/api/v1/models/rolling"


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(mr.router)
    app.dependency_overrides[get_current_user] = lambda: dict(_USER)
    return TestClient(app)


@pytest.fixture()
def raw_client():
    app = FastAPI()
    app.include_router(mr.router)
    return TestClient(app)


@pytest.fixture()
def dispatch_spy(monkeypatch):
    """execute_dispatch 替身 + 内存守卫放行。"""
    spy: dict = {
        "calls": [],
        "result": {
            "status": "dispatched",
            "campaign_id": "rc_cn_cn_nativetft_base_20261001_manual",
            "run_id": "run_1",
        },
    }

    async def fake_execute(**kwargs):
        spy["calls"].append(kwargs)
        return dict(spy["result"])

    monkeypatch.setattr(rd, "execute_dispatch", fake_execute)
    monkeypatch.setattr(
        rd, "mem_guard", lambda *a, **k: {"ok": True, "available_gb": 100}
    )
    return spy


@pytest.fixture()
def schedule_spy(monkeypatch):
    """retrain_scheduler 配置面替身：默认只有 CN 有有效配方。"""
    from backend.services.engine.tasks import retrain_scheduler as rts

    saved: list = []
    monkeypatch.setattr(rts, "recipe_markets", lambda: ["CN"])
    monkeypatch.setattr(
        rts,
        "get_all_schedules",
        lambda **kw: {"CN": {"enabled": False, "recipe_id": "cn_nativetft_base"}},
    )

    def fake_save(market, cfg):
        saved.append((market, cfg))
        return {**cfg, "normalized": True}

    monkeypatch.setattr(rts, "save_schedule", fake_save)
    monkeypatch.setattr(
        rr, "load_recipe", lambda rid: SimpleNamespace(market="CN", recipe_id=rid)
    )
    # 派发器心跳读替换成确定值（真实 read_heartbeats 会碰 Redis；其判定语义
    # 已由 test_scheduler_registry 锁定，这里只验证路由把它原样透出）。
    from backend.shared import scheduler_registry as sreg

    monkeypatch.setattr(
        sreg,
        "read_heartbeats",
        lambda keys, **kw: [
            {
                "key": k,
                "name": "滚动重训派发",
                "enabled": True,
                "state": "ok",
                "age": 5,
                "ttl": 1800,
            }
            for k in keys
        ],
    )
    return saved


# ---------------------------------------------------------------------------
# 认证面
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_requires_jwt_without_credentials(raw_client):
    assert raw_client.get(f"{BASE}/recipes").status_code == 401
    assert raw_client.get(f"{BASE}/campaigns").status_code == 401
    assert raw_client.get(f"{BASE}/schedule").status_code == 401
    assert (
        raw_client.post(
            f"{BASE}/dispatch", json={"market": "CN", "recipe_id": "x"}
        ).status_code
        == 401
    )
    assert (
        raw_client.post(f"{BASE}/derive", json={"model_id": MODEL_ID}).status_code
        == 401
    )
    assert (
        raw_client.put(f"{BASE}/schedule/CN", json={"recipe_id": "x"}).status_code
        == 401
    )


# ---------------------------------------------------------------------------
# 只读面
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_recipes_passthrough(client, monkeypatch):
    monkeypatch.setattr(
        rr,
        "list_recipes",
        lambda: [{"recipe_id": "r1", "valid": True, "source": "builtin"}],
    )
    resp = client.get(f"{BASE}/recipes")
    assert resp.status_code == 200
    assert resp.json() == {
        "recipes": [{"recipe_id": "r1", "valid": True, "source": "builtin"}],
        "count": 1,
    }


@pytest.mark.unit
def test_campaigns_forwards_filters(client, monkeypatch):
    captured: dict = {}

    async def fake_list(**kwargs):
        captured.update(kwargs)
        return [{"campaign_id": "rc_x"}]

    monkeypatch.setattr(rc, "list_campaigns", fake_list)
    resp = client.get(
        f"{BASE}/campaigns",
        params={"market": "CN", "status": "dispatched", "limit": 10},
    )
    assert resp.status_code == 200
    assert resp.json() == {"campaigns": [{"campaign_id": "rc_x"}], "count": 1}
    assert captured == {"market": "CN", "status": "dispatched", "limit": 10}
    assert client.get(f"{BASE}/campaigns", params={"limit": 0}).status_code == 422


# ---------------------------------------------------------------------------
# 手动派发
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_dispatch_manual_attribution_and_passthrough(client, dispatch_spy):
    resp = client.post(
        f"{BASE}/dispatch",
        json={"market": "CN", "recipe_id": "cn_nativetft_base", "dry_run": True},
    )
    assert resp.status_code == 200
    (call,) = dispatch_spy["calls"]
    assert call["market"] == "CN"
    assert call["recipe_id"] == "cn_nativetft_base"
    assert call["trigger"] == "manual"
    assert call["dispatched_by"] == rd.DISPATCHED_BY_MANUAL
    assert call["dry_run"] is True
    assert call["current_user"] == _USER
    assert resp.json() == dispatch_spy["result"]


@pytest.mark.unit
def test_dispatch_dry_run_skips_mem_guard(client, dispatch_spy, monkeypatch):
    def explode(*a, **k):
        raise AssertionError("dry_run 预览不得触发内存守卫")

    monkeypatch.setattr(rd, "mem_guard", explode)
    resp = client.post(
        f"{BASE}/dispatch",
        json={"market": "CN", "recipe_id": "r", "dry_run": True},
    )
    assert resp.status_code == 200


@pytest.mark.unit
def test_dispatch_low_memory_blocks_before_submit(client, dispatch_spy, monkeypatch):
    monkeypatch.setattr(
        rd,
        "mem_guard",
        lambda *a, **k: {"ok": False, "available_gb": 12.3, "min_gb": 50.0},
    )
    resp = client.post(f"{BASE}/dispatch", json={"market": "CN", "recipe_id": "r"})
    assert resp.status_code == 409
    assert "12.3" in resp.json()["detail"]
    assert dispatch_spy["calls"] == []


@pytest.mark.unit
def test_dispatch_error_mapping_mirrors_internal(client, dispatch_spy, monkeypatch):
    async def recipe_missing(**kwargs):
        raise rr.RecipeError("配方不存在: nope")

    monkeypatch.setattr(rd, "execute_dispatch", recipe_missing)
    resp = client.post(f"{BASE}/dispatch", json={"market": "CN", "recipe_id": "nope"})
    assert resp.status_code == 404

    async def market_mismatch(**kwargs):
        raise ValueError("市场不匹配")

    monkeypatch.setattr(rd, "execute_dispatch", market_mismatch)
    resp = client.post(f"{BASE}/dispatch", json={"market": "CN", "recipe_id": "r"})
    assert resp.status_code == 400


@pytest.mark.unit
def test_dispatch_trigger_is_not_client_controllable(client, dispatch_spy):
    """trigger 由服务端定死 manual——客户端夹带的 schedule 不改变记账人。"""
    resp = client.post(
        f"{BASE}/dispatch",
        json={"market": "CN", "recipe_id": "r", "trigger": "schedule"},
    )
    assert resp.status_code == 200
    (call,) = dispatch_spy["calls"]
    assert call["trigger"] == "manual"
    assert call["dispatched_by"] == rd.DISPATCHED_BY_MANUAL


# ---------------------------------------------------------------------------
# 调度配置面（与内部端点同口径）
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_schedule_get_passthrough(client, schedule_spy):
    resp = client.get(f"{BASE}/schedule")
    assert resp.status_code == 200
    assert resp.json()["markets"] == ["CN"]
    assert resp.json()["schedules"]["CN"]["enabled"] is False
    # 心跳块随调度一起返回（默认替身为 ok）；面板据此渲染派发器状态
    assert resp.json()["dispatch"]["state"] == "ok"


@pytest.mark.unit
def test_schedule_get_reports_stale_dispatch_heartbeat(
    client, schedule_spy, monkeypatch
):
    """派发器心跳过期原样透出：调度保存了 enabled 但没人派发的真相出口（M5）。"""
    from backend.shared import scheduler_registry as sreg

    monkeypatch.setattr(
        sreg,
        "read_heartbeats",
        lambda keys, **kw: [
            {
                "key": "retrain_dispatch",
                "name": "滚动重训派发",
                "enabled": True,
                "state": "stale",
                "age": 4000,
                "ttl": 1800,
            }
        ],
    )

    resp = client.get(f"{BASE}/schedule")

    assert resp.status_code == 200
    dispatch = resp.json()["dispatch"]
    assert dispatch["state"] == "stale"
    assert dispatch["enabled"] is True
    assert dispatch["age"] == 4000


@pytest.mark.unit
def test_schedule_put_saves_with_full_defaults(client, schedule_spy):
    resp = client.put(
        f"{BASE}/schedule/cn", json={"enabled": True, "recipe_id": "cn_nativetft_base"}
    )
    assert resp.status_code == 200
    assert resp.json()["market"] == "CN"
    market, cfg = schedule_spy[0]
    assert market == "CN"
    assert cfg["day_rule"] == "first_trading_day"
    assert cfg["observation_days"] == 20
    assert cfg["max_time_minutes"] == 240
    assert cfg["executor"] == "local"


@pytest.mark.unit
def test_schedule_put_rejects_policy_override_and_remote(client, schedule_spy):
    resp = client.put(
        f"{BASE}/schedule/CN",
        json={"recipe_id": "cn_nativetft_base", "window_policy": {"train_days": 500}},
    )
    assert resp.status_code == 400
    assert "配方所有" in resp.json()["detail"]
    resp = client.put(
        f"{BASE}/schedule/CN",
        json={"recipe_id": "cn_nativetft_base", "executor": "remote"},
    )
    assert resp.status_code == 400
    assert schedule_spy == []


@pytest.mark.unit
def test_schedule_put_market_and_recipe_validation(client, schedule_spy, monkeypatch):
    resp = client.put(f"{BASE}/schedule/HK", json={"recipe_id": "cn_nativetft_base"})
    assert resp.status_code == 404

    def _missing(rid):
        raise rr.RecipeError(f"配方不存在: {rid}")

    monkeypatch.setattr(rr, "load_recipe", _missing)
    resp = client.put(f"{BASE}/schedule/CN", json={"recipe_id": "nope"})
    assert resp.status_code == 404

    monkeypatch.setattr(
        rr, "load_recipe", lambda rid: SimpleNamespace(market="HK", recipe_id=rid)
    )
    resp = client.put(f"{BASE}/schedule/CN", json={"recipe_id": "hk_recipe"})
    assert resp.status_code == 400


@pytest.mark.unit
def test_schedule_put_rejects_unbounded_or_malformed_fields(client, schedule_spy):
    """配置整份落共享 Redis 并被 GET/告警回显：字段必须有界；time 必须 24h HH:mm
    （坏时间此前会被调度器静默归一成默认值——保存成功但改了个寂寞，不如 422）。"""
    for bad in (
        {"time": "25:99"},
        {"time": "3:5"},
        {"day_rule": "x" * 500},
        {"recipe_id": "../escape"},
        {"observation_days": 0},
        {"max_time_minutes": 99999},
    ):
        resp = client.put(
            f"{BASE}/schedule/CN", json={"recipe_id": "cn_nativetft_base", **bad}
        )
        assert resp.status_code == 422, bad
    assert schedule_spy == []


# ---------------------------------------------------------------------------
# 派发入参守卫（recipe_id 即文件名，字符集白名单双层第一层）
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_dispatch_rejects_illegal_recipe_id(client, dispatch_spy):
    for bad in ("../etc/passwd", "a/b", "with space", "x" * 200):
        resp = client.post(f"{BASE}/dispatch", json={"market": "CN", "recipe_id": bad})
        assert resp.status_code == 422, bad
    assert dispatch_spy["calls"] == []


# ---------------------------------------------------------------------------
# 从模型派生配方
# ---------------------------------------------------------------------------


def _model_dir(base: Path) -> Path:
    d = base / MODEL_ID
    d.mkdir(parents=True)
    meta = {
        "model_id": MODEL_ID,
        "model_name": "云端导入 · 演示",
        "market": "CN",
        "model_type": "nativetft",
        "features": ["f_a", "f_b"],
        "factor_source": "l1_factors",
        "factor_catalog_version": "qdb-custom-l1_factors-deadbeef",
        "target_horizon_days": 5,
        "target_mode": "return",
        "context": {"market": "CUSTOM", "benchmark": "SH000300"},
    }
    (d / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    cfg = {
        "model": {"type": "nativetft", "dl_params": {"n_epochs": 8}},
        "data": {"features": ["f_a", "f_b"]},
        "label": {"target_horizon_days": 5, "target_mode": "return"},
        "context": {"market": "CUSTOM", "benchmark": "SH000300"},
        "split": {"train_start": "2016-01-04", "test_end": "2026-09-11"},
    }
    (d / "config.yaml").write_text(
        yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8"
    )
    return d


@pytest.fixture()
def derive_env(tmp_path, monkeypatch):
    """注册表替身（返回 tmp 模型目录）+ 用户配方目录/模型根都指向 tmp。"""
    roots = tmp_path / "roots"
    roots.mkdir()
    model_dir = _model_dir(roots)
    recipes = tmp_path / "rolling_recipes"
    monkeypatch.setenv("USER_MODELS_ROOT", str(roots))
    monkeypatch.setenv("QM_ROLLING_RECIPE_DIR", str(recipes))

    async def fake_get_model(**kwargs):
        assert kwargs["tenant_id"] == "default"
        assert kwargs["user_id"] == "10000001"
        assert kwargs["model_id"] == MODEL_ID
        return {
            "model_id": MODEL_ID,
            "storage_path": str(model_dir),
            "metadata_json": {},
            "status": "ready",
        }

    monkeypatch.setattr(mreg.model_registry_service, "get_model", fake_get_model)
    return SimpleNamespace(model_dir=model_dir, recipes=recipes)


@pytest.mark.unit
def test_derive_preview_then_save_then_unchanged(client, derive_env):
    # 预览：不落盘
    resp = client.post(f"{BASE}/derive", json={"model_id": MODEL_ID, "dry_run": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "preview"
    assert body["recipe_id"] == f"model_{MODEL_ID}"
    assert body["market"] == "CN"
    assert body["feature_count"] == 2
    assert not derive_env.recipes.exists()

    # 保存：落盘 + 可被注册表装载
    resp = client.post(f"{BASE}/derive", json={"model_id": MODEL_ID})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "saved"
    assert body["recipe_hash"]
    path = derive_env.recipes / f"model_{MODEL_ID}.json"
    assert path.is_file()
    loaded = rr.load_recipe(f"model_{MODEL_ID}")
    assert loaded.source_model_id == MODEL_ID
    assert loaded.payload["features"] == ["f_a", "f_b"]

    # 幂等：同内容重派生 → unchanged（不重写）
    before = path.stat().st_mtime_ns
    resp = client.post(f"{BASE}/derive", json={"model_id": MODEL_ID})
    assert resp.json()["status"] == "unchanged"
    assert path.stat().st_mtime_ns == before


@pytest.mark.unit
def test_derive_model_not_found_404(client, derive_env, monkeypatch):
    async def missing(**kwargs):
        return None

    monkeypatch.setattr(mreg.model_registry_service, "get_model", missing)
    resp = client.post(f"{BASE}/derive", json={"model_id": "mdl_none"})
    assert resp.status_code == 404


@pytest.mark.unit
def test_derive_rejects_storage_path_outside_roots(client, derive_env, monkeypatch):
    async def outside(**kwargs):
        return {"model_id": MODEL_ID, "storage_path": "/etc", "status": "ready"}

    monkeypatch.setattr(mreg.model_registry_service, "get_model", outside)
    resp = client.post(f"{BASE}/derive", json={"model_id": MODEL_ID})
    assert resp.status_code == 400
    assert "越界" in resp.json()["detail"] or "root" in resp.json()["detail"].lower()


@pytest.mark.unit
def test_derive_unable_package_maps_to_400(client, derive_env, monkeypatch):
    async def bare(**kwargs):
        empty = derive_env.model_dir.parent / "bare"
        empty.mkdir()
        return {"model_id": MODEL_ID, "storage_path": str(empty), "status": "ready"}

    monkeypatch.setattr(mreg.model_registry_service, "get_model", bare)
    resp = client.post(f"{BASE}/derive", json={"model_id": MODEL_ID})
    assert resp.status_code == 400
    assert "metadata.json" in resp.json()["detail"]
