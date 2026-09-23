"""风控运维权（T-RC-02）：状态 / 配置 / 全撤——require_admin 收口。

- `GET  /api/v1/risk/status`     —— 配置 + 当日决策计数（含影子拒绝数，翻闸判据）
- `POST /api/v1/risk/config`     —— 改配置（enabled/shadow/rules），version 自增 + 留痕
- `POST /api/v1/risk/cancel-all` —— HALT 全撤指定账户全部未成交模拟单（OrderRouter.cancel_all）

纪律：影子→强制翻闸是一次显式配置变更（shadow=false），版本号随变更自增并写入 Redis
`qm:risk:config`；决策流与计数见 `qm:risk:decisions|metrics:{date}`。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.services.trade.services import risk_gate_service as risk
from backend.services.trade_shared.deps import (
    AuthContext,
    get_db,
    get_redis,
    require_admin,
)

logger = logging.getLogger(__name__)
router = APIRouter()

CST = timezone(timedelta(hours=8))


def _client(redis: Any):
    return getattr(redis, "client", redis)


@router.get("/risk/status")
async def risk_status(
    redis: Any = Depends(get_redis),
    auth: AuthContext = Depends(require_admin),
) -> dict[str, Any]:
    """配置 + 当日计数（影子报告基础数据）。"""
    try:
        client = _client(redis)
        raw = client.hgetall(risk.CONFIG_KEY) or {}
        day = datetime.now(tz=CST).strftime("%Y%m%d")
        metrics = client.hgetall(risk.METRICS_KEY.format(date=day)) or {}
        rules: dict[str, Any] = {}
        try:
            rules = json.loads(raw.get("rules") or "{}")
        except (TypeError, ValueError):
            rules = {}
        return {
            "success": True,
            "data": {
                "configured": bool(raw),
                "enabled": str(raw.get("enabled", "false")),
                "shadow": str(raw.get("shadow", "true")),
                "version": str(raw.get("version", "0")),
                "rules": rules,
                "today": metrics,
                "caliber": "影子=判定留痕不拦单；rejected=全量拒绝计数，shadow_rejected=其中未拦的",
            },
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Redis 不可读: {exc}") from exc


@router.get("/risk/tier")
async def risk_tier_status(
    redis: Any = Depends(get_redis),
    auth: AuthContext = Depends(require_admin),
) -> dict[str, Any]:
    """当前风险档位（P1.8）：原文 + 新鲜度 + 实际改写（或不改写）了哪些规则参数。

    `applied` 来自 `load_config` 的同一套合并（不另算一遍）——档位读侧与判定侧
    必须是同一个数，否则面板会显示一个"看起来生效"的档位。
    """
    from backend.shared.risk.tiers import (
        PENDING_KEYS,
        TARGETS,
        TIER_DETAIL_KEY,
        TIER_KEY,
        tier_stale_reason,
    )

    try:
        client = _client(redis)
        raw = client.hgetall(TIER_KEY) or {}
        cfg = risk.load_config(redis)
        today = datetime.now(tz=CST).date()

        def _loads(value: Any, fallback: Any) -> Any:
            try:
                return json.loads(value) if value else fallback
            except (TypeError, ValueError):
                return fallback

        history = []
        for back in range(5):
            d = (today - timedelta(days=back)).strftime("%Y%m%d")
            try:
                row = client.hgetall(TIER_DETAIL_KEY.format(date=d)) or {}
            except Exception:  # noqa: BLE001 - 历史明细读失败不影响当前档位展示
                row = {}
            if row:
                history.append(
                    {
                        "date": d,
                        "level": row.get("level", ""),
                        "label": row.get("label", ""),
                        "source": row.get("source", ""),
                        "reasons": _loads(row.get("reasons"), []),
                    }
                )
        return {
            "success": True,
            "data": {
                "present": bool(raw),
                "date": raw.get("date", ""),
                "level": raw.get("level", ""),
                "label": raw.get("label", ""),
                "source": raw.get("source", ""),
                "budget": _loads(raw.get("budget"), {}),
                "reasons": _loads(raw.get("reasons"), []),
                "inputs": _loads(raw.get("inputs"), {}),
                "updated_at": raw.get("updated_at", ""),
                "stale_reason": tier_stale_reason(raw or None, today=today),
                # 生效面：闸门这一侧实际合并后的结果（含 source=absent 时的"未生效"）
                "effective_level": getattr(cfg, "tier_level", "") if cfg else "",
                "effective_source": getattr(cfg, "tier_source", "") if cfg else "unconfigured",
                "applied": getattr(cfg, "tier_applied", {}) if cfg else {},
                "history": history,
                "pending_keys": sorted(PENDING_KEYS),
                "targets": {k: list(v) for k, v in TARGETS.items()},
                "caliber": (
                    "档位只收紧不放宽（与配置取更严者）；source=absent 表示从未定档、"
                    "不覆盖任何参数；stale 表示文档过期，买入侧已回退防守参数"
                ),
            },
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"档位不可读: {exc}") from exc


class RiskConfigUpdate(BaseModel):
    enabled: bool | None = None
    shadow: bool | None = None
    rules: dict[str, dict[str, Any]] | None = None


@router.post("/risk/config")
async def risk_config_update(
    payload: RiskConfigUpdate,
    redis: Any = Depends(get_redis),
    auth: AuthContext = Depends(require_admin),
) -> dict[str, Any]:
    """改风控配置（version 自增；rules 只允许已注册规则 id）。"""
    from backend.shared.risk import get_rule

    updates: dict[str, str] = {}
    if payload.enabled is not None:
        updates["enabled"] = "true" if payload.enabled else "false"
    if payload.shadow is not None:
        updates["shadow"] = "true" if payload.shadow else "false"
    if payload.rules is not None:
        unknown = [rid for rid in payload.rules if get_rule(rid) is None]
        if unknown:
            raise HTTPException(status_code=400, detail=f"未注册规则 id: {unknown[:6]}")
        updates["rules"] = json.dumps(payload.rules, ensure_ascii=False)
    if not updates:
        raise HTTPException(status_code=400, detail="无更新字段")

    try:
        client = _client(redis)
        current = client.hgetall(risk.CONFIG_KEY) or {}
        try:
            version = int(current.get("version") or 0) + 1
        except (TypeError, ValueError):
            version = 1
        updates["version"] = str(version)
        # 首次配置：缺省填齐 enabled/shadow/rules（enabled=true, shadow=true 起步）
        if not current:
            updates.setdefault("enabled", "true")
            updates.setdefault("shadow", "true")
            updates.setdefault("rules", json.dumps(risk.DEFAULT_RULES, ensure_ascii=False))
        client.hset(risk.CONFIG_KEY, mapping=updates)
        logger.warning(
            "[RiskCtl] 配置变更 by=%s version=%d updates=%s",
            getattr(auth, "username", ""), version, sorted(updates),
        )
        return {"success": True, "data": {"version": version, "updates": sorted(updates)}}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Redis 不可写: {exc}") from exc


class CancelAllRequest(BaseModel):
    user_id: int = Field(..., gt=0)
    tenant_id: str = "default"
    reason: str = "risk_halt"


@router.post("/risk/cancel-all")
async def risk_cancel_all(
    payload: CancelAllRequest,
    db: Any = Depends(get_db),
    redis: Any = Depends(get_redis),
    auth: AuthContext = Depends(require_admin),
) -> dict[str, Any]:
    """HALT 全撤：撤销指定账户全部未成交模拟单（实盘通道属 T-RC-03）。"""
    from backend.services.simulation.services.order_router import cancel_all

    started = time.time()
    result = await cancel_all(
        db, redis,
        tenant_id=str(payload.tenant_id or "default"),
        user_id=int(payload.user_id),
        reason=str(payload.reason or "risk_halt")[:80],
    )
    logger.warning(
        "[RiskCtl] cancel-all by=%s user=%s → cancelled=%d failed=%d (%.2fs)",
        getattr(auth, "username", ""), payload.user_id,
        result["cancelled"], result["failed"], time.time() - started,
    )
    return {"success": True, "data": result}
