"""RD 挖掘因子物化的后台运维面（状态 + 启动）。

背景：物化器 ``backend/scripts/rd_mined_materialize.py`` 此前只有 CLI 入口，
挖掘自动挂接（``RD_AGENT_AUTO_MATERIALIZE``）之外的补跑/全量回填只能上机器敲
命令。本模块把「看状态」与「触发一次物化（含目录注册）」搬进后台页面：

- ``GET  /materialize/status``：flock 探测运行态 + 候选分桶（与真实物化同一
  判定实现，见 ``materialize_overview``）+ 清单统计 + 库面/目录状态 + 日志尾。
- ``POST /materialize/start``：忙（探测到锁被占）→ 409 拒绝；否则固定 argv
  子进程后台跑（``--register``），回包前确认子进程真正拿住锁（拿不到/退出
  如实报 409/500，不假装 started），日志经 ``.tmp`` 原子换名后返回 pid/路径。

**刻意没有停止端点**：中途 kill 会在 ``rd_mined`` 库留下「部分分区已有该列」
的中间态（读取层无 union_by_name，列漂移会响亮失败，只能靠收尾对齐修复）。
让一次物化跑到收尾对齐/清单落盘，永远比中断它便宜；重复点击由 409 挡住。
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from backend.services.api.user_app.middleware.auth import require_admin

router = APIRouter(dependencies=[Depends(require_admin)])
logger = logging.getLogger(__name__)

# 进程内启动闩：探测→子进程拿锁之间有 ~4s 解释器启动窗口（实测冷启 3.8~4.3s），
# 期间再来的 POST 会看到「未运行」。非阻塞拿闩，第二个 POST 直接 409 让位。
_START_GUARD = threading.Lock()
_START_CONFIRM_TIMEOUT_S = 15.0
_START_CONFIRM_POLL_S = 0.25


@router.get("/materialize/status", summary="RD 挖掘因子物化状态（后台面板）")
async def rd_mined_materialize_status() -> dict[str, Any]:
    """物化运行态 + 待办分桶 + 日志尾。

    无业务写入，也不跑建表迁移（候选查询显式 ``ensure=False``）——这是一条
    会被面板每 10s 轮询的只读路径，不允许携带 DDL 副作用。DB/库面任何一段
    读不出来都降级成 error 字段返回：运行态与日志尾恰恰是故障时最要看的。
    """
    from backend.scripts.rd_mined_materialize import (
        materialize_overview,
        probe_run_lock,
        tail_web_log,
    )

    return {
        "running": probe_run_lock(),
        "overview": await materialize_overview(),
        "log": tail_web_log(),
    }


@router.post("/materialize/start", summary="启动一次 RD 挖掘因子物化（后台）")
async def start_rd_mined_materialize() -> dict[str, Any]:
    """启动物化子进程（固定 argv，无用户输入），完成后自动注册训练目录。

    并发：``started=true`` 的含义是「已确认有一个物化进程真的在跑」——
    flock 探测只是快速拒绝，子进程要 ~4s 启动完才拿锁，所以回包前要等锁
    被拿住（或子进程退出并如实报错）。期间用进程内启动闩挡住第二个 POST，
    免得两个请求各自起一个子进程、后起的截掉先起者的日志。
    """
    from backend.scripts.rd_mined_materialize import probe_run_lock

    if probe_run_lock():
        raise HTTPException(
            status_code=409,
            detail="已有物化进程在运行，本次未启动（物化独占同一座库，避免并发写坏分区）",
        )
    if not _START_GUARD.acquire(blocking=False):
        raise HTTPException(
            status_code=409, detail="上一次启动确认尚未完成，请稍后重试"
        )
    try:
        return await _spawn_and_confirm()
    finally:
        _START_GUARD.release()


async def _spawn_and_confirm() -> dict[str, Any]:
    """起子进程 → 确认锁被拿住 → 起回收线程 → 回包（失败一律抛 HTTPException）。"""
    from backend.scripts.rd_mined_materialize import (
        _web_log_path,
        build_run_command,
        probe_run_lock,
        project_root,
    )

    log_path = _web_log_path()
    if log_path.is_symlink():
        raise HTTPException(
            status_code=500,
            detail=f"物化日志路径 {log_path} 是符号链接，拒绝写入（防止覆盖任意文件）",
        )
    command = build_run_command()
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
        raise HTTPException(
            status_code=500, detail=f"物化日志文件不可写：{tmp_path}（{exc}）"
        ) from exc

    log_handle = os.fdopen(fd, "w", encoding="utf-8")
    try:
        process = subprocess.Popen(  # noqa: S603 - 固定 argv 常量，无外部输入
            command,
            cwd=str(project_root()),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # 脱离 API 进程组：api 子进程重启不牵连物化
            env=os.environ.copy(),
        )
    except OSError as exc:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=500, detail=f"物化子进程启动失败：{exc}（上一轮日志未动）"
        ) from exc
    finally:
        log_handle.close()  # 子进程持有自己的 fd，父侧句柄即可关闭
    os.replace(tmp_path, log_path)  # 子进程 fd 跟随 inode，继续写正式路径

    confirmed = await _confirm_child_took_lock(process, probe_run_lock, log_path)
    if process.poll() is None:
        # 回收僵尸并记录退出码（物化可能跑数小时，退出时 API 早已不在栈上）
        threading.Thread(
            target=_reap_materialize_process,
            args=(process, logger),
            name="rd-mined-materialize-reaper",
            daemon=True,
        ).start()
    logger.info(
        "RD 挖掘因子物化已启动：pid=%s log=%s argv=%s",
        process.pid,
        log_path,
        command,
    )
    return {
        "started": True,
        "pid": process.pid,
        "log_path": str(log_path),
        "message": (
            "物化已确认在后台运行；完成后会自动刷新字段注册并发布训练目录"
            if confirmed
            else "物化进程存活但暂未确认持锁，面板会继续探测；请留意下方日志"
        ),
    }


async def _confirm_child_took_lock(
    process: subprocess.Popen, probe: Any, log_path: Path
) -> bool:
    """等子进程拿住独占锁（或退出）再回包；拿不到锁=别家在跑，如实 409。

    返回 True=确认持锁；False=进程存活但超时仍未确认（慢机器上导入卡顿等，
    不算失败——进程还活着，面板轮询会补上确认）。
    """
    deadline = time.monotonic() + _START_CONFIRM_TIMEOUT_S
    while time.monotonic() < deadline:
        if probe():
            return True
        code = process.poll()
        if code is not None:
            process.wait()  # 回收已退出的子进程，避免僵尸
            if probe():
                raise HTTPException(
                    status_code=409,
                    detail="已有物化进程在运行（本次子进程未取得独占锁，已退出，未写任何数据）",
                )
            raise HTTPException(
                status_code=500,
                detail=f"物化启动失败（退出码 {code}），详见日志：{log_path}",
            )
        await asyncio.sleep(_START_CONFIRM_POLL_S)
    return False


def _reap_materialize_process(process: subprocess.Popen, log: logging.Logger) -> None:
    code = process.wait()
    if code == 0:
        log.info("RD 挖掘因子物化进程正常退出（pid=%s）", process.pid)
    else:
        log.warning(
            "RD 挖掘因子物化进程退出码 %s（pid=%s），详见日志", code, process.pid
        )
