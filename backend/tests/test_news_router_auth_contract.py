"""新闻代理路由鉴权契约（BUG-01 回归）。

原实现 `/api/v1/news/*` 全部端点无任何鉴权：router 不带 dependencies，
无一个端点声明 Depends，API 服务的中间件只有 request_id/错误处理/访问日志/CORS，
nginx 也没有 auth_request。叠加业务端口对局域网公开，未授权访问者可直接调用
`/admin/purge-old`（删数据）、`/admin/sources`（改连接器配置）与
`/enrichment/run`（无界触发 GPU）。

本测试用 AST 固化修复后的分层，避免后续新增端点再次漏加：
- router         业务端点，router 级 `dependencies=[Depends(get_current_user)]`
- public_router  浏览器直取的资源，必须保持免鉴权
- 破坏性/配置类端点额外要求 `Depends(require_admin)`

用 AST 而非导入：news.py 的导入链会拉起 httpx/FastAPI 及鉴权中间件（需 DB），
在纯单元测试环境不可用。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]
NEWS_PY = _BACKEND / "services" / "api" / "routers" / "news.py"
MAIN_PY = _BACKEND / "services" / "api" / "main.py"

# 必须保持免鉴权的端点：浏览器直取，无法携带 Authorization 头
EXPECTED_PUBLIC = {
    "/health",
    "/rsshub/{path:path}",
    "/huntly-ui/api/{path:path}",
    "/huntly-ui/{path:path}",
    "/huntly-ui",
}

# 必须要求管理员权限的端点（(方法, 路径)；api_route 记为 "ANY"）
EXPECTED_ADMIN = {
    ("POST", "/sources/{source_id}/refresh"),
    ("GET", "/admin/folders"),
    ("POST", "/admin/folders"),
    ("PUT", "/admin/folders/{folder_id}"),
    ("DELETE", "/admin/folders/{folder_id}"),
    ("GET", "/admin/preview"),
    ("POST", "/admin/sources"),
    ("PUT", "/admin/sources/{connector_id}"),
    ("DELETE", "/admin/sources/{connector_id}"),
    ("GET", "/admin/sources/{connector_id}/setting"),
    ("POST", "/enrichment/run"),
    ("POST", "/enrichment/rebuild-all"),
    ("GET", "/admin/tags"),
    ("POST", "/admin/tags"),
    ("PUT", "/admin/tags/{tag_id}"),
    ("DELETE", "/admin/tags/{tag_id}"),
    ("PATCH", "/admin/tags/{tag_id}/toggle"),
    ("POST", "/admin/purge-old"),
}


def _has_dep(func: ast.AST, name: str) -> bool:
    """函数签名默认值里是否出现 `Depends(<name>)`。

    注意参数在 `Depends(...)` 的 args 里，不是 default.func 本身。
    """
    args = func.args
    defaults = list(args.defaults) + [d for d in args.kw_defaults if d is not None]
    for default in defaults:
        if not isinstance(default, ast.Call):
            continue
        inner = default.func
        if not (isinstance(inner, ast.Name) and inner.id == "Depends"):
            continue
        for arg in default.args:
            if isinstance(arg, ast.Name) and arg.id == name:
                return True
    return False


def _collect_routes() -> list[dict]:
    tree = ast.parse(NEWS_PY.read_text(encoding="utf-8"))
    routes: list[dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            fn = dec.func
            if not isinstance(fn, ast.Attribute) or not isinstance(fn.value, ast.Name):
                continue
            if fn.value.id not in ("router", "public_router"):
                continue
            method = "ANY" if fn.attr == "api_route" else fn.attr.upper()
            path = dec.args[0].value if dec.args and isinstance(dec.args[0], ast.Constant) else None
            routes.append(
                {
                    "router": fn.value.id,
                    "method": method,
                    "path": path,
                    "func": node,
                }
            )
    return routes


@pytest.fixture(scope="module")
def routes() -> list[dict]:
    return _collect_routes()


def test_router_requires_login_at_router_level(routes: list[dict]) -> None:
    """业务 router 必须在 router 级声明 get_current_user，而非逐端点补。"""
    src = NEWS_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    router_def = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "router" for t in node.targets
        ):
            router_def = node
            break
    assert router_def is not None, "未找到 router 定义"
    assert isinstance(router_def.value, ast.Call), "router 应由 APIRouter(...) 构造"

    deps_kw = next(
        (kw for kw in router_def.value.keywords if kw.arg == "dependencies"), None
    )
    assert deps_kw is not None, "router 缺少 dependencies=，端点将全部无鉴权"
    names = {
        n.id
        for n in ast.walk(deps_kw.value)
        if isinstance(n, ast.Name)
    }
    assert "get_current_user" in names, "router.dependencies 必须包含 Depends(get_current_user)"


def test_public_endpoints_are_exactly_the_browser_fetched_ones(routes: list[dict]) -> None:
    """public_router 上只能有浏览器直取的资源，多一个都是缺口。"""
    public = {r["path"] for r in routes if r["router"] == "public_router"}
    assert public == EXPECTED_PUBLIC, (
        f"public_router 端点集合不符\n  多出: {public - EXPECTED_PUBLIC}\n  缺少: {EXPECTED_PUBLIC - public}"
    )


def test_no_admin_endpoint_is_public(routes: list[dict]) -> None:
    """任何 /admin/* 或触发型端点都不允许出现在免鉴权 router 上。"""
    leaked = {
        r["path"]
        for r in routes
        if r["router"] == "public_router"
        and r["path"]
        and (r["path"].startswith("/admin") or r["path"].startswith("/enrichment"))
    }
    assert not leaked, f"免鉴权 router 上出现管理/触发端点: {leaked}"


def test_admin_endpoints_require_admin(routes: list[dict]) -> None:
    """破坏性/配置类端点必须要求管理员权限。"""
    missing = [
        (r["method"], r["path"])
        for r in routes
        if r["path"]
        and (r["path"].startswith("/admin") or r["path"] in {
            "/sources/{source_id}/refresh",
            "/enrichment/run",
            "/enrichment/rebuild-all",
        })
        and not _has_dep(r["func"], "require_admin")
    ]
    assert not missing, f"以下端点缺少 Depends(require_admin): {missing}"


def test_all_expected_admin_endpoints_exist(routes: list[dict]) -> None:
    """反向校验：EXPECTED_ADMIN 不能因端点改名而失效。"""
    actual = {(r["method"], r["path"]) for r in routes}
    absent = EXPECTED_ADMIN - actual
    assert not absent, f"契约里登记的端点已不存在（改名？）: {absent}"


def test_route_total_is_32(routes: list[dict]) -> None:
    """端点总数守恒：拆分 router 不应丢失任何端点。"""
    assert len(routes) == 32, f"端点总数 {len(routes)} != 32"


def test_main_registers_both_routers() -> None:
    """两个 router 都要挂到 app 上，否则 public 端点会 404。"""
    src = MAIN_PY.read_text(encoding="utf-8")
    assert "news_router" in src, "main.py 未注册业务 news_router"
    assert "news_public_router" in src, "main.py 未注册 news_public_router"


def test_rsshub_proxy_rejects_path_escape() -> None:
    """免鉴权代理必须收敛 path，避免被当作任意路径转发器。"""
    src = NEWS_PY.read_text(encoding="utf-8")
    start = src.index("async def proxy_rsshub_asset")
    body = src[start : start + 1400]
    assert '".."' in body or "'..'" in body, "rsshub 代理未拦截 .. 路径穿越"
    assert "://" in body, "rsshub 代理未拦截带 scheme 的绝对地址"


def _module_assign_source(src: str, tree: ast.AST, name: str) -> str | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return ast.get_source_segment(src, node)
    return None


def test_huntly_ui_writes_require_login() -> None:
    """Huntly UI 代理的写方法必须要求登录态（审计 C3）：

    此前该前缀全方法匿名可达，任何无凭据客户端都能借后端注入的管理员会话
    写 Huntly（增删连接器/数据）。读方法保持公开（新标签页 SPA 资源加载）。
    """
    src = NEWS_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)

    write_methods = _module_assign_source(src, tree, "_HUNTLY_UI_WRITE_METHODS")
    assert write_methods, "未定义 _HUNTLY_UI_WRITE_METHODS——写方法集合缺失"
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert method in write_methods, f"写方法集合缺少 {method}"

    handler = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_huntly_ui_proxy_api"
        ):
            handler = ast.get_source_segment(src, node)
    assert handler, "未找到 _huntly_ui_proxy_api"
    assert "get_current_user" in handler, "Huntly UI 写方法未要求登录态"
    assert 'startswith("bearer ")' in handler or "startswith('bearer ')" in handler, (
        "写门未从 Authorization: Bearer 头解析凭据"
    )


def test_huntly_ui_rewrite_injects_access_token() -> None:
    """内嵌 UI 重写脚本必须把 localStorage 的 access_token 注入请求头。

    后端写门要求登录态后，内嵌 UI 的写请求（fetch/XHR）若不携带 JWT 会 401；
    重写脚本运行在 QuantMind 同源页面里，可直读登录态。
    """
    tree = ast.parse(NEWS_PY.read_text(encoding="utf-8"))
    script = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_HUNTLY_UI_REWRITE_SCRIPT"
            for t in node.targets
        ):
            script = ast.literal_eval(node.value)
    assert script, "未找到 _HUNTLY_UI_REWRITE_SCRIPT"
    assert "access_token" in script, "重写脚本未注入 QuantMind JWT（写方法将 401）"
    assert "Authorization" in script, "重写脚本未设置 Authorization 头"
    assert "WebSocket" in script, "WS 重写丢失"
    assert "/api/v1/news/huntly-ui/api/" in script, "API 路径重写丢失"
