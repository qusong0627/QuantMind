"""quantdb_sync_jobs 僵死任务回收与心跳时间戳的单元测试。

背景：Celery 硬超时（SIGKILL）不会执行任务收尾逻辑，任务记录会一直停在
running，前端据此永久禁用「同步」按钮（见 2026-11-05 factor_dictionary
全量重下被 3600s 硬限制杀死的故障）。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from backend.shared import quantdb_sync_jobs as jobs


class FakeRedis:
    """只实现本模块用到的命令，避免单测依赖真实 Redis。"""

    def __init__(self) -> None:
        self.store: dict[str, dict[bytes, bytes]] = {}

    def keys(self, pattern: str):
        import fnmatch

        return [k.encode() for k in self.store if fnmatch.fnmatch(k, pattern)]

    def scan_iter(self, pattern: str):
        return iter(self.keys(pattern))

    def hset(self, name: str, mapping: dict):
        bucket = self.store.setdefault(name, {})
        for k, v in mapping.items():
            bucket[str(k).encode()] = str(v).encode()

    def hgetall(self, name: str):
        return dict(self.store.get(name, {}))

    def expire(self, name: str, ttl: int) -> None:
        return None

    def get(self, name: str):
        return self.store.get(name, {}).get(b"value")

    def delete(self, name: str) -> None:
        self.store.pop(name, None)


@pytest.fixture()
def fake_redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(jobs, "_redis", lambda: fake)
    return fake


def test_upsert_job_writes_updated_at(fake_redis):
    jobs.upsert_job("qdb-celery-x", status="running")
    job = jobs.get_job("qdb-celery-x")
    assert job is not None
    assert job["updated_at"]
    datetime.fromisoformat(job["updated_at"])


def test_new_celery_job_keeps_task_id(fake_redis):
    job = jobs.new_celery_job(with_pg=True, celery_task_id="task-123")
    stored = jobs.get_job(job["job_id"])
    assert stored["celery_task_id"] == "task-123"
    assert stored["updated_at"]


def test_reclaim_stale_running_job(fake_redis):
    job = jobs.new_celery_job(celery_task_id="task-1")
    stale = (datetime.now() - timedelta(seconds=3600)).isoformat()
    jobs.upsert_job(job["job_id"], updated_at=stale)

    reclaimed = jobs.reclaim_stale_jobs(max_idle_seconds=900)

    assert reclaimed == [job["job_id"]]
    stored = jobs.get_job(job["job_id"])
    assert stored["status"] == "failed"
    assert stored["stage"] == "error"
    assert stored["finished_at"]


def test_reclaim_skips_fresh_running_job(fake_redis):
    job = jobs.new_celery_job(celery_task_id="task-2")
    assert jobs.reclaim_stale_jobs(max_idle_seconds=900) == []
    assert jobs.get_job(job["job_id"])["status"] == "running"


def test_reclaim_skips_finished_job(fake_redis):
    job = jobs.new_celery_job(celery_task_id="task-3")
    stale = (datetime.now() - timedelta(seconds=3600)).isoformat()
    jobs.upsert_job(
        job["job_id"], status="completed", updated_at=stale, finished_at=stale
    )

    assert jobs.reclaim_stale_jobs(max_idle_seconds=900) == []
    assert jobs.get_job(job["job_id"])["status"] == "completed"


def test_progress_cb_tracks_dataset_level_total(fake_redis):
    """dataset_start 要写入数据集级 done/total，前端才不会显示 10/null。"""
    job = jobs.new_celery_job(celery_task_id="task-4")
    job_id = job["job_id"]
    cb = jobs.celery_progress_cb(job_id)

    cb("dataset_start", dataset="features_daily", index=1, total=28)
    stored = jobs.get_job(job_id)
    assert stored["done"] == 0
    assert stored["total"] == 28
    assert stored["current_detail"]["phase"] == "dataset_start"

    cb("dataset_done", dataset="features_daily", synced=120, errors=0)
    stored = jobs.get_job(job_id)
    assert stored["done"] == 1

    cb("dataset_start", dataset="l1_factors", index=2, total=28)
    assert jobs.get_job(job_id)["done"] == 1

    cb("file", dataset="l1_factors", done=1400, total=2144)
    stored = jobs.get_job(job_id)
    assert stored["current_detail"]["phase"] == "downloading"
    assert stored["current_detail"]["done"] == 1400
    assert stored["current_detail"]["total"] == 2144
    assert stored["total"] == 28


def test_progress_records_failed_dataset(fake_redis):
    job = jobs.new_celery_job()
    cb = jobs.celery_progress_cb(job["job_id"])
    cb("dataset_start", dataset="holder_num", index=1, total=1)
    cb("dataset_done", dataset="holder_num", synced=0, errors=1, error="database is locked")
    stored = jobs.get_job(job["job_id"])
    assert stored["done"] == 1
    assert stored["results"] == [{"dataset": "holder_num", "status": "failed", "downloaded": 0, "error": "database is locked"}]
