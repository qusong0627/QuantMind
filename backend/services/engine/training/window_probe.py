"""训练数据窗口探针 —— 时间切分的唯一入口（探针模式）。

设计（2026-09 重构）
--------------------
时间切分不再跟着后端因子目录的「草稿 / 发布」状态走，而是由探针直接读取
数据侧的真实区间与交易日序列：

* ``kind="local"``  → 进程内直读本机 QuantDB（``/data/quantdb``）
* ``kind="remote"`` → 经 SSH 探针读训练节点自己的 QuantDB（AutoDL / 魔搭数据集）

切分点用**探针读到的交易日序列**按 val_ratio 换算成绝对日期，本地与远端
各自自洽（远端魔搭数据集与中心日期集合并不相同：实测 l1_factors 少 225 个
交易日且整段缺失，用中心日历算切分会让节点上的样本占比失真）。

两端日期集合差异单独做差分校验：只在尾部 → 正常滞后（钳制末端）；出现在
窗口中间成段 → 节点数据不完整，应当拦截。

发布目录（``qm_training_factor_catalog_version``）只负责「逻辑字段 → 原始列」
的映射，不参与时间切分。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_FACTOR_SOURCE = "l1_factors"
DEFAULT_TTL_SEC = 600
# 相邻缺失交易日间隔超过该值即视为新的一段（>4 天 ≈ 跨周末 + 节假日）
_SEGMENT_GAP_DAYS = 4
MIN_TRADING_DAYS_FOR_SPLIT = 30


@dataclass
class DataWindow:
    """某一侧（本地 / 远程节点）的数据窗口画像。"""

    kind: str  # "local" | "remote"
    node_id: str
    source: str
    market: str
    ready: bool
    min_date: str | None
    max_date: str | None
    trading_dates: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    schema_hash: str = ""
    reason: str | None = None
    probed_at: str = ""

    def dates_in(self, start: str | None = None, end: str | None = None) -> list[str]:
        return [
            d
            for d in self.trading_dates
            if (not start or d >= start) and (not end or d <= end)
        ]

    def to_dict(self, *, with_dates: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind,
            "node_id": self.node_id,
            "source": self.source,
            "market": self.market,
            "ready": self.ready,
            "min_date": self.min_date,
            "max_date": self.max_date,
            "trading_days": len(self.trading_dates),
            "column_count": len(self.columns),
            "schema_hash": self.schema_hash,
            "reason": self.reason,
            "probed_at": self.probed_at,
        }
        if with_dates:
            payload["trading_dates"] = list(self.trading_dates)
        return payload


# (kind, node_id, source, market) -> (expire_monotonic, DataWindow)
_CACHE: dict[tuple[str, str, str, str], tuple[float, DataWindow]] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cache_get(key: tuple[str, str, str, str]) -> DataWindow | None:
    hit = _CACHE.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    return None


def _cache_put(key: tuple[str, str, str, str], window: DataWindow, ttl: int) -> DataWindow:
    if ttl > 0:
        _CACHE[key] = (time.monotonic() + ttl, window)
    return window


def clear_cache() -> None:
    """测试/手动刷新用。"""
    _CACHE.clear()


# ---------------------------------------------------------------------------
# 探针：读本地 / 远程的数据窗口
# ---------------------------------------------------------------------------
def _probe_local(source: str, market: str, node_id: str = "local") -> DataWindow:
    """直读本机 QuantDB（describe 走 dt= 分区目录，不扫全表）。"""
    from backend.services.engine.data_platform.quantdb_factor_reader import (
        QuantDBFactorReader,
    )

    reader = QuantDBFactorReader(market=market)
    try:
        status = reader.describe(source)
        dates = reader.available_dates(source)
        columns = sorted(reader.factor_columns(source))
    except Exception as exc:  # noqa: BLE001
        logger.warning("local window probe failed (%s/%s): %s", market, source, exc)
        return DataWindow(
            kind="local",
            node_id=node_id,
            source=source,
            market=market,
            ready=False,
            min_date=None,
            max_date=None,
            reason=str(exc),
            probed_at=_now_iso(),
        )
    return DataWindow(
        kind="local",
        node_id=node_id,
        source=source,
        market=market,
        ready=bool(status.ready),
        min_date=status.min_date,
        max_date=status.max_date,
        trading_dates=list(dates),
        columns=columns,
        schema_hash=str(status.schema_hash or ""),
        reason=status.reason,
        probed_at=_now_iso(),
    )


def probe_local_window(
    source: str, market: str = "CN", node_id: str = "local"
) -> DataWindow:
    """同步直读本机窗口（供同步代码路径 / 兜底使用）。"""
    return _probe_local(str(source or DEFAULT_FACTOR_SOURCE), str(market or "CN").upper(), node_id)


async def _probe_remote(node_id: str, source: str, market: str) -> DataWindow:
    """经 SSH 探针读训练节点的 QuantDB 窗口（只读，不触发任何数据同步）。"""
    from backend.services.engine.training.orchestrator_base import get_orchestrator

    orchestrator = get_orchestrator(node_id=node_id)
    probe = getattr(orchestrator, "probe_data_profile", None)
    if probe is None:
        raise RuntimeError(
            f"节点 {node_id} 不支持数据探针：只有远程训练节点可被探测，"
            "本地请使用 node_id='local'"
        )
    profile = await probe(source, market=market)
    return DataWindow(
        kind="remote",
        node_id=node_id,
        source=source,
        market=market,
        ready=bool(profile.get("ready")),
        min_date=profile.get("min_date"),
        max_date=profile.get("max_date"),
        trading_dates=list(profile.get("trading_dates") or []),
        columns=list(profile.get("columns") or []),
        schema_hash=str(profile.get("schema_hash") or ""),
        reason=profile.get("reason"),
        probed_at=str(profile.get("probed_at") or _now_iso()),
    )


async def probe_data_window(
    node_id: str | None = None,
    source: str = DEFAULT_FACTOR_SOURCE,
    market: str = "CN",
    *,
    ttl: int = DEFAULT_TTL_SEC,
) -> DataWindow:
    """探测数据窗口：``node_id`` 为空 / "local" 读本机，否则 SSH 读远程节点。

    结果按 (kind, node_id, source, market) 缓存 ttl 秒。
    """
    node = str(node_id or "local").strip() or "local"
    kind = "local" if node == "local" else "remote"
    market = str(market or "CN").upper()
    key = (kind, node, source, market)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    if kind == "local":
        window = await asyncio.to_thread(_probe_local, source, market)
    else:
        window = await _probe_remote(node, source, market)
    return _cache_put(key, window, ttl)


async def probe_center_window(
    source: str = DEFAULT_FACTOR_SOURCE, market: str = "CN", *, ttl: int = DEFAULT_TTL_SEC
) -> DataWindow:
    """中心（本机）窗口，作为差分校验的对照侧。"""
    return await probe_data_window("local", source, market, ttl=ttl)


# ---------------------------------------------------------------------------
# 切分：在探针读到的交易日序列上取绝对日期边界
# ---------------------------------------------------------------------------
def split_bounds(dates: list[str], val_ratio: float) -> dict[str, list[str]] | None:
    """在给定交易日序列上按 val_ratio 求三段绝对日期边界。

    与 docker/training/data/splits.py 的 val_ratio 分支同口径：
      train=[d0, val_start 前一交易日]  valid=[val_start, test_start 前一交易日]
      test=[test_start, 末日]；test_ratio = val_ratio / 2
    """
    if len(dates) < MIN_TRADING_DAYS_FOR_SPLIT or not 0 < val_ratio < 1:
        return None
    val_idx = int(len(dates) * (1 - val_ratio))
    test_idx = int(len(dates) * (1 - val_ratio / 2.0))
    if not (0 < val_idx < test_idx < len(dates)):
        return None
    return {
        "train": [dates[0], dates[val_idx - 1]],
        "valid": [dates[val_idx], dates[test_idx - 1]],
        "test": [dates[test_idx], dates[-1]],
    }


def window_span(payload: dict) -> tuple[str, str] | None:
    """训练窗口 = [train_start, test_end|valid_end|train_end]。"""
    start = str(payload.get("train_start") or "").strip()
    end = str(
        payload.get("test_end")
        or payload.get("valid_end")
        or payload.get("train_end")
        or ""
    ).strip()
    if not start or not end:
        return None
    return start, end


def build_split_from_window(
    window: DataWindow, payload: dict, *, val_ratio: float | None = None
) -> dict[str, list[str]] | None:
    """用探针窗口的交易日序列，把 val_ratio 换算成三段绝对日期。"""
    span = window_span(payload)
    if not span:
        return None
    if val_ratio is None:
        try:
            val_ratio = float(payload.get("val_ratio") or 0.15)
        except (TypeError, ValueError):
            return None
    return split_bounds(window.dates_in(span[0], span[1]), val_ratio)


# ---------------------------------------------------------------------------
# 差分：中心 vs 节点（成段缺失会被拦截）
# ---------------------------------------------------------------------------
def missing_dates(
    center: DataWindow, node: DataWindow, *, start: str, end: str
) -> list[str]:
    """窗口内「中心有、节点没有」的交易日（日期集合级，非 min/max 比较）。"""
    node_set = set(node.trading_dates)
    return [d for d in center.dates_in(start, end) if d not in node_set]


def segmentize(dates: list[str], gap_days: int = _SEGMENT_GAP_DAYS) -> list[dict[str, Any]]:
    """把缺失日期聚成连续段（间隔 > gap_days 视为新段）。"""
    from datetime import date as _date

    segments: list[list[str]] = []
    for d in sorted(dates):
        if segments and (
            _date.fromisoformat(d) - _date.fromisoformat(segments[-1][-1])
        ).days <= gap_days:
            segments[-1].append(d)
        else:
            segments.append([d])
    return [
        {"start": seg[0], "end": seg[-1], "days": len(seg)} for seg in segments
    ]


def is_tail_lag_only(
    center: DataWindow, node: DataWindow, *, start: str, end: str
) -> bool:
    """缺失是否只是「尾部滞后」（节点数据比中心少最近若干天）。

    尾部滞后是远端魔搭数据集的常态；中间成段缺失则是数据不完整。
    """
    missing = missing_dates(center, node, start=start, end=end)
    if not missing:
        return False
    center_dates = center.dates_in(start, end)
    node_max = node.max_date or ""
    if center_dates and missing[0] <= node_max:
        # 缺失区间的起点早于节点覆盖末端 → 中间有洞，不是纯滞后
        return False
    return len(segmentize(missing)) == 1


def coverage_report(
    center: DataWindow, node: DataWindow, *, start: str, end: str
) -> dict[str, Any]:
    """窗口内两端覆盖对照，供接口 / 前端展示与提交前拦截使用。"""
    missing = missing_dates(center, node, start=start, end=end)
    segments = segmentize(missing)
    window_days = len(center.dates_in(start, end))
    return {
        "window": {"start": start, "end": end},
        "center": center.to_dict(),
        "node": node.to_dict(),
        "center_trading_days": window_days,
        "node_trading_days": len(node.dates_in(start, end)),
        "missing_days": len(missing),
        "missing_ratio": round(len(missing) / window_days, 4) if window_days else 0.0,
        "missing_segments": segments,
        "tail_lag_only": is_tail_lag_only(center, node, start=start, end=end),
        "aligned": not missing,
    }
