"""老虎证券桥接：tigeropen SDK（v3.7.1）——港股/美股 实盘+模拟盘。

- 凭据: tiger_id + rsa_private_key + account（config/brokers.json tiger 段，设置页填）
- 模拟盘: account 填模拟账户号自动识别（AccountUtil.is_paper_account），
  或显式 is_paper=true；测试消息标注模拟/实盘
- 3.7.1 注意：配置类用属性赋值（tiger_open_config.TigerOpenClientConfig）；
  OrderRequest 已废，用 create_order(account, contract, action, type, qty)；
  行情用 get_briefs/get_bars；资产在 PortfolioAccount.summary.cash
- 市场: symbol 纯代码（00700=港股 / AAPL=美股）
"""

import os
from typing import Any, Dict, List, Optional

from agent_tools.brokers.base import Broker, BrokerError


class TigerBridgeBroker(Broker):
    name = "tiger"
    markets = "both"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.tiger_id = self.config.get("tiger_id") or os.getenv("TIGEROPEN_TIGER_ID", "")
        # UI 存 rsa_private_key（tdx_live BROKER_FIELDS），旧代码读 private_key——两者都认
        self.private_key = (self.config.get("rsa_private_key")
                            or self.config.get("private_key")
                            or os.getenv("TIGEROPEN_PRIVATE_KEY", ""))
        self.account = self.config.get("account") or os.getenv("TIGEROPEN_ACCOUNT", "")
        # 模拟盘：显式 is_paper 或按账户号自动识别（AccountUtil.is_paper_account）
        self.is_paper = bool(self.config.get("is_paper")) or os.getenv("TIGEROPEN_IS_PAPER") == "1"
        if not self.is_paper and self.account:
            try:
                from tigeropen.common.util.account_util import AccountUtil

                self.is_paper = AccountUtil.is_paper_account(self.account)
            except Exception:  # noqa: BLE001
                pass

    # ---------- 客户端 ----------

    def _get_clients(self):
        if not self.tiger_id or not self.private_key:
            raise BrokerError("老虎证券未配置：tiger_id / rsa_private_key（设置页填写）")
        try:
            from tigeropen.tiger_open_config import TigerOpenClientConfig
            from tigeropen.quote.quote_client import QuoteClient
            from tigeropen.trade.trade_client import TradeClient

            config = TigerOpenClientConfig()
            config.tiger_id = self.tiger_id
            config.private_key = self.private_key
            if self.account:
                config.account = self.account
            if self.is_paper:
                config.is_paper = True
            return QuoteClient(config), TradeClient(config)
        except Exception as exc:
            raise BrokerError(f"老虎 SDK 初始化失败: {exc}") from exc

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
    def _currency(market: str) -> str:
        return "HKD" if market == "hk" else "USD"

    # ---------- 行情（3.7.1: get_briefs / get_bars） ----------

    def get_quote(self, symbol: str, date: str, market: str = "hk") -> Optional[Dict[str, Any]]:
        try:
            quote_client, _ = self._get_clients()
            briefs = quote_client.get_briefs([symbol]) or []
            if not briefs:
                return None
            b = briefs[0]
            price = (getattr(b, "latest_price", None) or getattr(b, "latest", None)
                     or getattr(b, "price", None) or 0)
            return {"symbol": symbol, "date": date, "buy price": float(price or 0)}
        except BrokerError:
            raise
        except Exception as exc:
            raise BrokerError(f"老虎行情查询失败: {exc}") from exc

    def get_klines(self, symbol: str, start: str, end: str, interval: str = "daily",
                   market: str = "hk") -> List[Dict[str, Any]]:
        try:
            quote_client, _ = self._get_clients()
            from tigeropen.common.consts import BarPeriod

            period = BarPeriod.DAY if interval == "daily" else BarPeriod.HOUR
            bars = quote_client.get_bars(symbol, period=period,
                                         begin_time=start, end_time=end) or []
            return [
                {"date": str(getattr(b, "time", ""))[:10],
                 "open": float(getattr(b, "open", 0) or 0),
                 "close": float(getattr(b, "close", 0) or 0),
                 "high": float(getattr(b, "high", 0) or 0),
                 "low": float(getattr(b, "low", 0) or 0),
                 "volume": float(getattr(b, "volume", 0) or 0)}
                for b in bars
            ]
        except BrokerError:
            raise
        except Exception as exc:
            raise BrokerError(f"老虎 K 线查询失败: {exc}") from exc

    # ---------- 交易（3.7.1: create_order + place_order） ----------

    def buy(self, signature: str, today_date: str, symbol: str, amount: int,
            price: Optional[float] = None, market: str = "hk") -> Dict[str, Any]:
        self._check_approval_gate()
        return self._place_order(symbol, amount, price, "BUY", market)

    def sell(self, signature: str, today_date: str, symbol: str, amount: int,
             price: Optional[float] = None, market: str = "hk") -> Dict[str, Any]:
        self._check_approval_gate()
        return self._place_order(symbol, amount, price, "SELL", market)

    def _place_order(self, symbol: str, amount: int, price: Optional[float],
                     action: str, market: str = "hk") -> Dict[str, Any]:
        if not self.account:
            raise BrokerError("老虎账户未配置：account（设置页填写）")
        try:
            from tigeropen.common.util.contract_utils import stock_contract

            _, trade_client = self._get_clients()
            contract = stock_contract(symbol=symbol, currency=self._currency(market))
            order_type = "LMT" if price else "MKT"
            order = trade_client.create_order(
                self.account, contract, action, order_type, int(amount),
                limit_price=float(price) if price else None)
            trade_client.place_order(order)
            return {"order_id": str(getattr(order, "order_id", "") or getattr(order, "id", "")),
                    "status": "submitted",
                    "message": f"老虎已受理: {symbol} {action} {amount}（{order_type}）"}
        except BrokerError:
            raise
        except Exception as exc:
            raise BrokerError(f"老虎下单失败: {exc}") from exc

    # ---------- 持仓 / 资金 / 委托（3.7.1: PortfolioAccount.summary / Position / Order） ----------

    def get_positions(self, signature: str, today_date: str,
                      market: str = "hk") -> Dict[str, Any]:
        """持仓查询：{symbol: {volume, cost_price, market_value, currency}}。"""
        if not self.account:
            raise BrokerError("老虎账户未配置：account（设置页填写）")
        try:
            from tigeropen.common.consts import Market as TigerMarket

            _, trade_client = self._get_clients()
            mkt = TigerMarket.HK if market == "hk" else TigerMarket.US
            positions = trade_client.get_positions(account=self.account, market=mkt) or []
            out: Dict[str, Any] = {}
            for p in positions:
                contract = getattr(p, "contract", None)
                sym = (getattr(contract, "symbol", "") if contract else "") or getattr(p, "symbol", "")
                qty = float(getattr(p, "quantity", 0) or 0)
                if not sym or qty <= 0:
                    continue
                out[sym] = {
                    "symbol": sym,
                    "volume": qty,
                    "cost_price": float(getattr(p, "average_cost", 0) or 0),
                    "market_value": float(getattr(p, "market_value", 0) or 0),
                    "currency": str(getattr(contract, "currency", "") if contract else ""),
                }
            return out
        except BrokerError:
            raise
        except Exception as exc:
            raise BrokerError(f"老虎持仓查询失败: {exc}") from exc

    def get_cash(self, signature: str, today_date: str) -> float:
        """资金查询：PortfolioAccount.summary.cash（3.7.1 结构）。"""
        if not self.account:
            raise BrokerError("老虎账户未配置：account（设置页填写）")
        try:
            _, trade_client = self._get_clients()
            assets = trade_client.get_assets(account=self.account) or []
            cash = 0.0
            for a in assets:
                s = getattr(a, "summary", None)
                if s is not None:
                    cash += float(getattr(s, "cash", 0) or 0)
                else:
                    d = a.to_dict() if hasattr(a, "to_dict") else {}
                    cash += float(d.get("cash") or d.get("available_funds") or 0)
            return cash
        except BrokerError:
            raise
        except Exception as exc:
            raise BrokerError(f"老虎资金查询失败: {exc}") from exc

    def get_orders(self, market: str = "hk", limit: int = 50) -> List[Dict[str, Any]]:
        """当日委托（含成交/在途），字段对齐前端消费形状。"""
        if not self.account:
            raise BrokerError("老虎账户未配置：account（设置页填写）")
        try:
            from tigeropen.common.consts import Market as TigerMarket

            _, trade_client = self._get_clients()
            mkt = TigerMarket.HK if market == "hk" else TigerMarket.US
            orders = trade_client.get_orders(account=self.account, market=mkt,
                                             limit=int(limit)) or []
            out = []
            for o in orders:
                contract = getattr(o, "contract", None)
                sym = (getattr(contract, "symbol", "") if contract else "") or getattr(o, "symbol", "")
                out.append({
                    "order_id": str(getattr(o, "order_id", "") or getattr(o, "id", "")),
                    "stock_code": sym,
                    "side": str(getattr(o, "action", "") or "").lower(),
                    "status": str(getattr(o, "status", "") or "").lower(),
                    "filled_volume": float(getattr(o, "filled", 0) or 0),
                    "filled_price": float(getattr(o, "avg_fill_price", 0) or 0),
                    "order_price": float(getattr(o, "limit_price", 0) or 0),
                    "time": str(getattr(o, "order_time", "") or "")[:19],
                })
            return out
        except BrokerError:
            raise
        except Exception as exc:
            raise BrokerError(f"老虎委托查询失败: {exc}") from exc

    def get_filled_orders(self, start_date: str, end_date: str,
                          market: str = "hk", limit: int = 200) -> List[Dict[str, Any]]:
        """历史成交（get_filled_orders，日期范围必填）——「已完成」历史数据源。"""
        if not self.account:
            raise BrokerError("老虎账户未配置：account（设置页填写）")
        try:
            from tigeropen.common.consts import Market as TigerMarket

            _, trade_client = self._get_clients()
            mkt = TigerMarket.HK if market == "hk" else TigerMarket.US
            orders = trade_client.get_filled_orders(
                account=self.account, market=mkt, limit=int(limit),
                start_time=start_date, end_time=end_date) or []
            out = []
            for o in orders:
                contract = getattr(o, "contract", None)
                sym = (getattr(contract, "symbol", "") if contract else "") or getattr(o, "symbol", "")
                out.append({
                    "order_id": str(getattr(o, "order_id", "") or getattr(o, "id", "")),
                    "stock_code": sym,
                    "side": str(getattr(o, "action", "") or "").lower(),
                    "status": str(getattr(o, "status", "") or "").lower(),
                    "filled_volume": float(getattr(o, "filled", 0) or 0),
                    "filled_price": float(getattr(o, "avg_fill_price", 0) or 0),
                    "time": str(getattr(o, "trade_time", "") or "")[:19],
                })
            return out
        except BrokerError:
            raise
        except Exception as exc:
            raise BrokerError(f"老虎历史成交查询失败: {exc}") from exc


def register() -> None:
    from agent_tools.brokers.base import registry

    registry.register(TigerBridgeBroker)


register()
