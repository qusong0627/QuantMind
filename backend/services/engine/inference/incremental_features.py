"""增量特征引擎（T-P6-07）：快照流 → 当日形成 bar + 分钟桶 + TIER 特征快车道。

定位：T-P6-08 实时热集推理的**特征供给层**。批量特征（``feature_defs``，单标的 ≈112ms）
无法按实时节拍全量重算；本引擎维护仓位级增量状态：
- 历史窗口环（close/high/low/volume/amount，长度 ``TIER_MAX_WINDOW-1``）；
- 当日形成中 bar（open=首帧/feed.Open，high/low=极值跟踪，close=最新价，
  volume/amount=feed 累计值取最新——**不求和**：TDX 推送为当日累计口径）；
- 分钟桶环（ts//60 → 最新价/累计量，供后续分钟级特征与 T-P6-13）；
- TIER 列快车道求值（``shared/feature_incremental``，与批量口径金样锁定）；
- **provenance**：``features_with_fallback`` 用 live 值覆盖 T-1 快照行，其余列显式标注 T-1。

显式状态语义（D 类，全部计数可观测，绝不静默）：
- 乱序帧（ts 早于最新）→ 整帧拒绝 ``out_of_order``；
- 同日重复 ts → 幂等更新（close/量取最新）；
- 缺口（相邻帧间隔 > ``gap_s``）→ ``gaps`` 计数（bar 仍有效：feed 累计字段自成锚）；
- 日切（帧日期 > 形成日）→ 旧 bar 入环 ``rollovers``，开新 bar；
- **同日历史遮蔽**：引导历史末行日期 == 形成日时，计算窗口剔除该行（防重复计日）；
- 冷启动（历史 < 窗口）→ 与批量截断窗同语义（min_periods 自然 NaN），``cold`` 标记。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np

from backend.shared.feature_incremental import TIER_MAX_WINDOW, Window, compute_tier

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))
DEFAULT_GAP_S = 90.0
MINUTE_RING_SIZE = 256
COLD_HISTORY_MIN = (
    21  # 历史 bar 少于该数 → cold（部分 20 日窗特征不完整，min_periods 自然 NaN）
)


def _snap_get(snap: dict[str, Any], *names: str) -> Any:
    """标准键(Now/Open/...)/TDX 原始键(price/open/...)双风格取值。"""
    for name in names:
        if name in snap and snap[name] is not None:
            return snap[name]
    return None


def _to_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if np.isnan(out) or np.isinf(out):
        return None
    return out


class SymbolState:
    """单标的增量状态（历史环 + 形成 bar + 分钟桶）。"""

    __slots__ = (
        "symbol",
        "closes",
        "highs",
        "lows",
        "volumes",
        "amounts",
        "bar",
        "bar_day",
        "last_ts",
        "last_hist_date",
        "minutes",
        "counters",
        "pre_close",
        "masked_day",
    )

    def __init__(self, symbol: str, window: int) -> None:
        self.symbol = symbol
        hist = max(1, int(window) - 1)
        self.closes: deque[float] = deque(maxlen=hist)
        self.highs: deque[float] = deque(maxlen=hist)
        self.lows: deque[float] = deque(maxlen=hist)
        self.volumes: deque[float] = deque(maxlen=hist)
        self.amounts: deque[float] = deque(maxlen=hist)
        self.bar: dict[str, float] | None = None
        self.bar_day: str | None = None  # YYYYMMDD(CST)
        self.last_ts: float | None = None
        self.last_hist_date: str | None = None
        self.minutes: deque[tuple[int, float, float | None]] = deque(
            maxlen=MINUTE_RING_SIZE
        )
        self.pre_close: float | None = None
        self.masked_day: str | None = (
            None  # 已计入 day_masks 的日期（防每次 compute 重复计数）
        )
        self.counters: dict[str, Any] = {
            "updates": 0,
            "out_of_order": 0,
            "rollovers": 0,
            "gaps": 0,
            "day_masks": 0,
            "rejected": 0,
            "last_error": None,
        }


class IncrementalFeatureEngine:
    """仓位级增量特征引擎（线程安全；纯内存状态，无 IO）。"""

    def __init__(
        self, *, window: int = TIER_MAX_WINDOW, gap_s: float = DEFAULT_GAP_S
    ) -> None:
        self.window = max(25, int(window))
        self.gap_s = float(gap_s)
        self._states: dict[str, SymbolState] = {}
        self._lock = threading.Lock()

    # ── 引导 ────────────────────────────────────────────────────────

    def bootstrap(self, symbol: str, history: Any) -> int:
        """引导历史（DataFrame：trade_date/close/high/low/volume/amount，升序）。返回入环行数。

        行过滤与 NaN 语义与批量路径严格一致：close<=0 或 volume==0 视为停牌**整行剔除**
        （feature_defs 同规则）；其余缺失值以 NaN 入环（numpy skipna 与 pandas 同语义）。
        """
        with self._lock:
            state = self._states.get(symbol) or SymbolState(symbol, self.window)
            self._states[symbol] = state
            rows = (
                history.sort_values("trade_date")
                if hasattr(history, "sort_values")
                else history
            )

            def _raw(value: Any) -> float:
                out = _to_float(value)
                return out if out is not None else float("nan")

            for _, row in rows.iterrows():
                close = _to_float(row.get("close"))
                volume = _to_float(row.get("volume"))
                if (close is not None and close <= 0) or (
                    volume is not None and volume == 0
                ):
                    state.counters["boot_skipped"] = (
                        state.counters.get("boot_skipped", 0) + 1
                    )
                    continue
                state.closes.append(close if close is not None else float("nan"))
                state.highs.append(_raw(row.get("high")))
                state.lows.append(_raw(row.get("low")))
                state.volumes.append(volume if volume is not None else float("nan"))
                state.amounts.append(_raw(row.get("amount")))
                day = row.get("trade_date")
                state.last_hist_date = (
                    day.strftime("%Y%m%d")
                    if hasattr(day, "strftime")
                    else str(day)[:10].replace("-", "")
                )
            return len(state.closes)

    # ── 快照流 ──────────────────────────────────────────────────────

    def on_snapshot(self, symbol: str, snap: dict[str, Any]) -> dict[str, Any]:
        """喂入一帧快照（标准键或 TDX 原始键）；返回处理结果摘要（供测试/巡检）。"""
        ts_raw = _snap_get(snap, "ts", "timestamp")
        price = _to_float(_snap_get(snap, "price", "Now"))
        ts = _to_float(ts_raw)
        if price is None or price <= 0 or ts is None or ts <= 0:
            return self._reject(symbol, "bad_frame")
        with self._lock:
            state = self._states.get(symbol)
            if state is None:
                state = SymbolState(symbol, self.window)
                self._states[symbol] = state

            if state.last_ts is not None and ts < state.last_ts:
                state.counters["out_of_order"] += 1
                return {"accepted": False, "reason": "out_of_order"}
            if state.last_ts is not None and (ts - state.last_ts) > self.gap_s:
                state.counters["gaps"] += 1

            day = datetime.fromtimestamp(ts, tz=CST).strftime("%Y%m%d")
            if state.bar_day is not None and day > state.bar_day:
                self._roll_day(state)  # 旧 bar 入环，开新日

            feed_open = _to_float(_snap_get(snap, "open", "Open")) or price
            feed_high = _to_float(_snap_get(snap, "high", "High"))
            feed_low = _to_float(_snap_get(snap, "low", "Low"))
            feed_vol = _to_float(_snap_get(snap, "volume", "Volume"))
            feed_amt = _to_float(_snap_get(snap, "amount", "Amount"))
            pre_close = _to_float(_snap_get(snap, "pre_close", "PreClose"))
            if pre_close is not None:
                state.pre_close = pre_close

            if state.bar is None:
                state.bar = {
                    "open": feed_open,
                    "high": max(feed_high or price, price),
                    "low": min(feed_low or price, price),
                    "close": price,
                    "volume": feed_vol if feed_vol is not None else 0.0,
                    "amount": feed_amt if feed_amt is not None else 0.0,
                }
                state.bar_day = day
            else:
                bar = state.bar
                bar["high"] = max(bar["high"], feed_high or price, price)
                bar["low"] = min(bar["low"], feed_low or price, price)
                bar["close"] = price
                if feed_vol is not None:
                    bar["volume"] = feed_vol  # 累计口径：取最新，不求和
                if feed_amt is not None:
                    bar["amount"] = feed_amt
            state.last_ts = ts
            state.counters["updates"] += 1
            minute = int(ts // 60)
            if state.minutes and state.minutes[-1][0] == minute:
                state.minutes[-1] = (minute, price, state.bar["volume"])
            else:
                state.minutes.append((minute, price, state.bar["volume"]))
            return {"accepted": True, "day": day, "bar": dict(state.bar)}

    def _roll_day(self, state: SymbolState) -> None:
        """日切：形成 bar 入历史环（若有效），重置为新日。"""
        bar = state.bar
        if bar is not None and bar.get("close", 0) > 0:
            state.closes.append(float(bar["close"]))
            state.highs.append(float(bar["high"]))
            state.lows.append(float(bar["low"]))
            state.volumes.append(float(bar.get("volume") or 0.0))
            state.amounts.append(float(bar.get("amount") or 0.0))
            state.last_hist_date = state.bar_day
            state.counters["rollovers"] += 1
        state.bar = None
        state.bar_day = None
        state.minutes.clear()

    def _reject(self, symbol: str, reason: str) -> dict[str, Any]:
        with self._lock:
            state = self._states.get(symbol)
            if state is not None:
                state.counters["rejected"] += 1
        return {"accepted": False, "reason": reason}

    # ── 求值与读取 ──────────────────────────────────────────────────

    def _window(self, state: SymbolState) -> Window:
        """历史环 + 形成 bar → 窗口数组；同日历史遮蔽在此剔除。"""
        closes = list(state.closes)
        highs = list(state.highs)
        lows = list(state.lows)
        volumes = list(state.volumes)
        amounts = list(state.amounts)
        if (
            state.bar_day is not None
            and state.last_hist_date == state.bar_day
            and closes
        ):
            closes, highs, lows, volumes, amounts = (
                closes[:-1],
                highs[:-1],
                lows[:-1],
                volumes[:-1],
                amounts[:-1],
            )
            if state.masked_day != state.bar_day:
                state.masked_day = state.bar_day
                state.counters["day_masks"] += 1
        if state.bar is not None:
            closes.append(float(state.bar["close"]))
            highs.append(float(state.bar["high"]))
            lows.append(float(state.bar["low"]))
            volumes.append(float(state.bar.get("volume") or 0.0))
            amounts.append(float(state.bar.get("amount") or 0.0))
        return Window(
            np.asarray(closes),
            np.asarray(highs),
            np.asarray(lows),
            np.asarray(volumes),
            np.asarray(amounts),
        )

    def compute(self, symbol: str) -> dict[str, Any] | None:
        """TIER 列 live 值；无状态返回 None。"""
        with self._lock:
            state = self._states.get(symbol)
            if state is None:
                return None
            window = self._window(state)
            cold = (len(window.close) - 1) < COLD_HISTORY_MIN
            day = state.bar_day
        out = compute_tier(window)
        out["_meta"] = {"cold": cold, "window_len": len(window.close), "day": day}
        return out

    def features_with_fallback(
        self, symbol: str, baseline: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """live TIER 覆盖 T-1 baseline（模型全量行）；返回 (row, provenance)。

        provenance: 列 → "live"（本引擎快车道实值）| "t1"（T-1 基线携带；live 失败或
        cold 时回退）| "cold"（live 与基线皆缺）。**live 为 NaN 且有基线值时回退基线**，
        保证模型输入不因单列缺口整体降级。
        """
        live = self.compute(symbol)
        row: dict[str, Any] = dict(baseline or {})
        prov: dict[str, str] = {}
        if live is not None:
            live.pop("_meta", None)
            for col, value in live.items():
                live_missing = value is None or (
                    isinstance(value, float) and np.isnan(value)
                )
                base_value = row.get(col)
                base_finite = base_value is not None and not (
                    isinstance(base_value, float) and np.isnan(base_value)
                )
                if live_missing:
                    if base_finite:
                        prov[col] = "t1"  # 回退基线（row 保留基线值）
                    else:
                        prov[col] = "cold"
                else:
                    row[col] = value
                    prov[col] = "live"
        if baseline:
            for col in baseline:
                prov.setdefault(col, "t1")
        return row, prov

    def forming_bar(self, symbol: str) -> dict[str, Any] | None:
        with self._lock:
            state = self._states.get(symbol)
            if state is None or state.bar is None:
                return None
            out = dict(state.bar)
            out["day"] = state.bar_day
            return out

    def minute_ring(self, symbol: str) -> list[tuple[int, float, float | None]]:
        with self._lock:
            state = self._states.get(symbol)
            return list(state.minutes) if state else []

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "symbols": len(self._states),
                "window": self.window,
                "details": {
                    sym: {
                        **dict(st.counters),
                        "ring": len(st.closes),
                        "day": st.bar_day,
                    }
                    for sym, st in self._states.items()
                },
                "ts": time.time(),
            }

    def reset(self, symbol: str | None = None) -> None:
        with self._lock:
            if symbol:
                self._states.pop(symbol, None)
            else:
                self._states.clear()
