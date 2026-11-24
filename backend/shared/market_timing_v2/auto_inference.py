"""机构择时 v2：信号日落后时自动补推理。

只在盘后快照任务/管理员手动刷新里调用；推理走平台现有
``InferenceRouterService.run_daily_inference_script``（与「一键推理」同一路径），
成功后由 ``merge_signals_into_pred`` 回写 pred.parquet / pred_daily。

安全边界：
- 默认最多补 5 个交易日；缺口更大时跳过并在快照里标记，避免误触发长区间补推。
- 模型/数据不可用时不重试死循环，只记录失败，快照继续用现有 TFT 信号。
"""

from __future__ import annotations

import logging
import os
from typing import Any

import pandas as pd

from .history import build_market_history
from .signals import TFT_MODEL_ID, build_snapshot, load_tft_history

logger = logging.getLogger(__name__)

TENANT_ID = os.environ.get("MARKET_TIMING_TENANT_ID", "default")
USER_ID = os.environ.get("MARKET_TIMING_USER_ID", "10000001")
AUTO_INFERENCE_ENABLED = (
    os.environ.get("MARKET_TIMING_AUTO_INFERENCE", "true").lower() == "true"
)
MAX_AUTO_DAYS = int(os.environ.get("MARKET_TIMING_MAX_AUTO_INFERENCE_DAYS", "5"))


def missing_signal_dates(
    history: pd.DataFrame,
    tft_latest: pd.Timestamp | None,
    max_days: int = MAX_AUTO_DAYS,
) -> list[str]:
    """TFT 信号日之后、行情日之前的交易日；超过上限返回空（不自动长区间补推）。"""
    if tft_latest is None or pd.isna(tft_latest):
        return []
    dates = sorted(
        {
            pd.Timestamp(d).strftime("%Y-%m-%d")
            for d in history["date"].dropna().unique()
            if pd.Timestamp(d) > pd.Timestamp(tft_latest)
        }
    )
    if not dates or len(dates) > max_days:
        return []
    return dates


def ensure_tft_inference(history: pd.DataFrame, tft: pd.DataFrame) -> dict[str, Any]:
    """把 TFT pred 补到行情最新日；返回可审计的补推理摘要。"""
    latest_tft = pd.to_datetime(tft["date"]).max() if not tft.empty else None
    data_as_of = pd.to_datetime(history["date"]).max()
    dates = missing_signal_dates(history, latest_tft)
    info: dict[str, Any] = {
        "enabled": AUTO_INFERENCE_ENABLED,
        "tft_as_of_before": latest_tft.strftime("%Y-%m-%d")
        if latest_tft is not None
        else None,
        "data_as_of": data_as_of.strftime("%Y-%m-%d") if pd.notna(data_as_of) else None,
        "missing_dates": dates,
        "attempted": 0,
        "succeeded": 0,
        "failed": [],
        "status": "current",
    }
    if not AUTO_INFERENCE_ENABLED:
        info["status"] = "disabled"
        return info
    if latest_tft is None or pd.isna(latest_tft):
        info["status"] = "no_tft_signal"
        return info
    if not dates:
        # 可能已经最新，也可能缺口超过上限；区分一下
        all_missing = [
            pd.Timestamp(d).strftime("%Y-%m-%d")
            for d in sorted(set(history["date"].dropna().unique()))
            if pd.Timestamp(d) > pd.Timestamp(latest_tft)
        ]
        info["status"] = (
            "gap_too_large" if len(all_missing) > MAX_AUTO_DAYS else "current"
        )
        if all_missing:
            info["missing_dates"] = all_missing
        return info

    try:
        from backend.services.engine.inference.router_service import (
            InferenceRouterService,
        )
        from backend.shared.model_registry import model_registry_service

        resolved = model_registry_service.resolve_effective_model_sync(
            tenant_id=TENANT_ID,
            user_id=USER_ID,
            model_id=TFT_MODEL_ID,
            market="CN",
        )
        if not str(resolved.get("storage_path") or "").strip():
            info["status"] = "model_unresolved"
            return info
        router = InferenceRouterService()
        for day in dates:
            info["attempted"] += 1
            try:
                result = router.run_daily_inference_script(
                    date=day,
                    tenant_id=TENANT_ID,
                    user_id=USER_ID,
                    model_id=TFT_MODEL_ID,
                    resolved_model=resolved,
                    market="CN",
                    persist=True,
                )
                if result.success:
                    info["succeeded"] += 1
                else:
                    info["failed"].append(
                        {
                            "date": day,
                            "reason": str(result.fallback_reason or "inference_failed")[
                                :200
                            ],
                        }
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[market-timing-v2] auto inference %s failed: %s", day, exc
                )
                info["failed"].append({"date": day, "reason": str(exc)[:200]})
        info["status"] = "completed" if info["succeeded"] else "failed"
        return info
    except Exception as exc:  # noqa: BLE001
        logger.exception("[market-timing-v2] auto inference setup failed: %s", exc)
        info["status"] = "error"
        info["error"] = str(exc)[:300]
        return info


def refresh_snapshot(*, force: bool = True) -> dict[str, Any]:
    """构建行情面板 → 需要时补推理 TFT → 生成快照（单次完整刷新）。"""
    history = build_market_history(force=force)
    tft = load_tft_history()
    info = ensure_tft_inference(history, tft)
    snapshot = build_snapshot(force=False, history=history, inference_info=info)
    logger.info(
        "[market-timing-v2] inference=%s attempted=%s succeeded=%s tft=%s",
        info.get("status"),
        info.get("attempted"),
        info.get("succeeded"),
        snapshot.get("tft_as_of"),
    )
    return snapshot


if __name__ == "__main__":
    snap = refresh_snapshot(force=True)
    print(
        {
            "status": snap.get("status"),
            "tft_as_of": snap.get("tft_as_of"),
            "inference": snap.get("inference"),
        }
    )
