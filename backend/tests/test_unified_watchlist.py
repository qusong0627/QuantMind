"""自选池统一视图的单测（纯函数层）。

覆盖 `merge_sources` 的并集语义：同票多来源合并成一行、排序优先级、
候选截断不动持仓与手工自选、counts 如实给出全量与展示量。
取数层（httpx/PG）不在这里测——那是集成测试的事，这里盯的是口径。
"""

from __future__ import annotations

import pytest

from backend.services.api.unified_watchlist import (
    _norm_symbol,
    _position_payload,
    _snapshot_source_for_broker,
    merge_sources,
)


def _cand(symbol: str, score: float) -> dict:
    return {
        "symbol": symbol,
        "score": score,
        "side": "BUY",
        "freq": "daily",
        "asOf": "2026-09-21",
    }


class TestNormSymbol:
    def test_normalizes_suffix_to_prefix(self):
        assert _norm_symbol("600036.SH") == "SH600036"

    def test_strips_margin_side_marker(self):
        # 两融持仓键形 SH600036::long —— 同一只票不能因为侧标变成两行
        assert _norm_symbol("SH600036::long") == "SH600036"

    def test_rejects_non_a_share(self):
        assert _norm_symbol("AAPL") is None
        assert _norm_symbol("") is None
        assert _norm_symbol(None) is None


class TestPositionPayload:
    def test_missing_values_stay_none_instead_of_zero(self):
        # 诚实纪律：缺值不能填 0（0 手会被前端渲染成「已清仓」）
        payload = _position_payload({"volume": 700})
        assert payload["volume"] == 700
        assert payload["availableVolume"] is None
        assert payload["cost"] is None
        assert payload["marketValue"] is None

    def test_accepts_sim_cost_key_and_real_cost_price_key(self):
        sim = _position_payload({"cost": 24.85, "price": 28.26})
        real = _position_payload({"cost_price": 13.59, "last_price": 13.15})
        assert sim["cost"] == pytest.approx(24.85)
        assert sim["price"] == pytest.approx(28.26)
        assert real["cost"] == pytest.approx(13.59)
        assert real["price"] == pytest.approx(13.15)


class TestMergeSources:
    def test_same_symbol_from_four_sources_merges_into_one_row(self):
        # Arrange：同一只票同时是手工自选、模拟持仓、实盘持仓、正分候选
        items, counts = merge_sources(
            manual={"SH600036": {"stockName": "招商银行", "addedAt": "2026-09-01"}},
            sim={"SH600036": {"volume": 700, "available_volume": 700, "cost": 24.85}},
            real={"SH600036": {"volume": 1000, "cost_price": 30.0, "name": "招商银行"}},
            candidates=[_cand("SH600036", 0.5)],
        )
        # Assert
        assert len(items) == 1
        row = items[0]
        assert row["sources"] == [
            "manual",
            "position_sim",
            "position_real",
            "candidate",
        ]
        assert row["position"]["sim"]["volume"] == 700
        assert row["position"]["real"]["volume"] == 1000
        assert row["score"]["value"] == pytest.approx(0.5)
        assert counts["total"] == 1

    def test_positions_come_first_sorted_by_market_value_desc(self):
        items, _ = merge_sources(
            manual={"SH600000": {"stockName": "浦发银行"}},
            sim={
                "SH600036": {"volume": 100, "market_value": 1000.0},
                "SZ000001": {"volume": 100, "market_value": 9000.0},
            },
            real={},
            candidates=[_cand("SH601988", 0.99)],
        )
        assert [it["symbol"] for it in items] == [
            "SZ000001",
            "SH600036",
            "SH600000",
            "SH601988",
        ]

    def test_candidates_sorted_by_score_desc_after_priority_rows(self):
        items, _ = merge_sources(
            manual={},
            sim={},
            real={},
            candidates=[
                _cand("SH600001", 0.2),
                _cand("SH600002", 0.8),
                _cand("SH600003", 0.5),
            ],
        )
        assert [it["symbol"] for it in items] == ["SH600002", "SH600003", "SH600001"]

    def test_candidate_cap_drops_candidates_but_never_positions_or_manual(self):
        # Arrange：200 只候选 + 1 只持仓 + 1 只手工自选，cap 只留 2
        sim = {"SH600036": {"volume": 100, "market_value": 5000.0}}
        manual = {"SH600000": {"stockName": "浦发银行"}}
        candidates = [
            _cand(f"SH{601000 + i:06d}", 1.0 - i / 1000) for i in range(200)
        ]
        # Act
        items, counts = merge_sources(
            manual=manual, sim=sim, real={}, candidates=candidates, candidate_cap=2
        )
        # Assert：持仓与手工自选不参与截断
        syms = [it["symbol"] for it in items]
        assert "SH600036" in syms
        assert "SH600000" in syms
        assert counts["candidate_shown"] == 2
        assert counts["candidate_total"] == 200
        assert counts["total"] == 202
        assert counts["shown"] == 4

    def test_limit_budget_only_trims_candidates(self):
        sim = {"SH600036": {"volume": 100, "market_value": 5000.0}}
        manual = {"SH600000": {"stockName": "浦发银行"}}
        candidates = [
            _cand(f"SH{601000 + i:06d}", 1.0 - i / 1000) for i in range(50)
        ]
        items, counts = merge_sources(
            manual=manual,
            sim=sim,
            real={},
            candidates=candidates,
            candidate_cap=50,
            limit=12,
        )
        # limit=12，优先级行 2 → 候选最多 10；持仓/手工仍在
        assert counts["shown"] == 12
        assert counts["total"] == 52
        syms = [it["symbol"] for it in items]
        assert syms[:2] == ["SH600036", "SH600000"]

    def test_candidate_channel_marks_score_freq_realtime_when_realtime(self):
        items, _ = merge_sources(
            manual={},
            sim={},
            real={},
            candidates=[
                {
                    "symbol": "SH600036",
                    "score": 0.3,
                    "side": "BUY",
                    "freq": "realtime",
                    "asOf": "2026-09-21",
                }
            ],
        )
        assert items[0]["score"]["freq"] == "realtime"

    def test_manual_row_keeps_name_when_positions_have_none(self):
        items, _ = merge_sources(
            manual={"SH600036": {"stockName": "招商银行", "addedAt": "2026-09-01"}},
            sim={"SH600036": {"volume": 700}},
            real={},
            candidates=[],
        )
        assert items[0]["stockName"] == "招商银行"
        assert items[0]["addedAt"] == "2026-09-01"

    def test_negative_or_zero_candidates_never_reach_merge(self):
        # 正分筛选是取数层（load_signal_scores）的职责；这里固定「传进来就是正分」的契约
        items, counts = merge_sources(manual={}, sim={}, real={}, candidates=[])
        assert items == []
        assert counts["candidate_total"] == 0

    def test_real_position_payload_carries_broker_source(self):
        # Phase 4 一键卖出要按出处路由券商，source 必须穿透到载荷
        payload = _position_payload(
            {"volume": 800, "source": "qmt_exec", "sources": ["qmt_exec", "tdx_bridge"]}
        )
        assert payload["source"] == "qmt_exec"
        assert payload["sources"] == ["qmt_exec", "tdx_bridge"]


class TestSnapshotSourceForBroker:
    def test_maps_tdx_and_qmt_variants(self):
        assert _snapshot_source_for_broker("tdx") == "tdx_bridge"
        assert _snapshot_source_for_broker("tdx_bridge") == "tdx_bridge"
        assert _snapshot_source_for_broker("qmt_exec") == "qmt_exec"
        assert _snapshot_source_for_broker("qmt") == "qmt_exec"

    def test_unknown_broker_has_no_snapshot_source(self):
        # tiger/futu/ib 没有落 real_account_snapshots，不能误映射到别人的快照
        assert _snapshot_source_for_broker("tiger") is None
        assert _snapshot_source_for_broker("") is None
        assert _snapshot_source_for_broker(None) is None
