"""唤醒评估器纯逻辑测试（Stage 2 第二批·丙1/丙2）。

钉五组行为：

1. 四类触发判据的边界与方向（含 L2=五档代理、板块=门控恒空）；
2. 基线锚语义（首评初始化 / 轮次后重置 / 无基线不触发）；
3. 冷却与日预算（同码同向 60 分钟 / 20 轮封顶）；
4. 状态编解码（含坏 JSON 容错）；
5. 渲染文本（唤醒块/状态留痕一行）。
"""

from datetime import datetime, timedelta, timezone

import pytest

from backend.services.trade.services import decision_round_wake as W

CST = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 15, 10, 37, 0, tzinfo=CST)


def _obs(**kw) -> W.WakeObs:
    base = {"codes": ("600036.SH",), "names": {"600036.SH": "招商银行"}}
    base.update(kw)
    return W.WakeObs(**base)


def _state(**kw) -> W.WakeState:
    base = {"day": "20261015"}
    base.update(kw)
    return W.WakeState(**base)


# ── 开关与环境口径 ───────────────────────────────────────────────────


def test_wake_enabled_default_on_and_strict_true(monkeypatch):
    """唯一读法 env_flag：未设=开；只有小写 true 判开（1/yes/on 一律关）。"""
    monkeypatch.delenv("QM_DECISION_WAKE_ENABLED", raising=False)
    assert W.wake_enabled() is True
    monkeypatch.setenv("QM_DECISION_WAKE_ENABLED", "true")
    assert W.wake_enabled() is True
    for off in ("false", "1", "yes", "on", ""):
        monkeypatch.setenv("QM_DECISION_WAKE_ENABLED", off)
        assert W.wake_enabled() is False, off


# ── 状态编解码 ───────────────────────────────────────────────────────


def test_state_codec_round_trip():
    state = _state(
        last_eval_ts=100.0,
        last_round_ts=200.0,
        wake_count=3,
        news_ts=300.0,
        baselines={"600036.SH": {"pnl": 1.5, "chg": 2.0, "imb5": 0.1}},
        cooldowns={"600036.SH|up": 4000.0},
        last_note="唤醒触发：波动 招商银行(600036.SH)",
        sector_gap_day="20261015",
    )
    raw = W.encode_state(state)
    assert all(isinstance(v, str) for v in raw.values())
    back = W.decode_state(raw, day="20261015")
    assert back.wake_count == 3
    assert back.last_round_ts == 200.0
    assert back.baselines["600036.SH"]["pnl"] == 1.5
    assert back.cooldowns["600036.SH|up"] == 4000.0
    assert back.sector_gap_day == "20261015"


def test_state_decode_tolerates_garbage():
    raw = {
        "last_eval_ts": "abc",
        "wake_count": "",
        "baselines": "{bad json",
        "cooldowns": '{"600036.SH|up": "xx"}',
        "news_ts": None,
    }
    state = W.decode_state(raw, day="20261015")
    assert state.last_eval_ts == 0.0
    assert state.wake_count == 0
    assert state.baselines == {}
    assert state.cooldowns == {}


def test_decode_state_none_is_empty():
    state = W.decode_state(None, day="20261015")
    assert state == _state()


# ── 波动判据 ─────────────────────────────────────────────────────────


def test_volatility_fires_on_pnl_delta_and_reports_direction():
    state = _state(baselines={"600036.SH": {"pnl": 1.0, "chg": 1.0, "imb5": None}})
    obs = _obs(pnl_pct={"600036.SH": 4.2}, day_chg={"600036.SH": 3.0})
    out = W.volatility_triggers(obs, state)
    assert len(out) == 1
    t = out[0]
    assert (t.kind, t.code, t.direction) == ("volatility", "600036.SH", "up")
    assert "Δ+3.20pp" in t.detail and "今日 +3.00%" in t.detail


def test_volatility_below_delta_threshold_quiet():
    state = _state(baselines={"600036.SH": {"pnl": 1.0, "chg": 1.0, "imb5": None}})
    obs = _obs(pnl_pct={"600036.SH": 3.9}, day_chg={"600036.SH": 3.0})
    assert W.volatility_triggers(obs, state) == ()


def test_volatility_absolute_day_chg_fires_without_pnl_baseline():
    """当日 |涨跌|≥5% 是绝对阈值：首评无基线也能触发（冷却+预算兜重复）。"""
    obs = _obs(pnl_pct={"600036.SH": None}, day_chg={"600036.SH": -6.1})
    out = W.volatility_triggers(obs, _state())
    assert len(out) == 1
    assert out[0].direction == "down"
    assert "阈值" in out[0].detail


def test_volatility_pnl_delta_preferred_over_day_chg():
    state = _state(baselines={"600036.SH": {"pnl": 0.0, "chg": 0.0, "imb5": None}})
    obs = _obs(pnl_pct={"600036.SH": -4.0}, day_chg={"600036.SH": -7.0})
    out = W.volatility_triggers(obs, state)
    assert len(out) == 1
    assert "浮盈" in out[0].detail  # 同股只出一条，浮盈优先


def test_volatility_no_data_quiet():
    assert W.volatility_triggers(_obs(), _state()) == ()


# ── 盘口判据（L2 代理） ──────────────────────────────────────────────


def test_book_fires_on_imb5_delta_and_labels_proxy():
    state = _state(baselines={"600036.SH": {"pnl": None, "chg": None, "imb5": 0.10}})
    obs = _obs(imb5={"600036.SH": -0.25})
    out = W.book_triggers(obs, state)
    assert len(out) == 1
    assert out[0].kind == "book" and out[0].direction == "down"
    assert "五档代理" in out[0].detail
    assert "Δ-0.35" in out[0].detail


def test_book_no_baseline_or_missing_quote_quiet():
    assert W.book_triggers(_obs(imb5={"600036.SH": 0.9}), _state()) == ()
    state = _state(baselines={"600036.SH": {"pnl": None, "chg": None, "imb5": 0.1}})
    assert W.book_triggers(_obs(imb5={"600036.SH": None}), state) == ()


# ── 新闻判据 ─────────────────────────────────────────────────────────


def _hit(**kw) -> W.NewsHit:
    base = {
        "code": "600036.SH",
        "name": "招商银行",
        "verdict": "利空",
        "impact": 1.0,
        "event_type": "公告",
        "headline": "某某利空",
    }
    base.update(kw)
    return W.NewsHit(**base)


def test_news_fires_on_new_brief_with_held_hit():
    obs = _obs(brief_ts=1000.0, news_hits=(_hit(),))
    out = W.news_triggers(obs, _state(news_ts=900.0))
    assert len(out) == 1
    assert out[0].direction == "down"
    assert "[利空]" in out[0].detail


def test_news_same_or_older_ts_quiet_and_weak_impact_quiet():
    obs = _obs(brief_ts=900.0, news_hits=(_hit(),))
    assert W.news_triggers(obs, _state(news_ts=900.0)) == ()
    weak = _obs(brief_ts=1000.0, news_hits=(_hit(impact=0.4),))
    assert W.news_triggers(weak, _state(news_ts=900.0)) == ()


def test_news_p0_fires_without_impact_and_is_priority():
    obs = _obs(brief_ts=1000.0, news_hits=(_hit(impact=None, decision="P0", pre5=0.07),))
    out = W.news_triggers(obs, _state(news_ts=900.0))
    assert len(out) == 1
    assert out[0].priority is True
    assert "[P0]" in out[0].detail and "+7.0%" in out[0].detail


def test_news_p2_watch_do_not_fire():
    for decision in ("P2", "WATCH", "INFO"):
        obs = _obs(brief_ts=1000.0, news_hits=(_hit(impact=2.0, decision=decision),))
        assert W.news_triggers(obs, _state(news_ts=900.0)) == (), decision


def test_p0_does_not_count_toward_budget_and_uses_5min_cooldown():
    hit = _hit(impact=None, decision="P0")
    obs = _obs(brief_ts=1000.0, news_hits=(hit,))
    fired = W.news_triggers(obs, _state(news_ts=900.0))
    state = _state(wake_count=W.DAILY_WAKE_LIMIT)
    out = W.woken_state(state, obs, now=NOW, fired=fired, log_round_ts=0.0)
    assert out.wake_count == W.DAILY_WAKE_LIMIT
    key = W.cooldown_key(fired[0])
    assert abs(out.cooldowns[key] - (NOW.timestamp() + 300.0)) < 1e-6


# ── 板块门控 ─────────────────────────────────────────────────────────


def test_sector_triggers_gated_without_source():
    assert W.sector_triggers(None, None) == ()
    assert W.sector_triggers({}, {"600036.SH": "银行"}) == ()


def test_sector_triggers_judgement_ready_for_source():
    out = W.sector_triggers(
        {"银行": -5.6, "白酒": 2.0}, {"600036.SH": "银行", "600519.SH": "白酒"}
    )
    assert len(out) == 1
    assert out[0].name == "银行" and out[0].direction == "down"


def test_evaluate_triggers_merges_four_kinds_with_sector_gated():
    state = _state(
        news_ts=0.0,
        baselines={"600036.SH": {"pnl": 0.0, "chg": None, "imb5": 0.0}},
    )
    obs = _obs(
        pnl_pct={"600036.SH": 3.5},
        day_chg={"600036.SH": 3.5},
        imb5={"600036.SH": 0.5},
        brief_ts=1.0,
        news_hits=(_hit(verdict="利好"),),
    )
    kinds = [t.kind for t in W.evaluate_triggers(obs, state, NOW)]
    assert kinds == ["volatility", "book", "news"]  # sector 恒缺席（门控）


# ── 冷却与预算 ───────────────────────────────────────────────────────


def test_cooldown_suppresses_same_code_and_direction_only():
    t_up = W.WakeTrigger(
        kind="volatility", code="600036.SH", name="招商银行", direction="up"
    )
    t_down = W.WakeTrigger(
        kind="volatility", code="600036.SH", name="招商银行", direction="down"
    )
    state = _state(cooldowns={W.cooldown_key(t_up): NOW.timestamp() + 600})
    fire, held = W.filter_triggers((t_up, t_down), state, NOW)
    assert fire == (t_down,)
    assert held == (t_up,)


def test_cooldown_expires():
    t = W.WakeTrigger(kind="book", code="600036.SH", direction="up")
    state = _state(cooldowns={W.cooldown_key(t): NOW.timestamp() - 1})
    fire, held = W.filter_triggers((t,), state, NOW)
    assert fire == (t,) and held == ()


def test_budget_caps_at_daily_limit():
    assert W.budget_ok(_state(wake_count=W.DAILY_WAKE_LIMIT - 1)) is True
    assert W.budget_ok(_state(wake_count=W.DAILY_WAKE_LIMIT)) is False


# ── 基线锚语义 ───────────────────────────────────────────────────────


def test_idle_state_initializes_baseline_on_first_eval():
    obs = _obs(
        pnl_pct={"600036.SH": 2.0}, day_chg={"600036.SH": 1.0}, imb5={"600036.SH": 0.2}
    )
    state = W.idle_state(_state(), obs, now=NOW, log_round_ts=0.0)
    assert state.baselines["600036.SH"]["pnl"] == 2.0
    assert state.baselines["600036.SH"]["imb5"] == 0.2
    assert state.last_eval_ts == NOW.timestamp()


def test_idle_state_resets_baseline_when_new_round_seen():
    obs = _obs(pnl_pct={"600036.SH": 5.0})
    state = _state(
        last_round_ts=100.0,
        baselines={"600036.SH": {"pnl": 1.0, "chg": None, "imb5": None}},
    )
    out = W.idle_state(state, obs, now=NOW, log_round_ts=150.0)
    assert out.baselines["600036.SH"]["pnl"] == 5.0
    assert out.last_round_ts == 150.0


def test_idle_state_keeps_old_baseline_without_new_round():
    obs = _obs(pnl_pct={"600036.SH": 5.0})
    state = _state(
        last_round_ts=100.0,
        baselines={"600036.SH": {"pnl": 1.0, "chg": None, "imb5": None}},
    )
    out = W.idle_state(state, obs, now=NOW, log_round_ts=100.0)
    assert out.baselines["600036.SH"]["pnl"] == 1.0  # 漂移靠旧锚累积出来


def test_idle_state_advances_news_anchor_only_when_not_pending():
    seen = W.idle_state(
        _state(news_ts=100.0),
        _obs(brief_ts=200.0, news_hits=()),
        now=NOW,
        log_round_ts=0.0,
    )
    assert seen.news_ts == 200.0
    pending = W.idle_state(
        _state(news_ts=100.0),
        _obs(brief_ts=200.0, news_hits=(_hit(),)),
        now=NOW,
        log_round_ts=0.0,
    )
    assert pending.news_ts == 100.0  # 挂起（被冷却抑制）→ 锚不推进，防吞新闻


def test_woken_state_counts_sets_cooldown_and_resets_baseline():
    obs = _obs(pnl_pct={"600036.SH": 7.0}, brief_ts=555.0)
    fired = [W.WakeTrigger(kind="volatility", code="600036.SH", direction="up")]
    state = _state(
        wake_count=2,
        news_ts=100.0,
        baselines={"600036.SH": {"pnl": 1.0, "chg": None, "imb5": None}},
    )
    out = W.woken_state(state, obs, now=NOW, fired=fired, log_round_ts=0.0)
    assert out.wake_count == 3
    assert out.baselines["600036.SH"]["pnl"] == 7.0
    assert out.news_ts == 555.0
    assert (
        out.cooldowns["600036.SH|up"] == NOW.timestamp() + W.SAME_DIR_COOLDOWN_MIN * 60
    )


# ── 渲染 ─────────────────────────────────────────────────────────────


def test_context_text_lists_triggers_and_suppressed():
    fired = [
        W.WakeTrigger(
            kind="volatility",
            code="600036.SH",
            name="招商银行",
            direction="up",
            detail="浮盈 +1.00% → +5.00%（Δ+4.00pp）",
        )
    ]
    held = [
        W.WakeTrigger(
            kind="book",
            code="000001.SZ",
            name="平安银行",
            direction="down",
            detail="五档失衡",
        )
    ]
    text = W.context_text(fired, NOW, suppressed=held)
    assert text.startswith("【本轮为触发唤醒】（10:37，非整点槽位")
    assert "- 波动 招商银行(600036.SH)：浮盈 +1.00% → +5.00%（Δ+4.00pp）" in text
    assert "60 分钟冷却中" in text and "盘口 平安银行(000001.SZ)" in text
    assert text.rstrip().endswith("其余持仓按既有纪律管理。")


def test_snapshot_pnl_guards():
    assert W.snapshot_pnl(10.0, 11.0) == pytest.approx(10.0)
    assert W.snapshot_pnl(None, 11.0) is None
    assert W.snapshot_pnl(0.0, 11.0) is None
    assert W.snapshot_pnl(10.0, -1.0) is None
