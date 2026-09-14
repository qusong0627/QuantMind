#!/usr/bin/env python3
"""把推荐因子集写进训练因子目录 → 训练页默认勾选。

链路（详见 docs/因子组合到训练勾选_设计方案.md）：
  factor_portfolio.json（P2 产出）→ 克隆当前已发布目录为草稿 → 入选因子 default_selected=true
  （其余 false，或 --keep-others 保留原样）→ 发布 → 训练页的勾选初值即来自该 flag。

为什么走目录：训练页把 catalog 的 default_selected 当勾选初值的 truth source
（electron/src/pages/training/trainingUtils.tsx:991）。

用法：
  python backend/scripts/apply_factor_selection_to_catalog.py --dataset l1_l2_factors --dry-run
  python backend/scripts/apply_factor_selection_to_catalog.py --dataset l1_l2_factors
  python backend/scripts/apply_factor_selection_to_catalog.py --dataset all --keep-others
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import text  # noqa: E402

from backend.services.engine.factor_report.datasets import DATASETS  # noqa: E402
from backend.services.engine.factor_report.portfolio import portfolio_path  # noqa: E402
from backend.shared.database_manager_v2 import get_session  # noqa: E402


def _seeds_from_kept(kept_data: dict, without: set[str]) -> dict[str, set[str]]:
    """筛选保留集 → {数据集: 因子名集合}（可按库剔除；仅保留有训练目录的数据集）。"""
    out: dict[str, set[str]] = {}
    for k in kept_data.get("kept", []):
        lib = str(k.get("library") or "")
        name = str(k.get("name") or "")
        if not name or lib in without:
            continue
        if lib not in DATASETS:
            continue  # 研究专用（如 factor_research）无训练目录
        out.setdefault(lib, set()).add(name)
    return out


async def _apply_one(
    dataset: str,
    *,
    market: str,
    keep_others: bool,
    dry_run: bool,
    version_name: str | None,
    selected: set[str] | None = None,
) -> dict:
    if selected is None:
        pj = portfolio_path(dataset)
        if not pj.exists():
            return {"dataset": dataset, "ok": False, "reason": f"缺少 {pj}（先跑 build_factor_portfolio.py）"}
        payload = json.loads(pj.read_text(encoding="utf-8"))
        if not payload.get("available"):
            return {"dataset": dataset, "ok": False, "reason": payload.get("reason") or "组合不可用"}
        selected = {f["name"] for f in payload.get("factors", [])}
    if not selected:
        return {"dataset": dataset, "ok": False, "reason": "推荐集为空"}

    async with get_session() as session:
        # 必须带市场：l1_factors 在 CN/US/HK 等多市场各有目录版本，只按数据集查会命中别的市场
        pub = (await session.execute(text("""
            SELECT version_id, market FROM qm_training_factor_catalog_version
            WHERE source_dataset = :ds AND market = :market AND status = 'published'
            ORDER BY published_at DESC NULLS LAST LIMIT 1
        """), {"ds": dataset, "market": market})).mappings().first()
        if not pub:
            return {"dataset": dataset, "ok": False, "reason": f"{market} 的 {dataset} 没有已发布的目录版本"}
        mapping_count = (await session.execute(text("""
            SELECT count(*) FROM qm_training_factor_mapping WHERE version_id = :v
        """), {"v": pub["version_id"]})).scalar_one()

        summary = {
            "dataset": dataset,
            "from_version": pub["version_id"],
            "mappings": int(mapping_count),
            "selected": len(selected),
        }
        if dry_run:
            hit = (await session.execute(text("""
                SELECT count(*) FROM qm_training_factor_mapping
                WHERE version_id = :v AND source_column = ANY(:names)
            """), {"v": pub["version_id"], "names": list(selected)})).scalar_one()
            summary.update({"ok": True, "dry_run": True, "hit_in_catalog": int(hit)})
            return summary

        # ① 克隆已发布版本为草稿（保留 enabled/required/sort_order，只改 default_selected）
        clone_id = f"qdb-{str(pub['market']).lower()}-{dataset}-{uuid.uuid4().hex[:12]}"
        await session.execute(text("""
            INSERT INTO qm_training_factor_catalog_version
              (version_id, market, version_name, status, source_dataset, created_by)
            VALUES (:cid, :market, :name, 'draft', :ds, 'factor-selection')
        """), {"cid": clone_id, "market": pub["market"], "ds": dataset,
               "name": version_name or f"{DATASETS.get(dataset, {}).get('label', dataset)}（因子体检优选）"})
        await session.execute(text("""
            INSERT INTO qm_training_factor_mapping
              (mapping_id, version_id, source_dataset, source_column, feature_key, display_name,
               category_id, category_name, enabled, default_selected, required, sort_order)
            SELECT :prefix || mapping_id, :cid, source_dataset, source_column, feature_key, display_name,
                   category_id, category_name, enabled, default_selected, required, sort_order
            FROM qm_training_factor_mapping WHERE version_id = :old
        """), {"prefix": f"{uuid.uuid4().hex[:8]}-", "cid": clone_id, "old": pub["version_id"]})

        # ② 勾选推荐因子（其余按 --keep-others 决定是否清空）
        names = list(selected)
        await session.execute(text("""
            UPDATE qm_training_factor_mapping SET default_selected = TRUE
            WHERE version_id = :cid AND source_column = ANY(:names)
        """), {"cid": clone_id, "names": names})
        if not keep_others:
            await session.execute(text("""
                UPDATE qm_training_factor_mapping SET default_selected = FALSE
                WHERE version_id = :cid AND NOT (source_column = ANY(:names))
            """), {"cid": clone_id, "names": names})
        await session.execute(text("""
            UPDATE qm_training_factor_mapping SET enabled = TRUE
            WHERE version_id = :cid AND source_column = ANY(:names) AND NOT enabled
        """), {"cid": clone_id, "names": names})

        # ③ 发布（与 API publish 同语义：旧版本转 archived）
        await session.execute(text("""
            UPDATE qm_training_factor_catalog_version SET status = 'archived'
            WHERE source_dataset = :ds AND market = :market AND status = 'published'
        """), {"ds": dataset, "market": pub["market"]})
        await session.execute(text("""
            UPDATE qm_training_factor_catalog_version
            SET status = 'published', published_at = :ts WHERE version_id = :cid
        """), {"cid": clone_id, "ts": datetime.now(timezone.utc)})

        # ④ 复核
        checked = (await session.execute(text("""
            SELECT count(*) FROM qm_training_factor_mapping
            WHERE version_id = :cid AND default_selected
        """), {"cid": clone_id})).scalar_one()
        summary.update({"ok": True, "new_version": clone_id, "checked_after": int(checked)})
        return summary


async def main() -> int:
    ap = argparse.ArgumentParser(description="把推荐因子集写进训练目录（训练页勾选）")
    ap.add_argument("--dataset", default="all")
    ap.add_argument("--keep-others", action="store_true", help="保留其它因子的原勾选状态（默认全部清掉只留推荐集）")
    ap.add_argument("--version-name", default=None, help="新版本名（默认「<数据集>（因子体检优选）」）")
    ap.add_argument("--market", default="CN", help="目录市场（默认 CN；推荐集基于 A 股因子表）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--source",
        choices=["portfolio", "kept"],
        default="portfolio",
        help="勾选来源：portfolio=因子体检推荐组合（默认）；kept=因子研究筛选保留集",
    )
    ap.add_argument(
        "--without-libraries",
        default="",
        help="kept 模式下要剔除的因子库（逗号分隔，如 l2_factors / l1_factors,l2_factors）",
    )
    ap.add_argument(
        "--kept-file",
        default="",
        help="kept 模式读取的筛选结果（默认 <quantdb>/factor_research/screening/factor_selection.json）",
    )
    args = ap.parse_args()

    datasets = list(DATASETS) if args.dataset == "all" else [args.dataset]
    datasets = [d for d in datasets if d in DATASETS] or list(DATASETS)

    failed = 0
    seeds: dict[str, set[str]] | None = None
    if args.source == "kept":
        without = {x.strip() for x in args.without_libraries.split(",") if x.strip()}
        kept_path = Path(args.kept_file).expanduser() if args.kept_file else None
        if kept_path is None:
            from backend.shared.quantdb_paths import resolve_quantdb_dir

            kept_path = (
                resolve_quantdb_dir()
                / "factor_research"
                / "screening"
                / "factor_selection.json"
            )
        if not kept_path.exists():
            print(f"[kept] 找不到筛选结果: {kept_path}（先跑 screen_factors.py）")
            return 1
        kept_data = json.loads(kept_path.read_text(encoding="utf-8"))
        seeds = _seeds_from_kept(kept_data, without)
        total = sum(len(v) for v in seeds.values())
        print(
            f"[kept] {kept_path}：剔除库 {sorted(without) or '无'} 后，"
            f"可用 {total} 个因子（按数据集 { {k: len(v) for k, v in seeds.items()} }）"
        )
        if without:
            export_path = kept_path.parent / (
                "kept_features_excl_" + "_".join(sorted(without)) + ".txt"
            )
            export_path.write_text(
                "\n".join(sorted(e for v in seeds.values() for e in v)) + "\n",
                encoding="utf-8",
            )
            print(f"[kept] 已导出因子清单 -> {export_path}")

    for ds in datasets:
        if seeds is not None and ds not in seeds:
            continue
        res = await _apply_one(
            ds,
            market=args.market.upper(),
            keep_others=args.keep_others,
            dry_run=args.dry_run,
            version_name=args.version_name,
            selected=seeds.get(ds) if seeds is not None else None,
        )
        if res.get("ok"):
            if res.get("dry_run"):
                print(f"[{ds}] 预演：目录 {res['mappings']} 条中命中推荐因子 {res['hit_in_catalog']} 个"
                      f"（推荐集 {res['selected']} 个）｜来源版本 {res['from_version']}")
            else:
                print(f"[{ds}] 已发布 {res['new_version']}：勾选 {res['checked_after']} 个"
                      f"（推荐集 {res['selected']} 个，来源 {res['from_version']}）")
        else:
            failed += 1
            print(f"[{ds}] 失败：{res.get('reason')}")
    return 1 if failed and failed == len(datasets) else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
