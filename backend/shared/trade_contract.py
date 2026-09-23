"""成交（``trades``）的持久化唯一键：一笔成交只允许一行——由 DB 说了算。

## 为什么需要它（P2.7 分账的前置）

分账账本（``agent_ledger_fill``）把「成交 → 记账」做成**同事务**的原子动作，
幂等键是券商成交号 ``exchange_trade_id``。而这个幂等**建立在成交行本身只落一次**
之上：两条成交行 = 两次 ``apply_fill`` = 虚拟现金凭空多一笔、额度被同一笔扣两回。

已知的窗口是「commit 成功、进程以为失败」：``execution_stream_consumer`` 的
``_handle_order_filled`` 与 ``qmt_exec_reconciler.apply_execution_report`` 都是
**SELECT-then-INSERT**，而重投走 ``_retry_or_dlq`` 把**同一份 fields** 带
``retry_count+1`` 重新 xadd 回原流——没有事件级去重 id，也没有 DB 约束兜底。
隔壁台账用 ``applied_fills[order_id]`` 这个文件标记解决同一问题；本仓用部分唯一索引。

## 口径

``(tenant_id, user_id, exchange_trade_id) WHERE exchange_trade_id IS NOT NULL``

* **带租户/用户**：券商成交号只在**一个账户**内唯一。两个账户（多租户部署，或两个
  券商各自从 1 开始编号）会撞出同一个号，全库唯一会把后者的成交判成重复 →
  **静默丢单**（不落成交行、不更新订单、不记分账）。写入侧的 SELECT 也必须带同
  一维度，否则索引与查询各说各话：索引拦得住的，查询先一步把正常成交丢了。
* **部分索引**：状态回调（无唯一成交号）本就不落成交行，排除 NULL 才能让索引只管
  真正需要唯一的那批行。
* **合成成交**：``qmt-synth-<order_key>``（``SYNTH_TRADE_PREFIX``）也在唯一范围内
  ——同一订单同一时刻只会有一条，随后被真实明细**就地改键**（升级）。
  改键撞上「真实行已存在」时的收口在 ``qmt_exec_poller._upgrade_synth_trade``
  （删合成行而不是改键），本模块只提供前缀常量。

## 撞键之后的语义

撞键 = 同一笔成交被两条路径同时写，唯一键把**第二条**挡在门外。它抛
``IntegrityError``：消费者侧显式接住并当「已记过」处理（该事件的事务里没有别的
工作），reconciler 侧**不接**——它由调用方（poller 的周期兜底 / 桥上报的请求级
catch）接住并重投，下一轮 SELECT 命中已有行、安静跳过。**这是有意的**：静默吞掉
撞键会掩盖真正的双写缺陷，而响亮地重投不改变最终结果（不双计）。

安全化三纪律与 ``order_contract`` 的两个唯一索引同款：零 DDL 快路径 → 仅真变更
才 DDL（``lock_timeout=3s``）→ 异常只告警不抛（无索引 = 旧语义，业务不中断）。
存量重复存在时**不建索引**并点名——自动删金融行比重复本身更危险。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: 成交表上的部分唯一索引名（体检 C14 也按这个名字探测）
TRADE_UNIQUE_INDEX = "uq_trades_scope_exchange_trade_id"

#: 合成成交的幂等键前缀。唯一生产者是 ``qmt_exec_poller``（那里按本常量赋值，
#: 不再自己写一份字面量）；登记在这里是为了让对账/体检认得出「键不是券商编号」
#: 的那批行——它们与真实成交的幂等键**不同族**，对账要按族分开处理。
SYNTH_TRADE_PREFIX = "qmt-synth-"

#: 存量重复扫描（唯一索引的预检，也是体检 C14 的判定输入）。
#: 一条 SQL 两处用，避免「预检口径」与「体检口径」漂移。
DUP_FILLS_SQL = (
    "SELECT tenant_id, user_id, exchange_trade_id, count(*) AS c "
    "FROM trades WHERE exchange_trade_id IS NOT NULL "
    "GROUP BY tenant_id, user_id, exchange_trade_id HAVING count(*) > 1 "
    "LIMIT :limit"
)

_trade_index_ready: bool | None = None


async def find_duplicate_fills_async(limit: int = 3) -> list[dict[str, Any]]:
    """存量重复成交（同租户同用户同成交号）。查不到/表缺失时返回空表。"""
    from sqlalchemy import text as sa_text

    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=True) as session:
        rows = (
            await session.execute(sa_text(DUP_FILLS_SQL), {"limit": int(limit)})
        ).fetchall()
    return [
        {
            "tenant_id": str(r[0]),
            "user_id": str(r[1]),
            "exchange_trade_id": str(r[2]),
            "c": int(r[3]),
        }
        for r in rows
    ]


async def trade_unique_index_ready_async() -> bool:
    """探测唯一索引是否已存在（进程内缓存）。探测失败按未就绪。"""
    global _trade_index_ready
    if _trade_index_ready is not None:
        return _trade_index_ready
    from sqlalchemy import text as sa_text

    from backend.shared.database_manager_v2 import get_session

    try:
        async with get_session(read_only=True) as session:
            row = (
                await session.execute(
                    sa_text("SELECT 1 FROM pg_indexes WHERE indexname = :n LIMIT 1"),
                    {"n": TRADE_UNIQUE_INDEX},
                )
            ).fetchone()
        _trade_index_ready = row is not None
    except Exception as exc:  # noqa: BLE001 - 探测失败按未就绪（旧语义）
        logger.warning("[TradeContract] 唯一索引探测失败: %s", exc)
        return False
    return bool(_trade_index_ready)


async def ensure_trade_unique_index_async() -> bool:
    """幂等启用成交唯一索引（P2.7）。就绪/新建成 True，未启用 False。

    顺序：pg_indexes 预检 → 存量重复预检 → 零 DDL 快路径 → 仅真新建才 DDL。
    存量重复存在时**不建**并 ERROR 点名（先去重再重试；见
    ``scripts/repair_*`` 家族的处置口径：金融行不自动删）。
    """
    global _trade_index_ready
    if _trade_index_ready:
        return True
    from sqlalchemy import text as sa_text

    from backend.shared.database_manager_v2 import get_session

    try:
        async with get_session(read_only=True) as session:
            exists = (
                await session.execute(
                    sa_text("SELECT 1 FROM pg_indexes WHERE indexname = :n LIMIT 1"),
                    {"n": TRADE_UNIQUE_INDEX},
                )
            ).fetchone()
            if exists is not None:
                _trade_index_ready = True
                return True
            dupes = (
                await session.execute(sa_text(DUP_FILLS_SQL), {"limit": 3})
            ).fetchall()
            if dupes:
                logger.error(
                    "[TradeContract] 存量重复成交阻止唯一索引启用（需先人工去重，"
                    "分账账本的双记防线暂未生效）: %s",
                    [(str(d[0]), str(d[1]), str(d[2]), int(d[3])) for d in dupes],
                )
                return False
        async with get_session(read_only=False) as session:
            await session.execute(sa_text("SET LOCAL lock_timeout = '3s'"))
            await session.execute(
                sa_text(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS {TRADE_UNIQUE_INDEX} "
                    "ON trades (tenant_id, user_id, exchange_trade_id) "
                    "WHERE exchange_trade_id IS NOT NULL"
                )
            )
            await session.commit()
        _trade_index_ready = True
        logger.info(
            "[TradeContract] 成交唯一索引已启用（P2.7）: %s", TRADE_UNIQUE_INDEX
        )
        return True
    except Exception as exc:  # noqa: BLE001 - 失败不阻断（旧语义继续，体检 C14 可查）
        logger.warning("[TradeContract] 唯一索引自愈失败（不阻断）: %s", exc)
        return False
