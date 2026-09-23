"""轮次的生产接线（P2.8）：把 :class:`RoundDeps` 的每个注入点接到本仓的既有实现上。

一条纪律：**本模块不复制任何取数口径**。账户三数选源走 ``real_positions`` 的
源仲裁、账户身份走 ``simulation_account_keys`` 的收口、档位走 ``shared/risk/tiers``、
行情走 ``remote_quote_config``——每一样在别处都已有唯一实现，这里只做「谁是当前
该用的那一个」的拼装。替身测试不需要本模块（``RoundDeps`` 逐项换掉即可）。

Redis 客户端**两套并存**且分工明确（理由见 ``decision_round`` 模块 docstring）：
认领/去重/状态键走本模块的 :func:`native_redis_client`（原生，异常可见），
规则表/档位走 ``trade_shared.deps.get_redis()``（包装，内部自带解包）。
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

from backend.services.trade.services.decision_round_core import (
    CST,
    ENV_ACCOUNT_USER,
    LAST_KEY,
    LAST_TTL_S,
    LOG_KEEP,
    LOG_KEY,
    SLOT_TTL_S,
    TENANT_ID,
    AccountRead,
    ExclusionRead,
    LLMBinding,
    RoundDeps,
    RoundResult,
    as_float,
)

logger = logging.getLogger(__name__)


async def load_account_numbers(tenant_id: str, user_id: str) -> AccountRead:
    """实盘账户额度三数（**一行快照**：cash + market_value + total_asset）。

    选源与选户都走既有唯一实现：``real_positions.snapshot_source_for_broker(
    active_broker_type(strict=True))`` + ``simulation_account_keys.
    ledger_user_id_candidates``。——同一 ``(tenant, user)`` 下 ``tdx_bridge`` 与
    ``qmt_exec`` 是两座互不相交的真实账户（实测规模差 ~25 倍），不带源地「取最新
    一行」等于在两座账户间掷硬币。券商类型**没映射到任何源**时同样不做无源取数：
    那是「不知道该读哪座账」，按 ``ok=False`` 报错让编排层中止本轮（不是猜一行继续）。

    **券商选择读不到时不回退**（``strict=True``）：回退 ``REAL_BROKER_TYPE`` 的语义是
    「没人显式选过」，而读失败是「不知道有没有人选过」——本轮的额度可能因此来自另一座
    账户（规模差 ~25 倍），故按 ``ok=False`` 中止（同「不知道是谁的账」那条纪律）。

    ``cash`` 或 ``market_value`` 取不到 = **账不可信**（``ok=False``）：拿一个编出来的
    数字当可用资金，比不跑这一轮危险得多（模型会据此报出买不起的单）。``total_asset``
    只进对照——额度三数由 cash+market_value 导出，不让第三个数字参与决策。
    """
    from sqlalchemy import bindparam, text as _text

    from backend.shared.database_manager_v2 import get_session
    from backend.shared.real_positions import (
        BrokerSelectionUnreadable,
        active_broker_type,
        snapshot_source_for_broker,
    )
    from backend.shared.simulation_account_keys import ledger_user_id_candidates

    account = str(user_id or "").strip()
    if not account:
        return AccountRead(errors=("账户身份为空：无法读资金面",))

    try:
        broker = active_broker_type(strict=True)
    except BrokerSelectionUnreadable as exc:
        return AccountRead(
            errors=(
                f"券商选择读取失败：{exc}（不知道有没有人选过、选的是谁：无法确定读"
                "哪座账户的资金面，本轮不做）",
            )
        )
    snap_source = snapshot_source_for_broker(broker)
    if not snap_source:
        # 券商类型没映射到任何快照源 ⇒ **不知道该读哪座账户**。此时若照旧不加
        # ``source`` 过滤，SQL 会取「全源最新一行」——两座真实账户规模差 ~25 倍，
        # 等于在两座账户之间掷硬币决定本轮额度。「不知道是谁的账」不许读成
        # 「就取这一行」（同 P2.2 在途账那条纪律），本轮不做。
        return AccountRead(
            errors=(
                f"券商类型 {broker or '(未配置)'} 未映射到快照源（REAL_BROKER_TYPE/"
                "broker:selected:CN）：无法确定读哪座账户的资金面（本轮不做）",
            )
        )
    sql = (
        "SELECT cash, total_asset, market_value, snapshot_at, source "
        "FROM real_account_snapshots "
        "WHERE tenant_id = :t AND user_id IN :u AND source = :s "
        "ORDER BY snapshot_at DESC, id DESC LIMIT 1"
    )
    params: dict[str, Any] = {
        "t": tenant_id,
        "u": ledger_user_id_candidates(account),
        "s": snap_source,
    }
    try:
        async with get_session(read_only=True) as session:
            row = (
                await session.execute(
                    _text(sql).bindparams(bindparam("u", expanding=True)), params
                )
            ).fetchone()
    except Exception as exc:  # noqa: BLE001 读不到 = 账不可信（调用点 abort）
        return AccountRead(errors=(f"账户快照读取失败：{type(exc).__name__}: {exc}",))

    if row is None:
        return AccountRead(
            errors=(
                f"账户快照不存在（tenant={tenant_id} user={account} "
                f"source={snap_source or '(未选源)'}）：资金面不可得",
            )
        )
    cash = as_float(row[0])
    market_value = as_float(row[2])
    errors: list[str] = []
    if cash is None:
        errors.append("账户快照缺 cash 列值")
    if market_value is None:
        errors.append("账户快照缺 market_value 列值")
    snap_at = row[3]
    age_min: float | None = None
    if snap_at is not None:
        from backend.shared.utc_datetime import as_utc, utc_now

        age_min = max(0.0, (utc_now() - as_utc(snap_at)).total_seconds()) / 60.0
    return AccountRead(
        ok=not errors,
        cash=cash,
        market_value=market_value,
        total_asset=as_float(row[1]),
        source=str(row[4] or ""),
        broker=str(broker or ""),
        snapshot_at=(
            snap_at.isoformat() if hasattr(snap_at, "isoformat") else str(snap_at or "")
        ),
        age_min=None if age_min is None else round(age_min, 1),
        errors=tuple(errors),
    )


def load_excluded_symbols() -> ExclusionRead:
    """排除名单（blocking 且未过期）→ 集合 + 留痕文案。

    ``load_exclusion_list`` 返回 ``None`` = 文件不在盘（**不是空名单**）：调用方
    必须显式留下「名单未导入」，不许静默当没有风险股——档位层的先例是「从未配置 =
    ``absent``：不覆盖、不改行为，只告警一次（不是故障）」。
    """
    from backend.shared.exclusion_list import load_exclusion_list

    try:
        doc = load_exclusion_list()
    except Exception as exc:  # noqa: BLE001 名单读不动 ≠ 名单为空
        return ExclusionRead(note=f"排除名单读取失败：{type(exc).__name__}: {exc}")
    if doc is None:
        return ExclusionRead(note="排除名单未导入（文件不在盘）：本轮按空名单跑")
    try:
        symbols = frozenset(doc.symbols())
    except Exception as exc:  # noqa: BLE001
        return ExclusionRead(note=f"排除名单解析失败：{type(exc).__name__}: {exc}")
    return ExclusionRead(
        symbols=symbols, present=True, note=f"排除名单生效 {len(symbols)} 只"
    )


def _default_llm_binding() -> LLMBinding:
    """env → (模型名, 调用器)。未配置抛 ``LLMNotConfigured``（调用点接住转状态）。"""
    from backend.shared.decision.llm_call import decide_with_retry
    from backend.shared.decision_llm_client import make_caller, resolve_config

    config = resolve_config()
    caller = make_caller(config=config)
    return LLMBinding(
        model=config.model,
        decide=lambda prompt, schema: decide_with_retry(caller, prompt, schema=schema),
    )


def default_round_deps() -> RoundDeps:
    """生产接线（每项都指向既有唯一实现，本模块不复制任何取数口径）。"""
    from backend.services.trade.services.decision_executor import run_round
    from backend.services.trade_shared.deps import get_redis
    from backend.services.live_trading.services.trading_session import is_trading_time
    from backend.shared.database_manager_v2 import get_session
    from backend.shared.decision_context_source import (
        load_pool_doc,
        now_cn,
        read_snapshots,
    )
    from backend.shared.decision_ledger_store import upsert_rows
    from backend.shared.decision.watch_writer import write_watch_plan
    from backend.shared.live_trading_gate import is_real_trading_enabled
    from backend.shared.real_positions import load_real_positions
    from backend.shared.remote_quote_config import make_sync_client
    from backend.shared.risk.tiers import load_tier
    from backend.shared.simulation_account_keys import resolve_db_account_user

    async def _run_exec(*, db: Any, **kwargs: Any) -> Any:
        return await run_round(db=db, redis=get_redis(), **kwargs)

    def _write_watch(agent: str, plan: Any) -> Any:
        return write_watch_plan(get_redis(), plan, agent=agent)

    async def _write_ledger(db: Any, records: Sequence[Any]) -> int:
        return await upsert_rows(db, records)

    async def _is_trading_day(day: date) -> bool:
        """交易日闸门：**降级判定一律拒绝**（宁缺勿滥）。

        ``trading_day_verdict`` 的 ``weekday_fallback`` 意味着日历没取到、只按周末判断
        ——中秋/国庆/春节都会被判成交易日，真钱轮次于是照常开盘下单。这里抛出去，让
        编排层走既有的「交易日判定失败 → 本 tick 不跑」分支（日志留痕，不静默）。
        """
        from backend.shared.trading_calendar import (
            SRC_WEEKDAY_FALLBACK,
            TradingCalendarService,
        )

        verdict, source = await TradingCalendarService().trading_day_verdict(
            market="CN",
            trade_date=day,
            tenant_id=TENANT_ID,
            user_id=resolve_db_account_user(ENV_ACCOUNT_USER),
        )
        if source == SRC_WEEKDAY_FALLBACK:
            raise RuntimeError(
                f"交易日历不可用（降级为工作日判断，依据={source}），拒绝在 {day} 判定交易日"
            )
        return verdict

    return RoundDeps(
        account_user=lambda: resolve_db_account_user(ENV_ACCOUNT_USER),
        load_positions=load_real_positions,
        load_account=load_account_numbers,
        load_pool=load_pool_doc,
        load_excluded=load_excluded_symbols,
        quote_client=make_sync_client,
        read_snaps=read_snapshots,
        load_tier=lambda: load_tier(get_redis()),
        load_llm=_default_llm_binding,
        open_db=lambda: get_session(read_only=False),
        run_exec=_run_exec,
        write_watch=_write_watch,
        write_ledger=_write_ledger,
        is_trading_day=_is_trading_day,
        is_trading_time=is_trading_time,
        real_enabled=is_real_trading_enabled,
        now=now_cn,
    )


def native_redis_client() -> Any:
    """原生 redis-py 客户端（**交易库**）——认领/去重/状态专用（见模块 docstring）。"""
    import redis as _redis_lib

    return _redis_lib.Redis(
        host=os.getenv("REDIS_HOST") or "redis",
        port=int(os.getenv("REDIS_PORT", "6379")),
        password=os.getenv("REDIS_PASSWORD") or None,
        db=int(os.getenv("REDIS_DB_TRADE", "2")),
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=5,
    )


#: 认领键的取值。自动轮写 ``auto``；手动重跑写 ``manual`` —— 值本身是**留给下一次
#: 排障的线索**（「这一槽是谁认的」），键的**存在性**才是闸门。
CLAIM_AUTO = "auto"
CLAIM_MANUAL = "manual"


def claim_slot(native: Any, key: str, *, force: bool = False) -> bool:
    """槽位认领（``SET NX EX``）。**异常上抛**：原生客户端的失败必须可见。

    ``force=True``（手动重跑）**拿掉 NX**：已在册的槽位也改写并放行。

    认领键挡的是「同一次到点有两个调用者」，**不是**「今天跑过没」（后者是 done 键，
    由 ``force`` 另行忽略）——而手动重跑要的正是「这一次我再来一遍」。今天的实现若
    只忽略 done 键、认领照旧走 NX，则控制台重跑对**任何跑过一次的槽位**都是空转：
    CLI 打一行 info 就退出，运营看到的是「当前无到点槽位」。所以这里的覆盖写不是
    放宽，是补上另一半语义。

    改写只能拦住**之后**的自动 tick（它们的 NX 依旧撞键），拦不住**正在跑**的那
    一次。真正兜底的是执行段的订单幂等键：同槽同 round_id ⇒ 同标的同方向撞同一个
    ``client_order_id``，重复腿被下游挡住；只有模型这轮给出**新**意图才会产生新单，
    而那正是「重跑」的本意。
    """
    if force:
        return bool(native.set(key, CLAIM_MANUAL, ex=SLOT_TTL_S))
    return bool(native.set(key, CLAIM_AUTO, nx=True, ex=SLOT_TTL_S))


def write_status(
    native: Any, result: RoundResult, *, at: datetime | None = None
) -> None:
    """状态键：``last``（最近一轮）+ ``log``（LPUSH 截断）。失败只告警不抛。"""
    payload = json.dumps(
        result.as_status(at=at or datetime.now(CST)), ensure_ascii=False, default=str
    )
    try:
        native.set(LAST_KEY, payload, ex=LAST_TTL_S)
        native.lpush(LOG_KEY, payload)
        native.ltrim(LOG_KEY, 0, LOG_KEEP - 1)
    except Exception as exc:  # noqa: BLE001 状态写失败不该影响已完成的决策
        logger.warning("[DecisionRound] 状态键写入失败: %s", exc)
