"""决策执行段的 IO 适配：**同一份快照** → 腿 → ``OrderRouter``（P2.3b）。

纯核心在 :mod:`backend.shared.decision.execution`（判「能不能变成一张单、多少股、
什么价」），本模块只负责把它要的东西取来、把它的结论送出去。

三条纪律（都是「别让执行看到的账与模型看到的账不一样」的推论）
----------------------------------------------------------
1. **不重读行情**：持仓与快照由调用点在渲染提示词时读好，原样传进本模块。
   执行段再读一次，就可能出现「模型按 ¥10.00 决策、执行按 ¥10.20 报单」——
   决策与执行之间的价差不是滑点，是**两个数据源**。同一份入参同时喂
   :func:`~backend.shared.decision.context.render_prompt` 与
   :func:`~backend.shared.decision.execution.plan_orders`。
2. **阈值现算但单源**：涨跌停阈值取 ``local_market_data.limit_threshold``（回测引擎
   同源，``tests/test_rule_parity.py`` 钉着），ST 判定用 ``symbol_policy.is_risky_name``
   （与买入闸 ``gates.check_symbol_boundary`` 同一谓词）。行情快照里**没有**涨跌停
   字段（桥的 ``get_market_snapshot`` 不提供），所以不能指望上游给。
3. **在途委托是唯一新增取数**：提示词不需要它，执行必须知道它——同一个账户里
   哨兵、上一轮补投、隔壁桥镜像都可能在途，模型不知道这些。

在途账为**账户级**（``tenant_id`` + ``user_id``，不分 agent）
----------------------------------------------------------
多模型分账下每个 agent 各有一本决策账，但券商只有一个账户：agent A 挂着的卖单与
agent B 想卖的**是同一批股票**。故在途判定按账户取，宁可多拦（下一轮再决策），
不可漏拦（第二笔真委托撤不掉）。这是**有意的过拦**——若日志里出现「被别的 agent
的在途单挡住」的记录，先看那两笔是不是真的同向重叠，再考虑收窄。

读不到在途账 = **本轮不下单**（fail-closed）
------------------------------------------
查在途失败时不能当「没有在途」：那正是重复下单的成因（``close_cleanup_audit_task``
同款纪律：把「查不到」当成「没有残留」会让报表静默变绿）。本模块返回
:class:`ExecutionOutcome`（``aborted`` 非空、零提交），**不发一张单**——少做一轮
调仓下一轮能补，多下一笔真单撤不掉。

实盘开关
--------
真单镜像只在 ``shared.live_trading_gate.is_real_trading_enabled()`` 为真时开启
（``mirror=True`` + ``mirror_source``）；关着的时候腿照走模拟台账（影子期口径），
与 ``push_orders`` 的候选推送同形。

不下单的两件事（与 ``push_orders`` 同纪律，别在这里补）
---------------------------------------------------
* **不包** ``SimulationAccountManager.locked_execution``：``submit_order`` 内部已持
  同用户撮合临界区锁，重入即死锁（``copilot.py`` 有实测注释）。
* **单腿失败不阻断其余**：一条腿报错只记进它的 receipt，后面的腿照发；整批的结果
  由 :meth:`ExecutionOutcome.summary` 如实统计（成功 / 失败 / 去重各自计数）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from backend.shared.decision.execution import (
    ExecutionPlan,
    Holding,
    Leg,
    Quote,
    plan_orders,
)
from backend.shared.order_contract import (
    SOURCE_LLM_DECISION,
    build_llm_decision_client_order_id,
)
from backend.shared.stock_utils import StockCodeUtil

logger = logging.getLogger(__name__)

#: 在途账每本书的取数上限「+1 探测」：**读到超过上限即视为账不可信**，不是截断取前
#: N 条。去重查询少读一行，结果就是多下一笔真委托——把「读不完」和「没有重复」判成
#: 同一件事，正是这套防线最不该有的失误（账真堆到这个量级，本身也该人工看一眼）。
INFLIGHT_LIMIT = 500

#: 快照里可能出现的停牌标记字段（桥/推送两套命名，都没有就是「不知道」）。
_HALTED_KEYS = ("halted", "is_halted", "suspended")

#: 现价字段：标准键契约 ``Now`` 优先，原始推送字段 ``now`` 兜底（同 ``context_source``）。
_PRICE_KEYS = ("Now", "now", "price")
_PRE_CLOSE_KEYS = ("PreClose", "pre_close")


# ── 纯翻译（上下文取数的形状 → 执行段的入参）────────────────────────────
def _pick(obj: object, names: Sequence[str]) -> Any:
    """从 Mapping 或对象上取第一个存在的字段（两种形状都收：桥快照是 dict、
    上下文行是 dataclass）。取不到返回 ``None``——**不编默认值**。"""
    if isinstance(obj, Mapping):
        for n in names:
            if obj.get(n) is not None:
                return obj[n]
        return None
    for n in names:
        value = getattr(obj, n, None)
        if value is not None:
            return value
    return None


def holdings_from_rows(rows: Iterable[object]) -> dict[str, Holding]:
    """上下文持仓行（``decision.context.HoldingRow`` 或同形的 dict）→ 执行段持仓。

    键统一成**后缀式**（与 ``plan_orders`` 的期望一致）；没有代码的行直接丢
    （留着会让 ``sell_not_held`` 的判据面对一个空串键）。``avail`` 缺失 → 0：
    可卖量未知就是不可卖，不是「全可卖」——T+1 未解禁时前者拦下，后者会下出一张
    必被券商废掉的单。
    """
    out: dict[str, Holding] = {}
    for row in rows or ():
        raw = _pick(row, ("code", "symbol"))
        symbol = StockCodeUtil.to_suffix(str(raw or "").strip())
        if not symbol:
            continue
        avail = _ratio(_pick(row, ("avail", "available_volume", "available")))
        out[symbol] = Holding(
            symbol=symbol,
            # 脏值 → 0（不可卖）：可卖量是**我们自己的**类型化字段（``HoldingRow.avail``
            # 是 int），脏说明上游有 bug——按「不可卖」落到 ``l1.t1_sellable``（登记表
            # 里是 structural，不吃影子代价账），比让整个轮次炸掉更合适，也仍然可见。
            available=float(avail or 0),
            name=str(_pick(row, ("name",)) or ""),
        )
    return out


def _ratio(value: object) -> float | None:
    """数量类字段 → float；空/脏/非有限一律 ``None``（**不是 0**）。允许 0 与负值。"""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return out if out == out and out not in (float("inf"), float("-inf")) else None


#: 停牌类布尔字段的文本取值（``_flag`` 用）。**刻意不含空串**：字段在但没值
#: 是「不知道」，不是「没停牌」。
_FLAG_TRUE = frozenset({"1", "t", "true", "y", "yes", "on"})
_FLAG_FALSE = frozenset({"0", "f", "false", "n", "no", "off"})


def _flag(value: object) -> bool | None:
    """停牌类字段 → ``True``/``False``；**读不出语义一律 ``None``**（不知道）。

    这里绝不能用 ``bool(value)``：``bool("0")`` 与 ``bool(float("nan"))`` 都是
    ``True``，而 ``"0"``/``0`` 恰是快照里「没停牌」的常见写法。读反了会拦下一批
    本该买的单，且 ``l4.halted`` 的措辞是「标的停牌（放行也是废单）」——归因指向
    另一个事故。与 ``gates.check_halted`` 的 ``None ≠ False`` 同口径。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return value != 0
    text = str(value).strip().lower()
    if text in _FLAG_TRUE:
        return True
    if text in _FLAG_FALSE:
        return False
    return None


def _price(value: object) -> float | None:
    """价格类字段 → float；**非正数一律 ``None``**（0 是「没采到」的常见写法）。

    这条必须在翻译层拦：0 价放进去会算出 ``day_chg_ratio = 0/pre − 1 = −100%``，
    一个**凭空的跌停**——``at_limit_down`` 于是拦下这条腿并记 ``l4.sell_limit_down``，
    归因指向「跌停卖不掉」，而真相是「没采到价」。错的方向还是「该卖的没卖」。
    """
    out = _ratio(value)
    return out if out is not None and out > 0 else None


def is_st_by_name(symbol: str) -> bool | None:
    """ST/退市判定（名称口径，**三态**）——与买入闸 ``gates.check_symbol_boundary`` 同一谓词。

    ``True``/``False`` = 查到名称后的判定；``None`` = **名称不可得**（未收录 / 取数抛错）。

    ``None`` 不再被压成 ``False``。压成「不是 ST」= 在这一层替调用方**猜一个 10% 板**：
    对一只 5% 板的 ST 股，跌停腿照卖（卖在当日最差价）、涨停腿照买（买在封板）。
    名称索引是仓库外的文件（``data/stocks/stocks_index.json``，不进 git），
    「索引缺失」是**真实存在**的部署状态而不是理论分支——那一刻全市场的 ST 判定
    都变成这个猜测。

    不知道就不判：调用点拿到 ``None`` 一律按「阈值不可得」处置（``quotes_for`` 置
    ``None`` + 汇总告警，纯核心逐条留痕）。这与 ``gates.check_halted`` 的
    ``None ≠ False``、``at_limit_down`` 的「缺一不判」是同一条口径——
    「不知道」可以被容忍，「猜错」不行。
    """
    from backend.shared.symbol_policy import is_risky_name

    try:
        from backend.shared.stock_name_mapper import resolve_name

        name = str(resolve_name(symbol) or "").strip()
    except Exception as exc:  # noqa: BLE001 取不到 ≠ 不是 ST（见 docstring）
        logger.debug("证券简称取不到（%s）：%s", symbol, exc)
        return None
    if not name:
        return None
    return bool(is_risky_name(name))


def quotes_for(
    symbols: Iterable[str],
    snaps: Mapping[str, Mapping[str, Any]],
    *,
    trade_date: Any,
    is_st_of: Callable[[str], bool | None] = is_st_by_name,
    threshold_of: Callable[..., float] | None = None,
) -> dict[str, Quote]:
    """持仓/候选代码 + 快照 → 执行段行情切片。

    ``snaps`` 的键是**后缀式**（``context_source.read_snapshots`` 的既定契约：
    「``code`` 用 suffix 形态做键」），与这里的归一方向一致。喂进来一份前缀式键的
    快照不会静默出错：每只票都会拿到「无价」切片 → ``l3.no_quote`` 否决 → 审计行
    与影子账里看得见（整轮都不下单是显眼的，不是安静的）。

    ``day_chg_ratio`` 与 ``limit_threshold_ratio`` 都是**比例**（0.098 = 跌 9.8%），
    与 ``at_limit_down`` 的期望一致——注意别拿 ``context_source.day_change_pct``
    （那是**百分点**，已 ×100）直接塞进来，量纲错了会宽 100 倍。

    阈值算不出来 → ``None``：跌停**不判**并留痕，**绝不**填一个「大概 10%」——
    那会让 ST 股（5% 板）的跌停腿照卖。两条取不到的路都到这里：阈值函数抛错，
    以及 ``is_st_of`` 返回 ``None``（**名称不可得**，见 :func:`is_st_by_name`——
    ST 是板别的一半输入，一半不知道 = 整个阈值不猜）。
    """
    if threshold_of is None:
        from backend.services.simulation.services.local_market_data import (
            limit_threshold as threshold_of,
        )

    out: dict[str, Quote] = {}
    st_unknown: list[str] = []
    for raw in symbols or ():
        symbol = StockCodeUtil.to_suffix(str(raw or "").strip())
        if not symbol or symbol in out:
            continue
        snap = snaps.get(symbol)
        if not isinstance(snap, Mapping):
            # 没采到快照 ≠ 快照是空的：出一片「只有代码」的切片，让纯核心按
            # 「无价」判（``l3.no_quote``），而不是在这里猜一个价。
            out[symbol] = Quote(symbol=symbol)
            continue
        price = _price(_pick(snap, _PRICE_KEYS))
        pre = _price(_pick(snap, _PRE_CLOSE_KEYS))
        day_chg = price / pre - 1 if price is not None and pre is not None else None
        is_st = is_st_of(symbol)
        if is_st is None:
            # 名称不可得 → 板别的 ST 那半边不知道。**不拿「大概不是 ST」去凑**：
            # 猜 10% 会让 5% 板的 ST 股在跌停被卖、在封板被买（见 is_st_by_name）。
            st_unknown.append(symbol)
            threshold = None
        else:
            try:
                threshold = float(
                    threshold_of(symbol, is_st=is_st, trade_date=trade_date)
                )
            except Exception as exc:  # noqa: BLE001 阈值取不到就不判跌停（纯核心留痕）
                logger.warning("涨跌停阈值不可用（%s）：%s", symbol, exc)
                threshold = None
        halted = _pick(snap, _HALTED_KEYS)
        out[symbol] = Quote(
            symbol=symbol,
            price=price,
            day_chg_ratio=day_chg,
            limit_threshold_ratio=threshold,
            halted=_flag(halted),
        )
    if st_unknown:
        # 一轮一条（不是一只一条）：索引整个缺失时逐只告警会把日志冲垮，
        # 而「本脚本一只都没有」这个事实一条就够说清。逐条的痕迹在执行计划的
        # notes 里（纯核心对「阈值不可用」本来就留痕）。
        logger.warning(
            "ST 判据不可用：%d/%d 只查不到证券简称（示例 %s）→ 这些标的的涨跌停阈值"
            "一律置空（跌停腿照常卖出、涨停腿不拦）。多半是名称索引缺失"
            "（data/stocks/stocks_index.json）",
            len(st_unknown),
            len(out),
            ", ".join(st_unknown[:5]),
        )
    return out


# ── 在途委托（本模块唯一的取数增量）──────────────────────────────────
@dataclass(frozen=True)
class InflightRead:
    """在途账的读取结果。``errors`` 非空 = **账不可信**（调用点必须放弃本轮）。"""

    keys: frozenset[tuple[str, str]] = frozenset()
    errors: tuple[str, ...] = ()
    #: 各本书的条数（如实记录：0 与「没读」是两件事，两者都不等于「没有在途」）
    counts: Mapping[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


def _inflight_key(symbol: object, side: object) -> tuple[str, str] | None:
    """任意形态的 (代码, 方向) → ``(后缀式, 小写)``；任一项取不出 → ``None``。

    方向取 ``.value``：两个台账的 side 都是枚举列，py3.10 下 ``str(OrderSide.BUY)``
    得到 ``"OrderSide.BUY"``——那样构造的键永远匹配不上 ``"buy"``（静默失效，
    正是要防的那类 bug）。
    """
    code = StockCodeUtil.to_suffix(str(symbol or "").strip())
    direction = str(getattr(side, "value", side) or "").strip().lower()
    if not code or direction not in ("buy", "sell"):
        return None
    return (code, direction)


async def read_inflight(
    db, *, tenant_id: str, user_id: object, limit: int = INFLIGHT_LIMIT
) -> InflightRead:
    """账户级在途（未终态）委托 → ``{(后缀式代码, 方向)}``。

    两本书都读：模拟台账（``sim_orders``：pending/submitted）与实盘台账
    （``orders``：REAL + pending/submitted/partially_filled）。任一本读失败 →
    ``errors`` 非空（**不**把「查不到」当「没有」，见模块 docstring）。

    账户身份经 ``simulation_account_keys`` 收口（``10000001`` 规范名）：两张表的
    ``user_id`` **列型不同**（``sim_orders.user_id`` 是 Integer、``orders.user_id``
    是 String(32)），但指同一个账户——各写各的名字正是历史上委托唯一键冲突的成因。
    """
    from backend.shared.simulation_account_keys import (
        normalize_runtime_tenant,
        normalize_runtime_user,
    )

    tenant = normalize_runtime_tenant(tenant_id)
    account = normalize_runtime_user(user_id)

    if not account:
        # ``user_id=0``（本模块各入口的「没给」默认值）归一后是**空串**，不是 "0"。
        # 空账户若照常往下走，两本书都会「查到 0 条」，于是「不知道是谁的账」被读成
        # 「这个账户没有在途」——静默少一道防线。按账不可信办：本轮不下单。
        return InflightRead(
            errors=(
                f"账户身份为空（user_id={user_id!r}）：无法确认在途委托，本轮不下单",
            ),
        )

    keys: set[tuple[str, str]] = set()
    errors: list[str] = []
    counts: dict[str, int] = {}

    # sim 侧：非数字账户在 Integer 列上不可能有行（结构性不存在，不是「没读到」）
    if account.isdigit():
        rows, err = await _read_sim_pending(
            db, tenant_id=tenant, user_id=int(account), limit=limit
        )
    else:
        rows, err = set(), ""
    if err:
        errors.append(err)
    else:
        counts["sim"] = len(rows)
        keys |= rows

    real_rows, real_err = await _read_real_pending(
        db, tenant_id=tenant, user_id=account, limit=limit
    )
    if real_err:
        errors.append(real_err)
    else:
        counts["real"] = len(real_rows)
        keys |= real_rows

    return InflightRead(keys=frozenset(keys), errors=tuple(errors), counts=counts)


async def _read_sim_pending(
    db, *, tenant_id: str, user_id: int, limit: int
) -> tuple[set[tuple[str, str]], str]:
    """模拟台账的非终态委托（``idx_sim_order_tenant_user_status`` 正好覆盖本查询）。"""
    from sqlalchemy import select

    from backend.services.simulation.models.order import OrderStatus, SimOrder

    try:
        rows = (
            await db.execute(
                select(SimOrder.symbol, SimOrder.side)
                .where(
                    SimOrder.tenant_id == tenant_id,
                    SimOrder.user_id == int(user_id),
                    SimOrder.status.in_(
                        [OrderStatus.PENDING.value, OrderStatus.SUBMITTED.value]
                    ),
                )
                .limit(
                    limit + 1
                )  # +1 探测：读到 limit+1 条 = 读不完（见 INFLIGHT_LIMIT）
            )
        ).all()
    except Exception as exc:  # noqa: BLE001 查不到 ⇒ 账不可信（fail-closed 在调用点）
        return set(), f"sim_orders 读取失败：{type(exc).__name__}: {exc}"
    if len(rows) > limit:
        return set(), (
            f"sim_orders 在途委托超过 {limit} 条：去重无法确认（本轮不下单）"
        )
    return {k for k in (_inflight_key(s, sd) for s, sd in rows) if k}, ""


async def _read_real_pending(
    db, *, tenant_id: str, user_id: str, limit: int
) -> tuple[set[tuple[str, str]], str]:
    """实盘台账的非终态委托。

    **不**照抄 ``close_cleanup_audit_task._qmt_channel_clause``：那是「本机 QMT 通道
    残留」的报表口径，而重复下单的风险来自**任何**在途真单（手工/托管/隔壁桥镜像
    都算）。少一个筛子只会多拦，多一个筛子会漏拦。
    """
    from sqlalchemy import select

    from backend.services.trade_shared.models.enums import OrderStatus, TradingMode
    from backend.services.trade_shared.models.order import Order

    try:
        rows = (
            await db.execute(
                select(Order.symbol, Order.side)
                .where(
                    Order.tenant_id == tenant_id,
                    Order.user_id == str(user_id),
                    Order.trading_mode == TradingMode.REAL,
                    Order.status.in_(
                        [
                            OrderStatus.PENDING,
                            OrderStatus.SUBMITTED,
                            OrderStatus.PARTIALLY_FILLED,
                        ]
                    ),
                )
                .limit(limit + 1)
            )
        ).all()
    except Exception as exc:  # noqa: BLE001 同上
        return set(), f"orders(REAL) 读取失败：{type(exc).__name__}: {exc}"
    if len(rows) > limit:
        return (
            set(),
            f"orders(REAL) 在途委托超过 {limit} 条：去重无法确认（本轮不下单）",
        )
    return {k for k in (_inflight_key(s, sd) for s, sd in rows) if k}, ""


# ── 结果对象 ─────────────────────────────────────────────────────────
@dataclass(frozen=True)
class LegReceipt:
    """一条腿的提交回执（成功失败同构；``duplicate`` 是幂等命中，**不是**失败）。"""

    index: int
    symbol: str
    side: str
    quantity: float
    limit_price: float | None
    success: bool
    order_id: str = ""
    message: str = ""
    duplicate: bool = False
    mirror: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "limit_price": self.limit_price,
            "success": self.success,
            "order_id": self.order_id,
            "message": self.message,
            "duplicate": self.duplicate,
            "mirror": dict(self.mirror) if self.mirror else None,
        }


@dataclass(frozen=True)
class ExecutionOutcome:
    """一轮执行的结果：计划（想做什么）+ 回执（做了什么）+ 是否整体放弃。"""

    round_id: str
    plan: ExecutionPlan
    receipts: tuple[LegReceipt, ...] = ()
    #: 非空 = 本轮**一张单都没发**（目前只有「在途账读不到」一种成因）。整段文案直接
    #: 写进审计行的 ``reject_reason``，让「为什么这一轮什么都没做」可查。
    aborted: str = ""

    @property
    def outcomes(self) -> dict[int, dict[str, Any]]:
        """→ ``decision_ledger_store.build_records(outcomes=…)`` 的入参（按决策序号）。

        覆盖「有腿的」与「被拦的」两类；``noop``/``watch`` 故意不进：它们的
        「为什么没单」就在决策原文里（模型说了 hold / watch），系统再编一条
        ``reject_reason`` 等于把模型的话复述成系统的话，反而分不清是谁的决定。
        """
        out: dict[int, dict[str, Any]] = {}
        for veto in self.plan.vetoes:
            out[veto.index] = {
                "armed": False,
                "reject_reason": veto.rule,
                "notes": (veto.reason,),
                "order_id": "",
            }
        for receipt in self.receipts:
            # 与 veto 同序号不可能同时出现（一条决策要么出腿要么被拦），故直接覆盖：
            # 回执比计划更接近事实。
            out[receipt.index] = {
                "armed": bool(receipt.success),
                "reject_reason": "" if receipt.success else receipt.message,
                "notes": (),
                "order_id": receipt.order_id,
            }
        for leg in self.plan.legs:
            # 有腿但没回执 = 提交循环还没轮到它就结束了（aborted / 异常上抛）
            out.setdefault(
                leg.index,
                {
                    "armed": False,
                    "reject_reason": self.aborted or "腿未提交（无回执）",
                    "notes": (leg.note,) if leg.note else (),
                    "order_id": "",
                },
            )
        return out

    @property
    def submitted(self) -> tuple[LegReceipt, ...]:
        """真正发出去的单（幂等命中不算——它没有产生新委托）。"""
        return tuple(r for r in self.receipts if r.success and not r.duplicate)

    @property
    def failed(self) -> tuple[LegReceipt, ...]:
        return tuple(r for r in self.receipts if not r.success)

    def summary(self) -> dict[str, Any]:
        """一行日志/审计用的统计（**如实计数**，含去重命中与失败明细）。"""
        return {
            "round_id": self.round_id,
            "aborted": self.aborted,
            "legs": len(self.plan.legs),
            "submitted": len(self.submitted),
            "duplicates": sum(1 for r in self.receipts if r.duplicate),
            "failed": len(self.failed),
            "vetoes": len(self.plan.vetoes),
            "noops": len(self.plan.noops),
            "watches": len(self.plan.watches),
            "notes": list(self.plan.notes),
        }


# ── 提交 ─────────────────────────────────────────────────────────────
#: 提交器形状：``(leg, client_order_id, real) -> RouterOutcome``。**账户坐标不在参数里**
#: ——它属于「这一轮是谁在跑」，由构造时闭合进去；否则每个测试替身都得先认识租户。
Submitter = Callable[[Leg, str | None, bool], Any]


def _make_default_submitter(
    *, db, redis, tenant_id: str, user_id: object, source: str, agent: str = ""
) -> Submitter:
    """默认提交器：``OrderRouter.submit_order``（唯一入口，链内含风控/幂等/落账）。

    ``real`` 决定是否镜像真单：**模拟腿的成交价仍取服务端快照价**（``order_type``
    用 market），``price``/``real_limit_price`` 只约束镜像出去的那一笔——与
    ``push_orders`` 的候选推送完全同形，别在这里另创一套。

    ``agent`` 由构造时闭合（同 ``tenant_id``：它属于「这一轮是谁在跑」，不是每笔
    腿各自决定的事），随 ``OrderRequest`` 落台账并跟着镜像进真单——成交回报只能
    从订单上读归属（P2.7）。
    """
    from backend.shared.simulation_account_keys import normalize_runtime_user

    account = normalize_runtime_user(user_id)

    async def _submit(leg: Leg, client_order_id: str | None, real: bool):
        from backend.services.simulation.services.order_router import (
            OrderRequest,
            submit_order,
        )

        return await submit_order(
            db,
            redis,
            OrderRequest(
                tenant_id=tenant_id,
                user_id=int(account) if account.isdigit() else 0,
                symbol=leg.symbol,
                side=leg.side,
                quantity=leg.quantity,
                order_type="market",
                price=leg.limit_price,
                source=source,
                client_order_id=client_order_id,
                remarks=(leg.reason or "")[:200],
                mirror=real,
                mirror_source=source if real else "",
                real_limit_price=leg.limit_price if real else None,
                agent=agent,
            ),
        )

    return _submit


async def execute_batch(
    batch,
    *,
    round_id: str,
    holdings: Mapping[str, Holding],
    quotes: Mapping[str, Quote],
    gate: Any = None,
    quota: float | None = None,
    new_buys_round: int = 0,
    inflight: frozenset[tuple[str, str]] = frozenset(),
    agent: str = "",
    db=None,
    redis=None,
    real: bool | None = None,
    submitter: Submitter | None = None,
    tenant_id: str = "default",
    user_id: object = 0,
) -> ExecutionOutcome:
    """一轮决策 → 提交 → 回执。

    ``inflight`` 的**默认值是空集**（不是「读取失败」）：本函数是「计划已给定、
    我照发」的低层入口，取数失败必须由 :func:`run_round` 那层判成 ``aborted``——
    把「不知道」和「没有」压成同一个参数，正是重复下单的温床。

    ``real=None`` 时按 ``is_real_trading_enabled()`` 现读（进程级 env，一轮内不变）。

    ``agent`` 进幂等键（见 :func:`~backend.shared.order_contract.
    build_llm_decision_client_order_id`）：**一轮里有多个 agent 必须传**，否则两家
    模型在同标的同方向上算出同一个键，后一家被静默去重。单 agent 轮次留空即可
    （键与历史一致）。
    """
    plan = plan_orders(
        batch,
        holdings=holdings,
        quotes=quotes,
        gate=gate,
        quota=quota,
        new_buys_round=new_buys_round,
        inflight=inflight,
    )
    if not plan.legs:
        return ExecutionOutcome(round_id=round_id, plan=plan)

    if real is None:
        from backend.shared.live_trading_gate import is_real_trading_enabled

        real = is_real_trading_enabled()

    submit = submitter
    if submit is None:
        submit = _make_default_submitter(
            db=db,
            redis=redis,
            tenant_id=tenant_id,
            user_id=user_id,
            source=SOURCE_LLM_DECISION,
            # 归属与幂等键同源（同一个 ``agent`` 既进 cid 也落订单列）：成交回报回来时
            # 只认订单，届时靠 ``orders.agent`` 才知道这笔该记进哪本分账（P2.7）。
            agent=agent,
        )

    receipts: list[LegReceipt] = []
    for leg in plan.legs:
        key = build_llm_decision_client_order_id(
            round_id, leg.symbol, leg.side, agent=agent
        )
        if key is None:
            # 幂等键缺参 ⇒ 不下。带占位符重试会让**后续轮**的同标的同方向真单撞上
            # 同一个键被静默去重（见 order_contract.build_llm_decision_client_order_id）。
            logger.error(
                "[DecisionExec] 幂等键构造失败（round=%s %s %s）：本轮该腿不下",
                round_id,
                leg.symbol,
                leg.side,
            )
            receipts.append(
                LegReceipt(
                    index=leg.index,
                    symbol=leg.symbol,
                    side=leg.side,
                    quantity=leg.quantity,
                    limit_price=leg.limit_price,
                    success=False,
                    message="幂等键构造失败（round_id/代码/方向缺一）：不下单",
                )
            )
            continue
        try:
            outcome = await submit(leg, key, bool(real))
            receipts.append(
                LegReceipt(
                    index=leg.index,
                    symbol=leg.symbol,
                    side=leg.side,
                    quantity=leg.quantity,
                    limit_price=leg.limit_price,
                    success=bool(getattr(outcome, "success", False)),
                    order_id=str(getattr(outcome, "order_id", "") or ""),
                    message=str(getattr(outcome, "message", "") or "")[:300],
                    duplicate=bool(getattr(outcome, "duplicate", False)),
                    mirror=getattr(outcome, "mirror", None),
                )
            )
        except Exception as exc:  # noqa: BLE001 单腿失败不阻断其余（同 push_orders）
            logger.warning(
                "[DecisionExec] 下单异常 round=%s %s %s: %s",
                round_id,
                leg.symbol,
                leg.side,
                exc,
            )
            receipts.append(
                LegReceipt(
                    index=leg.index,
                    symbol=leg.symbol,
                    side=leg.side,
                    quantity=leg.quantity,
                    limit_price=leg.limit_price,
                    success=False,
                    message=f"{type(exc).__name__}: {exc}"[:300],
                )
            )
    return ExecutionOutcome(round_id=round_id, plan=plan, receipts=tuple(receipts))


async def run_round(
    batch,
    *,
    round_id: str,
    holdings: Mapping[str, Holding],
    quotes: Mapping[str, Quote],
    gate: Any = None,
    quota: float | None = None,
    new_buys_round: int = 0,
    inflight: frozenset[tuple[str, str]] | None = None,
    agent: str = "",
    db=None,
    redis=None,
    real: bool | None = None,
    tenant_id: str = "default",
    user_id: object = 0,
    submitter: Submitter | None = None,
) -> ExecutionOutcome:
    """``execute_batch`` 的编排版：自己读在途账，读不到就**一张单都不发**。

    ``inflight`` 显式给出时跳过取数（调用点已有可信来源，例如刚从券商对账回来）。
    两种失败语义在这里分家：``inflight=None`` + 读取失败 = ``aborted``（本轮不做），
    ``inflight=frozenset()`` = 「我确认没有在途」。
    """
    if inflight is None:
        read = await read_inflight(db, tenant_id=tenant_id, user_id=user_id)
        if not read.ok:
            # 计划照算（留痕：本来想做什么），但一张单都不发。
            plan = plan_orders(
                batch,
                holdings=holdings,
                quotes=quotes,
                gate=gate,
                quota=quota,
                new_buys_round=new_buys_round,
            )
            reason = "在途委托不可信：本轮不下单（fail-closed）— " + "；".join(
                read.errors
            )
            logger.error("[DecisionExec] %s", reason)
            return ExecutionOutcome(round_id=round_id, plan=plan, aborted=reason)
        inflight = read.keys
    return await execute_batch(
        batch,
        round_id=round_id,
        holdings=holdings,
        quotes=quotes,
        gate=gate,
        quota=quota,
        new_buys_round=new_buys_round,
        inflight=inflight,
        agent=agent,
        db=db,
        redis=redis,
        real=real,
        submitter=submitter,
        tenant_id=tenant_id,
        user_id=user_id,
    )
