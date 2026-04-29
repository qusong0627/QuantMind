"""Qlib 回测服务查询与管理逻辑"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional

from backend.services.engine.qlib_app.schemas.backtest import QlibBacktestResult
from backend.services.engine.qlib_app.services.risk_analyzer import RiskAnalyzer
from backend.shared.utils import normalize_user_id
from backend.services.engine.qlib_app.utils.structured_logger import StructuredTaskLogger

logger = logging.getLogger(__name__)
task_logger = StructuredTaskLogger(logger, "BacktestServiceQuery")


class QlibBacktestServiceQueryMixin:
    """Qlib 回测结果、历史、状态与管理逻辑 mixin"""

    async def get_result(
        self,
        backtest_id: str,
        tenant_id: str | None = None,
        user_id: str | None = None,
        exclude_trades: bool = False,
    ) -> QlibBacktestResult | None:
        """获取回测结果，优先从内存、缓存，最后从持久化层获取"""
        try:
            if not self._initialized:
                self.initialize()
        except Exception as exc:
            task_logger.warning("initialize_failed", "get_result 初始化 Qlib 失败，将继续返回原始结果", error=str(exc))

        normalized_user_id = normalize_user_id(user_id) if user_id else None
        cache_key = (
            f"{tenant_id}:{normalized_user_id}:{backtest_id}"
            if tenant_id and normalized_user_id
            else (f"{tenant_id}:{backtest_id}" if tenant_id else backtest_id)
        )
        if self._cache and not exclude_trades:
            try:
                cached_result = self._cache.get_backtest_result(cache_key)
                if cached_result:
                    task_logger.debug("cache_hit_result", "从缓存读取回测结果", backtest_id=backtest_id)
                    model = QlibBacktestResult(**cached_result)
                    return self._normalize_result_trades(model)
            except Exception as e:
                task_logger.warning("cache_read_result_failed", "从缓存读取失败", error=str(e))

        # 1. 尝试从内存获取
        run = self._runs.get(backtest_id)
        if (
            run
            and run.get("result")
            and (tenant_id is None or run.get("tenant_id") == tenant_id)
            and (normalized_user_id is None or run.get("user_id") == normalized_user_id)
        ):
            result = run.get("result")
            if exclude_trades:
                # 克隆一份结果并剔除大数据量字段，避免影响内存中的原始对象
                res_dict = result.dict()
                res_dict.pop("trades", None)
                res_dict.pop("positions", None)
                if hasattr(QlibBacktestResult, "model_validate"):
                    return QlibBacktestResult.model_validate(res_dict)
                return QlibBacktestResult.parse_obj(res_dict)
            return self._normalize_result_trades(result)

        # 2. 尝试从持久化层获取 (持久化层已优化：只读 JSON 分片并跳过 Pydantic 校验)
        exclude_fields = ["trades", "positions"] if exclude_trades else None
        result = await self._persistence.get_result(
            backtest_id,
            tenant_id=tenant_id,
            user_id=normalized_user_id,
            exclude_fields=exclude_fields,
        )

        if result:
            if not exclude_trades:
                result = self._normalize_result_trades(result)
                if self._cache:
                    try:
                        self._cache.set_backtest_result(cache_key, result.dict())
                    except Exception as e:
                        task_logger.warning("cache_write_result_failed", "写入缓存失败", error=str(e))

        return result

    def _normalize_result_trades(self, result: QlibBacktestResult | None) -> QlibBacktestResult | None:
        if result is None:
            return None
        try:
            trades = getattr(result, "trades", None)
            if isinstance(trades, list) and trades:
                result.trades = RiskAnalyzer.normalize_trades_for_display(trades)
        except Exception as exc:
            task_logger.warning("normalize_result_trades_failed", "Failed to normalize result trades for display", error=str(exc))
        return result

    async def get_status(
        self,
        backtest_id: str,
        tenant_id: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """获取回测状态（用于轮询）"""
        normalized_user_id = normalize_user_id(user_id) if user_id else None
        if backtest_id in self._runs:
            run = self._runs[backtest_id]
            if tenant_id is not None and run.get("tenant_id") != tenant_id:
                return {
                    "backtest_id": backtest_id,
                    "status": "not_found",
                    "progress": 0.0,
                    "message": "Backtest not found",
                }
            if normalized_user_id is not None and run.get("user_id") != normalized_user_id:
                return {
                    "backtest_id": backtest_id,
                    "status": "not_found",
                    "progress": 0.0,
                    "message": "Backtest not found",
                }
            progress = 0.5
            if run["status"] == "completed":
                progress = 1.0
            elif run["status"] == "failed":
                progress = 1.0
            elif run["status"] == "running":
                elapsed = (datetime.now() - run["created_at"]).total_seconds()
                progress = min(0.95, elapsed / 30.0)

            return {
                "backtest_id": backtest_id,
                "status": run["status"],
                "progress": progress,
                "message": run.get("error_message", "Processing..."),
                "error_message": run.get("error_message"),
                "full_error": run.get("full_error"),
            }

        persisted_status = await self._persistence.get_status(
            backtest_id,
            tenant_id=tenant_id,
            user_id=normalized_user_id,
        )
        if persisted_status:
            return persisted_status

        result = await self.get_result(
            backtest_id,
            tenant_id=tenant_id,
            user_id=normalized_user_id,
        )
        if result:
            return {
                "backtest_id": backtest_id,
                "status": result.status,
                "progress": 1.0,
                "message": ("Completed" if result.status == "completed" else result.error_message),
            }

        return {
            "backtest_id": backtest_id,
            "status": "not_found",
            "progress": 0.0,
            "message": "Backtest not found",
        }

    async def list_history(self, user_id: str, tenant_id: str, limit: int = 10) -> list[QlibBacktestResult]:
        """获取用户历史（优先从缓存读取）"""
        user_id = normalize_user_id(user_id)
        history_limit = max(1, int(limit))

        if self._cache:
            try:
                cached_history = self._cache.get_user_history(f"{tenant_id}:{user_id}")
                if cached_history:
                    task_logger.debug("cache_hit_history", "从缓存读取用户历史", user_id=user_id, tenant_id=tenant_id)
                    cached_items = [QlibBacktestResult(**item) for item in cached_history]
                    return cached_items[:history_limit]
            except Exception as e:
                task_logger.warning("cache_read_history_failed", "从缓存读取历史失败", error=str(e))

        results: list[QlibBacktestResult] = []
        for run in self._runs.values():
            if run.get("user_id") == user_id and run.get("tenant_id") == tenant_id and run.get("result"):
                results.append(run["result"])

        persisted = await self._persistence.list_history(user_id, tenant_id=tenant_id, limit=history_limit)
        for item in persisted:
            if all(existing.backtest_id != item.backtest_id for existing in results):
                results.append(item)

        results.sort(key=lambda r: r.created_at or datetime.min, reverse=True)
        results = results[:history_limit]

        if self._cache and results:
            try:
                self._cache.set_user_history(f"{tenant_id}:{user_id}", [r.dict() for r in results])
            except Exception as e:
                task_logger.warning("cache_write_history_failed", "写入历史缓存失败", error=str(e))

        return results

    async def _notify_progress(
        self,
        backtest_id: str,
        user_id: str,
        status: str,
        progress: float,
        error_message: str | None = None,
        full_error: str | None = None,
        strategy_name: str | None = None,
        benchmark_symbol: str | None = None,
        initial_capital: float | None = None,
        information_ratio: float | None = None,
        beta: float | None = None,
        benchmark_return: float | None = None,
        message: str | None = None,
    ) -> None:
        """发送回测进度通知 (支持 Redis Pub/Sub 和 HTTP)"""

        payload = {
            "backtest_id": backtest_id,
            "user_id": normalize_user_id(user_id),
            "tenant_id": self._runs.get(backtest_id, {}).get("tenant_id"),
            "status": status,
            "progress": progress,
            "message": message or error_message,
            "error_message": error_message,
            "full_error": full_error,
            "strategy_name": strategy_name,
            "benchmark_symbol": benchmark_symbol,
            "initial_capital": initial_capital,
            "information_ratio": information_ratio,
            "beta": beta,
            "benchmark_return": benchmark_return,
            "timestamp": datetime.now().isoformat(),
        }

        try:
            from backend.shared.redis_sentinel_client import get_redis_sentinel_client

            r = get_redis_sentinel_client()
            channel = f"qlib:backtest:progress:{backtest_id}"
            r.publish(channel, json.dumps(payload))
            r.set(
                f"qlib:backtest:status:{backtest_id}",
                json.dumps(payload).encode("utf-8"),
                ex=3600,
            )
        except Exception as re:
            task_logger.warning("redis_progress_failed", "Redis 进度推送失败", error=str(re))

    def _build_config_payload(
        self, request: Any, signal_meta: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return RiskAnalyzer._build_config_payload(request, signal_meta=signal_meta)

    def _build_market_state_kwargs(self, request: Any) -> dict[str, Any]:
        if not request.dynamic_position:
            return {}

        symbol = request.market_state_symbol or request.benchmark
        window = request.market_state_window
        strategy_total_position = (
            request.strategy_total_position if request.strategy_total_position is not None else 1.0
        )

        try:
            risk_series, position_by_state = self._market_state_service.build_risk_degree_series(
                symbol=symbol,
                start_date=request.start_date,
                end_date=request.end_date,
                window=window,
                style=request.style,
                strategy_total_position=strategy_total_position,
            )
        except Exception as exc:
            task_logger.warning("market_state_failed", "动态仓位计算失败", error=str(exc))
            return {"risk_degree": strategy_total_position}

        if not risk_series:
            return {"risk_degree": strategy_total_position}

        return {
            "risk_degree": strategy_total_position,
            "market_state_series": risk_series,
            "position_by_state": position_by_state,
            "strategy_total_position": strategy_total_position,
        }

    def _normalize_signal_config(self, signal: Any) -> Any:
        if signal is None:
            return "<PRED>"
        if isinstance(signal, dict):
            # module_path 仅用于定位模块，不是可调用标识；缺 class/func 时应直接回退。
            if "class" not in signal and "func" not in signal:
                return "$close"
            normalized = dict(signal)
            # 不设置空 module_path，避免 qlib importlib.import_module("") 崩溃
            if "module_path" in normalized and not normalized["module_path"]:
                del normalized["module_path"]
            return normalized
        return signal

    async def delete_backtest(self, backtest_id: str, user_id: str, tenant_id: str) -> bool:
        """删除回测记录"""
        user_id = normalize_user_id(user_id)
        if backtest_id in self._runs:
            run = self._runs[backtest_id]
            if run.get("user_id") != user_id or run.get("tenant_id") != tenant_id:
                task_logger.warning("delete_backtest_denied", "用户无权删除回测", user_id=user_id, backtest_id=backtest_id)
                return False
            del self._runs[backtest_id]

        # 1. 删除数据库记录
        success = await self._persistence.delete_run(backtest_id, user_id, tenant_id)

        # 2. 清理 Redis 缓存
        if success and self._cache:
            try:
                # 清理单条结果缓存
                cache_key = f"{tenant_id}:{user_id}:{backtest_id}" if tenant_id else backtest_id
                self._cache.delete_backtest_result(cache_key)

                # 清理状态缓存
                self._cache.client.delete(f"qlib:backtest:status:{backtest_id}")

                # 使该用户的历史列表缓存失效
                history_key = f"{tenant_id}:{user_id}" if tenant_id else user_id
                self._cache.invalidate_user_history(history_key)

                task_logger.info("delete_backtest_cache_cleared", "回测 Redis 缓存已清理", backtest_id=backtest_id, user_id=user_id)
            except Exception as e:
                task_logger.warning("delete_backtest_cache_clear_failed", "同步清理 Redis 缓存失败", error=str(e))

        return success

    async def compare_backtests(self, id1: str, id2: str, user_id: str, tenant_id: str) -> dict[str, Any]:
        """对比两个回测结果"""
        user_id = normalize_user_id(user_id)
        summary_fields = [
            "backtest_id",
            "status",
            "created_at",
            "completed_at",
            "user_id",
            "tenant_id",
            "config",
            "symbol",
            "strategy_name",
            "start_date",
            "end_date",
            "initial_capital",
            "benchmark_symbol",
            "total_return",
            "annual_return",
            "max_drawdown",
            "sharpe_ratio",
            "alpha",
            "beta",
            "information_ratio",
            "benchmark_return",
            "total_trades",
            "win_rate",
            "profit_factor",
            "avg_win",
            "avg_loss",
            "volatility",
            "execution_time",
            "error_message",
            "full_error",
            "model_name",
        ]
        results = await self._persistence.get_multiple_results(
            [id1, id2],
            tenant_id=tenant_id,
            user_id=user_id,
            include_fields=summary_fields,
        )

        if len(results) < 2:
            raise ValueError(f"无法找到足够的回测结果进行对比（需要2个，找到{len(results)}个）")

        result1 = next((r for r in results if r.backtest_id == id1), None)
        result2 = next((r for r in results if r.backtest_id == id2), None)

        if not result1 or not result2:
            raise ValueError("无法找到指定的回测结果")

        if (
            result1.config.get("user_id") != user_id
            or result2.config.get("user_id") != user_id
            or result1.tenant_id != tenant_id
            or result2.tenant_id != tenant_id
        ):
            raise ValueError("无权访问指定的回测结果")

        metrics_comparison = [
            {
                "metric": "年化收益率",
                "metric_key": "annual_return",
                "value1": result1.annual_return or 0.0,
                "value2": result2.annual_return or 0.0,
                "unit": "%",
                "higher_is_better": True,
            },
            {
                "metric": "夏普比率",
                "metric_key": "sharpe_ratio",
                "value1": result1.sharpe_ratio or 0.0,
                "value2": result2.sharpe_ratio or 0.0,
                "unit": "",
                "higher_is_better": True,
            },
            {
                "metric": "最大回撤",
                "metric_key": "max_drawdown",
                "value1": result1.max_drawdown or 0.0,
                "value2": result2.max_drawdown or 0.0,
                "unit": "%",
                "higher_is_better": False,
            },
            {
                "metric": "总收益率",
                "metric_key": "total_return",
                "value1": result1.total_return or 0.0,
                "value2": result2.total_return or 0.0,
                "unit": "%",
                "higher_is_better": True,
            },
            {
                "metric": "胜率",
                "metric_key": "win_rate",
                "value1": result1.win_rate or 0.0,
                "value2": result2.win_rate or 0.0,
                "unit": "%",
                "higher_is_better": True,
            },
        ]

        return {
            "backtest_id_1": id1,
            "backtest_id_2": id2,
            "backtest1": result1.dict(),
            "backtest2": result2.dict(),
            "result_1": result1.dict(),
            "result_2": result2.dict(),
            "metrics_comparison": metrics_comparison,
            "insights": [],
        }
