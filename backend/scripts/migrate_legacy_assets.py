#!/usr/bin/env python3
"""P3-② 隔壁（quant-Trader）资产落盘 CLI —— 逐字节搬运 + 清单 + 复验。

**宿主侧运行**（需要读隔壁目录；容器里没有这个目录）。落地点默认
``data/legacy/quanttrader/``（``./data`` 是指向 ``/media/zbox/data/quantmind`` 的符号链接，
**在工作树之外**——这正是要点：迁移产物绝不进仓，公开仓更不会被密钥污染）。

用法::

    # 1) 先看计划（默认就是 --plan）：搬什么 / 跳什么 / 要人看什么，一行不写
    python backend/scripts/migrate_legacy_assets.py
    # 2) 落盘（写文件 + MANIFEST.jsonl + MANIFEST.sha256 + REPORT.md），随即自复验
    python backend/scripts/migrate_legacy_assets.py --apply
    # 3) 任何时候复验（切换日搬完后、归档前、日后怀疑丢了文件时）
    python backend/scripts/migrate_legacy_assets.py --verify

退出码：``0`` 全清 / ``1`` **要人看一眼**（有跳过、有告警、复验有差异）/
``2`` 环境或参数错误（源/落地点不对、落地点在 git 工作树里、清单缺失或坏）。

纪律（各自的失效形态都吃过亏）
------------------------------
1. **只搬白名单**：``.env`` / ``config/``（券商私钥）这类从不进入任何 include 规则。
2. **落地点不许在工作树里**：本仓 ``data/`` 是符号链接，仓里还有 11 个 ``data/*.sql``
   是被跟踪的——把 60MB 产物写进工作树，下一次 ``git add -A`` 就会连带删改它们。
3. **凭据两道闸**（路径名 + 落地字节内容）：硬形状不落盘、软形状告警照搬。
4. **可复验**：清单逐文件双哈希，复验能查出缺失 / 被改 / **多出**（多出和缺失一样可疑）。
5. **只读源**：不删不改源目录；落地区只增不删（多出的由复验报，不静默清理）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.shared.legacy_assets import (  # noqa: E402
    ARCHIVE_DIR,
    MANIFEST_NAME,
    apply_plan,
    plan_legacy_assets,
    verify,
)
from backend.shared.migration_paths import (  # noqa: E402
    artifact_refusal,
    inside_git_worktree,
)

EXIT_OK = 0
EXIT_ATTENTION = 1
EXIT_USAGE = 2

#: 隔壁仓库默认位置（symlink ``/home/zbox/baymax`` 指向同一处）
DEFAULT_SOURCE_ROOT = "/home/zbox/quant-Trader"
#: 落地点：``data`` 是指向 ``/media/zbox/data/quantmind`` 的符号链接 ⇒ 工作树之外
DEFAULT_DEST = PROJECT_ROOT / "data" / "legacy" / "quanttrader"


def _print_plan(plan) -> None:
    print(
        f"[计划] 源 {plan.source_root} → {len(plan.files)} 只 / {plan.total_bytes} 字节"
    )
    for name, count in sorted(plan.categories.items()):
        print(f"  · {name:11s} {count:4d} 只")
    for s in plan.skipped:
        print(f"  [跳过] {s.path} —— {s.reason}")
    for w in plan.warnings:
        print(f"  [告警] {w}")
    for p in plan.problems:
        print(f"  [问题] {p}")


def _inside_git_worktree(path: Path) -> str | None:
    """落地点若在某个 git 工作树里，返回该工作树根（否则 ``None``）。

    判据的**唯一实现**已搬到 ``shared/legacy_assets.inside_git_worktree``
    （P5 守护计划迁移的 ``--record`` 存档用同一份：两处各写一份，收紧一处就会漏掉
    另一处，而漏掉的那一处正好是恢复现场时唯一还留着证据的文件）。这里保留这个
    名字，是为了不动既有用例的调用点。
    """
    return inside_git_worktree(path)


def _dest_refusal(dest: Path) -> str | None:
    """落地点不合规时返回理由（``None`` = 放行）。

    判据的**唯一实现**已搬到 ``shared/migration_paths.artifact_refusal``（P5 守护计划
    迁移的 ``--record`` 存档用同一份：见 ``migrate_legacy_watch._record_refusal``）。
    这里保留这个函数名，是为了不动既有用例的调用点。
    """
    return artifact_refusal(dest, project_root=PROJECT_ROOT)


def _run(args: argparse.Namespace) -> int:
    source = Path(args.source).expanduser()
    dest = Path(args.dest).expanduser()

    if args.command in ("plan", "apply"):
        why = _dest_refusal(dest)
        if why is not None:
            print(
                f"[迁移] 落地点 {dest}（解析后 {dest.resolve()}）{why}：拒绝执行。"
                "迁移产物必须落在仓库树之外——本仓 data/ 是符号链接、data/*.sql 是"
                "被跟踪文件，写进仓库树会被下一次 git add -A 波及。本命令在**宿主机**上跑"
                "（容器内 /app 没有 .git，判断不可靠）。"
            )
            return EXIT_USAGE
        plan = plan_legacy_assets(source)
        _print_plan(plan)
        if plan.problems:
            return EXIT_USAGE
        if args.command == "plan":
            if not plan.ok:
                print("[计划] 有跳过或告警：搬之前先看一眼上面几行")
                return EXIT_ATTENTION
            print("[计划] 全清；加 --apply 落盘")
            return EXIT_OK
        report = apply_plan(plan, dest)
        if not report.ok:
            for p in report.problems:
                print(f"[落盘] {p}")
            return EXIT_USAGE
        print(
            f"[落盘] 写入 {report.written} 只 / {report.bytes_written} 字节 → {dest}"
            f"（{ARCHIVE_DIR}/ + {MANIFEST_NAME} + {MANIFEST_NAME.replace('.jsonl', '.sha256')}）"
        )
        # 落盘后立刻自复验：写进去的和清单说的必须一致（含源侧重比）
        v = verify(dest, source_root=source, check_source=True)
        print(f"[复验] {v.detail()}")
        if not v.ok:
            return EXIT_ATTENTION
        return EXIT_ATTENTION if (plan.skipped or plan.warnings) else EXIT_OK

    # verify
    if not (dest / MANIFEST_NAME).is_file():
        print(f"[复验] 清单不存在：{dest / MANIFEST_NAME}（先跑 --apply）")
        return EXIT_USAGE
    v = verify(
        dest,
        source_root=source,
        check_source=not args.no_source_check,
    )
    print(f"[复验] {v.detail()}")
    if v.problems:
        return EXIT_USAGE
    return EXIT_OK if v.ok else EXIT_ATTENTION


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="P3-② 隔壁资产落盘（逐字节 + 清单 + 复验）",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply",
        dest="command",
        action="store_const",
        const="apply",
        help="落盘（默认只出计划）",
    )
    mode.add_argument(
        "--verify",
        dest="command",
        action="store_const",
        const="verify",
        help="复验已落盘结果（不写任何东西）",
    )
    parser.set_defaults(command="plan")
    parser.add_argument(
        "--from",
        dest="source",
        default=DEFAULT_SOURCE_ROOT,
        help=f"源根（默认 {DEFAULT_SOURCE_ROOT}）",
    )
    parser.add_argument(
        "--dest", default=str(DEFAULT_DEST), help=f"落地点（默认 {DEFAULT_DEST}）"
    )
    parser.add_argument(
        "--no-source-check",
        action="store_true",
        help="复验时不重算源侧哈希（源已归档/不可读时用）",
    )
    args = parser.parse_args(argv)

    if args.no_source_check and args.command != "verify":
        print("[迁移] --no-source-check 只对 --verify 有意义")
        return EXIT_USAGE
    try:
        return _run(args)
    except OSError as exc:
        print(f"[迁移] 环境错误：{exc}")
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
