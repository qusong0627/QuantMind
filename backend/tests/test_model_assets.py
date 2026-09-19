"""模型产物资产检查的回归测试。

这组用例锁的是一个已经发生过的**误报**：权重文件名按固定清单（`model.lgb`）
匹配，而真实训练产物叫 `model_lgb.lgb`，导致全部 HK/US 模型被判「缺权重」。
误报比不报更糟——治理面板会把整片健康模型涂红，用户学会忽略这一列。
"""

from __future__ import annotations

from pathlib import Path

from backend.shared.model_assets import model_asset_gaps


def _make_model_dir(root: Path, *, weight: str | None = "model_lgb.lgb", files: tuple[str, ...] = ()) -> Path:
    """铺一个最小模型目录：权重 + inference.py + pred.parquet（可用 files 覆盖）。"""
    d = root / "mdl_demo"
    d.mkdir(parents=True)
    (d / "inference.py").write_text("")
    (d / "pred.parquet").write_text("")
    if weight:
        (d / weight).write_text("")
    for name in files:
        (d / name).write_text("")
    return d


def test_healthy_model_has_no_gaps(tmp_path: Path) -> None:
    d = _make_model_dir(tmp_path)

    assert model_asset_gaps(str(d)) == []


def test_weight_name_variants_all_count_as_weights(tmp_path: Path) -> None:
    """真实产物命名不止一种：`model.lgb` / `model_lgb.lgb` / `model_nativetft.pth`。"""
    for i, weight in enumerate(("model.lgb", "model_lgb.lgb", "model_nativetft.pth", "pytorch_model.bin")):
        # Act
        d = _make_model_dir(tmp_path / f"case{i}", weight=weight)

        # Assert
        assert model_asset_gaps(str(d)) == [], f"{weight} 应被认作权重产物"


def test_prediction_dump_is_not_a_weight(tmp_path: Path) -> None:
    """`pred.pkl` 后缀与权重集合重合，但它是预测转储不是权重，必须按文件名排除。"""
    d = _make_model_dir(tmp_path, weight=None, files=("pred.pkl",))

    assert model_asset_gaps(str(d)) == ["模型权重"]


def test_missing_directory_is_reported(tmp_path: Path) -> None:
    assert model_asset_gaps(str(tmp_path / "gone")) == ["模型目录不存在"]


def test_empty_path_is_reported() -> None:
    assert model_asset_gaps("") == ["注册表无 storage_path"]
    assert model_asset_gaps(None) == ["注册表无 storage_path"]


def test_missing_pred_parquet_is_reported(tmp_path: Path) -> None:
    """实测两个 nativetft 模型：权重在、`pred.parquet` 没写出来，读分路径必然空手。"""
    d = _make_model_dir(tmp_path)
    (d / "pred.parquet").unlink()

    assert model_asset_gaps(str(d)) == ["pred.parquet"]


def test_missing_inference_script_is_reported(tmp_path: Path) -> None:
    d = _make_model_dir(tmp_path)
    (d / "inference.py").unlink()

    assert model_asset_gaps(str(d)) == ["inference.py"]
