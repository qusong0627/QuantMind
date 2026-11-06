"""Private history-weighted sampling adapted from AutoAlpha 4aaeda8b0e58477085762c1ac2e7faa7bdab096c."""

from __future__ import annotations

import logging
import math
import platform
import random
import json
from collections.abc import Sequence

from sqlalchemy import text

from backend.shared.database_manager_v2 import get_session
from backend.shared.owner_scope import OwnerScope
from backend.shared.utc_datetime import to_utc_iso, utc_now

logger = logging.getLogger(__name__)
WEIGHTING_BLANKNESS = "blankness"
WEIGHTING_UNIFORM_FALLBACK = "uniform_fallback"
MAX_CANDIDATES = 100
MAX_LABEL_CHARS = 1000
_SEED_RANGE = 2**31


def clean_directions(directions: Sequence[str]) -> list[str]:
    if not isinstance(directions, (list, tuple)) or len(directions) > MAX_CANDIDATES:
        raise ValueError("方向候选最多100项")
    clean = []
    for direction in directions:
        if not isinstance(direction, str) or len(direction) > MAX_LABEL_CHARS:
            raise ValueError("方向候选须为不超过1000字的字符串")
        if "\x00" in direction:
            raise ValueError("方向候选不能包含空字符")
        value = direction.strip()
        if value and value not in clean:
            clean.append(value)
    try:
        size = len(json.dumps(list(directions), ensure_ascii=False).encode("utf-8"))
    except UnicodeError as exc:
        raise ValueError("方向候选包含无效Unicode字符") from exc
    if size > 128 * 1024:
        raise ValueError("方向候选的UTF-8快照超过128 KiB")
    return clean


def blankness_weights(attempts: Sequence[int]) -> list[float]:
    return [1.0 / (1 + max(0, int(count))) for count in attempts]


async def count_by_direction(
    *, owner: OwnerScope, market: str, universe: str, directions
):
    if not isinstance(owner, OwnerScope) or market != "a_share" or not universe:
        raise ValueError("完整本人A股股票池身份是必需的")
    clean = clean_directions(directions)
    if not clean:
        return {}
    async with get_session() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        await session.execute(text("SET LOCAL statement_timeout='5s'"))
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT task.direction,count(*) AS attempts FROM alpha_mining_tasks task "
                        "LEFT JOIN alpha_mining_task_specs spec ON spec.tenant_id=task.tenant_id "
                        "AND spec.user_id=task.user_id AND spec.task_id=task.task_id "
                        "WHERE task.tenant_id=:tenant_id AND task.user_id=:user_id "
                        "AND task.market=:market AND task.universe=:universe "
                        "AND COALESCE(spec.algorithm,'rdagent')='rdagent' "
                        "AND task.direction=ANY(CAST(:directions AS TEXT[])) GROUP BY task.direction"
                    ),
                    {
                        **owner.sql_params(),
                        "market": market,
                        "universe": universe,
                        "directions": clean,
                    },
                )
            )
            .mappings()
            .all()
        )
    return {row["direction"]: int(row["attempts"]) for row in rows}


async def _read_attempts(clean, *, owner, market, universe, counter=None):
    # Identity errors must never be swallowed as a history-read fallback.
    if (
        not isinstance(owner, OwnerScope)
        or market != "a_share"
        or not isinstance(universe, str)
        or not universe
    ):
        raise ValueError("完整本人A股股票池身份是必需的")
    try:
        counts = await (counter or count_by_direction)(
            owner=owner, market=market, universe=universe, directions=list(clean)
        )
        if not isinstance(counts, dict) or any(
            key not in clean or type(value) is not int or value < 0
            for key, value in counts.items()
        ):
            raise ValueError("Invalid owned direction counts")
        return counts, WEIGHTING_BLANKNESS
    except Exception as exc:
        logger.warning(
            "Owned direction-history read unavailable (%s); using explicit uniform fallback",
            type(exc).__name__,
        )
        return {}, WEIGHTING_UNIFORM_FALLBACK


def _draw(clean, counts, weighting, *, market, universe):
    attempts = [counts.get(value, 0) for value in clean]
    weights = blankness_weights(attempts)
    seed = random.SystemRandom().randrange(_SEED_RANGE)
    picked = random.Random(seed).choices(clean, weights=weights, k=1)[0]
    return picked, {
        "schema_version": 1,
        "mode": "random",
        "weighting": weighting,
        "seed": seed,
        "picked": picked,
        "scope": {"market": market, "universe": universe, "algorithm": "rdagent"},
        "counted_statuses": "all",
        "history_source": "alpha_mining_tasks",
        "rng": "python.random.Random.choices",
        "python_version": platform.python_version(),
        "captured_at": to_utc_iso(utc_now()),
        "candidates": [
            {"direction": value, "attempts": count, "weight": weight}
            for value, count, weight in zip(clean, attempts, weights, strict=True)
        ],
    }


async def sample_weighted_direction(
    directions, *, owner: OwnerScope, market: str, universe: str, counter=None
):
    clean = clean_directions(directions)
    if not clean:
        raise ValueError("随机选择至少需要一个非空方向")
    counts, weighting = await _read_attempts(
        clean, owner=owner, market=market, universe=universe, counter=counter
    )
    return _draw(clean, counts, weighting, market=market, universe=universe)


async def sample_weighted_directions_n(
    directions, n, *, owner: OwnerScope, market: str, universe: str, counter=None
):
    clean = clean_directions(directions)
    if not clean or type(n) is not int or not 1 <= n <= MAX_CANDIDATES:
        raise ValueError("方向数须为1至100之间的整数且候选不能为空")
    if not isinstance(owner, OwnerScope) or market != "a_share" or not universe:
        raise ValueError("完整本人A股股票池身份是必需的")
    if n == 1:
        return [
            await sample_weighted_direction(
                clean, owner=owner, market=market, universe=universe, counter=counter
            )
        ]
    if n >= len(clean):
        return [(value, None) for value in clean]
    counts, weighting = await _read_attempts(
        clean, owner=owner, market=market, universe=universe, counter=counter
    )
    remaining, picks = list(clean), []
    for _ in range(n):
        picked, meta = _draw(
            remaining, counts, weighting, market=market, universe=universe
        )
        picks.append((picked, meta))
        remaining.remove(picked)
    return picks


def validate_selection(meta, *, direction: str, market: str, universe: str):
    if not isinstance(meta, dict) or meta.get("mode") not in {"random", "quota"}:
        raise ValueError("方向抽样凭证无效")
    if meta.get("mode") == "random":
        return _validate_random_selection(
            meta, direction=direction, market=market, universe=universe
        )
    return _validate_quota_selection(
        meta, direction=direction, market=market, universe=universe
    )


def _validate_random_selection(meta, *, direction: str, market: str, universe: str):
    fields = {
        "schema_version",
        "mode",
        "weighting",
        "seed",
        "picked",
        "scope",
        "counted_statuses",
        "history_source",
        "rng",
        "python_version",
        "captured_at",
        "candidates",
    }
    extra = set(meta) - fields
    if (
        type(meta.get("schema_version")) is not int
        or meta["schema_version"] != 1
        # quota 输入不可用时的兜底只允许显式标注，不允许其他私有字段
        or (extra and extra != {"quota_fallback"})
        or ("quota_fallback" in meta and meta["quota_fallback"] is not True)
    ):
        raise ValueError("方向抽样凭证无效")
    if (
        meta.get("rng") != "python.random.Random.choices"
        or meta.get("python_version") != platform.python_version()
        or meta.get("history_source") != "alpha_mining_tasks"
        or meta.get("counted_statuses") != "all"
    ):
        raise ValueError("方向抽样实现或历史来源无效")
    if meta.get("picked") != direction or meta.get("scope") != {
        "market": market,
        "universe": universe,
        "algorithm": "rdagent",
    }:
        raise ValueError("方向抽样凭证与任务参数不一致")
    if meta.get("weighting") not in {WEIGHTING_BLANKNESS, WEIGHTING_UNIFORM_FALLBACK}:
        raise ValueError("未知方向权重口径")
    if type(meta.get("seed")) is not int or not 0 <= meta["seed"] < _SEED_RANGE:
        raise ValueError("方向抽样种子无效")
    candidates = meta.get("candidates")
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= MAX_CANDIDATES:
        raise ValueError("方向抽样候选快照无效")
    labels, weights = [], []
    for item in candidates:
        if not isinstance(item, dict) or set(item) != {
            "direction",
            "attempts",
            "weight",
        }:
            raise ValueError("方向抽样候选快照无效")
        label, count, weight = (
            item.get("direction"),
            item.get("attempts"),
            item.get("weight"),
        )
        if (
            not isinstance(label, str)
            or not label
            or len(label) > MAX_LABEL_CHARS
            or label in labels
        ):
            raise ValueError("方向抽样候选无效")
        if (
            type(count) is not int
            or count < 0
            or type(weight) not in {float, int}
            or not math.isfinite(weight)
            or weight != 1.0 / (1 + count)
        ):
            raise ValueError("方向抽样权重无效")
        if meta["weighting"] == WEIGHTING_UNIFORM_FALLBACK and count != 0:
            raise ValueError("均匀兜底不能声称读到了历史")
        labels.append(label)
        weights.append(weight)
    if (
        random.Random(meta["seed"]).choices(labels, weights=weights, k=1)[0]
        != direction
    ):
        raise ValueError("方向抽样凭证不能重放到指定方向")
    return meta


def _validate_quota_selection(meta, *, direction: str, market: str, universe: str):
    # 三档配额凭证与随机凭证同纪律：字段集合、权重口径、seed 重放与打分
    # 快照内部一致性全部严格校验；打分常数以 direction_quota 为单源。
    from .direction_quota import SATURATED_MIN_DUP_HITS_90D

    fields = {
        "schema_version",
        "mode",
        "tier",
        "weighting",
        "seed",
        "picked",
        "scope",
        "counted_statuses",
        "history_source",
        "dup_source",
        "rng",
        "python_version",
        "captured_at",
        "candidates",
        "scores_snapshot",
    }
    extra = set(meta) - fields
    if (
        type(meta.get("schema_version")) is not int
        or meta["schema_version"] != 1
        or (extra and extra != {"tier_fallback"})
        or ("tier_fallback" in meta and meta["tier_fallback"] not in {"saturated", "mutate_empty"})
        or meta.get("tier") not in {"explore", "exploit", "mutate"}
        or meta.get("weighting") not in {"blankness", "proxy_score", "uniform"}
        or meta.get("rng") != "python.random.Random.choices"
        or meta.get("python_version") != platform.python_version()
        or meta.get("history_source") != "alpha_mining_tasks"
        or meta.get("dup_source") != "alpha_factor_materializations"
        or meta.get("counted_statuses") != "all"
        or meta.get("scope") != {
            "market": market,
            "universe": universe,
            "algorithm": "rdagent",
        }
        or meta.get("picked") != direction
    ):
        raise ValueError("方向配额凭证无效")
    if type(meta.get("seed")) is not int or not 0 <= meta["seed"] < _SEED_RANGE:
        raise ValueError("方向抽样种子无效")
    candidates = meta.get("candidates")
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= MAX_CANDIDATES:
        raise ValueError("方向抽样候选快照无效")
    labels, weights = [], []
    for item in candidates:
        if not isinstance(item, dict) or set(item) != {"direction", "value", "weight"}:
            raise ValueError("方向抽样候选快照无效")
        label, value, weight = (
            item.get("direction"),
            item.get("value"),
            item.get("weight"),
        )
        if (
            not isinstance(label, str)
            or not label
            or len(label) > MAX_LABEL_CHARS
            or label in labels
        ):
            raise ValueError("方向抽样候选无效")
        if (
            type(weight) not in {float, int}
            or not math.isfinite(weight)
            or weight < 0
        ):
            raise ValueError("方向抽样权重无效")
        if meta["weighting"] == "blankness":
            if type(value) is not int or value < 0 or weight != 1.0 / (1 + value):
                raise ValueError("方向配额权重无效")
        elif meta["weighting"] == "uniform":
            if type(value) is not int or value < 0 or weight != 1.0:
                raise ValueError("方向配额权重无效")
        elif (
            type(value) not in {float, int}
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError("方向配额权重无效")
        labels.append(label)
        weights.append(weight)
    if meta["weighting"] == "proxy_score":
        total = sum(item["value"] for item in candidates)
        if total <= 0:
            raise ValueError("方向配额权重无效")
        for item in candidates:
            if item["weight"] != item["value"] / total:
                raise ValueError("方向配额权重无效")
    if random.Random(meta["seed"]).choices(labels, weights=weights, k=1)[0] != (
        direction
    ):
        raise ValueError("方向抽样凭证不能重放到指定方向")
    snapshot = meta.get("scores_snapshot")
    if not isinstance(snapshot, list) or not 1 <= len(snapshot) <= MAX_CANDIDATES:
        raise ValueError("方向配额打分快照无效")
    seen = set()
    for row in snapshot:
        if not isinstance(row, dict) or set(row) != {
            "direction",
            "attempts",
            "n_tasks",
            "n_success",
            "p",
            "dup_hits_90d",
            "sat_penalty",
            "score",
            "saturated",
        }:
            raise ValueError("方向配额打分快照无效")
        label = row.get("direction")
        if not isinstance(label, str) or not label or label in seen:
            raise ValueError("方向配额打分快照无效")
        seen.add(label)
        for key in ("attempts", "n_tasks", "n_success", "dup_hits_90d"):
            if type(row.get(key)) is not int or row[key] < 0:
                raise ValueError("方向配额打分快照无效")
        p, sat, score = row.get("p"), row.get("sat_penalty"), row.get("score")
        if (
            type(p) not in {float, int}
            or not math.isfinite(p)
            or not 0 <= p <= 1
            or type(sat) not in {float, int}
            or not math.isfinite(sat)
            or not 0 <= sat <= 0.5
            or type(score) not in {float, int}
            or not math.isfinite(score)
            or score < 0
            or score != p * (1.0 - sat)
            or row.get("saturated") != (row["dup_hits_90d"] >= SATURATED_MIN_DUP_HITS_90D)
        ):
            raise ValueError("方向配额打分快照无效")
    if direction not in seen:
        raise ValueError("方向配额打分快照缺少命中方向")
    return meta
