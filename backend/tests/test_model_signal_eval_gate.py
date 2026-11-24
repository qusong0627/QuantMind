"""标准化评估闸门的接线测试（模型激活/默认/策略绑定前）。"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from fastapi import HTTPException

from backend.services.api.routers import model_training as router

RED = {
    "grade": "red",
    "checks": {"ranking": False},
    "reasons": ["截面排序"],
    "window_end": "2026-11-13",
}
YELLOW = {
    "grade": "yellow",
    "checks": {"ranking": True, "neutral": True, "long": False},
    "reasons": ["TopN 长多超额"],
    "window_end": "2026-11-13",
}


def _model(market: str = "CN") -> dict:
    return {
        "metadata_json": {"market": market, "target_horizon_days": 5},
        "storage_path": "/tmp/model",
    }


def _call(admission: dict, *, force: bool, market: str = "CN"):
    with patch(
        "backend.services.api.model_signal_eval_service.admission_status",
        return_value=admission,
    ):
        return asyncio.run(
            router._require_signal_admission(
                "m1", _model(market), force=force, action="activate"
            )
        )


def test_gate_blocks_red_without_force():
    try:
        _call(RED, force=False)
    except HTTPException as exc:
        assert exc.status_code == 409
        detail = exc.detail
        assert detail["code"] == "model_signal_eval_gate"
        assert detail["grade"] == "red"
        assert "截面排序" in detail["reasons"]
    else:
        raise AssertionError("red 且未 force 应被 409 拦截")


def test_gate_allows_force_and_yellow():
    assert _call(RED, force=True)["grade"] == "red"
    assert _call(YELLOW, force=False)["grade"] == "yellow"


def test_gate_skips_non_cn_models():
    assert _call(RED, force=False, market="HK") is None


def test_set_default_endpoint_blocks_red_before_registry_write():
    from unittest.mock import AsyncMock

    with (
        patch.object(
            router.model_registry_service,
            "get_model",
            AsyncMock(return_value=_model()),
        ),
        patch.object(
            router.model_registry_service,
            "set_default_model",
            AsyncMock(return_value={"model_id": "m1"}),
        ) as set_default,
        patch(
            "backend.services.api.model_signal_eval_service.admission_status",
            return_value=RED,
        ),
    ):
        try:
            asyncio.run(
                router.set_default_model(
                    router.SetDefaultModelRequest(model_id="m1"),
                    {"tenant_id": "default", "user_id": "1"},
                )
            )
        except HTTPException as exc:
            assert exc.status_code == 409
        else:
            raise AssertionError("red 模型不应完成 set_default")
    set_default.assert_not_awaited()
