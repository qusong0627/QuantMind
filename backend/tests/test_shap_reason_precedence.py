"""SHAP 归因失败码的**优先级**：架构不支持 vs 产物损坏。

实测背景（20260920）：CN 那批 torch 模型的 metadata.json 声明
``model_file="model_nativetft.pth"``，而目录里实际只有 ``model.pth``。
原来的判定把这种「声明文件不存在」一律报成 ``model_artifact_mismatch``，
于是界面上写的是：

    「模型目录里的元数据与其权重文件不一致（疑似同批次训练覆写）……
      建议重新训练或修复该模型产物。」

而真实结论是 ``framework_unsupported:pytorch`` —— nativeftt 本来就**不在**
树模型归因通道里，这是模型的固有属性，重训一百遍也拿不到 SHAP。
假原因把用户送去做无用功，比没有原因更糟。

判定规则：只有「本该能归因」时才说产物不一致 —— 声明框架是树模型，
或声明的文件名本身就是树产物（.lgb/.xgb/.cbm）。

跑法（纯单测，不需要容器/数据库）：
    python -m pytest backend/tests/test_shap_reason_precedence.py -q
"""

from __future__ import annotations

import glob as _glob_mod
import json
from pathlib import Path

import pytest

from backend.services.api.routers import research_service as rs

_DUMMY_ID = "mdl_test_reason_precedence"


def _make_model_dir(tmp_path: Path, metadata: dict, weights: list[str]) -> Path:
    """造一个模型目录：目录名必须是 model_id（调用方按目录名回查注册表）。"""
    model_dir = tmp_path / _DUMMY_ID
    model_dir.mkdir(parents=True)
    (model_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False), encoding="utf-8"
    )
    for name in weights:
        (model_dir / name).write_bytes(b"stub")
    return model_dir


@pytest.fixture
def wire_model_dir(tmp_path, monkeypatch):
    """让 `_compute_shap_drivers_sync` 只看我们造的目录，且不碰数据库。"""

    def _install(metadata: dict, weights: list[str]):
        model_dir = _make_model_dir(tmp_path, metadata, weights)
        meta_path = str(model_dir / "metadata.json")
        monkeypatch.setattr(
            _glob_mod,
            "glob",
            lambda pattern, *a, **k: [meta_path] if "metadata.json" in pattern else [],
        )
        # 注册表回落：这里刻意给 None（模拟「注册表里没有可恢复的记录」那条分支）
        monkeypatch.setattr(rs, "_load_model_registry_meta_sync", lambda _mid: None)
        return model_dir

    return _install


def _reason() -> str | None:
    drivers, reason, _meta_src = rs._compute_shap_drivers_sync(
        _DUMMY_ID, "600036.SH", "2026-09-18", "CN"
    )
    assert drivers == [], "这条分支不该产出归因"
    return reason


def test_torch_model_declaring_missing_weights_is_reported_as_unsupported_framework(
    wire_model_dir,
):
    """架构不支持要压过产物不一致 —— 否则用户会去重训一个本就不支持归因的模型。"""
    wire_model_dir(
        {"framework": "pytorch", "model_file": "model_nativetft.pth", "feature_columns": []},
        ["model.pth", "pred.pkl"],
    )
    assert _reason() == "framework_unsupported:pytorch"


def test_tree_model_with_missing_weights_still_reports_artifact_mismatch(wire_model_dir):
    """声明 lightgbm 却找不到 .lgb —— 这是真的产物损坏，必须照旧报出来。"""
    wire_model_dir(
        {"framework": "lightgbm", "model_file": "model_lgb.lgb", "feature_columns": []},
        ["model.pth"],
    )
    assert _reason() == "model_artifact_mismatch"


def test_declared_tree_filename_wins_even_when_framework_field_says_pytorch(wire_model_dir):
    """声明文件名本身就是树产物扩展名时，也要按「产物损坏」报。

    港股 0902 那批的 metadata.json 里 framework 字段被同批 GRU 覆写成 pytorch，
    但 ``model_file`` 里仍留着 ``model_lgb.lgb`` —— 只看 framework 字段会把
    「产物被覆写」误判成「架构不支持」，正好是最需要用户去修的那一类。
    """
    wire_model_dir(
        {"framework": "pytorch", "model_file": "model_lgb.lgb", "feature_columns": []},
        ["model.pth"],
    )
    assert _reason() == "model_artifact_mismatch"


def test_both_reasons_have_human_notes():
    """零项参与即失败：两条码都必须有面向人的说明，否则界面还是白板。"""
    for code in ("model_artifact_mismatch", "framework_unsupported:pytorch"):
        note = rs._shap_reason_note(code)
        assert note and note.strip(), f"{code} 没有说明文案"
