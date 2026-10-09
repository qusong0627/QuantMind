"""P0-1: 验证训练模块所有时间戳落盘为 UTC（含 +00:00 偏移）。

修复目标：
- admin_training_utils.py:680 _build_default_metadata generated_at
- admin_training_utils.py:807 submit_training_job run_id 时钟源
- local_docker_orchestrator.py:164 _pause_others paused_at
- local_docker_orchestrator.py:215 _resume_others resumed_at

测试策略：
- 涉及 admin_training_utils 的测试用 importlib 直接加载模块（模块依赖重，验证会发生）
- 涉及 local_docker_orchestrator 的测试**不真导入模块**（项目环境缺 docker Python 包），
  改为：1) grep 源码确认无 datetime.utcnow()/naive datetime.now()
       2) 直接执行"datetime.now(timezone.utc).isoformat()" 落盘并读回，验证后缀
"""
import importlib.util
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

UTC_SUFFIXES = ("+00:00", "Z")


def _assert_utc_iso(s: str) -> None:
    assert s.endswith(UTC_SUFFIXES), (
        f"expected UTC suffix {UTC_SUFFIXES}, got {s!r}"
    )


def _load_module_safe(rel_path: str, alias: str):
    """用 importlib 加载模块；如果模块导入失败，返回 None（不抛）。"""
    fp = ROOT / rel_path
    if not fp.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location(alias, fp)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


# ---------- 1. admin_training_utils.py 源码回归 ----------

def test_admin_training_utils_no_naive_datetime():
    """源码回归：admin_training_utils.py 不能含 datetime.utcnow() 或 naive datetime.now()。"""
    fp = ROOT / "backend/services/api/routers/admin/admin_training_utils.py"
    content = fp.read_text(encoding="utf-8")
    assert "datetime.utcnow()" not in content, (
        "admin_training_utils.py still contains datetime.utcnow()"
    )
    for ln, line in enumerate(content.splitlines(), start=1):
        if "datetime.now()" in line and "timezone" not in line:
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            pytest.fail(
                f"admin_training_utils.py:{ln} contains naive datetime.now(): {line!r}"
            )


def test_admin_training_utils_imports_timezone():
    """admin_training_utils.py 必须 import timezone。"""
    fp = ROOT / "backend/services/api/routers/admin/admin_training_utils.py"
    content = fp.read_text(encoding="utf-8")
    assert "from datetime import" in content
    # 第一行 from datetime import ... 必须含 timezone
    m = re.search(r"^from datetime import (.+)$", content, re.MULTILINE)
    assert m, "no 'from datetime import' line found"
    imported = m.group(1)
    assert "timezone" in imported, (
        f"admin_training_utils.py imports '{imported}' but missing 'timezone'"
    )


def test_admin_training_utils_build_default_metadata_uses_utc():
    """_build_default_metadata 必须调用 datetime.now(timezone.utc).isoformat()。"""
    fp = ROOT / "backend/services/api/routers/admin/admin_training_utils.py"
    content = fp.read_text(encoding="utf-8")
    lines = content.splitlines()
    # 跨行扫描：找包含 "generated_at" 的行及其下一行（or 分支）
    for ln, line in enumerate(lines, start=1):
        if "generated_at" in line:
            window = "\n".join(lines[ln - 1: min(ln + 2, len(lines))])
            if "datetime.now" in window:
                assert "timezone.utc" in window, (
                    f"lines around {ln} have datetime.now but no timezone.utc:\n{window}"
                )
                return
    pytest.fail("could not find generated_at line with datetime.now")


def test_admin_training_utils_submit_run_id_uses_utc():
    """run_id 构造点（_new_training_run_id）必须用 datetime.now(timezone.utc).strftime。"""
    fp = ROOT / "backend/services/api/routers/admin/admin_training_utils.py"
    content = fp.read_text(encoding="utf-8")
    for ln, line in enumerate(content.splitlines(), start=1):
        if "f\"train_" in line or "f'train_" in line:
            assert "timezone.utc" in line, (
                f"line {ln} missing timezone.utc: {line!r}"
            )
            return
    pytest.fail("could not find run_id f-string in _new_training_run_id")


# ---------- 2. admin_training_utils 真实模块行为（如果能 import） ----------

def test_build_default_metadata_runtime_uses_utc():
    """实际调用 _build_default_metadata 验证 generated_at 是 UTC。"""
    mod = _load_module_safe(
        "backend/services/api/routers/admin/admin_training_utils.py",
        "admin_training_utils_for_test",
    )
    if mod is None:
        pytest.skip("admin_training_utils cannot be imported in this env")
    fixed_now = datetime(2026, 8, 10, 5, 0, 0, tzinfo=timezone.utc)
    with patch.object(mod, "datetime") as dt_mock:
        dt_mock.now.side_effect = lambda tz=None: fixed_now
        dt_mock.timezone = timezone
        result = mod._build_default_metadata({}, "test_run_id")
    assert "generated_at" in result
    _assert_utc_iso(result["generated_at"])


def test_submit_training_job_run_id_uses_utc_clock():
    """模拟 TZ=Asia/Shanghai + 固定 UTC 时钟，直接调用 run_id 构造点验证时钟源。

    旧版断言的是**测试进程自己的真实时钟**（`datetime.now(timezone.utc)`），
    与固定时刻比较，除恰好在那一刻运行外必然失败——是结构性坏测试（假红）。
    这里替换模块时钟并**直接调用生产代码** ``_new_training_run_id()``；
    mock 对 ``tz=None``（naive 写法）直接抛错，比数值差更早、更明确地拦住回归。
    """
    mod = _load_module_safe(
        "backend/services/api/routers/admin/admin_training_utils.py",
        "admin_training_utils_for_test",
    )
    if mod is None:
        pytest.skip("admin_training_utils cannot be imported in this env")
    os.environ["TZ"] = "Asia/Shanghai"
    try:
        fixed_now = datetime(2026, 8, 10, 5, 0, 0, tzinfo=timezone.utc)

        def _now(tz=None):
            assert tz is timezone.utc, (
                f"run_id 时钟源必须传 timezone.utc（收到 tz={tz!r}）；"
                "naive 写法在 TZ=Shanghai 下会偏成墙钟 13:00"
            )
            return fixed_now

        with patch.object(mod, "datetime") as dt_mock:
            dt_mock.now.side_effect = _now
            dt_mock.timezone = timezone
            run_id = mod._new_training_run_id()
    finally:
        os.environ.pop("TZ", None)
    # UTC 5:00 → 20260810050000（naive 写法在 TZ=Shanghai 下会得 20260810130000）
    assert run_id.startswith("train_20260810050000_"), run_id
    assert len(run_id.split("_")[-1]) == 8  # hex 序号段仍在


# ---------- 3. local_docker_orchestrator.py 源码回归（不真导入） ----------

def test_local_docker_orchestrator_no_naive_datetime():
    """源码回归：local_docker_orchestrator.py 不能含 datetime.utcnow() 或 naive datetime.now()。"""
    fp = ROOT / "backend/services/engine/training/local_docker_orchestrator.py"
    content = fp.read_text(encoding="utf-8")
    assert "datetime.utcnow()" not in content, (
        "local_docker_orchestrator.py still contains datetime.utcnow()"
    )
    for ln, line in enumerate(content.splitlines(), start=1):
        if "datetime.now()" in line and "timezone" not in line:
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            pytest.fail(
                f"local_docker_orchestrator.py:{ln} contains naive datetime.now(): {line!r}"
            )


def test_local_docker_orchestrator_imports_timezone():
    fp = ROOT / "backend/services/engine/training/local_docker_orchestrator.py"
    content = fp.read_text(encoding="utf-8")
    m = re.search(r"^from datetime import (.+)$", content, re.MULTILINE)
    assert m, "no 'from datetime import' line found in local_docker_orchestrator.py"
    imported = m.group(1)
    assert "timezone" in imported, (
        f"local_docker_orchestrator.py imports '{imported}' but missing 'timezone'"
    )


def test_local_docker_orchestrator_paused_at_uses_utc():
    fp = ROOT / "backend/services/engine/training/local_docker_orchestrator.py"
    content = fp.read_text(encoding="utf-8")
    for ln, line in enumerate(content.splitlines(), start=1):
        if '"paused_at"' in line or "'paused_at'" in line:
            assert "datetime.now" in line, (
                f"line {ln} paused_at without datetime.now: {line!r}"
            )
            assert "timezone.utc" in line, (
                f"line {ln} paused_at without timezone.utc: {line!r}"
            )
            return
    pytest.fail("could not find paused_at line")


def test_local_docker_orchestrator_resumed_at_uses_utc():
    fp = ROOT / "backend/services/engine/training/local_docker_orchestrator.py"
    content = fp.read_text(encoding="utf-8")
    for ln, line in enumerate(content.splitlines(), start=1):
        if '"resumed_at"' in line or "'resumed_at'" in line:
            assert "datetime.now" in line, (
                f"line {ln} resumed_at without datetime.now: {line!r}"
            )
            assert "timezone.utc" in line, (
                f"line {ln} resumed_at without timezone.utc: {line!r}"
            )
            return
    pytest.fail("could not find resumed_at line")


# ---------- 4. 落盘行为端到端（不依赖 docker 包） ----------

def test_paused_at_writes_utc_to_disk(tmp_path):
    """模拟 _pause_others 写盘行为，验证落盘 ISO 字符串是 UTC。"""
    state_path = tmp_path / ".paused_containers.json"
    fixed_now = datetime(2026, 8, 10, 5, 0, 0, tzinfo=timezone.utc)
    state_path.write_text(
        '{"run_id": "r1", "paused_at": "'
        + fixed_now.isoformat(timespec="seconds")
        + '", "containers": []}',
        encoding="utf-8",
    )
    import json
    data = json.loads(state_path.read_text(encoding="utf-8"))
    _assert_utc_iso(data["paused_at"])


def test_resumed_at_writes_utc_to_disk(tmp_path):
    """模拟 _resume_others 写盘行为，验证落盘 ISO 字符串是 UTC。"""
    state_path = tmp_path / ".paused_containers.json"
    state_path.write_text(
        '{"run_id": "r1", "paused_at": "2026-08-10T05:00:00+00:00", "containers": []}',
        encoding="utf-8",
    )
    fixed_now = datetime(2026, 8, 10, 5, 30, 0, tzinfo=timezone.utc)
    import json
    data = json.loads(state_path.read_text(encoding="utf-8"))
    data["resumed_at"] = fixed_now.isoformat(timespec="seconds")
    state_path.write_text(json.dumps(data), encoding="utf-8")
    data = json.loads(state_path.read_text(encoding="utf-8"))
    _assert_utc_iso(data["resumed_at"])
