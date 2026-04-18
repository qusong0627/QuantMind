import importlib
import logging
import os
import re
import sys
from contextlib import contextmanager
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)
from backend.services.engine.qlib_app.utils.structured_logger import StructuredTaskLogger

task_logger = StructuredTaskLogger(logger, "QlibUtils")


def _disabled_benchmark_series(start_time: Any = None) -> pd.Series:
    """构造一个“空影响”的基准序列，避免 qlib 在 benchmark=None 时回退到默认 SH000300。"""
    try:
        ts = pd.Timestamp(start_time).normalize() if start_time is not None else pd.Timestamp("1970-01-01")
    except Exception:
        ts = pd.Timestamp("1970-01-01")
    return pd.Series([0.0], index=pd.DatetimeIndex([ts]))


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def is_bj_instrument(code: str) -> bool:
    code_str = str(code or "").upper()
    return code_str.startswith("BJ")


def exclude_bj_instruments(codes: list[str]) -> list[str]:
    return [c for c in codes if not is_bj_instrument(c)]


@contextmanager
def np_patch():
    """
    NumPy 2.0 兼容性补丁上下文管理器。
    处理由旧版 NumPy (1.26.x) 生成的 pickle 文件在 NumPy 2.0 环境下的加载问题。
    """
    patched_modules = {}
    # 如果当前是旧版 NumPy (1.x)，但读取由 2.x 生成的 pickle，或者反之
    # 核心映射：处理 numpy._core 找不到的问题
    if not hasattr(np, "_core") and hasattr(np, "core"):
        # NumPy 1.x 模拟 2.x 的路径
        if "numpy._core" not in sys.modules:
            patched_modules["numpy._core"] = sys.modules.get("numpy._core")
            sys.modules["numpy._core"] = np.core

            submodules = [
                "multiarray",
                "umath",
                "numeric",
                "fromnumeric",
                "defchararray",
                "records",
                "memmap",
                "function_base",
                "machar",
                "getlimits",
                "shape_base",
                "einsumfunc",
                "dtype",
                "scalar",
            ]
            for sub in submodules:
                target = f"numpy._core.{sub}"
                if target not in sys.modules:
                    source = getattr(np.core, sub, None) or getattr(np, sub, None)
                    if source:
                        patched_modules[target] = sys.modules.get(target)
                        sys.modules[target] = source
    elif hasattr(np, "_core") and "numpy.core" not in sys.modules:
        # NumPy 2.x 模拟 1.x 的路径 (虽然通常 2.x 会自带兼容，但这里作为双重保险)
        sys.modules["numpy.core"] = np._core
        patched_modules["numpy.core"] = None

    try:
        yield
    finally:
        # 恢复环境
        for mod, old_val in reversed(patched_modules.items()):
            if old_val is None:
                if mod in sys.modules:
                    del sys.modules[mod]
            else:
                sys.modules[mod] = old_val


def safe_backtest(*args, **kwargs):
    """
    兼容性封装的 qlib backtest 函数。
    自动过滤在不同 Qlib 版本间可能引起冲突的参数。
    """
    # 显式关闭 qlib 默认 benchmark（默认值是 SH000300）。
    # qlib 在 benchmark=None 的路径里会把 benchmark_config 置为 {}，随后又回退默认 CSI300。
    # 因此这里改为传入一个“零收益”序列，彻底绕开默认基准加载。
    if "benchmark" not in kwargs or kwargs.get("benchmark") is None:
        kwargs["benchmark"] = _disabled_benchmark_series(kwargs.get("start_time"))

    if "server" in kwargs:
        task_logger.debug("safe_backtest_filter_legacy", "过滤掉 legacy 参数 server")
        kwargs.pop("server")

    task_logger.info(
        "safe_backtest_call",
        "执行 safe_backtest",
        has_benchmark=("benchmark" in kwargs),
        benchmark=kwargs.get("benchmark"),
    )

    try:
        from qlib.backtest import backtest as q_backtest

        return q_backtest(*args, **kwargs)
    except Exception as e:
        msg = str(e)
        # 当调用端传入了 benchmark 时，不论异常文案是否精确匹配，都兜底尝试禁用 benchmark。
        # 这样可以规避不同 qlib 版本/异常封装导致的文案差异。
        current_benchmark = kwargs.get("benchmark")
        if current_benchmark is not None:
            retry_kwargs = dict(kwargs)
            retry_kwargs["benchmark"] = _disabled_benchmark_series(kwargs.get("start_time"))
            try:
                task_logger.warning(
                    "safe_backtest_retry_without_benchmark",
                    "首次回测失败，自动替换为禁用基准序列后重试",
                    original_benchmark=current_benchmark,
                    original_error=msg,
                )
                return q_backtest(*args, **retry_kwargs)
            except Exception as retry_err:
                task_logger.warning(
                    "safe_backtest_retry_without_benchmark_failed",
                    "禁用 benchmark 重试仍失败，继续原异常处理",
                    retry_error=str(retry_err),
                )

        if _is_missing_benchmark_error(msg):
            benchmark = kwargs.get("benchmark")
            for candidate in _iter_benchmark_aliases(benchmark):
                retry_kwargs = dict(kwargs)
                retry_kwargs["benchmark"] = candidate
                try:
                    task_logger.warning(
                        "safe_backtest_retry_benchmark_alias",
                        "检测到 benchmark 不存在，尝试自动映射后重试",
                        original_benchmark=benchmark,
                        retry_benchmark=candidate,
                    )
                    return q_backtest(*args, **retry_kwargs)
                except Exception as retry_err:
                    if _is_missing_benchmark_error(str(retry_err)):
                        continue
                    raise

            if kwargs.get("benchmark") is not None or "benchmark" not in kwargs:
                retry_kwargs = dict(kwargs)
                retry_kwargs["benchmark"] = None
                task_logger.warning(
                    "safe_backtest_disable_benchmark",
                    "benchmark 不可用，自动禁用 benchmark 后重试",
                    original_benchmark=benchmark,
                )
                return q_backtest(*args, **retry_kwargs)

        task_logger.error("safe_backtest_failed", "safe_backtest 执行失败", error=str(e))
        raise


def _is_missing_benchmark_error(message: str) -> bool:
    msg = str(message or "").lower()
    return "benchmark" in msg and "does not exist" in msg


def _iter_benchmark_aliases(benchmark: Any) -> list[str]:
    code = str(benchmark or "").strip().upper()
    if not code:
        return []

    candidates: list[str] = []
    match = re.match(r"^(SH|SZ|BJ)(\d{6})$", code)
    if match:
        market, digits = match.groups()
        candidates.append(f"{digits}.{market}")
        candidates.append(digits)
    else:
        match = re.match(r"^(\d{6})\.(SH|SZ|BJ)$", code)
        if match:
            digits, market = match.groups()
            candidates.append(f"{market}{digits}")
            candidates.append(digits)
        elif re.match(r"^\d{6}$", code):
            market = "SZ" if code.startswith(("2", "3", "399")) else "SH"
            candidates.append(f"{market}{code}")
            candidates.append(f"{code}.{market}")

    deduped: list[str] = []
    seen: set[str] = {code}
    for item in candidates:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    return deduped


def resolve_qlib_backend(allow_mock: bool = None) -> tuple[Any, Any, Any, str]:
    """Resolve Qlib backend (real or mock)"""
    use_mock = env_bool("ENGINE_ALLOW_MOCK_QLIB", False) if allow_mock is None else allow_mock
    try:
        qlib_mod = importlib.import_module("qlib")
        # 优先使用包装后的 backtest
        backtest_fn = safe_backtest
        d_obj = importlib.import_module("qlib.data").D
        task_logger.info("backend_resolved", "使用真实 qlib 模块 (通过 safe_backtest 封装)", backend="real")
        return qlib_mod, backtest_fn, d_obj, "real"
    except Exception as real_err:
        if not use_mock:
            task_logger.error(
                "real_backend_unavailable",
                "真实 qlib 不可用且 ENGINE_ALLOW_MOCK_QLIB=false，拒绝回退到 mock",
                error=str(real_err),
            )
            raise ImportError("真实 qlib 不可用且 ENGINE_ALLOW_MOCK_QLIB=false，已禁用 mock 回退") from real_err

        mock_mod = importlib.import_module("backend.services.engine.qlib_mock")
        task_logger.warning("backend_fallback_mock", "真实 qlib 不可用，已启用 mock qlib", backend="mock")
        return mock_mod, mock_mod.backtest, mock_mod.D, "mock"


# Global instance for easy import
# 强制加载真实的 qlib，不再静默捕获 ImportError
# 如果加载失败，让进程在启动阶段就崩溃，暴露出底层的系统依赖或环境问题
qlib, backtest, D, QLIB_BACKEND = resolve_qlib_backend()
