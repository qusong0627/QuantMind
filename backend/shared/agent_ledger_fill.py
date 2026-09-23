"""成交 → 分账账本（P2.7）：真单成交落账的**唯一入口**。

为什么需要这一层：分账账本按 agent 切段（``qm_agent_ledger_*``），而**成交回报里没有
agent**——券商回报只带来订单号、成交号、量、价；「这一轮是哪家模型在跑」在成交那一刻
已经不在数据里了。归属的唯一载体是 ``orders.agent``（下单时由决策段写死：
``OrderRequest.agent`` → ``OrderCreate.agent`` → ``orders.agent``）。本模块做的事就是
在成交落库的那一刻把它读回来，记进同名段。

三条纪律（与调用方的契约，全部有测试钉住）：

1. **没有 agent = 静默跳过**（返回 ``None``）。人点单/风控单/托管单/隔壁桥镜像单都不
   写分账——它们的成交属于账户，不属于任何模型，这些单的 ``orders.agent`` 恒 NULL。
2. **一笔成交一根幂等键**（``fill_key`` = 券商成交号）。重投（桥重发、poller 重启补拉、
   对账重放）在账本侧第一步就停住：返回 ``duplicate=True``，不会再记一笔。
3. **不 catch**：与调用方**同 session 同事务**。账本写不进去 ⇒ 这笔成交也不落库
   （调用方 commit 一起失败）⇒ 下一轮轮询重投 ⇒ 重试落账。宁可晚记，不可「成交在、
   账本无」：这本账用来算「这家模型还能买多少」，少记一笔就是把额度放给不该买的腿。
   代价是：账本若永久写不进去（表缺失/权限），订单状态更新也会一起停在原地——这是
   刻意选的失败形态，它**响**（poller 每轮告警 + errors 计数），而静默少记不响。
   写入侧的建表由 trade 服务启动期 ``ensure_agent_ledger_tables_async`` 保证。

已知有界偏差（不修，因为修法比问题更坏）：QMT 轮询在「委托已成交但查不到成交明细」时
先用合成成交号落一行（``qmt_exec_poller._maybe_synthesize_trade``），真实明细到达后就地
升级该行（改 ``exchange_trade_id``、按差额校正数量与价格）。账本流水记在**合成键**下且
不跟着改价：差额 = 同一订单「柜面均价」与「成交明细价」之差，有界且通常为 0。行数与
成交笔数仍一一对应（升级不新增行），故「额度用了多少」不受影响；要做到改价同步，就得
允许账本流水被改写，那会动掉「流水即事实」的前提，不做。
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from backend.shared.agent_ledger_store import ApplyOutcome, apply_fill
from backend.shared.order_contract import normalize_agent
from backend.shared.stock_utils import StockCodeUtil
from backend.shared.utc_datetime import as_utc, utc_now

logger = logging.getLogger(__name__)

#: 判不出市场时的兜底。实盘路径（QMT 桥）就是 A 股；判据用共享的
#: ``StockCodeUtil.detect_market``（唯一定义处），判不出时与账本契约列的缺省一致。
DEFAULT_MARKET = "CN"


def market_of(symbol: str) -> str:
    """标的 → 账本的市场列：走共享判据，判不出按 :data:`DEFAULT_MARKET`。"""
    return StockCodeUtil.detect_market(str(symbol or "")) or DEFAULT_MARKET


async def post_fill_for_order(
    session: Any,
    *,
    order: Any,
    fill_key: str,
    quantity: Any,
    price: Any,
    filled_at: datetime | None = None,
    trade_date: date | str | None = None,
) -> ApplyOutcome | None:
    """把一笔真单成交记进 ``order.agent`` 名下；无归属返回 ``None``。

    :param session: **调用方的** session，本函数不 commit（见模块 docstring 纪律 3）。
    :param fill_key: 券商成交号。空键当场 ``ValueError``：没有唯一成交号就没有幂等，
        调用方本来也不该在那条分支上落成交行（``qmt_exec_poller`` 的「状态回调无成交
        号」分支只更状态、不落 Trade）。
    :param quantity: 本笔成交数量（不是累计）。
    :param filled_at: 成交瞬时（缺省 = 现在）。调用方应传**与成交行同一个值**。
    :param trade_date: 成交日，缺省取 ``filled_at`` 的 **UTC 日**。它与 ``fill_key``
        一起构成账本的当日唯一键，所以必须来自成交数据而不是处理时刻——跨零点的重投
        要能算出同一天。A/HK/US 日盘成交落在 UTC 01:30~21:00，UTC 日 == 当地交易日。

    未知方向/空代码/非法量价不抛：``apply_fill`` 落一条 ``applied_volume=0`` 的流水并
    留 note（可查），本函数把 note 打进日志。
    """
    agent = normalize_agent(getattr(order, "agent", None))
    if not agent:
        return None
    key = str(fill_key or "").strip()
    if not key:
        raise ValueError(
            "post_fill_for_order：没有成交号（fill_key）——分账账本拒绝无幂等键的记账"
        )
    stamp = as_utc(filled_at) if filled_at is not None else utc_now()
    day: date | str = trade_date if trade_date is not None else stamp.date()
    symbol = str(getattr(order, "symbol", "") or "")
    order_id = str(getattr(order, "order_id", "") or "")
    outcome = await apply_fill(
        session,
        tenant_id=str(getattr(order, "tenant_id", "") or "default"),
        user_id=str(getattr(order, "user_id", "") or ""),
        agent=agent,
        code=symbol,
        side=getattr(order, "side", ""),
        volume=quantity,
        price=price,
        fill_key=key,
        trade_date=day,
        order_id=order_id,
        filled_at=stamp,
        market=market_of(symbol),
    )
    if outcome.note:
        # store 的写纪律 3：note 非空必须进日志——它是「这笔为什么没记全」的唯一答案
        logger.warning(
            "[AgentLedger] 成交落账留痕 agent=%s order_id=%s fill_key=%s：%s",
            agent,
            order_id,
            key,
            outcome.note,
        )
    elif outcome.duplicate:
        logger.info(
            "[AgentLedger] 成交重投（账本已有） agent=%s order_id=%s fill_key=%s",
            agent,
            order_id,
            key,
        )
    else:
        logger.info(
            "[AgentLedger] 分账成交 agent=%s %s 量=%s 价=%s 单=%s",
            agent,
            symbol,
            quantity,
            price,
            order_id,
        )
    return outcome
