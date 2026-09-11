#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量跑 A 股策略模板回测，输出对比表（在 quantmind 容器内运行）。

用法（容器内）：
    docker exec quantmind python /app/scripts/verify_ashare_batch.py \
        --model-id mdl_cn_train_20260906064130_43e64ff0_b4cf437f \
        --start 2024-07-01 --end 2026-06-30 \
        --ids standard_topk as01_core_multifactor as16_momentum_20 ...

每个模板独立回测；任何失败只记录不中断。输出 TSV 便于排序比较。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from verify_ashare_backtest import _load_template_params  # noqa: E402


async def _run_one(template_id: str, start: str, end: str, model_id: str | None) -> dict:
    from backend.services.engine.qlib_app.schemas.backtest import (
        QlibBacktestRequest,
        QlibStrategyParams,
    )
    from backend.services.engine.qlib_app.services.backtest_service import QlibBacktestService

    try:
        params = _load_template_params(template_id)
    except FileNotFoundError:
        return {"id": template_id, "status": "no_template"}
    request = QlibBacktestRequest(
        strategy_type=template_id,
        strategy_params=QlibStrategyParams(**params),
        start_date=start,
        end_date=end,
        user_id="00000001",
        tenant_id="default",
        model_id=model_id,
    )
    try:
        result = await QlibBacktestService().run_backtest(request)
    except Exception as exc:  # noqa: BLE001
        return {"id": template_id, "status": f"error:{type(exc).__name__}", "detail": str(exc)[:120]}
    return {
        "id": template_id,
        "status": result.status,
        "annual": result.annual_return,
        "sharpe": result.sharpe_ratio,
        "mdd": result.max_drawdown,
        "bench": result.benchmark_return,
        "trades": result.total_trades,
        "win": result.win_rate,
    }


def _fmt(row: dict) -> str:
    if row["status"] != "completed":
        return f"{row['id']}\t{row['status']}\t{row.get('detail', '')}"
    pct = lambda v: f"{v:.2%}" if isinstance(v, (int, float)) else "-"  # noqa: E731
    num = lambda v: f"{v:.3f}" if isinstance(v, (int, float)) else "-"  # noqa: E731
    return "\t".join(
        [
            row["id"], row["status"], pct(row["annual"]), num(row["sharpe"]),
            pct(row["mdd"]), pct(row["bench"]), str(row["trades"]), pct(row["win"]),
        ]
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description="批量 A 股模板回测")
    parser.add_argument("--ids", nargs="+", required=True)
    parser.add_argument("--start", default="2024-07-01")
    parser.add_argument("--end", default="2026-06-30")
    parser.add_argument("--model-id", default=None)
    args = parser.parse_args()

    print("id\tstatus\t年化\t夏普\t最大回撤\t基准\t交易笔数\t胜率")
    for template_id in args.ids:
        row = await _run_one(template_id, args.start, args.end, args.model_id)
        print(_fmt(row), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
