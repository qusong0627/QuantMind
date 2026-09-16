"""模拟盘对账作业：Redis 账户 vs PG ledger 台账投影（确权：台账为主）。

SIM_RECONCILE_AUTOFIX 默认 true：按台账投影回填 Redis（持久化对账语义）。
主入口是权益结算 worker 的 30s 周期调用；每日 03:20 的独立 worker 已下线。
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from backend.services.trade_shared.redis_client import RedisClient

logger = logging.getLogger(__name__)

_SH_TZ = ZoneInfo("Asia/Shanghai")
_DIFF_TOL = 0.01

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS simulation_reconcile_reports (
    id SERIAL PRIMARY KEY,
    checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    tenant_id VARCHAR(64) NOT NULL DEFAULT 'default',
    user_id VARCHAR(64) NOT NULL,
    market VARCHAR(16) NOT NULL DEFAULT 'CN',
    field VARCHAR(32) NOT NULL,
    symbol VARCHAR(32) NOT NULL DEFAULT '',
    redis_value DOUBLE PRECISION NOT NULL DEFAULT 0,
    pg_value DOUBLE PRECISION NOT NULL DEFAULT 0,
    diff DOUBLE PRECISION NOT NULL DEFAULT 0,
    autofixed BOOLEAN NOT NULL DEFAULT FALSE
)
"""


_table_ensured = False

# 当日已写过 clean 行的 (date, tenant, user, market)；进程内去重——30s 周期不落重复证据，
# 重启后最多多写一行（幂等无害）。
_clean_marked: set[tuple[str, str, str, str]] = set()


def classify_reconcile_outcome(rebuilt: bool, diff_count: int) -> str:
    """对账结果分类（纯函数）：ledger_empty（PG 台账空，显式可见）/ clean / diff。"""
    if not rebuilt:
        return "ledger_empty"
    return "diff" if diff_count > 0 else "clean"


async def _ensure_table() -> None:
    """确保对账报告表存在（CREATE IF NOT EXISTS）。

    进程内只跑一次：权益结算 worker 每 30s 调一次 run_reconcile_once，
    逐周期重复 DDL 是纯冗余往返。失败不置位，下周期重试自愈。
    """
    global _table_ensured
    if _table_ensured:
        return
    from sqlalchemy import text as _text

    from backend.shared.database_manager_v2 import get_session as _get_session

    async with _get_session() as session:
        await session.execute(_text(_CREATE_TABLE_SQL))
        await session.commit()
    _table_ensured = True


def _positions_by_symbol(
    positions: Any, field: str = "volume"
) -> dict[str, float]:
    out: dict[str, float] = {}
    if not isinstance(positions, dict):
        return out
    for key, pos in positions.items():
        if not isinstance(pos, dict):
            continue
        code = str(key).split("::", 1)[0].strip().upper()
        if not code:
            continue
        out[code] = out.get(code, 0.0) + float(pos.get(field) or 0)
    return out


async def _write_clean_row(
    *, tenant_id: str, user_id: str, market: str
) -> None:
    """零差异时写当日"clean"证据行（每日每账户一行）——健康也留痕。

    证据矩阵要求"账本 | 对账零差异 | 对账报告 | 每日"：不落行时 C06 无法区分
    "跑过且干净"与"从未跑过"（恒 warn"无对账报告"）。
    """
    today = datetime.now(_SH_TZ).strftime("%Y%m%d")
    key = (today, tenant_id, user_id, market)
    if key in _clean_marked:
        return
    from sqlalchemy import text as _text

    from backend.shared.database_manager_v2 import get_session as _get_session

    async with _get_session() as session:
        await session.execute(
            _text(
                "INSERT INTO simulation_reconcile_reports "
                "(tenant_id, user_id, market, field, symbol, "
                "redis_value, pg_value, diff, autofixed) "
                "VALUES (:tid, :uid, :mkt, 'clean', '', 0, 0, 0, false)"
            ),
            {"tid": tenant_id, "uid": user_id, "mkt": market},
        )
        await session.commit()
    _clean_marked.add(key)


async def run_reconcile_once(
    redis: RedisClient, *, autofix: bool = False
) -> dict[str, Any]:
    """执行一次全量对账，返回统计。永不抛异常。"""
    from sqlalchemy import text as _text

    from backend.services.trade_shared.simulation_manager import (
        SimulationAccountManager,
    )
    from backend.shared.database_manager_v2 import get_session as _get_session

    stats = {"checked": 0, "diff_fields": 0, "autofixed": 0, "clean": 0, "ledger_empty": 0}
    if not redis.client:
        return stats
    try:
        await _ensure_table()
    except Exception as exc:
        logger.warning("reconcile ensure table failed: %s", exc)
        return stats

    manager = SimulationAccountManager(redis)
    try:
        keys = await asyncio.to_thread(
            lambda: list(
                redis.client.scan_iter(match="simulation:account:*", count=500)
            )
        )
    except Exception as exc:
        logger.warning("reconcile scan failed: %s", exc)
        return stats

    for raw_key in keys:
        key = str(raw_key)
        parts = key.split(":")
        if len(parts) < 4:
            continue
        tenant_id = parts[2].strip() or "default"
        user_raw = parts[3].strip()
        market = parts[4].strip().upper() if len(parts) > 4 else "CN"
        if not user_raw.isdigit():
            continue
        user_id = int(user_raw)
        stats["checked"] += 1
        try:
            import json as _json

            live_raw = await asyncio.to_thread(redis.client.get, key)
            live = _json.loads(live_raw) if live_raw else {}
            if not isinstance(live, dict):
                live = {}
            rebuilt = await manager._rebuild_from_ledger(user_id, tenant_id, market)
            outcome = classify_reconcile_outcome(bool(rebuilt), 0)
            if outcome == "ledger_empty":
                # Redis 有账但 PG 台账空（历史 Redis-only 时代遗留）：显式计数，不静默跳过
                stats["ledger_empty"] += 1
                continue
            diffs: list[dict[str, Any]] = []
            cash_diff = float(live.get("cash") or 0) - float(rebuilt.get("cash") or 0)
            if abs(cash_diff) > _DIFF_TOL:
                diffs.append({
                    "field": "cash", "symbol": "",
                    "redis_value": float(live.get("cash") or 0),
                    "pg_value": float(rebuilt.get("cash") or 0),
                    "diff": cash_diff,
                })
            live_pos = _positions_by_symbol(live.get("positions"))
            pg_pos = _positions_by_symbol(rebuilt.get("positions"))
            for code in sorted(set(live_pos) | set(pg_pos)):
                d = live_pos.get(code, 0.0) - pg_pos.get(code, 0.0)
                if abs(d) > _DIFF_TOL:
                    diffs.append({
                        "field": "volume", "symbol": code,
                        "redis_value": live_pos.get(code, 0.0),
                        "pg_value": pg_pos.get(code, 0.0),
                        "diff": d,
                    })
            live_available = _positions_by_symbol(
                live.get("positions"), "available_volume"
            )
            pg_available = _positions_by_symbol(
                rebuilt.get("positions"), "available_volume"
            )
            for code in sorted(set(live_available) | set(pg_available)):
                d = live_available.get(code, 0.0) - pg_available.get(code, 0.0)
                if abs(d) > _DIFF_TOL:
                    diffs.append(
                        {
                            "field": "available_volume",
                            "symbol": code,
                            "redis_value": live_available.get(code, 0.0),
                            "pg_value": pg_available.get(code, 0.0),
                            "diff": d,
                        }
                    )
            if not diffs:
                # T-P2-06：零差异也落当日证据行（每日一行，进程内去重）
                stats["clean"] += 1
                try:
                    await _write_clean_row(
                        tenant_id=tenant_id, user_id=user_raw, market=market
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("reconcile clean 行写入失败: %s", exc)
                continue
                continue
            stats["diff_fields"] += len(diffs)
            fixed = False
            if autofix:
                try:
                    from backend.shared.trade_account_cache import write_json_cache

                    await asyncio.to_thread(write_json_cache, redis, key, rebuilt)
                    fixed = True
                    stats["autofixed"] += 1
                except Exception as exc:
                    logger.warning("reconcile autofix failed %s: %s", key, exc)
            try:
                async with _get_session() as session:
                    for d in diffs:
                        await session.execute(
                            _text(
                                "INSERT INTO simulation_reconcile_reports "
                                "(tenant_id, user_id, market, field, symbol, "
                                "redis_value, pg_value, diff, autofixed) "
                                "VALUES (:tid, :uid, :mkt, :field, :sym, :rv, :pv, :df, :fx)"
                            ),
                            {
                                "tid": tenant_id,
                                "uid": user_raw,
                                "mkt": market,
                                "field": d["field"],
                                "sym": d["symbol"],
                                "rv": d["redis_value"],
                                "pv": d["pg_value"],
                                "df": d["diff"],
                                "fx": fixed,
                            },
                        )
                    await session.commit()
            except Exception as exc:
                logger.warning("reconcile report write failed: %s", exc)
        except Exception as exc:
            logger.debug("reconcile skipped %s: %s", key, exc)
            continue
    log = logger.info if stats["diff_fields"] else logger.debug
    log(
        "simulation reconcile done: checked=%d diff_fields=%d autofixed=%d "
        "clean=%d ledger_empty=%d",
        stats["checked"],
        stats["diff_fields"],
        stats["autofixed"],
        stats["clean"],
        stats["ledger_empty"],
    )
    return stats


def autofix_enabled() -> bool:
    # 默认 true：权益结算 worker 每 30s 以 PG 台账为准确权 Redis（持久化对账）。
    return os.getenv("SIM_RECONCILE_AUTOFIX", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


async def run_simulation_reconcile_worker() -> None:
    """每天 03:20 跑一次对账（EOD 03:05 之后）。"""
    last_run_date = ""
    while True:
        try:
            now = datetime.now(_SH_TZ)
            today = now.strftime("%Y%m%d")
            if (
                today != last_run_date
                and (now.hour, now.minute) >= (3, 20)
                and now.weekday() < 5
            ):
                last_run_date = today
                from backend.services.trade_shared.redis_client import redis_client

                await run_reconcile_once(redis_client, autofix=autofix_enabled())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("simulation reconcile worker failed: %s", exc)
        await asyncio.sleep(300)
