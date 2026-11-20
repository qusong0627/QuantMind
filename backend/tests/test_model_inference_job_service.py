"""串行补推任务编排与持久化单元测试。

- ``ModelInferenceJobPersistence``：DDL 自愈、JSON 解析、白名单更新、
  活跃任务冲突（含唯一索引竞态）、取消语义、心跳超时自愈；
- ``model_inference_job_service``：参数归一、ETA 中位数、预览排除/告警、
  创建校验、零缺口即完成、后台执行终态（完成/失败/异常）；
- ``_ProgressWriter`` 与 ``_job_status`` 的状态折算。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from backend.services.engine.inference.model_backfill import (
    BackfillCoverage,
    BackfillModel,
    ModelRunResult,
    SerialResult,
)
from backend.services.engine.services import model_inference_job_persistence as mp
from backend.services.engine.services import model_inference_job_service as js


def _run(coro):
    return asyncio.run(coro)


# ── 假 DB ─────────────────────────────────────────────────────────────────


class _Result:
    def __init__(self, value=None, rowcount: int = -1) -> None:
        self._value = value
        self.rowcount = rowcount

    def mappings(self):
        return self

    def first(self):
        return self._value

    def all(self):
        if isinstance(self._value, list):
            return self._value
        return [] if self._value is None else [self._value]


class _Session:
    """按顺序消费预置结果；同时记录 SQL 与参数。"""

    def __init__(self, results=None) -> None:
        self.results = list(results or [])
        self.executed: list[tuple[str, dict | None]] = []
        self.raise_on: list[Exception | None] = []

    async def execute(self, statement, params=None):
        self.executed.append((str(statement), params))
        if self.raise_on:
            error = self.raise_on.pop(0)
            if error is not None:
                raise error
        value = self.results.pop(0) if self.results else None
        return value if isinstance(value, _Result) else _Result(value)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _patch_session(monkeypatch, module, session):
    @asynccontextmanager
    async def fake_get_session(*_args, **_kwargs):
        yield session

    monkeypatch.setattr(module, "get_session", fake_get_session)


def _row(**overrides) -> dict:
    payload = {
        "job_id": "job_1",
        "tenant_id": "default",
        "user_id": "u1",
        "status": "running",
        "model_ids": ["m1"],
        "request": {},
        "model_results": [],
        "logs": [],
    }
    payload.update(overrides)
    return payload


# ── 持久化 ────────────────────────────────────────────────────────────────


def test_ensure_tables_creates_table_and_indexes(monkeypatch):
    session = _Session()
    _patch_session(monkeypatch, mp, session)
    _run(mp.ModelInferenceJobPersistence().ensure_tables())
    statements = "\n".join(sql for sql, _ in session.executed)
    assert "CREATE TABLE IF NOT EXISTS qm_model_inference_jobs" in statements
    assert "uq_qm_inference_job_active" in statements
    assert "idx_qm_inference_jobs_owner_created" in statements
    assert "idx_qm_inference_jobs_status_heartbeat" in statements


#: PostgreSQL 里这类「无括号函数式关键字」不能当裸列名：建表直接报
#: ``syntax error at or near "current_date"``。SQLAlchemy 的 RESERVED_WORDS
#: 不收录它们，所以这里显式列出来做回归闸门。
_PG_FUNCTION_KEYWORDS = frozenset(
    {
        "current_date",
        "current_time",
        "current_timestamp",
        "localtime",
        "localtimestamp",
        "current_user",
        "session_user",
        "current_role",
        "current_catalog",
        "current_schema",
        "user",
    }
)


def _ddl_columns() -> list[str]:
    ddl = mp._DDL[0]
    body = ddl.split("(", 1)[1].rsplit(")", 1)[0]
    return [
        line.strip().rstrip(",").split()[0]
        for line in body.splitlines()
        if line.strip().rstrip(",")
    ]


def test_ddl_columns_avoid_postgres_function_keywords():
    columns = _ddl_columns()
    # 任务进度列必须是合规名：2026-10-21 线上启动失败就是 current_date 建表被拒
    assert "current_trade_date" in columns
    assert sorted(set(columns) & _PG_FUNCTION_KEYWORDS) == []


def test_row_parses_json_strings_and_tolerates_garbage():
    parsed = mp.ModelInferenceJobPersistence._row(
        {
            "job_id": "j",
            "model_ids": '["m1","m2"]',
            "request": {"mode": "gaps"},
            "model_results": None,
            "logs": "not-json",
        }
    )
    assert parsed["model_ids"] == ["m1", "m2"]
    assert parsed["request"] == {"mode": "gaps"}
    assert parsed["model_results"] is None
    assert parsed["logs"] == "not-json"
    assert mp.ModelInferenceJobPersistence._row(None) is None


def test_get_job_and_active_job(monkeypatch):
    session = _Session([_row(), None])
    _patch_session(monkeypatch, mp, session)
    persistence = mp.ModelInferenceJobPersistence()
    found = _run(persistence.get_job("job_1", tenant_id="default", user_id="u1"))
    assert found["job_id"] == "job_1"
    assert _run(persistence.get_job("none", tenant_id="default", user_id="u1")) is None
    assert _run(persistence.active_job(tenant_id="default", user_id="u1")) is None


def test_list_jobs_clamps_limit(monkeypatch):
    session = _Session([[_row(), _row(job_id="job_2")]])
    _patch_session(monkeypatch, mp, session)
    jobs = _run(
        mp.ModelInferenceJobPersistence().list_jobs(
            tenant_id="default", user_id="u1", limit=10_000
        )
    )
    assert [job["job_id"] for job in jobs] == ["job_1", "job_2"]
    assert session.executed[0][1]["limit"] == 100


def test_create_job_rejects_existing_active(monkeypatch):
    session = _Session([_row(job_id="active")])
    _patch_session(monkeypatch, mp, session)
    with pytest.raises(mp.ActiveJobExistsError) as exc:
        _run(
            mp.ModelInferenceJobPersistence().create_job(
                job_id="new",
                tenant_id="default",
                user_id="u1",
                model_ids=["m1"],
                request={},
                total_models=1,
                total_days=3,
            )
        )
    assert exc.value.job_id == "active"
    assert exc.value.job is not None and exc.value.job["job_id"] == "active"
    assert len(session.executed) == 1


def test_create_job_inserts_json_and_reads_back(monkeypatch):
    session = _Session([None, None, _row(job_id="new")])
    _patch_session(monkeypatch, mp, session)
    created = _run(
        mp.ModelInferenceJobPersistence().create_job(
            job_id="new",
            tenant_id="default",
            user_id="u1",
            model_ids=["m1", "m2"],
            request={"mode": "gaps"},
            total_models=2,
            total_days=5,
        )
    )
    assert created["job_id"] == "new"
    insert_sql, params = session.executed[1]
    assert "CAST(:model_ids AS JSONB)" in insert_sql
    assert params["model_ids"] == '["m1", "m2"]'
    assert params["request"] == '{"mode": "gaps"}'
    assert params["total_days"] == 5


def test_create_job_maps_integrity_error_to_active_conflict(monkeypatch):
    # 预检查 active 无行 → INSERT 撞唯一索引 → 重新查询拿到赢家
    session = _Session([None, _row(job_id="winner")])
    _patch_session(monkeypatch, mp, session)
    session.raise_on = [None, IntegrityError("INSERT", {}, Exception("dup"))]
    with pytest.raises(mp.ActiveJobExistsError) as exc:
        _run(
            mp.ModelInferenceJobPersistence().create_job(
                job_id="loser",
                tenant_id="default",
                user_id="u1",
                model_ids=["m1"],
                request={},
                total_models=1,
                total_days=1,
            )
        )
    assert exc.value.job_id == "winner"
    assert exc.value.job is not None and exc.value.job["job_id"] == "winner"


def test_update_job_whitelists_columns_and_casts_json(monkeypatch):
    session = _Session()
    _patch_session(monkeypatch, mp, session)
    _run(
        mp.ModelInferenceJobPersistence().update_job(
            "job_1",
            status="running",
            model_results=[{"model_id": "m1"}],
            logs=["a"],
            model_id="hijack",
            tenant_id="other",
        )
    )
    sql, params = session.executed[0]
    assert "status = :status" in sql
    assert "model_results = CAST(:model_results AS JSONB)" in sql
    assert "updated_at = NOW()" in sql
    assert "model_id =" not in sql and "tenant_id =" not in sql
    assert params["model_results"] == '[{"model_id": "m1"}]'
    assert params["logs"] == '["a"]'
    assert "hijack" not in str(params)


def test_update_job_noop_when_nothing_allowed(monkeypatch):
    session = _Session()
    _patch_session(monkeypatch, mp, session)
    _run(mp.ModelInferenceJobPersistence().update_job("job_1", nope=1))
    assert session.executed == []


def test_request_cancel_missing_and_terminal(monkeypatch):
    session = _Session([None, _row(status="completed")])
    _patch_session(monkeypatch, mp, session)
    persistence = mp.ModelInferenceJobPersistence()
    assert _run(persistence.request_cancel("missing", tenant_id="default", user_id="u1")) is None
    terminal = _run(persistence.request_cancel("job_1", tenant_id="default", user_id="u1"))
    assert terminal["status"] == "completed"
    assert len(session.executed) == 2  # 两次读，无更新


def test_request_cancel_queued_finalizes_immediately(monkeypatch):
    session = _Session([_row(status="queued"), None, _row(status="cancelled")])
    _patch_session(monkeypatch, mp, session)
    job = _run(
        mp.ModelInferenceJobPersistence().request_cancel(
            "job_1", tenant_id="default", user_id="u1"
        )
    )
    assert job["status"] == "cancelled"
    update_sql, params = session.executed[1]
    assert "status = :status" in update_sql
    assert params["status"] == "cancelled"
    assert params["cancel_requested"] is True
    assert params["finished_at"] is not None


def test_request_cancel_running_only_flags(monkeypatch):
    session = _Session([_row(status="running"), None, _row(status="running")])
    _patch_session(monkeypatch, mp, session)
    _run(
        mp.ModelInferenceJobPersistence().request_cancel(
            "job_1", tenant_id="default", user_id="u1"
        )
    )
    _, params = session.executed[1]
    assert params == {"job_id": "job_1", "cancel_requested": True}


def test_reap_stale_returns_rowcount_and_clamps_timeout(monkeypatch):
    session = _Session([_Result(rowcount=3)])
    _patch_session(monkeypatch, mp, session)
    reaped = _run(
        mp.ModelInferenceJobPersistence().reap_stale(
            tenant_id="default", user_id="u1", heartbeat_timeout_seconds=0
        )
    )
    assert reaped == 3
    sql, params = session.executed[0]
    assert "COALESCE(heartbeat_at, created_at)" in sql
    assert params["timeout"] == 1


# ── 预览与创建 ────────────────────────────────────────────────────────────


def _model_row(tmp_path, model_id="m1", **overrides) -> dict:
    payload = {
        "model_id": model_id,
        "status": "ready",
        "storage_path": str(tmp_path),
        "metadata_json": {"display_name": model_id.upper(), "model_type": "lightgbm"},
    }
    payload.update(overrides)
    return payload


def _coverage(gaps=(), **overrides) -> BackfillCoverage:
    payload = {
        "min_date": "2026-10-01",
        "max_date": "2026-10-12",
        "gap_dates": tuple(gaps),
        "latest_trade_date": "2026-10-21",
        "count": 3,
    }
    payload.update(overrides)
    return BackfillCoverage(**payload)


def test_normalize_model_ids_dedupes_and_validates():
    assert js.normalize_model_ids([" m1 ", "", "m1", "m2"]) == ["m1", "m2"]
    assert js.normalize_model_ids(["m1"], min_models=1) == ["m1"]
    with pytest.raises(js.JobValidationError) as few:
        js.normalize_model_ids(["m1"])
    assert few.value.code == "too_few_models"
    # 2026-10-23：请求上限与**可补推**上限分离——勾选全表（含归档）不再 400，
    # 归档/未就绪在预览里排除后只按 eligible 计 20 个。
    with pytest.raises(js.JobValidationError) as many:
        js.normalize_model_ids([f"m{i}" for i in range(js.MAX_REQUESTED_MODELS + 1)])
    assert many.value.code == "too_many_models"


def test_build_preview_allows_raw_selection_beyond_job_cap(monkeypatch, tmp_path):
    """勾选 MAX_MODELS+1（1 个归档）→ 可补推仍只有 MAX_MODELS 个，预览必须成功。

    事故口径（2026-10-23）：模型治理「全选」50 行（多为归档）直接撞
    `最多选择 20 个模型`，用户看到 [object Object] + 空预览。
    """
    rows = [_model_row(tmp_path, f"m{i}") for i in range(js.MAX_MODELS)]
    rows.append(_model_row(tmp_path, "old", status="archived"))
    _patch_registry(monkeypatch, rows)
    monkeypatch.setattr(js, "estimate_seconds_by_model", _async_none)
    monkeypatch.setattr(js, "compute_coverage", lambda model: _coverage(["2026-10-13"]))

    preview = _run(
        js.build_preview(
            tenant_id="default",
            user_id="u1",
            model_ids=[f"m{i}" for i in range(js.MAX_MODELS)] + ["old"],
        )
    )
    assert len(preview.eligible_entries) == js.MAX_MODELS
    assert any(entry.reason == "archived" for entry in preview.entries)


def test_build_preview_rejects_more_than_max_eligible(monkeypatch, tmp_path):
    """真正可补推 >20 才拒，并给出可执行文案（分批）。"""
    rows = [_model_row(tmp_path, f"m{i}") for i in range(js.MAX_MODELS + 1)]
    _patch_registry(monkeypatch, rows)
    monkeypatch.setattr(js, "estimate_seconds_by_model", _async_none)
    monkeypatch.setattr(js, "compute_coverage", lambda model: _coverage(["2026-10-13"]))

    with pytest.raises(js.JobValidationError) as exc:
        _run(
            js.build_preview(
                tenant_id="default",
                user_id="u1",
                model_ids=[f"m{i}" for i in range(js.MAX_MODELS + 1)],
            )
        )
    assert exc.value.code == "too_many_models"
    assert f"可补推模型最多 {js.MAX_MODELS} 个" in exc.value.message
    assert "分批" in exc.value.message


def test_estimate_seconds_by_model_uses_median(monkeypatch):
    rows = [
        {"model_id": "m1", "duration_ms": 60_000},
        {"model_id": "m1", "duration_ms": 120_000},
        {"model_id": "m1", "duration_ms": 180_000},
        {"model_id": "m2", "duration_ms": 30_000},
    ]
    session = _Session([rows])
    _patch_session(monkeypatch, js, session)
    estimates = _run(js.estimate_seconds_by_model("default", "u1", ["m1", "m2"]))
    assert estimates == {"m1": 120.0, "m2": 30.0}
    assert _run(js.estimate_seconds_by_model("default", "u1", [])) == {}


def test_estimate_seconds_by_model_degrades_on_db_error(monkeypatch):
    class BoomSession(_Session):
        async def execute(self, statement, params=None):
            raise RuntimeError("db down")

    _patch_session(monkeypatch, js, BoomSession())
    assert _run(js.estimate_seconds_by_model("default", "u1", ["m1"])) == {}


def _patch_registry(monkeypatch, rows):
    async def fake_list_models(**_kwargs):
        return rows

    monkeypatch.setattr(js.model_registry_service, "list_models", fake_list_models)


def test_build_preview_excludes_and_warns(monkeypatch, tmp_path):
    _patch_registry(
        monkeypatch,
        [
            _model_row(tmp_path, "m1"),
            _model_row(tmp_path, "m2", storage_path=str(tmp_path / "gone")),
            _model_row(tmp_path, "old", status="archived"),
        ],
    )

    async def fake_estimates(*_args):
        return {"m1": 10.0}

    monkeypatch.setattr(js, "estimate_seconds_by_model", fake_estimates)

    def fake_coverage(model):
        if model.model_id == "m1":
            return _coverage(["2026-10-13", "2026-10-14"])
        raise RuntimeError("parquet 损坏")

    monkeypatch.setattr(js, "compute_coverage", fake_coverage)
    preview = _run(
        js.build_preview(
            tenant_id="default",
            user_id="u1",
            model_ids=["m1", "m2", "old", "sys-x", "ghost"],
        )
    )
    payload = preview.as_dict()
    reasons = {item["model_id"]: item["reason"] for item in payload["models"]}
    assert reasons["m1"] is None
    assert reasons["m2"] == "missing_storage"
    assert reasons["old"] == "archived"
    assert {item["model_id"]: item["reason"] for item in payload["excluded"]} == {
        "sys-x": "system_readonly",
        "ghost": "not_found",
    }
    assert payload["totals"]["days"] == 2
    # 10 秒/日 × 2 日 = 20 秒
    assert payload["totals"]["estimated_seconds"] == 20
    warnings = "\n".join(payload["warnings"])
    assert "系统模型只读" in warnings
    assert "未就绪或无产物" in warnings
    assert "串行执行" in warnings


def test_build_preview_coverage_failure_is_per_model(monkeypatch, tmp_path):
    _patch_registry(monkeypatch, [_model_row(tmp_path, "m1"), _model_row(tmp_path, "m2")])
    monkeypatch.setattr(js, "estimate_seconds_by_model", _async_none)

    def fake_coverage(model):
        if model.model_id == "m1":
            raise RuntimeError("duckdb 炸了")
        return _coverage()

    monkeypatch.setattr(js, "compute_coverage", fake_coverage)
    preview = _run(
        js.build_preview(tenant_id="default", user_id="u1", model_ids=["m1", "m2"])
    )
    entries = {entry.model.model_id: entry for entry in preview.entries}
    assert entries["m1"].eligible is False
    assert entries["m1"].reason == "coverage_failed"
    assert entries["m2"].eligible is True


async def _async_none(*_args, **_kwargs):
    return {}


def test_build_preview_warns_gap_too_large_and_multi_market(monkeypatch, tmp_path):
    _patch_registry(
        monkeypatch,
        [
            _model_row(tmp_path, "m1"),
            _model_row(
                tmp_path,
                "m2",
                metadata_json={"context": {"market": "HK"}, "display_name": "M2"},
            ),
        ],
    )
    monkeypatch.setattr(js, "estimate_seconds_by_model", _async_none)
    monkeypatch.setattr(
        js,
        "compute_coverage",
        lambda model: _coverage(["d"] * (js.MAX_GAP_DAYS_PER_MODEL + 1)),
    )
    preview = _run(
        js.build_preview(tenant_id="default", user_id="u1", model_ids=["m1", "m2"])
    )
    warnings = "\n".join(preview.warnings)
    assert "缺口超过" in warnings
    assert "跨市场" in warnings
    # 个别模型违规只排除自己，不拖死整批：reason 在表格里可见
    entries = {entry.model.model_id: entry for entry in preview.entries}
    assert entries["m1"].eligible is False
    assert entries["m1"].reason == "gap_too_large"
    assert entries["m2"].eligible is False
    assert entries["m2"].reason == "market_mismatch"
    assert preview.eligible_entries == []


class _FakePersistenceForCreate:
    def __init__(self, job=None, raise_error: Exception | None = None) -> None:
        self.created: dict | None = None
        self.updates: list[tuple[str, dict]] = []
        self.reaped: list[dict] = []
        self.job = job or _row(job_id="job_new", status="queued")
        self.raise_error = raise_error

    async def reap_stale(self, **kwargs):
        self.reaped.append(kwargs)
        return 1

    async def create_job(self, **kwargs):
        if self.raise_error is not None:
            raise self.raise_error
        self.created = kwargs
        self.job = dict(self.job, job_id=kwargs["job_id"])
        return self.job

    async def update_job(self, job_id, **fields):
        self.updates.append((job_id, fields))

    async def get_job(self, job_id, **_kwargs):
        return dict(self.job, **{k: v for _, fields in self.updates for k, v in fields.items()})


def _entry(model_id="m1", *, market="CN", gaps=2, seconds=10.0):
    model = BackfillModel(
        model_id=model_id,
        display_name=model_id.upper(),
        model_type="lightgbm",
        market=market,
        target_horizon_days=10,
        status="ready",
        storage_path="/tmp",
    )
    return js.PreviewEntry(
        model=model,
        coverage=_coverage(["2026-10-13"] * gaps),
        eligible=True,
        reason=None,
        seconds_per_day=seconds,
        estimated_seconds=seconds * gaps,
    )


def _preview(entries):
    return js.PreviewResult(entries=list(entries))


def _patch_preview(monkeypatch, preview):
    async def fake_preview(**_kwargs):
        return preview

    monkeypatch.setattr(js, "build_preview", fake_preview)


def test_create_job_rejects_no_eligible_models(monkeypatch):
    _patch_preview(monkeypatch, _preview([]))
    with pytest.raises(js.JobValidationError) as exc:
        _run(
            js.create_job(
                tenant_id="default",
                user_id="u1",
                model_ids=["m1", "m2"],
                persistence=_FakePersistenceForCreate(),
            )
        )
    assert exc.value.code == "no_eligible_models"


def test_create_job_rejects_market_mismatch_and_large_gap(monkeypatch):
    persistence = _FakePersistenceForCreate()
    _patch_preview(
        monkeypatch, _preview([_entry("m1"), _entry("m2", market="HK")])
    )
    with pytest.raises(js.JobValidationError) as mismatch:
        _run(
            js.create_job(
                tenant_id="default",
                user_id="u1",
                model_ids=["m1", "m2"],
                persistence=persistence,
            )
        )
    assert mismatch.value.code == "market_mismatch"

    _patch_preview(
        monkeypatch,
        _preview([_entry("m1", gaps=js.MAX_GAP_DAYS_PER_MODEL + 1), _entry("m2")]),
    )
    with pytest.raises(js.JobValidationError) as too_large:
        _run(
            js.create_job(
                tenant_id="default",
                user_id="u1",
                model_ids=["m1", "m2"],
                persistence=persistence,
            )
        )
    assert too_large.value.code == "gap_too_large"


def test_create_job_maps_active_conflict(monkeypatch):
    persistence = _FakePersistenceForCreate(
        raise_error=mp.ActiveJobExistsError("other")
    )
    _patch_preview(monkeypatch, _preview([_entry("m1"), _entry("m2")]))
    with pytest.raises(js.JobValidationError) as exc:
        _run(
            js.create_job(
                tenant_id="default",
                user_id="u1",
                model_ids=["m1", "m2"],
                persistence=persistence,
            )
        )
    assert exc.value.code == "active_job_exists"


def test_describe_active_job_names_model_progress_and_eta():
    started = datetime(2026, 11, 17, 2, 0, tzinfo=timezone.utc)
    job = {
        "job_id": "job_run",
        "status": "running",
        "model_ids": ["m1"],
        "current_model_id": "m1",
        "model_results": [
            {"model_id": "m1", "display_name": "T+5 跨库300因子 CatBoost_CN"}
        ],
        "total_days": 17,
        "completed_days": 8,
        "failed_days": 0,
        "skipped_days": 0,
        "started_at": started,
    }
    message = js.describe_active_job(
        job, now=started + timedelta(minutes=16)
    )
    assert "T+5 跨库300因子 CatBoost_CN" in message
    assert "8/17 日" in message
    assert "剩 9 日" in message
    assert "约 18 分钟" in message
    assert "job_id=job_run" in message


def test_describe_active_job_queued_and_missing_job():
    queued = js.describe_active_job(
        {"job_id": "job_q", "status": "queued", "model_ids": ["m9"], "total_days": 0}
    )
    assert "排队中" in queued
    assert "m9" in queued
    # 只有 job_id、没有任务行时退回旧文案，不能抛异常
    assert js.describe_active_job(None, job_id="job_x") == (
        "已有补推任务在执行（job_id=job_x）"
    )


def test_create_job_active_conflict_message_carries_active_job(monkeypatch):
    started = datetime.now(timezone.utc) - timedelta(minutes=16)
    active = {
        "job_id": "other",
        "status": "running",
        "model_ids": ["m1"],
        "current_model_id": "m1",
        "model_results": [{"model_id": "m1", "display_name": "L1L2 优选模型"}],
        "total_days": 17,
        "completed_days": 8,
        "started_at": started,
    }
    persistence = _FakePersistenceForCreate(
        raise_error=mp.ActiveJobExistsError("other", job=active)
    )
    _patch_preview(monkeypatch, _preview([_entry("m1"), _entry("m2")]))
    with pytest.raises(js.JobValidationError) as exc:
        _run(
            js.create_job(
                tenant_id="default",
                user_id="u1",
                model_ids=["m1", "m2"],
                persistence=persistence,
            )
        )
    assert exc.value.code == "active_job_exists"
    assert "L1L2 优选模型" in exc.value.message
    assert "8/17 日" in exc.value.message
    assert "剩 9 日" in exc.value.message


def test_create_job_zero_gap_marks_completed_without_runner(monkeypatch):
    persistence = _FakePersistenceForCreate()
    _patch_preview(monkeypatch, _preview([_entry("m1", gaps=0), _entry("m2", gaps=0)]))
    monkeypatch.setattr(
        js, "start_job_runner", lambda *a, **k: pytest.fail("零缺口不启动后台任务")
    )
    job = _run(
        js.create_job(
            tenant_id="default",
            user_id="u1",
            model_ids=["m1", "m2"],
            persistence=persistence,
        )
    )
    assert job["status"] == "completed"
    assert persistence.updates[0][1]["status"] == "completed"
    assert persistence.created["total_days"] == 0


def test_create_job_starts_runner_when_days_present(monkeypatch):
    persistence = _FakePersistenceForCreate()
    _patch_preview(monkeypatch, _preview([_entry("m1"), _entry("m2")]))
    started: list[str] = []
    monkeypatch.setattr(
        js, "start_job_runner", lambda job_id, **kwargs: started.append(job_id)
    )
    job = _run(
        js.create_job(
            tenant_id="default",
            user_id="u1",
            model_ids=["m1", "m2"],
            persistence=persistence,
        )
    )
    assert started == [job["job_id"]]
    assert persistence.created["model_ids"] == ["m1", "m2"]


def test_create_job_reaps_stale_jobs_before_create(monkeypatch):
    """建单前必须回收心跳死任务，否则陈旧 running 行会永久占住唯一索引。

    旧单模型端点（「推理管理」页）只轮询状态、不读 jobs 列表端点（reap 原本
    只挂在那里），进程重启后整页推理入口会被一条死任务堵死。
    """
    persistence = _FakePersistenceForCreate()
    _patch_preview(monkeypatch, _preview([_entry("m1"), _entry("m2")]))
    monkeypatch.setattr(js, "start_job_runner", lambda *a, **k: None)
    _run(
        js.create_job(
            tenant_id="default",
            user_id="u1",
            model_ids=["m1", "m2"],
            persistence=persistence,
        )
    )
    assert persistence.reaped == [
        {
            "tenant_id": "default",
            "user_id": "u1",
            "heartbeat_timeout_seconds": js.REAP_TIMEOUT_SECONDS,
        }
    ]
    assert persistence.created is not None


def test_job_status_branches():
    assert js._job_status(
        SerialResult((), 0, 0, 0, cancelled=True), total_days=5
    )[0] == "cancelled"
    assert js._job_status(SerialResult((), 0, 0, 0, cancelled=False), 0)[0] == "completed"
    completed = SerialResult((), 2, 0, 0, cancelled=False)
    assert js._job_status(completed, 2) == ("completed", None)
    failed = SerialResult((), 0, 2, 0, cancelled=False)
    assert js._job_status(failed, 2) == ("failed", "全部 2 日补推失败")
    partial = SerialResult((), 1, 0, 1, cancelled=False)
    status, message = js._job_status(partial, 2)
    assert status == "partial"
    assert "1/2 日成功" in message and "稍后重跑" in message


# ── 后台执行 ──────────────────────────────────────────────────────────────


class _FakeRunnerPersistence:
    def __init__(self, job: dict) -> None:
        self.job = job
        self.updates: list[tuple[str, dict]] = []

    async def get_job(self, job_id, **_kwargs):
        return dict(self.job)

    async def claim_running(self, job_id):
        # 真实实现是 queued → running 的条件迁移；已终结（如取消）时不得覆盖
        if self.job.get("status") != "queued":
            return False
        self.updates.append((job_id, {"status": "running"}))
        self.job["status"] = "running"
        return True

    async def update_job(self, job_id, **fields):
        self.updates.append((job_id, fields))
        self.job.update(fields)


def _serial_result(*, completed=2, failed=0, skipped=0) -> SerialResult:
    summary = ModelRunResult(
        "m1",
        "M1",
        status="completed",
        days_total=completed + failed + skipped,
        days_completed=completed,
        days_failed=failed,
        days_skipped=skipped,
        days_merged=completed,
        gap_dates=["2026-10-13", "2026-10-14"],
    )
    return SerialResult((summary,), completed, failed, skipped, cancelled=False)


def _patch_run_job_deps(monkeypatch, tmp_path, *, result=None, raise_error=None):
    model = BackfillModel(
        model_id="m1",
        display_name="M1",
        model_type="lightgbm",
        market="CN",
        target_horizon_days=10,
        status="ready",
        storage_path=str(tmp_path),
    )

    async def fake_resolve(*_args):
        return model

    async def fake_run_serial(*_args, **kwargs):
        if raise_error is not None:
            raise raise_error
        # 钉住回调契约：真实 run_serial 会 await 两个回调，同步实现会在这里抛
        # TypeError（曾导致所有补推任务在第一日前直接失败）
        skip = kwargs.get("should_skip_model")
        assert skip is not None
        assert await skip("m1") is False
        cancel = kwargs.get("should_cancel")
        assert cancel is not None
        assert await cancel() is False
        return result

    monkeypatch.setattr(js, "resolve_backfill_model", fake_resolve)
    monkeypatch.setattr(js, "ineligibility_reason", lambda _model: None)
    monkeypatch.setattr(
        js,
        "compute_coverage",
        lambda _model: _coverage(["2026-10-13", "2026-10-14"]),
    )
    monkeypatch.setattr(js, "run_serial", fake_run_serial)
    monkeypatch.setattr(js, "create_redis_client", lambda: None)
    monkeypatch.setattr(js, "HEARTBEAT_SECONDS", 3600)
    monkeypatch.setattr(
        js.batch_inference_orchestrator, "is_running", lambda *a, **k: False
    )


def test_run_job_completes_and_writes_terminal_state(monkeypatch, tmp_path):
    persistence = _FakeRunnerPersistence(
        _row(job_id="job_1", status="queued", model_ids=["m1"])
    )
    _patch_run_job_deps(monkeypatch, tmp_path, result=_serial_result())
    _run(js._run_job("job_1", tenant_id="default", user_id="u1", persistence=persistence))
    statuses = [fields.get("status") for _, fields in persistence.updates if "status" in fields]
    assert statuses == ["running", "completed"]
    final = persistence.updates[-1][1]
    assert final["completed_days"] == 2
    assert final["model_results"][0]["model_id"] == "m1"


def test_run_job_stops_when_cancel_wins_the_queued_race(monkeypatch, tmp_path):
    persistence = _FakeRunnerPersistence(
        _row(job_id="job_1", status="queued", model_ids=["m1"])
    )
    _patch_run_job_deps(monkeypatch, tmp_path, result=_serial_result())

    async def cancel_won(_job_id):
        return False  # 读到 queued 后、写 running 前被取消

    monkeypatch.setattr(persistence, "claim_running", cancel_won)
    _run(js._run_job("job_1", tenant_id="default", user_id="u1", persistence=persistence))

    # 不得再写 running/终态：cancel 落下的 cancelled 是最终结论
    assert persistence.updates == []


def test_run_job_returns_when_not_queued(monkeypatch, tmp_path):
    persistence = _FakeRunnerPersistence(_row(status="completed"))
    _patch_run_job_deps(monkeypatch, tmp_path, result=_serial_result())
    _run(js._run_job("job_1", tenant_id="default", user_id="u1", persistence=persistence))
    assert persistence.updates == []


def test_run_job_fails_when_no_eligible_pairs(monkeypatch, tmp_path):
    persistence = _FakeRunnerPersistence(
        _row(job_id="job_1", status="queued", model_ids=["m1"])
    )
    _patch_run_job_deps(monkeypatch, tmp_path, result=_serial_result())

    async def none_resolve(*_args):
        return None

    monkeypatch.setattr(js, "resolve_backfill_model", none_resolve)
    _run(js._run_job("job_1", tenant_id="default", user_id="u1", persistence=persistence))
    final = persistence.updates[-1][1]
    assert final["status"] == "failed"
    assert final["error"] == "没有可补推的模型"


def test_run_job_exception_lands_failed(monkeypatch, tmp_path):
    persistence = _FakeRunnerPersistence(
        _row(job_id="job_1", status="queued", model_ids=["m1"])
    )
    _patch_run_job_deps(monkeypatch, tmp_path, raise_error=RuntimeError("引擎崩了"))
    _run(js._run_job("job_1", tenant_id="default", user_id="u1", persistence=persistence))
    final = persistence.updates[-1][1]
    assert final["status"] == "failed"
    assert "引擎崩了" in final["error"]


def test_progress_writer_covers_started_lock_and_failure_logs():
    class FakePersistence:
        def __init__(self) -> None:
            self.writes: list[dict] = []

        async def update_job(self, job_id, **fields):
            self.writes.append(fields)

    persistence = FakePersistence()
    writer = js._ProgressWriter(
        job_id="j1",
        tenant_id="default",
        user_id="u1",
        persistence=persistence,
        summaries=[{"model_id": "m1", "days_completed": 0}],
    )

    async def scenario():
        await writer(
            {
                "type": "model_started",
                "model": {"model_id": "m1", "display_name": "M1", "status": "running"},
            }
        )
        await writer(
            {
                "type": "day",
                "model_id": "m1",
                "day": "2026-10-13",
                "result": {"status": "skipped_lock"},
                "model": {"model_id": "m1", "display_name": "M1", "status": "running"},
            }
        )
        await writer(
            {
                "type": "day",
                "model_id": "m1",
                "day": "2026-10-14",
                "result": {"status": "failed", "error": "坏数据"},
                "model": {"model_id": "m1", "display_name": "M1", "status": "running"},
            }
        )
        await writer({"type": "unknown"})

    _run(scenario())
    logs = "\n".join(writer.logs)
    assert "跳过：同日推理锁被占用" in logs
    assert "失败：坏数据" in logs
    # model_started 强制写、两次非 completed 日强制写
    assert len(persistence.writes) == 3
    assert persistence.writes[0]["current_model_id"] == "m1"
