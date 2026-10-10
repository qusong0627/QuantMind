"""风控影子报告（T-RC-02）「会拦」口径：灰度档位（P2-1）后 verdict 两态拆分。

`would_block` 只计**未生效**的 reject/halt 留痕（假设会拦），`blocked_enforced`
计**已生效**拦截（事实）——混算会把翻闸后预计新增的拦截数算高，而这正是定档
要读的那个数（评审 M1 消费面）。
"""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from backend.scripts import risk_shadow_report as rsr

_CST = ZoneInfo("Asia/Shanghai")


class _FakeStreamRedis:
    """最小 xrange 假体：键 → [(id, fields)]（字段值模拟 Redis 里的 JSON 字符串）。"""

    def __init__(self, rows_by_key: dict[str, list[tuple[str, dict]]]):
        self.rows_by_key = rows_by_key

    def xrange(self, key):
        return self.rows_by_key.get(key, [])

    def close(self):
        pass


def _entry(verdict: str, enforced: bool) -> dict:
    return {
        "ts": "0",  # 0 → 不计盘中样本（本文件只验「会拦」口径）
        "verdict": json.dumps(verdict),
        "enforced": json.dumps(enforced),
        "source": json.dumps("unit"),
        "decisions": json.dumps([]),
    }


def test_would_block_counts_only_unenforced_entries(monkeypatch):
    # Arrange：影子会拦 2（reject+halt）、灰度已生效 1（reject）、warn/pass 不拦
    rows = [
        ("1-0", _entry("reject", False)),
        ("2-0", _entry("halt", False)),
        ("3-0", _entry("reject", True)),
        ("4-0", _entry("warn", False)),
        ("5-0", _entry("pass", False)),
    ]
    fake = _FakeStreamRedis({"qm:risk:decisions:20261010": rows})
    monkeypatch.setattr(rsr, "_redis", lambda: fake)

    # Act
    rep = rsr.collect(days=1, today=datetime(2026, 10, 10, 12, 0, tzinfo=_CST))

    # Assert：假设与事实分列；verdict 分布不受影响（旧字段逐字保留）
    assert rep["would_block"] == 2
    assert rep["blocked_enforced"] == 1
    assert rep["verdicts"] == {"reject": 2, "halt": 1, "warn": 1, "pass": 1}
    assert rep["total"] == 5


def test_would_block_matches_legacy_global_mode(monkeypatch):
    """旧配置全局同档（全影子）：全部留痕未生效 → would_block=全部拦项、blocked=0，
    数额与旧口径「会拦 N」一致（升级不改旧报告的数）。"""
    rows = [
        ("1-0", _entry("reject", False)),
        ("2-0", _entry("reject", False)),
        ("3-0", _entry("halt", False)),
    ]
    fake = _FakeStreamRedis({"qm:risk:decisions:20261010": rows})
    monkeypatch.setattr(rsr, "_redis", lambda: fake)

    rep = rsr.collect(days=1, today=datetime(2026, 10, 10, 12, 0, tzinfo=_CST))

    assert rep["would_block"] == 3
    assert rep["blocked_enforced"] == 0
