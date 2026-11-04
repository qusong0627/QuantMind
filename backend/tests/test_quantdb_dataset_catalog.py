"""QuantDB 数据集目录与同步清单的对齐测试。

背景（2026-11-05）：上游 SDK 0.5.0 新增 ``factor_dictionary``（V5 因子宽表，
571 列）与 ``etf_kline``，并把 ``per_share`` 纳入同步目录。设置页「数据集」
读的是本仓自己的 ``backend/shared/quantdb_datasets.py`` 静态规格，同步走
``backend/scripts/quantdb_daily_sync.py`` 的清单——三处必须同时登记，否则
会出现「目录里有、点同步却被标 skipped」或「同步清单里有、设置页看不见」。

``factor_dictionary`` 单库 32GB，**不得**进每日默认同步（datasets=None）；
只有用户在前端显式勾选时才同步。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import backend.scripts.quantdb_daily_sync as qds
from backend.shared.quantdb_datasets import DATASETS, GROUPS

# 上游 0.5.0 新增、本仓需要同步登记的数据集
NEW_UPSTREAM_DATASETS = {"etf_kline", "factor_dictionary"}


def _spec(name: str):
    return next(ds for ds in DATASETS if ds.dataset == name)


def test_new_upstream_datasets_registered_in_catalog():
    names = {ds.dataset for ds in DATASETS}
    assert NEW_UPSTREAM_DATASETS <= names


def test_new_upstream_datasets_have_expected_layout_and_dir():
    etf = _spec("etf_kline")
    assert etf.group == "bond_etf"
    assert etf.layout == "partition"
    assert etf.rel_dir == "4_bond_etf/etf_kline"

    v5 = _spec("factor_dictionary")
    assert v5.group == "ml"
    assert v5.layout == "partition"
    assert v5.rel_dir == "6_ml_datasets/factor_dictionary"


def test_catalog_specs_are_internally_consistent():
    group_categories = {g["id"]: g["category_id"] for g in GROUPS}
    names = [ds.dataset for ds in DATASETS]
    assert len(names) == len(set(names)), "数据集名重复"
    for ds in DATASETS:
        assert ds.group in group_categories, f"{ds.dataset}: 未知分组 {ds.group}"
        assert ds.category_id == group_categories[ds.group], (
            f"{ds.dataset}: category_id 与分组不一致"
        )
        # rel_dir 的首层目录必须与 category_id 对应（SDK layout.CATEGORY_DIRS 口径）
        assert ds.rel_dir.split("/", 1)[0].startswith(f"{ds.category_id}_"), (
            f"{ds.dataset}: rel_dir 与 category_id 不一致: {ds.rel_dir}"
        )
        assert ds.layout in {"partition", "symbol", "single"}


def test_new_datasets_are_sync_covered():
    covered = {
        ds["sub_category"]
        for ds in qds.V2_DATASETS + qds.V1_DATASETS + qds.OPTIONAL_V2_DATASETS
    }
    assert NEW_UPSTREAM_DATASETS <= covered


def test_factor_dictionary_registered_as_cn_training_source():
    """设置页/训练页的因子源清单必须能选到 V5 宽表（动态发现外再显式登记）。"""
    from backend.services.engine.data_platform.quantdb_factor_reader import (
        FACTOR_SOURCE_DIRS,
        sources_for_market,
    )

    assert "factor_dictionary" in sources_for_market("CN")
    assert FACTOR_SOURCE_DIRS["factor_dictionary"] == "6_ml_datasets/factor_dictionary"


def test_release_metadata_objects_are_not_downloaded():
    """V5 的 v5_factor_dictionary.json 在 release 里但不是 parquet 下载面对象。"""
    assert qds._is_parquet_object(
        "6_ml_datasets/factor_dictionary/dt=20261104/data.parquet"
    )
    assert not qds._is_parquet_object(
        "6_ml_datasets/factor_dictionary/v5_factor_dictionary.json"
    )


def test_per_share_is_sync_covered_as_v1():
    assert "per_share" in {ds["sub_category"] for ds in qds.V1_DATASETS}


def test_heavy_new_datasets_not_in_default_sync():
    """32GB 的 V5 宽表不能因为升级 SDK 就变成每日自动下载。"""
    default = {ds["sub_category"] for ds in qds.V2_DATASETS + qds.V1_DATASETS}
    assert NEW_UPSTREAM_DATASETS.isdisjoint(default)


def test_optional_v2_dataset_uses_v2_downloader(monkeypatch):
    calls: dict[str, list[str]] = {"v2": [], "v1": []}

    def _fake_v2(_client, _state, _cat, sub, **_kw):
        calls["v2"].append(sub)
        return 0, []

    def _fake_v1(_client, _state, _cat, sub, **_kw):
        calls["v1"].append(sub)
        return 0, []

    monkeypatch.setattr(qds, "_make_client", lambda: object())
    monkeypatch.setattr(
        qds, "_open_state", lambda: type("S", (), {"close": lambda self: None})()
    )
    monkeypatch.setattr(qds, "_sync_v2_dataset", _fake_v2)
    monkeypatch.setattr(qds, "_sync_v1_dataset", _fake_v1)

    spec = next(
        ds
        for ds in qds.OPTIONAL_V2_DATASETS
        if ds["sub_category"] == "factor_dictionary"
    )
    qds.sync_parquet([spec])

    assert calls == {"v2": ["factor_dictionary"], "v1": []}


def test_run_daily_sync_treats_optional_dataset_as_covered(monkeypatch):
    seen: dict = {}

    def _fake_sync_parquet(ds_list, **_kwargs):
        seen["datasets"] = [d["sub_category"] for d in ds_list]
        return {"synced": 0}

    monkeypatch.setattr(qds, "sync_parquet", _fake_sync_parquet)
    monkeypatch.setattr(qds, "_sync_extra_sources", lambda **_kwargs: {})

    result = qds.run_daily_sync(
        datasets=["factor_dictionary"],
        skip_pg=True,
        skip_qlib=True,
        skip_snapshot=True,
    )

    assert result["uncovered_datasets"] == []
    assert seen["datasets"] == ["factor_dictionary"]
