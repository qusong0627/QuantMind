"""P4 定时任务迁移：隔壁 quant-Trader crontab → 本仓（纯层 / 机制层 / 分类表 / CLI 四段）。

为什么要这么多层测试：这个工具**删掉的是隔壁的下单与守护作业**，两种失效方向都无声——
删多了本仓盘后链路断粮（cron 不报错、脚本不跑也不报错），删少了隔壁继续在盘中对同一
账户下单。所以每一层都锁一条「无声失效」：

1. **纯层**（``legacy_cron``）：身份必须由内容派生（与行号、解释器路径、日志落点无关），
   认不出的行必须标 ``unknown`` 而不是猜；解析只做结构校验，语义留给 ``crontab -n``。
2. **机制层**（``legacy_cron_target``）：开关口径不许谎报 on（本仓同名变量有严/宽两个
   读者，实测分叉）；代码级证据永远要人看一眼；新增段必须二次运行安全。
3. **分类表**（``legacy_cron_plan``）：活表 49 个身份与表 49 条**一一对应**（多一个少一个
   都让工具拒绝执行）；处置分布即裁决结果，被写死在这里，改动必然过测试。
4. **CLI**：真表路径全部走桩（本用例**绝不碰线上 crontab**）；离线 ``--file`` 走真实现。

真表快照 = ``backend/tests/fixtures/legacy_crontab.txt``（sha256 锁死：改快照 = 重新裁决）。
"""

from __future__ import annotations

import hashlib
import importlib.util
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from backend.shared.legacy_cron import CronLine, JobKey, parse_crontab, paused_job
from backend.shared.legacy_cron_plan import ADDITIONS, PLAN, PLAN_BY_KEY, Entry
from backend.shared.legacy_cron_target import (
    ADDED_HEADER,
    DISPOSITIONS,
    REMOVABLE,
    Coverage,
    Evidence,
    build_target,
    check_evidence,
    parse_env_flag,
)

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "legacy_crontab.txt"
_FIXTURE_SHA = "16e3353d5e6c717bd23a10aefafd9d8f055cfbaba400fa307a3f181063da8854"
_CLI_PATH = Path(__file__).resolve().parents[1] / "scripts" / "migrate_legacy_cron.py"

_spec = importlib.util.spec_from_file_location(
    "backend.scripts.migrate_legacy_cron", _CLI_PATH
)
assert _spec is not None and _spec.loader is not None
migrate = importlib.util.module_from_spec(_spec)
sys.modules["backend.scripts.migrate_legacy_cron"] = migrate
_spec.loader.exec_module(migrate)


@pytest.fixture(scope="module")
def fixture_text() -> str:
    return _FIXTURE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def fixture_lines(fixture_text: str) -> list[CronLine]:
    return parse_crontab(fixture_text)


def _job(line: str) -> JobKey:
    """一行命令（五段补齐）→ 身份。"""
    lines = parse_crontab(f"*/5 * * * * {line}")
    assert lines[0].is_job and lines[0].job is not None, f"解析不出作业：{line!r}"
    return lines[0].job


def _line(text: str) -> CronLine:
    lines = parse_crontab(text)
    assert len(lines) == 1
    return lines[0]


# ── ① 纯层：解析与身份 ─────────────────────────────────────────────────────
def test_parse_classifies_all_five_kinds() -> None:
    text = "\n".join(
        [
            "# 注释",
            "",
            "SHELL=/bin/bash",
            "*/5 * * * * /usr/bin/python3 /opt/a.py",
        ]
    )
    assert [ln.kind for ln in parse_crontab(text)] == [
        "comment",
        "blank",
        "env",
        "job",
    ]


def test_parser_is_structural_only__semantics_belong_to_crontab_dash_n() -> None:
    """``this is not a cron line at all`` 结构上是合法五段行 —— 纯层**不许**加语义校验。

    锁这条是防「顺手在解析器里补一套 cron 语法」：那会与 ``crontab -n`` 形成第二套
    口径，两边不一致时以谁为准没人说得清。cron 自己会以 ``bad minute`` 拒绝它。
    """
    ln = _line("this is not a cron line at all")
    assert ln.kind == "job"
    assert ln.schedule == "this is not a cron"
    assert ln.command == "line at all"


def test_identity_ignores_interpreter_log_target_and_trailing_note() -> None:
    a = _job(
        "/home/zbox/baymax/.venv/bin/python /home/zbox/baymax/live_price_watch.py"
        " >> /home/zbox/baymax/logs/watch.log 2>&1  # 隔壁的守护"
    )
    b = _job("/usr/bin/python3 /elsewhere/live_price_watch.py >> /tmp/x.log 2>&1")
    assert a == b == JobKey("live_price_watch.py")


def test_identity_keeps_args__execute_and_catchup_are_two_jobs() -> None:
    a = _job("python3 /x/live_llm_trade.py --execute")
    b = _job("python3 /x/live_llm_trade.py --execute --catch-up")
    assert a != b
    assert a == JobKey("live_llm_trade.py", ("--execute",))


def test_identity_ignores_schedule__triggers_collapse_into_one_job(
    fixture_lines: list[CronLine],
) -> None:
    """迁移按**作业**判，不按时刻判。

    真表里 ``live_hourly_analysis.py`` 挂在 3 个时刻上（无参）+ 2 个时刻上
    （``--record-only``）：前者 = 一个身份（本仓已接手），后者 = **另一个**身份
    （随隔壁停）—— 参数不同就是不同作业，少判一个就少删一条或错删一条。
    """
    plain = [ln for ln in fixture_lines if ln.job == JobKey("live_hourly_analysis.py")]
    sampled = [
        ln
        for ln in fixture_lines
        if ln.job == JobKey("live_hourly_analysis.py", ("--record-only",))
    ]
    assert len(plain) == 3 and len(sampled) == 2
    assert len({ln.schedule for ln in plain}) == 3
    assert len({ln.schedule for ln in sampled}) == 2
    assert PLAN_BY_KEY[JobKey("live_hourly_analysis.py")].disposition == "qm_covered"
    assert (
        PLAN_BY_KEY[JobKey("live_hourly_analysis.py", ("--record-only",))].disposition
        == "retire_stop"
    )


def test_compound_identity_is_the_script_names_in_order() -> None:
    j = _job(
        "/usr/bin/python3 /a/fundamental_flags.py build"
        " && /usr/bin/python3 /a/industry_risk.py run"
    )
    assert j == JobKey("<compound>", ("fundamental_flags.py", "industry_risk.py"))
    # 顺序进身份：换顺序是另一条命令（虽然脚本名相同）
    rev = _job(
        "/usr/bin/python3 /a/industry_risk.py run"
        " && /usr/bin/python3 /a/fundamental_flags.py build"
    )
    assert rev != j


def test_compound_identity_ignores_interpreter_and_flags_between_scripts() -> None:
    a = _job("/usr/bin/python3 /x/a.py --flag && /usr/bin/python3 /x/b.py")
    b = _job("/other/python3 /y/a.py && bash /y/b.py --other")
    assert a == b == JobKey("<compound>", ("a.py", "b.py"))


def test_compound_identity_falls_back_to_literal_when_no_script_found() -> None:
    j = _job("echo hi && date")
    assert j.script == "<compound>"
    assert j.args == ("echo hi && date",)  # 一个脚本名都认不出时不许给出空身份


@pytest.mark.parametrize(
    "text",
    [
        "*/5 * * * * python3 'unterminated",  # 引号不配对
        "*/5 * * foo",  # 字段不够五段
        "*/5 * * * *",  # 有时刻没命令
        "*/5 * * * * >/dev/null",  # 命令段只剩重定向
    ],
)
def test_unparsable_shapes_are_unknown_never_guessed(text: str) -> None:
    assert _line(text).kind == "unknown"


def test_round_trip_preserves_every_line_verbatim(
    fixture_text: str, fixture_lines: list[CronLine]
) -> None:
    """逐字保留是「只删行、不改写」的地基：拼回去只能差末尾那个换行。"""
    joined = "\n".join(ln.raw for ln in fixture_lines)
    assert fixture_text.startswith(joined)
    assert len(fixture_text) - len(joined) <= 1


def test_paused_job_reads_both_note_styles_and_ignores_prose() -> None:
    half = _line(
        "# [2026-09-11 用户要求:暂停] 35 10 * * 1-5 /x/.venv/bin/python /x/a.py"
    )
    full = _line("#【A股优先·暂停】0 11-17 * * 1-5 /x/.venv/bin/python /x/b.py")
    prose = _line("# 注意 记录 每天 都要 手工 检查")
    plain = _line("# 普通注释，没有任何作业")
    assert paused_job(half) == JobKey("a.py")
    assert paused_job(full) == JobKey("b.py")
    assert paused_job(prose) is None  # 结构就不是作业
    assert paused_job(plain) is None
    assert paused_job(_line("*/5 * * * * /bin/echo x")) is None  # 真作业行不算停用


def test_paused_job_needs_the_command_to_look_like_a_call() -> None:
    """散文也可能**结构上**像作业：前五段恰好是合法字段。

    拦住它的是第二条判据「命令段得像调用」。缺了这条，`# 每天 30 1 * * 2-6 跑影子账`
    这种**记录调度的话**会被报成「被停用的作业」——报告里凭空多出一条，读的人以为有东西
    被停了、要去把它恢复起来（正是这条报告存在的目的的反面）。
    """
    scheduled = _line("# 每天 30 1 * * 2-6 跑影子账，见迁移计划 P4 补")
    assert paused_job(scheduled) is None
    # 五段合法 + 命令像调用 ⇒ 才认（正对照，别把判据写死成「永不返回」）
    assert paused_job(_line("# 30 1 * * 2-6 /x/.venv/bin/python /x/c.py")) == JobKey("c.py")


def test_fixture_is_the_real_live_table(
    fixture_text: str, fixture_lines: list[CronLine]
) -> None:
    """快照级守卫：改了快照就必须重新裁决分类表，所以这里把 sha256 与计数一起锁死。"""
    sha = hashlib.sha256(fixture_text.encode("utf-8")).hexdigest()
    assert sha == _FIXTURE_SHA, (
        "真表快照被改过：分类表的 49 条裁决是按这份快照逐条判的，"
        "换快照必须回 backend/shared/legacy_cron_plan.py 重新裁决（并更新本 sha）。"
    )
    kinds: dict[str, int] = {}
    for ln in fixture_lines:
        kinds[ln.kind] = kinds.get(ln.kind, 0) + 1
    assert kinds == {"job": 59, "comment": 14, "blank": 3}
    assert len({ln.job for ln in fixture_lines if ln.is_job}) == 49
    assert kinds.get("unknown", 0) == 0


# ── ② 机制层：开关口径、证据分级、构建 ───────────────────────────────────────
@pytest.mark.parametrize(
    ("raw", "expect"),
    [
        ("true", "on"),
        ("TRUE", "on"),
        (" true ", "on"),
        ("1", "unknown"),  # 严词表（env_flags）判关、宽词表判开 ⇒ 分叉
        ("yes", "unknown"),
        ("on", "unknown"),
        ("enabled", "unknown"),
        ("false", "off"),
        ("0", "off"),
        ("off", "off"),
        ("", "unknown"),
        (None, "unknown"),
        ("随便什么", "unknown"),
    ],
)
def test_parse_env_flag_never_overclaims_on(raw: str | None, expect: str) -> None:
    assert parse_env_flag(raw) == expect


def test_tool_never_claims_on_where_the_two_real_readers_disagree(monkeypatch) -> None:
    """对着**两个真读者**跑一遍分叉矩阵（不是复述词表）。

    严 = ``shared/env_flags.env_flag``（合规咽喉，只认 ``true``）；宽 = 注册表
    ``switch_enabled``（``1``/``yes``/``on`` 也认）。两边不一致时工具必须 ``unknown``：
    ``QM_DECISION_ROUND_ENABLED`` 恰好走严读法，谎报一个 √ 就是「隔壁删了、本仓没开」。
    """
    from backend.shared.env_flags import env_flag
    from backend.shared.scheduler_registry import JOBS_BY_KEY, switch_enabled

    spec = JOBS_BY_KEY["decision_round"]
    checked = 0
    for raw in ("true", "TRUE", "1", "yes", "on", "enabled", "false", "0", "off"):
        monkeypatch.setenv(spec.switch_env, raw)
        strict, wide = (
            env_flag(spec.switch_env),
            switch_enabled(spec, {spec.switch_env: raw}),
        )
        tool = parse_env_flag(raw)
        if strict and wide:
            assert tool == "on", raw
        elif not strict and not wide:
            assert tool == "off", raw
        else:
            assert tool == "unknown", (
                f"{raw!r}: 严={strict} 宽={wide} 分叉，工具不许下结论"
            )
        checked += 1
    assert checked == 9


@dataclass(frozen=True)
class _FakeEntry:
    covered_by: str = ""
    disposition: str = "qm_covered"
    manual_checks: tuple[str, ...] = ()


def _evidence_for(entry: _FakeEntry, *, env: dict[str, str] | None = None) -> Evidence:
    from backend.shared.scheduler_registry import JOBS_BY_KEY

    return check_evidence(
        {JobKey("neighbor.py"): entry},
        jobs_by_key=JOBS_BY_KEY,
        env=env or {},
        code_probe=lambda path, symbol: True,
    )


def test_registry_probe_reports_switch_state_and_marks_off_as_risky() -> None:
    ev = _evidence_for(_FakeEntry(covered_by="registry:decision_round"))
    assert ev.refusals == ()
    (cov,) = ev.coverages
    assert cov.kind == "registry" and cov.exists and cov.risky
    assert "未设置，注册表默认 关" in cov.detail
    assert len(ev.manual_checks) == 1

    ev_on = _evidence_for(_FakeEntry(covered_by="registry:decision_round"), env={})
    assert ev_on.coverages[0].risky  # 默认关：没给 env 时同样要人看


def test_registry_probe_marks_a_running_job_as_clean() -> None:
    ev = _evidence_for(
        _FakeEntry(covered_by="registry:risk_tier"),
        env={"QM_RISK_TIER_ENABLED": "true"},
    )
    (cov,) = ev.coverages
    assert not cov.risky
    assert ev.manual_checks == ()


def test_registry_probe_refuses_a_key_that_is_not_in_the_registry() -> None:
    ev = _evidence_for(_FakeEntry(covered_by="registry:no_such_job"))
    assert ev.coverages == ()
    assert len(ev.refusals) == 1 and "no_such_job" in ev.refusals[0]


def test_code_evidence_is_always_risky_even_when_the_probe_finds_it() -> None:
    """代码级证据只能证明「接手方在仓里」，证不了「它在跑」——永远不许给 √。"""
    ev = _evidence_for(
        _FakeEntry(
            covered_by="code:backend/services/live_trading/services/sltp_executor.py:run_qmt_sltp_executor_task"
        )
    )
    (cov,) = ev.coverages
    assert cov.kind == "code" and cov.exists and cov.risky
    assert len(ev.manual_checks) == 1 and "判不了它此刻是否在跑" in ev.manual_checks[0]


def test_code_evidence_refuses_a_symbol_that_is_not_there() -> None:
    ev = check_evidence(
        {
            JobKey("neighbor.py"): _FakeEntry(
                covered_by="code:backend/shared/legacy_cron.py:definitely_not_defined"
            )
        },
        jobs_by_key={},
        env={},
        code_probe=lambda path, symbol: False,
    )
    assert ev.coverages == ()
    assert len(ev.refusals) == 1


def test_code_evidence_refuses_an_unrecognised_evidence_form() -> None:
    """两种写坏法都要拒：①压根没给证据 ②给了 ``code:`` 但缺符号。"""
    none_given = _evidence_for(_FakeEntry(covered_by="看代码里那个函数"))
    assert none_given.coverages == ()
    assert len(none_given.refusals) == 1 and "没给证据" in none_given.refusals[0]

    no_symbol = _evidence_for(
        _FakeEntry(covered_by="code:backend/shared/legacy_cron.py")
    )
    assert no_symbol.coverages == ()
    assert len(no_symbol.refusals) == 1 and "证据写法认不出" in no_symbol.refusals[0]


def test_qm_covered_without_evidence_is_a_refusal_not_a_warning() -> None:
    ev = _evidence_for(_FakeEntry(covered_by=""))
    assert ev.refusals and "没给证据" in ev.refusals[0]


def test_code_probe_uses_ast_not_grep(tmp_path: Path, monkeypatch) -> None:
    """注释里提一句、字符串里写一次都不算「接手方在那儿」。"""
    mod = tmp_path / "probe_target.py"
    mod.write_text(
        "# run_qmt_sltp_executor_task 只是被提了一句\n"
        'DOC = "run_qmt_sltp_executor_task"\n'
        "TOKEN = 1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(migrate, "PROJECT_ROOT", tmp_path)
    assert migrate._code_probe("probe_target.py", "run_qmt_sltp_executor_task") is False
    assert migrate._code_probe("probe_target.py", "TOKEN") is True  # 模块级赋值算定义
    mod.write_text("def run_qmt_sltp_executor_task():\n    pass\n", encoding="utf-8")
    assert migrate._code_probe("probe_target.py", "run_qmt_sltp_executor_task") is True
    assert migrate._code_probe("nope.py", "run_qmt_sltp_executor_task") is False


# ── 构建：只删可删的、未动的逐字保留、新增段二次运行安全 ──────────────────────
_REMOVABLE_KEY = JobKey("live_price_watch.py")
_KEEP_KEY = JobKey("night_pool_agent.py")


def _plan(*entries: Entry) -> dict[JobKey, Entry]:
    return {e.job: e for e in entries}


def test_build_target_drops_only_removable_and_keeps_the_rest_verbatim() -> None:
    plan = _plan(
        Entry("live_price_watch.py", "qm_covered", covered_by="code:x.py:y"),
        Entry("night_pool_agent.py", "must_keep", why="供数"),
    )
    text = (
        "# 表头注释\n"
        "SHELL=/bin/bash\n"
        "*/1 * * * * /usr/bin/python3 /x/live_price_watch.py >> /x/l.log 2>&1  # 守护\n"
        "0 3 * * * /usr/bin/python3 /x/night_pool_agent.py\n"
    )
    lines = parse_crontab(text)
    target = build_target(lines, plan=plan)
    assert target.ok and target.dropped == (lines[2],) and len(target.kept) == 3
    assert target.lines == (lines[0].raw, lines[1].raw, lines[3].raw)  # 逐字保留
    assert lines[2].raw not in target.text
    assert ">> /x/l.log 2>&1  # 守护" not in target.text


def test_build_target_refuses_unknown_and_unclassified_lines() -> None:
    plan = _plan(Entry("night_pool_agent.py", "must_keep"))
    lines = parse_crontab(
        "*/5 * * * * /x/mystery_job.py\n"  # 没分类
        "*/5 * * foo\n"  # 认不出
    )
    target = build_target(lines, plan=plan)
    assert not target.ok and len(target.refusals) == 2
    assert target.dropped == ()


_KEEP_PLAN = _plan(Entry("keep.py", "must_keep", why="本仓作业，留"))
_KEEP_LINE = "0 3 * * * /usr/bin/python3 /x/keep.py\n"


def test_build_target_additions_land_under_the_header() -> None:
    add = "45 0 * * 2-6 /usr/bin/docker exec -w /app quantmind python3 /app/x.py run"
    target = build_target(parse_crontab(_KEEP_LINE), plan=_KEEP_PLAN, additions=(add,))
    assert target.ok and target.added == (add,)
    assert ADDED_HEADER in target.lines
    assert target.lines.index(ADDED_HEADER) < target.lines.index(add)


def test_build_target_additions_are_idempotent_on_the_second_run() -> None:
    """二次运行安全是硬要求：工具跑过一次之后不能再被自己的新增行拒掉。"""
    add = "45 0 * * 2-6 /usr/bin/docker exec -w /app quantmind python3 /app/x.py run"
    first = build_target(parse_crontab(_KEEP_LINE), plan=_KEEP_PLAN, additions=(add,))
    second = build_target(parse_crontab(first.text), plan=_KEEP_PLAN, additions=(add,))
    assert second.ok and second.added == () and second.dropped == ()
    assert second.text == first.text
    # 认的是**身份**不是字面：日志落点/时刻改了也不该重复追加
    moved = add.replace("/app/x.py run", "/app/x.py run >> /a/b.log 2>&1")
    third = build_target(parse_crontab(first.text), plan=_KEEP_PLAN, additions=(moved,))
    assert third.added == () and third.refusals == () and third.text == first.text


def test_build_target_appends_only_the_missing_additions_and_warns() -> None:
    a1 = "45 0 * * 2-6 /usr/bin/docker exec -w /app quantmind python3 /app/x.py extract --apply"
    a2 = "50 0 * * 2-6 /usr/bin/docker exec -w /app quantmind python3 /app/x.py price --apply"
    # 走真的二次运行路径：先跑一次装进 a1（表头随之写进表里），再引入 a2 复跑
    first = build_target(parse_crontab(_KEEP_LINE), plan=_KEEP_PLAN, additions=(a1,))
    assert ADDED_HEADER in first.lines
    partial = build_target(
        parse_crontab(first.text),
        plan=_KEEP_PLAN,
        additions=(a1, a2),
    )
    assert partial.ok and partial.added == (a2,)
    assert len(partial.warnings) == 1 and "表头不重复写" in partial.warnings[0]
    assert partial.lines.count(ADDED_HEADER) == 1  # 表头不重复


@pytest.mark.parametrize("bad", ["这是一句说明，不是 cron 行", "*/5 * * foo", ""])
def test_build_target_refuses_an_addition_that_cannot_be_parsed(bad: str) -> None:
    target = build_target(parse_crontab(_KEEP_LINE), plan=_KEEP_PLAN, additions=(bad,))
    assert (target.refusals == ()) == (bad == "")  # 空串只是被跳过，不算声明
    assert target.added == ()


# ── ③ 分类表：活表 49 ↔ 表 49，裁决分布即结论 ────────────────────────────────
def test_plan_covers_exactly_the_live_table_identities(
    fixture_lines: list[CronLine],
) -> None:
    """头号不变量：**多一个少一个都不行**。

    少了 ⇒ 工具拒绝执行（有没分类的野作业）；多了 ⇒ 表里躺着一条活表上没有的裁决，
    说明快照与表已经脱节（多半是有人改了快照而没重新裁决）。
    """
    live = {ln.job for ln in fixture_lines if ln.is_job and ln.job is not None}
    assert len(live) == 49 and len(PLAN_BY_KEY) == 49
    assert live - set(PLAN_BY_KEY) == set(), "活表上有作业没判过归属"
    assert set(PLAN_BY_KEY) - live == set(), "表里有裁决指向活表上不存在的作业"


def test_plan_disposition_distribution_is_the_adjudicated_verdict() -> None:
    """2026-09-24 的裁决结果。**改动必须过这条测试** —— 它就是把结论写死的地方。

    删 24 个身份（本仓已接手 8 + 随隔壁停 16）= 28 行；留 25 个身份。
    留的比删的多是正常的：本仓决策上下文现在还挂在隔壁的池/新闻/排除名单上。
    """
    counts: dict[str, int] = {}
    for e in PLAN:
        counts[e.disposition] = counts.get(e.disposition, 0) + 1
    assert counts == {
        "qm_covered": 8,
        "retire_stop": 16,
        "must_keep": 7,
        "migrate": 3,
        "keep_as_is": 4,
        "pending_decision": 11,
    }
    assert sum(counts.values()) == 49
    assert sum(v for k, v in counts.items() if k in REMOVABLE) == 24
    assert set(counts) <= DISPOSITIONS


def test_plan_validator_rejects_a_bad_table() -> None:
    from backend.shared.legacy_cron_plan import _validate

    good = Entry("a.py", "must_keep", why="x")
    cases = {
        "未知处置": (Entry("a.py", "也许该删"), "未知处置"),
        "可删的没写 why": (Entry("a.py", "retire_stop"), "必须写 why"),
        # qm_covered 属可删类 ⇒ 「必须写 why」先炸；这里补上 why 才走到「必须给证据」
        "接手了却没证据": (
            Entry("a.py", "qm_covered", why="口头说接手了"),
            "必须给证据",
        ),
        "没接手却写证据": (
            Entry("a.py", "must_keep", covered_by="registry:x"),
            "只有 qm_covered",
        ),
        "身份重复": (good, "身份重复"),
    }
    for name, (bad, needle) in cases.items():
        with pytest.raises(ValueError, match=needle):
            _validate((good, bad) if name != "身份重复" else (good, good))


def test_every_removable_entry_says_why_and_where_needed_gives_evidence() -> None:
    removable = [e for e in PLAN if e.disposition in REMOVABLE]
    assert len(removable) == 24
    assert all(e.why.strip() for e in removable)
    assert all(e.covered_by for e in removable if e.disposition == "qm_covered"), (
        "qm_covered 条目必须给证据（否则工具会拒整轮）"
    )


def test_additions_do_not_collide_with_the_plan_and_are_unique() -> None:
    assert len(ADDITIONS) == 3
    keys = []
    for raw in ADDITIONS:
        parsed = [ln for ln in parse_crontab(raw) if ln.is_job]
        assert len(parsed) == 1 and parsed[0].job is not None, raw
        keys.append(parsed[0].job)
    assert len(set(keys)) == 3, "三条新增必须互相不同（否则按身份去重会吃掉一条）"
    assert not (set(keys) & set(PLAN_BY_KEY)), "新增行与分类表撞身份 = 语义自相矛盾"


def test_additions_keep_the_documented_schedule_and_entrypoint() -> None:
    """新增段的理由写在 plan 模块头（为什么走容器、为什么排在 postmarket 之后）。

    这里把它锁成断言：换调度、换入口、少一条子命令都要显式改测试。
    """
    jobs = []
    for raw in ADDITIONS:
        (ln,) = [x for x in parse_crontab(raw) if x.is_job]
        jobs.append(ln)
    assert [ln.schedule for ln in jobs] == [
        "45 0 * * 2-6",
        "50 0 * * 2-6",
        "55 0 * * 2-6",
    ]
    script = "/app/backend/scripts/risk_ghost_ledger.py"
    for ln, sub in zip(jobs, ("extract", "price", "report"), strict=True):
        args = ln.job.args
        assert script in args, ln.job
        assert args[args.index(script) + 1] == sub
        assert ln.command.startswith("/usr/bin/docker exec -w /app quantmind python3")
        assert (
            ">> /home/zbox/projects/quantmind/logs/risk_ghost_ledger.log 2>&1" in ln.raw
        )


# ── ④ CLI（真表路径全部走桩；本文件绝不碰线上 crontab）────────────────────────
def _patch_live(monkeypatch, live_text: str) -> dict[str, str]:
    """把 CLI 的真表读写全部换成内存桩，返回可观测的 state。"""
    state = {"text": live_text, "installed": None, "dry": None, "calls": ""}

    def fake_read(_bin: str) -> str:
        state["calls"] += "read;"
        return state["installed"] if state["installed"] is not None else state["text"]

    def fake_dry(_bin: str, text: str):
        state["calls"] += "dry;"
        state["dry"] = text
        return None

    def fake_install(_bin: str, text: str) -> None:
        state["calls"] += "install;"
        state["installed"] = text

    monkeypatch.setattr(migrate, "_crontab_bin", lambda: "/usr/bin/crontab")
    monkeypatch.setattr(migrate, "_read_live", fake_read)
    monkeypatch.setattr(migrate, "_native_dry_run", fake_dry)
    monkeypatch.setattr(migrate, "_install", fake_install)
    return state


def _clean_evidence() -> Evidence:
    return Evidence(
        coverages=(
            Coverage(
                job=_REMOVABLE_KEY,
                kind="registry",
                evidence="decision_round",
                exists=True,
                detail="决策轮（每 5 分钟）；QM_DECISION_ROUND_ENABLED='true'",
            ),
        )
    )


_LIVE_TWO = (
    "# 隔壁表\n"
    "*/1 * * * * /usr/bin/python3 /x/live_price_watch.py >> /x/l.log 2>&1\n"
    "0 3 * * * /usr/bin/python3 /x/night_pool_agent.py\n"
)


def test_cli_offline_preview_reports_counts_and_exits_attention(
    fixture_text: str, capsys
) -> None:
    """离线预演走**真实现**（不碰真表）：退 1 且把该念的都念出来。"""
    code = migrate.main(["--file", str(_FIXTURE)])
    out = capsys.readouterr().out
    assert code == migrate.EXIT_ATTENTION
    assert "行数 76 · 作业行 59 · 身份 49" in out
    assert "本仓已接手（删） 8" in out and "随隔壁停（删） 16" in out
    assert "--apply" in out  # 退 1 的收尾提示
    assert "停用行 3 条" in out


def test_cli_offline_target_is_short_the_removable_only(
    fixture_text: str, capsys
) -> None:
    code = migrate.main(["--file", str(_FIXTURE), "--print-target"])
    out = capsys.readouterr().out
    assert code == migrate.EXIT_ATTENTION
    body = out.split("── 目标 crontab 全文")[1]
    target = parse_crontab(body)
    jobs = [ln for ln in target if ln.is_job]
    assert len(jobs) == 59 - 28 + 3  # 原作业行 − 删 28 + 新增 3
    assert all(
        PLAN_BY_KEY[ln.job].disposition not in REMOVABLE
        for ln in jobs
        if ln.job in PLAN_BY_KEY
    )


@pytest.mark.parametrize(
    ("argv", "needle"),
    [
        (
            ["--apply", "--file", "x", "--record", "/tmp/x"],
            "--apply 不能与 --file 同用",
        ),
        (["--apply"], "--record"),
        (["--verify"], "--record"),
    ],
)
def test_cli_usage_guards(argv: list[str], needle: str, capsys) -> None:
    assert migrate.main(argv) == migrate.EXIT_USAGE
    assert needle in capsys.readouterr().err


def test_cli_refuses_to_run_without_a_crontab_binary(monkeypatch, capsys) -> None:
    """容器里没有 crontab：必须拒绝，而不是「读不到活表」当成空表继续。"""
    monkeypatch.setattr(migrate.shutil, "which", lambda _name: None)
    assert migrate.main([]) == migrate.EXIT_USAGE
    assert "找不到 `crontab` 命令" in capsys.readouterr().err


def test_cli_apply_stops_on_refusals_and_installs_nothing(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    state = _patch_live(monkeypatch, _LIVE_TWO + "*/5 * * * * /x/mystery.py\n")
    monkeypatch.setattr(migrate, "build_evidence", _clean_evidence)
    code = migrate.main(["--apply", "--record", str(tmp_path / "rec")])
    assert code == migrate.EXIT_USAGE
    assert state["installed"] is None and "install" not in state["calls"]
    assert "拒绝项" in capsys.readouterr().out
    assert not (tmp_path / "rec").exists()  # 存档也不写


def test_cli_apply_requires_accept_risks_when_a_coverer_may_not_be_running(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    state = _patch_live(monkeypatch, _LIVE_TWO)
    risky = Evidence(
        coverages=(
            Coverage(
                job=_REMOVABLE_KEY,
                kind="code",
                evidence="x.py:run",
                exists=True,
                risky=True,
            ),
        ),
        manual_checks=("接手方只到代码级",),
    )
    monkeypatch.setattr(migrate, "build_evidence", lambda: risky)
    code = migrate.main(["--apply", "--record", str(tmp_path / "rec")])
    assert code == migrate.EXIT_USAGE
    assert state["installed"] is None
    err = capsys.readouterr().err
    assert "--accept-risks" in err and "静默裸奔" in err


def test_cli_apply_archives_dry_runs_then_installs_and_reads_back(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    rec = tmp_path / "rec"
    state = _patch_live(monkeypatch, _LIVE_TWO)
    monkeypatch.setattr(migrate, "build_evidence", _clean_evidence)
    code = migrate.main(["--apply", "--record", str(rec)])
    out = capsys.readouterr().out
    assert code == migrate.EXIT_OK, out
    # 顺序：先干跑、再落存档、最后才装
    assert state["calls"].index("dry") < state["calls"].index("install")
    assert state["dry"] is not None and "live_price_watch.py" not in state["dry"]
    assert state["installed"] == state["dry"]
    for name in ("before.crontab", "after.crontab", "report.json"):
        assert (rec / name).is_file(), name
    assert (rec / "before.crontab").read_text(encoding="utf-8") == _LIVE_TWO
    report = (rec / "report.json").read_text(encoding="utf-8")
    assert '"action": "apply"' in report and '"dropped"' in report
    assert "night_pool_agent.py" in report  # 留的那条进了 kept_lines
    assert "已安装并回读校验通过" in out


def test_cli_apply_is_a_noop_when_the_live_table_already_matches(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    target_text = build_target(
        parse_crontab(_LIVE_TWO), plan=PLAN_BY_KEY, additions=ADDITIONS
    ).text
    state = _patch_live(monkeypatch, target_text)
    monkeypatch.setattr(migrate, "build_evidence", _clean_evidence)
    code = migrate.main(["--apply", "--record", str(tmp_path / "rec")])
    assert code == migrate.EXIT_OK
    assert "already" not in state["calls"] and "install" not in state["calls"]
    assert "无需变更" in capsys.readouterr().out


def test_cli_apply_refuses_a_record_dir_inside_the_repo(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _patch_live(monkeypatch, _LIVE_TWO)
    monkeypatch.setattr(migrate, "build_evidence", _clean_evidence)
    inside = (
        Path(__file__).resolve().parents[2]
        / "backend"
        / "tests"
        / ".p4_should_never_exist"
    )
    code = migrate.main(["--apply", "--record", str(inside)])
    assert code == migrate.EXIT_USAGE
    assert "存档落点不合规" in capsys.readouterr().err
    assert not inside.exists()


def test_cli_apply_stops_when_the_native_dry_run_fails(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    state = _patch_live(monkeypatch, _LIVE_TWO)
    monkeypatch.setattr(migrate, "_native_dry_run", lambda _b, _t: "bad minute")
    monkeypatch.setattr(migrate, "build_evidence", _clean_evidence)
    code = migrate.main(["--apply", "--record", str(tmp_path / "rec")])
    assert code == migrate.EXIT_USAGE
    assert state["installed"] is None
    assert "没有安装任何东西" in capsys.readouterr().err


def test_cli_apply_stops_on_readback_mismatch_and_prints_the_rollback(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    rec = tmp_path / "rec"
    state = _patch_live(monkeypatch, _LIVE_TWO)
    monkeypatch.setattr(migrate, "build_evidence", _clean_evidence)

    def lying_install(_bin: str, text: str) -> None:
        state["installed"] = text.replace(
            "night_pool_agent.py", "night_pool_agent.py # 被谁动过"
        )

    monkeypatch.setattr(migrate, "_install", lying_install)
    code = migrate.main(["--apply", "--record", str(rec)])
    err = capsys.readouterr().err
    assert code == migrate.EXIT_USAGE
    assert "回读不一致" in err and f"回滚: crontab {rec / 'before.crontab'}" in err


def test_cli_verify_passes_on_a_matching_table_and_flags_drift(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    rec = tmp_path / "rec"
    rec.mkdir()
    target_text = build_target(
        parse_crontab(_LIVE_TWO), plan=PLAN_BY_KEY, additions=ADDITIONS
    ).text
    (rec / "after.crontab").write_text(target_text, encoding="utf-8")
    _patch_live(monkeypatch, target_text)
    monkeypatch.setattr(migrate, "build_evidence", _clean_evidence)
    assert migrate.main(["--verify", "--record", str(rec)]) == migrate.EXIT_OK
    assert "一致（逐行归一比对通过）" in capsys.readouterr().out

    _patch_live(monkeypatch, target_text + "*/5 * * * * /x/someone_added_this.py\n")
    monkeypatch.setattr(migrate, "build_evidence", _clean_evidence)
    code = migrate.main(["--verify", "--record", str(rec)])
    out = capsys.readouterr().out
    assert code == migrate.EXIT_ATTENTION
    assert "漂移" in out and "someone_added_this.py" in out


def test_cli_verify_refuses_a_record_without_after_crontab(
    tmp_path: Path, capsys
) -> None:
    assert migrate.main(["--verify", "--record", str(tmp_path)]) == migrate.EXIT_USAGE
    assert "没有 after.crontab" in capsys.readouterr().err


def _fake_crontab(tmp_path: Path) -> tuple[Path, Path]:
    """一个满足 CLI 契约的假 ``crontab``：``-l`` 读、``-n`` 干跑、``<文件>`` 装。

    用它把 ``_read_live``/``_native_dry_run``/``_write_temp``/``_install`` 全走**真子进程**，
    同时又一根手指都不碰真表（真表路径在容器里本就拒绝执行）。
    """
    state = tmp_path / "state.crontab"
    fake = tmp_path / "fake_crontab.py"
    fake.write_text(
        f"""#!{sys.executable}
import shutil, sys
from pathlib import Path
STATE = Path({str(state)!r})
a = sys.argv[1:]
if a[:1] == ["-l"]:
    if not STATE.exists():
        print("no crontab for zbox", file=sys.stderr)
        sys.exit(1)
    sys.stdout.write(STATE.read_text())
elif a[:1] == ["-n"]:
    if "bad minute" in Path(a[1]).read_text():
        print("errors in crontab file, can't install.", file=sys.stderr)
        sys.exit(1)
    print("The syntax of the crontab file was successfully checked.")
else:
    shutil.copyfile(a[0], STATE)
""",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return fake, state


def test_cli_apply_end_to_end_through_real_subprocesses(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """真子进程端到端：读活表 → 干跑 → 存档 → 安装 → 回读校验，再复验、再幂等。"""
    fake, state = _fake_crontab(tmp_path)
    state.write_text(_LIVE_TWO, encoding="utf-8")
    monkeypatch.setattr(migrate, "_crontab_bin", lambda: str(fake))
    monkeypatch.setattr(migrate, "build_evidence", _clean_evidence)
    rec = tmp_path / "rec"

    code = migrate.main(["--apply", "--record", str(rec)])
    out = capsys.readouterr().out
    assert code == migrate.EXIT_OK, out
    assert "已安装并回读校验通过" in out
    installed = state.read_text(encoding="utf-8")
    assert "live_price_watch.py" not in installed
    assert "night_pool_agent.py" in installed
    assert ADDED_HEADER in installed
    assert (rec / "before.crontab").read_text(encoding="utf-8") == _LIVE_TWO
    assert (rec / "after.crontab").read_text(encoding="utf-8") == installed

    # 复验：此刻活表 == 存档目标表
    assert migrate.main(["--verify", "--record", str(rec)]) == migrate.EXIT_OK
    assert "一致（逐行归一比对通过）" in capsys.readouterr().out

    # 再跑一次 apply：活表已是目标表 ⇒ 无事可做（幂等）
    assert migrate.main(["--apply", "--record", str(rec)]) == migrate.EXIT_OK
    assert "无需变更" in capsys.readouterr().out


def test_cli_apply_refuses_an_empty_live_table(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """假 crontab 报「no crontab」= 空表：异常态，必须拒绝而不是把整张表写空。"""
    fake, state = _fake_crontab(tmp_path)
    assert not state.exists()
    monkeypatch.setattr(migrate, "_crontab_bin", lambda: str(fake))
    monkeypatch.setattr(migrate, "build_evidence", _clean_evidence)
    code = migrate.main(["--apply", "--record", str(tmp_path / "rec")])
    assert code == migrate.EXIT_USAGE
    assert "没有任何作业行" in capsys.readouterr().err
    assert not state.exists()


@pytest.mark.skipif(
    shutil.which("crontab") is None, reason="容器里没有 crontab（工具本就只在宿主跑）"
)
def test_native_dry_run_accepts_the_fixture_and_rejects_broken_text_on_the_host(
    fixture_text: str,
) -> None:
    """原生干跑（``crontab -n``，不安装）：真表快照必须通过，坏文本必须被拒。"""
    bin_path = migrate._crontab_bin()
    assert migrate._native_dry_run(bin_path, fixture_text) is None
    assert migrate._native_dry_run(bin_path, "99 99 * * * /bin/echo bad\n") is not None
