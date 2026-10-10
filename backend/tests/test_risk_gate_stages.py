"""风控灰度档位（P2-1 / H6）：resolve_stage / load_config.enforce / 判定聚合矩阵 / 写侧校验。

口径（与 `risk_gate_service` 模块 docstring 同源）：

- **兼容基线**：无 `enforce` 条目时与旧二值翻闸 passed/verdict/enforced/计数桶逐字节
  一致——影子期全部记录原生 verdict 但放行；全权期（shadow=false）按原生动作全拦。
  本文件把这两条钉死。唯一有意差异是阻断归因：`rule_id`/`reason` 取首条**生效**拦截
  （旧实现取 decisions[0]，WARN 前置时误报——见 `test_block_attribution_...`）。
- **有档位时**：生效动作 = min(原生动作, 档位上限)。封顶只降不升：WARN 原生规则在
  reject 档下仍只是 warn（档位是刹车不是油门）。
- **记录词**：拦下时取**生效**等级（halt 仅当确有生效的熔断级拦截——原生 HALT 被
  reject 档压成拒单后仍记 halt，会把「halted 计数>0」误读成系统熔断发生过）；
  未拦下时取**原生**等级（旧影子留痕逐字兼容）。

`l0.session`（always_on）用 `ctx.now_ts or time.time()` 判时段——**不钉时间的话，
周末/夜间跑测试会整片 REJECT**（今天 2026-10-10 恰是周六）。本文件所有判定上下文
一律经 `_patch_ctx` 注入周五盘中时间戳。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.services.trade.routers import risk_ctl
from backend.services.trade.services import risk_gate_service as rgs
from backend.tests.test_risk_gate_wiring import FakeRedis, _cfg, _req

#: 2026-10-09（周五）10:00 CST = 02:00 UTC：工作日盘中——l0.session 放行。
SESSION_OK_TS = datetime(2026, 10, 9, 2, 0, tzinfo=timezone.utc).timestamp()


def _cfg_rules(rules: dict, **over) -> dict:
    """最小规则集配置（避免 DEFAULT_RULES 满配带来的无关触发）。"""
    return _cfg(rules=json.dumps(rules, ensure_ascii=False), **over)


def _patch_ctx(monkeypatch, **over):
    """注入判定上下文：默认周五盘中、买 100 股 @40（amount=4000）。

    available_cash=0.0 + 规则集含 l1.available_cash → 稳定的原生 REJECT；
    kill_switch=True → l0.kill_switch（always_on）稳定的原生 HALT。
    """
    from backend.shared.risk import RiskContext

    base = {
        "market": "CN",
        "symbol": "600036.SH",
        "side": "BUY",
        "quantity": 100,
        "price": 40.0,
        "now_ts": SESSION_OK_TS,
        "kill_switch": False,
    }
    base.update(over)
    ctx = RiskContext(**base)

    async def _b(req, *, db, redis, need_counts=False, need_daily_pnl=False):
        return ctx

    monkeypatch.setattr(rgs, "build_context", _b)
    return ctx


# ── resolve_stage：显式条目 > 全局缺省；非法档位回退缺省 ─────────────


def test_resolve_stage_explicit_wins_and_default_follows_shadow():
    # Arrange
    enforce = {"l1.available_cash": "reject"}

    # Act & Assert
    assert (
        rgs.resolve_stage("l1.available_cash", enforce=enforce, shadow=True) == "reject"
    )
    # 无条目 → 缺省：影子 off / 全权 halt
    assert rgs.resolve_stage("l1.position_cap", enforce=enforce, shadow=True) == "off"
    assert rgs.resolve_stage("l1.position_cap", enforce=enforce, shadow=False) == "halt"
    # 大小写/空白容错（手改 Redis 常带空白）
    assert rgs.resolve_stage("x", enforce={"x": "  HALT "}, shadow=True) == "halt"
    # 非法档位回退**缺省**（而非拼写值，也而非静默 off 之外的更严档）
    assert rgs.resolve_stage("x", enforce={"x": "haltt"}, shadow=False) == "halt"
    assert rgs.resolve_stage("x", enforce={"x": "haltt"}, shadow=True) == "off"


# ── load_config：enforce 解析与 fail-closed ──────────────────────────


def test_load_config_parses_enforce_and_normalizes():
    # Arrange
    redis = FakeRedis(
        config=_cfg(
            enforce=json.dumps(
                {"l1.available_cash": " Reject ", "l0.kill_switch": "HALT"}
            )
        )
    )

    # Act
    cfg = rgs.load_config(redis)

    # Assert
    assert cfg is not None
    assert cfg.enforce == {"l1.available_cash": "reject", "l0.kill_switch": "halt"}


def test_load_config_without_enforce_defaults_empty():
    """旧配置（无该键）读成空表 = 全局缺省——与旧二值翻闸行为一致。"""
    cfg = rgs.load_config(FakeRedis(config=_cfg()))
    assert cfg is not None and cfg.enforce == {}


def test_load_config_broken_enforce_fails_closed():
    """结构坏 = 配置不可信 → RuntimeError（上层 fail-closed 拒单，与 rules 同纪律）。"""
    with pytest.raises(RuntimeError):
        rgs.load_config(FakeRedis(config=_cfg(enforce="{not json")))
    with pytest.raises(RuntimeError):
        rgs.load_config(FakeRedis(config=_cfg(enforce='["l1.available_cash"]')))


def test_enforce_bad_entries_warn_once(monkeypatch, caplog):
    """失效形态（拼错/未注册/规则未启用）→ 告警且同集合只打一次，绝不拒载。"""
    # Arrange：重置模块级去重集（跨测试隔离）
    monkeypatch.setattr(rgs, "_bad_enforce_warned", frozenset())
    redis = FakeRedis(
        config=_cfg_rules(
            {"l1.available_cash": {}},
            enforce=json.dumps(
                {
                    "l1.available_cash": "rejcet",  # ① 拼错
                    "no.such_rule": "halt",  # ② 未注册
                    "l1.position_cap": "reject",  # ③ 合法但未启用（不在 rules 里）
                }
            ),
        )
    )

    # Act
    with caplog.at_level(logging.WARNING):
        cfg1 = rgs.load_config(redis)
        cfg2 = rgs.load_config(redis)  # 同一集合第二次不再刷屏

    # Assert
    assert cfg1 is not None and cfg2 is not None
    hits = [r for r in caplog.records if "enforce 档位表含无效条目" in r.getMessage()]
    assert len(hits) == 1
    msg = hits[0].getMessage()
    assert "l1.available_cash" in msg  # 拼错档位显影
    assert "no.such_rule" in msg  # 未注册 id 显影
    assert "l1.position_cap" in msg  # 写了档位却不跑也显影
    # 拼错的档位不改变加载结果（回退由 resolve_stage 在判定时做）
    assert cfg1.enforce["l1.available_cash"] == "rejcet"


# ── 判定聚合矩阵（真引擎 + 钉时上下文）───────────────────────────────


@pytest.mark.asyncio
async def test_shadow_no_enforce_records_native_and_passes(monkeypatch):
    """兼容基线①：影子 + 无档位 → 原生 HALT/REJECT 全记录、全放行（旧行为逐字节）。"""
    # Arrange
    redis = FakeRedis(config=_cfg_rules({"l1.available_cash": {}}))
    _patch_ctx(monkeypatch, kill_switch=True, available_cash=0.0)

    # Act
    v = await rgs.evaluate_order(_req(), db=None, redis=redis, record=True)

    # Assert
    assert v.passed and not v.enforced
    assert v.verdict == "halt"  # 未拦下取原生最高等级（与旧留痕一致）
    rec = redis.xadds[-1][1]
    assert rec["verdict"] == "halt" and rec["enforced"] == "false"
    assert redis.hincr.get("halted") == 1
    stages = {d["rule_id"]: d["stage"] for d in v.decisions}
    assert stages == {"l0.kill_switch": "off", "l1.available_cash": "off"}
    assert "l0.session" in rec.get("checked", "").split(",")


@pytest.mark.asyncio
async def test_shadow_no_enforce_matches_legacy_enforce_mode(monkeypatch):
    """兼容基线②：shadow=false + 无档位 → 按原生动作全拦（旧行为逐字节）。"""
    # Arrange
    redis = FakeRedis(config=_cfg_rules({"l1.available_cash": {}}, shadow="false"))
    _patch_ctx(monkeypatch, kill_switch=True, available_cash=0.0)

    # Act
    v = await rgs.evaluate_order(_req(), db=None, redis=redis, record=True)

    # Assert
    assert not v.passed and v.enforced
    assert v.verdict == "halt"  # 确有生效熔断 → 生效等级=原生 halt
    assert v.rule_id == "l0.kill_switch"  # primary=首条生效熔断
    rec = redis.xadds[-1][1]
    assert rec["enforced"] == "true" and redis.hincr.get("halted") == 1
    assert redis.hincr.get("rejected") is None  # 单标签：halt 压过 reject，旧口径


@pytest.mark.asyncio
async def test_single_rule_reject_stage_blocks_under_shadow(monkeypatch):
    """灰度核心场景：影子主开关未翻（shadow=true），但一条规则按 reject 档真拦。"""
    # Arrange
    redis = FakeRedis(
        config=_cfg_rules(
            {"l1.available_cash": {}},
            enforce=json.dumps({"l1.available_cash": "reject"}),
        )
    )
    _patch_ctx(monkeypatch, available_cash=0.0)

    # Act
    v = await rgs.evaluate_order(_req(), db=None, redis=redis, record=True)

    # Assert
    assert not v.passed and v.enforced
    assert v.verdict == "reject" and v.rule_id == "l1.available_cash"
    assert v.shadow is True  # 全局影子仍在，是单规则失效
    rec = redis.xadds[-1][1]
    assert rec["verdict"] == "reject" and rec["enforced"] == "true"
    assert redis.hincr.get("rejected") == 1
    assert redis.hincr.get("shadow_rejected") is None  # 真拦，不进影子桶
    assert json.loads(rec["decisions"])[0]["stage"] == "reject"


@pytest.mark.asyncio
async def test_warn_stage_records_but_passes(monkeypatch):
    """warn 档 = 观察：与影子同效（记录原生 verdict、放行、进影子桶），但档位显式。"""
    # Arrange
    redis = FakeRedis(
        config=_cfg_rules(
            {"l1.available_cash": {}},
            enforce=json.dumps({"l1.available_cash": "warn"}),
        )
    )
    _patch_ctx(monkeypatch, available_cash=0.0)

    # Act
    v = await rgs.evaluate_order(_req(), db=None, redis=redis, record=True)

    # Assert
    assert v.passed and not v.enforced
    assert v.verdict == "reject"  # 未拦下 → 原生等级
    rec = redis.xadds[-1][1]
    assert rec["enforced"] == "false"
    assert redis.hincr.get("rejected") == 1
    assert redis.hincr.get("shadow_rejected") == 1  # 记录但放行 → 影子桶
    assert v.decisions[0]["stage"] == "warn" and v.decisions[0]["action"] == "REJECT"


@pytest.mark.asyncio
async def test_off_stage_exempts_rule_in_enforce_mode(monkeypatch):
    """shadow=false（全权）+ off 档 = 单规则豁免：唯一被免的规则放行，其余照样全拦。"""
    # Arrange
    redis = FakeRedis(
        config=_cfg_rules(
            {"l1.available_cash": {}},
            shadow="false",
            enforce=json.dumps({"l1.available_cash": "off"}),
        )
    )
    _patch_ctx(monkeypatch, available_cash=0.0)

    # Act
    v = await rgs.evaluate_order(_req(), db=None, redis=redis, record=True)

    # Assert
    assert v.passed and not v.enforced  # 豁免生效
    assert v.verdict == "reject"  # 未拦下 → 原生等级（留痕仍能看到本该拦）
    assert redis.xadds[-1][1]["enforced"] == "false"
    assert redis.hincr.get("shadow_rejected") == 1
    assert v.decisions[0]["stage"] == "off"


@pytest.mark.asyncio
async def test_reject_stage_caps_native_halt(monkeypatch):
    """原生 HALT 在 reject 档下压成拒单：拦单但**不触发**全局熔断语义。

    记录词必须取生效等级 reject——若跟着原生记 halt，「halted 计数>0」会被
    误读成系统熔断发生过。
    """
    # Arrange
    redis = FakeRedis(
        config=_cfg_rules({}, enforce=json.dumps({"l0.kill_switch": "reject"}))
    )
    _patch_ctx(monkeypatch, kill_switch=True)

    # Act
    v = await rgs.evaluate_order(_req(), db=None, redis=redis, record=True)

    # Assert
    assert not v.passed and v.enforced
    assert v.verdict == "reject"  # 生效等级（原生是 HALT）
    assert v.rule_id == "l0.kill_switch"  # primary=生效拦截
    rec = redis.xadds[-1][1]
    assert rec["verdict"] == "reject"
    assert redis.hincr.get("rejected") == 1
    assert redis.hincr.get("halted") is None  # 未发生系统熔断
    d = v.decisions[0]
    assert d["action"] == "HALT" and d["stage"] == "reject"  # 审计面保留原生等级


@pytest.mark.asyncio
async def test_halt_stage_enables_kill_switch_under_shadow(monkeypatch):
    """影子期先把急停闸按 halt 档全权打开：一按急停就真熔断（其余仍影子）。"""
    # Arrange
    redis = FakeRedis(
        config=_cfg_rules({}, enforce=json.dumps({"l0.kill_switch": "halt"}))
    )
    _patch_ctx(monkeypatch, kill_switch=True)

    # Act
    v = await rgs.evaluate_order(_req(), db=None, redis=redis, record=True)

    # Assert
    assert not v.passed and v.enforced
    assert v.verdict == "halt" and v.rule_id == "l0.kill_switch"
    assert redis.hincr.get("halted") == 1


@pytest.mark.asyncio
async def test_warn_native_rule_not_escalated_by_reject_stage(monkeypatch):
    """封顶只降不升：WARN 原生规则在 reject 档下仍只是 warn——档位是刹车不是油门。"""
    # Arrange
    redis = FakeRedis(
        config=_cfg_rules(
            {"l3.cancel_ratio": {"max_ratio": 0.4, "min_orders": 10}},
            shadow="false",
            enforce=json.dumps({"l3.cancel_ratio": "reject"}),
        )
    )
    _patch_ctx(monkeypatch, orders_today=12, cancels_today=10)  # 撤单率 0.83 > 0.4

    # Act
    v = await rgs.evaluate_order(_req(), db=None, redis=redis, record=True)

    # Assert
    assert v.passed and not v.enforced
    assert v.verdict == "warn"
    assert v.decisions[0]["action"] == "WARN" and v.decisions[0]["stage"] == "reject"
    assert redis.hincr.get("warned") == 1


@pytest.mark.asyncio
async def test_effective_primary_skips_exempted_and_native_halt(monkeypatch):
    """多决策时 primary = 首条**生效**拦截：被 off 豁免的急停不算，原生 HALT 被压后
    也不算生效熔断——记录词取生效等级 reject。"""
    # Arrange
    redis = FakeRedis(
        config=_cfg_rules(
            {"l1.available_cash": {}},
            enforce=json.dumps(
                {"l0.kill_switch": "off", "l1.available_cash": "reject"}
            ),
        )
    )
    _patch_ctx(monkeypatch, kill_switch=True, available_cash=0.0)

    # Act
    v = await rgs.evaluate_order(_req(), db=None, redis=redis, record=True)

    # Assert
    assert not v.passed and v.enforced
    assert v.verdict == "reject" and v.rule_id == "l1.available_cash"
    stages = {d["rule_id"]: d["stage"] for d in v.decisions}
    assert stages == {"l0.kill_switch": "off", "l1.available_cash": "reject"}
    assert redis.hincr.get("rejected") == 1 and redis.hincr.get("halted") is None


@pytest.mark.asyncio
async def test_block_attribution_prefers_effective_blocker_over_leading_warn(
    monkeypatch,
):
    """评审 M3：WARN 条目排在前时，归因（rule_id/reason）必须取**生效拦截**。

    旧实现取 decisions[0]——撤单率这类观察项排在前，会把「建议限频」的 WARN
    报成拒单原因（推送面板 problem 文案与 [RiskGate] 拒单告警都吃这个字段）。
    """
    # Arrange：撤单率 WARN 与下单频率 REJECT 同批命中；决策按 (level, rule_id) 排序，
    # l3.cancel_ratio 在前 → 旧实现必错、新实现必对，两侧都被这一条钉住。
    redis = FakeRedis(
        config=_cfg_rules(
            {
                "l3.cancel_ratio": {"max_ratio": 0.4, "min_orders": 10},
                "l3.order_frequency": {"max_per_minute": 20},
            },
            shadow="false",
        )
    )
    _patch_ctx(monkeypatch, orders_today=12, cancels_today=10, orders_last_minute=25)

    # Act
    v = await rgs.evaluate_order(_req(), db=None, redis=redis, record=True)

    # Assert
    assert not v.passed and v.enforced
    assert v.rule_id == "l3.order_frequency"  # 生效拦截，而非 decisions[0] 的 WARN
    assert v.reason == "下单频率超限"
    assert [d["rule_id"] for d in v.decisions] == [
        "l3.cancel_ratio",
        "l3.order_frequency",
    ]
    assert v.decisions[0]["action"] == "WARN"  # 前置条目确为观察项


@pytest.mark.asyncio
async def test_decisions_carry_per_rule_enforced_flag(monkeypatch):
    """评审 M1 的证据面：灰度期一条影子规则会随**被别的规则拦下**的单一起拿到
    entry 级 enforced=true——逐条生效标记必须区分「这条规则真拦没有」，否则影子
    报告会把它记进「已实现代价」臂，污染定档样本。
    """
    # Arrange：影子主开关未翻；l3.order_frequency 按 reject 档真拦，l1 仍 off（没拦）
    redis = FakeRedis(
        config=_cfg_rules(
            {"l1.available_cash": {}, "l3.order_frequency": {"max_per_minute": 20}},
            enforce=json.dumps({"l3.order_frequency": "reject"}),
        )
    )
    _patch_ctx(monkeypatch, available_cash=0.0, orders_last_minute=25)

    # Act
    v = await rgs.evaluate_order(_req(), db=None, redis=redis, record=True)

    # Assert
    assert not v.passed and v.enforced  # 单被拦：entry 级 enforced=true
    assert {d["rule_id"]: d["enforced"] for d in v.decisions} == {
        "l1.available_cash": False,  # 影子规则：记录但没拦 → 影子臂
        "l3.order_frequency": True,  # 真拦 → 已实现代价臂
    }
    # 留痕 JSON 同字段可读（ghost 分臂的数据源）
    rec = redis.xadds[-1][1]
    assert {d["rule_id"]: d["enforced"] for d in json.loads(rec["decisions"])} == {
        "l1.available_cash": False,
        "l3.order_frequency": True,
    }


@pytest.mark.asyncio
async def test_enforce_cannot_weaken_fail_closed_config(monkeypatch):
    """评审 D-i：enforce 是刹车不是油门——配置结构坏 + 急停按 off 档豁免，仍然
    fail-closed 拒单（l0.config）。档位表碰不到这条前置闸（它对 enforce 值一无所知）。
    """
    # Arrange
    redis = FakeRedis(
        config=_cfg(
            rules="{not json",
            enforce=json.dumps({"l0.kill_switch": "off"}),
        )
    )
    _patch_ctx(monkeypatch)  # 上下文全干净

    # Act
    v = await rgs.evaluate_order(_req(), db=None, redis=redis, record=True)

    # Assert
    assert not v.passed and v.enforced
    assert v.verdict == "error" and v.rule_id == "l0.config"
    rec = redis.xadds[-1][1]
    assert rec["enforced"] == "true" and "config:" in rec.get("error", "")


# ── 运维面：GET /risk/status 档位表 + POST /risk/config 写侧校验 ─────


@pytest.mark.asyncio
async def test_status_exposes_effective_stage_table():
    """档位可见面：逐规则生效档位与判定侧同源（resolve_stage 同一实现）。"""
    # Arrange
    redis = FakeRedis(
        config=_cfg(
            enforce=json.dumps({"l0.kill_switch": "reject", "no.such_rule": "halt"})
        )
    )

    # Act
    out = await risk_ctl.risk_status(redis=redis, auth=SimpleNamespace(username="t"))

    # Assert
    data = out["data"]
    assert data["enforce"] == {"l0.kill_switch": "reject", "no.such_rule": "halt"}
    assert data["stages"]["l0.kill_switch"] == {"stage": "reject", "enabled": True}
    # 未列条目的缺省（影子 → off）；规则在 DEFAULT_RULES 里 → enabled
    assert data["stages"]["l1.available_cash"] == {"stage": "off", "enabled": True}
    # 配置比代码新：未知 id 占位显影且不谎称启用
    assert data["stages"]["no.such_rule"] == {"stage": "halt", "enabled": False}


@pytest.mark.asyncio
async def test_status_shadow_parse_matches_engine_for_noncanonical_values():
    """评审 M2a：手改的 "1"/"yes"/"on"/空串——面板与引擎必须同判（引擎 `_as_bool`
    把这些全算影子开），未列规则的缺省档位显示 off。旧解析把它们判成 shadow=false，
    面板显示全 halt 档而闸门实际在放行——操作员读到的与事实相反。
    """
    for raw_shadow in ("1", "yes", "on", ""):
        redis = FakeRedis(config=_cfg(shadow=raw_shadow))

        out = await risk_ctl.risk_status(
            redis=redis, auth=SimpleNamespace(username="t")
        )

        assert out["data"]["stages"]["l0.kill_switch"]["stage"] == "off", raw_shadow

    # 反方向：显式关（"off"）→ 引擎 shadow=False → 缺省 halt（面板不能反过来说 off）
    redis = FakeRedis(config=_cfg(shadow="off"))
    out = await risk_ctl.risk_status(redis=redis, auth=SimpleNamespace(username="t"))
    assert out["data"]["stages"]["l0.kill_switch"]["stage"] == "halt"


@pytest.mark.asyncio
async def test_status_enabled_uses_engine_merged_rules(monkeypatch):
    """评审 M2b：enabled 必须按引擎合并视图（`load_config().rules`，含档位 tier
    启用的规则）——拿原始 rules 判会把「档位正在管这条规则」显示成不会跑。
    """
    # Arrange：原始 rules 只列 l3.lot_size；合并视图（模拟档位启用）多出 l1.position_cap
    redis = FakeRedis(config=_cfg_rules({"l3.lot_size": {}}))
    monkeypatch.setattr(
        rgs,
        "load_config",
        lambda _redis: SimpleNamespace(
            rules={"l3.lot_size": {}, "l1.position_cap": {}}
        ),
    )

    # Act
    out = await risk_ctl.risk_status(redis=redis, auth=SimpleNamespace(username="t"))

    # Assert
    stages = out["data"]["stages"]
    assert stages["l1.position_cap"]["enabled"] is True  # 档位启用 → 会跑
    assert stages["l3.lot_size"]["enabled"] is True


@pytest.mark.asyncio
async def test_status_enabled_falls_back_when_config_broken(monkeypatch):
    """配置损坏 → 判定侧已 fail-closed（l0.config 拒单）；面板不二次 503，
    enabled 退回原始 rules 显影（宁可少标，不谎报会跑）。"""
    # Arrange
    redis = FakeRedis(config=_cfg())

    def _boom(_redis):
        raise RuntimeError("qm:risk:config.rules 解析失败: boom")

    monkeypatch.setattr(rgs, "load_config", _boom)

    # Act
    out = await risk_ctl.risk_status(redis=redis, auth=SimpleNamespace(username="t"))

    # Assert：仍 200，enabled 按原始 rules（_cfg 的 DEFAULT_RULES 含 l1.available_cash）
    assert out["data"]["stages"]["l1.available_cash"]["enabled"] is True


@pytest.mark.asyncio
async def test_config_update_validates_enforce_and_writes():
    """写侧纪律：非法档位/未注册 id 一律 400（拒早于生效）；合法值规范化后落 Redis。"""
    # Arrange
    redis = FakeRedis(config=_cfg())
    auth = SimpleNamespace(username="t")

    # Act & Assert：① 拼错档位 → 400
    with pytest.raises(HTTPException) as e1:
        await risk_ctl.risk_config_update(
            risk_ctl.RiskConfigUpdate(enforce={"l1.available_cash": "block"}),
            redis=redis,
            auth=auth,
        )
    assert e1.value.status_code == 400 and "非法档位" in str(e1.value.detail)

    # ② 未注册规则 id → 400
    with pytest.raises(HTTPException) as e2:
        await risk_ctl.risk_config_update(
            risk_ctl.RiskConfigUpdate(enforce={"no.such": "halt"}),
            redis=redis,
            auth=auth,
        )
    assert e2.value.status_code == 400 and "未注册规则" in str(e2.value.detail)

    # ③ 合法写入：大小写/空白规范化，version 自增
    out = await risk_ctl.risk_config_update(
        risk_ctl.RiskConfigUpdate(enforce={"l1.available_cash": " Reject "}),
        redis=redis,
        auth=auth,
    )
    assert out["success"] and "enforce" in out["data"]["updates"]
    assert json.loads(redis.config["enforce"]) == {"l1.available_cash": "reject"}
    assert redis.config["version"] == "2"

    # ④ 整表清空回缺省（回滚灰度）
    out2 = await risk_ctl.risk_config_update(
        risk_ctl.RiskConfigUpdate(enforce={}), redis=redis, auth=auth
    )
    assert out2["success"] and json.loads(redis.config["enforce"]) == {}


@pytest.mark.asyncio
async def test_single_rule_reject_stage_writes_back_to_config_key():
    """POST 写出的 enforce 必须能被判定侧原样读到（写读同键同格式，防两侧漂移）。

    经**真实端点**写入再读（评审 D-ii）：手工塞 Redis 只验了读侧，验不到写侧序列化
    （如 ensure_ascii / 大小写规范化）与判定侧解析的一致性。
    """
    # Arrange
    redis = FakeRedis(config=_cfg())

    # Act：端点写入
    out = await risk_ctl.risk_config_update(
        risk_ctl.RiskConfigUpdate(enforce={"l0.kill_switch": " Reject "}),
        redis=redis,
        auth=SimpleNamespace(username="t"),
    )

    # Assert：判定侧同一读取路径拿到的就是档位本身
    assert out["success"]
    cfg = rgs.load_config(redis)
    assert cfg is not None and cfg.enforce == {"l0.kill_switch": "reject"}
    assert (
        rgs.resolve_stage("l0.kill_switch", enforce=cfg.enforce, shadow=cfg.shadow)
        == "reject"
    )
