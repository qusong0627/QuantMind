"""盘中全市场结构/量能采集（TDX 免费桥，trade 常驻任务）。

**只读免费通道**：``/api/v1/tdx/call`` 的 ``get_pricevol`` / ``get_more_info`` /
``get_market_data`` / ``get_stock_list`` / ``get_stock_list_in_sector``，不碰
TdxAiData 付费通道。产出写交易库 db2：

* ``qm:intraday:market:{YYYYMMDD}``（Hash，``doc`` = 提示词块 JSON）
* ``qm:intraday:events:{YYYYMMDD}``（Stream，涨跌停/炸板/量能异动）
* ``qm:intraday:extreme:{YYYYMMDD}``（Hash，当日采样极值，炸板判定用）
* ``qm:intraday:universe:{YYYYMMDD}`` / ``qm:intraday:boards:{YYYYMMDD}``
* ``qm:intraday:market:status``（Hash，体检）
* ``qm:intraday:sector:latest``（Hash，板块成分日缓存；15:35 后刷新）

口径与纪律：

1. **全市场**：``get_pricevol`` 一次 5,582 只（实测 3.1s），每 ``interval_sec``
   一轮；广度/涨跌停都基于全市场，不用热集样本冒充。
2. **炸板是采样口径**：靠每轮 ``Now`` 维护当日极值，两次轮询之间的秒级触板会漏；
   文档 ``broke_sampled=True`` 明示，不写成精确值。
3. **量比/涨速/资金净流入/委比/竞价**：``get_more_info`` 单股，**必须带 ``_nc``**
   绕开桥的 86400s 缓存（否则拿到的是当天第一次调用时的旧值，2026-11-16 实测
   ``get_exday_data`` 盘中全零就是缓存锁死）。只覆盖持仓+池，≤60 只，默认 120s 一轮。
4. **板块**：``get_stock_list(market=11/12, list_type=1)`` 取板块名 + 逐板块
   ``get_stock_list_in_sector`` 取成分，每日 15:05 后刷新一次（约 397 次桥调用，
   盘后预算内）。盘中只读缓存，缓存缺失时板块段不渲染。
5. 任何一块取不到 → 文档缺该段/值写 ``None``，渲染层显示 ``—``；绝不补 0。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, time as dt_time
from typing import Any

import redis as redis_lib

from backend.services.live_trading.services.tdx_push_service import tdx_pusher
from backend.services.live_trading.services.trading_session import (
    TZ,
    is_continuous_trading_time,
    is_trading_time,
)
from backend.shared.decision_context_source import read_snapshots
from backend.shared.env_flags import env_flag
from backend.shared.intraday_market_calc import (
    compute_breadth,
    compute_metrics,
    compute_sector_strength,
    count_consecutive_boards,
    detect_events,
    is_st_name,
    qmt_tick_to_snapshot,
)
from backend.shared.remote_quote_config import make_sync_client
from backend.shared.stock_utils import StockCodeUtil

logger = logging.getLogger(__name__)

MARKET_KEY = "qm:intraday:market:{day}"
EVENT_KEY = "qm:intraday:events:{day}"
EXTREME_KEY = "qm:intraday:extreme:{day}"
UNIVERSE_KEY = "qm:intraday:universe:{day}"
BOARD_KEY = "qm:intraday:boards:{day}"
STATUS_KEY = "qm:intraday:market:status"
SECTOR_KEY = "qm:intraday:sector:latest"

_TTL_S = 2 * 24 * 3600
_SECTOR_TTL_S = 7 * 24 * 3600

#: 采集周期（秒）；全市场一次 pricevol ≈3s，60s 足够。
INTERVAL_S = max(20, int(os.getenv("QM_INTRADAY_WATCH_INTERVAL_S", "60") or 60))
#: more_info（量比/涨速/资金/委比）刷新周期；只覆盖持仓+池，避开 600/min 预算。
METRICS_INTERVAL_S = max(
    60, int(os.getenv("QM_INTRADAY_METRICS_INTERVAL_S", "120") or 120)
)
MAX_METRICS_CODES = max(1, int(os.getenv("QM_INTRADAY_METRICS_MAX", "40") or 40))
#: 每轮只刷新一批 ``more_info``（轮转覆盖），避免 40 只连续调用堵住主循环/撞桥限流。
METRICS_BATCH = max(1, int(os.getenv("QM_INTRADAY_METRICS_BATCH", "20") or 20))
#: 连板计算：当日涨停名单逐只取 20 根日线，只取一次/日。
MAX_BOARD_CODES = max(1, int(os.getenv("QM_INTRADAY_BOARDS_MAX", "120") or 120))
MORE_INFO_PACING_S = max(
    0.0, float(os.getenv("QM_INTRADAY_MORE_INFO_PACING_S", "0.8") or 0.8)
)
BOARD_PACING_S = max(
    0.0, float(os.getenv("QM_INTRADAY_BOARD_PACING_S", "0.15") or 0.15)
)
SECTOR_PACING_S = max(
    0.0, float(os.getenv("QM_INTRADAY_SECTOR_PACING_S", "0.2") or 0.2)
)
#: 连板计算每轮最多取几只日线（避免首轮 100+ 调用把桥配额打满；跨轮补齐）。
MAX_BOARDS_PER_CYCLE = max(
    1, int(os.getenv("QM_INTRADAY_BOARDS_PER_CYCLE", "8") or 8)
)
SECTOR_REFRESH_AT = dt_time(15, 5)
SECTOR_REFRESH_UNTIL = dt_time(16, 30)

#: Redis 写到提示词之间的最大允许年龄（秒）；超过则读侧返回 None，防旧文档冒充。
DOC_MAX_AGE_S = float(os.getenv("QM_INTRADAY_DOC_MAX_AGE_S", "900") or 900)

#: 涨停封单额：只取涨停梯队前 N 只（快照 1 档足够：封住时买一=涨停价）。
SEAL_TOP_N = max(0, int(os.getenv("QM_INTRADAY_SEAL_TOP_N", "10") or 10))
SEAL_REFRESH_S = max(30.0, float(os.getenv("QM_INTRADAY_SEAL_REFRESH_S", "120") or 120))
SEAL_PACING_S = max(0.2, float(os.getenv("QM_INTRADAY_SEAL_PACING_S", "0.6") or 0.6))
#: 真五档：QMT 伴生桥 `get_full_tick`（免费、5 档），写 db3 的
#: `market:snapshot:qmt:{suffix}`，由 ``decision_context_source.read_snapshots`` 优先读。
QMT_ENABLED = env_flag("QM_INTRADAY_QMT_ENABLED", default=True)
QMT_KEY = "market:snapshot:qmt:%s"
QMT_SNAPSHOT_TTL_S = 180

_REDIS: Any = None
_prev_breadth: dict[str, Any] | None = None
_last_more: dict[str, dict[str, Any]] = {}
_last_more_ts = 0.0
_metrics_cursor = 0
_last_extreme_save = 0.0
_event_seen: dict[tuple[str, str], float] = {}
_recent_events: list[dict[str, Any]] = []
_seal_cache: dict[str, tuple[float, float | None]] = {}
_targets_cache_ts = 0.0
_targets_cache_codes: list[str] = []
TARGETS_CACHE_S = max(
    60.0, float(os.getenv("QM_INTRADAY_TARGETS_CACHE_S", "300") or 300)
)
_status: dict[str, Any] = {
    "running": False,
    "last_cycle_at": None,
    "last_error": None,
    "breadth_n": 0,
    "limit_up": 0,
    "broke": 0,
    "metrics": 0,
    "sectors": 0,
    "universe": 0,
}


def _redis() -> Any:
    global _REDIS
    if _REDIS is None:
        _REDIS = redis_lib.Redis(
            host=os.getenv("REDIS_HOST") or "redis",
            port=int(os.getenv("REDIS_PORT", "6379")),
            password=os.getenv("REDIS_PASSWORD") or None,
            db=int(os.getenv("REDIS_DB_TRADE", "2")),
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=15,
        )
    return _REDIS


def _f(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and abs(out) != float("inf") else None


def _write_status(**fields: Any) -> None:
    _status.update(fields)
    try:
        _redis().hset(
            STATUS_KEY,
            mapping={
                key: ("" if value is None else str(value))
                for key, value in _status.items()
            },
        )
        _redis().expire(STATUS_KEY, _TTL_S)
    except Exception as exc:  # noqa: BLE001 - 状态写失败只记日志
        logger.debug("[IntradayWatch] 状态写失败: %s", exc)


# ---------------------------------------------------------------- 桥取数


async def load_universe(day: str, client: Any) -> dict[str, str]:
    """全 A 代码 → 名称（suffix 键）；按日缓存。"""
    key = UNIVERSE_KEY.format(day=day)
    cached = client.hgetall(key)
    if cached:
        return dict(cached)
    res = await tdx_pusher.tdx_call(
        "get_stock_list", {"market": 5, "list_type": 1}
    )
    rows = res.get("Value") if isinstance(res, dict) else None
    out: dict[str, str] = {}
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        code = str(row.get("Code") or "").strip()
        if code:
            out[code] = str(row.get("Name") or "")
    if out:
        client.hset(key, mapping=out)
        client.expire(key, _TTL_S)
    return out


async def load_pricevol(codes: list[str]) -> dict[str, dict[str, Any]]:
    """全市场快照（一次调用）。失败返回空表，不抛。"""
    if not codes:
        return {}
    try:
        res = await tdx_pusher.tdx_call("get_pricevol", {"stock_list": codes})
    except Exception as exc:  # noqa: BLE001 - 桥失败按不可得降级
        logger.warning("[IntradayWatch] get_pricevol 失败: %s", exc)
        return {}
    value = res.get("Value") if isinstance(res, dict) else None
    return value if isinstance(value, dict) else {}


async def load_more_info(codes: list[str]) -> dict[str, dict[str, Any]]:
    """单股 92 字段（**带 _nc 绕缓存**）；只给持仓+池，失败逐只跳过。"""
    out: dict[str, dict[str, Any]] = {}
    for raw in codes:
        code = StockCodeUtil.to_suffix(raw)
        if not code:
            continue
        try:
            res = await tdx_pusher.tdx_call(
                "get_more_info", {"stock_code": code, "_nc": str(time.time())}
            )
            value = res.get("Value") if isinstance(res, dict) else None
            if isinstance(value, dict) and value:
                out[code] = value
        except Exception as exc:  # noqa: BLE001 - 单只失败不拦整轮
            logger.debug("[IntradayWatch] get_more_info %s 失败: %s", code, exc)
        await asyncio.sleep(MORE_INFO_PACING_S)
    return out


async def load_daily_bars(code: str) -> list[dict[str, Any]]:
    """20 根日线（连板计算）；失败返回空表。"""
    code = StockCodeUtil.to_suffix(code)
    if not code:
        return []
    try:
        res = await tdx_pusher.tdx_call(
            "get_market_data",
            {
                "stock_list": [code],
                "period": "1d",
                "count": 20,
                "dividend_type": "none",
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[IntradayWatch] 日线 %s 失败: %s", code, exc)
        return []
    value = res.get("Value") if isinstance(res, dict) else None
    row = (value or {}).get(code) if isinstance(value, dict) else None
    if not isinstance(row, dict):
        return []
    dates = row.get("Date") or []
    closes = row.get("Close") or []
    return [
        {"Date": str(d), "Close": c}
        for d, c in zip(dates, closes, strict=False)
    ]


async def attach_seals(
    breadth: dict[str, Any], now_ts: float
) -> None:
    """涨停梯队前 N 只补封单额（元）：封住时买一价×买一量×100。"""
    top = breadth.get("top_limit_up") or []
    for row in top[:SEAL_TOP_N]:
        code = str(row.get("code") or "")
        if not code:
            continue
        cached = _seal_cache.get(code)
        if cached is not None and now_ts - cached[0] < SEAL_REFRESH_S:
            row["seal"] = cached[1]
            continue
        seal: float | None = None
        try:
            res = await tdx_pusher.tdx_call(
                "get_market_snapshot", {"stock_code": code}
            )
            raw = res.get("Value") if isinstance(res, dict) and "Value" in res else res
            if isinstance(raw, list) and raw:
                raw = raw[0]
            if isinstance(raw, dict):
                bid_p = _f((raw.get("Buyp") or [None])[0])
                bid_v = _f((raw.get("Buyv") or [None])[0])
                if bid_p and bid_v and bid_p > 0 and bid_v > 0:
                    seal = bid_p * bid_v * 100.0
        except Exception as exc:  # noqa: BLE001 - 封单是增益面
            logger.debug("[IntradayWatch] 封单 %s 失败: %s", code, exc)
        _seal_cache[code] = (now_ts, seal)
        row["seal"] = seal
        await asyncio.sleep(SEAL_PACING_S)


def load_snapshots(codes: list[str]) -> dict[str, dict[str, Any]]:
    """读 db3 标准键快照（同步、失败空表）。"""
    if not codes:
        return {}
    try:
        client = make_sync_client()
        if client is None:
            return {}
        return dict(read_snapshots(client, codes, include_qmt=True) or {})
    except Exception as exc:  # noqa: BLE001 - 行情是增益面
        logger.debug("[IntradayWatch] 快照读取失败: %s", exc)
        return {}


async def load_qmt_ticks(codes: list[str]) -> dict[str, dict[str, Any]]:
    """QMT 伴生桥 ``get_full_tick``（免费真五档，一次批量 ≤100 只）。"""
    if not QMT_ENABLED or not codes:
        return {}
    qmt_codes: list[str] = []
    for raw in codes:
        code = StockCodeUtil.to_suffix(raw)
        if code and code not in qmt_codes:
            qmt_codes.append(code)
    if not qmt_codes:
        return {}
    try:
        result = await tdx_pusher.tdx_call(
            "get_full_tick", {"codes": qmt_codes}, source="qmt"
        )
    except Exception as exc:  # noqa: BLE001 - QMT 不可用回落既有快照
        logger.warning("[IntradayWatch] QMT get_full_tick 失败: %s", exc)
        return {}
    return result if isinstance(result, dict) else {}


def write_qmt_snapshots(ticks: dict[str, dict[str, Any]]) -> int:
    """QMT tick → db3 ``market:snapshot:qmt:{suffix}``（独立命名空间，不盖主源）。"""
    if not ticks:
        return 0
    client = None
    try:
        client = make_sync_client()
        if client is None:
            return 0
        pipe = client.pipeline(transaction=False)
        written = 0
        for code, tick in ticks.items():
            snap = qmt_tick_to_snapshot(tick, code)
            if not snap:
                continue
            suffix = StockCodeUtil.to_suffix(code)
            if not suffix:
                continue
            key = QMT_KEY % suffix.upper()
            pipe.hset(key, mapping={k: str(v) for k, v in snap.items()})
            pipe.expire(key, QMT_SNAPSHOT_TTL_S)
            written += 1
        pipe.execute()
        return written
    except Exception as exc:  # noqa: BLE001 - 写失败回落既有快照
        logger.warning("[IntradayWatch] QMT 快照写入失败: %s", exc)
        return 0
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------- 持久化


def load_extremes(
    client: Any, day: str
) -> tuple[dict[str, float], dict[str, float]]:
    """当日采样极值（炸板判定）；坏行跳过。"""
    raw = client.hgetall(EXTREME_KEY.format(day=day))
    max_map: dict[str, float] = {}
    min_map: dict[str, float] = {}
    for code, value in raw.items():
        try:
            doc = json.loads(value)
        except (TypeError, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        try:
            if doc.get("max") is not None:
                max_map[code] = float(doc["max"])
            if doc.get("min") is not None:
                min_map[code] = float(doc["min"])
        except (TypeError, ValueError):
            continue
    return max_map, min_map


def save_extremes(
    client: Any,
    day: str,
    max_map: dict[str, float],
    min_map: dict[str, float],
) -> None:
    """全市场极值落 Redis（5,582 个 field；默认每 5 分钟一次）。"""
    key = EXTREME_KEY.format(day=day)
    pipe = client.pipeline(transaction=False)
    for code, value in max_map.items():
        pipe.hset(
            key,
            code,
            json.dumps({"max": value, "min": min_map.get(code)}),
        )
    pipe.expire(key, _TTL_S)
    pipe.execute()


def _sector_cache(client: Any) -> dict[str, list[str]]:
    raw = client.hget(SECTOR_KEY, "data")
    if not raw:
        return {}
    try:
        doc = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(doc, dict):
        return {}
    return {
        str(name): [str(x) for x in codes if str(x)]
        for name, codes in doc.items()
        if isinstance(codes, list)
    }


async def refresh_sectors(client: Any) -> dict[str, list[str]]:
    """板块名 + 成分日缓存（397 次桥调用；只在盘后窗口跑）。"""
    names: dict[str, str] = {}
    for market in (11, 12):
        try:
            res = await tdx_pusher.tdx_call(
                "get_stock_list", {"market": market, "list_type": 1}
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[IntradayWatch] 板块名 market=%s 失败: %s", market, exc)
            continue
        rows = res.get("Value") if isinstance(res, dict) else None
        for row in rows or ():
            if isinstance(row, dict) and row.get("Code"):
                names[str(row["Code"])] = str(row.get("Name") or "")
    members: dict[str, list[str]] = {}
    for code, name in names.items():
        try:
            res = await tdx_pusher.tdx_call(
                "get_stock_list_in_sector",
                {"block_code": code, "block_type": 0},
            )
            value = res.get("Value") if isinstance(res, dict) else None
            if isinstance(value, list) and value:
                members[name] = [str(x) for x in value if str(x)]
        except Exception as exc:  # noqa: BLE001 - 单板块失败跳过
            logger.debug("[IntradayWatch] 板块 %s 失败: %s", code, exc)
        await asyncio.sleep(SECTOR_PACING_S)
    if members:
        client.hset(
            SECTOR_KEY,
            mapping={
                "date": datetime.now(TZ).strftime("%Y%m%d"),
                "ts": str(time.time()),
                "data": json.dumps(members, ensure_ascii=False),
            },
        )
        client.expire(SECTOR_KEY, _SECTOR_TTL_S)
    return members


def _sector_refresh_window(now: datetime) -> bool:
    return SECTOR_REFRESH_AT <= now.time() < SECTOR_REFRESH_UNTIL


# ---------------------------------------------------------------- 主循环


async def _load_boards(
    codes: list[str], names: dict[str, str], day: str, client: Any
) -> dict[str, int]:
    key = BOARD_KEY.format(day=day)
    cached = client.hgetall(key)
    out: dict[str, int] = {}
    for code, value in cached.items():
        try:
            out[code] = int(value)
        except (TypeError, ValueError):
            continue
    missing = [code for code in codes if code not in out][:MAX_BOARDS_PER_CYCLE]
    if not missing or len(out) >= MAX_BOARD_CODES:
        return out
    day_date = datetime.strptime(day, "%Y%m%d").date()
    for code in missing:
        bars = await load_daily_bars(code)
        boards = count_consecutive_boards(
            bars, code, is_st_name(names.get(code, "")), day_date
        )
        if boards is not None:
            out[code] = boards
            client.hset(key, code, boards)
        await asyncio.sleep(BOARD_PACING_S)
    client.expire(key, _TTL_S)
    return out


async def _metrics_codes(day: str) -> list[str]:
    """持仓 ∪ 当日池（与竞价任务同源）；5 分钟缓存，失败返回空表。"""
    global _targets_cache_ts, _targets_cache_codes
    now_ts = time.time()
    if _targets_cache_codes and now_ts - _targets_cache_ts < TARGETS_CACHE_S:
        return list(_targets_cache_codes)
    try:
        from backend.services.live_trading.services.call_auction_watch import (
            collect_targets,
        )

        targets, _errors = await collect_targets(day)
        codes = [str(code) for code in list(targets)[:MAX_METRICS_CODES]]
        _targets_cache_codes = codes
        _targets_cache_ts = now_ts
        return list(codes)
    except Exception as exc:  # noqa: BLE001 - 面失败只降级量能段
        logger.warning("[IntradayWatch] 持仓/池目标读取失败: %s", exc)
        return []


def _append_events(
    client: Any, day: str, events: list[dict[str, Any]]
) -> None:
    if not events:
        return
    key = EVENT_KEY.format(day=day)
    pipe = client.pipeline(transaction=False)
    for event in events:
        pipe.xadd(
            key,
            {
                "ts": event["ts"],
                "code": event.get("code") or "",
                "kind": event.get("kind") or "",
                "text": event.get("text") or "",
            },
            maxlen=500,
            approximate=True,
        )
    pipe.expire(key, _TTL_S)
    pipe.execute()


def _metric_events(
    metrics: dict[str, dict[str, Any]], names: dict[str, str], now_ts: float
) -> list[dict[str, Any]]:
    """量比≥2.5 且涨速≥1% 的池/持仓异动（同码 30 分钟去重）。"""
    events: list[dict[str, Any]] = []
    for prefix, row in metrics.items():
        vr = row.get("vr")
        speed = row.get("speed")
        if vr is None or speed is None or vr < 2.5 or speed < 1.0:
            continue
        key = (prefix, "vol_speed")
        last = _event_seen.get(key)
        if last is not None and now_ts - last < 1800.0:
            continue
        _event_seen[key] = now_ts
        suffix = StockCodeUtil.to_suffix(prefix)
        name = str(row.get("name") or names.get(suffix) or "")
        label = f"{name}({prefix})" if name else prefix
        events.append(
            {
                "ts": now_ts,
                "code": suffix,
                "kind": "vol_speed",
                "text": f"{label} 量比 {vr:.2f} / 涨速 {speed:+.2f}%",
            }
        )
    return events


async def run_cycle(now: datetime | None = None) -> dict[str, Any]:
    """跑一轮并写 Redis；返回本轮文档（测试可直接调用纯逻辑部分）。"""
    global _prev_breadth, _last_more, _last_more_ts, _last_extreme_save
    global _recent_events, _metrics_cursor

    moment = now or datetime.now(TZ)
    day = moment.strftime("%Y%m%d")
    client = _redis()
    universe = await load_universe(day, client)
    if not universe:
        _write_status(last_error="universe 为空（桥未连通）", running=True)
        return {}
    rows = await load_pricevol(list(universe))
    if not rows:
        _write_status(last_error="get_pricevol 为空", running=True)
        return {}

    max_map, min_map = load_extremes(client, day)
    breadth, max_map, min_map = compute_breadth(
        rows, universe, moment.date(), max_map, min_map
    )
    trading = is_trading_time(moment)
    boards: dict[str, int] = {}
    if trading:
        boards = await _load_boards(
            [row["code"] for row in breadth.get("top_limit_up", [])],
            universe,
            day,
            client,
        )
        for row in breadth.get("top_limit_up", []):
            if row["code"] in boards:
                row["boards"] = boards[row["code"]]

    now_ts = time.time()
    if trading and SEAL_TOP_N > 0:
        await attach_seals(breadth, now_ts)
    if now_ts - _last_extreme_save >= 300.0:
        save_extremes(client, day, max_map, min_map)
        _last_extreme_save = now_ts

    members = _sector_cache(client)
    if (
        not members
        and env_flag("QM_INTRADAY_SECTOR_ENABLED", default=True)
        and _sector_refresh_window(moment)
    ):
        members = await refresh_sectors(client)
    sectors = (
        compute_sector_strength(rows, members, breadth.get("limit_up_codes", []))
        if members
        else []
    )

    metrics: dict[str, dict[str, Any]] = {}
    if trading:
        codes = await _metrics_codes(day)
        if codes:
            qmt_ticks = await load_qmt_ticks(codes)
            if qmt_ticks:
                write_qmt_snapshots(qmt_ticks)
            snaps = load_snapshots(codes)
            for code, tick in qmt_ticks.items():
                snap = qmt_tick_to_snapshot(tick, code)
                if snap:
                    snaps[StockCodeUtil.to_suffix(code)] = snap
            if now_ts - _last_more_ts >= METRICS_INTERVAL_S:
                start = _metrics_cursor % len(codes)
                batch = codes[start : start + METRICS_BATCH]
                if not batch:
                    batch = codes[:METRICS_BATCH]
                _last_more.update(await load_more_info(batch))
                _metrics_cursor = (start + METRICS_BATCH) % len(codes)
                _last_more_ts = now_ts
            metrics = compute_metrics(codes, universe, _last_more, snaps)

    events = _metric_events(metrics, universe, now_ts)
    if _prev_breadth is not None:
        events = detect_events(_prev_breadth, breadth, now_ts, universe) + events
    _prev_breadth = dict(breadth)
    _append_events(client, day, events)
    if events:
        _recent_events = (list(_recent_events) + events)[-30:]
    doc = {
        "ts": now_ts,
        "source": "tdx_bridge",
        "phase": (
            "continuous"
            if is_continuous_trading_time(moment)
            else "non_continuous"
        ),
        "breadth": breadth,
        "sectors": sectors,
        "metrics": metrics,
        "events": list(_recent_events)[-20:],
    }
    client.hset(
        MARKET_KEY.format(day=day),
        mapping={"doc": json.dumps(doc, ensure_ascii=False), "ts": now_ts},
    )
    client.expire(MARKET_KEY.format(day=day), _TTL_S)
    _write_status(
        running=True,
        last_cycle_at=moment.isoformat(),
        last_error=None,
        breadth_n=breadth.get("n", 0),
        limit_up=breadth.get("limit_up", 0),
        broke=breadth.get("broke", 0),
        metrics=len(metrics),
        sectors=len(sectors),
        universe=len(universe),
    )
    return doc


def _watch_window(now: datetime) -> bool:
    return is_trading_time(now) or _sector_refresh_window(now)


async def run_intraday_market_watch_task() -> None:
    """常驻循环：交易时段每 ``INTERVAL_S`` 一轮；盘后 15:05–16:30 补板块缓存。"""
    _write_status(running=True)
    logger.info("[IntradayWatch] 启动（interval=%ss）", INTERVAL_S)
    while True:
        try:
            now = datetime.now(TZ)
            if not _watch_window(now):
                await asyncio.sleep(30)
                continue
            await run_cycle(now)
        except asyncio.CancelledError:
            _write_status(running=False)
            raise
        except Exception as exc:  # noqa: BLE001 - 单轮失败不退出常驻任务
            logger.warning("[IntradayWatch] 本轮失败: %s", exc, exc_info=True)
            _write_status(last_error=f"{type(exc).__name__}: {exc}")
        await asyncio.sleep(INTERVAL_S)


__all__ = [
    "DOC_MAX_AGE_S",
    "MARKET_KEY",
    "STATUS_KEY",
    "compute_breadth",
    "load_extremes",
    "run_cycle",
    "run_intraday_market_watch_task",
]


if __name__ == "__main__":  # 手工单跑：python -m ... [--once|--refresh-sectors]
    import argparse

    parser = argparse.ArgumentParser(description="盘中全市场结构/量能采集")
    parser.add_argument("--once", action="store_true", help="只跑一轮后退出")
    parser.add_argument(
        "--refresh-sectors",
        action="store_true",
        help="只刷新板块成分日缓存（盘后手动补；约 397 次桥调用）",
    )
    _args = parser.parse_args()
    if _args.refresh_sectors:
        asyncio.run(refresh_sectors(_redis()))
    elif _args.once:
        asyncio.run(run_cycle(datetime.now(TZ)))
    else:
        asyncio.run(run_intraday_market_watch_task())
