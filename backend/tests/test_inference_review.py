import asyncio
from datetime import datetime, timezone

import pytest

from backend.services.api.services import inference_review as review
from backend.services.stream.market_app.services import remote_redis_source

_REAL_FETCH_BENCHMARK = review._fetch_benchmark_quote


@pytest.fixture(autouse=True)
def no_external_benchmark(monkeypatch):
    async def unavailable(*args):
        return None
    monkeypatch.setattr(review, '_fetch_benchmark_quote', unavailable)


class NoQuotes:
    _client = None

    async def fetch_quotes(self, symbols, **kwargs):
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
    monkeypatch.setattr(review, '_previous_closes', lambda *args: {})
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
        async def fetch_quotes(self, symbols, **kwargs):
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


def test_series_quotes_retains_last_quote_but_rejects_future():
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
    assert set(result) == {'600036.SH', '600519.SH'}
    assert result['600036.SH']['kind'] == '最近行情估算'


def test_daily_change_available_without_start_price(monkeypatch):
    today = datetime.now(review._TZ).date().isoformat()
    monkeypatch.setattr(review, '_historical', lambda *args: ([{'code': '600036.SH'}], {}, {}, 0))
    class QuoteWithoutOpen:
        _client = None
        async def fetch_quotes(self, symbols, **kwargs):
            return [{'symbol': '600036.SH', 'timestamp': datetime.now(timezone.utc),
                     'is_stale': False, 'open_price': None, 'current_price': 11, 'close_price': 10}]
    monkeypatch.setattr(remote_redis_source, 'RemoteRedisDataSource', QuoteWithoutOpen)
    monkeypatch.setattr(review, '_previous_closes', lambda *args: {'600036.SH': 5})
    data = asyncio.run(review.calculate_review({'summary': {'prediction_trade_date': today}}, 10, True, '000300.SH'))
    assert data['stocks'][0]['daily'] == pytest.approx(10)
    assert data['stocks'][0]['cumulative'] is None


def test_today_partial_daily_uses_redis_only_for_missing_stocks(monkeypatch):
    from datetime import datetime, timezone
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 6, 14, 0, tzinfo=review._TZ).astimezone(tz or timezone.utc)
    now = FixedDateTime.now(timezone.utc)
    stocks = [{'code': '600036.SH'}, {'code': '600519.SH'}]
    history = {
        '600036.SH': {'2026-11-05': {'open': 10, 'close': 10}, '2026-11-06': {'open': 10, 'close': 12}},
        '600519.SH': {'2026-11-05': {'open': 20, 'close': 20}},
    }
    index = {'2026-11-05': {'open': 100, 'close': 101}, '2026-11-06': {'open': 101, 'close': 102}}
    monkeypatch.setattr(review, 'datetime', FixedDateTime)
    monkeypatch.setattr(review, '_historical', lambda *args: (stocks, history, index, 0))
    class Quotes:
        _client = None
        async def fetch_quotes(self, symbols, **kwargs):
            return [
                {'symbol':'600036.SH', 'timestamp':now, 'is_stale':False, 'open_price':10, 'current_price':99, 'close_price':10},
                {'symbol':'600519.SH', 'timestamp':now, 'is_stale':False, 'open_price':20, 'current_price':22, 'close_price':20},
            ]
    monkeypatch.setattr(remote_redis_source, 'RemoteRedisDataSource', Quotes)
    monkeypatch.setattr(review, '_previous_closes', lambda *args: {'600036.SH':10, '600519.SH':20})
    data = asyncio.run(review.calculate_review({'summary': {'prediction_trade_date': '2026-11-05'}}, 10, True, '000300.SH'))
    latest = data['days'][-1]
    assert latest['date'] == '2026-11-06'
    assert latest['cumulative'] == pytest.approx(15)
    assert latest['coverage'] == 2
    assert latest['benchmark'] == pytest.approx(2)
    assert latest['source'] == '日线+实时'
    assert data['stocks'][0]['latest'] == 12
    assert data['stocks'][0]['source'] == '日线'
    assert data['stocks'][1]['latest'] == 22
    assert data['stocks'][1]['source'] == '实时'


def test_today_partial_daily_does_not_fill_missing_with_wrong_date(monkeypatch):
    from datetime import datetime, timezone, timedelta
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 6, 14, 0, tzinfo=review._TZ).astimezone(tz or timezone.utc)
    now = FixedDateTime.now(timezone.utc)
    stocks = [{'code': '600036.SH'}, {'code': '600519.SH'}]
    history = {
        '600036.SH': {'2026-11-05': {'open':10, 'close':10}, '2026-11-06': {'open':10, 'close':12}},
        '600519.SH': {'2026-11-05': {'open':20, 'close':20}},
    }
    monkeypatch.setattr(review, 'datetime', FixedDateTime)
    monkeypatch.setattr(review, '_historical', lambda *args: (stocks, history, {}, 0))
    class YesterdayQuote:
        _client = None
        async def fetch_quotes(self, symbols, **kwargs):
            return [{'symbol':'600519.SH','timestamp':now-timedelta(days=1),'is_stale':False,'open_price':20,'current_price':22,'close_price':20}]
    monkeypatch.setattr(remote_redis_source, 'RemoteRedisDataSource', YesterdayQuote)
    monkeypatch.setattr(review, '_series_quotes', lambda *args: asyncio.sleep(0, result={}))
    data = asyncio.run(review.calculate_review({'summary': {'prediction_trade_date': '2026-11-05'}}, 10, True, '000300.SH'))
    assert data['days'][-1]['cumulative'] is None
    assert data['days'][-1]['coverage'] == 1
    assert data['stocks'][1]['source'] == '缺失'


def test_missing_adjustment_basis_does_not_mix_raw_redis_with_qfq(monkeypatch):
    from datetime import datetime, timezone
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 6, 14, 0, tzinfo=review._TZ).astimezone(tz or timezone.utc)
    now = FixedDateTime.now(timezone.utc)
    stocks = [{'code': '600036.SH'}, {'code': '600519.SH'}]
    history = {
        '600036.SH': {'2026-11-05': {'open':10, 'close':10}, '2026-11-06': {'open':10, 'close':12}},
        '600519.SH': {'2026-11-05': {'open':20, 'close':20}},
    }
    monkeypatch.setattr(review, 'datetime', FixedDateTime)
    monkeypatch.setattr(review, '_historical', lambda *args: (stocks, history, {}, 0))
    class CurrentQuote:
        _client = None
        async def fetch_quotes(self, symbols, **kwargs):
            return [{'symbol':'600519.SH','timestamp':now,'is_stale':False,'open_price':20,'current_price':22,'close_price':20}]
    monkeypatch.setattr(remote_redis_source, 'RemoteRedisDataSource', CurrentQuote)
    monkeypatch.setattr(review, '_previous_closes', lambda *args: {})
    monkeypatch.setattr(review, '_series_quotes', lambda *args: asyncio.sleep(0, result={}))
    data = asyncio.run(review.calculate_review({'summary': {'prediction_trade_date': '2026-11-05'}}, 10, True, '000300.SH'))
    assert data['days'][-1]['cumulative'] is None
    assert data['days'][-1]['coverage'] == 1
    assert data['stocks'][1]['cumulative'] is None


def test_historical_daily_uses_previous_session_not_previous_available_quote(monkeypatch):
    stocks = [{'code': '600036.SH'}, {'code': '600519.SH'}]
    history = {
        '600036.SH': {'2026-11-05': {'open': 10, 'close': 10}, '2026-11-06': {'open': 10, 'close': 12}},
        '600519.SH': {'2026-11-04': {'open': 20, 'close': 20}, '2026-11-06': {'open': 20, 'close': 22}},
    }
    calls = []
    def previous(symbols, day, adjust):
        calls.append((symbols, day, adjust))
        return {}
    monkeypatch.setattr(review, '_historical', lambda *args: (stocks, history, {}, 0))
    monkeypatch.setattr(review, '_previous_closes', previous)
    monkeypatch.setattr(remote_redis_source, 'RemoteRedisDataSource', NoQuotes)
    data = asyncio.run(review.calculate_review({'summary': {'prediction_trade_date': '2026-11-04'}}, 10, False, '000300.SH'))
    assert data['stocks'][0]['daily'] == pytest.approx(20)
    assert data['stocks'][1]['daily'] is None
    assert calls == [(['600519.SH'], '2026-11-05', 'qfq')]


def test_first_historical_day_fetches_previous_close(monkeypatch):
    stocks = [{'code': '600036.SH'}]
    history = {'600036.SH': {'2026-11-06': {'open': 10, 'close': 11}}}
    monkeypatch.setattr(review, '_historical', lambda *args: (stocks, history, {}, 0))
    monkeypatch.setattr(review, '_previous_closes', lambda symbols, day, adjust: {'600036.SH': 10} if day == '2026-11-05' and adjust == 'qfq' else {})
    monkeypatch.setattr(remote_redis_source, 'RemoteRedisDataSource', NoQuotes)
    data = asyncio.run(review.calculate_review({'summary': {'prediction_trade_date': '2026-11-06'}}, 10, False, '000300.SH'))
    assert data['stocks'][0]['daily'] == pytest.approx(10)


def test_last_quote_is_kept_across_midnight_until_a_newer_day(monkeypatch):
    from datetime import timedelta
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 7, 0, 1, tzinfo=review._TZ).astimezone(tz or timezone.utc)
    stamp = FixedDateTime.now(timezone.utc) - timedelta(hours=9)
    monkeypatch.setattr(review, 'datetime', FixedDateTime)
    monkeypatch.setattr(review, '_historical', lambda *args: ([{'code': '600036.SH'}], {}, {}, 0))
    class OldQuote:
        _client = None
        async def fetch_quotes(self, symbols, **kwargs):
            return [{'symbol':'600036.SH', 'timestamp':stamp, 'is_stale':True, 'open_price':10, 'current_price':11, 'close_price':10}]
    monkeypatch.setattr(remote_redis_source, 'RemoteRedisDataSource', OldQuote)
    monkeypatch.setattr(review, '_previous_closes', lambda *args: {'600036.SH': 10})
    data = asyncio.run(review.calculate_review({'summary': {'prediction_trade_date': '2026-11-06'}}, 10, False, '000300.SH'))
    assert data['days'][0]['date'] == '2026-11-06'
    assert data['stocks'][0]['daily'] == pytest.approx(10)
    assert data['quote_time'] == stamp
    assert data['stocks'][0]['source'] == '最近行情'


def test_benchmark_falls_back_to_matching_tencent_quote(monkeypatch):
    stocks = [{'code': '600036.SH'}]
    history = {'600036.SH': {'2026-11-06': {'open': 10, 'close': 11}}}
    monkeypatch.setattr(review, '_historical', lambda *args: (stocks, history, {}, 0))
    monkeypatch.setattr(review, '_previous_closes', lambda *args: {})
    monkeypatch.setattr(remote_redis_source, 'RemoteRedisDataSource', NoQuotes)
    async def quote(*args):
        return {'timestamp': datetime(2026, 11, 6, 15, tzinfo=review._TZ), 'price': 110, 'open': 100}
    monkeypatch.setattr(review, '_fetch_benchmark_quote', quote)
    data = asyncio.run(review.calculate_review({'summary': {'prediction_trade_date': '2026-11-06'}}, 10, False, '000300.SH'))
    assert data['days'][-1]['benchmark'] == pytest.approx(10)
    assert data['days'][-1]['benchmark_source'] == '腾讯财经'
    assert data['days'][-1]['benchmark_time'] == '2026-11-06T15:00:00+08:00'


def test_benchmark_does_not_use_wrong_date_or_missing_target_open(monkeypatch):
    stocks = [{'code': '600036.SH'}]
    history = {'600036.SH': {'2026-11-06': {'open': 10, 'close': 11}}}
    monkeypatch.setattr(review, '_historical', lambda *args: (stocks, history, {}, 0))
    monkeypatch.setattr(review, '_previous_closes', lambda *args: {})
    monkeypatch.setattr(remote_redis_source, 'RemoteRedisDataSource', NoQuotes)
    async def wrong_date(*args):
        return {'timestamp': datetime(2026, 11, 5, 15, tzinfo=review._TZ), 'price': 110, 'open': 100}
    monkeypatch.setattr(review, '_fetch_benchmark_quote', wrong_date)
    data = asyncio.run(review.calculate_review({'summary': {'prediction_trade_date': '2026-11-05'}}, 10, False, '000300.SH'))
    assert data['days'][-1]['benchmark'] is None
    assert data['benchmark_unavailable_reason'] == '指数与组合行情日期不一致'
    async def matching(*args):
        return {'timestamp': datetime(2026, 11, 6, 15, tzinfo=review._TZ), 'price': 110, 'open': 100}
    monkeypatch.setattr(review, '_fetch_benchmark_quote', matching)
    data = asyncio.run(review.calculate_review({'summary': {'prediction_trade_date': '2026-11-05'}}, 10, False, '000300.SH'))
    assert data['days'][-1]['benchmark'] is None
    assert data['benchmark_unavailable_reason'] == '目标日指数开盘价缺失'


def test_benchmark_prefers_quantdb_and_skips_tencent(monkeypatch):
    stocks = [{'code': '600036.SH'}]
    history = {'600036.SH': {'2026-11-06': {'open': 10, 'close': 11}}}
    index = {'2026-11-06': {'open': 100, 'close': 102}}
    monkeypatch.setattr(review, '_historical', lambda *args: (stocks, history, index, 0))
    monkeypatch.setattr(review, '_previous_closes', lambda *args: {})
    monkeypatch.setattr(remote_redis_source, 'RemoteRedisDataSource', NoQuotes)
    async def fail(*args):
        raise AssertionError('QuantDB should take precedence')
    monkeypatch.setattr(review, '_fetch_benchmark_quote', fail)
    data = asyncio.run(review.calculate_review({'summary': {'prediction_trade_date': '2026-11-06'}}, 10, False, '000300.SH'))
    assert data['days'][-1]['benchmark'] == pytest.approx(2)
    assert data['days'][-1]['benchmark_source'] == 'QuantDB 日线'


def test_tencent_quote_fetch_keeps_last_valid_on_failure(monkeypatch):
    import aiohttp
    review._benchmark_cache.clear()
    stamp = datetime(2026, 11, 9, 12, 30, tzinfo=review._TZ)
    fields = [''] * 31
    fields[3], fields[5], fields[30] = '110', '100', '20261109113000'
    class Response:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def raise_for_status(self): pass
        async def read(self): return ('v_sh000300="' + '~'.join(fields) + '";').encode('gb18030')
    class Session:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def get(self, url): return Response()
    monkeypatch.setattr(aiohttp, 'ClientSession', Session)
    original = _REAL_FETCH_BENCHMARK
    first = asyncio.run(original('000300.SH', stamp))
    assert first['price'] == 110 and first['open'] == 100
    class FailedSession(Session):
        def get(self, url): raise aiohttp.ClientConnectionError()
    monkeypatch.setattr(aiohttp, 'ClientSession', FailedSession)
    assert asyncio.run(original('000300.SH', stamp)) == first
    review._benchmark_cache.clear()


def test_quantdb_close_wins_when_tencent_supplies_target_open(monkeypatch):
    stocks = [{'code': '600036.SH'}]
    history = {'600036.SH': {'2026-11-06': {'open': 10, 'close': 11}}}
    index = {'2026-11-06': {'open': None, 'close': 105}}
    monkeypatch.setattr(review, '_historical', lambda *args: (stocks, history, index, 0))
    monkeypatch.setattr(review, '_previous_closes', lambda *args: {})
    monkeypatch.setattr(remote_redis_source, 'RemoteRedisDataSource', NoQuotes)
    async def quote(*args):
        return {'timestamp': datetime(2026, 11, 6, 15, tzinfo=review._TZ), 'price': 110, 'open': 100}
    monkeypatch.setattr(review, '_fetch_benchmark_quote', quote)
    data = asyncio.run(review.calculate_review({'summary': {'prediction_trade_date': '2026-11-06'}}, 10, False, '000300.SH'))
    assert data['days'][-1]['benchmark'] == pytest.approx(5)
    assert data['days'][-1]['benchmark_source'] == 'QuantDB 日线，腾讯开盘价'
