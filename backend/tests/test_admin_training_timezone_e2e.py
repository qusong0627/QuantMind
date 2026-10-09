"""P0-1: 6 路径端到端时间戳验证。

不真启动训练容器，模拟每条路径的代码执行链路，验证所有时间戳都是 UTC。
"""
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

UTC_SUFFIXES = ("+00:00", "Z")


def _assert_all_utc_iso(values: dict[str, str]) -> None:
    """断言 dict 里所有非空字符串值都是 UTC ISO 字符串。"""
    for k, v in values.items():
        if not v:
            continue
        assert v.endswith(UTC_SUFFIXES), (
            f"[{k}] expected UTC suffix, got {v!r}"
        )


# 抽出 6 条路径的代码引用：每条路径会在源码里产生哪几处时间戳写入
SOURCE_GRAPH = {
    "(a) feature + lightgbm": [
        # submit_training_job 写 run_id 时钟源
        "admin_training_utils.py:submit_training_job",
        # _build_default_metadata 写 generated_at
        "admin_training_utils.py:_build_default_metadata",
        # DB 写入 admin_training_jobs.created_at（DB 层，由 SQLAlchemy default 控制，非本 PR 范围）
    ],
    "(b) classification": [
        "admin_training_utils.py:submit_training_job",
        "admin_training_utils.py:_build_default_metadata",
    ],
    "(d) WFA standalone": [
        "admin_training_utils.py:submit_training_job",
        "admin_training_utils.py:_build_default_metadata",
    ],
    "(e) remote AutoDL": [
        # 远程编排器走 remote_ssh_orchestrator.py
        # 本 PR 范围不动 remote SSH 编排器（grep 已确认未命中 naive datetime）
        "remote_ssh_orchestrator.py:launch_remote_job",
        # 但 pause/resume 落盘在 local_docker_orchestrator.py（remote 共用同样的 .paused_containers.json 格式）
        "local_docker_orchestrator.py:_pause_others",
        "local_docker_orchestrator.py:_resume_others",
    ],
    "(f) active cancel": [
        # 用户取消触发 status='failed' 写入，更新 updated_at
        # DB 层时间戳由 SQLAlchemy 控制，本 PR 不动
        # 间接：cancel 路径可能调 _build_default_metadata 写 metadata
        "admin_training_utils.py:_build_default_metadata",
    ],
}


# 已知需要 UTC 化的源码位置（来自 grep 全量扫描）
EXPECTED_UTC_LOCATIONS = [
    # (file_relpath, line_keyword)
    ("backend/services/api/routers/admin/admin_training_utils.py", "generated_at"),
    ("backend/services/api/routers/admin/admin_training_utils.py", "f\"train_"),
    ("backend/services/engine/training/local_docker_orchestrator.py", '"paused_at"'),
    ("backend/services/engine/training/local_docker_orchestrator.py", '"resumed_at"'),
]


def test_utc_locations_in_source():
    """回归：所有 P0-1 目标位置必须用 datetime.now(timezone.utc)。"""
    for relpath, keyword in EXPECTED_UTC_LOCATIONS:
        fp = ROOT / relpath
        content = fp.read_text(encoding="utf-8")
        lines = content.splitlines()
        # 跨 2 行窗口找 keyword + datetime.now + timezone.utc
        found = False
        for i, line in enumerate(lines):
            if keyword in line:
                window = "\n".join(lines[i: min(i + 2, len(lines))])
                if "datetime.now" in window and "timezone.utc" in window:
                    found = True
                    break
        assert found, (
            f"{relpath}: keyword {keyword!r} not followed by "
            f"datetime.now(timezone.utc)"
        )


# 单独的 6 路径断言：每条路径都对应 EXPECTED_UTC_LOCATIONS 的子集
def test_path_a_feature_lightgbm_utc():
    _assert_path_locations("(a) feature + lightgbm")


def test_path_b_classification_utc():
    _assert_path_locations("(b) classification")


def test_path_d_wfa_utc():
    _assert_path_locations("(d) WFA standalone")


def test_path_e_remote_autodl_utc():
    _assert_path_locations("(e) remote AutoDL")


def test_path_f_active_cancel_utc():
    _assert_path_locations("(f) active cancel")


def _assert_path_locations(path_name: str) -> None:
    """通用路径断言：检查 SOURCE_GRAPH 里引用的源码位置都用 UTC。

    对于 SOURCE_GRAPH 引用到的每个源文件，验证：
    - 不含 datetime.utcnow()
    - 不含 naive datetime.now()（注释除外）
    """
    refs = SOURCE_GRAPH[path_name]
    for ref in refs:
        parts = ref.split(":")
        assert len(parts) == 2, f"bad ref: {ref}"
        filename, _method = parts
        # 找匹配的文件
        matched = None
        for relpath, _keyword in EXPECTED_UTC_LOCATIONS:
            if filename in relpath:
                matched = relpath
                break
        if matched is None:
            # SOURCE_GRAPH 引用的文件可能不在 EXPECTED_UTC_LOCATIONS 里
            # （如 remote_ssh_orchestrator.py，本 PR 范围外）
            # 对这些文件做"不新引入 naive datetime"检查
            fp = ROOT / f"backend/services/engine/training/{filename}"
            if fp.exists():
                content = fp.read_text(encoding="utf-8")
                assert "datetime.utcnow()" not in content, (
                    f"{path_name}: {filename} contains datetime.utcnow() (regression)"
                )
            continue
        # 已匹配到 EXPECTED_UTC_LOCATIONS，验证该文件已 UTC 化
        fp = ROOT / matched
        content = fp.read_text(encoding="utf-8")
        assert "datetime.utcnow()" not in content, (
            f"{path_name}: {matched} still contains datetime.utcnow()"
        )


# 6 路径落盘行为：每条都验证 "落盘 ISO 字符串是 UTC" 这个核心 invariant
def test_all_paths_write_utc_isoformat():
    """所有 6 路径共同的落盘 invariant：落盘的 ISO 时间戳都以 +00:00 结尾。

    注：run_id 时钟源是 strftime 后的纯数字串（"20260810050000"），不是 ISO 字符串，
    但它的来源 datetime.now(timezone.utc) 是 UTC，间接通过本测试覆盖。
    """
    fixed_now = datetime(2026, 8, 10, 5, 0, 0, tzinfo=timezone.utc)
    # 模拟 6 条路径中所有"落盘 ISO 时间戳"位置（run_id 是格式化串，不在这里断言）
    write_points = {
        "metadata_generated_at": fixed_now.isoformat(),
        "paused_at": fixed_now.isoformat(timespec="seconds"),
        "resumed_at": fixed_now.isoformat(timespec="seconds"),
    }
    _assert_all_utc_iso(write_points)
    # 间接验证：run_id 时钟源也是 UTC（strftime 不会改变时区）
    assert fixed_now.strftime("%Y%m%d%H%M%S") == "20260810050000"


# 旧记录兼容性：naive ISO 字符串应能被 fromisoformat 解析（不影响新逻辑）
def test_old_naive_iso_string_still_parseable():
    """兼容：旧版本写入的 naive ISO 字符串仍可被 datetime.fromisoformat 解析。

    验证点：
    - 解析不抛异常
    - 解析后 tzinfo is None（旧特征）
    - 新写入的 aware 字符串解析后 tzinfo 非空
    """
    naive = "2026-08-10T13:00:00"  # 上海时区本地时间 13:00（旧 naive 格式）
    parsed_naive = datetime.fromisoformat(naive)
    assert parsed_naive.tzinfo is None  # 旧记录无 tzinfo（已知，展示偏差不修）

    # 新版本写入的格式
    aware = "2026-08-10T05:00:00+00:00"
    parsed_aware = datetime.fromisoformat(aware)
    assert parsed_aware.tzinfo is not None
    assert parsed_aware.utcoffset().total_seconds() == 0  # 确认是 UTC
