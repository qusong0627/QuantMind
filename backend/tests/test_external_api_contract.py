"""对外 API 的**契约**：OpenAPI schema 本身就是给外部节点的说明书。

为什么把 schema 当承重件测
--------------------------
这套接口面向的是**机器**（Windows 上的交易节点、将来包一层的 MCP server）。
人在浏览器里点错一个字段会发现，自动生成的客户端不会——它按 schema 编译，
字段改名/类型变化在它那边是**运行时才炸**，而那时错误现场在对面。

`/capabilities` 更是「先问再做」的入口：外部节点靠它判断这个部署有什么、
实盘开没开。它曾经返回裸 `dict[str, Any]`，在 OpenAPI 里是一团
`additionalProperties`——等于没有说明书。

本文件钉住三件事：
1. 响应是**有类型**的（`$ref` 到具名 schema，不是裸 object）；
2. 字段集合**被钉死**——增删字段必须来改这里，不能悄悄发生；
3. 鉴权声明**出现在 schema 里**（机器客户端据此决定带不带 Authorization）。
"""

from __future__ import annotations

import warnings

import pytest

from backend.shared import live_trading_gate as gate

#: 对外能力文档的字段集合。改这里 = 改对外契约，必须是有意的。
EXPECTED_CAPABILITY_FIELDS = {
    "api_version",
    "server_time",
    "principal",
    "trading",
    "planes",
}

#: 五个「面」。批次 3/4 只翻 `available`，不该增删这个集合。
EXPECTED_PLANES = {"control", "task", "data", "stream", "trading"}


def _spec() -> dict:
    from backend.services.api.main import app

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # 网关里已有若干 duplicate-operation-id 噪声
        return app.openapi()


def _resolve(spec: dict, node: dict) -> dict:
    """跟一层 `$ref` 到 components.schemas。"""
    ref = node.get("$ref")
    if not ref:
        return node
    return spec["components"]["schemas"][ref.rsplit("/", 1)[-1]]


def _json_schema(spec: dict, path: str, method: str, status: str = "200") -> dict:
    op = spec["paths"][path][method]
    return op["responses"][status]["content"]["application/json"]["schema"]


# ---------------------------------------------------------------------------
# /capabilities —— 外部节点的发现文档
# ---------------------------------------------------------------------------


def test_capabilities_response_is_a_named_typed_schema() -> None:
    """**核心断言**：必须是 `$ref` 到具名 schema，不是裸 object。

    这条是防空转的：裸 `dict[str, Any]` 也能通过「有 responses.200」这种松断言，
    但它给不出任何字段信息。
    """
    schema = _json_schema(_spec(), "/api/ext/v1/capabilities", "get")
    assert "$ref" in schema, (
        "capabilities 的响应 schema 不是具名模型（退化成了裸 object/dict）——"
        "外部节点据此生成的客户端会拿不到任何字段信息。"
    )
    assert schema["$ref"].endswith("/CapabilitiesResponse")


def test_capabilities_field_set_is_pinned() -> None:
    """字段集合钉死：增删都必须在测试里显式改一次。"""
    resolved = _resolve(
        _spec(), _json_schema(_spec(), "/api/ext/v1/capabilities", "get")
    )
    assert set(resolved.get("properties", {})) == EXPECTED_CAPABILITY_FIELDS
    # 全部必填：可空字段会让客户端到处写 if
    assert set(resolved.get("required", [])) == EXPECTED_CAPABILITY_FIELDS


def test_capabilities_planes_are_typed_and_complete() -> None:
    """`planes` 的每一项必须是有类型的，且五个面一个不少。"""
    spec = _spec()
    resolved = _resolve(spec, _json_schema(spec, "/api/ext/v1/capabilities", "get"))
    items = resolved["properties"]["planes"]["items"]
    plane = _resolve(spec, items)
    assert set(plane.get("properties", {})) == {
        "plane",
        "description",
        "transport",
        "available",
    }


@pytest.mark.asyncio
async def test_capabilities_served_planes_match_the_plan() -> None:
    """**实际返回**的 plane 集合与规划一致。

    上面那条测的是 schema（类型），这条测的是内容（是否真有那五个面）。
    分开是因为「类型对了但只返回两个面」这种漂移，schema 检查看不到。
    """
    import time

    from backend.services.api.routers.external.auth import ExternalPrincipal
    from backend.services.api.routers.external.router import get_capabilities

    principal = ExternalPrincipal(
        access_key="ak-contract-test",
        user_id="u-1",
        tenant_id="default",
        permissions=(),
        session_expires_at=0,
    )
    result = await get_capabilities(principal)  # type: ignore[arg-type]
    assert {p.plane for p in result.planes} == EXPECTED_PLANES
    assert result.api_version == "v1"
    # server_time 必须是「现在的」Unix 秒——客户端用它对齐时钟/判断新鲜度
    assert abs(result.server_time - time.time()) < 60


# ---------------------------------------------------------------------------
# 鉴权声明：机器客户端按 schema 决定带不带 Authorization
# ---------------------------------------------------------------------------


def test_capabilities_declares_bearer_security() -> None:
    """机器接口的 `security` 不能是 null——那等于告诉客户端「不用带鉴权」。

    这是本仓踩过的坑：用 `Header()` 而不是 `HTTPBearer` 时，OpenAPI 里
    `security` 是 null，Authorization 只表现为一个**可选普通请求头**，
    自动生成的客户端会默认不带它。
    """
    op = _spec()["paths"]["/api/ext/v1/capabilities"]["get"]
    assert op.get("security") == [{"HTTPBearer": []}]


def test_handshake_endpoint_declares_no_security() -> None:
    """反向对照：握手端点**本来**就是匿名可达的（它就是去换令牌的）。

    它若被标成需要鉴权，客户端会陷入「先有令牌才能拿令牌」的死循环。
    """
    op = _spec()["paths"]["/api/ext/v1/auth/session"]["post"]
    assert op.get("security") in (None, [])


def test_bearer_scheme_is_declared_in_components() -> None:
    schemes = _spec()["components"].get("securitySchemes", {})
    assert "HTTPBearer" in schemes, f"没有声明 HTTPBearer：{sorted(schemes)}"
    assert schemes["HTTPBearer"].get("type") == "http"
    assert schemes["HTTPBearer"].get("scheme") == "bearer"


# ---------------------------------------------------------------------------
# 握手请求契约
# ---------------------------------------------------------------------------


def test_session_request_requires_both_credential_fields() -> None:
    """两个字段都必填且有长度下界——太短的值不该进到 bcrypt 那一步。"""
    spec = _spec()
    op = spec["paths"]["/api/ext/v1/auth/session"]["post"]
    body = op["requestBody"]["content"]["application/json"]["schema"]
    req = _resolve(spec, body)
    assert set(req["properties"]) == {"access_key", "secret_key"}
    assert set(req.get("required", [])) == {"access_key", "secret_key"}
    for field in ("access_key", "secret_key"):
        assert req["properties"][field].get("minLength", 0) >= 8, (
            f"{field} 没有长度下界——空串/单字符会直接进 bcrypt 比对"
        )


def test_session_response_field_names_follow_rfc6749() -> None:
    """握手响应的字段名用标准名——机器客户端的实现者按标准去找。

    `token_type`（值是 `Bearer`）是 RFC 6749 §5.1 的字段名。此前叫
    `token_prefix`：自造词，外部实现者不会猜到，也没有任何标准工具认得。
    """
    spec = _spec()
    resp = _resolve(spec, _json_schema(spec, "/api/ext/v1/auth/session", "post"))
    fields = set(resp.get("properties", {}))
    assert "token_type" in fields, f"握手响应缺 token_type（RFC 6749）：{sorted(fields)}"
    assert "token_prefix" not in fields, "自造的 token_prefix 又回来了"
    # 续期语义必须在契约里说清楚，否则外部节点会等 401 才换令牌
    assert {"token", "expires_at", "ttl_seconds", "renew_after"} <= fields


def test_ext_namespace_is_actually_served() -> None:
    """防空转：schema 里一条对外路径都没有时，上面所有断言都在空转。

    路径集合的**唯一出处**是 `test_external_api_gate_coverage.EXPECTED_EXT_ROUTES`
    （那边同时拿它判闸门登记是否覆盖）。这里 import 过来而不是再抄一份：
    两处各抄一份的结果是加端点时只改一处，另一处变成一条在测旧世界的断言。
    """
    from backend.tests.test_external_api_gate_coverage import EXPECTED_EXT_ROUTES

    served = {p for p in _spec()["paths"] if p.startswith(gate.EXT_API)}
    assert served == EXPECTED_EXT_ROUTES


def test_data_plane_is_reachable_through_the_namespace() -> None:
    """数据面在 schema 里必须真的挂着，且**一个面一个前缀**。

    `/api/ext/v1/data/data/...` 这种「前缀写重了一遍」是第一版真实犯过的错
    （子路由里又写了一次 `/data`）。它在 router 级断言里看不出来——路径拼出来
    仍然是合法的、只是多了一层。所以这里直接盯着形状。
    """
    paths = {p for p in _spec()["paths"] if p.startswith(f"{gate.EXT_API}/data/")}
    assert paths, "数据面一条路径都没有挂上"
    doubled = [p for p in paths if p.startswith(f"{gate.EXT_API}/data/data/")]
    assert not doubled, f"数据面前缀写重了：{doubled}"
