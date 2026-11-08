"""build_calendar 裁剪预发布未来交易日：日历末日期不得超出真实行情覆盖。

背景：QuantDB trading_calendar 是交易所按全年预先发布的（含未来交易日），
旧实现原样写入 day.txt，导致“日历范围”显示 2026-12-31，而 features 实际
只到最新 daily_forward 分区日（如 2026-11-06），管理台/回测/推理误判覆盖。
"""

from pathlib import Path

import pandas as pd
import pytest

from backend.services.engine.qlib_data_builder import QlibDataBuilder


class _StubHub:
    def __init__(self, data_dir: Path, calendar_df: pd.DataFrame) -> None:
        self.data_dir = data_dir
        self.available = True
        self._calendar_df = calendar_df

    def fetch_calendar(self) -> pd.DataFrame:
        return self._calendar_df


def _make_daily_forward(data_dir: Path, partition_days: list[str]) -> None:
    fwd = data_dir / "1_kline_data" / "daily_forward"
    for d in partition_days:
        (fwd / f"dt={d}").mkdir(parents=True, exist_ok=True)
        # 不放 data.parquet：_dates_from_recent_parquet 读不到文件会安全返回 []，
        # 本测试只关心 dt= 目录名推导出的覆盖上限。


def _year_ahead_calendar(start: str = "2016-01-04", end: str = "2026-12-31") -> pd.DataFrame:
    days = pd.bdate_range(start, end)
    return pd.DataFrame({"TradingDate": days, "IsTradingDay": 1})


def _build(tmp_path: Path, calendar_df: pd.DataFrame) -> list[str]:
    qlib_dir = tmp_path / "qlib"
    builder = QlibDataBuilder(_StubHub(tmp_path / "quantdb", calendar_df), qlib_dir=qlib_dir)
    n = builder.build_calendar()
    lines = [x.strip() for x in (qlib_dir / "calendars" / "day.txt").read_text().splitlines() if x.strip()]
    assert n == len(lines)
    return lines


def test_calendar_clipped_to_latest_partition(tmp_path: Path):
    (tmp_path / "quantdb").mkdir(parents=True)
    _make_daily_forward(tmp_path / "quantdb", ["20261103", "20261104", "20261105", "20261106"])

    lines = _build(tmp_path, _year_ahead_calendar())

    assert lines[-1] == "2026-11-06"  # 未来交易日（2026-12-31 前）全部裁掉
    assert all(x <= "2026-11-06" for x in lines)
    assert lines[0] == "2016-01-04"


def test_calendar_kept_when_upstream_calendar_lags(tmp_path: Path):
    """日历发布滞后于行情分区时（原有补齐逻辑），不得因裁剪丢行情日期。"""
    (tmp_path / "quantdb").mkdir(parents=True)
    _make_daily_forward(tmp_path / "quantdb", ["20261104", "20261105", "20261106"])

    # 日历只发到 11-04：<= cap(11-06)，不触发裁剪，保持全量
    days = pd.bdate_range("2026-01-05", "2026-11-04")
    lines = _build(tmp_path, pd.DataFrame({"TradingDate": days, "IsTradingDay": 1}))
    assert lines[-1] == "2026-11-04"


def test_no_clip_without_parquet_partitions(tmp_path: Path):
    """行情尚未落地（首次构建）时保持旧行为：不裁剪。"""
    (tmp_path / "quantdb").mkdir(parents=True)
    lines = _build(tmp_path, _year_ahead_calendar(start="2026-10-01"))
    assert lines[-1] == "2026-12-31"


if __name__ == "__main__":
    pytest.main([__file__])
