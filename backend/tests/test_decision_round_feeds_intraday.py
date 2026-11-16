"""决策轮盘中结构读口（feeds）单测：有效/缺失/坏 JSON/过期。"""

from __future__ import annotations

import json
import time

from backend.services.trade.services.decision_round_feeds import read_intraday_live


class _FakeRedis:
    def __init__(self, raw=None):
        self.raw = raw

    def hget(self, *_args, **_kwargs):
        if isinstance(self.raw, Exception):
            raise self.raw
        return self.raw


def test_returns_doc_when_fresh():
    doc = {"ts": time.time(), "breadth": {"n": 5582}}
    got = read_intraday_live(_FakeRedis(json.dumps(doc)))
    assert got == doc


def test_missing_or_none_client_returns_none():
    assert read_intraday_live(None) is None
    assert read_intraday_live(_FakeRedis(None)) is None


def test_stale_doc_returns_none():
    doc = {"ts": time.time() - 1000, "breadth": {"n": 1}}
    assert read_intraday_live(_FakeRedis(json.dumps(doc))) is None


def test_bad_json_returns_none():
    assert read_intraday_live(_FakeRedis("{not json")) is None


def test_non_mapping_doc_returns_none():
    assert read_intraday_live(_FakeRedis("[1,2,3]")) is None


def test_redis_failure_returns_none():
    assert read_intraday_live(_FakeRedis(RuntimeError("boom"))) is None
