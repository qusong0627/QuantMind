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
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime

import pytest

from backend.shared.decision.context import DirectionBlock, PoolRow
from backend.shared.decision.contract import SCHEMA_INTRADAY, SCHEMA_REBALANCE
from backend.shared.decision.llm_call import decide_with_retry
from backend.shared.decision.watch_writer import WatchWriteResult
from backend.shared.decision_context_source import PoolDoc
from backend.services.trade.services import decision_round as R
from backend.services.trade.services import decision_round_io as IO
from backend.services.trade.services import decision_round_runner as RUN
from backend.services.trade.services import decision_round_tick as TICK
from backend.services.trade.services.decision_round import run_once
from backend.services.trade.services.decision_round_tick import round_tick
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
    ExclusionRead,
    LLMBinding,
    RoundDeps,
    RoundResult,
    RoundSlot,
    as_float,
    build_pool_stamps,
    due_slots,
    gate_row_to_pool_row,
    merge_outcomes,
    pool_row_to_gate_row,
    position_source_meta,
    positions_consistency_issue,
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
#: 守护规则该不该写按 schema 分（见 ``_maybe_write_watch``）。
SLOT_1000 = next(s for s in SLOTS if s.hhmm == "1000")
SLOT_1005 = next(s for s in SLOTS if s.hhmm == "1005")


# ══ 替身 ═════════════════════════════════════════════════════════════
@dataclass
class Verdict:
    """``gates.GateVerdict`` 的最小同形（位置戳只用它两个字段）。"""

    rule: str = ""
    reason: str = ""


class FakeNative:
    """原生 redis 客户端的字典替身：``set(nx=True)`` 的语义与真件一致。"""

    def __init__(
        self,
        *,
        fail_claim: bool = False,
        fail_get: bool = False,
        fail_done_set: bool = False,
        fail_delete: bool = False,
    ) -> None:
        self.store: dict[str, str] = {}
        self.calls: list[tuple] = []
        self.fail_claim = fail_claim
        self.fail_get = fail_get
        self.fail_done_set = fail_done_set
        self.fail_delete = fail_delete
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

    def lpush(self, key, value):
        self.calls.append(("lpush", key))
        return 1

    def ltrim(self, key, start, end):
        self.calls.append(("ltrim", key))
        return True

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


def attempt_from(text: str, *, schema: str = SCHEMA_INTRADAY):
    """假 caller 只吐文本，其余（重试/解析/契约）全走真件。"""
    return decide_with_retry(
        lambda _p: (text, {"prompt_tokens": 7}), "提示词", schema=schema
    )


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
        return attempt_from(text, schema=schema)

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
        real_enabled=over.pop("real_enabled", lambda: False),
        now=over.pop("now", lambda: NOW),
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
    """档位写成 15（而非 0.15）：夹取会变成空操作，一笔打满额——不许继续。"""
    h = make_harness(
        tier=type(
            "T",
            (),
            {"level": "normal", "source": "doc", "budget": {"per_stock_pct": 15}},
        )()
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
    # 额度：额度三数同源 ⇒ 剩余额度 ≡ 可用现金，逐元一致
    assert sent["quota"] == 50000.0
    assert sent["new_buys_round"] == 2  # 档位 max_new_buys 覆盖上下文默认 3
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
        "total": 170000.0,
        "used": 120000.0,
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
async def test_run_once_prompt_carries_the_real_account_numbers():
    """提示词首行必须是真账户口径：管理 ¥总资产 / 已用 ¥市值 / 剩余 ¥现金。"""
    h = make_harness()
    await run_once(SLOT_0935, deps=h.deps, now=NOW)
    prompt = h.log["llm"][0]["prompt"]
    assert "170,000" in prompt and "120,000" in prompt and "50,000" in prompt


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


# ══ F. tick：认领 → 跑 → 置键 → 状态 ════════════════════════════════
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

    monkeypatch.setenv(RUN.ENV_FLAG, "true")
    # 补丁打在 **runner** 上（它按值 import 了 round_tick）：打在编排层上不生效，
    # 这个用例会退化成「真的去连 Redis 跑一轮」。
    monkeypatch.setattr(RUN, "round_tick", fake_tick)
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

    monkeypatch.setenv(RUN.ENV_FLAG, "true")
    monkeypatch.setattr(RUN, "round_tick", no_slots)
    monkeypatch.setattr(RUN, "asyncio", _Sleep)
    monkeypatch.setattr("backend.shared.scheduler_registry.heartbeat", boom_heartbeat)
    with pytest.raises(_Stop):
        await RUN.run_decision_round_worker()  # 走到了 sleep 就是活着的


def test_cli_dry_run_lists_slots(capsys):
    assert RUN.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "到点槽位" in out and "09:35" in out and "14:45" in out


def test_cli_rejects_an_unknown_slot(capsys):
    assert RUN.main(["--slot", "9999"]) == 2
    assert "未知槽位" in capsys.readouterr().err


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


def test_write_status_never_raises_on_a_broken_client():
    """状态键写失败不该影响已完成的决策（只告警）。"""

    class _Broken(FakeNative):
        def set(self, key, value, nx=False, ex=None):  # noqa: A002
            raise RuntimeError("redis 挂了")

    result = RoundResult(status=R.STATUS_OK, day=DAY, slot=SLOT_0935)
    IO.write_status(_Broken(), result, at=NOW)  # 不抛就是通过


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
    """五块分工：core 无 IO、io 无编排、round 只编排、tick 只调度、runner 只驱动。

    每块 < 800 行——``round`` 曾在一个文件里同时装「一轮里发生了什么」与「哪些槽位
    该跑」（900 行），拆出 ``tick`` 才回到预算内。
    """
    from pathlib import Path

    base = Path(__file__).resolve().parents[1] / "services/trade/services"
    src = {
        name: (
            (base / "decision_round.py")
            if name == "round"
            else (base / f"decision_round_{name}.py")
        ).read_text(encoding="utf-8")
        for name in ("core", "io", "round", "tick", "runner")
    }
    for name, text in src.items():
        assert len(text.splitlines()) < 800, f"decision_round_{name} 超出单文件上限"
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
        "io": (
            "services.trade.services.decision_round import",
            "decision_round_tick import",
            "decision_round_runner import",
        ),
        "round": (
            "decision_round_tick import",
            "decision_round_runner import",
        ),
        "tick": ("decision_round_runner import",),
    }
    for layer, markers in forbidden.items():
        for marker in markers:
            assert marker not in src[layer], f"{layer} 反向依赖了上层：{marker}"
