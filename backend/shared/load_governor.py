"""过载治理（T-P6-10）：周期时长自适应降级/回切——纯逻辑、线程安全、可注入时钟。

**设计原则（防过载自保，P6 纪律）**：
- 只做**退化**不做熔断：过载时先降非关键计算（如实时覆盖），再放慢节拍；行情/风控链不受其管；
- 分级显式：level 0 正常 → 1 降载（调用方可跳可选计算）→ 2 放慢节拍；降级原因与判定依据
  （滚动 p95 与阈值）全部可观测，绝不静默；
- **回切带滞环**：升级需连续 N 个周期超阈值，降级回切需连续 M 个周期低于回切线——防抖动。

滚动窗口径：最近 `window` 个周期时长（ms）。p95 用最近秩（与全仓 `_percentile_nearest_rank`
同口径，经 shared.signal_thresholds 复用）。
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from typing import Any, Callable

from backend.shared.signal_thresholds import _percentile_nearest_rank

DEFAULT_WINDOW = 20
DEGRADE_P95_RATIO = 0.8   # 周期 p95 > 节拍 × 该比 → 升载一级
SLOW_P95_RATIO = 1.2      # p95 > 节拍 × 该比 → 再升一级（放慢节拍）
RECOVER_RATIO = 0.5       # p95 < 节拍 × 该比 才允许回切
UPGRADE_STREAK = 3        # 连续超阈周期数才升级
RECOVER_STREAK = 10       # 连续低于回切线才回切
MAX_LEVEL = 2
CADENCE_SLOW_FACTOR = 2.0


class LoadGovernor:
    """周期时长 → 降级级别/有效节拍（线程安全；now 可注入供测试）。"""

    def __init__(
        self,
        *,
        base_cadence_s: float,
        window: int = DEFAULT_WINDOW,
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        self.base_cadence_s = max(0.1, float(base_cadence_s))
        self._samples: deque[float] = deque(maxlen=max(4, int(window)))
        self._lock = threading.Lock()
        self._now = now_fn or time.monotonic
        self.level = 0
        self._over_streak = 0
        self._under_streak = 0
        self.counters: dict[str, Any] = {
            "cycles": 0, "degradations": 0, "recoveries": 0,
            "last_ms": None, "p95_ms": None, "level_since": None,
        }

    # ── 记录与判定 ──────────────────────────────────────────────────

    def record(self, duration_ms: float) -> int:
        """记录一个周期时长；返回当前降级级别（0-2）。"""
        try:
            value = float(duration_ms)
        except (TypeError, ValueError):
            return self.level
        if not math.isfinite(value) or value < 0:
            return self.level
        with self._lock:
            self._samples.append(value)
            self.counters["cycles"] += 1
            self.counters["last_ms"] = round(value, 1)
            p95 = self._p95_locked()
            self.counters["p95_ms"] = None if p95 is None else round(p95, 1)
            budget_ms = self.base_cadence_s * 1000.0
            if p95 is None:
                return self.level
            # 分级阈值：0→1 用降载线（0.8×），1→2 用重度线（1.2×）——逐级加重才继续升
            upgrade_line = (
                budget_ms * SLOW_P95_RATIO if self.level >= 1 else budget_ms * DEGRADE_P95_RATIO
            )
            if p95 > upgrade_line:
                self._over_streak += 1
                self._under_streak = 0
            elif p95 < budget_ms * RECOVER_RATIO:
                self._under_streak += 1
                self._over_streak = 0
            else:
                self._over_streak = 0
                self._under_streak = 0
            if self._over_streak >= UPGRADE_STREAK and self.level < MAX_LEVEL:
                self.level += 1
                self.counters["degradations"] += 1
                self.counters["level_since"] = self._now()
                self._over_streak = 0
            elif self._under_streak >= RECOVER_STREAK and self.level > 0:
                self.level -= 1
                self.counters["recoveries"] += 1
                self.counters["level_since"] = self._now()
                self._under_streak = 0
            return self.level

    def _p95_locked(self) -> float | None:
        if len(self._samples) < max(4, self._samples.maxlen // 2):
            return None  # 样本不足不判定（建立期宽限）
        ordered = sorted(self._samples)
        return float(_percentile_nearest_rank(ordered, 0.95))

    # ── 对外行为面 ──────────────────────────────────────────────────

    def skip_optional(self) -> bool:
        """level ≥ 1：调用方应跳过可选计算（如 live 覆盖）。"""
        return self.level >= 1

    def keep_snapshots(self) -> bool:
        """level ≥ 2 也绝不跳过快照读取/发布（行情链优先；降的是节拍不是数据）。"""
        return True

    def effective_cadence_s(self) -> float:
        """有效节拍：level 2 放慢（×2），其余基准。"""
        if self.level >= MAX_LEVEL:
            return self.base_cadence_s * CADENCE_SLOW_FACTOR
        return self.base_cadence_s

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "level": self.level,
                "base_cadence_s": self.base_cadence_s,
                "effective_cadence_s": self.effective_cadence_s(),
                "degraded": self.level > 0,
                **dict(self.counters),
            }
