"""ONNX 导出链（T-P6-08）：模型目录 → ONNX，含**逐值等价校验**（导出即证）。

支持框架（按 ``metadata.json`` 的 framework/model_file 分派）：
- ``lightgbm``  .lgb/.txt  → onnxmltools.convert_lightgbm
- ``xgboost``   .xgb/.json → onnxmltools.convert_xgboost
- ``sklearn``   .pkl       → skl2onnx.convert_sklearn
- ``catboost``  .cbm       → CatBoost 原生 save_model(format="onnx")
- ``pytorch``   .pth       → **不在本条链路**（需要模型类源码，由训练侧 torch.onnx.export
  自行导出；本模块如实返回 ok=False 并说明，绝不静默降级）

等价校验：按 ``feature_columns`` + ``fill_values``（缺失回退 0）生成固定种子特征矩阵，
原模型与 onnxruntime 会话逐值比对，``max_abs_diff ≤ 1e-4`` 才判 ok——**导出不可信即失败**。
目标 opset 固定 21（onnx 1.17 / onnxruntime 1.20 矩阵实测一致，见 2026-09-17 依赖探雷）。
"""

from __future__ import annotations

import logging
import pickle
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

TARGET_OPSET = 21
# onnxmltools（lgb/xgb 转换器）支持上限 15（实测 2026-09-17：>15 直接 RuntimeError）；
# skl2onnx 支持 21；catboost 原生导出自带 opset。按框架分档，避免一刀切踩上限。
_ONNXMLTOOLS_OPSET = 15
INPUT_NAME = "float_input"
TOL_ABS = 1e-4
DEFAULT_SAMPLE_ROWS = 64
_DEFAULT_FEATURE_FALLBACK = 0.0


class OnnxExportError(RuntimeError):
    """导出链不可继续（缺文件/框架不支持/校验失败给出明确原因）。"""


def _load_metadata(model_dir: Path) -> dict[str, Any]:
    import json

    meta_path = model_dir / "metadata.json"
    if not meta_path.is_file():
        raise OnnxExportError(f"metadata.json 不存在: {model_dir}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if not isinstance(meta, dict):
        raise OnnxExportError("metadata.json 结构非法")
    return meta


def _feature_matrix(meta: dict[str, Any], rows: int, seed: int) -> np.ndarray:
    """固定种子合成特征矩阵（feature_columns × rows；fill_values 为基准 + 5% 抖动）。

    非零填充值需要非退化输入才能暴露导出差异（全零矩阵会掩盖切片/转置错误），
    抖动幅度按 |fill| 缩放并设下限，保证列间量纲差异被覆盖。
    """
    cols = list(meta.get("feature_columns") or [])
    if not cols:
        raise OnnxExportError("metadata 缺 feature_columns")
    fills = meta.get("fill_values") or {}
    rng = np.random.default_rng(seed)
    out = np.empty((rows, len(cols)), dtype=np.float32)
    for i, col in enumerate(cols):
        base = fills.get(col, _DEFAULT_FEATURE_FALLBACK)
        try:
            base = float(base)
        except (TypeError, ValueError):
            base = 0.0
        if not np.isfinite(base):
            base = 0.0
        scale = max(abs(base) * 0.05, 0.05)
        out[:, i] = (base + rng.normal(0.0, scale, rows)).astype(np.float32)
    return out


def _resolve_model_path(model_dir: Path, meta: dict[str, Any]) -> Path:
    model_file = str(meta.get("model_file") or "")
    if not model_file:
        raise OnnxExportError("metadata 缺 model_file")
    path = model_dir / model_file
    if not path.is_file():
        raise OnnxExportError(f"模型文件不存在: {path}")
    return path


# ── 原模型加载与预测 ─────────────────────────────────────────────────


def _load_original(framework: str, path: Path):
    if framework == "lightgbm":
        import lightgbm as lgb

        return lgb.Booster(model_file=str(path))
    if framework == "xgboost":
        import xgboost as xgb

        booster = xgb.Booster()
        booster.load_model(str(path))
        return booster
    if framework == "sklearn":
        with open(path, "rb") as f:
            return pickle.load(f)
    if framework == "catboost":
        from catboost import CatBoostClassifier, CatBoostRegressor

        try:
            model = CatBoostRegressor()
            model.load_model(str(path))
            return model
        except Exception:  # noqa: BLE001 - 分类器回退
            model = CatBoostClassifier()
            model.load_model(str(path))
            return model
    raise OnnxExportError(f"框架不在导出链: {framework}（pytorch 走训练侧 torch.onnx.export）")


def _predict_original(framework: str, model: Any, x: np.ndarray) -> np.ndarray:
    if framework == "xgboost":
        import xgboost as xgb

        names = getattr(model, "feature_names", None)
        if names:
            import pandas as pd

            return np.asarray(model.predict(xgb.DMatrix(pd.DataFrame(x, columns=names)))).reshape(-1)
        return np.asarray(model.predict(xgb.DMatrix(x))).reshape(-1)
    return np.asarray(model.predict(x)).reshape(-1)


def _model_feature_names(framework: str, model: Any) -> list[str] | None:
    try:
        if framework == "lightgbm":
            names = list(model.feature_name() or [])
        elif framework == "xgboost":
            names = list(getattr(model, "feature_names", None) or [])
        else:
            return None
    except Exception:  # noqa: BLE001
        return None
    return names or None


def _align_columns(
    x: np.ndarray, metadata_cols: list[str], framework: str, model: Any
) -> np.ndarray:
    """按模型自带特征名序重排列（不一致时）；缺名/同名序原样返回。"""
    names = _model_feature_names(framework, model)
    if not names or list(names) == list(metadata_cols):
        return x
    if set(names) != set(metadata_cols):
        # 名称集合不一致：模型训练列与 metadata 脱节——如实失败，不猜测
        raise OnnxExportError(
            f"模型特征名与 metadata.feature_columns 集合不一致（{len(names)} vs {len(metadata_cols)}）"
        )
    order = [metadata_cols.index(n) for n in names]
    return x[:, order]


# ── 导出 ─────────────────────────────────────────────────────────────


def _export(framework: str, model: Any, n_features: int, out_path: Path) -> None:
    if framework in ("lightgbm", "xgboost"):
        from onnxmltools.convert.common.data_types import FloatTensorType

        converter = (
            __import__("onnxmltools").convert_lightgbm
            if framework == "lightgbm"
            else __import__("onnxmltools").convert_xgboost
        )
        names = None
        if framework == "xgboost":
            # onnxmltools 1.16 要求特征名 f%d 模式；转换按位置进行，摘名无信息损失
            # （探雷 2026-09-17：真实名 → RuntimeError；xgb 3.x base_score="[v]" 由 1.16 处理）
            names = model.feature_names
            model.feature_names = None
        try:
            onnx_model = converter(
                model,
                initial_types=[(INPUT_NAME, FloatTensorType([None, n_features]))],
                target_opset=_ONNXMLTOOLS_OPSET,
            )
        finally:
            if names is not None:
                model.feature_names = names
        out_path.write_bytes(onnx_model.SerializeToString())
        return
    if framework == "sklearn":
        from skl2onnx import convert_sklearn
        from skl2onnx.common.data_types import FloatTensorType

        onnx_model = convert_sklearn(
            model,
            initial_types=[(INPUT_NAME, FloatTensorType([None, n_features]))],
            target_opset=TARGET_OPSET,
        )
        out_path.write_bytes(onnx_model.SerializeToString())
        return
    if framework == "catboost":
        model.save_model(str(out_path), format="onnx")  # 原生导出
        return
    raise OnnxExportError(f"框架不在导出链: {framework}")


def _predict_onnx(onnx_path: Path, x: np.ndarray) -> np.ndarray:
    import onnxruntime as ort

    sess = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
    input_name = sess.get_inputs()[0].name
    out = sess.run(None, {input_name: x})
    return np.asarray(out[0]).reshape(-1)


# ── 公开入口 ─────────────────────────────────────────────────────────


def export_model_to_onnx(
    model_dir: str | Path,
    *,
    output_path: str | Path | None = None,
    verify: bool = True,
    sample_rows: int = DEFAULT_SAMPLE_ROWS,
    seed: int = 7,
) -> dict[str, Any]:
    """模型目录 → ONNX 文件 + 等价校验报告。

    返回 {ok, reason?, framework, onnx_path, n_features, max_abs_diff?}；失败绝不抛给调用方
    之外的沉默路径——ok=False 携带原因（写入面/巡检可留痕）。
    """
    model_dir = Path(model_dir)
    result: dict[str, Any] = {"ok": False, "model_dir": str(model_dir)}
    try:
        meta = _load_metadata(model_dir)
        framework = str(meta.get("framework") or "").strip().lower()
        result["framework"] = framework
        if framework not in {"lightgbm", "xgboost", "sklearn", "catboost"}:
            raise OnnxExportError(
                f"框架不在导出链: {framework or '未知'}"
                "（pytorch 需训练侧 torch.onnx.export 携带模型类）"
            )
        model_path = _resolve_model_path(model_dir, meta)
        cols = list(meta.get("feature_columns") or [])
        n_features = len(cols)
        if n_features <= 0:
            raise OnnxExportError("feature_columns 为空")
        out_path = Path(output_path) if output_path else model_dir / "model.onnx"

        model = _load_original(framework, model_path)
        _export(framework, model, n_features, out_path)
        result["onnx_path"] = str(out_path)
        result["n_features"] = n_features

        if verify:
            x = _feature_matrix(meta, sample_rows, seed)
            # 列顺序对齐：模型自带特征名序优先（与 metadata.feature_columns 序不一致时按名重排）
            x = _align_columns(x, cols, framework, model)
            original = _predict_original(framework, model, x)
            onnx_out = _predict_onnx(out_path, x)
            if original.shape != onnx_out.shape:
                raise OnnxExportError(
                    f"输出形状不一致: original{original.shape} vs onnx{onnx_out.shape}"
                )
            max_diff = float(np.max(np.abs(original - onnx_out))) if len(original) else 0.0
            result["max_abs_diff"] = max_diff
            if not np.isfinite(max_diff) or max_diff > TOL_ABS:
                raise OnnxExportError(f"等价校验失败: max_abs_diff={max_diff:.3e} > {TOL_ABS}")
        result["ok"] = True
        return result
    except OnnxExportError as exc:
        result["reason"] = str(exc)
        return result
    except Exception as exc:  # noqa: BLE001 - 未知异常如实回报（调用方决定是否告警）
        logger.error("ONNX 导出异常 %s: %s", model_dir, exc, exc_info=True)
        result["reason"] = f"{type(exc).__name__}: {exc}"
        return result
