"""挖掘任务中心 store —— ``rd_agent_mining_tasks``（机构级 P0 / T-FM-01）。

任务状态此前只在进程内存 + `/tmp/alpha_agent_logs/<task_id>/task_state.json`：
engine 一重启就失忆、任务列表上限 20 条、direction 从不落库。本模块给出
PG 权威的任务记录行，让「每次挖了什么 / 挖出几个因子 / 什么状态」可回看。

纪律（与本仓其它 store 一致 + 项目时间口径）：

- 三列时间全走 `utc_now()` 写入、`to_utc_iso` 输出（TIMESTAMPTZ + Z 后缀）；
- 终态只能走 :meth:`mark_terminal`：它同时维护 ``completed_at``。
  ``update_progress`` 只收 pending/running——从进度通道把任务写成 completed
  会绕过 completed_at 的维护（历史页只能显示「完成时间未知」）；
- 状态白名单在**进 SQL 前**解析：未知状态显式 ValueError，绝不静默查空
  （静默查空会让「挖了但没显示」变成无从定位的失忆）。任务状态与因子状态
  不同名（任务有 cancelled、没有 backtesting），跨域混用靠白名单拦下；
- 对账 :meth:`reconcile_orphans` 只翻 pending/running（见方法 docstring）。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from sqlalchemy import text

from backend.shared.database_manager_v2 import get_session
from backend.shared.utc_datetime import to_utc_iso, utc_now

logger = logging.getLogger(__name__)

DEFAULT_HISTORY_LIMIT = 50
MAX_HISTORY_LIMIT = 200
MAX_DIRECTION_CHARS = 20000

#: 单文档任务明细（T-FM-19b 一文档多方向）的返回上限；真实总数另走
#: :meth:`MiningTaskStore.count_tasks_by_docs`——明细截断时界面靠它报「共 N 个」，
#: 绝不把「最近 20 条」冒充全部。
DOC_TASKS_LIMIT = 20

# 任务状态全集。注意与**因子**状态（pending/backtesting/completed/failed）
# 不是同一套：任务多了 queued/cancelled、没有 backtesting。
# queued = 已达并发上限、等名额的排队任务（launcher 排水后翻 running）；
# 重启对账（reconcile_orphans）**不碰** queued 行——它不是孤儿。
TASK_STATUSES = ("pending", "queued", "running", "completed", "failed", "cancelled")
TERMINAL_STATUSES = ("completed", "failed", "cancelled")
_PROGRESS_STATUSES = ("pending", "running")
#: 建行时允许的初始状态：running 只能由心跳/对账到来，终态只能走 mark_terminal。
_INITIAL_STATUSES = ("pending", "queued")

_TS_FIELDS = ("created_at", "updated_at", "completed_at")

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS rd_agent_mining_tasks (
  task_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  market TEXT NOT NULL DEFAULT 'a_share',
  universe TEXT,
  data_source TEXT,
  direction TEXT NOT NULL DEFAULT '',
  direction_mode TEXT,
  loop_n INTEGER,
  source TEXT NOT NULL DEFAULT 'text',
  doc_id TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  progress_pct INTEGER NOT NULL DEFAULT 0,
  current_loop INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  factor_count INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  completed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_rd_mining_tasks_user ON rd_agent_mining_tasks (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_rd_mining_tasks_status ON rd_agent_mining_tasks (status);
"""


def clamp_direction(value: str | None) -> str:
    """方向文本入库前的归一：None/空白 → ""；超长截断（防超长文档草稿撑爆行）。"""
    text_value = (value or "").strip()
    return text_value[:MAX_DIRECTION_CHARS]


def resolve_history_filters(
    *,
    market: str | None,
    status: str | None,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    """列表过滤参数的唯一解析点：空白=不过滤、limit/offset 收敛、状态白名单。

    未知状态抛 ``ValueError`` —— 静默查空的代价是「挖了但没显示」无从定位。
    """
    clean_market = (market or "").strip() or None
    clean_status = (status or "").strip() or None
    if clean_status is not None and clean_status not in TASK_STATUSES:
        raise ValueError(
            f"unknown task status: {clean_status!r}; expected one of {TASK_STATUSES}"
        )

    return {
        "market": clean_market,
        "status": clean_status,
        "limit": max(1, min(int(limit), MAX_HISTORY_LIMIT)),
        "offset": max(0, int(offset)),
    }


def _history_where(
    user_id: str, filters: Mapping[str, Any]
) -> tuple[list[str], dict[str, Any]]:
    """历史列表/计数的共用 WHERE（过滤参数须先过 resolve_history_filters）。"""
    where = ["user_id = :user_id"]
    params: dict[str, Any] = {"user_id": user_id}
    if filters["market"] is not None:
        where.append("market = :market")
        params["market"] = filters["market"]
    if filters["status"] is not None:
        where.append("status = :status")
        params["status"] = filters["status"]
    return where, params


def row_to_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    """DB 行 → API 字典：三列时间统一 ISO-8601 UTC（带 Z）。

    未知列原样透传（前向兼容）；缺列不补——接口对「没有的值」必须诚实。
    """
    out = dict(row)
    for field in _TS_FIELDS:
        if field in out:
            out[field] = to_utc_iso(out[field])
    return out


class MiningTaskStore:
    """``rd_agent_mining_tasks`` 的读写。所有写入失败都由调用方吞掉（不拦挖掘主链）。"""

    async def ensure_tables(self) -> None:
        """建表 + 索引（幂等；engine 启动期调用）。"""
        async with get_session() as session:
            for stmt in [s.strip() for s in _CREATE_TABLE_SQL.split(";") if s.strip()]:
                await session.execute(text(stmt))
        logger.info("rd_agent_mining_tasks table ensured")

    async def create_task(
        self,
        *,
        task_id: str,
        user_id: str,
        status: str = "pending",
        market: str = "a_share",
        universe: str = "",
        data_source: str = "",
        direction: str = "",
        direction_mode: str | None = None,
        loop_n: int = 5,
        source: str = "text",
        doc_id: str | None = None,
    ) -> None:
        """落任务行。task_id 撞键是无操作（重放/重试不该把首次写入覆盖掉）。

        ``status`` 只收初始态（pending/queued）——校验发生在开 session 之前
        （无库环境里也要红/绿分明）。
        """
        if status not in _INITIAL_STATUSES:
            raise ValueError(
                f"create_task 只收初始状态 {_INITIAL_STATUSES}，收到 {status!r}；"
                "running 由心跳/对账到来，终态请走 mark_terminal"
            )
        now = utc_now()
        async with get_session() as session:
            await session.execute(
                text("""
                    INSERT INTO rd_agent_mining_tasks
                      (task_id, user_id, market, universe, data_source, direction,
                       direction_mode, loop_n, source, doc_id, status,
                       progress_pct, current_loop, created_at, updated_at)
                    VALUES
                      (:task_id, :user_id, :market, :universe, :data_source, :direction,
                       :direction_mode, :loop_n, :source, :doc_id, :status,
                       0, 0, :now, :now)
                    ON CONFLICT (task_id) DO NOTHING
                    """),
                {
                    "task_id": task_id,
                    "user_id": user_id,
                    "status": status,
                    "market": market or "a_share",
                    "universe": universe or "",
                    "data_source": data_source or "",
                    "direction": clamp_direction(direction),
                    "direction_mode": direction_mode,
                    "loop_n": int(loop_n),
                    "source": source or "text",
                    "doc_id": doc_id,
                    "now": now,
                },
            )

    async def update_progress(
        self,
        task_id: str,
        *,
        status: str = "running",
        progress_pct: int,
        current_loop: int,
    ) -> None:
        """进度心跳。只收 pending/running——终态必须走 :meth:`mark_terminal`。"""
        if status not in _PROGRESS_STATUSES:
            raise ValueError(
                f"update_progress 只收 {_PROGRESS_STATUSES}，收到 {status!r}；"
                "终态请走 mark_terminal（它维护 completed_at）"
            )
        async with get_session() as session:
            await session.execute(
                text("""
                    UPDATE rd_agent_mining_tasks
                    SET status = :status, progress_pct = :pct, current_loop = :loop,
                        updated_at = :now
                    WHERE task_id = :task_id
                    """),
                {
                    "status": status,
                    "pct": int(progress_pct),
                    "loop": int(current_loop),
                    "now": utc_now(),
                    "task_id": task_id,
                },
            )

    async def mark_terminal(
        self,
        task_id: str,
        *,
        status: str,
        error: str | None = None,
        factor_count: int | None = None,
    ) -> None:
        """写终态：同时补 ``completed_at``（error 只在失败/取消时给）。"""
        if status not in TERMINAL_STATUSES:
            raise ValueError(
                f"mark_terminal 只收终态 {TERMINAL_STATUSES}，收到 {status!r}"
            )

        now = utc_now()
        fields: dict[str, Any] = {
            "status": status,
            "error": error,
            "now": now,
            "task_id": task_id,
        }
        set_clause = (
            "status = :status, error = :error, completed_at = :now, updated_at = :now"
        )
        if factor_count is not None:
            set_clause += ", factor_count = :factor_count"
            fields["factor_count"] = int(factor_count)

        async with get_session() as session:
            await session.execute(
                text(
                    f"UPDATE rd_agent_mining_tasks SET {set_clause} WHERE task_id = :task_id"
                ),
                fields,
            )

    async def count_factors(self, task_id: str) -> int:
        """该任务已落库的因子数（``metadata_json->>'task_id'`` 口径，与 /factors 端点一致）。"""
        async with get_session(read_only=True) as session:
            return int(
                (
                    await session.execute(
                        text(
                            "SELECT count(*) FROM rd_agent_factors "
                            "WHERE metadata_json->>'task_id' = :task_id"
                        ),
                        {"task_id": task_id},
                    )
                ).scalar_one()
            )

    async def get_task(
        self, task_id: str, *, user_id: str | None = None
    ) -> dict[str, Any] | None:
        """按 id 取行；给 user_id 时按归属收口（查空 = 不存在或不属于该用户）。"""
        query = "SELECT * FROM rd_agent_mining_tasks WHERE task_id = :task_id"
        params: dict[str, Any] = {"task_id": task_id}
        if user_id is not None:
            query += " AND user_id = :user_id"
            params["user_id"] = user_id

        async with get_session(read_only=True) as session:
            row = (await session.execute(text(query), params)).mappings().first()
        return row_to_dict(row) if row else None

    async def list_history(
        self,
        *,
        user_id: str,
        market: str | None = None,
        status: str | None = None,
        limit: int = DEFAULT_HISTORY_LIMIT,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """用户历史（倒序 + 分页）。过滤参数先经 :func:`resolve_history_filters`。"""
        filters = resolve_history_filters(
            market=market, status=status, limit=limit, offset=offset
        )

        where, params = _history_where(user_id, filters)
        params["limit"] = filters["limit"]
        params["offset"] = filters["offset"]

        query = (
            "SELECT * FROM rd_agent_mining_tasks "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY created_at DESC, task_id DESC "
            "LIMIT :limit OFFSET :offset"
        )
        async with get_session(read_only=True) as session:
            rows = (await session.execute(text(query), params)).mappings().all()
        return [row_to_dict(r) for r in rows]

    async def count_history(
        self,
        *,
        user_id: str,
        market: str | None = None,
        status: str | None = None,
    ) -> int:
        """同过滤条件下的全量行数（分页「共 N 条」）。

        必须是真的 COUNT：拿本页行数当 total，分页器永远显示 ≤ limit 条。
        """
        filters = resolve_history_filters(
            market=market, status=status, limit=1, offset=0
        )
        where, params = _history_where(user_id, filters)
        query = (
            f"SELECT count(*) FROM rd_agent_mining_tasks WHERE {' AND '.join(where)}"
        )
        async with get_session(read_only=True) as session:
            return int((await session.execute(text(query), params)).scalar_one())

    async def count_tasks_by_docs(
        self, *, user_id: str, doc_ids: list[str]
    ) -> dict[str, int]:
        """按文档批量计任务数（文档列表「N 个方向」徽标），**只回非零项**。

        空入参零 SQL 直返：空文档列表不该为一次没有内容的聚合伙付查询往返。
        没出现的 doc_id 语义 = 0 条任务（真零，不是「查不到」）——调用方
        只在查询本身失败时才有「未知」态，两者绝不可混。
        """
        ids = [d for d in doc_ids if d]
        if not ids:
            return {}
        async with get_session(read_only=True) as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT doc_id, count(*) AS n FROM rd_agent_mining_tasks "
                            "WHERE user_id = :user_id AND doc_id = ANY(:doc_ids) "
                            "GROUP BY doc_id"
                        ),
                        {"user_id": user_id, "doc_ids": ids},
                    )
                )
                .mappings()
                .all()
            )
        return {str(r["doc_id"]): int(r["n"]) for r in rows}

    async def list_by_doc(
        self, *, user_id: str, doc_id: str, limit: int = DOC_TASKS_LIMIT
    ) -> list[dict[str, Any]]:
        """某文档关联的挖掘任务（最近优先）——一文档多方向回看的明细面。

        只选展示四列（task_id/status/direction/created_at）：direction 单条
        可达两万字，整行透出会把详情响应撑大一个量级；市场/池等字段任务中心
        页面已有，本处不重复搬运。
        """
        capped = max(1, min(int(limit), MAX_HISTORY_LIMIT))
        async with get_session(read_only=True) as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT task_id, status, direction, created_at "
                            "FROM rd_agent_mining_tasks "
                            "WHERE user_id = :user_id AND doc_id = :doc_id "
                            "ORDER BY created_at DESC, task_id DESC "
                            "LIMIT :limit"
                        ),
                        {"user_id": user_id, "doc_id": doc_id, "limit": capped},
                    )
                )
                .mappings()
                .all()
            )
        return [row_to_dict(r) for r in rows]

    async def reconcile_orphans(self, *, user_id: str | None = None) -> int:
        """重启对账：把孤儿行（pending/running）翻 failed，返回翻转行数。

        只在 engine 启动期调用：那一刻本进程没有任何活任务，表里还挂着
        pending/running 的行必然是上次进程留下的孤儿（子进程随会话起、
        进程亡后无人回收）。completed 行绝不触碰。
        """
        now = utc_now()
        query = (
            "UPDATE rd_agent_mining_tasks "
            "SET status = 'failed', "
            "    error = COALESCE(error, 'Server restarted while task was running'), "
            "    completed_at = :now, updated_at = :now "
            "WHERE status IN ('pending', 'running')"
        )
        params: dict[str, Any] = {"now": now}
        if user_id is not None:
            query += " AND user_id = :user_id"
            params["user_id"] = user_id

        async with get_session() as session:
            result = await session.execute(text(query), params)
            flipped = int(result.rowcount or 0)
        if flipped:
            logger.warning(
                "reconcile_orphans flipped %d orphan mining task(s) to failed", flipped
            )
        return flipped


_store: MiningTaskStore | None = None


def get_mining_task_store() -> MiningTaskStore:
    global _store
    if _store is None:
        _store = MiningTaskStore()
    return _store
