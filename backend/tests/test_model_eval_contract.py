"""P0-1/P0-4 测试：模型对比口径 + 样本外评估契约 + 产物独立性预检。

覆盖（缺陷出处：docs/滚动训练与模型生命周期_设计方案.md §2.2-1/5）：
1. `/models/compare` 键错回归：注册表返回 `metadata_json`/`metrics_json`，
   对比取值不能再读 `metadata`（P0-1，实测恒空）；
2. `resolve_oos_metrics` 口径固化：只认 `eval_report.by_split.test`（优先）
   与扁平 `test_*`（回退），headline（合并窗口，实测虚高 1.47×）永不进决策；
3. 产物独立性预检：指标逐位相同 + pred md5 相同 = 复制品（HK 13 子模型事件）；
4. 注册表软门禁抽取器改为委托本契约（by_split 优先）。
"""

from __future__ import annotations

import hashlib

from backend.shared.model_eval_contract import (
    OOS_SOURCE_BY_SPLIT,
    OOS_SOURCE_FLAT,
    artifact_independence,
    compare_metrics_of,
    features_of,
    file_md5,
    metrics_bitwise_equal,
    resolve_oos_metrics,
    resolve_pred_path,
)

# ── 注册表行形状的夹具（_row_to_model 的返回键：metadata_json/metrics_json）──


def _registry_model(**overrides):
    model = {
        "model_id": "mdl_a",
        "status": "ready",
        "is_default": False,
        "created_at": "2026-10-01T10:00:00",
        "storage_path": "user_models/default/u1/mdl_a",
        "metadata_json": {
            "model_type": "native_tft",
            "target_horizon_days": 5,
            "feature_count": 3,
            "features": ["f1", "f2", "f3"],
            "metrics": {"test_rank_ic": 0.011, "test_rank_icir": 0.07},
            "eval_report": {
                "by_split": {
                    "test": {
                        "mean": 0.021,
                        "std": 0.1,
                        "icir": 0.31,
                        "win_rate": 0.55,
                        "t_stat": 2.4,
                        "n_days": 40,
                        "n_rows": 4000,
                    }
                }
            },
        },
        "metrics_json": {"test_rank_ic": 0.011, "test_rank_icir": 0.07},
    }
    model.update(overrides)
    return model


# ── P0-1：对比取值必须读注册表真实键 ────────────────────────────────────────


def test_compare_metrics_reads_registry_shape():
    """回归：读 metadata_json.metrics —— 旧实现读 m['metadata'] 恒空。"""
    metrics = compare_metrics_of(_registry_model())
    assert metrics["test_rank_ic"] == 0.011
    assert metrics["test_rank_icir"] == 0.07
    assert metrics["model_type"] == "native_tft"
    assert metrics["target_horizon_days"] == 5
    assert metrics["feature_count"] == 3
    assert metrics["status"] == "ready"


def test_compare_metrics_tolerates_legacy_metadata_key():
    """防御：老形状 dict（metadata 键）仍可取到值。"""
    legacy = {
        "model_id": "mdl_old",
        "metadata": {"model_type": "lgbm", "metrics": {"val_rank_ic": 0.05}},
    }
    metrics = compare_metrics_of(legacy)
    assert metrics["model_type"] == "lgbm"
    assert metrics["val_rank_ic"] == 0.05
    assert metrics["test_rank_ic"] is None


def test_features_of_reads_registry_shape():
    assert features_of(_registry_model()) == {"f1", "f2", "f3"}
    assert features_of({"metadata_json": {"feature_columns": ["a"]}}) == {"a"}
    assert features_of({}) == set()


# ── P0-4：样本外口径固化（by_split.test 优先，headline 永不进决策）──────────


def test_resolve_oos_prefers_by_split_test():
    oos = resolve_oos_metrics(_registry_model()["metadata_json"], {"test_rank_ic": 0.011})
    assert oos["source"] == OOS_SOURCE_BY_SPLIT
    assert oos["is_oos_verified"] is True
    assert oos["rank_ic"] == 0.021  # by_split，不是扁平的 0.011
    assert oos["rank_icir"] == 0.31
    assert oos["t_stat"] == 2.4
    assert oos["n_days"] == 40


def test_resolve_oos_flat_fallback_not_verified():
    metadata = {"metrics": {"test_rank_ic": 0.011, "test_rank_icir": 0.07}}
    oos = resolve_oos_metrics(metadata, {"test_rank_ic": 0.011})
    assert oos["source"] == OOS_SOURCE_FLAT
    assert oos["is_oos_verified"] is False
    assert oos["rank_ic"] == 0.011
    assert oos["rank_icir"] == 0.07
    assert oos["t_stat"] is None


def test_resolve_oos_never_uses_headline():
    """headline（合并窗口）与 train/valid 段一律不得回退。"""
    metadata = {
        "rank_ic": 0.9,
        "ic": 0.8,
        "metrics": {"rank_ic": 0.9, "val_rank_ic": 0.5, "train_rank_ic": 0.95},
        "eval_report": {"by_split": {"train": {"mean": 0.95}, "valid": {"mean": 0.5}}},
    }
    oos = resolve_oos_metrics(metadata, {"rank_ic": 0.9})
    assert oos["rank_ic"] is None
    assert oos["source"] is None
    assert oos["is_oos_verified"] is False


def test_resolve_oos_empty_inputs():
    for md, mj in ((None, None), ({}, {}), ({}, None)):
        oos = resolve_oos_metrics(md, mj)
        assert oos["rank_ic"] is None and oos["source"] is None


# ── 产物独立性预检 ──────────────────────────────────────────────────────────


def test_metrics_bitwise_equal_rules():
    a = {"test_rank_ic": 0.011, "val_rank_ic": 0.05}
    assert metrics_bitwise_equal(a, dict(a)) is True
    assert metrics_bitwise_equal(a, {"test_rank_ic": 0.011, "val_rank_ic": 0.06}) is False
    # 无共同可比键 → 无法判定
    assert metrics_bitwise_equal({"test_rank_ic": 1.0}, {"val_ic": 0.2}) is None
    assert metrics_bitwise_equal({}, {}) is None


def test_artifact_independence_identical_copy():
    m = {"test_rank_ic": 0.017, "test_rank_icir": -0.0244, "val_rank_ic": 0.03}
    verdict = artifact_independence(
        {"model_id": "a", "metrics": m, "pred_md5": "d41d8"},
        {"model_id": "b", "metrics": dict(m), "pred_md5": "d41d8"},
    )
    assert verdict["verdict"] == "identical"
    assert verdict["metrics_equal"] is True
    assert verdict["pred_md5_equal"] is True
    assert verdict["reasons"]


def test_artifact_independence_metrics_copied():
    """HK 13 子模型事件形状：指标逐位相同（复制），pred 若缺失/不同也不放行。"""
    m = {"test_rank_ic": 0.017, "test_rank_icir": -0.0244}
    verdict = artifact_independence(
        {"model_id": "a", "metrics": m, "pred_md5": None},
        {"model_id": "b", "metrics": dict(m), "pred_md5": None},
    )
    assert verdict["verdict"] == "metrics_copied"
    assert verdict["metrics_equal"] is True


def test_artifact_independence_independent_and_inconclusive():
    v = artifact_independence(
        {"model_id": "a", "metrics": {"test_rank_ic": 0.01}, "pred_md5": "aaa"},
        {"model_id": "b", "metrics": {"test_rank_ic": 0.02}, "pred_md5": "bbb"},
    )
    assert v["verdict"] == "independent"
    assert v["metrics_equal"] is False

    v2 = artifact_independence(
        {"model_id": "a", "metrics": None, "pred_md5": None},
        {"model_id": "b", "metrics": {}, "pred_md5": None},
    )
    assert v2["verdict"] == "inconclusive"


# ── 文件级工具 ──────────────────────────────────────────────────────────────


def test_file_md5_and_resolve_pred_path(tmp_path):
    pred = tmp_path / "pred.parquet"
    payload = b"not-a-real-parquet-but-hashable"
    pred.write_bytes(payload)
    assert file_md5(pred) == hashlib.md5(payload).hexdigest()
    assert file_md5(tmp_path / "missing.parquet") is None

    assert resolve_pred_path({"storage_path": str(tmp_path)}) == pred
    assert resolve_pred_path({"storage_path": str(tmp_path / "nope")}) is None
    assert resolve_pred_path({}) is None


# ── 注册表软门禁抽取器委托（by_split 优先）─────────────────────────────────


def test_registry_extractors_delegate_to_contract():
    from backend.shared.model_registry import ModelRegistryService

    metadata = _registry_model()["metadata_json"]
    metrics = {"test_rank_ic": 0.011, "test_rank_icir": 0.07}
    # by_split.test 存在 → 门禁按 by_split 值判（0.31 / 0.021），不是扁平键
    assert ModelRegistryService._extract_test_rank_icir(metadata, metrics) == 0.31
    assert ModelRegistryService._extract_test_rank_ic(metadata, metrics) == 0.021
    # 无 by_split → 扁平回退（行为与旧实现一致）
    flat_meta = {"metrics": {"test_rank_ic": 0.011, "test_rank_icir": 0.07}}
    assert ModelRegistryService._extract_test_rank_icir(flat_meta, {}) == 0.07
    assert ModelRegistryService._extract_test_rank_ic(flat_meta, {}) == 0.011
    # 只有 headline → None（不进软门禁）
    assert ModelRegistryService._extract_test_rank_ic({"rank_ic": 0.9}, {}) is None
