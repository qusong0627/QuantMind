#!/usr/bin/env python3
"""清单库因子目录引导：为不参与自动发现的库补状态/字段/目录版本。

背景（库=包、读时拼接）：跨库组合训练需要参与库各自有**已发布**的目录版本，
训练载荷才能 pin ``factor_catalog_versions``。``factor_defs``/``alpha_library``
这类清单库被 ``EXCLUDED_FROM_DISCOVERY`` 挡在「刷新字段」之外，后台点不出来，
只能用本脚本按同一套 SQL 口径离线引导。

做三件事（每库幂等）：
  1. 扫描 schema（``QuantDBFactorReader.describe``）→ 写数据源状态表 + 字段注册表；
  2. 已发布该库目录则跳过（除非 ``--force-new-version``）；
  3. 否则建草稿 → 全量播种映射 → 发布。

用法（在 quantmind 容器内执行，容器有 DB 与 /data 挂载）：
    docker exec -w /app quantmind python backend/scripts/bootstrap_factor_catalog.py \
        --libs factor_defs,gap_mined --market CN [--dry-run] [--force-new-version]

只写注册表与目录表，**不碰任何 parquet 数据**。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import text  # noqa: E402

from backend.services.api.routers.admin.quantdb_factor_catalog import (  # noqa: E402
    _ensure_schema,
    create_catalog_draft,
    publish_catalog_version,
    record_source_fields,
    seed_catalog_mappings,
)
from backend.services.engine.data_platform.quantdb_factor_reader import (  # noqa: E402
    QuantDBFactorReader,
    normalize_market,
)
from backend.shared.database_manager_v2 import get_session  # noqa: E402


def _status_dict(reader: QuantDBFactorReader, lib: str) -> dict:
    status = reader.describe(lib)
    return {
        "path": str(status.path),
        "files": int(status.files),
        "columns": list(status.columns),
        "column_types": dict(status.column_types or {}),
        "schema_hash": str(status.schema_hash or ""),
        "min_date": status.min_date,
        "max_date": status.max_date,
        "ready": bool(status.ready),
        "missing_required": list(status.missing_required),
        "reason": status.reason,
    }


async def _published_version(session, lib: str, market: str) -> str | None:
    row = (await session.execute(text("""
        SELECT version_id FROM qm_training_factor_catalog_version
        WHERE source_dataset = :lib AND market = :market AND status = 'published'
        ORDER BY published_at DESC NULLS LAST LIMIT 1
    """), {"lib": lib, "market": market})).first()
    return str(row[0]) if row else None


async def bootstrap(libs: list[str], market: str, *, dry_run: bool, force_new: bool) -> int:
    market = normalize_market(market)
    reader = QuantDBFactorReader(market=market)
    failed = 0
    async with get_session() as session:
        await _ensure_schema(session)
        for lib in libs:
            try:
                status = _status_dict(reader, lib)
            except Exception as exc:  # noqa: BLE001 — 单库失败不阻断其余库
                print(f"[{lib}] ✗ 扫描失败：{exc}")
                failed += 1
                continue
            print(
                f"[{lib}] 列 {len(status['columns'])} / 文件 {status['files']} / "
                f"{status['min_date']}~{status['max_date']} / ready={status['ready']}"
            )
            published = await _published_version(session, lib, market)
            if published and not force_new:
                print(f"[{lib}] 已有已发布目录 {published}，跳过（--force-new-version 可强制重建）")
                continue
            if dry_run:
                print(f"[{lib}] dry-run：将写入状态/字段并新建草稿 → 播种 → 发布")
                continue
            await record_source_fields(session, lib, status, market)
            version_id = await create_catalog_draft(
                session, lib, f"{lib} bootstrap", market, created_by="bootstrap-script",
            )
            seeded = await seed_catalog_mappings(session, {
                "version_id": version_id, "market": market, "source_dataset": lib,
            })
            await publish_catalog_version(session, version_id)
            print(
                f"[{lib}] ✓ 已发布 {version_id}：启用 {seeded['enabled_fields']} 个映射"
                f"（默认勾选 {seeded['default_selected_fields']}）"
            )
    return failed


def main() -> int:
    parser = argparse.ArgumentParser(description="引导清单库的因子目录（字段 + 版本 + 发布）")
    parser.add_argument("--libs", required=True, help="逗号分隔的数据集名，如 factor_defs,gap_mined")
    parser.add_argument("--market", default="CN")
    parser.add_argument("--dry-run", action="store_true", help="只打印将做什么，不写库")
    parser.add_argument(
        "--force-new-version", action="store_true",
        help="已有已发布目录时也重建一版（旧版自动归档）",
    )
    args = parser.parse_args()
    libs = [lib.strip() for lib in args.libs.split(",") if lib.strip()]
    if not libs:
        print("--libs 为空")
        return 2
    failed = asyncio.run(bootstrap(
        libs, args.market, dry_run=args.dry_run, force_new=args.force_new_version,
    ))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
