"""池检索打分纯函数（AlphaPROBE 启发，**只搬思想**）——无 IO、无 DB、可单测。

打分链（全部系数可配，缺省见 ``ScoringParams``）::

    q_norm     = 池内 ICIR 平均秩分位（缺失 → None，**不进召回**，绝不按 0 参与）
    fatigue    = 1 / (1 + w·times_retrieved)
    redundancy = 1 - λ·max(0, max_pool_corr)     （None = 未知 → 不惩罚）
    freshness  = 0.5 ** (age_days / halflife)    （None = 未知 → 1.0 中性）
    score      = q_norm × fatigue × redundancy × freshness

设计裁决（写进 docstring 供后人）：
* **缺失的 ICIR 不参与召回**：按 0 参与等于判它最差，按 1.0 等于判它最好，都是
  编造。池内大多数因子来自已完成回测、ICIR 在，缺失是少数（服务层记数告警）。
* **冗余惩罚只取池内最大 |ρ|（静态存储值），不做选中集上的 MMR**：v1 的
  ``max_pool_corr`` 是 upsert 时算好落库的单数，贪心集内相似度需要两两矩阵，
  成本高一个量级、收益未验证——留为后续扩展点（参数形状已留 λ）。
* **同分按 ``factor_id`` 升序**：注入块要可复现（同输入同输出），哈希序不行。
* 摘要里的缺失指标一律「—」——写进 prompt 的「0.0000」会教 LLM 把缺失当事实。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

#: 显示面缺失占位（与前端 metricRegistry 的 MISSING_METRIC_TEXT 同符号）
MISSING_TEXT = "—"

#: 注入摘要的默认上限（字符数）；超限先砍低分条目，永远保留标题行
DEFAULT_MAX_DIGEST_CHARS = 2400

DIGEST_TITLE = "### 历史挖掘记忆（因子池检索，仅供方向参考）"
DIGEST_HINT = (
    "以上为本池历史挖出因子的表现摘要；**仅参考方向与思路，勿直接复名或照抄公式**。"
)


@dataclass(frozen=True)
class PoolCandidate:
    """参与召回排序的池内因子（由 pool_service 从库里装配）。"""

    factor_id: str
    icir: float | None = None
    times_retrieved: int = 0
    max_pool_corr: float | None = None
    age_days: float | None = None


@dataclass(frozen=True)
class ScoringParams:
    fatigue_weight: float = 0.5
    redundancy_lambda: float = 0.5
    freshness_halflife_days: float = 90.0


@dataclass(frozen=True)
class ScoredCandidate:
    candidate: PoolCandidate
    q_norm: float
    score: float


def icir_percentiles(values: Sequence[float | None]) -> list[float | None]:
    """池内 ICIR 的平均秩分位 ∈ (0, 1]；缺失位保持 None。最高者 = 1.0。

    并列取平均秩（两个并列占秩 1,2 → 1.5）；只出现一个值 → 1.0。
    """
    present = [(i, float(v)) for i, v in enumerate(values) if v is not None]
    out: list[float | None] = [None] * len(values)
    if not present:
        return out
    n = len(present)
    if n == 1:
        out[present[0][0]] = 1.0
        return out
    order = sorted(present, key=lambda t: (t[1], t[0]))
    # 平均秩：对每个值找等值区间 [lo, hi]，秩 = (lo+1+hi+1)/2…直接用 1-based 平均
    ranks: dict[int, float] = {}
    lo = 0
    while lo < n:
        hi = lo
        while hi + 1 < n and order[hi + 1][1] == order[lo][1]:
            hi += 1
        avg_rank = (lo + 1 + hi + 1) / 2.0
        for k in range(lo, hi + 1):
            ranks[order[k][0]] = avg_rank
        lo = hi + 1
    for i, _v in present:
        out[i] = ranks[i] / n
    return out


def freshness_score(age_days: float | None, halflife_days: float) -> float:
    """指数半衰新鲜度；未知年龄中性 1.0；负年龄截到 1.0（时钟偏移不奖励）。"""
    if age_days is None or halflife_days <= 0:
        return 1.0
    age = max(0.0, float(age_days))
    return float(0.5 ** (age / halflife_days))


def retrieval_score(
    q_norm: float, candidate: PoolCandidate, params: ScoringParams
) -> float:
    """四因子相乘的检索分（见模块 docstring 公式）。"""
    fatigue = 1.0 / (1.0 + params.fatigue_weight * max(0, candidate.times_retrieved))
    corr = candidate.max_pool_corr
    redundancy = (
        1.0 - params.redundancy_lambda * max(0.0, float(corr))
        if corr is not None
        else 1.0
    )
    fresh = freshness_score(candidate.age_days, params.freshness_halflife_days)
    return float(q_norm) * fatigue * redundancy * fresh


def rank_candidates(
    candidates: Iterable[PoolCandidate], params: ScoringParams | None = None
) -> list[ScoredCandidate]:
    """按 score 降序（同分 factor_id 升序）排出可召回清单；无 ICIR 者剔除。"""
    params = params or ScoringParams()
    cands = list(candidates)
    pcts = icir_percentiles([c.icir for c in cands])
    scored: list[ScoredCandidate] = []
    for cand, q_norm in zip(cands, pcts, strict=True):
        if q_norm is None:
            continue
        scored.append(
            ScoredCandidate(
                candidate=cand,
                q_norm=float(q_norm),
                score=retrieval_score(float(q_norm), cand, params),
            )
        )
    scored.sort(key=lambda s: (-s.score, s.candidate.factor_id))
    return scored


def select_topk(
    candidates: Iterable[PoolCandidate],
    k: int,
    *,
    exclude: Iterable[str] = (),
    params: ScoringParams | None = None,
) -> list[ScoredCandidate]:
    """取 score 最高的 k 个；``exclude``（本任务自己的因子）先剔。"""
    if k <= 0:
        return []
    excluded = {str(x) for x in exclude}
    ranked = rank_candidates(
        (c for c in candidates if c.factor_id not in excluded), params
    )
    return ranked[:k]


@dataclass(frozen=True)
class DigestEntry:
    """注入摘要的一条（渲染层只消费展示字段，不碰 DB）。"""

    factor_name: str
    formula: str
    ic: float | None = None
    icir: float | None = None
    pfs: float | None = None
    max_pool_corr: float | None = None
    round_label: str | None = None


@dataclass(frozen=True)
class PoolSota:
    """池整体水平线（给 LLM 一个「要超越的标杆」，进摘要标题下方一行）。

    ``best_ic`` 语义是**最大 |IC|**（因子方向可翻转，负号不是强弱）；
    icir/pfs 取原始最大值。缺失一律「—」——写 0 会教 LLM 把缺失当事实。
    """

    count: int
    best_ic: float | None = None
    best_icir: float | None = None
    best_pfs: float | None = None

    def render(self) -> str:
        return (
            f"池内共 {self.count} 条已完成回测因子；当前标杆："
            f"max|IC|={_fmt(self.best_ic)}、ICIR={_fmt(self.best_icir)}、"
            f"PFS={_fmt(self.best_pfs)}。"
            "新因子需在这些口径上具备竞争力，且与池内因子保持正交。"
        )


def _fmt(value: float | None) -> str:
    return MISSING_TEXT if value is None else f"{float(value):.4f}"


def _render_entry(index: int, entry: DigestEntry) -> str:
    parts = [
        f"{index}. `{entry.factor_name}`",
        f"IC={_fmt(entry.ic)} ICIR={_fmt(entry.icir)} PFS={_fmt(entry.pfs)}"
        f" 池内max|ρ|={_fmt(entry.max_pool_corr)}",
    ]
    if entry.round_label:
        parts.append(entry.round_label)
    formula = (entry.formula or "").strip().replace("\n", " ")
    parts.append(f"公式: {formula}（仅参考方向与思路，勿直接复名）")
    return " | ".join(parts)


def render_digest(
    entries: Sequence[DigestEntry],
    *,
    max_chars: int = DEFAULT_MAX_DIGEST_CHARS,
    include: list[DigestEntry] | None = None,
    sota: PoolSota | None = None,
) -> str:
    """渲染注入块。entries 须已按 score 降序（低分在尾部先被砍）。

    截断策略：从高分往低分累积，放不下下一条即停；重新编号保证无空洞；
    标题行永远保留（调用方据空串判定「无池可注入」）。``sota`` 给定时插在
    标题下方、参与同一字符预算（标杆线先于条目保留）。

    ``include`` 给定时按序收集**真正进入文本**的条目——疲劳计数只许给
    真注入的（被截掉的也计数会把疲劳加到没进 prompt 的因子上）。
    """
    if not entries:
        return ""
    lines = [DIGEST_TITLE, ""]
    if sota is not None:
        lines.append(sota.render())
        lines.append("")
    fitted = 0
    for entry in entries:
        # 编号只数**条目**（SOTA 行不占序号），保证无空洞
        candidate_line = _render_entry(fitted + 1, entry)
        # 预留尾部提示的行数（只有真截断时提示才重要）
        tail_reserve = len(DIGEST_HINT) + 2 if len(lines) > 2 else len(DIGEST_HINT) + 2
        if (
            sum(len(line) + 1 for line in lines)
            + len(candidate_line)
            + 1
            + tail_reserve
            > max_chars
        ):
            break
        lines.append(candidate_line)
        fitted += 1
        if include is not None:
            include.append(entry)
    if fitted == 0:
        # 连一条都放不下：保留标题（+标杆线，若有）+提示，让调用方看得见截空
        return "\n".join(lines + [DIGEST_HINT])
    lines.append("")
    lines.append(DIGEST_HINT)
    return "\n".join(lines)
