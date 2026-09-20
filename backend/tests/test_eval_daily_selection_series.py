"""每日选股长序列载荷（`scripts/eval/daily_selection_series.py`，设计 §1.6）。

每日选股天然就是时间序列：`eval_scores` 里每个交易日一行，序列 = 跨日期的
「入选数 / 事后 T+H 超额 / 命中率」。

两条纪律（与阶段 1 一脉相承）：

1. **待回填的日子不进曲线**：T+H 前向数据没齐就跳过并计数，绝不补 0——补 0 会被
   读成「那天超额是 0」，而事实是「还不知道」；
2. **维度缺省不猜**：`dimensions.coverage.detail.picked` 缺失就当这天不参与，
   不拿别处的数字顶上。
"""

from __future__ import annotations

import pytest

from backend.scripts.eval.daily_selection_series import (
    daily_selection_series_payload,
    history_upto,
    selection_point,
)


def _row(
    day: str,
    *,
    picked: int | None = 3,
    realized: dict | None = None,
    quality: dict | None = None,
) -> dict:
    """eval_scores 行的最小形态（只带序列用得到的维度）。"""
    dims: dict = {}
    if picked is not None:
        dims["coverage"] = {"score": 100.0, "detail": {"picked": picked}}
    if realized is not None:
        dims["realized"] = {"score": 60.0, "detail": realized}
    if quality is not None:
        dims["quality"] = {"score": 70.0, "detail": quality}
    return {"snapshot_date": day, "dimensions": dims}


def _realized(excess: float, hit: float, *, horizon: int = 5) -> dict:
    return {"horizon": horizon, "n": 3, "mean_excess": excess, "hit_rate": hit}


# ── 单行抽取 ─────────────────────────────────────────────────────────


@pytest.mark.unit
def test_selection_point_extracts_picked_and_realized():
    """从一行评分卡里取出三个量（口径即该行 dimensions 的 detail）。"""
    # Act
    point = selection_point(_row("2026-09-18", picked=4, realized=_realized(0.021, 0.667)))

    # Assert
    assert point["date"] == "2026-09-18"
    assert point["picked"] == 4
    assert point["mean_excess"] == 0.021
    assert point["hit_rate"] == 0.667
    assert point["pending"] is False


@pytest.mark.unit
def test_selection_point_marks_pending_day():
    """未回填的日子 pending=True 且没有超额/命中率（不是 0）。"""
    # Act
    point = selection_point(
        _row("2026-09-19", picked=2, realized={"horizon": 5, "pending": True})
    )

    # Assert
    assert point["pending"] is True
    assert point["mean_excess"] is None
    assert point["hit_rate"] is None
    assert point["picked"] == 2


@pytest.mark.unit
def test_selection_point_without_dimensions_is_none():
    """维度和日期都缺 → None（调用方据此跳过并计数，不编一行出来）。"""
    assert selection_point({"dimensions": {}}) is None
    assert selection_point({}) is None
    assert selection_point(None) is None


@pytest.mark.unit
def test_selection_point_tolerates_malformed_detail():
    """detail 不是字典 / 值不是数 → 该项记 None，不抛异常。"""
    # Act
    point = selection_point(
        {
            "snapshot_date": "2026-09-18",
            "dimensions": {
                "coverage": {"detail": "not-a-dict"},
                "realized": {"detail": {"mean_excess": "abc", "hit_rate": None}},
            },
        }
    )

    # Assert
    assert point["picked"] is None
    assert point["mean_excess"] is None
    assert point["hit_rate"] is None


# ── 序列组装 ─────────────────────────────────────────────────────────


@pytest.mark.unit
def test_daily_selection_series_payload_builds_three_series_in_date_order():
    """三条序列（入选数 / 超额 / 命中率）都按日期升序，顺序与输入无关。"""
    # Arrange
    history = [
        _row("2026-09-18", picked=4, realized=_realized(0.021, 0.667)),
        _row("2026-09-16", picked=1, realized=_realized(-0.008, 0.333)),
        _row("2026-09-17", picked=3, realized=_realized(0.004, 0.5)),
    ]

    # Act
    payload = daily_selection_series_payload(history)

    # Assert
    assert payload["series"]["picked"] == [
        {"date": "2026-09-16", "value": 1.0},
        {"date": "2026-09-17", "value": 3.0},
        {"date": "2026-09-18", "value": 4.0},
    ]
    assert [p["date"] for p in payload["series"]["realized_excess"]] == [
        "2026-09-16",
        "2026-09-17",
        "2026-09-18",
    ]
    assert [p["value"] for p in payload["series"]["hit_rate"]] == [0.333, 0.5, 0.667]
    assert payload["scalars"]["n_dates"] == 3


@pytest.mark.unit
def test_daily_selection_series_payload_excludes_pending_days_from_realized():
    """待回填的日子：入选数照画（那是当日事实），超额/命中率不进曲线。"""
    # Arrange
    history = [
        _row("2026-09-17", picked=3, realized=_realized(0.004, 0.5)),
        _row("2026-09-18", picked=2, realized={"horizon": 5, "pending": True}),
    ]

    # Act
    payload = daily_selection_series_payload(history)

    # Assert
    assert [p["date"] for p in payload["series"]["picked"]] == [
        "2026-09-17",
        "2026-09-18",
    ]
    assert [p["date"] for p in payload["series"]["realized_excess"]] == ["2026-09-17"]
    assert payload["scalars"]["n_pending"] == 1
    assert "1" in payload["notes"]["realized_excess"]
    assert "回填" in payload["notes"]["realized_excess"]


@pytest.mark.unit
def test_daily_selection_series_payload_dedupes_same_date_keeping_last():
    """同一日期多行（重跑写了新快照）→ 只留最后一行，避免同一天两个点。"""
    # Arrange
    history = [
        _row("2026-09-18", picked=1, realized=_realized(-0.05, 0.0)),
        _row("2026-09-18", picked=4, realized=_realized(0.03, 0.75)),
    ]

    # Act
    payload = daily_selection_series_payload(history)

    # Assert
    assert payload["series"]["picked"] == [{"date": "2026-09-18", "value": 4.0}]
    assert payload["scalars"]["n_dates"] == 1


@pytest.mark.unit
def test_daily_selection_series_payload_skips_dates_without_picked():
    """没有入选数维度的那天不参与（不拿别处数字顶上），并计数。"""
    # Arrange
    history = [
        _row("2026-09-17", picked=3, realized=_realized(0.004, 0.5)),
        _row("2026-09-18", picked=None, realized=_realized(0.01, 0.6)),
    ]

    # Act
    payload = daily_selection_series_payload(history)

    # Assert
    assert [p["date"] for p in payload["series"]["picked"]] == ["2026-09-17"]
    assert payload["scalars"]["n_skipped"] == 1
    assert "1" in payload["notes"]["picked"]


@pytest.mark.unit
def test_daily_selection_series_payload_empty_history_explains_absence():
    """没有历史 → 空序列 + 原因，不留空白。"""
    # Act
    payload = daily_selection_series_payload([])

    # Assert
    assert payload["series"]["picked"] == []
    assert payload["scalars"]["n_dates"] == 0
    assert payload["notes"]["picked"]


@pytest.mark.unit
def test_daily_selection_series_payload_carries_latest_scalars():
    """最新一天的标量单独带出（详情页头部与曲线同源）。"""
    # Arrange
    history = [
        _row("2026-09-17", picked=3, realized=_realized(0.004, 0.5)),
        _row("2026-09-18", picked=7, realized=_realized(0.021, 0.667)),
    ]

    # Act
    payload = daily_selection_series_payload(history)

    # Assert
    assert payload["scalars"]["latest_date"] == "2026-09-18"
    assert payload["scalars"]["latest_picked"] == 7
    assert payload["scalars"]["latest_excess"] == 0.021
    assert payload["scalars"]["latest_hit_rate"] == 0.667


@pytest.mark.unit
def test_daily_selection_series_payload_truncates_to_recent_window():
    """超长历史只留最近 N 天（近端才是决策依据），截断写进 notes。"""
    # Arrange
    history = [
        _row(f"2026-01-{d:02d}", picked=1, realized=_realized(0.0, 0.5))
        for d in range(1, 29)
    ] + [
        _row(f"2026-02-{d:02d}", picked=2, realized=_realized(0.0, 0.5))
        for d in range(1, 29)
    ]

    # Act
    payload = daily_selection_series_payload(history, max_points=10)

    # Assert
    assert len(payload["series"]["picked"]) == 10
    assert payload["scalars"]["n_dates"] == 56
    assert "10" in payload["notes"]["picked"]


# ── 按日期切片（详情页不许看到未来）──────────────────────────────────


@pytest.mark.unit
def test_history_upto_cuts_rows_after_the_reference_day():
    """某日的侧车只装该日及以前：09-15 的曲线里不许出现 09-18 的成绩。"""
    # Arrange
    history = [_row("2026-09-15"), _row("2026-09-18"), _row("2026-09-11")]

    # Act
    rows = history_upto(history, "2026-09-15")

    # Assert
    assert [r["snapshot_date"] for r in rows] == ["2026-09-15", "2026-09-11"]


@pytest.mark.unit
def test_history_upto_without_a_day_or_history_is_empty():
    """没有参考日期 / 没有历史行 → 空（调用方据此只画当天，不猜）。"""
    assert history_upto([_row("2026-09-15")], "") == []
    assert history_upto(None, "2026-09-15") == []
    assert history_upto([{"no_date": True}], "2026-09-15") == []


@pytest.mark.unit
def test_history_upto_keeps_rows_on_the_same_day():
    """当日这一行要留下（当日侧车必须包含自己那天的点）。"""
    rows = history_upto([_row("2026-09-18")], "2026-09-18")

    assert [r["snapshot_date"] for r in rows] == ["2026-09-18"]


@pytest.mark.unit
def test_payload_carries_extra_note_into_every_curve_note():
    """历史读取失败这类原因必须出现在每条曲线的 note 上（缺数据可以，不说不行）。"""
    # Act
    payload = daily_selection_series_payload(
        [_row("2026-09-18", realized={"horizon": 5, "mean_excess": 0.01, "hit_rate": 0.6})],
        extra_note="历史行读取失败（OperationalError）：连接中断",
    )

    # Assert
    for key in ("picked", "realized_excess", "hit_rate"):
        assert "历史行读取失败" in payload["notes"][key]
