#!/usr/bin/env python3
"""§7 退役保留策略巡检 CLI：archive 保留 N=3 + 30 天冷却 → 清退超限归档模型的**产物目录**。

口径与安全约束见 ``backend/shared/model_retirement.py`` 模块 docstring。要点：

- 默认 **dry-run**（只打印计划）；``--apply`` 才真清退（删产物 + 写墓碑 + user_audit_logs）；
- DB 行永不删——清退只删 ``storage_path`` 指向的产物目录（越出用户模型根目录 / 符号链接
  / 被活跃 rollout 引用 / 缺归档时间戳 → 一律跳过，原因随计划打印）；
- 不内置调度：何时跑由运维/用户定（与市场同步同一纪律）。

用法::

    # 预演（全部租户）
    python backend/scripts/model_retirement_sweep.py

    # 执行
    python backend/scripts/model_retirement_sweep.py --apply

    # 限租户/用户（集成测试与小范围操作）
    python backend/scripts/model_retirement_sweep.py --tenant t1 --user 10000001 --apply

    # 机器可读输出
    python backend/scripts/model_retirement_sweep.py --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.shared.model_retirement import (  # noqa: E402
    COOLDOWN_DAYS,
    KEEP_N,
    sweep_retirement,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="§7 退役清退巡检（archive 保留 N 版 + 冷却期；默认预演）",
    )
    parser.add_argument("--apply", action="store_true", help="执行清退（默认只预演）")
    parser.add_argument("--keep-n", type=int, default=KEEP_N, help=f"保留版本数（默认 {KEEP_N}）")
    parser.add_argument(
        "--cooldown-days", type=int, default=COOLDOWN_DAYS,
        help=f"清退冷却天数（默认 {COOLDOWN_DAYS}）",
    )
    parser.add_argument("--tenant", default=None, help="仅处理该租户")
    parser.add_argument("--user", default=None, help="仅处理该用户（须与 --tenant 同用时最精确）")
    parser.add_argument("--models-root", default=None, help="用户模型根目录（默认取 USER_MODELS_ROOT）")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON（默认人类可读摘要）")
    return parser


def _print_human(summary: dict) -> None:
    mode = "执行（--apply）" if not summary["dry_run"] else "预演（dry-run）"
    print(f"§7 退役清退巡检 · {mode}")
    print(
        f"  策略 keep_n={summary['keep_n']} cooldown_days={summary['cooldown_days']} · "
        f"模型根 {summary['models_root']}"
    )
    print(f"  保留 {len(summary['kept'])} · 清退 {len(summary['purge'])} · 跳过 {len(summary['skipped'])}")
    if summary["purge"]:
        print("— 清退清单（最老先清）—")
        for item in summary["purge"]:
            mark = ""
            if item.get("purged"):
                mark = f" [已清 {item.get('files_removed', 0)} 文件 / {item.get('bytes_freed', 0)} B]"
            print(
                f"  {item['model_id']}  market={item['market']}  "
                f"归档 {item['age_days']} 天  路径 {item.get('storage_path') or '(按根目录推定)'}{mark}"
            )
    reasons: dict[str, int] = {}
    for item in summary["skipped"]:
        reasons[item["reason"]] = reasons.get(item["reason"], 0) + 1
    if reasons:
        print("— 跳过原因 —")
        for reason, count in sorted(reasons.items()):
            print(f"  {reason}: {count}")
        for item in summary["skipped"]:
            if item["reason"] == "cooldown":
                print(f"    · {item['model_id']} 可清退于 {item['eligible_at']}")
    for err in summary["errors"]:
        print(f"  !! {err['model_id']}: {err['reason']}", file=sys.stderr)
    if summary["dry_run"] and summary["purge"]:
        print("提示：以上为预演，未动任何文件；确认后加 --apply 执行。")


async def _run(args: argparse.Namespace) -> int:
    summary = await sweep_retirement(
        dry_run=not args.apply,
        keep_n=args.keep_n,
        cooldown_days=args.cooldown_days,
        tenant_id=args.tenant,
        user_id=args.user,
        models_root=args.models_root,
    )
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    else:
        _print_human(summary)
    return 1 if summary["errors"] else 0


def main() -> int:
    args = _build_parser().parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
