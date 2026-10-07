#!/usr/bin/env python3
"""rd_mined 挖掘因子 → 物化库 → 训练目录 的状态体检（只读：无 DDL、无写入）。

回答四个问题：
  1. 挖掘产出有多少、谁还没物化（分「从未尝试 / 试过被值级查重拒 / 无码孤儿」）；
  2. 物化库（parquet）与清单（manifest）现状、最近一次物化时间；
  3. 训练目录：线上发布了哪版、启用多少；每份草稿与线上的启用差集——发布草稿
     = **替换**线上口径，草稿少了线上有的特征就是「发布即缩」，先看这里的告警；
  4. 从未尝试过的因子清单（可直接喂物化器 ``--factor-ids``）。

在 quantmind 容器内执行（需先拷入，容器内没有仓库挂载）：
    docker cp skills/factor-materialize-catalog/scripts/catalog_status.py \\
        quantmind:/tmp/catalog_status.py
    docker exec -w /app quantmind python3 /tmp/catalog_status.py [--json]

退出码恒为 0：这是体检不是门禁，读数异常也要能跑完看到原因。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

sys.path.insert(0, "/app")

from sqlalchemy import text  # noqa: E402

from backend.scripts.rd_mined_materialize import (  # noqa: E402
    LIB_MARKET,
    RD_MINED_SOURCE,
    _lib_root,
    _load_manifest,
    materialize_overview,
)
from backend.shared.database_manager_v2 import get_session  # noqa: E402
from backend.shared.factor_identity import feature_column_name  # noqa: E402

_VERSIONS_SQL = text("""
    SELECT version_id, version_name, status, created_by, created_at, published_at
    FROM qm_training_factor_catalog_version
    WHERE market = :market AND source_dataset = :source
    ORDER BY created_at
""")
_ENABLED_SQL = text("""
    SELECT feature_key FROM qm_training_factor_mapping
    WHERE version_id = :version_id AND enabled
""")
_FACTORS_SQL = text("""
    SELECT factor_id, factor_name, factor_code, status, created_at,
           metadata_json->>'task_id' AS task_id
    FROM rd_agent_factors
    WHERE COALESCE(market, 'a_share') = 'a_share'
    ORDER BY created_at
""")


async def _collect() -> dict[str, Any]:
    overview = await materialize_overview()
    manifest = _load_manifest(_lib_root())

    async with get_session(read_only=True) as session:
        versions = (
            (
                await session.execute(
                    _VERSIONS_SQL,
                    {
                        "market": LIB_MARKET,
                        "source": RD_MINED_SOURCE,
                    },
                )
            )
            .mappings()
            .all()
        )
        enabled: dict[str, set[str]] = {}
        for row in versions:
            if row["status"] in ("published", "draft"):
                keys = (
                    (
                        await session.execute(
                            _ENABLED_SQL, {"version_id": row["version_id"]}
                        )
                    )
                    .scalars()
                    .all()
                )
                enabled[row["version_id"]] = {str(k) for k in keys}
        factors = (await session.execute(_FACTORS_SQL)).mappings().all()

    by_status: dict[str, int] = {}
    for f in factors:
        by_status[str(f["status"])] = by_status.get(str(f["status"]), 0) + 1

    published = [r for r in versions if r["status"] == "published"]
    drafts = [r for r in versions if r["status"] == "draft"]
    online: set[str] = set()
    online_id = None
    if published:
        latest = max(published, key=lambda r: str(r["published_at"] or ""))
        online_id = str(latest["version_id"])
        online = enabled.get(online_id, set())

    draft_rows = []
    for d in drafts:
        keys = enabled.get(str(d["version_id"]), set())
        draft_rows.append(
            {
                "version_id": str(d["version_id"]),
                "version_name": str(d["version_name"]),
                "enabled": len(keys),
                # 发布=替换：草稿缺线上任何一个 = 点发布就会把线上缩掉
                "missing_vs_online": sorted(online - keys),
                "extra_vs_online": sorted(keys - online),
            }
        )

    never: list[dict[str, Any]] = []
    for f in factors:
        if str(f["status"]) != "completed":
            continue
        if not str(f["factor_code"] or "").strip():
            continue
        if str(f["factor_id"]) in manifest:
            continue
        never.append(
            {
                "factor_id": str(f["factor_id"]),
                "factor_name": str(f["factor_name"]),
                "feature_column": feature_column_name(str(f["factor_name"])),
                "task_id": str(f["task_id"] or "")[:8],
                "created_at": str(f["created_at"])[:16],
            }
        )

    return {
        "overview": overview,
        "factor_status": by_status,
        "versions": [
            {
                "version_id": str(r["version_id"]),
                "version_name": str(r["version_name"]),
                "status": str(r["status"]),
                "created_by": str(r["created_by"]),
                "created_at": str(r["created_at"])[:19],
            }
            for r in versions
        ],
        "online_version_id": online_id,
        "online_enabled": len(online),
        "drafts": draft_rows,
        "never_attempted": never,
    }


def _render(data: dict[str, Any]) -> str:
    o = data["overview"]
    out: list[str] = []
    skipped = o.get("candidates", {}).get("skipped", {})
    out.append("== 挖掘产出（rd_agent_factors, a_share）==")
    out.append(f"  按状态：{data['factor_status']}")
    out.append(
        "  候选分桶：已物化 {a} / 试过判重 {b} / 无码孤儿 {c} / 从未尝试 {d}".format(
            a=skipped.get("already_materialized", 0),
            b=skipped.get("rejected_duplicate", 0),
            c=skipped.get("no_code", 0),
            d=len(data["never_attempted"]),
        )
    )
    lib = o.get("library", {})
    out.append("\n== 物化库（rd_mined parquet）==")
    out.append(
        f"  因子列 {lib.get('factor_columns')} / 分区 {lib.get('partitions')} 个"
        f" / {lib.get('min_date')} ~ {lib.get('max_date')} / ready={lib.get('ready')}"
    )
    m = o.get("manifest", {})
    out.append(
        f"  清单 {m.get('total')} 条 {m.get('by_status')}，最近 {m.get('last_at')}"
    )

    cat = o.get("catalog", {})
    out.append("\n== 训练目录 ==")
    out.append(
        f"  线上 {data['online_version_id']}：启用 {data['online_enabled']} 列"
        f"（库面 {lib.get('factor_columns')} 列，up_to_date={cat.get('up_to_date')}）"
    )
    for d in data["drafts"]:
        flag = ""
        if d["missing_vs_online"]:
            flag = f"  ⚠ 发布即缩：线上有而草稿没有 {len(d['missing_vs_online'])} 个"
        out.append(
            f"  草稿 {d['version_id']}「{d['version_name']}」启用 {d['enabled']}"
            f"，比线上多 {len(d['extra_vs_online'])}{flag}"
        )
        for key in d["extra_vs_online"][:20]:
            out.append(f"      + {key}")
        if len(d["extra_vs_online"]) > 20:
            out.append(f"      … 其余 {len(d['extra_vs_online']) - 20} 个")
        for key in d["missing_vs_online"][:10]:
            out.append(f"      - {key}（线上有，草稿缺）")

    out.append("\n== 从未尝试物化的因子（可直接 --factor-ids）==")
    if not data["never_attempted"]:
        out.append("  无")
    for f in data["never_attempted"]:
        out.append(
            f"  {f['factor_id'][:8]} {f['factor_name'][:36]:<36}"
            f" → {f['feature_column'][:36]:<36} task={f['task_id']} {f['created_at']}"
        )
    if data["never_attempted"]:
        ids = ",".join(f["factor_id"] for f in data["never_attempted"])
        out.append(
            "\n  一键物化（演练在上，写入去掉 --dry-run 并加 --register 自动发布）："
        )
        out.append(
            "  docker exec -w /app quantmind python backend/scripts/rd_mined_materialize.py"
            f" --factor-ids {ids} --dry-run"
        )
    return "\n".join(out)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="输出 JSON 而非文本")
    args = parser.parse_args()
    data = await _collect()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    else:
        print(_render(data))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
