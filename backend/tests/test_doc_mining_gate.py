"""文档挖掘闸门（T-FM-13）—— `ENABLE_DOC_MINING` 默认关，docs 端点全 403。

三个静默故障点，逐条钉住：

1. **默认关**：未设 env 时，docs 面每个端点都必须 403（含将来新加的——
   闸门挂在 router 级 dependencies 上，新路由默认落在拒绝侧）。
2. **调用时读 env**：import 时冻结的话，测试与运维改了 env 都不生效。
3. **机器可读 detail**：前端与探针按 ``doc_mining_disabled`` 字面量断言，
   改字面量必须在这里红。

403 而非 404：说清「这东西存在但本部署没开」；404 会让运维去查路由注册。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.engine.alpha_agent import doc_gate  # noqa: E402
from backend.services.engine.routers import alpha_agent_docs as docs_mod  # noqa: E402


# ── 纯函数闸门 ──────────────────────────────────────────────────────


def test_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv(doc_gate.ENV_KEY, raising=False)
    assert doc_gate.is_doc_mining_enabled() is False
    with pytest.raises(HTTPException) as ei:
        doc_gate.require_doc_mining()
    assert ei.value.status_code == 403
    assert ei.value.detail == doc_gate.DISABLED_DETAIL == "doc_mining_disabled"


def test_reads_env_at_call_time(monkeypatch) -> None:
    """先关后开（不重建模块）——import 冻结的实现会在这里红。"""
    monkeypatch.setenv(doc_gate.ENV_KEY, "false")
    with pytest.raises(HTTPException):
        doc_gate.require_doc_mining()
    monkeypatch.setenv(doc_gate.ENV_KEY, "true")
    doc_gate.require_doc_mining()  # 不抛


# 词表 = `backend/shared/env_flags.py` 的唯一口径：归一（strip+lower）后**只有**
# `true`。`1/yes/on` 是 pydantic 的宽松口径，闸门侧刻意不收（合规咽喉从严，
# 放宽词表 = 让更多写法能打开云上传）。`" true "` / `"true\r"` 来自 Windows
# 便携包的 CRLF .env——strip 保证「设了就是设了」。
@pytest.mark.parametrize("raw", ["true", "TRUE", " True ", "true\r"])
def test_truthy_spellings_enable(monkeypatch, raw: str) -> None:
    monkeypatch.setenv(doc_gate.ENV_KEY, raw)
    assert doc_gate.is_doc_mining_enabled() is True


@pytest.mark.parametrize(
    "raw", ["", "0", "false", "no", "off", "1", "yes", "on", "2", "tru"]
)
def test_falsy_spellings_stay_disabled(monkeypatch, raw: str) -> None:
    monkeypatch.setenv(doc_gate.ENV_KEY, raw)
    assert doc_gate.is_doc_mining_enabled() is False


# ── 路由覆盖：新路由默认砍 ───────────────────────────────────────────


def test_every_docs_route_carries_gate_dependency() -> None:
    """router 级依赖会合并进每条路由；将来往这个 router 加漏挂闸的路由 → 红。"""
    paths = []
    for route in docs_mod.router.routes:
        paths.append(route.path)
        calls = [d.call for d in route.dependant.dependencies]
        assert doc_gate.require_doc_mining in calls, (
            f"路由 {route.path} 没挂文档挖掘闸门（router 级 dependencies）"
        )
    assert len(paths) >= 7, f"docs 路由疑似缺失：{paths}"


@pytest.mark.asyncio
async def test_gate_off_all_docs_routes_403(monkeypatch) -> None:
    """全关矩阵：闸门关闭时每个 docs 端点 403 + 机器可读 detail（handler 不执行）。"""
    monkeypatch.delenv(doc_gate.ENV_KEY, raising=False)
    app = FastAPI()
    app.include_router(docs_mod.router)
    transport = ASGITransport(app=app)
    endpoints = [
        ("POST", "/api/v1/alpha-agent/docs/upload"),
        ("GET", "/api/v1/alpha-agent/docs"),
        ("GET", "/api/v1/alpha-agent/docs/quota"),
        ("GET", "/api/v1/alpha-agent/docs/d-1"),
        ("GET", "/api/v1/alpha-agent/docs/d-1/file"),
        ("POST", "/api/v1/alpha-agent/docs/d-1/organize"),
        ("DELETE", "/api/v1/alpha-agent/docs/d-1"),
    ]
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for method, path in endpoints:
            resp = await client.request(method, path)
            assert resp.status_code == 403, (method, path, resp.status_code)
            assert resp.json()["detail"] == "doc_mining_disabled", (method, path)


def test_docs_route_order_quota_before_doc_id() -> None:
    """/docs/quota 必须先于 /docs/{doc_id} 注册，否则被参数路由吞掉（P0 同款教训）。"""
    paths = [r.path for r in docs_mod.router.routes]
    quota_idx = paths.index("/api/v1/alpha-agent/docs/quota")
    doc_id_idx = paths.index("/api/v1/alpha-agent/docs/{doc_id}")
    assert quota_idx < doc_id_idx
