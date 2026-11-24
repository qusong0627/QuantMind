"""机构择时 v2：纯函数与快照读取测试（不训练模型、不读 QuantDB）。"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd

from backend.shared.market_timing_v2 import signals as svc
from backend.shared.market_timing_v2.auto_inference import missing_signal_dates


def test_rule_states():
    tft = np.array([1.0, 1.0, -1.0, -1.0, 0.0])
    mlp = np.array([1.0, -1.0, 1.0, -1.0, 1.0])
    assert list(svc._rule(tft, mlp)) == [1.0, 0.5, 0.0, 0.0, 0.0]


def test_expanding_z_has_no_lookahead():
    s = pd.Series(np.arange(100, dtype=float))
    z = svc._expanding_z(s, min_periods=60)
    assert z.iloc[:60].isna().all()  # 第 60 个点之前没有统计量
    # 改变最后一个值不应影响它之前的 z
    s2 = s.copy()
    s2.iloc[-1] = 9999
    z2 = svc._expanding_z(s2, min_periods=60)
    pd.testing.assert_series_equal(z.iloc[:-1], z2.iloc[:-1])


def test_metrics_long_flat_and_costs():
    ret = pd.Series([0.01] * 10 + [0.0] * 10)
    pos = pd.Series([1.0] * 10 + [0.0] * 10)
    m = svc._metrics(ret, pos, cost_bps=0)
    assert m["ann"] > 0
    assert m["exposure"] == 0.5
    assert m["n"] == 20
    # 空仓段收益为 0，不应出现负年化
    assert m["mdd"] <= 0


def test_clean_json_types():
    out = svc._clean(
        {
            "a": np.float64(1.5),
            "b": np.int64(2),
            "c": float("nan"),
            "d": pd.Timestamp("2026-11-17"),
            "e": [np.float64(0.5)],
        }
    )
    assert out == {"a": 1.5, "b": 2, "c": None, "d": "2026-11-17", "e": [0.5]}
    json.dumps(out)


def test_load_snapshot_missing_and_present(tmp_path, monkeypatch):
    missing = tmp_path / "latest.json"
    monkeypatch.setattr(svc, "SNAPSHOT_PATH", missing)
    assert svc.load_snapshot() is None
    missing.write_text('{"status": "ok", "as_of": "2026-11-17"}', encoding="utf-8")
    assert svc.load_snapshot()["status"] == "ok"


def test_missing_signal_dates_and_cap():
    history = pd.DataFrame(
        {
            "date": pd.to_datetime(
                ["2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20", "2026-11-23"]
            )
        }
    )
    assert missing_signal_dates(history, pd.Timestamp("2026-11-17"), max_days=5) == [
        "2026-11-18",
        "2026-11-19",
        "2026-11-20",
        "2026-11-23",
    ]
    # 缺口超过上限：不自动补，交给页面标 stale
    assert missing_signal_dates(history, pd.Timestamp("2026-11-17"), max_days=2) == []
    assert missing_signal_dates(history, pd.Timestamp("2026-11-23"), max_days=5) == []


def test_load_tft_history_reads_daily_scores(tmp_path, monkeypatch):
    model_dir = tmp_path / "model"
    daily = model_dir / "pred_daily" / "dt=20261117"
    daily.mkdir(parents=True)
    pd.DataFrame({"symbol": ["SH600000", "SZ000001"], "score": [0.1, -0.3]}).to_parquet(
        daily / "data.parquet", index=False
    )
    monkeypatch.setattr(svc, "TFT_MODEL_DIR", model_dir)
    out = svc.load_tft_history()
    assert len(out) == 1
    assert out.iloc[0]["tft_n"] == 2
    assert abs(out.iloc[0]["tft_pred_mean"] - (-0.1)) < 1e-9
