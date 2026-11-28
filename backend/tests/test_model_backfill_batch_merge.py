"""批量补推：同一模型的多日只合并一次 pred.parquet。

回归背景（2026-11-28）：逐日补推每天把 5.8M 行的 pred.parquet 读进来再整份
写回；13 模型 × 21 日 = 273 次全量读写，是补推慢的主因。``run_one_model``
把同一模型的全部缺口日信号收集后只调一次 ``merge_signals_into_pred``。
"""

from __future__ import annotations

import asyncio
import types
from pathlib import Path

from backend.services.engine.inference import model_backfill as mb


def _model() -> mb.BackfillModel:
    return mb.BackfillModel(
        model_id="mdl_test",
        display_name="test",
        model_type="lightgbm",
        market="CN",
        target_horizon_days=5,
        status="ready",
        storage_path="/tmp/qm_test_model",
        metadata={},
    )


def test_run_one_model_merges_all_successful_days_once(monkeypatch) -> None:
    merge_calls: list[tuple[str, int, list[str]]] = []

    def fake_infer(model, day, tenant_id, user_id, redis_client):
        if day.endswith("26"):
            return (
                types.SimpleNamespace(
                    success=False,
                    error="boom",
                    signals=[],
                    run_id="run",
                    signals_count=0,
                    fallback_reason="",
                ),
                [],
            )
        signal = {"symbol": "SH600000", "score": 1.0}
        return (
            types.SimpleNamespace(
                success=True,
                error="",
                signals=[signal],
                run_id="run",
                signals_count=1,
                fallback_reason="",
            ),
            [signal],
        )

    def fake_merge(path, pairs, create_if_missing=True):
        merge_calls.append((str(path), len(pairs), [day for day, _ in pairs]))
        return len(pairs)

    async def fake_record(*args, **kwargs):
        return None

    monkeypatch.setattr(mb, "_infer_sync", fake_infer)
    monkeypatch.setattr(mb, "merge_signals_into_pred", fake_merge)
    monkeypatch.setattr(
        mb, "resolve_pred_parquet", lambda storage_path: Path(storage_path) / "pred.parquet"
    )
    monkeypatch.setattr(mb, "_record_failed_run", fake_record)

    summary = asyncio.run(
        mb.run_one_model(
            _model(),
            ["2026-11-25", "2026-11-26", "2026-11-27"],
            "default",
            "10000001",
        )
    )

    assert summary.days_completed == 2
    assert summary.days_failed == 1
    assert summary.days_merged == 2
    assert summary.status == "partial"
    assert merge_calls == [
        (
            "/tmp/qm_test_model/pred.parquet",
            2,
            ["2026-11-25", "2026-11-27"],
        )
    ]


def test_run_one_model_skips_merge_when_all_days_fail(monkeypatch) -> None:
    merge_calls: list[object] = []

    def fake_infer(model, day, tenant_id, user_id, redis_client):
        return (
            types.SimpleNamespace(
                success=False,
                error="down",
                signals=[],
                run_id="run",
                signals_count=0,
                fallback_reason="",
            ),
            [],
        )

    def fake_merge(*args, **kwargs):
        merge_calls.append(args)
        return 0

    async def fake_record(*args, **kwargs):
        return None

    monkeypatch.setattr(mb, "_infer_sync", fake_infer)
    monkeypatch.setattr(mb, "merge_signals_into_pred", fake_merge)
    monkeypatch.setattr(mb, "_record_failed_run", fake_record)

    summary = asyncio.run(
        mb.run_one_model(
            _model(),
            ["2026-11-26", "2026-11-27"],
            "default",
            "10000001",
            failure_limit=1,
        )
    )

    assert summary.days_completed == 0
    assert summary.days_failed == 1
    assert summary.days_merged == 0
    assert merge_calls == []
