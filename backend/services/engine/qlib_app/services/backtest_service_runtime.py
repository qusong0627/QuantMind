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
from typing import Any
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
from backend.services.engine.qlib_app.services.strategy_templates import (
    get_template_by_id,
)
from backend.services.engine.qlib_app.utils.margin_position import (
    ensure_margin_backtest_support,
)
from backend.services.engine.qlib_app.utils.benchmark_symbol import (
    normalize_benchmark_symbol,
)
from backend.services.engine.qlib_app.utils.qlib_utils import (
    QLIB_BACKEND,
    D,
    _disabled_benchmark_series,
    backtest,
    exclude_bj_instruments,
    qlib,
)
from backend.services.engine.qlib_app.utils.strategy_adapter import StrategyAdapter
from backend.services.engine.qlib_app.utils.structured_logger import (
    StructuredTaskLogger,
)
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
task_logger.info(
    "project_root_resolved", "Project root resolved", root=str(PROJECT_ROOT)
)


class QlibBacktestServiceRuntimeMixin(QlibBacktestServiceQueryMixin):
    """Qlib 回测运行逻辑 mixin"""

    async def run_backtest(self, request: QlibBacktestRequest) -> QlibBacktestResult:
        """运行回测"""
        self._cleanup_stale_runs()
        start_time = time.time()
        signal_meta: dict[str, Any] = {"source": "unknown"}
        result: QlibBacktestResult | None = None
        is_optimization_child = (
            str(getattr(request, "history_source", "manual") or "manual")
            .strip()
            .lower()
            == "optimization"
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
        request.benchmark = normalize_benchmark_symbol(request.benchmark)
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
            self.initialize(
                provider_uri=getattr(request, "qlib_provider_uri", None),
                region=getattr(request, "qlib_region", None),
            )
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
                    task_log.info(
                        "resolve_universe",
                        "Resolving cloud universe key",
                        universe=request.universe,
                    )
                    local_pool_path = await resolver.resolve_to_local_path(
                        request.universe
                    )
                    request.universe = str(local_pool_path)
                    task_log.info(
                        "resolve_universe_done",
                        "Resolved universe to local path",
                        universe=request.universe,
                    )

                if request.strategy_content:
                    if request.strategy_content.isdigit():
                        task_log.info(
                            "resolve_strategy",
                            "Resolving DB strategy ID",
                            strategy_content=request.strategy_content,
                        )
                        local_strategy_path = await resolver.resolve_to_local_path(
                            request.strategy_content
                        )
                        request.strategy_content = local_strategy_path.read_text(
                            encoding="utf-8"
                        )
                    elif (
                        "user_strategies/" in request.strategy_content
                        or request.strategy_content.startswith("cos://")
                    ):
                        task_log.info(
                            "resolve_strategy",
                            "Resolving COS strategy key",
                            strategy_content=request.strategy_content,
                        )
                        local_strategy_path = await resolver.resolve_to_local_path(
                            request.strategy_content
                        )
                        request.strategy_content = local_strategy_path.read_text(
                            encoding="utf-8"
                        )
            except Exception as res_err:
                task_log.exception(
                    "storage_resolution_failed",
                    "Storage resolution failed",
                    error=res_err,
                )
            # --- Storage Resolution [END] ---

            # --- Pool File Resolution [START] ---
            # If strategy code has POOL_FILE, use it to override universe.
            if request.strategy_content:
                import re

                pool_match = re.search(
                    r'^POOL_FILE\s*=\s*["\']([^"\']+)["\']',
                    request.strategy_content,
                    re.MULTILINE,
                )
                if pool_match:
                    pool_file = pool_match.group(1).strip()
                    task_log.info(
                        "resolve_pool_file",
                        "从策略代码提取到股票池文件",
                        pool_file=pool_file,
                    )
                    try:
                        from backend.shared.storage_resolver import get_storage_resolver

                        resolver = get_storage_resolver()
                        local_pool_path = await resolver.resolve_to_local_path(
                            pool_file
                        )
                        request.universe = str(local_pool_path)
                        task_log.info(
                            "pool_file_applied",
                            "股票池已覆盖",
                            universe=request.universe,
                        )
                    except Exception as pool_err:
                        task_log.warning(
                            "pool_resolution_failed",
                            "Pool file resolution failed",
                            pool_file=pool_file,
                            error=pool_err,
                        )
            # --- Pool File Resolution [END] ---

            task_log.info(
                "signal_raw", "原始signal配置", signal=request.strategy_params.signal,
                model_id=getattr(request, "model_id", None),
                strategy_id=getattr(request, "strategy_id", None),
            )
            signal_data, signal_meta = await self._build_signal_data(request)

            # --- 信号日期预截断 [START] ---
            # 在质量预检之前，先用信号数据的日期范围截断回测区间。
            # 这样当 pred.pkl 数据不覆盖回测部分区间时，不会因 rows_in_range=0 直接报错，
            # 而是自动截断到数据可用范围后再做质量检查。
            max_signal_date = signal_meta.get("max_signal_date")
            if max_signal_date:
                signal_ts = pd.Timestamp(max_signal_date)
                request_start_ts = pd.Timestamp(request.start_date)
                request_end_ts = pd.Timestamp(request.end_date)

                # 信号数据完全不覆盖回测区间 → 尝试自动切换到覆盖该区间的模型
                if signal_ts < request_start_ts:
                    original_model_id = getattr(request, "model_id", None)
                    task_log.warning(
                        "signal_data_out_of_range",
                        "当前模型信号不覆盖回测区间，尝试自动切换模型",
                        current_max_signal_date=max_signal_date,
                        requested_start=request.start_date,
                        current_model_id=original_model_id,
                    )
                    # 尝试查找覆盖回测起始日期的模型
                    swapped = await self._try_swap_to_covering_model(
                        request, request_start_ts
                    )
                    if swapped:
                        # 重新构建信号数据
                        signal_data, signal_meta = await self._build_signal_data(request)
                        new_max = signal_meta.get("max_signal_date")
                        task_log.info(
                            "model_auto_swapped",
                            "已自动切换到覆盖回测区间的模型",
                            old_model_id=original_model_id,
                            new_model_id=getattr(request, "model_id", None),
                            new_max_signal_date=new_max,
                        )
                        # 更新 signal_ts
                        if new_max:
                            signal_ts = pd.Timestamp(new_max)
                    else:
                        raise ValueError(
                            f"当前模型信号最晚日期为 {max_signal_date}，"
                            f"不覆盖回测起始日期 {request.start_date}，"
                            f"且未找到覆盖该区间的其他模型。"
                            f"请将回测起始日期调整至 {max_signal_date} 之前，"
                            f"或训练/选择覆盖该区间的模型。"
                        )

                # 信号数据部分覆盖：自动截断回测终点到信号最大日期
                # 后续的日期自适应校准会进一步处理，这里先确保 rows_in_range > 0
                if signal_ts < request_end_ts:
                    task_log.warning(
                        "signal_end_date_truncated",
                        "信号数据不覆盖回测终点，已截断回测区间到信号最大日期",
                        original_end=request.end_date,
                        truncated_end=str(signal_ts.date()),
                        signal_max_date=max_signal_date,
                    )
                    request.end_date = str(signal_ts.date())
            # --- 信号日期预截断 [END] ---

            self._enforce_signal_quality(signal_meta, request=request)
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

            try:
                full_cal = D.calendar(freq="day")
            except Exception as cal_err:
                task_log.warning(
                    "calendar_load_failed",
                    "Qlib calendar 加载失败，使用默认日期范围",
                    error=str(cal_err),
                )
                full_cal = pd.date_range(start=request.start_date, end=request.end_date, freq="B")
            cal_max_ts = pd.Timestamp(full_cal[-1].date())
            end_ts = pd.Timestamp(request.end_date)

            task_log.info(
                "date_adjustment_start",
                "开始日期校准决策",
                requested_end=request.end_date,
                cal_max=str(cal_max_ts.date()),
                signal_source=signal_meta.get("source"),
            )

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
                # 边界语义：cal_max_ts 是日历最后一天，若请求终点 >= cal_max_ts，
                # 则实际终点收缩到 cal_max_ts 本身（可用数据最后一天），而不是
                # 倒数第二天 full_cal[-2]。
                # 否则会污染两个下游环节：
                #   a) signal_end_date_truncated（上方）：信号只覆盖到 cal_max_ts，
                #      但 request.end_date 被写成 cal_max_ts-1，导致 rows_in_range
                #      少算一天；
                #   b) qlib.backtest() 以 request.end_date 作为终点：当日历完全
                #      不覆盖区间时 qlib 静默用工作日日历补 44 天空转（0 成交、
                #      全部指标 0），而收缩到 cal_max_ts 后即可正常出信号。
                if end_ts >= cal_max_ts:
                    actual_end_date = str(cal_max_ts.date())
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
                    task_log.info(
                        "request_date_synchronized",
                        "同步更新请求对象日期",
                        old=request.end_date,
                        new=actual_end_date,
                    )
                    request.end_date = actual_end_date

            except Exception as cal_err:
                task_log.error(
                    "date_decision_error", "日期决策逻辑异常", error=str(cal_err)
                )
                actual_end_date = request.end_date

            # 3. 区间反转守卫（必须在 try 之外，异常不能被上面的兜底吞掉）：
            #    请求起始日整体晚于行情日历最后可用日。
            #    qlib.backtest() 收到 start>end 的反转区间会静默用工作日日历
            #    补足空转（0 成交、全指标 0、状态 completed），前端表现为
            #    「回测完成但全是 0」。此处直接给出可操作的错误。
            request_start_ts = pd.Timestamp(request.start_date)
            actual_end_ts = pd.Timestamp(actual_end_date)
            if request_start_ts > actual_end_ts:
                raise ValueError(
                    f"回测起始日期 {request.start_date} 晚于行情数据最后可用日期 "
                    f"{actual_end_date}。请将回测区间调整到 {actual_end_date} 之前，"
                    f"或在数据管理页同步最新数据并重建 Qlib 缓存。"
                )
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
            if (
                isinstance(strategy, dict)
                and "kwargs" in strategy
                and isinstance(strategy["kwargs"], dict)
            ):
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
                    curr_signal is None
                    or curr_signal == "<PRED>"
                    or (isinstance(curr_signal, str) and curr_signal.startswith("$"))
                ) and signal_data is not None:
                    strategy["kwargs"]["signal"] = signal_data

                # --- 信号实例化保障 [START] ---
                # qlib create_signal_from() 不接受 dict 类型，必须在此统一实例化。
                # 涵盖以下路径：
                #   1. _load_pred_pkl 成功/失败返回的 SimpleSignal dict
                #   2. _build_signal_data 透传的 signal_dict 来源
                #   3. DeepTimeSeriesBuilder 嵌入的内联 signal dict
                final_signal = strategy["kwargs"].get("signal")
                if isinstance(final_signal, dict) and "class" in final_signal:
                    from qlib.utils import init_instance_by_config

                    task_log.info(
                        "signal_instantiating",
                        "检测到 dict 类型 signal，开始实例化",
                        signal_class=final_signal.get("class"),
                        module_path=final_signal.get("module_path", "<auto>"),
                    )
                    try:
                        instantiated_signal = init_instance_by_config(final_signal)
                        strategy["kwargs"]["signal"] = instantiated_signal
                        task_log.info(
                            "signal_instantiated",
                            "Signal dict 已成功实例化为对象",
                            signal_class=final_signal.get("class"),
                            signal_type=type(instantiated_signal).__name__,
                        )
                    except Exception as inst_err:
                        task_log.error(
                            "signal_instantiate_failed",
                            "Signal dict 实例化失败",
                            signal_class=final_signal.get("class"),
                            module_path=final_signal.get("module_path"),
                            error=str(inst_err),
                        )
                        raise ValueError(
                            f"信号实例化失败（class={final_signal.get('class')}）：{inst_err}。"
                            "请检查 pred_path 是否存在，或 SimpleSignal 模块路径是否正确。"
                        ) from inst_err
                # --- 信号实例化保障 [END] ---

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
            # 费率口径统一：无论走聚合费率(buy_cost/sell_cost)还是明细费率路径，
            # 过户费均按明细费率计算，佣金从聚合买入费率中剥离过户费部分，
            # 避免两条路径沪市费用口径不一致。
            market = self._infer_backtest_market(request)
            buy_cost = request.buy_cost
            sell_cost = request.sell_cost
            if market == "HK" and (buy_cost is not None or sell_cost is not None):
                # 港股禁聚合口径（明细与双边印花聚合并存会重复计提），忽略聚合字段走明细默认
                task_log.warning(
                    "hk_aggregated_fee_ignored",
                    "港股回测忽略聚合费率 buy_cost/sell_cost，改用市场明细默认",
                    buy_cost=buy_cost,
                    sell_cost=sell_cost,
                )
                buy_cost = None
                sell_cost = None
            comm = buy_cost if buy_cost is not None else request.commission
            tf = request.transfer_fee
            if buy_cost is not None:
                # 聚合买入费率 = 佣金 + 过户费（前端口径），剥离出纯佣金
                comm = max(0.0, buy_cost - request.transfer_fee)
            tax = (
                (sell_cost - buy_cost)
                if sell_cost is not None and buy_cost is not None
                else (
                    (sell_cost - request.commission)
                    if sell_cost is not None
                    else request.stamp_duty
                )
            )

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
                    # 港股无涨跌停：不读 $change 判停牌限制（防未来 HK 缓存补 change 后被 9.5% 误拦）
                    "has_price_limits": market != "HK",
                },
            }
            pos_type = "Position"
            if enable_short_selling:
                pos_type = ensure_margin_backtest_support()
            backtest_config = {
                "start_time": request.start_date,
                "end_time": request.end_date,
                "account": request.initial_capital,
                "benchmark": _disabled_benchmark_series(request.start_date),
                "pos_type": pos_type,
                "exchange_kwargs": {
                    "exchange": exchange_config,
                },
            }

            task_log.info(
                "benchmark_runtime_mode",
                "主回测阶段禁用 qlib 原生 benchmark，基准对比指标改由后分析阶段独立计算",
                requested_benchmark=request.benchmark,
            )

            if request.universe and request.universe != "csi300":
                task_log.info(
                    "custom_universe", "使用自定义股票池", universe=request.universe
                )
                # universe 只用于信号过滤，不传入 qlib.backtest()
                # （qlib.backtest 不接受 universe 参数）

            task_log.info(
                "run_start",
                "开始回测",
                strategy=request.strategy_type,
                period=f"{request.start_date}~{request.end_date}",
            )

            if "kwargs" in strategy:
                task_log.info(
                    "strategy_kwargs", "最终策略配置参数", kwargs=strategy["kwargs"]
                )
            task_log.info(
                "rebalance_days",
                "最终调仓周期参数",
                rebalance_days=strategy["kwargs"].get(
                    "rebalance_days", "<missing; strategy default applies>"
                ),
            )

            use_vect = getattr(request, "use_vectorized", False)
            if use_vect and not self._is_vectorized_safe(request, strategy):
                use_vect = False
                task_log.info(
                    "vectorized_safety_gate",
                    "策略含向量化引擎无法表达的逻辑，已退回 step 模式保证语义正确",
                    strategy_type=request.strategy_type,
                )
            task_log.info(
                "engine_mode",
                "回测引擎模式",
                use_vectorized=use_vect,
                mode="vectorized" if use_vect else "step",
                safety_gate_applied=True,
            )

            if use_vect:
                task_log.info("vectorized_start", "启动驻留内存的极速向量化回测引擎")
                from backend.shared.vectorized_backtest.engine import (
                    VectorizedBacktestEngine,
                    VectorizedBacktestConfig,
                )

                if isinstance(signal_data, str) and signal_data.startswith("$"):
                    raise ValueError(
                        "Vectorized backtest requires pre-computed predictions (DataFrame), not raw feature strings."
                    )

                # 将 SimpleSignal 配置/pred.pkl 物化为 DataFrame 供向量化引擎使用
                pred_df = self._materialize_signal_dataframe(signal_data, request)
                if pred_df.empty or "score" not in pred_df.columns:
                    raise ValueError(
                        "向量化极速回测信号为空或缺少 score 列，无法执行。"
                    )
                # 信号裁剪到回测区间：pred 常为全历史大表（如港股 2016→今 500 万行），
                # 全量传入会让引擎 valid_dates 交集/ffill 极端退化（实测 0 成交 0 收益）。
                # 已含 signal_lag 平移的日期（+1 交易日），裁剪按请求区间上界即可。
                if "datetime" in pred_df.index.names:
                    pred_df = pred_df[
                        (pred_df.index.get_level_values("datetime") >= request.start_date)
                        & (pred_df.index.get_level_values("datetime") <= request.end_date)
                    ]
                    if pred_df.empty:
                        raise ValueError(
                            "信号裁剪到回测区间后为空，请检查 pred 日期覆盖或调整回测区间。"
                        )
                if signal_meta.get("source") == "pred_pkl":
                    self._enforce_signal_quality(signal_meta, request=request)

                # 只加载信号覆盖的股票池，避免对 universe=all 全市场做全量 D.features 加载
                pred_instruments = sorted(
                    set(map(str, pred_df.index.get_level_values("instrument")))
                )
                if pred_instruments:
                    vectorized_universe = pred_instruments[: int(os.getenv("QLIB_SIGNAL_MAX_INSTRUMENTS", "2000"))]
                else:
                    vectorized_universe = D.instruments(request.universe)

                price_df = D.features(
                    vectorized_universe,
                    ["$close"],
                    start_time=request.start_date,
                    end_time=request.end_date,
                )

                # Load $change for limit-up/limit-down detection
                # （仅 CN 缓存有 change.day.bin；HK/US 等无涨跌停市场直接跳过，
                #   省去对全部标的的逐目录探测开销）
                change_df = None
                if market == "CN":
                    try:
                        change_df = D.features(
                            vectorized_universe,
                            ["$change"],
                            start_time=request.start_date,
                            end_time=request.end_date,
                        )
                    except Exception:
                        change_df = None

                cfg = VectorizedBacktestConfig(
                    initial_capital=request.initial_capital,
                    # 向量化引擎换手成本公式: avg_cost = commission + slippage + sell_cost*0.5
                    # commission 应为纯佣金(双向), sell_cost 为仅卖出侧费率(印花税+过户费)，
                    # 与主引擎 CnExchange 口径对齐, 不再重复计税。
                    commission=comm,
                    slippage=request.impact_cost_coefficient,
                    topk=request.strategy_params.topk,
                    sell_cost=max(0, tax + tf),
                )

                v_engine = VectorizedBacktestEngine(cfg)
                v_res = await asyncio.to_thread(
                    v_engine.run_backtest,
                    signals=pred_df,
                    prices=price_df,
                    changes=change_df,
                )

                if not v_res.success:
                    raise RuntimeError(f"向量化极速回测执行失败: {v_res.error_message}")

                execution_time = time.time() - start_time

                # 向量化引擎产出的是组合日收益曲线，回填 benchmark 对比指标与交易统计，
                # 补齐与 step 引擎一致的呈现（annual_return/sharpe/max_drawdown 已由向量化引擎计算）
                result = await RiskAnalyzer.analyze(
                    portfolio_dict=v_res.portfolio_dict or {"report": pd.DataFrame()},
                    request=request,
                    backtest_id=backtest_id,
                    created_at=created_at,
                    execution_time=execution_time,
                    signal_data=pred_df,
                    signal_meta=signal_meta,
                )
                task_log.info(
                    "vectorized_done",
                    "向量化极速回测完成",
                    execution_time=f"{execution_time:.2f}",
                )
            else:
                portfolio_dict, indicator_dict = await asyncio.to_thread(
                    backtest,
                    strategy=strategy,
                    executor=executor,
                    **backtest_config,
                )

                execution_time = time.time() - start_time
                task_log.info(
                    "run_done", "回测完成", execution_time=f"{execution_time:.2f}"
                )

                # 使用 RiskAnalyzer 提取指标
                async def analysis_progress_callback(
                    val: float, msg: str | None = None
                ):
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
                long_short_is_theoretical=request.strategy_params.short_topk > 0,
                signal_lag_days=request.signal_lag_days,
                deal_price=request.deal_price,
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
        lag_days = int(getattr(request, "signal_lag_days", 1) or 0)
        effective_pred = self._lag_signal_frame(pred, lag_days)
        datetime_index = (
            pd.to_datetime(effective_pred.index.get_level_values("datetime"))
            if len(effective_pred.index) > 0
            else pd.DatetimeIndex([])
        )
        max_available_date = datetime_index.max()
        task_logger.info(
            "signal_meta_extraction",
            "提取信号元数据",
            max_date=str(max_available_date.date())
            if not pd.isnull(max_available_date)
            else None,
        )

        date_mask = (
            (datetime_index >= pd.Timestamp(request.start_date))
            & (datetime_index <= pd.Timestamp(request.end_date))
            if len(datetime_index) > 0
            else []
        )
        pred_in_range = (
            effective_pred.loc[date_mask]
            if len(datetime_index) > 0
            else effective_pred.iloc[0:0]
        )
        score = (
            pred_in_range["score"]
            if "score" in pred_in_range.columns
            else pd.Series(dtype=float)
        )
        nan_ratio = float(score.isna().mean()) if len(score) > 0 else 1.0
        return {
            "source": "pred_pkl",
            "pred_path": pred_path,
            "max_signal_date": str(max_available_date.date())
            if not pd.isnull(max_available_date)
            else None,
            "rows_in_range": int(len(pred_in_range)),
            "date_count": int(
                pred_in_range.index.get_level_values("datetime").nunique()
            ),
            "instrument_count": int(
                pred_in_range.index.get_level_values("instrument").nunique()
            ),
            "score_nan_ratio": nan_ratio,
            "signal_lag_days": lag_days,
        }

    @staticmethod
    def _feature_fallback_allowed(request: QlibBacktestRequest | None = None) -> bool:
        if request is not None and bool(
            getattr(request, "allow_feature_signal_fallback", False)
        ):
            return True
        return os.getenv(
            "QLIB_ALLOW_FEATURE_SIGNAL_FALLBACK", "false"
        ).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    def _enforce_signal_quality(
        self,
        signal_meta: dict[str, Any],
        request: QlibBacktestRequest | None = None,
    ) -> None:
        source = signal_meta.get("source")
        if source == "close_fallback" and not self._feature_fallback_allowed(request):
            fallback_reason = signal_meta.get("fallback_reason", "unknown")
            pred_path_hint = (
                signal_meta.get("resolved_pred_path")
                or signal_meta.get("model_storage_path")
                or signal_meta.get("pred_path")
                or signal_meta.get("legacy_pred_path")
                or "<未解析>"
            )
            raise ValueError(
                f"信号质量预检失败：预测信号缺失（原因：{fallback_reason}）。"
                f"尝试查找的 pred 路径：{pred_path_hint}。"
                "如确需使用行情特征信号（$close），请显式设置 allow_feature_signal_fallback=true 或环境变量 QLIB_ALLOW_FEATURE_SIGNAL_FALLBACK=true。"
            )
        require_pred = os.getenv(
            "QLIB_BACKTEST_REQUIRE_PRED", "false"
        ).strip().lower() in {"1", "true", "yes", "on"}
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
            raise ValueError(
                f"信号质量预检失败：有效交易日不足（{date_count} < {min_dates}）"
            )
        if instrument_count < min_instruments:
            raise ValueError(
                f"信号质量预检失败：有效股票数不足（{instrument_count} < {min_instruments}）"
            )
        if nan_ratio > max_nan_ratio:
            raise ValueError(
                f"信号质量预检失败：score 空值比例过高（{nan_ratio:.2%} > {max_nan_ratio:.2%}）"
            )

    def _read_pred_frame(self, pred_path: str) -> pd.DataFrame | None:
        """读取 pred.pkl/pred.parquet 为规范化 MultiIndex (datetime, instrument) + score DataFrame。

        供向量化极速回测直接使用（信号需为 DataFrame 而非 SimpleSignal 配置）。
        """
        try:
            from backend.services.engine.qlib_app.utils.qlib_utils import np_patch

            with np_patch():
                if pred_path.endswith(".parquet"):
                    raw = pd.read_parquet(pred_path, engine="pyarrow")
                    score_col = "pred" if "pred" in raw.columns else raw.columns[-1]
                    pred = (
                        raw[["trade_date", "symbol", score_col]]
                        .rename(
                            columns={
                                "trade_date": "datetime",
                                "symbol": "instrument",
                                score_col: "score",
                            }
                        )
                        .assign(datetime=lambda d: pd.to_datetime(d["datetime"]))
                        .set_index(["datetime", "instrument"])
                        .sort_index()
                    )
                else:
                    pred = pd.read_pickle(pred_path)
            if isinstance(pred, pd.Series):
                pred = pred.to_frame("score")
            if not isinstance(pred, pd.DataFrame):
                return None
            if not (
                hasattr(pred, "index")
                and "datetime" in pred.index.names
                and "instrument" in pred.index.names
            ):
                return None
            if "score" not in pred.columns:
                pred = pred.rename(columns={pred.columns[-1]: "score"})
            return pred
        except Exception as exc:
            task_logger.warning(
                "read_pred_frame_failed",
                "读取 pred 为 DataFrame 失败",
                path=pred_path,
                error=str(exc),
            )
            return None

    @staticmethod
    def _to_qlib_prefix_code(code: str) -> str:
        """将股票代码转为 qlib 前缀格式（sh600000 / sz000001 / bj920000 / hk_0001.HK / us_aapl）。

        注意：非 CN 市场的 qlib instrument 保留原符号大小写（hk_0001.HK），
        只有 features 目录强制小写——这里必须与 instruments 池文件的写法一致。
        """
        raw = str(code or "").strip()
        # 港股后缀: 0001.HK / 1022.HK → hk_0001.HK（保留 .HK 大小写）
        if raw.endswith((".HK", ".hk")) and raw[:-3].isdigit():
            return "hk_" + raw
        s = raw.lower()
        if not s:
            return s
        # 已是 qlib 小写前缀格式
        if len(s) == 8 and s[:2] in {"sh", "sz", "bj"}:
            return s
        # 后缀格式: 600036.SH → sh600036
        if "." in s:
            parts = s.split(".")
            if len(parts) == 2 and len(parts[0]) == 6 and parts[0].isdigit():
                return parts[1].lower() + parts[0]
            # 港股后缀: 0001.HK / 1022.HK → hk_0001.hk
            if len(parts) == 2 and parts[1] == "hk" and parts[0].isdigit():
                return "hk_" + s
            # 美股带点后缀: BRK.B → us_brk.b
            if len(parts) == 2 and parts[0].isalpha() and len(parts[0]) <= 6 and len(parts[1]) <= 2:
                return "us_" + s
        # 前缀大写: SH600036 → sh600036
        if len(s) == 8 and s[:2] in {"sh", "sz", "bj"}:
            return s
        # 美股 ticker: aapl → us_aapl（已带市场前缀的不会重复加）
        if s.isalpha() and 1 <= len(s) <= 8 and not s.startswith(
            ("sh", "sz", "bj", "us_", "hk_", "bc_", "fut_")
        ):
            return "us_" + s
        # 纯6位数字: 600036 → sh600036
        if s.isdigit() and len(s) == 6:
            if s.startswith(("6", "9")):
                return "sh" + s
            if s.startswith(("0", "2", "3")):
                return "sz" + s
            if s.startswith(("4", "8")):
                return "bj" + s
        return s

    @staticmethod
    def _extract_strategy_config_from_code(content: str) -> dict[str, Any]:
        """从策略代码中提取 STRATEGY_CONFIG / get_strategy_config() 的配置。"""
        import ast

        config: dict[str, Any] = {}
        if not content:
            return config
        try:
            tree = ast.parse(content)
        except Exception:
            return config
        for node in tree.body:
            # STRATEGY_CONFIG = {...}
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = (
                    node.targets
                    if isinstance(node, ast.Assign)
                    else [node.target]
                )
                for t in targets:
                    if isinstance(t, ast.Name) and t.id == "STRATEGY_CONFIG":
                        try:
                            config = ast.literal_eval(node.value)
                        except Exception:
                            pass
                        return config
        return config

    @staticmethod
    def _vectorized_unsafe_strategy_class(class_name: str) -> bool:
        """策略类是否包含向量化引擎无法表达的逻辑。

        向量化引擎实现的是「每日 TopK 等权选股 + 涨跌停/停牌过滤 + 交易成本」。
        以下策略类包含超出该语义的逻辑，必须走 step 模式保真：
        - RedisWeightStrategy: 分数加权权重（非等权）
        - RedisTopkStrategy 是安全子集；任何自定义/其他类名视为不安全
        """
        name = str(class_name or "").strip().lower()
        if not name:
            return True
        if name in {"redistopkstrategy", "topkdropout", "standard_topk"}:
            return False
        return True

    def _is_vectorized_safe(
        self, request: QlibBacktestRequest, strategy: Any
    ) -> bool:
        """判断策略是否可由向量化极速引擎保真执行。

        安全条件（全部满足）：
          1. 信号是模型预测信号（<PRED> 或 pred 文件），非行情特征回退
          2. 策略类为纯 TopK 型（RedisTopkStrategy / TopkDropout）
          3. 无 pool_file 股票池覆盖（向量化引擎不加载池文件）
          4. 无显式配置的调仓周期 / 止损止盈 / 分数加权等参数

        注意：只以用户代码 STRATEGY_CONFIG 中的显式配置为准，
        schema 默认值（如 stop_loss=-0.08）不作为判据，否则会误伤默认策略。
        """
        try:
            # 1. 信号必须是 pred 类信号
            signal = self._normalize_signal_config(request.strategy_params.signal)
            if isinstance(signal, str) and not (
                signal.strip().upper() == "<PRED>"
                or signal.strip().lower().endswith((".pkl", ".parquet"))
            ):
                return False

            # 2. 从策略代码 STRATEGY_CONFIG 判断策略类与 kwargs（用户显式配置）
            content = str(getattr(request, "strategy_content", "") or "")
            cfg = self._extract_strategy_config_from_code(content)
            if cfg:
                class_name = str(cfg.get("class") or "").strip()
                if self._vectorized_unsafe_strategy_class(class_name):
                    return False
                kwargs = cfg.get("kwargs") or {}
                if kwargs.get("pool_file"):
                    return False
                if kwargs.get("rebalance_days") not in (None, 1):
                    return False
                if kwargs.get("stop_loss"):
                    return False
                if kwargs.get("take_profit"):
                    return False
                if kwargs.get("max_weight") or kwargs.get("min_score"):
                    return False  # 分数加权 → 非等权
                # n_drop 控制每期调仓数量：只有 n_drop>=topk（每期全换）才与向量化
                # 「每日全 TopK」语义一致；部分调仓/零换手约束向量化无法表达
                topk = int(kwargs.get("topk") or 50)
                n_drop = kwargs.get("n_drop")
                if n_drop is not None and int(n_drop) < topk:
                    return False
            elif isinstance(strategy, dict):
                # 无 STRATEGY_CONFIG：检查已解析策略对象
                class_name = str(strategy.get("class") or "").strip()
                if self._vectorized_unsafe_strategy_class(class_name):
                    return False
                kwargs = strategy.get("kwargs") or {}
                if kwargs.get("pool_file") or kwargs.get("max_weight") or kwargs.get("min_score"):
                    return False
                if kwargs.get("rebalance_days") not in (None, 1):
                    return False
                if kwargs.get("stop_loss") or kwargs.get("take_profit"):
                    return False
                topk = int(kwargs.get("topk") or 50)
                n_drop = kwargs.get("n_drop")
                if n_drop is not None and int(n_drop) < topk:
                    return False
            else:
                # 既无 STRATEGY_CONFIG 也非策略 dict：保守退回 step
                return False

            return True
        except Exception as exc:
            task_logger.warning(
                "vectorized_safety_check_failed",
                "向量化安全检测异常，保守退回 step 模式",
                error=str(exc),
            )
            return False

    def _align_pred_instruments(
        self, pred: pd.DataFrame, request: QlibBacktestRequest
    ) -> pd.DataFrame:
        """将 pred 信号的 instrument 对齐到 qlib 缓存代码格式。

        pred.pkl 常为后缀/前缀大写格式（000001.SH / SH600036），
        而 qlib 缓存为小写前缀格式（sh600000）。简单映射后按 universe
        校验，无法匹配的 instrument 剔除，避免向量化 price 对齐为空。
        """
        if pred.empty or "instrument" not in pred.index.names:
            return pred
        inst_values = pred.index.get_level_values("instrument")
        pred_codes = set(map(str, inst_values))
        # 快速检查：若已有一定重叠（按 qlib 小写前缀归一化后），无需转换
        raw_prefix = {self._to_qlib_prefix_code(c) for c in pred_codes}
        try:
            qlib_instruments = D.list_instruments(
                D.instruments(str(request.universe) or "all"), as_list=True
            )
        except Exception:
            qlib_instruments = []
        qlib_codes = set(qlib_instruments)
        if not qlib_codes:
            return pred
        overlap = len(raw_prefix & qlib_codes)
        if overlap == 0:
            task_logger.warning(
                "align_pred_instruments_no_overlap",
                "pred 与 qlib instrument 无重叠，信号将为空",
                pred_sample=list(pred_codes)[:3],
                qlib_sample=list(qlib_codes)[:3],
            )
            return pred.iloc[0:0]
        # 归一化为 qlib 小写前缀，剔除无法匹配的
        new_inst = pd.Series(
            inst_values.map(self._to_qlib_prefix_code).values,
            index=pred.index,
        )
        valid = new_inst.isin(qlib_codes)
        task_logger.info(
            "align_pred_instruments",
            "对齐 pred instrument 到 qlib 格式",
            before=len(inst_values),
            kept=int(valid.sum()),
            dropped=int((~valid).sum()),
        )
        result = pred.loc[valid].copy()
        # 用映射后的 instrument 重建 MultiIndex（保持与 datetime 的行级对齐）
        result.index = pd.MultiIndex.from_arrays(
            [
                result.index.get_level_values("datetime"),
                new_inst[valid].values,
            ],
            names=["datetime", "instrument"],
        )
        # pred 可能同时含两种代码格式（如 BJ920000 与 920000.BJ）映射到同一
        # qlib 代码，产生重复 (datetime, instrument)。去重（保留优先级最高的格式：
        # 原生小写前缀 > 大写前缀 > 后缀），否则向量化引擎 unstack 会因重复索引报错。
        if result.index.duplicated().any():
            task_logger.warning(
                "align_pred_instruments_duplicates",
                "pred 信号存在重复 (datetime, instrument)，已按格式优先级去重",
                duplicated=int(result.index.duplicated().sum()),
            )
            # 格式优先级：小写前缀=0（qlib 原生）> 大写前缀=1 > 后缀=2
            inst_str = result.index.get_level_values("instrument").astype(str)

            def _fmt_priority(code: str) -> int:
                c = code.lower()
                if len(c) == 8 and c[:2] in {"sh", "sz", "bj"}:
                    return 0
                if len(c) == 8 and c[:2] in {"SH", "SZ", "BJ"}:
                    return 1
                if "." in c:
                    return 2
                return 3

            prio = inst_str.map(_fmt_priority)
            result = result.assign(_prio=prio.values).sort_index()
            result = result[
                ~result.index.duplicated(keep="first")
            ].drop(columns="_prio")
        return result.sort_index()

    def _materialize_signal_dataframe(
        self, signal_data: Any, request: QlibBacktestRequest
    ) -> pd.DataFrame:
        """将信号物化为 DataFrame 供向量化极速回测使用。

        SimpleSignal 配置按 kwargs.pred_path 读取；已是 DataFrame/Series 则直接使用。
        读取 pred_path 时会应用 signal_lag_days 滞后（与 SimpleSignal 内部行为一致），
        避免向量化路径出现信号超前一天的前视偏差。
        """
        lag_days = int(getattr(request, "signal_lag_days", 1) or 0)
        if isinstance(signal_data, pd.Series):
            return signal_data.to_frame("score")
        if isinstance(signal_data, pd.DataFrame):
            return signal_data
        if isinstance(signal_data, dict):
            pred_path = (signal_data.get("kwargs") or {}).get("pred_path")
            if pred_path and os.path.exists(pred_path):
                pred = self._read_pred_frame(pred_path)
                if pred is not None:
                    pred = self._align_pred_instruments(pred, request)
                    if lag_days > 0:
                        pred = self._lag_signal_frame(pred, lag_days)
                    return pred
        raise ValueError(
            "向量化极速回测需要 DataFrame 信号（pred.pkl 或特征表），当前信号无法物化。"
        )

    def _load_pred_pkl(
        self, pred_path: str, request: QlibBacktestRequest
    ) -> tuple[Any, dict[str, Any]]:
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
                        .rename(
                            columns={
                                "trade_date": "datetime",
                                "symbol": "instrument",
                                score_col: "score",
                            }
                        )
                        .assign(datetime=lambda d: pd.to_datetime(d["datetime"]))
                        .set_index(["datetime", "instrument"])
                        .sort_index()
                    )
                    task_logger.info(
                        "pred_parquet_loaded",
                        "pred.parquet 加载并转换成功",
                        rows=len(pred),
                    )
                else:
                    pred = pd.read_pickle(pred_path)
                if isinstance(pred, pd.Series):
                    pred = pred.to_frame("score")

                if not isinstance(pred, pd.DataFrame):
                    task_logger.warning(
                        "pred_format_invalid",
                        "pred.pkl 格式错误",
                        pred_type=type(pred).__name__,
                    )
                    return "$close", {
                        "source": "close_fallback",
                        "fallback_reason": "pred_pkl_invalid_type",
                        "pred_path": pred_path,
                        "signal_lag_days": int(
                            getattr(request, "signal_lag_days", 1) or 0
                        ),
                    }

                if not (
                    hasattr(pred, "index")
                    and "datetime" in pred.index.names
                    and "instrument" in pred.index.names
                ):
                    task_logger.warning(
                        "pred_index_invalid",
                        "pred.pkl 索引必须包含 datetime 和 instrument",
                    )
                    return "$close", {
                        "source": "close_fallback",
                        "fallback_reason": "pred_pkl_invalid_index",
                        "pred_path": pred_path,
                    }

                if "score" not in pred.columns:
                    score_col = pred.columns[-1]
                    pred = pred.rename(columns={score_col: "score"})

                task_logger.info(
                    "pred_pickle_loaded",
                    "pred.pkl 加载成功，将直接使用文件中的预测作为信号",
                )
                signal_meta = self._build_pred_signal_meta(pred, pred_path, request)
                return {
                    "class": "SimpleSignal",
                    "module_path": "backend.services.engine.qlib_app.utils.simple_signal",
                    "kwargs": {
                        "pred_path": pred_path,
                        "universe": request.universe,
                        "signal_lag_days": int(
                            getattr(request, "signal_lag_days", 1) or 0
                        ),
                    },
                }, signal_meta

            except Exception as exc:
                task_logger.warning(
                    "load_pred_failed",
                    "Load pred.pkl failed",
                    pred_path=pred_path,
                    error=str(exc),
                )
                # pkl 读取失败（如远程 AutoDL numpy2 打包）时，回退同目录 pred.parquet
                if pred_path.endswith(".pkl"):
                    parquet_path = pred_path[:-4] + ".parquet"
                    if os.path.exists(parquet_path):
                        task_logger.info(
                            "load_pred_parquet_fallback",
                            "pred.pkl 读取失败，回退同目录 pred.parquet",
                            pkl=pred_path,
                            parquet=parquet_path,
                        )
                        return self._load_pred_pkl(parquet_path, request)
                return {
                    "class": "SimpleSignal",
                    "module_path": "backend.services.engine.qlib_app.utils.simple_signal",
                    "kwargs": {
                        "metric": "$close",
                        "universe": request.universe,
                        "signal_lag_days": int(
                            getattr(request, "signal_lag_days", 1) or 0
                        ),
                    },
                }, {
                    "source": "close_fallback",
                    "fallback_reason": "pred_pkl_load_failed",
                    "pred_path": pred_path,
                }

    @staticmethod
    def _infer_backtest_market(request: "QlibBacktestRequest") -> str:
        """从回测请求中推断目标市场（CN/HK/US/CRYPTO）。"""
        # 1. 从 qlib_provider_uri 或 qlib_region 推断
        provider_uri = str(getattr(request, "qlib_provider_uri", "") or "").lower()
        region = str(getattr(request, "qlib_region", "") or "").lower()
        if "hk_data" in provider_uri or region == "hk":
            return "HK"
        if "us_data" in provider_uri or region == "us":
            return "US"
        if "crypto_data" in provider_uri or region == "crypto":
            return "CRYPTO"
        # 2. 从 benchmark 推断
        benchmark = str(getattr(request, "benchmark_symbol", "") or "").upper()
        if "HSI" in benchmark or "HSCEI" in benchmark or "HSTECH" in benchmark:
            return "HK"
        if "SPX" in benchmark or "NDX" in benchmark or "DJI" in benchmark:
            return "US"
        if "BTC" in benchmark or "ETH" in benchmark:
            return "CRYPTO"
        # 3. 从 universe 路径推断
        universe = str(getattr(request, "universe", "") or "").lower()
        if "hk" in universe:
            return "HK"
        if "us" in universe:
            return "US"
        if "crypto" in universe:
            return "CRYPTO"
        # 默认 A 股
        return "CN"

    @staticmethod
    def _infer_model_market(model: dict) -> str:
        """从模型元数据中推断市场（CN/HK/US/CRYPTO）。"""
        meta = model.get("metadata_json") or {}
        if isinstance(meta, str):
            try:
                import json
                meta = json.loads(meta)
            except Exception:
                meta = {}
        # 1. 从 metadata_json.context.market 推断
        context = meta.get("context") or {}
        if isinstance(context, dict):
            market = str(context.get("market") or "").upper().strip()
            if market in ("HK", "HONG_KONG", "港股"):
                return "HK"
            if market in ("US", "美股"):
                return "US"
            if market in ("CRYPTO", "加密"):
                return "CRYPTO"
            if market in ("CN", "A_SHARE", "A股", "CHINA"):
                return "CN"
        # 2. 从 benchmark 推断
        benchmark = str(context.get("benchmark") or "").upper()
        if "HSI" in benchmark:
            return "HK"
        if "SPX" in benchmark or "NDX" in benchmark:
            return "US"
        # 3. 从 model_id 推断（如包含 HK/US/CRYPTO）
        model_id = str(model.get("model_id") or "").upper()
        if "_HK" in model_id:
            return "HK"
        if "_US" in model_id:
            return "US"
        if "_CRYPTO" in model_id:
            return "CRYPTO"
        # 无法确定时返回空字符串，表示不限制
        return ""

    async def _try_swap_to_covering_model(
        self,
        request: "QlibBacktestRequest",
        target_start_ts: pd.Timestamp,
    ) -> bool:
        """
        当前模型的 pred.pkl 不覆盖回测起始日期时，自动在用户的所有可用模型中
        查找 pred.pkl 覆盖目标日期的模型，找到后更新 request.model_id。
        返回 True 表示已切换，False 表示未找到。
        """
        import pickle as _pickle

        try:
            from backend.shared.model_registry import model_registry_service

            tenant_id = str(request.tenant_id or "default")
            user_id = str(request.user_id or "").strip()
            if not user_id:
                return False

            models = await model_registry_service.list_models(
                tenant_id=tenant_id, user_id=user_id, include_archived=False
            )
            # 按更新时间降序，优先选最新的模型
            models.sort(
                key=lambda m: str(m.get("updated_at") or ""), reverse=True
            )

            # 确定当前回测的目标市场
            request_market = self._infer_backtest_market(request)

            for m in models:
                if str(m.get("status") or "") not in ("ready", "active"):
                    continue
                # 排除市场不匹配的模型（港股模型不能跑 A 股回测等）
                model_market = self._infer_model_market(m)
                if model_market and request_market and model_market != request_market:
                    continue
                storage_path = str(m.get("storage_path") or "")
                model_id = str(m.get("model_id") or "")
                if not storage_path:
                    continue
                # 检查 pred.parquet（优先，pyarrow 跨版本稳定），pred.pkl 兜底
                pred_path = Path(storage_path) / "pred.parquet"
                if not pred_path.exists():
                    pred_path = Path(storage_path) / "pred.pkl"
                if not pred_path.exists():
                    continue
                # 快速读取 pred 最大日期（只读 index，不加载全量数据）
                try:
                    if str(pred_path).endswith(".parquet"):
                        import pyarrow.parquet as pq

                        pf = pq.ParquetFile(str(pred_path))
                        # 从 footer metadata 中读取日期列统计（训练产物列名为 trade_date）
                        schema_names = list(pf.schema_arrow.names)
                        td_idx = -1
                        for i, n in enumerate(schema_names):
                            if n == "trade_date" or n == "datetime":
                                td_idx = i
                                break
                        if td_idx < 0:
                            continue
                        md = pf.metadata
                        max_date = None
                        for rg in range(md.num_row_groups):
                            col = md.row_group(rg).column(td_idx)
                            stats = col.statistics
                            if stats and stats.has_min_max:
                                from datetime import date as _date

                                v = stats.max
                                if isinstance(v, bytes):
                                    v = v.decode()
                                try:
                                    d = pd.Timestamp(str(v)[:10])
                                    if max_date is None or d > max_date:
                                        max_date = d
                                except Exception:
                                    continue
                        if max_date is not None and max_date >= target_start_ts:
                            request.model_id = model_id
                            task_logger.info(
                                "auto_swap_model",
                                "自动切换到覆盖回测区间的模型",
                                old_model_id=getattr(request, "_original_model_id", None),
                                new_model_id=model_id,
                                pred_max_date=str(max_date.date()),
                            )
                            return True
                    else:
                        # pickle: 只读 index 的 datetime level
                        with open(pred_path, "rb") as f:
                            pred = _pickle.load(f)
                        if hasattr(pred, "index") and len(pred.index) > 0:
                            dates = pred.index.get_level_values("datetime")
                            max_date = pd.Timestamp(dates.max())
                            if max_date >= target_start_ts:
                                request.model_id = model_id
                                task_logger.info(
                                    "auto_swap_model",
                                    "自动切换到覆盖回测区间的模型",
                                    new_model_id=model_id,
                                    pred_max_date=str(max_date.date()),
                                )
                                return True
                except Exception:
                    continue

            return False
        except Exception as exc:
            task_logger.warning(
                "swap_model_failed",
                "自动切换模型失败",
                error=str(exc),
            )
            return False

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
            task_logger.warning(
                "resolve_model_failed",
                "Resolve model from registry failed",
                error=str(exc),
            )
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
        # 优先 pred.parquet（pyarrow 格式跨 numpy 版本稳定），pred.pkl 仅当
        # 无 parquet 时回退（pkl 由远程 AutoDL numpy2 打包时本容器读不了）。
        for pred_filename in ("pred.parquet", "pred.pkl"):
            candidate_paths.append(storage / pred_filename)
        if not storage.is_absolute():
            resolved_storage = self._resolve_path(storage_path)
            if resolved_storage:
                for pred_filename in ("pred.parquet", "pred.pkl"):
                    candidate_paths.append(Path(resolved_storage) / pred_filename)

        for candidate in candidate_paths:
            if candidate.exists():
                meta["resolved_pred_path"] = str(candidate)
                return str(candidate), meta

        # 融合模型（model_file=ensemble_config.json）无 pred.pkl 时，
        # 自动用子模型 pred 融合生成，避免 AI-IDE 回测因缺信号失败。
        model_file = str(meta.get("model_file") or "").strip()
        if "ensemble_config" in model_file:
            try:
                from backend.services.engine.services.prediction_artifact import (
                    generate_ensemble_pred,
                )

                generated = generate_ensemble_pred(model_dir=storage)
                meta["resolved_pred_path"] = str(generated)
                meta["ensemble_pred_generated"] = True
                return str(generated), meta
            except Exception as gen_err:
                task_logger.warning(
                    "ensemble_pred_generation_failed",
                    "融合模型 pred 自动生成失败",
                    model_dir=str(storage),
                    error=str(gen_err),
                )
                meta["ensemble_pred_generation_error"] = str(gen_err)

        meta["resolved_pred_path"] = str(candidate_paths[0]) if candidate_paths else ""
        meta["fallback_reason"] = "pred_pkl_not_found_in_model_storage"
        return None, meta

    async def _build_signal_data(
        self, request: QlibBacktestRequest
    ) -> tuple[Any, dict[str, Any]]:
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
            (
                registry_pred_path,
                registry_meta,
            ) = await self._resolve_pred_path_from_model_registry(request)
            if registry_pred_path and os.path.exists(registry_pred_path):
                signal_data, signal_meta = self._load_pred_pkl(
                    registry_pred_path, request
                )
                return signal_data, {**registry_meta, **signal_meta}

            pred_path = os.getenv(
                "QLIB_PRED_PATH",
                "db/qlib_data/predictions/pred.pkl",
            )
            resolved_path = self._resolve_path(pred_path)
            if resolved_path and os.path.exists(resolved_path):
                signal_data, signal_meta = self._load_pred_pkl(resolved_path, request)
                merged_meta = {
                    **registry_meta,
                    **signal_meta,
                    "legacy_pred_path": resolved_path,
                }
                if merged_meta.get("source") == "pred_pkl" and not merged_meta.get(
                    "resolved_pred_path"
                ):
                    merged_meta["resolved_pred_path"] = resolved_path
                return signal_data, merged_meta
            # 显式指定了模型但无预测文件：抛明确错误，避免静默 fallback $close 空转
            explicit_model_id = str(getattr(request, "model_id", "") or "").strip()
            allow_fallback = bool(
                getattr(request, "allow_feature_signal_fallback", True)
            )
            if explicit_model_id:
                raise ValueError(
                    f"模型 {explicit_model_id} 无可用预测文件（pred.pkl/pred.parquet）。"
                    "请先在模型管理页对该模型执行推理生成预测，再运行回测。"
                    f"（已检查: {registry_pred_path or registry_meta.get('resolved_pred_path', '')}）"
                )
            if not allow_fallback:
                raise ValueError(
                    "未找到预测文件（pred.pkl）且未允许特征信号回退。"
                    "请先在模型管理页执行推理生成预测，或启用 allow_feature_signal_fallback。"
                )

            task_logger.warning(
                "pred_path_not_found",
                "QLIB_PRED_PATH not found, fallback to $close (no model_id)",
                pred_path=pred_path,
                resolved_path=resolved_path,
            )
            return {
                "class": "SimpleSignal",
                "module_path": "backend.services.engine.qlib_app.utils.simple_signal",
                "kwargs": {
                    "metric": "$close",
                    "universe": request.universe,
                    "signal_lag_days": int(getattr(request, "signal_lag_days", 1) or 0),
                },
            }, {
                **registry_meta,
                "source": "close_fallback",
                "fallback_reason": "pred_path_not_found",
                "legacy_pred_path": resolved_path,
                "signal_lag_days": int(getattr(request, "signal_lag_days", 1) or 0),
            }
        elif feature.endswith((".pkl", ".parquet")):
            resolved_path = self._resolve_path(feature)
            if resolved_path and os.path.exists(resolved_path):
                task_logger.info(
                    "load_prediction",
                    "Loading prediction from path",
                    feature=feature,
                    resolved_path=resolved_path,
                )
                return self._load_pred_pkl(resolved_path, request)
            else:
                task_logger.warning(
                    "model_file_missing",
                    "Model file not found",
                    feature=feature,
                    resolved_path=resolved_path,
                )

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
                task_logger.info(
                    "pool_loaded",
                    "从池文件加载股票",
                    instrument_count=len(instrument_list),
                )
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
            lag_days = int(getattr(request, "signal_lag_days", 1) or 0)
            df = self._lag_signal_frame(df, lag_days)
            return df, {
                "source": "feature_field",
                "feature": feature,
                "rows_in_range": int(len(df)),
                "date_count": int(df.index.get_level_values("datetime").nunique()),
                "instrument_count": int(
                    df.index.get_level_values("instrument").nunique()
                ),
                "signal_lag_days": lag_days,
            }
        except Exception as exc:
            task_logger.warning(
                "signal_build_failed",
                "Signal build failed",
                feature=feature,
                error=str(exc),
            )
            return {
                "class": "SimpleSignal",
                "module_path": "backend.services.engine.qlib_app.utils.simple_signal",
                "kwargs": {
                    "metric": "$close",
                    "universe": request.universe,
                    "signal_lag_days": int(getattr(request, "signal_lag_days", 1) or 0),
                },
            }, {
                "source": "close_fallback",
                "fallback_reason": "feature_build_failed",
                "feature": feature,
                "signal_lag_days": int(getattr(request, "signal_lag_days", 1) or 0),
            }

    @staticmethod
    def _lag_signal_frame(
        data: pd.DataFrame | pd.Series, lag_days: int
    ) -> pd.DataFrame | pd.Series:
        if (
            lag_days <= 0
            or not isinstance(data.index, pd.MultiIndex)
            or "datetime" not in data.index.names
        ):
            return data
        date_values = pd.to_datetime(
            data.index.get_level_values("datetime")
        ).normalize()
        unique_dates = pd.Index(date_values.unique()).sort_values()
        if len(unique_dates) == 0:
            return data
        shifted_dates = unique_dates.to_series(index=unique_dates).shift(-lag_days)
        mapped_dates = date_values.map(shifted_dates)
        valid_mask = ~pd.isna(mapped_dates)
        if not valid_mask.any():
            return data.iloc[0:0]
        result = data.loc[valid_mask].copy()
        arrays = []
        for name in result.index.names:
            if name == "datetime":
                arrays.append(pd.DatetimeIndex(mapped_dates[valid_mask]))
            else:
                arrays.append(result.index.get_level_values(name))
        result.index = pd.MultiIndex.from_arrays(arrays, names=result.index.names)
        return result.sort_index()

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
            task_logger.debug(
                "cleanup_stale_runs", "清理过期回测记录", count=len(stale)
            )
