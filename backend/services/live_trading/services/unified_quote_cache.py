# -*- coding: utf-8 -*-
"""统一行情（V2）：TDX 主 + QMT 备，按新鲜度/完整度合并最新快照。

这是**新增**的内部读面；现有 `quote_cache_reader` / TDX / QMT 接口不变。
V2 先覆盖「最新价 + 一档盘口」的合并与交叉校验；1m/tick 历史仍走
QMT 今日缓存 / TDX L2 / QuantDB。
"""

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)

DEFAULT_TDX_MAX_AGE_S = 3.0
DEFAULT_QMT_MAX_AGE_S = 5.0
DEFAULT_DIVERGENCE_PCT = 1.0

TDX_SOURCE_KEY = "market:snapshot:tdx:%s"
QMT_SOURCE_KEY = "market:snapshot:qmt:%s"
MERGED_KEY = "market:snapshot:merged:%s"


def tdx_snapshot_keys(code: str) -> tuple[str, str]:
    """代码 → (TDX 来源键, TDX canonical 键)，均为小写前缀式。"""
    from backend.shared.stock_utils import StockCodeUtil

    raw = str(code or "").strip().upper()
    prefix = StockCodeUtil.to_prefix(raw) if "." in raw else raw
    prefix = str(prefix or "").strip().lower()
    if not prefix:
        return "", ""
    return TDX_SOURCE_KEY % prefix, "market:snapshot:%s" % prefix


def read_tdx_snapshots(symbols, *, client_factory=None) -> dict[str, dict]:
    """读 TDX 来源键（缺失回落 canonical），返回以**后缀式**为键的原始 hash。

    TDX 行情在 stream/quote Redis（与桥 db5 不同库）；读取失败只返回空。
    """
    codes = [_symbol(code) for code in (symbols or []) if _symbol(code)]
    if not codes:
        return {}
    client = None
    try:
        if client_factory is None:
            from backend.services.live_trading.routers.real_trading_utils import (
                _get_stream_series_redis_client,
            )

            client, _host, _port = _get_stream_series_redis_client()
        else:
            client = client_factory()
        pipe = client.pipeline(transaction=False)
        keys: list[tuple[str, str, str]] = []
        for code in codes:
            source_key, canonical_key = tdx_snapshot_keys(code)
            if not source_key:
                continue
            pipe.hgetall(source_key)
            pipe.hgetall(canonical_key)
            keys.append((code, source_key, canonical_key))
        values = pipe.execute()
        out: dict[str, dict] = {}
        for index, (code, _source_key, _canonical_key) in enumerate(keys):
            source_row = values[index * 2] or {}
            canonical_row = values[index * 2 + 1] or {}
            row = source_row or canonical_row
            if isinstance(row, dict) and row:
                row = dict(row)
                row.setdefault("source", "tdx")
                out[code] = row
        return out
    except Exception as exc:  # noqa: BLE001 - TDX 不可用只降级
        logger.warning("[UnifiedQuote] TDX 来源键读取失败: %s", exc)
        return {}
    finally:
        try:
            if client is not None:
                client.close()
        except Exception:  # noqa: BLE001
            pass


def _f(value, default=None):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _symbol(code: str) -> str:
    from backend.shared.stock_utils import StockCodeUtil

    raw = str(code or "").strip().upper()
    if not raw:
        return ""
    return raw if "." in raw else StockCodeUtil.to_suffix(raw).upper()


def _tdx_latest(raw: dict, now: float, max_age_s: float) -> dict | None:
    if not isinstance(raw, dict):
        return None
    source = str(raw.get("source") or "")
    if "qmt" in source.lower():
        return None  # canonical key 被 QMT 覆盖时，不能当 TDX 用
    last = _f(raw.get("last"))
    if last is None:
        last = _f(raw.get("price"))
    ts = _f(raw.get("ts"), 0.0) or 0.0
    if not last or last <= 0 or ts <= 0 or now - ts > max_age_s:
        return None
    return {
        "last": last,
        "ts": ts,
        "bid1": _f(raw.get("bid1_p")),
        "ask1": _f(raw.get("ask1_p")),
        "source": source or "tdx",
    }


def _qmt_latest(columns: dict, now: float, max_age_s: float) -> dict | None:
    if not isinstance(columns, dict) or columns.get("stale"):
        return None
    lasts = columns.get("last") or []
    times = columns.get("ts_ms") or []
    if not lasts or not times:
        return None
    last = _f(lasts[-1])
    ts_ms = _f(times[-1], 0.0) or 0.0
    ts = ts_ms / 1000.0
    if not last or last <= 0 or ts <= 0 or now - ts > max_age_s:
        return None
    bids = columns.get("bid1") or []
    asks = columns.get("ask1") or []
    return {
        "last": last,
        "ts": ts,
        "bid1": _f(bids[-1]) if bids else None,
        "ask1": _f(asks[-1]) if asks else None,
        "source": "qmt",
    }


def merge_latest(
    tdx_rows: dict,
    qmt_columns: dict,
    *,
    now: float | None = None,
    tdx_max_age_s: float = DEFAULT_TDX_MAX_AGE_S,
    qmt_max_age_s: float = DEFAULT_QMT_MAX_AGE_S,
    divergence_pct: float = DEFAULT_DIVERGENCE_PCT,
) -> dict[str, dict]:
    """合并 TDX/QMT 最新快照（纯函数）。

    * 都新鲜：TDX 优先（10 档/L2），价差 > ``divergence_pct`` 标 divergent；
    * 仅一方新鲜：用新鲜方；
    * 都陈旧/缺失：不返回该标的（调用方显式处理 missing）。
    """
    now = time.time() if now is None else float(now)
    tdx_rows = {
        _symbol(code): row for code, row in (tdx_rows or {}).items() if _symbol(code)
    }
    qmt_columns = {
        _symbol(code): row for code, row in (qmt_columns or {}).items() if _symbol(code)
    }
    symbols = set(tdx_rows) | set(qmt_columns)
    out: dict[str, dict] = {}
    for symbol in symbols:
        tdx = _tdx_latest(tdx_rows.get(symbol), now, tdx_max_age_s)
        qmt = _qmt_latest(qmt_columns.get(symbol), now, qmt_max_age_s)
        if tdx is None and qmt is None:
            continue
        chosen = tdx or qmt
        sources = [name for name, item in (("tdx", tdx), ("qmt", qmt)) if item]
        quality = "ok"
        divergence = None
        if tdx is not None and qmt is not None:
            base = max(abs(tdx["last"]), abs(qmt["last"]), 1e-9)
            diff_pct = abs(tdx["last"] - qmt["last"]) / base * 100.0
            if diff_pct > float(divergence_pct):
                quality = "divergent"
                divergence = round(diff_pct, 3)
        out[symbol] = {
            "last": chosen["last"],
            "ts": chosen["ts"],
            "source": chosen["source"],
            "bid1": chosen.get("bid1"),
            "ask1": chosen.get("ask1"),
            "quality": quality,
            "sources": sources,
            "divergence_pct": divergence,
            "stale": False,
        }
    return out


def publish_merged(redis_client, merged: dict, *, ttl_s: int = 10) -> int:
    """把合并结果写入 ``market:snapshot:merged:{symbol}``（可选，调用方注入 Redis）。"""
    if not merged:
        return 0
    pipe = redis_client.pipeline(transaction=False)
    count = 0
    for symbol, row in merged.items():
        key = MERGED_KEY % symbol
        mapping = {
            "last": row.get("last"),
            "ts": row.get("ts"),
            "source": row.get("source"),
            "quality": row.get("quality"),
            "sources": ",".join(row.get("sources") or []),
            "divergence_pct": row.get("divergence_pct"),
            "bid1": row.get("bid1"),
            "ask1": row.get("ask1"),
        }
        pipe.hset(key, mapping=mapping)
        pipe.expire(key, int(ttl_s))
        count += 1
    pipe.execute()
    return count


async def latest_quotes(
    symbols,
    *,
    tdx_max_age_s: float = DEFAULT_TDX_MAX_AGE_S,
    qmt_max_age_s: float = DEFAULT_QMT_MAX_AGE_S,
    divergence_pct: float = DEFAULT_DIVERGENCE_PCT,
) -> dict[str, dict]:
    """TDX（canonical snapshot）+ QMT 热集 tick → 合并最新快照。

    任一源不可用都只降级，不抛异常；缺失标的不会出现在返回里。
    """
    codes = [str(code) for code in (symbols or []) if str(code).strip()]
    if not codes:
        return {}
    tdx_rows: dict = {}
    qmt_columns: dict = {}
    try:
        tdx_rows = await asyncio.to_thread(read_tdx_snapshots, codes)
    except Exception as exc:  # noqa: BLE001 - TDX 不可用只降级
        logger.warning("[UnifiedQuote] TDX 读快照失败: %s", exc)
    try:
        from backend.services.live_trading.services import quote_cache_reader

        # 桥 db5 canonical 后缀键：QMT 写入；TDX 前缀键在上面的 TDX 库读取。
        qmt_rows = await quote_cache_reader.read_snapshots(codes)
        qmt_columns = {
            _symbol(code): {
                "last": [_f(row.get("last"))],
                "ts_ms": [int(_f(row.get("ts"), 0.0) * 1000)],
                "bid1": [_f(row.get("bid1_p"))],
                "ask1": [_f(row.get("ask1_p"))],
                "stale": False,
            }
            for code, row in (qmt_rows or {}).items()
            if isinstance(row, dict)
        }
    except Exception as exc:  # noqa: BLE001 - QMT 缓存不可用只降级
        logger.warning("[UnifiedQuote] QMT 读快照失败: %s", exc)
    try:
        from backend.services.live_trading.services import qmt_today_cache_client

        hot = await asyncio.to_thread(qmt_today_cache_client.get_hot_ticks, codes)
        for code, columns in (hot or {}).items():
            key = _symbol(code)
            # 热集 tick 已 stale 时不要覆盖桥 canonical 里可能更新的快照。
            if columns.get("stale") and key in qmt_columns:
                continue
            qmt_columns[key] = columns
    except Exception as exc:  # noqa: BLE001 - QMT 热集不可用只降级
        logger.warning("[UnifiedQuote] QMT 热集读取失败: %s", exc)
    return merge_latest(
        tdx_rows,
        qmt_columns,
        now=time.time(),
        tdx_max_age_s=tdx_max_age_s,
        qmt_max_age_s=qmt_max_age_s,
        divergence_pct=divergence_pct,
    )
