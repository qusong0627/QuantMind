"""持仓预警 API（持仓哨兵的读侧 + 用户配置）。

- `GET  /api/v1/trading/holding-alerts`              预警列表（默认只看 active）+ 计数 + 配置 + 哨兵状态
- `POST /api/v1/trading/holding-alerts/{id}/dismiss`  忽略（留痕，不再出现在默认列表）
- `POST /api/v1/trading/holding-alerts/{id}/executed` 已卖出（前端推送成功后的回写）
- `GET  /api/v1/trading/holding-alerts/config`        监控范围/阈值/三通道开关
- `PUT  /api/v1/trading/holding-alerts/config`        改配置（部分字段合并，坏值退回默认）
- `GET  /api/v1/trading/holding-alerts/status`        哨兵存活与监控面（如实显示「没在跑」）

**读写同一张表、同一份 Redis**：预警由 trade 服务的 `holding_sentinel` 写入，
状态回写与配置由 api 服务直接落 PG / Redis——两边共用 `trade_shared.redis_client`
（db=2），所以配置 PUT 后哨兵下一轮就能读到，不需要跨服务通知。

口径单源：判定规则在 `backend.shared.holding_alert_contract`，这里只读不判定。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text

from backend.services.api.user_app.middleware.auth import get_current_user
from backend.shared.database_manager_v2 import get_session
from backend.shared.holding_alert_contract import (
    CONFIG_KEY_PREFIX,
    DEFAULT_CONFIG,
    STATUS_ACTIVE,
    STATUS_DISMISSED,
    STATUS_EXECUTED,
    TABLE,
    ensure_holding_alerts_table_async,
    normalize_status,
    parse_alert_config,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/trading/holding-alerts", tags=["Holding Alerts"])

#: 哨兵状态键（与 holding_sentinel 共用；读不到 = 没在跑，如实告知）
_SENTINEL_STATUS_KEY = "qm:holding:sentinel:status"

_MAX_LIMIT = 200


class AlertConfigUpdate(BaseModel):
    """部分更新：只传要改的字段（未传的保持原值）。"""

    enabled: bool | None = None
    score_threshold: float | None = None
    watch_sim: bool | None = None
    watch_real: bool | None = None
    watch_manual: bool | None = None
    notify_inapp: bool | None = None
    notify_desktop: bool | None = None
    notify_sound: bool | None = None
    min_severity: str | None = None


def _tenant_of(current_user: dict) -> str:
    return str(current_user.get("tenant_id") or "default")


async def _alert_scope(current_user: dict) -> tuple[str, list[str], str]:
    """→ ``(tenant, 可匹配的 user_id 别名, 规范 user_id)``。

    预警行的 ``user_id`` 是 ``users.user_id``（哨兵遍历 users 写下来的），而 JWT 里的
    sub 可能是 ``users.id`` 或历史 admin 别名（``00000001``/``1``）。只按 token 原文
    查会「自己的预警一条都查不到」。别名集合一次性覆盖三种写法（见
    `simulation_account_keys.ledger_user_id_candidates`），不动数据。

    **规范 id 单独返回**：Redis 配置键与状态键必须与哨兵**逐字节一致**（它用
    ``users.user_id`` 拼键）——把配置写到 ``00000001`` 下去，用户以为改了、哨兵读的是
    ``10000001``，这个偏差不会报错。
    """
    from backend.shared.simulation_account_keys import ledger_user_id_candidates

    raw = str(current_user.get("user_id") or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="Invalid user_id in token")
    tenant = _tenant_of(current_user)
    aliases = {raw, *ledger_user_id_candidates(raw)}
    canonical = raw
    try:
        async with get_session(read_only=True) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT id, user_id FROM users "
                        "WHERE user_id = :raw OR id::text = :raw LIMIT 1"
                    ),
                    {"raw": raw},
                )
            ).first()
    except Exception as exc:  # noqa: BLE001 - 查不到就用 token 原文兜底，不阻断读
        logger.warning("[holding_alerts] 用户别名解析失败: %s", exc)
        row = None
    if row is not None:
        aliases.add(str(row[0]))
        aliases.add(str(row[1]))
        canonical = str(row[1])
    return tenant, sorted(a for a in aliases if a), canonical


def _config_key(tenant: str, user_id: str) -> str:
    return f"{CONFIG_KEY_PREFIX}{tenant}:{user_id}"


def _redis():
    """**原始** redis 客户端（hash 操作 wrapper 没暴露，并且它的 ``get`` 会 json.loads）。

    直接用 ``get_redis()`` 返回的 wrapper 调 ``hget``/``hgetall`` 会抛 AttributeError；
    若调用方再把异常吞成「用默认值」，就会得到一个静默失效的配置面板——用户改了阈值、
    界面回显默认值、哨兵按默认值跑，全程没有一处报错。
    """
    from backend.services.trade_shared.redis_client import get_redis

    wrapper = get_redis()
    return getattr(wrapper, "client", None)


def _load_config(tenant: str, user_id: str) -> dict[str, Any]:
    """读用户配置；Redis 不可用时返回默认值（默认全开，不会静默少提醒）。"""
    client = _redis()
    if client is None:
        logger.warning("[holding_alerts] Redis 不可用，配置回退默认值")
        return dict(DEFAULT_CONFIG)
    try:
        return parse_alert_config(
            client.hget(_config_key(tenant, user_id), "settings")
        )
    except Exception as exc:  # noqa: BLE001 - 配置读不到不该让面板 500
        logger.warning("[holding_alerts] 配置读取失败: %s", exc)
        return dict(DEFAULT_CONFIG)


def _sentinel_status(tenant: str, user_id: str) -> dict[str, Any]:
    """哨兵存活 + 该用户的监控面（读不到 running=False，不粉饰）。"""
    try:
        client = _redis()
        if client is None:
            return {"running": False, "reason": "Redis 不可用"}
        raw = client.hgetall(_SENTINEL_STATUS_KEY) or {}
        per_user = {}
        if raw:
            try:
                per_user = json.loads(
                    client.get(f"{_SENTINEL_STATUS_KEY}:users") or "{}"
                )
            except (TypeError, ValueError):
                per_user = {}
        entry = (per_user or {}).get(f"{tenant}:{user_id}")
        return {
            "running": bool(raw),
            "lastScanAt": raw.get("updated_at"),
            "lastScanEpoch": int(raw["last_scan_epoch"])
            if raw.get("last_scan_epoch")
            else None,
            "users": int(raw["users"]) if raw.get("users") else None,
            "monitored": int(raw["monitored"]) if raw.get("monitored") else None,
            "orphanAccounts": int(raw["orphan_accounts"])
            if raw.get("orphan_accounts")
            else 0,
            "mine": entry or None,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("[holding_alerts] 哨兵状态读取失败: %s", exc)
        return {"running": False, "reason": str(exc)}


@router.get("")
async def list_alerts(
    status: str = Query("active", description="active / dismissed / executed / all"),
    limit: int = Query(50, ge=1, le=_MAX_LIMIT),
    symbol: str | None = Query(None, description="只看某只票（任意键形）"),
    current_user: dict = Depends(get_current_user),
):
    """预警列表（默认只 active，按时间倒序）。"""
    if not await ensure_holding_alerts_table_async():
        raise HTTPException(status_code=503, detail="预警存储不可用")
    tenant, aliases, canonical = await _alert_scope(current_user)

    where = ["tenant_id = :tid", "user_id = ANY(:uids)"]
    params: dict[str, Any] = {"tid": tenant, "uids": aliases, "limit": limit}
    wanted = normalize_status(status) if status and status != "all" else None
    if wanted:
        where.append("status = :status")
        params["status"] = wanted
    if symbol:
        from backend.shared.signal_scores import normalize_position_symbol

        sym = normalize_position_symbol(symbol)
        if not sym:
            raise HTTPException(status_code=400, detail=f"无法识别的标的: {symbol}")
        where.append("symbol = :symbol")
        params["symbol"] = sym

    sql = (
        "SELECT id, symbol, stock_name, kind, severity, title, content, detail, "
        "score_prev, score_now, score_as_of, status, action_url, created_at, "
        "resolved_at FROM " + TABLE + " WHERE " + " AND ".join(where) + " "
        "ORDER BY created_at DESC, id DESC LIMIT :limit"
    )
    counts_sql = (
        "SELECT status, COUNT(*) FROM " + TABLE + " "
        "WHERE tenant_id = :tid AND user_id = ANY(:uids) GROUP BY status"
    )
    try:
        async with get_session(read_only=True) as session:
            rows = (await session.execute(text(sql), params)).fetchall()
            count_rows = (
                await session.execute(
                    text(counts_sql), {"tid": tenant, "uids": aliases}
                )
            ).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.error("[holding_alerts] 列表查询失败: %s", exc)
        raise HTTPException(status_code=503, detail="预警查询失败") from exc

    items = [
        {
            "id": int(r[0]),
            "symbol": r[1],
            "stockName": r[2] or "",
            "kind": r[3],
            "severity": r[4],
            "title": r[5],
            "content": r[6] or "",
            "detail": r[7] or {},
            "scorePrev": r[8],
            "scoreNow": r[9],
            "scoreAsOf": str(r[10])[:10] if r[10] else None,
            "status": r[11],
            "actionUrl": r[12] or "",
            "createdAt": r[13].isoformat() if r[13] else None,
            "resolvedAt": r[14].isoformat() if r[14] else None,
        }
        for r in rows
    ]
    counts = {str(s): int(c) for s, c in count_rows}
    counts["total"] = sum(counts.values())
    return {
        "success": True,
        "data": {
            "items": items,
            "counts": counts,
            "config": _load_config(tenant, canonical),
            "sentinel": _sentinel_status(tenant, canonical),
        },
    }


async def _set_status(alert_id: int, current_user: dict, status: str) -> dict[str, Any]:
    """状态回写（限定本人 + 未定局的预警；重复点不报错，幂等）。"""
    if not await ensure_holding_alerts_table_async():
        raise HTTPException(status_code=503, detail="预警存储不可用")
    tenant, aliases, _canonical = await _alert_scope(current_user)
    sql = (
        "UPDATE " + TABLE + " SET status = :status, "
        "resolved_at = COALESCE(resolved_at, NOW()) "
        "WHERE id = :id AND tenant_id = :tid AND user_id = ANY(:uids) "
        "AND status = :active RETURNING id"
    )
    try:
        async with get_session(read_only=False) as session:
            row = (
                await session.execute(
                    text(sql),
                    {
                        "status": status,
                        "id": int(alert_id),
                        "tid": tenant,
                        "uids": aliases,
                        "active": STATUS_ACTIVE,
                    },
                )
            ).first()
            session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.error("[holding_alerts] 状态回写失败 id=%s: %s", alert_id, exc)
        raise HTTPException(status_code=503, detail="预警状态更新失败") from exc
    if row is None:
        # 可能是「不存在」也可能是「已经定局」——两种都不是错误，但要让前端知道
        return {"success": True, "data": {"id": int(alert_id), "changed": False}}
    return {
        "success": True,
        "data": {"id": int(alert_id), "changed": True, "status": status},
    }


@router.post("/{alert_id}/dismiss")
async def dismiss_alert(alert_id: int, current_user: dict = Depends(get_current_user)):
    """忽略这条预警（不再出现在默认列表；留痕不删）。"""
    return await _set_status(alert_id, current_user, STATUS_DISMISSED)


@router.post("/{alert_id}/executed")
async def mark_executed(alert_id: int, current_user: dict = Depends(get_current_user)):
    """标记已卖出（前端面板上的动作按钮推送成功后的回写）。"""
    return await _set_status(alert_id, current_user, STATUS_EXECUTED)


@router.get("/config")
async def get_config(current_user: dict = Depends(get_current_user)):
    tenant, _aliases, canonical = await _alert_scope(current_user)
    return {"success": True, "data": _load_config(tenant, canonical)}


@router.put("/config")
async def update_config(
    payload: AlertConfigUpdate, current_user: dict = Depends(get_current_user)
):
    """部分更新：与已存值合并后**整体校验**再落 Redis（坏值退回默认，不落半截配置）。"""
    tenant, _aliases, canonical = await _alert_scope(current_user)
    current = _load_config(tenant, canonical)
    patch = {k: v for k, v in payload.model_dump().items() if v is not None}
    merged = parse_alert_config({**current, **patch})
    try:
        client = _redis()
        if client is None:
            raise RuntimeError("Redis 不可用")
        client.hset(
            _config_key(tenant, canonical),
            "settings",
            json.dumps(merged, ensure_ascii=False),
        )
    except Exception as exc:  # noqa: BLE001 - 配置不落库必须显式失败（否则用户以为改了）
        logger.error("[holding_alerts] 配置写入失败: %s", exc)
        raise HTTPException(status_code=503, detail="预警配置保存失败") from exc
    return {"success": True, "data": merged}


@router.get("/status")
async def sentinel_status(current_user: dict = Depends(get_current_user)):
    tenant, _aliases, canonical = await _alert_scope(current_user)
    return {"success": True, "data": _sentinel_status(tenant, canonical)}
