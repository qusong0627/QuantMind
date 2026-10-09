"""模拟盘 Broker：复用 tool_trade.py 的本地 position.jsonl 交易逻辑。

买入/卖出/持仓全部落盘到 data/agent_data/{signature}/position/position.jsonl，
价格从 merged.jsonl 读取——与现有 MCP trade 工具完全一致，保证行为不漂移。
"""

from typing import Any, Dict, List, Optional

from tools.price_tools import get_open_prices, get_yesterday_open_and_close_price

from agent_tools.brokers.base import Broker, BrokerError


class SandboxBroker(Broker):
    """本地模拟盘。"""

    name = "sandbox"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        # 延迟导入，避免 MCP 工具加载顺序问题
        self._trade_module = None

    def _trade(self):
        if self._trade_module is None:
            import agent_tools.tool_trade as trade

            self._trade_module = trade
        return self._trade_module

    @staticmethod
    def _unwrap(tool):
        """fastmcp 装饰后的工具是 FunctionTool，取其原始函数。"""
        return getattr(tool, "fn", tool)

    def _ensure_initial_position(self, signature: str, today_date: str) -> None:
        """position.jsonl 不存在时写入初始持仓（全 0 + CASH）。

        与 main.py 中 register_agent 的初始结构一致，保证后续
        tool_trade 的 new_position[symbol] += amount 不越界。
        """
        import json
        from pathlib import Path

        from tools.general_tools import get_config_value

        from prompts.agent_prompt import all_nasdaq_100_symbols, all_sse_50_symbols

        log_path = get_config_value("LOG_PATH", "./data/agent_data")
        if log_path.startswith("./data/"):
            log_path = log_path[7:]
        position_file = (
            Path(__file__).resolve().parents[2]
            / "data" / log_path / signature / "position" / "position.jsonl"
        )
        if position_file.exists():
            return
        market = get_config_value("MARKET", "us")
        symbols = all_sse_50_symbols if market == "cn" else all_nasdaq_100_symbols
        position_file.parent.mkdir(parents=True, exist_ok=True)
        initial_cash = float(self.config.get("initial_cash", 10000.0))
        record = {
            "date": today_date,
            "id": 0,
            "positions": {**{s: 0 for s in symbols}, "CASH": initial_cash},
        }
        with position_file.open("w", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _today_date(self, today_date: Optional[str]) -> str:
        from tools.general_tools import get_config_value

        return today_date or get_config_value("TODAY_DATE")

    def get_positions(self, signature: str, today_date: str) -> Dict[str, float]:
        """最新一条持仓（当前状态），而非昨日初始持仓。"""
        latest = self._latest_position_record(signature)
        if not latest:
            return {}
        return {k: float(v) for k, v in latest.get("positions", {}).items() if k != "CASH"}

    def get_cash(self, signature: str, today_date: str) -> float:
        latest = self._latest_position_record(signature)
        if not latest:
            return 0.0
        return float(latest.get("positions", {}).get("CASH", 0.0))

    @staticmethod
    def _latest_position_record(signature: str) -> Optional[Dict[str, Any]]:
        import json
        from pathlib import Path

        from tools.general_tools import get_config_value

        log_path = get_config_value("LOG_PATH", "./data/agent_data")
        if log_path.startswith("./data/"):
            log_path = log_path[7:]
        position_file = (
            Path(__file__).resolve().parents[2]
            / "data" / log_path / signature / "position" / "position.jsonl"
        )
        if not position_file.exists():
            return None
        last = None
        try:
            with position_file.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            last = json.loads(line)
                        except json.JSONDecodeError:
                            continue
        except OSError:
            return None
        return last

    def buy(self, signature: str, today_date: str, symbol: str, amount: int,
            price: Optional[float] = None) -> Dict[str, Any]:
        self._ensure_initial_position(signature, today_date)
        trade = self._trade()
        result = self._unwrap(trade.buy)(symbol, amount)
        if "error" in result:
            raise BrokerError(result["error"])
        return result

    def sell(self, signature: str, today_date: str, symbol: str, amount: int,
             price: Optional[float] = None) -> Dict[str, Any]:
        self._ensure_initial_position(signature, today_date)
        trade = self._trade()
        result = self._unwrap(trade.sell)(symbol, amount)
        if "error" in result:
            raise BrokerError(result["error"])
        return result

    def get_quote(self, symbol: str, date: str, market: str = "us") -> Optional[Dict[str, Any]]:
        from tools.price_tools import get_open_prices

        prices = get_open_prices(date, [symbol], market=market)
        # get_open_prices 的 key 形如 "AAPL_price"
        price = prices.get(f"{symbol}_price") or prices.get(symbol)
        if price is None:
            return None
        return {"symbol": symbol, "date": date, "buy price": price}

    def get_klines(self, symbol: str, start: str, end: str, interval: str = "daily",
                   market: str = "us") -> List[Dict[str, Any]]:
        from tools.price_tools import get_yesterday_open_and_close_price

        # 日级：起止日各取一次开/收；小时级数据直接返回当日全部 bar
        try:
            start_prices, end_prices = get_yesterday_open_and_close_price(
                end, [symbol], market=market
            )
        except Exception as exc:
            raise BrokerError(f"获取 K 线失败: {exc}") from exc
        bars = []
        if symbol in start_prices:
            bars.append({"date": start, "type": "open", **start_prices[symbol]})
        if symbol in end_prices:
            bars.append({"date": end, "type": "close", **end_prices[symbol]})
        return bars


def register() -> None:
    from agent_tools.brokers.base import registry

    registry.register(SandboxBroker)


register()
