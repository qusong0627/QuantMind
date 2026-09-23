"""P2.1d 决策账 CLI 的纯函数守卫（不碰库、不碰盘）。

守的是三类口径——都是「错了不会报错、只会把数算歪」的那种：

1. **方向**：`side_of` 只有 `sell` 是卖。写反了，一字板那批会全判反（涨停留给
   「假如买」、跌停留给「假如卖」，两边都错）；
2. **分母**：`_stat` 只认拿得到的数，拿不到的一律不进分母、空组返回 `None`
   （**绝不退化成 0**——0 的语义是「平的」，那是另一个结论）；
3. **单调**：`price_record` 重跑定价**绝不降级**（一次失败的读盘不能抹掉旧数）。

与隔壁 `decision_track` 的口径对账不在本文件（那是一次性验证，见迁移计划文档）：
本文件只钉本仓自己的不变量，隔壁怎么算不在这里断言。
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from backend.scripts.decision_ledger import (
    BY_TAG_MIN,
    KIND_BULLISH,
    KIND_POSITION,
    KIND_SELL,
    MIN_SAMPLE,
    _fmt_stat,
    _pool_row,
    _stat,
    build_scorecard,
    needs_pricing,
    price_record,
    render,
    side_of,
)
from backend.shared.decision_ledger_store import DecisionRecord
from backend.shared.risk.ghost_pricing import (
    H_NO_DATA,
    H_NOT_MATURED,
    H_OK,
    PriceInput,
)

TS = datetime(2026, 9, 8, 6, 45, tzinfo=timezone.utc)
DAY = date(2026, 9, 8)


def _rec(*, action="watch", kind=KIND_BULLISH, **kw) -> DecisionRecord:
    """最小可用审计行（只给测试真正关心的字段）。"""
    base = {
        "id": "a" * 16,
        "pool_key": "b" * 16,
        "round_id": "r1",
        "tenant_id": "default",
        "user_id": "",
        "agent": "A",
        "market": "CN",
        "trade_date": DAY,
        "decided_at": TS,
        "code": "SH600519",
        "code_raw": "600519.SH",
        "action": action,
        "kind": kind,
    }
    return DecisionRecord(**{**base, **kw})


def _h(state: str, ret: float | None = None, excess: float | None = None):
    return {"state": state, "ret": ret, "bench": 0.0, "excess": excess, "cost": 0.0}


def _fwd(**by_horizon) -> dict:
    """`_fwd(t1=..., t5=...)` → 本表形状；不传的期**就是缺键**（≠ 有键值为 None）。"""
    return {f"t{k[1:]}": v for k, v in by_horizon.items()}


# ── 方向 ────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("action", "want"),
    [
        ("sell", "sell"),
        ("SELL", "sell"),
        ("  sell  ", "sell"),
        ("Sell", "sell"),
        ("buy", "buy"),
        ("watch", "buy"),
        ("hold", "buy"),
        ("", "buy"),
        (None, "buy"),
    ],
)
def test_side_of_only_sell_is_sell(action, want):
    """只有 `sell` 是卖：`buy`/`watch`/`hold` 都按「假如买」定价。"""
    assert side_of(action) == want


# ── 该不该回队列 ─────────────────────────────────────────────────────
def test_needs_pricing_true_when_never_priced():
    assert needs_pricing(_rec(fwd=None)) is True


def test_needs_pricing_true_when_a_horizon_key_is_absent():
    """缺键 = 那一期没落过，还能补——与「落过但没到期」同等对待。"""
    assert needs_pricing(_rec(fwd=_fwd(t1=_h(H_OK, 0.01)))) is True


@pytest.mark.parametrize("state", [H_NOT_MATURED, H_NO_DATA])
def test_needs_pricing_true_while_a_horizon_can_still_improve(state):
    """没到期（日历会推）与缺数（数据会补）都是「还有可能变好」。"""
    fwd = _fwd(t1=_h(H_OK, 0.01), t5=_h(state))
    assert needs_pricing(_rec(fwd=fwd)) is True


def test_needs_pricing_false_once_every_horizon_settled():
    """四期都是 ok 才退出队列——否则晚到的 t20/t60 永远补不上。"""
    fwd = _fwd(**{f"t{h}": _h(H_OK, 0.01) for h in (1, 5, 20, 60)})
    assert needs_pricing(_rec(fwd=fwd)) is False


# ── 单调：重跑绝不降级 ───────────────────────────────────────────────
def test_price_record_never_downgrades_a_settled_horizon():
    """一次读盘失败不能抹掉已定的价（P1.6 `merge_fwd` 的口径）。

    第二轮的 `exit_px` 里没有 t1 → 新算出来是 `no_data`；但 t1 上一轮已 ok，
    必须原样留着，并把期号报成「被拒降级」。
    """
    inp = PriceInput(
        entry_day="2026-09-09",
        entry_px=10.0,
        tradable=True,
        exit_px={1: 10.5, 5: 11.0},
        bench={1: 0.001, 5: 0.002},
    )
    rec, rejected = price_record(_rec(fwd=None), inp, tags=("追高",), priced_at=TS)
    assert rejected == ()
    assert rec.fwd["t1"]["state"] == H_OK
    first_ret = rec.fwd["t1"]["ret"]

    degraded, rejected2 = price_record(
        rec,
        PriceInput(
            entry_day="2026-09-09",
            entry_px=10.0,
            tradable=True,
            exit_px={},  # 这次读盘什么都没有
            bench={},
        ),
        tags=("追高",),
        priced_at=TS,
    )
    assert degraded.fwd["t1"]["state"] == H_OK, "已定的 t1 被降级了"
    assert degraded.fwd["t1"]["ret"] == first_ret, "已定的 t1 收益被改写"
    assert 1 in rejected2, "降级尝试没被报出来"


def test_price_record_writes_entry_fields_and_tags():
    rec, _ = price_record(
        _rec(fwd=None),
        PriceInput(
            entry_day="2026-09-09",
            entry_px=10.0,
            tradable=True,
            exit_px={1: 10.5},
            bench={1: 0.0},
        ),
        tags=("追高", "站上MA20"),
        priced_at=TS,
    )
    assert rec.entry_date == date(2026, 9, 9)
    assert rec.entry_px == 10.0
    assert rec.tradable is True
    assert rec.tags == ("追高", "站上MA20")
    assert rec.priced_at == TS


@pytest.mark.parametrize("bogus", ["20260909", "2026/09/09", "2026-9-9", "2026090"])
def test_price_record_rejects_a_non_iso_entry_date(bogus):
    """入场日格式漂移必须当场炸。

    `date.fromisoformat` 在 3.11+ 会**接受** `20260909`、在 3.10 抛错——若放它
    兜底，同一份决策账在新旧解释器上会走出两种读法。幽灵层全链路是 ISO
    （`next_trading_day`/`horizon_days`/`trading_days`），故这里按形态硬判。
    """
    with pytest.raises(ValueError, match="不是 ISO 形态"):
        price_record(
            _rec(fwd=None),
            PriceInput(
                entry_day=bogus,
                entry_px=10.0,
                tradable=True,
                exit_px={1: 10.5},
                bench={1: 0.0},
            ),
            tags=(),
            priced_at=TS,
        )


# ── 统计口径 ────────────────────────────────────────────────────────
def test_stat_returns_none_not_zero_for_an_empty_group():
    """空组**不是 0**：0 的语义是「平的」，那是另一个结论。"""
    s = _stat([], 5)
    assert s == {"n": 0, "win_rate": None, "avg_ret": None, "avg_excess": None}


def test_stat_skips_horizons_without_a_return():
    """`ret` 为 None 的期不进分母（不可得绝不退化成 0）。"""
    a = _stat([_rec(fwd=_fwd(t5=_h(H_OK, 0.10)))], 5)
    b = _stat(
        [
            _rec(fwd=_fwd(t5=_h(H_OK, 0.10))),
            _rec(fwd=_fwd(t5=_h(H_NOT_MATURED))),
            _rec(fwd=_fwd(t5=_h(H_NO_DATA))),
            _rec(fwd=None),
        ],
        5,
    )
    assert a["n"] == 1 and b["n"] == 1, "没数的那几行混进分母了"
    assert b["avg_ret"] == a["avg_ret"]


def test_stat_invert_negates_both_ret_and_excess():
    """卖出方向取负：卖后跌才算对（超额同向翻）。"""
    rows = [_rec(action="sell", kind=KIND_SELL, fwd=_fwd(t1=_h(H_OK, -0.03, -0.04)))]
    assert _stat(rows, 1)["avg_ret"] == -0.03
    inv = _stat(rows, 1, invert=True)
    assert inv["avg_ret"] == 0.03
    assert inv["avg_excess"] == 0.04
    assert inv["win_rate"] == 1.0, "卖后跌应记胜"


def test_fmt_stat_renders_missing_as_dash():
    """`—` 与 `0` 是两件事，版式上必须分得开。"""
    assert _fmt_stat({"n": 0, "win_rate": None, "avg_ret": None, "avg_excess": None})
    assert _fmt_stat({"n": 0}) == "n=0"
    flat = _fmt_stat({"n": 3, "win_rate": 0.5, "avg_ret": 0.0, "avg_excess": None})
    assert "0.0%" in flat and "—" not in flat
    assert "超额" not in _fmt_stat({"n": 1, "win_rate": 1.0, "avg_ret": 0.1})


# ── 记分卡 ──────────────────────────────────────────────────────────
def test_scorecard_negates_sell_horizons():
    sc = build_scorecard(
        [_rec(action="sell", kind=KIND_SELL, fwd=_fwd(t1=_h(H_OK, -0.02)))],
        today="2026-09-23",
    )
    assert sc["agents"]["A"][KIND_SELL]["t1"]["avg_ret"] == 0.02, "卖出没取负"


def test_scorecard_bullish_drops_rows_that_could_not_be_bought():
    """一字板买不进，留着会系统性高估选股能力（隔壁同口径）。"""
    ok = _rec(fwd=_fwd(t1=_h(H_OK, 0.01)), tradable=True)
    limit_up = _rec(fwd=_fwd(t1=_h(H_OK, 0.50)), tradable=False)
    unknown = _rec(fwd=_fwd(t1=_h(H_OK, 0.02)), tradable=None)
    sub = build_scorecard([ok, limit_up, unknown], today="2026-09-23")["agents"]["A"][
        KIND_BULLISH
    ]
    assert sub["n"] == 2, "tradable=False 的行没被剔"
    assert sub["t1"]["avg_ret"] == pytest.approx(0.015), (
        "unknown 不该被剔（None≠False）"
    )


def test_scorecard_position_keeps_untradable_rows():
    """剔除只针对 bullish：持仓/卖出决策本来就不涉及「买不买得进」。"""
    sub = build_scorecard(
        [_rec(kind=KIND_POSITION, fwd=_fwd(t1=_h(H_OK, 0.01)), tradable=False)],
        today="2026-09-23",
    )["agents"]["A"][KIND_POSITION]
    assert sub["n"] == 1


def test_scorecard_tag_group_needs_the_minimum_sample():
    """单个标签样本 <5 不出组——4 条与 5 条是边界两侧。"""
    thin = [
        _rec(fwd=_fwd(t5=_h(H_OK, 0.01)), tags=("追高",)) for _ in range(BY_TAG_MIN - 1)
    ]
    fat = [_rec(fwd=_fwd(t5=_h(H_OK, 0.01)), tags=("放量",)) for _ in range(BY_TAG_MIN)]
    by_tag = build_scorecard(thin + fat, today="2026-09-23")["agents"]["A"][
        KIND_BULLISH
    ]["by_tag_t5"]
    assert "追高" not in by_tag, f"{BY_TAG_MIN - 1} 条的标签出组了"
    assert by_tag["放量"]["n"] == BY_TAG_MIN, "够样本的标签没出组"


def test_scorecard_drops_agents_with_no_comparable_kind():
    """只有 kind='none' 的行 → 这个 agent 整块不出现（不是出现一个空块）。"""
    sc = build_scorecard(
        [_rec(kind="none", fwd=_fwd(t1=_h(H_OK, 0.01)))], today="2026-09-23"
    )
    assert sc["agents"] == {}
    assert sc["n_entries"] == 1
    assert sc["min_sample"] == MIN_SAMPLE


def test_render_flags_samples_below_the_minimum():
    """样本不足要在**终端输出**里看得见。

    隔壁把 MIN_SAMPLE 写进了 json 字段却从不打印，读终端的人只看到一行光秃秃的
    均值——这里必须显式出现。
    """
    thin = [_rec(fwd=_fwd(t1=_h(H_OK, 0.01))) for _ in range(3)]
    fat = [_rec(fwd=_fwd(t1=_h(H_OK, 0.01))) for _ in range(MIN_SAMPLE)]
    assert any(f"样本<{MIN_SAMPLE}" in ln for ln in render(build_scorecard(thin)))
    assert not any(f"样本<{MIN_SAMPLE}" in ln for ln in render(build_scorecard(fat))), (
        "够样本的也报了告警"
    )


def test_render_survives_an_empty_pool():
    """没有任何可比类别时给一句人话，而不是一个空标题（看起来像报表坏了）。"""
    lines = render(build_scorecard([]))
    assert len(lines) >= 3
    assert any("没有任何 agent" in ln for ln in lines)


# ── 导出形态 ────────────────────────────────────────────────────────
def test_pool_row_carries_the_neighbour_field_names():
    """导出形态要与存量池对得上：同名同义在前，本表自有字段在后。

    按 `(agent, date, code_raw, action)` 元组配对——`code` 是本仓归一后的前缀式、
    隔壁存的是模型原样写法，`code_raw` 才是隔壁口径；哈希两边也不可比。
    """
    row = _pool_row(
        _rec(
            code="SH600519",
            code_raw="600519.SH",
            action="buy",
            fwd=_fwd(t1=_h(H_OK, 0.01)),
            tags=("追高",),
            entry_date=date(2026, 9, 9),
            entry_px=10.0,
            tradable=True,
            confidence=0.7,
            reason="突破",
        )
    )
    for k in (
        "agent",
        "date",
        "ts",
        "action",
        "kind",
        "code",
        "code_raw",
        "confidence",
        "stop_loss",
        "take_profit",
        "reason",
        "entry_dt",
        "entry_px",
        "tradable",
        "tags",
        "fwd",
    ):
        assert k in row, f"隔壁同名键 {k} 丢了"
    assert row["entry_dt"] == 20260909, "入场日要整数形态（隔壁池如此）"
    assert row["code"] == "SH600519" and row["code_raw"] == "600519.SH"
    assert row["fwd"]["t1"]["state"] == H_OK, "state 是本表超集，不能丢"
    assert row["priced_at"] is None
    assert row["pool_ctx"] is None, "未插桩与「池外」是两件事，None 要保住"


def test_pool_row_entry_dt_is_none_before_pricing():
    row = _pool_row(_rec())
    assert row["entry_dt"] is None and row["entry_px"] is None
    assert row["fwd"] == {}, "没定过价是 {}，不是 None"
