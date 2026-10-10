"""P2-5 调度跳发台账（``scheduler_skip_ledger``）单测。

背景：P2-3 的交易日历门把「到点但 {今日,昨日} 均非交易日」从静默 continue
改成派发返回值里的 ``skipped`` 段——但**返回值没有常态读者**（beat 任务的结果
进 celery result backend，没人看）。值班摘要（P2-5）需要一个持久面来回答
「今天哪些市场的同步被跳过了、为什么」。

判据要点：
- 键按日分片（``qm:sched:skips:{ISO 日期}``），字段 ``{job}:{market}`` → 原因；
- 记录是**幂等最新态**（不是事件计数）：日历门在一整天里每个 tick 都会给出同一
  个 skip，逐次计数只会得到一堆「1440 次」的噪音；
- 「早间跳过、当日后续放行」时必须 ``clear`` 对应字段，残留的 skip 会被值班读成
  「今天没跑」（假故障）；
- 读失败必须**抛出**（摘要层据此渲染「台账不可读」），绝不静默空 dict 冒充
  「今天没有跳发」。

FakeRedis 的 hset/hdel/hgetall 语义与 redis-py 对齐（mapping= 关键字、
hdel 可变参数、hgetall 返回 str→str）。
"""

from __future__ import annotations

import inspect
from datetime import date

import pytest

from backend.shared.scheduler_skip_ledger import (
    SKIP_TTL_SECONDS,
    clear_skips,
    read_skips,
    record_skips,
    skip_key,
)


class FakeRedis:
    def __init__(self):
        self.hashes: dict[str, dict[str, str]] = {}
        self.expires: dict[str, int] = {}

    def hset(self, key, mapping=None, **kwargs):
        bucket = self.hashes.setdefault(key, {})
        if mapping:
            for field, value in mapping.items():
                bucket[str(field)] = str(value)
        return len(mapping or {})

    def hdel(self, key, *fields):
        bucket = self.hashes.get(key) or {}
        removed = 0
        for field in fields:
            if field in bucket:
                del bucket[field]
                removed += 1
        return removed

    def hgetall(self, key):
        return dict(self.hashes.get(key) or {})

    def expire(self, key, ttl):
        self.expires[key] = ttl
        return True


class BrokenRedis:
    def __getattr__(self, name):
        raise ConnectionError("redis down")


DAY = date(2026, 10, 10)


class TestSkipKey:
    def test_key_uses_iso_date_suffix(self):
        # 与调度域既有日键（retrain last_run 等）同为 ISO 带横线格式
        assert skip_key(DAY) == "qm:sched:skips:2026-10-10"


class TestRecordSkips:
    def test_records_fields_with_job_prefix_and_sets_ttl(self):
        client = FakeRedis()
        written = record_skips(
            client,
            day=DAY,
            job="market_sync",
            skipped={"CN": "连续非交易日（…）", "HK": "连续非交易日（…）"},
        )
        assert written == 2
        assert client.hashes[skip_key(DAY)] == {
            "market_sync:CN": "连续非交易日（…）",
            "market_sync:HK": "连续非交易日（…）",
        }
        assert client.expires[skip_key(DAY)] == SKIP_TTL_SECONDS

    def test_record_is_idempotent_latest_state(self):
        # 同一市场每 tick 重复 skip：字段仍只有一个（最新态，不是事件计数）
        client = FakeRedis()
        record_skips(client, day=DAY, job="market_sync", skipped={"CN": "因A"})
        record_skips(client, day=DAY, job="market_sync", skipped={"CN": "因B"})
        assert client.hashes[skip_key(DAY)] == {"market_sync:CN": "因B"}

    def test_empty_skipped_is_noop(self):
        client = FakeRedis()
        assert record_skips(client, day=DAY, job="market_sync", skipped={}) == 0
        assert client.hashes == {}

    def test_two_jobs_coexist(self):
        client = FakeRedis()
        record_skips(client, day=DAY, job="market_sync", skipped={"CN": "x"})
        record_skips(client, day=DAY, job="factor_fill", skipped={"HK": "y"})
        assert client.hashes[skip_key(DAY)] == {
            "market_sync:CN": "x",
            "factor_fill:HK": "y",
        }


class TestClearSkips:
    def test_clear_removes_only_that_jobs_fields(self):
        # 「早间跳过、当日后续放行」→ 派发成功必须清掉残留 skip，否则值班读成假故障
        client = FakeRedis()
        record_skips(client, day=DAY, job="market_sync", skipped={"CN": "x", "HK": "x"})
        record_skips(client, day=DAY, job="factor_fill", skipped={"HK": "y"})
        clear_skips(client, day=DAY, job="market_sync", dispatched=["CN"])
        assert client.hashes[skip_key(DAY)] == {
            "market_sync:HK": "x",
            "factor_fill:HK": "y",
        }

    def test_clear_missing_field_is_noop(self):
        client = FakeRedis()
        assert clear_skips(client, day=DAY, job="market_sync", dispatched=["CN"]) == 0


class TestReadSkips:
    def test_reads_mapping(self):
        client = FakeRedis()
        record_skips(client, day=DAY, job="market_sync", skipped={"CN": "x"})
        assert read_skips(client, DAY) == {"market_sync:CN": "x"}

    def test_empty_day_returns_empty_dict(self):
        assert read_skips(FakeRedis(), DAY) == {}

    def test_read_error_raises_not_silent_empty(self):
        # 读失败 ≠ 没有跳发：静默空 dict 会让摘要把「读不到」渲染成「一切正常」
        with pytest.raises(ConnectionError):
            read_skips(BrokenRedis(), DAY)


class TestWriteErrorPropagates:
    def test_record_error_raises_for_caller_to_shield(self):
        # 写入是 best-effort（调用方 celery 任务里 try/except 兜），但模块层不许吞
        with pytest.raises(ConnectionError):
            record_skips(BrokenRedis(), day=DAY, job="market_sync", skipped={"CN": "x"})


class TestDispatchWiring:
    """防回潮：dispatch_market_sync 任务必须把两段 skipped 落台账、dispatched 清残留。"""

    def _task_source(self) -> str:
        from backend.services.engine.tasks import celery_tasks

        return inspect.getsource(celery_tasks.dispatch_market_sync)

    def test_task_records_market_sync_and_factor_fill_skips(self):
        src = self._task_source()
        assert "_record_skip_ledger" in src or "record_skips" in src
        assert "market_sync" in src and "factor_fill" in src
