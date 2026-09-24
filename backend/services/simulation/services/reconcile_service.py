"""模拟盘对账作业：Redis 账户 vs PG ledger 台账投影（确权：台账为主）。

SIM_RECONCILE_AUTOFIX 默认 true：按台账投影回填 Redis（持久化对账语义）。
主入口是权益结算 worker 的 30s 周期调用；每日 03:20 的独立 worker 已下线。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from backend.services.trade_shared.redis_client import RedisClient
from backend.shared.simulation_position_keys import SHORT, split_position_key

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

# 台账自身一致性审计节流（整表聚合，OSS 多账户时不必每 30s 跑）
_LEDGER_AUDIT_INTERVAL_SEC = 3600.0
_last_ledger_audit = 0.0


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
) -> dict[tuple[str, str], float]:
    """按 (代码, 方向) 汇总持仓字段。

    方向必须参与分组：多空同标的不汇总（否则一多一空净额会互相抵消，
    掩盖真实差异）。键形兼容 ``SYMBOL`` / ``SYMBOL::short`` / ``SYMBOL:short``。
    """
    out: dict[tuple[str, str], float] = {}
    if not isinstance(positions, dict):
        return out
    for key, pos in positions.items():
        if not isinstance(pos, dict):
            continue
        code, side = split_position_key(str(key))
        code = code.strip().upper()
        if not code:
            continue
        grouped_key = (code, side)
        out[grouped_key] = out.get(grouped_key, 0.0) + float(pos.get(field) or 0)
    return out


def _report_symbol(code: str, side: str) -> str:
    return code if side != SHORT else f"{code}::{SHORT}"


async def run_reconcile_once(
    redis: RedisClient, *, autofix: bool = False
) -> dict[str, Any]:
    """执行一次全量对账，返回统计。永不抛异常。"""
    from sqlalchemy import text as _text

    from backend.services.trade_shared.simulation_manager import (
        SimulationAccountManager,
    )
    from backend.shared.database_manager_v2 import get_session as _get_session

    stats = {"checked": 0, "diff_fields": 0, "autofixed": 0}
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

            from backend.services.simulation.services.projection_service import (
                SimulationProjectionService,
            )

            # 与撮合/结算共用同一把执行锁：避免「读 Redis → 台账重建 → 回写」
            # 期间穿插的成交被 autofix 整包覆盖（丢更新）。拿不到锁本周期跳过。
            async with manager.locked_execution(user_id, tenant_id):
                live_raw = await asyncio.to_thread(redis.client.get, key)
                live = _json.loads(live_raw) if live_raw else {}
                if not isinstance(live, dict):
                    live = {}
                rebuilt = await manager._rebuild_from_ledger(
                    user_id, tenant_id, market
                )
                if not rebuilt:
                    continue
                diffs: list[dict[str, Any]] = []
                cash_diff = float(live.get("cash") or 0) - float(
                    rebuilt.get("cash") or 0
                )
                if abs(cash_diff) > _DIFF_TOL:
                    diffs.append(
                        {
                            "field": "cash",
                            "symbol": "",
                            "redis_value": float(live.get("cash") or 0),
                            "pg_value": float(rebuilt.get("cash") or 0),
                            "diff": cash_diff,
                        }
                    )
                # 负债（融券户）：PG 有列、可直接比对。short_proceeds/market_value
                # 属 Redis 现值口径（PG 不可考 / 按收盘重估），不参与比对。
                liabilities_diff = float(live.get("liabilities") or 0) - float(
                    rebuilt.get("liabilities") or 0
                )
                if abs(liabilities_diff) > _DIFF_TOL:
                    diffs.append(
                        {
                            "field": "liabilities",
                            "symbol": "",
                            "redis_value": float(live.get("liabilities") or 0),
                            "pg_value": float(rebuilt.get("liabilities") or 0),
                            "diff": liabilities_diff,
                        }
                    )
                live_pos = _positions_by_symbol(live.get("positions"))
                pg_pos = _positions_by_symbol(rebuilt.get("positions"))
                for pos_key in sorted(set(live_pos) | set(pg_pos)):
                    d = live_pos.get(pos_key, 0.0) - pg_pos.get(pos_key, 0.0)
                    if abs(d) > _DIFF_TOL:
                        diffs.append(
                            {
                                "field": "volume",
                                "symbol": _report_symbol(*pos_key),
                                "redis_value": live_pos.get(pos_key, 0.0),
                                "pg_value": pg_pos.get(pos_key, 0.0),
                                "diff": d,
                            }
                        )
                live_available = _positions_by_symbol(
                    live.get("positions"), "available_volume"
                )
                pg_available = _positions_by_symbol(
                    rebuilt.get("positions"), "available_volume"
                )
                for pos_key in sorted(set(live_available) | set(pg_available)):
                    d = live_available.get(pos_key, 0.0) - pg_available.get(
                        pos_key, 0.0
                    )
                    if abs(d) > _DIFF_TOL:
                        diffs.append(
                            {
                                "field": "available_volume",
                                "symbol": _report_symbol(*pos_key),
                                "redis_value": live_available.get(pos_key, 0.0),
                                "pg_value": pg_available.get(pos_key, 0.0),
                                "diff": d,
                            }
                        )
                if not diffs:
                    continue
                stats["diff_fields"] += len(diffs)
                fixed = False
                if autofix:
                    try:
                        from backend.shared.trade_account_cache import (
                            write_json_cache,
                        )

                        # 保留 PG 不可考的 Redis 独有字段（short_proceeds /
                        # warning_level / market / t1_settlement_date）。
                        merged = SimulationProjectionService.merge_preserved(
                            live, rebuilt
                        )
                        await asyncio.to_thread(write_json_cache, redis, key, merged)
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

    # 台账自身一致性审计（每小时一次）：PG 台账是对账的"真理"，但它自己也可能
    # 漂移（漏记流水/重复扣款），Redis↔台账比对无法发现。只告警+留痕，不自动改。
    global _last_ledger_audit
    now_mono = time.monotonic()
    if now_mono - _last_ledger_audit >= _LEDGER_AUDIT_INTERVAL_SEC:
        _last_ledger_audit = now_mono
        try:
            stats["ledger_drift"] = await _audit_ledger_cash()
        except Exception as exc:
            logger.warning("ledger cash audit failed: %s", exc)

    log = logger.info if stats["diff_fields"] else logger.debug
    log(
        "simulation reconcile done: checked=%d diff_fields=%d autofixed=%d",
        stats["checked"],
        stats["diff_fields"],
        stats["autofixed"],
    )
    return stats


async def _audit_ledger_cash() -> int:
    """审计 PG 台账自身一致性，返回漂移账户数。

    判据：``simulation_accounts.cash`` 应等于
    ``initial_equity + Σ simulation_cash_ledger.amount``（成交/公司行为流水）。
    漂移只告警并写 simulation_reconcile_reports(field='ledger_cash')，不自动改数
    （需人工核对流水）。
    """
    from sqlalchemy import text as _text

    from backend.shared.database_manager_v2 import get_session as _get_session

    drifted = 0
    async with _get_session() as session:
        rows = (
            await session.execute(
                _text(
                    "SELECT a.account_id, a.tenant_id, a.user_id, "
                    "a.initial_equity, a.cash, "
                    "COALESCE(SUM(l.amount), 0) AS ledger_sum "
                    "FROM simulation_accounts a "
                    "LEFT JOIN simulation_cash_ledger l "
                    "  ON l.account_id = a.account_id "
                    "WHERE a.initial_equity > 0 "
                    "GROUP BY a.account_id, a.tenant_id, a.user_id, "
                    "a.initial_equity, a.cash"
                )
            )
        ).fetchall()
        for row in rows:
            m = row._mapping
            implied = float(m["initial_equity"] or 0.0) + float(
                m["ledger_sum"] or 0.0
            )
            cash = float(m["cash"] or 0.0)
            drift = cash - implied
            if abs(drift) <= _DIFF_TOL:
                continue
            drifted += 1
            logger.warning(
                "ledger cash drift account=%s cash=%.2f implied=%.2f drift=%.2f",
                m["account_id"],
                cash,
                implied,
                drift,
            )
            await session.execute(
                _text(
                    "INSERT INTO simulation_reconcile_reports "
                    "(tenant_id, user_id, market, field, symbol, "
                    "redis_value, pg_value, diff, autofixed) "
                    "VALUES (:tid, :uid, 'CN', 'ledger_cash', '', "
                    ":rv, :pv, :df, FALSE)"
                ),
                {
                    "tid": m["tenant_id"],
                    "uid": str(m["user_id"]),
                    "rv": implied,
                    "pv": cash,
                    "df": drift,
                },
            )
        if drifted:
            await session.commit()
    return drifted


def autofix_enabled() -> bool:
    # 默认 true：权益结算 worker 每 30s 以 PG 台账为准确权 Redis（持久化对账）。
    return os.getenv("SIM_RECONCILE_AUTOFIX", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


async def run_simulation_reconcile_worker() -> None:
    """每天 06:20 跑一次对账（EOD 06:05 之后，日线就绪窗口）。"""
    last_run_date = ""
    while True:
        try:
            now = datetime.now(_SH_TZ)
            today = now.strftime("%Y%m%d")
            if (
                today != last_run_date
                and (now.hour, now.minute) >= (6, 20)
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
