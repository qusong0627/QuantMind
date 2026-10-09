#!/usr/bin/env python3
"""挖掘历史 legacy 回填：``rd_agent_factors`` → ``rd_agent_mining_tasks``（机构级 P0 / T-FM-05）。

任务中心上线前挖过的任务没有记录行——历史页上它们是「不存在的过去」。
本脚本按 ``metadata_json->>'task_id'`` 把存量因子聚合回任务行：

- 一行 = 一个 task_id；user/market/universe 取该任务因子行的聚合值；
- direction 取 metadata 里的 ``direction``（早期 metadata 大多没有 → 空串，
  前端显示「（早期任务无方向记录）」而不是编造）；
- status 一律 ``completed``、source ``legacy``——因子都落库了说明挖掘跑完过；
  真实终态无从考证的部分宁缺毋滥（不猜 failed）；
- created_at/completed_at = 该任务最早/最晚因子时间；factor_count = 因子数。

幂等：``ON CONFLICT DO NOTHING``，已存在的 task_id（含本脚本重跑）一概不动。
默认 dry-run，``--apply`` 才落库。

用法::

    python backend/scripts/backfill_mining_task_history.py            # 干跑报告
    python backend/scripts/backfill_mining_task_history.py --apply    # 落库
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

_project_root = Path(__file__).resolve().parents[2]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from sqlalchemy import text  # noqa: E402

from backend.shared.database_manager_v2 import get_session  # noqa: E402
from backend.shared.utc_datetime import utc_now  # noqa: E402

logger = logging.getLogger("backfill_mining_task_history")

_AGG_SQL = """
SELECT metadata_json->>'task_id' AS task_id,
       COALESCE(max(user_id), '') AS user_id,
       COALESCE(max(market), 'a_share') AS market,
       COALESCE(max(universe), '') AS universe,
       COALESCE(max(metadata_json->>'direction'), '') AS direction,
       count(*) AS factor_count,
       min(created_at) AS created_at,
       max(created_at) AS completed_at
FROM rd_agent_factors
WHERE metadata_json->>'task_id' IS NOT NULL
  AND metadata_json->>'task_id' <> ''
GROUP BY 1
ORDER BY 7
"""

_INSERT_SQL = """
INSERT INTO rd_agent_mining_tasks
  (task_id, user_id, market, universe, data_source, direction, loop_n,
   source, status, progress_pct, current_loop, factor_count,
   created_at, updated_at, completed_at)
VALUES (:task_id, :user_id, :market, :universe, '', :direction, NULL,
        'legacy', 'completed', 100, 0, :factor_count,
        :created_at, :updated_at, :completed_at)
ON CONFLICT (task_id) DO NOTHING
"""


async def run(*, apply: bool) -> int:
    from backend.services.engine.alpha_agent.task_store import get_mining_task_store

    await get_mining_task_store().ensure_tables()

    async with get_session(read_only=True) as session:
        rows = (await session.execute(text(_AGG_SQL))).mappings().all()

    if not rows:
        logger.info("没有可回填的遗留任务（rd_agent_factors 无带 task_id 的因子）")
        return 0

    async with get_session(read_only=True) as session:
        existing = {
            r[0]
            for r in (
                await session.execute(text("SELECT task_id FROM rd_agent_mining_tasks"))
            ).all()
        }
    planned = [r for r in rows if r["task_id"] not in existing]

    logger.info(
        "聚合出 %d 个遗留任务；已有记录 %d 个；待回填 %d 个",
        len(rows),
        len(rows) - len(planned),
        len(planned),
    )
    for r in planned[:20]:
        logger.info(
            "  %s  user=%s market=%s 因子=%d 起=%s",
            r["task_id"],
            r["user_id"],
            r["market"],
            r["factor_count"],
            r["created_at"],
        )
    if len(planned) > 20:
        logger.info("  …… 其余 %d 个略", len(planned) - 20)

    if not apply:
        logger.info("dry-run：加 --apply 落库")
        return 0

    inserted = 0
    now = utc_now()
    async with get_session() as session:
        for r in planned:
            result = await session.execute(
                text(_INSERT_SQL),
                {
                    "task_id": r["task_id"],
                    "user_id": r["user_id"],
                    "market": r["market"],
                    "universe": r["universe"],
                    "direction": (r["direction"] or "").strip(),
                    "factor_count": int(r["factor_count"]),
                    "created_at": r["created_at"],
                    "updated_at": now,
                    "completed_at": r["completed_at"],
                },
            )
            inserted += int(result.rowcount or 0)

    logger.info("回填完成：新增 %d 行（其余为并发已存在的记录）", inserted)
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    parser = argparse.ArgumentParser(
        description="rd_agent_factors → rd_agent_mining_tasks 回填"
    )
    parser.add_argument(
        "--apply", action="store_true", help="落库（默认只干跑报告，不写任何行）"
    )
    args = parser.parse_args()
    return asyncio.run(run(apply=args.apply))


if __name__ == "__main__":
    sys.exit(main())
