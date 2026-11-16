"""热集构建服务（T-P6-06）：全用户持仓并集 ∪ 当日池 ∪ 候选 ∪ 异动池 → Redis 热集集合。

- **持仓**：模拟账户（交易 Redis ``simulation:account:*``，与对账同一先例）∪ **桥实盘/QMT
  真实持仓**（``real_positions.load_real_positions``，多券商并集）——量化实时推理必须覆盖
  真实持仓，否则「自己盘的票」只有恰好进了候选才被推理到（2026-10-08 实测：桥 4 只持仓
  仅 1 只在集）；
- **当日池**（2026-10-13 补）：``data/reports/stock_picks/{目标交易日}_picks.json`` ——
  **决策轮读的就是这份**（``decision_context_source.load_pool_doc``），而候选源是
  PG ``engine_signal_scores`` 的分数 TopN：两份名单**不相交**。池票不在热集 ⇒ 没有
  行情快照 ⇒ ``l3.stale_quote``（5s，fail-closed）全数拒单，当天那一批买单 22/22 全拒
  （实测 20261012：池 30 只，15 只不在热集 562）。池是全局产物、**不随租户过滤**。
  池文件缺席（北京 ~03:00/04:00 才产出）不是错误：记 ``pool_file=""``、``pool=0``，
  每轮（默认 60s）重读，文件出现即自动纳入；
- **候选**：``engine_signal_scores`` 最新交易日、CN 口径、按 fusion_score 降序 TopN；
- **异动**：T-P6-14 识别引擎接口占位（现返回空，接口已留）；
- **输出**：远端行情 Redis 集合 ``qm:hot_set:symbols``（与 T-P6-02 订阅 worker 同源同键）+
  ``:meta`` 构建元数据（built_at/统计/分片压力，运维可查）；**原子替换**（tmp 键 + RENAME，
  避免订阅侧读到半量集合）；空源 → 空集替换（过期标的退订）。
- **分片压力**：订阅侧 ``collector._shard_desired`` 按**字典序**硬截断到
  ``SDK_SUBSCRIBE_MAX``（100），排序与合成侧的优先级无关 ⇒ 热集逼近上限时被截掉的可能
  正是池票。此处如实算出每片应订数与余量并写进报告/meta，逼近上限时 WARNING。
- 失败隔离：任一源失败如实记录在 report（不阻断其余源）；循环 worker 心跳入调度注册表。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
import zlib
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from backend.shared import hot_set as hot_set_pure
from backend.shared.env_flags import env_flag
from backend.shared.tdx_aidata import config as tdx_config
from backend.shared.tdx_aidata.collector import SDK_SUBSCRIBE_MAX

logger = logging.getLogger(__name__)

DEFAULT_CANDIDATE_TOP_N = 500

#: 当日池并入开关（默认开：这是决策轮真的会下单的那份名单）。
POOL_FEATURE_ENV = "QM_HOT_SET_POOL"


class HotSetBuilder:
    def __init__(
        self,
        *,
        hot_set_key: str | None = None,
        cap: int | None = None,
        candidate_top_n: int = DEFAULT_CANDIDATE_TOP_N,
        positions_redis=None,
        output_redis_factory: Callable[[], Any] | None = None,
        real_positions_loader: Callable[[], Any] | None = None,
    ) -> None:
        self.hot_set_key = hot_set_key or tdx_config.hot_set_key()
        self.cap = int(cap if cap is not None else os.getenv("QM_HOT_SET_CAP", "2000"))
        self.candidate_top_n = int(candidate_top_n)
        self._positions_redis = positions_redis
        self._output_redis_factory = (
            output_redis_factory or self._default_output_factory
        )
        #: 桥实盘/QMT 持仓加载器（测试注入用；生产走 shared.real_positions）。
        self._real_positions_loader = real_positions_loader

    @staticmethod
    def _default_output_factory():
        from backend.shared.hot_set_store import make_hot_set_client

        return make_hot_set_client()

    def _trade_redis(self):
        if self._positions_redis is not None:
            return self._positions_redis
        from backend.services.trade_shared.redis_client import (
            redis_client as trade_redis,
        )

        if trade_redis.client is None:
            trade_redis.connect()
        return trade_redis

    # ── 源采集 ──────────────────────────────────────────────────────

    def _collect_positions(
        self, tenant_filter: str | None
    ) -> tuple[list[str], str | None]:
        """扫描模拟账户（全用户并集）→ 持仓 symbol 列表。"""
        symbols: list[str] = []
        try:
            client = self._trade_redis()
            if client.client is None:
                return [], "交易 Redis 不可用"
            keys = list(
                client.client.scan_iter(match="simulation:account:*", count=500)
            )
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

    async def _collect_real_positions(
        self, tenant_filter: str | None
    ) -> tuple[list[str], str | None]:
        """桥实盘/QMT 真实持仓（多券商并集，shared.real_positions 单一实现）。

        测试租户（``tenant_filter`` 非真实租户）**不并入**：真实账户属于
        ``QM_HOT_SET_REAL_TENANT``（默认 default），混进 t-* 测试构建等于把真实持仓
        写进别人的断言里（也违背「按租户隔离」语义）。
        """
        real_tenant = (
            os.getenv("QM_HOT_SET_REAL_TENANT", "default") or "default"
        ).strip()
        if tenant_filter and tenant_filter != real_tenant:
            return [], None
        try:
            if self._real_positions_loader is not None:
                positions, _meta = await self._real_positions_loader()
            else:
                from backend.shared.real_positions import load_real_positions
                from backend.shared.simulation_account_keys import (
                    resolve_db_account_user,
                )

                user = resolve_db_account_user("QM_HOT_SET_REAL_USER")
                positions, _meta = await load_real_positions(real_tenant, user)
            if not isinstance(positions, dict):
                return [], None
            return [str(sym) for sym in positions], None
        except Exception as exc:  # noqa: BLE001 - 实盘源失败不阻断其余源
            return [], f"实盘持仓采集失败: {exc}"

    async def _collect_candidates(
        self, tenant_filter: str | None
    ) -> tuple[list[str], str | None]:
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

    ANOMALY_POOL_MAX = 100  # 异动池并入热集的条数上限（防单轮异动风暴挤占候选）

    def _collect_anomalies(self) -> tuple[list[str], str | None]:
        """异动池：读识别引擎（T-P6-14）近 1h 异动标的（有界；读失败隔离为告警不阻断）。"""
        try:
            from backend.shared.anomaly_contract import read_recent_anomaly_symbols

            return read_recent_anomaly_symbols(limit=self.ANOMALY_POOL_MAX)[
                : self.ANOMALY_POOL_MAX
            ], None
        except Exception as exc:  # noqa: BLE001 - 异动源失败不阻断构建
            return [], f"异动池采集失败: {exc}"

    def _collect_pool(
        self, day: str | None = None
    ) -> tuple[list[str], str | None, str]:
        """当日候选池（**决策轮同源** ``load_pool_doc``）→ 标的清单。

        返回 ``(symbols, error, pool_file)``。三类结果必须可区分：

        * 池文件不在（凌晨未产出、周末/节假日）→ ``([], None, "")``——**不是错误**：
          每轮 60s 重读，文件出现即自动纳入；
        * 文件在但读不动/结构异常 → ``([], "候选池采集失败: …", path)`` 如实告警；
        * 正常 → ``(symbols, None, path)``。

        池是**全局产物**（全市场选股结果），不随 ``tenant_filter`` 过滤：过滤会让
        测试租户的构建看不到池票，而订阅是全局的、只认 ``qm:hot_set:symbols``。
        出口已是后缀式（``load_pool_doc`` 内部统一走 ``_to_suffix``），与
        ``compose_hot_set`` 的归一口径一致。
        """
        if not env_flag(POOL_FEATURE_ENV, default=True):
            return [], None, ""
        try:
            from backend.shared.decision_context_source import load_pool_doc, now_cn
        except Exception as exc:  # noqa: BLE001 - 依赖不可得时静默降级会掩盖问题
            return [], f"候选池模块不可用: {exc}", ""
        day_str = str(day or "").strip() or now_cn().strftime("%Y%m%d")
        try:
            doc = load_pool_doc(day_str)
        except Exception as exc:  # noqa: BLE001 - 池读失败不阻断其余源
            return [], f"候选池采集失败: {exc}", ""
        if doc is None:
            return [], None, ""
        symbols: list[str] = []
        for row in doc.rows:
            code = str(getattr(row, "code", "") or "").strip()
            if code:
                symbols.append(code)
        return symbols, None, str(doc.source or "")

    @staticmethod
    def _shard_pressure(symbols: list[str], shard_count: int) -> dict[str, Any]:
        """热集按订阅分片算法的压力画像（余量 = ``SDK_SUBSCRIBE_MAX - 最大片）。

        订阅侧 ``collector._shard_desired`` 对**每片**独立做 ``desired[:100]`` 硬截断，
        而分片函数是 ``crc32(symbol) % shard_count``（与 compose 的优先级无关）。
        也就是说：热集总量没超、但有某一**片**超过 100 时，静默被截掉的是该片字典序
        靠后的标的——可能是池票。合成侧无法改变订阅侧的削发顺序，只能**如实暴露**。
        """
        n = max(1, int(shard_count or 1))
        counts = [0] * n
        for sym in symbols:
            counts[zlib.crc32(str(sym).encode("utf-8")) % n] += 1
        shard_max = max(counts) if counts else 0
        return {
            "shard_count": n,
            "shard_max": shard_max,
            "sdk_cap": SDK_SUBSCRIBE_MAX,
            "headroom": SDK_SUBSCRIBE_MAX - shard_max,
            "shards": counts,
        }

    # ── 构建与写入 ──────────────────────────────────────────────────

    async def build_once(self, *, tenant_filter: str | None = None) -> dict[str, Any]:
        positions, pos_err = await asyncio.to_thread(
            self._collect_positions, tenant_filter
        )
        real_positions, real_err = await self._collect_real_positions(tenant_filter)
        pool, pool_err, pool_file = await asyncio.to_thread(self._collect_pool)
        candidates, cand_err = await self._collect_candidates(tenant_filter)
        anomalies, anom_err = self._collect_anomalies()

        composed = hot_set_pure.compose_hot_set(
            positions=[*positions, *real_positions],
            # 池次之：池票是「今天真的要下单」的，不能先被候选挤掉（见模块 docstring）
            pool=pool,
            anomalies=anomalies,
            candidates=candidates,
            indexes=hot_set_pure.REGIME_INDEXES,  # T-P6-13：regime 常驻指数（不占 cap）
            cap=self.cap,
        )
        shard_count = 1
        try:
            shard_count = int(tdx_config.shard_count())
        except Exception as exc:  # noqa: BLE001 - 配置读不到按单片算（余量只会更保守）
            logger.warning("读取订阅分片数失败，按单片估算压力: %s", exc)
        pressure = self._shard_pressure(composed["symbols"], shard_count)
        report: dict[str, Any] = {
            **composed["stats"],
            "built_at": datetime.now(timezone.utc).isoformat(),
            "sources": {
                "positions": len(positions) + len(real_positions),
                "positions_sim": len(positions),
                "positions_real": len(real_positions),
                "pool": len(pool),
                "candidates": len(candidates),
                "anomalies": len(anomalies),
                "indexes": len(hot_set_pure.REGIME_INDEXES),
            },
            # 池的**出处**（文件路径）如实带上：``pool=0`` 有两种含义——「今天还没有池」
            # 与「池读失败」，只有文件名能把它们分开（另一条区分在 errors 里）。
            "pool_file": pool_file,
            "shard_pressure": pressure,
            "errors": [
                e for e in (pos_err, real_err, pool_err, cand_err, anom_err) if e
            ],
        }
        await asyncio.to_thread(self._write, composed["symbols"], report)
        if report["errors"]:
            logger.warning("hot_set 构建部分源失败: %s", report["errors"])
        if pressure["headroom"] <= 0:
            # 逼近 SDK 单订阅上限 ⇒ 该片会被字典序硬截断（可能正好截掉池票）。
            # 这是**运维动作**问题（上调 QM_SUB_SHARDS），不是构建能自愈的。
            logger.warning(
                "hot_set 分片压力逼近上限：最大片 %s/%s（cap %s），可能截断池票，"
                "请上调订阅分片数（QM_SUB_SHARDS）",
                pressure["shard_max"],
                pressure["shard_count"],
                pressure["sdk_cap"],
            )
        logger.info(
            "hot_set built kept=%s total=%s truncated=%s skipped=%s pool=%s shard_max=%s",
            report["kept"],
            report["total"],
            report["truncated"],
            report["skipped"],
            len(pool),
            pressure["shard_max"],
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
                    "sources": json.dumps(
                        report.get("sources") or {}, ensure_ascii=False
                    ),
                    "pool_file": str(report.get("pool_file") or ""),
                    "shard_pressure": json.dumps(
                        report.get("shard_pressure") or {}, ensure_ascii=False
                    ),
                },
            )
            pipe.execute()
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


def mirror_hot_set_to_qmt(
    symbols: list[str] | None = None,
    *,
    client_factory: Callable[[], Any] | None = None,
    ttl_s: int = 3 * 86400,
    key: str = "qm:qmt:hot_set",
) -> int:
    """把本地热集镜像到 **QMT 桥 Redis（db5）** 的 ``qm:qmt:hot_set``。

    用途：伴生桥的 ``sweep`` 备源用 ``get_full_tick`` 分批轮询这个集合
    （listener 线程，不会卡 adjust 主线程）；桥的 tap 一律 ``subscribe_whole_quote``
    大列表会把主线程挂死（2026-11-16 实测 590 只直接 TransportTimeout），所以
    590 只热集只能走 sweep，不能走 tap。

    best-effort：任何失败只告警，不影响热集主集合构建。返回镜像条数。
    """
    try:
        if symbols is None:
            from backend.shared.hot_set_store import read_hot_set_symbols

            symbols = read_hot_set_symbols()
        clean = [str(s) for s in (symbols or []) if str(s)]
        if client_factory is None:
            import redis as _redis

            client_factory = lambda: _redis.Redis(  # noqa: E731
                host=os.getenv("REDIS_HOST") or "redis",
                port=int(os.getenv("REDIS_PORT", "6379")),
                password=os.getenv("REDIS_PASSWORD") or None,
                db=int(os.getenv("QM_QMT_HOT_SET_DB", "5")),
                decode_responses=True,
                socket_connect_timeout=3,
                socket_timeout=5,
            )
        client = client_factory()
        try:
            tmp_key = f"{key}:tmp:{uuid.uuid4().hex[:8]}"
            pipe = client.pipeline(transaction=True)
            if clean:
                pipe.sadd(tmp_key, *clean)
                pipe.rename(tmp_key, key)
            else:
                pipe.delete(key)
            pipe.expire(key, int(ttl_s))
            pipe.execute()
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
        return len(clean)
    except Exception as exc:  # noqa: BLE001 - 备源镜像失败不影响主热集
        logger.warning("QMT 热集镜像失败: %s", exc)
        return 0


async def run_hot_set_builder_worker() -> None:
    """常驻热集构建循环（trade 服务内注册；间隔/开关 env 可配）。"""
    interval = max(10, int(os.getenv("QM_HOT_SET_BUILD_S", "60")))
    builder = HotSetBuilder()
    logger.info(
        "hot_set builder started interval=%ss key=%s cap=%s top_n=%s",
        interval,
        builder.hot_set_key,
        builder.cap,
        builder.candidate_top_n,
    )
    while True:
        try:
            from backend.shared.scheduler_registry import heartbeat as _sched_heartbeat

            _sched_heartbeat("hot_set_builder")
        except Exception:  # noqa: BLE001
            pass
        try:
            await builder.build_once()
            mirrored = await asyncio.to_thread(mirror_hot_set_to_qmt)
            if mirrored:
                logger.info("hot_set 已镜像到 QMT db5: %s 只", mirrored)
        except Exception as exc:  # noqa: BLE001 - 循环永不退出
            logger.error("hot_set builder cycle failed: %s", exc, exc_info=True)
        await asyncio.sleep(interval)
