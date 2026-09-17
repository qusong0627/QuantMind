"""影子对照采集与日报（T-P2-06）—— 纯指标在 ``shared/shadow_compare.py``，本模块只做 IO。

职责：
- 采集当日「模拟成交 ↔ 镜像真单」配对（``collect_day_pairs``）；
- 采集两侧日度净值序列（``collect_equity_series``，**两侧 user_id 键形归一**：
  模拟侧归一整型（"1"）、实盘侧补零原始 sub（"00000001"）——以候选形式逐一探测）；
- 组装日报落 Redis ``mirror:shadow:{date}``（TTL 30 天，与双轨对账同族）；
- 常驻循环每日 15:15（双轨对账 15:10 之后）执行，心跳入调度注册表。

环境变量：
  MIRROR_SHADOW_ENABLED             默认 "1"
  MIRROR_SHADOW_TIME                默认 "15:15"（上海时区）
  MIRROR_SHADOW_CHECK_INTERVAL_SEC  默认 60
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import date, datetime, timedelta

from backend.shared.shadow_compare import build_shadow_report, compute_tracking_error
from backend.services.live_trading.services.trading_session import TZ

logger = logging.getLogger(__name__)

_REPORT_KEY = "mirror:shadow:{date}"
_DONE_KEY = "mirror:shadow:done:{date}"
_REPORT_TTL_SECONDS = 30 * 24 * 3600
_TRACKING_LOOKBACK_DAYS = 30
_TRACKING_MAX_USERS = 5

# 北京时间与 UTC 的固定偏移（A 股无夏令时）——与双轨对账同口径
_CST_OFFSET = timedelta(hours=8)


def _env_bool(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, "")).strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _config() -> dict:
    return {
        "enabled": _env_bool("MIRROR_SHADOW_ENABLED", True),
        "time": str(os.getenv("MIRROR_SHADOW_TIME", "15:15")),
        "interval": max(10, int(os.getenv("MIRROR_SHADOW_CHECK_INTERVAL_SEC", "60"))),
    }


def parse_shadow_time(raw: str) -> tuple[int, int]:
    """解析 ``HH:MM``，非法值回落 15:15。"""
    try:
        hour, minute = str(raw).strip().split(":", 1)
        h, m = int(hour), int(minute)
        if 0 <= h < 24 and 0 <= m < 60:
            return h, m
    except (TypeError, ValueError):
        pass
    return 15, 15


def cst_day_window(date_str: str) -> tuple[datetime, datetime]:
    """某日（YYYYMMDD）的北京时间 [起, 止) 窗口（aware）。"""
    day = datetime.strptime(date_str, "%Y%m%d").replace(tzinfo=TZ)
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def uid_forms(raw_user: str | None) -> list[str]:
    """user_id 候选键形（纯函数）：归一整型优先、补零形式兜底。

    历史教训（P0 诊断）：账户侧用归一整型（"1"），实盘/曲线侧曾用原始补零 sub
    （"00000001"），同一用户两套键形共存。查询必须按候选顺序探测，
    **取第一个有数据的形式**，避免半空表混并造成双计。
    """
    text = str(raw_user or "").strip()
    if not text:
        return []
    if not text.isdigit():
        return [text]
    forms: list[str] = []
    norm = str(int(text))
    for form in (norm, norm.zfill(8)):
        if form not in forms:
            forms.append(form)
    return forms


async def collect_day_pairs(date_str: str, *, tenant_id: str | None = None) -> dict:
    """采集某日「模拟成交 ↔ 镜像真单」并配对（返回 pair_orders 结构 + 用户信息）。

    ``tenant_id`` 缺省=None 表示不过滤（日报全局口径）；管理端点按租户过滤。
    """
    from sqlalchemy import text as sa_text

    from backend.shared.database_manager_v2 import get_session

    start, end = cst_day_window(date_str)
    # sim_trades.executed_at 为 **timestamptz**：直接以 aware 时刻窗口比较。
    # 历史坑（2026-09-16 实测修复）：旧实现把窗口平移成 naive-UTC 再比较，PG/asyncpg
    # 会按**会话时区（容器 Asia/Shanghai）**解释 naive 参数 → 实际窗口变为
    # [昨16:00, 今16:00) CST，16:00 之后当日成交全部查不到（时段性假阴性）。
    # 历史 naive-UTC 存量被 PG 解释为 CST，日历日归属不变——日窗查询下语义正确。
    # orders.created_at：naive 北京时间（容器 TZ=Asia/Shanghai）
    naive_start = start.replace(tzinfo=None)
    naive_end = end.replace(tzinfo=None)

    sim_sql = (
        "SELECT t.order_id::text AS order_id, t.symbol, t.side::text AS side, "
        "t.price AS fill_price, t.quantity AS filled_quantity, t.total_fee, "
        "t.user_id::text AS user_id, o.client_order_id, o.remarks, "
        "o.status::text AS status, "
        # F2（T-P6-19）：模拟侧取价来源/执行核（daily=synthetic_price / F2=snapshot_core）
        "o.execution_model, o.price AS order_price, t.price_source "
        "FROM sim_trades t LEFT JOIN sim_orders o ON o.order_id = t.order_id "
        "WHERE t.executed_at >= :s AND t.executed_at < :e"
    )
    real_sql = (
        "SELECT client_order_id, order_id::text AS order_id, symbol, "
        "exchange_order_id, side::text AS side, status::text AS status, "
        "average_price, price, filled_quantity, commission, price_source, remarks, "
        "user_id::text AS user_id "
        "FROM orders "
        "WHERE trading_mode = 'REAL' AND client_order_id LIKE 'mir-%' "
        "AND created_at >= :s AND created_at < :e"
    )
    sim_params: dict = {"s": start, "e": end}
    real_params: dict = {"s": naive_start, "e": naive_end}
    if tenant_id:
        sim_sql += " AND t.tenant_id = :tid"
        real_sql += " AND tenant_id = :tid"
        sim_params["tid"] = tenant_id
        real_params["tid"] = tenant_id

    async with get_session(read_only=True) as db:
        sim_rows = (
            (await db.execute(sa_text(sim_sql + " ORDER BY t.executed_at"), sim_params))
            .mappings()
            .all()
        )
        real_rows = (
            (await db.execute(sa_text(real_sql + " ORDER BY created_at"), real_params))
            .mappings()
            .all()
        )

    from backend.shared.shadow_compare import pair_orders

    pairing = pair_orders([dict(r) for r in sim_rows], [dict(r) for r in real_rows])
    pairing["date"] = date_str
    return pairing


async def _first_form_with_rows(
    *,
    table: str,
    tenant_id: str,
    forms: list[str],
    since: date,
) -> tuple[str, list[tuple[date, float]]]:
    """按候选键形逐一探测，取第一个有数据的形式返回 (form, [(date, equity)])。"""
    from sqlalchemy import text as sa_text

    from backend.shared.database_manager_v2 import get_session

    allowed_tables = {
        "simulation_fund_snapshots",
        "real_account_ledger_daily_snapshots",
    }
    if table not in allowed_tables:  # pragma: no cover - 防御（调用方为常量）
        raise ValueError(f"非法表名: {table}")

    # T-P1-07：模拟快照带市场维度后，影子对照取合并行（'ALL'）；
    # 实盘台账表无市场列，不受影响
    market_clause = ""
    if table == "simulation_fund_snapshots":
        from backend.shared.fund_snapshot_contract import (
            fund_snapshot_has_market_column_async,
        )

        if await fund_snapshot_has_market_column_async():
            market_clause = "AND market = 'ALL' "
    async with get_session(read_only=True) as db:
        for form in forms:
            rows = (
                await db.execute(
                    sa_text(
                        f"SELECT snapshot_date, total_asset FROM {table} "
                        "WHERE tenant_id = :t AND user_id = :u "
                        f"{market_clause}AND snapshot_date >= :since "
                        "ORDER BY snapshot_date"
                    ),
                    {"t": tenant_id, "u": form, "since": since},
                )
            ).all()
            if rows:
                return form, [
                    (r.snapshot_date, float(r.total_asset or 0.0)) for r in rows
                ]
    return (forms[0] if forms else ""), []


async def collect_equity_series(
    tenant_id: str,
    user_raw: str,
    *,
    days: int = _TRACKING_LOOKBACK_DAYS,
) -> dict:
    """两侧日度净值序列（模拟=fund_snapshots，实盘=ledger daily；键形各自探测）。"""
    forms = uid_forms(user_raw)
    since = datetime.now(TZ).date() - timedelta(days=max(1, int(days)))
    sim_form, sim_series = await _first_form_with_rows(
        table="simulation_fund_snapshots", tenant_id=tenant_id, forms=forms, since=since
    )
    real_form, real_series = await _first_form_with_rows(
        table="real_account_ledger_daily_snapshots",
        tenant_id=tenant_id,
        forms=forms,
        since=since,
    )
    return {
        "sim_series": sim_series,
        "real_series": real_series,
        "sim_user_form": sim_form,
        "real_user_form": real_form,
    }


async def run_shadow_compare(
    redis, date_str: str | None = None, *, tenant_id: str = "default"
) -> dict:
    """执行一次影子对照（可手动触发），返回日报并落 Redis。"""
    date_str = date_str or datetime.now(TZ).strftime("%Y%m%d")
    errors: list[str] = []
    pairing: dict = {
        "pairs": [],
        "sim_only": [],
        "real_only": [],
        "symbol_side_mismatch": 0,
    }
    try:
        pairing = await collect_day_pairs(date_str, tenant_id=tenant_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("[Shadow] 读取当日配对失败: %s", exc, exc_info=True)
        errors.append(f"pair_query_failed: {exc}")

    # 跟踪误差：对当日有模拟成交的用户逐户计算（按用户数上限截断）
    tracking: dict[str, dict] = {}
    sim_users: list[str] = []
    for row in pairing.get("pairs") or []:
        uid = str(row.get("sim_user_id") or "").strip()
        if uid and uid not in sim_users:
            sim_users.append(uid)
    for uid in sim_users[:_TRACKING_MAX_USERS]:
        try:
            series = await collect_equity_series(tenant_id, uid)
            tracking[uid] = compute_tracking_error(
                series["sim_series"], series["real_series"]
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Shadow] 用户 %s 跟踪误差计算失败: %s", uid, exc)
            tracking[uid] = {"sufficient": False, "reason": f"数据读取失败: {exc}"}

    try:
        from backend.services.trade_shared.trade_config import settings

        configured_bps = float(settings.SIMULATION_SLIPPAGE_BPS)
    except Exception:  # noqa: BLE001
        configured_bps = 5.0

    report = build_shadow_report(
        date_str=date_str,
        pairing=pairing,
        configured_bps=configured_bps,
        tracking=tracking
        if tracking
        else {"sufficient": False, "reason": "当日无模拟成交（无用户可算跟踪误差）"},
    )
    if errors:
        report["errors"] = errors
        report["ok"] = False
    report["mirror_source"] = "sim_trades ⋈ orders(mir-*)"

    _save_report(redis, report)
    logger.info(
        "[Shadow] %s 影子对照完成：配对=%d 仅模拟=%d 仅真单=%d 价格偏差n=%s 成交率=%s ok=%s",
        date_str,
        report["coverage"]["matched"],
        report["coverage"]["sim_only"],
        report["coverage"]["real_only"],
        report["price_deviation"]["n"],
        report["fill"]["fill_rate"],
        report["ok"],
    )
    return report


def _raw_client(redis) -> object | None:
    """兼容两种形态：RedisClient 包装器（``.client``）或原生 client（有 set/get）。

    历史陷阱（T-P2-06）：CLI/脚本手动触发传入原生客户端时不带 ``.client``，
    若只认包装器会静默"跑成功但没落盘"。
    """
    if redis is None:
        return None
    client = getattr(redis, "client", None)
    if client is not None:
        return client
    return redis if hasattr(redis, "set") else None


def _save_report(redis, report: dict) -> None:
    client = _raw_client(redis)
    if client is None:
        logger.warning("[Shadow] redis 客户端不可用，日报未落盘（date=%s）", report.get("date"))
        return
    try:
        client.set(
            _REPORT_KEY.format(date=report.get("date") or ""),
            json.dumps(report, ensure_ascii=False),
            ex=_REPORT_TTL_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Shadow] 日报写入失败: %s", exc)


def load_latest_report(redis, *, max_back_days: int = 7) -> dict | None:
    """读取最近一份日报（今日→回溯 max_back_days，交易台展示用）。"""
    client = _raw_client(redis)
    if client is None:
        return None
    today = datetime.now(TZ).date()
    for offset in range(max(1, int(max_back_days))):
        day = (today - timedelta(days=offset)).strftime("%Y%m%d")
        try:
            raw = client.get(_REPORT_KEY.format(date=day))
        except Exception:  # noqa: BLE001
            return None
        if not raw:
            continue
        try:
            report = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        except (TypeError, ValueError):
            continue
        report["stale"] = offset > 0
        return report
    return None


async def run_shadow_compare_worker() -> None:
    """常驻循环：每交易日到点后跑一次，Redis 标记当日已执行；心跳入调度注册表。"""
    cfg = _config()
    if not cfg["enabled"]:
        logger.info("[Shadow] 影子对照任务关闭（MIRROR_SHADOW_ENABLED=0）")
        return

    from backend.services.trade_shared.deps import get_redis
    from backend.shared.scheduler_registry import heartbeat as _sched_heartbeat

    target_h, target_m = parse_shadow_time(cfg["time"])
    logger.info("[Shadow] 影子对照任务启动：每日 %02d:%02d 执行", target_h, target_m)
    while True:
        try:
            _sched_heartbeat("mirror_shadow")
        except Exception:  # noqa: BLE001
            pass
        try:
            now = datetime.now(TZ)
            date_str = now.strftime("%Y%m%d")
            redis = get_redis()
            client = _raw_client(redis)
            already = False
            if client is not None:
                try:
                    already = bool(client.exists(_DONE_KEY.format(date=date_str)))
                except Exception:  # noqa: BLE001
                    already = False
            if (
                now.weekday() < 5
                and (now.hour, now.minute) >= (target_h, target_m)
                and not already
            ):
                await run_shadow_compare(redis, date_str)
                if client is not None:
                    try:
                        client.set(
                            _DONE_KEY.format(date=date_str), "1", ex=3 * 24 * 3600
                        )
                    except Exception:  # noqa: BLE001
                        pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("[Shadow] 影子对照任务异常: %s", exc, exc_info=True)
        await asyncio.sleep(cfg["interval"])
