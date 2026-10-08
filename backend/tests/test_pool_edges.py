"""公式级相似边（pool_edges）——token Jaccard 纯函数。

v1 谱系边里最廉价的一层：公式 token 集合的 Jaccard 相似度。
「换个写法的老因子」最终由值级相关层（pool_panels 的 |ρ|）兜底，
本层只负责把**肉眼可辨的同构公式**连边（top-k 限幅）。
"""

from __future__ import annotations

from backend.services.engine.mining_plugins.pool_edges import (
    formula_tokens,
    jaccard,
    similar_pairs,
)


class TestFormulaTokens:
    def test_normalizes_case_space_and_dollar(self):
        assert formula_tokens("$Close / Mean(Close, 20)$") == formula_tokens(
            "close/mean(close,20)"
        )

    def test_tokens_are_identifiers_and_numbers(self):
        toks = formula_tokens(r"\frac{close-mean(close,20)}{std(close,20)}")
        assert {"frac", "close", "mean", "std", "20"} <= set(toks)

    def test_empty_formula_empty_set(self):
        assert formula_tokens("") == frozenset()
        assert formula_tokens(None) == frozenset()


class TestJaccard:
    def test_identical_is_one(self):
        a = formula_tokens("close/mean(close,20)")
        assert jaccard(a, a) == 1.0

    def test_disjoint_is_zero(self):
        assert (
            jaccard(formula_tokens("close/mean(close,20)"), formula_tokens("vwap"))
            == 0.0
        )

    def test_empty_side_is_zero(self):
        assert jaccard(frozenset(), formula_tokens("close")) == 0.0
        assert jaccard(formula_tokens("close"), frozenset()) == 0.0

    def test_partial_overlap(self):
        # {a,b,c} vs {b,c,d} → 2/4
        assert jaccard(frozenset("abc"), frozenset("bcd")) == 0.5


class TestSimilarPairs:
    def _items(self):
        return [
            ("f1", formula_tokens("close/mean(close,20)")),
            ("f2", formula_tokens("close/mean(close,20)")),  # 与 f1 完全相同
            ("f3", formula_tokens("close/mean(close,60)")),  # 与 f1 高重叠
            ("f4", formula_tokens("volume/std(volume,10)")),  # 无关
        ]

    def test_pairs_above_threshold_with_weight(self):
        pairs = similar_pairs(self._items(), threshold=0.5, top_k=10)
        got = {(a, b): w for a, b, w in pairs}
        assert got[("f1", "f2")] == 1.0
        assert ("f1", "f4") not in got

    def test_canonical_order_and_symmetry_dedup(self):
        pairs = similar_pairs(self._items(), threshold=0.5, top_k=10)
        for a, b, _w in pairs:
            assert a < b  # 每条无向边只出一行，字典序定向

    def test_topk_limits_per_node_deterministic(self):
        pairs = similar_pairs(self._items(), threshold=0.4, top_k=1)
        # 每节点配额 1 后取无向并集：f1 保 (f1,f2)（1.0 最高）；f3 的候选
        # f1/f2 并列 0.5，按对方 id 升序保 f1 → (f1,f3)。f4 无相似邻居。
        assert pairs == [("f1", "f2", 1.0), ("f1", "f3", 0.5)]

    def test_threshold_filters_everything(self):
        assert similar_pairs(self._items(), threshold=1.01, top_k=3) == []
