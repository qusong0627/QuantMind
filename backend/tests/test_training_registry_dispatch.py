"""B2 验收（静态）：MODEL_REGISTRY 完整性 + 分派点查表化锁定。

无第三方依赖（AST 解析），本地可跑；数值等价 A/B 在服务器 GPU 环境执行。
- 6 个 GBDT/sklearn 训练器必须全部注册且 framework 映射正确。
- train_model / _train_single_model 内不得再直调裸训练函数（只允许经
  _dispatch_gbdt_sklearn；optuna/分位 helper 名字不同不受影响）。
- _train_wfa_single 保持直调（子集语义 warning+None，B2 明确不碰，见注释）。
- fallback 开关（TRAINING_OLD_DISPATCH）与未知类型 ValueError 路径必须存在。
"""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TRAIN_PY = REPO_ROOT / "docker" / "training" / "train.py"

GBDT_SIX = {
    "_train_lgb",
    "_train_xgb",
    "_train_catboost",
    "_train_linear",
    "_train_rf",
    "_train_mlp",
}

EXPECTED_REGISTRY = {
    "lightgbm": "lightgbm",
    "xgboost": "xgboost",
    "catboost": "catboost",
    "linear": "sklearn",
    "random_forest": "sklearn",
    "mlp": "sklearn",
}


def _load_tree() -> ast.Module:
    return ast.parse(TRAIN_PY.read_text(encoding="utf-8"))


def _funcs(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    return {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}


def _direct_trainer_calls(fn: ast.FunctionDef) -> set[str]:
    return {
        n.func.id
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id in GBDT_SIX
    }


def _calls_dispatch(fn: ast.FunctionDef) -> bool:
    return any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_dispatch_gbdt_sklearn"
        for n in ast.walk(fn)
    )


def test_registry_covers_gbdt_six_with_frameworks():
    tree = _load_tree()
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            if (
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Name)
                and dec.func.id == "register_trainer"
                and dec.args
                and isinstance(dec.args[0], ast.Constant)
            ):
                fw = "unknown"
                for kw in dec.keywords:
                    if kw.arg == "framework" and isinstance(kw.value, ast.Constant):
                        fw = kw.value.value
                found[dec.args[0].value] = fw
    assert found == EXPECTED_REGISTRY


def test_dispatch_points_use_registry_not_direct_calls():
    funcs = _funcs(_load_tree())
    for name in ("train_model", "_train_single_model"):
        assert _calls_dispatch(funcs[name]), name
        assert _direct_trainer_calls(funcs[name]) == set(), name


def test_wfa_single_keeps_direct_calls_out_of_scope():
    # WFA 只支持树+linear（其余 warning+None），语义与注册表不同，B2 不碰：
    # 锁定其直调现状，防止顺手“统一”造成行为变更。
    funcs = _funcs(_load_tree())
    assert _direct_trainer_calls(funcs["_train_wfa_single"]) == {
        "_train_lgb",
        "_train_xgb",
        "_train_catboost",
        "_train_linear",
    }
    assert not _calls_dispatch(funcs["_train_wfa_single"])


def test_fallback_switch_and_error_path_exist():
    tree = _load_tree()
    funcs = _funcs(tree)
    assert "_dispatch_gbdt_sklearn" in funcs and "_use_old_dispatch" in funcs
    src = TRAIN_PY.read_text(encoding="utf-8")
    assert "TRAINING_OLD_DISPATCH" in src
    raises = [
        n for n in ast.walk(funcs["_dispatch_gbdt_sklearn"]) if isinstance(n, ast.Raise)
    ]
    assert raises, "unknown model_type must raise ValueError"
    assert any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_use_old_dispatch"
        for n in ast.walk(funcs["_dispatch_gbdt_sklearn"])
    )
