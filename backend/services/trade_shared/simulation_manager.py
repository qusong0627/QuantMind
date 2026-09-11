"""
Simulation Account Manager - Manage paper trading accounts in Redis
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from backend.services.trade_shared.redis_client import RedisClient, redis_client
from backend.services.trade_shared.trade_config import settings
from backend.shared.simulation_account_keys import normalize_tenant
from backend.shared.trade_account_cache import read_json_cache, write_json_cache

logger = logging.getLogger(__name__)

# OSS 单用户部署的保留映射：JWT sub 为用户名（如 admin）时，历史模拟数据
# （sim_orders/sim_trades PG user_id、Redis 账户键）全落在 user 0 上。
# 改映射会丢历史，故保持 0，但用 error 日志标出，便于多用户部署时发现串号。
RESERVED_NON_NUMERIC_USER_ID = 0


def require_sim_user_id(raw_user_id: str, tenant_id: str = "default") -> int:
    """JWT sub 转模拟盘 int user_id。三处路由共用，禁止各自手写分叉。

    数字 sub 直接转 int；非数字（OSS 默认 admin 用户）归 0。
    同时旁路记录 sub 反查映射（见 record_sim_sub），供 WS 推送定位主题。
    """
    from fastapi import HTTPException

    if not raw_user_id:
        raise HTTPException(status_code=400, detail="Invalid user_id in token")
    raw = str(raw_user_id).strip()
    if raw.isdigit():
        sim_user_id = int(raw)
    else:
        logger.error(
            "Non-numeric user_id mapped to reserved account 0: %s "
            "(多用户部署下不同用户名会串号，请改用数字 sub)",
            raw,
        )
        sim_user_id = RESERVED_NON_NUMERIC_USER_ID
    record_sim_sub(sim_user_id, raw, tenant_id=tenant_id)
    return sim_user_id


# ── 模拟盘 uid -> JWT sub 反查映射 ────────────────────────────────────────
# 前端 WS 订阅主题是 `strategy.{JWT sub}`（如 strategy.admin），而模拟账户键
# 用的是数字 uid（非数字 sub 一律归 0）。仅凭账户键无法反推订阅主题，故在解析
# 身份时旁路记一份映射；推送侧据此把消息发到前端真正订阅的主题上。
# 非数字 sub 会多个名字映射到同一个 0 号账户，故用 SET 而非单值。
SIM_SUB_MAP_TTL_SECONDS = 7 * 24 * 3600


def _sub_map_key(sim_user_id: int, tenant_id: str = "default") -> str:
    return f"quantmind:sim:submap:{normalize_tenant(tenant_id)}:{sim_user_id}"


def _shared_redis():
    """取共享 Redis 客户端（未连接则先连），失败返回 None。"""
    try:
        if redis_client.client is None:
            redis_client.connect()
        return redis_client.client
    except Exception as exc:
        logger.debug("shared redis unavailable: %s", exc)
        return None


def record_sim_sub(
    sim_user_id: int, raw_user_id: str, tenant_id: str = "default"
) -> None:
    """记录 sub 反查映射。纯旁路，任何失败都只记日志，不影响鉴权主流程。"""
    try:
        raw = str(raw_user_id or "").strip()
        if not raw:
            return
        client = _shared_redis()
        if client is None:
            return
        key = _sub_map_key(sim_user_id, tenant_id)
        client.sadd(key, raw)
        client.expire(key, SIM_SUB_MAP_TTL_SECONDS)
    except Exception as exc:
        logger.debug("record_sim_sub failed: %s", exc)


def resolve_sim_subs(sim_user_id: int, tenant_id: str = "default") -> list[str]:
    """读取某模拟盘 uid 对应的全部 JWT sub（可能多个串号到同一账户）。"""
    try:
        client = _shared_redis()
        if client is None:
            return []
        members = client.smembers(_sub_map_key(sim_user_id, tenant_id)) or set()
        return sorted(str(m) for m in members if str(m).strip())
    except Exception as exc:
        logger.debug("resolve_sim_subs failed: %s", exc)
        return []


class SimulationAccountManager:
    """
    Manage simulation accounts state in Redis.
    Key: simulation:account:{tenant_id}:{user_id}
    """

    def __init__(self, redis: RedisClient):
        self.redis = redis
        # A股 T+1: 当日买入不可卖出。持仓用 volume(总量) 与 available_volume(可卖量) 双字段表达，
        # 买入只增 volume，卖出扣减 available_volume，次日开盘由 unlock_t1 把可卖量补齐。
        self._update_balance_lua = """
local key = KEYS[1]
local symbol = ARGV[1]
local delta_cash = tonumber(ARGV[2])
local delta_volume = tonumber(ARGV[3])
local price = tonumber(ARGV[4])

local raw = redis.call("GET", key)
if not raw then
    return cjson.encode({success=false, reason="ACCOUNT_NOT_FOUND"})
end

local account = cjson.decode(raw)
local cash = tonumber(account.cash or 0)
local positions = account.positions or {}

local pos = positions[symbol]
if not pos then
    pos = {volume=0, available_volume=0, cost=0, market_value=0, price=0}
end

-- ARGV[5]: 买入时新增的可卖量。CN(T+1) 传 0（锁定至次日），
-- T+0 市场传 delta_volume（买入即可卖）。卖出路径不受影响。
local avail_delta = tonumber(ARGV[5] or "0")

local old_volume = tonumber(pos.volume or 0)
-- 兼容 T+1 上线前写入的存量持仓: 它们没有 available_volume 字段，
-- 视为全部可卖。缺少这个回退会让存量持仓永久无法卖出且不报错。
local old_available
if pos.available_volume == nil then
    old_available = old_volume
else
    old_available = tonumber(pos.available_volume)
end

local new_cash = cash + delta_cash
local new_volume = old_volume + delta_volume

if new_cash < -0.000001 then
    return cjson.encode({success=false, reason="INSUFFICIENT_CASH"})
end

if new_volume < -0.000001 then
    return cjson.encode({success=false, reason="INSUFFICIENT_HOLDINGS"})
end

local new_available = old_available
if delta_volume < 0 then
    -- 卖出必须动用可卖量（T+1 约束）
    if old_available + delta_volume < -0.000001 then
        return cjson.encode({
            success=false,
            reason="INSUFFICIENT_AVAILABLE_VOLUME",
            available_volume=old_available,
            requested=-delta_volume
        })
    end
    new_available = old_available + delta_volume
elseif delta_volume > 0 then
    new_available = old_available + avail_delta
end

if delta_volume > 0 then
    local current_cost_total = tonumber(pos.cost or 0) * old_volume
    if new_volume > 0 then
        pos.cost = (current_cost_total + (delta_volume * price)) / new_volume
    else
        pos.cost = 0
    end
end

pos.volume = new_volume
pos.available_volume = new_available
pos.price = price
pos.market_value = new_volume * price

if new_volume <= 0.0001 then
    positions[symbol] = nil
else
    positions[symbol] = pos
end

local total_market_value = 0
for _, p in pairs(positions) do
    total_market_value = total_market_value + (tonumber(p.volume or 0) * tonumber(p.price or 0))
end

account.cash = new_cash
account.positions = positions
account.market_value = total_market_value
account.total_asset = new_cash + total_market_value

redis.call("SET", key, cjson.encode(account))
return cjson.encode({success=true})
"""

        # 交易日开盘前调用：把全部持仓的可卖量补齐为总量，解除 T+1 锁定。
        self._unlock_t1_lua = """
local key = KEYS[1]

local raw = redis.call("GET", key)
if not raw then
    return cjson.encode({success=false, reason="ACCOUNT_NOT_FOUND"})
end

local account = cjson.decode(raw)
local positions = account.positions or {}
local unlocked = 0

for symbol, pos in pairs(positions) do
    local volume = tonumber(pos.volume or 0)
    local available
    if pos.available_volume == nil then
        available = volume
    else
        available = tonumber(pos.available_volume)
    end
    if available < volume then
        unlocked = unlocked + 1
    end
    pos.available_volume = volume
    positions[symbol] = pos
end

account.positions = positions
redis.call("SET", key, cjson.encode(account))
return cjson.encode({success=true, unlocked=unlocked})
"""

    @staticmethod
    def _normalize_tenant(tenant_id: str | None) -> str:
        from backend.shared.simulation_account_keys import normalize_tenant

        return normalize_tenant(tenant_id)

    @staticmethod
    def _normalize_market(market: str | None) -> str:
        """市场标识（账户 Redis 键维度）。CN 保持无后缀旧键，兼容存量账户。"""
        from backend.shared.simulation_account_keys import normalize_market

        return normalize_market(market)

    def _get_key(self, user_id: int, tenant_id: str, market: str = "CN") -> str:
        from backend.shared.simulation_account_keys import account_key

        return account_key(tenant_id, user_id, market)

    @staticmethod
    def _exec_lock_key(user_id: int, tenant_id: str) -> str:
        return f"simulation:exec_lock:{SimulationAccountManager._normalize_tenant(tenant_id)}:{user_id}"

    @staticmethod
    def acquire_exec_lock(
        user_id: int, tenant_id: str = "default", ttl_seconds: int = 15
    ) -> str | None:
        """同一用户撮合/结算临界区分布式锁（SET NX EX），跨进程串行化。

        返回token（持锁成功）或None（未拿到锁/Redis不可用时返回特殊放行标记""）。
        调用方必须用 release_exec_lock(token) 释放。Redis不可用时返回""表示
        降级放行（单机并发仍有风险，但不阻塞交易）。
        """
        import uuid as _uuid

        try:
            client = _shared_redis()
            if client is None:
                return ""
            token = _uuid.uuid4().hex
            ok = client.set(
                SimulationAccountManager._exec_lock_key(user_id, tenant_id),
                token,
                nx=True,
                ex=max(3, int(ttl_seconds or 15)),
            )
            return token if ok else None
        except Exception:
            return ""

    @staticmethod
    def locked_execution(
        user_id: int, tenant_id: str = "default", ttl_seconds: int = 20
    ):
        """同用户撮合临界区异步上下文管理器（P0-1/P0-4）。

        用法: `async with SimulationAccountManager.locked_execution(uid, tid): ...`
        拿不到锁时重试~1s，仍失败则抛RuntimeError由调用方转拒单（不静默双花）。
        Redis不可用时降级放行（Lua原子扣减仍是兜底）。
        """
        import asyncio as _asyncio
        import contextlib as _ctxlib

        @_ctxlib.asynccontextmanager
        async def _cm():
            token: str | None = None
            for _attempt in range(10):
                token = SimulationAccountManager.acquire_exec_lock(
                    user_id, tenant_id, ttl_seconds=ttl_seconds
                )
                if token:
                    break
                if token == "":
                    break
                await _asyncio.sleep(0.1)
            if token is None:
                raise RuntimeError("SIM_EXEC_LOCK_BUSY")
            try:
                yield token
            finally:
                SimulationAccountManager.release_exec_lock(user_id, tenant_id, token)

        return _cm()

    @staticmethod
    def release_exec_lock(user_id: int, tenant_id: str, token: str | None) -> None:
        if not token:
            return
        try:
            client = _shared_redis()
            if client is None:
                return
            _release_lua = """
local key = KEYS[1]
if redis.call("GET", key) == ARGV[1] then
    return redis.call("DEL", key)
end
return 0
"""
            try:
                client.eval(
                    _release_lua,
                    1,
                    SimulationAccountManager._exec_lock_key(user_id, tenant_id),
                    token,
                )
            except Exception:
                # eval不可用（如代理限制）时退化为直接删除：最坏多放行一次，
                # 不影响资金安全（Lua扣减仍是原子兜底）。
                try:
                    client.delete(
                        SimulationAccountManager._exec_lock_key(user_id, tenant_id)
                    )
                except Exception:
                    pass
        except Exception:
            pass

    @staticmethod
    def parse_account_key(key: str) -> tuple[str, str, str] | None:
        """解析账户键 -> (tenant, user原文, market)，供扫描类任务使用。"""
        from backend.shared.simulation_account_keys import parse_account_key

        return parse_account_key(key)

    def _get_settings_key(self, user_id: int, tenant_id: str) -> str:
        from backend.shared.simulation_account_keys import settings_key

        return settings_key(tenant_id, user_id)

    @staticmethod
    def _position_key(symbol: str, position_side: str) -> str:
        side = str(position_side or "long").strip().lower()
        return f"{symbol.upper()}::{side}"

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(timezone.utc)

    async def get_settings(
        self,
        user_id: int,
        tenant_id: str = "default",
        default_initial_cash: float = 1_000_000.0,
        cooldown_days: int = 30,
    ) -> dict[str, Any]:
        tenant_id = self._normalize_tenant(tenant_id)
        key = self._get_settings_key(user_id, tenant_id)
        data = read_json_cache(self.redis, key)

        initial_cash = float(default_initial_cash)
        last_modified_at: str | None = None
        next_allowed_modified_at: str | None = None
        can_modify = True

        if data:
            initial_cash = float(data.get("initial_cash", default_initial_cash))
            last_modified_at = data.get("last_modified_at")
            if last_modified_at:
                try:
                    last_dt = datetime.fromisoformat(
                        last_modified_at.replace("Z", "+00:00")
                    )
                    next_dt = last_dt + timedelta(days=cooldown_days)
                    next_allowed_modified_at = next_dt.isoformat()
                    can_modify = self._utc_now() >= next_dt
                except Exception:
                    logger.warning(
                        "Failed to parse simulation settings timestamp for key=%s", key
                    )

        return {
            "initial_cash": initial_cash,
            "last_modified_at": last_modified_at,
            "next_allowed_modified_at": next_allowed_modified_at,
            "can_modify": can_modify,
            "cooldown_days": cooldown_days,
        }

    async def set_initial_cash(
        self,
        user_id: int,
        initial_cash: float,
        tenant_id: str = "default",
    ) -> None:
        """Update initial_cash in settings (used when syncing holdings)."""
        tenant_id = self._normalize_tenant(tenant_id)
        key = self._get_settings_key(user_id, tenant_id)

        # 读取现有 settings，保留其他字段
        data = read_json_cache(self.redis, key) or {}
        data["initial_cash"] = float(initial_cash)
        data["last_modified_at"] = self._utc_now().isoformat()

        write_json_cache(self.redis, key, data)
        logger.info(
            "Updated simulation settings initial_cash for tenant=%s user=%s to %.2f",
            tenant_id,
            user_id,
            initial_cash,
        )

    # set_settings removed as initial cash modification is deprecated.

    async def init_account(
        self,
        user_id: int,
        initial_cash: float = 1_000_000.0,
        tenant_id: str = "default",
        market: str = "CN",
    ) -> dict[str, Any]:
        """Initialize or reset simulation account."""
        tenant_id = self._normalize_tenant(tenant_id)
        key = self._get_key(user_id, tenant_id, market)

        account_data = {
            "cash": initial_cash,
            "total_asset": initial_cash,
            "market_value": 0.0,
            "short_market_value": 0.0,
            "liabilities": 0.0,
            "maintenance_margin_ratio": 0.0,
            "warning_level": "normal",
            "positions": {},
            "market": self._normalize_market(market),
        }

        write_json_cache(self.redis, key, account_data)
        logger.info(
            "Initialized simulation account for tenant=%s user=%s market=%s with %.2f",
            tenant_id,
            user_id,
            market,
            initial_cash,
        )

        return account_data

    async def get_account(
        self, user_id: int, tenant_id: str = "default", market: str = "CN"
    ) -> dict[str, Any] | None:
        """Get simulation account state. PG 台账为主、Redis 只是缓存。

        Redis 缺键时从 ledger 投影自愈（lots→持仓+收盘重估），并回填 Redis；
        台账也无记录时返回 None（表示账户从未创建，不自动建空账）。
        """
        if not self.redis.client:
            return None

        tenant_id = self._normalize_tenant(tenant_id)
        key = self._get_key(user_id, tenant_id, market)
        data = read_json_cache(self.redis, key)
        if data:
            return data

        rebuilt = await self._rebuild_from_ledger(user_id, tenant_id, market)
        if rebuilt:
            write_json_cache(self.redis, key, rebuilt)
            logger.warning(
                "Simulation account cache miss, rebuilt from PG: tenant=%s user=%s market=%s positions=%d",
                tenant_id,
                user_id,
                market,
                len(rebuilt.get("positions") or {}),
            )
            return rebuilt
        # 不再自动初始化，返回 None 表示账户未创建
        return None

    async def _rebuild_from_ledger(
        self, user_id: int, tenant_id: str, market: str = "CN"
    ) -> dict[str, Any] | None:
        """确权：PG 台账为主。Redis 缺键时从 ledger 投影重建（lots→持仓+收盘重估）。

        口径与 EOD 一致（收盘价重估市值），产出 payload 可直接回填 Redis。
        无台账账户返回 None（不自动建空账）。
        """
        try:
            from backend.services.simulation.services.eod_service import (
                _load_close_price,
            )
            from backend.services.simulation.services.projection_service import (
                SimulationProjectionService,
            )
            from backend.shared.database_manager_v2 import get_session as _get_session
        except Exception as exc:
            logger.warning("Simulation ledger rebuild unavailable: %s", exc)
            return None

        try:
            async with _get_session() as session:
                projection_svc = SimulationProjectionService(session)
                projection = await projection_svc.load_projection(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    latest_price_loader=lambda symbol: _load_close_price(
                        session, symbol
                    ),
                )
                projection_account = projection.account
                if projection_account is None:
                    return None
                positions = projection.positions or {}
                long_mv = 0.0
                short_mv = 0.0
                for pos in positions.values():
                    if not isinstance(pos, dict):
                        continue
                    mv = float(pos.get("market_value") or 0.0)
                    if str(pos.get("side") or "long").strip().lower() == "short":
                        short_mv += mv
                    else:
                        long_mv += mv
                cash = float(projection_account.cash or 0.0)
                # 口径与 EOD 一致：现金 + 多头市值 - 空头市值（Redis 缺键时
                # short_proceeds 不可考，按 0 计；EOD 会补齐）。
                total_asset = round(cash + long_mv - short_mv, 4)
                projection_account.long_market_value = round(long_mv, 4)
                projection_account.short_market_value = round(short_mv, 4)
                projection_account.total_asset = total_asset
                projection_account.equity = total_asset
                return SimulationProjectionService.build_cache_payload(
                    account=projection_account,
                    positions=positions,
                    source="ledger_rebuild",
                )
        except Exception as exc:
            logger.warning(
                "Simulation ledger rebuild failed tenant=%s user=%s: %s",
                tenant_id,
                user_id,
                exc,
            )
            return None

    async def _rebuild_from_pg(
        self, user_id: int, tenant_id: str, market: str = "CN"
    ) -> dict[str, Any] | None:
        """【已降级：灾难恢复手工脚本】从 sim_trades 回放重建模拟账户。

        日常链路不再调用（get_account 缺键走 _rebuild_from_ledger，对账走台账投影）。
        仅当 ledger 台账整体丢失、需从成交流水手工重建时由运维显式调用。
        无任何 PG 记录时返回 None。"""
        try:
            from sqlalchemy import text as _text

            from backend.shared.database_manager_v2 import get_session as _get_session
            from backend.shared.stock_utils import StockCodeUtil
        except Exception as exc:
            logger.warning("Simulation account PG rebuild unavailable: %s", exc)
            return None

        try:
            market_norm = self._normalize_market(market)
            async with _get_session() as session:
                # 初始资金口径与 /account 一致：Redis settings 优先，否则默认 100 万。
                # 注意不能用 fund 快照的 initial_capital——settings 缺失时它会被
                # total_asset 兜底污染，不再是真实初始值。
                initial_cash = 1_000_000.0
                try:
                    sraw = self.redis.client.get(
                        f"simulation:settings:{tenant_id}:{user_id}"
                    )
                    if sraw:
                        import json as _json

                        sdata = _json.loads(sraw)
                        if float(sdata.get("initial_cash") or 0) > 0:
                            initial_cash = float(sdata["initial_cash"])
                except Exception:
                    pass

                rows = (
                    await session.execute(
                        _text(
                            "SELECT symbol, side, quantity, price, commission, "
                            "stamp_duty, transfer_fee, executed_at FROM sim_trades "
                            "WHERE tenant_id=:tid AND user_id=:uid "
                            "ORDER BY id ASC"
                        ),
                        {"tid": tenant_id, "uid": int(user_id)},
                    )
                ).fetchall()
                if not rows:
                    return None

                from datetime import date as _date

                today = _date.today()
                positions: dict[str, dict[str, float]] = {}
                cash = float(initial_cash)
                for (
                    symbol,
                    side,
                    quantity,
                    price,
                    commission,
                    stamp_duty,
                    transfer_fee,
                    executed_at,
                ) in rows:
                    try:
                        prefix = StockCodeUtil.to_prefix(str(symbol))
                    except Exception:
                        continue
                    # 非 CN 市场的成交不计入 CN 账户（分市场键隔离）
                    suffix = prefix[2:] if len(prefix) > 2 else prefix
                    is_cn = prefix[:2] in {"SH", "SZ", "BJ"} and len(suffix) == 6
                    if (market_norm == "CN") != bool(is_cn):
                        continue
                    qty = float(quantity or 0)
                    px = float(price or 0)
                    fee = (
                        float(commission or 0)
                        + float(stamp_duty or 0)
                        + float(transfer_fee or 0)
                    )
                    # 多头键用订单 symbol 原格式（后缀大写，如 600928.SH），与 Lua
                    # update_balance 的 positions[symbol] 同口径；只有空头才带 ::side 后缀。
                    try:
                        pos_key = StockCodeUtil.to_suffix(str(symbol)).upper()
                    except Exception:
                        pos_key = str(symbol).strip().upper()
                    key = pos_key
                    pos = positions.get(key) or {
                        "volume": 0.0,
                        "available_volume": 0.0,
                        "cost": 0.0,
                        "market_value": 0.0,
                        "price": 0.0,
                    }
                    if str(side).lower() == "buy":
                        total_cost = pos["cost"] * pos["volume"] + qty * px
                        pos["volume"] += qty
                        # T+1：当日买入不计入可卖量，历史买入才可卖（重建不再直接解锁）
                        try:
                            trade_day = (
                                executed_at.date()
                                if hasattr(executed_at, "date")
                                else None
                            )
                        except Exception:
                            trade_day = None
                        if market_norm == "CN" and trade_day == today:
                            pass
                        else:
                            pos["available_volume"] += qty
                        pos["cost"] = (
                            total_cost / pos["volume"] if pos["volume"] > 0 else 0.0
                        )
                        cash -= qty * px + fee
                    else:
                        pos["volume"] = max(0.0, pos["volume"] - qty)
                        pos["available_volume"] = max(
                            0.0, pos["available_volume"] - qty
                        )
                        cash += qty * px - fee
                    if px > 0:
                        pos["price"] = px
                    positions[key] = pos

                if not positions and abs(cash - initial_cash) < 1e-9:
                    return None

                # 现价重估市值（无行情时回退成本价）
                market_value = 0.0
                for key, pos in positions.items():
                    sym = key.split("::", 1)[0]
                    last_px = 0.0
                    try:
                        for cand in (sym, StockCodeUtil.to_prefix(sym)):
                            r = (
                                await session.execute(
                                    _text(
                                        "SELECT close FROM stock_daily_latest "
                                        "WHERE symbol=:sym ORDER BY trade_date DESC LIMIT 1"
                                    ),
                                    {"sym": cand},
                                )
                            ).fetchone()
                            if r and float(r[0] or 0) > 0:
                                last_px = float(r[0])
                                break
                    except Exception:
                        pass
                    px = last_px if last_px > 0 else float(pos.get("cost") or 0)
                    pos["price"] = px
                    pos["market_value"] = round(pos["volume"] * px, 2)
                    market_value += pos["market_value"]
                market_value = round(market_value, 2)
                cash = round(cash, 2)
                return {
                    "cash": cash,
                    "total_asset": round(cash + market_value, 2),
                    "market_value": market_value,
                    "short_market_value": 0.0,
                    "liabilities": 0.0,
                    "maintenance_margin_ratio": 0.0,
                    "warning_level": "normal",
                    "positions": positions,
                    "market": market_norm,
                }
        except Exception as exc:
            logger.warning(
                "Simulation account PG rebuild failed tenant=%s user=%s: %s",
                tenant_id,
                user_id,
                exc,
            )
            return None

    async def update_balance(
        self,
        user_id: int,
        symbol: str,
        delta_cash: float,
        delta_volume: float,
        price: float,
        tenant_id: str = "default",
        trade_action: str | None = None,
        position_side: str = "long",
        is_margin_trade: bool = False,
        market: str = "CN",
        t_plus_1: bool = True,
    ) -> dict[str, Any]:
        """Update account balance after trade execution.

        t_plus_1: 买入是否锁定至次日（CN 规则）。T+0 市场传 False，
        买入即刻计入可卖量。
        """
        if not self.redis.client:
            return {"success": False, "reason": "REDIS_UNAVAILABLE"}

        tenant_id = self._normalize_tenant(tenant_id)
        key = self._get_key(user_id, tenant_id, market)

        # 如果账户不存在，先初始化（交易时需要账户存在）。
        # 新注册/默认用户统一按 100 万初始资金建账；
        # 用户对初始资金的修改已废弃（模拟盘设置修改 deprecated），故不读取 settings 残留值。
        if not self.redis.client.get(key):
            await self.init_account(
                user_id, initial_cash=1_000_000.0, tenant_id=tenant_id, market=market
            )

        if (
            is_margin_trade
            or str(position_side).lower() == "short"
            or (
                trade_action
                and trade_action.lower() in {"sell_to_open", "buy_to_close"}
            )
        ):
            return await self._update_balance_margin(
                user_id=user_id,
                symbol=symbol,
                price=price,
                tenant_id=tenant_id,
                trade_action=trade_action,
                quantity=abs(delta_volume),
                market=market,
            )

        # T+0 市场买入即可卖；CN 买入锁定（avail_delta=0）
        avail_delta = (
            float(delta_volume) if (delta_volume > 0 and not t_plus_1) else 0.0
        )

        try:
            result = self.redis.client.eval(
                self._update_balance_lua,
                1,
                key,
                symbol,
                str(delta_cash),
                str(delta_volume),
                str(price),
                str(avail_delta),
            )
            payload = json.loads(result) if isinstance(result, str) else result
            if isinstance(payload, dict):
                return payload
            return {"success": False, "reason": "INVALID_SCRIPT_RESULT"}
        except Exception as e:
            logger.error("Failed to update simulation account atomically: %s", e)
            return {"success": False, "reason": "ATOMIC_UPDATE_FAILED"}

    async def unlock_t1(
        self, user_id: int, tenant_id: str = "default", market: str = "CN"
    ) -> dict[str, Any]:
        """交易日开盘前解除 T+1 锁定：把所有持仓的可卖量补齐为总量。

        仅对 T+1 市场（CN）有意义；对 T+0 市场账户调用无副作用
        （可卖量恒等于总量）。
        """
        if not self.redis.client:
            return {"success": False, "reason": "REDIS_UNAVAILABLE"}

        tenant_id = self._normalize_tenant(tenant_id)
        key = self._get_key(user_id, tenant_id, market)

        try:
            result = self.redis.client.eval(self._unlock_t1_lua, 1, key)
            payload = json.loads(result) if isinstance(result, str) else result
            if isinstance(payload, dict):
                return payload
            return {"success": False, "reason": "INVALID_SCRIPT_RESULT"}
        except Exception as e:
            logger.error(
                "Failed to unlock T+1 for tenant=%s user=%s: %s", tenant_id, user_id, e
            )
            return {"success": False, "reason": "UNLOCK_T1_FAILED"}

    async def _update_balance_margin(
        self,
        *,
        user_id: int,
        symbol: str,
        price: float,
        tenant_id: str,
        trade_action: str | None,
        quantity: float,
        market: str = "CN",
    ) -> dict[str, Any]:
        key = self._get_key(user_id, tenant_id, market)
        # NOTE(P0-4): 本函数读-改-写非原子，依赖调用方（撮合入口/强平）持有
        # 同用户执行锁（acquire_exec_lock）来串行化；直接调用方必须先加锁。
        account = await self.get_account(user_id, tenant_id=tenant_id) or {}
        positions = dict(account.get("positions") or {})
        cash = float(account.get("cash") or 0.0)
        liabilities = float(account.get("liabilities") or 0.0)
        short_market_value = float(account.get("short_market_value") or 0.0)
        short_proceeds = float(account.get("short_proceeds") or 0.0)
        maintenance_ratio = float(account.get("maintenance_margin_ratio") or 0.0)
        warning_level = str(account.get("warning_level") or "normal")

        action = str(trade_action or "").lower()
        pos_key = self._position_key(symbol, "short")
        pos = dict(positions.get(pos_key) or {})
        old_qty = float(pos.get("volume") or 0.0)
        gross = float(quantity) * float(price)
        # 统一手续费费率，回测默认约 0.0015 包含印花税，这里简化表示
        borrow_fee = gross * float(settings.DEFAULT_BORROW_RATE) / 252.0

        if action == "sell_to_open":
            new_qty = old_qty + float(quantity)
            total_cost = float(pos.get("cost") or 0.0) * old_qty + gross
            pos["volume"] = new_qty
            pos["cost"] = total_cost / new_qty if new_qty > 0 else 0.0
            pos["price"] = float(price)
            pos["market_value"] = new_qty * float(price)
            pos["side"] = "short"
            pos["borrow_fee"] = float(pos.get("borrow_fee") or 0.0) + borrow_fee

            # 融券所得资金冻结
            short_proceeds += gross
            # 现金只扣减手续费
            cash -= borrow_fee

            liabilities += gross
            short_market_value += pos["market_value"]
        elif action == "buy_to_close":
            if old_qty < float(quantity):
                return {"success": False, "reason": "INSUFFICIENT_SHORT_POSITION"}

            avg_cost = float(pos.get("cost") or 0.0)
            short_entry_val = avg_cost * float(quantity)

            # 实现盈亏 = 融券开仓价值 - 买入平仓成本 - 手续费
            realized = short_entry_val - gross - borrow_fee

            # 只有净盈亏结算至可用现金
            cash += realized

            # 释放对应的冻结本金
            short_proceeds = max(0.0, short_proceeds - short_entry_val)

            new_qty = old_qty - float(quantity)
            liabilities = max(0.0, liabilities - short_entry_val)
            short_market_value = max(0.0, short_market_value - short_entry_val)

            if new_qty <= 1e-6:
                positions.pop(pos_key, None)
            else:
                pos["volume"] = new_qty
                pos["price"] = float(price)
                pos["market_value"] = new_qty * float(price)
                pos["borrow_fee"] = float(pos.get("borrow_fee") or 0.0) + borrow_fee
                pos["realized_pnl"] = float(pos.get("realized_pnl") or 0.0) + realized
                positions[pos_key] = pos
        else:
            return {"success": False, "reason": f"UNSUPPORTED_TRADE_ACTION:{action}"}

        if action == "sell_to_open":
            positions[pos_key] = pos

        total_market_value = 0.0
        for position in positions.values():
            qty = float(position.get("volume") or 0.0)
            px = float(position.get("price") or 0.0)
            side = str(position.get("side") or "long").lower()
            mv = qty * px
            total_market_value += mv if side == "long" else -mv

        equity = cash + short_proceeds + total_market_value

        # 维持担保比例 = 总资产 / 总负债
        if liabilities > 0:
            maintenance_ratio = equity / liabilities if liabilities else 0.0
            if maintenance_ratio <= float(settings.MARGIN_CLOSEOUT_RATIO):
                warning_level = "closeout"
            elif maintenance_ratio <= float(settings.MARGIN_WARNING_RATIO):
                warning_level = "warning"
            else:
                warning_level = "normal"

        account.update(
            {
                "cash": cash,
                "short_proceeds": short_proceeds,
                "positions": positions,
                "market_value": total_market_value,
                "short_market_value": short_market_value,
                "liabilities": liabilities,
                "maintenance_margin_ratio": maintenance_ratio,
                "warning_level": warning_level,
                "total_asset": equity,
            }
        )
        write_json_cache(self.redis, key, account)
        return {"success": True, "reason": "OK", "account": account}
