#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跑通 A 股策略模板的真实回测（在 quantmind 容器内运行）。

用途：验证 strategy_templates/as*.py|.json 能被回测链路端到端加载——
      template.code → CustomStrategyBuilder → STRATEGY_CONFIG → 真实信号回测。

用法（容器内）：
    docker exec quantmind python /app/scripts/verify_ashare_backtest.py as01_core_multifactor
    docker exec quantmind python /app/scripts/verify_ashare_backtest.py as01_core_multifactor --start 2024-07-01 --end 2026-06-30

说明：
- signal 占位符 <PRED> 由 Runtime 用 model_id 对应模型的 pred.parquet/pred.pkl 替换；
- strategy_params 从模板 JSON 的 params 默认值回填（模拟 AI-IDE 前端行为），
  再合并到 kwargs（strategy_builder 只覆盖 kwargs 中已存在的 ui_params）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

TEMPLATE_DIR = Path("/app/strategy_templates")
if not TEMPLATE_DIR.exists():
    TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "strategy_templates"

# QlibStrategyParams 中存在的字段（用于把模板 JSON 参数回填进请求）
_REQUEST_PARAM_FIELDS = {
    "topk", "short_topk", "n_drop", "min_score", "max_weight", "long_exposure",
    "short_exposure", "momentum_period", "topk_sectors", "lookback_days",
    "vol_lookback", "stop_loss", "take_profit", "rebalance_days",
    "enable_short_selling", "max_leverage", "account_stop_loss",
}


def _load_template_params(template_id: str) -> dict:
    meta = json.loads((TEMPLATE_DIR / f"{template_id}.json").read_text(encoding="utf-8"))
    return {
        p["name"]: p["default"]
        for p in meta.get("params", [])
        if p["name"] in _REQUEST_PARAM_FIELDS
    }


async def _run(
    template_id: str, start: str, end: str, user_id: str, tenant_id: str, model_id: str | None
) -> int:
    from backend.services.engine.qlib_app.schemas.backtest import (
        QlibBacktestRequest,
        QlibStrategyParams,
    )
    from backend.services.engine.qlib_app.services.backtest_service import QlibBacktestService

    params = _load_template_params(template_id)
    request = QlibBacktestRequest(
        strategy_type=template_id,
        strategy_params=QlibStrategyParams(**params),
        start_date=start,
        end_date=end,
        user_id=user_id,
        tenant_id=tenant_id,
        model_id=model_id,
    )
    print(f"回测 {template_id} | {start} → {end} | model={model_id or '默认'} | strategy_params={params}")

    result = await QlibBacktestService().run_backtest(request)
    if result.status != "completed":
        print(f"状态: {result.status}")
        return 1

    print(f"  status          : {result.status}")
    print(f"  年化收益        : {result.annual_return:.2%}" if result.annual_return is not None else "  年化收益: -")
    print(f"  夏普            : {result.sharpe_ratio:.3f}" if result.sharpe_ratio is not None else "  夏普: -")
    print(f"  最大回撤        : {result.max_drawdown:.2%}" if result.max_drawdown is not None else "  最大回撤: -")
    if result.benchmark_return is not None:
        print(f"  基准收益        : {result.benchmark_return:.2%}")
    if result.total_trades is not None:
        print(f"  交易笔数        : {result.total_trades}")
    if result.win_rate is not None:
        print(f"  胜率            : {result.win_rate:.2%}")
    cfg = (result.config or {}).get("strategy") or {}
    print(f"  策略类          : {cfg.get('class')}")
    print(f"  实际 kwargs     : {cfg.get('kwargs')}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="A 股策略模板真实回测验证")
    parser.add_argument("template_id", help="模板 ID，如 as01_core_multifactor")
    parser.add_argument("--start", default="2024-07-01")
    parser.add_argument("--end", default="2026-06-30")
    parser.add_argument("--user-id", default="00000001")
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument("--model-id", default=None, help="显式模型 ID；缺省用默认模型")
    args = parser.parse_args()
    return asyncio.run(
        _run(args.template_id, args.start, args.end, args.user_id, args.tenant_id, args.model_id)
    )


if __name__ == "__main__":
    sys.exit(main())
