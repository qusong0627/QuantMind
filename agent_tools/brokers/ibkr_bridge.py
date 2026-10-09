"""IBKR（盈透证券）桥接：ib_insync + IB Gateway。

- 连接: gateway_host/gateway_port/client_id（UI 设置页存 gateway_* 键）
- 每个操作在独立线程+独立事件循环里跑（py3.12+ eventkit import 需要 loop；
  后端 async 环境不能 set_event_loop 污染主循环 → 线程隔离一劳永逸）
- 持仓/资金/委托/行情/下单全套；下单前过风控审批门
- 市场: US 为主（AAPL→SMART/USD）；港股 0700.HK→SMART/HKD；A股 CNH
"""

import os
import threading
from typing import Any, Dict, List, Optional

from agent_tools.brokers.base import Broker, BrokerError


class IbkrBridgeBroker(Broker):
    name = "ibkr"
    markets = "both"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        # UI 存 gateway_host/gateway_port（tdx_live BROKER_FIELDS），旧代码读 host/port——两者都认
        self.host = (self.config.get("gateway_host") or self.config.get("host")
                     or os.getenv("IBKR_HOST", "127.0.0.1"))
        self.port = int(self.config.get("gateway_port") or self.config.get("port")
                        or os.getenv("IBKR_PORT", "7497"))
        self.client_id = int(self.config.get("client_id") or os.getenv("IBKR_CLIENT_ID", "1"))

    # ---------- 连接（独立线程 + 独立事件循环） ----------

    def _with_ib(self, fn, timeout: float = 30.0):
        """在独立线程+事件循环里连接 IB 并执行 fn(ib)，用完断开。
        eventkit 在 py3.12+ 无 loop 时 import 报错；async 环境不能 set_event_loop——
        线程隔离两种场景都安全。"""
        import asyncio

        result: dict = {}

        def worker():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            ib = None
            try:
                from ib_insync import IB

                ib = IB()
                if not ib.connect(self.host, self.port, clientId=self.client_id,
                                  timeout=10, readonly=False):
                    raise BrokerError(f"IBKR 连接失败（{self.host}:{self.port}）")
                result["v"] = fn(ib)
            except Exception as exc:  # noqa: BLE001
                result["e"] = exc
            finally:
                try:
                    if ib is not None:
                        ib.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    loop.close()
                except Exception:  # noqa: BLE001
                    pass

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            raise BrokerError("IBKR 调用超时（Gateway 未响应）")
        if "e" in result:
            e = result["e"]
            if isinstance(e, BrokerError):
                raise e
            raise BrokerError(f"IBKR 调用失败: {e}") from e
        return result.get("v")

    @staticmethod
    def _check_approval_gate() -> None:
        try:
            from agent_tools.risk import RiskPolicy

            if RiskPolicy.from_backend_config().approval_required:
                raise BrokerError("风控审批门开启（approval_required=true），实盘下单被拒绝")
        except BrokerError:
            raise
        except Exception:
            pass

    @staticmethod
    def _stock(symbol: str):
        from ib_insync import Stock

        # IBKR 代码：AAPL(美股) / 0700.HK(港股，需 SEHK 交易所 + 去前导零) / 600519.SH(A股)
        if symbol.endswith(".HK"):
            # 港股 IBKR 标准：无前导零（0700 → 700），交易所 SEHK（SMART 找不到定义）
            sym = symbol.replace(".HK", "").lstrip("0") or "0"
            return Stock(sym, "SEHK", "HKD")
        if symbol.endswith((".SH", ".SZ")):
            return Stock(symbol, "SMART", "CNH")
        return Stock(symbol, "SMART", "USD")

    # ---------- 行情 ----------

    def get_quote(self, symbol: str, date: str, market: str = "us") -> Optional[Dict[str, Any]]:
        try:
            return self._with_ib(lambda ib: self._quote(ib, symbol, date))
        except BrokerError:
            raise
        except Exception as exc:
            raise BrokerError(f"IBKR 行情查询失败: {exc}") from exc

    def _quote(self, ib, symbol: str, date: str) -> Optional[Dict[str, Any]]:
        import math

        ticker = ib.reqMktData(self._stock(symbol), "", False, False)
        ib.sleep(1.5)
        price = ticker.marketPrice()
        ib.cancelMktData(ticker.contract)
        if not price or not math.isfinite(float(price)) or float(price) <= 0:
            return None
        return {"symbol": symbol, "date": date, "buy price": float(price)}

    def get_klines(self, symbol: str, start: str, end: str, interval: str = "daily",
                   market: str = "us") -> List[Dict[str, Any]]:
        try:
            return self._with_ib(lambda ib: self._klines(ib, symbol, start, end, interval))
        except BrokerError:
            raise
        except Exception as exc:
            raise BrokerError(f"IBKR K 线查询失败: {exc}") from exc

    def _klines(self, ib, symbol: str, start: str, end: str, interval: str) -> List[Dict[str, Any]]:
        from ib_insync import util

        # IBKR 要求 yyyymmdd hh:mm:ss 或留空（=当前）；"2026-09-01" 会被拒
        end_dt = end.replace("-", "") + " 23:59:59" if end else ""
        bars = ib.reqHistoricalData(
            self._stock(symbol), endDateTime=end_dt, durationStr="1 Y",
            barSizeSetting="1 day" if interval == "daily" else "1 hour",
            whatToShow="TRADES", useRTH=True,
        )
        return [
            {"date": str(getattr(b, "date", ""))[:10],
             "open": float(getattr(b, "open", 0) or 0),
             "close": float(getattr(b, "close", 0) or 0),
             "high": float(getattr(b, "high", 0) or 0),
             "low": float(getattr(b, "low", 0) or 0),
             "volume": float(getattr(b, "volume", 0) or 0)}
            for b in (bars or [])
        ]

    # ---------- 交易 ----------

    def buy(self, signature: str, today_date: str, symbol: str, amount: int,
            price: Optional[float] = None) -> Dict[str, Any]:
        self._check_approval_gate()
        return self._place_order(symbol, amount, price, "BUY")

    def sell(self, signature: str, today_date: str, symbol: str, amount: int,
             price: Optional[float] = None) -> Dict[str, Any]:
        self._check_approval_gate()
        return self._place_order(symbol, amount, price, "SELL")

    def _place_order(self, symbol: str, amount: int, price: Optional[float],
                     action: str) -> Dict[str, Any]:
        def _do(ib):
            from ib_insync import LimitOrder, MarketOrder

            order = (LimitOrder(action, int(amount), float(price)) if price
                     else MarketOrder(action, int(amount)))
            trade = ib.placeOrder(self._stock(symbol), order)
            ib.sleep(2)
            status = trade.orderStatus.status
            if status in ("Cancelled", "Inactive"):
                raise BrokerError(f"IBKR 订单被拒: {status} {trade.orderStatus.whyHeld or ''}")
            return {"order_id": str(trade.order.orderId), "status": status,
                    "message": f"IBKR 已受理: {symbol} {action} {amount}"}

        try:
            return self._with_ib(_do)
        except BrokerError:
            raise
        except Exception as exc:
            raise BrokerError(f"IBKR 下单失败: {exc}") from exc

    # ---------- 持仓 / 资金 / 委托 ----------

    def get_positions(self, signature: str, today_date: str) -> Dict[str, float]:
        try:
            return self._with_ib(
                lambda ib: {p.contract.symbol: float(p.position)
                            for p in ib.positions() if p.position})
        except BrokerError:
            raise
        except Exception as exc:
            raise BrokerError(f"IBKR 持仓查询失败: {exc}") from exc

    def get_cash(self, signature: str, today_date: str) -> float:
        try:
            def _cash(ib):
                total = 0.0
                for v in ib.accountSummary():
                    if v.tag == "TotalCashValue":
                        total += float(v.value or 0)
                return total

            return self._with_ib(_cash)
        except BrokerError:
            raise
        except Exception as exc:
            raise BrokerError(f"IBKR 资金查询失败: {exc}") from exc

    def get_orders(self, limit: int = 50) -> List[Dict[str, Any]]:
        """在途委托（openTrades），字段对齐前端消费形状。"""
        def _orders(ib):
            out = []
            for t in ib.openTrades()[: int(limit)]:
                out.append({
                    "order_id": str(t.order.orderId),
                    "stock_code": t.contract.symbol,
                    "side": str(t.order.action or "").lower(),
                    "status": str(t.orderStatus.status or "").lower(),
                    "filled_volume": float(t.orderStatus.filled or 0),
                    "filled_price": float(t.orderStatus.avgFillPrice or 0),
                    "order_price": float(t.order.lmtPrice or 0),
                    "time": str(t.order.lastFillTime or "")[:19],
                })
            return out

        try:
            return self._with_ib(_orders)
        except BrokerError:
            raise
        except Exception as exc:
            raise BrokerError(f"IBKR 委托查询失败: {exc}") from exc


def register() -> None:
    from agent_tools.brokers.base import registry

    registry.register(IbkrBridgeBroker)


register()
