"""`stock_daily_latest_refresh.refresh_stock_daily_latest` 的门控与接线契约。

背景（停更事故）：市场定时同步调用 `run_daily_sync(skip_pg=True)`，PG 阶段从未
执行——`stock_daily_latest` 停在 2026-09-17，且 stock_name/industry 与
is_st/idx_*/roe/bp/ep/ln_mv/turnover/listed_days/concept_* 缺口列从未回填。

本组测试钉住修复形态：
1. 刷新自带「QuantDB 未推进则跳过」门控（force=False 时表内最新 >= QuantDB
   最新分区日即跳过；force=True 强制回刷）；
2. 刷新窗口 = 最近 N 天，fill 之后做名称/行业与缺口列富化，富化失败不阻断；
3. 三处接线必须存在：run_daily_sync Phase 2、管理台同步 PG 阶段、A 股市场
   同步调度（skip_pg=False）。
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

project_root = os.path.join(os.path.dirname(__file__), "../../")
sys.path.append(project_root)

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def sdl(monkeypatch):
    """导入刷新模块并隔离所有 DB 触点，返回 (module, 记录器)。"""
    from backend.scripts import stock_daily_latest_refresh as module

    captured: dict = {"fill_calls": [], "enrich": [], "cache": 0}

    def fake_fill(*, start_date=None, end_date=None, batch_days=20, **kw):
        captured["fill_calls"].append(
            {"start_date": start_date, "end_date": end_date, "batch_days": batch_days}
        )
        return {"status": "ok", "rows": 42}

    monkeypatch.setattr(
        "backend.scripts.quantdb_daily_sync.fill_pg_from_parquet", fake_fill
    )
    monkeypatch.setattr(
        module,
        "_enrich_names_industry",
        lambda url, start: captured["enrich"].append(("names", start)),
    )
    monkeypatch.setattr(
        module,
        "_enrich_gap_columns",
        lambda url, start, end: captured["enrich"].append(("gap", start, end)),
    )
    monkeypatch.setattr(
        module,
        "_invalidate_cache",
        lambda: captured.__setitem__("cache", captured["cache"] + 1),
    )
    return module, captured


def test_gate_skips_when_quantdb_not_advanced(sdl, monkeypatch):
    module, captured = sdl
    monkeypatch.setattr(
        module, "_table_range", lambda url: (100, date(2026, 9, 1), date(2026, 9, 30))
    )
    monkeypatch.setattr(module, "_quantdb_latest_trade_date", lambda: date(2026, 9, 30))

    out = module.refresh_stock_daily_latest(days=30, db_url="postgresql://test")

    assert out["status"] == "skipped"
    assert out["reason"] == "quantdb_not_advanced"
    assert captured["fill_calls"] == []


def test_force_bypasses_gate(sdl, monkeypatch):
    module, captured = sdl
    monkeypatch.setattr(
        module, "_table_range", lambda url: (100, date(2026, 9, 1), date(2026, 9, 30))
    )
    monkeypatch.setattr(module, "_quantdb_latest_trade_date", lambda: date(2026, 9, 30))

    out = module.refresh_stock_daily_latest(
        days=30, db_url="postgresql://test", force=True
    )

    assert out["status"] == "ok"
    assert len(captured["fill_calls"]) == 1


def test_refresh_window_and_enrich_order(sdl, monkeypatch):
    module, captured = sdl
    monkeypatch.setattr(
        module, "_table_range", lambda url: (100, date(2026, 9, 1), date(2026, 9, 17))
    )
    monkeypatch.setattr(module, "_quantdb_latest_trade_date", lambda: date(2026, 9, 30))

    out = module.refresh_stock_daily_latest(
        days=30, db_url="postgresql://test", batch_days=5
    )

    assert out["status"] == "ok"
    assert out["status_after"] == "ok"
    call = captured["fill_calls"][0]
    assert call["end_date"] == date.today()
    assert call["start_date"] == date.today() - timedelta(days=30)
    assert call["batch_days"] == 5
    # 富化在 fill 之后执行，且窗口与 fill 一致
    assert captured["enrich"][0] == ("names", date.today() - timedelta(days=30))
    assert captured["enrich"][1] == (
        "gap",
        date.today() - timedelta(days=30),
        date.today(),
    )
    assert captured["cache"] == 1


def test_enrich_failure_does_not_block(sdl, monkeypatch):
    module, captured = sdl
    monkeypatch.setattr(
        module, "_table_range", lambda url: (100, date(2026, 9, 1), date(2026, 9, 17))
    )
    monkeypatch.setattr(module, "_quantdb_latest_trade_date", lambda: date(2026, 9, 30))

    def boom_gap(url, start, end):
        raise RuntimeError("gap enrich boom")

    monkeypatch.setattr(module, "_enrich_gap_columns", boom_gap)

    out = module.refresh_stock_daily_latest(days=30, db_url="postgresql://test")

    assert out["status"] == "ok"  # 主结果不被富化异常吞掉
    assert captured["cache"] == 1  # 缓存失效照常执行


def test_daily_sync_phase2_wires_refresh():
    src = (REPO_ROOT / "backend/scripts/quantdb_daily_sync.py").read_text(
        encoding="utf-8"
    )
    assert "from backend.scripts.stock_daily_latest_refresh import" in src
    assert "refresh_stock_daily_latest(days=days)" in src


def test_admin_console_pg_stage_wires_refresh():
    src = (
        REPO_ROOT / "backend/services/api/routers/admin/quantdb_console.py"
    ).read_text(encoding="utf-8")
    assert "refresh_stock_daily_latest(days=30, force=True)" in src


def test_market_sync_scheduler_does_not_skip_pg():
    src = (
        REPO_ROOT / "backend/services/engine/tasks/market_sync_scheduler.py"
    ).read_text(encoding="utf-8")
    assert "run_daily_sync(skip_pg=False)" in src
    assert "run_daily_sync(skip_pg=True)" not in src


def test_cli_shell_exists():
    shell = REPO_ROOT / "scripts/data/maintenance/rolling_sync_stock_daily_recent.py"
    assert shell.exists()
    body = shell.read_text(encoding="utf-8")
    assert "refresh_stock_daily_latest" in body


def test_static_update_guarded_by_is_distinct_from():
    """静态置位语句必须带 IS DISTINCT FROM 守卫（防每日全表重写）。

    静态概念/指数映射按 symbol join 命中全历史（~1000 万行）：无守卫时每次
    刷新整表重写（实测单语句 6m21s / 831 万行），随 QuantDB 同步每日执行会
    把 PG 拖垮。守卫只跳过无变化的行，最终表态不变。
    """
    src = (REPO_ROOT / "backend/scripts/stock_daily_latest_refresh.py").read_text(
        encoding="utf-8"
    )
    assert "IS DISTINCT FROM" in src
    assert "WHERE s.symbol=m.symbol AND" in src


def test_hub_industry_falls_back_to_instrument_list(tmp_path):
    """instrument_detail.parquet 缺席时回退 instrument_list.parquet。

    行业富化依赖此映射；只认历史文件名时映射静默为空，industry 列永不回填。
    """
    pd = pytest.importorskip("pandas")
    from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

    d = tmp_path / "2_base_sector" / "instrument_detail"
    d.mkdir(parents=True)
    pd.DataFrame(
        {
            "Symbol": ["600000.SH", "000001.SZ"],
            "rs_hyname": ["银行", "银行"],
            "rs_hycode_sim": ["J66", "J66"],
        }
    ).to_parquet(d / "instrument_list.parquet")

    hub = QuantDBDataHub(str(tmp_path))
    out = hub.fetch_instrument_industry()

    assert not out.empty
    assert "ind_name_l1" in out.columns
    assert set(out["symbol"]) == {"600000.SH", "000001.SZ"}


def test_sync_script_normalize_frame_converts_symbol_to_prefix():
    """QuantDB 后缀式 symbol 必须转 PG 前缀式内码，避免写出后缀重复行。"""
    pytest.importorskip("psycopg2")
    pd = pytest.importorskip("pandas")
    import importlib

    maintenance = str(REPO_ROOT / "scripts/data/maintenance")
    if maintenance not in sys.path:
        sys.path.insert(0, maintenance)
    mod = importlib.import_module("sync_stock_daily_latest_from_parquet")

    frame = pd.DataFrame(
        {
            "trade_date": ["2026-09-30", "2026-09-30", "2026-09-30"],
            "symbol": ["600036.SH", "000001.SZ", "920268.BJ"],
            "close": [35.0, 12.0, 20.0],
        }
    )
    out, _ = mod.normalize_frame(frame, ["trade_date", "symbol", "close"])

    assert list(out["symbol"]) == ["SH600036", "SZ000001", "BJ920268"]
