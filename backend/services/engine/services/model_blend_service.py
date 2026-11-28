"""组合选股服务：成员解析、榜单、历史标注、纸面调仓计划。

分层：

- 纯计算在 ``blend_engine``（打分）与 ``blend_plan``（调仓裁定），本模块不重复实现；
- 本模块只做 IO 编排：取 run → 取分 → 折叠符号 → 调纯函数 → 补名称/行情 → 落库。

**符号口径**：``engine_signal_scores.symbol`` 实测并存三种写法（``600519`` /
``SH600519`` / ``600519.SH``，不同训练链写入）。组合必须先把成员分数折叠到
统一的前缀式再聚合，否则跨成员覆盖率会被写成 0（「同票不同码」）。
折叠规则与 ``StockCodeUtil.to_prefix`` 逐条对齐，SQL 侧表达式由
``test_blend_service`` 的集成用例对拍。

**边界**：只读写 ``qm_model_blend_*`` 纸面台账，不触碰 orders/sim_trades。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import time
from bisect import bisect_left, bisect_right
from datetime import date, timedelta
from typing import Any
from collections.abc import Mapping, Sequence

from sqlalchemy import text

from backend.services.engine.inference.blend_engine import (
    BlendResult,
    BlendRow,
    combine_member_ranks,
    dispersion_band,
    position_of,
    rank_pct_from_rank,
)
from backend.services.engine.inference import portfolio_metrics as metrics
from backend.services.engine.inference.period_performance import simulate_period
from backend.services.engine.inference.rolling_performance import (
    DEFAULT_ADV_LOOKBACK_DAYS,
    DEFAULT_ADV_PARTICIPATION,
    DEFAULT_MIN_ADV_DAYS,
    DEFAULT_MIN_ADV_YI,
    YI,
    cell_stats,
    compute_rolling_performance,
    simulate_daily_rotation,
)
from backend.services.engine.inference.blend_plan import (
    DEFAULT_LOOKBACK,
    PaperPosition,
    PlanConfig,
    build_rebalance_plan,
    config_hash,
    consecutive_days_in_topk,
    mean_score,
    positions_hash,
)
from backend.services.engine.services import combo_auto
from backend.services.engine.services.blend_pred_source import BlendPredSource
from backend.services.engine.services.model_blend_persistence import (
    ModelBlendPersistence,
)
from backend.shared.database_manager_v2 import get_session
from backend.shared.stock_utils import StockCodeUtil, build_prefix_symbol_sql

logger = logging.getLogger(__name__)

MIN_MEMBERS = 2
#: T+5 全组模板实有 24 个同签名模型，上限必须容得下整组模板
MAX_MEMBERS = 30
CN_MARKET = "CN"
#: 周期收益：区间上限（交易日）。300 ≈ 14 个月，覆盖「最近半年/近一年」。
MAX_PERIOD_TRADING_DAYS = 300
#: pred 测试集路径的上限：覆盖 414 天全量测试集
MAX_PRED_PERIOD_TRADING_DAYS = 420
#: median/trimmed 走慢路（逐行折叠符号），区间收紧，避免同步接口超时。
MAX_NON_MEAN_PERIOD_DAYS = 120
#: pred 路径的 median/trimmed 在 DuckDB 内聚合，可放宽到全测试集
MAX_PRED_NON_MEAN_PERIOD_DAYS = 420
#: 基准指数与 MA20 需要的回看自然日
BENCHMARK_SYMBOL = "000300.SH"
BENCHMARK_LOOKBACK_DAYS = 40
#: 评分覆盖率下限：低于 30% 拒绝计算，30~80% 标 partial。
MIN_PERIOD_COVERAGE = 0.30
PARTIAL_PERIOD_COVERAGE = 0.80
#: 周期收益默认初始资金（元）
DEFAULT_PERIOD_CAPITAL = 10_000_000.0
#: 周期收益持仓上限硬上限（用户口径「最多 50 只」）
MAX_PERIOD_POSITIONS = 50
#: 行业/市值快照缓存（秒）：快照是最新截面，不按日分区
PROFILE_CACHE_TTL_SECONDS = 300.0
_PROFILE_CACHE: dict[str, tuple[float, dict[str, dict[str, Any]]]] = {}
#: ST/退市风险标记缓存：``market → (ts, flags, error)``；失败也缓存，避免读盘风暴
_RISK_CACHE: dict[str, tuple[float, dict[str, str], str | None]] = {}


#: 与 ``StockCodeUtil.to_prefix`` 对齐的 SQL 折叠表达式（唯一实现在 shared）。
_prefix_symbol_sql = build_prefix_symbol_sql


#: 把任意写入形态折叠成前缀式（与 StockCodeUtil.to_prefix 对齐）。
PREFIX_SYMBOL_SQL = _prefix_symbol_sql()

_RUNS_CTE = """
runs AS (
  SELECT model_id, run_id, data_trade_date FROM (
    SELECT model_id, run_id, data_trade_date,
           ROW_NUMBER() OVER (
             PARTITION BY model_id, data_trade_date
             ORDER BY signals_count DESC, created_at DESC
           ) AS rn
    FROM qm_model_inference_runs
    WHERE tenant_id = :tenant_id AND user_id = :user_id
      AND model_id = ANY(:model_ids)
      AND data_trade_date BETWEEN :start AND :end
      AND status = 'completed' AND signals_count > 0
      AND fallback_used = FALSE
  ) x WHERE rn = 1
)
""".strip()

_SCORED_CTE = f"""
scored AS (
  SELECT r.data_trade_date, r.model_id, {PREFIX_SYMBOL_SQL} AS symbol,
         e.rank_pct AS pct
  FROM runs r
  JOIN engine_signal_scores e ON e.run_id = r.run_id
  WHERE e.tenant_id = :tenant_id AND e.user_id = :user_id
    AND e.rank_pct IS NOT NULL
)
""".strip()


class BlendError(Exception):
    """可映射为 HTTP 的业务错误（路由层统一转换）。"""

    def __init__(
        self, message: str, *, status_code: int = 400, code: str = "invalid_request"
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


def _blocking_list_symbols() -> frozenset[str]:
    """当前生效的交易黑名单（后缀式）。缺失按配置事故显式报错。"""
    from backend.shared.exclusion_list import load_exclusion_list

    exclusion = load_exclusion_list("CN")
    if exclusion is None:
        raise BlendError(
            "排除名单未导入（data/exclusions/cn.json），无法按黑名单口径计算",
            status_code=409,
            code="blacklist_unavailable",
        )
    return exclusion.symbols()


def _parse_date(value: str | date | None) -> date | None:
    if value is None or isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise BlendError(f"日期格式必须是 YYYY-MM-DD：{value}") from exc


def _finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _required_coverage(model_ids: Sequence[str], required: int | None) -> int:
    """共同数据日要求的最少成员数：``min(成员数, 指定门槛)``，至少 1。"""
    total = len({str(model_id) for model_id in model_ids})
    if total <= 0:
        return 1
    wanted = total if required is None else int(required)
    return max(1, min(total, wanted))


#: 窗口统计缓存：历史日的组合分与 TopK 在推理结果落定后不再变化，
#: 60s 内重复打开榜单/切换视图直接复用，避免每次重扫 ~70 万行。
#: 进程内小缓存（最多 32 个键）——OSS 单容器单进程；不写 Redis，避免占用
#: 已顶格的 maxmemory，也杜绝缓存故障拖慢主路径。
_WINDOW_CACHE_TTL_SECONDS = 60.0
_WINDOW_CACHE_MAX_ENTRIES = 32
_window_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _window_cache_key(
    *,
    tenant_id: str,
    user_id: str,
    members: Sequence[tuple[str, float]],
    method: str,
    trim_count: int,
    min_coverage: int,
    consensus_min_count: int,
    end_date: date,
    lookback: int,
    top_k: int,
    symbols: Sequence[str],
) -> str:
    raw = json.dumps(
        {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "members": sorted(
                (str(mid), round(float(weight), 12)) for mid, weight in members
            ),
            "method": method,
            "trim_count": int(trim_count),
            "min_coverage": int(min_coverage),
            "consensus_min_count": int(consensus_min_count),
            "end_date": end_date.isoformat(),
            "lookback": int(lookback),
            "top_k": int(top_k),
            "symbols": sorted(str(symbol) for symbol in symbols),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _clone_window(payload: Mapping[str, Any]) -> dict[str, Any]:
    """缓存命中返回副本：调用方可能把 ``history`` 挂到行上，防止串改。"""
    return {
        "scores": {
            day: dict(values) for day, values in (payload.get("scores") or {}).items()
        },
        "top": {day: set(values) for day, values in (payload.get("top") or {}).items()},
        "top_rows": {
            day: [dict(row) for row in rows]
            for day, rows in (payload.get("top_rows") or {}).items()
        },
        "days": list(payload.get("days") or []),
    }


def _window_cache_get(key: str) -> dict[str, Any] | None:
    entry = _window_cache.get(key)
    if entry is None:
        return None
    expires_at, payload = entry
    if expires_at <= time.monotonic():
        _window_cache.pop(key, None)
        return None
    return _clone_window(payload)


def _window_cache_put(key: str, payload: Mapping[str, Any]) -> None:
    now = time.monotonic()
    for stale in [
        cached for cached, (expires_at, _) in _window_cache.items() if expires_at <= now
    ]:
        _window_cache.pop(stale, None)
    while len(_window_cache) >= _WINDOW_CACHE_MAX_ENTRIES:
        _window_cache.pop(next(iter(_window_cache)))
    _window_cache[key] = (now + _WINDOW_CACHE_TTL_SECONDS, _clone_window(payload))


#: 周期区间缓存：历史日的组合排名在推理落定后不再变化。
#: 与窗口缓存分开计容，互不挤占（周期视图一次要 32~300 天）。
_RANGE_CACHE_TTL_SECONDS = 120.0
_RANGE_CACHE_MAX_ENTRIES = 16
_range_cache: dict[str, tuple[float, dict[date, list[dict[str, Any]]]]] = {}


def _range_cache_key(
    *,
    tenant_id: str,
    user_id: str,
    members: Sequence[tuple[str, float]],
    method: str,
    trim_count: int,
    min_coverage: int,
    consensus_min_count: int,
    start_date: date,
    end_date: date,
    top_k: int,
) -> str:
    raw = json.dumps(
        {
            "kind": "range",
            "tenant_id": tenant_id,
            "user_id": user_id,
            "members": sorted(
                (str(mid), round(float(weight), 12)) for mid, weight in members
            ),
            "method": method,
            "trim_count": int(trim_count),
            "min_coverage": int(min_coverage),
            "consensus_min_count": int(consensus_min_count),
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "top_k": int(top_k),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _range_cache_get(key: str) -> dict[date, list[dict[str, Any]]] | None:
    entry = _range_cache.get(key)
    if entry is None:
        return None
    expires_at, payload = entry
    if expires_at <= time.monotonic():
        _range_cache.pop(key, None)
        return None
    return {day: [dict(row) for row in rows] for day, rows in payload.items()}


def _range_cache_put(
    key: str, payload: Mapping[date, Sequence[Mapping[str, Any]]]
) -> None:
    now = time.monotonic()
    for stale in [
        cached for cached, (expires_at, _) in _range_cache.items() if expires_at <= now
    ]:
        _range_cache.pop(stale, None)
    while len(_range_cache) >= _RANGE_CACHE_MAX_ENTRIES:
        _range_cache.pop(next(iter(_range_cache)))
    _range_cache[key] = (
        now + _RANGE_CACHE_TTL_SECONDS,
        {day: [dict(row) for row in rows] for day, rows in payload.items()},
    )


class ModelBlendService:
    """组合选股编排（每个请求新建即可，内部无状态）。"""

    def __init__(
        self,
        persistence: ModelBlendPersistence | None = None,
        pred_source: BlendPredSource | None = None,
    ) -> None:
        self.persistence = persistence or ModelBlendPersistence()
        self._pred_source = pred_source or BlendPredSource()

    # ── 模型清单 ────────────────────────────────────────────────────────

    async def _normalized_models(
        self, *, tenant_id: str, user_id: str, market: str = CN_MARKET
    ) -> list[dict[str, Any]]:
        from backend.shared.model_registry import model_registry_service

        user_models = await model_registry_service.list_models(
            tenant_id=tenant_id, user_id=user_id, market=market
        )
        models = [combo_auto.normalize_model(row, source="user") for row in user_models]
        models.extend(
            combo_auto.normalize_model(row, source="system")
            for row in combo_auto.load_production_models()
        )
        return models

    async def eligible(
        self, *, tenant_id: str, user_id: str, market: str = CN_MARKET
    ) -> dict[str, Any]:
        models = await self._normalized_models(
            tenant_id=tenant_id, user_id=user_id, market=market
        )
        eligible, excluded = combo_auto.eligible_models(models)
        groups = combo_auto.group_models(eligible)
        payload_groups = []
        for group in groups:
            payload_groups.append(
                {
                    "key": group["key"],
                    "signature": group["signature"],
                    "models": [
                        {
                            "model_id": m["model_id"],
                            "display_name": m["display_name"],
                            "model_type": m["model_type"],
                            "model_source": m["model_source"],
                            "horizon_days": m["target_horizon_days"],
                            "feature_count": m["feature_count"],
                            "status": m["status"],
                        }
                        for m in group["models"]
                    ],
                }
            )
        return {
            "market": market.upper(),
            "groups": payload_groups,
            "excluded": excluded,
            "total_eligible": len(eligible),
        }

    async def auto_candidates(
        self, *, tenant_id: str, user_id: str, market: str = CN_MARKET
    ) -> dict[str, Any]:
        models = await self._normalized_models(
            tenant_id=tenant_id, user_id=user_id, market=market
        )
        runnable = await self._runnable_model_ids(
            tenant_id=tenant_id,
            user_id=user_id,
            model_ids=[str(m["model_id"]) for m in models],
        )
        evidence = combo_auto.load_evidence()
        return combo_auto.build_auto_candidates(models, evidence, runnable_ids=runnable)

    async def _runnable_model_ids(
        self, *, tenant_id: str, user_id: str, model_ids: Sequence[str]
    ) -> set[str] | None:
        """至少有过一次有效推理 run（completed / signals>0 / 非 fallback）的模型。

        模板只列「能产出信号」的成员：从未推理过的模型放进模板，榜单永远
        缺成员且日期被拖旧（K2）。查询失败不阻断清单（返回空集会让模板
        全部消失，反而更糟），降级为不过滤。
        """
        if not model_ids:
            return set()
        try:
            async with get_session(read_only=True) as session:
                rows = (
                    await session.execute(
                        text(
                            """
                            SELECT DISTINCT model_id FROM qm_model_inference_runs
                            WHERE tenant_id = :tenant_id AND user_id = :user_id
                              AND model_id = ANY(:model_ids)
                              AND status = 'completed' AND signals_count > 0
                              AND fallback_used = FALSE
                            """
                        ),
                        {
                            "tenant_id": tenant_id,
                            "user_id": user_id,
                            "model_ids": list(model_ids),
                        },
                    )
                ).all()
            return {str(row[0]) for row in rows}
        except Exception as exc:  # pragma: no cover - 查询失败降级为不过滤
            logger.warning("读取模型推理可用性失败，模板不做 run 过滤: %s", exc)
            return None

    # ── 成员解析与校验 ──────────────────────────────────────────────────

    async def _resolve_members(
        self,
        *,
        tenant_id: str,
        user_id: str,
        model_ids: Sequence[str],
        market: str,
        purpose: str,
    ) -> list[dict[str, Any]]:
        ordered = list(
            dict.fromkeys(str(m).strip() for m in model_ids if str(m).strip())
        )
        if len(ordered) < MIN_MEMBERS:
            raise BlendError(f"组合至少需要 {MIN_MEMBERS} 个模型")
        if len(ordered) > MAX_MEMBERS:
            raise BlendError(f"组合最多 {MAX_MEMBERS} 个模型")

        models = await self._normalized_models(
            tenant_id=tenant_id, user_id=user_id, market=market
        )
        by_id = {m["model_id"]: m for m in models}
        unknown = [model_id for model_id in ordered if model_id not in by_id]
        if unknown:
            raise BlendError(
                f"模型不存在或无权访问：{', '.join(unknown[:5])}", code="unknown_model"
            )
        eligible, excluded = combo_auto.eligible_models(models)
        eligible_ids = {m["model_id"] for m in eligible}
        blocked = {
            row["model_id"]: row["reason"]
            for row in excluded
            if row["model_id"] in ordered
        }
        if blocked:
            detail = "、".join(f"{k}({v})" for k, v in list(blocked.items())[:5])
            raise BlendError(f"模型不可参与组合：{detail}", code="model_not_eligible")
        if any(model_id not in eligible_ids for model_id in ordered):
            raise BlendError("存在不可参与组合的模型", code="model_not_eligible")

        members = [by_id[model_id] for model_id in ordered]
        signature = combo_auto.signature_of(members[0])
        for member in members[1:]:
            other = combo_auto.signature_of(member)
            # 周期不参与「同特征」判定：next_day 允许跨周期（附 mixed_horizon
            # 警告），rebalance 由下面的同周期校验单独拦。
            if any(
                other[key] != signature[key]
                for key in combo_auto.SIGNATURE_KEYS
                if key != "target_horizon_days"
            ):
                raise BlendError(
                    "所选模型不属于同一特征签名（factor_schema_hash/data_source/"
                    "feature_count/market 必须一致）",
                    code="signature_mismatch",
                )
        ModelBlendService._ensure_rebalance_horizon(members, purpose)
        return members

    @staticmethod
    def _ensure_rebalance_horizon(
        members: Sequence[Mapping[str, Any]], purpose: str
    ) -> None:
        """调仓组合允许任意周期（T+1…T+20），但成员必须同周期。

        新建、换成员、改用途三处共用同一口径；跨周期组合只允许
        ``next_day``（由 ``preview`` 附 ``mixed_horizon`` 警告）。
        """
        if purpose != "rebalance":
            return
        unknown = [
            str(m["model_id"]) for m in members if m.get("target_horizon_days") is None
        ]
        if unknown:
            raise BlendError(
                "调仓组合成员缺少预测周期：" + "、".join(unknown[:5]),
                code="horizon_unknown",
            )
        horizons = {int(m["target_horizon_days"]) for m in members}
        if len(horizons) > 1:
            raise BlendError(
                "调仓组合成员必须同预测周期："
                + "、".join(f"T+{h}" for h in sorted(horizons)),
                code="mixed_horizon",
            )

    @staticmethod
    def _validate_params(
        members: Sequence[Mapping[str, Any]],
        *,
        min_coverage: int,
        consensus_min_count: int,
    ) -> None:
        n = len(members)
        if not 2 <= int(min_coverage) <= n:
            raise BlendError(f"min_coverage 必须在 [2, {n}]")
        if not 0 <= int(consensus_min_count) <= n:
            raise BlendError(f"consensus_min_count 必须在 [0, {n}]")

    # ── 取数 ────────────────────────────────────────────────────────────

    async def latest_common_date(
        self,
        *,
        tenant_id: str,
        user_id: str,
        model_ids: Sequence[str],
        required: int | None = None,
    ) -> date | None:
        """最近一个「至少 ``required`` 个成员有可用 run」的交易日。

        缺省 ``required = len(model_ids)``（全员）；调仓组合按
        ``min_coverage`` 传入，避免 24 成员 T+5 全组因个别模型没补推而永远无日期。
        缺成员由 ``_combine_runs`` 继续产出 ``缺少当日推理`` 警告。
        """
        required = _required_coverage(model_ids, required)
        async with get_session(read_only=True) as session:
            row = (
                await session.execute(
                    text(
                        f"""
                        WITH {_RUNS_CTE}
                        SELECT data_trade_date,
                               COUNT(DISTINCT model_id) AS n_models
                        FROM runs
                        GROUP BY data_trade_date
                        HAVING COUNT(DISTINCT model_id) >= :required
                        ORDER BY data_trade_date DESC
                        LIMIT 1
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                        "model_ids": list(model_ids),
                        "start": date(2000, 1, 1),
                        "end": date(2100, 1, 1),
                        "required": required,
                    },
                )
            ).first()
        return row[0] if row else None

    async def _runs_for_date(
        self,
        *,
        tenant_id: str,
        user_id: str,
        model_ids: Sequence[str],
        data_trade_date: date,
    ) -> dict[str, str]:
        async with get_session(read_only=True) as session:
            rows = (
                await session.execute(
                    text(
                        f"""
                        WITH {_RUNS_CTE}
                        SELECT model_id, run_id FROM runs
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                        "model_ids": list(model_ids),
                        "start": data_trade_date,
                        "end": data_trade_date,
                    },
                )
            ).all()
        return {str(model_id): str(run_id) for model_id, run_id in rows}

    async def _scores_for_runs(
        self,
        *,
        tenant_id: str,
        user_id: str,
        runs: Mapping[str, str],
    ) -> dict[str, dict[str, float]]:
        """``model_id → {prefix_symbol: pct}``；同票多写法保留最高分那条。"""
        if not runs:
            return {}
        run_to_model = {run_id: model_id for model_id, run_id in runs.items()}
        async with get_session(read_only=True) as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT run_id, symbol, fusion_score,
                               PERCENT_RANK() OVER (
                                 PARTITION BY run_id ORDER BY fusion_score
                               ) AS pct
                        FROM engine_signal_scores
                        WHERE run_id = ANY(:run_ids)
                          AND tenant_id = :tenant_id AND user_id = :user_id
                          AND fusion_score <> 'NaN'::float8
                          AND fusion_score <> 'Infinity'::float8
                          AND fusion_score <> '-Infinity'::float8
                        """
                    ),
                    {
                        "run_ids": list(runs.values()),
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                    },
                )
            ).all()
        ranked: dict[str, dict[str, tuple[float, float]]] = {}
        for run_id, symbol, fusion_score, pct in rows:
            model_id = run_to_model.get(str(run_id))
            if model_id is None or not _finite(pct):
                continue
            prefix = StockCodeUtil.to_prefix(str(symbol)) or str(symbol)
            current = ranked.setdefault(model_id, {}).get(prefix)
            score = float(fusion_score if fusion_score is not None else 0.0)
            if current is None or score > current[1]:
                ranked[model_id][prefix] = (float(pct), score)
        return {
            model_id: {symbol: value[0] for symbol, value in scores.items()}
            for model_id, scores in ranked.items()
        }

    async def _combine_runs(
        self,
        *,
        tenant_id: str,
        user_id: str,
        members: Sequence[Mapping[str, Any]],
        data_trade_date: date,
        method: str,
        weights: Mapping[str, float] | None,
        min_coverage: int,
        trim_count: int,
        consensus_min_count: int,
    ) -> tuple[BlendResult, dict[str, str], list[str]]:
        model_ids = [str(m["model_id"]) for m in members]
        runs = await self._runs_for_date(
            tenant_id=tenant_id,
            user_id=user_id,
            model_ids=model_ids,
            data_trade_date=data_trade_date,
        )
        missing = [model_id for model_id in model_ids if model_id not in runs]
        ranks = await self._scores_for_runs(
            tenant_id=tenant_id, user_id=user_id, runs=runs
        )
        result = combine_member_ranks(
            ranks,
            model_ids,
            method=method,
            weights=weights,
            min_coverage=min_coverage,
            trim=trim_count,
            consensus_min_count=consensus_min_count,
        )
        warnings = [f"缺少当日推理:{model_id}" for model_id in missing]
        return result, runs, warnings

    # ── 榜单 ────────────────────────────────────────────────────────────

    def _row_payload(
        self,
        row: BlendRow,
        names: Mapping[str, str],
        profiles: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        profile = (profiles or {}).get(row.symbol) or {}
        return {
            "code": row.symbol,
            "symbol": row.symbol,
            "name": names.get(row.symbol) or "",
            # 缺失一律 None（前端显示 —），绝不把未知市值写成 0
            "industry": profile.get("industry"),
            "market_cap_yi": profile.get("market_cap_yi"),
            "S": row.score,
            "S_pct": row.pct,
            "score": row.score,
            "pct": row.pct,
            "rank": row.rank,
            "research_score": row.research_score,
            "position": row.position,
            "coverage": row.coverage,
            "dispersion": row.dispersion,
            # 分歧档位由后端唯一口径给出（前端不再自带阈值，避免两套算法漂移）
            "dispersion_band": dispersion_band(row.dispersion),
            "contributions": dict(row.contributions),
            "missing_members": list(row.missing_members),
        }

    def _member_payload(
        self,
        members: Sequence[Mapping[str, Any]],
        runs: Mapping[str, str],
        weights: Mapping[str, float] | None,
    ) -> list[dict[str, Any]]:
        return [
            {
                "model_id": m["model_id"],
                "display_name": m["display_name"],
                "model_source": m["model_source"],
                "model_type": m["model_type"],
                "horizon_days": m["target_horizon_days"],
                "weight": float((weights or {}).get(m["model_id"], 1.0)),
                "available": m["model_id"] in runs,
                "run_id": runs.get(m["model_id"]),
            }
            for m in members
        ]

    async def ranking(
        self,
        *,
        tenant_id: str,
        user_id: str,
        members: Sequence[Mapping[str, Any]],
        data_trade_date: date,
        method: str = "mean",
        weights: Mapping[str, float] | None = None,
        min_coverage: int = 2,
        trim_count: int = 2,
        consensus_min_count: int = 0,
        top: int = 500,
        blend_id: str | None = None,
        annotate: bool = False,
        exclude_blacklist: bool = False,
    ) -> dict[str, Any]:
        result, runs, warnings = await self._combine_runs(
            tenant_id=tenant_id,
            user_id=user_id,
            members=members,
            data_trade_date=data_trade_date,
            method=method,
            weights=weights,
            min_coverage=min_coverage,
            trim_count=trim_count,
            consensus_min_count=consensus_min_count,
        )
        blacklist_meta: dict[str, Any] = {"applied": False}
        source_rows = result.rows
        if exclude_blacklist:
            blocked = _blocking_list_symbols()
            kept = [
                row
                for row in source_rows
                if StockCodeUtil.to_suffix(str(row.symbol)) not in blocked
            ]
            blacklist_meta = {
                "applied": True,
                "removed": len(source_rows) - len(kept),
                "excluded": len(blocked),
            }
            source_rows = kept
        names = await self._stock_names()
        market = str(members[0].get("market") or CN_MARKET) if members else CN_MARKET
        profiles = await self._stock_profiles(market)
        rows = [
            self._row_payload(row, names, profiles)
            for row in source_rows[: max(1, top)]
        ]
        if annotate:
            window = await self._window_stats(
                tenant_id=tenant_id,
                user_id=user_id,
                members=members,
                weights=weights,
                method=method,
                trim_count=trim_count,
                min_coverage=min_coverage,
                consensus_min_count=consensus_min_count,
                end_date=data_trade_date,
                lookback=10,
                top_k=50,
                symbols=[row["symbol"] for row in rows],
            )
            annotations = await self._annotations(
                blend_id=blend_id,
                rows=rows,
                window=window,
                data_trade_date=data_trade_date,
            )
            for row in rows:
                row["history"] = annotations.get(row["symbol"], {})
        return {
            "data_trade_date": data_trade_date.isoformat(),
            "members": self._member_payload(members, runs, weights),
            "ranking": rows,
            "blacklist": blacklist_meta,
            "stats": dict(result.stats),
            "warnings": warnings,
        }

    async def preview(
        self,
        *,
        tenant_id: str,
        user_id: str,
        model_ids: Sequence[str],
        market: str = CN_MARKET,
        data_trade_date: date | None = None,
        purpose: str = "rebalance",
        method: str = "mean",
        trim_count: int = 2,
        weights: Mapping[str, float] | None = None,
        top_k: int = 50,
        min_coverage: int = 2,
        consensus_min_count: int = 0,
        top: int = 200,
        exclude_blacklist: bool = False,
    ) -> dict[str, Any]:
        members = await self._resolve_members(
            tenant_id=tenant_id,
            user_id=user_id,
            model_ids=model_ids,
            market=market,
            purpose=purpose,
        )
        self._validate_params(
            members,
            min_coverage=min_coverage,
            consensus_min_count=consensus_min_count,
        )
        _validate_weights(members, weights)
        target = data_trade_date or await self.latest_common_date(
            tenant_id=tenant_id,
            user_id=user_id,
            model_ids=[str(m["model_id"]) for m in members],
            required=min_coverage,
        )
        if target is None:
            raise BlendError(
                "所选模型没有共同数据日：请先对成员做串行补推，或调低最低覆盖数",
                code="no_common_date",
            )
        payload = await self.ranking(
            tenant_id=tenant_id,
            user_id=user_id,
            members=members,
            data_trade_date=target,
            method=method,
            weights=weights,
            min_coverage=min_coverage,
            trim_count=trim_count,
            consensus_min_count=consensus_min_count,
            top=max(top, top_k),
            exclude_blacklist=exclude_blacklist,
        )
        payload["data_trade_date"] = target.isoformat()
        payload["signature"] = combo_auto.signature_of(members[0])
        payload["target_date"] = (await self._next_trading_day(target)).isoformat()
        payload["purpose"] = purpose
        if (
            purpose == "next_day"
            and len({m["target_horizon_days"] for m in members}) > 1
        ):
            payload["warnings"].append("mixed_horizon")
        payload["ranking"] = payload["ranking"][: max(1, top)]
        return payload

    async def available_dates(
        self,
        blend: Mapping[str, Any],
        *,
        tenant_id: str,
        user_id: str,
        limit: int = 60,
    ) -> list[str]:
        members = await self.members_from_blend(blend)
        model_ids = [str(m["model_id"]) for m in members]
        required = _required_coverage(model_ids, int(blend.get("min_coverage") or 2))
        async with get_session(read_only=True) as session:
            rows = (
                await session.execute(
                    text(
                        f"""
                        WITH {_RUNS_CTE}
                        SELECT data_trade_date, COUNT(DISTINCT model_id) AS n_models
                        FROM runs
                        GROUP BY data_trade_date
                        HAVING COUNT(DISTINCT model_id) >= :required
                        ORDER BY data_trade_date DESC
                        LIMIT :limit
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                        "model_ids": model_ids,
                        "start": date(2000, 1, 1),
                        "end": date(2100, 1, 1),
                        "required": required,
                        "limit": max(1, min(365, limit)),
                    },
                )
            ).all()
        return [str(row[0]) for row in rows]

    # ── 历史数据源（pred 优先，整组合选源）────────────────────────────
    @staticmethod
    def _pred_inputs(
        members: Sequence[Mapping[str, Any]],
        weights: Mapping[str, float] | None,
    ) -> list[dict[str, Any]]:
        return [
            {
                "model_id": str(member["model_id"]),
                "weight": float((weights or {}).get(str(member["model_id"]), 1.0)),
            }
            for member in members
        ]

    async def _readiness_for(
        self,
        *,
        members: Sequence[Mapping[str, Any]],
        weights: Mapping[str, float] | None,
        tenant_id: str,
        user_id: str,
        market: str,
    ):
        return await self._pred_source.readiness(
            self._pred_inputs(members, weights),
            tenant_id=tenant_id,
            user_id=user_id,
            market=market,
        )

    async def _runs_bounds(
        self,
        *,
        tenant_id: str,
        user_id: str,
        model_ids: Sequence[str],
        required: int,
    ) -> dict[str, Any]:
        """推理运行记录（runs）的可用区间与共同数据日数。"""
        sql = f"""
            WITH {_RUNS_CTE}
            SELECT MIN(day) AS first_day, MAX(day) AS last_day, COUNT(*) AS day_count
            FROM (
              SELECT data_trade_date AS day FROM runs
              GROUP BY data_trade_date
              HAVING COUNT(DISTINCT model_id) >= :required
            ) x
        """
        async with get_session(read_only=True) as session:
            row = (
                await session.execute(
                    text(sql),
                    {
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                        "model_ids": list(model_ids),
                        "start": date(2000, 1, 1),
                        "end": date(2100, 1, 1),
                        "required": _required_coverage(model_ids, required),
                    },
                )
            ).first()
        if not row or row[0] is None:
            return {"start": None, "end": None, "trading_days": 0}
        first_day = row[0] if isinstance(row[0], date) else _parse_date(row[0])
        last_day = row[1] if isinstance(row[1], date) else _parse_date(row[1])
        return {
            "start": first_day.isoformat() if first_day else None,
            "end": last_day.isoformat() if last_day else None,
            "trading_days": int(row[2] or 0),
        }

    @staticmethod
    def _bounds_payload(
        readiness: Any,
        runs: Mapping[str, Any],
    ) -> dict[str, Any]:
        pred = None
        if readiness.ready_members:
            pred = {
                "start": readiness.start.isoformat() if readiness.start else None,
                "end": readiness.end.isoformat() if readiness.end else None,
                "trading_days": int(readiness.trading_days),
                "members_ready": readiness.ready_members,
                "members_total": readiness.members_total,
            }
        source = (
            "pred" if readiness.all_ready and readiness.trading_days > 0 else "runs"
        )
        effective = pred if source == "pred" else runs
        return {
            "source": source,
            "pred": pred,
            "runs": dict(runs),
            "effective": {
                "start": (effective or {}).get("start"),
                "end": (effective or {}).get("end"),
                "trading_days": int((effective or {}).get("trading_days") or 0),
            },
            "warnings": list(readiness.warnings),
        }

    @staticmethod
    def _benchmark_payload(
        closes: Mapping[date, float], *, market: str = CN_MARKET
    ) -> dict[str, Any]:
        """基准徽标的元信息：只描述数据是否可用，不重复行情序列。"""
        enabled = market == CN_MARKET
        return {
            "symbol": BENCHMARK_SYMBOL if enabled else None,
            "name": "沪深300" if enabled else None,
            "available": bool(closes),
            "days": len(closes),
        }

    async def _history_rows(
        self,
        *,
        tenant_id: str,
        user_id: str,
        members: Sequence[Mapping[str, Any]],
        weights: Mapping[str, float] | None,
        method: str,
        trim_count: int,
        min_coverage: int,
        consensus_min_count: int,
        start_date: date,
        end_date: date,
        top_k: int,
        market: str = CN_MARKET,
        readiness: Any | None = None,
    ) -> tuple[dict[date, list[dict[str, Any]]], str, Any]:
        """区间内每日组合分 TopK：pred 全成员可用才走 pred，否则整体回退 runs。"""
        ready = readiness or await self._readiness_for(
            members=members,
            weights=weights,
            tenant_id=tenant_id,
            user_id=user_id,
            market=market,
        )
        if ready.all_ready and ready.trading_days > 0:
            rows = await self._pred_source.load_top_rows(
                ready.members,
                method=method,
                trim_count=trim_count,
                min_coverage=min_coverage,
                consensus_min_count=consensus_min_count,
                start=start_date,
                end=end_date,
                top_k=top_k,
            )
            return rows, "pred", ready
        rows = await self._range_stats(
            tenant_id=tenant_id,
            user_id=user_id,
            members=members,
            weights=weights,
            method=method,
            trim_count=trim_count,
            min_coverage=min_coverage,
            consensus_min_count=consensus_min_count,
            start_date=start_date,
            end_date=end_date,
            top_k=top_k,
        )
        return rows, "runs", ready

    async def data_bounds(
        self,
        blend: Mapping[str, Any],
        *,
        tenant_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        """组合历史的统一数据端口：pred / runs 覆盖范围与生效区间。"""
        members = await self.members_from_blend(blend)
        weights = _weights_from_members(blend, members)
        market = str(blend.get("market") or CN_MARKET)
        readiness = await self._readiness_for(
            members=members,
            weights=weights,
            tenant_id=tenant_id,
            user_id=user_id,
            market=market,
        )
        runs = await self._runs_bounds(
            tenant_id=tenant_id,
            user_id=user_id,
            model_ids=[str(m["model_id"]) for m in members],
            required=int(blend.get("min_coverage") or 2),
        )
        return self._bounds_payload(readiness, runs)

    @staticmethod
    async def _benchmark_closes(
        start: date, end: date, *, market: str = CN_MARKET
    ) -> dict[date, float]:
        """沪深 300 指数收盘（MA20 需要向前多取一段）。失败返回空映射。"""
        if market != CN_MARKET or end < start:
            return {}
        try:
            from backend.services.engine.data_platform.quantdb_hub import (
                QuantDBDataHub,
            )

            hub = QuantDBDataHub.get_instance()
            if hub is None or not hub.available:
                return {}
            frame = await asyncio.to_thread(
                hub.fetch_index_kline,
                BENCHMARK_SYMBOL,
                start - timedelta(days=BENCHMARK_LOOKBACK_DAYS),
                end,
            )
        except Exception as exc:  # noqa: BLE001 - 基准缺失只降级，不阻断
            logger.warning("读取基准 %s 失败: %s", BENCHMARK_SYMBOL, exc)
            return {}
        if frame is None or getattr(frame, "empty", True):
            return {}
        out: dict[date, float] = {}
        for _, row in frame.iterrows():
            raw_day = row.get("trade_date")
            day = raw_day.date() if hasattr(raw_day, "date") else _parse_date(raw_day)
            close = row.get("close")
            if day is None or not _finite(close) or float(close) <= 0:
                continue
            out[day] = float(close)
        return out

    async def rolling(
        self,
        blend: Mapping[str, Any],
        *,
        tenant_id: str,
        user_id: str,
        end_date: date | None = None,
        window: int = 10,
        per_day: int = 5,
        capital: float = DEFAULT_PERIOD_CAPITAL,
        exclude_st: bool = True,
        min_adv_yi: float = DEFAULT_MIN_ADV_YI,
        adv_participation: float = DEFAULT_ADV_PARTICIPATION,
    ) -> dict[str, Any]:
        """滚动建仓视图：最近 ``window+1`` 个交易日，每日 Top ``per_day``。

        选中口径与榜单完全同源（组合分排序），历史数据源与 ``period()``
        共用 ``_history_rows``（pred 测试集优先、整组合降级 runs）。

        统计口径是**每日批次**：入选日收盘按等权预算建仓，单票金额再受
        ``ADV20 × 参与率`` 约束；ST/退市（当前截面）与成交额不足的标的不
        建仓、不替补，资金留现金。次一交易日收盘结算；最后一批只估值不结算
        （计入浮动）。``realized + unrealized == final_equity - initial_capital``。
        名单侧的连续在榜只服务列展示，不参与组合胜率。
        """
        if not 1 <= int(window) <= 60:
            raise BlendError("滚窗必须在 [1, 60]")
        if not 1 <= int(per_day) <= 50:
            raise BlendError("每日只数必须在 [1, 50]")
        if not _finite(capital) or float(capital) <= 0:
            raise BlendError("初始资金必须是正数")
        if not _finite(min_adv_yi) or float(min_adv_yi) < 0:
            raise BlendError("最低成交额必须为非负数")
        if not _finite(adv_participation) or not 0 < float(adv_participation) <= 0.2:
            raise BlendError("成交额参与率必须在 (0, 0.2]")
        capital = float(capital)
        min_adv_yi = float(min_adv_yi)
        adv_participation = float(adv_participation)
        members = await self.members_from_blend(blend)
        if len(members) < MIN_MEMBERS:
            raise BlendError(f"组合至少需要 {MIN_MEMBERS} 个模型")
        weights = _weights_from_members(blend, members)
        member_ids = [str(m["model_id"]) for m in members]
        min_coverage = int(blend.get("min_coverage") or 2)
        market = str(blend.get("market") or CN_MARKET)

        readiness = await self._readiness_for(
            members=members,
            weights=weights,
            tenant_id=tenant_id,
            user_id=user_id,
            market=market,
        )
        runs = await self._runs_bounds(
            tenant_id=tenant_id,
            user_id=user_id,
            model_ids=member_ids,
            required=min_coverage,
        )
        bounds = self._bounds_payload(readiness, runs)
        target = (
            end_date
            or _parse_date((bounds.get("effective") or {}).get("end"))
            or await self.latest_common_date(
                tenant_id=tenant_id,
                user_id=user_id,
                model_ids=member_ids,
                required=min_coverage,
            )
        )
        if target is None:
            raise BlendError(
                "所选模型没有共同数据日：请先对成员做串行补推，或调低最低覆盖数",
                code="no_common_date",
            )

        window_days = int(window) + 1
        trading_days = await self._trading_days(target, need=window_days)
        days = trading_days[-window_days:]
        rows_by_day: dict[date, list[dict[str, Any]]] = {}
        source = bounds.get("source") or "runs"
        if days:
            rows_by_day, source, readiness = await self._history_rows(
                tenant_id=tenant_id,
                user_id=user_id,
                members=members,
                weights=weights,
                method=str(blend.get("method") or "mean"),
                trim_count=int(blend.get("trim_count") or 2),
                min_coverage=min_coverage,
                consensus_min_count=int(blend.get("consensus_min_count") or 0),
                start_date=days[0],
                end_date=days[-1],
                top_k=int(per_day),
                market=market,
                readiness=readiness,
            )
            bounds = self._bounds_payload(readiness, runs)

        names = await self._stock_names()
        profiles = await self._stock_profiles(market)
        picks_by_day = {
            day: [
                str(row["symbol"])
                for row in (rows_by_day.get(day) or [])[: int(per_day)]
            ]
            for day in days
        }
        symbols = sorted(
            {symbol for picked in picks_by_day.values() for symbol in picked}
        )
        # 首日涨跌幅需要前一交易日收盘：起点向前多取 10 个自然日
        closes = (
            await self._close_history(symbols, days[0] - timedelta(days=10), days[-1])
            if days and symbols
            else {}
        )
        # 可交易性数据：仅 A 股有统一成交额与 ST/退市标记。
        # None = 不可用 → 纯函数等权回退/不剔除，并由 warnings 明示。
        amounts: dict[str, dict[date, float]] | None = None
        risk_flags: dict[str, str] | None = None
        st_error: str | None = None
        if days and symbols and market == CN_MARKET:
            amount_start = days[0] - timedelta(
                days=max(45, DEFAULT_ADV_LOOKBACK_DAYS * 3)
            )
            amounts = await self._amount_history(symbols, amount_start, days[-1])
            if not amounts:
                amounts = None
            if exclude_st:
                flags, st_error = await self._risk_flags(symbols, market=market)
                risk_flags = flags if st_error is None else None
        benchmark_closes: dict[date, float] = {}
        rotation: dict[str, Any] = {
            "batches": [],
            "daily": [],
            "equity_by_day": {},
            "summary": {},
        }
        cells: dict[str, Any] = {"by_symbol": {}, "by_day": {}}
        market_by_day: dict[date, dict[str, Any]] = {}
        if days:
            benchmark_closes = await self._benchmark_closes(
                days[0], days[-1], market=market
            )
            rotation = simulate_daily_rotation(
                days=days,
                picks_by_day=picks_by_day,
                closes=closes,
                capital=capital,
                benchmark_closes=benchmark_closes,
                amounts=amounts,
                risk_flags=risk_flags,
                min_adv_yi=min_adv_yi,
                adv_participation=adv_participation,
            )
            cells = cell_stats(
                days,
                picks_by_day,
                closes,
                equity_by_day=rotation["equity_by_day"],
                entry_plans=rotation.get("entry_plans"),
            )
            market_by_day = metrics.market_day_stats(days, benchmark_closes)

        performance = compute_rolling_performance(days, picks_by_day, closes)

        def exit_payload(trade: Mapping[str, Any]) -> dict[str, Any]:
            symbol = str(trade["symbol"])
            profile = profiles.get(symbol) or {}
            return {
                "symbol": symbol,
                "code": symbol,
                "name": names.get(symbol) or "",
                "industry": profile.get("industry"),
                "market_cap_yi": profile.get("market_cap_yi"),
                "entry_date": trade["entry_date"].isoformat(),
                "exit_date": trade["exit_date"].isoformat(),
                "entry_price": trade["entry_price"],
                "exit_price": trade["exit_price"],
                "return_pct": trade["return_pct"],
                "held_days": trade["held_days"],
                "entry_from_window_start": trade["entry_from_window_start"],
            }

        exit_rows_by_day = {
            day: [exit_payload(trade) for trade in trades]
            for day, trades in performance["exits_by_day"].items()
        }

        streak: dict[str, int] = {}
        previous: set[str] = set()
        columns: list[dict[str, Any]] = []
        for day in days:
            picks: list[dict[str, Any]] = []
            today: set[str] = set()
            day_cells = cells["by_day"].get(day) or {}
            day_plans = (rotation.get("entry_plans") or {}).get(day) or {}
            for row in (rows_by_day.get(day) or [])[: int(per_day)]:
                symbol = str(row["symbol"])
                today.add(symbol)
                held = streak.get(symbol, 0) + 1
                streak[symbol] = held
                profile = profiles.get(symbol) or {}
                cell = day_cells.get(symbol) or {}
                stats = cells["by_symbol"].get(symbol) or {}
                entry = day_plans.get(symbol) or {}
                adv = entry.get("adv")
                picks.append(
                    {
                        **row,
                        "code": symbol,
                        "name": names.get(symbol) or "",
                        "industry": profile.get("industry"),
                        "market_cap_yi": profile.get("market_cap_yi"),
                        "held_days": held,
                        "is_new": held == 1,
                        # 当日格子：涨跌幅 / 持仓权重 / 当日盈亏（仅当日实际持有）
                        "change_pct": cell.get("change_pct"),
                        "weight_pct": cell.get("weight_pct"),
                        "day_pnl_amount": cell.get("pnl_amount"),
                        # 当日计划：是否建仓 / 计划权重 / ADV20（亿元）
                        "buy_status": entry.get("status"),
                        "entry_weight_pct": (
                            round(float(entry.get("weight") or 0.0) * 100.0, 4)
                            if entry
                            else None
                        ),
                        "adv_yi": round(float(adv) / YI, 4) if adv else None,
                        "stats": {
                            "appearances": stats.get("appearances"),
                            "streak_days": stats.get("streak_days", held),
                            "first_seen": (
                                stats["first_seen"].isoformat()
                                if stats.get("first_seen")
                                else None
                            ),
                            "since_first_pct": stats.get("since_first_pct"),
                            "next_day_avg_pct": stats.get("next_day_avg_pct"),
                            "next_day_win_rate_pct": stats.get("next_day_win_rate_pct"),
                            "next_day_samples": stats.get("next_day_samples"),
                            "next_day_std_pct": stats.get("next_day_std_pct"),
                            "latest_close": stats.get("latest_close"),
                            "change_pct": stats.get("change_pct"),
                        },
                    }
                )
            market_row = market_by_day.get(day) or {
                "change_pct": None,
                "close": None,
                "ma20_deviation_pct": None,
                "position": "unknown",
                "tone": "unknown",
            }
            columns.append(
                {
                    "date": day.isoformat(),
                    "market": market_row,
                    "picks": picks,
                    "exits": sorted(previous - today),
                    "exit_rows": exit_rows_by_day.get(day, []),
                }
            )
            # 断榜后再次入选按「新进」计，不伪造连续持有
            streak = {
                symbol: count for symbol, count in streak.items() if symbol in today
            }
            previous = today

        latest = columns[-1] if columns else {"picks": [], "exits": []}
        batch_rows = [
            {
                **batch,
                "entry_date": batch["entry_date"].isoformat(),
                "exit_date": batch["exit_date"].isoformat(),
            }
            for batch in rotation["batches"]
        ]
        daily_rows = [
            {
                **row,
                "date": row["date"].isoformat(),
                "exit_date": row["exit_date"].isoformat(),
            }
            for row in rotation["daily"]
        ]
        warnings = list(readiness.warnings)
        if days and market == CN_MARKET and not benchmark_closes:
            warnings.append("benchmark_unavailable")
        if days and symbols and market == CN_MARKET:
            if amounts is None:
                # 成交额不可用：本轮未校验容量，按等权回退（明示，不静默）
                warnings.append("tradability_unavailable")
            if exclude_st:
                warnings.append(
                    "st_filter_static"
                    if risk_flags is not None
                    else "st_filter_unavailable"
                )
        filters = {
            "market": market,
            "exclude_st": bool(exclude_st),
            "st_applied": risk_flags is not None,
            # 只回固定原因码，异常原文只进服务端日志（避免响应面泄漏内部路径/SQL）
            "st_error": "risk_flags_unavailable" if st_error else None,
            "capacity_applied": amounts is not None,
            "min_adv_yi": min_adv_yi,
            "adv_participation": adv_participation,
            "adv_lookback_days": DEFAULT_ADV_LOOKBACK_DAYS,
            "min_adv_days": DEFAULT_MIN_ADV_DAYS,
        }
        return {
            "blend_id": str(blend["blend_id"]),
            "data_trade_date": target.isoformat(),
            "target_date": (await self._next_trading_day(target)).isoformat(),
            "window": int(window),
            "per_day": int(per_day),
            "capital": capital,
            "source": source,
            "bounds": bounds,
            "benchmark": self._benchmark_payload(benchmark_closes, market=market),
            "days": [day.isoformat() for day in days],
            "columns": columns,
            "batches": batch_rows,
            "daily": daily_rows,
            "members": self._member_payload(members, {}, weights),
            "filters": filters,
            "summary": {
                "unique_symbols": len(
                    {pick["symbol"] for column in columns for pick in column["picks"]}
                ),
                "new_today": sum(1 for pick in latest["picks"] if pick["is_new"]),
                "exited_today": len(latest["exits"]),
                # 每日批次口径：收盘建仓/次日收盘结算，未计费用
                "performance": rotation["summary"],
            },
            "warnings": warnings,
        }

    async def period(
        self,
        blend: Mapping[str, Any],
        *,
        tenant_id: str,
        user_id: str,
        start_date: date | None = None,
        end_date: date | None = None,
        capital: float = DEFAULT_PERIOD_CAPITAL,
        positions: int | None = None,
    ) -> dict[str, Any]:
        """周期收益：固定持有期滚动桶（收盘建仓/到期收盘退出，无费用）。

        与 ``rolling()`` 的区别：``rolling()`` 是**窗口内每日换仓**的看板，
        本方法按模板 ``entry_per_day × rolling_window`` 真正持有到到期日，
        初始资金按 ``positions`` 等分，可跨月/跨半年统计。
        """
        members = await self.members_from_blend(blend)
        if len(members) < MIN_MEMBERS:
            raise BlendError(f"组合至少需要 {MIN_MEMBERS} 个模型")
        weights = _weights_from_members(blend, members)
        member_ids = [str(m["model_id"]) for m in members]
        min_coverage = int(blend.get("min_coverage") or 2)
        method = str(blend.get("method") or "mean")
        entry_per_day = int(blend.get("entry_per_day") or 5)
        hold_days = int(blend.get("rolling_window") or 10)
        template_positions = int(blend.get("target_positions") or 50)
        if not 1 <= entry_per_day <= 50:
            raise BlendError("模板的每日新进数必须在 [1, 50]")
        if not 1 <= hold_days <= 60:
            raise BlendError("模板的持有天数必须在 [1, 60]")
        target_positions = (
            min(max(1, template_positions), MAX_PERIOD_POSITIONS)
            if positions is None
            else int(positions)
        )
        if not 1 <= target_positions <= MAX_PERIOD_POSITIONS:
            raise BlendError(f"持仓上限必须在 [1, {MAX_PERIOD_POSITIONS}]")
        if not _finite(capital) or float(capital) <= 0:
            raise BlendError("初始资金必须是正数")
        capital = float(capital)

        market = str(blend.get("market") or CN_MARKET)
        readiness = await self._readiness_for(
            members=members,
            weights=weights,
            tenant_id=tenant_id,
            user_id=user_id,
            market=market,
        )
        runs = await self._runs_bounds(
            tenant_id=tenant_id,
            user_id=user_id,
            model_ids=member_ids,
            required=min_coverage,
        )
        bounds = self._bounds_payload(readiness, runs)

        target_end = (
            end_date
            or _parse_date((bounds.get("effective") or {}).get("end"))
            or await self.latest_common_date(
                tenant_id=tenant_id,
                user_id=user_id,
                model_ids=member_ids,
                required=min_coverage,
            )
        )
        if target_end is None:
            raise BlendError(
                "所选模型没有共同数据日：请先对成员做串行补推，或调低最低覆盖数",
                code="no_common_date",
            )
        target_start = start_date or (target_end - timedelta(days=180))
        if target_start > target_end:
            raise BlendError("建仓日不能晚于截止日")
        calendar_days = await self._calendar_days(target_start, target_end)
        if not calendar_days:
            raise BlendError("所选区间内没有交易日")
        max_days = (
            MAX_PRED_PERIOD_TRADING_DAYS
            if bounds["source"] == "pred"
            else MAX_PERIOD_TRADING_DAYS
        )
        max_non_mean_days = (
            MAX_PRED_NON_MEAN_PERIOD_DAYS
            if bounds["source"] == "pred"
            else MAX_NON_MEAN_PERIOD_DAYS
        )
        if len(calendar_days) > max_days:
            raise BlendError(
                f"区间最多 {max_days} 个交易日，"
                f"请把建仓日推后（当前 {len(calendar_days)} 个交易日）",
                status_code=422,
                code="range_too_long",
            )
        if method != "mean" and len(calendar_days) > max_non_mean_days:
            raise BlendError(
                f"{method} 方法最多支持 {max_non_mean_days} 个交易日，"
                "请缩短区间或改用均值组合",
                status_code=422,
                code="range_too_long",
            )

        rows_by_day, source, readiness = await self._history_rows(
            tenant_id=tenant_id,
            user_id=user_id,
            members=members,
            weights=weights,
            method=method,
            trim_count=int(blend.get("trim_count") or 2),
            min_coverage=min_coverage,
            consensus_min_count=int(blend.get("consensus_min_count") or 0),
            start_date=target_start,
            end_date=target_end,
            top_k=target_positions,
            market=market,
            readiness=readiness,
        )
        bounds = self._bounds_payload(readiness, runs)
        day_set = set(calendar_days)
        scoring_days = sorted(day for day in rows_by_day if day in day_set)
        if len(scoring_days) < 2:
            raise BlendError(
                "该区间评分日不足 2 天，无法形成滚动周期：请放宽区间，"
                "或先对成员做串行补推",
                status_code=422,
                code="insufficient_scoring_days",
            )
        effective_start = scoring_days[0]
        effective_end = target_end
        sim_days = [day for day in calendar_days if day >= effective_start]
        coverage_ratio = len(scoring_days) / len(sim_days) if sim_days else 0.0
        if coverage_ratio < MIN_PERIOD_COVERAGE:
            raise BlendError(
                f"评分覆盖率仅 {coverage_ratio:.0%}（评分日 {len(scoring_days)} / "
                f"交易日 {len(sim_days)}），不足以统计周期收益；请缩短区间",
                status_code=422,
                code="insufficient_coverage",
            )

        picks_by_day = {
            day: [str(row["symbol"]) for row in rows[:target_positions]]
            for day, rows in rows_by_day.items()
            if effective_start <= day <= effective_end
        }
        symbols = sorted(
            {symbol for picks in picks_by_day.values() for symbol in picks}
        )
        closes = (
            await self._close_history(symbols, effective_start, effective_end)
            if symbols
            else {}
        )
        if not closes:
            raise BlendError(
                "行情源不可用，未计算周期收益（不使用 0 价或伪造行情）",
                status_code=422,
                code="quote_source_unavailable",
            )

        # 基准（沪深 300）：覆盖率不足 95% 时引擎侧把基准/超额一律置 None
        benchmark_closes = await self._benchmark_closes(
            effective_start, effective_end, market=market
        )
        result = simulate_period(
            days=sim_days,
            picks_by_day=picks_by_day,
            closes=closes,
            capital=capital,
            positions=target_positions,
            entry_per_day=entry_per_day,
            hold_days=hold_days,
            benchmark_closes=benchmark_closes,
        )
        names = await self._stock_names()
        warnings: list[str] = list(readiness.warnings)
        if market == CN_MARKET and not benchmark_closes:
            warnings.append("benchmark_unavailable")
        if target_start < effective_start:
            warnings.append("effective_start_clamped")
        if coverage_ratio < 1.0:
            warnings.append("scoring_gap")
        if coverage_ratio < PARTIAL_PERIOD_COVERAGE:
            warnings.append("partial_data")
        counts = result["warnings"]
        for code in ("missing_entry_price", "deferred_exit", "stale_mark"):
            if int(counts.get(code) or 0) > 0:
                warnings.append(code)
        if counts.get("positions_cap"):
            warnings.append("positions_cap")
        if counts.get("underfill"):
            warnings.append("underfill")

        summary = dict(result["summary"])
        summary["data_quality"] = (
            "partial" if coverage_ratio < PARTIAL_PERIOD_COVERAGE else "complete"
        )
        summary["scoring_coverage_pct"] = round(coverage_ratio * 100.0, 2)

        def trade_row(row: Mapping[str, Any], *, exit_side: bool) -> dict[str, Any]:
            symbol = str(row["symbol"])
            payload = {
                **row,
                "code": symbol,
                "name": names.get(symbol) or "",
                "entry_date": row["entry_date"].isoformat(),
            }
            if exit_side:
                payload["exit_date"] = row["exit_date"].isoformat()
            else:
                payload["mark_date"] = row["mark_date"].isoformat()
            return payload

        month_rows = [
            {
                **row,
                "start_date": row["start_date"].isoformat(),
                "end_date": row["end_date"].isoformat(),
            }
            for row in result["months"]
        ]
        daily_rows = [
            {**row, "date": row["date"].isoformat()} for row in result["daily"]
        ]
        return {
            "blend_id": str(blend["blend_id"]),
            "source": source,
            "bounds": bounds,
            "benchmark": self._benchmark_payload(benchmark_closes, market=market),
            "requested": {
                "start": target_start.isoformat(),
                "end": target_end.isoformat(),
                "capital": capital,
                "positions": target_positions,
            },
            "effective": {
                "start": effective_start.isoformat(),
                "end": effective_end.isoformat(),
                "first_scoring_day": scoring_days[0].isoformat(),
                "last_scoring_day": scoring_days[-1].isoformat(),
            },
            "template": {
                "entry_per_day": entry_per_day,
                "hold_days": hold_days,
                "target_positions": template_positions,
                "method": method,
            },
            "summary": summary,
            "months": month_rows,
            "daily": daily_rows,
            "trades": [trade_row(row, exit_side=True) for row in result["trades"]],
            "open": [trade_row(row, exit_side=False) for row in result["open"]],
            "warnings": warnings,
            "members": self._member_payload(members, {}, weights),
        }

    async def create_blend(
        self,
        *,
        tenant_id: str,
        user_id: str,
        name: str,
        model_ids: Sequence[str],
        market: str = CN_MARKET,
        purpose: str = "rebalance",
        method: str = "mean",
        trim_count: int = 2,
        weights: Mapping[str, float] | None = None,
        min_coverage: int = 8,
        consensus_min_count: int = 0,
        entry_per_day: int = 5,
        rolling_window: int = 10,
        target_positions: int = 50,
        source: str = "manual",
        template_id: str | None = None,
    ) -> dict[str, Any]:
        clean_name = combo_auto.template_slug(name)
        if not clean_name:
            raise BlendError("组合名称不能为空")
        members = await self._resolve_members(
            tenant_id=tenant_id,
            user_id=user_id,
            model_ids=model_ids,
            market=market,
            purpose=purpose,
        )
        self._validate_params(
            members,
            min_coverage=min_coverage,
            consensus_min_count=consensus_min_count,
        )
        _validate_weights(members, weights)
        weighting = "equal" if weights is None else "manual"
        signature = combo_auto.signature_of(members[0])
        return await self.persistence.create_blend(
            tenant_id=tenant_id,
            user_id=user_id,
            name=clean_name,
            market=str(signature["market"]),
            purpose=purpose,
            source=source,
            template_id=template_id,
            horizon_days=int(signature["target_horizon_days"] or 10),
            weighting=weighting,
            method=method,
            trim_count=int(trim_count),
            min_coverage=int(min_coverage),
            consensus_min_count=int(consensus_min_count),
            entry_per_day=int(entry_per_day),
            rolling_window=int(rolling_window),
            target_positions=int(target_positions),
            feature_signature=str(signature["factor_schema_hash"]),
            data_source=str(signature["data_source"]),
            members=[
                (
                    str(m["model_id"]),
                    str(m["model_source"]),
                    float((weights or {}).get(m["model_id"], 1.0)),
                )
                for m in members
            ],
        )

    async def update_blend(
        self,
        blend: Mapping[str, Any],
        *,
        tenant_id: str,
        user_id: str,
        fields: Mapping[str, Any],
        model_ids: Sequence[str] | None,
        weights: Mapping[str, float] | None,
    ) -> dict[str, Any]:
        clean: dict[str, Any] = {}
        if "name" in fields:
            name = combo_auto.template_slug(str(fields["name"]))
            if not name:
                raise BlendError("组合名称不能为空")
            clean["name"] = name
        for key in (
            "purpose",
            "method",
            "trim_count",
            "min_coverage",
            "consensus_min_count",
            "entry_per_day",
            "rolling_window",
            "target_positions",
        ):
            if key in fields and fields[key] is not None:
                clean[key] = fields[key]
        if weights is not None and model_ids is None:
            # 权重只在重算成员时落库；不拦就会静默丢弃并返回 200
            raise BlendError(
                "修改权重必须同时提交 model_ids",
                code="weights_require_members",
            )
        members_payload = None
        resolved_members: list[dict[str, Any]] | None = None
        if model_ids is not None:
            members = await self._resolve_members(
                tenant_id=tenant_id,
                user_id=user_id,
                model_ids=model_ids,
                market=str(blend.get("market") or CN_MARKET),
                purpose=str(
                    clean.get("purpose") or blend.get("purpose") or "rebalance"
                ),
            )
            _validate_weights(members, weights)
            resolved_members = list(members)
            members_payload = [
                (
                    str(m["model_id"]),
                    str(m["model_source"]),
                    float((weights or {}).get(m["model_id"], 1.0)),
                )
                for m in members
            ]
            clean["weighting"] = "equal" if weights is None else "manual"
            signature = combo_auto.signature_of(members[0])
            clean["feature_signature"] = str(signature["factor_schema_hash"])
            clean["data_source"] = str(signature["data_source"])
            clean["horizon_days"] = int(signature["target_horizon_days"] or 10)
        elif clean.get("purpose") == "rebalance":
            # 只改用途不换成员：存量成员同样要过「同周期」校验
            self._ensure_rebalance_horizon(
                await self.members_from_blend(blend), "rebalance"
            )
        merged_min = int(clean.get("min_coverage", blend.get("min_coverage") or 2))
        merged_consensus = int(
            clean.get("consensus_min_count", blend.get("consensus_min_count") or 0)
        )
        member_count = len(resolved_members or blend.get("members") or [])
        if resolved_members is not None:
            self._validate_params(
                resolved_members,
                min_coverage=merged_min,
                consensus_min_count=merged_consensus,
            )
        elif (
            not 2 <= merged_min <= member_count
            or not 0 <= merged_consensus <= member_count
        ):
            raise BlendError("门槛参数超出成员数范围")
        await self.persistence.update_blend(
            str(blend["blend_id"]),
            tenant_id=tenant_id,
            user_id=user_id,
            fields=clean,
            members=members_payload,
        )
        return (
            await self.persistence.get_blend(
                str(blend["blend_id"]), tenant_id=tenant_id, user_id=user_id
            )
            or {}
        )

    @staticmethod
    async def _stock_names() -> dict[str, str]:
        try:
            from backend.services.api.routers.model_training import (
                _load_quantdb_stock_names,
            )

            return _load_quantdb_stock_names()
        except Exception as exc:  # pragma: no cover - 名称缺失不影响主流程
            logger.warning("组合榜单读取股票简称失败: %s", exc)
            return {}

    @staticmethod
    async def _stock_profiles(
        market: str = CN_MARKET,
    ) -> dict[str, dict[str, Any]]:
        """``前缀式 symbol → {industry, market_cap_yi}``（5 分钟进程内缓存）。

        口径：``stock_snapshot`` 是**当前截面**，不是按日快照；历史日期榜单的
        行业/市值只能标「当前」，不得用于回测归因。缺失/NaN 一律 None。
        """
        key = str(market or CN_MARKET).upper()
        now = time.monotonic()
        cached = _PROFILE_CACHE.get(key)
        if cached is not None and now - cached[0] < PROFILE_CACHE_TTL_SECONDS:
            return cached[1]
        try:
            from backend.services.engine.factor_research import store

            # parquet 读取是阻塞 IO：放到线程池，避免卡住 API/engine 事件循环
            frame = await asyncio.to_thread(store.stock_snapshot, key)
        except Exception as exc:  # pragma: no cover - 缺失不影响榜单主流程
            logger.warning("组合榜单读取行业/市值失败(%s): %s", key, exc)
            # 失败也缓存，短暂窗口内不再反复读盘/刷日志
            _PROFILE_CACHE[key] = (now, {})
            return {}
        profiles: dict[str, dict[str, Any]] = {}
        if frame is not None and not getattr(frame, "empty", True):
            has_industry = "industry" in frame.columns
            has_mv = "total_mv_yi" in frame.columns
            for _, row in frame.iterrows():
                prefix = StockCodeUtil.to_prefix(str(row.get("symbol") or ""))
                if not prefix:
                    continue
                industry = row.get("industry") if has_industry else None
                market_cap = row.get("total_mv_yi") if has_mv else None
                profiles[prefix] = {
                    "industry": industry.strip()
                    if isinstance(industry, str) and industry.strip()
                    else None,
                    "market_cap_yi": float(market_cap) if _finite(market_cap) else None,
                }
        _PROFILE_CACHE[key] = (now, profiles)
        if len(_PROFILE_CACHE) > 8:  # 市场数量有限；防御性上限
            oldest = min(_PROFILE_CACHE.items(), key=lambda item: item[1][0])[0]
            _PROFILE_CACHE.pop(oldest, None)
        return profiles

    # ── 历史标注 ────────────────────────────────────────────────────────

    async def _recent_run_dates(
        self,
        *,
        tenant_id: str,
        user_id: str,
        model_ids: Sequence[str],
        end_date: date,
        limit: int,
    ) -> list[date]:
        """最近 ``limit`` 个有可用推理的交易日（降序）。

        只用于给 ``_window_stats`` 定左界。旧实现按自然日外推 40 天，
        会白扫 2~4 倍的 run/score 行；这里精确截到第 ``lookback + 1`` 个交易日。
        """
        async with get_session(read_only=True) as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT DISTINCT data_trade_date
                        FROM qm_model_inference_runs
                        WHERE tenant_id = :tenant_id AND user_id = :user_id
                          AND model_id = ANY(:model_ids)
                          AND data_trade_date <= :end
                          AND status = 'completed' AND signals_count > 0
                          AND fallback_used = FALSE
                        ORDER BY data_trade_date DESC
                        LIMIT :limit
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                        "model_ids": list(model_ids),
                        "end": end_date,
                        "limit": max(1, int(limit)),
                    },
                )
            ).all()
        return [
            row[0] if isinstance(row[0], date) else date.fromisoformat(str(row[0]))
            for row in rows
        ]

    async def _range_stats(
        self,
        *,
        tenant_id: str,
        user_id: str,
        members: Sequence[Mapping[str, Any]],
        weights: Mapping[str, float] | None,
        method: str,
        trim_count: int,
        min_coverage: int,
        consensus_min_count: int,
        start_date: date,
        end_date: date,
        top_k: int,
    ) -> dict[date, list[dict[str, Any]]]:
        """区间内每日组合分 TopK（``start_date <= day <= end_date``）。

        ``mean`` 走快路：SQL 先按 `(data_trade_date, 原始 symbol)` 聚合
        `SUM(pct×w) / SUM(w)`，Python 再折叠前缀并排序（与 ``_window_stats``
        逐日对拍 diff=0，见 ``test_blend_service``）。逐行正则折叠符号是
        慢路的瓶颈（32 天实测 ~10s / 12.5s），快路约 4s。

        ``median/trimmed`` 无法在折叠前做可合并聚合，复用 ``_window_stats``；
        调用方须先把区间限制在 120 个交易日内。
        """
        if method not in {"mean", "median", "trimmed"}:
            raise BlendError(f"未知组合方法：{method}")
        if method != "mean":
            lookback = max(1, (end_date - start_date).days + 5)
            window = await self._window_stats(
                tenant_id=tenant_id,
                user_id=user_id,
                members=members,
                weights=weights,
                method=method,
                trim_count=trim_count,
                min_coverage=min_coverage,
                consensus_min_count=consensus_min_count,
                end_date=end_date,
                lookback=lookback,
                top_k=top_k,
                symbols=[],
            )
            return {
                day: rows
                for day, rows in (window.get("top_rows") or {}).items()
                if start_date <= day <= end_date
            }

        model_ids = [str(m["model_id"]) for m in members]
        ordered_weights = [
            float((weights or {}).get(model_id, 1.0)) for model_id in model_ids
        ]
        equal_weights = all(
            abs(weight - ordered_weights[0]) < 1e-12 for weight in ordered_weights
        )
        weight_join = ""
        weight_params: dict[str, Any] = {}
        if equal_weights:
            score_expr = "SUM(e.rank_pct)"
            weight_sum_expr = "COUNT(*)"
        else:
            score_expr = "SUM(e.rank_pct * CAST(w.weight AS float8))"
            weight_sum_expr = "SUM(CAST(w.weight AS float8))"
            rows = ", ".join(
                f"(:wm{i}, CAST(:ww{i} AS float8))" for i in range(len(model_ids))
            )
            weight_join = (
                f"JOIN (VALUES {rows}) AS w(model_id, weight)"
                " ON w.model_id = e.model_id"
            )
            for index, (model_id, weight) in enumerate(
                zip(model_ids, ordered_weights, strict=True)
            ):
                weight_params[f"wm{index}"] = model_id
                weight_params[f"ww{index}"] = weight

        cache_key = _range_cache_key(
            tenant_id=tenant_id,
            user_id=user_id,
            members=list(zip(model_ids, ordered_weights, strict=True)),
            method=method,
            trim_count=trim_count,
            min_coverage=min_coverage,
            consensus_min_count=consensus_min_count,
            start_date=start_date,
            end_date=end_date,
            top_k=top_k,
        )
        cached = _range_cache_get(cache_key)
        if cached is not None:
            return cached

        sql = f"""
            WITH {_RUNS_CTE}
            SELECT r.data_trade_date, e.symbol AS raw_symbol,
                   {score_expr} AS wscore,
                   {weight_sum_expr} AS wsum,
                   COUNT(*) AS coverage,
                   COUNT(*) FILTER (WHERE e.rank_pct >= 0.70) AS consensus
            FROM runs r
            JOIN engine_signal_scores e ON e.run_id = r.run_id
            {weight_join}
            WHERE e.tenant_id = :tenant_id AND e.user_id = :user_id
              AND e.rank_pct IS NOT NULL
            GROUP BY r.data_trade_date, e.symbol
        """
        async with get_session(read_only=True) as session:
            rows = (
                await session.execute(
                    text(sql),
                    {
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                        "model_ids": model_ids,
                        "start": start_date,
                        "end": end_date,
                        **weight_params,
                    },
                )
            ).all()

        # 原始形态 → 前缀式：合并 (Σ pct×w, Σw, coverage, consensus)
        merged: dict[date, dict[str, list[float]]] = {}
        for day, raw_symbol, wscore, wsum, coverage, consensus in rows:
            key = day if isinstance(day, date) else date.fromisoformat(str(day))
            prefix = StockCodeUtil.to_prefix(str(raw_symbol)) or str(raw_symbol)
            acc = merged.setdefault(key, {}).get(prefix)
            if acc is None:
                merged[key][prefix] = [
                    float(wscore),
                    float(wsum),
                    int(coverage),
                    int(consensus),
                ]
            else:
                acc[0] += float(wscore)
                acc[1] += float(wsum)
                acc[2] += int(coverage)
                acc[3] += int(consensus)

        out: dict[date, list[dict[str, Any]]] = {}
        for day, symbols in merged.items():
            scored: list[tuple[str, float, int]] = []
            for symbol, acc in symbols.items():
                weight_sum = acc[1]
                coverage = int(acc[2])
                if weight_sum <= 0 or coverage < min_coverage:
                    continue
                if consensus_min_count > 0 and int(acc[3]) < consensus_min_count:
                    continue
                scored.append((symbol, acc[0] / weight_sum, coverage))
            if not scored:
                continue
            scored.sort(key=lambda row: (-row[1], -row[2], row[0]))
            n_total = len(scored)
            ascending = sorted(score for _, score, _ in scored)
            top_rows: list[dict[str, Any]] = []
            for row_rank, (symbol, score, coverage) in enumerate(
                scored[: max(1, int(top_k))], start=1
            ):
                # 与 _window_stats 同口径：降序名次、升序名次、截面分位
                pct = rank_pct_from_rank(bisect_left(ascending, score) + 1, n_total)
                top_rows.append(
                    {
                        "symbol": symbol,
                        "score": float(score),
                        "rank": n_total - bisect_right(ascending, score) + 1,
                        "row_rank": row_rank,
                        "coverage": coverage,
                        "pct": pct,
                        "position": position_of(pct),
                    }
                )
            out[day] = top_rows
        _range_cache_put(cache_key, out)
        return {day: [dict(row) for row in rows] for day, rows in out.items()}

    async def _window_stats(
        self,
        *,
        tenant_id: str,
        user_id: str,
        members: Sequence[Mapping[str, Any]],
        weights: Mapping[str, float] | None,
        method: str,
        trim_count: int,
        min_coverage: int,
        consensus_min_count: int,
        end_date: date,
        lookback: int,
        top_k: int,
        symbols: Sequence[str],
    ) -> dict[str, Any]:
        """窗口内每日组合分与 TopK 集合（只回候选与榜首，控制传输量）。

        分位与聚合都在 SQL 内完成；``method`` 只影响聚合表达式，不拼接用户输入。
        """
        model_ids = [str(m["model_id"]) for m in members]
        ordered_weights = [
            float((weights or {}).get(model_id, 1.0)) for model_id in model_ids
        ]
        equal_weights = all(
            abs(weight - ordered_weights[0]) < 1e-12 for weight in ordered_weights
        )
        weight_join = ""
        weight_params: dict[str, Any] = {}
        if method == "median":
            aggregate = "percentile_cont(0.5) WITHIN GROUP (ORDER BY s.pct)"
            extra_having = ""
        elif method == "trimmed":
            aggregate = (
                "(SELECT AVG(v) FROM ("
                "  SELECT v, ROW_NUMBER() OVER (ORDER BY v) AS rn, COUNT(*) OVER () AS c"
                "  FROM unnest(array_agg(s.pct)) AS u(v)"
                ") z WHERE z.rn > :trim_count AND z.rn <= z.c - :trim_count)"
            )
            extra_having = "AND COUNT(*) >= :min_rows"
        else:
            extra_having = ""
            if equal_weights:
                # 等权是默认面：直接用 AVG，省掉每行 13 次的 jsonb 展开。
                aggregate = "AVG(s.pct)"
            else:
                aggregate = (
                    "SUM(s.pct * CAST(w.weight AS float8))"
                    " / SUM(CAST(w.weight AS float8))"
                )
                rows = ", ".join(
                    f"(:wm{i}, CAST(:ww{i} AS float8))" for i in range(len(model_ids))
                )
                weight_join = (
                    f"JOIN (VALUES {rows}) AS w(model_id, weight)"
                    " ON w.model_id = s.model_id"
                )
                for index, (model_id, weight) in enumerate(
                    zip(model_ids, ordered_weights, strict=True)
                ):
                    weight_params[f"wm{index}"] = model_id
                    weight_params[f"ww{index}"] = weight
        consensus_having = (
            "AND COUNT(*) FILTER (WHERE s.pct >= 0.70) >= :consensus_min_count"
            if consensus_min_count > 0
            else ""
        )
        cache_key = _window_cache_key(
            tenant_id=tenant_id,
            user_id=user_id,
            members=list(zip(model_ids, ordered_weights, strict=True)),
            method=method,
            trim_count=trim_count,
            min_coverage=min_coverage,
            consensus_min_count=consensus_min_count,
            end_date=end_date,
            lookback=lookback,
            top_k=top_k,
            symbols=symbols,
        )
        cached = _window_cache_get(cache_key)
        if cached is not None:
            return cached
        sql = f"""
            WITH {_RUNS_CTE},
            {_SCORED_CTE},
            weighted AS (
              SELECT s.data_trade_date, s.symbol,
                     {aggregate} AS score,
                     COUNT(*) AS coverage
              FROM scored s
              {weight_join}
              GROUP BY s.data_trade_date, s.symbol
              HAVING COUNT(*) >= :min_coverage
                {extra_having}
                {consensus_having}
            ),
            ranked AS (
              SELECT data_trade_date, symbol, score, coverage,
                     ROW_NUMBER() OVER (
                       PARTITION BY data_trade_date
                       ORDER BY score DESC, coverage DESC, symbol ASC
                     ) AS rn,
                     RANK() OVER (
                       PARTITION BY data_trade_date ORDER BY score DESC
                     ) AS score_rank,
                     -- 分位必须用**升序**名次（分最低者 rank=1 → pct=0），
                     -- 与 compute_rank_pct / PG percent_rank() 同口径。
                     -- 用降序名次会把榜首算成 pct=0（「综合分 0.0 · 靠后」）。
                     RANK() OVER (
                       PARTITION BY data_trade_date ORDER BY score ASC
                     ) AS pct_rank,
                     COUNT(*) OVER (PARTITION BY data_trade_date) AS n_total
              FROM weighted
            )
            SELECT data_trade_date, symbol, score, coverage, rn, score_rank,
                   pct_rank, n_total
            FROM ranked
            WHERE rn <= :top_k OR symbol = ANY(:symbols)
            ORDER BY data_trade_date, rn
        """
        run_dates = await self._recent_run_dates(
            tenant_id=tenant_id,
            user_id=user_id,
            model_ids=model_ids,
            end_date=end_date,
            limit=lookback + 1,
        )
        if not run_dates:
            return {"scores": {}, "top": {}, "top_rows": {}, "days": []}
        start = min(run_dates)
        async with get_session(read_only=True) as session:
            rows = (
                await session.execute(
                    text(sql),
                    {
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                        "model_ids": model_ids,
                        "start": start,
                        "end": end_date,
                        "min_coverage": max(1, int(min_coverage)),
                        "min_rows": max(int(min_coverage), 2 * trim_count + 1),
                        "trim_count": int(trim_count),
                        "consensus_min_count": int(consensus_min_count),
                        "top_k": int(top_k),
                        "symbols": list(symbols),
                        **weight_params,
                    },
                )
            ).all()

        by_date: dict[date, dict[str, float]] = {}
        top_by_date: dict[date, set[str]] = {}
        top_rows_by_date: dict[date, list[dict[str, Any]]] = {}
        for day, symbol, score, coverage, rn, score_rank, pct_rank, n_total in rows:
            key = day if isinstance(day, date) else date.fromisoformat(str(day))
            symbol = str(symbol)
            if _finite(score):
                by_date.setdefault(key, {})[symbol] = float(score)
            if int(rn) <= top_k:
                top_by_date.setdefault(key, set()).add(symbol)
                # pct 与 blend_engine.percent_rank_map 同口径：(升序名次-1)/(n-1)；
                # 缺总数为 1 时按 0.0（与 compute_rank_pct 的 n==1 分支一致）。
                pct = rank_pct_from_rank(int(pct_rank), int(n_total))
                top_rows_by_date.setdefault(key, []).append(
                    {
                        "symbol": symbol,
                        "score": float(score) if _finite(score) else None,
                        "rank": int(score_rank),
                        "row_rank": int(rn),
                        "coverage": int(coverage),
                        "pct": pct,
                        "position": position_of(pct),
                    }
                )
        score_days = sorted(day for day in by_date if day < end_date)[-lookback:]
        top_days = sorted(day for day in top_by_date if day <= end_date)[
            -(lookback + 1) :
        ]
        payload = {
            "scores": {day: by_date[day] for day in score_days},
            "top": {day: top_by_date[day] for day in top_days},
            "top_rows": {
                day: sorted(
                    top_rows_by_date.get(day, []), key=lambda row: row["row_rank"]
                )
                for day in top_days
            },
            "days": score_days,
        }
        _window_cache_put(cache_key, payload)
        return _clone_window(payload)

    async def _annotations(
        self,
        *,
        blend_id: str | None,
        rows: Sequence[Mapping[str, Any]],
        window: Mapping[str, Any] | None,
        data_trade_date: date,
    ) -> dict[str, dict[str, Any]]:
        positions = (
            await self.persistence.list_position_history(blend_id) if blend_id else []
        )
        trading_days = await self._trading_days(data_trade_date, need=60)
        by_symbol: dict[str, list[dict[str, Any]]] = {}
        for position in positions:
            by_symbol.setdefault(str(position["symbol"]), []).append(position)
        top = (window or {}).get("top") or {}
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            symbol = str(row["symbol"])
            history = sorted(
                by_symbol.get(symbol, []), key=lambda p: str(p["entry_date"])
            )
            open_rows = [
                p
                for p in history
                if p["entry_date"] <= data_trade_date.isoformat()
                and (
                    p.get("exit_date") is None
                    or p["exit_date"] > data_trade_date.isoformat()
                )
            ]
            held_days = 0
            if open_rows:
                try:
                    entry = date.fromisoformat(str(open_rows[0]["entry_date"]))
                    held_days = sum(
                        1 for day in trading_days if entry < day <= data_trade_date
                    )
                except ValueError:
                    held_days = 0
            out[symbol] = {
                "first_entry_date": history[0]["entry_date"] if history else None,
                "last_entry_date": history[-1]["entry_date"] if history else None,
                "reentry_count": max(0, len(history) - 1),
                "held_days": held_days,
                "consecutive_days_in_top50": consecutive_days_in_topk(
                    top, symbol=symbol, date=data_trade_date
                ),
            }
        return out

    # ── 调仓计划 ────────────────────────────────────────────────────────

    @staticmethod
    async def _calendar_days(start: date, end: date) -> list[date]:
        """``[start, end]`` 内的 A 股交易日（升序，含首尾）。

        日历不可用时退化为自然日（与 ``_trading_days`` 同一兜底口径），
        这样周期视图不会因为日历服务故障直接 500。
        """
        if end < start:
            return []
        try:
            from backend.shared.trading_calendar import trading_days_xcal

            days = trading_days_xcal("CN", start, end + timedelta(days=1))
        except Exception:  # pragma: no cover - 日历不可用时退化为自然日
            days = None
        if not days:
            days = [
                start + timedelta(days=offset)
                for offset in range((end - start).days + 1)
            ]
        return [day for day in days if start <= day <= end]

    @staticmethod
    async def _trading_days(end: date, *, need: int) -> list[date]:
        start = end - timedelta(days=max(30, need * 3))
        try:
            from backend.shared.trading_calendar import trading_days_xcal

            days = trading_days_xcal("CN", start, end + timedelta(days=1))
        except Exception:  # pragma: no cover - 日历不可用时退化为自然日
            days = None
        if not days:
            days = [
                start + timedelta(days=offset)
                for offset in range((end - start).days + 1)
            ]
        return [day for day in days if day <= end]

    @staticmethod
    async def _quotes(
        symbols: Sequence[str],
        data_trade_date: date,
        *,
        lookback_days: int = 20,
        min_adv_days: int = 15,
    ) -> tuple[dict[str, float], dict[str, float], list[str]]:
        """T 日参考价 + ADV20（元）。

        返回 ``(prices, avg_amounts, warnings)``：``prices`` 只取 T 日收盘价；
        ``avg_amounts`` 是最近 ``lookback_days`` 个**有成交额**交易日的均值，
        有效日不足 ``min_adv_days`` 的标的不进映射（计划层会按「成交额缺失」拦截）。
        """
        suffix_to_prefix = {}
        for symbol in symbols:
            suffix = StockCodeUtil.to_suffix(symbol)
            if suffix:
                suffix_to_prefix[suffix] = symbol
        if not suffix_to_prefix:
            return {}, {}, []
        start = data_trade_date - timedelta(days=max(45, int(lookback_days) * 3))
        try:
            from backend.services.engine.data_platform.quantdb_hub import (
                QuantDBDataHub,
            )

            hub = QuantDBDataHub.get_instance()
            if hub is None or not hub.available:
                return {}, {}, ["行情源不可用，计划未校验价格与容量"]
            frame = hub.fetch_daily_kline_batch(
                list(suffix_to_prefix),
                start,
                data_trade_date,
                adjust="none",
            )
        except Exception as exc:  # pragma: no cover - 行情缺失按警告处理
            logger.warning("组合计划读取行情失败: %s", exc)
            return {}, {}, [f"行情读取失败：{exc}"]
        if frame is None or frame.empty:
            return {}, {}, ["当日行情缺失，计划未校验价格与容量"]
        history: dict[str, list[tuple[date, float, float]]] = {}
        for _, row in frame.iterrows():
            prefix = suffix_to_prefix.get(str(row.get("symbol")))
            if not prefix:
                continue
            close = row.get("close")
            amount = row.get("amount")
            # QuantDB daily_unadjusted.amount 单位为万元（实测 2026-10-20）。
            if not (_finite(amount) and float(amount) > 0):
                continue
            raw_day = row.get("trade_date")
            day = raw_day.date() if hasattr(raw_day, "date") else _parse_date(raw_day)
            if day is None or day > data_trade_date:
                continue
            history.setdefault(prefix, []).append(
                (
                    day,
                    float(close) if _finite(close) and float(close) > 0 else 0.0,
                    float(amount) * 10_000.0,
                )
            )
        prices: dict[str, float] = {}
        avg_amounts: dict[str, float] = {}
        for prefix, rows in history.items():
            rows.sort(key=lambda item: item[0])
            window = rows[-max(1, int(lookback_days)) :]
            if len(window) >= max(1, int(min_adv_days)):
                avg_amounts[prefix] = sum(item[2] for item in window) / len(window)
            today = next(
                (item for item in reversed(rows) if item[0] == data_trade_date),
                None,
            )
            if today is not None and today[1] > 0:
                prices[prefix] = today[1]
        return prices, avg_amounts, []

    @staticmethod
    async def _close_history(
        symbols: Sequence[str], start: date, end: date
    ) -> dict[str, dict[date, float]]:
        """窗口内每日前复权收盘价：``symbol → {date: close}``。

        尾盘买入口径：买入/卖出都用当日收盘价，不引入盘中价。跨日/跨月持有
        必须用前复权（``daily_forward``）——``daily_backward`` 是跨分区拼接的
        坏序列（会平滑漂移），不复权遇除权除息会产生假跳空。
        行情源不可用或读取失败返回空映射——损益展示为 `—`，不猜 0。
        """
        suffix_to_prefix: dict[str, str] = {}
        for symbol in symbols:
            suffix = StockCodeUtil.to_suffix(symbol)
            if suffix:
                suffix_to_prefix[suffix] = symbol
        if not suffix_to_prefix or end < start:
            return {}
        try:
            from backend.services.engine.data_platform.quantdb_hub import (
                QuantDBDataHub,
            )

            hub = QuantDBDataHub.get_instance()
            if hub is None or not hub.available:
                return {}
            # 同步 parquet/DuckDB 读取放线程池，避免阻塞 engine 事件循环
            frame = await asyncio.to_thread(
                hub.fetch_daily_kline_batch,
                list(suffix_to_prefix),
                start,
                end,
                adjust="qfq",
            )
        except Exception as exc:  # pragma: no cover - 行情缺失按缺失处理
            logger.warning("滚动建仓读取收盘价失败: %s", exc)
            return {}
        if frame is None or frame.empty:
            return {}
        out: dict[str, dict[date, float]] = {}
        for _, row in frame.iterrows():
            prefix = suffix_to_prefix.get(str(row.get("symbol")))
            if not prefix:
                continue
            raw_day = row.get("trade_date")
            day = raw_day.date() if hasattr(raw_day, "date") else _parse_date(raw_day)
            close = row.get("close")
            if day is None or not _finite(close) or float(close) <= 0:
                continue
            out.setdefault(prefix, {})[day] = float(close)
        return out

    @staticmethod
    async def _amount_history(
        symbols: Sequence[str], start: date, end: date
    ) -> dict[str, dict[date, float]]:
        """窗口内每日成交额（元）：``symbol → {date: amount}``。

        用不复权 ``daily_unadjusted``：成交额本身不受复权影响，且与
        ``stock_daily_latest.amount`` 同源。QuantDB 的 amount 单位是**万元**，
        这里统一 ×10,000 换成元（与 ``_quotes`` 的 ADV20 口径一致）。
        行情源不可用返回空映射，调用方按「成交额不可用」明示降级。
        """
        suffix_to_prefix: dict[str, str] = {}
        for symbol in symbols:
            suffix = StockCodeUtil.to_suffix(symbol)
            if suffix:
                suffix_to_prefix[suffix] = symbol
        if not suffix_to_prefix or end < start:
            return {}
        try:
            from backend.services.engine.data_platform.quantdb_hub import (
                QuantDBDataHub,
            )

            hub = QuantDBDataHub.get_instance()
            if hub is None or not hub.available:
                return {}
            frame = await asyncio.to_thread(
                hub.fetch_daily_kline_batch,
                list(suffix_to_prefix),
                start,
                end,
                adjust="none",
            )
        except Exception as exc:  # pragma: no cover - 行情缺失按缺失处理
            logger.warning("滚动建仓读取成交额失败: %s", exc)
            return {}
        if frame is None or frame.empty:
            return {}
        out: dict[str, dict[date, float]] = {}
        for _, row in frame.iterrows():
            prefix = suffix_to_prefix.get(str(row.get("symbol")))
            if not prefix:
                continue
            raw_day = row.get("trade_date")
            day = raw_day.date() if hasattr(raw_day, "date") else _parse_date(raw_day)
            amount = row.get("amount")
            if day is None or not (_finite(amount) and float(amount) > 0):
                continue
            out.setdefault(prefix, {})[day] = float(amount) * 10_000.0
        return out

    @staticmethod
    async def _risk_flags(
        symbols: Sequence[str], market: str = CN_MARKET
    ) -> tuple[dict[str, str], str | None]:
        """``前缀式 symbol → "ST" / "退市"``（当前截面）+ 数据源错误原因。

        数据源 ``factor_research.data.load_instrument("CN")``（``is_st`` /
        ``is_quit``，名称含 ST 兜底）。截面无时点性：历史重算时只能说明
        「按当前截面判定」，这一点由计划层 warnings 明示。

        **失败不再静默放行**：返回 ``(flags, reason)``，``reason`` 非 None 时
        计划层必须告警并标记 ``data_ok=False``（不可确认），否则「已剔除 ST」
        的硬条件会在数据源故障时悄悄失效。
        """
        if not symbols:
            return {}, None
        key = str(market or CN_MARKET).upper()
        now = time.monotonic()
        cached = _RISK_CACHE.get(key)
        if cached is not None and now - cached[0] < PROFILE_CACHE_TTL_SECONDS:
            all_flags, reason = cached[1], cached[2]
        else:
            all_flags, reason = {}, None
            if key != "CN":
                reason = f"暂不支持 {key} 的风险标记"
            else:
                try:
                    from backend.services.engine.factor_research.data import (
                        load_instrument,
                    )

                    # 全表 parquet 读取是阻塞 IO：放线程池，避免卡事件循环
                    frame = await asyncio.to_thread(load_instrument, "CN")
                except Exception as exc:  # pragma: no cover - 数据源故障路径
                    logger.warning("组合计划读取风险标记失败: %s", exc)
                    frame, reason = None, str(exc) or type(exc).__name__
                if reason is None and (frame is None or getattr(frame, "empty", True)):
                    reason = "风险标记源为空"
                if reason is None:
                    all_flags = ModelBlendService._flags_from_instruments(frame)
            _RISK_CACHE[key] = (now, all_flags, reason)
            if len(_RISK_CACHE) > 8:  # 市场数量有限；防御性上限
                oldest = min(_RISK_CACHE.items(), key=lambda item: item[1][0])[0]
                _RISK_CACHE.pop(oldest, None)
        wanted = {
            StockCodeUtil.to_prefix(str(symbol)) or str(symbol) for symbol in symbols
        }
        return {sym: flag for sym, flag in all_flags.items() if sym in wanted}, reason

    @staticmethod
    def _flags_from_instruments(frame: Any) -> dict[str, str]:
        """从全量证券主表提取 ST/退市标记（纯函数，便于单测）。"""
        has_st = "is_st" in frame.columns
        has_quit = "is_quit" in frame.columns
        flags: dict[str, str] = {}

        def truthy(value: Any) -> bool:
            if value is True:
                return True
            if value is False or value is None:
                return False
            try:
                number = float(value)
            except (TypeError, ValueError):
                return False
            # NaN 必须视为「未知」：bool(float('nan')) 是 True，
            # 不拦会把整个候选池误标成 ST/退市，计划直接清空。
            if math.isnan(number):
                return False
            return bool(number)

        for _, row in frame.iterrows():
            prefix = StockCodeUtil.to_prefix(str(row.get("symbol") or ""))
            if not prefix:
                continue
            name = str(row.get("name") or "")
            is_st = truthy(row.get("is_st")) if has_st else False
            if not is_st and "ST" in name.upper():
                is_st = True
            if has_quit and truthy(row.get("is_quit")):
                flags[prefix] = "退市"
            elif is_st:
                flags[prefix] = "ST"
        return flags

    async def plan(
        self,
        blend: Mapping[str, Any],
        *,
        tenant_id: str,
        user_id: str,
        data_trade_date: date | None = None,
        plan_config: PlanConfig | None = None,
        persist: bool = True,
    ) -> dict[str, Any]:
        cfg = plan_config or PlanConfig(
            top_k=int(blend.get("target_positions") or 50),
            entry_per_day=int(blend.get("entry_per_day") or 5),
            hold_days=int(blend.get("rolling_window") or 10),
            target_positions=int(blend.get("target_positions") or 50),
        )
        blend_id = str(blend["blend_id"])
        members = await self.members_from_blend(blend)
        member_ids = [str(m["model_id"]) for m in members]
        min_coverage = int(blend.get("min_coverage") or 2)
        target = data_trade_date or await self.latest_common_date(
            tenant_id=tenant_id,
            user_id=user_id,
            model_ids=member_ids,
            required=min_coverage,
        )
        if target is None:
            raise BlendError(
                "所选模型没有共同数据日：请先对成员做串行补推，或调低最低覆盖数",
                code="no_common_date",
            )

        weights = _weights_from_members(blend, members)
        ranking = await self.ranking(
            tenant_id=tenant_id,
            user_id=user_id,
            members=members,
            data_trade_date=target,
            method=str(blend.get("method") or "mean"),
            weights=weights,
            min_coverage=min_coverage,
            trim_count=int(blend.get("trim_count") or 2),
            consensus_min_count=int(blend.get("consensus_min_count") or 0),
            top=max(cfg.top_k, cfg.target_positions),
        )
        rows = _rows_from_payload(ranking["ranking"])
        positions_payload = await self.persistence.list_open_positions(
            blend_id, as_of=target
        )
        positions = [
            PaperPosition(
                symbol=str(p["symbol"]),
                entry_date=date.fromisoformat(str(p["entry_date"])),
                shares=int(p.get("shares") or 0),
                target_value=p.get("target_value"),
            )
            for p in positions_payload
        ]
        candidate_symbols = [row.symbol for row in rows] + [p.symbol for p in positions]
        window = await self._window_stats(
            tenant_id=tenant_id,
            user_id=user_id,
            members=members,
            weights=weights,
            method=str(blend.get("method") or "mean"),
            trim_count=int(blend.get("trim_count") or 2),
            min_coverage=min_coverage,
            consensus_min_count=int(blend.get("consensus_min_count") or 0),
            end_date=target,
            lookback=cfg.lookback,
            top_k=max(cfg.top_k, cfg.target_positions),
            symbols=candidate_symbols,
        )
        prices, avg_amounts, quote_warnings = await self._quotes(
            candidate_symbols,
            target,
            lookback_days=cfg.adv_lookback_days,
            min_adv_days=cfg.min_adv_days,
        )
        risk_flags: dict[str, str] = {}
        risk_error: str | None = None
        if cfg.exclude_st:
            risk_flags, risk_error = await self._risk_flags(
                candidate_symbols, market=str(blend.get("market") or CN_MARKET)
            )
        target_date = await self._next_trading_day(target)
        paper = build_rebalance_plan(
            data_trade_date=target,
            target_date=target_date,
            ranking=rows,
            history=window["scores"],
            trading_days=await self._trading_days(target, need=cfg.lookback + 1),
            positions=positions,
            config=cfg,
            prices=prices,
            avg_amounts=avg_amounts,
            risk_flags=risk_flags,
        )
        weight_map = weights or {}
        config_digest = config_hash(
            members=[
                (str(m["model_id"]), float(weight_map.get(m["model_id"], 1.0)))
                for m in members
            ],
            min_coverage=min_coverage,
            method=str(blend.get("method") or "mean"),
            trim_count=int(blend.get("trim_count") or 2),
            consensus_min_count=int(blend.get("consensus_min_count") or 0),
            top_k=cfg.top_k,
            hold_days=cfg.hold_days,
            entry_per_day=cfg.entry_per_day,
            target_positions=cfg.target_positions,
            cash_buffer=cfg.cash_buffer,
            adv_participation=cfg.adv_participation,
            exit_delta=cfg.exit_delta,
            lookback=cfg.lookback,
            min_lookback_days=cfg.min_lookback_days,
            capital=cfg.capital,
            min_adv_yi=cfg.min_adv_yi,
            exclude_st=cfg.exclude_st,
            adv_lookback_days=cfg.adv_lookback_days,
            min_adv_days=cfg.min_adv_days,
        )
        digest_positions = positions_hash(positions)
        payload = paper.to_dict()
        warnings = list(payload.get("warnings") or []) + quote_warnings
        # data_ok=False 表示行情/风险标记缺失：计划可以看，但**不可确认**，
        # 否则「已剔除 ST / 已按成交额匹配」的硬条件会在故障时静默失效。
        data_ok = not quote_warnings
        if cfg.exclude_st:
            if risk_error:
                warnings.append(f"风险标记不可用，未剔除 ST/退市：{risk_error}")
                data_ok = False
            else:
                warnings.append("ST/退市按当前截面判定，历史重算无时点性")
        payload["warnings"] = warnings
        payload["data_ok"] = data_ok
        payload["summary"]["data_ok"] = data_ok
        # 名称/行业/市值与榜单同源；计划行缺失时仍保持 None（前端显示 —）
        names = await self._stock_names()
        profiles = await self._stock_profiles(str(blend.get("market") or CN_MARKET))
        for section in ("entries", "holds", "exits"):
            for row in payload.get(section) or []:
                symbol = str(row.get("symbol"))
                profile = profiles.get(symbol) or {}
                row.setdefault("name", names.get(symbol) or None)
                row.setdefault("industry", profile.get("industry"))
                row.setdefault("market_cap_yi", profile.get("market_cap_yi"))
        if not persist:
            payload.update(
                {
                    "plan_id": None,
                    "blend_id": blend_id,
                    "config_hash": config_digest,
                    "positions_hash": digest_positions,
                    "confirmed_at": None,
                    "created_at": None,
                    "blend": {
                        "blend_id": blend_id,
                        "name": blend.get("name"),
                        "method": blend.get("method"),
                        "horizon_days": blend.get("horizon_days"),
                    },
                }
            )
            return payload
        stored = await self.persistence.upsert_plan(
            blend_id=blend_id,
            tenant_id=tenant_id,
            user_id=user_id,
            data_trade_date=target,
            target_date=target_date,
            config_hash=config_digest,
            positions_hash=digest_positions,
            params=payload["params"],
            summary=payload["summary"],
            entries=payload["entries"],
            holds=payload["holds"],
            exits=payload["exits"],
        )
        stored["warnings"] = payload["warnings"]
        stored["data_ok"] = data_ok
        stored["blend"] = {
            "blend_id": blend_id,
            "name": blend.get("name"),
            "method": blend.get("method"),
            "horizon_days": blend.get("horizon_days"),
        }
        return stored

    async def confirm_plan(
        self,
        blend: Mapping[str, Any],
        plan: Mapping[str, Any],
        *,
        tenant_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        if plan.get("confirmed_at"):
            raise BlendError("计划已确认", status_code=409, code="plan_confirmed")
        summary = (
            plan.get("summary") if isinstance(plan.get("summary"), Mapping) else {}
        )
        if plan.get("data_ok") is False or summary.get("data_ok") is False:
            raise BlendError(
                "计划数据不完整（行情或 ST/退市标记缺失），请重新生成后再确认",
                status_code=409,
                code="plan_data_degraded",
            )
        plan_date = date.fromisoformat(str(plan["data_trade_date"]))
        allowed = await self._recent_trading_days()
        if plan_date not in allowed:
            raise BlendError(
                "计划已过期（仅允许最近 2 个交易日）",
                status_code=409,
                code="plan_expired",
            )
        blend_id = str(blend["blend_id"])
        current = await self.persistence.list_open_positions(blend_id)
        current_hash = positions_hash(
            [
                PaperPosition(
                    symbol=str(p["symbol"]),
                    entry_date=date.fromisoformat(str(p["entry_date"])),
                    shares=int(p.get("shares") or 0),
                )
                for p in current
            ]
        )
        if current_hash != plan.get("positions_hash"):
            raise BlendError(
                "纸面台账已变化，请重新生成计划",
                status_code=409,
                code="stale_positions",
            )
        try:
            confirmed = await self.persistence.confirm_plan(
                plan=plan,
                target_date=date.fromisoformat(str(plan["target_date"])),
            )
        except Exception as exc:  # noqa: BLE001 - 并发确认靠唯一索引兜底
            if "uq_blend_plan_confirmed" in str(exc) or "unique" in str(exc).lower():
                raise BlendError(
                    "同一数据日已有确认计划", status_code=409, code="plan_confirmed"
                ) from exc
            raise
        if confirmed is False:
            # 并发确认：另一请求先抢到了 plan 行（rowcount 0）
            raise BlendError("计划已确认", status_code=409, code="plan_confirmed")
        refreshed = await self.persistence.get_plan(
            str(plan["plan_id"]), tenant_id=tenant_id, user_id=user_id
        )
        return refreshed or {}

    async def annotate_confirmable(
        self, plans: Sequence[dict[str, Any] | None]
    ) -> None:
        """给计划响应补 ``can_confirm``，与 confirm_plan 共用同一份日历口径。

        前端原先按 ``/dates`` 序位判断过期；缺评分日时会与后端「最近 2 个交易日」
        错位，且浏览器时钟（宿主 JST）与容器时钟不一致。时间判定统一放服务端。
        """
        allowed = {day.isoformat() for day in await self._recent_trading_days()}
        for plan in plans:
            if plan is None:
                continue
            plan_date = str(plan.get("data_trade_date") or "")[:10]
            summary = (
                plan.get("summary") if isinstance(plan.get("summary"), Mapping) else {}
            )
            plan["can_confirm"] = (
                bool(plan.get("plan_id"))
                and not plan.get("confirmed_at")
                and plan_date in allowed
                and plan.get("data_ok") is not False
                and summary.get("data_ok") is not False
            )

    @staticmethod
    async def _recent_trading_days() -> set[date]:
        today = date.today()
        try:
            from backend.shared.trading_calendar import calendar_service

            latest = await calendar_service.prev_trading_day(
                market=CN_MARKET,
                trade_date=today + timedelta(days=1),
                tenant_id="default",
                user_id="system",
            )
            previous = await calendar_service.prev_trading_day(
                market=CN_MARKET,
                trade_date=latest,
                tenant_id="default",
                user_id="system",
            )
            return {latest, previous}
        except Exception:  # pragma: no cover - 日历不可用退化为自然日
            return {today, today - timedelta(days=1)}

    async def members_from_blend(
        self, blend: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        models = await self._normalized_models(
            tenant_id=str(blend["tenant_id"]),
            user_id=str(blend["user_id"]),
            market=str(blend.get("market") or CN_MARKET),
        )
        by_id = {m["model_id"]: m for m in models}
        members = []
        for member in blend.get("members") or []:
            model_id = str(member["model_id"])
            if model_id not in by_id:
                raise BlendError(
                    f"组合成员已不可用：{model_id}", code="member_unavailable"
                )
            members.append(by_id[model_id])
        return members

    @staticmethod
    async def _next_trading_day(day: date) -> date:
        try:
            from backend.shared.trading_calendar import calendar_service

            return await calendar_service.next_trading_day(
                market=CN_MARKET, trade_date=day, tenant_id="default", user_id="system"
            )
        except Exception:  # pragma: no cover - 日历不可用时退化为次日
            return day + timedelta(days=1)


# ── 小工具（纯函数） ─────────────────────────────────────────────────────


def _validate_weights(
    members: Sequence[Mapping[str, Any]], weights: Mapping[str, float] | None
) -> None:
    if weights is None:
        return
    allowed = {str(m["model_id"]) for m in members}
    unknown = set(weights) - allowed
    if unknown:
        raise BlendError(f"权重包含非成员模型：{', '.join(sorted(unknown)[:5])}")
    for model_id in allowed:
        value = float(weights.get(model_id, 1.0))
        if not math.isfinite(value) or value <= 0:
            raise BlendError(f"成员权重必须是正的有限数：{model_id}={value}")


def _weights_from_members(
    blend: Mapping[str, Any], members: Sequence[Mapping[str, Any]]
) -> dict[str, float] | None:
    stored = {
        str(m["model_id"]): float(m.get("weight") or 1.0)
        for m in blend.get("members") or []
    }
    if str(blend.get("weighting") or "equal") == "equal":
        return None
    return {str(m["model_id"]): stored.get(str(m["model_id"]), 1.0) for m in members}


def _rows_from_payload(payload: Sequence[Mapping[str, Any]]) -> list[BlendRow]:
    """API 行 → 纯函数行（计划需要 rank/score/pct/position）。"""
    rows = []
    for item in payload:
        rows.append(
            BlendRow(
                symbol=str(item["symbol"]),
                score=float(item["score"]),
                rank=int(item["rank"]),
                coverage=int(item["coverage"]),
                dispersion=item.get("dispersion"),
                pct=item.get("pct"),
                research_score=item.get("research_score"),
                position=str(item.get("position") or position_of(item.get("pct"))),
                contributions=dict(item.get("contributions") or {}),
                missing_members=tuple(item.get("missing_members") or ()),
            )
        )
    return rows
