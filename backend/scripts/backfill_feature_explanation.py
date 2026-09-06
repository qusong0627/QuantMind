"""回填特征字典 explanation（用户可编辑长描述）。

- JSON: config/features/model_training_feature_catalog_v1.json 每个 feature 加 explanation（缺省用 quantdb 字典解释种子）
- DB: qm_feature_definition 加 explanation 列（幂等）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
JSON_PATH = PROJECT_ROOT / "config" / "features" / "model_training_feature_catalog_v1.json"

sys.path.insert(0, str(PROJECT_ROOT))
from backend.services.engine.data_platform.quantdb_factor_dictionary import definition_for


def main() -> None:
    raw = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    cats = raw.get("categories", [])
    filled = 0
    total = 0
    for cat in cats:
        for feat in cat.get("features", []):
            total += 1
            if str(feat.get("explanation") or "").strip():
                continue
            key = str(feat.get("key") or "").strip()
            # feature_name 缺失时由老 description 回填（兼容旧 JSON）
            if not feat.get("feature_name") and feat.get("description"):
                feat["feature_name"] = feat["description"]
            try:
                seed = str(definition_for(key).get("explanation") or "")
            except Exception:
                seed = ""
            # 模板化兜底文案不回填，保持空由前端提示添加
            if seed.startswith("具体计算口径") or "请参考官方帮助文档" in seed:
                seed = ""
            feat["explanation"] = seed
            if seed:
                filled += 1
    JSON_PATH.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"features={total} seeded_explanation={filled} path={JSON_PATH}")


if __name__ == "__main__":
    main()
