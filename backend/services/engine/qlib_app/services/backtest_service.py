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
from backend.shared.notification_publisher import publish_notification_async
from backend.shared.utils import normalize_user_id
from backend.services.engine.qlib_app.utils.structured_logger import StructuredTaskLogger
from .backtest_service_runtime import QlibBacktestServiceRuntimeMixin

logger = logging.getLogger(__name__)
task_logger = StructuredTaskLogger(logger, "BacktestService")


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


def _infer_market_from_uri(provider_uri: str, region: str | None = None) -> str:
    """从 provider_uri 路径与 region 推断市场（沿用原有的关键字判定规则）。"""
    low = str(provider_uri or "").lower()
    reg = str(region or "").lower()
    if "hk_data" in low or reg == "hk":
        return "HK"
    if "us_data" in low or reg == "us":
        return "US"
    if "bc_data" in low or reg == "crypto":
        return "CRYPTO"
    if "futures_data" in low:
        return "FUTURES"
    return "CN"


class QlibBacktestService(QlibBacktestServiceRuntimeMixin):
    """Qlib 回测服务"""

    def __init__(
        self,
        provider_uri: str | None = None,
        region: str = "cn",
    ):
        if provider_uri is None:
            env_val = os.getenv("QLIB_DATA_PATH", "").strip()
            if env_val:
                provider_uri = env_val
        if provider_uri is not None:
            # 旧客户端/旧环境变量里钉死的 /app/db/qlib_data 归一到 qlib_paths 解析结果，
            # 否则夜间同步写一份缓存、回测读另一份
            from backend.shared.qlib_paths import normalize_qlib_provider_uri

            provider_uri = normalize_qlib_provider_uri(provider_uri)
        else:
            from backend.shared.qlib_paths import resolve_qlib_provider_uri
            provider_uri = resolve_qlib_provider_uri()

        if not provider_uri.startswith("~") and not os.path.isabs(provider_uri):
            try:
                potential_path = PROJECT_ROOT / provider_uri
                if potential_path.exists():
                    provider_uri = str(potential_path)
                    task_logger.info("provider_uri_resolved", "Resolved provider_uri", provider_uri=provider_uri)
            except Exception:
                pass

        self.provider_uri = provider_uri
        self.region = region
        self._initialized = False
        self._runs: dict[str, dict[str, Any]] = {}
        self._persistence = BacktestPersistence()
        self._seed = self._load_seed()
        self._kernels = self._load_kernels()
        self._joblib_backend = self._load_joblib_backend()
        self._adapter = StrategyAdapter(PROJECT_ROOT)
        self._market_state_service = MarketStateService()

        from backend.services.engine.qlib_app.cache_manager import get_cache_manager

        try:
            self._cache = get_cache_manager()
            task_logger.info("cache_manager_initialized", "缓存管理器已初始化")
        except Exception as e:
            task_logger.warning("cache_manager_init_failed", "缓存管理器初始化失败，将不使用缓存", error=str(e))
            self._cache = None

    def _load_seed(self) -> int | None:
        seed = os.getenv("QLIB_BACKTEST_SEED")
        if not seed:
            return None
        try:
            return int(seed)
        except ValueError:
            task_logger.warning("invalid_seed", "Invalid QLIB_BACKTEST_SEED", seed=seed)
            return None

    def _load_kernels(self) -> int:
        raw = os.getenv("QLIB_BACKTEST_KERNELS")
        if raw:
            try:
                value = int(raw)
                if value > 0:
                    return value
            except ValueError:
                task_logger.warning("invalid_kernels", "Invalid QLIB_BACKTEST_KERNELS", value=raw)
        return max(1, min((os.cpu_count() or 1), 8))

    def _load_joblib_backend(self) -> str:
        backend = os.getenv("QLIB_JOBLIB_BACKEND")
        if backend:
            return backend.strip()
        return "threading" if os.name == "nt" else "loky"

    def _set_deterministic_seed(self, seed: int | None) -> None:
        if seed is None:
            return
        random.seed(seed)
        np.random.seed(seed)
        try:
            import torch

            torch.manual_seed(seed)
            if torch.cuda.is_available():
                torch.cuda.manual_seed_all(seed)
            torch.backends.cudnn.deterministic = True
            torch.backends.cudnn.benchmark = False
        except Exception:
            pass

    def _resolve_seed(self, request_seed: int | None) -> int | None:
        return request_seed if request_seed is not None else self._seed

    @staticmethod
    def _should_enable_short_selling(request: QlibBacktestRequest) -> bool:
        return str(request.strategy_type or "").strip().lower() == "long_short_topk" and bool(
            request.strategy_params.enable_short_selling
        )

    @staticmethod
    def _resolve_strategy_builder(request: QlibBacktestRequest):
        """
        解析策略 Builder。
        优先使用内置映射；若未命中且存在同 ID 模板，则自动回退到模板代码执行。
        """
        builder, is_fallback, normalized = StrategyFactory.resolve_builder(request.strategy_type)
        if not is_fallback:
            return builder

        if request.strategy_content and request.strategy_content.strip():
            return StrategyFactory.get_builder("CustomStrategy")

        template = get_template_by_id(request.strategy_type) or get_template_by_id(normalized)
        if template and getattr(template, "code", "").strip():
            request.strategy_content = template.code
            request.strategy_type = "CustomStrategy"
            # 官方模板模式：JSON 声明的参数以 UI/strategy_params 为准，
            # 模板代码里的硬编码值仅是缺省，不再钉死滑条（问题背景：修复前
            # CustomStrategyBuilder「代码优先」把 topk/n_drop 等 UI 值全部丢弃）。
            request.template_mode = True
            request.template_params = {p.name: p.model_dump() for p in (template.params or [])}
            task_logger.info(
                "strategy_template_matched",
                "Unknown strategy_type matched template",
                strategy_type=normalized,
                template_id=template.id,
            )
            return StrategyFactory.get_builder("CustomStrategy")

        task_logger.warning(
            "strategy_template_not_found",
            "Unknown strategy_type not found in template directory, keep TopkDropout fallback",
            strategy_type=normalized,
        )
        return builder

    def _build_strategy_from_content(self, content: str):
        """
        兼容旧单测入口：从策略代码构建策略实例。
        """
        namespace: dict[str, Any] = {}
        exec(content, namespace)

        if callable(namespace.get("get_strategy_instance")):
            return namespace["get_strategy_instance"]()

        for value in namespace.values():
            if isinstance(value, type):
                try:
                    return value()
                except Exception:
                    continue

        raise ValueError("策略代码未提供可实例化对象")

    def initialize(self, provider_uri: str | None = None, region: str | None = None):
        """初始化 Qlib 并进行数据完整性预检。

        支持按市场切换 provider_uri / region：如果传入值与当前不同，
        会重置 _initialized 并重新初始化 qlib。
        """
        # 客户端可能保留了旧的 QuantDB 缓存路径。该目录在数据尚未迁移、
        # 或仅导入独立 Qlib 数据包时会存在但并不包含完整 day 数据；此时
        # 回退到当前市场可用的 provider，而不是把无效路径交给 Qlib 并在
        # Exchange 初始化阶段才报出难以定位的错误。
        if provider_uri:
            from backend.shared.qlib_paths import (
                is_qlib_provider_ready,
                normalize_qlib_provider_uri,
                resolve_qlib_provider_uri,
            )

            # 先按市场归一到 qlib_paths 解析出的缓存目录，再做完整性判定：
            # 否则旧客户端钉死的 /app/db/qlib_data 会让回测读到一份夜间同步
            # 根本没在写的缓存（写入与读取分裂，数据修复静默失效）。
            normalized = normalize_qlib_provider_uri(
                provider_uri, _infer_market_from_uri(provider_uri, region)
            )
            if normalized != provider_uri:
                task_logger.info(
                    "provider_uri_normalized",
                    "Caller-pinned Qlib provider normalized to the resolved cache",
                    requested_uri=provider_uri,
                    normalized_uri=normalized,
                )
            provider_uri = normalized

            requested_uri = provider_uri
            if not is_qlib_provider_ready(requested_uri):
                fallback_uri = resolve_qlib_provider_uri(
                    _infer_market_from_uri(requested_uri, region)
                )
                if is_qlib_provider_ready(fallback_uri):
                    provider_uri = fallback_uri
                    task_logger.warning(
                        "invalid_provider_uri_fallback",
                        "Requested Qlib provider is incomplete; using ready provider",
                        requested_uri=requested_uri,
                        fallback_uri=fallback_uri,
                        market=_infer_market_from_uri(requested_uri, region),
                    )

        # 如果请求的 provider_uri/region 与当前不同，需要重新初始化
        if provider_uri and provider_uri != self.provider_uri:
            self.provider_uri = provider_uri
            self._initialized = False
        if region and region != self.region:
            self.region = region
            self._initialized = False

        if not self._initialized:
            try:
                from backend.shared.qlib_paths import is_qlib_provider_ready

                if not is_qlib_provider_ready(self.provider_uri):
                    required_paths = (
                        "calendars/day.txt, instruments/all.txt, and features/"
                    )
                    raise RuntimeError(
                        "Qlib day-frequency data is not ready at "
                        f"{self.provider_uri!r}; required: {required_paths}. "
                        "Rebuild the Qlib cache before running a backtest."
                    )
                qlib.init(
                    provider_uri=self.provider_uri,
                    region=self.region,
                    joblib_backend=self._joblib_backend,
                )
                from qlib.config import C

                C["joblib_backend"] = self._joblib_backend
                C["kernels"] = self._kernels
                self._initialized = True
                task_logger.info(
                    "qlib_initialized",
                    "Qlib 初始化成功",
                    provider_uri=self.provider_uri,
                    region=self.region,
                    joblib_backend=self._joblib_backend,
                    kernels=self._kernels,
                )
                self._audit_data_quality()
            except Exception as e:
                task_logger.error("qlib_init_failed", "Qlib 初始化失败", error=str(e))
                raise

    def _audit_data_quality(self):
        """轻量级数据质量审计 — 已禁用，避免 D.features() 在容器内触发 CPU 死循环"""
        pass

    def check_health(self) -> dict[str, Any]:
        """健康检查（不触发初始化，避免 qlib.init 卡死导致健康检查也卡住）"""
        try:
            return {
                "status": "healthy" if self._initialized else "degraded",
                "qlib_initialized": self._initialized,
                "version": qlib.__version__,
                "data_available": False,
                "qlib_backend": QLIB_BACKEND,
            }
        except Exception as e:
            return {
                "status": "unhealthy",
                "qlib_initialized": False,
                "version": "unknown",
                "data_available": False,
                "qlib_backend": QLIB_BACKEND,
                "error": str(e),
            }

# 全局服务实例
qlib_service: QlibBacktestService = None  # type: ignore
