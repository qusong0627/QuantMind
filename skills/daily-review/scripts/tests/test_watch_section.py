"""§九 自选/持仓复盘的渲染单测（含**持仓源注记**，T3-1 的展示面验收）。

跑法：``cd skills/daily-review/scripts && python3 -m pytest tests/ -q``（宿主侧）。

盯两件事：注记（停更/缺失/读取失败）必须原样进报告——该段可以**只有注记没有表**
（空段+提示），但绝不许在注记之下出现任何编出来的持仓行；老行为（无注记时整段
与历史输出逐字一致）不许被改坏。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import daily_review as dr  # noqa: E402


def _row(**over):
    row = {
        "symbol": "600036.SH",
        "name": "招商银行",
        "industry": "银行",
        "close": 40.1,
        "pct": 1.2,
        "amount_yi": 12.3,
        "turnover_pct": 0.5,
        "ma20": 39.0,
        "category": "normal",
    }
    row.update(over)
    return row


class TestWatchSection:
    def test_no_watch_and_no_note_renders_nothing(self):
        assert dr.render_watch_section({}) == ""
        assert dr.render_watch_section({"watch": [], "watch_note": None}) == ""

    def test_stale_note_renders_annotation_without_any_table(self):
        """验收：陈旧 → 标注 + 空段（表头都不出现，杜绝顺手填名单）。"""
        stats = {
            "watch": [],
            "watch_note": "⚠️ 持仓源 2026-09-29 停更：实盘快照最新一条早于复盘日 "
            "2026-10-09（快照未随当日更新），持仓段留空——绝不按旧名单编复盘。",
        }

        sec = dr.render_watch_section(stats)

        assert "## 九、自选/持仓复盘" in sec
        assert "> ⚠️ 持仓源 2026-09-29 停更" in sec  # 注记以引用块原样落报告
        assert "| 名称 |" not in sec  # 空段：没有表头
        assert "绝不按旧名单编复盘" in sec

    def test_missing_source_note_also_renders_as_empty_section(self):
        sec = dr.render_watch_section(
            {"watch": [], "watch_note": "⚠️ 实盘快照源缺失：real_account_snapshots 无记录，持仓段留空。"}
        )

        assert "源缺失" in sec and "| 名称 |" not in sec

    def test_fresh_note_precedes_the_table(self):
        stats = {
            "watch": [_row()],
            "watch_note": "持仓源：实盘快照 real_account_snapshots（2026-10-09 14:10 北京，qmt_exec/tdx_bridge 并集，1 只）",
        }

        sec = dr.render_watch_section(stats)

        assert sec.index("> 持仓源：") < sec.index("| 名称 |")  # 先交代出处，再列表
        assert "| 招商银行 | 600036.SH | 40.1 | +1.20% | 12.30 亿元 | 0.50% | 39.00 | 银行 | normal |" in sec

    def test_classic_section_without_note_is_unchanged(self):
        """无注记时保持历史形态：无引用块，表照旧。"""
        sec = dr.render_watch_section({"watch": [_row(ma20=None)]})

        assert ">" not in sec
        assert "| 名称 |" in sec and "[缺失]" in sec  # MA20 缺失走老旁路

    def test_row_with_note_field_renders_short_row(self):
        sec = dr.render_watch_section({"watch": [{"symbol": "000001.SZ", "name": "", "note": "当日无数据"}]})

        assert "|  | 000001.SZ | 当日无数据 |" in sec
