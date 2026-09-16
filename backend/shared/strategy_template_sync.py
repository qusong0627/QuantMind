"""将 strategy_templates 目录中的内置策略同步到用户 PG 策略表。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


def existing_template_markers(
    items: list[dict[str, Any]],
) -> tuple[set[str], set[str]]:
    """已落库策略里的模板身份：parameters.strategy_type、tags 里的 template:<id>、同名。"""
    types: set[str] = set()
    names: set[str] = set()
    for item in items:
        params = item.get("parameters") if isinstance(item.get("parameters"), dict) else {}
        strategy_type = str(params.get("strategy_type") or "").strip()
        if strategy_type:
            types.add(strategy_type)
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


def _market_for_template(t) -> str | None:
    """模板 markets 标记 → 策略 market 参数（空=历史 A 股，不写，保持 NULL 兼容）。"""
    ms = set(getattr(t, "markets", None) or [])
    if "hong_kong" in ms:
        return "HK"
    if "a_share" in ms:
        # 显式标 A：避免 NULL 与 A 股视图歧义（CN 视图把 NULL 视作 A 股，HK 视图排除）
        return "A"
    if "us_stock" in ms:
        return "US"
    if "crypto" in ms:
        return "CRYPTO"
    if "futures" in ms:
        return "FUTURES"
    return None


def _market_for_strategy_id(strategy_id: str) -> str | None:
    """按模板 ID 前缀兜底补标（存量数据修复）：hk_→HK、us_→US。"""
    sid = str(strategy_id or "").lower()
    if sid.startswith("hk_"):
        return "HK"
    if sid.startswith("us_"):
        return "US"
    return None


async def sync_builtin_templates(user_id: str) -> int:
    """把缺失的内置模板克隆到用户策略库。去重键为 strategy_type / template:<id> / 同名。

    含存量补标（幂等）：历史同步的 hk_/us_ 模板可能缺 market 标记，导致各市场
    视图混用——market 非空即跳过。
    """
    svc = _storage_service()
    templates = _load_templates()
    by_id = {t.id: t for t in templates}
    existing = await asyncio.to_thread(svc.list, user_id=user_id)

    # 0. 存量补标：历史同步的模板缺 market（重构期丢失标记）逐条补齐
    for s in existing:
        params = s.get("parameters") or {}
        if params.get("market"):
            continue
        mkt = _market_for_strategy_id(params.get("strategy_type"))
        if not mkt:
            tpl = by_id.get(str(params.get("strategy_type") or ""))
            if tpl is not None:
                mkt = _market_for_template(tpl)
        if mkt:
            merged = {**params, "market": mkt}
            await svc.save(
                user_id=user_id,
                strategy_id=s["id"],
                name=s.get("name") or "",
                code=s.get("code") or "",
                metadata={
                    "description": s.get("description") or "",
                    "tags": s.get("tags") or [],
                    "status": "ACTIVE",
                    "is_verified": s.get("is_verified", True),
                    "parameters": merged,
                },
            )

    existing_types, existing_names = existing_template_markers(existing)
    synced_count = 0
    for t in templates:
        if t.id in existing_types or t.name in existing_names:
            continue

        params: dict[str, Any] = {
            "strategy_type": t.id,
            "topk": 50,
            "signal": "<PRED>",
            "sort": int(getattr(t, "sort", 100) or 100),
        }
        mkt = _market_for_template(t)
        if mkt:
            params["market"] = mkt  # HK/US/CRYPTO 模板打市场标，供策略库按市场隔离
        if getattr(t, "dir", None):
            # AI-IDE 工作空间文件夹（策略在 IDE 文件树中的归属，不新建表字段）
            params["ide_dir"] = t.dir

        await svc.save(
            user_id=user_id,
            name=t.name,
            code=t.code,
            metadata={
                "description": t.description,
                "tags": [t.category, t.difficulty, "SystemSync", f"template:{t.id}"],
                "status": "ACTIVE",
                "is_verified": True,
                "parameters": params,
            },
        )
        synced_count += 1
        existing_types.add(t.id)
        existing_names.add(t.name)
    if synced_count:
        logger.info(
            "synced %s builtin strategy templates for user %s", synced_count, user_id
        )
    return synced_count
