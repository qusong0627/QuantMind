"""建议卡兑现回填测试（T-P6-16 闭环）：口径纯函数（U）+ 真机回填（I）。

验收口径：
- 方向调整：buy 取实际收益、sell 取规避收益；超额=个股−沪深300；hit=超额>0；
- 渐进兑现：缺 horizon 不进 summary 分母，全部齐才 done；
- 纯建议卡（无动作）not_scorable；超宽限无行情 no_data（绝不编造收益）。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

_CST = timezone(timedelta(hours=8))


@pytest.mark.unit
def test_score_actions_direction_hit_and_progressive(monkeypatch):
    from backend.services.trade.services import sentinel_backfill as sb
    from backend.services.trade.services.advice_backfill import score_actions

    def fake_loader(symbol, base_date, horizons=(1,), *, view=None, normalize=True):
        if str(symbol).upper() == "000300.SH":
            return 4000.0, {1: 4020.0, 3: 4040.0, 5: 4080.0}  # +0.5% / +1% / +2%
        if str(symbol) == "600036.SH":
            return 40.0, {1: 40.8}  # buy：T+1 +2%，T+3/T+5 未到
        if str(symbol) == "600000.SH":
            return 10.0, {1: 9.8, 3: 9.6}  # sell：下跌 = 规避收益为正
        return None

    monkeypatch.setattr(sb, "load_relative_closes", fake_loader)
    doc = score_actions(
        [
            {"symbol": "600036.SH", "side": "buy"},
            {"symbol": "600000.SH", "side": "sell"},
        ],
        date(2026, 9, 18),
    )
    assert doc is not None
    buy_t1 = doc["actions"][0]["horizons"]["1"]
    assert buy_t1["ret"] == pytest.approx(0.02)
    assert buy_t1["bench"] == pytest.approx(0.005)
    assert buy_t1["excess"] == pytest.approx(0.015)
    assert buy_t1["hit"] is True
    sell_t1 = doc["actions"][1]["horizons"]["1"]
    assert sell_t1["ret"] == pytest.approx(0.02)  # -(9.8/10 - 1)
    assert sell_t1["hit"] is True
    # 渐进：T+1 两条、T+3 仅 sell 一条、T+5 空分母
    assert doc["summary"]["1"]["n"] == 2 and doc["summary"]["1"]["hits"] == 2
    assert doc["summary"]["3"]["n"] == 1
    assert doc["summary"]["5"]["n"] == 0
    assert doc["base_date"] == "2026-09-18" and doc["benchmark"] == "000300.SH"


@pytest.mark.unit
def test_all_horizons_present_gate():
    from backend.services.trade.services.advice_backfill import _all_horizons_present

    partial = {"actions": [{"horizons": {"1": {}, "3": {}}}]}
    assert _all_horizons_present(partial) is False
    full = {"actions": [{"horizons": {"1": {}, "3": {}, "5": {}}}]}
    assert _all_horizons_present(full) is True
    none = {"actions": [{"error": "no_base_close"}]}
    assert _all_horizons_present(none) is False


@pytest.mark.unit
def test_score_actions_none_when_no_progress(monkeypatch):
    from backend.services.trade.services import sentinel_backfill as sb
    from backend.services.trade.services.advice_backfill import score_actions

    monkeypatch.setattr(sb, "load_relative_closes", lambda *a, **k: None)
    assert score_actions([{"symbol": "600036.SH", "side": "buy"}], date(2026, 9, 18)) is None


@pytest.mark.integration
def test_backfill_once_fills_real_advice_and_cleans_up():
    """真机：插入决策于 10 天前的建议卡 → 回填 → 断言 outcome 落库（决策日收盘口径）→ 清理。

    全程走同步 session（sync_session）：backfill 本身就是同步实现，且规避
    asyncpg 池跨事件循环绑定（asyncio.run 多次会触发 "different loop"）。
    """
    from sqlalchemy import text as sql_text

    from backend.services.trade.services.advice_backfill import backfill_once
    from backend.shared.copilot_contract import ensure_copilot_advice_table
    from backend.shared.sync_db import sync_session

    assert ensure_copilot_advice_table()
    advice_id = str(uuid.uuid4())
    decided = (datetime.now(_CST) - timedelta(days=10)).replace(
        hour=15, minute=30, second=0, microsecond=0
    )
    try:
        with sync_session() as session:
            session.execute(
                sql_text(
                    "INSERT INTO copilot_advice (advice_id, tenant_id, user_id, source, title, "
                    "rationale, actions, status, decided_at) VALUES "
                    "(CAST(:a AS UUID), 'default', 0, 'test', 'T-advice-backfill', '', "
                    "CAST(:ac AS JSONB), 'rejected', :dt)"
                ),
                {
                    "a": advice_id,
                    "ac": '[{"symbol": "600036.SH", "side": "buy", "quantity": 100}]',
                    "dt": decided,
                },
            )
            session.commit()
        stats = backfill_once(limit=500)
        assert stats.get("failed", 0) == 0
        with sync_session() as session:
            row = session.execute(
                sql_text(
                    "SELECT outcome, outcome_status FROM copilot_advice "
                    "WHERE advice_id = CAST(:a AS UUID)"
                ),
                {"a": advice_id},
            ).fetchone()
        assert row is not None, f"建议卡行丢失（stats={stats}）"
        outcome, outcome_status = row
        assert outcome is not None, f"outcome 未回填（stats={stats}）"
        assert outcome_status in ("done", "partial")
        assert outcome["base_date"] == decided.date().isoformat()
        s1 = outcome["summary"]["1"]
        assert s1["n"] == 1
        assert isinstance(s1["hits"], int)
        assert outcome["actions"][0]["symbol"] == "600036.SH"
        assert "1" in outcome["actions"][0]["horizons"]
    finally:
        with sync_session() as session:
            session.execute(
                sql_text(
                    "DELETE FROM copilot_advice WHERE advice_id = CAST(:a AS UUID)"
                ),
                {"a": advice_id},
            )
            session.commit()
