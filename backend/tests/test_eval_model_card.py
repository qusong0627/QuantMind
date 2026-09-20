"""模型评分卡接线测试（`scripts/eval/model_card.py`，设计 §2.2 四维 + 证据闸门）。

覆盖：
- 四维不再写死 ``v1 缺省``：有 pred.parquet 时出真实维度、记明证据层级；
- 只有 metadata.json 时四维如实缺省，note 指向「无可用实证产物」；
- 产物不在盘的对象**不跳过**：出 ``score=None`` 的缺省卡（覆盖写回让陈旧 A 级现形）；
- ``partition_user_models``：盘上的进评分队列、不在盘的出卡，一个都不丢。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backend.scripts.eval.model_card import (
    missing_artifact_card,
    partition_user_models,
    score_model,
)
from backend.scripts.eval.model_realized import (
    NO_ARTIFACT_NOTE,
    OFF_DISK_NOTE,
    TIER_NONE,
    TIER_PRED,
)

FOUR_DIMS = ("stratification", "robustness", "rolling_health", "turnover_cost")


def _write_model_dir(root: Path, *, with_pred: bool) -> Path:
    """建一个最小可评分模型目录（可选带 test 段 pred.parquet）。"""
    root.mkdir(parents=True, exist_ok=True)
    (root / "metadata.json").write_text(
        json.dumps(
            {
                "run_id": "run_x",
                "model_type": "lightgbm",
                "metrics": {"test_rank_ic": 0.05, "test_rank_icir": 0.8},
            }
        ),
        encoding="utf-8",
    )
    if with_pred:
        n_days, n_stocks = 25, 30
        dates = [f"2026-0{1 + d // 28}-{d % 28 + 1:02d}" for d in range(n_days)]
        frame = pd.DataFrame(
            {
                "symbol": [
                    f"S{i:03d}.SZ" for _ in range(n_days) for i in range(n_stocks)
                ],
                "trade_date": pd.to_datetime(
                    [d for d in dates for _ in range(n_stocks)]
                ),
                "label": np.tile(np.arange(n_stocks, dtype=float) / 100.0, n_days),
                "pred": np.tile(np.arange(n_stocks, dtype=float), n_days),
                "split": ["test"] * (n_days * n_stocks),
            }
        )
        frame.to_parquet(root / "pred.parquet", index=False)
    return root / "metadata.json"


@pytest.mark.unit
def test_score_model_uses_real_dimensions_when_pred_parquet_present(tmp_path: Path):
    """一级证据在盘 → 四维是真实算出来的，并写明用了 pred.parquet。"""
    # Arrange
    meta_file = _write_model_dir(tmp_path / "mdl_with_pred", with_pred=True)

    # Act
    result = score_model("mdl_with_pred", meta_path=meta_file)

    # Assert
    assert result.get("error") is None
    strat = result["dimensions"]["stratification"]
    assert strat["score"] is not None  # 不再是写死的 v1 缺省
    assert strat["detail"]["monotonicity"] == pytest.approx(1.0)
    assert result["inputs_version"]["evidence"]["tier"] == TIER_PRED
    for key in FOUR_DIMS:
        assert result["dimensions"][key]["detail"]["evidence"] == TIER_PRED


@pytest.mark.unit
def test_score_model_reports_missing_artifacts_with_named_note(tmp_path: Path):
    """目录在盘但无实证产物 → 四维缺省，note 说明缺的是什么（不是「v1 缺省」）。"""
    # Arrange
    meta_file = _write_model_dir(tmp_path / "mdl_bare", with_pred=False)

    # Act
    result = score_model("mdl_bare", meta_path=meta_file)

    # Assert
    assert result["inputs_version"]["evidence"]["tier"] == TIER_NONE
    assert set(result["missing_dims"]) == set(FOUR_DIMS)
    for key in FOUR_DIMS:
        detail = result["dimensions"][key]["detail"]
        assert detail["insufficient"] is True
        assert detail["note"] == NO_ARTIFACT_NOTE


@pytest.mark.unit
def test_missing_artifact_card_is_insufficient_not_skipped():
    """产物不在盘：出缺省卡而非跳过——score=None 才能把陈旧 A 级覆盖掉。"""
    # Act
    card = missing_artifact_card(
        "mdl_gone", storage_path="/data/models/users/u/mdl_gone"
    )

    # Assert
    assert card.get("error") is None
    assert card["artifact_missing"] is True
    assert card["score"] is None and card["grade"] is None
    assert card["low_confidence"] is True
    assert set(card["missing_dims"]) == set(FOUR_DIMS) | {"oos_predictive"}
    for key in FOUR_DIMS:
        assert card["dimensions"][key]["detail"]["note"] == OFF_DISK_NOTE
    assert card["inputs_version"]["evidence"]["tier"] == TIER_NONE
    assert card["inputs_version"]["evidence"]["on_disk"] is False
    assert card["inputs_version"]["storage_path"] == "/data/models/users/u/mdl_gone"


@pytest.mark.unit
def test_partition_user_models_keeps_gone_artifacts_as_cards(tmp_path: Path):
    """DB 行分区：盘上的进评分队列，不在盘的出卡，一个都不丢。"""
    # Arrange
    on_disk = tmp_path / "ok" / "metadata.json"
    on_disk.parent.mkdir(parents=True)
    on_disk.write_text("{}", encoding="utf-8")
    items = [
        {"model_id": "mdl_ok", "meta_path": str(on_disk), "storage_path": ""},
        {"model_id": "mdl_gone", "meta_path": None, "storage_path": "/data/x/mdl_gone"},
        {"model_id": "", "meta_path": None, "storage_path": ""},  # 空 id 丢弃
    ]

    # Act
    cards, targets = partition_user_models(items)

    # Assert
    assert [c["object_id"] for c in cards] == ["mdl_gone"]
    assert [(mid, path.name) for mid, path in targets] == [("mdl_ok", "metadata.json")]


@pytest.mark.unit
def test_score_model_keeps_the_long_series_out_of_inputs_version(
    tmp_path: Path, monkeypatch
):
    """长序列必须落侧车，**不许进 inputs_version**。

    `inputs_version` 会整段写进 `eval_scores` 并随列表接口下发——上千点的逐日
    IC 一旦混进去，评估中心每次刷新都要拖着它走（这正是 §1.6 要避免的）。
    """
    # Arrange
    monkeypatch.setenv("QM_EVAL_SERIES_DIR", str(tmp_path / "series"))
    meta_file = _write_model_dir(tmp_path / "mdl_series", with_pred=True)

    # Act
    result = score_model("mdl_series", meta_path=meta_file)

    # Assert
    evidence = result["inputs_version"]["evidence"]
    assert "series" not in evidence and "series_note" not in evidence
    assert evidence["series_sidecar"]["written"] is True
    written = json.loads(
        (tmp_path / "series" / "model" / "mdl_series.json").read_text(encoding="utf-8")
    )
    assert written["series"]["daily_ic"]
    assert written["scalars"]["monotonicity"] == pytest.approx(1.0)


@pytest.mark.unit
def test_score_model_reports_why_there_is_no_series(tmp_path: Path, monkeypatch):
    """没有序列时如实说清是哪一种「没有」（该级证据本就不产 / 缓存未带）。"""
    # Arrange
    monkeypatch.setenv("QM_EVAL_SERIES_DIR", str(tmp_path / "series"))
    meta_file = _write_model_dir(tmp_path / "mdl_noseries", with_pred=False)

    # Act
    result = score_model("mdl_noseries", meta_path=meta_file)

    # Assert
    sidecar = result["inputs_version"]["evidence"]["series_sidecar"]
    assert sidecar["written"] is False
    assert TIER_NONE in str(sidecar["note"]) or "不产长序列" in str(sidecar["note"])
