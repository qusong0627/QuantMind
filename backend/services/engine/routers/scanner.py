"""机会扫描 API（T-P4-02）：新入口 `/api/v1/scanner/daily`。

Scanner SPI（T-P4-01）的服务化：阈值分位化（T-P4-03）默认口径，
机会发现与交易决策分离——扫描结果只呈现证据链，买不买由策略与风控决定。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Query, Request

from backend.services.engine.auth_context import get_authenticated_identity

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/scanner", tags=["Scanner"])


@router.get("/daily")
async def daily_scan(
    request: Request,
    strategy: str = Query(
        "balanced", description="策略预设 conservative|balanced|aggressive"
    ),
    date: str | None = Query(None, description="信号交易日 YYYY-MM-DD，缺省取最新"),
    mode: str = Query("quantile", description="阈值口径 quantile(默认)|absolute"),
) -> dict[str, Any]:
    """全路扫描 → 合并机会池（含分位阈值与市场状态证据）。"""
    user_id, tenant_id = get_authenticated_identity(request)
    from backend.services.engine.scanners.runner import run_scan

    report = await run_scan(
        trade_date=date,
        strategy=strategy,
        tenant_id=tenant_id,
        user_id=user_id,
        mode=mode,
    )
    identity_fallback = False
    first_meta = (report.get("meta") or [{}])[0]
    if first_meta.get("note"):
        # 当前身份无信号 → 回落最新写入身份（如实标注；身份双形态/历史写入的稳健兜底）
        retry = await run_scan(
            trade_date=date,
            strategy=strategy,
            tenant_id=tenant_id,
            user_id=None,
            mode=mode,
        )
        retry_meta = (retry.get("meta") or [{}])[0]
        if not retry_meta.get("note"):
            report, first_meta, identity_fallback = retry, retry_meta, True

    return {
        "status": "success",
        "success": True,
        "as_of": report.get("as_of"),
        "mode": report.get("mode"),
        "strategy": report.get("strategy"),
        "trade_date": first_meta.get("trade_date"),
        "market_state": {
            "state": first_meta.get("market_state"),
            **(first_meta.get("entry_gate") or {}),
        },
        "thresholds": first_meta.get("thresholds"),
        "identity_fallback": identity_fallback,
        "scanners_run": report.get("scanners_run"),
        "scanners_skipped": report.get("scanners_skipped"),
        "opportunities": report.get("opportunities") or [],
        "note": first_meta.get("note", ""),
    }
