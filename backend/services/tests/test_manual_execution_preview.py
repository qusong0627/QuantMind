from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from backend.services.trade.services.manual_execution_service import (
    ManualExecutionService,
    PreparedManualExecution,
    _build_execution_plan_from_signals,
    _build_preview_hash,
    _manual_task_account_poll_interval_seconds,
    _manual_task_buy_cancel_timeout_seconds,
    _manual_task_wait_next_account_timeout_seconds,
    _parse_iso_datetime,
    _rebuild_buy_orders_with_budget,
    _resolve_board_lot_size,
    _should_request_cancel_for_buy_status,
)
from backend.services.trade.services.manual_execution_persistence import manual_execution_persistence


def test_build_execution_plan_from_signals_generates_sell_and_buy_orders():
    account_snapshot = {
        "available_cash": 50_000.0,
        "positions": [
            {"symbol": "600001.SH", "available_volume": 500, "volume": 500, "last_price": 10.0, "market_value": 5_000.0},
            {"symbol": "600002.SH", "available_volume": 400, "volume": 400, "last_price": 8.0, "market_value": 3_200.0},
        ],
    }
    signal_rows = [
        {"symbol": "600010.SH", "fusion_score": 0.98, "signal_side": None, "expected_price": 12.5},
        {"symbol": "600011.SH", "fusion_score": 0.95, "signal_side": None, "expected_price": 10.0},
        {"symbol": "600012.SH", "fusion_score": 0.90, "signal_side": None, "expected_price": 8.0},
    ]
    plan = _build_execution_plan_from_signals(
        signal_rows=signal_rows,
        strategy_params={"strategy_type": "TopkDropout", "topk": 2, "n_drop": 1},
        account_snapshot=account_snapshot,
    )

    assert plan["summary"]["sell_order_count"] == 1
    assert plan["summary"]["buy_order_count"] == 1
    assert plan["sell_orders"][0]["symbol"] == "600001.SH"
    assert plan["sell_orders"][0]["quantity"] == 500
    assert plan["buy_orders"][0]["symbol"] == "600010.SH"
    assert plan["buy_orders"][0]["quantity"] % 100 == 0
    assert plan["summary"]["estimated_remaining_cash"] >= 0


def test_build_execution_plan_from_signals_marks_unexecutable_items_as_skipped():
    account_snapshot = {"available_cash": 20_000.0, "positions": []}
    signal_rows = [
        {"symbol": "600100.SH", "fusion_score": 0.90, "signal_side": "sell", "expected_price": 12.0},
        {"symbol": "600101.SH", "fusion_score": 0.88, "signal_side": "buy", "expected_price": 0.0},
    ]

    plan = _build_execution_plan_from_signals(
        signal_rows=signal_rows,
        strategy_params={"strategy_type": "alpha_cross_section", "topk": 2},
        account_snapshot=account_snapshot,
    )

    assert plan["sell_orders"] == []
    assert {item["symbol"] for item in plan["skipped_items"]} >= {"600100.SH"}


def test_build_execution_plan_applies_fundamental_constraints_and_keeps_explicit_sell(monkeypatch):
    account_snapshot = {
        "available_cash": 20_000.0,
        "positions": [
            {
                "symbol": "600300.SH",
                "available_volume": 300,
                "volume": 300,
                "last_price": 10.0,
                "market_value": 3_000.0,
            }
        ],
    }
    signal_rows = [
        {"symbol": "600001.SH", "fusion_score": 0.92, "signal_side": "buy", "expected_price": 10.0},
        {"symbol": "600002.SH", "fusion_score": 0.90, "signal_side": "buy", "expected_price": 10.0},
        {"symbol": "600300.SH", "fusion_score": 0.20, "signal_side": "sell"},
    ]

    monkeypatch.setattr(
        "backend.services.trade.services.manual_execution_service.fundamental_aligner.filter_instruments",
        lambda _dt, symbols, constraints=None: [s for s in symbols if s == "600001.SH"],
    )

    plan = _build_execution_plan_from_signals(
        signal_rows=signal_rows,
        strategy_params={"strategy_type": "alpha_cross_section", "topk": 2, "f_pe_ttm_max": 25},
        account_snapshot=account_snapshot,
        trade_date=date(2026, 4, 1),
    )

    assert plan["summary"]["raw_signal_count"] == 3
    assert plan["summary"]["fundamental_filtered_count"] == 1
    assert plan["summary"]["signal_count"] == 2
    assert plan["summary"]["sell_order_count"] == 1
    assert plan["sell_orders"][0]["symbol"] == "600300.SH"
    assert plan["summary"]["buy_order_count"] == 1
    assert plan["buy_orders"][0]["symbol"] == "600001.SH"


@pytest.mark.asyncio
async def test_submit_execution_plan_rejects_mismatched_preview_hash():
    service = ManualExecutionService()
    service.build_execution_preview = AsyncMock(return_value={  # type: ignore[method-assign]
        "preview_hash": "expected-hash",
        "summary": {},
        "sell_orders": [],
        "buy_orders": [],
        "skipped_items": [],
    })

    with pytest.raises(HTTPException) as exc_info:
        await service.submit_execution_plan(
            tenant_id="default",
            user_id="79311845",
            model_id="model_qlib",
            run_id="run-1",
            strategy_id="101",
            trading_mode="REAL",
            preview_hash="wrong-hash",
            note=None,
        )

    assert exc_info.value.status_code == 409
    assert "预览结果已失效" in str(exc_info.value.detail)


def test_preview_hash_is_stable_for_same_payload():
    payload = {
        "account_snapshot": {"total_asset": 1},
        "strategy_context": {"run_id": "run-1"},
        "sell_orders": [],
        "buy_orders": [{"symbol": "600000.SH", "quantity": 100}],
        "skipped_items": [],
        "summary": {"buy_order_count": 1},
    }

    assert _build_preview_hash(payload) == _build_preview_hash(dict(payload))


@pytest.mark.asyncio
async def test_create_hosted_task_uses_latest_default_model_run_and_db_signals(monkeypatch):
    service = ManualExecutionService()
    latest_run = {
        "run_id": "run_latest_default",
        "data_trade_date": date(2026, 4, 13),
        "prediction_trade_date": date(2026, 4, 14),
        "fallback_used": False,
        "model_source": "user_default",
    }

    monkeypatch.setattr(
        service,
        "get_default_model_hosted_status",
        AsyncMock(
            return_value={
                "model_id": "mdl_user_default",
                "available": True,
                "latest_default_model_id": "mdl_user_default",
                "latest_run_id": "run_latest_default",
                "prediction_trade_date": "2026-04-14",
                "execution_window_start": "2026-04-14",
                "execution_window_end": "2026-04-19",
                "target_horizon_days": 5,
            }
        ),
    )
    monkeypatch.setattr(
        service,
        "prepare_manual_execution",
        AsyncMock(
            return_value=PreparedManualExecution(
                task_id="",
                tenant_id="default",
                user_id="79311845",
                strategy_id="48",
                strategy_name="测试策略",
                run_id="run_latest_default",
                model_id="mdl_user_default",
                prediction_trade_date=date(2026, 4, 14),
                trading_mode="REAL",
                request_payload={"strategy_id": "48", "run_id": "run_latest_default"},
                run=latest_run,
                strategy={"id": "48", "name": "测试策略", "is_verified": True, "parameters": {"strategy_type": "TopkDropout"}},
            )
        ),
    )
    monkeypatch.setattr(
        service,
        "_load_latest_account_snapshot",
        AsyncMock(return_value={"available_cash": 100000, "positions": []}),
    )
    monkeypatch.setattr(
        service,
        "_load_signal_rows",
        AsyncMock(
            return_value=[
                {
                    "symbol": "600000.SH",
                    "fusion_score": 0.95,
                    "signal_side": "BUY",
                    "expected_price": 10.0,
                }
            ]
        ),
    )
    captured: dict[str, object] = {}

    def _fake_plan(*, signal_rows, strategy_params, account_snapshot, trade_date=None):
        captured["signal_rows"] = signal_rows
        captured["strategy_params"] = strategy_params
        captured["trade_date"] = trade_date
        return {
            "sell_orders": [],
            "buy_orders": [
                {
                    "symbol": "600000.SH",
                    "side": "BUY",
                    "quantity": 100,
                    "price": 10.0,
                    "fusion_score": 0.95,
                }
            ],
            "skipped_items": [],
            "summary": {
                "signal_count": 1,
                "buy_order_count": 1,
                "sell_order_count": 0,
                "skipped_count": 0,
            },
        }

    monkeypatch.setattr(
        "backend.services.trade.services.manual_execution_service._build_execution_plan_from_signals",
        _fake_plan,
    )
    monkeypatch.setattr(service, "_persist_task", AsyncMock(return_value={"task_id": "hosted_1", "status": "queued"}))

    result = await service.create_hosted_task(
        tenant_id="default",
        user_id="79311845",
        run_id="ignored-run",
        strategy_id="48",
        trading_mode="REAL",
        execution_config={"trading_mode": "REAL"},
        live_trade_config={"schedule_type": "interval", "rebalance_days": 5},
        trigger_context={"source": "runner"},
        parent_runtime_id="runtime-1",
        note=None,
    )

    assert result["status"] == "queued"
    assert captured["signal_rows"][0]["symbol"] == "600000.SH"
    persist_call = service._persist_task.call_args.kwargs
    assert persist_call["prepared"].run_id == "run_latest_default"
    assert persist_call["prepared"].model_id == "mdl_user_default"
    assert persist_call["request_payload"]["source_run_id"] == "run_latest_default"
    assert persist_call["request_payload"]["target_horizon_days"] == 5


@pytest.mark.asyncio
async def test_create_hosted_task_rejects_expired_default_model_run(monkeypatch):
    service = ManualExecutionService()

    monkeypatch.setattr(
        service,
        "_load_user_default_model_record",
        AsyncMock(
            return_value={
                "model_id": "mdl_user_default",
                "metadata_json": {"target_horizon_days": 5},
                "status": "ready",
            }
        ),
    )
    monkeypatch.setattr(
        service,
        "_load_latest_default_model_inference_run",
        AsyncMock(
            return_value={
                "run_id": "run_expired",
                "data_trade_date": date(2026, 4, 1),
                "prediction_trade_date": date(2026, 4, 2),
                "fallback_used": False,
                "model_source": "user_default",
            }
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        await service.create_hosted_task(
            tenant_id="default",
            user_id="79311845",
            run_id="ignored-run",
            strategy_id="48",
            trading_mode="REAL",
            execution_config={"trading_mode": "REAL"},
            live_trade_config={"schedule_type": "interval", "rebalance_days": 5},
            trigger_context={"source": "runner"},
            parent_runtime_id="runtime-1",
            note=None,
        )

    assert exc_info.value.status_code == 409
    assert "已超过可执行窗口" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_create_hosted_task_returns_existing_task_when_duplicate_task_id(monkeypatch):
    service = ManualExecutionService()

    monkeypatch.setattr(
        manual_execution_persistence,
        "get_task_any",
        AsyncMock(return_value={"task_id": "hosted_dup", "status": "completed", "result_json": {}}),
    )
    monkeypatch.setattr(
        service,
        "_load_user_default_model_record",
        AsyncMock(side_effect=AssertionError("duplicate task should short-circuit before model lookup")),
    )

    result = await service.create_hosted_task(
        tenant_id="default",
        user_id="79311845",
        task_id="hosted_dup",
        strategy_id="48",
        trading_mode="REAL",
        execution_config={"trading_mode": "REAL"},
        live_trade_config={"schedule_type": "interval", "rebalance_days": 5},
        trigger_context={"source": "runner"},
        parent_runtime_id="runtime-1",
        note=None,
    )

    assert result["task_id"] == "hosted_dup"
    assert result["duplicate"] is True
    assert result["noop"] is True


@pytest.mark.asyncio
async def test_get_default_model_hosted_status_distinguishes_latest_run_reasons(monkeypatch):
    service = ManualExecutionService()

    monkeypatch.setattr(
        service,
        "_load_user_default_model_record",
        AsyncMock(
            return_value={
                "model_id": "mdl_user_default",
                "metadata_json": {"target_horizon_days": 5},
                "status": "ready",
            }
        ),
    )
    monkeypatch.setattr(
        service,
        "_load_latest_default_model_inference_run",
        AsyncMock(
            return_value={
                "run_id": "run_latest_default",
                "data_trade_date": date(2026, 4, 10),
                "prediction_trade_date": date(2026, 4, 12),
                "fallback_used": True,
                "model_source": "vectorized_matcher_fallback",
            }
        ),
    )
    monkeypatch.setattr(
        service,
        "_resolve_hosted_execution_window",
        lambda **kwargs: (date(2026, 4, 11), date(2026, 4, 17)),
    )
    monkeypatch.setattr(
        "backend.services.trade.services.manual_execution_service.datetime",
        SimpleNamespace(
            now=lambda _tz=None: SimpleNamespace(date=lambda: date(2026, 4, 14))
        ),
    )

    status = await service.get_default_model_hosted_status(
        tenant_id="default",
        user_id="79311845",
    )

    assert status["available"] is False
    assert status["source"] == "fallback"
    assert status["reason_code"] == "fallback_used"
    assert status["latest_run_id"] == "run_latest_default"
    assert "兜底结果" in status["message"]


@pytest.mark.asyncio
async def test_get_default_model_hosted_status_accepts_explicit_system_model(monkeypatch):
    service = ManualExecutionService()

    monkeypatch.setattr(
        service,
        "_load_user_default_model_record",
        AsyncMock(
            return_value={
                "model_id": "mdl_user_default",
                "metadata_json": {"target_horizon_days": 5},
                "status": "ready",
            }
        ),
    )
    monkeypatch.setattr(
        service,
        "_load_latest_default_model_inference_run",
        AsyncMock(
            return_value={
                "run_id": "run_explicit_system",
                "data_trade_date": date(2026, 4, 10),
                "prediction_trade_date": date(2026, 4, 13),
                "fallback_used": False,
                "model_source": "explicit_system_model",
            }
        ),
    )
    monkeypatch.setattr(
        service,
        "_resolve_hosted_execution_window",
        lambda **kwargs: (date(2026, 4, 11), date(2026, 4, 17)),
    )
    monkeypatch.setattr(
        "backend.services.trade.services.manual_execution_service.datetime",
        SimpleNamespace(
            now=lambda _tz=None: SimpleNamespace(date=lambda: date(2026, 4, 14))
        ),
    )

    status = await service.get_default_model_hosted_status(
        tenant_id="default",
        user_id="79311845",
    )

    assert status["available"] is True
    assert status["source"] == "explicit_system_model"
    assert status["reason_code"] == "ready"
    assert status["latest_run_id"] == "run_explicit_system"


def test_parse_iso_datetime_supports_z_suffix_and_naive_value():
    parsed_z = _parse_iso_datetime("2026-05-13T10:00:00Z")
    parsed_naive = _parse_iso_datetime("2026-05-13T10:00:00")

    assert parsed_z == datetime(2026, 5, 13, 10, 0, 0, tzinfo=timezone.utc)
    assert parsed_naive == datetime(2026, 5, 13, 10, 0, 0, tzinfo=timezone.utc)
    assert _parse_iso_datetime("") is None


def test_manual_task_account_poll_config_bounds(monkeypatch):
    monkeypatch.setenv("MANUAL_TASK_WAIT_NEXT_ACCOUNT_TIMEOUT_SECONDS", "0")
    monkeypatch.setenv("MANUAL_TASK_ACCOUNT_POLL_INTERVAL_SECONDS", "999")
    monkeypatch.setenv("MANUAL_TASK_BUY_CANCEL_TIMEOUT_SECONDS", "-1")

    assert _manual_task_wait_next_account_timeout_seconds() == 1
    assert _manual_task_account_poll_interval_seconds() == 10.0
    assert _manual_task_buy_cancel_timeout_seconds() == 1


def test_rebuild_buy_orders_with_budget_uses_snapshot_cash_budget():
    recalculated, skipped, remaining_cash = _rebuild_buy_orders_with_budget(
        buy_orders=[
            {
                "symbol": "600000.SH",
                "quantity": 100,
                "price": 10.0,
                "reference_price": 10.0,
                "reason": "原始买单A",
            },
            {
                "symbol": "600001.SH",
                "quantity": 100,
                "price": 20.0,
                "reference_price": 20.0,
                "reason": "原始买单B",
            },
        ],
        buy_budget=2500.0,
    )

    assert len(recalculated) == 1
    assert recalculated[0]["symbol"] == "600000.SH"
    assert recalculated[0]["quantity"] == 100
    assert len(skipped) == 1
    assert skipped[0]["symbol"] == "600001.SH"
    assert remaining_cash == pytest.approx(1500.0)
    assert "账户快照资金重算" in str(recalculated[0].get("reason") or "")


def test_should_request_cancel_for_buy_status():
    assert _should_request_cancel_for_buy_status("submitted") is True
    assert _should_request_cancel_for_buy_status("partially_filled") is True
    assert _should_request_cancel_for_buy_status("filled") is False
    assert _should_request_cancel_for_buy_status("cancelled") is False


def test_resolve_board_lot_size_matches_risk_rule():
    assert _resolve_board_lot_size("688217.SH") == 200
    assert _resolve_board_lot_size("300001.SZ") == 100
    assert _resolve_board_lot_size("000001.SZ") == 100


def test_build_execution_plan_uses_star_board_lot_size_for_buy():
    account_snapshot = {"available_cash": 1_000_000.0, "positions": []}
    signal_rows = [
        {
            "symbol": "688217.SH",
            "fusion_score": 0.95,
            "signal_side": "buy",
            "expected_price": 25.0,
        }
    ]
    plan = _build_execution_plan_from_signals(
        signal_rows=signal_rows,
        strategy_params={"strategy_type": "alpha_cross_section", "topk": 1},
        account_snapshot=account_snapshot,
    )

    assert plan["buy_orders"]
    assert plan["buy_orders"][0]["quantity"] >= 200
    assert plan["buy_orders"][0]["quantity"] % 200 == 0
