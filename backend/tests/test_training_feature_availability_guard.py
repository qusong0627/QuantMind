"""特征可用性硬闸（2026-11-19 100特征模型静默少列事件回归）。

事件：``l1_factors`` 目录残留 24 个 ``tdx_*`` 陈旧映射（物理列已从该库移除），
请求 100 个特征 → 提交侧解析通过 → 编排器过滤掉 12 个只打 warning →
训练实际 88 个候选、34 个入模，模型名仍是“100特征”。

这里锁两条契约：
1. 提交侧（API resolver）：目录有映射但物理列不存在 → 422 并列出特征名；
2. 编排器侧（config.yaml 构造）：缺失特征默认硬阻断，只有显式
   ``allow_missing_features=true`` 才允许按可用特征继续。
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("docker")

from fastapi import HTTPException  # noqa: E402

from backend.services.api.routers.admin import admin_training_utils as atu  # noqa: E402
from backend.services.engine.data_platform import quantdb_factor_reader as qfr  # noqa: E402
from backend.services.engine.training.local_docker_orchestrator import (  # noqa: E402
    LocalDockerOrchestrator,
)


def _orchestrator() -> LocalDockerOrchestrator:
    orchestrator = LocalDockerOrchestrator.__new__(LocalDockerOrchestrator)
    orchestrator.api_base = "http://quantmind-api:8000"
    orchestrator.internal_secret = "secret"
    return orchestrator


def _patch_split(monkeypatch, valid, missing):
    """把编排器内延迟 import 的 reader/splitter 换成固定结果。"""

    class FakeReader:
        def __init__(self, market=None):
            self.market = market

        def assert_ready(self, source, *, start=None, end=None):
            return type(
                "Status",
                (),
                {"schema_hash": "h1", "min_date": "2016-01-01", "max_date": "2026-11-18"},
            )()

    monkeypatch.setattr(qfr, "QuantDBFactorReader", FakeReader)
    monkeypatch.setattr(
        qfr,
        "split_features_by_availability",
        lambda reader, features, feature_sources=None, *, anchor: (list(valid), list(missing)),
    )


def _payload(**extra):
    base = {
        "job_name": "guard_case",
        "factor_source": "l1_factors",
        "train_start": "2016-01-01",
        "train_end": "2024-12-31",
        "features": ["concept_leader_score", "tdx_amihud"],
    }
    base.update(extra)
    return base


def test_config_build_blocks_when_requested_features_missing(monkeypatch):
    _patch_split(monkeypatch, ["concept_leader_score"], ["tdx_amihud"])

    with pytest.raises(RuntimeError) as exc:
        _orchestrator()._build_config_yaml("run_guard", _payload())

    text = str(exc.value)
    assert "tdx_amihud" in text
    assert "allow_missing_features=true" in text


def test_config_build_allows_missing_only_with_explicit_flag(monkeypatch):
    _patch_split(monkeypatch, ["concept_leader_score"], ["tdx_amihud"])

    cfg = _orchestrator()._build_config_yaml(
        "run_guard_ok", _payload(allow_missing_features=True)
    )

    assert cfg["data"]["features"] == ["concept_leader_score"]


def test_assert_features_physically_available_raises_422(monkeypatch):
    monkeypatch.setattr(
        atu,
        "split_features_by_availability",
        lambda reader, features, feature_sources=None, *, anchor, columns_of=None: (
            ["concept_leader_score"],
            ["tdx_amihud", "tdx_ar"],
        ),
    )

    with pytest.raises(HTTPException) as exc:
        atu._assert_features_physically_available(
            object(),
            features=["concept_leader_score", "tdx_amihud", "tdx_ar"],
            feature_sources={"tdx_amihud": "tdx_amihud", "tdx_ar": "tdx_ar"},
            anchor="l1_factors",
        )

    assert exc.value.status_code == 422
    assert "tdx_amihud" in str(exc.value.detail)
    assert "tdx_ind:tdx_amihud" in str(exc.value.detail)


def test_assert_features_physically_available_passes(monkeypatch):
    monkeypatch.setattr(
        atu,
        "split_features_by_availability",
        lambda reader, features, feature_sources=None, *, anchor, columns_of=None: (
            list(features),
            [],
        ),
    )

    atu._assert_features_physically_available(
        object(),
        features=["concept_leader_score"],
        feature_sources={"concept_leader_score": "concept_leader_score"},
        anchor="l1_factors",
    )
