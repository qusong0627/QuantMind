"""盘中结构/量能纯计算单测（不连 Redis/桥）。"""

from __future__ import annotations

from datetime import date

from backend.shared.intraday_market_calc import (
    compute_breadth,
    compute_metrics,
    compute_sector_strength,
    count_consecutive_boards,
    depth_levels,
    detect_events,
    qmt_tick_to_snapshot,
)

DAY = date(2026, 11, 16)


def _limits(_symbol: str, pre: float, **_kwargs):
    return (round(pre * 1.1, 2), round(pre * 0.9, 2))


def _row(last: str, now: str, zaf: str) -> dict:
    return {"LastClose": last, "Now": now, "Zaf": zaf, "Volume": "1"}


def _rows():
    return {
        "600000.SH": _row("10.00", "10.20", "2.00"),
        "600001.SH": _row("10.00", "9.80", "-2.00"),
        "600002.SH": _row("10.00", "10.00", "0.00"),
        # 封住涨停（10% 板）
        "600003.SH": _row("10.00", "11.00", "10.00"),
        # 曾触板、当前回落 → 炸板
        "600004.SH": _row("10.00", "10.60", "6.00"),
        # 跌停
        "600005.SH": _row("10.00", "9.00", "-10.00"),
        # 无昨收 → 剔除
        "600006.SH": _row("0", "9.00", "0"),
    }


def test_compute_breadth_counts_limit_up_and_broken():
    doc, seen_max, seen_min = compute_breadth(
        _rows(),
        {"600003.SH": "涨停股", "600004.SH": "炸板股"},
        DAY,
        seen_max={"600004.SH": 11.0},
        limits_fn=_limits,
    )
    assert doc["n"] == 6
    assert doc["up"] == 3 and doc["down"] == 2 and doc["flat"] == 1
    assert doc["limit_up"] == 1 and doc["limit_down"] == 1
    assert doc["broke"] == 1
    assert doc["broke_rate"] == 50.0
    assert doc["limit_up_codes"] == ["600003.SH"]
    assert doc["broke_codes"] == ["600004.SH"]
    assert doc["top_limit_up"][0]["code"] == "600003.SH"
    assert doc["broke_sampled"] is True
    assert seen_max["600004.SH"] == 11.0


def test_compute_breadth_does_not_mutate_input_extremes():
    source = {"600004.SH": 11.0}
    compute_breadth(_rows(), {}, DAY, seen_max=source, limits_fn=_limits)
    assert source == {"600004.SH": 11.0}


def test_detect_events_only_reports_transitions_and_skips_first_cycle():
    prev = {
        "limit_up_codes": ["600003.SH"],
        "broke_codes": [],
        "limit_down_codes": [],
        "opened_down_codes": [],
    }
    cur = {
        "limit_up_codes": ["600003.SH", "600007.SH"],
        "broke_codes": ["600004.SH"],
        "limit_down_codes": [],
        "opened_down_codes": [],
    }
    names = {"600007.SH": "新涨停", "600004.SH": "炸板股"}
    events = detect_events(prev, cur, 1000.0, names)
    kinds = {(e["code"], e["kind"]) for e in events}
    assert kinds == {("600007.SH", "limit_up"), ("600004.SH", "broke")}
    assert "新涨停(600007.SH) 涨停" in events[0]["text"] or any(
        "新涨停(600007.SH) 涨停" in e["text"] for e in events
    )
    assert detect_events(None, cur, 1000.0, names) == []


def test_depth_levels_detects_one_and_five():
    assert depth_levels({}) == 0
    assert depth_levels({"bid1": "10.0", "ask1": "10.1"}) == 1
    snap = {}
    for i in range(1, 6):
        snap[f"bid{i}"] = str(10 - i * 0.01)
        snap[f"ask{i}"] = str(10 + i * 0.01)
    assert depth_levels(snap) == 5


def test_compute_metrics_maps_more_info_and_snapshot():
    more_info = {
        "600036.SH": {
            "fLianB": "1.12",
            "vzangsu": "0.03",
            "Zjl": "17842.75",
            "Wtb": "12.0",
            "OpenZAF": "0.39",
            "L2TicNum": "81156",
            "TotalBVol": "47421",
        }
    }
    snaps = {
        "600036.SH": {
            "Now": "43.20",
            "High": "43.47",
            "Low": "42.85",
            "Amount": "202321.47",
            "bid1": "43.19",
            "ask1": "43.21",
            "timestamp": "1000",
        }
    }
    out = compute_metrics(["SH600036"], {"600036.SH": "招商银行"}, more_info, snaps)
    row = out["SH600036"]
    assert row["name"] == "招商银行"
    assert row["vr"] == 1.12
    assert row["zjl"] == 17842.75
    assert row["depth_levels"] == 1
    assert round(row["pos"], 4) == round((43.20 - 42.85) / (43.47 - 42.85), 4)
    assert "f_hsl" not in row  # 缺失字段不写假值


def test_compute_sector_strength_aggregates_and_filters_small_sectors():
    rows = {
        "600000.SH": {"Zaf": "2.0"},
        "600001.SH": {"Zaf": "1.0"},
        "600002.SH": {"Zaf": "11.0"},
        "600003.SH": {"Zaf": "-2.0"},
    }
    memberships = {
        "强板块": ["600000", "600001", "600002"],
        "弱板块": ["600003"],
        "小板块": ["600000"],
    }
    out = compute_sector_strength(rows, memberships, ["600002.SH"], min_members=3)
    assert [row["name"] for row in out] == ["强板块"]
    assert out[0]["chg"] == round((2 + 1 + 11) / 3, 2)
    assert out[0]["limit_up"] == 1


def test_count_consecutive_boards_three_prior_limit_ups_is_four():
    bars = [
        {"Date": "20261110", "Close": "10.00"},
        {"Date": "20261111", "Close": "11.00"},
        {"Date": "20261112", "Close": "12.10"},
        {"Date": "20261113", "Close": "13.31"},
        {"Date": "20261116", "Close": "14.64"},  # 今日，应剔除
    ]

    def threshold(_symbol, *, is_st, trade_date):
        return 0.095

    boards = count_consecutive_boards(
        bars, "600000.SH", False, DAY, threshold_fn=threshold
    )
    assert boards == 4
    assert (
        count_consecutive_boards(
            bars[:1], "600000.SH", False, DAY, threshold_fn=threshold
        )
        is None
    )


def test_count_consecutive_boards_first_board_when_yesterday_not_limit_up():
    bars = [
        {"Date": "20261112", "Close": "10.00"},
        {"Date": "20261113", "Close": "10.50"},
        {"Date": "20261116", "Close": "11.55"},
    ]

    def threshold(_symbol, *, is_st, trade_date):
        return 0.095

    boards = count_consecutive_boards(
        bars, "600000.SH", False, DAY, threshold_fn=threshold
    )
    assert boards == 1


def test_qmt_tick_to_snapshot_converts_units_and_keeps_five_levels():
    tick = {
        "time": 1794808218000,
        "lastPrice": 43.27,
        "open": 42.9,
        "high": 43.47,
        "low": 42.85,
        "lastClose": 42.93,
        "amount": 2_469_028_000.0,
        "volume": 570_985,
        "askPrice": [43.28, 43.29, 43.30, 43.31, 43.32],
        "bidPrice": [43.27, 43.26, 43.25, 43.24, 43.23],
        "askVol": [83, 464, 1411, 110, 231],
        "bidVol": [12, 1158, 148, 793, 51],
    }
    snap = qmt_tick_to_snapshot(tick, "SH600036")
    assert snap["Now"] == 43.27
    assert snap["PreClose"] == 42.93
    assert snap["Amount"] == 246_902.8
    assert snap["timestamp"] == 1794808218
    assert snap["source"] == "qmt_full_tick"
    assert snap["symbol"] == "SH600036"
    assert snap["bid5"] == 43.23 and snap["ask_vol5"] == 231
    assert depth_levels(snap) == 5


def test_qmt_tick_without_price_is_not_fabricated():
    assert qmt_tick_to_snapshot({"lastPrice": 0, "lastClose": 10}, "SH600036") == {}
    assert qmt_tick_to_snapshot(None, "SH600036") == {}
