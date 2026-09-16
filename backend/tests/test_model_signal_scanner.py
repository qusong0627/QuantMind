"""T-P4-01 测试：模型信号扫描器 —— **等价性验证**（结果与现链路一致）。

等价性基线 = `inference_backtest_service._select_stocks_daily`（/selection 端点与回测
引擎共用的选股核心）。扫描器**复用而非复制**该实现；本套件用确定性夹具 + 真库
双侧对照锁定等价，并锁 Opportunity 字段映射与编排接线。
"""

from __future__ import annotations

from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]


def _fixture_snapshot():
    """确定性夹具：分数区间内外 / 主板创业板科创 / ST / 涨停 / 行业映射混合。"""
    import pandas as pd

    from backend.services.engine.scanners.model_signal_scanner import (
        ModelSignalSnapshot,
    )

    # 分数带 [0.10, 0.12]；含带外高分（0.13 应予排除）
    rows = [
        {"symbol": "600036.SH", "score": 0.118},  # 主板块内 ✓
        {"symbol": "000001.SZ", "score": 0.105},  # 主板块内 ✓
        {"symbol": "600519.SH", "score": 0.130},  # 带外（>max）✗
        {"symbol": "600000.SH", "score": 0.090},  # 带外（<min）✗
        {"symbol": "688001.SH", "score": 0.115},  # 科创板 ✗
        {"symbol": "300750.SZ", "score": 0.112},  # 创业板 ✗（非主板）
        {"symbol": "601318.SH", "score": 0.111},  # 主板块内但 ST ✗
        {"symbol": "603288.SH", "score": 0.107},  # 主板块内但涨停 ✗
        {"symbol": "002594.SZ", "score": 0.101},  # 主板块内 ✓（无价格数据 → 保留）
    ]
    day_scores = pd.DataFrame(rows)
    industry_map = {
        "600036.SH": "银行",
        "000001.SZ": "银行",
        "600519.SH": "食品饮料",
        "600000.SH": "银行",
        "688001.SH": "电子",
        "300750.SZ": "电力设备",
        "601318.SH": "非银金融",
        "603288.SH": "食品饮料",
        "002594.SZ": "汽车",
    }
    price_day = pd.DataFrame(
        [
            {"symbol": "600036.SH", "pct_change": 1.2, "is_st": 0},
            {"symbol": "000001.SZ", "pct_change": -0.5, "is_st": 0},
            {"symbol": "601318.SH", "pct_change": 0.3, "is_st": 1},  # ST
            {"symbol": "603288.SH", "pct_change": 9.9, "is_st": 0},  # 接近涨停
        ]
    )
    rank_map = {
        "600036.SH": 0.9985,
        "000001.SZ": 0.9750,
        "002594.SZ": 0.9600,
    }
    return ModelSignalSnapshot(
        trade_date="2026-09-15",
        day_scores=day_scores,
        industry_map=industry_map,
        price_day=price_day,
        rank_pct_by_symbol=rank_map,
        index_ma20_ok=True,
    )


@pytest.mark.unit
def test_scanner_matches_legacy_selection_exactly():
    """等价性（核心断言）：同快照下 扫描器 symbol 序列 == _select_stocks_daily。"""
    from backend.services.engine.inference.inference_backtest_service import (
        StrategyConfig,
        _select_stocks_daily,
    )
    from backend.services.engine.scanners.model_signal_scanner import scan_model_signals

    snapshot = _fixture_snapshot()
    cfg = StrategyConfig()
    legacy = _select_stocks_daily(
        snapshot.day_scores, snapshot.industry_map, cfg, snapshot.price_day
    )
    opportunities, _meta = scan_model_signals(
        snapshot, cfg, ts="2026-09-15T15:30:00+08:00"
    )

    legacy_syms = [p["symbol"] for p in legacy]
    scanner_syms = [o.symbol for o in opportunities]
    assert scanner_syms == legacy_syms, "扫描器与现链路选股结果必须完全一致"
    # 夹具本身要“有货”且有区分度（防夹具退化为空集的假绿）
    assert set(legacy_syms) == {"600036.SH", "000001.SZ", "002594.SZ"}

    # 逐字段对照（score/industry/trend 透传无损）
    by_sym = {p["symbol"]: p for p in legacy}
    for o in opportunities:
        ref = by_sym[o.symbol]
        assert o.evidence["fusion_score"] == pytest.approx(ref["score"])
        assert o.evidence["industry"] == ref["industry"]
        assert o.evidence["trend"] == ref["trend"]


@pytest.mark.unit
def test_opportunity_field_mapping_uses_rank_pct():
    """strength=rank 分位、score=round(rank×100)；缺失 rank 记 0。"""
    from backend.services.engine.scanners.model_signal_scanner import scan_model_signals

    snapshot = _fixture_snapshot()
    opportunities, meta = scan_model_signals(snapshot, ts="2026-09-15T15:30:00+08:00")
    by_sym = {o.symbol: o for o in opportunities}
    assert by_sym["600036.SH"].strength == pytest.approx(0.9985)
    assert by_sym["600036.SH"].score == 100  # round(99.85)
    assert by_sym["000001.SZ"].score == 98  # round(97.5)
    assert by_sym["002594.SZ"].score == 96
    assert all(o.sources == ("model_signal",) for o in opportunities)
    assert all(o.horizon == "T+1..T+5" and o.state == "watch" for o in opportunities)
    # meta 批级证据（扫描器只呈现不拦截）
    assert meta["market_state"] in {"牛市", "震荡偏强", "震荡", "震荡偏弱", "熊市"}
    assert meta["entry_gate"]["ma20_ok"] is True
    assert meta["picked"] == 3


@pytest.mark.unit
def test_scanner_meta_matches_industry_signals():
    """meta 的 avg_top1/强行业数与 _compute_industry_signals 同源一致。"""
    from backend.services.engine.inference.inference_backtest_service import (
        _compute_industry_signals,
        _market_state,
    )
    from backend.services.engine.scanners.model_signal_scanner import scan_model_signals

    snapshot = _fixture_snapshot()
    _opps, meta = scan_model_signals(snapshot)
    _top1, _cnt, avg_top1, strong = _compute_industry_signals(
        snapshot.day_scores, snapshot.industry_map
    )
    assert meta["avg_top1"] == pytest.approx(round(float(avg_top1), 6))
    assert meta["strong_industry_count"] == int(strong)
    assert meta["market_state"] == _market_state(avg_top1, strong)


@pytest.mark.unit
def test_every_registered_scanner_has_dispatch():
    """注册即生效 tripwire：注册表里每个 id 必须接线 dispatch（漏接即红）。"""
    from backend.shared.scanner_spi import SCANNERS
    from backend.services.engine.scanners.runner import _SCANNER_DISPATCH

    registered = {s.id for s in SCANNERS}
    assert registered <= set(_SCANNER_DISPATCH), "注册扫描器缺少 dispatch 接线"


@pytest.mark.asyncio
async def test_real_data_equivalence_via_loader():
    """真库等价（最强验证）：真实交易日信号 → 装载快照 → 扫描器 vs 现链路逐符号一致。"""
    try:
        from sqlalchemy import text

        from backend.shared.database_manager_v2 import get_session
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"依赖不可用: {exc}")

    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"DB 连接抖动: {exc}")

    from backend.services.engine.inference.inference_backtest_service import (
        StrategyConfig,
        _select_stocks_daily,
    )
    from backend.services.engine.scanners.model_signal_loader import (
        load_model_signal_snapshot,
    )
    from backend.services.engine.scanners.model_signal_scanner import scan_model_signals

    snapshot = await load_model_signal_snapshot()
    if snapshot is None or snapshot.day_scores.empty:
        pytest.skip("无模型信号数据（推理未运行）")

    cfg = StrategyConfig()
    legacy = _select_stocks_daily(
        snapshot.day_scores, snapshot.industry_map, cfg, snapshot.price_day
    )
    opportunities, meta = scan_model_signals(snapshot, cfg)
    assert [o.symbol for o in opportunities] == [p["symbol"] for p in legacy]
    assert meta["trade_date"] == snapshot.trade_date
    assert meta["picked"] == len(legacy)
    # 跨测试确定性：用后关池（asyncpg 连接绑定事件循环；pytest-asyncio 每测试独立
    # loop，残留池会让同进程后续真库测试撞 "attached to a different loop"）。
    from backend.shared.database_manager_v2 import close_database

    await close_database()


@pytest.mark.unit
def test_runtime_entry_points_exist():
    """运行入口就绪：编排器 + CLI（防重构误删）。"""
    runner_src = (_BACKEND / "services/engine/scanners/runner.py").read_text(
        encoding="utf-8"
    )
    assert "merge_opportunities(" in runner_src
    assert "scanner_switch_enabled" in runner_src
    cli = _BACKEND / "scripts/run_scanner.py"
    assert cli.exists()
    assert "run_scan" in cli.read_text(encoding="utf-8")


@pytest.mark.unit
def test_legacy_core_empty_band_returns_empty_without_crash():
    """回归（T-P4-01 等价性工具实测抓到）：分数带内为空 → 返回 []，
    不得因"空 DataFrame × 空布尔索引丢列"KeyError 崩溃（真库空带日曾复现）。"""
    import pandas as pd

    from backend.services.engine.inference.inference_backtest_service import (
        StrategyConfig,
        _select_stocks_daily,
    )

    day_scores = pd.DataFrame(
        [
            {"symbol": "600036.SH", "score": 0.005},
            {"symbol": "000001.SZ", "score": 0.012},
        ]
    )
    # 无价格数据、无行业映射（最简崩溃路径）
    assert _select_stocks_daily(day_scores, {}, StrategyConfig(), None) == []
