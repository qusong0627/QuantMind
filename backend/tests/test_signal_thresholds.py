"""T-P4-03 测试：分位阈值唯一实现 + 量纲回归（尺度不变性）。

口径（统一交易栈 §4.1 铁律）：选股阈值**只允许引用分位**（由分数分布推得的百分位），
绝对分数只做诊断展示。根因回归：模型分数分布 [-0.048, 0.012]（9/15 实测）与硬编码
绝对带 [0.10,0.12] 错位 → 恒空仓；分位化后阈值随分布自适应，**任何尺度变换不可能再错位**。

核心断言 = **尺度不变性**：同一票分数整体 ×137.5 / ÷1000 / 平移 ±0.5 后，
分位模式选股结果逐一相同（绝对模式对照归零）。
"""

from __future__ import annotations

import pytest


# ── resolver 纯函数 ─────────────────────────────────────────────────


@pytest.mark.unit
def test_resolve_thresholds_basic():
    from backend.shared.signal_thresholds import resolve_thresholds

    scores = [round(0.001 * i, 4) for i in range(1, 1001)]  # 0.001..1.0 均匀分布
    t = resolve_thresholds(scores)
    assert t.mode == "quantile"
    # 分位单调：exit ≤ entry ≤ strong ≤ max
    assert t.exit_avg_top1 <= t.entry_avg_top1 <= t.strong_top1 <= t.score_max
    # 个股带下界 ≈ p98 ≈ 0.98，上界 = max
    assert t.score_min == pytest.approx(0.98, abs=0.01)
    assert t.score_max == pytest.approx(1.0, abs=1e-9)
    # 审计留痕
    assert set(t.quantiles) >= {
        "score_min_q",
        "score_max_q",
        "strong_top1_q",
        "entry_avg_top1_q",
        "exit_avg_top1_q",
    }


@pytest.mark.unit
def test_resolve_thresholds_edge_cases():
    from backend.shared.signal_thresholds import resolve_thresholds

    assert resolve_thresholds([]) is None
    t1 = resolve_thresholds([0.5])
    assert t1.score_min <= 0.5 <= t1.score_max
    # 全同值（并列）
    t2 = resolve_thresholds([0.3] * 100)
    assert t2.score_min == 0.3 and t2.score_max == 0.3


@pytest.mark.unit
def test_resolve_thresholds_is_scale_equivariant():
    """resolver 自身：输入 ×k → 输出阈值 ×k（分位口径的定义性质）。"""
    from backend.shared.signal_thresholds import resolve_thresholds

    base = [round(0.0001 * i, 6) for i in range(1, 500)]
    k = 137.5
    t1 = resolve_thresholds(base)
    t2 = resolve_thresholds([s * k for s in base])
    assert t2.score_min == pytest.approx(t1.score_min * k, rel=1e-6)
    assert t2.entry_avg_top1 == pytest.approx(t1.entry_avg_top1 * k, rel=1e-6)
    assert t2.strong_top1 == pytest.approx(t1.strong_top1 * k, rel=1e-6)


# ── 扫描器级：尺度不变性（量纲回归核心）────────────────────────────


def _fixture_snapshot(scores_scale: float = 1.0, scores_shift: float = 0.0):
    import pandas as pd

    from backend.services.engine.scanners.model_signal_scanner import (
        ModelSignalSnapshot,
    )

    raw = [
        ("600036.SH", 0.118, "银行"),
        ("000001.SZ", 0.105, "银行"),
        ("600519.SH", 0.130, "食品饮料"),
        ("600000.SH", 0.090, "银行"),
        ("601318.SH", 0.111, "非银金融"),
        ("603288.SH", 0.107, "食品饮料"),
        ("002594.SZ", 0.101, "汽车"),
        ("600030.SH", 0.113, "非银金融"),
        ("600887.SH", 0.099, "食品饮料"),  # fidelity: allow-limit-threshold — 非阈值：信号分数夹具（不是涨跌幅）
        ("000002.SZ", 0.095, "房地产"),  # fidelity: allow-limit-threshold — 非阈值：信号分数夹具（不是涨跌幅）
    ]
    rows = [
        {"symbol": s, "score": sc * scores_scale + scores_shift} for s, sc, _ in raw
    ]
    industry_map = {s: ind for s, _, ind in raw}
    return ModelSignalSnapshot(
        trade_date="2026-09-15",
        day_scores=pd.DataFrame(rows),
        industry_map=industry_map,
        price_day=None,
        rank_pct_by_symbol={},
        index_ma20_ok=None,
    )


@pytest.mark.unit
def test_scanner_quantile_mode_is_scale_invariant():
    """核心量纲回归：×137.5 / ÷1000 / +0.5 平移 下，分位模式选股逐一同集；
    绝对模式在大尺度下归零作对照（证明测试有区分度）。"""
    from backend.services.engine.scanners.model_signal_scanner import (
        scan_model_signals,
    )

    def symbols(scale, shift, mode):
        opps, _meta = scan_model_signals(_fixture_snapshot(scale, shift), mode=mode)
        return sorted(o.symbol for o in opps)

    base_q = symbols(1.0, 0.0, "quantile")
    assert base_q, "分位模式夹具不应为空"
    assert symbols(137.5, 0.0, "quantile") == base_q
    assert symbols(0.001, 0.0, "quantile") == base_q
    assert symbols(1.0, 0.5, "quantile") == base_q
    assert symbols(137.5, 25.0, "quantile") == base_q

    # 对照：绝对模式在 ×137.5 下带内为空（量纲错位的复现）
    assert symbols(137.5, 0.0, "absolute") == []


@pytest.mark.unit
def test_scanner_quantile_meta_carries_threshold_audit():
    from backend.services.engine.scanners.model_signal_scanner import (
        scan_model_signals,
    )

    _opps, meta = scan_model_signals(_fixture_snapshot(), mode="quantile")
    assert meta["thresholds"]["mode"] == "quantile"
    assert meta["thresholds"]["score_min"] > 0
    assert meta["entry_gate"]["strong_ok"] in (True, False)


@pytest.mark.unit
def test_industry_signals_accept_strong_threshold_override():
    """核心扩展：_compute_industry_signals 的强行业阈值可注入（默认 0.10 保持存量等价）。"""
    import pandas as pd

    from backend.services.engine.inference.inference_backtest_service import (
        _compute_industry_signals,
    )

    df = pd.DataFrame(
        [
            {"symbol": "600036.SH", "score": 0.5},
            {"symbol": "000001.SZ", "score": 0.2},
        ]
    )
    ind = {"600036.SH": "银行", "000001.SZ": "券商"}
    _t, _c, _a, strong_default = _compute_industry_signals(df, ind)
    assert strong_default == 2  # 0.5 与 0.2 均 ≥ 0.10
    _t, _c, _a, strong_mid = _compute_industry_signals(df, ind, strong_threshold=0.3)
    assert strong_mid == 1  # 仅 0.5 ≥ 0.3
    _t, _c, _a, strong_override = _compute_industry_signals(
        df, ind, strong_threshold=0.6
    )
    assert strong_override == 0


@pytest.mark.asyncio
async def test_real_data_quantile_mode_produces_picks():
    """恒空仓修复的活证据（真库）：9/15 全市场分位模式选出非空且 ≤ daily_select_max；
    绝对模式（存量口径）为 0 作对照。"""
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
    )
    from backend.services.engine.scanners.model_signal_loader import (
        load_model_signal_snapshot,
    )
    from backend.services.engine.scanners.model_signal_scanner import scan_model_signals

    snapshot = await load_model_signal_snapshot()
    if snapshot is None or snapshot.day_scores.empty:
        pytest.skip("无模型信号数据（推理未运行）")

    cfg = StrategyConfig()
    q_opps, q_meta = scan_model_signals(snapshot, cfg, mode="quantile")
    a_opps, _ = scan_model_signals(snapshot, cfg, mode="absolute")

    assert q_meta["thresholds"]["mode"] == "quantile"
    # 分位模式非空（当前模型分数分布下这是恒空仓修复的直接证明）
    assert len(q_opps) > 0, "分位模式应选出候选（恒空仓修复验证）"
    assert len(q_opps) <= cfg.daily_select_max
    # 跨测试确定性：用后关池（见 test_model_signal_scanner 同注释）
    from backend.shared.database_manager_v2 import close_database

    await close_database()
