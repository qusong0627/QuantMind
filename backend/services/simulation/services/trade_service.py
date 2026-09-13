"""
Simulation trade service.
"""

from uuid import UUID

from sqlalchemy import String, and_, case, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.simulation.models.trade import SimTrade
from backend.services.simulation.services.market_rules import market_symbol_sql_regex


class SimTradeService:
    def __init__(self, db: AsyncSession, redis=None):
        self.db = db
        self.redis = redis
        # 允许测试传入 mock DB 时不报错
        try:
            from backend.services.trade_shared.trade_config import settings as _settings
            self._cache_ttl = getattr(_settings, "CACHE_TTL_TRADE", 600)
        except Exception:
            self._cache_ttl = 60

    @staticmethod
    def _market_condition(market: str | None):
        """市场过滤条件：sim_trades 无 market 列，按 symbol 形态判据过滤。

        pattern 来自 market_rules（与引擎 infer_market 同一套判据）；
        market 为空或不认识时返回 None（不过滤，保持历史行为）。
        """
        pattern = market_symbol_sql_regex(market)
        if not pattern:
            return None
        return SimTrade.symbol.op("~*")(pattern)

    async def get_trade(self, tenant_id: str, user_id: int, trade_id: UUID) -> SimTrade | None:
        result = await self.db.execute(
            select(SimTrade).where(
                and_(
                    SimTrade.tenant_id == tenant_id,
                    cast(SimTrade.user_id, String) == str(user_id),
                    SimTrade.trade_id == trade_id,
                )
            )
        )
        return result.scalar_one_or_none()

    def _list_cache_key(self, tenant_id: str, user_id: int, portfolio_id: int | None, symbol: str | None, limit: int, offset: int, market: str | None = None) -> str:
        sym = symbol.upper() if symbol else "all"
        port = str(portfolio_id) if portfolio_id is not None else "all"
        mkt = str(market).upper() if market else "all"
        return f"sim_trade:list:{tenant_id}:{user_id}:{port}:{sym}:{mkt}:{limit}:{offset}"

    async def list_trades(
        self,
        tenant_id: str,
        user_id: int,
        *,
        portfolio_id: int | None = None,
        symbol: str | None = None,
        market: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SimTrade]:
        # 确保联合索引存在（历史库无此索引会导致 ORDER BY 全表排序慢）
        try:
            from sqlalchemy import text as _text
            await self.db.execute(_text("CREATE INDEX IF NOT EXISTS idx_sim_trade_user_executed ON sim_trades (tenant_id, user_id, executed_at DESC)"))
        except Exception:
            pass
        # Redis 缓存：仅对常规分页生效，带 symbol 时仍缓存（key 已区分）
        cache_key = self._list_cache_key(tenant_id, user_id, portfolio_id, symbol, limit, offset, market)
        if self.redis and getattr(self.redis, "client", None):
            try:
                cached = self.redis.get(cache_key)
                # get via trade redis_client returns already json-decoded dict/list if using trade's RedisClient.get
                # trade's get does json.loads, so cached may be list of dicts; we need to return SimTrade-like objects
                # For cache hit, we reconstruct SimTrade objects from dicts if needed, but to keep fast path we return raw dicts and let router handle
                # Here we check if cached is not None and is list, we can return as is after reconstructing via SimTrade
                if cached is not None:
                    # cached from trade RedisClient is already json-decoded
                    if isinstance(cached, list) and cached:
                        # 需要从缓存恢复为 SimTrade 对象，但为避免 ORM 开销，直接返回缓存的 dict 列表会被 FastAPI 序列化接受
                        # 调用方期望 list[SimTrade]，但 Pydantic 会接受 dict；我们直接返回 cached 的重建
                        # 尝试重建 SimTrade  stub：若缓存是 dict 列表，直接返回（FastAPI 会按 SimTradeResponse 序列化）
                        # 为保持类型，返回前尝试用 SimTrade 构造（若失败则回退 DB）
                        try:
                            # 若缓存是序列化后的 Trade dict，无法直接转为 SimTrade ORM，直接返回 DB 查询以保证类型
                            # 但为性能，我们直接返回缓存的 dict，让上层透传（需 router 不强制类型）
                            # 这里选择直接返回缓存的 list（调用方会序列化），若调用方严格校验则回退
                            if isinstance(cached[0], dict):
                                # 返回缓存的原始 SimTrade dict，由 router 直接返回
                                return cached  # type: ignore[return-value]
                        except Exception:
                            pass
            except Exception:
                pass

        conditions = [SimTrade.tenant_id == tenant_id, cast(SimTrade.user_id, String) == str(user_id)]
        if portfolio_id is not None:
            conditions.append(SimTrade.portfolio_id == portfolio_id)
        if symbol:
            conditions.append(SimTrade.symbol == symbol.upper())
        market_cond = self._market_condition(market)
        if market_cond is not None:
            conditions.append(market_cond)

        stmt = (
            select(SimTrade).where(and_(*conditions)).order_by(SimTrade.executed_at.desc()).limit(limit).offset(offset)
        )
        result = await self.db.execute(stmt)
        trades = list(result.scalars().all())

        if self.redis and getattr(self.redis, "client", None) and trades:
            try:
                # 序列化为可缓存的 dict 列表
                import json
                from datetime import datetime as _dt
                from decimal import Decimal as _Dec
                from uuid import UUID as _UUID
                trade_dicts = []
                for t in trades:
                    d = {c.name: getattr(t, c.name) for c in t.__table__.columns}
                    for k, v in list(d.items()):
                        if isinstance(v, (_dt, _UUID, _Dec)):
                            d[k] = str(v)
                    trade_dicts.append(d)
                # 使用 trade RedisClient 的 set (自动 json.dumps)
                self.redis.set(cache_key, trade_dicts, ttl=self._cache_ttl)
            except Exception:
                pass
        return trades

    def _stats_cache_key(self, tenant_id: str, user_id: int, portfolio_id: int | None, market: str | None = None) -> str:
        port = str(portfolio_id) if portfolio_id is not None else "all"
        mkt = str(market).upper() if market else "all"
        return f"sim_trade:stats:{tenant_id}:{user_id}:{port}:{mkt}"

    async def get_stats(self, tenant_id: str, user_id: int, portfolio_id: int | None = None, market: str | None = None) -> dict:
        cache_key = self._stats_cache_key(tenant_id, user_id, portfolio_id, market)
        if self.redis and getattr(self.redis, "client", None):
            try:
                cached = self.redis.get(cache_key)
                if cached is not None:
                    # trade RedisClient.get already json.loads
                    if isinstance(cached, dict) and cached.get("total_trades") is not None:
                        return cached
            except Exception:
                pass
        conditions = [SimTrade.tenant_id == tenant_id, cast(SimTrade.user_id, String) == str(user_id)]
        if portfolio_id is not None:
            conditions.append(SimTrade.portfolio_id == portfolio_id)
        market_cond = self._market_condition(market)
        if market_cond is not None:
            conditions.append(market_cond)

        summary_stmt = select(
            func.count(SimTrade.id).label("total_trades"),
            func.coalesce(func.sum(SimTrade.trade_value), 0.0).label("total_value"),
            func.coalesce(func.sum(SimTrade.commission), 0.0).label("total_commission"),
            func.coalesce(func.sum(case((SimTrade.side == "buy", 1), else_=0)), 0).label("buy_trades"),
            func.coalesce(func.sum(case((SimTrade.side == "sell", 1), else_=0)), 0).label("sell_trades"),
        ).where(and_(*conditions))
        summary_row = (await self.db.execute(summary_stmt)).one()

        day_bucket = func.date(SimTrade.executed_at)
        daily_stmt = (
            select(day_bucket.label("trade_day"), func.count(SimTrade.id).label("trade_count"))
            .where(and_(*conditions))
            .group_by(day_bucket)
            .order_by(day_bucket.asc())
        )
        daily_rows = (await self.db.execute(daily_stmt)).all()
        daily_counts = []
        for row in daily_rows:
            trade_day = row.trade_day
            if not trade_day:
                continue
            day_text = trade_day.isoformat()
            daily_counts.append(
                {
                    "timestamp": f"{day_text}T00:00:00Z",
                    "value": int(row.trade_count or 0),
                    "label": "trade_count",
                }
            )

        result = {
            "daily_counts": daily_counts,
            "total_trades": int(summary_row.total_trades or 0),
            "total_value": float(summary_row.total_value or 0.0),
            "total_commission": float(summary_row.total_commission or 0.0),
            "buy_trades": int(summary_row.buy_trades or 0),
            "sell_trades": int(summary_row.sell_trades or 0),
            **(await self._realized_pnl_stats(conditions)),
        }
        if self.redis and getattr(self.redis, "client", None):
            try:
                self.redis.set(cache_key, result, ttl=300)
            except Exception:
                pass
        return result

    async def _realized_pnl_stats(self, conditions: list) -> dict:
        """按移动加权成本回放成交序列，统计已实现盈亏与胜率/盈亏比。

        口径：买入费用计入成本基底，卖出盈亏 = 卖出净额(扣费) - 平均成本×数量，
        每笔卖出计一次平仓；手续费全程计入盈亏。

        为避免超长历史全量回放拖慢统计接口，限制回放最近 N 笔成交
        （默认 5000；剩余开盘仓位不纳入已实现盈亏统计）。
        """
        stmt = (
            select(
                SimTrade.symbol,
                SimTrade.side,
                SimTrade.quantity,
                SimTrade.trade_value,
                SimTrade.total_fee,
            )
            .where(and_(*conditions))
            .order_by(SimTrade.executed_at.desc(), SimTrade.id.desc())
            .limit(5000)
        )
        rows = (await self.db.execute(stmt)).all()
        rows = list(reversed(rows))

        holdings: dict[str, dict[str, float]] = {}
        win_trades = 0
        loss_trades = 0
        total_win = 0.0
        total_loss = 0.0
        realized_pnl = 0.0

        for row in rows:
            fee = float(row.total_fee or 0.0)
            qty = float(row.quantity or 0.0)
            value = float(row.trade_value or 0.0)
            pos = holdings.setdefault(row.symbol, {"qty": 0.0, "cost": 0.0})
            side = row.side.value if hasattr(row.side, "value") else str(row.side)
            if side == "buy":
                pos["cost"] += value + fee
                pos["qty"] += qty
            else:
                avg_cost = pos["cost"] / pos["qty"] if pos["qty"] > 0 else 0.0
                pnl = (value - fee) - avg_cost * qty
                realized_pnl += pnl
                pos["cost"] = max(0.0, pos["cost"] - avg_cost * qty)
                pos["qty"] = max(0.0, pos["qty"] - qty)
                if pnl > 0:
                    win_trades += 1
                    total_win += pnl
                else:
                    loss_trades += 1
                    total_loss += abs(pnl)

        closed_trades = win_trades + loss_trades
        win_rate = win_trades / closed_trades if closed_trades > 0 else 0.0
        avg_win = total_win / win_trades if win_trades > 0 else 0.0
        avg_loss = total_loss / loss_trades if loss_trades > 0 else 0.0
        profit_loss_ratio = avg_win / avg_loss if avg_loss > 0 else 0.0

        return {
            "realized_pnl": round(realized_pnl, 2),
            "win_trades": win_trades,
            "loss_trades": loss_trades,
            "win_rate": round(win_rate, 4),
            "profit_loss_ratio": round(profit_loss_ratio, 4),
        }
