"""Qlib 数据管理控制台（CN / HK / US / CRYPTO / FUTURES 五市场）。

提供 Qlib 缓存的状态查询、从本地 parquet 重建、以及通过 QuantDB SDK
先同步 parquet 再重建 Qlib 的一键更新。任务进度复用 Redis 基建
`backend.shared.quantdb_sync_jobs`（与 quantdb 同步任务共用存储）。

路由前缀：/admin/data-platform/qlib
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.services.api.user_app.middleware.auth import require_admin
from backend.shared import quantdb_sync_jobs

logger = logging.getLogger(__name__)

# Qlib 增量数据依赖的 parquet 数据集（与实际构建源对齐：
#   - daily_backward    后复权 K 线，build_features_bulk 主数据源（OHLCV）
#   - daily_unadjusted  不复权价，乘上乘法复权因子后作为 close_bin
#   - index_daily       指数，build_features 里单独补建
#   - daily_forward     股票池/日历推导的行情分区（保持兼容展示）
QLIB_FEED_DATASETS = ["daily_backward", "daily_unadjusted", "index_daily", "daily_forward"]

router = APIRouter(dependencies=[Depends(require_admin)])


def _now_iso() -> str:
    return datetime.now().isoformat()


def _resolve_qlib_dir(market: str) -> Path:
    """按市场解析 Qlib 缓存目录（统一走 qlib_paths：/data/qlib/{market}_data 优先，
    回退各市场 .qlib_cache/。均与实际读取路径一致）。"""
    from backend.shared.qlib_paths import resolve_qlib_provider_uri

    return Path(resolve_qlib_provider_uri(market))


_MARKET_DATA_DIR_ENV = {
    "CN": "QM_QUANTDB_DATA_DIR",
    "HK": "QM_QUANTHK_DATA_DIR",
    "US": "QM_QUANTUS_DATA_DIR",
    "CRYPTO": "QM_QUANTBC_DATA_DIR",
    "FUTURES": "QM_QUANTFUTURES_DATA_DIR",
}


def _latest_parquet_date(market: str = "CN") -> str | None:
    """该市场日线 daily_forward 最新分区日期（YYYYMMDD）。"""
    market = market.upper()
    if market == "CN":
        from backend.scripts.quantdb_daily_sync import QUANTDB_DATA_DIR

        data_dir = QUANTDB_DATA_DIR
    else:
        from backend.services.engine.data_platform import quanthk_hub, quantus_hub, quantbc_hub, quantfutures_hub

        data_dir = {
            "HK": quanthk_hub._resolve_quanthk_data_dir(),
            "US": quantus_hub._resolve_quantus_data_dir(),
            "CRYPTO": quantbc_hub._resolve_quantbc_data_dir(),
            "FUTURES": quantfutures_hub._resolve_quantfutures_data_dir(),
        }.get(market)
    if not data_dir:
        return None
    fwd = Path(data_dir) / "1_kline_data" / "daily_forward"
    if not fwd.is_dir():
        return None
    parts = [p.name for p in fwd.glob("dt=*")]
    if not parts:
        return None
    latest = max(parts)
    return latest[len("dt="):] if latest.startswith("dt=") else None


def _market_enabled(market: str) -> bool:
    """市场是否启用（CRYPTO 受 ENABLE_CRYPTO 屏蔽、FUTURES 受 ENABLE_FUTURES 屏蔽）。"""
    if market == "CRYPTO":
        return os.getenv("ENABLE_CRYPTO", "false").strip().lower() in {"1", "true", "yes"}
    if market == "FUTURES":
        return os.getenv("ENABLE_FUTURES", "true").strip().lower() in {"1", "true", "yes"}
    return True


_MARKET_SCAN_IDS = {
    "CN": "a_share", "HK": "hong_kong", "US": "us_stock",
    "CRYPTO": "crypto", "FUTURES": "futures",
}


@router.get("/status", summary="查询指定市场 Qlib 缓存状态")
async def get_qlib_status(
    market: str = Query("CN", description="市场: CN/HK/US/CRYPTO/FUTURES"),
    current_user: dict = Depends(require_admin),
) -> dict[str, Any]:
    """返回指定市场 Qlib 缓存状态，并对比 parquet 上游给出滞后提示。"""
    _ = current_user
    market = market.upper()
    qlib_dir = _resolve_qlib_dir(market)

    from backend.services.api.routers.admin.data_status_scanner import _scan_qlib_info
    from backend.shared.qlib_paths import is_qlib_provider_ready

    ready = is_qlib_provider_ready(qlib_dir)
    info = _scan_qlib_info(qlib_dir, _MARKET_SCAN_IDS.get(market, market.lower()))

    qlib_last = info.get("calendar_last_date")
    parquet_latest = _latest_parquet_date(market)
    lag_days = None
    lag_hint = None
    if qlib_last and parquet_latest:
        try:
            q_last = date.fromisoformat(qlib_last)
            p_last = date.fromisoformat(f"{parquet_latest[:4]}-{parquet_latest[4:6]}-{parquet_latest[6:]}")
            lag_days = max(0, (p_last - q_last).days)
            if lag_days > 0:
                lag_hint = f"Qlib 日历最新 {qlib_last}，上游 parquet 已到 {p_last.isoformat()}，落后约 {lag_days} 天"
            else:
                lag_hint = "Qlib 已与上游 parquet 对齐"
        except Exception:
            lag_hint = None

    return {
        "market": market,
        "enabled": _market_enabled(market),
        "qlib_dir": str(qlib_dir),
        "ready": ready,
        "qlib_data": info,
        "parquet_latest_date": parquet_latest,
        "lag_days": lag_days,
        "lag_hint": lag_hint,
        "checked_at": _now_iso(),
    }


def _build_from_parquet_job(job_id: str, market: str, incremental: bool) -> None:
    """在系统实际使用的 Qlib 缓存路径上增量/全量重建（不另起新路径）。"""
    quantdb_sync_jobs.upsert_job(job_id, stage="qlib_build", current=f"[{market}] 开始构建 Qlib", progress=0)
    try:
        from backend.services.engine.qlib_data_builder import QlibDataBuilder

        # 目标目录取 resolve_qlib_provider_uri：即系统当前实际读取的缓存，
        # 避免 build 默认指到别处再重建一份并行缓存从而遮蔽完整数据。
        qlib_dir = _resolve_qlib_dir(market)
        builder = QlibDataBuilder.for_market(market, qlib_dir=qlib_dir)
        build_result = builder.build_all(incremental=incremental)
        status = builder.get_status()
        quantdb_sync_jobs.upsert_job(
            job_id,
            stage="done",
            current="Qlib 构建完成",
            progress=100,
            status="completed",
            result={"qlib_dir": str(qlib_dir), "build": build_result, "status": status},
            finished_at=_now_iso(),
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("qlib job %s build failed: %s", job_id, exc, exc_info=True)
        quantdb_sync_jobs.upsert_job(
            job_id,
            stage="error",
            status="failed",
            error=str(exc),
            finished_at=_now_iso(),
        )


def _sync_from_sdk_job(job_id: str, market: str) -> None:
    """从本地 parquet 增量重建 Qlib（本地数据已有独立同步流程，不再走 SDK 下载）。"""
    quantdb_sync_jobs.upsert_job(job_id, stage="qlib_build", current=f"[{market}] 开始从本地 parquet 重建 Qlib", progress=5)
    try:
        from backend.services.engine.qlib_data_builder import QlibDataBuilder

        qlib_dir = _resolve_qlib_dir(market)
        builder = QlibDataBuilder.for_market(market, qlib_dir=qlib_dir)
        build_result = builder.build_all(incremental=True)
        qlib_status = builder.get_status()
        quantdb_sync_jobs.upsert_job(
            job_id,
            stage="done",
            current="Qlib 更新完成",
            progress=100,
            status="completed",
            result={
                "qlib_dir": str(qlib_dir),
                "build": build_result,
                "status": qlib_status,
            },
            finished_at=_now_iso(),
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("qlib job %s update failed: %s", job_id, exc, exc_info=True)
        quantdb_sync_jobs.upsert_job(
            job_id, stage="error", status="failed", error=str(exc), finished_at=_now_iso()
        )


def _launch_job(kind: str, market: str, target) -> dict[str, Any]:
    job_id = f"qlib-{kind}-{market}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    job = {
        "job_id": job_id,
        "kind": kind,
        "status": "running",
        "stage": "pending",
        "progress": 0,
        "current": "排队中",
        "datasets": QLIB_FEED_DATASETS if kind == "sdk_update" else None,
        "total": None,
        "done": 0,
        "cancel_requested": False,
        "started_at": _now_iso(),
        "started_by": "admin-ui",
    }
    quantdb_sync_jobs.upsert_job(job_id, **{k: v for k, v in job.items() if k != "job_id"})
    threading.Thread(target=target, args=(job_id,), daemon=True).start()
    return {"job": job}


@router.post("/build", summary="从本地 parquet 构建/重建 Qlib")
async def build_qlib(
    market: str = Query("CN", description="市场: CN/HK/US/CRYPTO/FUTURES"),
    incremental: bool = Query(True, description="是否增量更新（false=全量重建）"),
    current_user: dict = Depends(require_admin),
) -> dict[str, Any]:
    _ = current_user
    market = market.upper()
    return _launch_job("build", market, lambda jid: _build_from_parquet_job(jid, market, incremental))


@router.post("/update-from-sdk", summary="从本地 parquet 增量更新 Qlib")
async def update_qlib_from_sdk(
    market: str = Query("CN", description="市场: CN/HK/US/CRYPTO/FUTURES"),
    current_user: dict = Depends(require_admin),
) -> dict[str, Any]:
    _ = current_user
    market = market.upper()
    return _launch_job("sdk_update", market, lambda jid: _sync_from_sdk_job(jid, market))


@router.get("/jobs", summary="Qlib 任务列表")
async def list_qlib_jobs(
    current_user: dict = Depends(require_admin),
) -> dict[str, Any]:
    _ = current_user
    jobs = [j for j in quantdb_sync_jobs.list_jobs() if str(j.get("job_id", "")).startswith("qlib-")]
    jobs.sort(key=lambda j: str(j.get("started_at", "")), reverse=True)
    return {"jobs": jobs, "timestamp": _now_iso()}


@router.get("/jobs/{job_id}", summary="Qlib 任务进度")
async def get_qlib_job(
    job_id: str,
    current_user: dict = Depends(require_admin),
):
    _ = current_user
    job = quantdb_sync_jobs.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return {"job": job}


@router.post("/jobs/{job_id}/cancel", summary="取消 Qlib 任务")
async def cancel_qlib_job(
    job_id: str,
    current_user: dict = Depends(require_admin),
) -> dict[str, Any]:
    _ = current_user
    job = quantdb_sync_jobs.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if job.get("status") in ("completed", "failed", "cancelled"):
        return {"job_id": job_id, "status": job.get("status"), "message": "任务已结束"}
    quantdb_sync_jobs.upsert_job(
        job_id, cancel_requested=True, status="cancelling", current="取消请求已提交"
    )
    return {"job_id": job_id, "status": "cancelling", "message": "已提交取消请求"}
