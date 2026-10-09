"""多市场默认模型审计（§5.5）：dry-run → 确认 → apply → 写后回读。

存量修复工具四纪律：查消费方读的那份（活库 pg_indexes + qm_user_models 行）、
写后回读（apply 后重查索引与默认行）、验收逐个实跑（dry-run 与 apply 都是真实
查询，不是纸上推演）。

只做**结构迁移**（data/upgrade_v1.1.3.sql：DROP 旧全局唯一索引 + 建按市场
部分唯一索引），不动任何行数据——迁移不给 HK/US 凭空造默认，各市场默认由
rollout 晋升或用户手动设置产生。

用法::

  python backend/scripts/audit_multimarket_defaults.py            # dry-run（默认）
  python backend/scripts/audit_multimarket_defaults.py --apply    # 应用 + 回读

退出码：0 = 成功（dry-run 含「可安全应用」判定）；2 = 拒绝/失败
（存在同市场多默认行——建索引会失败，需先人工裁决）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import text  # noqa: E402

from backend.shared.database_manager_v2 import get_session  # noqa: E402

OLD_INDEX = "uq_qm_user_models_default_per_user"
NEW_INDEX = "uq_qm_user_models_default_per_market"
UPGRADE_FILE = "upgrade_v1.1.3.sql"


def _resolve_sql_file() -> Path:
    """与 main_oss._upgrade_sql_files 同序：容器 /data 优先，源码仓库 <root>/data 兜底。"""
    candidates = [
        os.getenv("QM_UPGRADE_SQL_DIR", ""),
        os.getenv("QM_DATA_DIR", ""),
        "/data",
        str(PROJECT_ROOT / "data"),
    ]
    for directory in candidates:
        if not directory:
            continue
        candidate = Path(directory) / UPGRADE_FILE
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"未找到迁移脚本 {UPGRADE_FILE}（查过 {candidates}）")


def _split_statements(sql_text: str) -> list[str]:
    """去掉整行注释后按 `;` 切分（迁移文件是我们自己写的，形态受控）。"""
    lines = [
        line for line in sql_text.splitlines() if not line.strip().startswith("--")
    ]
    return [stmt.strip() for stmt in "\n".join(lines).split(";") if stmt.strip()]


async def _index_state() -> dict[str, Any]:
    async with get_session(read_only=True) as session:
        rows = (
            (
                await session.execute(
                    text(
                        """
                        SELECT indexname FROM pg_indexes
                        WHERE tablename = 'qm_user_models'
                        ORDER BY indexname
                        """
                    )
                )
            )
            .scalars()
            .all()
        )
    return {
        "indexes": [str(r) for r in rows],
        "old_index_present": OLD_INDEX in rows,
        "new_index_present": NEW_INDEX in rows,
    }


async def _defaults_state() -> dict[str, Any]:
    async with get_session(read_only=True) as session:
        defaults = (
            (
                await session.execute(
                    text(
                        """
                        SELECT tenant_id, user_id, model_id, status,
                               qm_market_of(metadata_json) AS market,
                               activated_at::text AS activated_at,
                               updated_at::text  AS updated_at
                        FROM qm_user_models
                        WHERE is_default = TRUE
                        ORDER BY tenant_id, user_id, market, model_id
                        """
                    )
                )
            )
            .mappings()
            .all()
        )
        # 「每市场 updated_at 最新 ready 模型」——迁移后该市场默认的候选保留集
        candidates = (
            (
                await session.execute(
                    text(
                        """
                        SELECT tenant_id, user_id, market, model_id, status, updated_at::text AS updated_at
                        FROM (
                            SELECT DISTINCT ON (tenant_id, user_id, market)
                                   tenant_id, user_id,
                                   qm_market_of(metadata_json) AS market,
                                   model_id, status, updated_at
                            FROM qm_user_models
                            WHERE status IN ('ready', 'active')
                            ORDER BY tenant_id, user_id, market, updated_at DESC
                        ) sub
                        ORDER BY tenant_id, user_id, market
                        """
                    )
                )
            )
            .mappings()
            .all()
        )

    groups: dict[tuple[str, str, str], list[str]] = {}
    for row in defaults:
        key = (str(row["tenant_id"]), str(row["user_id"]), str(row["market"]))
        groups.setdefault(key, []).append(str(row["model_id"]))
    dup_groups = {
        "/".join(key): ids for key, ids in groups.items() if len(ids) > 1
    }

    return {
        "current_defaults": [dict(r) for r in defaults],
        "per_market_latest_ready": [dict(r) for r in candidates],
        "duplicate_default_groups": dup_groups,
        "safe_to_apply": not dup_groups,
    }


async def _run_sql_file(path: Path) -> list[str]:
    statements = _split_statements(path.read_text(encoding="utf-8"))
    executed: list[str] = []
    async with get_session() as session:
        for stmt in statements:
            await session.execute(text(stmt))
            executed.append(stmt.split("\n", 1)[0][:120])
    return executed


async def _audit(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    report: dict[str, Any] = {"mode": "apply" if args.apply else "dry-run"}

    if args.apply:
        report["before"] = {
            "index": await _index_state(),
            "defaults": await _defaults_state(),
        }
        if not report["before"]["defaults"]["safe_to_apply"]:
            report["error"] = (
                "存在同市场多默认行，建唯一索引会失败——先人工裁决"
                " duplicate_default_groups 再重跑"
            )
            return report, 2
        sql_path = Path(args.sql_file) if args.sql_file else _resolve_sql_file()
        report["sql_file"] = str(sql_path)
        try:
            report["executed"] = await _run_sql_file(sql_path)
        except Exception as exc:  # noqa: BLE001 - 失败必须原样抬出
            report["error"] = f"执行迁移失败: {exc}"
            return report, 2
        # 写后回读：索引换没换、行数据有没有被动过
        after_index = await _index_state()
        after_defaults = await _defaults_state()
        report["after"] = {"index": after_index, "defaults": after_defaults}
        report["verified"] = (
            after_index["new_index_present"]
            and not after_index["old_index_present"]
            and after_defaults["current_defaults"]
            == report["before"]["defaults"]["current_defaults"]
        )
        report["ok"] = bool(report["verified"])
        return report, 0 if report["ok"] else 2

    report["index"] = await _index_state()
    report["defaults"] = await _defaults_state()
    report["already_migrated"] = (
        report["index"]["new_index_present"] and not report["index"]["old_index_present"]
    )
    report["ok"] = True
    return report, 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="应用迁移并写后回读")
    parser.add_argument("--sql-file", default=None, help="显式指定迁移 SQL 路径")
    args = parser.parse_args()

    report, code = asyncio.run(_audit(args))
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return code


if __name__ == "__main__":
    sys.exit(main())
