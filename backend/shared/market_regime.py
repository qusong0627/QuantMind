"""市场状态（regime）口径单源（T-P6-13）：bull / neutral / bear 三级。

**唯一分级实现**：``classify_regime``（纯函数）。日频 ``MarketStateService``（qlib_app，
回测/策略动态仓位）与日内 regime 服务（实时轨）共用——两侧任何改动必须同时过
``backend/tests/test_market_regime.py`` 的口径金样。

**日频语义（迁移自 MarketStateService，2026-09-17 逐行对齐）**：
- 输入逐日滚动：``ret = close/close.shift(window)-1``；``vol = daily_ret.rolling(window).std()``；
  ``vratio = volume / volume.rolling(window).mean()``；
- **标注位移**：第 i 行算出的状态标注到第 i+1 行的日期（防未来函数）；
- 规则：ret≥ret_up 且 vol≤vol_high → bull；ret≤ret_down 且 vol≥vol_high → bear；
  vratio≥volume_ratio_high 且 ret≥0 → bull；否则 neutral；ret/vol 为 NaN → neutral。

**日内语义**：历史行用收盘价/量，当日行用 live 值构形成输入（``forming_inputs``）——
15:00 终值收敛后与日频同式同值（验收 T-P6-13）。
"""

from __future__ import annotations

import math
from typing import Any

DEFAULT_THRESHOLDS: dict[str, float] = {
    "ret_up": 0.02,
    "ret_down": -0.02,
    "vol_high": 0.03,
    "volume_ratio_high": 1.2,
}
DEFAULT_WINDOW = 20
DEFAULT_REGIME_INDEX = "000300.SH"  # 与 backtest_health.DEFAULT_REGIME_INDEX 同源
STATES = ("bull", "neutral", "bear")
POSITION_BY_STATE: dict[str, float] = {"bull": 1.0, "neutral": 0.7, "bear": 0.3}


def classify_regime(
    ret: float | None,
    vol: float | None,
    vratio: float | None,
    thresholds: dict[str, float] | None = None,
) -> str:
    """唯一分级谓词（NaN/缺值 → neutral；与日频原实现逐行一致）。"""
    th = thresholds or DEFAULT_THRESHOLDS
    if ret is None or vol is None:
        return "neutral"
    if math.isnan(ret) or math.isnan(vol):
        return "neutral"
    if ret >= th["ret_up"] and vol <= th["vol_high"]:
        return "bull"
    if ret <= th["ret_down"] and vol >= th["vol_high"]:
        return "bear"
    if vratio is not None and not math.isnan(vratio):
        if vratio >= th["volume_ratio_high"] and ret >= 0:
            return "bull"
    return "neutral"


def build_state_series(
    closes: list[float],
    volumes: list[float] | None,
    dates: list[str],
    window: int = DEFAULT_WINDOW,
    thresholds: dict[str, float] | None = None,
) -> dict[str, str]:
    """日频序列构造（纯 list 版）：dates 与 closes 等长升序；第 i 行状态标注到 dates[i+1]。

    与 MarketStateService.build_market_state_series 的 qlib 取数层解耦——口径在
    classify_regime，本函数只负责滚动窗口与标注位移。
    """
    n = len(closes)
    if n != len(dates):
        raise ValueError("closes 与 dates 长度必须一致")
    use_vol = volumes if volumes and len(volumes) == n else [1.0] * n
    series: dict[str, str] = {}
    if n <= window + 1:
        return series
    for i in range(window, n - 1):
        ret = _roll_ret(closes, i, window)
        vol = _roll_std(closes, i, window)
        vratio = _roll_vratio(use_vol, i, window)
        series[str(dates[i + 1])] = classify_regime(ret, vol, vratio, thresholds)
    return series


def forming_inputs(
    closes: list[float],
    volumes: list[float] | None,
    live_close: float | None,
    live_volume: float | None,
    window: int = DEFAULT_WINDOW,
    *,
    volume_plausibility: tuple[float, float] = (0.1, 10.0),
) -> dict[str, Any]:
    """日内形成中一行的三输入：历史 + live 拼接后按同一公式滚动到尾位。

    返回 {ret, vol, vratio, ok, notes}。``ok=False``（历史不足 window 或 live 缺失）时
    classify 将按 NaN 规则给 neutral；notes 如实记录原因（绝不静默假值）。
    """
    notes: list[str] = []
    use_vol = list(volumes) if volumes and len(volumes) == len(closes) else [1.0] * len(closes)
    if live_close is None or live_close <= 0:
        return {"ret": None, "vol": None, "vratio": None, "ok": False, "notes": ["no_live_close"]}
    if len(closes) < window:
        return {"ret": None, "vol": None, "vratio": None, "ok": False, "notes": ["history_short"]}
    ret = _roll_ret_with_live(closes, live_close, window)
    vol = _roll_std_with_live(closes, live_close, window)
    vratio: float | None = None
    if live_volume is not None and live_volume > 0:
        # 自含当日（与 pandas volume/volume.rolling(w).mean() 一致）：窗内 w-1 历史 + live
        denom = (sum(use_vol[-(window - 1):]) + float(live_volume)) / window if window > 1 else float(live_volume)
        if denom > 0:
            ratio = float(live_volume) / denom
            lo, hi = volume_plausibility
            if lo <= ratio <= hi:
                vratio = ratio
            else:
                notes.append(f"volume_ratio_implausible({ratio:.2f})——量纲/单位可疑，弃用该输入")
    else:
        notes.append("no_live_volume")
    return {"ret": ret, "vol": vol, "vratio": vratio, "ok": True, "notes": notes}


# ── 滚动统计（列表版，与 pandas rolling 逐式对齐：ret=shift(w)；vol 取 w 个日收益；vratio 自含当日）──


def _roll_ret(closes: list[float], i: int, window: int) -> float | None:
    if i - window < 0:
        return None
    base = closes[i - window]
    return (closes[i] / base - 1.0) if base else None


def _roll_ret_with_live(closes: list[float], live_close: float, window: int) -> float | None:
    if len(closes) < window:
        return None
    base = closes[-window]
    return (live_close / base - 1.0) if base else None


def _std_of(vals: list[float]) -> float | None:
    rets = [(vals[k] / vals[k - 1] - 1.0) for k in range(1, len(vals)) if vals[k - 1]]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var)


def _roll_std(closes: list[float], i: int, window: int) -> float | None:
    """行 i 的滚动波动：pandas daily_ret.rolling(w).std() → 需 closes[i-w .. i]（w+1 值）。"""
    if i - window < 0:
        return None
    return _std_of(closes[i - window: i + 1])


def _roll_std_with_live(closes: list[float], live_close: float, window: int) -> float | None:
    """含 live 当日：vals = closes[-(w)..-1] + [live]（w+1 值 → w 个日收益）。"""
    vals = closes[-window:] + [float(live_close)] if window <= len(closes) else closes + [float(live_close)]
    return _std_of(vals)


def _roll_vratio(volumes: list[float], i: int, window: int) -> float | None:
    """行 i 的 vratio：pandas volume/volume.rolling(w).mean() —— 均值**自含当日**。"""
    if i - window + 1 < 0:
        return None
    mean = sum(volumes[i - window + 1: i + 1]) / window
    if mean <= 0:
        return None
    return volumes[i] / mean
