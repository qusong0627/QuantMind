#!/usr/bin/env python3
"""模型 × 因子库的血缘体检（只读）：哪些模型的锚库已经变了。

回答「跨库训练的模型，后面推理受影响吗」这一个问题的**实测**版本：

  1. 每个 QuantDB 直读模型（``data_source=quantdb_factors``）的锚库、参与库、
     钉住的目录版本，一目了然；
  2. 锚库 ``schema_hash`` 与训练时记录的是否一致 —— 不一致即「漂移」；
  3. 漂移对三条推理链的含义（本脚本只判前两条，第三条由代码保证）：

     | 链 | 判据 | 漂移后果 |
     |---|---|---|
     | 实时/回放 | 逐库按列名取数，无哈希闸门 | 无影响 |
     | 批量**预检** | 缺列硬失败；漂移只提示 | 放行，带 ``schema_drift`` 标记 |
     | 批量**执行** | 同预检（2026-10-08 对齐） | 放行；缺列才 exit 2 |

  关键前提：**哈希只钉锚库**（``factor_schema_hash`` 由锚库的 ``assert_ready``
  产出）。副库增列永远不影响任何模型；只有锚库增列才会漂移。

在 quantmind 容器内执行（需先拷入，容器内没有仓库挂载）：
    docker cp skills/factor-materialize-catalog/scripts/model_lineage_drift.py \\
        quantmind:/tmp/model_lineage_drift.py
    docker exec -w /app quantmind python3 /tmp/model_lineage_drift.py [--json]

退出码恒为 0（体检不是门禁），除非依赖不可用。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, "/app")

MODELS_ROOT = Path("/app/models")


def _models() -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    for meta_path in Path(MODELS_ROOT).rglob("metadata.json"):
        try:
            meta = json.loads(meta_path.read_text("utf-8"))
        except Exception:  # noqa: BLE001 - 坏 metadata 不该让体检跑不完
            continue
        if isinstance(meta, dict) and meta.get("data_source") == "quantdb_factors":
            out.append((str(meta_path.parent), meta))
    return out


def _scan() -> dict[str, Any]:
    """按（目录, 市场, 锚库）分组 describe 一次，避免逐模型重复全量扫描。"""
    from backend.services.engine.data_platform.quantdb_factor_reader import (
        QuantDBFactorReader,
    )
    from backend.services.engine.inference.script_runner import (
        _resolve_market_factor_data_dir,
        _resolve_model_market,
    )

    groups: dict[tuple[str, str, str], list[tuple[str, dict[str, Any]]]] = {}
    for path, meta in _models():
        key = (
            _resolve_market_factor_data_dir(meta),
            _resolve_model_market(meta),
            str(meta.get("factor_source") or "l1_l2_factors"),
        )
        groups.setdefault(key, []).append((path, meta))

    rows: list[dict[str, Any]] = []
    for (data_dir, market, anchor), items in sorted(groups.items()):
        try:
            status = QuantDBFactorReader(data_dir, market=market).describe(anchor)
            live_hash, live_columns, err = status.schema_hash, len(status.columns), None
        except Exception as exc:  # noqa: BLE001 - 库读不出来要报，不能吞
            live_hash, live_columns, err = "", 0, str(exc)
        for path, meta in items:
            sources = {
                str(k): str(v)
                for k, v in (meta.get("factor_field_sources") or {}).items()
            }
            secondary = sorted(
                {v.split(":", 1)[0] for v in sources.values() if ":" in v}
            )
            recorded = str(meta.get("factor_schema_hash") or "")
            rows.append(
                {
                    "model": Path(path).name,
                    "path": path,
                    "market": market,
                    "anchor": anchor,
                    "data_dir": data_dir,
                    "secondary_libs": secondary,
                    "catalog_versions": meta.get("factor_catalog_versions") or {},
                    "recorded_hash": recorded[:12],
                    "live_hash": live_hash[:12],
                    "live_columns": live_columns,
                    "drift": bool(recorded) and recorded != live_hash,
                    "error": err,
                }
            )
    return {"models": rows, "count": len(rows)}


def _render(data: dict[str, Any]) -> str:
    rows = data["models"]
    out = [f"== QuantDB 直读模型 {data['count']} 个 =="]
    # 必须按**目录**分组，不能只按 (锚库, 市场)：同一个 CN/l1_factors 在
    # /data/quantdb 与 /data/quantcustom 是两个不同的库面，合并会把两套
    # schema_hash 混在一起显示，看上去像「同一锚库有三种记录」。
    by_anchor: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        by_anchor.setdefault(
            (row["anchor"], row["market"], row["data_dir"]), []
        ).append(row)
    for (anchor, market, data_dir), items in sorted(by_anchor.items()):
        drift = [r for r in items if r["drift"]]
        first = items[0]
        out.append(
            f"\n锚库 {anchor} [{market}] @ {data_dir}"
            f"  库面 {first['live_columns']} 列 · 模型 {len(items)} 个 · 漂移 {len(drift)} 个"
        )
        uniq = sorted({r["recorded_hash"] or "(无)" for r in items})
        out.append(f"   live={first['live_hash'] or '(describe 失败)'}  记录={uniq}")
        for row in drift:
            subs = ",".join(row["secondary_libs"]) or "无副库"
            out.append(f"   ⚠ {row['model']}  记录={row['recorded_hash']}  副库={subs}")
        if first.get("error"):
            out.append(f"   ✗ describe 失败：{first['error']}")
    cross = [r for r in rows if r["secondary_libs"]]
    out.append(f"\n== 跨库组合模型 {len(cross)} 个（副库增列永远不影响它们）==")
    for row in cross:
        vers = ",".join(sorted(row["catalog_versions"]))
        out.append(f"  {row['model']}  锚={row['anchor']}  参与库={vers}")
    if not cross:
        out.append("  无")
    out.append(
        "\n判读：漂移≠坏。锚库增列时——实时/回放与批量都按**列名**取数，"
        "列还在就能跑；\n     exit 2「该日期无数据」只在**要用的列真的没了**时发生。"
    )
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="输出 JSON 而非文本")
    args = parser.parse_args()
    data = _scan()
    print(
        json.dumps(data, ensure_ascii=False, indent=2) if args.json else _render(data)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
