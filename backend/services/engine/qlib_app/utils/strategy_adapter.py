import logging
import os
from pathlib import Path
from typing import Any

from backend.services.engine.qlib_app.utils.structured_logger import (
    StructuredTaskLogger,
)

logger = logging.getLogger(__name__)
task_logger = StructuredTaskLogger(logger, "StrategyAdapter")


class StrategyAdapter:
    """
    策略配置适配器中间层
    负责在策略实例化前进行参数补全、路径解析和合规性检查
    """

    def __init__(self, project_root: Path):
        self.project_root = project_root

    def adapt(
        self, strategy_config: dict[str, Any], context: dict[str, Any] = None
    ) -> dict[str, Any]:
        """
        主适配入口
        :param strategy_config: 原始策略配置字典
        :param context: 上下文信息（如 backtest_id, universe 等）
        """
        if not isinstance(strategy_config, dict):
            return strategy_config

        # 1. 基础清理
        config = self._sanitize_config(strategy_config)

        # 2. 识别策略类并进行针对性适配
        strategy_class = config.get("class")
        kwargs = config.get("kwargs", {})

        # 3. 路径自动解析适配器 (通用)
        kwargs = self._resolve_all_paths(kwargs)

        # 4. 针对特定策略的“打补丁”逻辑
        if strategy_class in ("EnhancedIndexingStrategy", "EnhancedIndexing"):
            kwargs = self._adapt_enhanced_indexing(kwargs, context)
        elif strategy_class in (
            "TopkDropoutStrategy",
            "RedisTopkStrategy",
            "RedisRecordingStrategy",
        ):
            kwargs = self._adapt_topk_dropout(kwargs, context)
        elif strategy_class in (
            "RedisWeightStrategy",
            "SimpleWeightStrategy",
            "RedisFullAlphaStrategy",
        ):
            kwargs = self._adapt_weight_strategy(kwargs, context)

        # 5. 注入业务通用参数 (如 backtest_id 用于日志记录)
        if context and "backtest_id" in context:
            # 仅在支持记录的策略中注入 (匹配 Redis 前缀的策略类)
            if strategy_class and (
                strategy_class.startswith("Redis")
                or strategy_class == "SimpleWeightStrategy"
            ):
                kwargs["backtest_id"] = context["backtest_id"]

        config["kwargs"] = kwargs
        return config

    def _resolve_all_paths(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """遍历所有参数，自动识别并解析可能的路径"""
        path_keys = ["riskmodel_root", "model_path", "pred_path", "data_dir"]
        for key in path_keys:
            if key in kwargs and isinstance(kwargs[key], str):
                val = kwargs[key]
                # 先展开用户目录（处理 ~ 路径）
                expanded_val = os.path.expanduser(val)
                # 如果是相对路径，转换为绝对路径
                if expanded_val and not os.path.isabs(expanded_val):
                    resolved = self.project_root / expanded_val
                    kwargs[key] = str(resolved)
                    # 自动创建目录（如果是 root/dir 类型）
                    if "root" in key or "dir" in key:
                        os.makedirs(resolved, exist_ok=True)
                else:
                    # 已是绝对路径（包括已展开的 ~），直接写回
                    kwargs[key] = expanded_val
        return kwargs

    def _adapt_enhanced_indexing(
        self, kwargs: dict[str, Any], context: dict[str, Any] | None
    ) -> dict[str, Any]:
        """增强指数策略适配逻辑"""
        # 1. 处理必选的 riskmodel_root
        if "riskmodel_root" not in kwargs:
            # 默认补全一个基于 universe 的路径
            universe = context.get("universe", "csi300") if context else "csi300"
            default_path = self.project_root / f"db/risk_model/{universe}"
            kwargs["riskmodel_root"] = str(default_path)
            os.makedirs(default_path, exist_ok=True)
            task_logger.info(
                "auto_fill_riskmodel_root",
                "Auto-filled missing riskmodel_root",
                riskmodel_root=kwargs["riskmodel_root"],
            )

        # 2. 移除该策略不支持的参数 (该策略基于权重优化，不支持 topk)
        for unsupported in ["topk", "n_drop", "buffer_margin"]:
            if unsupported in kwargs:
                val = kwargs.pop(unsupported)
                task_logger.warning(
                    "remove_unsupported_param",
                    "Removed unsupported parameter for EnhancedIndexingStrategy",
                    param=unsupported,
                    value=val,
                )

        # 3. 默认补充更稳健的优化器参数（可被显式传入覆盖）
        optimizer_kwargs = kwargs.get("optimizer_kwargs")
        if optimizer_kwargs is None:
            kwargs["optimizer_kwargs"] = {
                "lamb": 0.1,
                "delta": None,
                "b_dev": None,
            }
        elif isinstance(optimizer_kwargs, dict):
            optimizer_kwargs.setdefault("lamb", 0.1)
            optimizer_kwargs.setdefault("delta", None)
            optimizer_kwargs.setdefault("b_dev", None)
            kwargs["optimizer_kwargs"] = optimizer_kwargs

        return kwargs

    def _adapt_topk_dropout(
        self, kwargs: dict[str, Any], context: dict[str, Any] | None
    ) -> dict[str, Any]:
        """TopkDropout 策略适配逻辑"""
        # 确保关键参数存在
        if "topk" not in kwargs:
            kwargs["topk"] = 50
        if "n_drop" not in kwargs:
            topk = kwargs.get("topk", 50)
            try:
                topk_num = int(topk)
            except (TypeError, ValueError):
                topk_num = 50
            kwargs["n_drop"] = max(1, int(round(topk_num * 0.2)))
        return kwargs

    def _adapt_weight_strategy(
        self, kwargs: dict[str, Any], context: dict[str, Any] | None
    ) -> dict[str, Any]:
        """WeightStrategy 策略适配逻辑"""
        # 1. 移除不支持的参数 (WeightStrategy 不使用 n_drop)
        for unsupported in ["n_drop", "buffer_margin"]:
            if unsupported in kwargs:
                val = kwargs.pop(unsupported)
                task_logger.warning(
                    "remove_unsupported_param",
                    "移除 WeightStrategy 不支持的参数",
                    param=unsupported,
                    value=val,
                )

        # 2. 补充默认值
        kwargs.setdefault("topk", 50)  # 选股数量
        kwargs.setdefault("min_score", 0.0)  # 最小分数阈值
        kwargs.setdefault("max_weight", 1.0)  # 单标的最大权重

        task_logger.info(
            "weight_strategy_params",
            "WeightStrategy 参数",
            topk=kwargs.get("topk"),
            min_score=kwargs.get("min_score"),
            max_weight=kwargs.get("max_weight"),
        )

        return kwargs

    def _sanitize_config(self, config: dict[str, Any]) -> dict[str, Any]:
        """清理配置中的无效字段"""
        # 不将 None 替换为 ""，空 module_path 应直接移除，避免 qlib importlib.import_module("") 崩溃
        if "module_path" in config and not config["module_path"]:
            del config["module_path"]
        return config
