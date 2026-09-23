"""决策层买入闸门（纯核心，无 IO）：LLM 说买之后、下单之前的最后一道判断。

为什么必须有这一层
------------------
隔壁 BayMax 的 21 条闸口对到本仓既有风控规则后，**缺的四条全部在买入侧**
（标的边界 / 池成员 / 涨停 / 跌停），再加一条「买不起一手的票别推给模型」
（``unseen``）。这四条不是移植，是新建——本仓的规则层里：
``l3.stale_quote`` / ``l3.price_deviation`` 只管「价新不新、离不离谱」，
``ghost_pricing`` 只**算价**判可成交性，``lot_rules`` 明说「涨跌停幅度不在此重算」，
``exclusion_list`` 只是一份**名单**（没人拿它拦单）。于是没人拦「涨停板上追高」
与「买一只不在候选池里的票」——正是 LLM 决策链上两个最大的风险口。

为什么不塞进 ``backend/shared/risk/`` 的规则注册表
--------------------------------------------------
那张表的规则签名是 ``fn(ctx, params)``，而 ``RiskContext`` 里**没有**
``day_chg`` / 候选池 / ``name`` 这些字段，且规则不读任何配置存储。把判定挂到
没人填的字段上，就是本仓已经吃过的那个坑：**判定与单测都在，但 ctx 字段没人填
→ 规则恒不触发**（``l1.new_buys_per_day`` 的复盘结论，见
``backend/shared/risk/`` 的登记表 docstring）。

故这里是**第二族规则**：同样的纯函数纪律、同样的 rule_id 命名（L0–L6 分层），
入参由决策轮自己给（行情、池子、额度都在它手上），规则 id 登记进**同一张**
``backend/shared/risk/gate_registry.py``，两族由 ``test_risk_gate_registry.py``
一起守（双向覆盖 = 两族并集）。

fail-open / fail-closed 的分界（照抄隔壁实测口径，不许自由发挥）
----------------------------------------------------------------
* **名称取不到 → 放行**（``symbol_policy`` 原文的理由：宁可漏拦一只，也不能因为
  名称表拉不到就把当天所有买入停掉）。代码黑名单不依赖名称，任何时候都生效。
* **额度/现金类字段取不到 → 不判并留 ``note``**，绝不臆造数字；真正的资金闸在
  执行段（``l1.available_cash``，fail-closed），本层不复制它。
* **缺一半输入是调用点错误 → 直接 ``ValueError``**。静默按「未判」处理会让
  「忘了传行情」伪装成「行情说可以买」。
* **卖出永远放行**：被套的仓位出不来比买错更糟。故本模块只有 ``check_buy``，
  没有对称的 ``check_sell``——不是没写，是刻意不设。

涨跌停阈值一律**注入**，本模块不产出任何阈值
--------------------------------------------
唯一事实源是 ``services/simulation/services/local_market_data.limit_threshold``
（按板别/ST/制度日期解析，返回**比例**）。本模块只做「拿到的两个数比大小」，
理由有三：① ``shared/`` 反向 import ``services/simulation`` 会把依赖方向搞乱；
② 同一条判据在两个地方各算一遍，就是下一个「两处口径打架」；
③ 自写阈值的字面量会被 ``shared/market_fidelity.py`` 的源码扫描器直接判 HIGH。
**单位守卫**：阈值 > 1 一律 ``ValueError``——本仓两族单位并存（这里是比例、
``market_breadth`` 那边是百分点），静默按错单位比较的后果是**一条都不拦**
（0.098 ≥ 9.5 恒假）。

本层刻意不做的事（别在这里补）
------------------------------
* 当日累计新开仓上限 → 既有 ``l1.new_buys_per_day``（本层只管**本轮**）。
* 账户可用资金 / 总杠杆 / 单票市值上限 → 既有 L1 族（本层只管**子账户虚拟现金**，
  是 P2.7 多模型竞争的那本分账）。
* 熔断（当日亏损限额）→ 既有 ``l1.daily_loss_limit`` 与 L0。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any

from backend.services.live_trading.services.lot_rules import min_buy_quantity
from backend.shared.decision.contract import (
    BUY,
    FRAC_DIRTY,
    FRAC_OK,
    Decision,
)
from backend.shared.stock_utils import StockCodeUtil
from backend.shared.symbol_policy import is_risky_name

__all__ = [
    "ALL_RULE_IDS",
    "BuyGate",
    "GateVerdict",
    "RULE_BELOW_MIN_LOT",
    "RULE_HALTED",
    "RULE_LIMIT_DOWN",
    "RULE_LIMIT_UP",
    "RULE_PCT_INVALID",
    "RULE_PCT_ZERO",
    "RULE_POOL_NOT_MEMBER",
    "RULE_POOL_ROW_INVALID",
    "RULE_ROUND_NEW_BUYS",
    "RULE_SYMBOL_BOUNDARY",
    "RULE_UNAFFORDABLE",
    "RULE_VCASH",
    "check_buy",
    "check_halted",
    "check_limit_reach",
    "check_symbol_boundary",
    "filter_pool",
]

# ── 规则 id（分层命名，与 shared/risk 的执行族共用一套前缀）──────────────
#: `unseen`：候选在**进模型视野之前**被剔除。它记的不是「被否决的决策」，
#: 而是「假设模型会选它」的弱反事实（见 gate_registry 的 kind 说明）。
RULE_UNAFFORDABLE = "l1.unaffordable"
#: 子账户虚拟现金（P2.7 多模型竞争的**分账**口径，不是账户真钱）。
RULE_VCASH = "l1.vcash"
RULE_POOL_NOT_MEMBER = "l2.pool_not_member"
#: 候选行本身不合法（缺 code / 缺价）。属池子构建侧缺陷，不是「排除了一只票」。
RULE_POOL_ROW_INVALID = "l2.pool_row_invalid"
RULE_ROUND_NEW_BUYS = "l2.round_new_buys"
RULE_PCT_INVALID = "l2.pct_invalid"
RULE_PCT_ZERO = "l2.pct_zero"
RULE_BELOW_MIN_LOT = "l3.below_min_lot"
RULE_SYMBOL_BOUNDARY = "l4.symbol_boundary"
RULE_LIMIT_UP = "l4.limit_up"
RULE_LIMIT_DOWN = "l4.limit_down"
#: ⚠️ **当前无生产者**（2026-09-24 实测）：判定与单测都在，但没人往
#: ``check_buy(halted=…)`` 里填过值——执行段读快照的 ``halted``/``is_halted``/
#: ``suspended`` 三键，而快照写入方（``tdx_aidata/collector.py:118``、
#: ``tdx_quote_feed.py:122``、``qmt_quote_backup.py:186``）只写
#: ``Now/Open/PreClose/High/Low/Volume/Amount`` + 五档，三键一个都没有，
#: 于是 ``halted`` 恒为 ``None``（不判 + 留痕）⇒ 本规则至今一次未触发。接上停牌源
#: （如 QMT 合约详情里的状态位）之前，它是影子账里那条「永不命中的规则」。
RULE_HALTED = "l4.halted"

#: 本族全部规则 id。**新增规则必须同时**：加常量、进本元组、在
#: ``shared/risk/gate_registry.py`` 加条目——三处缺一即红（测试逐条钉住）。
ALL_RULE_IDS: tuple[str, ...] = (
    RULE_UNAFFORDABLE,
    RULE_VCASH,
    RULE_POOL_NOT_MEMBER,
    RULE_POOL_ROW_INVALID,
    RULE_ROUND_NEW_BUYS,
    RULE_PCT_INVALID,
    RULE_PCT_ZERO,
    RULE_BELOW_MIN_LOT,
    RULE_SYMBOL_BOUNDARY,
    RULE_LIMIT_UP,
    RULE_LIMIT_DOWN,
    RULE_HALTED,
)


def _norm(code: object) -> str:
    """任意形态的代码 → 后缀式（``600036.SH``）。归一是**必须**的：
    黑名单/候选池里存后缀式、模型吐裸码或前缀式是常态，不归一就是静默放行。
    认不出的输入原样返回（垃圾进垃圾出，不会误配到某个真代码上）。"""
    return StockCodeUtil.to_suffix(str(code or "").strip())


def _as_float(x: object) -> float | None:
    """宽松转 float；转不了或非有限 → None（按「没给」处理，绝不臆造 0.0）。

    ``bool`` 必须先判掉：``float(True) == 1.0`` 会让一个误传的开关变成
    「涨幅 100%」（见 contract 里同一处陷阱）。
    """
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


# ── 裁定结果 ─────────────────────────────────────────────────────────
@dataclass(frozen=True)
class GateVerdict:
    """一次判定的结果。**放行与拒绝同构**，调用点不必分叉两种返回类型。

    * ``rule`` 是稳定 id（放行时空串）——留痕/代价记账按它聚合，不要解析 ``reason``；
    * ``pct`` 是**夹取之后**的可执行比例（放行时才有意义）：调用点**必须**用它，
      自己再算一遍就会得到与闸门不同的数；
    * ``evidence`` 是 ``(键, 值)`` 元组（不是 dict——本类是 frozen，装一个可变
      dict 等于把「不可变」写成装饰）；审计表落库时 ``dict(v.evidence)`` 即可；
    * ``note`` 记「没判什么、为什么」，与 ``reason``（拒因）分开：放行的单子也
      可能带着「涨跌停没判」这种必须可见的说明。
    """

    allowed: bool
    rule: str = ""
    reason: str = ""
    pct: float = 0.0
    evidence: tuple[tuple[str, Any], ...] = ()
    note: str = ""


def _ok(*, pct: float = 0.0, note: str = "") -> GateVerdict:
    return GateVerdict(allowed=True, pct=pct, note=note)


def _block(rule: str, reason: str, *, note: str = "", **evidence: Any) -> GateVerdict:
    return GateVerdict(
        allowed=False,
        rule=rule,
        reason=reason,
        evidence=tuple(evidence.items()),
        note=note,
    )


def _annotate(verdict: GateVerdict, notes: list[str]) -> GateVerdict:
    """把累计的 ``notes`` 并进一条子裁定（frozen → 造新的，不改原对象）。

    子判据（停牌/涨跌停）自己带 note（「未判」类说明），与调用点累计的
    note（「额度未配置」类）合并后一起留给审计表——放行的单子也常常带着
    「有一项没判」这种必须可见的说明。
    """
    merged = [n for n in (*notes, verdict.note) if n]
    if not merged:
        return verdict
    return replace(verdict, note="；".join(merged))


@dataclass(frozen=True)
class BuyGate:
    """一次买入判定所需的全部外部约束（**由调用点注入**，本层不读任何存储）。

    ``pool_codes`` / ``blocked_symbols`` 在构造时就归一到后缀式，故成员判断是
    O(1) 且天然对形态不敏感——库里存前缀式、模型吐裸码都不会漏判。

    ``per_stock_pct``：单票买入比例上限（**比例**，0.2 = 两成）。
    ``0`` 表示**未配置**（不夹取 + 留 note），``>1`` 或负数是单位/配置错误 →
    ``ValueError``：把 15（十五个百分点）当比例读会让夹取变成空操作，
    于是一笔打到 100% 额度，比不设上限更危险。
    """

    pool_codes: frozenset[str] = frozenset()
    blocked_symbols: frozenset[str] = frozenset()
    per_stock_pct: float = 0.0
    allow_st: bool = False
    #: 本轮允许**新开仓**的标的数上限；0 = 未配置（不判 + 留 note）。
    #: 当日累计额度归既有的 ``l1.new_buys_per_day``，本层不复制。
    max_new_buys_round: int = 0
    #: 子账户虚拟现金（元）。``None`` = 未建模（P2.7 之前），不判 + 留 note。
    virtual_cash: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "pool_codes", frozenset(c for c in map(_norm, self.pool_codes) if c)
        )
        object.__setattr__(
            self,
            "blocked_symbols",
            frozenset(c for c in map(_norm, self.blocked_symbols) if c),
        )
        if self.per_stock_pct < 0 or self.per_stock_pct > 1:
            raise ValueError(
                f"per_stock_pct={self.per_stock_pct!r} 不是比例（合法区间 [0, 1]，"
                f"0 表示未配置；十五个百分点要写 0.15 而不是 15）"
            )
        if self.max_new_buys_round < 0:
            raise ValueError(f"max_new_buys_round={self.max_new_buys_round!r} 不能为负")


# ── 单条判据 ─────────────────────────────────────────────────────────
def check_symbol_boundary(code: str, name: str, gate: BuyGate) -> GateVerdict:
    """标的边界：黑名单（精确码）→ ST/退市（名称）。

    顺序不可颠倒：黑名单是按 6 位号精确匹配的**人工判决**，名称类判据是推断；
    一只票同时命中两者时，能说清原因的那个（号在名单里）更该被记下来。

    名称取不到（空串）→ 放行，见模块 docstring 的 fail-open 分界。
    """
    symbol = _norm(code)
    if not symbol:
        return _block(RULE_SYMBOL_BOUNDARY, "决策没有标的代码，无法执行", code=code)
    if symbol in gate.blocked_symbols:
        return _block(RULE_SYMBOL_BOUNDARY, f"{symbol} 在买入黑名单中", symbol=symbol)
    if gate.allow_st:
        return _ok()
    if is_risky_name(name):
        return _block(
            RULE_SYMBOL_BOUNDARY,
            f"{symbol} 名称 {name!r} 命中 ST/退市（禁买）",
            symbol=symbol,
            name=name,
        )
    return _ok()


def check_halted(halted: bool | None) -> GateVerdict:
    """停牌：``True`` 拒；``False`` 放行；``None``（不知道）**不判** + 留痕。

    ``None ≠ False`` 必须显式分开：把「没查到」当「没停牌」，等于用一次查询失败
    换来一笔必然废掉的买单；反过来把「没查到」当「停牌」，会在行情源抖动时
    停掉当天所有交易。
    """
    if halted is None:
        return _ok(note="停牌状态未知：未判（不知道≠没停牌）")
    if halted:
        return _block(RULE_HALTED, "标的停牌（放行也是废单）")
    return _ok()


def check_limit_reach(
    *,
    day_chg_ratio: float | None,
    limit_threshold_ratio: float | None,
) -> GateVerdict:
    """是否已到涨/跌停板（两个入参都是**比例**：0.098 表示当日涨 9.8%）。

    「到板」用 ``>=``（含等号）：差 0.1 个百分点就是追高。阈值由调用点注入
    （唯一事实源见模块 docstring），本函数**不产出、不猜测**任何阈值。

    四种情况**不判**（放行 + note）而非猜：两个入参都没给、涨幅不可用（NaN）、
    阈值不可用（0/NaN）。给一半是调用点错误 → ``ValueError``。
    """
    if (day_chg_ratio is None) != (limit_threshold_ratio is None):
        raise ValueError(
            "涨跌停判定需要 day_chg_ratio 与 limit_threshold_ratio 同时给出"
            "（只给一半是调用点错误，不是「数据缺失」）"
        )
    if day_chg_ratio is None:  # 两个都缺（只给一半已在上面报错）
        return _ok(note="缺当日涨跌幅与涨跌停阈值：涨跌停未判")

    thr = _as_float(limit_threshold_ratio)
    if thr is None or thr <= 0:
        return _ok(note=f"涨跌停阈值不可用（{limit_threshold_ratio!r}）：涨跌停未判")
    if thr > 1:
        raise ValueError(
            f"limit_threshold_ratio={thr!r} 超出比例区间 [0, 1]——"
            f"这看起来是百分点（如 10.0）而不是比例（如 0.1）；"
            f"静默按错单位比较会让这条闸一条都不拦"
        )

    chg = _as_float(day_chg_ratio)
    if chg is None:
        return _ok(note=f"当日涨跌幅不可用（{day_chg_ratio!r}）：涨跌停未判")

    if chg >= thr:
        return _block(
            RULE_LIMIT_UP,
            f"已在涨停（涨幅 {chg:.4f} ≥ 阈值 {thr:.4f}，均为比例）",
            day_chg_ratio=chg,
            limit_threshold_ratio=thr,
        )
    if chg <= -thr:
        return _block(
            RULE_LIMIT_DOWN,
            f"已在跌停（跌幅 {chg:.4f} ≤ -{thr:.4f}，均为比例）",
            day_chg_ratio=chg,
            limit_threshold_ratio=thr,
        )
    return _ok()


def check_buy(
    decision: Decision,
    *,
    gate: BuyGate,
    held: bool = False,
    new_buys_round: int = 0,
    halted: bool | None = None,
    day_chg_ratio: float | None = None,
    limit_threshold_ratio: float | None = None,
    need_amount: float | None = None,
    budget: float | None = None,
    price: float | None = None,
) -> GateVerdict:
    """一条 ``buy`` 决策的完整判定。**只判买入**，非 buy 一律 ``ValueError``。

    判定顺序（先便宜的、再需要算的；先「该不该买这只票」再「现在能不能买」）::

        标的边界 → 比例可执行 → 池成员/本轮上限（仅新开仓）
        → 停牌 → 涨跌停 → 虚拟现金 → 买得起一手

    参数：``held`` = 该票**已有持仓**（加仓不占池/本轮额度）；``new_buys_round`` =
    本轮**已**产生的新开仓数；``need_amount`` = 本单金额（元，调用点算）；
    ``budget`` / ``price`` = 可用额度与现价（判「买得起一手」用，缺一不判）。
    """
    if decision.action != BUY:
        raise ValueError(
            f"check_buy 只判 buy，收到 action={decision.action!r}"
            f"（卖出走独立路径且**永不拦**：被套的仓位出不来比买错更糟）"
        )

    notes: list[str] = []
    symbol = _norm(decision.code)

    verdict = check_symbol_boundary(decision.code, decision.name, gate)
    if not verdict.allowed:
        return verdict

    pct, frac = decision.buy_intent()
    if frac == FRAC_DIRTY:
        # 给了值但读不出（如 "0.3股"）：停手留痕，**不许**当成「没表达」按默认额度买
        return _block(
            RULE_PCT_INVALID,
            f"买入比例无法解析（模型给了 {decision.pct.raw!r}）：停手留痕",
            pct_raw=decision.pct.raw,
        )
    if frac != FRAC_OK:
        # 缺比例 ≠ 用满额度：买入是「用多少额度」的声明，模型不说就是没说
        return _block(
            RULE_PCT_ZERO,
            "模型未给出可执行的买入比例（缺比例、明说 0 或负值）",
            pct_state=decision.pct.state,
        )

    if gate.per_stock_pct > 0:
        if pct > gate.per_stock_pct:
            notes.append(f"按单票额度夹取 {pct:g}→{gate.per_stock_pct:g}")
            pct = gate.per_stock_pct
    else:
        notes.append("单票额度未配置：不夹取")

    if not held:
        if not gate.pool_codes or symbol not in gate.pool_codes:
            return _block(
                RULE_POOL_NOT_MEMBER,
                f"{symbol} 不在本轮候选池内（新开仓只能在池内选）",
                symbol=symbol,
                pool_size=len(gate.pool_codes),
                note="；".join(notes),
            )
        if gate.max_new_buys_round > 0:
            if new_buys_round >= gate.max_new_buys_round:
                return _block(
                    RULE_ROUND_NEW_BUYS,
                    f"本轮新开仓已达上限（{new_buys_round}/{gate.max_new_buys_round}）",
                    new_buys_round=new_buys_round,
                    max_new_buys_round=gate.max_new_buys_round,
                    note="；".join(notes),
                )
        else:
            notes.append("本轮新开仓上限未配置：未判")

    verdict = check_halted(halted)
    if not verdict.allowed:
        return _annotate(verdict, notes)
    if verdict.note:
        notes.append(verdict.note)

    verdict = check_limit_reach(
        day_chg_ratio=day_chg_ratio, limit_threshold_ratio=limit_threshold_ratio
    )
    if not verdict.allowed:
        return _annotate(verdict, notes)
    if verdict.note:
        notes.append(verdict.note)

    if gate.virtual_cash is None:
        notes.append(
            "子账户虚拟现金未建模：资金闸未判（真钱闸在执行段 l1.available_cash）"
        )
    elif need_amount is None:
        notes.append("本单金额未知：虚拟现金闸未判")
    elif need_amount > gate.virtual_cash:
        return _block(
            RULE_VCASH,
            f"本单金额 {need_amount:g} 超出子账户虚拟现金 {gate.virtual_cash:g}",
            need_amount=need_amount,
            virtual_cash=gate.virtual_cash,
            note="；".join(notes),
        )

    if price is None or budget is None:
        notes.append("缺现价或可用额度：最小一手未判")
    else:
        need = _min_lot_cost(symbol, price)
        if need is not None and budget < need:
            return _block(
                RULE_BELOW_MIN_LOT,
                f"可用额度 {budget:g} 买不起一手（{symbol} 需 {need:g}）",
                min_lot_cost=need,
                budget=budget,
                note="；".join(notes),
            )

    return _ok(pct=pct, note="；".join(notes))


def _min_lot_cost(symbol: str, price: object) -> float | None:
    """一手要多少钱（元）；价格不可用 → None（不判）。"""
    p = _as_float(price)
    if p is None or p <= 0:
        return None
    return min_buy_quantity(symbol) * p


def filter_pool(
    rows: list[Any] | tuple[Any, ...] | None,
    gate: BuyGate,
    *,
    budget: float | None,
    slack: float = 1.0,
) -> tuple[list[Any], list[tuple[Any, GateVerdict]]]:
    """候选池**进模型视野之前**的筛选 → ``(保留, [(被剔的行, 裁定)])``。

    两类剔除，理由要给得出来：

    * **买不起一手**（``l1.unaffordable``，``kind=unseen``）：不是「否决了模型的
      选择」，而是「模型压根没机会看见它」——把它留在池里，模型选它之后被执行段
      拒掉，账面上就成了「模型老是给出不可执行的决策」，而真相是可用额度买不起。
      `unseen` 与 `veto` 混记，会把「视野被资金规模截断」读成「规则的成本」。
    * **标的边界**（ST/退市/黑名单）：进了视野也只是白占一次推理。

    ``slack``：一手成本的余量系数（``1.0`` = 刚好够就算够）。隔壁用 1.02 给
    成交价漂移留 2%，本仓**不设默认余量**——决策价与成交价之间已经有
    ``l3.price_deviation`` 与 ``align_buy_quantity`` 两道执行侧的兜底，在这里
    提前 2% 就把「买得起」的票剔出视野，是拿信息换保险。要留余量就显式传。

    ``budget=None`` = 额度未知 → **不筛资金**（但边界照筛）：两件事不互相掩护。
    """
    if slack < 1.0:
        raise ValueError(
            f"slack={slack!r} 必须 ≥ 1（余量系数，<1 意味着要求买到不足一手）"
        )

    kept: list[Any] = []
    dropped: list[tuple[Any, GateVerdict]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            dropped.append(
                (
                    row,
                    _block(
                        RULE_POOL_ROW_INVALID,
                        f"候选行不是对象（{type(row).__name__}）",
                    ),
                )
            )
            continue
        raw_code = str(row.get("code") or "").strip()
        if not raw_code:
            dropped.append((row, _block(RULE_POOL_ROW_INVALID, "候选行缺少 code")))
            continue

        verdict = check_symbol_boundary(raw_code, str(row.get("name") or ""), gate)
        if not verdict.allowed:
            dropped.append((row, verdict))
            continue

        if budget is not None:
            need = _min_lot_cost(raw_code, row.get("price"))
            if need is None:
                dropped.append(
                    (
                        row,
                        _block(
                            RULE_POOL_ROW_INVALID,
                            f"{raw_code} 缺少可用现价：无法判断是否买得起一手",
                        ),
                    )
                )
                continue
            if budget < need * slack:
                dropped.append(
                    (
                        row,
                        _block(
                            RULE_UNAFFORDABLE,
                            f"可用额度 {budget:g} 买不起一手"
                            f"（{raw_code} 需 {need * slack:g}）",
                            min_lot_cost=need * slack,
                            budget=budget,
                        ),
                    )
                )
                continue

        kept.append(row)

    return kept, dropped
