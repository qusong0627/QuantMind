"""持仓行情真实供数源审计（quote_source_audit）单元测试。

覆盖：按源聚合（覆盖数/新近度/时钟偏斜/缺失）、键候选归一、Redis 采样（小写键优先、
大写键回退、去重、限流、故障如实标注）、以及 /tdx/quote-feed/status 的 quote_sources 段接线。
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

import pytest

from backend.shared.freshness import FRESH, STALE, UNAVAILABLE, FreshnessPolicy
from backend.services.live_trading.services.quote_source_audit import (
    QUOTE_SOURCE_LABELS,
    UNKNOWN_SOURCE,
    aggregate_quote_sources,
    collect_quote_sources,
    quote_source_label,
    snapshot_key_candidates,
)

NOW = 1_800_000_000.0
POLICY = FreshnessPolicy(fresh_within_s=60.0, stale_within_s=300.0)


def _row(source: str, age_sec: float) -> dict[str, str]:
    """构造一条快照行：timestamp 为 epoch 秒字符串（写侧契约）。"""
    return {"source": source, "timestamp": str(NOW - age_sec)}


class TestAggregateQuoteSources:
    def test_dominant_is_source_covering_most_symbols(self):
        # Arrange：桥覆盖 3 只、备源 1 只（备源更新，但覆盖少不算主源）
        rows = [_row("tdx_bridge", 3), _row("tdx_bridge", 5), _row("tdx_bridge", 9), _row("qmt_big", 1)]

        # Act
        result = aggregate_quote_sources(rows, now_ts=NOW, policy=POLICY)

        # Assert
        assert result["dominant"] == "tdx_bridge"
        assert result["dominant_label"] == "通达信桥"
        assert result["level"] == FRESH
        assert [s["source"] for s in result["sources"]] == ["tdx_bridge", "qmt_big"]
        assert result["sources"][0]["count"] == 3
        assert result["sources"][0]["newest_age_sec"] == 3.0

    def test_tie_on_coverage_prefers_newer_source(self):
        rows = [_row("tdx_bridge", 45), _row("tdx_bridge", 50), _row("qmt_big", 2), _row("qmt_big", 4)]

        result = aggregate_quote_sources(rows, now_ts=NOW, policy=POLICY)

        assert result["dominant"] == "qmt_big"
        assert result["newest_age_sec"] == 2.0

    def test_missing_symbols_counted_and_stale_level_kept(self):
        rows = [_row("tdx_bridge", 120), {}, None]

        result = aggregate_quote_sources(rows, now_ts=NOW, policy=POLICY)

        assert result["missing"] == 2
        assert result["requested"] == 3
        assert result["level"] == STALE

    def test_no_rows_is_unavailable_not_fresh(self):
        result = aggregate_quote_sources([], now_ts=NOW, policy=POLICY, requested=7)

        assert result["dominant"] is None
        assert result["dominant_label"] is None
        assert result["level"] == UNAVAILABLE
        assert result["sources"] == []
        assert result["requested"] == 7
        assert result["missing"] == 0
        assert result["error"] is None

    def test_row_without_source_lands_in_unknown_bucket(self):
        result = aggregate_quote_sources([_row("", 3)], now_ts=NOW, policy=POLICY)

        assert result["dominant"] == UNKNOWN_SOURCE
        assert result["dominant_label"] == UNKNOWN_SOURCE

    def test_source_without_timestamp_is_unavailable(self):
        result = aggregate_quote_sources([{"source": "qmt_big"}], now_ts=NOW, policy=POLICY)

        assert result["sources"][0]["level"] == UNAVAILABLE
        assert result["sources"][0]["newest_age_sec"] is None

    def test_clock_skew_beyond_tolerance_is_not_fresh(self):
        # 时间戳比本机 now 晚 60s → 跨机时钟异常；不得被 max(0,·) 洗成 fresh
        result = aggregate_quote_sources([_row("qmt_big", -60)], now_ts=NOW, policy=POLICY)

        assert result["level"] == UNAVAILABLE
        assert result["sources"][0]["newest_age_sec"] == -60.0

    def test_small_negative_age_within_skew_tolerance_is_fresh(self):
        result = aggregate_quote_sources([_row("qmt_big", -1)], now_ts=NOW, policy=POLICY)

        assert result["level"] == FRESH


class TestQuoteSourceLabel:
    def test_known_sources_have_chinese_labels(self):
        assert quote_source_label("tdx_bridge") == "通达信桥"
        assert quote_source_label("tdx_aidata_sub") == "TDX 订阅"
        assert quote_source_label("qmt_big") == "QMT 备源"
        assert quote_source_label("qmt_exec") == "QMT 执行端"

    def test_unknown_source_passes_through_not_fabricated(self):
        assert set(QUOTE_SOURCE_LABELS) == {"tdx_bridge", "tdx_aidata_sub", "qmt_big", "qmt_exec"}
        assert quote_source_label("some_new_feed") == "some_new_feed"
        assert quote_source_label(None) == "未知来源"


class TestSnapshotKeyCandidates:
    @pytest.mark.parametrize("symbol", ["600036.SH", "SH600036", "sh600036", "600036"])
    def test_all_a_share_code_shapes_map_to_same_keys(self, symbol):
        assert snapshot_key_candidates(symbol) == [
            "market:snapshot:sh600036",
            "market:snapshot:SH600036",
        ]

    @pytest.mark.parametrize("symbol", ["00700.HK", "BABA", "600036.US", "", "   ", "sh600036.SH"])
    def test_non_a_share_or_malformed_codes_are_not_guessed(self, symbol):
        assert snapshot_key_candidates(symbol) == []

    def test_sz_and_bj_prefixes_supported(self):
        assert snapshot_key_candidates("000001")[0] == "market:snapshot:sz000001"
        assert snapshot_key_candidates("430047.BJ")[0] == "market:snapshot:bj430047"


class _FakePipeline:
    def __init__(self, store: dict):
        self._store = store
        self.issued_keys: list[str] = []

    def __enter__(self) -> "_FakePipeline":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def hgetall(self, key: str) -> "_FakePipeline":
        self.issued_keys.append(key)
        return self

    def execute(self) -> list[dict]:
        return [dict(self._store.get(key) or {}) for key in self.issued_keys]


class _FakeRedis:
    def __init__(self, store: dict | None = None):
        self.store = store or {}
        self.pipes: list[_FakePipeline] = []

    def pipeline(self, transaction: bool = False) -> _FakePipeline:
        pipe = _FakePipeline(self.store)
        self.pipes.append(pipe)
        return pipe


class TestCollectQuoteSources:
    def test_lowercase_key_wins_when_both_present(self):
        now = time.time()
        client = _FakeRedis(
            {
                "market:snapshot:sh600036": {"source": "tdx_bridge", "timestamp": str(now - 5)},
                "market:snapshot:SH600036": {"source": "qmt_big", "timestamp": str(now - 1)},
            }
        )

        result = collect_quote_sources(["600036.SH"], client_getter=lambda: client)

        assert result["dominant"] == "tdx_bridge"
        assert result["level"] == FRESH
        # 小写键命中即止：大写候选不参与聚合（与 stream 读侧同序）
        assert [s["source"] for s in result["sources"]] == ["tdx_bridge"]

    def test_falls_back_to_uppercase_key(self):
        now = time.time()
        client = _FakeRedis(
            {"market:snapshot:SH600000": {"source": "qmt_big", "timestamp": str(now - 3)}}
        )

        result = collect_quote_sources(["600000.SH"], client_getter=lambda: client)

        assert result["dominant"] == "qmt_big"
        assert result["missing"] == 0

    def test_missing_symbols_reported_and_redis_untouched_when_none_match(self):
        client = _FakeRedis({})

        result = collect_quote_sources(["600036.SH", "000001.SZ"], client_getter=lambda: client)

        assert result["requested"] == 2
        assert result["missing"] == 2
        assert result["dominant"] is None
        assert result["level"] == UNAVAILABLE

    def test_dedupes_symbols_and_caps_at_limit(self):
        client = _FakeRedis({})

        result = collect_quote_sources(
            ["600036.SH", "SH600036", "600036", "600000.SH", "600001.SH"],
            limit=2,
            client_getter=lambda: client,
        )

        assert result["requested"] == 2
        assert len(client.pipes) == 1
        # 2 只 × 2 个候选键 = 4 次 hgetall（同一 pipeline 一次往返）
        assert len(client.pipes[0].issued_keys) == 4

    def test_redis_failure_is_reported_not_silently_empty(self):
        def _boom():
            raise ConnectionError("redis down")

        result = collect_quote_sources(["600036.SH"], client_getter=_boom)

        assert result["dominant"] is None
        assert result["level"] == UNAVAILABLE
        assert result["sources"] == []
        assert result["error"] is not None
        assert result["error"].startswith("quote_redis_unavailable")
        assert "redis down" in result["error"]

    def test_empty_symbol_list_does_not_touch_redis(self):
        def _explode():
            raise AssertionError("空标的列表不应访问 Redis")

        result = collect_quote_sources([], client_getter=_explode)

        assert result["requested"] == 0
        assert result["error"] is None

    def test_unparsable_symbols_do_not_touch_redis(self):
        def _explode():
            raise AssertionError("无可采样 A 股代码时不应访问 Redis")

        result = collect_quote_sources(["00700.HK", "BABA"], client_getter=_explode)

        assert result["requested"] == 2
        assert result["sources"] == []
        assert result["error"] is None


def _fake_collect(captured: dict):
    def _collect(symbols, *, limit):
        captured["symbols"] = list(symbols)
        captured["limit"] = limit
        return {
            "requested": len(symbols),
            "missing": 0,
            "dominant": "tdx_bridge",
            "dominant_label": "通达信桥",
            "level": FRESH,
            "newest_age_sec": 2.0,
            "sources": [],
            "as_of": 0.0,
            "error": None,
        }

    return _collect


class TestQuoteFeedStatusQuoteSources:
    def _auth(self):
        from backend.services.trade_shared.deps import AuthContext

        return AuthContext(user_id="10000001", tenant_id="default", raw_sub="u", roles=["user"])

    def test_samples_holdings_when_symbols_argument_absent(self):
        from backend.services.live_trading.services.tdx_quote_feed import feed_status
        from backend.services.trade.routers.tdx_quote_feed import get_quote_feed_status

        captured: dict = {}
        with patch(
            "backend.services.live_trading.services.quote_source_audit.collect_quote_sources",
            _fake_collect(captured),
        ), patch.dict(feed_status, {"symbols": ["SH600036", "SZ000001"]}):
            status = asyncio.run(get_quote_feed_status(symbols=None, auth=self._auth()))

        assert captured["symbols"] == ["SH600036", "SZ000001"]
        assert captured["limit"] == 300
        assert status["quote_sources"]["dominant"] == "tdx_bridge"
        # 原状态字段仍在（新增段不得覆盖持仓馈送/hot_set）
        assert "hot_set" in status and "is_trading_time" in status

    def test_explicit_symbols_override_holdings(self):
        from backend.services.live_trading.services.tdx_quote_feed import feed_status
        from backend.services.trade.routers.tdx_quote_feed import get_quote_feed_status

        captured: dict = {}
        with patch(
            "backend.services.live_trading.services.quote_source_audit.collect_quote_sources",
            _fake_collect(captured),
        ), patch.dict(feed_status, {"symbols": ["SH600036"]}):
            asyncio.run(get_quote_feed_status(symbols="600036.SH, 000001", auth=self._auth()))

        assert captured["symbols"] == ["600036.SH", "000001"]


class TestParseSymbolsArg:
    def test_handles_fullwidth_comma_and_trims(self):
        from backend.services.trade.routers.tdx_quote_feed import _parse_symbols_arg

        assert _parse_symbols_arg("600036.SH，000001") == ["600036.SH", "000001"]
        assert _parse_symbols_arg(" 600036.SH , 000001 ") == ["600036.SH", "000001"]

    def test_blank_input_returns_empty(self):
        from backend.services.trade.routers.tdx_quote_feed import _parse_symbols_arg

        assert _parse_symbols_arg(None) == []
        assert _parse_symbols_arg("") == []
        assert _parse_symbols_arg(" , ，") == []
