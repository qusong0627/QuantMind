"""机构级融合权重引擎（纯函数，无 IO/无 DB）。

设计见 docs/机构级模型融合_设计方案.md §3。输入：
- member_ics: 成员 -> 日度已实现 rank_ic 序列（时间升序；防前视截断由适配层
  fusion_quality.py 负责，本模块只管统计口径）。
- corr: 成员间截面分相关系数矩阵（按日 Spearman 均值），缺省视为无信息（pen=1）。

输出 FusionWeights：权重（恒非负、和=1）+ 逐成员诊断（IC 统计/惩罚/收缩/剔除根因），
供 API 预览、前端展示与 JSON 落盘（weight_snapshot.json）。

策略：
- icir_shrunk（默认）：w_pre_i = λ_i * raw_i + (1-λ_i) * mean(raw)，λ_i = n_i/(n_i+K)。
  样本少的成员自动收缩回成员平均（1/N 先验的等价形式），ICIR 非正/天数不足者
  raw=0 不参与倾斜；raw = max(ICIR,0) * pen，pen = 1/(1+mean_{j≠i} max(C_ij,0))。
- equal：等权（对照与兜底）。
- manual：用户给定的正权重直接归一化（专家覆盖，跳过收缩/上限/剔除）。
- recent_ic：旧策略，映射到 icir_shrunk（其每日刷新任务已随 6c469eb7 删除）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from collections.abc import Mapping, Sequence

__all__ = [
    "FusionWeightConfig",
    "FusionWeights",
    "MemberWeightDiagnostic",
    "compute_fusion_weights",
    "STRATEGY_ICIR_SHRUNK",
    "STRATEGY_EQUAL",
    "STRATEGY_MANUAL",
]

STRATEGY_ICIR_SHRUNK = "icir_shrunk"
STRATEGY_EQUAL = "equal"
STRATEGY_MANUAL = "manual"

_STRATEGY_ALIASES = {"recent_ic": STRATEGY_ICIR_SHRUNK}
_KNOWN_STRATEGIES = (STRATEGY_ICIR_SHRUNK, STRATEGY_EQUAL, STRATEGY_MANUAL)

_EPS = 1e-12
_STD_FLOOR = 1e-9  # 低于此视为无波动，ICIR 记 0（常数序列无法评估一致性）
_WATER_FILL_ROUNDS = 10


@dataclass(frozen=True)
class FusionWeightConfig:
    """权重引擎参数（默认值 = 机构级口径，见设计方案 §3）。"""

    strategy: str = STRATEGY_ICIR_SHRUNK
    min_days: int = 20            # 少于该有效天数的成员不参与 ICIR 倾斜
    shrink_k: float = 60.0        # 收缩强度：λ = n/(n+K)
    max_weight: float = 0.40      # 单成员上限（水位填充；不可行时按 1/存活数放宽）
    drop_threshold: float = 0.02  # 收缩预权低于此值 → 剔除并重归一
    dedup_corr: float = 0.98      # 两成员相关超过此值：ICIR 低者失去倾斜
    manual_weights: Mapping[str, float] | None = None


@dataclass(frozen=True)
class MemberWeightDiagnostic:
    """逐成员审计信息（weight_snapshot.json 的 diagnostics 段）。"""

    member_id: str
    n_days: int
    ic_mean: float | None
    ic_std: float | None
    icir: float | None
    corr_penalty: float
    raw: float          # 收缩前的倾斜分 = max(ICIR,0)*pen（0 = 不倾斜）
    weight: float       # 最终权重
    dropped: bool       # 是否因权重过小被剔除
    reason: str         # "" | insufficient_days | nonpositive_icir | duplicate


@dataclass(frozen=True)
class FusionWeights:
    weights: dict[str, float]
    diagnostics: tuple[MemberWeightDiagnostic, ...]
    strategy: str
    warning: str = ""


def _clean_series(values: Sequence[float | None]) -> list[float]:
    out: list[float] = []
    for v in values:
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isnan(f) or math.isinf(f):
            continue
        out.append(f)
    return out


def _stats(values: Sequence[float | None]) -> tuple[int, float | None, float | None, float | None]:
    """返回 (n, mean, std, icir)；n==0 时统计量为 None。std 用样本标准差（ddof=1）。"""
    clean = _clean_series(values)
    n = len(clean)
    if n == 0:
        return 0, None, None, None
    mean = sum(clean) / n
    if n > 1:
        var = sum((x - mean) ** 2 for x in clean) / (n - 1)
        std = math.sqrt(var)
    else:
        std = 0.0
    icir = mean / std if std > _STD_FLOOR else 0.0
    return n, mean, std, icir


def _corr_value(corr: Mapping[str, Mapping[str, float]] | None, a: str, b: str) -> float | None:
    if not corr:
        return None
    row = corr.get(a) or {}
    v = row.get(b)
    if v is None:
        v = (corr.get(b) or {}).get(a)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _corr_penalty(
    member: str, ids: Sequence[str], corr: Mapping[str, Mapping[str, float]] | None
) -> float:
    others = [j for j in ids if j != member]
    if not others or not corr:
        return 1.0
    vals = []
    for j in others:
        c = _corr_value(corr, member, j)
        vals.append(max(c, 0.0) if c is not None else 0.0)
    mean_pos = sum(vals) / len(vals)
    return 1.0 / (1.0 + mean_pos)


def _dedup(
    raw: dict[str, float],
    reasons: dict[str, str],
    stats: dict[str, tuple],
    ids: Sequence[str],
    corr: Mapping[str, Mapping[str, float]] | None,
    dedup_corr: float,
) -> None:
    """近似重复剔除（in-place）：C_ij > 阈值时 ICIR 低者失去倾斜项。

    胜者判据 (ICIR, n) 降序；全平局取字典序小者（确定性）。输家 raw 清零，
    根因标记 duplicate（不覆盖更强的 insufficient_days/nonpositive_icir）；
    它仍保留收缩先验份额 —— 相关性一旦破裂仍是分散器。
    """
    if not corr:
        return
    for i_a in range(len(ids)):
        for i_b in range(i_a + 1, len(ids)):
            a, b = ids[i_a], ids[i_b]
            c = _corr_value(corr, a, b)
            if c is None or c <= dedup_corr:
                continue
            rank_a = (stats[a][3] or float("-inf"), stats[a][0])
            rank_b = (stats[b][3] or float("-inf"), stats[b][0])
            loser = b if rank_a >= rank_b else a
            if raw.get(loser, 0.0) > 0.0:
                raw[loser] = 0.0
            if reasons.get(loser, "") == "":
                reasons[loser] = "duplicate"


def _water_fill(weights: dict[str, float], cap: float) -> dict[str, float]:
    """单成员上限（水位填充）：超出 cap 的部分按未封顶成员当前权重再分配。

    已剔除（0 权重）成员不参与再分配（不得被「救活」）。轮次上限防御性收尾，
    尾部分布对收敛精度不敏感（权重快照有最小变更阈值防抖）。
    """
    w = dict(weights)
    for _ in range(_WATER_FILL_ROUNDS):
        over = {i: v for i, v in w.items() if v > cap + _EPS}
        if not over:
            break
        excess = sum(v - cap for v in over.values())
        for i in over:
            w[i] = cap
        free = [i for i, v in w.items() if _EPS < v < cap - _EPS]
        base = sum(w[i] for i in free)
        if not free or base <= 0:
            break
        for i in free:
            w[i] += excess * (w[i] / base)
    total = sum(w.values())
    if total <= 0:
        n = len(w)
        return dict.fromkeys(w, 1.0 / n)
    return {i: v / total for i, v in w.items()}


def compute_fusion_weights(
    member_ics: Mapping[str, Sequence[float | None]],
    corr: Mapping[str, Mapping[str, float]] | None = None,
    config: FusionWeightConfig | None = None,
) -> FusionWeights:
    """计算融合权重。成员数 < 2 或策略非法 / manual 缺成员时抛 ValueError。"""
    cfg = config or FusionWeightConfig()
    strategy = _STRATEGY_ALIASES.get(cfg.strategy, cfg.strategy)
    if strategy not in _KNOWN_STRATEGIES:
        raise ValueError(f"unknown fusion weight strategy: {cfg.strategy!r}")

    ids = sorted(member_ics.keys())
    m = len(ids)
    if m < 2:
        raise ValueError("fusion requires at least 2 members")

    stats = {i: _stats(member_ics[i]) for i in ids}

    if strategy == STRATEGY_EQUAL:
        weights = dict.fromkeys(ids, 1.0 / m)
        return FusionWeights(
            weights=weights,
            diagnostics=_build_diagnostics(ids, stats, weights, {}, {}, {}),
            strategy=strategy,
        )

    if strategy == STRATEGY_MANUAL:
        provided = cfg.manual_weights or {}
        missing = [i for i in ids if i not in provided]
        if missing:
            raise ValueError(f"manual_weights missing members: {missing}")
        values: dict[str, float] = {}
        for i in ids:
            v = float(provided[i])
            if not math.isfinite(v) or v < 0:
                raise ValueError(f"manual weight for {i!r} must be finite and >= 0")
            values[i] = v
        total = sum(values.values())
        if total <= 0:
            raise ValueError("manual_weights sum must be positive")
        weights = {i: values[i] / total for i in ids}
        raw = dict(values)
        return FusionWeights(
            weights=weights,
            diagnostics=_build_diagnostics(ids, stats, weights, raw, {}, {}),
            strategy=strategy,
        )

    # ── icir_shrunk ──
    raw: dict[str, float] = {}
    reasons: dict[str, str] = {}
    penalties: dict[str, float] = {}
    for i in ids:
        n, _mean, _std, icir = stats[i]
        penalties[i] = _corr_penalty(i, ids, corr)
        if n < cfg.min_days:
            raw[i] = 0.0
            reasons[i] = "insufficient_days"
        elif icir is None or icir <= 0:
            raw[i] = 0.0
            reasons[i] = "nonpositive_icir"
        else:
            raw[i] = icir * penalties[i]
            reasons[i] = ""

    _dedup(raw, reasons, stats, ids, corr, cfg.dedup_corr)

    total_raw = sum(raw.values())
    if total_raw <= _EPS:
        weights = dict.fromkeys(ids, 1.0 / m)
        # 警告须指向真实根因：全员样本不足（新融合模型的常见态）与全员 ICIR 非正
        # 对用户是完全不同的信号，不可混为一谈。
        if all(reasons.get(i, "") == "insufficient_days" for i in ids):
            warning = "all_insufficient_days_fallback_equal"
        else:
            warning = "all_icir_nonpositive_fallback_equal"
        return FusionWeights(
            weights=weights,
            diagnostics=_build_diagnostics(ids, stats, weights, raw, penalties, reasons),
            strategy=strategy,
            warning=warning,
        )

    raw_mean = total_raw / m
    lam = {i: stats[i][0] / (stats[i][0] + cfg.shrink_k) for i in ids}
    pre = {i: lam[i] * raw[i] + (1.0 - lam[i]) * raw_mean for i in ids}
    pre_total = sum(pre.values())
    pre = {i: v / pre_total for i, v in pre.items()}

    # 阈值清理必须在封顶之前：否则上限再分配会把微弱成员抬高，永远剔不掉。
    dropped = {i: v < cfg.drop_threshold - _EPS for i, v in pre.items()}
    if any(dropped.values()):
        keep_total = sum(v for i, v in pre.items() if not dropped[i])
        if keep_total > _EPS:
            weights = {i: (0.0 if dropped[i] else pre[i] / keep_total) for i in ids}
        else:  # 全部低于阈值（理论不可达）→ 等权兜底
            weights = dict.fromkeys(ids, 1.0 / m)
            dropped = dict.fromkeys(ids, False)
    else:
        weights = pre

    # 有效上限 = max(配置值, 1/存活成员数)：两个成员时 0.40 数学不可行。
    survivors = [i for i in ids if weights[i] > _EPS]
    if survivors:
        effective_cap = max(cfg.max_weight, 1.0 / len(survivors))
        weights = _water_fill(weights, effective_cap)

    return FusionWeights(
        weights=weights,
        diagnostics=_build_diagnostics(ids, stats, weights, raw, penalties, reasons, dropped),
        strategy=strategy,
    )


def _build_diagnostics(
    ids: Sequence[str],
    stats: dict[str, tuple],
    weights: dict[str, float],
    raw: Mapping[str, float],
    penalties: Mapping[str, float],
    reasons: Mapping[str, str],
    dropped: Mapping[str, bool] | None = None,
) -> tuple[MemberWeightDiagnostic, ...]:
    drop_map = dropped or {}
    return tuple(
        MemberWeightDiagnostic(
            member_id=i,
            n_days=stats[i][0],
            ic_mean=stats[i][1],
            ic_std=stats[i][2],
            icir=stats[i][3] if stats[i][0] > 0 else None,
            corr_penalty=penalties.get(i, 1.0),
            raw=raw.get(i, 0.0),
            weight=weights[i],
            dropped=drop_map.get(i, False),
            reason=reasons.get(i, ""),
        )
        for i in ids
    )
