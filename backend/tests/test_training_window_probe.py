"""时间切分探针（window_probe）：切分基于节点自己的交易日序列。

背景：远端节点数据来自魔搭数据集（月度更新），与中心每日更新的交易日集合
并不同步——实测 l1_factors 少 225 个交易日且成段缺失（2022-10 ~ 2023-09）。
因此切分必须以**节点探针读到的交易日序列**为基准，并把成段缺失识别出来。
"""

from __future__ import annotations

import datetime as _dt


def _bdays(start: str, end: str) -> list[str]:
    d0 = _dt.date.fromisoformat(start)
    d1 = _dt.date.fromisoformat(end)
    out, cur = [], d0
    while cur <= d1:
        if cur.weekday() < 5:
            out.append(cur.isoformat())
        cur += _dt.timedelta(days=1)
    return out


def _window(kind: str, dates: list[str], *, node_id: str = "local"):
    from backend.services.engine.training import window_probe as wp

    return wp.DataWindow(
        kind=kind,
        node_id=node_id,
        source="l1_factors",
        market="CN",
        ready=True,
        min_date=dates[0] if dates else None,
        max_date=dates[-1] if dates else None,
        trading_dates=list(dates),
        columns=["close", "mom_ret_1d"],
        schema_hash="deadbeef",
        probed_at="2026-09-24T00:00:00+00:00",
    )


def _payload(start: str, end: str, val_ratio: float = 0.15) -> dict:
    return {
        "factor_source": "l1_factors",
        "train_start": start,
        "train_end": end,
        "val_ratio": val_ratio,
    }


def test_split_bounds_matches_splits_formula():
    from backend.services.engine.training import window_probe as wp

    dates = _bdays("2020-01-01", "2024-12-31")
    split = wp.split_bounds(dates, 0.15)
    assert split is not None
    n = len(dates)
    val_idx = int(n * (1 - 0.15))
    test_idx = int(n * (1 - 0.15 / 2))
    assert split["train"] == [dates[0], dates[val_idx - 1]]
    assert split["valid"] == [dates[val_idx], dates[test_idx - 1]]
    assert split["test"] == [dates[test_idx], dates[-1]]


def test_split_uses_node_dates_not_center_dates():
    """节点缺段时，切分点必须按节点自己的序列算（与中心日历结果不同）。"""
    from backend.services.engine.training import window_probe as wp

    center_dates = _bdays("2020-01-01", "2021-12-31")
    # 模拟整段缺失：挖掉中间 3 个月
    node_dates = [d for d in center_dates if not ("2021-01-01" <= d <= "2021-03-31")]
    payload = _payload("2020-01-01", "2021-12-31")

    node_split = wp.build_split_from_window(_window("remote", node_dates, node_id="autodl-2"), payload)
    center_split = wp.build_split_from_window(_window("local", center_dates), payload)

    assert node_split is not None and center_split is not None
    assert node_split == wp.split_bounds(node_dates, 0.15)
    assert node_split != center_split
    # 节点上三段都非空且顺序正确
    assert node_split["train"][0] < node_split["train"][1] < node_split["valid"][0]
    assert node_split["valid"][1] < node_split["test"][0]


def test_tail_lag_is_normal_but_middle_gap_is_not():
    from backend.services.engine.training import window_probe as wp

    center_dates = _bdays("2020-01-01", "2021-12-31")
    center = _window("local", center_dates)
    start, end = "2020-01-01", "2021-12-31"

    # 1) 只缺尾部（节点数据截止早几天）→ 纯滞后
    node_tail = _window("remote", center_dates[:-5], node_id="autodl-2")
    tail = wp.coverage_report(center, node_tail, start=start, end=end)
    assert tail["missing_days"] == 5
    assert tail["tail_lag_only"] is True
    assert tail["aligned"] is False

    # 2) 中间成段缺失 → 不是滞后，应被拦截
    node_gap = _window(
        "remote",
        [d for d in center_dates if not ("2021-01-04" <= d <= "2021-02-26")],
        node_id="autodl-2",
    )
    gap = wp.coverage_report(center, node_gap, start=start, end=end)
    assert gap["missing_days"] > 30
    assert gap["tail_lag_only"] is False
    assert len(gap["missing_segments"]) == 1
    seg = gap["missing_segments"][0]
    assert seg["start"] == "2021-01-04" and seg["end"] == "2021-02-26"
    assert gap["missing_ratio"] > 0.05

    # 3) 完全一致 → aligned
    same = wp.coverage_report(center, _window("local", center_dates), start=start, end=end)
    assert same["aligned"] is True and same["missing_days"] == 0


def test_segmentize_splits_far_apart_gaps():
    from backend.services.engine.training import window_probe as wp

    segs = wp.segmentize(["2022-10-25", "2022-10-26", "2023-01-30", "2023-01-31"])
    assert len(segs) == 2
    assert segs[0] == {"start": "2022-10-25", "end": "2022-10-26", "days": 2}
    assert segs[1]["start"] == "2023-01-30"


def test_window_dict_hides_dates_by_default():
    from backend.services.engine.training import window_probe as wp

    w = _window("remote", _bdays("2024-01-01", "2024-03-29"), node_id="autodl-2")
    plain = w.to_dict()
    assert "trading_dates" not in plain
    assert plain["trading_days"] == len(w.trading_dates)
    assert "trading_dates" in w.to_dict(with_dates=True)
