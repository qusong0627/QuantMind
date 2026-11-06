"""Small, owner-checked progress snapshots shared with the RD subprocess."""

import json
import os
import re
import tempfile
import threading
from pathlib import Path

from backend.shared.utc_datetime import utc_now

FILENAME = "mining_progress.json"
FIELDS = (
    "current_loop",
    "loop_n",
    "completed_factors",
    "total_factors",
    "progress_updated_at",
)


class MiningProgress:
    def __init__(self, directory, task_id, tenant_id, user_id, loop_n):
        self.directory = Path(directory)
        self.lock = threading.Lock()
        self.snapshot = {
            "task_id": task_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "loop_n": loop_n,
            "current_loop": 0,
            "progress_pct": 0,
            "completed_factors": 0,
            "total_factors": None,
        }

    def update(self, phase, message, percent, **fields):
        with self.lock:
            self.snapshot.update(fields)
            self.snapshot.update(
                phase=phase,
                progress=message,
                progress_pct=max(self.snapshot["progress_pct"], min(99, percent)),
                progress_updated_at=utc_now().isoformat().replace("+00:00", "Z"),
            )
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.directory, delete=False
            ) as handle:
                temporary = handle.name
                json.dump(self.snapshot, handle, ensure_ascii=False)
            try:
                os.replace(temporary, self.directory / FILENAME)
            finally:
                Path(temporary).unlink(missing_ok=True)

    def on_step(self, message, step_count):
        match = re.fullmatch(r"Start Loop (\d+), Step (\d+): (\w+)", message)
        if not match:
            return
        loop, step = int(match[1]), int(match[2])
        phase = match[3]
        label = {
            "direct_exp_gen": "生成假设",
            "coding": "编写因子",
            "running": "运行实验",
            "feedback": "评估实验",
            "record": "汇总本轮",
        }.get(phase, phase)
        current = min(loop + 1, self.snapshot["loop_n"])
        percent = 5 + int(
            70
            * (loop * step_count + step)
            / max(1, self.snapshot["loop_n"] * step_count)
        )
        self.update(
            phase,
            f"第 {current}/{self.snapshot['loop_n']} 轮 — {label}",
            percent,
            current_loop=current,
        )


def read_progress(directory, task_id, tenant_id, user_id):
    try:
        path = Path(directory) / FILENAME
        if path.stat().st_size > 8192:
            return None
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        if any(
            snapshot.get(key) != value
            for key, value in (
                ("task_id", task_id),
                ("tenant_id", tenant_id),
                ("user_id", user_id),
            )
        ):
            return None
        percent = snapshot.get("progress_pct")
        if type(percent) is not int or not 0 <= percent <= 99:
            return None
        if not all(
            isinstance(snapshot.get(key), str) and snapshot[key]
            for key in ("phase", "progress")
        ):
            return None
        if not all(
            type(snapshot.get(key)) is int and snapshot[key] >= 0
            for key in ("current_loop", "loop_n", "completed_factors")
        ):
            return None
        if snapshot["loop_n"] < 1 or snapshot["current_loop"] > snapshot["loop_n"]:
            return None
        return snapshot
    except (OSError, ValueError, TypeError, AttributeError):
        return None
