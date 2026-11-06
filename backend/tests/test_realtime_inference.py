"""T-P6-08 热集实时推理服务测试：周期构建（假注入）/门控降级/真库发布闭环。

覆盖：
1. U：build_cycle——注入假热集/快照/基线 + 真实 sklearn 小模型（临时目录）→
   载荷字段/排名/覆盖白名单生效（开与关得分不同）；ONNX 按需导出走导出链；
2. D：未启用 → None；模型目录缺 metadata → 明确异常；
3. I：默认发布路径（同进程直调契约端点）→ engine_feature_runs=signal_ready +
   engine_signal_scores(source=realtime) 逐行断言 → 清理。
"""

from __future__ import annotations

import json
import pickle
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

_CST = timezone(timedelta(hours=8))


def _no_status_write(payload) -> None:
    """**每条 tick_once 用例都必须注入**：默认落点是生产状态镜像
    `qm:realtime:infer:status`（`_default_status_write` → hset），而 tick_once 的
    finally 每轮都会写一次。

    2026-10-08 实测：跑本文件的两个 tick 用例后，生产镜像从真身的
    `cycles=48 / model_dir=…catboost_8f24cd5c / cadence=15` 变成测试的
    `cycles=0 / model_dir="" / cadence=3 / last_skip=启用但未配置模型目录`
    —— 面板对着正在正常跑的服务报「配置缺失」（真身 15s 后盖回；服务真停时
    这行假故障会挂满 24h TTL），所以测试不许碰这个键。"""
    return None


def _make_model_dir(tmp_path: Path, n_features: int = 6, *, extra_feature: str | None = None) -> Path:
    """真实 sklearn 模型目录（含 metadata），供导出链与服务使用。"""
    from sklearn.linear_model import Ridge

    rng = np.random.default_rng(3)
    cols = [f"f{i}" for i in range(n_features)]
    cols[0] = "mom_ret_1d"  # 白名单列之一
    if extra_feature:
        cols[-1] = extra_feature  # 影子/覆盖测试用的第二列 TIER 特征
    x = rng.normal(0, 1, (200, n_features))
    y = x @ np.linspace(0.1, 0.6, n_features) + rng.normal(0, 0.01, 200)
    model = Ridge(alpha=1.0).fit(x, y)
    d = tmp_path / "mdl_rt_test"
    d.mkdir()
    with open(d / "model.pkl", "wb") as f:
        pickle.dump(model, f)
    (d / "metadata.json").write_text(
        json.dumps(
            {
                "framework": "sklearn",
                "model_file": "model.pkl",
                "feature_columns": cols,
                "fill_values": dict.fromkeys(cols, 0.0),
                "model_version": "rt-test-v1",
                "factor_catalog_version": "test",
            }
        ),
        encoding="utf-8",
    )
    return d


def _cfg(model_dir: Path, *, enabled: bool = True, whitelist: tuple[str, ...] = ()):
    from backend.services.engine.inference.realtime_service import RealtimeInferConfig

    return RealtimeInferConfig(
        enabled=enabled, model_dir=str(model_dir), cadence_s=3,
        override_whitelist=whitelist,
    )


def _history(closes: list[float]):
    import pandas as pd

    n = len(closes)
    return pd.DataFrame(
        {
            "symbol": ["600036.SH"] * n,
            "trade_date": pd.date_range("2026-08-01", periods=n, freq="B"),
            "open": closes, "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes], "close": closes,
            "volume": [1e6] * n, "amount": [1e7] * n,
        }
    )


def _fake_engine_inputs(price: float = 12.0):
    hot = ["600036.SH", "000001.SZ"]
    snaps = {
        "600036.SH": {
            "Now": str(price), "Open": "11.0", "High": str(price), "Low": "10.9",
            "Volume": "100000", "Amount": "1200000", "PreClose": "11.0",
            "timestamp": str(int(datetime.now(tz=_CST).timestamp())),
        },
        "000001.SZ": {
            "Now": "10.5", "Open": "10.4", "High": "10.6", "Low": "10.3",
            "Volume": "80000", "Amount": "840000", "PreClose": "10.4",
            "timestamp": str(int(datetime.now(tz=_CST).timestamp())),
        },
    }
    # 基线键=后缀身份（compute_cycle 身份空间，审计 M2；指数与同数字股票不互串）
    rows = {
        "600036.SH": {"mom_ret_1d": 0.01, "f1": 0.2, "f2": -0.1, "f3": 0.05, "f4": 0.3, "f5": 0.0},
        "000001.SZ": {"mom_ret_1d": -0.02, "f1": 0.1, "f2": 0.4, "f3": -0.2, "f4": 0.0, "f5": 0.2},
    }
    history = {
        "600036.SH": _history([10.0 + 0.04 * i for i in range(25)]),  # 末位≈10.96
        "000001.SZ": _history([10.4] * 25),
    }
    return hot, snaps, {"rows": rows, "history": history}


@pytest.mark.unit
def test_build_cycle_with_override_whitelist(tmp_path):
    from backend.services.engine.inference.realtime_service import RealtimeInferenceService

    model_dir = _make_model_dir(tmp_path)
    hot, snaps, baseline = _fake_engine_inputs(price=12.0)

    def svc(whitelist):
        return RealtimeInferenceService(
            config_loader=lambda: _cfg(model_dir, whitelist=whitelist),
            hot_set_fetcher=lambda: hot,
            snapshot_fetcher=lambda syms: snaps,
            baseline_loader=lambda syms, day: baseline,
            ledger_sink=lambda _e: None,  # 测试绝不写生产账本（Redis）
        )

    p_plain = svc(()).build_cycle()
    assert p_plain is not None
    assert p_plain["run_id"].startswith("rt-mdl_rt_test-")
    assert p_plain["feature_dim"] == 6 and p_plain["ready_symbols"] == 2
    assert {s["symbol"] for s in p_plain["scores"]} == {"600036.SH", "000001.SZ"}
    ranks = sorted(s["score_rank"] for s in p_plain["scores"])
    assert ranks == [1, 2]
    assert p_plain["quality"]["override_whitelist"] == []
    assert p_plain["overridden_cells"] == 0

    p_override = svc(("mom_ret_1d",)).build_cycle()
    assert p_override["overridden_cells"] == 2  # 两只标的 mom_ret_1d 均被 live 覆盖
    assert p_override["quality"]["override_whitelist"] == ["mom_ret_1d"]
    # 覆盖改变输入 → 得分必须变化（证明覆盖真实进入矩阵）
    s_plain = {s["symbol"]: s["fusion_score"] for s in p_plain["scores"]}
    s_over = {s["symbol"]: s["fusion_score"] for s in p_override["scores"]}
    assert any(abs(s_plain[k] - s_over[k]) > 1e-9 for k in s_plain)
    # 000036 的 live mom_ret_1d = 12.0/11.0 - 1 ≈ 0.0909（非基线 0.01）


@pytest.mark.unit
def test_disabled_and_broken_model_dir(tmp_path):
    from backend.services.engine.inference.realtime_service import RealtimeInferenceService

    model_dir = _make_model_dir(tmp_path)
    svc_off = RealtimeInferenceService(config_loader=lambda: _cfg(model_dir, enabled=False))
    assert svc_off.build_cycle() is None

    broken = tmp_path / "broken"
    broken.mkdir()
    svc_bad = RealtimeInferenceService(config_loader=lambda: _cfg(broken))
    with pytest.raises(RuntimeError, match="metadata.json"):
        svc_bad.build_cycle()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_publish_end_to_end_real_db(tmp_path):
    """默认发布路径 → 真库断言：feature_runs=signal_ready + scores(source=realtime) → 清理。"""
    from sqlalchemy import text

    from backend.services.engine.inference.realtime_service import RealtimeInferenceService
    from backend.shared.database_manager_v2 import get_session

    model_dir = _make_model_dir(tmp_path)
    hot, snaps, baseline = _fake_engine_inputs()
    suffix = uuid.uuid4().hex[:8]
    svc = RealtimeInferenceService(
        config_loader=lambda: _cfg(model_dir),
        hot_set_fetcher=lambda: hot,
        snapshot_fetcher=lambda syms: snaps,
        baseline_loader=lambda syms, day: baseline,
        ledger_sink=lambda _e: None,  # 测试绝不写生产账本（Redis）
    )
    payload = svc.build_cycle()
    assert payload is not None
    # 唯一化 run_id/model_version，避免与生产数据互相污染
    payload["run_id"] = f"rt-test-{suffix}"
    payload["model_version"] = f"rt-test-{suffix}"
    cfg = _cfg(model_dir)

    await svc._default_publish(payload, cfg)
    try:
        async with get_session(read_only=True) as db:
            run = (
                await db.execute(
                    text("SELECT status FROM engine_feature_runs WHERE run_id=:r"),
                    {"r": payload["run_id"]},
                )
            ).first()
            assert run is not None and run[0] == "signal_ready"
            rows = (
                await db.execute(
                    text(
                        "SELECT symbol, fusion_score, source, rank_pct, model_version "
                        "FROM engine_signal_scores WHERE run_id=:r ORDER BY symbol"
                    ),
                    {"r": payload["run_id"]},
                )
            ).all()
        assert len(rows) == 2
        assert all(r[2] == "realtime" for r in rows)
        # 落库 symbol=后缀身份（审计 M2：mark_signal_ready 冲突键以它为准，指数不并股票行）
        assert {r[0] for r in rows} == {"600036.SH", "000001.SZ"}
        assert all(0.0 <= float(r[3]) <= 1.0 for r in rows)  # rank_pct 端点现算
    finally:
        async with get_session(read_only=False) as db:
            await db.execute(
                text("DELETE FROM engine_signal_scores WHERE run_id=:r"), {"r": payload["run_id"]}
            )
            await db.execute(
                text("DELETE FROM engine_feature_runs WHERE run_id=:r"), {"r": payload["run_id"]}
            )
        async with get_session(read_only=True) as db:
            left = (
                await db.execute(
                    text("SELECT COUNT(*) FROM engine_signal_scores WHERE run_id=:r"),
                    {"r": payload["run_id"]},
                )
            ).scalar()
        assert int(left or 0) == 0


@pytest.mark.unit
def test_snapshot_key_forms():
    from backend.services.engine.inference.realtime_service import snapshot_key

    assert snapshot_key("600036.SH") == "market:snapshot:sh600036"
    assert snapshot_key("SH600036") == "market:snapshot:sh600036"
    assert snapshot_key("000001.SZ") == "market:snapshot:sz000001"
    assert snapshot_key("300750") == "market:snapshot:sz300750"
    assert snapshot_key("600519") == "market:snapshot:sh600519"
    assert snapshot_key("430047") == "market:snapshot:bj430047"
    assert snapshot_key("BAD!!!") is None


# ── 发布闸门（2026-09-17 收口）：可用实时快照覆盖率不足 → 不发布"伪实时" ──


@pytest.mark.unit
def test_live_coverage_pure_rules():
    import time as _t

    from backend.services.engine.inference.realtime_core import live_coverage

    now = _t.time()
    fresh = {"timestamp": str(int(now - 5))}
    stale_ok = {"timestamp": str(int(now - 120))}   # ≤ stale 线（300s）仍可用
    dead = {"timestamp": str(int(now - 900))}       # > stale 线 → 不可用
    no_ts = {"Now": "10.0"}
    future = {"timestamp": str(int(now + 600))}     # 未来偏斜超容差 → 不可用

    assert live_coverage(["A"], {"A": fresh}, now=now) == 1.0
    assert live_coverage(["A"], {"A": stale_ok}, now=now) == 1.0
    assert live_coverage(["A", "B"], {"A": fresh, "B": dead}, now=now) == 0.5
    assert live_coverage(["A"], {"A": no_ts}, now=now) == 0.0
    assert live_coverage(["A"], {"A": future}, now=now) == 0.0
    assert live_coverage(["A"], {}, now=now) == 0.0
    assert live_coverage([], {}, now=now) == 0.0


@pytest.mark.unit
def test_build_cycle_gated_without_live(tmp_path):
    """零/陈旧快照：不发布（None）+ 计数可见；min_live_coverage=0 时放行（旋钮语义）。"""
    from backend.services.engine.inference.realtime_service import RealtimeInferenceService

    model_dir = _make_model_dir(tmp_path)
    hot, fresh_snaps, bundle = _fake_engine_inputs()
    ledger: list = []

    def _svc(snaps, min_cov):
        cfg = _cfg(model_dir)
        cfg.min_live_coverage = min_cov
        return RealtimeInferenceService(
            config_loader=lambda: cfg,
            hot_set_fetcher=lambda: hot,
            snapshot_fetcher=lambda symbols: snaps,
            baseline_loader=lambda symbols, day: bundle,
            publisher=lambda payload, cfg: None,
            ledger_sink=ledger.append,
        )

    # 零快照 → 闸门拦截
    svc = _svc({}, 0.5)
    assert svc.build_cycle() is None
    assert svc.counters["skipped_no_live"] == 1
    assert "行情未到达" in (svc.counters["last_skip"] or "")
    assert ledger == []  # 账本不落（没有周期发生）

    # 陈旧快照（超 stale 线）→ 同样拦截
    import time as _t

    old_ts = str(int(_t.time() - 900))
    stale = {sym: {**snap, "timestamp": old_ts} for sym, snap in fresh_snaps.items()}
    svc2 = _svc(stale, 0.5)
    assert svc2.build_cycle() is None and svc2.counters["skipped_no_live"] == 1

    # min_live_coverage=0 → 显式放行（旋钮语义：运维明确承担 T-1 基线口径才可关闸门）
    svc3 = _svc({}, 0.0)
    payload = svc3.build_cycle()
    assert payload is not None and payload["scores"]
    assert payload["live_coverage"] == 0.0
    assert ledger and ledger[0]["live_coverage"] == 0.0


@pytest.mark.unit
def test_build_cycle_full_coverage_passes_and_reports(tmp_path):
    from backend.services.engine.inference.realtime_service import RealtimeInferenceService

    model_dir = _make_model_dir(tmp_path)
    hot, snaps, bundle = _fake_engine_inputs()
    ledger: list = []
    cfg = _cfg(model_dir)
    svc = RealtimeInferenceService(
        config_loader=lambda: cfg,
        hot_set_fetcher=lambda: hot,
        snapshot_fetcher=lambda symbols: snaps,
        baseline_loader=lambda symbols, day: bundle,
        publisher=lambda payload, cfg: None,
        ledger_sink=ledger.append,
    )
    payload = svc.build_cycle()
    assert payload is not None
    assert payload["live_coverage"] == 1.0
    assert payload["quality"]["live_coverage"] == 1.0
    assert svc.counters["last_skip"] is None
    assert ledger and ledger[0]["live_coverage"] == 1.0


@pytest.mark.unit
def test_min_live_coverage_config_parsing():
    from backend.services.engine.inference.realtime_service import RealtimeInferConfig

    assert RealtimeInferConfig.from_mapping({}).min_live_coverage == 0.5
    assert RealtimeInferConfig.from_mapping({"min_live_coverage": "0.8"}).min_live_coverage == 0.8
    assert RealtimeInferConfig.from_mapping({"min_live_coverage": "5"}).min_live_coverage == 1.0
    assert RealtimeInferConfig.from_mapping({"min_live_coverage": "-1"}).min_live_coverage == 0.0


@pytest.mark.unit
def test_baseline_loader_tolerates_unknown_feature_columns(tmp_path):
    """模型 feature_columns 含 parquet 未收录列 → 取交集读取，缺失列交 fill 兜底（不再硬崩）。

    回归 2026-09-17 实测：273 列自定义模型（含 JQ110_*）在基线加载处 ArrowInvalid 硬崩。
    """
    import pandas as pd

    from backend.services.engine.inference.realtime_core import load_baseline_bundle

    parquet = tmp_path / "feat.parquet"
    pd.DataFrame(
        {
            "symbol": ["600036"] * 3,
            "trade_date": pd.to_datetime(["2026-09-09", "2026-09-10", "2026-09-11"]),
            "open": [10.0, 10.1, 10.2], "high": [10.2, 10.3, 10.4],
            "low": [9.9, 10.0, 10.1], "close": [10.1, 10.2, 10.3],  # fidelity: allow-limit-threshold — 非阈值：特征快照夹具的 low
            "volume": [1e6, 1e6, 1e6], "amount": [1e7, 1e7, 1e7],
            "f1": [0.1, 0.2, 0.3],
        }
    ).to_parquet(parquet)
    bundle = load_baseline_bundle(
        ["600036.SH"], __import__("datetime").date(2026, 9, 17),
        parquet_path=parquet, cols=["f1", "JQ110_52week_rank", "not_in_parquet"],
    )
    # 键=后缀身份（identity(源行纯数字)；审计 M2）
    row = bundle["rows"]["600036.SH"]
    assert row["f1"] == 0.3           # 存在的列正常取到
    assert row.get("JQ110_52week_rank") is None  # 未收录列不崩、交 fill 兜底
    assert len(bundle["history"]["600036.SH"]) == 3


# ── 基线分派（2026-09-17 迁移）：quantdb 直读 / 遗留快照 ──────────────────────


class _FakeQuantDBReader:
    """QuantDBFactorReader 假体：录制调用参数并返回可控 DataFrame。"""

    def __init__(self, *, dates, columns, day_df, hist_df):
        self.dates = list(dates)
        self.columns = list(columns)
        self.day_df = day_df
        self.hist_df = hist_df
        self.calls: list[tuple] = []

    def available_dates(self, source, *, start=None, end=None):
        self.calls.append(("available_dates", source, start, end))
        return [
            d for d in self.dates
            if (not start or d >= start) and (not end or d <= end)
        ]

    def describe(self, source):
        from types import SimpleNamespace

        self.calls.append(("describe", source))
        return SimpleNamespace(columns=list(self.columns))

    def read_day(self, source, *, features, trade_date, feature_sources=None):
        self.calls.append(("read_day", source, tuple(features), trade_date, feature_sources))
        return self.day_df

    def read_range(self, source, *, features, start, end, include_ohlcv=True, feature_sources=None):
        self.calls.append(("read_range", source, start, end, tuple(features)))
        return self.hist_df


def _qdb_meta(**extra):
    meta = {
        "data_source": "quantdb_factors",
        "factor_source": "l1_l2_factors",
        "quantdb_dir": "/data/quantdb",
        "context": {"market": "CN"},
    }
    meta.update(extra)
    return meta


@pytest.mark.unit
def test_load_baseline_for_model_quantdb_dispatch():
    """quantdb 绑定 → 直读：最近可用日、只请求源内存在的列、历史 qfq tail(45)、键归一纯数字。"""
    import pandas as pd

    from backend.services.engine.inference.realtime_core import load_baseline_for_model

    class _FakeHub:
        """日线 hub 假体：录制 fetch_daily_kline_batch 参数并返回可控历史。"""

        def __init__(self, df, avail_dates=None):
            self.df = df
            self.avail = list(avail_dates or [])
            self.calls: list[tuple] = []

        def _partition_dates(self, rel_path, start=None, end=None):
            return [d for d in self.avail if end is None or d <= end.strftime("%Y%m%d")]

        def fetch_daily_kline_batch(self, symbols, start, end, *, adjust="qfq"):
            self.calls.append((tuple(symbols), start, end, adjust))
            return self.df

    dates = ["2026-08-01", "2026-09-11", "2026-09-14"]
    day_df = pd.DataFrame(
        {"symbol": ["SH600036", "SZ000001"], "f1": [0.5, -0.2], "open": [10.0, 5.0]}
    )
    hist_df = pd.DataFrame(
        [
            {
                "symbol": "600036.SH", "trade_date": f"2026-07-{i % 28 + 1:02d}",
                "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0,
                "volume": 1e6, "amount": 1e7,
            }
            for i in range(50)
        ]
    )
    reader = _FakeQuantDBReader(
        dates=dates,
        columns=["symbol", "date", "f1", "open", "close"],
        day_df=day_df,
        hist_df=pd.DataFrame(),
    )
    hub = _FakeHub(hist_df, avail_dates=["20260914", "20260915", "20260916"])
    bundle = load_baseline_for_model(
        ["600036.SH", "000001.SZ"],
        date(2026, 9, 17),
        meta=_qdb_meta(),
        cols=["f1", "f9", "open"],
        parquet_path="/nonexistent.parquet",
        reader=reader,
        history_hub=hub,
    )
    calls = {c[0]: c for c in reader.calls}
    assert calls["available_dates"][3] == "2026-09-16"      # day-1 上界
    assert calls["read_day"][2] == ("f1", "open")           # f9 源内不存在 → 不请求
    assert calls["read_day"][3] == "2026-09-14"             # 最近可用日
    rows = bundle["rows"]
    assert set(rows) == {"600036.SH", "000001.SZ"}          # 前缀式返回 → 归一后缀身份键（审计 M2）
    assert rows["600036.SH"]["f1"] == pytest.approx(0.5)
    assert rows["600036.SH"]["f9"] is None                  # 缺列交 fill 兜底
    # 历史走 daily_forward 前复权直读（与快照价/批量特征同口径；后复权混用会致 mom_* 偏负），
    # 结束日放宽到「≤ day-1 的最新 K 线可用日」（09-16，因子源只到 09-14）
    assert len(hub.calls) == 1
    _syms, h_start, h_end, h_adjust = hub.calls[0]
    assert h_start.isoformat() == "2026-08-01" and h_end.isoformat() == "2026-09-16"
    assert h_adjust == "qfq"
    assert len(bundle["history"]["600036.SH"]) == 45        # tail(45)


@pytest.mark.unit
def test_load_baseline_for_model_legacy_keeps_parquet(tmp_path):
    """未绑定 quantdb 的模型 → 遗留快照 parquet（不可变快照语义保持不动）。"""
    import pandas as pd

    from backend.services.engine.inference.realtime_core import load_baseline_for_model

    parquet = tmp_path / "feat.parquet"
    pd.DataFrame(
        {
            "symbol": ["600036"], "trade_date": pd.to_datetime(["2026-09-14"]),
            "open": [10.0], "high": [10.1], "low": [9.9], "close": [10.0],  # fidelity: allow-limit-threshold — 非阈值：特征快照夹具的 low
            "volume": [1e6], "amount": [1e7], "f1": [0.3],
        }
    ).to_parquet(parquet)
    bundle = load_baseline_for_model(
        ["600036.SH"],
        date(2026, 9, 17),
        meta={"feature_columns": ["f1"]},
        cols=["f1"],
        parquet_path=parquet,
    )
    assert bundle["rows"]["600036.SH"]["f1"] == pytest.approx(0.3)
    assert len(bundle["history"]["600036.SH"]) == 1


@pytest.mark.unit
def test_default_baseline_daily_cache(tmp_path, monkeypatch):
    """基线日级缓存：同日二次调用不重读；换日重新加载（15s 周期不重复整读）。"""
    from backend.services.engine.inference import realtime_service as rs

    model_dir = tmp_path / "mdl_cache"
    model_dir.mkdir()
    (model_dir / "metadata.json").write_text(
        json.dumps({"feature_columns": ["f1"]}), encoding="utf-8"
    )
    calls: list = []

    def _fake_loader(*args, **kwargs):
        calls.append(args)
        return {"rows": {}, "history": {}}

    monkeypatch.setattr(rs, "load_baseline_for_model", _fake_loader)
    svc = rs.RealtimeInferenceService()
    svc._model_dir = str(model_dir)
    svc._default_baseline(["600036.SH"], date(2026, 9, 17))
    svc._default_baseline(["600036.SH"], date(2026, 9, 17))
    assert len(calls) == 1
    svc._default_baseline(["600036.SH"], date(2026, 9, 18))
    assert len(calls) == 2


@pytest.mark.integration
def test_quantdb_baseline_real_source():
    """真源直读（集成）：l1_l2_factors 最近可用日行 + OHLCV 历史可用。"""
    from pathlib import Path as _P

    if not _P("/data/quantdb/6_ml_datasets/l1_factors").is_dir():
        pytest.skip("QuantDB 因子源不存在（非容器环境）")
    from backend.services.engine.inference.realtime_core import load_baseline_quantdb

    cols = ["mom_ret_1d", "mom_ret_5d", "vol_std_20"]
    bundle = load_baseline_quantdb(
        ["600036.SH", "000001.SZ"], date.today(), meta=_qdb_meta(), cols=cols
    )
    rows = bundle["rows"]
    assert rows, "最近可用日应触达热集标的"
    assert "600036.SH" in rows and "000001.SZ" in rows  # 键=后缀身份（审计 M2）
    assert rows["600036.SH"]["mom_ret_1d"] is not None
    hist = bundle["history"]["600036.SH"]
    assert 1 <= len(hist) <= 45
    assert {"open", "high", "low", "close", "volume", "amount"} <= set(hist.columns)


@pytest.mark.integration
def test_quantdb_baseline_cross_lib_real_source():
    """跨库基线（集成）：副库（库:列）特征必须真取到值，不得被锚库列集过滤成缺席。

    回归 2026-09-23 修掉的缺陷：``load_baseline_quantdb`` 曾拿锚库列名过滤全部请求
    特征，副库条目整体进不了 ``read_day`` → 实时推理写出全 NaN 行却**不报错**，
    表现为「模型在线但该列永远缺失」，只在特征重要性上留下无从解释的痕迹。
    """
    from pathlib import Path as _P

    if not _P("/data/quantdb/6_ml_datasets/l1_factors").is_dir():
        pytest.skip("QuantDB 因子源不存在（非容器环境）")
    from backend.services.engine.data_platform.quantdb_factor_reader import (
        QuantDBFactorReader,
    )
    from backend.services.engine.inference.realtime_core import load_baseline_quantdb

    reader = QuantDBFactorReader(market="CN")
    cross_col = "micro_liquidity_amihud_5"
    if cross_col not in set(reader.describe("l2_factors").columns):
        pytest.skip(f"l2_factors 已无 {cross_col}：换一列后保留本条跨库断言")
    latest = reader.available_dates("l1_factors")[-1]
    if latest not in set(reader.available_dates("l2_factors")):
        pytest.skip("锚库与副库当前无共同可用日，跨库断言无对象")

    meta = _qdb_meta(
        factor_source="l1_factors",
        factor_field_sources={
            "mom_ret_1d": "mom_ret_1d",
            cross_col: f"l2_factors:{cross_col}",
        },
    )
    day = date.fromisoformat(latest) + timedelta(days=1)
    bundle = load_baseline_quantdb(
        ["600036.SH"], day, meta=meta, cols=["mom_ret_1d", cross_col]
    )

    row = bundle["rows"]["600036.SH"]  # 键=后缀身份（审计 M2）
    assert row["mom_ret_1d"] is not None, "锚库列必须照常取到"
    assert row[cross_col] is not None, "副库列被静默丢弃——跨库判可用性回归了"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tick_clears_stale_error_on_normal_skip(tmp_path):
    """周期正常走完（含「无实时行情→不发布」的跳过）必须清掉 last_error。

    实测现场：模型目录被删 → 每 15s 记一次 metadata.json 不存在；修好配置后
    收盘期间 live_coverage=0 一直跳过，`last_error` 因为只在**发布成功**时才清，
    永远停在旧错误上 → 系统健康面板对着已修的故障反复报警。
    口径：last_error = **最近一次完成的周期**的错误；跳过不是错误（另有 last_skip）。
    """
    from backend.services.engine.inference.realtime_service import RealtimeInferenceService

    model_dir = _make_model_dir(tmp_path)
    hot, snaps, bundle = _fake_engine_inputs()
    cfg = _cfg(model_dir, enabled=True)

    svc = RealtimeInferenceService(
        config_loader=lambda: cfg,
        hot_set_fetcher=lambda: hot,
        snapshot_fetcher=lambda symbols: snaps,
        baseline_loader=lambda symbols, day: bundle,
        publisher=lambda payload, cfg: None,
        status_writer=_no_status_write,
    )

    # 第一条：模型目录失效 → 记错误
    broken = _cfg(tmp_path / "gone", enabled=True)
    svc._config_loader = lambda: broken
    await svc.tick_once()
    assert "metadata.json 不存在" in (svc.counters["last_error"] or "")

    # 第二条：配置已修好，但零快照 → 正常跳过；旧错误必须被清掉
    svc._config_loader = lambda: cfg
    svc._snapshot_fetcher = lambda symbols: {}
    await svc.tick_once()
    assert svc.counters["last_error"] is None, "跳过不是错误，不能让旧错误常驻"
    assert svc.counters["skipped_no_live"] >= 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tick_enabled_without_model_dir_leaves_skip_trace(tmp_path):
    """已启用但 model_dir 为空 = 配置缺失，必须在 last_skip 留痕。

    该路径整个周期体被跳过：cycles 冻住、last_error 停在旧错误、此前又不写 last_skip
    —— 运维只看到"周期不涨 + 一个旧错误"，分不清配置缺失还是服务卡死。
    """
    from backend.services.engine.inference.realtime_service import (
        RealtimeInferenceService,
        RealtimeInferConfig,
    )

    cfg = RealtimeInferConfig(enabled=True, model_dir="", cadence_s=3)

    svc = RealtimeInferenceService(
        config_loader=lambda: cfg, status_writer=_no_status_write
    )

    await svc.tick_once()

    assert svc.counters["last_error"] is None
    assert "未配置模型目录" in (svc.counters["last_skip"] or ""), (
        "启用但无模型目录时必须留下跳过原因，不能静默变绿"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tick_dispatches_status_payload_to_injected_writer():
    """注入的 status_writer 必须收到**每轮**镜像载荷（新缝的守卫）。

    没有这条，注入点被绕过（有人把 hset 写回 tick_once）时不会有任何测试变红。
    载荷三件套 = 面板的三个问题：counters=发了多少 / config=按什么参数 /
    governor=发得动吗（governor=None 也要如实出现，面板须能区分「未建立」）。
    用「启用但未配模型目录」这条确定性分支：无 IO、当轮 counters 必带 last_skip，
    可同时证明下发的是**本轮**计数而非空壳。
    """
    from backend.services.engine.inference.realtime_service import (
        RealtimeInferenceService,
        RealtimeInferConfig,
    )

    cfg = RealtimeInferConfig(enabled=True, model_dir="", cadence_s=3)
    written: list[dict] = []
    svc = RealtimeInferenceService(
        config_loader=lambda: cfg, status_writer=written.append
    )

    await svc.tick_once()

    assert len(written) == 1, "每轮 tick 必须下发一次镜像载荷"
    payload = written[0]
    assert set(payload) == {"updated_at", "counters", "config", "governor", "shadow"}
    assert json.loads(payload["config"])["enabled"] is True
    assert "未配置模型目录" in (json.loads(payload["counters"])["last_skip"] or "")
    # 治理器快照必须与本轮同一份（节拍取自本 cfg，而非上一轮/默认值）
    assert json.loads(payload["governor"])["base_cadence_s"] == 3
    assert svc.counters["published"] == 0


# ── 状态镜像载荷（面板唯一读面）─────────────────────────────────────


@pytest.mark.unit
def test_status_mirror_payload_carries_governor(tmp_path):
    """镜像必须带治理器快照——否则面板只能看到「已发布/拦截」计数，
    看不到「近窗 p95 时延 / 降级阶梯 / 有效节拍」，专业实时推理面板缺的正是这层。

    治理器跑在引擎进程内（非 Redis），镜像它是把这份数据送达面板的**唯一**通道：
    admin 端点按约定不做跨服务 HTTP（realtime_service 注释明写）。
    """
    from backend.services.engine.inference.realtime_service import (
        RealtimeInferConfig,
        status_mirror_payload,
    )

    cfg = RealtimeInferConfig(enabled=True, model_dir="/m/mdl_a", cadence_s=15.0)
    payload = status_mirror_payload(
        counters={"published": 3, "last_ms": 120.5},
        cfg=cfg,
        governor={
            "level": 1, "base_cadence_s": 15.0, "effective_cadence_s": 15.0,
            "degraded": True, "p95_ms": 13000.0, "degradations": 2, "recoveries": 0,
            "last_ms": 120.5, "cycles": 40, "level_since": 12345.6,
        },
    )

    assert json.loads(payload["counters"])["published"] == 3
    assert json.loads(payload["config"])["cadence_s"] == 15.0
    gov = json.loads(payload["governor"])
    assert gov["level"] == 1 and gov["p95_ms"] == 13000.0
    assert gov["degradations"] == 2


@pytest.mark.unit
def test_status_mirror_payload_governor_absent_is_explicit(tmp_path):
    """治理器还没建立时（首周期前/节拍刚变更）如实写 null，不写假快照。

    面板据此显示「治理器未建立」，而不是把 level=0 当成「一切正常」——
    「没数据」与「数据说正常」必须能分开。
    """
    from backend.services.engine.inference.realtime_service import (
        RealtimeInferConfig,
        status_mirror_payload,
    )

    payload = status_mirror_payload(
        counters={}, cfg=RealtimeInferConfig(enabled=False, model_dir="", cadence_s=15.0),
        governor=None,
    )

    # 两种写法都算如实：键缺失，或显式 "null"。不允许出现一个假的 level=0 快照。
    assert json.loads(payload.get("governor") or "null") is None


# ── T-RT-02 行情前进校验（2026-11-06）────────────────────────────────────

@pytest.mark.unit
def test_quote_liveness_counters(tmp_path):
    """同一快照第二轮 → 冻结；量价变化 → 前进；首轮没有上轮可比。"""
    from backend.services.engine.inference.realtime_service import RealtimeInferenceService

    model_dir = _make_model_dir(tmp_path)
    hot, snaps, baseline = _fake_engine_inputs(price=12.0)
    svc = RealtimeInferenceService(
        config_loader=lambda: _cfg(model_dir),
        hot_set_fetcher=lambda: hot,
        snapshot_fetcher=lambda syms: snaps,
        baseline_loader=lambda syms, day: baseline,
        ledger_sink=lambda _e: None,
    )
    svc.build_cycle()
    assert svc.counters["quote_compared"] == 0  # 首轮只登记基准
    assert svc.counters["never_seen"] == 0

    svc.build_cycle()  # 同一份快照 → 全部冻结
    assert svc.counters["quote_compared"] == 2
    assert svc.counters["quote_frozen"] == 2
    assert svc.counters["quote_advanced"] == 0
    assert svc.counters["last_advance_ratio"] == 0.0

    bumped = {
        sym: {**snap, "Volume": str(int(float(snap["Volume"])) + 1000)}
        for sym, snap in snaps.items()
    }
    svc._snapshot_fetcher = lambda syms: bumped
    svc.build_cycle()
    assert svc.counters["quote_advanced"] == 2
    assert svc.counters["quote_frozen"] == 0
    assert svc.counters["last_advance_ratio"] == 1.0


@pytest.mark.unit
def test_never_seen_counts_hot_symbols_without_snapshot(tmp_path):
    """热集里没有快照键的标的单独计数——回答「从未写入 vs 写过又过期」。"""
    from backend.services.engine.inference.realtime_service import RealtimeInferenceService

    model_dir = _make_model_dir(tmp_path)
    hot, snaps, baseline = _fake_engine_inputs(price=12.0)
    hot = [*hot, "300750.SZ"]  # 第三只没有快照
    svc = RealtimeInferenceService(
        config_loader=lambda: _cfg(model_dir),
        hot_set_fetcher=lambda: hot,
        snapshot_fetcher=lambda syms: snaps,
        baseline_loader=lambda syms, day: baseline,
        ledger_sink=lambda _e: None,
    )
    svc.build_cycle()
    assert svc.counters["hot_symbols"] == 3
    assert svc.counters["snapshot_symbols"] == 2
    assert svc.counters["never_seen"] == 1


# ── T-RT-04 影子白名单（2026-11-06）──────────────────────────────────────

@pytest.mark.unit
def test_shadow_whitelist_runs_without_touching_production(tmp_path):
    """影子用扩展白名单重算：不改生产分、不发布，摘要 + 逐标的分返回给采样。"""
    from backend.services.engine.inference.realtime_service import (
        RealtimeInferConfig,
        RealtimeInferenceService,
    )

    model_dir = _make_model_dir(tmp_path, extra_feature="vol_std_10")
    hot, snaps, baseline = _fake_engine_inputs(price=12.0)
    reported: list[dict] = []

    def cfg() -> RealtimeInferConfig:
        return RealtimeInferConfig(
            enabled=True,
            model_dir=str(model_dir),
            cadence_s=3,
            override_whitelist=("mom_ret_1d",),
            shadow_whitelist=("mom_ret_1d", "vol_std_10"),
            shadow_every_n_cycles=1,
        )

    svc = RealtimeInferenceService(
        config_loader=cfg,
        hot_set_fetcher=lambda: hot,
        snapshot_fetcher=lambda syms: snaps,
        baseline_loader=lambda syms, day: baseline,
        ledger_sink=lambda _e: None,
        shadow_sink=reported.append,
    )
    payload = svc.build_cycle()
    assert payload is not None
    shadow = payload.get("shadow")
    assert shadow is not None
    assert shadow["whitelist"] == ["mom_ret_1d", "vol_std_10"]
    assert len(shadow["scores"]) == 2
    assert shadow["rank_corr"] is not None
    # 扩展列（vol_std_10）确实改变了输入 → 分数不再完全相同
    assert shadow["mean_abs_diff"] > 0
    assert svc.counters["shadow_runs"] == 1
    assert reported and reported[0] is shadow
    # 生产载荷本身不受影子影响（published 口径不变）
    assert payload["quality"]["override_whitelist"] == ["mom_ret_1d"]


@pytest.mark.unit
def test_shadow_summary_reversed_is_minus_one():
    from backend.services.engine.inference.realtime_service import _shadow_summary

    base = [0.1, 0.2, 0.3]
    same = _shadow_summary(base, base)
    assert same["rank_corr"] == 1.0
    assert same["top_overlap"] == 3
    rev = _shadow_summary(base, list(reversed(base)), top_n=1)
    assert rev["rank_corr"] == -1.0
    assert rev["top_overlap"] == 0


@pytest.mark.unit
def test_shadow_off_by_default(tmp_path):
    from backend.services.engine.inference.realtime_service import RealtimeInferenceService

    model_dir = _make_model_dir(tmp_path)
    hot, snaps, baseline = _fake_engine_inputs(price=12.0)
    svc = RealtimeInferenceService(
        config_loader=lambda: _cfg(model_dir),
        hot_set_fetcher=lambda: hot,
        snapshot_fetcher=lambda syms: snaps,
        baseline_loader=lambda syms, day: baseline,
        ledger_sink=lambda _e: None,
    )
    payload = svc.build_cycle()
    assert payload is not None and "shadow" not in payload


# ── T-RT-05 采样（2026-11-06）───────────────────────────────────────────

@pytest.mark.unit
def test_build_sample_rows_rank_pct_and_variant():
    from backend.services.engine.inference.realtime_samples import build_sample_rows

    rows = build_sample_rows(
        run_id="rt-x",
        trade_date=date(2026, 11, 6),
        sample_ts=datetime(2026, 11, 6, 7, 0, tzinfo=timezone.utc),
        model_version="m1",
        feature_version="fv1",
        variant="production",
        items=[
            {"symbol": "600036.SH", "fusion_score": 0.1, "score_rank": 2},
            {"symbol": "000001.SZ", "fusion_score": 0.3, "score_rank": 1},
        ],
        live_coverage=0.5,
        degraded_level=0,
    )
    assert [r["symbol"] for r in rows] == ["600036.SH", "000001.SZ"]
    assert rows[0]["rank_pct"] == 0.0 and rows[1]["rank_pct"] == 1.0
    assert all(r["variant"] == "production" for r in rows)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tick_once_samples_production_and_shadow(tmp_path):
    """发布成功才采样；影子同刻另一变体；失败只计数不影响主循环。"""
    from backend.services.engine.inference.realtime_service import (
        RealtimeInferConfig,
        RealtimeInferenceService,
    )

    model_dir = _make_model_dir(tmp_path, extra_feature="vol_std_10")
    hot, snaps, baseline = _fake_engine_inputs(price=12.0)
    calls: list[dict] = []

    async def writer(**kwargs):
        calls.append(kwargs)
        return len(kwargs["items"])

    def cfg() -> RealtimeInferConfig:
        return RealtimeInferConfig(
            enabled=True,
            model_dir=str(model_dir),
            cadence_s=3,
            override_whitelist=("mom_ret_1d",),
            shadow_whitelist=("mom_ret_1d", "vol_std_10"),
            shadow_every_n_cycles=1,
            sample_interval_s=0.0,  # 0 = 关闭 → 下面显式打开
        )

    svc = RealtimeInferenceService(
        config_loader=cfg,
        hot_set_fetcher=lambda: hot,
        snapshot_fetcher=lambda syms: snaps,
        baseline_loader=lambda syms, day: baseline,
        publisher=lambda _p, _c: None,
        ledger_sink=lambda _e: None,
        status_writer=_no_status_write,
        sample_writer=writer,
    )
    # sample_interval_s=0 → 关闭：一次都不该采
    await svc.tick_once()
    assert calls == []
    assert svc.counters["sample_rows"] == 0

    def cfg_on() -> RealtimeInferConfig:
        c = cfg()
        c.sample_interval_s = 0.001
        return c

    svc._config_loader = cfg_on
    await svc.tick_once()
    assert [c["variant"] for c in calls] == ["production", "shadow"]
    assert calls[0]["sample_ts"] == calls[1]["sample_ts"]
    assert svc.counters["sample_rows"] == 4
    assert svc.counters["sample_errors"] == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tick_sample_failure_is_counted_not_fatal(tmp_path):
    from backend.services.engine.inference.realtime_service import (
        RealtimeInferConfig,
        RealtimeInferenceService,
    )

    model_dir = _make_model_dir(tmp_path)
    hot, snaps, baseline = _fake_engine_inputs(price=12.0)

    async def broken(**kwargs):
        raise RuntimeError("db down")

    cfg = RealtimeInferConfig(
        enabled=True, model_dir=str(model_dir), cadence_s=3,
        override_whitelist=(), sample_interval_s=0.001,
    )
    svc = RealtimeInferenceService(
        config_loader=lambda: cfg,
        hot_set_fetcher=lambda: hot,
        snapshot_fetcher=lambda syms: snaps,
        baseline_loader=lambda syms, day: baseline,
        publisher=lambda _p, _c: None,
        ledger_sink=lambda _e: None,
        status_writer=_no_status_write,
        sample_writer=broken,
    )
    await svc.tick_once()
    assert svc.counters["published"] == 1  # 主链路照常发布
    assert svc.counters["sample_errors"] == 1
    assert "db down" in str(svc.counters["last_sample_error"])


@pytest.mark.unit
def test_status_mirror_payload_carries_shadow_and_config():
    from backend.services.engine.inference.realtime_service import (
        RealtimeInferConfig,
        status_mirror_payload,
    )

    cfg = RealtimeInferConfig(shadow_whitelist=("vol_std_10",), shadow_every_n_cycles=7)
    payload = status_mirror_payload(
        counters={"cycles": 1}, cfg=cfg, governor=None, shadow={"rank_corr": 0.99}
    )
    assert json.loads(payload["shadow"])["rank_corr"] == 0.99
    parsed = json.loads(payload["config"])
    assert parsed["shadow_whitelist"] == ["vol_std_10"]
    assert parsed["shadow_every_n_cycles"] == 7
    # 未开启影子的服务写 "null"，面板据此区分「未开」与「开了没到轮」
    assert json.loads(status_mirror_payload(
        counters={}, cfg=RealtimeInferConfig(), governor=None
    )["shadow"]) is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sample_write_end_to_end_real_db():
    """真实写表：建表幂等 + 两变体落行 + 冲突幂等 → 删除。"""
    from sqlalchemy import text

    from backend.services.engine.inference.realtime_samples import (
        SAMPLE_TABLE,
        write_score_samples,
    )
    from backend.shared.database_manager_v2 import get_session

    run_id = f"rt-sample-test-{uuid.uuid4().hex[:8]}"
    sample_ts = datetime.now(tz=timezone.utc)
    items = [
        {"symbol": "600036.SH", "fusion_score": 0.1, "score_rank": 2},
        {"symbol": "000001.SZ", "fusion_score": 0.3, "score_rank": 1},
    ]
    try:
        n = await write_score_samples(
            run_id=run_id,
            trade_date=sample_ts.date(),
            sample_ts=sample_ts,
            model_version="m-test",
            feature_version="fv-test",
            variant="production",
            items=items,
            live_coverage=0.5,
            degraded_level=0,
        )
        assert n == 2
        # 重放同一批：ON CONFLICT DO NOTHING → 不新增行
        await write_score_samples(
            run_id=run_id,
            trade_date=sample_ts.date(),
            sample_ts=sample_ts,
            model_version="m-test",
            feature_version="fv-test",
            variant="production",
            items=items,
            live_coverage=0.5,
            degraded_level=0,
        )
        async with get_session(read_only=True) as db:
            rows = (
                await db.execute(
                    text(
                        f"SELECT symbol, score, rank_pct FROM {SAMPLE_TABLE} "
                        "WHERE run_id=:r ORDER BY symbol"
                    ),
                    {"r": run_id},
                )
            ).all()
        assert len(rows) == 2
        assert rows[0][0] == "000001.SZ" and rows[0][2] == 1.0
        assert rows[1][0] == "600036.SH" and rows[1][2] == 0.0
    finally:
        async with get_session(read_only=False) as db:
            await db.execute(
                text(f"DELETE FROM {SAMPLE_TABLE} WHERE run_id=:r"), {"r": run_id}
            )
        async with get_session(read_only=True) as db:
            left = (
                await db.execute(
                    text(f"SELECT COUNT(*) FROM {SAMPLE_TABLE} WHERE run_id=:r"),
                    {"r": run_id},
                )
            ).scalar()
        assert int(left or 0) == 0


@pytest.mark.unit
def test_shadow_noop_when_whitelist_cannot_expand(tmp_path):
    """影子白名单与生产白名单交集相同 → 跳过重算（结果必然 1.0，白烧算力）。

    实测背景（2026-11-06）：生产模型 273 个特征是 JQ110_* 命名，与 TIER 32 列
    只有 vol_gk_20 / vol_std_10 两个同名——当前模型根本没有"再扩两列"的空间。
    """
    from backend.services.engine.inference.realtime_service import (
        RealtimeInferConfig,
        RealtimeInferenceService,
    )

    model_dir = _make_model_dir(tmp_path)
    hot, snaps, baseline = _fake_engine_inputs(price=12.0)

    def cfg() -> RealtimeInferConfig:
        return RealtimeInferConfig(
            enabled=True,
            model_dir=str(model_dir),
            cadence_s=3,
            override_whitelist=("mom_ret_1d",),
            shadow_whitelist=("mom_ret_1d", "not_a_tier_column"),
            shadow_every_n_cycles=1,
        )

    svc = RealtimeInferenceService(
        config_loader=cfg,
        hot_set_fetcher=lambda: hot,
        snapshot_fetcher=lambda syms: snaps,
        baseline_loader=lambda syms, day: baseline,
        ledger_sink=lambda _e: None,
        shadow_sink=lambda _p: None,
    )
    payload = svc.build_cycle()
    assert payload is not None and "shadow" not in payload
    assert svc.counters["shadow_noop"] == 1
    assert svc.counters["shadow_runs"] == 0


@pytest.mark.unit
def test_shadow_skipped_when_governor_degraded(tmp_path):
    """过载降载时影子必须让路：它是纯可选重算，不能和保命降载抢算力。"""
    from backend.services.engine.inference.realtime_service import (
        RealtimeInferConfig,
        RealtimeInferenceService,
    )

    model_dir = _make_model_dir(tmp_path, extra_feature="vol_std_10")
    hot, snaps, baseline = _fake_engine_inputs(price=12.0)
    cfg = RealtimeInferConfig(
        enabled=True,
        model_dir=str(model_dir),
        cadence_s=3,
        override_whitelist=("mom_ret_1d",),
        shadow_whitelist=("mom_ret_1d", "vol_std_10"),
        shadow_every_n_cycles=1,
    )
    svc = RealtimeInferenceService(
        config_loader=lambda: cfg,
        hot_set_fetcher=lambda: hot,
        snapshot_fetcher=lambda syms: snaps,
        baseline_loader=lambda syms, day: baseline,
        ledger_sink=lambda _e: None,
        shadow_sink=lambda _p: None,
    )
    payload = svc.build_cycle(degrade_level=1)
    assert payload is not None and "shadow" not in payload
    assert svc.counters["shadow_skipped_degraded"] == 1
    assert svc.counters["shadow_runs"] == 0
