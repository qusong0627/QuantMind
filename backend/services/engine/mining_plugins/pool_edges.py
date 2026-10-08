"""公式级相似边：token Jaccard 纯函数（无 DB、无 IO）。

谱系边三层里的最廉价层（``similar_to`` / method ``formula``）：把**肉眼
可辨的同构公式**连边（``sum(close,20)`` vs ``mean(close,20)`` 这类）；
「换个写法的老因子」由值级相关层（pool_panels 的 |ρ|≥0.8，method
``value``）兜底，语义级 embedding 通道留作扩展点。

token 化在 ``factor_identity.normalize_formula``（单一出处：剥 $、去排版
噪声、去空白、统一小写）之上做，只在归一文本上抽标识符与数字——
两处归一逻辑各自演化会让「眼看的同一个公式」判成不同。

确定性：无向边按因子 id 字典序定向（a < b），每条边只出一行；
每节点 top-k 邻居同分按对方 id 升序。
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from backend.shared.factor_identity import normalize_formula

_TOKEN_RE = re.compile(r"[a-zA-Z_][a-zA-Z_0-9]*|\d+(?:\.\d+)?")

#: 判「同构公式」的默认阈值与每节点邻居上限（top-k 限幅防稠密图）
DEFAULT_THRESHOLD = 0.6
DEFAULT_TOP_K = 3


def formula_tokens(formula: str | None) -> frozenset[str]:
    """公式 → 归一 token 集（标识符 + 数字）；空公式 → 空集。"""
    norm = normalize_formula(formula)
    if not norm:
        return frozenset()
    return frozenset(_TOKEN_RE.findall(norm))


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """Jaccard 相似度；任一侧为空 → 0.0（空对空不算相似）。"""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def similar_pairs(
    items: Sequence[tuple[str, frozenset[str]]],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    top_k: int = DEFAULT_TOP_K,
) -> list[tuple[str, str, float]]:
    """公式相似边列表：``[(src, dst, weight)]``，src < dst 字典序、去重。

    每节点只保留相似度最高的 ``top_k`` 个邻居（同分按对方 id 升序），
    再取无向并集——稠密相似池不会把图变全连接。
    """
    if not items or top_k <= 0:
        return []
    neighbors: dict[str, list[tuple[float, str]]] = {fid: [] for fid, _ in items}
    for i, (a_id, a_toks) in enumerate(items):
        for b_id, b_toks in items[i + 1 :]:
            score = jaccard(a_toks, b_toks)
            if score < threshold or score <= 0.0:
                continue
            neighbors[a_id].append((score, b_id))
            neighbors[b_id].append((score, a_id))
    edges: set[tuple[str, str, float]] = set()
    for fid, cands in neighbors.items():
        cands.sort(key=lambda t: (-t[0], t[1]))
        for score, other in cands[:top_k]:
            src, dst = (fid, other) if fid < other else (other, fid)
            edges.add((src, dst, score))
    return sorted(edges)
