#!/usr/bin/env python3
"""校验 A/H 跨市场平价 Markdown 报告的结构与必要声明。

来源：quantskills/skill-cross-listing-parity 的 scripts/validate_report.py（GPL-3.0-only）。
本地化差异（2026-10 移植）：
  - 源校验器要求「ADR 折溢价」章节；本技能数据层只覆盖 A/H，ADR 章节不作要求
    （本地无 ADR 配对表与 USD/HKD 汇率源，见 references/methodology.md「ADR 延伸」）；
  - 「异常/极值/收敛」章节放宽为 异常|极值|收敛|分位；
  - 操作性表达禁用词在源清单之外补充「建议买/建议卖」。

只检查结构和必要声明，不判断结论对错；数据计算仍需人工核对来源、汇率与配对表版本。

用法：
  python3 validate_report.py <report.md>
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REQUIRED_SECTIONS = [
    (
        "title",
        r"^#\s+.*(A\/H|A股|跨市场|溢价)",
        "标题需含 A/H 或 A股 或 跨市场 或 溢价",
    ),
    ("summary", r"^##\s*(?:\d+[.、]\s*)?摘要", "缺少摘要"),
    ("ah", r"^##\s*(?:\d+[.、]\s*)?A\/H.*(溢价|平价)", "缺少 A/H 溢价章节"),
    (
        "anomaly",
        r"^##\s*(?:\d+[.、]\s*)?(异常|极值|收敛|分位)",
        "缺少异常/极值/分位章节",
    ),
    ("data_notes", r"^##\s*(?:\d+[.、]\s*)?数据说明", "缺少数据说明章节"),
    ("disclaimer", r"^##\s*(?:\d+[.、]\s*)?免责声明", "缺少免责声明章节"),
]

OPERATIONAL_WORDS = (
    r"(建仓|减仓|加仓|止盈|止损|目标价\s*\d|建议买|建议卖|推荐买|推荐卖)"
)


def validate(text: str) -> list[str]:
    issues: list[str] = []

    if len(text.strip()) < 500:
        issues.append("报告内容过短，可能不是完整跨市场平价报告")

    for _key, pattern, message in REQUIRED_SECTIONS:
        if not re.search(pattern, text, flags=re.MULTILINE):
            issues.append(message)

    if not re.search(r"(数据来源|来源接口|使用接口|QuantDB|数据集)", text):
        issues.append("缺少数据来源或来源接口说明")

    if not re.search(r"(数据日|数据截止|生成时间|截止时间)", text):
        issues.append("缺少数据日或数据截止时间说明")

    if not re.search(r"(汇率来源|USD|HKD|CNY|港元|人民币|美元)", text):
        issues.append("缺少汇率来源、币种或汇率日期说明")

    if not re.search(r"(T\+1|snapshot|Snapshot|快照|财报滞后|一致预期)", text):
        issues.append("缺少 T+1、snapshot、财报滞后或一致预期说明")

    if not re.search(r"(ratio|股数比|存托比例|映射表版本|配对表版本)", text):
        issues.append("缺少股数比、存托比例或映射表版本说明")

    if not re.search(
        r"(不构成投资建议|不构成任何投资建议|不提供操作建议|仅作.*事实|仅用于研究)",
        text,
    ):
        issues.append("缺少非投资建议/事实归纳声明")

    if re.search(OPERATIONAL_WORDS, text):
        issues.append("报告包含操作性表达")

    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="Path to the Markdown report")
    args = parser.parse_args()

    try:
        text = args.report.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        print(f"ERROR: report not found: {args.report}", file=sys.stderr)
        return 2

    issues = validate(text)
    if issues:
        print("FAIL")
        for issue in issues:
            print(f"- {issue}")
        return 1

    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
