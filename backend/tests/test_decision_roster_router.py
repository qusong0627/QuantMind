"""决策名册端点行为测试：信封 / 管理员门 / 校验不落盘 / 读写闭环。

跑在**临时 runtime.env** 上（``QM_RUNTIME_ENV_FILE``，即真实生效路径）：这些用例真的会
调用 ``set_secret``，不隔离就等于往生产的 ``config/runtime.env`` 里写配置。
"""

from __future__ import annotations

import json
import os

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit

PREFIX = "/api/v1/decision"


@pytest.fixture(autouse=True)
def isolated_runtime_env(tmp_path, monkeypatch):
    """真 set_secret 会写 runtime.env + 本进程环境 ⇒ 两样都要隔离并复原。"""
    monkeypatch.setenv("QM_RUNTIME_ENV_FILE", str(tmp_path / "runtime.env"))
    snapshot = {k: v for k, v in os.environ.items() if k.startswith("QM_DECISION_LLM_")}
    monkeypatch.delenv("QM_DECISION_LLM_ROSTER", raising=False)
    yield tmp_path
    for key in [k for k in os.environ if k.startswith("QM_DECISION_LLM_")]:
        os.environ.pop(key, None)
    os.environ.update(snapshot)


@pytest.fixture(autouse=True)
def no_redis(monkeypatch):
    """状态镜像走桩：端点测试不连 Redis（真读法由服务层用例覆盖）。"""
    from backend.services.trade.services import decision_roster_config as roster

    monkeypatch.setattr(
        roster,
        "read_agent_status",
        lambda **kw: {"last": None, "agents": {}, "error": ""},
    )


def _stub_admin():
    from backend.services.trade_shared.deps import AuthContext

    return AuthContext(
        user_id="10000001", tenant_id="default", raw_sub="10000001",
        roles=["admin"], is_admin=True,
    )


@pytest.fixture
def client():
    from backend.services.trade.routers.decision_roster import router
    from backend.services.trade_shared.deps import require_admin

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[require_admin] = _stub_admin
    return TestClient(app)


def _roster_payload() -> dict:
    return {
        "entries": [
            {
                "model": "glm-4.6",
                "base_url": "https://open.bigmodel.cn/api/paas/v4",
                "api_key": "sk-test-key",
                "max_tokens": 2000,
            }
        ]
    }


def test_get_reports_single_path_and_accepted_fields(client):
    resp = client.get(f"{PREFIX}/roster")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["roster_configured"] is False
    assert body["data"]["source"] == "none"      # 没配名册、也没有全局三件套
    assert "api_key" in body["accepted"]         # 字段说明随信封下发（前端不硬编码文案）


def test_put_get_delete_round_trip(client, monkeypatch):
    # 单家三件套可用（生产里它通常就是备胎）：清空后要落回它，所以先配上
    monkeypatch.setenv("QM_DECISION_LLM_BASE_URL", "https://api.deepseek.com/v1")
    monkeypatch.setenv("QM_DECISION_LLM_API_KEY", "sk-global")
    monkeypatch.setenv("QM_DECISION_LLM_MODEL", "deepseek-chat")
    resp = client.put(f"{PREFIX}/roster", json=_roster_payload())
    assert resp.status_code == 200, resp.text
    state = resp.json()["data"]
    assert state["roster_configured"] is True
    assert state["source"] == "roster"
    assert state["entries"][0]["agent"] == "glm-4.6"
    assert state["entries"][0]["api_key_set"] is True
    assert resp.json()["applied"]["ok"] is True  # 落盘细节在 applied，不在 data 里

    # 再读一遍：拿到的是**落盘后**的真实状态（不是保存时回显的那份）
    got = client.get(f"{PREFIX}/roster").json()["data"]
    assert got["entries"][0]["model"] == "glm-4.6"
    assert got["entries"][0]["max_tokens"] == 2000
    assert "sk-test-key" not in json.dumps(got)   # 端点也不回传 key
    # **一个资源一种形状**：PUT/DELETE 的 data 与 GET 的 data 键集完全相同。
    # （曾经 PUT 把现状塞进 data.state，消费方得按方法记两种读法——已收敛）
    assert set(resp.json()["data"]) == set(got)

    resp = client.delete(f"{PREFIX}/roster")
    assert resp.status_code == 200, resp.text
    state = resp.json()["data"]
    assert state["roster_configured"] is False
    assert state["source"] == "single"  # 落回单家三件套，不是「一家都没有」
    assert set(resp.json()["data"]) == set(got)


def test_put_validation_failure_is_400_with_all_reasons(client, isolated_runtime_env):
    body = {"entries": [{"model": "ok-model"}, {"model": "your-model-name"}]}
    resp = client.put(f"{PREFIX}/roster", json=body)
    assert resp.status_code == 400
    payload = resp.json()
    assert payload["success"] is False and payload["errors"]
    # 名校验失败**一条都不落盘**（含第一家那条合法项）
    assert not (isolated_runtime_env / "runtime.env").exists()


def test_non_admin_is_rejected_by_the_real_gate():
    """不覆盖 require_admin，只喂一个非管理员身份——验的是真闸门。"""
    from backend.services.trade.routers.decision_roster import router
    from backend.services.trade_shared.deps import AuthContext, get_auth_context

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    def stub_user():
        return AuthContext(
            user_id="90001", tenant_id="default", raw_sub="90001",
            roles=["user"], is_admin=False,
        )

    app.dependency_overrides[get_auth_context] = stub_user
    resp = TestClient(app).get(f"{PREFIX}/roster")
    assert resp.status_code == 403
    assert resp.json()["detail"] == "需要管理员权限"


def test_unauthenticated_is_401():
    """不带 token：HTTPBearer 直接 401/403，不能漏到 handler。"""
    from backend.services.trade.routers.decision_roster import router

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    resp = TestClient(app).get(f"{PREFIX}/roster")
    assert resp.status_code in (401, 403)


def test_delete_refuses_and_rolls_back_when_single_path_unusable(client):
    """清空后连单家都不通 → 400 + 名册原样保留。

    这不是保守过度：空名册 + 空三件套 = 每轮都跑不成、而心跳照写的**静默停机**
    （``resolve_roster`` 自己也把空名册判成硬错误）。要停轮次请关开关，不要靠清名册。
    """
    previous = json.dumps([{"model": "glm-4.6", "base_url": "https://x/v1", "api_key": "k"}])
    os.environ["QM_DECISION_LLM_ROSTER"] = previous
    resp = client.delete(f"{PREFIX}/roster")
    assert resp.status_code == 400
    error = resp.json()["errors"][0]
    assert "已回滚" in error and "QM_DECISION_LLM_BASE_URL" in error  # 告诉用户下一步
    assert os.environ["QM_DECISION_LLM_ROSTER"] == previous
    assert json.loads(os.environ["QM_DECISION_LLM_ROSTER"])[0]["model"] == "glm-4.6"


def test_router_declares_exactly_the_three_methods():
    """把端点清单钉住：少挂一个方法，前端会以 405/404 的形式「静默」坏掉。

    一个方法一条 ``APIRoute``（实测如此，别按「一条路径一个 handler」的想象写断言）。
    """
    from backend.services.trade.routers.decision_roster import router

    methods = {
        method
        for route in router.routes
        if getattr(route, "path", "") == "/decision/roster"
        for method in route.methods
    }
    assert methods == {"GET", "PUT", "DELETE"}
