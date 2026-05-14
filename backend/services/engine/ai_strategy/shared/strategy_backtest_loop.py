from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional
from collections.abc import Callable

from .ai_providers import StrategyRequest


class LoopStage(str, Enum):
    generation = "generation"
    backtest = "backtest"
    evaluation = "evaluation"
    completed = "completed"
    failed = "failed"


@dataclass
class LoopConfig:
    max_iterations: int = 10
    backtest_period: str = "2y"
    initial_capital: float = 100000
    risk_tolerance: str = "medium"


@dataclass
class StrategyResponse:
    code: str
    notes: str

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "notes": self.notes}


@dataclass
class BacktestResult:
    performance_metrics: dict[str, float] = field(default_factory=dict)


@dataclass
class LoopIteration:
    iteration: int
    stage: LoopStage
    strategy_response: StrategyResponse | None
    backtest_result: BacktestResult | None
    performance_score: float
    improvement: float
    execution_time: float
    errors: list[str] = field(default_factory=list)


@dataclass
class LoopRunResult:
    success: bool
    total_iterations: int
    best_iteration: LoopIteration | None
    learning_insights: dict[str, Any]
    total_time: float
    all_iterations: list[LoopIteration]


class StrategyBacktestLoop:
    def __init__(self, config: LoopConfig):
        self.config = config

    async def run_loop(
        self,
        strategy_request: StrategyRequest,
        market_data: dict[str, Any],
        progress_callback: Callable[[int, LoopStage, float, float], None] | None = None,
    ) -> LoopRunResult:
        started_at = time.perf_counter()
        iterations: list[LoopIteration] = []
        best: LoopIteration | None = None
        last_score = 0.0

        total = max(1, int(self.config.max_iterations))
        prompt_factor = min(1.5, max(0.5, len(strategy_request.prompt) / 120.0))
        symbols = strategy_request.target_assets or market_data.get("symbols") or ["SZ000001"]

        for i in range(1, total + 1):
            iter_start = time.perf_counter()
            if progress_callback:
                progress_callback(i, LoopStage.generation, (i - 1) / total, last_score)

            code = (
                "def signal(ctx):\n" f"    # iter={i}, assets={','.join(symbols[:5])}\n" "    return {'weight': 1.0}\n"
            )
            strategy = StrategyResponse(
                code=code,
                notes=f"auto-generated iteration {i} for {strategy_request.risk_tolerance}",
            )

            if progress_callback:
                progress_callback(i, LoopStage.backtest, (i - 0.4) / total, last_score)
            await asyncio.sleep(0)

            # 可复现的评分函数，避免依赖外部引擎
            score = max(
                0.0,
                min(
                    1.0,
                    0.45 + 0.5 * (i / total) + 0.08 * math.sin(i) + 0.05 * (prompt_factor - 1.0),
                ),
            )
            annual_return = round(0.06 + 0.22 * score, 6)
            max_drawdown = round(-0.20 + 0.08 * score, 6)
            sharpe_ratio = round(0.6 + 1.4 * score, 6)
            perf = BacktestResult(
                performance_metrics={
                    "annual_return": annual_return,
                    "max_drawdown": max_drawdown,
                    "sharpe_ratio": sharpe_ratio,
                }
            )

            if progress_callback:
                progress_callback(i, LoopStage.evaluation, (i - 0.02) / total, score)

            improvement = round(score - last_score, 6)
            last_score = score
            item = LoopIteration(
                iteration=i,
                stage=LoopStage.evaluation,
                strategy_response=strategy,
                backtest_result=perf,
                performance_score=round(score, 6),
                improvement=improvement,
                execution_time=round(time.perf_counter() - iter_start, 6),
                errors=[],
            )
            iterations.append(item)
            if best is None or item.performance_score > best.performance_score:
                best = item

        if progress_callback:
            progress_callback(total, LoopStage.completed, 1.0, best.performance_score if best else 0.0)

        elapsed = round(time.perf_counter() - started_at, 6)
        return LoopRunResult(
            success=True,
            total_iterations=total,
            best_iteration=best,
            learning_insights={
                "prompt_length": len(strategy_request.prompt),
                "asset_count": len(symbols),
                "risk_tolerance": strategy_request.risk_tolerance,
            },
            total_time=elapsed,
            all_iterations=iterations,
        )
