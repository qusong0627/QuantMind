"""SIM ↔ 真实镜像 双轨对账（每日收盘后）

背景：模拟盘成交是一次性全额记账（virtual_fill），而镜像真单可能被闸门跳过、
部分成交或不成交，两条账本天然会分叉。没有对账时用户只看到「模拟已成交」，
真单实际没打出去也无人知晓。

本任务每日收盘后（默认 15:10）对比：
  1) 模拟台账成交（``sim_trades``）按 (symbol, side) 聚合；
  2) 真实镜像单（``orders.client_order_id LIKE 'mir-%'``）按 (symbol, side) 聚合；
  3) 当日镜像跳过记录（Redis ``mirror:skipped:{YYYYMMDD}``，见 real_mirror_service）。

差异分两类：
  * ``shortfall``：模拟成交 > 真单成交（镜像被跳过/未成交/部分成交）；
  * ``excess``：真单成交 > 模拟成交（疑似重复下单，严重，需人工核实）。

带跳过原因的 shortfall 视为「已解释」；无原因或 excess 一律告警通知（每日一次）。

报表写入 Redis ``mirror:reconcile:{YYYYMMDD}``（JSON，TTL 30 天）。

环境变量：
  MIRROR_RECONCILE_ENABLED              默认 "1"
  MIRROR_RECONCILE_TIME                 默认 "15:10"（上海时区）
  MIRROR_RECONCILE_CHECK_INTERVAL_SEC   默认 60
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, select

from backend.services.live_trading.services.trading_session import TZ

logger = logging.getLogger(__name__)

_REPORT_KEY = "mirror:reconcile:{date}"
_DONE_KEY = "mirror:reconcile:done:{date}"
_REPORT_TTL_SECONDS = 30 * 24 * 3600

# 北京时间与 UTC 的固定偏移（A 股无夏令时）
_CST_OFFSET = timedelta(hours=8)


def _env_bool(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, "")).strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _config() -> dict:
    return {
        "enabled": _env_bool("MIRROR_RECONCILE_ENABLED", True),
        "time": str(os.getenv("MIRROR_RECONCILE_TIME", "15:10")),
        "interval": max(10, int(os.getenv("MIRROR_RECONCILE_CHECK_INTERVAL_SEC", "60"))),
    }


def _redis_client(redis) -> object | None:
    return getattr(redis, "client", None) if redis is not None else None


def parse_reconcile_time(raw: str) -> tuple[int, int]:
    """解析 ``HH:MM``，非法值回落 15:10。"""
    try:
        hour, minute = str(raw).strip().split(":", 1)
        h, m = int(hour), int(minute)
        if 0 <= h < 24 and 0 <= m < 60:
            return h, m
    except (TypeError, ValueError):
        pass
    return 15, 10


def cst_day_window(date_str: str) -> tuple[datetime, datetime]:
    """某日（YYYYMMDD）的北京时间 [起, 止) 窗口（aware）。"""
    day = datetime.strptime(date_str, "%Y%m%d").replace(tzinfo=TZ)
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def build_reconciliation_report(
    *,
    date_str: str,
    sim_rows: list[tuple[str, str, float]],
    real_rows: list[tuple[str, str, float]],
    skips: dict[str, int] | None = None,
) -> dict:
    """纯函数：聚合双轨数据并标注差异。

    ``sim_rows`` / ``real_rows``：``(symbol, side, quantity)`` 列表（side 为 BUY/SELL 大写）。
    ``skips``：``{"symbol:reason": count}``（real_mirror_service.load_skips 口径）。
    """
    skips = skips or {}
    skips_by_symbol: dict[str, dict[str, int]] = {}
    for field, count in skips.items():
        symbol, _, reason = str(field).partition(":")
        if not symbol or not reason:
            continue
        skips_by_symbol.setdefault(symbol.upper(), {})[reason] = int(count)

    def _aggregate(rows: list[tuple[str, str, float]]) -> dict[tuple[str, str], dict]:
        agg: dict[tuple[str, str], dict] = {}
        for symbol, side, quantity in rows:
            key = (str(symbol or "").upper(), str(side or "").upper())
            item = agg.setdefault(key, {"quantity": 0.0, "count": 0})
            item["quantity"] += float(quantity or 0)
            item["count"] += 1
        return agg

    sim_agg = _aggregate(sim_rows)
    real_agg = _aggregate(real_rows)

    diffs: list[dict] = []
    for key in sorted(set(sim_agg) | set(real_agg)):
        symbol, side = key
        sim = sim_agg.get(key, {"quantity": 0.0, "count": 0})
        real = real_agg.get(key, {"quantity": 0.0, "count": 0})
        delta = round(real["quantity"] - sim["quantity"], 4)
        if abs(delta) < 1e-6 and real["count"] == sim["count"]:
            continue
        symbol_skips = skips_by_symbol.get(symbol, {})
        kind = "excess" if delta > 0 else "shortfall"
        diffs.append(
            {
                "symbol": symbol,
                "side": side,
                "sim_quantity": sim["quantity"],
                "sim_count": sim["count"],
                "real_quantity": real["quantity"],
                "real_count": real["count"],
                "delta": delta,
                "kind": kind,
                # shortfall 且有跳过记录 → 已解释（原因随附）；excess 永远 unexplained
                "explained": kind == "shortfall" and bool(symbol_skips),
                "skip_reasons": symbol_skips,
            }
        )

    unexplained = [d for d in diffs if not d["explained"]]
    return {
        "date": date_str,
        "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "sim_symbols": len(sim_agg),
        "real_symbols": len(real_agg),
        "skip_events": int(sum(skips.values())),
        "diffs": diffs,
        "unexplained": unexplained,
        "ok": not unexplained,
    }


async def collect_sim_rows(date_str: str) -> list[tuple[str, str, float]]:
    """当日模拟成交（sim_trades）。

    ``executed_at`` 存的是 naive UTC（dispatcher 用 ``datetime.utcnow()``），
    故窗口按 UTC 口径平移，避免跨日错位。
    """
    from backend.services.simulation.models.trade import SimTrade

    start, end = cst_day_window(date_str)
    utc_start = (start - _CST_OFFSET).replace(tzinfo=None)
    utc_end = (end - _CST_OFFSET).replace(tzinfo=None)

    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=True) as db:
        rows = (
            await db.execute(
                select(SimTrade.symbol, SimTrade.side, SimTrade.quantity).where(
                    and_(SimTrade.executed_at >= utc_start, SimTrade.executed_at < utc_end)
                )
            )
        ).all()
    return [
        (str(symbol), str(getattr(side, "value", side) or "").upper(), float(qty or 0))
        for symbol, side, qty in rows
    ]


async def collect_real_rows(date_str: str) -> list[tuple[str, str, float]]:
    """当日真实镜像单成交（orders，``mir-`` 前缀）。

    ``created_at`` 为 naive 北京时间（容器 TZ=Asia/Shanghai），直接用北京窗口。
    """
    from backend.services.trade_shared.models.order import Order, TradingMode

    start, end = cst_day_window(date_str)
    naive_start = start.replace(tzinfo=None)
    naive_end = end.replace(tzinfo=None)

    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=True) as db:
        rows = (
            await db.execute(
                select(Order.symbol, Order.side, Order.filled_quantity).where(
                    and_(
                        Order.trading_mode == TradingMode.REAL,
                        Order.client_order_id.like("mir-%"),
                        Order.created_at >= naive_start,
                        Order.created_at < naive_end,
                    )
                )
            )
        ).all()
    return [
        (str(symbol), str(getattr(side, "value", side) or "").upper(), float(qty or 0))
        for symbol, side, qty in rows
    ]


def _save_report(redis, report: dict) -> None:
    client = _redis_client(redis)
    if client is None:
        return
    try:
        client.set(
            _REPORT_KEY.format(date=report.get("date") or ""),
            json.dumps(report, ensure_ascii=False),
            ex=_REPORT_TTL_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Reconcile] 报表写入失败: %s", exc)


def load_report(redis, date_str: str) -> dict | None:
    """读取某日对账报表（页面/CLI 用）。"""
    client = _redis_client(redis)
    if client is None:
        return None
    try:
        raw = client.get(_REPORT_KEY.format(date=date_str))
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        return json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    except (TypeError, ValueError):
        return None


async def run_dual_book_reconciliation(redis, date_str: str | None = None) -> dict:
    """执行一次对账（可手动触发），返回报表并落 Redis。"""
    from backend.services.live_trading.services.real_mirror_service import (
        load_config,
        load_skips,
        mirror_enabled,
    )

    date_str = date_str or datetime.now(TZ).strftime("%Y%m%d")

    sim_rows: list[tuple[str, str, float]] = []
    real_rows: list[tuple[str, str, float]] = []
    skips: dict[str, int] = {}
    errors: list[str] = []

    try:
        sim_rows = await collect_sim_rows(date_str)
    except Exception as exc:  # noqa: BLE001
        logger.error("[Reconcile] 读取模拟成交失败: %s", exc, exc_info=True)
        errors.append(f"sim_query_failed: {exc}")
    try:
        real_rows = await collect_real_rows(date_str)
    except Exception as exc:  # noqa: BLE001
        logger.error("[Reconcile] 读取真单成交失败: %s", exc, exc_info=True)
        errors.append(f"real_query_failed: {exc}")
    try:
        skips = load_skips(redis, date_str)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Reconcile] 读取跳过记录失败: %s", exc)

    report = build_reconciliation_report(
        date_str=date_str, sim_rows=sim_rows, real_rows=real_rows, skips=skips
    )
    if errors:
        report["errors"] = errors
        report["ok"] = False

    # 是否产生了「应有真单」的预期：镜像开启 或 当日确有空单（说明当天确实在镜像）
    try:
        cfg = load_config(redis)
        expected = bool(mirror_enabled(redis, cfg)) or bool(real_rows)
    except Exception:  # noqa: BLE001
        expected = bool(real_rows)
    report["mirror_expected"] = expected

    _save_report(redis, report)
    logger.info(
        "[Reconcile] %s 对账完成：sim=%d 标的 real=%d 标的 差异=%d（未解释=%d）跳过=%d",
        date_str,
        report["sim_symbols"],
        report["real_symbols"],
        len(report["diffs"]),
        len(report["unexplained"]),
        report["skip_events"],
    )

    if expected and (report["unexplained"] or errors):
        await _notify_report(redis, report)
    return report


async def _notify_report(redis, report: dict) -> None:
    """差异通知（每日一次，Redis NX 去重）。"""
    client = _redis_client(redis)
    date_str = str(report.get("date") or "")
    if client is not None:
        try:
            if not client.set(
                f"mirror:reconcile:notified:{date_str}", "1", nx=True, ex=_REPORT_TTL_SECONDS
            ):
                return
        except Exception:  # noqa: BLE001
            pass

    unexplained = report.get("unexplained") or []
    lines = []
    for item in unexplained[:5]:
        lines.append(
            f"{item['symbol']} {item['side']} 模拟 {item['sim_quantity']:.0f} 股 / "
            f"真单 {item['real_quantity']:.0f} 股（{'多出' if item['delta'] > 0 else '缺口'} "
            f"{abs(item['delta']):.0f}）"
        )
    if len(unexplained) > 5:
        lines.append(f"……另有 {len(unexplained) - 5} 项")
    content = f"{date_str} 模拟盘与真单成交不一致：\n" + "\n".join(lines)
    if report.get("errors"):
        content += f"\n对账任务异常：{'; '.join(report['errors'])}"
    try:
        from backend.shared.notification_publisher import publish_notification_async

        await publish_notification_async(
            user_id="1",
            tenant_id="default",
            title="双轨对账存在差异",
            content=content,
            type="trading",
            level="warning",
            action_url="/trading",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Reconcile] 差异通知失败: %s", exc)


async def run_dual_book_reconciliation_task() -> None:
    """常驻循环：每交易日到点后跑一次，Redis 标记当日已执行。"""
    cfg = _config()
    if not cfg["enabled"]:
        logger.info("[Reconcile] 双轨对账任务关闭（MIRROR_RECONCILE_ENABLED=0）")
        return

    from backend.services.trade_shared.deps import get_redis

    target_h, target_m = parse_reconcile_time(cfg["time"])
    logger.info("[Reconcile] 双轨对账任务启动：每日 %02d:%02d 执行", target_h, target_m)
    last_error = ""
    while True:
        try:
            now = datetime.now(TZ)
            date_str = now.strftime("%Y%m%d")
            client = _redis_client(get_redis())
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
                redis = get_redis()
                await run_dual_book_reconciliation(redis, date_str)
                if client is not None:
                    try:
                        client.set(_DONE_KEY.format(date=date_str), "1", ex=3 * 24 * 3600)
                    except Exception:  # noqa: BLE001
                        pass
            last_error = ""
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            if message != last_error:
                logger.error("[Reconcile] 任务异常: %s", exc, exc_info=True)
                last_error = message
        await asyncio.sleep(cfg["interval"])
