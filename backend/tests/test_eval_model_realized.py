"""模型四维实证引擎的评分与证据测试（`scripts/eval/model_realized.py`）。

覆盖（AAA 结构）：
- 四维评分红线：非单调 / 成本后 ≤0 / 滚动 20 日 <0.01；
- 秩退化闸门：pred 全常数 → 四维如实缺省（不编 IC=0 的分数）；
- 证据优先级降级：pred.parquet → metadata.eval_report → qm_model_inference_quality
  → 如实缺省，且**结果里必须写明用了哪一级**；
- 证据可用性闸门：产物不在盘 → 四维缺省 + note（不是静默跳过、不是沿用旧分）。

横截面统计纯函数（分层/分段/滚动/换手/秩退化）见 `test_eval_realized_stats.py`。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


# ── 四维评分与红线 ─────────────────────────────────────────────────────


@pytest.mark.unit
def test_score_stratification_red_line_on_non_monotonic_ladder():
    from backend.scripts.eval.model_realized import score_stratification
    from backend.scripts.eval.realized_stats import decile_stats

    # 桶状阶梯：中间高两头低 → 单调性 0，红线「非单调」
    pred = np.tile(np.arange(10, dtype=float), 3)
    label = np.tile(
        np.array([0.01, 0.02, 0.05, 0.09, 0.12, 0.12, 0.09, 0.05, 0.02, 0.01]), 3
    )
    dates = np.array([f"d{i}" for i in range(3) for _ in range(10)])
    stats = decile_stats(pred, label, dates, n_groups=5)

    dim = score_stratification(stats)

    assert dim.key == "stratification"
    assert dim.score is not None
    # 对称桶形阶梯：单调性恰为 0（最高档与最低档同样赚）
    assert dim.detail["monotonicity"] == pytest.approx(0.0, abs=1e-9)
    assert dim.red_line_failed is True
    assert "非单调" in dim.detail["red_line"]


@pytest.mark.unit
def test_score_turnover_cost_red_line_when_net_negative():
    from backend.scripts.eval.model_realized import score_turnover_cost
    from backend.services.engine.inference.trading_cost import CostModel

    # 毛利 1%/年，换手 2.0/日 → 成本拖累 2×0.0035×252 = 176% → 成本后为负
    rt = CostModel().round_trip_cost()
    dim = score_turnover_cost(
        {
            "sufficient": True,
            "turnover_mean": 2.0,
            "round_trip_cost": rt,
            "gross_annual": 0.01,
            "cost_drag_annual": 2.0 * rt * 252,
            "net_annual": 0.01 - 2.0 * rt * 252,
            "n_pairs": 100,
            "trading_days": 252,
        }
    )

    assert dim.red_line_failed is True
    assert dim.score == 0.0
    assert dim.detail["net_annual"] < 0


@pytest.mark.unit
def test_score_rolling_health_red_line_below_001():
    from backend.scripts.eval.model_realized import score_rolling_health

    dim = score_rolling_health(
        {
            "sufficient": True,
            "ic_mean_20": 0.002,
            "ic_mean_60": 0.03,
            "icir": 0.5,
            "drift": -0.028,
            "n_days": 80,
        }
    )

    assert dim.red_line_failed is True
    assert "0.01" in dim.detail["red_line"]


@pytest.mark.unit
def test_insufficient_dims_carry_verbatim_note():
    from backend.scripts.eval.model_realized import insufficient_dims

    dims = insufficient_dims("产物不在盘（storage_path 指向的目录已不存在）")

    assert set(dims) == {
        "stratification",
        "robustness",
        "rolling_health",
        "turnover_cost",
    }
    for dim in dims.values():
        assert dim.score is None
        assert dim.detail["insufficient"] is True
        assert dim.detail["note"] == "产物不在盘（storage_path 指向的目录已不存在）"


@pytest.mark.unit
def test_dims_from_pred_frame_marks_all_constant_model_insufficient():
    """全常数预测的模型：四维如实缺省，不得给出任何分数（实测有此类真实产物）。"""
    import pandas as pd

    from backend.scripts.eval.model_realized import (
        NO_SIGNAL_NOTE,
        TIER_PRED,
        dims_from_pred_frame,
    )

    rows = 30
    frame = pd.DataFrame(
        {
            "symbol": [f"S{i:03d}.SZ" for i in range(rows)],
            "trade_date": pd.to_datetime(
                [f"2026-01-{d + 1:02d}" for d in range(3) for _ in range(10)]
            ),
            "label": np.tile(np.arange(10, dtype=float) / 100.0, 3),
            "pred": np.full(rows, 2.0),  # 模型完全没输出排序
            "split": ["test"] * rows,
            "date_key": [f"2026-01-{d + 1:02d}" for d in range(3) for _ in range(10)],
        }
    )

    dims = dims_from_pred_frame(frame)

    for dim in dims.values():
        assert dim.score is None
        assert dim.detail["insufficient"] is True
        assert dim.detail["note"] == NO_SIGNAL_NOTE
        assert dim.detail["evidence"] == TIER_PRED
        assert dim.detail["coverage"]["n_days_degenerate"] == 3


@pytest.mark.unit
def test_dims_from_pred_frame_keeps_real_signal_days():
    """混合样本：坏日剔除后仍按好日给分，但退化日数必须留在证据里。"""
    import pandas as pd

    from backend.scripts.eval.model_realized import dims_from_pred_frame

    n_days, n_stocks = 3, 20
    dates = [f"2026-01-{d + 1:02d}" for d in range(n_days) for _ in range(n_stocks)]
    frame = pd.DataFrame(
        {
            "symbol": [f"S{i:03d}.SZ" for _ in range(n_days) for i in range(n_stocks)],
            "trade_date": pd.to_datetime(dates),
            "label": np.tile(np.arange(n_stocks, dtype=float) / 100.0, n_days),
            "pred": np.tile(np.arange(n_stocks, dtype=float), n_days),
            "split": ["test"] * (n_days * n_stocks),
            "date_key": dates,
        }
    )
    polluted = pd.concat(
        [
            frame,
            frame.assign(
                date_key="2026-01-09",
                trade_date=pd.to_datetime("2026-01-09"),
                pred=1.0,
            ),
        ],
        ignore_index=True,
    )

    dims = dims_from_pred_frame(polluted)

    assert dims["stratification"].score is not None
    assert dims["stratification"].detail["coverage"]["n_days_degenerate"] == 1
    assert dims["rolling_health"].detail["coverage"]["n_days_degenerate"] == 1
    assert dims["rolling_health"].detail["coverage"]["n_days_used"] == n_days


# ── 证据优先级降级 ─────────────────────────────────────────────────────


def _eval_report_fixture() -> dict:
    """metadata.eval_report 的最小真实形态（groups/long_short/by_split/ic_curve）。"""
    return {
        "version": 1,
        "n_rows": 1000,
        "n_days": 40,
        "rank_ic": {
            "mean": 0.04,
            "std": 0.04,
            "icir": 1.0,
            "win_rate": 0.6,
            "t_stat": 3.0,
        },
        "ic_curve": [0.05] * 20 + [0.03] * 20,
        "yearly": [
            {"year": 2025, "rank_ic": 0.06, "n_days": 20},
            {"year": 2026, "rank_ic": 0.04, "n_days": 20},
        ],
        "groups": {
            "n_groups": 10,
            "mean_returns": [-(i + 1) * 0.001 for i in range(10)][::-1],
            "monotonicity": 0.92,
        },
        "long_short": {
            "sharpe": 1.4,
            "ann_return": 0.18,
            "max_drawdown": -0.09,
            "curve": [0.001] * 40,
        },
        "by_split": {
            "train": {"rank_ic": 0.08, "n_days": 15},
            "valid": {"rank_ic": 0.05, "n_days": 12},
            "test": {"rank_ic": 0.03, "n_days": 13},
        },
    }


@pytest.mark.unit
def test_dims_from_eval_report_maps_groups_and_split_ic():
    from backend.scripts.eval.model_realized import dims_from_eval_report

    dims = dims_from_eval_report(_eval_report_fixture())

    strat = dims["stratification"]
    assert strat.score is not None
    assert strat.detail["mean_returns"][0] == pytest.approx(-0.010)
    assert strat.detail["mean_returns"][-1] == pytest.approx(-0.001)
    assert strat.detail["monotonicity"] == pytest.approx(0.92)
    assert strat.detail["evidence"] == "metadata.eval_report"

    robust = dims["robustness"]
    assert robust.detail["by_split"]["test"]["ic_mean"] == pytest.approx(0.03)
    # 年度段由 yearly 提供（口径与 pred 路径不同，如实标注）
    assert robust.detail["min_segment"] == pytest.approx(0.03)

    health = dims["rolling_health"]
    assert health.detail["ic_mean_20"] == pytest.approx(0.03)
    assert health.detail["n_days"] == 40

    # eval_report 不含换手证据 → 如实缺省（不硬造）
    assert dims["turnover_cost"].score is None
    assert dims["turnover_cost"].detail["insufficient"] is True


@pytest.mark.unit
def test_dims_from_inference_quality_only_fills_rolling_health():
    from backend.scripts.eval.model_realized import dims_from_inference_quality

    rows = [
        {"trade_date": f"2026-08-{i + 1:02d}", "rank_ic": 0.04 if i < 20 else 0.005}
        for i in range(30)
    ]

    dims = dims_from_inference_quality("mdl_x", rows)

    assert dims["rolling_health"].score is not None
    assert dims["rolling_health"].detail["evidence"] == "qm_model_inference_quality"
    for key in ("stratification", "robustness", "turnover_cost"):
        assert dims[key].score is None
        assert dims[key].detail["insufficient"] is True


@pytest.mark.unit
def test_resolve_dims_prefers_pred_over_eval_report(tmp_path: Path, monkeypatch):
    """证据优先级：pred.parquet 在盘且可读 → 用第一级，不降级。"""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from backend.scripts.eval import model_realized

    model_dir = tmp_path / "mdl_t"
    model_dir.mkdir()
    n_days, n_stocks = 3, 20
    frame = pd.DataFrame(
        {
            "symbol": [f"S{i:03d}.SZ" for _ in range(n_days) for i in range(n_stocks)],
            "trade_date": pd.to_datetime(
                [f"2026-01-{d + 1:02d}" for d in range(n_days) for _ in range(n_stocks)]
            ),
            "label": np.tile(np.arange(n_stocks, dtype=float) / 100.0, n_days),
            "pred": np.tile(np.arange(n_stocks, dtype=float), n_days),
            "split": ["test"] * (n_days * n_stocks),
        }
    )
    pq.write_table(
        pa.Table.from_pandas(frame, preserve_index=False), model_dir / "pred.parquet"
    )
    meta = {"eval_report": _eval_report_fixture()}

    dims, evidence = model_realized.resolve_dims(
        "mdl_t", meta=meta, model_dir=model_dir
    )

    assert evidence["tier"] == model_realized.TIER_PRED
    assert evidence["source"].endswith("pred.parquet")
    assert dims["stratification"].detail["evidence"] == model_realized.TIER_PRED
    # 缓存 sidecar 已落盘（下次直接吃缓存，不再读 parquet）
    assert (model_dir / ".eval_cache.json").is_file()


@pytest.mark.unit
def test_resolve_dims_falls_back_to_eval_report_without_pred(tmp_path: Path):
    from backend.scripts.eval import model_realized

    model_dir = tmp_path / "mdl_no_pred"
    model_dir.mkdir()

    dims, evidence = model_realized.resolve_dims(
        "mdl_no_pred", meta={"eval_report": _eval_report_fixture()}, model_dir=model_dir
    )

    assert evidence["tier"] == model_realized.TIER_EVAL_REPORT
    assert dims["stratification"].detail["mean_returns"] is not None
    assert dims["turnover_cost"].score is None


@pytest.mark.unit
def test_resolve_dims_uses_cached_sidecar_when_unchanged(tmp_path: Path, monkeypatch):
    """mtime+size 未变 → 吃 sidecar，不再打开 3.8GB 级 parquet（生产夜间批量的前提）。"""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from backend.scripts.eval import model_realized

    model_dir = tmp_path / "mdl_cache"
    model_dir.mkdir()
    n_days, n_stocks = 3, 10
    frame = pd.DataFrame(
        {
            "symbol": [f"S{i:03d}.SZ" for _ in range(n_days) for i in range(n_stocks)],
            "trade_date": pd.to_datetime(
                [f"2026-01-{d + 1:02d}" for d in range(n_days) for _ in range(n_stocks)]
            ),
            "label": np.tile(np.arange(n_stocks, dtype=float) / 100.0, n_days),
            "pred": np.tile(np.arange(n_stocks, dtype=float), n_days),
            "split": ["test"] * (n_days * n_stocks),
        }
    )
    pq.write_table(
        pa.Table.from_pandas(frame, preserve_index=False), model_dir / "pred.parquet"
    )

    first, _ = model_realized.resolve_dims("mdl_cache", meta=None, model_dir=model_dir)

    calls = {"n": 0}
    real_read = model_realized.read_test_split

    def _counting(path, **kwargs):
        calls["n"] += 1
        return real_read(path, **kwargs)

    monkeypatch.setattr(model_realized, "read_test_split", _counting)
    second, evidence = model_realized.resolve_dims(
        "mdl_cache", meta=None, model_dir=model_dir
    )

    assert calls["n"] == 0  # 命中缓存，零 parquet 读取
    assert evidence["cache"] == "hit"
    assert second["stratification"].detail["mean_returns"] == pytest.approx(
        first["stratification"].detail["mean_returns"]
    )


@pytest.mark.unit
def test_resolve_dims_reports_insufficient_when_artifact_off_disk(tmp_path: Path):
    """证据可用性闸门：产物不在盘 → 如实缺省 + 指定 note，绝不静默跳过。"""
    from backend.scripts.eval import model_realized

    missing = tmp_path / "gone"

    dims, evidence = model_realized.resolve_dims(
        "mdl_gone", meta=None, model_dir=missing
    )

    assert evidence["tier"] == model_realized.TIER_NONE
    assert evidence["on_disk"] is False
    for dim in dims.values():
        assert dim.score is None
        assert dim.detail["note"] == model_realized.OFF_DISK_NOTE


@pytest.mark.unit
def test_resolve_dims_exposes_tier_and_human_label(tmp_path: Path):
    """结果必须写明用了哪一级证据（前端页脚与「假证据」排查都靠它）。"""
    from backend.scripts.eval import model_realized

    model_dir = tmp_path / "mdl_tier"
    model_dir.mkdir()

    dims, evidence = model_realized.resolve_dims(
        "mdl_tier", meta={"eval_report": _eval_report_fixture()}, model_dir=model_dir
    )

    assert evidence["tier"] == model_realized.TIER_EVAL_REPORT
    assert evidence["tier_label"]
    assert dims["stratification"].detail["evidence"] == evidence["tier"]
