"""Qlib 数据管理控制台（仅 A 股 CN）。

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
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.services.api.user_app.middleware.auth import require_admin
from backend.shared import quantdb_sync_jobs

logger = logging.getLogger(__name__)

# 构建任务分布式锁：保证同进程/跨 uvicorn worker 同一时刻只跑一个 Qlib 重建。
_BUILD_LOCK = "quantmind:qlib:build_lock"
_LOCK_TTL = 6 * 3600
# 进程级 owner token：进程重启后旧任务的 token 对不上，即判为孤儿。
_TOKEN = f"{os.getpid()}-{uuid4().hex[:8]}"

# Qlib 增量数据依赖的 parquet 数据集（与实际构建源对齐：
#   - daily_backward    后复权 K 线，build_features_bulk 主数据源（OHLCV）
#   - daily_unadjusted  不复权价，乘上乘法复权因子后作为 close_bin
#   - index_daily       指数，build_features 里单独补建
#   - daily_forward     股票池/日历推导的行情分区（保持兼容展示）
QLIB_FEED_DATASETS = ["daily_backward", "daily_unadjusted", "index_daily", "daily_forward"]

router = APIRouter(dependencies=[Depends(require_admin)])


def _now_iso() -> str:
    return datetime.now().isoformat()


def _reclaim_orphan_jobs() -> None:
    """孤儿自愈：把 status=running/cancelling 但归属进程已退出的 qlib 任务标为 failed。

    进程重启后 owner_token 必然变化，旧任务的 token 匹配不上当前进程即为孤儿；
    这类任务不会再有线程回来收尾，必须先回收，避免前端被僵死的 running 卡住、
    以及提交按钮一直 disabled。"""
    try:
        for job in quantdb_sync_jobs.list_jobs():
            jid = str(job.get("job_id", ""))
            if not jid.startswith("qlib-"):
                continue
            if job.get("status") not in ("running", "cancelling"):
                continue
            if job.get("owner_token") == _TOKEN:
                continue
            quantdb_sync_jobs.upsert_job(
                jid,
                status="failed",
                stage="orphaned",
                current="任务因服务重启中断（所属进程已退出）",
                error="orphaned by process restart",
                finished_at=_now_iso(),
            )
    except Exception:  # noqa: BLE001 - 自愈失败不阻断
        logger.warning("qlib 孤儿任务回收失败", exc_info=True)


_PROGRESS_TEXT = {
    0: "排队中",
    5: "准备中",
    10: "构建交易日历/标的",
    30: "正在读取行情 parquet",
    50: "正在写入特征 binary",
    80: "特征构建进行中",
    95: "构建完成，收尾中",
    100: "已完成",
}


def _progress_tick(job_id: str):
    """构造在 build_all 各阶段/批次回调的进度上报闭包：更新 job 进度并续期锁心跳。"""
    last = {"p": -1}

    def _tick(p: int) -> None:
        quantdb_sync_jobs.keep_lock(_BUILD_LOCK, _TOKEN, _LOCK_TTL)
        if p == last["p"]:
            return
        last["p"] = p
        bucket = min((k for k in sorted(_PROGRESS_TEXT) if p >= k), default=0)
        stage = "done" if p >= 95 else "qlib_build"
        quantdb_sync_jobs.upsert_job(
            job_id, progress=p, stage=stage, current=_PROGRESS_TEXT[bucket]
        )

    return _tick


def _release_build_lock() -> None:
    quantdb_sync_jobs.release_lock(_BUILD_LOCK, _TOKEN)


def _resolve_cn_qlib_dir() -> Path:
    """A 股 Qlib 目录统一走 qlib_paths 解析。"""
    from backend.shared.qlib_paths import resolve_qlib_provider_uri

    return Path(resolve_qlib_provider_uri("CN"))


def _latest_parquet_date() -> str | None:
    """daily_forward 最新分区日期（YYYYMMDD）。"""
    from backend.scripts.quantdb_daily_sync import QUANTDB_DATA_DIR

    fwd = QUANTDB_DATA_DIR / "1_kline_data" / "daily_forward"
    if not fwd.is_dir():
        return None
    parts = [p.name for p in fwd.glob("dt=*")]
    if not parts:
        return None
    latest = max(parts)
    return latest[len("dt="):] if latest.startswith("dt=") else None


@router.get("/status", summary="查询 A 股 Qlib 缓存状态")
async def get_qlib_status(
    current_user: dict = Depends(require_admin),
) -> dict[str, Any]:
    """返回 CN Qlib 缓存状态，并对比 parquet 上游给出滞后提示。"""
    _ = current_user
    qlib_dir = _resolve_cn_qlib_dir()

    from backend.services.api.routers.admin.data_status_scanner import _scan_qlib_info
    from backend.shared.qlib_paths import is_qlib_provider_ready

    ready = is_qlib_provider_ready(qlib_dir)
    info = _scan_qlib_info(qlib_dir, "a_share")

    qlib_last = info.get("calendar_last_date")
    parquet_latest = _latest_parquet_date()
    lag_days = None
    lag_hint = None
    lag_status = None
    if qlib_last and parquet_latest:
        try:
            q_last = date.fromisoformat(qlib_last)
            p_last = date.fromisoformat(f"{parquet_latest[:4]}-{parquet_latest[4:6]}-{parquet_latest[6:]}")
            lag_days = max(0, (p_last - q_last).days)
            if lag_days > 0:
                lag_status = "lagging"
                lag_hint = f"Qlib 日历最新 {qlib_last}，上游 parquet 已到 {p_last.isoformat()}，落后约 {lag_days} 天"
            elif q_last > p_last:
                # 旧构建把上游预发布的未来交易日写进了日历（如全年日历写到 12-31），
                # 日历末日期超出真实行情覆盖，提示重建以便裁剪。
                lag_status = "ahead"
                lag_hint = (
                    f"Qlib 日历最新 {qlib_last} 超出上游 parquet 实际覆盖 "
                    f"{p_last.isoformat()}（含预发布未来交易日），请重建 Qlib 以裁剪日历"
                )
            else:
                lag_status = "aligned"
                lag_hint = "Qlib 已与上游 parquet 对齐"
        except Exception:
            lag_hint = None

    return {
        "market": "CN",
        "qlib_dir": str(qlib_dir),
        "ready": ready,
        "qlib_data": info,
        "parquet_latest_date": parquet_latest,
        "lag_days": lag_days,
        "lag_status": lag_status,
        "lag_hint": lag_hint,
        "checked_at": _now_iso(),
    }


def _build_from_parquet_job(job_id: str, incremental: bool) -> None:
    """在系统实际使用的 Qlib 缓存路径上增量/全量重建（不另起新路径）。"""
    tick = _progress_tick(job_id)
    quantdb_sync_jobs.upsert_job(job_id, stage="qlib_build", current="开始构建 Qlib", progress=0)
    try:
        from backend.services.engine.qlib_data_builder import QlibDataBuilder

        # 目标目录取 resolve_qlib_provider_uri：即系统当前实际读取的缓存，
        # 避免 build 默认指到 /data/qlib/cn_data 再重建一份并行缓存从而遮蔽完整数据。
        qlib_dir = _resolve_cn_qlib_dir()
        builder = QlibDataBuilder.for_market("CN", qlib_dir=qlib_dir)
        build_result = builder.build_all(incremental=incremental, progress_cb=tick)
        status = builder.get_status()
        cancel_requested = (quantdb_sync_jobs.get_job(job_id) or {}).get("cancel_requested")
        quantdb_sync_jobs.upsert_job(
            job_id,
            stage="done",
            current="Qlib 构建完成",
            progress=100,
            status="cancelled" if cancel_requested else "completed",
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
    finally:
        _release_build_lock()


def _sync_from_sdk_job(job_id: str) -> None:
    """从本地 parquet 增量重建 Qlib（本地 quantdb 已有独立同步流程，不再走 SDK 下载）。"""
    tick = _progress_tick(job_id)
    quantdb_sync_jobs.upsert_job(job_id, stage="qlib_build", current="开始从本地 parquet 重建 Qlib", progress=5)
    try:
        from backend.services.engine.qlib_data_builder import QlibDataBuilder

        qlib_dir = _resolve_cn_qlib_dir()
        builder = QlibDataBuilder.for_market("CN", qlib_dir=qlib_dir)
        build_result = builder.build_all(incremental=True, progress_cb=tick)
        qlib_status = builder.get_status()
        cancel_requested = (quantdb_sync_jobs.get_job(job_id) or {}).get("cancel_requested")
        quantdb_sync_jobs.upsert_job(
            job_id,
            stage="done",
            current="Qlib 更新完成",
            progress=100,
            status="cancelled" if cancel_requested else "completed",
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
    finally:
        _release_build_lock()


def _launch_job(kind: str, target) -> dict[str, Any]:
    # 先回收进程重启遗留的孤儿 running 任务，再抢锁；锁已被他人持有则拒绝新任务。
    _reclaim_orphan_jobs()
    if not quantdb_sync_jobs.acquire_lock(_BUILD_LOCK, _TOKEN, _LOCK_TTL):
        jobs = [
            j for j in quantdb_sync_jobs.list_jobs()
            if str(j.get("job_id", "")).startswith("qlib-") and j.get("status") in ("running", "cancelling")
        ]
        if jobs:
            raise HTTPException(
                status_code=409,
                detail=f"已有 Qlib 重建任务在执行（{jobs[0].get('job_id')}），请先等待完成或取消",
            )
        raise HTTPException(status_code=409, detail="已有 Qlib 构建任务在运行，请稍后重试")

    job_id = f"qlib-{kind}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
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
        "owner_token": _TOKEN,
        "started_at": _now_iso(),
        "started_by": "admin-ui",
    }
    quantdb_sync_jobs.upsert_job(job_id, **{k: v for k, v in job.items() if k != "job_id"})
    threading.Thread(target=target, args=(job_id,), daemon=True).start()
    return {"job": job}


@router.post("/build", summary="从本地 parquet 构建/重建 Qlib")
async def build_qlib(
    incremental: bool = Query(True, description="是否增量更新（false=全量重建）"),
    current_user: dict = Depends(require_admin),
) -> dict[str, Any]:
    _ = current_user
    return _launch_job("build", lambda jid: _build_from_parquet_job(jid, incremental))


@router.post("/update-from-sdk", summary="通过 QuantDB SDK 同步并更新 Qlib")
async def update_qlib_from_sdk(
    current_user: dict = Depends(require_admin),
) -> dict[str, Any]:
    _ = current_user
    return _launch_job("sdk_update", _sync_from_sdk_job)


@router.get("/jobs", summary="Qlib 任务列表")
async def list_qlib_jobs(
    current_user: dict = Depends(require_admin),
) -> dict[str, Any]:
    _ = current_user
    _reclaim_orphan_jobs()  # 只是查看页面也自愈一次，僵尸任务不被长期挂起
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
