"""Owner-scoped monthly factor lifecycle evaluation orchestration."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from typing import Any

from sqlalchemy import text

from backend.shared.database_manager_v2 import get_session
from backend.shared.utc_datetime import utc_now
from backend.services.engine.alpha_agent.factor_jobs import AlphaFactorJobPersistence
from backend.services.engine.qlib_app.services.rd_agent_persistence import (
    RDAgentFactorPersistence,
)

logger = logging.getLogger(__name__)
PAGE_SIZE = 200


async def _factor_page(
    cursor: tuple[str, str, str] | None,
) -> list[dict[str, Any]]:
    conditions = [
        "tenant_id IS NOT NULL AND btrim(tenant_id) <> ''",
        "user_id IS NOT NULL AND btrim(user_id) <> ''",
        "archived_at IS NULL",
    ]
    params: dict[str, Any] = {"limit": PAGE_SIZE}
    if cursor is not None:
        conditions.append("(tenant_id,user_id,factor_id) > (:tenant,:user,:factor)")
        params.update(tenant=cursor[0], user=cursor[1], factor=cursor[2])
    async with get_session(read_only=True) as session:
        result = await session.execute(
            text(
                "SELECT factor_id,tenant_id,user_id,status,metadata_json,created_at,"
                "(SELECT MAX(revision.revision_no) FROM rd_agent_factor_revisions AS revision "
                "WHERE revision.tenant_id=factor.tenant_id "
                "AND revision.user_id=factor.user_id "
                "AND revision.factor_id=factor.factor_id) AS factor_revision_no "
                "FROM rd_agent_factors AS factor WHERE "
                + " AND ".join(conditions)
                + " ORDER BY tenant_id,user_id,factor_id LIMIT :limit"
            ),
            params,
        )
        return [dict(row) for row in result.mappings().all()]


def _decode_snapshot(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return None
    return value if isinstance(value, dict) else None


async def evaluate_monthly() -> dict[str, int]:
    """Evaluate only latest completed evidence, writing under its real owner."""
    factors = RDAgentFactorPersistence()
    jobs = AlphaFactorJobPersistence()
    now = utc_now()
    evaluated = skipped = failed = 0
    cursor: tuple[str, str, str] | None = None

    while True:
        page = await _factor_page(cursor)
        if not page:
            break
        cursor = (
            str(page[-1]["tenant_id"]),
            str(page[-1]["user_id"]),
            str(page[-1]["factor_id"]),
        )
        by_owner: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in page:
            by_owner[(str(row["tenant_id"]), str(row["user_id"]))].append(row)

        for (tenant_id, user_id), owner_factors in by_owner.items():
            factor_ids = [str(row["factor_id"]) for row in owner_factors]
            evidence = await jobs.latest_completed_backtest_evidence(
                factor_ids, tenant_id=tenant_id, user_id=user_id
            )
            first_completed = await jobs.first_completed_backtest_at(
                factor_ids, tenant_id=tenant_id, user_id=user_id
            )
            from backend.shared.factor_lifecycle import evaluate_lifecycle

            for row in owner_factors:
                factor_id = str(row["factor_id"])
                evidence_row = evidence.get(factor_id)
                if not evidence_row:
                    skipped += 1
                    continue
                snapshot = _decode_snapshot(evidence_row.get("result"))
                if snapshot is None:
                    skipped += 1
                    continue
                chart = snapshot.get("series")
                series = (
                    chart.get("lifecycle_ic_series")
                    if isinstance(chart, dict)
                    else None
                )
                result = evaluate_lifecycle(
                    row,
                    series=series if isinstance(series, dict) else None,
                    first_completed_run_at=first_completed.get(factor_id),
                    now=now,
                )
                latest = (result.get("verdict") or {}).get("latest") or {}
                month_end = latest.get("month_end")
                try:
                    await factors.record_factor_lifecycle_evaluation(
                        factor_id,
                        result,
                        evaluated_at=now,
                        factor_revision_no=(
                            snapshot.get("factor_revision_no")
                            if isinstance(snapshot.get("factor_revision_no"), int)
                            else None
                        ),
                        evidence_job_id=evidence_row.get("job_id") or None,
                        user_id=user_id,
                        tenant_id=tenant_id,
                        idempotency_month_end=str(month_end) if month_end else None,
                        apply_downweight=False,
                    )
                    evaluated += 1
                except Exception:  # noqa: BLE001
                    logger.exception("Monthly factor lifecycle evaluation failed")
                    failed += 1

    if failed:
        raise RuntimeError(f"Monthly lifecycle evaluation failed for {failed} factors")
    return {"evaluated": evaluated, "skipped": skipped, "failed": failed}
