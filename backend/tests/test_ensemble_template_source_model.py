"""融合模板成员权重定位契约 —— 同后缀恢复 + 预测产物排除。

背景（2026-10-09 融合 E2E 实锤）：成员 metadata 声明 per-algorithm 名
（model_nativetft.pth），磁盘上落的是通用名 model.pth。融合模板 load_source_model
原来按扩展名列表盲搜、*.pkl 排在最前 —— 先命中 61MB 的 pred.pkl（预测结果
DataFrame），既分类不出 __dl_member__、也会崩在 predict()，DL 成员在融合里被
静默跳过（E2E 只出 LGB 成员的 3219 条，成员侧零可见日志）。修复 = 对齐成员模板
inference_parquet._resolve_model_path 口径：声明名对不上先试同后缀通用名
（model.<ext> 与 per-algorithm 名指同一文件）；盲搜按权重白名单，
pred*/result 等训练产物永不入选。本测试锁定该契约。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pandas as pd
import pytest

_TEMPLATE = (
    Path(__file__).resolve().parents[1]
    / "services"
    / "engine"
    / "inference"
    / "templates"
    / "inference_ensemble_src.py"
)


@pytest.fixture(scope="module")
def tpl():
    spec = importlib.util.spec_from_file_location(
        "inference_ensemble_src_source_model_under_test", _TEMPLATE
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _member_dir(
    tmp_path: Path, *, model_type: str, model_file: str, files: dict[str, str | None]
) -> Path:
    """搭一个成员模型目录。files 值 None = 写真实 DataFrame pickle（模拟 pred.pkl）。"""
    d = tmp_path / "member"
    d.mkdir()
    (d / "metadata.json").write_text(
        json.dumps({"model_type": model_type, "model_file": model_file}),
        encoding="utf-8",
    )
    for name, payload in files.items():
        if payload is None:
            pd.DataFrame({"sym": ["SH600036"], "score": [0.1]}).to_pickle(d / name)
        else:
            (d / name).write_bytes(payload.encode())
    return d


class TestResolveMemberModelPath:
    def test_declared_missing_recovers_same_suffix_generic_name(self, tpl, tmp_path):
        d = _member_dir(
            tmp_path,
            model_type="nativetft",
            model_file="model_nativetft.pth",
            files={"model.pth": "w", "pred.pkl": None},
        )
        meta = {"model_file": "model_nativetft.pth", "model_type": "nativetft"}
        assert tpl._resolve_member_model_path(d, meta) == d / "model.pth"

    def test_declared_present_uses_it_directly(self, tpl, tmp_path):
        d = _member_dir(
            tmp_path,
            model_type="lightgbm",
            model_file="model.lgb",
            files={"model.lgb": "w", "pred.pkl": None},
        )
        meta = {"model_file": "model.lgb", "model_type": "lightgbm"}
        assert tpl._resolve_member_model_path(d, meta) == d / "model.lgb"

    def test_blind_search_never_picks_pred_artifacts(self, tpl, tmp_path):
        # 声明名缺失且无同后缀通用名：只剩 pred.pkl 时必须判「没有权重」
        d = _member_dir(
            tmp_path,
            model_type="nativetft",
            model_file="model_nativetft.pth",
            files={"pred.pkl": None},
        )
        assert tpl._resolve_member_model_path(d, {"model_file": "model_nativetft.pth"}) is None

    def test_blind_search_skips_pred_and_finds_suffixed_weight(self, tpl, tmp_path):
        d = _member_dir(
            tmp_path,
            model_type="nativetft",
            model_file="",
            files={"pred.pkl": None, "gru_model.pth": "w"},
        )
        assert tpl._resolve_member_model_path(d, {}) == d / "gru_model.pth"

    def test_blind_search_prefers_generic_model_name(self, tpl, tmp_path):
        # model.* 优先于带算法后缀的拆分产物（即便 .pth 扩展名序在前）
        d = _member_dir(
            tmp_path,
            model_type="mlp",
            model_file="",
            files={"gru_model.pth": "w", "model.pkl": "w"},
        )
        assert tpl._resolve_member_model_path(d, {}) == d / "model.pkl"


class TestLoadSourceModelDLClassification:
    def test_nativetft_member_classified_as_dl_marker(self, tpl, tmp_path):
        # E2E 实锤场景：声明 model_nativetft.pth（不存在）+ pred.pkl + model.pth
        # （真权重）→ 必须走 __dl_member__ 委派，绝不加载 pred.pkl
        d = _member_dir(
            tmp_path,
            model_type="nativetft",
            model_file="model_nativetft.pth",
            files={"model.pth": "w", "pred.pkl": None},
        )
        model, meta = tpl.load_source_model(d)
        assert isinstance(model, dict) and model.get("__dl_member__") is True
        assert meta.get("model_type") == "nativetft"

    def test_missing_weight_raises_file_not_found(self, tpl, tmp_path):
        d = _member_dir(
            tmp_path,
            model_type="nativetft",
            model_file="model_nativetft.pth",
            files={"pred.pkl": None},
        )
        with pytest.raises(FileNotFoundError):
            tpl.load_source_model(d)
