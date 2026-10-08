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

**节假日层（2026-10-08 评审补）**：工作日+时段内还要过一层交易日历（CN→XSHG）。原先
只判「工作日+时段」，工作日假期（如国庆 10-01，周四）在闸门眼里与交易日无异——同样的
冻结快照/停牌数据照报，critical 还会给真实账户写标的锁。日历走
``shared.trading_calendar.is_trading_day_xcal``（平台同一把尺子的同步出口），引擎按日
缓存；**答不了就退回旧口径（放行）**——本改动只许关闸、不许凭空开闸，故降级口径要与
旧行为逐条对拍（见 ``test_calendar_unavailable_degrades_to_old_behaviour``）。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

pytestmark = pytest.mark.unit

CST = ZoneInfo("Asia/Shanghai")
# 2026-10-08 是周四；10-10/10-11 是周末；10-01（周四）是国庆假期——真日历上的非交易日
_THU, _SAT, _SUN, _MON = 8, 10, 11, 12
_HOLIDAY_THU = 1


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


def test_session_state_holiday_vs_closed_vs_open():
    """三态的判据分层：时段外恒 closed（**不问日历**）；时段内才由日历分出 holiday。"""
    from backend.services.engine.anomaly_engine import market_session_state

    # 时段外 / 周末：无论日历说什么都是 closed（日历答不了也不影响结论）
    assert market_session_state(_at(_THU, 20, 21), trading_day=True) == "closed"
    assert market_session_state(_at(_THU, 12, 0), trading_day=False) == "closed"
    assert market_session_state(_at(_SAT, 10, 30), trading_day=True) == "closed"
    # 时段内：日历否证 = holiday；肯定/答不了 = open（降级放行）
    assert market_session_state(_at(_THU, 10, 30), trading_day=False) == "holiday"
    assert market_session_state(_at(_THU, 10, 30), trading_day=True) == "open"
    assert market_session_state(_at(_THU, 10, 30), trading_day=None) == "open"


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


def _engine(now_epoch: float, trading_day: bool | None = True):
    """时段闸用例的引擎。``trading_day`` 是注入的日历结论（默认「是交易日」= 旧行为）。

    日历一律注入：这一层的行为由 ``test_calendar_lookup_*`` 与接线用例单独钉；时段闸
    用例只关心「给定日历结论，取数闸怎么动」。
    """
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
        trading_day_lookup=lambda d: trading_day,
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
        trading_day_lookup=lambda d: True,
        now_fn=lambda: holder["now"],
    )
    engine.build_once()
    assert holder["market"] == 0
    holder["now"] = _epoch(_THU, 10, 30)  # 同一个"今天"，只是到了盘中
    engine.build_once()
    assert holder["market"] == 1


def test_volume_fraction_follows_injected_clock_not_wall_clock():
    """量比的时段进度（frac）必须取自注入时钟——墙钟会让阈值随真实时间漂移。

    回归靶子（2026-10-08 评审 [2]）：``build_once`` 曾把 ``trading_elapsed_fraction()``
    按默认参数调用（墙钟），于是闸门走注入时钟、分母走墙钟——本模块自己声明的「闸门判定
    与节流/冷却共用同一时钟」只成立了一半。夹具让两种时钟给出不同 frac（09:30→0.05、
    10:30→0.25），断言检测里记的就是注入值：bug 版两条都报同一个墙钟值，至少一条红。
    """
    from backend.services.engine.anomaly_engine import AnomalyConfig, AnomalyEngine

    holder = {"now": _epoch(_THU, 9, 30)}
    published: list = []

    engine = AnomalyEngine(
        # 现量 800 / 均量 1000：09:30 量比 16、10:30 量比 3.2，两档都过阈值 3.0 而
        # frac 不同——两边都产检测，才有东西可断言
        config_loader=lambda: AnomalyConfig(
            enabled=True, volume_ratio_min=3.0, deny_enabled=False
        ),
        market_fetcher=lambda cfg: {
            "600036.SH": {
                "price": 40.0,
                "pct_chg": 0.0,
                "now_volume": 800.0,
                "avg_daily_volume": 1000.0,
            }
        },
        account_fetcher=lambda cfg: [],
        data_fetcher=lambda cfg: [],
        model_fetcher=lambda cfg: [],
        publisher=published.append,  # build_once 不回流 Detection，只能从动作侧取
        recorder=lambda d: None,
        denier=lambda d: {},
        recent_marker=lambda ds: None,
        deduper=lambda ds, cfg: (list(ds), 0),  # 冷却走真 Redis；此处只要检测本身
        status_writer=lambda payload: None,
        trading_day_lookup=lambda d: True,
        now_fn=lambda: holder["now"],
    )

    engine.build_once()  # 09:30 → frac 0.05
    holder["now"] = _epoch(_THU, 10, 30)
    engine.build_once()  # 10:30 → frac 0.25

    fracs = [d.metrics["elapsed_fraction"] for d in published]
    assert fracs == [0.05, 0.25], (
        f"frac 跟墙钟走了（trading_elapsed_fraction 无参调用）；期望注入时钟 [0.05, 0.25]，"
        f"实得 {fracs}"
    )
    assert engine.counters["errors"] == 0


def test_market_fetch_heartbeat_counters():
    """取数心跳（2026-10-08 评审 [3]）：盘外引擎与「源挂了」的引擎计数长得一样，
    这两个时间戳是运维判「今天真的取过数吗」的现场。

    - 时段外：不取数，两个时间戳都不许动（否则「昨天取过」会装成「引擎活着」）；
    - 时段内：每次取数记 ``last_market_fetch_at``；**有有效报价**才记
      ``last_market_quote_at``（零报价 = 热集空/源挂，与「取了但没值」分开）。
    """
    from backend.services.engine.anomaly_engine import AnomalyConfig, AnomalyEngine

    quote = {"600036.SH": {"price": 40.0, "pct_chg": 0.0}}

    def _mk(now_epoch: float, fetcher):
        return AnomalyEngine(
            config_loader=lambda: AnomalyConfig(enabled=True),
            market_fetcher=fetcher,
            account_fetcher=lambda cfg: [],
            data_fetcher=lambda cfg: [],
            model_fetcher=lambda cfg: [],
            publisher=lambda d: None,
            recorder=lambda d: None,
            denier=lambda d: {},
            recent_marker=lambda ds: None,
            deduper=lambda ds, cfg: (list(ds), 0),
            status_writer=lambda payload: None,
            trading_day_lookup=lambda d: True,
            now_fn=lambda: now_epoch,
        )

    off = _mk(_epoch(_THU, 20, 21), lambda cfg: quote)
    off.build_once()
    assert off.counters["last_market_fetch_at"] is None, "时段外不得记取数心跳"
    assert off.counters["last_market_quote_at"] is None

    full = _mk(_epoch(_THU, 10, 30), lambda cfg: quote)
    full.build_once()
    assert full.counters["last_market_fetch_at"], "盘中取过数，必须留心跳"
    assert full.counters["last_market_quote_at"]

    empty = _mk(_epoch(_THU, 10, 30), lambda cfg: {})
    empty.build_once()
    assert empty.counters["last_market_fetch_at"], "取数发生了（只是没取到值）"
    assert empty.counters["last_market_quote_at"] is None, "零报价不得记『取到过数』"


# ── 节假日层：日历结论如何改变取数闸 ────────────────────────────────


def test_holiday_in_session_skips_market_family():
    """工作日假期盘中（日历明示非交易日）：市场族**取数都不取**，记 holiday 而非 closed。

    这正是评审指出的缺口：改前闸门只认「工作日+时段」，假期盘中与交易日无异——
    冻结快照照报，critical 还按真实标的写锁。
    """
    engine, calls = _engine(_epoch(_THU, 10, 30), trading_day=False)

    result = engine.build_once()

    assert calls["market"] == 0, "非交易日盘中不得取数"
    assert result["detections"] == 0
    assert engine.counters["skipped_market_holiday"] == 1
    assert engine.counters["skipped_market_closed"] == 0, "假日与盘外必须分开计数"
    assert engine.counters["errors"] == 0, "这是正常路径，不是错误"


def test_holiday_only_gates_market_family():
    """假日只闸市场族：数据/账户/模型族照常——它们本就该盘后（含假期）出值。"""
    engine, calls = _engine(_epoch(_THU, 10, 30), trading_day=False)

    engine.build_once()

    assert calls == {"market": 0, "account": 1, "data": 1, "model": 1}


def test_calendar_unavailable_degrades_to_old_behaviour():
    """日历答不了（None）→ **退回旧口径**：时段内照常取数，不动计数。

    降级必须放行而不是关闸：本改动只许关闸、不许凭空开闸。关闸式降级（未知即跳）
    会让 XSHG 印发期（实测到 2026-12-31）一过，市场族在每个交易日静默停摆。
    """
    engine, calls = _engine(_epoch(_THU, 10, 30), trading_day=None)

    result = engine.build_once()

    assert calls["market"] == 1, "日历答不了不是停摆的理由"
    assert result["detections"] >= 1
    assert engine.counters["skipped_market_holiday"] == 0
    assert engine.counters["skipped_market_closed"] == 0


def test_calendar_lookup_is_per_day_cached_and_skipped_off_window():
    """日历按日缓存、且**时段外不问**：日频事实不该在分钟级循环里每分钟查一次。"""
    from backend.services.engine.anomaly_engine import AnomalyConfig, AnomalyEngine

    asked: list[str] = []
    holder = {"now": _epoch(_THU, 20, 21)}  # 从盘外起步：第一轮就不该问日历

    engine = AnomalyEngine(
        config_loader=lambda: AnomalyConfig(enabled=True),
        market_fetcher=lambda cfg: {},
        account_fetcher=lambda cfg: [],
        data_fetcher=lambda cfg: [],
        model_fetcher=lambda cfg: [],
        publisher=lambda d: None,
        recorder=lambda d: None,
        denier=lambda d: {},
        recent_marker=lambda ds: None,
        deduper=lambda ds, cfg: (list(ds), 0),
        status_writer=lambda payload: None,
        trading_day_lookup=lambda d: (asked.append(d.isoformat()), True)[1],
        now_fn=lambda: holder["now"],
    )

    engine.build_once()  # 20:21 外 → 不查
    assert asked == []
    holder["now"] = _epoch(_THU, 10, 30)
    engine.build_once()  # 盘中 → 查一次
    holder["now"] = _epoch(_THU, 14, 0)
    engine.build_once()  # 同日再查 → 命中缓存
    holder["now"] = _epoch(_MON, 10, 30)
    engine.build_once()  # 隔日 → 再查一次
    assert asked == ["2026-10-08", "2026-10-12"]


def test_calendar_lookup_exception_degrades_instead_of_crashing():
    """日历查询抛异常 = 答不了：**降级放行** + 记一条 WARNING，不许反噬主循环。

    本用例同时钉住 except 分支的**方向**（2026-10-08 评审 M1）：只断言「没崩、没记错」
    对放行与关闸**都成立**——把 except 里的 ``verdict = None`` 改成 ``False``（异常即
    当假期、关闸），旧断言照样全绿。而这一支正是 XSHG 印发期过后生产会走的路径，
    「只许关闸、不许凭空开闸」的降级政策就压在这里，必须用取数次数钉死方向。
    """
    from backend.services.engine.anomaly_engine import AnomalyConfig, AnomalyEngine

    def boom(day):
        raise RuntimeError("日历炸了")

    calls = {"market": 0}

    def market_fetcher(cfg):
        calls["market"] += 1
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
        trading_day_lookup=boom,
        now_fn=lambda: _epoch(_THU, 10, 30),
    )

    result = engine.build_once()

    assert result["enabled"] is True and engine.counters["errors"] == 0
    assert calls["market"] == 1, "查历异常必须降级放行——异常不是关闸的理由"
    assert engine.counters["skipped_market_holiday"] == 0, "异常 ≠ 假期（不许记成 holiday）"
    assert engine.counters["skipped_market_closed"] == 0


def test_default_lookup_is_wired_to_shared_calendar():
    """接线：不注入 lookup 时，引擎用的是**平台那把尺子**（shared xcal），不是「恒 True」。

    探针钉在 2026-10-01（周四·国庆，XSHG 印发区间内）：真日历说它不是交易日 ⇒ 市场族
    必须被跳过。把默认值改回 ``lambda d: True``、或把共享实现接错市场，这条立刻红
    ——时段闸自己的用例全是注入桩，没人钉接线就会「测试全绿而生产永远不关闸」。
    """
    from backend.services.engine.anomaly_engine import (
        AnomalyConfig,
        AnomalyEngine,
        _default_trading_day_lookup,
    )
    from backend.shared.trading_calendar import is_trading_day_xcal
    from datetime import date as _date

    assert _default_trading_day_lookup(_date(2026, 10, 1)) is False, (
        "共享尺子本身要答对"
    )
    assert is_trading_day_xcal("CN", _date(2026, 10, 1)) is False, "CN 走 XSHG"

    calls = {"market": 0}

    def market_fetcher(cfg):
        calls["market"] += 1
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
        now_fn=lambda: _epoch(_HOLIDAY_THU, 10, 30),
    )

    engine.build_once()

    assert calls["market"] == 0, "默认接线没接上共享日历（否则国庆盘中照样取数）"
    assert engine.counters["skipped_market_holiday"] == 1
