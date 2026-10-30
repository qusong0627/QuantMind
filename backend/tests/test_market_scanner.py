"""全市场扫描器 P1：准入过滤、截面打分与持仓高位提示（纯函数）。"""

from __future__ import annotations

import pytest

from backend.services.live_trading.services import market_scanner as ms


def _snap(
    *,
    last: float,
    pre: float,
    high: float | None = None,
    low: float | None = None,
    bid1: float | None = None,
    ask1: float | None = None,
    bv: float = 100,
    av: float = 100,
    amount: float = 50_000_000,
) -> dict:
    row = {
        "last": str(last),
        "pre_close": str(pre),
        "amount": str(amount),
        "bid1_v": str(bv),
        "ask1_v": str(av),
    }
    if high is not None:
        row["high"] = str(high)
    if low is not None:
        row["low"] = str(low)
    if bid1 is not None:
        row["bid1_p"] = str(bid1)
    if ask1 is not None:
        row["ask1_p"] = str(ask1)
    return row


def test_filters_missing_book_low_amount_and_near_limit_up() -> None:
    snaps = {
        "600036.SH": _snap(
            last=41.0, pre=40.0, high=41.2, low=39.8, bid1=40.99, ask1=41.01
        ),
        "000001.SZ": _snap(
            last=11.0, pre=11.0, high=11.1, low=10.9, ask1=11.01
        ),  # 缺 bid1
        "600000.SH": _snap(
            last=9.5, pre=9.4, high=9.6, low=9.3, bid1=9.49, ask1=9.51, amount=1_000_000
        ),  # 成交额不足
        "600519.SH": _snap(
            last=110.0, pre=100.0, high=110.0, low=101.0, bid1=109.9, ask1=110.0
        ),  # 涨 10%，接近涨停买不到
    }
    out = ms.score_universe(snaps)
    assert out["universe"] == 4
    assert out["eligible"] == 1
    assert [item["code"] for item in out["top"]] == ["600036.SH"]


def test_orders_by_cross_sectional_score() -> None:
    snaps = {
        "A.SH": _snap(
            last=10.5,
            pre=10.0,
            high=10.6,
            low=9.9,
            bid1=10.5,
            ask1=10.51,
            bv=900,
            av=100,
            amount=90_000_000,
        ),
        "B.SH": _snap(
            last=10.1,
            pre=10.0,
            high=10.2,
            low=9.9,
            bid1=10.1,
            ask1=10.11,
            bv=300,
            av=300,
            amount=60_000_000,
        ),
        "C.SH": _snap(
            last=9.5,
            pre=10.0,
            high=10.0,
            low=9.4,
            bid1=9.49,
            ask1=9.5,
            bv=100,
            av=900,
            amount=30_000_000,
        ),
    }
    out = ms.score_universe(snaps)
    assert [item["code"] for item in out["top"]][0] == "A.SH"
    assert [item["code"] for item in out["top"]][-1] == "C.SH"


def test_constant_universe_scores_zero() -> None:
    row = _snap(last=10.5, pre=10.0, high=10.6, low=9.9, bid1=10.5, ask1=10.51)
    out = ms.score_universe({"A.SH": dict(row), "B.SH": dict(row)})
    assert all(item["score"] == 0.0 for item in out["top"])


def test_top_n_cap() -> None:
    snaps = {
        f"60{i:04d}.SH": _snap(
            last=10 + i / 100, pre=10.0, high=10.2, low=9.9, bid1=10.0, ask1=10.01
        )
        for i in range(10)
    }
    out = ms.score_universe(snaps, top_n=3)
    assert len(out["top"]) == 3


def test_holdings_high_position_flag() -> None:
    snaps = {
        "600036.SH": _snap(
            last=10.9,
            pre=10.0,
            high=11.0,
            low=10.0,
            bid1=10.89,
            ask1=10.9,
            bv=100,
            av=900,
        ),
        "000001.SZ": _snap(
            last=11.0,
            pre=10.0,
            high=11.0,
            low=10.0,
            bid1=10.99,
            ask1=11.0,
            bv=900,
            av=100,
        ),
    }
    out = ms.score_universe(snaps, holdings={"600036.SH", "000001.SZ"})
    flagged = {item["code"] for item in out["holdings"]}
    assert "600036.SH" in flagged  # 日内高位 + 卖压
    assert "000001.SZ" not in flagged  # 买盘强


def test_row_features_rejects_bad_pre_close() -> None:
    assert ms._row_features({"last": "10", "pre_close": "0"}) is None
    assert ms._row_features({"last": "", "pre_close": "10"}) is None


@pytest.mark.parametrize(
    "now_hhmm,expected",
    [(1000, True), (1200, False), (1430, True), (1600, False)],
)
def test_session_active_windows(now_hhmm: int, expected: bool) -> None:
    import time as _time

    hour, minute = divmod(now_hhmm, 100)
    ts = _time.mktime((2026, 10, 30, hour, minute, 0, 0, 0, -1))  # 周五
    assert ms.session_active(ts) is expected


def test_excludes_st_and_delisting_names() -> None:
    snaps = {
        "600036.SH": _snap(
            last=41.0, pre=40.0, high=41.2, low=39.8, bid1=40.99, ask1=41.01
        ),
        "600289.SH": _snap(last=5.3, pre=5.0, high=5.4, low=5.0, bid1=5.29, ask1=5.3),
        "000003.SZ": _snap(last=2.2, pre=2.0, high=2.2, low=2.0, bid1=2.19, ask1=2.2),
    }
    out = ms.score_universe(
        snaps,
        names={"600036.SH": "招商银行", "600289.SH": "ST信通", "000003.SZ": "退市中测"},
    )
    assert [item["code"] for item in out["top"]] == ["600036.SH"]
