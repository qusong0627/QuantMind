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

import hashlib
import logging
import os
import re
import time
from typing import Any

logger = logging.getLogger(__name__)

SIM_ORDER_COLUMNS = (
    ("client_order_id", "VARCHAR(100)"),
    ("source", "VARCHAR(32)"),
    ("agent", "VARCHAR(64)"),
)

ORDER_COLUMNS = (
    ("price_source", "VARCHAR(64)"),
    ("source", "VARCHAR(32)"),
    ("agent", "VARCHAR(64)"),
    # P1.6 TCA 基准价：**决策时点我们看到的那个价**（LLM 腿 = 决策报价；镜像腿 =
    # 模拟虚拟成交价/强平盘口价）。成交侧的滑点 = 成交价相对这一列算，缺了就把该笔
    # 归入"不可定价"——**不拿成交价倒推假基准**（倒推出来的滑点恒等于我们自己设的
    # 缓冲，读起来像"执行完美"，见 ``backend/shared/exec_cost.py``）。
    # 可空：老单没有这个价（字段上线前）、市价单/风控直插单也没有。
    # 明确的**不填**：前端直单的限价是"我愿意出的价"（那是 ``price`` 的语义，衡量
    # 口径是 ``cushion_used_bps``），不是市场参考价——填进去等于换了口径。
    ("ref_price", "DOUBLE PRECISION"),
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

#: ``agent`` 列宽（P2.7 分账）。别名而不是每处写 ``VARCHAR(64)``：
#: 三个写入点（契约列、两个模型）必须同宽，改一处漏一处会在入库时被 PG 静默截断。
AGENT_LEN = 64


def normalize_agent(value: Any) -> str:
    """agent 名 → 入库形态（去空白 + 按列宽截断）；空 → 空串。

    **截断口径必须唯一**：``llm_decision`` 腿的 agent 名来自 env 里的模型名
    （``deepseek-v4-flash`` 这种），多个写入点各自 ``[:64]`` 迟早会有一个写成
    ``[:32]``，而两处宽度不同 = 同一条腿在两张表里是两个 agent，对账时查不出原因。
    同一个名字还是四处共用的**键**：幂等键的 agent 段、分账账本的分段名、
    ``sim_orders.agent``、``orders.agent``——差一个字符就是「账本记在 A 名下、
    订单挂在 B 名下」的账。

    生产链在**身份**产生处就该调它（决策轮取 ``binding.model`` 时、派发器读队列
    载荷时、提交段组 ``SimOrderCreate`` 时）；下游 schema 的 ``max_length`` 是兜底
    而不是唯一防线：它**抛校验错**（整笔单发不出去），而这里只是截断。
    给模型厂商看的 id（API 调用参数）**不要**过这里——截了就不是同一把模型。
    """
    return str(value or "").strip()[:AGENT_LEN]


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
    的去重口径）。长度预算：4+24+1+15+1+9+1+4 = 59 < 100。
    """
    rid = "".join(ch for ch in str(round_id or "") if ch.isalnum())[:24]
    sym = str(symbol or "").strip().upper()
    sd = str(side or "").strip().lower()
    if not rid or not sym or not sd:
        return None
    ag = _agent_segment(agent)
    segments = ["lld", rid] + ([ag] if ag else []) + [sym, sd]
    return "-".join(segments)[:MAX_CLIENT_ORDER_ID_LEN]


def _agent_segment(agent: Any) -> str:
    """agent 名 → 幂等键里的一段（≤15 字符，**不同 agent 必须不同段**）。

    前 8 个字符 + 6 位 SHA1 尾巴。为什么不直接用 ``[:8]``：``deepseek-v4-flash`` 与
    ``deepseek-v4-pro`` 的前 8 个字符**一模一样**（都是 ``deepseek``），同轮同标的
    同方向上两条腿会算出一个键，第二条被 ``uq_sim_orders_scope_client_order_id``
    当重复单丢掉——多模型分账刚落地就会被自己的幂等键吃掉一半腿，而台账上只留一行
    ``duplicate``。截断本身没问题，截断后**没有区分度**才是问题。

    名字 ≤8 字符时**不加尾巴**（``flash``/``pro`` 与历史键逐字一致）：既有台账里
    短名的键不变，长名的键从此带上尾巴——两族键不会互相碰撞（前者更短）。
    哈希取 SHA1 前 6 位十六进制（16^6 ≈ 1.7e7）：同一轮的 agent 数以十计，
    碰撞概率可忽略；它**不是**安全边界，只是区分码。
    """
    raw = "".join(ch for ch in str(agent or "") if ch.isalnum())
    if not raw:
        return ""
    if len(raw) <= 8:
        return raw
    return f"{raw[:8]}-{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:6]}"


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


# ── orders(REAL) 幂等键唯一索引（P2.7-⑧）────────────────────────────
#
# sim_orders 的唯一性是 (tenant_id, user_id, client_order_id) 限定的（T-P2-08），
# 而 orders 至今是**全库唯一** `orders_client_order_id_key`（2026-09-24 实测 dev 库
# pg_constraint），与查重/落账口径（派发层两处查询都按租户+用户限定）**不一致**：
# 跨租户同键 → INSERT 撞全局唯一 → ``except IntegrityError`` 兜底再按 (租户,用户)
# 反查**查不到**冲突行（它属于别家）→ ``raise`` → HTTP 500，**真单发不出去**。
# 这不是概率问题：``lld-*`` 的 round 段是 ``rnd-{日期}-{槽位}``，不含租户，两个租户
# 在同一决策槽**必然**算出同一个键（多租户部署 = OSS 的常态）。

ORDER_SCOPE_UNIQUE_INDEX = "uq_orders_scope_client_order_id"

#: 要被取代的旧形态：全库唯一约束（用 pg_get_constraintdef 精确匹配，不按名字猜）
_LEGACY_CID_CONSTRAINT_DEF = "UNIQUE (client_order_id)"

_order_scope_index_ready: bool | None = None

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


async def ensure_real_order_scope_unique_index_async() -> bool:
    """幂等把 orders 的幂等键唯一性收敛到 ``(tenant_id, user_id, client_order_id)``。

    顺序**先建后删**：CREATE 失败时不碰全局约束（旧语义=全库唯一继续拦着，业务只是
    维持现状），绝不出现「两边都没有」的窗口——台账一旦没有唯一性，同一 cid 能落两行，
    幂等反查就会拿到任意一行。

    存量重复（同租户同用户同键）存在时**什么都不做**并 ERROR 点名：删了全局约束又建不成
    限定索引，等于把唯一性整个拿掉，比重复本身更危险（自动删金融行更不可接受）。
    与列迁移/sim 索引同款三纪律：零 DDL 快路径 → 仅真变更才 DDL（lock_timeout=3s）→
    异常只告警不抛出（不阻断启动/写入）。
    """
    global _order_scope_index_ready
    if _order_scope_index_ready:
        return True
    from sqlalchemy import text as sa_text

    from backend.shared.database_manager_v2 import get_session

    try:
        async with get_session(read_only=True) as session:
            indexed_row = await session.execute(
                sa_text("SELECT 1 FROM pg_indexes WHERE indexname = :n LIMIT 1"),
                {"n": ORDER_SCOPE_UNIQUE_INDEX},
            )
            indexed = indexed_row.fetchone() is not None
            legacy = [
                str(row[0])
                for row in (
                    await session.execute(
                        sa_text(
                            "SELECT conname FROM pg_constraint "
                            "WHERE conrelid = 'orders'::regclass AND contype = 'u' "
                            "AND pg_get_constraintdef(oid) = :d"
                        ),
                        {"d": _LEGACY_CID_CONSTRAINT_DEF},
                    )
                ).fetchall()
                if _IDENT_RE.match(str(row[0]))
            ]
            if indexed and not legacy:
                _order_scope_index_ready = True
                return True
            if not indexed:
                dupes = (
                    await session.execute(
                        sa_text(
                            "SELECT tenant_id, user_id, client_order_id, count(*) AS c "
                            "FROM orders WHERE client_order_id IS NOT NULL "
                            "GROUP BY tenant_id, user_id, client_order_id "
                            "HAVING count(*) > 1 LIMIT 3"
                        )
                    )
                ).fetchall()
                if dupes:
                    logger.error(
                        "[OrderContract] 存量重复真单阻止 orders 限定索引启用"
                        "（需人工处置，不自动删金融行）: %s",
                        [(str(d[0]), str(d[1]), str(d[2]), int(d[3])) for d in dupes],
                    )
                    return False
        async with get_session(read_only=False) as session:
            await session.execute(sa_text("SET LOCAL lock_timeout = '3s'"))
            if not indexed:
                await session.execute(
                    sa_text(
                        f"CREATE UNIQUE INDEX IF NOT EXISTS {ORDER_SCOPE_UNIQUE_INDEX} "
                        "ON orders (tenant_id, user_id, client_order_id) "
                        "WHERE client_order_id IS NOT NULL"
                    )
                )
            for name in legacy:
                await session.execute(
                    sa_text(f'ALTER TABLE orders DROP CONSTRAINT "{name}"')
                )
            await session.commit()
        _order_scope_index_ready = True
        logger.info(
            "[OrderContract] orders 幂等键唯一索引已收敛到租户/账户维度（P2.7-⑧）："
            "新建=%s，删除旧全局约束=%s",
            not indexed,
            legacy or "无",
        )
        return True
    except Exception as exc:  # noqa: BLE001 - 失败不阻断（旧全局唯一继续，业务维持现状）
        logger.warning("[OrderContract] orders 限定索引自愈失败（不阻断）: %s", exc)
        return False
