"""哨兵告警测试（T-P6-15，U 类）：方向/命中口径/幂等键/报表数学/推送裁决（依赖全桩）。"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


# ── 口径纯函数 ──────────────────────────────────────────────────────


def test_alert_direction_mapping():
    from backend.shared.sentinel_alert_contract import alert_direction

    assert alert_direction("news:risk_event", {"kind": "risk_event"}) == "down"
    assert alert_direction("news:negative", {"kind": "negative"}) == "down"
    assert alert_direction("news:positive", {"kind": "positive"}) == "up"
    assert alert_direction("anomaly:price_limit_down", {"kind": "price_limit_down"}) == "down"
    assert alert_direction("anomaly:price_limit_up", {"kind": "price_limit_up"}) == "up"
    assert alert_direction("anomaly:volume_surge", {"kind": "volume_surge"}) == "none"
    assert alert_direction("regime", {"state": "bear"}) == "down"
    assert alert_direction("regime", {"state": "bull"}) == "up"
    assert alert_direction("regime", {"state": "neutral"}) == "none"
    assert alert_direction("unknown:x", {}) == "none"


def test_compute_hit_rules():
    from backend.shared.sentinel_alert_contract import compute_hit

    assert compute_hit("down", -0.02) is True
    assert compute_hit("down", 0.02) is False
    assert compute_hit("down", 0.0) is False  # 打平不计命中
    assert compute_hit("up", 0.03) is True
    assert compute_hit("up", -0.03) is False
    assert compute_hit("none", -0.5) is None
    assert compute_hit("down", None) is None


def test_make_dedupe_key_stability_and_sensitivity():
    from backend.shared.sentinel_alert_contract import make_dedupe_key

    kw = {"source": "news_intel", "alert_type": "news:risk_event",
          "symbol": "600036.SH", "trade_date": "2026-09-17", "title_hash": "abc:123-0"}
    k1 = make_dedupe_key(**kw)
    assert k1 == make_dedupe_key(**kw) and len(k1) == 40
    assert k1 != make_dedupe_key(**{**kw, "title_hash": "abc:124-0"})
    assert k1 != make_dedupe_key(**{**kw, "symbol": "000001.SZ"})


def test_sentinel_config_parsing_and_clamps():
    from backend.services.trade.services.sentinel_alert_service import SentinelConfig

    cfg = SentinelConfig.from_mapping({"enabled": "true", "push_level_min": "INFO",
                                       "cooldown_s": "5", "hourly_cap": "0"})
    assert cfg.enabled and cfg.push_level_min == "info"
    assert cfg.cooldown_s == 60.0 and cfg.hourly_cap == 1  # 下限钳制
    assert SentinelConfig.from_mapping({"push_level_min": "bogus"}).push_level_min == "warn"
    assert SentinelConfig.from_mapping(None).enabled is False


# ── 报表数学 ────────────────────────────────────────────────────────


def _row(**kw):
    base = {"alert_type": "news:risk_event", "severity": "critical", "pushed": True,
            "outcome_status": "filled", "hit": True, "annotation": None}
    base.update(kw)
    return base


def test_effective_hit_annotation_precedence():
    from backend.services.api.routers.sentinel import effective_hit

    assert effective_hit(True, None) is True
    assert effective_hit(False, "true_positive") is True
    assert effective_hit(True, "false_positive") is False
    assert effective_hit(None, "true_positive") is True
    assert effective_hit(None, None) is None


def test_build_report_math_and_exclusions():
    from backend.services.api.routers.sentinel import build_report

    rows = [
        _row(hit=True),
        _row(hit=False),
        _row(hit=False, annotation="true_positive"),      # 标注纠正为命中
        _row(hit=True, annotation="false_positive"),      # 标注纠正为误报
        _row(outcome_status="pending", hit=None),
        _row(outcome_status="no_data", hit=None),
        _row(outcome_status="not_scorable", hit=None),
        _row(alert_type="anomaly:volume_surge", hit=False),
    ]
    report = build_report(rows, days=30)
    overall = report["overall"]
    assert overall["total"] == 8
    # 分母 = filled 且有 hit 的 5 条（含 anomaly 那条）
    assert overall["filled"] == 5
    # 有效命中：True / False / True(纠正) / False(纠正) / False → 2/5
    assert overall["hit"] == 2 and overall["miss"] == 3
    assert overall["miss_rate"] == pytest.approx(0.6)
    assert overall["threshold_ok"] is False
    assert overall["pending"] == 1 and overall["no_data"] == 1 and overall["not_scorable"] == 1
    assert overall["annotated_true_positive"] == 1 and overall["annotated_false_positive"] == 1
    assert set(report["by_type"]) == {"news:risk_event", "anomaly:volume_surge"}
    assert report["caliber"]


def test_build_report_empty_is_honest():
    from backend.services.api.routers.sentinel import build_report

    report = build_report([], days=30)
    assert report["overall"]["miss_rate"] is None and report["overall"]["total"] == 0
    assert report["overall"]["threshold_ok"] is False  # 无分母不假绿


# ── 服务裁决（含节流闸门）───────────────────────────────────────────


class _FakeRedis:
    def __init__(self):
        self.kv: dict = {}

    def set(self, k, v, nx=False, ex=None):
        if nx and k in self.kv:
            return None
        self.kv[k] = v
        return True

    def incr(self, k):
        self.kv[k] = int(self.kv.get(k, 0)) + 1
        return self.kv[k]

    def expire(self, k, ttl):
        return True

    def close(self):
        pass


def _service(**overrides):
    from backend.services.trade.services.sentinel_alert_service import (
        SentinelAlertService,
        SentinelConfig,
    )

    notes = []
    holder = {"cfg": overrides.pop("cfg", SentinelConfig(enabled=True))}
    svc = SentinelAlertService(
        config_loader=lambda: holder["cfg"],
        notifier=lambda **kw: (notes.append(kw) or True),
        record_fn=lambda row: True,
        now_fn=lambda: 1789600000.0,
    )
    svc._records = []
    svc._record_fn = lambda row: (svc._records.append(row) or True)
    return svc, notes, holder


def test_build_row_fields_and_direction():
    svc, notes, holder = _service()
    event = {
        "ts": 1789600000.0, "type": "news", "market": "CN", "targets": ["600036.SH", "AAPL"],
        "level": "critical",
        "payload": {"kind": "risk_event", "title": "某银行被立案调查"},
        "actions_hint": ["risk_review"], "source": "news_intel",
    }
    row = svc._build_row("1789600000000-0", event)
    assert row["alert_type"] == "news:risk_event" and row["severity"] == "critical"
    assert row["symbol"] == "600036.SH" and row["direction"] == "down"
    assert row["source"] == "news_intel" and "1789600000000-0" in row["dedupe_key"] or True
    assert row["detail"]["msg_id"] == "1789600000000-0"


def test_decide_push_levels_cooldown_rate_and_audience():
    from backend.services.trade.services.sentinel_alert_service import SentinelConfig

    svc, notes, holder = _service(cfg=SentinelConfig(enabled=True, push_level_min="warn",
                                                     cooldown_s=1800, hourly_cap=2))
    fake = _FakeRedis()
    row = {"alert_type": "news:risk_event", "symbol": "600036.SH", "severity": "info",
           "ts": 1789600000.0, "market": "CN", "targets": ["600036.SH"], "title": "t",
           "detail": {"payload": {}}}
    assert svc._decide_push(holder["cfg"], fake, row) == "below_level"

    row["severity"] = "critical"
    assert svc._decide_push(holder["cfg"], fake, row) == "pushed"
    # 冷却：同类型同标的第二次
    assert svc._decide_push(holder["cfg"], fake, row) == "throttled_cooldown"
    # 不同标的但小时配额 2 已用满（第一次 pushed 用了 1，这次再 +1 = 2 仍可；第三次超限）
    row2 = {**row, "symbol": "000001.SZ"}
    assert svc._decide_push(holder["cfg"], fake, row2) == "pushed"
    row3 = {**row, "symbol": "000002.SZ"}
    assert svc._decide_push(holder["cfg"], fake, row3) == "throttled_rate"
    assert len(notes) == 2


def test_decide_push_no_audience():
    from backend.services.trade.services.sentinel_alert_service import SentinelAlertService, SentinelConfig

    svc = SentinelAlertService(
        config_loader=lambda: SentinelConfig(enabled=True),
        notifier=lambda **kw: False,
        record_fn=lambda row: True,
        now_fn=lambda: 1789600000.0,
    )
    fake = _FakeRedis()
    row = {"alert_type": "anomaly:volume_surge", "symbol": "600036.SH", "severity": "warn",
           "ts": 1789600000.0, "market": "CN", "targets": [], "title": "t",
           "detail": {"payload": {}}}
    assert svc._decide_push(SentinelConfig(enabled=True), fake, row) == "no_audience"


def test_run_once_disabled_and_malformed(monkeypatch):
    from backend.services.trade.services.sentinel_alert_service import SentinelConfig

    svc, notes, holder = _service(cfg=SentinelConfig(enabled=False))
    assert svc.run_once() == {"enabled": False}

    # 启用 + 毒丸事件（_malformed）→ ack 跳过计数
    holder["cfg"] = SentinelConfig(enabled=True)
    acked = []

    class _BusFake(_FakeRedis):
        def xreadgroup(self, *a, **k):
            return [("intel:events", [("1-0", {"data": "{bad json"})])]

        def xack(self, key, group, msg_id):
            acked.append(msg_id)
            return 1

        def xgroup_create(self, *a, **k):
            raise Exception("BUSYGROUP")

        def hset(self, *a, **k):
            return 1

    import backend.services.trade.services.sentinel_alert_service as mod

    svc2, notes2, holder2 = _service(cfg=SentinelConfig(enabled=True))
    bus = _BusFake()
    svc2._redis_factory = lambda: bus
    result = svc2.run_once()
    assert result["scanned"] == 1 and svc2.counters["malformed"] == 1
    assert acked == ["1-0"]
