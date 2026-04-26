import asyncio
import logging
import random
import statistics
import time
from typing import Any, Dict, List

from backend.services.engine.qlib_app.schemas.backtest import (
    GeneticHistoryRecord,
    OptimizationParamRange,
    QlibGeneticOptimizationRequest,
    QlibGeneticOptimizationResult,
)
from backend.services.engine.qlib_app.services.backtest_service import (
    QlibBacktestService,
)
from backend.services.engine.qlib_app.services.optimization_service import (
    OptimizationCancelledError,
)
from backend.services.engine.qlib_app.utils.structured_logger import (
    StructuredTaskLogger,
)

logger = logging.getLogger(__name__)
task_logger = StructuredTaskLogger(logger, "GeneticOptimizationService")


class GeneticOptimizationService:
    """遗传算法参数优化服务"""

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
        request: QlibGeneticOptimizationRequest,
        progress_callback=None,
        cancellation_checker=None,
    ) -> QlibGeneticOptimizationResult:
        """运行遗传算法优化"""
        start_time = time.time()
        # 使用前端传入的 optimization_id
        optimization_id = request.optimization_id

        task_log = StructuredTaskLogger(
            logger,
            "qlib-genetic-optimization-service",
            {
                "optimization_id": optimization_id,
                "target": request.optimization_target,
                "population_size": request.population_size,
                "generations": request.generations,
            },
        )

        def push_log(event: str, message: str, **fields: Any):
            """统一输出结构化日志，交由任务级 handler 写入 Redis。"""
            task_log.info(event, message, **fields)

        push_log("start", "开始遗传算法优化")
        push_log(
            "config",
            "优化配置",
            mutation_rate=request.mutation_rate,
        )

        # 1. 初始化种群
        population = self._init_population(
            request.population_size, request.param_ranges
        )
        push_log("population_init", "初始化种群完成", population_size=len(population))

        history: list[GeneticHistoryRecord] = []
        best_individual = None
        best_fitness = -float("inf")

        total_individuals = request.generations * request.population_size
        completed_count = 0

        # 定义内部回调，用于细粒度进度更新
        def on_individual_completed():
            nonlocal completed_count
            completed_count += 1
            if progress_callback:
                # 计算总体进度
                progress = completed_count / total_individuals
                # 估算当前代数
                current_gen = (completed_count - 1) // request.population_size + 1

                progress_callback(
                    {
                        "optimization_id": optimization_id,
                        "generation": current_gen,
                        "progress": progress,
                        "status": "running",
                        "message": f"正在评估第 {current_gen}/{request.generations} 代: {(completed_count - 1) % request.population_size + 1}/{request.population_size}",
                    }
                )

        async def check_is_cancelled():
            if cancellation_checker is None:
                return False
            maybe = cancellation_checker()
            if asyncio.iscoroutine(maybe):
                maybe = await maybe
            return bool(maybe)

        # 2. 迭代进化
        for gen in range(request.generations):
            if await check_is_cancelled():
                push_log(
                    "cancelled", "遗传算法优化在代际迭代前被取消", generation=gen + 1
                )
                raise OptimizationCancelledError("用户手动停止遗传优化任务")

            push_log(
                "generation_start",
                "开始评估代际",
                generation=gen + 1,
                total_generations=request.generations,
            )

            # 2.1 评估适应度 (传递细粒度回调)
            fitness_scores = await self._evaluate_population(
                population,
                request,
                optimization_id,
                on_individual_completed,
                check_is_cancelled,
            )

            push_log(
                "generation_eval_done",
                "完成适应度评估",
                generation=gen + 1,
                evaluated=len(population),
            )

            # 2.2 记录统计信息
            valid_scores = [s for s in fitness_scores if s is not None]
            if not valid_scores:
                push_log(
                    "generation_empty",
                    f"第 {gen + 1} 代无有效结果，跳过",
                    generation=gen + 1,
                )
                task_log.warning(
                    "generation_empty",
                    "Generation has no valid results",
                    generation=gen + 1,
                )
                continue

            max_fit = max(valid_scores)
            avg_fit = statistics.mean(valid_scores)
            std_fit = statistics.stdev(valid_scores) if len(valid_scores) > 1 else 0.0

            history.append(
                GeneticHistoryRecord(
                    generation=gen + 1,
                    max_fitness=max_fit,
                    avg_fitness=avg_fit,
                    std_fitness=std_fit,
                )
            )

            # 更新全局最优
            for i, score in enumerate(fitness_scores):
                if score is not None and score > best_fitness:
                    best_fitness = score
                    best_individual = population[i]

            push_log(
                "generation_stats",
                "代际统计",
                generation=gen + 1,
                max_fitness=f"{max_fit:.4f}",
                avg_fitness=f"{avg_fit:.4f}",
                std_fitness=f"{std_fit:.4f}",
                best_fitness=f"{best_fitness:.4f}",
                best_individual=best_individual,
            )

            # 代际完成后的汇总回调
            if progress_callback:
                progress = (gen + 1) / request.generations
                progress_callback(
                    {
                        "optimization_id": optimization_id,
                        "generation": gen + 1,
                        "progress": progress,
                        "status": "running",
                        "max_fitness": max_fit,
                        "avg_fitness": avg_fit,
                        "std_fitness": std_fit,
                        "message": f"完成第 {gen + 1} 代评估",
                    }
                )

            # 检查收敛 - 如果种群已收敛，提前终止
            if self._check_convergence(history, window=3, threshold=0.01):
                push_log("converged", "种群已收敛，提前终止", generation=gen + 1)
                task_log.info("converged", "种群已收敛，提前终止", generation=gen + 1)
                if progress_callback:
                    progress_callback(
                        {
                            "optimization_id": optimization_id,
                            "status": "converged",
                            "message": f"种群已收敛，在第 {gen + 1} 代提前终止",
                        }
                    )
                break

            # 如果是最后一代，跳过繁衍
            if gen == request.generations - 1:
                break

            # 2.3 精英保留 + 选择 + 交叉 + 变异（生成下一代）
            push_log("next_generation", "开始生成下一代", elite_count=2)
            population = self._generate_next_generation(
                population, fitness_scores, request, elite_count=2
            )
            push_log("next_generation_done", "下一代种群已生成", elite_count=2)

        execution_time = time.time() - start_time

        # 3. 获取最优参数
        best_params = best_individual if best_individual else {}

        push_log(
            "complete",
            "遗传算法优化完成",
            best_params=best_params,
            best_fitness=f"{best_fitness:.4f}",
            generations=len(history),
            execution_time=f"{execution_time:.2f}",
        )

        return QlibGeneticOptimizationResult(
            optimization_id=optimization_id,
            best_params=best_params,
            best_fitness=best_fitness,
            history=history,
            execution_time=execution_time,
        )

    def _init_population(
        self, size: int, ranges: list[OptimizationParamRange]
    ) -> list[dict[str, Any]]:
        """初始化随机种群"""
        population = []
        for _ in range(size):
            individual = {}
            for param in ranges:
                # 根据步长随机生成
                steps = int((param.max - param.min) / param.step)
                random_step = random.randint(0, steps)
                value = param.min + random_step * param.step

                # 如果步长是整数，结果取整
                if param.step.is_integer() and param.min.is_integer():
                    individual[param.name] = int(value)
                else:
                    individual[param.name] = float(value)
            population.append(individual)
        return population

    async def _evaluate_population(
        self,
        population: list[dict[str, Any]],
        request: QlibGeneticOptimizationRequest,
        opt_id: str,
        on_complete=None,
        check_is_cancelled=None,
    ) -> list[float]:
        """并行评估种群适应度"""

        semaphore = asyncio.Semaphore(request.max_parallel)

        async def eval_one(params: dict[str, Any]):
            async with semaphore:
                if check_is_cancelled and await check_is_cancelled():
                    # 这里无法简单的 raise，因为 gather 会等待所有。
                    # 但可以返回 None 或抛出异常让 gather 终止。
                    raise OptimizationCancelledError("用户手动停止遗传优化任务")

                try:
                    # 构造回测请求
                    task_req = request.base_request.copy(deep=True)
                    # 覆盖策略参数
                    for k, v in params.items():
                        if hasattr(task_req.strategy_params, k):
                            # 类型转换
                            val = (
                                int(v) if isinstance(v, float) and v.is_integer() else v
                            )
                            setattr(task_req.strategy_params, k, val)

                    # 运行回测
                    res = await self.backtest_service.run_backtest(task_req)

                    # 获取目标指标并转换为适应度值
                    fitness = self._get_fitness_value(res, request.optimization_target)

                    if on_complete:
                        on_complete()

                    return fitness
                except Exception as e:
                    task_logger.error(
                        "ga_eval_failed", "GA Eval failed", params=params, error=str(e)
                    )
                    if on_complete:
                        on_complete()
                    return None

        tasks = [eval_one(ind) for ind in population]
        return await asyncio.gather(*tasks)

    def _get_fitness_value(self, result, target: str) -> float:
        """根据指标方向转换适应度值

        注意：max_drawdown 是负值（-0.10, -0.15等），
        数值越大越好（-0.10 > -0.15），所以标记为 maximize
        """
        value = getattr(result, target, None)
        if value is None:
            return -1e9

        direction = self.METRIC_DIRECTIONS.get(target, "maximize")
        if direction == "minimize":
            # 对于真正的 minimize 指标 (如 volatility)
            # 转换为最大化问题：适应度 = -value
            return -value
        return value

    def _generate_next_generation(
        self, population: list[dict], scores: list[float], request, elite_count: int = 2
    ) -> list[dict]:
        """生成下一代（含精英保留）

        Args:
            population: 当前种群
            scores: 适应度分数
            request: 优化请求（包含参数范围、变异率等）
            elite_count: 精英个体数量（默认2个）

        Returns:
            下一代种群
        """
        # 1. 精英保留 - 保留最优的 elite_count 个个体
        valid_pop = [(p, s) for p, s in zip(population, scores) if s is not None]
        if not valid_pop:
            # 如果没有有效个体，返回原种群
            task_logger.warning(
                "no_valid_individuals", "No valid individuals for next generation"
            )
            return population

        # 按适应度降序排序
        valid_pop.sort(key=lambda x: x[1], reverse=True)
        elites = [p for p, _ in valid_pop[:elite_count]]

        task_logger.debug(
            "elite_retained",
            "精英保留",
            elite_count=len(elites),
            best_fitness=f"{valid_pop[0][1]:.4f}",
        )

        # 2. 选择父代
        selected = self._selection(population, scores)

        # 3. 交叉 + 变异（生成剩余个体）
        next_gen = elites.copy()
        while len(next_gen) < request.population_size:
            parent1 = random.choice(selected)
            parent2 = random.choice(selected)
            child = self._crossover(parent1, parent2)
            child = self._mutate(child, request.param_ranges, request.mutation_rate)
            next_gen.append(child)

        return next_gen

    def _check_convergence(
        self,
        history: list[GeneticHistoryRecord],
        window: int = 3,
        threshold: float = 0.01,
    ) -> bool:
        """检查种群是否收敛

        如果连续 window 代的适应度标准差都小于 threshold，则认为已收敛

        Args:
            history: 代际历史记录
            window: 检查窗口大小（连续多少代）
            threshold: 标准差阈值

        Returns:
            是否已收敛
        """
        if len(history) < window:
            return False

        # 检查最近 window 代的标准差是否都小于阈值
        recent_std = [h.std_fitness for h in history[-window:]]
        converged = all(s < threshold for s in recent_std)

        if converged:
            task_logger.info(
                "convergence_detected",
                "收敛检测",
                window=window,
                threshold=threshold,
                recent_std=recent_std,
            )

        return converged

    def _selection(
        self, population: list[dict], scores: list[float], k=3
    ) -> list[dict]:
        """锦标赛选择"""
        # 过滤无效个体
        valid_pop = []
        for i, p in enumerate(population):
            if scores[i] is not None:
                valid_pop.append((p, scores[i]))

        if not valid_pop:
            return population  # Fallback

        selected = []
        for _ in range(len(population)):
            # 随机选k个打比赛
            contestants = random.sample(valid_pop, min(k, len(valid_pop)))
            # 选分数最高的
            winner = max(contestants, key=lambda x: x[1])[0]
            selected.append(winner)
        return selected

    def _crossover(self, p1: dict, p2: dict) -> dict:
        """单点交叉"""
        keys = list(p1.keys())
        if len(keys) < 2:
            return p1.copy()

        point = random.randint(1, len(keys) - 1)
        child = {}
        # 前半部分来自p1
        for k in keys[:point]:
            child[k] = p1[k]
        # 后半部分来自p2
        for k in keys[point:]:
            child[k] = p2[k]
        return child

    def _mutate(
        self,
        individual: dict,
        ranges: list[OptimizationParamRange],
        rate: float,
    ) -> dict:
        """随机变异"""
        mutated = individual.copy()
        for param in ranges:
            if random.random() < rate:
                # 重新随机生成该参数
                steps = int((param.max - param.min) / param.step)
                random_step = random.randint(0, steps)
                value = param.min + random_step * param.step

                if param.step.is_integer() and param.min.is_integer():
                    mutated[param.name] = int(value)
                else:
                    mutated[param.name] = float(value)
        return mutated
