"""P0-2 测试：质量回填的 QuantDB 真实收益源（口径修复）。

背景（docs/滚动训练与模型生命周期_设计方案.md §2.2-2）：
- 旧实现兜底读 `1_kline_data/daily_backward`（**已知复权缺陷序列**——
  拼接缝长假跳变，memory: daily-backward-defective-use-qfq），其次
  `daily_unadjusted`（未复权，除权日假跌）→ 生产 IC 可能算在坏收益上；
- `dts[0]` 取「第一个 >= trade_date 的日期」，trade_date 不在库中时
  **静默换窗**，T 日错位而无人知；
- market 在调用侧硬编码 "CN"。

修复口径：CN 只读 `daily_forward`（qfq 前复权，收益口径正确）；
d0 必须精确等于 trade_date，否则 fail-closed 返回空 + 告警；
非 CN 市场 fail-closed（该函数只服务 CN QuantDB 目录）；
坏序列目录（daily_backward / daily_unadjusted）**一律不读**。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.services.engine.inference.inference_quality_backfill import (
    _as_date,
    _load_real_returns,
    _load_real_returns_quantdb,
    _load_real_returns_snapshot,
    _now_utc,
    _pearson_ic_from_scores,
    _rank_ic_from_scores,
    _resolve_parquet_path,
)

# ── 夹具：迷你 QuantDB 目录 ─────────────────────────────────────────────────

_DATES = ["20260910", "20260911", "20260914", "20260915"]

_FORWARD_CLOSES = {
    "600036.SH": {
        "20260910": 10.0,
        "20260911": 10.5,
        "20260914": 11.0,
        "20260915": 11.2,
    },
    "000001.SZ": {
        "20260910": 20.0,
        "20260911": 19.5,
        "20260914": 19.0,
        "20260915": 18.8,
    },
}


def _write_kline(base, dataset: str, closes: dict[str, dict[str, float]]) -> None:
    """在 {base}/1_kline_data/{dataset}/dt=YYYYMMDD/part.parquet 写日线。"""
    for dt in _DATES:
        rows = [
            {"symbol": sym, "close": float(prices[dt])}
            for sym, prices in closes.items()
            if dt in prices
        ]
        if not rows:
            continue
        day_dir = base / "1_kline_data" / dataset / f"dt={dt}"
        day_dir.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows).to_parquet(day_dir / "part.parquet", index=False)


@pytest.fixture()
def quantdb_dir(tmp_path):
    base = tmp_path / "quantdb"
    _write_kline(base, "daily_forward", _FORWARD_CLOSES)
    # 坏序列：同日期、closes 放大 100 倍——一旦被读入，收益值必与期望不符
    _write_kline(
        base,
        "daily_backward",
        {
            sym: {dt: c * 100 for dt, c in prices.items()}
            for sym, prices in _FORWARD_CLOSES.items()
        },
    )
    return base


# ── 主路径 ──────────────────────────────────────────────────────────────────


def test_returns_from_daily_forward_qfq(quantdb_dir):
    df = _load_real_returns_quantdb(
        "2026-09-10", 2, market="CN", quantdb_dir=str(quantdb_dir)
    )
    assert not df.empty
    got = dict(zip(df["symbol"], df["label"], strict=True))
    # 600036.SH: 11.0/10.0 - 1 = 0.10（用 daily_backward 的 ×100 数据会得 0.0）
    assert got["SH600036"] == pytest.approx(0.10)
    # 000001.SZ: 19.0/20.0 - 1 = -0.05
    assert got["SZ000001"] == pytest.approx(-0.05)
    # symbol 已转 prefix 口径
    assert set(got) == {"SH600036", "SZ000001"}


def test_never_reads_daily_backward_when_forward_missing(tmp_path):
    """坏序列存在也不能兜底：forward 缺失 → fail-closed 空结果。"""
    base = tmp_path / "quantdb"
    _write_kline(
        base,
        "daily_backward",
        {sym: dict(p) for sym, p in _FORWARD_CLOSES.items()},
    )
    _write_kline(
        base,
        "daily_unadjusted",
        {sym: dict(p) for sym, p in _FORWARD_CLOSES.items()},
    )
    df = _load_real_returns_quantdb("2026-09-10", 2, market="CN", quantdb_dir=str(base))
    assert df.empty
    assert list(df.columns) == ["symbol", "label"]


def test_fail_closed_when_trade_date_missing_from_series(quantdb_dir):
    """d0 必须精确等于 trade_date；缺失时不得静默换窗（旧 dts[0] 行为）。"""
    df = _load_real_returns_quantdb(
        "2026-09-09",
        2,
        market="CN",
        quantdb_dir=str(quantdb_dir),  # 库中无 09-09
    )
    assert df.empty


def test_fail_closed_for_non_cn_market(quantdb_dir):
    """该函数只服务 CN QuantDB 目录；HK/US 等一律不下钻（由快照路径负责）。"""
    for market in ("HK", "US", "CRYPTO"):
        df = _load_real_returns_quantdb(
            "2026-09-10", 2, market=market, quantdb_dir=str(quantdb_dir)
        )
        assert df.empty, market


def test_fail_closed_when_future_days_insufficient(quantdb_dir):
    # 可用日期 4 个（含 d0），horizon=4 需要 d0..d4 共 5 个 → 不足
    df = _load_real_returns_quantdb(
        "2026-09-10", 4, market="CN", quantdb_dir=str(quantdb_dir)
    )
    assert df.empty


def test_outer_fallback_passes_market_and_dir(quantdb_dir):
    """入口 `_load_real_returns`：CN 快照取不到 → QuantDB（qfq）；非 CN 不回退。"""
    df_cn = _load_real_returns(
        str(quantdb_dir / "no_snapshots"),
        "2026-09-10",
        "CN",
        2,
        quantdb_dir=str(quantdb_dir),
    )
    assert not df_cn.empty

    df_hk = _load_real_returns(
        str(quantdb_dir / "no_snapshots"),
        "2026-09-10",
        "HK",
        2,
        quantdb_dir=str(quantdb_dir),
    )
    assert df_hk.empty


def test_quantdb_day_without_parquet_files_fail_closed(tmp_path):
    """dt= 目录存在但无 parquet 文件 → 空结果（不炸、不换窗）。"""
    base = tmp_path / "quantdb"
    for dt in _DATES:
        (base / "1_kline_data" / "daily_forward" / f"dt={dt}").mkdir(parents=True)
    df = _load_real_returns_quantdb("2026-09-10", 2, market="CN", quantdb_dir=str(base))
    assert df.empty and list(df.columns) == ["symbol", "label"]


def test_quantdb_empty_partition_rows_fail_closed(tmp_path):
    """dt= 目录有 parquet 文件但零行 → 透视缺列 → 空结果（防御分支）。"""
    base = tmp_path / "quantdb"
    for dt in _DATES:
        day_dir = base / "1_kline_data" / "daily_forward" / f"dt={dt}"
        day_dir.mkdir(parents=True)
        pd.DataFrame({"symbol": [], "close": []}).to_parquet(
            day_dir / "part.parquet", index=False
        )
    df = _load_real_returns_quantdb("2026-09-10", 2, market="CN", quantdb_dir=str(base))
    assert df.empty


def test_now_utc_is_aware_utc():
    from datetime import timezone

    now = _now_utc()
    assert now.tzinfo is timezone.utc


# ── 特征快照主源（2026-10-08 修复：单日过滤必须在 shift 之后）──────────────

_SNAP_DATES = ["2026-09-10", "2026-09-11", "2026-09-12", "2026-09-13", "2026-09-14"]


def _write_snapshot(path, rows) -> None:
    pd.DataFrame(rows).to_parquet(path, index=False)


def _snap_row(symbol, dt, **extra):
    row = {"instrument": symbol, "trade_date": dt}
    row.update(extra)
    return row


def test_snapshot_loader_shifts_across_dates(tmp_path):
    """修复前：先滤到单日 → 每 symbol 组内只剩 1 行 → shift(-H) 全 NaN → 主源恒空，
    所有回填静默退化到 QuantDB 兜底（且非 CN 市场直接拿不到真实收益）。"""
    rows = []
    for i, dt in enumerate(_SNAP_DATES):
        rows.append(_snap_row("600036.SH", dt, mom_ret_2d=float(i)))
        rows.append(_snap_row("000001.SZ", dt, mom_ret_2d=float(i) * 10))
    # 该 symbol 无 trade_date 当日行 → 过滤后不得出现
    for dt in _SNAP_DATES[2:]:
        rows.append(_snap_row("300750.SZ", dt, mom_ret_2d=99.0))
    _write_snapshot(tmp_path / "model_features_2026.parquet", rows)

    df = _load_real_returns_snapshot(str(tmp_path), "2026-09-10", "CN", 2)
    got = dict(zip(df["symbol"], df["label"], strict=True))
    # T=09-10 的标签 = 09-12 行的 mom_ret_2d（shift(-2)，index 2）；symbol 归一为 prefix
    assert got == {"SH600036": pytest.approx(2.0), "SZ000001": pytest.approx(20.0)}


def test_snapshot_loader_mom_ret_1d_compounds(tmp_path):
    rows = [_snap_row("600036.SH", dt, mom_ret_1d=0.01) for dt in _SNAP_DATES]
    _write_snapshot(tmp_path / "model_features_2026.parquet", rows)

    df = _load_real_returns_snapshot(str(tmp_path), "2026-09-10", "CN", 2)
    assert len(df) == 1
    assert df["symbol"].iloc[0] == "SH600036"
    # 未来两日复利：(1.01)^2 - 1 = 0.0201
    assert df["label"].iloc[0] == pytest.approx(0.0201)


def test_snapshot_loader_compounds_within_symbol_not_across(tmp_path):
    """MEDIUM-8 回归：对 transform 结果做**全局** .shift(-H) 会在排序后的
    symbol 块尾部取到下一个标的的数值（跨标的串味标签）。

    判别位=末个交易日：修正后每个 symbol 的末日行都没有「未来 H 日」→
    整日为空；旧全局 shift 会把第二块（600036.SH）的复利值串给第一块
    （000001.SZ）的末日行 → 冒出一行错标签。
    """
    rows = []
    for dt in _SNAP_DATES:  # 排序后第一块：000001.SZ
        rows.append(_snap_row("000001.SZ", dt, mom_ret_1d=0.05))
    for dt in _SNAP_DATES:  # 第二块：600036.SH
        rows.append(_snap_row("600036.SH", dt, mom_ret_1d=0.01))
    _write_snapshot(tmp_path / "model_features_2026.parquet", rows)

    # 组内配对仍正确（与单 symbol 版同值）
    df = _load_real_returns_snapshot(str(tmp_path), "2026-09-12", "CN", 2)
    got = dict(zip(df["symbol"], df["label"], strict=True))
    assert got == {
        "SZ000001": pytest.approx(0.1025),  # (1.05)^2 - 1
        "SH600036": pytest.approx(0.0201),  # (1.01)^2 - 1
    }
    # 末日：修正后两组都无未来 → 空（旧实现此处串出 0.0201 的假行）
    assert _load_real_returns_snapshot(str(tmp_path), "2026-09-14", "CN", 2).empty


def test_snapshot_symbol_normalized_for_scores_merge(tmp_path):
    """真实快照 symbol 是裸 6 位（实测 model_features_2026.parquet）；
    scores 读取侧（_get_scores_for_date）用 to_prefix → 本侧必须同函数归一，
    否则 merge on=symbol 静默全空、回填出不了质量行。"""
    rows = [_snap_row("000001", dt, mom_ret_1d=0.01) for dt in _SNAP_DATES]
    _write_snapshot(tmp_path / "model_features_2026.parquet", rows)

    df = _load_real_returns_snapshot(str(tmp_path), "2026-09-10", "CN", 2)
    assert list(df["symbol"]) == ["SZ000001"]
    # scores 侧同归一后的 merge 键必须命中
    scores = pd.DataFrame({"symbol": ["SZ000001"], "score": [1.0]})
    assert len(scores.merge(df, on="symbol", how="inner")) == 1


def test_snapshot_loader_no_return_columns_fail_closed(tmp_path):
    rows = [_snap_row("600036.SH", dt) for dt in _SNAP_DATES]
    _write_snapshot(tmp_path / "model_features_2026.parquet", rows)
    df = _load_real_returns_snapshot(str(tmp_path), "2026-09-10", "CN", 2)
    assert df.empty and list(df.columns) == ["symbol", "label"]


def test_snapshot_loader_tail_rows_without_future_dropped(tmp_path):
    # 只有两行、horizon=2 → 每行都没有未来 → 空
    rows = [
        _snap_row("600036.SH", "2026-09-10", mom_ret_2d=1.0),
        _snap_row("600036.SH", "2026-09-11", mom_ret_2d=2.0),
    ]
    _write_snapshot(tmp_path / "model_features_2026.parquet", rows)
    assert _load_real_returns_snapshot(str(tmp_path), "2026-09-10", "CN", 2).empty


def test_outer_prefers_snapshot_when_available(tmp_path):
    """快照有数据时直接用快照，不回退 QuantDB（quantdb_dir 指向不存在目录也无妨）。"""
    rows = [_snap_row("600036.SH", dt, mom_ret_1d=0.05) for dt in _SNAP_DATES]
    _write_snapshot(tmp_path / "model_features_2026.parquet", rows)
    df = _load_real_returns(
        str(tmp_path), "2026-09-10", "CN", 1, quantdb_dir=str(tmp_path / "no_qdb")
    )
    assert not df.empty
    assert df["label"].iloc[0] == pytest.approx(0.05)


def test_resolve_parquet_path_precedence(tmp_path):
    year_file = tmp_path / "model_features_2026.parquet"
    year_file.write_bytes(b"")
    # 无市场专属文件 → 年份文件兜底
    assert _resolve_parquet_path(str(tmp_path), "2026-09-10", "CN") == str(year_file)
    assert _resolve_parquet_path(str(tmp_path), "2026-09-10", "HK") == str(year_file)
    # 市场专属文件存在 → 优先
    hk_file = tmp_path / "model_features_hk.parquet"
    hk_file.write_bytes(b"")
    assert _resolve_parquet_path(str(tmp_path), "2026-09-10", "HK") == str(hk_file)
    # 都不存在 → None
    assert _resolve_parquet_path(str(tmp_path), "2027-01-05", "CRYPTO") is None


# ── 纯函数（日期归一 / IC 计算）────────────────────────────────────────────


def test_as_date_variants():
    from datetime import date, datetime, timezone

    assert _as_date("2026-09-10") == date(2026, 9, 10)
    assert _as_date("2026-09-10T15:00:00+08:00") == date(2026, 9, 10)
    assert _as_date(date(2026, 9, 10)) == date(2026, 9, 10)
    assert _as_date(datetime(2026, 9, 10, 23, 30, tzinfo=timezone.utc)) == date(
        2026, 9, 10
    )


def test_rank_ic_monotonic_and_guards():
    n = 30
    df = pd.DataFrame({"score": range(n), "label": [x * 2.5 for x in range(n)]})
    assert _rank_ic_from_scores(df) == pytest.approx(1.0)
    # 样本不足（<10 有效对）→ NaN
    assert np.isnan(_rank_ic_from_scores(df.head(9)))
    # 常数预测（std=0）→ NaN
    const = pd.DataFrame({"score": [1.0] * 12, "label": list(range(12))})
    assert np.isnan(_rank_ic_from_scores(const))
    # dropna 后不足 10 → NaN
    holey = pd.DataFrame({"score": [1.0] * 9 + [None], "label": list(range(10))})
    assert np.isnan(_rank_ic_from_scores(holey))


def test_pearson_ic_linear_and_guards():
    n = 20
    df = pd.DataFrame({"score": range(n), "label": [3 * x + 1 for x in range(n)]})
    assert _pearson_ic_from_scores(df) == pytest.approx(1.0)
    assert np.isnan(_pearson_ic_from_scores(df.head(2)))  # <3 → NaN
    const = pd.DataFrame({"score": [0.0] * 5, "label": list(range(5))})
    assert np.isnan(_pearson_ic_from_scores(const))
