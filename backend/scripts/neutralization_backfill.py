#!/usr/bin/env python3
"""中性化影子层历史回填 CLI。

用法：

    python -m backend.scripts.neutralization_backfill \
        --model-id mdl_cn_train_xxx --from 2025-01-02 --to 2026-10-23

    # 对全部已启用（mode != off）的模型回填
    python -m backend.scripts.neutralization_backfill --all-enabled --from ... --to ...

说明：

- 只写 ``qm_neutral_runs`` / ``qm_neutral_scores`` 影子表，不改原始分数；
- 每天取该模型当天最近一次 completed 推理 run；
- 缺口（无 run / 控制变量分区缺失）逐日记录并继续，最后统一汇总。
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime

from sqlalchemy import text

from backend.services.engine.inference import neutralization_service as svc

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("neutralization_backfill")


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _runs_by_date(
    session,
    *,
    tenant_id: str,
    user_id: str,
    model_id: str,
    start: date,
    end: date,
) -> list[dict]:
    rows = (
        session.execute(
            text(
                """
            SELECT DISTINCT ON (data_trade_date)
                   run_id, data_trade_date
            FROM qm_model_inference_runs
            WHERE tenant_id=:t AND user_id=:u AND model_id=:m
              AND status='completed'
              AND data_trade_date BETWEEN :start AND :end
            ORDER BY data_trade_date, updated_at DESC NULLS LAST
            """
            ),
            {"t": tenant_id, "u": user_id, "m": model_id, "start": start, "end": end},
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


def _backfill_model(
    *,
    tenant_id: str,
    user_id: str,
    model_id: str,
    start: date,
    end: date,
) -> dict:
    with svc.get_session_factory()() as session:
        runs = _runs_by_date(
            session,
            tenant_id=tenant_id,
            user_id=user_id,
            model_id=model_id,
            start=start,
            end=end,
        )
    stats = {"ok": 0, "degraded": 0, "failed": 0, "skipped": 0}
    failures: list[tuple[str, str]] = []
    for row in runs:
        run_id = row["run_id"]
        try:
            outcome = svc.compute_for_run(
                run_id,
                tenant_id=tenant_id,
                user_id=user_id,
                model_id=model_id,
                data_trade_date=row["data_trade_date"],
                force=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s %s 回填失败: %s", model_id, row["data_trade_date"], exc)
            stats["failed"] += 1
            failures.append((str(row["data_trade_date"]), str(exc)[:200]))
            continue
        if outcome is None:
            stats["skipped"] += 1
            continue
        stats[outcome.status if outcome.status in stats else "failed"] += 1
        if outcome.status != "ok":
            failures.append((str(row["data_trade_date"]), outcome.reason or ""))
    return {"model_id": model_id, "runs": len(runs), **stats, "failures": failures}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="中性化影子层历史回填")
    parser.add_argument("--model-id", action="append", default=[])
    parser.add_argument("--all-enabled", action="store_true")
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument("--user-id", default="10000001")
    parser.add_argument("--from", dest="start", required=True, type=_parse_date)
    parser.add_argument("--to", dest="end", required=True, type=_parse_date)
    args = parser.parse_args(argv)

    if not args.model_id and not args.all_enabled:
        parser.error("必须指定 --model-id 或 --all-enabled")

    targets: list[tuple[str, str, str]] = []
    if args.all_enabled:
        with svc.get_session_factory()() as session:
            for row in svc.enabled_models(session, tenant_id=args.tenant_id):
                targets.append((row["tenant_id"], row["user_id"], row["model_id"]))
    for model_id in args.model_id:
        targets.append((args.tenant_id, args.user_id, model_id))

    if not targets:
        logger.warning("没有可回填的模型")
        return 1

    summary = []
    for tenant_id, user_id, model_id in targets:
        logger.info(
            "回填 %s (%s/%s) %s ~ %s",
            model_id,
            tenant_id,
            user_id,
            args.start,
            args.end,
        )
        summary.append(
            _backfill_model(
                tenant_id=tenant_id,
                user_id=user_id,
                model_id=model_id,
                start=args.start,
                end=args.end,
            )
        )

    for item in summary:
        logger.info(
            "%s: runs=%s ok=%s degraded=%s failed=%s skipped=%s",
            item["model_id"],
            item["runs"],
            item["ok"],
            item["degraded"],
            item["failed"],
            item["skipped"],
        )
        for day, reason in item["failures"][:5]:
            logger.info("  %s %s", day, reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
