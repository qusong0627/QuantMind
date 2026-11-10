"""训练失败归档：Exit 137 ≠ OOM（2026-11-10 P1 诊断）。

反例（调查实锤）：残留容器 ``qm-train-train_20261110082420_aea54a07``
``Exited 137`` 但 ``docker inspect .State.OOMKilled=false``（用户取消）；
另有物化 peak≈22G（容器上限 53G）仍在 catboost 迭代中被杀的任务。
因此归档行必须落 ``OOMKilled`` 与内存上限，且 ``OOMKilled=false`` 时明确
「不可判为 OOM」，不能把 137 一律写成 OOM。

本组用例只测纯格式化函数，不启动容器、不连 Docker。
"""

from __future__ import annotations

import pytest

from backend.services.engine.training.local_docker_orchestrator import (
    _format_failure_diag,
)

_LIMIT = 53 * 1024**3


@pytest.mark.unit
def test_oom_killed_true_is_reported_with_memory_limit() -> None:
    line = _format_failure_diag(137, {"OOMKilled": True}, {"Memory": _LIMIT})

    assert "ExitCode=137" in line
    assert "OOMKilled=true" in line
    assert f"MemoryLimitBytes={_LIMIT}" in line
    assert "不可判为 OOM" not in line


@pytest.mark.unit
def test_137_without_oom_flag_is_not_labelled_oom() -> None:
    line = _format_failure_diag(137, {"OOMKilled": False}, {"Memory": _LIMIT})

    assert "OOMKilled=false" in line
    assert "不可判为 OOM" in line


@pytest.mark.unit
def test_non_137_failure_has_no_oom_verdict() -> None:
    line = _format_failure_diag(2, {}, {"Memory": 0})

    assert "ExitCode=2" in line
    assert "OOMKilled=false" in line
    assert "MemoryLimitBytes=0" in line
    assert "不可判为 OOM" not in line
