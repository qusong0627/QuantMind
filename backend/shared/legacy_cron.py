"""隔壁（quant-Trader）crontab 的行模型与归属分类机制 —— 纯层，不读也不写。

P4 定时任务迁移的解码器。要解的题：隔壁 crontab 是本机**唯一的事实源**——隔壁仓里
没有 crontab 的版本化源文件（实测），76 行里混着三类东西：本仓自己的作业（如
``postmarket_pipeline.py`` 两条）、隔壁的作业（会真下单的 8 条 + 研究/新闻/池）、
以及与本机/记忆网关相关的作业（NAS 挂载守护、``.memory-tencentdb`` 开收窗）。
**无脑清空会连本仓的盘后复盘一起停掉。**

所以归属不按**行号**判（行号会随增删漂移），而是给每条作业行算一个**内容派生的
身份** ``JobKey`` = (脚本 basename, 参数元组)。身份刻意**不含**解释器路径、日志
重定向与尾部注释：迁移时把 ``/home/zbox/baymax/.venv/bin/python`` 换成别的解释器、
或把日志重定向到别处，都不该被当成「换了一个作业」。

身份也刻意**不含**五段时刻：同一条 ``live_hourly_analysis.py`` 挂在 4 个时刻上
（L4/L9/L52/L51 无参 + ``--record-only``），它们是**一个**作业的多次触发；反过来
``live_llm_trade.py --execute`` 与 ``--execute --catch-up`` 参数不同 ⇒ 两个作业。

**时刻一律原样保留**：cron 读本机时区（JST），表内时刻与注释里的「北京 = 表内 −1h」
是一套自洽口径，迁移时改时刻等于改行为。迁移**只删行、不改写**：未删的行按 ``raw``
逐字保留，所以时刻、重定向、尾部注释全都不动。

纯函数：本模块不读 crontab、不起子进程。读写与 ``crontab -n`` 干跑都在 CLI 层，
语义校验交给 cron 自己（``crontab -n`` 只查语法不安装，实测可用）。
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

__all__ = [
    "CronLine",
    "JobKey",
    "parse_crontab",
    "paused_job",
]

# 五段字段的**结构**校验：只查字符集，不查语义（``99`` 分钟这里不拦，``crontab -n`` 拦）。
# 语义交给 cron 自己，理由是不重复实现一套 cron 规则再去和它对账。
_FIELD_OK = re.compile(r"^[0-9A-Za-z*/,\-]+$")
_NICKNAMES = frozenset(
    {
        "@reboot",
        "@yearly",
        "@annually",
        "@monthly",
        "@weekly",
        "@daily",
        "@midnight",
        "@hourly",
    }
)
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# 解释器：第一个 token 是它就把自己摘掉，下一位才是脚本。
_INTERPRETERS = frozenset(
    {
        "python",
        "python2",
        "python3",
        "python3.10",
        "python3.11",
        "python3.12",
        "bash",
        "sh",
        "zsh",
    }
)
# 重定向：``2>&1`` 这种自带 fd 目标的整体丢弃；``>>`` / ``2>`` 这种要连目标一起丢；
# 粘连写法（``>/dev/null``、``2>/var/log/x``）是**一个** token，也要整体丢 —— 不丢就会
# 混进身份（``a.py`` 的参数里多出一个 ``>/dev/null``），而它恰恰是最常见的写法。
_REDIR_SELF = re.compile(r"^\d*[<>]{1,2}&\d+$")
_REDIR_GLUED = re.compile(r"^\d*[<>]{1,2}[^<>&\s]+$")
_REDIR_OP = re.compile(r"^\d*[<>]{1,2}$")
# 复合命令（``a && b``、``a; b``、``a | b``）：整条归一化后当一个身份，不做拆解
# （拆解要判 shell 语义，收益只有「表更短」，代价是可能拆错）。
_COMPOUND = re.compile(r"(?:^|\s)(?:&&|\|\||;|\|)(?:\s|$)")
# 停用理由的方括号注记（半角 ``[…]`` 与全角 ``【…】`` 两种写法都见过）
_NOTE_PREFIX = re.compile(r"^\s*[\[【][^\]】]*[\]】]\s*")


@dataclass(frozen=True)
class JobKey:
    """一条作业的**内容派生身份**（与行号无关、与解释器路径/重定向无关）。

    ``script`` = 脚本 basename；复合命令没有单一脚本，``script`` 用 ``"<compound>"``，
    此时 ``args`` 是它按序跑的几个**脚本名**（一个都认不出时才是整条命令字面）。
    """

    script: str
    args: tuple[str, ...] = ()

    def __str__(self) -> str:
        return " ".join((self.script, *self.args)).strip()


@dataclass(frozen=True)
class CronLine:
    """crontab 的一行。

    ``kind``: ``blank`` / ``comment`` / ``env`` / ``job`` / ``unknown``。
    ``unknown`` = 既不像空行/注释/环境变量、又解析不出作业行 ⇒ **CLI 必须拒绝执行**
    （fail-closed：认不出的行不能静默放行，它可能正是会下单的那条）。
    """

    raw: str
    kind: str
    schedule: str = ""
    command: str = ""
    trailing: str = ""
    job: JobKey | None = None

    @property
    def is_job(self) -> bool:
        return self.kind == "job"


def _split_fields(text: str, count: int) -> tuple[list[str], str] | None:
    """从左吃 ``count`` 个空白分隔的字段，余下部分**原样**返回（命令段不折叠空格）。"""
    fields: list[str] = []
    pos = 0
    for _ in range(count):
        m = re.compile(r"\S+").search(text, pos)
        if m is None:
            return None
        fields.append(m.group())
        pos = m.end()
    return fields, text[pos:].lstrip()


def _strip_trailing_comment(command: str) -> tuple[str, str]:
    """切出尾部 shell 注释（`` # ...``）。**前有空白**的 ``#`` 才是注释（同 shell 语义，
    ``a#b`` 不是）。引号内的 ``#`` 不切。返回 ``(命令, 注释含 # )``。"""
    quote = ""
    for i, ch in enumerate(command):
        if quote:
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
            continue
        if ch == "#" and i > 0 and command[i - 1].isspace():
            return command[:i].rstrip(), command[i:]
    return command, ""


def _strip_redirections(tokens: list[str]) -> list[str]:
    """丢掉重定向与后台符号（身份不该被日志落点影响）。自带 fd 目标的整体丢，
    粘连写法整体丢，操作符带目标的一起丢。"""
    out: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if _REDIR_SELF.match(tok) or _REDIR_GLUED.match(tok) or tok == "&":
            i += 1
            continue
        if _REDIR_OP.match(tok):
            i += 2  # 操作符 + 目标文件
            continue
        out.append(tok)
        i += 1
    return out


def _identity(command: str) -> JobKey | None:
    """命令段 → 身份。认不出（引号不配对等）返回 ``None`` ⇒ 上层拒绝。"""
    body, _ = _strip_trailing_comment(command)
    body = body.strip()
    if not body:
        return None
    if _COMPOUND.search(body):
        # 复合命令不拆解（拆要判 shell 语义），重定向同样不进身份。
        # 身份取「这条命令跑哪几个脚本」（按出现顺序）而不是整条命令字面：解释器换路径、
        # 加个 flag、空格变一下都不该被当成「换了一个作业」——与非复合分支同一条纪律。
        # 一个脚本名都认不出时退回整条字面（身份不能为空）。
        joined = _strip_redirections(body.split())
        scripts = [
            tok.rsplit("/", 1)[-1]
            for tok in joined
            if "/" in tok and tok.rsplit("/", 1)[-1].endswith((".py", ".sh"))
        ]
        if scripts:
            return JobKey("<compound>", tuple(scripts))
        return JobKey("<compound>", (" ".join(joined),))
    try:
        tokens = shlex.split(body)
    except ValueError:
        return None
    tokens = _strip_redirections(tokens)
    # 前导 ``KEY=VALUE``（命令前的环境变量）不参与身份
    while tokens and _ENV_ASSIGN.match(tokens[0]):
        tokens = tokens[1:]
    if not tokens:
        return None
    head = tokens[0]
    if head.rsplit("/", 1)[-1] in _INTERPRETERS:
        tokens = tokens[1:]
        if not tokens:
            return None
        head = tokens[0]
    return JobKey(head.rsplit("/", 1)[-1], tuple(tokens[1:]))


def _parse_job(rest: str) -> tuple[str, str, str, JobKey] | None:
    """``<五段或 @nick> <命令>`` → ``(schedule, command, trailing, job)``。认不出返回 None。"""
    stripped = rest.strip()
    if not stripped:
        return None
    if stripped[0] == "@":
        parts = _split_fields(stripped, 1)
        if parts is None:
            return None
        (schedule,), command = parts
        if schedule.lower() not in _NICKNAMES:
            return None
    else:
        parts = _split_fields(stripped, 5)
        if parts is None:
            return None
        fields, command = parts
        if not all(_FIELD_OK.match(f) for f in fields):
            return None
        schedule = " ".join(fields)
    if not command.strip():
        return None
    job = _identity(command)
    if job is None:
        return None
    _, trailing = _strip_trailing_comment(command)
    return schedule, command, trailing, job


def parse_crontab(text: str) -> list[CronLine]:
    """crontab 文本 → 行列表。**永不抛**；认不出的行标 ``unknown`` 交给上层拒绝。"""
    lines: list[CronLine] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            lines.append(CronLine(raw=raw, kind="blank"))
            continue
        if stripped[0] == "#":
            lines.append(CronLine(raw=raw, kind="comment"))
            continue
        if _ENV_ASSIGN.match(stripped):
            lines.append(CronLine(raw=raw, kind="env"))
            continue
        parsed = _parse_job(stripped)
        if parsed is None:
            lines.append(CronLine(raw=raw, kind="unknown"))
            continue
        schedule, command, trailing, job = parsed
        lines.append(
            CronLine(
                raw=raw,
                kind="job",
                schedule=schedule,
                command=command,
                trailing=trailing,
                job=job,
            )
        )
    return lines


def paused_job(line: CronLine) -> JobKey | None:
    """注释行里若嵌着一条**被停用**的作业，返回它的身份（否则 ``None``）。

    只用于盘点：注释行永远原样保留，工具绝不因为这里返回了什么而去动它。被停用的
    作业是地雷——它引用的脚本随隔壁一起停掉后，谁恢复这行注释谁踩。

    判据两条，缺一不可：①从某个位置起能解析出五段/``@nick``；②命令段长得像调用
    （含 ``/`` 或 ``.py``/``.sh``）——否则纯散文注释也会被认成作业。
    """
    if line.kind != "comment":
        return None
    body = line.raw.strip()
    while body.startswith("#"):
        body = body[1:]
    # 停用理由常写成方括号注记（``[2026-09-11 用户要求:…]`` 或 ``【A股优先·暂停】``），
    # 且第二种会**紧贴时刻**（``…暂停】35 10 * * 1-5``），不剥掉就切不出干净的五段。
    body = _NOTE_PREFIX.sub("", body)
    tokens = body.split()
    for start in range(len(tokens)):
        candidate = " ".join(tokens[start:])
        parsed = _parse_job(candidate)
        if parsed is None:
            continue
        _, command, _, job = parsed
        if "/" in command or ".py" in command or ".sh" in command:
            return job
        return None  # 解析成功但不像调用 ⇒ 这是散文，不是作业
    return None
