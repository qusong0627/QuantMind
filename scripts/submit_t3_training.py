"""提交 T+3 CatBoost 训练任务（复刻现有 T+5 L2 CatBoost 配置，仅改周期）。

现有 T+5: train 2023-01-03~2025-12-31, val 2026-01-05~2026-06-30, test 2026-07-05~2026-08-18
本任务: 同样时间，target_horizon_days=3。
"""
import asyncio
import os
from backend.shared.database_manager_v2 import get_session
from sqlalchemy import text


async def get_t5_features():
    """从现有 T+5 run 的 request_payload 拿特征列表（保证 T+3 用完全一样特征）"""
    async with get_session(read_only=True) as s:
        r = await s.execute(text(
            "SELECT request_payload FROM admin_training_jobs WHERE id='train_20260819100559_9163cb84'"
        ))
        row = r.fetchone()
        if not row:
            return None
        import json
        p = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        return p


async def main():
    payload = await get_t5_features()
    if not payload:
        print("找不到 T+5 训练请求，从 metadata 构造")
        return
    # 复制 T+5 payload，改 T+3
    t3 = dict(payload)
    t3["target_horizon_days"] = 3
    t3["job_name"] = "L2_catboost_2023_2025T3"
    t3["display_name"] = "L2 CatBoost T+3 (2023-2025训练)_CN"
    # 特征保持一样
    t3.pop("model_id", None)
    t3.pop("run_id", None)
    import json as _json
    print("T+3 payload 关键字段:")
    for k in ["model_type", "target_horizon_days", "train_start", "train_end",
              "val_start", "val_end", "test_start", "test_end", "job_name", "display_name"]:
        print(f"  {k}: {t3.get(k)}")
    print(f"  features: {len(t3.get('features', []))}")
    print(f"  num_boost_round: {t3.get('num_boost_round')}")
    print(f"  early_stopping: {t3.get('early_stopping_rounds')}")
    print(f"  lr: {t3.get('learning_rate')}")
    # 提交到 run-training
    import httpx
    async with httpx.AsyncClient(base_url="http://localhost:8000/api/v1", timeout=60) as c:
        login = await c.post("/auth/login", json={"username": "admin", "password": os.getenv("QM_PASSWORD", ""), "tenant_id": "default"})
        tok = login.json().get("access_token") or login.json().get("data", {}).get("access_token", "")
        r = await c.post("/models/run-training", json=t3, headers={"Authorization": f"Bearer {tok}"})
        print("提交结果:", r.status_code, str(r.text)[:300])


asyncio.run(main())
