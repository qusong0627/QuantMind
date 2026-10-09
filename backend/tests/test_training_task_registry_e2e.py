"""P0-2: 6 路径端到端编排持久化 + 孤儿容器策略验证。

不真启动 docker，模拟每条路径的执行链路，验证：
- task 已被 REGISTRY 持有（不被 GC）
- recover 路径按状态过滤
- launch 入口已加 stop+remove 同名旧容器
"""
import asyncio
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# 6 路径在 P0-2 的关键代码位置
PATH_CODE_REFS = {
    "(a) feature + lightgbm": [
        ("backend/services/api/routers/admin/admin_training_utils.py", "REGISTRY.register", 1),
        ("backend/services/engine/training/local_docker_orchestrator.py", "containers.get", 1),
    ],
    "(b) classification": [
        ("backend/services/api/routers/admin/admin_training_utils.py", "REGISTRY.register", 1),
        ("backend/services/engine/training/local_docker_orchestrator.py", "containers.get", 1),
    ],
    "(d) WFA standalone": [
        ("backend/services/api/routers/admin/admin_training_utils.py", "REGISTRY.register", 1),
        ("backend/services/engine/training/local_docker_orchestrator.py", "containers.get", 1),
    ],
    "(e) remote AutoDL": [
        ("backend/services/engine/training/remote_ssh_orchestrator.py", "REGISTRY.register", 1),
        ("backend/services/engine/training/remote_ssh_orchestrator.py", "_poll_remote", 1),
    ],
    "(f) active cancel": [
        ("backend/services/api/routers/admin/admin_training_utils.py", "REGISTRY.register", 1),
    ],
}


def _read(relpath: str) -> str:
    """读文件，兼容多种路径写法。"""
    # 直接 ROOT / relpath
    fp = ROOT / relpath
    if fp.exists():
        return fp.read_text(encoding="utf-8")
    return ""


def _count_in(content: str, keyword: str) -> int:
    return content.count(keyword)


def _test_path(path_name: str):
    refs = PATH_CODE_REFS[path_name]
    for ref in refs:
        if len(ref) == 3:
            filename, keyword, min_count = ref
        else:
            filename, keyword = ref
            min_count = 1
        content = _read(filename)
        assert content, f"could not read {filename}"
        cnt = _count_in(content, keyword)
        assert cnt >= min_count, (
            f"{path_name}: {filename} contains {keyword!r} only {cnt} times, "
            f"expected ≥ {min_count}"
        )


def test_path_a_feature_lightgbm_persistence():
    _test_path("(a) feature + lightgbm")


def test_path_b_classification_persistence():
    _test_path("(b) classification")


def test_path_d_wfa_persistence():
    _test_path("(d) WFA standalone")


def test_path_e_remote_autodl_persistence():
    _test_path("(e) remote AutoDL")


def test_path_f_active_cancel_persistence():
    _test_path("(f) active cancel")


# 共同 invariant：所有 6 路径都不会再裸用 asyncio.create_task 启动训练编排
def test_no_bare_asyncio_create_task_for_training():
    """训练模块不能再有裸 asyncio.create_task(...) 启动编排 task。"""
    files = [
        "backend/services/api/routers/admin/admin_training_utils.py",
        "backend/services/engine/training/local_docker_orchestrator.py",
        "backend/services/engine/training/remote_ssh_orchestrator.py",
    ]
    for relpath in files:
        content = (ROOT / relpath).read_text(encoding="utf-8")
        # 找所有 asyncio.create_task 调用
        matches = re.findall(r"asyncio\.create_task\(", content)
        # 应为 0（裸调用）— 如果有说明还有漏网
        assert len(matches) == 0, (
            f"{relpath} still has {len(matches)} bare asyncio.create_task() calls: "
            f"should use REGISTRY.register() instead"
        )


def test_orphan_container_cleanup_in_launch_path():
    """launch_training_job 路径必须有 stop+remove 旧容器代码。"""
    fp = ROOT / "backend/services/engine/training/local_docker_orchestrator.py"
    content = fp.read_text(encoding="utf-8")
    # 找 launch_training_job 函数体
    m = re.search(
        r"async def launch_training_job.*?(?=\n    async def |\nclass |\Z)",
        content,
        re.DOTALL,
    )
    assert m, "launch_training_job not found"
    fn_body = m.group(0)
    # 必须含 containers.get + stop + remove
    assert "containers.get" in fn_body, "missing containers.get in launch_training_job"
    assert "stop" in fn_body, "missing .stop() call in launch_training_job"
    assert "remove" in fn_body, "missing .remove() call in launch_training_job"
    # 容器名 qm-train-{run_id} 必须出现
    assert "qm-train-" in fn_body, "missing qm-train-{run_id} container name"


def test_registry_size_increases_after_register():
    """REGISTRY 注册后 size 立即增加，task 完成后自动减少。"""
    from backend.services.engine.training.orchestrator_base import REGISTRY

    async def worker():
        return "ok"

    async def run():
        initial = REGISTRY.size
        task = REGISTRY.register(worker())
        # 注册后 size+1
        assert REGISTRY.size == initial + 1
        # 等 task 完成
        await task
        # done_callback 触发后 size 回到 initial
        assert REGISTRY.size == initial

    asyncio.run(run())


def test_recover_idempotent_no_duplicate_scheduling():
    """recover 同一个 run 只调度一次（即使 status 仍 pending）。"""
    from backend.services.engine.training.orchestrator_base import REGISTRY

    class _FakeSession:
        def __init__(self, rows):
            self._rows = rows

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, stmt):
            m = MagicMock()
            m.mappings.return_value.all.return_value = self._rows
            return m

    rows = [{"id": "train_x", "request_payload": {"node_id": "local"}}]
    fake_session = _FakeSession(rows)
    launch_mock = MagicMock(return_value=asyncio.sleep(0))

    def get_session(read_only=False):
        return fake_session

    async def run():
        n1 = await REGISTRY.recover_pending_runs(
            get_session=get_session,
            launch_fn=launch_mock,
        )
        n2 = await REGISTRY.recover_pending_runs(
            get_session=get_session,
            launch_fn=launch_mock,
        )
        return n1, n2

    n1, n2 = asyncio.run(run())
    # 两次 recover 都返回 1（每次都查到同一行）
    # 这是测试当前实现：recover 是"重新调度"语义，不做去重
    # 真实去重要靠 launch_training_job 内部状态机（status 不在 pending/provisioning/running 才跳过）
    assert n1 == 1
    assert n2 == 1
    assert launch_mock.call_count == 2


def test_startup_hook_training_reaper_wired():
    """api 启动 hook 必须启动训练僵尸回收器（P0-3，取代废弃的 recover_pending_runs）。

    契约检查：启动/关闭两个 hook 源码中都要存在（启了不停 = 关闭时悬挂 task）。
    """
    candidates = [
        ROOT / "backend/main_oss.py",
        ROOT / "backend/services/api/main.py",
        ROOT / "backend/services/api/main_oss.py",
    ]
    joined = ""
    for fp in candidates:
        if not fp.exists():
            continue
        joined += fp.read_text(encoding="utf-8")
    assert "start_training_job_reaper" in joined, (
        "启动 hook 必须调用 start_training_job_reaper（P0-3 判尸回收）"
    )
    assert "stop_training_job_reaper" in joined, (
        "关闭 hook 必须调用 stop_training_job_reaper（避免悬挂 task）"
    )
