"""减仓执行器契约与纯函数（P2.6）：档位表 ``leverage_trim_to`` 的消费者。

为什么要有这一层
----------------
闸门里与杠杆有关的只有 ``l1.leverage_cap``（超限**拒买**）——已持仓在杠杆超限时
**不会被压回**。隔壁 2026-09 的实测形态就是「防守档下总敞口仍停在 1.4×：买入侧
全被拒，仓位一动不动」。本层把「档位说该减到多少」变成**可执行、可审计**的减仓计划：
触发线 ``leverage_max``、目标线 ``leverage_trim_to``，两条线之间的带宽就是防抖带。

与隔壁 ``leverage_guard.py`` 的三处差异（**刻意不照抄**，见迁移计划 P2.6）
--------------------------------------------------------------------------
1. **定价不写死 −2%**：报单价由调用点走 ``sltp_executor.resolve_protect_price``
   （默认 ``aggressive`` = ``max(跌停价, 现价×0.99)``）。隔壁写死
   ``round(price*0.98, 2)`` 的那条路在 2026-09-21 造出过 002074 的 42 笔越界废单。
2. **按档位目标减，不贴线补缺口**：目标是 ``trim_to × 权益``。贴线（减到刚好不超限）
   会让价格一抖就再次超限，形成每分钟一卖的抖动；按目标减完落在带宽内，天然防抖。
3. **轮内循环**：按市值从大到小排完所有腿直到覆盖缺口，而不是「一轮只卖最大的一腿」；
   覆盖不全时把缺口与「剩下的腿为什么没卖」**显式记进计划**
   （``remaining_value`` / ``skipped``），不靠下一拍碰运气。

保留隔壁做对的部分：最大腿优先、整手（``lot_rules.align_sell_quantity`` 唯一口径）、
T+1 可卖量复核（``can_use_volume``）、已封跌停不接（``at_limit_down`` 唯一谓词）、
无行情不下手、在途卖单去重（``inflight_dup`` 唯一谓词）。

本模块**无 IO**：账户、行情、档位、在途一律由调用点注入（生产接线见
``leverage_trim.py``）。因此「跌停 / 无价 / 可用 0 / 在途 / 档位缺键」这些真线
一天才碰一次的分支全都能在无网络、无账户下断言。
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from backend.services.live_trading.services.lot_rules import (
    align_sell_quantity,
    is_full_position_sell,
)
from backend.shared.decision.execution import at_limit_down
from backend.shared.risk.registry import get_rule

#: 与 ``builtin_rules._EPS`` 同口径：**等于阈值放行**（``lev > max + eps`` 才算超限）。
_EPS = 1e-9

#: 一轮的动作（字符串常量：要进状态 JSON 与运维端点，不放进枚举）。
ACTION_IDLE = "idle"  # 条件没到 / 有意不动手（在限额内、非交易时段、档位无减仓键）
ACTION_TRIM = "trim"  # 有计划（可能只覆盖部分缺口，见 TrimPlan.remaining_value）
ACTION_BLOCKED = "blocked"  # **该动手却动不了**（数据故障）——必须留痕并告警

#: ``TrimPlan.code`` —— **稳定的成因码**（不含任何每轮都在变的数）。
#: 调用点拿它做告警去重键（「同一成因当日一次」）：用 ``reason`` 当键会因为里面
#: 带着 ``0.4123`` 这样的杠杆数而每轮都算「新成因」，60s 一拍刷成噪声（评审 H3）。
CODE_UNDER_LIMIT = "under-limit"
CODE_NO_LIMITS = "no-limits"
CODE_EQUITY_UNAVAILABLE = "equity-unavailable"
CODE_VALUE_UNAVAILABLE = "value-unavailable"
CODE_NEED_NONPOSITIVE = "need-nonpositive"
CODE_NO_EXECUTABLE_LEG = "no-executable-leg"
CODE_TRIM = "trim"

#: 目标比例的合法带：``trim_to`` 必须落在 ``[leverage_max × 0.5, leverage_max]``。
#: 三档真实取值都在这条带里（calm 1.3/1.5、caution 1.15/1.2、defensive 1.0/1.0）。
#: 带外一律按脏文档拒绝执行：一个手写 ``trim_to=0.01`` 的档位文档会把账户减到 1%，
#: 而强减是**不可逆**的真钱动作——宁可这轮不动手并喊人。
TRIM_TO_MIN_RATIO = 0.5

#: 单轮最多排几条腿（护栏，不是业务规则）：腿再多也只卖这么多，
#: 剩下的下一轮继续（每轮都会重新读账户，不会漏）。
MAX_LEGS_PER_ROUND = 10


def _finite(value: Any) -> float | None:
    """安全浮点：非数 / NaN / ±Inf / 不可解析 → ``None``（**不返回 0**）。"""
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _flag(value: Any) -> bool:
    """配置里的开关位：``risk_gate_service._as_bool`` 的同一词表（它就是这么读的）。

    不直接 ``bool(value)``：配置视图若由原始字符串构造（或测试替身），``"false"``
    是真值——把「配置关着」读成「正在强制」会让执行器按一个不该生效的上限减仓
    （真钱方向不可逆，见 ``decision-round-p28`` 那次同一类教训）。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    return bool(text) and text not in {"0", "false", "no", "off"}


@dataclass(frozen=True)
class TrimLimits:
    """减仓参数：``leverage_max`` 触发线 + ``leverage_trim_to`` 目标线。

    ``leverage_max`` 是**生效上限**：档位文档的值与配置侧正在强制执行的上限取更严者
    （见 ``limits_from_budget`` 的 ``enforcing_cap``）。
    """

    leverage_max: float
    trim_to: float
    level: str = ""
    source: str = ""
    #: 生效上限被配置侧收紧时的**来源注记**（空串 = 上限就是档位文档自己的值）。
    #: 不放内部（只留数字）会让「档位明明写着 1.3，为什么减到 1.0」在事后无从回答。
    cap_note: str = ""

    @property
    def target(self) -> float:
        """目标比例，**不得高于上限**。

        压低上限而目标保持原值会组合出「减到仍超限」的强平循环——
        ``tiers.FALLBACK_LIMITS`` 的注释写明的形态，故在此夹取而不是信任文档。
        配置侧把上限压到档位目标以下时走的正是这条（评审 M2）。
        """
        return min(self.trim_to, self.leverage_max)

    @property
    def describe(self) -> str:
        """给人看的一句来源描述（``TrimPlan.reason`` 里那个括号）。"""
        text = f"档位 {self.level or '?'}/{self.source or '?'}"
        return f"{text}；{self.cap_note}" if self.cap_note else text


#: ``limits_from_budget`` 的三种结局。**「不能减」要分两种**：缺键是档位层的
#: 有意姿态（静默停下），脏文档疑似故障（停下**并喊人**）——两者的运维含义相反，
#: 合并成一个 ``None`` 会让「档位文档被写坏」永远无人知晓。
LIMITS_OK = "ok"  # 可消费
LIMITS_ABSENT = "absent"  # 档位没给减仓参数（含 FALLBACK_LIMITS / 从未定档）
LIMITS_DIRTY = "dirty"  # 给了但不能用（非正 / 非数 / 带外）——疑似脏文档

#: 脏档位的**稳定成因码**（``LimitsRead.code``）：告警去重键用它，不用带数字的文案。
LIMITS_CODE_NO_PARAMS = "no-params"
LIMITS_CODE_UNPARSEABLE = "unparseable"
LIMITS_CODE_NON_POSITIVE = "non-positive"
LIMITS_CODE_OUT_OF_BAND = "out-of-band"
#: 配置侧说自己在强制执行（``enabled`` 且非影子期），给出的上限却不可用。
LIMITS_CODE_CAP_UNUSABLE = "cap-unusable"
#: 配置侧上限与档位上限**互相矛盾**（配置比档位严太多，见 ``TRIM_TO_MIN_RATIO``）。
LIMITS_CODE_CAP_CONFLICT = "cap-conflict"


@dataclass(frozen=True)
class LimitsRead:
    """读档位的结果：``limits`` 为 ``None`` 时看 ``kind`` 决定是静默还是喊人。"""

    limits: TrimLimits | None
    reason: str = ""
    kind: str = LIMITS_OK
    #: 稳定成因码（见 ``LIMITS_CODE_*``；只在 ``kind != ok`` 时有意义）。
    code: str = ""


def limits_from_budget(
    budget: Mapping[str, Any] | None,
    *,
    level: str = "",
    source: str = "",
    enforcing_cap: float | None = None,
) -> LimitsRead:
    """档位预算 → :class:`LimitsRead`（参数 + 拒绝原因 + **拒绝的类别**）。

    ``enforcing_cap`` = **配置侧正在强制执行**的总杠杆上限（闸门的合并口径，
    见 ``risk_gate_service.load_config``：``min(配置值, 档位值)``）。给了就取更严者；
    没给（配置关着/影子期/读不出）表示配置侧不构成约束，只按档位走（评审 M2）。

    **缺键是有意姿态而非故障**：``tiers.FALLBACK_LIMITS`` 有意不含这两个键
    （「强减是风险动作，数据故障不应触发强平」）。故缺键归 ``absent``，
    由调用点记成 ``idle`` 且**不告警**；带外/非正/非数归 ``dirty``，
    同样是 ``idle``（不动手）但要留痕喊人。
    """
    raw = budget or {}
    # 「没给」与「给坏了」的分界看**键在不在**，不看能不能解析：档位文档里写了
    # 一个 ``null``/``"abc"``/``NaN`` 是写坏了（要喊人），两个键都没有才是
    # 档位层的有意姿态（静默）。
    has_max = raw.get("leverage_max") not in (None, "")
    has_trim = raw.get("leverage_trim_to") not in (None, "")
    if not has_max and not has_trim:
        return LimitsRead(
            None,
            "档位未给出减仓参数（leverage_max/leverage_trim_to 缺一）："
            "结构性故障与「从未定档」都落这一姿态，按档位层的设计不动手",
            LIMITS_ABSENT,
            LIMITS_CODE_NO_PARAMS,
        )
    max_lev = _finite(raw.get("leverage_max"))
    trim_to = _finite(raw.get("leverage_trim_to"))
    if max_lev is None or trim_to is None:
        return LimitsRead(
            None,
            f"档位减仓参数缺失或不可解析（max={raw.get('leverage_max')!r} "
            f"trim_to={raw.get('leverage_trim_to')!r}）：疑似脏文档，不动手",
            LIMITS_DIRTY,
            LIMITS_CODE_UNPARSEABLE,
        )
    if max_lev <= 0 or trim_to <= 0:
        return LimitsRead(
            None,
            f"档位减仓参数非正（max={max_lev:g} trim_to={trim_to:g}）：疑似脏文档，不动手",
            LIMITS_DIRTY,
            LIMITS_CODE_NON_POSITIVE,
        )
    if trim_to < max_lev * TRIM_TO_MIN_RATIO:
        return LimitsRead(
            None,
            f"档位目标 {trim_to:g} 低于上限 {max_lev:g} 的 "
            f"{TRIM_TO_MIN_RATIO:.0%}（带外）：疑似脏文档，不动手",
            LIMITS_DIRTY,
            LIMITS_CODE_OUT_OF_BAND,
        )
    # 配置侧正在强制执行的上限**只收紧、不放大**（``tiers.apply_to_rules`` 的 min 合并）。
    # 不合并会在 ``[配置上限, 档位上限)`` 留一条**死区**：闸门已按更严的配置上限拒买，
    # 而本执行器按档位上限判「没超」→ 账户整天钉在超限里没人压仓（评审 M2）。
    cap = _finite(enforcing_cap)
    if enforcing_cap is not None and (cap is None or cap <= 0):
        return LimitsRead(
            None,
            f"配置侧强制上限不可用（{enforcing_cap!r}）：不知道它强制多少就不按它减仓，"
            "也不假装它不存在（配置的 ``l1.leverage_cap.max_leverage`` 须为正数）",
            LIMITS_DIRTY,
            LIMITS_CODE_CAP_UNUSABLE,
        )
    cap_note = ""
    if cap is not None and cap < max_lev:
        if cap < max_lev * TRIM_TO_MIN_RATIO:
            # 两侧差得太远（配置要把账户压到档位上限的一半以下）：按配置减 = 一轮卖掉
            # 半个账户，按档位减 = 无视配置。两个方向都在赌，而**强减不可逆**——
            # 停手喊人，让人去对齐这两个数（这是配置，不是行情，不会自己变回来）。
            return LimitsRead(
                None,
                f"配置侧强制上限 {cap:g} 与档位上限 {max_lev:g} 矛盾（低于其 "
                f"{TRIM_TO_MIN_RATIO:.0%}）：按哪个减都不能自动决定，不动手。"
                "请对齐配置的 ``l1.leverage_cap.max_leverage`` 与档位表",
                LIMITS_DIRTY,
                LIMITS_CODE_CAP_CONFLICT,
            )
        cap_note = f"配置侧强制上限 {cap:g} 更严（档位 {max_lev:g}），按配置减"
        max_lev = cap
    return LimitsRead(
        TrimLimits(
            leverage_max=max_lev,
            trim_to=trim_to,
            level=str(level),
            source=str(source),
            cap_note=cap_note,
        )
    )


#: 配置侧强制上限所在的规则与参数键（与 ``tiers.TARGETS`` / ``risk_gate_service``
#: 同一把键）。**规则默认值不在此复制**：从注册表读——闸门就是「规则默认值 ⊎ 配置条目」
#: 地取参的（``shared/risk/engine.py``: ``params.update(entry)``），抄一份默认值会在
#: 那天改默认时静默分叉。
CAP_RULE_ID = "l1.leverage_cap"
CAP_PARAM = "max_leverage"


def enforcing_cap_from_config(config: Any) -> tuple[float | None, str]:
    """风险配置视图 → 正在强制执行的 ``l1.leverage_cap.max_leverage``：``(值, 问题)``。

    ``config`` 是 ``risk_gate_service.load_config`` 的返回值（``RiskConfig | None``），
    按鸭子类型读 ``enabled`` / ``shadow`` / ``rules``——本层禁 IO 依赖，故不 import 那个
    dataclass；判据留在纯层，可单测。

    为什么需要它（评审 M2）：``tiers.apply_to_rules`` 把档位合进配置时取**更严者**，
    真正在拦单的上限因此是 ``min(配置值, 档位值)``。本执行器只读档位就会在
    ``[配置上限, 档位上限)`` 这段**死区**里判「没超限」：闸门已按更严的配置上限拒买，
    账户却整天没人压仓。

    ``(None, "")`` 的三种情形都**不是**故障：配置关着（``enabled`` 假）、影子期
    （只记不拦）、配置根本没列出这条规则——三者都表示配置侧不构成约束。
    非空问题串 = 配置说自己在强制执行，却读不出它强制多少（缺参/坏值/规则未注册），
    调用点必须**停手喊人**：数据故障不是卖出信号。
    """
    if config is None:
        return None, ""
    if not _flag(getattr(config, "enabled", False)) or _flag(
        getattr(config, "shadow", False)
    ):
        return None, ""
    rules = getattr(config, "rules", None)
    if not isinstance(rules, Mapping) or CAP_RULE_ID not in rules:
        return None, ""
    entry = rules.get(CAP_RULE_ID)
    if entry is not None and not isinstance(entry, Mapping):
        return None, f"配置的 {CAP_RULE_ID} 不是参数对象（{entry!r}）"
    import backend.shared.risk.builtin_rules  # noqa: F401 - 确保内置规则已注册

    spec = get_rule(CAP_RULE_ID)
    if spec is None:
        return None, f"本进程未注册规则 {CAP_RULE_ID}（配置比代码新）"
    params: dict[str, Any] = dict(getattr(spec, "default_params", None) or {})
    if isinstance(entry, Mapping):
        params.update(entry)
    raw = params.get(CAP_PARAM)
    cap = _finite(raw)
    if cap is None or cap <= 0:
        return None, (
            f"配置侧强制上限不可用：{CAP_RULE_ID}.{CAP_PARAM}={raw!r}（须为正数）"
        )
    return cap, ""


@dataclass(frozen=True)
class LegInput:
    """一腿的**原始输入**（调用点取数，本层不碰 IO）。

    ``price`` / ``available`` 的 ``None`` 与 0 是两种意思，分得越细文案越有用：
    「读不出」是链路问题，「是 0」是 T+1 锁定或已被挂单占用。
    """

    symbol: str  # 后缀式（``600036.SH``）
    volume: float  # 持仓量（柜台口径，含当日买入未解锁部分）
    available: float | None = None  # 柜台可用量（T+1 可卖）；None = 读不出
    price: float | None = None  # 实时价（桥 tick）；None = 无行情
    market_value: float | None = None  # 柜台自报市值（无价时估值兜底，不用于报价）
    day_chg_ratio: float | None = None  # 当日涨跌幅（**比例**：-0.098 = 跌 9.8%）
    limit_threshold_ratio: float | None = None  # 该股涨跌停阈值（比例，板别/ST 口径）
    inflight: bool = False  # 同标的同方向已有在途委托（本仓已出腿或跨轮在途）
    name: str = ""  # 证券简称（仅文案，可空）

    @property
    def value(self) -> float:
        """这一腿在**总敞口**里的估值——与闸门 ``l1.leverage_cap`` 的分子同口径。

        三条与闸门 ``_sum_position_value`` 对齐的规则（三条都有实测来由）：

        * 有实时价用实时价，否则退回柜台自报市值——绝不用成本价充当市价（那是另一个
          量纲的错误来源）；
        * **量为 0 的行整行不计**：柜台快照会留已清仓的历史行（真账户实测有 4 个幻影
          标的），残值一旦非 0 就会虚增分子，让本执行器按一个偏大的缺口**多卖**——
          闸门同样跳过 0 量行，分子必须逐字对齐，否则「闸门说没超、执行器说超了」；
        * **取绝对值**：分母（权益）是净资产，分子按**总敞口**计，空头不能净掉多头。
        """
        vol = _finite(self.volume)
        if vol is None or vol == 0:
            return 0.0
        px = _finite(self.price)
        if px is not None and px > 0:
            return abs(px * vol)
        return abs(_finite(self.market_value) or 0.0)


def aggregate_value(reported: float | None, legs: Iterable[LegInput]) -> float:
    """账户持仓市值合计 = ``max(账户自报, 逐腿重建)``（**总敞口**口径）。

    与闸门 ``risk_gate_service._sum_position_value`` 的「两口径取大」同向：
    自报值可能漏腿（快照列滞后），逐腿重建可能因无行情而低配——取大是保守侧，
    两个数都拿不到时是 0（调用点会因此判「未超限」，见 ``plan_trim`` 的权益闸）。
    """
    rebuilt = math.fsum(leg.value for leg in legs)
    rep = _finite(reported)
    return max(rep, rebuilt) if rep is not None else rebuilt


@dataclass(frozen=True)
class LegPlan:
    """一条**待报**的减仓腿（数量已过整手口径；价格是实时价，报单价由调用点定）。"""

    symbol: str
    quantity: float
    price: float
    value: float
    note: str = ""
    #: 这一腿卖的是**全部**实时可用量（整仓卖出）。派发层的整手预检只能看当日快照，
    #: 快照比实时大时会把合法的碎股全清判成整手违规 —— 带着它随委托下发，判据见
    #: ``lot_rules.is_full_position_sell``（评审 M4）。
    full_exit: bool = False


@dataclass(frozen=True)
class TrimPlan:
    """一轮减仓计划（纯数据；调用点负责报价、下单与留痕）。"""

    action: str
    reason: str
    #: 稳定成因码（见 ``CODE_*``）：给告警去重键用。``reason`` 是给人看的（含数字、
    #: 每轮都在变），**不能**当键；两者分工见 ``CODE_UNDER_LIMIT`` 处的注释。
    code: str = ""
    leverage: float | None = None
    equity: float | None = None
    position_value: float | None = None
    target_value: float | None = None
    need_value: float = 0.0
    legs: tuple[LegPlan, ...] = ()
    skipped: tuple[tuple[str, str], ...] = field(default=())  # (代码, 原因)

    @property
    def planned_value(self) -> float:
        return math.fsum(leg.value for leg in self.legs)

    @property
    def remaining_value(self) -> float:
        """计划覆盖不到的缺口（>0 = 本轮修不完，下一轮接着来；**不许当 0 静默**）。"""
        return max(0.0, float(self.need_value) - self.planned_value)

    @property
    def overshoot_value(self) -> float:
        """计划**超出**缺口的部分（>0 = 超卖，元的绝对值）。

        整手取整的必然产物：缺口 3,000 元、最小一手 9,900 元时，按手数只能卖一手
        （``align_sell_quantity`` 的「不足最小申报量抬到最小申报量」/「剩余碎股全清」）。
        评审 M5 的追问是「超卖幅度相对缺口不封顶」——**不封顶是对的，但要可见**：
        一手封顶的是**绝对**幅度（≤ 1 手/腿/轮），停手换不来别的，只把账户**永久留在
        触发线上方**（那正是本执行器唯一的职责所在，且与隔壁实盘已验证的「补到触发线」
        冲突）。故这里是**披露字段**而非停手条件，调用点据此在面板/通知/文案里说明。
        """
        return max(0.0, self.planned_value - float(self.need_value))


def _leg_skip_reason(leg: LegInput) -> str:
    """这一腿能不能卖（返回原因字符串；``""`` = 可卖）。判定顺序即文案优先级。"""
    if leg.inflight:
        return "已有在途卖单（同标的同方向，等它落地再算）"
    vol = _finite(leg.volume) or 0.0
    if vol <= 0:
        return "持仓为 0"
    px = _finite(leg.price)
    if px is None or px <= 0:
        return "无行情（无价不报价，下一轮再看）"
    if at_limit_down(leg.day_chg_ratio, leg.limit_threshold_ratio):
        return "已封跌停（卖不出去只会占住可用量，下一轮再看）"
    if leg.available is None:
        return "柜台可用量读不出（T+1 未复核，不猜）"
    if float(leg.available) <= 0:
        return "柜台可用 0（T+1 锁定或已被挂单占用）"
    return ""


def plan_trim(
    *,
    equity: float | None,
    position_value: float | None,
    legs: Iterable[LegInput],
    limits: TrimLimits | None,
    limits_reason: str = "",
) -> TrimPlan:
    """一轮减仓计划（**纯函数**，同入参同结果）。

    顺序即风控优先级：**先确认有没有分母（权益），再确认超没超线，最后才谈卖什么**。
    权益/敞口读不出时一律 ``blocked`` 且不下手——按一个假的小分母去强减，
    等于拿数据故障当卖出信号（``tiers.FALLBACK_LIMITS`` 的同一条理由）。
    """
    if limits is None:
        return TrimPlan(
            action=ACTION_IDLE,
            code=CODE_NO_LIMITS,
            reason=limits_reason or "档位未给出减仓参数",
        )

    eq = _finite(equity)
    if eq is None or eq <= 0:
        return TrimPlan(
            action=ACTION_BLOCKED,
            code=CODE_EQUITY_UNAVAILABLE,
            reason=f"账户权益不可得（{equity!r}）：没有分母就不减仓（不拿脏数当卖出信号）",
        )
    val = _finite(position_value)
    if val is None:
        return TrimPlan(
            action=ACTION_BLOCKED,
            code=CODE_VALUE_UNAVAILABLE,
            reason="持仓市值合计不可得：算不出杠杆就不减仓",
            equity=eq,
        )
    lev = val / eq
    if lev <= limits.leverage_max + _EPS:
        return TrimPlan(
            action=ACTION_IDLE,
            code=CODE_UNDER_LIMIT,
            reason=(
                f"总杠杆 {lev:.4f} 未超上限 {limits.leverage_max:g}"
                f"（{limits.describe}）"
            ),
            leverage=lev,
            equity=eq,
            position_value=val,
        )

    target_value = limits.target * eq
    need = val - target_value
    if need <= 0:
        # 目标 ≤ 上限且 lev > 上限时不该发生；真发生说明入参自相矛盾 → 不动手并说清。
        return TrimPlan(
            action=ACTION_BLOCKED,
            code=CODE_NEED_NONPOSITIVE,
            reason=f"缺口非正（敞口 {val:.2f} ≤ 目标 {target_value:.2f}）却判超限：入参矛盾，不动手",
            leverage=lev,
            equity=eq,
            position_value=val,
            target_value=target_value,
        )

    items = list(legs)
    # **没有「缺口小于一手就不动手」的闸**（评审 M5 复核后的结论，别再往回改）：
    # 缺口 3,000 元 / 最小一手 9,900 元时，按手数只能卖一手（超卖 3.3×），而停手的
    # 代价是把账户**永久留在触发线上方**——`idle` 与 `lev > max` 并存会让面板自相
    # 矛盾，也与隔壁实盘已验证的「补到触发线」语义相反。超卖**绝对幅度**本来就封顶：
    # 每条腿最多抬到最小申报量（或把碎股一次清掉），且循环在 ``remaining <= 0`` 即停，
    # 故一轮超卖 ≤ 1 手/腿。处置是**可见**（``TrimPlan.overshoot_value`` + 计划文案 +
    # 面板 + 通知），不是不动手。
    ordered = sorted(items, key=lambda item: item.value, reverse=True)
    planned: list[LegPlan] = []
    skipped: list[tuple[str, str]] = []
    remaining = need
    for leg in ordered:
        if remaining <= 0 or len(planned) >= MAX_LEGS_PER_ROUND:
            break
        why = _leg_skip_reason(leg)
        if why:
            skipped.append((leg.symbol, why))
            continue
        price = float(leg.price)  # _leg_skip_reason 已确认 > 0
        # 缺口是「至少卖这么多」→ 向上取整到股；整手合规交给唯一口径 align_sell_quantity
        # （它按最近整手取整并处理碎股全清，见 2026-09-08 的 600×33% 实录）。
        want = math.ceil(remaining / price)
        qty, note = align_sell_quantity(leg.symbol, want, float(leg.available))
        if qty <= 0:
            skipped.append((leg.symbol, note or "可卖数量算得 0"))
            continue
        value = qty * price
        planned.append(
            LegPlan(
                leg.symbol,
                qty,
                price,
                value,
                note,
                # 卖光实时可用量 = 整仓卖出（零股合法）：派发层据此放行碎股，
                # 不再拿当日的旧快照反推「这是不是全清」。
                full_exit=is_full_position_sell("SELL", qty, leg.available),
            )
        )
        remaining -= value

    if not planned:
        # 超限却一条腿都排不出来 = **该动手却动不了**：必须显眼（原因在 skipped 里）。
        detail = "；".join(f"{sym}：{why}" for sym, why in skipped[:5]) or "无持仓腿"
        return TrimPlan(
            action=ACTION_BLOCKED,
            code=CODE_NO_EXECUTABLE_LEG,
            reason=(
                f"总杠杆 {lev:.4f} 超上限 {limits.leverage_max:g}，但本轮无可执行腿——{detail}"
            ),
            leverage=lev,
            equity=eq,
            position_value=val,
            target_value=target_value,
            need_value=need,
            skipped=tuple(skipped),
        )

    planned_total = math.fsum(item.value for item in planned)
    over = planned_total - need
    partial = "；缺口未覆盖，下一轮继续" if remaining > 0 else ""
    # 整手取整带来的超卖要在**文案里**（评审 M5）：面板 / 日志 / 通知都读这一句，
    # 光有结构化字段没人看。超卖不是错，但必须是个**看得见的数**。
    overshoot_note = f"；整手取整超卖 {over:.2f} 元" if over > _EPS else ""
    return TrimPlan(
        action=ACTION_TRIM,
        code=CODE_TRIM,
        reason=(
            f"总杠杆 {lev:.4f} 超上限 {limits.leverage_max:g}（{limits.describe}），"
            f"目标 {limits.target:g}"
            f"（≈{target_value:.2f} 元）；计划卖出 {len(planned)} 腿"
            f"{partial}{overshoot_note}"
        ),
        leverage=lev,
        equity=eq,
        position_value=val,
        target_value=target_value,
        need_value=need,
        legs=tuple(planned),
        skipped=tuple(skipped),
    )


def trim_client_order_id(symbol: str, day: str, generation: int) -> str:
    """减仓单当日幂等委托号：``trim-<code>-<YYYYMMDD>-g<N>``。

    与 sltp 的 ``rule_client_order_id`` 同一条理由：触发后进程崩溃 / 状态写回失败时
    按同号重试，``orders.client_order_id`` 唯一索引 + 派发层的先查后插会返回已有委托
    而不是重复下单——**下真单的链路不允许靠状态机兜底防重**。

    ``generation`` = 当日该标的第几次减仓：同一标的一天里可以合法地减两次
    （上一笔成交后价格又涨回触发线之上），故按当日**已用掉的号**递增
    （已确认提交的 + 已作废的：被拒或幂等命中的那一轮也落过委托行，号不能再用）。

    **只看成功笔数是错的**（评审 HIGH-1）：被拒的号在下一轮被重算出来，会撞上派发层
    按 cid 先查后插（不看状态）的那一行，得到 ok=True 的「幂等命中」——尝试计数永不
    增长，停手闸永不触发。防重由 ``read_inflight`` 负责，不靠号。
    """
    gen = max(1, int(generation or 1))
    return f"trim-{symbol}-{str(day or '').strip()}-g{gen}"
