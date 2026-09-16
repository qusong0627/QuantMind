"""持仓退出状态供给（T-P2-04b）：high_water / 开仓日 / hold_days 的唯一取数实现。

背景（2026-09-16 侦察实证）：`exit_rules` 五规则的判定早已是唯一实现，但 live 侧
**没有任何调用方喂状态**——`high_water_price` / `hold_days` 零填充：
trailing_stop 静默退化为按开仓价的固定线、time_stop 永不触发。

取数口径（机构级，宁可如实缺省不编数据）：
- **开仓日**：PG 台账 lots 最早未平行（``open_date``）→ 旧账本回退 sim_trades 最早 BUY
  ``executed_at``（T-P1-04 之前的 Redis-only 持仓两处皆无 → None，规则不可用并在
  评估方按周期**点名**，绝不猜）。
- **持仓最高价**：``max(自开仓日以来的不复权日线 high, 当前价, 持久化值)``——
  - 日线 high 用**不复权**：cost/entry 是成交原价，复权价会污染回撤线；
  - 持久化在 ``simexit:hw:{tenant}:{user}:{market}`` 哈希（``{"hw": x, "d": "YYYY-MM-DD"}``）：
    覆盖无开仓日的旧持仓（增量采样）；**开仓日晚于持久化日期时丢弃持久化值**
    （同标的二次建仓不继承上一轮高点）；
  - 折叠复用 ``sltp_executor.update_highest_price``——高水位语义唯一实现，
    与实盘止损执行器（QMT/TDX）同源。
- **持有交易日**：``backtest_health.trading_days_between``（交易日历唯一实现，左开右闭）。

口径边界（记录在案）：
- 模拟引擎为**日频评估**（trailing 触发用评估时点价，与硬止损同节奏）；实盘侧为
  报价级增量维护——规则同源、采样频率不同；
- 非 CN 市场经各自 hub 的批量日线接口取 high（同构）；取不到即如实降级到
  "持久化值 ∪ 当前价"（仍优于开仓价近似），不报假数。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

logger = logging.getLogger(__name__)

HW_KEY_TEMPLATE = "simexit:hw:{tenant}:{user}:{market}"
ALLOWED_MARKETS = {"CN", "HK", "US", "FUTURES", "CRYPTO"}


def _norm_symbol(symbol: object) -> str:
    """账户键双形态归一（'SH600983' 与 '600036.SH' 混存）→ 后缀式小写无关大写统一。"""
    text = str(symbol or "").strip().upper()
    if not text:
        return ""
    try:
        from backend.shared.stock_utils import StockCodeUtil

        suffix = StockCodeUtil.to_suffix(text)
        if suffix:
            return suffix.upper()
    except Exception:  # noqa: BLE001 - 归一失败用原文（比较两侧同规则即可）
        pass
    return text


def _norm_market(market: object) -> str:
    text = str(market or "CN").strip().upper() or "CN"
    return text if text in ALLOWED_MARKETS else "CN"


def hw_key(tenant_id: str, user_id: str, market: object) -> str:
    # user 归一到 int 形态：引擎（uid="1"）与 reset（"00000001"）两口径收敛到同一键
    raw_user = str(user_id or "").strip()
    user = str(int(raw_user)) if raw_user.isdigit() else raw_user
    return HW_KEY_TEMPLATE.format(
        tenant=str(tenant_id or "default"), user=user, market=_norm_market(market)
    )


def compute_high_water(
    previous: float | None,
    bar_highs: list[float] | None,
    last_price: float | None,
) -> float:
    """高水位折叠（纯函数）：复用实盘执行器的 update_highest_price（只升不降，唯一实现）。"""
    from backend.services.live_trading.services.sltp_executor import update_highest_price

    hw = previous if previous is not None and previous > 0 else None
    for value in (bar_highs or []):
        try:
            v = float(value)
        except (TypeError, ValueError):
            continue
        if v > 0:
            hw = update_highest_price(hw, v)
    if last_price is not None:
        try:
            v = float(last_price)
            if v > 0:
                hw = update_highest_price(hw, v)
        except (TypeError, ValueError):
            pass
    return float(hw or 0.0)


@dataclass(frozen=True)
class SymbolExitState:
    symbol: str  # 归一后缀式
    open_date: date | None
    high_water: float | None
    hold_days: int | None
    source: str  # lots / trades / stored / none（可观测）


async def resolve_open_dates(
    tenant_id: str, user_id: str, symbols: list[str]
) -> dict[str, date]:
    """开仓日解析（lots 最早未平行 → sim_trades 最早 BUY）。键为归一后缀式。"""
    wanted = {_norm_symbol(s) for s in symbols if _norm_symbol(s)}
    if not wanted:
        return {}
    out: dict[str, date] = {}
    try:
        from sqlalchemy import text as sa_text

        from backend.shared.database_manager_v2 import get_session

        # user_id 列类型两表不同（lots=varchar / trades=integer）——统一经 CAST 文本比较，
        # 两种用户口径（原文 "00000001" / 归一 "1"）都进候选（2026-09-16 E2E 实证修正：
        # lots 的 user_id 是 varchar，直传 int 会 asyncpg DataError）
        us_raw = str(user_id or "").strip()
        candidates = [c for c in {us_raw, str(int(us_raw)) if us_raw.isdigit() else us_raw} if c]
        async with get_session(read_only=True) as session:
            lot_rows = (
                await session.execute(
                    sa_text(
                        "SELECT symbol, min(open_date) FROM simulation_position_lots "
                        "WHERE tenant_id = :t AND CAST(user_id AS varchar) = ANY(:us) "
                        "AND quantity_remaining > 0 AND open_date IS NOT NULL "
                        "GROUP BY symbol"
                    ),
                    {"t": tenant_id, "us": candidates},
                )
            ).fetchall()
            for sym, open_date in lot_rows:
                key = _norm_symbol(sym)
                if key in wanted and open_date is not None:
                    out[key] = (
                        open_date.date() if isinstance(open_date, datetime) else open_date
                    )
            trade_rows = (
                await session.execute(
                    sa_text(
                        "SELECT symbol, min(executed_at) FROM sim_trades "
                        "WHERE tenant_id = :t AND CAST(user_id AS varchar) = ANY(:us) "
                        "AND side::text = 'buy' AND executed_at IS NOT NULL "
                        "GROUP BY symbol"
                    ),
                    {"t": tenant_id, "us": candidates},
                )
            ).fetchall()
            for sym, first_at in trade_rows:
                key = _norm_symbol(sym)
                if key in wanted and key not in out and first_at is not None:
                    out[key] = first_at.date() if isinstance(first_at, datetime) else first_at
    except Exception as exc:  # noqa: BLE001 - 台账不可用不阻断（如实缺省 + 调用方点名）
        logger.warning("[ExitState] 开仓日解析失败（按缺省处理）: %s", exc)
    return out


def load_daily_highs_batch(
    market: object,
    open_dates: dict[str, date],
    as_of: date,
) -> dict[str, list[float]]:
    """按各自开仓日分窗取不复权日线 high（一次批量查询；失败/无数据如实空）。"""
    if not open_dates:
        return {}
    start = min(open_dates.values())
    symbols = sorted(open_dates.keys())
    try:
        from backend.services.simulation.services.local_market_data import (
            LocalMarketData,
        )
        from backend.services.simulation.services.market_rules import normalize_market

        hub = LocalMarketData._resolve_hub(normalize_market(market))  # 各市场 hub 同构接口
        # hub 批量接口用市场规范形（CN=后缀式 600036.SH，即 _norm_symbol 的输出）
        candidates = list(symbols)
        df = hub.fetch_daily_kline_batch(candidates, start, as_of, adjust="none")
        if df is None or len(df) == 0:
            return {}
        by_key: dict[str, list[float]] = {}
        sym_col = "symbol" if "symbol" in df.columns else None
        high_col = "high" if "high" in df.columns else None
        date_col = "trade_date" if "trade_date" in df.columns else None
        if not sym_col or not high_col or not date_col:
            return {}
        for rec in df[[sym_col, date_col, high_col]].itertuples(index=False):
            key = _norm_symbol(rec[0])
            open_d = open_dates.get(key)
            try:
                d = rec[1]
                d = d.date() if isinstance(d, datetime) else d
            except Exception:  # noqa: BLE001
                continue
            if open_d is None or d < open_d:
                continue  # 只看建仓以来的 high
            try:
                h = float(rec[2])
            except (TypeError, ValueError):
                continue
            if h > 0:
                by_key.setdefault(key, []).append(h)
        return by_key
    except Exception as exc:  # noqa: BLE001 - 取数失败降级（持久化值 ∪ 当前价）
        logger.warning("[ExitState] 日线 high 批量取数失败（降级持久化值）: %s", exc)
        return {}


def _read_hw_store(redis_like: Any, key: str) -> dict[str, dict[str, Any]]:
    try:
        raw_client = getattr(redis_like, "client", None) or redis_like
        data = raw_client.hgetall(key) or {}
        out: dict[str, dict[str, Any]] = {}
        for field, value in data.items():
            field_s = field.decode() if isinstance(field, bytes) else str(field)
            value_s = value.decode() if isinstance(value, bytes) else str(value)
            try:
                parsed = json.loads(value_s)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(parsed, dict):
                out[field_s] = parsed
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ExitState] 高水位读取失败（按无持久化处理）: %s", exc)
        return {}


def _write_hw_store(redis_like: Any, key: str, updates: dict[str, dict[str, Any]]) -> None:
    if not updates:
        return
    try:
        raw_client = getattr(redis_like, "client", None) or redis_like
        raw_client.hset(
            key,
            mapping={
                sym: json.dumps(val, ensure_ascii=False) for sym, val in updates.items()
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ExitState] 高水位写回失败（下次评估重算）: %s", exc)


async def load_symbol_exit_states(
    *,
    redis_like: Any,
    tenant_id: str,
    user_id: str,
    market: object,
    positions: dict[str, dict[str, Any]],
    last_prices: dict[str, float],
    as_of: date | None = None,
) -> tuple[dict[str, SymbolExitState], list[str]]:
    """一次评估所需的全部持仓状态。

    返回 (states, missing_open_date)：states 键为**归一后缀式**（账户键双形态统一）；
    missing_open_date 为无开仓日历史的持仓（调用方按周期点名，不静默）。
    """
    today = as_of or datetime.now().date()
    keys_by_symbol = {_norm_symbol(sym): sym for sym in positions}
    open_dates = await resolve_open_dates(tenant_id, user_id, list(positions.keys()))
    highs = load_daily_highs_batch(market, open_dates, today)

    store = _read_hw_store(redis_like, hw_key(tenant_id, user_id, market))
    states: dict[str, SymbolExitState] = {}
    missing: list[str] = []
    updates: dict[str, dict[str, Any]] = {}
    for norm, _raw_symbol in keys_by_symbol.items():
        if not norm:
            continue
        open_d = open_dates.get(norm)
        stored = store.get(norm) or {}
        previous: float | None = None
        try:
            previous = float(stored.get("hw")) if stored.get("hw") is not None else None
        except (TypeError, ValueError):
            previous = None
        # 同标的二次建仓：持久化值早于开仓日 → 丢弃（不继承上一轮高点）
        stored_date = str(stored.get("d") or "")
        if open_d is not None and stored_date and stored_date < open_d.isoformat():
            previous = None

        last_price = last_prices.get(norm)
        hw = compute_high_water(previous, highs.get(norm), last_price)
        hold_days: int | None = None
        if open_d is not None:
            from backend.shared.backtest_health import trading_days_between

            hold_days = trading_days_between(open_d.isoformat(), today.isoformat())
        else:
            missing.append(norm)

        if hw > 0:
            updates[norm] = {"hw": round(hw, 4), "d": today.isoformat()}
        source = "lots/trades+bars" if open_d is not None else ("stored" if previous else "none")
        states[norm] = SymbolExitState(
            symbol=norm,
            open_date=open_d,
            high_water=hw if hw > 0 else None,
            hold_days=hold_days,
            source=source,
        )
    _write_hw_store(redis_like, hw_key(tenant_id, user_id, market), updates)
    return states, missing


def clear_high_water(redis_like: Any, tenant_id: str, user_id: str, market: object = "CN") -> int:
    """清市场高水位哈希（reset / OCR 重对齐时调用：防同标的重开后继承旧高点）。"""
    try:
        raw_client = getattr(redis_like, "client", None) or redis_like
        return int(raw_client.delete(hw_key(tenant_id, user_id, market)) or 0)
    except Exception as exc:  # noqa: BLE001 - 清理失败不阻断主流程
        logger.warning("[ExitState] 高水位清理失败（不阻断）: %s", exc)
        return 0
