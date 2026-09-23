"""P2.7 分账账本纯核心（无 IO）：几个决策 agent 共用**一个真实账户**时的虚拟子账。

为什么要有这一层
----------------
多模型竞争下每家模型各自决策，但券商只有一个账户——**持仓是同一批股票**。
2026-09-08 隔壁实录：`pro` 卖了 `flash` 的生益电子。根因不在「卖出门没拦」，
而在**提示词把共享账户的全量持仓当成「你名下」喂给了空账本的 agent**
（`scripts/live_llm_trade.py:707-710` 的注释逐字记着这次事故）。故本层的核心
只有一个动作：**把「这本账里有没有它」变成可见性**（:func:`mine_of`），
卖出门（``execution.RULE_SELL_NOT_HELD``）是第二道，不是第一道。

账本形态**逐字对齐隔壁 `logs/live_ledger.json` 的 `agents` 段**
--------------------------------------------------------------
    {"agents": {"<模型名>": {"positions": {"<后缀码>": {"volume", "cost_price",
                                                        "buy_ts", "last_ts"}},
                             "virtual_cash": <float>}}}

不是照抄，是因为**读侧已经写好了**：``decision/context.ledger_cost_rows`` 的入参
契约就是 ``{code: {"volume", "cost_price", …}}``（提示词的成本列与盈亏列按它渲染），
换成别的形态等于两处口径各写一遍。agent 键 = ``LLMBinding.model``（与隔壁同口径，
**不另造 agent 身份**）。

三处**刻意与隔壁不同**（都往「静默」的反方向走，逐条有单测点名）
----------------------------------------------------------------
1. **超额卖出按持有量夹取后再记现金**。隔壁用未夹取的 `volume` 记现金、却按夹取量
   删持仓（``scripts/account_protocol.py:169-196``）——持有 100 股卖 500 时持仓删掉、
   现金多进 400 股的钱。隔壁自己在 ``live_fills.py:163`` 的注释里承认了这点，选择在
   **调用点**绕开；资金口径的账不该要求调用点替它把关。
2. **卖出未记账必须留痕**。隔壁 ``code not in pos`` 时 ``return ledger``：调用方拿到
   一本**看起来正常的账**，而「这次卖出没记上」在账本里没有任何痕迹。台账与柜台
   漂移是最难查的一类事故，故本仓返回 :class:`LedgerChange`，``applied=0`` 时
   ``note`` 必须说清为什么。
3. **金额/数量非有限或非正一律拒**。隔壁只对成交价做了 ±40% 坏价闸
   （:func:`sane_fill_price`，逐字移植，见下），对 NaN/Inf/负数没有统一守卫；
   ``inf > 0`` 为真，一笔 Inf 成本会把这本账永久污染成 nan。

坏价闸（逐字移植隔壁实测口径，**不是**自由发挥）
------------------------------------------------
:func:`sane_fill_price`：``|fp/ref − 1| > 40%`` → 记 ``ref`` 并置 ``approx``。
出处是隔壁 2026-09-08 实录：桥报 001312 成交价 4.789 而实时价 17.5，坏成本入账
虚增 pro 虚拟净值约 1.4 万。40% 的余量来自「全市场最大涨跌停 ±30%」。

本层刻意不做的事（别在这里补）
------------------------------
* **不落盘**：持久化在 store 层（PG + 唯一索引），本层只收 dict、只返回新 dict。
* **不判资金够不够**：那是 ``gates.BuyGate``（``RULE_VCASH``）与执行段
  ``l1.available_cash`` 的事；本层只**供给** ``virtual_cash`` 这个数。
* **不做成交去重的判定**：唯一键是 DB 的事，本层只提供幂等**算术**
  （:func:`recorded_baseline` / :func:`fill_delta`）。

期初结转（P3 数据迁移）
-----------------------
:func:`parse_legacy_ledger` 把隔壁 ``logs/live_ledger.json`` 解析成结转计划（纯函数，
同一份文件两次解析逐字相同）。切换日之后本仓账本从零起，而三个 agent 名下的仓已经在
真实账户里——不搬这一段，模型看不见自己的持仓（``mine_of`` 全空），与 2026-09-08
事故同族、只是方向反过来。只搬 ``agents`` 段；搬的是**状态**，故 store 侧另有一条
只许在空账本上执行的结转写入（见 ``agent_ledger_store.import_legacy_seed``）。

但**解析结果不是可落库的计划**：隔壁台账是派生数据，卖出没归因回去时它只增不减
（实测多出两只幻影仓）。故中间必须有 :func:`reconcile_seed_with_bridge`——**仓位唯一
事实源是桥**（PG ``real_account_snapshots``），台账只提供归属；幻影剔除、单一认领人
按桥锚定量、孤仓显式记录、人工判定留理由，最后过一道双向对账断言才准落库。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from backend.shared.stock_utils import StockCodeUtil
from backend.shared.utc_datetime import as_utc, to_utc_iso

__all__ = [
    "BRIDGE_ANCHORED",
    "BRIDGE_DRIFT",
    "BRIDGE_MATCH",
    "BRIDGE_ORPHAN",
    "BRIDGE_OVERRIDE",
    "BRIDGE_PHANTOM",
    "DEFAULT_AGENT_QUOTA",
    "SEED_FILL_PREFIX",
    "BridgePosition",
    "LedgerChange",
    "LegacySeed",
    "ReconcileRecord",
    "SeedAgent",
    "SeedPosition",
    "SeedReconciliation",
    "agent_cash",
    "agent_positions",
    "agent_remaining",
    "agent_used",
    "bridge_positions",
    "bridge_rows_from_view",
    "ensure_agent",
    "fill_delta",
    "holding_days",
    "mine_of",
    "parse_legacy_ledger",
    "reconcile_seed_with_bridge",
    "record_buy",
    "record_sell",
    "recorded_baseline",
    "sane_fill_price",
    "seed_fill_key",
]

#: 每个 agent 的初始虚拟额度（元）。¥10 万是隔壁 ``scripts/live_ledger.py:33`` 的
#: ``AGENT_QUOTA``——**名义额度，与真实账户资金解耦**（隔壁账户里另有约 ¥92 万
#: 2026-08-31 之前的既存仓，明确不入分账）。真实资金闸在**执行段**，本值只决定
#: 「这家模型这条线还能买多少」，不决定「账户还有多少钱」。
DEFAULT_AGENT_QUOTA = 100_000.0

#: 坏 tick 的相对带宽：|成交价/参考价 − 1| 超过它就按参考价记并标 approx。
#: 取值理由见模块 docstring（全市场最大涨跌停 ±30%，留 10 个百分点余量）。
BAD_TICK_TOLERANCE = 0.40


@dataclass(frozen=True, slots=True)
class LedgerChange:
    """一次记账的结果：**新账本 + 记了多少 + 为什么没记全**。

    ``applied`` 是**实际**记进账的数量（夹取后），不是入参数量——调用点拿它写
    审计与幂等标记，拿错就等于把差额当成已成交。``note`` 非空即代表这次记账
    与入参不一致（超卖、卖非持仓、脏值），**必须进日志/告警**，不是可选项。
    """

    ledger: dict[str, Any]
    applied: float = 0.0
    note: str = ""


# ── 读侧（全部容错：账本脏值不许炸穿调用点）────────────────────────────


def _agents(ledger: Any) -> dict[str, Any]:
    if not isinstance(ledger, dict):
        return {}
    agents = ledger.get("agents")
    return agents if isinstance(agents, dict) else {}


def agent_positions(ledger: Any, agent: str) -> dict[str, Any]:
    """该 agent 的持仓段（``{后缀码: {...}}``）；任何形态坏值 → 空 dict。

    这里**不做 ``or {}`` 之外的修补**：账本顶层坏掉（load 失败）与「这本账是空的」
    长得一样，但前者必须由调用点判成 fail-closed（读不出就不动手），故调用点要拿
    原始返回值做区分，而不是让本函数臆造。
    """
    rec = _agents(ledger).get(agent)
    if not isinstance(rec, dict):
        return {}
    pos = rec.get("positions")
    return pos if isinstance(pos, dict) else {}


def mine_of(ledger: Any, agent: str) -> frozenset[str]:
    """**本 agent 名下有哪几只**——互卖防线的唯一判据。

    调用点拿它裁剪持仓表：``[h for h in holdings if h.code in mine_of(...)]``。
    空账本 = 无持仓（**不是**「没配分账，那就全给我」）。隔壁 2026-09-08 的事故
    正是 ``if mine else holdings`` 这个兜底写的，那行代码的代价是一次跨 agent 卖仓。
    """
    return frozenset(agent_positions(ledger, agent))


def agent_cash(ledger: Any, agent: str, quota: float = DEFAULT_AGENT_QUOTA) -> float:
    """该 agent 的虚拟现金。**键缺失**才回退 ``quota``——恰好为 0 是合法值。

    隔壁 2026-09-18 评审 L-5 的同款坑：``rec.get("virtual_cash") or quota`` 把
    现金恰好用完的账户读成满额，等于给透支的线放开一个 quota 的买入力。
    """
    rec = _agents(ledger).get(agent)
    if not isinstance(rec, dict):
        return quota
    v = rec.get("virtual_cash")
    if v is None or isinstance(v, bool):
        return quota
    try:
        f = float(v)
    except (TypeError, ValueError):
        return quota
    return f if math.isfinite(f) else quota


def position_cost(ledger: Any, agent: str, code: str) -> float:
    """该 agent 名下**某一只**的成本（``volume × cost_price``）；无仓/脏值 → 0.0。"""
    held = agent_positions(ledger, agent).get(code)
    if not isinstance(held, dict):
        return 0.0
    v = _finite(held.get("volume"))
    c = _finite(held.get("cost_price"))
    if v is None or c is None:
        return 0.0
    return round(v * c, 2)


def agent_used(ledger: Any, agent: str) -> float:
    """**已用额度 = 现持仓成本合计**（不是累计买入额：卖出即释放）。

    与 ``virtual_cash`` 是两个不同的数：``used`` 是「买了多少货」，``virtual_cash``
    是「账上还剩多少钱」。隔壁两条线都要（单票预算取两者与比例的最小值），
    对「已实现亏损把现金打下去、但仓位还很重」的账户，只判一条会放开买入。
    """
    total = 0.0
    for code in agent_positions(ledger, agent):
        total += position_cost(ledger, agent, code)
    return round(total, 2)


def agent_remaining(
    ledger: Any, agent: str, quota: float = DEFAULT_AGENT_QUOTA
) -> float:
    """剩余额度 = ``quota − used``。**超用如实为负**，不夹到 0。"""
    return round(quota - agent_used(ledger, agent), 2)


# ── 坏价闸 ───────────────────────────────────────────────────────────


def sane_fill_price(fill_price: Any, ref_price: Any) -> tuple[Any, bool]:
    """成交价护栏（逐字移植隔壁 ``live_ledger.sane_fill_price``）。

    越界返回 ``(ref, True)`` 供调用点按参考价记账并标 ``approx``；正常原样返回。
    任一侧 ≤ 0 或非数 → 不判（返回原值，``False``）——参考价都没有时，「坏不坏」
    本身无从谈起，此时**不臆造一个参考价**。
    """
    fp = _finite(fill_price)
    ref = _finite(ref_price)
    if fp is None or ref is None or fp <= 0 or ref <= 0:
        return fill_price, False
    if (1.0 - BAD_TICK_TOLERANCE) <= fp / ref <= (1.0 + BAD_TICK_TOLERANCE):
        return fill_price, False
    return round(ref, 2), True


# ── 写侧（不可变：返回新账本）────────────────────────────────────────


def ensure_agent(ledger: Any, agent: str, quota: float = DEFAULT_AGENT_QUOTA) -> dict:
    """确保 agent 有条目（初始虚拟现金 = ``quota``）。已存在则**原样返回**。

    ``quota`` **不写进账本**：它是绑定层的参数，存进账本就有了第二个事实源，
    改配额时「库里那个 10 万」会静默压过配置。
    """
    base = ledger if isinstance(ledger, dict) else {}
    agents = {**_agents(base)}
    if agent not in agents:
        agents[agent] = {"positions": {}, "virtual_cash": quota}
    return {**base, "agents": agents}


def record_buy(
    ledger: Any,
    agent: str,
    code: str,
    volume: Any,
    cost_price: Any,
    ts: datetime,
    *,
    quota: float = DEFAULT_AGENT_QUOTA,
) -> LedgerChange:
    """买入记账：新建或按**加权平均**更新成本，同时扣减虚拟现金。

    加仓不动 ``buy_ts``（那是**首次**建仓时间，持仓天数与回合台账按它算），
    只推进 ``last_ts``。
    """
    reason = _reject(code, volume, cost_price)
    if reason:
        return LedgerChange(ledger=ledger, note=reason)

    vol = float(volume)
    price = float(cost_price)
    base = ensure_agent(ledger, agent, quota=quota)
    pos = dict(agent_positions(base, agent))
    stamp = to_utc_iso(ts)
    held = pos.get(code)
    if isinstance(held, dict):
        old_v = _finite(held.get("volume")) or 0.0
        old_c = _finite(held.get("cost_price")) or 0.0
        total = old_v + vol
        avg = (old_v * old_c + vol * price) / total if total > 0 else price
        pos[code] = {
            **held,
            "volume": _num(total),
            "cost_price": round(avg, 4),
            "last_ts": stamp,
        }
    else:
        pos[code] = {
            "volume": _num(vol),
            "cost_price": round(price, 4),
            "buy_ts": stamp,
            "last_ts": stamp,
        }
    return LedgerChange(
        ledger=_write(base, agent, pos, -vol * price, quota), applied=vol
    )


def record_sell(
    ledger: Any,
    agent: str,
    code: str,
    volume: Any,
    sell_price: Any,
    ts: datetime,
    *,
    quota: float = DEFAULT_AGENT_QUOTA,
    market: str = "CN",
    exit_reason: str | None = None,
) -> LedgerChange:
    """卖出记账：扣减数量（减到 0 移除）、按**实际记入量**加回虚拟现金，并产出一条回合记录。

    ``market`` / ``exit_reason`` 只是**记录**（回合台账要按市场选行情源、按理由做行为
    归因），不参与任何判定。默认 ``CN`` 是因为本账本就是 A 股那本（隔壁 A/HK/US
    各一个文件）；港股/美股接进来时必须显式传自己的市场，别让它默认成 A 股。
    """
    reason = _reject(code, volume, sell_price)
    if reason:
        return LedgerChange(ledger=ledger, note=reason)

    want = float(volume)
    price = float(sell_price)
    pos = dict(agent_positions(ledger, agent))
    held = pos.get(code)
    if not isinstance(held, dict):
        # 见模块 docstring 差异②：静默返回一本看不出问题的账，是台账漂移最好的藏身处。
        return LedgerChange(
            ledger=ledger,
            note=(
                f"{agent} 无持仓 {code}：卖出 {_num(want)} 股未记账"
                f"（这本账里没有它，柜台可能有——跨 agent 卖仓或台账已漂移）"
            ),
        )

    have = _finite(held.get("volume")) or 0.0
    sold = min(want, have)
    gap = want - sold
    remaining = have - sold

    if remaining <= 0:
        pos.pop(code, None)
    else:
        pos[code] = {
            **held,
            "volume": _num(remaining),
            "last_ts": to_utc_iso(ts),
        }
    base = _write(ledger, agent, pos, sold * price, quota)
    base = {
        **base,
        "roundtrips": [
            *(
                base.get("roundtrips")
                if isinstance(base.get("roundtrips"), list)
                else []
            ),
            _roundtrip_row(
                held=held,
                agent=agent,
                code=code,
                sold=sold,
                sell_price=price,
                ts=ts,
                exit_reason=exit_reason,
                closed=remaining <= 0,
                market=market,
            ),
        ],
    }
    note = ""
    if gap > 1e-9:
        # 见模块 docstring 差异①：差额要说出来，且**只按实际持有量记现金**。
        note = (
            f"卖出量 {_num(want)} 超出 {agent} 持有 {_num(have)} 股："
            f"只记 {_num(sold)} 股（缺口 {_num(gap)} 股，柜台侧可能多出这一笔）"
        )
    return LedgerChange(ledger=base, applied=sold, note=note)


# ── 成交补记的幂等算术 ───────────────────────────────────────────────


def recorded_baseline(
    pending_recorded: Any = 0,
    applied_filled: Any = 0,
    *,
    marker_ts: str | None = None,
    today: str | None = None,
) -> int:
    """该委托「已记账成交量」的基准 = ``max(pending 侧, 账本幂等标记侧)``。

    为什么必须取**两者的大值**：两次落盘（pending 与账本）不是一次原子写，
    中间被 kill 就有一侧落后；只看一侧会把同一笔成交再记一次（现金多记、持仓多扣，
    账本自身看不出异常）。隔壁 2026-09-11 审查 MEDIUM 的同款结论。

    ``marker_ts``/``today`` **同时给出**才做当日过滤：委托号**每日重排**的场景下
    （A 股柜台），昨天的同号标记会把今天的新单吞掉——真成交永远不记账。不给出日期
    就不做过滤（标记按可信处理）——那正是防止重复记账的方向，两条风险不对称。
    """
    booked = 0
    if not (marker_ts is not None and today is not None and marker_ts != today):
        booked = _int(applied_filled)
    return max(_int(pending_recorded), booked)


def fill_delta(filled: Any, recorded: Any) -> float:
    """本次该补记的数量 = ``桥累计成交 − 已记账``；**不为负**（桥回退/重发不算退货）。"""
    return max(0.0, float(_int(filled) - _int(recorded)))


def holding_days(buy_ts: Any, sell_ts: Any) -> float | None:
    """持仓天数（卖出 − 买入）；任一侧不可解析 → ``None``（不臆造）。"""
    b = _parse_ts(buy_ts)
    s = _parse_ts(sell_ts)
    if b is None or s is None:
        return None
    return round((s - b).total_seconds() / 86400, 3)


# ── 期初结转（P3：隔壁 live_ledger.json → 本仓账本）────────────────────
#
# 切换日之后本仓的账本从零开始，而三个 agent 名下的仓**已经在真实账户里**——
# 不把这一段搬过来，模型看不到自己的持仓（``mine_of`` 全空 ⇒ 提示词被裁空 ⇒
# 该止盈止损的仓永远不卖），而账面全绿。这正是 2026-09-08 事故的同族形态：
# 「账本看不见真实持仓」，只是方向反过来。
#
# 只搬 ``agents`` 段（持仓 + 子账户现金）。``applied_fills`` **不搬**：那是隔壁
# 用**委托号**做的当日幂等标记（"25446": {"filled": 200, "ts": "2026-09-23"}），
# 而本仓的幂等是 ``(租户, 用户, 成交日, fill_key)`` 唯一索引、键是券商成交号——
# 委托号在这里既不是键也认不出归属，搬过去只是一串没人读的历史。故只记条数。


#: 期初结转流水的 ``fill_key`` 前缀。带它的行**不是本仓成交**，是「迁入时就有的
#: 仓」：对账侧（体检 C14）按同一常量把这类行单独计数，不当「无对应成交」报。
SEED_FILL_PREFIX = "legacy-seed:"


def seed_fill_key(agent: Any, code: Any) -> str:
    """期初结转的流水幂等键：``legacy-seed:{agent}:{后缀码}``。

    ``agent`` 必须在键里：流水表按 ``(租户, 用户, 成交日, fill_key)`` 唯一，而
    **两个 agent 可以同持一只票、同日买入**（实测 600276.SH 就是 flash 与 pro 各
    100 股）——键里不带 agent，第二家的结转流水会被幂等守卫静默跳过，它的持仓在
    流水表里就查无此行（「状态要能从流水推回来」当场断掉）。

    长度：前缀 12 + agent(≤64) + 1 + 码(≤32) < ``FILL_KEY_LEN``=128，不会截断撞键。
    """
    who = str(agent or "").strip()
    suffix = StockCodeUtil.to_suffix(str(code or "").strip())
    return f"{SEED_FILL_PREFIX}{who}:{suffix}"


@dataclass(frozen=True, slots=True)
class SeedPosition:
    """一条迁入的持仓（``code`` 已归一到后缀式）。"""

    code: str
    volume: float
    cost_price: float
    buy_ts: datetime | None = None
    last_ts: datetime | None = None

    @property
    def cost(self) -> float:
        """这条仓的成本（``volume × cost_price``）。"""
        return round(self.volume * self.cost_price, 2)


@dataclass(frozen=True, slots=True)
class SeedAgent:
    """一个 agent 的期初结转段。"""

    agent: str
    virtual_cash: float
    positions: tuple[SeedPosition, ...] = ()

    @property
    def used(self) -> float:
        """已用额度 = 现持仓成本合计（与 :func:`agent_used` 同口径，供报告比对）。"""
        return round(sum(p.cost for p in self.positions), 2)


@dataclass(frozen=True, slots=True)
class LegacySeed:
    """解析结果。``problems`` 非空 = **不许落库**（调用点必须拒绝，不许按猜的搬）。"""

    version: int
    agents: tuple[SeedAgent, ...]
    problems: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    applied_fills: int = 0

    @property
    def ok(self) -> bool:
        return not self.problems

    def agent(self, name: str) -> SeedAgent | None:
        for a in self.agents:
            if a.agent == name:
                return a
        return None


def parse_legacy_ledger(raw: Any) -> LegacySeed:
    """隔壁 ``logs/live_ledger.json`` → 期初结转计划（**纯解析，不碰库**）。

    三种输出分得很清，落库侧只认第一种：

    * ``problems``：**阻断**（版本不认识、现金/数量/成本非有限或非正、同一 agent
      内归一后重码、类型不对）——按猜测搬会把脏值写进一本新账，之后再也分不清
      「迁入时就这样」还是「本仓记错了」；
    * ``notes``：**降级但不阻断**（时间戳缺失/不可解析 ⇒ 持仓天数将为 ``None``、
      现金为负 ⇒ 透支如实搬、数量非整手、``used`` 字段与现算不符、未知字段）；
    * 其余照搬。

    ``agents`` 按名字排序（报告可复现，两次跑同一份文件输出逐字相同）。
    """
    problems: list[str] = []
    notes: list[str] = []
    if not isinstance(raw, Mapping):
        return LegacySeed(1, (), (f"顶层不是 JSON 对象（{type(raw).__name__}）",), ())

    version = raw.get("version")
    if version != 1:
        problems.append(f"版本 {version!r} 不是 1：不认识的账本格式（拒绝按猜测解析）")

    agents_raw = raw.get("agents")
    if not isinstance(agents_raw, Mapping):
        problems.append(f"agents 段不是对象（{type(agents_raw).__name__}）")
        agents_raw = {}

    agents: list[SeedAgent] = []
    for name, rec in sorted(agents_raw.items(), key=lambda kv: str(kv[0])):
        agent_name = str(name or "").strip()
        if not agent_name:
            problems.append("有一个 agent 名为空：无法定位归属")
            continue
        if not isinstance(rec, Mapping):
            problems.append(f"{agent_name}: 段不是对象（{type(rec).__name__}）")
            continue
        for key in rec:
            if key not in ("positions", "virtual_cash", "used"):
                notes.append(f"{agent_name}: 忽略未知字段 {key!r}")

        cash = _finite(rec.get("virtual_cash"))
        if cash is None:
            problems.append(
                f"{agent_name}: virtual_cash {rec.get('virtual_cash')!r} 非有限"
            )
            cash = 0.0
        elif cash < 0:
            notes.append(f"{agent_name}: 虚拟现金为负（{cash:.2f}）——透支如实搬")

        positions, pos_problems, pos_notes = _parse_seed_positions(
            agent_name, rec.get("positions")
        )
        problems.extend(pos_problems)
        notes.extend(pos_notes)

        used_raw = _finite(rec.get("used"))
        seed_agent = SeedAgent(agent_name, round(cash, 4), tuple(positions))
        if used_raw is not None and abs(used_raw - seed_agent.used) > 0.01:
            # 实测（2026-09-23 dump）：文件里的 used 是**旧版遗留**，与按持仓现算
            # 差一个数量级（356421.2 vs 19174.0）。读侧（隔壁 agent_used）本来就是
            # 现算的，故以现算为准，只把不一致记成 note。
            notes.append(
                f"{agent_name}: 文件 used={used_raw:.2f} 与按持仓现算 "
                f"{seed_agent.used:.2f} 不符（该字段已弃用，以现算为准）"
            )
        agents.append(seed_agent)

    applied = raw.get("applied_fills")
    if isinstance(applied, Mapping):
        applied_count = len(applied)
    else:
        applied_count = 0
        if applied is not None:
            notes.append(
                f"applied_fills 段不是对象（{type(applied).__name__}）：按 0 条计"
            )
    for key in raw:
        if key not in ("version", "agents", "applied_fills"):
            notes.append(f"忽略未知顶层字段 {key!r}")

    return LegacySeed(
        version=1 if version == 1 else 0,
        agents=tuple(agents),
        problems=tuple(problems),
        notes=tuple(notes),
        applied_fills=applied_count,
    )


def _parse_seed_positions(
    agent_name: str, raw: Any
) -> tuple[list[SeedPosition], list[str], list[str]]:
    """持仓段解析：``(持仓, problems, notes)``（缺段按空仓，不阻断）。"""
    problems: list[str] = []
    notes: list[str] = []
    out: list[SeedPosition] = []
    if raw is None:
        notes.append(f"{agent_name}: 无 positions 段（按空仓处理）")
        return out, problems, notes
    if not isinstance(raw, Mapping):
        problems.append(f"{agent_name}: positions 段不是对象（{type(raw).__name__}）")
        return out, problems, notes

    seen: set[str] = set()
    for code_raw, pos in sorted(raw.items(), key=lambda kv: str(kv[0])):
        if not isinstance(pos, Mapping):
            problems.append(
                f"{agent_name}/{code_raw}: 持仓不是对象（{type(pos).__name__}）"
            )
            continue
        code = StockCodeUtil.to_suffix(str(code_raw or "").strip())
        if not code:
            problems.append(f"{agent_name}/{code_raw}: 标的代码归一后为空")
            continue
        if code != str(code_raw):
            notes.append(f"{agent_name}: 代码 {code_raw} 归一为 {code}")
        if code in seen:
            problems.append(
                f"{agent_name}: 归一后重码 {code}（同一只票两行：无法合并）"
            )
            continue
        seen.add(code)

        volume = _finite(pos.get("volume"))
        price = _finite(pos.get("cost_price"))
        if volume is None or volume <= 0:
            problems.append(
                f"{agent_name}/{code}: 数量 {pos.get('volume')!r} 非正或非有限"
            )
            continue
        if price is None or price <= 0:
            problems.append(
                f"{agent_name}/{code}: 成本价 {pos.get('cost_price')!r} 非正或非有限"
            )
            continue
        if not float(volume).is_integer():
            notes.append(f"{agent_name}/{code}: 数量 {volume} 非整股")
        elif int(volume) % 100:
            notes.append(
                f"{agent_name}/{code}: 数量 {int(volume)} 非整手（送股/零股属正常）"
            )
        for key in pos:
            if key not in ("volume", "cost_price", "buy_ts", "last_ts"):
                notes.append(f"{agent_name}/{code}: 忽略未知字段 {key!r}")

        buy_ts = _parse_ts(pos.get("buy_ts"))
        last_ts = _parse_ts(pos.get("last_ts"))
        if pos.get("buy_ts") is not None and buy_ts is None:
            notes.append(
                f"{agent_name}/{code}: buy_ts {pos.get('buy_ts')!r} 不可解析（持仓天数将为 None）"
            )
        if buy_ts is None:
            notes.append(
                f"{agent_name}/{code}: 无 buy_ts（持仓天数与结转流水日期将用结转日）"
            )
        out.append(
            SeedPosition(code, float(volume), round(float(price), 4), buy_ts, last_ts)
        )
    return out, problems, notes


# ── 期初结转的**桥锚定对账**（P0.4：仓位唯一事实源是桥）────────────────
#
# 为什么结转必须过这一层：隔壁台账的仓位是**派生数据**——「卖出没归因回去」时它
# 只增不减（实测：台账 2400 股 vs 桥 1900 股，差 `002518.SZ` 400 + `603678.SH` 100
# 两只幻影；桥侧那两只早已是**量为 0 的清仓残留行**）。照搬台账 = 把幻影迁进新账本
# ⇒ 提示词告诉模型「你还有 400 股」而柜台没有 ⇒ 挂单被拒/部分成交，且**新账本自身
# 看不出这是怎么来的**。故：**仓位唯一事实源是桥**（``real_positions.load_real_positions``
# → PG ``real_account_snapshots``），台账只提供**归属**（这一只是谁买的）。
#
# 逐只标的的四种判定（全部落进 :class:`ReconcileRecord`，不静默丢弃）：
#
# | 判定 | 情形 | 处置 |
# |---|---|---|
# | ``match`` | 认领合计 == 桥 | 原样迁入（含多 agent 各持一份：600276.SH 实测两家各 100） |
# | ``anchored`` | **单一认领人**、量不符 | 量按桥锚定（台账只说「这只归谁」，不说「还有多少股」） |
# | ``phantom`` | 桥为 0/无此仓、台账有量 | 剔除，记录「原台账量 / 桥实况 / 差额 / 判定」 |
# | ``orphan`` | 桥有量、无任何 agent 认领 | 不入分账（属总账户既有仓），显式记录 |
#
# 多认领人且合计≠桥 ⇒ **阻断**（归属不可判定，任何摊派都是编造——尤其按比例摊派会
# 让其中一家**静默少仓**，那正是本层要消灭的那类事故）。人工判定走 ``overrides``，
# 理由必填（不加理由的判定不可审计）。
#
# 成本价**不锚定**：桥的成本是账户级摊薄成本（实测 002074.SZ 桥 24.87 vs 台账 25.44
# ——含费/含该股历史回合），而台账那个数才是**该 agent 的真实买入成本**，它是盈亏列与
# 回合台账的输入。量以桥为准、成本以台账为准，两者各管各的。


#: 对账判定（进 :class:`ReconcileRecord`；``match`` 之外都是「要人看一眼」的）。
#: ``drift`` = 多认领人合计不齐（**阻断**，见 :func:`reconcile_seed_with_bridge`）。
BRIDGE_MATCH = "match"
BRIDGE_ANCHORED = "anchored"
BRIDGE_PHANTOM = "phantom"
BRIDGE_ORPHAN = "orphan"
BRIDGE_OVERRIDE = "override"
BRIDGE_DRIFT = "drift"

#: 股数比较容差（送股/拆股会带来小数股；1e-6 之下算同一批货）。
_QTY_EPS = 1e-6


@dataclass(frozen=True, slots=True)
class BridgePosition:
    """桥侧一只票（``code`` 后缀式）。``volume == 0`` = 快照仍有该行但已清仓。"""

    code: str
    volume: float
    cost_price: float | None = None
    source: str = ""

    @property
    def held(self) -> bool:
        return self.volume > _QTY_EPS


def bridge_rows_from_view(view: Mapping[str, Any]) -> list[dict[str, Any]]:
    """``real_positions.merge_real_sources`` 的视图（prefix 键）→ 捕获行（后缀码）。

    只取对账用得到的四个字段：代码、数量、成本、出处。视图里的键是 **prefix 式**
    （``SH600036``），而账本列与 :class:`BridgePosition` 一律后缀式——这里若忘了归一，
    ``mine_of`` 那类匹配会静默全空，故出口只留一种写法（``symbol`` 缺了也要归一次键）。
    """
    rows: list[dict[str, Any]] = []
    for key, item in (view or {}).items():
        rec = item if isinstance(item, Mapping) else {}
        rows.append(
            {
                "code": StockCodeUtil.to_suffix(
                    str(rec.get("symbol") or key or "").strip()
                ),
                "volume": rec.get("volume"),
                "cost_price": rec.get("cost_price"),
                "source": rec.get("source") or "",
            }
        )
    return sorted(rows, key=lambda r: str(r.get("code") or ""))


def bridge_positions(
    items: Any,
) -> tuple[dict[str, BridgePosition], list[str]]:
    """捕获行（list[dict]）→ ``{后缀码: BridgePosition}`` + 阻断理由。

    阻断（``手写快照文件`` 出错时宁可拒，不许按猜的对账）：同一只票两行（归属与数量
    都无从判起）、数量非有限或为负、缺代码。量为 0 的行**保留**（它是「快照仍留着这只
    的残留行」这一事实的载体，判定侧要靠它区分「桥无此仓」与「桥有行但已清仓」）。
    """
    if items is None:
        return {}, []
    if isinstance(items, Mapping):  # 容忍 {code: {...}} / {code: volume} 的手写形态
        items = [
            (dict(v, code=k) if isinstance(v, Mapping) else {"code": k, "volume": v})
            for k, v in items.items()
        ]
    if not isinstance(items, (list, tuple)):
        return {}, [f"桥快照不是列表（{type(items).__name__}）：无法解析"]

    out: dict[str, BridgePosition] = {}
    problems: list[str] = []
    for row in items:
        if not isinstance(row, Mapping):
            problems.append(f"桥快照有一行不是对象（{type(row).__name__}）")
            continue
        code = StockCodeUtil.to_suffix(
            str(row.get("code") or row.get("symbol") or "").strip()
        )
        if not code:
            problems.append(f"桥快照有一行代码为空：{dict(row)!r}")
            continue
        if code in out:
            problems.append(f"桥快照里 {code} 出现两行：数量/归属无从判起（拒绝对账）")
            continue
        vol = _finite(row.get("volume"))
        if vol is None or vol < 0:
            problems.append(f"桥快照 {code}: 数量 {row.get('volume')!r} 非有限或为负")
            continue
        cost = _finite(row.get("cost_price"))
        out[code] = BridgePosition(
            code,
            _num(vol),
            None if cost is None else round(cost, 4),
            str(row.get("source") or ""),
        )
    return out, problems


@dataclass(frozen=True, slots=True)
class ReconcileRecord:
    """一只标的的对账记录（P0.4 规则 2 要的那四项：原台账量 / 桥实况 / 差额 / 判定）。"""

    code: str
    kind: str
    ledger_volume: float
    bridge_volume: float
    carried_volume: float = 0.0
    claims: tuple[tuple[str, float], ...] = ()
    bridge_present: bool = True
    verdict: str = ""
    reason: str = ""

    @property
    def delta(self) -> float:
        """桥 − 台账（正 = 桥比台账多，负 = 台账虚增/幻影）。"""
        return _num(round(self.bridge_volume - self.ledger_volume, 4))


@dataclass(frozen=True, slots=True)
class SeedReconciliation:
    """对账结果：``seed`` 是**可落库**的计划（幻影已剔、量已按桥锚定）。

    ``ok`` 同时要求「无 problems」与「双向对账断言通过」（P0.4 规则 4）——断言不是
    事后检查项，而是 ``ok`` 的一半：一个不平衡的计划在类型上就不算合格产物。

    ``bridge_held`` / ``carried`` 是断言的两个操作数（``(码, 量)`` 对，排序可复现），
    ``orphan_total`` 是「桥有量但无人认领」的合计——断言把它从桥侧扣掉，因为孤仓
    **按定义**不在任何 agent 名下（与 2026-08-31 之前那批 ¥92 万既存仓同性质）。
    """

    seed: LegacySeed
    records: tuple[ReconcileRecord, ...] = ()
    problems: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    bridge_held: tuple[tuple[str, float], ...] = ()
    carried: tuple[tuple[str, float], ...] = ()
    orphan_total: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.problems and not self.assert_balances()

    @property
    def bridge_total(self) -> float:
        return _num(round(sum(v for _c, v in self.bridge_held), 4))

    @property
    def carried_total(self) -> float:
        return _num(round(sum(v for _c, v in self.carried), 4))

    @property
    def changed(self) -> tuple[ReconcileRecord, ...]:
        """与台账不一致的判定（幻影/锚定/孤仓/人工判定）——都为「要人看一眼」。"""
        return tuple(r for r in self.records if r.kind != BRIDGE_MATCH)

    @property
    def phantom_codes(self) -> tuple[str, ...]:
        return tuple(r.code for r in self.records if r.kind == BRIDGE_PHANTOM)

    @property
    def orphan_codes(self) -> tuple[str, ...]:
        return tuple(r.code for r in self.records if r.kind == BRIDGE_ORPHAN)

    def record(self, code: str) -> ReconcileRecord | None:
        want = StockCodeUtil.to_suffix(str(code or "").strip())
        for r in self.records:
            if r.code == want:
                return r
        return None

    def note_for(self, code: str) -> str:
        """该标的的对账说明（进结转流水行的 ``note``）；逐字相符的返回空串。"""
        r = self.record(code)
        if r is None or r.kind == BRIDGE_MATCH:
            return ""
        return f"对账：{r.verdict}"

    def assert_balances(self) -> str:
        """P0.4 规则 4 的双向对账断言；通过返回 ``""``，否则返回**人话理由**。

        三条：① 迁入的每只在桥里都有、且量逐只相等；② 迁入合计 == 桥合计 − 孤仓合计；
        ③ （由 ① 的「在桥里都有」隐含）迁入不含桥侧没有的标的——幻影正是靠这条被拦。
        """
        held = dict(self.bridge_held)
        carried = dict(self.carried)
        for code, vol in carried.items():
            if code not in held:
                return f"{code}: 计划迁入 {vol:g} 股，但桥侧没有这只（幻影不得迁入）"
            if abs(float(held[code]) - float(vol)) > _QTY_EPS:
                return f"{code}: 计划迁入 {vol:g} 股 ≠ 桥 {held[code]:g} 股"
        want = round(sum(float(v) for v in held.values()) - float(self.orphan_total), 4)
        got = round(sum(float(v) for v in carried.values()), 4)
        if abs(want - got) > _QTY_EPS:
            return (
                f"迁入合计 {_num(got)} 股 ≠ 桥 {_num(self.bridge_total)} 股 − 孤仓 "
                f"{_num(self.orphan_total)} 股 = {_num(want)} 股"
            )
        return ""


def reconcile_seed_with_bridge(
    seed: LegacySeed,
    bridge: Mapping[str, Any],
    *,
    overrides: Mapping[str, Mapping[str, Any]] | None = None,
    reasons: Mapping[str, str] | None = None,
) -> SeedReconciliation:
    """把解析出的台账**按桥重建**成可落库的结转计划（纯函数，两次跑逐字相同）。

    :param seed: :func:`parse_legacy_ledger` 的结果（``problems`` 非空则不进入对账）。
    :param bridge: ``{后缀码: BridgePosition}``（:func:`bridge_positions` 的产物；
        也容忍 ``{码: {"volume": …}}`` 或 ``{码: 数量}`` 的手写形态）。
    :param overrides: 人工判定：``{码: {agent: 数量}}``——**整只替换**该码的认领表
        （要减一家就把它从表里去掉，不许写 0 蒙混）；只有多认领人合计不齐（阻断）
        时才需要它。
    :param reasons: 人工判定的**理由**（与 ``overrides`` 同键）：空理由即阻断——
        明天没人能解释「为什么这只票归它」。

    返回 :class:`SeedReconciliation`；``ok`` 为假时调用点必须拒绝落库（store 侧另有一道）。
    """
    problems: list[str] = list(seed.problems)
    notes: list[str] = list(seed.notes)
    if not seed.ok:
        # 解析都没过，谈不上对账：原样把阻断项往上抛（含「不许按猜的搬」）。
        return SeedReconciliation(
            seed=seed, problems=tuple(problems), notes=tuple(notes)
        )

    bmap: dict[str, BridgePosition] = {}
    for code, val in (bridge or {}).items():
        bp = _as_bridge_pos(code, val)
        if bp is None:
            problems.append(f"桥表 {code!r} 形态不认识：{val!r}（拒绝对账）")
            continue
        bmap[bp.code] = bp

    ov: dict[str, dict[str, float]] = {}
    for code_raw, claims in (overrides or {}).items():
        code = StockCodeUtil.to_suffix(str(code_raw or "").strip())
        if not code:
            problems.append(f"人工判定缺代码：{code_raw!r}")
            continue
        if not isinstance(claims, Mapping):
            problems.append(
                f"人工判定 {code}: 认领表不是对象（{type(claims).__name__}）"
            )
            continue
        ov[code] = dict(claims)
    rs = {
        StockCodeUtil.to_suffix(str(k or "").strip()): str(v or "").strip()
        for k, v in (reasons or {}).items()
    }

    names = {a.agent for a in seed.agents}
    bad_overrides: set[str] = set()
    for code, claims in sorted(ov.items()):
        if not rs.get(code):
            problems.append(
                f"人工判定 {code}: 缺 reason——不加理由的判定不可审计（拒绝结转）"
            )
            bad_overrides.add(code)
        if not claims:
            problems.append(f"人工判定 {code}: 认领表为空（要剔除请别写这条判定）")
            bad_overrides.add(code)
        for agent, vol in sorted(claims.items()):
            if agent not in names:
                problems.append(
                    f"人工判定 {code}: agent {agent!r} 不在台账里——不许凭空开户"
                )
            v = _finite(vol)
            if v is None or v <= 0:
                problems.append(f"人工判定 {code}/{agent}: 数量 {vol!r} 非正或非有限")

    ledger_claims: dict[str, dict[str, float]] = {}
    for a in seed.agents:
        for p in a.positions:
            ledger_claims.setdefault(p.code, {})[a.agent] = float(p.volume)

    carried: dict[str, dict[str, float]] = {}
    records: list[ReconcileRecord] = []
    for code in sorted(set(ledger_claims) | set(bmap) | set(ov)):
        valid_override = code in ov and code not in bad_overrides
        orig = dict(ledger_claims.get(code) or {})
        claims = dict(ov[code]) if valid_override else dict(orig)
        bp = bmap.get(code)
        b_held = bp is not None and bp.held
        b_vol = float(bp.volume) if bp is not None else 0.0
        l_vol = round(sum(orig.values()), 4)
        claims_sorted = tuple(sorted((a, float(v)) for a, v in claims.items()))

        if not claims:
            if bp is None:
                # 只可能是「被判无效的残条人工判定」（否则它必在台账或桥里）。
                continue
            if not b_held:
                # 桥侧残留的零量行（实测 4 条）且无人认领：无信息量，记 note 即可。
                notes.append(
                    f"桥快照有 {code} 但量为 0 且无 agent 认领（清仓残留行）：忽略"
                )
                continue
            records.append(
                ReconcileRecord(
                    code,
                    BRIDGE_ORPHAN,
                    0.0,
                    b_vol,
                    0.0,
                    (),
                    verdict=f"桥有 {_num(bp.volume)} 股、无任何 agent 认领：不入分账"
                    f"（属总账户既有仓，与 2026-08-31 之前那批同性质）",
                )
            )
            continue
        if not b_held:
            records.append(
                ReconcileRecord(
                    code,
                    BRIDGE_PHANTOM,
                    l_vol,
                    0.0,
                    0.0,
                    claims_sorted,
                    bridge_present=bp is not None,
                    verdict=(
                        f"台账 {_num(l_vol)} 股，桥"
                        + (
                            "该行为 0 股（券商已清仓、台账未回写）"
                            if bp is not None
                            else "无此仓"
                        )
                        + "——幻影，不迁入"
                    ),
                )
            )
            continue

        total = round(sum(claims.values()), 4)
        if valid_override:
            kind = BRIDGE_OVERRIDE
            verdict = (
                f"人工判定：原台账 {_num(l_vol)} 股 → {_num(total)} 股"
                f"（理由：{rs.get(code, '')}）"
            )
            reason = rs.get(code, "")
        elif abs(total - float(bp.volume)) <= _QTY_EPS:
            kind = BRIDGE_MATCH
            verdict = f"台账 {_num(l_vol)} 股 == 桥 {_num(bp.volume)} 股：逐只相符"
            reason = ""
        elif len(claims) == 1:
            agent = next(iter(claims))
            kind = BRIDGE_ANCHORED
            reason = ""
            verdict = (
                f"单一认领人 {agent}：台账 {_num(l_vol)} 股 → 桥 {_num(bp.volume)} 股"
                f"（仓位以桥为准，台账只提供归属）"
            )
            claims = {agent: float(bp.volume)}
            total = round(sum(claims.values()), 4)
        else:
            # 多认领人、合计不齐：归属不可判定。阻断（本码不迁入，整批不落库）。
            records.append(
                ReconcileRecord(
                    code,
                    BRIDGE_DRIFT,
                    l_vol,
                    float(bp.volume),
                    0.0,
                    claims_sorted,
                    verdict=f"台账合计 {_num(l_vol)} 股 vs 桥 {_num(bp.volume)} 股："
                    f"多个 agent 认领，归属不可判定——不迁入，待人工判定",
                )
            )
            problems.append(
                f"{code}: 台账合计 {_num(l_vol)} 股 vs 桥 {_num(bp.volume)} 股"
                f"（差 {_num(round(float(bp.volume) - l_vol, 4)):+g}）——"
                f"{len(claims)} 个 agent 都认领，无法判定归属；"
                f"请用 --resolve 给出人工判定（含理由）后重跑"
            )
            continue

        carried[code] = claims
        records.append(
            ReconcileRecord(
                code,
                kind,
                l_vol,
                float(bp.volume),
                _num(round(total, 4)),
                claims_sorted,
                verdict=verdict,
                reason=reason,
            )
        )

    agents_out: list[SeedAgent] = []
    for a in seed.agents:
        keep: list[SeedPosition] = []
        for p in a.positions:
            mine = carried.get(p.code) or {}
            if a.agent not in mine:
                continue
            keep.append(replace(p, volume=_num(float(mine[a.agent]))))
        dropped = len(a.positions) - len(keep)
        if dropped:
            notes.append(
                f"{a.agent}: {dropped} 只未迁入（见对账记录）"
                + ("——结转后名下无持仓（现金照迁）" if not keep else "")
            )
        agents_out.append(SeedAgent(a.agent, a.virtual_cash, tuple(keep)))

    plan = LegacySeed(
        version=seed.version,
        agents=tuple(agents_out),
        problems=(),
        notes=(),
        applied_fills=seed.applied_fills,
    )
    rec = SeedReconciliation(
        seed=plan,
        records=tuple(records),
        problems=tuple(problems),
        notes=tuple(notes),
        bridge_held=tuple(sorted((c, float(b.volume)) for c, b in bmap.items())),
        carried=tuple(
            sorted((c, round(sum(v.values()), 4)) for c, v in carried.items())
        ),
        orphan_total=_num(
            round(sum(r.bridge_volume for r in records if r.kind == BRIDGE_ORPHAN), 4)
        ),
    )
    reason = rec.assert_balances()
    if reason:
        # P0.4 规则 4：断言失败 = 不许落库。把它写成问题（不是断言异常——CLI 要能
        # 把它打印给人看，而不是甩一个 traceback）。
        rec = replace(rec, problems=(*rec.problems, f"对账断言未通过：{reason}"))
    return rec


def _as_bridge_pos(code: Any, val: Any) -> BridgePosition | None:
    """桥表的一项 → :class:`BridgePosition`（容忍手写形态；认不出返回 ``None``）。"""
    if isinstance(val, BridgePosition):
        return val
    if isinstance(val, Mapping):
        v = _finite(val.get("volume"))
        cost = _finite(val.get("cost_price"))
        code_s = StockCodeUtil.to_suffix(
            str(val.get("code") or val.get("symbol") or code or "").strip()
        )
        if not code_s or v is None or v < 0:
            return None
        return BridgePosition(
            code_s,
            _num(v),
            None if cost is None else round(cost, 4),
            str(val.get("source") or ""),
        )
    v = _finite(val)
    code_s = StockCodeUtil.to_suffix(str(code or "").strip())
    if not code_s or v is None or v < 0:
        return None
    return BridgePosition(code_s, _num(v))


# ── 内部 ────────────────────────────────────────────────────────────


def _roundtrip_row(
    *,
    held: dict[str, Any],
    agent: str,
    code: str,
    sold: float,
    sell_price: float,
    ts: datetime,
    exit_reason: str | None,
    closed: bool,
    market: str,
) -> dict[str, Any]:
    """平仓/减仓一条回合记录（影子账户与行为归因的数据底座）。

    价格类特征（RSI、前 5 日收益等）**不在这里算**——那要按点位时间离线回溯，
    见隔壁 ``scripts/roundtrip_features.py`` 的分工。
    """
    cost = _finite(held.get("cost_price")) or 0.0
    buy_ts = held.get("buy_ts")
    return {
        "market": market,
        "agent": agent,
        "code": code,
        "volume": _num(sold),
        "cost_price": cost,
        "sell_price": sell_price,
        "buy_ts": buy_ts,
        "sell_ts": to_utc_iso(ts),
        "holding_days": holding_days(buy_ts, ts),
        "realized_pnl": round((sell_price - cost) * sold, 2),
        "pnl_pct": (round((sell_price - cost) / cost * 100, 3) if cost > 0 else None),
        "closed": closed,
        "exit_reason": exit_reason,
    }


def _write(
    ledger: Any, agent: str, positions: dict[str, Any], cash_delta: float, quota: float
) -> dict:
    """把持仓段与现金增量落进新账本（不可变；现金按 ``_cash`` 的缺失语义取基数）。"""
    base = ensure_agent(ledger, agent, quota=quota)
    agents = {**_agents(base)}
    rec = dict(agents.get(agent) or {})
    rec["positions"] = positions
    rec["virtual_cash"] = round(agent_cash(base, agent, quota=quota) + cash_delta, 2)
    agents[agent] = rec
    extra = {k: base[k] for k in ("roundtrips", "deferred", "version") if k in base}
    return {**extra, "agents": agents}


def _reject(code: Any, volume: Any, price: Any) -> str:
    """入参整除（见模块 docstring 差异③）：返回拒绝理由，合法返回空串。"""
    if not str(code or "").strip():
        return "标的代码为空：记账未执行"
    v = _finite(volume)
    if v is None or v <= 0:
        return f"数量 {volume!r} 非正或非有限：记账未执行（量必须 > 0）"
    p = _finite(price)
    if p is None or p <= 0:
        return f"价格 {price!r} 非正或非有限：记账未执行（价必须 > 0）"
    return ""


def _finite(x: Any) -> float | None:
    """宽松转 float；``bool``／非数／非有限 → ``None``（按「没给」处理）。"""
    if x is None or isinstance(x, bool):
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _int(x: Any) -> int:
    f = _finite(x)
    return int(f) if f is not None else 0


def _num(x: float) -> float | int:
    """整数股数写成 int（与隔壁 JSON 的 ``"volume": 700`` 同形，便于对账时肉眼比）。"""
    return int(x) if float(x).is_integer() else round(x, 4)


def _parse_ts(value: Any) -> datetime | None:
    """ISO 字符串 → aware datetime；不可解析 → ``None``。

    先把尾部的 ``Z`` 换成 ``+00:00``：``datetime.fromisoformat`` 对 ``Z`` 的支持
    是 **3.11 才有的**，而本仓主栈跑在 3.10——不换的话本仓自己 ``to_utc_iso``
    写出来的时间戳全部解析失败（那会让持仓天数恒为 ``None``，且毫无报错）。
    """
    if isinstance(value, datetime):
        return as_utc(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        return as_utc(datetime.fromisoformat(text))
    except ValueError:
        return None
