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


def _make_model_dir(tmp_path: Path, n_features: int = 6) -> Path:
    """真实 sklearn 模型目录（含 metadata），供导出链与服务使用。"""
    from sklearn.linear_model import Ridge

    rng = np.random.default_rng(3)
    cols = [f"f{i}" for i in range(n_features)]
    cols[0] = "mom_ret_1d"  # 白名单列之一
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
            "symbol": ["600036"] * n,
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
    rows = {
        "600036": {"mom_ret_1d": 0.01, "f1": 0.2, "f2": -0.1, "f3": 0.05, "f4": 0.3, "f5": 0.0},
        "000001": {"mom_ret_1d": -0.02, "f1": 0.1, "f2": 0.4, "f3": -0.2, "f4": 0.0, "f5": 0.2},
    }
    history = {
        "600036": _history([10.0 + 0.04 * i for i in range(25)]),  # 末位≈10.96
        "000001": _history([10.4] * 25),
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
        )

    p_plain = svc(()).build_cycle()
    assert p_plain is not None
    assert p_plain["run_id"].startswith("rt-mdl_rt_test-")
    assert p_plain["feature_dim"] == 6 and p_plain["ready_symbols"] == 2
    assert {s["symbol"] for s in p_plain["scores"]} == {"600036", "000001"}
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
        assert {r[0] for r in rows} == {"600036", "000001"}
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
