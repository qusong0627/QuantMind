"""通达信桥远程重启测试：CIFS 挂载守卫 + flag 投递幂等 + 审计留痕 + 接线源断言。

核心防回退：**本地空目录 ≠ 共享**。fstab 用 nofail，挂载失败时
/mnt/tdx-shared 只是空目录——老系统 ops_worker 往这种目录投递 flag
从来不报错、也从来到不了 Windows。未挂载时必须如实拒绝且**不写任何文件**。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from backend.services.live_trading.services.bridge_restart import (
    BRIDGE_SUBDIR,
    FLAG_NAME,
    check_share,
    is_cifs_mount,
    request_restart,
    restart_status,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

_MOUNTS_OK = (
    "sysfs /sys sysfs rw,nosuid 0 0\n"
    "//192.168.31.13/PYPlugins /mnt/tdx-shared cifs rw,relatime,vers=3.0 0 0\n"
)
_MOUNTS_OTHER = "sysfs /sys sysfs rw,nosuid 0 0\ntmpfs /mnt/tdx-shared tmpfs rw 0 0\n"
_MOUNTS_PREFIX_ONLY = "//192.168.31.13/PYPlugins /mnt cifs rw 0 0\n"


# ── 挂载判定（纯函数）────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    "text,root,expected",
    [
        (_MOUNTS_OK, "/mnt/tdx-shared", True),
        (_MOUNTS_OK, "/mnt/tdx-shared/", True),  # 尾斜杠归一
        (_MOUNTS_OK, "/mnt/tdx-other", False),
        (_MOUNTS_OTHER, "/mnt/tdx-shared", False),  # 挂了但 fstype 不是 cifs
        (_MOUNTS_PREFIX_ONLY, "/mnt/tdx-shared", False),  # 只挂了 /mnt，不算子路径已挂
        ("", "/mnt/tdx-shared", False),
    ],
)
def test_is_cifs_mount(text, root, expected):
    assert is_cifs_mount(text, root) is expected


# ── 双重守卫 ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_check_share_rejects_unmounted(tmp_path):
    """未挂载：目录存在（本地空目录假象）也必须拒绝，文案点明是黑洞。"""
    share = tmp_path / "tdx-shared"
    (share / BRIDGE_SUBDIR).mkdir(parents=True)  # 甚至子目录都在（手工建错场景）
    ok, why = check_share(share, _MOUNTS_OTHER)
    assert ok is False
    assert "未挂载" in why
    assert "sudo mount" in why


@pytest.mark.unit
def test_check_share_rejects_missing_bridge_dir(tmp_path):
    """已挂载共享但 bridge-windows 不在 → 如实拒绝（共享名/部署路径不符）。"""
    share = tmp_path / "tdx-shared"
    share.mkdir()
    ok, why = check_share(share, _MOUNTS_OK.replace("/mnt/tdx-shared", str(share)))
    assert ok is False
    assert "桥目录不存在" in why


@pytest.mark.unit
def test_check_share_ok(tmp_path):
    share = tmp_path / "tdx-shared"
    (share / BRIDGE_SUBDIR).mkdir(parents=True)
    ok, why = check_share(share, _MOUNTS_OK.replace("/mnt/tdx-shared", str(share)))
    assert ok is True and why == "ok"


# ── 投递 ────────────────────────────────────────────────────────────────


def _mounted_share(tmp_path: Path) -> tuple[Path, str]:
    share = tmp_path / "tdx-shared"
    (share / BRIDGE_SUBDIR).mkdir(parents=True)
    return share, _MOUNTS_OK.replace("/mnt/tdx-shared", str(share))


@pytest.mark.unit
def test_request_restart_writes_nothing_when_unmounted(tmp_path):
    """黑洞守卫核验：未挂载时不许写任何文件（写了就是静默无效投递）。"""
    share = tmp_path / "tdx-shared"
    (share / BRIDGE_SUBDIR).mkdir(parents=True)
    audit = tmp_path / "audit.jsonl"
    result = request_restart(share=share, mounts_text=_MOUNTS_OTHER, audit_path=audit)
    assert result["success"] is False and result["flag_written"] is False
    assert not (share / BRIDGE_SUBDIR / FLAG_NAME).exists(), "未挂载时写了 flag = 黑洞投递"
    assert not audit.exists(), "被拒绝的请求不应留审计（没发生的事不记账）"


@pytest.mark.unit
def test_request_restart_writes_flag_and_audit(tmp_path):
    share, mounts = _mounted_share(tmp_path)
    audit = tmp_path / "audit.jsonl"
    result = request_restart(
        share=share, mounts_text=mounts, audit_path=audit, actor="admin"
    )
    assert result["success"] is True and result["flag_written"] is True
    assert result["already_pending"] is False and result["mounted"] is True
    flag = share / BRIDGE_SUBDIR / FLAG_NAME
    assert flag.is_file()
    record = json.loads(audit.read_text(encoding="utf-8").strip())
    assert record["event"] == "bridge_restart_requested"
    assert record["actor"] == "admin"
    assert record["flag"] == str(flag)
    assert record["ts"].endswith("+00:00"), "瞬时时间必须 aware UTC"


@pytest.mark.unit
def test_request_restart_pending_flag_not_rewritten(tmp_path):
    """flag 已存在（上次未消费）→ 幂等返回 pending，不重复写（mtime 不变）。"""
    share, mounts = _mounted_share(tmp_path)
    flag = share / BRIDGE_SUBDIR / FLAG_NAME
    flag.touch()
    before = flag.stat().st_mtime_ns
    time.sleep(0.01)
    result = request_restart(
        share=share, mounts_text=mounts, audit_path=tmp_path / "audit.jsonl"
    )
    assert result["success"] is True and result["already_pending"] is True
    assert result["flag_written"] is False
    assert flag.stat().st_mtime_ns == before, "重复投递不应重写 flag"
    assert not (tmp_path / "audit.jsonl").exists()


@pytest.mark.unit
def test_request_restart_write_failure_is_reported(tmp_path, monkeypatch):
    """touch 抛 OSError（CIFS 断连/权限位）→ 如实报失败，不假装成功。"""
    share, mounts = _mounted_share(tmp_path)

    def _boom(self):
        raise OSError("Permission denied")

    monkeypatch.setattr(Path, "touch", _boom)
    result = request_restart(
        share=share, mounts_text=mounts, audit_path=tmp_path / "audit.jsonl"
    )
    assert result["success"] is False and result["flag_written"] is False
    assert "Permission denied" in result["detail"]


@pytest.mark.unit
def test_restart_status_reports_each_guard(tmp_path):
    share = tmp_path / "tdx-shared"
    (share / BRIDGE_SUBDIR).mkdir(parents=True)
    mounts = _MOUNTS_OK.replace("/mnt/tdx-shared", str(share))

    st = restart_status(share=share, mounts_text=_MOUNTS_OTHER)
    assert st == {
        "share_root": str(share),
        "mounted": False,
        "bridge_dir_ok": False,
        "flag_pending": False,
    }
    st = restart_status(share=share, mounts_text=mounts)
    assert st["mounted"] is True and st["bridge_dir_ok"] is True
    assert st["flag_pending"] is False
    (share / BRIDGE_SUBDIR / FLAG_NAME).touch()
    assert restart_status(share=share, mounts_text=mounts)["flag_pending"] is True


# ── 接线源断言（防回退：端点/权限/compose 挂载）─────────────────────────


@pytest.mark.unit
def test_route_is_admin_gated_and_wired():
    src = (
        _PROJECT_ROOT
        / "backend/services/trade/routers/tdx_config.py"
    ).read_text(encoding="utf-8")
    assert '"/tdx/bridge-restart"' in src, "重启端点缺失"
    assert '"/tdx/bridge-restart/status"' in src
    restart_block = src.split('"/tdx/bridge-restart"', 1)[1].split("@router", 1)[0]
    assert "require_admin" in restart_block, "重启桥属管理员面（写共享目录=主机面操作）"
    assert "request_restart" in restart_block


@pytest.mark.unit
def test_compose_mounts_tdx_shared_into_core_container():
    """功能前提：共享目录必须挂进核心容器，否则端点永远报未挂载。"""
    src = (_PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "/mnt/tdx-shared:/mnt/tdx-shared" in src
