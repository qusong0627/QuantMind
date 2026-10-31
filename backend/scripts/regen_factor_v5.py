#!/usr/bin/env python3
"""从 QuantDB 库内因子字典重新生成 quantdb_factor_v5.py 快照。

数据源：SDK `get_field_dictionary`（网关 JSON，不消耗流量），与
COS 上的 6_ml_datasets/factor_dictionary/v5_factor_dictionary.json 同源。

用法：
  python backend/scripts/regen_factor_v5.py            # 拉取并重写快照
  python backend/scripts/regen_factor_v5.py --check   # 只对比差异不写盘
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

SNAPSHOT_PATH = (
    PROJECT_ROOT
    / "backend"
    / "services"
    / "engine"
    / "data_platform"
    / "quantdb_factor_v5.py"
)


def fetch_dictionary() -> dict:
    import time

    from backend.shared.runtime_secrets import get_secret, load_runtime_env

    load_runtime_env()
    api_key = get_secret("QUANTDB_API_KEY")
    if not api_key:
        raise RuntimeError("QUANTDB_API_KEY 未配置")
    from quantdb_sdk import QuantDBClient

    client = QuantDBClient(api_key=api_key)
    groups_df = client.get_field_groups("factor_dictionary")
    groups = list(dict.fromkeys(str(r) for r in groups_df["group"].tolist()))
    columns: list[dict] = []
    seen: set[str] = set()
    for i, group in enumerate(groups):
        for attempt in range(1, 6):
            try:
                payload = client.get_field_dictionary(
                    "factor_dictionary", group=group
                )
                break
            except Exception as exc:  # noqa: BLE001 - 网关限流时退避重试
                if attempt >= 5:
                    raise RuntimeError(
                        f"拉取分组 {group} 失败（已重试）: {exc}"
                    ) from exc
                time.sleep(2.0 * attempt)
        for col in payload.get("columns", []):
            # 分组查询可能跨组回传重复列，按 field 去重（首见为准）
            name = str(col.get("field") or "")
            if name and name not in seen:
                seen.add(name)
                columns.append(col)
        if i + 1 < len(groups):
            time.sleep(0.5)  # 分组间歇，避免触发网关限流
    columns.sort(key=lambda c: int(c.get("order") or 0))
    first = client.get_field_dictionary("factor_dictionary")
    return {
        "schema_version": first.get("schema_version", ""),
        "generated_at": first.get("generated_at", ""),
        "total_columns": first.get("total_columns", len(columns)),
        "columns": columns,
    }


def render(data: dict) -> str:
    cols = data["columns"]
    lines = []
    lines.append('"""V5 因子字典快照（库内权威字典的本地只读镜像）。')
    lines.append('')
    lines.append('由 backend/scripts/regen_factor_v5.py 从 QuantDB 库内因子字典')
    lines.append('（6_ml_datasets/factor_dictionary/v5_factor_dictionary.json）生成。')
    lines.append(
        f'schema={data["schema_version"]}，生成于 {data["generated_at"]}，'
        f'共 {data["total_columns"]} 列。'
    )
    lines.append('管理台展示与训练目录播种直接消费本快照；未知列回退到')
    lines.append('quantdb_factor_dictionary 的启发式规则。请勿手改。')
    lines.append('"""')
    lines.append('')
    lines.append('from __future__ import annotations')
    lines.append('')
    lines.append(f'SCHEMA_VERSION = "{data["schema_version"]}"')
    lines.append(f'GENERATED_AT = "{data["generated_at"]}"')
    lines.append(f'TOTAL_COLUMNS = {data["total_columns"]}')
    lines.append('')
    lines.append('V5_FIELDS: dict[str, dict[str, object]] = {')
    for c in cols:
        f = str(c["field"])
        name = str(c.get("name_cn") or f)
        formula = str(c.get("formula") or "")
        desc = str(c.get("description") or "").strip()
        expl = desc or (f"{name}（{formula}）" if formula else name)
        lines.append(f'    {f!r}: {{')
        lines.append(f'        {"display_name"!r}: {name!r},')
        lines.append(f'        {"explanation"!r}: {expl!r},')
        lines.append(f'        {"category_id"!r}: {str(c.get("group") or "other")!r},')
        lines.append(f'        {"category_name"!r}: {str(c.get("group") or "其他因子")!r},')
        lines.append(f'        {"sort_order"!r}: {int(c.get("order") or 0)},')
        lines.append(f'        {"formula"!r}: {formula!r},')
        lines.append(f'        {"direction"!r}: {str(c.get("direction") or "")!r},')
        lines.append(f'        {"source"!r}: {str(c.get("source") or "")!r},')
        lines.append('    },')
    lines.append('}')
    lines.append('')
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="只对比差异不写盘")
    args = ap.parse_args()

    data = fetch_dictionary()
    print(
        f"dictionary: {data['schema_version']} "
        f"{data['total_columns']} 列 / {len(data['columns'])} 字段"
    )
    text = render(data)
    current = (
        SNAPSHOT_PATH.read_text(encoding="utf-8") if SNAPSHOT_PATH.is_file() else ""
    )
    if current == text:
        print("snapshot 已是最新，无需更新")
        return 0
    if args.check:
        print("snapshot 与库内字典不一致（--check 模式，未写盘）")
        return 1
    SNAPSHOT_PATH.write_text(text, encoding="utf-8")
    print(f"written: {SNAPSHOT_PATH} ({len(data['columns'])} 字段)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
