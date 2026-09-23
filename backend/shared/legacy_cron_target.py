"""P4 目标 crontab 的构建与**归属断言核对** —— 纯层，不读也不写。

四件事：

1. **归属断言要能被机器核对**。「本仓已接手」必须给出证据，证据分两级：
   ``registry:<键>``（本仓调度注册表里的作业——工具核对它存在、报出它的开关状态）
   与 ``code:<相对路径>:<符号>``（代码落点——工具只能确认文件在这个符号也在，
   **判不了它此刻是不是在跑**）。分级是必要的：实盘循环族（``sltp_executor``、
   ``tdx_l2_capture``…）**不在**注册表里，混为一谈会让「已接手」这句话失去可核性。
2. **接手方默认关是常态**（``QM_DECISION_ROUND_ENABLED``、``QM_LEVERAGE_TRIM_ENABLED``
   默认 false）——「本仓已接手」与「本仓此刻在跑」是两句话，看错一句就是**静默裸奔**：
   隔壁那条删了、本仓这条没开，盘中既没有守护也没有下单方。故开关状态不是 on 即进
   ``manual_checks`` 并把该条标 ``risky``（``--apply`` 需要 ``--accept-risks`` 才放行）。
   开关取值还有**两种读法**（严词表只认 ``"true"``，宽词表认 ``1/yes/on``），工具只在
   两边口径一致时下结论，见 ``parse_env_flag``。
3. **工具必须说清自己的盲区**。判定不了的（开关在 Redis 里、证据只到代码级、删除有
   前置条件）一律走 ``manual_checks`` 打出来给人，不假装已核。
4. **fail-closed**：认不出的行、没分类的作业、点名的接手方不存在，一律进 ``refusals``，
   CLI 见到非空即拒绝执行（退 2）。宁可不动，不可乱动。
5. **新增段是声明式的**：新增行同样有自己的身份，二次运行时按身份认出「已经写过了」，
   不重复追加、也不被当成没分类的野作业拒掉（否则工具跑过一次之后就再也跑不动）。

时刻一律不动：cron 读本机时区（JST），表内时刻与注释里「北京 = 表内 −1h」自洽；
改时刻等于改行为。本模块**只可能删行，不改写任何一行**（未删的行逐字保留）。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from backend.shared.legacy_cron import CronLine, JobKey, parse_crontab

__all__ = [
    "ADDED_HEADER",
    "Coverage",
    "Target",
    "build_target",
    "check_evidence",
    "parse_env_flag",
    "REMOVABLE",
    "DISPOSITIONS",
]

#: 归属分类。只有前两个是可删的，其余结构上保留。
DISPOSITIONS = frozenset(
    {
        "qm_covered",  # 本仓已接手（必须给证据）
        "retire_stop",  # 随隔壁一起停：它服务的东西随隔壁消失
        "must_keep",  # 本仓依赖它，删了断粮
        "keep_as_is",  # 机器级/他人资产：不是本仓作业，也不随隔壁停
        "migrate",  # 作业本体本属本仓，只是调度挂在隔壁 cron 上 ⇒ 保留但改归本仓账
        "pending_decision",  # 有争议：本工具不删，列出来给人裁决
    }
)

REMOVABLE = frozenset({"qm_covered", "retire_stop"})

# 新增行插在带这个表头的段里，便于人工复核与二次运行时的重复检测。
ADDED_HEADER = "# ===== QuantMind 迁移新增（migrate_legacy_cron）====="

#: 唯一被**两种读法**都认作开的 token（``shared/env_flags.py`` 的 ``TRUE_TOKEN``）。
_TRUE_TOKEN = "true"
#: 两边都认作关的 token（宽词表的假值集）。
_FALSE = frozenset({"0", "false", "no", "off"})
_CODE_EVIDENCE = re.compile(
    r"^code:(?P<path>[^:]+):(?P<symbol>[A-Za-z_][A-Za-z0-9_]*)$"
)


def parse_env_flag(raw: str | None) -> str:
    """环境变量取值 → ``"on"`` / ``"off"`` / ``"unknown"``，**不谎报 on**。

    本仓同名变量有两种读法（分叉矩阵见 ``backend/shared/env_flags.py`` 模块头，实测过）：
    合规咽喉侧只认 ``"true"``（``1``/``yes``/``on`` 一律判**关**），注册表
    ``switch_enabled`` 的宽词表却认。工具要回答的是「删掉隔壁那条守护之后本仓这条到底
    跑不跑」，所以只在两边口径一致时下结论：

    * 都说开（归一后 == ``"true"``）⇒ ``on``；
    * 都说关（``0``/``false``/``no``/``off``）⇒ ``off``；
    * 其余一律 ``unknown`` —— ``"1"``/``"yes"``/``"on"`` 正是分叉点，而
      ``QM_DECISION_ROUND_ENABLED``/``QM_LEVERAGE_TRIM_ENABLED`` 恰好走严读法；
    * 未设置与空串也是 ``unknown``：默认值归注册表定（``switch_default_on``），这里不假装知道。

    ``unknown`` 一律进 ``risky`` + ``manual_checks``（人多看一眼），绝不进 √。
    """
    if raw is None:
        return "unknown"
    val = raw.strip().lower()
    if val == _TRUE_TOKEN:
        return "on"
    if val in _FALSE:
        return "off"
    return "unknown"


@dataclass(frozen=True)
class Coverage:
    """一条归属断言（``qm_covered``）的核对结果。"""

    job: JobKey
    kind: str  # "registry" | "code"
    evidence: str  # 注册表键 或 代码路径
    exists: bool
    detail: str = ""
    #: 需要人看一眼（开关状态不是 on，或证据只到代码级 ⇒ 判不了存活）
    risky: bool = False


@dataclass(frozen=True)
class Evidence:
    """``check_evidence`` 的产物：核对过的断言 + 待人工确认项 + 拒绝理由。"""

    coverages: tuple[Coverage, ...] = ()
    manual_checks: tuple[str, ...] = ()
    refusals: tuple[str, ...] = ()


def _probe_registry(
    entry: object,
    job: JobKey,
    jobs_by_key: Mapping[str, object],
    env: Mapping[str, str],
) -> tuple[Coverage | None, str | None, str | None]:
    """``registry:<键>`` 证据 → ``(核对结果, 人工确认项, 拒绝理由)``。"""
    key = str(getattr(entry, "covered_by", ""))[len("registry:") :]
    spec = jobs_by_key.get(key)
    if spec is None:
        return (
            None,
            None,
            f"点名接手方 {key!r}，但调度注册表里没有这个键 —— 断言指向不存在的作业"
            "（注册表 backend/shared/scheduler_registry.py）。",
        )
    switch_env = getattr(spec, "switch_env", None)
    default_on = bool(getattr(spec, "switch_default_on", True))
    # 空串按「未设置」算：注册表 ``switch_enabled`` 对空串走的也是默认值分支。
    raw = env.get(switch_env) if switch_env else None
    if not str(raw or "").strip():
        raw = None
    if switch_env is None:
        state = "on"
        shown = "无开关（常开）"
    elif raw is None:
        state = "on" if default_on else "off"
        shown = f"{switch_env} 未设置，注册表默认 {'开' if default_on else '关'}"
    else:
        state = parse_env_flag(raw)
        shown = f"{switch_env}={raw!r}"
    name = str(getattr(spec, "name", ""))
    detail = f"{name}（{getattr(spec, 'schedule', '')}）；{shown}"
    cov = Coverage(
        job=job,
        kind="registry",
        evidence=key,
        exists=True,
        detail=detail,
        risky=state != "on",
    )
    manual = None
    if state != "on":
        manual = f"接手方 '{key}'（{name}）开关状态 = {state}：删掉被覆盖的那条 cron 之前，它必须真的在跑。"
    return cov, manual, None


def _probe_code(
    entry: object, job: JobKey, code_probe: Callable[[str, str], bool]
) -> tuple[Coverage | None, str | None, str | None]:
    """``code:<路径>:<符号>`` 证据 → ``(核对结果, 人工确认项, 拒绝理由)``。"""
    raw = str(getattr(entry, "covered_by", ""))
    m = _CODE_EVIDENCE.match(raw)
    if m is None:
        return (
            None,
            None,
            f"证据写法认不出：{raw!r}（只认 ``registry:<键>`` 与 ``code:<相对路径>:<符号>``）。",
        )
    path, symbol = m.group("path"), m.group("symbol")
    found = code_probe(path, symbol)
    if not found:
        return (
            None,
            None,
            f"点名代码落点 {path}:{symbol}，但文件不存在或其中没有这个符号 —— "
            "断言指向不存在的接手方。",
        )
    return (
        # ``risky=True``：代码级证据**永远**要人看一眼 —— 工具只能确认「接手方在仓里」，
        # 判不了「它此刻在跑」。实盘循环族（sltp/l2/quote_backup）都不在注册表里，
        # 这条区分就是它们唯一的防线：标成 √ 会让人以为已经核过了。
        Coverage(
            job=job, kind="code", evidence=f"{path}:{symbol}", exists=True, risky=True
        ),
        f"接手方 '{path}:{symbol}' 是**代码级**证据（不是注册表作业）："
        f"本工具只能确认它在仓里，判不了它此刻是否在跑 —— 删 {job} 之前请人工确认。",
        None,
    )


def check_evidence(
    plan: Mapping[JobKey, object],
    *,
    jobs_by_key: Mapping[str, object],
    env: Mapping[str, str],
    code_probe: Callable[[str, str], bool],
) -> Evidence:
    """核对分类表里所有 ``qm_covered`` 断言，并收齐人工确认项。

    ``code_probe(path, symbol)`` 由 CLI 提供（查文件存在 + 符号在其中）；纯层不碰文件系统，
    用例给假探针即可。
    """
    coverages: list[Coverage] = []
    manual: list[str] = []
    refusals: list[str] = []
    for job, entry in plan.items():
        for item in getattr(entry, "manual_checks", ()) or ():
            manual.append(f"{job}：{item}")
        if getattr(entry, "disposition", None) != "qm_covered":
            continue
        raw = str(getattr(entry, "covered_by", ""))
        if raw.startswith("registry:"):
            cov, note, why = _probe_registry(entry, job, jobs_by_key, env)
        elif raw.startswith("code:"):
            cov, note, why = _probe_code(entry, job, code_probe)
        else:
            cov, note, why = (
                None,
                None,
                "归属写「本仓已接手」却没给证据 —— 没证据就无法核对接手方是否真的存在、"
                "是否真的开着（证据写法见 backend/shared/legacy_cron_target.py）。",
            )
        if why is not None:
            refusals.append(f"{job}：{why}")
            continue
        if cov is not None:
            coverages.append(
                Coverage(
                    job=job,
                    kind=cov.kind,
                    evidence=cov.evidence,
                    exists=cov.exists,
                    detail=cov.detail,
                    risky=cov.risky,
                )
            )
        if note:
            manual.append(note)
    return Evidence(
        coverages=tuple(coverages),
        manual_checks=tuple(manual),
        refusals=tuple(refusals),
    )


@dataclass(frozen=True)
class Target:
    """目标 crontab 与它的账。``refusals`` 非空 ⇒ 上层必须拒绝执行。"""

    lines: tuple[str, ...] = ()
    kept: tuple[CronLine, ...] = ()
    dropped: tuple[CronLine, ...] = ()
    added: tuple[str, ...] = ()
    refusals: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.refusals

    @property
    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


def _declared_additions(
    additions: Iterable[str],
) -> tuple[dict[JobKey, str], list[str]]:
    """新增声明 → ``{身份: 原始行}``。认不出（或一行解析出多个身份）⇒ 拒绝理由。

    新增行**也有身份**，这不是可有可无的：二次运行时它已经在 crontab 里，必须能被认出是
    「本工具写的」而不是「没分类的野作业」—— 否则工具跑过一次之后就再也跑不动了
    （会被自己的新增行拒掉）。判据用身份而不是字面，人改了日志路径/时刻也不会被重复追加。
    """
    declared: dict[JobKey, str] = {}
    refusals: list[str] = []
    for raw in additions:
        text = str(raw).strip()
        if not text:
            continue
        parsed = [ln for ln in parse_crontab(text) if ln.is_job]
        if len(parsed) != 1 or parsed[0].job is None:
            refusals.append(
                f"新增行声明认不出（必须是一行可解析的作业行，且只解析出一条）：{text!r}"
            )
            continue
        declared[parsed[0].job] = text
    return declared, refusals


def build_target(
    lines: Iterable[CronLine],
    *,
    plan: Mapping[JobKey, object],
    additions: Iterable[str] = (),
) -> Target:
    """构建目标 crontab。未删的行**逐字保留**；认不出/没分类的行进 ``refusals``。

    新增段是**声明式**的：已存在的（按身份认）不重复追加，缺的补上 —— 二次运行安全。
    """
    lines = list(lines)
    refusals: list[str] = []
    warnings: list[str] = []
    kept: list[CronLine] = []
    dropped: list[CronLine] = []
    present: set[JobKey] = set()
    out: list[str] = []

    declared, declared_refusals = _declared_additions(additions)
    refusals.extend(declared_refusals)

    for ln in lines:
        if ln.kind == "unknown":
            refusals.append(
                f"{ln.raw!r}：既不是空行/注释/环境变量，也解析不出五段作业行 —— "
                "认不出的行可能正是会下单的那条，不能静默放行。"
            )
            continue
        if not ln.is_job:
            kept.append(ln)
            out.append(ln.raw)
            continue
        assert ln.job is not None  # is_job 保证
        if ln.job in declared:
            kept.append(ln)
            out.append(ln.raw)
            present.add(ln.job)
            continue
        entry = plan.get(ln.job)
        if entry is None:
            refusals.append(
                f"{ln.job}：分类表里没有这条作业 —— 新增的定时任务必须先在 "
                "backend/shared/legacy_cron_plan.py 里判过归属。"
            )
            continue
        if getattr(entry, "disposition", "") in REMOVABLE:
            dropped.append(ln)
            continue
        kept.append(ln)
        out.append(ln.raw)

    missing = [raw for key, raw in declared.items() if key not in present]
    if missing:
        if any(ADDED_HEADER in ln.raw for ln in lines):
            warnings.append(
                f"crontab 里已有新增段表头 {ADDED_HEADER!r} 但新增行不全："
                "缺的已补到文件末尾，表头不重复写。"
            )
        else:
            out.append("")
            out.append(ADDED_HEADER)
        out.extend(missing)

    return Target(
        lines=tuple(out),
        kept=tuple(kept),
        dropped=tuple(dropped),
        added=tuple(missing),
        refusals=tuple(refusals),
        warnings=tuple(warnings),
    )
