"""实盘选股的 ST 判据口径（`selection._st_for` / `_load_st_snapshot`）。

背景：PG 侧 `stock_daily_latest.is_st` 实测 `count(is_st)=0`（1085 万行全 NULL），
`stock_daily_new_*` 同样 —— 既有代码 `float(p["is_st"]) == 1` 恒假，于是
`exclude_st=True` 一只都排不掉，而 meta 照样写 `true`。改后 ST 取自 QuantDB
`instrument_detail.IsSTGP`，但那是**单一静态快照**，所以必须卡住两件事：

1. **实盘窗口内才给真值**，历史日一律空集（拿今天的名单过滤历史 = 前视偏差）；
2. **快照读不到时如实返回空**，不能假装「全市场非 ST」——那会把 ST 股放进来。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from backend.services.engine.routers import selection as sel


@pytest.fixture(autouse=True)
def _clear_st_cache():
    """每个用例都从干净缓存起跑（快照带 TTL 缓存，会串味）。"""
    sel._st_cache.update({"map": None, "hqdate": "", "ts": 0.0})
    yield
    sel._st_cache.update({"map": None, "hqdate": "", "ts": 0.0})


def _iso(offset_days: int) -> str:
    return (date.today() + timedelta(days=offset_days)).isoformat()


def _fake_snapshot(
    monkeypatch, mapping: dict[str, int], hqdate: str = "20260918"
) -> None:
    monkeypatch.setattr(sel, "_load_st_snapshot", lambda: (mapping, hqdate))


# ---------------------------------------------------------------- 窗口纪律


def test_historical_date_gets_no_st_flags(monkeypatch) -> None:
    """历史交易日**不得**套用静态快照——否则就是把今天的 ST 名单当成 60 天前的。"""
    # Arrange
    _fake_snapshot(monkeypatch, {"600036.SH": 1})

    # Act
    flags, source = sel._st_for(_iso(-60), {"600036.SH"})

    # Assert
    assert flags == {}
    assert "前视" in source or "历史" in source


def test_live_window_gets_real_flags(monkeypatch) -> None:
    """正对照：落在实盘窗口内的交易日必须拿到真值（防上一条只是恒真）。"""
    # Arrange
    _fake_snapshot(monkeypatch, {"600036.SH": 1, "600519.SH": 0})

    # Act
    flags, source = sel._st_for(_iso(0), {"600036.SH", "600519.SH"})

    # Assert
    assert flags == {"600036.SH": 1, "600519.SH": 0}
    assert "IsSTGP" in source


def test_window_boundary_is_inclusive(monkeypatch) -> None:
    """窗口边界当日本身仍算实盘（`> window` 才拒），否则周末/长假会掉出窗口。"""
    # Arrange
    _fake_snapshot(monkeypatch, {"600036.SH": 1})

    # Act
    inside, _ = sel._st_for(_iso(-sel._ST_LIVE_WINDOW_DAYS), {"600036.SH"})
    outside, _ = sel._st_for(_iso(-sel._ST_LIVE_WINDOW_DAYS - 1), {"600036.SH"})

    # Assert
    assert inside == {"600036.SH": 1}
    assert outside == {}


def test_future_date_is_rejected(monkeypatch) -> None:
    """未来日期不套用（信号日可能是 T+1，跨过一个交易日仍应给真值，跨太多则拒）。"""
    # Arrange
    _fake_snapshot(monkeypatch, {"600036.SH": 1})

    # Act
    tomorrow, _ = sel._st_for(_iso(1), {"600036.SH"})
    far_future, _ = sel._st_for(_iso(30), {"600036.SH"})

    # Assert
    assert tomorrow == {"600036.SH": 1}
    assert far_future == {}


# ---------------------------------------------------------------- 快照缺失


def test_missing_snapshot_yields_empty_not_all_clear(monkeypatch) -> None:
    """快照读不到 → 空集 + 说明；**不能**返回全 0 冒充「全市场非 ST」。"""
    # Arrange
    monkeypatch.setattr(sel, "_load_st_snapshot", lambda: ({}, ""))

    # Act
    flags, source = sel._st_for(_iso(0), {"600036.SH"})

    # Assert
    assert flags == {}
    assert source == "ST 快照不可用"


def test_unparsable_trade_date_is_safe() -> None:
    """空/非法交易日不得抛异常（调用方在请求路径上）。"""
    # Act / Assert
    assert sel._st_for("", {"600036.SH"}) == ({}, "交易日不可解析")
    assert sel._st_for("not-a-date", {"600036.SH"}) == ({}, "交易日不可解析")


# ---------------------------------------------------------------- 快照读取


def test_snapshot_reads_isstgp_into_suffix_map(monkeypatch) -> None:
    """`IsSTGP` 的 "1"/"0"/空 都要落到 {symbol: 0|1}，且只收 ST 的那些。"""
    # Arrange
    import pandas as pd

    frame = pd.DataFrame(
        {
            "symbol": ["600036.SH", "600519.SH", "000001.SZ", "300750.SZ"],
            "IsSTGP": ["1", "0", None, "1"],
            "HqDate": ["20260918"] * 4,
        }
    )
    from backend.services.engine.data_platform import quantdb_hub

    monkeypatch.setattr(
        quantdb_hub.QuantDBDataHub, "fetch_stock_list", lambda self: frame, raising=True
    )

    # Act
    mapping, hqdate = sel._load_st_snapshot()

    # Assert
    assert mapping == {"600036.SH": 1, "300750.SZ": 1}
    assert hqdate == "20260918"


def test_snapshot_failure_is_swallowed(monkeypatch) -> None:
    """读取抛异常 → 空 dict（选股不能因为 ST 证据位挂掉）。"""
    # Arrange
    from backend.services.engine.data_platform import quantdb_hub

    def _boom(self):
        raise RuntimeError("parquet 损坏")

    monkeypatch.setattr(
        quantdb_hub.QuantDBDataHub, "fetch_stock_list", _boom, raising=True
    )

    # Act
    mapping, hqdate = sel._load_st_snapshot()

    # Assert
    assert mapping == {}
    assert hqdate == ""


def test_source_hint_never_returns_empty_string() -> None:
    """口径说明为空也要给个兜底串（meta 里出现空串就等于没交代）。"""
    # Act / Assert
    assert sel.st_source_hint("") == "交易日不可解析"
    assert sel.st_source_hint(_iso(-99)) != ""
