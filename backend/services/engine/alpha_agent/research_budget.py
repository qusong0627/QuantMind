"""Independent leases for short research LLM calls, never heavy compute slots."""

from __future__ import annotations

import argparse
import os
from functools import cache

from backend.services.engine.alpha_agent.factor_job_budget import build_execution_budget
from backend.shared.resource_lease import LeaseUnavailable

POOL = "alpha_research_llm"


@cache
def _build(pid: int):
    del pid
    return build_execution_budget(
        POOL,
        per_user=int(os.getenv("RESEARCH_LLM_PER_USER", "1")),
        capacity=int(os.getenv("RESEARCH_LLM_GLOBAL", "2")),
    )


def get_budget():
    return _build(os.getpid())


def require_ready_budget():
    budget = get_budget()
    try:
        if not budget.projection.client.get(budget.projection.ready_key):
            raise LeaseUnavailable("研究请求预算尚未对账")
    except Exception as exc:
        if isinstance(exc, LeaseUnavailable):
            raise
        raise LeaseUnavailable("研究请求预算暂时不可用") from exc
    return budget


def daily_limits():
    limits = (
        int(os.getenv("RESEARCH_LLM_CALLS_PER_USER_DAY", "100")),
        int(os.getenv("RESEARCH_LLM_CALLS_GLOBAL_DAY", "1000")),
    )
    if min(limits) < 1:
        raise ValueError("Research model-call limits must be positive")
    return limits


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Maintain short research LLM leases")
    parser.add_argument("action", choices=("initialize", "recover"))
    parser.add_argument("--legacy-drained", action="store_true")
    args = parser.parse_args()
    if args.action == "initialize":
        if not args.legacy_drained:
            parser.error(
                "initialize requires --legacy-drained after the old engine stopped"
            )
        get_budget().store.provision_pool(POOL, legacy_drained=True)
    from backend.services.engine.alpha_agent.research_requests import reconcile

    reconcile()
