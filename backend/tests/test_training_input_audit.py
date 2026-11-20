"""WP1 输入可审计：目录发布闸 + 训练血缘 + 请求/解析/入模三段漏斗。

2026-11-19 事故：l1_factors 目录带 24 个已删除的 tdx_* 映射发布，训练请求按
裸名命中锚库 → 物理核对失败 → 静默少 12 列。这里锁三件事：
1. 发布期必须拦住「启用但物理列不存在」的映射（409）；
2. 训练 metadata 记录请求原文 / 目录解析 / 缺失清单；
3. 血缘字段（config_hash / seed / git commit）稳定可复现。
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("docker")

from fastapi import HTTPException  # noqa: E402

from backend.services.api.routers.admin import admin_training_utils as atu  # noqa: E402
from backend.services.api.routers.admin import quantdb_factor_catalog as qfc  # noqa: E402


class _Rows:
    def __init__(self, rows):
        self._rows = list(rows)

    def mappings(self):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar_one(self):
        return self._rows[0] if self._rows else 0


class _FakeSession:
    """按 SQL 关键字返回固定结果；UPDATE 返回 rowcount=1。"""

    def __init__(self, *, version, enabled_count, stale):
        self._version = version
        self._enabled_count = enabled_count
        self._stale = stale

    async def execute(self, statement, params=None):
        sql = str(statement)
        if "SELECT version_id, source_dataset, market, status" in sql:
            return _Rows([self._version] if self._version else [])
        if "SELECT count(*) FROM qm_training_factor_mapping" in sql:
            return _Rows([self._enabled_count])
        if "LEFT JOIN qm_quantdb_factor_field" in sql:
            return _Rows(self._stale)
        return _Rows([])


@pytest.mark.asyncio
async def test_publish_blocks_stale_mappings():
    session = _FakeSession(
        version={"version_id": "v1", "source_dataset": "l1_factors", "market": "CN", "status": "draft"},
        enabled_count=134,
        stale=[
            {"feature_key": "tdx_amihud", "source_column": "tdx_amihud"},
            {"feature_key": "tdx_ar", "source_column": "tdx_ar"},
        ],
    )
    with pytest.raises(HTTPException) as exc:
        await qfc.publish_catalog_version(session, "v1")
    assert exc.value.status_code == 409
    assert "tdx_amihud" in str(exc.value.detail)
    assert "刷新字段" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_publish_passes_when_no_stale():
    session = _FakeSession(
        version={"version_id": "v1", "source_dataset": "l1_factors", "market": "CN", "status": "draft"},
        enabled_count=110,
        stale=[],
    )
    await qfc.publish_catalog_version(session, "v1")


@pytest.mark.asyncio
async def test_find_stale_mappings_shape():
    session = _FakeSession(
        version=None,
        enabled_count=0,
        stale=[{"feature_key": "tdx_mass", "source_column": "tdx_mass"}],
    )
    rows = await qfc.find_stale_catalog_mappings(session, version_id="v1")
    assert rows == [{"feature_key": "tdx_mass", "source_column": "tdx_mass"}]


def test_build_default_metadata_carries_feature_funnel():
    payload = {
        "features": ["a", "b"],
        "requested_features_original": ["a", "b", "tdx_amihud"],
        "resolved_features": ["a", "b"],
        "missing_features": ["tdx_amihud"],
        "lineage": {"config_hash": "abc123", "seed": 42},
        "target_horizon_days": 10,
    }
    meta = atu._build_default_metadata(payload, "train_x")
    assert meta["requested_features_original"] == ["a", "b", "tdx_amihud"]
    assert meta["requested_feature_count_original"] == 3
    assert meta["resolved_feature_count"] == 2
    assert meta["missing_features"] == ["tdx_amihud"]
    assert meta["lineage"]["config_hash"] == "abc123"


def test_build_lineage_is_stable_and_records_seed():
    payload = {
        "features": ["a"],
        "factor_catalog_versions": {"l1_factors": "v1"},
        "factor_schema_hash": "h1",
        "lgb_params": {"seed": 7, "bagging_seed": 7},
        "generated_at": "2026-11-20T00:00:00Z",
    }
    first = atu._build_lineage(payload)
    payload["generated_at"] = "2026-11-20T01:00:00Z"
    second = atu._build_lineage(payload)
    assert first["config_hash"] == second["config_hash"], "generated_at 不应影响 config_hash"
    assert first["seed"] == 7
    assert first["factor_catalog_versions"] == {"l1_factors": "v1"}
    assert isinstance(first["git_commit"], str)


def test_normalize_payload_keeps_funnel_and_lineage():
    out = atu._normalize_payload(
        {
            "job_name": "wp1",
            "features": ["a"],
            "requested_features_original": ["a", "tdx_amihud"],
            "resolved_features": ["a"],
            "missing_features": ["tdx_amihud"],
            "lgb_params": {"seed": 7},
        },
        [],
    )
    assert out["requested_features_original"] == ["a", "tdx_amihud"]
    assert out["missing_features"] == ["tdx_amihud"]
    assert out["lineage"]["seed"] == 7


def test_rank_loss_rejects_quantile_and_optuna():
    base = {
        "job_name": "rank_case",
        "features": [],
        "lgb_params": {"objective": "rank_xendcg"},
    }
    with pytest.raises(HTTPException) as exc:
        atu._normalize_payload({**base, "prediction_mode": "quantile"}, [])
    assert "互斥" in str(exc.value.detail)

    with pytest.raises(HTTPException):
        atu._normalize_payload({**base, "optuna": {"enabled": True}}, [])


def test_sample_weight_normalized():
    out = atu._normalize_payload(
        {
            "job_name": "sw",
            "features": [],
            "sample_weight": {
                "tradeable_mask": "true",
                "st": 1,
                "time_decay_half_life_days": "730",
            },
        },
        [],
    )
    assert out["sample_weight"] == {
        "tradeable_mask": True,
        "st": True,
        "time_decay_half_life_days": 730,
    }
