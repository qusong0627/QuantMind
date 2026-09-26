"""引擎推理模型管理端点的鉴权/归属契约（BUG-09 回归）。

原实现三个端点调用 `InferenceService` 上**不存在**的方法，恒抛 AttributeError
被 `except Exception` 吞成 HTTP 500：

    GET    /api/v1/inference/models          → inference_service.list_models()   → 500
    POST   /api/v1/inference/models/load     → inference_service.load_model()   → 500
    DELETE /api/v1/inference/models/{id}     → inference_service.unload_model() → 500

第四个端点虽然存在，但用 production_dir 解析模型目录，看不到任何用户模型，
所有用户模型一律 404。

另外它没有任何归属维度：只要登录（引擎中间件对 /api/v1/inference/ 前缀强制要求
user_id），就能列举/加载/卸载任意用户的模型。

本测试用 AST 固化修复后的契约：
- 四个端点必须取用认证身份（get_authenticated_identity）
- 不得再调用 InferenceService 上不存在的三个方法
- 模型归属必须经注册表（model_registry_service）校验
- ModelLoader 必须提供 unload_model
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]
ROUTER_PY = _BACKEND / "services" / "engine" / "routers" / "inference.py"
LOADER_PY = _BACKEND / "services" / "engine" / "inference" / "model_loader.py"

MANAGED_ENDPOINTS = {"list_models", "get_model_info", "load_model", "unload_model"}

# 这三个方法在 InferenceService 上并不存在
NONEXISTENT_SERVICE_METHODS = {"list_models", "load_model", "unload_model"}


def _router_tree() -> ast.Module:
    return ast.parse(ROUTER_PY.read_text(encoding="utf-8"))


def _endpoint_funcs() -> dict[str, ast.AST]:
    out: dict[str, ast.AST] = {}
    for node in ast.walk(_router_tree()):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = node
    return out


def test_managed_endpoints_take_request_and_read_identity() -> None:
    """四个端点都必须注入 Request 并取用认证身份，否则无法做归属判断。"""
    funcs = _endpoint_funcs()
    problems = []
    for name in sorted(MANAGED_ENDPOINTS):
        func = funcs.get(name)
        if func is None:
            problems.append(f"{name}: 端点不存在")
            continue
        arg_names = {a.arg for a in func.args.args} | {a.arg for a in func.args.kwonlyargs}
        if "http_request" not in arg_names:
            problems.append(f"{name}: 缺少 http_request: Request 参数")
        src = ast.dump(func)
        if "get_authenticated_identity" not in src:
            problems.append(f"{name}: 未调用 get_authenticated_identity")
    assert not problems, "鉴权契约不满足:\n  " + "\n  ".join(problems)


def test_no_calls_to_nonexistent_service_methods() -> None:
    """不得再调用 InferenceService 上不存在的三个方法（否则恒 500）。"""
    offenders = []
    for node in ast.walk(_router_tree()):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not isinstance(fn, ast.Attribute) or fn.attr not in NONEXISTENT_SERVICE_METHODS:
            continue
        base = fn.value
        # inference_service.list_models(...) 形式
        if isinstance(base, ast.Name) and base.id == "inference_service":
            offenders.append(fn.attr)
    assert not offenders, f"仍在调用不存在的 InferenceService 方法: {sorted(set(offenders))}"


def test_model_ownership_goes_through_registry() -> None:
    """归属校验必须经注册表（tenant/user 维度），不能只靠 production_dir。"""
    src = ROUTER_PY.read_text(encoding="utf-8")
    assert "model_registry_service" in src, "未引入 model_registry_service，无法做归属校验"
    assert "get_model(" in src, "未通过注册表 get_model 校验模型归属"
    assert "_resolve_owned_model_dir" in src, "缺少归属校验 + 目录解析的统一入口"


def test_cache_namespace_is_tenant_scoped() -> None:
    """load/unload 必须使用租户/用户命名空间，否则会误卸载他人模型。"""
    src = ROUTER_PY.read_text(encoding="utf-8")
    assert "_model_cache_key" in src, "缺少租户/用户缓存命名空间构造"
    assert 'f"{tenant_id}:{user_id}"' in src, "缓存命名空间未包含 tenant_id/user_id"


def test_model_loader_exposes_unload_model() -> None:
    """ModelLoader 必须提供 unload_model（原先不存在，导致端点 500）。"""
    tree = ast.parse(LOADER_PY.read_text(encoding="utf-8"))
    loader = next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.ClassDef) and n.name == "ModelLoader"
        ),
        None,
    )
    assert loader is not None, "未找到 ModelLoader 类"
    methods = {
        n.name for n in loader.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for required in ("load_model", "get_model", "unload_model"):
        assert required in methods, f"ModelLoader 缺少 {required}"


def test_unload_requires_explicit_cache_key() -> None:
    """unload_model 的 cache_key 必须是显式关键字参数，避免调用方漏传而误删全局缓存。"""
    tree = ast.parse(LOADER_PY.read_text(encoding="utf-8"))
    loader = next(
        n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "ModelLoader"
    )
    fn = next(
        n
        for n in loader.body
        if isinstance(n, ast.FunctionDef) and n.name == "unload_model"
    )
    assert fn.args.kwonlyargs, "unload_model 应把 cache_key 设为 keyword-only"
    assert {a.arg for a in fn.args.kwonlyargs} >= {"cache_key"}
