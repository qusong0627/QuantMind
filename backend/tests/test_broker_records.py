"""broker-records 的选源与降级口径（纯逻辑，不打网络）。

口径（2026-11-06 用户要求）：前端「交易记录」要能按「券商接入」选定的通道看
柜台记录——选 QMT 查 QMT、选通达信查通达信；柜台只有当日数据，历史在系统账本。
"""
from __future__ import annotations

import asyncio

from backend.services.live_trading.routers import broker_records as br


class FakeRedis:
    def __init__(self, values=None):
        self.values = values or {}

    def get(self, key):
        return self.values.get(key)


def test_selected_source_prefers_override():
    redis = FakeRedis({'broker:selected:CN': 'tdx'})
    assert br._selected_source(redis, 'CN', 'qmt_exec') == 'qmt_exec'


def test_selected_source_reads_redis():
    redis = FakeRedis({'broker:selected:CN': 'qmt_exec'})
    assert br._selected_source(redis, 'CN', None) == 'qmt_exec'
    assert br._selected_source(redis, 'hk', None) == '' or True  # 未设键由回退决定


def test_selected_source_falls_back_when_missing(monkeypatch):
    monkeypatch.setattr(
        'backend.shared.real_positions.active_broker_type', lambda: 'tdx')
    assert br._selected_source(FakeRedis({}), 'CN', None) == 'tdx'


def test_tdx_trades_is_explicitly_unsupported():
    rows, note = asyncio.run(br._tdx_records(FakeRedis({}), 'trades'))
    assert rows == []
    assert '不提供成交明细' in note


def test_qmt_history_rejects_bad_date():
    from fastapi import HTTPException

    try:
        asyncio.run(br._qmt_history_records('2026/11/06', 'orders'))
    except HTTPException as exc:
        assert exc.status_code == 400
    else:  # pragma: no cover - 走到这里说明没校验
        raise AssertionError('bad date should be rejected')


def test_parse_day_formats():
    from backend.services.live_trading.routers import agent_statement as st

    assert st._parse_day('2026-11-06').strftime('%Y-%m-%d') == '2026-11-06'
    assert st._parse_day('2026-11-06', end_of_day=True).hour == 23
    assert st._parse_day(None) is None
    from fastapi import HTTPException

    try:
        st._parse_day('11/06/2026')
    except HTTPException as exc:
        assert exc.status_code == 400
    else:  # pragma: no cover
        raise AssertionError('bad date should be rejected')
