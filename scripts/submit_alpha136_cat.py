"""提交 T5_Alpha136 CatBoost 复训任务（CN，T+5，train 2016-2024 / valid 2025 / test 2026）。

背景：2026-08-17 的原始任务 train_20260817084510_d18103ea 产物目录已被清理
（注册表仍为 ready，storage_path 不存在），本次按同一份 136 特征清单、同一份
模型参数复刻一份，产物用于打包交付。

切分（平台强制 train < valid < test，故 2025 作早停集、2026 作最终评估集）：
  train  2016-01-04 ~ 2024-12-31
  valid  2025-01-02 ~ 2025-12-31
  test   2026-01-05 ~ 2026-08-18   ← 遗留特征快照 core parquet 的末日

数据源：不传 factor_source → 走遗留 L1 特征池 db/feature_snapshots
（model_features_core.parquet，436 列，136 特征全命中）。当前 QuantDB
l1_factors 只含其中 72 个，无法组齐清单。

用法：python scripts/submit_alpha136_cat.py [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import json

ORIGINAL_RUN_ID = "train_20260817084510_d18103ea"

JOB_NAME = "T5_Alpha136_2016_2024_CAT"
DISPLAY_NAME = "T5_Alpha136_2016_2024_CAT"

SPLIT = {
    "train_start": "2016-01-04",
    "train_end": "2024-12-31",
    "valid_start": "2025-01-02",
    "valid_end": "2025-12-31",
    "test_start": "2026-01-05",
    "test_end": "2026-08-18",
}


async def _load_original_payload() -> dict:
    """取原始任务的请求载荷：特征清单与模型参数的唯一事实源。"""
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=True) as s:
        row = (
            await s.execute(
                text("SELECT request_payload FROM admin_training_jobs WHERE id=:i"),
                {"i": ORIGINAL_RUN_ID},
            )
        ).mappings().first()
    if not row:
        raise SystemExit(f"找不到原始任务 {ORIGINAL_RUN_ID}")
    payload = row["request_payload"]
    return json.loads(payload) if isinstance(payload, str) else dict(payload)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印载荷，不提交")
    args = ap.parse_args()

    original = await _load_original_payload()
    features = list(original["features"])
    if len(features) != 136:
        raise SystemExit(f"原始特征数不是 136（{len(features)}），中止")

    payload = {
        "job_name": JOB_NAME,
        "display_name": DISPLAY_NAME,
        "model_type": "catboost",
        "features": features,
        "target_horizon_days": 5,
        "target_mode": original.get("target_mode", "return"),
        "label_formula": original.get("label_formula", ""),
        "effective_trade_date": "",
        "training_window": "",
        "num_boost_round": original.get("num_boost_round", 2000),
        "early_stopping_rounds": original.get("early_stopping_rounds", 100),
        "catboost_params": original["catboost_params"],
        "max_time_minutes": original.get("max_time_minutes", 720),
        "context": original["context"],
        # 原任务关闭了 SHAP（enable_shap=False），保持一致以免额外耗时
        "explain": original["explain"],
        "ensemble": "none",
        "deploy_to_production": False,
        # method 留空 = 不做 IC/ICIR 筛选，136 个特征全量入模。
        # 不传这个键的话，编排器默认注入 method=ic_icir、n_top=80，
        # 会把特征裁到 80 个以内（原始任务的 feature_selection 键名不被白名单
        # 接收，等于没传，本次改用正确键名并显式关闭）。
        "factor_selection": {
            "method": "",
            "n_top": 136,
            "ic_threshold": 0.01,
            "icir_threshold": 0.15,
            "correlation_threshold": 0.9,
            "pfs_enabled": False,
            "dh_enabled": False,
        },
        "required_artifacts": [
            "model.cbm",
            "pred.parquet",
            "metadata.json",
            "config.yaml",
            "result.json",
        ],
        **SPLIT,
    }

    print(f"特征数: {len(payload['features'])}")
    print(f"catboost_params: {payload['catboost_params']}")
    for k in ("train_start", "train_end", "valid_start", "valid_end", "test_start", "test_end"):
        print(f"  {k}: {payload[k]}")
    if args.dry_run:
        print("[dry-run] 未提交")
        return

    import httpx

    async with httpx.AsyncClient(base_url="http://localhost:8000/api/v1", timeout=120) as c:
        login = await c.post(
            "/auth/login",
            json={"username": "admin", "password": "admin123", "tenant_id": "default"},
        )
        body = login.json()
        tok = body.get("access_token") or body.get("data", {}).get("access_token", "")
        if not tok:
            raise SystemExit(f"登录失败: {login.status_code} {str(login.text)[:200]}")
        r = await c.post(
            "/models/run-training", json=payload, headers={"Authorization": f"Bearer {tok}"}
        )
        print("提交结果:", r.status_code, str(r.text)[:400])


asyncio.run(main())
