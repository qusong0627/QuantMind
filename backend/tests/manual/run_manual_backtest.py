import pandas as pd
import qlib
from qlib.backtest import backtest
from qlib.backtest.executor import SimulatorExecutor
from qlib.backtest.signal import SignalWCache
from qlib.contrib.strategy.signal_strategy import TopkDropoutStrategy

qlib.init(provider_uri="db/qlib_data")
pred = pd.read_pickle("research/data_adapter/qlib_data/predictions/pred.pkl")

strategy = TopkDropoutStrategy(signal=SignalWCache(pred), topk=50, n_drop=5)

executor = SimulatorExecutor(time_per_step="day", generate_portfolio_metrics=True)

backtest_config = {
    "start_time": "2025-01-01",
    "end_time": "2025-12-31",
    "account": 100000000,
    "benchmark": "SH000300",
    "exchange_kwargs": {
        "freq": "day",
        "deal_price": "close",
        "open_cost": 0.0005,
        "close_cost": 0.0015,
        "min_cost": 5,
    },
}

portfolio_dict, indicator_dict = backtest(strategy=strategy, executor=executor, **backtest_config)

report = portfolio_dict.get("1day")[0]
print(f"Report head:\n{report.head()}")
print(f"Report tail:\n{report.tail()}")
print(f"Total return: {report['return'].sum()}")
print(f"Trades count: {len(portfolio_dict.get('1day')[1])}")
