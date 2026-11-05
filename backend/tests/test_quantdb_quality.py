"""QuantDB 同步语义质量闸门（A1）单元测试。

守三条契约：
1. 「缺最新分区 / 行数断崖 / 列集漂移」必须能发现，正常数据零误报；
2. 默认 warn 模式不阻断同步，block 模式才返回 blocked=True；
3. 同一问题同一天只告警一次（避免夜间任务每小时刷屏）。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from backend.services.engine.data_platform import quantdb_quality as q

DAILY = "daily_forward"
REL = "1_kline_data/daily_forward"
CAL_REL = "2_base_sector/trading_calendar"

# 2026-01-05 ~ 2026-01-16，10 个工作日
DATES = [
    "2026-01-05",
    "2026-01-06",
    "2026-01-07",
    "2026-01-08",
    "2026-01-09",
    "2026-01-12",
    "2026-01-13",
    "2026-01-14",
    "2026-01-15",
    "2026-01-16",
]
TODAY = date(2026, 1, 16)
EXPECTED_LATEST = "20260115"  # today 未收盘，允许 1 天 lag


def _write_calendar(root: Path, dates: list[str] = DATES) -> None:
    target = root / CAL_REL
    target.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {
            "TradingDate": [int(d.replace("-", "")) for d in dates],
            "IsTradingDay": [1] * len(dates),
        }
    ).to_parquet(target / "trading_days.parquet", index=False)


def _write_partition(
    root: Path,
    dt: str,
    *,
    rows: int = 100,
    extra_col: str | None = None,
    rel: str = REL,
) -> Path:
    """写一个标准 V2 分区：dt=YYYYMMDD/data.parquet。"""
    target = root / rel / f"dt={dt.replace('-', '')}"
    target.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(
        {
            "symbol": [f"SH60000{i % 10}" for i in range(rows)],
            "time": [dt] * rows,
            "close": [1.0] * rows,
        }
    )
    if extra_col:
        frame[extra_col] = 1.0
    path = target / "data.parquet"
    frame.to_parquet(path, index=False)
    return path


def _write_history(root: Path, dates: list[str], *, rows: int = 100) -> None:
    for dt in dates:
        _write_partition(root, dt, rows=rows)


def _gate(root: Path, **kwargs):
    params = {
        "datasets": [DAILY],
        "root": root,
        "lookback_days": 10,
        "max_lag_days": 1,
        "today": TODAY,
        "mode": "warn",
        "alert_service": False,  # 不回退到真实告警服务
    }
    params.update(kwargs)
    return q.run_quality_gate(**params)


def test_clean_data_has_no_issues(tmp_path: Path):
    _write_calendar(tmp_path)
    _write_history(tmp_path, DATES[:-1])  # 到 1/15

    report = _gate(tmp_path)

    assert report["ok"] is True
    assert report["blocked"] is False
    assert report["issues"] == []
    assert report["datasets"][DAILY]["latest"] == EXPECTED_LATEST


def test_missing_latest_partition_is_error(tmp_path: Path):
    _write_calendar(tmp_path)
    _write_history(tmp_path, DATES[:-2])  # 只到 1/14，1/15 应到未到

    report = _gate(tmp_path)

    assert report["ok"] is False
    types = {issue["issue_type"] for issue in report["issues"]}
    assert "latest_lag" in types
    error = next(i for i in report["issues"] if i["issue_type"] == "latest_lag")
    assert error["severity"] == "error"


def test_missing_middle_partition_is_warning(tmp_path: Path):
    _write_calendar(tmp_path)
    dates = [d for d in DATES[:-1] if d != "2026-01-12"]
    _write_history(tmp_path, dates)

    report = _gate(tmp_path)

    assert report["ok"] is True  # 最新在，历史缺口只 warning
    issue = next(i for i in report["issues"] if i["issue_type"] == "missing_partition")
    assert issue["severity"] == "warning"
    assert "20260112" in issue["message"]


def test_row_count_drop_is_error(tmp_path: Path):
    _write_calendar(tmp_path)
    _write_history(tmp_path, DATES[:-2], rows=100)  # 1/5 ~ 1/14
    _write_partition(tmp_path, DATES[-2], rows=1)  # 1/15 断崖

    report = _gate(tmp_path)

    assert report["ok"] is False
    issue = next(i for i in report["issues"] if i["issue_type"] == "row_count_drop")
    assert issue["severity"] == "error"
    assert issue["details"]["latest_rows"] == 1
    assert issue["details"]["median_rows"] == 100


def test_schema_drift_is_warning_once(tmp_path: Path):
    _write_calendar(tmp_path)
    _write_history(tmp_path, DATES[:-1])
    _write_partition(tmp_path, DATES[-2], extra_col="new_factor")

    first = _gate(tmp_path)
    drift = [i for i in first["issues"] if i["issue_type"] == "schema_drift"]
    assert len(drift) == 1
    assert drift[0]["severity"] == "warning"
    assert "new_factor" in drift[0]["details"]["added"]

    # 第二次：状态已更新，不再报同一漂移
    second = _gate(tmp_path)
    assert [i for i in second["issues"] if i["issue_type"] == "schema_drift"] == []


def test_block_mode_marks_blocked(tmp_path: Path):
    _write_calendar(tmp_path)
    _write_history(tmp_path, DATES[:-2])

    report = _gate(tmp_path, mode="block")

    assert report["ok"] is False
    assert report["blocked"] is True


def test_alerts_are_deduped_per_day(tmp_path: Path):
    class FakeAlerts:
        def __init__(self):
            self.calls: list[dict] = []

        def write_alert(self, **kwargs):
            self.calls.append(kwargs)
            return len(self.calls)

    _write_calendar(tmp_path)
    _write_history(tmp_path, DATES[:-2])  # 缺最新 → error
    alerts = FakeAlerts()

    first = _gate(tmp_path, alert_service=alerts)
    assert first["ok"] is False
    assert len(alerts.calls) == 1

    second = _gate(tmp_path, alert_service=alerts)
    assert second["ok"] is False
    assert len(alerts.calls) == 1  # 同一天同问题不重复告警


def test_calendar_falls_back_to_daily_forward_partitions(tmp_path: Path):
    """没有 trading_calendar 时用 daily_forward 自己的分区当日历（不误报）。"""
    _write_history(tmp_path, DATES[:-1])

    report = _gate(tmp_path)

    assert report["ok"] is True
    assert report["calendar_source"] == "daily_forward"


def test_lag_allowance_handles_same_day_sync(tmp_path: Path):
    """today 是交易日、当天分区还没下来：max_lag_days=1 不报错。"""
    _write_calendar(tmp_path)
    _write_history(tmp_path, DATES[:-1])  # 到 1/15

    report = _gate(tmp_path, max_lag_days=1)
    assert report["ok"] is True

    strict = _gate(tmp_path, max_lag_days=0)
    assert strict["ok"] is False
    assert any(i["issue_type"] == "latest_lag" for i in strict["issues"])


def test_unknown_dataset_is_skipped_not_crashing(tmp_path: Path):
    _write_calendar(tmp_path)

    report = q.run_quality_gate(
        ["not_a_dataset"],
        tmp_path,
        today=TODAY,
        alert_service=False,
    )

    assert report["ok"] is True
    assert report["datasets"]["not_a_dataset"]["skipped"] is True


def test_default_datasets_only_include_existing_dirs(tmp_path: Path):
    """未同步过的 optional 数据集不在默认检查面里，避免正常同步后报红。"""
    _write_calendar(tmp_path)
    _write_history(tmp_path, DATES[:-1])

    names = q.default_datasets(tmp_path)

    assert DAILY in names
    assert "factor_dictionary" not in names


def test_margin_trading_allows_t_plus_one_lag(tmp_path: Path):
    """两融 T+1：today 11/05、全局按 11/04，两融按 11/03 判定不误报。"""
    dates = ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"]
    _write_calendar(tmp_path, dates)
    for dt in dates[:2]:  # 11/02、11/03
        _write_partition(tmp_path, dt, rel="2_base_sector/margin_trading")

    report = q.run_quality_gate(
        ["margin_trading"],
        tmp_path,
        today=date(2026, 11, 5),
        max_lag_days=1,
        alert_service=False,
    )

    assert report["ok"] is True
    assert report["datasets"]["margin_trading"]["expected_latest"] == "20261103"
    assert report["issues"] == []


def test_non_dt_layout_is_skipped_not_error(tmp_path: Path):
    """tick_data 这类按 symbol 落盘的非 dt 布局：显式请求时跳过，不报缺分区。"""
    _write_calendar(tmp_path)
    tick_dir = tmp_path / "1_kline_data" / "tick_data"
    tick_dir.mkdir(parents=True)
    (tick_dir / "000001_SZ_20260511.parquet").write_bytes(b"not-a-real-parquet")

    report = q.run_quality_gate(
        ["tick_data"], tmp_path, today=TODAY, alert_service=False
    )

    assert report["ok"] is True
    assert report["datasets"]["tick_data"]["skipped"] is True
    assert report["datasets"]["tick_data"]["reason"] == "no_dt_partitions"


# ---------------------------------------------------------------------------
# 同步钩子（quantdb_daily_sync.run_daily_sync）
# ---------------------------------------------------------------------------


def _patch_sync_deps(monkeypatch, tmp_path, gate_result):
    import backend.scripts.quantdb_daily_sync as sync_mod

    calls: dict = {}

    def fake_gate(datasets, root, **kwargs):
        calls["datasets"] = datasets
        calls["root"] = root
        calls["mode"] = kwargs.get("mode")
        return gate_result

    monkeypatch.setattr(
        "backend.services.engine.data_platform.quantdb_quality.run_quality_gate",
        fake_gate,
    )
    monkeypatch.setattr(
        sync_mod,
        "sync_parquet",
        lambda *a, **k: {
            "synced": 0,
            "up_to_date": 1,
            "errors": [],
            "total_downloaded": 0,
        },
    )
    monkeypatch.setattr(sync_mod, "_sync_extra_sources", lambda **k: {})
    monkeypatch.setattr(
        "backend.scripts.delisted_daily_store.inject_after_sync",
        lambda *a, **k: {"status": "skipped"},
    )
    monkeypatch.setattr(sync_mod, "QUANTDB_DATA_DIR", tmp_path)
    return sync_mod, calls


def test_sync_gate_block_skips_pg_and_qlib(monkeypatch, tmp_path: Path):
    sync_mod, calls = _patch_sync_deps(
        monkeypatch,
        tmp_path,
        {
            "ok": False,
            "blocked": True,
            "issues": [
                {
                    "dataset": DAILY,
                    "issue_type": "latest_lag",
                    "severity": "error",
                    "message": "missing",
                    "details": {},
                }
            ],
        },
    )

    result = sync_mod.run_daily_sync(parquet_only=True, datasets=[DAILY])

    assert result["quality_gate_blocked_phases"] == ["pg", "qlib"]
    assert calls["datasets"] == [DAILY]
    assert calls["root"] == tmp_path


def test_sync_gate_off_does_not_run(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("QM_QUANTDB_QUALITY_GATE", "off")
    sync_mod, calls = _patch_sync_deps(
        monkeypatch, tmp_path, {"ok": True, "blocked": False, "issues": []}
    )

    result = sync_mod.run_daily_sync(parquet_only=True, datasets=[DAILY])

    assert "quality_gate" not in result
    assert calls == {}


def test_sync_gate_skipped_on_dry_run(monkeypatch, tmp_path: Path):
    sync_mod, calls = _patch_sync_deps(
        monkeypatch, tmp_path, {"ok": True, "blocked": False, "issues": []}
    )

    result = sync_mod.run_daily_sync(parquet_only=True, datasets=[DAILY], dry_run=True)

    assert "quality_gate" not in result
    assert calls == {}


# ---------------------------------------------------------------------------
# 管理端 API（容器内才有 fastapi；宿主机自动 skip）
# ---------------------------------------------------------------------------


def test_quality_api_reads_cached_report(monkeypatch, tmp_path: Path):
    qc = pytest.importorskip("backend.services.api.routers.admin.quantdb_console")
    import asyncio
    import json

    monkeypatch.setattr(
        "backend.shared.quantdb_paths.resolve_quantdb_dir", lambda: tmp_path
    )
    (tmp_path / ".quality").mkdir()
    (tmp_path / ".quality" / "latest.json").write_text(
        json.dumps({"ok": True, "issues": []}), encoding="utf-8"
    )

    out = asyncio.run(qc.get_quantdb_quality(refresh=False))

    assert out["success"] is True
    assert out["data"]["ok"] is True
    assert out["source"] == "cache"


def test_quality_api_refresh_calls_gate(monkeypatch, tmp_path: Path):
    qc = pytest.importorskip("backend.services.api.routers.admin.quantdb_console")
    import asyncio

    monkeypatch.setattr(
        "backend.shared.quantdb_paths.resolve_quantdb_dir", lambda: tmp_path
    )
    called: dict = {}

    def fake_gate(*args, **kwargs):
        called["args"] = args
        called["kwargs"] = kwargs
        return {"ok": True, "issues": [], "blocked": False}

    monkeypatch.setattr(
        "backend.services.engine.data_platform.quantdb_quality.run_quality_gate",
        fake_gate,
    )

    out = asyncio.run(qc.get_quantdb_quality(refresh=True))

    assert out["source"] == "fresh"
    assert called["kwargs"]["mode"] == "warn"
    assert called["kwargs"]["alert_service"] is False
