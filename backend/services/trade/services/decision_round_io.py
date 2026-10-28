"""轮次的生产接线（P2.8）：把 :class:`RoundDeps` 的每个注入点接到本仓的既有实现上。

一条纪律：**本模块不复制任何取数口径**。账户三数选源走 ``real_positions`` 的
源仲裁、账户身份走 ``simulation_account_keys`` 的收口、档位走 ``shared/risk/tiers``、
行情走 ``remote_quote_config``——每一样在别处都已有唯一实现，这里只做「谁是当前
该用的那一个」的拼装。替身测试不需要本模块（``RoundDeps`` 逐项换掉即可）。

Redis 客户端**两套并存**且分工明确（理由见 ``decision_round`` 模块 docstring）：
认领/去重/状态键走本模块的 :func:`native_redis_client`（原生，异常可见），
规则表/档位走 ``trade_shared.deps.get_redis()``（包装，内部自带解包）。

名册（P2.9）：:func:`default_roster` 只在 ``QM_DECISION_LLM_ROSTER`` **显式开了**时
逐家返回，未开返回空 ⇒ 走单家路径、键形与 P2.8 逐字一致（升级零风险，见该函数）。
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

from backend.services.trade.services import decision_round_feeds as feeds
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
    AgentLedgerRead,
    AgentRun,
    ExclusionRead,
    LLMBinding,
    RoundDeps,
    RoundResult,
    RoundSlot,
    as_float,
)
from backend.shared.trading_policy import load_trading_policy
from backend.shared.decision.contract import SCHEMA_INTRADAY

# arena 文件面写侧是**本机独有**的包（与「实盘交易」栏同纪律，见 .gitignore）：
# 公开仓缺它时轮次与端点树都不许变，故缺席即 ``None``（用法见 ``record_round_dialogue``）。
try:  # pragma: no cover - 取决于本机是否有该包
    from backend.services.agent_arena import arena_files
except ImportError:  # pragma: no cover
    arena_files = None

logger = logging.getLogger(__name__)


async def load_account_numbers(tenant_id: str, user_id: str) -> AccountRead:
    """实盘账户额度三数（**一行快照**：cash + market_value + total_asset）。

    选源与选户都走既有唯一实现：``real_positions.snapshot_source_for_broker(
    active_broker_type(strict=True))`` + ``simulation_account_keys.
    ledger_user_id_candidates``。——同一 ``(tenant, user)`` 下 ``tdx_bridge`` 与
    ``qmt_exec`` 是两座互不相交的真实账户（实测规模差 ~25 倍），不带源地「取最新
    一行」等于在两座账户间掷硬币。券商类型**没映射到任何源**时同样不做无源取数：
    那是「不知道该读哪座账」，按 ``ok=False`` 报错让编排层中止本轮（不是猜一行继续）。

    **券商选择读不到时不回退**（``strict=True``）：回退是「没人显式选过」，读失败是
    「不知道有没有人选过」；额度可能来自另一座账户（差 ~25 倍），故按 ``ok=False`` 中止。

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


async def load_agent_ledger(
    tenant_id: str, user_id: str, agent: str
) -> AgentLedgerRead:
    """本 agent 的分账账本段（P2.7）——持仓 + 子账户虚拟现金。

    读失败（DB 不可用、表缺失、agent 名为空）一律 ``ok=False``：调用点据此中止本轮。
    **不 catch 成空账本**——「读不到」被当成「名下没有货」的后果是模型看不见自己的
    持仓、于是永远不卖（该止盈止损的仓位烂在账上），而账面全绿。

    ``known`` 与 ``ok`` 分开：读到账本里查无此 agent 是**正常状态**（这家模型还没在
    这本账里成交过），语义是「名下没有货」而不是「没配分账就全给我」。

    归一：``agent`` 空串直接报错不查库（空 agent 会命中一切 ``agent = ''`` 的行，
    那是别家厂商的历史垃圾行，不是「没有这个模型」）。
    """
    from backend.shared.agent_ledger_store import load_ledger as _load_ledger
    from backend.shared.database_manager_v2 import get_session

    name = str(agent or "").strip()
    if not name:
        return AgentLedgerRead(errors=("agent 名为空：无法定位分账账本段",))
    agent_policy = load_trading_policy().for_agent(name)
    try:
        async with get_session(read_only=True) as session:
            doc = await _load_ledger(
                session, tenant_id=tenant_id, user_id=user_id, agents=[name]
            )
    except Exception as exc:  # noqa: BLE001 读不到 = 账本不可信（调用点 abort）
        return AgentLedgerRead(
            errors=(f"分账账本读取失败：{type(exc).__name__}: {exc}",),
        )
    rec = (doc.get("agents") or {}).get(name)
    if not isinstance(rec, Mapping):
        return AgentLedgerRead(ok=True, known=False, quota=agent_policy.quota)
    cash = as_float(rec.get("virtual_cash"))
    positions = rec.get("positions")
    return AgentLedgerRead(
        ok=True,
        known=True,
        positions=dict(positions) if isinstance(positions, Mapping) else {},
        # 现金为负**原样带回**（不夹到 0）：透支是真实状态，夹掉只会让「为什么一直
        # 不买」变成一个查不出的问题。负值在下游自然是「什么都买不起」。
        virtual_cash=agent_policy.quota if cash is None else float(cash),
        quota=agent_policy.quota,
    )


def load_intraday_regime() -> dict[str, Any] | None:
    """盘中 regime 快照（db0 通用库 ``qm:regime:intraday``，engine 侧 60 秒刷新）。

    键与 ``services/engine/realtime_regime.SNAPSHOT_KEY`` 同字面量（该模块带引擎侧
    heavy import，贸易服务不引它——重复的只有这一个字符串常量）;读侧先例见
    ``advice_generator._load_regime_hint`` / ``routers/copilot``。

    **fail-open 返回 ``None``**（不抛、不拦轮）：市场状态是增益块，服务没开时
    提示词按「本轮不可得」如实降级——与资金面/分账那种「读不到本轮不做」不同，
    模型没有 regime 仍能按池与分数决策。读失败留 warning（链路排障的入口）。
    """
    import redis as _redis_lib

    client = _redis_lib.Redis(
        host=os.getenv("REDIS_HOST") or "redis",
        port=int(os.getenv("REDIS_PORT", "6379")),
        password=os.getenv("REDIS_PASSWORD") or None,
        db=int(os.getenv("REDIS_DB_GENERAL", "0")),
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=5,
    )
    try:
        doc = client.hgetall("qm:regime:intraday")
    except Exception as exc:  # noqa: BLE001 增益块取不到不许拦轮
        logger.warning(
            "[DecisionRound] 盘中 regime 快照读取失败（按不可得降级）: %s", exc
        )
        return None
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
    return dict(doc) if doc else None


async def load_signal_scores(
    tenant_id: str, user_id: str
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """模型分数快照——**委托** ``shared.signal_scores.load_score_snapshot``（唯一实现）。

    本模块不复制任何取数口径（见模块 docstring）：分数表读什么日、取哪一行、
    桶隔离，全在那边一处收口——否则「列表显示 +0.12、决策轮按 −0.03 下单」这类
    自相矛盾迟早出现。``user_id`` 传决策账户：桶隔离按该用户解析生效模型；
    取数失败在那边已吞成 ``(空 map, ok=False + reason)``，本函数**不**再包 try。
    """
    from backend.shared.signal_scores import load_score_snapshot

    return await load_score_snapshot(tenant_id, user_id=user_id)


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


def _binding_for(config: Any) -> LLMBinding:
    """一套已解析的 LLM 配置 → (模型名, 调用器)。构造本身不发请求。"""
    from backend.shared.decision.llm_call import decide_with_retry
    from backend.shared.decision_llm_client import make_caller

    caller = make_caller(config=config)
    return LLMBinding(
        model=config.model,
        decide=lambda prompt, schema: decide_with_retry(caller, prompt, schema=schema),
    )


def _default_llm_binding() -> LLMBinding:
    """env → (模型名, 调用器)。未配置抛 ``LLMNotConfigured``（调用点接住转状态）。"""
    from backend.shared.decision_llm_client import resolve_config

    return _binding_for(resolve_config())


def _run_for(config: Any) -> AgentRun:
    """一套配置 → 名册里的一家（身份 = 归一后的模型名）。"""
    from backend.shared.order_contract import normalize_agent

    return AgentRun(
        agent=normalize_agent(config.model),
        load_llm=lambda: _binding_for(config),
    )


def default_roster() -> tuple[AgentRun, ...]:
    """名册（P2.9）：``QM_DECISION_LLM_ROSTER`` 未开时**返回空**（= 单家路径）。

    返回空而不是「把全局那一套包成一家」：后者会让键形从 ``…:{schema}`` 变成
    ``…:{schema}:{模型名}``，升级当天已跑过的槽在 done 键上查无此人 ⇒ 补跑槽把出过
    决策的槽**再跑一遍真钱**（见 ``slot_keys``）。**只有显式开了名册**才走逐家键。

    ``LLMNotConfigured`` **不接住**：名册写错一项时，编排层要按「一家都跑不动」如实
    记录每一槽的失败原因（那层合成一个抛错的家）；在这里吞掉只剩单家三家变一家，
    正是迁移计划里「迁完只剩一个模型」那条警告的形态。名册串**文件优先**（见实现）。
    """
    from backend.shared.decision_llm_client import (
        ENV_ROSTER,
        effective_env,
        resolve_roster,
    )

    # 文件优先：子进程重启继承 supervisor 启动时的旧环境，只读 ``os.environ``
    # 会让保存过的名册在重启后静默回退（2026-10-19：max_tokens 又回缺省 4000）。
    src = effective_env()
    if not str(src.get(ENV_ROSTER) or "").strip():
        return ()
    return tuple(_run_for(config) for config in resolve_roster(src))


def _load_pool_with_overlay(day: str) -> Any:
    """池产物 + 可选因子概率板叠加（P3，细案 §十一）。

    开关（``consume.OVERLAY_FLAG`` = ``QM_DECISION_BOARD_OVERLAY``，默认关；两处
    字面量由测试锁死）**关态**：与 ``load_pool=load_pool_doc`` 直接接线逐字节一致
    ——不 import 因子板、不读板盘、原对象原样返回。**开态**：只标注 + 稳定重排
    （不新增行、不改 score/rank、不并名单——共振标的不获得新开仓资格，见
    ``factor_board.consume`` 模块 docstring）；板缺失/叠加链任何异常 → 原 doc +
    warning（增益组件永不拦决策轮）。
    """
    from backend.shared.decision_context_source import load_pool_doc
    from backend.shared.env_flags import env_flag

    if not env_flag("QM_DECISION_BOARD_OVERLAY"):
        return load_pool_doc(day)

    doc = load_pool_doc(day)
    if doc is None:
        return None
    try:
        from dataclasses import replace

        from backend.shared.factor_board import consume

        overlay = consume.load_overlay(as_of=day)
        if overlay is None:
            return doc
        if overlay.note:
            logger.warning("[DecisionRound] 板叠加注记：%s", overlay.note)
        return replace(doc, rows=consume.annotate_pool(doc.rows, overlay))
    except Exception as exc:  # noqa: BLE001 — 叠加是增益，坏掉不拦决策轮
        logger.warning("[DecisionRound] 板叠加失败，用原池：%s", exc)
        return doc


def default_round_deps() -> RoundDeps:
    """生产接线（每项都指向既有唯一实现，本模块不复制任何取数口径）。"""
    from backend.services.trade.services.decision_executor import run_round
    from backend.services.trade_shared.deps import get_redis
    from backend.services.live_trading.services import trading_session
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

    def _mirror_ready() -> tuple[bool, str]:
        """真单镜像就绪探测（P2-2）：与镜像链路同一实现，**不复刻判定**。

        决策轮是 CN 市场（``build_records(market="CN")``），就绪口径按 CN 读
        ``broker:selected:CN``；跨市场决策轮落地时这里要跟着市场走。
        """
        from backend.services.live_trading.services import real_mirror_service

        return real_mirror_service.real_trading_ready(get_redis(), "CN")

    def _read_mirror_ctx() -> Mapping[str, Any]:
        """镜像提示词面（2026-10-15 审计 P0-B/P0-C）：闸门约束 + 当日跳过明细。

        与下单侧同一实现（``prompt_mirror_context``），读交易库（``get_redis``=db2，与 ``record_skip`` 写侧同库）；读取失败那边已兜成 ``skips=None``。
        """
        from backend.services.live_trading.services import real_mirror_service

        return real_mirror_service.prompt_mirror_context(get_redis(), "CN")

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
        load_agent_ledger=load_agent_ledger,
        load_pool=_load_pool_with_overlay,
        load_excluded=load_excluded_symbols,
        quote_client=make_sync_client,
        read_snaps=read_snapshots,
        load_tier=lambda: load_tier(get_redis()),
        load_llm=_default_llm_binding,
        roster=default_roster,
        open_db=lambda: get_session(read_only=False),
        run_exec=_run_exec,
        write_watch=_write_watch,
        write_ledger=_write_ledger,
        is_trading_day=_is_trading_day,
        is_trading_time=trading_session.is_trading_time,
        is_continuous_trading=trading_session.is_continuous_trading_time,
        real_enabled=is_real_trading_enabled,
        mirror_ready=_mirror_ready,
        now=now_cn,
        load_trading_policy=load_trading_policy,
        load_regime=load_intraday_regime,
        load_scores=load_signal_scores,
        # Stage 2 第二批：丁1–丁9 取数面 + 丙1 唤醒上下文（口径与降级见 feeds 模块）
        load_news_files=feeds.read_news_files,
        load_risk_files=feeds.read_risk_files,
        read_series_summary=feeds.series_summaries,
        load_l2_snapshot=feeds.l2_snapshots,
        load_decision_rows=feeds.load_decision_rows,
        read_wake_ctx=_read_wake_ctx,
        # 镜像闸门/回注（P0 审计修）：非镜像部署下 active=False → 两块整段空串
        read_mirror_ctx=_read_mirror_ctx,
        load_research_brief=feeds.research_brief,  # 研究总控（前一日夜间综合）
    )


def _read_wake_ctx(round_id: str) -> str:
    """唤醒上下文读口：现造现关的原生客户端（**交易库 db2，与写侧同库**）。

    与写侧 ``wake_tick`` 同库是契约（不同库=卡片读不到且全链无报错）；连不上降级。
    """
    client = None
    try:
        client = native_redis_client()
        return feeds.read_wake_ctx(client, round_id)
    except Exception as exc:  # noqa: BLE001 连不上就当非唤醒轮
        logger.warning("[DecisionRound] 唤醒上下文客户端不可用: %s", exc)
        return ""
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


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
    """状态键：``last`` + ``last:{agent}``（名册下按家）+ ``log``（LPUSH 截断）。

    不带家段的那把保留给既有读者（前端/C07 心跳），语义变成「**最后完成的那一家**」；
    名册下问「pro 那家跑得怎么样」得有一把不会被别家覆盖的键，故另写按家的那把。
    空 ``agent``（单家、名册解析失败那家）**不写** ``last:`` 空段键——空段的键没人
    读得到，只会变成 Redis 里的孤儿。失败只告警不抛。
    """
    payload = json.dumps(
        result.as_status(at=at or datetime.now(CST)), ensure_ascii=False, default=str
    )
    try:
        native.set(LAST_KEY, payload, ex=LAST_TTL_S)
        if result.agent:
            native.set(f"{LAST_KEY}:{result.agent}", payload, ex=LAST_TTL_S)
        native.lpush(LOG_KEY, payload)
        native.ltrim(LOG_KEY, 0, LOG_KEEP - 1)
    except Exception as exc:  # noqa: BLE001 状态写失败不该影响已完成的决策
        logger.warning("[DecisionRound] 状态键写入失败: %s", exc)


def write_stall_check(
    native: Any,
    *,
    day: date,
    rounds: int,
    alerted: bool,
    at: datetime,
    note: str = "",
) -> bool:
    """停滞检查回执（P2-5）：「今天已过最后槽位+宽限后查过一遍」的持久痕。

    值 = 检查结论 JSON（``rounds`` 在册轮次数 / ``alerted`` 是否推了告警 /
    ``note`` 日历附注）。死手检查（celery 侧，跨进程树）只核对**键存在**——
    内容供人查。与状态键同纪律：写失败只返回 False（调用方记日志），
    **绝不抛**——回执是可见性旁路，不许掀翻停滞检查本身。
    """
    from backend.shared.duty_receipts import STALL_CHECK_TTL_SECONDS, stall_check_key

    payload = json.dumps(
        {
            "at": at.isoformat(),
            "day": day.isoformat(),
            "rounds": int(rounds),
            "alerted": bool(alerted),
            "note": str(note or ""),
        },
        ensure_ascii=False,
    )
    try:
        native.set(stall_check_key(day), payload, ex=STALL_CHECK_TTL_SECONDS)
        return True
    except Exception as exc:  # noqa: BLE001 回执写失败不抛（见 docstring）
        logger.warning("[DecisionRound] 停滞检查回执写入失败: %s", exc)
        return False


def record_round_dialogue(
    *,
    agent: str,
    prompt: str,
    attempt: Any,
    now: datetime,
    notes: list[str],
) -> None:
    """⑧b 对话落盘——arena「模型对话」tab 的**写侧**。

    为什么这件事归决策轮
    --------------------
    上游 ``live_hourly_analysis.py`` 是**一个回路**：同一次调用既产出给人看的分析
    文本（落 ``log.jsonl`` → 前端模型对话），又产出给程序执行的决策 JSON（→ 闸门 →
    下单）。迁 QM 时这两半被拆开，决策这半落在决策轮、对话那半落在一个**明令不下单
    的只读脚本**里，两边互不知情——于是真在下单的那轮全过程在 arena 上一个字都看不到
    （前端三个 tab 全停在 2026-09-29 的根因之一）。写侧接回后，一轮 = 一行，
    对话与腿出自同一次调用、天然对得上。

    三条纪律
    --------
    * **在闸门之前调用**（见 ``_run_once_inner`` 的调用点）：模型说了什么与腿发没发
      是两件事。放在执行段之后，执行段一抛异常这轮对话就整条丢失——而「模型当时怎么
      想的」恰恰是复盘最需要的。展示面的可靠性不与真钱链路的成败挂钩。
    * **只记成功的轮**：调用方在 ``attempt`` 失败时已早返回。失败轮不写——补跑槽会再
      来一次，先写一条会在同一槽位留多条半截对话，界面还要展示一条空的助手回复；
      失败本身已由 ``decision_round_alerts`` 走 QQ + 状态键两路可见。
    * **写失败不改轮次状态，但必须留痕**：真钱链路照走；一行日志没人看，所以同一句
      话也进 ``notes`` → 审计行的 ``context_meta.notes``（那一列是要被复盘的）。

    文本取 ``display_text``（= 模型原文全文）而不是 ``attempt.raw``：后者是**审计
    额度**内的文本，被 ``RAW_LIMIT`` 截到 8000 字符。实测历史 2700 条回复里 4.4%
    超限（峰值 13958），拿被截的写盘会静默切掉近 5% 的对话、切口在句子中间。

    ``arena_files`` 是本机独有的包（与「实盘交易」栏同纪律，见 ``.gitignore``）：
    公开仓缺它时**轮次照跑**，只是这一轮不落对话——那种部署里 arena 栏本身也不存在，
    「看得见却没数据」这个面不会出现。范式同 ``services/api/main.py`` 的 try/except 注册。

    ``attempt.truncated``（``finish_reason=length``）时往 ``notes`` 记一条：成功轮里
    本函数是唯一同时拿到 ``attempt`` 与 ``notes`` 的写侧，且截断事实直接关系刚落盘的
    对话质量（尾部被切，前端看起来只是戛然而止）。
    """
    if getattr(attempt, "truncated", False):
        notes.append(
            "供应商输出顶格截断（finish_reason=length）：对话与决策原文尾部可能被切，"
            "建议调大该家 max_tokens"
        )
    if arena_files is None:  # pragma: no cover - 取决于本机是否有该包
        return
    try:
        ok = arena_files.append_round_dialogue(
            agent=agent,
            prompt=prompt,
            response=getattr(attempt, "display_text", "") or "",
            usage=getattr(attempt, "usage", None),
            timestamp=now,
        )
    except Exception as exc:  # noqa: BLE001 写侧任何异常都不许打断真钱回路
        logger.error("[DecisionRound] arena 对话落盘异常: %s", exc, exc_info=True)
        ok = False
    if not ok:
        notes.append("arena 对话落盘未完成（模型对话 tab 本轮不可见，详见日志）")


def record_round_position(
    *,
    agent: str,
    ledger: AgentLedgerRead,
    now: datetime,
    notes: list[str],
) -> None:
    """⑩b 持仓落盘——arena「持仓/净值」文件面的**快照写侧**（甲4）。

    上游 ``tool_trade.py`` 在每笔成交落一行 ``position.jsonl``（``this_action``），
    迁 QM 后这个写者随 quant-Trader 退役，文件冻结在 2026-09-29——前端 8 个读侧
    （``load_position_records``/``compute_equity_series``/``list_agents`` 的 cash …）
    全部对着一个没有写者的文件。本函数把写侧接回来：**每跑成一轮 = 每家一行快照**
    （无 ``this_action``，``load_trades`` 靠这个键区分成交行与快照行）。

    数据源取**本 agent 的分账账本**（``qm_agent_ledger_*``，即 ``ledger`` 那份读）：
    共用账户里桥的持仓是全账户混合口径，只有账本段知道「哪些票是这家的、还剩多少
    虚拟现金」。依赖 TDX 桥链路的分账落账（乙2）先修——否则账本冻结在 10-09，
    写出来的快照也是旧值（写侧不编数：账本说什么就落什么）。

    **只落快照，不落成交行**：真单的成交（价/量/时刻）由桥回询**异步**入账，
    决策时刻还没有它；此刻按委托价补一条 ``this_action`` 就是把「打算成交」写成
    「已经成交」。成交面另有读侧（``live_family.load_real_fills_pg`` 读 PG ``trades``
    ⨝ ``orders``），那里才是逐笔事实。

    纪律同 ``record_round_dialogue``：写失败不改轮次状态（真钱链路照走），但同一句
    话也进 ``notes`` → 审计行 ``context_meta.notes``。``arena_files`` 缺席（公开仓）
    时整段静默跳过——那种部署里 arena 栏本身不存在，每轮记一条只会淹掉真注记。
    """
    if arena_files is None:  # pragma: no cover - 取决于本机是否有该包
        return
    try:
        ok = arena_files.append_position(
            agent=agent,
            positions=arena_files.positions_from_ledger(
                ledger.positions, ledger.virtual_cash
            ),
            when=now,
        )
    except Exception as exc:  # noqa: BLE001 写侧任何异常都不许打断真钱回路
        logger.error("[DecisionRound] arena 持仓落盘异常: %s", exc, exc_info=True)
        ok = False
    if not ok:
        notes.append("arena 持仓落盘未完成（持仓/净值文件面本轮无新行，详见日志）")


def maybe_write_watch(
    *,
    deps: RoundDeps,
    slot: RoundSlot,
    decisions: Sequence[Any],
    agent: str,
    notes: list[str],
    ledger: Any = None,
) -> Any:
    """守护规则（``watch`` → 规则表，整组替换）。

    两条纪律：

    * **rebalance schema 的轮次不碰守护规则**：它的 action 白名单里没有 ``watch``，
      而 ``write_watch_plan`` 是**整组替换**——拿建仓轮的手去写守护层，等于把 09:00
      挂上的止损单全清掉。
    * **本轮一条 ``watch`` 都没有时也不写**：整组替换在空集上会把该 agent 全部规则
      摘掉。零条 watch 更可能是「模型这轮没提守护」而不是「撤销全部守护」——要撤
      也得先有一条明确的规则。这一条是本仓**有意**比隔壁保守的地方（隔壁整组替换
      不问空集）。

    ``ledger``：本 agent 的分账账本 —— 成本价回填 ``entry_price``（已显式带成本
    不覆盖；2026-10-19 601000.SH），同时是持仓闸门：账本里没有的标的拒绝 ``watch``
    （watch 落库是卖出规则，未持仓没有可保护的对象；2026-10-20 688543.SH）。
    ``None`` = 不校验（老调用点）。
    """
    if slot.schema != SCHEMA_INTRADAY:
        return None
    from backend.services.trade.services.decision_round_ledger import (
        entry_prices_from_ledger,
        held_symbols_from_ledger,
    )
    from backend.shared.decision.watch_map import REJECT_NOT_HELD, plan_watch

    held = None if ledger is None else held_symbols_from_ledger(ledger)
    watch_plan = plan_watch(decisions, agent=agent, held_symbols=held)
    entry_prices = entry_prices_from_ledger(ledger)
    not_held = sum(1 for r in watch_plan.rejected if r.reason == REJECT_NOT_HELD)
    if not_held:
        notes.append(
            f"守护规则拒绝 {not_held} 条未持仓标的：watch 只保护现有持仓，不建仓"
        )
    if not getattr(watch_plan, "rules", ()):
        if getattr(watch_plan, "rejected", ()):
            notes.append(
                f"本轮 {len(watch_plan.rejected)} 条 watch 全部未挂上："
                "守护规则表**整组保留**（不写空集）"
            )
        else:
            notes.append("本轮无 watch 决策：守护规则表**整组保留**（不写空集）")
        return None
    if entry_prices:
        filled = 0
        for w in watch_plan.rules:
            if w.rule.get("entry_price") is not None:
                continue
            cost = as_float(entry_prices.get(str(w.symbol or "")))
            if cost is None or cost <= 0:
                continue
            w.rule["entry_price"] = round(float(cost), 3)
            filled += 1
        if filled:
            notes.append(f"守护规则回填分账成本价 {filled} 条（来源：agent 账本）")
    try:
        result = deps.write_watch(agent, watch_plan)
    except Exception as exc:  # noqa: BLE001 守护段失败不拖垮买卖段
        logger.error("[DecisionRound] 守护规则写入异常: %s", exc, exc_info=True)
        notes.append(f"守护规则写入异常：{type(exc).__name__}: {exc}")
        return None
    if not getattr(result, "ok", False):
        # 只写日志是不够的：``watch_armed`` 只是**少了一个数**，运营读状态键时看不出
        # 「少的那条是没挂上还是本轮就没提」。P2.4 的 ok 定义是
        # ``not (errors or unverified or problems)``——写被静默丢弃恰恰不以 errors
        # 的形式出现（unverified/problems），所以这三项都要进 notes 留痕。
        detail = (
            f"errors={list(getattr(result, 'errors', ()) or ())} "
            f"unverified={list(getattr(result, 'unverified', ()) or ())} "
            f"problems={list(getattr(result, 'problems', ()) or ())}"
        )
        logger.warning("[DecisionRound] 守护规则未全部落库：%s", detail)
        notes.append(f"守护规则未全部落库：{detail}")
    return result
