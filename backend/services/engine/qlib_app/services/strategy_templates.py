"""
Strategy Templates Configuration — Dynamic File-System Loader (Single Source of Truth)

后台更新策略模板只需在 `strategy_templates/` 目录中修改/新增/删除
*.py（代码）和同名 *.json（元数据）即可，无需修改代码或重启服务。

目录搜索顺序：
  1. 环境变量 STRATEGY_TEMPLATES_DIR（绝对路径）
  2. 项目根目录 / strategy_templates

缓存：
  TTL 由 STRATEGY_TEMPLATES_CACHE_TTL（秒，默认 60）控制。
  TTL 到期后下次请求时自动重新扫描目录。
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

logger = logging.getLogger(__name__)
from backend.services.engine.qlib_app.utils.structured_logger import StructuredTaskLogger

task_logger = StructuredTaskLogger(logger, "StrategyTemplateLoader")


# ---------------------------------------------------------------------------
# Schema（与旧版兼容，字段保持不变）
# ---------------------------------------------------------------------------


class StrategyParameter(BaseModel):
    name: str
    description: str
    default: Any
    min: float | None = None
    max: float | None = None


class StrategyTemplate(BaseModel):
    id: str
    name: str
    description: str
    category: str  # basic | advanced | risk_control
    difficulty: str  # beginner | intermediate | advanced
    code: str
    params: list[StrategyParameter]
    execution_defaults: dict[str, Any] = {}
    live_defaults: dict[str, Any] = {}
    live_config_tips: list[str] = []
    markets: list[str] = []  # a_share, hong_kong, us_stock, crypto
    # 注意：markets 为空 = 历史 A 股模板，仅出现在 A 股视图（template_applies_to_market）；
    # 需要进入港股等其他视图的模板必须显式列出 markets。


# ---------------------------------------------------------------------------
# 市场匹配
# ---------------------------------------------------------------------------


def _canonical_market(market: str | None) -> str | None:
    """将市场查询参数归一化为模板标记 token。

    别名映射：CN/A/a_share → a_share；HK/hong_kong → hong_kong；
    US/us_stock → us_stock；CRYPTO/crypto → crypto。
    无法识别时返回 None（调用方视为不过滤，向后兼容全量列表）。
    """
    raw = str(market or "").strip().lower().replace("-", "_")
    if not raw:
        return None
    if raw in {"a", "cn", "a_share", "ashare"}:
        return "a_share"
    if raw in {"hk", "hong_kong"}:
        return "hong_kong"
    if raw in {"us", "us_stock"}:
        return "us_stock"
    if raw in {"crypto"}:
        return "crypto"
    return None


def template_applies_to_market(template: StrategyTemplate, market: str | None) -> bool:
    """判断模板是否属于指定市场视图（供 GET /strategies/templates?market= 过滤）。

    - market 缺省或无法识别 → 不过滤（返回 True，保持旧全量行为）；
    - 无 markets 标记的历史 A 股模板 → 仅出现在 A 股视图，避免混入港股等视图；
    - 显式标记的模板 → 仅出现在含对应标记的市场视图（大小写不敏感）。
    """
    canonical = _canonical_market(market)
    if canonical is None:
        return True
    if not template.markets:
        return canonical == "a_share"
    return canonical in {token.strip().lower() for token in template.markets}


# ---------------------------------------------------------------------------
# 目录解析
# ---------------------------------------------------------------------------


def _find_project_root() -> Path:
    """向上查找项目根目录（包含 GEMINI.md 或 requirements.txt）。"""
    curr = Path(__file__).resolve().parent
    for _ in range(10):
        if (curr / "GEMINI.md").exists() or (curr / "requirements.txt").exists():
            return curr
        if curr.parent == curr:
            break
        curr = curr.parent
    return Path(__file__).resolve().parents[6]


def _resolve_templates_dir() -> Path:
    """解析模板目录（优先环境变量）。"""
    env_dir = os.getenv("STRATEGY_TEMPLATES_DIR", "").strip()
    if env_dir:
        p = Path(env_dir)
        if p.is_dir():
            return p
        task_logger.warning("templates_dir_missing", "STRATEGY_TEMPLATES_DIR 不存在，回退到默认路径", path=env_dir)
    return _find_project_root() / "strategy_templates"


# ---------------------------------------------------------------------------
# 动态加载器
# ---------------------------------------------------------------------------

_CACHE_TTL = int(os.getenv("STRATEGY_TEMPLATES_CACHE_TTL", "60"))


class StrategyTemplateLoader:
    """
    从文件系统动态加载策略模板（带 TTL 内存缓存）。

    每个策略由两个同名文件组成：
      - <id>.json  —  元数据（name, description, category, difficulty, params, …）
      - <id>.py    —  策略代码
    """

    def __init__(self) -> None:
        self._templates_dir: Path = _resolve_templates_dir()
        self._cache: list[StrategyTemplate] | None = None
        self._cache_at: float = 0.0

        task_logger.info("initialized", "StrategyTemplateLoader 初始化", templates_dir=str(self._templates_dir), cache_ttl=_CACHE_TTL)

    def _is_cache_valid(self) -> bool:
        return self._cache is not None and (time.time() - self._cache_at) < _CACHE_TTL

    def _load_one(self, json_path: Path) -> StrategyTemplate | None:
        """从一对 .json + .py 文件加载单个模板，失败时返回 None。"""
        py_path = json_path.with_suffix(".py")
        if not py_path.exists():
            task_logger.warning("missing_strategy_code", "缺少策略代码文件，跳过", path=str(py_path))
            return None

        try:
            with open(json_path, encoding="utf-8") as f:
                meta: dict[str, Any] = json.load(f)
        except Exception as e:
            task_logger.error("read_template_meta_failed", "读取模板元数据失败", path=str(json_path), error=str(e))
            return None

        try:
            with open(py_path, encoding="utf-8") as f:
                code = f.read()
        except Exception as e:
            task_logger.error("read_strategy_code_failed", "读取策略代码失败", path=str(py_path), error=str(e))
            return None

        # 必填字段校验
        required = ("id", "name", "description", "category", "difficulty")
        missing = [k for k in required if not meta.get(k)]
        if missing:
            task_logger.warning("template_missing_required", "模板缺少必填字段，跳过", template_id=json_path.stem, missing=missing)
            return None

        try:
            raw_params = meta.get("params", [])
            params = [StrategyParameter(**p) for p in raw_params]
            return StrategyTemplate(
                id=meta["id"],
                name=meta["name"],
                description=meta["description"],
                category=meta["category"],
                difficulty=meta["difficulty"],
                code=code,
                params=params,
                execution_defaults=meta.get("execution_defaults", {}),
                live_defaults=meta.get("live_defaults", {}),
                live_config_tips=meta.get("live_config_tips", []),
                markets=meta.get("markets", []),
            )
        except Exception as e:
            task_logger.error("build_template_failed", "构建模板对象失败", template_id=json_path.stem, error=str(e))
            return None

    def load(self) -> list[StrategyTemplate]:
        """返回所有策略模板（优先使用缓存）。"""
        if self._is_cache_valid():
            return self._cache  # type: ignore[return-value]

        if not self._templates_dir.exists():
            task_logger.warning("templates_dir_not_found", "策略模板目录不存在，返回空列表", path=str(self._templates_dir))
            # 保留旧缓存、不更新时间戳，以便下次仍会重试
            return self._cache or []

        templates: list[StrategyTemplate] = []
        for json_path in sorted(self._templates_dir.glob("*.json")):
            t = self._load_one(json_path)
            if t is not None:
                templates.append(t)

        # 排序逻辑：按难度排序（简单优先），难度相同按 ID 排序
        # beginner (1) < intermediate (2) < advanced (3)
        difficulty_map = {"beginner": 1, "intermediate": 2, "advanced": 3}
        templates.sort(key=lambda x: (difficulty_map.get(x.difficulty, 9), x.id.lower()))

        self._cache = templates
        self._cache_at = time.time()

        task_logger.info("templates_loaded", "策略模板加载完成", count=len(templates), templates_dir=str(self._templates_dir))
        return templates

    def get_by_id(self, template_id: str) -> StrategyTemplate | None:
        """根据 ID 获取单个模板（不区分大小写）。"""
        lower_id = (template_id or "").lower()
        for t in self.load():
            if t.id.lower() == lower_id:
                return t
        return None

    def invalidate_cache(self) -> None:
        """手动失效缓存（用于测试或强制刷新）。"""
        self._cache = None
        self._cache_at = 0.0
        task_logger.info("cache_invalidated", "策略模板缓存已手动清除")


# ---------------------------------------------------------------------------
# 全局单例
# ---------------------------------------------------------------------------

_loader = StrategyTemplateLoader()


# ---------------------------------------------------------------------------
# 公开接口（与旧版 strategy_templates.py 完全兼容）
# ---------------------------------------------------------------------------


def get_all_templates() -> list[StrategyTemplate]:
    """返回所有可用策略模板。"""
    return _loader.load()


def get_template_by_id(template_id: str) -> StrategyTemplate | None:
    """按 ID 查询单个策略模板（不区分大小写）。"""
    return _loader.get_by_id(template_id)


def invalidate_templates_cache() -> None:
    """强制失效模板缓存（用于管理员刷新场景）。"""
    _loader.invalidate_cache()
