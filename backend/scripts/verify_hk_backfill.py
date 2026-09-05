"""回填完成后自动验证 2026-02-27 起的港股回测（容器内执行，结果落 /tmp/hk_verify_result.txt）。"""
import duckdb

r = duckdb.connect().execute(
    "SELECT count(*), min(trade_date), max(trade_date) FROM read_parquet("
    "'/app/models/users/default/00000001/hk/mdl_hk_hk-mini-13-0902_lightgbm_2d361e3d/pred.parquet')"
).fetchone()
print("pred 最终覆盖:", r)

import asyncio

from backend.services.engine.qlib_app.schemas.backtest import QlibBacktestRequest
from backend.services.engine.qlib_app.services.backtest_service import QlibBacktestService

req = QlibBacktestRequest(
    strategy_type="TopkDropout",
    start_date="2026-02-27",
    end_date="2026-08-20",
    initial_capital=10_000_000,
    qlib_provider_uri="/data/quanthk/.qlib_cache/hk_data",
    qlib_region="cn",
    universe="hsgt",
    benchmark_symbol="HSI",
    model_id="mdl_hk_hk-mini-13-0902_lightgbm_2d361e3d",
    user_id="00000001",
    tenant_id="default",
    use_vectorized=True,
    strategy_params={"topk": 10, "n_drop": 2, "signal": "<PRED>"},
)
res = asyncio.run(QlibBacktestService().run_backtest(req))
print("用户区间(2026-02-27~08-20)回测 status:", getattr(res, "status", None))
print("annual:", round(float(getattr(res, "annual_return", 0) or 0), 4),
      "| sharpe:", round(float(getattr(res, "sharpe_ratio", 0) or 0), 4),
      "| mdd:", round(float(getattr(res, "max_drawdown", 0) or 0), 4))
print("trades:", getattr(res, "total_trades", None), "| win:", getattr(res, "win_rate", None))
print("error:", getattr(res, "error_message", None))
