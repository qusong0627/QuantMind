"""P2.2 上下文构建：金样重放 + 渲染边界。

金样 ``fixtures/decision_prompt_golden.json`` 由隔壁（quant-Trader）**真实实现**
产出（``live_llm_trade.build_prompt`` + ``live_prompt_context.*``，生成器
``docs/local/gen_decision_prompt_golden.py``）。纪律同 P2.1a：期望值不是
「跑一遍现在代码存下来」，而是与独立实现逐行差分对齐后才写死。

三类断言：
1. **逐字节复现** —— 隔壁没炸的用例，``render_prompt`` 输出必须**完全相同**；
2. **刻意分叉** —— 隔壁会炸或渲染假值的三个用例，钉住 QM 的渲染，
   且钉住「隔壁当时错在哪」（假值/异常类型记在 fixture 里，改了就红）；
3. **边界** —— 账本口径回退、额度三数一致、缺值不显示成 0。
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from backend.shared.decision import context as ctxmod
from backend.shared.decision.contract import (
    INTRADAY_SCHEMA_JSON,
    REBALANCE_SCHEMA_JSON,
    SCHEMA_INTRADAY,
    SCHEMA_REBALANCE,
    parse_decisions,
)
from backend.shared.decision.context import (
    COST_SRC_BRIDGE,
    COST_SRC_LEDGER,
    MISSING,
    DirectionBlock,
    HoldingRow,
    PoolQuote,
    PoolRow,
    RebalanceContext,
    budget_filter_note,
    ledger_cost_rows,
    render_intraday_prompt,
    render_prompt,
    render_prompt_for,
    render_quote_block,
)

GOLDEN = Path(__file__).parent / "fixtures" / "decision_prompt_golden.json"
CN = ZoneInfo("Asia/Shanghai")


@pytest.fixture(scope="module")
def golden() -> dict:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# fixture → 本模块入参（金样是隔壁的数据形状，这里是翻译层）
# ---------------------------------------------------------------------------


def _holding(d: dict) -> HoldingRow:
    return HoldingRow(
        code=d["code"],
        name=d["name"],
        volume=int(d["volume"]),
        cost=float(d["cost"]),
        price=float(d["price"]),
        pnl_pct=float(d["pnl_pct"]),
        day_chg=None if d.get("day_chg") is None else float(d["day_chg"]),
        avail=int(d["avail"]),
    )


def _pool_row(d: dict) -> PoolRow:
    return PoolRow(
        code=d["code"],
        name=d.get("name", ""),
        industry=d.get("industry", ""),
        score=None if d.get("score") is None else float(d["score"]),
        fusion=None if d.get("fusion") is None else float(d["fusion"]),
        rank=d.get("rank"),
        remark=d.get("remark", ""),
    )


def _with_blocks(
    ctx: RebalanceContext, *, risk: str = "", industry: str = ""
) -> RebalanceContext:
    """金样把风险/行业两段替身化（QM 无对应数据源），重放时补齐替身内容。"""
    return replace(ctx, risk_block=risk, industry_caution_block=industry)


def _spec_to_ctx(
    spec: dict, quotes: tuple[PoolQuote, ...] = (), stale_count: int = 0
) -> RebalanceContext:
    """金样 ``inputs`` → :class:`RebalanceContext`（隔壁数据形状 → 本模块入参）。"""
    ledger = spec["ledger"]
    positions = ((ledger.get("agents") or {}).get(spec["agent"]) or {}).get(
        "positions"
    ) or {}
    return RebalanceContext(
        agent=spec["agent"],
        now=datetime(2026, 9, 23, 9, 35, 12, tzinfo=CN),
        holdings=tuple(_holding(h) for h in spec["holdings"]),
        ledger_positions=positions,
        direction=DirectionBlock(
            direction=spec["direction"].get("direction", MISSING),
            total_score=spec["direction"].get("total_score"),
        ),
        pool=tuple(_pool_row(p) for p in spec["pool"]),
        quotes=quotes,
        quotes_stale_count=stale_count,
        extra_context=spec["extra"],
        quota_total=ctxmod.AGENT_QUOTA_TOTAL,
        quota_used=spec["used"],
    )


#: 金样里被替身化的两段（内容由生成器给定，见其 ``RISK_BLOCK_STUB``）。
_RISK_STUB = "【事件风险警示（系统风险清单，硬约束）】\n- 000858.SZ 五粮液：解禁窗口内（2026-09-25）"
_INDUSTRY_STUB = "【行业整体劣化（赛道级软提示，不硬拦）】\n- 酿酒：近 20 日长期排除清单占比 18.4%，全市场基线 4.1%"
#: 金样里被替身化的池内行情（生成器的 ``L2_FACTORS``，已按新鲜度筛过）。
_FULL_QUOTES = (
    PoolQuote(
        code="600519.SH",
        name="贵州茅台",
        price=1523.40,
        age_min=1.17,
        pre_close=1501.00,
        signal_score=88.2,
    ),
    PoolQuote(
        code="000858.SZ",
        name="五粮液",
        price=128.66,
        age_min=2.03,
        pre_close=130.10,
        signal_score=71.5,
    ),
)


# ---------------------------------------------------------------------------
# 1. 逐字节复现
# ---------------------------------------------------------------------------


def test_full_prompt_matches_baymax_byte_for_byte(golden: dict) -> None:
    """隔壁的成功用例：输出必须逐字节相同（含空行位置与那处不对称）。"""
    case = next(c for c in golden["cases"] if c["name"] == "full")
    ctx = _spec_to_ctx(case["inputs"], _FULL_QUOTES, stale_count=1)
    ctx = _with_blocks(ctx, risk=_RISK_STUB, industry=_INDUSTRY_STUB)
    assert render_prompt(ctx) == case["baymax_prompt"]


def test_minimal_prompt_matches_baymax_byte_for_byte(golden: dict) -> None:
    """空持仓 + 空池：表头与规则仍要在，且两段风险提示整段不出现。"""
    case = next(c for c in golden["cases"] if c["name"] == "minimal")
    out = render_prompt(_spec_to_ctx(case["inputs"]))
    assert out == case["baymax_prompt"]
    assert "【事件风险警示" not in out
    assert "【候选池】" not in out
    assert "【候选池实时行情" not in out


def test_golden_is_not_a_selfie(golden: dict) -> None:
    """金样必须可追溯到隔壁的独立实现——改成本仓生成的快照就失去差分能力。"""
    meta = golden["_meta"]
    assert "live_llm_trade.build_prompt" in meta["source"]
    assert "gen_decision_prompt_golden" in meta["generated_by"]
    assert meta["pinned"]["agent_quota"] == 100_000.0


# ---------------------------------------------------------------------------
# 2. 刻意分叉（隔壁会炸 / 渲染假值）
# ---------------------------------------------------------------------------


def test_divergence_recorded_in_golden(golden: dict) -> None:
    """金样必须**写明隔壁错在哪**——只钉我们的修复，等于没记录分叉。

    集合**逐个列名**（不是 ``>=``）：新增分叉时必须来这条测试里登记，
    否则「悄悄多改了一处」没人会发现。
    """
    d = golden["_meta"]["divergences"]
    assert set(d) == {
        # P2.2 渲染侧
        "day_chg_none",
        "cost_nonpositive",
        "pool_score_missing",
        # P2.3a 调用侧（详情与断言在 test_decision_llm_client.py）
        "llm_not_configured",
        "usage_missing_keys",
        # 2026-10-09 重试纠正语（断言在 test_decision_contract.TestSchemaLiterals）
        "zero_holdings_retry_hint",
    }
    by_name = {c["name"]: c for c in golden["divergent_cases"]}
    assert by_name["day_chg_none"]["baymax_raises"] == "TypeError"
    assert "NoneType" in by_name["day_chg_none"]["baymax_error"]
    # 另两条不抛：钉住隔壁渲染出的**假值**原样
    # （真语料见过 002141.SZ 成本 −11.02 → 盈亏 −164.27%，见 fixture 生成器注释）
    assert (
        "| 002141.SZ | 贤丰控股 | 100 | -11.02* | 7.08 | -164.27% | +0.00% | 100 |"
        in (by_name["cost_nonpositive"]["baymax_prompt"])
    )
    assert (
        "| 300750.SZ | 宁德时代 | 300 | 0* | 241.5 | +0.00% | +2.11% | 300 |"
        in (by_name["cost_nonpositive"]["baymax_prompt"])
    )
    assert (
        "| 1 | 600519.SH | 贵州茅台 | 酿酒 | 0.000 | 0.873 | 分数列缺失 |"
        in (by_name["pool_score_missing"]["baymax_prompt"])
    )


def test_day_chg_none_renders_missing_instead_of_crashing(golden: dict) -> None:
    """``day_chg is None``：隔壁 TypeError（提示词构建失败 = 该轮 0 决策）。

    涨跌列渲染 ``—``（不是 ``—%``：``%`` 跟着数值一起消失）；该用例账本为空，
    成本列因此是桥值 + ``*``。
    """
    case = next(c for c in golden["divergent_cases"] if c["name"] == "day_chg_none")
    out = render_prompt(_spec_to_ctx(case["inputs"]))
    assert "| 600036.SH | 招商银行 | 1200 | 33.21* | 35.8 | +7.80% | — | 1200 |" in out
    assert "None" not in out


@pytest.mark.parametrize(
    ("code", "name", "volume", "price", "day_chg"),
    [
        ("002141.SZ", "贤丰控股", 100, "7.08", "+0.00%"),
        ("300750.SZ", "宁德时代", 300, "241.5", "+2.11%"),
    ],
)
def test_nonpositive_cost_renders_missing_not_a_fake_pnl(
    golden: dict, code: str, name: str, volume: int, price: str, day_chg: str
) -> None:
    """成本 ≤0 = 两本账都没这个数，不是「不赚不亏」，更不是 −164%。

    成本与**盈亏一起**列 ``—``：盈亏是相对成本的差，成本没有则盈亏无从谈起。
    涨跌列不受影响（它不依赖成本）。隔壁把这一行原样喂给了模型。
    """
    case = next(c for c in golden["divergent_cases"] if c["name"] == "cost_nonpositive")
    out = render_prompt(_spec_to_ctx(case["inputs"]))
    # 行形状本身就把话钉死了：成本列 `—`、盈亏列 `—`、涨跌列照旧（它不依赖成本）
    assert (
        f"| {code} | {name} | {volume} | — | {price} | — | {day_chg} | {volume} |"
        in out
    )
    assert "-11.02" not in out
    assert "-164.27" not in out


def test_pool_score_missing_renders_missing_not_zero(golden: dict) -> None:
    """缺 score ≠ 评分 0.000（后者会被读成「评级极低」）。"""
    case = next(
        c for c in golden["divergent_cases"] if c["name"] == "pool_score_missing"
    )
    out = render_prompt(_spec_to_ctx(case["inputs"]))
    assert "| 1 | 600519.SH | 贵州茅台 | 酿酒 | — | 0.873 | 分数列缺失 |" in out
    assert "0.000" not in out


# ---------------------------------------------------------------------------
# 3. 账本口径（P1-1：混合成本会翻转盈亏结论）
# ---------------------------------------------------------------------------


def _row(
    code: str = "600036.SH", cost: float = 33.21, price: float = 35.80
) -> HoldingRow:
    return HoldingRow(
        code=code,
        name="招商银行",
        volume=1200,
        cost=cost,
        price=price,
        pnl_pct=round((price - cost) / cost * 100, 2),
        day_chg=1.24,
        avail=1200,
    )


def test_ledger_cost_wins_and_pnl_is_recomputed() -> None:
    """账本成本覆盖桥值，盈亏**按账本成本重算**——不重算就是拿桥成本算账本盈亏。"""
    (out,) = ledger_cost_rows([_row()], {"600036.SH": {"cost_price": 31.05}})
    assert out.cost == 31.05
    assert out.cost_src == COST_SRC_LEDGER
    assert out.pnl_pct == pytest.approx(round((35.80 - 31.05) / 31.05 * 100, 2))
    assert out.pnl_pct != _row().pnl_pct


def test_falls_back_to_bridge_cost_when_ledger_has_no_record() -> None:
    (out,) = ledger_cost_rows([_row()], {})
    assert out.cost == 33.21 and out.cost_src == COST_SRC_BRIDGE


@pytest.mark.parametrize("bad", [0, -1.0, float("nan"), float("inf"), None, "x"])
def test_dirty_ledger_cost_falls_back(bad: object) -> None:
    """0 / 负 / NaN / Inf / 非数 一律回退桥值——不判 NaN/Inf 会渲染出 ¥inf、+nan%。"""
    (out,) = ledger_cost_rows([_row()], {"600036.SH": {"cost_price": bad}})
    assert out.cost_src == COST_SRC_BRIDGE and out.cost == 33.21


def test_ledger_cost_rows_does_not_mutate_input() -> None:
    """桥口径的 ``pnl_pct`` 还被波动基线/强制减仓消费，展示层绝不许改它。"""
    rows = [_row()]
    ledger_cost_rows(rows, {"600036.SH": {"cost_price": 31.05}})
    assert rows[0].cost == 33.21
    assert rows[0].pnl_pct == _row().pnl_pct


def test_ledger_volume_caps_displayed_position() -> None:
    """数量/可卖量按**本 agent 账本持有量**展示，不照抄共享桥账户的合计。

    同一只票可被多家模型同时持有（601985 实测 500/800/800，桥 avail=2100）。
    照抄桥值，模型会以为自己手里有 2100 股并按 100% 报卖 → 三家合计三倍超卖。
    """
    bridge = replace(_row(code="601985.SH"), volume=2100, avail=2100)
    (out,) = ledger_cost_rows(
        [bridge], {"601985.SH": {"cost_price": 9.0, "volume": 800}}
    )
    assert out.volume == 800
    assert out.avail == 800


def test_ledger_volume_never_exceeds_bridge_availability() -> None:
    """T+1 锁定只有桥知道：账本 1200 股、桥今天只可卖 300 → 展示 300。"""
    (out,) = ledger_cost_rows(
        [replace(_row(), avail=300)],
        {"600036.SH": {"cost_price": 31.05, "volume": 1200}},
    )
    assert out.volume == 1200 and out.avail == 300


def test_ledger_without_volume_keeps_bridge_quantities() -> None:
    """账本只记成本（旧数据/测试桩）→ 数量口径保持桥值，避免凭空归零。"""
    (out,) = ledger_cost_rows([_row()], {"600036.SH": {"cost_price": 31.05}})
    assert out.volume == 1200 and out.avail == 1200


def test_broken_positions_payload_does_not_crash() -> None:
    """账本结构坏了（不是 Mapping / 值是字符串）→ 回退，提示词不许炸。"""
    for payload in (None, [], "oops", {"600036.SH": "not-a-dict"}):
        (out,) = ledger_cost_rows([_row()], payload)  # type: ignore[arg-type]
        assert out.cost_src == COST_SRC_BRIDGE


def test_bridge_fallback_is_marked_with_star() -> None:
    """``*`` 是给模型看的口径标记：账本口径不带，回退桥值带（两种来源必须可辨）。"""
    ctx = RebalanceContext(
        agent="flash",
        now=datetime(2026, 9, 23, 9, 35, 12, tzinfo=CN),
        holdings=(_row(),),
        ledger_positions={"600036.SH": {"cost_price": 31.05}},
    )
    assert "| 31.05 |" in render_prompt(ctx)
    assert "| 33.21* |" in render_prompt(replace(ctx, ledger_positions={}))


# ---------------------------------------------------------------------------
# 4. 额度三数一致 / 预算
# ---------------------------------------------------------------------------


def test_remaining_is_derived_never_passed_in_separately() -> None:
    """剩余额由 quota−used 导出：三个独立入参迟早会算出互相矛盾的提示词。"""
    ctx = RebalanceContext(agent="flash", now=datetime.now(CN), quota_used=38_760.0)
    assert ctx.quota_remaining == 61_240.0
    assert ctx.budget == pytest.approx(61_240.0 * 0.20)
    assert "剩余 ¥61,240" in render_prompt(ctx)


def test_risk_driven_limits_reach_the_prompt() -> None:
    """风控档位覆盖（defensive：单票 10%、新开仓 1 只）必须落到规则第 3 条。"""
    ctx = RebalanceContext(
        agent="flash",
        now=datetime.now(CN),
        per_stock_pct=0.10,
        max_new_buys=1,
    )
    out = render_prompt(ctx)
    assert "每票 ≤10%，当日新开仓 ≤1 只" in out
    assert budget_filter_note(0.10) in out


# ---------------------------------------------------------------------------
# 5. 候选池实时行情块
# ---------------------------------------------------------------------------


def test_quote_block_reports_freshest_age_and_stale_tail() -> None:
    q = (
        PoolQuote(
            "600519.SH", "贵州茅台", 1523.40, 1.17, pre_close=1501.00, signal_score=88.2
        ),
        PoolQuote("000858.SZ", "五粮液", 128.66, 12.4, pre_close=130.10),
    )
    block = render_quote_block(q, stale_count=1)
    assert "最新 1 分钟前" in block  # min(age)，不是最后一条
    assert "- 贵州茅台 600519.SH 现价 ¥1523.40（+1.49%） · 信号分 88.2" in block
    assert "- 五粮液 000858.SZ 现价 ¥128.66（-1.11%）" in block
    assert "另有 1 只池内标的行情已过期，已剔除" in block
    assert "不要凭记忆报价" in block


def test_quote_block_omits_change_and_score_when_unknown() -> None:
    """没有昨收就不给涨跌（不臆造），没有信号分就不给分。"""
    block = render_quote_block((PoolQuote("600000.SH", "浦发银行", 9.87, 3.0),))
    quote_line = block.splitlines()[1]
    assert quote_line == "- 浦发银行 600000.SH 现价 ¥9.87"
    assert "信号分" not in block


def test_quote_block_empty_when_nothing_usable() -> None:
    """一条可用都没有 → 整段不出现（绝不臆造价格）。"""
    assert render_quote_block(()) == ""
    assert render_quote_block((PoolQuote("600000.SH", "浦发银行", 0.0, 3.0),)) == ""


def test_prompt_without_quotes_has_no_quote_section() -> None:
    ctx = RebalanceContext(
        agent="flash",
        now=datetime.now(CN),
        pool=(PoolRow(code="600519.SH", name="贵州茅台", score=0.9),),
    )
    out = render_prompt(ctx)
    assert "【候选池】" in out and "【候选池实时行情" not in out


# ---------------------------------------------------------------------------
# 6. 块序（模型读到的是顺序，顺序错了语义就错了）
# ---------------------------------------------------------------------------


def test_block_order_holdings_direction_pool_risk_rules() -> None:
    ctx = RebalanceContext(
        agent="flash",
        now=datetime.now(CN),
        holdings=(_row(),),
        direction=DirectionBlock("偏多", 7),
        pool=(PoolRow(code="600519.SH", name="贵州茅台", score=0.9),),
        quotes=(PoolQuote("600519.SH", "贵州茅台", 1523.40, 1.0),),
        extra_context="【盘面状态】…",
        risk_block="【事件风险警示】…",
        industry_caution_block="【行业整体劣化】…",
    )
    out = render_prompt(ctx)
    order = [
        out.index("【你名下的现有持仓】"),
        out.index("【今日大盘方向】"),
        out.index("【盘面状态】"),
        out.index("【候选池】"),
        out.index("【候选池实时行情"),
        out.index("【事件风险警示】"),
        out.index("【行业整体劣化】"),
        out.rindex("【决策规则】"),
    ]
    assert order == sorted(order)


# ---------------------------------------------------------------------------
# 7. 盘中轮提示词（四段式 + watch schema）——arena「模型对话」的分段依据
# ---------------------------------------------------------------------------


def _intraday_ctx() -> RebalanceContext:
    """带满块的盘中轮快照（持仓 + 池 + 行情 + 两个软块，覆盖所有可选段）。"""
    return RebalanceContext(
        agent="flash",
        now=datetime(2026, 10, 14, 13, 0, 0, tzinfo=CN),
        holdings=(_row(),),
        direction=DirectionBlock("偏空", 4),
        pool=(PoolRow(code="600519.SH", name="贵州茅台", score=0.9),),
        quotes=(PoolQuote("600519.SH", "贵州茅台", 1523.40, 1.0),),
        extra_context="【盘面状态】…",
        risk_block="【事件风险警示】…",
        industry_caution_block="【行业整体劣化】…",
    )


def test_intraday_prompt_has_the_four_section_markers() -> None:
    """四段式标记必须齐、且按 ①②③④ 顺序出现。

    这是**前端契约**：``parseAnalysis.ts`` 用 ``/①|【总体总结】/`` …
    ``/④|【决策】/`` 四处 ``indexOf`` 切段，任一标记缺席或乱序，切出来的段就
    串位（总结栏拿到决策块、决策卡拿到散文）。改标记 = 同时改前端。
    """
    out = render_intraday_prompt(_intraday_ctx())
    idx = [
        out.index(m)
        for m in ("①【总体总结】", "②【分析链路】", "③【推理论证】", "④【决策】")
    ]
    assert idx == sorted(idx)


def test_intraday_prompt_carries_watch_schema_not_the_rebalance_one() -> None:
    """盘中轮的输出契约是 ``INTRADAY_SCHEMA_JSON``——含 watch 一族字段。

    反面同样要钉：调仓轮的 ``REBALANCE_SCHEMA_JSON`` 里没有 ``watch``，
    它出现在盘中提示词里会让模型漏掉条件位（实测 ``watch_armed`` 恒 0 的根因）。
    """
    out = render_intraday_prompt(_intraday_ctx())
    assert INTRADAY_SCHEMA_JSON in out
    assert REBALANCE_SCHEMA_JSON not in out
    assert "watch" in out and "stop_loss" in out and "move_stop" in out


def test_rebalance_prompt_gains_no_four_section_marker() -> None:
    """调仓轮一个字不改：四段式是**盘中轮专用**收尾。

    调仓轮的字节被隔壁金样钉死（见本文件 1/2 节），这里再钉一次语义面：
    它不该出现任何 ①②③④ 标记，否则前端会把一份纯 JSON 的轮次也当成四段式切。
    """
    out = render_prompt(_intraday_ctx())
    assert "【总体总结】" not in out and "①" not in out
    assert REBALANCE_SCHEMA_JSON in out and INTRADAY_SCHEMA_JSON not in out


def test_both_slots_see_the_identical_body_bytes() -> None:
    """两槽位的事实面**逐字节相同**——只有收尾段不同。

    分开渲染两遍事实（持仓/大盘/池/风险块）是这类双槽位最容易长出来的漂移：
    某天给盘中轮单独加一列，模型在两个槽位看到的账户就不是同一个了。
    """
    ctx = _intraday_ctx()
    intraday, rebalance = render_intraday_prompt(ctx), render_prompt(ctx)
    # 调仓轮的正文（收尾段之前）必须是盘中轮的**前缀**——同一份字节，逐字相同
    head = rebalance.split("【决策规则】")[0]
    assert head.startswith("现在是北京时间")
    assert intraday.startswith(head)


def test_intraday_rules_render_the_round_limits() -> None:
    """收尾段里的额度/新开仓上限随 ctx 走（风控档位会覆写这两个数）。"""
    ctx = replace(_intraday_ctx(), per_stock_pct=0.08, max_new_buys=1)
    out = render_intraday_prompt(ctx)
    assert "≤8%" in out and "≤1 只" in out
    assert "≤20%" not in out and "≤3 只" not in out


def test_render_prompt_for_dispatches_on_schema() -> None:
    """调用方只认这一个入口：schema 决定提示词。"""
    ctx = _intraday_ctx()
    assert render_prompt_for(ctx, SCHEMA_INTRADAY) == render_intraday_prompt(ctx)
    assert render_prompt_for(ctx, SCHEMA_REBALANCE) == render_prompt(ctx)
    # 未知 schema 回退最保守的那份（纯 JSON、无 watch），不让新 schema 静默
    # 继承 watch 提示词——白名单对不上时报错该由 parse_decisions 抛。
    assert render_prompt_for(ctx, "whatever") == render_prompt(ctx)


def test_fenced_decision_block_as_the_prompt_asks_is_parseable() -> None:
    """提示词要什么，解析器就得吃什么——四段式散文 + ```json 围栏。

    这一条把「提示词契约」与「解析契约」焊在一起：改动任一侧而忘了另一侧，
    模型会按新格式输出、解析器却取不出决策，整轮 ``llm_failed``。
    """
    reply = (
        "①【总体总结】仓位轻，持有为主。（置信度 中）\n\n"
        "②【分析链路】桥快照 → 13:00 现价，新鲜；账本成本已按分账口径改写。\n\n"
        "③【推理论证】大盘震荡 → 港口/石油顺风 → 国轩高科逆风。\n\n"
        "④【决策】\n```json\n"
        '{"decisions": [{"action": "watch", "code": "600338.SH", '
        '"stop_loss": 11.5, "pct": 0.3, "reason": "跌破 11.5 减三成"}]}\n'
        "```\n"
    )
    batch = parse_decisions(reply, schema=SCHEMA_INTRADAY)
    assert batch.ok
    assert [d.action for d in batch.decisions] == ["watch"]
    assert batch.decisions[0].stop_loss == 11.5


# ---------------------------------------------------------------------------
# 现价取数口径（2026-10-15 审计修）：主源价 / 桥兜底标记 / legacy 无痕
# ---------------------------------------------------------------------------


def _price_row(*, price: float, price_src: str) -> HoldingRow:
    return HoldingRow(
        code="601857.SH",
        name="中国石油",
        volume=1500,
        cost=12.33,
        price=price,
        pnl_pct=round((price - 12.33) / 12.33 * 100, 2),
        day_chg=None,
        avail=700,
        price_src=price_src,
    )


def _render_one_holding(row: HoldingRow) -> str:
    return render_prompt(
        RebalanceContext(
            agent="flash",
            now=datetime(2026, 10, 15, 13, 0, tzinfo=CN),
            holdings=(row,),
            direction=DirectionBlock("偏多"),
        )
    )


def test_live_source_price_renders_note_without_marker() -> None:
    """主源价（snap/series）：数字照用、无 ``‡``，口径注出现。"""
    out = _render_one_holding(_price_row(price=12.26, price_src="snap"))
    assert "| 601857.SH | 中国石油 | 1500 | 12.33* | 12.26 |" in out
    assert "12.26‡" not in out  # 口径注本身含 ‡ 字样，钉的是**价格格**不带标记
    assert ctxmod.PRICE_SRC_NOTE in out


def test_bridge_fallback_price_is_marked_and_explained() -> None:
    """桥兜底价：``‡`` 标记 + 口径注（桥价当日实测全天=昨收）。"""
    out = _render_one_holding(
        _price_row(price=12.55, price_src=ctxmod.PRICE_SRC_BRIDGE)
    )
    assert "| 601857.SH | 中国石油 | 1500 | 12.33* | 12.55‡ |" in out
    assert ctxmod.PRICE_SRC_NOTE in out


def test_legacy_rows_keep_the_prompt_free_of_markers_and_notes() -> None:
    """``price_src=""``（legacy/金样翻译层）：无 ``‡``、无口径注——逐字节冻结路径。"""
    out = _render_one_holding(_price_row(price=12.55, price_src=""))
    assert "‡" not in out
    assert ctxmod.PRICE_SRC_NOTE not in out
    assert "注：**现价" not in out


# ---------------------------------------------------------------------------
# 8. 2026-10-28 实盘资金策略：「股票不限」+ 盈利优先
# ---------------------------------------------------------------------------


def test_out_of_pool_scope_is_rendered_in_both_slots() -> None:
    """池外可买时两个槽位都改口径：不再说「表里没有的代码不要报买入」。"""
    ctx = replace(
        _intraday_ctx(),
        allow_out_of_pool=True,
        per_stock_pct=1.0,
        max_new_buys=9,
    )
    for out in (render_prompt(ctx), render_intraday_prompt(ctx)):
        assert "池外 A 股也可买" in out
        assert "表里没有的代码不要报买入" not in out
        assert "≤100%" in out


def test_default_prompt_keeps_pool_only_language() -> None:
    """默认关（历史/金样路径）：池外口径一个字都不出现。"""
    out = render_prompt(_intraday_ctx())
    assert "表里没有的代码不要报买入" in out
    assert "池外 A 股也可买" not in out


def test_profit_first_block_reaches_both_slots() -> None:
    ctx = replace(_intraday_ctx(), profit_first=True)
    for out in (render_prompt(ctx), render_intraday_prompt(ctx)):
        assert "【盈利优先】" in out
        assert "最大化净值增长" in out


def test_profit_first_is_off_by_default() -> None:
    assert "【盈利优先】" not in render_prompt(_intraday_ctx())


def test_session_phase_keeps_trading_hours_byte_identical() -> None:
    """提示词时段标签：交易时段仍是「开盘后」（金样），非交易时段如实标注。"""
    tz = ZoneInfo("Asia/Shanghai")

    def at(day: int, hour: int, minute: int) -> str:
        return ctxmod._session_phase(
            datetime(2026, 10, day, hour, minute, tzinfo=tz)
        )

    assert at(30, 8, 30) == "盘前"
    assert at(30, 9, 30) == "开盘后"
    assert at(30, 11, 30) == "开盘后"
    assert at(30, 12, 0) == "午间休市"
    assert at(30, 13, 0) == "开盘后"
    assert at(30, 15, 0) == "开盘后"
    assert at(30, 15, 30) == "盘后"
    assert at(31, 10, 0) == "周末休市"  # 2026-10-31 周六
