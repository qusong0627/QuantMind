"""建议卡规则生成器测试：选择/建卡纯函数（U）+ 真机两轮幂等（I）。

验收口径：
- 否决（风险告警）/已持仓/近 5 交易日重复 → 不生成；每轮 ≤3；BUY 侧 Top 次序；
- regime position_hint ≤ 0.4 → 整体停发；
- 真机：同参数两轮 → 第二轮 0 新建（gen 键幂等）；卡片动作过 validate_actions。
"""

from __future__ import annotations

from datetime import date

import pytest

from backend.services.trade.services import advice_generator as ag


@pytest.mark.unit
def test_select_candidates_filters_and_order():
    signals = [
        {"symbol": "600834", "fusion_score": 0.0115, "signal_side": "BUY"},
        {"symbol": "300014.SZ", "fusion_score": 0.0110, "signal_side": "BUY"},   # 有风险告警 → 否决
        {"symbol": "600036", "fusion_score": 0.0105, "signal_side": "BUY"},      # 已持仓 → 跳过
        {"symbol": "688121", "fusion_score": 0.0100, "signal_side": "BUY"},      # 近5日已生成 → 跳过
        {"symbol": "603359", "fusion_score": 0.0099, "signal_side": "SELL"},     # 非 BUY → 跳过
        {"symbol": "001322", "fusion_score": 0.0098, "signal_side": "BUY"},
        {"symbol": "002227", "fusion_score": 0.0097, "signal_side": "BUY"},
    ]
    picked = ag.select_candidates(
        signals=signals,
        veto_symbols={"300014.SZ"},
        held_symbols={"600036.SH", "688121.SH"},
        positive_symbols={"600834.SH"},
        k=3,
    )
    syms = [p["symbol"] for p in picked]
    assert syms == ["600834.SH", "001322.SZ", "002227.SZ"]
    assert picked[0]["resonance"] is True and picked[0]["rank"] == 1
    assert picked[1]["resonance"] is False


@pytest.mark.unit
def test_build_card_contract():
    card = ag.build_card(
        candidate={"symbol": "600834.SH", "fusion_score": 0.0115, "rank": 1, "resonance": True},
        gen_key="adv-gen-20260918-600834.sh",
        trade_date="2026-09-18",
        side_counts={"BUY": 1073, "SELL": 830, "HOLD": 3290},
        regime_hint=0.7,
    )
    assert card["actions"][0]["symbol"] == "600834.SH"
    assert card["actions"][0]["quantity"] == ag.OBSERVE_LOT
    assert card["context_refs"]["gen"].endswith("600834.sh")
    assert "纪律声明" in card["rationale"] and "观察仓" in card["title"]
    # 动作必须过 copilot 契约校验
    from types import SimpleNamespace

    from backend.services.api.routers.copilot import validate_actions

    ok = validate_actions([SimpleNamespace(**a) for a in card["actions"]])
    assert ok[0]["symbol"] == "600834.SH"


@pytest.mark.unit
def test_generate_skips_on_weak_regime(monkeypatch):
    import asyncio

    monkeypatch.setattr(ag, "_load_regime_hint", lambda: 0.3)
    stats = asyncio.run(ag.generate_once(today=date(2026, 9, 18)))
    assert stats["skipped_regime"] == 1 and stats["created"] == 0


@pytest.mark.integration
def test_generate_once_idempotent_two_rounds_real_db(monkeypatch):
    """真机两轮：第一轮建 ≤3 张（合成信号，专门符号），第二轮 0 新建（gen 键幂等）→ 清理。"""
    from sqlalchemy import text as sql_text

    from backend.shared.copilot_contract import ensure_copilot_advice_table
    from backend.shared.sync_db import sync_session

    assert ensure_copilot_advice_table()
    test_day = date(2026, 9, 18)
    my_symbols = ["600107.SH", "001322.SZ", "002227.SZ"]

    async def _fake_signals(limit: int = ag.TOP_N):
        # 仅含 3 只专用符号（第二轮应因 gen 键幂等整体 0 新建）
        return (
            [
                {"symbol": "600107", "fusion_score": 0.0115, "signal_side": "BUY"},
                {"symbol": "001322", "fusion_score": 0.0105, "signal_side": "BUY"},
                {"symbol": "002227", "fusion_score": 0.0100, "signal_side": "BUY"},
            ],
            "2026-09-18",
            {"BUY": 1073, "SELL": 830, "HOLD": 3290},
        )

    async def _fake_alerts():
        return set(), {"600834.SH"}

    async def _fake_held():
        return set()

    monkeypatch.setattr(ag, "_load_regime_hint", lambda: 0.7)
    monkeypatch.setattr(ag, "_load_signals", _fake_signals)
    monkeypatch.setattr(ag, "_load_alert_symbols", _fake_alerts)
    monkeypatch.setattr(ag, "_load_held_symbols", _fake_held)

    async def _run():
        from backend.shared.database_manager_v2 import close_database

        try:
            await close_database()
        except Exception:  # noqa: BLE001
            pass
        s1 = await ag.generate_once(today=test_day)
        # 第二轮：_load_recent_gen_symbols 读真实库 → 应命中第一轮的 gen 键（幂等）
        s2 = await ag.generate_once(today=test_day)
        return s1, s2

    import asyncio

    try:
        s1, s2 = asyncio.run(_run())
        assert s1["created"] == len(my_symbols), f"第一轮应建 {len(my_symbols)} 张: {s1}"
        assert s2["created"] == 0, f"第二轮应幂等 0 新建: {s2}"
        # 落库校验：动作/来源/依据
        with sync_session() as session:
            rows = session.execute(
                sql_text(
                    "SELECT symbol, source, actions, context_refs->>'gen' FROM ("
                    "  SELECT actions, source, context_refs, "
                    "         (a->>'symbol') AS symbol FROM copilot_advice, "
                    "         jsonb_array_elements(actions) a"
                    ") t WHERE source = :src AND context_refs::text LIKE :pat"
                ),
                {"src": ag.SOURCE, "pat": f"%adv-gen-{test_day.strftime('%Y%m%d')}%"},
            ).fetchall()
        assert len(rows) == len(my_symbols)
        for symbol, source, actions, gen in rows:
            assert symbol in my_symbols and source == ag.SOURCE
            assert gen and gen.startswith(f"adv-gen-{test_day.strftime('%Y%m%d')}-")
    finally:
        with sync_session() as session:
            session.execute(
                sql_text(
                    "DELETE FROM copilot_advice WHERE source = :src "
                    "AND context_refs->>'gen' LIKE :pat"
                ),
                {"src": ag.SOURCE, "pat": f"adv-gen-{test_day.strftime('%Y%m%d')}-%"},
            )
            session.commit()
