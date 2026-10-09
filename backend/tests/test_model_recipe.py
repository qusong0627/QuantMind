"""模型 → 滚动配方 推导测试（模型管理页「滚动训练」派生面）。

口径：
- 派生配方 = 复刻被推导模型自身的训练语义（类型/特征/因子源/超参/上下文），
  窗外策略用 rolling_window 默认档或调用方显式覆盖；
- 真实 config.yaml 带 split 六键段 —— 推导必须显式丢弃，配方禁止携带
  派发时注入键（六键 / rolling_meta / wfa）；
- 缺 config.yaml 的云端包：尽力从 metadata.json 推导 + warnings 明示降级；
  但缺 features / 可训练 model_type / factor_source / 目录版本时**拒绝生成**
  （宁可不产出，不产一个派发即 422 的假配方）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from backend.shared.training.model_recipe import derive_recipe_from_model
from backend.shared.training.recipe_registry import (
    RecipeError,
    validate_recipe,
)

MODEL_ID = "train_20260917064612_33659b62"


def _metadata(**overrides) -> dict:
    meta = {
        "model_id": MODEL_ID,
        "model_name": "ML7 solo · nativetft",
        "market": "CN",
        "model_type": "nativetft",
        "features": ["f_a", "f_b", "f_c"],
        "feature_count": 3,
        "factor_source": "l1_factors",
        "factor_catalog_version": "qdb-custom-l1_factors-deadbeef",
        "target_horizon_days": 5,
        "target_mode": "return",
        "label_formula": "",
        "prediction_mode": "point",
        "context": {
            "market": "CUSTOM",
            "benchmark": "SH000300",
            "commission_rate": 0.0005,
            "slippage": 0.0005,
            "deal_price": "close",
            "initial_capital": 1000000.0,
        },
    }
    meta.update(overrides)
    return meta


def _config(**overrides) -> dict:
    cfg = {
        "max_time_minutes": 240,
        "context": {
            "market": "CUSTOM",
            "benchmark": "SH000300",
            "commission_rate": 0.0005,
            "slippage": 0.0005,
            "deal_price": "close",
            "initial_capital": 1000000.0,
            "industry_as_feature": False,
        },
        "data": {
            "features": ["f_a", "f_b", "f_c"],
            "factor_source": "l1_factors",
            "factor_catalog_version": "qdb-custom-l1_factors-deadbeef",
        },
        "explain": {
            "shap_split": "valid",
            "enable_shap": True,
            "shap_sample_rows": 30000,
        },
        "factor_selection": {"n_top": 150, "dh_enabled": True, "pfs_enabled": True},
        "label": {
            "target_horizon_days": 5,
            "target_mode": "return",
            "label_formula": "",
            "training_window": "",
        },
        "model": {
            "type": "nativetft",
            "ensemble": "none",
            "num_boost_round": 1000,
            "early_stopping_rounds": 100,
            "val_ratio": None,
            "params": {},
            "xgb_params": {"max_depth": 8},
            "catboost_params": {},
            "dl_params": {
                "n_epochs": 8,
                "step_len": 20,
                "batch_size": 4000,
                "early_stopping_rounds": 5,
            },
            "prediction_mode": "point",
        },
        "preprocessing": {"winsor": True, "enabled": True},
        "output": {
            "required_artifacts": [
                "model.lgb",
                "pred.pkl",
                "metadata.json",
                "config.yaml",
                "result.json",
            ]
        },
        # 真实 config.yaml 带 split 六键段 —— 推导必须丢弃（不能进配方模板）
        "split": {
            "train_start": "2016-01-04",
            "train_end": "2024-12-31",
            "valid_start": "2025-01-06",
            "valid_end": "2025-12-31",
            "test_start": "2026-01-06",
            "test_end": "2026-09-11",
        },
    }
    cfg.update(overrides)
    return cfg


def _write_model_dir(
    base: Path, *, metadata: dict | None = None, config: dict | None | str = "auto"
) -> Path:
    d = base / MODEL_ID
    d.mkdir(parents=True)
    if metadata is not None:
        (d / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False), encoding="utf-8"
        )
    if config == "auto":
        config = _config()
    if config is not None:
        (d / "config.yaml").write_text(
            yaml.safe_dump(config, allow_unicode=True), encoding="utf-8"
        )
    return d


# ---------------------------------------------------------------------------
# 全保真：metadata + config 双源
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_derive_full_fidelity_from_metadata_and_config(tmp_path):
    d = _write_model_dir(tmp_path, metadata=_metadata())
    result = derive_recipe_from_model(d, model_id=MODEL_ID)
    recipe = result.recipe

    assert recipe.recipe_id == f"model_{MODEL_ID}"
    assert recipe.market == "CN"
    assert recipe.calendar_market == "CN"
    # 市场迁移后 metadata.market=CN，但训练当时的因子面是 CUSTOM（与基础配方同款）
    assert recipe.factor_market == "CUSTOM"
    assert recipe.factor_source == "l1_factors"
    assert recipe.target_horizon_days == 5
    assert recipe.source_model_id == MODEL_ID
    assert recipe.derived_at and recipe.derived_at.endswith("Z")

    wp = recipe.window_policy
    assert (wp.train_days, wp.valid_days, wp.test_days) == (756, 126, 63)
    assert wp.mode == "sliding"

    p = recipe.payload
    assert p["model_type"] == "nativetft"
    assert p["features"] == ["f_a", "f_b", "f_c"]
    assert p["context"]["market"] == "CUSTOM"
    assert p["factor_source"] == "l1_factors"
    assert p["factor_catalog_version"] == "qdb-custom-l1_factors-deadbeef"
    assert p["dl_params"]["n_epochs"] == 8
    assert p["num_boost_round"] == 1000
    assert p["early_stopping_rounds"] == 100
    assert p["explain"]["shap_split"] == "valid"
    assert p["preprocessing"] == {"winsor": True, "enabled": True}
    assert p["factor_selection"]["n_top"] == 150
    assert p["auto_feature_filter"] == "false"
    assert p["max_time_minutes"] == 240
    assert p["target_horizon_days"] == 5
    assert p["deploy_to_production"] is False
    # 全信息模型零降级告警（有则说明悄悄丢了什么）
    assert result.warnings == []


@pytest.mark.unit
def test_derive_drops_split_section_from_config(tmp_path):
    d = _write_model_dir(tmp_path, metadata=_metadata())
    result = derive_recipe_from_model(d, model_id=MODEL_ID)
    for key in (
        "train_start",
        "train_end",
        "valid_start",
        "valid_end",
        "test_start",
        "test_end",
        "rolling_meta",
        "wfa",
    ):
        assert key not in result.recipe.payload, key


@pytest.mark.unit
def test_derive_recipe_dict_serializable_and_revalidates(tmp_path):
    d = _write_model_dir(tmp_path, metadata=_metadata())
    result = derive_recipe_from_model(d, model_id=MODEL_ID)
    # 落盘前必须可 JSON 序列化（不能靠 default=str 掩盖不可序列化对象）
    json.dumps(result.recipe_dict, ensure_ascii=False)
    validate_recipe(result.recipe_dict)
    assert result.recipe_dict["source_model_id"] == MODEL_ID


# ---------------------------------------------------------------------------
# 降级：缺 config.yaml 的云端包
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_derive_metadata_only_degrades_with_warnings(tmp_path):
    d = _write_model_dir(tmp_path, metadata=_metadata(), config=None)
    result = derive_recipe_from_model(d, model_id=MODEL_ID)
    p = result.recipe.payload
    assert p["model_type"] == "nativetft"
    assert p["features"] == ["f_a", "f_b", "f_c"]
    assert "dl_params" not in p
    # 无 factor_selection 时显式关闭自动筛选注入，避免默认 top-80 悄悄砍特征
    assert p["auto_feature_filter"] == "false"
    assert any("config.yaml" in w for w in result.warnings)
    assert result.recipe.factor_market == "CUSTOM"  # 来自 metadata.context.market


@pytest.mark.unit
@pytest.mark.parametrize(
    ("drop", "needle"),
    [
        ("features", "特征"),
        ("model_type", "模型类型"),
        ("factor_source", "因子源"),
        ("factor_catalog_version", "目录版本"),
    ],
)
def test_derive_refuses_when_required_info_missing(tmp_path, drop, needle):
    d = _write_model_dir(tmp_path, metadata=_metadata(**{drop: None}), config=None)
    with pytest.raises(RecipeError) as ei:
        derive_recipe_from_model(d, model_id=MODEL_ID)
    assert needle in str(ei.value)


@pytest.mark.unit
def test_derive_rejects_untrainable_model_type(tmp_path):
    """云端合成的 metadata json model_type=algorithm：不可训练，拒绝派生。"""
    d = _write_model_dir(
        tmp_path, metadata=_metadata(model_type="algorithm"), config=None
    )
    with pytest.raises(RecipeError) as ei:
        derive_recipe_from_model(d, model_id=MODEL_ID)
    assert "algorithm" in str(ei.value)


@pytest.mark.unit
def test_derive_requires_artifacts(tmp_path):
    d = tmp_path / "empty_model"
    d.mkdir()
    with pytest.raises(RecipeError) as ei:
        derive_recipe_from_model(d, model_id=MODEL_ID)
    assert "metadata.json" in str(ei.value) and "config.yaml" in str(ei.value)


@pytest.mark.unit
def test_derive_requires_existing_directory(tmp_path):
    with pytest.raises(RecipeError):
        derive_recipe_from_model(tmp_path / "nope", model_id=MODEL_ID)


# ---------------------------------------------------------------------------
# 标识与窗口策略
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["../evil", "a/b", "with space", "", "x" * 200])
def test_derive_rejects_illegal_model_id(tmp_path, bad):
    d = _write_model_dir(tmp_path, metadata=_metadata())
    with pytest.raises(RecipeError):
        derive_recipe_from_model(d, model_id=bad)


@pytest.mark.unit
def test_derive_window_policy_override_and_validation(tmp_path):
    d = _write_model_dir(tmp_path, metadata=_metadata())
    result = derive_recipe_from_model(
        d, model_id=MODEL_ID, window_policy={"train_days": 500}
    )
    wp = result.recipe.window_policy
    assert wp.train_days == 500
    assert wp.valid_days == 126  # 其余走默认档
    with pytest.raises(RecipeError):
        derive_recipe_from_model(d, model_id=MODEL_ID, window_policy={"train_days": 0})
    with pytest.raises(RecipeError):
        derive_recipe_from_model(d, model_id=MODEL_ID, window_policy={"mode": "weird"})


@pytest.mark.unit
def test_derive_infers_market_from_model_id_prefix(tmp_path):
    """metadata/config 都缺市场时，从 mdl_{market}_ 前缀兜底（云端导入模型 id 惯例）。"""
    mid = "mdl_us_hub_momentum_abc123"
    meta = _metadata(model_id=mid, market=None, context={})
    d = _write_model_dir(tmp_path, metadata=meta, config=None)
    result = derive_recipe_from_model(d, model_id=mid)
    assert result.recipe.market == "US"
    assert result.recipe.factor_market == "US"


# ---------------------------------------------------------------------------
# 市场别名规范化与符号链接拒绝（评审回归）
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw_market", "expected"),
    [
        ("hong_kong", "HK"),
        ("a_share", "CN"),
        ("美股", "US"),
        ("XHKG", "HK"),
        ("sse", "CN"),
    ],
)
def test_derive_canonicalizes_market_aliases(tmp_path, raw_market, expected):
    """别名表（含交易所/日历代号）是唯一口径：'HONG_KONG' 若只 upper() 放行，
    到因子读取处会静默坍缩成 CN——「HK 配方训练了 A 股数据」。"""
    d = _write_model_dir(tmp_path, metadata=_metadata(market=raw_market))
    result = derive_recipe_from_model(d, model_id=MODEL_ID)
    assert result.recipe.market == expected
    # 可识别的别名不产生降级告警
    assert not any("别名表" in w for w in result.warnings)


@pytest.mark.unit
def test_derive_unknown_market_falls_back_with_warning(tmp_path):
    """不认识的市场标识按 benchmark 推断 + 明示告警（不能静默丢）。"""
    d = _write_model_dir(tmp_path, metadata=_metadata(market="MARS"))
    result = derive_recipe_from_model(d, model_id=MODEL_ID)
    assert result.recipe.market == "CN"  # benchmark=SH000300 → CN
    assert any("不在平台别名表内" in w and "MARS" in w for w in result.warnings)


@pytest.mark.unit
def test_derive_refuses_unknown_factor_market(tmp_path):
    """非规范因子面标识（如 'US_STOCK'）宁可不产出——坍缩成 CN 的假配方更糟。"""
    cfg = _config()
    cfg["context"]["market"] = "US_STOCK"
    d = _write_model_dir(tmp_path, metadata=_metadata(), config=cfg)
    with pytest.raises(RecipeError) as ei:
        derive_recipe_from_model(d, model_id=MODEL_ID)
    assert "因子面" in str(ei.value) and "US_STOCK" in str(ei.value)


@pytest.mark.unit
@pytest.mark.parametrize("linked", ["metadata.json", "config.yaml"])
def test_derive_refuses_symlinked_model_files(tmp_path, linked):
    """模型包文件不得是符号链接：目录级越界守卫在 router 层，文件级链接逃逸在此拦。"""
    d = _write_model_dir(tmp_path, metadata=_metadata())
    real = tmp_path / f"outside_{linked}"
    (d / linked).rename(real)
    (d / linked).symlink_to(real)
    with pytest.raises(RecipeError) as ei:
        derive_recipe_from_model(d, model_id=MODEL_ID)
    assert "符号链接" in str(ei.value)
