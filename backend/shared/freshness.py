"""行情新鲜度分级唯一谓词（T-P6-05）。

fresh / stale / unavailable 三级；唯一实现 ``classify_age``（纯函数，边界显式）。
历史散点全部收敛到本模块（源守卫测试防回潮，见 ``test_freshness_latency.py``）：
- stream ``remote_redis_source``：硬编码 >60s 陈旧 / >300s 不可用；
- simulation ``redis_series_quote`` + ``execution_engine``：``SIM_REDIS_QUOTE_MAX_AGE_SEC``；
- preflight ``real_trading_utils.check_stream_series_freshness``：
  ``PREFLIGHT_SERIES_STALE_THRESHOLD_SEC``（trade 预检共用同一函数）。

边界口径（评审红线，改动即影响交易门禁）：
    age <= fresh_within_s                     → fresh
    fresh_within_s < age <= stale_within_s    → stale（可用但须如实标注降级）
    age > stale_within_s / None / ts<=0       → unavailable（禁止使用）
    未来戳 age < -FUTURE_SKEW_TOLERANCE_S     → unavailable（防时钟异常/未来分数长期霸榜
    ZSET——zrevrange 按分数取最新，未来分数会永远排在最前）；容差内（-5s..0）视为 fresh
    （跨机时钟偏斜为常态）。

阈值唯一读取点：``quote_policy()``（env ``QM_QUOTE_FRESH_WITHIN_S`` / ``QM_QUOTE_STALE_WITHIN_S``；
旧名 ``SIM_REDIS_QUOTE_MAX_AGE_SEC`` / ``PREFLIGHT_SERIES_STALE_THRESHOLD_SEC`` 作为兼容别名
映射到 stale 窗口——仅在本模块读取）。
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger(__name__)

FRESH: Literal["fresh"] = "fresh"
STALE: Literal["stale"] = "stale"
UNAVAILABLE: Literal["unavailable"] = "unavailable"
Level = Literal["fresh", "stale", "unavailable"]

FUTURE_SKEW_TOLERANCE_S = 5.0  # 未来时间戳容忍（跨机时钟偏斜；超出判时钟异常）

DEFAULT_FRESH_WITHIN_S = 60.0  # 与 stream 历史 60s 陈旧线一致
DEFAULT_STALE_WITHIN_S = 300.0  # 与 simulation / preflight 历史 300s 不可用线一致

_warned: set[str] = set()


def classify_age(
    age_s: float | None, *, fresh_within_s: float, stale_within_s: float
) -> Level:
    """唯一分级谓词：数据年龄（秒）→ fresh / stale / unavailable。纯函数、无 IO。"""
    if age_s is None:
        return UNAVAILABLE
    try:
        age = float(age_s)
    except (TypeError, ValueError):
        return UNAVAILABLE
    if math.isnan(age) or math.isinf(age):
        return UNAVAILABLE
    if age < -FUTURE_SKEW_TOLERANCE_S:
        return UNAVAILABLE
    if age <= fresh_within_s:
        return FRESH
    if age <= stale_within_s:
        return STALE
    return UNAVAILABLE


@dataclass(frozen=True)
class FreshnessPolicy:
    """分级策略（两条阈值线）；消费方一律经 ``quote_policy()`` 获取，不自行读 env。"""

    fresh_within_s: float = DEFAULT_FRESH_WITHIN_S
    stale_within_s: float = DEFAULT_STALE_WITHIN_S

    def classify(self, age_s: float | None) -> Level:
        return classify_age(
            age_s, fresh_within_s=self.fresh_within_s, stale_within_s=self.stale_within_s
        )

    def classify_ts(self, ts: Any, now_ts: Any) -> Level:
        """时间戳入口：ts/now_ts 为 epoch 秒；ts 缺失/非正/非数 → unavailable。"""
        try:
            ts_f = float(ts)
            now_f = float(now_ts)
        except (TypeError, ValueError):
            return UNAVAILABLE
        if ts_f <= 0:
            return UNAVAILABLE
        return self.classify(now_f - ts_f)

    def is_usable(self, age_s: float | None) -> bool:
        """可用 = 非 unavailable（stale 可用，但消费方必须如实标注降级）。"""
        return self.classify(age_s) != UNAVAILABLE


def _env_float(env: dict[str, str] | None, name: str) -> float | None:
    raw = (env if env is not None else os.environ).get(name)
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        if name not in _warned:
            _warned.add(name)
            logger.warning("freshness env %s 非法（%r），忽略", name, raw)
        return None


def quote_policy(env: dict[str, str] | None = None) -> FreshnessPolicy:
    """行情快照新鲜度策略（唯一读取点）。每次调用读 env——支持热调。"""
    fresh = _env_float(env, "QM_QUOTE_FRESH_WITHIN_S")
    if fresh is None:
        fresh = DEFAULT_FRESH_WITHIN_S
    stale = _env_float(env, "QM_QUOTE_STALE_WITHIN_S")
    if stale is None:
        # 兼容别名（旧三处散点env，仅在此处读取）
        stale = _env_float(env, "SIM_REDIS_QUOTE_MAX_AGE_SEC")
    if stale is None:
        stale = _env_float(env, "PREFLIGHT_SERIES_STALE_THRESHOLD_SEC")
    if stale is None:
        stale = DEFAULT_STALE_WITHIN_S
    if fresh < 0:
        fresh = 0.0
    if stale < fresh:
        key = "stale<fresh"
        if key not in _warned:
            _warned.add(key)
            logger.warning(
                "freshness 阈值非法（stale=%s < fresh=%s），按 fresh 抬齐", stale, fresh
            )
        stale = fresh
    return FreshnessPolicy(fresh_within_s=fresh, stale_within_s=stale)
