"""盘中结构/量能块渲染（纯函数）单测。"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from backend.shared.decision_blocks import build_orderbook_block
from backend.shared.intraday_live_blocks import build_intraday_live_block

CST = ZoneInfo("Asia/Shanghai")


def _holdings(*codes: str):
    return tuple(SimpleNamespace(code=code, name="") for code in codes)


def test_returns_empty_without_doc():
    assert build_intraday_live_block(None) == ""
    assert build_intraday_live_block({}) == ""


def test_renders_breadth_and_limit_ladder():
    doc = {
        "ts": 0,
        "source": "tdx_bridge",
        "breadth": {
            "n": 5582,
            "up": 1234,
            "down": 3456,
            "flat": 892,
            "limit_up": 38,
            "limit_down": 5,
            "broke": 12,
            "broke_rate": 24.0,
            "sample": "full",
            "broke_sampled": True,
            "top_limit_up": [
                {
                    "code": "300530.SZ",
                    "name": "达志科技",
                    "pct": 20.01,
                    "boards": 3,
                    "seal": 120_000_000.0,
                },
                {"code": "002281.SZ", "name": "光迅科技", "pct": 10.0, "boards": 1},
            ],
        },
    }
    text = build_intraday_live_block(doc, now=datetime(2026, 11, 16, 13, 5, tzinfo=CST))
    assert text.startswith("【盘中结构】")
    assert "全市场 5,582 只" in text
    assert "涨 1,234 / 跌 3,456 / 平 892" in text
    assert "涨停 38" in text and "跌停 5" in text
    assert "炸板 12（炸板率 24.0%）" in text
    assert "达志科技(300530.SZ) 3板 +20.01%" in text
    assert "封单 1.20 亿" in text
    assert "炸板按轮询采样统计" in text


def test_renders_sector_strength():
    doc = {
        "ts": 0,
        "sectors": [
            {"name": "煤炭开采", "chg": 2.31, "limit_up": 3},
            {"name": "银行", "chg": 0.55, "limit_up": 0},
            {"name": "半导体", "chg": -1.42, "limit_up": 1},
        ],
    }
    text = build_intraday_live_block(doc)
    assert "板块领涨：煤炭开采 +2.31%（涨停 3） ｜ 银行 +0.55%" in text
    assert "板块领跌：半导体 -1.42%（涨停 1）" in text


def test_renders_metrics_and_dedupes_holdings_pool():
    doc = {
        "ts": 0,
        "metrics": {
            "SH600036": {
                "name": "招商银行",
                "vr": 1.12,
                "speed": 0.03,
                "pos": 0.62,
                "zjl": 17842.75,
                "wtb": 12.0,
                "open_zaf": 0.39,
                "depth_levels": 5,
            },
            "SZ000725": {
                "name": "京东方A",
                "vr": None,
                "speed": None,
                "pos": None,
                "depth_levels": 1,
            },
        },
    }
    holdings = _holdings("SH600036")
    pool = _holdings("SH600036", "SZ000725")
    text = build_intraday_live_block(doc, holdings=holdings, pool=pool)
    assert text.count("招商银行(SH600036)") == 1
    assert "[持仓] 招商银行(SH600036)：量比 1.12 ｜ 涨速 +0.03% ｜ 日内位置 62%" in text
    assert "资金净流入 +1.78 亿" in text
    assert "委比 +12.0%" in text
    assert "[池] 京东方A(SZ000725)：量比 — ｜ 涨速 — ｜ 日内位置 —" in text
    assert "盘口 一档" in text
    assert "盘口档位=1" in text


def test_renders_events_and_stale_header():
    doc = {
        "ts": 1000.0,
        "events": [
            {"ts": 1000.0, "text": "某某(300001.SZ) 涨停"},
            {"ts": 1001.0, "text": "某某(300002.SZ) 炸板"},
        ],
    }
    now = datetime.fromtimestamp(1000.0 + 600, tz=CST)
    text = build_intraday_live_block(doc, now=now)
    assert "快照已过期" in text
    assert "10 分钟前" in text
    assert "某某(300001.SZ) 涨停" in text
    assert "某某(300002.SZ) 炸板" in text


def test_marks_non_continuous_phase_as_unsettled():
    doc = {"ts": 0, "phase": "non_continuous", "breadth": {"n": 10, "up": 1}}
    text = build_intraday_live_block(doc)
    assert "非连续竞价阶段" in text and "未定型" in text


def test_metrics_missing_is_counted_not_silently_dropped():
    doc = {"ts": 0, "metrics": {"SH600036": {"name": "招商银行", "vr": 1.1}}}
    text = build_intraday_live_block(
        doc,
        holdings=_holdings("SH600036", "SH601668"),
    )
    assert "另有 1 只持仓/池本轮无量能数据" in text


def test_never_fabricates_zero_for_missing_values():
    doc = {"ts": 0, "metrics": {"SH600036": {"name": "招商银行"}}}
    text = build_intraday_live_block(doc, holdings=_holdings("SH600036"))
    assert "量比 —" in text
    assert "量比 0.00" not in text
    assert "资金净流入" not in text  # 缺失的块不渲染，不写 +0 万


def test_orderbook_marks_one_level_source_honestly():
    holdings = (SimpleNamespace(code="SH600036", name="招商银行"),)
    snap = {
        "Now": "43.20",
        "PreClose": "42.93",
        "bid1": "43.19",
        "ask1": "43.21",
        "bid_vol1": "10",
        "ask_vol1": "20",
    }
    text = build_orderbook_block(
        holdings, {"SH600036": snap}, {}, datetime(2026, 11, 16, 13, 5, tzinfo=CST)
    )
    assert "一档失衡" in text
    assert "五档失衡 +" not in text and "五档失衡 -" not in text
    assert "只回 1 档" in text


def test_orderbook_keeps_five_level_wording_when_source_has_it():
    holdings = (SimpleNamespace(code="SH600036", name="招商银行"),)
    snap = {"Now": "43.20", "PreClose": "42.93"}
    for i in range(1, 6):
        snap[f"bid{i}"] = str(43.20 - i * 0.01)
        snap[f"ask{i}"] = str(43.20 + i * 0.01)
        snap[f"bid_vol{i}"] = "10"
        snap[f"ask_vol{i}"] = "20"
    text = build_orderbook_block(
        holdings, {"SH600036": snap}, {}, datetime(2026, 11, 16, 13, 5, tzinfo=CST)
    )
    assert "五档失衡" in text
    assert "只回一档" not in text
