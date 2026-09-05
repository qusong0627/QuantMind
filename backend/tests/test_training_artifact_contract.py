"""B4 产物契约快照：落盘文件名/框架映射/默认产物清单被测试锁死。

B0 不变量：“落盘文件名与字段逐字节不能变”。本文件用 AST 锁定
_save_model / _get_model_framework 的映射，schema 侧锁定默认产物清单，
任何改名/改落盘行为必须先改本快照（显式评审）。
无重型依赖，本地可跑。
"""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TRAIN_PY = REPO_ROOT / "docker" / "training" / "train.py"
ORCH = (
    REPO_ROOT
    / "backend"
    / "services"
    / "engine"
    / "training"
    / "local_docker_orchestrator.py"
)

EXPECTED_SAVE_MAPPING = {
    "lightgbm": "model.lgb",
    "xgboost": "model.xgb",
    "catboost": "model.cbm",
    "linear": "model.pkl",
    "mlp": "model.pkl",
    "hybrid_gru_tree": "model.pkl",
    "gru": "model.pth",
    "lstm": "model.pth",
    "alstm": "model.pth",
    "transformer": "model.pth",
    "tabnet": "model.pth",
    "tcn": "model.pth",
    # random_forest / tra / hist 等走兜底分支
    "ELSE": "model.pkl",
}

EXPECTED_FRAMEWORKS = {
    "lightgbm": "lightgbm",
    "xgboost": "xgboost",
    "catboost": "catboost",
    "linear": "sklearn",
    "random_forest": "sklearn",
    "gru": "pytorch",
    "lstm": "pytorch",
    "alstm": "pytorch",
    "transformer": "pytorch",
    "tra": "pytorch",
    "hist": "pytorch",
    "tabnet": "pytorch",
    "tcn": "pytorch",
    "nativetft": "pytorch",
    "mlp": "pytorch",
    "hybrid_gru_tree": "pytorch",
}

EXPECTED_REQUIRED_ARTIFACTS = ["model.lgb", "pred.pkl", "metadata.json", "result.json"]


def _module(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _func(tree: ast.Module, name: str) -> ast.FunctionDef:
    return next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name
    )


def _set_consts(tree: ast.Module, name: str) -> set[str]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return {e.value for e in node.value.elts if isinstance(e, ast.Constant)}
    raise AssertionError(f"{name} not found")


def _eq_values(test: ast.expr, var: str) -> set[str] | None:
    """model_type == 'x' → {'x'}；model_type in NAME → 解析集合；否则 None。"""
    if (
        isinstance(test, ast.Compare)
        and len(test.ops) == 1
        and len(test.comparators) == 1
    ):
        left, op, right = test.left, test.ops[0], test.comparators[0]
        if isinstance(left, ast.Name) and left.id == var and isinstance(op, ast.Eq):
            if isinstance(right, ast.Constant):
                return {right.value}
        if isinstance(left, ast.Name) and left.id == var and isinstance(op, ast.In):
            if isinstance(right, ast.Name):
                return set(_set_consts(_module(TRAIN_PY), right.id))
    return None


def _save_mapping() -> dict[str, str]:
    """遍历 _save_model，记录每个 model_type 分区返回的文件名。

    同一 block 内按语句顺序维护剩余集：分支命中后从剩余集中扣除，
    裸 return 只落到尚未命中的剩余分区（与 Python 语义一致）。
    """
    tree = _module(TRAIN_PY)
    fn = _func(tree, "_save_model")
    mapping: dict[str, str] = {}

    def visit_block(stmts, active):
        remaining = set(active) if active is not None else None
        for s in stmts:
            if isinstance(s, ast.If):
                hit = _eq_values(s.test, "model_type")
                if hit is None:
                    visit_block(s.body, remaining)
                    visit_block(s.orelse, remaining)
                else:
                    visit_block(s.body, hit if remaining is None else (remaining & hit))
                    if remaining is not None:
                        remaining -= hit
                    visit_block(s.orelse, remaining)
            elif isinstance(s, ast.Return) and isinstance(s.value, ast.Constant):
                if remaining is None:
                    mapping["ELSE"] = s.value.value
                else:
                    for v in remaining:
                        mapping[v] = s.value.value
                return
            else:
                for child in ast.iter_child_nodes(s):
                    visit_block([child], remaining)

    visit_block(fn.body, None)
    return mapping


def test_save_model_filename_contract():
    mapping = _save_mapping()
    for model_type, filename in EXPECTED_SAVE_MAPPING.items():
        assert mapping.get(model_type) == filename, (
            model_type,
            mapping.get(model_type),
        )


def _dict_const(mapping_node: ast.Dict) -> dict[str, str]:
    return {
        k.value: v.value
        for k, v in zip(mapping_node.keys, mapping_node.values, strict=True)
        if isinstance(k, ast.Constant) and isinstance(v, ast.Constant)
    }


def test_framework_mapping_contract():
    tree = _module(TRAIN_PY)
    fn = _func(tree, "_get_model_framework")
    found: dict[str, str] = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.Dict):
            found.update(_dict_const(node))
    assert found == EXPECTED_FRAMEWORKS


def test_required_artifacts_default_contract():
    from backend.shared.training.schemas import TrainingConfig

    assert TrainingConfig().output.required_artifacts == EXPECTED_REQUIRED_ARTIFACTS
    orch_src = ORCH.read_text(encoding="utf-8")
    assert '["model.lgb", "pred.pkl", "metadata.json", "result.json"]' in orch_src
