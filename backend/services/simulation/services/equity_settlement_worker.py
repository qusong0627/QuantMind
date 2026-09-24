"""模拟盘持久化权益结算 worker（对账确权 → 行情重估 → 权益持久化）。

背景：旧链路把权益刷新拆在三个互相独立的 worker 上——remark 仅 A 股
交易时段运行、reconcile 每日 03:20 一次、fund snapshot 每 300s 把 Redis
（可能是冻结旧值）持久化进 PG。服务器关机/重启后（尤其盘外）无人刷新
权益，前端资金概览与收益曲线停留在旧值，直到次日对账窗口。

本 worker 合并三者为单一周期任务（默认 30 秒，无交易时段门控，系统
运行期间不间断），并在启动后立即执行首个周期消除重启空窗：

1. 对账确权：run_reconcile_once(autofix) 以 PG 台账为准回填 Redis 的
   现金/持仓量，差异落 simulation_reconcile_reports 留痕；
2. 行情重估：Lua 脚本原子更新 Redis 账户的 price/market_value/
   total_asset——与交易 update_balance Lua 在同一 Redis 单线程上天然
   串行，且不碰 cash/cost/available_volume，消除旧 remark
   GET→改→SET 与交易并发丢更新的竞态；
3. 权益持久化：capture_all 把 Redis 账户 upsert 进
   simulation_fund_snapshots（前端收益曲线当日实时）；UPDATE
   simulation_accounts 的市值/权益字段与 last_projected_at（台账投影
   新鲜度）。

取价链：远端 Redis series tick（新鲜）→ 盘后保留 series 末笔 →
次日 06:00 后才允许本地日线收盘兜底（日线往往凌晨才发布，过早用会
把权益打回 T-1）。

simulation_account_daily 仍由 EOD 日终链路负责（历史日曲线口径），
本 worker 不做盘中 delete+insert 维护，避免 2880 次/天的写放大。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from backend.services.trade_shared.redis_client import RedisClient
from backend.shared.simulation_position_keys import (
    build_position_key,  # noqa: F401  (re-export for callers/tests)
    split_position_key,
)

logger = logging.getLogger(__name__)

_SH_TZ = ZoneInfo("Asia/Shanghai")

# series 末笔盘后保留窗口（秒）：覆盖「收盘→次日 06:00」约 15h，默认 20h
_DEFAULT_OVERNIGHT_SERIES_MAX_AGE_SEC = 20 * 3600


def settle_enabled() -> bool:
    return os.getenv("SIM_EQUITY_SETTLE_ENABLED", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def settle_interval_seconds() -> int:
    try:
        return max(10, int(os.getenv("SIM_EQUITY_SETTLE_INTERVAL_SECONDS", "30")))
    except (TypeError, ValueError):
        return 30


def settle_cycle_timeout_seconds() -> int:
    """Hard cap for one cycle. Sync Redis on the event loop cannot be
    interrupted; callers must offload those calls to a thread first."""
    try:
        return max(10, int(os.getenv("SIM_EQUITY_SETTLE_CYCLE_TIMEOUT_SECONDS", "25")))
    except (TypeError, ValueError):
        return 25


def settle_heartbeat_cycles() -> int:
    try:
        return max(1, int(os.getenv("SIM_EQUITY_SETTLE_HEARTBEAT_CYCLES", "20")))
    except (TypeError, ValueError):
        return 20


def daily_close_fallback_allowed(now: datetime | None = None) -> bool:
    """是否允许用本地日线收盘做权益重估兜底。

    日线数据往往凌晨才发布；盘后～次日 06:00 前若用日线会把市值打回 T-1。
    允许窗口（上海时区，可配）：
      SIM_DAILY_CLOSE_FALLBACK_AFTER_HOUR（默认 6）≤ hour <
      SIM_DAILY_CLOSE_FALLBACK_SESSION_END_HOUR（默认 15）
    """
    try:
        after_hour = int(os.getenv("SIM_DAILY_CLOSE_FALLBACK_AFTER_HOUR", "6") or 6)
    except (TypeError, ValueError):
        after_hour = 6
    try:
        session_end = int(
            os.getenv("SIM_DAILY_CLOSE_FALLBACK_SESSION_END_HOUR", "15") or 15
        )
    except (TypeError, ValueError):
        session_end = 15
    after_hour = max(0, min(23, after_hour))
    session_end = max(after_hour + 1, min(24, session_end))

    if now is None:
        current = datetime.now(_SH_TZ)
    elif now.tzinfo is None:
        current = now.replace(tzinfo=_SH_TZ)
    else:
        current = now.astimezone(_SH_TZ)
    return after_hour <= current.hour < session_end


def overnight_series_max_age_sec() -> int:
    """盘后 series 末笔可用的最大年龄（秒）。"""
    try:
        return max(
            3600,
            int(
                os.getenv(
                    "SIM_OVERNIGHT_SERIES_MAX_AGE_SEC",
                    str(_DEFAULT_OVERNIGHT_SERIES_MAX_AGE_SEC),
                )
                or _DEFAULT_OVERNIGHT_SERIES_MAX_AGE_SEC
            ),
        )
    except (TypeError, ValueError):
        return _DEFAULT_OVERNIGHT_SERIES_MAX_AGE_SEC


# 持仓键 → (代码, 方向)。统一收口到 backend.shared.simulation_position_keys，
# 兼容 SYMBOL / SYMBOL::long / SYMBOL:short 等历史键形（同名再导出）。


# 原子重估：只改 price/market_value 与派生汇总字段，不碰现金/成本/可卖量。
# KEYS[1]=账户键，ARGV[1]=JSON {"持仓键": {"price": p, "market_value": mv}}。
# 空更新也可调用，用于仅重算汇总（market_value/short_market_value/total_asset）。
_REMARK_LUA = """
local key = KEYS[1]
local updates = cjson.decode(ARGV[1])

local raw = redis.call("GET", key)
if not raw then
    return cjson.encode({success=false, reason="ACCOUNT_NOT_FOUND"})
end

local account = cjson.decode(raw)
local positions = account.positions or {}
local changed = 0

for pos_key, u in pairs(updates) do
    local pos = positions[pos_key]
    if pos then
        pos.price = tonumber(u.price)
        pos.last_price = tonumber(u.price)
        pos.market_value = tonumber(u.market_value)
        positions[pos_key] = pos
        changed = changed + 1
    end
end

local long_mv = 0
local short_mv = 0
for pos_key, p in pairs(positions) do
    local mv = tonumber(p.market_value or 0)
    if string.sub(pos_key, -7) == "::short" or string.sub(pos_key, -6) == ":short" then
        short_mv = short_mv + mv
    else
        long_mv = long_mv + mv
    end
end

account.positions = positions
account.market_value = long_mv
account.long_market_value = long_mv
account.short_market_value = short_mv
local short_proceeds = tonumber(account.short_proceeds or 0)
account.total_asset = tonumber(account.cash or 0) + short_proceeds + long_mv - short_mv
account.equity = account.total_asset
account.available_cash = tonumber(account.cash or 0)
account.frozen_cash = 0

redis.call("SET", key, cjson.encode(account))
return cjson.encode({success=true, changed=changed, total_asset=account.total_asset})
"""


def summarize_positions(
    positions: Any,
) -> tuple[float, float, float]:
    """按持仓键方向汇总 (long_mv, short_mv, net)。纯函数，可单测。"""
    long_mv = 0.0
    short_mv = 0.0
    if not isinstance(positions, dict):
        return 0.0, 0.0, 0.0
    for pos_key, pos in positions.items():
        if not isinstance(pos, dict):
            continue
        mv = float(pos.get("market_value") or 0)
        if split_position_key(str(pos_key))[1] == "short":
            short_mv += mv
        else:
            long_mv += mv
    return long_mv, short_mv, round(long_mv - short_mv, 4)


def _codes_with_positive_mark(accounts: list[dict[str, Any]]) -> set[str]:
    """扫描账户中已有正数 mark price 的标的集合。"""
    marked: set[str] = set()
    for item in accounts:
        positions = item.get("account", {}).get("positions")
        if not isinstance(positions, dict):
            continue
        for pos_key, pos in positions.items():
            if not isinstance(pos, dict):
                continue
            try:
                if float(pos.get("price") or 0) <= 0:
                    continue
            except (TypeError, ValueError):
                continue
            code, _side = split_position_key(str(pos_key))
            if code:
                marked.add(code)
    return marked


def build_remark_updates(
    account: dict[str, Any], prices: dict[str, float]
) -> dict[str, dict[str, float]]:
    """按最新价构建持仓重估更新；价格无效的持仓跳过（保留原值）。"""
    updates: dict[str, dict[str, float]] = {}
    positions = account.get("positions")
    if not isinstance(positions, dict):
        return updates
    for pos_key, pos in positions.items():
        if not isinstance(pos, dict):
            continue
        vol = float(pos.get("volume") or 0)
        if vol <= 0:
            continue
        code, _side = split_position_key(str(pos_key))
        px = float(prices.get(code) or 0)
        if px <= 0:
            continue
        px_rounded = round(px, 4)
        mv = round(vol * px, 2)
        if (
            abs(px_rounded - float(pos.get("price") or 0)) > 1e-9
            or abs(mv - float(pos.get("market_value") or 0)) > 1e-9
        ):
            updates[str(pos_key)] = {"price": px_rounded, "market_value": mv}
    return updates


class SimulationEquitySettlementWorker:
    """周期性执行：对账确权 → 行情重估 → 权益持久化。"""

    def __init__(self, redis: RedisClient, interval_seconds: int = 30):
        self.redis = redis
        self.interval_seconds = max(10, int(interval_seconds))
        self._stopped = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._cycle_count = 0

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._run(), name="sim-equity-settle-worker")

    async def stop(self) -> None:
        self._stopped.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        # 启动即执行首个周期：重启后不等第一个 interval，几秒内恢复权益数据。
        heartbeat_every = settle_heartbeat_cycles()
        while not self._stopped.is_set():
            try:
                stats = await asyncio.wait_for(
                    self.run_cycle(),
                    timeout=settle_cycle_timeout_seconds(),
                )
                self._cycle_count += 1
                # Quiet cycles stay silent; heartbeat keeps overnight stalls visible.
                if (
                    self._cycle_count == 1
                    or self._cycle_count % heartbeat_every == 0
                    or stats.get("remarked")
                    or (stats.get("reconcile") or {}).get("diff_fields")
                    or stats.get("error")
                ):
                    logger.info("Simulation equity settle cycle: %s", stats)
            except asyncio.TimeoutError:
                self._cycle_count += 1
                logger.error(
                    "Simulation equity settle cycle timed out after %ss (cycle=%s)",
                    settle_cycle_timeout_seconds(),
                    self._cycle_count,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Simulation equity settle cycle failed: %s", exc)
            try:
                await asyncio.wait_for(
                    self._stopped.wait(), timeout=self.interval_seconds
                )
            except asyncio.TimeoutError:
                continue

    # ------------------------------------------------------------------
    # 周期主流程
    # ------------------------------------------------------------------

    async def run_cycle(self) -> dict[str, Any]:
        stats: dict[str, Any] = {
            "cycle": self._cycle_count + 1,
            "accounts": 0,
            "remarked": 0,
            "snapshots": 0,
            "pg_accounts": 0,
            "reconcile": {},
            "error": None,
        }
        if not self.redis.client:
            stats["error"] = "REDIS_UNAVAILABLE"
            return stats

        # Step 1: 对账确权（PG 台账为主；差异回填 Redis 并落报告）
        try:
            from backend.services.simulation.services.reconcile_service import (
                autofix_enabled,
                run_reconcile_once,
            )

            stats["reconcile"] = await run_reconcile_once(
                self.redis, autofix=autofix_enabled()
            )
        except Exception as exc:
            stats["reconcile"] = {"error": str(exc)}
            logger.warning("Equity settle reconcile step failed: %s", exc)

        # Step 2: 行情重估（对账之后再读账户，拿到确权后的最新值）
        accounts: list[dict[str, Any]] = []
        try:
            accounts = await asyncio.to_thread(self._load_accounts)
            stats["accounts"] = len(accounts)
            if accounts:
                prices = await self._resolve_prices(accounts)
                for item in accounts:
                    try:
                        if await self._remark_account(item, prices):
                            stats["remarked"] += 1
                    except Exception as exc:
                        logger.debug(
                            "Equity settle remark skipped %s: %s", item["key"], exc
                        )
        except Exception as exc:
            stats["error"] = f"remark: {exc}"
            logger.warning("Equity settle remark step failed: %s", exc)

        # Step 3: 权益持久化（资金快照 upsert + 台账账户投影字段刷新）
        # 重估写在 Redis；持久化前重读，避免用 remark 前的内存快照回写 PG。
        try:
            from backend.services.simulation.services.fund_snapshot_service import (
                SimulationFundSnapshotService,
            )

            result = await SimulationFundSnapshotService.capture_all(self.redis)
            stats["snapshots"] = result.upserted_rows
            accounts = await asyncio.to_thread(self._load_accounts)
            stats["pg_accounts"] = await self._update_pg_accounts(accounts)
        except Exception as exc:
            stats["error"] = stats.get("error") or f"persist: {exc}"
            logger.warning("Equity settle persist step failed: %s", exc)

        return stats

    # ------------------------------------------------------------------
    # 账户扫描与取价
    # ------------------------------------------------------------------

    def _load_accounts(self) -> list[dict[str, Any]]:
        """扫描全部模拟账户键并解析。键形 simulation:account:{tenant}:{user}[:{market}]。"""
        out: list[dict[str, Any]] = []
        try:
            keys = list(
                self.redis.client.scan_iter(match="simulation:account:*", count=500)
            )
        except Exception as exc:
            logger.warning("Equity settle scan accounts failed: %s", exc)
            return out
        for raw_key in keys:
            key = str(raw_key)
            parts = key.split(":")
            if len(parts) < 4:
                continue
            tenant_id = parts[2].strip() or "default"
            user_id = parts[3].strip()
            market = parts[4].strip().upper() if len(parts) > 4 else "CN"
            try:
                raw = self.redis.client.get(key)
                if not raw:
                    continue
                account = json.loads(raw)
                if not isinstance(account, dict):
                    continue
                # 市场以账户字段优先（多市场重估选择对应行情源）
                market = str(account.get("market") or market or "CN").upper()
            except Exception as exc:
                logger.debug("Equity settle parse account failed %s: %s", key, exc)
                continue
            out.append(
                {
                    "key": key,
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "market": market,
                    "account": account,
                }
            )
        return out

    async def _resolve_prices(self, accounts: list[dict[str, Any]]) -> dict[str, float]:
        """批量解析最新价：新鲜 series → 盘后 series 末笔 →（次日 06:00 后）日线。"""
        market_by_code: dict[str, str] = {}
        for item in accounts:
            positions = item["account"].get("positions")
            if not isinstance(positions, dict):
                continue
            for pos_key in positions:
                code, _side = split_position_key(str(pos_key))
                if code:
                    market_by_code.setdefault(code, item["market"])

        if not market_by_code:
            return {}

        from backend.services.simulation.services.redis_series_quote import (
            fetch_series_ticks,
        )

        codes = sorted(market_by_code)
        ticks = await fetch_series_ticks(codes)

        prices: dict[str, float] = {}
        missing_codes: list[str] = []
        for code in codes:
            tick = ticks.get(code)
            px = 0.0
            if isinstance(tick, dict):
                try:
                    px = float(tick.get("price") or 0)
                except (TypeError, ValueError):
                    px = 0.0
            if px > 0:
                prices[code] = px
            else:
                missing_codes.append(code)

        if not missing_codes:
            return prices

        # 盘后/凌晨：新鲜 tick 已过期，改用 series 末笔保住尾盘价，避免过早日线打回 T-1
        allow_daily = daily_close_fallback_allowed()
        if not allow_daily:
            stale_ticks = await fetch_series_ticks(
                missing_codes, max_age_sec=overnight_series_max_age_sec()
            )
            still_missing: list[str] = []
            for code in missing_codes:
                tick = stale_ticks.get(code)
                px = 0.0
                if isinstance(tick, dict):
                    try:
                        px = float(tick.get("price") or 0)
                    except (TypeError, ValueError):
                        px = 0.0
                if px > 0:
                    prices[code] = px
                else:
                    still_missing.append(code)
            missing_codes = still_missing

        if not missing_codes or not allow_daily:
            return prices

        # 日线收盘兜底：仅次日 06:00 后、且仅补「尚无有效市价」的标的。
        # 已有 mark 时保留原值，避免盘中 tick 短暂缺失时被过期收盘覆盖。
        marked_codes = _codes_with_positive_mark(accounts)
        for code in missing_codes:
            if code in marked_codes:
                continue
            px = await self._fallback_close(code, market_by_code[code])
            if px > 0:
                prices[code] = px
        return prices

    async def _fallback_close(self, code: str, market: str) -> float:
        """本地日线最近交易日收盘价（同步磁盘 IO 放线程里跑）。"""

        def _read() -> float:
            try:
                from backend.services.simulation.services.local_market_data import (
                    get_local_market_data,
                )

                lmd = get_local_market_data(market)
                trade_date = lmd.latest_trade_date()
                if trade_date is None:
                    return 0.0
                bar = lmd.get_bar(code, trade_date)
                if bar is None:
                    return 0.0
                close = float(bar.close or 0)
                return close if close > 0 else 0.0
            except Exception as exc:
                logger.debug("Equity settle fallback close failed %s: %s", code, exc)
                return 0.0

        return await asyncio.to_thread(_read)

    # ------------------------------------------------------------------
    # 重估与持久化
    # ------------------------------------------------------------------

    async def _remark_account(
        self, item: dict[str, Any], prices: dict[str, float]
    ) -> bool:
        """Lua 原子重估单账户；无可变更且汇总无漂移时跳过。返回是否执行了写。"""
        account = item["account"]
        updates = build_remark_updates(account, prices)
        positions = account.get("positions") or {}
        cash = float(account.get("cash") or 0)
        short_proceeds = float(account.get("short_proceeds") or 0.0)
        _long_mv, _short_mv, net_mv = summarize_positions(positions)
        drift = (
            abs(
                float(account.get("total_asset") or 0)
                - (cash + short_proceeds + net_mv)
            )
            > 0.01
        )
        if not updates and not drift:
            return False
        if not self.redis.client:
            return False
        try:
            result = await asyncio.to_thread(
                self.redis.client.eval,
                _REMARK_LUA,
                1,
                item["key"],
                json.dumps(updates, ensure_ascii=False),
            )
            payload = json.loads(result) if isinstance(result, str) else result
            if isinstance(payload, dict) and payload.get("success"):
                return True
            logger.debug(
                "Equity settle remark lua rejected %s: %s", item["key"], payload
            )
        except Exception as exc:
            logger.warning("Equity settle remark lua failed %s: %s", item["key"], exc)
        return False

    async def _update_pg_accounts(self, accounts: list[dict[str, Any]]) -> int:
        """刷新 simulation_accounts 台账投影字段（仅 UPDATE 已有行，不新建）。"""
        if not accounts:
            return 0
        from sqlalchemy import update as sa_update

        from backend.services.simulation.models.account import SimulationAccount
        from backend.shared.database_manager_v2 import get_session

        # 同一 (tenant, user) 只取一个账户（CN 优先，与台账 account_id 口径一致）
        chosen: dict[tuple[str, str], dict[str, Any]] = {}
        for item in accounts:
            if not item["user_id"].isdigit():
                continue
            k = (item["tenant_id"], item["user_id"])
            if k not in chosen or item["market"] == "CN":
                chosen[k] = item

        updated = 0
        now = datetime.utcnow()
        async with get_session() as session:
            for (tenant_id, user_id), item in chosen.items():
                account = item["account"]
                account_id = f"sim:{tenant_id}:{user_id}"
                long_mv, short_mv, net_mv = summarize_positions(
                    account.get("positions")
                )
                cash = float(account.get("cash") or 0)
                short_proceeds = float(account.get("short_proceeds") or 0.0)
                total_asset = round(cash + short_proceeds + net_mv, 4)
                result = await session.execute(
                    sa_update(SimulationAccount)
                    .where(SimulationAccount.account_id == account_id)
                    .values(
                        long_market_value=round(long_mv, 4),
                        short_market_value=round(short_mv, 4),
                        total_asset=total_asset,
                        equity=total_asset,
                        last_projected_at=now,
                    )
                )
                updated += int(getattr(result, "rowcount", 0) or 0)
            await session.commit()
        return updated
