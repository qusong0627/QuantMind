import asyncio
import logging
import os
from datetime import datetime
from typing import Any, Dict, List

from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.services.trade.deps import get_redis
from backend.services.trade.services.simulation_manager import SimulationAccountManager

logger = logging.getLogger(__name__)


class SimulationSettler:
    """
    模拟交易结算器：负责无需 Pod 的“公式化”模拟盘运行逻辑
    """

    async def run_daily_settlement(self, db: Session, user_id: int, strategy_id: str):
        """
        执行一次模拟盘的“每日步进”
        """
        logger.info(f"[SimSettle] 开始结算用户 {user_id} 的策略 {strategy_id}...")

        # 获取 Redis 客户端
        redis = get_redis()
        sim_manager = SimulationAccountManager(redis)

        # 1. 获取当前账户状态
        account_data = await sim_manager.get_account(user_id)
        if not account_data:
            logger.error(f"找不到用户 {user_id} 的模拟账户")
            return

        # 2. 从 Engine 获取策略目标权重
        target_weights = await self._get_strategy_signals_from_engine(db, user_id, strategy_id)
        symbols = list(target_weights.keys())

        # 3. 从 stream 服务获取真实快照数据（含当前价和昨收价）
        market_data = await self._get_real_data_from_stream(symbols)

        # 4. 执行虚拟成交
        executed_trades = []
        total_asset = float(
            account_data.get("total_asset") or (account_data.get("cash", 0) + account_data.get("market_value", 0)) or 0
        )
        positions = account_data.get("positions", {}) or {}
        for symbol, weight in target_weights.items():
            data = market_data.get(symbol)
            if not data or not data.get("current_price"):
                logger.warning(f"[SimSettle] 无法获取标的 {symbol} 的实时行情，跳过调仓")
                continue

            real_price = data["current_price"]
            last_close = data.get("last_close")
            current_position = positions.get(symbol, {}) if isinstance(positions, dict) else {}
            current_volume = float(
                current_position.get("volume")
                or current_position.get("quantity")
                or current_position.get("shares")
                or 0
            )
            target_value = max(0.0, float(weight) * total_asset)
            target_volume = int(target_value / real_price) if real_price > 0 else 0
            delta_volume = target_volume - int(current_volume)

            # --- 核心风控逻辑：大跌拦截 (Falling Knife Protection) ---
            # 备注：此处 last_close 优先使用复权后的 PreClose
            # 这能避免在除权除息日因价格跳空误触拦截
            if delta_volume > 0 and last_close:
                change_percent = (real_price - last_close) / last_close
                if change_percent < -0.03:
                    logger.warning(f"[SimSettle] 风险拦截: {symbol} 今日跌幅 {change_percent:.2%}, 触发大跌不买入规则")
                    continue
            # -------------------------------------------------------

            if delta_volume == 0:
                continue

            try:
                delta_cash = -delta_volume * real_price
                order_result = await sim_manager.update_balance(
                    user_id=user_id, symbol=symbol, delta_cash=delta_cash, delta_volume=delta_volume, price=real_price
                )
                if order_result.get("success"):
                    executed_trades.append(order_result)
            except Exception as e:
                logger.warning(f"标的 {symbol} 模拟成交失败: {e}")

        logger.info("[SimSettle] 结算完毕，已应用 3% 跌幅拦截保护")
        return {"user_id": user_id, "trades_count": len(executed_trades), "timestamp": datetime.now().isoformat()}

    async def _get_real_data_from_stream(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        """
        调用 quantmind-stream 获取 30s Redis 快照价格及昨收价
        """
        import httpx

        stream_url = os.getenv("STREAM_SERVICE_URL", "http://127.0.0.1:8003/api/v1/quotes")
        results = {}

        async with httpx.AsyncClient() as client:
            for symbol in symbols:
                try:
                    resp = await client.get(f"{stream_url}/{symbol}", params={"source": "remote_redis"}, timeout=2.0)
                    if resp.status_code == 200:
                        d = resp.json()
                        results[symbol] = {
                            "current_price": d.get("current_price"),
                            "last_close": d.get("close_price"),  # remote_redis 驱动已对齐此字段
                        }
                except Exception as e:
                    logger.error(f"获取行情失败 {symbol}: {e}")
        return results

    async def _get_strategy_signals_from_engine(self, db: Session, user_id: int, strategy_id: str) -> dict[str, float]:
        """
        从 Engine 的 engine_signal_scores 表获取最新 Alpha 信号，
        将 top-N 高分标的等权分配为目标仓位。
        """
        try:
            sql = text("""
                SELECT symbol, fusion_score
                FROM engine_signal_scores
                WHERE trade_date = (
                    SELECT MAX(trade_date) FROM engine_signal_scores
                )
                ORDER BY fusion_score DESC
                LIMIT 20
            """)
            rows = db.execute(sql).fetchall()
            if not rows:
                logger.warning(f"[SimSettle] engine_signal_scores 无可用信号，用户 {user_id} 跳过结算")
                return {}

            # 取 fusion_score > 0.5 的标的；若全低于阈值则取前 5
            top = [(r.symbol, r.fusion_score) for r in rows if r.fusion_score > 0.5]
            if not top:
                top = [(r.symbol, r.fusion_score) for r in rows[:5]]

            weight = round(1.0 / len(top), 4)
            logger.info(f"[SimSettle] 从 Engine 获取信号: {len(top)} 个标的，等权 {weight}")
            return {sym: weight for sym, _ in top}

        except Exception as e:
            logger.warning(f"[SimSettle] 读取 engine_signal_scores 失败: {e}，跳过本次结算")
            return {}

    async def _mock_get_strategy_signals(self, user_id: int, strategy_id: str) -> dict[str, float]:
        """已废弃：仅供本地调试使用，生产环境请勿调用"""
        await asyncio.sleep(0.5)
        return {
            "SH600519": 0.2,
            "SZ000001": 0.1,
        }


settler = SimulationSettler()
