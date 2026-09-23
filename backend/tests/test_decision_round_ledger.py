"""P2.7 接线测试：决策轮怎么用分账账本（裁剪 / 额度 / 成本列 / fail-closed）。

为什么单独一个文件（而不是塞进 ``test_decision_round.py``）
--------------------------------------------------------
那边测的是「一轮的编排」（到点、取数、闸门、守护、审计），已经 2300+ 行；本文件的
每一件事都只由**一个注入点**（``load_agent_ledger``）驱动，是同一个新机制的不同侧面。
分家的判据是「坏了的时候你会先去哪找」——分账出了事，该一眼找到一个文件读完。

要钉住的三件事（前两件是事故形状，第三件是静默失效形状）
------------------------------------------------------
1. **裁剪**：桥账户是多家模型共用的，模型只能看见自己名下的货。看不见的仓位它卖不掉
   ——这是**可见性**防线。2026-09-08 隔壁实录：空账本的 agent 拿到全账户持仓，把
   另一家模型的票（生益电子）卖了。
2. **空账本不回退**：``mine`` 为空就是空。历史上有过 ``if mine else holdings`` 的
   兜底，那一行就是事故本身，故它在本仓**没有可以选的分支**。
3. **读失败 = 中止本轮**，不是「当成空账本」：把「读不到」当成「名下没有货」，后果是
   该卖的永远不卖，而状态键、日志、审计三面全绿。

测试替身来自 ``test_decision_round``（本仓既有做法：跨测试文件 import 公共替身，
见 ``test_sltp_*`` 族）。那个 harness 的默认账本**与持仓同集**（不显式换
``ledger=`` 的用例里裁剪是空操作），故这里每条用例都显式给一个账本。

边界：本文件只测**轮的输入**（交给模型与执行段的持仓、闸门参数、审计段）。真执行器
拿到这份输入之后怎么判，是 ``test_desk_plan_execute`` 与 ``test_decision_round``
里真执行器用例的事。
"""

from __future__ import annotations

import dataclasses

import pytest

from backend.services.trade.services import decision_round as R
from backend.services.trade.services.decision_round import run_once
from backend.services.trade.services.decision_round_core import (
    STATUS_ABORTED,
    AgentLedgerRead,
)
from backend.shared.decision.agent_ledger import DEFAULT_AGENT_QUOTA
from backend.tests.test_decision_round import (
    BANK_ROW,
    NOW,
    SLOT_0935,
    decisions_json,
    make_harness,
    pool_doc,
)

#: 桥账户里两只（前缀式 payload = 生产形态）。账本只认领 600036 一只。
TWO_HOLDINGS = {
    "SH600036": {
        "symbol": "600036.SH",
        "name": "招商银行",
        "volume": 1000,
        "available_volume": 1000,
        "cost_price": 38.0,
        "price": 40.0,
    },
    "SZ000001": {
        "symbol": "000001.SZ",
        "name": "平安银行",
        "volume": 500,
        "available_volume": 500,
        "cost_price": 11.0,
        "price": 12.0,
    },
}
#: 600036 归本 agent，000001 归**别家**（这正是要挡住的那种情形）。
#: 账本成本 30.0 与桥口径 38.0 故意不同：提示词的「成本/盈亏」该用前者。
LEDGER_ONLY_600036 = AgentLedgerRead(
    ok=True,
    known=True,
    positions={
        "600036.SH": {
            "volume": 1000,
            "cost_price": 30.0,
            "buy_ts": "2026-09-20T01:30:00Z",
            "last_ts": "2026-09-20T01:30:00Z",
        }
    },
    virtual_cash=100_000.0,
)

_HOLDINGS_HEAD = "【你名下的现有持仓】"


def _table_cells(prompt: str, head: str, first_header: str) -> list[list[str]]:
    """``head`` 那一节的表格数据行（每行拆成单元格，不含列名行）。

    **必须从表头往下切**：候选池的表也是 ``|`` 开头、也含 ``600036.SH``（它同时是
    持仓和池内票），全局搜第一行会搜到池表，于是「持仓成本列」断言实际上在看
    「行业列」——一条永远为真的假测试。
    """
    lines = prompt.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith(head))
    out: list[list[str]] = []
    in_table = False
    for ln in lines[start + 1 :]:  # 表头与表格之间隔着一个空行，不能见非 ``|`` 就停
        if not ln.startswith("|"):
            if in_table:
                break
            continue
        in_table = True
        cells = [c.strip() for c in ln.strip("|").split("|")]
        if set(cells[0]) == {"-"}:  # 分隔行
            continue
        out.append(cells)
    assert out and out[0][0] == first_header, f"{head} 表没找到：{out!r}"
    return out[1:]  # 首行是列名


def holding_cells(prompt: str) -> list[list[str]]:
    return _table_cells(prompt, _HOLDINGS_HEAD, "代码")


def pool_cells(prompt: str) -> list[list[str]]:
    return _table_cells(prompt, "【候选池】", "排名")


# ── ① 裁剪 ──────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_holdings_not_in_the_ledger_are_hidden_from_both_sides():
    """别家的持仓既不进提示词，也不进执行段——两个口子都要堵。"""
    h = make_harness(positions=TWO_HOLDINGS, ledger=LEDGER_ONLY_600036)
    result = await run_once(SLOT_0935, deps=h.deps, now=NOW)

    assert result.status == R.STATUS_OK
    prompt = h.log["llm"][0]["prompt"]
    assert "招商银行" in prompt
    assert "000001.SZ" not in prompt, "别家的持仓进了提示词"
    assert "平安银行" not in prompt

    sent = h.log["exec"][0]
    assert list(sent["holdings"]) == ["600036.SH"], "别家的持仓进了执行段"
    assert "000001.SZ" not in sent["quotes"], "别家的票还带了行情进执行段"

    meta = h.log["ledger"][0][0].context_meta
    assert meta["ledger"] == {
        "ok": True,
        "known": True,
        "positions": 1,
        "virtual_cash": 100000.0,
    }
    assert any("分账裁剪" in n for n in meta["notes"]), (
        "裁剪没有留痕：模型对某只票视而不见的原因就查不出来了"
    )


@pytest.mark.asyncio
async def test_sell_leg_for_another_agents_holding_cannot_reach_the_broker():
    """模型若仍点名别家的票（脏上下文/历史会话），执行段手里根本没有它。

    决策文本照旧原样递到执行段（**不在这里改模型的话**——改了下游就看不到模型
    到底想干什么）；挡人的是事实源：执行段的卖出以 ``holdings`` 为准，而别家的票
    不在里面，连行情都没给它取。真执行器据此只会判 ``sell_not_held``（那条判定在
    executor 自己的用例里）。
    """
    h = make_harness(
        positions=TWO_HOLDINGS,
        ledger=LEDGER_ONLY_600036,
        decision_text=decisions_json(
            {"action": "sell", "code": "000001.SZ", "pct": 1.0, "reason": "换股"}
        ),
    )
    await run_once(SLOT_0935, deps=h.deps, now=NOW)

    sent = h.log["exec"][0]
    assert "000001.SZ" not in sent["holdings"]
    assert "000001.SZ" not in sent["quotes"], "别家的票连行情都不该取"
    # 模型的原话照递（不改模型的话）：审计里要能看到「它想卖别家的票」这件事
    assert any(d.code == "000001.SZ" for d in sent["batch"].decisions)


@pytest.mark.asyncio
async def test_empty_ledger_never_falls_back_to_the_whole_account():
    """``mine`` 为空就是空：没有 ``if mine else holdings`` 那条分支。

    这是 2026-09-08 事故的正身——空账本的 agent 拿到了全账户持仓，把别家的票卖了。
    """
    h = make_harness(
        positions=TWO_HOLDINGS, ledger=AgentLedgerRead(ok=True, known=False)
    )
    result = await run_once(SLOT_0935, deps=h.deps, now=NOW)

    assert result.status == R.STATUS_OK
    assert list(h.log["exec"][0]["holdings"]) == [], "空账本回退到了全账户持仓"
    prompt = h.log["llm"][0]["prompt"]
    # 表头仍在（提示词要能整段渲染），只是数据行一行都没有
    assert _HOLDINGS_HEAD in prompt
    assert holding_cells(prompt) == []
    assert "平安银行" not in prompt  # 别家的票一只都不进视野
    # 池子照旧：空账本**只影响持仓侧**，不把候选也一起关掉（那会让模型无票可买）。
    # 池里只剩一只不是我这条路径干的——茅台一手 17 万，单票预算 7500 本来就买不起。
    assert {r[1] for r in pool_cells(prompt)} == {"600036.SH"}
    meta = h.log["ledger"][0][0].context_meta
    assert meta["ledger"]["known"] is False and meta["ledger"]["positions"] == 0
    assert any("分账裁剪" in n for n in meta["notes"])


@pytest.mark.asyncio
async def test_ledger_is_read_with_the_tenant_user_and_agent():
    """三参都要对：agent 错一位就是别家模型的账。"""
    h = make_harness(ledger=LEDGER_ONLY_600036)
    await run_once(SLOT_0935, deps=h.deps, now=NOW)
    assert h.log["agent_ledger"] == [("default", "10000001", "fake-model")]


# ── ② 读失败 = 中止（不是空账本）────────────────────────────────────
@pytest.mark.asyncio
async def test_unreadable_ledger_aborts_before_asking_the_model():
    bad = AgentLedgerRead(ok=False, errors=("分账账本读取失败：boom",))
    h = make_harness(ledger=bad)
    result = await run_once(SLOT_0935, deps=h.deps, now=NOW)

    assert result.status == STATUS_ABORTED
    assert "分账账本不可读" in result.note and "boom" in result.note
    assert h.log["llm"] == [], "账本读不到还是问了模型"
    assert h.log["exec"] == [], "账本读不到还是发了腿"
    assert h.log["ledger"] == [], "abort 的轮次不许写审计行"


@pytest.mark.asyncio
async def test_ledger_read_exception_aborts_the_round():
    async def boom(tenant, user, agent):
        raise RuntimeError("connection reset")

    h = make_harness(load_agent_ledger=boom)
    result = await run_once(SLOT_0935, deps=h.deps, now=NOW)

    assert result.status == STATUS_ABORTED
    assert "分账账本读取异常" in result.note and "connection reset" in result.note
    assert h.log["llm"] == [] and h.log["exec"] == []


@pytest.mark.asyncio
async def test_ledger_read_happens_before_any_llm_spend_or_quote_pull():
    """顺序也是契约：账本读失败时**一次 LLM 都没调、一次行情都没拉**。"""
    h = make_harness(ledger=AgentLedgerRead(ok=False, errors=("读不到",)))
    await run_once(SLOT_0935, deps=h.deps, now=NOW)
    assert h.log["llm"] == []
    assert h.log["snaps"] == [], "账本都读不到还去拉了行情"


# ── ③ 额度两条闸 ────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_virtual_cash_reaches_the_buy_gate():
    ledger = AgentLedgerRead(ok=True, known=True, virtual_cash=12345.0)
    h = make_harness(ledger=ledger)
    await run_once(SLOT_0935, deps=h.deps, now=NOW)
    assert h.log["exec"][0]["gate"].virtual_cash == 12345.0


@pytest.mark.asyncio
async def test_negative_virtual_cash_is_reported_as_is():
    """透支是真实状态，不许夹到 0：夹掉之后「为什么一直不买」就查不出来了。"""
    h = make_harness(ledger=AgentLedgerRead(ok=True, known=True, virtual_cash=-500.0))
    await run_once(SLOT_0935, deps=h.deps, now=NOW)
    assert h.log["exec"][0]["gate"].virtual_cash == -500.0
    assert h.log["ledger"][0][0].context_meta["ledger"]["virtual_cash"] == -500.0


@pytest.mark.asyncio
async def test_budget_and_exec_quota_take_the_tighter_of_the_two():
    """口径：真实剩余 × pct 与子账户现金，**取更紧的那个**（与隔壁同形）。

    固定量：真实剩余 50000 × 15% = 7500。子账户给 1234（更紧）⇒ 单票预算 1234，
    600036 一手 4000 买不起，池子被剔空；执行段额度也同步收成 1234。
    只取真实额度：腿按 7500 算股数，超出子账户线的那部分只会被 ``l2.vcash`` 否决
    ——本可成交的腿变成一条否决记录。
    """
    h = make_harness(ledger=AgentLedgerRead(ok=True, known=True, virtual_cash=1234.0))
    result = await run_once(SLOT_0935, deps=h.deps, now=NOW)

    assert result.status == R.STATUS_OK  # 池子剔光是正常路径，不是异常
    assert h.log["exec"][0]["quota"] == 1234.0
    meta = h.log["ledger"][0][0].context_meta
    assert meta["pool"] == {
        "file": "/data/reports/stock_picks/20260924_agent_picks.json",
        "shown": 0,
        "dropped": 2,
        "dropped_rules": ["l1.unaffordable"],
    }


@pytest.mark.asyncio
async def test_a_rich_sub_account_does_not_shrink_the_pool():
    """反面对照（证明上一条不是恒真）：子账户钱多时，预算回到真实口径 7500，
    600036 一手 4000 买得起 ⇒ 池子留下它。两条只差 ``virtual_cash`` 一个变量。"""
    h = make_harness(
        ledger=AgentLedgerRead(ok=True, known=True, virtual_cash=100_000.0)
    )
    await run_once(SLOT_0935, deps=h.deps, now=NOW)

    meta = h.log["ledger"][0][0].context_meta
    # 7500 = 真实剩余 50000 × 15%（子账户 10 万不构成约束）；茅台一手 17 万仍买不起
    assert meta["pool"]["shown"] == 1 and meta["pool"]["dropped"] == 1
    assert h.log["exec"][0]["quota"] == 50000.0


@pytest.mark.asyncio
async def test_one_lot_row_only_survives_on_a_budget_it_can_afford():
    """池里只剩一只、且恰好卡在预算边缘时的行为：买得起留下，买不起剔掉。"""
    rich = make_harness(pool=pool_doc(BANK_ROW), ledger=LEDGER_ONLY_600036)
    await run_once(SLOT_0935, deps=rich.deps, now=NOW)
    assert rich.log["ledger"][0][0].context_meta["pool"]["shown"] == 1

    poor = make_harness(
        pool=pool_doc(BANK_ROW),
        ledger=AgentLedgerRead(ok=True, known=True, virtual_cash=10.0),
    )
    await run_once(SLOT_0935, deps=poor.deps, now=NOW)
    assert poor.log["ledger"][0][0].context_meta["pool"]["shown"] == 0


# ── ④ 成本列口径 ────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_prompt_cost_column_uses_the_ledger_not_the_bridge():
    """共用账户里桥的成本是混合口径，拿它算「我这笔赚没赚」基准就错了。"""
    h = make_harness(positions=TWO_HOLDINGS, ledger=LEDGER_ONLY_600036)
    await run_once(SLOT_0935, deps=h.deps, now=NOW)

    rows = holding_cells(h.log["llm"][0]["prompt"])
    assert len(rows) == 1, f"持仓表应只有本 agent 那一只：{rows}"
    assert rows[0][0] == "600036.SH"
    assert rows[0][3] == "30.0", "成本列不是账本口径"
    assert rows[0][5] == "+33.33%", "盈亏该按账本成本 (40-30)/30 算"
    # 数量/可卖量仍取桥（账本是归属的事实源，桥是「今天能不能卖」的事实源）
    assert rows[0][2] == "1000" and rows[0][7] == "1000"


@pytest.mark.asyncio
async def test_ledger_cost_of_zero_falls_back_to_the_bridge_and_marks_it():
    """账本里成本为 0（送股/缺失）→ 回退桥口径并打 ``*``，不许拿 0 当成本。"""
    ledger = AgentLedgerRead(
        ok=True,
        known=True,
        positions={
            "600036.SH": {
                "volume": 1000,
                "cost_price": 0.0,
                "buy_ts": "2026-09-20T01:30:00Z",
                "last_ts": "2026-09-20T01:30:00Z",
            }
        },
    )
    h = make_harness(positions=TWO_HOLDINGS, ledger=ledger)
    await run_once(SLOT_0935, deps=h.deps, now=NOW)

    rows = holding_cells(h.log["llm"][0]["prompt"])
    assert rows[0][3] == "38.0*", "回退桥口径必须带星号"
    assert rows[0][5] == "+5.26%"  # (40-38)/38


# ── ⑤ 审计段 ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_audit_ledger_section_never_leaks_codes():
    """审计记的是**只数与现金**，不是代码清单：抄一份清单只会在两处之间制造对不上。"""
    h = make_harness(positions=TWO_HOLDINGS, ledger=LEDGER_ONLY_600036)
    await run_once(SLOT_0935, deps=h.deps, now=NOW)
    section = h.log["ledger"][0][0].context_meta["ledger"]
    assert set(section) == {"ok", "known", "positions", "virtual_cash"}
    blob = repr(section)
    assert "600036" not in blob and "000001" not in blob


def test_ledger_meta_tolerates_a_shape_it_does_not_know():
    """形状怪异的替身不该把审计炸成 error（``context_meta`` 只许更宽容）。"""
    from backend.services.trade.services.decision_round_core import ledger_meta

    assert ledger_meta(None) == {}

    class Alien:
        pass

    assert ledger_meta(Alien()) == {
        "ok": False,
        "known": False,
        "positions": 0,
        "virtual_cash": 0.0,
    }


# ── ⑥ DTO 形状 ──────────────────────────────────────────────────────
def test_agent_ledger_read_default_is_full_quota():
    """DTO 默认值即「还没读过」：满额 ¥10 万、零持仓、``ok=False``。"""
    empty = AgentLedgerRead()
    assert empty.ok is False
    assert empty.virtual_cash == DEFAULT_AGENT_QUOTA == 100_000.0
    assert empty.mine() == frozenset()


def test_mine_returns_suffix_codes_verbatim():
    """``mine`` 与 ``HoldingRow.code`` 同形态（后缀式）：错一位就是永远卖不掉。"""
    led = AgentLedgerRead(ok=True, known=True, positions={"600036.SH": {}})
    assert led.mine() == frozenset({"600036.SH"})


def test_round_deps_requires_the_ledger_reader():
    """``load_agent_ledger`` 是**必填**注入点：缺了它「谁名下的票」就没出处，
    而那个问题的默认答案（整座账户）就是事故本身。"""
    fields = {f.name: f for f in dataclasses.fields(R.RoundDeps)}
    assert "load_agent_ledger" in fields
    assert fields["load_agent_ledger"].default is dataclasses.MISSING
    assert fields["load_agent_ledger"].default_factory is dataclasses.MISSING


def test_agent_ledger_read_is_frozen():
    led = AgentLedgerRead(ok=True, known=True)
    with pytest.raises(dataclasses.FrozenInstanceError):
        led.ok = False  # type: ignore[misc]
