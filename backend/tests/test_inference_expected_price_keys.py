"""推理批量取价：键形 / 字段名兼容（2026-11-10 P0）。

故障：celery 已连上行情 Redis（``www.quantmindai.cn:6379 db=3``），随后日志
「批量取价：请求 3272 只，命中 0 只」，当日约 19549 条
``engine_signal_scores.expected_price`` 全 NULL。不是连接失败——是
``_fetch_expected_prices`` 只查 ``market:snapshot:{prefix.lower()}`` 且只读
字段 ``Now``；远端行情服写侧实测是后缀式 ``market:snapshot:600036.SH``。

本组用例把「双键形 + 双字段名 + 缺失不写 0」钉成不变量。全部使用假 Redis，
不连任何真实实例。
"""

from __future__ import annotations

import pytest

from backend.services.engine.inference.script_runner import InferenceScriptRunner
from backend.shared.decision_context_source import snapshot_price


class _FakePipeline:
    """最小 hgetall 管道替身：记录发出的键、按 store 返回 hash。"""

    def __init__(self, store: dict[str, dict[str, str]], sink: list[str]) -> None:
        self._store = store
        self._sink = sink
        self._issued: list[str] = []

    def hgetall(self, key: str):
        self._issued.append(key)
        self._sink.append(key)
        return self

    def execute(self):
        out = [self._store.get(key, {}) for key in self._issued]
        self._issued = []
        return out


class _FakeRedis:
    def __init__(self, store: dict[str, dict[str, str]]) -> None:
        self.store = store
        self.issued_keys: list[str] = []
        self.pipeline_calls = 0

    def pipeline(self, transaction: bool = True):
        # read_snapshots 的契约：远端行情服禁 MULTI，必须非事务管道
        assert transaction is False, "批量取价必须用 transaction=False 管道"
        self.pipeline_calls += 1
        return _FakePipeline(self.store, self.issued_keys)


@pytest.mark.unit
@pytest.mark.parametrize("raw_symbol", ["sh600036", "600036.SH", "600036"])
def test_suffix_only_keys_hit_for_all_symbol_forms(raw_symbol: str):
    """远端只落后缀键时，三种输入形态都必须命中。"""
    redis = _FakeRedis({"market:snapshot:600036.SH": {"Now": "42.77"}})

    out = InferenceScriptRunner._fetch_expected_prices(
        redis, ["600036"], [raw_symbol]
    )

    assert out == {"600036": 42.77}
    assert "market:snapshot:600036.SH" in redis.issued_keys
    assert "market:snapshot:sh600036" in redis.issued_keys


@pytest.mark.unit
def test_prefix_only_keys_still_hit_with_lowercase_field():
    """只落后缀式旧键、字段为 ``now`` 时也必须命中（兼容两套写侧）。"""
    redis = _FakeRedis({"market:snapshot:sh600036": {"now": "42.77"}})

    out = InferenceScriptRunner._fetch_expected_prices(
        redis, ["600036"], ["SH600036"]
    )

    assert out == {"600036": 42.77}


@pytest.mark.unit
def test_suffix_form_wins_when_both_keys_present():
    """两套键都有时后缀式优先（read_snapshots 的既定顺序）。"""
    redis = _FakeRedis(
        {
            "market:snapshot:600036.SH": {"Now": "42.77"},
            "market:snapshot:sh600036": {"Now": "99.99"},
        }
    )

    out = InferenceScriptRunner._fetch_expected_prices(
        redis, ["600036"], ["600036.SH"]
    )

    assert out == {"600036": 42.77}


@pytest.mark.unit
def test_missing_or_nonpositive_price_is_absent_not_zero():
    """取不到 / 0 价 / 空串一律不进表——调用方写 NULL，不得伪造 0.0。"""
    redis = _FakeRedis(
        {
            "market:snapshot:600036.SH": {"Now": "0"},
            "market:snapshot:000001.SZ": {"Now": ""},
            "market:snapshot:300750.SZ": {"Now": "-1"},
        }
    )

    out = InferenceScriptRunner._fetch_expected_prices(
        redis, ["600036", "000001", "300750"], None
    )

    assert out == {}


@pytest.mark.unit
def test_snapshot_price_field_and_value_contract():
    assert snapshot_price({"Now": "1.5"}) == 1.5
    assert snapshot_price({"now": "1.5"}) == 1.5
    # Now 存在但为空时不能把 now 当第二选择以外的来源（字段优先级契约）
    assert snapshot_price({"Now": "", "now": "1.5"}) == 1.5
    assert snapshot_price({"Now": 0}) is None
    assert snapshot_price({"Now": "-1"}) is None
    assert snapshot_price({"PreClose": "9.9"}) is None
    assert snapshot_price({}) is None
    assert snapshot_price(None) is None
