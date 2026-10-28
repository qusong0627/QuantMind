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
* 不自己造 ``risk_block`` / ``industry_caution_block`` 的内容：这两个数据源
  （``data/risk_block.json`` / ``industry_risk.json`` / ``fundamental_flags.json``）
  由 ``shared/decision_risk_blocks`` 渲染成文本后入参（取数在决策轮 feeds 层，
  Stage 2 第二批起接线），缺省空串即整段不出现。

两套收尾（正文共用，见 :func:`_context_body`）：

* :func:`render_prompt` —— **调仓轮**（09:35/10:05/11:05），收尾是「严格 JSON」
  + ``REBALANCE_SCHEMA_JSON``，逐字节对齐隔壁金样；
* :func:`render_intraday_prompt` —— **盘中轮**（其余 8 个槽位），收尾是四段式
  （前端 arena「模型对话」按段渲染，见 ``contract.ANALYSIS_SECTIONS``）
  + ``INTRADAY_SCHEMA_JSON``（含 ``watch``/``stop_loss``/``take_profit``/``move_stop``）。

调用方一律走 :func:`render_prompt_for` 按槽位 schema 分派，不要自己写 if。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from collections.abc import Mapping, Sequence
from typing import Any

from backend.shared.decision.agent_ledger import holding_age_days
from backend.shared.decision.contract import (
    ANALYSIS_SECTIONS,
    INTRADAY_SCHEMA_JSON,
    REBALANCE_SCHEMA_JSON,
    SCHEMA_INTRADAY,
)

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
    "render_intraday_prompt",
    "render_prompt",
    "render_prompt_for",
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

#: 持仓行现价取数源：``snap``/``series`` = 行情主源；``bridge`` = 传了行情面但
#: 实时价未取到、回退桥快照价（渲染加 ``‡``）；``""`` = 调用方未接行情面（legacy，
#: 无痕）。桥 ``price`` 当日实测全天=昨收——直取它 = 模型在错价上决策。
PRICE_SRC_BRIDGE = "bridge"

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
    #: 现价取数源（见 ``PRICE_SRC_BRIDGE`` 注释）：``""`` = 未标注（legacy/金样）。
    price_src: str = ""
    #: 自首次建仓起的持有天数（自然日，四舍五入）。``None`` = 账本没有 ``buy_ts``
    #: （渲染 ``—``，绝不写成 0——0 的意思是「今天刚买」）。默认 ``None`` 让
    #: legacy/金样路径不出现该列，提示词逐字节不变。
    held_days: int | None = None


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
    #: 【盘中市场状态】块（盘中 regime 快照渲染，见 ``shared/decision_blocks``）。
    #: 空串 = 整段不出现——默认值下与上游金样逐字节一致。
    market_state_block: str = ""
    #: 【盘中模型信号】块（行情行内「信号分」的出处/刻度/覆盖率）。只注解行情块，
    #: 行情块没渲染时它也不出现（见 :func:`_pool_lines`）。
    signal_meta_block: str = ""
    #: 丁8【数据新鲜度警告】（各块降级留痕汇总）：置顶渲染（开场白之后、持仓表
    #: 之前）。空串 = 本轮无降级，整段不出现。
    alert_block: str = ""
    #: 丁5【大盘指数】行（指数快照 + 板块涨幅门控）。空串 = 未接线/未取到路径由
    #: 调用方自决（渲染序字节冻结纪律同 `market_state_block`）。
    indices_block: str = ""
    #: Stage 2 第二批聚合块：丁1 盘口 / 丁3 分钟 / 丁2 L2 门控 / 丁4 新闻 /
    #: 丁6 跨 agent / 丁9 上一轮（装配顺序即渲染顺序，见 ``shared/decision_blocks``）。
    #: 空串 = 未接线（旧替身）→ 提示词逐字节等于历史。
    stage2_block: str = ""
    #: 丁7【风险预算档位】（当前 tiers 五键）。空串 = 未接线。
    tier_block: str = ""
    #: 【研究总控·交易日前瞻】块（前一日夜间综合 + 当日各档复盘；IO 侧从
    #: ``data/research_control/brief/{target}.json`` 取紧凑版）。空串 = 未接线/
    #: 研究总控未产出——默认路径与历史金样逐字节一致。
    research_block: str = ""
    #: 授予额度（**只认分账额度**，不是真实账户总资产）：2026-10-19 用户拍板，
    #: agent 不得看见账户总金额。真实账户三数只走内部风控/审计。
    quota_total: float = AGENT_QUOTA_TOTAL
    #: 已用额度 = 本 agent 名下持仓成本合计（卖出即释放）。
    quota_used: float = 0.0
    per_stock_pct: float = DEFAULT_PER_STOCK_PCT
    max_new_buys: int = DEFAULT_MAX_NEW_BUYS
    #: 允许买入候选池之外的标的（2026-10-28 用户口径「股票不限」）。默认 False
    #: = 历史提示词逐字节不变；实盘轮由 ``TradingPolicy.for_agent(agent)`` 填充。
    allow_out_of_pool: bool = False
    #: 盈利优先的提示词段落（2026-10-28 用户口径）。默认 False = 金样不变；
    #: 实盘轮由 ``TradingPolicy.for_agent(agent)`` 填充。
    profit_first: bool = False

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


#: 现价取数口径注（2026-10-15 审计修）。**只在持仓行带取数源标注时渲染**——
#: legacy/金样路径（``price_src`` 全空）一个字节都不出现。
PRICE_SRC_NOTE = (
    "注：**现价 = 行情主源实时价**（快照 → 分钟序列，两源优先）；带 `‡` 的行"
    "实时价未取到、回退桥快照价——盘前=昨收属正常，**盘中未必是现价，"
    "勿作触发/定价依据**。"
)


def budget_filter_note(pct: float, *, allow_out_of_pool: bool = False) -> str:
    """候选池已按资金量裁剪的说明（09:35 主入口与整点轮共用同一句，防口径漂移）。

    ``allow_out_of_pool=True``（2026-10-28「股票不限」）时**不再**说「表里没有的
    代码不要报买入」：池外标的允许买，但由系统按实时行情/名称索引校验后执行。
    """
    size = "单票上限 = 剩余额度" if pct >= 1.0 else f"单票预算 = 剩余额度×{pct:.0%}"
    if allow_out_of_pool:
        return (
            f"候选池已按你的资金量剔除买不起的标的（{size}；"
            "最小一手 100 股、科创板 200 股都超预算的票不会出现在表里）。"
            "候选池是**重点参考**，不是唯一范围：你也可以买入池外 A 股——"
            "必须给出准确代码、名称与买入依据；系统会按实时行情、名称索引、"
            "黑名单/ST/停牌逐条校验后执行，校验不过的单不会成交。"
        )
    return (
        f"候选池已按你的资金量剔除买不起的标的（{size}；"
        "最小一手 100 股、科创板 200 股都超预算的票不会出现在表里），"
        "表里没有的代码不要报买入。"
    )


def ledger_cost_rows(
    rows: Sequence[HoldingRow],
    positions: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
) -> tuple[HoldingRow, ...]:
    """持仓行 → 提示词展示副本：成本/浮盈改**分账账本**口径。

    账本里的 ``cost_price`` 才是「我这笔仓位赚没赚」的正确基准。账本无该票、
    成本 ≤0、或非有限值（NaN/Inf——``inf > 0`` 为真会让成本列渲染出 ¥inf、
    盈亏渲染出 +nan%，模型照着 nan% 决策）→ 回退桥值并标 :data:`COST_SRC_BRIDGE`，
    渲染端加 ``*``。

    **只改展示**：返回新对象，绝不改入参。桥口径的 ``pnl_pct`` 在别处还被
    波动触发基线、强制减仓与执行路径消费，交易判定一律不动。

    数量同口径：账本记了 ``volume`` 时，展示的 ``volume`` 取账本量、``avail``
    取 ``min(桥可卖, 账本量)``——共享账户里桥的合计是**多家模型的总和**，照抄
    会让模型按别人的股数报卖（601985 实测 500/800/800）。账本没记量（legacy/
    金样）→ 保持桥值。

    ``positions`` = ``{code: {"volume", "cost_price", "buy_ts", ...}}``（账本里该
    agent 的持仓段）。类型坏值（不是 Mapping、``cost_price`` 不是数）一律走回退
    分支，不许炸穿提示词——提示词构建失败等于该轮 0 决策。

    ``now`` 给出时补「自首次建仓起的天数」（``positions[code].buy_ts``）；账本没有
    该票/没有 ``buy_ts`` → ``held_days=None``（渲染 ``—``，绝不写成 0）。这只是展示
    层增强，算不出来不影响本轮决策。``now=None`` 时保持原样（金样/legacy 路径）。
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
        held_days = r.held_days
        if held_days is None and now is not None:
            buy_ts = pos.get("buy_ts") if isinstance(pos, Mapping) else None
            held_days = holding_age_days(buy_ts, now)
        # 数量/可卖量按**本 agent 的账本持有量**展示（桥的 volume/avail 是整座
        # 共享账户的合计）。同一只票可以被多家模型同时持有，照抄桥口径会让模型
        # 以为 601985 的 2100 股全是自己的，进而按 100% 报出三倍超卖。账本没有
        # 这一行（legacy/金样路径）→ 保持桥值，逐字节不变。
        volume, avail = r.volume, r.avail
        if isinstance(pos, Mapping):
            try:
                own = float(pos.get("volume") or 0)
            except (TypeError, ValueError):
                own = 0.0
            if math.isfinite(own) and own > 0:
                volume = int(own)
                avail = int(max(0.0, min(float(r.avail), own)))
        if math.isfinite(lc) and lc > 0:
            out.append(
                HoldingRow(
                    code=r.code,
                    name=r.name,
                    volume=volume,
                    cost=round(lc, 2),
                    price=r.price,
                    pnl_pct=round((r.price - lc) / lc * 100, 2),
                    day_chg=r.day_chg,
                    avail=avail,
                    cost_src=COST_SRC_LEDGER,
                    price_src=r.price_src,
                    held_days=held_days,
                )
            )
        else:
            out.append(
                HoldingRow(
                    code=r.code,
                    name=r.name,
                    volume=volume,
                    cost=r.cost,
                    price=r.price,
                    pnl_pct=r.pnl_pct,
                    day_chg=r.day_chg,
                    avail=avail,
                    cost_src=COST_SRC_BRIDGE,
                    price_src=r.price_src,
                    held_days=held_days,
                )
            )
    return tuple(out)


def _display_holdings(ctx: RebalanceContext) -> tuple[HoldingRow, ...]:
    """提示词展示行：成本/浮盈/持有天数统一按分账账本口径补全。

    正文表与收尾的「T+5/T+10 换仓」规则必须看**同一份**行：规则段若按原始
    ``ctx.holdings`` 判有没有天数，账本里明明有 ``buy_ts`` 也不会出规则。
    """
    return ledger_cost_rows(ctx.holdings, ctx.ledger_positions, now=ctx.now)


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


def _holding_row_line(h: HoldingRow, *, show_age: bool = False) -> str:
    """一行持仓。价格/成本沿用隔壁的**裸 float 渲染**（``35.8`` 而非 ``35.80``）——
    逐字节对齐优先于好看。

    **成本缺失时盈亏一并列 ``—``**：盈亏是相对成本的差，成本没有则盈亏无从谈起。
    隔壁在这里给的是 ``成本 0 / 盈亏 +0.00%``——读起来像「不赚不亏」，
    实际是「两本账都没这个数」，是会被模型当成持有理由的假值。

    现价带 ``‡`` = 实时行情未取到、回退桥快照价（见 :data:`PRICE_SRC_BRIDGE`）；
    ``price_src=""``（legacy/金样）不标——逐字节不动。

    ``show_age=True`` 时追加「持有天数」列（调用方按**整表**是否拿到账本
    ``buy_ts`` 统一决定，单行缺失渲染 ``—``）——同一张表不能有的行有列、有的行没有。
    """
    mark = "" if h.cost_src == COST_SRC_LEDGER else "*"
    has_cost = math.isfinite(h.cost) and h.cost > 0
    cost_cell = format(h.cost, "") + mark if has_cost else MISSING
    price_cell = f"{h.price}‡" if h.price_src == PRICE_SRC_BRIDGE else h.price
    cells = [
        h.code,
        h.name,
        str(h.volume),
        cost_cell,
        str(price_cell),
        _pct(h.pnl_pct) if has_cost else MISSING,
        _pct(h.day_chg),
        str(h.avail),
    ]
    if show_age:
        cells.append(MISSING if h.held_days is None else str(h.held_days))
    return "| " + " | ".join(cells) + " |"


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

#: 拿到账本 ``buy_ts`` 时才用：多一列「持有天数」（自然日）。没拿到时**整列不出现**
#: ——legacy/金样路径的提示词必须逐字节不变（见 ``_context_body``）。
_HOLDINGS_HEADER_AGE = (
    "【你名下的现有持仓】（成本与盈亏% = 你名下分账账本口径；现价/数量/可卖量 = "
    "桥账户实时口径，可卖量 0 = 今日买入 T+1 不可卖；持有天数 = 自首次建仓起的自然日）：",
    "",
    "| 代码 | 名称 | 数量 | 成本 | 现价 | 盈亏% | 今日涨跌% | 可卖量 | 持有天数 |",
    "|------|------|------|------|------|-------|-----------|--------|----------|",
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


def _pool_lines(ctx: RebalanceContext) -> list[str]:
    if not ctx.pool:
        return []
    lines = list(_POOL_HEADER)
    lines += [_pool_row_line(p) for p in ctx.pool]
    qb = render_quote_block(ctx.quotes, ctx.quotes_stale_count)
    if qb:
        lines += ["", qb]
        # 【盘中模型信号】只注解行情行内的「信号分」后缀——行情块没渲染时
        # 它跟着不出现（没有后缀可注解）。默认空串 = 与上游金样逐字节一致。
        if ctx.signal_meta_block:
            lines += ["", ctx.signal_meta_block]
    return lines


def _decision_rule_lines(ctx: RebalanceContext) -> list[str]:
    buy_scope = (
        "2. 需要买入时优先从候选池选（分数高、行业顺大盘方向的优先）；"
        "**池外 A 股也可买**——必须给出准确代码、名称与买入依据，系统会按实时行情、"
        "名称索引、黑名单/ST/停牌逐条校验后执行。"
        if ctx.allow_out_of_pool
        else "2. 需要买入时从候选池选：优先分数高、行业顺大盘方向的；"
    )
    rules = [
        "",
        "【决策规则】",
        "1. 逐只现有持仓判断：hold（继续持有）/ sell（减仓或清仓换股）。"
        "如果现有持股趋势/基本面仍优于候选池，可以全部 hold 不换股。",
        buy_scope + "允许换仓（同轮先 sell 再 buy），以信号分数+板块主线+新闻分子"
        "综合权衡，不必拘泥原有持仓。",
        f"3. sell 的 pct = 卖出可卖量的比例（0~1）；buy 的 pct = 使用剩余额度的比例"
        f"（每票 ≤{ctx.per_stock_pct:.0%}，当日新开仓 ≤{ctx.max_new_buys} 只；"
        f"超出的会被闸门裁掉）。"
        + budget_filter_note(
            ctx.per_stock_pct, allow_out_of_pool=ctx.allow_out_of_pool
        ),
        "4. T+1：可卖量 0 的持仓不能卖。ST/*ST/退市整理股与黑名单标的**不可买入**（闸门硬拦）。",
    ]
    if ctx.profit_first:
        rules += [
            "",
            "【盈利优先】（2026-10-28 用户口径）：目标是在可控回撤下最大化净值增长。"
            "在额度与风控允许范围内，把资金投向信号最强、催化最明确的标的；"
            "没有优势信号时持有现金、不为凑交易而交易。允许集中持仓，"
            "但每一笔买入都要在 reason 里写明止损/退出条件（盘中轮会落成 watch 条件位），"
            "把单票风险限制在可承受范围内。研究总控的「禁新开仓」等方向判断是"
            "**参考**：个股信号/新闻催化足够强且风险预算允许时仍可建仓，"
            "在 reason 里写明与大盘方向的权衡。",
        ]
    if any(h.held_days is not None for h in _display_holdings(ctx)):
        rules.append(
            "5. 持有天数 = 自首次建仓起的自然日（见持仓表）。信号模型的持有周期常见为 "
            "T+5 与 T+10：持有达到 5 天且该票信号分已明显走弱、或已跌出候选池/池内排名"
            "靠后时，应优先考虑减仓或卖出换仓；持有达到 10 天时，除非信号分仍在池内"
            "靠前且买入逻辑未破坏，否则优先换入更强候选。信号仍强时不必为凑天数换仓。"
        )
        rules.append(
            "6. 输出**严格 JSON**（不要 markdown 代码块、不要额外文字），格式："
        )
    else:
        rules.append(
            "5. 输出**严格 JSON**（不要 markdown 代码块、不要额外文字），格式："
        )
    rules.append(REBALANCE_SCHEMA_JSON)
    return rules


def _intraday_rule_lines(ctx: RebalanceContext) -> list[str]:
    """盘中槽位的收尾段：四段式契约 + 守护字段 schema + 执行规则。

    与 :func:`_decision_rule_lines` 的差别**只是收尾**——正文两个槽位完全共用
    （见 :func:`_context_body`）。盘中轮多了 ``watch``（条件位）一族字段，故
    规则里要把「文字里写了价位就必须落成 watch」说死：不落盘的价位**没有**任何
    分钟级哨兵在盯，等于没设（上游踩过这个坑，见 assets 段原话）。
    """
    rules = [
        "",
        *ANALYSIS_SECTIONS,
        "",
        "【决策块格式】",
        INTRADAY_SCHEMA_JSON,
        "",
        "【决策规则】",
        f"1. sell 的 pct = 卖出可卖量的比例（0~1，清仓=1.0）；buy 的 pct = 使用剩余额度的"
        f"比例（每票 ≤{ctx.per_stock_pct:.0%}，当日新开仓 ≤{ctx.max_new_buys} 只；"
        f"超出的会被闸门裁掉）。"
        + budget_filter_note(
            ctx.per_stock_pct, allow_out_of_pool=ctx.allow_out_of_pool
        ),
        "2. T+1：可卖量 0 的持仓不能卖。ST/*ST/退市整理股与黑名单标的**不可买入**"
        "（闸门硬拦）。买卖/换仓依据 = 信号分数 + 大盘盘面 + 板块主线 + 新闻分子"
        "综合判断，HOLD/BUY 侧标签一律不作为依据。"
        + (
            "候选池是重点参考；池外 A 股也可买（准确代码/名称/买入依据，"
            "系统按实时行情与名称索引校验后执行）。"
            if ctx.allow_out_of_pool
            else ""
        ),
        "3. watch = 给**现有持仓**挂**条件位**：stop_loss=跌破此价自动减仓 pct 比例、"
        "take_profit=涨到此价自动止盈 pct（至少给一个，由分钟级价格哨兵盯，不用等"
        "下一个整点）。move_stop 可选：价格**上触**该价后把 stop_loss 上移到该价"
        "（只用于浮盈锁利，必须高于当前 stop_loss，填低了会被忽略；下破触发请用 "
        "stop_loss，别用 move_stop）。watch 只会减仓/止盈、**不会建仓**——"
        "未持仓的标的不要用 watch，想等回踩再买请在 buy 决策里写清买入条件与 stop_loss。",
        "4. 「③推理论证」里写了具体价位的**持仓**，**必须**同步落成 watch 决策"
        "（带 code 与 reason 写明触发价与动作）——只写在文字里等于没设防。",
    ]
    if ctx.profit_first:
        rules += [
            "",
            "【盈利优先】（2026-10-28 用户口径）：目标是在可控回撤下最大化净值增长。"
            "在额度与风控允许范围内，把资金投向信号最强、催化最明确的标的；"
            "没有优势信号时持有现金、不为凑交易而交易。允许集中持仓，"
            "但每一笔买入都要给出止损/退出条件，把单票风险限制在可承受范围内。"
            "研究总控的「禁新开仓」等方向判断是**参考**：个股信号/新闻催化足够强且"
            "风险预算允许时仍可建仓，在 reason 里写明与大盘方向的权衡。",
        ]
    if any(h.held_days is not None for h in _display_holdings(ctx)):
        rules.append(
            "5. 持有天数 = 自首次建仓起的自然日（见持仓表）。信号模型周期常见为 T+5/T+10："
            "持有达到 5 天且信号分走弱、或跌出候选池/排名靠后时，考虑减仓或卖出换仓；"
            "持有达到 10 天时，除非信号分仍在池内靠前且逻辑未破坏，否则优先换入更强候选。"
        )
        rules.append(
            "6. buy/sell 的 reason 必填；buy 还应给出 stop_loss（或 move_stop）与 "
            "invalidation（退出框架），三者缺一会被判为残缺决策。"
            "逐只现有持仓都要有判断（hold / sell / watch），包括本轮不动的那几只。"
        )
    else:
        rules.append(
            "5. buy/sell 的 reason 必填；buy 还应给出 stop_loss（或 move_stop）与 "
            "invalidation（退出框架），三者缺一会被判为残缺决策。"
            "逐只现有持仓都要有判断（hold / sell / watch），包括本轮不动的那几只。"
        )
    return rules


def _context_body(ctx: RebalanceContext) -> list[str]:
    """两套提示词**逐字节共用**的正文：持仓表 → 大盘方向 →（新鲜度/市场状态/
    指数）→（唤醒）→ 候选池 →（行情+信号块）→（盘口/分钟/L2/新闻/互看/上一轮）
    → 风险块 →（档位）。

    分出来只为让「盘中轮与调仓轮看到的事实完全一致」这件事在代码里是显然的——
    两边唯一的差别是收尾段（:func:`_decision_rule_lines` /
    :func:`_intraday_rule_lines`）。**行序与空行位置一个都不能动**：调仓轮那侧
    逐字节对齐隔壁金样（见模块 docstring），这里挪一行就红。带括号的各块是
    QM 补块（Stage 2 两批）——默认空串、数据门控，金样路径不受影响。
    """
    display = _display_holdings(ctx)
    # 整表统一：只要有一只拿到账本 buy_ts 就出「持有天数」列，其余行缺失渲染 —。
    # 一只都没有 → 整列不出现（legacy/金样逐字节不动）。
    show_age = any(h.held_days is not None for h in display)
    lines = [
        f"现在是北京时间 {ctx.now:%F %T}（开盘后）。你是 {ctx.agent} 的 A股实盘调仓决策模型，"
        f"管理 ¥{ctx.quota_total:,.0f} 虚拟额度（已用 ¥{ctx.quota_used:,.0f}，"
        f"剩余 ¥{ctx.quota_remaining:,.0f}）。",
        "",
    ]
    if ctx.profit_first:
        lines += [
            "目标：在可控回撤下**最大化净值增长**（盈利优先，其次才是交易频率）；"
            "没有优势信号时允许持有现金。",
            "",
        ]
    # 丁8 新鲜度/降级警告：置顶（开场白之后、持仓表之前）——模型先知道哪些
    # 数据是降级的，再读事实。空串 = 无降级，不出现。
    if ctx.alert_block:
        lines += [ctx.alert_block, ""]
    lines += [*(_HOLDINGS_HEADER_AGE if show_age else _HOLDINGS_HEADER)]
    lines += [_holding_row_line(h, show_age=show_age) for h in display]
    # 现价取数口径注：只在接上行情面（price_src 有标注）时出现——legacy/金样路径
    # 逐字节不动（见 :data:`PRICE_SRC_NOTE`）。
    if any(h.price_src for h in display):
        lines += ["", PRICE_SRC_NOTE]
    lines += [
        "",
        LEDGER_COST_NOTE,
        "",
        "【今日大盘方向】（最新研究产出）：",
        _direction_line(ctx.direction),
        "",
    ]
    # 【盘中市场状态】（大盘 regime 实时快照）：紧跟大盘方向之后——两者是
    # 「同一件事的慢/快两拍」。空串 = 整段不出现（金样逐字节不动的默认路径）。
    if ctx.market_state_block:
        lines += [ctx.market_state_block, ""]
    # 丁5【大盘指数】行：状态块的取数面（同一批快照）。空串 = 未接线。
    if ctx.indices_block:
        lines += [ctx.indices_block, ""]
    # 【研究总控·交易日前瞻】：慢变量（前一日夜间综合 + 当日各档复盘），
    # 紧跟大盘快照之后。空串 = 未接线/未产出，逐字节不出现。
    if ctx.research_block:
        lines += [ctx.research_block, ""]
    if ctx.extra_context:
        lines += [ctx.extra_context, ""]
    lines += _pool_lines(ctx)
    # Stage 2 第二批聚合块（丁1/丁3/丁2/丁4/丁6/丁9）：池与行情之后、风险块之前。
    # 空串 = 未接线（旧替身）→ 以下所有块与历史逐字节一致。
    if ctx.stage2_block:
        lines += ["", ctx.stage2_block]
    # 事件风险警示：持仓命中提示退出、候选命中别选（闸门已硬拦买入，这里省一轮
    # 无效决策）。**无前置空行**——与隔壁逐字节一致。
    if ctx.risk_block:
        lines += [ctx.risk_block]
    # 行业整体劣化：赛道级软提示，不硬拦。有前置空行。
    if ctx.industry_caution_block:
        lines += ["", ctx.industry_caution_block]
    # 丁7【风险预算档位】：收尾块（「本轮受什么约束」显式化）。空串 = 未接线。
    if ctx.tier_block:
        lines += ["", ctx.tier_block]
    return lines


def render_intraday_prompt(ctx: RebalanceContext) -> str:
    """盘中槽位（整点）的完整提示词：同一份正文 + 四段式收尾。

    盘中轮要回答的不只是「换不换股」，还有「手上的票要不要挂条件位」——所以
    收尾段换成四段式（前端据此分节）+ ``INTRADAY_SCHEMA_JSON``（含 ``watch``/
    ``stop_loss``/``take_profit``/``move_stop``/``invalidation``）。正文与
    :func:`render_prompt` 完全一致，见 :func:`_context_body`。
    """
    lines = _context_body(ctx)
    lines += _intraday_rule_lines(ctx)
    return "\n".join(lines)


def render_prompt(ctx: RebalanceContext) -> str:
    """一轮调仓的完整提示词。

    ``ctx.holdings`` 必须是**桥口径原始行**——账本改写在这里面做，不给调用方
    漏掉的机会（漏掉的后果是模型拿着混合成本算盈亏，见 :class:`HoldingRow`）。
    """
    lines = _context_body(ctx)
    lines += _decision_rule_lines(ctx)
    return "\n".join(lines)


def render_prompt_for(ctx: RebalanceContext, schema: str) -> str:
    """按槽位 schema 选提示词——**调用方只该用这一个入口**。

    ``schema`` 决定 action 白名单（``contract.SCHEMAS``），提示词必须跟着它走：
    调仓轮（``rebalance``）的 schema 里没有 ``watch``，提示词就不该要求模型给
    条件位；盘中轮反过来。两边混用会把「模型没按 schema 输出」变成常态——实测
    11 个槽位全用调仓提示词时 ``watch_armed`` 每轮都是 0。

    未知 schema **回退调仓轮**（最保守那份：无 ``watch``、纯 JSON），
    与 :func:`parse_decisions` 的显式 ``ValueError`` 配合——报错在这里是
    「白名单对不上」，在那边是「解析契约被破坏」，两回事。
    """
    if schema == SCHEMA_INTRADAY:
        return render_intraday_prompt(ctx)
    return render_prompt(ctx)
