"""持仓行情**真实供数源**审计：谁在喂我的持仓？

为什么需要：``market:snapshot:{sym}`` 同一只票同一时刻可能是**多个写席轮流写**——
桥热集（``source=tdx_bridge``，529 只轮转 ~95s/只）、QMT 备源席（``source=qmt_big``，
主源陈旧时接管）、TDX 订阅（``source=tdx_aidata_sub``）、旧持仓馈送（``tdx_bridge``）。
读侧（``remote_redis_source`` / WS）**不带 source**，所以「行情来源」条此前读的是另一条
链路的心跳（TDX 持仓馈送 ``bridge_ok``），与真正供数方无关：文案在两个源之间来回切、
现价跟着跳。本模块按持仓标的**实际采样快照的 source 字段**聚合出真实供数方。

口径与 stream 读侧一致：
- 候选键先小写前缀（``market:snapshot:sh600036``）后大写前缀，取第一个非空；
- 新鲜度分级走 ``freshness.quote_policy()`` 唯一谓词（fresh ≤60s / stale ≤300s / unavailable）。
"""

from __future__ import annotations

import time
from typing import Any, Callable, Iterable

from backend.shared.freshness import UNAVAILABLE, FreshnessPolicy, quote_policy

#: 行情快照 source 显示名。注意与账户快照的 source 命名空间不同：
#: 这里 ``qmt_big`` 是**备源席**（不是账户侧的执行端）。
QUOTE_SOURCE_LABELS: dict[str, str] = {
    "tdx_bridge": "通达信桥",
    "tdx_aidata_sub": "TDX 订阅",
    "qmt_big": "QMT 备源",
    "qmt_exec": "QMT 执行端",
}

UNKNOWN_SOURCE = "unknown"

#: 单次采样上限（持仓/热集都在 100~600 只量级，键读走 pipeline，一次往返）
DEFAULT_SAMPLE_LIMIT = 300


def quote_source_label(source: str | None) -> str:
    """行情源显示名（未知源原样返回，不编造中文名）。"""
    src = str(source or "").strip()
    if not src:
        return "未知来源"
    return QUOTE_SOURCE_LABELS.get(src, src)


def snapshot_key_candidates(symbol: str) -> list[str]:
    """代码（前缀式/后缀式/裸码）→ market:snapshot 候选键（与 stream 读侧同序）。

    代码口径见 CLAUDE.md：行情层用后缀式（``600036.SH``）、Redis 键用前缀式
    （``SH600036``，键名再小写）。认不出的返回空列表（不猜）。
    """
    from backend.shared.stock_utils import StockCodeUtil

    prefix = str(StockCodeUtil.to_prefix(str(symbol or "").strip()) or "").strip().upper()
    if len(prefix) != 8 or not prefix[2:].isdigit() or prefix[:2] not in {"SH", "SZ", "BJ"}:
        return []
    return [f"market:snapshot:{prefix.lower()}", f"market:snapshot:{prefix}"]


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def aggregate_quote_sources(
    rows: list[dict[str, Any]],
    *,
    now_ts: float,
    policy: FreshnessPolicy,
    requested: int | None = None,
) -> dict[str, Any]:
    """快照行（含 ``source`` / ``timestamp``）→ 按源聚合（纯函数，便于盯口径）。

    ``dominant`` = 覆盖持仓最多的源（并列时取更新的）——页面主标签就显示它，
    其余源作为「同池还有谁在写」如实并列，不再用一个笼统的「行情来源」糊过去。
    """
    per_source: dict[str, dict[str, Any]] = {}
    missing = 0
    for row in rows:
        if not row:
            missing += 1
            continue
        source = str(row.get("source") or "").strip() or UNKNOWN_SOURCE
        ts = _to_float(row.get("timestamp"))
        # 年龄不夹逼：负值=跨机时钟偏斜，交给唯一谓词判（≤5s 判 fresh，超出判 unavailable），
        # 与 stream 侧 classify_ts 同口径；自行 max(0,·) 会把时钟异常洗成 fresh。
        age_sec = None if ts is None or ts <= 0 else now_ts - ts
        level = policy.classify(age_sec)
        entry = per_source.setdefault(
            source,
            {
                "source": source,
                "label": quote_source_label(source),
                "count": 0,
                "newest_age_sec": None,
                "level": UNAVAILABLE,
            },
        )
        entry["count"] += 1
        newest = entry["newest_age_sec"]
        if age_sec is not None and (newest is None or age_sec < newest):
            entry["newest_age_sec"] = round(age_sec, 1)
            entry["level"] = level

    sources = sorted(
        per_source.values(),
        key=lambda item: (
            -item["count"],
            item["newest_age_sec"]
            if item["newest_age_sec"] is not None
            else float("inf"),
        ),
    )
    dominant = sources[0] if sources else None
    return {
        "requested": len(rows) if requested is None else requested,
        "missing": missing,
        "dominant": dominant["source"] if dominant else None,
        "dominant_label": dominant["label"] if dominant else None,
        "level": dominant["level"] if dominant else UNAVAILABLE,
        "newest_age_sec": dominant["newest_age_sec"] if dominant else None,
        "sources": sources,
        "as_of": now_ts,
        "error": None,
    }


def _default_client_getter() -> Any:
    """行情快照 Redis（与写侧同一份配置：REMOTE_QUOTE_REDIS_* → 交易 Redis 兜底）。"""
    from backend.services.live_trading.routers.real_trading_utils import (
        _get_stream_series_redis_client,
    )

    client, _host, _port = _get_stream_series_redis_client()
    return client


def collect_quote_sources(
    symbols: Iterable[str],
    *,
    limit: int = DEFAULT_SAMPLE_LIMIT,
    client_getter: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """采样给定标的的 ``market:snapshot`` → 聚合真实供数源（同步，供状态端点调用）。

    读不到就是读不到：Redis 异常时在 ``error`` 里如实报出，绝不返回一个
    「看起来是 fresh」的空聚合（那正是老来源条的毛病）。
    """
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in symbols:
        symbol = str(raw or "").strip()
        if not symbol:
            continue
        prefix = symbol.upper()
        if prefix in seen:
            continue
        seen.add(prefix)
        cleaned.append(symbol)
    cleaned = cleaned[:limit]
    now_ts = time.time()
    policy = quote_policy()
    if not cleaned:
        return aggregate_quote_sources([], now_ts=now_ts, policy=policy, requested=0)

    key_plan: list[list[str]] = [snapshot_key_candidates(sym) for sym in cleaned]
    flat_keys = [key for candidates in key_plan for key in candidates]
    if not flat_keys:
        return aggregate_quote_sources([], now_ts=now_ts, policy=policy, requested=len(cleaned))

    try:
        getter = client_getter or _default_client_getter
        client = getter()
        with client.pipeline(transaction=False) as pipe:
            for key in flat_keys:
                pipe.hgetall(key)
            results = pipe.execute()
    except Exception as exc:  # noqa: BLE001 - 端点如实标注，不吞
        result = aggregate_quote_sources(
            [], now_ts=now_ts, policy=policy, requested=len(cleaned)
        )
        result["error"] = f"quote_redis_unavailable: {exc}"
        return result

    rows: list[dict[str, Any]] = []
    cursor = 0
    for candidates in key_plan:
        found: dict[str, Any] = {}
        for offset in range(len(candidates)):
            candidate = results[cursor + offset]
            if candidate:
                found = dict(candidate)
                break
        cursor += len(candidates)
        rows.append(found)
    return aggregate_quote_sources(rows, now_ts=now_ts, policy=policy, requested=len(cleaned))
