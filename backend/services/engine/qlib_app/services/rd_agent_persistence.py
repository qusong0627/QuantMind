"""RD-Agent 因子持久化 — 创建和管理 rd_agent_factors 表"""

import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import text

from backend.shared.database_manager_v2 import get_session

logger = logging.getLogger(__name__)


class RDAgentFactorPersistence:
    """管理 RD-Agent 生成的因子数据，供 QuantMind 回测读取共享"""

    async def ensure_tables(self) -> None:
        """确保 rd_agent_factors 表存在（含历史表向后兼容的列）"""
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
        """
        async with get_session() as session:
            for s in [x.strip() for x in stmt.split(";") if x.strip()]:
                await session.execute(text(s))
        logger.info("rd_agent_factors table ensured")

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
