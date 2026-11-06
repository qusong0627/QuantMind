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

import asyncio
import logging
import os
import time
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


#: 历史区间上限（自然日）与单次 QMT 调用跨度。QMT 的
#: ``get_history_trade_detail_data`` 一次传整段也行，但管道单响应体积有限，
#: 拆成 ≤31 天一段更稳（券商本地数据一般按月归档）。
_HISTORY_MAX_DAYS = 400
_HISTORY_CHUNK_DAYS = 31


def _compact_day(value: str | None, field: str) -> str:
    text = str(value or "").strip().replace("-", "")
    if len(text) != 8 or not text.isdigit():
        raise HTTPException(
            status_code=400, detail=f"{field} 格式应为 YYYY-MM-DD：{value}"
        )
    return text


def _iter_day_chunks(start: str, end: str, span: int) -> list[tuple[str, str]]:
    from datetime import datetime, timedelta

    begin = datetime.strptime(start, "%Y%m%d")
    finish = datetime.strptime(end, "%Y%m%d")
    if begin > finish:
        raise HTTPException(
            status_code=400, detail=f"开始日期晚于结束日期：{start} > {end}"
        )
    if (finish - begin).days > _HISTORY_MAX_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"历史区间最长 {_HISTORY_MAX_DAYS} 天（收到 {start}–{end}）",
        )
    chunks: list[tuple[str, str]] = []
    cursor = begin
    while cursor <= finish:
        tail = min(cursor + timedelta(days=span - 1), finish)
        chunks.append((cursor.strftime("%Y%m%d"), tail.strftime("%Y%m%d")))
        cursor = tail + timedelta(days=1)
    return chunks


def _pick(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _history_symbol(row: dict[str, Any]) -> str:
    """QMT 历史行 → 前缀式代码（``600519.SH`` → ``SH600519``）。"""
    raw = _pick(row, "symbol", "stock_code", "m_strInstrumentID", "instrument_id")
    market = str(_pick(row, "m_strExchangeID", "exchange_id") or "").strip().upper()
    text = str(raw or "").strip().upper()
    if not text:
        return ""
    if "." in text:
        code, _, suffix = text.partition(".")
        return f"{suffix}{code}" if suffix else code
    if market and text[:1].isdigit():
        return f"{market}{text}"
    return text


def _history_side(row: dict[str, Any]) -> str:
    raw = _pick(row, "side", "direction", "m_nDirection", "m_nOffsetFlag")
    text = str(raw or "").strip().lower()
    if text in ("buy", "sell", "买入", "卖出"):
        return "buy" if text in ("buy", "买入") else "sell"
    code = _to_float(raw, -1)
    if code in (23, 48):
        return "buy"
    if code in (24, 49):
        return "sell"
    return ""


def _normalize_qmt_history_row(row: dict[str, Any], kind: str) -> dict[str, Any]:
    """把 ``get_history_trade_detail_data`` 的原始行压成与当日行同形。

    历史对象字段名随 QMT 版本在 ``m_*`` 与短名之间摆动，这里两套都认；
    UI 只认 ``symbol/side/order_volume/traded_volume/price/...`` 这套。
    """
    from backend.services.live_trading.services.qmt_exec_client import QMT_STATUS_MAP

    is_order = kind == "orders"
    status_code = _pick(row, "status_code", "m_nOrderStatus", "order_status")
    status_map = QMT_STATUS_MAP if is_order else {}
    try:
        status_code_int = (
            int(float(status_code)) if status_code not in (None, "") else -1
        )
    except (TypeError, ValueError):
        status_code_int = -1
    norm: dict[str, Any] = {
        "symbol": _history_symbol(row),
        "stock_code": str(
            _pick(row, "stock_code", "m_strInstrumentID", "instrument_id") or ""
        ),
        "instrument_name": str(
            _pick(row, "instrument_name", "stock_name", "m_strInstrumentName") or ""
        ),
        "side": _history_side(row),
        "time": str(
            _pick(row, "time", "m_strTradeTime", "m_strOrderTime", "m_strTime") or ""
        ),
        "date": str(
            _pick(row, "date", "m_strTradeDate", "m_strOrderDate", "m_strDate") or ""
        ),
        "order_sysid": str(_pick(row, "order_sysid", "m_strOrderSysID") or ""),
        "order_id": str(_pick(row, "order_id", "m_strOrderRef") or ""),
        "remark": str(_pick(row, "order_remark", "m_strRemark", "remark") or ""),
    }
    if is_order:
        norm.update(
            {
                "order_volume": _to_float(
                    _pick(row, "order_volume", "m_nOrderVolume", "volume")
                ),
                "traded_volume": _to_float(
                    _pick(row, "traded_volume", "m_nTradedVolume")
                ),
                "price": _to_float(_pick(row, "price", "m_dLimitPrice", "m_dPrice")),
                "traded_price": _to_float(
                    _pick(row, "traded_price", "m_dTradedPrice", "m_dPrice")
                ),
                "status_code": status_code_int,
                "status": status_map.get(status_code_int, "SUBMITTED"),
            }
        )
    else:
        volume = _to_float(_pick(row, "traded_volume", "m_nVolume", "volume"))
        price = _to_float(_pick(row, "traded_price", "m_dPrice", "price"))
        norm.update(
            {
                "trade_id": str(_pick(row, "trade_id", "m_strTradeID") or ""),
                "order_volume": volume,
                "traded_volume": volume,
                "price": price,
                "traded_price": price,
                "traded_amount": _to_float(
                    _pick(row, "traded_amount", "m_dTradeAmount"), volume * price
                ),
                "status": "FILLED",
            }
        )
    return norm


async def _qmt_history_records(
    start_day: str, end_day: str, kind: str
) -> list[dict[str, Any]]:
    """QMT **历史**成交/委托（``get_history_trade_detail_data``，按日期区间）。

    这是券商侧唯一的历史入口：柜台实时接口只回当日，跨日必须走它
    （或由客户端导出对账单）。区间按 ≤31 天切段，逐段调用后合并。
    """
    from backend.services.live_trading.services.qmt_exec_client import (
        get_qmt_exec_client,
    )

    start_day = _compact_day(start_day, "start")
    end_day = _compact_day(end_day, "end")
    chunks = _iter_day_chunks(start_day, end_day, _HISTORY_CHUNK_DAYS)
    client = get_qmt_exec_client()
    await client.refresh_settings()
    detail = "ORDER" if kind == "orders" else "DEAL"
    rows: list[dict[str, Any]] = []
    for chunk_start, chunk_end in chunks:
        part = await asyncio.wait_for(
            client.query_history_trades(chunk_start, chunk_end, detail), timeout=45.0
        )
        rows.extend(part or [])
    return [_normalize_qmt_history_row(row, kind) for row in rows]


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
        raise HTTPException(
            status_code=400, detail="通达信桥地址未配置（设置 → 券商接入 → 通达信）"
        )
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
    date: str | None = Query(None, description="YYYY-MM-DD；留空=当日"),
    start: str | None = Query(None, description="区间开始 YYYY-MM-DD（QMT 历史）"),
    end: str | None = Query(None, description="区间结束 YYYY-MM-DD（QMT 历史）"),
    auth: AuthContext = Depends(get_auth_context),
    redis: Any = Depends(get_redis),
):
    """柜台侧委托/成交。

    * ``source`` 留空 = 用「券商接入」里当前选定的券商
      （``broker:selected:{market}``，回退 ``REAL_BROKER_TYPE``）；
    * ``kind`` = orders | trades；
    * 当日：实时柜台接口；**历史**：``start``/``end`` 区间（QMT 走
      ``get_history_trade_detail_data``；通达信桥没有历史接口，只回当日委托）；
    * 跨日完整记录仍在系统账本（``/api/v1/orders``，PG 全历史）。
    """
    _ = auth
    src = _selected_source(redis, market, source)
    if not src:
        raise HTTPException(status_code=400, detail="未选择券商（设置 → 券商接入）")

    today = time.strftime("%Y-%m-%d")
    range_start = str(start or date or "").strip()
    range_end = str(end or date or start or "").strip()
    if not range_start:
        range_start = range_end = today
    if not range_end:
        range_end = range_start
    is_today_only = range_start == today and range_end == today
    history_mode = not is_today_only

    try:
        if src.startswith("qmt"):
            if history_mode:
                rows = await _qmt_history_records(
                    _compact_day(range_start, "start"),
                    _compact_day(range_end, "end"),
                    kind,
                )
                note = (
                    f"QMT 历史（{range_start} ~ {range_end}）：来自柜台 "
                    "get_history_trade_detail_data"
                )
            else:
                rows = await _qmt_records(kind)
                note = ""
        elif src.startswith("tdx"):
            if history_mode:
                rows, note = (
                    [],
                    (
                        "通达信柜台接口只提供**当日**委托（无历史委托/成交查询）。"
                        f"{range_start}~{range_end} 的完整记录请看系统账本，"
                        "或从通达信客户端导出对账单。"
                    ),
                )
            else:
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
        "start": range_start,
        "end": range_end,
        "count": len(rows),
        "note": note,
        "rows": rows,
    }
