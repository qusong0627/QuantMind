"""T-FB-06 跨市场回测台账持久化。

复用既有 ``rd_agent_factor_backtests`` 表（一次运行一行），做三件扩展：

1. **扩列**：``kind``（functional/factor_class）+ ``params_json``（窗口/费率等
   运行参数快照——数字可复现的前提）；
2. **七态词表**：原 CHECK 仅四态，诚实降级三态（``data_unsupported`` 静态缺列 /
   ``insufficient`` 有效日不足 / ``unavailable`` 市场数据未就绪）必须可落账；
3. **序列表** ``rd_agent_factor_backtest_series``：run_id 主键的曲线载荷
   （nav/IC/分位/换手），与台账行分离——列表查询不拖大 JSON。

纪律：
- 收口一律**按 run_id 精确收口**（``WHERE run_id=:run_id AND status='running'
  AND finished_at IS NULL``），绝不「最新未完结行」启发式；
- 本模块的 ``ensure_tables`` 要求 ``RDAgentFactorPersistence.ensure_tables``
  已先行（engine 启动次序保证）——它负责基表，本模块只做扩展，避免双份 DDL 漂移；
- **跨市场 run 只写本表，绝不更新 ``rd_agent_factors`` 行**（那是 CN 因子列表
  的家，被 HK/US 指标覆盖就毁了样本内基准）。
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import uuid4

from sqlalchemy import text

from backend.shared.database_manager_v2 import get_session

logger = logging.getLogger(__name__)

#: 终态词表（running 只能由 start_run 写；收口不接受）。
TERMINAL_STATUSES: tuple[str, ...] = (
    "completed",
    "failed",
    "cancelled",
    "data_unsupported",
    "insufficient",
    "unavailable",
)

_ALL_STATUSES: tuple[str, ...] = ("running",) + TERMINAL_STATUSES

#: 批量任务终态（T-FB-08/09）：aborted = 熔断/台账不可用主动中止（≠ 用户取消）。
BATCH_TERMINAL_STATUSES: tuple[str, ...] = ("completed", "cancelled", "aborted")


async def ensure_tables() -> None:
    """扩展列 + 七态词表 + 序列表（幂等；基表先由 RDAgentFactorPersistence 建好）。"""
    async with get_session() as session:
        await session.execute(
            text(
                "ALTER TABLE rd_agent_factor_backtests "
                "ADD COLUMN IF NOT EXISTS kind TEXT"
            )
        )
        await session.execute(
            text(
                "ALTER TABLE rd_agent_factor_backtests "
                "ADD COLUMN IF NOT EXISTS params_json JSONB"
            )
        )
        await session.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_rd_agent_factor_backtests_market "
                "ON rd_agent_factor_backtests(market, created_at DESC)"
            )
        )
        # 词表扩七态：DROP+ADD 幂等（老库四态 CHECK 必须先撤再挂）。存量行 status
        # 全部落在新词表内（只是多了被允许的值），迁移不会失败。
        values = ", ".join(f"'{s}'" for s in _ALL_STATUSES)
        await session.execute(
            text(
                "ALTER TABLE rd_agent_factor_backtests "
                "DROP CONSTRAINT IF EXISTS rd_agent_factor_backtests_status_check"
            )
        )
        await session.execute(
            text(
                "ALTER TABLE rd_agent_factor_backtests "
                "ADD CONSTRAINT rd_agent_factor_backtests_status_check "
                f"CHECK (status IN ({values}))"
            )
        )
        await session.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS rd_agent_factor_backtest_series (
                  run_id TEXT PRIMARY KEY,
                  factor_id TEXT NOT NULL,
                  market TEXT NOT NULL,
                  series_json JSONB NOT NULL,
                  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        )
        await session.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_rd_agent_fb_series_factor "
                "ON rd_agent_factor_backtest_series(factor_id, market)"
            )
        )
        # T-FB-08/09 批量引擎：台账行挂 batch_id（行终态回读 = 批次进度的唯一事实源），
        # 批次头表只存 spec 与批次级终态（running → completed/cancelled/aborted）。
        await session.execute(
            text(
                "ALTER TABLE rd_agent_factor_backtests "
                "ADD COLUMN IF NOT EXISTS batch_id TEXT"
            )
        )
        await session.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_rd_agent_factor_backtests_batch "
                "ON rd_agent_factor_backtests(batch_id, status)"
            )
        )
        await session.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS rd_agent_factor_backtest_batches (
                  batch_id TEXT PRIMARY KEY,
                  user_id TEXT,
                  status TEXT NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running', 'completed', 'cancelled', 'aborted')),
                  spec_json JSONB NOT NULL,
                  error TEXT,
                  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                  finished_at TIMESTAMPTZ
                )
                """
            )
        )
        await session.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_rd_agent_fb_batches_status "
                "ON rd_agent_factor_backtest_batches(status, created_at DESC)"
            )
        )
    logger.info(
        "factor_backtest tables ensured (kind/params_json/series/7-status/batch)"
    )


async def start_run(
    factor_id: str,
    *,
    kind: str,
    market: str | None = None,
    universe: str | None = None,
    data_source: str = "qlib_bin",
    params: dict[str, Any] | None = None,
    factor_name: str | None = None,
    user_id: str | None = None,
    batch_id: str | None = None,
) -> str:
    """登记一次跨市场回测运行（status='running'）。返回 run_id。"""
    run_id = f"fb-{uuid4().hex}"
    async with get_session() as session:
        await session.execute(
            text(
                """
                INSERT INTO rd_agent_factor_backtests
                    (run_id, factor_id, factor_name, user_id, status,
                     market, universe, data_source, kind, params_json, batch_id)
                VALUES
                    (:run_id, :factor_id, :factor_name, :user_id, 'running',
                     :market, :universe, :data_source, :kind,
                     CAST(:params_json AS JSONB), :batch_id)
                """
            ),
            {
                "run_id": run_id,
                "factor_id": factor_id,
                "factor_name": factor_name,
                "user_id": user_id,
                "market": market,
                "universe": universe,
                "data_source": data_source,
                "kind": kind,
                "params_json": json.dumps(params or {}, ensure_ascii=False),
                "batch_id": batch_id,
            },
        )
    return run_id


async def finish_run(
    run_id: str,
    status: str,
    *,
    ic_value: float | None = None,
    rank_ic: float | None = None,
    icir: float | None = None,
    rank_icir: float | None = None,
    sharpe_ratio: float | None = None,
    annual_return: float | None = None,
    max_drawdown: float | None = None,
    universe: str | None = None,
    data_source: str | None = None,
    date_range: str | None = None,
    metrics: dict[str, Any] | None = None,
    error: str | None = None,
) -> bool:
    """按 run_id 精确收口（幂等：仅当该行仍在 running 且未收口时生效）。

    与 ``RDAgentFactorPersistence.finish_backtest_run`` 同一纪律；本模块独立
    实现是为了支持七态终态（原实现仅收三态）。第二次调用返回 False。
    """
    if status not in TERMINAL_STATUSES:
        raise ValueError(f"invalid terminal status: {status!r}")

    fields: dict[str, Any] = {"status": status}
    for key, value in (
        ("ic_value", ic_value),
        ("rank_ic", rank_ic),
        ("icir", icir),
        ("rank_icir", rank_icir),
        ("sharpe_ratio", sharpe_ratio),
        ("annual_return", annual_return),
        ("max_drawdown", max_drawdown),
        ("universe", universe),
        ("data_source", data_source),
        ("date_range", date_range),
    ):
        if value is not None:
            fields[key] = value
    params: dict[str, Any] = {**fields, "run_id": run_id}
    set_clause = ", ".join(f"{k} = :{k}" for k in fields)
    if metrics is not None:
        set_clause += ", metrics_json = CAST(:metrics_json AS JSONB)"
        params["metrics_json"] = json.dumps(metrics, ensure_ascii=False)
    if error is not None:
        set_clause += ", error = :error"
        params["error"] = error

    async with get_session() as session:
        result = await session.execute(
            text(
                f"""
                UPDATE rd_agent_factor_backtests
                SET {set_clause}, finished_at = now()
                WHERE run_id = :run_id
                  AND status = 'running'
                  AND finished_at IS NULL
                """
            ),
            params,
        )
        return (result.rowcount or 0) > 0


async def save_series(
    run_id: str, *, factor_id: str, market: str, payload: dict[str, Any]
) -> None:
    """落曲线载荷（同 run_id 幂等覆盖）。"""
    async with get_session() as session:
        await session.execute(
            text(
                """
                INSERT INTO rd_agent_factor_backtest_series
                    (run_id, factor_id, market, series_json)
                VALUES (:run_id, :factor_id, :market, CAST(:series_json AS JSONB))
                ON CONFLICT (run_id) DO UPDATE
                    SET series_json = EXCLUDED.series_json
                """
            ),
            {
                "run_id": run_id,
                "factor_id": factor_id,
                "market": market,
                "series_json": json.dumps(payload, ensure_ascii=False),
            },
        )


async def get_series(run_id: str) -> dict[str, Any] | None:
    """取曲线载荷；无则 None（路由层翻 404）。"""
    async with get_session(read_only=True) as session:
        rows = await session.execute(
            text(
                """
                SELECT run_id, factor_id, market, series_json, created_at
                FROM rd_agent_factor_backtest_series
                WHERE run_id = :run_id
                """
            ),
            {"run_id": run_id},
        )
        row = rows.mappings().first()
    if row is None:
        return None
    item = dict(row)
    raw = item.pop("series_json", None)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    item["series"] = raw if isinstance(raw, dict) else None
    return item


async def list_runs(
    *,
    factor_id: str | None = None,
    market: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """台账列表（新→旧，分页）；``has_series`` 直接告诉前端能否下钻曲线。"""
    clauses: list[str] = []
    params: dict[str, Any] = {"limit": int(limit), "offset": int(offset)}
    if factor_id:
        clauses.append("b.factor_id = :factor_id")
        params["factor_id"] = factor_id
    if market:
        clauses.append("b.market = :market")
        params["market"] = market
    if status:
        clauses.append("b.status = :status")
        params["status"] = status
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    async with get_session(read_only=True) as session:
        rows = await session.execute(
            text(
                f"""
                SELECT b.run_id, b.factor_id, b.factor_name, b.status, b.kind,
                       b.market, b.universe, b.data_source, b.date_range,
                       b.ic_value, b.rank_ic, b.icir, b.rank_icir,
                       b.sharpe_ratio, b.annual_return, b.max_drawdown,
                       b.metrics_json, b.params_json, b.error,
                       b.created_at, b.finished_at,
                       EXISTS (
                           SELECT 1 FROM rd_agent_factor_backtest_series s
                           WHERE s.run_id = b.run_id
                       ) AS has_series
                FROM rd_agent_factor_backtests b
                {where}
                ORDER BY b.created_at DESC, b.run_id DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            params,
        )
        return [_unpack(dict(r)) for r in rows.mappings().all()]


async def latest_cells(
    factor_ids: list[str],
    markets: list[str] | None = None,
) -> list[dict[str, Any]]:
    """矩阵单元格：每个 (factor_id, market) 的**最近一次**运行（任意终态）。

    取最近一次而非「最近一次成功」：``insufficient``/``failed`` 本身就是要
    展示的适配结论（诚实降级），静默跳过会把失败化妆成「没跑过」。
    """
    if not factor_ids:
        return []
    params: dict[str, Any] = {"factor_ids": list(factor_ids)}
    market_clause = ""
    if markets:
        market_clause = "AND market = ANY(:markets)"
        params["markets"] = list(markets)
    async with get_session(read_only=True) as session:
        rows = await session.execute(
            text(
                f"""
                SELECT DISTINCT ON (factor_id, market)
                       run_id, factor_id, factor_name, status, kind,
                       market, universe, data_source, date_range,
                       ic_value, rank_ic, icir, rank_icir,
                       sharpe_ratio, annual_return, max_drawdown,
                       metrics_json, error, created_at, finished_at
                FROM rd_agent_factor_backtests
                WHERE factor_id = ANY(:factor_ids)
                  {market_clause}
                ORDER BY factor_id, market, created_at DESC, run_id DESC
                """
            ),
            params,
        )
        return [_unpack(dict(r)) for r in rows.mappings().all()]


async def get_run(run_id: str) -> dict[str, Any] | None:
    """按 run_id 取单行（归属校验/报告装配用；``batch_id`` 供报告层查族）。"""
    async with get_session(read_only=True) as session:
        rows = await session.execute(
            text(
                """
                SELECT b.run_id, b.factor_id, b.factor_name, b.status, b.kind,
                       b.batch_id,
                       b.market, b.universe, b.data_source, b.date_range,
                       b.ic_value, b.rank_ic, b.icir, b.rank_icir,
                       b.sharpe_ratio, b.annual_return, b.max_drawdown,
                       b.metrics_json, b.params_json, b.error,
                       b.created_at, b.finished_at,
                       EXISTS (
                           SELECT 1 FROM rd_agent_factor_backtest_series s
                           WHERE s.run_id = b.run_id
                       ) AS has_series
                FROM rd_agent_factor_backtests b
                WHERE b.run_id = :run_id
                """
            ),
            {"run_id": run_id},
        )
        row = rows.mappings().first()
    return _unpack(dict(row)) if row is not None else None


async def get_factor_meta(factor_ids: list[str]) -> list[dict[str, Any]]:
    """只读因子行元数据（矩阵表头/归属过滤）。**绝不写因子行**——跨市场 run
    的纪律是图表只进台账与序列表，rd_agent_factors 保持 CN 因子库语义。"""
    if not factor_ids:
        return []
    async with get_session(read_only=True) as session:
        rows = await session.execute(
            text(
                """
                SELECT factor_id, factor_name, factor_code, market, status,
                       ic_value, user_id
                FROM rd_agent_factors
                WHERE factor_id = ANY(:ids)
                """
            ),
            {"ids": list(factor_ids)},
        )
        return [dict(r) for r in rows.mappings().all()]


# ── 批量任务（T-FB-08/09）────────────────────────────────────────────


async def create_batch(
    batch_id: str, *, user_id: str | None, spec: dict[str, Any]
) -> None:
    """登记批次头（status='running'）；进度唯一事实源仍是各 run 行（batch_id）。"""
    async with get_session() as session:
        await session.execute(
            text(
                """
                INSERT INTO rd_agent_factor_backtest_batches
                    (batch_id, user_id, status, spec_json)
                VALUES (:batch_id, :user_id, 'running', CAST(:spec_json AS JSONB))
                """
            ),
            {
                "batch_id": batch_id,
                "user_id": user_id,
                "spec_json": json.dumps(spec, ensure_ascii=False),
            },
        )


async def get_batch(batch_id: str) -> dict[str, Any] | None:
    """取批次头（spec 解包为 ``spec``）。"""
    async with get_session(read_only=True) as session:
        rows = await session.execute(
            text(
                """
                SELECT batch_id, user_id, status, spec_json, error,
                       created_at, finished_at
                FROM rd_agent_factor_backtest_batches
                WHERE batch_id = :batch_id
                """
            ),
            {"batch_id": batch_id},
        )
        row = rows.mappings().first()
    if row is None:
        return None
    item = dict(row)
    raw = item.pop("spec_json", None)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    item["spec"] = raw if isinstance(raw, dict) else {}
    return item


async def finish_batch(batch_id: str, status: str, *, error: str | None = None) -> bool:
    """批次收口（幂等：仅当仍在 running 时生效）。第二次调用返回 False。"""
    if status not in BATCH_TERMINAL_STATUSES:
        raise ValueError(f"invalid batch terminal status: {status!r}")
    async with get_session() as session:
        result = await session.execute(
            text(
                """
                UPDATE rd_agent_factor_backtest_batches
                SET status = :status, error = :error, finished_at = now()
                WHERE batch_id = :batch_id
                  AND status = 'running'
                """
            ),
            {"batch_id": batch_id, "status": status, "error": error},
        )
        return (result.rowcount or 0) > 0


async def list_batches(
    *, user_id: str | None = None, limit: int = 20
) -> list[dict[str, Any]]:
    """批次列表（新→旧）。``user_id`` 给定时按其过滤（历史空属主行只读可见）。"""
    params: dict[str, Any] = {"limit": int(limit)}
    where = ""
    if user_id is not None:
        where = "WHERE (user_id = :user_id OR user_id IS NULL OR user_id = '')"
        params["user_id"] = user_id
    async with get_session(read_only=True) as session:
        rows = await session.execute(
            text(
                f"""
                SELECT batch_id, user_id, status, spec_json, error,
                       created_at, finished_at
                FROM rd_agent_factor_backtest_batches
                {where}
                ORDER BY created_at DESC, batch_id DESC
                LIMIT :limit
                """
            ),
            params,
        )
        out = []
        for r in rows.mappings().all():
            item = dict(r)
            raw = item.pop("spec_json", None)
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except ValueError:
                    raw = None
            item["spec"] = raw if isinstance(raw, dict) else {}
            out.append(item)
        return out


async def list_running_batches(limit: int = 20) -> list[dict[str, Any]]:
    """重启可重入扫描：所有仍未收口的批次（engine 启动/状态轮询时续跑）。"""
    async with get_session(read_only=True) as session:
        rows = await session.execute(
            text(
                """
                SELECT batch_id, user_id, status, spec_json, error,
                       created_at, finished_at
                FROM rd_agent_factor_backtest_batches
                WHERE status = 'running'
                ORDER BY created_at ASC
                LIMIT :limit
                """
            ),
            {"limit": int(limit)},
        )
        out = []
        for r in rows.mappings().all():
            item = dict(r)
            raw = item.pop("spec_json", None)
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except ValueError:
                    raw = None
            item["spec"] = raw if isinstance(raw, dict) else {}
            out.append(item)
        return out


async def batch_runs(batch_id: str) -> list[dict[str, Any]]:
    """批次全部 run 行（含各状态）——状态端点的「行终态回读」数据面。"""
    async with get_session(read_only=True) as session:
        rows = await session.execute(
            text(
                """
                SELECT run_id, factor_id, factor_name, status, kind,
                       market, universe, data_source, date_range,
                       ic_value, rank_ic, icir, rank_icir,
                       sharpe_ratio, annual_return, max_drawdown,
                       metrics_json, error, created_at, finished_at
                FROM rd_agent_factor_backtests
                WHERE batch_id = :batch_id
                ORDER BY created_at ASC, run_id ASC
                """
            ),
            {"batch_id": batch_id},
        )
        return [_unpack(dict(r)) for r in rows.mappings().all()]


async def running_factor_pairs(factor_ids: list[str]) -> list[dict[str, Any]]:
    """这些因子当前的活跃运行（按 factor 粒度：子进程登记以 factor_id 为键，
    同一因子不得跨市场并发——背压判据比 (factor, market) 更粗也更强）。"""
    if not factor_ids:
        return []
    async with get_session(read_only=True) as session:
        rows = await session.execute(
            text(
                """
                SELECT factor_id, market
                FROM rd_agent_factor_backtests
                WHERE factor_id = ANY(:ids)
                  AND status = 'running'
                  AND finished_at IS NULL
                """
            ),
            {"ids": list(factor_ids)},
        )
        return [dict(r) for r in rows.mappings().all()]


async def settle_orphan_running(batch_id: str, *, error: str) -> int:
    """重启恢复：把该批次遗留的 running 行收口为 failed。

    仅在「进程内无该批次状态」时调用（重启/重建路径）——此时这些 running 行
    的子进程已随旧进程消亡，纯孤儿；行终态回读纪律要求先把它们关闭断案，
    绝不留下永不收口的 running 行。返回收口行数。
    """
    async with get_session() as session:
        result = await session.execute(
            text(
                """
                UPDATE rd_agent_factor_backtests
                SET status = 'failed', error = :error, finished_at = now()
                WHERE batch_id = :batch_id
                  AND status = 'running'
                  AND finished_at IS NULL
                """
            ),
            {"batch_id": batch_id, "error": error},
        )
        return result.rowcount or 0


async def settle_orphan_single_runs(*, error: str) -> int:
    """重启恢复：把**无批次归属**的遗留 running 单跑收口为 failed。

    单跑（``POST /single``）的兜底在 ``_run_single`` 的 try/except——进程活着
    则必有终态；能留下 running 的唯一路径是**引擎进程死亡**（重启/被杀），此
    时子进程已随旧进程消亡，行是纯孤儿。收口策略：标 failed + 错误原文，
    **不自动重试**（单跑是用户一次性动作，UI 上可直接重派；「恰一次」重试语义
    只属于批次单元，见 ``batch._pending_units``）。返回收口行数。
    """
    async with get_session() as session:
        result = await session.execute(
            text(
                """
                UPDATE rd_agent_factor_backtests
                SET status = 'failed', error = :error, finished_at = now()
                WHERE batch_id IS NULL
                  AND status = 'running'
                  AND finished_at IS NULL
                """
            ),
            {"error": error},
        )
        return result.rowcount or 0


def _unpack(item: dict[str, Any]) -> dict[str, Any]:
    """JSONB 列解包（asyncpg 可能给 dict 或 str）；``metrics`` 键名与旧接口一致。"""
    for raw_key, out_key in (("metrics_json", "metrics"), ("params_json", "params")):
        raw = item.pop(raw_key, None)
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                raw = None
        item[out_key] = raw if isinstance(raw, dict) else {}
    return item
