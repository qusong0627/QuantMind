"""
用户策略加载服务（PG + COS 统一存储版）

主路径：通过 backend.shared.strategy_storage.StrategyStorageService 读写 PG + COS。
兜底路径：STORAGE_MODE=local 或 DB 不可用时保留文件系统扫描。
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

try:
    from backend.shared.database_pool import get_db, init_default_databases
except ImportError:
    try:
        from shared.database_pool import get_db, init_default_databases  # type: ignore
    except ImportError:
        get_db = None  # type: ignore
        init_default_databases = None  # type: ignore

try:
    from backend.shared.strategy_storage import get_strategy_storage_service

    _has_strategy_storage = True
except ImportError:
    try:
        from shared.strategy_storage import get_strategy_storage_service  # type: ignore

        _has_strategy_storage = True
    except ImportError:
        _has_strategy_storage = False

import asyncio
from backend.services.engine.qlib_app.utils.structured_logger import StructuredTaskLogger

logger = logging.getLogger(__name__)
task_logger = StructuredTaskLogger(logger, "UserStrategyLoader")


# 文件系统兜底配置（仅 STORAGE_MODE=local 或 DB 不可用时使用）


def _find_project_root() -> Path:
    curr = Path(__file__).resolve().parent
    for _ in range(10):
        if (curr / "GEMINI.md").exists() or (curr / "requirements.txt").exists():
            return curr
        if curr.parent == curr:
            break
        curr = curr.parent
    return Path(__file__).resolve().parents[4]


PROJECT_ROOT = _find_project_root()
USER_STRATEGIES_DIR = PROJECT_ROOT / "user_strategies"

task_logger.info(
    "initialized",
    "UserStrategyLoader initialized",
    project_root=str(PROJECT_ROOT),
    strategies_dir=str(USER_STRATEGIES_DIR),
    has_strategy_storage=_has_strategy_storage,
)

_STORAGE_MODE = (os.getenv("STORAGE_MODE") or "cos").strip().lower()
_USE_CLOUD = _has_strategy_storage and _STORAGE_MODE != "local"


# ---------------------------------------------------------------------------
# 文件系统工具（兜底）
# ---------------------------------------------------------------------------


def _load_metadata_from_fs(strategy_path: Path) -> dict | None:
    """从文件系统加载策略元数据（兼容旧格式）。"""
    json_path = strategy_path.with_suffix(".json")
    if json_path.exists():
        try:
            with open(json_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            task_logger.error("load_metadata_failed", "加载元数据失败", path=str(json_path), error=str(e))
    return {
        "id": strategy_path.stem,
        "name": strategy_path.stem.replace("_", " ").title(),
        "description": "无描述",
        "file_path": str(strategy_path),
        "category": "未分类",
        "author": "未知",
    }


def _validate_code(code: str) -> None:
    """AST 安全检查，防止保存危险代码（T-P0-02：统一走共享闸门，白名单口径）。"""
    from backend.shared.strategy_code_gate import validate_strategy_code

    validate_strategy_code(code)


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------


class UserStrategyLoader:
    """用户策略加载器（PG+COS 优先，文件系统兜底）。"""

    def __init__(self) -> None:
        self.strategies_dir = USER_STRATEGIES_DIR
        if not _USE_CLOUD:
            self._ensure_fs_directories()

    def _ensure_fs_directories(self) -> None:
        for d in ["ai_generated", "manual_created", "templates", "archived"]:
            (self.strategies_dir / d).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 公开接口（与旧版兼容）
    # ------------------------------------------------------------------

    def load_strategies(
        self,
        category: str | None = None,
        search: str | None = None,
        tags: list[str] | None = None,
        user_id: str | None = None,
    ) -> list[dict]:
        """
        加载策略列表。

        优先从 PG（通过 StrategyStorageService）加载；
        STORAGE_MODE=local 或 DB 不可用时降级到文件系统扫描。
        """
        # 1. 主路径：PG + COS
        if _USE_CLOUD and user_id:
            try:
                svc = get_strategy_storage_service()
                return svc.list(
                    user_id=user_id,
                    category=category,
                    search=search,
                    tags=tags,
                )
            except Exception as e:
                task_logger.warning("load_strategies_pg_failed", "PG 加载策略失败，降级到文件系统", error=str(e))

        # 2. 兜底：文件系统扫描
        return self._load_from_fs(category=category, search=search, tags=tags, user_id=user_id)

    def get_strategy(self, strategy_id: str, category: str | None = None) -> dict | None:
        """
        获取单个策略详情（含代码）。

        数字 ID → PG + COS（含 resolve_code）；
        字符串 ID → 文件系统扫描（兼容旧格式）。
        """
        # 1. 数字 ID → PG
        if _USE_CLOUD and strategy_id.isdigit():
            try:
                svc = get_strategy_storage_service()
                # 使用 asyncio.run 或 event loop
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        task_logger.warning(
                            "event_loop_running",
                            "事件循环运行中，跳过 PG 同步获取策略并回退到文件系统",
                            strategy_id=strategy_id,
                        )
                        return self._get_from_fs(strategy_id=strategy_id, category=category)
                    return loop.run_until_complete(svc.get(int(strategy_id), resolve_code=True))
                except RuntimeError:
                    return asyncio.run(svc.get(int(strategy_id), resolve_code=True))
            except Exception as e:
                task_logger.warning("get_strategy_pg_failed", "PG 获取策略失败，回退到文件系统", strategy_id=strategy_id, error=str(e))

        # 2. 文件系统兜底（字符串 ID 或 PG 失败）
        return self._get_from_fs(strategy_id=strategy_id, category=category)

    def save_strategy(
        self,
        code: str,
        metadata: dict,
        category: str = "manual_created",
        strategy_id: str | None = None,
    ) -> str:
        """
        保存策略：安全校验 → PG + COS。

        Returns:
            新策略的 ID（字符串）
        """
        _validate_code(code)

        user_id = str(metadata.get("user_id") or metadata.get("author") or "default")

        if _USE_CLOUD:
            try:
                svc = get_strategy_storage_service()
                # 自动打标签
                tags = list(set(metadata.get("tags", [])))
                if category == "ai_generated":
                    tags += ["AI", "Wizard"]
                    if metadata.get("model_id"):
                        tags.append(str(metadata["model_id"]))
                    if metadata.get("style_preference"):
                        tags.append(str(metadata["style_preference"]))
                metadata["tags"] = list(set(tags))

                result = asyncio.run(
                    svc.save(
                        user_id=user_id,
                        name=metadata.get("name") or f"策略_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
                        code=code,
                        metadata=metadata,
                        strategy_id=strategy_id,
                    )
                )
                task_logger.info("save_strategy_pg_success", "策略已保存到 PG+COS", strategy_id=str(result["id"]))
                return result["id"]
            except Exception as e:
                task_logger.error("save_strategy_pg_failed", "PG+COS 保存失败，降级到文件系统", error=str(e))

        # 文件系统兜底保存
        return self._save_to_fs(code=code, metadata=metadata, category=category, strategy_id=strategy_id)

    def delete_strategy(
        self, strategy_id: str, category: str, user_id: str | None = None
    ) -> bool:
        """删除策略（PG 主路径 + 文件系统兜底）。

        T-P3-02 修复初版即存的三个缺陷：① 未 await（协程对象被当"成功"返回、
        策略实际从未删除）；② user_id 硬编码 "0"（get 预检查必然查空）；
        ③ 删除结果不反映实际行数。user_id 为 None 时拒绝走 PG（不给静默假成功）。
        """
        if _USE_CLOUD and strategy_id.isdigit():
            if not user_id:
                task_logger.warning(
                    "delete_strategy_missing_user",
                    "缺少 user_id，拒绝 PG 删除（请从请求上下文传入）",
                    strategy_id=strategy_id,
                )
                return False
            try:
                svc = get_strategy_storage_service()
                return asyncio.run(
                    svc.delete(strategy_id=int(strategy_id), user_id=str(user_id))
                )
            except Exception as e:
                task_logger.warning("delete_strategy_pg_failed", "PG 删除策略失败", strategy_id=strategy_id, error=str(e))

        # 文件系统兜底
        return self._delete_from_fs(strategy_id=strategy_id, category=category)

    # ------------------------------------------------------------------
    # 文件系统兜底实现
    # ------------------------------------------------------------------

    def _load_from_fs(
        self,
        category: str | None = None,
        search: str | None = None,
        tags: list[str] | None = None,
        user_id: str | None = None,
    ) -> list[dict]:
        strategies = []
        search_dirs: list[Path] = []

        if category and category not in ("db_stored",):
            search_dirs = [self.strategies_dir / category]
        else:
            search_dirs = [
                self.strategies_dir / "ai_generated",
                self.strategies_dir / "manual_created",
                self.strategies_dir / "templates",
            ]

        for d in search_dirs:
            if not d.exists():
                continue
            for py_file in d.glob("*.py"):
                if py_file.stem.startswith("_"):
                    continue
                try:
                    meta = _load_metadata_from_fs(py_file) or {}
                    meta["category_dir"] = d.name
                    meta["file_path"] = str(py_file)
                    meta["source"] = "file"
                    meta.setdefault("cos_url", None)

                    if search:
                        sl = search.lower()
                        if sl not in meta.get("name", "").lower() and sl not in meta.get("description", "").lower():
                            continue
                    if tags:
                        if not set(meta.get("tags", [])).intersection(set(tags)):
                            continue
                    strategies.append(meta)
                except Exception as e:
                    task_logger.error("load_strategy_fs_failed", "加载文件系统策略失败", path=str(py_file), error=str(e))

        return strategies

    def _get_from_fs(self, strategy_id: str, category: str | None = None) -> dict | None:
        search_dirs: list[Path] = []
        if category:
            search_dirs = [self.strategies_dir / category]
        else:
            search_dirs = [
                self.strategies_dir / "ai_generated",
                self.strategies_dir / "manual_created",
                self.strategies_dir / "templates",
            ]

        for d in search_dirs:
            py_path = d / f"{strategy_id}.py"
            if py_path.exists():
                meta = _load_metadata_from_fs(py_path) or {}
                with open(py_path, encoding="utf-8") as f:
                    meta["code"] = f.read()
                meta["file_path"] = str(py_path)
                meta["source"] = "file"
                meta["category_dir"] = d.name
                meta.setdefault("cos_url", None)
                meta.setdefault("author", "用户")
                meta.setdefault("risk_level", "medium")
                meta.setdefault("version", "1.0.0")
                meta.setdefault("tags", [])
                meta.setdefault("parameters", {})
                return meta

        return None

    def _save_to_fs(
        self,
        code: str,
        metadata: dict,
        category: str = "manual_created",
        strategy_id: str | None = None,
    ) -> str:
        import shutil
        from uuid import uuid4

        if not strategy_id:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            prefix = "ai_" if category == "ai_generated" else "strategy_"
            style_suffix = f"_{metadata.get('style_preference')}" if metadata.get("style_preference") else ""
            strategy_id = f"{prefix}{ts}{style_suffix}_{uuid4().hex[:4]}"

        metadata["id"] = strategy_id
        metadata.setdefault("created_at", datetime.now().isoformat())
        metadata["updated_at"] = datetime.now().isoformat()

        save_dir = self.strategies_dir / category
        save_dir.mkdir(parents=True, exist_ok=True)

        py_path = save_dir / f"{strategy_id}.py"
        if py_path.exists():
            bak_ts = datetime.fromtimestamp(py_path.stat().st_mtime).strftime("%Y%m%d_%H%M%S")
            versions_dir = save_dir / "versions"
            versions_dir.mkdir(exist_ok=True)
            shutil.copy2(str(py_path), str(versions_dir / f"{strategy_id}_{bak_ts}.py.bak"))

        with open(py_path, "w", encoding="utf-8") as f:
            f.write(code)

        json_path = save_dir / f"{strategy_id}.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)

        task_logger.info("save_strategy_fs_success", "策略已保存到文件系统", path=str(py_path))
        return strategy_id

    def _delete_from_fs(self, strategy_id: str, category: str) -> bool:
        import shutil

        source_dir = self.strategies_dir / category
        archive_dir = self.strategies_dir / "archived"
        archive_dir.mkdir(parents=True, exist_ok=True)

        py_path = source_dir / f"{strategy_id}.py"
        json_path = source_dir / f"{strategy_id}.json"

        if not py_path.exists():
            return False

        shutil.move(str(py_path), str(archive_dir / py_path.name))
        if json_path.exists():
            shutil.move(str(json_path), str(archive_dir / json_path.name))

        task_logger.info("archive_strategy_fs_success", "策略已归档（文件系统）", strategy_id=strategy_id)
        return True


# 全局单例
user_strategy_loader = UserStrategyLoader()
