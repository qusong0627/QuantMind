#!/usr/bin/env python3
"""三库经典因子计算引擎: Alpha 101 + GTJA 191 + Alpha 158 → 6_ml_datasets/alpha_library

数据契约（单位契约 UNIT CONTRACT，全部实测验证）:
  daily_forward: open/high/low/close=元(前复权), volume=股, amount=万元(未复权)
  ⚠️ 复权一致性（2026-08-28 实测）: 价格前复权但 amount/volume 未复权，
     故 vwap = amount*10000/volume × f，f = close_forward/close_unadjusted（复权因子），
     amount/advN 同样乘 f 统一到复权基准；volume 保持股数不变。
  valuation:    total_mv/float_mv=元, total_capital=股
  行业分类:     instrument_detail.rs_hycode_sim（128 细分行业，中性化用）

无未来函数铁律:
  1. 所有滚动窗口 min_periods=窗口长度，窗口闭口在当日（含当日，不含未来）
  2. 禁止负滞后（无 shift(-k)）
  3. 横截面操作（rank/scale/indneutralize）只用当日截面
  4. 未来收益标签绝不写入特征表（训练侧单独计算）
  5. 缺失自然传播 NaN，禁止 ffill/bfill
  6. 冒烟测试含"截断不变性"断言：t 日因子值不随 t 之后数据增减而改变

用法:
  python backend/scripts/alpha_library_factors.py --smoke            # 冒烟: 50只×400天
  python backend/scripts/alpha_library_factors.py                    # 全量（已有分区跳过）
  python backend/scripts/alpha_library_factors.py --start-year 2016  # 指定起始年
  python backend/scripts/alpha_library_factors.py --max-symbols 200  # 仅前 N 只（调试）
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = PROJECT_ROOT / "data" / "quantdb"
OUT_ROOT = DATA_ROOT / "6_ml_datasets" / "alpha_library"
PLAN = OUT_ROOT / "PLAN.md"

log = logging.getLogger("alpha_library")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

WAN = 10_000.0  # amount 万元 → 元
QUANTDB = str(DATA_ROOT)
KLINE = f"{QUANTDB}/1_kline_data/daily_forward/dt=*/data.parquet"
INDEX = f"{QUANTDB}/1_kline_data/index_daily/dt=*/data.parquet"
VALUATION = f"{QUANTDB}/5_technical_derived/valuation/dt=*/data.parquet"
INSTRUMENT = f"{QUANTDB}/2_base_sector/instrument_detail/*.parquet"

# ---------------------------------------------------------------------------
# 1. 数据加载（单位契约在此落地）
# ---------------------------------------------------------------------------


def load_kline(start_year: int | None = None, max_symbols: int | None = None) -> dict[str, pd.DataFrame]:
    """读 daily_forward 全历史 → 每字段一张宽表 (index=time, columns=symbol)。"""
    con = duckdb.connect()
    q = f"SELECT symbol, time, open, high, low, close, volume, amount FROM read_parquet('{KLINE}')"
    if start_year:
        q += f" WHERE year(time) >= {start_year}"
    df = con.execute(q).fetchdf()
    con.close()
    df = df.drop_duplicates(subset=["symbol", "time"])
    df["time"] = pd.to_datetime(df["time"])
    if max_symbols:
        syms = sorted(df["symbol"].unique())[:max_symbols]
        df = df[df["symbol"].isin(syms)]
    df = df.sort_values(["symbol", "time"])

    W: dict[str, pd.DataFrame] = {}
    for col in ["open", "high", "low", "close", "volume"]:
        W[col] = df.pivot(index="time", columns="symbol", values=col)
    # 金额敏感: amount 万元 → 元
    W["amount"] = df.pivot(index="time", columns="symbol", values="amount") * WAN
    # ── 复权一致性修正（2026-08-28 实测发现）────────────────────────────
    # daily_forward 价格是前复权，但 volume/amount 是未复权基准：
    # amount/volume 直接算出的 vwap 与复权 close 系统性偏差（历史越远越大）。
    # 用 close_forward/close_unadjusted 得到复权因子 f，把 amount/vwap/adv 统一到复权基准。
    con = duckdb.connect()
    uq = (
        f"SELECT symbol, time, close FROM read_parquet('{QUANTDB}/1_kline_data/"
        f"daily_unadjusted/dt=*/data.parquet')"
    )
    uclose = con.execute(uq).fetchdf()
    con.close()
    uclose["time"] = pd.to_datetime(uclose["time"])
    raw_close = uclose.pivot(index="time", columns="symbol", values="close")
    raw_close = raw_close.reindex(index=W["close"].index, columns=W["close"].columns)
    adj_factor = (W["close"] / raw_close).ffill(limit=10).fillna(1.0)
    W["adj_factor"] = adj_factor
    W["amount"] = W["amount"] * adj_factor  # 金额统一到复权基准
    # vwap 推算（单位契约核心）——复权基准
    W["vwap"] = W["amount"] / W["volume"]
    W["prev_close"] = W["close"].shift(1)
    W["ret"] = W["close"] / W["prev_close"] - 1.0
    W["log_vol"] = np.log(W["volume"] + 1.0)
    # advN: 金额 N 日均值（元/日）
    W["adv5"] = W["amount"].rolling(5, min_periods=5).mean()
    W["adv10"] = W["amount"].rolling(10, min_periods=10).mean()
    W["adv15"] = W["amount"].rolling(15, min_periods=15).mean()
    W["adv20"] = W["amount"].rolling(20, min_periods=20).mean()
    W["adv30"] = W["amount"].rolling(30, min_periods=30).mean()
    W["adv40"] = W["amount"].rolling(40, min_periods=40).mean()
    W["adv50"] = W["amount"].rolling(50, min_periods=50).mean()
    W["adv60"] = W["amount"].rolling(60, min_periods=60).mean()
    W["adv81"] = W["amount"].rolling(81, min_periods=81).mean()
    W["adv120"] = W["amount"].rolling(120, min_periods=120).mean()
    W["adv150"] = W["amount"].rolling(150, min_periods=150).mean()
    W["adv180"] = W["amount"].rolling(180, min_periods=180).mean()
    log.info(
        "kline loaded: %d days × %d symbols (%.1fM rows), %s ~ %s",
        len(W["close"]), W["close"].shape[1], df.shape[0] / 1e6,
        W["close"].index[0].date(), W["close"].index[-1].date(),
    )
    global _TEMPLATE
    _TEMPLATE = W["close"]
    return W


def load_benchmark() -> tuple[pd.Series, pd.Series]:
    """基准指数 000001.SH（上证综指）open/close，对齐到 kline 日期。"""
    con = duckdb.connect()
    q = (
        f"SELECT time, open, close FROM read_parquet('{INDEX}') "
        f"WHERE symbol='000001.SH' ORDER BY time"
    )
    df = con.execute(q).fetchdf()
    con.close()
    df["time"] = pd.to_datetime(df["time"])
    return (
        df.set_index("time")["open"].astype(float),
        df.set_index("time")["close"].astype(float),
    )


def load_industry_map() -> pd.Series:
    """Symbol → rs_hycode_sim（128 细分行业）。"""
    con = duckdb.connect()
    df = con.execute(
        f"SELECT Symbol AS symbol, rs_hycode_sim AS ind FROM read_parquet('{INSTRUMENT}')"
    ).fetchdf()
    con.close()
    df = df.dropna(subset=["ind"]).drop_duplicates("symbol")
    return df.set_index("symbol")["ind"].astype(str)


def load_total_mv(dates: pd.Index, symbols: list[str]) -> pd.DataFrame:
    """valuation.total_mv（元）宽表，reindex 对齐 kline。"""
    con = duckdb.connect()
    df = con.execute(
        f"SELECT symbol, time, total_mv FROM read_parquet('{VALUATION}')"
    ).fetchdf()
    con.close()
    df["time"] = pd.to_datetime(df["time"])
    wide = df.pivot(index="time", columns="symbol", values="total_mv")
    wide = wide.reindex(index=dates, columns=symbols)
    return wide


# ---------------------------------------------------------------------------
# 2. 基础算子（全部宽表向量化；滑窗类用 numpy，按列分块控内存）
# ---------------------------------------------------------------------------

def _sliding(mat: np.ndarray, w: int, fn, chunk: int = 128) -> np.ndarray:
    """对每列做滑窗: sliding_window_view 输出 (n-w+1, n_cols, w)，
    fn(view) → (n-w+1, n_cols)。掩码按列（axis=2）：只该列窗口含 NaN 才置 NaN，
    绝不能让同 chunk 其他列的 NaN 污染本列（2026-08-29 修复）。"""
    n, c = mat.shape
    out = np.full((n, c), np.nan)
    if n < w:
        return out
    for i in range(0, c, chunk):
        j = min(i + chunk, c)
        v = np.lib.stride_tricks.sliding_window_view(mat[:, i:j], w, axis=0)
        vals = fn(v)
        valid = ~np.isnan(v).any(axis=2)  # (n-w+1, n_cols)：按列掩码
        out[w - 1:, i:j] = np.where(valid, vals, np.nan)
    return out


_TEMPLATE: pd.DataFrame | None = None  # ndarray → DataFrame 强转模板（load_kline 时设置）


def _as_df(x):
    """算子统一入口：ndarray 强转回宽表 DataFrame（np.where/标量混合产生）。"""
    if isinstance(x, np.ndarray):
        assert _TEMPLATE is not None, "_TEMPLATE not set"
        return _wrap(_TEMPLATE, x)
    return x


def _wrap(df: pd.DataFrame, mat: np.ndarray) -> pd.DataFrame:
    return pd.DataFrame(mat, index=df.index, columns=df.columns)


def TSRANK(df: pd.DataFrame, w: int) -> pd.DataFrame:
    """窗口内最后一位的百分位排名（对齐 rankdata 平均法）。"""
    df = _as_df(df)

    def f(v):
        last = v[:, :, -1][:, :, None]
        less = (v < last).sum(axis=2)
        equal = (v == last).sum(axis=2)
        return (less + (equal + 1.0) / 2.0) / w

    return _wrap(df, _sliding(df.values, w, f))


def TSARGMAX(df: pd.DataFrame, w: int) -> pd.DataFrame:
    return _wrap(_as_df(df), _sliding(_as_df(df).values, w, lambda v: v.argmax(axis=2).astype(float)))


def TSARGMIN(df: pd.DataFrame, w: int) -> pd.DataFrame:
    return _wrap(_as_df(df), _sliding(_as_df(df).values, w, lambda v: v.argmin(axis=2).astype(float)))


def DECAY(df: pd.DataFrame, w: int) -> pd.DataFrame:
    """线性衰减加权和，权重递增（近大远小，func_decaylinear 语义）。"""
    df = _as_df(df)
    wt = np.arange(1, w + 1, dtype=float)
    wt /= wt.sum()
    return _wrap(df, _sliding(df.values, w, lambda v: v @ wt))


def QTL(df: pd.DataFrame, w: int, q: float) -> pd.DataFrame:
    df = _as_df(df)
    return _wrap(df, _sliding(df.values, w, lambda v: np.quantile(v, q, axis=2)))


def HIGHDAY(df: pd.DataFrame, w: int) -> pd.DataFrame:
    """距窗口内最高价的天数（0=当日）。"""
    df = _as_df(df)
    return _wrap(df, _sliding(df.values, w, lambda v: (w - 1 - v.argmax(axis=2)).astype(float)))


def LOWDAY(df: pd.DataFrame, w: int) -> pd.DataFrame:
    df = _as_df(df)
    return _wrap(df, _sliding(df.values, w, lambda v: (w - 1 - v.argmin(axis=2)).astype(float)))


def REG(df: pd.DataFrame, w: int) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """线性回归 (x=1..w, y=close)：slope, rsquare, resi(末点残差)。"""
    df = _as_df(df)
    X = np.arange(1, w + 1, dtype=float)
    sx, sxx = X.sum(), (X * X).sum()

    def f(v):
        sy = v.sum(axis=2)
        syy = (v * v).sum(axis=2)
        sxy = v @ X
        denom = w * sxx - sx * sx
        slope = (w * sxy - sx * sy) / denom
        intercept = (sy - slope * sx) / w
        rsqr = (w * sxy - sx * sy) ** 2 / (denom * (w * syy - sy * sy))
        resi = v[:, :, -1] - (intercept + slope * w)
        return slope, rsqr, resi

    m = df.values
    n, c = m.shape
    out = [np.full((n, c), np.nan) for _ in range(3)]
    if n >= w:
        for i in range(0, c, 128):
            j = min(i + 128, c)
            v = np.lib.stride_tricks.sliding_window_view(m[:, i:j], w, axis=0)
            s, r2, rs = f(v)
            valid = ~np.isnan(v).any(axis=2)  # 按列掩码
            for o, val in zip(out, (s, r2, rs)):
                o[w - 1:, i:j] = np.where(valid, val, np.nan)
    return (_wrap(df, out[0]), _wrap(df, out[1]), _wrap(df, out[2]))


def SUM(df: pd.DataFrame, w: int) -> pd.DataFrame:
    return _as_df(df).rolling(w, min_periods=w).sum()


def PROD(df: pd.DataFrame, w: int) -> pd.DataFrame:
    return _as_df(df).rolling(w, min_periods=w).apply(np.prod, raw=True)


def MEAN(df: pd.DataFrame, w: int) -> pd.DataFrame:
    return _as_df(df).rolling(w, min_periods=w).mean()


def STD(df: pd.DataFrame, w: int) -> pd.DataFrame:
    return _as_df(df).rolling(w, min_periods=w).std()  # ddof=1 对齐参考实现


def MAX(df: pd.DataFrame, w: int) -> pd.DataFrame:
    return _as_df(df).rolling(w, min_periods=w).max()


def MIN(df: pd.DataFrame, w: int) -> pd.DataFrame:
    return _as_df(df).rolling(w, min_periods=w).min()


def CORR(x: pd.DataFrame, y: pd.DataFrame, w: int) -> pd.DataFrame:
    return _as_df(x).rolling(w, min_periods=w).corr(_as_df(y))


def COV(x: pd.DataFrame, y: pd.DataFrame, w: int) -> pd.DataFrame:
    return _as_df(x).rolling(w, min_periods=w).cov(_as_df(y))


def EWMA(df: pd.DataFrame, alpha: float) -> pd.DataFrame:
    """pd.ewma 默认 adjust=True 语义。"""
    return _as_df(df).ewm(alpha=alpha, adjust=True).mean()


def EMA_SPAN(df: pd.DataFrame, span: float, adjust: bool = False) -> pd.DataFrame:
    """SMA(X,N,2) 类递归平滑 → ewm(span=N-2, adjust=False)。"""
    return _as_df(df).ewm(span=span, adjust=adjust).mean()


def R(df: pd.DataFrame) -> pd.DataFrame:
    """横截面排名（当日截面，axis=1），对齐参考实现 rank(axis=1, pct=True)。"""
    return _as_df(df).rank(axis=1, pct=True)


def RTS(df: pd.DataFrame) -> pd.DataFrame:
    """时间序列排名（每列独立，axis=0）——用于少数 GTJA 因子的时间排名。"""
    return _as_df(df).rank(axis=0, pct=True)


def SCALE(df: pd.DataFrame, k: float = 1.0) -> pd.DataFrame:
    """按日缩放使 sum(|x|) = k（当日截面）。inf 视为缺失。"""
    df = _as_df(df).replace([np.inf, -np.inf], np.nan)
    s = df.abs().sum(axis=1).replace(0, np.nan)
    return df.div(s, axis=0) * k


def IN(df: pd.DataFrame, ind_onehot: np.ndarray) -> pd.DataFrame:
    """行业中性化: 当日截面内减行业均值（128 行业）。ind_onehot: (n_sym, n_ind) 列归一化。
    ⚠️ inf 必须先行转为 NaN（否则 inf×0=NaN 会污染整行行业均值，2026-08-29 实测）。"""
    df = _as_df(df).replace([np.inf, -np.inf], np.nan)
    vals = df.values
    if np.isnan(vals).all():
        return df.copy()
    # 每行(每日)行业均值 = vals @ onehot → 每符号回填 = mean_mat @ onehot.T
    mean_ind = vals @ ind_onehot  # (n_days, n_ind)；NaN 会污染，按列均值替代
    # NaN 处理：把 NaN 视为 0 参与，行业均值按有效计数归一（onehot 已按 1/count 归一）
    valid = ~np.isnan(vals)
    vals_filled = np.where(valid, vals, 0.0)
    sum_ind = vals_filled @ ind_onehot  # (n_days, n_ind) 分子
    cnt_ind = valid.astype(float) @ ind_onehot  # 有效计数加权
    mean_ind = np.divide(sum_ind, cnt_ind, out=np.zeros_like(sum_ind), where=cnt_ind > 0)
    sym_mean = mean_ind @ ind_onehot.T  # (n_days, n_sym)
    out = vals - sym_mean
    return _wrap(df, out)


# ---------------------------------------------------------------------------
# 3. Alpha 158（158 个，连续值、无横截面排名）
# ---------------------------------------------------------------------------

def compute_a158(W: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    global _TEMPLATE
    o, h, l, c, v, vwap = W["open"], W["high"], W["low"], W["close"], W["volume"], W["vwap"]
    _TEMPLATE = c
    dc, dv = W["prev_close"], W["volume"].shift(1)
    eps = 1e-12
    F: dict[str, pd.DataFrame] = {}
    F["a158_KMID"] = (c - o) / o
    F["a158_KLEN"] = (h - l) / o
    F["a158_KMID2"] = (c - o) / (h - l + eps)
    F["a158_KUP"] = (h - np.maximum(o, c)) / o
    F["a158_KUP2"] = (h - np.maximum(o, c)) / (h - l + eps)
    F["a158_KLOW"] = (np.minimum(o, c) - l) / o
    F["a158_KLOW2"] = (np.minimum(o, c) - l) / (h - l + eps)
    F["a158_KSFT"] = (2 * c - h - l) / o
    F["a158_KSFT2"] = (2 * c - h - l) / (h - l + eps)
    F["a158_OPEN0"] = o / c
    F["a158_HIGH0"] = h / c
    F["a158_LOW0"] = l / c
    F["a158_VWAP0"] = vwap / c

    def kw(name: str, base: str) -> list[str]:
        return [f"{name}{w}" for w in (5, 10, 20, 30, 60)]

    WINDOWS = (5, 10, 20, 30, 60)
    for w in WINDOWS:
        F[f"a158_ROC{w}"] = W["close"].shift(w) / c
        F[f"a158_MA{w}"] = MEAN(c, w) / c
        F[f"a158_STD{w}"] = STD(c, w) / c
        F[f"a158_MAX{w}"] = MAX(h, w) / c
        F[f"a158_MIN{w}"] = MIN(l, w) / c
        F[f"a158_QTLU{w}"] = QTL(c, w, 0.8) / c
        F[f"a158_QTLD{w}"] = QTL(c, w, 0.2) / c
        F[f"a158_RANK{w}"] = TSRANK(c, w)
        F[f"a158_RSV{w}"] = (c - MIN(l, w)) / (MAX(h, w) - MIN(l, w) + eps)
        F[f"a158_IMAX{w}"] = TSARGMAX(h, w) / w
        F[f"a158_IMIN{w}"] = TSARGMIN(l, w) / w
        F[f"a158_IMXD{w}"] = (TSARGMAX(h, w) - TSARGMIN(l, w)) / w
        F[f"a158_CORR{w}"] = CORR(c, W["log_vol"], w)
        F[f"a158_CORD{w}"] = CORR(c / dc, np.log(v / dv + 1.0), w)
        F[f"a158_CNTP{w}"] = (c > dc).rolling(w, min_periods=w).mean()
        F[f"a158_CNTN{w}"] = (c < dc).rolling(w, min_periods=w).mean()
        F[f"a158_CNTD{w}"] = F[f"a158_CNTP{w}"] - F[f"a158_CNTN{w}"]
        up = np.maximum(c - dc, 0.0)
        dn = np.maximum(dc - c, 0.0)
        den = SUM((c - dc).abs(), w) + eps
        sp = SUM(up, w) / den
        sn = SUM(dn, w) / den
        F[f"a158_SUMP{w}"] = sp
        F[f"a158_SUMN{w}"] = sn
        F[f"a158_SUMD{w}"] = sp - sn
        F[f"a158_VMA{w}"] = MEAN(v, w) / (v + eps)
        F[f"a158_VSTD{w}"] = STD(v, w) / (v + eps)
        wv = (c / dc - 1.0).abs() * v
        F[f"a158_WVMA{w}"] = STD(wv, w) / (MEAN(wv, w) + eps)
        vup = np.maximum(v - dv, 0.0)
        vdn = np.maximum(dv - v, 0.0)
        vden = SUM((v - dv).abs(), w) + eps
        vsp = SUM(vup, w) / vden
        vsn = SUM(vdn, w) / vden
        F[f"a158_VSUMP{w}"] = vsp
        F[f"a158_VSUMN{w}"] = vsn
        F[f"a158_VSUMD{w}"] = vsp - vsn
    slope, rsqr, resi = REG(c, 5)
    F["a158_BETA5"], F["a158_RSQR5"], F["a158_RESI5"] = slope / c, rsqr, resi / c
    for w in (10, 20, 30, 60):
        slope, rsqr, resi = REG(c, w)
        F[f"a158_BETA{w}"] = slope / c
        F[f"a158_RSQR{w}"] = rsqr
        F[f"a158_RESI{w}"] = resi / c
    assert len(F) == 158, f"a158 count={len(F)}"
    return F


# ---------------------------------------------------------------------------
# 4. Alpha 101（101 个）
# ---------------------------------------------------------------------------

def compute_a101(W: dict[str, pd.DataFrame], ind_map: pd.Series, mv: pd.DataFrame) -> dict[str, pd.DataFrame]:
    global _TEMPLATE
    o, h, l, c, v = W["open"], W["high"], W["low"], W["close"], W["volume"]
    _TEMPLATE = c
    vwap, dc, ret = W["vwap"], W["prev_close"], W["ret"]
    A = W["amount"]
    a20 = W["adv20"]
    F: dict[str, pd.DataFrame] = {}

    def put(n: int, x) -> None:
        if isinstance(x, np.ndarray):
            x = _wrap(c, x)
        F[f"a101_{n:03d}"] = x.replace([np.inf, -np.inf], np.nan)

    inner1 = _wrap(c, np.where((ret < 0).values, STD(ret, 20).values, c.values))
    put(1, R(TSARGMAX(inner1 ** 2, 5)) - 0.5)
    put(2, -1.0 * CORR(R(W["log_vol"].diff(2)), R((c - o) / o), 6))
    put(3, -1.0 * CORR(R(o), R(v), 10))
    put(4, -1.0 * TSRANK(R(l), 9))
    put(5, R(o - MEAN(vwap, 10)) * (-(R(c - vwap).abs())))
    put(6, -1.0 * CORR(o, v, 10))
    cond7 = a20 < v
    put(7, np.where(cond7, -TSRANK((c.diff(7)).abs(), 60) * np.sign(c.diff(7)), -1.0))
    put(8, -R(SUM(o, 5) * SUM(ret, 5) - (SUM(o, 5) * SUM(ret, 5)).shift(10)))
    d1 = c.diff(1)
    tsmin5, tsmax5 = d1.rolling(5, min_periods=5).min(), d1.rolling(5, min_periods=5).max()
    put(9, np.where((tsmin5 > 0) | (tsmax5 < 0), d1, -d1))
    tsmin4, tsmax4 = d1.rolling(4, min_periods=4).min(), d1.rolling(4, min_periods=4).max()
    put(10, np.where((tsmin4 > 0) | (tsmax4 < 0), d1, -d1))
    put(11, (R(MAX(vwap - c, 3)) + R(MIN(vwap - c, 3))) * R(v.diff(3)))
    put(12, np.sign(v.diff(1)) * (-c.diff(1)))
    put(13, -1.0 * R(COV(R(c), R(v), 5)))
    put(14, -1.0 * R(ret.diff(3)) * CORR(o, v, 10))
    put(15, -1.0 * SUM(R(CORR(R(h), R(v), 3)), 3))
    put(16, -1.0 * R(COV(R(h), R(v), 5)))
    put(17, -R(TSRANK(c, 10)) * R(c.diff(1).diff(1)) * R(TSRANK(v / a20, 5)))
    put(18, -R(STD((c - o).abs(), 5) + (c - o) + CORR(c, o, 10)))
    put(19, -np.sign((c - c.shift(7)) + c.diff(7)) * (1 + R(1 + SUM(ret, 250))))
    put(20, -R(o - h.shift(1)) * R(o - c.shift(1)) * R(o - l.shift(1)))
    sma8, std8, sma2 = MEAN(c, 8), STD(c, 8), MEAN(c, 2)
    put(21, np.select(
        [sma8 + std8 < sma2, sma2 < sma8 - std8, v / a20 >= 1],
        [-1.0, 1.0, 1.0], default=-1.0))
    put(22, -CORR(h, v, 5).diff(5) * R(STD(c, 20)))
    put(23, np.where(MEAN(h, 20) < h, -h.diff(2), 0.0))
    cond24 = (MEAN(c, 100).diff(100) / c.shift(100)) <= 0.05
    put(24, np.where(cond24, -(c - MIN(c, 100)), -c.diff(3)))
    put(25, R(-ret * a20 * vwap * (h - c)))
    put(26, -MAX(CORR(TSRANK(v, 5), TSRANK(h, 5), 5), 3))
    put(27, np.where(R(MEAN(CORR(R(v), R(vwap), 6), 2) / 2) > 0.5, -1.0, 1.0))
    put(28, SCALE(CORR(a20, l, 5) + (h + l) / 2 - c))
    put(29, MIN(R(R(SCALE(np.log(SUM(R(R(-R(c.diff(5)))), 2))))), 5) + TSRANK((-ret).shift(6), 5))
    inner30 = np.sign(d1) + np.sign(d1.shift(1)) + np.sign(d1.shift(2))
    put(30, ((1.0 - R(inner30)) * SUM(v, 5)) / SUM(v, 20))
    put(31, R(R(R(DECAY(-R(R(c.diff(10))), 10)))) + R(-c.diff(3)) + np.sign(SCALE(CORR(a20, l, 12))))
    put(32, SCALE(MEAN(c, 7) / 7 - c) + 20 * SCALE(CORR(vwap, c.shift(5), 230)))
    put(33, R(-1.0 + o / c))
    put(34, R(1 - R(STD(ret, 2) / STD(ret, 5)) + 1 - R(c.diff(1))))
    put(35, TSRANK(v, 32) * (1 - TSRANK(c + h - l, 16)) * (1 - TSRANK(ret, 32)))
    put(36, 2.21 * R(CORR(c - o, v.shift(1), 15)) + 0.7 * R(o - c)
             + 0.73 * R(TSRANK((-ret).shift(6), 5)) + R((CORR(vwap, a20, 6)).abs())
             + 0.6 * R((MEAN(c, 200) / 200 - o) * (c - o)))
    put(37, R(CORR((o - c).shift(1), c, 200)) + R(o - c))
    put(38, -R(TSRANK(o, 10)) * R(c / o))
    put(39, -R(c.diff(7) * (1 - R(DECAY(v / a20, 9)))) * (1 + R(MEAN(ret, 250))))
    put(40, -1.0 * R(STD(h, 10)) * CORR(h, v, 10))
    put(41, np.sqrt(h * l) - vwap)
    put(42, R(vwap - c) / R(vwap + c))
    put(43, TSRANK(v / a20, 20) * TSRANK(-c.diff(7), 8))
    put(44, -1.0 * CORR(h, R(v), 5))
    put(45, -1.0 * R(MEAN(c.shift(5), 20)) * CORR(c, v, 2) * R(CORR(SUM(c, 5), SUM(c, 20), 2)))
    inner46 = (c.shift(20) - c.shift(10)) / 10 - (c.shift(10) - c) / 10
    put(46, np.select([inner46 > 0.25, inner46 < 0], [-1.0, 1.0], default=-d1))
    put(47, R(1 / c) * v / a20 * (h * R(h - c)) / MEAN(h, 5) * 5 - R(vwap - vwap.shift(5)))
    put(49, np.where(inner46 < -0.1, 1.0, -d1))
    put(50, -MAX(R(CORR(R(v), R(vwap), 5)), 5))
    put(51, np.where(inner46 < -0.05, 1.0, -d1))
    put(52, (-MIN(l, 5).diff(5)) * R((SUM(ret, 240) - SUM(ret, 20)) / 220) * TSRANK(v, 5))
    put(53, -((((c - l) - (h - c)) / (c - l)).diff(9)))
    put(54, -((l - c) * o ** 5) / ((l - h) * c ** 5))
    rsv12 = (c - MIN(l, 12)) / (MAX(h, 12) - MIN(l, 12))
    put(55, -1.0 * CORR(R(rsv12), R(v), 6))
    put(57, -(c - vwap) / DECAY(R(TSARGMAX(c, 30)), 2))
    put(60, -(2 * SCALE(R(((c - l) - (h - c)) * v / (h - l))) - SCALE(R(TSARGMAX(c, 10)))))
    put(61, (R(vwap - MIN(vwap, 16)) < R(CORR(vwap, W["adv180"], 18))).astype(float))
    put(62, ((R(CORR(vwap, MEAN(a20, 22), 10)) < R(R(o) * 2 < R((h + l) / 2) + R(h))) * -1).astype(float))
    put(64, ((R(CORR(MEAN(o * 0.178 + l * 0.822, 13), MEAN(W["adv120"], 13), 17))
              < R(((h + l) / 2 * 0.178 + vwap * 0.822).diff(4))) * -1).astype(float))
    put(65, ((R(CORR(o * 0.008 + vwap * 0.992, MEAN(W["adv60"], 9), 6)) < R(o - MIN(o, 14))) * -1).astype(float))
    put(66, (R(DECAY(vwap.diff(4), 7)) + TSRANK(DECAY((l - vwap) / (o - (h + l) / 2), 11), 7)) * -1)
    put(68, ((TSRANK(CORR(R(h), R(W["adv15"]), 9), 14) < R((c * 0.518 + l * 0.482).diff(1))) * -1).astype(float))
    put(71, np.maximum(
        TSRANK(DECAY(CORR(TSRANK(c, 3), TSRANK(W["adv180"], 12), 18), 4), 16),
        TSRANK(DECAY(R((l + o) - (vwap * 2)) ** 2, 16), 4)))
    put(72, R(DECAY(CORR((h + l) / 2, W["adv40"], 9), 10)) / R(DECAY(CORR(TSRANK(vwap, 4), TSRANK(v, 19), 7), 3)))
    put(73, np.maximum(R(DECAY(vwap.diff(5), 3)),
                       TSRANK(DECAY((o * 0.147 + l * 0.853).diff(2) / (o * 0.147 + l * 0.853) * -1, 3), 17)) * -1)
    put(74, ((R(CORR(c, MEAN(W["adv30"], 37), 15)) < R(CORR(R(h * 0.026 + vwap * 0.974), R(v), 11))) * -1).astype(float))
    put(75, (R(CORR(vwap, v, 4)) < R(CORR(R(l), R(W["adv50"]), 12))).astype(float))
    put(77, np.minimum(R(DECAY(((h + l) / 2 + h) - (vwap + h), 20)),
                        R(DECAY(CORR((h + l) / 2, W["adv40"], 3), 6))))
    put(78, R(CORR(SUM(l * 0.353 + vwap * 0.647, 20), SUM(W["adv40"], 20), 7)) ** R(CORR(R(vwap), R(v), 6)))
    prod81 = PROD(R(CORR(vwap, SUM(W["adv10"], 50), 8) ** 4), 15)
    put(81, (R(np.log(prod81)) < R(CORR(R(vwap), R(v), 5))) * -1)
    amp5 = (h - l) / (MEAN(c, 5) / 5)
    put(83, R(amp5.shift(2)) * R(R(v)) / (amp5 / (vwap - c)))
    put(84, TSRANK(vwap - MAX(vwap, 15), 21) ** c.diff(5))
    put(85, R(CORR(h * 0.877 + c * 0.123, W["adv30"], 10)) ** R(CORR(TSRANK((h + l) / 2, 4), TSRANK(v, 10), 7)))
    put(86, ((TSRANK(CORR(c, MEAN(a20, 15), 6), 20) < R(c - vwap)) * -1).astype(float))
    put(88, np.minimum(
        R(DECAY(R(o) + R(l) - R(h) - R(c), 8)),
        TSRANK(DECAY(CORR(TSRANK(c, 8), TSRANK(W["adv60"], 21), 8), 7), 3)))
    put(92, np.minimum(
        TSRANK(DECAY(((h + l) / 2 + c) < (l + o), 15), 19),
        TSRANK(DECAY(CORR(R(l), R(W["adv30"]), 8), 7), 7)))
    put(94, R(vwap - MIN(vwap, 12)) ** TSRANK(CORR(TSRANK(vwap, 20), TSRANK(W["adv60"], 4), 18), 3) * -1)
    put(95, (R(o - MIN(o, 12)) < TSRANK(R(CORR(MEAN((h + l) / 2, 19), MEAN(W["adv40"], 19), 13)) ** 5, 12)).astype(float))
    put(96, np.maximum(
        TSRANK(DECAY(CORR(R(vwap), R(v), 4), 4), 8),
        TSRANK(DECAY(TSARGMAX(CORR(TSRANK(c, 7), TSRANK(W["adv60"], 4), 4), 13), 14), 13)) * -1)
    put(98, R(DECAY(CORR(vwap, MEAN(W["adv5"], 26), 5), 7))
             - R(DECAY(TSRANK(TSARGMIN(CORR(R(o), R(W["adv15"]), 21), 9), 7), 8)))
    put(99, ((R(CORR(SUM((h + l) / 2, 20), SUM(W["adv60"], 20), 9)) < R(CORR(l, v, 6))) * -1).astype(float))
    put(101, (c - o) / (h - l + 0.001))

    # ---- 行业中性化族（19 个）: 128 行业 indneutralize ----
    ind = ind_map
    syms = list(c.columns)
    codes = ind.reindex(syms).fillna("UNKNOWN")
    cats = sorted(set(codes))
    onehot = np.zeros((len(syms), len(cats)))
    for i, s in enumerate(syms):
        onehot[i, cats.index(codes[s])] = 1.0
    onehot = onehot / onehot.sum(axis=0, keepdims=True)  # 列归一 → 均值矩阵

    def wi(x: pd.DataFrame) -> pd.DataFrame:
        return IN(x, onehot)

    d1r = c.diff(1)
    put(48, wi(CORR(d1r, d1r.shift(1), 250) * d1r / c) / SUM((d1r / c.shift(1)) ** 2, 250))
    put(56, -(R(SUM(ret, 10) / SUM(SUM(ret, 2), 3)) * R(ret * mv)))
    put(58, -TSRANK(DECAY(CORR(wi(vwap), v, 4), 8), 6))
    put(59, -TSRANK(DECAY(CORR(wi(vwap), v, 4), 16), 8))
    put(63, (R(DECAY(wi(c).diff(2), 8)) - R(DECAY(CORR(vwap * 0.318 + o * 0.682, SUM(W["adv180"], 37), 14), 12))) * -1)
    put(67, (R(h - MIN(h, 2)) ** R(CORR(wi(vwap), wi(a20), 6))) * -1)
    put(69, (R(MAX(wi(vwap).diff(3), 5)) ** TSRANK(CORR(c * 0.49 + vwap * 0.51, a20, 5), 9)) * -1)
    put(70, (R(vwap.diff(1)) ** TSRANK(CORR(wi(c), W["adv50"], 18), 18)) * -1)
    put(76, np.maximum(
        R(DECAY(vwap.diff(1), 12)),
        TSRANK(DECAY(TSRANK(CORR(wi(l), W["adv81"], 8), 20), 17), 19)) * -1)
    put(79, (R(wi(c * 0.607 + o * 0.393).diff(1)) < R(CORR(TSRANK(vwap, 4), TSRANK(W["adv150"], 9), 15))).astype(float))
    put(80, (R(np.sign(wi(o * 0.868 + h * 0.132).diff(4))) ** TSRANK(CORR(h, W["adv10"], 5), 6)) * -1)
    put(82, np.minimum(
        R(DECAY(o.diff(1), 15)),
        TSRANK(DECAY(CORR(wi(v), o, 17), 7), 13)) * -1)
    put(87, np.maximum(
        R(DECAY((c * 0.37 + vwap * 0.63).diff(2), 3)),
        TSRANK(DECAY((CORR(wi(W["adv81"]), c, 13)).abs(), 5), 14)) * -1)
    put(89, TSRANK(DECAY(CORR(wi(vwap), W["adv10"], 7), 6), 4)
             - TSRANK(DECAY(wi(vwap).diff(3), 10), 15))
    put(90, (R(c - MAX(c, 5)) ** TSRANK(CORR(wi(W["adv40"]), l, 5), 3)) * -1)
    put(91, (TSRANK(DECAY(DECAY(CORR(wi(c), v, 10), 16), 4), 5)
             - R(DECAY(CORR(vwap, W["adv30"], 4), 3))) * -1)
    put(93, TSRANK(DECAY(CORR(wi(vwap), W["adv81"], 17), 20), 8)
             / R(DECAY((c * 0.524 + vwap * 0.476).diff(3), 16)))
    put(97, (R(DECAY(wi(l * 0.721 + vwap * 0.279).diff(3), 20))
             - TSRANK(DECAY(TSRANK(CORR(TSRANK(l, 8), TSRANK(W["adv60"], 17), 5), 19), 16), 7)) * -1)
    pos100 = ((c - l) - (h - c)) / (h - l) * v
    part100a = SCALE(wi(wi(R(pos100))))
    part100b = SCALE(wi(CORR(c, R(a20), 5) - R(TSARGMIN(c, 30))))
    put(100, -(1.5 * part100a - part100b) * (v / a20))

    assert len(F) == 101, f"a101 count={len(F)}"
    return F


# ---------------------------------------------------------------------------
# 5. GTJA 191（170 个；21 个跳过见 PLAN §6.2）
# ---------------------------------------------------------------------------

def compute_gtja(W: dict[str, pd.DataFrame], bench_open: pd.Series, bench_close: pd.Series) -> dict[str, pd.DataFrame]:
    global _TEMPLATE
    o, h, l, c, v = W["open"], W["high"], W["low"], W["close"], W["volume"]
    _TEMPLATE = c
    vwap, dc, ret, A = W["vwap"], W["prev_close"], W["ret"], W["amount"]
    F: dict[str, pd.DataFrame] = {}

    def put(n: int, x) -> None:
        if isinstance(x, np.ndarray):
            x = _wrap(c, x)
        F[f"gtja_{n:03d}"] = x.replace([np.inf, -np.inf], np.nan)

    dc1 = (c - dc).abs()
    up = np.maximum(c - dc, 0.0)
    down = np.maximum(dc - c, 0.0)
    upv = np.where(c > dc, v, 0.0)
    dnvol = np.where(c < dc, v, 0.0)

    put(1, -CORR(R(v.diff(1)), R((c - o) / o), 6))
    put(2, -((((c - l) - (h - c)) / (h - l)).diff(1)))
    put(3, SUM(np.where(c > dc, c - np.minimum(dc, l), 0.0), 6)
           + SUM(np.where(c < dc, c - np.maximum(dc, l), 0.0), 6))
    sma8, std8, sma2 = MEAN(c, 8), STD(c, 8), MEAN(c, 2)
    put(4, np.select([sma8 + std8 < sma2, sma2 < sma8 - std8, v / W["adv20"] >= 1],
                     [-1.0, 1.0, 1.0], default=-1.0))
    put(5, MAX(CORR(TSRANK(h, 7), TSRANK(v, 7), 5), 5))
    put(6, R(np.select([(o * 0.85 + h * 0.15).diff(4) > 1,
                        (o * 0.85 + h * 0.15).diff(4) == 1],
                       [1.0, 0.0], default=-1.0)))
    put(7, R(np.maximum(vwap - c, 3)) + R(np.minimum(vwap - c, 3)) * R(v.diff(3)))
    put(8, R(-(h * 0.1 + l * 0.1 + vwap * 0.8).diff(4)))
    put(9, EWMA((h + l) / 2 - (h.shift() + l.shift()) / 2 * (h - l) / v, 2 / 7))
    put(10, R(np.maximum(np.where(ret < 0, STD(ret, 20), c) ** 2, 5)))
    put(11, SUM(((c - l) - (h - c)) / (h - l) * v, 6))
    put(12, R(o - MEAN(vwap, 10)) * (-R((c - vwap).abs())))
    put(13, np.sqrt(h - l) - vwap)
    put(14, c - c.shift(5))
    put(15, o / dc - 1)
    put(16, -MAX(CORR(R(v), R(vwap), 5), 5))
    put(17, R(c - MAX(vwap, 15)) ** c.diff(5))
    put(18, c / c.shift(5))
    put(19, np.where(c < c.shift(5), (c - c.shift(5)) / c.shift(5),
                     (c - c.shift(5)) / c))
    put(20, (c - c.shift(6)) * 100 / c.shift(6))
    put(21, _linreg_slope_pfilter(MEAN(c, 6), 6))
    put(22, EWMA((c - MEAN(c, 6)) / MEAN(c, 6) - ((c - MEAN(c, 6)) / MEAN(c, 6)).shift(3), 1 / 12))
    put(23, EWMA(np.where(c > dc, STD(c, 20), 0.0), 1 / 20) * 100
             / (EWMA(np.where(c > dc, STD(c, 20), 0.0), 1 / 20) + EWMA(np.where(c <= dc, STD(c, 20), 0.0), 1 / 20)))
    put(24, EWMA(c - c.shift(5), 1 / 5))
    w9 = np.arange(1, 10, dtype=float) * 2 / (9 * 10)
    put(25, -R(c.diff(7)) * (1 - (v / MEAN(v, 20)).rolling(9, min_periods=9).apply(
        lambda x: (x * w9).sum(), raw=True)) * (1 + R(SUM(ret, 250))))
    put(26, MEAN(c, 7) / 7 - c + CORR(vwap, c.shift(5), 230))
    # 027 跳过
    put(28, 3 * EWMA((c - MIN(l, 9)) / (MAX(h, 9) - MIN(l, 9)) * 100, 1 / 3)
             - 2 * EWMA(EWMA((c - MIN(l, 9)) / (MAX(h, 9) - MIN(l, 9)) * 100, 1 / 3), 1 / 3))
    put(29, (c - c.shift(6)) * v / c.shift(6))
    # 030 跳过
    put(31, (c - MEAN(c, 12)) * 100 / MEAN(c, 12))
    put(32, -SUM(R(CORR(R(h), R(v), 3)), 3))
    put(33, -MIN(l, 5).diff(5) + R((SUM(ret, 240) - SUM(ret, 20)) / 220) + TSRANK(v, 5))
    put(34, MEAN(c, 12) / c)
    put(35, np.minimum(R(_wsum(o.diff(1), 15)), -_wsum(CORR(o, v, 17), 7)))
    put(36, R(SUM(CORR(R(v), R(vwap), 6), 2)))
    put(37, -R(SUM(o, 5) * SUM(ret, 5)) - (SUM(o, 5) * SUM(ret, 5)).shift(10))
    put(38, np.where(MEAN(h, 20) < h, -h.diff(2), 0.0))
    put(39, R(_wsum(c.diff(2), 8)) - _wsum(CORR(0.3 * vwap + 0.7 * o, SUM(MEAN(v, 180), 37), 14), 12))
    put(40, SUM(upv, 26) / SUM(dnvol, 26) * 100)
    put(41, -R(np.maximum(vwap.diff(3), 5)))
    put(42, -CORR(h, v, 10) * R(STD(h, 10)))
    put(43, SUM(np.where(c > dc, v, 0.0) - np.where(c < dc, v, 0.0), 6))
    put(44, R(_wsum(CORR(l, MEAN(v, 10), 7), 6)) + R(_wsum(vwap.diff(3), 10)))
    put(45, R((c * 0.6 + o * 0.4).diff(1)) * R(CORR(vwap, MEAN(v, 150), 15)))
    put(46, (MEAN(c, 3) + MEAN(c, 6) + MEAN(c, 12) + MEAN(c, 24)) * 0.25 / c)
    put(47, EWMA(100 * (MAX(h, 6) - c) / (MAX(h, 6) - MIN(l, 6)), 1 / 9))
    s3 = np.sign(c - dc) + np.sign(dc - dc.shift(1)) + np.sign(dc.shift(1) - dc.shift(2))
    put(48, -s3 * SUM(v, 5) / SUM(v, 20))
    hlp = np.maximum((h - h.shift()).abs(), (l - l.shift()).abs())
    expand = h + l >= h.shift() + l.shift()
    shrink = h + l <= h.shift() + l.shift()
    p1 = SUM(np.where(expand, 0.0, hlp), 12)
    p2 = SUM(np.where(shrink, 0.0, hlp), 12)
    put(49, p1 / (p1 + p2))
    # 050 051 跳过
    tp = (h + l + c) / 3
    put(52, SUM(np.maximum(h - tp.shift(), 0.0), 26) + SUM(np.maximum(tp.shift() - l, 0.0), 26))
    put(53, (c > dc).rolling(12, min_periods=12).sum() * 100 / 12)
    put(54, R(STD(c - o, 250) + (c - o) + CORR(c, o, 10)))
    # 055 跳过
    put(56, (R(o - MIN(o, 12)) < (R(CORR(SUM((h + l) / 2, 19), SUM(MEAN(v, 40), 19), 13)) ** 5).rank(axis=1, pct=True)).astype(float))
    put(57, EWMA(100 * (c - MIN(l, 9)) / (MAX(h, 9) - MIN(l, 9)), 1 / 3))
    put(58, (c > dc).rolling(20, min_periods=20).sum() * 100 / 20)
    put(59, SUM(c - np.where(c > dc, np.minimum(l, dc), 0.0) - np.where(c < dc, np.maximum(h, dc), 0.0), 20))
    put(60, SUM(v * ((c - l) - (h - c)) / (h - l), 20))
    put(61, np.maximum(R(_wsum(vwap.diff(1), 12)), -R(_wsum(R(CORR(l, MEAN(v, 80), 8)), 17))))
    put(62, -CORR(h, R(v), 5))
    put(63, EWMA(up, 1 / 6) * 100 / EWMA(dc1, 1 / 6))
    put(64, np.maximum(R(_wsum(CORR(R(vwap), R(v), 4), 4)),
                       -R(_wsum(np.maximum(CORR(R(c), MEAN(v, 60), 4), 13), 14))))
    put(65, MEAN(c, 6) / c)
    put(66, (c - MEAN(c, 6)) / MEAN(c, 6))
    put(67, EWMA(up, 1 / 24) * 100 / EWMA(dc1, 1 / 24))
    put(68, EWMA((h + l) / 2 - h.shift() + 0.5 * l.shift() * (h - l) / v, 2 / 15) * 100)
    # 069 跳过
    put(70, STD(A, 6))
    put(71, (c - MEAN(c, 24)) / MEAN(c, 24) * 100)
    put(72, EWMA((MAX(h, 6) - c) / (MAX(h, 6) - MIN(l, 6)) * 100, 1 / 15))
    # 073 跳过
    put(74, R(CORR(SUM(l * 0.35 + vwap * 0.65, 20), MEAN(v, 40), 7))
             + R(CORR(R(vwap), R(v), 6)))
    # 基准指数按日广播（Series 按 date 索引，广播为全 symbol 宽表）
    def _bcast(s: pd.Series) -> pd.DataFrame:
        s = s.reindex(c.index).fillna(False)
        return pd.DataFrame(
            np.broadcast_to(s.values[:, None], (len(c), c.shape[1])),
            index=c.index, columns=c.columns)

    bench_down_b = _bcast(bench_close < bench_open)
    bench_up_b = _bcast(bench_close > bench_open)
    put(75, ((c > o) & bench_down_b).rolling(50, min_periods=50).sum()
         / bench_down_b.rolling(50, min_periods=50).sum())
    put(76, STD(dc1 / v, 20) / MEAN(dc1 / v, 20))
    put(77, np.minimum(R(DECAY((h + l) / 2 + h - (vwap + h), 20)),
                        R(DECAY(CORR((h + l) / 2, MEAN(v, 40), 3), 6))))
    tp12 = MEAN((h + l + c) / 3, 12)
    put(78, ((h + l + c) / 3 - tp12) / (0.015 * MEAN((c - tp12).abs(), 12)))
    put(79, EWMA(up, 1 / 12) * 100 / EWMA(dc1, 1 / 12))
    put(80, (v - v.shift(5)) / v.shift(5) * 100)
    put(81, EWMA(v, 2 / 21))
    put(82, EWMA((MAX(h, 6) - c) / (MAX(h, 6) - MIN(l, 6)) * 100, 1 / 20))
    put(83, -CORR(TSRANK(h, 250), TSRANK(v, 250), 5))
    put(84, SUM(np.where(c > dc, v, 0.0) - np.where(c < dc, v, 0.0), 20))
    put(85, TSRANK(v / MEAN(v, 20), 20) * TSRANK(-c.diff(7), 8))
    inner86 = (c.shift(20) - c.shift(10)) / 10 - (c.shift(10) - c) / 10
    put(86, np.select([inner86 > 0.25, inner86 < 0], [-1.0, 1.0], default=-c.diff(1)))
    put(87, R(_wsum(vwap.diff(4), 7)) + R(DECAY((l - vwap) / (o - (h + l) / 2), 11)))
    put(88, (c - c.shift(20)) / c.shift(20) * 100)
    put(89, 2 * (EMA_SPAN(c, 12) - EMA_SPAN(c, 26) - EMA_SPAN(EMA_SPAN(c, 12) - EMA_SPAN(c, 26), 9)))
    put(90, -R(CORR(R(vwap), R(v), 5)))
    put(91, -R(c - np.maximum(c, 5)) * R(CORR(MEAN(v, 40), l, 5)))
    put(92, -np.maximum(R(DECAY((c * 0.35 + vwap * 0.65).diff(2), 3)),
                         TSRANK(DECAY((CORR(MEAN(v, 180), c, 13)).abs(), 5), 15)))
    gap_o = np.maximum(o - l, o - o.shift(1))
    put(93, SUM(np.where(o >= o.shift(1), 0.0, gap_o), 20))
    put(94, SUM(np.where(c > dc, v, np.where(c < dc, -v, 0.0)), 30))
    put(95, STD(A, 20))
    put(96, EMA_SPAN(100 * (c - MIN(l, 9)) / (MAX(h, 9) - MIN(l, 9)), 5))
    put(97, STD(v, 10))
    cond98 = (MEAN(c, 100).diff(100) / c.shift(100)) <= 0.05
    put(98, np.where(cond98, -(c - MIN(c, 100)), -c.diff(3)))
    put(99, -R(COV(R(c), R(v), 5)))
    put(100, STD(v, 20))
    put(101, (-(R(CORR(c, SUM(MEAN(v, 30), 37), 15)) < R(CORR(R(h * 0.1 + vwap * 0.9), R(v), 11)))).astype(float))
    put(102, EWMA(np.maximum(v.diff(1), 0.0), 1 / 11) / EWMA(v.diff(1).abs(), 1 / 11) * 100)
    put(103, (20 - LOWDAY(l, 20)) / 20 * 100)
    put(104, -CORR(h, v, 5).diff(5) * R(STD(c, 20)))
    put(105, -CORR(R(o), R(v), 10))
    put(106, c - c.shift(20))
    put(107, -R(o - h.shift(1)) * R(o - c.shift(1)) * R(o - l.shift(1)))
    put(108, -R(h - np.maximum(h, 2)) ** R(CORR(vwap, MEAN(v, 120), 6)))
    put(109, (h - l).ewm(span=9, adjust=False).mean() / (h - l).ewm(span=9, adjust=False).mean().ewm(span=9, adjust=False).mean())
    put(110, SUM(np.maximum(h - dc, 0.0), 20) / SUM(np.maximum(dc - l, 0.0), 20) * 100)
    pos = v * ((c - l) - (h - c)) / (h - l)
    put(111, EWMA(pos, 2 / 11) - EWMA(pos, 0.5))
    put(112, (SUM(up, 12) - SUM(down, 12)) / (SUM(up, 12) + SUM(down, 12)) * 100)
    put(113, -R(MEAN(c.shift(5), 20)) * CORR(c, v, 2) * R(CORR(SUM(c, 5), SUM(c, 20), 2)))
    amp = (h - l) / (MEAN(c, 5) / 5)
    put(114, R(amp.shift(2)) * R(R(v)) / (amp / (vwap - c)))
    put(115, R(CORR(h * 0.9 + c * 0.1, MEAN(v, 30), 10)) ** R(CORR(TSRANK((h + l) / 2, 4), TSRANK(v, 10), 7)))
    put(116, REG(c, 20)[0])  # REGBETA：20 日回归斜率（源码用 corr，按研报实现 slope）
    put(117, TSRANK(v, 32) * (1 - TSRANK(c + h - l, 16)) * (1 - TSRANK(ret, 32)))
    put(118, SUM(h - o, 20) / SUM(o - l, 20) * 100)
    rank4a = TSRANK(CORR(R(o), R(MEAN(v, 15)), 21).rolling(9, min_periods=9).min(), 7)
    put(119, R(DECAY(CORR(vwap, SUM(MEAN(v, 5), 26), 5), 7)) - R(DECAY(rank4a, 8)))
    put(120, R(vwap - c) / R(vwap + c))
    # 121 跳过
    put(122, EMA_SPAN(EMA_SPAN(EMA_SPAN(np.log(c), 12), 12), 12)
             / EMA_SPAN(EMA_SPAN(EMA_SPAN(np.log(c), 12), 12), 12).shift(1) - 1)
    put(123, (-(R(CORR(SUM((h + l) / 2, 20), SUM(MEAN(v, 60), 20), 9)) < R(CORR(l, v, 6)))).astype(float))
    put(124, (c - vwap) / DECAY(R(MAX(c, 30)), 2))
    put(125, R(DECAY(CORR(vwap, MEAN(v, 80), 17), 20)) / R(DECAY((c * 0.5 + vwap * 0.5).diff(3), 16)))
    put(126, (c + h + l) / 3)
    # 127 128 跳过
    put(129, SUM(np.where(c < dc, dc1, 0.0), 12))
    put(130, R(DECAY(CORR((h + l) / 2, MEAN(v, 40), 9), 10)) / R(DECAY(CORR(R(vwap), R(v), 7), 3)))
    # 131 跳过
    put(132, MEAN(A, 20))
    put(133, (20 - HIGHDAY(h, 20)) / 20 * 100 - (20 - LOWDAY(l, 20)) / 20 * 100)
    put(134, (c / c.shift(12) - 1) * v)
    put(135, (c / c.shift(20)).shift(1).ewm(alpha=1 / 20, adjust=False).mean())
    put(136, -R(ret.diff(3)) * CORR(o, v, 10))
    # 137 跳过
    put(138, R(DECAY((l * 0.7 + vwap * 0.3).diff(3), 20))
             - TSRANK(DECAY(TSRANK(CORR(TSRANK(l, 8), TSRANK(MEAN(v, 60), 17), 5), 19), 16), 7))
    put(139, -CORR(o, v, 10))
    put(140, np.minimum(
        R(DECAY(R(o) + R(l) - R(h) - R(c), 8)),
        TSRANK(DECAY(CORR(TSRANK(c, 8), TSRANK(MEAN(v, 60), 20), 8), 7), 3)))
    put(141, -R(CORR(R(h), R(MEAN(v, 15)), 9)))
    put(142, -R(TSRANK(c, 10)) * R(c.diff(1).diff(1)) * R(TSRANK(v / MEAN(v, 20), 5)))
    # 143 跳过（自引用）
    put(144, SUM(np.where(c < dc, dc1 / A, 0.0), 20) / (c < dc).rolling(20, min_periods=20).sum())
    put(145, (MEAN(v, 9) - MEAN(v, 26)) / MEAN(v, 12) * 100)
    # 146 147 跳过
    put(148, (-(R(CORR(o, SUM(MEAN(v, 60), 9), 6)) < R(o - MIN(o, 14)))).astype(float))
    # 149 跳过
    put(150, (c + h + l) / 3 * v)
    # 151 跳过
    r9 = EMA_SPAN((c / c.shift(9)).shift(1), 17, adjust=False).shift(1)
    put(152, EWMA(MEAN(r9, 12) - MEAN(r9, 26), 1 / 9))
    put(153, (MEAN(c, 3) + MEAN(c, 6) + MEAN(c, 12) + MEAN(c, 24)) / 4)
    put(154, ((vwap - MIN(vwap, 16)) < CORR(vwap, MEAN(v, 180), 18)).astype(float))
    put(155, EMA_SPAN(v, 12) - EMA_SPAN(v, 26) - EMA_SPAN(EMA_SPAN(v, 12) - EMA_SPAN(v, 26), 9))
    put(156, -np.maximum(R(DECAY(vwap.diff(5), 3)),
                          R(DECAY(-(o * 0.15 + l * 0.85).diff(2) / (o * 0.15 + l * 0.85), 3))))
    rank1 = R(R(R(-c.diff(5))))
    min1 = rank1.rolling(2, min_periods=2).min()
    rank2 = R(R(np.log(min1)))
    rank2 = np.where(rank2 > 5, 5.0, rank2)
    put(157, rank2 + TSRANK((-ret).shift(6), 5))
    put(158, ((h - EMA_SPAN(c, 14)) - (l - EMA_SPAN(c, 14))) / c)
    lo6 = np.minimum(l, dc)
    hi6 = np.maximum(h, dc)
    x = (c - SUM(lo6, 6)) / SUM(dc - lo6, 6) * 12 * 24
    y = (c - SUM(lo6, 12)) / SUM(dc - lo6, 12) * 6 * 24
    z = (c - SUM(lo6, 24)) / SUM(dc - lo6, 24) * 6 * 24
    put(159, (x + y + z) * (100 / (6 * 12 + 12 * 24 + 6 * 24)))
    put(160, _as_df(np.where(c <= dc, STD(c, 20), 0.0)).ewm(alpha=1 / 20, adjust=False).mean())
    tr = np.maximum(h - l, np.maximum((h - dc).abs(), (l - dc).abs()))
    put(161, MEAN(tr, 12))
    x162 = EWMA(up, 1 / 12) * 100 / EWMA(dc1, 1 / 12)
    put(162, (x162 - MIN(x162, 12)) / (MAX(x162, 12) - MIN(x162, 12)))
    put(163, R(-ret * MEAN(v, 20) * vwap * (h - c)))
    x164 = np.where(c > dc, 1 / (c - dc), 1.0)
    put(164, EWMA((x164 - MIN(x164, 12)) / (h - l) * 100, 1 / 12))
    # 165 166 跳过
    put(167, SUM(np.maximum(c - dc, 0.0), 12))
    put(168, -v / MEAN(v, 20))
    d169 = EMA_SPAN(c.diff(1), 17).shift(1)
    put(169, EWMA(MEAN(d169, 12) - MEAN(d169, 26), 1 / 10))
    put(170, R(1 / c) * v / MEAN(v, 20) * (h * R(h - c)) / MEAN(h, 5) - R(vwap - vwap.shift(5)))
    put(171, -((l - c) * o ** 5) / ((c - h) * c ** 5))
    hd = h.diff(1)
    ld = l.diff(1) * -1  # LOW - DELAY(LOW) = -diff? 保留与源码一致: ld = l.shift()-l
    hd = h - h.shift(1)
    ld = l.shift(1) - l
    di_up = SUM(np.where((hd > 0) & (hd > ld), hd, 0.0), 14) * 100 / SUM(tr, 14)
    di_dn = SUM(np.where((ld > 0) & (ld > hd), ld, 0.0), 14) * 100 / SUM(tr, 14)
    put(172, MEAN((di_up - di_dn).abs() / (di_up + di_dn) * 100, 6))
    put(173, 3 * EMA_SPAN(c, 12, adjust=True) - 2 * EMA_SPAN(EMA_SPAN(c, 12, adjust=True), 12, adjust=True)
             + EMA_SPAN(EMA_SPAN(EMA_SPAN(np.log(c), 12, adjust=True), 12, adjust=True), 12, adjust=True))
    put(174, _as_df(np.where(c > dc, STD(c, 20), 0.0)).ewm(alpha=1 / 20, adjust=False).mean())
    put(175, MEAN(tr, 6))
    put(176, CORR(R((c - MIN(l, 12)) / (MAX(h, 12) - MIN(l, 12))), R(v), 6))
    put(177, (20 - HIGHDAY(h, 20)) / 20 * 100)
    put(178, (c - dc) / dc * v)
    put(179, R(CORR(vwap, v, 4)) * R(CORR(R(l), R(MEAN(v, 50)), 12)))
    cond180 = MEAN(v, 20) < v
    left180 = -TSRANK(c.diff(7).abs(), 60) * np.sign(c.diff(7))
    put(180, np.where(cond180, left180, -v))
    # 181 跳过
    put(182, ((c > o) & bench_up_b | (c < o) & bench_down_b).rolling(20, min_periods=20).sum() / 20)
    # 183 跳过
    put(184, R(CORR((o - c).shift(1), c, 200)) + R(o - c))
    put(185, R(-(1 - o / c) ** 2))
    put(186, (MEAN((di_up - di_dn).abs() / (di_up + di_dn) * 100, 6)
              + MEAN((di_up - di_dn).abs() / (di_up + di_dn) * 100, 6).shift(6)) / 2)
    gap187 = np.maximum(h - o, o - o.shift(1))
    put(187, SUM(np.where(o <= o.shift(1), 0.0, gap187), 20))
    put(188, (h - l - EMA_SPAN(h - l, 10)) / EMA_SPAN(h - l, 10) * 100)
    put(189, MEAN((c - MEAN(c, 6)).abs(), 6))
    th = (c / c.shift(19)) ** (1 / 20) - 1
    upcnt = (ret > th).rolling(20, min_periods=20).sum()
    dnq = (ret < th).rolling(20, min_periods=20).sum()
    upsq = np.where(ret > th, (ret - th) ** 2, 0.0)
    dnsq = np.where(ret < th, (ret - th) ** 2, 0.0)
    put(190, np.log((upcnt - 1) * SUM(dnsq, 20) / (dnq * SUM(upsq, 20))))
    put(191, CORR(MEAN(v, 20), l, 5) + (h + l) / 2 - c)

    # 跳过清单（与 PLAN 一致）
    SKIP = {27, 30, 50, 51, 55, 69, 73, 121, 127, 128, 131, 137, 143, 146, 147, 149, 151, 165, 166, 181, 183}
    expected = {f"gtja_{n:03d}" for n in range(1, 192) if n not in SKIP}
    missing = expected - set(F.keys())
    assert not missing, f"gtja missing: {sorted(missing)}"
    assert len(F) == 170, f"gtja count={len(F)}"
    return F


def _wsum(df: pd.DataFrame, w: int) -> pd.DataFrame:
    """递增权重加权和（seq 2i/(n(n+1))，近大远小）。"""
    df = _as_df(df)
    wt = np.arange(1, w + 1, dtype=float) * 2 / (w * (w + 1))
    n = df.shape[0]
    out = np.full(df.shape, np.nan)
    if n >= w:
        m = df.values
        for i in range(0, df.shape[1], 128):
            j = min(i + 128, df.shape[1])
            v = np.lib.stride_tricks.sliding_window_view(m[:, i:j], w, axis=0)
            vals = v @ wt
            valid = ~np.isnan(v).any(axis=2)  # 按列掩码
            out[w - 1:, i:j] = np.where(valid, vals, np.nan)
    return _wrap(df, out)


def _linreg_slope_pfilter(df: pd.DataFrame, w: int) -> pd.DataFrame:
    """alpha_021: 对 MA6 序列最后 6 个值做线性回归取斜率；p>0.05 置 NaN（df=4, |t|>2.776）。"""
    df = _as_df(df)
    out = np.full(df.shape, np.nan)
    n, c = df.shape
    if n >= w:
        m = df.values
        X = np.arange(1, w + 1, dtype=float)
        sx, sxx = X.sum(), (X * X).sum()
        tcrit = 2.776  # t(0.975, df=4)
        for i in range(0, c, 128):
            j = min(i + 128, c)
            v = np.lib.stride_tricks.sliding_window_view(m[:, i:j], w, axis=0)
            sy = v.sum(axis=2)
            syy = (v * v).sum(axis=2)
            sxy = v @ X
            denom = w * sxx - sx * sx
            slope = (w * sxy - sx * sy) / denom
            inter = (sy - slope * sx) / w
            resid = v - (inter[:, :, None] + slope[:, :, None] * X[None, None, :])
            ssr = (resid * resid).sum(axis=2)
            mse = ssr / (w - 2)
            se = np.sqrt(mse / denom)
            tstat = slope / se
            valid = (~np.isnan(v).any(axis=2)) & (np.abs(tstat) > tcrit)
            out[w - 1:, i:j] = np.where(valid, slope, np.nan)
    return _wrap(df, out)


# ---------------------------------------------------------------------------
# 6. 汇总与落盘
# ---------------------------------------------------------------------------

def write_partitions(factors: dict[str, pd.DataFrame], *, start_dt: str = "20160101",
                     out_root: Path | None = None) -> int:
    """按日分区原子写盘: <out_root>/dt=YYYYMMDD/data.parquet。"""
    out_root = out_root or OUT_ROOT
    names = list(factors.keys())
    frames = list(factors.values())
    dates = frames[0].index
    syms = list(frames[0].columns)
    n_days = len(dates)
    n_fac = len(names)
    log.info("assembly: %d dates × %d symbols × %d factors → %s", n_days, len(syms), n_fac, out_root.name)

    # 统一为 numpy 数组，避免逐因子 loc
    arrs = [f.values for f in frames]
    written = 0
    chunk = 40  # 每批 40 天
    for b in range(0, n_days, chunk):
        b_end = min(b + chunk, n_days)
        block = np.stack([a[b:b_end] for a in arrs], axis=2)  # (nb, n_sym, n_fac)
        for k in range(b, b_end):
            dt_str = pd.Timestamp(dates[k]).strftime("%Y%m%d")
            if dt_str < start_dt:
                continue
            day = pd.DataFrame(block[k - b], index=syms, columns=names)
            day = day.reset_index().rename(columns={"index": "symbol"})
            day.insert(1, "time", pd.Timestamp(dates[k]))
            day = day.replace([np.inf, -np.inf], np.nan)
            # float32 落盘：训练特征无需 float64，体积减半
            for col in names:
                day[col] = day[col].astype(np.float32)
            dt_dir = out_root / f"dt={dt_str}"
            dt_dir.mkdir(parents=True, exist_ok=True)
            target = dt_dir / "data.parquet"
            if target.exists():
                continue  # 增量：已有分区跳过
            tmp = dt_dir / ".tmp-data.parquet"
            try:
                day.to_parquet(tmp, index=False)
                tmp.replace(target)
                written += 1
            finally:
                tmp.unlink(missing_ok=True)
        del block
    log.info("partitions written: %d", written)
    return written


def merge_partials(partials: list[Path], final_root: Path, *, start_dt: str = "20160101") -> int:
    """把分库写入的分区合并为最终数据集（按日读取三个分库分区 → concat → 原子写，边合并边删 partial）。"""
    dt_dirs = sorted({p.name for p in partials[0].glob("dt=*")})
    written = 0
    for dt_name in dt_dirs:
        dt_str = dt_name[3:]
        if dt_str < start_dt:
            continue
        frames = []
        for p in partials:
            f = p / dt_name / "data.parquet"
            if f.exists():
                frames.append(pd.read_parquet(f))
        if not frames:
            continue
        merged = frames[0]
        for f in frames[1:]:
            merged = merged.merge(f, on=["symbol", "time"], how="outer")
        merged = merged.replace([np.inf, -np.inf], np.nan)
        dt_dir = final_root / dt_name
        dt_dir.mkdir(parents=True, exist_ok=True)
        target = dt_dir / "data.parquet"
        tmp = dt_dir / ".tmp-data.parquet"
        try:
            merged.to_parquet(tmp, index=False)
            tmp.replace(target)
            written += 1
        finally:
            tmp.unlink(missing_ok=True)
        # 合并后删除当日 partial（控制磁盘峰值）
        for p in partials:
            pd_dir = p / dt_name
            if pd_dir.exists():
                import shutil
                shutil.rmtree(pd_dir, ignore_errors=True)
    log.info("merged partitions: %d → %s", written, final_root.name)
    return written


# ---------------------------------------------------------------------------
# 7. 训练标签（独立数据集，与特征表物理隔离）
# ---------------------------------------------------------------------------

LABELS_OUT = OUT_ROOT.parent / "alpha_library_labels"


def compute_labels(W: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """未来收益标签。⚠️ 全引擎唯一允许使用未来数据的地方（shift(-k)）：
    只写入 alpha_library_labels/，绝不进 alpha_library 特征表。"""
    c = W["close"]
    L = {
        "fwd_ret_1": c.shift(-1) / c - 1.0,          # 持有 1 日
        "fwd_ret_2": c.shift(-3) / c.shift(-1) - 1.0,  # Qlib Alpha158 标准: 从 T+1 到 T+3
        "fwd_ret_5": c.shift(-5) / c - 1.0,
        "fwd_ret_10": c.shift(-10) / c - 1.0,
        "fwd_ret_20": c.shift(-20) / c - 1.0,
    }
    return L


def write_labels(factors: dict[str, pd.DataFrame], *, start_dt: str = "20160101") -> int:
    names = list(factors.keys())
    frames = list(factors.values())
    dates = frames[0].index
    syms = list(frames[0].columns)
    arrs = [f.values for f in frames]
    written = 0
    for k, dt in enumerate(dates):
        dt_str = pd.Timestamp(dt).strftime("%Y%m%d")
        if dt_str < start_dt:
            continue
        day = pd.DataFrame({n: arr[k, :] for n, arr in zip(names, arrs)}, index=syms)
        day = day.reset_index().rename(columns={"index": "symbol"})
        day.insert(1, "time", pd.Timestamp(dt))
        dt_dir = LABELS_OUT / f"dt={dt_str}"
        dt_dir.mkdir(parents=True, exist_ok=True)
        target = dt_dir / "data.parquet"
        if target.exists():
            continue
        tmp = dt_dir / ".tmp-data.parquet"
        try:
            day.to_parquet(tmp, index=False)
            tmp.replace(target)
            written += 1
        finally:
            tmp.unlink(missing_ok=True)
    return written


# ---------------------------------------------------------------------------
# 8. 冒烟验证（单位契约 + 无未来函数截断不变性）
# ---------------------------------------------------------------------------

def smoke_test() -> None:
    log.info("=== SMOKE: 50 symbols × 400 days ===")
    W = load_kline(start_year=2024, max_symbols=50)
    dates = W["close"].index
    syms = list(W["close"].columns)
    ind = load_industry_map()
    mv = load_total_mv(dates, syms)
    bo, bc = load_benchmark()

    # 1) 单位契约：vwap（复权基准）应落在当日 [low, high]（±0.5% 容差）
    lo, hi, vw = W["low"], W["high"], W["vwap"]
    ok = ((vw >= lo * 0.995) & (vw <= hi * 1.005) & vw.notna()).mean().mean()
    assert ok > 0.95, f"vwap out of range ratio={ok:.4f}"
    log.info("[unit] vwap within OHLC range: %.4f ✓", ok)

    # 2) 三库计算
    F158 = compute_a158(W)
    F101 = compute_a101(W, ind, mv)
    FGT = compute_gtja(W, bo, bc)
    F = {**F158, **F101, **FGT}
    assert len(F) == 429, f"total factors={len(F)}"
    log.info("[count] factors: a158=%d a101=%d gtja=%d total=429 ✓", len(F158), len(F101), len(FGT))

    # 3) 截断不变性（无未来函数核心验证）：去掉最后 60 天重算，前段值必须一致
    cut = dates[-61]
    W2 = {k: v.loc[:cut] for k, v in W.items()}
    F158b = compute_a158(W2)
    F101b = compute_a101(W2, ind, mv.loc[:cut])
    FGTb = compute_gtja(W2, bo.loc[:cut], bc.loc[:cut])
    Fb = {**F158b, **F101b, **FGTb}
    probe = dates[-120]  # 截断点前 60 天取一个日期
    diffs = []
    for name, f in F.items():
        a = f.loc[probe].astype(float)
        b = Fb[name].loc[probe].astype(float)
        mask = a.notna() & b.notna()
        if mask.any():
            d = (a[mask] - b[mask]).abs().max()
            if d > 1e-9:
                diffs.append((name, float(d)))
    assert not diffs, f"look-ahead leakage: {diffs[:10]}"
    log.info("[no-lookahead] truncation invariance at %s ✓ (all %d factors unchanged)", probe.date(), len(F))

    # 4) 值域 sanity
    r = F["a101_013"]
    assert (r.dropna() <= 0.0 + 1e-9).all().all(), "a101_013 should be <= 0"
    assert ((F["a101_101"].dropna().abs() <= 2.0)).all().all(), "a101_101 bounds"
    tsr = F["a158_RANK20"]
    assert ((tsr.dropna() >= 0) & (tsr.dropna() <= 1)).all().all(), "a158_RANK20 range"
    log.info("[range] a101_013<=0 ✓ a101_101∈[-2,2] ✓ a158_RANK20∈[0,1] ✓")

    # 5) 分区写盘往返
    n = write_partitions(F, start_dt=str(pd.Timestamp(dates[-1]).strftime("%Y%m%d")))
    assert n >= 1, "no partition written"
    last_dt = sorted(p.name for p in (OUT_ROOT / "").glob("dt=*"))[-1]
    back = pd.read_parquet(OUT_ROOT / last_dt / "data.parquet")
    assert back.shape[1] == 429 + 2, f"roundtrip cols={back.shape[1]}"
    assert back["symbol"].is_unique
    log.info("[roundtrip] partition %s: %d rows × %d cols ✓", last_dt, back.shape[0], back.shape[1])

    # 6) 滑窗因子非 NaN 率（防跨列掩码污染回归：2026-08-29 bug）
    #    数据最全的老股票，近期滑窗因子应有 >90% 非 NaN。
    #    a101_100 例外：内含 corr(close, rank(adv20), 5)，50 只烟熏集排名粒度粗
    #    （adv20 排名粘滞 → 窗口内恒定时 rolling corr 除零得 NaN，数据固有属性；
    #    全市场 5554 只下实测 NaN≈6%）。此处只断言其未被整列污染（>30%）。
    rich_sym = W["close"].notna().sum().idxmax()
    recent = dates[-60:]
    for name, thr in [("a158_RANK20", 0.9), ("a158_IMAX20", 0.9), ("a101_013", 0.9),
                      ("a101_100", 0.3), ("gtja_103", 0.9), ("a158_BETA20", 0.9)]:
        vals = F[name].loc[recent, rich_sym]
        ratio = vals.notna().mean()
        assert ratio > thr, f"{name} non-NaN ratio={ratio:.3f} for {rich_sym}"
    log.info("[sliding] rich-symbol sliding factors non-NaN check: %s ✓", rich_sym)
    log.info("SMOKE PASSED ✓")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true", help="冒烟测试（50 只 × 400 天）")
    ap.add_argument("--start-year", type=int, default=None)
    ap.add_argument("--max-symbols", type=int, default=None)
    ap.add_argument("--start-dt", default="20160101", help="分区写盘起始日期 YYYYMMDD")
    ap.add_argument("--rebuild", action="store_true", help="覆盖重写已有分区")
    ap.add_argument("--labels", action="store_true", help="仅计算未来收益标签（独立数据集）")
    args = ap.parse_args()

    t0 = time.time()
    if args.labels:
        W = load_kline(start_year=args.start_year, max_symbols=args.max_symbols)
        n = write_labels(compute_labels(W), start_dt=args.start_dt)
        log.info("labels written: %d partitions (%.0fs)", n, time.time() - t0)
        return
    if args.smoke:
        smoke_test()
        log.info("smoke done in %.0fs", time.time() - t0)
        return

    assert PLAN.is_file(), f"missing {PLAN}"
    W = load_kline(start_year=args.start_year, max_symbols=args.max_symbols)
    dates, syms = W["close"].index, list(W["close"].columns)
    ind = load_industry_map()
    log.info("industry map: %d symbols, %d industries", len(ind), ind.nunique())
    mv = load_total_mv(dates, syms)
    bo, bc = load_benchmark()

    # 分库计算 → 分库落盘 → 立即释放 → 最后按日合并（OOM 防护：峰值 ~15GB）
    import gc
    import shutil

    partials: list[Path] = []
    jobs = [
        ("a158", compute_a158, (W,)),
        ("a101", compute_a101, (W, ind, mv)),
        ("gtja", compute_gtja, (W, bo, bc)),
    ]
    for name, fn, job_args in jobs:
        F = fn(*job_args)
        log.info("%s done: %d factors (%.0fs)", name, len(F), time.time() - t0)
        root = OUT_ROOT.parent / f"_partial_{name}"
        write_partitions(F, start_dt=args.start_dt, out_root=root)
        del F
        gc.collect()
        partials.append(root)

    total = merge_partials(partials, OUT_ROOT, start_dt=args.start_dt)
    for p in partials:
        shutil.rmtree(p, ignore_errors=True)
    log.info("ALL DONE: %d partitions merged in %.0fs", total, time.time() - t0)


if __name__ == "__main__":
    main()
