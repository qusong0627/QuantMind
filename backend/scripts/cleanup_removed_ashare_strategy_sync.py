#!/usr/bin/env python3
"""清理已下线的 50 个第三方 A 股策略在用户策略库里的 SystemSync 副本。

背景：`strategy_templates/as01_*.py|.json` ~ `as50_*.py|.json`（来源为第三方
TradingView 策略）已从仓库删除。存量库中由
`backend/shared/strategy_template_sync.py::sync_builtin_templates` 写入个人
策略库的副本仍会出现在「策略中心」，本脚本负责清理。

默认只删除同时满足以下条件的行：
  1. tags 含 `SystemSync`；
  2. parameters.strategy_type 命中 as01 ~ as50；
  3. is_public = FALSE；
  4. 未被 qm_strategy_model_bindings / strategy_loop_tasks 引用。

用法（容器内；默认 dry-run，只统计不写库）：
    docker exec quantmind python backend/scripts/cleanup_removed_ashare_strategy_sync.py
    docker exec quantmind python backend/scripts/cleanup_removed_ashare_strategy_sync.py --apply
    docker exec quantmind python backend/scripts/cleanup_removed_ashare_strategy_sync.py --apply --user-id 10000001
    docker exec quantmind python backend/scripts/cleanup_removed_ashare_strategy_sync.py --apply --include-referenced

--apply 前会把命中行（含 code，便于回灌）导出到 /data/backups；
可用 --backup-dir 覆盖。--include-referenced 仅在确认绑定/循环任务可一并废弃时使用。

幂等：重复执行匹配 0 行。升级链路另有 data/upgrade_v1.0.18.sql 自动清理，
本脚本用于升级前预检、单用户定向清理或自动补丁跳过后的兜底。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("cleanup_removed_ashare_strategy_sync")

# as01_... ~ as50_...（与 data/upgrade_v1.0.18.sql 保持一致）
REMOVED_TYPE_REGEX = r"^as(0[1-9]|[1-4][0-9]|50)_"

# 基础条件：系统同步副本 + as01~as50 + 未公开
BASE_PREDICATE = (
    "'SystemSync' = ANY(s.tags)"
    " AND COALESCE(s.parameters->>'strategy_type', '') ~ :as_re"
    " AND COALESCE(s.is_public, FALSE) = FALSE"
)


def _resolve_backup_dir(raw: str | None) -> Path:
    if raw:
        return Path(raw)
    for candidate in (Path("/data/backups"), PROJECT_ROOT / "data" / "backups"):
        if candidate.parent.exists():
            return candidate
    return PROJECT_ROOT / "data" / "backups"


async def _reference_guards(session) -> list[str]:
    """只对库里真实存在的引用表追加排除条件。"""
    from sqlalchemy import text

    guards: list[str] = []
    has_bind = (
        await session.execute(
            text("SELECT to_regclass('public.qm_strategy_model_bindings') IS NOT NULL")
        )
    ).scalar()
    if has_bind:
        guards.append(
            "NOT EXISTS (SELECT 1 FROM qm_strategy_model_bindings b "
            "WHERE b.strategy_id::text = s.id::text)"
        )
    has_loop = (
        await session.execute(
            text("SELECT to_regclass('public.strategy_loop_tasks') IS NOT NULL")
        )
    ).scalar()
    if has_loop:
        guards.append(
            "NOT EXISTS (SELECT 1 FROM strategy_loop_tasks t "
            "WHERE t.request_json->>'strategy_id' = s.id::text)"
        )
    return guards


async def cleanup(
    *,
    apply: bool,
    user_id: str | None,
    include_referenced: bool,
    backup_dir: Path,
) -> dict[str, Any]:
    from sqlalchemy import bindparam, text

    from backend.shared.database_manager_v2 import get_session

    params: dict[str, Any] = {"as_re": REMOVED_TYPE_REGEX}
    guards: list[str] = []

    async with get_session() as session:
        if not include_referenced:
            guards = await _reference_guards(session)

        where = BASE_PREDICATE
        if user_id:
            from backend.shared.strategy_storage import _ensure_int_user_id

            params["uid"] = _ensure_int_user_id(user_id)
            where += " AND s.user_id = :uid"
        for guard in guards:
            where += f" AND {guard}"

        rows = (
            (
                await session.execute(
                    text(
                        "SELECT s.id, s.user_id, s.name, s.code, s.parameters, "
                        "s.tags, s.is_public, s.backtest_count, "
                        "s.created_at, s.updated_at "
                        f"FROM strategies s WHERE {where} ORDER BY s.user_id, s.id"
                    ),
                    params,
                )
            )
            .mappings()
            .all()
        )
        matches = [dict(r) for r in rows]

        summary: dict[str, Any] = {
            "matched": len(matches),
            "by_user": {},
            "deleted": 0,
            "backup_file": None,
            "applied": apply,
            "include_referenced": include_referenced,
        }
        for item in matches:
            key = str(item["user_id"])
            summary["by_user"][key] = summary["by_user"].get(key, 0) + 1
            log.info(
                "[MATCH] user=%s id=%s type=%s name=%s",
                item["user_id"],
                item["id"],
                (item.get("parameters") or {}).get("strategy_type"),
                item["name"],
            )

        if not matches:
            return summary
        if not apply:
            log.info("[DRY-RUN] 命中 %s 条，未写库；加 --apply 执行删除", len(matches))
            return summary

        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_file = backup_dir / f"removed_ashare_sync_{stamp}.jsonl"
        with backup_file.open("w", encoding="utf-8") as fh:
            for item in matches:
                fh.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
        summary["backup_file"] = str(backup_file)
        log.info("[BACKUP] %s (%s 行)", backup_file, len(matches))

        ids = [int(item["id"]) for item in matches]
        result = await session.execute(
            text(
                "DELETE FROM strategies s "
                f"WHERE {where} AND s.id IN :ids"
            ).bindparams(bindparam("ids", expanding=True)),
            {**params, "ids": ids},
        )
        summary["deleted"] = result.rowcount or 0
        await session.commit()
        log.info("[DELETE] %s 行", summary["deleted"])
        return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="清理已下线 A 股策略（as01~as50）的 SystemSync 副本"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="实际删除（默认 dry-run，只统计不写库）",
    )
    parser.add_argument("--user-id", help="只清理指定用户（业务 user_id 或数字主键）")
    parser.add_argument(
        "--include-referenced",
        action="store_true",
        help="连同被模型绑定/策略循环任务引用的行一起删（默认保留）",
    )
    parser.add_argument(
        "--backup-dir",
        help="删除前备份 JSONL 的目录（默认 /data/backups）",
    )
    args = parser.parse_args()

    summary = asyncio.run(
        cleanup(
            apply=args.apply,
            user_id=args.user_id,
            include_referenced=args.include_referenced,
            backup_dir=_resolve_backup_dir(args.backup_dir),
        )
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
