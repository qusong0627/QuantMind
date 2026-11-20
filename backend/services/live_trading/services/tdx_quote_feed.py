"""TDX 实时行情 Feed — 把通达信桥实时快照写入 Redis 行情时序 + 持仓股实时提醒 + tick 落库。

链路：Windows 桥(tqcenter) get_market_snapshot → Redis db3（market:snapshot / market:series）
      → stream 服务 QuotePusher 2s 循环读取 → WebSocket 推送前端 / Data Feed 新鲜度检查转绿。

同时基于实时价对【仅持仓股】做提醒（不发自动委托，只推送）：
  - 止损/止盈/移动止损：现价触发阈值 → 站内通知 + 通达信预警（TDX 弹窗）
  - 策略提醒：持仓股最新推理 fusion_score ≤ 滚动阈值 → 卖出提醒（每个新 run 只提醒一次）

tick 持久化：每 3s 快照落 PG（tdx_position_ticks），按持仓生命周期维护会话
（tdx_position_sessions）：持仓出现 → 开会话开始记 tick；清仓 → 闭会话停止记录。
"""

import asyncio
import json
import logging
import os
import time
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from backend.shared.simulation_account_keys import resolve_db_account_user
from backend.shared.stock_display_name import identity as display_identity
from backend.shared.stock_utils import StockCodeUtil
from backend.services.live_trading.services.tdx_push_service import tdx_pusher

logger = logging.getLogger(__name__)

TZ = ZoneInfo("Asia/Shanghai")

# A股交易时段（集合竞价含在内）；午休不拉取
TRADING_SESSIONS = ((9, 15, 11, 35), (12, 55, 15, 5))
# 与桥 get_market_snapshot 3s 内存缓存对齐的轮询周期
POLL_INTERVAL = 3.0
# 持仓列表刷新周期（桥 account/query 一次）
POSITIONS_REFRESH_SECONDS = 30.0
# 非交易时段探测周期（只需低频检查是否开盘）
OFF_HOURS_SLEEP = 30.0

SNAPSHOT_TTL = 300  # 快照 Hash TTL（与行情快照写入规范一致）
SERIES_TTL = 172800  # 时序 ZSET TTL（stream 侧一致）
SERIES_MAX_POINTS = 6000  # 时序保留点数（stream 侧一致）

ALERT_COOLDOWN_SECONDS = 300.0  # 同标的同类型提醒冷却，防刷屏

SLTP_CONFIG_KEY = "trade:sltp_config:{tenant_id}:{user_id}"
LAST_RUN_KEY = "trade:tdx_feed:last_run:{tenant_id}:{user_id}"
ALERT_MARK_KEY = "trade:tdx_feed:alert:{tenant_id}:{user_id}:{symbol}"

DEFAULT_STOP_LOSS_PCT = 0.08  # 兜底止损幅度（对应 execution_config.stop_loss=-0.08）

# tick 落库：批量刷新周期（每周期合并一次 PG 写入）
TICK_FLUSH_SECONDS = 10.0
TICK_INSERT_BATCH = 500

# 持仓会话（tick 归属）：进程内缓存 + PG tdx_position_sessions 持久化（重启可恢复）
_tick_sessions: dict[str, str] = {}  # symbol(prefix) -> session_id
_tick_buffer: list[dict] = []  # 待落库的 tick 行
_tick_tables_ready = False

# Feed 运行状态（供 GET /tdx/quote-feed/status 读取）
feed_status: dict = {
    "running": False,
    "bridge_ok": False,
    "last_feed_at": None,  # ISO 时间
    "last_feed_age_sec": None,
    "symbols": [],  # 当前监控的持仓 symbol（prefix 格式）
    "quote_points_written": 0,
    "sltp_alerts_fired": 0,
    "strategy_alerts_fired": 0,
    "ticks_saved": 0,  # 已落库 tick 行数
    "active_sessions": 0,
    "last_error": None,
    "rate_limited": False,  # 桥限流避让中（60次/分钟）
    "member_gate": {"enabled": True, "allowed": True, "checked_at": None},
    # 派发前 on-demand 刷新（2026-10-12 决策轮 stale_quote 修复）：观察面——
    # 「刷新到底跑了没有、成了几只」在 /tdx/quote-feed/status 一眼可见。
    "on_demand_calls": 0,
    "on_demand_ok": 0,
    "on_demand_last_at": None,
    "on_demand_last_error": None,
}


def _now_sh() -> datetime:
    """当前 Asia/Shanghai 时间（无依赖，纯函数便于测试）。"""
    return datetime.now(TZ)


def is_trading_time(now: datetime | None = None) -> bool:
    """是否为 A 股交易时段（周一至周五 09:15-11:35 / 12:55-15:05）。

    午休（11:35-12:55）与周末/节假日（不判断节假日，节假日本身无行情写入无害）返回 False。
    """
    now = now or _now_sh()
    if now.weekday() >= 5:
        return False
    hm = (now.hour, now.minute)
    return any((sh, sm) <= hm < (eh, em) for sh, sm, eh, em in TRADING_SESSIONS)


def map_snapshot(result: dict) -> dict:
    """通达信 get_market_snapshot 原始字段 → 行情快照规范字段（纯函数）。

    TDX 字段：Now/Open/Max/Min/LastClose/Volume/Amount → Now/Open/High/Low/PreClose/Volume/Amount。
    返回带 timestamp 的字典；关键字段缺失时返回 None。
    """
    if not isinstance(result, dict):
        return None

    def _f(v):
        try:
            x = float(v)
            return x if x == x else None  # 过滤 NaN
        except (TypeError, ValueError):
            return None

    now_price = _f(result.get("Now"))
    open_price = _f(result.get("Open"))
    pre_close = _f(result.get("LastClose"))
    # 快照必填字段（与 docs/行情快照写入规范.md 一致）
    if now_price is None or open_price is None or pre_close is None:
        return None
    return {
        "Now": now_price,
        "Open": open_price,
        "High": _f(result.get("Max")),
        "Low": _f(result.get("Min")),
        "PreClose": pre_close,
        "Volume": int(_f(result.get("Volume")) or 0),
        "Amount": _f(result.get("Amount")),
        "timestamp": int(time.time()),
    }


def check_sltp_trigger(price: float, entry_price: float, cfg: dict) -> tuple[bool, str]:
    """止损/止盈/移动止损触发判断（T-P2-04：**委托 exit_rules 唯一实现**）。

    cfg: {stop_loss_pct, stop_loss_price, take_profit_pct, take_profit_price,
          trailing_stop_pct, highest_price}
    highest_price 为持仓以来最高价（由调用方维护，只升不降）。
    ``stop_loss_price`` 是绝对价硬止损（P1.3；与 pct 同时给时取更紧的那条）；
    ``take_profit_price`` 是绝对价止盈（P1.3b；与 pct 同时给时取更早触发的那条）。
    **两个绝对价键都是可选的**——不传时行为与历史逐字一致（桥 daemon 现网形态
    不带这两个键）。
    返回 (triggered, reason)——文案与历史口径保持兼容（桥 daemon 与
    sltp_executor 零改动）。
    """
    from backend.shared.exit_rules import ExitRuleSet, PositionState, evaluate_exit

    rules = ExitRuleSet(
        hard_stop_pct=cfg.get("stop_loss_pct"),
        hard_stop_price=cfg.get("stop_loss_price"),
        take_profit_pct=cfg.get("take_profit_pct"),
        take_profit_price=cfg.get("take_profit_price"),
        trailing_stop_pct=cfg.get("trailing_stop_pct"),
    )
    pos = PositionState(
        entry_price=entry_price,
        last_price=price,
        high_water_price=cfg.get("highest_price"),
    )
    decision = evaluate_exit(rules, pos)
    return decision.should_exit, decision.reason


def load_sltp_config(tenant_id: str, user_id: str) -> dict:
    """读取止损止盈配置（Redis），未保存时用默认止损（与 execution_config.stop_loss 对齐）。

    返回 {stop_loss_pct, take_profit_pct, trailing_stop_pct, enabled}。
    """
    cfg = {
        "stop_loss_pct": float(
            os.getenv("TDX_SLTP_STOP_LOSS_PCT", str(DEFAULT_STOP_LOSS_PCT))
        ),
        "take_profit_pct": None,
        "trailing_stop_pct": None,
        "enabled": True,
    }
    try:
        from backend.services.trade_shared.redis_client import get_redis

        saved = get_redis().get(
            SLTP_CONFIG_KEY.format(tenant_id=tenant_id, user_id=user_id)
        )
        if isinstance(saved, dict):
            for key in (
                "stop_loss_pct",
                "take_profit_pct",
                "trailing_stop_pct",
                "enabled",
            ):
                if key in saved and saved[key] is not None:
                    cfg[key] = saved[key]
    except Exception as exc:
        logger.warning("[TdxFeed] 读取止损止盈配置失败，使用默认值: %s", exc)
    return cfg


def save_sltp_config(tenant_id: str, user_id: str, cfg: dict) -> dict:
    """保存止损止盈配置到 Redis（设置页）。"""
    clean = {
        "stop_loss_pct": float(cfg.get("stop_loss_pct") or DEFAULT_STOP_LOSS_PCT),
        "take_profit_pct": float(cfg["take_profit_pct"])
        if cfg.get("take_profit_pct")
        else None,
        "trailing_stop_pct": float(cfg["trailing_stop_pct"])
        if cfg.get("trailing_stop_pct")
        else None,
        "enabled": bool(cfg.get("enabled", True)),
    }
    from backend.services.trade_shared.redis_client import get_redis

    get_redis().set(SLTP_CONFIG_KEY.format(tenant_id=tenant_id, user_id=user_id), clean)
    return clean


def _normalize_prefix(symbol: str) -> str:
    """任意格式（后缀/前缀/小写前缀）→ 规范前缀 SH600036。"""
    try:
        return StockCodeUtil.to_prefix(symbol)
    except Exception:
        return str(symbol).upper()


def _to_suffix(prefix: str) -> str:
    """前缀 SH600036 → 桥需要的 600036.SH。"""
    try:
        return StockCodeUtil.to_suffix(prefix)
    except Exception:
        return prefix


def _series_key(prefix: str) -> str:
    return f"market:series:{prefix}"


def _snapshot_key(prefix: str) -> str:
    return f"market:snapshot:{prefix.lower()}"


async def _pull_positions() -> list[dict]:
    """拉取持仓（仅 volume>0 的）。优先桥实时，失败回退 PG 最近快照。

    返回元素统一为 {symbol(prefix), suffix, name, volume, cost_price}。
    """
    positions: list[dict] = []
    try:
        raw = await tdx_pusher.pull_positions()
        for p in raw:
            code = str(p.get("stock_code") or "").strip()
            volume = int(p.get("total_volume") or p.get("volume") or 0)
            if not code or volume <= 0:
                continue
            prefix = _normalize_prefix(code)
            positions.append(
                {
                    "symbol": prefix,
                    "suffix": _to_suffix(prefix),
                    "name": str(p.get("stock_name") or "").strip(),
                    "volume": volume,
                    "cost_price": float(p.get("cost_price") or 0),
                }
            )
        if positions:
            return positions
    except Exception as exc:
        logger.warning("[TdxFeed] 桥持仓拉取失败，回退 PG 快照: %s", exc)

    # 回退：PG real_account_snapshots 最近快照
    try:
        from sqlalchemy import select
        from backend.shared.database_manager_v2 import get_session
        from backend.services.trade_shared.models.real_account_snapshot import (
            RealAccountSnapshot,
        )

        async with get_session(read_only=True) as db:
            row = (
                await db.execute(
                    select(RealAccountSnapshot.payload_json)
                    .order_by(RealAccountSnapshot.snapshot_at.desc())
                    .limit(1)
                )
            ).first()
        if row and isinstance(row[0], dict):
            for p in row[0].get("positions", []):
                code = str(p.get("symbol") or "").strip()
                volume = int(p.get("volume") or 0)
                if not code or volume <= 0:
                    continue
                prefix = _normalize_prefix(code)
                positions.append(
                    {
                        "symbol": prefix,
                        "suffix": _to_suffix(prefix),
                        "name": str(p.get("name") or "").strip(),
                        "volume": volume,
                        "cost_price": float(p.get("cost_price") or 0),
                    }
                )
    except Exception as exc:
        logger.warning("[TdxFeed] PG 持仓快照读取失败: %s", exc)
    return positions


async def _write_snapshot(prefix: str, snap: dict) -> bool:
    """写 Redis：market:snapshot:{sh600000} Hash + market:series:{SH600000} 时序点。

    与 stream 服务 remote_redis_source 的读写格式完全一致（同步 Redis 放线程池）。
    """

    def _do() -> bool:
        try:
            from backend.services.live_trading.routers.real_trading_utils import (
                _get_stream_series_redis_client,
            )

            client, _, _ = _get_stream_series_redis_client()
            ts = int(snap["timestamp"])
            payload = {
                "symbol": prefix,
                "normalized_symbol": prefix,
                "timestamp": ts,
                "datetime": datetime.fromtimestamp(ts, tz=TZ).astimezone().isoformat(),
                "price": snap["Now"],
                "open": snap["Open"],
                "high": snap.get("High"),
                "low": snap.get("Low"),
                "volume": snap.get("Volume"),
                "amount": snap.get("Amount"),
                "is_stale": False,
                "source": "tdx_bridge",
            }
            pipe = client.pipeline(transaction=False)
            pipe.hset(
                _snapshot_key(prefix),
                mapping={
                    **snap,
                    "source": "tdx_bridge",
                },
            )
            pipe.expire(_snapshot_key(prefix), SNAPSHOT_TTL)
            pipe.zadd(
                _series_key(prefix), {json.dumps(payload, ensure_ascii=False): ts}
            )
            pipe.zremrangebyrank(_series_key(prefix), 0, -(SERIES_MAX_POINTS + 1))
            pipe.expire(_series_key(prefix), SERIES_TTL)
            pipe.execute()
            return True
        except Exception as exc:
            logger.warning("[TdxFeed] 行情写入失败 %s: %s", prefix, exc)
            return False

    return await asyncio.to_thread(_do)


#: 派发前 on-demand 刷新的单次上限与节流：桥 rate_limit 600/min 与持仓馈送
#: （~40/min）/热集轮转/账户同步共用——决策轮腿数通常 <30，逐只直查（2026-10-12
#: 实测 21–56ms/只）+ 0.05s 间隔，22 腿整批 ≈1.8s，突发不挤爆预算。
ON_DEMAND_MAX_SYMBOLS = 50
ON_DEMAND_PACING_S = 0.05
#: 时延护栏：httpx 默认 10s/请求且无重试——桥活着但通达信客户端挂起时，
#: 没有这两道闸就是 50 只 × 10s = 500s 把派发卡死。单只 2s、整批 8s 封顶，
#: 超了跳过剩余（best-effort 语义：刷不到就按老数据走闸门）。
ON_DEMAND_CALL_TIMEOUT_S = 2.0
ON_DEMAND_TOTAL_BUDGET_S = 8.0


async def refresh_symbols_now(symbols: Iterable[str]) -> dict[str, bool]:
    """按需直取桥快照并写标准键（派发前调用）——**best-effort，绝不抛出**。

    为什么存在（2026-10-12 决策轮 22/22 拒 ``l3.stale_quote`` 的修复面）：热集
    轮转 ~0.18s/只 × 500+ 只 ≈ **100s 才刷新同一只票一次**，而风控时效门
    ``max_age_s=5``——池票在派发时刻的快照年龄期望 ≈50s，``risk_gate_service.
    _quote_snapshot`` 读到即拒（「陈旧」=有数据但老的变体）。持仓票没这个问题：
    持仓馈送按 ~3s 周期直查（手动单因此总能成交）。本函数把同一份直查搬到
    **决策腿提交前**：对这批标的现查现写 ``market:snapshot/series`` 标准键，
    让风控读到的就是真实新鲜行情，而不是等缓存轮转到。

    边界（与「谁负责什么」的分工一致）：

    * **只刷新数据，不改判定**——桥不可达/字段缺失/限流时**原样放行后续提交**，
      闸门照旧按已有快照裁定（l3.stale_quote 的 fail-closed 语义不动）；
    * **不喂执行计划**——``plan_orders`` 用的仍是轮次开始读的那份快照
      （``decision_executor`` 模块头纪律 1：执行段不重读行情）；本函数影响的
      只是风控在**提交时刻**读到的行情新鲜度；
    * 队列去重（同票多腿只查一次）、上限 ``ON_DEMAND_MAX_SYMBOLS`` 截断；
    * 逐只失败静默计数——一只查不到不该挡整批派发。

    返回 ``{suffix 式代码: 是否写入成功}``（供调用方留痕/测试断言）。
    """
    if not tdx_pusher.enabled:
        # OSS 默认无桥：这里一次日志都不该刷（每轮双腿数条 warning = 纯噪声），
        # 且立即返回——桥都没配，闸门本来就有既有快照的裁定口径。
        feed_status["on_demand_last_error"] = "桥未配置（TDX_BRIDGE_URL/TOKEN）"
        return {}

    wanted: list[str] = []
    seen: set[str] = set()
    for raw in symbols or ():
        code = str(raw or "").strip()
        if not code:
            continue
        suffix = StockCodeUtil.to_suffix(code) or code
        if suffix in seen:
            continue
        seen.add(suffix)
        wanted.append(suffix)
        if len(wanted) >= ON_DEMAND_MAX_SYMBOLS:
            break

    result: dict[str, bool] = {}
    last_idx = len(wanted) - 1
    deadline = time.monotonic() + ON_DEMAND_TOTAL_BUDGET_S
    for idx, suffix in enumerate(wanted):
        if time.monotonic() >= deadline:
            feed_status["on_demand_last_error"] = (
                f"总预算 {ON_DEMAND_TOTAL_BUDGET_S:.0f}s 用尽，跳过剩余 {last_idx - idx + 1} 只"
            )
            break
        ok = False
        try:
            raw_snap = await asyncio.wait_for(
                tdx_pusher.tdx_call("get_market_snapshot", {"stock_code": suffix}),
                timeout=ON_DEMAND_CALL_TIMEOUT_S,
            )
            snap = map_snapshot(raw_snap if isinstance(raw_snap, dict) else {})
            if snap is not None:
                prefix = StockCodeUtil.to_prefix(suffix) or suffix
                ok = await _write_snapshot(prefix, snap)
            else:
                feed_status["on_demand_last_error"] = (
                    f"{suffix}: 桥快照映射失败（空载荷/字段缺失）"
                )
        except Exception as exc:  # noqa: BLE001 单只失败静默（闸门照旧裁定）
            logger.warning("[TdxFeed] 派发前快照刷新失败 %s: %s", suffix, exc)
            feed_status["on_demand_last_error"] = f"{suffix}: {str(exc)[:120]}"
        result[suffix] = ok
        feed_status["on_demand_calls"] += 1
        if ok:
            feed_status["on_demand_ok"] += 1
        if ON_DEMAND_PACING_S > 0 and idx < last_idx:
            await asyncio.sleep(ON_DEMAND_PACING_S)
    if wanted:
        feed_status["on_demand_last_at"] = _now_sh().isoformat(timespec="seconds")
    return result


def reconcile_sessions(
    held_symbols: set[str],
    current: dict[str, str],
    now: datetime,
    tenant_id: str,
    user_id: str,
) -> tuple[dict[str, str], dict[str, str], list[str], list[str]]:
    """持仓会话调和（纯函数）：返回 (new_sessions, kept, opened_symbols, closed_symbols)。

    持仓里新出现的 symbol → 开会话；不再持有的 symbol → 闭会话。
    当前会话之外的符号（历史持仓）不产生动作。
    """
    new_sessions = {}
    opened, closed = [], []
    for sym in held_symbols - set(current):
        sid = f"tdx:{tenant_id}:{user_id}:{sym}:{now.strftime('%Y%m%d%H%M%S')}"
        new_sessions[sym] = sid
        opened.append(sym)
    kept = {sym: sid for sym, sid in current.items() if sym in held_symbols}
    for sym in set(current) - held_symbols:
        closed.append(sym)
    return new_sessions, kept, opened, closed


def build_tick_row(
    *,
    tenant_id: str,
    user_id: str,
    symbol: str,
    session_id: str,
    snap: dict,
    now: datetime,
) -> dict:
    """快照 → tick 落库行（纯函数）。"""
    return {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "session_id": session_id,
        "symbol": symbol,
        "tick_time": now,
        "price": snap.get("Now"),
        "open": snap.get("Open"),
        "high": snap.get("High"),
        "low": snap.get("Low"),
        "volume": snap.get("Volume"),
        "amount": snap.get("Amount"),
        "source": "tdx_bridge",
        "is_stale": False,
    }


async def ensure_tdx_quote_tables() -> None:
    """创建 tick/会话表（幂等，进程内只建一次）。"""
    global _tick_tables_ready
    if _tick_tables_ready:
        return
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    async with get_session() as db:
        await db.execute(
            text(
                """
            CREATE TABLE IF NOT EXISTS tdx_position_sessions (
                session_id   VARCHAR(128) PRIMARY KEY,
                tenant_id    VARCHAR(64)  NOT NULL,
                user_id      VARCHAR(64)  NOT NULL,
                symbol       VARCHAR(32)  NOT NULL,
                entry_tick_time TIMESTAMPTZ NOT NULL,
                exit_tick_time  TIMESTAMPTZ,
                status       VARCHAR(16)  NOT NULL DEFAULT 'OPEN',
                created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
            )
        )
        await db.execute(
            text(
                """
            CREATE TABLE IF NOT EXISTS tdx_position_ticks (
                id           BIGSERIAL PRIMARY KEY,
                tenant_id    VARCHAR(64)  NOT NULL,
                user_id      VARCHAR(64)  NOT NULL,
                session_id   VARCHAR(128) NOT NULL,
                symbol       VARCHAR(32)  NOT NULL,
                tick_time    TIMESTAMPTZ  NOT NULL,
                price        NUMERIC(12,4),
                open         NUMERIC(12,4),
                high         NUMERIC(12,4),
                low          NUMERIC(12,4),
                volume       BIGINT,
                amount       NUMERIC(20,4),
                source       VARCHAR(16)  NOT NULL DEFAULT 'tdx_bridge',
                is_stale     BOOLEAN      NOT NULL DEFAULT FALSE,
                created_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
            )
            """
            )
        )
        await db.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_tdx_ticks_session ON tdx_position_ticks(session_id, tick_time)"
            )
        )
        await db.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_tdx_ticks_symbol ON tdx_position_ticks(symbol, tick_time)"
            )
        )
        await db.commit()
    _tick_tables_ready = True
    logger.info("[TdxFeed] tick 表已就绪: tdx_position_ticks / tdx_position_sessions")


async def restore_open_sessions() -> None:
    """启动时从 PG 恢复未关闭会话（容器重启后继续记 tick）。"""
    global _tick_sessions
    try:
        from sqlalchemy import text

        from backend.shared.database_manager_v2 import get_session

        async with get_session(read_only=True) as db:
            rows = (
                (
                    await db.execute(
                        text(
                            """
                        SELECT session_id, symbol FROM tdx_position_sessions
                        WHERE status = 'OPEN'
                        """
                        )
                    )
                )
                .mappings()
                .all()
            )
        _tick_sessions = {str(r["symbol"]): str(r["session_id"]) for r in rows}
        if _tick_sessions:
            logger.info(
                "[TdxFeed] 恢复 %d 个持仓会话: %s",
                len(_tick_sessions),
                list(_tick_sessions),
            )
    except Exception as exc:
        logger.warning("[TdxFeed] 恢复持仓会话失败: %s", exc)


async def _apply_session_changes(
    *,
    tenant_id: str,
    user_id: str,
    held: set[str],
    now: datetime,
) -> None:
    """根据最新持仓清单开会话/闭会话（更新 PG 与进程内状态）。"""
    global _tick_sessions
    new_sessions, kept, opened, closed = reconcile_sessions(
        held,
        _tick_sessions,
        now,
        tenant_id,
        user_id,
    )
    if not opened and not closed:
        _tick_sessions = kept
        return
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    async with get_session() as db:
        if opened:
            await db.execute(
                text(
                    """
                    INSERT INTO tdx_position_sessions
                        (session_id, tenant_id, user_id, symbol, entry_tick_time, status)
                    VALUES (:session_id, :tenant_id, :user_id, :symbol, :entry_tick_time, 'OPEN')
                    """
                ),
                [
                    {
                        "session_id": new_sessions[sym],
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                        "symbol": sym,
                        "entry_tick_time": now,
                    }
                    for sym in opened
                ],
            )
        if closed:
            await db.execute(
                text(
                    """
                    UPDATE tdx_position_sessions
                    SET status = 'CLOSED', exit_tick_time = :exit_tick_time, updated_at = now()
                    WHERE session_id = :session_id
                    """
                ),
                [
                    {"session_id": _tick_sessions[sym], "exit_tick_time": now}
                    for sym in closed
                ],
            )
        await db.commit()
    _tick_sessions = {**kept, **new_sessions}
    if opened:
        logger.info("[TdxFeed] 持仓会话开启 %s: %s", len(opened), opened)
    if closed:
        logger.info("[TdxFeed] 持仓会话关闭 %s: %s", len(closed), closed)
    feed_status["active_sessions"] = len(_tick_sessions)


async def flush_ticks() -> int:
    """把缓冲的 tick 批量写入 PG，返回写入行数（空缓冲返回 0）。"""
    global _tick_buffer
    if not _tick_buffer:
        return 0
    rows = _tick_buffer[:TICK_INSERT_BATCH]
    _tick_buffer = _tick_buffer[TICK_INSERT_BATCH:]
    try:
        from sqlalchemy import text

        from backend.shared.database_manager_v2 import get_session

        async with get_session() as db:
            await db.execute(
                text(
                    """
                    INSERT INTO tdx_position_ticks
                        (tenant_id, user_id, session_id, symbol, tick_time,
                         price, open, high, low, volume, amount, source, is_stale)
                    VALUES
                        (:tenant_id, :user_id, :session_id, :symbol, :tick_time,
                         :price, :open, :high, :low, :volume, :amount, :source, :is_stale)
                    """
                ),
                rows,
            )
            await db.commit()
        feed_status["ticks_saved"] += len(rows)
        return len(rows)
    except Exception as exc:
        # 失败不丢数据：写回缓冲尾部，下次再试
        logger.warning("[TdxFeed] tick 落库失败（%d 行待重试）: %s", len(rows), exc)
        _tick_buffer.extend(rows)
        return 0


def _buffer_tick(*, tenant_id: str, user_id: str, symbol: str, snap: dict) -> None:
    """快照入 tick 缓冲（同步内存，极廉价）。"""
    session_id = _tick_sessions.get(symbol)
    if not session_id:
        return
    _tick_buffer.append(
        build_tick_row(
            tenant_id=tenant_id,
            user_id=user_id,
            symbol=symbol,
            session_id=session_id,
            snap=snap,
            now=datetime.now(timezone.utc),
        )
    )


async def _notify(
    *,
    tenant_id: str,
    user_id: str,
    title: str,
    content: str,
    level: str = "warning",
    push_tdx: list | None = None,
    qq_alert: bool = True,
) -> None:
    """站内通知（前端通知铃铛）+ 可选通达信预警弹窗 + QQ 旁路。失败不阻断。

    ``qq_alert=False``：本面 QQ 走 ``qq_digest`` 批量卡（策略提醒 2026-10-16 拍板）。
    """
    try:
        from backend.shared.notification_publisher import publish_notification_async

        await publish_notification_async(
            user_id=user_id,
            tenant_id=tenant_id,
            title=title[:128],
            content=content[:4000],
            type="trading",
            level=level,
            qq_alert=qq_alert,
        )
    except Exception as exc:
        logger.warning("[TdxFeed] 站内通知失败: %s", exc)
    if push_tdx:
        try:
            await tdx_pusher.push_warnings(push_tdx)
        except Exception as exc:
            logger.warning("[TdxFeed] 通达信预警推送失败: %s", exc)


def _alert_fired(redis_client, tenant_id: str, user_id: str, symbol: str) -> bool:
    """检查/标记提醒冷却（同标的 5 分钟内不重复提醒）。"""
    key = ALERT_MARK_KEY.format(tenant_id=tenant_id, user_id=user_id, symbol=symbol)
    if redis_client.get(key):
        return True
    redis_client.set(key, "1", ttl=int(ALERT_COOLDOWN_SECONDS))
    return False


def format_sltp_alert_text(
    *, symbol: str, name: str, reason: str, entry: float, price: float
) -> tuple[str, str]:
    """止损/止盈提醒 → （标题, 正文）。形如：

    ``⚠️ 止损止盈 · 国轩高科(SZ002074)`` / ``触发止盈 ｜ 成本 ¥41.20 ｜ 现价 ¥43.55``

    纯函数、绝不抛（名字缺了只显示代码，桥持仓 name 为空时不再裸码上标题）。
    """
    ident = display_identity(symbol, hint=name) or str(symbol or "")
    title = f"⚠️ 止损止盈 · {ident}"
    content = f"{reason} ｜ 成本 ¥{float(entry):.2f} ｜ 现价 ¥{float(price):.2f}"
    return title, content


def strategy_alert_decision(
    *,
    score: float | None,
    rank_pct: float | None,
    threshold_mode: str,
    score_threshold: float,
    rank_pct_threshold: float,
) -> tuple[bool, str]:
    """策略提醒判定（纯函数）：返回 (是否提醒, 文案)。

    - rank_pct：缺分位 → 不提醒（数据缺失不是卖出信号）；
    - score：缺分数 → 不提醒（旧配置也改为保守口径，避免 0/None 误卖）；
    - 阈值比较：持仓分位 ≤ 阈值 / 分数 ≤ 阈值。
    """
    if threshold_mode == "rank_pct":
        if not isinstance(rank_pct, (int, float)):
            return False, ""
        if float(rank_pct) > float(rank_pct_threshold):
            return False, ""
        return True, (
            f"最新推理位置靠后（分位 {float(rank_pct):.3f} ≤ "
            f"{float(rank_pct_threshold):.3f}）"
        )
    if not isinstance(score, (int, float)):
        return False, ""
    if float(score) > float(score_threshold):
        return False, ""
    return True, (
        f"最新推理分数 {float(score):.2f} ≤ 阈值 {float(score_threshold):.2f}"
    )


def format_strategy_digest_line(
    *,
    symbol: str,
    name: str,
    score: float | None,
    threshold: float,
    rank_pct: float | None = None,
    threshold_mode: str = "score",
    rank_pct_threshold: float | None = None,
    model_name: str = "",
) -> str:
    """策略提醒 → QQ 摘要单行（``qq_digest`` 批量卡用）。纯函数、绝不抛。

    ``rank_pct`` 模式（新默认）说**位置**（分位），score 模式保留旧文案。
    """
    ident = display_identity(symbol, hint=name) or str(symbol or "")
    if threshold_mode == "rank_pct" and rank_pct is not None:
        cut = float(rank_pct_threshold if rank_pct_threshold is not None else 0.10)
        model = f" ｜ {model_name}" if model_name else ""
        return (
            f"⚠️ {ident} 位置靠后（分位 {float(rank_pct):.3f} ≤ {cut:.3f}）{model}"
        )
    score_text = f"{float(score):.2f}" if score is not None else "—"
    return f"⚠️ {ident} 推理分数 {score_text} ≤ 阈值 {float(threshold):.2f}"


def _enqueue_strategy_qq(
    *,
    tenant_id: str,
    user_id: str,
    symbol: str,
    name: str,
    score: float | None,
    threshold: float,
    rank_pct: float | None = None,
    threshold_mode: str = "score",
    rank_pct_threshold: float | None = None,
    model_name: str = "",
) -> None:
    """本轮策略提醒 → QQ 摘要卡（120s 窗口合并，2026-10-16 拍板）；失败绝不阻断。"""
    try:
        from backend.shared.qq_digest import enqueue

        enqueue(
            digest_key=f"strategy:{tenant_id}:{user_id}",
            header="🔔 策略提醒",
            line=format_strategy_digest_line(
                symbol=symbol,
                name=name,
                score=score,
                threshold=threshold,
                rank_pct=rank_pct,
                threshold_mode=threshold_mode,
                rank_pct_threshold=rank_pct_threshold,
                model_name=model_name,
            ),
            footer="交易台 › 持仓监控",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[TdxFeed] 策略提醒 QQ 摘要入队失败: %s", exc)


async def _check_sltp_alerts(
    *,
    tenant_id: str,
    user_id: str,
    positions: list[dict],
    prices: dict[str, float],
    highest: dict[str, float],
) -> None:
    """持仓股止损/止盈/移动止损提醒（现价触发，仅提醒不下单）。"""
    cfg = load_sltp_config(tenant_id, user_id)
    if not cfg.get("enabled", True):
        return
    try:
        from backend.services.trade_shared.redis_client import get_redis

        redis_client = get_redis()
    except Exception:
        redis_client = None

    # 执行器（真单）已武装的标的不再发「仅提醒」：同一触发条件会同时产生一条
    # 不下单的警告和一张真单，用户看到的是自相矛盾的两条消息。规则优先；
    # 没有规则的持仓仍走本条提醒（它是 auto_arm 开启前的兜底面）。
    covered: set[str] = set()
    if redis_client is not None:
        try:
            from backend.services.live_trading.services import sltp_executor as _sltp

            exec_cfg = _sltp.load_config(redis_client)
            if exec_cfg.get("enabled"):
                covered = {
                    _sltp.normalize_symbol(str(rule.get("symbol") or ""))
                    for rule in (exec_cfg.get("rules") or [])
                    if rule.get("enabled", True)
                }
        except Exception as exc:  # noqa: BLE001 - 提醒面不因执行器配置读失败而中断
            logger.debug("[TdxFeed] 读取执行器规则失败（提醒面继续）: %s", exc)
            covered = set()

    for p in positions:
        price = prices.get(p["symbol"])
        entry = float(p.get("cost_price") or 0)
        if price is None or entry <= 0:
            continue
        if _to_suffix(p["symbol"]) in covered:
            continue
        trigger_cfg = dict(cfg)
        trigger_cfg["highest_price"] = highest.get(p["symbol"], entry)
        triggered, reason = check_sltp_trigger(price, entry, trigger_cfg)
        if not triggered:
            continue
        if redis_client and _alert_fired(redis_client, tenant_id, user_id, p["symbol"]):
            continue
        name = str(p.get("name") or "").strip()
        title, content = format_sltp_alert_text(
            symbol=p["symbol"], name=name, reason=reason, entry=entry, price=price
        )
        logger.warning("[TdxFeed] %s %s: %s", name or p["symbol"], p["symbol"], reason)
        await _notify(
            tenant_id=tenant_id,
            user_id=user_id,
            title=title,
            content=content,
            level="warning",
            push_tdx=[
                {
                    "symbol": p["suffix"],
                    "side": "sell",
                    "price": price,
                    "close": price,
                    "volume": int(p.get("volume") or 0),
                    "reason": f"{reason} 注意持仓风控",
                }
            ],
        )
        feed_status["sltp_alerts_fired"] += 1


async def _check_strategy_alerts(
    *,
    tenant_id: str,
    user_id: str,
    positions: list[dict],
) -> None:
    """策略提醒：持仓股在「信号模型」最新推理里的位置/分数落到阈值下 → 提醒。

    口径与滚动买卖共用 ``load_rolling_settings``：
    - 默认 ``rank_pct``：持仓分位 ≤ 阈值（默认 0.10）才提醒；**缺分位跳过**
      （数据缺失不是卖出信号，2026-11-20 修：旧绝对分阈值 2.2 对当前模型恒真，
      曾把全部持仓报成「分数 0.00 ≤ 2.20」）；
    - 显式 ``score`` 旧配置：保留「分数 ≤ 阈值」文案；缺分数同样跳过。

    信号模型固定为 显式配置 → 策略绑定 → 用户默认模型（``resolve_signal_model``），
    不再取「最后跑完的 run」——多模型并行时那会让同一持仓的结论随机漂移。
    每个新推理 run 检查一次（last_run 存 Redis，避免每 3s 重复查库/重复提醒）。

    站内通知若走 rank 口径，说的是**位置**（分位），不是买卖建议；通达信预警仍按
    sell 侧推送 —— 那是执行面，客户端收的是委托要素。
    """
    held = {p["symbol"]: p for p in positions}
    if not held:
        return
    try:
        from backend.services.trade_shared.redis_client import get_redis
        from backend.services.live_trading.services.tdx_rolling_trade_service import (
            load_rolling_settings,
            resolve_signal_model,
        )

        settings = load_rolling_settings(tenant_id, user_id)
        signal_model = await resolve_signal_model(
            tenant_id, user_id, settings.signal_model_id
        )
        model_label = (
            signal_model.display_name or signal_model.model_id or "未解析默认模型"
        )
        redis_client = get_redis()
        last_key = LAST_RUN_KEY.format(tenant_id=tenant_id, user_id=user_id)
        last_run = redis_client.get(last_key)

        from sqlalchemy import text
        from backend.shared.database_manager_v2 import get_session

        async with get_session(read_only=True) as db:
            run_sql = """
                SELECT run_id, prediction_trade_date::text AS prediction_trade_date
                FROM qm_model_inference_runs
                WHERE tenant_id = :tenant_id
                  AND user_id = :user_id
                  AND status = 'completed'
            """
            run_params: dict = {"tenant_id": tenant_id, "user_id": user_id}
            if signal_model.model_id:
                run_sql += " AND model_id = :model_id"
                run_params["model_id"] = signal_model.model_id
            run_sql += " ORDER BY created_at DESC LIMIT 1"
            row = (
                (await db.execute(text(run_sql), run_params)).mappings().first()
            )
            if not row:
                return
            run_id = str(row.get("run_id") or "")
            if not run_id or run_id == last_run:
                return
            rows = (
                (
                    await db.execute(
                        text(
                            """
                        SELECT symbol, fusion_score, rank_pct
                        FROM engine_signal_scores
                        WHERE run_id = :run_id AND tenant_id = :tenant_id AND user_id = :user_id
                          AND (universe_tag IS NULL OR universe_tag = 'CN')
                        """
                        ),
                        {"run_id": run_id, "tenant_id": tenant_id, "user_id": user_id},
                    )
                )
                .mappings()
                .all()
            )

        redis_client.set(last_key, run_id)
        skipped = 0
        for r in rows:
            symbol = _normalize_prefix(str(r.get("symbol") or ""))
            p = held.get(symbol)
            if p is None:
                continue
            score = r.get("fusion_score")
            rank = r.get("rank_pct")
            should_alert, reason = strategy_alert_decision(
                score=score if isinstance(score, (int, float)) else None,
                rank_pct=rank if isinstance(rank, (int, float)) else None,
                threshold_mode=settings.threshold_mode,
                score_threshold=settings.score_threshold,
                rank_pct_threshold=settings.rank_pct_threshold,
            )
            if not should_alert:
                if (
                    settings.threshold_mode == "rank_pct"
                    and not isinstance(rank, (int, float))
                ) or (
                    settings.threshold_mode != "rank_pct"
                    and not isinstance(score, (int, float))
                ):
                    skipped += 1
                continue
            name = str(p.get("name") or "").strip()
            logger.info(
                "[TdxFeed] 策略分数提醒 %s %s: %s（模型 %s / run %s）",
                name or symbol,
                symbol,
                reason,
                model_label,
                run_id,
            )
            await _notify(
                tenant_id=tenant_id,
                user_id=user_id,
                title=(f"策略提醒 · {display_identity(symbol, hint=name)}"),
                content=(
                    f"{reason} ｜ 模型 {model_label} ｜ run {run_id} ｜ "
                    f"预测日 {row.get('prediction_trade_date') or '--'}"
                ),
                level="warning",
                qq_alert=False,  # QQ 面走 qq_digest 批量卡（2026-10-16 拍板）
                push_tdx=[
                    {
                        "symbol": p["suffix"],
                        "side": "sell",
                        "price": 0,
                        "close": 0,
                        "volume": int(p.get("volume") or 0),
                        "reason": reason,
                    }
                ],
            )
            _enqueue_strategy_qq(
                tenant_id=tenant_id,
                user_id=user_id,
                symbol=symbol,
                name=name,
                score=float(score) if isinstance(score, (int, float)) else None,
                threshold=float(settings.score_threshold),
                rank_pct=float(rank) if isinstance(rank, (int, float)) else None,
                threshold_mode=settings.threshold_mode,
                rank_pct_threshold=settings.rank_pct_threshold,
                model_name=model_label,
            )
            feed_status["strategy_alerts_fired"] += 1
        if skipped:
            logger.info(
                "[TdxFeed] 策略提醒跳过 %d 只无分位/分数的持仓（数据缺失不卖）",
                skipped,
            )
    except Exception as exc:
        logger.warning("[TdxFeed] 策略提醒检查失败: %s", exc)


async def run_tdx_quote_feed_task(interval: float = POLL_INTERVAL):
    """TDX 实时行情 Feed 主循环（trade 服务 lifespan 启动，ENABLE_TDX_PUSH 门控）。

    - 非交易时段：只低频探测，不写行情（Redis 时序保持陈旧 → Data Feed 走 QuantDB 日线兜底）
    - 交易时段：每 3s 拉持仓股实时快照写 Redis + tick 落库 + 触发止损止盈/策略提醒
    - 持仓会话：持仓出现开会话、清仓闭会话，重启后恢复未关闭会话
    """
    if not tdx_pusher.enabled:
        logger.info("[TdxFeed] TDX_BRIDGE_URL/TOKEN 未配置，行情 Feed 跳过")
        return
    tenant_id = "default"
    user_id = resolve_db_account_user("TDX_ACCOUNT_USER_ID")
    try:
        await ensure_tdx_quote_tables()
        await restore_open_sessions()
    except Exception as exc:
        logger.warning(
            "[TdxFeed] tick 表/会话初始化失败（tick 持久化暂不可用）: %s", exc
        )
    feed_status["running"] = True
    feed_status["active_sessions"] = len(_tick_sessions)

    # 会员门控已移除：持仓实时行情对所有已配置 TDX 桥的账户开放。
    feed_status["member_gate"] = {
        "enabled": True,
        "allowed": True,
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    logger.info(
        "[TdxFeed] TDX 实时行情 Feed 启动: bridge=%s interval=%ss",
        tdx_pusher.bridge_url,
        interval,
    )

    positions: list[dict] = []
    highest: dict[str, float] = {}
    last_positions_at = 0.0
    last_flush_at = time.monotonic()
    cycle = 0.0
    while True:
        try:
            now = _now_sh()
            if not is_trading_time(now):
                await asyncio.sleep(OFF_HOURS_SLEEP)
                continue

            # 持仓列表定时刷新（桥 account/query 成本高，30s 一次）
            if time.monotonic() - last_positions_at >= POSITIONS_REFRESH_SECONDS:
                positions = await _pull_positions()
                last_positions_at = time.monotonic()
                feed_status["symbols"] = [p["symbol"] for p in positions]
                # 持仓会话调和：新持仓开会话，清仓闭会话
                await _apply_session_changes(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    held={p["symbol"] for p in positions},
                    now=datetime.now(timezone.utc),
                )
                # 持仓刷新后同步一次策略提醒（新 run 才触发）
                await _check_strategy_alerts(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    positions=positions,
                )

            if not positions:
                await asyncio.sleep(max(1.0, interval))
                continue

            # 桥限流保护：60次/分钟预算。轮询周期按持仓数自适应
            # （每只 1.5s → 速率恒定 ≈40次/分钟，留余量给账户同步/健康检查）
            cycle = max(3.0, min(30.0, len(positions) * 1.5))
            rate_limited = False
            prices: dict[str, float] = {}
            for p in positions:
                try:
                    result = await tdx_pusher.tdx_call(
                        "get_market_snapshot", {"stock_code": p["suffix"]}
                    )
                except Exception as exc:
                    msg = str(exc)
                    if "RATE_LIMITED" in msg:
                        rate_limited = True
                    else:
                        feed_status["last_error"] = f"{p['symbol']}: {msg}"
                    continue
                snap = map_snapshot(result)
                if snap is None:
                    continue
                if await _write_snapshot(p["symbol"], snap):
                    prices[p["symbol"]] = snap["Now"]
                    if snap["Now"] > highest.get(p["symbol"], 0):
                        highest[p["symbol"]] = snap["Now"]
                    feed_status["quote_points_written"] += 1
                    # tick 持久化（仅持仓会话内）
                    _buffer_tick(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        symbol=p["symbol"],
                        snap=snap,
                    )

            if rate_limited:
                # 桥限流：拉长周期避让，不当作桥故障
                feed_status["rate_limited"] = True
                feed_status["bridge_ok"] = False
                await asyncio.sleep(cycle * 2)
                continue
            feed_status["rate_limited"] = False

            if prices:
                feed_status["bridge_ok"] = True
                feed_status["last_error"] = None
                feed_status["last_feed_at"] = _now_sh().isoformat(timespec="seconds")
                feed_status["last_feed_age_sec"] = 0
                await _check_sltp_alerts(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    positions=positions,
                    prices=prices,
                    highest=highest,
                )

            # 周期批量落库（每 10s 一次）
            if time.monotonic() - last_flush_at >= TICK_FLUSH_SECONDS:
                await flush_ticks()
                last_flush_at = time.monotonic()
        except asyncio.CancelledError:
            # 退出前把缓冲 tick 一次性落库，避免丢数据
            try:
                await flush_ticks()
            except Exception:
                pass
            raise
        except Exception as exc:
            feed_status["last_error"] = str(exc)
            logger.warning("[TdxFeed] 行情 Feed 循环异常: %s", exc)
        await asyncio.sleep(cycle if cycle else max(1.0, interval))
