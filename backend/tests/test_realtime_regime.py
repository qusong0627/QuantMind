"""T-P6-13 市场状态测试：口径金样（共享 vs pandas 原式）/形成输入终值收敛/服务真链路。

覆盖：
1. U：classify_regime 四分支 + NaN 金样；
2. U：build_state_series 与**日频原式**（pandas pct_change/rolling）逐值一致 + 标注位移守卫；
3. U（核心验收）：forming_inputs(历史 + live 终值) == pandas 日频公式在当日行的三输入（ε）；
4. U：量比失真（量纲可疑）→ vratio 弃用 + notes 如实；
5. I：服务 build_once（假数据源）→ 真 Redis 快照 + 总线事件落账 → 清理；未启用/无 live 路径；
6. G：日频服务委托共享实现（不得保留第二份分级）。
"""

from __future__ import annotations

import json
import uuid

import numpy as np
import pandas as pd
import pytest


def _synth(n: int = 60, seed: int = 7) -> tuple[list[float], list[float], list[str]]:
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.001, 0.012, n)
    closes = list(np.round(4000 * np.cumprod(1 + rets), 2))
    volumes = list(np.round(rng.uniform(0.8, 1.4, n) * 1e8, 0))
    dates = [d.strftime("%Y-%m-%d") for d in pd.date_range("2026-05-01", periods=n, freq="B")]
    return closes, volumes, dates


def _pandas_daily_inputs(closes: list[float], volumes: list[float], window: int):
    """日频原式（pandas）三输入行 i 的参考实现（迁移前逻辑的等价复刻）。"""
    c = pd.Series(closes, dtype=float)
    v = pd.Series(volumes, dtype=float)
    roll_ret = c / c.shift(window) - 1.0
    roll_vol = c.pct_change().rolling(window).std()
    vratio = v / v.rolling(window).mean()
    return roll_ret, roll_vol, vratio


# ── 1. 分级金样 ─────────────────────────────────────────────────────


@pytest.mark.unit
def test_classify_regime_branches():
    from backend.shared.market_regime import classify_regime

    th = {"ret_up": 0.02, "ret_down": -0.02, "vol_high": 0.03, "volume_ratio_high": 1.2}
    assert classify_regime(0.03, 0.01, 0.5, th) == "bull"      # ret/vol 分支
    assert classify_regime(-0.03, 0.05, 0.5, th) == "bear"     # 熊式
    assert classify_regime(0.001, 0.05, 1.5, th) == "bull"     # vratio 分支（ret≥0）
    assert classify_regime(-0.005, 0.05, 1.5, th) == "neutral" # vratio 高但 ret<0
    assert classify_regime(0.0, 0.01, None, th) == "neutral"   # 回落
    assert classify_regime(float("nan"), 0.01, 1.0, th) == "neutral"
    assert classify_regime(0.03, float("nan"), 1.0, th) == "neutral"
    assert classify_regime(None, 0.01, 1.0, th) == "neutral"


# ── 2. 序列构造 = 日频原式 ──────────────────────────────────────────


@pytest.mark.unit
def test_build_state_series_matches_pandas_daily_formula():
    from backend.shared.market_regime import build_state_series, classify_regime

    closes, volumes, dates = _synth()
    window = 20
    got = build_state_series(closes, volumes, dates, window=window)

    roll_ret, roll_vol, vratio = _pandas_daily_inputs(closes, volumes, window)
    expected = {}
    for i in range(window, len(closes) - 1):  # 原式：row i 的状态标注到 dates[i+1]
        expected[dates[i + 1]] = classify_regime(
            float(roll_ret.iloc[i]), float(roll_vol.iloc[i]), float(vratio.iloc[i])
        )
    assert got == expected
    assert len(got) == len(closes) - 1 - window
    # 标注位移守卫：首条目必须挂在 dates[window+1]（而非 dates[window]）
    assert dates[window + 1] in got and dates[window] not in got


@pytest.mark.unit
def test_build_state_series_window_guard():
    from backend.shared.market_regime import build_state_series

    closes, volumes, dates = _synth(20)
    assert build_state_series(closes, volumes, dates, window=20) == {}


# ── 3. 核心验收：live 终值收敛 == 日频当日行 ────────────────────────


@pytest.mark.unit
def test_forming_inputs_converges_to_daily_row():
    """15:00 终值（live=当日收盘/量）→ 三输入与 pandas 日频当日行逐值一致（ε）。"""
    from backend.shared.market_regime import forming_inputs

    closes, volumes, _ = _synth()
    window = 20
    D = len(closes) - 1  # 当日 = 序列末行（live 即其终值）
    forming = forming_inputs(closes[:-1], volumes[:-1], closes[D], volumes[D], window)
    roll_ret, roll_vol, vratio = _pandas_daily_inputs(closes, volumes, window)
    assert forming["ok"] is True
    assert forming["ret"] == pytest.approx(float(roll_ret.iloc[D]), rel=1e-9)
    assert forming["vol"] == pytest.approx(float(roll_vol.iloc[D]), rel=1e-9)
    assert forming["vratio"] == pytest.approx(float(vratio.iloc[D]), rel=1e-9)
    assert forming["notes"] == []
    # 状态也随之逐值一致（唯一分级实现，两端同式）
    from backend.shared.market_regime import classify_regime

    assert classify_regime(forming["ret"], forming["vol"], forming["vratio"]) == classify_regime(
        float(roll_ret.iloc[D]), float(roll_vol.iloc[D]), float(vratio.iloc[D])
    )


@pytest.mark.unit
def test_forming_inputs_implausible_volume_flagged():
    from backend.shared.market_regime import forming_inputs

    closes, volumes, _ = _synth(40)
    # 量纲差 100 倍（如手 vs 股混用）→ vratio 弃用 + notes 点名
    out = forming_inputs(closes[:-1], volumes[:-1], closes[-1], volumes[-1] * 100, 20)
    assert out["vratio"] is None
    assert any("implausible" in n for n in out["notes"])
    # 历史不足
    out2 = forming_inputs(closes[:5], volumes[:5], closes[5], volumes[5], 20)
    assert out2["ok"] is False and "history_short" in out2["notes"]
    # 无 live
    out3 = forming_inputs(closes[:-1], volumes[:-1], None, None, 20)
    assert out3["ok"] is False and "no_live_close" in out3["notes"]


# ── 4. 服务真链路（假数据源 → 真 Redis 快照 + 总线）──────────────────


@pytest.mark.integration
def test_regime_service_publishes_snapshot_and_event():
    import redis as redis_lib

    from backend.services.engine.realtime_regime import RegimeConfig, RealtimeRegimeService
    from backend.shared import intel_events as ie
    from backend.shared.remote_quote_config import resolve_remote_quote_redis

    # 总线用主 Redis（与默认发布一致）；测试独立消费组读回
    main = redis_lib.Redis(host="redis", port=6379, db=0, decode_responses=True, socket_timeout=5)
    try:
        main.ping()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Redis 不可达: {exc}")

    closes, volumes, dates = _synth()
    cfg = RegimeConfig(enabled=True, cadence_s=60, window=20)
    svc = RealtimeRegimeService(
        config_loader=lambda: cfg,
        history_loader=lambda sym: {"dates": dates, "closes": closes, "volumes": volumes},
        index_snapshot_fetcher=lambda sym: {
            "close": closes[-1], "volume": volumes[-1], "ts": 1.0,
        },
        breadth_fetcher=lambda: {"coverage": "hot_set", "sample": 3, "up": 2, "down": 1,
                                 "flat": 0, "limit_up": 0, "limit_down": 0},
    )
    try:
        payload = svc.build_once()
        assert payload is not None and payload["state"] in ("bull", "neutral", "bear")
        snap = main.hgetall("qm:regime:intraday")
        assert snap and snap["state"] == payload["state"] and snap["index"] == "000300.SH"
        assert json.loads(snap["breadth"])["coverage"] == "hot_set"
        # 总线事件可被消费（独立组从头读，按 type=regime 过滤取最新一条）
        group = f"regime-test-{uuid.uuid4().hex[:8]}"
        main.xgroup_create(ie.STREAM_KEY, group, id="0", mkstream=True)
        got = ie.read_events(main, group=group, consumer="t", block_ms=200, count=500)
        events = [ev for _id, ev in got if isinstance(ev, dict) and ev.get("type") == "regime"]
        assert events and events[-1]["source"] == "realtime_regime"
        assert events[-1]["payload"]["state"] == payload["state"]
        for msg_id, _ev in got:
            ie.ack_event(main, msg_id, group=group)
    finally:
        main.delete("qm:regime:intraday")
        main.close()

    # 未启用 → None；无 live → None + 计数
    off = RealtimeRegimeService(config_loader=lambda: RegimeConfig(enabled=False))
    assert off.build_once() is None
    no_live = RealtimeRegimeService(
        config_loader=lambda: cfg,
        history_loader=lambda sym: {"dates": dates, "closes": closes, "volumes": volumes},
        index_snapshot_fetcher=lambda sym: None,
        publisher=lambda p: None,
    )
    assert no_live.build_once() is None
    assert no_live.counters["skipped_no_live"] == 1


# ── 5. 守卫：日频服务委托共享 ───────────────────────────────────────


@pytest.mark.integration
def test_default_history_loader_real_quantdb():
    """默认历史加载器真库冒烟（注入式测试的盲区——日期口径 bug 曾在此漏网）。"""
    from backend.services.engine.realtime_regime import RealtimeRegimeService

    svc = RealtimeRegimeService()
    history = svc._default_history("000300.SH")
    assert len(history["closes"]) >= 21, history
    assert len(history["volumes"]) == len(history["closes"])
    assert all(c > 0 for c in history["closes"][-5:])
    # 日期升序且为 YYYY-MM-DD
    assert history["dates"][-1] >= history["dates"][0]
    assert len(history["dates"][-1]) == 10 and history["dates"][-1][4] == "-"


@pytest.mark.unit
def test_daily_service_delegates_to_shared():
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "services/engine/qlib_app/services/market_state_service.py"
    text = src.read_text(encoding="utf-8")
    assert "build_state_series" in text and "backend.shared.market_regime" in text
    assert 'thresholds["ret_up"]' not in text, "分级第二实现残留"
    assert "rolling(window).mean()" not in text, "滚动第二实现残留"
