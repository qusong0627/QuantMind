"""P2.8 测试：决策轮调度（槽位 / 认领 / 位置戳 / 编排 / fail-closed 分层）。

替身策略（决定了这些测试能钉住什么）：

* **编排层全替身**：``RoundDeps`` 逐项换成记录器，Redis 用字典客户端——测的是
  「到点没到点」「读不到什么时怎么办」「给执行段传了什么」；
* **LLM 用真解析链**：假 caller 只负责吐文本，``decide_with_retry`` →
  ``parse_decisions`` → ``Decision`` 全是真件。把文本→决策那层也替掉的话，这里
  测的就成了替身自己的行为（那层另有专门单测）；
* **守护规则用真 ``WatchPlan``/``WatchWriteResult``**，只把「落库」那一步换成
  记录器：整组替换、空集不写这两条纪律的判据在真件里。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from backend.shared.decision.context import DirectionBlock, HoldingRow, PoolRow
from backend.shared.decision.contract import (
    BUY,
    HOLD,
    SELL,
    WATCH,
    SCHEMA_INTRADAY,
    SCHEMA_REBALANCE,
    Decision,
)
from backend.shared.decision.execution import ExecutionPlan, Leg, Veto
from backend.shared.decision.llm_call import decide_with_retry
from backend.shared.decision.watch_writer import WatchWriteResult
from backend.shared.decision_blocks import INDEX_CODES, SeriesSummary
from backend.shared.decision_context_source import PoolDoc
from backend.shared.trading_policy import AgentPolicy, TradingPolicy
from backend.services.trade.services import decision_round as R
from backend.services.trade.services import decision_round_alerts as A
from backend.services.trade.services import decision_round_feeds as F
from backend.services.trade.services import decision_round_io as IO
from backend.services.trade.services import decision_round_notes as NOTES
from backend.services.trade.services import decision_round_runner as RUN
from backend.services.trade.services import decision_round_tick as TICK
from backend.services.trade.services import decision_round_wake as W
from backend.services.trade.services.decision_round import run_once
from backend.services.trade.services.decision_executor import (
    ExecutionOutcome,
    LegReceipt,
)
from backend.services.trade.services.decision_round_tick import round_tick, wake_tick
from backend.services.trade.services.decision_round_io import (
    CLAIM_AUTO,
    CLAIM_MANUAL,
)
from backend.services.trade.services.decision_round_core import (
    CST,
    LAST_KEY,
    LOG_KEY,
    SLOTS,
    STATUS_SKIPPED,
    AccountRead,
    AgentLedgerRead,
    AgentRun,
    ExclusionRead,
    LLMBinding,
    RoundDeps,
    RoundResult,
    RoundSlot,
    as_float,
    build_pool_stamps,
    clip_holdings_to_ledger,
    due_slots,
    gate_row_to_pool_row,
    merge_outcomes,
    mirror_meta,
    pool_row_to_gate_row,
    position_source_meta,
    positions_consistency_issue,
    select_runs,
    refusing_submitter,
    round_id_for,
    slot_keys,
    snap_price,
    tier_numbers,
)

DAY = date(2026, 9, 24)
#: 09:35（**建仓轮**的真实到点时刻）。注意宽限窗下 09:00 的槽这时仍在窗内——
#: tick 层的用例另有取值，见下。
NOW = datetime(2026, 9, 24, 9, 35, tzinfo=CST)
#: 只有一个槽到点的时刻（09:00 已超窗、09:35 仍在窗内）：tick 层用例用。
TICK_NOW = datetime(2026, 9, 24, 9, 50, tzinfo=CST)
#: 10:05（**rebalance 补跑槽**的真实到点时刻）。
TEN_05 = datetime(2026, 9, 24, 10, 5, tzinfo=CST)
#: 只有 10:05 补跑槽到点的时刻（10:00 已超窗、11:00 未到）。
TICK_1005 = datetime(2026, 9, 24, 10, 47, tzinfo=CST)

SLOT_0830 = next(s for s in SLOTS if s.hhmm == "0830")
SLOT_0935 = next(s for s in SLOTS if s.hhmm == "0935")
#: 10:00 **intraday**（守护轮）；10:05 是 rebalance 补跑槽——两者别混，
#: 守护规则该不该写按 schema 分（见 ``decision_round_io.maybe_write_watch``）。
SLOT_1000 = next(s for s in SLOTS if s.hhmm == "1000")
SLOT_1005 = next(s for s in SLOTS if s.hhmm == "1005")


@pytest.fixture(autouse=True)
def _isolate_runtime_env(monkeypatch):
    """``runtime.env`` 属于**部署**：测试里指向不存在的文件（名册/密钥不读真机）。

    资金策略已由 ``backend/tests/conftest.py`` 的 ``QM_TRADING_POLICY_FILE``
    夹具隔离；本夹具只负责把 LLM 名册等运行时配置挡在测试之外。
    """
    monkeypatch.setenv("QM_RUNTIME_ENV_FILE", "/tmp/qm-decision-test-no-runtime.env")


# ══ 替身 ═════════════════════════════════════════════════════════════
@dataclass
class Verdict:
    """``gates.GateVerdict`` 的最小同形（位置戳只用它两个字段）。"""

    rule: str = ""
    reason: str = ""


class FakeNative:
    """原生 redis 客户端的字典替身：``set(nx=True)`` 的语义与真件一致。

    哈希/列表面（第二批 wake 用）：``hset/hgetall`` 真存真取（唤起状态键靠它
    解码）；``lpush/ltrim/lrange`` 真存真取且序与 redis 一致（列表首 = 最新，
    log 头判据读的就是 ``lrange(LOG_KEY, 0, 0)``）。``ttl`` 只记调用（唤醒状态
    的过期是运维面，不是判据）。
    """

    def __init__(
        self,
        *,
        fail_claim: bool = False,
        fail_get: bool = False,
        fail_done_set: bool = False,
        fail_delete: bool = False,
        fail_hset: bool = False,
    ) -> None:
        self.store: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.lists: dict[str, list[str]] = {}
        self.calls: list[tuple] = []
        self.fail_claim = fail_claim
        self.fail_get = fail_get
        self.fail_done_set = fail_done_set
        self.fail_delete = fail_delete
        self.fail_hset = fail_hset
        self.closed = False

    def set(self, key, value, nx=False, ex=None):  # noqa: A002 - 与 redis-py 同形
        self.calls.append(("set", key, nx))
        if nx and self.fail_claim:
            raise RuntimeError("redis 写不下来")
        # 只砸 done 键（非 nx 的写里认这个键），状态键照常写——否则测的就不是
        # 「done 键写不成」而是「Redis 整个坏了」，两件事的处置完全不同。
        if not nx and self.fail_done_set and ":done:" in str(key):
            raise RuntimeError("done 键写不下来")
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def get(self, key):
        if self.fail_get:
            raise RuntimeError("redis 读不下来")
        return self.store.get(key)

    def delete(self, key):
        self.calls.append(("delete", key))
        if self.fail_delete:
            raise RuntimeError("redis 删不下来")
        return 1 if self.store.pop(key, None) is not None else 0

    def hset(self, key, mapping=None, **kw):
        self.calls.append(("hset", key))
        if self.fail_hset:
            raise RuntimeError("redis 哈希写不下来")
        bucket = self.hashes.setdefault(key, {})
        for field, value in dict(mapping or {}, **kw).items():
            bucket[str(field)] = value
        return len(bucket)

    def hgetall(self, key):
        if self.fail_get:
            raise RuntimeError("redis 读不下来")
        return dict(self.hashes.get(key, {}))

    def expire(self, key, seconds):
        self.calls.append(("expire", key, seconds))
        return True

    def lpush(self, key, value):
        self.calls.append(("lpush", key))
        self.lists.setdefault(key, []).insert(0, str(value))
        return len(self.lists[key])

    def ltrim(self, key, start, end):
        self.calls.append(("ltrim", key))
        rows = self.lists.get(key, [])
        stop = None if end == -1 else int(end) + 1
        self.lists[key] = rows[int(start) : stop]
        return True

    def lrange(self, key, start, end):
        rows = self.lists.get(key, [])
        stop = None if end == -1 else int(end) + 1
        return rows[int(start) : stop]

    def close(self):
        self.closed = True


class FakeOutcome:
    """``decision_executor.ExecutionOutcome`` 的最小同形。"""

    def __init__(self, *, outcomes=None, aborted="", **summary) -> None:
        self.outcomes = dict(outcomes or {})
        self.aborted = aborted
        self._summary = {
            "legs": 0,
            "submitted": 0,
            "failed": 0,
            "vetoes": 0,
            "noops": 0,
            "watches": 0,
            "duplicates": 0,
            "notes": [],
        }
        self._summary.update(summary)

    def summary(self) -> dict:
        return dict(self._summary)


class _DBCM:
    async def __aenter__(self):
        return "DB"

    async def __aexit__(self, *exc):
        return False


def pool_doc(*rows: PoolRow) -> PoolDoc:
    return PoolDoc(
        rows=rows,
        direction=DirectionBlock(direction="震荡偏多", total_score=6),
        missing_columns=(),
        source="/data/reports/stock_picks/20260924_agent_picks.json",
    )


BANK_ROW = PoolRow(
    code="600036.SH",
    name="招商银行",
    industry="银行",
    score=1.2,
    fusion=0.8,
    rank=1,
    remark="主力净流入",
)
PRICEY_ROW = PoolRow(
    code="600519.SH",
    name="贵州茅台",
    industry="白酒",
    score=1.1,
    fusion=0.7,
    rank=2,
    # 备注是**这一行专有**的哨兵串：schema 示例 JSON 里就写着 600519.SH，
    # 想验「这只没进模型视野」只能看行内字段，不能看代码。
    remark="预算外-勿推",
)

HOLDING_PAYLOAD = {
    "symbol": "600036.SH",
    "name": "招商银行",
    "volume": 1000,
    "available_volume": 1000,
    "cost_price": 38.0,
    "price": 40.0,
}
SNAPS = {
    "600036.SH": {"Now": 40.0, "PreClose": 39.5, "timestamp": NOW.timestamp()},
    "600519.SH": {"Now": 1700.0, "PreClose": 1690.0, "timestamp": NOW.timestamp()},
}


def decisions_json(*rows: Mapping) -> str:
    return json.dumps({"decisions": list(rows)}, ensure_ascii=False)


def attempt_from(text: str, *, schema: str = SCHEMA_INTRADAY, finish_reason: str = ""):
    """假 caller 只吐文本，其余（重试/解析/契约）全走真件。

    ``finish_reason`` 非空时走 3 元 caller（顶格截断的留痕路径）。
    """
    out: tuple = (
        (text, {"prompt_tokens": 7}, finish_reason)
        if finish_reason
        else (text, {"prompt_tokens": 7})
    )
    return decide_with_retry(lambda _p: out, "提示词", schema=schema)


@dataclass
class Harness:
    """一轮的全部替身 + 记录（``log`` 里是各注入点收到的实参）。"""

    deps: RoundDeps
    log: dict


def make_harness(**over) -> Harness:
    """默认：交易日 09:35（交易时段内）、账户可信、池两只、行情可用、intraday。

    每项都能用同名关键字换掉（``positions=``、``account=``、``pool=``、``snaps=``、
    ``outcome=``、``tier=``、``decision_text=``、``trading_day=``，以及 ``RoundDeps``
    的任何字段名）；没消费完的参数会 assert 出来，防拼错。
    """
    text = over.pop(
        "decision_text",
        decisions_json({"action": "hold", "code": "600036.SH", "reason": "趋势未破"}),
    )
    finish_reason = over.pop("finish_reason", "")
    positions = over.pop("positions", {"SH600036": dict(HOLDING_PAYLOAD)})
    account = over.pop(
        "account",
        AccountRead(
            ok=True,
            cash=50000.0,
            market_value=120000.0,
            total_asset=170000.0,
            source="tdx_bridge",
            snapshot_at="2026-09-24T09:30:00+08:00",
            age_min=5.0,
        ),
    )
    pool = over.pop("pool", pool_doc(BANK_ROW, PRICEY_ROW))
    snaps = over.pop("snaps", SNAPS)
    outcome = over.pop("outcome", FakeOutcome(legs=1, submitted=1))
    trading_day = over.pop("trading_day", True)
    tier = over.pop(
        "tier",
        type(
            "T",
            (),
            {
                "level": "normal",
                "source": "doc",
                "budget": {"per_stock_pct": 0.15, "max_new_buys": 2},
            },
        )(),
    )
    log: dict[str, list] = {
        "positions": [],
        "account": [],
        "agent_ledger": [],
        "pool": [],
        "snaps": [],
        "llm": [],
        "exec": [],
        "watch": [],
        "ledger": [],
    }

    def _decide(prompt, schema):
        log["llm"].append({"prompt": prompt, "schema": schema})
        return attempt_from(text, schema=schema, finish_reason=finish_reason)

    # meta 形状与生产同形（``real_positions.merge_real_sources``）：每源一个 dict，
    # 空持仓时的归因（读到几行 / 是否停更）就靠这几个字段，形状不同则测试测不到真分支。
    pos_meta = over.pop(
        "pos_meta",
        {
            "sources": {
                "tdx_bridge": {
                    "snapshot_at": "2026-09-24T09:30:00+08:00",
                    "positions": len(positions),
                    "stale": False,
                    "lag_min": 0.0,
                    "active_broker": True,
                }
            }
        },
    )

    async def load_positions(tenant, user):
        log["positions"].append((tenant, user))
        return positions, pos_meta

    async def load_account(tenant, user):
        log["account"].append((tenant, user))
        return account

    # 分账账本默认**与持仓同集**（键转后缀式）：不显式换 ``ledger=`` 的用例里，
    # 裁剪是空操作，既有断言全部照旧。要测裁剪的用例自己传一个更小的账本。
    ledger = over.pop("ledger", None)
    if ledger is None:
        from backend.shared.stock_utils import StockCodeUtil

        ledger = AgentLedgerRead(
            ok=True,
            known=bool(positions),
            positions={
                StockCodeUtil.to_suffix(str(code)): {
                    "volume": float(row.get("volume") or 0),
                    "cost_price": float(row.get("cost_price") or 0),
                    "buy_ts": "2026-09-20T01:30:00Z",
                    "last_ts": "2026-09-20T01:30:00Z",
                }
                for code, row in positions.items()
                if isinstance(row, Mapping)
            },
        )

    async def load_agent_ledger(tenant, user, agent):
        log["agent_ledger"].append((tenant, user, agent))
        return ledger

    def load_pool(day: str):
        log["pool"].append(day)
        return pool

    def read_snaps(client, codes):
        log["snaps"].append(sorted(codes))
        return snaps

    async def run_exec(*, db, **kwargs):
        log["exec"].append(kwargs)
        return outcome

    def write_watch(agent, plan):
        log["watch"].append({"agent": agent, "plan": plan})
        return WatchWriteResult(
            owner=f"llm:{agent}",
            plan=plan,
            armed=tuple(w.symbol for w in plan.rules),
        )

    async def write_ledger(db, records):
        log["ledger"].append(records)
        return len(records)

    async def is_trading_day(day):
        log.setdefault("trading_day", []).append(day)
        return trading_day

    deps = RoundDeps(
        account_user=over.pop("account_user", lambda: "10000001"),
        load_positions=over.pop("load_positions", load_positions),
        load_account=over.pop("load_account", load_account),
        load_agent_ledger=over.pop("load_agent_ledger", load_agent_ledger),
        load_pool=over.pop("load_pool", load_pool),
        load_excluded=over.pop(
            "load_excluded",
            lambda: ExclusionRead(symbols=frozenset(), present=True, note=""),
        ),
        quote_client=over.pop("quote_client", lambda: object()),
        read_snaps=over.pop("read_snaps", read_snaps),
        load_tier=over.pop("load_tier", lambda: tier),
        load_llm=over.pop(
            "load_llm", lambda: LLMBinding(model="fake-model", decide=_decide)
        ),
        open_db=over.pop("open_db", lambda: _DBCM()),
        run_exec=over.pop("run_exec", run_exec),
        write_watch=over.pop("write_watch", write_watch),
        write_ledger=over.pop("write_ledger", write_ledger),
        is_trading_day=over.pop("is_trading_day", is_trading_day),
        is_trading_time=over.pop("is_trading_time", lambda now: True),
        is_continuous_trading=over.pop("is_continuous_trading", None),
        real_enabled=over.pop("real_enabled", lambda: False),
        now=over.pop("now", lambda: NOW),
        # 默认不接线镜像探测 = 「探不到」（老替身/未接线），审计段落 None。
        # 要钉就绪段口径的用例自己传 ``mirror_ready=lambda: (False, "…")``。
        mirror_ready=over.pop("mirror_ready", None),
        # 默认无名册 = 单家路径（历史行为）。多模型用例自己传 ``roster=``。
        roster=over.pop("roster", None),
        # 默认不接线（= 历史行为：两个补块整段不渲染，提示词与旧版逐字节一致）。
        # 要钉补块口径的用例自己传 ``load_regime=`` / ``load_scores=``。
        load_regime=over.pop("load_regime", None),
        load_scores=over.pop("load_scores", None),
        # Stage 2 第二批注入点：默认 None = 未接线（丁1–丁9/丙1 整段不渲染，
        # 历史用例的提示词字节冻结）。要钉块口径的用例自己传同名关键字。
        load_news_files=over.pop("load_news_files", None),
        load_risk_files=over.pop("load_risk_files", None),
        read_series_summary=over.pop("read_series_summary", None),
        load_l2_snapshot=over.pop("load_l2_snapshot", None),
        load_decision_rows=over.pop("load_decision_rows", None),
        read_wake_ctx=over.pop("read_wake_ctx", None),
        # 镜像闸门/回注（P0 审计修）：默认 None = 未接线（两块整段不渲染）。
        read_mirror_ctx=over.pop("read_mirror_ctx", None),
        # 阶段 1：默认策略与默认档位同值（0.15/2），历史用例的闸门/提示词字节不变。
        load_trading_policy=over.pop(
            "load_trading_policy",
            lambda: TradingPolicy(
                default=AgentPolicy(per_stock_pct=0.15, max_new_buys=2)
            ),
        ),
    )
    assert not over, f"未消费的替身参数：{sorted(over)}"
    return Harness(deps=deps, log=log)


async def _boom_positions(tenant, user):
    raise RuntimeError("桥没回话")


def _exec_returning(outcome):
    """执行段替身：原样返回给定对象（用来换掉 ``Harness`` 默认的那个 ``FakeOutcome``）。"""

    async def run_exec(*, db, **kwargs):
        return outcome

    return run_exec


# ══ A. 槽位与到点（纯） ══════════════════════════════════════════════
def test_round_slot_rejects_bad_hhmm_and_schema():
    with pytest.raises(ValueError):
        RoundSlot("935", SCHEMA_INTRADAY)
    with pytest.raises(ValueError):
        RoundSlot("09:35", SCHEMA_INTRADAY)
    with pytest.raises(ValueError):
        RoundSlot("0935", "rebalance-2")


def test_due_slots_window_and_order():
    # 未到点不跑
    assert due_slots(datetime(2026, 9, 24, 8, 29, tzinfo=CST)) == ()
    # 09:00（窗到 09:45）与 09:35（窗到 10:20）在 09:35 重叠：两个都跑，按时刻升序
    assert [s.hhmm for s in due_slots(NOW)] == ["0900", "0935"]
    # 09:00 超窗（>45min）而 09:35 仍在窗内
    assert [s.hhmm for s in due_slots(TICK_NOW)] == ["0935"]
    # 10:05 的补跑槽在 10:47 仍可跑（10:00 已超窗 47 分钟）
    assert [s.hhmm for s in due_slots(TICK_1005)] == ["1005"]
    # 10:05 超窗（46min）而 11:00 未到点：无槽可跑（迟到太久就不补了）
    assert due_slots(datetime(2026, 9, 24, 10, 51, tzinfo=CST)) == ()


def test_due_slots_returns_each_slot_once_in_time_order():
    # 服务重启后一次 tick 可能要补多个槽：10:05 时刻 09:35/10:00/10:05 都在窗内
    assert [s.hhmm for s in due_slots(TEN_05)] == ["0935", "1000", "1005"]


def test_slot_keys_separate_schema_and_day():
    slot_key, done_key = slot_keys(DAY, SLOT_0935)
    assert slot_key == "trade:decision-round:slot:20260924:0935:rebalance"
    assert done_key == "trade:decision-round:done:20260924:rebalance"
    # done 键是 **schema 维度**：建仓轮出过决策不等于守护轮出过
    assert done_key != slot_keys(DAY, SLOT_0830)[1]


def test_slot_keys_segment_by_agent_only_when_named():
    """多模型名册（P2.9）：**agent 是槽位键与 done 键的一个维度**。

    不留空段是刻意的：空的 agent 逐字保留历史键形（今天的部署升级后不会突然
    「所有 done 键查无此人」⇒ 当天补跑槽集体重跑一轮真钱）。名册一开，每家的
    认领键与 done 键各自独立——一家的「今天跑过了」不许替另一家说话。
    """
    base_slot, base_done = slot_keys(DAY, SLOT_0935)
    pro_slot, pro_done = slot_keys(DAY, SLOT_0935, "deepseek-v4-pro")
    assert pro_slot == f"{base_slot}:deepseek-v4-pro"
    assert pro_done == f"{base_done}:deepseek-v4-pro"
    assert pro_slot != slot_keys(DAY, SLOT_0935, "deepseek-v4-flash")[0]
    assert slot_keys(DAY, SLOT_0935, "") == (base_slot, base_done)  # 空 = 历史键形


def test_select_runs_refuses_a_name_that_is_not_in_the_roster():
    """点名一家不在名册里的模型 = **参数错**，不是「那就跑全家」。

    静默跑全家是这里最坏的失败形态：运营以为在重跑一家，实际三家都下了单。
    """
    runs = (
        AgentRun(agent="a", load_llm=lambda: None),
        AgentRun(agent="b", load_llm=lambda: None),
    )
    assert [r.agent for r in select_runs(runs, "b")] == ["b"]
    assert [r.agent for r in select_runs(runs, "")] == ["a", "b"]  # 不点名 = 全员
    with pytest.raises(ValueError) as exc:
        select_runs(runs, "c")
    assert "c" in str(exc.value) and "a, b" in str(exc.value)


def test_round_id_shape_feeds_the_order_idempotency_key():
    assert round_id_for(DAY, SLOT_0935) == "rnd-20260924-0935"
    assert round_id_for(DAY, SLOT_1005) == "rnd-20260924-1005"


# ══ B. 位置戳 ════════════════════════════════════════════════════════
def test_build_pool_stamps_three_states():
    stamps = build_pool_stamps(
        kept=[pool_row_to_gate_row(BANK_ROW, 40.0)],
        dropped=[
            (
                pool_row_to_gate_row(PRICEY_ROW, 1700.0),
                Verdict("l1.unaffordable", "可用额度 7500 买不起一手（需 170000）"),
            )
        ],
        wanted=["600036.SH", "000001.SZ"],
    )
    assert stamps["600036.SH"]["state"] == "shown"
    assert stamps["600036.SH"]["shown"] is True
    assert stamps["600036.SH"]["rank"] == 1  # 位置戳带着池位置
    assert stamps["600519.SH"]["state"] == "dropped"
    assert stamps["600519.SH"]["rule"] == "l1.unaffordable"
    assert "买不起一手" in stamps["600519.SH"]["reason"]
    # 模型点了池外的票：给一条 off 戳；**没点**的池外票不出现（缺席 ≠ 池外）
    assert stamps["000001.SZ"] == {"state": "off", "shown": False}


def test_build_pool_stamps_skips_rows_without_code():
    stamps = build_pool_stamps(
        kept=[{"code": "", "rank": 1}],
        dropped=[({"name": "无码"}, Verdict("l2.pool_row_invalid", "缺 code"))],
        wanted=[""],
    )
    assert stamps == {}


def test_merge_outcomes_prefers_first_and_warns(caplog):
    merged = merge_outcomes(
        {0: {"armed": True, "reject_reason": ""}},
        {
            0: {"armed": False, "reject_reason": "冲突"},
            1: {"armed": False, "reject_reason": "x"},
        },
    )
    assert merged[0]["armed"] is True  # 先到的那一段留下
    assert merged[1]["reject_reason"] == "x"  # 不相交的序号照常合并
    assert any("审计序号冲突" in r.message for r in caplog.records)


# ══ B2. 本轮要点（▸ 段）：摘要/告警的人话行 ═════════════════════════════
# 用户 2026-10-15 原话：「光这些通知、通知了、不给建议吗」——决策轮摘要此前只有
# 计数（轮次/池/决策/腿/提交），零标的、零方向、零理由。要点行把「这一轮到底打算
# 做什么 / 做成了什么」压进摘要与告警：数据**全取本轮真件**（决策给标的与理由，
# 腿给股数与限价，回执给终态，否决给拦因），取不到的段就省——绝不替模型编理由；
# hold 与三无（无腿、无回执、无否决）决策不出行（条数在「决策 N」里）。
def _outcome(*, legs=(), vetoes=(), receipts=(), aborted="") -> ExecutionOutcome:
    """要点取数吃的执行段同形（真 ``ExecutionOutcome``：plan/receipts/aborted）。"""
    return ExecutionOutcome(
        round_id="rnd-20260924-0935",
        plan=ExecutionPlan(legs=tuple(legs), vetoes=tuple(vetoes)),
        receipts=tuple(receipts),
        aborted=aborted,
    )


def test_highlights_render_a_submitted_leg_with_qty_price_and_reason():
    decisions = (
        Decision(
            action=BUY, code="SH601857", name="中国石油", reason="放量突破 60 日新高"
        ),
    )
    leg = Leg(
        index=0,
        symbol="SH601857",
        side="buy",
        quantity=800,
        limit_price=12.33,
        reason="放量突破",
    )
    rec = LegReceipt(
        index=0,
        symbol="SH601857",
        side="buy",
        quantity=800,
        limit_price=12.33,
        success=True,
        order_id="o-1",
    )
    lines = NOTES.round_highlights(decisions, _outcome(legs=(leg,), receipts=(rec,)))
    assert lines == ["买入 中国石油(SH601857) 800股 @¥12.33（放量突破 60 日新高）"]


def test_highlights_name_the_veto_rule_for_a_blocked_decision():
    decisions = (Decision(action=SELL, code="SH601985", name="中国核电"),)
    veto = Veto(
        index=0,
        symbol="SH601985",
        side="sell",
        rule="l2.vcash",
        reason="可用资金不足",
    )
    lines = NOTES.round_highlights(decisions, _outcome(vetoes=(veto,)))
    assert lines == ["卖出 中国核电(SH601985)（被拦 l2.vcash：可用资金不足）"]


def test_highlights_render_failures_and_duplicates_as_such():
    decisions = (
        Decision(action=BUY, code="SH600036", name="招商银行", reason="r1"),
        Decision(action=BUY, code="SH600519", name="贵州茅台", reason="r2"),
    )
    legs = (
        Leg(
            index=0,
            symbol="SH600036",
            side="buy",
            quantity=100,
            limit_price=35.5,
            reason="r1",
        ),
        Leg(
            index=1,
            symbol="SH600519",
            side="buy",
            quantity=100,
            limit_price=1400.0,
            reason="r2",
        ),
    )
    receipts = (
        LegReceipt(
            index=0,
            symbol="SH600036",
            side="buy",
            quantity=100,
            limit_price=35.5,
            success=False,
            message="券商拒单：涨跌停价不可委托",
        ),
        LegReceipt(
            index=1,
            symbol="SH600519",
            side="buy",
            quantity=100,
            limit_price=1400.0,
            success=True,
            duplicate=True,
        ),
    )
    lines = NOTES.round_highlights(decisions, _outcome(legs=legs, receipts=receipts))
    assert lines[0] == (
        "买入 招商银行(SH600036) 100股 @¥35.50（失败：券商拒单：涨跌停价不可委托）"
    )
    assert lines[1] == (
        "买入 贵州茅台(SH600519) 100股 @¥1400.00（此前轮已在途，未重复提交）"
    )


def test_highlights_skip_hold_and_traceless_decisions():
    """hold 与三无决策不出行：它们**没有**「本来要做」可言（计数仍在「决策 N」里）。"""
    decisions = (
        Decision(action=HOLD, code="SH600000"),
        Decision(action=SELL, code="SH600036", name="招商银行", reason="减仓"),
    )
    assert NOTES.round_highlights(decisions, _outcome()) == []


def test_highlights_mark_planned_but_unsent_legs():
    """aborted 轮的腿（计划了、一张没发）：要点行要写「未发出」而不是沉默。"""
    decisions = (
        Decision(action=SELL, code="SH601985", name="中国核电", reason="跌破止损"),
    )
    leg = Leg(
        index=0,
        symbol="SH601985",
        side="sell",
        quantity=500,
        limit_price=9.18,
        reason="跌破止损",
    )
    lines = NOTES.round_highlights(
        decisions,
        _outcome(legs=(leg,), aborted="在途委托不可信：本轮不下单（fail-closed）"),
    )
    assert lines == ["卖出 中国核电(SH601985) 500股 @¥9.18（未发出）"]


def test_highlights_backfill_name_for_code_only_decision(monkeypatch):
    """2026-10-16：模型只给 code（无 name）时用 ``stock_display_name`` 补中文名。"""
    monkeypatch.setattr(
        "backend.shared.stock_display_name.resolve",
        lambda symbol, hint="": {"SH601985": "中国核电"}.get(
            str(symbol or "").strip().upper(), ""
        ),
    )
    decisions = (Decision(action=SELL, code="SH601985", reason="跌破止损"),)
    leg = Leg(
        index=0,
        symbol="SH601985",
        side="sell",
        quantity=500,
        limit_price=9.18,
        reason="跌破止损",
    )
    rec = LegReceipt(
        index=0,
        symbol="SH601985",
        side="sell",
        quantity=500,
        limit_price=9.18,
        success=True,
        order_id="o-2",
    )
    lines = NOTES.round_highlights(decisions, _outcome(legs=(leg,), receipts=(rec,)))
    assert lines == ["卖出 中国核电(SH601985) 500股 @¥9.18（跌破止损）"]


def test_highlights_code_only_unresolvable_degrades_to_display_code(monkeypatch):
    """名字解析不到时只显示规范代码（后缀→前缀），绝不编名。"""
    monkeypatch.setattr(
        "backend.shared.stock_display_name.resolve", lambda symbol, hint="": ""
    )
    decisions = (Decision(action=SELL, code="600036.SH", reason="减仓"),)
    leg = Leg(
        index=0,
        symbol="600036.SH",
        side="sell",
        quantity=100,
        limit_price=35.5,
        reason="减仓",
    )
    rec = LegReceipt(
        index=0,
        symbol="600036.SH",
        side="sell",
        quantity=100,
        limit_price=35.5,
        success=True,
        order_id="o-3",
    )
    lines = NOTES.round_highlights(decisions, _outcome(legs=(leg,), receipts=(rec,)))
    assert lines == ["卖出 SH600036 100股 @¥35.50（减仓）"]


def test_highlights_render_watch_rules_with_the_stops():
    decisions = (
        Decision(
            action=WATCH,
            code="SH601985",
            name="中国核电",
            stop_loss=9.2,
            take_profit=10.6,
        ),
    )
    assert NOTES.round_highlights(decisions, _outcome()) == [
        "守望 中国核电(SH601985)（止损 9.20，止盈 10.60）"
    ]


def test_highlights_cap_and_roll_up_the_remainder():
    decisions = tuple(
        Decision(action=BUY, code=f"SH60010{i}", name=f"股{i}", reason="r")
        for i in range(7)
    )
    legs = tuple(
        Leg(
            index=i,
            symbol=f"SH60010{i}",
            side="buy",
            quantity=100,
            limit_price=10.0,
            reason="r",
        )
        for i in range(7)
    )
    receipts = tuple(
        LegReceipt(
            index=i,
            symbol=f"SH60010{i}",
            side="buy",
            quantity=100,
            limit_price=10.0,
            success=True,
        )
        for i in range(7)
    )
    lines = NOTES.round_highlights(decisions, _outcome(legs=legs, receipts=receipts))
    assert len(lines) == 6  # 5 条 + 汇总条
    assert lines[4].startswith("买入 股4(SH600104)")
    assert lines[-1] == "…另有 2 条（见审计行）"


def test_highlights_are_empty_when_the_outcome_gives_nothing():
    """取数纪律 + 兜底：没有 plan 的执行段（老形态/替身）取不到就不编；None 也不抛。"""
    decisions = (Decision(action=BUY, code="SH601857", name="中国石油", reason="x"),)
    assert NOTES.round_highlights(decisions, FakeOutcome()) == []
    assert NOTES.round_highlights(decisions, None) == []


@pytest.mark.asyncio
async def test_ok_round_meta_carries_the_execution_highlights(monkeypatch):
    """接线（OK 路径）：meta 带上 highlights——摘要/QQ 的 ▸ 段据此说人话。"""
    monkeypatch.setattr(
        "backend.shared.stock_display_name.resolve",
        lambda symbol, hint="": {"600036.SH": "招商银行"}.get(
            str(symbol or "").strip().upper(), ""
        ),
    )
    outcome = ExecutionOutcome(
        round_id="rnd-20260924-0935",
        plan=ExecutionPlan(
            legs=(
                Leg(
                    index=0,
                    symbol="600036.SH",
                    side="buy",
                    quantity=100,
                    limit_price=35.5,
                    reason="加仓",
                ),
            )
        ),
        receipts=(
            LegReceipt(
                index=0,
                symbol="600036.SH",
                side="buy",
                quantity=100,
                limit_price=35.5,
                success=True,
                order_id="o-1",
            ),
        ),
    )
    h = make_harness(
        decision_text=decisions_json(
            {"action": "buy", "code": "600036.SH", "pct": 0.2, "reason": "加仓"}
        ),
        outcome=None,
    )
    result = await run_once(
        SLOT_0935, deps=replace(h.deps, run_exec=_exec_returning(outcome)), now=NOW
    )
    assert result.status == R.STATUS_OK
    assert result.meta["highlights"] == [
        "买入 招商银行(SH600036) 100股 @¥35.50（加仓）"
    ]


@pytest.mark.asyncio
async def test_aborted_round_meta_carries_the_planned_highlights(monkeypatch):
    """接线（aborted 路径）：计划了腿却没发——要点行标注「未发出」，不留白。"""
    monkeypatch.setattr(
        "backend.shared.stock_display_name.resolve",
        lambda symbol, hint="": {"600036.SH": "招商银行"}.get(
            str(symbol or "").strip().upper(), ""
        ),
    )
    outcome = ExecutionOutcome(
        round_id="rnd-20260924-0935",
        plan=ExecutionPlan(
            legs=(
                Leg(
                    index=0,
                    symbol="600036.SH",
                    side="buy",
                    quantity=100,
                    limit_price=35.5,
                    reason="加仓",
                ),
            )
        ),
        aborted="在途账读不到：本轮不提交腿",
    )
    h = make_harness(
        decision_text=decisions_json(
            {"action": "buy", "code": "600036.SH", "pct": 0.2, "reason": "加仓"}
        ),
        outcome=None,
    )
    result = await run_once(
        SLOT_0935, deps=replace(h.deps, run_exec=_exec_returning(outcome)), now=NOW
    )
    assert result.status == R.STATUS_ABORTED
    assert result.meta["highlights"] == [
        "买入 招商银行(SH600036) 100股 @¥35.50（未发出）"
    ]


# ══ C. 闸门行 ↔ 池行往返（「功能不能少」的守卫） ═══════════════════════
@pytest.mark.parametrize(
    ("holdings", "market_value", "total_asset", "expect_issue"),
    [
        (2, 120000.0, 170000.0, False),  # 有持仓：这一条闸门不管
        (0, 0.0, 50000.0, False),  # 全现金账户：正常形态
        (0, None, 50000.0, False),  # 市值读不到 ⇒ 交给「资金面不可信」那条闸门
        (0, 100.0, 20000.0, False),  # 0.5% 的零头：不判
        (0, 200.0, 20000.0, False),  # 恰好 1%（闭区间端点）：仍不判
        (0, 201.0, 20000.0, True),  # 略高于 1%：判
        (0, 120000.0, 170000.0, True),  # 空持仓 + 七成市值：持仓面不可信
        # 总资产缺失时**拿市值自己当分母**：任何非零市值都判矛盾。「判不了就不判」
        # 在这里等于放行一次「拿着 12 万持仓当空仓」的决策——恰恰是最贵的那种错。
        (0, 120000.0, None, True),
    ],
)
def test_positions_consistency_issue_boundaries(
    holdings, market_value, total_asset, expect_issue
):
    got = positions_consistency_issue(
        holdings=holdings, market_value=market_value, total_asset=total_asset
    )
    assert (got != "") is expect_issue
    if expect_issue:
        assert "持仓面与资金面自相矛盾" in got


def test_positions_consistency_issue_renders_both_numbers():
    """理由里必须带上两个数：只说「矛盾」的话，运营无法判断是不是桥侧掉了。"""
    got = positions_consistency_issue(
        holdings=0, market_value=120000.0, total_asset=170000.0
    )
    assert "120,000" in got and "170,000" in got and "70.6%" in got


def test_positions_consistency_issue_says_which_source_state_emptied_the_table():
    """空持仓的成因要指向**下一步查哪里**——三种成因的排查方向完全不同：

    源读到 0 行（源侧链路）／读到行但全部停更（新鲜度窗口或时钟）／读到行但没有
    一行的代码能识别（本仓 normalize 口径——源其实是好的）。一句笼统的「持仓链路
    断了」会把最后一种引到错处。
    """
    kw = {"holdings": 0, "market_value": 120000.0, "total_asset": 170000.0}
    zero = positions_consistency_issue(
        **kw, sources={"tdx_bridge": {"positions": 0, "stale": False}}
    )
    assert "读到 0 行" in zero

    all_stale = positions_consistency_issue(
        **kw, sources={"qmt_exec": {"positions": 12, "stale": True}}
    )
    assert "12 行" in all_stale and "全部被判停更" in all_stale

    no_codes = positions_consistency_issue(
        **kw, sources={"qmt_exec": {"positions": 7, "stale": False}}
    )
    assert "7 行" in no_codes and "symbol 口径" in no_codes

    # 源摘要读不出（不是映射 / 行数不是数字）：**数不动就如实说数不动**，不许猜成
    # 0 行（那会把排查引向源侧链路），也不许炸出去（abort 的理由文本正是这一轮唯一
    # 说得清的东西）
    odd = positions_consistency_issue(**kw, sources={"weird": "not-a-mapping"})
    assert "weird" in odd and "读不出" in odd
    unreadable = positions_consistency_issue(
        **kw, sources={"weird": {"positions": "nan?"}}
    )
    assert "weird" in unreadable and "读不出" in unreadable
    # 有源读得出来、也有源读不出：结论照给，但**读不出的那处必须仍然可见**
    mixed = positions_consistency_issue(
        **kw, sources={"qmt_exec": {"positions": 7}, "weird": {"positions": "nan?"}}
    )
    assert "7 行" in mixed and "另有源的行数读不出：weird" in mixed
    assert position_source_meta(None) == {} and position_source_meta("nope") == {}
    assert position_source_meta({"sources": {"s": 1}}) == {"s": 1}


def test_positions_consistency_issue_labels_the_denominator_honestly():
    """总资产读不到时**不许**写「占总资产的 X%」——那个分母是市值自己（=100%）。

    拿市值跟自己比出来的百分比会被读成「七成仓位」，而真相是「分母缺失」。
    """
    without = positions_consistency_issue(
        holdings=0, market_value=120000.0, total_asset=None
    )
    assert "总资产读不到" in without and "非占比" in without
    assert "占总资产" not in without

    with_ta = positions_consistency_issue(
        holdings=0, market_value=120000.0, total_asset=170000.0
    )
    assert "占总资产 170,000 的 70.6%" in with_ta


def test_gate_row_roundtrip_keeps_every_prompt_column():
    """闸门只认 code/name/price，但筛一遍池子不该把行业/理由/排名筛没。"""
    row = pool_row_to_gate_row(BANK_ROW, 40.0)
    assert row["price"] == 40.0
    assert gate_row_to_pool_row(row) == BANK_ROW


def test_gate_row_to_pool_row_normalizes_none_to_empty_text():
    back = gate_row_to_pool_row({"code": "600036.SH", "name": None, "remark": None})
    assert back.name == "" and back.remark == ""  # 不渲染出字面的 None
    assert back.score is None  # 分数缺失仍是 None（渲染成 —）


# ══ D. 小工具 ════════════════════════════════════════════════════════
def test_as_float_rejects_dirty_values():
    assert as_float("12.5") == 12.5
    assert as_float(None) is None and as_float("") is None
    assert as_float(True) is None  # bool 不是数字
    assert as_float(float("nan")) is None and as_float(float("inf")) is None
    assert as_float([]) is None  # float() 直接抛 → 也是 None，不许带异常出去


def test_tier_numbers_reads_only_real_numbers():
    """档位文档是外来的：脏值要退成 ``None``（用默认值），不能变成预算。"""

    def tier(budget):
        return type("T", (), {"budget": budget})()

    assert tier_numbers(tier({"per_stock_pct": 0.15, "max_new_buys": 2})) == (0.15, 2)
    assert tier_numbers(tier({})) == (None, None)  # absent：从未配置
    assert tier_numbers(tier(None)) == (None, None)
    assert tier_numbers(tier({"per_stock_pct": "0.5", "max_new_buys": 2.9})) == (
        None,
        2,
    )  # 字符串不是数字；2.9 只取整（取整方向由闸门自己管）
    assert tier_numbers(tier({"per_stock_pct": True, "max_new_buys": False})) == (
        None,
        None,
    )  # bool 是 int 的子类：不挡会变成 pct=1.0（满仓）


def test_agent_ledger_quota_is_grant_minus_position_cost():
    """授予额度口径：总量取 ``TradingPolicy`` 注入的 ``quota``，
    已用 = 名下持仓成本（卖出即释放）。

    ``virtual_cash`` 不参与「已用」——它是账上现钱（含已实现盈亏），
    与「买了多少货」是两个数（见 ``agent_ledger.agent_used`` 的 docstring）。
    """
    ledger = AgentLedgerRead(
        ok=True,
        known=True,
        positions={
            "600036.SH": {"volume": 1000, "cost_price": 38.0},
            "000001.SZ": {"volume": 4000, "cost_price": 10.05},
        },
        virtual_cash=1.0,
        quota=200_000.0,
    )
    assert ledger.quota_total == 200_000.0
    assert ledger.quota_used == 78_200.0  # 38000 + 40200
    assert ledger.quota_remaining == 121_800.0


def test_agent_ledger_quota_falls_back_to_default_for_legacy_callers():
    from backend.shared.decision.agent_ledger import DEFAULT_AGENT_QUOTA

    assert AgentLedgerRead().quota_total == DEFAULT_AGENT_QUOTA


def test_agent_ledger_quota_skips_dirty_rows_instead_of_turning_nan():
    """一行脏账本不许把整轮额度算成 NaN：NaN 参与比较恒 False = 放开所有闸门。"""

    ledger = AgentLedgerRead(
        ok=True,
        known=True,
        positions={
            "600036.SH": {"volume": 1000, "cost_price": 38.0},
            "000001.SZ": {"volume": float("nan"), "cost_price": 10.0},
            "000002.SZ": {"volume": 100, "cost_price": float("inf")},
            "000003.SZ": {"volume": "坏值", "cost_price": 10.0},
            "000004.SZ": "不是 dict",
        },
    )
    assert ledger.quota_used == 38_000.0
    assert ledger.quota_remaining == 62_000.0


def test_snap_price_prefers_standard_key_and_rejects_nonpositive():
    assert snap_price({"Now": 40.0, "now": 39.0}) == 40.0
    assert snap_price({"now": 39.0}) == 39.0  # 原始推送字段兜底
    assert snap_price({"Now": 0}) is None  # 0 = 没采到，不是「免费」
    assert snap_price(None) is None


@pytest.mark.asyncio
async def test_refusing_submitter_returns_a_receipt_shaped_refusal():
    submit = refusing_submitter("非交易时段")
    out = await submit(object(), "cid", False)
    assert out.success is False and out.duplicate is False and out.mirror is None
    assert "非交易时段" in out.message


def test_round_result_as_status_shape():
    result = RoundResult(
        status=R.STATUS_OK,
        day=DAY,
        slot=SLOT_0935,
        round_id="rnd-20260924-0935",
        agent="fake-model",
        mode="sim",
        decisions=2,
        legs=1,
        submitted=1,
        meta={"pool": {"shown": 1, "dropped": 0}},
    )
    payload = result.as_status(at=NOW)
    assert payload["slot"] == "0935" and payload["slot_label"] == "09:35"
    assert payload["schema"] == SCHEMA_REBALANCE
    assert payload["status"] == "ok" and payload["errors"] == []
    assert payload["pool"]["shown"] == 1  # meta 平铺进状态键
    assert payload["ts"].startswith("2026-09-24T09:35")


# ══ E. run_once 编排（全替身） ══════════════════════════════════════
@pytest.mark.asyncio
async def test_run_once_aborts_on_empty_account_user():
    h = make_harness(account_user=lambda: "")
    result = await run_once(SLOT_0935, deps=h.deps, now=NOW)
    assert result.status == R.STATUS_ABORTED and "账户身份" in result.note
    assert h.log["llm"] == [] and h.log["exec"] == []  # 不问模型也不下单


@pytest.mark.asyncio
async def test_run_once_aborts_when_positions_unreadable():
    h = make_harness()
    result = await run_once(
        SLOT_0935, deps=replace(h.deps, load_positions=_boom_positions), now=NOW
    )
    assert result.status == R.STATUS_ABORTED
    assert "持仓读取失败" in result.note
    assert h.log["llm"] == []  # 模型看不到持仓就不该动它


@pytest.mark.asyncio
async def test_empty_positions_on_a_cash_account_is_not_a_failure():
    """全现金账户（空持仓 **且** 市值≈0）是正常状态——只有读**失败**才 abort。"""
    h = make_harness(
        positions={},
        account=AccountRead(
            ok=True,
            cash=50000.0,
            market_value=0.0,
            total_asset=50000.0,
            source="tdx_bridge",
            snapshot_at="2026-09-24T09:30:00+08:00",
            age_min=5.0,
        ),
    )
    result = await run_once(SLOT_1005, deps=h.deps, now=TEN_05)
    assert result.status == R.STATUS_OK
    assert h.log["llm"] != []


@pytest.mark.asyncio
async def test_run_once_aborts_when_empty_holdings_contradict_market_value():
    """持仓表为空而市值占总资产七成 ⇒ **持仓面不可信**（不是空仓），本轮不做。

    默认替身账户：市值 12 万 / 总资产 17 万（70.6%）且持仓表为空——两面对不上，
    照此决策会「该卖的没卖、不该买的买了」。空仓的正常形态见上一条用例。
    """
    h = make_harness(
        positions={},
        # 源侧真的读到 1 行、未停更，但并进来 0 行 ⇒ 归因必须指向 symbol 口径
        # （另外两种成因的文案见 core 的单测；文案给错会把排查引到别的链路上）
        pos_meta={"sources": {"tdx_bridge": {"positions": 1, "stale": False}}},
    )
    result = await run_once(SLOT_0935, deps=h.deps, now=NOW)
    assert result.status == R.STATUS_ABORTED
    assert "持仓面与资金面自相矛盾" in result.note
    assert "读到 1 行但无一行有可识别代码" in result.note
    assert result.meta["account"]["source"] == "tdx_bridge"
    assert result.meta["positions"] == {
        "sources": {"tdx_bridge": {"positions": 1, "stale": False}}
    }
    assert h.log["llm"] == [] and h.log["exec"] == [] and h.log["ledger"] == []


@pytest.mark.asyncio
async def test_run_once_aborts_when_account_numbers_untrusted():
    h = make_harness(
        account=AccountRead(
            ok=False, cash=None, market_value=1.0, errors=("账户快照缺 cash 列值",)
        )
    )
    result = await run_once(SLOT_0935, deps=h.deps, now=NOW)
    assert result.status == R.STATUS_ABORTED
    assert "资金面不可信" in result.note and "cash" in result.note
    assert h.log["llm"] == [] and h.log["ledger"] == []


@pytest.mark.asyncio
async def test_run_once_aborts_on_illegal_gate_pct_before_asking_llm():
    """策略写成 15（而非 0.15）：夹取会变成空操作，一笔打满额——不许继续。"""
    h = make_harness(
        load_trading_policy=lambda: TradingPolicy(
            default=AgentPolicy(per_stock_pct=15)
        )
    )
    result = await run_once(SLOT_0935, deps=h.deps, now=NOW)
    assert result.status == R.STATUS_ABORTED and "闸门参数非法" in result.note
    assert h.log["llm"] == []


@pytest.mark.asyncio
async def test_run_once_llm_not_configured_is_llm_failed_without_audit():
    def boom():
        raise RuntimeError("LLMNotConfigured: 缺 DECISION_LLM_API_KEY")

    h = make_harness(load_llm=boom)
    result = await run_once(SLOT_0935, deps=h.deps, now=NOW)
    assert result.status == R.STATUS_LLM_FAILED and "未就绪" in result.note
    assert h.log["exec"] == [] and h.log["ledger"] == []


@pytest.mark.asyncio
async def test_run_once_parse_failure_writes_nothing_so_catch_up_can_retry():
    h = make_harness(decision_text="今天没什么好说的")  # 无 JSON → parse_failed
    result = await run_once(SLOT_0935, deps=h.deps, now=NOW)
    assert result.status == R.STATUS_LLM_FAILED
    assert h.log["exec"] == [] and h.log["ledger"] == [] and h.log["watch"] == []
    assert result.meta["calls"] >= 1  # 重试次数可见


@pytest.mark.asyncio
async def test_run_once_truncated_parse_failure_names_truncation_and_no_reason():
    """顶格截断 + 取不出 JSON：note 要指认「被切了」，且光杆冒号有兜底文案。"""
    h = make_harness(decision_text="今天没什么好说的", finish_reason="length")
    result = await run_once(SLOT_0935, deps=h.deps, now=NOW)
    assert result.status == R.STATUS_LLM_FAILED
    assert "未报告原因" in result.note  # parse_failed 无 error 条目：不许留空冒号
    assert "顶格截断" in result.note
    assert h.log["exec"] == [] and h.log["ledger"] == []


@pytest.mark.asyncio
async def test_run_once_truncation_on_success_round_lands_in_notes(arena_root):
    """解析成功但输出被切过：轮照跑，审计 notes 留下截断事实（对话尾部可能就是被切的）。"""
    h = make_harness(
        decision_text=decisions_json(
            {"action": "buy", "code": "600036.SH", "pct": 0.2, "reason": "加仓"}
        ),
        finish_reason="length",
    )
    result = await run_once(SLOT_0935, deps=h.deps, now=NOW)

    assert result.status == R.STATUS_OK  # 截断不动轮次状态
    notes = h.log["ledger"][0][0].context_meta["notes"]
    assert any("顶格截断" in n for n in notes), notes


@pytest.mark.asyncio
async def test_run_once_happy_path_wires_round_quota_and_audit():
    h = make_harness(
        decision_text=decisions_json(
            {"action": "buy", "code": "600036.SH", "pct": 0.2, "reason": "加仓"}
        )
    )
    result = await run_once(SLOT_0935, deps=h.deps, now=NOW)

    assert result.status == R.STATUS_OK
    assert (result.round_id, result.agent, result.mode) == (
        "rnd-20260924-0935",
        "fake-model",
        "sim",
    )
    assert (result.decisions, result.legs, result.submitted, result.audit_rows) == (
        1,
        1,
        1,
        1,
    )
    assert h.log["pool"] == ["20260924"]  # ISO 日期会静默拿到 None：必须是 %Y%m%d
    assert h.log["account"] == [("default", "10000001")]
    assert h.log["positions"] == [("default", "10000001")]

    sent = h.log["exec"][0]
    assert sent["round_id"] == "rnd-20260924-0935"  # 进腿幂等键的 round 段
    # 额度：授予额度剩余 100,000−38,000=62,000（与虚拟现金 100,000 取更紧者）
    assert sent["quota"] == 62000.0
    # ``new_buys_round`` 是「本轮**已**产生的新开仓数」：单轮一次规划，调用前必为 0，
    # 计数在 ``_Plan`` 内过闸自增。把档位上限本身传进来 = 首单即 ``used >= cap``，
    # **每一笔**新开仓都被「已达上限」全否（2026-10-09 实盘 13:00 轮 legs=0 实锤）。
    assert sent["new_buys_round"] == 0
    assert sent["gate"].max_new_buys_round == 2  # 档位 max_new_buys 覆盖上下文默认 3
    assert sent["agent"] == "fake-model"  # 模型名进幂等键
    assert sent["real"] is False and sent["submitter"] is None  # 时段内、模拟模式
    assert sent["inflight"] is None  # 时段内发单 → 在途账由 run_round 自己读
    assert sent["tenant_id"] == "default" and sent["user_id"] == "10000001"
    assert sent["gate"] is not None  # 闸门随轮次下传（买入侧四条缺失闸在这层生效）
    assert list(sent["holdings"]) == ["600036.SH"]  # 执行段持仓键 = 后缀式

    records = h.log["ledger"][0]
    assert len(records) == 1
    meta = records[0].context_meta
    assert meta["round"]["round_id"] == "rnd-20260924-0935"
    assert meta["round"]["model"] == "fake-model" and meta["round"]["mode"] == "sim"
    assert meta["quota"] == {
        "total": 100_000.0,
        "used": 38_000.0,
        "per_stock_pct": 0.15,
        "max_new_buys": 2,
    }
    assert meta["account"]["source"] == "tdx_bridge"
    assert meta["account"]["age_min"] == 5.0
    assert meta["account"]["broker"] == ""  # 替身没给券商：宁可为空，不许编一个
    assert meta[
        "sources"
    ] == {  # 持仓源面（空持仓时的归因依据）：生产同形 per-source dict
        "tdx_bridge": {
            "snapshot_at": "2026-09-24T09:30:00+08:00",
            "positions": 1,
            "stale": False,
            "lag_min": 0.0,
            "active_broker": True,
        }
    }
    assert meta["tier"] == {"level": "normal", "source": "doc"}
    assert meta["quotes"]["client"] is True
    # pool_ctx 是**这一行自己的**位置戳（按原始写法代码对上号），不是按代码分组的映射
    assert records[0].pool_ctx["state"] == "shown" and records[0].pool_ctx["rank"] == 1


@pytest.mark.asyncio
async def test_run_once_prompt_carries_the_granted_quota_not_the_account_total():
    """提示词首行只认**授予额度**：¥100,000 / 已用 ¥38,000 / 剩余 ¥62,000。

    真实账户三数（170,000 / 120,000 / 50,000）**一个字都不许进提示词**——
    账户里有多少钱是账户主人的隐私（2026-10-19 用户拍板）。
    """
    h = make_harness()
    await run_once(SLOT_0935, deps=h.deps, now=NOW)
    prompt = h.log["llm"][0]["prompt"]
    assert "100,000" in prompt and "38,000" in prompt and "62,000" in prompt
    assert "170,000" not in prompt
    assert "120,000" not in prompt
    assert "50,000" not in prompt


@pytest.mark.asyncio
async def test_run_once_uses_the_injected_trading_policy_for_every_layer():
    """阶段 1 G1/G3：额度/单票/新开仓/池外/盈利优先全部来自同一份策略。

    提示词首行、``BuyGate``、执行段 quota 三处必须是同一组数——任一处分叉都是
    2026-10-28 之前「五层各一份配置」的旧病。
    """
    h = make_harness(
        ledger=AgentLedgerRead(
            ok=True, known=False, virtual_cash=200_000.0, quota=200_000.0
        ),
        load_trading_policy=lambda: TradingPolicy(
            default=AgentPolicy(
                quota=200_000.0,
                per_stock_pct=1.0,
                max_new_buys=9,
                allow_out_of_pool=True,
                profit_first=True,
                score_leaders_limit=20,
            )
        ),
    )
    await run_once(SLOT_0935, deps=h.deps, now=NOW)

    prompt = h.log["llm"][0]["prompt"]
    assert "200,000" in prompt and "≤100%" in prompt and "≤9 只" in prompt
    assert "池外 A 股也可买" in prompt and "【盈利优先】" in prompt
    sent = h.log["exec"][0]
    assert sent["quota"] == 200_000.0
    assert sent["gate"].per_stock_pct == 1.0
    assert sent["gate"].max_new_buys_round == 9
    assert sent["gate"].allow_out_of_pool is True


@pytest.mark.asyncio
async def test_run_once_filters_the_pool_by_single_stock_budget():
    """单票预算 = 剩余额度 × pct = 50000×0.15 = 7500：一手茅台 17 万被剔出视野。"""
    h = make_harness()
    await run_once(SLOT_0935, deps=h.deps, now=NOW)
    prompt = h.log["llm"][0]["prompt"]
    assert "主力净流入" in prompt  # 保留的行带着它的理由进了表
    assert "预算外-勿推" not in prompt  # 被 l1.unaffordable 剔掉
    meta = h.log["ledger"][0][0].context_meta
    assert meta["pool"]["shown"] == 1 and meta["pool"]["dropped"] == 1
    assert meta["pool"]["dropped_rules"] == ["l1.unaffordable"]


@pytest.mark.asyncio
async def test_run_once_pool_missing_still_runs_and_leaves_a_trace():
    h = make_harness(pool=None)
    result = await run_once(SLOT_0935, deps=h.deps, now=NOW)
    assert result.status == R.STATUS_OK  # 守护轮不依赖池：没有池也要跑
    meta = h.log["ledger"][0][0].context_meta
    assert meta["pool"]["file"] == "" and meta["pool"]["shown"] == 0


# ── 零输出可见性（2026-10-10 审计 H3/M10）：轮终 meta/note 的取证字段 ──
# 判据在 ``decision_round_alerts``；这里钉的是**取数**：轮次必须把
# ``pool.rows``（供给面原始行数）与 ``in_session`` 显式写进 meta，
# 读侧才能区分「真的是 0」与「取不到」；note 不再对零产出说「提交完成」。
@pytest.mark.asyncio
async def test_bearish_cleared_pool_marks_the_note_as_designed_empty():
    """看空清空：note 写「空池（看空清空）」+ meta 带 rows=0/direction——
    与「供给链断了」在状态键上从此可区分（旧版两者都是一句「提交完成」）。"""
    cleared = PoolDoc(
        rows=(),
        direction=DirectionBlock(direction="看空", total_score=-2.5),
        missing_columns=(),
        source="/data/reports/stock_picks/20260924_picks.json",
    )
    h = make_harness(pool=cleared, outcome=FakeOutcome(legs=0, submitted=0))
    result = await run_once(SLOT_0935, deps=h.deps, now=NOW)

    assert result.status == R.STATUS_OK and result.submitted == 0
    assert "空池（看空清空）" in result.note and "看空" in result.note
    assert result.meta["pool"]["rows"] == 0
    assert result.meta["pool"]["direction"] == "看空"
    assert result.meta["pool"]["total_score"] == -2.5
    assert result.meta["in_session"] is True


@pytest.mark.asyncio
async def test_in_session_zero_submit_note_says_zero_legs():
    """时段内零腿：note 必须显式带「（0 腿）」——旧版与正常完成同为「提交完成」。"""
    h = make_harness(outcome=FakeOutcome(legs=0, submitted=0))
    result = await run_once(SLOT_0935, deps=h.deps, now=NOW)

    assert result.status == R.STATUS_OK
    assert result.note == "提交完成（0 腿）"
    assert result.meta["pool"]["rows"] == 2  # 池非空：这是「有票却没出去」那一类
    assert result.meta["in_session"] is True


@pytest.mark.asyncio
async def test_pool_missing_meta_carries_rows_zero_and_the_empty_file():
    """缺池轮：meta 键**在且为 0**（告警判据的「真的是 0」），file 为空可再分因。"""
    h = make_harness(pool=None, outcome=FakeOutcome(legs=0, submitted=0))
    result = await run_once(SLOT_0935, deps=h.deps, now=NOW)

    assert result.status == R.STATUS_OK
    assert result.meta["pool"]["rows"] == 0
    assert result.meta["pool"]["file"] == ""
    assert result.meta["in_session"] is True


@pytest.mark.asyncio
async def test_submitted_round_keeps_the_plain_note():
    """正向对照：有腿提交的轮次 note 一字不改（本次改动不许波及正常路径）。"""
    h = make_harness()  # 默认替身 outcome：legs=1, submitted=1
    result = await run_once(SLOT_0935, deps=h.deps, now=NOW)
    assert result.status == R.STATUS_OK and result.submitted == 1
    assert result.note == "提交完成"


@pytest.mark.asyncio
async def test_run_once_pool_missing_without_quotes_does_not_trip_the_quote_gate():
    """行情闸门判的是**池面**：没有池文件时「一只价都没有」无从谈起，不许据此 abort。

    缺池 + 缺行情同时发生是盘前常态（池文件还没生成、行情服务器还没起）。此时
    持仓与守护规则仍要靠它跑——把闸门挂在「行情可用性」而不是「池里无可用价」上，
    会让这类轮次整片消失。
    """
    h = make_harness(pool=None, quote_client=lambda: None)
    result = await run_once(SLOT_0935, deps=h.deps, now=NOW)
    assert result.status == R.STATUS_OK
    assert h.log["llm"] != []


@pytest.mark.asyncio
async def test_partially_unpriced_pool_still_runs_and_says_so():
    """只有部分无价 ⇒ 照跑（有价的那只仍是候选），但「谁没进模型视野」必须留痕。

    「池里 N 只没进视野」与「模型没选它们」在审计里长得一模一样，不留痕时归因会
    指向模型。
    """
    h = make_harness(snaps={"600036.SH": SNAPS["600036.SH"]})  # 茅台无价
    result = await run_once(SLOT_0935, deps=h.deps, now=NOW)
    assert result.status == R.STATUS_OK
    meta = h.log["ledger"][0][0].context_meta
    assert meta["pool"]["shown"] == 1 and meta["pool"]["dropped"] == 1
    assert any("1/2 只无可用现价" in n for n in meta["notes"])


@pytest.mark.asyncio
async def test_all_dropped_pool_warning_splits_no_price_from_unaffordable(caplog):
    """全池被剔光时的 warning 要**按规则分流**：缺现价（行情链路）与买不起一手
    （预算/档位）排查方向完全不同，一句「全部未过闸」会把人引向错处。

    池面：茅台有价但一手 17 万 > 单票预算 7,500 ⇒ ``l1.unaffordable``；另一只
    压根没价 ⇒ ``l2.pool_row_invalid``。两只都剔光但**原因不同**（有一只有价
    就不触发行情闸门——那条判的是「一只价都取不到」）。
    """
    unpriced = PoolRow(
        code="000001.SZ",
        name="平安银行",
        industry="银行",
        score=1.0,
        fusion=0.6,
        rank=2,
        remark="无价哨兵",
    )
    h = make_harness(
        pool=pool_doc(PRICEY_ROW, unpriced),
        snaps={"600519.SH": SNAPS["600519.SH"]},  # 只有茅台有价
    )
    with caplog.at_level("WARNING"):
        result = await run_once(SLOT_0935, deps=h.deps, now=NOW)
    hit = [r for r in caplog.records if "全部未过闸" in r.getMessage()]
    assert hit, "整池剔光必须留一条 warning（否则归因指向模型）"
    msg = hit[0].getMessage()
    assert "缺可用现价 1 只" in msg and "买不起一手 1 只" in msg
    assert result.status == R.STATUS_OK  # 有价 ⇒ 不是行情闸门那条，照常出决策
    assert h.log["ledger"][0][0].context_meta["pool"]["shown"] == 0


@pytest.mark.asyncio
async def test_run_once_aborts_in_session_when_not_a_single_pool_row_has_a_price():
    """池里一只价都取不到 **且** 在交易时段 ⇒ 本轮不做（不是降级）。

    模型只能看到空池，回报必然是「全部持有」——那不是决策，是把「行情断了」写成
    「今天不建仓」。而 ``round_tick`` 按 ``result.ok`` 置 done 键，当天 10:05/11:05
    两个补跑槽会全部「当日已出过决策」跳过：**一次行情抖动吃掉当天全部建仓轮**，
    状态键/日志/CLI 退出码三面却全绿。
    """
    h = make_harness(quote_client=lambda: None)
    result = await run_once(SLOT_0935, deps=h.deps, now=NOW)
    assert result.status == R.STATUS_ABORTED
    assert "全部无可用现价" in result.note
    assert "行情客户端未配置" in result.note  # 原因写清楚：客户端没造出来
    assert h.log["snaps"] == []  # 没客户端就不去读
    assert h.log["llm"] == [] and h.log["exec"] == [] and h.log["watch"] == []
    assert result.meta["quotes"] == {"client": False, "rows": 0}
    assert result.meta["pool"]["rows"] == 2  # 池面可查：2 只一条价都没有


@pytest.mark.asyncio
async def test_run_once_quote_read_failure_aborts_in_session_with_the_cause():
    """读失败与「没客户端」在 abort 理由里必须分得开（一个查配置、一个查链路）。"""
    h = make_harness()

    def boom(client, codes):
        raise RuntimeError("远端行情不通")

    result = await run_once(SLOT_0935, deps=replace(h.deps, read_snaps=boom), now=NOW)
    assert result.status == R.STATUS_ABORTED
    assert "行情快照读取失败" in result.note and "远端行情不通" in result.note
    assert result.meta["quotes"]["client"] is True  # 客户端在，是读的时候断的
    assert h.log["llm"] == []


@pytest.mark.asyncio
async def test_run_once_degrade_survives_outside_the_session():
    """非交易时段的盘前轮（08:30）拿不到价是常态：只出计划与守护规则，照跑。

    那类轮本就不发腿（``refusing_submitter``），「模型看到空池」不构成危险——它
    要的不是候选就是要守的仓。这条与上面两条是同一条判据的两侧，缺了它就容易
    把「fail-closed」误扩成「盘前一没价就什么都不做」。
    """
    h = make_harness(quote_client=lambda: None, is_trading_time=lambda now: False)
    result = await run_once(
        SLOT_0830, deps=h.deps, now=datetime(2026, 9, 24, 8, 30, tzinfo=CST)
    )
    assert result.status == R.STATUS_OK
    assert h.log["llm"] != []  # 照常问模型
    meta = h.log["ledger"][0][0].context_meta
    assert meta["quotes"] == {"rows": 0, "stale": 0, "client": False}


# ── 守护规则（整组替换的两条纪律） ───────────────────────────────────
WATCH_TEXT = decisions_json(
    {"action": "hold", "code": "600036.SH", "reason": "趋势未破"},
    {
        "action": "watch",
        "code": "600036.SH",
        "pct": 0.5,
        "stop_loss": 38.5,
        "take_profit": 44.0,
        "invalidation": "跌破 38.5",
        "confidence": 0.7,
        "reason": "跌破止损减半",
    },
)


@pytest.mark.asyncio
async def test_rebalance_round_never_touches_the_watch_table():
    """rebalance 白名单里没有 watch；它的手不许碰守护层（整组替换会清空别人的）。"""
    h = make_harness(decision_text=WATCH_TEXT)
    result = await run_once(SLOT_0935, deps=h.deps, now=NOW)
    assert result.status == R.STATUS_OK  # watch 行被 schema 忽略，hold 照常解析
    assert h.log["watch"] == [] and result.watch_armed == 0


@pytest.mark.asyncio
async def test_intraday_with_watch_arms_rules_and_counts_them():
    h = make_harness(decision_text=WATCH_TEXT)
    result = await run_once(SLOT_1000, deps=h.deps, now=TEN_05)
    assert result.status == R.STATUS_OK
    written = h.log["watch"]
    assert len(written) == 1 and written[0]["agent"] == "fake-model"
    assert len(written[0]["plan"].rules) == 1
    assert result.watch_armed == 1


@pytest.mark.asyncio
async def test_watch_rule_entry_price_comes_from_agent_ledger_not_bridge():
    """规则写入时带上分账成本价（2026-10-19 601000.SH：账本有成本，规则却是 None）。

    判据取**账本与桥不同值**的持仓：桥是共享账户的混合成本（38.0），账本才是本
    agent 的真实建仓价（36.5）。填进规则的是后者——执行器拿到成本就不必每轮向
    柜台要，柜台读不到的瞬时窗口不会再让整条规则被跳过。
    """
    h = make_harness(
        decision_text=WATCH_TEXT,
        ledger=AgentLedgerRead(
            ok=True,
            known=True,
            positions={
                "600036.SH": {
                    "volume": 1000.0,
                    "cost_price": 36.5,
                    "buy_ts": "2026-09-20T01:30:00Z",
                    "last_ts": "2026-09-20T01:30:00Z",
                }
            },
        ),
    )
    result = await run_once(SLOT_1000, deps=h.deps, now=TEN_05)

    assert result.status == R.STATUS_OK and result.watch_armed == 1
    rule = h.log["watch"][0]["plan"].rules[0].rule
    assert rule["entry_price"] == 36.5
    notes = h.log["ledger"][0][0].context_meta["notes"]
    assert any("回填分账成本价 1 条" in n for n in notes)


@pytest.mark.asyncio
async def test_watch_rule_without_ledger_cost_leaves_entry_price_empty():
    """账本没有可用成本（0/缺失）时不瞎填：留 None，由执行器走柜台兜底。"""
    h = make_harness(
        decision_text=WATCH_TEXT,
        ledger=AgentLedgerRead(
            ok=True,
            known=True,
            positions={
                "600036.SH": {
                    "volume": 1000.0,
                    "cost_price": 0.0,
                    "buy_ts": "2026-09-20T01:30:00Z",
                    "last_ts": "2026-09-20T01:30:00Z",
                }
            },
        ),
    )
    result = await run_once(SLOT_1000, deps=h.deps, now=TEN_05)

    assert result.status == R.STATUS_OK and result.watch_armed == 1
    rule = h.log["watch"][0]["plan"].rules[0].rule
    assert rule["entry_price"] is None
    notes = h.log["ledger"][0][0].context_meta["notes"]
    assert not any("回填分账成本价" in n for n in notes)


@pytest.mark.asyncio
async def test_watch_on_non_held_symbol_is_rejected_and_table_kept():
    """688543.SH 实录：模型把「回踩 47.60 再首建」写成了 watch。

    watch 落库后是**卖出规则**（跌破减仓/涨到止盈），无持仓时永远不会成交，
    执行器只会每天推一条「缺少成本价」。分账账本里没有的标的必须拒绝并留痕；
    全拒时**不写空集**（整组替换会摘掉该 agent 的全部既有守护）。
    """
    h = make_harness(
        decision_text=decisions_json(
            {
                "action": "watch",
                "code": "688543.SH",
                "pct": 0.1,
                "stop_loss": 47.6,
                "reason": "回踩 47.60 不破再小仓首建",
            }
        )
    )
    result = await run_once(SLOT_1000, deps=h.deps, now=TEN_05)
    assert result.status == R.STATUS_OK
    assert result.watch_armed == 0
    assert h.log["watch"] == []  # 全拒 → 一次写都没有（不写空集）
    notes = h.log["ledger"][0][0].context_meta["notes"]
    assert any("拒绝 1 条未持仓标的" in n for n in notes)
    assert any("整组保留" in n for n in notes)


@pytest.mark.unit
def test_entry_price_backfill_keeps_explicit_value_and_rounds(monkeypatch):
    """回填只补空缺：规则已带成本价时不动它；补进去的值归一到三位小数。"""
    from backend.shared.decision import watch_map

    def fake_plan(decisions, *, agent="", held_symbols=None):  # noqa: ARG001
        return watch_map.WatchPlan(
            rules=(
                watch_map.WatchRule(
                    symbol="600036.SH", rule={"entry_price": 40.0}, agent=agent
                ),
                watch_map.WatchRule(
                    symbol="601000.SH", rule={"entry_price": None}, agent=agent
                ),
            ),
            agent=agent,
        )

    monkeypatch.setattr(watch_map, "plan_watch", fake_plan)
    h = make_harness()
    notes: list[str] = []

    IO.maybe_write_watch(
        deps=h.deps,
        slot=SLOT_1000,
        decisions=[],
        agent="fake-model",
        notes=notes,
        ledger=AgentLedgerRead(
            ok=True,
            known=True,
            positions={
                "600036.SH": {"cost_price": 36.5},
                "601000.SH": {"cost_price": 4.5412},
            },
        ),
    )

    rules = h.log["watch"][0]["plan"].rules
    assert rules[0].rule["entry_price"] == 40.0  # 显式值不覆盖
    assert rules[1].rule["entry_price"] == 4.541  # 缺失值回填 + 三位小数
    assert any("回填分账成本价 1 条" in n for n in notes)


@pytest.mark.asyncio
async def test_intraday_without_any_watch_keeps_the_existing_rules():
    """空集不写：一次「模型只吐 hold」不该把该 agent 的全部守护规则摘掉。"""
    h = make_harness()
    result = await run_once(SLOT_1000, deps=h.deps, now=TEN_05)
    assert result.status == R.STATUS_OK
    assert h.log["watch"] == []
    meta = h.log["ledger"][0][0].context_meta
    assert any("整组保留" in n for n in meta["notes"])


# ── 非交易时段：出决策与守护规则，但不提交腿 ─────────────────────────
@pytest.mark.asyncio
async def test_off_session_round_refuses_every_leg_but_still_arms_watch():
    h = make_harness(
        decision_text=WATCH_TEXT,
        is_trading_time=lambda now: False,
        outcome=FakeOutcome(legs=1, submitted=0),  # 拒发 → 一条都没成
    )
    result = await run_once(
        SLOT_0830, deps=h.deps, now=datetime(2026, 9, 24, 8, 30, tzinfo=CST)
    )
    assert result.status == R.STATUS_OK and result.submitted == 0
    assert result.note == "非交易时段：未提交腿"
    sent = h.log["exec"][0]
    assert sent["submitter"] is not None  # 拒发提交器，不是 None（不是「没传」）
    assert sent["inflight"] == frozenset()  # 时段外不发单 → 不必读在途账
    refusal = await sent["submitter"](object(), "cid", False)
    assert refusal.success is False and "非交易时段" in refusal.message
    assert len(h.log["watch"]) == 1  # 守护规则照写：它就是给开盘用的


@pytest.mark.asyncio
async def test_exec_failure_is_reported_as_error_status():
    h = make_harness()

    async def boom(*, db, **kwargs):
        h.log["exec"].append(kwargs)
        raise RuntimeError("路由炸了")

    result = await run_once(SLOT_0935, deps=replace(h.deps, run_exec=boom), now=NOW)
    assert result.status == R.STATUS_ERROR and "执行段失败" in result.note
    assert result.decisions == 1  # 决策出了、执行段失败——两者分开记
    assert h.log["ledger"] == []  # 执行段抛了就不落审计（补跑槽可重来）


@pytest.mark.asyncio
async def test_run_once_aborts_when_account_read_raises():
    async def boom(tenant, user):
        raise RuntimeError("桥的超时")

    h = make_harness()
    result = await run_once(SLOT_0935, deps=replace(h.deps, load_account=boom), now=NOW)
    assert result.status == R.STATUS_ABORTED and "资金面读取异常" in result.note
    assert h.log["llm"] == []


@pytest.mark.asyncio
async def test_stale_account_snapshot_warns_but_does_not_abort(caplog):
    """08:30 盘前读到的必然是昨日收盘那份：年龄进审计，但**不拦轮**。"""
    stale = replace(
        AccountRead(ok=True, cash=50000.0, market_value=120000.0, source="tdx_bridge"),
        age_min=900.0,
    )
    h = make_harness(account=stale)
    result = await run_once(SLOT_0830, deps=h.deps, now=NOW)
    assert result.status == R.STATUS_OK
    assert any("账户快照偏旧" in r.message for r in caplog.records)
    meta = h.log["ledger"][0][0].context_meta
    assert meta["account"]["age_min"] == 900.0  # 「拿旧资金做的决策」可查


@pytest.mark.asyncio
async def test_quote_client_construction_failure_aborts_in_session():
    """客户端构造抛异常与「没配」同一条路：原因带上，交易时段内 fail-closed。"""

    def boom():
        raise RuntimeError("远端行情未配置")

    h = make_harness(quote_client=boom)
    result = await run_once(SLOT_0935, deps=h.deps, now=NOW)
    assert result.status == R.STATUS_ABORTED
    assert "行情客户端不可用" in result.note and "远端行情未配置" in result.note
    assert h.log["snaps"] == []
    assert h.log["exec"] == []


@pytest.mark.asyncio
async def test_watch_write_failure_does_not_sink_the_round():
    """守护段与买卖段互不拖累：写不进规则表要留痕，但决策与审计照常落。"""

    def boom(agent, plan):
        raise RuntimeError("规则表写不进")

    h = make_harness(decision_text=WATCH_TEXT)
    result = await run_once(
        SLOT_1000, deps=replace(h.deps, write_watch=boom), now=TEN_05
    )
    assert result.status == R.STATUS_OK and result.audit_rows == 2
    meta = h.log["ledger"][0][0].context_meta
    assert any("守护规则写入异常" in n for n in meta["notes"])
    assert result.watch_armed == 0  # 没写成就不许记成「挂上了」


@pytest.mark.asyncio
async def test_partially_lost_watch_write_is_noted_and_not_counted_as_armed():
    """写被静默丢弃**不走 errors**（P2.4 的 ok 是 not(errors|unverified|problems)）。

    只少了 ``watch_armed`` 一个数，运营从状态键看不出「少的那条是没挂上，还是本轮
    压根没提」——所以三项要一起进 notes。
    """
    from backend.shared.decision.watch_map import plan_watch

    h = make_harness(decision_text=WATCH_TEXT)
    parsed = attempt_from(WATCH_TEXT, schema=SCHEMA_INTRADAY)
    partial = WatchWriteResult(
        owner="llm:fake-model",
        plan=plan_watch(parsed.decisions, agent="fake-model"),
        armed=("600036.SH",),
        unverified=(("600519.SH", "回读没确认到"),),
        problems=("他方规则被改写",),
    )
    result = await run_once(
        SLOT_1000, deps=replace(h.deps, write_watch=lambda a, p: partial), now=TEN_05
    )
    assert result.status == R.STATUS_OK  # 守护段半失败不是交易失败
    assert result.watch_armed == 1  # 只有确认落库的那条算数
    notes = h.log["ledger"][0][0].context_meta["notes"]
    assert any("守护规则未全部落库" in n for n in notes)
    assert any("600519.SH" in n and "他方规则被改写" in n for n in notes)


@pytest.mark.asyncio
async def test_missing_exclusion_list_is_noted_but_does_not_stop_the_round():
    """名单未导入 ≠ 无风险股：照跑，但必须留痕（静默当空名单是最危险的一种「正常」）。"""
    h = make_harness()
    missing = ExclusionRead(note="排除名单未导入（文件不在盘）：本轮按空名单跑")
    result = await run_once(
        SLOT_0935, deps=replace(h.deps, load_excluded=lambda: missing), now=NOW
    )
    assert result.status == R.STATUS_OK
    notes = h.log["ledger"][0][0].context_meta["notes"]
    assert any("排除名单未导入" in n for n in notes)


# ── 真单镜像就绪段（P2-2，审计 H2/M9）───────────────────────────────
# 这段记的是**通道就绪**（此刻发真单，镜像闸门放不放行），与逐腿 mirror 段
# （那是结果）是两件事；两段合读才能答「模拟轮为什么没镜像出去」。
# 核心纪律：探不到 → None，绝不回落 True（那正是真单面静默要消灭的形态）。
def test_mirror_meta_only_accepts_a_bool_and_a_reason():
    """归一：``(bool, 原因)`` 两种序列形态收，怪形状一律 None（不猜）。"""
    assert mirror_meta((True, "")) == {"ready": True, "reason": ""}
    assert mirror_meta((False, "real_trading_disabled")) == {
        "ready": False,
        "reason": "real_trading_disabled",
    }
    assert mirror_meta([False, None]) == {"ready": False, "reason": ""}
    # 原因自身可含冒号（如 broker_not_qmt_exec:tdx_bridge）：原样保留
    assert mirror_meta((False, "broker_not_qmt_exec:tdx_bridge"))["reason"] == (
        "broker_not_qmt_exec:tdx_bridge"
    )
    # 怪形状：探不到就是探不到，不许编
    assert mirror_meta(None) is None
    assert mirror_meta("ready") is None
    assert mirror_meta((True,)) is None
    assert mirror_meta(("yes", "就绪")) is None  # 1/0、"yes" 不算 bool
    assert mirror_meta((1, "就绪")) is None


@pytest.mark.asyncio
async def test_context_meta_records_the_mirror_readiness_probe():
    """接线了就绪探测 → 审计行 mirror 段如实落 ``ready``+原因（含冒号的尾段保留）。"""
    h = make_harness(mirror_ready=lambda: (False, "broker_not_qmt_exec:tdx_bridge"))
    await run_once(SLOT_0935, deps=h.deps, now=NOW)
    meta = h.log["ledger"][0][0].context_meta
    assert meta["mirror"] == {
        "ready": False,
        "reason": "broker_not_qmt_exec:tdx_bridge",
    }


@pytest.mark.asyncio
async def test_context_meta_mirror_is_none_when_the_probe_is_unwired():
    """未接线（老替身/旧部署）= 探不到：写 None，**绝不**写成就绪（H2 的静默形态）。"""
    h = make_harness()
    await run_once(SLOT_0935, deps=h.deps, now=NOW)
    meta = h.log["ledger"][0][0].context_meta
    assert "mirror" in meta and meta["mirror"] is None


@pytest.mark.asyncio
async def test_mirror_probe_failure_is_none_with_a_warning_and_never_blocks(caplog):
    """探测抛异常：记 None + warning（轮照跑、账照落）——探测绝不阻断决策轮。"""

    def boom():
        raise RuntimeError("Redis 没连上")

    h = make_harness(mirror_ready=boom)
    with caplog.at_level(logging.WARNING):
        result = await run_once(SLOT_0935, deps=h.deps, now=NOW)
    assert result.status == R.STATUS_OK
    meta = h.log["ledger"][0][0].context_meta
    assert meta["mirror"] is None
    assert any("镜像就绪探测失败" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_exec_summary_as_plain_dict_still_counts():
    """执行段返回 ``summary`` 是 dict（不是方法）时也算得出腿数——少这一支就静默记 0。"""

    class _DictOutcome:
        outcomes = {}
        aborted = ""
        summary = {"legs": 2, "submitted": 1, "notes": ["虚拟现金未建模"]}

    h = make_harness(outcome=None)
    result = await run_once(
        SLOT_0935,
        deps=replace(h.deps, run_exec=_exec_returning(_DictOutcome())),
        now=NOW,
    )
    assert result.status == R.STATUS_OK
    assert result.legs == 2 and result.submitted == 1
    assert result.errors == ()  # 有 outcomes 属性 ⇒ 逐决策结果回收得到，不留痕

    class _Bare:  # 什么也没说的执行段：记 0 是对的，但必须是**真 0**而不是取不到
        pass

    bare = await run_once(
        SLOT_0935, deps=replace(h.deps, run_exec=_exec_returning(_Bare())), now=NOW
    )
    assert bare.status == R.STATUS_OK and bare.legs == 0
    # 缺 ``outcomes`` 属性时合并结果只剩守护段，审计行会显示「这一轮什么也没发生」
    # ——而腿可能已经真出去了。此刻状态不改（单已在下，报错会让补跑槽重来一轮），
    # 但「取不到」必须与「真的是 0」分得开：带一条 errors 让状态键不留全绿。
    assert any("缺 outcomes 属性" in e for e in bare.errors)


@pytest.mark.asyncio
async def test_executor_abort_without_a_single_order_is_aborted_not_ok():
    """执行段 ``aborted`` 且一张单都没发 ⇒ 本轮**不算跑过**（后续槽位仍会再来）。

    此前它只进 ``note``、状态仍是 ``ok``——而 ``round_tick`` 按 ``result.ok`` 写
    done 键，于是当天 10:05/11:05 两个 rebalance 补跑槽全部「当日已出过决策」跳过：
    **一次瞬时读失败吃掉当天全部建仓轮**，状态键、日志、CLI 退出码三面却全绿。

    note 里还要点名**守护规则表没被本轮动过**：那张表是外部状态，审计表里看不出它。
    """
    h = make_harness(
        outcome=FakeOutcome(aborted="在途账读不到：本轮不提交腿", legs=2, submitted=0)
    )
    result = await run_once(SLOT_0935, deps=h.deps, now=NOW)
    assert result.status == R.STATUS_ABORTED
    assert result.note == "在途账读不到：本轮不提交腿（守护规则表本轮整组保留）"
    assert result.errors == ("在途账读不到：本轮不提交腿",)
    assert result.submitted == 0


@pytest.mark.asyncio
async def test_aborted_round_never_touches_the_watch_table():
    """H3：执行段 aborted 且零提交时**不许写守护规则表**（整组替换会清掉既有止损）。

    ``write_watch_plan`` 是整组替换：拿一轮「一张单都没发」的决策去覆盖，等于把
    09:00 挂上的止损全摘掉，还给一批**从未买入**的标的 arm 上规则——守卫的空仓
    卖出提醒就是这么来的。守护段与执行段共享同一份快照，执行段说「这轮不算数」时
    守护段必须跟着不动。
    """
    h = make_harness(
        decision_text=WATCH_TEXT,
        outcome=FakeOutcome(aborted="在途账读不到：本轮不提交腿", legs=1, submitted=0),
    )
    result = await run_once(SLOT_1000, deps=h.deps, now=TEN_05)
    assert result.status == R.STATUS_ABORTED
    assert h.log["watch"] == []  # 规则表一根手指头都没碰
    assert result.watch_armed == 0
    meta = h.log["ledger"][0][0].context_meta
    assert any("守护规则表整组保留" in n for n in meta["notes"])  # 审计里也看得见


@pytest.mark.asyncio
async def test_aborted_round_with_orders_already_out_still_writes_the_watch():
    """已有腿真出去 ⇒ 这轮的决策就是有效的：守护规则照写（判据是「零提交」）。"""
    h = make_harness(
        decision_text=WATCH_TEXT,
        outcome=FakeOutcome(aborted="后半程在途账读不到", legs=2, submitted=1),
    )
    result = await run_once(SLOT_1000, deps=h.deps, now=TEN_05)
    assert result.status == R.STATUS_OK
    assert len(h.log["watch"]) == 1 and result.watch_armed == 1


@pytest.mark.asyncio
async def test_executor_abort_with_orders_already_out_is_ok_not_aborted():
    """``aborted`` 但已有腿真出去 ⇒ 必须报 ok：报 aborted 会让补跑槽**再发一批新单**。

    ``aborted`` 的语义是「一张单都没发」——当前 executor 只在在途账读不到时置它
    （`run_round` 里唯一一条赋值，那条路径 `submitted` 必为空）。这条用例钉的是**组合
    规则**而非今天的实现：将来若出现「提交到一半中止」，带上 ``aborted`` + 已提交腿时
    仍不许报 aborted，否则补跑槽会把已经出去的那批**再下一遍**。
    """
    h = make_harness(
        outcome=FakeOutcome(aborted="后半程在途账读不到", legs=3, submitted=1)
    )
    result = await run_once(SLOT_0935, deps=h.deps, now=NOW)
    assert result.status == R.STATUS_OK
    assert result.submitted == 1 and result.legs == 3
    assert result.note == "后半程在途账读不到"  # 例外仍留痕，只是不改状态


@pytest.mark.asyncio
async def test_unexpected_orchestration_error_is_error_status_not_a_crash():
    """档位读取没有自己的 try：它炸了要由 run_once 兜成 error，不能带走 worker。"""

    def boom():
        raise RuntimeError("档位存储炸了")

    h = make_harness(load_tier=boom)
    result = await run_once(SLOT_0935, deps=h.deps, now=NOW)
    assert result.status == R.STATUS_ERROR
    assert result.note.startswith("RuntimeError:")  # 异常类型与原因原样带上
    assert "档位存储炸了" in result.note
    assert result.round_id == "rnd-20260924-0935"  # 状态键里仍能对上号


# ── 补块入提示词（Stage 2 第一批）：模型分数 + 市场状态 ──────────────────
#
# 数据面实测都在产（engine_signal_scores / db0 qm:regime:intraday），缺的只是
# 「接进下单那条回路」——本批三条纪律：接线才渲染（旧替身历史字节不变）、
# 缺失如实降级（不拦轮、不编数）、指数代码并入询价清单。

REGIME_FIXTURE = {
    "state": "bear",
    "breadth": (
        '{"coverage": "hot_set", "sample": 540, "up": 107, "down": 417,'
        ' "flat": 16, "limit_up": 9, "limit_down": 2}'
    ),
    "ret": "-0.0748",
    "window": "20",
    "vol": "0.0083",
    "position_hint": "0.3",
    "generated_at": NOW.isoformat(),
    "inputs_ok": "True",
    "index": "000300.SH",
    "notes": "[]",
}


def _scores_ok(rank_pct: float = 0.882):
    async def load_scores(tenant, user):
        return (
            {
                "SH600036": {
                    "value": 0.31,
                    "side": "BUY",
                    "freq": "realtime",
                    "asOf": "2026-09-24",
                    "rank_pct": rank_pct,
                }
            },
            {
                "ok": True,
                "model_id": "mdl_test_bucket",
                "signal_date": "2026-09-24",
                "rows": 591,
                "realtime_rows": 591,
                "fallback": False,
            },
        )

    return load_scores


@pytest.mark.asyncio
async def test_round_renders_scores_and_regime_blocks_when_wired():
    """接线后：行情行内「信号分」（研究评分刻度）+ 两个补块进提示词；
    regime 的指数代码并入询价清单。"""
    h = make_harness(load_regime=lambda: REGIME_FIXTURE, load_scores=_scores_ok())
    result = await run_once(SLOT_0935, deps=h.deps, now=NOW)
    assert result.status == R.STATUS_OK
    prompt = h.log["llm"][0]["prompt"]
    # 模型分数：rank_pct 0.882 → 研究评分 88.2（与前端/金样同刻度）
    assert "· 信号分 88.2" in prompt
    assert "【盘中模型信号】" in prompt
    assert "mdl_test_bucket" in prompt
    assert "该行所属推理批次内部的截面分位" in prompt
    assert "可能混合多个推理批次" in prompt
    # 市场状态块
    assert "【盘中市场状态】" in prompt
    assert "状态：偏弱（bear）" in prompt
    assert "建议仓位系数 0.3" in prompt
    assert "20 日收益 -7.48%" in prompt
    assert "涨 107 ／ 跌 417" in prompt
    # regime 指数并入询价（市场状态块的指数现价行用）
    assert "000300.SH" in h.log["snaps"][0]


@pytest.mark.asyncio
async def test_round_unwired_blocks_keep_legacy_prompt_bytes():
    """未接线（旧替身/历史行为）：两个补块整段不渲染——升级不动存量提示词。"""
    h = make_harness()  # load_regime/load_scores 默认 None
    await run_once(SLOT_0935, deps=h.deps, now=NOW)
    prompt = h.log["llm"][0]["prompt"]
    assert "【盘中市场状态】" not in prompt
    assert "【盘中模型信号】" not in prompt
    assert "· 信号分" not in prompt  # 历史指令文本里的「信号分数」不算，看渲染后缀


@pytest.mark.asyncio
async def test_round_degrades_loudly_when_feeds_unavailable():
    """接线了但取不到：**降级不拦轮**——块渲染「本轮不可得」、notes 留痕，
    绝不静默消失（否则「当日提示词为什么没有信号分」无处可查）。"""

    async def dead_scores(tenant, user):
        return {}, {"ok": False, "reason": "分数快照取数失败: db down"}

    h = make_harness(load_regime=lambda: None, load_scores=dead_scores)
    result = await run_once(SLOT_0935, deps=h.deps, now=NOW)
    assert result.status == R.STATUS_OK  # 增益块缺失不改轮次状态
    prompt = h.log["llm"][0]["prompt"]
    assert "【盘中市场状态】本轮不可得" in prompt
    assert "【盘中模型信号】本轮不可得" in prompt
    assert "不要凭空引用模型分数" in prompt
    notes = h.log["ledger"][0][0].context_meta["notes"]
    assert any("模型分数不可得" in n for n in notes)
    assert any("regime 不可得" in n for n in notes)


@pytest.mark.asyncio
async def test_round_survives_score_loader_exception():
    """分数取数抛异常：轮次照跑（按不可得降级），不许把真钱回路拖成 error。"""

    async def boom(tenant, user):
        raise RuntimeError("分数库连接炸了")

    h = make_harness(load_regime=lambda: REGIME_FIXTURE, load_scores=boom)
    result = await run_once(SLOT_0935, deps=h.deps, now=NOW)
    assert result.status == R.STATUS_OK
    prompt = h.log["llm"][0]["prompt"]
    assert "【盘中模型信号】本轮不可得" in prompt
    assert "分数库连接炸了" in prompt  # 原因如实进块（排障痕迹）
    notes = h.log["ledger"][0][0].context_meta["notes"]
    assert any("模型分数不可得" in n for n in notes)


# ══ E1b. Stage 2 第二批：丁1–丁9 块 + 风险两槽 + 丙1 唤醒上下文 ═════════
# 五个新注入点（load_news_files / load_risk_files / read_series_summary /
# load_decision_rows / read_wake_ctx）各自门控对应块：默认 None = 未接线 =
# 历史字节；接线后按细案顺序渲染、缺失按「本轮不可得」如实降级。
SERIES_FIXTURE = {
    "600036.SH": SeriesSummary(
        last_ts=NOW.timestamp() - 60,
        last_price=38.5,
        mom30m_pct=1.2,
        mom5m_pct=0.4,
        vol5m_shares=12300,
        source="tdx_bridge",
    )
}
INDEX_SNAPS = {
    "000300.SH": {"Now": 3500.0, "PreClose": 3480.0, "timestamp": NOW.timestamp()},
    "000001.SH": {"Now": 3100.0, "PreClose": 3090.0, "timestamp": NOW.timestamp()},
    "399001.SZ": {"Now": 10200.0, "PreClose": 10150.0, "timestamp": NOW.timestamp()},
    "399006.SZ": {"Now": 2050.0, "PreClose": 2040.0, "timestamp": NOW.timestamp()},
}
RISK_FIXTURE = {
    "asof": "2026-09-24",
    "items": {
        "600036": {"reason": "立案调查", "expire": "2026-10-01"},
        # 预算外标的（600519 一手 17 万 > 单票预算被池过滤）不进风险宇宙——
        # 下面断言它有名单口径却**不**渲染（名单条目 ⊆ 宇宙 的求交纪律）。
        "600519": {"reason": "监管问询", "expire": "2026-10-05"},
        "601988": {"reason": "大额解禁", "expire": "2026-10-08"},
    },
    "watch": {},
    "warns": {},
}
INDUSTRY_FIXTURE = {
    "asof": "2026-09-24",
    "warn_codes": {"600036": "银行"},
    "items": [{"industry": "银行", "n": 30, "total": 42, "rate": 0.714}],
}
BRIEF_FIXTURE = {
    "ts": (NOW - timedelta(minutes=10)).isoformat(),
    "holdings": [
        {
            "code": "600036.SH",
            "name": "招商银行",
            "verdict": "利空",
            "event_type": "公告",
            "headline": "高管变动",
            "impact": -1.5,
            "source": "news",
        }
    ],
}


class _Rec:
    """审计行最小同形（丁6/丁9 只读这七个属性，见 ``decision_blocks``）。"""

    def __init__(self, **kw) -> None:
        self.__dict__.update(kw)


REC_SELL = _Rec(
    # 单家路径的 agent 名 = normalize_agent(binding.model)（见 decision_round.py
    # 「agent 名先归一」）——"本 agent 上一轮"的判据就是这个名字，不是空串。
    agent="fake-model",
    round_id="rnd-20260924-0935",
    decided_at=datetime(2026, 9, 24, 9, 36, tzinfo=CST),
    id=101,
    action="sell",
    code="600519.SH",
    code_raw="",
    pct=0.5,
    reason="估值过高",
)
REC_PRO = _Rec(
    agent="pro",
    round_id="rnd-20260924-0935",
    decided_at=datetime(2026, 9, 24, 9, 38, tzinfo=CST),
    id=102,
    action="buy",
    code="600519.SH",
    code_raw="",
    pct=None,
    reason="主力流入",
)


def _rows_fixture(*rows):
    async def load_decision_rows(trade_date, user):
        return tuple(rows)

    return load_decision_rows


@pytest.mark.asyncio
async def test_round_renders_batch2_blocks_when_wired():
    """接线后九个块按细案顺序进提示词：唤醒说明在池前、丁1–丁9 在池后、
    风险两槽随后、丁7 最末；唤醒说明经 ``read_wake_ctx`` 按 round_id 真读回。"""
    native = FakeNative()
    wake_rids: list[str] = []

    def _read_wake_ctx(round_id):
        wake_rids.append(round_id)
        return F.read_wake_ctx(native, round_id)

    # 低价候选（601988，一手 ¥420 过预算）加进池：风险块的「候选池命中」侧
    # 只有在池票真进宇宙时才可断言（600519 一手 17 万被池过滤，是反例）。
    cheap = PoolRow(
        code="601988.SH",
        name="中国银行",
        industry="银行",
        score=1.0,
        fusion=0.6,
        rank=3,
    )
    h = make_harness(
        pool=pool_doc(BANK_ROW, PRICEY_ROW, cheap),
        snaps={
            **SNAPS,
            **INDEX_SNAPS,
            "601988.SH": {"Now": 4.2, "PreClose": 4.15, "timestamp": NOW.timestamp()},
        },
        read_series_summary=lambda codes: dict(SERIES_FIXTURE),
        load_news_files=lambda: (BRIEF_FIXTURE, None),
        load_decision_rows=_rows_fixture(REC_SELL, REC_PRO),
        load_risk_files=lambda: (RISK_FIXTURE, INDUSTRY_FIXTURE, None),
        read_wake_ctx=_read_wake_ctx,
    )
    promised = round_id_for(DAY, SLOT_0935)
    native.store[W.WAKE_CTX_KEY.format(round_id=promised)] = (
        "【本轮为触发唤醒】（10:52，非整点槽位——盘中触发器把控制权叫醒）\n"
        "- 波动 招商银行(600036.SH)：浮盈 -5.00% → +5.26%"
    )
    result = await run_once(SLOT_0935, deps=h.deps, now=NOW)
    assert result.status == R.STATUS_OK
    prompt = h.log["llm"][0]["prompt"]

    # 丁1 盘口（五档缺失 → 不渲染失衡段；5 分钟涨速来自 series）
    assert "【实时盘口】" in prompt
    assert "- [持仓] 招商银行(600036.SH) 现价 40.00（+1.27%）" in prompt
    assert " ｜ 5分钟涨速 +0.40%" in prompt
    # 丁3 分钟特征（12300 股 = 123 手）
    assert "【分钟特征】" in prompt
    assert "30 分钟 +1.20% ｜ 5 分钟 +0.40% ｜ 5 分钟量 123 手" in prompt
    # 丁2 L2：门控块恒渲染「不可得」，不静默
    assert "【L2 微观结构】本轮不可得" in prompt
    # 丁4 新闻：简报持仓命中
    assert "【盘中新闻】（news_brief 简报：持仓命中 + 池外异动）" in prompt
    assert "- [利空] 招商银行(600036.SH) 公告：高管变动（news）" in prompt
    # 丁6 其他 agent / 丁9 上一轮
    assert "- pro（09:38）：买入 600519.SH：主力流入" in prompt
    assert "【上一轮你的建议】（09:36，1 条）" in prompt
    assert "- 卖出 50% 600519.SH：估值过高" in prompt
    assert "一致性规则" in prompt
    # 丁5 大盘指数（四只全取到）
    assert "【大盘指数】" in prompt
    assert "- 沪深300：3,500.00（+0.57%）" in prompt
    assert "本轮未取到" not in prompt
    # 事件风险（持仓条目 + 候选池条目标注「本轮勿买」）
    assert "【事件风险警示（系统风险清单，硬约束）】（清单日期 2026-09-24）" in prompt
    assert "- 600036.SH 招商银行：立案调查（有效期至 2026-10-01）" in prompt
    assert (
        "- 601988.SH 中国银行：大额解禁（有效期至 2026-10-08）"
        "（候选池命中，本轮勿买）" in prompt
    )
    # 名单里有、但（被预算过滤的）600519 不在宇宙：求交后**不**渲染
    assert "600519.SH 贵州茅台" not in prompt
    # 行业劣化（银行命中持仓）
    assert "【行业整体劣化（赛道级软提示，不硬拦）】（清单日期 2026-09-24）" in prompt
    assert "- 银行：长期排除清单剔除率 71.4%（30/42 只）" in prompt
    # 丙1 唤醒说明（extra_context）
    assert "【本轮为触发唤醒】（10:52" in prompt
    # 丁7 风险预算档位
    assert "【风险预算档位】（normal，来源 doc）" in prompt
    assert "- 单票买入上限 15% ｜ 单轮最多新开 2 只" in prompt
    # 全部健康 → 丁8 整段不出现
    assert "【数据新鲜度警告】" not in prompt
    # 渲染顺序（细案 line 487–522）：指数 < 唤醒说明 < 丁1… < 风险 < 行业 < 丁7 最末
    order = [
        "【大盘指数】",
        "【本轮为触发唤醒】",
        "【实时盘口】",
        "【分钟特征】",
        "【L2 微观结构】",
        "【盘中新闻】",
        "【其他 agent 动态】",
        "【上一轮你的建议】",
        "【事件风险警示",
        "【行业整体劣化",
        "【风险预算档位】",
    ]
    idx = [prompt.index(m) for m in order]
    assert idx == sorted(idx), [(m, i) for m, i in zip(order, idx, strict=True)]
    # 唤醒说明按 **本轮 round_id** 真读回（写读两侧同库同键）
    assert wake_rids == [promised]
    # 丁5 四只指数进了询价清单（深市两只是本批常驻源）
    assert set(INDEX_CODES).issubset(set(h.log["snaps"][0]))


@pytest.mark.asyncio
async def test_round_feeds_paid_tdx_l2_snapshots_into_the_prompt():
    """丁2 接线：通达信付费 L2 的 13 因子快照进**买卖决策**提示词（2026-10-20 用户
    要求「付费通达信的数据为啥不用」）。取数按持仓+池一次读，代码后缀进、快照按
    前缀存——渲染时两处都有标记。"""
    calls: dict = {}

    async def load_l2(codes, trade_date):
        calls["codes"] = list(codes)
        calls["day"] = trade_date
        ts = NOW - timedelta(seconds=41)
        return {
            "SH600036": {
                "symbol": "SH600036",
                "ts": ts,
                "signal_score": 61.2,
                "factors": {
                    "micro_zone_distribution": 0.31,
                    "micro_vpin_vol_ratio": 0.22,
                    "vol_price_divergence": -0.18,
                },
                "now_price": 40.0,
                "pre_close": 39.5,
                "cjbs": 8200,
                "b_order": 100.0,
                "b_cancel": 25.0,
                "s_order": 100.0,
                "s_cancel": 10.0,
            }
        }

    cheap = PoolRow(
        code="601988.SH",
        name="中国银行",
        industry="银行",
        score=1.0,
        fusion=0.6,
        rank=3,
    )
    h = make_harness(
        pool=pool_doc(BANK_ROW, PRICEY_ROW, cheap),
        snaps={
            **SNAPS,
            **INDEX_SNAPS,
            "601988.SH": {"Now": 4.2, "PreClose": 4.15, "timestamp": NOW.timestamp()},
        },
        read_series_summary=lambda codes: dict(SERIES_FIXTURE),
        load_l2_snapshot=load_l2,
    )
    await run_once(SLOT_0935, deps=h.deps, now=NOW)
    prompt = h.log["llm"][0]["prompt"]

    assert "【L2 微观结构】（通达信付费 L2" in prompt
    assert "未接入盘中逐笔" not in prompt
    assert "- [持仓] 招商银行(600036.SH) 现价 40.00（+1.27%）" in prompt
    assert "分 61.2（原始）" in prompt
    assert "快照 41s 前" in prompt
    assert "L2五档失衡 +0.31" in prompt
    assert "VPIN量/额 0.22/—" in prompt  # 额侧因子缺 → 显式缺位，不补 0
    assert "量价背离 -0.18" in prompt
    assert "委撤 买 25.0%/卖 10.0%" in prompt
    # 池票也在询价清单里（没有自己的快照 → 计数，不编）
    assert "另有" in prompt and "本轮无 L2 快照" in prompt
    # 后缀码进、交易日按本轮 now 取（跨日不会拿昨天的快照）
    assert "600036.SH" in calls["codes"]
    assert calls["day"] == DAY
    # 健康路径不产生降级告警
    assert "通达信 L2 快照" not in prompt.split("【L2 微观结构】")[0]


@pytest.mark.asyncio
async def test_round_unwired_batch2_keeps_legacy_prompt_bytes():
    """未接线（旧替身/历史行为）：丁1–丁9 与风险两槽整段不渲染——
    升级不动存量提示词（字节冻结纪律的第二批延续）。"""
    h = make_harness()
    await run_once(SLOT_0935, deps=h.deps, now=NOW)
    prompt = h.log["llm"][0]["prompt"]
    for marker in (
        "【实时盘口】",
        "【分钟特征】",
        "【L2 微观结构】",
        "【盘中新闻】",
        "【其他 agent 动态】",
        "【上一轮你的建议】",
        "【大盘指数】",
        "【数据新鲜度警告】",
        "【事件风险警示",
        "【行业整体劣化",
        "【风险预算档位】",
        "【本轮为触发唤醒】",
        "【真单镜像闸门】",
        "【今日真单执行受阻】",
    ):
        assert marker not in prompt, marker


MIRROR_LIMITS = {
    "max_order_value": 10000.0,
    "max_daily_value": 50000.0,
    "max_daily_orders": 20,
    "max_daily_symbols": 5,
    "max_slippage_pct": 0.02,
}
MIRROR_SKIP_MAX_ORDER = {
    "symbol": "601985.SH",
    "reason": "max_order_value",
    "count": 1,
    "side": "BUY",
    "quantity": 1100,
    "value": 10362.0,
    "at": "2026-10-14T09:35:35",
}


@pytest.mark.asyncio
async def test_round_injects_mirror_gates_and_skip_feedback():
    """镜像启用（P0-B/P0-C）：生效约束与当日跳过明细进提示词；未启用整段不出现。"""
    h = make_harness(
        read_mirror_ctx=lambda: {
            "active": True,
            "limits": MIRROR_LIMITS,
            "skips": [MIRROR_SKIP_MAX_ORDER],
        }
    )
    result = await run_once(SLOT_0935, deps=h.deps, now=NOW)
    assert result.status == R.STATUS_OK
    prompt = h.log["llm"][0]["prompt"]
    assert "【真单镜像闸门】" in prompt
    assert "单笔金额上限 ¥10,000" in prompt
    assert "【今日真单执行受阻】" in prompt
    assert "- 09:35 601985.SH 买入 1100 股（约 ¥10,362） · 单笔金额超上限" in prompt

    off = make_harness(
        read_mirror_ctx=lambda: {
            "active": False,
            "limits": MIRROR_LIMITS,
            "skips": [MIRROR_SKIP_MAX_ORDER],
        }
    )
    result = await run_once(SLOT_0935, deps=off.deps, now=NOW)
    assert result.status == R.STATUS_OK
    off_prompt = off.log["llm"][0]["prompt"]
    assert "【真单镜像闸门】" not in off_prompt
    assert "【今日真单执行受阻】" not in off_prompt  # active=False：连回注也不渲染


@pytest.mark.asyncio
async def test_round_mirror_context_degradation_is_loud_but_not_blocking():
    """镜像上下文读失败/台账读不到：降级如实渲染 + 丁8 置顶留痕，绝不拦轮。"""
    boom = make_harness(read_mirror_ctx=_raising_mirror_ctx)
    result = await run_once(SLOT_0935, deps=boom.deps, now=NOW)
    assert result.status == R.STATUS_OK
    prompt = boom.log["llm"][0]["prompt"]
    assert "【真单镜像闸门】" not in prompt
    assert "【今日真单执行受阻】" not in prompt

    dead = make_harness(
        read_mirror_ctx=lambda: {
            "active": True,
            "limits": MIRROR_LIMITS,
            "skips": None,
        }
    )
    result = await run_once(SLOT_0935, deps=dead.deps, now=NOW)
    assert result.status == R.STATUS_OK
    prompt = dead.log["llm"][0]["prompt"]
    assert "跳过台账读取失败" in prompt
    assert "镜像跳过台账读取失败：执行受阻回注降级" in prompt  # 丁8 置顶汇总
    assert "【数据新鲜度警告】" in prompt


def _raising_mirror_ctx():
    raise RuntimeError("镜像上下文读取炸了")


@pytest.mark.asyncio
async def test_round_batch2_degrades_loudly_and_never_fakes_first_round():
    """接线了但取数坏：**降级不拦轮**——各块「本轮不可得」+ 丁8 置顶汇总；
    台账读失败时丁6/丁9 **整段不出**（绝不谎报「今日首轮」）。"""

    def boom():
        raise RuntimeError("文件读不了")

    def series_boom(codes):
        raise RuntimeError("序列库不可用")

    async def rows_boom(trade_date, user):
        raise RuntimeError("台账库连接炸了")

    async def l2_boom(codes, trade_date):
        raise RuntimeError("L2 快照表连不上")

    native = FakeNative()
    h = make_harness(
        read_series_summary=series_boom,
        load_l2_snapshot=l2_boom,
        load_news_files=lambda: (None, None),
        load_decision_rows=rows_boom,
        load_risk_files=boom,
        read_wake_ctx=lambda rid: F.read_wake_ctx(native, rid),
    )
    result = await run_once(SLOT_0935, deps=h.deps, now=NOW)
    assert result.status == R.STATUS_OK  # 增益块全灭不改轮次状态
    prompt = h.log["llm"][0]["prompt"]
    # 丁8 置顶汇总（开场白之后、持仓段之前）
    assert "【数据新鲜度警告】" in prompt
    assert prompt.index("【数据新鲜度警告】") < prompt.index("【你名下的现有持仓】")
    assert "指数快照未取全（0/4）" in prompt
    assert "行情序列 market:series 本轮无成员" in prompt
    assert "通达信 L2 快照读取失败（RuntimeError）" in prompt
    assert "【L2 微观结构】本轮不可得" in prompt
    assert "新闻简报文件缺失" in prompt
    assert "决策台账读取失败（RuntimeError）" in prompt
    assert "事件风险清单未产出" in prompt
    assert "行业风险清单未产出" in prompt
    # 各块自身的「不可得」姿态（读不到与没有是两件事）
    assert "【盘中新闻】本轮不可得" in prompt
    assert "【分钟特征】本轮不可得" in prompt
    assert "- 沪深300：本轮未取到" in prompt
    assert "【实时盘口】" in prompt  # 快照还在：盘口行照渲染
    assert "现价 40.00（+1.27%）" in prompt
    # 台账坏 → 两块整段不出，且绝不出现假「今日首轮」
    assert "【其他 agent 动态】" not in prompt
    assert "【上一轮你的建议】" not in prompt
    assert "今日首轮" not in prompt


@pytest.mark.asyncio
async def test_round_batch2_empty_ledger_is_an_honest_first_round():
    """台账**读成功但为空**才是真「今日首轮」——与读失败是两回事。"""
    h = make_harness(
        load_decision_rows=_rows_fixture(),
        read_series_summary=lambda codes: dict(SERIES_FIXTURE),
    )
    await run_once(SLOT_0935, deps=h.deps, now=NOW)
    prompt = h.log["llm"][0]["prompt"]
    assert "【上一轮你的建议】本轮为今日首轮分析，无上一轮建议。" in prompt
    assert "【其他 agent 动态】今日暂无可参考的他家决策。" in prompt


# ── 丙1 唤醒编排（wake_tick：闸门 → 观测 → 判据 → 合成槽轮）──────────
#: 10:52：无槽位临期（10:05 窗到 10:50）、小时内评估节拍已过——唤醒轮的可跑时刻。
WAKE_NOW = datetime(2026, 9, 24, 10, 52, tzinfo=CST)


def _seed_wake_state(native, *, day="20260924", **fields):
    """预置唤醒状态键（走真实编解码：encode → hset → 被测代码 hgetall/decode）。"""
    state = W.WakeState(day=day, **fields)
    native.hset(W.WAKE_STATE_KEY.format(day=day), mapping=W.encode_state(state))
    return state


def _state_of(native, *, day="20260924"):
    return W.decode_state(native.hashes.get(W.WAKE_STATE_KEY.format(day=day)), day=day)


@pytest.mark.asyncio
async def test_wake_tick_fires_a_synthetic_round_on_a_volatility_trigger():
    """浮盈偏离基线 10.26pp → 合成槽 1052 跑一轮：唤醒说明进提示词（经真读回）、
    状态计数/心跳/冷却落键、round_id 与合成槽一致。"""
    native = FakeNative()
    seen: list[str] = []

    def _read_wake_ctx(round_id):
        seen.append(round_id)
        return F.read_wake_ctx(native, round_id)

    h = make_harness(read_wake_ctx=_read_wake_ctx)
    _seed_wake_state(
        native,
        last_eval_ts=WAKE_NOW.timestamp() - 120,
        baselines={"600036.SH": {"pnl": -5.0, "chg": None, "imb5": None}},
    )
    results = await wake_tick(deps=h.deps, native=native, now=WAKE_NOW)
    assert [r.status for r in results] == [R.STATUS_OK]
    assert results[0].slot.hhmm == "1052"
    assert results[0].round_id == "rnd-20260924-1052"
    prompt = h.log["llm"][0]["prompt"]
    assert "【本轮为触发唤醒】（10:52" in prompt
    assert "波动 招商银行(600036.SH)：浮盈 -5.00% → +5.26%（Δ+10.26pp）" in prompt
    # 唤醒说明按本轮 round_id 写键、轮内按同一键读回
    ctx_key = W.WAKE_CTX_KEY.format(round_id="rnd-20260924-1052")
    assert ctx_key in native.store
    assert "本轮结论只针对以上触发事项" in native.store[ctx_key]
    assert seen == ["rnd-20260924-1052"]
    # 状态推进：计数 +1、心跳、同码同向冷却 60 分钟、基线重置为当前观测
    st = _state_of(native)
    assert st.wake_count == 1
    assert st.last_eval_ts == WAKE_NOW.timestamp()
    assert st.cooldowns["600036.SH|up"] == (
        WAKE_NOW.timestamp() + W.SAME_DIR_COOLDOWN_MIN * 60
    )
    assert st.baselines["600036.SH"]["pnl"] == 5.2632


@pytest.mark.asyncio
async def test_wake_tick_gates_skip_without_burning_the_round(caplog):
    """四道闸门各自拦住评估：节拍（60s，不写心跳）、距上轮 <20min（写心跳）、
    槽位临期（写心跳）、非交易时段（连 Redis 都不碰）。"""
    h = make_harness()
    # ① 评估节拍未到（距上次评估 30 秒）→ 不评、状态原样
    native = FakeNative()
    _seed_wake_state(native, last_eval_ts=WAKE_NOW.timestamp() - 30)
    assert await wake_tick(deps=h.deps, native=native, now=WAKE_NOW) == ()
    assert _state_of(native).last_eval_ts == WAKE_NOW.timestamp() - 30
    # ② 距上一轮不足 20 分钟（轮次日志键头）→ 不评，心跳照推
    native = FakeNative()
    _seed_wake_state(native, last_eval_ts=WAKE_NOW.timestamp() - 120)
    native.lpush(
        LOG_KEY, json.dumps({"ts": (WAKE_NOW - timedelta(minutes=5)).isoformat()})
    )
    assert await wake_tick(deps=h.deps, native=native, now=WAKE_NOW) == ()
    assert _state_of(native).last_eval_ts == WAKE_NOW.timestamp()
    # ③ 槽位临期（09:35 时 0900/0935 都在宽限窗内）→ 不评（正点轮本来就要跑）
    native = FakeNative()
    _seed_wake_state(native, last_eval_ts=NOW.timestamp() - 120)
    assert await wake_tick(deps=h.deps, native=native, now=NOW) == ()
    assert _state_of(native).last_eval_ts == NOW.timestamp()
    assert h.log["llm"] == []  # 全程没有一轮被烧掉
    # ④ 非交易时段 → 连 Redis 都不碰
    native = FakeNative()
    h2 = make_harness(is_trading_time=lambda now: False)
    assert await wake_tick(deps=h2.deps, native=native, now=WAKE_NOW) == ()
    assert native.calls == []


@pytest.mark.asyncio
async def test_wake_tick_skips_order_buffers_without_continuous_quotes():
    """报单缓冲（09:15-09:30 / 12:55-13:00）：可下单但**没有连续竞价行情**。

    这段 ``is_trading_time`` 为真（真单闸门放行），但决策轮拿不到连续行情：
    12:55 唤醒轮若照跑，池票在行情轮转采到之前就是空池 → 三家模型三连中止
    告警（2026-10-19 12:55 实测）。唤醒只认连续竞价，把机会交给 13:00 正点槽。
    """
    native = FakeNative()
    h = make_harness(
        is_trading_time=lambda now: True,
        is_continuous_trading=lambda now: False,
    )
    assert await wake_tick(deps=h.deps, native=native, now=WAKE_NOW) == ()
    assert native.calls == []  # 连唤醒状态键都不碰

    # 未接线的替身（None）保持旧口径：is_trading_time 说了算。
    native2 = FakeNative()
    h2 = make_harness(is_trading_time=lambda now: False)
    assert await wake_tick(deps=h2.deps, native=native2, now=WAKE_NOW) == ()
    assert native2.calls == []


@pytest.mark.asyncio
async def test_wake_tick_off_switch_and_redis_failure_never_raise(monkeypatch):
    """总开关关（唯一读法 env_flag）→ 不评不碰 Redis；状态键读失败 → 跳过
    （基线没了 = 判据失真），两条路都**绝不抛**（runner 里只有一行裸 await）。"""
    h = make_harness()
    monkeypatch.setenv("QM_DECISION_WAKE_ENABLED", "false")
    native = FakeNative()
    assert await wake_tick(deps=h.deps, native=native, now=WAKE_NOW) == ()
    assert native.calls == []
    monkeypatch.delenv("QM_DECISION_WAKE_ENABLED")
    # 状态键读不下来（hgetall 抛）→ 跳过而不是带着空基线误判
    broken = FakeNative(fail_get=True)
    assert await wake_tick(deps=h.deps, native=broken, now=WAKE_NOW) == ()
    assert h.log["llm"] == []


@pytest.mark.asyncio
async def test_wake_tick_budget_exhausted_records_cooldown_without_a_round():
    """日预算（20 轮）用尽：照记冷却防 60 秒一遍刷屏，但**不跑轮**（丙2 护栏）。"""
    h = make_harness()
    native = FakeNative()
    _seed_wake_state(
        native,
        last_eval_ts=WAKE_NOW.timestamp() - 120,
        wake_count=W.DAILY_WAKE_LIMIT,
        baselines={"600036.SH": {"pnl": -5.0, "chg": None, "imb5": None}},
    )
    assert await wake_tick(deps=h.deps, native=native, now=WAKE_NOW) == ()
    assert h.log["llm"] == []  # 不跑轮
    assert W.WAKE_CTX_KEY.format(round_id="rnd-20260924-1052") not in native.store
    st = _state_of(native)
    assert "预算用尽" in st.last_note
    assert st.cooldowns["600036.SH|up"] > WAKE_NOW.timestamp()  # 冷却照落


@pytest.mark.asyncio
async def test_wake_tick_idle_marks_the_sector_gap_once_per_day():
    """无触发（基线就等于当前值）：不跑轮；板块判据无源**每日一次**留痕——
    已留过的日子不再追加（状态键里运营可见，不静默）。"""
    h = make_harness()
    native = FakeNative()
    _seed_wake_state(
        native,
        last_eval_ts=WAKE_NOW.timestamp() - 120,
        baselines={"600036.SH": {"pnl": 5.2632, "chg": 1.2658, "imb5": None}},
    )
    assert await wake_tick(deps=h.deps, native=native, now=WAKE_NOW) == ()
    st = _state_of(native)
    assert st.wake_count == 0
    assert "板块类判据无源" in st.last_note
    assert st.sector_gap_day == "20260924"
    # 同日后评：已有留痕 → 不重复追加（预置 last_note 原样保留）
    native2 = FakeNative()
    _seed_wake_state(
        native2,
        last_eval_ts=WAKE_NOW.timestamp() - 120,
        baselines={"600036.SH": {"pnl": 5.2632, "chg": 1.2658, "imb5": None}},
        sector_gap_day="20260924",
        last_note="既有留痕",
    )
    assert await wake_tick(deps=h.deps, native=native2, now=WAKE_NOW) == ()
    assert _state_of(native2).last_note == "既有留痕"


# ══ E2. arena 文件面写侧（决策轮 → 前端「模型对话」tab）═══════════════
@pytest.fixture()
def arena_root() -> Path:
    """本用例的 arena 数据根。

    **读环境变量**而不是另取 ``tmp_path``：换根入口只有 ``QM_ARENA_DATA_ROOT`` 一个。
    拿不到就 ``KeyError``——``conftest._sandbox_arena_data_root`` 被摘掉时这些用例当场
    炸，而不是悄悄写进真数据根。这不是假想的谨慎：2026-10-14 实测过一次，接线当天
    就有测试把 ``fake-model`` / ``flash`` / ``pro`` 三个幽灵 agent 连同 78 万字节对话
    写进了 ``data/agent_data_astock/``，并往两个真 agent 的当日对话日志尾部追加了 4 行。
    """
    # arena 包是本机独有的（.gitignore）：公开仓没有它，这些用例整组 skip 而不是
    # 报错——公开仓里 arena 栏本身也不存在，没有可断言的面。
    pytest.importorskip("backend.services.agent_arena.arena_files")
    return Path(os.environ["QM_ARENA_DATA_ROOT"])


def _dialogue_rows(arena_root: Path, agent: str, day: str = "2026-09-24") -> list[dict]:
    """该家该日的 ``log.jsonl`` 逐行（缺文件 = 空表）。"""
    path = arena_root / "agent_data_astock" / agent / "log" / day / "log.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(x)
        for x in path.read_text(encoding="utf-8").splitlines()
        if x.strip()
    ]


@pytest.mark.asyncio
async def test_successful_round_appends_one_dialogue_row(arena_root):
    """跑成的一轮 = ``log.jsonl`` 恰好多一行，且落的是**真发出去的那条提示词**。

    「恰好多一行」是判据不是形式：一轮多次调用（解析重试）只在 ``calls`` 里计数，
    落盘一次——首行提示词与重问的提示词差一个后缀，两条都写会让前端显示两轮。
    """
    text = decisions_json({"action": "hold", "code": "600036.SH", "reason": "趋势未破"})
    h = make_harness(decision_text=text)
    result = await run_once(SLOT_0935, deps=h.deps, now=NOW)

    assert result.status == R.STATUS_OK
    rows = _dialogue_rows(arena_root, "fake-model")
    assert len(rows) == 1
    row = rows[0]
    assert row["signature"] == "fake-model"
    assert row["new_messages"][0]["content"] == h.log["llm"][0]["prompt"]
    assert row["new_messages"][1]["content"] == text
    # ``/token-usage`` 端点只统计带 usage 的行：缺了这键这一轮不进 token 账
    assert row["usage"] == {"prompt_tokens": 7}


@pytest.mark.asyncio
async def test_dialogue_lands_in_the_round_day_not_today(arena_root):
    """目录日**取自本轮时钟**：补跑历史槽时要落到那一天，取「今天」会让它出现在错的日子。"""
    h = make_harness()
    await run_once(SLOT_0935, deps=h.deps, now=NOW)  # NOW = 2026-09-24 09:35

    assert len(_dialogue_rows(arena_root, "fake-model", "2026-09-24")) == 1
    row = _dialogue_rows(arena_root, "fake-model")[0]
    # 墙钟 + ``+08:00``：前端 fmtDateTime 是字符串切片不做时区换算，写 UTC ``Z`` 会差 8 小时
    assert row["timestamp"] == "2026-09-24T09:35:00+08:00"


@pytest.mark.asyncio
async def test_dialogue_keeps_text_beyond_the_audit_truncation(arena_root):
    """展示面落**全文**：审计用的 ``attempt.raw`` 被 ``RAW_LIMIT`` 截到 8000 字符。

    实测历史 2700 条回复里 4.4% 超 8000（三 agent 峰值 13958）：拿被截的 ``raw`` 落盘
    会静默切掉近 5% 的对话，且切口在句子中间——界面上只表现为一段戛然而止的分析。
    """
    text = decisions_json(
        {"action": "hold", "code": "600036.SH", "reason": "趋势未破，" * 2000}
    )
    assert len(text) > 8000  # 前提：这条回复确实超了审计额度
    h = make_harness(decision_text=text)
    assert (await run_once(SLOT_0935, deps=h.deps, now=NOW)).status == R.STATUS_OK

    row = _dialogue_rows(arena_root, "fake-model")[0]
    assert row["new_messages"][1]["content"] == text
    assert len(row["new_messages"][1]["content"]) > 8000


@pytest.mark.asyncio
async def test_failed_round_writes_no_dialogue(arena_root):
    """失败轮不落对话——补跑槽会再来一次，先写一条会在同一槽位留下半截重复。

    失败本身另有可见面（``decision_round_alerts`` 走 QQ + 状态键），不需要靠这个文件。
    """
    h = make_harness(decision_text="今天没什么好说的")  # 无 JSON → parse_failed
    result = await run_once(SLOT_0935, deps=h.deps, now=NOW)

    assert result.status == R.STATUS_LLM_FAILED
    assert _dialogue_rows(arena_root, "fake-model") == []


@pytest.mark.asyncio
async def test_dialogue_write_failure_is_noted_but_does_not_sink_the_round(
    arena_root, monkeypatch
):
    """落盘失败不改轮次状态（真钱链路照走），但审计行必须留痕。

    展示面可以坏，**不可以在没人知道的情况下坏**：一行日志没人看，所以同一句话也
    进审计行的 ``context_meta.notes``（那一列是要被复盘的）。
    """
    from backend.services.agent_arena import arena_files

    monkeypatch.setattr(arena_files, "_write_line", lambda path, record: False)
    h = make_harness(
        decision_text=decisions_json(
            {"action": "buy", "code": "600036.SH", "pct": 0.2, "reason": "加仓"}
        )
    )
    result = await run_once(SLOT_0935, deps=h.deps, now=NOW)

    assert result.status == R.STATUS_OK  # 落盘坏了不动状态
    assert h.log["exec"] and h.log["ledger"]  # 执行段与审计段照走
    notes = h.log["ledger"][0][0].context_meta["notes"]
    assert any("arena 对话落盘未完成" in n for n in notes), notes
    assert _dialogue_rows(arena_root, "fake-model") == []


@pytest.mark.asyncio
async def test_dialogue_write_exception_does_not_take_the_round_down(
    arena_root, monkeypatch
):
    """写侧**抛**异常（不是返回 False）也要被收住：一次磁盘满不该打断真钱回路。"""
    from backend.services.agent_arena import arena_files

    def boom(**_kw):
        raise OSError("No space left on device")

    monkeypatch.setattr(arena_files, "append_round_dialogue", boom)
    h = make_harness(
        decision_text=decisions_json(
            {"action": "buy", "code": "600036.SH", "pct": 0.2, "reason": "加仓"}
        )
    )
    result = await run_once(SLOT_0935, deps=h.deps, now=NOW)

    assert result.status == R.STATUS_OK
    assert h.log["exec"] and h.log["ledger"]
    notes = h.log["ledger"][0][0].context_meta["notes"]
    assert any("arena 对话落盘未完成" in n for n in notes), notes


@pytest.mark.asyncio
async def test_round_runs_when_the_arena_package_is_absent(monkeypatch):
    """公开仓没有 arena 包（``.gitignore``）：轮次必须照跑，且**不留 notes**。

    不留 notes 是判据而不是省事：那种部署里 arena 栏本身也不存在（路由未注册），
    「这一轮没落对话」不是异常，每轮记一条只会把审计行的 notes 淹掉。这条用例在
    两种仓里都跑——本机仓靠强行置 ``None`` 走同一条分支。
    """
    monkeypatch.setattr(IO, "arena_files", None)
    h = make_harness(
        decision_text=decisions_json(
            {"action": "buy", "code": "600036.SH", "pct": 0.2, "reason": "加仓"}
        )
    )
    result = await run_once(SLOT_0935, deps=h.deps, now=NOW)

    assert result.status == R.STATUS_OK
    assert h.log["exec"] and h.log["ledger"]  # 执行段与审计段照走
    notes = h.log["ledger"][0][0].context_meta["notes"]
    assert not any("arena" in n for n in notes), notes


@pytest.mark.asyncio
async def test_roster_round_gives_every_agent_its_own_dialogue_file(arena_root):
    """一槽多家：各家落各家的文件。混写一处的后果是**只有一家看得见**，另一家整天空白。"""
    h = _roster_harness("deepseek-v4-pro", "deepseek-v4-flash")
    results = await round_tick(deps=h.deps, native=FakeNative(), now=TICK_NOW)

    assert [r.status for r in results] == [R.STATUS_OK, R.STATUS_OK]
    for agent in ("deepseek-v4-pro", "deepseek-v4-flash"):
        rows = _dialogue_rows(arena_root, agent)
        assert len(rows) == 1, agent
        assert rows[0]["signature"] == agent


# ══ E3. arena 文件面写侧（决策轮 → 前端「持仓/净值」tab，甲4）═══════════


def _position_rows(arena_root: Path, agent: str) -> list[dict]:
    """该家 ``position.jsonl`` 逐行（缺文件 = 空表）。"""
    path = arena_root / "agent_data_astock" / agent / "position" / "position.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(x)
        for x in path.read_text(encoding="utf-8").splitlines()
        if x.strip()
    ]


@pytest.mark.asyncio
async def test_successful_round_appends_one_position_snapshot(arena_root):
    """跑成的一轮 = ``position.jsonl`` 恰好多一行快照：账本持仓（后缀码）+ CASH。

    快照行**不带** ``this_action``：那个键是成交行标记（``load_trades`` 靠它筛成交），
    而决策时刻成交还没发生（桥回询异步入账）——此刻落成交行 = 把「打算」写成「已成」。
    """
    from backend.shared.decision.agent_ledger import DEFAULT_AGENT_QUOTA

    h = make_harness()
    result = await run_once(SLOT_0935, deps=h.deps, now=NOW)

    assert result.status == R.STATUS_OK
    rows = _position_rows(arena_root, "fake-model")
    assert len(rows) == 1
    row = rows[0]
    assert row["date"] == "2026-09-24"
    assert row["id"] == 1
    assert "this_action" not in row
    # 账本段 1000 股（前缀码 SH600036 归一到后缀式）+ 默认子账户现金
    assert row["positions"] == {"600036.SH": 1000.0, "CASH": DEFAULT_AGENT_QUOTA}


@pytest.mark.asyncio
async def test_position_snapshot_ids_keep_growing(arena_root):
    """两轮 = 两行，``id`` 读尾行 + 1 单调自增（前端按它排序去重）。"""
    h = make_harness()
    await run_once(SLOT_0935, deps=h.deps, now=NOW)
    await run_once(SLOT_0935, deps=h.deps, now=NOW)

    rows = _position_rows(arena_root, "fake-model")
    assert [r["id"] for r in rows] == [1, 2]


@pytest.mark.asyncio
async def test_failed_round_writes_no_position_snapshot(arena_root):
    """失败轮不落快照——补跑槽会再来一次，先写一条会在净值序列里插出重复点。"""
    h = make_harness(decision_text="今天没什么好说的")  # 无 JSON → parse_failed
    result = await run_once(SLOT_0935, deps=h.deps, now=NOW)

    assert result.status == R.STATUS_LLM_FAILED
    assert _position_rows(arena_root, "fake-model") == []


@pytest.mark.asyncio
async def test_position_write_failure_is_noted_but_does_not_sink_the_round(
    arena_root, monkeypatch
):
    """落盘失败不改轮次状态（真钱链路照走），但审计行必须留痕（同对话落盘纪律）。

    展示面可以坏，**不可以在没人知道的情况下坏**。
    """
    from backend.services.agent_arena import arena_files

    monkeypatch.setattr(arena_files, "_write_line", lambda path, record: False)
    h = make_harness()
    result = await run_once(SLOT_0935, deps=h.deps, now=NOW)

    assert result.status == R.STATUS_OK
    assert h.log["exec"] and h.log["ledger"]  # 执行段与审计段照走
    notes = h.log["ledger"][0][0].context_meta["notes"]
    assert any("arena 持仓落盘未完成" in n for n in notes), notes
    assert _position_rows(arena_root, "fake-model") == []


@pytest.mark.asyncio
async def test_position_write_exception_does_not_take_the_round_down(
    arena_root, monkeypatch
):
    """写侧**抛**异常（不是返回 False）也要被收住：一次磁盘满不该打断真钱回路。"""
    from backend.services.agent_arena import arena_files

    def boom(**_kw):
        raise OSError("No space left on device")

    monkeypatch.setattr(arena_files, "append_position", boom)
    h = make_harness()
    result = await run_once(SLOT_0935, deps=h.deps, now=NOW)

    assert result.status == R.STATUS_OK
    notes = h.log["ledger"][0][0].context_meta["notes"]
    assert any("arena 持仓落盘未完成" in n for n in notes), notes


@pytest.mark.asyncio
async def test_roster_round_gives_every_agent_its_own_position_file(arena_root):
    """一槽多家：快照落各家的 ``position.jsonl``（混写一处会让别家持仓显示成这家）。"""
    h = _roster_harness("deepseek-v4-pro", "deepseek-v4-flash")
    results = await round_tick(deps=h.deps, native=FakeNative(), now=TICK_NOW)

    assert [r.status for r in results] == [R.STATUS_OK, R.STATUS_OK]
    for agent in ("deepseek-v4-pro", "deepseek-v4-flash"):
        rows = _position_rows(arena_root, agent)
        assert len(rows) == 1, agent
        assert "600036.SH" in rows[0]["positions"], agent


# ══ F. tick：认领 → 跑 → 置键 → 状态 ════════════════════════════════
@pytest.fixture(autouse=True)
def _stub_summary_qq(monkeypatch):
    """通知面两处真发送口在**本文件一律打桩**：

    * 值班摘要（T2-5）：tick 每轮跑完都发——日志每轮落、QQ 只推动钱的轮；
    * 失败告警的 ``default_notifier``：不上报桩就会走 ``publish_notification``
      （落库）并触发 error 级 QQ 旁路——本仓测试容器里 QQ 凭据是活的
      （``config/runtime.env``），下面这些跑真实一轮的用例会把测试消息真推给值班。

    默认桩 = 通道不可用（返回 False，与未配置环境同形），deliver_alert/摘要的
    失败分支照走；单项测试要验发送行为的自行覆盖。
    """

    async def _unavailable(*args, **kwargs) -> bool:
        return False

    monkeypatch.setattr(A, "_send_summary_qq", _unavailable)
    monkeypatch.setattr(A, "default_notifier", lambda: _unavailable)


@pytest.mark.asyncio
async def test_tick_does_not_run_on_a_non_trading_day():
    h = make_harness(trading_day=False)
    native = FakeNative()
    assert await round_tick(deps=h.deps, native=native, now=TICK_NOW) == ()
    assert native.calls == []  # 连认领都不做


@pytest.mark.asyncio
async def test_tick_does_not_run_when_the_calendar_is_unavailable():
    h = make_harness()

    async def boom(day):
        raise RuntimeError("日历表读不到")

    native = FakeNative()
    assert (
        await round_tick(
            deps=replace(h.deps, is_trading_day=boom), native=native, now=TICK_NOW
        )
        == ()
    )
    assert native.calls == []


@pytest.mark.asyncio
async def test_tick_skips_a_slot_someone_else_claimed():
    h = make_harness()
    native = FakeNative()
    native.store[slot_keys(DAY, SLOT_0935)[0]] = "1"  # 别处已认领
    assert await round_tick(deps=h.deps, native=native, now=TICK_NOW) == ()
    assert h.log["llm"] == []


@pytest.mark.asyncio
async def test_tick_claim_error_never_marks_the_slot_as_done():
    """认领写不进去时必须**不跑**，也不能把失败当「已跑过」——否则这一槽永不执行。"""
    h = make_harness()
    native = FakeNative(fail_claim=True)
    assert await round_tick(deps=h.deps, native=native, now=TICK_NOW) == ()
    assert h.log["llm"] == []
    assert all("done" not in k for k in native.store)  # 没有 done 键被写下


@pytest.mark.asyncio
async def test_tick_does_not_mark_an_aborted_round_as_done():
    """abort 的轮次**不算跑过**：done 键一写，当天的补跑槽就再也不来了。

    这是「aborted 不置 done」在 tick 层的落点：``round_tick`` 按 ``result.ok`` 置键，
    而 abort 的语义正是「本轮什么也没做」。置了键 = 把一次瞬时故障升级成「今天跑过了」。
    """
    h = make_harness(account_user=lambda: "")  # 账户身份读不到 → abort
    native = FakeNative()
    results = await round_tick(deps=h.deps, native=native, now=TICK_NOW)
    assert [r.status for r in results] == [R.STATUS_ABORTED]
    slot_key, done_key = slot_keys(DAY, SLOT_0935)
    assert slot_key in native.store  # 认领键留着（EX 2d 自然过期）
    assert done_key not in native.store  # 但绝不算「跑过」
    assert h.log["llm"] == [] and h.log["exec"] == []


@pytest.mark.asyncio
async def test_tick_warns_when_a_claim_has_no_done_key(caplog):
    """认领键在、done 键不在 = 上一轮半途夭折。

    「有人正在跑」与「跑到一半没了」在认领键上长得一模一样，但后者意味着状态键、
    审计表里**都没有这一轮**，心跳照写（worker 活着）——唯一的痕迹就是那行日志。
    所以它必须是 warning 并给出补跑入口，不能降级成与「正常跳过」同一句 info。
    """
    h = make_harness()
    native = FakeNative()
    slot_key, _ = slot_keys(DAY, SLOT_0935)
    native.store[slot_key] = CLAIM_AUTO  # 有人认了，却从没写 done
    with caplog.at_level("INFO"):
        assert await round_tick(deps=h.deps, native=native, now=TICK_NOW) == ()
    hit = [r for r in caplog.records if "已被认领" in r.message]
    assert hit and all(r.levelname == "WARNING" for r in hit)
    assert any("夭折" in r.getMessage() and "--force" in r.getMessage() for r in hit)
    assert h.log["llm"] == []


@pytest.mark.asyncio
async def test_tick_stays_quiet_when_a_claimed_slot_already_has_its_done_key(caplog):
    """认领 + done 都在 = 「今天跑过了」的正常形态：不告警（告警要有信息量）。"""
    h = make_harness()
    native = FakeNative()
    slot_key, done_key = slot_keys(DAY, SLOT_0935)
    native.store[slot_key] = CLAIM_AUTO
    native.store[done_key] = "1"
    with caplog.at_level("INFO"):
        assert await round_tick(deps=h.deps, native=native, now=TICK_NOW) == ()
    hits = [r for r in caplog.records if "已被认领" in r.message]
    assert hits  # 确实走到了「跳过」这条分支（少了这句，下面的断言恒真）
    assert all(r.levelno < logging.WARNING for r in hits)
    assert h.log["llm"] == []


@pytest.mark.asyncio
async def test_tick_does_not_guess_abandonment_when_the_done_key_is_unreadable(caplog):
    """done 键读不到时**不猜夭折**：认领可能是别人正在跑的那一轮，猜错会喊狼来了。"""
    h = make_harness()
    native = FakeNative(fail_get=True)
    slot_key, _ = slot_keys(DAY, SLOT_0935)
    native.store[slot_key] = CLAIM_AUTO
    with caplog.at_level("INFO"):
        assert await round_tick(deps=h.deps, native=native, now=TICK_NOW) == ()
    hits = [r for r in caplog.records if "已被认领" in r.message]
    assert hits and all(r.levelno < logging.WARNING for r in hits)
    assert h.log["llm"] == []


@pytest.mark.asyncio
async def test_tick_without_a_redis_client_never_runs(monkeypatch):
    """拿不到原生客户端 = 没有认领/去重能力 = 不跑。

    没有去重就开跑，两个进程会同时把同一轮决策下成两批真单——「不知道有没有人
    在跑」在这种场景下的正确处置是**不动**。（构造失败要抛，不能被吞成 ``None``。）
    """

    def boom():
        raise RuntimeError("redis 构造不了")

    monkeypatch.setattr(TICK, "native_redis_client", boom)
    h = make_harness()
    assert await round_tick(deps=h.deps, native=None, now=TICK_NOW) == ()
    assert h.log["llm"] == []


@pytest.mark.asyncio
async def test_tick_done_key_write_failure_still_reports_the_round(caplog):
    """done 键写不成：本轮结果照常返回（决策已经做了、单已经下了），但补跑槽会重来。

    这条差别是**有意的**：done 键是「防重跑」不是「防重复下单」，后者靠幂等键
    （同槽重试同 round_id）。写不成只影响补跑槽知道不知道自己已经跑过——所以
    宁可多跑一轮（幂等键挡住重复腿），不可静默丢掉这一轮的状态。
    """
    h = make_harness()
    native = FakeNative(fail_done_set=True)
    with caplog.at_level("ERROR"):
        results = await round_tick(deps=h.deps, native=native, now=TICK_NOW)
    assert [r.status for r in results] == [R.STATUS_OK]
    assert slot_keys(DAY, SLOT_0935)[1] not in native.store  # done 键确实没写上
    assert any("done 键写入失败" in r.message for r in caplog.records)
    assert json.loads(native.store[LAST_KEY])["status"] == "ok"  # 状态照写


@pytest.mark.asyncio
async def test_tick_closes_a_client_it_opened_itself(monkeypatch):
    """自己开的连接自己关：worker 是常驻循环，每 tick 漏一条连接就是稳定泄漏。"""
    h = make_harness()
    native = FakeNative()
    monkeypatch.setattr(TICK, "native_redis_client", lambda: native)
    results = await round_tick(deps=h.deps, native=None, now=TICK_NOW)
    assert [r.status for r in results] == [R.STATUS_OK]
    assert native.closed is True

    # 传进来的客户端是**调用方的**：不许替别人关（下一个 tick 还要用同一个）。
    borrowed = FakeNative()
    await round_tick(deps=h.deps, native=borrowed, now=TICK_NOW)
    assert borrowed.closed is False

    class _StubbornClose(FakeNative):
        def close(self):
            raise RuntimeError("连接关不掉")

    monkeypatch.setattr(TICK, "native_redis_client", lambda: _StubbornClose())
    results = await round_tick(deps=h.deps, native=None, now=TICK_NOW)
    assert [r.status for r in results] == [R.STATUS_OK]  # 关不掉也吞掉：结果不能丢


@pytest.mark.asyncio
async def test_tick_marks_done_and_writes_status_on_success():
    h = make_harness()
    native = FakeNative()
    results = await round_tick(deps=h.deps, native=native, now=TICK_NOW)
    assert [r.status for r in results] == [R.STATUS_OK]
    assert native.store[slot_keys(DAY, SLOT_0935)[1]] == "1"  # done（schema 维度）
    last = json.loads(native.store[LAST_KEY])
    assert last["status"] == "ok" and last["round_id"] == "rnd-20260924-0935"
    assert any(c[0] == "lpush" and c[1] == LOG_KEY for c in native.calls)


@pytest.mark.asyncio
async def test_tick_failed_round_does_not_mark_done():
    h = make_harness(decision_text="没有 JSON")
    native = FakeNative()
    results = await round_tick(deps=h.deps, native=native, now=TICK_NOW)
    assert [r.status for r in results] == [R.STATUS_LLM_FAILED]
    assert all("done" not in k for k in native.store)  # 补跑槽还能再来一次


@pytest.mark.asyncio
async def test_catch_up_slot_is_skipped_when_the_day_already_has_a_decision():
    h = make_harness()
    native = FakeNative()
    native.store[slot_keys(DAY, SLOT_1005)[1]] = "1"  # 当日已有 rebalance 决策
    results = await round_tick(deps=h.deps, native=native, now=TICK_1005)
    assert [r.status for r in results] == [STATUS_SKIPPED]
    assert results[0].slot.hhmm == "1005" and "已出过" in results[0].note
    assert h.log["llm"] == []  # 补跑没有真的问模型


@pytest.mark.asyncio
async def test_main_slot_does_not_consult_the_done_key():
    """主槽不查 done 键：09:35 出了决策，10:05 的补跑才该跳过——反过来不成立。"""
    h = make_harness()
    native = FakeNative()
    native.store[slot_keys(DAY, SLOT_0935)[1]] = "1"  # 当日 rebalance 的 done 已置
    results = await round_tick(deps=h.deps, native=native, now=TICK_NOW)
    assert [r.status for r in results] == [R.STATUS_OK]  # 09:35 不是补跑槽，照跑


@pytest.mark.asyncio
async def test_catch_up_does_not_run_when_the_done_key_cannot_be_read():
    """补跑槽读不到 done 键 ⇒ **不跑**（SKIPPED），且放掉认领让窗口内还能再试。

    「补跑槽存在」的全部意义是「当日还没出过这个 schema 的决策」；读不到 = 不知道
    出没出过。此时跑下去可能发出**第二批**建仓计划——补跑的 round_id 与主槽不同，
    订单幂等键挡不住重复腿（同槽重试才同键）。漏跑一轮的代价是「今天少建一次仓」，
    重跑一轮的代价是「同一批意图下两次单」，两者不对称，所以 fail-closed。

    认领键必须放掉：不放的话同一个 45 分钟宽限窗里后续每次 tick 都只回一句「已被
    认领」，故障修好了也没人再来——而这条路径**从来没有真的跑过一轮**，放键不会
    让任何东西重复执行。
    """
    h = make_harness()
    native = FakeNative(fail_get=True)
    results = await round_tick(deps=h.deps, native=native, now=TICK_1005)
    assert [r.status for r in results] == [STATUS_SKIPPED]
    assert "done 键读取失败" in results[0].note
    assert "--force" in results[0].note  # 出口：修不好时人要能显式重跑
    assert h.log["llm"] == [] and h.log["exec"] == [] and h.log["watch"] == []
    slot_key, done_key = slot_keys(DAY, SLOT_1005)
    assert slot_key not in native.store  # 认领已放掉
    assert done_key not in native.store  # 也绝不算「跑过」
    assert json.loads(native.store[LAST_KEY])["status"] == "skipped"  # 状态键如实


@pytest.mark.asyncio
async def test_catch_up_retries_within_the_window_once_the_read_recovers():
    """同一条失败的补跑，读恢复后**在下一次 tick 里真的跑起来**（认领键没被占死）。"""
    h = make_harness()
    native = FakeNative(fail_get=True)
    assert [
        r.status for r in await round_tick(deps=h.deps, native=native, now=TICK_1005)
    ] == [STATUS_SKIPPED]
    native.fail_get = False  # 运维修好了
    results = await round_tick(deps=h.deps, native=native, now=TICK_1005)
    assert [r.status for r in results] == [R.STATUS_OK]
    assert h.log["llm"] != []  # 这一次真的问了模型


@pytest.mark.asyncio
async def test_catch_up_claim_release_failure_still_reports_skipped(caplog):
    """认领键删不掉（Redis 抖）不能让这一轮变成 error：只是少一次重试机会。"""
    h = make_harness()
    native = FakeNative(fail_get=True, fail_delete=True)
    with caplog.at_level("WARNING"):
        results = await round_tick(deps=h.deps, native=native, now=TICK_1005)
    assert [r.status for r in results] == [STATUS_SKIPPED]
    assert any("认领键释放失败" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_tick_never_marks_done_when_the_quote_gate_aborts(caplog):
    """H1 在 tick 层的落点：行情整段不可用 ⇒ 不置 done 键 ⇒ 补跑槽照来。

    09:35 建仓轮在「池里一只价都没有」时 abort；若它在 tick 层仍被记成「跑过」，
    10:05/11:05 两个 rebalance 补跑槽就都不会来——行情十分钟后恢复了也没用。
    """
    h = make_harness(quote_client=lambda: None)
    native = FakeNative()
    results = await round_tick(deps=h.deps, native=native, now=NOW, slot=SLOT_0935)
    assert [r.status for r in results] == [R.STATUS_ABORTED]
    done_key = slot_keys(DAY, SLOT_0935)[1]
    assert done_key not in native.store
    # 紧接着的补跑槽：done 键不在 ⇒ 它真的会跑（这里用同一批替身再 tick 一次）
    catch_up = await round_tick(deps=h.deps, native=native, now=TICK_1005)
    assert [r.status for r in catch_up] == [R.STATUS_ABORTED]  # 行情还没好，仍在 abort
    assert h.log["llm"] == []


@pytest.mark.asyncio
async def test_force_reruns_even_when_done_is_set():
    h = make_harness()
    native = FakeNative()
    native.store[slot_keys(DAY, SLOT_1005)[1]] = "1"
    results = await round_tick(deps=h.deps, native=native, now=TICK_1005, force=True)
    assert [r.status for r in results] == [R.STATUS_OK]


@pytest.mark.asyncio
async def test_force_takes_over_an_already_claimed_slot():
    """``--force`` 必须能**抢占已认领的槽位**，否则「重跑」对任何跑过的槽位都是空转。

    认领键（``SET NX EX 2d``）在 done 键之前就把重跑挡住了：只忽略 done 键的实现，
    对 09:35 已经自动跑过一次的槽位，CLI 会打一行 info 然后打印「无到点槽位」退出
    ——运营据此去查时刻表，而真因是槽位被自己早上那轮认领着。
    """
    h = make_harness()
    native = FakeNative()
    slot_key = slot_keys(DAY, SLOT_0935)[0]
    native.store[slot_key] = CLAIM_AUTO  # 自动轮已认领
    results = await round_tick(deps=h.deps, native=native, now=TICK_NOW, force=True)
    assert [r.status for r in results] == [R.STATUS_OK]
    assert native.store[slot_key] == CLAIM_MANUAL  # 认领键改写：值即「谁认的」
    assert h.log["llm"], "抢占后必须真的问一次模型"


@pytest.mark.asyncio
async def test_force_takeover_is_written_into_the_round_note():
    """抢占要在**这一轮的结果里**留痕：状态键只打 ok 的话，复盘分不清这批单是
    计划内的一轮还是人手点出来的（后者可以点很多次）。"""
    h = make_harness()
    native = FakeNative()
    slot_key = slot_keys(DAY, SLOT_0935)[0]
    native.store[slot_key] = CLAIM_AUTO
    results = await round_tick(deps=h.deps, native=native, now=TICK_NOW, force=True)
    assert results[0].note.startswith("手动重跑：抢占已认领槽位")
    assert CLAIM_AUTO in results[0].note  # 原认领是谁也记下来
    assert json.loads(native.store[LAST_KEY])["note"].startswith("手动重跑")


@pytest.mark.asyncio
async def test_force_on_a_free_slot_leaves_no_takeover_note():
    """没人认领时不许谎报「抢占」——那会把一次正常的手动首跑写成异常。"""
    h = make_harness()
    native = FakeNative()
    results = await round_tick(deps=h.deps, native=native, now=TICK_NOW, force=True)
    assert [r.status for r in results] == [R.STATUS_OK]
    assert "抢占" not in results[0].note
    assert native.store[slot_keys(DAY, SLOT_0935)[0]] == CLAIM_MANUAL


@pytest.mark.asyncio
async def test_force_still_blocks_a_second_automatic_tick():
    """覆盖写只能拦住**之后**的自动 tick：手动跑完那一槽，自动轮不许再跑一遍。

    这条是「抢占」与「放开闸门」的分界——若 force 走的是「删键」而不是「改写」，
    紧接着的一次自动 tick 就会把同一槽再跑一轮，同槽两批单。
    """
    h = make_harness()
    native = FakeNative()
    forced = await round_tick(deps=h.deps, native=native, now=TICK_NOW, force=True)
    assert [r.status for r in forced] == [R.STATUS_OK]
    assert await round_tick(deps=h.deps, native=native, now=TICK_NOW) == ()
    assert len(h.log["llm"]) == 1  # 模型只被问了一次


@pytest.mark.asyncio
async def test_force_read_failure_takes_over_quietly_but_still_runs():
    """抢占前的「读一眼原认领」失败：当作没人认领（少一条注记），但**照跑**。

    读失败若被当成「已经在跑」就会把重跑吞掉——运营按了重跑、命令返回 0 还是 1
    都拿不到结论，且没有任何一层会报告「这轮没跑」。
    """
    h = make_harness()
    native = FakeNative(fail_get=True)
    native.store[slot_keys(DAY, SLOT_0935)[0]] = CLAIM_AUTO
    results = await round_tick(deps=h.deps, native=native, now=TICK_NOW, force=True)
    assert [r.status for r in results] == [R.STATUS_OK]
    assert "抢占" not in results[0].note


@pytest.mark.asyncio
async def test_expired_slot_is_not_caught_up():
    h = make_harness()
    native = FakeNative()
    late = datetime(2026, 9, 24, 10, 51, tzinfo=CST)  # 10:05 已过期 46 分钟
    assert await round_tick(deps=h.deps, native=native, now=late) == ()
    assert native.calls == []


@pytest.mark.asyncio
async def test_explicit_slot_bypasses_the_schedule_for_operators():
    h = make_harness()
    native = FakeNative()
    results = await round_tick(deps=h.deps, native=native, now=NOW, slot=SLOT_1005)
    assert [r.slot.hhmm for r in results] == ["1005"]  # 到点与否由人说了算


# ══ F2. 多模型名册（P2.9）：一槽多家，各家一本账 ═════════════════════
def _roster_harness(*agents: str, **over):
    """一份名册替身：每家一个 ``AgentRun``，各带绑定（身份与模型名同名）。

    ``decision_text`` 与单家 harness 同一语义（不传 = 各家持有的那家观望）；每家的
    调用都记进 ``log["llm"]`` 并带上 agent，让「哪家真问过模型」可断言。
    断言另有四处：``log["agent_ledger"]``（分账按家切段）、``log["watch"]``（守护归属）、
    ``log["ledger"]``（审计行 ``agent`` 与 ``id`` 不撞主键）、状态键 ``last:{agent}``。
    """
    text = over.get("decision_text") or decisions_json(
        {"action": "hold", "code": "600036.SH", "reason": "名册：各家观望"}
    )
    h = make_harness(**over)

    def _run(agent: str) -> AgentRun:
        def _load() -> LLMBinding:
            def _decide(prompt, schema):
                h.log["llm"].append(
                    {"agent": agent, "prompt": prompt, "schema": schema}
                )
                return attempt_from(text, schema=schema)

            return LLMBinding(model=agent, decide=_decide)

        return AgentRun(agent=agent, load_llm=_load)

    h.deps = replace(h.deps, roster=lambda: tuple(_run(a) for a in agents))
    return h


@pytest.mark.asyncio
async def test_tick_runs_every_roster_agent_in_one_slot():
    """一家一槽一轮：两家都有决策、都写账本、都写守护规则——**各写各的**。"""
    h = _roster_harness("deepseek-v4-pro", "deepseek-v4-flash")
    native = FakeNative()
    results = await round_tick(deps=h.deps, native=native, now=TICK_NOW)

    assert [r.agent for r in results] == ["deepseek-v4-pro", "deepseek-v4-flash"]
    assert [r.status for r in results] == [R.STATUS_OK, R.STATUS_OK]
    # 分账账本按家切段：第二家绝不许看见第一家的账（2026-09-08 跨 agent 卖仓那条）
    assert [a for _t, _u, a in h.log["agent_ledger"]] == [
        "deepseek-v4-pro",
        "deepseek-v4-flash",
    ]
    # 审计行：两家的 id 不同（同轮同序号同码同动作也不许撞主键）
    ids = [rows[0].id for rows in h.log["ledger"]]
    assert len(ids) == 2 and ids[0] != ids[1]
    assert [rows[0].agent for rows in h.log["ledger"]] == [
        "deepseek-v4-pro",
        "deepseek-v4-flash",
    ]
    assert len(h.log["llm"]) == 2  # 两家各问了一次


@pytest.mark.asyncio
async def test_watch_rules_are_written_per_agent_not_as_one_group():
    """守护规则归属按家（``llm:<agent>``）：盘中轮两家各写各的那组。

    写错归属的后果不是「少一条规则」：整组替换只动 ``owner == 自己`` 那组，两家
    若共用一个 owner，后跑的那家会把前一家的止损单**整组摘掉**——账户当场裸奔。
    """
    h = _roster_harness("pro", "flash", decision_text=WATCH_TEXT)
    native = FakeNative()
    results = await round_tick(deps=h.deps, native=native, now=TEN_05, slot=SLOT_1000)
    assert [r.status for r in results] == [R.STATUS_OK, R.STATUS_OK]
    assert [w["agent"] for w in h.log["watch"]] == ["pro", "flash"]
    assert [r.watch_armed for r in results] == [1, 1]


@pytest.mark.asyncio
async def test_tick_keeps_claim_and_done_keys_per_agent():
    """键必须按家分段：**一家的 done 键不许替另一家说话**。

    不分段的后果是「迁完只剩一个模型」：第一家认领并置 done 之后，第二家进到
    同一个槽位时被自己的认领键挡住（或补跑槽读到的 done 键说「今天出过了」），
    状态键上一切正常，而另外两家整天空转——正是迁移计划里警告的那一条。
    """
    h = _roster_harness("pro", "flash")
    native = FakeNative()
    await round_tick(deps=h.deps, native=native, now=TICK_NOW)

    for agent in ("pro", "flash"):
        slot_key, done_key = slot_keys(DAY, SLOT_0935, agent)
        assert slot_key in native.store and done_key in native.store
        assert json.loads(native.store[f"{LAST_KEY}:{agent}"])["agent"] == agent
    assert slot_keys(DAY, SLOT_0935)[1] not in native.store  # 不带段的那把不写


@pytest.mark.asyncio
async def test_tick_one_agent_failure_does_not_take_the_others_down():
    """某家 LLM 没配好 ⇒ 只有那家记 llm_failed，其余家照跑（隔壁的 ``continue``）。

    一家的 key 过期/欠费是**常态**，不是事故；让整槽 abort 等于「便宜的模型欠费
    导致主力模型当天不决策」——而那正是这次要保住的能力。
    """
    from backend.shared.decision.llm_call import LLMNotConfigured

    h = _roster_harness("pro", "flash")
    good = h.deps.roster

    def _load_bad():
        raise LLMNotConfigured("决策 LLM 未配置：QM_DECISION_LLM_API_KEY 为空")

    def _roster():
        pro, flash = good()
        return (AgentRun(agent=pro.agent, load_llm=_load_bad), flash)

    h.deps = replace(h.deps, roster=_roster)
    native = FakeNative()
    results = await round_tick(deps=h.deps, native=native, now=TICK_NOW)

    assert [(r.agent, r.status) for r in results] == [
        ("pro", R.STATUS_LLM_FAILED),
        ("flash", R.STATUS_OK),
    ]
    assert json.loads(native.store[f"{LAST_KEY}:pro"])["status"] == "llm_failed"
    assert slot_keys(DAY, SLOT_0935, "pro")[1] not in native.store  # 没跑成 ⇒ 不算跑过
    assert slot_keys(DAY, SLOT_0935, "flash")[1] in native.store
    assert h.log["llm"], "第二家必须真的问过模型"


@pytest.mark.asyncio
async def test_catch_up_slot_resumes_only_the_agent_that_missed():
    """补跑槽按家续：pro 当日已出过 rebalance，flash 没有 ⇒ 10:47 只补 flash 一家。

    done 键是 **schema 维度**（09:35 建仓轮置的键，10:05/10:47 补跑槽读的是同一把）；
    分段之后每家读**自己那把**——pro 跑成了不许替 flash 说话，否则 flash 当天一次
    决策都没有（分账里的货没人管），而状态键上一切正常。
    """
    h = _roster_harness("pro", "flash")
    native = FakeNative()
    native.store[slot_keys(DAY, SLOT_0935, "pro")[1]] = "1"  # pro 出过决策
    results = await round_tick(deps=h.deps, native=native, now=TICK_1005)
    assert [(r.agent, r.status) for r in results] == [
        ("pro", STATUS_SKIPPED),
        ("flash", R.STATUS_OK),
    ]
    assert [t["agent"] for t in h.log["llm"]] == ["flash"]  # pro 没重问模型
    assert slot_keys(DAY, SLOT_0935, "flash")[1] in native.store  # flash 补上了

    # 窗口内不再重跑（认领键按家、按槽按着）：要来第二次只能由人点名，而点名的
    # 那家**只跑那家**——静默跑全员 = 运营以为在补 flash，实际三家都下了单。
    assert await round_tick(deps=h.deps, native=native, now=TICK_1005) == ()
    h.log["llm"].clear()
    forced = await round_tick(
        deps=h.deps, native=native, now=TICK_1005, force=True, agent="flash"
    )
    assert [(r.agent, r.status) for r in forced] == [("flash", R.STATUS_OK)]
    assert [t["agent"] for t in h.log["llm"]] == ["flash"]


@pytest.mark.asyncio
async def test_agent_filter_runs_only_the_named_agent():
    """``--agent``：运营点名一家（例：那家的 key 刚续上）。"""
    h = _roster_harness("pro", "flash")
    native = FakeNative()
    results = await round_tick(deps=h.deps, native=native, now=TICK_NOW, agent="flash")
    assert [r.agent for r in results] == ["flash"]
    assert slot_keys(DAY, SLOT_0935, "pro")[0] not in native.store  # pro 的键没动

    with pytest.raises(ValueError):
        await round_tick(deps=h.deps, native=native, now=TICK_NOW, agent="nope")


@pytest.mark.asyncio
async def test_roster_resolution_failure_marks_the_slot_llm_failed():
    """名册本身坏了（写错一项）⇒ 每槽如实记 llm_failed + 原因，**不许回落到单家**。

    回落是这里最坏的选择：全局三件套还有效时它会让轮次**照常下单**，只是三家变
    一家，而名册里的另外两家再也不出现——审计上看起来像「模型没意见」。
    """
    h = make_harness()
    from backend.shared.decision.llm_call import LLMNotConfigured

    def _boom():
        raise LLMNotConfigured("QM_DECISION_LLM_ROSTER 第 2 项缺 model")

    h.deps = replace(h.deps, roster=_boom)
    native = FakeNative()
    results = await round_tick(deps=h.deps, native=native, now=TICK_NOW)

    assert len(results) == 1 and results[0].status == R.STATUS_LLM_FAILED
    assert "第 2 项缺 model" in results[0].note
    assert h.log["llm"] == [] and h.log["exec"] == []  # 一轮都没跑起来
    assert slot_keys(DAY, SLOT_0935)[1] not in native.store


@pytest.mark.asyncio
async def test_empty_roster_keeps_the_single_agent_path():
    """``roster`` 给空（测试替身/未接线的 deps）⇒ 走单家：键不分段、只跑一轮。"""
    h = make_harness(roster=lambda: ())
    native = FakeNative()
    results = await round_tick(deps=h.deps, native=native, now=TICK_NOW)
    assert [r.status for r in results] == [R.STATUS_OK]
    # 认领/done 键**不分段**（升级当天已跑过的槽还认得出）；状态键照旧写 ``last``，
    # 按家的那把由模型名给出（身份 = 归一后的模型名），只是**绝不留空段键** ——
    # ``last:`` 空段没人读得到，只会变成 Redis 里的孤儿。
    assert slot_keys(DAY, SLOT_0935)[0] in native.store
    assert LAST_KEY in native.store
    assert f"{LAST_KEY}:" not in native.store


# ══ G. worker / CLI 开关（驱动层：decision_round_runner） ═════════════
def test_worker_returns_immediately_unless_the_flag_is_exactly_true(monkeypatch):
    """未开（含 "1"/空串）时立即返回——若误开，2 秒超时会把用例判红而不是挂住。"""

    async def _run(flag_value: str) -> None:
        monkeypatch.setenv(RUN.ENV_FLAG, flag_value)
        await asyncio.wait_for(RUN.run_decision_round_worker(), timeout=2)

    monkeypatch.delenv(RUN.ENV_FLAG, raising=False)
    asyncio.run(asyncio.wait_for(RUN.run_decision_round_worker(), timeout=2))
    asyncio.run(_run("1"))
    asyncio.run(_run(""))


def test_poll_and_grace_env_reading_falls_back_on_garbage(monkeypatch):
    """``int("")``/``int("abc")`` 会让 worker 在启动第一行就炸——回退值要顶得上。"""
    monkeypatch.delenv(RUN.ENV_POLL_S, raising=False)
    monkeypatch.delenv(RUN.ENV_GRACE_MIN, raising=False)
    assert RUN._poll_s() == 30 and RUN._grace_min() == 45
    monkeypatch.setenv(RUN.ENV_POLL_S, "abc")
    monkeypatch.setenv(RUN.ENV_GRACE_MIN, "  ")
    assert RUN._poll_s() == 30 and RUN._grace_min() == 45
    monkeypatch.setenv(RUN.ENV_POLL_S, "1")  # 下界：不许打出比 5s 更密的轮询
    monkeypatch.setenv(RUN.ENV_GRACE_MIN, "-3")
    assert RUN._poll_s() == 5 and RUN._grace_min() == 0
    monkeypatch.setenv(RUN.ENV_POLL_S, "60")
    monkeypatch.setenv(RUN.ENV_GRACE_MIN, "10")
    assert RUN._poll_s() == 60 and RUN._grace_min() == 10


@pytest.mark.asyncio
async def test_worker_loop_heartbeats_logs_and_survives_a_failing_round(
    monkeypatch, caplog
):
    """循环体三件事：心跳（唯一可观测信号）、日志格式（占位符对不上会当场抛）、
    一轮炸了下一轮继续（异常不许带走 worker）。"""

    class _Stop(Exception):
        pass

    shim_sleeps: list[float] = []

    class _Sleep:
        """只替掉本模块看到的 ``asyncio``：不去改真模块（那会波及整个进程）。"""

        @staticmethod
        async def sleep(seconds):
            shim_sleeps.append(seconds)
            if len(shim_sleeps) >= 2:
                raise _Stop

    beats: list[str] = []
    calls = {"n": 0}

    async def fake_tick(*, grace_min):
        calls["n"] += 1
        if calls["n"] == 1:
            return (
                RoundResult(
                    status=R.STATUS_OK,
                    day=DAY,
                    slot=SLOT_0935,
                    round_id="rnd-20260924-0935",
                    decisions=1,
                    legs=1,
                    submitted=1,
                    audit_rows=1,
                    note="提交完成",
                ),
            )
        raise RuntimeError("这一轮炸了")

    wake_calls = {"n": 0}

    async def fake_wake():
        wake_calls["n"] += 1
        return ()

    monkeypatch.setenv(RUN.ENV_FLAG, "true")
    # 补丁打在 **runner** 上（它按值 import 了 round_tick）：打在编排层上不生效，
    # 这个用例会退化成「真的去连 Redis 跑一轮」。wake_tick 同理——不桩掉它就是
    # 真构造生产 deps 去连 Redis（交易时段里甚至会真的触发一轮合成槽）。
    monkeypatch.setattr(RUN, "round_tick", fake_tick)
    monkeypatch.setattr(RUN, "wake_tick", fake_wake)
    monkeypatch.setattr(RUN, "asyncio", _Sleep)
    monkeypatch.setattr(
        "backend.shared.scheduler_registry.heartbeat",
        lambda key, **kw: beats.append(key) or True,
    )
    with caplog.at_level("INFO"):
        with pytest.raises(_Stop):
            await RUN.run_decision_round_worker()

    assert beats == ["decision_round", "decision_round"]  # 每轮都敲，包括失败那轮
    assert calls["n"] == 2 and shim_sleeps == [30, 30]
    # 唤醒评估与 tick 同任务串行、**每轮都评**（哪怕 tick 炸了那轮）
    assert wake_calls["n"] == 2
    rendered = [r.getMessage() for r in caplog.records]
    assert any("rnd-20260924-0935" in m and "status=ok" in m for m in rendered)
    assert any("tick 异常" in m and "这一轮炸了" in m for m in rendered)


@pytest.mark.asyncio
async def test_worker_keeps_looping_when_the_heartbeat_write_throws(monkeypatch):
    """心跳是 best-effort：它写不进去只该少一个可观测信号，不该停掉决策轮。"""

    class _Stop(Exception):
        pass

    class _Sleep:
        @staticmethod
        async def sleep(_seconds):
            raise _Stop

    def boom_heartbeat(key, **kw):
        raise RuntimeError("心跳写不下")

    async def no_slots(*, grace_min):
        return ()

    async def no_wake():
        return ()

    monkeypatch.setenv(RUN.ENV_FLAG, "true")
    monkeypatch.setattr(RUN, "round_tick", no_slots)
    monkeypatch.setattr(RUN, "wake_tick", no_wake)  # 不许真连 Redis（同上）
    monkeypatch.setattr(RUN, "asyncio", _Sleep)
    monkeypatch.setattr("backend.shared.scheduler_registry.heartbeat", boom_heartbeat)
    with pytest.raises(_Stop):
        await RUN.run_decision_round_worker()  # 走到了 sleep 就是活着的


def test_cli_dry_run_lists_slots(capsys, monkeypatch):
    """``--dry-run`` 也要说清**名册**：运营拿它核对「今天这一槽会有几家出主意」。"""
    for name in ("QM_DECISION_LLM_ROSTER",):
        monkeypatch.delenv(name, raising=False)
    assert RUN.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "到点槽位" in out and "09:35" in out and "14:45" in out
    assert "名册：" in out  # 未开名册时也要给出「单家（…）」这句

    monkeypatch.setenv(
        "QM_DECISION_LLM_ROSTER",
        '[{"model": "pro", "base_url_env": "U", "api_key_env": "K"},'
        ' {"model": "flash", "base_url_env": "U", "api_key_env": "K"}]',
    )
    monkeypatch.setenv("U", "https://example.invalid/v1")
    monkeypatch.setenv("K", "sk-not-printed")
    assert RUN.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "名册 2 家" in out and "'pro'" in out and "'flash'" in out
    assert "sk-not-printed" not in out  # 名册行只报家名，绝不回显 key


def test_cli_rejects_an_unknown_slot(capsys):
    assert RUN.main(["--slot", "9999"]) == 2
    assert "未知槽位" in capsys.readouterr().err


def test_cli_rejects_an_agent_that_is_not_in_the_roster(capsys, monkeypatch):
    """``--agent`` 校验要在**跑之前**：点名不在名册里 = 参数错（rc 2）。

    放进去的两种坏法：① 在 tick 里抛栈（操作员看到一屏 traceback）；② 静默跑全员
    （运营以为在补一家，实际名册上每家都下了一批单）。两种都不许有。
    """
    monkeypatch.setenv(
        "QM_DECISION_LLM_ROSTER",
        '[{"model": "pro", "base_url_env": "U", "api_key_env": "K"}]',
    )
    monkeypatch.setenv("U", "https://example.invalid/v1")
    monkeypatch.setenv("K", "sk-x")
    assert RUN.main(["--agent", "nope"]) == 2
    err = capsys.readouterr().err
    assert "'nope'" in err and "pro" in err

    # 名册没开（单家路径）：没有可点名的家 —— 也要 rc 2，而不是「点了名却跑全员」
    monkeypatch.delenv("QM_DECISION_LLM_ROSTER", raising=False)
    assert RUN.main(["--agent", "pro"]) == 2
    assert "不在名册里" in capsys.readouterr().err


def test_cli_stops_on_a_broken_roster_before_touching_anything(capsys, monkeypatch):
    """名册写坏一项 = 配置错（rc 2），**不进真钱路径**（不取账户、不问模型）。"""
    monkeypatch.setenv("QM_DECISION_LLM_ROSTER", '[{"base_url": "x"}]')
    called = []

    async def _never(**kw):  # pragma: no cover - 真被调用就是回归
        called.append(kw)
        return ()

    monkeypatch.setattr(RUN, "round_tick", _never)
    assert RUN.main([]) == 2
    err = capsys.readouterr().err
    assert "名册不可用" in err and "QM_DECISION_LLM_ROSTER" in err
    assert called == []


def test_cli_passes_the_agent_filter_down_and_prints_it(monkeypatch, capsys):
    """点名一家 ⇒ 只跑那家，输出行里带 ``agent=``（运维 grep 的对象）。"""
    import backend.shared.database_manager_v2 as _dbm

    async def _no_db():
        pass

    seen = {}

    async def _one(**kw):
        seen.update(kw)
        return (
            RoundResult(
                status=R.STATUS_OK,
                day=DAY,
                slot=SLOT_0935,
                round_id="rnd-20260924-0935",
                agent=kw.get("agent") or "",
            ),
        )

    monkeypatch.setenv(
        "QM_DECISION_LLM_ROSTER",
        '[{"model": "pro", "base_url_env": "U", "api_key_env": "K"}]',
    )
    monkeypatch.setenv("U", "https://example.invalid/v1")
    monkeypatch.setenv("K", "sk-x")
    monkeypatch.setattr(RUN, "round_tick", _one)
    monkeypatch.setattr(_dbm, "close_database", _no_db)
    assert RUN.main(["--agent", "pro"]) == 0
    assert seen["agent"] == "pro"
    assert "agent=pro" in capsys.readouterr().out


async def _empty_tick(**kw):
    """CLI 层替身：一个到点槽位都没有（不碰 Redis/DB）。"""
    return ()


def test_cli_empty_result_names_all_four_causes(monkeypatch, capsys):
    """空结果的四种成因必须逐项点名，且给出「确实要再来一遍」的入口。

    只写「当前无到点槽位」时，**已被认领**（= 今天跑过/正在跑）会被读成「没到点」，
    运营于是去核时刻表——真因在 Redis 键上。这一句是给凌晨排障的人看的。
    """
    import backend.shared.database_manager_v2 as _dbm

    async def _no_db():
        pass

    monkeypatch.setattr(RUN, "round_tick", _empty_tick)
    monkeypatch.setattr(_dbm, "close_database", _no_db)  # 单测不建连接池
    assert RUN.main([]) == 1
    err = capsys.readouterr().err
    for cause in ("没到点", "非交易日", "交易日历读不到", "已被认领"):
        assert cause in err, f"空结果提示漏了成因：{cause}"
    assert "--force" in err and "--slot" in err  # 出口也得写出来


@pytest.mark.parametrize(
    ("status", "expected_rc"),
    [
        (R.STATUS_OK, 0),
        # 「按设计跳过」（补跑槽当天已有该 schema 的决策）**不是失败**：退出码 1 会让
        # 依赖它的封装（脚本、控制台按钮、cron 包装）去查一个不存在的问题。
        (STATUS_SKIPPED, 0),
        (R.STATUS_ABORTED, 1),
        (R.STATUS_ERROR, 1),
    ],
)
def test_cli_prints_one_line_per_round_and_exit_code_follows_ok(
    monkeypatch, capsys, status, expected_rc
):
    """每轮一行（运维 grep 的对象）+ 退出码 = 全 ok（或按设计跳过）才是 0。

    退出码是**监控唯一读得懂的东西**：一轮 abort（真钱路径的 fail-closed 出口）
    必须以非零码结束，否则定时拉起它的编排器会认为「今天这轮跑过了，很好」。
    """
    import backend.shared.database_manager_v2 as _dbm

    async def _no_db():
        pass

    async def _one_round(**kw):
        return (
            RoundResult(
                status=status,
                day=DAY,
                slot=SLOT_0935,
                round_id="rnd-20260924-0935",
                decisions=2,
                legs=3,
                submitted=3,
                watch_armed=1,
                audit_rows=2,
                note="人工触发",
            ),
        )

    monkeypatch.setattr(RUN, "round_tick", _one_round)
    monkeypatch.setattr(_dbm, "close_database", _no_db)
    assert RUN.main(["--force"]) == expected_rc
    out = capsys.readouterr().out
    assert out.count("rnd-20260924-0935") == 1
    for field in ("decisions=2", "legs=3", "submitted=3", "watch=1", "audit=2"):
        assert field in out
    assert "人工触发" in out  # note 是最容易在打印时被丢掉的一项


# ══ I. 生产接线（io 层：验拼装与口径，不碰真库/真 Redis） ═══════════════
class _FakeResult:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeSession:
    def __init__(self, row):
        self._row = row
        self.params = None

    async def execute(self, stmt, params=None):
        self.params = params
        return _FakeResult(self._row)


class _SessionCM:
    def __init__(self, row):
        self._row = row

    async def __aenter__(self):
        self.session = _FakeSession(self._row)
        return self.session

    async def __aexit__(self, *exc):
        return False


def _patch_snapshot(monkeypatch, row):
    """账户快照的单行替身：把 ``get_session`` 与选源换掉，SQL 仍是真件。

    替身签名必须收 ``strict=`` —— ``load_account_numbers`` 要的是「读不到就不许
    回退 env 默认」的那个严格口径（不回退是它的全部意义，见那里的 docstring）。
    """
    import backend.shared.database_manager_v2 as dbm
    import backend.shared.real_positions as rp

    cm = _SessionCM(row)
    monkeypatch.setattr(dbm, "get_session", lambda **kw: cm)
    monkeypatch.setattr(rp, "active_broker_type", lambda **kw: "qmt")
    monkeypatch.setattr(rp, "snapshot_source_for_broker", lambda b: "tdx_bridge")
    return cm


@pytest.mark.asyncio
async def test_load_account_numbers_reads_one_row_and_derives_the_three(monkeypatch):
    from datetime import timezone as _tz

    snap_at = datetime.now(_tz.utc)
    cm = _patch_snapshot(
        monkeypatch, (50000.0, 170000.0, 120000.0, snap_at, "tdx_bridge")
    )
    account = await IO.load_account_numbers("default", "10000001")

    assert account.ok is True and account.source == "tdx_bridge"
    assert (account.cash, account.market_value, account.total_asset) == (
        50000.0,
        120000.0,
        170000.0,
    )
    # 额度三数同源同刻：剩余额度 ≡ cash，逐元一致
    assert account.quota_total == 170000.0 and account.quota_used == 120000.0
    assert account.quota_total - account.quota_used == account.cash
    assert account.age_min is not None and account.age_min < 1.0
    assert cm.session.params["s"] == "tdx_bridge"  # 选源进了 SQL（两座真账户不许混读）
    # 用的是哪家券商要能一路查到审计：源与券商是两个维度（映射表可改，映射错时
    # 光看 source 分不清「运维选的就是它」还是「读错了回退成 env 默认」）。
    assert account.broker == "qmt"


@pytest.mark.asyncio
async def test_load_account_numbers_fails_closed_when_the_broker_read_fails(
    monkeypatch,
):
    """券商选择**读不到** ⇒ 本轮不做，**不许**回退 ``REAL_BROKER_TYPE``。

    回退的语义是「没人显式选过，用部署默认」；读失败的语义是「不知道有没有人选过、
    选的是谁」。两座真实账户（tdx 8 只 / qmt 50 只，实测差 ~25 倍）之间掷硬币决定
    本轮额度，比不跑危险得多——本轮**一行都不许读库**。
    """
    import backend.shared.database_manager_v2 as dbm
    import backend.shared.real_positions as rp

    def boom(**kw):
        raise rp.BrokerSelectionUnreadable("RuntimeError: 交易库 Redis 不可用")

    monkeypatch.setattr(rp, "active_broker_type", boom)

    def _no_db(**kw):  # 券商未定时还去读库 = 这条用例要钉的那件事发生了
        raise AssertionError("券商选择读不到时不该读账户表")

    monkeypatch.setattr(dbm, "get_session", _no_db)
    account = await IO.load_account_numbers("default", "10000001")
    assert account.ok is False
    reasons = "".join(account.errors)
    assert "券商选择读取失败" in reasons and "本轮不做" in reasons
    assert "交易库 Redis 不可用" in reasons  # 原异常留下来（排障要看根因）


@pytest.mark.asyncio
async def test_load_account_numbers_fails_closed_on_missing_columns(monkeypatch):

    _patch_snapshot(monkeypatch, (None, 170000.0, None, None, "tdx_bridge"))
    account = await IO.load_account_numbers("default", "10000001")
    assert account.ok is False  # 拿编出来的数字当可用资金比不跑更危险
    assert any("cash" in e for e in account.errors)
    assert any("market_value" in e for e in account.errors)


@pytest.mark.asyncio
async def test_load_account_numbers_reports_missing_row_and_read_failure(monkeypatch):

    _patch_snapshot(monkeypatch, None)
    none_row = await IO.load_account_numbers("default", "10000001")
    assert none_row.ok is False and "不存在" in "".join(none_row.errors)

    import backend.shared.database_manager_v2 as dbm

    def boom(**kw):
        raise RuntimeError("库连不上")

    monkeypatch.setattr(dbm, "get_session", boom)
    failed = await IO.load_account_numbers("default", "10000001")
    assert failed.ok is False and "读取失败" in "".join(failed.errors)


@pytest.mark.asyncio
async def test_load_account_numbers_refuses_an_empty_account_identity():

    account = await IO.load_account_numbers("default", "  ")
    assert account.ok is False and "账户身份为空" in "".join(account.errors)


@pytest.mark.asyncio
async def test_load_account_numbers_refuses_when_the_broker_maps_to_no_source(
    monkeypatch,
):
    """券商类型没映射到快照源 ⇒ **不读库**。

    此时若照旧让 ``source`` 缺席，SQL 会取「全源最新一行」——同一 ``(tenant, user)``
    下的 ``tdx_bridge`` 与 ``qmt_exec`` 是两座互不相交的真实账户（实测规模差 ~25 倍），
    等于在两座账户之间掷硬币决定本轮额度。「不知道是谁的账」不许读成「就取这一行」。
    """
    import backend.shared.database_manager_v2 as dbm
    import backend.shared.real_positions as rp

    monkeypatch.setattr(rp, "active_broker_type", lambda **kw: "some_broker")
    monkeypatch.setattr(rp, "snapshot_source_for_broker", lambda b: "")

    def _no_db(**kw):  # 无源还去读库 = 这条用例要钉的那件事发生了
        raise AssertionError("券商未映射到快照源时不该读账户表")

    monkeypatch.setattr(dbm, "get_session", _no_db)
    account = await IO.load_account_numbers("default", "10000001")
    assert account.ok is False
    reasons = "".join(account.errors)
    assert "未映射到快照源" in reasons and "some_broker" in reasons


def test_load_excluded_symbols_tells_missing_from_empty(monkeypatch):
    from backend.shared import exclusion_list as el

    monkeypatch.setattr(el, "load_exclusion_list", lambda: None)
    missing = IO.load_excluded_symbols()
    assert missing.present is False and missing.symbols == frozenset()
    assert "未导入" in missing.note  # 缺席必须说出来，不许静默当没有风险股

    class _Doc:
        def symbols(self):
            return ["600036.SH", "600036.SH", "000001.SZ"]

    monkeypatch.setattr(el, "load_exclusion_list", lambda: _Doc())
    present = IO.load_excluded_symbols()
    assert present.present is True and present.symbols == frozenset(
        {"600036.SH", "000001.SZ"}
    )

    def boom():
        raise RuntimeError("名单文件坏了")

    monkeypatch.setattr(el, "load_exclusion_list", boom)
    broken = IO.load_excluded_symbols()
    assert broken.present is False and "读取失败" in broken.note

    class _BadDoc:
        def symbols(self):
            raise ValueError("名单格式不对")

    monkeypatch.setattr(el, "load_exclusion_list", lambda: _BadDoc())
    bad_doc = IO.load_excluded_symbols()
    assert bad_doc.present is False and "解析失败" in bad_doc.note


def test_default_llm_binding_keeps_model_and_caller_from_one_config(monkeypatch):
    from backend.shared import decision_llm_client as client

    monkeypatch.setattr(
        client, "resolve_config", lambda: type("C", (), {"model": "m-1"})()
    )
    monkeypatch.setattr(
        client,
        "make_caller",
        lambda **kw: (
            lambda prompt: (
                decisions_json({"action": "hold", "code": "600036.SH", "reason": "x"}),
                {"prompt_tokens": 3},
            )
        ),
    )
    binding = IO._default_llm_binding()
    assert binding.model == "m-1"
    attempt = binding.decide("提示词", SCHEMA_INTRADAY)
    assert attempt.ok and [d.code for d in attempt.decisions] == ["600036.SH"]


def test_default_llm_binding_propagates_not_configured(monkeypatch):
    from backend.shared import decision_llm_client as client

    def boom():
        raise RuntimeError("LLMNotConfigured")

    monkeypatch.setattr(client, "resolve_config", boom)
    with pytest.raises(RuntimeError, match="LLMNotConfigured"):
        IO._default_llm_binding()


def test_native_redis_client_targets_the_trade_db_with_visible_failures(monkeypatch):
    import redis as redis_lib

    seen: dict = {}

    class _R:
        def __init__(self, **kw):
            seen.update(kw)

    monkeypatch.setattr(redis_lib, "Redis", _R)
    monkeypatch.setenv("REDIS_HOST", "redis-host")
    monkeypatch.setenv("REDIS_PORT", "6390")
    monkeypatch.setenv("REDIS_DB_TRADE", "2")
    monkeypatch.delenv("REDIS_PASSWORD", raising=False)
    IO.native_redis_client()
    assert seen["host"] == "redis-host" and seen["port"] == 6390
    assert seen["db"] == 2  # 交易库
    assert seen["decode_responses"] is True  # 状态键要当字符串读
    assert "socket_timeout" in seen  # 卡死要有上限


def test_claim_slot_and_write_status_on_a_dict_client():

    native = FakeNative()
    key = slot_keys(DAY, SLOT_0935)[0]
    assert IO.claim_slot(native, key) is True
    assert native.store[key] == IO.CLAIM_AUTO  # 值即「谁认的」
    assert IO.claim_slot(native, key) is False  # 第二次认领失败 = 别人在跑
    assert IO.claim_slot(native, key, force=True) is True  # 手动重跑：覆盖写
    assert native.store[key] == IO.CLAIM_MANUAL
    # 覆盖写**不是**删键：紧接着的自动 tick 依旧被挡（否则同一槽会跑两轮）。
    assert IO.claim_slot(native, key) is False
    result = RoundResult(status=R.STATUS_OK, day=DAY, slot=SLOT_0935, round_id="rnd-x")
    IO.write_status(native, result, at=NOW)
    assert json.loads(native.store[LAST_KEY])["round_id"] == "rnd-x"
    assert any(c[0] == "ltrim" and c[1] == LOG_KEY for c in native.calls)


@pytest.mark.parametrize("force", [False, True])
def test_claim_slot_always_sets_a_ttl(force):
    """认领键**必须带 TTL**（两种模式都是）：``EX 2d`` 是「同一天内不重跑」的载体，
    键里已经带了日期——TTL 不是去重机制，而是**不让槽位键无限堆积**。写漏了不会
    有任何一条测试红，只会在半年后表现成 Redis 里几万条陈键。"""

    seen: dict = {}

    class _Spy:
        def set(self, key, value, **kw):
            seen.update(kw)
            seen["key"], seen["value"] = key, value
            return True

    key = slot_keys(DAY, SLOT_0935)[0]
    assert IO.claim_slot(_Spy(), key, force=force) is True
    assert seen["ex"] == IO.SLOT_TTL_S > 0
    assert seen["key"] == key
    assert seen["value"] == (IO.CLAIM_MANUAL if force else IO.CLAIM_AUTO)


def test_write_status_adds_a_per_agent_last_key():
    """``last``/``log`` 照旧（前端与 C07 心跳读它们），**另加**按家的 ``last:{agent}``。

    不带 agent 段的那把是「**最后完成的那一家**」——它保留下来是为了不动既有读者；
    要问「pro 那家跑得怎么样」就得有一把不会被别的家覆盖的键。
    """
    native = FakeNative()
    result = RoundResult(
        status=R.STATUS_OK, day=DAY, slot=SLOT_0935, round_id="rnd-x", agent="pro"
    )
    IO.write_status(native, result, at=NOW)
    assert json.loads(native.store[LAST_KEY])["agent"] == "pro"
    assert json.loads(native.store[f"{LAST_KEY}:pro"])["round_id"] == "rnd-x"
    assert any(
        c[0] == "ltrim" and c[1] == LOG_KEY for c in native.calls
    )  # 日志仍是共享时间线

    # 空 agent（单家/名册解析失败那家）：只有不带段的那把，不留 ``last:`` 空段键
    native2 = FakeNative()
    IO.write_status(native2, replace(result, agent=""), at=NOW)
    assert native2.store[LAST_KEY]
    assert f"{LAST_KEY}:" not in " ".join(native2.store)


def test_write_status_never_raises_on_a_broken_client():
    """状态键写失败不该影响已完成的决策（只告警）。"""

    class _Broken(FakeNative):
        def set(self, key, value, nx=False, ex=None):  # noqa: A002
            raise RuntimeError("redis 挂了")

    result = RoundResult(status=R.STATUS_OK, day=DAY, slot=SLOT_0935)
    IO.write_status(_Broken(), result, at=NOW)  # 不抛就是通过


def test_default_roster_stays_empty_until_the_roster_env_is_set(monkeypatch, tmp_path):
    """未开名册 ⇒ ``()``（**单家路径**）：不能把全局那一家也包进名册。

    包进来的后果不是「多跑一家」，而是**键形变了**：认领/done 键从
    ``…:{schema}`` 变成 ``…:{schema}:{模型名}``，升级当天已跑过的槽在 done 键上
    查无此人 ⇒ 补跑槽把出过决策的槽再跑一遍真钱（见 ``slot_keys``）。
    """
    monkeypatch.setenv("QM_RUNTIME_ENV_FILE", str(tmp_path / "missing.env"))
    for name in ("QM_DECISION_LLM_ROSTER", "QM_DECISION_LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    assert IO.default_roster() == ()

    monkeypatch.setenv(
        "QM_DECISION_LLM_ROSTER",
        '[{"model": "deepseek-v4-pro", "base_url_env": "U", "api_key_env": "K"},'
        ' {"model": "deepseek-v4-flash", "base_url_env": "U", "api_key_env": "K"}]',
    )
    monkeypatch.setenv("U", "https://example.invalid/v1")
    monkeypatch.setenv("K", "sk-x")
    runs = IO.default_roster()
    assert [r.agent for r in runs] == ["deepseek-v4-pro", "deepseek-v4-flash"]
    # 每家的绑定指向自己那套配置（顺序即执行顺序；绑定延迟到真要问模型时才建客户端）
    assert [r.load_llm().model for r in runs] == [
        "deepseek-v4-pro",
        "deepseek-v4-flash",
    ]


def test_default_roster_prefers_runtime_env_over_the_stale_process_env(
    monkeypatch, tmp_path
):
    """重启后的进程环境是 supervisor 的旧快照；runtime.env 才是管理台写入口。

    2026-10-19 实测：trade 被杀后 watchdog 重启的子进程继承 supervisor 启动时的名册
    （无 ``max_tokens``），刚在管理台保存的 16384 被静默回退成缺省 4000，下一轮决策
    又顶格截断、解析失败。文件优先是这条重启链的唯一兜底。
    """
    monkeypatch.setenv(
        "QM_DECISION_LLM_ROSTER",
        '[{"model": "old-model", "base_url_env": "U", "api_key_env": "K"}]',
    )
    monkeypatch.setenv("U", "https://example.invalid/v1")
    monkeypatch.setenv("K", "sk-x")
    env_file = tmp_path / "runtime.env"
    env_file.write_text(
        'QM_DECISION_LLM_ROSTER=[{"model": "deepseek-v4-pro", "base_url_env": "U", '
        '"api_key_env": "K", "max_tokens": 16384}]\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("QM_RUNTIME_ENV_FILE", str(env_file))

    runs = IO.default_roster()

    assert [r.agent for r in runs] == ["deepseek-v4-pro"]
    assert runs[0].load_llm().model == "deepseek-v4-pro"


def test_default_round_deps_fills_every_injection_point(monkeypatch):
    """接线少一项就会在 ``run_once`` 里以 AttributeError 现形——这里先拦下。"""
    from dataclasses import fields as _fields

    deps = IO.default_round_deps()
    assert isinstance(deps, RoundDeps)
    empty = [f.name for f in _fields(RoundDeps) if getattr(deps, f.name) is None]
    assert empty == [], f"注入点没接线：{empty}"


@pytest.mark.asyncio
async def test_default_round_deps_closures_reach_the_real_entries(monkeypatch):
    """「非 None」不等于「接对了」：三个闭包与交易日判定要真调到既有实现上。

    把 agent 传丢、给执行段漏 Redis、交易日判定用错市场——这三类接线错误在
    ``is None`` 那一层全都看不见，只有在闭包真被调用时才现形。
    """
    from backend.services.trade.services import decision_executor as EX
    from backend.services.trade_shared import deps as TD
    from backend.shared import decision_ledger_store as LS
    from backend.shared import trading_calendar as TC
    from backend.shared.decision import watch_writer as WW

    seen: dict = {}

    async def fake_run_round(*, db, redis, **kw):
        seen["exec"] = {"db": db, "redis": redis, **kw}
        return "OUT"

    async def fake_upsert(db, records):
        seen["ledger"] = (db, records)
        return 7

    def fake_write_watch(redis, plan, *, agent):
        seen["watch"] = (redis, plan, agent)
        return "W"

    class _Cal:
        async def trading_day_verdict(self, **kw):
            seen["cal"] = kw
            return True, TC.SRC_EXCHANGE_CALENDAR

    monkeypatch.setattr(EX, "run_round", fake_run_round)
    monkeypatch.setattr(TD, "get_redis", lambda: "R")
    monkeypatch.setattr(WW, "write_watch_plan", fake_write_watch)
    monkeypatch.setattr(LS, "upsert_rows", fake_upsert)
    monkeypatch.setattr(TC, "TradingCalendarService", _Cal)

    deps = IO.default_round_deps()
    assert await deps.run_exec(db="DB", round_id="rnd-x") == "OUT"
    assert seen["exec"]["redis"] == "R"  # 执行段拿到的是同一个 Redis 客户端
    assert seen["exec"]["round_id"] == "rnd-x"
    assert deps.write_watch("fake-model", "PLAN") == "W"
    assert seen["watch"] == ("R", "PLAN", "fake-model")  # agent 不能丢（决定规则归属）
    assert await deps.write_ledger("DB", ["a", "b"]) == 7
    assert seen["ledger"] == ("DB", ["a", "b"])
    assert await deps.is_trading_day(DAY) is True
    assert seen["cal"]["market"] == "CN" and seen["cal"]["trade_date"] == DAY
    assert seen["cal"]["user_id"]  # 日历按账户维度取（空身份会让日历查不到假期）


def test_default_round_deps_regime_closure_reaches_the_real_reader(monkeypatch):
    """regime 闭包要真打到 db0 ``qm:regime:intraday``——键名/库号接错在
    ``is None`` 那层同样看不见，只有真调用才现形。"""
    import redis as redis_lib

    seen: dict = {}

    class _Redis:
        def __init__(self, **kw):
            seen["kw"] = kw

        def hgetall(self, key):
            seen["key"] = key
            return {"state": "bear", "ret": "-0.0748"}

        def close(self):
            seen["closed"] = True

    monkeypatch.setenv("REDIS_DB_GENERAL", "0")
    monkeypatch.setattr(redis_lib, "Redis", _Redis)
    deps = IO.default_round_deps()
    assert deps.load_regime() == {"state": "bear", "ret": "-0.0748"}
    assert seen["key"] == "qm:regime:intraday"
    assert seen["kw"]["db"] == 0  # 通用库（engine 写侧同一库）
    assert seen["closed"]  # 客户端要关，连接不泄


def test_load_intraday_regime_fails_open_on_a_dead_redis(monkeypatch):
    """Redis 挂了 → ``None``（增益块取不到不拦轮），但客户端仍要关。"""
    import redis as redis_lib

    seen: dict = {}

    class _Dead:
        def __init__(self, **kw):
            pass

        def hgetall(self, key):
            raise RuntimeError("redis 挂了")

        def close(self):
            seen["closed"] = True

    monkeypatch.setattr(redis_lib, "Redis", _Dead)
    assert IO.load_intraday_regime() is None
    assert seen["closed"]


@pytest.mark.asyncio
async def test_default_round_deps_scores_closure_reaches_the_real_reader(monkeypatch):
    """分数闭包要真打到 ``shared.signal_scores.load_score_snapshot``（唯一实现），
    user_id 传决策账户——桶隔离按它解析生效模型，丢了就读错桶。"""
    from backend.shared import signal_scores as SS

    seen: dict = {}

    async def fake_load(tenant, *, user_id=None, model_id=None):
        seen["args"] = (tenant, user_id, model_id)
        return {"SH600036": {"rank_pct": 0.9}}, {
            "ok": True,
            "signal_date": "2026-09-24",
        }

    monkeypatch.setattr(SS, "load_score_snapshot", fake_load)
    deps = IO.default_round_deps()
    score_map, meta = await deps.load_scores("t1", "u1")
    assert score_map == {"SH600036": {"rank_pct": 0.9}}
    assert meta["ok"] is True
    assert seen["args"] == ("t1", "u1", None)


@pytest.mark.asyncio
async def test_default_round_deps_refuse_a_degraded_trading_day_verdict(monkeypatch):
    """**降级判定必须被拒绝**：日历取不到时服务只按周末判断，节假日会被判成交易日。

    实测坐标：2026-09-25 是中秋（周五）、2026-10-01 是国庆（周四）——两个工作日。
    降级时它俩都被判成交易日，真钱轮次于是照常开盘下单。所以依据是 weekday_fallback
    时这里要抛出去，由编排层走既有「日历不可用 → 本 tick 不跑」分支（宁缺勿滥）。
    """
    from backend.shared import trading_calendar as TC

    class _Degraded:
        async def trading_day_verdict(self, **kw):
            return True, TC.SRC_WEEKDAY_FALLBACK

    monkeypatch.setattr(TC, "TradingCalendarService", _Degraded)
    deps = IO.default_round_deps()
    with pytest.raises(RuntimeError) as ei:
        await deps.is_trading_day(DAY)
    assert TC.SRC_WEEKDAY_FALLBACK in str(ei.value), ei.value


# ══ H. 分层与体量的源码守卫 ══════════════════════════════════════════
def test_modules_stay_within_the_file_budget_and_layering():
    """九块分工：core 无 IO、io 无编排、round 只编排、tick 只调度、runner 只驱动、
    blocks/feeds 只装配与取数（补块：Stage 2 第一/第二批）、wake 只判据（纯逻辑）、
    notes 只取数（执行段 summary 形状归一 + 摘要 ▸ 要点行）。

    每块 < 800 行——``round`` 曾在一个文件里同时装「一轮里发生了什么」与「哪些槽位
    该跑」（900 行），拆出 ``tick`` 才回到预算内；``round``/``io`` 后来都顶到预算
    顶，「分数/regime → 提示词补块」的装配于是拆出 ``blocks``，第二批的取数面拆出
    ``feeds``（都与 io 同层：可被 round import，彼此与上层互不反向 import）；唤醒
    判据（丙1）是纯函数，单独成 ``wake`` 供 tick 编排层 import；摘要要点与
    ``exec_summary`` 是读侧纯函数（2026-10-15），拆出 ``notes``（叶子：只标准库）。
    """
    from pathlib import Path

    base = Path(__file__).resolve().parents[1] / "services/trade/services"
    src = {
        name: (
            (base / "decision_round.py")
            if name == "round"
            else (base / f"decision_round_{name}.py")
        ).read_text(encoding="utf-8")
        for name in (
            "core",
            "ledger",
            "io",
            "blocks",
            "round",
            "tick",
            "runner",
            "alerts",
            "wake",
            "feeds",
            "notes",
        )
    }
    for name, text in src.items():
        assert len(text.splitlines()) < 800, f"decision_round_{name} 超出单文件上限"
    # alerts 是**叶子**：只认 core 的常量与 RoundResult。它会 import 谁都写在下面
    # 那张反向依赖表里——可见性这层不许反过来碰执行/取数，否则「推不动就不推」
    # 这条兜底会被它自己拉起的重依赖破坏。
    assert "decision_round_alerts" not in src["core"]
    # core 不许碰 IO（否则「纯函数可单测」这条就没了）
    for banned in (
        "import redis",
        "database_manager",
        "sqlalchemy",
        "requests",
        "httpx",
    ):
        assert banned not in src["core"], f"core 里出现了 IO 依赖：{banned}"
    # 认领/状态键走原生客户端（包装客户端会把异常吞成 None，见模块 docstring）：
    # 这一层在 tick 而不是 round——拆层时最容易跟着 round_tick 一起搬错地方。
    assert "RedisClient(" not in src["tick"] and "import redis" not in src["tick"]
    assert "native_redis_client" in src["tick"] and "claim_slot" in src["tick"]
    # 编排层不许直接拿 Redis 键（它只回答「一轮里发生了什么」）
    assert (
        "claim_slot" not in src["round"] and "native_redis_client" not in src["round"]
    )
    # 驱动层**不许出现下单/取数**：这里出事只能是「不跑」，不能是「乱下」。
    # 断言的是**执行与取数模块名**（不是 "submit" 这种会撞上日志占位符的词）。
    for banned in (
        "decision_executor",
        "build_llm_decision_client_order_id",
        "real_positions",
        "make_sync_client",
        "get_session",  # 驱动层不读库：CLI 只允许 import close_database 收连接池
    ):
        assert banned not in src["runner"], f"runner 里出现了执行/取数代码：{banned}"
    # 依赖方向单向：runner → tick → round → io → core。反向 import 会成环或成
    # 「谁都能抓谁」的泥球——逐层钉死它不许 import 上层的模块路径。
    forbidden = {
        "core": (
            "decision_round_io import",
            "services.trade.services.decision_round import",
            "decision_round_tick import",
            "decision_round_runner import",
        ),
        # 账本 DTO 是**叶子**（只 import shared）：反向拉任何编排/取数层都会成环。
        "ledger": (
            "decision_round_core import",
            "decision_round_io import",
            "services.trade.services.decision_round import",
            "decision_round_tick import",
            "decision_round_runner import",
        ),
        "io": (
            "services.trade.services.decision_round import",
            "decision_round_tick import",
            "decision_round_runner import",
        ),
        # 补块装配层与 io 同层：``round`` 可以 import 它，它只许往下（core/shared）——
        # 反向拉编排或取数层会让「补块降级不拦轮」这条纪律失去独立位置。
        "blocks": (
            "decision_round_io import",
            "services.trade.services.decision_round import",
            "decision_round_tick import",
            "decision_round_runner import",
        ),
        "round": (
            "decision_round_tick import",
            "decision_round_runner import",
        ),
        "tick": ("decision_round_runner import",),
        # 可见性层（P4 附-②）只许 import core：它要在**跑完之后**用，任何往回拉
        # 执行/取数/调度的依赖都会让它从「最后一步的旁路」变成链条上的一环。
        "alerts": (
            "decision_round_io import",
            "services.trade.services.decision_round import",
            "decision_round_tick import",
            "decision_round_runner import",
        ),
        # 取数面（Stage 2 第二批）与 blocks 同层：io 可以 import 它，它只许往下
        # （core/wake/shared）。反向拉 io/tick 会把「取数降级不拦轮」的独立位置
        # 变成环——wake 上下文客户端的库口径（trade db2）就是靠这条保住的。
        "feeds": (
            "decision_round_io import",
            "services.trade.services.decision_round import",
            "decision_round_tick import",
            "decision_round_runner import",
        ),
        # 唤醒判据层（丙1）是**纯函数**，谁都能 import 它，它谁也不许往回拉——
        # 拉了取数（feeds）或编排（tick/round）就再也无法脱库单测。
        "wake": (
            "decision_round_io import",
            "services.trade.services.decision_round import",
            "decision_round_tick import",
            "decision_round_runner import",
            "decision_round_feeds import",
        ),
        # 摘要取数叶子（2026-10-15 从 round 拆出，压 800 行预算）：只许标准库。
        # 谁都能 import 它（round 在编排里调它），它谁也不许往回拉——碰了 IO 就
        # 不再是纯函数，「要点提炼绝不抛」这条兜底就失去可单测的立身之处。
        "notes": (
            "decision_round_io import",
            "services.trade.services.decision_round import",
            "decision_round_tick import",
            "decision_round_runner import",
            "decision_round_feeds import",
            "decision_round_blocks import",
            "decision_round_wake import",
            "decision_round_core import",
        ),
    }
    for layer, markers in forbidden.items():
        for marker in markers:
            assert marker not in src[layer], f"{layer} 反向依赖了上层：{marker}"


@pytest.mark.asyncio
async def test_llm_decide_runs_off_the_event_loop_thread():
    """to_thread 守卫（2026-10-09 14:45 trade 冻结实证）。

    ``binding.decide`` 是同步阻塞 httpx（单次最长 ~120s，见 decision_llm_client
    模块契约）。在 trade 的共享事件循环里直呼会冻结全体任务（含 ``/health``），
    主进程监督者 3×30s 健康检查判死重启——认领键残留后该槽位后续 tick 永久
    「已被认领（跳过）」（当日 1445 pro 轮因此整轮丢失）。必须在工作线程执行。
    """
    import threading

    threads: list[int] = []
    text = decisions_json({"action": "hold", "code": "600036.SH", "reason": "观望"})

    def _decide(prompt, schema):
        threads.append(threading.get_ident())
        return attempt_from(text, schema=schema)

    h = make_harness(load_llm=lambda: LLMBinding(model="fake-model", decide=_decide))
    result = await run_once(SLOT_0935, deps=h.deps, now=NOW)

    assert result.status == R.STATUS_OK
    assert threads, "LLM 未被调用"
    assert threads[0] != threading.get_ident(), (
        "decide 跑在事件循环主线程——to_thread 下放缺失，阻塞即冻结 trade"
    )


# ---------------------------------------------------------------------------
# 分账裁剪：账本管归属、桥管可卖量
# ---------------------------------------------------------------------------


def _holding_row(code: str, volume: int, avail: int) -> HoldingRow:
    return HoldingRow(
        code=code,
        name="测试",
        volume=volume,
        cost=10.0,
        price=10.0,
        pnl_pct=0.0,
        day_chg=0.0,
        avail=avail,
    )


def test_clip_holds_only_codes_the_agent_owns() -> None:
    """桥账户有、账本没有的票**不可见**：模型的提示词与执行段都不该看到别人的仓。"""
    rows = (
        _holding_row("601985.SH", 2100, 2100),
        _holding_row("600036.SH", 1200, 1200),
    )
    visible, hidden = clip_holdings_to_ledger(
        rows, {"601985.SH": {"volume": 800, "cost_price": 9.0}}
    )
    assert hidden == 1
    assert [r.code for r in visible] == ["601985.SH"]


def test_clip_caps_avail_to_the_agents_own_shares() -> None:
    """同一只票三家共持（500/800/800，桥 avail=2100）：每家只见自己的可卖量。

    不设上限，三家各按 2100 报卖 → 合计三倍超卖，桥侧后两家只能拒单。
    """
    rows = (_holding_row("601985.SH", 2100, 2100),)
    for own in (500, 800, 800):
        (row,), hidden = clip_holdings_to_ledger(
            rows, {"601985.SH": {"volume": own, "cost_price": 9.0}}
        )
        assert hidden == 0
        assert row.avail == own


def test_clip_keeps_bridge_t1_lock() -> None:
    """桥的 avail 更小 = 今日 T+1 锁定，账本持有量再大也只能按桥可卖量出。"""
    rows = (_holding_row("600036.SH", 5000, 300),)
    (row,), _ = clip_holdings_to_ledger(rows, {"600036.SH": {"volume": 5000}})
    assert row.volume == 5000 and row.avail == 300


def test_clip_hides_zero_or_broken_ledger_rows() -> None:
    """账本量为 0/脏值不能当成「持有」：否则会生成一张无中生有的卖单。"""
    rows = (_holding_row("600036.SH", 1200, 1200), _holding_row("601857.SH", 700, 700))
    visible, hidden = clip_holdings_to_ledger(
        rows, {"600036.SH": {"volume": 0}, "601857.SH": "oops"}
    )
    assert visible == () and hidden == 2


def test_clip_empty_ledger_hides_everything() -> None:
    """账本为空 ≠ 看到全账户：宁可不给模型看，也不能把别人的仓算成本 agent 的。"""
    rows = (_holding_row("600036.SH", 1200, 1200),)
    assert clip_holdings_to_ledger(rows, {}) == ((), 1)
