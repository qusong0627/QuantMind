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
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from backend.shared.utc_datetime import as_utc, to_utc_iso

__all__ = [
    "DEFAULT_AGENT_QUOTA",
    "LedgerChange",
    "agent_cash",
    "agent_positions",
    "agent_remaining",
    "agent_used",
    "ensure_agent",
    "fill_delta",
    "holding_days",
    "mine_of",
    "record_buy",
    "record_sell",
    "recorded_baseline",
    "sane_fill_price",
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
