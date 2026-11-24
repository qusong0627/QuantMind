"""自动推理膨胀防线的回归护栏。

背景：客户反馈「系统反复推理，把 PG 撑到几十 GB」。根因不是一处，而是四件事叠加：

1. 「某一天是否已经推理过」的唯一凭证是模型目录 ``pred.parquet``，而推理落库
   先 commit、parquet 合并在循环之外且失败只写日志 —— 只要合并没成功或进程被杀，
   缺口就永不闭合，Beat 工作日 6-16 点每 30 分钟再推一遍；
2. ``run_id`` 每次执行都是新 UUID，``engine_signal_scores`` 唯一键含 ``run_id``，
   跨 run 删除又已被移除，于是「重推同一天」是净新增整份全市场截面；
3. 保留期清理被 ``model_version='inference_script' + 本次 feature_version`` 绑死，
   且默认 730 天 —— 换过一次模型，旧 feature_version 的整块历史就再也没有删除路径；
4. ``qm_research_candidate_snapshot`` 完全没有时间维度清理。

静态护栏部分按仓库既有做法（``test_inference_persistence_contract.py``）读源码断言；
行为部分直接跑 ``backfill_model_gaps``——被测函数是在函数体内部
``from x import Y`` 取符号的，所以必须 patch **源模块属性**才有效。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from backend.services.engine.inference import (
    backfill_guard,
    gap_backfill,
    pred_merge,
    script_runner,
)

_BACKEND = Path(__file__).resolve().parents[1]


def _backend_src(rel: str) -> str:
    return (_BACKEND / rel).read_text(encoding="utf-8")


GAP_BACKFILL = "services/engine/inference/gap_backfill.py"
SCRIPT_RUNNER = "services/engine/inference/script_runner.py"
BACKFILL_GUARD = "services/engine/inference/backfill_guard.py"
CELERY_CONFIG = "services/engine/qlib_app/celery_config.py"
JANITOR = "services/engine/services/signal_storage_janitor.py"


async def _no_record(**kwargs) -> None:
    return None


def _patch_common(monkeypatch) -> None:
    """把补全链路的外部依赖收敛成可控替身（模型来源解析 + 推理历史落库）。"""
    monkeypatch.setattr(
        gap_backfill,
        "_resolve_backfill_model_lineage",
        lambda **k: ("m", "user_default"),
    )
    monkeypatch.setattr(gap_backfill, "_mark_failed_run", _no_record)
    monkeypatch.setattr(gap_backfill, "_mark_success_run", _no_record)


class _OkRunResult:
    success = True
    run_id = "run_20261120_deadbeef"
    signals_count = 2
    signals = [{"symbol": "600036", "score": 0.5}, {"symbol": "000001", "score": 0.2}]
    error = ""
    failure_stage = ""


# ---------------------------------------------------------------------------
# 1. 缺口判定必须以「已落库分数」为准，而不是只看 pred.parquet
# ---------------------------------------------------------------------------


def test_backfill_checks_db_before_reinferring():
    """一键补全/Beat 必须先查库里有没有该日分数，有就不再推理。"""
    src = _backend_src(GAP_BACKFILL)
    assert "from backend.services.engine.inference.backfill_guard import (" in src
    assert "covered_dates_from_db(" in src
    assert "scores_for_dates(" in src
    # 已有分数的日期必须从 gaps 里摘掉，不能只是「跳过落库」
    assert "gaps = [d for d in gaps if d not in _recovered_dates]" in src
    # 守卫查询以候选快照为准：它同时带 model_id 与 data_trade_date
    # （engine_signal_scores 只有 T+1 生效日，映射不回数据日）
    guard = _backend_src(BACKFILL_GUARD)
    assert "qm_research_candidate_snapshot" in guard
    assert "data_trade_date" in guard
    assert "model_id" in guard


def test_backfill_reuses_db_scores_without_inferring(tmp_path, monkeypatch):
    """库里已有分数：一次都不推理，只把已落库分数回写 pred.parquet。"""
    calls: list[str] = []

    class _ForbiddenRunner:
        def __init__(self, **kwargs):
            pass

        def execute(self, **kwargs):
            calls.append(str(kwargs.get("date")))
            raise AssertionError("已有落库分数，不该再推理")

    gaps = ["2026-11-20", "2026-11-21"]
    _patch_common(monkeypatch)
    monkeypatch.setattr(script_runner, "InferenceScriptRunner", _ForbiddenRunner)
    monkeypatch.setattr(
        backfill_guard,
        "covered_dates_from_db",
        lambda **kw: dict.fromkeys(kw["dates"], 5200),
    )
    monkeypatch.setattr(
        backfill_guard,
        "scores_for_dates",
        lambda **kw: [
            (
                d,
                [
                    {"symbol": "600036", "score": 0.5},
                    {"symbol": "000001", "score": 0.1},
                ],
            )
            for d in kw["dates"]
        ],
    )

    parquet_file = tmp_path / "pred.parquet"
    pred_merge.merge_signals_into_pred(
        parquet_file,
        [("2026-11-19", [{"symbol": "600036", "score": 0.9}])],
        create_if_missing=True,
    )

    result = asyncio.run(
        gap_backfill.backfill_model_gaps(
            tenant_id="t",
            user_id="u",
            model_id="m",
            storage_path=str(tmp_path),
            gaps=list(gaps),
        )
    )
    assert calls == []
    assert result["status"] == "completed"
    assert result["appended"] == 0
    assert result["rebuilt"] == 2
    assert result["merge_error"] == ""
    assert result["failed"] == 0

    import duckdb

    con = duckdb.connect()
    try:
        days = {
            str(r[0])[:10]
            for r in con.execute(
                f"SELECT DISTINCT trade_date FROM read_parquet('{parquet_file}')"
            ).fetchall()
        }
    finally:
        con.close()
    assert set(gaps) <= days, "已落库分数应回写进 pred.parquet，真正把缺口闭合"


def test_guard_helpers_degrade_without_database(monkeypatch):
    """守卫拿不到 DB 时必须返回「无证据」，绝不能因此阻断正常补全。"""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert backfill_guard._sync_session() == (None, None)
    assert (
        backfill_guard.covered_dates_from_db(
            tenant_id="default", user_id="u", model_id="m", dates=["2026-11-24"]
        )
        == {}
    )
    assert (
        backfill_guard.scores_for_dates(
            tenant_id="default", user_id="u", model_id="m", dates=[]
        )
        == []
    )
    assert backfill_guard.scores_for_date(
        tenant_id="", user_id="u", model_id="m", data_trade_date="2026-11-24"
    ) == ("", [])


# ---------------------------------------------------------------------------
# 2. 单轮工作量与落盘节奏必须有上限；合并失败不得伪装成功
# ---------------------------------------------------------------------------


def test_backfill_caps_days_per_run(tmp_path, monkeypatch):
    """单轮真实推理天数受 INFERENCE_BACKFILL_MAX_DAYS_PER_RUN 约束。"""
    calls: list[str] = []

    class _CountingRunner:
        def __init__(self, **kwargs):
            pass

        def execute(self, **kwargs):
            calls.append(str(kwargs.get("date")))
            raise RuntimeError("stop")

    _patch_common(monkeypatch)
    monkeypatch.setattr(script_runner, "InferenceScriptRunner", _CountingRunner)
    monkeypatch.setattr(backfill_guard, "covered_dates_from_db", lambda **kw: {})
    monkeypatch.setenv("INFERENCE_BACKFILL_MAX_DAYS_PER_RUN", "2")

    result = asyncio.run(
        gap_backfill.backfill_model_gaps(
            tenant_id="t",
            user_id="u",
            model_id="m",
            storage_path=str(tmp_path),
            gaps=["2026-11-18", "2026-11-19", "2026-11-20", "2026-11-21"],
        )
    )
    assert len(calls) == 2
    assert result["capped"] is True
    assert result["gap"] == 2
    assert result["gap_requested"] == 4


def test_backfill_flushes_pred_parquet_incrementally():
    """分批落盘：循环内周期性 flush，收尾再 flush，进程被杀不作废已完成的日期。"""
    src = _backend_src(GAP_BACKFILL)
    assert "INFERENCE_BACKFILL_MERGE_EVERY_DAYS" in src
    assert "if (idx + 1) % _merge_every == 0:" in src
    assert src.count("_flush_pred()") >= 3
    assert "def _flush_pred() -> None:" in src


def _patch_full_market_run(monkeypatch) -> None:
    _patch_common(monkeypatch)
    monkeypatch.setattr(backfill_guard, "covered_dates_from_db", lambda **kw: {})

    class _OkRunner:
        def __init__(self, **kwargs):
            pass

        def execute(self, **kwargs):
            return _OkRunResult()

    monkeypatch.setattr(script_runner, "InferenceScriptRunner", _OkRunner)


def test_backfill_reports_merge_failure(tmp_path, monkeypatch):
    """合并抛异常 → 状态必须是 partial，并把原因写进 error/merge_error。"""
    _patch_full_market_run(monkeypatch)

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(pred_merge, "merge_signals_into_pred", _boom)

    result = asyncio.run(
        gap_backfill.backfill_model_gaps(
            tenant_id="t",
            user_id="u",
            model_id="m",
            storage_path=str(tmp_path),
            gaps=["2026-11-20"],
        )
    )
    assert result["status"] == "partial"
    assert "合并失败" in result["merge_error"]
    assert "合并失败" in result["error"]


def test_backfill_zero_row_merge_is_not_success(tmp_path, monkeypatch):
    """merge 返回 0 行同样不能算完成（旧实现会静默 completed）。"""
    _patch_full_market_run(monkeypatch)
    monkeypatch.setattr(pred_merge, "merge_signals_into_pred", lambda *a, **k: 0)

    result = asyncio.run(
        gap_backfill.backfill_model_gaps(
            tenant_id="t",
            user_id="u",
            model_id="m",
            storage_path=str(tmp_path),
            gaps=["2026-11-20"],
        )
    )
    assert result["status"] == "partial"
    assert "0 行" in result["merge_error"]


def test_merge_failure_has_its_own_dispatch_reason_code():
    """管理台要能区分「推理失败」与「落盘失败」。"""
    tasks = _backend_src("services/engine/tasks/celery_tasks.py")
    assert "PRED_MERGE_FAILED" in tasks
    src = _backend_src(GAP_BACKFILL)
    assert '"merge_error": merge_error' in src


# ---------------------------------------------------------------------------
# 3. 保留期必须真的生效（覆盖全部来源，不再绑死 feature_version）
# ---------------------------------------------------------------------------


def test_retention_default_matches_documentation():
    src = _backend_src(SCRIPT_RUNNER)
    # 曾经：注释/README 写 30 天，代码默认 730 天
    assert 'os.getenv("INFERENCE_PREDICTION_RETENTION_DAYS", "730")' not in src
    assert 'os.getenv("INFERENCE_PREDICTION_RETENTION_DAYS", "365")' in src
    # 批量回测的缺日警告必须与同一窗口同源，不能再各写一套
    batch = _backend_src("services/engine/inference/batch_orchestrator.py")
    assert (
        'PREDICTION_RETENTION_DAYS = int(os.getenv("INFERENCE_PREDICTION_RETENTION_DAYS"'
        in batch
    )


def test_retention_delete_is_not_scoped_by_feature_version():
    """Step 0.1 的 DELETE 不允许再带 model_version / feature_version 条件。"""
    src = _backend_src(SCRIPT_RUNNER)
    start = src.index("Step 0.1: 清理超出保留期的历史数据")
    end = src.index("Step 0.1b", start)
    block = src[start:end]
    assert "AND model_version = 'inference_script'" not in block
    assert "AND feature_version = :feature_version" not in block
    assert "AND source = 'inference_script'" not in block
    # 三张表都要清：明细、就绪标记、候选快照（快照此前完全没有时间清理）
    assert block.count("DELETE FROM engine_signal_scores") == 1
    assert block.count("DELETE FROM engine_feature_runs") == 1
    assert block.count("DELETE FROM qm_research_candidate_snapshot") == 1
    assert "prediction_trade_date < :retention_floor" in block
    # 批量回测本轮刚写入的历史日不能被顺手删掉
    assert block.count("created_at < NOW() - INTERVAL '1 day'") == 3


def test_candidate_snapshot_has_time_based_cleanup_outside_runner():
    """已经不再被推理的用户/模型，也要有独立 janitor 兜底清理。"""
    janitor = _backend_src(JANITOR)
    for table in (
        "engine_signal_scores",
        "qm_research_candidate_snapshot",
        "qm_model_inference_runs",
        "qm_model_inference_dispatch_logs",
    ):
        assert table in janitor, f"janitor 漏掉 {table}"
    assert "dry_run: bool = True" in janitor
    assert "engine.tasks.purge_inference_signal_storage" in _backend_src(CELERY_CONFIG)
    tasks = _backend_src("services/engine/tasks/celery_tasks.py")
    assert 'name="engine.tasks.purge_inference_signal_storage"' in tasks


# ---------------------------------------------------------------------------
# 4. 单日并存 run 数必须有上限（重推同一天不再是无界追加）
# ---------------------------------------------------------------------------


def test_persist_caps_runs_per_prediction_day():
    src = _backend_src(SCRIPT_RUNNER)
    assert "INFERENCE_MAX_RUNS_PER_PREDICTION_DAY" in src
    start = src.index("Step 0.1b: 单日并存 run 数硬上限")
    end = src.index("Step 0.2: 幂等清理", start)
    block = src[start:end]
    # 只淘汰同日「全市场量级」的旧 run，本次 run 与单股/池 run 不参与
    assert "AND run_id <> :run_id" in block
    assert "HAVING count(*) >= :min_symbols" in block
    # 被淘汰的 run 的明细/快照/就绪标记都要清掉；推理历史不删（策略/托管会以
    # run_id 回链），只标 archived，避免「点进详情是空」的幽灵批次与断链。
    assert block.count("DELETE FROM engine_signal_scores") == 1
    assert block.count("DELETE FROM qm_research_candidate_snapshot") == 1
    assert block.count("DELETE FROM engine_feature_runs") == 1
    assert "DELETE FROM qm_model_inference_runs" not in block
    assert "status = 'archived'" in block
    assert "last_run_id = ANY(:run_ids)" in block
    # 保留最新：升序列表只留末尾 cap-1 个
    assert "surplus_run_ids" in block


# ---------------------------------------------------------------------------
# 5. 存量脏数据的收敛入口 + 索引
# ---------------------------------------------------------------------------


def test_cleanup_script_exists_and_defaults_to_dry_run():
    src = _backend_src("scripts/cleanup_inference_signal_bloat.py")
    assert "孤儿 run" in src
    assert "--apply" in src and "--purge-orphan-runs" in src
    assert "dry_run=not args.apply" in src


def test_snapshot_governance_indexes_declared_idempotently():
    """守卫/清理查询都是按 (tenant,user,model,日期) 过滤，缺索引就是全表扫描。"""
    sql = (_BACKEND / "shared" / "db_init.sql").read_text(encoding="utf-8")
    assert "idx_qrcs_tenant_user_ptd" in sql
    assert "idx_qrcs_tenant_user_model_data" in sql
    assert "CREATE INDEX IF NOT EXISTS" in sql
