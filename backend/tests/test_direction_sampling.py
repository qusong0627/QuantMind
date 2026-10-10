"""方向加权抽样（T-MV-03）不变量 —— random 模式按空白度加权 + 可复现落档。

口径（唯一出处 = ``direction_sampling`` 模块 docstring）：

- 空白度 = 候选方向在**本用户 × 本市场**的挖掘史出现次数 n（全状态计数）；
- 权重 w = 1/(1+n)：没挖过 = 1.0，挖过 k 次 = 1/(1+k)。史全空 → 权重全 1，
  自然退化为旧的均匀随机（行为兼容，不是突变）；
- 抽签用显式 seed 的 ``random.Random(seed)``；seed/候选/权重/命中全部落
  ``direction_meta``——按 meta 重放必得同一命中（验收：「可复现」）。

本文件锁四件事：

1. **权重可测**：w=1/(1+n) 在纯函数级逐值断言（含脏数据防御）；
2. **可复现**：meta 重放命中一致——这是验收条款的机器化表达；
3. **诚实降级**：读史失败退均匀兜底，meta 如实标注 ``uniform_fallback``，
   绝不假装加权成功；空候选显式拒绝（静默返回空方向会让任务凭空无方向）；
4. **默认接线**：counter=None 走 task_store.count_by_direction（user×market 收口）。
"""

from __future__ import annotations

import collections
import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.engine.alpha_agent import task_store as task_store_mod  # noqa: E402
from backend.services.engine.alpha_agent.direction_sampling import (  # noqa: E402
    WEIGHTING_BLANKNESS,
    WEIGHTING_UNIFORM_FALLBACK,
    blankness_weights,
    sample_weighted_direction,
    sample_weighted_directions_n,
)


def _counter(counts: dict[str, int], calls: list | None = None):
    """按候选收口的假计数（真实现只回非零项——假体同契约）。"""

    async def _c(*, user_id, market, directions):
        if calls is not None:
            calls.append(
                {"user_id": user_id, "market": market, "directions": directions}
            )
        return {k: v for k, v in counts.items() if k in directions}

    return _c


# ── 权重纯函数 ───────────────────────────────────────────────────────


def test_blankness_weights_inverse_attempts() -> None:
    assert blankness_weights([0, 1, 9]) == [1.0, 0.5, 0.1]
    assert blankness_weights([]) == []


def test_blankness_weights_clamp_negative_dirty_values() -> None:
    """脏计数（负数）按 0 收敛——权重不可为负，也不能拿负值把分母变 0。"""
    assert blankness_weights([-3, 0]) == [1.0, 1.0]


# ── 抽样与可复现 ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sample_meta_carries_seed_candidates_weights_and_replays() -> None:
    """验收核心：抽中方向落 meta 且**可复现**——按记录的 seed/权重重放得同一命中。"""
    picked, meta = await sample_weighted_direction(
        ["方向A", "方向B", "方向C"],
        user_id="u-1",
        market="a_share",
        counter=_counter({"方向B": 4}),
    )

    assert picked in {"方向A", "方向B", "方向C"}
    assert meta["mode"] == "random"
    assert meta["weighting"] == WEIGHTING_BLANKNESS
    assert meta["picked"] == picked
    assert isinstance(meta["seed"], int)

    by_dir = {c["direction"]: c for c in meta["candidates"]}
    assert [c["direction"] for c in meta["candidates"]] == [
        "方向A",
        "方向B",
        "方向C",
    ], "候选顺序 = 传入顺序（choices 的累计权重按序展开，重放依赖它）"
    assert by_dir["方向A"]["attempts"] == 0 and by_dir["方向A"]["weight"] == 1.0
    assert by_dir["方向B"]["attempts"] == 4 and by_dir["方向B"][
        "weight"
    ] == pytest.approx(0.2)
    assert by_dir["方向C"]["weight"] == 1.0

    replay_dirs = [c["direction"] for c in meta["candidates"]]
    replay_weights = [c["weight"] for c in meta["candidates"]]
    replayed = random.Random(meta["seed"]).choices(
        replay_dirs, weights=replay_weights, k=1
    )[0]
    assert replayed == picked, "同 seed 同权重必得同一命中——meta 就是复现凭证"


@pytest.mark.asyncio
async def test_sample_uniform_when_history_empty() -> None:
    """史全空 → 权重全 1 → 与旧均匀随机一致（行为兼容，不是突变）。"""
    picked, meta = await sample_weighted_direction(
        ["方向A", "方向B"],
        user_id="u-1",
        market="a_share",
        counter=_counter({}),
    )

    assert picked in {"方向A", "方向B"}
    assert [c["weight"] for c in meta["candidates"]] == [1.0, 1.0]
    assert meta["weighting"] == WEIGHTING_BLANKNESS, (
        "无史 ≠ 降级：加权公式照常执行且全部退化为 1；"
        "uniform_fallback 只留给「读史失败」"
    )


@pytest.mark.asyncio
async def test_sample_favors_blank_direction_over_mined_one() -> None:
    """挖过 9 次的方向权重 1/10：大样本下空白方向显著更常被抽中。

    确定性部分（权重值）已在前面的用例逐值锁死；这里只做方向性验证，
    阈值取 ~9σ 下界（P(空白)=10/11≈0.909，300 次期望 ~273，200 即失败概率 <1e-9），
    不会把偶发抖动变成红灯。
    """
    counter = _counter({"挖过的": 9})
    picks: collections.Counter = collections.Counter()
    for _ in range(300):
        picked, _meta = await sample_weighted_direction(
            ["空白", "挖过的"], user_id="u-1", market="a_share", counter=counter
        )
        picks[picked] += 1

    assert picks["空白"] > 200, dict(picks)


@pytest.mark.asyncio
async def test_counter_failure_falls_back_uniform_with_honest_meta() -> None:
    """读史失败不许拦任务创建：退均匀 + meta 如实标注兜底（不假装加权成功）。"""

    async def _boom(*, user_id, market, directions):
        raise RuntimeError("pg down")

    picked, meta = await sample_weighted_direction(
        ["方向A", "方向B"], user_id="u-1", market="a_share", counter=_boom
    )

    assert picked in {"方向A", "方向B"}
    assert meta["weighting"] == WEIGHTING_UNIFORM_FALLBACK
    assert [c["weight"] for c in meta["candidates"]] == [1.0, 1.0]
    assert [c["attempts"] for c in meta["candidates"]] == [0, 0]


@pytest.mark.asyncio
async def test_sample_rejects_empty_directions() -> None:
    """空候选显式拒绝：静默返回空方向会让任务凭空「无方向」，比报错更难查。"""
    with pytest.raises(ValueError):
        await sample_weighted_direction(
            [], user_id="u-1", market="a_share", counter=_counter({})
        )


@pytest.mark.asyncio
async def test_default_counter_wiring_uses_task_store(monkeypatch) -> None:
    """counter=None 的默认接线：走 task_store.count_by_direction（user×market 收口）。"""
    calls: list[dict] = []

    class _FakeStore:
        async def count_by_direction(self, **kw):
            calls.append(kw)
            return {"方向A": 2}

    monkeypatch.setattr(task_store_mod, "get_mining_task_store", lambda: _FakeStore())

    _picked, meta = await sample_weighted_direction(
        ["方向A"], user_id="u-7", market="crypto"
    )

    assert calls == [{"user_id": "u-7", "market": "crypto", "directions": ["方向A"]}]
    assert meta["candidates"][0]["attempts"] == 2


# ── 并行方向数（T-MV-04）：不放回抽 N 条 ─────────────────────────────


def _replay_n(meta: dict) -> str:
    return random.Random(meta["seed"]).choices(
        [c["direction"] for c in meta["candidates"]],
        weights=[c["weight"] for c in meta["candidates"]],
        k=1,
    )[0]


@pytest.mark.asyncio
async def test_n_picks_are_distinct_and_each_meta_replays_independently() -> None:
    """逐步不放回：每步 meta 的 candidates = 该步剩余集合，任何一条独立可重放。"""
    picks = await sample_weighted_directions_n(
        ["方向A", "方向B", "方向C"],
        2,
        user_id="u-1",
        market="a_share",
        counter=_counter({"方向A": 2}),
    )

    assert len(picks) == 2
    dirs = [d for d, _ in picks]
    assert len(set(dirs)) == 2, "并行方向互不相同（不放回）"

    for meta in (m for _, m in picks):
        assert meta is not None
        assert meta["weighting"] == WEIGHTING_BLANKNESS
        assert _replay_n(meta) == meta["picked"]
    # 第 1 步候选 = 全集；第 2 步候选 = 全集减去第 1 步命中
    first_meta = picks[0][1]
    second_meta = picks[1][1]
    assert [c["direction"] for c in first_meta["candidates"]] == [
        "方向A",
        "方向B",
        "方向C",
    ]
    assert {c["direction"] for c in second_meta["candidates"]} == {
        "方向A",
        "方向B",
        "方向C",
    } - {picks[0][0]}
    # 权重按剩余集合重算，但口径不变：A 挖过 2 次 → 1/3
    a_first = next(c for c in first_meta["candidates"] if c["direction"] == "方向A")
    assert a_first["attempts"] == 2 and a_first["weight"] == pytest.approx(1 / 3)


@pytest.mark.asyncio
async def test_n_covering_all_candidates_is_exhaustive_without_lottery() -> None:
    """N >= 候选数：全集直派（保序去重）、meta=None——没抽签就没有抽签凭证。"""
    picks = await sample_weighted_directions_n(
        ["方向B", "方向A", "方向B"],
        5,
        user_id="u-1",
        market="a_share",
        counter=_counter({}),
    )

    assert picks == [("方向B", None), ("方向A", None)]


@pytest.mark.asyncio
async def test_n_equals_one_delegates_to_single_sampler() -> None:
    picks = await sample_weighted_directions_n(
        ["唯一方向"], 1, user_id="u-1", market="a_share", counter=_counter({})
    )

    assert len(picks) == 1
    picked, meta = picks[0]
    assert picked == "唯一方向" and meta is not None and meta["picked"] == picked


@pytest.mark.asyncio
async def test_n_history_failure_marks_every_meta_uniform_fallback() -> None:
    async def _boom(*, user_id, market, directions):
        raise RuntimeError("pg down")

    picks = await sample_weighted_directions_n(
        ["方向A", "方向B", "方向C"], 2, user_id="u-1", market="a_share", counter=_boom
    )

    assert len(picks) == 2
    for _d, meta in picks:
        assert meta["weighting"] == WEIGHTING_UNIFORM_FALLBACK
        assert [c["weight"] for c in meta["candidates"]] == [
            1.0 for _ in meta["candidates"]
        ]


@pytest.mark.asyncio
async def test_n_rejects_empty_after_cleaning() -> None:
    with pytest.raises(ValueError):
        await sample_weighted_directions_n(
            ["", "   "], 2, user_id="u-1", market="a_share", counter=_counter({})
        )
