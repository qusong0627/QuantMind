#!/usr/bin/env python3
"""空/桩策略代码回填（T-P3-03）：按名称匹配 strategy_templates 补齐可执行代码。

用法（容器内）:
    python backend/scripts/repair_strategy_code_formats.py            # DRY-RUN（默认）
    python backend/scripts/repair_strategy_code_formats.py --apply    # 实际写库

规则：
- 仅处理分类为 empty 的行（空 code / "# New Strategy" 桩壳）；
- 按 **name 精确匹配** strategy_templates/*.json 的模板名 → 回填同名 .py 代码 + sha256；
- 无模板匹配的行**不猜测**：列入 unmatched 清单交人工；
- 幂等：回填后 code 非空，二次运行为 0；
- ``--only-ids 1,2,3`` 限定处理范围（测试/定点修复用）。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.shared.strategy_format import FORMAT_EMPTY, classify_strategy_code  # noqa: E402


def _load_template_index() -> dict[str, tuple[str, str]]:
    """模板名 → (code, code_hash)。目录 = 项目根 strategy_templates。"""
    tmpl_dir = PROJECT_ROOT / "strategy_templates"
    index: dict[str, tuple[str, str]] = {}
    for jf in sorted(tmpl_dir.glob("*.json")):
        py = jf.with_suffix(".py")
        if not py.exists():
            continue
        try:
            name = str(
                json.loads(jf.read_text(encoding="utf-8")).get("name") or ""
            ).strip()
        except Exception:  # noqa: BLE001
            continue
        if not name:
            continue
        code = py.read_text(encoding="utf-8")
        index[name] = (code, hashlib.sha256(code.encode("utf-8")).hexdigest())
    return index


async def repair_formats(
    apply: bool = False, only_ids: list[int] | None = None
) -> dict:
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    templates = _load_template_index()
    to_fill: list[dict] = []
    unmatched: list[dict] = []

    async with get_session(read_only=not apply) as session:
        result = await session.execute(
            text("SELECT id, name, code FROM strategies ORDER BY id")
        )
        for row in result.mappings():
            sid = int(row["id"])
            if only_ids is not None and sid not in only_ids:
                continue
            code = str(row["code"] or "")
            if classify_strategy_code(code) != FORMAT_EMPTY:
                continue
            name = str(row["name"] or "").strip()
            hit = templates.get(name)
            if hit is None:
                unmatched.append({"id": sid, "name": name, "code_head": code[:40]})
                continue
            to_fill.append({"id": sid, "name": name})
            if apply:
                await session.execute(
                    text(
                        "UPDATE strategies SET code = :code, code_hash = :hash, "
                        "updated_at = now() WHERE id = :sid"
                    ),
                    {"code": hit[0], "hash": hit[1], "sid": sid},
                )
        if apply:
            await session.commit()

    return {
        "dry_run": not apply,
        "filled": len(to_fill) if apply else 0,
        "to_fill": to_fill,
        "unmatched": unmatched,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="空/桩策略代码回填（T-P3-03）")
    parser.add_argument("--apply", action="store_true", help="实际写库（默认 DRY-RUN）")
    parser.add_argument(
        "--only-ids", default="", help="限定策略 id（逗号分隔，测试/定点修复用）"
    )
    args = parser.parse_args()
    only_ids = (
        [int(x) for x in args.only_ids.split(",") if x.strip().isdigit()]
        if args.only_ids.strip()
        else None
    )

    report = asyncio.run(repair_formats(apply=args.apply, only_ids=only_ids))
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] 可回填 {len(report['to_fill'])} 行（filled={report['filled']}）")
    for r in report["to_fill"]:
        print(f"  id={r['id']} ← 模板「{r['name']}」")
    if report["unmatched"]:
        print(f"无模板匹配（人工处置）{len(report['unmatched'])} 行:")
        for r in report["unmatched"]:
            print(f"  id={r['id']} {r['name'][:28]} | code={r['code_head']!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
