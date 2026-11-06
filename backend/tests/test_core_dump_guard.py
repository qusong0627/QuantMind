"""core dump 限额守卫的回归闸（2026-11-07）。

背景：宿主 ``kernel.core_pattern`` 是裸的 ``core``，崩溃进程的 core 写进 CWD——
容器 CWD 是可写层，``/app/backend`` 还是宿主仓库 bind mount。实测 2026-10-15
一次 pytest SIGABRT 留 15.9GB（落进仓库工作树）、2026-11-06 十份 spawn SIGSEGV
共 38GB。所以三个入口（main_oss / celery worker+beat / tdx worker）都必须关掉它。
"""

from __future__ import annotations

import resource
from pathlib import Path

from backend.shared import core_dump_guard as guard

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_disable_core_dumps_sets_zero(monkeypatch):
    calls: list[tuple[int, tuple[int, int]]] = []
    monkeypatch.setattr(
        resource, "setrlimit", lambda what, limits: calls.append((what, limits))
    )

    guard.disable_core_dumps()

    assert calls == [(resource.RLIMIT_CORE, (0, 0))]


def test_disable_core_dumps_swallows_errors(monkeypatch):
    def boom(*_args, **_kwargs):
        raise OSError("restricted")

    monkeypatch.setattr(resource, "setrlimit", boom)

    # 不抛：磁盘保护不值得阻断启动
    guard.disable_core_dumps()


def test_all_process_entrypoints_call_the_guard():
    """三个启动入口都要接上（worker/beat 不走 main_oss）。"""
    files = {
        "backend/main_oss.py": "主容器全部服务",
        "backend/services/engine/qlib_app/celery_config.py": "celery worker/beat",
        "backend/shared/tdx_aidata/worker.py": "TDX 行情 worker",
    }
    for rel, label in files.items():
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "disable_core_dumps()" in text, f"{label}（{rel}）未关闭 core dump"
