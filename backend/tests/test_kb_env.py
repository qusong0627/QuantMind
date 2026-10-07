"""CoSTEER 经验记忆（知识库）环境配置测试。

批次 1b 打开知识库路径。RD-Agent 默认把 `knowledge_base_path` /
`new_knowledge_base_path` 留成 None，于是 `dump_knowledge_base()` 只打一行
warning 就跳过，学到的经验进程退出即丢。

这里锁定三条契约：
  1. 读写路径成对出现，默认指向同一文件（这才构成「累积式记忆」）
  2. **相对路径必须报错**——挖掘子进程的 cwd 是每任务独立的 task_log_dir，
     相对路径会让 KB 落进任务日志目录、下个任务读不到，而日志一切正常
  3. filelock 默认开启——并发任务各自 load→generate→dump 同一文件，
     不开锁就是「后写覆盖先写」，先完成那批的经验被静默丢掉
"""

from __future__ import annotations

import pytest

from backend.services.engine.rd_agent.kb_env import (
    DEFAULT_KB_PATH,
    knowledge_base_env,
)

KB_VARS = (
    "FACTOR_CoSTEER_KNOWLEDGE_BASE_PATH",
    "FACTOR_CoSTEER_NEW_KNOWLEDGE_BASE_PATH",
    "FACTOR_CoSTEER_FILELOCK_PATH",
    "FACTOR_CoSTEER_ENABLE_FILELOCK",
)


@pytest.fixture
def clean_kb_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """隔离宿主环境变量，确保用例断言的是默认值而非本机残留。"""
    for key in KB_VARS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_defaults_land_on_persistent_volume(clean_kb_env) -> None:
    """默认路径在 /data 下（compose 持久卷），容器重建不丢。"""
    env = knowledge_base_env()
    assert env["FACTOR_CoSTEER_KNOWLEDGE_BASE_PATH"] == DEFAULT_KB_PATH
    assert DEFAULT_KB_PATH.startswith("/data/")


def test_read_and_write_default_to_same_file(clean_kb_env) -> None:
    """读=写 才是累积式记忆：这一步学到的，下一步/下个任务能读到。"""
    env = knowledge_base_env()
    assert (
        env["FACTOR_CoSTEER_NEW_KNOWLEDGE_BASE_PATH"]
        == env["FACTOR_CoSTEER_KNOWLEDGE_BASE_PATH"]
    )


def test_relative_read_path_is_rejected(clean_kb_env) -> None:
    """相对路径必须响亮失败，而不是让记忆静默失效。"""
    clean_kb_env.setenv("FACTOR_CoSTEER_KNOWLEDGE_BASE_PATH", "coasteer_kb.pkl")
    with pytest.raises(ValueError, match="必须是绝对路径"):
        knowledge_base_env()


def test_relative_write_path_is_rejected(clean_kb_env) -> None:
    """只把写路径设成相对同样要拦——它会落进每任务独立的日志目录。"""
    clean_kb_env.setenv("FACTOR_CoSTEER_KNOWLEDGE_BASE_PATH", "/data/kb.pkl")
    clean_kb_env.setenv("FACTOR_CoSTEER_NEW_KNOWLEDGE_BASE_PATH", "out/kb.pkl")
    with pytest.raises(ValueError, match="必须是绝对路径"):
        knowledge_base_env()


def test_filelock_enabled_by_default(clean_kb_env) -> None:
    env = knowledge_base_env()
    assert env["FACTOR_CoSTEER_ENABLE_FILELOCK"] == "true"


def test_lock_path_derives_from_kb_path(clean_kb_env) -> None:
    """换 KB 路径时锁要跟着走，否则两个库共用一个锁（或都不锁）。"""
    clean_kb_env.setenv("FACTOR_CoSTEER_KNOWLEDGE_BASE_PATH", "/data/other/kb.pkl")
    env = knowledge_base_env()
    assert env["FACTOR_CoSTEER_FILELOCK_PATH"] == "/data/other/kb.pkl.lock"


def test_explicit_env_overrides_win(clean_kb_env) -> None:
    clean_kb_env.setenv("FACTOR_CoSTEER_KNOWLEDGE_BASE_PATH", "/data/a.pkl")
    clean_kb_env.setenv("FACTOR_CoSTEER_NEW_KNOWLEDGE_BASE_PATH", "/data/b.pkl")
    clean_kb_env.setenv("FACTOR_CoSTEER_ENABLE_FILELOCK", "false")
    env = knowledge_base_env()
    assert env["FACTOR_CoSTEER_KNOWLEDGE_BASE_PATH"] == "/data/a.pkl"
    assert env["FACTOR_CoSTEER_NEW_KNOWLEDGE_BASE_PATH"] == "/data/b.pkl"
    assert env["FACTOR_CoSTEER_ENABLE_FILELOCK"] == "false"


def test_write_path_may_diverge_from_read(clean_kb_env) -> None:
    """想把新知识另存一份时，只设 NEW_ 即可（读路径仍跟默认）。"""
    clean_kb_env.setenv("FACTOR_CoSTEER_NEW_KNOWLEDGE_BASE_PATH", "/data/new.pkl")
    env = knowledge_base_env()
    assert env["FACTOR_CoSTEER_KNOWLEDGE_BASE_PATH"] == DEFAULT_KB_PATH
    assert env["FACTOR_CoSTEER_NEW_KNOWLEDGE_BASE_PATH"] == "/data/new.pkl"


def test_does_not_mutate_caller_env(clean_kb_env) -> None:
    """只返回覆盖项，不动调用方的 dict（便于与既有 env 合并）。"""
    caller = {"OPENAI_API_KEY": "sk-chat"}
    snapshot = dict(caller)
    knowledge_base_env()
    assert caller == snapshot


def test_blank_value_falls_back_to_default(clean_kb_env) -> None:
    """空串等同未设置——否则会走到 `os.path.isabs("")` 的边界。"""
    clean_kb_env.setenv("FACTOR_CoSTEER_KNOWLEDGE_BASE_PATH", "   ")
    env = knowledge_base_env()
    assert env["FACTOR_CoSTEER_KNOWLEDGE_BASE_PATH"] == DEFAULT_KB_PATH
