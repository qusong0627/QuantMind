"""论文复现回归脚本：结构质量检查判据。

真 LLM 输出不可进 CI，但 `check_paper_payload` 是纯函数——判据本身必须
两头准：完整输出不许误伤（否则回归噪音大没人用），退化输出（方向过短、
公式丢失、因子重复、退化成摘抄）必须抓出来。

运行（容器）：python3 -m pytest backend/tests/test_doc_paper_regression.py -q
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.alpha_agent.doc_paper_regression import (  # noqa: E402
    MAX_FACTORS,
    check_paper_payload,
)

GOOD_PAYLOAD = {
    "kind": "paper",
    "title": "动量复现",
    "summary": "论文检验 A 股 12-2 横截面动量，WML 月均 2.24%，FF3 调整后仍显著。",
    "direction": (
        "在 A 股全市场复现 12-2 动量：以月频复权价计算 t-12 至 t-2 累计收益，"
        "分十组构造等权多空组合持有 3 个月，报告 FF3 调整后 alpha。"
    ),
    "method": "按 12-2 动量分十组，买入赢家卖出输家。",
    "replication_notes": "因子做 1%/99% 缩尾；停牌日按 T+1 可成交口径过滤。",
    "factors": [
        {"name": "MOM_12_2", "formula": "prod(1+R) - 1", "intuition": "中期动量"},
        {"name": "WML", "formula": "mean(W) - mean(L)", "intuition": "多空溢价"},
    ],
}

GOOD_MD = (
    "# 动量复现\n\n## 摘要\n\n论文检验动量。\n\n## 复现因子\n\n### 1. MOM_12_2\n\n"
    "### 2. WML\n\n## 挖掘方向（可直接用于 RD Agent）\n\n复现 12-2 动量。\n"
)


def _payload(**overrides) -> dict:
    return {**GOOD_PAYLOAD, **overrides}


def test_complete_payload_passes_without_false_positive() -> None:
    assert check_paper_payload(GOOD_PAYLOAD, GOOD_MD) == []


def test_short_direction_is_flagged() -> None:
    problems = check_paper_payload(_payload(direction="复现动量。"), GOOD_MD)
    assert any("方向过短" in p for p in problems)


def test_missing_formula_is_flagged() -> None:
    payload = _payload(
        factors=[{"name": "M", "formula": "x", "intuition": "i"}],
    )
    problems = check_paper_payload(payload, GOOD_MD.replace("MOM_12_2", "M"))
    assert any("公式缺失" in p for p in problems)


def test_missing_intuition_is_flagged() -> None:
    payload = _payload(
        factors=[{"name": "MOM_12_2", "formula": "prod(1+R) - 1", "intuition": ""}],
    )
    problems = check_paper_payload(payload, GOOD_MD)
    assert any("缺直觉解释" in p for p in problems)


def test_duplicate_factor_names_are_flagged() -> None:
    payload = _payload(
        factors=[
            {"name": "MOM", "formula": "f1", "intuition": "a"},
            {"name": "MOM", "formula": "f2", "intuition": "b"},
        ],
    )
    problems = check_paper_payload(payload, GOOD_MD)
    assert any("因子名重复" in p for p in problems)


def test_factor_over_schema_cap_is_flagged() -> None:
    payload = _payload(
        factors=[
            {"name": f"F{i}", "formula": f"f{i}", "intuition": "i"}
            for i in range(MAX_FACTORS + 1)
        ],
    )
    problems = check_paper_payload(payload, GOOD_MD)
    assert any("摘抄" in p for p in problems)


def test_empty_factors_are_flagged() -> None:
    problems = check_paper_payload(_payload(factors=[]), GOOD_MD)
    assert any("因子列表为空" in p for p in problems)


def test_factor_missing_from_markdown_is_flagged() -> None:
    problems = check_paper_payload(GOOD_PAYLOAD, GOOD_MD.replace("WML", "OTHER"))
    assert any("未含因子名 WML" in p for p in problems)


def test_missing_section_is_flagged() -> None:
    problems = check_paper_payload(GOOD_PAYLOAD, "## 摘要\n\nMOM_12_2 WML\n")
    assert any("挖掘方向" in p for p in problems)


def test_method_and_notes_both_empty_flagged() -> None:
    problems = check_paper_payload(_payload(method="", replication_notes=""), GOOD_MD)
    assert any("复现卡" in p for p in problems)
