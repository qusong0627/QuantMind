"""账户/持仓分组件新鲜度（纯函数）单测。

核心不变量：
* 资产取数失败不得把自己的观测时间刷新成"现在"；
* 资产与持仓各自有 stale 阈值，任一超限整份快照 stale；
* 从未观测 = unavailable，不是 ok。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[1] / "bridge_account_state.py"
_spec = importlib.util.spec_from_file_location("bridge_account_state", _MODULE_PATH)
_state = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _state
_spec.loader.exec_module(_state)


def test_never_observed_is_unavailable_not_ok():
    meta = _state.merge_observation(
        asset_observed_at=None,
        positions_observed_at=1000.0,
        now=1000.0,
        asset_stale_after_s=120,
        positions_stale_after_s=30,
    )
    assert meta["asset_status"] == _state.STATUS_UNAVAILABLE
    assert meta["positions_status"] == _state.STATUS_OK
    assert meta["stale"] is True
    assert "asset:unavailable" in meta["stale_reasons"]


def test_fresh_components_are_ok():
    meta = _state.merge_observation(
        asset_observed_at=990.0,
        positions_observed_at=1000.0,
        now=1000.0,
        asset_stale_after_s=120,
        positions_stale_after_s=30,
    )
    assert meta["asset_status"] == _state.STATUS_OK
    assert meta["positions_status"] == _state.STATUS_OK
    assert meta["stale"] is False
    assert meta["stale_reasons"] == []
    assert meta["asset_age_s"] == 10.0
    assert meta["positions_age_s"] == 0.0


def test_each_component_has_its_own_threshold():
    meta = _state.merge_observation(
        asset_observed_at=880.0,      # 120s old: at threshold (not >) -> ok
        positions_observed_at=970.0,  # 30s old: at threshold -> ok
        now=1000.0,
        asset_stale_after_s=120,
        positions_stale_after_s=30,
    )
    assert meta["asset_status"] == _state.STATUS_OK
    assert meta["positions_status"] == _state.STATUS_OK

    stale = _state.merge_observation(
        asset_observed_at=879.0,
        positions_observed_at=969.0,
        now=1000.0,
        asset_stale_after_s=120,
        positions_stale_after_s=30,
    )
    assert stale["asset_status"] == _state.STATUS_STALE
    assert stale["positions_status"] == _state.STATUS_STALE
    assert stale["stale"] is True
    assert "asset:stale" in stale["stale_reasons"]
    assert "positions:stale" in stale["stale_reasons"]


def test_last_refresh_error_is_reported_without_false_stale():
    """刷新失败但缓存仍在阈值内：状态 ok，degraded 里如实记录失败。"""
    meta = _state.merge_observation(
        asset_observed_at=990.0,
        positions_observed_at=1000.0,
        now=1000.0,
        asset_stale_after_s=120,
        positions_stale_after_s=30,
        asset_error="get_asset timeout",
    )
    assert meta["asset_status"] == _state.STATUS_OK
    assert meta["stale"] is False
    assert meta["asset_error"] == "get_asset timeout"
    assert "asset:last_refresh_failed" in meta["degraded_reasons"]


def test_negative_observation_is_unavailable():
    meta = _state.merge_observation(
        asset_observed_at=-1.0,
        positions_observed_at=0.0,
        now=1000.0,
        asset_stale_after_s=120,
        positions_stale_after_s=30,
    )
    assert meta["asset_status"] == _state.STATUS_UNAVAILABLE
    assert meta["positions_status"] == _state.STATUS_UNAVAILABLE
    assert meta["asset_age_s"] is None
    assert meta["positions_age_s"] is None
