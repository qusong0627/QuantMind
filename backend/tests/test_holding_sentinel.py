"""持仓哨兵的单测：监控集装配、市场告警分类、分数迁移扫描。

盯死三类**静默错误**（不报错但会让用户按错信息操作）：

1. 把已清仓的残影（``volume=0``）当持仓 → 对着空仓发提醒；
2. 把账户级/模型级/数据级告警当利空 → 教用户忽略提醒；
3. 首次见到非正分就报「下穿 0」→ 开哨兵当天全部持仓一起响。

第 3 条在契约层已有单测；这里补的是**扫描器真的按契约接线**（基线播种、跨线只报
一次、基线过期不报）。
"""

from __future__ import annotations

import json

import pytest

from backend.services.trade.services.holding_sentinel import (
    HoldingSentinel,
    build_monitor_set,
    classify_market_alert,
    new_symbols,
    positions_from_payload,
    target_symbols,
)
from backend.shared.holding_alert_contract import (
    BASELINE_KEY_PREFIX,
    KIND_RISK_ANOMALY,
    KIND_RISK_NEWS,
    KIND_SCORE_CROSS_ZERO,
    KIND_SCORE_DROP,
    SEVERITY_CRITICAL,
    SEVERITY_WARNING,
)


class FakeRedis:
    """够用的 Redis 替身（hash + string + scan_iter）。"""

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.strings: dict[str, str] = {}
        self.expires: list[str] = []

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hset(self, key, mapping=None, **kwargs):
        bucket = self.hashes.setdefault(key, {})
        bucket.update({str(k): str(v) for k, v in (mapping or {}).items()})
        return len(mapping or {})

    def hdel(self, key, *fields):
        bucket = self.hashes.get(key, {})
        for field in fields:
            bucket.pop(field, None)
        return len(fields)

    def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    def get(self, key):
        return self.strings.get(key)

    def set(self, key, value, ex=None):
        self.strings[key] = str(value)
        return True

    def expire(self, key, seconds):
        self.expires.append(key)
        return True

    def scan_iter(self, match=None, count=None):
        prefix = (match or "").rstrip("*")
        return [
            k for k in list(self.strings) + list(self.hashes) if k.startswith(prefix)
        ]


class TestPositionsFromPayload:
    def test_zero_and_negative_volume_rows_are_dropped(self):
        # 清仓残影：库留着它对账，不代表还持有
        payload = json.dumps(
            {
                "positions": {
                    "SH600036": {"symbol": "SH600036", "volume": 0},
                    "SZ000001": {"symbol": "SZ000001", "volume": -5},
                    "SH601318": {"symbol": "SH601318", "volume": 300},
                }
            }
        )
        out = positions_from_payload(payload)

        assert set(out) == {"SH601318"}

    def test_key_is_used_when_row_has_no_symbol(self):
        payload = {"positions": {"SH600036": {"volume": 100}}}
        out = positions_from_payload(payload)

        assert set(out) == {"SH600036"}

    def test_broken_payload_is_empty_not_crash(self):
        assert positions_from_payload("{not json") == {}
        assert positions_from_payload(None) == {}
        assert positions_from_payload({"positions": []}) == {}

    def test_non_a_share_rows_are_dropped(self):
        payload = {"positions": {"AAPL": {"volume": 10, "symbol": "AAPL"}}}
        assert positions_from_payload(payload) == {}

    def test_side_suffix_is_stripped(self):
        # 两融持仓键带 ::long/::short；不切掉同一只票会变成两行
        payload = {"positions": {"SH600036::long": {"volume": 100}}}
        assert set(positions_from_payload(payload)) == {"SH600036"}


class TestBuildMonitorSet:
    def test_sources_union_and_names(self):
        monitor = build_monitor_set(
            sim={"SH600036": {"volume": 100, "name": "招商银行"}},
            real={"SH600036": {"volume": 500, "name": "招商银行"}},
            manual={"SZ000001": {"stockName": "平安银行"}},
            name_resolver=lambda s: "兜底名",
        )

        assert set(monitor) == {"SH600036", "SZ000001"}
        assert monitor["SH600036"]["sources"] == ["sim", "real"]
        # 实盘腿优先（真金白银的那一份）
        assert monitor["SH600036"]["position"]["volume"] == 500
        assert monitor["SZ000001"]["sources"] == ["manual"]
        assert monitor["SZ000001"]["stockName"] == "平安银行"

    def test_missing_name_falls_back_to_resolver(self):
        monitor = build_monitor_set(
            sim={"SH600036": {"volume": 1}}, name_resolver=str.lower
        )

        assert monitor["SH600036"]["stockName"] == "sh600036"

    def test_empty_inputs_give_empty_set(self):
        assert build_monitor_set() == {}


class TestClassifyMarketAlert:
    def test_down_news_is_a_risk_alert(self):
        assert classify_market_alert("news:negative", "down", "warn") == (
            KIND_RISK_NEWS,
            SEVERITY_WARNING,
        )
        assert classify_market_alert("news:risk_event", "down", "critical") == (
            KIND_RISK_NEWS,
            SEVERITY_CRITICAL,
        )

    def test_upward_or_neutral_never_alerts(self):
        # 利好不是「该卖」的理由；regime/sentiment 同理
        assert classify_market_alert("news:positive", "up", "info") is None
        assert classify_market_alert("regime", "none", "info") is None
        assert classify_market_alert("news:sentiment_spike", "none", "warn") is None

    def test_account_model_and_data_level_alerts_never_alert(self):
        # 撤单率/IC 掉/日线跳变是「系统有事」，不是「该卖股票」
        assert (
            classify_market_alert("anomaly:account_cancel_ratio", "down", "critical")
            is None
        )
        assert (
            classify_market_alert("anomaly:model_ic_drop", "down", "critical") is None
        )
        assert classify_market_alert("anomaly:data_jump", "down", "critical") is None
        assert classify_market_alert("anomaly:volume_surge", "none", "critical") is None

    def test_price_move_is_an_anomaly_alert(self):
        assert classify_market_alert("anomaly:price_drop", "down", "warn") == (
            KIND_RISK_ANOMALY,
            SEVERITY_WARNING,
        )

    def test_severity_maps_up_but_never_down(self):
        # info 严重度 + down 方向（price_surge 判成 down）仍按类型兜底报，不静默丢掉
        assert classify_market_alert("anomaly:price_surge", "down", "info") == (
            KIND_RISK_ANOMALY,
            SEVERITY_WARNING,
        )
        assert classify_market_alert("news:negative", "down", "critical") == (
            KIND_RISK_NEWS,
            SEVERITY_CRITICAL,
        )

    def test_unknown_type_is_ignored(self):
        assert classify_market_alert("brand:new_thing", "down", "critical") is None
        assert classify_market_alert("", "down", "critical") is None


class TestTargetSymbols:
    def test_market_wide_row_falls_back_to_targets(self):
        # 实测 symbol='*' 的行靠 targets 才能定位到票
        assert target_symbols("*", ["600036.SH", "000001.SZ"]) == [
            "SH600036",
            "SZ000001",
        ]

    def test_symbol_is_normalized_and_deduped(self):
        assert target_symbols("600036.SH", ["600036.SH", "600036"]) == ["SH600036"]

    def test_non_stock_targets_are_dropped(self):
        assert target_symbols("*", ["9000ba63", "itest-6f79fd"]) == []

    def test_json_string_targets_are_parsed(self):
        assert target_symbols("*", '["600036.SH"]') == ["SH600036"]

    def test_target_count_is_capped(self):
        many = [f"{600000 + i}.SH" for i in range(80)]
        assert len(target_symbols("*", many)) == 50


class TestNewSymbols:
    def test_first_run_reports_nothing(self):
        # 首轮没有基线：把名单上所有持仓报一遍就是告警风暴
        assert new_symbols(set(), {"600036.SH", "000001.SZ"}) == set()

    def test_only_additions_are_reported(self):
        assert new_symbols({"600036.SH"}, {"600036.SH", "000001.SZ"}) == {"000001.SZ"}

    def test_removals_are_not_reported(self):
        assert new_symbols({"600036.SH", "000001.SZ"}, {"600036.SH"}) == set()


class TestScoreScan:
    """`_scan_scores` 与契约的接线：基线播种 → 跨线报一次 → 不重复。"""

    def _sentinel(
        self, scores_by_call: list[dict], redis: FakeRedis
    ) -> HoldingSentinel:
        calls = {"n": 0}

        async def _loader(tenant: str):
            idx = min(calls["n"], len(scores_by_call) - 1)
            calls["n"] += 1
            return scores_by_call[idx], {"signal_date": "2026-09-20", "ok": True}

        return HoldingSentinel(redis=redis, score_loader=_loader)

    @staticmethod
    def _watch():
        return {"SH600036": {"stockName": "招商银行", "sources": ["real"]}}

    @staticmethod
    def _scores(value: float, *, as_of: str = "2026-09-20", freq: str = "daily"):
        return {
            "SH600036": {
                "value": value,
                "side": "BUY" if value > 0 else "SELL",
                "freq": freq,
                "asOf": as_of,
            }
        }

    @pytest.mark.asyncio
    async def test_first_scan_only_seeds_the_baseline(self):
        redis = FakeRedis()
        sentinel = self._sentinel([self._scores(-0.4)], redis)

        alerts = await sentinel._scan_scores(
            redis,
            {"tenant_id": "default", "user_id": "10000001"},
            self._watch(),
            {},
            1e9,
        )

        assert alerts == []
        stored = redis.hashes[f"{BASELINE_KEY_PREFIX}default:10000001"]
        record = json.loads(stored["SH600036"])
        assert record["v"] == -0.4
        # 首见即设当日基准（anchor 只在同日有效，跨日首扫要重设）
        assert record["a"] == -0.4
        assert record["ad"] == "2001-09-09"  # now_ts=1e9 的上海墙钟日

    @pytest.mark.asyncio
    async def test_crossing_zero_alerts_once(self):
        redis = FakeRedis()
        sentinel = self._sentinel([self._scores(0.12), self._scores(-0.03)], redis)
        user = {"tenant_id": "default", "user_id": "10000001"}

        await sentinel._scan_scores(redis, user, self._watch(), {}, 1e9)
        alerts = await sentinel._scan_scores(redis, user, self._watch(), {}, 1e9 + 60)

        assert len(alerts) == 1
        assert alerts[0]["kind"] == KIND_SCORE_CROSS_ZERO
        assert alerts[0]["severity"] == SEVERITY_CRITICAL
        assert alerts[0]["symbol"] == "SH600036"
        assert alerts[0]["stock_name"] == "招商银行"
        assert alerts[0]["score_prev"] == 0.12
        assert alerts[0]["score_now"] == -0.03

    @pytest.mark.asyncio
    async def test_staying_negative_does_not_realert(self):
        redis = FakeRedis()
        sentinel = self._sentinel(
            [self._scores(0.12), self._scores(-0.03), self._scores(-0.8)], redis
        )
        user = {"tenant_id": "default", "user_id": "10000001"}

        await sentinel._scan_scores(redis, user, self._watch(), {}, 1e9)
        await sentinel._scan_scores(redis, user, self._watch(), {}, 1e9 + 60)
        alerts = await sentinel._scan_scores(redis, user, self._watch(), {}, 1e9 + 120)

        assert alerts == []

    @pytest.mark.asyncio
    async def test_threshold_rule_uses_user_config(self):
        redis = FakeRedis()
        sentinel = self._sentinel([self._scores(0.5), self._scores(0.15)], redis)
        user = {"tenant_id": "default", "user_id": "10000001"}

        await sentinel._scan_scores(redis, user, self._watch(), {}, 1e9)
        alerts = await sentinel._scan_scores(
            redis, user, self._watch(), {"score_threshold": 0.2}, 1e9 + 60
        )

        assert len(alerts) == 1
        assert alerts[0]["severity"] == SEVERITY_WARNING

    @pytest.mark.asyncio
    async def test_stale_baseline_is_reseeded_silently(self):
        redis = FakeRedis()
        sentinel = self._sentinel(
            [
                self._scores(0.5, as_of="2026-09-01"),
                self._scores(-0.3, as_of="2026-09-20"),
            ],
            redis,
        )
        user = {"tenant_id": "default", "user_id": "10000001"}

        await sentinel._scan_scores(redis, user, self._watch(), {}, 1e9)
        alerts = await sentinel._scan_scores(redis, user, self._watch(), {}, 1e9 + 60)

        assert alerts == []  # 停摆 19 天：那跌是上周的事，不报成今天
        stored = redis.hashes[f"{BASELINE_KEY_PREFIX}default:10000001"]
        assert json.loads(stored["SH600036"])["v"] == -0.3  # 但基线要跟上

    @pytest.mark.asyncio
    async def test_unmonitored_symbols_are_not_stored(self):
        redis = FakeRedis()
        sentinel = self._sentinel(
            [
                {
                    "SZ000001": {
                        "value": 0.3,
                        "side": "BUY",
                        "freq": "daily",
                        "asOf": "2026-09-20",
                    }
                }
            ],
            redis,
        )

        alerts = await sentinel._scan_scores(
            redis,
            {"tenant_id": "default", "user_id": "10000001"},
            self._watch(),
            {},
            1e9,
        )

        assert alerts == []
        assert redis.hashes == {}

    @pytest.mark.asyncio
    async def test_empty_score_snapshot_does_not_touch_baseline(self):
        # 取不到分 ≠ 分数是 0：动了基线，下一轮恢复有分时会被当成「变化」
        redis = FakeRedis()

        async def _empty(tenant: str):
            return {}, {"ok": False, "reason": "DB down"}

        sentinel = HoldingSentinel(redis=redis, score_loader=_empty)
        before = dict(redis.hashes)

        alerts = await sentinel._scan_scores(
            redis,
            {"tenant_id": "default", "user_id": "10000001"},
            self._watch(),
            {},
            1e9,
        )

        assert alerts == []
        assert redis.hashes == before

    @pytest.mark.asyncio
    async def test_exited_symbols_are_removed_from_baseline(self):
        redis = FakeRedis()
        redis.hashes[f"{BASELINE_KEY_PREFIX}default:10000001"] = {
            "SH600036": json.dumps({"v": 0.1, "d": "2026-09-20"}),
            "SZ000001": json.dumps({"v": 0.2, "d": "2026-09-20"}),
        }
        sentinel = self._sentinel([self._scores(0.1)], redis)

        await sentinel._scan_scores(
            redis,
            {"tenant_id": "default", "user_id": "10000001"},
            self._watch(),
            {},
            1e9,
        )

        stored = redis.hashes[f"{BASELINE_KEY_PREFIX}default:10000001"]
        assert set(stored) == {"SH600036"}

    @pytest.mark.asyncio
    async def test_drop_rule_alerts_on_single_step_collapse(self):
        """隔夜重打分跳水（一次迁移内跌超阈值）→ score_drop 警告。"""
        redis = FakeRedis()
        sentinel = self._sentinel([self._scores(0.5), self._scores(0.1)], redis)
        user = {"tenant_id": "default", "user_id": "10000001"}
        cfg = {"score_drop_threshold": 0.35}

        await sentinel._scan_scores(redis, user, self._watch(), cfg, 1e9)
        alerts = await sentinel._scan_scores(redis, user, self._watch(), cfg, 1e9 + 60)

        assert len(alerts) == 1
        assert alerts[0]["kind"] == KIND_SCORE_DROP
        assert alerts[0]["severity"] == SEVERITY_WARNING
        assert alerts[0]["score_prev"] == 0.5
        assert alerts[0]["score_now"] == 0.1
        record = json.loads(
            redis.hashes[f"{BASELINE_KEY_PREFIX}default:10000001"]["SH600036"]
        )
        assert record["a"] == 0.5  # 当日基准保持首见值，不随分移动
        assert record["ad"] == "2001-09-09"

    @pytest.mark.asyncio
    async def test_small_drop_is_below_threshold_and_silent(self):
        redis = FakeRedis()
        sentinel = self._sentinel([self._scores(0.5), self._scores(0.2)], redis)
        user = {"tenant_id": "default", "user_id": "10000001"}

        await sentinel._scan_scores(redis, user, self._watch(), {}, 1e9)
        alerts = await sentinel._scan_scores(
            redis, user, self._watch(), {"score_drop_threshold": 0.35}, 1e9 + 60
        )

        assert alerts == []

    @pytest.mark.asyncio
    async def test_gradual_slide_fires_when_day_anchor_crossed(self):
        """阴跌（每步小于阈值）累计越过当日基准回落线 → 报一次。"""
        redis = FakeRedis()
        sentinel = self._sentinel(
            [self._scores(0.5), self._scores(0.35), self._scores(0.2), self._scores(0.1)],
            redis,
        )
        user = {"tenant_id": "default", "user_id": "10000001"}
        cfg = {"score_drop_threshold": 0.35}

        await sentinel._scan_scores(redis, user, self._watch(), cfg, 1e9)
        a1 = await sentinel._scan_scores(redis, user, self._watch(), cfg, 1e9 + 60)
        a2 = await sentinel._scan_scores(redis, user, self._watch(), cfg, 1e9 + 120)
        a3 = await sentinel._scan_scores(redis, user, self._watch(), cfg, 1e9 + 180)

        assert a1 == [] and a2 == []
        assert len(a3) == 1
        assert a3[0]["kind"] == KIND_SCORE_DROP
        # 越线后继续阴跌不重复报（同一段回落只有一个事件）
        a4 = await sentinel._scan_scores(
            redis, user, self._watch(), cfg, 1e9 + 240
        )
        assert a4 == []

    @pytest.mark.asyncio
    async def test_new_day_reanchors_before_measuring(self):
        """跨日首扫重设当日基准：昨天的跌幅不按「今日回落」重报。"""
        redis = FakeRedis()
        sentinel = self._sentinel(
            [self._scores(0.5), self._scores(0.3), self._scores(0.1)], redis
        )
        user = {"tenant_id": "default", "user_id": "10000001"}
        cfg = {"score_drop_threshold": 0.35}

        await sentinel._scan_scores(redis, user, self._watch(), cfg, 1e9)  # 当日锚 0.5
        await sentinel._scan_scores(redis, user, self._watch(), cfg, 1e9 + 86400)
        alerts = await sentinel._scan_scores(
            redis, user, self._watch(), cfg, 1e9 + 86460
        )

        # 新一天从 0.3 起锚：0.3→0.1 单步 0.2、距锚 0.2，都不到 0.35
        assert alerts == []
        record = json.loads(
            redis.hashes[f"{BASELINE_KEY_PREFIX}default:10000001"]["SH600036"]
        )
        assert record["a"] == 0.3
        assert record["ad"] == "2001-09-10"

    @pytest.mark.asyncio
    async def test_new_day_reanchor_does_not_swallow_cross_zero(self):
        """重锚只让基准交叉规则本轮失效——prev 类规则照跑。

        哨兵停机一夜后第一个早晨若把隔夜下穿 0 静默吞掉，用户就少了一次
        最该响的提醒。
        """
        redis = FakeRedis()
        redis.hashes[f"{BASELINE_KEY_PREFIX}default:10000001"] = {
            "SH600036": json.dumps(
                {"v": 0.4, "d": "2026-09-19", "a": 0.4, "ad": "2001-09-08"}
            )
        }
        sentinel = self._sentinel([self._scores(-0.1)], redis)

        alerts = await sentinel._scan_scores(
            redis,
            {"tenant_id": "default", "user_id": "10000001"},
            self._watch(),
            {"score_drop_threshold": 0.35},
            1e9,
        )

        assert len(alerts) == 1
        assert alerts[0]["kind"] == KIND_SCORE_CROSS_ZERO
        assert alerts[0]["severity"] == SEVERITY_CRITICAL

    @pytest.mark.asyncio
    async def test_legacy_baseline_without_anchor_is_upgraded_silently(self):
        """老格式记录（无 a/ad）升级：补锚、不凭缺失字段报「回落」。"""
        redis = FakeRedis()
        redis.hashes[f"{BASELINE_KEY_PREFIX}default:10000001"] = {
            "SH600036": json.dumps({"v": 0.5, "d": "2026-09-20"})
        }
        sentinel = self._sentinel([self._scores(0.3)], redis)

        alerts = await sentinel._scan_scores(
            redis,
            {"tenant_id": "default", "user_id": "10000001"},
            self._watch(),
            {"score_drop_threshold": 0.35},
            1e9,
        )

        assert alerts == []  # 0.5→0.3 单步 0.2 < 0.35；锚缺失不臆造交叉事件
        record = json.loads(
            redis.hashes[f"{BASELINE_KEY_PREFIX}default:10000001"]["SH600036"]
        )
        assert record["a"] == 0.3
        assert record["ad"] == "2001-09-09"

    @pytest.mark.asyncio
    async def test_drop_rule_zero_disables(self):
        redis = FakeRedis()
        sentinel = self._sentinel([self._scores(0.5), self._scores(0.12)], redis)
        user = {"tenant_id": "default", "user_id": "10000001"}

        await sentinel._scan_scores(redis, user, self._watch(), {}, 1e9)
        alerts = await sentinel._scan_scores(
            redis, user, self._watch(), {"score_drop_threshold": 0.0}, 1e9 + 60
        )

        assert alerts == []


# ── QQ 摘要接线（2026-10-09 降噪）────────────────────────────────────


class TestQqDigestWiring:
    """一轮几十条持仓预警 → 站内逐条、QQ 一条摘要（防手机被淹没）。"""

    @staticmethod
    def _alert(**over):
        base = {
            "tenant_id": "default",
            "user_id": "10000001",
            "symbol": "SH600036",
            "stock_name": "招商银行",
            "kind": KIND_SCORE_CROSS_ZERO,
            "severity": SEVERITY_CRITICAL,
            "title": "招商银行 分数降至 0 及以下",
            "content": "SH600036 信号分 +0.120 → -0.030 详情见交易台 · 持仓监控。",
            "score_prev": 0.12,
            "score_now": -0.03,
            "action_url": "/trading?tab=position&symbol=SH600036",
        }
        base.update(over)
        return base

    def test_notify_keeps_qq_off_for_inapp(self, monkeypatch):
        """站内卡片照旧逐条落库；QQ 面不得逐条旁路（归 _persist 的摘要）。"""
        captured: dict = {}

        def _fake_publish(**kw):
            captured.update(kw)
            return True

        monkeypatch.setattr(
            "backend.shared.notification_publisher.publish_notification", _fake_publish
        )
        sentinel = HoldingSentinel(redis=FakeRedis())
        assert sentinel._notify(self._alert(), {"notify_inapp": True}) is True
        assert captured["qq_alert"] is False
        assert captured["type"] == "holding_alert"

    def test_enqueue_digest_folds_lines_under_one_key(self, monkeypatch):
        import backend.shared.qq_digest as qq_digest

        calls: list[dict] = []
        monkeypatch.setattr(qq_digest, "enqueue", lambda **kw: calls.append(kw))
        HoldingSentinel._enqueue_qq_digest("default", "10000001", ["行1", "行2"])

        assert [c["line"] for c in calls] == ["行1", "行2"]
        assert {c["digest_key"] for c in calls} == {"holding:default:10000001"}
        assert all(c["header"] == "持仓预警" for c in calls)
        assert all("持仓监控" in c["footer"] for c in calls)

    def test_persist_source_guards_digest_after_notify(self):
        """源码守卫：摘要行只在站内通知**成功后**收集（通知失败的告警不进摘要），
        收集完逐用户入队。_persist 依赖真库会话，用源码顺序钉住接线。"""
        import inspect

        src = inspect.getsource(HoldingSentinel._persist)
        idx_notify = src.index("self._notify(")
        idx_digest = src.index("format_holding_digest_line(")
        idx_enqueue = src.index("self._enqueue_qq_digest(")
        assert idx_notify < idx_digest < idx_enqueue
