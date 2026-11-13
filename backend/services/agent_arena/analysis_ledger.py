"""手动分析任务台账（「立即分析」）——api 写入、宿主 worker 消费，单一实现。

台账文件：``<数据根>/logs/analysis_jobs.jsonl``（容器 ``/data/logs`` ↔ 宿主
``<repo>/data/logs``，同一物理文件）。一行为一条任务，状态机
``pending → running → done|failed``；管线忙时回退 ``pending`` 下轮补跑
（消费端见 ``scripts/analysis_trigger_worker.py``）。

并发：api 进程（容器内）与 worker（宿主）是同一内核上的两个进程——flock 跨
进程/跨容器有效，所有读改写一律走 :func:`jobs_lock` 排他锁；写盘 tmp +
``os.replace`` 原子替换。读侧**逐行容错**（写一半的行只丢那一行，不能让整个
队列读成空——旧平台 2026-09-12 踩过：一行损坏 = 按钮永久静默失效）。
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from collections.abc import Iterator

from backend.services.agent_arena.arena_config import logs_dir

#: 台账文件名与锁名（锁是 sidecar：台账本体是 append 数据文件，不借它上锁）
_JOBS_NAME = "analysis_jobs.jsonl"
_LOCK_NAME = ".analysis_jobs.lock"

#: 合法任务类型与状态（与前端 AnalysisJob 契约一致）
VALID_TYPES = ("live", "news")
VALID_STATUS = ("pending", "running", "done", "failed")

#: 终态任务保留期（天）；过期由 worker 清理
KEEP_DAYS = 3

BJ = timezone(timedelta(hours=8))


def jobs_file() -> Path:
    return logs_dir() / _JOBS_NAME


def _lock_file() -> Path:
    return logs_dir() / _LOCK_NAME


def _chmod_shared(path: Path) -> None:
    """台账文件创建后放成双方可写（0o666），失败静默。

    api 进程在容器里以 root 跑、宿主 worker 以普通用户跑，写的是**同一物理
    文件**（/data 挂载）却是两种 uid：谁先创建谁定权限，默认 644 会让另一方
    append 直接 PermissionError（2026-10-08 实测：容器建锁文件后宿主 worker
    起不来）。chmod 在创建者一侧必然成功；非属主侧 EPERM 属预期——那时文件
    已是对手 chmod 过的 666，跳过即可。
    """
    try:
        os.chmod(path, 0o666)
    except OSError:
        pass


def now_bj() -> str:
    return datetime.now(BJ).isoformat(timespec="seconds")


@contextmanager
def jobs_lock() -> Iterator[None]:
    """排他 flock（跨 api 进程与宿主 worker；锁文件 stay-open 自愈）。"""
    lock = _lock_file()
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as f:
        _chmod_shared(lock)
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def load_jobs() -> list[dict[str, Any]]:
    """读全部任务行；**逐行容错**——坏行跳过并计数（不静默、不整表报废）。"""
    path = jobs_file()
    if not path.is_file():
        return []
    try:
        # errors="replace"：写侧非原子 append 撕裂多字节字符时，严格解码会整文件炸
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    bad = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            continue
        if isinstance(row, dict):
            rows.append(row)
        else:
            bad += 1
    if bad:
        print(f"⚠️ analysis_jobs 有 {bad} 行无法解析，已跳过（其余 {len(rows)} 条照常）")
    return rows


def _save_locked(rows: list[dict[str, Any]]) -> None:
    """整表重写（调用方须已持锁）。tmp + os.replace 原子替换。"""
    path = jobs_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)
    _chmod_shared(path)


def _append_locked(job: dict[str, Any]) -> None:
    """单行追加（调用方须已持锁）；单次 write 单行，撕裂行由读侧兜底。"""
    path = jobs_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(job, ensure_ascii=False) + "\n")
    _chmod_shared(path)


def new_job(job_type: str, agents: str | list[str]) -> dict[str, Any]:
    return {
        "id": f"job-{int(time.time() * 1000)}",
        "ts": now_bj(),
        "type": job_type,
        "agents": agents,
        "status": "pending",
        "note": "",
    }


def trigger_job(job_type: str, agents: str | list[str]) -> dict[str, Any]:
    """api 侧入口：锁内去重（同 type+agents 的 pending 直接返回，防连点堆积）

    → 追加新任务行并返回。``agents`` 已由路由层校验/归一化。
    """
    with jobs_lock():
        for row in load_jobs():
            if (
                row.get("status") == "pending"
                and row.get("type") == job_type
                and row.get("agents") == agents
            ):
                return row
        job = new_job(job_type, agents)
        _append_locked(job)
        return job


def update_job(job_id: str, **fields: Any) -> bool:
    """按 id 更新字段（锁内重读→改→整表写回，不覆盖并发追加的新行）。"""
    with jobs_lock():
        rows = load_jobs()
        hit = False
        for row in rows:
            if row.get("id") == job_id:
                row.update(fields)
                hit = True
        if hit:
            _save_locked(rows)
        return hit


def cleanup_old() -> int:
    """清理 3 天前的终态任务；返回删除条数（锁内读改写）。"""
    cutoff = (datetime.now(BJ) - timedelta(days=KEEP_DAYS)).isoformat()
    with jobs_lock():
        rows = load_jobs()
        keep = [
            r
            for r in rows
            if r.get("status") in ("pending", "running")
            or (r.get("done_ts") or "") >= cutoff
        ]
        if len(keep) != len(rows):
            _save_locked(keep)
        return len(rows) - len(keep)
