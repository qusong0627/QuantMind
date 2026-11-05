"""Redis-backed QuantDB 同步任务进度存储。

API 控制台的 `_jobs` 是 API 进程内存；Celery worker 是独立进程，无法直接写它。
定时同步（Celery）通过本模块把进度写入 Redis，API 控制台读取 Redis 合并展示。
"""
from __future__ import annotations

import json
import os
from datetime import datetime

KEY_PREFIX = "quantmind:quantdb:job:"
TTL = 6 * 3600  # 任务记录保留 6 小时

# 僵死判定：任务心跳（每次 upsert_job 都会刷新 updated_at）超过该秒数即认为
# worker 已被杀 / 进程已退出，避免任务记录永远停在 running 挡住下一次同步。
STALE_IDLE_SECONDS = int(os.getenv("QUANTDB_SYNC_STALE_SECONDS", "900"))
CELERY_JOB_PREFIX = "qdb-celery-"
ACTIVE_STATUSES = ("running", "cancelling")


def _redis():
    import redis
    url = os.getenv("REDIS_URL") or "redis://redis:6379/0"
    return redis.from_url(url, socket_timeout=3)


# ---------------------------------------------------------------------------
# 任务级分布式锁（构建类任务：Qlib 重建等）
# 用 owner token 支持进程重启后的安全回收与防并发。
# ---------------------------------------------------------------------------

def acquire_lock(key: str, token: str, ttl: int = TTL) -> bool:
    """NX 抢占锁。成功返回 True，已被他人持有返回 False。"""
    r = _redis()
    try:
        return bool(r.set(key, token, nx=True, ex=ttl))
    except Exception:
        return False


def keep_lock(key: str, token: str, ttl: int = TTL) -> None:
    """续期锁，仅当锁仍归属本 token 时刷新 TTL（心跳）。"""
    r = _redis()
    try:
        if r.get(key) == token.encode("utf-8"):
            r.expire(key, ttl)
    except Exception:
        pass


def release_lock(key: str, token: str) -> None:
    """释放锁，仅当锁仍归属本 token 时删除（CAS，避免误删他人刚抢到的锁）。"""
    r = _redis()
    try:
        cas = r.register_script(
            "if redis.call('get', KEYS[1]) == ARGV[1] then "
            "return redis.call('del', KEYS[1]) else return 0 end"
        )
        cas(keys=[key], args=[token])
    except Exception:  # noqa: BLE001 - 释放失败由 TTL 兜底
        try:
            if r.get(key) == token.encode("utf-8"):
                r.delete(key)
        except Exception:
            pass


def _now_iso() -> str:
    return datetime.now().isoformat()


def _encode(value) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def upsert_job(job_id: str, **fields) -> None:
    data = dict(fields)
    data.setdefault("job_id", job_id)
    # 心跳时间：任何进度/状态写入都算一次存活证明，供 reclaim_stale_jobs 判活。
    data["updated_at"] = fields.get("updated_at") or _now_iso()
    r = _redis()
    try:
        r.hset(KEY_PREFIX + job_id, mapping={k: _encode(v) for k, v in data.items()})
        r.expire(KEY_PREFIX + job_id, TTL)
    except Exception:
        pass


def get_job(job_id: str) -> dict | None:
    r = _redis()
    try:
        raw = r.hgetall(KEY_PREFIX + job_id)
    except Exception:
        return None
    if not raw:
        return None
    job: dict = {}
    for k, v in raw.items():
        k = k.decode("utf-8", "ignore")
        v = v.decode("utf-8", "ignore")
        try:
            job[k] = json.loads(v)
        except Exception:
            job[k] = v
    return job


def list_jobs() -> list[dict]:
    r = _redis()
    try:
        keys = list(r.scan_iter(KEY_PREFIX + "*"))
    except Exception:
        return []
    jobs = []
    for key in keys:
        try:
            job_id = key.decode("utf-8", "ignore").split(":", -1)[-1]
        except Exception:
            continue
        job = get_job(job_id)
        if job:
            jobs.append(job)
    return jobs


def new_celery_job(
    datasets: list[str] | None = None,
    with_pg: bool = False,
    with_qlib: bool = False,
    celery_task_id: str | None = None,
) -> dict:
    """创建一个定时同步任务记录并写入 Redis，返回 job dict 与回调。"""
    job_id = f"{CELERY_JOB_PREFIX}{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    job = {
        "job_id": job_id,
        "status": "running",
        "stage": "sync_parquet",
        "datasets": list(datasets or []),
        "total": len(datasets) if datasets else None,
        "done": 0,
        "current": "等待开始",
        "current_detail": None,
        "results": [],
        "with_pg": with_pg,
        "with_qlib": with_qlib,
        "cancel_requested": False,
        "celery_task_id": celery_task_id,
        "started_at": _now_iso(),
        "started_by": "celery-scheduler",
    }
    upsert_job(job["job_id"], **{k: v for k, v in job.items() if k != "job_id"})
    return job


def heartbeat(job_id: str) -> None:
    """刷新任务存活时间 + 记录保留期（供长任务后台心跳线程调用）。"""
    upsert_job(job_id, heartbeat_at=_now_iso())


def _parse_iso(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def reclaim_stale_jobs(
    max_idle_seconds: int | None = None, prefix: str = CELERY_JOB_PREFIX
) -> list[str]:
    """把心跳超时的僵死任务标记为 failed，返回被回收的 job_id 列表。

    Celery 硬超时（SIGKILL）不会走任务里的任何收尾逻辑，任务记录会一直挂着
    running，前端据此禁用同步按钮。判定依据是 updated_at（每次 upsert 刷新）
    而不是 created_at，所以正常跑的长任务不会被误杀。
    """
    idle = STALE_IDLE_SECONDS if max_idle_seconds is None else max_idle_seconds
    reclaimed: list[str] = []
    r = _redis()
    try:
        # 用 scan_iter 而不是 keys()：该函数在 /sync-jobs 每次轮询都会跑，
        # 不能让 O(整个 keyspace) 的 KEYS 阻塞 Redis。
        keys = list(r.scan_iter(KEY_PREFIX + prefix + "*"))
    except Exception:
        return reclaimed
    now = datetime.now()
    for key in keys:
        job_id = key.decode("utf-8", "ignore").split(":")[-1]
        job = get_job(job_id)
        if not job or job.get("status") not in ACTIVE_STATUSES:
            continue
        last = _parse_iso(job.get("updated_at")) or _parse_iso(job.get("started_at"))
        if last is None:
            continue
        idle_seconds = (now - last).total_seconds()
        if idle_seconds <= idle:
            continue
        logger_msg = (
            f"任务心跳中断 {int(idle_seconds)}s（>{idle}s），判定 worker 已退出/被杀"
        )
        upsert_job(
            job_id,
            status="failed",
            stage="error",
            current="任务已中断（心跳超时，worker 可能被超时杀死）",
            error=logger_msg,
            finished_at=_now_iso(),
            cancel_requested=False,
        )
        reclaimed.append(job_id)
    return reclaimed


def celery_progress_cb(job_id: str):
    """构建写入 Redis 的进度回调（配合 quantdb_daily_sync 的 progress_cb 使用）。"""

    def _cb(event: str, **kw) -> None:
        ds = kw.get("dataset")
        fields: dict = {}
        if event == "dataset_start":
            fields = {
                "current": f"{ds} 开始同步",
                "current_detail": {"dataset": ds, "done": 0, "total": kw.get("total"), "phase": "dataset_start"},
            }
        elif event == "file":
            fields = {
                "current": f"{ds} 下载 {kw.get('done')}/{kw.get('total')}",
                "current_detail": {"dataset": ds, "done": kw.get("done"), "total": kw.get("total"), "phase": "downloading"},
            }
        elif event == "dataset_done":
            fields = {
                "done": (get_job(job_id) or {}).get("done", 0) + 1,
                "current": f"{ds} 完成 (同步 {kw.get('synced', 0)})",
                "current_detail": {"dataset": ds, "done": kw.get("synced", 0), "total": kw.get("total"), "phase": "dataset_done"},
            }
        if fields:
            upsert_job(job_id, **fields)

    return _cb
