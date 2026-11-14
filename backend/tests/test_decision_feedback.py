"""Agent 近期战绩块：事实渲染 + 小样本不写结论。"""

from __future__ import annotations

from backend.shared.decision_feedback import build_agent_feedback_block


class TestAgentFeedback:
    def test_losing_streak_gets_caution(self):
        fb = {
            "n": 4,
            "rows": [
                {"code": "600050.SH", "pnl": -176, "pct": -2.48, "days": 1},
                {"code": "600050.SH", "pnl": -240, "pct": -3.38, "days": 2.2},
                {"code": "001388.SZ", "pnl": -88, "pct": -1.81, "days": 1.9},
                {"code": "001388.SZ", "pnl": -186, "pct": -2.55, "days": 2.0},
            ],
        }
        text = build_agent_feedback_block(fb)
        assert "最近 4 笔平仓" in text
        assert "胜率 0/4" in text
        assert "减少低质量交易" in text
        assert "600050.SH -2.48%" in text

    def test_small_sample_has_no_conclusion(self):
        fb = {
            "n": 2,
            "rows": [
                {"code": "A", "pnl": -1, "pct": -1.0, "days": 1},
                {"code": "B", "pnl": 2, "pct": 2.0, "days": 1},
            ],
        }
        text = build_agent_feedback_block(fb)
        assert "读法" not in text

    def test_empty_returns_empty(self):
        assert build_agent_feedback_block(None) == ""
        assert "暂无已平仓记录" in build_agent_feedback_block({"n": 0, "rows": []})
