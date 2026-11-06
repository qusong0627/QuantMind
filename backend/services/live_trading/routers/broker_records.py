"""券商柜台侧的委托/成交查询（跟随「券商接入」选定的通道）。

前端「交易记录」除了系统账本（PG orders/trades），还要能看券商自己那份：
选了 QMT 就看 QMT、选了通达信就看通达信（2026-11-06 用户要求）。

数据源：
* ``qmt_exec`` → 管道 ``query_orders`` / ``query_trades``（柜台实时，走伴生桥）
* ``tdx``      → Windows 桥 ``POST /api/v1/tdx/orders/query``（TDX 桥只有委托
  查询，没有成交明细；成交维度返回空并带 ``note`` 说明，不伪造数据）

注意：柜台接口只回**当日**委托/成交。历史记录在系统账本（PG），两个视图
按「系统账本 / 券商柜台」分开呈现，不混在一起。
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

from backend.services.trade_shared.deps import (
    AuthContext,
    get_auth_context,
    get_redis,
)

router = APIRouter()
logger = logging.getLogger(__name__)

_SELECTED_KEY = "broker:selected:{market}"
_TDX_CFG_KEY = "broker:config:tdx"


def _selected_source(redis: Any, market: str, override: str | None) -> str:
    """当前券商：显式 override > broker:selected:{market} > REAL_BROKER_TYPE。"""
    if override:
        return str(override).strip().lower()
    try:
        raw = redis.get(_SELECTED_KEY.format(market=str(market or "CN").upper()))
    except Exception:  # noqa: BLE001
        raw = None
    value = str(raw or "").strip().lower()
    if value:
        return value
    try:
        from backend.shared.real_positions import active_broker_type

        return str(active_broker_type() or "").strip().lower()
    except Exception:  # noqa: BLE001
        return ""


def _bridge_cfg(redis: Any) -> tuple[str, str]:
    cfg = {}
    try:
        cfg = redis.get(_TDX_CFG_KEY) or {}
    except Exception:  # noqa: BLE001
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    url = str(cfg.get("bridge_url") or os.getenv("TDX_BRIDGE_URL", "")).rstrip("/")
    token = str(cfg.get("bridge_token") or os.getenv("TDX_BRIDGE_TOKEN", ""))
    return url, token


async def _qmt_records(kind: str) -> list[dict[str, Any]]:
    from backend.services.live_trading.services.qmt_exec_client import (
        get_qmt_exec_client,
    )

    client = get_qmt_exec_client()
    await client.refresh_settings()
    if kind == "trades":
        return list(await client.query_trades() or [])
    return list(await client.query_orders() or [])


async def _tdx_records(redis: Any, kind: str) -> tuple[list[dict[str, Any]], str]:
    if kind == "trades":
        # 通达信桥只实现了 query_stock_orders；成交明细没有接口，如实说明。
        return [], "通达信桥不提供成交明细，仅可查当日委托"
    url, token = _bridge_cfg(redis)
    if not url:
        raise HTTPException(status_code=400,
                            detail="通达信桥地址未配置（设置 → 券商接入 → 通达信）")
    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            resp = await http.post(
                f"{url}/api/v1/tdx/orders/query",
                json={},
                headers={"Authorization": f"Bearer {token}"},
            )
        payload = resp.json() if resp.content else {}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=f"通达信桥查询失败：{type(exc).__name__}: {exc}",
        ) from exc
    if resp.status_code >= 400:
        detail = payload.get("error") if isinstance(payload, dict) else payload
        raise HTTPException(status_code=502, detail=f"通达信桥拒绝：{detail}")
    rows = payload.get("orders") if isinstance(payload, dict) else []
    return list(rows or []), ""


@router.get("/broker-records")
async def broker_records(
    kind: str = Query("orders", pattern="^(orders|trades)$"),
    market: str = "CN",
    source: str | None = None,
    auth: AuthContext = Depends(get_auth_context),
    redis: Any = Depends(get_redis),
):
    """柜台侧委托/成交（跟随当前券商）。

    * ``source`` 留空 = 用「券商接入」里当前选定的券商
      （``broker:selected:{market}``，回退 ``REAL_BROKER_TYPE``）；
    * ``kind`` = orders | trades；
    * 只回**当日**柜台数据；跨日历史在系统账本（``/api/v1/orders``）。
    """
    _ = auth
    src = _selected_source(redis, market, source)
    if not src:
        raise HTTPException(status_code=400, detail="未选择券商（设置 → 券商接入）")

    try:
        if src.startswith("qmt"):
            rows = await _qmt_records(kind)
            note = ""
        elif src.startswith("tdx"):
            rows, note = await _tdx_records(redis, kind)
        else:
            raise HTTPException(
                status_code=400,
                detail=f"当前券商（{src}）暂不支持柜台记录查询",
            )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("[BrokerRecords] %s %s 查询失败: %s", src, kind, exc)
        raise HTTPException(
            status_code=502,
            detail=f"柜台查询失败：{type(exc).__name__}: {exc}",
        ) from exc

    return {
        "success": True,
        "broker": src,
        "market": str(market or "CN").upper(),
        "kind": kind,
        "count": len(rows),
        "note": note,
        "rows": rows,
    }
