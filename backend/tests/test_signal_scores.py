"""信号分快照口径的单测（**唯一实现**共享模块）。

口径漂移的代价：自选页显示 +0.12、哨兵按 -0.03 报警 —— 用户没法信任任何一边。
所以这里盯死三件事：键形归一（裸 6 位 ↔ prefix）、实时/日频如实标注、
分数为 NULL 时**保留 None**（「没有分数」和「分数是 0」不是一回事）。
"""

from __future__ import annotations

from backend.shared.signal_scores import (
    MIN_SIGNAL_COVERAGE,
    SQL_LATEST_ANY_DATE,
    SQL_LATEST_COVERED_DATE,
    SQL_SCORES_BY_DATE,
    build_score_map,
    normalize_a_share_symbol,
)


class TestNormalizeAShareSymbol:
    def test_bare_six_digits_gain_market_prefix(self):
        # engine_signal_scores.symbol 是裸 6 位；持仓/自选侧是 prefix
        assert normalize_a_share_symbol("600036") == "SH600036"
        assert normalize_a_share_symbol("000001") == "SZ000001"
        assert normalize_a_share_symbol("300750") == "SZ300750"
        assert normalize_a_share_symbol("430047") == "BJ430047"

    def test_suffix_form_converts_to_prefix(self):
        assert normalize_a_share_symbol("600036.SH") == "SH600036"

    def test_lowercase_prefix_upper_cases(self):
        assert normalize_a_share_symbol("sh600036") == "SH600036"

    def test_prefix_passes_through(self):
        assert normalize_a_share_symbol("SH600036") == "SH600036"

    def test_non_a_share_is_none(self):
        # 港股/美股/空值一律 None —— 调用方按「这行不参与」处理，不猜市场
        assert normalize_a_share_symbol("AAPL") is None
        assert normalize_a_share_symbol("00700.HK") is None
        assert normalize_a_share_symbol("") is None
        assert normalize_a_share_symbol(None) is None


class TestBuildScoreMap:
    def test_maps_rows_to_prefix_keyed_entries(self):
        rows = [("600036", 0.31, "BUY", "batch", "2026-09-19")]
        score_map, realtime_rows = build_score_map(rows, "2026-09-19")

        assert score_map == {
            "SH600036": {
                "value": 0.31,
                "side": "BUY",
                "freq": "daily",
                "asOf": "2026-09-19",
            }
        }
        assert realtime_rows == 0

    def test_realtime_source_is_flagged_and_counted(self):
        # source='realtime' 是盘中热集推理落库的行 —— 前端据此标「实时」，不然
        # 用户会把盘中分当昨天日频分看
        rows = [
            ("600036", 0.4, "BUY", "realtime", "2026-09-20"),
            ("000001", 0.1, "HOLD", "batch", "2026-09-20"),
        ]
        score_map, realtime_rows = build_score_map(rows, "2026-09-20")

        assert score_map["SH600036"]["freq"] == "realtime"
        assert score_map["SZ000001"]["freq"] == "daily"
        assert realtime_rows == 1

    def test_null_score_is_kept_as_none_not_zero(self):
        rows = [("600036", None, None, "batch", None)]
        score_map, _ = build_score_map(rows, "2026-09-19")

        assert score_map["SH600036"]["value"] is None
        assert score_map["SH600036"]["side"] is None

    def test_as_of_falls_back_to_trade_date_when_row_lacks_it(self):
        # 老调用方（不需要 asOf 的查询）只取 4 列，索引越界不能炸
        rows = [("600036", 0.2, "BUY", "batch")]
        score_map, _ = build_score_map(rows, "2026-09-19")

        assert score_map["SH600036"]["asOf"] == "2026-09-19"

    def test_non_a_share_and_blank_rows_are_skipped(self):
        rows = [
            ("00700.HK", 0.9, "BUY", "batch", "2026-09-19"),
            ("", 0.5, "BUY", "batch", "2026-09-19"),
        ]
        score_map, realtime_rows = build_score_map(rows, "2026-09-19")

        assert score_map == {}
        assert realtime_rows == 0

    def test_last_row_wins_for_duplicate_symbol(self):
        # SQL 侧 DISTINCT ON 已保证每标的仅一行；万一有人改成全量取，
        # 后出现（created_at 更新）的那条必须覆盖前一条，而不是被前一条挡住
        rows = [
            ("600036", 0.1, "BUY", "batch", "2026-09-19"),
            ("600036", -0.2, "SELL", "realtime", "2026-09-19"),
        ]
        score_map, realtime_rows = build_score_map(rows, "2026-09-19")

        assert score_map["SH600036"]["value"] == -0.2
        assert score_map["SH600036"]["side"] == "SELL"
        assert realtime_rows == 1


class TestSqlConstants:
    def test_market_predicate_tolerates_null_market(self):
        # 老库 market 列未回填；裸 `market = 'CN'` 会把 CN 行整片查空（静默）
        for sql in (SQL_LATEST_COVERED_DATE, SQL_LATEST_ANY_DATE, SQL_SCORES_BY_DATE):
            assert "market IS NULL OR market = 'CN'" in sql

    def test_coverage_threshold_is_bound_not_inlined(self):
        assert ":min_cov" in SQL_LATEST_COVERED_DATE
        assert MIN_SIGNAL_COVERAGE == 1000

    def test_scores_query_picks_latest_row_per_symbol(self):
        assert "DISTINCT ON (symbol)" in SQL_SCORES_BY_DATE
        assert "created_at DESC" in SQL_SCORES_BY_DATE
