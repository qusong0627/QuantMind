"""`watch` → 守护单映射（P2.1c）的行为与差分验收。

两层验证，缺一不可：

1. **分支级单测**：比例三态、无价位、去重、棘轮配对——每条都对着隔壁
   `scripts/live_price_watch.py:340-439` 的既有口径写期望值；
2. **真实语料差分**：把本仓 `plan_watch` 与隔壁那段逻辑的**逐字转写**跑在同一批
   真实 LLM 输出上，要求「挂没挂上、挂成什么价」完全一致。

第 2 条是这一层的主要证据：全量语料（655 条真实输出 / 1136 条 watch 行）实测
**两边各挂 291 条、双向差集均为 0**。那批语料是私有生产数据、不进公开仓库，
故这里用已入库的 7 条金样跑同一套差分逻辑，守住回归；全量结论记在
`docs/local/quant-trader-migration-plan.md` 的 P2.1c 记录里。
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from backend.services.live_trading.services.sltp_executor import (
    DEFAULT_RULE,
    normalize_rule,
    normalize_symbol,
    rule_reject_reason,
)
from backend.shared.decision.contract import (
    SCHEMA_INTRADAY,
    Decision,
    parse_decisions,
)
from backend.shared.decision.watch_map import (
    FULL_REDUCE_PCT,
    NOTE_PCT_CLAMPED,
    NOTE_PCT_DIRTY,
    NOTE_PCT_MISSING,
    REJECT_DUPLICATE,
    REJECT_NO_CODE,
    REJECT_NO_LEVEL,
    REJECT_PCT_ZERO,
    WatchPlan,
    plan_watch,
)

FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "decision_corpus_real.jsonl"
)


def _rows(*rows: dict) -> list[Decision]:
    """若干行 JSON → Decision 列表（**过真解析器**，不手工构造，免得绕过契约）。"""
    payload = json.dumps({"decisions": list(rows)}, ensure_ascii=False)
    batch = parse_decisions(payload, schema=SCHEMA_INTRADAY)
    assert batch.ok, f"测试语料没解析通过：{batch.status} {batch.raw[:120]}"
    return list(batch.decisions)


def _watch(code: str = "600519.SH", **kw) -> dict:
    return {"action": "watch", "code": code, **kw}


class TestEligibility:
    def test_non_watch_actions_produce_nothing(self) -> None:
        plan = plan_watch(
            _rows(
                {"action": "hold", "code": "600519.SH"},
                {"action": "sell", "code": "600519.SH", "pct": 0.5},
                {"action": "buy", "code": "600519.SH", "pct": 0.1},
            )
        )
        assert plan.rules == ()
        assert plan.rejected == ()

    def test_watch_without_code_is_rejected(self) -> None:
        plan = plan_watch([Decision(action="watch", code="", stop_loss=10.0)])
        assert plan.rules == ()
        assert plan.rejected[0].reason == REJECT_NO_CODE

    def test_watch_without_any_level_is_rejected(self) -> None:
        """既有口径：没有价位的条件位没有意义（只见棘轮也不挂）。"""
        plan = plan_watch(_rows(_watch(move_stop=105.0)))
        assert plan.rules == ()
        assert plan.rejected[0].reason == REJECT_NO_LEVEL

    @pytest.mark.parametrize("field", ["stop_loss", "take_profit"])
    def test_either_level_alone_is_enough(self, field: str) -> None:
        plan = plan_watch(_rows(_watch(**{field: 100.0})))
        assert len(plan.rules) == 1


class TestReducePctThreeStates:
    def test_given_pct_is_used(self) -> None:
        plan = plan_watch(_rows(_watch(stop_loss=1500.0, pct=0.3)))
        assert plan.rules[0].rule["reduce_pct"] == 0.3
        assert plan.rules[0].notes == ()

    @pytest.mark.parametrize(
        ("pct", "note"),
        [
            (None, NOTE_PCT_MISSING),
            ("0.3股", NOTE_PCT_DIRTY),
            ("三成", NOTE_PCT_DIRTY),
            (True, NOTE_PCT_DIRTY),
        ],
    )
    def test_missing_or_dirty_falls_back_to_full(self, pct: object, note: str) -> None:
        """漏挂 = 当日裸奔，故缺/脏都按**全仓**挂，且必须留注记（不静默）。"""
        row = _watch(stop_loss=1500.0)
        if pct is not None:
            row["pct"] = pct
        plan = plan_watch(_rows(row))
        assert plan.rules[0].rule["reduce_pct"] == FULL_REDUCE_PCT
        assert plan.rules[0].notes == (note,)

    def test_percent_string_is_read_not_treated_as_dirty(self) -> None:
        """``"30%"`` 有明确语义（契约层已归一成 0.3）——别把它归到脏值。"""
        plan = plan_watch(_rows(_watch(stop_loss=1500.0, pct="30%")))
        assert plan.rules[0].rule["reduce_pct"] == 0.3
        assert plan.rules[0].notes == ()

    @pytest.mark.parametrize("pct", [0, -0.2])
    def test_explicit_zero_or_negative_is_not_armed(self, pct: float) -> None:
        """模型**明说**不表达卖出量 → 不挂。替它按全仓挂就是凭空多卖。"""
        plan = plan_watch(_rows(_watch(stop_loss=1500.0, pct=pct)))
        assert plan.rules == ()
        assert plan.rejected[0].reason == REJECT_PCT_ZERO

    def test_pct_above_one_is_clamped_to_full_with_note(self) -> None:
        """``pct=1.5`` 疑似百分数笔误 → 按全仓 + 注记（既不放大也不静默）。"""
        plan = plan_watch(_rows(_watch(stop_loss=1500.0, pct=1.5)))
        assert plan.rules[0].rule["reduce_pct"] == FULL_REDUCE_PCT
        assert plan.rules[0].notes == (NOTE_PCT_CLAMPED,)


class TestRuleShape:
    def _rule(self, **kw) -> dict:
        return plan_watch(_rows(_watch(**kw))).rules[0].rule

    def test_keys_stay_inside_the_executor_vocabulary(self) -> None:
        """**关键不变量**：`normalize_rule` 会静默丢掉词表外的键，多写的字段等于没写。"""
        rule = self._rule(stop_loss=10.0, take_profit=12.0, move_stop=11.0, pct=0.5)
        assert set(rule) <= set(DEFAULT_RULE)

    def test_levels_land_on_price_fields(self) -> None:
        rule = self._rule(stop_loss=10.0, take_profit=12.0)
        assert rule["stop_loss_price"] == 10.0
        assert rule["take_profit_price"] == 12.0
        assert rule["stop_loss_pct"] is None and rule["take_profit_pct"] is None

    def test_move_stop_becomes_a_zero_gap_ratchet(self) -> None:
        """单值棘轮 = 触发价与目标价同值（隔壁 49.3% 的决策都是这一形态）。"""
        rule = self._rule(stop_loss=10.0, move_stop=11.0)
        assert rule["move_stop_trigger"] == 11.0
        assert rule["move_stop_to"] == 11.0

    def test_symbol_is_normalised_to_suffix_form(self) -> None:
        assert self._rule(stop_loss=10.0)["symbol"] == "600519.SH"
        plan = plan_watch(_rows(_watch(code="SH600519", stop_loss=10.0)))
        assert plan.rules[0].symbol == "600519.SH"
        assert plan.rules[0].rule["symbol"] == "600519.SH"

    def test_rule_survives_executor_normalisation_unchanged(self) -> None:
        """规则要能**原样**通过执行器的清洗：任何字段被改写都说明词表对不上。"""
        rule = self._rule(stop_loss=10.0, take_profit=12.0, move_stop=11.0, pct=0.5)
        assert normalize_rule(rule) == rule

    def test_armed_rule_always_passes_the_single_source_validation(self) -> None:
        plan = plan_watch(_rows(_watch(stop_loss=10.0, take_profit=12.0, pct=0.5)))
        assert rule_reject_reason(plan.rules[0].rule) == ""


class TestDedup:
    def test_identical_rows_collapse_to_one(self) -> None:
        plan = plan_watch(
            _rows(_watch(stop_loss=10.0, pct=0.5), _watch(stop_loss=10.0, pct=0.5))
        )
        assert len(plan.rules) == 1
        assert plan.rejected[0].reason == REJECT_DUPLICATE

    def test_same_code_different_levels_are_both_kept(self) -> None:
        """两个不同价位是两个意图——去掉哪个都是替模型做决定。"""
        plan = plan_watch(
            _rows(_watch(stop_loss=10.0, pct=0.5), _watch(stop_loss=9.0, pct=0.5))
        )
        assert len(plan.rules) == 2
        assert plan.rejected == ()

    def test_same_levels_different_pct_are_both_kept(self) -> None:
        plan = plan_watch(
            _rows(_watch(stop_loss=10.0, pct=0.5), _watch(stop_loss=10.0, pct=0.3))
        )
        assert len(plan.rules) == 2

    def test_levels_compared_at_three_decimals(self) -> None:
        """10.0 与 10.0004 归一到三位小数后同价 → 去重（既有 `_lvl` 口径）。"""
        plan = plan_watch(
            _rows(_watch(stop_loss=10.0, pct=0.5), _watch(stop_loss=10.0004, pct=0.5))
        )
        assert len(plan.rules) == 1

    def test_symbol_forms_are_deduped_after_normalisation(self) -> None:
        plan = plan_watch(
            _rows(_watch(code="SH600519", stop_loss=10.0), _watch(stop_loss=10.0))
        )
        assert len(plan.rules) == 1


class TestLedgerFields:
    def test_invalidation_and_risk_are_carried(self) -> None:
        """**这层修掉的就是这里**：隔壁把 invalidation 整条丢掉了。"""
        plan = plan_watch(
            _rows(
                _watch(
                    stop_loss=10.0,
                    invalidation="放量跌破 10 就不再等 12",
                    risk_amount=21600.0,
                    confidence=0.7,
                    reason="支撑位",
                )
            )
        )
        w = plan.rules[0]
        assert w.invalidation == "放量跌破 10 就不再等 12"
        assert w.risk_amount == 21600.0
        assert w.confidence == 0.7
        assert w.reason == "支撑位"

    def test_index_is_the_position_in_the_batch_and_is_unique(self) -> None:
        plan = plan_watch(
            _rows(
                {"action": "hold", "code": "600000.SH"},
                _watch(code="600519.SH", stop_loss=10.0),
                _watch(code="000001.SZ", stop_loss=11.0),
            )
        )
        assert [w.index for w in plan.rules] == [1, 2]

    def test_agent_is_stamped_on_plan_and_rules(self) -> None:
        plan = plan_watch(_rows(_watch(stop_loss=10.0)), agent="deepseek-v4-pro")
        assert plan.agent == "deepseek-v4-pro"
        assert plan.rules[0].agent == "deepseek-v4-pro"

    def test_each_rule_gets_its_own_dict(self) -> None:
        """两行同标的规则不能共用同一个 dict——改一条会串到另一条。"""
        plan = plan_watch(
            _rows(_watch(stop_loss=10.0, pct=0.5), _watch(stop_loss=9.0, pct=0.5))
        )
        assert plan.rules[0].rule is not plan.rules[1].rule


def test_every_watch_decision_lands_somewhere() -> None:
    """不丢行：每条 watch 决策必须落进 rules 或 rejected 之一。

    静默少挂一个守护位 = 那只票当日裸奔，而且事后没人知道少的是哪条。
    """
    rows = _rows(
        _watch(code="600519.SH", stop_loss=1500.0, pct=0.3),
        _watch(code="600519.SH", stop_loss=1500.0, pct=0.3),  # 重复
        _watch(code="000001.SZ", pct=0.5),  # 无价位
        _watch(code="600000.SH", stop_loss=10.0, pct=0),  # 明说 0
        {"action": "hold", "code": "300750.SZ"},
    )
    plan = plan_watch(rows)
    n_watch = sum(1 for r in rows if r.is_watch)
    assert len(plan.rules) + len(plan.rejected) == n_watch


# ── 真实语料差分 ──────────────────────────────────────────────────────
#: 隔壁 `scripts/live_price_watch.py:340-361` 的逐字转写（`_pct` / `_lvl`）
def _old_pct(v: object, default: float = 1.0) -> float:
    try:
        f = min(max(float(v), 0.0), 1.0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def _old_lvl(v: object) -> float | None:
    try:
        return round(float(v), 3)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _old_rules(rows: list[dict]) -> list[tuple]:
    """隔壁 `live_price_watch.py:363-440` 的转写 → 「挂出来的东西」的集合。

    转写而不是调用：隔壁那支模块 import 一大串桥/行情依赖，本仓测试起不来；这里
    要比的只是这段纯逻辑。差异**只允许出现在本仓新增的能力上**（见 `_DIFF_NOTES`），
    挂没挂上、挂成什么价必须逐条一致。
    """
    mine, seen = [], set()
    for d in rows:
        if d.get("action") != "watch":
            continue
        code = str(d.get("code") or "").strip()
        if not code or (d.get("stop_loss") is None and d.get("take_profit") is None):
            continue
        pct = _old_pct(d.get("pct"))
        if pct <= 0:
            if d.get("pct_given", True):
                continue
            pct = 1.0
        sig = (code, _old_lvl(d.get("stop_loss")), _old_lvl(d.get("take_profit")), pct)
        if sig in seen:
            continue
        seen.add(sig)
        mine.append(
            (normalize_symbol(code), d.get("stop_loss"), d.get("take_profit"), pct)
        )
    return mine


def _to_old_dict(d: Decision) -> dict:
    """本仓 Decision → 隔壁解析器产出的字典形状。

    ``pct_given = (state == given)``（`live_prompt_context.py:202`），且隔壁把
    缺/脏都压成 ``pct=0.0``——两者都按 ``pct_given=False`` 走「全仓」分支。
    """
    p = d.pct
    raw, given = (p.value, True) if p.is_given else (0.0, False)
    return {
        "action": d.action,
        "code": d.code,
        "pct": raw,
        "pct_given": given,
        "stop_loss": d.stop_loss,
        "take_profit": d.take_profit,
        "move_stop": d.move_stop,
    }


def _armed_by_us(plan: WatchPlan) -> list[tuple]:
    return [
        (
            w.symbol,
            w.rule["stop_loss_price"],
            w.rule["take_profit_price"],
            w.rule["reduce_pct"],
        )
        for w in plan.rules
    ]


def _corpus_samples() -> list[dict]:
    out = []
    for line in FIXTURE_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    assert out, "语料 fixture 空了——差分就没在跑"
    return out


def test_corpus_is_not_empty_and_has_watch_rows() -> None:
    """非空守卫：语料被清空 / 一条 watch 都没有时，下面的差分是**空转通过**。"""
    samples = _corpus_samples()
    n_watch = 0
    for s in samples:
        batch = parse_decisions(s["text"], schema=SCHEMA_INTRADAY)
        n_watch += sum(1 for d in batch.decisions if d.is_watch)
    assert n_watch >= 2, f"语料里 watch 行只剩 {n_watch} 条，差分失去意义"


def test_arming_matches_the_ported_implementation_on_real_corpus() -> None:
    """真实输出上逐条对齐：挂没挂上、挂成什么价。"""
    for s in _corpus_samples():
        batch = parse_decisions(s["text"], schema=SCHEMA_INTRADAY)
        rows = list(batch.decisions)
        mine = sorted(_armed_by_us(plan_watch(rows)), key=str)
        old = sorted(_old_rules([_to_old_dict(d) for d in rows]), key=str)
        assert mine == old, f"{s['id']}：与隔壁口径不一致\n本仓={mine}\n隔壁={old}"


def test_corpus_rules_keep_the_invalidation_the_old_system_dropped() -> None:
    """隔壁丢掉 invalidation 的那一条，本仓必须带上（有就带，没有不编）。"""
    for s in _corpus_samples():
        batch = parse_decisions(s["text"], schema=SCHEMA_INTRADAY)
        plan = plan_watch(list(batch.decisions))
        for w in plan.rules:
            source = [d for d in batch.decisions if d.is_watch and d.code == w.symbol]
            expected = next((d.invalidation for d in source if d.invalidation), "")
            if expected:
                assert w.invalidation == expected, f"{s['id']}：invalidation 丢了"
