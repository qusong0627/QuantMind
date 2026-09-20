"""持仓预警判定口径的单测（纯函数层）。

这套规则决定「什么时候吵醒用户」。盯死四类边界：
由正转负只报一次、跌破阈值与转负不重复报、没有基线不报（首次见到负分不是「跌了」）、
停摆一周后不把陈年下跌报成「今天跌了」。
"""

from __future__ import annotations

from backend.shared.holding_alert_contract import (
    COOLDOWN_SECONDS,
    DEFAULT_CONFIG,
    KIND_RISK_NEWS,
    KIND_SCORE_BELOW_THRESHOLD,
    KIND_SCORE_CROSS_ZERO,
    MAX_BASELINE_GAP_DAYS,
    SEVERITY_CRITICAL,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    alert_action_url,
    baseline_is_comparable,
    build_alert_content,
    build_alert_title,
    cooldown_bucket,
    dedupe_alert_rows,
    evaluate_score_transition,
    format_score,
    make_holding_dedupe_key,
    meets_min_severity,
    notification_level,
    normalize_status,
    parse_alert_config,
)


class TestEvaluateScoreTransition:
    def test_cross_zero_is_critical(self):
        # 用户口径：由正转负 = 该走了
        assert evaluate_score_transition(0.12, -0.03) == (
            KIND_SCORE_CROSS_ZERO,
            SEVERITY_CRITICAL,
        )

    def test_exactly_zero_counts_as_crossed(self):
        # 0 已经是「没有买入理由」；停在 0 上不报会让持有等到负分才走
        assert evaluate_score_transition(0.12, 0.0) == (
            KIND_SCORE_CROSS_ZERO,
            SEVERITY_CRITICAL,
        )

    def test_staying_negative_does_not_realert(self):
        # 昨天 -0.2 今天 -0.3 是同一段下跌，重复报会把真信号淹掉
        assert evaluate_score_transition(-0.2, -0.3) is None

    def test_threshold_break_is_warning(self):
        assert evaluate_score_transition(0.5, 0.1, threshold=0.2) == (
            KIND_SCORE_BELOW_THRESHOLD,
            SEVERITY_WARNING,
        )

    def test_cross_zero_wins_over_threshold(self):
        # 两条规则同时成立时报更严重的那条（转负），不重复两条
        assert evaluate_score_transition(0.5, -0.1, threshold=0.2) == (
            KIND_SCORE_CROSS_ZERO,
            SEVERITY_CRITICAL,
        )

    def test_threshold_zero_disables_the_rule(self):
        assert evaluate_score_transition(0.5, 0.1, threshold=0.0) is None

    def test_rising_score_is_never_an_alert(self):
        assert evaluate_score_transition(-0.3, 0.4, threshold=0.2) is None

    def test_missing_side_is_not_an_alert(self):
        # 没有分数 ≠ 分数变差；在数据缺口上编故事比不报更糟
        assert evaluate_score_transition(None, -0.3) is None
        assert evaluate_score_transition(0.3, None) is None

    def test_first_seen_negative_is_not_an_alert(self):
        # 首次见到就是负分（哨兵刚开/新买入）——那叫「不该买」，不叫「跌了」
        assert evaluate_score_transition(None, -0.5) is None


class TestBaselineComparable:
    def test_same_day_and_adjacent_days_are_comparable(self):
        assert baseline_is_comparable("2026-09-19", "2026-09-20") is True
        assert baseline_is_comparable("2026-09-20", "2026-09-20") is True

    def test_gap_beyond_window_is_not_comparable(self):
        # 哨兵停摆一周后不该把陈年下跌报成「今天跌了」
        assert baseline_is_comparable("2026-09-01", "2026-09-20") is False
        assert (
            baseline_is_comparable("2026-09-01", "2026-09-07", max_gap_days=3) is False
        )

    def test_window_edge_is_inclusive(self):
        assert (
            baseline_is_comparable("2026-09-15", "2026-09-20", max_gap_days=5) is True
        )
        assert baseline_is_comparable("2026-09-14", "2026-09-20") is False
        assert MAX_BASELINE_GAP_DAYS == 5

    def test_unparsable_or_missing_dates_are_not_comparable(self):
        assert baseline_is_comparable(None, "2026-09-20") is False
        assert baseline_is_comparable("", "2026-09-20") is False
        assert baseline_is_comparable("not-a-date", "2026-09-20") is False

    def test_datetime_strings_are_truncated_to_date(self):
        assert baseline_is_comparable("2026-09-20T09:30:00", "2026-09-20") is True


class TestCooldownAndDedupe:
    def test_same_symbol_kind_within_cooldown_shares_a_key(self):
        t0 = 1_800_000_000.0
        k1 = make_holding_dedupe_key(
            tenant_id="default",
            user_id="10000001",
            symbol="SH600036",
            kind=KIND_SCORE_CROSS_ZERO,
            bucket=cooldown_bucket(t0),
        )
        k2 = make_holding_dedupe_key(
            tenant_id="default",
            user_id="10000001",
            symbol="SH600036",
            kind=KIND_SCORE_CROSS_ZERO,
            bucket=cooldown_bucket(t0 + COOLDOWN_SECONDS - 1),
        )
        assert k1 == k2

    def test_next_cooldown_window_allows_a_new_alert(self):
        t0 = 1_800_000_000.0
        k1 = make_holding_dedupe_key(
            tenant_id="default",
            user_id="10000001",
            symbol="SH600036",
            kind=KIND_SCORE_CROSS_ZERO,
            bucket=cooldown_bucket(t0),
        )
        k2 = make_holding_dedupe_key(
            tenant_id="default",
            user_id="10000001",
            symbol="SH600036",
            kind=KIND_SCORE_CROSS_ZERO,
            bucket=cooldown_bucket(t0 + COOLDOWN_SECONDS),
        )
        assert k1 != k2

    def test_key_separates_users_and_kinds(self):
        base = {"tenant_id": "default", "symbol": "SH600036", "bucket": 1}
        keys = {
            make_holding_dedupe_key(
                user_id="10000001", kind="score_cross_zero", **base
            ),
            make_holding_dedupe_key(user_id="1", kind="score_cross_zero", **base),
            make_holding_dedupe_key(user_id="10000001", kind="risk_news", **base),
            make_holding_dedupe_key(
                tenant_id="t2",
                user_id="10000001",
                kind="score_cross_zero",
                symbol="SH600036",
                bucket=1,
            ),
        }
        assert len(keys) == 4

    def test_dedupe_rows_keeps_more_severe(self):
        rows = [
            {
                "symbol": "SH600036",
                "kind": KIND_SCORE_CROSS_ZERO,
                "severity": SEVERITY_WARNING,
            },
            {
                "symbol": "SH600036",
                "kind": KIND_SCORE_CROSS_ZERO,
                "severity": SEVERITY_CRITICAL,
            },
        ]
        out = dedupe_alert_rows(rows)

        assert len(out) == 1
        assert out[0]["severity"] == SEVERITY_CRITICAL


class TestConfig:
    def test_defaults_are_used_when_nothing_saved(self):
        assert parse_alert_config(None) == DEFAULT_CONFIG
        assert parse_alert_config({}) == DEFAULT_CONFIG

    def test_partial_update_keeps_other_defaults(self):
        cfg = parse_alert_config({"score_threshold": 0.25})

        assert cfg["score_threshold"] == 0.25
        assert cfg["notify_desktop"] is True
        assert cfg["enabled"] is True

    def test_json_string_from_redis_is_accepted(self):
        cfg = parse_alert_config('{"enabled": false, "min_severity": "info"}')

        assert cfg["enabled"] is False
        assert cfg["min_severity"] == SEVERITY_INFO

    def test_broken_json_falls_back_to_defaults_not_crash(self):
        # Redis 里可能是半年前写的老结构/半截字符串；哨兵不能因此停摆
        assert parse_alert_config("{not json") == DEFAULT_CONFIG

    def test_bad_values_fall_back_per_field(self):
        cfg = parse_alert_config(
            {"score_threshold": "abc", "enabled": "maybe", "min_severity": "HUGE"}
        )

        assert cfg["score_threshold"] == 0.0
        assert cfg["enabled"] is True
        assert cfg["min_severity"] == SEVERITY_WARNING

    def test_threshold_is_clamped(self):
        assert parse_alert_config({"score_threshold": -3})["score_threshold"] == 0.0
        assert parse_alert_config({"score_threshold": 99})["score_threshold"] == 1.0

    def test_string_booleans_are_coerced(self):
        cfg = parse_alert_config({"notify_sound": "off", "notify_desktop": "1"})

        assert cfg["notify_sound"] is False
        assert cfg["notify_desktop"] is True


class TestSeverityAndText:
    def test_min_severity_gate(self):
        assert meets_min_severity(SEVERITY_CRITICAL, SEVERITY_WARNING) is True
        assert meets_min_severity(SEVERITY_WARNING, SEVERITY_WARNING) is True
        assert meets_min_severity(SEVERITY_INFO, SEVERITY_WARNING) is False

    def test_notification_level_mapping(self):
        assert notification_level(SEVERITY_CRITICAL) == "error"
        assert notification_level(SEVERITY_WARNING) == "warning"
        assert notification_level(SEVERITY_INFO) == "info"

    def test_title_says_what_happened(self):
        assert "由正转负" in build_alert_title(
            KIND_SCORE_CROSS_ZERO, "招商银行", "SH600036"
        )
        assert "招商银行" in build_alert_title(
            KIND_SCORE_CROSS_ZERO, "招商银行", "SH600036"
        )
        # 日频分要如实标注，别让用户以为这是盘中的分数
        assert "日频" in build_alert_title(
            KIND_SCORE_CROSS_ZERO, "招商银行", "SH600036", freq="daily"
        )
        assert "日频" not in build_alert_title(
            KIND_SCORE_CROSS_ZERO, "招商银行", "SH600036", freq="realtime"
        )

    def test_content_carries_both_numbers(self):
        text = build_alert_content(
            kind=KIND_SCORE_CROSS_ZERO,
            symbol="SH600036",
            score_prev=0.123,
            score_now=-0.045,
        )

        assert "+0.123" in text and "-0.045" in text

    def test_content_for_news_alert_keeps_the_reason(self):
        text = build_alert_content(
            kind=KIND_RISK_NEWS, symbol="SH600036", extra="证监会立案调查"
        )

        assert "证监会立案调查" in text

    def test_format_score_handles_missing(self):
        assert format_score(None) == "—"
        assert format_score(0.1234) == "+0.123"

    def test_action_url_deep_links_to_positions(self):
        # tab 值必须与前端页签 id 逐字一致（`position`，不是复数）——写错时前端
        # 只会静默回落「系统健康」，点了不报错也到不了持仓页。
        assert alert_action_url("SH600036") == "/trading?tab=position&symbol=SH600036"
        assert alert_action_url(None) == "/trading?tab=position"

    def test_status_normalization(self):
        assert normalize_status("EXECUTED") == "executed"
        assert normalize_status("nonsense") == "active"
        assert normalize_status(None) == "active"
