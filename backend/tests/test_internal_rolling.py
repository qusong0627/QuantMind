"""内部滚动派发端点测试（P1 · 设计文档 §4.2/§4.6）。

覆盖 ``/api/v1/internal/rolling/*`` 的门面契约：

- **密钥 fail-closed**：缺/错 ``X-Internal-Call-Secret`` 一律 401，且 ``_verify``
  必须真的委托 admin 的 ``_verify_internal_call_secret``（源码级断言——admin 包
  当前因并行改动不可导入，行为级验证等端点实机验收；源码断言至少挡住「把
  _verify 改成 pass」这类静默开门）。
- **trigger 白名单**：Literal 校验（422）；schedule → dispatched_by=retrain_scheduler，
  其余触发源 → manual_api（§4.6 四条触发源共用出口，记账人不同）。
- **skipped 也是 2xx**：这是给调度器的「本轮不记账、下一 tick 重试」信号，
  改判非 2xx 会让 mark-after-dispatch 全部失效。
- **异常分派顺序**：RecipeError（ValueError 子类）→ 404 先于 ValueError → 400。
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import backend.services.engine.training.rolling_dispatch as rd
import backend.shared.rolling_campaigns as rc
import backend.shared.training.recipe_registry as rr
from backend.services.api.routers import internal_rolling as ir

_SECRET = "unit-test-secret"
_URL = "/api/v1/internal/rolling/dispatch"


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(ir.router)
    return TestClient(app)


@pytest.fixture()
def secure(monkeypatch):
    """把端点密钥校验替换成记录器 + 真实 fail-closed 行为。"""
    seen: list[str] = []

    def fake_verify(secret: str) -> None:
        seen.append(secret)
        if secret != _SECRET:
            raise HTTPException(status_code=401, detail="Invalid internal call secret")

    monkeypatch.setattr(ir, "_verify", fake_verify)
    return seen


@pytest.fixture()
def dispatch_spy(monkeypatch):
    """记录 execute_dispatch 调用参数的替身（返回可注入的裁决）。

    裁决必须从 spy 持有者实时读取——测试里 `spy["result"] = ...` 换的是持有者
    里的引用，闭包直接绑旧 dict 会让替换静默失效。
    """
    spy: dict = {
        "calls": [],
        "result": {
            "status": "dispatched",
            "campaign_id": "rc_cn_cn_nativetft_base_20261001",
            "run_id": "run_dispatched_1",
            "anchor_date": "2026-10-01",
        },
    }

    async def fake_execute(**kwargs):
        spy["calls"].append(kwargs)
        return dict(spy["result"])

    monkeypatch.setattr(rd, "execute_dispatch", fake_execute)
    return spy


def _post(client, body, secret=_SECRET):
    headers = {"X-Internal-Call-Secret": secret} if secret is not None else {}
    return client.post(_URL, json=body, headers=headers)


# ---------------------------------------------------------------------------
# 密钥面
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_missing_or_wrong_secret_is_401(client, secure, dispatch_spy):
    resp = _post(client, {"market": "CN", "recipe_id": "cn_nativetft_base"}, secret=None)
    assert resp.status_code == 401
    resp = _post(client, {"market": "CN", "recipe_id": "cn_nativetft_base"}, secret="wrong")
    assert resp.status_code == 401
    assert dispatch_spy["calls"] == []
    # 头部值确实被交给校验器（缺省空串——fail-closed 的三条路径由 admin 侧覆盖）
    assert secure == ["", "wrong"]


@pytest.mark.unit
def test_verify_delegates_to_admin_secret_checker():
    """源码级守卫：_verify 必须委托 admin 的校验器，不得静默放行。"""
    src = inspect.getsource(ir._verify)
    assert "_verify_internal_call_secret" in src
    assert "admin_training_utils" in src


# ---------------------------------------------------------------------------
# 请求契约
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_unknown_trigger_rejected_422(client, secure, dispatch_spy):
    resp = _post(
        client,
        {"market": "CN", "recipe_id": "r", "trigger": "bogus"},
    )
    assert resp.status_code == 422
    assert dispatch_spy["calls"] == []


@pytest.mark.unit
def test_missing_required_fields_rejected_422(client, secure):
    assert _post(client, {"recipe_id": "r"}).status_code == 422
    assert _post(client, {"market": "CN"}).status_code == 422


@pytest.mark.unit
def test_defaults_schedule_trigger_and_not_dry_run(client, secure, dispatch_spy):
    resp = _post(client, {"market": "CN", "recipe_id": "cn_nativetft_base"})
    assert resp.status_code == 200
    (call,) = dispatch_spy["calls"]
    assert call["market"] == "CN"
    assert call["recipe_id"] == "cn_nativetft_base"
    assert call["trigger"] == "schedule"
    assert call["dispatched_by"] == rd.DISPATCHED_BY_SCHEDULER
    assert call["dry_run"] is False
    assert call["anchor_date"] is None
    assert resp.json() == dispatch_spy["result"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("trigger", "expected"),
    [
        ("schedule", rd.DISPATCHED_BY_SCHEDULER),
        ("manual", rd.DISPATCHED_BY_MANUAL),
        ("sentinel", rd.DISPATCHED_BY_MANUAL),
        ("drift", rd.DISPATCHED_BY_MANUAL),
    ],
)
def test_trigger_to_dispatched_by_derivation(client, secure, dispatch_spy, trigger, expected):
    resp = _post(
        client, {"market": "CN", "recipe_id": "r", "trigger": trigger, "dry_run": True}
    )
    assert resp.status_code == 200
    (call,) = dispatch_spy["calls"]
    assert call["trigger"] == trigger
    assert call["dispatched_by"] == expected
    assert call["dry_run"] is True


@pytest.mark.unit
def test_anchor_date_passthrough(client, secure, dispatch_spy):
    _post(
        client,
        {"market": "CN", "recipe_id": "r", "anchor_date": "2026-09-30"},
    )
    (call,) = dispatch_spy["calls"]
    assert call["anchor_date"] == "2026-09-30"


@pytest.mark.unit
def test_skipped_stays_2xx(client, secure, dispatch_spy):
    """busy/data_lag 等 skipped 必须 2xx——调度器以此判「不写 last_run」。"""
    dispatch_spy["result"] = {"status": "skipped", "reason": "busy", "detail": {}}
    resp = _post(client, {"market": "CN", "recipe_id": "r"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "skipped"


# ---------------------------------------------------------------------------
# 异常分派（RecipeError 是 ValueError 子类，404 必须先捕获）
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_recipe_error_maps_to_404_not_400(client, secure, monkeypatch):
    async def boom(**kwargs):
        raise rr.RecipeError("配方不存在: nope")

    monkeypatch.setattr(rd, "execute_dispatch", boom)
    resp = _post(client, {"market": "CN", "recipe_id": "nope"})
    assert resp.status_code == 404
    assert "nope" in resp.json()["detail"]


@pytest.mark.unit
def test_value_error_maps_to_400(client, secure, monkeypatch):
    async def boom(**kwargs):
        raise ValueError("市场不匹配: recipe 属 HK")

    monkeypatch.setattr(rd, "execute_dispatch", boom)
    resp = _post(client, {"market": "CN", "recipe_id": "r"})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 只读面
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_campaigns_requires_secret_and_forwards_filters(client, secure, monkeypatch):
    captured: dict = {}

    async def fake_list(**kwargs):
        captured.update(kwargs)
        return [{"campaign_id": "rc_x", "status": "dispatched"}]

    monkeypatch.setattr(rc, "list_campaigns", fake_list)

    assert client.get("/api/v1/internal/rolling/campaigns").status_code == 401
    resp = client.get(
        "/api/v1/internal/rolling/campaigns",
        params={"market": "CN", "status": "dispatched", "limit": 10},
        headers={"X-Internal-Call-Secret": _SECRET},
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "campaigns": [{"campaign_id": "rc_x", "status": "dispatched"}],
        "count": 1,
    }
    assert captured == {"market": "CN", "status": "dispatched", "limit": 10}

    # limit 越界 → 422（FastAPI Query 边界）
    bad = client.get(
        "/api/v1/internal/rolling/campaigns",
        params={"limit": 0},
        headers={"X-Internal-Call-Secret": _SECRET},
    )
    assert bad.status_code == 422


@pytest.mark.unit
def test_recipes_requires_secret_and_returns_summaries(client, secure, monkeypatch):
    monkeypatch.setattr(rr, "list_recipes", lambda: [{"recipe_id": "cn_nativetft_base"}])

    assert client.get("/api/v1/internal/rolling/recipes").status_code == 401
    resp = client.get(
        "/api/v1/internal/rolling/recipes",
        headers={"X-Internal-Call-Secret": _SECRET},
    )
    assert resp.status_code == 200
    assert resp.json() == {"recipes": [{"recipe_id": "cn_nativetft_base"}], "count": 1}


# ---------------------------------------------------------------------------
# 调度配置面（GET/PUT /schedule）
# ---------------------------------------------------------------------------


@pytest.fixture()
def schedule_spy(monkeypatch):
    """retrain_scheduler 配置面替身：默认只有 CN 有有效配方。"""
    from backend.services.engine.tasks import retrain_scheduler as rts

    saved: list = []
    monkeypatch.setattr(rts, "recipe_markets", lambda: ["CN"])
    monkeypatch.setattr(
        rts, "get_all_schedules", lambda **kw: {"CN": {"enabled": False, "recipe_id": "cn_nativetft_base"}}
    )

    def fake_save(market, cfg):
        saved.append((market, cfg))
        return {**cfg, "time": "15:30", "normalized": True}

    monkeypatch.setattr(rts, "save_schedule", fake_save)
    monkeypatch.setattr(
        rr, "load_recipe", lambda rid: SimpleNamespace(market="CN", recipe_id=rid)
    )
    return saved


@pytest.mark.unit
def test_schedule_get_requires_secret_and_lists_markets(client, secure, schedule_spy):
    url = "/api/v1/internal/rolling/schedule"
    assert client.get(url).status_code == 401
    resp = client.get(url, headers={"X-Internal-Call-Secret": _SECRET})
    assert resp.status_code == 200
    assert resp.json()["markets"] == ["CN"]
    assert resp.json()["schedules"]["CN"]["enabled"] is False


@pytest.mark.unit
def test_schedule_put_normalizes_and_returns_saved(client, secure, schedule_spy):
    resp = client.put(
        "/api/v1/internal/rolling/schedule/cn",
        json={"enabled": True, "recipe_id": "cn_nativetft_base"},
        headers={"X-Internal-Call-Secret": _SECRET},
    )
    assert resp.status_code == 200
    assert resp.json()["market"] == "CN"
    market, cfg = schedule_spy[0]
    assert market == "CN"
    # 模型默认值全量落盘（缺键保存 = 静默用默认，键面必须显式）
    assert cfg["day_rule"] == "first_trading_day"
    assert cfg["observation_days"] == 20
    assert cfg["max_time_minutes"] == 240
    assert cfg["executor"] == "local"
    assert cfg["window_policy"] is None and cfg["purge_days"] is None


@pytest.mark.unit
def test_schedule_put_rejects_market_without_recipe(client, secure, schedule_spy):
    resp = client.put(
        "/api/v1/internal/rolling/schedule/HK",
        json={"recipe_id": "cn_nativetft_base"},
        headers={"X-Internal-Call-Secret": _SECRET},
    )
    assert resp.status_code == 404
    assert schedule_spy == []


@pytest.mark.unit
def test_schedule_put_rejects_unknown_recipe_and_market_mismatch(
    client, secure, monkeypatch
):
    from backend.services.engine.tasks import retrain_scheduler as rts

    monkeypatch.setattr(rts, "recipe_markets", lambda: ["CN"])
    monkeypatch.setattr(rts, "save_schedule", lambda m, c: c)

    def _missing(rid):
        raise rr.RecipeError(f"配方不存在: {rid}")

    monkeypatch.setattr(rr, "load_recipe", _missing)
    assert (
        client.put(
            "/api/v1/internal/rolling/schedule/CN",
            json={"recipe_id": "nope"},
            headers={"X-Internal-Call-Secret": _SECRET},
        ).status_code
        == 404
    )

    monkeypatch.setattr(
        rr, "load_recipe", lambda rid: SimpleNamespace(market="HK", recipe_id=rid)
    )
    resp = client.put(
        "/api/v1/internal/rolling/schedule/CN",
        json={"recipe_id": "hk_recipe"},
        headers={"X-Internal-Call-Secret": _SECRET},
    )
    assert resp.status_code == 400


@pytest.mark.unit
def test_schedule_put_validates_field_bounds(client, secure, schedule_spy):
    resp = client.put(
        "/api/v1/internal/rolling/schedule/CN",
        json={"recipe_id": "cn_nativetft_base", "executor": "quantum"},
        headers={"X-Internal-Call-Secret": _SECRET},
    )
    assert resp.status_code == 422
    resp = client.put(
        "/api/v1/internal/rolling/schedule/CN",
        json={"recipe_id": "cn_nativetft_base", "max_time_minutes": 1},
        headers={"X-Internal-Call-Secret": _SECRET},
    )
    assert resp.status_code == 422


@pytest.mark.unit
def test_schedule_put_rejects_policy_override_as_visible_error(client, secure, schedule_spy):
    """窗口策略归配方所有：调度层覆写无消费者，必须 400 而非静默存空转配置。"""
    for field, value in (("window_policy", {"train_days": 500}), ("purge_days", 7)):
        resp = client.put(
            "/api/v1/internal/rolling/schedule/CN",
            json={"recipe_id": "cn_nativetft_base", field: value},
            headers={"X-Internal-Call-Secret": _SECRET},
        )
        assert resp.status_code == 400, field
        assert "配方所有" in resp.json()["detail"]
    assert schedule_spy == []
    # None 是合法往返值（缺省=沿用配方策略），不得误伤
    resp = client.put(
        "/api/v1/internal/rolling/schedule/CN",
        json={"recipe_id": "cn_nativetft_base", "window_policy": None, "purge_days": None},
        headers={"X-Internal-Call-Secret": _SECRET},
    )
    assert resp.status_code == 200


@pytest.mark.unit
def test_schedule_put_rejects_remote_executor_until_wired(client, secure, schedule_spy):
    """executor=remote 未接线：400 拒绝，避免「存了不生效还拆掉内存守卫」（审查 F2）。"""
    resp = client.put(
        "/api/v1/internal/rolling/schedule/CN",
        json={"recipe_id": "cn_nativetft_base", "executor": "remote"},
        headers={"X-Internal-Call-Secret": _SECRET},
    )
    assert resp.status_code == 400
    assert "remote" in resp.json()["detail"]
    assert schedule_spy == []
    # local（缺省与显式）照常
    resp = client.put(
        "/api/v1/internal/rolling/schedule/CN",
        json={"recipe_id": "cn_nativetft_base", "executor": "local"},
        headers={"X-Internal-Call-Secret": _SECRET},
    )
    assert resp.status_code == 200
