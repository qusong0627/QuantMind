"""因子身份/查重：名称与 LaTeX 公式归一化、代码指纹、查重判定与批内去重。

这是 RD-Agent 挖掘链路两层防重的「廉价层」：
- 挖掘落库前（``run_rd_agent.py``）：与 ``rd_agent_factors`` 存量 + 同批内比对；
- 物化器复用 ``feature_column_name`` / ``code_fingerprint``（列名与代码改写检测），
  不走本模块的存量索引。

值级（相关）防重在物化器里另做，不在此模块。
"""

from __future__ import annotations

import hashlib
import re

import pytest

from backend.shared.factor_identity import (
    DuplicateVerdict,
    code_fingerprint,
    feature_column_name,
    find_duplicate,
    normalize_factor_name,
    normalize_formula,
    partition_duplicates,
)


# ── normalize_factor_name ──────────────────────────────────────────────


@pytest.mark.unit
def test_normalize_name_case_and_separators_collapse():
    assert (
        normalize_factor_name("Momentum_5D")
        == normalize_factor_name("momentum 5d")
        == normalize_factor_name("  MOMENTUM-5D  ")
    )


@pytest.mark.unit
def test_normalize_name_keeps_unicode_alnum():
    assert normalize_factor_name("动量_20") == "动量20"


@pytest.mark.unit
def test_normalize_name_empty_forms():
    assert normalize_factor_name(None) == ""
    assert normalize_factor_name("   ") == ""
    assert normalize_factor_name("___") == ""


# ── normalize_formula ──────────────────────────────────────────────────


@pytest.mark.unit
def test_normalize_formula_latex_noise_removed():
    plain = normalize_formula(r"\frac{MA5 - MA20}{std20}")
    noisy = normalize_formula(r"\frac{\,MA5 \;-\; MA20\,}{std20}")
    assert plain == noisy


@pytest.mark.unit
def test_normalize_formula_left_right_marks_removed():
    # \left / \right 是纯排版自适应命令，去掉后与普通括号等价
    assert normalize_formula(r"\left(x\right)") == normalize_formula("(x)")
    # 但括号本身保留：带括号与不带括号是不同写法，不做过度归一（保守避免误判）


@pytest.mark.unit
def test_normalize_formula_dollar_wrapper_and_spaces():
    assert normalize_formula("$x + y$") == normalize_formula("x+y")


@pytest.mark.unit
def test_normalize_formula_distinct_formulas_differ():
    assert normalize_formula(r"\frac{a}{b}") != normalize_formula(r"\frac{b}{a}")
    # 空白折叠后仍不同（数值窗口不同）
    assert normalize_formula("MA5 - MA20") != normalize_formula("MA5 - MA60")


@pytest.mark.unit
def test_normalize_formula_empty_forms():
    assert normalize_formula(None) == ""
    assert normalize_formula("  ") == ""


# ── code_fingerprint ───────────────────────────────────────────────────


@pytest.mark.unit
def test_code_fingerprint_ignores_comments_blank_lines_indent():
    a = code_fingerprint("def f():\n    return 1  # 计算\n\n")
    b = code_fingerprint("def f():\n\treturn 1\n")
    assert a is not None and a == b


@pytest.mark.unit
def test_code_fingerprint_distinct_code_differs():
    assert code_fingerprint("x = 1") != code_fingerprint("x = 2")


@pytest.mark.unit
def test_code_fingerprint_empty_returns_none():
    assert code_fingerprint("") is None
    assert code_fingerprint("# 只有注释\n\n") is None
    assert code_fingerprint(None) is None


# ── feature_column_name ────────────────────────────────────────────────


@pytest.mark.unit
def test_feature_column_name_basic_slug():
    assert feature_column_name("VZ5") == "rd_vz5"
    assert feature_column_name("Momentum 5D") == "rd_momentum_5d"
    assert feature_column_name("  Vol-Ratio_20 ") == "rd_vol_ratio_20"


@pytest.mark.unit
def test_feature_column_name_non_ascii_gets_hash_suffix():
    a = feature_column_name("动量_20")
    b = feature_column_name("换手_20")
    assert a != b, "不同中文名 ASCII 化后会塌缩，必须靠 hash 后缀区分"
    assert a.startswith("rd_")
    # 训练直读要求映射列名是合法 SQL 标识符（read_range 对 alias 有 fullmatch 闸门）
    assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", a)


@pytest.mark.unit
def test_feature_column_name_empty_and_digit_start():
    assert feature_column_name(None) == "rd_f"
    assert feature_column_name("  ") == "rd_f"
    # 纯数字名：ASCII 前缀保证首字符是字母
    assert feature_column_name("123").startswith("rd_")
    assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", feature_column_name("123"))


@pytest.mark.unit
def test_feature_column_name_truncated_to_80():
    assert len(feature_column_name("x" * 300)) == 80


@pytest.mark.unit
def test_feature_column_name_long_non_ascii_hash_survives_truncation():
    """长名截断必须给 hash 腾位：hash 被 80 上限削掉 = 塌缩病复发。"""
    n1 = "x" * 100 + "动量"
    n2 = "x" * 100 + "换手"
    c1, c2 = feature_column_name(n1), feature_column_name(n2)
    assert len(c1) == 80 and len(c2) == 80
    assert c1 != c2, "同长 ASCII 前缀 + 不同中文尾，只靠 hash 区分"
    digest = hashlib.sha1(n1.encode("utf-8")).hexdigest()[:6]
    assert c1.endswith(digest), "6 位名 hash 必须完整保留在列名内"
    assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", c1)


# ── find_duplicate / partition_duplicates ──────────────────────────────

_EXISTING = [
    {
        "factor_id": "id_mom",
        "factor_name": "Momentum5d",
        "factor_formulation": r"Momentum_{5d} = \frac{MA5 - MA20}{std20}",
        "factor_code": "def f():\n    return 1  # 动量",
    },
    {
        "factor_id": "id_vol",
        "factor_name": "VolRatio",
        "factor_formulation": r"VolRatio = \frac{vol_{t}}{vol_{t-20}}",
        "factor_code": "def g():\n    return 2",
    },
]


@pytest.mark.unit
def test_find_duplicate_by_name_variant():
    v = find_duplicate("momentum_5d", "", "", _EXISTING)
    assert isinstance(v, DuplicateVerdict)
    assert v.reason == "name"
    assert v.matched_id == "id_mom"
    assert v.matched_name == "Momentum5d"


@pytest.mark.unit
def test_find_duplicate_by_formula_latex_variant():
    variant = r"Momentum_{5d} = \frac{\,MA5-MA20\,}{std20}"
    v = find_duplicate("TotallyNewName", variant, "", _EXISTING)
    assert v is not None and v.reason == "formula" and v.matched_id == "id_mom"


@pytest.mark.unit
def test_find_duplicate_by_code_fingerprint():
    v = find_duplicate("AnotherName", "x = 1", "def f():\n\treturn 1\n", _EXISTING)
    assert v is not None and v.reason == "code" and v.matched_id == "id_mom"


@pytest.mark.unit
def test_find_duplicate_miss_and_empty_fields():
    assert find_duplicate("BrandNew", r"e^{i\pi} + 1 = 0", "y = 42", _EXISTING) is None
    # 候选字段全空：不得与空值存量互相误判
    assert find_duplicate("", "", "", _EXISTING) is None
    assert find_duplicate("", "", "", [{"factor_id": "e", "factor_name": ""}]) is None


@pytest.mark.unit
def test_find_duplicate_empty_corpus():
    assert find_duplicate("A", "B", "C", []) is None


@pytest.mark.unit
def test_partition_duplicates_keeps_first_and_reports_rest():
    cands = [
        {
            "factor_id": "c1",
            "factor_name": "AlphaOne",
            "factor_formulation": "a1",
            "factor_code": "x = 1",
        },
        {
            "factor_id": "c2",
            "factor_name": "alpha one",
            "factor_formulation": "a1",
            "factor_code": "x = 1",
        },
        {
            "factor_id": "c3",
            "factor_name": "BetaTwo",
            "factor_formulation": "b2",
            "factor_code": "y = 2",
        },
        {
            "factor_id": "c4",
            "factor_name": "MomVariant",
            "factor_formulation": "x = 1".replace("x = 1", "Momentum5d deriv"),
            "factor_code": "z = 3",
        },
    ]
    kept, dupes = partition_duplicates(cands, _EXISTING)
    kept_ids = [k["factor_id"] for k in kept]
    assert kept_ids == ["c1", "c3", "c4"], "批内同指纹只留首个；与存量不同指纹的保留"
    assert len(dupes) == 1
    cand, verdict = dupes[0]
    assert cand["factor_id"] == "c2"
    assert verdict.reason == "name"
    assert verdict.matched_id == "c1"


@pytest.mark.unit
def test_partition_duplicates_against_existing():
    cands = [
        # 与存量 id_vol 同名 → 拒
        {
            "factor_id": "k1",
            "factor_name": "volratio",
            "factor_formulation": "",
            "factor_code": "",
        },
        # 与存量 id_mom 代码指纹同 → 拒
        {
            "factor_id": "k2",
            "factor_name": "Fresh",
            "factor_formulation": "",
            "factor_code": "def f():\n  return 1",
        },
        # 全新 → 留
        {
            "factor_id": "k3",
            "factor_name": "Novel",
            "factor_formulation": r"\alpha = \beta",
            "factor_code": "w = 9",
        },
    ]
    kept, dupes = partition_duplicates(cands, _EXISTING)
    assert [k["factor_id"] for k in kept] == ["k3"]
    assert {d[0]["factor_id"]: d[1].reason for d in dupes} == {
        "k1": "name",
        "k2": "code",
    }


@pytest.mark.unit
def test_partition_duplicates_preserves_order_and_empty():
    assert partition_duplicates([], _EXISTING) == ([], [])
    cands = [{"factor_id": f"n{i}", "factor_name": f"Name{i}"} for i in range(5)]
    kept, dupes = partition_duplicates(cands, [])
    assert [k["factor_id"] for k in kept] == ["n0", "n1", "n2", "n3", "n4"]
    assert dupes == []


@pytest.mark.unit
def test_dedupe_glue_excludes_own_task_rows_and_dedupes_batch():
    """run_rd_agent 胶水层：本任务自己的存量行不挡更新；批内同指纹去重。"""
    from scripts.alpha_agent.run_rd_agent import _dedupe_against_corpus

    task_id = "task-1"

    def fid(name: str) -> str:
        return hashlib.md5(f"{task_id}:{name}".encode()).hexdigest()

    corpus = [
        # 本任务自己的存量行（同名同公式同码）——不得把本批候选挡掉
        {
            "factor_id": fid("Mom5"),
            "factor_name": "Mom5",
            "factor_formulation": "f1",
            "factor_code": "c1",
        },
        # 他任务的因子：与本批候选同名 → 命中
        {
            "factor_id": "other-id",
            "factor_name": "OtherName",
            "factor_formulation": r"\beta",
            "factor_code": "x = 9",
        },
    ]
    factors = [
        {"name": "Mom5", "formulation": "f1-v2", "code": "c1-v2"},  # 自己的 → 保留更新
        {"name": "OtherName", "formulation": "z", "code": "y"},  # 撞他任务 → 跳过
        {"name": "Novel", "formulation": "n", "code": "nc"},  # 全新 → 保留
        {"name": "novel", "formulation": "n2", "code": "nc2"},  # 批内撞 Novel → 跳过
    ]
    skipped = _dedupe_against_corpus(corpus, factors, task_id)
    assert skipped == {fid("OtherName"), fid("novel")}
