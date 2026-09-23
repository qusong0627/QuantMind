"""Order/Fill 契约（T-P1-03）：订单台账的契约列 + 客户端幂等键合成。

侦察结论（2026-09-16，按真实缺口收窄）：
- ``sim_orders`` 已有 ``price_source/execution_model``（apply_filled 已在写取价来源），
  ``reason`` 已由 ``remarks`` 承载——**不重复造列**；
- 真实缺口：① ``client_order_id`` 只写 ``simulation_orders`` 投影（注释自述），投影表
  为空时幂等实际断链 → 落到 ``sim_orders``；② ``orders``(REAL) 无 ``price_source``；
  ③ 两表均无 ``source``（rebalance/manual/mirror/sltp 来源分类，供对账与交易台下钻）。

迁移沿用自愈式先例（独立事务，不污染调用方；失败不置标记可重试）。

**唯一索引（T-P2-08，2026-09-16 启用）**：``uq_sim_orders_scope_client_order_id``——
``(tenant_id, user_id, client_order_id) WHERE client_order_id IS NOT NULL`` 部分唯一索引。
启用前的顾虑（"硬约束会把重复单变成 500"）以两侧收口解决：
① 写入侧（``SimOrderService.create_order``）捕获 IntegrityError → 按幂等键反查已有单 →
   抛 ``DuplicateSimOrderError``，由各调用方转既有 duplicate 语义（不再 500）；
② 迁移侧先查存量重复——**有重复则不建索引并 ERROR 点名**（自动删金融行比重复更危险，
   交 repair 脚本/人工），去重后下一次调用自动启用；
③ 风控直插单 cid 恒 NULL，部分索引不覆盖（其幂等靠 Redis already_fired，语义不变）。
"""

from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger(__name__)

SIM_ORDER_COLUMNS = (
    ("client_order_id", "VARCHAR(100)"),
    ("source", "VARCHAR(32)"),
)

ORDER_COLUMNS = (
    ("price_source", "VARCHAR(64)"),
    ("source", "VARCHAR(32)"),
)

# source 取值域（Order 契约：来源分类，供过滤/对账/下钻）
SOURCE_REBALANCE = "rebalance"
SOURCE_MANUAL = "manual"
SOURCE_HOSTED = "hosted"  # 托管调度自动单（dispatcher auto- 前缀）
SOURCE_FORCED_LIQUIDATION = "forced_liquidation"  # 融券维持担保比例强平
SOURCE_INTERNAL = "internal"
SOURCE_MIRROR = "mirror"
SOURCE_SLTP = "sltp"
SOURCE_SANDBOX = "sandbox"  # 沙箱策略信号（T-P2-01 收敛入 Router）
SOURCE_TDX_ROLLING = "tdx_rolling"  # 通达信滚动 paper 单（T-P2-01 收敛入 Router）
SOURCE_CO_PILOT = "co_pilot"  # 副驾驶建议卡一键执行（T-P6-16）
SOURCE_CANDIDATE_PUSH = "candidate_push"  # 候选信号页多选一键推送（T-FE-09）
SOURCE_REAL_DIRECT = "real_direct"  # 实盘独有持仓直卖（用户一键卖出，不经模拟台账）
SOURCE_LLM_DECISION = "llm_decision"  # 决策层 LLM 调仓腿（P2.3b；执行段执行、决策层不管执行）

# Fill 取价来源（REAL 侧：成交回报来自券商）
PRICE_SOURCE_BROKER_FILL = "broker_fill"
PRICE_SOURCE_SNAPSHOT = "snapshot"  # F2 快照级撮合取价（T-P6-17）

# 幂等键长度上限（与 VARCHAR(100) 对齐）
MAX_CLIENT_ORDER_ID_LEN = 100

_ensured = False


def build_copilot_client_order_id(advice_id: str, symbol: str, side: str) -> str:
    """副驾驶建议执行幂等键：同建议同标的同方向 → 同键（重复点击不重复下单）。"""
    aid = "".join(ch for ch in str(advice_id or "") if ch.isalnum())[:8] or "noadv"
    sym = str(symbol or "").strip().upper() or "NA"
    sd = str(side or "").strip().lower() or "na"
    return f"cop-{aid}-{sym}-{sd}"[:MAX_CLIENT_ORDER_ID_LEN]


def build_candidate_client_order_id(batch_id: str, symbol: str, side: str) -> str:
    """候选信号一键推送的幂等键：同批量同标的同方向 → 同键。

    ``batch_id`` 由前端在**打开确认面板时生成一次**（而不是点「确认」时），
    这样重复点击、网络重试、浏览器重放都落在同一个键上；用户若想真的再下一单，
    重新打开一次面板即可。

    长度预算：``cand-`` 5 + batch 24 + ``-`` 1 + symbol 9（``600036.SH`` 上限）
    + ``-`` 1 + side 4 = 44 < 100，正常输入下**截断永不触发**；batch 超出 24 字符的
    部分被裁掉，因此两个 batch_id 只有第 25 位以后不同时会撞键（前端用 UUID4 前 24 位，
    可忽略）。
    """
    bid = "".join(ch for ch in str(batch_id or "") if ch.isalnum())[:24] or "nobatch"
    sym = str(symbol or "").strip().upper() or "NA"
    sd = str(side or "").strip().lower() or "na"
    return f"cand-{bid}-{sym}-{sd}"[:MAX_CLIENT_ORDER_ID_LEN]


def build_llm_decision_client_order_id(
    round_id: str, symbol: str, side: str, *, agent: str = ""
) -> str | None:
    """决策层 LLM 调仓腿的幂等键：同轮同标的同方向 → 同键；**缺参不强造**。

    ``round_id`` 是**那一轮决策**的标识（调用点在开轮时生成一次，跨重试不变）。
    它不是时间戳也不是内容哈希：轮内重试、进程重启后重放同一轮、桥回执丢失后的
    补投，都必须落在同一个键上。换 ``round_id`` 的唯一理由是**要真的再下一单**
    （也就是下一轮决策）。

    这里是 ``build_candidate_client_order_id`` 的**镜像选择**，理由不同：那一族
    的入参由前端生成，缺参说明请求坏了，占位符能让重试落在同一键上；本族三个入参
    全由本仓生成，缺参说明代码有 bug，而**固定占位符会把「我不知道这是哪一轮」
    变成「所有未知轮都是同一轮」**——同一个 (标的, 方向) 在后续轮次里会被静默去重，
    即「模型让卖、系统静默不卖」。故缺参一律 ``None``（不带幂等键的单照下，由
    调用点告警；``client_order_id`` 列可空、唯一索引是部分索引，这条路本来就支持）。

    与 ``cand-`` 同族形态（同长度预算、同截断口径），前缀不同是为了让「这条单从哪来」
    在台账里一眼可查——``cand`` 是人点的，``lld`` 是模型定的。

    ``agent`` = 做这条腿的那个**模型/agent**（多模型竞争 P2.7 落地时由调用点传入）。
    **一轮里有多个 agent 就必须传**：``round_id`` 是「轮」的标识，两家模型在同标的同
    方向上会算出**同一个键**，后一家被静默去重——正是本函数开头那段「模型让卖、
    系统静默不卖」的另一种形态。单 agent 轮次留空，键与历史完全一致（不改既有台账
    的去重口径）。长度预算：4+24+1+8+1+9+1+4 = 52 < 100。
    """
    rid = "".join(ch for ch in str(round_id or "") if ch.isalnum())[:24]
    sym = str(symbol or "").strip().upper()
    sd = str(side or "").strip().lower()
    if not rid or not sym or not sd:
        return None
    ag = "".join(ch for ch in str(agent or "") if ch.isalnum())[:8]
    segments = ["lld", rid] + ([ag] if ag else []) + [sym, sd]
    return "-".join(segments)[:MAX_CLIENT_ORDER_ID_LEN]


def build_bridge_plan_id(
    client_order_id: str | None = None, *, now_ns: int | None = None
) -> str:
    """合成下发给交易桥的 ``plan_id``（``/api/v1/plans/execute``）；给了键就原样用。

    精度**必须**是纳秒。桥侧 ``tools/bridge-windows/src/executor/plan_executor.py``
    的 ``execute_plan`` 按 ``plan_id`` 去重（命中即 ``status=duplicate`` → HTTP 409
    ``DUPLICATE_PLAN``），所以撞号不是「多下一单」而是**整笔单被丢掉**：没有
    ``order_id``、没有成交，客户端只拿到失败回执。原实现用 ``int(time.time())``（秒），
    同一秒内连发两笔（止损批量卖出多只标的最典型）必然撞号、第二笔凭空消失。

    ``now_ns`` 仅供测试注入，生产链路不传（取真实时钟）。
    """
    if client_order_id:
        return str(client_order_id)
    ns = time.time_ns() if now_ns is None else int(now_ns)
    return f"qm_{ns}_{os.getpid()}"


def build_sim_client_order_id(run_id: str, symbol: str, side: str) -> str | None:
    """合成引擎直发路径的确定性幂等键（同 run 同标的同方向 → 同键）。

    供托管调仓重跑时观测/未来去重使用；run_id 缺失返回 None（不强造）。
    """
    rid = str(run_id or "").strip()
    if not rid:
        return None
    sym = str(symbol or "").strip()
    sd = str(side or "").strip().lower()
    if not sym or not sd:
        return None
    return f"sim-{rid}-{sym}-{sd}"[:MAX_CLIENT_ORDER_ID_LEN]


_TABLE_COLUMNS = {
    "sim_orders": SIM_ORDER_COLUMNS,
    "orders": ORDER_COLUMNS,
}

_PRECHECK_SQL = (
    "SELECT column_name FROM information_schema.columns "
    "WHERE table_name = :table AND column_name = ANY(:cols)"
)


def _missing_for(table: str, present: set[str]) -> list[tuple[str, str]]:
    return [
        (name, col_type)
        for name, col_type in _TABLE_COLUMNS[table]
        if name not in present
    ]


async def _missing_columns_async(session) -> dict[str, set[str]]:
    from sqlalchemy import text as sa_text

    out: dict[str, set[str]] = {}
    for table, cols in _TABLE_COLUMNS.items():
        names = [n for n, _ in cols]
        rows = (
            await session.execute(
                sa_text(_PRECHECK_SQL), {"table": table, "cols": names}
            )
        ).fetchall()
        present = {str(r[0]) for r in rows}
        out[table] = {n for n in names if n not in present}
    return out


def ensure_order_contract_columns(conn) -> None:
    """幂等补齐契约列（同步；安全化：先查 existence，只对缺列 DDL + lock_timeout）。

    同日事故教训（见 signal_contract 注释）：热表无条件 ADD COLUMN IF NOT EXISTS
    仍申请 AccessExclusive，会与调用方未提交事务自阻塞并堵死全表；故先走
    information_schema 预检，列齐全零 DDL；缺列才 ALTER 且 3s 超时快速失败；
    异常只告警不抛出。
    """
    global _ensured
    if _ensured:
        return
    import logging

    from sqlalchemy import text as sa_text

    logger = logging.getLogger(__name__)
    try:
        missing: dict[str, list[tuple[str, str]]] = {}
        for table, cols in _TABLE_COLUMNS.items():
            names = [n for n, _ in cols]
            rows = conn.execute(
                sa_text(_PRECHECK_SQL), {"table": table, "cols": names}
            ).fetchall()
            present = {str(r[0]) for r in rows}
            gaps = _missing_for(table, present)
            if gaps:
                missing[table] = gaps
        if not missing:
            _ensured = True
            return
        engine = conn.get_bind()
        with engine.begin() as migration_conn:
            migration_conn.execute(sa_text("SET LOCAL lock_timeout = '3s'"))
            for table, gaps in missing.items():
                for name, col_type in gaps:
                    migration_conn.execute(
                        sa_text(
                            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {col_type}"
                        )
                    )
        _ensured = True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[OrderContract] 契约列自愈失败（不阻断业务；缺口列将在写入时报错）: %s", exc
        )


async def ensure_order_contract_columns_async() -> None:
    """幂等补齐契约列（异步；与同步变体同款安全化）。"""
    global _ensured
    if _ensured:
        return
    import logging

    from sqlalchemy import text as sa_text

    from backend.shared.database_manager_v2 import get_session

    logger = logging.getLogger(__name__)
    try:
        async with get_session(read_only=False) as pre_session:
            missing = await _missing_columns_async(pre_session)
        gaps = {t: cols for t, cols in missing.items() if cols}
        if not gaps:
            _ensured = True
            return
        async with get_session(read_only=False) as migration_session:
            await migration_session.execute(sa_text("SET LOCAL lock_timeout = '3s'"))
            for table, names in gaps.items():
                for name, col_type in _missing_for(table, set()):
                    if name in names:
                        await migration_session.execute(
                            sa_text(
                                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {col_type}"
                            )
                        )
            await migration_session.commit()
        _ensured = True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[OrderContract] 契约列自愈失败（不阻断业务；缺口列将在写入时报错）: %s", exc
        )


# ── sim_orders 幂等键唯一索引（T-P2-08）─────────────────────────────

SIM_ORDER_UNIQUE_INDEX = "uq_sim_orders_scope_client_order_id"

_unique_index_ready: bool | None = None


async def sim_order_unique_index_ready_async() -> bool:
    """探测唯一索引是否已存在（进程内缓存）。"""
    global _unique_index_ready
    if _unique_index_ready is not None:
        return _unique_index_ready
    from sqlalchemy import text as sa_text

    from backend.shared.database_manager_v2 import get_session

    try:
        async with get_session(read_only=True) as session:
            row = (
                await session.execute(
                    sa_text(
                        "SELECT 1 FROM pg_indexes WHERE indexname = :n LIMIT 1"
                    ),
                    {"n": SIM_ORDER_UNIQUE_INDEX},
                )
            ).fetchone()
        _unique_index_ready = row is not None
    except Exception as exc:  # noqa: BLE001 - 探测失败按未就绪（旧语义）处理
        logger.warning("[OrderContract] 唯一索引探测失败: %s", exc)
        return False
    return bool(_unique_index_ready)


async def ensure_sim_order_unique_index_async() -> bool:
    """幂等启用 sim_orders 幂等键唯一索引（T-P2-08）。就绪/新建成 True，未启用 False。

    安全化（与列迁移同款三纪律）：pg_indexes/存量重复预检 → 零 DDL 快路径 →
    仅新建才 DDL（lock_timeout=3s）→ 异常只告警不抛出（无索引=旧语义，业务不中断）。
    存量重复存在时**不建索引**并 ERROR 点名（health C05c 也在扫同口径重复）。
    """
    global _unique_index_ready
    if _unique_index_ready:
        return True
    from sqlalchemy import text as sa_text

    from backend.shared.database_manager_v2 import get_session

    try:
        async with get_session(read_only=True) as session:
            exists = (
                await session.execute(
                    sa_text("SELECT 1 FROM pg_indexes WHERE indexname = :n LIMIT 1"),
                    {"n": SIM_ORDER_UNIQUE_INDEX},
                )
            ).fetchone()
            if exists is not None:
                _unique_index_ready = True
                return True
            dupes = (
                await session.execute(
                    sa_text(
                        "SELECT tenant_id, user_id, client_order_id, count(*) AS c "
                        "FROM sim_orders WHERE client_order_id IS NOT NULL "
                        "GROUP BY tenant_id, user_id, client_order_id "
                        "HAVING count(*) > 1 LIMIT 3"
                    )
                )
            ).fetchall()
            if dupes:
                logger.warning(
                    "[OrderContract] 存量重复单阻止唯一索引启用（需先去重，"
                    "见 scripts/repair_sim_order_duplicates.py）: %s",
                    [(str(d[0]), str(d[1]), str(d[2]), int(d[3])) for d in dupes],
                )
                return False
        async with get_session(read_only=False) as session:
            await session.execute(sa_text("SET LOCAL lock_timeout = '3s'"))
            await session.execute(
                sa_text(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS {SIM_ORDER_UNIQUE_INDEX} "
                    "ON sim_orders (tenant_id, user_id, client_order_id) "
                    "WHERE client_order_id IS NOT NULL"
                )
            )
            await session.commit()
        _unique_index_ready = True
        logger.info("[OrderContract] sim_orders 幂等键唯一索引已启用（T-P2-08）")
        return True
    except Exception as exc:  # noqa: BLE001 - 失败不阻断（旧语义继续，体检可查）
        logger.warning("[OrderContract] 唯一索引自愈失败（不阻断）: %s", exc)
        return False
