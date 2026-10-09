"""P2 回放配对统计（``scripts/eval/paired_stats.py``）纯函数测试。

覆盖（AAA 结构）：

- 符号归一：A 股后缀/前缀/裸码、港股各形态、美股 → 配对键；
- 帧准备：NaN 剔除、重复 ``(date_key, symbol)`` 去重（keep last）如实计数；
- 配对 ΔIC：**共同日期 ∩ 共同池**（池不同先取交集——跨池直接比均值会得出
  方向相反的结论，实测教训）；日期不对称 / 秩退化 / 单票日 / 标签不一致
  都如实留痕且不参与配对；
- 汇总：mean/std/单侧 t/p（ddof=1）；零方差与样本不足如实说不足；
- 月度统计：近 N 个月切片、最差月、月间 std；
- 换手：k 截断与成本拖累关系，口径转发 ``realized_stats``（不重造）。

IO 与 CLI（campaign 拼接、模型目录解析）不在本文件——那部分是
``walkforward_rollup.py`` 的职责，验收走真实产物。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest


def _frame(rows: list[tuple[str, str, float, float]]) -> pd.DataFrame:
    """``(date_key, symbol, pred, label)`` 行表 → 最小归一帧。"""
    return pd.DataFrame(
        rows, columns=["date_key", "symbol", "pred", "label"]
    ).astype({"date_key": str, "symbol": str})


#: 四只标的（prefix 形式——实测 pred.parquet 就是这一形态）
SYMS4 = ["SH600001", "SH600002", "SH600003", "SH600004"]

#: D1/D2 两日的标签（升序 1%..4%）
LABELS = [0.01, 0.02, 0.03, 0.04]


def _paired_base() -> tuple[pd.DataFrame, pd.DataFrame]:
    """两日基础对：D1 Δ=+2.0，D2 Δ=−0.8（手算见下）。

    D1：挑战者满序 IC=+1，冠军反序 IC=−1 → Δ=+2。
    D2：挑战者 pred 秩 [4,1,3,2]（IC=−0.4），冠军 pred 秩 [1,4,2,3]（IC=+0.4）
        → Δ=−0.8。
    """
    c_rows, h_rows = [], []
    for day, c_preds, h_preds in (
        ("2026-01-05", [0.1, 0.2, 0.3, 0.4], [0.4, 0.3, 0.2, 0.1]),
        ("2026-01-06", [0.4, 0.1, 0.3, 0.2], [0.1, 0.4, 0.2, 0.3]),
    ):
        for sym, pc, ph, lb in zip(SYMS4, c_preds, h_preds, LABELS, strict=True):
            c_rows.append((day, sym, pc, lb))
            h_rows.append((day, sym, ph, lb))
    return _frame(c_rows), _frame(h_rows)


# ── 符号归一 ───────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("600036.SH", "SH600036"),
        ("sh600036", "SH600036"),
        ("SH600036", "SH600036"),
        ("600036", "SH600036"),
        ("000001.SZ", "SZ000001"),
        ("000001", "SZ000001"),
        ("700.HK", "0700.HK"),
        ("00700.HK", "0700.HK"),
        ("HK00700", "0700.HK"),
        ("80001", "80001.HK"),
        ("AAPL", "AAPL"),
        ("aapl", "AAPL"),
        ("  ", ""),
        (None, ""),
    ],
)
def test_canon_symbol_forms(raw, expected):
    from backend.scripts.eval.paired_stats import canon_symbol

    assert canon_symbol(raw) == expected


# ── 帧准备 ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_prepare_frame_drops_nan_and_dups_with_counts():
    from backend.scripts.eval.paired_stats import prepare_frame

    # Arrange：5 行里 1 行重复（keep last）、2 行 NaN
    frame = _frame(
        [
            ("2026-01-05", "SH600001", 0.1, 0.01),
            ("2026-01-05", "SH600001", 0.2, 0.01),  # 重复：留这条
            ("2026-01-05", "SH600002", float("nan"), 0.02),
            ("2026-01-05", "SH600003", 0.3, float("nan")),
            ("2026-01-05", "SH600004", 0.4, 0.04),
        ]
    )

    # Act
    clean, notes = prepare_frame(frame)

    # Assert
    assert notes == {
        "rows_in": 5,
        "rows_out": 2,
        "dropped_symbol": 0,
        "dropped_nan": 2,
        "dropped_dup": 1,
    }
    kept = clean[clean["symbol"] == "SH600001"]
    assert len(kept) == 1
    assert float(kept["pred"].iloc[0]) == pytest.approx(0.2)


@pytest.mark.unit
def test_prepare_frame_requires_columns():
    from backend.scripts.eval.paired_stats import prepare_frame

    with pytest.raises(ValueError, match="pred"):
        prepare_frame(_frame([("2026-01-05", "SH600001", 0.1, 0.01)]).drop(columns=["pred"]))


# ── 配对 ΔIC ───────────────────────────────────────────────────────────


@pytest.mark.unit
def test_paired_delta_matches_hand_computed_ics_and_summary():
    from backend.scripts.eval.paired_stats import (
        paired_daily_delta,
        prepare_frame,
        summarize_delta,
    )

    # Arrange
    c_raw, h_raw = _paired_base()
    c, c_notes = prepare_frame(c_raw)
    h, h_notes = prepare_frame(h_raw)

    # Act
    paired = paired_daily_delta(c, h, challenger_notes=c_notes, champion_notes=h_notes)
    summary = summarize_delta(paired["delta"])

    # Assert：Δ = C_IC − H_IC 逐日已知
    assert set(paired["delta"]) == {"2026-01-05", "2026-01-06"}
    assert paired["delta"]["2026-01-05"] == pytest.approx(2.0, abs=1e-6)
    assert paired["delta"]["2026-01-06"] == pytest.approx(-0.8, abs=1e-6)
    assert paired["n_days_paired"] == 2
    assert paired["n_days_rows_common"] == 2
    assert paired["challenger"]["n_days"] == 2
    assert paired["champion"]["n_days"] == 2

    # mean=0.6；std(ddof=1)=1.97990；t=0.6/(std/√2)=0.42857；单侧 p≈0.3711
    assert summary["sufficient"] is True
    assert summary["mean"] == pytest.approx(0.6, abs=1e-6)
    assert summary["std"] == pytest.approx(1.97990, abs=1e-4)
    assert summary["t"] == pytest.approx(0.42857, abs=1e-4)
    assert summary["p_one_sided"] == pytest.approx(0.3711, abs=2e-3)
    assert summary["share_positive"] == pytest.approx(0.5)
    assert summary["median"] == pytest.approx(0.6)


@pytest.mark.unit
def test_paired_delta_intersects_pool_before_ic():
    """池不同先取交集：挑战者多出的极端行不得影响逐日 IC。"""
    from backend.scripts.eval.paired_stats import paired_daily_delta, prepare_frame

    # Arrange：挑战者 D1 多一只极端票（pred/label 都 0.99），若混入会翻转 IC
    c_raw, h_raw = _paired_base()
    c_raw = pd.concat(
        [c_raw, _frame([("2026-01-05", "SH600009", 0.99, 0.99)])], ignore_index=True
    )
    c, _ = prepare_frame(c_raw)
    h, _ = prepare_frame(h_raw)

    # Act
    paired = paired_daily_delta(c, h)

    # Assert：D1 的 IC 在 4 只共同池上 = +1（未被第 5 只污染）
    assert paired["challenger_ic"]["2026-01-05"] == pytest.approx(1.0, abs=1e-6)
    assert paired["pool_median"] == 4
    assert paired["pool_min"] == 4
    assert paired["pool_max"] == 4
    # 重叠占比（对共同日期取中位数）：挑战者侧 [4/5, 4/4] → 0.9；冠军侧 1.0
    assert paired["overlap_share_challenger_median"] == pytest.approx(0.9)
    assert paired["overlap_share_champion_median"] == pytest.approx(1.0)


@pytest.mark.unit
def test_paired_delta_date_asymmetry_degenerate_and_thin_days():
    from backend.scripts.eval.paired_stats import paired_daily_delta, prepare_frame

    # Arrange：基础两日 + 冠军独有 D3 + 冠军退化 D4 + 单票 D5
    c_raw, h_raw = _paired_base()
    c_raw = pd.concat(
        [
            c_raw,
            _frame(
                [
                    ("2026-01-07", SYMS4[0], 0.1, 0.01),
                    ("2026-01-07", SYMS4[1], 0.2, 0.02),
                    ("2026-01-07", SYMS4[2], 0.3, 0.03),
                    ("2026-01-07", SYMS4[3], 0.4, 0.04),
                ]
            ),
        ],
        ignore_index=True,
    )
    h_raw = pd.concat(
        [
            h_raw,
            # D3：冠军独有
            _frame(
                [
                    ("2026-01-03", SYMS4[0], 0.1, 0.01),
                    ("2026-01-03", SYMS4[1], 0.2, 0.02),
                ]
            ),
            # D4：冠军 pred 全天常数 → 秩退化
            _frame([("2026-01-07", s, 0.5, lb) for s, lb in zip(SYMS4, LABELS, strict=True)]),
            # D5：两侧都只有一只 → thin
            _frame([("2026-01-08", SYMS4[0], 0.1, 0.01)]),
        ],
        ignore_index=True,
    )
    c_raw = pd.concat(
        [c_raw, _frame([("2026-01-08", SYMS4[0], 0.2, 0.01)])], ignore_index=True
    )
    c, _ = prepare_frame(c_raw)
    h, _ = prepare_frame(h_raw)

    # Act
    paired = paired_daily_delta(c, h)

    # Assert
    assert paired["dates_only_champion"] == ["2026-01-03"]
    assert paired["n_dates_only_champion"] == 1
    assert paired["n_dates_only_challenger"] == 0
    assert "2026-01-07" not in paired["delta"]  # 冠军退化日不配对
    assert paired["n_days_champion_no_ic"] == 1
    assert paired["n_days_challenger_no_ic"] == 0
    assert paired["n_days_thin_common"] == 1  # D5
    assert paired["n_days_paired"] == 2


@pytest.mark.unit
def test_paired_delta_label_mismatch_counted_and_paired():
    from backend.scripts.eval.paired_stats import paired_daily_delta, prepare_frame

    # Arrange：D1 上一只票两侧标签不一致（0.9 vs 0.04）
    c_raw, h_raw = _paired_base()
    h_raw.loc[
        (h_raw["date_key"] == "2026-01-05") & (h_raw["symbol"] == SYMS4[3]), "label"
    ] = 0.9
    c, _ = prepare_frame(c_raw)
    h, _ = prepare_frame(h_raw)

    # Act
    paired = paired_daily_delta(c, h)

    # Assert：如实计数，仍配对（标签各自成对）
    assert paired["n_label_mismatch_rows"] == 1
    assert "2026-01-05" in paired["label_mismatch_dates"]
    assert paired["n_days_paired"] == 2


# ── 汇总 ───────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_summarize_delta_insufficient_and_zero_variance():
    from backend.scripts.eval.paired_stats import summarize_delta

    empty = summarize_delta({})
    assert empty["sufficient"] is False
    assert empty["t"] is None

    one = summarize_delta({"2026-01-05": 1.0})
    assert one["sufficient"] is False
    assert "不足" in one["reason"]

    flat = summarize_delta({"d1": 0.5, "d2": 0.5, "d3": 0.5})
    assert flat["sufficient"] is True
    assert flat["mean"] == pytest.approx(0.5)
    assert flat["t"] is None
    assert flat["reason"] and "方差" in flat["reason"]


# ── 月度统计（G3 口径） ────────────────────────────────────────────────


@pytest.mark.unit
def test_monthly_ic_stats_last_12_months_worst_and_std():
    from backend.scripts.eval.paired_stats import monthly_ic_stats

    # Arrange：14 个月；2026-01 两天求均值；2026-02 最差
    ic = {}
    for i in range(12):
        ic[f"2025-{i + 1:02d}-15"] = 0.001 * (i + 1)
    ic["2026-01-05"] = 0.02
    ic["2026-01-06"] = 0.04
    ic["2026-02-10"] = -0.05

    # Act
    stats = monthly_ic_stats(ic, last_months=12)

    # Assert
    assert stats["n_months_total"] == 14
    assert stats["n_months"] == 12
    assert stats["window"] == ["2025-03", "2026-02"]
    assert "2025-01" not in stats["months"] and "2025-02" not in stats["months"]
    assert stats["months"]["2026-01"] == pytest.approx(0.03)  # (0.02+0.04)/2
    assert stats["worst_month"] == ["2026-02", pytest.approx(-0.05)]
    kept = [stats["months"][m] for m in sorted(stats["months"])]
    # 证据字段按 6 位小数落盘，容差跟着走
    assert stats["month_std"] == pytest.approx(float(np.std(kept, ddof=1)), abs=1e-6)


@pytest.mark.unit
def test_monthly_ic_stats_single_month_has_no_std():
    from backend.scripts.eval.paired_stats import monthly_ic_stats

    stats = monthly_ic_stats({"2026-01-05": 0.01}, last_months=12)
    assert stats["n_months"] == 1
    assert stats["month_std"] is None
    assert stats["worst_month"] == ["2026-01", pytest.approx(0.01)]


# ── 换手（G6 取数） ────────────────────────────────────────────────────


@pytest.mark.unit
def test_annualized_turnover_wraps_topk_and_cost_drag():
    from backend.scripts.eval.paired_stats import annualized_turnover

    # Arrange：3 日 × 6 只；k=2 的 top2 集合 d1={E,F} → d2={A,B} → d3={E,F}
    # → 逐对换手 1.0，均值 1.0
    syms = [f"SH60000{i}" for i in range(1, 7)]
    rows = []
    for day, preds in (
        ("2026-01-05", [1, 2, 3, 4, 5, 6]),
        ("2026-01-06", [6, 5, 4, 3, 2, 1]),
        ("2026-01-07", [1, 2, 3, 4, 5, 6]),
    ):
        for sym, p in zip(syms, preds, strict=True):
            rows.append((day, sym, float(p), 0.01))
    frame = _frame(rows)

    # Act
    out = annualized_turnover(frame, k=2, round_trip_cost=0.001)

    # Assert
    assert out["sufficient"] is True
    assert out["top_k"] == 2
    assert out["turnover_mean"] == pytest.approx(1.0)
    assert out["cost_drag_annual"] == pytest.approx(1.0 * 0.001 * 252)
