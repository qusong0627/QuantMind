"""Three-tier direction quotas adapted from frozen AutoAlpha
4aaeda8b0e58477085762c1ac2e7faa7bdab096c (alpha_agent/direction_quota.py, T-FE-10).

Proxy model v1 = Jeffreys-smoothed success rate with a saturation penalty
(deliberately no LightGBM at the current run volume). Saturation counts come
from the owner-scoped materialization ledger (alpha_factor_materializations,
90-day rejected_duplicate hits), never from same-round statistical dedup.

Slot allocation is reproducible: every pick carries a strict evidence object
(pool, weights, seed, picked, score snapshot, fallback reason) that replays via
``Random(seed).choices(pool, weights)``. Picking more than the candidate count
dispatches the full set without inventing a sampling seed. Quota input failures
never block task creation: allocation degrades to blankness-weighted sampling
flagged ``quota_fallback``. Identity errors are raised before any fallback.
"""

from __future__ import annotations

import logging
import platform
import random
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import text

from backend.shared.database_manager_v2 import get_session
from backend.shared.owner_scope import OwnerScope
from backend.shared.utc_datetime import to_utc_iso, utc_now
from .direction_sampling import (
    WEIGHTING_BLANKNESS,
    blankness_weights,
    clean_directions,
    sample_weighted_directions_n,
)

logger = logging.getLogger(__name__)

MODE_QUOTA = "quota"
TIER_EXPLORE = "explore"
TIER_EXPLOIT = "exploit"
TIER_MUTATE = "mutate"
TIER_ORDER = (TIER_EXPLORE, TIER_EXPLOIT, TIER_MUTATE)

#: 配额比例（来源 D2 决策）：名额切分 = floor + 余数给 exploit。
QUOTA_EXPLORE = 0.3
QUOTA_EXPLOIT = 0.5
QUOTA_MUTATE = 0.2

WEIGHTING_PROXY = "proxy_score"
WEIGHTING_UNIFORM = "uniform"

FALLBACK_SATURATED = "saturated"
FALLBACK_MUTATE_EMPTY = "mutate_empty"

JEFFREYS_SUCCESS = 0.5
JEFFREYS_TOTAL = 1.0

SAT_PENALTY_CAP = 0.5
SAT_PENALTY_DIVISOR = 4.0
SATURATED_MIN_DUP_HITS_90D = 2

EXPLOIT_TOPK = 6
RECENT_SUCCESS_TASKS = 5
DEFAULT_PREVIEW_N = 3

SATURATION_WINDOW_DAYS = 90
_SEED_RANGE = 2**31

_TERMINAL_STATUSES = ("completed", "failed", "cancelled")


@dataclass(frozen=True)
class DirectionScore:
    """One direction score row shared by preview and allocation."""

    direction: str
    attempts: int
    n_tasks: int
    n_success: int
    p: float
    dup_hits_90d: int
    sat_penalty: float
    score: float
    saturated: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "attempts": self.attempts,
            "n_tasks": self.n_tasks,
            "n_success": self.n_success,
            "p": self.p,
            "dup_hits_90d": self.dup_hits_90d,
            "sat_penalty": self.sat_penalty,
            "score": self.score,
            "saturated": self.saturated,
        }


def proxy_success_rate(*, n_success: int, n_tasks: int) -> float:
    """Jeffreys-smoothed success rate; dirty inputs clamp to zero."""
    ns = max(0, int(n_success))
    nt = max(0, int(n_tasks))
    return min(1.0, (ns + JEFFREYS_SUCCESS) / (nt + JEFFREYS_TOTAL))


def saturation_penalty(dup_hits_90d: int) -> float:
    """``min(0.5, dup_hits_90d / 4)``; a missing count stays zero, never invented."""
    return min(SAT_PENALTY_CAP, max(0, int(dup_hits_90d)) / SAT_PENALTY_DIVISOR)


def build_score(
    *,
    direction: str,
    attempts: int,
    n_tasks: int,
    n_success: int,
    dup_hits_90d: int,
) -> DirectionScore:
    dup = max(0, int(dup_hits_90d))
    p = proxy_success_rate(n_success=n_success, n_tasks=n_tasks)
    sat = saturation_penalty(dup)
    return DirectionScore(
        direction=str(direction),
        attempts=max(0, int(attempts)),
        n_tasks=max(0, int(n_tasks)),
        n_success=max(0, int(n_success)),
        p=p,
        dup_hits_90d=dup,
        sat_penalty=sat,
        score=p * (1.0 - sat),
        saturated=dup >= SATURATED_MIN_DUP_HITS_90D,
    )


def split_slots(n: int) -> dict[str, int]:
    """Split N slots by the quota ratios: floor + remainder to exploit."""
    total = max(0, int(n))
    raw = [int(total * q) for q in (QUOTA_EXPLORE, QUOTA_EXPLOIT, QUOTA_MUTATE)]
    raw[1] += total - sum(raw)
    return dict(zip(TIER_ORDER, raw, strict=True))


@dataclass(frozen=True)
class TierPlan:
    """A tier's preview pool; allocation consumes shrinking versions of it."""

    tier: str
    pool: tuple[str, ...]
    weighting: str
    fallback: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "pool": list(self.pool),
            "weighting": self.weighting,
            "fallback": self.fallback,
        }


def _topk(dirs: Sequence[str], by_dir: Mapping[str, DirectionScore]) -> list[str]:
    return sorted(dirs, key=lambda d: (-by_dir[d].score, d))[:EXPLOIT_TOPK]


def _blankness_pool(pool, by_dir):
    weights = blankness_weights([by_dir[d].attempts for d in pool])
    return pool, weights, WEIGHTING_BLANKNESS, "attempts"


def _exploit_pool(pool, by_dir):
    scores = [by_dir[d].score for d in pool]
    total = sum(scores)
    weights = [s / total for s in scores] if total > 0 else [1.0] * len(pool)
    return pool, weights, WEIGHTING_PROXY, "score"


def _uniform_pool(pool, by_dir):
    return pool, [1.0] * len(pool), WEIGHTING_UNIFORM, "attempts"


def _resolve_pool(tier, remaining, by_dir, recent_dirs):
    """One tier's pool and weights on the current remaining set."""
    base = list(remaining)
    if not base:
        raise ValueError("方向配额取池需要至少一个候选")
    unsat = [d for d in base if not by_dir[d].saturated]

    if tier == TIER_EXPLORE:
        if unsat:
            return (*_blankness_pool(unsat, by_dir), None)
        return (*_blankness_pool(base, by_dir), FALLBACK_SATURATED)

    if tier == TIER_EXPLOIT:
        pool = _topk(unsat, by_dir)
        if pool:
            return (*_exploit_pool(pool, by_dir), None)
        return (*_exploit_pool(_topk(base, by_dir), by_dir), FALLBACK_SATURATED)

    recent = set(recent_dirs)
    mut = [d for d in base if d in recent]
    mut_unsat = [d for d in mut if not by_dir[d].saturated]
    if mut_unsat:
        return (*_uniform_pool(mut_unsat, by_dir), None)
    if mut:
        return (*_uniform_pool(mut, by_dir), FALLBACK_SATURATED)
    pool = _topk(unsat, by_dir)
    if pool:
        return (*_exploit_pool(pool, by_dir), FALLBACK_MUTATE_EMPTY)
    return (
        *_exploit_pool(_topk(base, by_dir), by_dir),
        FALLBACK_MUTATE_EMPTY,
    )


def plan_tiers(scores, recent_success_dirs) -> dict[str, TierPlan]:
    by_dir = {s.direction: s for s in scores}
    remaining = [s.direction for s in scores]
    if not remaining:
        raise ValueError("plan_tiers 需要至少一个候选方向")
    plans: dict[str, TierPlan] = {}
    for tier in TIER_ORDER:
        pool, _w, weighting, _vk, fallback = _resolve_pool(
            tier, remaining, by_dir, recent_success_dirs
        )
        plans[tier] = TierPlan(
            tier=tier, pool=tuple(pool), weighting=weighting, fallback=fallback
        )
    return plans


def _allocate_pure(
    directions: Sequence[str],
    n: int,
    scores: Sequence[DirectionScore],
    *,
    recent_success_dirs: Sequence[str],
    market: str,
    universe: str,
) -> list[tuple[str, dict[str, Any]]]:
    by_dir = {s.direction: s for s in scores}
    missing = [d for d in directions if d not in by_dir]
    if missing:
        raise ValueError(f"打分表缺候选方向：{missing[:3]}")
    snapshot = [s.to_dict() for s in scores]
    slots = split_slots(n)
    remaining = list(directions)
    picks: list[tuple[str, dict[str, Any]]] = []
    for tier in TIER_ORDER:
        for _ in range(slots[tier]):
            pool, weights, weighting, value_key, fallback = _resolve_pool(
                tier, remaining, by_dir, recent_success_dirs
            )
            seed = random.SystemRandom().randrange(_SEED_RANGE)
            picked = random.Random(seed).choices(pool, weights=weights, k=1)[0]
            meta: dict[str, Any] = {
                "schema_version": 1,
                "mode": MODE_QUOTA,
                "tier": tier,
                "weighting": weighting,
                "seed": seed,
                "picked": picked,
                "scope": {
                    "market": market,
                    "universe": universe,
                    "algorithm": "rdagent",
                },
                "counted_statuses": "all",
                "history_source": "alpha_mining_tasks",
                "dup_source": "alpha_factor_materializations",
                "rng": "python.random.Random.choices",
                "python_version": platform.python_version(),
                "captured_at": to_utc_iso(utc_now()),
                "candidates": [
                    {
                        "direction": d,
                        "value": (
                            by_dir[d].attempts
                            if value_key == "attempts"
                            else by_dir[d].score
                        ),
                        "weight": w,
                    }
                    for d, w in zip(pool, weights, strict=True)
                ],
                "scores_snapshot": snapshot,
            }
            if fallback:
                meta["tier_fallback"] = fallback
            picks.append((picked, meta))
            remaining.remove(picked)
    return picks


async def _blankness_fallback(
    clean, n, *, owner: OwnerScope, market: str, universe: str
):
    """Quota inputs unavailable: blankness sampling flagged quota_fallback."""
    pairs = await sample_weighted_directions_n(
        clean, n, owner=owner, market=market, universe=universe
    )
    return [
        (direction, ({**meta, "quota_fallback": True} if meta else None))
        for direction, meta in pairs
    ]


@dataclass(frozen=True)
class QuotaInputs:
    scores: tuple[DirectionScore, ...]
    recent_success_dirs: tuple[str, ...]


OutcomesReader = Callable[..., Awaitable[Mapping[str, tuple[int, int, int]]]]
RecentsReader = Callable[..., Awaitable[Sequence[str]]]
DupHitsReader = Callable[..., Awaitable[Mapping[str, int]]]


def _validate_scope(owner: OwnerScope, market: str, universe: str) -> None:
    if not isinstance(owner, OwnerScope) or market != "a_share" or not universe:
        raise ValueError("完整本人A股股票池身份是必需的")


async def _read_direction_outcomes(
    directions: Sequence[str] | None,
    *,
    owner: OwnerScope,
    market: str,
    universe: str,
) -> dict[str, tuple[int, int, int]]:
    """本人×市场×股票池按方向聚合：attempts（全状态）/终态数/成功数。

    attempts 与空白度口径同源（count_by_direction：含排队/失败/取消/归档）；
    n_success = completed 且仍有未归档因子（与经验记忆的可用语义同域）。
    """
    _validate_scope(owner, market, universe)
    ids = None if directions is None else [d for d in directions if d]
    if ids is not None and not ids:
        return {}
    sql = (
        "SELECT task.direction, count(*) AS attempts, "
        "count(*) FILTER (WHERE task.status = ANY(:terminal)) AS n_tasks, "
        "count(*) FILTER (WHERE task.status='completed' AND EXISTS ("
        "SELECT 1 FROM rd_agent_factors f WHERE f.tenant_id=task.tenant_id "
        "AND f.user_id=task.user_id AND f.metadata_json->>'task_id'=task.task_id "
        "AND f.archived_at IS NULL)) AS n_success "
        "FROM alpha_mining_tasks task "
        "LEFT JOIN alpha_mining_task_specs spec ON spec.tenant_id=task.tenant_id "
        "AND spec.user_id=task.user_id AND spec.task_id=task.task_id "
        "WHERE task.tenant_id=:tenant_id AND task.user_id=:user_id "
        "AND task.market=:market AND task.universe=:universe "
        "AND COALESCE(spec.algorithm,'rdagent')='rdagent' "
        "AND task.direction <> ''"
    )
    params: dict[str, Any] = {
        **owner.sql_params(),
        "market": market,
        "universe": universe,
        "terminal": list(_TERMINAL_STATUSES),
    }
    if ids is not None:
        sql += " AND task.direction=ANY(CAST(:directions AS TEXT[]))"
        params["directions"] = ids
    sql += " GROUP BY task.direction"
    async with get_session(read_only=True) as session:
        await session.execute(text("SET LOCAL statement_timeout='5s'"))
        rows = (await session.execute(text(sql), params)).mappings().all()
    return {
        str(row["direction"]): (
            int(row["attempts"]),
            int(row["n_tasks"]),
            int(row["n_success"]),
        )
        for row in rows
    }


async def _read_recent_success_dirs(
    *, owner: OwnerScope, market: str, universe: str, limit: int = RECENT_SUCCESS_TASKS
) -> list[str]:
    """最近 limit 个成功任务的方向（按任务行数限额，去重保序）。"""
    _validate_scope(owner, market, universe)
    async with get_session(read_only=True) as session:
        await session.execute(text("SET LOCAL statement_timeout='5s'"))
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT task.direction FROM alpha_mining_tasks task "
                        "LEFT JOIN alpha_mining_task_specs spec "
                        "ON spec.tenant_id=task.tenant_id AND spec.user_id=task.user_id "
                        "AND spec.task_id=task.task_id "
                        "WHERE task.tenant_id=:tenant_id AND task.user_id=:user_id "
                        "AND task.market=:market AND task.universe=:universe "
                        "AND COALESCE(spec.algorithm,'rdagent')='rdagent' "
                        "AND task.status='completed' AND task.direction <> '' "
                        "AND EXISTS (SELECT 1 FROM rd_agent_factors f "
                        "WHERE f.tenant_id=task.tenant_id AND f.user_id=task.user_id "
                        "AND f.metadata_json->>'task_id'=task.task_id "
                        "AND f.archived_at IS NULL) "
                        "ORDER BY task.completed_at DESC NULLS LAST,"
                        "task.created_at DESC,task.task_id DESC LIMIT :lim"
                    ),
                    {
                        **owner.sql_params(),
                        "market": market,
                        "universe": universe,
                        "lim": int(limit),
                    },
                )
            )
            .mappings()
            .all()
        )
    out: list[str] = []
    for row in rows:
        direction = str(row["direction"] or "")
        if direction and direction not in out:
            out.append(direction)
    return out


async def _read_dup_hits(
    *, owner: OwnerScope, cutoff
) -> dict[str, int]:
    """物化台账 → {direction: 90日 rejected_duplicate 命中}；缺的方向 = 真零。"""
    async with get_session(read_only=True) as session:
        await session.execute(text("SET LOCAL statement_timeout='5s'"))
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT direction,count(*) AS hits FROM "
                        "alpha_factor_materializations WHERE tenant_id=:tenant_id "
                        "AND user_id=:user_id AND status='rejected_duplicate' "
                        "AND created_at >= :cutoff GROUP BY direction"
                    ),
                    {
                        **owner.sql_params(),
                        "cutoff": cutoff,
                    },
                )
            )
            .mappings()
            .all()
        )
    return {str(row["direction"]): int(row["hits"]) for row in rows}


async def _load_inputs(
    directions,
    *,
    owner: OwnerScope,
    market: str,
    universe: str,
    outcomes_reader=None,
    recents_reader=None,
    dup_reader=None,
) -> QuotaInputs:
    _validate_scope(owner, market, universe)
    outcomes = await (outcomes_reader or _read_direction_outcomes)(
        directions, owner=owner, market=market, universe=universe
    )
    recents = await (recents_reader or _read_recent_success_dirs)(
        owner=owner, market=market, universe=universe
    )
    dup = await (dup_reader or _read_dup_hits)(
        owner=owner, cutoff=utc_now() - timedelta(days=SATURATION_WINDOW_DAYS)
    )
    keys = sorted(outcomes) if directions is None else list(directions)
    scores = []
    for direction in keys:
        attempts, n_tasks, n_success = outcomes.get(direction, (0, 0, 0))
        scores.append(
            build_score(
                direction=direction,
                attempts=attempts,
                n_tasks=n_tasks,
                n_success=n_success,
                dup_hits_90d=int(dup.get(direction, 0)),
            )
        )
    return QuotaInputs(
        scores=tuple(scores),
        recent_success_dirs=tuple(str(d) for d in recents),
    )


async def allocate(
    directions: Sequence[str],
    n: int,
    *,
    owner: OwnerScope,
    market: str,
    universe: str,
    outcomes_reader: OutcomesReader | None = None,
    recents_reader: RecentsReader | None = None,
    dup_reader: DupHitsReader | None = None,
) -> list[tuple[str, dict[str, Any] | None]]:
    """配额分配 N 条方向：``[(direction, meta|None)]``（顺序 = 槽位顺序）。

    N ≥ 候选数 → 全集直派 meta=None；读取失败 → 空白度加权 + quota_fallback
    标注（fail-open，不拦任务创建、不假装配额成功）。
    """
    _validate_scope(owner, market, universe)
    clean = clean_directions(directions)
    if not clean:
        raise ValueError("allocate 需要至少一个非空方向")
    total = max(1, int(n))
    if total >= len(clean):
        return [(direction, None) for direction in clean]
    try:
        inputs = await _load_inputs(
            clean,
            owner=owner,
            market=market,
            universe=universe,
            outcomes_reader=outcomes_reader,
            recents_reader=recents_reader,
            dup_reader=dup_reader,
        )
        return _allocate_pure(
            clean,
            total,
            inputs.scores,
            recent_success_dirs=inputs.recent_success_dirs,
            market=market,
            universe=universe,
        )
    except Exception as exc:  # noqa: BLE001 - 配额输入失败不许拦任务创建
        logger.warning(
            "[direction-quota] 配额输入不可用，退随机空白度加权: %s", type(exc).__name__
        )
        return await _blankness_fallback(
            clean, total, owner=owner, market=market, universe=universe
        )


async def preview(
    *,
    owner: OwnerScope,
    market: str,
    universe: str,
    directions: Sequence[str] = (),
    n: int = DEFAULT_PREVIEW_N,
    outcomes_reader: OutcomesReader | None = None,
    recents_reader: RecentsReader | None = None,
    dup_reader: DupHitsReader | None = None,
) -> dict[str, Any]:
    """只读预览：打分表 + 名额切分 + 三档预演；失败时 available=false。"""
    try:
        _validate_scope(owner, market, universe)
        clean = clean_directions(directions or ())
    except ValueError as exc:
        return {
            "available": False,
            "market": market,
            "universe": universe,
            "n": max(1, int(n)),
            "quota": {
                "explore": QUOTA_EXPLORE,
                "exploit": QUOTA_EXPLOIT,
                "mutate": QUOTA_MUTATE,
            },
            "slots": split_slots(max(1, int(n))),
            "saturated_threshold": SATURATED_MIN_DUP_HITS_90D,
            "recent_success_tasks": RECENT_SUCCESS_TASKS,
            "directions": [],
            "tiers": None,
            "recent_success_dirs": [],
            "error": str(exc),
        }
    total = max(1, int(n))
    base: dict[str, Any] = {
        "available": True,
        "market": market,
        "universe": universe,
        "n": total,
        "quota": {
            "explore": QUOTA_EXPLORE,
            "exploit": QUOTA_EXPLOIT,
            "mutate": QUOTA_MUTATE,
        },
        "slots": split_slots(total),
        "saturated_threshold": SATURATED_MIN_DUP_HITS_90D,
        "recent_success_tasks": RECENT_SUCCESS_TASKS,
        "dup_source": "alpha_factor_materializations",
    }
    try:
        inputs = await _load_inputs(
            clean or None,
            owner=owner,
            market=market,
            universe=universe,
            outcomes_reader=outcomes_reader,
            recents_reader=recents_reader,
            dup_reader=dup_reader,
        )
        rows = (
            inputs.scores
            if clean
            else tuple(sorted(inputs.scores, key=lambda s: (-s.score, s.direction)))
        )
        tiers = (
            {
                tier: plan.to_dict()
                for tier, plan in plan_tiers(
                    inputs.scores, inputs.recent_success_dirs
                ).items()
            }
            if clean
            else None
        )
        return {
            **base,
            "directions": [s.to_dict() for s in rows],
            "tiers": tiers,
            "recent_success_dirs": list(inputs.recent_success_dirs),
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 - 预览失败如实降级，不 5xx
        logger.warning("[direction-quota] 预览读取失败: %s", type(exc).__name__)
        return {
            **base,
            "available": False,
            "directions": [],
            "tiers": None,
            "recent_success_dirs": [],
            "error": "方向史/饱和数据读取出错，配额预览暂不可用（详见服务端日志）",
        }
