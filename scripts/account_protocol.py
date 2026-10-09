#!/usr/bin/env python3
"""账户协议：账户模型 + 纯操作（QIFI 式解耦）。

借鉴 HKUDS/Vibe-Trading 的 QIFI 协议——**把"策略/调用方"与"账户实现"解耦**：
调用方只认协议（字段与操作语义），市场差异收敛到实现层的**绑定**
（台账文件路径 + 额度 + 将来的市场特有规则）。

为什么需要（实测证据，见 docs/ACCOUNT_PROTOCOL_PLAN.md §0）：
  本项目原有三套市场台账（A股/港股/美股）各自复制一份实现，已实测出漂移——
  HK 与 US 互为副本，且都落后 A 股：**缺 `find_holder` / 延期单重放 / 回合台账**。
  其中"延期单重放"是 2026-09-01 事故复盘后建的防线，却只护住了 A 股。

本模块的约定（与 scripts/test_live_ledger.py 的"纯函数不碰文件"不变量一致）：
  * **无 IO、无全局可变状态**：所有操作接收账本、返回**新**账本；
  * 额度（quota）由调用方传入——这是三市场之间**唯一**的真实差异；
  * 市场特有的扩展（路径绑定、错误语义）留在各实现层。

账本结构：
    {"version": 1,
     "agents": {<agent>: {"positions": {<code>: {volume, cost_price, buy_ts, last_ts}},
                          "virtual_cash": <float>}},
     "deferred":   [ {agent, side, code, volume, reason, ts} ],
     "roundtrips": [ <回合记录> ]}     # 由 save 落盘后清空
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

#: 默认额度（仅作为缺省；实现层应显式传自己的口径）
DEFAULT_QUOTA = 100_000.0


# ---------- 账户模型访问 ----------
def ensure_agent(ledger: dict, agent: str, quota: float = DEFAULT_QUOTA) -> dict:
    """确保 agent 有账本条目（初始虚拟现金）。已存在则原样返回。"""
    agents = {**ledger.get("agents", {})}
    if agent not in agents:
        agents[agent] = {"quota": quota, "virtual_cash": quota, "positions": {}}
    return {**ledger, "agents": agents}


def positions(ledger: dict, agent: str) -> dict:
    """该 agent 的持仓字典（缺失返回空 dict）。"""
    if not isinstance(ledger, dict):     # 顶层类型坏（load_ledger 只保证可解析）
        return {}
    return ((ledger.get("agents") or {}).get(agent) or {}).get("positions") or {}


def _cash(rec: dict, quota: float) -> float:
    """虚拟现金取值：**键缺失**才回退 quota——0.0 是合法值。

    2026-09-18 评审 L-5：旧写法 `rec.get("virtual_cash") or quota` 把恰好归零的
    现金读成 10 万（键存在但值为 0 → 假现金）。虚拟现金是买入侧的硬约束输入，
    资金恰好用完的账户反而被放开一个 quota 的买入力。"""
    v = rec.get("virtual_cash")
    return float(v) if v is not None else quota


def agent_used(ledger: dict, agent: str) -> float:
    """该 agent 名下持仓成本合计（used 额度）。"""
    return round(
        sum(float(p["volume"]) * float(p["cost_price"])
            for p in positions(ledger, agent).values()),
        2,
    )


def position_cost(ledger: dict, agent: str, code: str) -> float:
    """该 agent 名下**某一只**的持仓成本（volume×cost_price）；无持仓/脏值 → 0.0。

    供单票集中度闸（buy_gate.position_cap_reason）取数；脏值计 0 = 放行，
    与调用方其他闸的容错方向一致（绝不因账本脏值炸掉下单流程）。
    """
    held = positions(ledger, agent).get(code)
    if not held:
        return 0.0
    try:
        return round(float(held["volume"]) * float(held["cost_price"]), 2)
    except (KeyError, TypeError, ValueError):
        return 0.0


def agent_remaining(ledger: dict, agent: str, quota: float = DEFAULT_QUOTA) -> float:
    """剩余可买额度 = quota − used。"""
    return round(quota - agent_used(ledger, agent), 2)


def agent_virtual_cash(ledger: dict, agent: str, quota: float = DEFAULT_QUOTA) -> float:
    """该 agent 虚拟现金（初始 quota；买入扣、卖出加）。键缺失回退 quota，0.0 原样。"""
    rec = (ledger.get("agents") or {}).get(agent) or {}
    return _cash(rec, quota)


def find_holder(ledger: dict, code: str) -> str | None:
    """持有该代码的 agent（轮候分配下每只只归属一个 agent）；无人持有返回 None。"""
    for agent in (ledger.get("agents") or {}):
        if code in positions(ledger, agent):
            return agent
    return None


# ---------- 回合台账（影子账户 / 行为归因的数据底座） ----------
def holding_days(buy_ts: str | None, sell_ts: str) -> float | None:
    """持仓天数（卖出 − 买入）。时间戳不可解析/时区不可比 → None，不臆造。"""
    try:
        b = datetime.fromisoformat(str(buy_ts or ""))
        s = datetime.fromisoformat(str(sell_ts or ""))
        return round((s - b).total_seconds() / 86400, 3)
    except (ValueError, TypeError):
        return None


def roundtrip_row(held: dict, agent: str, code: str, sold: int,
                  sell_price: float, ts: str, exit_reason: str | None,
                  closed: bool, market: str | None = None) -> dict:
    """平仓一条记录（含影子账户所用的日志类特征）。

    `market` 是三市场的绑定层事实（A股/港股/美股），写进记录里——
    下游回溯价格类特征要按市场选不同数据源，没有它就无法区分。

    ⚠️ 价格类特征（entry_rsi14 / prior_5d_return）不在这里记——那由
    scripts/roundtrip_features.py 离线回溯计算（买入当时记或事后算都要遵守
    点位时间纪律，见 docs/ROUNDTRIP_FEATURES_PLAN.md）。
    """
    cost = float(held.get("cost_price") or 0)
    sp = float(sell_price or 0)
    return {
        "market": market,
        "agent": agent,
        "code": code,
        "volume": int(sold),
        "cost_price": cost,
        "sell_price": sp,
        "buy_ts": held.get("buy_ts"),
        "sell_ts": ts,
        "holding_days": holding_days(held.get("buy_ts"), ts),
        "realized_pnl": round((sp - cost) * sold, 2),
        "pnl_pct": round((sp - cost) / cost * 100, 3) if cost > 0 else None,
        "closed": closed,            # False = 部分平仓，仓位仍在
        "exit_reason": exit_reason,
    }


# ---------- 记账操作（唯一真相源） ----------
def record_buy(ledger: dict, agent: str, code: str, volume: int,
               cost_price: float, ts: str, quota: float = DEFAULT_QUOTA) -> dict:
    """买入记账（不可变，返回新账本）：加仓时按加权平均更新成本；
    同时扣减该 agent 虚拟现金。"""
    pos = dict(positions(ledger, agent))
    cost = float(cost_price)          # 券商标量可能是字符串 → 统一强转（原仅 A 股实现有）
    if code in pos:
        old = pos[code]
        total = old["volume"] + volume
        avg = (old["volume"] * float(old["cost_price"]) + volume * cost) / total
        pos[code] = {**old, "volume": total, "cost_price": round(avg, 4), "last_ts": ts}
    else:
        pos[code] = {"volume": volume, "cost_price": round(cost, 4),
                     "buy_ts": ts, "last_ts": ts}
    agents = {**ledger.get("agents", {})}
    rec = dict(agents.get(agent) or {})
    rec["positions"] = pos
    rec["virtual_cash"] = round(
        _cash(rec, quota) - volume * cost, 2)
    agents[agent] = rec
    return {**ledger, "agents": agents}


def record_sell(ledger: dict, agent: str, code: str, volume: int,
                sell_price: float, ts: str, exit_reason: str | None = None,
                quota: float = DEFAULT_QUOTA, market: str | None = None) -> dict:
    """卖出记账（不可变）：扣减数量，减到 0 移除；虚拟现金加回卖出金额；
    不存在的持仓原样返回。

    同时产出一条回合记录挂到 ledger['roundtrips']（由实现层的 save 统一落盘）。
    exit_reason 由卖出侧传入（止损/止盈/调仓/强平…），行为归因时按它分类。
    market 由实现层绑定（A股/港股/美股），供下游按市场选行情源。
    """
    pos = dict(positions(ledger, agent))
    if code not in pos:
        return ledger
    held = pos[code]
    sold = min(int(volume), int(held.get("volume") or 0))
    remaining = held["volume"] - volume
    rt = roundtrip_row(held, agent, code, sold, sell_price, ts,
                       exit_reason, closed=remaining <= 0, market=market)
    if remaining <= 0:
        del pos[code]
    else:
        pos[code] = {**held, "volume": remaining, "last_ts": ts}
    agents = {**ledger.get("agents", {})}
    rec = dict(agents.get(agent) or {})
    rec["positions"] = pos
    rec["virtual_cash"] = round(
        _cash(rec, quota) + volume * float(sell_price), 2)
    agents[agent] = rec
    return {**ledger, "agents": agents,
            "roundtrips": [*(ledger.get("roundtrips") or []), rt]}


# ---------- 延期单（纯操作部分；落盘与错误判定留在实现层） ----------
def load_deferred(ledger: dict) -> list:
    """待重放订单：在途、被拒（行情断开/桥不可达）的买卖意图。"""
    return list(ledger.get("deferred") or [])


def save_deferred(ledger: dict, agent: str, side: str, code: str, volume: int,
                  reason: str, ts: str) -> dict:
    """登记一笔延期单（不可变）。同 agent+code+side 只保留最新一笔，
    防止断链期间每轮重复堆积。"""
    item = {"agent": agent, "side": side, "code": code, "volume": int(volume),
            "reason": reason, "ts": ts}
    deferred = [d for d in load_deferred(ledger)
                if not (d.get("agent") == agent and d.get("code") == code
                        and d.get("side") == side)]
    deferred.append(item)
    return {**ledger, "deferred": deferred}


def clear_deferred(ledger: dict, agent: str, side: str, code: str) -> dict:
    """清除某笔延期单（重放成功后调用）。"""
    return {**ledger, "deferred": [
        d for d in load_deferred(ledger)
        if not (d.get("agent") == agent and d.get("code") == code and d.get("side") == side)
    ]}


# ---------- 回合台账落盘：**本模块唯一的 IO** ----------
# 为什么 IO 放在协议层（而非各实现层）：落盘逻辑复制三份正是本模块要消灭的病。
# 三市场共用一个文件，靠记录里的 `market` 字段区分（下游按市场选行情源）。
# ⚠️ 迁移中：live_ledger 目前仍用自己那份（有单测覆盖），待 A 股阶段一并切过来。
ROUNDTRIP_LOG = Path(__file__).resolve().parent.parent / "logs" / "live_roundtrips.jsonl"


def append_roundtrips(rows: list, path: Path | None = None) -> bool:
    """追加回合记录到 jsonl；全部成功才返回 True。"""
    import json

    target = path or ROUNDTRIP_LOG
    try:
        target.parent.mkdir(exist_ok=True)
        with target.open("a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def flush_roundtrips(ledger: dict, path: Path | None = None) -> dict:
    """把账本里累积的 roundtrips 落盘；**落盘成功才从账本中清掉**——
    写失败则留在账本里等下次 save 重试，不丢。"""
    pending = list(ledger.get("roundtrips") or [])
    if pending and append_roundtrips(pending, path):
        return {k: v for k, v in ledger.items() if k != "roundtrips"}
    return ledger
