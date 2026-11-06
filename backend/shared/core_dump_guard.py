"""禁止进程产生 core dump（磁盘保护）。

宿主 ``kernel.core_pattern`` 是裸的 ``core``，内核把崩溃进程的 core 写进它的 **CWD**；
容器 CWD（``/app``）是**可写层**，``/app/backend`` 还是宿主仓库的 bind mount ——
一次崩溃就是 3~16GB：2026-09-21 曾把根分区写到 0 字节；2026-10-15 后端全量 pytest
SIGABRT 留下 15.9GB（直接落在仓库工作树里）；2026-11-06 一小时内 10 个
``multiprocessing.spawn`` 子进程 SIGSEGV 共 38GB。

原生栈（torch / OpenMP / qlib）的 core 诊断价值低——排障走日志、
``docker exec ... cat /proc/<pid>/maps``、``readelf -n`` 取 NT_PRSTATUS/NT_FILE；
真要取证就以 ``ulimit -c unlimited`` 直跑单实例（隔离探针模式）。

与 docker-compose 里 ``ulimits: core: 0`` 的分工：compose 的限额在**容器创建时**
生效，运行中的旧容器改不到；本模块覆盖「未重建」这段窗口，也覆盖手工
debug 容器与宿主直跑脚本。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def disable_core_dumps() -> None:
    """把当前进程的 ``RLIMIT_CORE`` 置 0（子进程 fork/spawn 后继承）。

    非 POSIX 或受限环境静默跳过——磁盘保护不值得阻断启动。
    """
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except Exception as exc:  # noqa: BLE001
        logger.debug("RLIMIT_CORE 置零失败（忽略）: %s", exc)
