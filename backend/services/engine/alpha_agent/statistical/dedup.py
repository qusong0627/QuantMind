"""统计派 · 入选因子相关性去重 + 稳定因子身份（移植 Tier 3 / T-S3-03）。

两件事：

1. **值级相关性去重**（:func:`greedy_select` / :func:`dedup_selected`）——
   候选两两「逐日截面 Spearman 的时间均值」（``ic_stats.daily_rank_corr``，
   与 IC 同族口径）。沿选择分序贪心：与任何**已保留**者 |ρ| ≥ ``reject``
   （0.9）→ 拒；``[warn, reject)`` → 保留但记警。此 0.9/0.8 是**批内**阈值
   （候选池是固定目录，去重防同批席位重复），与库级值级闸
   （``rd_mined_materialize`` 0.5 拒 / 0.4 警，T-FM-03 2026-10-11 起）
   **刻意分开**——批内和库级不是一回事。两条语义（2026-10-11 起）：

   - **选择期**（:func:`greedy_select`，引擎主路径）：Top-K 是**席位**——
     被拒者不占席，后续候选继续**递补**，凑满 K 即停。``provider`` 按需
     取值，K 席之外（或递补后才轮到）的候选不白算；
   - **事后全判**（:func:`dedup_selected`，k=∞ 特例）：对给定全集逐个判决，
     被拒者不再当 champion，**不补位**。当前仓内**无生产消费方**（保留为
     兼容 API；物化器自带值级去重实现），语义仅由测试守护。

   ρ 算不出的对（无重叠日/全并列）按「无证据」处理：保留并以 NaN 记账，
   绝不把「没算出相关」当「不相关」之外的结论。

2. **稳定因子身份**（:func:`stable_factor_id`）——以不可变任务指纹溯源，
   使用 tenant/user 双维与规范 JSON 编码的 SHA256，避免跨租户及分隔符碰撞。
   同一份挖掘配置（P3 指纹相同）重跑 → 同一 ``factor_id`` → 落库走
   ``ON CONFLICT DO UPDATE`` **幂等刷新**，不再每次重跑堆一行重复因子；
   不同配置 = 不同实验，各留一行（跨版本同候选的值级查重仍归物化器，
   那是离线全库操作，不在这里做）。无指纹（手工直跑/旧任务）如实退回
   任务级身份（``scheme="task"``），metadata 记录 scheme，可追溯。

   为什么不再挂 ``factor_identity.find_duplicate``：统计派候选是**固定目录**
   （32 个 key 恒定），名称天然跨任务重复——用名称级查重会把「同候选的
   新一次实验」全部判重拒掉。统计派的正确语义是「同一实验幂等刷新 +
   值级去重挤重复」，不是「名字重复就不许落库」。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from backend.shared.owner_scope import OwnerScope

from . import ic_stats as _ic

#: 值级去重阈值——**批内**口径（0.9 拒 / 0.8 警）；与库级闸
#: （``rd_mined_materialize`` 0.5 拒 / 0.4 警，T-FM-03）刻意分开，不要对齐
DEFAULT_CORR_REJECT = 0.9
DEFAULT_CORR_WARN = 0.8

#: 稳定身份前缀（与 RD-Agent 系因子的 id 空间区分开）
IDENTITY_PREFIX = "statistical-v2-owner"
#: 身份口径：config = 由 P3 任务指纹定身份；task = 无指纹退回任务级
SCHEME_CONFIG = "config"
SCHEME_TASK = "task"


@dataclass(frozen=True)
class DedupReport:
    """一次值级去重的全量记录（kept 序 = 步行序，即选择分降序/递补后的最终席位）。"""

    kept: tuple[str, ...]
    #: (key, matched_key, ρ)——被拒者及其命中对象
    dropped: tuple[tuple[str, str, float], ...]
    #: (key_a, key_b, ρ)——保留但对内 ρ ∈ [warn, reject)
    warnings: tuple[tuple[str, str, float], ...]
    n_pairs: int  # 实际评估过的对数（含算不出 ρ 的对）
    reject: float = DEFAULT_CORR_REJECT
    warn: float = DEFAULT_CORR_WARN
    #: 保留 key → 与其他保留者的最大 |ρ|（对称口径；算不出 → 不在表里）
    max_corr: dict[str, float] = field(default_factory=dict)

    def max_abs_corr(self, key: str) -> float:
        """该保留因子与其他保留因子的最大 |ρ|（单因子入选/算不出 → NaN）。"""
        value = self.max_corr.get(key)
        return float(value) if value is not None else float("nan")

    def warn_note(self, key: str) -> str:
        """该保留因子的相关性警语（无 → 空串）。"""
        hits = [
            (other if key == a else a, rho)
            for a, other, rho in self.warnings
            if key in (a, other)
        ]
        if not hits:
            return ""
        other, rho = max(hits, key=lambda item: abs(item[1]))
        return f"与 {other} 相关偏高（ρ={rho:.2f}，阈值 {self.reject:g}）"


def greedy_select(
    order: Sequence[str],
    *,
    provider: Callable[[str], pd.Series],
    top_k: int,
    reject: float = DEFAULT_CORR_REJECT,
    warn: float = DEFAULT_CORR_WARN,
    min_names: int = _ic.DEFAULT_MIN_NAMES_PER_DAY,
    on_drop: Callable[[str], None] | None = None,
) -> DedupReport:
    """按 ``order``（选择分降序）做**选择期**贪心去重：凑满 ``top_k`` 即停。

    与 :func:`dedup_selected`（事后全判、不补位）的差别在**递补**：与已保留
    者 |ρ| ≥ ``reject`` 的候选被跳过、不占 K 席，后续候选继续递补——Top-K
    是席位（信息量相同的因子只留代表），不是「先到先得、挤掉就空着」。
    ``provider`` 按需取因子值（调用方可懒计算 + 缓存）：K 席之外或递补后
    才轮到的候选根本不会被求值。

    ``on_drop(key)``：候选**被拒判决的当下**回调一次（保留者不回调）——给
    调用方释放该候选的缓存值用（面板百万行级时，被拒者攒在缓存里会把峰值
    从 O(K+1) 抬到 O(走过的候选数)）。回调异常按调用方 bug 直接抛，不吞。

    ``provider`` 缺 key 应抛 KeyError（调用方有 bug，不许静默跳过一对）；
    ρ 算不出的对（无重叠日/全并列）按「无证据」处理：保留并记 NaN。
    """
    if not 0.0 <= warn < reject <= 1.0:
        raise ValueError(f"相关性阈值非法：warn={warn!r} reject={reject!r}")
    if top_k < 1:
        raise ValueError(f"top_k 至少为 1（收到 {top_k!r}）")
    kept: list[str] = []
    dropped: list[tuple[str, str, float]] = []
    warnings: list[tuple[str, str, float]] = []
    pairs: list[tuple[str, str, float]] = []
    n_pairs = 0
    for key in order:
        if len(kept) >= top_k:
            break
        series = provider(key)
        best_rho = float("nan")
        best_against = ""
        for champion in kept:
            rho, _n_days = _ic.daily_rank_corr(
                series, provider(champion), min_names=min_names
            )
            n_pairs += 1
            if np.isfinite(rho):
                pairs.append((key, champion, float(rho)))
                if not np.isfinite(best_rho) or abs(rho) > abs(best_rho):
                    best_rho, best_against = float(rho), champion
        if np.isfinite(best_rho) and abs(best_rho) >= reject:
            dropped.append((key, best_against, best_rho))
            if on_drop is not None:
                on_drop(key)
            continue
        if np.isfinite(best_rho) and abs(best_rho) >= warn:
            warnings.append((key, best_against, best_rho))
        kept.append(key)
    kept_set = set(kept)
    max_corr: dict[str, float] = {}
    for a, b, rho in pairs:
        if a not in kept_set or b not in kept_set:
            continue
        for side in (a, b):
            max_corr[side] = max(max_corr.get(side, 0.0), abs(rho))
    return DedupReport(
        kept=tuple(kept),
        dropped=tuple(dropped),
        warnings=tuple(warnings),
        n_pairs=n_pairs,
        reject=reject,
        warn=warn,
        max_corr=max_corr,
    )


def dedup_selected(
    factors: Mapping[str, pd.Series],
    *,
    order: Sequence[str],
    reject: float = DEFAULT_CORR_REJECT,
    warn: float = DEFAULT_CORR_WARN,
    min_names: int = _ic.DEFAULT_MIN_NAMES_PER_DAY,
) -> DedupReport:
    """定选集**事后全判**去重（按 ``order`` 降序贪心，不补位）。

    选择期主路径已收敛到 :func:`greedy_select`（带递补、按需取值）；本函数
    是它 ``top_k = len(order)``（走完全集、从不提前停）的特例。当前仓内
    **无生产消费方**（保留兼容 API，语义 = greedy_select 的 k=∞ 特例），
    行为仅由测试守护。

    ``factors``：key → 挖掘窗内（可交易 ∧ 中性化后）的因子值序列；缺 key
    即抛 KeyError（调用方有 bug，不许静默跳过一对）。
    """
    return greedy_select(
        order,
        provider=factors.__getitem__,
        # 空 order 也要过阈值校验（top_k ≥ 1），步行空转即退——不是「无阈值」
        top_k=max(1, len(order)),
        reject=reject,
        warn=warn,
        min_names=min_names,
    )


def stable_factor_id(
    *,
    tenant_id: str,
    user_id: str,
    market: str,
    universe: str,
    candidate_key: str,
    task_fingerprint: str = "",
    task_id: str = "",
) -> tuple[str, str]:
    """Return an owner-scoped identity; require an actual experiment anchor.

    Scope construction validates completeness, not authorization. Callers must
    supply the authenticated owner or the owner read from the durable task.
    """
    owner = OwnerScope(tenant_id, user_id)
    if market != "a_share":
        raise ValueError("Only A-share statistical factors are supported")
    for value in (universe, candidate_key):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Factor universe and candidate key are required")
    for value in (task_fingerprint, task_id):
        if not isinstance(value, str):
            raise ValueError("Factor experiment anchors must be text")
    fingerprint = task_fingerprint.strip()
    anchor = fingerprint or task_id.strip()
    if not anchor:
        raise ValueError("A task fingerprint or task ID is required")
    scheme = SCHEME_CONFIG if fingerprint else SCHEME_TASK
    raw = json.dumps(
        [
            IDENTITY_PREFIX,
            owner.tenant_id,
            owner.user_id,
            market,
            universe,
            candidate_key,
            scheme,
            anchor,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest(), scheme
