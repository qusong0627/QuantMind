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

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from backend.services.api.user_app.middleware.auth import require_admin
from backend.shared.rd_mined_materialize_launch import (
    START_GUARD,
    MaterializeSpawnError,
    reap_process,
    spawn_materialize,
)

router = APIRouter(dependencies=[Depends(require_admin)])
logger = logging.getLogger(__name__)

# 进程内启动闩：探测→子进程拿锁之间有 ~4s 解释器启动窗口（实测冷启 3.8~4.3s），
# 期间再来的 POST 会看到「未运行」。非阻塞拿闩，第二个 POST 直接 409 让位。
# 与用户自助物化面（alpha_agent 路由）共用 shared 里的同一把闩。
_START_GUARD = START_GUARD
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
    """起子进程 → 确认锁被拿住 → 起回收线程 → 回包（失败一律抛 HTTPException）。

    启动/确认/换名/回收纪律在 ``backend.shared.rd_mined_materialize_launch``
    （与用户自助物化面共用同一实现）；本函数只负责取路径与常量、把
    ``MaterializeSpawnError`` 映射成 HTTPException。
    """
    from backend.scripts.rd_mined_materialize import (
        _web_log_path,
        build_run_command,
        probe_run_lock,
        project_root,
    )

    try:
        return await spawn_materialize(
            command=build_run_command(),
            log_path=_web_log_path(),
            cwd=project_root(),
            probe=probe_run_lock,
            log_holder=logger,
            confirm_timeout_s=_START_CONFIRM_TIMEOUT_S,
            confirm_poll_s=_START_CONFIRM_POLL_S,
        )
    except MaterializeSpawnError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def _reap_materialize_process(process: Any, log: logging.Logger) -> None:
    reap_process(process, log)
