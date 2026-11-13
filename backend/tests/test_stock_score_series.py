"""分数序列读取 / 模型元数据（`model_training` 内部函数）回归。

这些函数依赖 duckdb 与模型注册表导入链；在缺少 docker 依赖的环境里整模块跳过，
在完整测试环境（容器/CI）里执行。
"""

from __future__ import annotations

from datetime import date

import pytest

try:
    model_training = pytest.importorskip("backend.services.api.routers.model_training")
except ImportError as exc:  # 本地仓根的 docker/ 目录会遮蔽 PyPI docker 包
    pytest.skip(
        f"model_training 导入链不可用（需要完整运行环境）: {exc}",
        allow_module_level=True,
    )


def _write_pred(path, rows):
    import pandas as pd

    df = pd.DataFrame(
        {
            "symbol": [r[0] for r in rows],
            "trade_date": pd.to_datetime([r[1] for r in rows]),
            "pred": [r[2] for r in rows],
        }
    )
    df.to_parquet(path, index=False)


@pytest.fixture()
def pred_model(tmp_path, monkeypatch):
    parquet = tmp_path / "pred.parquet"
    _write_pred(
        parquet,
        [
            ("600519.SH", "2026-01-05", 0.10),
            ("600519.SH", "2026-01-06", 0.30),
            ("SH600519", "2026-01-06", 0.20),  # 同一裸码的另一种写法
            ("000001.SZ", "2026-01-05", 0.50),
        ],
    )
    monkeypatch.setattr(
        model_training, "_resolve_pred_candidates", lambda _sp: [parquet]
    )
    return tmp_path


def test_read_stock_pred_history_plain_returns_window(pred_model) -> None:
    rows = model_training._read_stock_pred_history(
        str(pred_model), "600519", date(2026, 1, 1), "m1"
    )
    assert [r["trade_date"] for r in rows] == ["2026-01-06", "2026-01-05"]
    assert all(r["signal_side"] is None for r in rows)
    assert all(r["score_rank"] is None for r in rows)


def test_read_stock_pred_history_anchor_and_rank(pred_model) -> None:
    rows = model_training._read_stock_pred_history(
        str(pred_model),
        "600519",
        date(2026, 1, 1),
        "m1",
        anchor=date(2026, 1, 5),
        include_rank=True,
    )
    assert [r["trade_date"] for r in rows] == ["2026-01-05"]
    # 全市场当天两只股票：000001 分数更高，600519 排第 2
    assert rows[0]["score_rank"] == 2
    assert rows[0]["total_in_market"] == 2


def test_pred_last_date_cache(pred_model) -> None:
    first = model_training._pred_last_date(str(pred_model))
    second = model_training._pred_last_date(str(pred_model))
    assert first == "2026-01-06"
    assert second == first


def test_model_dropdown_item_reports_horizon_and_pred(pred_model) -> None:
    item = model_training._model_dropdown_item(
        {
            "model_id": "m1",
            "storage_path": str(pred_model),
            "is_default": True,
            "metadata_json": {
                "target_horizon_days": 5,
                "target_mode": "return",
                "execution_lag_days": 1,
            },
        },
        include_last_date=True,
    )
    assert item["horizon"] == 5
    assert item["target_mode"] == "return"
    assert item["execution_lag_days"] == 1
    assert item["has_pred"] is True
    assert item["pred_last_date"] == "2026-01-06"
    assert item["is_default"] is True


def test_model_dropdown_item_without_pred_reports_gap(tmp_path) -> None:
    item = model_training._model_dropdown_item(
        {
            "model_id": "m2",
            "storage_path": str(tmp_path),
            "metadata_json": {"target_horizon_days": 20},
        }
    )
    assert item["has_pred"] is False
    assert item["pred_last_date"] is None
    assert item["pred_mtime"] is None
    assert item["asset_gaps"]
