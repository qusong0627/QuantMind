"""风险档位层（`shared/risk/tiers.py`）：不变量测试。

本文件是隔壁 `quant-Trader/tests/test_risk_budget_freshness.py`（262 行，含两轮
机构级审计的结论）的移植 + 扩展。**每条失效姿态都必须有测试**——档位层的失效
形态是"静默按更松的参数跑"，那是全链路最难发现的失效（没有报错、没有留痕）。

四个方向，每个方向都要有测试钉死：

1. **判定**（decide_level）：风控输入缺失**不许**落到最松档；
2. **新鲜度**（parse/load）：档位不可信 → 买入侧回退收紧，杠杆键不放宽、
   也**不制造卖出**（数据故障不该触发强平）；
3. **防抖**（resolve_level）：同日只收紧一次，隔日按最新状态恢复；
4. **合并**（apply_to_rules）：与配置取"更严者"，只收紧不许放大。
"""

from __future__ import annotations

from datetime import date, timedelta, timezone

import pytest

from backend.shared.risk import tiers as T

CST = timezone(timedelta(hours=8))

WED = date(2026, 9, 9)  # 交易日（周三）
FRI = date(2026, 9, 11)  # 交易日（周五）
SAT = date(2026, 9, 12)  # 非交易日（周六）→ 应定档日 = 周五

#: 一份"谨慎"档的档位文档（五键齐全；字段名与隔壁 risk_budget.json 一致）
BUDGET = {
    "leverage_max": 1.2,
    "per_stock_pct": 0.15,
    "max_new_buys": 2,
    "leverage_trim_to": 1.15,
    "per_stock_pos_pct": 0.25,
}
FALLBACK = {"per_stock_pct": 0.10, "max_new_buys": 1, "per_stock_pos_pct": 0.15}
TIGHTENED = {**BUDGET, **FALLBACK}


def _doc(d: date, budget: dict | None = None, **extra) -> dict:
    return {
        "date": d.isoformat(),
        "level": "caution",
        "budget": dict(BUDGET if budget is None else budget),
        **extra,
    }


# ── 1. 判定（纯函数）─────────────────────────────────────────────────


def test_decide_level_all_calm_is_calm():
    # Arrange / Act
    level, reasons = T.decide_level(vol20=0.8, drawdown20=1.0, limit_up=50)

    # Assert
    assert level == "calm"
    assert reasons == []


def test_decide_level_missing_two_inputs_degrades_to_defensive():
    """fail-safe：风控输入缺失**不允许**落到最松档（2026-09-08 隔壁实测：
    波动/回撤取不到时被静默跳过 → 只剩情绪闸 → 数据故障反而放宽）。"""
    level, reasons = T.decide_level(vol20=None, drawdown20=None, limit_up=50)

    assert level == "defensive"
    assert any("风控数据缺失" in r for r in reasons)


def test_decide_level_missing_all_inputs_is_defensive_not_calm():
    level, reasons = T.decide_level(vol20=None, drawdown20=None, limit_up=None)

    assert level == "defensive"
    assert len(reasons) == 1 and "风控数据缺失" in reasons[0]


def test_decide_level_missing_one_input_is_at_least_caution():
    level, reasons = T.decide_level(vol20=None, drawdown20=1.0, limit_up=50)

    assert level == "caution"
    assert any("至少谨慎" in r for r in reasons)


def test_decide_level_deep_drawdown_forces_defensive_even_with_one_missing():
    level, reasons = T.decide_level(vol20=None, drawdown20=5.2, limit_up=50)

    assert level == "defensive"
    assert any("回撤" in r for r in reasons)


def test_decide_level_high_volatility_is_caution():
    level, reasons = T.decide_level(vol20=1.35, drawdown20=0.5, limit_up=50)

    assert level == "caution"
    assert any("波动" in r for r in reasons)


def test_decide_level_deepening_drawdown_is_caution():
    level, _ = T.decide_level(vol20=0.5, drawdown20=3.5, limit_up=50)

    assert level == "caution"


@pytest.mark.parametrize("zt", [24, 91])
def test_decide_level_sentiment_extremes_are_caution(zt):
    """情绪两头都要收紧：过冷（承接差）与过热（隔日炸板）。"""
    level, reasons = T.decide_level(vol20=0.5, drawdown20=0.5, limit_up=zt)

    assert level == "caution"
    assert any("情绪" in r for r in reasons)


@pytest.mark.parametrize("zt", [25, 90])
def test_decide_level_sentiment_thresholds_are_inclusive(zt):
    """阈值本身落在"平静"侧：25 家不算冷、90 家不算过热（边界可测）。"""
    level, _ = T.decide_level(vol20=0.5, drawdown20=0.5, limit_up=zt)

    assert level == "calm"


def test_levels_are_monotonically_tighter():
    """档位表本身的不变量：越严的档，每个键都 ≤ 上一档（只收紧）。

    这是"防抖"与"取更严者"能成立的前提——若某档的某个键反而更松，
    `resolve_level` 的"维持更严档"语义就会在那一维上失真。
    """
    calm, caution, defensive = (T.LEVELS[k] for k in ("calm", "caution", "defensive"))
    for key in T.LIMIT_KEYS:
        assert defensive[key] <= caution[key] <= calm[key], key


def test_levels_buy_side_never_looser_than_fallback():
    """回退参数必须 ≤ 每一档的买入侧值，否则"故障回退"在某个档上反而是放宽。"""
    for name, spec in T.LEVELS.items():
        for key, val in T.FALLBACK_LIMITS.items():
            assert val <= spec[key], f"{name}.{key}"


# ── 2. 防抖（同日只收紧一次）─────────────────────────────────────────


def test_resolve_level_same_day_keeps_tighter_level():
    level, note = T.resolve_level(
        computed="calm", prev_level="defensive", prev_date=WED.isoformat(), today=WED
    )

    assert level == "defensive"
    assert note and "防抖" in note


def test_resolve_level_same_day_allows_tightening():
    level, note = T.resolve_level(
        computed="defensive", prev_level="caution", prev_date=WED.isoformat(), today=WED
    )

    assert level == "defensive"
    assert note is None


def test_resolve_level_next_day_restores_by_latest_state():
    """隔日按最新状态恢复：状态回来了就该放松，防抖不是单向棘轮。"""
    level, note = T.resolve_level(
        computed="calm", prev_level="defensive", prev_date=WED.isoformat(), today=FRI
    )

    assert level == "calm"
    assert note is None


def test_resolve_level_without_history_takes_computed():
    level, note = T.resolve_level(
        computed="caution", prev_level=None, prev_date=None, today=WED
    )

    assert level == "caution"
    assert note is None


# ── 3. 应定档日与过期 ────────────────────────────────────────────────


def test_should_be_dated_day_on_trading_day_is_today():
    assert T.should_be_dated_day(WED) == WED


def test_should_be_dated_day_on_weekend_is_previous_friday():
    assert T.should_be_dated_day(SAT) == FRI


def test_should_be_dated_day_skips_holidays():
    """节假日（周四）→ 应定档日回退到周三。近似失败的方向必须是"更严"：
    宁可把新鲜档位判成过期（收紧），不可把过期档位判成新鲜（放宽）。"""
    thu = date(2026, 10, 1)

    assert T.should_be_dated_day(thu, holidays=(thu.isoformat(),)) == date(2026, 9, 30)


def test_tier_stale_reason_on_fresh_doc_is_empty():
    assert T.tier_stale_reason(_doc(WED), today=WED) == ""


def test_tier_stale_reason_friday_doc_on_saturday_is_fresh():
    """周末/假期沿用上一交易日档位：周五的档位对周六不算过期。"""
    assert T.tier_stale_reason(_doc(FRI), today=SAT) == ""


def test_tier_stale_reason_reports_yesterday_doc_today():
    reason = T.tier_stale_reason(_doc(WED - timedelta(days=1)), today=WED)

    assert "早于应定档日" in reason


def test_tier_stale_reason_reports_missing_date():
    reason = T.tier_stale_reason(
        {"level": "caution", "budget": dict(BUDGET)}, today=WED
    )

    assert "日期" in reason


# ── 4. 解析与失效姿态（parse_tier_doc）───────────────────────────────


def test_fresh_tier_doc_keeps_budget_verbatim():
    st = T.parse_tier_doc(_doc(WED), today=WED)

    assert st.level == "caution"
    assert dict(st.budget) == BUDGET
    assert st.source == "doc"
    assert st.problems == ()


def test_missing_tier_is_absent_not_a_fault():
    """**没有档位文档 ≠ 档位坏了**。从未启用档位层的实例（含全新建库）不该被
    静默套上防守档——那是把一次升级变成一次无人知情的收紧。故障姿态只适用于
    "配过、但读不到了"（见下一个测试）。"""
    st = T.parse_tier_doc(None, today=WED)

    assert st.source == "absent"
    assert dict(st.budget) == {}
    assert st.problems == ()


def test_stale_doc_tightens_buy_side_but_keeps_leverage():
    """过期不再沿用旧档位买入侧，而是回退收紧；杠杆/强减键取文档原值
    （不因数据故障放宽、也不制造强平——隔壁 2026-09-18 评审 H-1）。"""
    st = T.parse_tier_doc(_doc(WED - timedelta(days=3)), today=WED)

    assert dict(st.budget) == TIGHTENED
    assert st.budget["leverage_max"] == BUDGET["leverage_max"]
    assert st.source == "stale"
    assert any("早于应定档日" in p for p in st.problems)


def test_unknown_level_name_makes_whole_doc_untrusted():
    """写入侧档位名写错（`defensiv`）→ **整份文档不可信**，按 `fallback` 处理。

    初版实现只把名字改成"防守"、数值仍取文档原值——那是"标签说防守、参数是平静"
    （拼写错误 / 版本漂移时数值可能真属于另一档，读侧无从分辨）。故障只能朝更严的
    方向失败：买入侧回退到 FALLBACK，杠杆键取文档原值（不制造强平）。
    """
    st = T.parse_tier_doc(_doc(WED, level="defensiv"), today=WED)

    assert st.source == "fallback"
    assert dict(st.budget) == T.FALLBACK_LIMITS  # 与"JSON 坏"同一姿态：整份丢弃
    assert any("档位名不认识" in p for p in st.problems)
    # 不猜杠杆：杠杆上限交由配置侧那条规则独立兜底（1.0），不从不可信文档里捡
    assert "leverage_max" not in st.budget


def test_missing_date_field_is_stale():
    st = T.parse_tier_doc({"level": "caution", "budget": dict(BUDGET)}, today=WED)

    assert st.source == "stale"
    assert dict(st.budget) == TIGHTENED


def test_non_dict_doc_is_fallback():
    for bad in ([1, 2], 5, "oops"):
        st = T.parse_tier_doc(bad, today=WED)

        assert st.source == "fallback"
        assert dict(st.budget) == T.FALLBACK_LIMITS
        assert st.problems


def test_corrupt_budget_field_is_fallback():
    st = T.parse_tier_doc({"date": WED.isoformat(), "budget": "{不是 JSON"}, today=WED)

    assert st.source == "fallback"
    assert dict(st.budget) == T.FALLBACK_LIMITS


def test_non_dict_budget_field_is_fallback():
    st = T.parse_tier_doc({"date": WED.isoformat(), "budget": [1]}, today=WED)

    assert st.source == "fallback"
    assert dict(st.budget) == T.FALLBACK_LIMITS


def test_empty_budget_object_is_fallback():
    st = T.parse_tier_doc({"date": WED.isoformat(), "budget": {}}, today=WED)

    assert st.source == "fallback"
    assert any("可用" in p for p in st.problems)


def test_unparsable_values_are_dropped_and_reported():
    doc = _doc(WED, budget={**BUDGET, "leverage_max": "abc"})

    st = T.parse_tier_doc(doc, today=WED)

    assert (
        st.budget["leverage_max"] == T.LEVELS["caution"]["leverage_max"]
    )  # 回落到档位表
    assert any("leverage_max" in p for p in st.problems)


def test_missing_buy_side_key_filled_from_fallback():
    """写读键集漂移（新键已上线而写入侧还是旧版）→ 缺的**买入侧**键按防守补齐
    且必须留痕；缺的杠杆类键不猜（交给调用方默认）。"""
    batch = {k: v for k, v in BUDGET.items() if k != "per_stock_pos_pct"}
    st = T.parse_tier_doc(_doc(WED, budget=batch), today=WED)

    # 用 .get：缺键**就是**这里要断言的失败（写成 [] 会让失败变成 KeyError，
    # 探针无法区分"守卫生效"与"测试自己炸了"）
    assert st.budget.get("per_stock_pos_pct") == FALLBACK["per_stock_pos_pct"]
    assert st.budget.get("leverage_max") == BUDGET["leverage_max"]  # 杠杆键取文档值
    assert any("per_stock_pos_pct" in p for p in st.problems)


def test_fallback_limits_never_contain_leverage_keys():
    """钉死设计取舍：故障回退**只动买入侧**。

    强平是风险动作，不该由数据故障触发；且"压低 leverage_max 而 trim_to 保持
    原值"会组合出"减到仍超限"的强平循环（隔壁 2026-09-18 评审 H-1）。
    """
    assert "leverage_max" not in T.FALLBACK_LIMITS
    assert "leverage_trim_to" not in T.FALLBACK_LIMITS
    assert T.FALLBACK_LIMITS["per_stock_pct"] <= 0.10
    assert T.FALLBACK_LIMITS["max_new_buys"] <= 1


def test_max_new_buys_is_int_not_float():
    """键类型是契约：整数计数写成 1.0 会在下游 `int()`/比较时出现形态漂移。"""
    st = T.parse_tier_doc(_doc(WED), today=WED)

    assert isinstance(st.budget["max_new_buys"], int)


# ── 5. 合并进规则配置（apply_to_rules：只收紧）───────────────────────


def test_apply_absent_tier_leaves_rules_untouched():
    rules = {
        "l1.leverage_cap": {"max_leverage": 1.5},
        "l1.position_cap": {"max_pct": 0.2},
    }

    merged, applied, problems = T.apply_to_rules(
        rules, T.parse_tier_doc(None, today=WED)
    )

    assert merged == rules
    assert applied == {}
    assert problems == ()


def test_apply_tightens_rule_params():
    rules = {
        "l1.leverage_cap": {"max_leverage": 1.5},
        "l1.position_cap": {"max_pct": 0.30},
    }
    st = T.parse_tier_doc(
        _doc(WED, budget={**BUDGET, "leverage_max": 1.0, "per_stock_pos_pct": 0.15}),
        today=WED,
    )

    merged, applied, _ = T.apply_to_rules(rules, st)

    assert merged["l1.leverage_cap"]["max_leverage"] == 1.0
    assert merged["l1.position_cap"]["max_pct"] == 0.15
    # 五键全部有消费者（2026-09-23 批次 B 起）：没在配置里的买入侧规则也由档位启用
    assert applied == {
        "l1.leverage_cap": {"max_leverage": 1.0},
        "l1.position_cap": {"max_pct": 0.15},
        "l1.per_order_pct": {"max_pct": BUDGET["per_stock_pct"]},
        "l1.new_buys_per_day": {"max_new_buys": BUDGET["max_new_buys"]},
    }


def test_apply_never_loosens_configured_value():
    """配置比档位更严时，配置赢——档位是**上限**，不是赋值。"""
    rules = {"l1.leverage_cap": {"max_leverage": 0.5}}

    merged, applied, _ = T.apply_to_rules(rules, T.parse_tier_doc(_doc(WED), today=WED))

    assert merged["l1.leverage_cap"]["max_leverage"] == 0.5
    assert "l1.leverage_cap" not in applied  # 未被档位改动（不等于什么都没做：
    # 同一档位会**启用**未配置的买入侧规则，那是有意行为，见下一个测试）


def test_apply_preserves_other_params_of_touched_rule():
    rules = {"l1.leverage_cap": {"max_leverage": 1.5, "stale_warn_s": 120.0}}

    merged, _, _ = T.apply_to_rules(rules, T.parse_tier_doc(_doc(WED), today=WED))

    assert merged["l1.leverage_cap"]["max_leverage"] == 1.2
    assert merged["l1.leverage_cap"]["stale_warn_s"] == 120.0


def test_apply_does_not_mutate_input_rules():
    """不可变：返回新视图，绝不原地改调用方（Redis 里的原始配置）字典。"""
    rules = {"l1.leverage_cap": {"max_leverage": 1.5}}

    merged, _, _ = T.apply_to_rules(rules, T.parse_tier_doc(_doc(WED), today=WED))

    assert rules == {"l1.leverage_cap": {"max_leverage": 1.5}}
    assert merged is not rules


def test_apply_enables_disabled_buy_side_rule_with_tier_value():
    """档位对买入侧是**权威**：规则没在配置里启用时，档位给出的（只会更严的）
    参数要把规则启用起来。

    隔壁 2026-09-08 的实况教训：预算定档"防守"，但 09:35 主入口硬编码宽松档 →
    "风险预算只兑现了一半"。规则"没配置"不等于"可以绕过档位"。
    """
    rules: dict = {}

    merged, applied, _ = T.apply_to_rules(rules, T.parse_tier_doc(_doc(WED), today=WED))

    assert merged["l1.leverage_cap"]["max_leverage"] == BUDGET["leverage_max"]
    assert merged["l1.position_cap"]["max_pct"] == BUDGET["per_stock_pos_pct"]
    assert merged["l1.per_order_pct"]["max_pct"] == BUDGET["per_stock_pct"]
    assert merged["l1.new_buys_per_day"]["max_new_buys"] == BUDGET["max_new_buys"]
    # leverage_trim_to 无规则消费者（PENDING_KEYS）→ 不进 applied
    assert set(applied) == {
        "l1.leverage_cap",
        "l1.position_cap",
        "l1.per_order_pct",
        "l1.new_buys_per_day",
    }


def test_apply_stale_tier_applies_fallback_to_buy_side():
    rules = {"l1.leverage_cap": {"max_leverage": 1.5}}
    st = T.parse_tier_doc(_doc(WED - timedelta(days=3)), today=WED)

    merged, _applied, _ = T.apply_to_rules(rules, st)

    assert (
        merged["l1.leverage_cap"]["max_leverage"] == BUDGET["leverage_max"]
    )  # 取文档值
    assert (
        merged["l1.position_cap"]["max_pct"] == FALLBACK["per_stock_pos_pct"]
    )  # 买入侧回退


def test_unknown_budget_key_is_reported_at_boundary():
    """档位文档里的未知键（新写入方上线、读侧还没升）→ 在**解析边界**就报出来。

    静默丢弃的形态是：写入侧加了新键而读侧不认，档位看着"已生效"、实际那一维
    从没被约束过。另：未知键只上报，**不改** source——其余键仍然可用。
    """
    st = T.parse_tier_doc(_doc(WED, budget={**BUDGET, "unknown_knob": 0.5}), today=WED)

    assert any("unknown_knob" in p for p in st.problems)
    assert st.source == "doc"  # 未知键不污染其余键的可用性
    assert "unknown_knob" not in st.budget
    _, _, problems = T.apply_to_rules({}, st)
    assert any("unknown_knob" in p for p in problems)  # 也随档位态冒到调用方


def test_apply_pending_keys_are_not_reported():
    """`leverage_trim_to` 是**已知待接**（减仓执行器 P2.6 的输入）→ 不算异常。"""
    _, _, problems = T.apply_to_rules({}, T.parse_tier_doc(_doc(WED), today=WED))

    assert not any("leverage_trim_to" in p for p in problems)


# ── 6. IO 层（load_tier / save_tier，假 Redis）────────────────────────


class _FakeRedis:
    """最小 hash 语义替身（`hgetall`/`hset`/`expire`）。"""

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}

    def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    def hset(self, key: str, mapping: dict | None = None, **kw) -> int:
        h = self.hashes.setdefault(key, {})
        h.update({str(k): str(v) for k, v in (mapping or {}).items()})
        return 1

    def expire(self, key: str, ttl: int) -> bool:
        return True


def test_save_then_load_roundtrip():
    r = _FakeRedis()

    saved = T.save_tier(
        r,
        level="defensive",
        reasons=["回撤 5.2% 超限"],
        inputs={"drawdown20": 5.2},
        today=WED,
    )
    loaded = T.load_tier(r, today=WED)

    assert loaded.level == "defensive"
    assert dict(loaded.budget) == {k: T.LEVELS["defensive"][k] for k in T.LIMIT_KEYS}
    assert "label" not in loaded.budget  # 表意字段不进预算（那是 level/label 的事）
    assert loaded.source == "doc"
    assert saved.reasons == ("回撤 5.2% 超限",)


def test_save_writes_daily_detail():
    r = _FakeRedis()

    T.save_tier(r, level="caution", today=WED)

    assert T.TIER_DETAIL_KEY.format(date="20260909") in r.hashes


def test_save_rejects_unknown_level():
    r = _FakeRedis()

    with pytest.raises(ValueError):
        T.save_tier(r, level="yolo", today=WED)


def test_load_tier_on_empty_redis_is_absent():
    assert T.load_tier(_FakeRedis(), today=WED).source == "absent"


def test_load_tier_read_failure_is_fallback_not_raise():
    """读 Redis 失败（连接断/超时）→ 回退买入侧防守，**不许**抛出去。

    抛出去 = 每次判定都异常 = 闸门整体 fail-closed 拒掉一切买入；那会把一次
    Redis 抖动放大成"全线不能买"。档位读不到的正确姿态是**收紧**，不是停摆。
    """

    class _Boom(_FakeRedis):
        def hgetall(self, key: str):
            raise OSError("redis down")

    st = T.load_tier(_Boom(), today=WED)

    assert st.source == "fallback"
    assert dict(st.budget) == T.FALLBACK_LIMITS
    assert st.problems


def test_load_tier_survives_hash_written_by_older_version():
    """旧版写入方（无 `date` 字段）→ 过期判定失败 → 买入侧回退，不炸。"""
    r = _FakeRedis()
    r.hset(T.TIER_KEY, mapping={"level": "calm", "budget": '{"leverage_max": 1.5}'})

    st = T.load_tier(r, today=WED)

    assert st.source == "stale"
    assert st.budget.get("per_stock_pos_pct") == FALLBACK["per_stock_pos_pct"]


# ── 7. 档位键 → 规则参数映射（契约）───────────────────────────────────


def test_targets_only_point_at_registered_rules():
    """映射表指向的规则必须真在引擎里注册——否则档位"生效"了却没有消费者
    （正是本层要消灭的形态：看着已生效、实际没约束）。

    未注册的键必须在 `TARGETS_PENDING` 里**显式登记**；规则一旦注册就必须从
    该集合移除（下一条测试钉死这条自清理不变量，"待接"不许变成永久借口）。
    """
    from backend.shared.risk.registry import get_rule

    for key, (rule_id, _param, _direction) in T.TARGETS.items():
        if get_rule(rule_id) is None:
            assert key in T.TARGETS_PENDING, (
                f"{key} 指向未注册规则 {rule_id} 且未登记为待接"
            )
        else:
            assert key not in T.TARGETS_PENDING, (
                f"{key} 的规则已注册，应从 TARGETS_PENDING 移除"
            )


def test_pending_targets_self_clean():
    """待接集合与注册表必须互斥：注册即生效，不留"看着有映射其实没接"的存量。"""
    from backend.shared.risk.registry import get_rule

    for key in T.TARGETS_PENDING:
        rule_id = T.TARGETS[key][0]
        assert get_rule(rule_id) is None, (
            f"{rule_id} 已注册，{key} 应从 TARGETS_PENDING 移除"
        )


def test_apply_marks_unregistered_target_as_pending_not_silent(monkeypatch):
    """映射到未注册规则 → 记入 problems 以便可见，而不是无声消失。

    **不遍历真实的 `TARGETS_PENDING`**（批次 B 后它已清空，那样断言循环零次参与
    = 空转通过）：改为造一条指向不存在规则的映射，钉死"待接必须上报"这条行为本身。
    """
    monkeypatch.setitem(
        T.TARGETS, "per_stock_pct", ("l9.rule_that_does_not_exist", "max_pct", "lower")
    )
    st = T.parse_tier_doc(_doc(WED), today=WED)

    merged, applied, problems = T.apply_to_rules({}, st)

    assert any("per_stock_pct" in p and "待接" in p for p in problems)
    assert "l9.rule_that_does_not_exist" not in merged
    assert "l9.rule_that_does_not_exist" not in applied  # 未注册则不假装启用


def test_client_helper_does_not_mistake_redis_client_method_for_wrapper():
    """原生 `redis.Redis` 自带 `client()` 方法——不能被当包装拆掉。

    2026-09-23 定档首次真跑：调用方先把原生客户端交给本层时，裸
    `getattr(redis, "client", redis)` 返回方法对象，随后 `.hset` 抛
    `'function' object has no attribute 'hset'`。包装的 `.client` 是实例属性
    （不可调用），据此区分——本测试同时锁两个方向。
    """

    class _RawLike:
        def client(self):  # 与 redis-py 同名同形
            return self

    raw = _RawLike()
    assert T._client(raw) is raw

    class _Wrapper:
        def __init__(self, inner):
            self.client = inner

    inner = object()
    assert T._client(_Wrapper(inner)) is inner

    # 未连接（`.client is None`）也不下钻——回退到原对象，由调用方按"读失败"处理
    class _Unconnected:
        client = None

    unconnected = _Unconnected()
    assert T._client(unconnected) is unconnected
