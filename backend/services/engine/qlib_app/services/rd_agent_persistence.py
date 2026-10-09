"""RD-Agent 因子持久化 — 创建和管理 rd_agent_factors 表"""

import json
import logging
from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy import text

from backend.shared.database_manager_v2 import get_session

logger = logging.getLogger(__name__)


class RDAgentFactorPersistence:
    """管理 RD-Agent 生成的因子数据，供 QuantMind 回测读取共享"""

    async def ensure_tables(self) -> None:
        """确保 rd_agent_factors 与其回测历史台账 rd_agent_factor_backtests 存在
        （含历史表向后兼容的列）"""
        stmt = """
        CREATE TABLE IF NOT EXISTS rd_agent_factors (
          factor_id TEXT PRIMARY KEY,
          factor_name TEXT NOT NULL,
          factor_code TEXT,
          status TEXT NOT NULL DEFAULT 'pending',
          ic_value DOUBLE PRECISION,
          sharpe_ratio DOUBLE PRECISION,
          annual_return DOUBLE PRECISION,
          max_drawdown DOUBLE PRECISION,
          user_id TEXT,
          metadata_json JSONB,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        ALTER TABLE rd_agent_factors ADD COLUMN IF NOT EXISTS user_id TEXT;
        ALTER TABLE rd_agent_factors ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();
        ALTER TABLE rd_agent_factors ADD COLUMN IF NOT EXISTS market TEXT;
        ALTER TABLE rd_agent_factors ADD COLUMN IF NOT EXISTS universe TEXT;
        ALTER TABLE rd_agent_factors ADD COLUMN IF NOT EXISTS rank_ic DOUBLE PRECISION;
        ALTER TABLE rd_agent_factors ADD COLUMN IF NOT EXISTS factor_formulation TEXT;
        ALTER TABLE rd_agent_factors ADD COLUMN IF NOT EXISTS data_source TEXT;
        ALTER TABLE rd_agent_factors ADD COLUMN IF NOT EXISTS date_range TEXT;
        CREATE INDEX IF NOT EXISTS idx_rd_agent_factors_user_id ON rd_agent_factors(user_id);
        CREATE INDEX IF NOT EXISTS idx_rd_agent_factors_market ON rd_agent_factors(market);
        CREATE INDEX IF NOT EXISTS idx_rd_agent_factors_universe ON rd_agent_factors(universe);
        CREATE TABLE IF NOT EXISTS rd_agent_factor_backtests (
          run_id TEXT PRIMARY KEY,
          factor_id TEXT NOT NULL,
          factor_name TEXT,
          user_id TEXT,
          status TEXT NOT NULL DEFAULT 'running'
            CHECK (status IN ('running','completed','failed','cancelled')),
          market TEXT,
          universe TEXT,
          data_source TEXT,
          date_range TEXT,
          ic_value DOUBLE PRECISION,
          rank_ic DOUBLE PRECISION,
          icir DOUBLE PRECISION,
          rank_icir DOUBLE PRECISION,
          sharpe_ratio DOUBLE PRECISION,
          annual_return DOUBLE PRECISION,
          max_drawdown DOUBLE PRECISION,
          metrics_json JSONB,
          error TEXT,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          finished_at TIMESTAMPTZ
        );
        CREATE INDEX IF NOT EXISTS idx_rd_agent_factor_backtests_factor ON rd_agent_factor_backtests(factor_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_rd_agent_factor_backtests_open ON rd_agent_factor_backtests(status) WHERE finished_at IS NULL;
        """
        async with get_session() as session:
            for s in [x.strip() for x in stmt.split(";") if x.strip()]:
                await session.execute(text(s))
            # 状态词表约束：老表（先于本约束创建）补挂 CHECK。DO 块自带分号，
            # 不能进上面的按分号拆分循环；按 pg_constraint 判存在，幂等。
            await session.execute(
                text("""
                DO $$
                BEGIN
                  IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'rd_agent_factor_backtests_status_check'
                      AND conrelid = 'rd_agent_factor_backtests'::regclass
                  ) THEN
                    ALTER TABLE rd_agent_factor_backtests
                      ADD CONSTRAINT rd_agent_factor_backtests_status_check
                      CHECK (status IN ('running','completed','failed','cancelled'));
                  END IF;
                END $$;
                """)
            )
        logger.info("rd_agent_factors tables ensured")

        # 迁移 metadata_json 中的字段到专列
        await self.migrate_metadata_to_columns()

    async def migrate_metadata_to_columns(self) -> None:
        """将 metadata_json 中的 market/formulation 等字段提取到专列。幂等执行。"""
        try:
            async with get_session() as session:
                await session.execute(text("""
                    UPDATE rd_agent_factors
                    SET market = metadata_json::jsonb ->> 'market'
                    WHERE market IS NULL
                      AND metadata_json IS NOT NULL
                      AND metadata_json::jsonb ->> 'market' IS NOT NULL
                """))
                await session.execute(text("""
                    UPDATE rd_agent_factors
                    SET factor_formulation = metadata_json::jsonb ->> 'factor_formulation'
                    WHERE factor_formulation IS NULL
                      AND metadata_json IS NOT NULL
                      AND metadata_json::jsonb ->> 'factor_formulation' IS NOT NULL
                """))
            logger.info("rd_agent_factors metadata migration completed")
        except Exception as exc:
            logger.warning("rd_agent_factors metadata migration failed (non-fatal): %s", exc)

    async def save_factor(
        self,
        factor_id: str,
        factor_name: str,
        factor_code: str,
        user_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        market: str | None = None,
        universe: str | None = None,
        factor_formulation: str | None = None,
        data_source: str | None = None,
    ) -> None:
        """保存 RD-Agent 生成的因子"""
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)
        async with get_session() as session:
            await session.execute(
                text("""
                    INSERT INTO rd_agent_factors
                        (factor_id, factor_name, factor_code, status, user_id, metadata_json,
                         market, universe, factor_formulation, data_source)
                    VALUES
                        (:factor_id, :factor_name, :factor_code, 'pending', :user_id, :metadata_json,
                         :market, :universe, :factor_formulation, :data_source)
                    ON CONFLICT (factor_id) DO UPDATE SET
                        factor_name = EXCLUDED.factor_name,
                        factor_code = EXCLUDED.factor_code,
                        updated_at = now()
                    """),
                {
                    "factor_id": factor_id,
                    "factor_name": factor_name,
                    "factor_code": factor_code,
                    "user_id": user_id,
                    "metadata_json": meta_json,
                    "market": market,
                    "universe": universe,
                    "factor_formulation": factor_formulation,
                    "data_source": data_source,
                },
            )

    async def list_factors(
        self,
        user_id: str | None = None,
        status: str | None = None,
        market: str | None = None,
        universe: str | None = None,
        limit: int = 50,
        task_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """列出因子（支持按状态、用户、市场、宇宙、来源任务过滤）"""
        conditions = []
        params: dict[str, Any] = {"limit": limit}
        if user_id:
            conditions.append("user_id = :user_id")
            params["user_id"] = user_id
        if status:
            conditions.append("status = :status")
            params["status"] = status
        if market:
            conditions.append("market = :market")
            params["market"] = market
        if universe:
            conditions.append("universe = :universe")
            params["universe"] = universe
        if task_id:
            conditions.append("metadata_json->>'task_id' = :task_id")
            params["task_id"] = task_id

        where = " AND ".join(conditions) if conditions else "1=1"
        async with get_session(read_only=True) as session:
            rows = await session.execute(
                text(f"""
                    SELECT factor_id, factor_name, factor_code, status, ic_value, sharpe_ratio,
                           annual_return, max_drawdown, rank_ic, user_id, market, universe,
                           factor_formulation, data_source, date_range, metadata_json, created_at, updated_at
                    FROM rd_agent_factors
                    WHERE {where}
                    ORDER BY created_at DESC
                    LIMIT :limit
                    """),
                params,
            )
            data = rows.mappings().all()
            results = []
            for r in data:
                item = dict(r)
                raw_meta = item.pop("metadata_json", None)
                if isinstance(raw_meta, dict):
                    item["metadata"] = raw_meta
                elif isinstance(raw_meta, str):
                    try:
                        item["metadata"] = json.loads(raw_meta)
                    except Exception:
                        item["metadata"] = {}
                else:
                    item["metadata"] = {}
                results.append(item)
            return results

    async def get_factor(self, factor_id: str) -> dict[str, Any] | None:
        """获取单个因子详情"""
        async with get_session(read_only=True) as session:
            row = await session.execute(
                text("""
                    SELECT factor_id, factor_name, factor_code, status, ic_value, sharpe_ratio,
                           annual_return, max_drawdown, rank_ic, user_id, market, universe,
                           factor_formulation, data_source, date_range, metadata_json, created_at, updated_at
                    FROM rd_agent_factors
                    WHERE factor_id = :factor_id
                    """),
                {"factor_id": factor_id},
            )
            r = row.mappings().first()
            if not r:
                return None
            item = dict(r)
            raw_meta = item.pop("metadata_json", None)
            if isinstance(raw_meta, dict):
                item["metadata"] = raw_meta
            elif isinstance(raw_meta, str):
                try:
                    item["metadata"] = json.loads(raw_meta)
                except Exception:
                    item["metadata"] = {}
            else:
                item["metadata"] = {}
            return item

    async def update_factor_metrics(
        self,
        factor_id: str,
        status: str | None = None,
        ic_value: float | None = None,
        sharpe_ratio: float | None = None,
        annual_return: float | None = None,
        max_drawdown: float | None = None,
        rank_ic: float | None = None,
        icir: float | None = None,
        rank_icir: float | None = None,
        universe: str | None = None,
        date_range: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """更新因子的回测指标，metadata 与已有值合并而非覆盖

        icir / rank_icir 不是表字段，写入 metadata_json（前端从 metadata 读取）。
        """
        fields: dict[str, Any] = {"updated_at": datetime.now()}
        if status is not None:
            fields["status"] = status
        if ic_value is not None:
            fields["ic_value"] = ic_value
        if sharpe_ratio is not None:
            fields["sharpe_ratio"] = sharpe_ratio
        if annual_return is not None:
            fields["annual_return"] = annual_return
        if max_drawdown is not None:
            fields["max_drawdown"] = max_drawdown
        if rank_ic is not None:
            fields["rank_ic"] = rank_ic
        if universe is not None:
            fields["universe"] = universe
        if date_range is not None:
            fields["date_range"] = date_range
        metric_meta: dict[str, Any] = {}
        if icir is not None:
            metric_meta["icir"] = icir
        if rank_icir is not None:
            metric_meta["rank_icir"] = rank_icir
        if metadata is not None or metric_meta or status == "completed":
            # Merge with existing metadata to preserve task_id, market, etc.
            async with get_session(read_only=True) as session:
                row = await session.execute(
                    text("SELECT metadata_json FROM rd_agent_factors WHERE factor_id = :factor_id"),
                    {"factor_id": factor_id},
                )
                existing = row.scalar()
            merged = {}
            if existing:
                try:
                    merged = json.loads(existing) if isinstance(existing, str) else (existing or {})
                except Exception:
                    merged = {}
            merged.update(metadata or {})
            merged.update(metric_meta)
            if status == "completed":
                # 回测成功时清掉历史失败原因，否则前端会把上一次的报错当成当前状态
                merged["backtest_error"] = None
            fields["metadata_json"] = json.dumps(merged, ensure_ascii=False)

        set_clause = ", ".join(f"{k} = :{k}" for k in fields)
        async with get_session() as session:
            await session.execute(
                text(f"""
                    UPDATE rd_agent_factors SET {set_clause}
                    WHERE factor_id = :factor_id
                    """),
                {**fields, "factor_id": factor_id},
            )

    async def recover_stuck_factors(
        self, max_age_min: int = 15, target_status: str = "backtesting"
    ) -> int:
        """恢复卡死超过 max_age_min 分钟的因子（默认 backtesting 状态）。

        把超时的 status 改成 failed，并在 metadata.backtest_error 写入原因。
        用于引擎进程崩溃 / 600s subprocess timeout 后清理。

        Returns: 受影响的行数。
        """
        async with get_session() as session:
            result = await session.execute(
                text("""
                    UPDATE rd_agent_factors
                    SET status = 'failed',
                        metadata_json = jsonb_set(
                            COALESCE(metadata_json, '{}'::jsonb),
                            '{backtest_error}',
                            to_jsonb('timeout_or_engine_crash'::text),
                            true
                        ),
                        updated_at = now()
                    WHERE status = :s
                      AND updated_at < now() - (:max_age_min || ' minutes')::interval
                    """),
                {"s": target_status, "max_age_min": str(max_age_min)},
            )
            count = result.rowcount or 0
            if count:
                logger.warning(
                    "Recovered %d stuck factors (status=%s, older than %d min)",
                    count, target_status, max_age_min,
                )
            return count

    # ==================== 回测历史台账（一次运行一行） ====================
    # 用户诉求「每次单个因子回测的历史数据，后面好对比」：rd_agent_factors
    # 每因子只有一行，指标列被每次回测覆盖；本表按运行追加，永不覆盖。

    async def start_backtest_run(
        self,
        factor_id: str,
        *,
        factor_name: str | None = None,
        user_id: str | None = None,
        market: str | None = None,
        universe: str | None = None,
        data_source: str | None = None,
    ) -> str:
        """登记一次因子回测运行（status='running'，finished_at 留空）。返回 run_id。"""
        run_id = uuid4().hex
        async with get_session() as session:
            await session.execute(
                text("""
                    INSERT INTO rd_agent_factor_backtests
                        (run_id, factor_id, factor_name, user_id, status,
                         market, universe, data_source)
                    VALUES
                        (:run_id, :factor_id, :factor_name, :user_id, 'running',
                         :market, :universe, :data_source)
                    """),
                {
                    "run_id": run_id,
                    "factor_id": factor_id,
                    "factor_name": factor_name,
                    "user_id": user_id,
                    "market": market,
                    "universe": universe,
                    "data_source": data_source,
                },
            )
        return run_id

    async def finish_backtest_run(
        self,
        run_id: str,
        status: Literal["completed", "failed", "cancelled"],
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
        """按 run_id 精确收口一次回测运行到终态（completed/failed/cancelled）。

        不能用「该因子最新未完结行」定位：取消端点会先放行重跑，旧后台任务
        延迟收尾时，启发式会把**新运行**的行收错、真结果永久丢失。run_id 由
        `start_backtest_run` 返回、随任务一路携带——谁开的运行谁收口。

        只改该 run_id 且 status='running' 且 finished_at IS NULL 的行：取消
        端点与后台任务可能先后收口，第二次调用幂等返回 False，已完结行绝不
        改写。时间戳一律由数据库 now() 生成（容器时钟偏移不影响耗时计算）。
        """
        if status not in ("completed", "failed", "cancelled"):
            raise ValueError(f"invalid terminal status: {status!r}")
        fields: dict[str, Any] = {"status": status}
        if ic_value is not None:
            fields["ic_value"] = ic_value
        if rank_ic is not None:
            fields["rank_ic"] = rank_ic
        if icir is not None:
            fields["icir"] = icir
        if rank_icir is not None:
            fields["rank_icir"] = rank_icir
        if sharpe_ratio is not None:
            fields["sharpe_ratio"] = sharpe_ratio
        if annual_return is not None:
            fields["annual_return"] = annual_return
        if max_drawdown is not None:
            fields["max_drawdown"] = max_drawdown
        if universe is not None:
            fields["universe"] = universe
        if data_source is not None:
            fields["data_source"] = data_source
        if date_range is not None:
            fields["date_range"] = date_range
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
                text(f"""
                    UPDATE rd_agent_factor_backtests
                    SET {set_clause}, finished_at = now()
                    WHERE run_id = :run_id
                      AND status = 'running'
                      AND finished_at IS NULL
                    """),
                params,
            )
            return (result.rowcount or 0) > 0

    async def list_backtest_runs(
        self, factor_id: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        """列出一个因子的历次回测（新→旧，一次运行一行）。

        metrics_json 解包为 ``metadata`` 键——与 ``get_factor`` 的行形状一致，
        前端两处共用同一套指标映射。
        """
        async with get_session(read_only=True) as session:
            rows = await session.execute(
                text("""
                    SELECT run_id, factor_id, factor_name, status, market, universe,
                           data_source, date_range, ic_value, rank_ic, icir, rank_icir,
                           sharpe_ratio, annual_return, max_drawdown, metrics_json,
                           error, created_at, finished_at
                    FROM rd_agent_factor_backtests
                    WHERE factor_id = :factor_id
                    ORDER BY created_at DESC, run_id DESC
                    LIMIT :limit
                    """),
                {"factor_id": factor_id, "limit": limit},
            )
            results = []
            for r in rows.mappings().all():
                item = dict(r)
                raw_meta = item.pop("metrics_json", None)
                if isinstance(raw_meta, dict):
                    item["metadata"] = raw_meta
                elif isinstance(raw_meta, str):
                    try:
                        item["metadata"] = json.loads(raw_meta)
                    except Exception:
                        item["metadata"] = {}
                else:
                    item["metadata"] = {}
                results.append(item)
            return results

    async def recover_stuck_backtest_runs(self, max_age_min: int = 15) -> int:
        """把陈旧未收口的回测运行收口为 failed/timeout_or_engine_crash。

        与 ``recover_stuck_factors`` 同口径（引擎进程崩溃 / 600s subprocess
        超时后遗留的 running 行），由启动钩子一并调用。

        Returns: 受影响的行数。
        """
        async with get_session() as session:
            result = await session.execute(
                text("""
                    UPDATE rd_agent_factor_backtests
                    SET status = 'failed',
                        error = 'timeout_or_engine_crash',
                        finished_at = now()
                    WHERE status = 'running'
                      AND finished_at IS NULL
                      AND created_at < now() - (:max_age_min || ' minutes')::interval
                    """),
                {"max_age_min": str(max_age_min)},
            )
            count = result.rowcount or 0
            if count:
                logger.warning(
                    "Recovered %d stuck backtest runs (older than %d min)",
                    count,
                    max_age_min,
                )
            return count
