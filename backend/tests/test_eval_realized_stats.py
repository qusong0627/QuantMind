"""评估引擎的横截面统计纯函数测试（`scripts/eval/realized_stats.py`）。

覆盖：pred schema 归一 / 缺标签显式报错 / 日等权分层 / 分段 IC 崩坏 /
滚动健康窗口序 / Top-K 换手成本 / 秩退化闸门（常数预测 ≠ 零相关）。
评分口径与证据降级见 `test_eval_model_realized.py`。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest


# ── normalize_pred_frame（两种 schema + 缺列显式报错） ──────────────────


@pytest.mark.unit
def test_normalize_pred_frame_accepts_both_schema_variants():
    from backend.scripts.eval.realized_stats import normalize_pred_frame

    # Arrange：schema A（label）与 schema B（label_return）
    frame_a = pd.DataFrame(
        {
            "symbol": ["SH600000", "SH600001"],
            "trade_date": pd.to_datetime(["2026-01-05", "2026-01-05"]),
            "label": [0.01, -0.02],
            "pred": [0.9, 0.1],
            "split": ["test", "test"],
        }
    )
    frame_b = pd.DataFrame(
        {
            "symbol": ["SH600000", "SH600001"],
            "trade_date": pd.to_datetime(["2026-01-05", "2026-01-05"]),
            "label_return": [0.01, -0.02],
            "pred": [0.9, 0.1],
            "split": ["test", "test"],
        }
    )

    # Act
    out_a = normalize_pred_frame(frame_a)
    out_b = normalize_pred_frame(frame_b)

    # Assert：两条路径产出同一口径（label 列 + 保留 label_return 供追溯）
    assert list(out_a["label"]) == [0.01, -0.02]
    assert list(out_b["label"]) == [0.01, -0.02]
    assert "label_return" in out_b.columns
    assert out_a["label"].dtype == float


@pytest.mark.unit
def test_normalize_pred_frame_raises_when_label_column_missing():
    """缺标签列必须显式报错——绝不当 0 算（那是把「没标签」伪装成「收益为 0」）。"""
    from backend.scripts.eval.realized_stats import normalize_pred_frame

    frame = pd.DataFrame(
        {
            "symbol": ["SH600000"],
            "trade_date": pd.to_datetime(["2026-01-05"]),
            "pred": [0.9],
            "split": ["test"],
        }
    )

    with pytest.raises(ValueError, match="标签列"):
        normalize_pred_frame(frame)

    with pytest.raises(ValueError, match="标签列"):
        normalize_pred_frame(frame.drop(columns=["pred"]))


@pytest.mark.unit
def test_normalize_pred_frame_does_not_mutate_input_and_filters_split():
    from backend.scripts.eval.realized_stats import normalize_pred_frame

    frame = pd.DataFrame(
        {
            "symbol": ["A", "B", "C"],
            "trade_date": pd.to_datetime(["2026-01-05", "2026-01-05", "2026-01-06"]),
            "label": [0.01, 0.02, 0.03],
            "pred": [1.0, 2.0, 3.0],
            "split": ["train", "test", "test"],
        }
    )
    before = frame.copy(deep=True)

    out = normalize_pred_frame(frame, split="test")

    pd.testing.assert_frame_equal(frame, before)  # 不可变：入参原样
    assert list(out["symbol"]) == ["B", "C"]
    assert set(out["split"]) == {"test"}


@pytest.mark.unit
def test_normalize_pred_frame_prefers_label_when_both_present():
    from backend.scripts.eval.realized_stats import normalize_pred_frame

    frame = pd.DataFrame(
        {
            "symbol": ["A"],
            "trade_date": pd.to_datetime(["2026-01-05"]),
            "label": [0.05],
            "label_return": [0.99],
            "pred": [1.0],
            "split": ["test"],
        }
    )

    out = normalize_pred_frame(frame)

    assert list(out["label"]) == [0.05]


# ── decile_stats（分层） ────────────────────────────────────────────────


@pytest.mark.unit
def test_decile_stats_empty_single_and_all_nan_are_insufficient():
    from backend.scripts.eval.realized_stats import decile_stats

    empty = decile_stats(np.array([]), np.array([]), np.array([]))
    assert empty["sufficient"] is False and empty["mean_returns"] is None

    # 单票：每天只有一只股票，构不成分组
    single = decile_stats(
        np.array([1.0, 2.0]),
        np.array([0.01, 0.02]),
        np.array(["2026-01-05", "2026-01-06"]),
    )
    assert single["sufficient"] is False
    assert "不足" in str(single["reason"])

    # 全 NaN 标签：不得当 0（否则「没标签」会伪装成「零收益分层」）
    nan_label = decile_stats(
        np.arange(20, dtype=float),
        np.full(20, np.nan),
        np.array(["2026-01-05"] * 20),
    )
    assert nan_label["sufficient"] is False


@pytest.mark.unit
def test_decile_stats_reproduces_strictly_monotonic_ladder():
    from backend.scripts.eval.realized_stats import decile_stats

    # Arrange：三天 × 10 票，pred 升序 → label 升序（完全单调）
    pred = np.tile(np.arange(10, dtype=float), 3)
    label = np.tile(np.arange(10, dtype=float) / 100.0, 3)
    dates = np.array(
        [d for d in ("2026-01-05", "2026-01-06", "2026-01-07") for _ in range(10)]
    )

    # Act
    stats = decile_stats(pred, label, dates, n_groups=5)

    # Assert：五档严格单调（Q1 0.005 → Q5 0.085），多空均值 = Q5 − Q1
    assert stats["sufficient"] is True
    assert stats["n_groups"] == 5
    means = stats["mean_returns"]
    assert means == sorted(means)
    assert means[0] == pytest.approx(0.005)
    assert means[-1] == pytest.approx(0.085)
    assert stats["monotonicity"] == pytest.approx(1.0)
    assert stats["strict_monotonic"] is True
    assert stats["ls_mean"] == pytest.approx(0.08)
    assert stats["n_days"] == 3


@pytest.mark.unit
def test_decile_stats_is_day_balanced_not_pooled():
    """日等权：只有 6 只票的那天不得被 200 只票的两天淹没。"""
    from backend.scripts.eval.realized_stats import decile_stats

    small, big = 6, 200
    # 小样本日：干净的高低两半（+0.20）；大样本日：pred 升 → label 降（约 −0.05）
    pred = np.concatenate(
        [
            np.repeat([0.0, 1.0], small // 2),
            np.linspace(0.0, 1.0, big),
            np.linspace(0.0, 1.0, big),
        ]
    )
    label = np.concatenate(
        [
            np.repeat([-0.10, 0.10], small // 2),
            np.linspace(0.0, -0.10, big),
            np.linspace(0.0, -0.10, big),
        ]
    )
    dates = np.array(["d1"] * small + ["d2"] * big + ["d3"] * big)

    stats = decile_stats(pred, label, dates, n_groups=2)

    assert stats["n_days"] == 3
    assert stats["ls_by_day"][0] == pytest.approx(0.20, abs=1e-9)
    assert stats["ls_by_day"][1] < 0
    assert stats["ls_mean"] == pytest.approx(sum(stats["ls_by_day"]) / 3, abs=1e-5)
    # 日等权下小样本日占 1/3 权重；按行合并只占 6/406，多空方向会翻负
    assert stats["ls_mean"] > 0


# ── segment_ic（稳健性） ────────────────────────────────────────────────


@pytest.mark.unit
def test_segment_ic_flags_sign_flipped_segment_as_broken():
    from backend.scripts.eval.realized_stats import segment_ic

    # Arrange：三段，各 30 天 × 10 票；第三段 pred/label 完全反向（符号翻转）
    n_days, n_stocks = 30, 10
    pred, label, dates, segs = [], [], [], []
    for seg, sign in (("train", 1.0), ("valid", 1.0), ("test", -1.0)):
        for d in range(n_days):
            base = np.arange(n_stocks, dtype=float)
            pred.extend(base)
            label.extend(sign * base / 100.0)
            dates.extend([f"{seg}-{d:02d}"] * n_stocks)
            segs.extend([seg] * n_stocks)

    out = segment_ic(np.array(pred), np.array(label), np.array(dates), np.array(segs))

    assert out["sufficient"] is True
    assert out["by_split"]["train"]["ic_mean"] > 0.9
    assert out["by_split"]["test"]["ic_mean"] < -0.9
    assert out["min_segment"] == pytest.approx(
        out["by_split"]["test"]["ic_mean"], abs=1e-9
    )
    assert out["broken_segments"] == ["test"]  # 符号翻转 = 崩坏


@pytest.mark.unit
def test_segment_ic_ignores_segments_below_min_days():
    from backend.scripts.eval.realized_stats import segment_ic

    # 3 天的小段不参与「崩坏」判定（样本太短，"崩坏"是噪声不是证据）
    pred = np.tile(np.arange(10, dtype=float), 4)
    label = np.tile(np.arange(10, dtype=float) / 100.0, 4)
    label[30:] *= -1.0  # 第四天（10 票）反向
    dates = np.array([f"d{i}" for i in range(4) for _ in range(10)])
    segs = np.array(["a"] * 30 + ["tiny"] * 10)

    out = segment_ic(pred, label, dates, segs, min_segment_days=20)

    assert "tiny" in out["by_split"]
    assert out["broken_segments"] == []


# ── rolling_health（滚动健康） ──────────────────────────────────────────


@pytest.mark.unit
def test_rolling_health_detects_ic_decay():
    from backend.scripts.eval.realized_stats import rolling_health

    # 前 60 天 IC=0.05，后 20 天 IC=0.002（跌破设计 §2.2 的 0.01 红线）
    ic = {f"d{i:03d}": (0.05 if i < 60 else 0.002) for i in range(80)}

    out = rolling_health(ic, short=20, long=60)

    assert out["sufficient"] is True
    assert out["n_days"] == 80
    assert out["ic_mean_20"] == pytest.approx(0.002)
    assert out["ic_mean_60"] == pytest.approx((0.05 * 40 + 0.002 * 20) / 60)
    assert out["drift"] == pytest.approx(0.002 - (0.05 * 40 + 0.002 * 20) / 60)
    assert out["icir"] == pytest.approx(
        float(np.mean(list(ic.values()))) / float(np.std(list(ic.values()), ddof=1))
    )


@pytest.mark.unit
def test_rolling_health_insufficient_below_min_days():
    from backend.scripts.eval.realized_stats import rolling_health

    out = rolling_health({"d0": 0.05, "d1": 0.04}, short=20, long=60)

    assert out["sufficient"] is False
    assert out["ic_mean_20"] is None


@pytest.mark.unit
def test_rolling_health_preserves_caller_date_order():
    """窗口顺序 = 入参顺序：若按键字典序重排，「近 20 日」会取到别的日子上去。"""
    from backend.scripts.eval.realized_stats import rolling_health

    # 未补零的 str(i) 键：字典序为 0,1,10,11,…,19,2,20,…，与时间序完全不同
    ic = {str(i): float(i) for i in range(30)}

    out = rolling_health(ic, short=20, long=30)

    assert out["ic_mean_20"] == pytest.approx(np.mean(range(10, 30)))
    assert out["ic_last"] == pytest.approx(29.0)
    assert out["n_days"] == 30


@pytest.mark.unit
def test_daily_ic_returns_chronological_keys():
    from backend.scripts.eval.realized_stats import daily_ic

    # 行序被打乱（模拟 parquet 行序不保证按日聚集）：07 → 05 → 06
    ladder = np.tile([0.0, 1.0, 2.0], 3)
    dates = np.array(["2026-01-07"] * 3 + ["2026-01-05"] * 3 + ["2026-01-06"] * 3)

    out = daily_ic(ladder, ladder, dates)

    assert list(out) == ["2026-01-05", "2026-01-06", "2026-01-07"]
    assert set(out.values()) == {1.0}


# ── topk_turnover（换手与成本） ────────────────────────────────────────


@pytest.mark.unit
def test_topk_turnover_counts_replaced_names():
    from backend.scripts.eval.realized_stats import topk_turnover

    rank_by_date = {
        "d1": ["A", "B", "C"],
        "d2": ["A", "B", "C"],  # 完全不变 → 换手 0
        "d3": ["A", "D", "E"],  # 换掉 2/3
    }

    out = topk_turnover(rank_by_date, k=3, round_trip_cost=0.0)

    assert out["sufficient"] is True
    assert out["n_pairs"] == 2
    assert out["turnover_mean"] == pytest.approx((0.0 + 2.0 / 3.0) / 2, abs=1e-6)
    assert out["turnover_series"][0] == 0.0
    assert out["turnover_series"][1] == pytest.approx(2.0 / 3.0, abs=1e-6)


@pytest.mark.unit
def test_topk_turnover_cost_drag_uses_round_trip_cost():
    """成本拖累 = 日均换手 × 双边成本 × 年交易日（费率取 trading_cost 唯一出处）。"""
    from backend.scripts.eval.realized_stats import topk_turnover

    try:
        from backend.services.engine.inference.trading_cost import CostModel
    except ModuleNotFoundError as exc:  # 本地轻量 .venv 无 sqlalchemy（inference 包连带导入）
        pytest.skip(f"本地 .venv 缺依赖（{exc.name}）：费率口径测试在容器内跑")

    rank_by_date = {"d1": ["A", "B"], "d2": ["C", "D"]}  # 完全换仓 → 换手 1.0
    rt = CostModel().round_trip_cost()

    out = topk_turnover(rank_by_date, k=2, round_trip_cost=rt, trading_days=252)

    assert out["turnover_mean"] == pytest.approx(1.0)
    assert out["round_trip_cost"] == pytest.approx(rt)
    assert out["cost_drag_annual"] == pytest.approx(1.0 * rt * 252)


@pytest.mark.unit
def test_topk_turnover_insufficient_without_pairs():
    from backend.scripts.eval.realized_stats import topk_turnover

    out = topk_turnover({"d1": ["A", "B"]}, k=2, round_trip_cost=0.0)
    assert out["sufficient"] is False and out["turnover_mean"] is None


# ── 秩退化闸门（常数预测 = 伪证据） ────────────────────────────────────


@pytest.mark.unit
def test_constant_prediction_day_yields_no_ic_and_is_counted():
    """常数预测日：IC 无定义。返回 0.0 等于编造「零相关」，必须剔除并计数。"""
    from backend.scripts.eval.realized_stats import daily_ic, daily_ic_coverage

    # d1/d2 有真实排序，d3 的 pred 全天常数
    pred = np.array([0.0, 1.0, 2.0, 0.0, 1.0, 2.0, 5.0, 5.0, 5.0])
    label = np.array([0.0, 1.0, 2.0, 2.0, 1.0, 0.0, 0.5, -0.5, 0.0])
    dates = np.array(["d1"] * 3 + ["d2"] * 3 + ["d3"] * 3)

    ic = daily_ic(pred, label, dates)
    coverage = daily_ic_coverage(pred, label, dates)

    assert "d3" not in ic  # 不编 0.0
    assert ic["d1"] == pytest.approx(1.0)
    assert coverage["n_days_ok"] == 2
    assert coverage["n_days_degenerate"] == 1
    assert coverage["degenerate_days"] == ["d3"]


@pytest.mark.unit
def test_constant_label_day_is_also_degenerate():
    from backend.scripts.eval.realized_stats import daily_ic_coverage

    pred = np.array([0.0, 1.0, 2.0])
    label = np.array([0.01, 0.01, 0.01])  # 全截面同涨同跌 → 秩相关无定义
    dates = np.array(["d1"] * 3)

    coverage = daily_ic_coverage(pred, label, dates)

    assert coverage["n_days_degenerate"] == 1


@pytest.mark.unit
def test_decile_stats_skips_constant_prediction_days():
    """常数预测日的「档」只是行序伪影——跳过并计数，不当证据。"""
    from backend.scripts.eval.realized_stats import decile_stats

    good = np.tile(np.arange(10, dtype=float), 3)
    good_label = np.tile(np.arange(10, dtype=float) / 100.0, 3)
    good_dates = np.array([d for d in ("d1", "d2", "d3") for _ in range(10)])

    pred = np.concatenate([good, np.full(10, 7.0)])
    label = np.concatenate([good_label, np.arange(10, dtype=float) / 100.0])
    dates = np.concatenate([good_dates, np.array(["d4"] * 10)])

    stats = decile_stats(pred, label, dates, n_groups=5)

    assert stats["n_days"] == 3  # d4 未被计入
    assert stats["n_days_degenerate"] == 1
    assert stats["mean_returns"][-1] == pytest.approx(0.085)  # 阶梯未受 d4 污染


@pytest.mark.unit
def test_decile_stats_all_days_constant_is_insufficient_with_reason():
    from backend.scripts.eval.realized_stats import decile_stats

    pred = np.full(30, 3.0)
    label = np.tile(np.arange(10, dtype=float) / 100.0, 3)
    dates = np.array(["d1"] * 10 + ["d2"] * 10 + ["d3"] * 10)

    stats = decile_stats(pred, label, dates, n_groups=5)

    assert stats["sufficient"] is False
    assert stats["n_days_degenerate"] == 3
    assert "常数" in str(stats["reason"])
