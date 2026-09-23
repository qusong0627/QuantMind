"""存量池导入（P2.1 收尾）：隔壁 `decision_pool.jsonl` → 审计行。

两层（与 `test_decision_ledger_store` 同组织）：**纯映射**（绝大多数，无 DB）＋
**真库端到端**（`--apply` 写一次，测试租户写后即清）——后者是干跑够不着的那一段。

这一层的存在理由是**一次性迁移不可回滚**：隔壁那 207 行随系统下线而消失，导错的
行事后与「模型那天没说话」完全同形，没有任何下游会报出来。故这里钉四件事：

1. **决策原样收**——`kind` 绝不重算。隔壁池的 `kind` 编码的是**决策当期持仓状态**
   （`watch` 拆成 79 `position` / 68 `bullish`），那份知识随系统下线就没了；用本仓
   当前持仓去重建会把一批持仓管理记成选股意向，而这两件事正是 P2.1 要分开的。
2. **行情段一律不搬**——隔壁的 `entry_px`/`fwd` 出自 `daily_backward`（跨 vintage
   拼缝的坏序列）。搬过来等于把已知「符号可能翻转」的收益当历史战绩喂给模型。
3. **形态硬判**——`date.fromisoformat` 在 3.11+ 接受 `20260909`、3.10 抛错；边界写成
   标准库调用，同一份池在新旧解释器上就是两种读法。
4. **要么全进、要么一条不进**——坏行逐条点名（含行号），调用方见 `problems` 非空
   就一条都不写。

金样行的字段形状逐字取自真实池首行（后缀式 `code`、`+08:00` 的 `ts`、带满行情段），
数值随测试改写；**真实 207 行不入库、不进仓库**（那是模型选股记录）。
"""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import date, datetime, timezone

import pytest

from backend.scripts.decision_ledger import main
from backend.shared.decision.contract import PCT_GIVEN, PCT_MISSING
from backend.shared.decision_ledger_store import (
    KIND_BULLISH,
    KIND_POSITION,
    KIND_SELL,
    POOL_IMPORT_PREFIX,
    decision_id,
    iso_date,
    pool_key,
    records_from_pool,
)

#: 真实池首行的形状（值也照抄，含那个 6 位微秒的 `+08:00` 时刻）
_TS_SHANGHAI = "2026-09-08T14:45:01.183741+08:00"
_TS_UTC = datetime(2026, 9, 8, 6, 45, 1, 183741, tzinfo=timezone.utc)
_DAY = date(2026, 9, 8)


def _row(**over):
    """一条形状照抄真实池的行（未覆盖的键就是隔壁写出来的那些）。"""
    row = {
        "id": "c183fa694280cdf1",
        "agent": "deepseek-v4-pro",
        "date": "2026-09-08",
        "ts": _TS_SHANGHAI,
        "action": "watch",
        "kind": "bullish",
        "code": "001312.SZ",
        "name": "福恩股份",
        "pct": 1.0,
        "confidence": 0.85,
        "stop_loss": 16.6,
        "take_profit": None,
        "reason": "T+1不可卖量0：现价17.63贴成本+0.41%缩量横盘，无加仓理由",
        "source": "backfill_last_decisions",
        "entry_dt": 20260909,
        "entry_px": 17.75,
        "tradable": True,
        "tags": ["破MA20"],
        "fwd": {"t1": {"ret": 0.0, "bench": -0.0038, "excess": 0.0038}},
        "updated": "2026-09-23",
    }
    row.update(over)
    return row


def _one(**over):
    """导一行且断言干净（省掉每个用例重复的解包与断言）。"""
    recs, problems = records_from_pool([_row(**over)])
    assert problems == ()
    assert len(recs) == 1
    return recs[0]


# ── 决策原样收 ──────────────────────────────────────────────────────
def test_real_shaped_row_maps_field_by_field():
    r = _one()
    # 身份：审计 id 用**归一后**代码、池键用同一套输入（见 pool_key 的说明）
    assert r.round_id == f"{POOL_IMPORT_PREFIX}:c183fa694280cdf1"
    assert r.code_raw == "001312.SZ", "隔壁原样写法必须留底（对账按元组，靠它）"
    assert r.code == "SZ001312"
    assert r.id == decision_id(r.round_id, 0, "SZ001312", "watch")
    assert r.pool_key == pool_key("deepseek-v4-pro", _DAY, "SZ001312", "watch")
    # 时刻：上海墙钟 → aware UTC（瞬时列口径）
    assert r.decided_at == _TS_UTC
    assert r.trade_date == _DAY
    # 决策内容
    assert (r.agent, r.action, r.kind) == ("deepseek-v4-pro", "watch", KIND_BULLISH)
    assert r.pct == 1.0 and r.pct_state == PCT_GIVEN and r.pct_raw == "1.0"
    assert r.confidence == 0.85 and r.stop_loss == 16.6 and r.take_profit is None
    assert r.reason.startswith("T+1不可卖量0")
    # 存量池没有的维度：用户留空、租户落默认
    assert r.user_id == "" and r.tenant_id == "default" and r.market == "CN"


def test_market_segment_is_deliberately_dropped():
    """隔壁带满行情段的行进来，行情段**一律为空**——等本仓 qfq 重算。

    这是本函数最容易「好心办坏事」的地方：那 6 个键在隔壁都有值（实测 137 行带
    `entry_dt`/`entry_px`），照搬看着无害，实则是把坏序列（`daily_backward`）的
    收益当历史战绩。
    """
    r = _one(
        entry_dt=20260909,
        entry_px=17.75,
        tradable=True,
        tags=["破MA20", "放量"],
        fwd={"t1": {"ret": 0.0123}, "t5": {"ret": 0.04}},
    )
    assert r.entry_date is None
    assert r.entry_px is None
    assert r.tradable is None
    assert r.tags == ()
    assert r.fwd is None
    assert r.priced_at is None, "没定价 → load_pending 才会捞它去补行情"


@pytest.mark.parametrize("kind", [KIND_BULLISH, KIND_POSITION, KIND_SELL])
def test_kind_is_taken_verbatim_never_recomputed(kind):
    """`kind` 原样收：本函数**没有**持仓信息，重算必然是编的。

    `watch` + `position` 这一格是分水岭：本表 `kind_of('watch', code, held)` 要问
    「决策当时是否持有」，而那是隔壁运行时的状态——本函数拿不到，也不该猜。
    """
    r = _one(action="watch", kind=kind)
    assert r.kind == kind


def test_pool_kind_none_is_rejected():
    """`none` 是本表 `kind_of` 的不跟踪档，**不该出现在池里**（池只落跟踪得了的）。

    实测那 207 行只有 bullish/position/sell。真收进 `none` 只会让记分卡多一个永远
    空掉的类别——空类别看起来像「这类还没样本」，不像「导错了」。
    """
    recs, problems = records_from_pool([_row(kind="none")])
    assert recs == [] and len(problems) == 1
    assert "kind 不认识" in problems[0]


# ── 三态与 bool 陷阱 ────────────────────────────────────────────────
def test_pct_none_is_missing_not_zero():
    """`pct=None` → 三态里的 `missing`，**绝不落成 0.0**。

    0.0 是个声明（「模型说仓位为零」），None 是没给——记分卡按 `given` 分组统计时
    这一格会分错组。
    """
    r = _one(pct=None)
    assert r.pct is None and r.pct_state == PCT_MISSING and r.pct_raw == ""


def test_pct_zero_is_given():
    """对照组：真的 0.0 是 `given`（`isinstance` 判的是类型不是真假——
    这里若写成 `if pct:`，0.0 会被吞成 missing）。"""
    r = _one(pct=0.0)
    assert r.pct == 0.0 and r.pct_state == PCT_GIVEN and r.pct_raw == "0.0"


@pytest.mark.parametrize("key", ["pct", "confidence", "stop_loss", "take_profit"])
def test_bool_is_not_a_number(key):
    """`bool` 是 `int` 的子类——`True` 会静默变成 1.0 的仓位/止损价。"""
    assert getattr(_one(**{key: True}), key) is None


# ── 形态硬判 ────────────────────────────────────────────────────────
@pytest.mark.parametrize("bogus", ["20260908", "2026/09/08", "2026-9-8", "2026-09"])
def test_compact_or_slashed_date_is_rejected_not_coerced(bogus):
    """`20260908` 是**解释器版本的函数**：3.11+ 的 `fromisoformat` 收，3.10 抛。

    放它过去，同一份池在新旧解释器上就是两种读法。故这条边界按形态判，不交标准库。
    """
    recs, problems = records_from_pool([_row(date=bogus)])
    assert recs == [] and len(problems) == 1
    assert "不是 ISO 形态" in problems[0]


def test_iso_date_rejects_and_names_the_subject():
    assert iso_date("2026-09-08") == _DAY
    with pytest.raises(ValueError, match="影子账入场日不是 ISO 形态"):
        iso_date("20260908", what="影子账入场日")


def test_unparsable_ts_is_a_named_problem_not_a_crash():
    """`ts` 解析不出 → **点名**，不是抛穿（也不是回落到写入时刻）。

    与本表 `_decided_at` 的「宁可当场炸」不矛盾：那边是写单行时的最后一道闸，
    这边是批量导入的体检——体检要一次报全，不能第一行就掀桌。
    """
    recs, problems = records_from_pool([_row(ts="昨天下午")])
    assert recs == [] and len(problems) == 1
    assert "decided_at 不是可解析的时间" in problems[0]


def test_bare_date_ts_is_accepted_as_midnight_utc():
    """`ts` 只给日期时按 UTC 零点收（`_to_dt` 的既有契约，不是本函数新定的）。"""
    r = _one(ts="2026-09-08")
    assert r.decided_at == datetime(2026, 9, 8, tzinfo=timezone.utc)


# ── 要么全进、要么一条不进 ──────────────────────────────────────────
@pytest.mark.parametrize("key", ["id", "agent", "date", "ts", "action", "kind", "code"])
def test_each_required_field_missing_is_named(key):
    """七个必需键逐个拔掉：**点名到字段**（不导的是哪一条、缺的是什么）。"""
    row = _row()
    del row[key]
    recs, problems = records_from_pool([row])
    assert recs == []
    assert len(problems) == 1
    assert "第 1 行" in problems[0] and key in problems[0]


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_blank_string_counts_as_missing(blank):
    """空白串不算有值——`" "` 能过 `if not v` 的话，`agent=" "` 会造出一个空 agent
    的类别行，而它看起来像是某个模型的成绩。"""
    recs, problems = records_from_pool([_row(code=blank)])
    assert recs == [] and "code" in problems[0]


def test_unknown_action_is_rejected():
    recs, problems = records_from_pool([_row(action="hold")])
    assert recs == [] and "action 不认识" in problems[0]


def test_bad_rows_do_not_stop_the_scan_and_row_numbers_are_1_based():
    """三行里坏中间那行：好的两条照样转出来，坏的那条点名到**第 2 行**。

    扫描不短路是刻意的：体检要一次报全（调用方见 `problems` 非空会整体拒收，
    但人得一次看全才能把文件修对）。
    """
    good_a, bad, good_b = _row(id="a"), _row(id="b", action="hold"), _row(id="c")
    recs, problems = records_from_pool([good_a, bad, good_b])
    assert [r.code_raw for r in recs] == ["001312.SZ", "001312.SZ"]
    assert len(problems) == 1 and problems[0].startswith("第 2 行 ")


def test_empty_input_is_clean():
    assert records_from_pool([]) == ([], ())
    assert records_from_pool(None) == ([], ())  # type: ignore[arg-type]


# ── 身份键与本仓口径的接缝 ──────────────────────────────────────────
def test_same_decision_in_two_code_forms_merges_in_this_table():
    """同一决策写成 `001312.SZ` / `SZ001312`：本表并成**一个** pool_key。

    隔壁的键吃模型原样写法，会裂成两条池行；本表先归一。这是**防御性**断言——
    实测那 207 行全是后缀式、没有这种裂法——写下来是因为「归一」这件事一旦被
    去掉（例如有人为了「与隔壁逐字对齐」改回原样喂哈希），池的行数会静默变化。
    """
    a, b = _row(code="001312.SZ"), _row(code="SZ001312")
    assert a["code"] != b["code"]
    recs, problems = records_from_pool([a, b])
    assert problems == ()
    assert recs[0].pool_key == recs[1].pool_key
    assert recs[0].code_raw != recs[1].code_raw, "原样写法仍各自留底"


def test_same_id_different_code_makes_two_rows_with_one_round_id():
    """同 `id` 不同代码 → 审计 id 不同（能共存），但 `round_id` 撞键。

    这正是 `cmd_import_pool` 见到重复 `round_id` 就**硬停**的原因：隔壁的 `id`
    由 (agent/日/标的/动作) 生成，撞 id 而标的不同说明池本身坏了，导进去后
    「哪条是原有的」无从分辨。
    """
    recs, problems = records_from_pool([_row(code="001312.SZ"), _row(code="600036.SH")])
    assert problems == ()
    assert recs[0].round_id == recs[1].round_id
    assert recs[0].id != recs[1].id


def test_id_index_is_pinned_to_zero():
    """一个池行 = 一轮一条决策（序号 0）。序号若跟着列表位置走，同一行在两个文件
    里的位置不同就会算出两个审计 id——迁移重跑会造出重复行。"""
    assert _one().id == decision_id("qt-pool:c183fa694280cdf1", 0, "SZ001312", "watch")


# ── 参数与纯度 ──────────────────────────────────────────────────────
def test_prefix_and_tenant_and_market_are_carried():
    recs, _ = records_from_pool(
        [_row()], round_prefix="qt-2021", tenant_id="acme", market="HK"
    )
    r = recs[0]
    assert r.round_id.startswith("qt-2021:") and r.tenant_id == "acme"
    assert r.market == "HK"


def test_blank_tenant_falls_back_to_default():
    recs, _ = records_from_pool([_row()], tenant_id="")
    assert recs[0].tenant_id == "default"


@pytest.mark.parametrize("ctx", [None, "shown", 3])
def test_non_mapping_pool_ctx_becomes_none_not_empty_dict(ctx):
    """`None`（本轮未插桩）与 `{}`（池外）是两件事，不许抹平。

    实测那 207 行 138 行没有 `pool_ctx`：写成 `{}` 会把「没插桩」变成「查过、不在
    池里」——后者是**关于模型的信息**，前者不是。
    """
    assert _one(pool_ctx=ctx).pool_ctx is None


def test_pool_ctx_is_copied_not_aliased():
    """行内嵌的 dict 要拷贝：别把可变对象挂进审计行（本仓不可变纪律）。

    别名还有一层实害——调用方事后改原 dict，会连带改掉「已经审过的那一行」。
    """
    ctx = {"file": "20260921_agent_picks.json", "n_shown": 16, "shown": True}
    r = _one(pool_ctx=ctx)
    assert r.pool_ctx == ctx
    assert r.pool_ctx is not ctx


def test_input_rows_are_not_mutated():
    row = _row(pool_ctx={"shown": True})
    before = copy.deepcopy(row)
    records_from_pool([row])
    assert row == before


def test_long_reason_is_truncated_to_the_column_limit():
    r = _one(reason="字" * 3000)
    assert len(r.reason) == 2000


# ── 真库端到端：`--apply` 写库（测试租户，写后即清）─────────────────
#: 测试租户：带 `t-` 前缀（与 P1.6 同约定，读侧默认按 `strpos` 排除）
_APPLY_TENANT = "t-p21-import"


async def _ensure_db_pool() -> None:
    """探一次并**立即关掉**——与 `test_decision_ledger_store` 的差别就在那个 finally。

    那边的用例整个跑在一个 loop 里，引擎留着正好；本文件的真库用例是**同步**的
    （`cmd_import_pool` 内部自己 `asyncio.run`），探完不关，`main()` 就会拿到绑在
    **上一个已结束的 loop** 上的引擎。实测：第一次跑就是这么炸的，报
    `RuntimeError: Event loop is closed` + `got Future attached to a different loop`，
    而 `main()` 把 `RuntimeError` 收成退出码 2——症状是「导入失败」，根因却在这一行。
    """
    from sqlalchemy import text as _t

    from backend.shared.database_manager_v2 import close_database, get_session

    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(_t("SELECT 1"))
    except Exception:  # noqa: BLE001 - 陈旧池子即刷新
        await close_database()
        async with get_session(read_only=True) as probe:
            await probe.execute(_t("SELECT 1"))
    finally:
        await close_database()


async def _read_tenant(tenant: str):
    from backend.shared.database_manager_v2 import close_database, get_session
    from backend.shared.decision_ledger_store import load_pool

    try:
        async with get_session(read_only=True) as session:
            # dedup=False + include_unpriced=True：看**审计视角的全量**，
            # 否则「导入的行还没定价」会让它一条都读不回来（priced_at IS NULL 被滤掉）
            return await load_pool(
                session, tenant_id=tenant, dedup=False, include_unpriced=True
            )
    finally:
        await close_database()


async def _clear_tenant(tenant: str) -> None:
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import close_database, get_session
    from backend.shared.decision_ledger_contract import TABLE

    try:
        async with get_session(read_only=False) as session:
            await session.execute(
                text(f"DELETE FROM {TABLE} WHERE tenant_id = :t"), {"t": tenant}
            )
            await session.commit()
    finally:
        await close_database()


def _write_pool(tmp_path, rows) -> str:
    p = tmp_path / "decision_pool.jsonl"
    p.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )
    return str(p)


def test_apply_writes_the_file_and_reruns_idempotently(tmp_path):
    """`import-pool --apply` 端到端一次真库：写进去的是什么，读回来就是什么。

    这条用例的存在理由是**干跑够不着这一层**：dry-run 在写库之前就 return 了，
    那几行只有真写一次才被执行。本批就在那里逮到一个真 bug——
    `upsert_rows(session, records, priced=False)`：`priced` 这个形参在「拨次按行内
    有无 `fwd` 自判」那次重构里已经去掉，纯函数测试和干跑**双双看不到**，
    `--apply` 一跑就是 `TypeError`（一次性迁移里这意味着「第一次真跑才发现」）。

    幂等是同一件事的另一半：迁移要能重跑（补导、换前缀），重跑不得改决策字段。
    """
    rows = [
        _row(id="r-1"),
        _row(id="r-2", action="buy", kind=KIND_BULLISH, code="600036.SH", pct=None),
        _row(id="r-3", action="sell", kind=KIND_SELL, code="000001.SZ", pct=0.5),
    ]
    src = _write_pool(tmp_path, rows)

    try:
        asyncio.run(_ensure_db_pool())
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"DB 不可用: {exc}")

    try:
        assert (
            main(["import-pool", "--from", src, "--tenant", _APPLY_TENANT, "--apply"])
            == 0
        )
        first = asyncio.run(_read_tenant(_APPLY_TENANT))
        assert len(first) == 3, "三条都得进库（池的 kind 里没有 none，不会有人被滤掉）"
        by_id = {r.round_id: r for r in first}
        assert set(by_id) == {f"{POOL_IMPORT_PREFIX}:r-{i}" for i in (1, 2, 3)}
        r1 = by_id[f"{POOL_IMPORT_PREFIX}:r-1"]
        assert r1.code_raw == "001312.SZ" and r1.code == "SZ001312"
        assert r1.kind == KIND_BULLISH and r1.decided_at == _TS_UTC
        assert r1.pct_state == PCT_GIVEN and r1.pct == 1.0
        r2 = by_id[f"{POOL_IMPORT_PREFIX}:r-2"]
        assert r2.pct_state == PCT_MISSING and r2.pct is None
        assert by_id[f"{POOL_IMPORT_PREFIX}:r-3"].action == "sell"
        assert all(r.priced_at is None for r in first), (
            "没定价才会被 load_pending 捞去补行情"
        )
        assert all(r.pool_ctx is None for r in first)

        # 重跑：行数不变、决策字段不变（写纪律 1 的行语义，只有真库能验）
        assert (
            main(["import-pool", "--from", src, "--tenant", _APPLY_TENANT, "--apply"])
            == 0
        )
        again = asyncio.run(_read_tenant(_APPLY_TENANT))
        assert len(again) == 3
        assert {r.id for r in again} == {r.id for r in first}
        assert {r.reason for r in again} == {r.reason for r in first}
    finally:
        asyncio.run(_clear_tenant(_APPLY_TENANT))
