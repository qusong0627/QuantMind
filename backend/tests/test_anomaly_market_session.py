"""市场族时段闸（生产缺陷回归：盘外冻结快照被当实时异动报）。

**缺陷（2026-10-08 实测）**：`build_once` 的市场族在**任何时刻**都取数评测，而行情源
（公网免费行情服 / 桥源席，同一实例）在盘外仍留有「PreClose 已翻篇、Now 还是上一根」
的冻结快照——拿它评量价异动 = 把昨天的涨跌当今天实时报。实证（`qm_market_anomalies`）：

- 国庆假期 10-01~10-06 共 **287 条 price_surge 全部落在时段外**（00:00/08:09/20:21/23:57
  都在报），时段内 0 条；
- 09-25 的 48 条全在 08:11–08:17 盘前（前一日涨跌被重放），时段内 0 条。

**口径**：市场族只在 A 股连续竞价时段（工作日 09:30–11:30 / 13:00–15:00，Asia/Shanghai）
取数；数据/账户/模型族**不受影响**（它们本就该盘后跑：日线跳变、IC 骤降都在收盘后才有值）。
时钟走 `now_fn` 注入点，闸门判定与节流/冷却共用同一时钟。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

pytestmark = pytest.mark.unit

CST = ZoneInfo("Asia/Shanghai")
# 2026-10-08 是周四；10-10/10-11 是周末
_THU, _SAT, _SUN, _MON = 8, 10, 11, 12


def _at(day: int, h: int, m: int) -> datetime:
    return datetime(2026, 10, day, h, m, tzinfo=CST)


def _epoch(day: int, h: int, m: int) -> float:
    return _at(day, h, m).timestamp()


# ── 谓词边界 ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("day", "h", "m", "expected"),
    [
        (_THU, 0, 0, False),    # 半夜（实测 00:00:57 在报的形态）
        (_THU, 8, 15, False),   # 盘前（实测 08:11–08:17 在报的形态）
        (_THU, 9, 29, False),
        (_THU, 9, 30, True),    # 开盘（含端点）
        (_THU, 10, 30, True),
        (_THU, 11, 30, True),   # 上午收盘（含端点）
        (_THU, 11, 31, False),
        (_THU, 12, 59, False),
        (_THU, 13, 0, True),    # 下午开盘
        (_THU, 15, 0, True),    # 收盘（含端点）
        (_THU, 15, 1, False),
        (_THU, 20, 21, False),  # 晚上（实测 20:21 在报的形态）
        (_THU, 23, 57, False),  # 实测 23:57 在报的形态
    ],
)
def test_in_market_session_boundaries(day, h, m, expected):
    from backend.services.engine.anomaly_engine import in_market_session

    assert in_market_session(_at(day, h, m)) is expected


def test_weekend_is_never_a_session():
    from backend.services.engine.anomaly_engine import in_market_session

    assert in_market_session(_at(_SAT, 10, 30)) is False
    assert in_market_session(_at(_SUN, 14, 0)) is False
    assert in_market_session(_at(_MON, 10, 30)) is True


def test_session_windows_match_shared_cn_table():
    """防漂移：引擎时段常量必须等于 ``shared.market_sessions`` 的 CN AM/PM 窗口。

    平台时段表不止一份历史（真单闸 / 调度器 / 本模块），此处把本模块钉在共享表上——
    改共享表而忘了引擎，这条红。
    """
    from backend.services.engine.anomaly_engine import _SESSIONS
    from backend.shared.market_sessions import session_ranges_local

    cn = session_ranges_local("CN")
    got = [(s.strftime("%H:%M"), e.strftime("%H:%M")) for s, e in _SESSIONS]
    assert got == [cn["AM"], cn["PM"]], "引擎 _SESSIONS 与 shared CN 时段表漂移"


# ── 行为：盘外不取数、盘中照常、其他族不受影响 ──────────────────────


def _engine(now_epoch: float, **counters):
    from backend.services.engine.anomaly_engine import AnomalyConfig, AnomalyEngine

    calls = {"market": 0, "account": 0, "data": 0, "model": 0}

    def market_fetcher(cfg):
        calls["market"] += 1
        return {"600036.SH": {"price": 40.0, "pct_chg": 0.01, "now_volume": 100_000.0,
                              "avg_daily_volume": 1_000.0}}  # 巨量 → critical

    def counting(key, rows):
        def _f(cfg):
            calls[key] += 1
            return rows
        return _f

    engine = AnomalyEngine(
        config_loader=lambda: AnomalyConfig(enabled=True, volume_ratio_min=3.0),
        market_fetcher=market_fetcher,
        account_fetcher=counting("account", []),
        data_fetcher=counting("data", []),
        model_fetcher=counting("model", []),
        publisher=lambda d: None,
        recorder=lambda d: None,
        denier=lambda d: {},
        recent_marker=lambda ds: None,
        deduper=lambda ds, cfg: (list(ds), 0),
        status_writer=lambda payload: None,
        now_fn=lambda: now_epoch,
    )
    return engine, calls


def test_out_of_session_skips_market_fetch_entirely():
    """盘外（20:21）：市场族**取数都不取**，只记一笔 skipped_market_closed。"""
    engine, calls = _engine(_epoch(_THU, 20, 21))

    result = engine.build_once()

    assert calls["market"] == 0, "盘外不得取数（冻结快照的入口就在这里）"
    assert result["detections"] == 0
    assert engine.counters["skipped_market_closed"] == 1
    assert engine.counters["errors"] == 0, "这是正常路径，不是错误"


def test_in_session_market_fetch_runs_as_before():
    """盘中（10:30）：与改动前完全一致——取数、产检测、计数。"""
    engine, calls = _engine(_epoch(_THU, 10, 30))

    result = engine.build_once()

    assert calls["market"] == 1
    assert result["detections"] >= 1
    assert engine.counters["skipped_market_closed"] == 0


def test_off_session_still_runs_other_families():
    """盘外只闸市场族：数据（日线）/账户/模型（IC）族照常跑——它们本就该盘后出值。"""
    engine, calls = _engine(_epoch(_THU, 20, 21))

    engine.build_once()

    assert calls == {"market": 0, "account": 1, "data": 1, "model": 1}


def test_session_gate_uses_injected_clock_not_wall_clock():
    """闸门与节流共用注入时钟：同一引擎换个时钟就换判定（测试确定性的前提）。"""
    from backend.services.engine.anomaly_engine import AnomalyConfig, AnomalyEngine

    holder = {"now": _epoch(_THU, 20, 21), "market": 0}

    def market_fetcher(cfg):
        holder["market"] += 1
        return {}

    engine = AnomalyEngine(
        config_loader=lambda: AnomalyConfig(enabled=True),
        market_fetcher=market_fetcher,
        account_fetcher=lambda cfg: [],
        data_fetcher=lambda cfg: [],
        model_fetcher=lambda cfg: [],
        publisher=lambda d: None,
        recorder=lambda d: None,
        denier=lambda d: {},
        recent_marker=lambda ds: None,
        deduper=lambda ds, cfg: (list(ds), 0),
        status_writer=lambda payload: None,
        now_fn=lambda: holder["now"],
    )
    engine.build_once()
    assert holder["market"] == 0
    holder["now"] = _epoch(_THU, 10, 30)  # 同一个"今天"，只是到了盘中
    engine.build_once()
    assert holder["market"] == 1
