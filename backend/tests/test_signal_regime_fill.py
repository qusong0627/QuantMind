"""P3 §6.4：``engine_signal_scores.regime`` 填当日生效值（两条写侧）+ 缺行 NULL 诚实。

- 存储层 ``load_day_state`` / ``load_day_state_async``：单日直读、缺行 None、边界日期转换；
- 推理脚本写库（``script_runner._persist_locked``）：INSERT 参数 = qm_regime_daily 当日值；
  表外市场 / 读失败 → NULL（不再写 ``'normal'`` 撒谎，已核 0 消费方）；
- realtime signal-ready（``realtime_contract.mark_signal_ready``）：缺省派生、显式值优先、
  按 (market, trade_date) 单次读库。
"""

from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace

import pytest

from backend.shared.regime_daily_store import load_day_state, load_day_state_async


class _OneRowSession:
    """同步假会话：execute 记录 (sql, params)，fetchone 返回预设行。"""

    def __init__(self, row):
        self._row = row
        self.calls: list[tuple[str, dict]] = []

    def execute(self, sql, params=None):
        self.calls.append((str(sql), dict(params or {})))
        return SimpleNamespace(fetchone=lambda: self._row)


class _AsyncOneRowSession(_OneRowSession):
    async def execute(self, sql, params=None):  # type: ignore[override]
        return super().execute(sql, params)


# ── 存储层单日直读 ──────────────────────────────────────────────────


@pytest.mark.unit
def test_load_day_state_returns_state_and_converts_date():
    session = _OneRowSession(("bear",))
    state = load_day_state(session, "CN", "2026-10-12")
    assert state == "bear"
    sql, params = session.calls[0]
    assert "qm_regime_daily" in sql
    assert params["market"] == "CN"
    assert params["trade_date"] == date(2026, 10, 12)  # str → date（asyncpg 边界）


@pytest.mark.unit
@pytest.mark.parametrize("row", [None, (None,)])
def test_load_day_state_missing_row_is_none(row):
    assert load_day_state(_OneRowSession(row), "CN", date(2026, 10, 12)) is None


@pytest.mark.unit
def test_load_day_state_async_parity():
    session = _AsyncOneRowSession(("bull",))
    state = asyncio.run(load_day_state_async(session, "HK", date(2026, 10, 12)))
    assert state == "bull"
    sql, params = session.calls[0]
    assert ":market" in sql and params["market"] == "HK"


# ── 推理脚本写库（script_runner._persist_locked）────────────────────


class _RecordingSession:
    """同步假会话（script_runner 用）：记录全部 execute；结果对象惰性空。"""

    rowcount = 1

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.commits = 0

    def execute(self, sql, params=None):
        self.calls.append((str(sql), dict(params or {})))
        return SimpleNamespace(rowcount=1, fetchone=lambda: None, fetchall=lambda: [])

    def commit(self):
        self.commits += 1

    def score_inserts(self) -> list[tuple[str, dict]]:
        return [
            (s, p) for s, p in self.calls if "INSERT INTO engine_signal_scores" in s
        ]


def _persist(db, market: str = "A"):
    from backend.services.engine.inference.script_runner import InferenceScriptRunner

    runner = InferenceScriptRunner(primary_model_dir="/nonexistent")
    runner._persist_locked(
        db=db,
        run_id="run_test_regime_1",
        prediction_trade_date="2026-10-12",
        tenant_id="t",
        user_id="u",
        signals=[{"symbol": "600036", "score": 1.0}],
        symbols=["600036"],
        scores=[1.0],
        feature_dim=32,
        model_name="model_test",
        feature_version="v1",
        inference_date="2026-10-10",
        signal_sides=["BUY"],
        market=market,
    )


@pytest.fixture()
def _no_redis(monkeypatch):
    """quote Redis 连接失败（quote_redis=None）——测试不碰网络。"""

    class _DeadRedis:
        def __init__(self, *a, **k):
            raise RuntimeError("no redis in test")

    monkeypatch.setattr("redis.Redis", _DeadRedis)


@pytest.mark.unit
def test_persist_fills_regime_from_daily_table(monkeypatch, _no_redis):
    from backend.shared import regime_daily_store

    stored: list[tuple[str, object]] = []

    def _fake_load(session, market, trade_date):
        stored.append((market, trade_date))
        return "bear"

    monkeypatch.setattr(regime_daily_store, "load_day_state", _fake_load)
    db = _RecordingSession()
    _persist(db, market="A")

    inserts = db.score_inserts()
    assert inserts, "必须写到 engine_signal_scores"
    sql, params = inserts[0]
    assert ":regime" in sql and "'normal'" not in sql
    assert params["regime"] == "bear"
    assert params["market"] == "CN"
    # 读的是 (CN, 预测交易日) 的生效值
    assert stored == [("CN", date(2026, 10, 12))]


@pytest.mark.unit
def test_persist_offtable_market_writes_null(monkeypatch, _no_redis):
    from backend.shared import regime_daily_store

    def _must_not_call(session, market, trade_date):  # pragma: no cover
        raise AssertionError("表外市场不应读 regime 表")

    monkeypatch.setattr(regime_daily_store, "load_day_state", _must_not_call)
    db = _RecordingSession()
    _persist(db, market="CRYPTO")

    _, params = db.score_inserts()[0]
    assert params["regime"] is None
    assert params["market"] == "CRYPTO"


@pytest.mark.unit
def test_persist_regime_read_failure_writes_null(monkeypatch, _no_redis):
    from backend.shared import regime_daily_store

    def _boom(session, market, trade_date):
        raise RuntimeError("db down")

    monkeypatch.setattr(regime_daily_store, "load_day_state", _boom)
    db = _RecordingSession()
    _persist(db, market="A")  # 不抛出（告警 + NULL）

    _, params = db.score_inserts()[0]
    assert params["regime"] is None


# ── realtime signal-ready ───────────────────────────────────────────


class _AsyncRecordingSession:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, sql, params=None):
        self.calls.append((str(sql), dict(params or {})))
        return SimpleNamespace(rowcount=1)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def score_inserts(self) -> list[tuple[str, dict]]:
        return [
            (s, p) for s, p in self.calls if "INSERT INTO engine_signal_scores" in s
        ]


def _signal_ready(scores: list, trade_date=date(2026, 10, 12)):
    from backend.services.engine.routers import realtime_contract as rc

    payload = rc.SignalReadyRequest(
        tenant_id="t",
        user_id="u",
        trade_date=trade_date,
        model_version="mv1",
        feature_version="fv1",
        scores=scores,
    )
    return asyncio.run(rc.mark_signal_ready("run_rt_1", payload))


@pytest.fixture()
def _wire_realtime(monkeypatch):
    def _wire(db_session):
        from backend.services.engine.routers import realtime_contract as rc
        from backend.shared import signal_contract

        async def _noop():
            return None

        monkeypatch.setattr(rc, "get_session", lambda read_only=False: db_session)
        monkeypatch.setattr(
            signal_contract, "ensure_signal_contract_columns_async", _noop
        )
        return rc

    return _wire


@pytest.mark.unit
def test_signal_ready_derives_regime_and_caches_per_day(monkeypatch, _wire_realtime):
    from backend.services.engine.routers import realtime_contract as rc
    from backend.shared import regime_daily_store

    reads: list[tuple[str, object]] = []

    async def _fake_load(session, market, trade_date):
        reads.append((market, trade_date))
        return "bull"

    monkeypatch.setattr(regime_daily_store, "load_day_state_async", _fake_load)
    db = _AsyncRecordingSession()
    _wire_realtime(db)

    _signal_ready(
        [
            rc.SignalScoreItem(symbol="SH600036", fusion_score=1.0),
            rc.SignalScoreItem(symbol="SH600000", fusion_score=0.9),
        ],
    )

    inserts = db.score_inserts()
    assert len(inserts) == 2
    assert all(p["regime"] == "bull" for _, p in inserts)
    assert reads == [("CN", date(2026, 10, 12))]  # 同批只读一次库


@pytest.mark.unit
def test_signal_ready_explicit_regime_wins_and_offtable_null(
    monkeypatch, _wire_realtime
):
    from backend.services.engine.routers import realtime_contract as rc
    from backend.shared import regime_daily_store

    async def _must_not_call(session, market, trade_date):  # pragma: no cover
        raise AssertionError("显式给值 / 表外市场都不应读库")

    monkeypatch.setattr(regime_daily_store, "load_day_state_async", _must_not_call)
    db = _AsyncRecordingSession()
    _wire_realtime(db)

    _signal_ready(
        [
            rc.SignalScoreItem(symbol="SH600036", fusion_score=1.0, regime="custom"),
            rc.SignalScoreItem(
                symbol="BTCUSDT", fusion_score=0.9, market="CRYPTO", regime=None
            ),
        ],
    )

    inserts = db.score_inserts()
    assert [p["regime"] for _, p in inserts] == ["custom", None]


@pytest.mark.unit
def test_signal_ready_regime_read_failure_writes_null(monkeypatch, _wire_realtime):
    from backend.services.engine.routers import realtime_contract as rc
    from backend.shared import regime_daily_store

    async def _boom(session, market, trade_date):
        raise RuntimeError("db down")

    monkeypatch.setattr(regime_daily_store, "load_day_state_async", _boom)
    db = _AsyncRecordingSession()
    _wire_realtime(db)

    _signal_ready([rc.SignalScoreItem(symbol="SH600036", fusion_score=1.0)])
    assert db.score_inserts()[0][1]["regime"] is None  # 不阻断，NULL 诚实
