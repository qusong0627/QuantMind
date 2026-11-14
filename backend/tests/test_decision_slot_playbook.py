"""槽位守则与今日 10:10 块：预算/阶段/时间门控的纯逻辑测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend.shared.decision_slot_playbook import (
    SLOT_NEW_BUY_BUDGET,
    build_slot_playbook_block,
)
from backend.shared.intraday_1010 import build_intraday_1010_block

CST = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 16, 10, 15, tzinfo=CST)


class TestSlotPlaybook:
    def test_budget_map_keeps_all_slots(self):
        assert SLOT_NEW_BUY_BUDGET["1015"] == 2
        assert SLOT_NEW_BUY_BUDGET["1445"] == 0
        assert SLOT_NEW_BUY_BUDGET["1200"] == 0

    def test_1015_uses_1010_and_q1_q2(self):
        text = build_slot_playbook_block(
            "1015", stage={"score": -9, "state": "防守"}, now=NOW
        )
        assert text.startswith("【槽位守则｜1015 10:15】")
        assert "本轮新开预算：最多 2 笔" in text
        assert "10:10 快照刚出" in text
        assert "Q1/Q2" in text
        assert "防守阶段" in text
        assert "双确认" in text

    def test_1445_no_new_and_overnight_rule(self):
        text = build_slot_playbook_block("1445", now=NOW)
        assert "0 笔（禁新开）" in text
        assert "14:50 后只限价" in text
        assert "弱票收盘卖" in text

    def test_unknown_slot_still_renders(self):
        text = build_slot_playbook_block("9999", now=NOW)
        assert text.startswith("【槽位守则｜9999")
        assert "按档位预算" in text


class TestIntraday1010Gate:
    def test_before_1010_is_empty(self):
        before = datetime(2026, 11, 16, 10, 5, tzinfo=CST)
        assert build_intraday_1010_block(before) == ""
