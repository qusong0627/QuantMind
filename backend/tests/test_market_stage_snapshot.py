"""收盘市场阶段快照（``shared.market_stage_snapshot``）纯渲染单测。

不读真实快照文件、不连 Redis/PG：只吃构造的 dict，守三件事：
1. 六块/五层/事件/情绪/个股广度都进文本；
2. 缺失的个股快照/情绪如实写“不可得/—”；
3. 交易 agent 提示词必须带“上一交易日收盘，不是盘中实时”。
"""

from __future__ import annotations

from backend.shared.market_stage_snapshot import (
    render_market_stage_prompt,
    render_stage_text,
)

SNAP = {
    "date": "2026-11-13",
    "universe": "ALL",
    "universe_label": "全A等权",
    "state": "防守",
    "score": -9,
    "summary": "长期下方、中期下跌中继、短期超卖",
    "exposure": {"actual": 0.2, "ceiling": 0.4},
    "top_risk": {"flag": False, "text": "无"},
    "bottom": {"flag": False, "text": "无"},
    "events": {
        "trin_vol": 0.49,
        "trin_surrender": False,
        "amount_gap_pp": 9.2,
        "amount_gap_risk": False,
        "adl_divergence": False,
    },
    "stock_breadth": {
        "available": True,
        "sample": "hot_set",
        "n": 587,
        "quintiles": [
            {"q": "Q1", "ret_after": -0.91},
            {"q": "Q2", "ret_after": -1.03},
            {"q": "Q3", "ret_after": -1.07},
            {"q": "Q4", "ret_after": -1.36},
            {"q": "Q5", "ret_after": -1.68},
        ],
    },
    "layers": [
        {"key": "long", "label": "长期", "stage": "MA250下方", "score": -2},
        {"key": "medium", "label": "中期", "stage": "下跌中继", "score": -1},
        {"key": "short", "label": "短期", "stage": "超卖（反抽候选）", "score": 1},
        {"key": "intraday", "label": "当日", "stage": "中性", "score": 0},
    ],
    "sector": {
        "up_share": 7.0,
        "dispersion": 1.37,
        "top": [{"name": "影视院线", "ret": 4.37}],
        "bottom": [{"name": "元器件", "ret": -6.33}],
    },
    "breadth": {"profit_effect": 13.7, "total_turnover_yi": 16670.7},
    "sentiment": {
        "stress_score": 70.7,
        "zone_label": "承压",
        "main_net_yi": 26.54,
    },
    "gaps": [],
}


class TestRenderStageText:
    def test_core_fields(self):
        text = render_stage_text(SNAP)

        assert "【市场阶段｜全A等权｜截至 2026-11-13 收盘】" in text
        assert "六块 -9/±12（防守）" in text
        assert "长期 MA250下方" in text and "短期 超卖（反抽候选）" in text
        assert "基础暴露 0.2 / 0.4" in text
        assert "TRIN_量 0.49（不投降）" in text
        assert "额价差 +9.2pp（未到20pp）" in text
        assert "ADL 背离 无" in text
        assert "情绪压力 70.7（承压）" in text
        assert "赚钱效应 13.7" in text
        assert "主力净流入 26.5 亿" in text
        assert "Q1 -0.91" in text and "Q5 -1.68" in text
        assert "热集 587 只" in text
        assert "影视院线 +4.37%" in text

    def test_stock_breadth_missing_is_explicit(self):
        snap = {**SNAP, "stock_breadth": {"available": False}}
        text = render_stage_text(snap)
        assert "该日无 10:10 快照，Q1–Q5 不可得" in text

    def test_gaps_are_carried(self):
        snap = {**SNAP, "gaps": ["sentiment_snapshot_missing_for_day"]}
        assert "数据缺口：sentiment_snapshot_missing_for_day" in render_stage_text(snap)


class TestRenderPrompt:
    def test_prompt_has_prior_close_caveat(self):
        text = render_market_stage_prompt(SNAP)
        assert text.startswith("【上一交易日市场阶段·参考（截至 2026-11-13 收盘）】")
        assert "上一交易日收盘，不是盘中实时" in text
        assert "六块 -9/±12（防守）" in text

    def test_missing_snapshot_renders_empty_block(self):
        assert render_market_stage_prompt(None) == ""
