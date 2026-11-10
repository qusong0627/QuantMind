"""推理落库性能修复（2026-10-13）单元测试。

覆盖三条改动，每条都直接对着「原来的错误行为」断言：

1. 行情取价：键走 ``snapshot_key()`` 契约（`market:snapshot:{prefix.lower()}`），
   **不是**已退役的 `stock:{code}.SH`；且 Step 2/Step 3 共用一次批量查询（原来逐行查两遍）。
2. 保留期 DELETE：先跑索引友好的存在性探针，未命中即整体短路（原来每次付全表顺序扫）。
3. 失效锁 `_INFER_PERSIST_LOCK` 已移除（进程内锁在 prefork 下无跨进程语义）。

实测背景（容器内取证，见 docs/推理落库慢_与_成交台账双写_修复细案.md）：
远端 `hget` 46 ms × 3206 只 × 2 遍 ≈ 296 s；键名错（`stock:*` 远端 n=0）**叠加**
连接漏传 db（连到 db0，写侧全落 db3），故两遍查询历史上从未取到任何值。两条
都必须钉住——只修键名仍会因 db 错而恒查空（2026-10-13 补测证实）。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.services.engine.inference.script_runner import InferenceScriptRunner

_BACKEND = Path(__file__).resolve().parents[1]


# ── 1. 行情取价：契约键 + 批量 ────────────────────────────────────────


class _FakePipeline:
    def __init__(self, store: dict[str, dict[str, str]], sink: list[str]):
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
    def __init__(self, store: dict[str, dict[str, str]]):
        self.store = store
        self.issued_keys: list[str] = []
        self.pipeline_calls = 0

    def pipeline(self, transaction: bool = True):
        assert transaction is False, "批量取价必须用非事务 pipeline（逐键事务无收益）"
        self.pipeline_calls += 1
        return _FakePipeline(self.store, self.issued_keys)

    def close(self):
        pass


@pytest.mark.unit
def test_fetch_expected_prices_uses_snapshot_contract_keys():
    """键必须是 market:snapshot:{prefix.lower()}，绝不能是 stock:{code}.SH。"""
    redis = _FakeRedis(
        {
            "market:snapshot:sh600036": {"Now": "42.77"},
            "market:snapshot:sz000001": {"Now": "11.64"},
        }
    )
    out = InferenceScriptRunner._fetch_expected_prices(
        redis, ["600036", "000001", "300750"], ["SH600036", "SZ000001", "SZ300750"]
    )

    assert out == {"600036": 42.77, "000001": 11.64}
    assert "300750" not in out, "未命中的不写 0，也不进表（诚实缺失）"
    assert all(k.startswith("market:snapshot:") for k in redis.issued_keys)
    assert "market:snapshot:600036.SH" in redis.issued_keys, (
        "必须发后缀式键（远端行情服写侧实测 5563/5563 是该形）"
    )
    assert not any(k.startswith("stock:") for k in redis.issued_keys), (
        "stock:{code}.SH 是已退役命名空间（远端 n=0），出现即为回退"
    )


@pytest.mark.unit
def test_fetch_expected_prices_is_one_pipeline_for_many_symbols():
    """3206 只应走一次 pipeline（分片内），而不是 3206 次逐键往返。"""
    syms = [f"{600000 + i:06d}" for i in range(500)]
    store = {f"market:snapshot:sh{s}": {"Now": "10.0"} for s in syms}
    redis = _FakeRedis(store)

    out = InferenceScriptRunner._fetch_expected_prices(redis, syms, None)

    assert len(out) == 500
    # 500 只 / 分片 200 → 3 次 pipeline（不是 500 次单键往返）；
    # read_snapshots 每只发后缀+前缀两套键 → 500 × 2 条命令。
    assert redis.pipeline_calls == 3
    assert len(redis.issued_keys) == 1000


@pytest.mark.unit
def test_fetch_expected_prices_connection_failure_returns_empty(caplog):
    """取数失败 → 空表 + WARNING 留痕（原实现是 debug/静默 pass）。"""

    class _Boom:
        def pipeline(self, transaction: bool = True):
            raise RuntimeError("remote quote down")

        def close(self):
            pass

    with caplog.at_level("WARNING"):
        out = InferenceScriptRunner._fetch_expected_prices(
            _Boom(), ["600036"], ["SH600036"]
        )

    assert out == {}
    assert any("批量取价失败" in r.message for r in caplog.records)


@pytest.mark.unit
def test_fetch_expected_prices_no_redis_is_empty():
    assert InferenceScriptRunner._fetch_expected_prices(None, ["600036"], None) == {}


@pytest.mark.unit
def test_fetch_expected_prices_skips_unrecognizable_symbols():
    """认不出的形态（港股/美股/日期串）不猜键，直接不进查询。"""
    redis = _FakeRedis({})
    InferenceScriptRunner._fetch_expected_prices(
        redis, ["00700.HK"], ["00700.HK"]
    )
    # snapshot_key("00700.HK") → market:snapshot:0700.hk（大写后缀式，非 A 股契约）；
    # 该形态不在候选内，键面必须仍是 market:snapshot:*，且不含 stock:
    assert all(k.startswith("market:snapshot:") for k in redis.issued_keys)


@pytest.mark.unit
def test_quote_redis_connection_passes_resolved_db(monkeypatch):
    """行情连接必须透传解析出的 ``db``。

    写侧（tdx_aidata collector/worker、能力席位、桥）全部落 db3；连接漏传
    ``db=`` 会连到默认 db0（实测空集）→ 键名再对也恒查空。这是与「错键名」
    并列的第二条静默失败根因，必须有测试钉住。
    """
    from backend.shared import regime_daily_store, remote_quote_config

    monkeypatch.setattr(regime_daily_store, "load_day_state", lambda *a, **k: None)
    monkeypatch.setattr(
        remote_quote_config,
        "resolve_remote_quote_redis",
        lambda: ("quote-host", 6379, "pw", 3),
    )
    captured: dict = {}

    class _CapturingRedis:
        def __init__(self, *a, **k):
            captured.update(k)
            raise RuntimeError("captured — stop before ping")

    monkeypatch.setattr("redis.Redis", _CapturingRedis)
    _persist(_RecordingSession(probe_rows=[]))

    assert captured.get("host") == "quote-host"
    assert captured.get("db") == 3, "行情连接必须落写侧同一个 db（漏传 → db0 恒查空）"


# ── 2. 保留期：存在性探针短路 ────────────────────────────────────────


class _RecordingSession:
    """同步假会话：记录全部 execute，结果可编排。"""

    def __init__(self, probe_rows: list = None):
        self.calls: list[tuple[str, dict]] = []
        self.commits = 0
        self._probe_rows = probe_rows or []

    def execute(self, sql, params=None):
        s = str(sql)
        self.calls.append((s, dict(params or {})))
        rows = self._probe_rows if "LIMIT 1" in s else []
        return SimpleNamespace(
            rowcount=1,
            fetchone=lambda: (rows[0] if rows else None),
            fetchall=lambda: [],
        )

    def commit(self):
        self.commits += 1

    def deletes(self, table: str) -> list[str]:
        """只取**保留期** DELETE（谓词含 retention_floor）。

        写库事务里还有一条按当日 run 的 DELETE（`trade_date = :trade_date`），
        与保留期无关，不能混进断言。
        """
        return [
            s
            for s, p in self.calls
            if f"DELETE FROM {table}" in s and "retention_floor" in p
        ]


def _persist(db, market: str = "A"):
    runner = InferenceScriptRunner(primary_model_dir="/nonexistent")
    runner._persist_locked(
        db=db,
        run_id="run_perf_1",
        prediction_trade_date="2026-10-13",
        tenant_id="t",
        user_id="u",
        signals=[{"symbol": "600036", "score": 1.0}],
        symbols=["600036"],
        scores=[1.0],
        feature_dim=32,
        model_name="model_test",
        feature_version="v1",
        inference_date="2026-10-12",
        signal_sides=["BUY"],
        market=market,
    )


@pytest.fixture()
def _no_redis(monkeypatch):
    class _DeadRedis:
        def __init__(self, *a, **k):
            raise RuntimeError("no redis in test")

    monkeypatch.setattr("redis.Redis", _DeadRedis)


@pytest.mark.unit
def test_retention_short_circuits_when_probe_misses(monkeypatch, _no_redis):
    """探针未命中 → 不执行任何保留期 DELETE（原来每次都跑）。"""
    from backend.shared import regime_daily_store

    monkeypatch.setattr(
        regime_daily_store, "load_day_state", lambda *a, **k: None
    )
    db = _RecordingSession(probe_rows=[])  # 探针返回 None
    _persist(db)

    assert db.deletes("engine_signal_scores") == []
    assert db.deletes("engine_feature_runs") == []
    # 探针本身必须跑（两处）
    assert sum(1 for s, _ in db.calls if "LIMIT 1" in s) == 2


@pytest.mark.unit
def test_retention_deletes_when_probe_hits(monkeypatch, _no_redis):
    """探针命中 → 照原样执行 DELETE（语义不变）。"""
    from backend.shared import regime_daily_store

    monkeypatch.setattr(
        regime_daily_store, "load_day_state", lambda *a, **k: None
    )
    db = _RecordingSession(probe_rows=[(1,)])
    _persist(db)

    assert len(db.deletes("engine_signal_scores")) == 1
    assert len(db.deletes("engine_feature_runs")) == 1


@pytest.mark.unit
def test_retention_probe_failure_falls_back_to_delete(monkeypatch, _no_redis):
    """探针自身故障 → 保守回退为直接 DELETE（宁可白跑，不可漏清）。"""
    from backend.shared import regime_daily_store

    monkeypatch.setattr(
        regime_daily_store, "load_day_state", lambda *a, **k: None
    )

    class _ProbeBoom(_RecordingSession):
        def execute(self, sql, params=None):
            if "LIMIT 1" in str(sql):
                self.calls.append((str(sql), dict(params or {})))
                raise RuntimeError("probe exploded")
            return super().execute(sql, params)

    db = _ProbeBoom()
    _persist(db)
    assert len(db.deletes("engine_signal_scores")) == 1
    assert len(db.deletes("engine_feature_runs")) == 1


@pytest.mark.unit
def test_retention_probe_uses_retention_floor_param(monkeypatch, _no_redis):
    """探针谓词与 DELETE 同参（同一 retention_floor），否则短路判据失真。"""
    from backend.shared import regime_daily_store

    monkeypatch.setattr(
        regime_daily_store, "load_day_state", lambda *a, **k: None
    )
    db = _RecordingSession(probe_rows=[])
    _persist(db)

    probes = [p for s, p in db.calls if "LIMIT 1" in s]
    assert probes, "必须有探针"
    for p in probes:
        assert p["retention_floor"] == str(
            date(2026, 10, 13) - __import__("datetime").timedelta(days=730)
        )


# ── 3. 失效锁已移除 ─────────────────────────────────────────────────


@pytest.mark.unit
def test_no_process_local_persist_lock():
    """进程内 threading.Lock 在 celery prefork 下无跨进程语义，不得回退。"""
    src = (
        _BACKEND / "services/engine/inference/script_runner.py"
    ).read_text(encoding="utf-8")
    assert "_INFER_PERSIST_LOCK = " not in src, "失效的进程内锁不得复活"
    assert "with _INFER_PERSIST_LOCK:" not in src
    # 真正的跨进程互斥是 Redis 锁（inference_lock），必须在 execute 层保留
    assert "from backend.shared.inference_lock import" in src
