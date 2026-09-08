"""Strategy Builder System"""

import ast
import logging
import os
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple

from backend.services.engine.qlib_app.schemas.backtest import QlibBacktestRequest
from backend.services.engine.qlib_app.services.strategy_formatter import StrategyFormatterService
from backend.services.engine.qlib_app.utils.structured_logger import StructuredTaskLogger

logger = StructuredTaskLogger(logging.getLogger(__name__), "StrategyBuilder")


# 平台内置策略类 → 模块路径映射，供 CustomStrategyBuilder 自动补全 module_path
_BUILTIN_CLASS_MODULE_MAP: dict[str, str] = {
    "RedisTopkStrategy": "backend.services.engine.qlib_app.utils.extended_strategies",
    "RedisRecordingStrategy": "backend.services.engine.qlib_app.utils.recording_strategy",
    "RedisWeightStrategy": "backend.services.engine.qlib_app.utils.recording_strategy",
    "RedisLongShortTopkStrategy": "backend.services.engine.qlib_app.utils.extended_strategies",
    "RedisStopLossStrategy": "backend.services.engine.qlib_app.utils.extended_strategies",
    "RedisCrashBuyDipStrategy": "backend.services.engine.qlib_app.utils.extended_strategies",
    "SimpleWeightStrategy": "backend.services.engine.qlib_app.utils.recording_strategy",
}

# minibt 脚本框架仅能在专用运行时（AI-IDE / quantmind-minibt-runner 镜像）执行。
# 本引擎（qlib 回测）未安装该库，直接 exec 会抛 ModuleNotFoundError 裸栈
# （2026-09-08 回测中心实录：minibt_* 策略走 qlib 回测必失败），
# 因此在 AST 校验阶段拦截并给出可操作提示。
_MINIBT_REJECT_MESSAGE = (
    "该策略基于 minibt 脚本框架，qlib 回测引擎不支持；"
    "请在 AI-IDE 中使用 minibt 运行时回测，或改用 qlib 策略模板。"
)


class StrategyBuilder(ABC):
    """Abstract Base Class for Strategy Builders"""

    @abstractmethod
    def build(
        self,
        request: QlibBacktestRequest,
        market_state_kwargs: dict[str, Any],
        signal_data: Any,
        backtest_id: str,
    ) -> dict[str, Any]:
        pass

    def _get_redis_config(self, backtest_id: str) -> dict[str, Any]:
        return {
            "backtest_id": backtest_id,
            "redis_host": os.getenv("REDIS_HOST", "host.docker.internal"),
            "redis_port": int(os.getenv("REDIS_PORT", 6379)),
            "redis_password": os.getenv("REDIS_PASSWORD"),
        }

    def _sanitize_module_path_config(self, value: Any) -> Any:
        if isinstance(value, dict):
            normalized: dict[str, Any] = {}
            module_path = value.get("module_path")
            class_name = value.get("class")
            # 若 module_path 为空且 class 是内置类，自动补全
            if not module_path and class_name and class_name in _BUILTIN_CLASS_MODULE_MAP:
                module_path = _BUILTIN_CLASS_MODULE_MAP[class_name]
            if "class" in value or "module_path" in value:
                # 只有在有有效路径时才写入，避免将 None/空串传给 qlib
                if module_path:
                    normalized["module_path"] = module_path
            for key, item in value.items():
                if key == "module_path":
                    # 已在上方处理
                    continue
                normalized[key] = self._sanitize_module_path_config(item)
            return normalized
        if isinstance(value, list):
            return [self._sanitize_module_path_config(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self._sanitize_module_path_config(item) for item in value)
        return value


class TopkDropoutBuilder(StrategyBuilder):
    def build(
        self,
        request: QlibBacktestRequest,
        market_state_kwargs: dict[str, Any],
        signal_data: Any,
        backtest_id: str,
    ) -> dict[str, Any]:
        logger.info(
            "build_topk_dropout",
            "Building TopkDropout strategy",
            topk=request.strategy_params.topk,
            n_drop=request.strategy_params.n_drop,
            rebalance_days=request.strategy_params.rebalance_days,
        )
        strategy = {
            "class": "RedisRecordingStrategy",
            "module_path": "backend.services.engine.qlib_app.utils.recording_strategy",
            "kwargs": {
                "signal": "<PRED>",
                "topk": request.strategy_params.topk,
                "n_drop": (
                    request.strategy_params.n_drop
                    if request.strategy_params.n_drop > 0
                    else request.strategy_params.topk
                ),
                "rebalance_days": request.strategy_params.rebalance_days,
                "account_stop_loss": request.strategy_params.account_stop_loss,
                "max_leverage": request.strategy_params.max_leverage,
                **market_state_kwargs,
                **self._get_redis_config(backtest_id),
            },
        }
        return self._sanitize_module_path_config(strategy)


class WeightStrategyBuilder(StrategyBuilder):
    def build(
        self,
        request: QlibBacktestRequest,
        market_state_kwargs: dict[str, Any],
        signal_data: Any,
        backtest_id: str,
    ) -> dict[str, Any]:
        logger.info("build_weight_strategy", "Building WeightStrategy strategy")
        strategy = {
            "class": "RedisWeightStrategy",
            "module_path": "backend.services.engine.qlib_app.utils.recording_strategy",
            "kwargs": {
                "signal": "<PRED>",
                "topk": request.strategy_params.topk,
                "min_score": request.strategy_params.min_score,
                "max_weight": request.strategy_params.max_weight,
                "rebalance_days": request.strategy_params.rebalance_days,
                "account_stop_loss": request.strategy_params.account_stop_loss,
                "max_leverage": request.strategy_params.max_leverage,
                **market_state_kwargs,
                **self._get_redis_config(backtest_id),
            },
        }
        return self._sanitize_module_path_config(strategy)


class SimpleTopkBuilder(StrategyBuilder):
    def build(
        self,
        request: QlibBacktestRequest,
        market_state_kwargs: dict[str, Any],
        signal_data: Any,
        backtest_id: str,
    ) -> dict[str, Any]:
        logger.info(
            "build_simple_topk",
            "Building SimpleTopk strategy",
            topk=request.strategy_params.topk,
            n_drop=request.strategy_params.n_drop,
            rebalance_days=request.strategy_params.rebalance_days,
        )
        strategy = {
            "class": "RedisTopkStrategy",
            "module_path": "backend.services.engine.qlib_app.utils.extended_strategies",
            "kwargs": {
                "signal": "<PRED>",
                "topk": request.strategy_params.topk,
                "n_drop": (
                    request.strategy_params.n_drop
                    if request.strategy_params.n_drop > 0
                    else request.strategy_params.topk
                ),
                "rebalance_days": request.strategy_params.rebalance_days,
                "account_stop_loss": request.strategy_params.account_stop_loss,
                "max_leverage": request.strategy_params.max_leverage,
                **market_state_kwargs,
                **self._get_redis_config(backtest_id),
            },
        }
        return self._sanitize_module_path_config(strategy)


class DeepTimeSeriesBuilder(StrategyBuilder):
    def build(
        self,
        request: QlibBacktestRequest,
        market_state_kwargs: dict[str, Any],
        signal_data: Any,
        backtest_id: str,
    ) -> dict[str, Any]:
        logger.info("build_deep_time_series", "Building DeepTimeSeries strategy")

        # 修复：不再在此嵌入内联 signal dict（会绕过 Runtime 的实例化保障）。
        # 使用 "<PRED>" 占位符，由 Runtime 层统一通过 signal_data 替换，
        # 保持与 TopkDropoutBuilder 等一致的信号处理链路。
        # signal_data 已由 _build_signal_data() 构建（含 pred.pkl 加载逻辑），
        # 并经 Runtime 实例化保障块转换为合法 Signal 对象。
        strategy = {
            "class": "RedisRecordingStrategy",
            "module_path": "backend.services.engine.qlib_app.utils.recording_strategy",
            "kwargs": {
                "signal": "<PRED>",
                "topk": request.strategy_params.topk,
                "n_drop": (
                    request.strategy_params.n_drop
                    if request.strategy_params.n_drop > 0
                    else request.strategy_params.topk
                ),
                "rebalance_days": request.strategy_params.rebalance_days,
                "account_stop_loss": request.strategy_params.account_stop_loss,
                "max_leverage": request.strategy_params.max_leverage,
                **market_state_kwargs,
                **self._get_redis_config(backtest_id),
            },
        }
        return self._sanitize_module_path_config(strategy)


class AdaptiveDriftBuilder(StrategyBuilder):
    def build(
        self,
        request: QlibBacktestRequest,
        market_state_kwargs: dict[str, Any],
        signal_data: Any,
        backtest_id: str,
    ) -> dict[str, Any]:
        logger.info("build_adaptive_drift", "Building AdaptiveDrift strategy")
        # Ensure dynamic_position is True for this builder
        if not market_state_kwargs:
            # Fallback if not injected by backtest_service automatically
            market_state_kwargs = {"dynamic_position": True}

        strategy = {
            "class": "RedisRecordingStrategy",
            "module_path": "backend.services.engine.qlib_app.utils.recording_strategy",
            "kwargs": {
                "signal": "<PRED>",
                "topk": request.strategy_params.topk,
                "n_drop": (
                    request.strategy_params.n_drop
                    if request.strategy_params.n_drop > 0
                    else request.strategy_params.topk
                ),
                "drop_thresh": 30,
                "rebalance_days": request.strategy_params.rebalance_days,
                "account_stop_loss": request.strategy_params.account_stop_loss,
                "max_leverage": request.strategy_params.max_leverage,
                **market_state_kwargs,
                **self._get_redis_config(backtest_id),
            },
        }
        return self._sanitize_module_path_config(strategy)


class CustomStrategyBuilder(StrategyBuilder):
    def build(
        self,
        request: QlibBacktestRequest,
        market_state_kwargs: dict[str, Any],
        signal_data: Any,
        backtest_id: str,
    ) -> Any:
        logger.info("build_custom_strategy", "Building CustomStrategy from content")
        if not request.strategy_content or not request.strategy_content.strip():
            raise ValueError("Strategy content is empty")

        # 1. 触发中间层清洗代码
        formatter = StrategyFormatterService()
        clean_content = formatter.format_and_repair(request.strategy_content)

        # 2. 解析并获取配置/对象
        strategy, namespace = self._build_strategy_from_content(clean_content, request=request)
        strategy_module_name = namespace.get("__strategy_module_name__")

        # 2. 如果已经是实例化的 BaseStrategy 对象，直接返回
        from qlib.strategy.base import BaseStrategy

        if isinstance(strategy, BaseStrategy):
            return strategy

        # 3. 如果是字典配置，尝试合并业务参数
        if isinstance(strategy, dict) and "kwargs" in strategy and isinstance(strategy["kwargs"], dict):
            kwargs = strategy["kwargs"]
            # Track original user-specified kwargs BEFORE any injection
            original_kwargs = set(kwargs.keys())
            # 注入 Redis 记录和风险参数 (仅当用户代码中没有同名参数且使用的是支持的 Mixin 或类时)
            # 或者总是注入，让类自行决定是否 pop
            kwargs.update(self._get_redis_config(backtest_id))
            kwargs.update(market_state_kwargs)

            # --- Parameter Merging & Filtering [START] ---
            # Try to find the class in namespace to inspect for support
            class_name = strategy.get("class")
            if class_name in namespace:
                cls = namespace[class_name]
                import inspect

                try:
                    sig = inspect.signature(cls)
                    # All parameters the class explicitly accepts
                    explicit_params = set(sig.parameters.keys())
                    # Does it have **kwargs?
                    has_var_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())

                    # 1. Proactive Repair (Mandatory params)
                    mandatory_params = [
                        p.name
                        for p in sig.parameters.values()
                        if p.default == inspect.Parameter.empty
                        and p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.POSITIONAL_ONLY)
                        and p.name != "self"
                    ]
                    safe_defaults = {
                        "signal": signal_data if signal_data is not None else "<PRED>",
                        "topk": 50,
                        "n_drop": 10,
                        "pool_file_key": "",
                        "pool_file_url": "",
                        "condition": {},
                        "position_config": {},
                        "style_params": {},
                    }
                    for p in mandatory_params:
                        if p not in kwargs:
                            val = safe_defaults.get(p, None)
                            logger.info(
                                "patch_missing_mandatory_param",
                                "CustomStrategyBuilder patching missing mandatory param",
                                param=p,
                            )
                            kwargs[p] = val

                    # 2. Smart UI Slider Merging
                    # ONLY merge if it's already in kwargs OR explicitly in the signature
                    ui_params = [
                        "topk",
                        "n_drop",
                        "min_score",
                        "max_weight",
                        "stop_loss",
                        "take_profit",
                        "rebalance_days",
                        "enable_short_selling",
                        "margin_stock_pool",
                        "financing_rate",
                        "borrow_rate",
                        "max_short_exposure",
                        "max_leverage",
                        "account_stop_loss",
                    ]
                    # 调仓周期属于平台关键业务参数：即使用户代码中未显式声明，也应透传给支持 **kwargs 的策略。
                    force_passthrough_ui_params = {"rebalance_days"}
                    for key in ui_params:
                        val = getattr(request.strategy_params, key, None)
                        if val is not None:
                            if (
                                key in explicit_params
                                or key in kwargs
                                or (has_var_kwargs and key in force_passthrough_ui_params)
                            ):
                                # 特殊处理：n_drop=0 表示不限调仓即全速调仓
                                if key == "n_drop" and val == 0:
                                    val = getattr(request.strategy_params, "topk", 50)
                                logger.info(
                                    "merge_ui_param",
                                    "CustomStrategyBuilder merging UI param",
                                    key=key,
                                    value=val,
                                )
                                kwargs[key] = val

                    # 3. Final Filtering
                    # System params to be careful about
                    system_params = {"backtest_id", "redis_host", "redis_port", "redis_password", "dynamic_position"}

                    final_kwargs = {}
                    for k, v in kwargs.items():
                        # Keep if explicitly in signature
                        if k in explicit_params:
                            final_kwargs[k] = v
                        # If **kwargs is supported, keep if it's NOT a system/UI param we forced
                        # OR if it was already in the user's config (meaning they want it)
                        elif has_var_kwargs:
                            if k not in system_params and k not in ui_params:
                                final_kwargs[k] = v
                            elif (
                                k in original_kwargs or k in force_passthrough_ui_params
                            ):  # 保留用户显式参数与平台关键参数
                                final_kwargs[k] = v

                    strategy["kwargs"] = final_kwargs
                except Exception as e:
                    logger.warning(
                        "smart_adapt_failed",
                        "Failed to smart-adapt parameters for class",
                        class_name=class_name,
                        error=str(e),
                    )
            else:
                # Fallback UI Merging (external class):
                # 当 STRATEGY_CONFIG 只引用外部类（class 不在 namespace）时，
                # 仍然需要将 UI 参数回填到 kwargs，避免关键参数丢失。
                ui_params = [
                    "topk",
                    "n_drop",
                    "min_score",
                    "max_weight",
                    "stop_loss",
                    "take_profit",
                    "rebalance_days",
                    "enable_short_selling",
                    "margin_stock_pool",
                    "financing_rate",
                    "borrow_rate",
                    "max_short_exposure",
                    "max_leverage",
                    "account_stop_loss",
                ]
                for key in ui_params:
                    val = getattr(request.strategy_params, key, None)
                    if val is None:
                        continue
                    if key == "rebalance_days" or key in kwargs:
                        logger.info(
                            "fallback_merge_ui_param",
                            "CustomStrategyBuilder fallback merge UI param",
                            key=key,
                            value=val,
                            class_name=class_name,
                        )
                        kwargs[key] = val
            # --- Parameter Merging & Filtering [END] ---

        # 4. 如果类定义在动态模块中，优先回填 module_path，让 qlib 走标准反射链路。
        if (
            isinstance(strategy, dict)
            and not strategy.get("module_path")
            and strategy.get("class") in namespace
            and strategy_module_name
        ):
            strategy["module_path"] = str(strategy_module_name)

        # 5. 如果 module_path 仍为空，说明类仅在临时 namespace 中可见，最后再尝试手动实例化。
        if isinstance(strategy, dict) and not strategy.get("module_path"):
            class_name = strategy.get("class")
            if class_name in namespace:
                cls = namespace[class_name]
                logger.info(
                    "instantiate_local_strategy_class",
                    "Instantiating local strategy class",
                    class_name=class_name,
                )
                try:
                    return cls(**strategy.get("kwargs", {}))
                except TypeError:
                    import inspect

                    sig = inspect.signature(cls)
                    params = [
                        p.name
                        for p in sig.parameters.values()
                        if p.default == inspect.Parameter.empty
                        and p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.POSITIONAL_ONLY)
                    ]
                    missing = [p for p in params if p not in strategy.get("kwargs", {})]
                    if missing:
                        msg = f"Strategy class '{class_name}' requires mandatory arguments which were not provided: {missing}. Please check your STRATEGY_CONFIG."
                        logger.error(
                            "instantiate_local_strategy_missing_args",
                            msg,
                            class_name=class_name,
                            missing=missing,
                        )
                        raise ValueError(msg)
                    raise
                except Exception as e:
                    logger.error(
                        "instantiate_local_strategy_failed",
                        "Failed to instantiate local class",
                        class_name=class_name,
                        error=str(e),
                    )
                    # 如果实例化失败，我们仍然返回 dict，让 qlib 报错更详尽
            else:
                logger.warning(
                    "strategy_class_not_found",
                    "Strategy class not found in code namespace. Returning config dict.",
                    class_name=class_name,
                )

        return self._sanitize_module_path_config(strategy)

    def _validate_strategy_content(self, content: str) -> None:
        """AST security check"""
        if os.getenv("ALLOW_CUSTOM_STRATEGY", "true").lower() != "true":
            raise PermissionError("Custom strategy execution is disabled.")

        try:
            tree = ast.parse(content)
        except SyntaxError as e:
            raise ValueError(f"Syntax error in strategy code: {e}")

        blacklist = {
            "os",
            "sys",
            "subprocess",
            "shutil",
            "pathlib",
            "pickle",
            "marshal",
            "importlib",
            "imp",
            "pkgutil",
            "ctypes",
            "cffi",
            "mmap",
            "signal",
            "resource",
            "socket",
            "ssl",
            "http",
            "urllib",
            "urllib2",
            "urllib3",
            "requests",
            "httpx",
            "ftplib",
            "telnetlib",
            "smtplib",
            "imaplib",
            "threading",
            "multiprocessing",
            "concurrent",
            "asyncio",
            "code",
            "codeop",
            "compileall",
            "py_compile",
            "dis",
            "pty",
            "tty",
            "termios",
            "readline",
            "builtins",
            "gc",
            "inspect",
        }
        dangerous_dunders = {
            "__class__",
            "__bases__",
            "__subclasses__",
            "__globals__",
            "__builtins__",
            "__import__",
            "__loader__",
            "__spec__",
            "__code__",
            "__func__",
            "__self__",
            "__wrapped__",
        }

        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = []
                if isinstance(node, ast.Import):
                    names = [n.name.split(".")[0] for n in node.names]
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        names = [node.module.split(".")[0]]
                for name in names:
                    if name in blacklist:
                        raise ValueError(f"Importing dangerous module '{name}' is forbidden")
                    if name == "minibt":
                        raise ValueError(_MINIBT_REJECT_MESSAGE)

            if isinstance(node, ast.Attribute):
                if node.attr in dangerous_dunders:
                    raise ValueError(f"Accessing dangerous attribute '{node.attr}' is forbidden")

    def _build_strategy_from_content(
        self, content: str, request: QlibBacktestRequest | None = None
    ) -> tuple[Any, dict[str, Any]]:
        self._validate_strategy_content(content)

        import builtins
        import sys
        import types
        from uuid import uuid4

        # 创建一个虚拟模块，确保类定义的 __module__ 正确
        module_name = f"custom_strategy_{uuid4().hex}"
        mod = types.ModuleType(module_name)
        mod.__file__ = f"<{module_name}>"

        # 注入必要的全局变量
        exec_globals = mod.__dict__
        exec_globals["__builtins__"] = builtins.__dict__

        try:
            from qlib.strategy.base import BaseStrategy

            exec_globals["BaseStrategy"] = BaseStrategy

            # 执行用户代码
            exec(content, exec_globals)

            # 将模块注册到 sys.modules，防止反射失败
            sys.modules[module_name] = mod
            namespace = exec_globals
            namespace["__strategy_module_name__"] = module_name
        except Exception as exc:
            raise ValueError(f"Failed to execute strategy code: {exc}") from exc

        # 1. 尝试多种方式获取策略对象/配置
        strategy = None
        if "get_strategy_instance" in namespace:
            strategy = namespace["get_strategy_instance"]()
        elif "get_strategy_config" in namespace:
            strategy = namespace["get_strategy_config"]()
        elif "STRATEGY_CONFIG" in namespace:
            strategy = namespace["STRATEGY_CONFIG"]

        # 2. 处理已经实例化的策略对象
        if strategy is not None and isinstance(strategy, BaseStrategy):
            return strategy, namespace

        # 3. 如果 strategy 为空，尝试从命名空间中寻找唯一的 Strategy 类
        if strategy is None:
            strategy_classes = [
                name
                for name, obj in namespace.items()
                if isinstance(obj, type) and issubclass(obj, BaseStrategy) and obj is not BaseStrategy
            ]

            if len(strategy_classes) == 1:
                class_name = strategy_classes[0]
                logger.info("inferred_strategy_class", "Inferred strategy class", class_name=class_name)
                strategy = {"class": class_name, "module_path": "", "kwargs": {}}
            elif len(strategy_classes) > 1:
                raise ValueError(
                    f"Found multiple strategy classes {strategy_classes}. "
                    "Please define STRATEGY_CONFIG to specify which one to use."
                )
            else:
                raise ValueError(
                    "No STRATEGY_CONFIG found and no BaseStrategy subclasses found in code. "
                    "Ensure your strategy class inherits from BaseStrategy."
                )

        # 4. 最终校验并补齐字典配置
        if not isinstance(strategy, dict):
            raise ValueError(
                "Strategy code must provide STRATEGY_CONFIG(dict), "
                "get_strategy_config()(dict) or get_strategy_instance()(object)"
            )

        # 补齐默认值
        if "module_path" not in strategy:
            strategy["module_path"] = ""
        if "kwargs" not in strategy:
            strategy["kwargs"] = {}

        # 保存原始 kwargs 用于过滤判断
        if isinstance(strategy.get("kwargs"), dict):
            strategy["original_kwargs"] = dict(strategy["kwargs"])
        else:
            strategy["original_kwargs"] = {}

        if "class" not in strategy:
            # 再次尝试从命名空间推断
            strategy_classes = [
                name
                for name, obj in namespace.items()
                if isinstance(obj, type) and issubclass(obj, BaseStrategy) and obj is not BaseStrategy
            ]
            if len(strategy_classes) == 1:
                strategy["class"] = strategy_classes[0]
                logger.info(
                    "inferred_strategy_config_class",
                    "Inferred class for STRATEGY_CONFIG",
                    class_name=strategy["class"],
                )
            else:
                raise ValueError("STRATEGY_CONFIG missing 'class' key and could not be inferred.")

        return strategy, namespace


class StopLossBuilder(StrategyBuilder):
    def build(
        self,
        request: QlibBacktestRequest,
        market_state_kwargs: dict[str, Any],
        signal_data: Any,
        backtest_id: str,
    ) -> dict[str, Any]:
        logger.info(
            "build_stop_loss",
            "Building StopLoss strategy",
            topk=request.strategy_params.topk,
            stop_loss=request.strategy_params.stop_loss,
            take_profit=request.strategy_params.take_profit,
        )
        strategy = {
            "class": "RedisStopLossStrategy",
            "module_path": "backend.services.engine.qlib_app.utils.extended_strategies",
            "kwargs": {
                "signal": "<PRED>",
                "topk": request.strategy_params.topk,
                "n_drop": request.strategy_params.n_drop,
                "stop_loss": request.strategy_params.stop_loss,
                "take_profit": request.strategy_params.take_profit,
                "account_stop_loss": request.strategy_params.account_stop_loss,
                "max_leverage": request.strategy_params.max_leverage,
                **market_state_kwargs,
                **self._get_redis_config(backtest_id),
            },
        }
        return self._sanitize_module_path_config(strategy)



class LongShortTopkBuilder(StrategyBuilder):
    """多空 TopK 策略 Builder"""

    def build(
        self,
        request: QlibBacktestRequest,
        market_state_kwargs: dict[str, Any],
        signal_data: Any,
        backtest_id: str,
    ) -> dict[str, Any]:
        logger.info(
            "build_long_short_topk",
            "Building LongShortTopK strategy",
            topk=request.strategy_params.topk,
            short_topk=request.strategy_params.short_topk,
            rebalance_days=request.strategy_params.rebalance_days,
        )
        strategy = {
            "class": "RedisLongShortTopkStrategy",
            "module_path": "backend.services.engine.qlib_app.utils.extended_strategies",
            "kwargs": {
                "signal": "<PRED>",
                "topk": request.strategy_params.topk,
                "short_topk": request.strategy_params.short_topk,
                "min_score": request.strategy_params.min_score,
                "max_weight": request.strategy_params.max_weight,
                "long_exposure": request.strategy_params.long_exposure,
                "short_exposure": request.strategy_params.short_exposure,
                "rebalance_days": request.strategy_params.rebalance_days,
                "account_stop_loss": request.strategy_params.account_stop_loss,
                "max_leverage": request.strategy_params.max_leverage,
                **market_state_kwargs,
                **self._get_redis_config(backtest_id),
            },
        }
        return self._sanitize_module_path_config(strategy)



class CrashBuyDipBuilder(StrategyBuilder):
    """大盘暴跌抄底策略 Builder"""

    def build(
        self,
        request: QlibBacktestRequest,
        market_state_kwargs: dict[str, Any],
        signal_data: Any,
        backtest_id: str,
    ) -> dict[str, Any]:
        logger.info(
            "build_crash_buy_dip",
            "Building CrashBuyDip strategy",
            top_k=request.strategy_params.topk,
            hold_days=request.strategy_params.rebalance_days,
            stop_loss=request.strategy_params.stop_loss,
            take_profit=request.strategy_params.take_profit,
        )
        strategy = {
            "class": "RedisCrashBuyDipStrategy",
            "module_path": "backend.services.engine.qlib_app.utils.extended_strategies",
            "kwargs": {
                "top_k": request.strategy_params.topk,
                "hold_days": request.strategy_params.rebalance_days,
                "stop_loss": request.strategy_params.stop_loss,
                "take_profit": request.strategy_params.take_profit,
                "crash_threshold_points": 100,
                "crash_threshold_pct": 0.025,
                "max_wait_days": 3,
                "benchmark": request.benchmark or "SH000300",
                "account_stop_loss": request.strategy_params.account_stop_loss,
                "max_leverage": request.strategy_params.max_leverage,
                **market_state_kwargs,
                **self._get_redis_config(backtest_id),
            },
        }
        return self._sanitize_module_path_config(strategy)


class StrategyFactory:
    _builders = {
        # Native IDs
        "TopkDropout": TopkDropoutBuilder(),
        "WeightStrategy": WeightStrategyBuilder(),
        "CustomStrategy": CustomStrategyBuilder(),
        # Frontend Template Mapping (Native Call)
        "standard_topk": SimpleTopkBuilder(),
        "alpha_cross_section": WeightStrategyBuilder(),
        "long_short_topk": LongShortTopkBuilder(),
        "deep_time_series": DeepTimeSeriesBuilder(),
        "adaptive_drift": AdaptiveDriftBuilder(),
        "score_weighted": WeightStrategyBuilder(),
        # Extended strategies
        "simple_topk": SimpleTopkBuilder(),
        "momentum": TopkDropoutBuilder(),
        "stop_loss": StopLossBuilder(),
        "aggressive_topk_strategy": SimpleTopkBuilder(),
        "crash_buy_dip": CrashBuyDipBuilder(),
        # Upper case fallbacks for compatibility
        "Momentum": TopkDropoutBuilder(),
        "StopLoss": StopLossBuilder(),
        "CrashBuyDip": CrashBuyDipBuilder(),
    }

    _aliases = {
        "topkdropout": "TopkDropout",
        "weightstrategy": "WeightStrategy",
        "customstrategy": "CustomStrategy",
        "custom": "CustomStrategy",
        "custom_strategy": "CustomStrategy",
        "standard_topk": "standard_topk",
        "simple_topk": "simple_topk",
        "score_weighted": "score_weighted",
        "alpha_cross_section": "alpha_cross_section",
        "long_short_topk": "long_short_topk",
        "deep_time_series": "deep_time_series",
        "stoploss": "StopLoss",
        "momentum": "momentum",
        "crash_buy_dip": "crash_buy_dip",
        "crashbuydip": "CrashBuyDip",
    }

    @classmethod
    def _normalize_strategy_type(cls, strategy_type: str) -> str:
        raw = (strategy_type or "").strip()
        if not raw:
            return "TopkDropout"
        if raw in cls._builders:
            return raw
        lowered = raw.lower()
        return cls._aliases.get(lowered, raw)

    @classmethod
    def resolve_builder(cls, strategy_type: str) -> tuple[StrategyBuilder, bool, str]:
        normalized = cls._normalize_strategy_type(strategy_type)
        builder = cls._builders.get(normalized)
        if not builder:
            return cls._builders["TopkDropout"], True, normalized
        return builder, False, normalized

    @classmethod
    def get_builder(cls, strategy_type: str) -> StrategyBuilder:
        builder, is_fallback, _normalized = cls.resolve_builder(strategy_type)
        if is_fallback:
            # Fallback to TopkDropout if unknown to avoid crash
            logger.warning(
                "unknown_strategy_type_fallback",
                "Unknown strategy type, falling back to TopkDropout",
                strategy_type=strategy_type,
            )
        return builder

    @classmethod
    def register(cls, strategy_type: str, builder: StrategyBuilder):
        cls._builders[strategy_type] = builder
