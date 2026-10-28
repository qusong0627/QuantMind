"""分账实盘净值/统计的纯核心单测（arena 本地模块）。

2026-10-20 用户口径：智能体台账只认通达信实盘（``qm_agent_ledger_*``），
不再用退役文件里的模拟盘回放。这里锁三件事：

1. **反推锚定**：以当前现金/持仓为终点、沿成交历史倒放得到起点，再正向重放；
   净值曲线因此与账本逐分对得上（不是拿成本价假装市值）。
2. **日线拼接**：历史用 ``stock_daily_new_YYYY_MM.close``，当日用实时快照；
   缺价只许前值顺延并标 ``incomplete``，绝不拿成本价冒充现价。
3. **统计口径**：回合台账（roundtrip）算胜率/盈亏比/期望，费用未知时
   ``total_fee=None``（缺失显示 —，不是 0）。

同日多笔成交、部分卖出、停牌缺价、旧文件线拼接都在覆盖内。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from backend.services.agent_arena import live_family as lf

_TZ = ZoneInfo("Asia/Shanghai")


def _fill(day: date, code: str, side: str, volume: float, price: float, seq: int = 0):
    return {
        "id": seq,
        "day": day,
        "ts": datetime(day.year, day.month, day.day, 10, 0, tzinfo=_TZ),
        "code": code,
        "side": side,
        "volume": volume,
        "price": price,
    }


# ── 反推 + 正向重放 ────────────────────────────────────────────────
def test_reverse_anchor_then_replay_matches_every_fill():
    d1, d2, d3 = date(2026, 10, 8), date(2026, 10, 9), date(2026, 10, 12)
    fills = [
        _fill(d1, "600036.SH", "buy", 100, 10.0, 1),
        _fill(d2, "600036.SH", "sell", 50, 12.0, 2),
        _fill(d3, "600036.SH", "buy", 50, 11.0, 3),
    ]
    closes = {
        d1: {"600036.SH": 11.0},
        d2: {"600036.SH": 12.0},
        d3: {"600036.SH": 10.0},
    }

    series = lf.build_ledger_equity_series(
        fills,
        end_cash=9050.0,
        end_positions={"600036.SH": 100},
        closes=closes,
        quotes={},
        today=d3,
    )

    # 反推起点：10000 现金、空仓
    assert [(p["date"], p["cash"], p["equity"]) for p in series] == [
        ("2026-10-08", 9000.0, 10100.0),
        ("2026-10-09", 9600.0, 10200.0),
        ("2026-10-12", 9050.0, 10050.0),
    ]
    assert series[0]["ts"].startswith("2026-10-08T15:00:00")
    assert series[-1]["market_value"] == 1000.0
    assert all(p["incomplete"] is False for p in series)


def test_today_uses_live_quote_not_last_close():
    d1, d2 = date(2026, 10, 19), date(2026, 10, 20)
    fills = [_fill(d1, "600036.SH", "buy", 100, 10.0, 1)]
    series = lf.build_ledger_equity_series(
        fills,
        end_cash=9000.0,
        end_positions={"600036.SH": 100},
        closes={d1: {"600036.SH": 11.0}},
        quotes={"600036.SH": 13.0},
        today=d2,
        now=datetime(2026, 10, 20, 10, 30, tzinfo=_TZ),
    )

    assert series[-1]["date"] == "2026-10-20"
    assert series[-1]["equity"] == 10300.0
    assert series[-1]["ts"].startswith("2026-10-20T10:30:00")


def test_missing_close_forward_fills_last_price_and_flags_incomplete():
    d1, d2 = date(2026, 10, 8), date(2026, 10, 9)
    fills = [_fill(d1, "600036.SH", "buy", 100, 10.0, 1)]
    series = lf.build_ledger_equity_series(
        fills,
        end_cash=9000.0,
        end_positions={"600036.SH": 100},
        closes={d1: {"600036.SH": 12.0}},  # 次日停牌/无行
        quotes={},
        today=d2,
    )

    assert series[1]["equity"] == 10200.0  # 前值 12.0 顺延
    assert series[1]["incomplete"] is True


def test_no_fills_but_open_position_yields_today_mark_only():
    series = lf.build_ledger_equity_series(
        [],
        end_cash=5000.0,
        end_positions={"600036.SH": 100},
        closes={},
        quotes={"600036.SH": 10.0},
        today=date(2026, 10, 20),
    )
    assert len(series) == 1
    assert series[0]["equity"] == 6000.0


# ── 回合统计 ───────────────────────────────────────────────────────
def _rt(pnl, hold, closed=True):
    return {
        "realized_pnl": pnl,
        "holding_days": hold,
        "closed": closed,
        "volume": 100,
        "cost_price": 10.0,
        "sell_price": 10.0 + pnl / 100,
    }


def test_roundtrip_stats_match_existing_summary_semantics():
    rows = [
        _rt(200.0, 3.0),
        _rt(100.0, 1.0),
        _rt(-50.0, 2.0),
        _rt(-150.0, 4.0),
        _rt(0.0, 5.0, closed=False),
    ]
    stats = lf.summarize_ledger_roundtrips(rows)

    assert stats["closed_trades"] == 5
    assert stats["win_rate"] == 0.4  # 2 / 5（与旧 summary 同口径，0 计入亏损侧）
    assert stats["profit_factor"] == 1.5  # 300 / 200
    assert stats["biggest_win"] == 200.0
    assert stats["biggest_loss"] == -150.0
    assert stats["avg_trade_pnl"] == 20.0
    assert stats["expectancy"] == 20.0  # 0.4*150 − 0.6*66.67
    assert stats["avg_hold_days"] == 3.0
    assert stats["median_hold_days"] == 3.0
    assert stats["full_liquidations"] == 4
    assert stats["total_fee"] is None
    assert stats["fee_ratio"] is None


def test_roundtrip_stats_empty_is_all_none_not_zero():
    stats = lf.summarize_ledger_roundtrips([])
    assert stats["closed_trades"] == 0
    assert stats["win_rate"] is None
    assert stats["profit_factor"] is None
    assert stats["expectancy"] is None


# ── 旧文件线拼接 ───────────────────────────────────────────────────
def test_merge_replaces_stale_file_points_after_cutover():
    file_data = {
        "total": [{"date": "2026-10-20", "ts": "2026-10-20T10:00:00+08:00", "value": 800000}],
        "agents": {
            "flash": [
                {"date": "2026-09-29", "ts": "2026-09-29T15:00:00+08:00", "value": 125000.0},
                # 冻结账本算出的错误值（09-30 起）：必须被 PG 日线替换
                {"date": "2026-10-09", "ts": "2026-10-09T15:00:00+08:00", "value": 999999.0},
                # 采样器修复后写出的当日真实点（带 source=pg）
                {"date": "2026-10-20", "ts": "2026-10-20T13:05:00+08:00", "value": 114800.0, "source": "pg"},
            ]
        },
    }
    pg_series = {
        "flash": [
            {"date": "2026-09-30", "ts": "2026-09-30T15:00:00+08:00", "equity": 124000.0},
            {"date": "2026-10-09", "ts": "2026-10-09T15:00:00+08:00", "equity": 121000.0},
            {"date": "2026-10-19", "ts": "2026-10-19T15:00:00+08:00", "equity": 115000.0},
            {"date": "2026-10-20", "ts": "2026-10-20T10:30:00+08:00", "equity": 114790.0},
        ]
    }

    merged = lf.merge_live_equity(file_data, pg_series)
    pts = merged["agents"]["flash"]

    assert pts[0]["value"] == 125000.0  # 冻结前保留分钟级历史
    assert [p["value"] for p in pts[1:]] == [124000.0, 121000.0, 115000.0, 114800.0]
    # 当日以采样器真实分钟点收尾，不追加 PG 的 10:30 单点
    assert pts[-1]["ts"] == "2026-10-20T13:05:00+08:00"
    assert all(p["value"] != 999999.0 for p in pts)
    # 总账户线原样保留
    assert merged["total"] == file_data["total"]


# ── 分账视图补充现价/权益 ─────────────────────────────────────────
def test_ledger_view_enriches_cash_market_value_and_equity():
    from backend.shared.decision.agent_ledger import agent_quota

    ledger = {
        "agents": {
            "flash": {
                "virtual_cash": 9000.0,
                "positions": {
                    "600036.SH": {"volume": 100, "cost_price": 10.0, "buy_ts": ""},
                    "000001.SZ": {"volume": 200, "cost_price": 5.0, "buy_ts": ""},
                },
            }
        }
    }
    view = lf.ledger_view(
        ledger, {"600036.SH": "招商银行"},
        prices={"600036.SH": 12.0},  # 000001 缺价
    )
    rec = view["agents"]["flash"]

    assert rec["cash"] == 9000.0
    assert rec["used"] == 2000.0
    assert rec["remaining"] == agent_quota() - 2000.0
    assert rec["market_value"] == 1200.0
    # 缺价持仓不许被悄悄丢掉：equity 为 None（UI 显示 —），不是 cash+部分市值
    assert rec["equity"] is None
    assert rec["incomplete"] is True
    by_code = {p["code"]: p for p in rec["positions"]}
    assert by_code["600036.SH"]["price"] == 12.0
    assert by_code["600036.SH"]["market_value"] == 1200.0
    assert by_code["600036.SH"]["pnl"] == 200.0
    assert by_code["000001.SZ"]["price"] is None
    assert by_code["000001.SZ"]["market_value"] is None
    assert by_code["000001.SZ"]["pnl"] is None


def test_ledger_view_equity_complete_prices():
    ledger = {
        "agents": {
            "flash": {
                "virtual_cash": 9000.0,
                "positions": {
                    "600036.SH": {"volume": 100, "cost_price": 10.0, "buy_ts": ""},
                    "000001.SZ": {"volume": 200, "cost_price": 5.0, "buy_ts": ""},
                },
            }
        }
    }
    rec = lf.ledger_view(
        ledger, {}, prices={"600036.SH": 12.0, "000001.SZ": 4.5}
    )["agents"]["flash"]
    assert rec["market_value"] == 2100.0
    assert rec["equity"] == 11100.0
    assert rec["incomplete"] is False
    by_code = {p["code"]: p for p in rec["positions"]}
    assert by_code["000001.SZ"]["pnl"] == -100.0
    assert by_code["000001.SZ"]["pnl_pct"] == -0.1
    # 有价时 position_value 是市值（Live 页按它估单价）
    assert by_code["600036.SH"]["position_value"] == 1200.0


def test_merge_does_not_duplicate_days_covered_by_file_history():
    """旧文件线已覆盖的交易日，不再追加 PG 日线点（两套口径同一天不出锯齿）。"""
    file_data = {
        "total": [],
        "agents": {
            "flash": [
                {"date": "2026-09-11", "ts": "2026-09-11T15:00:00+08:00", "value": 125292.8},
                {"date": "2026-09-29", "ts": "2026-09-29T15:00:00+08:00", "value": 126000.0},
            ]
        },
    }
    pg_series = {
        "flash": [
            {"date": "2026-09-11", "ts": "2026-09-11T15:00:00+08:00", "equity": 125000.0},
            {"date": "2026-09-20", "ts": "2026-09-20T15:00:00+08:00", "equity": 124500.0},
            {"date": "2026-09-30", "ts": "2026-09-30T15:00:00+08:00", "equity": 124000.0},
            {"date": "2026-10-20", "ts": "2026-10-20T10:30:00+08:00", "equity": 114790.0},
        ]
    }
    pts = lf.merge_live_equity(file_data, pg_series)["agents"]["flash"]
    assert [p["date"] for p in pts] == [
        "2026-09-11",
        "2026-09-29",  # 文件线最后一点
        "2026-09-30",
        "2026-10-20",
    ]


def test_merge_fills_pg_days_after_file_history_ends_before_cutover():
    """文件线提前断档时，PG 日线仍补齐断档日（不漏历史）。"""
    file_data = {
        "total": [],
        "agents": {
            "flash": [
                {"date": "2026-09-11", "ts": "2026-09-11T15:00:00+08:00", "value": 125292.8},
            ]
        },
    }
    pg_series = {
        "flash": [
            {"date": "2026-09-11", "ts": "2026-09-11T15:00:00+08:00", "equity": 125000.0},
            {"date": "2026-09-15", "ts": "2026-09-15T15:00:00+08:00", "equity": 124500.0},
            {"date": "2026-09-30", "ts": "2026-09-30T15:00:00+08:00", "equity": 124000.0},
            {"date": "2026-10-20", "ts": "2026-10-20T10:30:00+08:00", "equity": 114790.0},
        ]
    }
    pts = lf.merge_live_equity(file_data, pg_series)["agents"]["flash"]
    assert [p["date"] for p in pts] == [
        "2026-09-11",
        "2026-09-15",
        "2026-09-30",
        "2026-10-20",
    ]


def test_merge_seam_disagrees_keeps_file_history_skips_pg_daily():
    """迁移缺口造成的接缝断层：保留冻结点前的真实采样，跳过 PG 反推日线。

    2026-10-20 实测：pro 文件线末点 102,967.8 vs PG 首点 84,780.8（-17.7%）、
    glm 96,670.34 vs 85,334.34（-11.7%）——都是期初持仓未入 PG 账的迁移缺口，
    不是市场波动。PG 日线是从今日账本反推的重建值，断层说明它的历史水平不可
    信；旧线（冻结前分钟采样）保留，但中间不混入 PG 重建日线，直接连到当日
    PG 尾点——两端都是真值，也不再把整段历史吞掉。
    """
    file_data = {
        "total": [],
        "agents": {
            "pro": [
                {"date": "2026-09-11", "ts": "2026-09-11T15:00:00+08:00", "value": 100000.0},
                {"date": "2026-09-29", "ts": "2026-09-29T15:00:00+08:00", "value": 102967.8},
                # 采样器当日真实分钟点：仍以它收尾
                {"date": "2026-10-20", "ts": "2026-10-20T13:05:00+08:00", "value": 86060.0, "source": "pg"},
            ]
        },
    }
    pg_series = {
        "pro": [
            {"date": "2026-09-29", "ts": "2026-09-29T15:00:00+08:00", "equity": 84780.8},
            {"date": "2026-09-30", "ts": "2026-09-30T15:00:00+08:00", "equity": 84843.8},
            {"date": "2026-10-20", "ts": "2026-10-20T10:30:00+08:00", "equity": 86018.8},
        ]
    }
    pts = lf.merge_live_equity(file_data, pg_series)["agents"]["pro"]

    # 旧线保留（左侧历史不再消失）；PG 反推日线（09-30）被跳过，避免把
    # 不可信的重建值画成真实逐日曲线；当日 source=pg 的尾点仍然收尾。
    assert [p["date"] for p in pts] == ["2026-09-11", "2026-09-29", "2026-10-20"]
    assert pts[1]["value"] == 102967.8
    assert pts[-1]["ts"] == "2026-10-20T13:05:00+08:00"


def test_merge_keeps_file_line_when_seam_within_tolerance():
    """接缝两侧水平一致（<5%）→ 保留冻结前的分钟级历史。

    差 3% 跨越 10 个自然日（含国庆长假）属于正常行情波动，不是迁移断层。
    """
    file_data = {
        "total": [],
        "agents": {
            "flash": [
                {"date": "2026-09-29", "ts": "2026-09-29T15:00:00+08:00", "value": 100000.0},
            ]
        },
    }
    pg_series = {
        "flash": [
            {"date": "2026-10-09", "ts": "2026-10-09T15:00:00+08:00", "equity": 103000.0},
            {"date": "2026-10-20", "ts": "2026-10-20T10:30:00+08:00", "equity": 104000.0},
        ]
    }
    pts = lf.merge_live_equity(file_data, pg_series)["agents"]["flash"]
    assert [p["value"] for p in pts] == [100000.0, 103000.0, 104000.0]


def test_load_equity_preserves_point_source(tmp_path, monkeypatch):
    """load_equity 必须保留 source：merge_live_equity 靠它识别当日真实分钟点。

    2026-10-20 线上故障：加载器重建点字典时漏掉 ``source``，``source=pg`` 的
    当日采样尾巴整段被判为非 PG 点，图表当天只剩一个单点（曲线无盘中形状）。
    """
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "live_equity.jsonl").write_text(
        '{"date":"2026-10-20","ts":"2026-10-20T09:30:00+08:00","value":100000,'
        '"agent":"flash","source":"pg"}\n'
        '{"date":"2026-10-20","ts":"2026-10-20T09:31:00+08:00","value":100010,'
        '"agent":"flash"}\n'
        '{"date":"2026-10-20","ts":"2026-10-20T09:32:00+08:00","value":900000}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("QM_ARENA_DATA_ROOT", str(tmp_path))

    data = lf.load_equity()

    assert data["agents"]["flash"][0]["source"] == "pg"
    assert data["agents"]["flash"][1].get("source") is None
    assert data["total"][0]["value"] == 900000.0


def test_load_then_merge_keeps_today_intraday_tail(tmp_path, monkeypatch):
    """端到端锁：文件里的当日 ``source=pg`` 分钟点必须用作曲线收尾。

    只测 merge 不够——旧测试直接喂字典，绕过了 load_equity；线上正是加载器
    丢字段导致今日尾巴退化成一个 PG 单点。
    """
    logs = tmp_path / "logs"
    logs.mkdir()
    rows = [
        '{"date":"2026-09-29","ts":"2026-09-29T15:00:00+08:00","value":100000,'
        '"agent":"flash"}',
        '{"date":"2026-10-20","ts":"2026-10-20T09:30:00+08:00","value":100200,'
        '"agent":"flash","source":"pg"}',
        '{"date":"2026-10-20","ts":"2026-10-20T13:05:00+08:00","value":100150,'
        '"agent":"flash","source":"pg"}',
    ]
    (logs / "live_equity.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")
    monkeypatch.setenv("QM_ARENA_DATA_ROOT", str(tmp_path))

    pg_series = {
        "flash": [
            {"date": "2026-09-30", "ts": "2026-09-30T15:00:00+08:00", "equity": 100100.0},
            {"date": "2026-10-20", "ts": "2026-10-20T10:30:00+08:00", "equity": 100190.0},
        ]
    }
    pts = lf.merge_live_equity(lf.load_equity(), pg_series)["agents"]["flash"]
    today = [p for p in pts if p["date"] == "2026-10-20"]

    assert [p["ts"] for p in today] == [
        "2026-10-20T09:30:00+08:00",
        "2026-10-20T13:05:00+08:00",
    ]
    assert pts[-1]["ts"] == "2026-10-20T13:05:00+08:00"


def test_merge_live_equity_keeps_agents_without_pg_series():
    file_data = {
        "total": [],
        "agents": {"old": [{"date": "2026-10-09", "ts": "2026-10-09T10:00:00+08:00", "value": 1}]},
    }
    merged = lf.merge_live_equity(file_data, {})
    assert merged["agents"]["old"] == file_data["agents"]["old"]
