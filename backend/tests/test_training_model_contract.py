"""模型特征契约（B3）单元测试。"""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pytest

# backend.shared.training.__init__ 会 import request（依赖 fastapi）；
# 宿主机没有 fastapi 时整组跳过，容器内正常执行。
pytest.importorskip("fastapi")

from backend.shared.training import contract as c


@pytest.fixture(autouse=True)
def _clear_cache():
    c.clear_contract_cache()
    yield
    c.clear_contract_cache()


def _meta(**overrides):
    base = {
        "factor_source": "l1_factors",
        "features": ["mom_ret_5d", "close", "lib:alpha_col"],
        "factor_schema_hash": "h1",
        "factor_field_sources": {"lib:alpha_col": "factor_defs:alpha_col"},
        "market": "CN",
        "data_fingerprint": {"labels_max_date": date.today().isoformat()},
    }
    base.update(overrides)
    return base


def _patch_reader(
    monkeypatch, status=None, missing=None, raises: Exception | None = None
):
    status = status or SimpleNamespace(
        ready=True, schema_hash="h1", max_date="2026-11-04", reason=None
    )
    missing = missing or []

    class FakeReader:
        def __init__(self, market=None):
            self.market = market

        def describe(self, source):
            if raises is not None:
                raise raises
            return status

    def fake_split(reader, features, feature_sources=None, *, anchor):
        return [f for f in features if f not in missing], list(missing)

    monkeypatch.setattr(c, "QuantDBFactorReader", FakeReader)
    monkeypatch.setattr(c, "split_features_by_availability", fake_split)


def test_non_quantdb_model_is_skipped():
    report = c.validate_model_contract("m1", metadata={"features": ["x"]})
    assert report["skipped"] is True
    assert report["ok"] is True
    assert report["blocked"] is False


def test_ok_contract(monkeypatch):
    _patch_reader(monkeypatch)
    report = c.validate_model_contract("m1", metadata=_meta())
    assert report["ok"] is True
    assert report["blocked"] is False
    assert report["missing_features"] == []
    assert report["blockers"] == []


def test_missing_features_block(monkeypatch):
    _patch_reader(monkeypatch, missing=["lib:alpha_col"])
    report = c.validate_model_contract("m1", metadata=_meta())
    assert report["blocked"] is True
    assert report["missing_features"] == ["lib:alpha_col"]
    assert any("features_missing" in b for b in report["blockers"])


def test_schema_drift_blocks_by_default(monkeypatch):
    _patch_reader(
        monkeypatch,
        status=SimpleNamespace(
            ready=True, schema_hash="h2", max_date="2026-11-04", reason=None
        ),
    )
    report = c.validate_model_contract("m1", metadata=_meta())
    assert report["blocked"] is True
    assert any("factor_schema_drift" in b for b in report["blockers"])


def test_schema_drift_can_be_allowed_with_warning(monkeypatch):
    _patch_reader(
        monkeypatch,
        status=SimpleNamespace(
            ready=True, schema_hash="h2", max_date="2026-11-04", reason=None
        ),
    )
    report = c.validate_model_contract("m1", metadata=_meta(), allow_schema_drift=True)
    assert report["ok"] is True
    assert report["blocked"] is False
    assert any("factor_schema_drift" in w for w in report["warnings"])


def test_source_not_ready_blocks(monkeypatch):
    _patch_reader(
        monkeypatch,
        status=SimpleNamespace(
            ready=False, schema_hash="", max_date=None, reason="no partitions"
        ),
    )
    report = c.validate_model_contract("m1", metadata=_meta())
    assert report["blocked"] is True
    assert any("no partitions" in b for b in report["blockers"])


def test_describe_exception_blocks(monkeypatch):
    _patch_reader(monkeypatch, raises=RuntimeError("parquet broken"))
    report = c.validate_model_contract("m1", metadata=_meta())
    assert report["blocked"] is True
    assert any("parquet broken" in b for b in report["blockers"])


def test_missing_schema_hash_is_warning_not_blocker(monkeypatch):
    _patch_reader(monkeypatch)
    report = c.validate_model_contract("m1", metadata=_meta(factor_schema_hash=None))
    assert report["ok"] is True
    assert "factor_schema_hash_missing" in report["warnings"]


def test_stale_model_data_is_warning(monkeypatch):
    _patch_reader(monkeypatch)
    old = (date.today() - timedelta(days=200)).isoformat()
    report = c.validate_model_contract(
        "m1", metadata=_meta(data_fingerprint={"labels_max_date": old})
    )
    assert report["ok"] is True
    assert any("model_data_stale" in w for w in report["warnings"])


def test_features_empty_blocks(monkeypatch):
    _patch_reader(monkeypatch)
    report = c.validate_model_contract("m1", metadata=_meta(features=[]))
    assert report["blocked"] is True
    assert "features_empty" in report["blockers"]


def test_contract_block_message_is_actionable():
    msg = c.contract_block_message(
        {"blockers": ["features_missing(1): x", "factor_schema_drift: a b"]}
    )
    assert "模型特征契约未通过" in msg
    assert "features_missing" in msg


def test_both_default_switch_paths_are_gated():
    """两条默认切换路径都必须调用契约校验（rollout 晋升 + 手动 set_default）。"""
    import inspect

    from backend.services.engine.services.model_rollout_service import (
        ModelRolloutService,
    )
    from backend.shared.model_registry import ModelRegistryService

    set_src = inspect.getsource(ModelRegistryService.set_default_model)
    assert "validate_model_contract" in set_src
    assert 'contract.get("blocked")' in set_src
    assert "enforce_contract" in set_src

    promote_src = inspect.getsource(ModelRolloutService.promote)
    assert "validate_model_contract" in promote_src
    assert "allow_schema_drift" in promote_src
    assert "promotion_contract" in promote_src
