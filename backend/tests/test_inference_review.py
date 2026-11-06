import asyncio
from datetime import datetime, timezone

import pytest

from backend.services.api.services import inference_review as review
from backend.services.stream.market_app.services import remote_redis_source


class NoQuotes:
    _client = None

    async def fetch_quotes(self, symbols):
        return []


def test_historical_equal_weights_and_missing_quotes(monkeypatch):
    stocks = [{'code': '600036.SH'}, {'code': '600519.SH'}]
    history = {
        '600036.SH': {'2026-01-02': {'open': 10, 'close': 11}, '2026-01-05': {'open': 11, 'close': 12}},
        '600519.SH': {'2026-01-02': {'open': 20, 'close': 18}},
    }
    index = {'2026-01-02': {'open': 100, 'close': 102}, '2026-01-05': {'open': 102, 'close': 103}}
    monkeypatch.setattr(review, '_historical', lambda *args: (stocks, history, index, 2))
    monkeypatch.setattr(remote_redis_source, 'RemoteRedisDataSource', NoQuotes)
    data = asyncio.run(review.calculate_review({'summary': {'prediction_trade_date': '2026-01-02'}}, 10, True, '000300.SH'))
    assert data['days'][0]['cumulative'] == pytest.approx(0)
    assert data['days'][1]['cumulative'] is None
    assert data['days'][1]['coverage'] == 1
    assert data['days'][1]['benchmark'] == pytest.approx(3)
    assert data['benchmark_start'] == 100
    assert data['unknown_st'] == 2


def test_live_target_day_uses_open_and_never_renormalizes_partial(monkeypatch):
    today = datetime.now(review._TZ).date().isoformat()
    stocks = [{'code': '600036.SH'}, {'code': '600519.SH'}]
    monkeypatch.setattr(review, '_historical', lambda *args: (stocks, {}, {}, 0))
    class PartialQuotes:
        _client = None
        async def fetch_quotes(self, symbols):
            return [{'symbol': '600036.SH', 'timestamp': datetime.now(timezone.utc),
                     'is_stale': False, 'open_price': 10, 'current_price': 11, 'close_price': 9}]
    monkeypatch.setattr(remote_redis_source, 'RemoteRedisDataSource', PartialQuotes)
    monkeypatch.setattr(review, '_previous_closes', lambda *args: {'600036.SH': 4.5})
    data = asyncio.run(review.calculate_review({'summary': {'prediction_trade_date': today}}, 10, True, '000300.SH'))
    assert data['stocks'][0]['cumulative'] == pytest.approx(10)
    assert data['stocks'][0]['start'] == pytest.approx(5)
    assert data['stocks'][0]['latest'] == pytest.approx(5.5)
    assert data['days'][0]['cumulative'] is None
    assert data['days'][0]['coverage'] == 1


def test_series_quotes_accepts_tail_but_rejects_early_and_future():
    import json
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo('Asia/Shanghai')
    now = datetime(2026, 11, 6, 16, 30, tzinfo=tz)
    tail = datetime(2026, 11, 6, 14, 59, tzinfo=tz).timestamp()
    early = datetime(2026, 11, 6, 14, 0, tzinfo=tz).timestamp()
    future = datetime(2026, 11, 6, 16, 31, tzinfo=tz).timestamp()

    class Pipe:
        def __init__(self):
            self.keys = []
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        def zrevrange(self, key, *args, **kwargs):
            self.keys.append(key)
        async def execute(self):
            values = {'600036.SH': tail, '600519.SH': early, '300750.SZ': future}
            return [[(json.dumps({'price': 10, 'open': 9}), values[code])]
                    if (code := next((k for k in values if k in key), None)) else []
                    for key in self.keys]
    class Client:
        def pipeline(self, **kwargs):
            return Pipe()
    class Source:
        def _get_client(self):
            return Client()
    result = asyncio.run(review._series_quotes(Source(), ['600036.SH', '600519.SH', '300750.SZ'], now))
    assert list(result) == ['600036.SH']
    assert result['600036.SH']['kind'] == '尾盘估算'


def test_daily_change_available_without_start_price(monkeypatch):
    today = datetime.now(review._TZ).date().isoformat()
    monkeypatch.setattr(review, '_historical', lambda *args: ([{'code': '600036.SH'}], {}, {}, 0))
    class QuoteWithoutOpen:
        _client = None
        async def fetch_quotes(self, symbols):
            return [{'symbol': '600036.SH', 'timestamp': datetime.now(timezone.utc),
                     'is_stale': False, 'open_price': None, 'current_price': 11, 'close_price': 10}]
    monkeypatch.setattr(remote_redis_source, 'RemoteRedisDataSource', QuoteWithoutOpen)
    monkeypatch.setattr(review, '_previous_closes', lambda *args: {'600036.SH': 5})
    data = asyncio.run(review.calculate_review({'summary': {'prediction_trade_date': today}}, 10, True, '000300.SH'))
    assert data['stocks'][0]['daily'] == pytest.approx(10)
    assert data['stocks'][0]['cumulative'] is None
