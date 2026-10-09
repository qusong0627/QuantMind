"""论文复现卡·真实模型回归脚本

对**真实解析产物**（MinerU 输出的 full.md，或内置金样语料）跑**真实 LLM**
的文档整理链（`doc_organize.organize_document`），验收两件事：

1. **结构完整性**（确定性金样由单测锁定，本脚本补真实模型侧）：
   摘要/方法/方向齐备、因子卡 name+formula+intuition 非空、因子名唯一、
   Markdown 章节齐备且含全部因子名；
2. **跨次稳定性**（`--runs N`）：多次独立调用都通过结构检查，
   并报告各次因子名的重合度（金样管口径，真实回归管稳定性）。

用法（容器内；真实 LLM Key 在容器环境里）：

    docker exec -w /app quantmind python scripts/alpha_agent/doc_paper_regression.py \
        /data/rd_agent_docs/<doc_id>/full.md
    # 无真实论文时用内置金样语料冒烟（仍走真实 LLM）：
    docker exec -w /app quantmind python scripts/alpha_agent/doc_paper_regression.py --fixture

退出码：0 = 全部通过；1 = 结构检查有失败项；2 = 用法/环境错误。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable
from pathlib import Path

_project_root = Path(__file__).resolve().parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from backend.services.engine.alpha_agent.doc_organize import (  # noqa: E402
    OrganizeError,
    organize_document,
)
from backend.services.engine.alpha_agent.llm_client import (  # noqa: E402
    resolve_llm_config,
)

GOLDEN_PATH = _project_root / "backend/tests/fixtures/docPaperGolden.json"

MIN_DIRECTION_CHARS = 30
MIN_SUMMARY_CHARS = 20
MIN_FORMULA_CHARS = 3
MAX_FACTORS = 10


def check_paper_payload(payload: dict, markdown: str) -> list[str]:
    """论文口径结构质量检查；返回问题列表（空 = 通过）。"""
    problems: list[str] = []

    if len(payload.get("summary", "")) < MIN_SUMMARY_CHARS:
        problems.append(f"摘要过短（< {MIN_SUMMARY_CHARS} 字）")
    if len(payload.get("direction", "")) < MIN_DIRECTION_CHARS:
        problems.append(f"挖掘方向过短（< {MIN_DIRECTION_CHARS} 字），不足以驱动挖掘")
    if (
        not payload.get("method", "").strip()
        and not payload.get("replication_notes", "").strip()
    ):
        problems.append("方法概述与复现注记同时为空——复现卡无法指导下游")

    factors = payload.get("factors") or []
    if not factors:
        problems.append("因子列表为空")
    if len(factors) > MAX_FACTORS:
        problems.append(
            f"因子数 {len(factors)} 超出 {MAX_FACTORS}——提炼失败，退化成摘抄"
        )
    names: list[str] = []
    for i, factor in enumerate(factors):
        name = factor.get("name", "").strip()
        formula = factor.get("formula", "").strip()
        if not name:
            problems.append(f"factors[{i}] 缺 name")
        if len(formula) < MIN_FORMULA_CHARS:
            problems.append(f"factors[{i}]（{name or '未命名'}）公式缺失或过短")
        if not factor.get("intuition", "").strip():
            problems.append(f"factors[{i}]（{name or '未命名'}）缺直觉解释")
        names.append(name)
    if len(names) != len(set(names)):
        problems.append(f"因子名重复：{names}")

    for needle, label in (
        ("## 摘要", "摘要"),
        ("## 挖掘方向（可直接用于 RD Agent）", "挖掘方向"),
    ):
        if needle not in markdown:
            problems.append(f"Markdown 缺「{label}」章节")
    for name in set(names):
        if name and name not in markdown:
            problems.append(f"Markdown 未含因子名 {name}")
    return problems


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


async def run_once(
    text: str,
    *,
    kind: str,
    extra: str | None,
    config,
    check: Callable[[dict, str], list[str]],
) -> tuple[dict, list[str]]:
    result = await organize_document(text=text, kind=kind, extra=extra, config=config)
    problems = check(result["payload"], result["markdown"])
    if result.get("truncated"):
        problems.append(
            "⚠️ 文本超长被截断采样"
            f"（chunks={result.get('chunks_used', 0)}）——真实论文请拆分或抬高上限"
        )
    return result, problems


async def main_async(args: argparse.Namespace) -> int:
    config = resolve_llm_config()
    if config is None:
        print(
            "未配置可用的 LLM Key（DEEPSEEK_API_KEY / AI_IDE_LLM_API_KEY / "
            "OPENAI_API_KEY 均为空或占位符）——在容器环境变量里配置后重跑。",
            file=sys.stderr,
        )
        return 2

    if args.fixture:
        golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
        text = golden["input_md"]
        source = f"内置金样语料（{GOLDEN_PATH.name}）"
    else:
        md_path = Path(args.md_path)
        if not md_path.is_file():
            print(f"解析产物不存在：{md_path}", file=sys.stderr)
            return 2
        text = md_path.read_text(encoding="utf-8")
        source = str(md_path)

    kind = args.kind
    check = check_paper_payload if kind == "paper" else None
    if check is None:
        print(f"暂不支持 kind={kind}（现支持 paper）", file=sys.stderr)
        return 2

    print(f"来源：{source}（{len(text)} 字符）· 口径：{kind} · 轮次：{args.runs}")
    failures = 0
    factor_sets: list[set[str]] = []
    for i in range(1, args.runs + 1):
        try:
            result, problems = await run_once(
                text, kind=kind, extra=args.extra, config=config, check=check
            )
        except OrganizeError as exc:
            print(f"[{i}/{args.runs}] ✗ 整理链失败：{exc}")
            failures += 1
            continue
        names = {f.get("name", "") for f in result["payload"].get("factors", [])}
        factor_sets.append(names)
        if problems:
            failures += 1
            print(f"[{i}/{args.runs}] ✗ {len(problems)} 项问题：")
            for p in problems:
                print(f"    - {p}")
        else:
            print(
                f"[{i}/{args.runs}] ✓ 因子 {len(names)} 个："
                f"{'、'.join(sorted(names))}（chunks={result['chunks_used']}）"
            )

    if len(factor_sets) > 1:
        pairs = [
            _jaccard(factor_sets[i], factor_sets[j])
            for i in range(len(factor_sets))
            for j in range(i + 1, len(factor_sets))
        ]
        print(
            f"跨次因子名重合度（Jaccard）：min={min(pairs):.2f} avg={sum(pairs) / len(pairs):.2f}"
        )

    if args.out:
        Path(args.out).write_text(
            json.dumps(
                {
                    "source": source,
                    "kind": kind,
                    "runs": args.runs,
                    "failures": failures,
                    "factor_sets": [sorted(s) for s in factor_sets],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"报告已写入 {args.out}")

    passed = args.runs - failures
    print(f"结论：{passed}/{args.runs} 轮通过结构检查")
    return 0 if failures == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="论文复现卡·真实模型回归")
    parser.add_argument("md_path", nargs="?", help="MinerU 解析产物 full.md 路径")
    parser.add_argument(
        "--fixture", action="store_true", help="用内置金样语料冒烟（仍走真实 LLM）"
    )
    parser.add_argument("--kind", default="paper", choices=["paper"], help="整理口径")
    parser.add_argument("--runs", type=int, default=1, help="重复轮次（稳定性）")
    parser.add_argument("--extra", default=None, help="附加整理要求（≤2000 字）")
    parser.add_argument("--out", default=None, help="报告 JSON 输出路径")
    args = parser.parse_args()
    if not args.fixture and not args.md_path:
        parser.error("需要给出 md_path 或 --fixture 之一")
    if args.runs < 1:
        parser.error("--runs 至少为 1")
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
