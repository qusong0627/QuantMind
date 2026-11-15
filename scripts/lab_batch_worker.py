#!/usr/bin/env python3
"""批量回测宿主 worker：消费 ``<数据根>/lab_batch/queue/*.json``。

与 Pine 转写 worker 同构：容器里的 API 只负责入队（容器 Python 3.10 装不了
pynecore），本脚本在宿主用 ``pine_lab/.venv`` 跑 ``scripts/lab_batch_backtest.py``，
把进度写回 ``status/<run_id>.json``，界面轮询这个文件。

cron（每分钟；跑批本身可能几分钟，flock 保证不重叠）::

    * * * * * cd <repo> && pine_lab/.venv/bin/python scripts/lab_batch_worker.py \\
        >> logs/lab_batch_worker.log 2>&1

同时间只跑一个批次：队列里有多条时按文件名（时间戳）先来先跑，下一条等下一轮 cron。
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.services.agent_arena import lab_batch as lb  # noqa: E402

CLI = ROOT / "scripts" / "lab_batch_backtest.py"
VENV_PY = ROOT / "pine_lab" / ".venv" / "bin" / "python"
PYTHON = VENV_PY if VENV_PY.is_file() else Path(sys.executable)
LOCK = lb.batch_dir() / "worker.lock"

RE_TOTAL = re.compile(r"：(\d+) 个标的")
RE_PROGRESS = re.compile(r"\[(\d+)/(\d+)\]\s+(\d+)/(\d+)\s+标的\s+·\s+行\s+(\d+)")
LOG_TAIL = 40


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _claim_lock():
    """进程级锁：cron 每分钟起一次，上一轮没跑完就安静退出（不排队堆积）。"""
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK, "w", encoding="utf-8")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def _next_request() -> tuple[str, dict] | None:
    qdir = lb.queue_dir()
    if not qdir.is_dir():
        return None
    for path in sorted(qdir.glob("*.json")):
        try:
            req = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            path.unlink(missing_ok=True)      # 坏请求不堵队列
            lb.write_status(path.stem, status="failed", stage="queued",
                            error="请求文件损坏，已丢弃")
            continue
        path.unlink(missing_ok=True)          # 认领：状态文件是唯一进度来源
        return path.stem, req
    return None


def _argv(req: dict) -> list[str]:
    run_id = req["run_id"]
    argv = [
        str(PYTHON), str(CLI),
        "--run-id", run_id,
        "--strategies", ",".join(req.get("strategies") or []),
        "--adj", str(req.get("adj") or "backward"),
        "--min-bars", str(int(req.get("min_bars") or lb.MIN_BARS)),
        "--out-dir", str(lb.batch_dir()),
    ]
    codes = req.get("codes") or []
    if codes:
        lb.requests_dir().mkdir(parents=True, exist_ok=True)
        codes_path = lb.requests_dir() / f"{run_id}.json"
        tmp = codes_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"codes": codes}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(codes_path)
        argv += ["--pool", f"file:{codes_path}"]
    else:
        argv += ["--pool", str(req.get("pool") or "hs300")]
        if int(req.get("limit") or 0):
            argv += ["--limit", str(int(req["limit"]))]
    return argv


def _patch_result_pool(run_id: str, req: dict) -> None:
    """自定义池：CLI 把 pool 写成 file:<path>，结果里改回 custom + 可读标签。"""
    path = lb.batch_dir() / f"{run_id}.json"
    if not path.is_file():
        return
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    d["pool"] = "custom" if req.get("codes") else d.get("pool")
    d["pool_label"] = req.get("pool_label") or d.get("pool_label")
    lb.save_run(d)


def process(run_id: str, req: dict) -> dict:
    lb.write_status(
        run_id, status="running", stage="backtest", started=_now(), error="",
        total=len(req.get("codes") or []) or 0, done=0, rows=0,
        pool=req.get("pool"), pool_label=req.get("pool_label"),
        strategies=req.get("strategies"), adj=req.get("adj"),
    )
    argv = _argv(req)
    tail: list[str] = []
    total, done, rows = 0, 0, 0
    last_flush = 0.0
    t0 = time.time()

    proc = subprocess.Popen(
        argv, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip("\n")
        tail.append(line)
        if len(tail) > LOG_TAIL:
            tail.pop(0)
        m = RE_TOTAL.search(line)
        if m:
            total = int(m.group(1))
            lb.write_status(run_id, total=total, stage="backtest")
        m = RE_PROGRESS.search(line)
        if m:
            done, rows = int(m.group(3)), int(m.group(5))
            if total and int(m.group(4)):
                total = int(m.group(4))
            now = time.time()
            if now - last_flush >= 2:
                last_flush = now
                lb.write_status(
                    run_id, done=done, rows=rows, total=total,
                    progress=round(done / total, 4) if total else None,
                    elapsed_sec=round(now - t0, 1),
                )
    code = proc.wait()
    elapsed = round(time.time() - t0, 1)

    if code == 0:
        _patch_result_pool(run_id, req)
        result_path = lb.batch_dir() / f"{run_id}.json"
        counts = {}
        universe = total
        try:
            d = json.loads(result_path.read_text(encoding="utf-8"))
            counts = d.get("counts") or {}
            universe = d.get("universe", universe)
        except (OSError, json.JSONDecodeError):
            pass
        return lb.write_status(
            run_id, status="done", stage="done", done=done or universe,
            total=total or universe, rows=counts.get("rows", rows),
            counts=counts, universe=universe, elapsed_sec=elapsed, error="",
            finished=_now(),
        )
    detail = next((ln for ln in reversed(tail) if ln.strip()), f"exit {code}")
    return lb.write_status(
        run_id, status="failed", stage="backtest", done=done, total=total,
        rows=rows, elapsed_sec=elapsed, error=detail[:500],
        log="\n".join(tail), finished=_now(),
    )


def main() -> int:
    lock = _claim_lock()
    if lock is None:
        return 0
    try:
        item = _next_request()
        if item is None:
            return 0
        run_id, req = item
        req.setdefault("run_id", run_id)
        try:
            status = process(run_id, req)
        except Exception as exc:  # noqa: BLE001 单批异常不能把 worker 打死
            status = lb.write_status(run_id, status="failed", stage="worker",
                                     error=f"worker 异常: {exc}"[:500], finished=_now())
        print(f"[{run_id}] {status.get('status')} · "
              f"{status.get('done', 0)}/{status.get('total', 0)} · "
              f"{status.get('error', '')}", flush=True)
        return 0
    finally:
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
        finally:
            lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
