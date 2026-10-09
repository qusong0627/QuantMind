"""富途（FutuOpenD）Broker：经 OpenD 网关（默认 127.0.0.1:11111）对接富途。

支持市场：港股/美股/A股（OpenD 已开通权限的市场）。
配置（.env）：
  FUTU_OPEND_HOST=127.0.0.1
  FUTU_OPEND_PORT=11111
  FUTU_TRD_PWD=交易密码（下单必需，未配置时禁止下单）

安全：下单前过风控审批门（backend.yaml risk.approval_required）。
"""

import os
from typing import Any, Dict, List, Optional

from agent_tools.brokers.base import Broker, BrokerError


class FutuBridgeBroker(Broker):
    name = "futu"
    markets = "both"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.host = self.config.get("opend_host") or os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
        self.port = int(self.config.get("opend_port") or os.getenv("FUTU_OPEND_PORT", "11111"))
        self.trd_pwd = self.config.get("trd_pwd") or os.getenv("FUTU_TRD_PWD", "")
        self._quote = None
        self._trade = None

    # ---------- 连接 ----------

    def _get_quote_ctx(self):
        if self._quote is None:
            try:
                from futu import OpenQuoteContext

                self._quote = OpenQuoteContext(host=self.host, port=self.port, max_retry=1)
            except Exception as exc:
                raise BrokerError(f"富途 OpenD 连接失败（{self.host}:{self.port}）: {exc}") from exc
        return self._quote

    def _get_trade_ctx(self):
        if self._trade is None:
            if not self.trd_pwd:
                raise BrokerError("富途交易密码未配置（FUTU_TRD_PWD），禁止下单")
            try:
                from futu import OpenSecTradeContext, TrdMarket

                self._trade = OpenSecTradeContext(
                    filter_trdmarket=TrdMarket.HK | TrdMarket.US, host=self.host, port=self.port,
                    security_firm=0, max_retry=1,
                )
                # 解锁交易
                ret, _ = self._trade.unlock_trade(password=self.trd_pwd)
                if ret != 0:
                    raise BrokerError("富途交易解锁失败（密码错误或未开通）")
            except BrokerError:
                raise
            except Exception as exc:
                raise BrokerError(f"富途交易通道初始化失败: {exc}") from exc
        return self._trade

    @staticmethod
    def _check_approval_gate() -> None:
        """审批门：approval_required=true 时拒绝实盘下单。"""
        try:
            from agent_tools.risk import RiskPolicy

            if RiskPolicy.from_backend_config().approval_required:
                raise BrokerError("风控审批门开启（approval_required=true），实盘下单被拒绝")
        except BrokerError:
            raise
        except Exception:
            pass

    # ---------- 行情 ----------

    def get_quote(self, symbol: str, date: str, market: str = "hk") -> Optional[Dict[str, Any]]:
        try:
            from futu import RET_OK

            ctx = self._get_quote_ctx()
            # 富途代码格式：HK.00700 / US.AAPL
            ft_code = f"HK.{symbol.replace('.HK', '')}" if symbol.endswith(".HK") else (
                f"US.{symbol}" if market == "us" else symbol)
            ret, data = ctx.get_market_snapshot([ft_code])
            if ret != RET_OK or data.empty:
                return None
            row = data.iloc[0]
            return {
                "symbol": symbol, "date": date,
                "buy price": float(row.get("last_price", 0) or 0),
                "high": float(row.get("high_price", 0) or 0),
                "low": float(row.get("low_price", 0) or 0),
            }
        except Exception as exc:
            raise BrokerError(f"富途行情查询失败: {exc}") from exc

    def get_klines(self, symbol: str, start: str, end: str, interval: str = "daily",
                   market: str = "hk") -> List[Dict[str, Any]]:
        try:
            from futu import KLType, RET_OK

            ctx = self._get_quote_ctx()
            ft_code = f"HK.{symbol.replace('.HK', '')}" if symbol.endswith(".HK") else symbol
            ktype = KLType.K_DAY if interval == "daily" else KLType.K_60M
            ret, data = ctx.request_history_kline(ft_code, start=start, end=end, ktype=ktype)
            if ret != RET_OK or data is None or data.empty:
                return []
            return [
                {"date": row["time_key"], "open": float(row["open"]), "close": float(row["close"]),
                 "high": float(row["high"]), "low": float(row["low"]), "volume": float(row["volume"])}
                for _, row in data.iterrows()
            ]
        except Exception as exc:
            raise BrokerError(f"富途 K 线查询失败: {exc}") from exc

    # ---------- 交易 ----------

    def buy(self, signature: str, today_date: str, symbol: str, amount: int,
            price: Optional[float] = None) -> Dict[str, Any]:
        self._check_approval_gate()
        return self._place_order(symbol, amount, price, "buy")

    def sell(self, signature: str, today_date: str, symbol: str, amount: int,
             price: Optional[float] = None) -> Dict[str, Any]:
        self._check_approval_gate()
        return self._place_order(symbol, amount, price, "sell")

    def _place_order(self, symbol: str, amount: int, price: Optional[float],
                     side: str) -> Dict[str, Any]:
        try:
            from futu import OrderType, RET_OK, TrdEnv, TrdSide

            ctx = self._get_trade_ctx()
            ft_code = f"HK.{symbol.replace('.HK', '')}" if symbol.endswith(".HK") else (
                f"US.{symbol}" if not symbol.endswith((".SH", ".SZ")) else symbol)
            trd_side = TrdSide.BUY if side == "buy" else TrdSide.SELL
            order_type = OrderType.NORMAL if price else OrderType.MARKET
            ret, data = ctx.place_order(
                price=float(price) if price else 0.0,
                qty=int(amount),
                code=ft_code,
                trd_side=trd_side,
                order_type=order_type,
                trd_env=TrdEnv.SIMULATE,  # 默认模拟环境，实盘需显式切换
            )
            if ret != RET_OK:
                raise BrokerError(f"富途下单被拒: {data}")
            return {"order_id": str(data.iloc[0]["order_id"]), "status": "submitted",
                    "message": f"富途已受理（模拟环境）: {symbol} {side} {amount}"}
        except BrokerError:
            raise
        except Exception as exc:
            raise BrokerError(f"富途下单失败: {exc}") from exc

    def get_positions(self, signature: str, today_date: str) -> Dict[str, float]:
        try:
            from futu import RET_OK, TrdEnv

            ctx = self._get_trade_ctx()
            ret, data = ctx.position_list_query(trd_env=TrdEnv.SIMULATE)
            if ret != RET_OK or data is None or data.empty:
                return {}
            return {row["code"]: float(row["qty"]) for _, row in data.iterrows() if float(row["qty"]) > 0}
        except Exception as exc:
            raise BrokerError(f"富途持仓查询失败: {exc}") from exc

    def get_cash(self, signature: str, today_date: str) -> float:
        try:
            from futu import RET_OK, TrdEnv

            ctx = self._get_trade_ctx()
            ret, data = ctx.accinfo_query(trd_env=TrdEnv.SIMULATE)
            if ret != RET_OK or data is None or data.empty:
                return 0.0
            return float(data.iloc[0].get("cash", 0) or 0)
        except Exception as exc:
            raise BrokerError(f"富途资金查询失败: {exc}") from exc


def register() -> None:
    from agent_tools.brokers.base import registry

    registry.register(FutuBridgeBroker)


register()
