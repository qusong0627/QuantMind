"""因子挖掘 CoSTEER 经验记忆（知识库）环境配置。

RD-Agent 的 CoSTEER 会把「因子代码怎么写才能过评测」的经验存进一张语义图谱，
在演化过程中逐步累积。但默认两个路径设置都是 None，后果是：

  - `dump_knowledge_base()` 只打一行 warning 就跳过（"Dump knowledge base path is
    not set, skip dumping."）→ 学到的经验**进程退出即丢**
  - 记忆**不跨任务累积**，每个挖掘任务都从零开始

打开记忆只需两个变量，但**必须成对设置且用绝对路径**，见 `knowledge_base_env`。

变量名的出处（已在容器内实测核对，不是照抄文档）：

  rdagent/components/coder/factor_coder/config.py:11   env_prefix="FACTOR_CoSTEER_"
  rdagent/components/coder/CoSTEER/config.py:30,33      knowledge_base_path / new_knowledge_base_path
  rdagent/components/coder/factor_coder/__init__.py:19-23
      FactorCoSTEER 把 FACTOR_COSTEER_SETTINGS（而非基类 CoSTEER_SETTINGS）
      交给 CoSTEER.__init__，所以前缀是 FACTOR_CoSTEER_，不是 CoSTEER_。

读写语义（`evolving_agent.py:186-191` 每个演化步跑一次）：

  KNOWLEDGE_BASE_PATH      构造期读一次（load_or_init_knowledge_base）
  NEW_KNOWLEDGE_BASE_PATH  每步 load → generate → dump 循环读写

两者指向同一文件即「累积式记忆」。本模块默认如此。
"""

from __future__ import annotations

import os

DEFAULT_KB_PATH = "/data/rd_agent_kb/coasteer_kb.pkl"
"""默认落在 compose 的持久卷 /data 下（容器重建不丢），且不在共享/第三方可写目录
——本地是裸 pickle 加载（上游 #1471 才加签名校验），不要指向别人能写的位置。"""


def _require_absolute(path: str, var: str) -> str:
    """拒绝相对路径。

    挖掘子进程的 cwd 是**每任务独立**的 task_log_dir（launcher 的 Popen cwd=），
    相对路径会让知识库落进任务日志目录、下个任务读不到——记忆静默失效，
    而日志里只会看到「正常」的 KB 读写。宁可在这里响亮地失败。
    """
    if not os.path.isabs(path):
        raise ValueError(
            f"{var} 必须是绝对路径，收到 {path!r}。挖掘子进程的 cwd 是每任务独立的"
            "日志目录，相对路径会让知识库落进任务日志里、下个任务读不到。"
        )
    return path


def _env_or(key: str, default: str) -> str:
    return (os.getenv(key) or "").strip() or default


def knowledge_base_env() -> dict[str, str]:
    """返回要注入挖掘子进程的 CoSTEER 记忆变量。

    只返回覆盖项，不改动调用方已有的 env（便于单测）。
    """
    kb_path = _require_absolute(
        _env_or("FACTOR_CoSTEER_KNOWLEDGE_BASE_PATH", DEFAULT_KB_PATH),
        "FACTOR_CoSTEER_KNOWLEDGE_BASE_PATH",
    )
    write_path = _require_absolute(
        _env_or("FACTOR_CoSTEER_NEW_KNOWLEDGE_BASE_PATH", kb_path),
        "FACTOR_CoSTEER_NEW_KNOWLEDGE_BASE_PATH",
    )
    lock_path = _require_absolute(
        _env_or("FACTOR_CoSTEER_FILELOCK_PATH", f"{kb_path}.lock"),
        "FACTOR_CoSTEER_FILELOCK_PATH",
    )
    return {
        "FACTOR_CoSTEER_KNOWLEDGE_BASE_PATH": kb_path,
        "FACTOR_CoSTEER_NEW_KNOWLEDGE_BASE_PATH": write_path,
        # 并发挖掘任务各自 load→generate→dump 同一文件，不开锁就是「后写覆盖先写」：
        # 先完成任务学到的经验会被静默丢掉。
        "FACTOR_CoSTEER_ENABLE_FILELOCK": _env_or("FACTOR_CoSTEER_ENABLE_FILELOCK", "true"),
        "FACTOR_CoSTEER_FILELOCK_PATH": lock_path,
    }
