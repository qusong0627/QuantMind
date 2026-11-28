"""把 13 模型训练产物补推（推理）到最新交易日。

前置：
1. `register_all13.py` 已注册父模型并拆出 13 条子模型；
2. 子模型状态为 ready/active（软门禁未拦住）。

用法（API 容器内，长任务建议放后台）：
    docker exec quantmind python /app/docker/training/qm_ab_30f/backfill_all13.py

脚本会：
- 按 source_run_id 查出本次训练的 13 条子模型（排除父模型）；
- 调 create_job 建一个串行补推任务（后端同一进程的 asyncio loop 内运行）；
- 轮询任务直到 completed/failed/cancelled，并打印每个模型的完成/失败天数。
"""

from __future__ import annotations

import asyncio
import json
import os

# 补推只做「算分 + 合并 pred」，不触发实盘侧效应：
# ENABLE_TDX_PUSH=true 时 InferenceScriptRunner 会推送通达信选股并执行滚动买卖；
# 批量补历史日绝不能下真单/触发滚动，且桥不可达时每天都会等超时。
os.environ["ENABLE_TDX_PUSH"] = "false"
# 同一模型的多个交易日并发推理（各日独立子进程，互不冲突）；默认 1 是安全值。
os.environ["BACKFILL_DAY_WORKERS"] = "4"

from sqlalchemy import text

from backend.services.engine.services.model_inference_job_service import (
    create_job,
    model_inference_job_persistence,
)
from backend.shared.database_manager_v2 import get_session
from backend.shared.model_registry import model_registry_service

RUN_ID = "qm_ab30_all13_20261127"
TENANT_ID = "default"
USER_ID = "10000001"
POLL_SECONDS = 15


async def _child_model_ids() -> list[str]:
    parent_id = model_registry_service.build_model_id_from_run(RUN_ID, market="CN")
    async with get_session() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT model_id, status
                    FROM qm_user_models
                    WHERE tenant_id = :tenant_id
                      AND user_id = :user_id
                      AND source_run_id = :run_id
                    ORDER BY model_id
                    """
                ),
                {
                    "tenant_id": TENANT_ID,
                    "user_id": USER_ID,
                    "run_id": RUN_ID,
                },
            )
        ).fetchall()
    entries = [(str(r[0]), str(r[1] or "").lower()) for r in rows]
    print(f"[info] parent={parent_id} rows={len(entries)}")
    for mid, status in entries:
        print(f"  - {mid} status={status}")
    return [
        mid
        for mid, status in entries
        if mid != parent_id and status in ("ready", "active")
    ]


async def main() -> None:
    model_ids = await _child_model_ids()
    if not model_ids:
        print("[error] 没有 ready/active 的子模型；先跑 register_all13.py 或检查门禁状态")
        return
    print(f"[info] backfill models={len(model_ids)}")
    job = await create_job(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        model_ids=model_ids,
        persistence=model_inference_job_persistence,
    )
    job_id = str(job.get("job_id") or "")
    print("[job]", json.dumps(job, ensure_ascii=False, default=str)[:3000])
    if not job_id:
        return
    while True:
        await asyncio.sleep(POLL_SECONDS)
        current = await model_inference_job_persistence.get_job(
            job_id, tenant_id=TENANT_ID, user_id=USER_ID
        )
        if not current:
            print("[warn] job row disappeared")
            return
        status = str(current.get("status") or "")
        print(
            f"[progress] {status} models={current.get('completed_models')}/"
            f"{current.get('total_models')} days={current.get('completed_days')}/"
            f"{current.get('total_days')} failed={current.get('failed_days')}",
            flush=True,
        )
        if status in ("completed", "failed", "cancelled", "partial"):
            print("[final]", json.dumps(current, ensure_ascii=False, default=str)[:12000])
            return


if __name__ == "__main__":
    asyncio.run(main())
