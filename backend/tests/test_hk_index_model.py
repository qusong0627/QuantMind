"""港股宽基指数模型排序分聚合（hk_index_model）单元测试。"""
from __future__ import annotations

import pandas as pd
import pytest

from backend.services.api.market_analysis_hk import hk_index_model as m


def test_position_label_thresholds():
    assert m._position_label(0.53) == "靠前"
    assert m._position_label(0.52) == "靠前"
    assert m._position_label(0.50) == "居中"
    assert m._position_label(0.48) == "靠后"
    assert m._position_label(None) is None


def test_aggregate_index_mean_breadth_and_constituents():
    members = pd.DataFrame(
        {
            "index_name": ["HSI"] * 4,
            "stock_code": ["0700.HK", "0005.HK", "9988.HK", "1299.HK"],
            "name": ["腾讯", "汇丰", "阿里", "友邦"],
            "weight": [None, None, None, None],
        }
    )
    scores = pd.DataFrame(
        {
            "symbol": ["0700.HK", "0005.HK", "9988.HK", "1299.HK"],
            "pred": [0.60, 0.55, 0.45, 0.40],
        }
    )
    out = m._aggregate_index(
        {"code": "HSI", "name": "恒生指数", "symbol": "HSI.HK"},
        members,
        scores,
        {"0700.HK": "腾讯", "0005.HK": "汇丰", "9988.HK": "阿里", "1299.HK": "友邦"},
        {"top_quintile_cut": 0.55, "bottom_quintile_cut": 0.45},
    )
    assert out["available"] is True
    assert out["n_members"] == 4
    assert out["n_scored"] == 4
    assert out["coverage"] == 1.0
    assert out["mean_score"] == pytest.approx(0.5)
    assert out["median_score"] == pytest.approx(0.5)
    assert out["breadth"] == pytest.approx(0.5)  # 严格 >0.5，两条命中
    assert out["top_quintile_share"] == pytest.approx(0.5)
    assert out["bottom_quintile_share"] == pytest.approx(0.5)
    assert out["position"] == "居中"
    assert [x["symbol"] for x in out["top_constituents"][:2]] == ["0700.HK", "0005.HK"]
    assert out["bottom_constituents"][0]["symbol"] == "1299.HK"


def test_aggregate_index_missing_membership_is_unavailable():
    out = m._aggregate_index(
        {"code": "HSCCI", "name": "恒生红筹", "symbol": "HSCCI.HK"},
        None,
        pd.DataFrame({"symbol": ["0700.HK"], "pred": [0.6]}),
        {},
        {},
    )
    assert out["available"] is False
    assert out["reason"] == "暂无成分快照"
    assert out["top_constituents"] == []


def _write_membership(root, code: str, rows: list[dict]) -> None:
    d = root / "2_base_sector" / "index_membership" / "dt=20261114"
    d.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(d / f"{code}.parquet", index=False)


def test_compute_reads_latest_pred_and_marks_hscci_missing(tmp_path, monkeypatch):
    data_root = tmp_path / "quanthk"
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    pd.DataFrame(
        {
            "symbol": ["0700.HK", "0005.HK", "9988.HK", "1299.HK"],
            "trade_date": [
                "2026-11-13",
                "2026-11-13",
                "2026-11-13",
                "2026-11-12",
            ],
            "pred": [0.50, 0.70, 0.55, 0.10],
        }
    ).to_parquet(model_dir / "pred.parquet", index=False)
    for code in ("HSI", "HSCEI", "HSTECH"):
        _write_membership(
            data_root,
            code,
            [
                {"index_name": code, "stock_code": "0700.HK", "weight": 1.0},
                {"index_name": code, "stock_code": "0005.HK", "weight": 2.0},
            ],
        )
    monkeypatch.setattr(m, "DATA_DIR", data_root)

    out = m._compute(
        {
            "model_id": "mdl_hk_test",
            "storage_path": str(model_dir),
            "status": "ready",
            "metadata_json": {
                "display_name": "港股测试模型",
                "model_type": "lightgbm",
                "feature_count": 17,
                "target_horizon_days": 20,
            },
            "metrics_json": {"ic": 0.1, "ic_t": 2.0},
        }
    )
    assert out["available"] is True
    assert out["model"]["score_date"] == "2026-11-13"
    assert out["model"]["universe_size"] == 3  # 11-12 的旧行被截掉
    assert out["membership_date"] == "20261114"
    hsi = next(x for x in out["indices"] if x["code"] == "HSI")
    assert hsi["available"] is True
    assert hsi["mean_score"] == pytest.approx(0.6)
    assert hsi["position"] == "靠前"
    assert hsi["weighted_score"] == pytest.approx(0.6333, abs=1e-4)
    hscci = next(x for x in out["indices"] if x["code"] == "HSCCI")
    assert hscci["available"] is False
    assert hscci["reason"] == "暂无成分快照"


def test_compute_missing_pred_explains_how_to_fix(tmp_path):
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    out = m._compute(
        {
            "model_id": "mdl_hk_test",
            "storage_path": str(model_dir),
            "status": "ready",
            "metadata_json": {},
            "metrics_json": {},
        }
    )
    assert out["available"] is False
    assert "pred.parquet" in out["reason"]
