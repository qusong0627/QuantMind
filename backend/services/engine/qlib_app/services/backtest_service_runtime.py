"""Qlib 回测服务"""

import asyncio
import json
import logging
import os
import random
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from uuid import uuid4

import numpy as np
import pandas as pd

from backend.services.engine.qlib_app.schemas.backtest import (
    QlibBacktestRequest,
    QlibBacktestResult,
)
from backend.services.engine.qlib_app.services.backtest_persistence import (
    BacktestPersistence,
)
from backend.services.engine.qlib_app.services.market_state_service import (
    MarketStateService,
)
from backend.services.engine.qlib_app.services.risk_analyzer import RiskAnalyzer
from backend.services.engine.qlib_app.services.strategy_builder import StrategyFactory
from backend.services.engine.qlib_app.services.strategy_templates import get_template_by_id
from backend.services.engine.qlib_app.utils.margin_position import ensure_margin_backtest_support
from backend.services.engine.qlib_app.utils.qlib_utils import (
    QLIB_BACKEND,
    D,
    backtest,
    exclude_bj_instruments,
    qlib,
)
from backend.services.engine.qlib_app.utils.strategy_adapter import StrategyAdapter
from backend.services.engine.qlib_app.utils.structured_logger import StructuredTaskLogger
from backend.shared.notification_publisher import publish_notification_async
from backend.shared.utils import normalize_user_id
from .backtest_service_query import QlibBacktestServiceQueryMixin

logger = logging.getLogger(__name__)
task_logger = StructuredTaskLogger(logger, "BacktestRuntime")


# 计算项目根目录
def _find_project_root() -> Path:
    try:
        curr = Path(__file__).resolve().parent
        for _ in range(10):
            if (curr / "GEMINI.md").exists() or (curr / "requirements.txt").exists():
                return curr
            if curr.parent == curr:
                break
            curr = curr.parent
    except Exception:
        pass
    return Path(os.getcwd())


PROJECT_ROOT = _find_project_root()
task_logger.info("project_root_resolved", "Project root resolved", root=str(PROJECT_ROOT))



class QlibBacktestServiceRuntimeMixin(QlibBacktestServiceQueryMixin):
    """Qlib 回测运行逻辑 mixin"""
    async def run_backtest(self, request: QlibBacktestRequest) -> QlibBacktestResult:
        """运行回测"""
        self._cleanup_stale_runs()
        start_time = time.time()
        signal_meta: dict[str, Any] = {"source": "unknown"}
        result: QlibBacktestResult | None = None
        is_optimization_child = (
            str(getattr(request, "history_source", "manual") or "manual").strip().lower() == "optimization"
        )

        backtest_id = getattr(request, "backtest_id", None) or uuid4().hex
        created_at = datetime.now()
        task_log = StructuredTaskLogger(
            logger,
            "qlib-backtest-runtime",
            {
                "backtest_id": backtest_id,
                "tenant_id": request.tenant_id,
                "user_id": request.user_id,
                "strategy_type": request.strategy_type,
            },
        )
        self._runs[backtest_id] = {
            "status": "running",
            "created_at": created_at,
            "completed_at": None,
            "user_id": request.user_id,
            "tenant_id": request.tenant_id,
        }
        if not is_optimization_child:
            await self._persistence.save_run(
                backtest_id=backtest_id,
                user_id=request.user_id,
                tenant_id=request.tenant_id,
                status="running",
                created_at=created_at,
                config=self._build_config_payload(request, signal_meta=signal_meta),
                result=None,
            )
        await self._notify_progress(
            backtest_id,
            request.user_id,
            status="running",
            progress=0.05,
            strategy_name=request.strategy_type,
            benchmark_symbol=request.benchmark,
            initial_capital=request.initial_capital,
        )

        try:
            self.initialize()
            self._set_deterministic_seed(self._resolve_seed(request.seed))

            # --- Storage Resolution [START] ---
            try:
                from backend.shared.storage_resolver import get_storage_resolver

                resolver = get_storage_resolver()

                if request.universe and (
                    "user_strategies/" in request.universe
                    or "stock_pool" in request.universe
                    or request.universe.startswith("cos://")
                ):
                    task_log.info("resolve_universe", "Resolving cloud universe key", universe=request.universe)
                    local_pool_path = await resolver.resolve_to_local_path(request.universe)
                    request.universe = str(local_pool_path)
                    task_log.info("resolve_universe_done", "Resolved universe to local path", universe=request.universe)

                if request.strategy_content:
                    if request.strategy_content.isdigit():
                        task_log.info("resolve_strategy", "Resolving DB strategy ID", strategy_content=request.strategy_content)
                        local_strategy_path = await resolver.resolve_to_local_path(request.strategy_content)
                        request.strategy_content = local_strategy_path.read_text(encoding="utf-8")
                    elif "user_strategies/" in request.strategy_content or request.strategy_content.startswith(
                        "cos://"
                    ):
                        task_log.info("resolve_strategy", "Resolving COS strategy key", strategy_content=request.strategy_content)
                        local_strategy_path = await resolver.resolve_to_local_path(request.strategy_content)
                        request.strategy_content = local_strategy_path.read_text(encoding="utf-8")
            except Exception as res_err:
                task_log.exception("storage_resolution_failed", "Storage resolution failed", error=res_err)
            # --- Storage Resolution [END] ---

            # --- Pool File Resolution [START] ---
            # If strategy code has POOL_FILE_LOCAL / POOL_FILE_URL / POOL_FILE_KEY,
            # use it to override universe with local-first priority.
            if request.strategy_content:
                import re

                pool_defs = re.findall(
                    r'^(POOL_FILE_LOCAL|POOL_FILE_URL|POOL_FILE_KEY)\s*=\s*["\']([^"\']+)["\']',
                    request.strategy_content,
                    re.MULTILINE,
                )
                if pool_defs:
                    pool_refs: dict[str, str] = {}
                    for var_name, var_value in pool_defs:
                        pool_refs[var_name.strip()] = var_value.strip()

                    for var_name in ("POOL_FILE_LOCAL", "POOL_FILE_URL", "POOL_FILE_KEY"):
                        pool_file_ref = pool_refs.get(var_name, "")
                        if not pool_file_ref:
                            continue
                        task_log.info("resolve_pool_file", "从策略代码提取到池文件引用", var_name=var_name, pool_file_ref=pool_file_ref)
                        try:
                            from backend.shared.storage_resolver import get_storage_resolver

                            resolver = get_storage_resolver()
                            local_pool_path = await resolver.resolve_to_local_path(pool_file_ref)
                            request.universe = str(local_pool_path)
                            task_log.info("pool_file_applied", "股票池已覆盖", universe=request.universe)
                            break
                        except Exception as pool_err:
                            task_log.warning(
                                "pool_resolution_failed",
                                "Pool file resolution failed, keep trying fallback refs",
                                var_name=var_name,
                                pool_file_ref=pool_file_ref,
                                error=pool_err,
                            )
            # --- Pool File Resolution [END] ---

            task_log.info("signal_raw", "原始signal配置", signal=request.strategy_params.signal)
            signal_data, signal_meta = await self._build_signal_data(request)
            self._enforce_signal_quality(signal_meta)
            is_dataframe = isinstance(signal_data, (pd.DataFrame, pd.Series))
            task_log.info(
                "signal_built",
                "处理后signal_data已构建",
                signal_data_type=type(signal_data).__name__,
                signal_data_kind="DataFrame/Series" if is_dataframe else signal_data,
                signal_meta=signal_meta,
            )

            # --- 日期自适应校准 [START] ---
            # 必须在构建策略和交易所配置之前完成日期截断，否则会导致配置冲突
            from qlib.data import D
            full_cal = D.calendar()
            cal_max_ts = pd.Timestamp(full_cal[-1].date())
            end_ts = pd.Timestamp(request.end_date)

            task_log.info("date_adjustment_start", "开始日期校准决策",
                         requested_end=request.end_date,
                         cal_max=str(cal_max_ts.date()),
                         signal_source=signal_meta.get("source"))

            try:
                # 1. 模型预测信号截断 (核心约束)
                max_signal_date = signal_meta.get("max_signal_date")
                if max_signal_date:
                    signal_ts = pd.Timestamp(max_signal_date)
                    if signal_ts < end_ts:
                        task_log.warning(
                            "signal_truncation_applied",
                            "当前信号文件数据不足，已强制将回测终点截断至信号最后一天",
                            original_end=str(end_ts.date()),
                            truncated_end=str(signal_ts.date()),
                        )
                        end_ts = signal_ts

                # 2. Qlib 物理日历边界检查
                if end_ts >= cal_max_ts:
                    actual_end_date = str(full_cal[-2].date())
                    task_log.info(
                        "calendar_limit_reached",
                        "检测到目标日期达到日历边界，执行安全回退",
                        target_ts=str(end_ts.date()),
                        actual_end=actual_end_date,
                    )
                else:
                    actual_end_date = str(end_ts.date())

                # 重要：同步更新 request 对象，确保后续所有模块（交易所、分析器、日志）对齐
                if request.end_date != actual_end_date:
                    task_log.info("request_date_synchronized", "同步更新请求对象日期",
                                 old=request.end_date, new=actual_end_date)
                    request.end_date = actual_end_date

            except Exception as cal_err:
                task_log.error("date_decision_error", "日期决策逻辑异常", error=str(cal_err))
                actual_end_date = request.end_date
            # --- 日期自适应校准 [END] ---

            # 构建策略配置 (使用工厂模式)
            market_state_kwargs = self._build_market_state_kwargs(request)
            builder = self._resolve_strategy_builder(request)
            strategy = builder.build(
                request=request,
                market_state_kwargs=market_state_kwargs,
                signal_data=signal_data,
                backtest_id=backtest_id,
            )

            # 在最终实例化前通过适配器
            strategy = self._adapter.adapt(
                strategy,
                context={"backtest_id": backtest_id, "universe": request.universe},
            )

            task_log.info("strategy_adapted", "策略配置清理与适配完成")

            # 最终检查并替换 signal (无论是自定义代码还是预设策略)
            if isinstance(strategy, dict) and "kwargs" in strategy and isinstance(strategy["kwargs"], dict):
                curr_signal = strategy["kwargs"].get("signal")
                if isinstance(curr_signal, dict):
                    normalized_curr_signal = self._normalize_signal_config(curr_signal)
                    if normalized_curr_signal != curr_signal:
                        task_log.warning(
                            "signal_recovered",
                            "检测到非法 signal dict 配置，已自动回退",
                            current_signal=curr_signal,
                            normalized_signal=normalized_curr_signal,
                        )
                        strategy["kwargs"]["signal"] = normalized_curr_signal
                        curr_signal = normalized_curr_signal
                if (
                    curr_signal == "<PRED>" or (isinstance(curr_signal, str) and curr_signal.startswith("$"))
                ) and signal_data is not None:
                    strategy["kwargs"]["signal"] = signal_data

            # 构建执行器配置
            executor = {
                "class": "SimulatorExecutor",
                "module_path": "qlib.backtest.executor",
                "kwargs": {
                    "time_per_step": "day",
                    "generate_portfolio_metrics": True,
                },
            }

            # 使用自定义 CnExchange
            comm = request.buy_cost if request.buy_cost is not None else request.commission
            tf = 0 if request.buy_cost is not None else request.transfer_fee
            tax = (request.sell_cost - comm) if request.sell_cost is not None else request.stamp_duty

            enable_short_selling = self._should_enable_short_selling(request)

            exchange_config = {
                "class": "CnExchange",
                "module_path": "backend.services.engine.qlib_app.utils.cn_exchange",
                "kwargs": {
                    "freq": "day",
                    "start_time": request.start_date,
                    "end_time": request.end_date,
                    "limit_threshold": 0.095,
                    "deal_price": request.deal_price,
                    "commission": comm,
                    "min_commission": request.min_commission,
                    "stamp_duty": max(0, tax),
                    "transfer_fee": tf,
                    "min_transfer_fee": request.min_transfer_fee,
                    "impact_cost_coefficient": request.impact_cost_coefficient,
                    "backtest_id": backtest_id,
                    "allow_short_selling": enable_short_selling,
                },
            }
            pos_type = "Position"
            if enable_short_selling:
                pos_type = ensure_margin_backtest_support()
            backtest_config = {
                "start_time": request.start_date,
                "end_time": request.end_date,
                "account": request.initial_capital,
                "benchmark": request.benchmark,
                "pos_type": pos_type,
                "exchange_kwargs": {
                    "exchange": exchange_config,
                },
            }

            if request.universe and request.universe != "csi300":
                task_log.info("custom_universe", "使用自定义股票池", universe=request.universe)
                # universe 只用于信号过滤，不传入 qlib.backtest()
                # （qlib.backtest 不接受 universe 参数）

            task_log.info(
                "run_start",
                "开始回测",
                strategy=request.strategy_type,
                period=f"{request.start_date}~{request.end_date}",
            )

            if "kwargs" in strategy:
                task_log.info("strategy_kwargs", "最终策略配置参数", kwargs=strategy["kwargs"])
            task_log.info(
                "rebalance_days",
                "最终调仓周期参数",
                rebalance_days=strategy["kwargs"].get("rebalance_days", "<missing; strategy default applies>"),
            )

            use_vect = getattr(request, "use_vectorized", False)
            task_log.info("engine_mode", "回测引擎模式", use_vectorized=use_vect, mode="vectorized" if use_vect else "step")

            if use_vect:
                task_log.info("vectorized_start", "启动驻留内存的极速向量化回测引擎")
                from backend.shared.vectorized_backtest.engine import VectorizedBacktestEngine, VectorizedBacktestConfig

                if isinstance(signal_data, str) and signal_data.startswith("$"):
                    raise ValueError("Vectorized backtest requires pre-computed predictions (DataFrame), not raw feature strings.")

                price_df = D.features(
                    D.instruments(request.universe),
                    ['$close'],
                    start_time=request.start_date,
                    end_time=request.end_date
                )

                cfg = VectorizedBacktestConfig(
                    initial_capital=request.initial_capital,
                    commission=comm + tax + tf,
                    slippage=request.impact_cost_coefficient,
                    topk=request.strategy_params.topk
                )

                v_engine = VectorizedBacktestEngine(cfg)
                v_res = await asyncio.to_thread(v_engine.run_backtest, signals=signal_data, prices=price_df)

                if not v_res.success:
                    raise RuntimeError(f"向量化极速回测执行失败: {v_res.error_message}")

                execution_time = time.time() - start_time
                task_log.info("vectorized_done", "向量化极速回测完成", execution_time=f"{execution_time:.2f}")

                result = QlibBacktestResult(
                    backtest_id=backtest_id,
                    tenant_id=request.tenant_id,
                    status="completed",
                    created_at=created_at,
                    completed_at=datetime.now(),
                    config=self._build_config_payload(request, signal_meta=signal_meta),
                    annual_return=v_res.annual_return,
                    sharpe_ratio=v_res.sharpe_ratio,
                    max_drawdown=v_res.max_drawdown,
                    total_return=v_res.total_return,
                    win_rate=v_res.win_rate,
                    execution_time=execution_time
                )
            else:
                portfolio_dict, indicator_dict = await asyncio.to_thread(
                    backtest,
                    strategy=strategy,
                    executor=executor,
                    **backtest_config,
                )

                execution_time = time.time() - start_time
                task_log.info("run_done", "回测完成", execution_time=f"{execution_time:.2f}")

                # 使用 RiskAnalyzer 提取指标
                async def analysis_progress_callback(val: float, msg: str | None = None):
                    await self._notify_progress(
                        backtest_id,
                        request.user_id,
                        status="running",
                        progress=val,
                        strategy_name=request.strategy_type,
                        message=msg,
                    )

                result = await RiskAnalyzer.analyze(
                    portfolio_dict=portfolio_dict,
                    request=request,
                    backtest_id=backtest_id,
                    created_at=created_at,
                    execution_time=execution_time,
                    signal_data=signal_data,
                    signal_meta=signal_meta,
                    on_progress=analysis_progress_callback,
                )

            self._runs[backtest_id].update(
                {
                    "status": result.status,
                    "completed_at": result.completed_at,
                    "result": result,
                }
            )
            if not is_optimization_child:
                await self._persistence.save_run(
                    backtest_id=backtest_id,
                    user_id=request.user_id,
                    tenant_id=request.tenant_id,
                    status=result.status,
                    created_at=created_at,
                    completed_at=result.completed_at,
                    config=self._build_config_payload(request, signal_meta=signal_meta),
                    result=result,
                )
            await self._notify_progress(
                backtest_id,
                request.user_id,
                status="completed",
                progress=1.0,
                strategy_name=request.strategy_type,
                benchmark_symbol=request.benchmark,
                initial_capital=request.initial_capital,
                information_ratio=result.information_ratio,
                beta=result.beta,
                benchmark_return=result.benchmark_return,
            )
            if not is_optimization_child:
                await publish_notification_async(
                    user_id=str(request.user_id),
                    tenant_id=str(request.tenant_id or "default"),
                    title="回测已完成",
                    content=f"{request.strategy_type} 回测完成，年化 {result.annual_return:.2%}，最大回撤 {result.max_drawdown:.2%}",
                    type="strategy",
                    level="success",
                    action_url="/backtest",
                )

            return result

        except Exception as e:
            execution_time = time.time() - start_time
            error_detail = traceback.format_exc()
            task_log.exception("run_failed", "回测失败", error=e)

            # Create failure result for persistence
            result = QlibBacktestResult(
                backtest_id=backtest_id,
                tenant_id=request.tenant_id,
                status="failed",
                created_at=created_at,
                completed_at=datetime.now(),
                config=self._build_config_payload(request, signal_meta=signal_meta),
                annual_return=0.0,
                sharpe_ratio=0.0,
                max_drawdown=0.0,
                alpha=0.0,
                error_message=f"{str(e)}",
                full_error=error_detail,
                execution_time=execution_time,
            )

            self._runs[backtest_id].update(
                {
                    "status": "failed",
                    "completed_at": datetime.now(),
                    "error_message": str(e),
                    "full_error": error_detail,
                }
            )
            if not is_optimization_child:
                await self._persistence.save_run(
                    backtest_id=backtest_id,
                    user_id=request.user_id,
                    tenant_id=request.tenant_id,
                    status="failed",
                    created_at=created_at,
                    completed_at=datetime.now(),
                    config=self._build_config_payload(request, signal_meta=signal_meta),
                    result=result,
                )
            await self._notify_progress(
                backtest_id,
                request.user_id,
                status="failed",
                progress=1.0,
                error_message=f"{str(e)}",
                full_error=error_detail,
            )
            if not is_optimization_child:
                await publish_notification_async(
                    user_id=str(request.user_id),
                    tenant_id=str(request.tenant_id or "default"),
                    title="回测执行失败",
                    content=f"{request.strategy_type} 回测失败：{str(e)}",
                    type="strategy",
                    level="error",
                    action_url="/backtest",
                )

            return result

    def _resolve_path(self, path_str: str) -> str | None:
        if not path_str:
            return None
        path = Path(path_str)
        if path.is_absolute():
            return str(path)
        resolved = PROJECT_ROOT / path
        return str(resolved)

    def _build_pred_signal_meta(
        self, pred: pd.DataFrame, pred_path: str, request: QlibBacktestRequest
    ) -> dict[str, Any]:
        datetime_index = pd.to_datetime(pred.index.get_level_values("datetime"))
        max_available_date = datetime_index.max()
        task_logger.info("signal_meta_extraction", "提取信号元数据",
                         max_date=str(max_available_date.date()) if not pd.isnull(max_available_date) else None)

        date_mask = (datetime_index >= pd.Timestamp(request.start_date)) & (
            datetime_index <= pd.Timestamp(request.end_date)
        )
        pred_in_range = pred.loc[date_mask]
        score = pred_in_range["score"] if "score" in pred_in_range.columns else pd.Series(dtype=float)
        nan_ratio = float(score.isna().mean()) if len(score) > 0 else 1.0
        return {
            "source": "pred_pkl",
            "pred_path": pred_path,
            "max_signal_date": str(max_available_date.date()) if not pd.isnull(max_available_date) else None,
            "rows_in_range": int(len(pred_in_range)),
            "date_count": int(pred_in_range.index.get_level_values("datetime").nunique()),
            "instrument_count": int(pred_in_range.index.get_level_values("instrument").nunique()),
            "score_nan_ratio": nan_ratio,
        }

    def _enforce_signal_quality(self, signal_meta: dict[str, Any]) -> None:
        source = signal_meta.get("source")
        require_pred = os.getenv("QLIB_BACKTEST_REQUIRE_PRED", "false").strip().lower() in {"1", "true", "yes", "on"}
        if source == "close_fallback" and require_pred:
            raise ValueError(
                f"信号质量预检失败：未使用模型预测信号，原因={signal_meta.get('fallback_reason', 'unknown')}"
            )
        if source != "pred_pkl":
            return

        min_dates = int(os.getenv("QLIB_SIGNAL_MIN_DATES", "30"))
        min_instruments = int(os.getenv("QLIB_SIGNAL_MIN_INSTRUMENTS", "100"))
        max_nan_ratio = float(os.getenv("QLIB_SIGNAL_MAX_NAN_RATIO", "0.2"))
        date_count = int(signal_meta.get("date_count") or 0)
        instrument_count = int(signal_meta.get("instrument_count") or 0)
        nan_ratio = float(signal_meta.get("score_nan_ratio") or 0.0)
        rows = int(signal_meta.get("rows_in_range") or 0)

        if rows <= 0:
            raise ValueError("信号质量预检失败：pred.pkl 在回测区间内无有效记录")
        if date_count < min_dates:
            raise ValueError(f"信号质量预检失败：有效交易日不足（{date_count} < {min_dates}）")
        if instrument_count < min_instruments:
            raise ValueError(f"信号质量预检失败：有效股票数不足（{instrument_count} < {min_instruments}）")
        if nan_ratio > max_nan_ratio:
            raise ValueError(f"信号质量预检失败：score 空值比例过高（{nan_ratio:.2%} > {max_nan_ratio:.2%}）")

    def _load_pred_pkl(self, pred_path: str, request: QlibBacktestRequest) -> tuple[Any, dict[str, Any]]:
        from backend.services.engine.qlib_app.utils.qlib_utils import np_patch

        with np_patch():
            try:
                # 支持 parquet 格式（将普通 DataFrame 转为 MultiIndex 格式）
                if pred_path.endswith(".parquet"):
                    raw = pd.read_parquet(pred_path, engine="pyarrow")
                    # 将训练输出格式转换为回测引擎格式
                    score_col = "pred" if "pred" in raw.columns else raw.columns[-1]
                    pred = (
                        raw[["trade_date", "symbol", score_col]]
                        .rename(columns={"trade_date": "datetime", "symbol": "instrument", score_col: "score"})
                        .assign(datetime=lambda d: pd.to_datetime(d["datetime"]))
                        .set_index(["datetime", "instrument"])
                        .sort_index()
                    )
                    task_logger.info("pred_parquet_loaded", "pred.parquet 加载并转换成功", rows=len(pred))
                else:
                    pred = pd.read_pickle(pred_path)
                if isinstance(pred, pd.Series):
                    pred = pred.to_frame("score")

                if not isinstance(pred, pd.DataFrame):
                    task_logger.warning("pred_format_invalid", "pred.pkl 格式错误", pred_type=type(pred).__name__)
                    return "$close", {
                        "source": "close_fallback",
                        "fallback_reason": "pred_pkl_invalid_type",
                        "pred_path": pred_path,
                    }

                if not (hasattr(pred, "index") and "datetime" in pred.index.names and "instrument" in pred.index.names):
                    task_logger.warning("pred_index_invalid", "pred.pkl 索引必须包含 datetime 和 instrument")
                    return "$close", {
                        "source": "close_fallback",
                        "fallback_reason": "pred_pkl_invalid_index",
                        "pred_path": pred_path,
                    }

                if "score" not in pred.columns:
                    score_col = pred.columns[-1]
                    pred = pred.rename(columns={score_col: "score"})

                task_logger.info("pred_pickle_loaded", "pred.pkl 加载成功，将直接使用文件中的预测作为信号")
                signal_meta = self._build_pred_signal_meta(pred, pred_path, request)
                return {
                    "class": "SimpleSignal",
                    "module_path": "backend.services.engine.qlib_app.utils.simple_signal",
                    "kwargs": {
                        "pred_path": pred_path,
                        "universe": request.universe,
                    },
                }, signal_meta

            except Exception as exc:
                task_logger.warning("load_pred_failed", "Load pred.pkl failed", pred_path=pred_path, error=str(exc))
                return {
                    "class": "SimpleSignal",
                    "module_path": "backend.services.engine.qlib_app.utils.simple_signal",
                    "kwargs": {
                        "metric": "$close",
                        "universe": request.universe,
                    },
                }, {
                    "source": "close_fallback",
                    "fallback_reason": "pred_pkl_load_failed",
                    "pred_path": pred_path,
                }

    async def _resolve_pred_path_from_model_registry(
        self,
        request: QlibBacktestRequest,
    ) -> tuple[str | None, dict[str, Any]]:
        tenant_id = str(request.tenant_id or "default")
        user_id_raw = str(request.user_id or "").strip()
        normalized_user_id = normalize_user_id(user_id_raw) if user_id_raw else ""
        strategy_id = str(request.strategy_id or "").strip() or None
        explicit_model_id = str(getattr(request, "model_id", "") or "").strip() or None

        meta: dict[str, Any] = {
            "tenant_id": tenant_id,
            "user_id": normalized_user_id,
            "requested_model_id": explicit_model_id,
            "strategy_id": strategy_id,
        }

        if not normalized_user_id:
            meta["model_resolution"] = "skipped"
            meta["fallback_reason"] = "missing_user_id"
            return None, meta

        try:
            from backend.shared.model_registry import model_registry_service

            resolved = await model_registry_service.resolve_effective_model(
                tenant_id=tenant_id,
                user_id=normalized_user_id,
                strategy_id=strategy_id,
                model_id=explicit_model_id,
            )
            meta.update(
                {
                    "model_resolution": "resolved",
                    "active_model_id": explicit_model_id or "",
                    "effective_model_id": resolved.effective_model_id,
                    "model_source": resolved.model_source,
                    "fallback_used": bool(resolved.fallback_used),
                    "fallback_reason": resolved.fallback_reason or "",
                    "model_storage_path": resolved.storage_path,
                    "model_file": resolved.model_file,
                }
            )
        except Exception as exc:
            task_logger.warning("resolve_model_failed", "Resolve model from registry failed", error=str(exc))
            meta["model_resolution"] = "failed"
            meta["fallback_reason"] = "model_registry_resolve_failed"
            meta["resolution_error"] = str(exc)
            return None, meta

        storage_path = str(meta.get("model_storage_path") or "").strip()
        if not storage_path:
            meta["fallback_reason"] = "empty_model_storage_path"
            return None, meta

        candidate_paths: list[Path] = []
        storage = Path(storage_path)
        # 优先检查 pred.pkl，再检查 pred.parquet（训练产物使用 parquet 格式）
        for pred_filename in ("pred.pkl", "pred.parquet"):
            candidate_paths.append(storage / pred_filename)
        if not storage.is_absolute():
            resolved_storage = self._resolve_path(storage_path)
            if resolved_storage:
                for pred_filename in ("pred.pkl", "pred.parquet"):
                    candidate_paths.append(Path(resolved_storage) / pred_filename)

        for candidate in candidate_paths:
            if candidate.exists():
                meta["resolved_pred_path"] = str(candidate)
                return str(candidate), meta

        meta["resolved_pred_path"] = str(candidate_paths[0]) if candidate_paths else ""
        meta["fallback_reason"] = "pred_pkl_not_found_in_model_storage"
        return None, meta

    async def _build_signal_data(self, request: QlibBacktestRequest) -> tuple[Any, dict[str, Any]]:
        signal = self._normalize_signal_config(request.strategy_params.signal)
        if isinstance(signal, dict):
            # qlib 的可调用配置至少要有 class 或 func，module_path 不能单独作为合法信号配置。
            if "class" not in signal and "func" not in signal:
                return "$close", {
                    "source": "close_fallback",
                    "fallback_reason": "invalid_signal_dict",
                }
            normalized = dict(signal)
            if normalized.get("module_path") is None:
                normalized["module_path"] = ""
            return normalized, {"source": "signal_dict"}
        if not isinstance(signal, str):
            return "$close", {
                "source": "close_fallback",
                "fallback_reason": "non_string_signal",
            }

        feature = signal.strip()

        if feature == "<PRED>":
            registry_pred_path, registry_meta = await self._resolve_pred_path_from_model_registry(request)
            if registry_pred_path and os.path.exists(registry_pred_path):
                signal_data, signal_meta = self._load_pred_pkl(registry_pred_path, request)
                return signal_data, {**registry_meta, **signal_meta}

            pred_path = os.getenv(
                "QLIB_PRED_PATH",
                "research/data_adapter/qlib_data/predictions/pred.pkl",
            )
            resolved_path = self._resolve_path(pred_path)
            if resolved_path and os.path.exists(resolved_path):
                signal_data, signal_meta = self._load_pred_pkl(resolved_path, request)
                merged_meta = {
                    **registry_meta,
                    **signal_meta,
                    "legacy_pred_path": resolved_path,
                }
                if merged_meta.get("source") == "pred_pkl" and not merged_meta.get("resolved_pred_path"):
                    merged_meta["resolved_pred_path"] = resolved_path
                return signal_data, merged_meta
            task_logger.warning(
                "pred_path_not_found",
                "QLIB_PRED_PATH not found, fallback to $close",
                pred_path=pred_path,
                resolved_path=resolved_path,
            )
            return "$close", {
                **registry_meta,
                "source": "close_fallback",
                "fallback_reason": "pred_path_not_found",
                "legacy_pred_path": resolved_path,
            }
        elif feature.endswith(".pkl"):
            resolved_path = self._resolve_path(feature)
            if resolved_path and os.path.exists(resolved_path):
                task_logger.info("load_prediction", "Loading prediction from path", feature=feature, resolved_path=resolved_path)
                return self._load_pred_pkl(resolved_path, request)
            else:
                task_logger.warning("model_file_missing", "Model file not found", feature=feature, resolved_path=resolved_path)

        if not feature.startswith("$"):
            feature = f"${feature}"

        try:
            # If universe is a local file path, read instruments directly
            if request.universe and os.path.isfile(request.universe):
                instrument_list = []
                with open(request.universe, encoding="utf-8") as fp:
                    for line in fp:
                        code = line.strip()
                        if code and not code.startswith("#"):
                            instrument_list.append(code)
                instrument_list = exclude_bj_instruments(instrument_list)
                task_logger.info("pool_loaded", "从池文件加载股票", instrument_count=len(instrument_list))
            else:
                instruments = D.instruments(request.universe)
                instrument_list = D.list_instruments(instruments, as_list=True)
                instrument_list = exclude_bj_instruments(instrument_list)
                max_instruments = int(os.getenv("QLIB_SIGNAL_MAX_INSTRUMENTS", "200"))
                if max_instruments > 0 and len(instrument_list) > max_instruments:
                    instrument_list = instrument_list[:max_instruments]
            df = D.features(
                instrument_list,
                [feature],
                start_time=request.start_date,
                end_time=request.end_date,
            )
            if df is None or df.empty:
                raise ValueError("signal data is empty")
            return df, {
                "source": "feature_field",
                "feature": feature,
                "rows_in_range": int(len(df)),
                "date_count": int(df.index.get_level_values("datetime").nunique()),
                "instrument_count": int(df.index.get_level_values("instrument").nunique()),
            }
        except Exception as exc:
            task_logger.warning("signal_build_failed", "Signal build failed", feature=feature, error=str(exc))
            return "$close", {
                "source": "close_fallback",
                "fallback_reason": "feature_build_failed",
                "feature": feature,
            }

    def _cleanup_stale_runs(self, ttl_hours: int = 2) -> None:
        """清理内存中超过 ttl_hours 小时的已完成/失败任务，防止内存泄漏"""
        cutoff = datetime.now().timestamp() - ttl_hours * 3600
        stale = [
            bid
            for bid, run in self._runs.items()
            if run["status"] in ("completed", "failed")
            and run.get("completed_at") is not None
            and run["completed_at"].timestamp() < cutoff
        ]
        for bid in stale:
            del self._runs[bid]
        if stale:
            task_logger.debug("cleanup_stale_runs", "清理过期回测记录", count=len(stale))
