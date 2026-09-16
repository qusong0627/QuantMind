"""评估读 API（FE-E 数据出口）：`eval_scores` 表的唯一对外读取面。

用途（前端评估中心/评分卡/体检档案，T-FE-14/15/16）：
- `GET /api/v1/eval/scores`        最新快照列表（评分卡网格；可选 latest_only 取每对象最新一条）
- `GET /api/v1/eval/scores/history` 单对象历史序列（评分卡历史曲线）
- `GET /api/v1/eval/health/{strategy_id}` 策略体检档案（最新 + 历史 + **晋级门禁预演**）

纪律：
- 只读；前端禁止直连表（契约收口在本路由）；
- 可见性：`tenant_id = 当前租户 AND (user_id = 当前用户 OR user_id = '')`——因子/模型等
  全租户共享行（写侧 user_id 为空）对所有用户可见，用户私有行（策略/账户/选股）仅本人可见；
- 行数据 → 前端契约的转换纯函数化（可单测），SQL 与转换分层。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.services.api.user_app.middleware.auth import get_current_user
from backend.shared.database_manager_v2 import get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/eval", tags=["Eval"])

# 允许的评分卡类型（与写入侧 object_type 唯一一致；strategy_health 为体检留档）
ALLOWED_OBJECT_TYPES = (
    "factor",
    "model",
    "strategy",
    "account",
    "daily_selection",
    "strategy_health",
)


def _row_to_score(row: Any) -> dict[str, Any]:
    """eval_scores 行 → 前端评分卡契约（纯函数）。"""
    return {
        "object_type": row["object_type"],
        "object_id": row["object_id"],
        "snapshot_date": row["snapshot_date"].isoformat() if row["snapshot_date"] else None,
        "score": row["score"],
        "grade": row["grade"],
        "low_confidence": bool(row["low_confidence"]),
        "red_line_failed": list(row["red_line_failed"] or []),
        "dimensions": row["dimensions"] or {},
        "inputs_version": row["inputs_version"] or {},
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


def build_gate_preview(health: dict[str, Any] | None, *, mode: str = "SIMULATION") -> dict[str, Any]:
    """晋级门禁预演（纯函数）：与执行点 `promotion_gate` 同源——前端展示即真实口径。"""
    from backend.shared.backtest_health import promotion_gate

    allowed, note = promotion_gate(health, mode=mode)
    return {"mode": mode, "allowed": allowed, "note": note}


def _validate_object_type(object_type: str) -> str:
    ot = str(object_type or "").strip()
    if ot not in ALLOWED_OBJECT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"object_type 非法: {object_type}（允许: {', '.join(ALLOWED_OBJECT_TYPES)}）",
        )
    return ot


@router.get("/scores")
async def list_scores(
    object_type: str = Query(..., description="评分卡类型"),
    object_id: str | None = Query(None, description="对象 ID（可选）"),
    latest_only: bool = Query(True, description="每个对象只取最新一条（网格视图）"),
    limit: int = Query(50, ge=1, le=500),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """评分卡最新快照列表（最新优先）。"""
    from sqlalchemy import text as _text

    ot = _validate_object_type(object_type)
    tenant_id = str(current_user.get("tenant_id") or "default")
    user_id = str(current_user.get("user_id") or "")

    params: dict[str, Any] = {"t": tenant_id, "u": user_id, "n": limit}
    where = "object_type = :ot AND tenant_id = :t AND (user_id = :u OR user_id = '')"
    params["ot"] = ot
    if object_id:
        where += " AND object_id = :oid"
        params["oid"] = str(object_id)

    if latest_only:
        sql = (
            "SELECT DISTINCT ON (object_id) * FROM eval_scores "
            f"WHERE {where} "
            "ORDER BY object_id, snapshot_date DESC, created_at DESC"
        )
        sql = f"SELECT * FROM ({sql}) latest ORDER BY score DESC NULLS LAST LIMIT :n"
    else:
        sql = (
            f"SELECT * FROM eval_scores WHERE {where} "
            "ORDER BY snapshot_date DESC, created_at DESC LIMIT :n"
        )

    async with get_session(read_only=True) as session:
        rows = (await session.execute(_text(sql), params)).mappings().all()
    data = [_row_to_score(r) for r in rows]
    return {"success": True, "data": data, "meta": {"count": len(data), "object_type": ot}}


@router.get("/scores/history")
async def score_history(
    object_type: str = Query(...),
    object_id: str = Query(...),
    limit: int = Query(180, ge=2, le=2000),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """单对象评分历史（按日期升序，供历史曲线）。"""
    from sqlalchemy import text as _text

    ot = _validate_object_type(object_type)
    tenant_id = str(current_user.get("tenant_id") or "default")
    user_id = str(current_user.get("user_id") or "")

    async with get_session(read_only=True) as session:
        rows = (
            (
                await session.execute(
                    _text(
                        "SELECT * FROM eval_scores WHERE object_type = :ot AND object_id = :oid "
                        "AND tenant_id = :t AND (user_id = :u OR user_id = '') "
                        "ORDER BY snapshot_date DESC, created_at DESC LIMIT :n"
                    ),
                    {
                        "ot": ot,
                        "oid": str(object_id),
                        "t": tenant_id,
                        "u": user_id,
                        "n": limit,
                    },
                )
            )
            .mappings()
            .all()
        )
    data = [_row_to_score(r) for r in reversed(rows)]  # 升序返回
    return {
        "success": True,
        "data": data,
        "meta": {"count": len(data), "object_type": ot, "object_id": object_id},
    }


@router.get("/health/{strategy_id}")
async def strategy_health(
    strategy_id: str,
    limit: int = Query(24, ge=1, le=240),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """策略体检档案：最新报告 + 历史结论 + 晋级门禁预演（与执行点同源）。"""
    from sqlalchemy import text as _text

    sid = str(strategy_id or "").strip()
    if not sid.isdigit():
        raise HTTPException(status_code=400, detail="strategy_id 须为数字")
    tenant_id = str(current_user.get("tenant_id") or "default")
    user_id = str(current_user.get("user_id") or "")

    async with get_session(read_only=True) as session:
        rows = (
            (
                await session.execute(
                    _text(
                        "SELECT * FROM eval_scores WHERE object_type='strategy_health' "
                        "AND object_id=:sid AND tenant_id=:t AND (user_id=:u OR user_id='') "
                        "ORDER BY snapshot_date DESC, created_at DESC LIMIT :n"
                    ),
                    {"sid": sid, "t": tenant_id, "u": user_id, "n": limit},
                )
            )
            .mappings()
            .all()
        )
    history = [_row_to_score(r) for r in rows]
    latest = history[0] if history else None
    latest_health = None
    if latest:
        dims = latest["dimensions"] or {}
        latest_health = {
            "verdict": latest["grade"],
            "verdict_label": dims.get("verdict_label"),
            "confidence": latest["score"],
            "reasons": dims.get("reasons") or [],
            "suggestions": dims.get("suggestions") or [],
            "items": dims.get("items") or {},
            "backtest_id": (latest["inputs_version"] or {}).get("backtest_id"),
            "evidence_source": (latest["inputs_version"] or {}).get("evidence_source"),
            "snapshot_date": latest["snapshot_date"],
        }
    return {
        "success": True,
        "data": {
            "strategy_id": sid,
            "latest": latest_health,
            "history": [
                {
                    "snapshot_date": h["snapshot_date"],
                    "verdict": h["grade"],
                    "confidence": h["score"],
                    "evidence_source": (h["inputs_version"] or {}).get("evidence_source"),
                }
                for h in history
            ],
            "gate": build_gate_preview(latest_health, mode="SIMULATION"),
        },
        "meta": {"count": len(history)},
    }


@router.get("/object-types")
async def object_types() -> dict[str, Any]:
    """支持的评分卡类型（前端页签枚举的唯一来源）。"""
    labels = {
        "factor": "因子评分卡",
        "model": "模型评分卡",
        "strategy": "策略评分卡",
        "account": "账户评分卡",
        "daily_selection": "每日选股",
        "strategy_health": "体检留档",
    }
    return {
        "success": True,
        "data": [{"object_type": ot, "label": labels[ot]} for ot in ALLOWED_OBJECT_TYPES],
        "meta": {"count": len(ALLOWED_OBJECT_TYPES)},
    }
