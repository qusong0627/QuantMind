"""扫描编排（T-P4-01）：注册表开关 → 快照装载 → 单路 scan → 合并去重（纯函数）。

铁律：扫描结果只进"机会池"呈现层——**不直接下单**，买不买由策略与风控决定。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from backend.shared.scanner_spi import (
    SCANNERS,
    Opportunity,
    merge_opportunities,
    opportunity_to_dict,
    scanner_switch_enabled,
)
from backend.services.engine.inference.inference_backtest_service import StrategyConfig
from backend.services.engine.scanners.model_signal_loader import (
    load_model_signal_snapshot,
)
from backend.services.engine.scanners.model_signal_scanner import scan_model_signals

logger = logging.getLogger(__name__)

_SH_TZ = ZoneInfo("Asia/Shanghai")


async def _scan_model_signal(
    *,
    trade_date: str | None,
    strategy: str,
    tenant_id: str,
    user_id: str | None,
    ts: str,
) -> tuple[list[Opportunity], dict[str, Any]]:
    cfg = StrategyConfig.preset(strategy)
    snapshot = await load_model_signal_snapshot(
        trade_date, tenant_id=tenant_id, user_id=user_id, config=cfg
    )
    if snapshot is None:
        return [], {
            "scanner": "model_signal",
            "trade_date": trade_date,
            "picked": 0,
            "note": "无信号",
        }
    return scan_model_signals(snapshot, cfg, ts=ts)


_SCANNER_DISPATCH: dict[str, Callable] = {
    "model_signal": _scan_model_signal,
}


async def run_scan(
    *,
    trade_date: str | None = None,
    strategy: str = "balanced",
    tenant_id: str = "default",
    user_id: str | None = None,
    prior: list[Opportunity] | None = None,
    cooldown_days: int = 1,
    now: datetime | None = None,
) -> dict[str, Any]:
    """全路批扫描（盘后）→ 合并机会池（未持久化，v1 由调用方决定去处）。"""
    current = now or datetime.now(_SH_TZ)
    ts = current.isoformat(timespec="seconds")
    opportunities: list[Opportunity] = []
    metas: list[dict[str, Any]] = []
    skipped: list[str] = []
    for spec in SCANNERS:
        if not scanner_switch_enabled(spec):
            skipped.append(spec.id)
            continue
        dispatch = _SCANNER_DISPATCH.get(spec.id)
        if dispatch is None:
            logger.warning("[Scanner] 注册表中 %s 未接线 dispatch", spec.id)
            skipped.append(spec.id)
            continue
        opps, meta = await dispatch(
            trade_date=trade_date,
            strategy=strategy,
            tenant_id=tenant_id,
            user_id=user_id,
            ts=ts,
        )
        opportunities.extend(opps)
        metas.append(meta)
    merged = merge_opportunities(
        opportunities,
        prior=prior,
        cooldown_days=cooldown_days,
        as_of=ts,
    )
    return {
        "as_of": ts,
        "strategy": strategy,
        "scanners_run": [m.get("scanner") for m in metas],
        "scanners_skipped": skipped,
        "meta": metas,
        "opportunities": [opportunity_to_dict(o) for o in merged],
    }
