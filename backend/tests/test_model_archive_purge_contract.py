"""已归档模型保留期清理的回归契约（BUG-12）。

背景：归档是软删除——`archive_model` 只把 `qm_user_models.status` 改成 'archived'，
磁盘目录（含 pred.parquet 全量历史分数）与 DB 行永久残留。本特性加每日 beat 任务
硬删除超过保留期者。

因为这是**无人值守的不可逆删除**，安全约束必须被测试锁住：
- 仍被策略绑定的模型跳过（否则策略会指向不存在的模型）
- readonly 系统模型跳过
- storage_path 必须位于 USER_MODELS_ROOT 之内（防路径污染导致误删）
- dry_run 不删任何东西
- 先删文件再删行（文件删失败则保留行，下一轮重试）

测试用 fake session 直接驱动真实 `purge_archived_models`，不依赖 DB。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend.shared import model_registry as registry_mod
from backend.shared.model_registry import model_registry_service

_BACKEND = Path(__file__).resolve().parents[1]
TASKS_PY = _BACKEND / "services" / "engine" / "tasks" / "celery_tasks.py"
CONFIG_PY = _BACKEND / "services" / "engine" / "qlib_app" / "celery_config.py"


class _FakeResult:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def mappings(self) -> _FakeResult:
        return self

    def all(self) -> list[dict]:
        return self._rows

    def first(self) -> dict | None:
        return self._rows[0] if self._rows else None


class _FakeSession:
    def __init__(self, state: dict) -> None:
        self._state = state

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def execute(self, stmt: object, params: dict | None = None) -> _FakeResult:
        sql = str(stmt)
        self._state["statements"].append(sql)
        # 顺序重要：DELETE 语句同时包含 "FROM qm_user_models"
        if "FROM qm_strategy_model_bindings" in sql:
            return _FakeResult(self._state["bindings"])
        if "DELETE FROM qm_user_models" in sql:
            self._state["deleted"].append((params or {}).get("model_id"))
            return _FakeResult([])
        if "FROM qm_user_models" in sql:
            return _FakeResult(self._state["candidates"])
        return _FakeResult([])


def _install_fake_db(monkeypatch: pytest.MonkeyPatch, state: dict) -> None:
    class _CM:
        async def __aenter__(self) -> _FakeSession:
            return _FakeSession(state)

        async def __aexit__(self, *exc: object) -> bool:
            return False

    monkeypatch.setattr(registry_mod, "get_session", lambda **kw: _CM())


@pytest.fixture
def sandbox(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """构造 root 目录 + 一个已归档模型目录，并把 user_models_root 指向 root。"""
    root = tmp_path / "users"
    model_dir = root / "default" / "10000001" / "mdl_expired"
    model_dir.mkdir(parents=True)
    (model_dir / "pred.parquet").write_bytes(b"x" * 64)
    monkeypatch.setattr(model_registry_service, "user_models_root", root)

    state: dict = {
        "candidates": [],
        "bindings": [],
        "deleted": [],
        "statements": [],
    }
    _install_fake_db(monkeypatch, state)
    return root, model_dir, state


def _candidate(model_dir: Path, model_id: str = "mdl_expired", metadata: dict | None = None) -> dict:
    return {
        "tenant_id": "default",
        "user_id": "10000001",
        "model_id": model_id,
        "storage_path": str(model_dir),
        "metadata_json": metadata or {},
    }


@pytest.mark.asyncio
async def test_purge_removes_expired_model_and_files(sandbox) -> None:
    root, model_dir, state = sandbox
    state["candidates"] = [_candidate(model_dir)]

    result = await model_registry_service.purge_archived_models(retention_days=7)

    assert result["purged"] == 1
    assert result["failed"] == []
    assert not model_dir.exists(), "磁盘目录必须被删除"
    assert state["deleted"] == ["mdl_expired"], "DB 行必须被删除"
    assert root.exists(), "root 本身不能被删"


@pytest.mark.asyncio
async def test_purge_skips_strategy_bound_model(sandbox) -> None:
    """仍被策略绑定的模型绝不能删，否则策略指向不存在的模型。"""
    _root, model_dir, state = sandbox
    state["candidates"] = [_candidate(model_dir)]
    state["bindings"] = [{"model_id": "mdl_expired"}]

    result = await model_registry_service.purge_archived_models(retention_days=7)

    assert result["purged"] == 0
    assert result["skipped_referenced"] == ["mdl_expired"]
    assert model_dir.exists(), "被引用的模型目录不能被删"
    assert state["deleted"] == [], "被引用的模型 DB 行不能被删"


@pytest.mark.asyncio
async def test_purge_skips_readonly_system_model(sandbox) -> None:
    _root, model_dir, state = sandbox
    state["candidates"] = [_candidate(model_dir, metadata={"readonly": True})]

    result = await model_registry_service.purge_archived_models(retention_days=7)

    assert result["purged"] == 0
    assert result["skipped_readonly"] == ["mdl_expired"]
    assert model_dir.exists()
    assert state["deleted"] == []


@pytest.mark.asyncio
async def test_purge_refuses_path_outside_root(sandbox, tmp_path: Path) -> None:
    """storage_path 被污染成 root 之外时拒绝删除，防误删任意目录。"""
    _root, _model_dir, state = sandbox
    outside = tmp_path / "elsewhere" / "important"
    outside.mkdir(parents=True)
    (outside / "keep.txt").write_text("do not delete", encoding="utf-8")
    state["candidates"] = [
        {
            "tenant_id": "default",
            "user_id": "10000001",
            "model_id": "mdl_evil",
            "storage_path": str(outside),
            "metadata_json": {},
        }
    ]

    result = await model_registry_service.purge_archived_models(retention_days=7)

    assert result["purged"] == 0
    assert len(result["failed"]) == 1
    assert "outside user_models_root" in result["failed"][0]["error"]
    assert outside.exists(), "root 之外的目录绝不能被删"
    assert state["deleted"] == []


@pytest.mark.asyncio
async def test_purge_refuses_root_itself(sandbox) -> None:
    """storage_path 指向 root 本身时必须拒绝（否则等于清空所有用户模型）。"""
    root, _model_dir, state = sandbox
    state["candidates"] = [
        {
            "tenant_id": "default",
            "user_id": "10000001",
            "model_id": "mdl_root",
            "storage_path": str(root),
            "metadata_json": {},
        }
    ]

    result = await model_registry_service.purge_archived_models(retention_days=7)

    assert result["purged"] == 0
    assert len(result["failed"]) == 1
    assert root.exists(), "root 不能被删"


@pytest.mark.asyncio
async def test_purge_dry_run_deletes_nothing(sandbox) -> None:
    _root, model_dir, state = sandbox
    state["candidates"] = [_candidate(model_dir)]

    result = await model_registry_service.purge_archived_models(
        retention_days=7, dry_run=True
    )

    assert result["dry_run"] is True
    assert result["purged"] == 1, "dry_run 仍应报告将要删除的内容"
    assert model_dir.exists(), "dry_run 不能删文件"
    assert state["deleted"] == [], "dry_run 不能删 DB 行"


@pytest.mark.asyncio
async def test_purge_keeps_row_when_file_removal_fails(
    sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    """文件删失败必须保留 DB 行，让下一轮重试（否则会留下孤儿文件）。"""
    _root, model_dir, state = sandbox
    state["candidates"] = [_candidate(model_dir)]

    def _boom(*_a: object, **_kw: object) -> None:
        raise OSError("device busy")

    monkeypatch.setattr(registry_mod.shutil, "rmtree", _boom)

    result = await model_registry_service.purge_archived_models(retention_days=7)

    assert result["purged"] == 0
    assert len(result["failed"]) == 1
    assert "rmtree failed" in result["failed"][0]["error"]
    assert state["deleted"] == [], "文件未删成功时不能删 DB 行"


@pytest.mark.asyncio
async def test_purge_rejects_negative_retention(sandbox) -> None:
    with pytest.raises(ValueError):
        await model_registry_service.purge_archived_models(retention_days=-1)


@pytest.mark.asyncio
async def test_purge_only_targets_archived_rows(sandbox) -> None:
    """候选查询必须限定 status='archived'，绝不能碰在用模型。"""
    _root, model_dir, state = sandbox
    state["candidates"] = [_candidate(model_dir)]

    await model_registry_service.purge_archived_models(retention_days=7)

    select_sql = next(
        s for s in state["statements"] if "FROM qm_user_models" in s and "DELETE" not in s
    )
    assert "status = 'archived'" in select_sql
    assert "updated_at < :cutoff" in select_sql


# --- 接线守卫：任务与调度必须成对存在 ---


def test_celery_task_registered_with_expected_name() -> None:
    text = TASKS_PY.read_text(encoding="utf-8")
    assert 'name="engine.tasks.purge_archived_models"' in text
    assert "def purge_archived_models_task(" in text


def test_beat_schedule_wires_the_task_daily() -> None:
    text = CONFIG_PY.read_text(encoding="utf-8")
    assert '"purge-archived-models-daily"' in text
    assert '"task": "engine.tasks.purge_archived_models"' in text
    # 每日一次
    assert re.search(r'crontab\(minute="30",\s*hour="3"\)', text), "应为每日 03:30"


def test_beat_schedule_exposes_both_env_knobs() -> None:
    text = CONFIG_PY.read_text(encoding="utf-8")
    assert "MODEL_ARCHIVE_PURGE_ENABLED" in text, "缺少总开关"
    assert "MODEL_ARCHIVE_RETENTION_DAYS" in text, "缺少保留期配置"


def test_beat_task_does_not_collide_with_existing_slots() -> None:
    """03:30 需避开 02:30 质量回填与 04:00-05:50 每 10 分钟的快照。"""
    text = CONFIG_PY.read_text(encoding="utf-8")
    assert 'crontab(minute="30", hour="2"' in text, "02:30 已被推理质量回填占用"
    assert 'crontab(minute="*/10", hour="4-5"' in text, "04:00-05:50 已被快照占用"
    assert 'crontab(minute="30", hour="3")' in text
