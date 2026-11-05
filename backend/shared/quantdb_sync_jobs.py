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
        keys = r.keys(KEY_PREFIX + "*")
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


def new_celery_job(datasets: list[str] | None = None, with_pg: bool = False, with_qlib: bool = False) -> dict:
    """创建一个定时同步任务记录并写入 Redis，返回 job dict 与回调。"""
    job_id = f"qdb-celery-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
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
        "started_at": _now_iso(),
        "started_by": "celery-scheduler",
    }
    upsert_job(job["job_id"], **{k: v for k, v in job.items() if k != "job_id"})
    return job


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
