"""Durable private pool orthogonality from frozen, owner-scoped panel versions."""

from __future__ import annotations

import asyncio
import hashlib
import math
import threading
import time
from pathlib import Path

from sqlalchemy import text

from backend.shared.database_manager_v2 import get_session
from backend.shared.owner_scope import OwnerScope
from backend.shared.resource_lease import LeaseUnavailable

CONTRACT = "pool_orthogonality_diversity_v2"
MAX_MEMBERS = 100
MAX_TOTAL_ROWS = 2_000_000
TIME_LIMIT_SECONDS = 900
CRITERIA = {
    "max_parents": 5,
    "min_stocks_per_day": 30,
    "min_sample_days": 20,
    "max_parent_abs_corr": 0.3,
}


class PoolResearchConflict(ValueError):
    pass


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def numeric_context():
    import numpy as np
    import pandas as pd

    directory = Path(__file__).parent
    hashes = {
        name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
        for name in (
            "pool_research.py",
            "factor_pool_orthogonality.py",
            "factor_pool_metrics.py",
            "factor_pool_edges.py",
            "factor_pool_diversity.py",
            "factor_pool_panels.py",
        )
    }
    return {"code_sha256": hashes, "numpy": np.__version__, "pandas": pd.__version__}


def _members_statement(*, lock=False, all_universes=False):
    return text(
        "SELECT p.universe,p.factor_id,p.panel_version,f.factor_name,p.extra->'panel_source' AS panel_source,"
        "(panel_job.status='completed') AS panel_source_completed,"
        "f.metadata_json->>'icir' AS icir,"
        "(SELECT max(r.revision_no) FROM rd_agent_factor_revisions r "
        "WHERE r.tenant_id=f.tenant_id AND r.user_id=f.user_id "
        "AND r.factor_id=f.factor_id) AS revision_no "
        "FROM rd_agent_factor_pool p JOIN rd_agent_factors f "
        "ON f.tenant_id=p.tenant_id AND f.user_id=p.user_id AND f.factor_id=p.factor_id "
        "LEFT JOIN alpha_factor_jobs panel_job ON panel_job.tenant_id=p.tenant_id "
        "AND panel_job.user_id=p.user_id AND panel_job.kind='backtest' "
        "AND panel_job.job_id=p.extra->'panel_source'->>'job_id' "
        "AND panel_job.execution_generation::text=p.extra->'panel_source'->>'execution_generation' "
        "AND panel_job.request_snapshot->'request'->>'universe'=p.universe "
        "AND ((panel_job.request_snapshot->'request'->>'factor_id'=p.factor_id "
        "AND panel_job.request_snapshot->'request'->>'factor_revision_no'=p.extra->'panel_source'->>'factor_revision_no') "
        "OR panel_job.request_snapshot->'request'->'factor_items' @> "
        "jsonb_build_array(jsonb_build_object('factor_id',p.factor_id,'factor_revision_no',p.extra->'panel_source'->'factor_revision_no'))) "
        "WHERE p.tenant_id=:tenant_id AND p.user_id=:user_id "
        "AND f.tenant_id=:tenant_id AND f.user_id=:user_id "
        "AND p.market='a_share' "
        + ("" if all_universes else "AND p.universe=:universe ")
        + "AND p.archived_at IS NULL AND f.archived_at IS NULL AND f.status='completed' "
        "ORDER BY p.universe,p.factor_id "
        + ("LIMIT 5001" if all_universes else "LIMIT 101")
        + (" FOR SHARE OF p" if lock else "")
    )


def _members(rows):
    if not rows:
        raise ValueError("本人股票池内没有可评估的已完成因子")
    if len(rows) > MAX_MEMBERS:
        raise ValueError("池正交评估最多支持100个完成成员；不会截断成员或父本候选")
    output = []
    for row in rows:
        revision = row["revision_no"]
        version = row["panel_version"]
        source = row["panel_source"]
        source_revision = (
            source.get("factor_revision_no")
            if isinstance(source, dict)
            and source.get("panel_version") == version
            and row["panel_source_completed"] is True
            else None
        )
        if type(source_revision) is not int or source_revision < 1:
            source_revision = None
        if source_revision is not None and (
            not isinstance(source.get("job_id"), str)
            or not source["job_id"]
            or type(source.get("execution_generation")) is not int
            or source["execution_generation"] < 1
        ):
            source_revision = None
        if type(revision) is not int or revision < 1:
            raise PoolResearchConflict("因子代码版本不可用，请等待版本迁移完成")
        if version is not None and (
            not isinstance(version, str)
            or len(version) != 64
            or any(c not in "0123456789abcdef" for c in version)
        ):
            raise PoolResearchConflict("因子面板版本无效，请重新物化本人因子")
        output.append(
            {
                "factor_id": row["factor_id"],
                "factor_name": row["factor_name"] or row["factor_id"],
                "factor_revision_no": revision,
                "panel_version": version,
                "panel_source_revision_no": source_revision,
                "panel_source_job_id": source["job_id"]
                if source_revision is not None
                else None,
                "panel_source_generation": source["execution_generation"]
                if source_revision is not None
                else None,
                "icir": number(row["icir"]),
            }
        )
    return output


async def freeze(owner: OwnerScope, universe: str):
    from .factor_pool_diversity import CRITERIA as DIVERSITY_CRITERIA

    async with get_session() as session:
        await session.execute(text("SET LOCAL statement_timeout='5s'"))
        rows = (
            (
                await session.execute(
                    _members_statement(), {**owner.sql_params(), "universe": universe}
                )
            )
            .mappings()
            .all()
        )
    items = _members(rows)
    return {
        "contract": CONTRACT,
        "market": "a_share",
        "universe": universe,
        "submission": {"universe": universe},
        "factor_ids": [item["factor_id"] for item in items],
        "factor_items": items,
        "criteria": dict(CRITERIA),
        "diversity_criteria": dict(DIVERSITY_CRITERIA),
        "numeric_context": await asyncio.to_thread(numeric_context),
        "max_total_rows": MAX_TOTAL_ROWS,
    }


def verify_admission(session, owner, request):
    current = _members(
        session.execute(
            _members_statement(lock=True),
            {**owner.sql_params(), "universe": request["universe"]},
        )
        .mappings()
        .all()
    )
    if current != request["factor_items"]:
        raise PoolResearchConflict("本人池成员、指标、面板或代码版本已变化，请重新提交")


def calculate(request, owner, stop_event, progress):
    import numpy as np
    import pandas as pd

    from backend.services.engine.alpha_agent.factor_pool_orthogonality import (
        OrthogonalCriteria,
        build_traces,
    )
    from backend.services.engine.alpha_agent.factor_pool_panels import read_panel
    from .factor_pool_diversity import (
        CRITERIA as DIVERSITY_CRITERIA,
        calculate_diversity,
    )

    if request.get("contract") != CONTRACT or request.get("market") != "a_share":
        raise ValueError("池研究协议已变化，请重新提交")
    if (
        request.get("criteria") != CRITERIA
        or request.get("diversity_criteria") != DIVERSITY_CRITERIA
        or request.get("max_total_rows") != MAX_TOTAL_ROWS
    ):
        raise ValueError("池研究计算参数不匹配")
    if request.get("numeric_context") != numeric_context():
        raise ValueError(
            "池研究数值实现或依赖已变化，请重新提交，旧任务不会按新算法重算"
        )
    items = request.get("factor_items")
    if (
        not isinstance(items, list)
        or not 1 <= len(items) <= MAX_MEMBERS
        or any(not isinstance(item, dict) for item in items)
        or [item.get("factor_id") for item in items] != request.get("factor_ids")
        or len(set(request["factor_ids"])) != len(items)
    ):
        raise ValueError("池研究成员快照无效")
    deadline = time.monotonic() + TIME_LIMIT_SECONDS

    def checkpoint():
        if time.monotonic() >= deadline:
            raise TimeoutError("池正交评估已超过900秒计算期限")
        return stop_event.is_set()

    frames, windows, unavailable, unverified = {}, {}, set(), set()
    total_rows = 0
    for index, item in enumerate(items):
        if checkpoint():
            from backend.services.engine.alpha_agent.factor_pool_orthogonality import (
                OrthogonalityCancelled,
            )

            raise OrthogonalityCancelled("池研究已停止")
        factor_id, version = item["factor_id"], item.get("panel_version")
        if (
            version is not None
            and item.get("panel_source_revision_no") != item["factor_revision_no"]
        ):
            unverified.add(factor_id)
            version = None
        frame = (
            None
            if version is None
            else read_panel(
                owner.tenant_id,
                owner.user_id,
                "a_share",
                factor_id,
                expected_version=version,
            )
        )
        if frame is None or frame.empty:
            if version is not None:
                unavailable.add(factor_id)
        else:
            total_rows += len(frame)
            if total_rows > MAX_TOTAL_ROWS:
                raise ValueError(
                    "完整私有面板合计超过200万行；未抽掉成员、行或父本计算近似结果"
                )
            if frame.duplicated(["trade_date", "symbol"]).any():
                raise ValueError("本人面板包含重复日期/股票，不能计算正交证据")
            frame = frame.copy().rename(columns={"forward_return": "fret"})
            for column in ("zscore", "fret"):
                if column in frame:
                    frame[column] = pd.to_numeric(
                        frame[column], errors="coerce"
                    ).replace([np.inf, -np.inf], np.nan)
            frames[factor_id] = frame
            windows[factor_id] = {
                "start_date": str(frame.trade_date.min()),
                "end_date": str(frame.trade_date.max()),
                "n_rows": len(frame),
                "n_days": int(frame.trade_date.nunique()),
                "has_forward_returns": "fret" in frame
                and bool(frame.fret.notna().any()),
            }
        progress(
            {
                "stage": "load_pool_panels",
                "factor_count": len(items),
                "factor_index": index + 1,
                "progress_pct": int(30 * (index + 1) / len(items)),
            }
        )
    progress(
        {"stage": "pool_orthogonality", "factor_count": len(items), "progress_pct": 30}
    )
    traces, stats = build_traces(
        items, frames, criteria=OrthogonalCriteria(**CRITERIA), should_cancel=checkpoint
    )
    progress(
        {"stage": "pool_diversity", "factor_count": len(items), "progress_pct": 85}
    )
    diversity = calculate_diversity(
        request["factor_ids"], frames, should_cancel=checkpoint
    )
    by_id = {item["factor_id"]: item for item in items}
    results = []
    for item in items:
        factor_id = item["factor_id"]
        trace = traces[factor_id]
        for parent in trace.get("parents", []):
            parent["factor_revision_no"] = by_id[parent["factor_id"]][
                "factor_revision_no"
            ]
            parent["panel_version"] = by_id[parent["factor_id"]]["panel_version"]
        if factor_id in unavailable:
            trace["note"] = "frozen_panel_unavailable"
        if factor_id in unverified:
            trace["note"] = "panel_source_unverified"
        results.append(
            {**item, "orthogonality": trace, "panel_window": windows.get(factor_id)}
        )
    progress(
        {
            "stage": "pool_evidence_ready",
            "factor_count": len(items),
            "progress_pct": 100,
        }
    )
    return {
        "contract": CONTRACT,
        "market": "a_share",
        "universe": request["universe"],
        "factor_count": len(items),
        "factor_ids": request["factor_ids"],
        "results": results,
        "criteria": dict(CRITERIA),
        "stats": stats,
        "panel_rows": total_rows,
        "numeric_context": request["numeric_context"],
        "return_basis": "saved_panel.forward_return",
        "diversity": diversity,
    }


async def execute_pool_research(
    request,
    *,
    owner,
    job_id,
    generation,
    jobs,
    budget,
    lease,
    lifecycle,
    thread_progress,
):
    from backend.services.engine.alpha_agent.factor_pool_orthogonality import (
        OrthogonalityCancelled,
    )
    from backend.services.engine.alpha_agent.factor_job_budget import POOL
    from backend.services.engine.strategy_lab.runner.container_runner import (
        SandboxExecutionCancelled,
    )

    stop_event = threading.Event()
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(
        None, calculate, request, owner, stop_event, thread_progress
    )
    lifecycle.update(create_started=True, confirmed_stopped=False)
    ticks = 0
    try:
        while not future.done():
            await asyncio.wait((future,), timeout=1)
            if future.done():
                break
            ticks += 1
            latest = await jobs.get(
                job_id,
                tenant_id=owner.tenant_id,
                user_id=owner.user_id,
                consistent=True,
            )
            if (
                latest is None
                or latest["status"] != "running"
                or latest["execution_generation"] != generation
            ):
                stop_event.set()
            elif ticks % 5 == 0:
                if not await asyncio.to_thread(budget.projection.heartbeat, lease):
                    raise LeaseUnavailable("池研究资源投影不可用")
                if not await asyncio.to_thread(
                    budget.store.transition, POOL, lease, "running"
                ):
                    stop_event.set()
        try:
            return future.result()
        except OrthogonalityCancelled as exc:
            raise SandboxExecutionCancelled(str(exc)) from exc
    finally:
        # run_in_executor's Future is deliberately never cancelled. A cancelled
        # asyncio wrapper alone is not proof that its numerical thread stopped.
        stop_event.set()
        while not future.done():
            try:
                await asyncio.shield(future)
            except BaseException:
                if future.done():
                    break
        if future.done() and not future.cancelled():
            future.exception()
            lifecycle["confirmed_stopped"] = True
