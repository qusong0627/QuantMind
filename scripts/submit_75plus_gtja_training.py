"""提交 CatBoost T+5 训练：旧75特征 + 13个GTJA = 88特征（跳过因子筛选，手动指定）。

目的：验证 GTJA 在原有 75 个有效因子基础上是否有增量预测力。
- 特征：旧 T+5 模型的 75 个入选特征 + 13 个 GTJA Alpha191（去重后 88 个）
- 不开 factor_selection（手动指定，跳过 IC/ICIR 筛选）
- 截面预处理：enabled=true, winsor=true（中位数填充+缩尾+Z-score）
- 训练区间 2023-2025，T+5 周期
"""
import asyncio
import json
import os

import httpx

FEATURES_FILE = "/tmp/combined_features_87.json"


async def main():
    with open(FEATURES_FILE) as f:
        features = json.load(f)
    print(f"特征数: {len(features)}")
    print(f"含 GTJA: {[f for f in features if f.startswith('gtja')]}")

    payload = {
        "model_type": "catboost",
        "job_name": "L2_catboost_2023_2025T5_75plusGTJA",
        "display_name": "L2 CatBoost T+5 旧75特征+GTJA13 (2023-2025)",
        "features": features,
        "catboost_params": {
            "depth": 6,
            "od_wait": 150,
            "l2_leaf_reg": 3.0,
            "learning_rate": 0.03,
            "random_strength": 1.2,
            "bagging_temperature": 0.8,
        },
        "ensemble": "none",
        "factor_selection": {"method": "none"},
        "preprocessing": {"enabled": True, "winsor": True},
        "target_horizon_days": 5,
        "target_mode": "return",
        "num_boost_round": 3000,
        "early_stopping_rounds": 150,
        "train_start": "2023-01-03",
        "train_end": "2025-12-31",
        "valid_start": "2026-01-05",
        "valid_end": "2026-06-30",
        "test_start": "2026-07-05",
        "test_end": "2026-08-18",
        "val_ratio": 0.15,
        "context": {
            "market": "CN",
            "slippage": 0.0005,
            "benchmark": "SH000300",
            "deal_price": "close",
            "commission_rate": 0.00025,
            "initial_capital": 1000000.0,
            "industry_as_feature": False,
        },
        "required_artifacts": [
            "model.lgb", "pred.pkl", "metadata.json", "config.yaml", "result.json",
        ],
    }

    async with httpx.AsyncClient(base_url="http://localhost:8000/api/v1", timeout=120) as c:
        login = await c.post("/auth/login", json={
            "username": "admin", "password": os.getenv("QM_PASSWORD", ""), "tenant_id": "default"})
        tok = (login.json().get("access_token")
               or login.json().get("data", {}).get("access_token", ""))
        headers = {"Authorization": f"Bearer {tok}"}

        r = await c.post("/models/run-training", json=payload, headers=headers)
        print(f"Status: {r.status_code}")
        print(f"Response: {r.text[:500]}")


asyncio.run(main())
