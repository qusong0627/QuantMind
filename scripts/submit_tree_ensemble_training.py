"""提交 L2 三树模型训练（CatBoost / XGBoost / LightGBM，同规格 2023-2025 训练）。

复刻现有 T+5 L2 CatBoost 配置（train_20260819100559_9163cb84），仅换 model_type，
供后续融合（Ensemble）对比。特征/时间切分/标签完全一致。
"""
import asyncio
import json
import os

from backend.shared.database_manager_v2 import get_session
from sqlalchemy import text

BASE_JOB_ID = "train_20260819100559_9163cb84"

JOBS = [
    {
        "model_type": "catboost",
        "job_name": "L2_catboost_2023_2025_T5_tree",
        "display_name": "L2 CatBoost T+5 树融合 (2023-2025训练)",
        "params_key": "catboost_params",
        "params": {"depth": 6, "od_wait": 150, "l2_leaf_reg": 3.0,
                   "learning_rate": 0.03, "random_strength": 1.2,
                   "bagging_temperature": 0.8},
    },
    {
        "model_type": "xgboost",
        "job_name": "L2_xgboost_2023_2025_T5_tree",
        "display_name": "L2 XGBoost T+5 树融合 (2023-2025训练)",
        "params_key": "xgb_params",
        "params": {"max_depth": 6, "learning_rate": 0.03, "subsample": 0.8,
                   "colsample_bytree": 0.8, "min_child_weight": 50,
                   "reg_lambda": 3.0, "tree_method": "hist"},
    },
    {
        "model_type": "lightgbm",
        "job_name": "L2_lightgbm_2023_2025_T5_tree",
        "display_name": "L2 LightGBM T+5 树融合 (2023-2025训练)",
        "params_key": "lgb_params",
        "params": {"objective": "regression", "metric": "l2", "learning_rate": 0.03,
                   "num_leaves": 63, "feature_fraction": 0.8, "bagging_fraction": 0.8,
                   "bagging_freq": 1, "lambda_l2": 3.0, "min_child_samples": 100,
                   "max_depth": -1, "verbosity": -1},
    },
]


async def get_base_payload() -> dict | None:
    async with get_session(read_only=True) as s:
        r = await s.execute(text(
            f"SELECT request_payload FROM admin_training_jobs WHERE id='{BASE_JOB_ID}'"
        ))
        row = r.fetchone()
        if not row:
            return None
        p = row[0]
        return json.loads(p) if isinstance(p, str) else p


async def main():
    base = await get_base_payload()
    if not base:
        print("找不到基准训练请求")
        return

    import httpx
    async with httpx.AsyncClient(base_url="http://localhost:8000/api/v1", timeout=120) as c:
        login = await c.post("/auth/login", json={
            "username": "admin", "password": os.getenv("QM_PASSWORD", ""), "tenant_id": "default"})
        tok = (login.json().get("access_token")
               or login.json().get("data", {}).get("access_token", ""))
        headers = {"Authorization": f"Bearer {tok}"}

        for job in JOBS:
            payload = dict(base)
            payload["model_type"] = job["model_type"]
            payload["job_name"] = job["job_name"]
            payload["display_name"] = job["display_name"]
            payload[job["params_key"]] = job["params"]
            payload["ensemble"] = "none"
            payload.pop("model_id", None)
            payload.pop("run_id", None)

            r = await c.post("/models/run-training", json=payload, headers=headers)
            print(f"[{job['model_type']}] {r.status_code} {str(r.text)[:200]}")


asyncio.run(main())
