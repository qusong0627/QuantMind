from datetime import date

from backend.shared.inference_coverage import find_inference_gap_dates


def test_find_inference_gap_dates_includes_historical_holes(monkeypatch):
    """最大日期已存在时，仍应找出中间漏跑的交易日。"""
    monkeypatch.setattr(
        "backend.shared.inference_coverage.trading_dates_between",
        lambda _start, _end: [
            "2026-08-24",
            "2026-08-25",
            "2026-08-26",
            "2026-08-27",
            "2026-08-28",
        ],
    )

    gaps = find_inference_gap_dates(["2026-08-24", "2026-08-28"], date(2026, 8, 28))

    assert gaps == ["2026-08-25", "2026-08-26", "2026-08-27"]


def test_find_inference_gap_dates_includes_tail_and_excludes_non_trading_days(
    monkeypatch,
):
    monkeypatch.setattr(
        "backend.shared.inference_coverage.trading_dates_between",
        lambda _start, _end: ["2026-08-24", "2026-08-25", "2026-08-28", "2026-08-31"],
    )

    gaps = find_inference_gap_dates(["2026-08-24", "2026-08-28"], "2026-08-31")

    assert gaps == ["2026-08-25", "2026-08-31"]
