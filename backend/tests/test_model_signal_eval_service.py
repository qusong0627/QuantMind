"""滚动实盘力评估服务的纯函数单测（容器内 python3 直跑，无 pytest 依赖）。"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from backend.services.api import model_signal_eval_service as svc
from backend.services.api.model_signal_eval_service import (
    _atomic_write_json,
    _daily_stats,
    _read_json,
    _weekly_ic,
    build_verdict,
    pick_window,
)


def test_pick_window_keeps_last_complete_signal_days():
    tdays = [
        d.strftime("%Y%m%d") for d in pd.date_range("2026-01-01", periods=100, freq="B")
    ]
    dates = tdays[::2]  # 每两个交易日一个信号日
    window = pick_window(dates, tdays, horizon=5, window_days=20)
    # 最后 6 个交易日（T+1..T+5 + 当天）没有完整标签，必须被裁掉
    assert window["end"] <= tdays[-7]
    assert window["signal_days"] == 20
    idx = {d: i for i, d in enumerate(tdays)}
    assert idx[window["end"]] + 1 + 5 < len(tdays)
    assert window["start"] < window["end"]


def test_pick_window_rejects_short_history():
    tdays = ["20260101", "20260102", "20260103"]
    try:
        pick_window(["20260101"], tdays, horizon=5, window_days=20)
    except Exception as exc:
        assert "不足" in str(exc)
    else:
        raise AssertionError("窗口不足应报 EvalUnavailable")


def test_daily_stats_returns_and_excess():
    dates = pd.date_range("2026-08-01", periods=10, freq="B")
    daily = pd.Series([0.01] * 10, index=dates)
    market = pd.Series([0.005] * 10, index=dates)
    stats = _daily_stats(daily, market)
    assert abs(stats["total_return"] - (1.01**10 - 1)) < 1e-9
    assert abs(stats["market_return"] - (1.005**10 - 1)) < 1e-9
    assert stats["excess"] > 0
    assert abs(stats["mdd"]) < 1e-12
    assert stats["days"] == 10


def test_daily_stats_computes_four_index_excesses():
    dates = pd.date_range("2026-08-01", periods=10, freq="B")
    daily = pd.Series([0.01] * 10, index=dates)
    market = pd.Series([0.005] * 10, index=dates)
    benchmarks = {
        "hs300": pd.Series([0.002] * 10, index=dates),
        "sse": pd.Series([0.003] * 10, index=dates),
        "szse": pd.Series([0.004] * 10, index=dates),
        "zz1000": pd.Series([0.006] * 10, index=dates),
    }
    stats = _daily_stats(daily, market, benchmarks)
    for name in benchmarks:
        assert f"excess_{name}" in stats
        assert f"{name}_return" in stats
        assert stats[f"excess_{name}"] > 0
    assert stats["excess_zz1000"] < stats["excess_hs300"]


def test_build_verdict_ranking_ok_but_long_fails():
    raw = {"mean": 0.10, "hac_t": 3.0}
    neutral = {"mean": 0.08, "hac_t": 3.0}
    quant = {"monotonicity": 0.9, "spread_mean": 0.01}
    stab = {"rank_autocorr_daily": 0.9}
    topn = [
        {"n": 20, "excess": -0.02, "excess_t": -0.4},
        {"n": 50, "excess": -0.01, "excess_t": -0.2},
    ]
    verdict = build_verdict(raw, neutral, quant, stab, topn)
    assert verdict["ranking_pass"] and verdict["neutral_pass"]
    assert verdict["decile_pass"] and verdict["stability_pass"]
    assert not verdict["long_pass"]
    assert "长多没跑赢" in verdict["summary"]


def test_build_verdict_weak_ranking():
    raw = {"mean": 0.005, "hac_t": 0.5}
    neutral = {"mean": 0.002, "hac_t": 0.4}
    quant = {"monotonicity": 0.2, "spread_mean": -0.01}
    stab = {"rank_autocorr_daily": 0.6}
    verdict = build_verdict(raw, neutral, quant, stab, [])
    assert not verdict["ranking_pass"]
    assert "没有可确认" in verdict["summary"]


def test_atomic_write_and_read_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "nested" / "report.json"
        payload = {"fingerprint": {"mtime": 1.0}, "report": {"value": 0.5}}
        _atomic_write_json(path, payload)
        assert _read_json(path) == payload
        leftovers = list(path.parent.glob("*.tmp.*"))
        assert leftovers == []


def test_weekly_ic_labels_are_week_start():
    ic = pd.DataFrame(
        {
            "ic_raw": [0.1, 0.2, 0.3],
            "ic_neutral": [0.08, 0.18, 0.28],
        },
        index=["20260810", "20260811", "20260817"],
    )
    rows = _weekly_ic(ic)
    assert [row["week"] for row in rows] == ["08-10", "08-17"]
    assert abs(rows[0]["ic"] - 0.15) < 1e-9
    assert np.isfinite(rows[1]["ic_neutral"])


def test_get_state_ready_stale_and_crash_recovery():
    with tempfile.TemporaryDirectory() as tmp:
        cache = Path(tmp)
        pred = cache / "pred.parquet"
        with (
            patch.object(svc, "_cache_dir", return_value=cache),
            patch.object(svc, "pred_parquet_path", return_value=pred),
            patch.object(svc, "pred_fingerprint", return_value={"fp": 1}),
        ):
            paths = svc._cache_paths("m1", 63)
            _atomic_write_json(
                paths["report"],
                {"fingerprint": {"fp": 1}, "report": {"status": "ready"}},
            )
            _atomic_write_json(paths["status"], {"status": "done", "progress": 100})
            assert svc.get_state("m1", "/fake", window_days=63)["status"] == "ready"

            # 进程被杀：status 仍 running 但锁文件已不在 → 不应永远卡在 running
            _atomic_write_json(
                paths["status"], {"status": "running", "progress": 10, "step": "ic"}
            )
            assert svc.get_state("m1", "/fake", window_days=63)["status"] == "ready"

        with (
            patch.object(svc, "_cache_dir", return_value=cache),
            patch.object(svc, "pred_parquet_path", return_value=pred),
            patch.object(svc, "pred_fingerprint", return_value={"fp": 2}),
        ):
            state = svc.get_state("m1", "/fake", window_days=63)
            assert state["status"] == "stale"
            assert state["report"] == {"status": "ready"}


def _verdict(**overrides: bool) -> dict:
    base = {
        "ranking_pass": True,
        "neutral_pass": True,
        "decile_pass": True,
        "long_pass": True,
        "stability_pass": True,
    }
    base.update(overrides)
    return base


def test_admission_grades():
    report = {
        "window": {"end": "2026-11-13"},
        "computed_at": "2026-11-24 10:00:00",
        "eval_version": 2,
        "verdict": _verdict(),
    }
    assert svc.admission_from_report(report)["grade"] == "green"
    assert (
        svc.admission_from_report({**report, "verdict": _verdict(long_pass=False)})[
            "grade"
        ]
        == "yellow"
    )
    red = svc.admission_from_report(
        {**report, "verdict": _verdict(ranking_pass=False, neutral_pass=False)}
    )
    assert red["grade"] == "red"
    assert "截面排序" in red["reasons"]
    assert svc.admission_from_report(report, stale=True)["stale"] is True
    short = svc.admission_from_report(
        {**report, "window": {"end": "2026-11-13", "independent_periods": 3}}
    )
    assert short["grade"] == "yellow"
    assert any("独立周期" in reason for reason in short["reasons"])


def test_coverage_stats_flags_low_days():
    import tempfile as _tempfile

    with _tempfile.TemporaryDirectory() as tmp:
        pred = Path(tmp) / "pred.parquet"
        dates = pd.to_datetime(["2026-08-11", "2026-08-12"])
        raw = pd.DataFrame(
            {
                "trade_date": [dates[0]] * 4 + [dates[1]] * 4,
                "pred": [0.1] * 8,
                "split": ["test"] * 8,
            }
        )
        raw.to_parquet(pred)
        frame = pd.DataFrame(
            {
                "date_key": ["20260811", "20260811", "20260812"],
                "pred": [0.1, 0.2, 0.3],
            }
        )
        window = {"start": "2026-08-11", "end": "2026-08-12"}
        stats = svc.coverage_stats(pred, window, frame)
    assert stats["avg"] == 0.375
    assert stats["days_below_50"] == 1
    assert len(stats["low_days"]) == 1


def test_history_append_dedupes_same_window():
    with tempfile.TemporaryDirectory() as tmp:
        with patch.object(svc, "_cache_dir", return_value=Path(tmp)):
            report = {
                "window": {
                    "start": "2026-08-11",
                    "end": "2026-11-13",
                    "signal_days": 63,
                },
                "computed_at": "2026-11-24 10:00:00",
                "eval_version": 2,
                "metrics": {
                    "rank_ic": {"mean": 0.1, "icir": 0.8},
                    "neutral_ic": {"mean": 0.08},
                    "quantiles": {"spread_mean": 0.01, "monotonicity": 0.9},
                    "stability": {"rank_autocorr_daily": 0.9},
                },
                "topn": [
                    {"n": 20, "total_return": -0.1, "excess": -0.02},
                    {"n": 50, "total_return": -0.08, "excess": -0.01},
                ],
                "verdict": _verdict(long_pass=False),
                "admission": svc.admission_from_report(
                    {
                        "window": {"end": "2026-11-13"},
                        "verdict": _verdict(long_pass=False),
                    }
                ),
            }
            svc.append_history("m1", 63, report)
            svc.append_history(
                "m1", 63, {**report, "computed_at": "2026-11-24 11:00:00"}
            )
            rows = svc.read_history("m1", 63)
            assert len(rows) == 1
            assert rows[0]["computed_at"] == "2026-11-24 11:00:00"
            svc.append_history(
                "m1",
                63,
                {
                    **report,
                    "window": {
                        **report["window"],
                        "start": "2026-08-12",
                        "end": "2026-11-14",
                    },
                },
            )
            assert len(svc.read_history("m1", 63)) == 2


def test_deterioration_reasons_red_and_streak():
    from backend.services.engine.tasks.model_signal_eval_tasks import (
        _deterioration_reasons,
    )

    red_report = {
        "admission": {"grade": "red", "reasons": ["截面排序"]},
        "verdict": _verdict(ranking_pass=False),
    }
    assert _deterioration_reasons(red_report, []) == ["评估 red：截面排序"]
    yellow_report = {"admission": {"grade": "yellow", "reasons": []}}
    history = [{"long_pass": False, "top20_excess": -0.01} for _ in range(5)]
    reasons = _deterioration_reasons(yellow_report, history)
    assert reasons and "连续" in reasons[0]


def test_resolve_execution_price_follows_training_contract(tmp_path: Path):
    """评估口径必须跟随训练 execution_price：meta > 公式 > config.yaml > 目标契约默认。"""
    assert (
        svc.resolve_execution_price({"label": {"execution_price": "open"}}, None)
        == "open"
    )
    assert svc.resolve_execution_price({"execution_price": "close"}, None) == "close"
    assert (
        svc.resolve_execution_price(
            {"label_formula": "label = close(T+11) / open(T+1) - 1"}, None
        )
        == "open"
    )
    assert (
        svc.resolve_execution_price(
            {"label_formula": "label = close(T+6) / close(T+1) - 1"}, None
        )
        == "close"
    )
    # metadata 缺字段时回退模型目录 config.yaml
    (tmp_path / "config.yaml").write_text(
        "label:\n  execution_price: open\n", encoding="utf-8"
    )
    assert svc.resolve_execution_price({}, str(tmp_path)) == "open"
    # 目标契约默认 open；旧 legacy 默认 close
    assert svc.resolve_execution_price({"target_id": "cn_t5_rank"}, None) == "open"
    assert svc.resolve_execution_price({"target_id": "legacy"}, None) == "close"


def test_fingerprint_includes_execution_price(tmp_path: Path):
    pred = tmp_path / "pred.parquet"
    pred.write_bytes(b"x")
    with patch.object(svc, "_latest_data_day", return_value="20261127"):
        fp_open = svc.pred_fingerprint(
            pred, horizon=5, window_days=63, execution_price="open"
        )
        fp_close = svc.pred_fingerprint(
            pred, horizon=5, window_days=63, execution_price="close"
        )
    assert fp_open["execution_price"] == "open"
    assert fp_close["execution_price"] == "close"
    assert fp_open != fp_close
