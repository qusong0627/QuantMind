"""影子代价账核心的不变量（P1.6）。

样本形状取自**线上真留痕**（`qm:risk:decisions:20260923` 的 xrevrange 首条），
不是按代码想象造的——留痕里既有拦单的 REJECT 也有放行的 WARN，两者的区别正是
这份提取器的全部难点。
"""

from __future__ import annotations

import json
from dataclasses import replace

from backend.shared.risk.ghost import (
    GhostRow,
    cost_of,
    cost_sign,
    dedup_rows,
    extract_rows,
    ghost_id,
    normalize_symbol,
    parse_decisions,
    plan_rekey,
    priced_rows,
    rows_from_decision_entry,
    summary_lines,
)
from backend.shared.risk.gate_registry import KIND_STRUCTURAL, KIND_VETO


# ── 线上真留痕（2026-09-23 18:26 一条真影子拒单，字段原样）─────────────
LIVE_ENTRY = {
    "ts": "1790159202.039",
    "tenant": "default",
    "uid": "10000001",
    "symbol": "SH600000",
    "side": "buy",
    "qty": "900.0",
    "source": "tdx_l2",
    "verdict": "reject",
    "enforced": "false",
    "version": "2",
    "decisions": json.dumps(
        [
            {
                "rule_id": "l0.session",
                "level": "L0",
                "action": "WARN",
                "reason": "盘后入队：申报时段校验延后到派发环节",
                "evidence": {"hm": "18:26"},
            },
            {
                "rule_id": "l1.leverage_cap",
                "level": "L1",
                "action": "WARN",
                "reason": "账户快照陈旧或时点不可得（数据可得性，放行并记录）",
                "evidence": {"projected_leverage": 0.0674, "account_source": "tdx_bridge"},
            },
            {
                "rule_id": "l1.new_buys_per_day",
                "level": "L1",
                "action": "REJECT",
                "reason": "当日新开仓已达上限",
                "evidence": {
                    "opened_today": 1,
                    "max_new_buys": 1,
                    "opened_symbols": ["SH600276"],
                },
            },
            {
                "rule_id": "l3.stale_quote",
                "level": "L3",
                "action": "WARN",
                "reason": "盘后入队：行情时效校验延后到派发环节",
                "evidence": {"age_s": None, "price_source": "fallback_close"},
            },
        ],
        ensure_ascii=False,
    ),
    "checked": "l0.clock_drift,l0.kill_switch,l0.session,...",
    "tier": "defensive/doc",
}


def test_live_entry_yields_only_the_blocking_rule():
    """线上样本：4 条 decisions 里只有 1 条 REJECT，其余是 WARN（放行）。"""
    rows = rows_from_decision_entry(LIVE_ENTRY)
    assert [r.rule_id for r in rows] == ["l1.new_buys_per_day"]
    row = rows[0]
    assert row.symbol == "SH600000"
    assert row.side == "buy"
    assert row.quantity == 900.0
    assert row.source == "tdx_l2"
    assert row.enforced is False
    assert row.version == 2
    assert row.kind == KIND_VETO
    assert row.registered is True
    assert row.evidence["max_new_buys"] == 1


def test_warn_only_entry_yields_nothing():
    """整条留痕只有 WARN → 没有影子账行（WARN 不拦单）。"""
    entry = dict(LIVE_ENTRY)
    entry["decisions"] = json.dumps(
        [{"rule_id": "l0.session", "action": "WARN", "reason": "盘后入队"}]
    )
    assert rows_from_decision_entry(entry) == []


def test_structural_rule_is_recorded_but_not_priced():
    """structural 规则照记（触发数要可见），但不进代价统计。"""
    entry = dict(LIVE_ENTRY)
    entry["decisions"] = json.dumps(
        [
            {
                "rule_id": "l3.lot_size",
                "action": "REJECT",
                "reason": "买入数量非整手",
                "evidence": {"quantity": 150, "lot": 100},
            }
        ]
    )
    rows = rows_from_decision_entry(entry)
    assert len(rows) == 1
    assert rows[0].kind == KIND_STRUCTURAL
    assert priced_rows(rows) == [], "structural 规则不得进入代价统计"


def test_unknown_rule_is_recorded_as_unregistered_veto():
    """未登记的规则照记，但标 registered=False（报告里必须显式列出）。"""
    entry = dict(LIVE_ENTRY)
    entry["decisions"] = json.dumps([{"rule_id": "l9.brand_new", "action": "REJECT"}])
    rows = rows_from_decision_entry(entry)
    assert len(rows) == 1
    assert rows[0].registered is False
    assert rows[0].kind == KIND_VETO, "未登记规则按最强口径记"


def test_unknown_rule_is_not_counted_as_priced_kind_but_still_extracted():
    """未登记 ≠ 不该记：仍进账本（供人工审阅），只是 kind 按 veto 记。"""
    entry = dict(LIVE_ENTRY)
    entry["decisions"] = json.dumps([{"rule_id": "l9.brand_new", "action": "HALT"}])
    rows = rows_from_decision_entry(entry)
    assert len(priced_rows(rows)) == 1


def test_idempotency_key_includes_rule_and_side():
    base = rows_from_decision_entry(LIVE_ENTRY)[0]
    same = rows_from_decision_entry(LIVE_ENTRY)[0]
    assert ghost_id(base) == ghost_id(same), "同输入必须同键（幂等写库的前提）"

    other_rule = GhostRow(**{**base.__dict__, "rule_id": "l1.position_cap"})
    assert ghost_id(base) != ghost_id(other_rule), "不同规则同日同标的是两件事"

    other_side = GhostRow(**{**base.__dict__, "side": "sell"})
    assert ghost_id(base) != ghost_id(other_side), "买被拦与卖被拦成本符号相反"

    other_day = GhostRow(**{**base.__dict__, "date": "2026-09-22"})
    assert ghost_id(base) != ghost_id(other_day)
    other_tenant = GhostRow(**{**base.__dict__, "tenant": "t2"})
    assert ghost_id(base) != ghost_id(other_tenant)


def test_dedup_keeps_earliest_and_is_order_independent():
    """同规则同日同标的反复触发只记一次，且保留**最早**那条。"""
    early = rows_from_decision_entry({**LIVE_ENTRY, "ts": "1790150000.000"})[0]
    late = rows_from_decision_entry({**LIVE_ENTRY, "ts": "1790159202.039"})[0]
    assert early.ts < late.ts

    assert len(dedup_rows([late, early])) == 1
    assert dedup_rows([late, early])[0].ts == early.ts
    assert dedup_rows([early, late])[0].ts == early.ts, "去重结果与输入顺序无关"


def test_dedup_keeps_distinct_rules_on_same_symbol_day():
    d1 = json.dumps([{"rule_id": "l1.new_buys_per_day", "action": "REJECT"}])
    d2 = json.dumps([{"rule_id": "l1.position_cap", "action": "REJECT"}])
    rows = extract_rows(
        [{**LIVE_ENTRY, "decisions": d1}, {**LIVE_ENTRY, "decisions": d2}]
    )
    assert sorted(r.rule_id for r in rows) == ["l1.new_buys_per_day", "l1.position_cap"]


def test_one_order_blocked_by_two_rules_yields_two_rows():
    """同一单被两条规则拦 → 两行（成本各归各的规则）。"""
    entry = dict(LIVE_ENTRY)
    entry["decisions"] = json.dumps(
        [
            {"rule_id": "l1.leverage_cap", "action": "REJECT", "reason": "总杠杆超上限"},
            {"rule_id": "l3.max_order_value", "action": "REJECT", "reason": "单笔金额超限"},
        ]
    )
    rows = rows_from_decision_entry(entry)
    assert len(rows) == 2
    assert len({ghost_id(r) for r in rows}) == 2


# ── 时间口径 ────────────────────────────────────────────────────────
def test_day_is_resolved_in_cst_not_utc():
    """留痕 ts 是 epoch 秒：00:30 CST 属于**当天**，不能算成 UTC 的前一天。"""
    # 2026-09-23 00:30:00 CST == 2026-09-22 16:30:00 UTC
    ts = 1790094600.0
    from datetime import datetime, timedelta, timezone

    assert datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=8))).strftime(
        "%Y-%m-%d %H:%M"
    ) == "2026-09-23 00:30"

    entry = {**LIVE_ENTRY, "ts": str(ts)}
    assert rows_from_decision_entry(entry)[0].date == "2026-09-23"


# ── 脏值与缺字段：宁可少记，不可造行 ──────────────────────────────────
def test_missing_symbol_or_side_yields_nothing():
    d = json.dumps([{"rule_id": "l1.leverage_cap", "action": "REJECT"}])
    assert rows_from_decision_entry({**LIVE_ENTRY, "symbol": "", "decisions": d}) == []
    assert rows_from_decision_entry({**LIVE_ENTRY, "side": "", "decisions": d}) == []
    # 方向不认识（既非买也非卖）→ 成本符号不可定 → 不记
    assert rows_from_decision_entry({**LIVE_ENTRY, "side": "hold", "decisions": d}) == []


def test_ts_missing_yields_nothing():
    """日期解释不出来就不记：没有日期的行会被算进错误的窗口。"""
    d = json.dumps([{"rule_id": "l1.leverage_cap", "action": "REJECT"}])
    assert rows_from_decision_entry({**LIVE_ENTRY, "ts": "", "decisions": d}) == []
    assert rows_from_decision_entry({**LIVE_ENTRY, "ts": "not-a-number", "decisions": d}) == []


def test_broken_decisions_json_is_empty_not_raise():
    assert parse_decisions("{not json") == []
    assert parse_decisions(None) == []
    assert parse_decisions({"rule_id": "x"}) == []
    assert parse_decisions([{"rule_id": "x"}, "junk", 3]) == [{"rule_id": "x"}]
    assert rows_from_decision_entry({**LIVE_ENTRY, "decisions": "{not json"}) == []


def test_decision_without_rule_id_is_skipped():
    entry = dict(LIVE_ENTRY)
    entry["decisions"] = json.dumps([{"action": "REJECT", "reason": "无 id"}])
    assert rows_from_decision_entry(entry) == []


def test_evidence_non_mapping_becomes_empty_dict():
    entry = dict(LIVE_ENTRY)
    entry["decisions"] = json.dumps(
        [{"rule_id": "l1.leverage_cap", "action": "REJECT", "evidence": "oops"}]
    )
    rows = rows_from_decision_entry(entry)
    assert rows[0].evidence == {}


def test_quantity_dirty_value_becomes_none():
    entry = dict(LIVE_ENTRY)
    entry["qty"] = "九百股"
    entry["decisions"] = json.dumps([{"rule_id": "l1.leverage_cap", "action": "REJECT"}])
    assert rows_from_decision_entry(entry)[0].quantity is None


def test_reason_is_truncated():
    long_reason = "拦" * 500
    entry = dict(LIVE_ENTRY)
    entry["decisions"] = json.dumps(
        [{"rule_id": "l1.leverage_cap", "action": "REJECT", "reason": long_reason}]
    )
    assert len(rows_from_decision_entry(entry)[0].reason) == 200


# ── 成本口径 ────────────────────────────────────────────────────────
def test_cost_sign_convention():
    """cost = sign(side) × excess；cost>0 读作「这条规则花了钱」。"""
    # 买单被拦、标的涨了 → 错过收益 → 成本为正
    assert cost_of("buy", 0.03) == 0.03
    # 买单被拦、标的跌了 → 躲过下跌 → 成本为负（省钱）
    assert cost_of("buy", -0.03) == -0.03
    # 卖单被拦、标的跌了 → 仓位被套 → 成本为正
    assert cost_of("sell", -0.03) == 0.03
    # 卖单被拦、标的涨了 → 没卖反而赚了 → 成本为负
    assert cost_of("sell", 0.03) == -0.03
    # 大小写与空白
    assert cost_of(" BUY ", 0.1) == 0.1


def test_cost_of_none_stays_none():
    """不可得一律 None——绝不退化成 0（0 会被读成"这条规则不花钱"）。"""
    assert cost_of("buy", None) is None
    assert cost_of("buy", float("nan")) is None
    assert cost_of("buy", "abc") is None
    assert cost_of("hold", 0.03) is None
    assert cost_sign("hold") == 0
    assert cost_sign("") == 0


def test_priced_rows_excludes_structural_and_unknown_side():
    buy = rows_from_decision_entry(LIVE_ENTRY)[0]
    sell = GhostRow(**{**buy.__dict__, "side": "sell"})
    struct = GhostRow(**{**buy.__dict__, "rule_id": "l3.lot_size", "kind": KIND_STRUCTURAL})
    weird = GhostRow(**{**buy.__dict__, "side": "hold"})
    assert priced_rows([buy, sell]) == [buy, sell]
    assert priced_rows([struct, weird]) == []


# ── 定价回填 ────────────────────────────────────────────────────────
def test_priced_returns_new_object_and_marks_priced():
    row = rows_from_decision_entry(LIVE_ENTRY)[0]
    assert row.is_priced is False
    out = row.priced(
        entry_date="2026-09-24",
        entry_px=9.10,
        tradable=True,
        fwd={"t1": {"ret": 0.01, "bench": 0.002, "cost": 0.008}},
        priced_at="2026-09-25T15:30:00+08:00",
    )
    assert row.fwd is None, "原对象不被就地改写（不可变）"
    assert out.is_priced and out.entry_px == 9.10
    assert out.to_record()["fwd"]["t1"]["cost"] == 0.008


def test_to_record_carries_id_and_all_fields():
    row = rows_from_decision_entry(LIVE_ENTRY)[0]
    rec = row.to_record()
    assert rec["id"] == ghost_id(row)
    for key in (
        "date",
        "rule_id",
        "kind",
        "tenant",
        "uid",
        "symbol",
        "side",
        "quantity",
        "source",
        "reason",
        "evidence",
        "enforced",
        "version",
        "registered",
    ):
        assert key in rec, f"落库记录缺字段 {key}"


def test_summary_lines_reads_human():
    rows = extract_rows([LIVE_ENTRY, {**LIVE_ENTRY, "ts": "1790240000.000", "symbol": "SH600276"}])
    lines = summary_lines(rows)
    assert any("影子账行数" in line for line in lines)
    assert any("2026-09-23" in line for line in lines)


# ── 实测防线①：标的先归一（真留痕里两种写法都有）─────────────────────
def test_suffix_form_and_prefix_form_of_one_stock_are_the_same_row():
    """上游换个写法不能把同一笔单拆成两行——`ghost_id` 含标的，必须先归一。

    实测：845 条真留痕里 495 条后缀式、350 条前缀式，两种写法同时存在。
    """
    suf = rows_from_decision_entry({**LIVE_ENTRY, "symbol": "600036.SH"})[0]
    pre = rows_from_decision_entry({**LIVE_ENTRY, "symbol": "SH600036"})[0]
    assert suf.symbol == pre.symbol == "SH600036"
    assert ghost_id(suf) == ghost_id(pre), "两种写法必须落成同一行（幂等键含标的）"
    assert len(dedup_rows([suf, pre])) == 1


def test_lowercase_and_bare_digits_also_normalize():
    for raw in ("sh600036", "600036", " SH600036 "):
        assert rows_from_decision_entry({**LIVE_ENTRY, "symbol": raw})[0].symbol == "SH600036"


def test_non_ashare_symbols_are_left_alone():
    """`to_prefix` 认不出的（美股 Ticker 等）原样保留——不猜、不改写。"""
    from backend.shared.risk.ghost import normalize_symbol

    assert normalize_symbol("AAPL") == "AAPL"
    assert normalize_symbol("0700.HK") == "0700.HK"
    assert normalize_symbol("") == "" and normalize_symbol(None) == ""


# ── 实测防线②：测试租户不进账（每跑一次测试就灌一批假样本）──────────
def test_test_tenant_decisions_are_refused():
    """集成测试真往决策流里写，租户名随机 → 不拒收就是每条规则灌假样本。

    实测：845 条里 230 条属此类（`t-pending-life-*`）。
    """
    got = rows_from_decision_entry({**LIVE_ENTRY, "tenant": "t-pending-life-4e4040"})
    assert got == [], "测试租户的决策不是真单，计进代价就是假样本"


def test_all_known_test_tenant_prefixes_are_covered():
    from backend.shared.risk.ghost import TEST_TENANT_PREFIXES, is_test_tenant

    assert TEST_TENANT_PREFIXES, "反空洞：清单为空则下面每条都恒真"
    for p in TEST_TENANT_PREFIXES:
        assert is_test_tenant(f"{p}-abc123"), p
        assert is_test_tenant(p), p
    assert not is_test_tenant("default")
    assert not is_test_tenant("") and not is_test_tenant(None)


def test_extra_exclusions_can_be_added_by_env(monkeypatch):
    from backend.shared.risk.ghost import is_test_tenant

    assert not is_test_tenant("acme-shadow")
    monkeypatch.setenv("QM_GHOST_EXCLUDE_TENANTS", "acme-shadow, other")
    assert is_test_tenant("acme-shadow-1") and is_test_tenant("other-2")
    assert not is_test_tenant("default")


def test_real_tenant_rows_still_come_through():
    """拒收不能拒过头：真租户必须照常出数（否则是全表静默清空）。"""
    assert len(rows_from_decision_entry(LIVE_ENTRY)) == 1


# ── 旧键迁移（2026-09-23 加归一之前落库的行）────────────────────────
def _legacy_row(symbol: str, **kw) -> GhostRow:
    """库里的**旧行**：标的原样存着（没过归一）——迁移命令要处理的就是这种。

    不能借 `rows_from_decision_entry` 造：那个入口现在会归一，造出来的行
    恰恰是迁移的**结果**而不是输入。
    """
    base = {
        "date": "2026-09-18",
        "rule_id": "l1.position_cap",
        "kind": KIND_VETO,
        "tenant": "default",
        "uid": "10000001",
        "symbol": symbol,
        "side": "buy",
        "quantity": 900.0,
        "source": "tdx_l2",
        "reason": "集中度超限",
    }
    return GhostRow(**{**base, **kw})


def test_rekey_renames_a_lone_non_canonical_row():
    """没撞键 → 就地改名（**保住价**：计划里只有改名，没有删除）。"""
    r = _legacy_row("600036.SH", fwd={"t1": {"state": "ok", "cost": 0.01}}, entry_px=10.0)
    plan = plan_rekey([r])
    assert plan.doomed == () and plan.carried == ()
    assert plan.renames == ((ghost_id(r), ghost_id(replace(r, symbol="SH600036")), "SH600036"),)
    assert plan.n_changes == 1


def test_rekey_is_empty_when_every_symbol_is_already_canonical():
    plan = plan_rekey([_legacy_row("SH600036"), _legacy_row("SH600000")])
    assert plan.is_empty and plan.renames == () and plan.doomed == ()


def test_the_canonical_row_is_the_one_that_survives_a_key_collision():
    """同一笔单两种写法都在库里（实测 83 组）→ 规范的那行留下，旧写法的删掉。"""
    old, new = _legacy_row("600036.SH"), _legacy_row("SH600036")
    plan = plan_rekey([old, new])
    assert plan.doomed == (ghost_id(old),), "被删的必须是旧写法那行"
    assert plan.renames == (), "撞键时不该再有改名（新键已有人占）"
    assert plan.n_changes == 1


def test_order_does_not_change_who_survives():
    """库里读出来的顺序不该影响谁被删——否则清理结果不可复现。"""
    old, new = _legacy_row("600036.SH"), _legacy_row("SH600036")
    assert plan_rekey([new, old]).doomed == plan_rekey([old, new]).doomed


def test_pricing_of_the_deleted_twin_is_carried_over():
    """删之前先看价：被删的有价而保留者没有 → 补回去（价不能丢）。"""
    old = _legacy_row(
        "600036.SH", fwd={"t5": {"state": "ok", "cost": 0.02}}, entry_px=9.9
    )
    new = _legacy_row("SH600036")
    plan = plan_rekey([old, new])
    assert plan.doomed == (ghost_id(old),)
    assert len(plan.carried) == 1
    got = plan.carried[0]
    assert got.symbol == "SH600036", "补价要补在**保留者**身上"
    assert got.fwd == {"t5": {"state": "ok", "cost": 0.02}} and got.entry_px == 9.9


def test_nothing_is_carried_when_the_survivor_already_has_pricing():
    """保留者已有价就不动它——旧写法的价是同一笔单的同一个价，覆盖没有意义。"""
    old = _legacy_row("600036.SH", fwd={"t1": {"state": "ok", "cost": 0.9}})
    new = _legacy_row("SH600036", fwd={"t1": {"state": "ok", "cost": 0.01}})
    plan = plan_rekey([old, new])
    assert plan.carried == () and plan.doomed == (ghost_id(old),)


def test_two_non_canonical_rows_of_one_stock_collapse_to_one():
    """两种写法**都不是**规范写法时，先到的当保留者，后面的删——不能两条都改名。"""
    a, b = _legacy_row("600036.SH"), _legacy_row("sh600036")
    plan = plan_rekey([a, b])
    assert len(plan.renames) == 1 and len(plan.doomed) == 1
    assert plan.renames[0][1] == ghost_id(_legacy_row("SH600036"))


def test_rekey_never_plans_a_delete_of_a_canonical_row():
    """反空洞：迁移只许动非规范行，规范行的 id 必须一个都不出现在删除清单里。"""
    rows = [_legacy_row("SH600036"), _legacy_row("600036.SH"), _legacy_row("SH600000")]
    plan = plan_rekey(rows)
    canonical_ids = {ghost_id(r) for r in rows if normalize_symbol(r.symbol) == r.symbol}
    assert canonical_ids, "反空洞：规范 id 集为空的话下面这条恒真"
    assert not (set(plan.doomed) & canonical_ids)


def test_empty_exclusion_list_is_the_escape_hatch():
    """`exclude_tenants=()` 表示不拒收（CLI `--include-test-tenants` 走这条）。

    与 `None` 必须可区分：`None` 是"用默认清单"，`()` 是"什么都别排"。
    """
    e = {**LIVE_ENTRY, "tenant": "t-pending-life-4e4040"}
    assert rows_from_decision_entry(e, exclude_tenants=()) != []
    assert rows_from_decision_entry(e, exclude_tenants=None) == []
    assert extract_rows([e], exclude_tenants=()) != []
    assert extract_rows([e]) == []
