"""将 strategy_templates 目录中的内置策略同步到用户 PG 策略表。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


def template_marker(item: dict[str, Any]) -> str | None:
    """系统副本的模板身份：优先 ``template:<id>`` 标签，退回 parameters.strategy_type。"""
    for tag in item.get("tags") or []:
        text = str(tag)
        if text.startswith("template:"):
            marker = text.split(":", 1)[1].strip()
            if marker:
                return marker
    params = item.get("parameters") if isinstance(item.get("parameters"), dict) else {}
    marker = str(params.get("strategy_type") or "").strip()
    return marker or None


def existing_template_markers(
    items: list[dict[str, Any]],
) -> tuple[set[str], set[str]]:
    """已落库策略里的模板身份：parameters.strategy_type、tags 里的 template:<id>、同名。"""
    types: set[str] = set()
    names: set[str] = set()
    for item in items:
        marker = template_marker(item)
        if marker:
            types.add(marker)
        for tag in item.get("tags") or []:
            text = str(tag)
            if text.startswith("template:"):
                marker = text.split(":", 1)[1].strip()
                if marker:
                    types.add(marker)
        name = str(item.get("name") or "").strip()
        if name:
            names.add(name)
    return types, names


def _load_templates():
    from backend.services.engine.qlib_app.services.strategy_templates import (
        get_all_templates,
    )

    return get_all_templates()


def _storage_service():
    from backend.shared.strategy_storage import get_strategy_storage_service

    return get_strategy_storage_service()


def _template_metadata(t) -> dict[str, Any]:
    return {
        "description": t.description,
        "tags": [t.category, t.difficulty, "SystemSync", f"template:{t.id}"],
        "status": "ACTIVE",
        "is_verified": True,
        "parameters": {
            "strategy_type": t.id,
            "topk": 50,
            "signal": "<PRED>",
            "sort": int(getattr(t, "sort", 100) or 100),
        },
    }


async def _has_references(strategy_id: str) -> bool:
    """被模型绑定/策略循环任务引用时不删除；查询失败按已引用处理。"""
    try:
        from sqlalchemy import text

        from backend.shared.database_manager_v2 import get_session

        async with get_session() as session:
            for table, predicate in (
                ("qm_strategy_model_bindings", "strategy_id::text = :sid"),
                ("strategy_loop_tasks", "request_json->>'strategy_id' = :sid"),
            ):
                exists = (
                    await session.execute(
                        text(f"SELECT to_regclass('public.{table}') IS NOT NULL")
                    )
                ).scalar()
                if not exists:
                    continue
                hit = (
                    await session.execute(
                        text(f"SELECT EXISTS (SELECT 1 FROM {table} WHERE {predicate})"),
                        {"sid": strategy_id},
                    )
                ).scalar()
                if hit:
                    return True
    except Exception:
        return True
    return False


async def sync_builtin_templates(user_id: str) -> int:
    """把内置模板同步到用户策略库：缺失则新增，系统副本则更新，下线则清理。

    - 已存在的 ``SystemSync`` + ``template:<id>`` 副本：用文件内容覆盖
      code/description/parameters（文件模板更新后能真正生效）。
    - 用户自建或改过标签的同类型/同名策略：不覆盖。
    - 文件已删除但仍留在库里的系统副本：未被模型绑定/循环任务引用时删除。
    """
    svc = _storage_service()
    templates = _load_templates()
    existing = await asyncio.to_thread(svc.list, user_id=user_id)
    template_ids = {t.id for t in templates}

    by_marker: dict[str, dict[str, Any]] = {}
    for item in existing:
        marker = template_marker(item)
        if marker and marker not in by_marker:
            by_marker[marker] = item
    existing_names = {str(item.get("name") or "").strip() for item in existing}

    changed = 0
    for t in templates:
        metadata = _template_metadata(t)
        current = by_marker.get(t.id)
        if current is not None:
            if "SystemSync" not in (current.get("tags") or []):
                continue  # 用户自建的同类型策略，保持不动
            await svc.save(
                user_id=user_id,
                name=t.name,
                code=t.code,
                metadata=metadata,
                strategy_id=str(current.get("id")),
            )
            changed += 1
            continue

        if t.name in existing_names:
            continue
        await svc.save(user_id=user_id, name=t.name, code=t.code, metadata=metadata)
        changed += 1
        existing_names.add(t.name)

    for item in existing:
        if "SystemSync" not in (item.get("tags") or []):
            continue
        marker = template_marker(item)
        if marker and marker in template_ids:
            continue
        sid = str(item.get("id") or "")
        if not sid.isdigit():
            continue
        if await _has_references(sid):
            continue
        try:
            await svc.delete(sid, user_id)
            changed += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to remove stale template copy %s: %s", sid, exc)

    if changed:
        logger.info(
            "synced %s builtin strategy templates for user %s", changed, user_id
        )
    return changed
