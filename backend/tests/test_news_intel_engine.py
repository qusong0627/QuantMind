"""新闻情报服务测试（T-P6-12）：门控/去重/时效/风险 veto 接线/突变（依赖全桩）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.unit
_CST = timezone(timedelta(hours=8))


def _row(page_id: int, *, tags=(), score=0.0, label="neutral", tickers=("600036.SH",),
         age_min: float = 5.0, title_hash: int | None = None):
    return {
        "huntly_page_id": page_id,
        "tickers": list(tickers),
        "industries": [],
        "event_tags": list(tags),
        "sentiment_score": score,
        "sentiment_label": label,
        "title": f"标题{page_id}",
        "title_hash": title_hash if title_hash is not None else page_id,
        "enriched_at": (datetime.now(_CST) - timedelta(minutes=age_min)).isoformat(),
    }


def _engine(rows, **overrides):
    from backend.services.engine.news_intel_engine import NewsIntelConfig, NewsIntelEngine

    calls = {"events": [], "veto": [], "status": []}
    holder = {"cfg": overrides.pop("cfg", NewsIntelConfig(enabled=True)), "rows": list(rows)}

    def fetch(cfg, cursor):
        batch = holder["rows"]
        holder["rows"] = []
        return batch, (str(batch[-1]["huntly_page_id"]) if batch else "")

    engine = NewsIntelEngine(
        config_loader=lambda: holder["cfg"],
        fetch_fn=fetch,
        publisher=lambda e: calls["events"].append(e),
        veto_marker=lambda syms: (calls["veto"].append(list(syms)) or len(list(syms))),
        status_writer=lambda p: calls["status"].append(p),
        now_fn=lambda: 1789600000.0,
    )
    engine._test_client = None  # 由测试注入假 redis（见 _with_fake_redis）
    return engine, calls, holder


class _FakeRedis:
    def __init__(self):
        self.kv: dict = {}
        self.sets: dict = {}
        self.zsets: dict = {}
        self.expires: dict = {}

    def get(self, k):
        return self.kv.get(k)

    def set(self, k, v, nx=False, ex=None):
        if nx and k in self.kv:
            return None
        self.kv[k] = str(v)
        if ex:
            self.expires[k] = ex
        return True

    def sadd(self, k, *vals):
        s = self.sets.setdefault(k, set())
        before = len(s)
        s.update(str(v) for v in vals)
        return len(s) - before

    def expire(self, k, ttl):
        self.expires[k] = ttl
        return True

    def zadd(self, k, mapping):
        z = self.zsets.setdefault(k, {})
        z.update(mapping)
        return len(mapping)

    def zremrangebyscore(self, k, lo, hi):
        z = self.zsets.get(k, {})
        drop = [m for m, s in z.items() if lo <= s <= hi]
        for m in drop:
            z.pop(m, None)
        return len(drop)

    def zcard(self, k):
        return len(self.zsets.get(k, {}))

    def pipeline(self, transaction=False):
        return _FakePipeline(self)

    def close(self):
        pass


class _FakePipeline:
    def __init__(self, client):
        self.client = client
        self.ops = []

    def __getattr__(self, name):
        def _stash(*a, **k):
            self.ops.append((name, a, k))
            return self
        return _stash

    def execute(self):
        out = []
        for name, a, k in self.ops:
            out.append(getattr(self.client, name)(*a, **k))
        self.ops = []
        return out


def _patch_redis(monkeypatch, engine, fake):
    import backend.services.engine.news_intel_engine as mod

    monkeypatch.setattr(mod, "_main_redis", lambda: fake)
    return engine


def test_disabled_noop(monkeypatch):
    engine, calls, holder = _engine([_row(1, tags=["处罚"])])
    holder["cfg"] = type(holder["cfg"])(enabled=False)
    assert engine.build_once() == {"enabled": False}
    assert calls["events"] == []


def test_dedupe_stale_neutral_and_publish(monkeypatch):
    rows = [
        _row(1, tags=["财务造假"]),                # risk → publish + veto
        _row(2, tags=["财务造假"]),                # 同 title_hash → dedupe
        _row(3, tags=["中标"], title_hash=3),      # info → publish
        _row(4, age_min=600, title_hash=4),        # 陈旧 → skip
        _row(5, title_hash=5),                     # 中性 → skip
    ]
    rows[1]["title_hash"] = 1  # 与 page 1 同哈希（跨源重复稿）
    engine, calls, holder = _engine(rows)
    fake = _FakeRedis()
    _patch_redis(monkeypatch, engine, fake)

    result = engine.build_once()
    assert result["published"] == 2
    assert engine.counters["skipped_dup"] == 1
    assert engine.counters["skipped_stale"] == 1
    assert engine.counters["skipped_neutral"] == 1
    assert calls["veto"] == [["600036.SH"]]
    kinds = [e["payload"]["kind"] for e in calls["events"]]
    assert kinds == ["risk_event", "positive"]
    assert fake.kv.get("qm:news:intel:cursor") == "5"


def test_spike_after_threshold_with_cooldown(monkeypatch):
    rows = [
        _row(1, score=-0.8, label="bearish", title_hash=1),
        _row(2, score=-0.9, label="bearish", title_hash=2),
        _row(3, score=-0.7, label="bearish", title_hash=3),
    ]
    engine, calls, holder = _engine(rows)
    fake = _FakeRedis()
    _patch_redis(monkeypatch, engine, fake)

    engine.build_once()
    spikes = [e for e in calls["events"] if e["payload"]["kind"] == "sentiment_spike"]
    # 第 2 条起达到阈值 → 触发一次；第 3 条被冷却抑制
    assert len(spikes) == 1
    assert spikes[0]["level"] == "warn" and spikes[0]["targets"] == ["600036.SH"]
    assert engine.counters["spikes"] == 1


def test_veto_disabled_by_config(monkeypatch):
    rows = [_row(1, tags=["立案调查"])]
    from backend.services.engine.news_intel_engine import NewsIntelConfig

    engine, calls, holder = _engine(rows, cfg=NewsIntelConfig(enabled=True, veto_enabled=False))
    fake = _FakeRedis()
    _patch_redis(monkeypatch, engine, fake)
    engine.build_once()
    assert calls["veto"] == []
    assert engine.counters["risk_events"] == 1
