# -*- coding: utf-8 -*-
"""桥侧「今日行情缓存」只读客户端（V1）。

这是**新增**的内部接口，不改现有 QMT/TDX/HTTP 接口；调用方拿不到缓存时
应回落到 QMT 管道或 QuantDB。
"""

from __future__ import annotations

import logging

from backend.scripts.qmt_api.client import QmtError, client

logger = logging.getLogger(__name__)


def cache_status(timeout: float = 2.0) -> dict:
    """桥缓存状态（命中率/内存/落盘队列）。失败返回空 dict。"""
    try:
        rpc = client("rpc", timeout=timeout)
        data = rpc.call("cache_status", {})
        return data if isinstance(data, dict) else {}
    except QmtError as exc:
        logger.warning("[QmtTodayCache] status 失败 code=%s: %s", exc.code, exc)
    except Exception as exc:  # noqa: BLE001 - 缓存只读，失败只降级
        logger.warning("[QmtTodayCache] status 异常: %s", exc)
    return {}


def get_today_1m(
    stock_list,
    *,
    start_time: str | None = None,
    end_time: str | None = None,
    timeout: float = 3.0,
) -> dict:
    """当天 1m bar（桥内存/Parquet 直出，列式）。

    返回 ``{code: {minute_ts: [...], open: [...], high: [...], low: [...],
    close: [...], volume: [...], amount: [...], vwap: [...],
    cum_volume: [...], cum_amount: [...], source: [...], final: [...]}}``。
    列式比 list-of-dicts 快一个量级、JSON 更小；调用方按列取值即可。
    单次最多 600 只（桥侧上限），超过时桥返回 ``ok=False``，调用方应分批。
    失败/未命中返回空 dict，调用方回落 QMT/QuantDB。
    """
    codes = [str(code).strip() for code in (stock_list or []) if str(code).strip()]
    if not codes:
        return {}
    params = {"stock_list": codes}
    if start_time:
        params["start_time"] = start_time
    if end_time:
        params["end_time"] = end_time
    try:
        rpc = client("rpc", timeout=timeout)
        data = rpc.call("cache_today_1m", params)
        if isinstance(data, dict):
            return data
        logger.warning("[QmtTodayCache] today_1m 返回类型异常: %r", type(data))
    except QmtError as exc:
        logger.warning("[QmtTodayCache] today_1m 失败 code=%s: %s", exc.code, exc)
    except Exception as exc:  # noqa: BLE001 - 缓存只读，失败只降级
        logger.warning("[QmtTodayCache] today_1m 异常: %s", exc)
    return {}


def get_hot_ticks(
    stock_list,
    *,
    start_time: str | None = None,
    end_time: str | None = None,
    timeout: float = 2.0,
) -> dict:
    """热集 tick（列式）：`{code: {ts_ms, last, volume, ..., stale, age_ms, gap}}`。

    单次最多 800 只；stale=True 的标的应由调用方用 TDX 快照补充。
    """
    codes = [str(code).strip() for code in (stock_list or []) if str(code).strip()]
    if not codes:
        return {}
    params = {"stock_list": codes}
    if start_time:
        params["start_time"] = start_time
    if end_time:
        params["end_time"] = end_time
    try:
        rpc = client("rpc", timeout=timeout)
        data = rpc.call("cache_hot_ticks", params)
        if isinstance(data, dict):
            return data
        logger.warning("[QmtTodayCache] hot_ticks 返回类型异常: %r", type(data))
    except QmtError as exc:
        logger.warning("[QmtTodayCache] hot_ticks 失败 code=%s: %s", exc.code, exc)
    except Exception as exc:  # noqa: BLE001 - 缓存只读，失败只降级
        logger.warning("[QmtTodayCache] hot_ticks 异常: %s", exc)
    return {}
