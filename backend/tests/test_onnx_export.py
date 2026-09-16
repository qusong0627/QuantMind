"""T-P6-08 ONNX 导出链测试：真实注册表模型逐框架导出 + 逐值等价校验。

覆盖：
1. I：注册表每框架抽 1 个真实模型 → 导出 ONNX → 原模型 vs onnxruntime 逐值 ≤1e-4；
2. U：不支持框架（pytorch）如实 ok=False + 原因明确（不静默）；
3. U：缺 feature_columns / 缺模型文件 → ok=False 原因明确；
4. G：onnxmltools/skl2onnx 只允许 onnx_exporter 引用（唯一转换面）。
"""

from __future__ import annotations

import glob
import json
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]
_USERS_GLOB = "/app/models/users/*/*/*/metadata.json"
_PRODUCTION_GLOB = "/app/models/production/*/metadata.json"

_WANTED = {"lightgbm", "xgboost", "sklearn", "catboost"}


def _pick_registry_samples() -> dict[str, Path]:
    picked: dict[str, Path] = {}
    for pattern in (_USERS_GLOB, _PRODUCTION_GLOB):
        for meta_path in glob.glob(pattern):
            try:
                meta = json.loads(Path(meta_path).read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            fw = str(meta.get("framework") or "").strip().lower()
            if fw in _WANTED and fw not in picked:
                model_file = meta.get("model_file") or ""
                if model_file and (Path(meta_path).parent / model_file).is_file():
                    picked[fw] = Path(meta_path).parent
    return picked


@pytest.mark.integration
@pytest.mark.parametrize("framework", sorted(_WANTED))
def test_export_real_registry_model_equivalent(framework, tmp_path):
    from backend.services.engine.inference.onnx_exporter import export_model_to_onnx

    samples = _pick_registry_samples()
    if framework not in samples:
        pytest.skip(f"注册表无 {framework} 样本")
    report = export_model_to_onnx(
        samples[framework], output_path=tmp_path / f"{framework}.onnx"
    )
    assert report["ok"] is True, report
    assert report["framework"] == framework
    assert report["n_features"] > 0
    assert report["max_abs_diff"] <= 1e-4, report
    assert Path(report["onnx_path"]).is_file()


@pytest.mark.unit
def test_unsupported_framework_reports_reason(tmp_path):
    from backend.services.engine.inference.onnx_exporter import export_model_to_onnx

    d = tmp_path / "m"
    d.mkdir()
    (d / "metadata.json").write_text(
        json.dumps({"framework": "pytorch", "model_file": "model.pth",
                    "feature_columns": ["a", "b"]}),
        encoding="utf-8",
    )
    (d / "model.pth").write_bytes(b"x")
    report = export_model_to_onnx(d)
    assert report["ok"] is False
    assert "pytorch" in report["reason"] and "torch.onnx.export" in report["reason"]


@pytest.mark.unit
def test_missing_pieces_report_reason(tmp_path):
    from backend.services.engine.inference.onnx_exporter import export_model_to_onnx

    d = tmp_path / "m"
    d.mkdir()
    # 缺 metadata
    report = export_model_to_onnx(d)
    assert report["ok"] is False and "metadata.json" in report["reason"]
    # 有 metadata 缺模型文件
    (d / "metadata.json").write_text(
        json.dumps({"framework": "lightgbm", "model_file": "nope.lgb",
                    "feature_columns": ["a"]}),
        encoding="utf-8",
    )
    report = export_model_to_onnx(d)
    assert report["ok"] is False and "模型文件不存在" in report["reason"]
    # 空 feature_columns
    (d / "m.lgb").write_bytes(b"x")
    (d / "metadata.json").write_text(
        json.dumps({"framework": "lightgbm", "model_file": "m.lgb", "feature_columns": []}),
        encoding="utf-8",
    )
    report = export_model_to_onnx(d)
    assert report["ok"] is False and "feature_columns" in report["reason"]


@pytest.mark.unit
def test_exporter_is_single_conversion_face():
    offenders: list[str] = []
    for path in (_BACKEND / "services").rglob("*.py"):
        if path.name == "onnx_exporter.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "import onnxmltools" in text or "import skl2onnx" in text or "from skl2onnx" in text:
            offenders.append(str(path.relative_to(_BACKEND)))
    assert offenders == [], f"ONNX 转换只允许在 onnx_exporter.py: {offenders}"
