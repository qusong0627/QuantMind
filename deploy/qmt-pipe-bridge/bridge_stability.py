# -*- coding: utf-8 -*-
"""伴生桥稳定性内核（纯函数，可在 Linux 单测）。

机构级加固的三件事，全部与 I/O 解耦：

1. **自适应交易查询调速（Governor）**
   QMT 的 ``get_trade_detail_data`` 只能在 adjust 主线程跑，连续查询会让主线程
   越跑越慢，最终完全挂死。固定令牌桶只在「挂死之后」限速；Governor 在**变慢
   阶段**就按 EWMA 延迟把交易查询间隔翻倍（最多 8×），给主线程留出恢复窗口，
   避免从「慢」直接掉进 ``unresponsive``。

2. **交易只读结果短缓存（TradeReadCache）**
   同一时刻有多个消费者查同一件事实：容器执行轮询、桥内 positions/orders 轮询、
   QMT 账户同步、健康探针。对 ``get_asset/get_positions/query_orders/query_trades``
   做 1–2 秒的命中缓存，把主线程调用次数压到「每种事实每 TTL 一次」。
   只缓存**成功结果**，错误与订单/撤单绝不缓存。

3. **桥重启风暴护栏（restart_decision）**
   看门狗的 ``bridge_restart_ts`` 曾无限增长（实测数百条），且固定冷却窗挡不住
   「每分钟重启一次」的风暴。这里做：时间戳裁剪（只留最近 50 条/24h）、指数退避
   （60s→120s→…→30min）、30 分钟内 ≥4 次判为风暴并停止自动重启，交人工在 QMT
   策略编辑器里恢复。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------- Governor
@dataclass(frozen=True)
class GovernorConfig:
    """调速阈值（毫秒）。"""

    slow_ms: float = 1200.0
    very_slow_ms: float = 3000.0
    alpha: float = 0.25
    max_multiplier: float = 8.0
    recover_ok: int = 3


@dataclass(frozen=True)
class GovernorState:
    """一次观测后的调速状态（不可变，调用方整体替换）。"""

    ewma_ms: float = 0.0
    timeouts: int = 0
    ok_streak: int = 0
    multiplier: float = 1.0


def observe(
    state: GovernorState,
    latency_ms: float,
    ok: bool,
    cfg: GovernorConfig | None = None,
) -> GovernorState:
    """一次交易查询结果 → 新状态（纯函数）。

    * 失败：EWMA 至少拉到 ``very_slow_ms``，倍率翻倍（封顶 ``max_multiplier``）；
    * 成功但 EWMA 仍高于 ``slow_ms``：保持至少 2×，直到连续 ``recover_ok`` 次
      健康后才逐级减半。**不因一次快查询就直接回到 1×**——主线程的恢复是渐进的。
    """
    cfg = cfg or GovernorConfig()
    if not ok:
        ewma = max(float(state.ewma_ms or 0.0), float(cfg.very_slow_ms))
        multiplier = min(
            float(cfg.max_multiplier),
            max(2.0, float(state.multiplier or 1.0) * 2.0),
        )
        return GovernorState(
            ewma_ms=ewma,
            timeouts=int(state.timeouts) + 1,
            ok_streak=0,
            multiplier=multiplier,
        )
    latency = max(0.0, float(latency_ms or 0.0))
    ewma = (
        latency
        if state.ewma_ms <= 0
        else float(cfg.alpha) * latency + (1.0 - float(cfg.alpha)) * state.ewma_ms
    )
    multiplier = float(state.multiplier or 1.0)
    streak = int(state.ok_streak) + 1
    # 恢复判据看**当前这一次**的延迟，而不是 EWMA：EWMA 从 3s 回落到 1.2s 需要
    # 十几次快查询，等它降下来才减半会让调速间隔在高位滞留过久。连续
    # ``recover_ok`` 次「本次延迟 ≤ slow_ms」已足够证明主线程回到健康区。
    if latency > float(cfg.slow_ms):
        multiplier = min(float(cfg.max_multiplier), max(multiplier, 2.0))
        streak = 0
    elif streak >= max(1, int(cfg.recover_ok)):
        multiplier = max(1.0, multiplier / 2.0)
        streak = 0
    return GovernorState(
        ewma_ms=ewma,
        timeouts=0,
        ok_streak=streak,
        multiplier=multiplier,
    )


def effective_interval(
    base_s: float, state: GovernorState, cfg: GovernorConfig | None = None
) -> float:
    """基础间隔 × 当前倍率（下限=基础，上限=基础×max_multiplier）。"""
    cfg = cfg or GovernorConfig()
    base = max(0.0, float(base_s or 0.0))
    mult = min(float(cfg.max_multiplier), max(1.0, float(state.multiplier or 1.0)))
    return base * mult


# ---------------------------------------------------------------- Trade cache
#: 可缓存的交易只读方法（线名与 canonical 名都列）。**白名单**而不是
#: 「所有 trade-context 方法」：`sync_positions` / `reload_deployment` 也在
#: trade-context 集合里，但它们不是纯读，缓存响应会造成状态幻觉。
CACHEABLE_TRADE_READS = frozenset(
    {
        'get_asset',
        'get_positions',
        'get_position_statistics',
        'query_orders',
        'query_trades',
        'query_execution_snapshot',
        'query_stock_asset',
        'query_stock_positions',
        'query_position_statistics',
        'query_stock_orders',
        'query_stock_trades',
    }
)


def is_cacheable_trade_read(method: str) -> bool:
    """该方法的结果能否做 1–2s 短缓存（只读白名单）。"""
    return str(method or '').strip().lower() in CACHEABLE_TRADE_READS


#: 不同交易只读方法的缓存 TTL（秒）。资产/持仓可以略长，委托/成交必须更短。
_TRADE_READ_TTL = {
    'get_asset': 2.0,
    'get_positions': 2.0,
    'query_stock_asset': 2.0,
    'query_stock_positions': 2.0,
    'query_orders': 1.0,
    'query_trades': 1.0,
    'query_stock_orders': 1.0,
    'query_stock_trades': 1.0,
}


def trade_read_ttl(method: str, default_s: float = 1.0) -> float:
    """方法 → 缓存 TTL（未知交易只读方法按 ``default_s``）。"""
    return float(_TRADE_READ_TTL.get(str(method or '').strip().lower(), default_s))


class TradeReadCache:
    """交易只读结果的短缓存（线程安全）。

    只给 ``rpc_worker`` 在**转发前**查、**成功后**写；错误/订单类不经过它。
    ``hits``/``misses`` 进桥指标，便于确认「缓存是否真的在削峰」。
    """

    def __init__(self, clock=time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._data: dict[str, tuple[float, Any]] = {}
        self.hits = 0
        self.misses = 0
        self.invalidations = 0

    @staticmethod
    def key(method: str, params: Any) -> str:
        try:
            body = json.dumps(params or {}, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:  # noqa: BLE001 - 不可序列化参数 = 不缓存
            body = repr(params)
        return '%s|%s' % (str(method or '').strip().lower(), body)

    def get(self, method: str, params: Any, now: float | None = None) -> tuple[bool, Any]:
        now = self._clock() if now is None else float(now)
        key = self.key(method, params)
        with self._lock:
            item = self._data.get(key)
            if item is None:
                self.misses += 1
                return False, None
            expires_at, value = item
            if now >= expires_at:
                self._data.pop(key, None)
                self.misses += 1
                return False, None
            self.hits += 1
            return True, value

    def put(
        self,
        method: str,
        params: Any,
        value: Any,
        *,
        ttl_s: float,
        now: float | None = None,
    ) -> None:
        ttl = float(ttl_s or 0.0)
        if ttl <= 0:
            return
        now = self._clock() if now is None else float(now)
        key = self.key(method, params)
        with self._lock:
            self._data[key] = (now + ttl, value)

    def invalidate(self) -> None:
        """订单/撤单之后清空：委托与资产事实必须立即重新读。"""
        with self._lock:
            if self._data:
                self.invalidations += 1
            self._data.clear()

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                'entries': len(self._data),
                'hits': self.hits,
                'misses': self.misses,
                'invalidations': self.invalidations,
            }


# ---------------------------------------------------------------- Restart guard
@dataclass(frozen=True)
class RestartConfig:
    base_backoff_s: float = 60.0
    max_backoff_s: float = 1800.0
    hourly_max: int = 6
    storm_threshold: int = 4
    storm_window_s: float = 1800.0
    stamp_keep: int = 50
    stamp_max_age_s: float = 86400.0


@dataclass(frozen=True)
class RestartDecision:
    allowed: bool
    reason: str
    storm: bool = False
    backoff_remaining_s: float = 0.0
    recent_hour: int = 0


def parse_stamps(raw: Any) -> list[float]:
    """``bridge_restart_ts``（逗号分隔字符串或序列）→ 浮点时间戳列表。"""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        values = list(raw)
    else:
        values = str(raw).split(',')
    out: list[float] = []
    for item in values:
        try:
            ts = float(str(item).strip())
        except (TypeError, ValueError):
            continue
        if ts > 0:
            out.append(ts)
    return out


def cap_stamps(
    stamps: list[float], now: float, cfg: RestartConfig | None = None
) -> list[float]:
    """只保留最近 ``keep`` 条、且不超过 ``max_age`` 的时间戳。

    这是生产缺陷修复：旧实现只追加不裁剪，实测 Redis 字段已经积累数百条，
    看门狗每次探测都要 split/float 全表，状态键本身也无限变大。
    """
    cfg = cfg or RestartConfig()
    now = float(now)
    fresh = sorted(
        (ts for ts in stamps if now - ts <= float(cfg.stamp_max_age_s)),
        reverse=True,
    )
    return sorted(fresh[: max(1, int(cfg.stamp_keep))])


def restart_decision(
    stamps: list[float], now: float, cfg: RestartConfig | None = None
) -> RestartDecision:
    """是否允许再一次自动重启伴生桥（纯函数）。

    顺序：先判**风暴**（停止自动重启、交人工），再判小时预算、指数退避。
    风暴阈值按 ``storm_window_s`` 内的重启次数计，而不是小时预算——连续快速
    重启说明「重启已经救不回来」，继续重启只会拉长账户空窗。
    """
    cfg = cfg or RestartConfig()
    now = float(now)
    capped = cap_stamps(stamps, now, cfg)
    recent_hour = [ts for ts in capped if now - ts < 3600.0]
    recent_storm = [ts for ts in capped if now - ts < float(cfg.storm_window_s)]
    if len(recent_storm) >= max(1, int(cfg.storm_threshold)):
        return RestartDecision(
            allowed=False,
            reason='restart storm (%d restarts in %.0fmin)'
            % (len(recent_storm), float(cfg.storm_window_s) / 60.0),
            storm=True,
            recent_hour=len(recent_hour),
        )
    if len(recent_hour) >= max(1, int(cfg.hourly_max)):
        return RestartDecision(
            allowed=False,
            reason='hourly restart budget exhausted (%d/%d)'
            % (len(recent_hour), int(cfg.hourly_max)),
            recent_hour=len(recent_hour),
        )
    if capped:
        attempts = len(recent_hour)
        backoff = min(
            float(cfg.max_backoff_s),
            float(cfg.base_backoff_s) * (2 ** max(0, attempts - 1)),
        )
        elapsed = now - max(capped)
        if elapsed < backoff:
            return RestartDecision(
                allowed=False,
                reason='backoff %.0fs/%.0fs' % (elapsed, backoff),
                backoff_remaining_s=max(0.0, backoff - elapsed),
                recent_hour=len(recent_hour),
            )
    return RestartDecision(allowed=True, reason='ok', recent_hour=len(recent_hour))


def append_stamp(
    stamps: list[float], now: float, cfg: RestartConfig | None = None
) -> list[float]:
    """追加一次重启并立即裁剪（写侧唯一入口，避免再出现无限增长）。"""
    return cap_stamps([*stamps, float(now)], float(now), cfg)
