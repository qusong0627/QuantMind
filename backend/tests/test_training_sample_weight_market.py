"""样本权重市场闸：tradeable_mask/st 仅 CN，非 CN 自动关闭（2026-11-22）。"""

from __future__ import annotations

import pytest

from backend.services.api.routers.admin.admin_training_utils import (
    _coerce_sample_weight_for_market,
    _normalize_payload,
)


def test_cn_keeps_enabled_weights():
    weight = {"tradeable_mask": True, "st": True}
    assert _coerce_sample_weight_for_market("CN", weight) == []
    assert weight == {"tradeable_mask": True, "st": True}


@pytest.mark.parametrize("market", ["CUSTOM", "HK", "US", "FUTURES", "CRYPTO"])
def test_non_cn_disables_weights_and_returns_notice(market):
    weight = {"tradeable_mask": True, "st": True}
    notices = _coerce_sample_weight_for_market(market, weight)
    assert weight == {"tradeable_mask": False, "st": False}
    assert len(notices) == 1
    assert "已自动关闭" in notices[0]
    assert market in notices[0]


def test_non_cn_keeps_disabled_weights_and_time_decay():
    weight = {
        "tradeable_mask": False,
        "st": False,
        "time_decay_half_life_days": 30,
    }
    assert _coerce_sample_weight_for_market("CUSTOM", weight) == []
    assert weight["time_decay_half_life_days"] == 30


def test_normalize_payload_reads_market_from_dict_context_and_disables_weights():
    """回归：req.context 是 dict，曾用 getattr 取 market 导致闸门永远不触发。"""
    normalized = _normalize_payload(
        {
            "context": {"market": "CUSTOM"},
            "sample_weight": {"tradeable_mask": True},
        },
        [],
    )
    assert normalized["sample_weight"]["tradeable_mask"] is False
    assert normalized["sample_weight"]["st"] is False
    assert any("已自动关闭" in notice for notice in normalized["system_notices"])
