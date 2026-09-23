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
    render_prompt,
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
