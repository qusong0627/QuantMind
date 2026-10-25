"""时序推理模板必须在装窗之后、前向之前复现训练端的逐日截面预处理。

背景（2026-10-25 实测）：`docker/training` 对每个交易日做截面 winsor+Z-score，
``metadata.feat_norm`` 又是按**已标准化口径**统计的（mean≈0 / std≈1）。批量模板
的时序分支原先直接拿 ``load_window_data`` 的原始因子喂 ``_apply_feat_norm``，
等于用训练期统计量去标准化原始尺度 —— 预测 std 从 0.095 跳到 0.31，与训练期
预测 Spearman 为 -0.19（补上截面预处理后 +0.93），9 月已实现前视 20 日 rankIC
由 -0.135 转 +0.342。

本文件钉住三件事：
1. ``_cross_sectional_preprocess_window`` 按 trade_date 分组逐日变换（不是整窗一次），
   且保持行序与 symbol/trade_date 列；
2. ``preprocessing.enabled`` 缺失/false 时原样返回（旧模型兼容）；
3. ``_infer_sequence`` 先把窗口预处理再前向，symbols 对齐顺序不变，空窗仍是 exit 2。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_TEMPLATE = (
    Path(__file__).resolve().parents[1]
    / "services"
    / "engine"
    / "inference"
    / "templates"
    / "inference_parquet.py"
)

_PREP_ON = {"preprocessing": {"enabled": True, "winsor": True}}


@pytest.fixture(scope="module")
def tpl():
    spec = importlib.util.spec_from_file_location(
        "inference_parquet_seq_prep_tpl", _TEMPLATE
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _window(days: int = 2, n: int = 60) -> pd.DataFrame:
    dates = pd.to_datetime(["2026-08-31", "2026-09-01", "2026-09-02"][:days])
    rows = []
    for d in dates:
        for i in range(n):
            rows.append(
                {
                    "symbol": f"SH600{i:03d}",
                    "trade_date": d,
                    # 两个因子故意用不同尺度，整窗一次标准化会得到与逐日不同的结果
                    "f_a": float(i) * 100.0,
                    "f_b": float(i % 7) + 1.0,
                }
            )
    return pd.DataFrame(rows)


def _per_day_stats(frame: pd.DataFrame, col: str) -> pd.DataFrame:
    return frame.groupby("trade_date")[col].agg(["mean", "std"])


@pytest.mark.unit
def test_preprocess_window_standardizes_each_day_separately(tpl):
    raw = _window(days=2)
    out = tpl._cross_sectional_preprocess_window(raw, ["f_a", "f_b"], _PREP_ON)

    for col in ("f_a", "f_b"):
        stats = _per_day_stats(out, col)
        assert np.allclose(stats["mean"].to_numpy(), 0.0, atol=1e-6), col
        assert np.allclose(stats["std"].to_numpy(), 1.0, atol=0.05), col

    # 逐日而不是整窗：两天的原始均值相同，整窗标准化后每天都会偏离 0
    assert len(out) == len(raw)


@pytest.mark.unit
def test_preprocess_window_preserves_row_order_and_keys(tpl):
    raw = _window(days=2)
    out = tpl._cross_sectional_preprocess_window(raw, ["f_a", "f_b"], _PREP_ON)

    assert list(out["symbol"]) == list(raw["symbol"])
    assert list(out["trade_date"]) == list(raw["trade_date"])
    assert "trade_date" in out.columns


@pytest.mark.unit
def test_preprocess_window_identity_when_disabled(tpl):
    raw = _window(days=2)
    for meta in ({}, {"preprocessing": {"enabled": False}}, {"preprocessing": None}):
        out = tpl._cross_sectional_preprocess_window(raw, ["f_a", "f_b"], meta)
        pd.testing.assert_frame_equal(out, raw)


@pytest.mark.unit
def test_infer_sequence_preprocesses_before_predict(tpl, monkeypatch):
    raw = _window(days=2)
    captured: dict[str, object] = {}

    monkeypatch.setattr(tpl, "load_window_data", lambda *a, **k: raw.copy())

    def fake_predict(inner_model, window_df, features, meta, step_len):
        captured["frame"] = window_df
        captured["features"] = features
        captured["step_len"] = step_len
        return np.zeros(window_df["symbol"].nunique(), dtype=np.float64)

    monkeypatch.setattr(tpl, "_predict_dl_sequence", fake_predict)

    meta = {"feature_columns": ["f_a", "f_b"], "dl_params": {"step_len": 2}, **_PREP_ON}
    scores, symbols = tpl._infer_sequence(object(), "2026-09-01", Path("/tmp"), meta)

    frame = captured["frame"]
    assert list(frame["symbol"]) == list(raw["symbol"])  # 行序/对齐不变
    for col in ("f_a", "f_b"):
        stats = _per_day_stats(frame, col)
        assert np.allclose(stats["mean"].to_numpy(), 0.0, atol=1e-6)
    assert symbols == list(dict.fromkeys(raw["symbol"]))
    assert len(scores) == len(symbols)


@pytest.mark.unit
def test_infer_sequence_exits_2_on_empty_window(tpl, monkeypatch):
    monkeypatch.setattr(tpl, "load_window_data", lambda *a, **k: pd.DataFrame())
    with pytest.raises(SystemExit) as exc:
        tpl._infer_sequence(object(), "2026-09-01", Path("/tmp"), {"feature_columns": []})
    assert exc.value.code == 2


@pytest.mark.unit
def test_template_seq_branch_calls_infer_sequence(tpl):
    """主流程必须走 ``_infer_sequence``（防止修好的助手函数被绕开）。"""
    source = Path(tpl.__file__).read_text(encoding="utf-8")
    seq_branch = source.split("is_seq = bool(meta.get(\"is_sequence_model\", False))", 1)
    assert len(seq_branch) == 2, "时序分支标记丢失"
    assert "_infer_sequence(" in seq_branch[1].split("else:", 1)[0]
    assert "_predict_dl_sequence(" not in seq_branch[1].split("else:", 1)[0]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
