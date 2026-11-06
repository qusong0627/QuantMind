"""把全市场多分类模型登记进平台「模型管理」（只登记元数据，不接推理）。

- 写 ``qm_user_models``，``status='candidate'``（不在 ready/active 里，
  不会被一键推理/补推选中）；
- ``metadata_json.research_only=true`` 明确标注研究模型；
- 幂等：按 ``model_id`` upsert；``--delete`` 可整组回滚。

用法（容器内）：
  cd /app && python3 -m backend.scripts.stock_classifier.register_models
  cd /app && python3 -m backend.scripts.stock_classifier.register_models --delete
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import psycopg2

ROOT = Path("/data/stock_classifier")
MODEL_DIR = ROOT / "models"
HORIZONS = (1, 3, 5)
MODEL_PREFIX = "mdl_stock_multiclass_l1l2_h"
TENANT = "default"
OWNER = "admin"


def _connect():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "db"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "quantmind"),
        user=os.getenv("DB_USER", "quantmind"),
        password=os.getenv("DB_PASSWORD", ""),
    )


def _metadata(horizon: int, report: dict) -> dict:
    return {
        "model_type": "custom_multiclass_l1l2",
        "research_only": True,
        "horizon": horizon,
        "n_classes": report.get("n_classes", 5),
        "feature_count": len(report.get("features", [])),
        "features": report.get("features", []),
        "train_start": "2018-01-02",
        "train_end": "2024-05-31",
        "valid": "2024-06-03~2025-06-30",
        "test": "2025-07-01~2026-06-30",
        "oos": "2026-07-01~2026-11-05",
        "inference": "未接入平台推理，需要自定义 adapter（构建 l1_l2_factors 面板 → 50 特征 → 5 类概率）",
        "note": "实盘口径回测（次日开盘/收盘成交 + 成本）未通过；alpha 主要在隔夜跳空",
        "doc": "docs/全市场多分类模型_L1L2筛选_训练与样本外评估_20261106.md",
    }


def _metrics(report: dict) -> dict:
    out = {}
    for split in ("valid", "test", "oos"):
        s = report.get("splits", {}).get(split, {})
        if not s:
            continue
        out[split] = {
            "ic": s.get("ic", {}).get("ic_mean"),
            "ic_ir": s.get("ic", {}).get("ic_ir"),
            "accuracy": s.get("classification", {}).get("accuracy"),
            "macro_f1": s.get("classification", {}).get("macro_f1"),
            "top_bottom_spread": s.get("decile", {}).get("top_minus_bottom"),
        }
    return out


def register() -> None:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            for horizon in HORIZONS:
                meta_path = MODEL_DIR / f"metadata_h{horizon}.json"
                model_path = MODEL_DIR / f"lgb_h{horizon}.pkl"
                if not meta_path.exists() or not model_path.exists():
                    print(f"skip h{horizon}: 缺 {meta_path.name} 或 {model_path.name}")
                    continue
                report = json.loads(meta_path.read_text("utf-8"))
                model_id = f"{MODEL_PREFIX}{horizon}"
                cur.execute(
                    """
                    INSERT INTO qm_user_models
                      (tenant_id, user_id, model_id, status, storage_path,
                       model_file, metadata_json, metrics_json, is_default)
                    VALUES (%s, %s, %s, 'candidate', %s, %s, %s, %s, FALSE)
                    ON CONFLICT (tenant_id, user_id, model_id) DO UPDATE SET
                      status = EXCLUDED.status,
                      storage_path = EXCLUDED.storage_path,
                      model_file = EXCLUDED.model_file,
                      metadata_json = EXCLUDED.metadata_json,
                      metrics_json = EXCLUDED.metrics_json,
                      updated_at = NOW()
                    """,
                    (
                        TENANT,
                        OWNER,
                        model_id,
                        str(MODEL_DIR),
                        model_path.name,
                        json.dumps(_metadata(horizon, report), ensure_ascii=False),
                        json.dumps(_metrics(report), ensure_ascii=False),
                    ),
                )
                print(f"registered {model_id}")
        conn.commit()
    finally:
        conn.close()


def delete() -> None:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM qm_user_models WHERE tenant_id=%s AND model_id LIKE %s",
                (TENANT, f"{MODEL_PREFIX}%"),
            )
            print(f"deleted {cur.rowcount} rows")
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--delete", action="store_true")
    args = parser.parse_args()
    delete() if args.delete else register()
