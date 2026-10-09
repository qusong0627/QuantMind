#!/usr/bin/env python3
"""ml-purged-cv — 金融时序防泄漏交叉验证索引生成器 + QuantDB 演示 CLI。

方法论移植自 quantskills/skill-ml-purged-cv（López de Prado purge + embargo），
核心索引生成器只依赖标准库，可直接 import：

    from purged_cv import PurgedKFold, CombinatorialPurgedCV, CausalWalkForward

口径约定（与来源仓库一脉相承）：
- 会话坐标：每个样本只有整数 session 序号（0..S-1，按交易日历升序）；
- 信息区间 = [interval_start, interval_end]（闭区间，单位=session 序号）：
  从该样本最早使用的信息，到决定该样本标签所需的最后时间；
- Purge：候选训练样本的完整信息区间与测试样本区间有任何重叠即剔除
  （区间相交判定，不是「测试块前后固定删 N 行」）；
- Embargo：每个连续测试块「最晚信息终点」之后 E 个 session 的候选样本再剔除，
  与 Purge 方向相同、职责不同（Purge 管已知区间重叠，Embargo 管声明的尾部依赖）；
- Pre-Test Gap：因果 Walk-Forward 中测试块之前 G 个 session 的隔离带（与 Embargo 反向）；
- 同一 session 的多样本不拆分（按 session 分组切分，防止同日截面状态跨侧泄漏）；
- Fold-Local：任何学习型预处理（填充/标准化/降维/特征筛选）必须在每折训练侧重新拟合。

CLI：
  python3 purged_cv.py --demo                     # 合成数据演示（需 numpy，宿主/容器皆可）
  python3 purged_cv.py --quantdb --market CN ...  # QuantDB 直读（需 pandas+numpy，容器内跑）

产出是结构证据与实测指标，不是盈利或可上线证明。仅限本地研究使用。
"""

from __future__ import annotations

import argparse
import bisect
import itertools
import json
import math
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

SCHEMA_VERSION = "1"

try:  # 核心不依赖 numpy；只有 --demo/--quantdb 与 sklearn 适配层需要
    import numpy as np
except ImportError:  # pragma: no cover
    np = None


# ───────────────────────────── 纯标准库核心 ─────────────────────────────


def merged_intervals(intervals):
    """合并相互重叠或相接的闭区间，返回按起点排序的不可变区间表。"""
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return tuple(merged)


def overlapping_positions(candidate_positions, intervals, protected_positions):
    """返回信息区间与任一受保护区间相交的候选样本位置集合（bisect，近线性）。"""
    protected = merged_intervals(intervals[p] for p in protected_positions)
    if not protected:
        return set()
    starts = [item[0] for item in protected]
    ends = [item[1] for item in protected]
    hit = set()
    for position in candidate_positions:
        start, end = intervals[position]
        index = bisect.bisect_right(starts, end) - 1
        if index >= 0 and ends[index] >= start:
            hit.add(position)
    return hit


def contiguous_blocks(sorted_sessions):
    """把升序 session 序号切成连续段（整数坐标上相邻即连续）。"""
    blocks = []
    start = previous = sorted_sessions[0]
    for session in sorted_sessions[1:]:
        if session == previous + 1:
            previous = session
        else:
            blocks.append((start, previous))
            start = previous = session
    blocks.append((start, previous))
    return tuple(blocks)


def _array_split_positions(total, n_splits):
    """等价 numpy.array_split：前 total % n_splits 段各多 1 个。"""
    base, extra = divmod(total, n_splits)
    chunks, cursor = [], 0
    for index in range(n_splits):
        size = base + (1 if index < extra else 0)
        chunks.append(list(range(cursor, cursor + size)))
        cursor += size
    return chunks


@dataclass(frozen=True)
class FoldAssignment:
    """一次训练/测试划分及其排除账目（位置索引，非会话号）。"""

    fold_index: int
    train: tuple
    test: tuple
    purged: tuple = ()
    embargoed: tuple = ()
    pre_test_gapped: tuple = ()
    test_blocks: tuple = ()
    combination_index: int | None = None
    test_group_indices: tuple = ()

    @property
    def exclusion_counts(self):
        return {
            "purged": len(self.purged),
            "embargoed": len(self.embargoed),
            "pre_test_gapped": len(self.pre_test_gapped),
            "retained": len(self.train),
        }


def apply_exclusions(
    sessions,
    intervals,
    test_positions,
    test_blocks,
    *,
    embargo_sessions=0,
    pre_test_gap_sessions=0,
    require_information_before=None,
):
    """对一次划分施加 Purge / Embargo / Pre-Test Gap / 因果约束，返回排除账目。

    执行顺序与来源实现一致：先 Purge（区间相交），再 Embargo / Pre-Test Gap /
    因果约束；后面的阶段不重复计算已被前面剔除的样本。
    """
    test_set = set(test_positions)
    candidates = tuple(i for i in range(len(sessions)) if i not in test_set)
    purged = overlapping_positions(candidates, intervals, test_positions)

    embargoed = set()
    if embargo_sessions > 0:
        for block_start, block_end in test_blocks:
            in_block = [
                p for p in test_positions if block_start <= sessions[p] <= block_end
            ]
            if not in_block:
                continue
            latest_end = max(intervals[p][1] for p in in_block)
            for position in candidates:
                if position in purged or position in embargoed:
                    continue
                if latest_end < sessions[position] <= latest_end + embargo_sessions:
                    embargoed.add(position)

    gapped = set()
    if pre_test_gap_sessions > 0:
        for block_start, _block_end in test_blocks:
            low, high = block_start - pre_test_gap_sessions, block_start - 1
            for position in candidates:
                if position in purged or position in embargoed or position in gapped:
                    continue
                if low <= sessions[position] <= high:
                    gapped.add(position)

    noncausal = set()
    if require_information_before is not None:
        for position in candidates:
            if position in purged or position in embargoed or position in gapped:
                continue
            if intervals[position][1] >= require_information_before:
                noncausal.add(position)

    excluded = purged | embargoed | gapped | noncausal
    train = tuple(p for p in candidates if p not in excluded)
    return train, purged, embargoed, gapped, noncausal


class PurgedKFold:
    """时间序 Purged K-Fold：连续 session 分块 + 完整区间 Purge + 可选 Embargo。"""

    def __init__(self, n_splits=5, embargo_sessions=0):
        if n_splits < 2:
            raise ValueError("n_splits 至少为 2")
        if embargo_sessions < 0:
            raise ValueError("embargo_sessions 不能为负")
        self.n_splits = n_splits
        self.embargo_sessions = embargo_sessions

    def split(self, sessions, interval_start, interval_end):
        """按 session 连续分块给出每折划分（生成器，产出 FoldAssignment）。"""
        intervals = _validate_inputs(sessions, interval_start, interval_end)
        axis = sorted(set(sessions))
        if self.n_splits > len(axis):
            raise ValueError("n_splits 不能超过有效 session 数")
        for fold_index, chunk in enumerate(
            _array_split_positions(len(axis), self.n_splits)
        ):
            chunk_sessions = {axis[i] for i in chunk}
            test_positions = tuple(
                i for i, s in enumerate(sessions) if s in chunk_sessions
            )
            blocks = contiguous_blocks(sorted(chunk_sessions))
            train, purged, embargoed, gapped, _ = apply_exclusions(
                sessions,
                intervals,
                test_positions,
                blocks,
                embargo_sessions=self.embargo_sessions,
            )
            yield FoldAssignment(
                fold_index=fold_index,
                train=train,
                test=test_positions,
                purged=tuple(sorted(purged)),
                embargoed=tuple(sorted(embargoed)),
                pre_test_gapped=tuple(sorted(gapped)),
                test_blocks=blocks,
            )


class CombinatorialPurgedCV:
    """CPCV：N 组中取 k 组做测试，C(N,k) 个组合；路径分解为 C(N-1,k-1) 条完整路径。"""

    def __init__(self, n_groups=6, n_test_groups=2, embargo_sessions=0):
        if n_groups < 3 or not 2 <= n_test_groups < n_groups:
            raise ValueError("CPCV 要求 n_groups>=3 且 2<=n_test_groups<n_groups")
        if embargo_sessions < 0:
            raise ValueError("embargo_sessions 不能为负")
        self.n_groups = n_groups
        self.n_test_groups = n_test_groups
        self.embargo_sessions = embargo_sessions

    @property
    def combination_count(self):
        return math.comb(self.n_groups, self.n_test_groups)

    @property
    def path_count(self):
        return math.comb(self.n_groups - 1, self.n_test_groups - 1)

    def split(self, sessions, interval_start, interval_end):
        intervals = _validate_inputs(sessions, interval_start, interval_end)
        axis = sorted(set(sessions))
        if self.n_groups > len(axis):
            raise ValueError("n_groups 不能超过有效 session 数")
        groups = [
            tuple(axis[i] for i in chunk)
            for chunk in _array_split_positions(len(axis), self.n_groups)
        ]
        for combination_index, group_indices in enumerate(
            itertools.combinations(range(self.n_groups), self.n_test_groups)
        ):
            test_sessions = {s for g in group_indices for s in groups[g]}
            test_positions = tuple(
                i for i, s in enumerate(sessions) if s in test_sessions
            )
            runs = _consecutive_runs(group_indices)
            blocks = tuple((groups[run[0]][0], groups[run[-1]][-1]) for run in runs)
            train, purged, embargoed, gapped, _ = apply_exclusions(
                sessions,
                intervals,
                test_positions,
                blocks,
                embargo_sessions=self.embargo_sessions,
            )
            yield FoldAssignment(
                fold_index=combination_index,
                train=train,
                test=test_positions,
                purged=tuple(sorted(purged)),
                embargoed=tuple(sorted(embargoed)),
                pre_test_gapped=tuple(sorted(gapped)),
                test_blocks=blocks,
                combination_index=combination_index,
                test_group_indices=group_indices,
            )

    def path_decomposition(self):
        """CPCV 路径分解（组合×测试组 → 完整路径），返回 tuple[tuple[(组合号, 组号)]]。"""
        return cpcv_path_decomposition(self.n_groups, self.n_test_groups)


class CausalWalkForward:
    """因果 Walk-Forward：测试块在时间轴末端，训练候选严格早于测试块（可加 Pre-Test Gap）。"""

    def __init__(
        self,
        n_splits=5,
        test_sessions=20,
        pre_test_gap_sessions=0,
        max_train_sessions=None,
    ):
        if n_splits < 1 or test_sessions < 1:
            raise ValueError("n_splits / test_sessions 至少为 1")
        if pre_test_gap_sessions < 0:
            raise ValueError("pre_test_gap_sessions 不能为负")
        self.n_splits = n_splits
        self.test_sessions = test_sessions
        self.pre_test_gap_sessions = pre_test_gap_sessions
        self.max_train_sessions = max_train_sessions

    def split(self, sessions, interval_start, interval_end):
        intervals = _validate_inputs(sessions, interval_start, interval_end)
        axis = sorted(set(sessions))
        required = self.n_splits * self.test_sessions
        if required >= len(axis):
            raise ValueError("Walk-Forward 要求测试块之前至少留 1 个有效 session")
        first_test = len(axis) - required
        for fold_index in range(self.n_splits):
            start = first_test + fold_index * self.test_sessions
            stop = start + self.test_sessions
            test_values = axis[start:stop]
            candidate_axis = axis[:start]
            if self.max_train_sessions is not None:
                candidate_axis = candidate_axis[-self.max_train_sessions :]
            candidate_values = set(candidate_axis)
            test_set = set(test_values)
            test_positions = tuple(i for i, s in enumerate(sessions) if s in test_set)
            candidate_positions = tuple(
                i for i, s in enumerate(sessions) if s in candidate_values
            )
            block = (test_values[0], test_values[-1])
            train, purged, embargoed, gapped, noncausal = _apply_exclusions_subset(
                sessions,
                intervals,
                candidate_positions,
                test_positions,
                (block,),
                pre_test_gap_sessions=self.pre_test_gap_sessions,
                require_information_before=test_values[0],
            )
            yield FoldAssignment(
                fold_index=fold_index,
                train=train,
                test=test_positions,
                purged=tuple(sorted(purged)),
                embargoed=tuple(sorted(embargoed)),
                pre_test_gapped=tuple(sorted(gapped)),
                test_blocks=(block,),
            )


def _apply_exclusions_subset(
    sessions,
    intervals,
    candidate_positions,
    test_positions,
    test_blocks,
    *,
    embargo_sessions=0,
    pre_test_gap_sessions=0,
    require_information_before=None,
):
    """同 apply_exclusions，但候选集是显式子集（Walk-Forward 只用过去 session）。"""
    zero = apply_exclusions(
        sessions,
        intervals,
        test_positions,
        test_blocks,
        embargo_sessions=embargo_sessions,
        pre_test_gap_sessions=pre_test_gap_sessions,
        require_information_before=require_information_before,
    )
    allowed = set(candidate_positions)
    train = tuple(p for p in zero[0] if p in allowed)
    purged = {p for p in zero[1] if p in allowed}
    embargoed = {p for p in zero[2] if p in allowed}
    gapped = {p for p in zero[3] if p in allowed}
    noncausal = {p for p in zero[4] if p in allowed}
    return train, purged, embargoed, gapped, noncausal


def _consecutive_runs(sorted_indices):
    runs = []
    for index in sorted_indices:
        if not runs or index != runs[-1][-1] + 1:
            runs.append([index])
        else:
            runs[-1].append(index)
    return tuple(tuple(run) for run in runs)


def _validate_inputs(sessions, interval_start, interval_end):
    if not (len(sessions) == len(interval_start) == len(interval_end)):
        raise ValueError("sessions / interval_start / interval_end 长度必须一致")
    intervals = tuple(zip(interval_start, interval_end, strict=True))
    for start, end in intervals:
        if start > end:
            raise ValueError("信息区间起点不能晚于终点")
    return intervals


def cpcv_path_decomposition(n_groups, n_test_groups):
    """把 C(N,k) 组合的边缘着色为 C(N-1,k-1) 条完整路径（确定性边着色，标准库实现）。

    性质：每条路径恰好覆盖每个组一次；同一组合的 k 个测试组落在 k 条不同路径。
    """
    combinations = tuple(itertools.combinations(range(n_groups), n_test_groups))
    path_count = math.comb(n_groups - 1, n_test_groups - 1)
    edges = tuple((c, g) for c, combo in enumerate(combinations) for g in combo)
    edge_color, color_at_combination, color_at_group = {}, {}, {}

    for edge_index, (combination_index, group_index) in enumerate(edges):
        missing_at_combination = {
            c
            for c in range(path_count)
            if (combination_index, c) not in color_at_combination
        }
        missing_at_group = {
            c for c in range(path_count) if (group_index, c) not in color_at_group
        }
        common = missing_at_combination & missing_at_group
        if common:
            color = min(common)
        else:
            first = min(missing_at_combination)
            second = min(missing_at_group)
            component = _alternating_component(
                combination_index,
                first,
                second,
                edges,
                color_at_combination,
                color_at_group,
            )
            for member in component:
                c, g = edges[member]
                old = edge_color[member]
                del color_at_combination[(c, old)]
                del color_at_group[(g, old)]
            for member in component:
                c, g = edges[member]
                new = second if edge_color[member] == first else first
                edge_color[member] = new
                color_at_combination[(c, new)] = member
                color_at_group[(g, new)] = member
            color = second
        edge_color[edge_index] = color
        color_at_combination[(combination_index, color)] = edge_index
        color_at_group[(group_index, color)] = edge_index

    paths = []
    for path_index in range(path_count):
        members = sorted(
            (
                (c, g)
                for edge_index, (c, g) in enumerate(edges)
                if edge_color[edge_index] == path_index
            ),
            key=lambda item: item[1],
        )
        paths.append(tuple(members))
    return tuple(paths)


def _alternating_component(
    start_combination, first, second, edges, color_at_combination, color_at_group
):
    stack = [("combination", start_combination)]
    visited, component = set(), set()
    while stack:
        side, vertex = stack.pop()
        if (side, vertex) in visited:
            continue
        visited.add((side, vertex))
        color_map = color_at_combination if side == "combination" else color_at_group
        for color in (first, second):
            edge_index = color_map.get((vertex, color))
            if edge_index is None:
                continue
            component.add(edge_index)
            combination_index, group_index = edges[edge_index]
            stack.append(
                ("group", group_index)
                if side == "combination"
                else ("combination", combination_index)
            )
    return component


# ─────────────────────────── sklearn 兼容适配层（可选） ───────────────────────────

try:
    from sklearn.model_selection import BaseCrossValidator as _BaseCrossValidator

    _HAS_SKLEARN = True
except ImportError:  # pragma: no cover
    _HAS_SKLEARN = False

if _HAS_SKLEARN:

    class SklearnPurgedKFold(_BaseCrossValidator):
        """sklearn 兼容 splitter：`split(X, y, groups)` 中 groups=每样本 session 序号。

        信息区间在构造时用数组给出；缺省 start=end=session（只用当日信息、标签瞬时）。
        可直接放进 sklearn 的 cross_val_score / GridSearchCV 生态。
        """

        def __init__(
            self, n_splits=5, embargo_sessions=0, interval_start=None, interval_end=None
        ):
            self.n_splits = n_splits
            self.embargo_sessions = embargo_sessions
            self.interval_start = interval_start
            self.interval_end = interval_end

        def get_n_splits(self, X=None, y=None, groups=None):
            return self.n_splits

        def split(self, X, y=None, groups=None):
            if groups is None:
                raise ValueError("必须通过 groups 传入每样本的 session 序号")
            sessions = [int(s) for s in groups]
            start = [
                int(v)
                for v in (
                    self.interval_start if self.interval_start is not None else sessions
                )
            ]
            end = [
                int(v)
                for v in (
                    self.interval_end if self.interval_end is not None else sessions
                )
            ]
            for fold in PurgedKFold(self.n_splits, self.embargo_sessions).split(
                sessions, start, end
            ):
                yield (
                    np.asarray(fold.train, dtype=int),
                    np.asarray(fold.test, dtype=int),
                )


# ─────────────────────────── numpy 评估层 ───────────────────────────


def _require_numpy():
    if np is None:
        raise SystemExit(
            "此模式需要 numpy（宿主/容器均可 pip 安装；容器 quantmind 已内置）"
        )


def fold_local_fit_predict(
    x_train, y_train, x_test, *, model="ridge", alpha=1.0, seed=42
):
    """在一折内完成缺失填充→标准化→拟合→预测（所有状态只看训练侧）。"""
    _require_numpy()
    x_train = np.asarray(x_train, dtype=float)
    x_test = np.asarray(x_test, dtype=float)
    median = np.nanmedian(x_train, axis=0)
    median = np.where(np.isfinite(median), median, 0.0)
    x_train = np.where(np.isfinite(x_train), x_train, median)
    x_test = np.where(np.isfinite(x_test), x_test, median)
    mean = x_train.mean(axis=0)
    std = x_train.std(axis=0)
    std = np.where(std > 1e-12, std, 1.0)
    x_train = (x_train - mean) / std
    x_test = (x_test - mean) / std
    if model in ("ridge", "ols"):
        penalties = (
            np.zeros(x_train.shape[1])
            if model == "ols"
            else np.full(x_train.shape[1], float(alpha))
        )
        gram = x_train.T @ x_train + np.diag(penalties)
        y_centered = y_train - y_train.mean()
        beta = np.linalg.solve(gram, x_train.T @ y_centered)
        return x_test @ beta + y_train.mean()
    if model == "hgb":
        from sklearn.ensemble import HistGradientBoostingRegressor

        estimator = HistGradientBoostingRegressor(random_state=seed)
        estimator.fit(x_train, y_train)
        return estimator.predict(x_test)
    raise ValueError(f"未知模型: {model}")


def _average_ranks(values):
    values = np.asarray(values, dtype=float)
    order = np.argsort(values, kind="mergesort")
    ranks = np.empty(len(values), dtype=float)
    ranks[order] = np.arange(len(values), dtype=float)
    sorted_values = values[order]
    start = 0
    for index in range(1, len(values) + 1):
        if index == len(values) or sorted_values[index] != sorted_values[start]:
            if index - start > 1:
                ranks[order[start:index]] = ranks[order[start:index]].mean()
            start = index
    return ranks


def _pearson(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    if len(a) < 2:
        return float("nan")
    a, b = a - a.mean(), b - b.mean()
    denominator = math.sqrt(float(a @ a) * float(b @ b))
    return float(a @ b / denominator) if denominator > 0 else float("nan")


def spearman_ic(prediction, target):
    return _pearson(_average_ranks(prediction), _average_ranks(target))


def evaluate_folds(folds, features, targets, *, model="ridge", alpha=1.0, seed=42):
    """对一组 FoldAssignment 逐折 fold-local 拟合，汇总测试集指标。"""
    _require_numpy()
    features = np.asarray(features, dtype=float)
    targets = np.asarray(targets, dtype=float)
    pooled_predictions, pooled_targets, per_fold_ic = [], [], []
    for fold in folds:
        if not fold.train or not fold.test:
            continue
        train_idx = np.asarray(fold.train, dtype=int)
        test_idx = np.asarray(fold.test, dtype=int)
        prediction = fold_local_fit_predict(
            features[train_idx],
            targets[train_idx],
            features[test_idx],
            model=model,
            alpha=alpha,
            seed=seed,
        )
        pooled_predictions.append(prediction)
        pooled_targets.append(targets[test_idx])
        per_fold_ic.append(_pearson(prediction, targets[test_idx]))
    if not pooled_predictions:
        return {
            "ic_pooled": None,
            "rank_ic_pooled": None,
            "mse_pooled": None,
            "ic_mean_fold": None,
            "per_fold_ic": [],
            "oos_rows": 0,
        }
    predictions = np.concatenate(pooled_predictions)
    truths = np.concatenate(pooled_targets)
    return {
        "ic_pooled": round(_pearson(predictions, truths), 6),
        "rank_ic_pooled": round(spearman_ic(predictions, truths), 6),
        "mse_pooled": round(float(np.mean((predictions - truths) ** 2)), 10),
        "ic_mean_fold": round(float(np.nanmean(per_fold_ic)), 6),
        "per_fold_ic": [None if math.isnan(v) else round(v, 6) for v in per_fold_ic],
        "oos_rows": int(len(predictions)),
    }


def retained_overlap_count(fold, sessions, intervals):
    """安全通道审计：保留训练样本中信息区间仍与测试区间相交的个数（应为 0）。"""
    if not fold.train:
        return 0
    return len(overlapping_positions(fold.train, intervals, fold.test))


# ─────────────────────────── 通道对比 ───────────────────────────


def _chronological_chunks(sessions, n_splits):
    axis = sorted(set(sessions))
    return [
        [axis[i] for i in chunk]
        for chunk in _array_split_positions(len(axis), n_splits)
    ]


def _vanilla_folds(sessions, test_session_chunks):
    """无任何排除的 vanilla 划分：训练=补集（用于对照与重叠审计）。"""
    folds = []
    for fold_index, chunk in enumerate(test_session_chunks):
        test_set = set(chunk)
        test = tuple(i for i, s in enumerate(sessions) if s in test_set)
        train = tuple(i for i, s in enumerate(sessions) if s not in test_set)
        folds.append(
            FoldAssignment(
                fold_index=fold_index,
                train=train,
                test=test,
                test_blocks=contiguous_blocks(sorted(test_set)),
            )
        )
    return folds


def run_channel_comparison(
    sessions, interval_start, interval_end, features, targets, config
):
    """四个通道同数据同模型对比：vanilla 乱序 / vanilla 时序 / purged / purged+embargo。"""
    _require_numpy()
    n_splits = config["n_splits"]
    embargo = config["embargo_sessions"]
    intervals = tuple(zip(interval_start, interval_end, strict=True))
    axis = sorted(set(sessions))
    import random as _random

    rng = _random.Random(config["seed"])
    shuffled_axis = list(axis)
    rng.shuffle(shuffled_axis)
    channels = {}

    def vanilla(name, test_session_chunks):
        folds = _vanilla_folds(sessions, test_session_chunks)
        metrics = evaluate_folds(
            folds, features, targets, model=config["model"], alpha=config["alpha"]
        )
        overlap = sum(retained_overlap_count(f, sessions, intervals) for f in folds)
        metrics.update(
            {
                "evidence": "unsafe",
                "retained_train_overlapping": overlap,
                "exclusions": {
                    "purged": 0,
                    "embargoed": 0,
                    "retained": sum(len(f.train) for f in folds),
                },
            }
        )
        channels[name] = metrics

    vanilla_chunks_shuffled = [
        [shuffled_axis[i] for i in chunk]
        for chunk in _array_split_positions(len(shuffled_axis), n_splits)
    ]
    vanilla("vanilla-shuffled-kfold", vanilla_chunks_shuffled)
    vanilla("vanilla-chronological-kfold", _chronological_chunks(sessions, n_splits))

    for name, splitter in (
        ("purged-kfold", PurgedKFold(n_splits=n_splits, embargo_sessions=0)),
        (
            "purged-kfold-embargo",
            PurgedKFold(n_splits=n_splits, embargo_sessions=embargo),
        ),
    ):
        folds = list(splitter.split(sessions, interval_start, interval_end))
        metrics = evaluate_folds(
            folds, features, targets, model=config["model"], alpha=config["alpha"]
        )
        overlap = sum(retained_overlap_count(f, sessions, intervals) for f in folds)
        metrics.update(
            {
                "evidence": "safe",
                "retained_train_overlapping": overlap,
                "exclusions": {
                    "purged": sum(len(f.purged) for f in folds),
                    "embargoed": sum(len(f.embargoed) for f in folds),
                    "retained": sum(len(f.train) for f in folds),
                },
            }
        )
        channels[name] = metrics
    return channels


# ─────────────────────────── QuantDB 数据装配（CN） ───────────────────────────

DEFAULT_FEATURES = ["ma_gap_5", "rsi_14", "vol_std_20", "macd_hist"]


def detect_data_root(explicit=None):
    candidates = [
        explicit,
        os.environ.get("QM_DATA_ROOT"),
        "/data",
        "/home/zbox/projects/quantmind/data",
        "./data",
    ]
    for candidate in candidates:
        if candidate and os.path.isdir(os.path.join(candidate, "quantdb")):
            return candidate
    raise SystemExit("未找到 QuantDB 数据目录（可用 --data-root 或 QM_DATA_ROOT 指定）")


def _list_partitions(directory):
    return sorted(
        int(entry.name.split("=")[1])
        for entry in os.scandir(directory)
        if entry.is_dir() and entry.name.startswith("dt=")
    )


def _read_partition(path, wanted_columns):
    """按分区实际 schema 取交集读列（features_daily 存在跨期 schema 漂移）。"""
    import pandas as pd
    import pyarrow.parquet as pq

    available = set(pq.read_schema(str(path)).names)
    use = [c for c in wanted_columns if c in available]
    frame = pd.read_parquet(path, columns=use) if use else pd.DataFrame()
    return frame, [c for c in wanted_columns if c not in available]


def load_quantdb_cn(
    data_root, *, start, end, horizon, features, symbols=None, max_symbols=80
):
    """读 features_daily（因子）+ daily_forward（前复权收益自算标签），装配建模数据。"""
    import pandas as pd

    feature_dir = os.path.join(data_root, "quantdb", "6_ml_datasets", "features_daily")
    kline_dir = os.path.join(data_root, "quantdb", "1_kline_data", "daily_forward")
    feature_dates = [d for d in _list_partitions(feature_dir) if start <= d <= end]
    if not feature_dates:
        raise SystemExit("features_daily 在窗口内没有分区")
    end = min(end, feature_dates[-1])

    warnings = []
    symbol_list = None
    if not symbols:
        anchor = max(d for d in _list_partitions(kline_dir) if d <= end)
        frame, _ = _read_partition(
            os.path.join(kline_dir, f"dt={anchor}", "data.parquet"),
            ["symbol", "amount"],
        )
        frame = frame.dropna(subset=["amount"]).sort_values(
            ["amount", "symbol"], ascending=[False, True]
        )
        symbol_list = frame["symbol"].head(max_symbols).tolist()
        warnings.append(
            f"未指定 --symbols，按 {anchor} 单日成交额取前 {max_symbols} 只（演示用确定性选样，非 PIT 资产池）"
        )
    else:
        symbol_list = list(symbols)
    symbol_set = set(symbol_list)

    wanted_features = list(features)
    feature_frames, missing_report = [], {}
    for date in feature_dates:
        frame, missing = _read_partition(
            os.path.join(feature_dir, f"dt={date}", "data.parquet"),
            ["symbol", "time"] + wanted_features,
        )
        for column in missing:
            missing_report[column] = missing_report.get(column, 0) + 1
        frame = frame[frame["symbol"].isin(symbol_set)]
        if not frame.empty:
            feature_frames.append(frame)
    dropped = sorted(c for c, n in missing_report.items() if n == len(feature_dates))
    usable_features = [c for c in wanted_features if c not in dropped]
    if dropped:
        warnings.append(f"因子列在窗口内缺失、已剔除: {dropped}")
    if not usable_features:
        raise SystemExit("窗口内没有任何可用因子列")
    feature_panel = pd.concat(feature_frames, ignore_index=True)

    end_buffer = (datetime.strptime(str(end), "%Y%m%d") + timedelta(days=45)).strftime(
        "%Y%m%d"
    )
    kline_frames = []
    for date in _list_partitions(kline_dir):
        if start <= date <= int(end_buffer):
            frame, _ = _read_partition(
                os.path.join(kline_dir, f"dt={date}", "data.parquet"),
                ["symbol", "time", "close"],
            )
            kline_frames.append(frame[frame["symbol"].isin(symbol_set)])
    kline = pd.concat(kline_frames, ignore_index=True).sort_values(["symbol", "time"])
    kline["label"] = (
        kline.groupby("symbol")["close"].shift(-horizon) / kline["close"] - 1.0
    )
    kline = kline.dropna(subset=["label"])

    frame = feature_panel.merge(
        kline[["symbol", "time", "label"]], on=["symbol", "time"], how="inner"
    )
    frame = frame.dropna(subset=["label"])
    frame = frame.sort_values(["time", "symbol"]).reset_index(drop=True)
    if frame.empty:
        raise SystemExit("features_daily 与 daily_forward 合并后没有样本")
    return frame, usable_features, symbol_list, warnings


def run_quantdb(args):
    _require_numpy()

    data_root = detect_data_root(args.data_root)
    end = (
        int(args.end.replace("-", ""))
        if args.end
        else _list_partitions(
            os.path.join(data_root, "quantdb", "6_ml_datasets", "features_daily")
        )[-1]
    )
    if args.start:
        start = int(args.start.replace("-", ""))
    else:
        end_dt = datetime.strptime(str(end), "%Y%m%d") - timedelta(days=365 * 3)
        start = int(end_dt.strftime("%Y%m%d"))
    horizon = int(args.horizon) if args.horizon is not None else 5
    lookback = max(1, int(args.lookback)) if args.lookback is not None else 20
    features = [c.strip() for c in args.features.split(",") if c.strip()]

    frame, usable_features, symbol_list, warnings = load_quantdb_cn(
        data_root,
        start=start,
        end=end,
        horizon=horizon,
        features=features,
        symbols=[s.strip() for s in args.symbols.split(",")] if args.symbols else None,
        max_symbols=int(args.max_symbols),
    )
    axis = sorted(frame["time"].unique())
    session_of = {value: index for index, value in enumerate(axis)}
    sessions = [session_of[t] for t in frame["time"]]
    interval_start = [s - (lookback - 1) for s in sessions]
    interval_end = [s + horizon for s in sessions]
    features_matrix = frame[usable_features].to_numpy(dtype=float)
    targets = frame["label"].to_numpy(dtype=float)

    config = {
        "n_splits": int(args.n_splits),
        "embargo_sessions": int(args.embargo),
        "model": args.model,
        "alpha": float(args.alpha),
        "seed": int(args.seed),
        "horizon": horizon,
        "lookback": lookback,
    }
    channels = run_channel_comparison(
        sessions, interval_start, interval_end, features_matrix, targets, config
    )
    dataset = {
        "market": "CN",
        "sessions": len(axis),
        "symbols": len(set(frame["symbol"])),
        "observations": int(len(frame)),
        "start": str(axis[0].date()),
        "end": str(axis[-1].date()),
        "features": usable_features,
        "label_definition": f"daily_forward(前复权) close 的 {horizon} 日前视收益",
        "interval_definition": f"[session-{lookback - 1}, session+{horizon}]（闭区间，单位=交易日序号）",
    }
    return _finalize_report("quantdb", config, dataset, channels, warnings, args)


def run_demo(args):
    """合成数据演示：AR(1) 潜状态 + 重叠标签，四个通道 + 结构不变量自检。"""
    _require_numpy()
    config = {
        "n_splits": int(args.n_splits),
        "embargo_sessions": int(args.embargo),
        "model": args.model,
        "alpha": float(args.alpha),
        "seed": int(args.seed),
        "horizon": int(args.horizon) if args.horizon is not None else 10,
        "lookback": max(1, int(args.lookback)) if args.lookback is not None else 20,
    }
    rng = np.random.default_rng(config["seed"])
    n_symbols, n_sessions = 60, 150
    horizon, lookback = config["horizon"], config["lookback"]
    # AR(1) 慢变潜状态 + i.i.d. 噪声收益：标签（未来 h 日累计收益）天然重叠，
    # 且相邻日特征高度相关 —— 这正是乱序 K 折会泄漏、Purge 必然改变结论的场景。
    # 泄漏幅度 ∝ 重叠天数 / 总 session 数 × 相邻样本特征相似度，故演示取短轴 + 强持续。
    phi, signal, noise = 0.99, 0.0015, 0.012
    total = n_sessions + lookback + horizon + 2
    state = np.zeros((total, n_symbols))
    for t in range(1, total):
        state[t] = phi * state[t - 1] + rng.normal(0.0, 1.0, n_symbols)
    returns = signal * state[1:] + noise * rng.normal(0.0, 1.0, (total - 1, n_symbols))
    cumulative = np.vstack([np.zeros((1, n_symbols)), np.cumsum(returns, axis=0)])

    sessions_grid = np.repeat(np.arange(n_sessions), n_symbols)
    symbols_grid = np.tile(np.arange(n_symbols), n_sessions)
    # 特征窗口必须 ≤ lookback，声明的信息区间才与真实用量一致（否则区间声明偏窄 = 漏 Purge）
    windows = sorted(w for w in {1, 5, lookback} if w <= lookback)
    trailing = np.column_stack(
        [
            cumulative[sessions_grid + lookback, symbols_grid]
            - cumulative[sessions_grid + lookback - window, symbols_grid]
            for window in windows
        ]
    )
    labels = np.array(
        cumulative[sessions_grid + lookback + 1 + horizon, symbols_grid]
        - cumulative[sessions_grid + lookback + 1, symbols_grid]
    )
    features = trailing
    sessions = sessions_grid.tolist()
    interval_start = [s - (lookback - 1) for s in sessions]
    interval_end = [s + horizon for s in sessions]

    # 结构自检 1：CPCV 路径分解性质（N=6, k=2 -> 5 条路径；N=6, k=3 -> 10 条路径）
    for n_groups, n_test_groups in ((6, 2), (6, 3)):
        paths = cpcv_path_decomposition(n_groups, n_test_groups)
        assert len(paths) == math.comb(n_groups - 1, n_test_groups - 1), (
            "CPCV 路径数应为 C(N-1,k-1)"
        )
        for path in paths:
            assert sorted(g for _, g in path) == list(range(n_groups)), (
                "每条路径必须覆盖每组一次"
            )
        combos_of = {}
        for combination, group in ((c, g) for path in paths for c, g in path):
            combos_of.setdefault(combination, []).append(group)
        assert all(
            len(set(gs)) == len(gs) == n_test_groups for gs in combos_of.values()
        ), "同组合测试组须落在不同路径"

    intervals = tuple(zip(interval_start, interval_end, strict=True))
    # 结构自检 2：CPCV 全组合保留训练集重叠必须为 0；Walk-Forward 保留训练集全部严格早于测试块
    cpcv = CombinatorialPurgedCV(
        n_groups=6, n_test_groups=2, embargo_sessions=config["embargo_sessions"]
    )
    cpcv_folds = list(cpcv.split(sessions, interval_start, interval_end))
    assert len(cpcv_folds) == cpcv.combination_count
    cpcv_overlap = sum(
        retained_overlap_count(f, sessions, intervals) for f in cpcv_folds
    )
    assert cpcv_overlap == 0, "CPCV 保留训练集不得与测试区间重叠"
    walk_forward = CausalWalkForward(
        n_splits=3, test_sessions=10, pre_test_gap_sessions=5
    )
    wf_folds = list(walk_forward.split(sessions, interval_start, interval_end))
    wf_overlap = sum(retained_overlap_count(f, sessions, intervals) for f in wf_folds)
    assert wf_overlap == 0, "Walk-Forward 保留训练集不得与测试区间重叠"
    for fold in wf_folds:
        test_start = min(sessions[i] for i in fold.test)
        assert all(intervals[i][1] < test_start for i in fold.train), (
            "训练样本信息终点必须严格早于测试块"
        )
    structural_checks = {
        "cpcv": {
            "n_groups": 6,
            "n_test_groups": 2,
            "combinations": cpcv.combination_count,
            "path_count": cpcv.path_count,
            "retained_train_overlapping": cpcv_overlap,
        },
        "causal_walk_forward": {
            "n_splits": 3,
            "test_sessions": 10,
            "pre_test_gap_sessions": 5,
            "retained_train_overlapping": wf_overlap,
        },
        "vanilla_chronological_retained_train_overlapping": sum(
            retained_overlap_count(f, sessions, intervals)
            for f in _vanilla_folds(
                sessions, _chronological_chunks(sessions, config["n_splits"])
            )
        ),
    }

    channels = run_channel_comparison(
        sessions, interval_start, interval_end, features, labels, config
    )
    dataset = {
        "market": "SYNTHETIC",
        "sessions": n_sessions,
        "symbols": n_symbols,
        "observations": int(len(labels)),
        "features": [f"trailing_{w}d" for w in windows],
        "label_definition": f"未来 {horizon} 日累计收益（重叠标签）",
        "interval_definition": f"[session-{lookback - 1}, session+{horizon}]",
    }
    warnings = ["合成数据仅用于演示区间重叠结构与通道差异，不代表真实市场结论"]
    return _finalize_report(
        "demo",
        config,
        dataset,
        channels,
        warnings,
        args,
        structural_checks=structural_checks,
    )


def _finalize_report(
    mode, config, dataset, channels, warnings, args, structural_checks=None
):
    safe_channels = [c for c, m in channels.items() if m["evidence"] == "safe"]
    overlap_ok = all(
        channels[c]["retained_train_overlapping"] == 0 for c in safe_channels
    )
    purged = channels.get("purged-kfold-embargo", {})
    vanilla = channels.get("vanilla-shuffled-kfold", {})
    leaked_samples = vanilla.get("retained_train_overlapping", 0)
    if purged.get("exclusions", {}).get("embargoed", 0) == 0:
        warnings.append(
            "NO_INCREMENTAL_EXCLUSION_AFTER_FULL_INTERVAL_PURGE：完整区间 Purge 已覆盖 "
            "Embargo 会剔除的样本（lookback 越大越常见），Embargo 已执行但增量=0，不是失效"
        )
    calibration = {
        "claim": "同数据同模型下 purged+embargo 测试集 IC 应 ≤ vanilla（泄漏被堵住通常使指标下降）",
        "vanilla_ic_pooled": vanilla.get("ic_pooled"),
        "purged_embargo_ic_pooled": purged.get("ic_pooled"),
        "purged_le_vanilla": (
            None
            if purged.get("ic_pooled") is None or vanilla.get("ic_pooled") is None
            else purged["ic_pooled"] <= vanilla["ic_pooled"]
        ),
        "vanilla_retained_train_overlapping": leaked_samples,
        "note": "如实报告：IC 差不是纯泄漏因果量；结构不变量（安全通道重叠=0）才是硬证据",
    }
    report = {
        "schema_version": SCHEMA_VERSION,
        "tool": "ml-purged-cv",
        "script": "purged_cv.py",
        "status": "success",
        "mode": mode,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "config": config,
        "dataset": dataset,
        "channels": channels,
        "leakage_control_status": "PASS" if overlap_ok else "FAIL",
        "calibration": calibration,
        "structural_checks": structural_checks,
        "warnings": warnings
        + ["结构控制与指标不是盈利或可上线证明；验证结论不构成投资建议"],
    }
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
        report["_out_written"] = args.out
    return report


# ─────────────────────────── CLI ───────────────────────────


def build_parser():
    parser = argparse.ArgumentParser(
        description="ml-purged-cv：Purged K-Fold / Embargo / CPCV / Causal Walk-Forward 索引生成 + QuantDB 演示"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--demo", action="store_true", help="合成数据演示（需 numpy）")
    mode.add_argument(
        "--quantdb",
        action="store_true",
        help="读 QuantDB 本地 parquet（需 pandas+numpy）",
    )
    parser.add_argument(
        "--market",
        default="CN",
        help="仅支持 CN（HK/US 的 daily_forward 不复权，标签口径需另定）",
    )
    parser.add_argument(
        "--data-root", default=None, help="数据根目录（含 quantdb/），默认自动探测"
    )
    parser.add_argument(
        "--symbols", default=None, help="逗号分隔后缀式代码，如 000001.SZ,600036.SH"
    )
    parser.add_argument(
        "--max-symbols", default=80, help="未显式给 --symbols 时按锚日成交额取前 N 只"
    )
    parser.add_argument(
        "--start", default=None, help="窗口起点 YYYY-MM-DD（默认 end 前 3 年）"
    )
    parser.add_argument(
        "--end", default=None, help="窗口终点 YYYY-MM-DD（默认最新分区）"
    )
    parser.add_argument(
        "--horizon",
        default=None,
        help="标签前视天数 h（quantdb 默认 5，demo 默认 10；>1 时标签区间重叠，Purge 的必要场景）",
    )
    parser.add_argument(
        "--lookback",
        default=None,
        help="因子回看天数（信息区间起点前移量；quantdb/demo 均默认 20，对 ma20 类因子）",
    )
    parser.add_argument(
        "--features", default=",".join(DEFAULT_FEATURES), help="逗号分隔因子列名"
    )
    parser.add_argument("--n-splits", default=5, help="折数")
    parser.add_argument("--embargo", default=5, help="Embargo 隔离的交易日数")
    parser.add_argument(
        "--model",
        default="ridge",
        choices=["ridge", "ols", "hgb"],
        help="每折模型（hgb 需 sklearn）",
    )
    parser.add_argument("--alpha", default=1.0, help="Ridge 正则强度")
    parser.add_argument("--seed", default=42, help="乱序/模型随机种子")
    parser.add_argument(
        "--out",
        default=None,
        help="JSON 报告落盘路径（建议容器内 /data/reports/purged-cv/）",
    )
    parser.add_argument(
        "--print-json", action="store_true", help="把完整 JSON 报告打到 stdout"
    )
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.market.upper() != "CN":
        raise SystemExit(
            "暂只支持 --market CN：HK/US 的 daily_forward 不复权，前视收益口径需另定"
        )
    started = time.time()
    report = run_demo(args) if args.demo else run_quantdb(args)
    report["elapsed_sec"] = round(time.time() - started, 2)
    if args.print_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(
            f"[ml-purged-cv] mode={report['mode']} status={report['status']} "
            f"leakage_control={report['leakage_control_status']} "
            f"dataset={report['dataset']['observations']} obs / {report['dataset']['sessions']} sessions"
        )
        print(
            f"{'channel':28s} {'IC(pooled)':>10s} {'IC(fold avg)':>13s} {'rankIC':>8s} {'overlap':>8s}"
        )
        for name, metrics in report["channels"].items():
            print(
                f"{name:28s} {metrics['ic_pooled']!s:>10s} {metrics['ic_mean_fold']!s:>13s} "
                f"{metrics['rank_ic_pooled']!s:>8s} {metrics['retained_train_overlapping']:>8d}"
            )
        print(
            f"calibration: purged+embargo IC <= vanilla: {report['calibration']['purged_le_vanilla']}"
        )
        for warning in report["warnings"]:
            print(f"  [warn] {warning}")
        if report.get("_out_written"):
            print(f"报告落盘: {report['_out_written']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
