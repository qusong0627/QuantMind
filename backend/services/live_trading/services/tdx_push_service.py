"""
TdxPushService - QuantMind ↔ 通达信 双向推送服务

推送 (Q→T): 选股信号写板块 / 预警信号 / 文本消息 / 策略源码 / 交易下单
拉取 (T→Q): 账户资产 / 持仓明细 / 当日委托 / 单笔委托状态 / 盈亏计算

所有请求经 Windows 桥 (TDX_BRIDGE_URL:8550) 转发到通达信客户端。
受 ENABLE_TDX_PUSH 开关控制, 未配置 token 时安全降级不报错。
"""
import asyncio
import logging
import os
import uuid
from typing import Any, Optional

from backend.shared.database_manager_v2 import get_session

logger = logging.getLogger(__name__)

TDX_BRIDGE_URL = os.getenv("TDX_BRIDGE_URL", "http://192.168.31.31:8550")
TDX_BRIDGE_TOKEN = os.getenv("TDX_BRIDGE_TOKEN", "")
TIMEOUT = 10.0

# A股费用估算（桥委托不含费用字段，按标准费率估算用于交易记录统计）
COMMISSION_RATE = 0.00025  # 佣金 万2.5，双边收取
COMMISSION_MIN = 5.0  # 佣金最低 5 元/笔
STAMP_TAX_RATE = 0.0005  # 印花税 万5，仅卖出单边（2023-08-28 起）
TRANSFER_FEE_RATE = 0.00001  # 过户费 万0.1，双边收取


def estimate_order_fee(filled_value: float, side: str = "buy") -> float:
    """估算 A股单笔委托总费用 = 佣金 + 印花税(仅卖出) + 过户费。"""
    if filled_value <= 0:
        return 0.0
    commission = max(filled_value * COMMISSION_RATE, COMMISSION_MIN)
    stamp_tax = filled_value * STAMP_TAX_RATE if side == "sell" else 0.0
    transfer_fee = filled_value * TRANSFER_FEE_RATE
    return round(commission + stamp_tax + transfer_fee, 2)


class TdxPushError(Exception):
    """通达信推送失败"""


def _batch_quantdb_last_close(symbols: list[str]) -> dict[str, float]:
    """批量从 QuantDB 本地日线读取最近交易日收盘价（与模拟撮合同源）。"""
    if not symbols:
        return {}
    result: dict[str, float] = {}
    try:
        from backend.services.simulation.services.local_market_data import (
            get_local_market_data,
        )
        from backend.shared.stock_utils import StockCodeUtil

        # 进程内共享实例：复用交易日枚举与按日行情缓存
        market_data = get_local_market_data()
        latest_date = market_data.latest_trade_date()
        if latest_date is None:
            return result
        for symbol in symbols:
            suffix = StockCodeUtil.to_suffix(symbol)
            if not suffix:
                continue
            bar = market_data.get_bar(suffix, latest_date)
            if bar is not None and bar.close > 0:
                result[symbol] = float(bar.close)
    except Exception as exc:
        logger.warning("[TdxSync] QuantDB 收盘价补全失败: %s", exc)
    return result


class TdxPushService:
    def __init__(self, bridge_url: str = "", bridge_token: str = ""):
        self.bridge_url = str(bridge_url or TDX_BRIDGE_URL).rstrip("/")
        self.bridge_token = str(bridge_token or TDX_BRIDGE_TOKEN).strip()
        self._client = None

    @property
    def enabled(self) -> bool:
        return bool(self.bridge_url) and bool(self.bridge_token)

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {self.bridge_token}",
        }

    async def _client_http(self):
        if self._client is None:
            import httpx
            self._client = httpx.AsyncClient(timeout=TIMEOUT)
        return self._client

    async def _post(self, path: str, payload: dict) -> dict:
        if not self.enabled:
            logger.warning("[TdxPush] TDX_BRIDGE_URL/TOKEN 未配置, 跳过 %s", path)
            raise TdxPushError("TDX_BRIDGE_URL/TOKEN 未配置")
        client = await self._client_http()
        resp = await client.post(
            f"{self.bridge_url}{path}", json=payload, headers=self._headers())
        if resp.status_code != 200:
            raise TdxPushError(f"桥返回 HTTP {resp.status_code}: {resp.text}")
        return resp.json()

    # ============ 推送 (Q→T) ============

    async def push_signals_to_block(self, stocks: list, block_code: str = "",
                                    block_name: str = "QuantMind今日选股",
                                    show: bool = True) -> dict:
        """把选股结果写入通达信自定义板块."""
        return await self._post("/api/v1/push/block", {
            "block_code": block_code, "stocks": stocks, "show": show})

    async def push_warnings(self, signals: list[dict]) -> dict:
        """推送买卖预警信号 (通达信支持双击闪电下单).

        signals 元素: {symbol, side(buy/sell), price, close, volume, reason}
        """
        stock_list = [s.get("symbol", "") for s in signals]
        bs_map = {"buy": "0", "sell": "1"}
        return await self._post("/api/v1/push/warnings", {
            "stock_list": stock_list,
            "price_list": [str(s.get("price", "0")) for s in signals],
            "close_list": [str(s.get("close", "0")) for s in signals],
            "volum_list": [str(s.get("volume", "0")) for s in signals],
            "bs_flag_list": [bs_map.get(str(s.get("side", "")).lower(), "2") for s in signals],
            "warn_type_list": ["1"] * len(signals),
            "reason_list": [s.get("reason", "")[:25] for s in signals],
            "count": len(signals),
        })

    async def push_message(self, msg: str) -> dict:
        """推送文本消息到通达信界面."""
        return await self._post("/api/v1/push/message", {"msg": msg})

    async def push_source(self, py_code: str, handle_type: int = 0) -> dict:
        """推送策略源码到通达信云回测平台."""
        return await self._post("/api/v1/push/source", {
            "py_code": py_code, "handle_type": handle_type})

    async def place_order(self, stock_code: str, side: str, volume: int,
                          price: float | None = None,
                          price_type: int | None = None,
                          plan_id: str = "") -> dict:
        """通过桥下单到通达信."""
        order = {
            "stock_code": stock_code,
            "side": side,
            "volume": volume,
            "order_type": "limit" if price else "market",
            "price_type": price_type if price_type is not None else (0 if price else 1),
            "price": price,
        }
        return await self._post("/api/v1/plans/execute", {
            "plan_id": plan_id or f"qm_{int(__import__('time').time())}",
            "orders": [order],
        })

    # ============ 拉取 (T→Q) ============

    async def pull_account(self) -> dict:
        """拉取通达信账户资产."""
        data = await self._post("/api/v1/account/query", {})
        return data.get("asset", {})

    async def pull_positions(self) -> list:
        """拉取通达信持仓明细."""
        data = await self._post("/api/v1/account/query", {})
        return data.get("positions", [])

    async def pull_orders(self, stock_code: str = "") -> list:
        """拉取通达信当日委托."""
        data = await self._post("/api/v1/orders/query", {"stock_code": stock_code})
        return data.get("orders", [])

    async def cancel_order(self, stock_code: str, order_id: str) -> dict:
        """撤单（桥 /api/v1/orders/cancel → 通达信 cancel_order_stock）。

        返回 {"success": bool, "message": str}；撤单成功不代表原委托一定未成交,
        调用方应随后 query 一次当日委托确认最终状态。
        """
        return await self._post("/api/v1/orders/cancel", {
            "stock_code": stock_code,
            "order_id": order_id,
        })

    async def tdx_call(self, method: str, params: dict | None = None) -> dict:
        """通用 JSON-RPC 透传（桥 /api/v1/tdx/call，白名单方法）。

        用于实时行情（get_market_snapshot 等）拉取。返回桥 result 字典。
        """
        resp = await self._post("/api/v1/tdx/call", {
            "method": method,
            "params": params or {},
        })
        result = resp.get("result") if isinstance(resp, dict) else resp
        return result if isinstance(result, dict) else {"result": result}

    async def pull_order_status(self, wtbh: str) -> dict:
        """按委托编号查单笔委托状态."""
        orders = await self.pull_orders()
        for o in orders:
            if str(o.get("order_id", "")) == str(wtbh):
                return o
        return {}

    async def pull_pnl(self) -> dict:
        """拉取账户并计算盈亏.

        返回: {total_asset, cash, market_value, balance, positions, pnl_by_pos, total_pnl}
        """
        data = await self._post("/api/v1/account/query", {})
        asset = data.get("asset", {})
        positions = data.get("positions", [])
        pnl_list = []
        total_pnl = 0.0
        for p in positions:
            cost = float(p.get("cost_price", 0) or 0)
            # 成本价 x 总持仓 与 持仓市值 的差
            market_value = float(p.get("market_value", 0) or 0)
            pnl = market_value - cost * float(p.get("total_volume", 0) or 0)
            pnl_list.append({"stock_code": p.get("stock_code", ""),
                             "cost_price": cost,
                             "volume": p.get("total_volume", 0),
                             "market_value": market_value,
                             "pnl": round(pnl, 2)})
            total_pnl += pnl
        return {
            "total_asset": asset.get("asset", 0),
            "cash": asset.get("cash", 0),
            "market_value": asset.get("market_value", 0),
            "balance": asset.get("balance", 0),
            "positions": pnl_list,
            "total_pnl": round(total_pnl, 2),
        }

    async def sync_account_to_pg(
        self,
        *,
        tenant_id: str = "default",
        user_id: str = "",
    ) -> dict[str, Any]:
        """拉取通达信账户/持仓并落库到 real_account_snapshots，供前端 /account 读取。

        返回: {success, account_id, total_asset, cash, market_value, position_count}
        """
        if not self.enabled:
            return {"success": False, "error": "TDX_BRIDGE_URL/TOKEN 未配置"}
        data = await self._post("/api/v1/account/query", {})
        asset = data.get("asset", {}) or {}
        positions_raw = data.get("positions", []) or []
        # 桥返回的持仓缺少现价/市值，用 QuantDB 最近收盘价补全（与模拟撮合同源）
        price_map = await asyncio.to_thread(
            _batch_quantdb_last_close,
            [str(p.get("stock_code") or "").strip() for p in positions_raw],
        )
        positions = []
        for p in positions_raw:
            symbol = str(p.get("stock_code") or "").strip()
            if not symbol:
                continue
            volume = int(p.get("total_volume") or 0)
            price = float(p.get("current_price") or p.get("price") or 0)
            if price <= 0:
                price = price_map.get(symbol, 0.0)
            positions.append(
                {
                    "symbol": symbol,
                    "name": str(p.get("stock_name") or "").strip(),
                    "volume": volume,
                    "available_volume": int(p.get("available_volume") or 0),
                    "cost_price": float(p.get("cost_price") or 0),
                    "price": price,
                    "market_value": round(volume * price, 2),
                }
            )
        total_asset = float(asset.get("asset") or 0)
        cash = float(asset.get("cash") or 0)
        # 桥的 market_value 可能为 0，按持仓市值累加
        market_value = float(asset.get("market_value") or 0)
        if market_value <= 0 and positions:
            market_value = round(sum(float(x.get("market_value") or 0) for x in positions), 2)

        # 零资产守卫：桥返回空账户（开盘前后/桥未就绪）时不落库，
        # 避免 0 资产快照污染日终账本与收益计算（与 qmt 链路 is_inconsistent_zero_total_snapshot 同语义）。
        if total_asset <= 0 and cash <= 0 and market_value <= 0:
            logger.info(
                "[TdxPush] 桥返回空账户 asset=%.2f cash=%.2f mv=%.2f，跳过落库",
                total_asset,
                cash,
                market_value,
            )
            return {
                "success": True,
                "skipped": True,
                "reason": "empty_account",
                "total_asset": total_asset,
                "cash": cash,
                "market_value": market_value,
                "position_count": len(positions),
            }

        from datetime import date, datetime, timezone

        from sqlalchemy import insert

        from backend.services.trade_shared.models.real_account_snapshot import (
            RealAccountSnapshot,
        )

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        account_id = f"tdx-{tenant_id}-{user_id}"
        snapshot_date = now.date()
        snapshot_month = snapshot_date.strftime("%Y-%m")
        payload = {
            "positions": positions,
            "source": "tdx_bridge",
            "broker_type": "tdx",
        }
        async with get_session() as db:
            await db.execute(
                insert(RealAccountSnapshot).values(
                    tenant_id=tenant_id,
                    user_id=user_id or "0",
                    account_id=account_id,
                    snapshot_at=now,
                    snapshot_date=snapshot_date,
                    snapshot_month=snapshot_month,
                    total_asset=total_asset,
                    cash=cash,
                    market_value=market_value,
                    today_pnl_raw=0.0,
                    total_pnl_raw=0.0,
                    floating_pnl_raw=0.0,
                    source="tdx_bridge",
                    payload_json=payload,
                )
            )
            await self._sync_orders_to_pg(
                db=db,
                tenant_id=tenant_id,
                user_id=user_id,
                now=now,
            )
            # —— 日终账本同步（与 qmt 链路共用 upsert_real_account_daily_ledger）——
            # tdx 桥不提供当日盈亏，用派生口径：今日盈亏 = 总资产 - 上一交易日收盘权益；
            # 首日无上一交易日则退化到今日首条快照。口径与 internal_strategy_utils 一致。
            from sqlalchemy import select

            from backend.services.trade.services.real_account_ledger_service import (
                upsert_real_account_daily_ledger,
            )

            async def _first_asset(*where) -> float | None:
                stmt = (
                    select(RealAccountSnapshot.total_asset)
                    .where(
                        RealAccountSnapshot.tenant_id == tenant_id,
                        RealAccountSnapshot.user_id == (user_id or "0"),
                        RealAccountSnapshot.account_id == account_id,
                        *where,
                    )
                    .order_by(
                        RealAccountSnapshot.snapshot_at.asc(),
                        RealAccountSnapshot.id.asc(),
                    )
                    .limit(1)
                )
                row = (await db.execute(stmt)).first()
                return float(row[0]) if row and row[0] else None

            prev_day_stmt = (
                select(RealAccountSnapshot.total_asset)
                .where(
                    RealAccountSnapshot.tenant_id == tenant_id,
                    RealAccountSnapshot.user_id == (user_id or "0"),
                    RealAccountSnapshot.account_id == account_id,
                    RealAccountSnapshot.snapshot_date < snapshot_date,
                )
                .order_by(
                    RealAccountSnapshot.snapshot_at.desc(),
                    RealAccountSnapshot.id.desc(),
                )
                .limit(1)
            )
            prev_row = (await db.execute(prev_day_stmt)).first()
            prev_close_equity = (
                float(prev_row[0]) if prev_row and prev_row[0] else None
            )
            day_open_equity = prev_close_equity or await _first_asset(
                RealAccountSnapshot.snapshot_date == snapshot_date
            )
            month_open_equity = await _first_asset(
                RealAccountSnapshot.snapshot_month == snapshot_month
            )
            initial_equity = await _first_asset() or total_asset
            today_pnl = total_asset - (day_open_equity or total_asset)
            total_pnl = total_asset - (initial_equity or total_asset)

            await upsert_real_account_daily_ledger(
                db,
                tenant_id=tenant_id,
                user_id=user_id or "0",
                account_id=account_id,
                snapshot_at=now,
                snapshot_date=snapshot_date,
                total_asset=total_asset,
                cash=cash,
                market_value=market_value,
                initial_equity=initial_equity or total_asset,
                day_open_equity=day_open_equity or total_asset,
                month_open_equity=month_open_equity or total_asset,
                today_pnl=today_pnl,
                total_pnl=total_pnl,
                floating_pnl=0.0,
                position_count=len(positions),
                source="tdx_bridge",
                payload_json=payload,
            )
            await db.commit()
        logger.info(
            "[TdxPush] 通达信账户已落库 PG: asset=%.2f cash=%.2f positions=%d",
            total_asset,
            cash,
            len(positions),
        )
        return {
            "success": True,
            "account_id": account_id,
            "total_asset": total_asset,
            "cash": cash,
            "market_value": market_value,
            "position_count": len(positions),
        }

    async def _sync_orders_to_pg(
        self,
        *,
        db,
        tenant_id: str,
        user_id: str,
        now,
    ) -> None:
        """把通达信当日委托同步到 orders 表（REAL 模式交易记录展示）。

        幂等 + 增量修正：按 exchange_order_id（桥的委托编号）去重；
        已存在的行用桥的最新状态/成交回报刷新。桥是真实成交的权威来源，
        若只插不更新，订单会永远停在 SUBMITTED，随后被超时扫描器误判为
        EXPIRED（表现为"交易记录全部已过期、成交为 0"）。
        """
        try:
            from sqlalchemy import select, text

            from backend.services.trade_shared.models.enums import (
                OrderSide,
                OrderStatus,
                OrderType,
                TradingMode,
            )
            from backend.services.trade_shared.models.order import Order

            orders = await self.pull_orders()
            if not orders:
                return

            # 桥委托的 order_id 是字符串（如 "160356"），映射为 exchange_order_id
            existing = {
                str(r[0]): str(r[1])
                for r in (
                    await db.execute(
                        select(Order.exchange_order_id, Order.order_id).where(
                            Order.exchange_order_id.is_not(None),
                            Order.tenant_id == tenant_id,
                            Order.user_id == str(user_id),
                        )
                    )
                ).fetchall()
            }

            def _map_bridge_status(status: str) -> OrderStatus:
                if status in ("filled",):
                    return OrderStatus.FILLED
                if status in ("partial", "partial_fill", "partially_filled"):
                    return OrderStatus.PARTIALLY_FILLED
                if status in ("cancel", "cancelled", "partial_cancelled"):
                    return OrderStatus.CANCELLED
                if status in ("rejected",):
                    return OrderStatus.REJECTED
                return OrderStatus.SUBMITTED

            for o in orders:
                exchange_id = str(o.get("order_id") or "").strip()
                if not exchange_id:
                    continue
                symbol = str(o.get("stock_code") or "").strip()
                if not symbol:
                    continue
                side = str(o.get("side") or "buy").strip().lower()
                status = str(o.get("status") or "pending").strip().lower()
                # 桥已修复方向解析: buy/sell/cancel(方向未知, 保守默认 buy)
                order_side = OrderSide.SELL if side == "sell" else OrderSide.BUY
                order_status = _map_bridge_status(status)

                time_hhmm = str(o.get("time") or "").strip()
                submitted_at = now
                if len(time_hhmm) >= 4:
                    try:
                        submitted_at = now.replace(
                            hour=int(time_hhmm[:2]),
                            minute=int(time_hhmm[2:4]),
                            second=0,
                            microsecond=0,
                        )
                    except (ValueError, TypeError):
                        pass
                order_type = (
                    OrderType.LIMIT
                    if float(o.get("order_price") or 0) > 0
                    else OrderType.MARKET
                )
                total_volume = float(o.get("total_volume") or 0)
                filled_volume = float(o.get("filled_volume") or 0)
                price = float(o.get("order_price") or 0)
                filled_price = float(o.get("filled_price") or price)
                filled_value = round(filled_volume * filled_price, 2)
                fee = estimate_order_fee(filled_value, side)
                filled_at = (
                    submitted_at if order_status == OrderStatus.FILLED else None
                )

                existing_id = existing.get(exchange_id)
                if existing_id:
                    # 已存在 → 用桥最新状态刷新（成交回报追平，避免被超时扫描器误标过期）
                    await db.execute(
                        text(
                            """
                            UPDATE orders SET
                                status = :status,
                                filled_quantity = :filled_quantity,
                                average_price = :average_price,
                                filled_value = :filled_value,
                                filled_at = :filled_at,
                                commission = :commission,
                                remarks = '通达信桥委托'
                            WHERE order_id = :order_id
                            """
                        ),
                        {
                            "status": order_status.value,
                            "filled_quantity": filled_volume,
                            "average_price": filled_price if filled_volume > 0 else None,
                            "filled_value": filled_value,
                            "filled_at": filled_at,
                            "commission": fee,
                            "order_id": existing_id,
                        },
                    )
                    continue

                result = await db.execute(
                    text(
                        """
                        INSERT INTO orders (
                            order_id, tenant_id, user_id, portfolio_id, strategy_id, symbol,
                            side, trade_action, position_side, is_margin_trade,
                            order_type, trading_mode, status,
                            quantity, filled_quantity, price, average_price,
                            order_value, filled_value, commission,
                            submitted_at, filled_at,
                            client_order_id, exchange_order_id, remarks
                        ) VALUES (
                            :order_id, :tenant_id, :user_id, :portfolio_id, NULL, :symbol,
                            :side, :trade_action, :position_side, FALSE,
                            :order_type, :trading_mode, :status,
                            :quantity, :filled_quantity, :price, :average_price,
                            :order_value, :filled_value, :commission,
                            :submitted_at, :filled_at,
                            :client_order_id, :exchange_order_id, :remarks
                        )
                        RETURNING order_id
                        """
                    ),
                    {
                        "order_id": uuid.uuid4(),
                        "tenant_id": tenant_id,
                        "user_id": user_id or "0",
                        "portfolio_id": 0,
                        "symbol": symbol,
                        "side": order_side.value,
                        # PG tradeaction enum: OPEN/CLOSE（与 Python 命名不同）
                        "trade_action": "OPEN" if order_side == OrderSide.BUY else "CLOSE",
                        # PG positionside enum: LONG/SHORT（大写）
                        "position_side": "LONG",
                        "order_type": order_type.value,
                        "trading_mode": TradingMode.REAL.value,
                        "status": order_status.value,
                        "quantity": total_volume,
                        "filled_quantity": filled_volume,
                        "price": price if price > 0 else None,
                        "average_price": filled_price if filled_volume > 0 else None,
                        "order_value": round(total_volume * price, 2),
                        "filled_value": filled_value,
                        "commission": fee,
                        "submitted_at": submitted_at,
                        "filled_at": filled_at,
                        "client_order_id": f"tdx-{exchange_id}",
                        "exchange_order_id": exchange_id,
                        "remarks": "通达信桥委托",
                    },
                )
                new_id = result.scalar()
                if new_id:
                    existing[exchange_id] = str(new_id)
            logger.info("[TdxSync] 通达信委托落库 %d 笔 (user=%s)", len(orders), user_id)
        except Exception as exc:
            logger.warning("[TdxSync] 通达信委托落库失败: %s", exc)

    async def check_order_success(self, wtbh: str) -> dict:
        """检查下单是否成功.

        返回: {wtbh, status_code, status_text, filled, all_filled}
          status_code: 0无效/1未成交/2部分成交/3全部成交/4部分撤/5全撤
        """
        order = await self.pull_order_status(wtbh)
        if not order:
            return {"wtbh": wtbh, "status_code": -1, "status_text": "未找到", "filled": False}
        code = int(order.get("status", -1))
        status_map = {0: "无效单", 1: "未成交", 2: "部分成交", 3: "全部成交",
                      4: "部分成交部分撤单", 5: "全部撤单"}
        filled = code in (2, 3)
        return {
            "wtbh": wtbh,
            "status_code": code,
            "status_text": status_map.get(code, "未知"),
            "filled": filled,
            "all_filled": code == 3,
            "filled_price": order.get("filled_price", 0),
            "filled_volume": order.get("filled_volume", 0),
        }


# 全局单例
tdx_pusher = TdxPushService()
