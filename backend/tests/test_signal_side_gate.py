"""signal_side 闸门单测：量纲归一化 / 共识可达性 / 置信度缺省。

回归 2026-08-11 起的「全 HOLD」事故：confidence 被解析层丢弃恒为 None、
共识阈值 4 对小融合永远不可达、绝对阈值 ±0.2 与单模型分数量纲不符。
"""

from __future__ import annotations

import json
import logging

import numpy as np
import pytest

from backend.services.engine.inference.script_runner import InferenceScriptRunner

pytestmark = pytest.mark.unit

# 单模型量纲：T+1 收益预测，量级 ±0.05（绝对阈值 0.2 永远够不到）
SINGLE_MODEL_SCORES = [
    0.05,
    0.04,
    0.03,
    0.02,
    0.01,
    0.0,
    -0.01,
    -0.02,
    -0.03,
    -0.04,
]
# 融合模板量纲：rank 百分位派生，[-1, 1]
FUSION_SCORES = [0.5, 0.4, 0.3, 0.2, 0.1, 0.0, -0.1, -0.2, -0.3, -0.4]


def _sides(scores, consensus=None, confidence=None):
    return InferenceScriptRunner._resolve_signal_sides(scores, consensus, confidence)


class TestRobustNormalize:
    def test_maps_quantiles_to_pm_one(self):
        arr = np.arange(101, dtype=float)
        out = InferenceScriptRunner._robust_normalize(arr)
        assert out[5] == pytest.approx(-1.0)
        assert out[95] == pytest.approx(1.0)
        assert out.min() >= -1.0
        assert out.max() <= 1.0
        assert np.all(np.diff(out) >= -1e-12)  # 单调不减

    def test_degenerate_returns_input_unchanged(self):
        for arr in ([0.5], [0.3, 0.3, 0.3], [0.1, float("nan"), 0.2]):
            src = np.asarray(arr, dtype=float)
            out = InferenceScriptRunner._robust_normalize(src)
            np.testing.assert_allclose(out, src, equal_nan=True)


class TestResolveSignalSides:
    def test_single_model_scale_still_yields_buy_and_sell(self):
        """修复前：单模型量纲够不到 ±0.2 → 全 HOLD。"""
        sides = _sides(SINGLE_MODEL_SCORES)
        assert sides.count("BUY") == 2
        assert sides.count("SELL") == 2
        assert sides[0] == "BUY"
        assert sides[-1] == "SELL"

    def test_confidence_all_none_skips_gate(self):
        """修复前：`[None]*n` 被判为低置信 → 全 HOLD。"""
        sides = _sides(FUSION_SCORES, None, [None] * len(FUSION_SCORES))
        assert sides.count("BUY") == 2
        assert sides.count("SELL") == 2

    def test_low_confidence_forces_hold_only_for_that_symbol(self):
        confidence = [0.9, 0.1, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9]
        sides = _sides(FUSION_SCORES, None, confidence)
        assert sides[0] == "BUY"  # 0.5，置信 0.9
        assert sides[1] == "HOLD"  # 0.4，置信 0.1 < 0.3
        assert sides.count("SELL") == 2

    def test_consensus_unreachable_skips_gate_with_warning(self, caplog):
        """修复前：min_consensus=4 而当日最大共识 2 → 全 HOLD。"""
        consensus = [2, 1, 0, 2, 1, 0, 2, 1, 0, 2]
        with caplog.at_level(logging.WARNING):
            sides = _sides(FUSION_SCORES, consensus)
        assert sides.count("BUY") == 2
        assert sides.count("SELL") == 2
        assert any("共识" in r.getMessage() for r in caplog.records)

    def test_consensus_reachable_still_filters(self):
        consensus = [3, 5, 5, 4, 5, 4, 5, 4, 5, 4]  # max=5 ≥ 4 → 闸门生效
        sides = _sides(FUSION_SCORES, consensus)
        assert sides[0] == "HOLD"  # 共识 3 < 4
        assert sides[1] == "BUY"  # 共识 5

    def test_all_gates_blocked_logs_warning(self, caplog):
        with caplog.at_level(logging.WARNING):
            sides = _sides(FUSION_SCORES, None, [0.1] * len(FUSION_SCORES))
        assert sides.count("BUY") == 0
        assert sides.count("SELL") == 0
        assert any("无 BUY/SELL" in r.getMessage() for r in caplog.records)

    def test_empty_scores(self):
        assert _sides([]) == []


class TestParseSignals:
    @staticmethod
    def _write(tmp_path, payload):
        path = tmp_path / "signals.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    def test_carries_confidence(self, tmp_path, monkeypatch):
        """修复前：confidence 在解析层被丢弃 → 下游恒为 None。"""
        monkeypatch.setattr(
            InferenceScriptRunner, "_get_st_symbols", staticmethod(lambda: [])
        )
        path = self._write(
            tmp_path,
            [
                {"symbol": "600036", "score": 0.5, "consensus": 2, "confidence": 0.42},
                {"symbol": "000001", "score": 0.1, "consensus": 2, "confidence": 0.11},
            ],
        )
        parsed = InferenceScriptRunner._parse_signals(path)
        assert parsed is not None
        assert parsed[0]["confidence"] == pytest.approx(0.42)
        assert parsed[1]["confidence"] == pytest.approx(0.11)

    def test_missing_confidence_is_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            InferenceScriptRunner, "_get_st_symbols", staticmethod(lambda: [])
        )
        path = self._write(
            tmp_path, [{"symbol": "600036", "score": 0.5, "consensus": 2}]
        )
        parsed = InferenceScriptRunner._parse_signals(path)
        assert parsed is not None
        assert parsed[0]["confidence"] is None
