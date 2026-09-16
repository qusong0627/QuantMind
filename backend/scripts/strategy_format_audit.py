#!/usr/bin/env python3
"""策略代码格式审计（T-P3-03）：全库五形态清点 + 下架清单。

用法（容器内）:
    python backend/scripts/strategy_format_audit.py            # 人类可读
    python backend/scripts/strategy_format_audit.py --json     # 机器可读

下架清单口径（详见 shared/strategy_format.py 头注）：
- handle_data 聚宽风（无执行器）——存量应恒为 0；出现即列清单；
- empty 残壳（空 code / "# New Strategy" 桩）——交 repair_strategy_code_formats 处理；
- script（沙箱/AI-IDE 运行时形态）——不进策略库，出现即列清单提示。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.shared.strategy_format import (  # noqa: E402
    FORMAT_EMPTY,
    FORMAT_HANDLE_DATA,
    FORMAT_MINIBT,
    FORMAT_SCRIPT,
    FORMAT_STRATEGY_CONFIG,
    classify_strategy_code,
    is_executable_format,
)

_FORMAT_LABELS = {
    FORMAT_STRATEGY_CONFIG: "STRATEGY_CONFIG 声明式（执行器：回测中心/托管）",
    FORMAT_MINIBT: "minibt DSL（执行器：AI-IDE py3.12 运行时）",
    FORMAT_HANDLE_DATA: "handle_data 聚宽风（无执行器——下架）",
    FORMAT_SCRIPT: "脚本/钩子形态（沙箱/AI-IDE 运行时，不进策略库）",
    FORMAT_EMPTY: "空/桩残壳（需回填或清理）",
}


async def audit() -> dict:
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    rows: list[dict] = []
    async with get_session(read_only=True) as session:
        result = await session.execute(
            text("SELECT id, name, status, code FROM strategies ORDER BY id")
        )
        for row in result.mappings():
            fmt = classify_strategy_code(row["code"])
            rows.append(
                {
                    "id": int(row["id"]),
                    "name": str(row["name"] or ""),
                    "status": str(row["status"] or ""),
                    "format": fmt,
                    "executable": is_executable_format(fmt),
                }
            )

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["format"]] = counts.get(r["format"], 0) + 1
    retire_list = [r for r in rows if not r["executable"]]
    return {
        "total": len(rows),
        "counts": counts,
        "executable_total": sum(1 for r in rows if r["executable"]),
        "retire_list": retire_list,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="策略代码格式审计（T-P3-03）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    report = asyncio.run(audit())
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print(f"策略总数: {report['total']}（可执行 {report['executable_total']}）")
    for fmt, count in sorted(report["counts"].items()):
        print(f"  {_FORMAT_LABELS.get(fmt, fmt)}: {count}")
    if report["retire_list"]:
        print("\n下架/修复清单:")
        for r in report["retire_list"]:
            print(f"  id={r['id']} [{r['format']}] {r['name'][:28]}（{r['status']}）")
        print(
            "→ 空壳回填: python backend/scripts/repair_strategy_code_formats.py --apply"
        )
    else:
        print("\n全部为可执行格式 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
