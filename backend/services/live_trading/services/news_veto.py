"""新闻风险 veto（T-P6-12）：标级 veto 标记 + 策略级开关 + 买单过滤（单一实现）。

数据流：新闻情报服务（engine/news_intel_engine）把 **critical 风险事件** 命中的标的写入
``risk:veto:news:{date}:{SYMBOL}``（当日 TTL）→ 本模块供两条消费路径共用：
① 模拟托管引擎（simulation/engine.py `_apply_news_veto`）在调仓买单落地前按**策略配置
   ``risk.veto.news_event=true``** 过滤并留痕（risk_events）；
② 运维/巡检只读查询。

口径：
- 标记键**平台级**（不带租户）——新闻风险是市场级事实，租户差异由策略开关表达；
- 策略开关读取 ``strategies.config``（容忍三种嵌套形态：``risk.veto.news_event`` /
  ``risk.veto_news_event`` / 顶层 ``veto_news_event``），60s 进程内缓存；
- 过滤语义与 risk_lock.filter_buy_orders 一致：**只拦买单**（side=BUY），卖单/撤单不受影响。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from backend.services.live_trading.services.risk_lock import (
    lock_ttl_seconds,
    normalize_lock_symbol,
)

logger = logging.getLogger(__name__)
_SH_TZ = ZoneInfo("Asia/Shanghai")

VETO_PREFIX = "risk:veto:news:"
_TRUTHY = {"1", "true", "yes", "on"}

_flag_cache: dict[str, tuple[float, bool]] = {}
_FLAG_CACHE_TTL_S = 60.0


def _trade_date_str(trade_date: date | str | None) -> str:
    if trade_date is None:
        return datetime.now(_SH_TZ).date().isoformat()
    if isinstance(trade_date, date):
        return trade_date.isoformat()
    return str(trade_date)


def veto_key(symbol: str, *, trade_date: date | str | None = None) -> str:
    return f"{VETO_PREFIX}{_trade_date_str(trade_date)}:{normalize_lock_symbol(symbol)}"


def _raw_redis(redis: Any):
    """兼容三种入参：原生 redis 客户端 / 带 .client 的包装器（trade_shared）/ None。

    注意：redis-py 的 ``Redis`` 自身也有 ``client()`` **方法**——必须用 callable 判定，
    否则会把方法当连接对象（2026-09-17 集成测试实锤：'function' has no attribute 'scan_iter'）。
    """
    if redis is None:
        return None
    client = getattr(redis, "client", None)
    if client is not None and not callable(client):
        return client
    return redis


def mark_news_veto(
    redis: Any,
    symbols: Iterable[str],
    *,
    trade_date: date | str | None = None,
    ttl_seconds: int | None = None,
) -> int:
    """写入当日 veto 标记（幂等覆盖）。返回写入条数（空/异常不抛，返回 0）。"""
    client = _raw_redis(redis)
    if client is None:
        return 0
    ttl = int(ttl_seconds) if ttl_seconds else lock_ttl_seconds()
    written = 0
    for symbol in symbols or []:
        text = str(symbol or "").strip()
        if not text:
            continue
        try:
            client.set(veto_key(text, trade_date=trade_date), "1", ex=max(60, ttl))
            written += 1
        except Exception as exc:  # noqa: BLE001 - 写标记失败不中断发布循环
            logger.warning("[news_veto] 标记写入失败 %s: %s", text, exc)
    return written


def load_news_vetoes(redis: Any, *, trade_date: date | str | None = None) -> set[str]:
    """读取当日 veto 标的集合（后缀式）。异常返回空集（fail-open：绝不因读失败拦单）。"""
    client = _raw_redis(redis)
    if client is None:
        return set()
    prefix = f"{VETO_PREFIX}{_trade_date_str(trade_date)}:"
    out: set[str] = set()
    try:
        for raw_key in client.scan_iter(match=f"{prefix}*", count=200):
            symbol = str(raw_key).rsplit(":", 1)[-1]
            if symbol:
                out.add(symbol)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[news_veto] veto 集合读取失败: %s", exc)
        return set()
    return out


def filter_news_veto_buys(orders: Iterable[Any], vetoes: set[str]) -> tuple[list[Any], list[Any]]:
    """按 veto 集合过滤买单；返回 (kept, dropped)。语义与 risk_lock.filter_buy_orders 一致。"""
    kept: list[Any] = []
    dropped: list[Any] = []
    locked = {normalize_lock_symbol(s) for s in (vetoes or set())}
    for order in orders:
        side = str(getattr(order, "side", "") or "").upper()
        symbol = normalize_lock_symbol(str(getattr(order, "symbol", "") or ""))
        if side == "BUY" and symbol in locked:
            dropped.append(order)
            continue
        kept.append(order)
    return kept, dropped


def _config_flag(config: dict[str, Any] | None) -> bool:
    """策略配置中的新闻 veto 开关（容忍三种形态；缺省 False）。"""
    cfg = config or {}
    try:
        risk = cfg.get("risk") or {}
        veto = risk.get("veto") or {}
        candidates = (
            veto.get("news_event"),
            risk.get("veto_news_event"),
            cfg.get("veto_news_event"),
        )
    except AttributeError:
        return False
    for value in candidates:
        if value is None:
            continue
        return str(value).strip().lower() in _TRUTHY
    return False


def strategy_news_veto_enabled(
    strategy_id: Any,
    *,
    now_fn: Any = time.monotonic,
    cache_ttl_s: float = _FLAG_CACHE_TTL_S,
) -> bool:
    """策略是否启用新闻 veto（读 strategies.config，60s 缓存；读失败=False 保守放行）。"""
    sid = str(strategy_id or "").strip()
    if not sid.isdigit():
        return False
    cached = _flag_cache.get(sid)
    if cached and (now_fn() - cached[0]) < cache_ttl_s:
        return cached[1]
    enabled = False
    try:
        from sqlalchemy import text

        from backend.shared.sync_db import sync_session

        with sync_session() as session:
            row = session.execute(
                text("SELECT config FROM strategies WHERE id = :sid"), {"sid": int(sid)}
            ).fetchone()
        enabled = _config_flag((row[0] if row else None) or {})
    except Exception as exc:  # noqa: BLE001 - 读失败不拦单（保守放行），有日志可查
        logger.warning("[news_veto] 策略 %s 配置读取失败: %s", sid, exc)
        enabled = False
    _flag_cache[sid] = (now_fn(), enabled)
    return enabled


def audit_veto_drop(
    *,
    tenant_id: str,
    user_id: Any,
    trade_date: date | str | None,
    symbols: list[str],
    message: str,
) -> None:
    """veto 拦截留痕（risk_events；best-effort，失败仅日志）。"""
    if not symbols:
        return
    try:
        from sqlalchemy import text

        from backend.shared.sync_db import sync_session

        with sync_session() as session:
            for symbol in symbols:
                session.execute(
                    text(
                        "INSERT INTO risk_events (rule_id, rule_type, tenant_id, user_id, "
                        "trade_date, symbol, action, status, message, created_at) "
                        "VALUES (NULL, 'news_event_veto', :t, :u, :d, :s, 'deny', 'applied', "
                        "        :msg, NOW())"
                    ),
                    {
                        "t": str(tenant_id or "default")[:64],
                        "u": int(str(user_id)) if str(user_id).isdigit() else 0,
                        "d": _trade_date_str(trade_date),
                        "s": str(symbol)[:32],
                        "msg": message[:500],
                    },
                )
            session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[news_veto] 拦截留痕写入失败: %s", exc)
