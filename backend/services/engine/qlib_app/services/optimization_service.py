"""
Qlib 参数优化服务
实现网格搜索与并行回测逻辑
"""

import asyncio
import itertools
import logging
import time
from typing import Any, Dict, List, Optional
from collections.abc import Callable
from uuid import uuid4

from backend.services.engine.qlib_app.schemas.backtest import (
    OptimizationTaskResult,
    QlibOptimizationRequest,
    QlibOptimizationResult,
    count_param_values,
)
from backend.services.engine.qlib_app.services.backtest_service import (
    QlibBacktestService,
)
from backend.services.engine.qlib_app.utils.structured_logger import (
    StructuredTaskLogger,
)

logger = logging.getLogger(__name__)


class OptimizationCancelledError(Exception):
    """优化任务被用户取消"""


class OptimizationService:
    """参数优化服务"""

    # 定义指标优化方向
    # 注意：max_drawdown 是负值，-0.10 (10%回撤) 好于 -0.15 (15%回撤)
    #      对于负值指标，数值越大越好，所以标记为 "maximize"
    METRIC_DIRECTIONS = {
        "sharpe_ratio": "maximize",
        "annual_return": "maximize",
        "alpha": "maximize",
        "information_ratio": "maximize",
        "total_return": "maximize",
        "max_drawdown": "maximize",  # 负值，-0.10 > -0.15，越大越好
        "volatility": "minimize",  # 正值，越小越好
    }

    def __init__(self, backtest_service: QlibBacktestService):
        self.backtest_service = backtest_service

    async def run_optimization(
        self,
        request: QlibOptimizationRequest,
        progress_callback: Callable[[dict[str, Any]], Any] | None = None,
        optimization_id: str | None = None,
        cancellation_checker: Callable[[], Any] | None = None,
    ) -> QlibOptimizationResult:
        """运行参数优化（网格搜索）"""
        start_time = time.time()
        optimization_id = optimization_id or uuid4().hex

        # 1. 生成所有参数组合
        param_names = [p.name for p in request.param_ranges]
        param_values = []
        for p in request.param_ranges:
            count = count_param_values(p.min, p.max, p.step)
            if count <= 0:
                raise ValueError(f"参数 {p.name} 未生成有效取值")
            # 使用索引计算值，避免浮点数累加误差
            values = [round(p.min + i * p.step, 10) for i in range(count)]
            param_values.append(values)

        combinations = list(itertools.product(*param_values))
        total_combinations = len(combinations)
        task_log = StructuredTaskLogger(
            logger,
            "qlib-optimization-service",
            {
                "optimization_id": optimization_id,
                "target": request.optimization_target,
                "max_parallel": request.max_parallel,
            },
        )
        task_log.info(
            "start", "开始参数优化任务", total_combinations=total_combinations
        )

        # 2. 并行执行任务（受限于 max_parallel）
        semaphore = asyncio.Semaphore(request.max_parallel)
        all_results: list[OptimizationTaskResult] = []
        cancellation_signaled = False

        async def is_cancelled() -> bool:
            if cancellation_checker is None:
                return False
            maybe = cancellation_checker()
            if asyncio.iscoroutine(maybe):
                maybe = await maybe
            return bool(maybe)

        async def run_single_task(combo):
            async with semaphore:
                if await is_cancelled():
                    return None, {
                        "params": dict(zip(param_names, combo)),
                        "cancelled": True,
                    }
                params = dict(zip(param_names, combo))

                # 构造回测请求
                task_req = request.base_request.copy(deep=True)
                # 标记为优化子任务，避免污染普通回测历史
                if hasattr(task_req, "history_source"):
                    task_req.history_source = "optimization"
                # 更新策略参数
                for k, v in params.items():
                    if hasattr(task_req.strategy_params, k):
                        setattr(
                            task_req.strategy_params,
                            k,
                            int(v) if v.is_integer() else v,
                        )

                try:
                    # 调用现有的回测服务
                    backtest_res = await self.backtest_service.run_backtest(task_req)
                    return OptimizationTaskResult(
                        params=params, metrics=backtest_res
                    ), None
                except Exception as e:
                    task_log.error(
                        "task_failed",
                        "Optimization task failed",
                        params=params,
                        error=e,
                    )
                    return None, {"params": params, "error": str(e)}

        tasks = [asyncio.create_task(run_single_task(combo)) for combo in combinations]
        completed_count = 0
        failed_count = 0
        failed_tasks: list[dict[str, Any]] = []
        best_result: OptimizationTaskResult | None = None
        direction = self.METRIC_DIRECTIONS.get(request.optimization_target, "maximize")

        for future in asyncio.as_completed(tasks):
            if await is_cancelled():
                cancellation_signaled = True
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                break

            result, error_info = await future
            completed_count += 1
            current_params = (
                result.params
                if result is not None
                else (error_info.get("params") if error_info else None)
            )
            if result is not None:
                all_results.append(result)
                if best_result is None:
                    best_result = result
                else:
                    current_value = getattr(
                        result.metrics, request.optimization_target, None
                    )
                    best_value = getattr(
                        best_result.metrics, request.optimization_target, None
                    )
                    current_value = (
                        current_value
                        if current_value is not None
                        else (-1e9 if direction == "maximize" else 1e9)
                    )
                    best_value = (
                        best_value
                        if best_value is not None
                        else (-1e9 if direction == "maximize" else 1e9)
                    )
                    is_better = (
                        current_value > best_value
                        if direction == "maximize"
                        else current_value < best_value
                    )
                    if is_better:
                        best_result = result
            else:
                failed_count += 1
                if error_info:
                    if error_info.get("cancelled"):
                        cancellation_signaled = True
                        for task in tasks:
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                        break
                    failed_tasks.append(error_info)

            if progress_callback:
                progress_payload = {
                    "optimization_id": optimization_id,
                    "progress": completed_count / total_combinations
                    if total_combinations
                    else 1.0,
                    "status": "running",
                    "message": f"已完成 {completed_count}/{total_combinations} 组参数",
                    "total_tasks": total_combinations,
                    "completed_count": completed_count,
                    "failed_count": failed_count,
                    "current_params": current_params,
                    "best_params": best_result.params if best_result else {},
                    "best_metric_value": (
                        getattr(best_result.metrics, request.optimization_target, None)
                        if best_result
                        else None
                    ),
                    "result_summary": {
                        "successful_count": len(all_results),
                        "failed_tasks": failed_tasks,
                    },
                    "all_results": [
                        item.model_dump(mode="json")
                        if hasattr(item, "model_dump")
                        else item.dict()
                        for item in all_results
                    ],
                }
                maybe_awaitable = progress_callback(progress_payload)
                if asyncio.iscoroutine(maybe_awaitable):
                    await maybe_awaitable

        if cancellation_signaled:
            if progress_callback:
                cancel_payload = {
                    "optimization_id": optimization_id,
                    "progress": completed_count / total_combinations
                    if total_combinations
                    else 1.0,
                    "status": "cancelled",
                    "message": "优化任务已取消",
                    "total_tasks": total_combinations,
                    "completed_count": completed_count,
                    "failed_count": failed_count,
                    "current_params": {},
                    "best_params": best_result.params if best_result else {},
                    "best_metric_value": (
                        getattr(best_result.metrics, request.optimization_target, None)
                        if best_result
                        else None
                    ),
                    "result_summary": {
                        "successful_count": len(all_results),
                        "failed_tasks": failed_tasks,
                    },
                    "all_results": [
                        item.model_dump(mode="json")
                        if hasattr(item, "model_dump")
                        else item.dict()
                        for item in all_results
                    ],
                }
                maybe_awaitable = progress_callback(cancel_payload)
                if asyncio.iscoroutine(maybe_awaitable):
                    await maybe_awaitable
            raise OptimizationCancelledError("用户手动停止优化任务")

        # 3. 寻找最优参数
        best_params = {}
        if all_results:
            target = request.optimization_target
            # 根据指标方向排序
            reverse = direction == "maximize"

            sorted_results = sorted(
                all_results,
                key=lambda x: (
                    getattr(x.metrics, target)
                    if getattr(x.metrics, target) is not None
                    else -1e9
                ),
                reverse=reverse,
            )
            best_params = sorted_results[0].params

        execution_time = time.time() - start_time

        return QlibOptimizationResult(
            optimization_id=optimization_id,
            best_params=best_params,
            all_results=all_results,
            target_metric=request.optimization_target,
            execution_time=execution_time,
        )
