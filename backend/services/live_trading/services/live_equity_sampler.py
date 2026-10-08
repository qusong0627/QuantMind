"""实盘净值分钟采样器（实况页净值图数据源的写入方）。

2026-10-08 从旧栈 ``quant-Trader/scripts/live_hourly_analysis.py --record-only``
移植：``live_equity.jsonl``（实况页净值图/模型卡数据源）的写入方随旧栈 09-29
停机，本仓只剩读取方（``live_family.load_equity``）→ 图线冻结在 09-29 10:39。
本模块把每分钟采样搬进 trade 服务常驻任务：

- 时段 9:25-11:30 / 13:00-15:10（北京，交易日）：午休/盘外桥价冻结，采样只
  会写出平线噪音，不入曲线；
- 总账户行（``agent=null``）：桥实时总资产（asset/cash）；桥断 → PG 最新
  tdx_bridge 快照降级估算（``estimated=true``，收盘口径）；资产 ≤0 一律跳过
  （宁缺毋滥——桥返资产 ≤0 是断线语义，曾有 -17% 假资产事故）；
- 每 agent 分账行：虚拟现金（live_ledger.json）+ 名下持仓 × 桥实时价；持仓
  全部取不到实时价 → 跳过该 agent（成本价假净值会造平线段）；
- 同一分钟 key + agent 去重（fcntl 锁内重扫 existing，防手动重跑/并发双写）；
- 落点 = ``arena_config.logs_dir()/live_equity.jsonl``（与读取方同一解析：
  容器内 /data/logs、宿主 data/logs 是同一份）。
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any
from collections.abc import Awaitable, Callable
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_CN_TZ = ZoneInfo("Asia/Shanghai")

#: 采样时段（北京分钟数）：9:25-11:30 / 13:00-15:10。
_SAMPLE_WINDOWS = ((9 * 60 + 25, 11 * 60 + 30), (13 * 60, 15 * 60 + 10))

#: 分账初始额度（与 scripts/live_ledger.AGENT_QUOTA 同值；键缺失时的回退）。
_DEFAULT_AGENT_QUOTA = 100_000.0

#: 每分钟采样一轮；时段外空转（检查是纯本地的，代价可忽略）。
_DEFAULT_INTERVAL_S = 60.0


def _f(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _now_cn() -> datetime:
    return datetime.now(_CN_TZ)


def in_sample_window(now: datetime) -> bool:
    """是否在采样时段（北京墙钟）。"""
    hm = now.hour * 60 + now.minute
    return any(lo <= hm <= hi for lo, hi in _SAMPLE_WINDOWS)


def is_trading_day(day: date) -> bool:
    """交易日判据（XSHG 日历）。日历答不了（越界/库缺失）→ 按交易日放行 + 告警。

    与市场取数闸同向：未知只许放行（fail-open），宁可多采一行也不把曲线掐断；
    真非交易日由日历给出 False（节假日不采样，桥价冻结只会产生平线噪音）。
    """
    try:
        from backend.shared.trading_calendar import is_trading_day_xcal

        verdict = is_trading_day_xcal("CN", day)
    except Exception as exc:  # noqa: BLE001 - 判据异常不阻断采样
        logger.warning("净值采样交易日判据异常，按交易日放行: %s", exc)
        return True
    if verdict is None:
        logger.warning("净值采样交易日判据不可用（日历越界？），按交易日放行: %s", day)
        return True
    return bool(verdict)


def _load_agents(ledger_path: Path) -> dict[str, Any]:
    """读分账账本（live_ledger.json）的 agents 段；缺失/损坏按空账本（不抛）。"""
    try:
        doc = json.loads(ledger_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    agents = doc.get("agents") if isinstance(doc, dict) else None
    return agents if isinstance(agents, dict) else {}


def _agent_virtual_cash(rec: dict[str, Any]) -> float:
    """该 agent 虚拟现金：键缺失回退额度，0.0 原样（与 account_protocol._cash 同口径）。"""
    if "virtual_cash" not in rec:
        return _DEFAULT_AGENT_QUOTA
    value = rec.get("virtual_cash")
    if value is None:
        return _DEFAULT_AGENT_QUOTA
    return _f(value)


async def _bridge_asset() -> tuple[float, float, bool]:
    """桥实时总资产 (总资产, 现金, 是否估算)。桥不可达抛异常，由调用方降级。"""
    from backend.services.agent_arena.live_family import _bridge_account_query

    payload = await _bridge_account_query()
    asset = payload.get("asset") if isinstance(payload.get("asset"), dict) else {}
    total = _f(asset.get("asset"))
    if total <= 0:
        total = _f(asset.get("cash")) + _f(asset.get("market_value"))
    return total, _f(asset.get("cash")), False


async def _degraded_asset_from_snapshot() -> tuple[float, float, bool]:
    """桥断降级：PG 最新 tdx_bridge 快照的（总资产, 现金），标注估算。"""
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session
    from backend.shared.simulation_account_keys import resolve_db_account_user

    user = resolve_db_account_user("QM_LIVE_EQUITY_ACCOUNT_USER")
    tenant = (
        os.getenv("QM_LIVE_EQUITY_ACCOUNT_TENANT", "default") or "default"
    ).strip()
    async with get_session(read_only=True) as session:
        row = (
            await session.execute(
                text(
                    "SELECT total_asset, cash FROM real_account_snapshots "
                    "WHERE tenant_id = :tid AND user_id = :uid AND source = 'tdx_bridge' "
                    "ORDER BY snapshot_at DESC LIMIT 1"
                ),
                {"tid": tenant, "uid": user},
            )
        ).fetchone()
    if row is None:
        return 0.0, 0.0, False
    return _f(row[0]), _f(row[1]), True


async def _default_quote_fetcher(codes: list[str]) -> dict[str, float]:
    """桥实时价（与持仓 tab/概览覆盖同一条链：live_family._bridge_quick_prices）。"""
    from backend.services.agent_arena.live_family import _bridge_quick_prices

    return await _bridge_quick_prices(codes)


def _append_entries(path: Path, entries: list[dict[str, Any]]) -> int:
    """锁内按 (key, agent) 去重后追加；返回实际写入行数。"""
    if not entries:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.parent / ".live_equity.lock"
    written = 0
    with lock_path.open("a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            existing: set[tuple[Any, Any]] = set()
            if path.is_file():
                for line in path.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines():
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    existing.add((record.get("key"), record.get("agent")))
            with path.open("a", encoding="utf-8") as out:
                for entry in entries:
                    dedupe_key = (entry["key"], entry["agent"])
                    if dedupe_key in existing:
                        continue
                    out.write(json.dumps(entry, ensure_ascii=False) + "\n")
                    existing.add(dedupe_key)
                    written += 1
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
    return written


async def sample_once(
    now: datetime | None = None,
    *,
    account_fetcher: Callable[[], Awaitable[tuple[float, float, bool]]] | None = None,
    degraded_fetcher: Callable[[], Awaitable[tuple[float, float, bool]]] | None = None,
    quote_fetcher: Callable[[list[str]], Awaitable[dict[str, float]]] | None = None,
    ledger_path: Path | None = None,
    equity_path: Path | None = None,
) -> dict[str, Any]:
    """采样一轮（幂等：同分钟已写过的行不重复写）。返回报告 dict。"""
    now = now or _now_cn()
    if not in_sample_window(now):
        return {"written": 0, "skipped": ["outside_window"]}
    if not is_trading_day(now.date()):
        return {"written": 0, "skipped": ["non_trading_day"]}

    from backend.services.agent_arena import arena_config

    ledger_file = ledger_path or (arena_config.logs_dir() / "live_ledger.json")
    out_file = equity_path or (arena_config.logs_dir() / "live_equity.jsonl")

    key = now.strftime("%Y-%m-%d %H:%M")
    date_str = now.strftime("%Y-%m-%d")
    ts = now.isoformat()
    entries: list[dict[str, Any]] = []
    skipped: list[str] = []

    # ① 总账户行：桥实时 → 快照降级（estimated）→ 跳过（宁缺毋滥）
    fetch_account = account_fetcher or _bridge_asset
    fetch_degraded = degraded_fetcher or _degraded_asset_from_snapshot
    asset = cash = 0.0
    estimated = False
    account_err: BaseException | None = None
    try:
        asset, cash, estimated = await fetch_account()
    except Exception as exc:  # noqa: BLE001 - 桥不可达 → 降级估算（曲线不断线）
        account_err = exc
        try:
            asset, cash, estimated = await fetch_degraded()
        except Exception as exc2:  # noqa: BLE001 - 降级也失败 → 本次无总账户行
            account_err = exc2
            asset = 0.0
    if asset > 0:
        row: dict[str, Any] = {
            "key": key,
            "agent": None,
            "date": date_str,
            "ts": ts,
            "value": round(asset, 2),
            "asset": round(asset, 2),
            "cash": round(cash, 2),
        }
        if estimated:
            row["estimated"] = True
        entries.append(row)
    else:
        skipped.append(
            f"account:asset<=0:{type(account_err).__name__ if account_err else 'unknown'}"
        )

    # ② 每 agent 分账行：虚拟现金 + 名下持仓 × 桥实时价
    agents = _load_agents(ledger_file)
    if agents:
        codes: list[str] = []
        for rec in agents.values():
            positions = rec.get("positions") if isinstance(rec, dict) else None
            if isinstance(positions, dict):
                codes.extend(str(code) for code in positions)
        fetch_quotes = quote_fetcher or _default_quote_fetcher
        try:
            prices = await fetch_quotes(codes) or {}
        except Exception as exc:  # noqa: BLE001 - 取价失败 → 本分钟全部 agent 行跳过
            prices = {}
            skipped.append(f"quotes_failed:{type(exc).__name__}")
        for agent, rec in agents.items():
            rec = rec if isinstance(rec, dict) else {}
            positions = (
                rec.get("positions") if isinstance(rec.get("positions"), dict) else {}
            )
            mkt = 0.0
            n_pos = n_quoted = 0
            for code, pos in positions.items():
                pos = pos if isinstance(pos, dict) else {}
                n_pos += 1
                vol = _f(pos.get("volume"))
                price = _f(pos.get("cost_price"))
                px = _f(prices.get(str(code)))
                if px > 0:
                    price = px
                    n_quoted += 1
                mkt += price * vol
            if n_pos and not n_quoted:
                skipped.append(f"agent:{agent}:no_quotes")
                continue
            entries.append(
                {
                    "key": key,
                    "agent": agent,
                    "date": date_str,
                    "ts": ts,
                    "value": round(_agent_virtual_cash(rec) + mkt, 2),
                }
            )

    written = _append_entries(out_file, entries)
    if written:
        logger.info("净值采样 %s 写入 %d 行 → %s", key, written, out_file)
    return {"written": written, "entries": len(entries), "skipped": skipped, "key": key}


async def run_live_equity_sampler_worker() -> None:
    """常驻采样循环（trade 服务内注册；分钟节拍，时段外空转）。"""
    interval = _f(os.getenv("QM_LIVE_EQUITY_SAMPLER_S", "") or _DEFAULT_INTERVAL_S)
    interval = max(5.0, interval or _DEFAULT_INTERVAL_S)
    logger.info(
        "live_equity sampler started interval=%ss window=9:25-11:30/13:00-15:10 CN",
        interval,
    )
    while True:
        try:
            from backend.shared.scheduler_registry import heartbeat as _sched_heartbeat

            _sched_heartbeat("live_equity_sampler")
        except Exception:  # noqa: BLE001 - 心跳非关键路径
            pass
        try:
            await sample_once()
        except Exception as exc:  # noqa: BLE001 - 循环永不退出
            logger.error("live_equity sampler cycle failed: %s", exc, exc_info=True)
        # 对齐分钟节拍（+1s 余量），与旧 cron 每分钟触发同节奏
        await asyncio.sleep(
            max(5.0, interval - (_now_cn().second % int(interval)) + 1.0)
        )
