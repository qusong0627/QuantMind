"""T-P6-07 增量特征引擎测试：快车道 vs 批量金样一致 + 状态机全策略。

覆盖：
1. I（核心验收）：真实快照数据 20 标的 × 20 日——fast(窗口) vs batch(全历史) 逐值一致 ε=1e-9；
2. U：冷启动（短窗 == 批量截断窗同语义）；
3. G：注册表与快车道输出同集合；批量定义单源（脚本不再本地定义）；
4. U：状态机——聚合/日切/同日遮蔽/乱序/重复/缺口计数；
5. U：live 覆盖 + T-1 回退 provenance；
6. I：真实推送样本帧 → 状态演化断言。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_BACKEND = Path(__file__).resolve().parents[1]
_SNAPSHOT = "/app/db/feature_snapshots/model_features_2026.parquet"
_CST = timezone(timedelta(hours=8))
_LOAD_COLS = [
    "symbol",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
]
_EPS_REL = 1e-9
_EPS_ABS = 1e-12


def _load_sample(n_symbols: int = 20, min_rows: int = 60):
    """真实快照 parquet → {symbol: 升序 frame}（纯数字形态=生产主数据）。"""
    if not Path(_SNAPSHOT).exists():
        pytest.skip("快照 parquet 不可用（容器外）")
    from backend.shared.feature_incremental import TIER_MAX_WINDOW

    df = pd.read_parquet(_SNAPSHOT, columns=_LOAD_COLS)
    df = df[~df["symbol"].str.endswith((".SZ", ".SH", ".BJ"), na=False)]
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    counts = df["symbol"].value_counts()
    syms = [
        s
        for s in sorted(counts.index)
        if counts[s] >= max(min_rows, TIER_MAX_WINDOW + 20)
    ][:n_symbols]
    if len(syms) < 2:
        pytest.skip("样本不足")
    out = {}
    for sym in syms:
        g = df[df["symbol"] == sym].sort_values("trade_date").reset_index(drop=True)
        g["adj_factor"] = 1.0  # 参考路径所需（tier 不依赖）
        out[sym] = g
    return out


def _close_enough(a: float, b: float) -> bool:
    if np.isnan(a) and np.isnan(b):
        return True
    if np.isnan(a) or np.isnan(b):
        return False
    return abs(a - b) <= max(_EPS_ABS, _EPS_REL * max(abs(a), abs(b)))


# ── 1. 核心验收：真实数据 fast vs batch ─────────────────────────────


@pytest.mark.integration
def test_tier_matches_batch_on_real_data():
    """20 标的 × 20 日：窗口快车道 == 全历史批量（逐值 ε=1e-9）。"""
    from backend.shared.feature_defs import compute_features_for_group
    from backend.shared.feature_incremental import (
        TIER_COLUMNS,
        TIER_MAX_WINDOW,
        Window,
        compute_tier,
    )

    sample = _load_sample()
    n_days = 20
    compared: dict[str, int] = dict.fromkeys(TIER_COLUMNS, 0)
    worst: dict[str, float] = dict.fromkeys(TIER_COLUMNS, 0.0)
    mismatches: list[tuple[str, str, str, float, float]] = []

    for sym, frame in sample.items():
        # 停牌行规则与批量一致（close<=0 或 volume==0 整行剔除；批量在有效行上求值）
        valid = frame[(frame["close"] > 0) & (frame["volume"] != 0)].reset_index(
            drop=True
        )
        if len(valid) < 2:
            continue
        for i in range(len(valid) - n_days, len(valid)):
            ref = compute_features_for_group(valid.iloc[: i + 1].copy())
            win_frame = valid.iloc[max(0, i - (TIER_MAX_WINDOW - 1)) : i + 1]
            fast = compute_tier(
                Window(
                    win_frame["close"].to_numpy(),
                    win_frame["high"].to_numpy(),
                    win_frame["low"].to_numpy(),
                    win_frame["volume"].to_numpy(),
                    win_frame["amount"].to_numpy(),
                )
            )
            day = str(valid["trade_date"].iloc[i].date())
            for col in TIER_COLUMNS:
                a = float(ref[col].iloc[-1])
                b = float(fast[col])
                if np.isnan(a) and np.isnan(b):
                    continue
                compared[col] += 1
                if np.isnan(a) or np.isnan(b):
                    mismatches.append((sym, day, col, a, b))
                    continue
                rel = abs(a - b) / max(abs(a), 1e-12)
                worst[col] = max(worst[col], rel)
                if not _close_enough(a, b):
                    mismatches.append((sym, day, col, a, b))

    assert not mismatches, f"逐值不一致 {len(mismatches)} 处；样例: {mismatches[:5]}"
    # 覆盖度 sanity：每个 tier 列至少有真实比较（多数应近满；vpin_ma_20 依赖窗自然更少）
    thin = [c for c, n in compared.items() if n < 100]
    assert not thin, "比较样本过少（数据退化?）: " + str({c: compared[c] for c in thin})
    assert max(worst.values(), default=0) <= _EPS_REL


# ── 2. 冷启动：短窗 == 批量截断窗 ───────────────────────────────────


@pytest.mark.unit
def test_cold_start_matches_truncated_batch():
    from backend.shared.feature_defs import compute_features_for_group
    from backend.shared.feature_incremental import TIER_COLUMNS, Window, compute_tier

    rng = np.random.default_rng(7)
    n = 5  # 明显小于 20 日窗 → 两边都按 min_periods 出 NaN/部分值
    close = np.round(10 + np.cumsum(rng.normal(0, 0.1, n)), 4)
    frame = pd.DataFrame(
        {
            "trade_date": pd.date_range("2026-01-05", periods=n, freq="B"),
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": 1e6 + rng.integers(0, 1e5, n),
            "amount": 1e7 + rng.integers(0, 1e6, n),
            "adj_factor": 1.0,
        }
    )
    ref = compute_features_for_group(frame.copy())
    fast = compute_tier(
        Window(
            frame["close"].to_numpy(),
            frame["high"].to_numpy(),
            frame["low"].to_numpy(),
            frame["volume"].to_numpy(),
            frame["amount"].to_numpy(),
        )
    )
    for col in TIER_COLUMNS:
        a, b = float(ref[col].iloc[-1]), float(fast[col])
        assert _close_enough(a, b), (col, a, b)


# ── 3. 守卫：注册表 / 单源 ──────────────────────────────────────────


@pytest.mark.unit
def test_registry_and_single_source_guards():
    from backend.shared.feature_incremental import TIER_COLUMNS, Window, compute_tier

    win = Window(
        np.array([1.0, 2.0]),
        np.array([1.0, 2.0]),
        np.array([1.0, 2.0]),
        np.array([1.0, 2.0]),
        np.array([1.0, 2.0]),
    )
    assert sorted(compute_tier(win)) == list(TIER_COLUMNS), (
        "注册表与快车道输出必须同集合"
    )

    script = (_BACKEND / "scripts/update_feature_parquet.py").read_text(
        encoding="utf-8"
    )
    assert "def compute_features_for_group" not in script, (
        "批量定义只允许在 shared/feature_defs.py"
    )
    assert "from backend.shared.feature_defs import" in script
    market = (_BACKEND / "scripts/update_market_features.py").read_text(
        encoding="utf-8"
    )
    assert (
        "from backend.shared.feature_defs import compute_features_for_group" in market
    )
    assert (
        "backend.scripts.update_feature_parquet import compute_features_for_group"
        not in market
    )


# ── 4. 状态机 ───────────────────────────────────────────────────────


def _day_ts(day: str, hhmmss: str) -> float:
    dt = datetime.strptime(day + hhmmss, "%Y%m%d%H%M%S").replace(tzinfo=_CST)
    return dt.timestamp()


def _frame(days: list[str], closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "trade_date": pd.to_datetime(days),
            "open": closes,
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": closes,
            "volume": [1e6] * len(days),
            "amount": [1e7] * len(days),
        }
    )


@pytest.mark.unit
def test_engine_aggregation_rollover_and_mask():
    from backend.services.engine.inference.incremental_features import (
        IncrementalFeatureEngine,
    )

    engine = IncrementalFeatureEngine()
    hist = _frame(["2026-09-15", "2026-09-16"], [10.0, 10.5])
    engine.bootstrap("600036.SH", hist)
    # 首帧：open=11.0，随后 high/low/close 跟踪
    r1 = engine.on_snapshot(
        "600036.SH",
        {
            "price": 11.0,
            "open": 11.0,
            "high": 11.0,
            "low": 11.0,
            "volume": 1000,
            "amount": 11000,
            "ts": _day_ts("20260917", "093005"),
        },
    )
    assert r1["accepted"]
    engine.on_snapshot(
        "600036.SH",
        {
            "price": 11.5,
            "open": 11.0,
            "high": 11.6,
            "low": 10.9,
            "volume": 5000,
            "amount": 56000,
            "ts": _day_ts("20260917", "094005"),
        },
    )
    engine.on_snapshot(
        "600036.SH",
        {
            "price": 11.2,
            "open": 11.0,
            "high": 11.6,
            "low": 10.9,
            "volume": 9000,
            "amount": 101000,
            "ts": _day_ts("20260917", "100005"),
        },
    )
    bar = engine.forming_bar("600036.SH")
    assert bar["close"] == 11.2 and bar["high"] == 11.6 and bar["low"] == 10.9
    assert bar["volume"] == 9000  # 累计口径取最新（不求和）
    # 分钟桶
    assert len(engine.minute_ring("600036.SH")) == 3
    # live 特征：mom_ret_1d = 11.2/10.5 - 1
    feats = engine.compute("600036.SH")
    assert feats["mom_ret_1d"] == pytest.approx(11.2 / 10.5 - 1, rel=1e-9)
    # 日切：次日帧 → 旧 bar 入环
    engine.on_snapshot(
        "600036.SH",
        {
            "price": 12.0,
            "open": 12.0,
            "high": 12.0,
            "low": 12.0,
            "volume": 500,
            "amount": 6000,
            "ts": _day_ts("20260918", "093005"),
        },
    )
    stats = engine.stats()["details"]["600036.SH"]
    assert stats["rollovers"] == 1 and stats["ring"] == 3
    # 同日遮蔽：引导历史已含形成日 → 计算窗口剔除重复日（day_masks 计 1，不随 compute 次数增长）
    engine.reset("600036.SH")
    engine.bootstrap("600036.SH", _frame(["2026-09-16", "2026-09-17"], [10.5, 10.8]))
    engine.on_snapshot(
        "600036.SH",
        {
            "price": 10.9,
            "open": 10.8,
            "high": 10.9,
            "low": 10.7,
            "volume": 800,
            "amount": 8700,
            "ts": _day_ts("20260917", "143005"),
        },
    )
    for _ in range(3):
        engine.compute("600036.SH")
    assert engine.stats()["details"]["600036.SH"]["day_masks"] == 1


@pytest.mark.unit
def test_engine_out_of_order_duplicate_and_gap_counters():
    from backend.services.engine.inference.incremental_features import (
        IncrementalFeatureEngine,
    )

    engine = IncrementalFeatureEngine(gap_s=30)
    engine.bootstrap("600036.SH", _frame(["2026-09-16"], [10.0]))
    engine.on_snapshot(
        "600036.SH", {"price": 10.5, "ts": _day_ts("20260917", "100000")}
    )
    # 乱序：整帧拒绝，close 不变
    r = engine.on_snapshot(
        "600036.SH", {"price": 9.9, "ts": _day_ts("20260917", "095900")}  # fidelity: allow-limit-threshold — 非阈值：乱序 tick 的价格夹具
    )
    assert r == {"accepted": False, "reason": "out_of_order"}
    assert engine.forming_bar("600036.SH")["close"] == 10.5
    # 重复 ts（同帧重放）：幂等接受
    r = engine.on_snapshot(
        "600036.SH", {"price": 10.5, "ts": _day_ts("20260917", "100000")}
    )
    assert r["accepted"]
    # 缺口：>gap_s → gaps 计数，bar 照常更新
    engine.on_snapshot(
        "600036.SH", {"price": 10.7, "ts": _day_ts("20260917", "100200")}
    )
    stats = engine.stats()["details"]["600036.SH"]
    assert stats["out_of_order"] == 1 and stats["gaps"] == 1
    # 坏帧
    r = engine.on_snapshot(
        "600036.SH", {"price": None, "ts": _day_ts("20260917", "100300")}
    )
    assert r["reason"] == "bad_frame"


@pytest.mark.unit
def test_features_with_fallback_provenance():
    from backend.services.engine.inference.incremental_features import (
        IncrementalFeatureEngine,
    )

    engine = IncrementalFeatureEngine()
    engine.bootstrap("600036.SH", _frame(["2026-09-15", "2026-09-16"], [10.0, 10.5]))
    engine.on_snapshot(
        "600036.SH", {"price": 11.0, "ts": _day_ts("20260917", "100000")}
    )
    baseline = {
        "mom_ret_1d": 0.04,
        "vol_std_20": 0.012,
        "pe_ttm": 12.3,
        "mom_ret_5d": None,
    }
    row, prov = engine.features_with_fallback("600036.SH", baseline)
    assert prov["mom_ret_1d"] == "live" and row["mom_ret_1d"] == pytest.approx(
        11.0 / 10.5 - 1
    )
    assert prov["pe_ttm"] == "t1" and row["pe_ttm"] == 12.3
    # live NaN（历史过短）且基线有值 → 回退 T-1 并标注
    assert prov["vol_std_20"] in {"live", "t1"}
    if prov["vol_std_20"] == "t1":
        assert row["vol_std_20"] == 0.012
    assert prov["mom_ret_5d"] in {"live", "cold"}
    # 无状态标的
    row2, prov2 = engine.features_with_fallback("999999.SZ", {"pe_ttm": 1.0})
    assert row2 == {"pe_ttm": 1.0} and prov2 == {"pe_ttm": "t1"}


# ── 5. 真实推送样本帧 ───────────────────────────────────────────────


@pytest.mark.integration
def test_engine_real_push_frames_state_evolution():
    from backend.shared.tdx_aidata.protocol import parse_push_payload
    from backend.services.engine.inference.incremental_features import (
        IncrementalFeatureEngine,
    )
    from backend.tests.test_l05_store import _PUSH_SAMPLE

    rec = parse_push_payload(_PUSH_SAMPLE)[0]
    engine = IncrementalFeatureEngine()
    engine.bootstrap("600036.SH", _frame(["2026-09-15", "2026-09-16"], [40.5, 41.1]))
    base_ts = _day_ts("20260917", "093000")
    snap1 = {**rec, "ts": base_ts}
    assert engine.on_snapshot("600036.SH", snap1)["accepted"]
    bar = engine.forming_bar("600036.SH")
    assert bar["open"] == 41.15 and bar["close"] == 40.92  # 与推送样本逐值
    assert bar["high"] == 41.41 and bar["low"] == 40.52
    # 第二帧：价格上行 → close 更新、high 跟随
    snap2 = {**rec, "price": 41.30, "ts": base_ts + 3}
    engine.on_snapshot("600036.SH", snap2)
    bar = engine.forming_bar("600036.SH")
    assert bar["close"] == 41.30 and bar["high"] == 41.41
    feats = engine.compute("600036.SH")
    assert feats["mom_ret_1d"] == pytest.approx(41.30 / 41.1 - 1, rel=1e-9)
    assert feats["liq_volume"] == 538019.0
    assert json.dumps(feats, default=str)  # 可序列化（状态面/日志用）
