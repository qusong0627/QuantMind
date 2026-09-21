"""风控网关（T-RC-02）：OrderRouter 内嵌接线的 IO 适配层——唯一判定入口。

架构（与 `docs/风险控制体系_设计方案.md` §五 / 实施细案 T-RC-02 对齐）：
- **判定核心**：`backend/shared/risk/`（纯函数，规则/状态机/fail-closed）；
- **本模块职责**：配置装载（Redis 热读 + 版本号）→ 上下文构建（账户/行情/急停/次数）→
  调用 `RiskGateCore.evaluate` → 决策全量留痕（`qm:risk:decisions` 流 + `qm:risk:metrics` 计数）
  → 按模式放行/拦截；
- **先影子后生效（ADR-0009）**：`shadow=true`（默认）时判定照跑、留痕照记、**不拦单**；
  翻闸 = 一次配置变更（`shadow=false`），全程留版本号；
- **fail-closed**：判定/上下文构建异常或配置不可读 → 拒单（错误如实入决策流与计数）。

配置：`qm:risk:config`（Hash）字段 `enabled`("true") / `shadow`("true") / `version`(int) /
`rules`(JSON：rule_id → params；不在表内的规则不启用，L0 急停/时段 always_on)。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.shared.programmatic_trading_disclosure import log_high_frequency_warning
from backend.shared.risk import RiskContext, RiskGateCore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DirectOrderReq:
    """非 OrderRouter 直连路径的最小订单视图（TDX 桥循环 / QMT 执行端接线用，T-RC-02b）。

    与 OrderRequest 同形（build_context 按 getattr 读取），trading_mode=REAL 时
    账户上下文取 real_account_snapshots 最近快照（而非模拟账户）。
    """

    tenant_id: str
    user_id: int
    symbol: str
    side: str
    quantity: float
    price: float | None = None
    order_type: str = "market"
    trading_mode: str = "REAL"
    source: str = "tdx_bridge"
    remarks: str | None = None
    strategy_id: str = ""
    client_order_id: str = ""


CST = timezone(timedelta(hours=8))
CONFIG_KEY = "qm:risk:config"
DECISIONS_KEY = "qm:risk:decisions:{date}"
METRICS_KEY = "qm:risk:metrics:{date}"
DECISIONS_MAXLEN = 20000

# 初始启用规则（配置初始化用；影子期只留痕不拦单，翻闸前按影子报告校准参数）
DEFAULT_RULES: dict[str, dict[str, Any]] = {
    "l0.clock_drift": {"max_skew_ms": 500.0},
    "l1.available_cash": {},
    "l1.t1_sellable": {},
    "l1.position_cap": {"max_pct": 0.15},
    "l1.daily_loss_limit": {"max_loss_pct": 3.0},
    "l3.max_order_value": {"max_value": 1_000_000.0},
    "l3.price_deviation": {"max_dev": 0.02, "sanity_max_dev": 0.20},
    "l3.stale_quote": {"max_age_s": 5.0},
    "l3.order_frequency": {"max_per_minute": 60},
    "l3.cancel_ratio": {"max_ratio": 0.40, "min_orders": 10},
    "l3.lot_size": {"default_lot": 100, "star_lot": 200},
    "l6.book_invalid": {},
}

_FORCED_EXIT_PREFIXES = (
    "sltp:",
    "flatten:",
    "forced-exit:",
    "forced_exit:",
    "flat-",
    "mir-",
)

_CORE = RiskGateCore()
_quote_client: Any = None  # 远端行情 Redis（懒建；快照读用）


def _client(redis: Any):
    """兼容 RedisClient 包装（.client）与原生 redis 客户端。"""
    return getattr(redis, "client", redis)


def _date_key(template: str) -> str:
    return template.format(date=datetime.now(tz=CST).strftime("%Y%m%d"))


# ── 配置 ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RiskConfig:
    enabled: bool = False
    shadow: bool = True
    version: int = 0
    rules: dict[str, dict[str, Any]] = field(default_factory=dict)


def _as_bool(v: Any, default: bool) -> bool:
    s = str(v if v is not None else "").strip().lower()
    if not s:
        return default
    return s not in {"0", "false", "no", "off"}


def load_config(redis: Any) -> RiskConfig | None:
    """读配置。键不存在 → None（视为未启用，放行并计数 config_missing）；读失败 → 抛（fail-closed）。"""
    raw = _client(redis).hgetall(CONFIG_KEY) or {}
    if not raw:
        return None
    rules: dict[str, dict[str, Any]] = {}
    try:
        rules = json.loads(raw.get("rules") or "{}") or {}
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"qm:risk:config.rules 解析失败: {exc}") from exc
    try:
        version = int(raw.get("version") or 0)
    except (TypeError, ValueError):
        version = 0
    _warn_if_order_rate_reaches_hft(rules)
    return RiskConfig(
        enabled=_as_bool(raw.get("enabled"), False),
        shadow=_as_bool(raw.get("shadow"), True),
        version=version,
        rules=rules,
    )


def _warn_if_order_rate_reaches_hft(rules: dict[str, dict[str, Any]]) -> None:
    """下单频率配置撞上高频认定线时告警。

    只告警、不改配置、不拒绝加载：撞线不违法，但要额外向券商报告并接受更严监管，
    真正的风险是**用户不知道自己已经在那一侧**。判定与阈值见
    `shared/programmatic_trading_disclosure.py`（法规常量的唯一出处）。
    """
    rule = rules.get("l3.order_frequency") or {}
    log_high_frequency_warning(rule.get("max_per_minute"), source=f"redis:{CONFIG_KEY}")


# ── 上下文构建 ───────────────────────────────────────────────────────


def _is_forced_exit(remarks: str | None) -> bool:
    r = str(remarks or "").strip().lower()
    return any(r.startswith(p) for p in _FORCED_EXIT_PREFIXES)


def _quote_snapshot(symbol: str) -> dict[str, Any]:
    """远端行情快照（best-effort；失败返回 {}）。"""
    global _quote_client
    try:
        if _quote_client is None:
            from backend.shared.remote_quote_config import make_sync_client

            _quote_client = make_sync_client()
        if _quote_client is None:
            return {}
        from backend.shared.stock_utils import StockCodeUtil

        prefix = (StockCodeUtil.to_prefix(symbol) or symbol).lower()
        return _quote_client.hgetall(f"market:snapshot:{prefix}") or {}
    except Exception:  # noqa: BLE001
        return {}


def _f(v: Any) -> float | None:
    try:
        x = float(v)
        return x if x == x else None
    except (TypeError, ValueError):
        return None


def _positions_lookup(positions: dict[str, Any], symbol: str) -> dict[str, Any] | None:
    """持仓键容错查找（前缀/后缀/纯数字三形态）。"""
    if not positions:
        return None
    if symbol in positions:
        return positions[symbol]
    from backend.shared.stock_utils import StockCodeUtil

    for form in (StockCodeUtil.to_prefix(symbol), StockCodeUtil.to_suffix(symbol)):
        if form and form in positions:
            return positions[form]
    return None


def _last_close_fallback(symbol: str) -> float | None:
    """最近一根已收盘日线（QuantDB 前复权）——快照缺失时金额类校验的兜底价。

    只用于金额估算（资金/占比/单笔上限）；行情时效由 queued_intent 语义单独裁定。
    单测可 monkeypatch 本函数（同步执行，调用方 to_thread）。
    """
    try:
        from datetime import date as _date, timedelta as _td

        from backend.services.trade.services.sentinel_backfill import (
            INDEX_SYMBOLS,
            _dt_int,
            _hub,
        )

        text = str(symbol or "").strip().upper()
        view = "qdb_index_daily" if text in INDEX_SYMBOLS else "qdb_daily_forward"
        if text not in INDEX_SYMBOLS:
            from backend.shared.stock_utils import StockCodeUtil

            text = StockCodeUtil.to_suffix(text) or text
        start = _date.today() - _td(days=15)
        df = _hub().fetch_series(
            view, text, _dt_int(start), _dt_int(_date.today()), columns=["close"]
        )
        if df is None or df.empty:
            return None
        rows = df.dropna(subset=["close"]).sort_values("dt")
        if rows.empty:
            return None
        close = float(rows.iloc[-1]["close"])
        return close if close > 0 else None
    except Exception:  # noqa: BLE001 - 兜底失败=价格不可得（金额规则 fail-closed）
        return None


async def build_context(
    req: Any, *, db: Any, redis: Any, need_counts: bool = False
) -> RiskContext:
    """OrderRequest → RiskContext（纯读；任何子项失败仅缺省该字段并留痕于 evidence）。"""
    now_ts = time.time()
    side = str(getattr(req, "side", "") or "").strip().upper()
    symbol = str(getattr(req, "symbol", "") or "")
    qty = int(float(getattr(req, "quantity", 0) or 0))
    price = _f(getattr(req, "price", None))
    order_type = str(getattr(req, "order_type", "market") or "market").strip().upper()
    remarks = getattr(req, "remarks", None)
    uid = int(getattr(req, "user_id", 0) or 0)
    tenant = str(getattr(req, "tenant_id", "") or "default")
    trading_mode = str(getattr(req, "trading_mode", "") or "").upper()
    source = str(getattr(req, "source", "") or "")

    # 盘后入队语义（2026-09-18 影子实测修复）：OrderRouter 盘后接单进入 pending 队列，
    # 下一交易时段由派发环节申报——时段/行情时效约束不应在入队时刻拒绝；强平类不适用。
    try:
        from backend.shared.risk.builtin_rules import CN_SESSION_DEFAULT, _hm_ok

        in_window = _hm_ok(datetime.now(tz=CST).strftime("%H:%M"), CN_SESSION_DEFAULT)
    except Exception:  # noqa: BLE001 - 判定失败按在场处理（保守：走 reject 路径）
        in_window = True
    forced_exit = _is_forced_exit(remarks)
    queued_intent = (not in_window) and (not forced_exit) and side in ("BUY", "SELL")

    # 急停（fail-closed：读失败按已急停）
    kill = False
    try:
        from backend.services.live_trading.services.real_mirror_service import (
            kill_switch_on,
        )

        kill = bool(kill_switch_on(redis))
    except Exception:  # noqa: BLE001
        kill = True

    # 行情（快照优先；缺失回落到最近收盘供金额类规则——来源如实标注）
    snap = _quote_snapshot(symbol)
    last_price = _f(snap.get("Now"))
    ts = _f(snap.get("timestamp"))
    quote_age = (now_ts - ts) if ts and ts > 0 else None
    price_source = "snapshot" if (last_price and last_price > 0) else ""
    if not price_source:
        fallback_close = await asyncio.to_thread(_last_close_fallback, symbol)
        if fallback_close:
            last_price = fallback_close
            quote_age = None  # 兜底价无"快照时效"语义（时效规则按 queued_intent 裁定）
            price_source = "fallback_close"

    # 账户快照（best-effort）；REAL=真账户最近快照（TDX/QMT 直连接线），否则模拟账户
    available_cash = total_assets = position_pct = sellable = None
    if trading_mode == "REAL":
        try:
            from sqlalchemy import text as _sql_text

            snap_source = (
                "tdx_bridge"
                if source.startswith("tdx")
                else ("qmt_exec" if source.startswith("qmt") else None)
            )
            sql = (
                "SELECT cash, total_asset, payload_json FROM real_account_snapshots "
                "WHERE tenant_id = :t"
                + (" AND source = :s" if snap_source else "")
                + " ORDER BY snapshot_at DESC LIMIT 1"
            )
            params: dict[str, Any] = {"t": tenant}
            if snap_source:
                params["s"] = snap_source
            row = (await db.execute(_sql_text(sql), params)).fetchone()
            if row:
                available_cash = _f(row[0])
                total_assets = _f(row[1])
                payload = (
                    row[2] if isinstance(row[2], dict) else json.loads(row[2] or "{}")
                )
                from backend.shared.stock_utils import StockCodeUtil

                target = (StockCodeUtil.to_suffix(symbol) or symbol).upper()
                for p in payload.get("positions") or []:
                    if str(p.get("symbol") or "").strip().upper() == target:
                        sellable = int(float(p.get("available_volume") or 0))
                        mv = _f(p.get("market_value"))
                        if total_assets and mv is not None:
                            position_pct = mv / total_assets
                        break
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[RiskGate] 真账户快照读取失败（字段按缺省，fail-closed 语义由规则裁定）: %s",
                exc,
            )
    else:
        try:
            from backend.services.trade_shared.simulation_manager import (
                SimulationAccountManager,
            )

            account = await SimulationAccountManager(redis).get_account(uid, tenant)
            if account:
                available_cash = _f(account.get("cash"))
                total_assets = _f(account.get("total_asset"))
                pos = _positions_lookup(account.get("positions") or {}, symbol)
                if pos:
                    sellable = int(float(pos.get("available_volume") or 0))
                    mv = _f(pos.get("market_value"))
                    if total_assets and mv is not None:
                        position_pct = mv / total_assets
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[RiskGate] 账户快照读取失败（字段按缺省，fail-closed 语义由规则裁定）: %s",
                exc,
            )

    # 次数（仅启用了频率/撤单率规则时才查库）；REAL=orders 真单表，否则 sim_orders
    orders_last_minute = orders_today = cancels_today = 0
    if need_counts and trading_mode == "REAL":
        try:
            from sqlalchemy import text as _sql_text

            # orders.created_at/updated_at 为 naive UTC（写入侧惯例）——参数同口径
            day_start = (
                datetime.now(tz=CST)
                .replace(hour=0, minute=0, second=0, microsecond=0)
                .astimezone(timezone.utc)
                .replace(tzinfo=None)
            )
            minute_ago = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
                seconds=60
            )
            row = (
                await db.execute(
                    _sql_text(
                        "SELECT "
                        " count(*) FILTER (WHERE created_at >= :day) AS today, "
                        " count(*) FILTER (WHERE created_at >= :minute) AS last_min, "
                        " count(*) FILTER (WHERE status = 'cancelled' AND updated_at >= :day) AS cancels "
                        "FROM orders WHERE tenant_id = :t AND user_id = :u "
                        "AND trading_mode::text = 'REAL'"
                    ),
                    {
                        "t": tenant,
                        "u": str(uid),
                        "day": day_start,
                        "minute": minute_ago,
                    },
                )
            ).fetchone()
            if row:
                orders_today = int(row[0] or 0)
                orders_last_minute = int(row[1] or 0)
                cancels_today = int(row[2] or 0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[RiskGate] 真单频率计数查询失败: %s", exc)
    elif need_counts:
        try:
            from sqlalchemy import String, cast, func, select

            from backend.services.simulation.models.order import SimOrder

            day_start = datetime.now(tz=CST).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            base = (
                select(func.count())
                .select_from(SimOrder)
                .where(
                    SimOrder.tenant_id == tenant,
                    cast(SimOrder.user_id, String) == str(uid),
                )
            )
            orders_last_minute = int(
                (
                    await db.execute(
                        base.where(
                            SimOrder.created_at
                            >= datetime.now(timezone.utc) - timedelta(seconds=60)
                        )
                    )
                ).scalar()
                or 0
            )
            orders_today = int(
                (
                    await db.execute(base.where(SimOrder.created_at >= day_start))
                ).scalar()
                or 0
            )
            cancels_today = int(
                (
                    await db.execute(
                        base.where(
                            SimOrder.status == "cancelled",
                            SimOrder.cancelled_at >= day_start,
                        )
                    )
                ).scalar()
                or 0
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[RiskGate] 频率计数查询失败: %s", exc)

    amount = None
    if price is not None and qty:
        amount = price * qty
    elif last_price is not None and qty:
        amount = last_price * qty  # 市价单：以最新价估额（供资金/占比规则）

    return RiskContext(
        market="CN",
        symbol=symbol,
        side=side,
        order_type=order_type,
        price=price,
        quantity=qty,
        amount=amount,
        client_order_id=str(getattr(req, "client_order_id", "") or ""),
        forced_exit=forced_exit,
        strategy_id=str(getattr(req, "strategy_id", "") or ""),
        queued_intent=queued_intent,
        price_source=price_source,
        available_cash=available_cash,
        sellable_volume=sellable,
        total_assets=total_assets,
        position_pct=position_pct,
        last_price=last_price,
        quote_age_s=quote_age,
        orders_last_minute=orders_last_minute,
        orders_today=orders_today,
        cancels_today=cancels_today,
        now_ts=now_ts,
        kill_switch=kill,
    )


# ── 判定与留痕 ───────────────────────────────────────────────────────


async def check_direct_order(
    *,
    tenant_id: str,
    user_id: Any,
    symbol: str,
    side: str,
    quantity: float,
    price: float | None,
    order_type: str = "market",
    source: str = "tdx_bridge",
    remarks: str | None = None,
    redis_client: Any = None,
) -> RiskCheck:
    """直连路径（TDX 滚动/L2/QMT）过闸便捷入口：自建只读会话 + trade Redis。

    fail-closed 纪律与 OrderRouter 内嵌一致：判定异常/闸不可用 → passed=False（拒单），
    调用方应按拒单处理并留痕。影子期（默认）恒放行、判定照记。
    """
    raw_uid = str(user_id if user_id is not None else "").strip()
    uid = int(raw_uid) if raw_uid.isdigit() else 0
    req = DirectOrderReq(
        tenant_id=str(tenant_id or "default"),
        user_id=uid,
        symbol=str(symbol or ""),
        side=str(side or "").lower(),
        quantity=float(quantity or 0),
        price=float(price) if price else None,
        order_type=str(order_type or "market"),
        trading_mode="REAL",
        source=str(source or "tdx_bridge"),
        remarks=remarks,
    )
    if redis_client is None:
        from backend.services.trade_shared.redis_client import (
            get_redis as _get_trade_redis,
        )

        redis_client = _get_trade_redis()
        if getattr(redis_client, "client", None) is None:
            redis_client.connect()
    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=True) as db:
        return await check_order(req, db=db, redis=redis_client)


@dataclass(frozen=True)
class RiskCheck:
    passed: bool
    enforced: bool = False  # True=强制模式下的拦截；False=影子放行
    rule_id: str | None = None
    reason: str = ""
    version: int = 0


@dataclass(frozen=True)
class RiskVerdict:
    """判定全貌（预检用）：比 RiskCheck 多出 `decisions` 全表与原始 verdict 词。

    「会不会被拦」与「被哪几条拦」是两件事：影子期下 `passed` 恒 True，只看它
    永远看不到 `l1.position_cap` 已经超了 —— 推送确认面板要的正是后者。
    """

    passed: bool
    verdict: str  # pass | warn | reject | halt | disabled | error
    enforced: bool = False
    rule_id: str | None = None
    reason: str = ""
    version: int = 0
    shadow: bool = False
    decisions: list[dict[str, Any]] = field(default_factory=list)


_PASS = RiskCheck(passed=True)


def _record(
    redis: Any,
    req: Any,
    *,
    verdict: str,
    enforced: bool,
    version: int,
    decisions: list[dict[str, Any]] | None = None,
    error: str | None = None,
) -> None:
    """决策留痕（best-effort：留痕失败不改变放行/拦截结果，但计数 errors）。"""
    try:
        client = _client(redis)
        pipe = client.pipeline(transaction=False)
        fields = {
            "ts": f"{time.time():.3f}",
            "tenant": str(getattr(req, "tenant_id", "") or "default"),
            "uid": str(getattr(req, "user_id", 0)),
            "symbol": str(getattr(req, "symbol", "") or ""),
            "side": str(getattr(req, "side", "") or ""),
            "qty": str(getattr(req, "quantity", 0)),
            "source": str(getattr(req, "source", "") or ""),
            "verdict": verdict,
            "enforced": "true" if enforced else "false",
            "version": str(version),
        }
        if decisions:
            fields["decisions"] = json.dumps(decisions, ensure_ascii=False)[:2000]
        if error:
            fields["error"] = str(error)[:500]
        pipe.xadd(
            _date_key(DECISIONS_KEY), fields, maxlen=DECISIONS_MAXLEN, approximate=True
        )
        metrics = _date_key(METRICS_KEY)
        pipe.hincrby(metrics, "evaluated", 1)
        if verdict == "reject":
            pipe.hincrby(metrics, "rejected", 1)
            if not enforced:
                pipe.hincrby(metrics, "shadow_rejected", 1)
        elif verdict == "halt":
            pipe.hincrby(metrics, "halted", 1)
        elif verdict == "warn":
            pipe.hincrby(metrics, "warned", 1)
        for d in decisions or []:
            rid = str(d.get("rule_id") or "")
            if rid:
                pipe.hincrby(metrics, f"r:{rid}", 1)
        if error:
            pipe.hincrby(metrics, "errors", 1)
        pipe.expire(metrics, 35 * 86400)
        pipe.execute()
    except Exception as exc:  # noqa: BLE001 - 留痕失败不改判
        logger.warning("[RiskGate] 决策留痕失败: %s", exc)


async def evaluate_order(
    req: Any, *, db: Any, redis: Any, record: bool = True
) -> RiskVerdict:
    """风控判定**唯一实现**。`record=False` 即预检：同一套规则、同一份上下文，但不落留痕。

    预检必须走 `record=False`：`_record` 每次调用都会 `hincrby evaluated`，逐笔预检一次
    10 只候选就等于往当日 metrics 里灌 10 次判定，影子报告会显示「今天拦了 N 单」而
    实际一单未发 —— 那是把「没发生的事」写进了证据。见 `preflight_order`。
    """
    try:
        cfg = load_config(redis)
    except Exception as exc:  # noqa: BLE001 - 配置不可读 = fail-closed
        if record:
            _record(
                redis,
                req,
                verdict="reject",
                enforced=True,
                version=0,
                error=f"config: {exc}",
            )
        return RiskVerdict(
            passed=False,
            verdict="error",
            enforced=True,
            rule_id="l0.config",
            reason=f"风控配置不可读（fail-closed）: {exc}"[:180],
        )
    if cfg is None or not cfg.enabled:
        # 未启用：不判定不拦单（计数一次 disabled，便于运维确认部署状态）
        if record:
            _record(redis, req, verdict="disabled", enforced=False, version=0)
        return RiskVerdict(passed=True, verdict="disabled")

    try:
        need_counts = any(
            k in cfg.rules for k in ("l3.order_frequency", "l3.cancel_ratio")
        )
        ctx = await build_context(req, db=db, redis=redis, need_counts=need_counts)
        verdict = _CORE.evaluate(ctx, cfg.rules, version=cfg.version)
    except Exception as exc:  # noqa: BLE001 - 判定异常 = fail-closed
        if record:
            _record(
                redis,
                req,
                verdict="reject",
                enforced=True,
                version=cfg.version,
                error=f"evaluate: {exc}",
            )
        return RiskVerdict(
            passed=False,
            verdict="error",
            enforced=True,
            rule_id="l0.evaluate",
            reason=f"风控判定异常（fail-closed）: {exc}"[:180],
            version=cfg.version,
        )

    decisions = [
        {
            "rule_id": d.rule_id,
            "level": d.level,
            "action": d.action,
            "reason": d.reason,
            "evidence": dict(d.evidence),
        }
        for d in verdict.decisions
    ]
    if verdict.halt:
        v, primary = "halt", next((d for d in decisions if d["action"] == "HALT"), None)
    elif verdict.rejects:
        v, primary = "reject", decisions[0] if decisions else None
    elif verdict.warns:
        v, primary = "warn", None
    else:
        v, primary = "pass", None

    enforced = (not cfg.shadow) and v in ("reject", "halt")
    if record:
        _record(
            redis,
            req,
            verdict=v,
            enforced=enforced,
            version=cfg.version,
            decisions=decisions,
        )

    if cfg.shadow or v == "pass" or v == "warn":
        return RiskVerdict(
            passed=True,
            verdict=v,
            enforced=False,
            version=cfg.version,
            shadow=bool(cfg.shadow),
            decisions=decisions,
        )
    rule_id = str((primary or {}).get("rule_id") or "risk")
    reason = str((primary or {}).get("reason") or "风控拦截")
    if record:
        logger.warning(
            "[RiskGate] 拒单 %s %s: [%s] %s",
            getattr(req, "side", ""),
            getattr(req, "symbol", ""),
            rule_id,
            reason,
        )
    return RiskVerdict(
        passed=False,
        verdict=v,
        enforced=True,
        rule_id=rule_id,
        reason=reason,
        version=cfg.version,
        shadow=bool(cfg.shadow),
        decisions=decisions,
    )


async def check_order(req: Any, *, db: Any, redis: Any) -> RiskCheck:
    """OrderRouter 内嵌调用点：返回 RiskCheck（passed=False 即拒单）。"""
    v = await evaluate_order(req, db=db, redis=redis, record=True)
    if v.verdict == "disabled":
        return _PASS
    return RiskCheck(
        passed=v.passed,
        enforced=v.enforced,
        rule_id=v.rule_id,
        reason=v.reason,
        version=v.version,
    )


async def preflight_order(req: Any, *, db: Any, redis: Any) -> RiskVerdict:
    """推送前预检：判定照跑、**不落留痕**，并回传 decisions 全表。

    与真实下单共用 `evaluate_order`，所以「预检说会过、下单却被拒」只可能来自
    下单那一刻的上下文变化（时段推移、资金变化），不会是两套口径。
    """
    return await evaluate_order(req, db=db, redis=redis, record=False)
