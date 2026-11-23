"""价格阈值预警（P0 即时）：到价推送通知，**不下单**。

为什么不复用 SLTP 自动止损：TQ 模拟盘对智能体单要求客户端人工确认
（``order_stock`` 返回 ``Value=1``），止损单会卡在确认队列并过期——2026-11-23
三笔实测（601825 卖 1000/500/500）桥无委托、平台侧停在 ``submitted``。
在免确认通道就位前，价格预警是唯一能真正送达的风控面。

配置键（trade 库 db2）：``qm:price_alert:config``::

    {"enabled": true,
     "rules": [{"symbol": "601825.SH", "below": 9.34, "note": "成本下方 3%"}]}

取价：``real_account_snapshots`` 最新 ``tdx_bridge`` 快照（桥 30s 同步），
不额外打行情接口。冷却：同规则每日一次（``qm:price_alert:fired:*``，7 天过期）。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

CONFIG_KEY = "qm:price_alert:config"
FIRED_PREFIX = "qm:price_alert:fired"
BEIJING = timezone(timedelta(hours=8))
SNAPSHOT_SQL = (
    "SELECT payload_json FROM real_account_snapshots "
    "WHERE tenant_id = :tid AND user_id = :uid AND source = 'tdx_bridge' "
    "ORDER BY snapshot_at DESC LIMIT 1"
)


def parse_config(raw: Any) -> dict[str, Any]:
    """宽容读配置：坏 JSON/坏规则一律丢，不抛（预警任务不能因配置坏掉而停）。"""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return {"enabled": False, "rules": []}
    if not isinstance(raw, dict):
        return {"enabled": False, "rules": []}
    rules: list[dict[str, Any]] = []
    for item in raw.get("rules") or []:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").strip().upper()
        try:
            below = float(item.get("below"))
        except (TypeError, ValueError):
            continue
        if not symbol or below <= 0:
            continue
        rules.append(
            {"symbol": symbol, "below": below, "note": str(item.get("note") or "")}
        )
    return {"enabled": bool(raw.get("enabled", True)), "rules": rules}


def evaluate_price_alerts(
    rules: list[dict[str, Any]],
    prices: dict[str, float],
    *,
    fired: set[str],
    day: str,
) -> list[dict[str, Any]]:
    """纯判定：现价 ≤ 阈值且今日未触发 → 返回命中规则（测试与生产共用一份）。"""
    hits: list[dict[str, Any]] = []
    for rule in rules:
        symbol = str(rule.get("symbol") or "")
        below = float(rule.get("below") or 0.0)
        price = prices.get(symbol)
        if price is None or price <= 0 or below <= 0:
            continue
        if price > below:
            continue
        key = f"{FIRED_PREFIX}:{symbol}:{below:g}:{day}"
        if key in fired:
            continue
        hits.append(
            {
                "symbol": symbol,
                "below": below,
                "price": price,
                "note": str(rule.get("note") or ""),
                "fired_key": key,
            }
        )
    return hits


async def _latest_prices(tenant: str, user: str) -> dict[str, float]:
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=True) as db:
        row = (
            await db.execute(text(SNAPSHOT_SQL), {"tid": tenant, "uid": user})
        ).fetchone()
    if row is None:
        return {}
    payload = row[0]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return {}
    out: dict[str, float] = {}
    for item in (payload or {}).get("positions") or []:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").strip().upper()
        try:
            price = float(item.get("price") or 0.0)
        except (TypeError, ValueError):
            continue
        if symbol and price > 0:
            out[symbol] = price
    return out


async def check_price_alerts() -> list[dict[str, Any]]:
    """读配置 → 取价 → 判定 → 推送。返回本轮命中（不含被冷却的）。"""
    from backend.services.trade_shared.redis_client import redis_client

    if redis_client.client is None:
        redis_client.connect()
    client = redis_client.client
    raw = client.get(CONFIG_KEY)
    cfg = parse_config(raw)
    if not cfg["enabled"] or not cfg["rules"]:
        return []

    tenant = "default"
    user = os.getenv("QM_DECISION_ACCOUNT_USER_ID", "").strip() or "10000001"
    now = datetime.now(BEIJING)
    day = now.strftime("%Y%m%d")

    prices = await _latest_prices(tenant, user)
    if not prices:
        return []

    fired = {
        key.decode("utf-8", "replace") if isinstance(key, (bytes, bytearray)) else str(key)
        for key in (client.keys(f"{FIRED_PREFIX}:*:{day}") or [])
    }
    hits = evaluate_price_alerts(cfg["rules"], prices, fired=fired, day=day)
    if not hits:
        return []

    from backend.shared.notification_publisher import publish_notification

    delivered: list[dict[str, Any]] = []
    for hit in hits:
        symbol = hit["symbol"]
        price = hit["price"]
        below = hit["below"]
        pct = (price / below - 1.0) * 100.0 if below > 0 else 0.0
        note = f"（{hit['note']}）" if hit.get("note") else ""
        title = f"📉 价格预警 · {symbol} 跌破 {below:g}"
        content = f"{symbol} 现价 {price:.3f} ≤ 预警线 {below:g}（{pct:+.2f}%）{note}"
        try:
            publish_notification(
                user_id=user,
                tenant_id=tenant,
                title=title,
                content=content,
                type="price_alert",
                level="critical",
                action_url="/trading",
                expire_days=3,
                qq_alert=True,
            )
        except Exception as exc:  # noqa: BLE001 - 推送失败不阻断其余规则
            logger.warning("[price_alert] 推送失败 %s: %s", symbol, exc)
            continue
        client.set(hit["fired_key"], now.isoformat(), ex=7 * 86400)
        delivered.append(hit)
        logger.warning("[price_alert] %s 现价 %.3f ≤ %.3f，已推送", symbol, price, below)
    return delivered
