"""score_direction（反向模型分数翻转）回归测试。

背景：训练把方向记在 `metadata.json` 的 **metrics.score_direction**，而推理模板读的是
**顶层** `meta["score_direction"]`（训练从不写顶层）→ 模板里的 `scores = -scores` 从未执行。
后果：验证集 IC 为负的模型（应翻转）分数一直是反的，且**从界面上看不出来**。

本文件钉住三件事：
1. 迁移脚本的方向判定与取负逻辑；
2. 真实产物：标记为 reversed 的模型，其 `pred.parquet` 在**验证集**上必须与 label 正相关
   （即方向已生效）——这条能抓住「将来重跑拆分时又把未取负的值写回去」；
3. 三份推理脚本（两个模板 + 训练端内嵌兜底）都必须从 metrics 取值 —— 这是一次
   「读错位置」的静默失效，运行时没有任何报错，只能靠源码守卫。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# 模型产物在容器内是 /app/models，宿主是 <repo>/models；测试里两个都试
MODELS_CANDIDATES = [Path("/app/models/users"), REPO_ROOT / "models" / "users"]


def _load_migration_module():
    """加载迁移脚本（backend/scripts 不在包路径里，用文件加载）。"""
    path = REPO_ROOT / "backend" / "scripts" / "fix_pred_score_direction.py"
    spec = importlib.util.spec_from_file_location("fix_pred_score_direction", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _iter_model_dirs():
    for root in MODELS_CANDIDATES:
        if root.exists():
            yield from sorted(root.glob("*/*/*/mdl_*"))
            return


# ── 1. 迁移脚本的方向判定 ──────────────────────────────────────────────

@pytest.mark.parametrize(
    "meta,expected",
    [
        ({"metrics": {"score_direction": "reversed"}}, "reversed"),
        ({"metrics": {"score_direction": "normal"}}, "normal"),
        ({"score_direction": "reversed"}, "reversed"),          # 历史写法（顶层）
        ({"score_direction": "normal", "metrics": {"score_direction": "reversed"}}, "normal"),  # 顶层优先
        ({}, ""),
        ({"metrics": None}, ""),
    ],
)
def test_direction_of_reads_metrics_with_top_level_fallback(meta, expected):
    mod = _load_migration_module()
    assert mod._direction_of(meta) == expected


def test_negate_parquet_flips_sign_only(tmp_path):
    mod = _load_migration_module()
    p = tmp_path / "pred.parquet"
    pd.DataFrame({"pred": [1.0, -2.0, 0.5], "label": [0.1, 0.2, 0.3]}).to_parquet(p)
    rows = mod._negate_parquet(p)
    out = pd.read_parquet(p)
    assert rows == 3
    assert out["pred"].tolist() == [-1.0, 2.0, -0.5]
    assert out["label"].tolist() == [0.1, 0.2, 0.3], "只应改 pred 列"


# ── 2. 真实产物：reversed 模型的方向必须已生效 ─────────────────────────

def _reversed_models_with_pred():
    out = []
    for d in _iter_model_dirs():
        meta_path, pred_path = d / "metadata.json", d / "pred.parquet"
        if not (meta_path.exists() and pred_path.exists()):
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if (meta.get("metrics") or {}).get("score_direction") == "reversed":
            out.append((d, meta, pred_path))
    return out


def test_reversed_models_pred_is_positively_correlated_on_valid():
    """反向模型的 pred 产物在验证集上必须与 label 正相关（正分=看涨）。

    迁移前它们的验证集 rank IC 是负的（如 xgboost -0.0292）；若有人重跑拆分却没带
    方向修正，这里会重新变负 —— 这正是本断言要拦的回归。
    """
    models = _reversed_models_with_pred()
    if not models:
        pytest.skip("没有 score_direction=reversed 且带 pred.parquet 的模型")

    bad = []
    for d, _meta, pred_path in models:
        df = pd.read_parquet(pred_path, columns=["pred", "label", "split"])
        valid = df[df["split"] == "valid"]
        if not len(valid):
            continue
        ic = valid["pred"].corr(valid["label"], method="spearman")
        if ic is not None and ic < 0:
            bad.append(f"{d.name}: valid rank_ic={ic:+.4f}")
    assert not bad, "reversed 模型的分数仍是反的：\n" + "\n".join(bad)


def test_reversed_models_are_marked_migrated():
    """迁移必须留下幂等标记，否则重复执行会来回翻转。"""
    for d, meta, _pred in _reversed_models_with_pred():
        assert meta.get("pred_direction_applied") == "reversed", f"{d.name} 缺少迁移标记"


# ── 3. 源码守卫：三份推理脚本都必须从 metrics 取值 ─────────────────────

INFERENCE_SCRIPTS = [
    "backend/services/engine/inference/templates/inference_parquet.py",
    "backend/services/engine/inference/templates/inference_ensemble_src.py",
    "docker/training/train.py",
]


@pytest.mark.parametrize("rel", INFERENCE_SCRIPTS)
def test_inference_scripts_read_score_direction_from_metrics(rel):
    """方向必须能从 metrics 读到（顶层为兼容）。

    这是**读错位置**型缺陷：写成只读顶层时不会有任何报错，只是翻转永不发生 ——
    所以用源码守卫钉住，比事后从分数反推更可靠。
    """
    src = (REPO_ROOT / rel).read_text(encoding="utf-8")
    assert "score_direction" in src, f"{rel} 里已看不到 score_direction"
    assert 'metrics' in src, f"{rel} 未从 metrics 读取 score_direction"
    # 允许两种写法：meta.get("score_direction") or metrics.get(...) / 显式 or 链
    assert 'get("score_direction")' in src or "get('score_direction')" in src
