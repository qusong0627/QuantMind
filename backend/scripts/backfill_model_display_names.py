"""回填模型展示名：metadata 缺 display_name/model_name 时用可读兜底并落库/落盘。

背景（2026-11-24）：美股一批模型 metadata 只有 ``job_name='unnamed'``，模型列表/
实盘选择器只剩长 model_id。前端已加兜底（``buildFallbackModelName``），本脚本把
同一口径写回 DB 与模型目录 ``metadata.json``，让所有消费面（预检、成绩单、QQ）
都能拿到可读名字。

用法（容器内，默认 dry-run）：
    python backend/scripts/backfill_model_display_names.py
    python backend/scripts/backfill_model_display_names.py --apply
    python backend/scripts/backfill_model_display_names.py --market US --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text

from backend.shared.database_manager_v2 import get_session

logger = logging.getLogger("backfill_model_names")

MARKET_LABELS = {
    "CN": "A股",
    "HK": "港股",
    "US": "美股",
    "CRYPTO": "加密",
    "FUTURES": "期货",
    "CUSTOM": "自定义",
}
ALGO_LABELS = {
    "lightgbm": "LightGBM",
    "lgb": "LightGBM",
    "xgboost": "XGBoost",
    "xgb": "XGBoost",
    "catboost": "CatBoost",
    "linear": "Linear",
    "gru": "GRU",
    "lstm": "LSTM",
    "alstm": "ALSTM",
    "transformer": "Transformer",
    "tcn": "TCN",
    "tabnet": "TabNet",
    "nativetft": "TFT",
    "tft": "TFT",
    "random_forest": "RandomForest",
    "rf": "RandomForest",
    "mlp": "MLP",
    "ensemble": "融合",
}

ELIGIBLE_SQL = """
SELECT tenant_id, user_id, model_id, storage_path, metadata_json
FROM qm_user_models
WHERE COALESCE(NULLIF(metadata_json->>'display_name', ''),
               NULLIF(metadata_json->>'model_name', ''), '') = ''
  AND status <> 'archived'
ORDER BY updated_at DESC NULLS LAST
"""


def _algo_label(meta: dict[str, Any]) -> str:
    raw = (
        str(meta.get("model_type") or meta.get("primary_model_type") or "")
        .strip()
        .lower()
    )
    if str(meta.get("is_ensemble") or "").lower() == "true" or raw == "ensemble":
        return "融合"
    if raw in ALGO_LABELS:
        return ALGO_LABELS[raw]
    # 子串匹配按 key 长度倒序：alstm 必须先于 lstm 命中
    for key in sorted(ALGO_LABELS, key=len, reverse=True):
        if key in raw:
            return ALGO_LABELS[key]
    return raw.upper() if raw else "模型"


def build_name(model_id: str, meta: dict[str, Any]) -> str:
    """与前端 ``buildFallbackModelName`` 同口径（市场 · 算法 · T+N · 特征数 · 短 ID）。"""
    if not str(model_id or "").strip():
        return ""
    job_name = str(meta.get("job_name") or "").strip()
    if job_name and job_name.lower() != "unnamed":
        # 同一 job 下多个算法（如美股一批 14 个模型）需要算法后缀区分
        return f"{job_name} · {_algo_label(meta)}"
    context = meta.get("context") if isinstance(meta.get("context"), dict) else {}
    market = str(meta.get("market") or context.get("market") or "").upper()
    parts = [MARKET_LABELS.get(market, market), _algo_label(meta)]
    try:
        horizon = int(meta.get("target_horizon_days") or meta.get("horizon_days") or 0)
    except (TypeError, ValueError):
        horizon = 0
    if horizon > 0:
        parts.append(f"T+{horizon}")
    try:
        features = int(
            meta.get("feature_count")
            or (len(meta["features"]) if isinstance(meta.get("features"), list) else 0)
        )
    except (TypeError, ValueError):
        features = 0
    if features > 0:
        parts.append(f"{features}特征")
    suffix = [p for p in str(model_id).split("_") if p][-1:] or []
    if suffix:
        parts.append(suffix[0][:6])
    return " · ".join(part for part in parts if part) or model_id


def _write_disk_metadata(storage_path: str, name: str) -> bool:
    raw = str(storage_path or "").strip()
    if not raw:
        return False
    path = Path(raw) / "metadata.json"
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    payload["display_name"] = name
    payload["model_name"] = name
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return True


async def run(*, apply: bool, market: str | None, limit: int) -> dict:
    async with get_session(read_only=True) as session:
        rows = (await session.execute(text(ELIGIBLE_SQL))).mappings().all()
    plans: list[dict[str, Any]] = []
    for row in rows:
        meta = row.get("metadata_json")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except json.JSONDecodeError:
                meta = {}
        if not isinstance(meta, dict):
            meta = {}
        row_market = str(
            meta.get("market") or (meta.get("context") or {}).get("market") or ""
        ).upper()
        if market and row_market != market.upper():
            continue
        name = build_name(str(row.get("model_id") or ""), meta)
        if not name:
            continue
        plans.append({**dict(row), "metadata": meta, "name": name})
        if len(plans) >= limit:
            break

    print(f"待回填 {len(plans)} 个模型（apply={apply}）")
    for item in plans:
        print(f"  {item['model_id']} -> {item['name']}")

    if not apply or not plans:
        return {"candidates": len(plans), "applied": 0}

    backup_path = (
        Path("/app/logs") / f"backfill_model_names_{datetime.now():%Y%m%d_%H%M%S}.json"
    )
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path.write_text(
        json.dumps(
            [
                {
                    "tenant_id": p["tenant_id"],
                    "user_id": p["user_id"],
                    "model_id": p["model_id"],
                    "metadata_json": p["metadata"],
                }
                for p in plans
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    applied = 0
    async with get_session() as session:
        for item in plans:
            meta = dict(item["metadata"])
            meta["display_name"] = item["name"]
            meta["model_name"] = item["name"]
            await session.execute(
                text(
                    "UPDATE qm_user_models SET metadata_json = CAST(:meta AS JSONB), "
                    "updated_at = now() WHERE tenant_id = :tid AND user_id = :uid AND model_id = :mid"
                ),
                {
                    "meta": json.dumps(meta, ensure_ascii=False),
                    "tid": item["tenant_id"],
                    "uid": item["user_id"],
                    "mid": item["model_id"],
                },
            )
            _write_disk_metadata(str(item.get("storage_path") or ""), item["name"])
            applied += 1
        await session.commit()
    print(f"已回填 {applied} 个；备份 {backup_path}")
    return {"candidates": len(plans), "applied": applied, "backup": str(backup_path)}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(
        description="回填模型展示名（缺 display_name/model_name 时）"
    )
    ap.add_argument("--apply", action="store_true", help="写库/写盘（默认 dry-run）")
    ap.add_argument("--market", default=None, help="只处理某市场（CN/HK/US/...）")
    ap.add_argument("--limit", type=int, default=500)
    args = ap.parse_args()
    asyncio.run(run(apply=args.apply, market=args.market, limit=args.limit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
