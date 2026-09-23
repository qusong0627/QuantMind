"""P5 步骤 3：隔壁 ``live_watch.json`` → 本仓守护规则表（迁移工具）。

迁移只有一次窗口，而它的失效形态是**「看起来搬完了」**（没有任何信号）。所以这个
文件盯的不是「函数返回了什么」，而是四件各自会让操作员读错现场的事：

1. **预演 = 真写**（差分用例）：预演（``legacy_watch.simulate_apply``）与真写一遍
   （``watch_writer.write_watch_plan``，逐个 agent 顺序落库）必须给出同一份结论——
   逐家比 ``armed``、比冲突标的、比**落库字典**。操作员是照着预演按确认的，两者
   分叉等于「印出来的报告与真发生的事不是一回事」；
2. **空集不写**：某家本轮无规则时整组保留。真写一遍空计划会**把该家已挂的守则全清**
   （下面有专门一条用例把这件事演出来，作为闸门存在的理由）；
3. **读不到 ≠ 空表**：源文件读不到必须是环境错误，绝不能退化成「每家都没有规则 ⇒
   什么都不写 ⇒ 退出码 0」这种在切换窗口里**与成功一模一样**的结果；
4. **名册闸**：名册外的 agent 写下去就是一组没人认领的规则（既不会被撤也不会跟着
   新分析走），比没挂上更坏——它看起来有保护。

不碰真 Redis（复用 ``test_watch_writer`` 的替身：它就是钉 ``write_watch_plan`` 行为
的那一份），不读真隔壁文件（真源天天在变，实跑留在切换日由 CLI 做并留档）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from backend.scripts import migrate_legacy_watch as cli
from backend.services.live_trading.services import sltp_executor as ex
from backend.shared.decision import legacy_watch as lw
from backend.shared.decision.watch_map import (
    REJECT_DUPLICATE,
    REJECT_NO_LEVEL,
    REJECT_PCT_ZERO,
    WatchPlan,
)
from backend.shared.decision import watch_writer as ww
from backend.shared.decision.watch_writer import write_watch_plan
from backend.tests.test_migrate_legacy_assets import remove_repo_residue
from backend.tests.test_watch_writer import (
    FakeRedis,
    cfg,
    rule,
    symbols_in,
    table,
)

GLM = "GLM-4.6"
TA = "TradingAgents-Deepseek"


# ---------------------------------------------------------------------------
# 夹具：形状照隔壁实文件（后缀式代码 + 哨兵边车键 + null 价位）
# ---------------------------------------------------------------------------
def neighbor_doc() -> dict[str, Any]:
    """一份三家的假文件：两家 agent + 人工/别家各占一个槽位的故事。

    * ``GLM-4.6`` 与 ``TradingAgents-Deepseek`` **都挂了 600036.SH**（实测有这种：隔壁
      台账里 600276.SH 就是两家各持一半）→ 排序在前的那家拿到槽位；
    * ``510300.SH`` 明说 ``pct=0``（不表达卖出量）→ 不挂；
    * ``600036.SH`` 那条混着哨兵自己的边车键（``_skip_notified`` / ``stop_from_move``）
      → 必须忽略（那是本仓哨兵要自己重建的状态，照抄等于两套状态机混一张表）。
    """
    return {
        TA: [
            {
                "code": "002074.SZ",
                "stop_loss": 44.6,
                "take_profit": None,
                "move_stop": None,
                "pct": 1.0,
                "reason": "跌破前低就走",
                "created_ts": "2026-09-23T09:12:03+08:00",
            },
            {
                "code": "600036.SH",
                "stop_loss": 30.0,
                "take_profit": 45.0,
                "move_stop": 42.0,
                "pct": 0.5,
                "reason": "上触 42 把防守抬到 42",
                "created_ts": "2026-09-23T09:12:04+08:00",
                "_skip_notified": True,
                "stop_from_move": 41.8,
            },
        ],
        GLM: [
            {
                "code": "600036.SH",
                "stop_loss": 31.0,
                "take_profit": None,
                "move_stop": None,
                "pct": 1.0,
                "reason": "同一标的，另一家的看法",
                "created_ts": "2026-09-23T09:12:05+08:00",
            },
            {
                "code": "510300.SH",
                "stop_loss": 3.8,
                "take_profit": None,
                "move_stop": None,
                "pct": 0.0,
                "reason": "明说不减仓",
                "created_ts": "2026-09-23T09:12:06+08:00",
            },
        ],
    }


def seed_table() -> list[dict[str, Any]]:
    """规则表的既存状态：一条人工、一条别家、一条「我自己的旧规则」。"""
    return [
        rule("600000.SH", owner=""),
        rule("000001.SZ", owner=ex.llm_owner("另一个模型")),
        rule("002074.SZ", owner=ex.llm_owner(TA)),
    ]


WITH_RULES = (GLM, TA)


@pytest.fixture
def redis() -> FakeRedis:
    return FakeRedis(cfg(*seed_table()))


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, redis: FakeRedis) -> FakeRedis:
    """CLI 的替身接线：Redis 走假货，名册走测试名单。"""
    monkeypatch.setattr(cli, "_connect", lambda: redis)
    monkeypatch.setattr(cli, "_roster_agents", lambda: (WITH_RULES, "测试名册 2 家"))
    return redis


def write_src(tmp_path: Path, doc: Any) -> Path:
    path = tmp_path / "live_watch.json"
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 读入层：容错但**不静默**
# ---------------------------------------------------------------------------
def test_parses_two_agents_in_name_order_with_all_fields() -> None:
    watch = lw.parse_legacy_watch(neighbor_doc())

    assert watch.ok
    assert watch.agents() == (GLM, TA)  # 按名字排序（与插入顺序无关）
    assert watch.source_rules == 4
    assert watch.problems == ()

    first = watch.groups[0].rules[0]
    assert (first.agent, first.code, first.index) == (GLM, "600036.SH", 0)
    assert first.decision.stop_loss == 31.0
    assert first.created_ts == "2026-09-23T09:12:05+08:00"
    assert first.reason == "同一标的，另一家的看法"  # 证据链只在这里（表里没有 reason）


def test_group_order_is_stable_regardless_of_json_key_order() -> None:
    """顺序必须确定：操作员是照着预演按确认的，两次跑出不同归属就没法确认。"""
    doc = neighbor_doc()
    shuffled = {k: doc[k] for k in reversed(list(doc))}
    assert (
        lw.parse_legacy_watch(doc).agents() == lw.parse_legacy_watch(shuffled).agents()
    )


def test_sentinel_sidecar_keys_are_ignored() -> None:
    watch = lw.parse_legacy_watch(neighbor_doc())
    plans = {p.agent: p for p in lw.plan_groups(watch)}
    armed = {r.symbol: r for r in plans[TA].rules}

    got = armed["600036.SH"].rule
    assert set(got) == set(ex.DEFAULT_RULE)  # 词表之外一个都不带
    assert got["move_stop_trigger"] == 42.0
    assert got["move_stop_to"] == 42.0  # 单值 = 零间隙棘轮
    assert got["reduce_pct"] == 0.5


def test_agent_names_are_normalized_like_every_other_identity() -> None:
    """归属口径必须与决策轮一致（``normalize_agent``），否则那组规则没人认领。"""
    from backend.shared.order_contract import normalize_agent

    long_name = "M" * 70  # 超过 AGENT_LEN(64)：活路径会截断，迁移必须截成同一个身份
    watch = lw.parse_legacy_watch({f"  {GLM}  ": [], long_name: []})

    assert watch.agents() == (GLM, normalize_agent(long_name))
    assert len(watch.agents()[1]) == 64


@pytest.mark.parametrize("doc", [[], "x", None, 7])
def test_a_non_object_document_is_refused_whole(doc: Any) -> None:
    watch = lw.parse_legacy_watch(doc)
    assert not watch.ok
    assert watch.groups == ()


def test_an_agent_whose_value_is_not_a_list_is_refused_whole() -> None:
    watch = lw.parse_legacy_watch({GLM: {"code": "600036.SH"}})
    assert not watch.ok
    assert lw.REJECT_AGENT_NOT_LIST in watch.errors[0]


def test_a_blank_agent_name_is_refused() -> None:
    watch = lw.parse_legacy_watch({"   ": []})
    assert not watch.ok
    assert lw.REJECT_NO_AGENT in watch.errors[0]


def test_two_agents_that_normalize_to_one_identity_are_refused() -> None:
    """重名的两家会互相整组替换（一家的分析清掉另一家的守则），必须整轮拒收。"""
    watch = lw.parse_legacy_watch({GLM: [], f" {GLM} ": []})
    assert not watch.ok
    assert lw.REJECT_DUPLICATE_AGENT in watch.errors[0]


def test_bad_rows_are_problems_but_do_not_refuse_the_round() -> None:
    watch = lw.parse_legacy_watch(
        {
            GLM: [None, {"code": "  "}, {"code": "600036.SH", "stop_loss": 9.0}],
            TA: [{"stop_loss": 9.0}],
        }
    )
    assert watch.ok  # 坏一条不牵连整轮（其余守则照挂）
    # source_rules 数的是**文件里写成规则的那些行**（含后来被判坏的：对账口径是
    # 「读进来多少 / 挂出多少」）；``None`` 那种连行都不算，不进这个数
    assert watch.source_rules == 3
    assert len(watch.problems) == 3
    assert any(p.startswith(f"{GLM}：") for p in watch.problems)
    assert any(p.startswith(f"{TA}：") for p in watch.problems)
    assert len(watch.groups[0].rules) == 1


def test_garbage_never_raises() -> None:
    for doc in (
        {GLM: [{"code": 5, "pct": [0.3]}]},
        {GLM: [{"code": "600036.SH", "stop_loss": "12.5%", "take_profit": True}]},
        {GLM: ["x", 3]},
    ):
        lw.parse_legacy_watch(doc)  # 不抛就是通过


# ---------------------------------------------------------------------------
# 映射层：比例三态 / 去重 / 组合校验都走既有实现
# ---------------------------------------------------------------------------
def test_pct_zero_is_not_armed_and_missing_pct_falls_back_to_full() -> None:
    watch = lw.parse_legacy_watch(
        {
            GLM: [
                {"code": "600036.SH", "stop_loss": 9.0, "pct": 0.0},
                {"code": "600519.SH", "stop_loss": 9.0},
                {"code": "600000.SH", "stop_loss": 9.0, "pct": "0.3股"},
            ]
        }
    )
    plans = lw.plan_groups(watch)
    armed = {r.symbol: r for r in plans[0].rules}

    assert set(armed) == {"600519.SH", "600000.SH"}
    assert armed["600519.SH"].rule["reduce_pct"] == 1.0
    assert armed["600000.SH"].rule["reduce_pct"] == 1.0  # 脏值 → 全仓 + 注记
    assert armed["600000.SH"].notes
    assert [r.reason for r in plans[0].rejected] == [REJECT_PCT_ZERO]


def test_a_watch_without_any_level_is_rejected_not_armed() -> None:
    """只见 move_stop 的条件位不挂（隔壁同口径）：没有触发价位就没有防守位。"""
    watch = lw.parse_legacy_watch(
        {GLM: [{"code": "600036.SH", "move_stop": 42.0, "pct": 1.0}]}
    )
    plans = lw.plan_groups(watch)
    assert plans[0].rules == ()
    assert plans[0].rejected[0].reason == REJECT_NO_LEVEL


def test_duplicate_levels_keep_only_the_first() -> None:
    dup = {"code": "600036.SH", "stop_loss": 9.0, "take_profit": 11.0, "pct": 1.0}
    watch = lw.parse_legacy_watch({GLM: [dict(dup), dict(dup)]})
    plans = lw.plan_groups(watch)
    assert len(plans[0].rules) == 1
    assert plans[0].rejected[0].reason == REJECT_DUPLICATE


# ---------------------------------------------------------------------------
# 预演：与真写一遍逐家比对（本文件最要紧的一条）
# ---------------------------------------------------------------------------
def test_simulate_matches_a_real_sequential_apply() -> None:
    watch = lw.parse_legacy_watch(neighbor_doc())
    plans = lw.plan_groups(watch)
    sim = lw.simulate_apply(plans, seed_table())

    assert sim.table_before == 3

    real_redis = FakeRedis(cfg(*seed_table()))
    real: dict[str, Any] = {}
    for plan in plans:
        if lw.has_rules(plan):
            real[plan.agent] = write_watch_plan(real_redis, plan, agent=plan.agent)

    for preview in sim.agents:
        result = real.get(preview.agent)
        if result is None:  # 空集：真写路径根本没被调用
            assert preview.kept, f"{preview.agent} 既没写也没保留旧组"
            continue
        assert result.ok, result.errors
        assert set(result.armed) == set(preview.armed)
        assert {c.symbol for c in result.conflicts} == {
            c.symbol for c in preview.conflicts
        }
        # 逐字比落库字典：预演说「会写成这样」，表里就必须**正是这样**
        mine = {
            str(r["symbol"]): dict(r)
            for r in table(real_redis)
            if str(r.get("owner") or "") == preview.owner
        }
        assert mine == {str(r["symbol"]): dict(r) for r in preview.arms}

    assert len(table(real_redis)) == sim.table_after
    assert symbols_in(real_redis) == {
        "600000.SH",  # 人工的，一条没碰
        "000001.SZ",  # 别家的，一条没碰
        "600036.SH",  # 两家抢，排序在前的 GLM 拿到
        "002074.SZ",  # TA 自己的旧规则，被本轮替换成新价位
    }


def test_the_alphabetically_first_agent_holds_a_contested_symbol() -> None:
    """先到先得且**顺序确定**（P2.4 口径，本层不另立一套仲裁）。"""
    watch = lw.parse_legacy_watch(neighbor_doc())
    sim = lw.simulate_apply(lw.plan_groups(watch), seed_table())
    by_agent = {a.agent: a for a in sim.agents}

    assert by_agent[GLM].armed == ("600036.SH",)
    assert by_agent[GLM].conflicts == ()
    assert by_agent[TA].armed == ("002074.SZ",)
    held = by_agent[TA].conflicts
    assert [c.symbol for c in held] == ["600036.SH"]
    assert held[0].holder == ex.llm_owner(GLM)
    assert held[0].reason == ww.REJECT_OTHER_AGENT.format(holder=ex.llm_owner(GLM))


def test_a_manual_rule_blocks_the_agent_and_survives() -> None:
    watch = lw.parse_legacy_watch(
        {GLM: [{"code": "600000.SH", "stop_loss": 9.0, "pct": 1.0}]}
    )
    plans = lw.plan_groups(watch)
    sim = lw.simulate_apply(plans, seed_table())

    assert sim.agents[0].armed == ()
    assert sim.agents[0].conflicts[0].reason == ww.REJECT_MANUAL_HOLD
    assert sim.agents[0].conflicts[0].holder == ""
    assert "600000.SH" in sim.attention()[0]

    redis = FakeRedis(cfg(*seed_table()))
    result = write_watch_plan(redis, plans[0], agent=GLM)
    assert result.armed == ()
    assert {c.symbol for c in result.conflicts} == {
        c.symbol for c in sim.agents[0].conflicts
    }
    manual = [r for r in table(redis) if r["symbol"] == "600000.SH"][0]
    assert manual["owner"] == ""  # 人工那条没被自动化删掉


def test_re_arming_my_own_symbol_is_not_a_conflict() -> None:
    """我自己的旧规则不算「别人占着」——整组替换就是要把它们换掉。"""
    watch = lw.parse_legacy_watch(
        {TA: [{"code": "002074.SZ", "stop_loss": 40.0, "pct": 0.5}]}
    )
    sim = lw.simulate_apply(lw.plan_groups(watch), seed_table())

    assert sim.agents[0].armed == ("002074.SZ",)
    assert sim.agents[0].conflicts == ()
    assert sim.agents[0].removed == ()  # 槽位还在（被自己接手），不算摘除
    assert sim.agents[0].arms[0]["stop_loss_price"] == 40.0


def test_a_symbol_that_leaves_the_group_is_reported_as_removed() -> None:
    watch = lw.parse_legacy_watch(
        {TA: [{"code": "600036.SH", "stop_loss": 9.0, "pct": 1.0}]}
    )
    sim = lw.simulate_apply(lw.plan_groups(watch), seed_table())

    assert sim.agents[0].removed == ("002074.SZ",)
    assert sim.table_after == 3  # 3 条既存 + 1 条新挂 - 1 条被替换


def test_exceeding_the_cap_writes_nothing_for_that_agent() -> None:
    # 别的家把 50 个**别的**标的占满（序号从 0000 起，避开下面那条 600999.SH）
    others = [
        rule(f"60{i:04d}.SH", owner=ex.llm_owner("别家")) for i in range(ex.MAX_RULES)
    ]
    watch = lw.parse_legacy_watch(
        {GLM: [{"code": "600999.SH", "stop_loss": 9.0, "pct": 1.0}]}
    )
    plans = lw.plan_groups(watch)
    sim = lw.simulate_apply(plans, others)

    assert sim.agents[0].armed == ()
    assert sim.agents[0].arms == ()
    assert "上限" in sim.agents[0].overflow
    assert sim.table_after == sim.table_before == ex.MAX_RULES

    redis = FakeRedis(cfg(*others))
    result = write_watch_plan(redis, plans[0], agent=GLM)
    assert result.armed == ()
    assert result.errors  # 超限在真写路径是**轮次级失败**
    assert len(table(redis)) == ex.MAX_RULES


def test_an_empty_plan_would_wipe_the_group_so_the_gate_keeps_it() -> None:
    """闸门存在的理由（把事故本身演一遍）：空计划 + 整组替换 = 守住的东西被清光。"""
    glm_rule = rule("600036.SH", owner=ex.llm_owner(GLM))
    redis = FakeRedis(cfg(*[glm_rule]))

    empty = WatchPlan(rules=(), agent=GLM)
    assert not lw.has_rules(empty)

    write_watch_plan(redis, empty, agent=GLM)  # 真写一遍：整组没了
    assert symbols_in(redis) == set()


def test_an_empty_plan_keeps_the_whole_group_and_never_calls_the_writer() -> None:
    glm_rule = rule("600036.SH", owner=ex.llm_owner(GLM))
    watch = lw.parse_legacy_watch({GLM: [{"code": "600036.SH", "move_stop": 42.0}]})
    plans = lw.plan_groups(watch)
    sim = lw.simulate_apply(plans, [glm_rule, rule("600000.SH", owner="")])

    assert sim.agents[0].kept == ("600036.SH",)
    assert sim.agents[0].armed == ()
    assert sim.table_after == sim.table_before == 2
    # 「有源规则、一条都挂不上」与「这家本来就没条目」必须分开：前者这家本轮等于没
    # 更新（表里留着的是上一次的守护），在切换窗口里与「搬完了」长得一模一样 ⇒ 吵
    assert len(sim.attention()) == 1
    assert "本轮这家没有更新" in sim.attention()[0]
    assert "600036.SH" in sim.attention()[0]


def test_an_agent_with_no_entries_at_all_is_not_an_attention_item() -> None:
    """文件里这家本来就没条目（空列表）⇒ 什么都没发生，不该吵。"""
    sim = lw.simulate_apply(lw.plan_groups(lw.parse_legacy_watch({GLM: []})), [])
    assert sim.attention() == ()
    assert sim.table_after == 0


def test_the_preview_summary_is_json_ready_and_carries_the_arms() -> None:
    watch = lw.parse_legacy_watch(neighbor_doc())
    sim = lw.simulate_apply(lw.plan_groups(watch), seed_table())
    doc = json.loads(json.dumps(sim.summary(), ensure_ascii=False))

    glm = next(a for a in doc["agents"] if a["agent"] == GLM)
    assert glm["owner"] == ex.llm_owner(GLM)
    assert [r["symbol"] for r in glm["arms"]] == ["600036.SH"]
    assert glm["arms"][0]["owner"] == ex.llm_owner(GLM)
    assert doc["table_after"] == 4
    assert sim.armed_total == 2
    assert sim.conflicts_total == 1


# ---------------------------------------------------------------------------
# 名册闸
# ---------------------------------------------------------------------------
def test_roster_mismatches_lists_unknown_agents_once_and_sorted() -> None:
    assert lw.roster_mismatches(("b", "a", "b", ""), ("a",)) == ("b",)
    assert lw.roster_mismatches(WITH_RULES, WITH_RULES) == ()


def test_roster_agents_prefers_the_roster(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.shared import decision_llm_client as client

    monkeypatch.setenv(client.ENV_ROSTER, "[]")
    monkeypatch.setattr(
        client,
        "resolve_roster",
        lambda: (
            client.DecisionLLMConfig("http://x/v1", "k", GLM),
            client.DecisionLLMConfig("http://x/v1", "k", TA),
        ),
    )
    agents, note = cli._roster_agents()
    assert agents == WITH_RULES
    assert note.startswith("名册 2 家")


def test_roster_agents_falls_back_to_the_single_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.shared import decision_llm_client as client

    monkeypatch.delenv(client.ENV_ROSTER, raising=False)
    monkeypatch.setattr(
        client,
        "resolve_config",
        lambda: client.DecisionLLMConfig("http://x/v1", "k", TA),
    )
    assert cli._roster_agents() == ((TA,), f"单家（{TA}，未开 {client.ENV_ROSTER}）")


# ---------------------------------------------------------------------------
# CLI：预演 / 落库 / 复验
# ---------------------------------------------------------------------------
def test_plan_mode_writes_nothing_and_prints_the_disabled_note(
    wired: FakeRedis,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    wired.store[ex.CONFIG_KEY]["enabled"] = False
    src = write_src(tmp_path, neighbor_doc())

    code = cli.main(["--file", str(src)])

    assert code == cli.EXIT_ATTENTION  # 有冲突 ⇒ 要人看一眼
    assert wired.set_calls == []
    assert symbols_in(wired) == {"600000.SH", "000001.SZ", "002074.SZ"}
    out = capsys.readouterr().out
    assert "总开关未开" in out
    assert "一行未写" in out
    assert "600036.SH" in out


def test_apply_stamps_ownership_and_writes_a_record(
    wired: FakeRedis, tmp_path: Path
) -> None:
    src = write_src(tmp_path, neighbor_doc())
    record = tmp_path / "record.json"

    code = cli.main(["--file", str(src), "--apply", "--record", str(record)])

    assert code == cli.EXIT_ATTENTION  # 两家抢一个标的：不许静默通过

    landed = {str(r["symbol"]): r for r in table(wired)}
    assert landed["600036.SH"]["owner"] == ex.llm_owner(GLM)
    assert landed["600036.SH"]["stop_loss_price"] == 31.0
    assert landed["002074.SZ"]["owner"] == ex.llm_owner(TA)
    assert landed["002074.SZ"]["stop_loss_price"] == 44.6  # 旧价 9.0 被整组替换
    assert landed["600000.SH"]["owner"] == ""  # 人工那条原地不动
    assert "510300.SH" not in landed  # pct=0 不挂

    doc = json.loads(record.read_text(encoding="utf-8"))
    assert doc["kind"] == "legacy-watch-migration"
    assert doc["source"]["source_rules"] == 4
    assert doc["verdict"]["ok"] is False
    assert len(doc["rules"]) == 4  # 逐条源规则都有归宿，一条不漏
    rows = {
        (r["agent"], r["code"]): r for r in doc["rules"]
    }  # 两家都挂同一个标的是常事
    assert rows[(TA, "002074.SZ")]["armed"] is True
    assert rows[(TA, "002074.SZ")]["expected_rule"]["symbol"] == "002074.SZ"
    assert rows[(TA, "002074.SZ")]["expected_rule"]["owner"] == ex.llm_owner(TA)
    assert rows[(TA, "002074.SZ")]["reason"] == "跌破前低就走"  # 证据链留档（表里没有）
    assert rows[(GLM, "600036.SH")]["armed"] is True
    assert rows[(TA, "600036.SH")]["armed"] is False  # 排序在后的那家拿不到槽位
    assert rows[(TA, "600036.SH")]["expected_rule"] is None
    assert rows[(GLM, "510300.SH")]["armed"] is False
    assert rows[(GLM, "510300.SH")]["expected_rule"] is None


def test_apply_is_idempotent(wired: FakeRedis, tmp_path: Path) -> None:
    """重复跑不会把上一次挂好的守护清掉（空集不写 + 整组替换是幂等的）。"""
    src = write_src(tmp_path, neighbor_doc())
    argv = ["--file", str(src), "--apply", "--record", str(tmp_path / "r.json")]

    cli.main(argv)
    after_first = json.dumps(table(wired), ensure_ascii=False, sort_keys=True)
    cli.main(argv)
    assert json.dumps(table(wired), ensure_ascii=False, sort_keys=True) == after_first


def test_an_agent_with_no_usable_rules_keeps_its_armed_group(
    wired: FakeRedis, tmp_path: Path
) -> None:
    """「重复跑一次就把上次挂好的止损清了」这件事的端到端样子。

    这家本该挂的那条没有触发价位（不挂）⇒ 本轮规则集为空 ⇒ **整组保留**：它上一轮
    武装好的守则必须原地不动。改掉这条闸门，下面第一行断言就会红。
    """
    wired.store[ex.CONFIG_KEY]["rules"] = [
        *seed_table(),
        rule("600519.SH", owner=ex.llm_owner(GLM)),
    ]
    src = write_src(
        tmp_path,
        {
            GLM: [{"code": "600519.SH", "move_stop": 42.0}],  # 没有触发价位
            TA: [{"code": "002074.SZ", "stop_loss": 44.6, "pct": 1.0}],
        },
    )

    code = cli.main(
        ["--file", str(src), "--apply", "--record", str(tmp_path / "r.json")]
    )

    assert symbols_in(wired) == {"600000.SH", "000001.SZ", "600519.SH", "002074.SZ"}
    kept = next(r for r in table(wired) if r["symbol"] == "600519.SH")
    assert kept["owner"] == ex.llm_owner(GLM)  # 还是那家的
    assert kept["stop_loss_price"] == 9.0  # 一个字段都没被动
    assert (
        code == cli.EXIT_ATTENTION
    )  # GLM 那条没挂上：要人看一眼（它还在，但基于旧价位）
    record = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    glm = next(a for a in record["preview"]["agents"] if a["agent"] == GLM)
    assert glm["kept"] == ["600519.SH"] and glm["armed"] == []


def test_a_missing_source_file_is_an_environment_error_not_an_empty_table(
    wired: FakeRedis, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """最危险的静默降级：把「读不到」当成「没有规则」⇒ 什么都没发生且退出码 0。"""
    code = cli.main(
        [
            "--file",
            str(tmp_path / "nope.json"),
            "--apply",
            "--record",
            str(tmp_path / "r.json"),
        ]
    )
    assert code == cli.EXIT_USAGE
    assert wired.set_calls == []
    assert "读不到" in capsys.readouterr().err


@pytest.mark.parametrize("empty", [{}, {GLM: []}])
def test_a_source_file_without_agents_is_an_attention_item_not_success(
    wired: FakeRedis, tmp_path: Path, capsys: pytest.CaptureFixture[str], empty: Any
) -> None:
    """空文件是最容易被读成「搬完了」的输入（实测隔壁停跑后它就变成了 ``{}``）。"""
    src = write_src(tmp_path, empty)

    code = cli.main(
        ["--file", str(src), "--apply", "--record", str(tmp_path / "r.json")]
    )

    assert code == cli.EXIT_ATTENTION
    assert wired.set_calls == []
    assert "什么都没做" in capsys.readouterr().err


def test_a_broken_source_file_is_refused(wired: FakeRedis, tmp_path: Path) -> None:
    bad = tmp_path / "live_watch.json"
    bad.write_text("{ not json", encoding="utf-8")
    assert cli.main(["--file", str(bad)]) == cli.EXIT_USAGE
    assert wired.set_calls == []


def test_apply_without_a_record_is_a_usage_error_and_writes_nothing(
    wired: FakeRedis, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """缺 ``--record`` 必须在**写第一条规则之前**就停下，且理由要点名 ``--record``。

    只断言退出码是不够的（扫掠里这一条**活过**一轮）：缺存档时 ``Path("")`` 会落到
    当前目录，容器里那正好在仓库树内 ⇒ 被**另一条**判据拦下，退出码同样是 2——于是
    「缺存档」这条纪律在测试里是**顺带**成立的。宿主上从别处跑则是另一种结果：守卫
    放行、规则照写、存档写到目录上抛 ``IsADirectoryError``——规则已经落库而没有任何
    存档。所以这里连理由一起钉住。
    """
    src = write_src(tmp_path, neighbor_doc())

    assert cli.main(["--file", str(src), "--apply"]) == cli.EXIT_USAGE

    assert wired.set_calls == []
    assert "--record" in capsys.readouterr().err


def test_a_record_inside_a_git_worktree_is_refused(
    wired: FakeRedis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = write_src(tmp_path, neighbor_doc())
    record = tmp_path / "record.json"
    monkeypatch.setattr(cli, "_record_refusal", lambda path: "在 git 工作树 /x 内")

    code = cli.main(["--file", str(src), "--apply", "--record", str(record)])

    assert code == cli.EXIT_USAGE
    assert wired.set_calls == []
    assert not record.exists()


def test_a_record_inside_the_repo_tree_is_refused_even_without_git(
    wired: FakeRedis, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """**容器里的那条判据**：``/app`` 就是仓库树，而容器里看不到 ``.git``。

    只问 git（``inside_git_worktree``）在容器里恒返回 ``None`` ⇒ 静默放行，一份带
    持仓成本与仓位判断的存档就躺在仓里等下一次 ``git add``。判据里「在仓库树内」
    这一条**不依赖 git**，正是为了这种情况——这里连 tmp_path 都不需要：目标就在
    真的仓库树里（且刻意选一个永不创建的名字，守卫必须在写之前拦住）。
    """
    src = write_src(tmp_path, neighbor_doc())
    inside = cli.PROJECT_ROOT / "backend/tests/_never_created_record.json"
    remove_repo_residue(inside)  # 上一次跑（尤其变红的负控跑法）的残留先清掉
    try:
        code = cli.main(["--file", str(src), "--apply", "--record", str(inside)])

        assert code == cli.EXIT_USAGE
        assert wired.set_calls == []
        assert not inside.exists()
        # 理由必须是**仓库树**那条：宿主上这里同时也在 git 工作树里，所以只断言退出码
        # 的用例在宿主上会因为另一条判据而变绿——那正好盖住「仓库树这条没了」的变异体
        assert "仓库树" in capsys.readouterr().err
    finally:
        # 这条用例变红时，现场正是「守卫没拦住、存档真的写进了仓」——不清掉，下一次
        # 基线会先红在 ``exists()`` 上（看起来像新 bug），而残留本身还会被 git 收走。
        # 负控跑法（变异体扫掠 / 反复重跑）必然踩到，所以清理写进 finally。
        remove_repo_residue(inside)


def test_the_roster_gate_refuses_agents_that_nobody_will_adopt(
    wired: FakeRedis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_roster_agents", lambda: (("别的模型",), "名册 1 家"))
    src = write_src(tmp_path, neighbor_doc())

    code = cli.main(
        ["--file", str(src), "--apply", "--record", str(tmp_path / "r.json")]
    )

    assert code == cli.EXIT_USAGE
    assert wired.set_calls == []
    assert not (tmp_path / "r.json").exists()


def test_an_unreadable_roster_is_a_usage_error(
    wired: FakeRedis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom():
        raise RuntimeError("没配决策 LLM")

    monkeypatch.setattr(cli, "_roster_agents", boom)
    src = write_src(tmp_path, neighbor_doc())
    assert cli.main(["--file", str(src)]) == cli.EXIT_USAGE
    assert wired.set_calls == []


def test_a_write_that_does_not_match_the_preview_is_shouted_about(
    wired: FakeRedis,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """看门狗：真写回来的 ``armed`` 与预演不一致时必须吵（并发改表 / 两处算法分叉）。

    预演是操作员**按确认**的那份报告，所以「报告说挂上了、实际没挂上」不能只体现在
    一个数字上。这里替身故意吞掉落库（回读会失败），模拟读-改-写之间表被改过。
    """
    src = write_src(tmp_path, neighbor_doc())
    monkeypatch.setattr(
        cli,
        "write_watch_plan",
        lambda redis, plan, agent: ww.WatchWriteResult(
            owner=ex.llm_owner(agent), plan=plan
        ),
    )

    code = cli.main(
        ["--file", str(src), "--apply", "--record", str(tmp_path / "r.json")]
    )

    assert code == cli.EXIT_ATTENTION
    out = capsys.readouterr().out
    assert "预演说挂上但实际没挂上" in out
    record = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    assert record["verdict"]["ok"] is False
    assert any("预演说挂上但实际没挂上" in a for a in record["verdict"]["attention"])


def test_verify_passes_right_after_apply(wired: FakeRedis, tmp_path: Path) -> None:
    src = write_src(tmp_path, neighbor_doc())
    record = tmp_path / "record.json"
    cli.main(["--file", str(src), "--apply", "--record", str(record)])

    code = cli.main(["--verify", "--record", str(record)])

    assert code == cli.EXIT_OK


def test_verify_reports_a_rule_that_is_gone(wired: FakeRedis, tmp_path: Path) -> None:
    src = write_src(tmp_path, neighbor_doc())
    record = tmp_path / "record.json"
    cli.main(["--file", str(src), "--apply", "--record", str(record)])

    kept = [r for r in table(wired) if r["symbol"] != "002074.SZ"]
    wired.store[ex.CONFIG_KEY] = {**wired.store[ex.CONFIG_KEY], "rules": kept}

    assert cli.main(["--verify", "--record", str(record)]) == cli.EXIT_ATTENTION


def test_verify_reports_a_rule_that_was_edited(
    wired: FakeRedis, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """开闸前要确认的是「现在表里是什么」，不是「待会儿会发生什么」。"""
    src = write_src(tmp_path, neighbor_doc())
    record = tmp_path / "record.json"
    cli.main(["--file", str(src), "--apply", "--record", str(record)])

    edited = []
    for r in table(wired):
        r = dict(r)
        if r["symbol"] == "002074.SZ":
            r["stop_loss_price"] = 1.0  # 人手动过
        edited.append(r)
    wired.store[ex.CONFIG_KEY] = {**wired.store[ex.CONFIG_KEY], "rules": edited}

    assert cli.main(["--verify", "--record", str(record)]) == cli.EXIT_ATTENTION
    assert "被改过" in capsys.readouterr().out


def test_verify_needs_a_record_and_reads_it_before_touching_redis(
    wired: FakeRedis, tmp_path: Path
) -> None:
    assert cli.main(["--verify"]) == cli.EXIT_USAGE
    assert (
        cli.main(["--verify", "--record", str(tmp_path / "nope.json")])
        == cli.EXIT_USAGE
    )
    assert wired.get_calls == 0


def test_argument_guards() -> None:
    assert cli.main(["--apply", "--verify", "--record", "x"]) == cli.EXIT_USAGE
    assert cli.main(["--expect-source-sha", "abc", "--file", "x"]) == cli.EXIT_USAGE


def test_rule_diff_ignores_derived_fields_and_accepts_int_float_equality() -> None:
    want = dict(ex.DEFAULT_RULE, symbol="600036.SH", stop_loss_price=9.0)
    live = {**want, "stop_loss_price": 9, "reject_reason": "x", "rejected_rules": []}

    assert cli._rule_diff(want, live) == ""
    assert "stop_loss_price" in cli._rule_diff(want, {**live, "stop_loss_price": 9.5})
    assert "owner" in cli._rule_diff(want, {**live, "owner": "llm:别人"})


def test_the_module_runs_as_a_script() -> None:
    """``python -m backend.scripts.migrate_legacy_watch --help`` 可直接跑。"""
    import subprocess
    import sys

    cp = subprocess.run(
        [sys.executable, "-m", "backend.scripts.migrate_legacy_watch", "--help"],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert cp.returncode == 0, cp.stderr
    assert "live_watch.json" in cp.stdout
