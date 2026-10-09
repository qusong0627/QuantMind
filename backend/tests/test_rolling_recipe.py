"""配方注册表测试（P1）：装载校验 / recipe_hash 稳定性 / payload 合成注入。

口径：配方是随代码进 git 的资产 —— 坏值立刻炸（RecipeError），
派发时注入的键（六键 split / rolling_meta / wfa）出现在模板里也炸。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from backend.shared.training.recipe_registry import (
    RECIPE_DIR,
    RecipeError,
    build_training_payload,
    list_recipes,
    load_recipe,
    recipe_hash,
    validate_recipe,
)
from backend.shared.training.rolling_window import RollingWindow

BASE_RECIPE_ID = "cn_nativetft_base"


@pytest.fixture(scope="module")
def base_payload_file() -> dict:
    return json.loads(
        (RECIPE_DIR / f"{BASE_RECIPE_ID}.json").read_text(encoding="utf-8")
    )


@pytest.mark.unit
def test_load_base_recipe_shape():
    recipe = load_recipe(BASE_RECIPE_ID)
    assert recipe.market == "CN"
    assert recipe.calendar_market == "CN"
    assert recipe.factor_market == "CUSTOM"
    assert recipe.factor_source == "l1_factors"
    assert recipe.target_horizon_days == 5
    assert recipe.window_policy.train_days == 756
    assert recipe.window_policy.valid_days == 126
    assert recipe.window_policy.test_days == 63
    assert recipe.window_policy.mode == "sliding"
    assert recipe.window_policy.purge_days is None
    assert recipe.payload["model_type"] == "nativetft"
    assert len(recipe.payload["features"]) == 273
    assert recipe.payload["context"]["market"] == "CUSTOM"
    assert recipe.payload["max_time_minutes"] == 240
    assert recipe.payload["deploy_to_production"] is False


@pytest.mark.unit
def test_load_recipe_unknown_id_raises():
    with pytest.raises(RecipeError):
        load_recipe("no_such_recipe")


@pytest.mark.unit
def test_list_recipes_reports_base_valid():
    entries = {item["recipe_id"]: item for item in list_recipes()}
    assert BASE_RECIPE_ID in entries
    assert entries[BASE_RECIPE_ID]["valid"] is True
    assert entries[BASE_RECIPE_ID]["recipe_hash"]


@pytest.mark.unit
def test_recipe_hash_is_canonical_and_semantic():
    recipe = load_recipe(BASE_RECIPE_ID)
    canonical = json.dumps(
        recipe.semantic_dict(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    assert recipe_hash(recipe) == hashlib.sha1(canonical.encode("utf-8")).hexdigest()

    # 键序无关：同一语义 dict 不同插入顺序 → 同 hash
    data = json.loads((RECIPE_DIR / f"{BASE_RECIPE_ID}.json").read_text(encoding="utf-8"))
    reordered = {k: data[k] for k in reversed(list(data.keys()))}
    assert recipe_hash(validate_recipe(reordered)) == recipe_hash(recipe)

    # 语义变化 → hash 变化
    mutated = json.loads(json.dumps(data))
    mutated["payload"]["dl_params"]["n_epochs"] = 12
    assert recipe_hash(validate_recipe(mutated)) != recipe_hash(recipe)


@pytest.mark.unit
@pytest.mark.parametrize(
    "needle",
    ["rolling_meta", "train_start", "test_end", "wfa"],
)
def test_validate_rejects_injected_keys_in_template(base_payload_file, needle):
    data = json.loads(json.dumps(base_payload_file))
    data["payload"][needle] = {"x": 1} if needle in ("rolling_meta", "wfa") else "2026-01-01"
    with pytest.raises(RecipeError) as ei:
        validate_recipe(data)
    assert needle in str(ei.value)


@pytest.mark.unit
def test_validate_rejects_factor_source_mismatch(base_payload_file):
    data = json.loads(json.dumps(base_payload_file))
    data["payload"]["factor_source"] = "l2_factors"
    with pytest.raises(RecipeError):
        validate_recipe(data)


@pytest.mark.unit
@pytest.mark.parametrize(
    "policy_patch",
    [
        {"mode": "weird"},
        {"train_days": 0},
        {"train_days": -5},
        {"train_days": "756"},
        {"purge_days": -1},
    ],
)
def test_validate_rejects_bad_window_policy(base_payload_file, policy_patch):
    data = json.loads(json.dumps(base_payload_file))
    data["window_policy"].update(policy_patch)
    with pytest.raises(RecipeError):
        validate_recipe(data)


@pytest.mark.unit
def test_validate_rejects_missing_features(base_payload_file):
    data = json.loads(json.dumps(base_payload_file))
    data["payload"]["features"] = []
    with pytest.raises(RecipeError):
        validate_recipe(data)


@pytest.mark.unit
def test_build_training_payload_injects_split_and_meta_without_mutating_recipe():
    recipe = load_recipe(BASE_RECIPE_ID)
    window = RollingWindow(
        anchor_date=date(2026, 9, 30),
        train_start=date(2022, 1, 4),
        train_end=date(2025, 6, 30),
        valid_start=date(2025, 7, 9),
        valid_end=date(2026, 1, 5),
        test_start=date(2026, 1, 14),
        test_end=date(2026, 9, 30),
        purge_days=6,
        mode="sliding",
        window_index=321,
    )
    payload = build_training_payload(
        recipe, window, campaign_id="rc_cn_cn_nativetft_base_20260930", dispatched_by="test"
    )
    for key, value in window.to_split_fields().items():
        assert payload[key] == value
    meta = payload["rolling_meta"]
    assert meta["campaign_id"] == "rc_cn_cn_nativetft_base_20260930"
    assert meta["window_index"] == 321
    assert meta["anchor_date"] == "2026-09-30"
    assert meta["purge_days"] == 6
    assert meta["recipe_hash"] == recipe_hash(recipe)
    assert meta["dispatched_by"] == "test"
    # 模板本体不被合成污染
    assert "train_start" not in recipe.payload
    assert "rolling_meta" not in recipe.payload
    # 派生 stamped 字段不在模板里（由服务端解析因子源时补）
    for derived in ("factor_field_sources", "factor_coverage", "factor_schema_hash"):
        assert derived not in payload


@pytest.mark.unit
def test_recipe_payload_roundtrip_is_json_serializable():
    recipe = load_recipe(BASE_RECIPE_ID)
    window = RollingWindow(
        anchor_date=date(2026, 9, 30),
        train_start=date(2022, 1, 4),
        train_end=date(2025, 6, 30),
        valid_start=date(2025, 7, 9),
        valid_end=date(2026, 1, 5),
        test_start=date(2026, 1, 14),
        test_end=date(2026, 9, 30),
        purge_days=6,
        mode="sliding",
        window_index=321,
    )
    payload = build_training_payload(recipe, window, "rc_x", "test")
    # 与训练编排链路同款：payload 会经 Redis/DB JSON 往返
    assert json.loads(json.dumps(payload, ensure_ascii=False)) == payload
