"""QuantDB 用量账本（B1）单元测试。

契约：
1. 下载按数据集累计 bytes / files / errors / seconds，落 data/quantdb/.usage/YYYYMMDD.json；
2. 进程内缓冲 + 显式 flush，重复 flush 幂等（同一批不会重复累加）；
3. load_usage 聚合最近 N 天，文件缺失/损坏不抛异常。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from backend.services.engine.data_platform import quantdb_usage as u


def test_record_and_flush_accumulates(tmp_path: Path):
    u.record_download(
        "daily_forward", nbytes=100, seconds=1.5, root=tmp_path, day="20261105"
    )
    u.record_download(
        "daily_forward", nbytes=50, seconds=0.5, root=tmp_path, day="20261105"
    )
    u.flush()

    data = u.load_usage(tmp_path, days=1, today=date(2026, 11, 5))
    row = data["days"][0]
    assert row["date"] == "20261105"
    assert row["datasets"]["daily_forward"]["bytes"] == 150
    assert row["datasets"]["daily_forward"]["files"] == 2
    assert row["datasets"]["daily_forward"]["seconds"] == 2.0
    assert row["datasets"]["daily_forward"]["errors"] == 0


def test_error_counts_and_idempotent_flush(tmp_path: Path):
    u.record_download(
        "l1_factors",
        nbytes=10,
        seconds=0.1,
        ok=False,
        error="HTTP 410",
        root=tmp_path,
        day="20261105",
    )
    u.flush()
    u.flush()  # 幂等：buffer 已清空，不重复累加

    data = u.load_usage(tmp_path, days=1, today=date(2026, 11, 5))
    ds = data["days"][0]["datasets"]["l1_factors"]
    assert ds["files"] == 1
    assert ds["errors"] == 1
    assert ds["bytes"] == 10


def test_load_usage_aggregates_multiple_days(tmp_path: Path):
    u.record_download("daily_forward", nbytes=100, root=tmp_path, day="20261104")
    u.record_download("daily_forward", nbytes=200, root=tmp_path, day="20261105")
    u.record_download("valuation", nbytes=50, root=tmp_path, day="20261105")
    u.flush()

    data = u.load_usage(tmp_path, days=7, today=date(2026, 11, 5))

    assert [d["date"] for d in data["days"]] == ["20261104", "20261105"]
    assert data["totals"]["bytes"] == 350
    assert data["totals"]["datasets"]["daily_forward"]["bytes"] == 300
    assert data["totals"]["datasets"]["valuation"]["bytes"] == 50


def test_load_usage_missing_or_broken_files_do_not_raise(tmp_path: Path):
    usage_dir = tmp_path / u.USAGE_DIR
    usage_dir.mkdir(parents=True)
    (usage_dir / "20261105.json").write_text("{not-json", encoding="utf-8")

    data = u.load_usage(tmp_path, days=3, today=date(2026, 11, 5))

    assert data["days"] == []
    assert data["totals"]["bytes"] == 0


def test_buffer_is_scoped_by_root(tmp_path: Path):
    other = tmp_path / "other"
    u.record_download("daily_forward", nbytes=1, root=tmp_path, day="20261105")
    u.record_download("daily_forward", nbytes=2, root=other, day="20261105")
    u.flush()

    a = u.load_usage(tmp_path, days=1, today=date(2026, 11, 5))
    b = u.load_usage(other, days=1, today=date(2026, 11, 5))
    assert a["totals"]["bytes"] == 1
    assert b["totals"]["bytes"] == 2


def test_usage_api_reads_ledger(monkeypatch, tmp_path: Path):
    import asyncio

    import pytest

    qc = pytest.importorskip("backend.services.api.routers.admin.quantdb_console")
    today = date.today().strftime("%Y%m%d")
    u.record_download("daily_forward", nbytes=100, root=tmp_path, day=today)
    u.flush()
    monkeypatch.setattr(
        "backend.shared.quantdb_paths.resolve_quantdb_dir", lambda: tmp_path
    )

    out = asyncio.run(qc.get_quantdb_usage_stats(days=1, include_account=False))

    assert out["success"] is True
    assert out["data"]["totals"]["bytes"] == 100
    assert out["data"]["totals"]["datasets"]["daily_forward"]["files"] == 1
