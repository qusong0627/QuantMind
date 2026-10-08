"""RD 挖掘因子物化：子进程启动的共享实现（admin 运维面 / 用户自助面共用）。

为什么放在 shared：admin 路由（``backend/services/api/routers/admin/``）在模块
顶层 import 了 API 层 middleware，engine 进程不能干净引入；而「起子进程 →
等子进程真拿住 flock → 日志 .tmp 原子换名 → 起回收线程」这段纪律必须在两个
面逐字节一致（``backend/tests/test_rd_mined_materialize_ops.py`` 回归钉死）。
本模块只依赖 stdlib + 可选 logger，不 import 任何应用层模块。

调用方约定：
- ``command`` 由调用方组装，任何用户输入进 argv 前必须过
  ``normalize_factor_ids``（argv 是子进程入口，注入等于任意命令执行）；
- ``probe`` 是物化锁探测（``rd_mined_materialize.probe_run_lock``），保持
  函数内 import 后再传入，测试 monkeypatch 才不会脱靶；
- 失败一律抛 ``MaterializeSpawnError``（携带 HTTP 状态码与既有文案），
  调用方映射成自己的异常类型。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from collections.abc import Callable, Iterable

# 单次请求可物化的因子数上限（argv 长度与单轮物化时长的双重护栏）。
MAX_FACTOR_IDS = 100

# 与真库 factor_id（32 位 hex）兼容；白名单顺带封死 argv 注入
# （空格 / 路径分隔符 / 换行 / 分号不可能通过；首字符不许是 ``-``，
# 即便将来有调用方忘了用 ``--factor-ids=`` 单 token 形式也拼不出 flag）。
FACTOR_ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:\-]{0,127}$")

# 进程内启动闩：探测→子进程拿锁之间有 ~4s 解释器启动窗口（实测冷启 3.8~4.3s），
# 期间再来的 POST 会看到「未运行」。非阻塞拿闩，第二个请求直接 409 让位。
# admin 面与用户自助面共用同一把闩（同进程内两个入口不许各自起子进程）。
START_GUARD = threading.Lock()


class MaterializeSpawnError(RuntimeError):
    """携带 HTTP 状态码的物化启动失败（调用方映射为 HTTPException）。"""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def normalize_factor_ids(
    raw: Iterable[str], *, max_ids: int = MAX_FACTOR_IDS
) -> list[str]:
    """strip → 白名单校验 → 去重保序；非法/为空/超限一律 ValueError。

    错误文案只带序号不回显原值：非法 id 按定义是不可信输入，原样回吐
    只会把它带进日志与界面。
    """
    ids: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        value = str(item).strip()
        if not FACTOR_ID_RE.match(value):
            raise ValueError(f"factor_ids 非法（第 {index + 1} 项）")
        if value in seen:
            continue
        seen.add(value)
        ids.append(value)
        if len(ids) > max_ids:
            raise ValueError(f"factor_ids 最多 {max_ids} 个")
    if not ids:
        raise ValueError("factor_ids 不能为空")
    return ids


async def spawn_materialize(
    *,
    command: list[str],
    log_path: Path,
    cwd: Path,
    probe: Callable[[], bool],
    log_holder: logging.Logger,
    confirm_timeout_s: float,
    confirm_poll_s: float,
) -> dict[str, Any]:
    """起子进程 → 确认锁被拿住 → 起回收线程 → 返回结果。

    回包前必须确认子进程真拿住锁（拿不到/退出如实报 409/500，不假装
    started）；日志先写 ``.tmp``、子进程起成功后原子换名（启动失败不毁
    上一轮日志）。``confirmed=False`` 且进程存活 = 慢机器上超时未确认，
    不算失败。
    """
    if log_path.is_symlink():
        raise MaterializeSpawnError(
            500,
            f"物化日志路径 {log_path} 是符号链接，拒绝写入（防止覆盖任意文件）",
        )
    # 先写 .tmp、子进程起成功后再原子换名到正式路径：启动失败（解释器缺失、
    # 权限变化等）时不毁掉上一轮的事后日志；os.replace 不跟随目标符号链接。
    tmp_path = log_path.with_name(log_path.name + ".tmp")
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(
            tmp_path,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
            0o640,
        )
    except OSError as exc:
        raise MaterializeSpawnError(
            500, f"物化日志文件不可写：{tmp_path}（{exc}）"
        ) from exc

    log_handle = os.fdopen(fd, "w", encoding="utf-8")
    try:
        process = subprocess.Popen(  # noqa: S603 - 调用方组装 argv（白名单校验过）
            command,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # 脱离服务进程组：服务重启不牵连物化
            env=os.environ.copy(),
        )
    except OSError as exc:
        tmp_path.unlink(missing_ok=True)
        raise MaterializeSpawnError(
            500, f"物化子进程启动失败：{exc}（上一轮日志未动）"
        ) from exc
    finally:
        log_handle.close()  # 子进程持有自己的 fd，父侧句柄即可关闭
    os.replace(tmp_path, log_path)  # 子进程 fd 跟随 inode，继续写正式路径

    confirmed = await _confirm_child_took_lock(
        process,
        probe,
        log_path,
        timeout_s=confirm_timeout_s,
        poll_s=confirm_poll_s,
    )
    if process.poll() is None:
        # 回收僵尸并记录退出码（物化可能跑数小时，退出时服务早已不在栈上）
        threading.Thread(
            target=reap_process,
            args=(process, log_holder),
            name="rd-mined-materialize-reaper",
            daemon=True,
        ).start()
    log_holder.info(
        "RD 挖掘因子物化已启动：pid=%s log=%s argv=%s",
        process.pid,
        log_path,
        command,
    )
    return {
        "started": True,
        "confirmed": confirmed,
        "pid": process.pid,
        "log_path": str(log_path),
        "message": (
            "物化已确认在后台运行；完成后会自动刷新字段注册并发布训练目录"
            if confirmed
            else "物化进程存活但暂未确认持锁，面板会继续探测；请留意下方日志"
        ),
    }


async def _confirm_child_took_lock(
    process: subprocess.Popen,
    probe: Callable[[], bool],
    log_path: Path,
    *,
    timeout_s: float,
    poll_s: float,
) -> bool:
    """等子进程拿住独占锁（或退出）再回包；拿不到锁=别家在跑，如实 409。

    返回 True=确认持锁；False=进程存活但超时仍未确认（慢机器上导入卡顿等，
    不算失败——进程还活着，轮询会补上确认）。
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if probe():
            return True
        code = process.poll()
        if code is not None:
            process.wait()  # 回收已退出的子进程，避免僵尸
            if probe():
                raise MaterializeSpawnError(
                    409,
                    "已有物化进程在运行（本次子进程未取得独占锁，已退出，未写任何数据）",
                )
            raise MaterializeSpawnError(
                500, f"物化启动失败（退出码 {code}），详见日志：{log_path}"
            )
        await asyncio.sleep(poll_s)
    return False


def reap_process(process: Any, log: logging.Logger) -> None:
    """回收物化子进程并记录退出码（防僵尸；退出时调用方服务可能已重启）。"""
    code = process.wait()
    if code == 0:
        log.info("RD 挖掘因子物化进程正常退出（pid=%s）", process.pid)
    else:
        log.warning(
            "RD 挖掘因子物化进程退出码 %s（pid=%s），详见日志", code, process.pid
        )
