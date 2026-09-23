"""P2.4：``watch`` → 止盈止损规则表的 IO 适配层（``decision.watch_writer``）。

纯核心 ``plan_watch`` 的用例在 ``test_watch_map.py``；这里只钉**写入这一侧**：

1. **归属**：``owner`` 必须在执行器词表里（表外的键会被 ``normalize_rule`` 静默丢掉），
   ``""`` = 人工、``llm:<agent>`` = 决策层一组，空 agent 一律拒写；
2. **整组替换**：只动自己那一组，人工的与别家的规则一条不碰；
3. **冲突**：同标的人工优先 / 别家先到先得 / 本轮同标的多条只挂第一条——三者都
   必须**可见地被拒**（静默少挂一条 = 当日以为有人看着）；
4. **fail-closed**：读不到不写、写被吞要回读发现、超上限一条不写；
   ``RedisClient.set`` 吞异常时失败会以「回读没确认到」出现，故 ``ok`` 不能只看 ``errors``；
5. **状态清理**：只清「槽位已空」的标的，在途真单不清，重挂不清（要走 /reset）。

全部用例不碰真 Redis：替身按需模拟「读抛错」「写抛错」「**写被静默丢弃**」三种真事故。
"""

from __future__ import annotations

from typing import Any

import pytest

from backend.services.live_trading.services import sltp_executor as ex
from backend.shared.decision import watch_writer as ww
from backend.shared.decision.watch_map import WatchPlan, WatchRule

DAY = ex.trade_date_str()
SYM = "600036.SH"
OTHER = "600519.SH"


# ---------------------------------------------------------------------------
# 替身
# ---------------------------------------------------------------------------
class FakeRedis:
    """字典式 Redis 替身 + 三个故障开关（真事故的三种样子）。

    * ``raise_get``：读抛错（连接断了）；
    * ``raise_set``：写抛错；
    * ``drop_writes``：**写被静默丢弃**——调用返回成功但什么都没落库。这正是
      ``RedisClient.set`` 的真实行为，也是「写后回读」这条防线唯一能抓到的那种。
    """

    def __init__(self, cfg: dict | None = None, state: dict | None = None) -> None:
        self.store: dict[str, Any] = {}
        if cfg is not None:
            self.store[ex.CONFIG_KEY] = cfg
        if state is not None:
            self.store[ex.STATE_KEY] = state
        self.raise_get = False
        self.raise_set = False
        self.drop_writes = False
        #: 键 → 从第 N 次读**该键**起抛错（用来单独演「回读那一次失败」「状态读失败」）
        self.fail_get_keys: dict[str, int] = {}
        self._per_key_reads: dict[str, int] = {}
        #: 落库时改写配置的钩子（演「他方规则被改/被删」这类并发写）
        self.mutate_on_set = None
        self.get_calls = 0
        self.set_calls: list[tuple[str, Any]] = []

    def get(self, key: str):
        self.get_calls += 1
        self._per_key_reads[key] = self._per_key_reads.get(key, 0) + 1
        fail_from = self.fail_get_keys.get(key)
        if self.raise_get or (
            fail_from is not None and self._per_key_reads[key] >= fail_from
        ):
            raise RuntimeError("Redis 读失败")
        return self.store.get(key)

    def set(self, key: str, value) -> None:
        self.set_calls.append((key, value))
        if self.raise_set:
            raise RuntimeError("Redis 写失败")
        if self.drop_writes:
            return
        payload = self.mutate_on_set(value) if self.mutate_on_set else value
        self.store[key] = payload


def rule(
    symbol: str = SYM,
    *,
    owner: str = "",
    stop: float = 9.0,
    take: float = 11.0,
    **extra: Any,
) -> dict[str, Any]:
    out = dict(ex.DEFAULT_RULE)
    out.update(
        {
            "symbol": symbol,
            "enabled": True,
            "side": "SELL",
            "stop_loss_price": stop,
            "take_profit_price": take,
            "owner": owner,
        }
    )
    out.update(extra)
    return out


def watch(
    symbol: str = SYM,
    *,
    index: int = 0,
    stop: float = 9.0,
    notes: tuple[str, ...] = (),
) -> WatchRule:
    return WatchRule(
        symbol=symbol,
        rule=rule(symbol, stop=stop),
        index=index,
        notes=notes,
    )


def plan(*rules: WatchRule, notes: tuple[str, ...] = ()) -> WatchPlan:
    return WatchPlan(rules=tuple(rules), agent="tft", notes=notes)


def cfg(*rules: dict[str, Any], enabled: bool = True) -> dict[str, Any]:
    return {**ex.DEFAULT_CONFIG, "enabled": enabled, "rules": list(rules)}


def table(redis: FakeRedis) -> list[dict[str, Any]]:
    return list((redis.store.get(ex.CONFIG_KEY) or {}).get("rules") or [])


def symbols_in(redis: FakeRedis) -> set[str]:
    return {r["symbol"] for r in table(redis)}


# ---------------------------------------------------------------------------
# 1. 归属：词表、构造、控制面契约
# ---------------------------------------------------------------------------
def test_owner_must_be_in_the_executor_vocabulary() -> None:
    """``normalize_rule`` 只认 ``DEFAULT_RULE`` 词表里的键，表外的键**静默丢弃**。

    归属要活过清洗只有一条路：进词表。这条用例钉的就是这个前提——哪天有人把
    ``owner`` 从 ``DEFAULT_RULE`` 里删掉，所有规则会悄悄变成人工规则。
    """
    assert "owner" in ex.DEFAULT_RULE
    assert ex.normalize_rule({"symbol": SYM, "owner": "llm:tft"})["owner"] == "llm:tft"
    # 归属一律成串：Redis 里手改过 / 老版本写过的 null 与数字都要归一
    assert ex.normalize_rule({"symbol": SYM, "owner": None})["owner"] == ""
    assert (
        ex.normalize_rule({"symbol": SYM, "owner": "  llm:tft  "})["owner"] == "llm:tft"
    )
    # 表外的键（比如 watch_map 的 invalidation）进不来——它落库位在决策审计表
    normalized = ex.normalize_rule({"symbol": SYM, "invalidation": "跌回 8.5 就错了"})
    assert "invalidation" not in normalized


def test_llm_owner_refuses_to_build_a_bare_prefix() -> None:
    """空 agent **不构造**归属：裸前缀 ``llm:`` 会让所有无名写者共用一组，互删。"""
    assert ex.llm_owner("") == ex.OWNER_MANUAL
    assert ex.llm_owner("   ") == ex.OWNER_MANUAL
    assert ex.llm_owner(" tft ") == "llm:tft"
    assert ex.llm_owner("tft") != ex.OWNER_LLM_PREFIX  # 裸前缀不是任何 agent 的组


def test_control_plane_rejects_the_bare_owner_prefix() -> None:
    """控制面 PUT 的契约与执行器同源：裸 ``llm:`` 在 API 层就被拒（422）。

    ``MAX_RULES`` 也必须单源——API 能存下的表决策层写不进去，等于每轮都撞上限。
    """
    from backend.services.trade.routers import qmt_sltp as api

    assert api.MAX_RULES == ex.MAX_RULES
    assert api.SltpConfigUpdate.model_fields["rules"].metadata[-1].max_length == (
        ex.MAX_RULES
    )
    base = {"symbol": SYM, "stop_loss_price": 9.0}
    assert api.SltpRule(**base, owner="llm:tft").owner == "llm:tft"
    assert api.SltpRule(**base, owner="").owner == ""
    with pytest.raises(ValueError):
        api.SltpRule(**base, owner=ex.OWNER_LLM_PREFIX)
    with pytest.raises(ValueError):
        api.SltpRule(**base, owner="llm:" + "a" * 100)  # 超长


# ---------------------------------------------------------------------------
# 2. 严格读 / 写后回读（既有防线里被 RedisClient 吞掉的那两处）
# ---------------------------------------------------------------------------
def test_strict_read_raises_where_load_config_would_fake_an_empty_table() -> None:
    """读失败：``load_config`` 回落默认（显示口径），``read_config_strict`` 抛错（写侧口径）。

    这一条是整个 P2.4 的根：``RedisClient.get`` 把读失败吞成 ``None``，写侧若用
    ``load_config``，「Redis 抖了一下」就变成「表是空的」，整表写回 = 把所有规则
    （含人工挂的）一把抹掉。
    """
    redis = FakeRedis(cfg(SYM and rule(SYM, owner="llm:tft")))
    redis.raise_get = True
    assert ex.load_config(redis)["rules"] == []  # 显示口径：这一拍空转
    with pytest.raises(RuntimeError):
        ex.read_config_strict(redis)


def test_strict_write_reads_back_and_refuses_a_silently_dropped_write() -> None:
    """写被静默吞掉 → ``save_config_strict`` 抛错（不靠返回值自证）。"""
    redis = FakeRedis(cfg(rule(SYM)))
    redis.drop_writes = True
    with pytest.raises(RuntimeError, match="写入未生效"):
        ex.save_config_strict(redis, cfg(rule(SYM), rule(OTHER)))


def test_set_enabled_never_overwrites_the_table_when_the_read_fails() -> None:
    """总开关那一按也不能把规则表抹掉：读失败直接抛（API 层据此回 503）。"""
    redis = FakeRedis(cfg(rule(SYM, owner="llm:tft")))
    redis.raise_get = True
    with pytest.raises(RuntimeError):
        ex.set_enabled(redis, False)
    assert redis.set_calls == []  # 一个字节都没写


# ---------------------------------------------------------------------------
# 3. 整组替换：只动自己那一组
# ---------------------------------------------------------------------------
def test_replaces_only_its_own_group() -> None:
    """``owner == 我`` 的整组换掉，人工的与别家的一条不碰。"""
    human = rule(OTHER)
    other_agent = rule("000001.SZ", owner="llm:lgbm")
    redis = FakeRedis(cfg(rule(SYM, owner="llm:tft"), human, other_agent))

    res = ww.write_watch_plan(redis, plan(watch(OTHER and "600036.SH")), agent="tft")

    assert res.armed == (SYM,)
    assert res.removed == ()  # 同标的的重挂不算「摘除」：状态要接着用（见下条用例）
    assert symbols_in(redis) == {SYM, OTHER, "000001.SZ"}
    assert {r["symbol"]: r["owner"] for r in table(redis)} == {
        SYM: "llm:tft",
        OTHER: "",
        "000001.SZ": "llm:lgbm",
    }
    assert res.ok


def test_an_empty_plan_clears_the_group_but_nothing_else() -> None:
    """本轮没 watch = 组内清空（「最新分析说了算」），人工/别家原样。"""
    redis = FakeRedis(
        cfg(rule(SYM, owner="llm:tft"), rule(OTHER, owner="llm:tft"), rule("000001.SZ"))
    )
    res = ww.write_watch_plan(redis, plan(), agent="tft")
    assert res.armed == ()
    assert symbols_in(redis) == {"000001.SZ"}
    assert res.ok


def test_rewriting_the_same_symbol_is_not_a_removal() -> None:
    """同标的同组重挂：新规则顶替旧的，**不算**「摘除」（状态也不清）。"""
    redis = FakeRedis(cfg(rule(SYM, owner="llm:tft", stop=9.0)))
    res = ww.write_watch_plan(redis, plan(watch(SYM, stop=9.5)), agent="tft")
    assert res.armed == (SYM,)
    assert res.removed == ()
    assert [r["stop_loss_price"] for r in table(redis)] == [9.5]


# ---------------------------------------------------------------------------
# 4. 冲突：人工优先 / 别家先到先得 / 同标的多条
# ---------------------------------------------------------------------------
def test_a_manual_rule_wins_and_the_conflict_is_recorded() -> None:
    """人工优先：本轮 watch **不覆盖**人工规则，改判「未挂上」并写明怎么交还。"""
    manual = rule(SYM, stop=8.0)
    redis = FakeRedis(cfg(manual))

    res = ww.write_watch_plan(redis, plan(watch(SYM, stop=9.5)), agent="tft")

    assert res.armed == ()
    (conflict,) = res.conflicts
    assert conflict.symbol == SYM and conflict.holder == ""
    assert conflict.reason == ww.REJECT_MANUAL_HOLD
    assert [r["stop_loss_price"] for r in table(redis)] == [8.0]  # 人工那条一字未改
    # ``ok`` 说的是**写入这一层干成没干成**：人工优先是它按既定规则做的判断，不是写失败
    # （「有一条没挂上」看 conflicts / outcomes——两处都有，且带具体理由）
    assert res.ok
    assert any("未挂上" in n for n in res.notes)
    assert res.outcomes()[0]["armed"] is False
    assert res.outcomes()[0]["reject_reason"] == ww.REJECT_MANUAL_HOLD


def test_another_agents_rule_holds_the_slot() -> None:
    """别家 agent 先挂的持有：本执行器一个标的只有一个状态位（仲裁是 P2.7 的事）。"""
    redis = FakeRedis(cfg(rule(SYM, owner="llm:lgbm")))
    res = ww.write_watch_plan(redis, plan(watch(SYM)), agent="tft")
    (conflict,) = res.conflicts
    assert conflict.holder == "llm:lgbm"
    assert conflict.reason == ww.REJECT_OTHER_AGENT.format(holder="llm:lgbm")
    assert [r["owner"] for r in table(redis)] == ["llm:lgbm"]


def test_two_watches_on_one_symbol_arm_only_the_first() -> None:
    """同标的两条共用同一个状态位、第二条永不触发——故明确拒绝第二条（先到先得）。"""
    redis = FakeRedis(cfg())
    res = ww.write_watch_plan(
        redis,
        plan(watch(SYM, index=3, stop=9.0), watch(SYM, index=7, stop=8.0)),
        agent="tft",
    )
    assert res.armed == (SYM,)
    (conflict,) = res.conflicts
    assert conflict.index == 7
    assert conflict.reason == ww.REJECT_SAME_SYMBOL.format(first=3)
    assert [r["stop_loss_price"] for r in table(redis)] == [9.0]
    out = res.outcomes()
    assert out[3]["armed"] is True  # 先到的那条照挂
    assert out[7]["armed"] is False
    assert out[7]["reject_reason"] == ww.REJECT_SAME_SYMBOL.format(first=3)


# ---------------------------------------------------------------------------
# 5. fail-closed：读不到不写 / 空 agent 拒写 / 超上限一条不写
# ---------------------------------------------------------------------------
def test_a_failed_read_writes_nothing_at_all() -> None:
    """读不到 —— **一个字节都不写**（写回一份「我刚编出来的表」= 抹掉全部规则）。"""
    redis = FakeRedis(cfg(rule(SYM), rule(OTHER)))
    redis.raise_get = True
    res = ww.write_watch_plan(redis, plan(watch(SYM)), agent="tft")
    assert res.errors and "读取规则配置失败" in res.errors[0]
    assert not res.ok
    assert redis.set_calls == []
    assert symbols_in(redis) == {SYM, OTHER}  # 表原样


def test_an_empty_agent_is_refused_because_ownership_is_undefined() -> None:
    """空 agent 无法确定归属 → 拒写（否则无名写者共用一组，彼此互删）。"""
    redis = FakeRedis(cfg(rule(SYM)))
    res = ww.write_watch_plan(redis, plan(watch(SYM)), agent="  ")
    assert res.errors == (ww.REJECT_NO_OWNER,)
    assert res.owner == ""
    assert redis.set_calls == []


def test_exceeding_the_rule_cap_writes_nothing() -> None:
    """合并后超上限 → 一条不写（写进去会让控制面 PUT 从此被 API 拒收）。"""
    redis = FakeRedis(cfg(rule(SYM), rule(OTHER)))
    res = ww.write_watch_plan(redis, plan(watch("000001.SZ")), agent="tft", max_rules=2)
    assert res.errors and "超上限" in res.errors[0]
    assert redis.set_calls == []
    assert symbols_in(redis) == {SYM, OTHER}


def test_the_cap_counts_both_sides() -> None:
    """上限按**合并后**的整表算：他方 + 本轮，而不是只看本轮。"""
    redis = FakeRedis(cfg(rule(SYM), rule(OTHER)))
    res = ww.write_watch_plan(redis, plan(watch("000001.SZ")), agent="tft", max_rules=3)
    assert res.ok and res.armed == ("000001.SZ",)
    assert len(table(redis)) == 3


# ---------------------------------------------------------------------------
# 6. 写成功但没落库：回读校验（错误必须从 errors 之外的地方也能看出来）
# ---------------------------------------------------------------------------
def test_a_silently_dropped_write_shows_up_as_unverified_not_as_success() -> None:
    """``RedisClient.set`` 吞掉写失败时 ``errors`` 是空的——这正是 ``ok`` 不能只看
    ``errors`` 的理由：失败以「回读没确认到」的形式出现。"""
    redis = FakeRedis(cfg())
    redis.drop_writes = True
    res = ww.write_watch_plan(redis, plan(watch(SYM)), agent="tft")

    assert res.errors == ()  # set 没抛错
    assert res.armed == ()
    (unverified,) = res.unverified
    assert unverified[0] == SYM and "没有这条规则" in unverified[1]
    assert res.ok is False
    out = res.outcomes()
    assert out[0]["armed"] is False
    assert str(out[0]["reject_reason"]).startswith(ww.REJECT_NOT_LANDED)


def test_a_failed_read_back_is_a_round_level_error() -> None:
    """回读本身失败 → 无法确认落库，本轮结论一律「未挂上」（不能假装成功）。"""
    redis = FakeRedis(cfg())
    redis.fail_get_keys = {ex.CONFIG_KEY: 2}  # 第 1 次是配置读，第 2 次是回读
    res = ww.write_watch_plan(redis, plan(watch(SYM)), agent="tft")
    assert res.errors and "回读失败" in res.errors[0]
    assert res.armed == () and not res.ok


def test_an_owner_rewritten_on_the_way_in_is_caught() -> None:
    """落库时归属被改写 → 记「未挂上」（挂上去也不会被下一轮认作自己那组）。"""
    redis = FakeRedis(cfg())

    def _rewrite(value: dict[str, Any]) -> dict[str, Any]:
        return {
            **value,
            "rules": [{**r, "owner": ""} for r in value.get("rules") or []],
        }

    redis.mutate_on_set = _rewrite
    res = ww.write_watch_plan(redis, plan(watch(SYM)), agent="tft")
    (unverified,) = res.unverified
    assert "归属被改写成 ''" in unverified[1]
    assert res.armed == () and not res.ok


def test_another_partys_rule_must_survive_the_write() -> None:
    """他方规则在写入后消失/被改写 → 轮次级 ``problems``（不是某一条 watch 的事，
    但必须有人看见；同时它也会写进 notes，审计表是唯一持久面）。"""
    redis = FakeRedis(cfg(rule(SYM, owner="llm:lgbm")))

    def _drop_others(value: dict[str, Any]) -> dict[str, Any]:
        return {
            **value,
            "rules": [r for r in value["rules"] if r["owner"] == "llm:tft"],
        }

    redis.mutate_on_set = _drop_others
    res = ww.write_watch_plan(redis, plan(watch(OTHER)), agent="tft")

    assert any("他方规则在写入后消失" in p for p in res.problems)
    assert not res.ok
    assert any("回读校验发现" in n for n in res.notes)


def test_an_old_rule_that_was_not_replaced_is_a_problem() -> None:
    """旧规则没被整组替换掉（合并写不进去）→ 也是轮次级问题。"""
    redis = FakeRedis(cfg(rule(SYM, owner="llm:tft"), rule(OTHER, owner="llm:tft")))

    def _keep_everything(value: dict[str, Any]) -> dict[str, Any]:
        merged = list(value["rules"])
        merged.extend(r for r in redis.store[ex.CONFIG_KEY]["rules"] if r not in merged)
        return {**value, "rules": merged}

    redis.mutate_on_set = _keep_everything
    res = ww.write_watch_plan(redis, plan(watch(SYM)), agent="tft")
    assert any("未被整组替换掉" in p for p in res.problems)
    assert not res.ok


# ---------------------------------------------------------------------------
# 7. 状态清理（摘掉规则之后）
# ---------------------------------------------------------------------------
def test_state_of_a_removed_symbol_is_cleared() -> None:
    """槽位已空 → 连同状态一起清（留着会让复用同名规则时沿用旧状态）。"""
    redis = FakeRedis(
        cfg(rule(SYM, owner="llm:tft")),
        {"date": DAY, "rules": {SYM: {"status": ex.ST_ARMED, "highest_price": 12.0}}},
    )
    res = ww.write_watch_plan(redis, plan(), agent="tft")
    assert res.removed == (SYM,)
    assert redis.store[ex.STATE_KEY]["rules"] == {}
    assert any("已清理摘除标的的状态" in n for n in res.notes)


def test_state_of_an_inflight_order_is_kept() -> None:
    """在途真单不清状态：清了等于丢掉这笔委托的跟踪与终态通知。"""
    redis = FakeRedis(
        cfg(rule(SYM, owner="llm:tft")),
        {
            "date": DAY,
            "rules": {SYM: {"status": ex.ST_SUBMITTED, "order_id": "ord-1"}},
        },
    )
    res = ww.write_watch_plan(redis, plan(), agent="tft")
    assert res.removed == ()  # 摘了规则，但状态还在跟踪
    assert SYM in redis.store[ex.STATE_KEY]["rules"]
    assert any("状态保留" in n for n in res.notes)


def test_state_of_a_slot_taken_over_by_a_human_is_left_alone() -> None:
    """槽位被别人接手（人工规则顶上来）→ 状态不动：那已经不是我们的槽位了。"""
    redis = FakeRedis(
        cfg(rule(SYM, owner="llm:tft")),
        {"date": DAY, "rules": {SYM: {"status": ex.ST_ARMED}}},
    )
    # 模拟「人工把标的长回来了」：先把配置改成人工规则
    redis.store[ex.CONFIG_KEY] = cfg(rule(SYM))
    res = ww.write_watch_plan(redis, plan(), agent="tft")
    assert res.armed == () and res.removed == ()
    assert SYM in redis.store[ex.STATE_KEY]["rules"]


def test_re_arming_a_symbol_that_already_triggered_is_flagged() -> None:
    """重挂一个当日已触发过的标的：规则会落库，但**不会再触发**，必须留痕。"""
    redis = FakeRedis(
        cfg(),
        {"date": DAY, "rules": {SYM: {"status": ex.ST_SUBMITTED, "order_id": "ord-1"}}},
    )
    res = ww.write_watch_plan(redis, plan(watch(SYM)), agent="tft")
    assert res.armed == (SYM,)
    assert any("今日已触发过" in n and "reset" in n for n in res.notes)


def test_yesterdays_state_does_not_make_a_symbol_look_stuck() -> None:
    """跨日：昨日状态不参与判定（``load_state`` 同口径）——否则每天第一次挂都是「已触发」。"""
    redis = FakeRedis(
        cfg(),
        {"date": "20200101", "rules": {SYM: {"status": ex.ST_SUBMITTED}}},
    )
    res = ww.write_watch_plan(redis, plan(watch(SYM)), agent="tft")
    assert res.armed == (SYM,)
    assert not any("今日已触发过" in n for n in res.notes)


def test_an_unreadable_state_skips_every_state_decision() -> None:
    """状态读不到 → 跳过状态检查与清理并留痕：清不掉旧状态是小事，误清才不可逆。"""
    redis = FakeRedis(
        cfg(rule(SYM, owner="llm:tft")),
        {"date": DAY, "rules": {SYM: {"status": ex.ST_ARMED}}},
    )
    # 只让**状态**那一读失败（配置的读与回读都正常）
    redis.fail_get_keys = {ex.STATE_KEY: 1}
    res = ww.write_watch_plan(redis, plan(), agent="tft")
    assert redis.store[ex.STATE_KEY]["rules"][SYM]["status"] == ex.ST_ARMED  # 没被动
    assert any("状态读不到" in n for n in res.notes)
    assert res.removed == ()  # 没清就没清，如实报


# ---------------------------------------------------------------------------
# 8. 留痕：总开关、注记、outcomes 的形状
# ---------------------------------------------------------------------------
def test_a_disabled_executor_is_recorded_as_a_note() -> None:
    """总开关没开：规则照样落库（这是配置，不是失败），但必须写明「不会触发」。"""
    redis = FakeRedis(cfg(enabled=False))
    res = ww.write_watch_plan(redis, plan(watch(SYM), notes=("模型注记",)), agent="tft")
    assert res.armed == (SYM,) and res.ok
    assert any("总开关未开" in n for n in res.notes)
    assert "模型注记" in res.notes  # plan 的注记原样带上


def test_outcomes_keep_the_per_rule_notes_from_the_plan() -> None:
    """改判只动结论与理由，**不动**规则自己的注记（比例三态那类说明要留下）。"""
    redis = FakeRedis(cfg(rule(SYM)))
    res = ww.write_watch_plan(
        redis, plan(watch(SYM, notes=("未给比例，按清仓",))), agent="tft"
    )
    out = res.outcomes()
    assert out[0]["armed"] is False  # 人工优先改判
    assert out[0]["notes"] == ["未给比例，按清仓"]


def test_summary_is_json_ready() -> None:
    """``summary()`` 进日志/审计，必须是可 JSON 化的纯数据（不是 tuple/对象）。"""
    import json

    redis = FakeRedis(cfg(rule(SYM)))
    res = ww.write_watch_plan(redis, plan(watch(OTHER)), agent="tft")
    blob = json.dumps(res.summary(), ensure_ascii=False)
    assert '"owner": "llm:tft"' in blob
    assert set(res.summary()) == {
        "owner",
        "armed",
        "removed",
        "conflicts",
        "unverified",
        "errors",
        "problems",
        "notes",
    }


def test_ok_is_false_whenever_any_rule_did_not_land() -> None:
    """``ok`` 的三态口径：errors / unverified / problems 任一非空都不是「按意图完成」。"""
    redis = FakeRedis(cfg())
    res = ww.write_watch_plan(redis, plan(watch(SYM)), agent="tft")
    assert res.ok and not res.problems and not res.unverified and not res.errors
