#!/usr/bin/env python3
"""P4 定时任务迁移 —— 把隔壁 quant-Trader 的 crontab 收敛成本仓的（**宿主工具**）。

用法（默认预演：只读，一行不改）

    python backend/scripts/migrate_legacy_cron.py                        # 预演：分类 + 目标 diff
    python backend/scripts/migrate_legacy_cron.py --print-target         # 连目标表原文一起打出来
    python backend/scripts/migrate_legacy_cron.py --file <crontab 文本>   # 离线预演（不碰活表）
    python backend/scripts/migrate_legacy_cron.py --apply --record <仓外目录> [--accept-risks]
    python backend/scripts/migrate_legacy_cron.py --verify --record <仓外目录>

退出码：``0`` 干净可执行 / ``1`` **有事要人看**（接手方开关没开、只有代码级证据、有告警、
有人工确认项）/ ``2`` 环境或参数错（有拒绝项、crontab 不可用、存档落点不合规…）。

五条纪律（每条对应一种「跑完看起来正常」的失效）:

1. **只在宿主上跑**。容器里没有 ``crontab`` 命令（实测），在容器里跑本工具只会在「读不到活表」
   与「假装成功」之间选一个 —— 所以直接拒绝（退 2）。
2. **语义校验交给 cron 自己**。``crontab -n <文件>`` 是原生干跑（只查语法、不安装）。
   本工具不重复实现一套 cron 语法再去和它对账，只做结构校验（见 ``legacy_cron``）。
3. **只删行、不改写**。未删的行按原文逐字保留（时刻、重定向、尾部注释全不动）——
   时刻改了就是改了行为，而 cron 读的是本机时区（JST，北京 = 表内 −1h）。
4. **先存档、再干跑、最后才装**。存档必须落在仓库树与 git 工作树之外
   （``artifact_refusal``，两条判据并列）；装完**回读**比对，不一致就停下喊人。
5. **不猜、不部分执行**。有任何一条拒绝项就整轮停手（退 2）；接手方开关没开时，
   ``--apply`` 需要 ``--accept-risks`` 明确承担（「删掉隔壁的下单方」必须是知情的）。

本工具**不是**停机开关：它只把「该删的删掉、该加的加上」写进 crontab。真正让隔壁停手的
是它那些作业本身被删除 —— 而这件事只能在「本仓实盘跑通」之后做（见迁移计划 P5/P6）。
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.shared.legacy_cron import CronLine, parse_crontab, paused_job  # noqa: E402
from backend.shared.legacy_cron_plan import (  # noqa: E402
    ADDITIONS,
    PLAN_BY_KEY,
    check_plan,
)
from backend.shared.legacy_cron_target import (  # noqa: E402
    Evidence,
    Target,
    build_target,
    check_evidence,
)
from backend.shared.migration_paths import artifact_refusal  # noqa: E402

EXIT_OK = 0
EXIT_ATTENTION = 1
EXIT_USAGE = 2

_DISPOSITION_ORDER = (
    "qm_covered",
    "retire_stop",
    "must_keep",
    "migrate",
    "keep_as_is",
    "pending_decision",
)
_DISPOSITION_LABEL = {
    "qm_covered": "本仓已接手（删）",
    "retire_stop": "随隔壁停（删）",
    "must_keep": "本仓依赖（留）",
    "migrate": "本仓作业（留）",
    "keep_as_is": "机器级/第三方（留）",
    "pending_decision": "要人裁决（留）",
}


class UsageError(RuntimeError):
    """环境或参数层面的错误（退 2）。"""


# ── 环境 ────────────────────────────────────────────────────────────────────
def _crontab_bin() -> str:
    found = shutil.which("crontab")
    if not found:
        raise UsageError(
            "找不到 `crontab` 命令：本工具只能在装有 cron 的**宿主**上跑"
            "（容器里没有 crontab；容器内跑只会在「读不到活表」和「假装成功」之间选一个）。"
        )
    return found


def _read_live(bin_path: str) -> str:
    proc = subprocess.run([bin_path, "-l"], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        err = (proc.stderr or "").strip() or (proc.stdout or "").strip()
        if "no crontab" in err.lower():
            return ""
        raise UsageError(f"`crontab -l` 失败（rc={proc.returncode}）：{err}")
    return proc.stdout


def _native_dry_run(bin_path: str, text: str) -> str | None:
    """``crontab -n <文件>``：cron 自己的语法干跑（**不安装**）。通过返回 ``None``。"""
    tmp = _write_temp(text)
    try:
        proc = subprocess.run(
            [bin_path, "-n", tmp], capture_output=True, text=True, check=False
        )
        if proc.returncode != 0:
            return (proc.stderr or proc.stdout or "").strip() or f"rc={proc.returncode}"
        return None
    finally:
        Path(tmp).unlink(missing_ok=True)


def _install(bin_path: str, text: str) -> None:
    tmp = _write_temp(text)
    try:
        proc = subprocess.run(
            [bin_path, tmp], capture_output=True, text=True, check=False
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            raise UsageError(
                f"`crontab <目标表>` 安装失败（rc={proc.returncode}）：{err}"
            )
    finally:
        Path(tmp).unlink(missing_ok=True)


def _write_temp(text: str) -> str:
    fd, path = tempfile.mkstemp(prefix="qm-crontab-", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def _code_probe(path: str, symbol: str) -> bool:
    """``code:<路径>:<符号>`` 证据的存在性核对。

    用 AST 而不是裸 grep：注释里提一句、字符串里写一次都不算「接手方在那儿」。
    只认**定义**（函数/类/模块级赋值）—— 引用不算。
    """
    target = PROJECT_ROOT / path
    try:
        tree = ast.parse(target.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return False
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == symbol:
                return True
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else None
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for t in targets or ():
            if isinstance(t, ast.Name) and t.id == symbol:
                return True
    return False


# ── 报告 ────────────────────────────────────────────────────────────────────
def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _norm_lines(text: str) -> list[str]:
    """比对用归一：逐行去尾空白 + 丢掉尾部空行（crontab 自己的规范化不该报成漂移）。"""
    out = [ln.rstrip() for ln in text.splitlines()]
    while out and not out[-1]:
        out.pop()
    return out


def _host_tz() -> str:
    now = datetime.now().astimezone()
    return f"{now.tzname()} {now.strftime('%z')}"


def _paused_identities(lines: list[CronLine]) -> list:
    """注释行里被停用的作业身份（按首次出现去重）。

    它们**不进分类、不进删除**（工具对注释行零动作），列出来只有一个用处：提醒
    「解注释之前先确认脚本还在」。隔壁那一族被停用的作业引的是隔壁 ``.venv``，
    隔壁一停，解注释得到的是一条死作业（cron 不会报错，只是什么都不发生）。
    """
    seen: dict[object, None] = {}
    for ln in lines:
        job = paused_job(ln)
        if job is not None:
            seen.setdefault(job, None)
    return list(seen)


def build_evidence() -> Evidence:
    from backend.shared.scheduler_registry import JOBS_BY_KEY

    return check_evidence(
        PLAN_BY_KEY,
        jobs_by_key=JOBS_BY_KEY,
        env=os.environ,
        code_probe=_code_probe,
    )


def print_report(
    *,
    source: str,
    lines: list[CronLine],
    target: Target,
    evidence: Evidence,
    live_text: str,
    print_target: bool,
) -> None:
    jobs = [ln for ln in lines if ln.is_job]
    counts = check_plan()
    print("=== P4 定时任务迁移（预演；一行未改）" + "=" * 12)
    print(f"活表来源: {source}")
    print(f"本机时区: {_host_tz()}（北京 = 表内 − 1h；时刻一律不动）")
    print(f"行数 {len(lines)} · 作业行 {len(jobs)} · 身份 {len(PLAN_BY_KEY)}")
    print(
        "分类: "
        + " / ".join(
            f"{_DISPOSITION_LABEL[d]} {counts.get(d, 0)}" for d in _DISPOSITION_ORDER
        )
    )

    print("\n── 接手方核对（本仓已接手的，逐条核过）" + "─" * 20)
    if not evidence.coverages:
        print("  （无）")
    for cov in evidence.coverages:
        mark = "!" if cov.risky else "√"
        detail = f"  {cov.detail}" if cov.detail else ""
        print(f"  {mark} {cov.job}  ←  {cov.kind}:{cov.evidence}{detail}")
    print(
        "  说明: √ 表示接手方存在且（注册表作业）开关为开；! 要人看一眼 —— 见下方人工确认项。"
    )

    print(
        f"\n── 将删除 {len(target.dropped)} 行（{_DISPOSITION_LABEL['qm_covered']} + "
        f"{_DISPOSITION_LABEL['retire_stop']}）" + "─" * 10
    )
    for ln in target.dropped:
        entry = PLAN_BY_KEY.get(ln.job) if ln.job else None
        tag = entry.disposition if entry else "?"
        print(f"  - [{tag}] {ln.schedule}  {ln.job}")

    print(f"\n── 迁移新增 {len(target.added)} 行（隔壁没有对应行）" + "─" * 10)
    for raw in target.added or ADDITIONS:
        print(f"  + {raw.split('  #')[0].strip()}")

    print("\n── 逐条裁决（留下的是为什么留）" + "─" * 20)
    for ln in target.kept:
        if not ln.is_job or ln.job is None:
            continue
        entry = PLAN_BY_KEY.get(ln.job)
        if entry is None:
            continue
        if entry.disposition == "keep_as_is":
            continue  # 机器级/第三方，不必逐条念
        print(f"  [{_DISPOSITION_LABEL[entry.disposition]}] {ln.job}")
        print(f"      {entry.why}")

    paused = _paused_identities(lines)
    if paused:
        print(
            f"\n── 停用行 {len(paused)} 条（注释掉的作业；工具**原样保留**，不删也不启）"
            + "─" * 4
        )
        for job in paused:
            print(f"  · {job}")
        print(
            "  说明: 停用行不进分类、不进删除，但**恢复它们之前要看一眼** —— 它们引用的"
            "脚本/解释器（例：隔壁 .venv）可能随隔壁一起消失，解注释就是一条死作业。"
        )

    if evidence.manual_checks:
        print(
            f"\n── 人工确认项 {len(evidence.manual_checks)} 条（工具判不了的）"
            + "─" * 12
        )
        for i, note in enumerate(evidence.manual_checks, 1):
            print(f"  {i}. {note}")

    if target.warnings:
        print(f"\n── 告警 {len(target.warnings)} 条" + "─" * 20)
        for w in target.warnings:
            print(f"  ! {w}")

    if target.refusals or evidence.refusals:
        print(
            f"\n── 拒绝项 {len(target.refusals) + len(evidence.refusals)} 条（**不执行**）"
            + "─" * 12
        )
        for r in (*target.refusals, *evidence.refusals):
            print(f"  ✘ {r}")

    print("\n── 结果" + "─" * 30)
    print(
        f"  活表 {_sha(live_text)[:12]} → 目标表 {_sha(target.text)[:12]}"
        f"（{len(target.lines)} 行）"
    )
    if print_target:
        print("\n── 目标 crontab 全文" + "─" * 20)
        print(target.text)


def report_json(
    *,
    source: str,
    lines: list[CronLine],
    target: Target,
    evidence: Evidence,
    live_text: str,
    action: str,
) -> dict:
    jobs = [ln for ln in lines if ln.is_job]
    return {
        "action": action,
        "generated_at": datetime.now().astimezone().isoformat(),
        "host_tz": _host_tz(),
        "source": source,
        "counts": {
            "lines": len(lines),
            "job_lines": len(jobs),
            "identities": len(PLAN_BY_KEY),
            "disposition": check_plan(),
        },
        "dropped": [
            {
                "schedule": ln.schedule,
                "job": str(ln.job),
                "raw": ln.raw,
                "disposition": getattr(PLAN_BY_KEY.get(ln.job), "disposition", ""),
                "why": getattr(PLAN_BY_KEY.get(ln.job), "why", ""),
            }
            for ln in target.dropped
        ],
        "added": list(target.added),
        "kept_lines": [ln.raw for ln in target.kept],
        "coverages": [
            {
                "job": str(c.job),
                "kind": c.kind,
                "evidence": c.evidence,
                "detail": c.detail,
                "risky": c.risky,
            }
            for c in evidence.coverages
        ],
        "manual_checks": list(evidence.manual_checks),
        "refusals": [*target.refusals, *evidence.refusals],
        "warnings": list(target.warnings),
        "before_sha256": _sha(live_text),
        "after_sha256": _sha(target.text),
    }


# ── 命令 ────────────────────────────────────────────────────────────────────
def _load_lines(args) -> tuple[list[CronLine], str, str | None]:
    """→ (行, 来源说明, crontab 可执行文件)。离线模式返回 ``None`` 当可执行文件。"""
    if args.file:
        text = Path(args.file).read_text(encoding="utf-8")
        return parse_crontab(text), f"文件 {args.file}", None
    bin_path = _crontab_bin()
    text = _read_live(bin_path)
    return parse_crontab(text), "crontab -l（本机活表）", bin_path


def _exit_code(target: Target, evidence: Evidence) -> int:
    if target.refusals or evidence.refusals:
        return EXIT_USAGE
    if (
        any(c.risky for c in evidence.coverages)
        or evidence.manual_checks
        or target.warnings
    ):
        return EXIT_ATTENTION
    return EXIT_OK


def _cmd_plan(args) -> int:
    try:
        lines, source, bin_path = _load_lines(args)
    except UsageError as exc:
        print(f"✘ {exc}", file=sys.stderr)
        return EXIT_USAGE
    evidence = build_evidence()
    target = build_target(lines, plan=PLAN_BY_KEY, additions=ADDITIONS)
    live_text = "\n".join(ln.raw for ln in lines) + "\n" if lines else ""
    print_report(
        source=source,
        lines=lines,
        target=target,
        evidence=evidence,
        live_text=live_text,
        print_target=args.print_target,
    )
    code = _exit_code(target, evidence)

    if not args.apply:
        if code == EXIT_ATTENTION:
            print(
                "\n（退 1：先把上面的「人工确认项 / 告警 / 带 ! 的接手方」处理掉再动手。）"
            )
        elif code == EXIT_USAGE:
            print("\n（退 2：有拒绝项，工具不往下走。）")
        else:
            print("\n（退 0：可以按 --apply 执行。）")
        return code

    return _do_apply(
        args,
        lines=lines,
        source=source,
        bin_path=bin_path,
        target=target,
        evidence=evidence,
        live_text=live_text,
        code=code,
    )


def _do_apply(
    args,
    *,
    lines: list[CronLine],
    source: str,
    bin_path: str | None,
    target: Target,
    evidence: Evidence,
    live_text: str,
    code: int,
) -> int:
    if bin_path is None:
        print("✘ --apply 不能与 --file 同用：装的是这台机器上的真表。", file=sys.stderr)
        return EXIT_USAGE
    if code == EXIT_USAGE:
        print("\n✘ 有拒绝项：整轮停手（不猜、不部分执行）。", file=sys.stderr)
        return EXIT_USAGE
    risky = [c for c in evidence.coverages if c.risky]
    if risky and not args.accept_risks:
        print(
            "\n✘ 接手方里有「开关没开 / 只有代码级证据」的："
            + ", ".join(f"{c.job} ← {c.evidence}" for c in risky),
            file=sys.stderr,
        )
        print(
            "  删掉被覆盖的那条 cron 之前，接手方必须**真的在跑** ——「本仓已接手」≠"
            "「本仓此刻在跑」，看错就是静默裸奔（承接方默认关：QM_DECISION_ROUND_ENABLED /"
            " QM_LEVERAGE_TRIM_ENABLED）。\n"
            "  确认知情并承担，加 --accept-risks 再跑。",
            file=sys.stderr,
        )
        return EXIT_USAGE
    if not [ln for ln in lines if ln.is_job]:
        print(
            "✘ 活表里没有任何作业行 —— 这不是预期状态，先确认 crontab 读对了。",
            file=sys.stderr,
        )
        return EXIT_USAGE

    record = Path(args.record)
    refusal = artifact_refusal(record, project_root=PROJECT_ROOT)
    if refusal:
        print(
            f"✘ 存档落点不合规（{refusal}）：存档要落在仓库树与 git 工作树之外，"
            f"例如 data/legacy/crontab（data/ 是指向仓外的符号链接）或 /tmp 再取走。",
            file=sys.stderr,
        )
        return EXIT_USAGE

    if _norm_lines(target.text) == _norm_lines(live_text):
        print("\n√ 活表已是目标表（无需变更；新增行按身份认出，不重复追加）。")
        return EXIT_OK

    dry = _native_dry_run(bin_path, target.text)
    if dry is not None:
        print(
            f"✘ crontab 原生干跑（crontab -n）不通过，**没有安装任何东西**：{dry}",
            file=sys.stderr,
        )
        return EXIT_USAGE

    record.mkdir(parents=True, exist_ok=True)
    (record / "before.crontab").write_text(live_text, encoding="utf-8")
    (record / "after.crontab").write_text(target.text, encoding="utf-8")
    (record / "report.json").write_text(
        json.dumps(
            report_json(
                source=source,
                lines=lines,
                target=target,
                evidence=evidence,
                live_text=live_text,
                action="apply",
            ),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        f"\n√ 存档三件已落盘: {record}（before.crontab / after.crontab / report.json）"
    )

    _install(bin_path, target.text)
    back = _read_live(bin_path)
    if _norm_lines(back) != _norm_lines(target.text):
        print(
            "✘ 安装后**回读不一致** —— 已停下，请人工核对（工具不自动回滚，回滚一条命令即可）",
            file=sys.stderr,
        )
        tgt, got = _norm_lines(target.text), _norm_lines(back)
        print(f"  回滚: crontab {record / 'before.crontab'}", file=sys.stderr)
        if len(tgt) != len(got):
            print(
                f"  行数不一致: 目标 {len(tgt)} 行 / 实读 {len(got)} 行",
                file=sys.stderr,
            )
        for i, (a, b) in enumerate(zip(tgt, got, strict=False), 1):
            if a != b:
                print(f"  第 {i} 行: 目标={a!r}\n          实读={b!r}", file=sys.stderr)
                break
        return EXIT_USAGE

    print(
        f"√ 已安装并回读校验通过：删 {len(target.dropped)} 行 / 增 {len(target.added)} 行 / "
        f"活表 {len(_norm_lines(back))} 行"
    )
    print("  注意：删掉的是隔壁那些作业 —— 只有在本仓实盘跑通之后才该做这一步。")
    return EXIT_OK


def _cmd_verify(args) -> int:
    record = Path(args.record)
    after_path = record / "after.crontab"
    if not after_path.is_file():
        print(f"✘ 存档里没有 after.crontab: {after_path}", file=sys.stderr)
        return EXIT_USAGE
    try:
        lines, source, _ = _load_lines(args)
    except UsageError as exc:
        print(f"✘ {exc}", file=sys.stderr)
        return EXIT_USAGE
    live_text = "\n".join(ln.raw for ln in lines) + "\n" if lines else ""
    recorded = after_path.read_text(encoding="utf-8")

    evidence = build_evidence()
    target = build_target(lines, plan=PLAN_BY_KEY, additions=ADDITIONS)

    print("=== P4 定时任务迁移（复验）" + "=" * 20)
    print(f"活表来源: {source} · 存档: {record}")
    same = _norm_lines(live_text) == _norm_lines(recorded)
    print(f"活表 == 存档目标表: {'是' if same else '**否（漂移）**'}")
    if not same:
        live_set, rec_set = _norm_lines(live_text), _norm_lines(recorded)
        only_live = [ln for ln in live_set if ln not in rec_set]
        only_rec = [ln for ln in rec_set if ln not in live_set]
        for ln in only_live[:10]:
            print(f"  + 活表多出: {ln}")
        for ln in only_rec[:10]:
            print(f"  - 活表缺少（存档里有）: {ln}")
        print(
            "  说明: 漂移=应用之后有人又改了 crontab。本工具不自动纠偏，请人工看一眼上面这些行。"
        )
        if target.dropped:
            print(
                f"  另外：按分类表此刻仍有 {len(target.dropped)} 行该删（说明归档之后又被人加回来了）。"
            )

    code = _exit_code(target, evidence)
    if not same:
        return EXIT_ATTENTION
    if code == EXIT_USAGE:
        print("✘ 活表与存档一致，但分类核对里有拒绝项：")
        for r in (*target.refusals, *evidence.refusals):
            print(f"  ✘ {r}")
        return EXIT_USAGE
    print("√ 活表与存档目标表一致（逐行归一比对通过）")
    return code


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="migrate_legacy_cron.py",
        description="P4 定时任务迁移：隔壁 quant-Trader crontab → 本仓（宿主工具，默认预演）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--file",
        help="离线：把这个 crontab 文本当活表读（不碰真表；不能与 --apply 同用）",
    )
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply", action="store_true", help="装目标表（需 --record；先干跑再装再回读）"
    )
    mode.add_argument(
        "--verify", action="store_true", help="复验：活表是否等于存档的目标表"
    )
    ap.add_argument("--record", help="存档目录（必须在仓库树与 git 工作树之外）")
    ap.add_argument(
        "--accept-risks",
        action="store_true",
        help="明知接手方开关没开/只有代码级证据，仍要删（知情承担）",
    )
    ap.add_argument(
        "--print-target", action="store_true", help="把目标 crontab 全文打到 stdout"
    )
    args = ap.parse_args(argv)

    if args.apply and args.file:
        print("✘ --apply 不能与 --file 同用。", file=sys.stderr)
        return EXIT_USAGE
    if (args.apply or args.verify) and not args.record:
        print(
            "✘ --apply / --verify 必须给 --record（存档目录，落在仓库树之外）。",
            file=sys.stderr,
        )
        return EXIT_USAGE
    return _cmd_verify(args) if args.verify else _cmd_plan(args)


if __name__ == "__main__":
    sys.exit(main())
