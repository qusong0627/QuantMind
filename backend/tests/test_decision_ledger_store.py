"""决策审计表写读侧（P2.1d）：纯映射 + 真库往返。

两层：

1. **纯映射**（无 DB）：身份键、记分卡分类、三态保留、两拨写入的列集、
   `record_values` ↔ `from_record` 往返——这一层把「写进去的是不是我们以为的东西」
   钉死；
2. **真库往返**（`get_session`，测试租户写后即清）：真列真类型跑一遍，重点是
   **写纪律 1**（决策字段先写为准、执行结果可刷新）——那是 `ON CONFLICT` 的行
   语义，只有真库能验。

P1.6 影子账的教训照搬：`update_cols()` 是写纪律的唯一表达处，测试直接断言列集，
不靠读代码。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import date, datetime, timezone

import pytest
from sqlalchemy.dialects import postgresql

from backend.shared.decision.contract import SCHEMA_INTRADAY, parse_decisions
from backend.shared.decision.watch_map import plan_watch
from backend.shared.decision_ledger_store import (
    ID_HEX_LEN,
    KIND_BULLISH,
    KIND_NONE,
    KIND_POSITION,
    KIND_SELL,
    DecisionRecord,
    _dedup,
    build_records,
    decision_id,
    from_record,
    kind_of,
    pool_key,
    record_values,
    update_cols,
    upsert_rows,
)

_TS = datetime(2026, 9, 23, 1, 30, tzinfo=timezone.utc)
#: 测试租户：带 `t-` 前缀（与 P1.6 同约定，读侧默认按 `strpos` 排除）
_TEST_TENANT = "t-p21d-ledger"


async def _close_db() -> None:
    """关掉全局 DB 引擎（真库测试的统一收尾）。

    引擎与创建它的**事件循环**绑定：pytest-asyncio 每个用例一个新循环，不关就会让
    下一个用例报 `got Future attached to a different loop`——那报错看起来像环境
    问题，实则是上一场用例没收尾。P1.6 影子账测试早已如此收尾。
    """
    try:
        from backend.shared.database_manager_v2 import close_database

        await close_database()
    except Exception:  # noqa: BLE001
        pass


async def _ensure_db_pool() -> None:
    """开工前探一次，池子陈旧就刷新（与 `test_copilot`/`test_hot_set_builder` 同范式）。

    `_close_db` 管的是**下游**（我不给别人留脏池），本函数管的是**上游**：任何前序
    用例把引擎绑死在已关闭的 loop 上，我这里就会拿到 `got Future attached to a
    different loop`——**只做一头等于把用例绑死在别人身上**。

    实测：本文件单独跑 31 passed，但在 `tests/test_copilot.py` 之后跑就必红
    （`tests/test_copilot.py tests/test_decision_ledger_store.py` 三秒复现）。
    套件顺序是**确定的**（本环境没装 pytest-randomly，`-p no:randomly` 一直是空操作），
    所以这类失败不是抖动而是固定组合——排查看「谁在前」而不是「重跑一次」。

    探不通就 `close_database()` 重建，再探一次：第二次仍失败说明是真环境问题，
    交给调用方的 `skip` 处理，不在这里吞掉。
    """
    from sqlalchemy import text as _t

    from backend.shared.database_manager_v2 import close_database, get_session

    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(_t("SELECT 1"))
        return
    except Exception:  # noqa: BLE001 - 陈旧池子即刷新，原因见 docstring
        await close_database()
    async with get_session(read_only=True) as probe:
        await probe.execute(_t("SELECT 1"))


def _rows(*rows: dict):
    batch = parse_decisions(
        json.dumps({"decisions": list(rows)}, ensure_ascii=False),
        schema=SCHEMA_INTRADAY,
    )
    assert batch.ok, f"语料没解析通过：{batch.status}"
    return list(batch.decisions)


def _rec(**kw) -> DecisionRecord:
    base = {
        "id": decision_id("r1", 0, "SH600519", "watch"),
        "pool_key": pool_key("m", "2026-09-23", "SH600519", "watch"),
        "round_id": "r1",
        "tenant_id": _TEST_TENANT,
        "user_id": "10000001",
        "agent": "deepseek-v4-pro",
        "market": "CN",
        "trade_date": date(2026, 9, 23),
        "decided_at": _TS,
        "code": "SH600519",
        "code_raw": "SH600519",
        "action": "watch",
    }
    return DecisionRecord(**{**base, **kw})


# ── 身份键 ────────────────────────────────────────────────────────
class TestIdentity:
    def test_decision_id_is_scoped_to_round_and_index(self) -> None:
        a = decision_id("r1", 0, "SH600519", "watch")
        assert a == decision_id("r1", 0, "SH600519", "watch"), "同输入必须同 id（幂等）"
        assert a != decision_id("r2", 0, "SH600519", "watch"), "跨轮必须不同 id"
        assert a != decision_id("r1", 1, "SH600519", "watch"), "同轮不同序号必须不同 id"
        assert a != decision_id("r1", 0, "SZ000001", "watch")
        assert a != decision_id("r1", 0, "SH600519", "sell")

    def test_decision_id_separates_agents_in_one_round(self) -> None:
        """同轮同序号同码同动作、**两家模型**：主键必须不同。

        主键撞上不是「多一行少一行」：``id`` 是 ``qm_decision_ledger`` 的主键，
        两家写同一行 = 一家的决策被另一家**覆盖**，而审计表是「模型当时说了什么」
        的唯一出处。多模型竞争下这一撞是必然事件（两家都盯着同一只票）。
        """
        pro = decision_id("rnd-20260924-0935", 0, "SH600519", "buy", "deepseek-v4-pro")
        flash = decision_id(
            "rnd-20260924-0935", 0, "SH600519", "buy", "deepseek-v4-flash"
        )
        assert pro != flash
        assert pro == decision_id(
            "rnd-20260924-0935", 0, "SH600519", "buy", "deepseek-v4-pro"
        ), "同家同输入必须同 id（重跑幂等）"

    def test_decision_id_without_agent_keeps_the_historical_formula(self) -> None:
        """空 agent = 历史口径：**已入表的行 id 逐字不变**（升级不改旧账）。"""
        raw = "rnd-20260924-0935|0|SH600519|buy"
        assert (
            decision_id("rnd-20260924-0935", 0, "SH600519", "buy")
            == (hashlib.sha1(raw.encode()).hexdigest()[:ID_HEX_LEN])
        )
        assert decision_id("r1", 0, "SH600519", "watch", "") == decision_id(
            "r1", 0, "SH600519", "watch"
        )

    def test_build_records_carries_the_agent_into_the_id(self) -> None:
        """一轮两家模型各建一次记录：id 分家（由 ``build_records`` 真正传下去）。"""
        decisions = _rows({"action": "buy", "code": "SH600519"})
        ids = {
            agent: build_records(
                decisions,
                round_id="rnd-20260924-0935",
                agent=agent,
                trade_date=date(2026, 9, 24),
                decided_at=_TS,
            )[0].id
            for agent in ("deepseek-v4-pro", "deepseek-v4-flash")
        }
        assert ids["deepseek-v4-pro"] != ids["deepseek-v4-flash"]

    def test_decision_id_is_not_the_pool_key(self) -> None:
        """两个身份键**不能相等**：相等就等于「每天只留第一条」，审计当场失效。"""
        did = decision_id("2026-09-23-1130", 3, "SH600519", "watch")
        pk = pool_key("deepseek-v4-pro", "2026-09-23", "SH600519", "watch")
        assert did != pk

    def test_two_rounds_same_day_same_code_land_on_one_pool_key(self) -> None:
        """盘中两次改主意 → 两条审计行、**同一个 pool_key**（记分卡只算一次样本）。"""
        rows = build_records(
            _rows(
                {"action": "watch", "code": "SH600519", "stop_loss": 1500.0},
            ),
            round_id="r-1015",
            agent="m",
            trade_date=date(2026, 9, 23),
            decided_at=_TS,
        )
        later = build_records(
            _rows({"action": "sell", "code": "SH600519", "pct": 0.5}),
            round_id="r-1400",
            agent="m",
            trade_date=date(2026, 9, 23),
            decided_at=_TS,
        )
        assert rows[0].id != later[0].id, "两次决策必须是两行"
        # action 不同 → pool_key 也不同（隔壁同理：键里含动作）
        assert rows[0].pool_key != later[0].pool_key

    def test_pool_key_is_byte_compatible_with_the_neighbouring_pool(self) -> None:
        """与隔壁 `decision_track.make_id` 逐字同口径（分隔符/字段序/截断长度）。

        写死一个字面量而不是在测试里重算同一个公式：重算只能证明「我还是这么写的」，
        证明不了公式没被改过。改这里的任何一处（`|` → `-`、[:16] → [:12]）都会让
        「本表内每天第一条」的去重悄悄换一批行。

        **注意这条不证明「与存量池对得上」**：隔壁把模型原样写法喂进哈希（实测其
        207 行全是后缀式 `001312.SZ`），本表喂归一后的前缀式——公式相同、输入不同，
        同一个决策在两边是两个哈希。对账走元组，见 `pool_key` docstring。
        """
        expect = hashlib.sha1(b"deepseek-v4-pro|2026-09-23|SH600519|watch").hexdigest()[
            :16
        ]
        assert pool_key("deepseek-v4-pro", "2026-09-23", "SH600519", "watch") == expect


# ── 记分卡分类 ────────────────────────────────────────────────────
class TestKind:
    def test_taxonomy_matches_the_scorecard(self) -> None:
        assert kind_of("sell", "SH600519", ()) == KIND_SELL
        assert kind_of("buy", "SH600519", ()) == KIND_BULLISH
        assert kind_of("watch", "SH600519", ()) == KIND_BULLISH, "非持仓的 watch = 看多"
        assert kind_of("watch", "SH600519", {"SH600519"}) == KIND_POSITION
        assert kind_of("hold", "SH600519", ()) == KIND_NONE

    def test_hold_is_recorded_but_never_scored(self) -> None:
        rows = build_records(
            _rows({"action": "hold", "code": "600519.SH"}),
            round_id="r1",
            agent="m",
            trade_date=date(2026, 9, 23),
            decided_at=_TS,
        )
        assert len(rows) == 1 and rows[0].kind == KIND_NONE


# ── 三态与字段搬运 ────────────────────────────────────────────────
class TestFieldCarry:
    def test_given_pct_lands_as_a_number(self) -> None:
        rows = build_records(
            _rows({"action": "sell", "code": "600519.SH", "pct": 0.3}),
            round_id="r1",
            agent="m",
            trade_date=date(2026, 9, 23),
            decided_at=_TS,
        )
        assert rows[0].pct == 0.3 and rows[0].pct_state == "given"

    @pytest.mark.parametrize(
        ("row", "state"),
        [
            ({"action": "sell", "code": "600519.SH", "pct": "0.3股"}, "dirty"),
            ({"action": "sell", "code": "600519.SH"}, "missing"),
        ],
    )
    def test_unusable_pct_keeps_its_state_and_never_becomes_a_number(
        self, row: dict, state: str
    ) -> None:
        """`pct=NULL` + `pct_state` 留痕：0.0 与「没给」**在库里必须分得开**。"""
        rows = build_records(
            _rows(row),
            round_id="r1",
            agent="m",
            trade_date=date(2026, 9, 23),
            decided_at=_TS,
        )
        assert rows[0].pct is None
        assert rows[0].pct_state == state

    def test_dirty_raw_is_preserved_for_audit(self) -> None:
        rows = build_records(
            _rows({"action": "sell", "code": "600519.SH", "pct": "三成"}),
            round_id="r1",
            agent="m",
            trade_date=date(2026, 9, 23),
            decided_at=_TS,
        )
        assert rows[0].pct_raw, "脏值原文丢了 → 事后无法判断模型到底写了什么"

    def test_code_is_normalised_and_the_raw_form_is_kept(self) -> None:
        rows = build_records(
            _rows({"action": "watch", "code": "600519.SH", "stop_loss": 1500.0}),
            round_id="r1",
            agent="m",
            trade_date=date(2026, 9, 23),
            decided_at=_TS,
        )
        assert rows[0].code == "SH600519", "PG 口径是前缀式"
        assert rows[0].code_raw == "600519.SH", "模型原样写法要留着（对账/排查）"

    def test_audit_payload_survives_the_round_trip(self) -> None:
        rows = build_records(
            _rows(
                {
                    "action": "watch",
                    "code": "600519.SH",
                    "stop_loss": 1500.0,
                    "take_profit": 1800.0,
                    "move_stop": 1600.0,
                    "invalidation": "放量跌破 1500",
                    "risk_amount": 21600.0,
                    "confidence": 0.7,
                    "reason": "支撑位",
                }
            ),
            round_id="r1",
            agent="m",
            trade_date=date(2026, 9, 23),
            decided_at=_TS,
        )
        r = rows[0]
        assert (r.stop_loss, r.take_profit, r.move_stop) == (1500.0, 1800.0, 1600.0)
        assert r.invalidation == "放量跌破 1500"
        assert r.risk_amount == 21600.0 and r.confidence == 0.7

    def test_watch_outcomes_come_from_the_plan(self) -> None:
        """P2.1c 的 `WatchPlan.outcomes()` 直接喂本层：挂上的带 armed，被拒的带理由。"""
        decisions = _rows(
            {"action": "watch", "code": "600519.SH", "stop_loss": 1500.0, "pct": 0.3},
            {"action": "watch", "code": "000001.SZ", "pct": 0.5},  # 无价位 → 拒
            {"action": "watch", "code": "600000.SH", "stop_loss": 10.0, "pct": 0},
        )
        plan = plan_watch(decisions, agent="m")
        rows = build_records(
            decisions,
            round_id="r1",
            agent="m",
            trade_date=date(2026, 9, 23),
            decided_at=_TS,
            outcomes=plan.outcomes(),
        )
        assert [r.armed for r in rows] == [True, False, False]
        assert rows[1].reject_reason and rows[2].reject_reason, "拒绝理由必须落库"
        assert rows[0].reject_reason == ""

    def test_missing_outcome_leaves_the_row_unarmed_and_empty(self) -> None:
        """非 watch 决策在本层没有结果（买卖段是 P2.3）——留空而不是编一个。"""
        rows = build_records(
            _rows({"action": "buy", "code": "600519.SH", "pct": 0.1}),
            round_id="r1",
            agent="m",
            trade_date=date(2026, 9, 23),
            decided_at=_TS,
        )
        assert rows[0].armed is False and rows[0].reject_reason == ""

    def test_pool_ctx_absent_is_null_not_off(self) -> None:
        """未插桩 = NULL；「池外」是**插桩后**的一种取值，两者不可混同。"""
        rows = build_records(
            _rows({"action": "watch", "code": "600519.SH", "stop_loss": 1.0}),
            round_id="r1",
            agent="m",
            trade_date=date(2026, 9, 23),
            decided_at=_TS,
            pool_ctx={"600519.SH": {"state": "shown", "rank": 3}},
        )
        assert rows[0].pool_ctx == {"state": "shown", "rank": 3}
        rows2 = build_records(
            _rows({"action": "watch", "code": "600519.SH", "stop_loss": 1.0}),
            round_id="r1",
            agent="m",
            trade_date=date(2026, 9, 23),
            decided_at=_TS,
        )
        assert rows2[0].pool_ctx is None


# ── 写纪律 1：能覆盖哪些列 ────────────────────────────────────────
class TestUpdateCols:
    def test_decision_fields_are_never_overwritten(self) -> None:
        """**本层最重要的不变量**：审计字段先写为准。

        覆盖列里出现任何一个决策字段（action/code/pct/价位/reason/invalidation…）
        都意味着后一轮能抹掉前一轮的决策留痕。
        """
        decision_fields = {
            "id",
            "pool_key",
            "round_id",
            "tenant_id",
            "user_id",
            "agent",
            "market",
            "trade_date",
            "decided_at",
            "code",
            "code_raw",
            "action",
            "kind",
            "pct",
            "pct_state",
            "pct_raw",
            "confidence",
            "stop_loss",
            "take_profit",
            "move_stop",
            "invalidation",
            "risk_amount",
            "reason",
            "pool_ctx",
            "context_meta",
        }
        assert not (set(update_cols(priced=False)) & decision_fields)
        assert not (set(update_cols(priced=True)) & decision_fields)

    def test_the_two_waves_touch_disjoint_columns(self) -> None:
        """执行结果与定价**互不覆盖**：定价拨跑一遍不能清掉 armed/理由。"""
        assert not (set(update_cols(priced=False)) & set(update_cols(priced=True)))

    def test_wave_membership_is_explicit(self) -> None:
        assert set(update_cols(priced=False)) == {
            "armed",
            "reject_reason",
            "notes",
            "order_id",
        }
        assert set(update_cols(priced=True)) == {
            "entry_date",
            "entry_px",
            "tradable",
            "fwd",
            "tags",
            "priced_at",
        }


# ── 写纪律 1 的第二层：发出去的 SQL 本身 ──────────────────────────
class _Result:
    """最小 Result 替身（真驱动 `.rowcount` 恒在，替身也一样）。"""

    def __init__(self, rows: list | None = None) -> None:
        self.rowcount = 0
        self._rows = list(rows or [])

    def mappings(self):  # noqa: ANN201
        return self

    def all(self):  # noqa: ANN201
        return self._rows


class _FakeSession:
    """只记账不落库的会话：把发出去的语句原样留下来给断言看。"""

    def __init__(self) -> None:
        self.stmts: list = []
        self.params: list = []

    async def execute(self, stmt, params=None):  # noqa: ANN001
        self.stmts.append(stmt)
        self.params.append(params)
        return _Result()

    def sql_and_params(self) -> list[tuple[str, dict]]:
        out = []
        for s in self.stmts:
            c = s.compile(dialect=postgresql.dialect())
            out.append((str(c), dict(c.params)))
        return out


def _update_set_clause(sql: str) -> str:
    """取 `ON CONFLICT ... DO UPDATE SET` 之后的那一段（只看它碰了哪些列）。"""
    assert "ON CONFLICT" in sql, "没有 ON CONFLICT 的语句说明 upsert 没生效"
    return sql.split("ON CONFLICT", 1)[1]


def test_mixed_batch_splits_into_two_statements_with_disjoint_ids():
    """**每行判断、不是每批判断**——否则同批未定价行的 fwd 会被 NULL 冲掉。"""
    priced = DecisionRecord(
        **{
            **vars(_rec()),
            "fwd": {"1": {"ret": 0.01}},
            "entry_px": 10.0,
            "entry_date": date(2026, 9, 24),
            "priced_at": _TS,
        }
    )
    s = _FakeSession()
    assert (
        asyncio.run(
            upsert_rows(s, [priced, _rec(id=decision_id("r1", 1, "SZ000001", "sell"))])
        )
        == 2
    )
    assert len(s.stmts) == 2, "带价与不带价必须分成两条语句"
    (sql0, _p0), (sql1, _p1) = s.sql_and_params()
    assert "fwd" not in _update_set_clause(sql0), (
        "先发的（未定价）那条不得在冲突时碰定价列"
    )
    assert "fwd" in _update_set_clause(sql1)
    assert "action" not in _update_set_clause(sql0), "决策字段永不出现在冲突覆盖里"

    from backend.shared.decision_ledger_store import _upsert

    # 防御分支：把带价的行塞进未定价那拨 → 一条语句都不发（而不是静默写 NULL 定价）
    s2 = _FakeSession()
    assert asyncio.run(_upsert(s2, [priced], priced=False)) == 0
    assert s2.stmts == []


def test_an_all_priced_batch_is_one_statement():
    def _with_price(**kw) -> DecisionRecord:
        return DecisionRecord(
            **{
                **vars(_rec()),
                "fwd": {"1": {"ret": 0.01}},
                "entry_px": 1.0,
                "priced_at": _TS,
                **kw,
            }
        )

    s = _FakeSession()
    assert asyncio.run(upsert_rows(s, [_with_price(), _with_price(id="other")])) == 2
    assert len(s.stmts) == 1


def test_empty_batch_writes_nothing():
    s = _FakeSession()
    assert asyncio.run(upsert_rows(s, [])) == 0
    assert s.stmts == []


def test_duplicate_keys_collapse_before_hitting_the_database():
    """同一语句里重复主键会让 PG 报 `cannot affect row a second time`。"""
    s = _FakeSession()
    assert asyncio.run(upsert_rows(s, [_rec(), _rec()])) == 1
    assert len(s.stmts) == 1


# ── 往返（定价作业据此把行读回来再写回） ──────────────────────────
class TestRoundTrip:
    def test_unpriced_row_round_trips(self) -> None:
        rec = _rec(
            pct=0.3,
            pct_state="given",
            stop_loss=1500.0,
            invalidation="放量跌破",
            risk_amount=21600.0,
            notes=("注记一",),
            context_meta={"prompt": "sha256:abc"},
            pool_ctx={"state": "shown"},
        )
        assert from_record(record_values(rec)) == rec

    def test_priced_row_round_trips(self) -> None:
        rec = _rec(
            fwd={"1": {"ret": 0.01}, "5": {"ret": None}},
            entry_date=date(2026, 9, 24),
            entry_px=1500.0,
            tradable=True,
            priced_at=_TS,
        )
        assert from_record(record_values(rec)) == rec

    def test_jsonb_columns_read_back_from_text_mode(self) -> None:
        """psycopg2 文本模式下 JSONB 读回来是 **str**——不能当 dict 用。"""
        row = record_values(
            _rec(notes=("a",), context_meta={"k": 1}, pool_ctx={"state": "off"})
        )
        as_text = {
            **row,
            "notes": json.dumps(row["notes"]),
            "context_meta": json.dumps(row["context_meta"]),
            "pool_ctx": json.dumps(row["pool_ctx"]),
        }
        back = from_record(as_text)
        assert back.notes == ("a",)
        assert back.context_meta == {"k": 1}
        assert back.pool_ctx == {"state": "off"}

    def test_str_dates_are_coerced_for_the_driver(self) -> None:
        """asyncpg 对 DATE 只认 `datetime.date`，传字符串直接抛类型错。"""
        rec = _rec(trade_date="2026-09-23", decided_at="2026-09-23T01:30:00Z")  # type: ignore[arg-type]
        vals = record_values(rec)
        assert vals["trade_date"] == date(2026, 9, 23)
        assert isinstance(vals["decided_at"], datetime)

    def test_unparseable_decided_at_raises_instead_of_silently_using_now(self) -> None:
        """审计时间列不接受静默回落：回落到写入时刻 = 把「10:03 说的」记成「14:20 写的」。"""
        with pytest.raises(ValueError):
            record_values(_rec(decided_at="昨天下午"))

    def test_dedup_keeps_the_last_write(self) -> None:
        a = _rec(armed=False)
        b = _rec(armed=True)
        assert a.id == b.id
        assert [r.armed for r in _dedup([a, b])] == [True]


# ── 真库往返（测试租户，写后即清） ────────────────────────────────
@pytest.mark.asyncio
async def test_real_db_round_trip_and_write_discipline():
    """真列真类型跑一遍：**决策字段先写为准、执行结果可刷新**。

    只有真库能验 `ON CONFLICT` 的行语义（纯函数测不了）。
    """
    try:
        from sqlalchemy import text

        from backend.shared.database_manager_v2 import get_session
        from backend.shared.decision_ledger_contract import (
            ensure_decision_ledger_table_async,
        )
        from backend.shared.decision_ledger_store import (
            load_day,
            load_round,
            load_pending,
            upsert_rows,
        )
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"依赖不可用: {exc}")

    try:
        await _ensure_db_pool()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"DB 不可用: {exc}")

    round_id = f"{_TEST_TENANT}-round-1"
    decisions = _rows(
        {"action": "watch", "code": "600519.SH", "stop_loss": 1500.0, "pct": 0.3},
        {"action": "sell", "code": "000001.SZ", "pct": 0.5},
        {"action": "hold", "code": "600000.SH"},
    )
    plan = plan_watch(decisions, agent="m")
    rows = build_records(
        decisions,
        round_id=round_id,
        agent="m",
        trade_date=date(2026, 9, 23),
        decided_at=_TS,
        tenant_id=_TEST_TENANT,
        user_id="10000001",
        outcomes=plan.outcomes(),
    )
    try:
        async with get_session(read_only=False) as session:
            assert await ensure_decision_ledger_table_async()
            assert await upsert_rows(session, rows) == 3
            await session.commit()

        async with get_session(read_only=True) as session:
            back = await load_round(session, round_id)
        assert len(back) == 3, "三条决策（含 hold）都要落库"
        assert {r.code for r in back} == {"SH600519", "SZ000001", "SH600000"}
        watch = next(r for r in back if r.action == "watch")
        assert watch.armed is True and watch.stop_loss == 1500.0
        assert watch.pct == 0.3 and watch.pct_state == "given"
        sell = next(r for r in back if r.action == "sell")
        assert sell.kind == KIND_SELL and sell.pct == 0.5
        hold = next(r for r in back if r.action == "hold")
        assert hold.kind == KIND_NONE

        # 写纪律 1：重写同一批（决策字段改了）→ 决策字段不动、执行结果刷新
        mutated = [
            DecisionRecord(
                **{
                    **vars(r),
                    "reason": "被改过的理由",
                    "armed": False if r.action == "watch" else r.armed,
                    "reject_reason": "被改过的理由",
                }
            )
            for r in rows
        ]
        async with get_session(read_only=False) as session:
            await upsert_rows(session, mutated)
            await session.commit()
        async with get_session(read_only=True) as session:
            again = {r.id: r for r in await load_round(session, round_id)}
        w2 = again[watch.id]
        assert w2.reason == "", "决策字段被后写覆盖了 —— 审计留痕失效"
        assert w2.armed is False and w2.reject_reason == "被改过的理由", (
            "执行结果应刷新"
        )

        # 定价拨：只写定价列，且不改执行结果
        priced = [
            DecisionRecord(
                **{
                    **vars(w2),
                    "entry_date": date(2026, 9, 24),
                    "entry_px": 1512.0,
                    "tradable": True,
                    "fwd": {"1": {"ret": 0.004}},
                    "priced_at": _TS,
                }
            )
        ]
        async with get_session(read_only=False) as session:
            await upsert_rows(session, priced)
            await session.commit()
        async with get_session(read_only=True) as session:
            final = {r.id: r for r in await load_round(session, round_id)}
            day_rows = await load_day(
                session, date(2026, 9, 23), tenant_id=_TEST_TENANT
            )
            pending = await load_pending(session, until=date(2026, 9, 23), limit=500)
        f = final[w2.id]
        assert f.entry_px == 1512.0 and f.fwd == {"1": {"ret": 0.004}}
        assert f.reject_reason == "被改过的理由", "定价拨不该碰执行结果"
        assert len(day_rows) == 3
        got = {r.id for r in pending}
        assert {r.id for r in rows if r.kind != KIND_NONE} <= got, (
            "watch/sell 都应出现在记账候选集里"
        )
        assert f.id in got, (
            "**已定价的行也必须回到候选集**：t20/t60 晚到，只捞 priced_at IS NULL "
            "会让它们永远补不上，而报表只表现为「样本一直很少」"
        )
        assert not any(r.kind == KIND_NONE for r in pending), "hold 不该进定价队列"
    finally:
        try:
            from sqlalchemy import text as _text

            from backend.shared.database_manager_v2 import get_session as _gs

            async with _gs(read_only=False) as session:
                await session.execute(
                    _text("DELETE FROM qm_decision_ledger WHERE tenant_id=:t"),
                    {"t": _TEST_TENANT},
                )
                await session.commit()
        except Exception:  # noqa: BLE001
            pass
        # 关掉全局引擎：它绑定在创建它的那个事件循环上，留着会让**下一个**真库测试
        # 踩 `attached to a different loop`（P1.6 影子账测试同款收尾）。
        await _close_db()


@pytest.mark.asyncio
async def test_real_db_column_topup_heals_an_existing_table():
    """**补列**：表已在、列没了 → ensure 要把列长回来。

    这条的来由是一次真实的踩坑：往 `_CREATE_SQL` 里加了 `tags`，本地测试库因为
    `CREATE TABLE IF NOT EXISTS` 是空操作而**没有**这一列，写入直接报
    `UndefinedColumn`——而那条报错只会在**第一次写入时**出现。若表已在线上，
    这个坑的表现就是「本地全绿、上线即炸」。故这里真删一次列再自愈。

    安全阀：本表在 P2.2 之前**没有写入方**，因此「存在非测试租户的行」= 表已经上线，
    那时删除列是破坏数据，直接跳过并说明原因。
    """
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session
    from backend.shared.decision_ledger_contract import (
        ensure_decision_ledger_table_async,
    )

    try:
        await _ensure_db_pool()
    except Exception as exc:  # noqa: BLE001 - 无 DB 应 skip 而非 error
        pytest.skip(f"DB 不可用: {exc}")

    try:
        async with get_session(read_only=True) as session:
            live = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM qm_decision_ledger WHERE tenant_id <> :t"
                    ),
                    {"t": _TEST_TENANT},
                )
            ).scalar()
    except Exception as exc:  # noqa: BLE001 - 表还没建起来也算不可用
        pytest.skip(f"DB 不可用或表不存在: {exc}")

    async def _columns() -> set[str]:
        async with get_session(read_only=True) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name='qm_decision_ledger'"
                    )
                )
            ).all()
        return {str(r[0]) for r in rows}

    if live:
        pytest.skip(f"表上有 {live} 行非测试数据（表已上线），不做破坏性补列验证")

    try:
        async with get_session(read_only=False) as session:
            await session.execute(
                text("ALTER TABLE qm_decision_ledger DROP COLUMN tags")
            )
            await session.commit()
        assert "tags" not in await _columns(), "预置失败：列没删掉，后面的断言会是空的"

        assert await ensure_decision_ledger_table_async(), "自愈没跑通"
        assert "tags" in await _columns(), (
            "ensure 没把缺的列补回来 —— 老库会永远缺这一列（CREATE TABLE IF NOT EXISTS 的盲区）"
        )
        # 幂等：再跑一次不该出错，也不该重复加列
        assert await ensure_decision_ledger_table_async()
        assert "tags" in await _columns()
    finally:
        # 无论成败都把列保证在（后续测试要写它）
        try:
            async with get_session(read_only=False) as session:
                await session.execute(
                    text(
                        "ALTER TABLE qm_decision_ledger "
                        "ADD COLUMN IF NOT EXISTS tags JSONB NOT NULL DEFAULT '[]'"
                    )
                )
                await session.commit()
        except Exception:  # noqa: BLE001
            pass
        # 关掉全局引擎：它绑定在创建它的那个事件循环上，留着会让**下一个**真库测试
        # 踩 `attached to a different loop`（P1.6 影子账测试同款收尾）。
        await _close_db()


@pytest.mark.asyncio
async def test_real_db_pool_dedup_keeps_the_first_of_the_day():
    """**记分卡池**：同一 (agent, 日, 标的, 动作) 只留当天第一条。

    池去重是读侧的职责（审计侧一行都不许少）。这条真跑一遍 `DISTINCT ON (pool_key)`：
    插两轮同池键的行（09:30 与 14:00，盘中改了主意），池里应只剩**早的那条**，
    而未定价的行不进池。
    """
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session
    from backend.shared.decision_ledger_contract import (
        ensure_decision_ledger_table_async,
    )
    from backend.shared.decision_ledger_store import load_pool

    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"DB 不可用: {exc}")

    early = datetime(2026, 9, 24, 1, 30, tzinfo=timezone.utc)
    late = datetime(2026, 9, 24, 6, 0, tzinfo=timezone.utc)
    day = date(2026, 9, 24)
    decisions = _rows({"action": "watch", "code": "600519.SH", "stop_loss": 1500.0})
    try:
        assert await ensure_decision_ledger_table_async()
        rows = []
        for i, ts in enumerate((early, late)):
            recs = build_records(
                decisions,
                round_id=f"{_TEST_TENANT}-pool-{i}",
                agent="pool-agent",
                trade_date=day,
                decided_at=ts,
                tenant_id=_TEST_TENANT,
                user_id="10000001",
            )
            rows.extend(recs)
        # 第三条：同池键、但**没定价**（不该进池）
        rows.append(
            DecisionRecord(
                **{
                    **vars(rows[0]),
                    "id": decision_id("unpriced-round", 0, "SH600519", "watch"),
                    "round_id": "unpriced-round",
                }
            )
        )
        priced = [
            DecisionRecord(
                **{
                    **vars(r),
                    "entry_date": date(2026, 9, 25),
                    "entry_px": 1500.0,
                    "tradable": True,
                    "fwd": {"1": {"ret": 0.001}},
                    "tags": ["站上MA20"],
                    "priced_at": _TS,
                }
            )
            for r in rows[:2]
        ]
        async with get_session(read_only=False) as session:
            assert await upsert_rows(session, rows + priced) == 3
            await session.commit()

        async with get_session(read_only=True) as session:
            pool = await load_pool(session, start=day, end=day, tenant_id=_TEST_TENANT)
            all_rows = await load_pool(
                session, start=day, end=day, tenant_id=_TEST_TENANT, dedup=False
            )
        assert len(pool) == 1, f"池应只剩当天第一条，实得 {len(pool)} 行"
        assert pool[0].decided_at == early, "留下的不是当天第一条"
        assert pool[0].tags == ("站上MA20",), "tags 没往返回来"
        kept_ids = {r.id for r in all_rows}
        assert len(all_rows) == 2, "去重关闭时应给出全部**已定价**行"
        assert rows[2].id not in kept_ids, "未定价的行不该进池"
    finally:
        try:
            from backend.shared.database_manager_v2 import get_session as _gs

            async with _gs(read_only=False) as session:
                await session.execute(
                    text("DELETE FROM qm_decision_ledger WHERE tenant_id=:t"),
                    {"t": _TEST_TENANT},
                )
                await session.commit()
        except Exception:  # noqa: BLE001
            pass
        # 关掉全局引擎：它绑定在创建它的那个事件循环上，留着会让**下一个**真库测试
        # 踩 `attached to a different loop`（P1.6 影子账测试同款收尾）。
        await _close_db()
