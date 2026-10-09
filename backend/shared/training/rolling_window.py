"""滚动训练窗口计算（P1 · 设计文档《滚动训练与模型生命周期》§4.1）。

模块定位：把「按月滚动重训」的窗口切分做成**唯一纯函数实现**——只有 stdlib，
不碰 DB / Redis / pandas，便于 golden 测试与跨进程复用（beat 调度、internal
端点、CLI、运维脚本全部走这里，禁止各自手算日期）。

口径（与设计文档 §4.1 逐条对应，"purge" = 净化带）：
- 全部按**交易日**计算（调用方传交易日历），禁止自然日近似；
- test = 末端 ``test_days`` 个交易日；anchor 固定 = test 段最后一天；
- 段间 purge 表现为相邻段之间的**空洞**：``valid_end = test_start - purge - 1``
  （交易日索引距离 = purge + 1），train↔valid 同理；
- purge 默认 = 标签跨度 horizon + 执行滞后（1），可用 ``policy.purge_days``
  显式覆盖；``splits.py`` 另有 train/valid 尾部 embargo（同一 horizon+lag 口径），
  重叠部分是刻意的双保险（设计文档已确认），不在此处去重；
- train = valid 之前的 ``train_days`` 个交易日（sliding）或日历起点到
  train_end（expanding）；
- 日历不足以容纳整窗时抛 :class:`WindowCalculationError`，调用方跳过本轮
  并告警（绝不静默缩窗）。

改动本文件口径必须先改 golden fixture
（``backend/tests/fixtures/rollingWindowGolden.json``）并说明理由。
"""

from __future__ import annotations

import bisect
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

DEFAULT_TRAIN_DAYS = 756
DEFAULT_VALID_DAYS = 126
DEFAULT_TEST_DAYS = 63
DEFAULT_MODE = "sliding"
DEFAULT_EXECUTION_LAG_DAYS = 1
VALID_MODES = ("sliding", "expanding")


class WindowCalculationError(ValueError):
    """窗口无法计算（日历为空/过短、anchor 越界、日期不可解析）。

    调用方（调度器/端点/CLI）应捕获后跳过本轮并告警——不允许静默缩窗。
    """


def parse_day(value: Any) -> date:
    """把 date / datetime / ISO 串统一成 date（容忍 "YYYY-MM-DDTHH:MM:SSZ" 前缀）。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError as exc:
            raise WindowCalculationError(f"无法解析日期: {value!r}") from exc
    raise WindowCalculationError(f"无法解析日期: {value!r}")


def _int_or(value: Any, default: int, minimum: int | None = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if minimum is not None and parsed < minimum:
        return default
    return parsed


def month_index(day: date) -> int:
    """绝对月序号（自 2000-01 起）——campaign 幂等/排序的主键之一，跨年单调。"""
    return (day.year - 2000) * 12 + (day.month - 1)


@dataclass(frozen=True)
class WindowPolicy:
    """窗口策略。默认值 = 设计文档 §4.1 的推荐档（756/126/63，purge 自动推导）。

    ``from_dict`` 对非法值一律**回落默认**而非报错（策略来自 Redis 配置，
    半坏配置不应该把调度器打死）；真正算不出来由 ``compute_window`` 抛错。
    """

    train_days: int = DEFAULT_TRAIN_DAYS
    valid_days: int = DEFAULT_VALID_DAYS
    test_days: int = DEFAULT_TEST_DAYS
    mode: str = DEFAULT_MODE
    purge_days: int | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> WindowPolicy:
        if not data:
            return cls()
        purge: int | None = None
        if data.get("purge_days") is not None:
            candidate = _int_or(data.get("purge_days"), -1)
            purge = candidate if candidate >= 0 else None
        mode = str(data.get("mode") or "").strip().lower()
        if mode not in VALID_MODES:
            mode = DEFAULT_MODE
        return cls(
            train_days=_int_or(data.get("train_days"), DEFAULT_TRAIN_DAYS, minimum=1),
            valid_days=_int_or(data.get("valid_days"), DEFAULT_VALID_DAYS, minimum=1),
            test_days=_int_or(data.get("test_days"), DEFAULT_TEST_DAYS, minimum=1),
            mode=mode,
            purge_days=purge,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "train_days": self.train_days,
            "valid_days": self.valid_days,
            "test_days": self.test_days,
            "mode": self.mode,
            "purge_days": self.purge_days,
        }

    def resolved_purge_days(
        self,
        horizon_days: Any,
        execution_lag_days: Any = DEFAULT_EXECUTION_LAG_DAYS,
    ) -> int:
        """净化带天数：显式覆盖优先，否则 = horizon + 执行滞后（下限 1）。"""
        if self.purge_days is not None:
            return self.purge_days
        horizon = _int_or(horizon_days, 0, minimum=0)
        lag = _int_or(execution_lag_days, DEFAULT_EXECUTION_LAG_DAYS, minimum=0)
        return max(1, horizon + lag)


@dataclass(frozen=True)
class RollingWindow:
    """一段冻结的滚动窗口（全部字段为交易日历上的真实交易日）。"""

    anchor_date: date
    train_start: date
    train_end: date
    valid_start: date
    valid_end: date
    test_start: date
    test_end: date
    purge_days: int
    mode: str
    window_index: int

    def to_plan(self) -> dict[str, Any]:
        """规范 plan（campaign.window_plan / rolling_meta / CLI 输出共用）。"""
        return {
            "anchor_date": self.anchor_date.isoformat(),
            "mode": self.mode,
            "purge_days": self.purge_days,
            "window_index": self.window_index,
            "train": [self.train_start.isoformat(), self.train_end.isoformat()],
            "valid": [self.valid_start.isoformat(), self.valid_end.isoformat()],
            "test": [self.test_start.isoformat(), self.test_end.isoformat()],
        }

    def to_split_fields(self) -> dict[str, str]:
        """训练请求 payload 的 split 六键（splits.py 显式切分路径消费）。"""
        return {
            "train_start": self.train_start.isoformat(),
            "train_end": self.train_end.isoformat(),
            "valid_start": self.valid_start.isoformat(),
            "valid_end": self.valid_end.isoformat(),
            "test_start": self.test_start.isoformat(),
            "test_end": self.test_end.isoformat(),
        }


def compute_window(
    anchor: Any,
    trading_days: Iterable[Any] | Sequence[Any],
    policy: WindowPolicy,
    horizon_days: Any,
    execution_lag_days: Any = DEFAULT_EXECUTION_LAG_DAYS,
) -> RollingWindow:
    """按交易日历切出滚动窗口。

    ``anchor`` 非交易日（周末/节假日）时向下取整到最近交易日；早于日历
    首日或整窗放不下时抛 :class:`WindowCalculationError`。
    """
    days = sorted({parse_day(day) for day in trading_days})
    if not days:
        raise WindowCalculationError("交易日历为空，无法计算滚动窗口")
    anchor_day = parse_day(anchor)
    idx = bisect.bisect_right(days, anchor_day) - 1
    if idx < 0:
        raise WindowCalculationError(f"anchor {anchor_day} 早于日历首个交易日 {days[0]}")

    purge = policy.resolved_purge_days(horizon_days, execution_lag_days)
    min_train = 1 if policy.mode == "expanding" else policy.train_days
    span = min_train + policy.valid_days + policy.test_days + 2 * purge
    if idx + 1 < span:
        train_desc = "train ≥1（expanding）" if policy.mode == "expanding" else f"train {policy.train_days}"
        raise WindowCalculationError(
            f"交易日不足：窗口需要 {span} 个交易日"
            f"（{train_desc} + purge {purge} + valid {policy.valid_days}"
            f" + purge {purge} + test {policy.test_days}），"
            f"anchor {days[idx]} 及之前仅有 {idx + 1} 个"
        )

    test_end_idx = idx
    test_start_idx = idx - policy.test_days + 1
    valid_end_idx = test_start_idx - purge - 1
    valid_start_idx = valid_end_idx - policy.valid_days + 1
    train_end_idx = valid_start_idx - purge - 1
    train_start_idx = (
        0 if policy.mode == "expanding" else train_end_idx - policy.train_days + 1
    )

    return RollingWindow(
        anchor_date=days[idx],
        train_start=days[train_start_idx],
        train_end=days[train_end_idx],
        valid_start=days[valid_start_idx],
        valid_end=days[valid_end_idx],
        test_start=days[test_start_idx],
        test_end=days[test_end_idx],
        purge_days=purge,
        mode=policy.mode,
        window_index=month_index(days[idx]),
    )


def resolve_anchor(
    available_dates: Iterable[Any],
    horizon_days: Any,
    execution_lag_days: Any = DEFAULT_EXECUTION_LAG_DAYS,
) -> date | None:
    """从因子源可用分区日期里定锚：最后一个**标签完整**的交易日。

    标签在 T 日收盘生成、T+lag 执行、持有 horizon 日 → T 的标签需要
    T+horizon+lag 的收盘价。因此倒数 ``horizon+lag`` 天的标签不完整，
    anchor = 分区末尾回退 ``horizon+lag`` 天的那一天（设计 §4.1 数据滞后守卫）。
    数据不足一个标签跨度时返回 None（调用方跳过本轮并告警）。
    """
    days = sorted({parse_day(day) for day in available_dates})
    back = _int_or(horizon_days, 0, minimum=0) + _int_or(
        execution_lag_days, DEFAULT_EXECUTION_LAG_DAYS, minimum=0
    )
    if len(days) <= back:
        return None
    return days[-(back + 1)]
