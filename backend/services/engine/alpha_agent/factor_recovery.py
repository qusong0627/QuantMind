"""Durable, owner-fenced missing-source reconstruction and sandbox evaluation."""

from __future__ import annotations

import asyncio
import hashlib
import json

from sqlalchemy import text

from backend.shared.owner_scope import OwnerScope
from backend.shared.secret_redaction import redact_text
from backend.services.engine.alpha_agent.factor_codegen import (
    PROMPT_VERSION,
    generate_factor_code,
    input_fingerprint,
)
from backend.services.engine.alpha_agent.factor_job_budget import POOL


def _locked_factor(session, owner: OwnerScope, item: dict, *, revision=None):
    row = (
        session.execute(
            text(
                "SELECT factor_id,factor_name,factor_formulation,factor_code,metadata_json,"
                "(SELECT max(r.revision_no) FROM rd_agent_factor_revisions r "
                "WHERE r.tenant_id=rd_agent_factors.tenant_id AND r.user_id=rd_agent_factors.user_id "
                "AND r.factor_id=rd_agent_factors.factor_id) AS current_revision "
                "FROM rd_agent_factors WHERE tenant_id=:tenant_id AND user_id=:user_id "
                "AND factor_id=:factor AND archived_at IS NULL FOR UPDATE"
            ),
            {**owner.sql_params(), "factor": item["factor_id"]},
        )
        .mappings()
        .first()
    )
    expected = item.get("factor_revision_no") if revision is None else revision
    if (
        row is None
        or input_fingerprint(dict(row)) != item["input_fingerprint"]
        or row["current_revision"] != expected
    ):
        raise ValueError("本人因子公式或可见性已变化，请重新提交恢复")
    return row


async def execute_recovery(
    request: dict,
    *,
    owner: OwnerScope,
    job_id: str,
    generation: int,
    jobs,
    budget,
    lease,
    lifecycle: dict,
    thread_progress,
) -> dict:
    from backend.services.engine.alpha_agent.factor_job_executor import (
        execute_factor_backtest,
    )
    from backend.services.engine.alpha_agent.llm_client import resolve_llm_config
    from backend.services.engine.strategy_lab.runner.container_runner import (
        SandboxExecutionCancelled,
    )

    results = []
    failures = 0
    if request.get("prompt_version") != PROMPT_VERSION:
        raise ValueError("恢复提示词版本已变化，请重新提交")

    async def cancelled():
        current = await jobs.get(
            job_id, tenant_id=owner.tenant_id, user_id=owner.user_id, consistent=True
        )
        return (
            current is None
            or current["status"] != "running"
            or current["execution_generation"] != generation
        )

    async def progress(snapshot):
        await jobs.update_progress(
            job_id,
            tenant_id=owner.tenant_id,
            user_id=owner.user_id,
            generation=generation,
            progress={**snapshot, "results": results},
        )

    async def transaction(callback):
        def persist(session):
            active = session.execute(
                text(
                    "SELECT job_id FROM alpha_factor_jobs WHERE tenant_id=:tenant_id "
                    "AND user_id=:user_id AND job_id=:job AND execution_generation=:generation "
                    "AND status='running' FOR UPDATE"
                ),
                {**owner.sql_params(), "job": job_id, "generation": generation},
            ).first()
            if active is None:
                raise SandboxExecutionCancelled("恢复作业已停止或代次已变化")
            callback(session)

        changed = await asyncio.to_thread(
            budget.store.transition_transactional,
            POOL,
            lease,
            "running",
            expected_states=("reserved", "running", "suspect"),
            persist=persist,
        )
        if not changed:
            raise SandboxExecutionCancelled("恢复执行租约已变化")

    for item in request["factor_items"]:
        if await cancelled():
            raise SandboxExecutionCancelled("代码恢复已停止，已保存的代码版本保留")
        lifecycle.update(create_started=False, confirmed_stopped=False)
        code = item.get("factor_code") or ""
        revision = item.get("factor_revision_no")
        try:
            # Check the live owner row before making a billable model request.
            def check_source(session):
                row = _locked_factor(session, owner, item)
                if (row["factor_code"] or "").strip() != code.strip():
                    raise ValueError("因子代码已变化，不能覆盖新版本")
                session.execute(
                    text(
                        "INSERT INTO alpha_factor_recovery_sources "
                        "(tenant_id,user_id,job_id,factor_id,execution_generation,revision_no,"
                        "input_fingerprint,code_sha256,prompt_version,model,status,published_at) VALUES "
                        "(:tenant_id,:user_id,:job,:factor,:generation,:revision,:input,:sha,:prompt,:model,:status,"
                        "CASE WHEN :has_code THEN now() ELSE NULL END)"
                    ),
                    {
                        **owner.sql_params(),
                        "job": job_id,
                        "factor": item["factor_id"],
                        "generation": generation,
                        "revision": revision if code.strip() else None,
                        "input": item["input_fingerprint"],
                        "sha": hashlib.sha256(code.encode()).hexdigest()
                        if code.strip()
                        else None,
                        "prompt": PROMPT_VERSION,
                        "model": (request.get("model") or "")
                        if not code.strip()
                        else "",
                        "status": "code_saved" if code.strip() else "generating",
                        "has_code": bool(code.strip()),
                    },
                )

            await transaction(check_source)
            if not code.strip():
                config = resolve_llm_config()
                if config is None:
                    raise ValueError("系统 AI 服务尚未配置")
                if config.model != request.get("model"):
                    raise ValueError("系统模型已变化，请重新确认恢复请求")
                lifecycle["create_started"] = True
                try:
                    code = await generate_factor_code(
                        item["source_input"],
                        config=config,
                        is_cancelled=cancelled,
                        progress=progress,
                    )
                finally:
                    lifecycle["confirmed_stopped"] = True
                if await cancelled():
                    raise SandboxExecutionCancelled(
                        "代码生成结束后已收到停止请求，未发布代码"
                    )
                code_hash = hashlib.sha256(code.encode()).hexdigest()
                published = {}

                def publish(session):
                    row = _locked_factor(session, owner, item)
                    if (row["factor_code"] or "").strip():
                        raise ValueError("已有新代码，拒绝覆盖")
                    updated = session.execute(
                        text(
                            "UPDATE rd_agent_factors SET factor_code=:code,status='pending',updated_at=now(),"
                            "ic_value=NULL,rank_ic=NULL,sharpe_ratio=NULL,annual_return=NULL,max_drawdown=NULL,"
                            "metadata_json=(COALESCE(metadata_json,'{}'::jsonb)-'icir'-'rank_ic'-'rank_icir')||CAST(:provenance AS jsonb) "
                            "WHERE tenant_id=:tenant_id AND user_id=:user_id AND factor_id=:factor "
                            "AND archived_at IS NULL AND factor_code IS NOT DISTINCT FROM :baseline_code"
                        ),
                        {
                            **owner.sql_params(),
                            "factor": item["factor_id"],
                            "code": code,
                            "baseline_code": row["factor_code"],
                            "provenance": json.dumps(
                                {
                                    "code_recovered": "llm_reconstruction",
                                    "code_recovery_job_id": job_id,
                                }
                            ),
                        },
                    )
                    if updated.rowcount != 1:
                        raise ValueError("因子代码发布围栏未通过")
                    revision_no = session.execute(
                        text(
                            "SELECT max(revision_no) FROM rd_agent_factor_revisions "
                            "WHERE tenant_id=:tenant_id AND user_id=:user_id AND factor_id=:factor "
                            "AND factor_code=:code"
                        ),
                        {
                            **owner.sql_params(),
                            "factor": item["factor_id"],
                            "code": code,
                        },
                    ).scalar_one()
                    if type(revision_no) is not int:
                        raise ValueError("代码版本未建立，拒绝发布")
                    saved = session.execute(
                        text(
                            "UPDATE alpha_factor_recovery_sources SET revision_no=:revision,code_sha256=:sha,"
                            "status='code_saved',published_at=now() WHERE tenant_id=:tenant_id "
                            "AND user_id=:user_id AND job_id=:job AND factor_id=:factor "
                            "AND execution_generation=:generation AND status='generating'"
                        ),
                        {
                            **owner.sql_params(),
                            "job": job_id,
                            "factor": item["factor_id"],
                            "generation": generation,
                            "revision": revision_no,
                            "sha": code_hash,
                        },
                    )
                    if saved.rowcount != 1:
                        raise ValueError("代码恢复尝试围栏未通过")
                    published["revision"] = revision_no

                await transaction(publish)
                revision = published["revision"]
            code_hash = hashlib.sha256(code.encode()).hexdigest()
            evaluation_request = {
                **request["evaluation"],
                "factor_id": item["factor_id"],
                "factor_code": code,
                "factor_code_sha256": code_hash,
                "factor_revision_no": revision,
                "_series_max_points": 150,
            }
            # Reuse the fixed Docker runner under the existing owner lease.
            lifecycle.update(create_started=False, confirmed_stopped=False)
            operation = asyncio.create_task(
                asyncio.to_thread(
                    execute_factor_backtest,
                    evaluation_request,
                    tenant_id=owner.tenant_id,
                    user_id=owner.user_id,
                    lease=lease,
                    budget=budget,
                    lifecycle=lifecycle,
                    progress=thread_progress,
                )
            )
            while True:
                try:
                    evaluation = await asyncio.shield(operation)
                    break
                except asyncio.CancelledError:
                    if operation.cancelled():
                        raise RuntimeError("恢复评估回执丢失")
            lifecycle["confirmed_stopped"] = True
            if await cancelled():
                raise SandboxExecutionCancelled("评估结束后已停止，未更新当前因子指标")

            def save_metrics(session):
                row = _locked_factor(session, owner, item, revision=revision)
                if (row["factor_code"] or "") != code:
                    raise ValueError("评估期间代码变化，拒绝覆盖当前指标")
                metrics = evaluation["metrics"]
                changed = session.execute(
                    text(
                        "UPDATE rd_agent_factors SET status='completed',ic_value=:ic,rank_ic=:rank,"
                        "sharpe_ratio=:sharpe,annual_return=:annual,max_drawdown=:drawdown,updated_at=now(),"
                        "metadata_json=COALESCE(metadata_json,'{}'::jsonb)||CAST(:reading AS jsonb) "
                        "WHERE tenant_id=:tenant_id AND user_id=:user_id AND factor_id=:factor "
                        "AND archived_at IS NULL AND factor_code=:code"
                    ),
                    {
                        **owner.sql_params(),
                        "factor": item["factor_id"],
                        "code": code,
                        "ic": metrics.get("ic_mean"),
                        "rank": metrics.get("rank_ic_median"),
                        "sharpe": metrics.get("sharpe_ratio"),
                        "annual": metrics.get("annual_return"),
                        "drawdown": metrics.get("max_drawdown"),
                        "reading": json.dumps(
                            {
                                "icir": metrics.get("icir"),
                                "recovery_evidence": {
                                    "job_id": job_id,
                                    "revision_no": revision,
                                    "start_date": evaluation["start_date"],
                                    "end_date": evaluation["end_date"],
                                },
                            },
                            allow_nan=False,
                        ),
                    },
                )
                if changed.rowcount != 1:
                    raise ValueError("恢复指标更新围栏未通过")
                saved = session.execute(
                    text(
                        "UPDATE alpha_factor_recovery_sources SET status='completed',result_snapshot=CAST(:result AS jsonb) "
                        "WHERE tenant_id=:tenant_id AND user_id=:user_id AND job_id=:job AND factor_id=:factor "
                        "AND execution_generation=:generation AND revision_no=:revision AND status='code_saved'"
                    ),
                    {
                        **owner.sql_params(),
                        "job": job_id,
                        "factor": item["factor_id"],
                        "generation": generation,
                        "revision": revision,
                        "result": json.dumps(
                            evaluation, allow_nan=False, ensure_ascii=False
                        ),
                    },
                )
                if saved.rowcount != 1:
                    raise ValueError("恢复评估记录已变化")

            await transaction(save_metrics)
            results.append(
                {
                    **evaluation,
                    "status": "completed",
                    "reconstructed": not bool((item.get("factor_code") or "").strip()),
                    "factor_code_sha256": code_hash,
                    "factor_revision_no": revision,
                }
            )
            failures = 0
        except SandboxExecutionCancelled:
            raise
        except Exception as exc:
            if lifecycle["create_started"] and not lifecycle["confirmed_stopped"]:
                raise
            results.append(
                {
                    "factor_id": item["factor_id"],
                    "status": "failed",
                    "factor_revision_no": revision,
                    "error": redact_text(str(exc))[:1000],
                }
            )
            error = results[-1]["error"]

            def failed(session):
                session.execute(
                    text(
                        "UPDATE alpha_factor_recovery_sources SET status='failed',error_message=:error "
                        "WHERE tenant_id=:tenant_id AND user_id=:user_id AND job_id=:job AND factor_id=:factor "
                        "AND execution_generation=:generation AND status IN ('generating','code_saved')"
                    ),
                    {
                        **owner.sql_params(),
                        "job": job_id,
                        "factor": item["factor_id"],
                        "generation": generation,
                        "error": error,
                    },
                )

            await transaction(failed)
            failures += 1
        await progress(
            {
                "stage": "恢复评估",
                "progress_pct": round(
                    100 * len(results) / len(request["factor_items"])
                ),
            }
        )
        if failures >= 3:
            break
    lifecycle["confirmed_stopped"] = True
    return {
        "results": results,
        "factor_ids": request["factor_ids"],
        "factor_count": len(request["factor_ids"]),
        "completed": sum(row["status"] == "completed" for row in results),
        "failed": sum(row["status"] == "failed" for row in results),
        "unprocessed": request["factor_ids"][len(results) :],
        "circuit_broken": failures >= 3,
    }
