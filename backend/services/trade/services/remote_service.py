import logging

import httpx

from backend.services.trade_shared.models.trade import Trade
from backend.services.trade_shared.trade_config import settings
from backend.shared.auth import get_internal_call_secret

logger = logging.getLogger(__name__)


class RemoteService:
    """远程服务客户端"""

    def __init__(self):
        self.portfolio_base_url = settings.PORTFOLIO_SERVICE_URL
        self.timeout = httpx.Timeout(5.0, connect=2.0)

    async def get_portfolio_cash(self, portfolio_id: int, user_id: int) -> float:
        """
        获取投资组合可用资金
        """
        url = f"{self.portfolio_base_url}/api/v1/portfolios/{portfolio_id}"
        # 需要内部鉴权或透传当前请求的 Token。这里暂假设内部接口可通过或有默认鉴权。
        # 实际开发中通常会在 Header 加入 X-Internal-Call: secret

        headers = {
            "X-User-Id": str(user_id),
            "X-Internal-Call": get_internal_call_secret(),
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                data = response.json()
                # PortfolioResponse 包含 available_cash
                return float(data.get("available_cash", 0.0))
            except Exception as e:
                logger.error(f"Failed to fetch portfolio {portfolio_id} cash: {e}")
                return 0.0

    async def sync_trade_to_portfolio(self, trade: Trade):
        """
        同步成交信息到 Portfolio Service
        """
        url = f"{self.portfolio_base_url}/api/v1/internal/sync-trade"

        payload = {
            "portfolio_id": trade.portfolio_id,
            "symbol": trade.symbol,
            "side": trade.side.value,  # Enum to string
            "trade_action": trade.trade_action.value if trade.trade_action else None,
            "position_side": trade.position_side.value if trade.position_side else None,
            "is_margin_trade": bool(trade.is_margin_trade),
            "quantity": trade.quantity,
            "price": float(trade.price),
            "commission": float(trade.commission),
            "trade_id": str(trade.trade_id),
        }

        headers = {
            "X-User-Id": str(getattr(trade, "user_id", "") or ""),
            "X-Internal-Call": get_internal_call_secret(),
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                # Fire and forget mechanism or wait?
                # Since it's called from TradeService, we should probably await but catch errors so we don't block trading
                response = await client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                logger.info(f"Synced trade {trade.trade_id} to portfolio {trade.portfolio_id}")
            except Exception as e:
                logger.error(f"Failed to sync trade {trade.trade_id} to portfolio: {e}")
                # We do NOT raise exception here to avoid rolling back the trade execution
                # In production, this should go to a Dead Letter Queue or Retry Queue


remote_service = RemoteService()
