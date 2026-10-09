"""非 SOTA 因子清理判据（P3）——纯函数：**只建议不删**，每条判据带数字证据。

定位：因子池会随挖掘轮次膨胀，弱/冗余因子拖低注入摘要的信噪比。本模块
给「清理建议」面板产出候选清单——用户看到判据后**自己决定**归档哪些；
归档 = ``archived_at`` 时间戳（见 ``factor_pool_contract``），不是删除，
随时可恢复。任何自动化都不得绕过这一步替用户删因子。

三条判据（互不依赖，可叠加）：

``duplicate``（冗余被支配）
    与池内某因子 |ρ| ≥ ``corr_dup``（默认 0.9），**且对方更强**。强弱比较
    ICIR 优先（质量证据、不受疲劳计数影响），双方 ICIR 都缺失时才退到
    池评分。双方无可比指标、或平手 → **不下裁决**——宁可漏报，不可把
    「比较不了」说成「更弱」。
``weak_icir``（预测力垫底）
    ICIR ≤ 池内后 ``weak_icir_quantile`` 分位（默认 20%）。有效样本少于
    ``min_icir_sample`` 不判（小样本分位无意义）；全员同值不判（没有
    分化，人人都是「中位数」）；**ICIR 缺失 ≠ 弱**（缺失是「不知道」，
    绝不按 0 参与——与 pool_scoring 同一条纪律）。
``no_diversity``（零多样性贡献）
    留一贡献 ≤ 0：把这个因子移出池，有效因子数不降。缺失（未跑过带
    factor_quality 的池刷新）= 不知道，不判。

severity：``duplicate`` 单独即 high（信息已被更强副本覆盖）；两条软判据
同时命中 → high；单条软判据 → medium。输出按 (severity, factor_id)
排序，同输入同输出（注入/面板可复现的同一纪律）。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

#: 判据代码（UI 徽章/汇总的稳定键，改文案不改这里）
REASON_DUPLICATE = "duplicate"
REASON_WEAK_ICIR = "weak_icir"
REASON_NO_DIVERSITY = "no_diversity"


@dataclass(frozen=True)
class CleanupCriteria:
    """判据阈值（yaml ``cleanup:`` 可覆盖，见 pool_service._cleanup_criteria）。"""

    corr_dup: float = 0.9
    weak_icir_quantile: float = 0.2
    min_icir_sample: int = 5


@dataclass(frozen=True)
class CleanupCandidate:
    """参与判据的池行快照（由 pool_service 从库里装配）。

    ``max_pool_corr`` 语义是 **|ρ|**（调用方取好绝对值；本模块内部再取一次
    幂等）。缺失一律 None，不按 0 参与。
    """

    factor_id: str
    factor_name: str = ""
    icir: float | None = None
    pool_score: float | None = None
    max_pool_corr: float | None = None
    max_pool_corr_with: str | None = None
    diversity_contrib: float | None = None


@dataclass(frozen=True)
class CleanupReason:
    """一条判据：``code`` 稳定、``label`` 人读、``detail`` 必须带数字证据。"""

    code: str
    label: str
    detail: str


@dataclass(frozen=True)
class CleanupSuggestion:
    candidate: CleanupCandidate
    severity: str  # "high" | "medium"
    reasons: tuple[CleanupReason, ...]


@dataclass(frozen=True)
class CleanupReport:
    """一次评估的完整结果；阈值一并回传（UI 展示「判据是什么」）。"""

    suggestions: tuple[CleanupSuggestion, ...]
    weak_icir_threshold: float | None
    icir_sample_size: int


def _comparison(
    mine: CleanupCandidate, theirs: CleanupCandidate
) -> tuple[str, float, float] | None:
    """(基准名, 我的值, 对方值)；无可共同指标 → None（不下裁决）。"""
    if mine.icir is not None and theirs.icir is not None:
        return ("ICIR", float(mine.icir), float(theirs.icir))
    if mine.pool_score is not None and theirs.pool_score is not None:
        return ("池评分", float(mine.pool_score), float(theirs.pool_score))
    return None


def _duplicate_reason(
    candidate: CleanupCandidate,
    by_id: dict[str, CleanupCandidate],
    criteria: CleanupCriteria,
) -> CleanupReason | None:
    corr = candidate.max_pool_corr
    other_id = candidate.max_pool_corr_with
    if corr is None or not other_id:
        return None
    threshold = abs(float(criteria.corr_dup))
    if abs(float(corr)) < threshold:
        return None
    other = by_id.get(str(other_id))
    if other is None:
        return None  # 对手不在本 scope（被 universe/状态过滤掉）→ 无法核实支配关系
    comparison = _comparison(candidate, other)
    if comparison is None:
        return None
    basis, mine, theirs = comparison
    if mine >= theirs:
        return None  # 平手不裁决；我更强也不该被标记
    name = other.factor_name or other.factor_id
    return CleanupReason(
        code=REASON_DUPLICATE,
        label="冗余被支配",
        detail=(
            f"与「{name}」|ρ|={abs(float(corr)):.2f}（{basis} {mine:.3f} vs {theirs:.3f}）"
            "，信息已被更强副本覆盖"
        ),
    )


def _weak_icir_threshold(
    values: Sequence[float], criteria: CleanupCriteria
) -> float | None:
    """池内 ICIR 的后 q 分位（无分化或样本不足 → None，不判弱）。"""
    n = len(values)
    if n < max(2, int(criteria.min_icir_sample)):
        return None
    q = min(max(float(criteria.weak_icir_quantile), 0.0), 1.0)
    # −1e-9 抵消浮点误差（0.2×10 = 2.0000…4，ceil 会错取第 3 小）
    k = max(1, math.ceil(q * n - 1e-9))
    threshold = sorted(float(v) for v in values)[k - 1]
    if threshold >= max(float(v) for v in values):
        return None  # 阈值顶到最大值 = 全员同值，没有「垫底」可言
    return threshold


def evaluate_cleanup(
    candidates: Sequence[CleanupCandidate],
    criteria: CleanupCriteria | None = None,
) -> CleanupReport:
    """全池评估 → 建议清单（按 severity 高在前、factor_id 升序）。"""
    criteria = criteria or CleanupCriteria()
    cands = list(candidates)
    by_id = {c.factor_id: c for c in cands}

    icir_values = [float(c.icir) for c in cands if c.icir is not None]
    weak_threshold = _weak_icir_threshold(icir_values, criteria)
    q_pct = int(round(min(max(float(criteria.weak_icir_quantile), 0.0), 1.0) * 100))

    suggestions: list[CleanupSuggestion] = []
    for cand in cands:
        reasons: list[CleanupReason] = []

        duplicate = _duplicate_reason(cand, by_id, criteria)
        if duplicate is not None:
            reasons.append(duplicate)

        if (
            weak_threshold is not None
            and cand.icir is not None
            and float(cand.icir) <= weak_threshold
        ):
            reasons.append(
                CleanupReason(
                    code=REASON_WEAK_ICIR,
                    label="预测力垫底",
                    detail=(
                        f"ICIR {float(cand.icir):.3f} ≤ 池内后 {q_pct}% 分位"
                        f"（{weak_threshold:.3f}，样本 {len(icir_values)}）"
                    ),
                )
            )

        if cand.diversity_contrib is not None and float(cand.diversity_contrib) <= 0.0:
            reasons.append(
                CleanupReason(
                    code=REASON_NO_DIVERSITY,
                    label="零多样性贡献",
                    detail=(
                        f"多样性贡献 {float(cand.diversity_contrib):+.3f} ≤ 0："
                        "移除后池有效因子数不降"
                    ),
                )
            )

        if not reasons:
            continue
        has_duplicate = any(r.code == REASON_DUPLICATE for r in reasons)
        severity = "high" if has_duplicate or len(reasons) >= 2 else "medium"
        suggestions.append(
            CleanupSuggestion(candidate=cand, severity=severity, reasons=tuple(reasons))
        )

    suggestions.sort(
        key=lambda s: (0 if s.severity == "high" else 1, s.candidate.factor_id)
    )
    return CleanupReport(
        suggestions=tuple(suggestions),
        weak_icir_threshold=weak_threshold,
        icir_sample_size=len(icir_values),
    )


__all__ = [
    "REASON_DUPLICATE",
    "REASON_NO_DIVERSITY",
    "REASON_WEAK_ICIR",
    "CleanupCandidate",
    "CleanupCriteria",
    "CleanupReason",
    "CleanupReport",
    "CleanupSuggestion",
    "evaluate_cleanup",
]
