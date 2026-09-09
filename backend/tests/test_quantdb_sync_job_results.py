"""quantdb_console 同步任务结果映射 / 失败可见性单元测试。

客户反馈「点更新没反应」的三条静默路径：
1. 请求的数据集不在云端增量清单里（min1/min5/tick/etf_pcf/convertible_bond）时，
   旧实现一律记成 up_to_date，前端渲染成绿色「最新」，用户以为同步过了；
2. `_run_sync_job` 的模块级 import 在 try 之外，导入失败时线程直接退出，任务
   永远停在 running，前端据此禁用同步按钮（点击无反应、无报错）；
3. 结果标签把 skipped（用户取消 / 云端不支持）也渲染成红色失败色，误导用户。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import backend.scripts.quantdb_daily_sync as qds
from backend.services.api.routers.admin import quantdb_console as qc


# ---------------------------------------------------------------------------
# 结果映射
# ---------------------------------------------------------------------------


def test_uncovered_dataset_marked_skipped_not_up_to_date():
    sync_result = {
        "parquet": {"synced": 0},
        "sources": {},
        "uncovered_datasets": ["min5_kline"],
    }

    results = qc._map_dataset_results(["min5_kline"], sync_result, cancelled=False)

    assert results == [
        {
            "dataset": "min5_kline",
            "status": "skipped",
            "downloaded": 0,
            "reason": qc.UNSUPPORTED_SYNC_REASON,
        }
    ]


def test_covered_dataset_without_download_is_up_to_date():
    sync_result = {"parquet": {"synced": 0}, "sources": {}, "uncovered_datasets": []}

    results = qc._map_dataset_results(["daily_forward"], sync_result, cancelled=False)

    assert results == [
        {"dataset": "daily_forward", "status": "up_to_date", "downloaded": 0}
    ]


def test_source_error_maps_to_failed():
    sync_result = {
        "parquet": {},
        "sources": {"hsgt_north": {"status": "error", "error": "boom"}},
    }

    results = qc._map_dataset_results(["hsgt_north"], sync_result, cancelled=False)

    assert results == [
        {
            "dataset": "hsgt_north",
            "status": "failed",
            "downloaded": 0,
            "error": "boom",
        }
    ]


def test_cancel_reason_wins_over_uncovered():
    sync_result = {"parquet": {}, "sources": {}, "uncovered_datasets": ["min5_kline"]}

    results = qc._map_dataset_results(["min5_kline"], sync_result, cancelled=True)

    assert results[0]["status"] == "skipped"
    assert results[0]["reason"] == "用户取消"


# ---------------------------------------------------------------------------
# run_daily_sync 上报未覆盖数据集
# ---------------------------------------------------------------------------


def test_run_daily_sync_reports_uncovered_datasets(monkeypatch):
    seen: dict = {}

    def _fake_sync_parquet(ds_list, **kwargs):
        seen["datasets"] = [d["sub_category"] for d in ds_list]
        return {"synced": 0}

    monkeypatch.setattr(qds, "sync_parquet", _fake_sync_parquet)
    monkeypatch.setattr(qds, "_sync_extra_sources", lambda **kwargs: {})

    result = qds.run_daily_sync(
        datasets=["daily_forward", "min5_kline"],
        skip_pg=True,
        skip_qlib=True,
        skip_snapshot=True,
    )

    assert result["uncovered_datasets"] == ["min5_kline"]
    assert seen["datasets"] == ["daily_forward"]


def test_run_daily_sync_skips_cloud_call_when_no_covered_dataset(monkeypatch):
    """请求的数据集云端清单全未收录时，不得调用 sync_parquet。

    缺 QUANTDB_API_KEY 时 sync_parquet 抛「QUANTDB_API_KEY 未配置」，会把本应
    「跳过」的结果变成整单失败（便携包实测：无 Key 点更新 → 任务 failed）。
    """

    def _boom(*args, **kwargs):
        raise RuntimeError("QUANTDB_API_KEY 未配置")

    monkeypatch.setattr(qds, "sync_parquet", _boom)
    monkeypatch.setattr(qds, "_sync_extra_sources", lambda **kwargs: {})

    result = qds.run_daily_sync(
        datasets=["min5_kline"],
        skip_pg=True,
        skip_qlib=True,
        skip_snapshot=True,
    )

    assert result["uncovered_datasets"] == ["min5_kline"]
    assert result["parquet"]["synced"] == 0
    assert result["parquet"]["errors"] == []


def test_run_daily_sync_fully_covered_reports_empty_uncovered(monkeypatch):
    monkeypatch.setattr(qds, "sync_parquet", lambda ds_list, **kwargs: {"synced": 0})
    monkeypatch.setattr(qds, "_sync_extra_sources", lambda **kwargs: {})

    result = qds.run_daily_sync(
        datasets=["daily_forward"],
        skip_pg=True,
        skip_qlib=True,
        skip_snapshot=True,
    )

    assert result["uncovered_datasets"] == []


# ---------------------------------------------------------------------------
# 导入失败不得留下僵尸 running 任务
# ---------------------------------------------------------------------------


def test_run_sync_job_marks_failed_when_sync_import_breaks(monkeypatch):
    monkeypatch.setitem(sys.modules, "backend.scripts.quantdb_daily_sync", None)
    job_id = "qdb-test-import"
    with qc._jobs_lock:
        qc._jobs[job_id] = {
            "job_id": job_id,
            "status": "running",
            "stage": "sync_parquet",
            "datasets": ["daily_forward"],
            "total": 1,
            "done": 0,
            "results": [],
            "cancel_requested": False,
        }
    try:
        qc._run_sync_job(job_id, qc.SyncDatasetsRequest(datasets=["daily_forward"]))
        with qc._jobs_lock:
            job = dict(qc._jobs[job_id])
    finally:
        with qc._jobs_lock:
            qc._jobs.pop(job_id, None)

    assert job["status"] == "failed"
    assert job["error"]
