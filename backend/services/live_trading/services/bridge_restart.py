"""通达信桥远程重启：向 Windows 桥目录投递 ``restart_bridge.flag``。

机制（源头=老系统 quant-Trader，机制原样保留）：Windows 桥目录下的
``watchdog.ps1`` 每 30s ``Test-Path restart_bridge.flag``，存在即杀掉占用
8550 的旧桥进程并重启（注释：「收到 Linux 侧重启信号」）。Linux 侧经 CIFS
把 ``//<win>/PYPlugins`` 挂到共享根（fstab 已有条目），写这个文件即投递。

**本地空目录 ≠ 共享**（老系统投递从来没成功过的根因）：fstab 该行是
``nofail``，开机网络未就绪时静默跳过挂载，之后 ``/mnt/tdx-shared`` 只是
一个本地空目录——flag 写进去也永远到不了 Windows，而且**不报任何错**。
所以投递前双重守卫：
① 该路径必须出现在 ``/proc/mounts`` 且 fstype 为 cifs；
② ``bridge-windows`` 子目录必须存在。
两条任一不满足即如实拒绝，不写黑洞。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from backend.shared.utc_datetime import utc_now

logger = logging.getLogger(__name__)

DEFAULT_SHARE_ROOT = "/mnt/tdx-shared"
BRIDGE_SUBDIR = "bridge-windows"
FLAG_NAME = "restart_bridge.flag"
DEFAULT_AUDIT_LOG = "/app/logs/bridge_restart_audit.jsonl"
MOUNTS_FILE = "/proc/mounts"


def share_root() -> Path:
    """共享根目录（env ``QM_TDX_SHARE_DIR`` 可覆盖，测试/多环境用）。"""
    return Path(os.getenv("QM_TDX_SHARE_DIR", DEFAULT_SHARE_ROOT))


def is_cifs_mount(mounts_text: str, root: str) -> bool:
    """root 是否为 /proc/mounts 里的 cifs 挂载点（第 2 列精确匹配）。

    只认挂载点本身：``/mnt`` 上挂了别的东西不算 ``/mnt/tdx-shared`` 已挂载。
    """
    target = root.rstrip("/") or "/"
    for line in mounts_text.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1] == target and parts[2] == "cifs":
            return True
    return False


def check_share(share: Path, mounts_text: str) -> tuple[bool, str]:
    """双重守卫：cifs 挂载点 + bridge-windows 子目录。"""
    if not is_cifs_mount(mounts_text, str(share)):
        return False, (
            f"共享未挂载：{share} 不在 /proc/mounts 的 cifs 挂载点里。"
            "写进去的 flag 只会留在本地空目录、永远到不了 Windows（静默黑洞）。"
            "请先在宿主机执行 sudo mount /mnt/tdx-shared（fstab 已有该条目）。"
        )
    bridge_dir = share / BRIDGE_SUBDIR
    if not bridge_dir.is_dir():
        return False, (
            f"桥目录不存在：{bridge_dir}。共享已挂载但目录结构不符——"
            "检查共享名是否为 PYPlugins、桥是否部署在 bridge-windows 子目录。"
        )
    return True, "ok"


def _read_mounts() -> str:
    try:
        return Path(MOUNTS_FILE).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:  # pragma: no cover - /proc 不可读属环境异常
        logger.warning("[BridgeRestart] 读取 %s 失败: %s", MOUNTS_FILE, exc)
        return ""


def _audit(record: dict, audit_path: Path) -> None:
    """留痕：JSONL 追加一行（失败只告警，不阻断投递）。"""
    try:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("[BridgeRestart] 审计日志写入失败: %s", exc)


def request_restart(
    *,
    share: Path | None = None,
    mounts_text: str | None = None,
    audit_path: Path | None = None,
    actor: str = "",
) -> dict:
    """投递重启 flag（同步；路由层经 to_thread 调用）。

    返回 ``{success, detail, flag_written, already_pending, mounted}``。
    幂等：flag 已存在（上次未被消费）时不重复写，如实返回 already_pending。
    """
    share = share if share is not None else share_root()
    text = _read_mounts() if mounts_text is None else mounts_text
    audit = audit_path or Path(os.getenv("QM_BRIDGE_AUDIT_LOG", DEFAULT_AUDIT_LOG))

    ok, why = check_share(share, text)
    if not ok:
        return {"success": False, "detail": why, "flag_written": False, "mounted": False}

    flag = share / BRIDGE_SUBDIR / FLAG_NAME
    if flag.exists():
        return {
            "success": True,
            "already_pending": True,
            "flag_written": False,
            "mounted": True,
            "detail": (
                "重启信号已在队列中（上次投递尚未被看门狗消费），未重复投递。"
                "若持续 30s 以上未消费，请到 Windows 上检查看门狗服务是否在运行。"
            ),
        }
    try:
        flag.touch()
    except OSError as exc:
        return {
            "success": False,
            "flag_written": False,
            "mounted": True,
            "detail": f"flag 写入失败：{exc}（共享可写性/权限位检查 file_mode）",
        }
    _audit(
        {
            "event": "bridge_restart_requested",
            "flag": str(flag),
            "actor": actor,
            "ts": utc_now().isoformat(),
        },
        audit,
    )
    logger.info("[BridgeRestart] 已投递重启 flag: %s（actor=%s）", flag, actor or "-")
    return {
        "success": True,
        "already_pending": False,
        "flag_written": True,
        "mounted": True,
        "detail": (
            "已投递重启信号，桥看门狗最长 30s 内重启（期间行情/交易通道短暂中断）。"
            "稍后用「测试连接」确认桥恢复；若 30s 后仍未恢复，到 Windows 检查看门狗服务。"
        ),
    }


def restart_status(
    *, share: Path | None = None, mounts_text: str | None = None
) -> dict:
    """状态查询：共享是否挂载 / 桥目录是否在位 / flag 是否待消费。"""
    share = share if share is not None else share_root()
    text = _read_mounts() if mounts_text is None else mounts_text
    mounted = is_cifs_mount(text, str(share))
    bridge_dir = share / BRIDGE_SUBDIR
    bridge_dir_ok = mounted and bridge_dir.is_dir()
    flag_pending = False
    if bridge_dir_ok:
        try:
            flag_pending = (bridge_dir / FLAG_NAME).exists()
        except OSError:  # pragma: no cover - CIFS 断连竞态
            flag_pending = False
    return {
        "share_root": str(share),
        "mounted": mounted,
        "bridge_dir_ok": bridge_dir_ok,
        "flag_pending": flag_pending,
    }
