"""收盘清理核对（每日日终）

与 30 分钟超时扫描器互补：扫描器管「本地判死」（本地单超时标 EXPIRED），
本任务管「柜台到底还有没有单」——A 股委托当日有效，收盘后柜台不应再有非终态委托。

每日（默认 15:05）核对：
  1) 柜台委托全部终结：``query_orders()`` 仍有 PENDING/SUBMITTED/PARTIALLY_FILLED
     → 告警（可能占资金/持仓到次日，需人工确认）；
  2) 本地无 submitted 残留：本地 REAL 非终态单 vs 柜台状态对照，
     柜台已终结而本地仍非终态 → 告警（轮询器漏回收，委托列表与柜台错位）；
  3) 核对结果写入 Redis ``trade:close-audit:{YYYYMMDD}``（JSON，TTL 30 天）。

环境变量：
  CLOSE_AUDIT_ENABLED           默认 "1"
  CLOSE_AUDIT_TIME              默认 "15:05"（上海时区）
  CLOSE_AUDIT_CHECK_INTERVAL_SEC 默认 60
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta

from sqlalchemy import and_, select

from backend.services.live_trading.services.trading_session import TZ

logger = logging.getLogger(__name__)

_REPORT_KEY = "trade:close-audit:{date}"
_DONE_KEY = "trade:close-audit:done:{date}"
_REPORT_TTL_SECONDS = 30 * 24 * 3600

# 柜台终态（qmt_exec_client.QMT_STATUS_MAP 口径）
_COUNTER_TERMINAL = {"FILLED", "CANCELLED", "REJECTED"}
# 本地非终态
_LOCAL_OPEN = {"pending", "submitted", "partially_filled"}


def _env_bool(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, "")).strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _config() -> dict:
    return {
        "enabled": _env_bool("CLOSE_AUDIT_ENABLED", True),
        "time": str(os.getenv("CLOSE_AUDIT_TIME", "15:05")),
        "interval": max(10, int(os.getenv("CLOSE_AUDIT_CHECK_INTERVAL_SEC", "60"))),
    }


def _redis_client(redis) -> object | None:
    return getattr(redis, "client", None) if redis is not None else None


def parse_audit_time(raw: str) -> tuple[int, int]:
    """解析 ``HH:MM``，非法值回落 15:05。"""
    try:
        hour, minute = str(raw).strip().split(":", 1)
        h, m = int(hour), int(minute)
        if 0 <= h < 24 and 0 <= m < 60:
            return h, m
    except (TypeError, ValueError):
        pass
    return 15, 5


def classify_close_state(
    *,
    counter_orders: list[dict] | None,
    local_orders: list[dict] | None,
) -> dict:
    """纯函数：收盘核对分类。

    * ``counter_orders``：``client.query_orders()`` 原始项（``status`` 为 QMT_STATUS_MAP 值）。
    * ``local_orders``：本地当日 REAL 订单（``status``、``exchange_order_id``、``symbol``）。

    返回 ``{"counter_open": [...], "local_stale": [...], "ok": bool}``：
    * ``counter_open``：柜台仍非终态的委托；
    * ``local_stale``：本地非终态、但柜台无对应单或柜台已终结的委托（漏回收）。
    """
    counter_orders = counter_orders or []
    local_orders = local_orders or []

    counter_open = [
        item for item in counter_orders if str(item.get("status") or "").upper() not in _COUNTER_TERMINAL
    ]
    counter_by_id = {
        str(item.get("order_id") or ""): item
        for item in counter_orders
        if str(item.get("order_id") or "")
    }
    counter_ids = set(counter_by_id)

    local_stale: list[dict] = []
    for item in local_orders:
        if str(item.get("status") or "").lower() not in _LOCAL_OPEN:
            continue
        exchange_id = str(item.get("exchange_order_id") or "")
        if not exchange_id:
            # 本地非终态且无柜台编号：可能还没报出去（PENDING），收盘后属残留
            local_stale.append({**item, "reason": "no_exchange_order_id"})
            continue
        counter = counter_by_id.get(exchange_id)
        if counter is None:
            if exchange_id not in counter_ids:
                local_stale.append({**item, "reason": "counter_order_missing"})
            continue
        if str(counter.get("status") or "").upper() in _COUNTER_TERMINAL:
            local_stale.append(
                {**item, "reason": "counter_terminal", "counter_status": counter.get("status")}
            )

    return {
        "counter_open": counter_open,
        "local_stale": local_stale,
        "ok": not counter_open and not local_stale,
    }


async def run_close_audit(redis, date_str: str | None = None) -> dict:
    """执行一次收盘核对，返回报表并落 Redis。"""
    from backend.services.live_trading.services.qmt_exec_client import get_qmt_exec_client

    date_str = date_str or datetime.now(TZ).strftime("%Y%m%d")
    report: dict = {
        "date": date_str,
        "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "counter_open": [],
        "local_stale": [],
        "errors": [],
    }

    client = get_qmt_exec_client()
    counter_orders: list[dict] = []
    try:
        counter_orders = list(await client.query_orders() or [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("[CloseAudit] 查询柜台委托失败: %s", exc)
        report["errors"].append(f"query_orders_failed: {exc}")

    local_orders = await _collect_local_open_orders(date_str)

    classified = classify_close_state(counter_orders=counter_orders, local_orders=local_orders)
    report["counter_orders"] = len(counter_orders)
    report["local_open_orders"] = len(local_orders)
    report["counter_open"] = [
        {
            "order_id": item.get("order_id"),
            "symbol": item.get("symbol") or item.get("stock_code"),
            "side": item.get("side"),
            "status": item.get("status"),
            "order_volume": item.get("order_volume"),
            "traded_volume": item.get("traded_volume"),
            "order_remark": item.get("order_remark"),
        }
        for item in classified["counter_open"]
    ]
    report["local_stale"] = [
        {
            "order_id": str(item.get("order_id") or ""),
            "symbol": item.get("symbol"),
            "status": item.get("status"),
            "exchange_order_id": item.get("exchange_order_id"),
            "reason": item.get("reason"),
            "counter_status": item.get("counter_status"),
        }
        for item in classified["local_stale"]
    ]
    report["ok"] = classified["ok"] and not report["errors"]

    _save_report(redis, report)
    logger.info(
        "[CloseAudit] %s 收盘核对：柜台委托=%d 未终结=%d 本地残留=%d",
        date_str,
        report["counter_orders"],
        len(report["counter_open"]),
        len(report["local_stale"]),
    )
    if not report["ok"]:
        await _notify_report(redis, report)
    return report


async def _collect_local_open_orders(date_str: str) -> list[dict]:
    """本地当日 REAL 非终态订单（created_at 为 naive 北京时间）。"""
    from backend.services.trade_shared.models.enums import OrderStatus
    from backend.services.trade_shared.models.order import Order, TradingMode

    day = datetime.strptime(date_str, "%Y%m%d")
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)

    from backend.shared.database_manager_v2 import get_session

    try:
        async with get_session(read_only=True) as db:
            rows = (
                await db.execute(
                    select(
                        Order.order_id,
                        Order.symbol,
                        Order.status,
                        Order.exchange_order_id,
                    ).where(
                        and_(
                            Order.trading_mode == TradingMode.REAL,
                            Order.created_at >= start,
                            Order.created_at < end,
                            Order.status.in_(
                                [
                                    OrderStatus.SUBMITTED,
                                    OrderStatus.PARTIALLY_FILLED,
                                    OrderStatus.PENDING,
                                ]
                            ),
                        )
                    )
                )
            ).all()
    except Exception as exc:  # noqa: BLE001
        logger.error("[CloseAudit] 查询本地订单失败: %s", exc, exc_info=True)
        return []
    return [
        {
            "order_id": str(order_id),
            "symbol": str(symbol or ""),
            "status": str(getattr(status, "value", status) or ""),
            "exchange_order_id": str(exchange_order_id or ""),
        }
        for order_id, symbol, status, exchange_order_id in rows
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
        logger.warning("[CloseAudit] 报表写入失败: %s", exc)


def load_report(redis, date_str: str) -> dict | None:
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


async def _notify_report(redis, report: dict) -> None:
    """异常通知（每日一次）。"""
    client = _redis_client(redis)
    date_str = str(report.get("date") or "")
    if client is not None:
        try:
            if not client.set(
                f"trade:close-audit:notified:{date_str}", "1", nx=True, ex=_REPORT_TTL_SECONDS
            ):
                return
        except Exception:  # noqa: BLE001
            pass

    lines: list[str] = []
    for item in report.get("counter_open") or []:
        lines.append(
            f"柜台未终结：{item.get('symbol')} {item.get('side')} {item.get('status')}"
            f"（委托号 {item.get('order_id')}）"
        )
    for item in report.get("local_stale") or []:
        lines.append(
            f"本地残留：{item.get('symbol')} 本地 {item.get('status')} / "
            f"柜台 {item.get('counter_status') or item.get('reason')}"
            f"（委托号 {item.get('order_id')}）"
        )
    if report.get("errors"):
        lines.append(f"核对任务异常：{'; '.join(report['errors'])}")
    content = f"{date_str} 收盘清理核对发现未了结项：\n" + "\n".join(lines[:8])
    try:
        from backend.shared.notification_publisher import publish_notification_async

        await publish_notification_async(
            user_id="1",
            tenant_id="default",
            title="收盘核对：存在未了结委托",
            content=content,
            type="trading",
            level="warning",
            action_url="/trading",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[CloseAudit] 通知失败: %s", exc)


async def run_close_audit_task() -> None:
    """常驻循环：每交易日到点后跑一次，Redis 标记当日已执行。"""
    cfg = _config()
    if not cfg["enabled"]:
        logger.info("[CloseAudit] 收盘核对任务关闭（CLOSE_AUDIT_ENABLED=0）")
        return

    from backend.services.trade_shared.deps import get_redis

    target_h, target_m = parse_audit_time(cfg["time"])
    logger.info("[CloseAudit] 收盘核对任务启动：每日 %02d:%02d 执行", target_h, target_m)
    last_error = ""
    while True:
        try:
            now = datetime.now(TZ)
            date_str = now.strftime("%Y%m%d")
            redis = get_redis()
            client = _redis_client(redis)
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
                await run_close_audit(redis, date_str)
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
                logger.error("[CloseAudit] 任务异常: %s", exc, exc_info=True)
                last_error = message
        await asyncio.sleep(cfg["interval"])
