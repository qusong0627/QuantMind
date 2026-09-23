"""提交 CatBoost T+5 全特征训练（含行业编码类别特征 + 截面预处理）。

复刻 T+5 L2 CatBoost 基准配置，并：
1. 特征改为目录中全部 enabled 特征（517 个，含 ind_code_l1/l2 行业编码）
2. 开启截面预处理：preprocessing.enabled=true, winsor=true
   （按交易日截面：中位数填充 + 1%/99% 分位缩尾 + Z-score 标准化）
3. 行业编码 ind_code_l1/l2 作为 CatBoost 原生 cat_features（不参与截面变换）
4. context.industry_as_feature=true

训练区间 2023-2025，T+5 周期。
"""
import asyncio
import json
import os

from backend.shared.database_manager_v2 import get_session
from sqlalchemy import text

BASE_JOB_ID = "train_20260819100559_9163cb84"
CATALOG_PATH = "/app/config/features/model_training_feature_catalog_v1.json"


def load_all_enabled_features(market: str = "CN") -> list[str]:
    """加载全部 CN-allowed enabled 特征，按分类封顶以避免 OOM。

    每个分类最多取 max_per_category 个（microstructure 等大类截断），
    但保证行业编码 ind_code_l1/l2 一定入选。总特征数控制在 ~300 以下，
    由训练流程的因子筛选（factor_selection）再精选到 250。
    """
    max_per_category = {
        "microstructure": 60,
        "fund_flow": 30,
        "volatility": 40,
        "technical": 25,
        "liquidity": 25,
        "fundamental": 25,
        "momentum": 34,
        "style": 10,
    }
    must_include = {"ind_code_l1", "ind_code_l2"}
    with open(CATALOG_PATH) as f:
        data = json.load(f)
    m = market.upper()
    out: list[str] = []
    seen: set[str] = set()
    for cat in data.get("categories", []):
        cid = cat.get("id", "")
        cap = max_per_category.get(cid, 50)
        collected: list[str] = []
        for feat in cat.get("features", []):
            key = feat.get("key")
            if not key or feat.get("enabled") is not False:
                pass
            else:
                continue
            if not key:
                continue
            markets = feat.get("markets")
            if isinstance(markets, list) and markets:
                declared = [str(x).upper().strip() for x in markets if str(x).strip()]
                if m not in declared:
                    continue
            if key in seen:
                continue
            collected.append(key)
        # 必选特征先加，再按顺序补到 cap
        cat_added = 0
        for key in collected:
            if key in must_include and key not in seen:
                out.append(key)
                seen.add(key)
                cat_added += 1
        for key in collected:
            if key in seen:
                continue
            if cat_added >= cap and key not in must_include:
                continue
            out.append(key)
            seen.add(key)
            cat_added += 1
    return out


async def get_base_payload() -> dict | None:
    async with get_session(read_only=True) as s:
        r = await s.execute(
            text(f"SELECT request_payload FROM admin_training_jobs WHERE id='{BASE_JOB_ID}'")
        )
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

    all_features = load_all_enabled_features(market="CN")
    print(f"目录 enabled 特征数: {len(all_features)}")
    print(f"含行业编码: ind_code_l1={'ind_code_l1' in all_features}, "
          f"ind_code_l2={'ind_code_l2' in all_features}")

    payload = dict(base)
    payload["features"] = all_features
    payload["model_type"] = "catboost"
    payload["job_name"] = "L2_catboost_2023_2025T5_allfeat_industry"
    payload["display_name"] = "L2 CatBoost T+5 全特征+行业编码+截面预处理 (2023-2025)"
    payload["catboost_params"] = {
        "depth": 6,
        "od_wait": 150,
        "l2_leaf_reg": 3.0,
        "learning_rate": 0.03,
        "random_strength": 1.2,
        "bagging_temperature": 0.8,
    }
    payload["ensemble"] = "none"
    # 因子筛选：IC/ICIR 评分，封顶 250 个因子
    payload["factor_selection"] = {
        "method": "ic_icir",
        "n_top": 250,
        "ic_threshold": 0.02,
        "icir_threshold": 0.3,
        "correlation_threshold": 0.85,
    }
    payload["preprocessing"] = {"enabled": True, "winsor": True}
    payload["target_horizon_days"] = 5
    payload["num_boost_round"] = 3000
    payload["early_stopping_rounds"] = 150
    payload["train_start"] = "2023-01-03"
    payload["train_end"] = "2025-12-31"
    payload["valid_start"] = "2026-01-05"
    payload["valid_end"] = "2026-06-30"
    payload["test_start"] = "2026-07-05"
    payload["test_end"] = "2026-08-18"

    # 行业编码作为类别特征
    ctx = dict(payload.get("context") or {})
    ctx["market"] = "CN"
    ctx["industry_as_feature"] = True
    payload["context"] = ctx

    payload.pop("model_id", None)
    payload.pop("run_id", None)

    import httpx
    async with httpx.AsyncClient(base_url="http://localhost:8000/api/v1", timeout=120) as c:
        login = await c.post("/auth/login", json={
            "username": "admin", "password": os.getenv("QM_PASSWORD", ""), "tenant_id": "default"})
        tok = (login.json().get("access_token")
               or login.json().get("data", {}).get("access_token", ""))
        headers = {"Authorization": f"Bearer {tok}"}

        r = await c.post("/models/run-training", json=payload, headers=headers)
        print(f"Status: {r.status_code}")
        print(f"Response: {r.text[:600]}")


asyncio.run(main())
