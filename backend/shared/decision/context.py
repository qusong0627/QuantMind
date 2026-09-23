"""P2.2 上下文构建：一轮调仓的输入快照 → 提示词（**纯渲染，不取数**）。

本模块回答一个问题：「09:35 这一轮，喂给模型的那串字节到底长什么样」。取数
（读池、读持仓、读行情）全在 IO 适配层，这里只做渲染，所以可以拿隔壁
（quant-Trader）的**真实实现**产出金样逐字节对其
（``backend/tests/fixtures/decision_prompt_golden.json``，生成器
``docs/local/gen_decision_prompt_golden.py``）。

移植自 ``quant-Trader/scripts/live_llm_trade.build_prompt`` 与
``live_prompt_context`` 的四个渲染函数。**行序、空行位置、标点全按隔壁逐字节
对齐**——包括一处不对称：风险段 ``lines += [rb]``（无前置空行）、行业段
``lines += ["", ib]``（有）。看着像笔误，改了就丢了对差分能力，故原样保留。

刻意修复的三处（隔壁会炸或说谎，见 fixture 的 ``_meta.divergences``）：

1. ``day_chg is None`` —— 隔壁 ``f"{h['day_chg']:+.2f}%"`` 直接
   ``TypeError``（``lq_klines`` 只回 1 根 K 线就触发）。提示词构建失败 = 该轮
   0 决策，且失败点是**数据缺一天**这种常态。这里渲染 ``—``。
2. ``cost <= 0`` —— 隔壁渲染 ``成本 0`` / ``盈亏 +0.00%``，读起来像「不赚不亏」，
   实际是「两本账都没这个数」。按本仓口径（缺失一律 ``—``，绝不显示成 0）渲染 ``—``。
3. 候选行缺 ``score`` —— 隔壁 ``p.get('score', 0)`` 渲染 ``0.000``，看起来像
   「评分极低」而非「没分」。渲染 ``—``。

刻意**不做**的事：

* 不取数、不读文件、不连 Redis——``render_prompt`` 只吃已经拿到的快照；
* 不做预算裁剪（``plan_affordable`` 在 IO 侧调用本模块的 ``min_buy_cost`` 口径）；
* 不拼 ``watch``（守护意图）段的提示词——那是整点轮 schema，P2.4 的事；
* 不自己造 ``risk_block`` / ``industry_caution_block`` 的内容：QM 侧这两个数据源
  （隔壁 ``risk_block.json`` / ``industry_risk.json``）**目前不存在**，所以它们
  以「已渲染好的一段文本」入参，缺省空串即整段不出现。缺口登记在迁移计划 P2.2。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from collections.abc import Mapping, Sequence
from typing import Any

from backend.shared.decision.contract import REBALANCE_SCHEMA_JSON

__all__ = [
    "AGENT_QUOTA_TOTAL",
    "DEFAULT_MAX_NEW_BUYS",
    "DEFAULT_PER_STOCK_PCT",
    "MISSING",
    "QUOTE_MAX_AGE_MIN",
    "COST_SRC_BRIDGE",
    "COST_SRC_LEDGER",
    "DirectionBlock",
    "HoldingRow",
    "PoolQuote",
    "PoolRow",
    "RebalanceContext",
    "budget_filter_note",
    "ledger_cost_rows",
    "render_prompt",
    "render_quote_block",
]

#: 每个 agent 的虚拟额度（隔壁 ``live_ledger.AGENT_QUOTA``）。这不是账户真金白银，
#: 是「这个模型这一轮最多能动多少」的分账尺子；实盘总额由风控档位另管。
AGENT_QUOTA_TOTAL = 100_000.0

#: 单票买入 ≤ 剩余额度该比例（隔壁 ``PER_STOCK_PCT``；风控档位可覆盖）。
DEFAULT_PER_STOCK_PCT = 0.20
#: 单轮新开仓上限（隔壁 ``MAX_NEW_BUYS``；风控档位可覆盖）。
DEFAULT_MAX_NEW_BUYS = 3

#: 池内行情可用上限（分钟）。盘中采集节奏 5 分钟，午休段（11:30→12:00 轮）最长
#: 空档 30 分钟仍可用；隔夜/盘前一律剔除——盘前要定价请用日 K，别拿昨天的
#: 盘中快照当现价。
QUOTE_MAX_AGE_MIN = 45

COST_SRC_LEDGER = "ledger"
COST_SRC_BRIDGE = "bridge"

#: 缺失值的唯一渲染（本仓口径：绝不显示成 0）。
MISSING = "—"


@dataclass(frozen=True, slots=True)
class HoldingRow:
    """一只持仓（**桥口径原始值**，账本改写发生在 :func:`render_prompt` 内部）。

    ``cost`` / ``pnl_pct`` 是券商共享账户的混合成本口径——它把历史仓位和别的
    来源都算在同一个账户里，照抄给模型会把口径差当成盈亏（隔壁 glm 曾把实际
    浮亏 −2.8% 讲成 +83% 并据此决策）。所以渲染前一律过 :func:`ledger_cost_rows`。
    """

    code: str
    name: str
    volume: int
    cost: float
    price: float
    pnl_pct: float
    day_chg: float | None
    avail: int
    cost_src: str = COST_SRC_BRIDGE


@dataclass(frozen=True, slots=True)
class PoolRow:
    """一只候选标的。除 ``code`` 外全可选——池产物的字段完整度按市场而异。"""

    code: str
    name: str = ""
    industry: str = ""
    score: float | None = None
    fusion: float | None = None
    rank: int | str | None = None
    remark: str = ""


@dataclass(frozen=True, slots=True)
class PoolQuote:
    """候选池内一只的实时行情（桥口径采集）。

    ``age_min`` 是采集时间到本轮渲染时刻的分钟数；**过期项由 IO 层剔除并计数**，
    这里只渲染活下来的。``name`` 用池行的名称（采集记录里没有名称）。
    """

    code: str
    name: str
    price: float
    age_min: float
    pre_close: float | None = None
    signal_score: float | None = None


@dataclass(frozen=True, slots=True)
class DirectionBlock:
    """【今日大盘方向】：最新研究产出的一句话（+ 可选总分）。

    ``direction`` 由 IO 层负责兜底为 :data:`MISSING`——渲染器不做「这个值算不算
    缺失」的判断，边界校验在边界做。
    """

    direction: str = MISSING
    total_score: int | float | None = None


@dataclass(frozen=True, slots=True)
class RebalanceContext:
    """09:35 调仓轮的全部输入。

    额度三数只存两个（``quota_total``/``quota_used``），剩余额由属性导出——
    隔壁是三个独立入参，任一处算错就是「模型按错的剩余额度下单」，
    这里从结构上不给它们分家的机会（隔壁 ``agent_remaining`` 也是 quota−used）。
    """

    agent: str
    now: datetime
    holdings: tuple[HoldingRow, ...] = ()
    ledger_positions: Mapping[str, Any] = field(default_factory=dict)
    direction: DirectionBlock = field(default_factory=DirectionBlock)
    pool: tuple[PoolRow, ...] = ()
    quotes: tuple[PoolQuote, ...] = ()
    quotes_stale_count: int = 0
    extra_context: str = ""
    risk_block: str = ""
    industry_caution_block: str = ""
    quota_total: float = AGENT_QUOTA_TOTAL
    quota_used: float = 0.0
    per_stock_pct: float = DEFAULT_PER_STOCK_PCT
    max_new_buys: int = DEFAULT_MAX_NEW_BUYS

    @property
    def quota_remaining(self) -> float:
        return round(self.quota_total - self.quota_used, 2)

    @property
    def budget(self) -> float:
        """单票预算 = 剩余额度 × 单票比例（IO 侧按此裁剪候选池）。"""
        return self.quota_remaining * self.per_stock_pct


#: 成本列口径说明（逐字移植隔壁 ``live_prompt_context.LEDGER_COST_NOTE``）。
#: 这段话是 2026-09-12 那次「glm 把浮亏讲成 +83%」事故的补丁，措辞即契约。
LEDGER_COST_NOTE = (
    "口径说明：**成本列 = 你名下分账账本**（你自己的加权平均买入成本，加仓加权、"
    "减仓不动余仓成本），该列盈亏/% 均按此成本计算；数量与可卖量 = 桥账户实时口径，"
    "执行（可卖量/手数）一律以桥为准。两本账在成交回报到账前可能短暂不一致"
    "（账本数量滞后于桥），属正常对账窗口。成本列带 `*` = 账本暂无该票记录、"
    "暂用券商账户混合成本（可能含历史仓位，仅供参照，勿据此算盈亏）。"
)


def budget_filter_note(pct: float) -> str:
    """候选池已按资金量裁剪的说明（09:35 主入口与整点轮共用同一句，防口径漂移）。"""
    return (
        f"候选池已按你的资金量剔除买不起的标的（单票预算 = 剩余额度×{pct:.0%}；"
        "最小一手 100 股、科创板 200 股都超预算的票不会出现在表里），"
        "表里没有的代码不要报买入。"
    )


def ledger_cost_rows(
    rows: Sequence[HoldingRow], positions: Mapping[str, Any] | None
) -> tuple[HoldingRow, ...]:
    """持仓行 → 提示词展示副本：成本/浮盈改**分账账本**口径。

    账本里的 ``cost_price`` 才是「我这笔仓位赚没赚」的正确基准。账本无该票、
    成本 ≤0、或非有限值（NaN/Inf——``inf > 0`` 为真会让成本列渲染出 ¥inf、
    盈亏渲染出 +nan%，模型照着 nan% 决策）→ 回退桥值并标 :data:`COST_SRC_BRIDGE`，
    渲染端加 ``*``。

    **只改展示**：返回新对象，绝不改入参。桥口径的 ``pnl_pct`` 在别处还被
    波动触发基线、强制减仓与执行路径消费，交易判定一律不动。

    ``positions`` = ``{code: {"volume", "cost_price", ...}}``（账本里该 agent 的
    持仓段）。类型坏值（不是 Mapping、``cost_price`` 不是数）一律走回退分支，
    不许炸穿提示词——提示词构建失败等于该轮 0 决策。
    """
    if not isinstance(positions, Mapping):
        positions = {}
    out: list[HoldingRow] = []
    for r in rows:
        pos = positions.get(r.code)
        try:
            lc = float((pos or {}).get("cost_price") or 0)
        except (TypeError, ValueError, AttributeError):
            lc = 0.0
        if math.isfinite(lc) and lc > 0:
            out.append(
                HoldingRow(
                    code=r.code,
                    name=r.name,
                    volume=r.volume,
                    cost=round(lc, 2),
                    price=r.price,
                    pnl_pct=round((r.price - lc) / lc * 100, 2),
                    day_chg=r.day_chg,
                    avail=r.avail,
                    cost_src=COST_SRC_LEDGER,
                )
            )
        else:
            out.append(
                HoldingRow(
                    code=r.code,
                    name=r.name,
                    volume=r.volume,
                    cost=r.cost,
                    price=r.price,
                    pnl_pct=r.pnl_pct,
                    day_chg=r.day_chg,
                    avail=r.avail,
                    cost_src=COST_SRC_BRIDGE,
                )
            )
    return tuple(out)


# ── 单元格渲染 ────────────────────────────────────────────────────────


def _num(value: float | None, fmt: str) -> str:
    """数值单元格：缺失渲染 ``—``，**绝不**回落到 0（见模块头「刻意修复」）。"""
    if value is None or not isinstance(value, (int, float)):
        return MISSING
    if isinstance(value, float) and not math.isfinite(value):
        return MISSING
    return format(value, fmt)


def _pct(value: float | None) -> str:
    """百分比单元格。``%`` 跟着数值一起消失——缺失时渲染 ``—%`` 是把话说半截。"""
    text = _num(value, "+.2f")
    return text if text == MISSING else text + "%"


def _holding_row_line(h: HoldingRow) -> str:
    """一行持仓。价格/成本沿用隔壁的**裸 float 渲染**（``35.8`` 而非 ``35.80``）——
    逐字节对齐优先于好看。

    **成本缺失时盈亏一并列 ``—``**：盈亏是相对成本的差，成本没有则盈亏无从谈起。
    隔壁在这里给的是 ``成本 0 / 盈亏 +0.00%``——读起来像「不赚不亏」，
    实际是「两本账都没这个数」，是会被模型当成持有理由的假值。
    """
    mark = "" if h.cost_src == COST_SRC_LEDGER else "*"
    has_cost = math.isfinite(h.cost) and h.cost > 0
    cost_cell = format(h.cost, "") + mark if has_cost else MISSING
    return (
        f"| {h.code} | {h.name} | {h.volume} | {cost_cell} | {h.price} "
        f"| {_pct(h.pnl_pct) if has_cost else MISSING} | {_pct(h.day_chg)} | {h.avail} |"
    )


def _pool_row_line(p: PoolRow) -> str:
    rank = MISSING if p.rank is None else str(p.rank)
    return (
        f"| {rank} | {p.code} | {p.name} "
        f"| {p.industry} | {_num(p.score, '.3f')} "
        f"| {_num(p.fusion, '.3f')} | {p.remark} |"
    )


def render_quote_block(quotes: Sequence[PoolQuote], stale_count: int = 0) -> str:
    """【候选池实时行情】：池内已采集的现价 → 提示词。

    背景（隔壁 2026-09-08 上线）：候选池此前只注入 rank/score/行业、**没有现价**，
    模型无法给新标的定价，只能输出 hold，或把买入意图写成 watch（哨兵只执行卖出），
    实盘因此 10 卖 1 买。把采集到的池内实时价回灌，「是否买入」才是个可判断的问题。

    块首标注的是**最新那条**的新鲜度（``min(age)``）。没有任何一条可用 → 返回空串
    （整段不出现），**绝不臆造价格**。``stale_count`` 由 IO 层数过期项。
    """
    lines: list[str] = []
    aged: float | None = None
    for q in quotes:
        if q.price <= 0:
            continue
        age = q.age_min
        aged = age if aged is None else min(aged, age)
        chg = ""
        if q.pre_close is not None and q.pre_close > 0:
            chg = f"（{(q.price / q.pre_close - 1) * 100:+.2f}%）"
        sig = ""
        if q.signal_score is not None and isinstance(q.signal_score, (int, float)):
            sig = f" · 信号分 {q.signal_score:.1f}"
        lines.append(f"- {q.name} {q.code} 现价 ¥{q.price:.2f}{chg}{sig}")
    if not lines:
        return ""
    head = (
        "【候选池实时行情（桥口径，系统采集，"
        + (f"最新 {aged:.0f} 分钟前" if aged is not None else "新鲜度未知")
        + "）】"
    )
    tail = (
        "（这是可直接下单的定价依据：买入按现价+1%限价撮合，"
        "不必因『查不到价』而放弃；未列出的标的本轮无实时价，不要凭记忆报价）"
    )
    if stale_count:
        tail += f"（另有 {stale_count} 只池内标的行情已过期，已剔除）"
    return head + "\n" + "\n".join(lines) + "\n" + tail


# ── 提示词 ────────────────────────────────────────────────────────────

_HOLDINGS_HEADER = (
    "【你名下的现有持仓】（成本与盈亏% = 你名下分账账本口径；现价/数量/可卖量 = "
    "桥账户实时口径，可卖量 0 = 今日买入 T+1 不可卖）：",
    "",
    "| 代码 | 名称 | 数量 | 成本 | 现价 | 盈亏% | 今日涨跌% | 可卖量 |",
    "|------|------|------|------|------|-------|-----------|--------|",
)

_POOL_HEADER = (
    "【候选池】（最新研究池：总分/融合分/备注；池内没有方向标签——"
    "买卖由你按 分数+大盘+板块+新闻分子 综合判断，HOLD/BUY 侧标签一律不作为依据）",
    "",
    "| 排名 | 代码 | 名称 | 行业 | 总分 | 融合分 | 备注 |",
    "|------|------|------|------|------|--------|------|",
)


def _direction_line(d: DirectionBlock) -> str:
    if d.total_score is None:
        return d.direction
    return f"{d.direction}（总分 {d.total_score}/11）"


def _decision_rule_lines(ctx: RebalanceContext) -> list[str]:
    return [
        "",
        "【决策规则】",
        "1. 逐只现有持仓判断：hold（继续持有）/ sell（减仓或清仓换股）。"
        "如果现有持股趋势/基本面仍优于候选池，可以全部 hold 不换股。",
        "2. 需要买入时从候选池选：优先分数高、行业顺大盘方向的；"
        "允许换仓（同轮先 sell 再 buy），以信号分数+板块主线+新闻分子综合权衡，不必拘泥原有持仓。",
        f"3. sell 的 pct = 卖出可卖量的比例（0~1）；buy 的 pct = 使用剩余额度的比例"
        f"（每票 ≤{ctx.per_stock_pct:.0%}，当日新开仓 ≤{ctx.max_new_buys} 只；"
        f"超出的会被闸门裁掉）。" + budget_filter_note(ctx.per_stock_pct),
        "4. T+1：可卖量 0 的持仓不能卖。ST/*ST/退市整理股与黑名单标的**不可买入**（闸门硬拦）。",
        "5. 输出**严格 JSON**（不要 markdown 代码块、不要额外文字），格式：",
        REBALANCE_SCHEMA_JSON,
    ]


def _pool_lines(ctx: RebalanceContext) -> list[str]:
    if not ctx.pool:
        return []
    lines = list(_POOL_HEADER)
    lines += [_pool_row_line(p) for p in ctx.pool]
    qb = render_quote_block(ctx.quotes, ctx.quotes_stale_count)
    if qb:
        lines += ["", qb]
    return lines


def render_prompt(ctx: RebalanceContext) -> str:
    """一轮调仓的完整提示词。

    ``ctx.holdings`` 必须是**桥口径原始行**——账本改写在这里面做，不给调用方
    漏掉的机会（漏掉的后果是模型拿着混合成本算盈亏，见 :class:`HoldingRow`）。
    """
    display = ledger_cost_rows(ctx.holdings, ctx.ledger_positions)
    lines = [
        f"现在是北京时间 {ctx.now:%F %T}（开盘后）。你是 {ctx.agent} 的 A股实盘调仓决策模型，"
        f"管理 ¥{ctx.quota_total:,.0f} 虚拟额度（已用 ¥{ctx.quota_used:,.0f}，"
        f"剩余 ¥{ctx.quota_remaining:,.0f}）。",
        "",
        *_HOLDINGS_HEADER,
    ]
    lines += [_holding_row_line(h) for h in display]
    lines += [
        "",
        LEDGER_COST_NOTE,
        "",
        "【今日大盘方向】（最新研究产出）：",
        _direction_line(ctx.direction),
        "",
    ]
    if ctx.extra_context:
        lines += [ctx.extra_context, ""]
    lines += _pool_lines(ctx)
    # 事件风险警示：持仓命中提示退出、候选命中别选（闸门已硬拦买入，这里省一轮
    # 无效决策）。**无前置空行**——与隔壁逐字节一致。
    if ctx.risk_block:
        lines += [ctx.risk_block]
    # 行业整体劣化：赛道级软提示，不硬拦。有前置空行。
    if ctx.industry_caution_block:
        lines += ["", ctx.industry_caution_block]
    lines += _decision_rule_lines(ctx)
    return "\n".join(lines)
