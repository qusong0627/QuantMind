"""热集构建服务（T-P6-06）：全用户持仓并集 ∪ 候选池 ∪ 异动池 → Redis 热集集合。

- **持仓**：遍历交易 Redis 的 ``simulation:account:*``（与对账同一先例），取各账户持仓 symbol；
- **候选**：``engine_signal_scores`` 最新交易日、CN 口径、按 fusion_score 降序 TopN；
- **异动**：T-P6-14 识别引擎接口占位（现返回空，接口已留）；
- **输出**：远端行情 Redis 集合 ``qm:hot_set:symbols``（与 T-P6-02 订阅 worker 同源同键）+
  ``:meta`` 构建元数据（built_at/统计，运维可查）；**原子替换**（tmp 键 + RENAME，
  避免订阅侧读到半量集合）；空源 → 空集替换（过期标的退订）。
- 失败隔离：任一源失败如实记录在 report（不阻断其余源）；循环 worker 心跳入调度注册表。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from backend.shared import hot_set as hot_set_pure
from backend.shared.tdx_aidata import config as tdx_config

logger = logging.getLogger(__name__)

DEFAULT_CANDIDATE_TOP_N = 500


class HotSetBuilder:
    def __init__(
        self,
        *,
        hot_set_key: str | None = None,
        cap: int | None = None,
        candidate_top_n: int = DEFAULT_CANDIDATE_TOP_N,
        positions_redis=None,
        output_redis_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.hot_set_key = hot_set_key or tdx_config.hot_set_key()
        self.cap = int(cap if cap is not None else os.getenv("QM_HOT_SET_CAP", "2000"))
        self.candidate_top_n = int(candidate_top_n)
        self._positions_redis = positions_redis
        self._output_redis_factory = output_redis_factory or self._default_output_factory

    @staticmethod
    def _default_output_factory():
        from backend.shared.remote_quote_config import make_sync_client

        client = make_sync_client()
        if client is None:
            raise RuntimeError("远端行情 Redis 未配置/已禁用")
        return client

    def _trade_redis(self):
        if self._positions_redis is not None:
            return self._positions_redis
        from backend.services.trade_shared.redis_client import redis_client as trade_redis

        if trade_redis.client is None:
            trade_redis.connect()
        return trade_redis

    # ── 源采集 ──────────────────────────────────────────────────────

    def _collect_positions(self, tenant_filter: str | None) -> tuple[list[str], str | None]:
        """扫描模拟账户（全用户并集）→ 持仓 symbol 列表。"""
        symbols: list[str] = []
        try:
            client = self._trade_redis()
            if client.client is None:
                return [], "交易 Redis 不可用"
            keys = list(client.client.scan_iter(match="simulation:account:*", count=500))
            for raw_key in keys:
                key = str(raw_key)
                parts = key.split(":")
                if len(parts) < 4:
                    continue
                if tenant_filter and parts[2] != tenant_filter:
                    continue
                raw = client.client.get(key)
                if not raw:
                    continue
                try:
                    payload = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                for sym, pos in (payload.get("positions") or {}).items():
                    try:
                        if float((pos or {}).get("volume") or 0) <= 0:
                            continue
                    except (TypeError, ValueError):
                        continue
                    symbols.append(sym)
            return symbols, None
        except Exception as exc:  # noqa: BLE001
            return symbols, f"持仓采集失败: {exc}"

    async def _collect_candidates(self, tenant_filter: str | None) -> tuple[list[str], str | None]:
        """当日推理 TopN（最新交易日、CN 口径、fusion_score 降序、截面去重）。"""
        from sqlalchemy import text

        from backend.shared.database_manager_v2 import get_session

        tenant_clause = "AND tenant_id = :tid " if tenant_filter else ""
        sql = (
            "SELECT symbol FROM ("
            "  SELECT DISTINCT ON (symbol) symbol, fusion_score "
            "  FROM engine_signal_scores "
            "  WHERE trade_date = (SELECT max(trade_date) FROM engine_signal_scores "
            f"                     WHERE COALESCE(market,'CN')='CN' {tenant_clause}) "
            f"  AND COALESCE(market,'CN')='CN' {tenant_clause}"
            "  ORDER BY symbol, fusion_score DESC"
            ") ranked ORDER BY fusion_score DESC LIMIT :n"
        )
        params: dict[str, Any] = {"n": self.candidate_top_n}
        if tenant_filter:
            params["tid"] = tenant_filter
        try:
            async with get_session(read_only=True) as session:
                rows = (await session.execute(text(sql), params)).fetchall()
            return [str(r[0]) for r in rows if r[0]], None
        except Exception as exc:  # noqa: BLE001
            return [], f"候选采集失败: {exc}"

    def _collect_anomalies(self) -> tuple[list[str], str | None]:
        """异动池：T-P6-14 识别引擎接线位（现为空）。"""
        return [], None

    # ── 构建与写入 ──────────────────────────────────────────────────

    async def build_once(self, *, tenant_filter: str | None = None) -> dict[str, Any]:
        positions, pos_err = await asyncio.to_thread(self._collect_positions, tenant_filter)
        candidates, cand_err = await self._collect_candidates(tenant_filter)
        anomalies, anom_err = self._collect_anomalies()

        composed = hot_set_pure.compose_hot_set(
            positions=positions, anomalies=anomalies, candidates=candidates, cap=self.cap
        )
        report: dict[str, Any] = {
            **composed["stats"],
            "built_at": datetime.now(timezone.utc).isoformat(),
            "sources": {
                "positions": len(positions),
                "candidates": len(candidates),
                "anomalies": len(anomalies),
            },
            "errors": [e for e in (pos_err, cand_err, anom_err) if e],
        }
        await asyncio.to_thread(self._write, composed["symbols"], report)
        if report["errors"]:
            logger.warning("hot_set 构建部分源失败: %s", report["errors"])
        logger.info(
            "hot_set built kept=%s total=%s truncated=%s skipped=%s",
            report["kept"], report["total"], report["truncated"], report["skipped"],
        )
        return report

    def _write(self, symbols: list[str], report: dict[str, Any]) -> None:
        client = self._output_redis_factory()
        tmp_key = f"{self.hot_set_key}:tmp:{uuid.uuid4().hex[:8]}"
        try:
            pipe = client.pipeline(transaction=True)
            if symbols:
                # 原子替换：旧集合在被 RENAME 覆盖前始终可读，订阅侧不会看到半量集合
                pipe.sadd(tmp_key, *symbols)
                pipe.rename(tmp_key, self.hot_set_key)
            else:
                # 空集：Redis 空集合自动消亡，直接删目标键（SMEMBERS 缺键 == 空集，语义等价）
                pipe.delete(self.hot_set_key)
            pipe.delete(f"{self.hot_set_key}:meta")
            pipe.hset(
                f"{self.hot_set_key}:meta",
                mapping={
                    "built_at": str(report.get("built_at") or ""),
                    "total": str(report.get("total")),
                    "kept": str(report.get("kept")),
                    "truncated": str(report.get("truncated")),
                    "skipped": str(report.get("skipped")),
                    "sources": json.dumps(report.get("sources") or {}, ensure_ascii=False),
                },
            )
            pipe.execute()
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


async def run_hot_set_builder_worker() -> None:
    """常驻热集构建循环（trade 服务内注册；间隔/开关 env 可配）。"""
    interval = max(10, int(os.getenv("QM_HOT_SET_BUILD_S", "60")))
    builder = HotSetBuilder()
    logger.info(
        "hot_set builder started interval=%ss key=%s cap=%s top_n=%s",
        interval, builder.hot_set_key, builder.cap, builder.candidate_top_n,
    )
    while True:
        try:
            from backend.shared.scheduler_registry import heartbeat as _sched_heartbeat

            _sched_heartbeat("hot_set_builder")
        except Exception:  # noqa: BLE001
            pass
        try:
            await builder.build_once()
        except Exception as exc:  # noqa: BLE001 - 循环永不退出
            logger.error("hot_set builder cycle failed: %s", exc, exc_info=True)
        await asyncio.sleep(interval)
