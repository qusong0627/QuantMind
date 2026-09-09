"""shenwan_industry 的 QuantDB 路径解析回归测试。

背景：便携包数据目录是 ``$STORAGE_ROOT/quantdb``，机器上没有 ``/data/quantdb``。
修复前候选目录只含硬编码绝对路径 → 申万 128 行业映射失效，退到 ``stocks.industry``
（证监会分类）或空，影响选股与训练的行业口径。
"""
from __future__ import annotations

import pytest

pytest.importorskip("pandas")

from backend.services.engine.inference import shenwan_industry  # noqa: E402


def _raise_if_called():
    raise AssertionError("不应走到 DB fallback（本用例必须由 parquet 命中）")


@pytest.fixture
def quantdb_root(tmp_path, monkeypatch):
    import pandas as pd

    root = tmp_path / "quantdb"
    detail = root / "2_base_sector" / "instrument_detail"
    detail.mkdir(parents=True)
    pd.DataFrame(
        {
            "Symbol": ["600036.SH", "000001.SZ", "600519.SH"],
            "rs_hyname": ["银行", "银行", "白酒"],
        }
    ).to_parquet(detail / "instrument_detail.parquet")
    monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(root))
    return root


@pytest.fixture(autouse=True)
def _clear_shenwan_cache():
    shenwan_industry.load_shenwan_industry_map.cache_clear()
    yield
    shenwan_industry.load_shenwan_industry_map.cache_clear()


@pytest.mark.unit
def test_industry_map_loaded_from_env_dir(quantdb_root, monkeypatch):
    monkeypatch.setattr(shenwan_industry, "_load_from_db_fallback", _raise_if_called)

    mapping = shenwan_industry.load_shenwan_industry_map()

    assert mapping["600036.SH"] == "银行"
    assert mapping["600519.SH"] == "白酒"
    assert set(mapping.values()) == {"银行", "白酒"}


@pytest.mark.unit
def test_missing_parquet_falls_back_to_db(tmp_path, monkeypatch):
    """数据目录里没有 instrument_detail：仍可退到 DB，不抛异常。"""
    root = tmp_path / "quantdb"
    (root / "placeholder").mkdir(parents=True)
    monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(root))
    monkeypatch.setattr(
        shenwan_industry, "_load_from_db_fallback", lambda: {"600036.SH": "银行"}
    )

    assert shenwan_industry.load_shenwan_industry_map() == {"600036.SH": "银行"}


@pytest.mark.unit
def test_missing_parquet_and_db_returns_empty(tmp_path, monkeypatch):
    root = tmp_path / "quantdb"
    (root / "placeholder").mkdir(parents=True)
    monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(root))
    monkeypatch.setattr(shenwan_industry, "_load_from_db_fallback", lambda: {})

    assert shenwan_industry.load_shenwan_industry_map() == {}
