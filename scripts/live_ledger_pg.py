"""宿主脚本的 **PG 分账账本**适配层（只认 ``qm_agent_ledger_*``）。

背景（2026-10-30）：文件账本 ``logs/live_ledger.json`` 自 2026-09-30 迁移夜起冻结，
真单成交只进 PG。任何「还剩多少额度 / 这只票归谁」的问题都必须读 PG，否则就是拿
一个月前的快照分配今天的真钱。

本模块只做四件事：
* 宿主解释器缺 ``asyncpg`` 时切到仓库 ``.venv``（系统 python 没有 PG 驱动）；
* 读 PG 活账（形状与旧文件账本一致：``{version, agents:{...}}``）；
* 额度逐 agent 取 ``TradingPolicy``（与决策轮/实盘页同一出处）；
* 成交按桥的**累计成交量台阶**写 ``apply_fill``（幂等键形状只由
  ``backend.shared.agent_ledger_fill`` 定义）。

不发单、不做风控判断——下单与闸门仍在 ``live_trade_picks`` / 桥侧。
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

LEDGER_TENANT = "default"
DEFAULT_LEDGER_USER = "10000001"
#: 成交回报轮询步长（秒）；桥限流 60 req/min，3s 一次仍有余量。
FILL_POLL_S = 3
CN_TZ = ZoneInfo("Asia/Shanghai")


# ---------- 运行环境 ----------


def ensure_pg_runtime() -> None:
    """解释器没有 asyncpg 时切到仓库 ``.venv``（宿主系统 python 没装 PG 驱动）。"""
    try:
        import asyncpg  # noqa: F401

        return
    except ModuleNotFoundError:
        pass
    venv = Path(__file__).resolve().parent.parent / ".venv" / "bin" / "python"
    target = Path(sys.argv[0] or "live_trade_picks.py").resolve()
    if venv.is_file() and os.environ.get("_QM_LTP_REEXEC") != "1":
        os.environ["_QM_LTP_REEXEC"] = "1"
        print(f"ℹ️ 系统 python 缺 asyncpg → 切到 {venv}", file=sys.stderr)
        os.execv(str(venv), [str(venv), str(target), *sys.argv[1:]])
    raise SystemExit(
        "❌ 当前解释器缺 asyncpg，无法读写 PG 分账账本；"
        f"请用 {venv} 运行（或 pip install asyncpg）"
    )


def load_env() -> None:
    """仓库根 ``.env`` → ``os.environ``（不覆盖已有键）；DB 地址走 ``host_env`` 同一判据。

    ``.env`` 的 ``DB_HOST`` 是 compose 服务名 ``db``（容器内才解析得开），宿主必须
    用 ``QM_DB_BIND`` 指向的 LAN 地址——与 ``live_model_analysis._pg_connect`` /
    ``host_env`` 同一口径。
    """
    root = Path(__file__).resolve().parent.parent
    try:
        lines = (root / ".env").read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        if key and not os.environ.get(key):
            os.environ[key] = value.strip().strip('"').strip("'")
    from host_env import resolve_postgres_host

    host = resolve_postgres_host()
    if host:
        os.environ["POSTGRES_HOST"] = host
        os.environ["DB_HOST"] = host


def _ledger_user() -> str:
    """(tenant, user) 与决策轮 ``resolve_db_account_user`` 同口径。"""
    from backend.shared.simulation_account_keys import normalize_runtime_user

    raw = str(os.environ.get("QM_DECISION_ACCOUNT_USER_ID") or "").strip()
    return normalize_runtime_user(raw or DEFAULT_LEDGER_USER)


_LOOP: asyncio.AbstractEventLoop | None = None


def _run(coro):
    """同一进程共用一个事件循环：DB 连接池按 loop 绑定，不能每次 asyncio.run 重建。"""
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


# ---------- 读侧（形状与旧文件账本一致） ----------


def load_ledger() -> dict:
    """读 PG 活账；失败抛异常（调用方 fail-fast，不拿旧文件账本兜底）。"""
    from backend.shared.agent_ledger_store import load_ledger as _load
    from backend.shared.database_manager_v2 import get_session

    async def _go():
        async with get_session(read_only=True) as session:
            return await _load(session, tenant_id=LEDGER_TENANT, user_id=_ledger_user())

    return _run(_go())


def load_policy():
    from backend.shared.trading_policy import load_trading_policy

    return load_trading_policy()


def _positions(ledger: dict, agent: str) -> dict:
    rec = (ledger.get("agents") or {}).get(agent) or {}
    return rec.get("positions") or {}


def agent_used(ledger: dict, agent: str) -> float:
    """已用额度 = 名下持仓成本合计（与决策轮 ``quota_used`` 同口径）。"""
    return round(
        sum(
            float(p.get("volume") or 0) * float(p.get("cost_price") or 0)
            for p in _positions(ledger, agent).values()
        ),
        2,
    )


def agent_virtual_cash(ledger: dict, agent: str) -> float | None:
    rec = (ledger.get("agents") or {}).get(agent)
    if not isinstance(rec, dict):
        return None
    cash = rec.get("virtual_cash")
    try:
        return float(cash) if cash is not None else None
    except (TypeError, ValueError):
        return None


def agent_quota(policy, agent: str) -> float:
    return float(policy.for_agent(agent).quota)


def agent_remaining(ledger: dict, agent: str, policy) -> float:
    """可用额度 = ``min(授予额度 − 已用, 账上虚拟现金)``——与决策轮执行段同式。"""
    quota = agent_quota(policy, agent)
    left = quota - agent_used(ledger, agent)
    cash = agent_virtual_cash(ledger, agent)
    return round(min(left, cash) if cash is not None else left, 2)


def holders_of(ledger: dict, code: str) -> list[str]:
    """持有该代码的 agent（卖出归属；多持有时由调用方提示后再选第一顺位）。"""
    return sorted(
        agent for agent in (ledger.get("agents") or {}) if code in _positions(ledger, agent)
    )


def holders_by_age(ledger: dict, code: str) -> list[tuple[str, int]]:
    """持有该代码的 ``[(agent, 可减持仓量)]``，按 ``buy_ts`` FIFO 排序。

    手工卖出是**共享账户级**动作：一次卖 6900 股可能是三家 agent 各持一段。账本
    必须按 FIFO 逐家扣减（与 ``repair_agent_ledger_manual_sells`` 同一分配纪律），
    否则全记到第一家会被 ``record_sell`` 夹到它自己的持仓、其余家留下幻影仓。
    """
    rows: list[tuple[str, int, str]] = []
    for agent in holders_of(ledger, code):
        pos = _positions(ledger, agent).get(code) or {}
        try:
            volume = int(float(pos.get("volume") or 0))
        except (TypeError, ValueError):
            volume = 0
        if volume > 0:
            rows.append((agent, volume, str(pos.get("buy_ts") or "")))
    rows.sort(key=lambda row: (row[2] or "9999", row[0]))
    return [(agent, volume) for agent, volume, _ in rows]


# ---------- 写侧（成交 → 分账） ----------


def apply_fill(
    *,
    agent: str,
    code: str,
    side: str,
    volume: int,
    price: float,
    order_id: str,
    cumulative_volume: int,
    exit_reason: str | None = None,
    key_suffix: str = "",
):
    """一笔成交写 PG 分账（幂等键 = 桥的累计量台阶，形状只由 ``agent_ledger_fill`` 定义）。

    ``key_suffix``：同一笔桥委托在**多家 agent** 之间分摊时，账本幂等键
    ``(tenant,user,trade_date,fill_key)`` 不含 agent，必须把 agent 拼进键，否则第二家
    的流水会被当重复吞掉（与 ``repair_agent_ledger_manual_sells`` 的 ``:<agent>`` 同理）。
    """
    from backend.shared.agent_ledger_fill import (
        derive_cumulative_fill_key,
        market_of,
    )
    from backend.shared.agent_ledger_store import apply_fill as _apply
    from backend.shared.database_manager_v2 import get_session
    from backend.shared.utc_datetime import utc_now

    stamp = utc_now()
    trade_day = stamp.astimezone(CN_TZ).date()
    fill_key = derive_cumulative_fill_key(
        broker_order_id=order_id,
        cumulative_volume=cumulative_volume,
        trade_date=trade_day,
    )
    if not fill_key:
        raise RuntimeError(
            f"无法派生成交幂等键（委托号 {order_id!r} / 累计成交量 {cumulative_volume}）"
        )
    if key_suffix:
        fill_key = f"{fill_key}:{key_suffix}"
    quota = agent_quota(load_policy(), agent)

    async def _go():
        async with get_session() as session:
            return await _apply(
                session,
                tenant_id=LEDGER_TENANT,
                user_id=_ledger_user(),
                agent=agent,
                code=code,
                side=side,
                volume=volume,
                price=price,
                fill_key=fill_key,
                trade_date=trade_day,
                order_id=str(order_id or ""),
                filled_at=stamp,
                quota=quota,
                market=market_of(code),
                exit_reason=exit_reason,
            )

    return _run(_go())


def settle_fill(
    broker,
    *,
    allocations: list[tuple[str, int]],
    code: str,
    side: str,
    order_id: str,
    limit_price: float,
    timeout_s: int,
) -> dict:
    """轮询委托到终态/超时，按**累计成交量台阶**逐段写 PG 分账。

    ``allocations`` = ``[(agent, 可减持仓量)]``（按 FIFO 排好）。买入传
    ``[(agent, 大数)]``；卖出传 ``holders_by_age`` 的结果——共享账户一笔卖单可能
    同时减多家 agent 的仓，全记到第一家会被 ``record_sell`` 夹取、其余家留幻影仓。

    下单 ≠ 成交：只有 ``filled_volume`` 增长才记账；未成交/未终态的余量在本工具里
    **没有**后续跟踪（旧实现挂的文件 pending 链写的也是已冻结的账本，不再使用），
    调用方必须把未终态/超时如实打印并落 ``live_trade`` 流水，供人工核对。

    桥只给「委托级累计均价」：第 2 台阶起的增量价从累计金额差还原，不把先成交那段
    的价差摊到后一段上。返回 ``{filled, price, status, terminal, error, by_agent}``；
    ``by_agent`` = 各 agent 实际记上的数量（幂等重复也计入，因为账上已有）。
    """
    from live_fills import TERMINAL_STATUSES

    remaining = [[agent, int(capacity)] for agent, capacity in allocations]
    recorded = 0
    recorded_cost = 0.0
    last_price = float(limit_price or 0)
    status = ""
    error = ""
    by_agent: dict[str, int] = {}
    deadline = time.time() + max(int(timeout_s), FILL_POLL_S)
    while True:
        order = None
        try:
            for row in broker.get_orders():
                if str(row.get("order_id") or "") == str(order_id):
                    order = row
                    break
        except Exception as exc:  # noqa: BLE001 查询失败不抹掉已记部分
            error = f"委托查询失败：{type(exc).__name__}: {exc}"
        if order is not None:
            status = str(order.get("status") or "")
            try:
                cum = int(float(order.get("filled_volume") or 0))
            except (TypeError, ValueError):
                cum = 0
            if cum > recorded:
                cum_avg = float(order.get("filled_price") or last_price or 0)
                if cum_avg <= 0:
                    error = f"成交价不可用（累计 {cum} 股）：本台阶不记账"
                    break
                last_price = cum_avg
                delta = cum - recorded
                price = cum_avg
                if recorded > 0:
                    price = (cum_avg * cum - recorded_cost) / delta
                    if price <= 0:
                        price = cum_avg
                left = delta
                failed = False
                for item in remaining:
                    if left <= 0:
                        break
                    agent, capacity = item
                    take = min(capacity, left)
                    if take <= 0:
                        continue
                    suffix = agent if len(remaining) > 1 else ""
                    try:
                        outcome = apply_fill(
                            agent=agent,
                            code=code,
                            side=side,
                            volume=take,
                            price=price,
                            order_id=order_id,
                            cumulative_volume=cum,
                            exit_reason="manual" if side == "sell" else None,
                            key_suffix=suffix,
                        )
                    except Exception as exc:  # noqa: BLE001 真金账本写失败必须显式报
                        error = (
                            f"分账记账失败（累计 {cum} 股 / {agent} {take} 股）："
                            f"{type(exc).__name__}: {exc}"
                        )
                        failed = True
                        break
                    item[1] = capacity - take
                    by_agent[agent] = by_agent.get(agent, 0) + take
                    left -= take
                    if outcome.duplicate:
                        print(
                            f"   ℹ️ 累计 {cum} 股这一台阶已记过账（{agent}），未重复记"
                        )
                    elif outcome.note:
                        print(f"   ⚠️ 分账留痕（{agent}）：{outcome.note}")
                if failed:
                    break  # 不推进 recorded：下一拍重试同一台阶（已记段幂等跳过）
                if left > 0:
                    error = (
                        f"成交 {delta} 股中有 {left} 股在分账中没有对应持仓可减"
                        f"（账本与真实账户不一致，需人工核对）"
                    )
                recorded = cum
                recorded_cost += price * delta
            if status in TERMINAL_STATUSES:
                break
        if time.time() >= deadline:
            break
        time.sleep(FILL_POLL_S)
    return {
        "filled": recorded,
        "price": last_price,
        "status": status,
        "terminal": status in TERMINAL_STATUSES,
        "error": error,
        "by_agent": by_agent,
    }
