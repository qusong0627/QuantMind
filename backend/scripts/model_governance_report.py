#!/usr/bin/env python3
"""模型治理审计报表（P4 §八）：台账决策链 + 生命周期审计一处可查（只读）。

两个事实源：

- ``qm_model_rollouts``——晋升/回滚/拒绝的治理链（谁在何时基于何证据决策，理由必填）；
  活跃阶段（replay_eval/observing/gate_passed）进度另列；
- ``user_audit_logs``——生命周期动作（``model.archive`` 归档、``model.retire_purge``
  退役清退；见 ``backend/shared/model_retirement.py``）。

用法::

    # 近 90 天全量（默认）
    python backend/scripts/model_governance_report.py

    # 近 30 天、限租户、机器可读
    python backend/scripts/model_governance_report.py --days 30 --tenant default --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.shared.model_retirement import (  # noqa: E402
    AUDIT_ACTION_ARCHIVE,
    AUDIT_ACTION_PURGE,
)

_DECIDED_STAGES = ("promoted", "rolled_back", "rejected")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="模型治理审计报表（只读）")
    parser.add_argument("--days", type=int, default=90, help="回看天数（默认 90）")
    parser.add_argument("--tenant", default=None, help="仅该租户")
    parser.add_argument("--user", default=None, help="仅该用户")
    parser.add_argument("--limit", type=int, default=200, help="每段行数上限（默认 200）")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON")
    return parser


async def _collect(args: argparse.Namespace) -> dict:
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session
    from backend.shared.model_rollout_store import ACTIVE_STAGES

    since = datetime.now(timezone.utc) - timedelta(days=args.days)

    def _owner_filter(column_tenant: str, column_user: str) -> tuple[str, dict]:
        clauses, params = [], {}
        if args.tenant:
            clauses.append(f"{column_tenant} = :tenant")
            params["tenant"] = args.tenant
        if args.user:
            clauses.append(f"{column_user} = :user")
            params["user"] = args.user
        return (" AND " + " AND ".join(clauses) if clauses else ""), params

    owner_sql, owner_params = _owner_filter("tenant_id", "user_id")
    stages = sorted(ACTIVE_STAGES)
    ph = ", ".join(f":s{i}" for i in range(len(stages)))
    stage_params = {f"s{i}": s for i, s in enumerate(stages)}
    dph = ", ".join(f":d{i}" for i in range(len(_DECIDED_STAGES)))
    dparams = {f"d{i}": s for i, s in enumerate(_DECIDED_STAGES)}

    out: dict = {"days": args.days, "since": since.isoformat(), "active": [],
                 "decisions": [], "lifecycle": []}
    async with get_session(read_only=True) as session:
        out["active"] = [
            dict(r)
            for r in (
                await session.execute(
                    text(
                        "SELECT tenant_id, user_id, market, rollout_id, stage, "
                        "champion_model_id, challenger_model_id, created_at, updated_at "
                        f"FROM qm_model_rollouts WHERE stage IN ({ph}){owner_sql} "
                        "ORDER BY updated_at DESC LIMIT :lim"
                    ),
                    {**stage_params, **owner_params, "lim": args.limit},
                )
            )
            .mappings()
            .all()
        ]
        out["decisions"] = [
            dict(r)
            for r in (
                await session.execute(
                    text(
                        "SELECT tenant_id, user_id, market, rollout_id, stage, "
                        "champion_model_id, challenger_model_id, prior_default_model_id, "
                        "decided_by, decided_at, notes "
                        f"FROM qm_model_rollouts WHERE stage IN ({dph}) "
                        f"AND decided_at >= :since{owner_sql} "
                        "ORDER BY decided_at DESC LIMIT :lim"
                    ),
                    {**dparams, **owner_params, "since": since, "lim": args.limit},
                )
            )
            .mappings()
            .all()
        ]
        out["lifecycle"] = [
            dict(r)
            for r in (
                await session.execute(
                    text(
                        "SELECT created_at, tenant_id, user_id, action, resource_id, description "
                        "FROM user_audit_logs "
                        "WHERE action IN (:a_archive, :a_purge) AND created_at >= :since"
                        f"{owner_sql} ORDER BY created_at DESC LIMIT :lim"
                    ),
                    {
                        "a_archive": AUDIT_ACTION_ARCHIVE,
                        "a_purge": AUDIT_ACTION_PURGE,
                        **owner_params,
                        "since": since,
                        "lim": args.limit,
                    },
                )
            )
            .mappings()
            .all()
        ]
    return out


def _print_human(report: dict) -> None:
    print(f"模型治理审计报表 · 近 {report['days']} 天（since {report['since']}）")

    print(f"\n— 活跃 rollout（{len(report['active'])}）—")
    for r in report["active"]:
        print(
            f"  [{r['market']}] {r['stage']}: 挑战者 {r['challenger_model_id']} "
            f"vs 冠军 {r['champion_model_id']}（{r['tenant_id']}/{r['user_id']}，"
            f"建于 {r['created_at']}）"
        )

    print(f"\n— 决策记录（{len(report['decisions'])}）—")
    for r in report["decisions"]:
        prior = r.get("prior_default_model_id") or "—"
        note = f" · {r['notes']}" if r.get("notes") else ""
        print(
            f"  {r['decided_at']} [{r['market']}] {r['stage']}: {r['challenger_model_id']}"
            f"（前任 {prior}）by {r.get('decided_by') or '—'}{note}"
        )

    print(f"\n— 生命周期审计（{len(report['lifecycle'])}）—")
    for r in report["lifecycle"]:
        print(
            f"  {r['created_at']} [{r['tenant_id']}/{r['user_id']}] {r['action']} "
            f"{r['resource_id']}: {r['description']}"
        )

    if not (report["active"] or report["decisions"] or report["lifecycle"]):
        print("\n（区间内无记录）")


async def _run(args: argparse.Namespace) -> int:
    report = await _collect(args)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        _print_human(report)
    return 0


def main() -> int:
    args = _build_parser().parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
