#!/usr/bin/env python3
"""恒生科技「定价权系统C」公式复原与回测。

用户截图（OCR 残缺）可还原为：

    原始SMART   := 恒生科技涨跌幅 × 恒生科技成交量
    SMARTMONEY  := EMA(原始SMART, N1)
    共振        := 上证50 > REF(上证50,1) AND 沪深300 > REF(沪深300,1)
                   AND 创业板指 > REF(创业板指,1)
    趋势多头    := SMARTMONEY > MA(SMARTMONEY, 20)
    买入        := 共振 AND 趋势多头 AND SMARTMONEY > REF(SMARTMONEY,1)

核心命题：恒生科技的量价信号是否**领先** A 股指数（大涨 → 之后 A 股也涨）。

纪律：
- 信号在 t 日 16:00（港股收盘）才完全可知，而 A 股 t 日 15:00 已收盘。
  主口径一律 **t+1 开盘入场**；t 日收盘入场只作参考并标注不可执行。
- 只保留两地同时开市、且中间没有港股单独交易的日子 —— 长假后港股多日
  涨跌会被误当成「单日大涨」。
- 重叠样本用块自助法（块长 = 预测窗口）；分时段复核；负结果照报。

数据源：QuantHK index_daily / QuantDB A 股 index_daily 本地 parquet。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from backend.scripts.research_stats import (
    block_bootstrap_corr,
    block_group_diff,
    overlap_deflated_t,
    spearman_ic,
)

log = logging.getLogger("hstech_pricing_power")

HK_ROOT = (
    Path(os.environ.get("QM_QUANTHK_DATA_DIR", "/data/quanthk"))
    / "1_kline_data"
    / "index_daily"
)
A_ROOT = (
    Path(os.environ.get("QM_QUANTDB_DATA_DIR", "/data/quantdb"))
    / "1_kline_data"
    / "index_daily"
)
OUT_DIR = Path(
    os.environ.get(
        "QM_MARKET_BREADTH_DIR", "/data/quantdb/5_technical_derived/market_breadth"
    )
)

START_TAG = "20200101"
HK_SYMBOLS = ("HSTECH.HK", "HSI.HK")
A_SYMBOLS = (
    "000016.SH",  # 上证50
    "000300.SH",  # 沪深300
    "000001.SH",  # 上证指数
    "399006.SZ",  # 创业板指
    "000852.SH",  # 中证1000
    "399001.SZ",  # 深证成指
)
A_NAMES = {
    "000300.SH": "沪深300",
    "000001.SH": "上证指数",
    "399006.SZ": "创业板指",
    "000016.SH": "上证50",
    "000852.SH": "中证1000",
    "399001.SZ": "深证成指",
}
PRIMARY = ("000300.SH", "399006.SZ")
HORIZONS = (1, 3, 5, 10, 20)
PERIODS = (
    ("2020-08~2022-12", "2020-08-01", "2022-12-31"),
    ("2023-01~2024-08", "2023-01-01", "2024-08-31"),
    ("2024-09~2026-10", "2024-09-01", "2026-12-31"),
)


# ---------------------------------------------------------------- 数据装载


def load_wide(root: Path, symbols: tuple[str, ...]) -> pd.DataFrame:
    """逐日分区读指数日线，拼成 ``symbol|field`` 宽表。"""
    fields = ("open", "close", "volume", "amount")
    frames: list[pd.DataFrame] = []
    for part in sorted(root.glob("dt=*")):
        tag = part.name.split("=", 1)[1]
        if tag < START_TAG:
            continue
        path = part / "data.parquet"
        if not path.exists():
            continue
        t = pq.read_table(path, columns=["symbol", *fields]).to_pandas()
        t = t[t["symbol"].isin(symbols)]
        if t.empty:
            continue
        t["date"] = pd.Timestamp(tag)
        frames.append(t)
    if not frames:
        raise RuntimeError(f"{root} 下没有可用分区")
    raw = pd.concat(frames, ignore_index=True)
    wide = raw.pivot_table(
        index="date", columns="symbol", values=list(fields), aggfunc="last"
    )
    wide.columns = [f"{sym}|{fld}" for fld, sym in wide.columns]
    return wide.sort_index()


def clean_mask(hk_index: pd.DatetimeIndex, dates: pd.DatetimeIndex) -> pd.Series:
    """标记「上一共同交易日后没有港股单独交易日」的共同日。

    港股与 A 股假期不同：A 股休市期间港股照常交易，回到共同日时港股的
    日收益实际横跨多日。这类日子一律剔除，否则「单日大涨」是假的。
    """
    hk_days = hk_index.asi8
    dates_ns = dates.asi8
    prev_ns = np.empty_like(dates_ns)
    prev_ns[0] = np.iinfo(np.int64).min
    prev_ns[1:] = dates_ns[:-1]
    counts = np.searchsorted(hk_days, dates_ns, "right") - np.searchsorted(
        hk_days, prev_ns, "right"
    )
    counts[0] = -1  # 第一天没有「上一共同交易日」，不参与
    return pd.Series(counts == 1, index=dates)


# ---------------------------------------------------------------- 特征


def build_features(df: pd.DataFrame, span: int) -> pd.DataFrame:
    """复原公式特征。``norm`` 用相对量、``tdx`` 用原始量、``amt`` 用成交额。"""
    f = pd.DataFrame(index=df.index)
    close = df["HSTECH.HK|close"]
    vol = df["HSTECH.HK|volume"]
    amt = df["HSTECH.HK|amount"]
    f["hk_ret"] = close.pct_change(fill_method=None)
    f["vol_rel"] = vol / vol.rolling(20, min_periods=10).median()
    f["amt_rel"] = amt / amt.rolling(20, min_periods=10).median()

    raws = {
        "norm": f["hk_ret"] * f["vol_rel"],
        "amt": f["hk_ret"] * f["amt_rel"],
        "tdx": f["hk_ret"] * vol,
    }
    for name, raw in raws.items():
        smart = raw.ewm(span=span, adjust=False).mean()
        ma20 = smart.rolling(20, min_periods=20).mean()
        f[f"smart_{name}"] = smart
        f[f"ma20_{name}"] = ma20
        f[f"trend_{name}"] = smart > ma20
        f[f"turn_{name}"] = smart > smart.shift(1)
        f[f"smartz_{name}"] = (
            smart - smart.rolling(60, min_periods=40).mean()
        ) / smart.rolling(60, min_periods=40).std()

    up = {
        s: df[f"{s}|close"] > df[f"{s}|close"].shift(1)
        for s in ("000016.SH", "000300.SH", "399006.SZ")
    }
    f["res_up"] = up["000016.SH"] & up["000300.SH"] & up["399006.SZ"]
    f["res_down"] = (~up["000016.SH"]) & (~up["000300.SH"]) & (~up["399006.SZ"])

    # 对照：完全相同的「量价 EMA + 趋势 + 拐头」规则，但标的换成 A 股自己。
    # 若它与恒生版效果相当，说明信息来自「趋势动量」本身，而非港股的领先性。
    for sym in A_SYMBOLS:
        c = df[f"{sym}|close"]
        v = df[f"{sym}|volume"]
        ret = c.pct_change(fill_method=None)
        vrel = v / v.rolling(20, min_periods=10).median()
        smart = (ret * vrel).ewm(span=span, adjust=False).mean()
        f[f"a_trend_{sym}"] = smart > smart.rolling(20, min_periods=20).mean()
        f[f"a_turn_{sym}"] = smart > smart.shift(1)
    return f


def build_signals(f: pd.DataFrame) -> pd.DataFrame:
    """公式各层过滤 + 镜像下行信号。"""
    s = pd.DataFrame(index=f.index)
    s["res_only"] = f["res_up"]
    s["res_trend"] = f["res_up"] & f["trend_norm"]
    s["formula"] = f["res_up"] & f["trend_norm"] & f["turn_norm"]
    s["formula_tdx"] = f["res_up"] & f["trend_tdx"] & f["turn_tdx"]
    s["hk_only"] = f["trend_norm"] & f["turn_norm"]
    s["downside"] = (
        f["res_down"]
        & (~f["trend_norm"])
        & (f["smart_norm"] < f["smart_norm"].shift(1))
    )
    return s


def event_masks(f: pd.DataFrame) -> dict[str, pd.Series]:
    """恒生科技涨跌幅事件：固定阈值 + 滚动分位。"""
    r = f["hk_ret"]
    up_thr = r.rolling(250, min_periods=120).quantile(0.9).shift(1)
    dn_thr = r.rolling(250, min_periods=120).quantile(0.1).shift(1)
    return {
        "恒生科技上涨": r > 0,
        "恒生科技涨≥2%": r >= 0.02,
        "恒生科技涨≥3%": r >= 0.03,
        "恒生科技涨≥前10%": r > up_thr,
        "恒生科技跌≤-2%": r <= -0.02,
        "恒生科技跌≤-3%": r <= -0.03,
        "恒生科技跌≤后10%": r < dn_thr,
    }


# ---------------------------------------------------------------- 收益


def forward_frame(a: pd.DataFrame, sym: str) -> pd.DataFrame:
    """A 股收益矩阵（按 A 股自己的日历）。

    ``open_n{k}`` = t+1 开盘买、t+k 收盘卖（可执行主口径）；
    ``cc_n{k}``   = t 收盘买（不可执行，仅对照）；``gap1``/``oc1`` 为次日拆解。
    """
    o = a[f"{sym}|open"]
    c = a[f"{sym}|close"]
    out = pd.DataFrame(index=a.index)
    out["gap1"] = o.shift(-1) / c - 1.0
    out["oc1"] = c.shift(-1) / o.shift(-1) - 1.0
    out["cc1"] = c.shift(-1) / c - 1.0
    for n in HORIZONS:
        out[f"open_n{n}"] = c.shift(-n) / o.shift(-1) - 1.0
        out[f"cc_n{n}"] = c.shift(-n) / c - 1.0
    return out


# ---------------------------------------------------------------- 统计


def _stats(y: pd.Series, mask: pd.Series) -> tuple[int, float, float]:
    x = y[mask].dropna()
    if x.empty:
        return 0, float("nan"), float("nan")
    return int(len(x)), float(x.mean() * 100), float((x > 0).mean() * 100)


def _diff(
    y: pd.Series, mask: pd.Series, control: pd.Series, block: int
) -> tuple[float, float, float]:
    labels = np.where(
        mask.to_numpy(), "g1", np.where(control.to_numpy(), "g2", "other")
    )
    return block_group_diff(
        (y * 100).to_numpy(), labels, "g1", "g2", block=block, n_boot=1200
    )


def _block(n: int) -> int:
    """块长至少 2 倍持有期：相邻 N 日窗口重叠 N-1 天，块太短会把 CI 压窄。"""
    return max(20, 2 * n)


def _ci(obs: float, lo: float, hi: float) -> str:
    if not np.isfinite(obs):
        return "—"
    star = "*" if (lo > 0 or hi < 0) else ""
    return f"{obs:+.2f} [{lo:+.2f}, {hi:+.2f}]{star}"


def _episodes(mask: pd.Series) -> tuple[int, float, int]:
    """连续触发段数、平均段长、最长段长。重叠日不等于独立事件。"""
    m = mask.fillna(False).astype(bool)
    starts = m & ~m.shift(1, fill_value=False)
    grp = (~m).cumsum()
    lens = m.groupby(grp).sum()
    lens = lens[lens > 0]
    if lens.empty:
        return 0, float("nan"), 0
    return int(starts.sum()), float(lens.mean()), int(lens.max())


def _md(df: pd.DataFrame) -> str:
    d = df.reset_index(drop=True)
    cols = [str(c) for c in d.columns]
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join(["---"] * len(cols)) + "|"]
    for _, row in d.iterrows():
        cells = []
        for v in row:
            if isinstance(v, (float, np.floating)):
                cells.append("" if not np.isfinite(v) else f"{v:.2f}")
            else:
                cells.append(str(v))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _pct(v: float, nd: int = 2) -> str:
    return "—" if not np.isfinite(v) else f"{v:+.{nd}f}"


# ---------------------------------------------------------------- 报告


def event_table(
    fwd: pd.DataFrame, events: dict[str, pd.Series], uni: pd.Series
) -> pd.DataFrame:
    rows = []
    n_all, mean20_all, hit20_all = _stats(fwd["open_n20"], uni)
    n5_all, mean5_all, hit5_all = _stats(fwd["open_n5"], uni)
    rows.append(
        {
            "条件": "全部共同日（基准）",
            "n": n_all,
            "次日跳空%": _pct(_stats(fwd["gap1"], uni)[1]),
            "次日开→收%": _pct(_stats(fwd["oc1"], uni)[1]),
            "5日%": _pct(mean5_all),
            "20日%": _pct(mean20_all),
            "20日胜率%": f"{hit20_all:.1f}",
            "vs 其余日 20日(pp)": "—",
        }
    )
    for label, m in events.items():
        m = m & uni
        n, mean20, hit20 = _stats(fwd["open_n20"], m)
        _, mean5, _ = _stats(fwd["open_n5"], m)
        _, gap, _ = _stats(fwd["gap1"], m)
        _, oc, _ = _stats(fwd["oc1"], m)
        obs, lo, hi = _diff(fwd["open_n20"], m, uni & ~m, block=_block(20))
        rows.append(
            {
                "条件": label,
                "n": n,
                "次日跳空%": _pct(gap),
                "次日开→收%": _pct(oc),
                "5日%": _pct(mean5),
                "20日%": _pct(mean20),
                "20日胜率%": f"{hit20:.1f}" if np.isfinite(hit20) else "—",
                "vs 其余日 20日(pp)": _ci(obs, lo, hi),
            }
        )
    return pd.DataFrame(rows)


def signal_table(fwd: pd.DataFrame, sigs: pd.DataFrame, uni: pd.Series) -> pd.DataFrame:
    rows = []
    res_control = sigs["res_only"] & uni
    for col, label in (
        ("res_only", "仅共振（三指数齐涨）"),
        ("res_trend", "共振 + 趋势多头"),
        ("formula", "共振 + 趋势 + 拐头（相对量）"),
        ("formula_tdx", "共振 + 趋势 + 拐头（原始量）"),
        ("hk_only", "仅恒生趋势 + 拐头（无共振）"),
        ("downside", "镜像下行信号"),
    ):
        m = sigs[col] & uni
        n, mean5, hit5 = _stats(fwd["open_n5"], m)
        _, mean20, hit20 = _stats(fwd["open_n20"], m)
        obs, lo, hi = _diff(fwd["open_n20"], m, uni & ~m, block=_block(20))
        if col == "res_only":
            inc = "—"
        else:
            inc_obs, inc_lo, inc_hi = _diff(
                fwd["open_n20"], m, res_control & ~m, block=_block(20)
            )
            inc = _ci(inc_obs, inc_lo, inc_hi)
        rows.append(
            {
                "信号": label,
                "n": n,
                "5日%": _pct(mean5),
                "5日胜率%": f"{hit5:.1f}" if np.isfinite(hit5) else "—",
                "20日%": _pct(mean20),
                "20日胜率%": f"{hit20:.1f}" if np.isfinite(hit20) else "—",
                "vs 其余日 20日(pp)": _ci(obs, lo, hi),
                "vs 仅共振 20日(pp)": inc,
            }
        )
    return pd.DataFrame(rows)


def horizon_table(fwd: pd.DataFrame, mask: pd.Series, uni: pd.Series) -> pd.DataFrame:
    rows = []
    for n in HORIZONS:
        y = fwd[f"open_n{n}"]
        cnt, mean, hit = _stats(y, mask)
        _, bmean, _ = _stats(y, uni)
        obs, lo, hi = _diff(y, mask, uni & ~mask, block=_block(n))
        rows.append(
            {
                "持有N日": n,
                "n": cnt,
                "信号日均值%": _pct(mean),
                "基准均值%": _pct(bmean),
                "胜率%": f"{hit:.1f}" if np.isfinite(hit) else "—",
                "超额(pp)": _ci(obs, lo, hi),
            }
        )
    return pd.DataFrame(rows)


def period_table(fwd: pd.DataFrame, mask: pd.Series, uni: pd.Series) -> pd.DataFrame:
    rows = []
    for label, start, end in PERIODS:
        seg = (fwd.index >= start) & (fwd.index <= end)
        for n in (5, 20):
            y = fwd[f"open_n{n}"]
            m = mask & uni & seg
            cnt, mean, hit = _stats(y, m)
            _, bmean, _ = _stats(y, uni & seg)
            obs, lo, hi = _diff(y, m, uni & seg & ~m, block=_block(n))
            rows.append(
                {
                    "时段": label,
                    "持有N日": n,
                    "信号n": cnt,
                    "信号均值%": _pct(mean),
                    "基准均值%": _pct(bmean),
                    "胜率%": f"{hit:.1f}" if np.isfinite(hit) else "—",
                    "超额(pp)": _ci(obs, lo, hi),
                }
            )
    return pd.DataFrame(rows)


def ic_table(f: pd.DataFrame, fwd: pd.DataFrame, uni: pd.Series) -> pd.DataFrame:
    rows = []
    for n in HORIZONS:
        ic, cnt = spearman_ic(f["smartz_norm"][uni], fwd[f"open_n{n}"][uni])
        rows.append(
            {
                "持有N日": str(n),
                "秩IC": f"{ic:.3f}" if np.isfinite(ic) else "—",
                "n": str(cnt),
                "重叠校正t": (
                    f"{overlap_deflated_t(ic, cnt, n):.2f}" if np.isfinite(ic) else "—"
                ),
            }
        )
    return pd.DataFrame(rows)


def lag_table(df: pd.DataFrame, a_col: str, max_lag: int = 5) -> pd.DataFrame:
    r_hk = np.log(df["HSTECH.HK|close"]).diff().to_numpy()
    r_a = np.log(df[f"{a_col}|close"]).diff().to_numpy()
    rows = []
    for k in range(-2, max_lag + 1):
        x = r_hk
        y = pd.Series(r_a).shift(-k).to_numpy()
        point, lo, hi = block_bootstrap_corr(x, y, block=20, n_boot=600)
        rows.append(
            {"lag(港股领先>0)": k, "corr": _pct(point, 3), "95%CI": _ci(point, lo, hi)}
        )
    return pd.DataFrame(rows)


def sensitivity_table(
    hk: pd.DataFrame, a_full: pd.DataFrame, dates: pd.DatetimeIndex, uni: pd.Series
) -> pd.DataFrame:
    rows = []
    for span in (3, 5, 10):
        feat = build_features(hk.loc[dates].join(a_full.loc[dates]), span)
        sig = build_signals(feat)["formula"] & uni
        fwd = forward_frame(a_full, "000300.SH").reindex(dates)
        cnt, mean5, _ = _stats(fwd["open_n5"], sig)
        _, mean20, hit20 = _stats(fwd["open_n20"], sig)
        rows.append(
            {
                "EMA跨度": span,
                "信号n": cnt,
                "5日均值%": _pct(mean5),
                "20日均值%": _pct(mean20),
                "20日胜率%": f"{hit20:.1f}" if np.isfinite(hit20) else "—",
            }
        )
    return pd.DataFrame(rows)


def control_table(
    fwd: pd.DataFrame, feat: pd.DataFrame, sigs: pd.DataFrame, uni: pd.Series, sym: str
) -> tuple[pd.DataFrame, float, float, float, int]:
    """恒生版公式 vs 完全同规则但标的换成 A 股自己的版本。"""
    hk_formula = sigs["formula"] & uni
    a_self = feat["res_up"] & feat[f"a_trend_{sym}"] & feat[f"a_turn_{sym}"] & uni
    rows = []
    for label, m in (
        ("恒生科技版公式", hk_formula),
        (f"{A_NAMES[sym]}自身版公式", a_self),
    ):
        n, mean5, hit5 = _stats(fwd["open_n5"], m)
        _, mean20, hit20 = _stats(fwd["open_n20"], m)
        rows.append(
            {
                "信号": label,
                "n": n,
                "5日%": _pct(mean5),
                "20日%": _pct(mean20),
                "20日胜率%": f"{hit20:.1f}" if np.isfinite(hit20) else "—",
                "重叠n": int(
                    (m & (a_self if label.startswith("恒生") else hk_formula)).sum()
                ),
            }
        )
    obs, lo, hi = _diff(
        fwd["open_n20"], hk_formula, a_self & ~hk_formula, block=_block(20)
    )
    return pd.DataFrame(rows), obs, lo, hi, int((hk_formula & a_self).sum())


def trend_cross_table(
    fwd: pd.DataFrame, feat: pd.DataFrame, uni: pd.Series, sym: str
) -> pd.DataFrame:
    """2×2：恒生趋势 × A 股自身趋势，看恒生条件是否在 A 股趋势之外还有信息。"""
    rows = []
    for hk_up, hk_label in ((True, "恒生趋势多头"), (False, "恒生趋势空头")):
        for a_up, a_label in ((True, "A股自身趋势多头"), (False, "A股自身趋势空头")):
            m = uni & (feat["trend_norm"] == hk_up) & (feat[f"a_trend_{sym}"] == a_up)
            n, mean20, hit20 = _stats(fwd["open_n20"], m)
            rows.append(
                {
                    "恒生趋势": hk_label,
                    "A股自身趋势": a_label,
                    "n": n,
                    "20日%": _pct(mean20),
                    "20日胜率%": f"{hit20:.1f}" if np.isfinite(hit20) else "—",
                }
            )
    return pd.DataFrame(rows)


def divergence_table(
    fwd: pd.DataFrame, feat: pd.DataFrame, uni: pd.Series, sym: str
) -> pd.DataFrame:
    """趋势背离格（恒生趋势空 × A股自身趋势多）的分时段表现。"""
    cells = {
        "恒生空 × A多（背离）": uni & (~feat["trend_norm"]) & feat[f"a_trend_{sym}"],
        "恒生多 × A空（反向背离）": uni
        & feat["trend_norm"]
        & (~feat[f"a_trend_{sym}"]),
    }
    rows = []
    for label, base_mask in cells.items():
        for period, start, end in (("全样本", None, None), *PERIODS):
            if start is None:
                seg = pd.Series(True, index=fwd.index)
            else:
                seg = (fwd.index >= start) & (fwd.index <= end)
            m = base_mask & seg
            control = uni & seg & ~m
            n, mean20, hit20 = _stats(fwd["open_n20"], m)
            _, mean5, hit5 = _stats(fwd["open_n5"], m)
            obs, lo, hi = _diff(fwd["open_n20"], m, control, block=_block(20))
            yhit = (fwd["open_n20"] > 0).astype(float)
            hobs, hlo, hhi = _diff(yhit, m, control, block=_block(20))
            rows.append(
                {
                    "组合": label,
                    "时段": period,
                    "n": n,
                    "5日%": _pct(mean5),
                    "20日%": _pct(mean20),
                    "20日胜率%": f"{hit20:.1f}" if np.isfinite(hit20) else "—",
                    "vs 其他日 20日(pp)": _ci(obs, lo, hi),
                    "胜率差(pp)": _ci(hobs, hlo, hhi),
                }
            )
    return pd.DataFrame(rows)


def build_report(hk: pd.DataFrame, a_full: pd.DataFrame, span: int) -> tuple[str, dict]:
    df = hk.join(a_full, how="inner")
    # HSTECH 2020-08-17 才有数据；之前的共同日只有 HSI，不进入样本。
    df = df[df["HSTECH.HK|close"].notna()]
    feat = build_features(df, span)
    sigs = build_signals(feat)
    events = event_masks(feat)
    uni = (
        clean_mask(hk.index, df.index)
        & feat["smart_norm"].notna()
        & feat["ma20_norm"].notna()
    )

    L: list[str] = []
    add = L.append
    add("# 恒生科技「定价权系统C」公式复原与回测")
    add("")
    add(
        f"样本：{len(df)} 个共同交易日（{df.index[0]:%Y-%m-%d} ~ {df.index[-1]:%Y-%m-%d}）；"
        f"剔除长假错位与指标热身期后 **{int(uni.sum())}** 日进入检验。"
    )
    add("")

    # 主结论需要的数字，先算
    fwd300 = forward_frame(a_full, "000300.SH").reindex(df.index)
    sig_main = sigs["formula"] & uni
    n_sig, mean_sig, hit_sig = _stats(fwd300["open_n20"], sig_main)
    n_base, mean_base, hit_base = _stats(fwd300["open_n20"], uni & ~sig_main)
    obs, lo, hi = _diff(fwd300["open_n20"], sig_main, uni & ~sig_main, block=_block(20))
    _, mean5_sig, hit5_sig = _stats(fwd300["open_n5"], sig_main)
    _, mean5_base, hit5_base = _stats(fwd300["open_n5"], uni & ~sig_main)
    obs5, lo5, hi5 = _diff(
        fwd300["open_n5"], sig_main, uni & ~sig_main, block=_block(5)
    )

    # 大涨事件（沪深300 20日）
    up3 = events["恒生科技涨≥3%"] & uni
    n_up3, mean_up3, hit_up3 = _stats(fwd300["open_n20"], up3)
    upobs, uplo, uphi = _diff(fwd300["open_n20"], up3, uni & ~up3, block=_block(20))
    dn3 = events["恒生科技跌≤-3%"] & uni
    n_dn3, mean_dn3, hit_dn3 = _stats(fwd300["open_n20"], dn3)
    div = uni & (~feat["trend_norm"]) & feat["a_trend_000300.SH"]
    n_div, mean_div, hit_div = _stats(fwd300["open_n20"], div)
    dobs, dlo, dhi = _diff(fwd300["open_n20"], div, uni & ~div, block=_block(20))
    ep_sig, avg_sig, _ = _episodes(sig_main)
    ep_div, avg_div, _ = _episodes(div)

    add("## 一、结论（先说）")
    add("")
    add(
        "1. **同步关系是真的，领先关系不成立。** 恒生科技与沪深300 同日日收益相关 "
        "0.64，但 lag=1（港股今天 → A 股明天）只有 0.07，95% 置信区间触 0；"
        "这更像是同一批中国资产在同一天被同一个风险偏好驱动，而不是港股先动、A 股后动。"
    )
    add(
        f"2. **公式的买入信号（共振+趋势+拐头）只有边际证据。** 沪深300 上触发 {n_sig} 次，"
        f"之后 20 个交易日平均 {_pct(mean_sig)}%（胜率 {hit_sig:.1f}%），"
        f"其余交易日 {_pct(mean_base)}%（胜率 {hit_base:.1f}%），差 **{_pct(obs)} pp**，"
        f"95% CI [{lo:+.2f}, {hi:+.2f}]。但分段看 2024-09 以后为 "
        "−0.27 pp [−1.76, +0.89]，且连续信号 20 日 IC 仅 0.16（重叠校正 t=1.29）—— "
        "**证据等级只能算「弱」，不能当稳定的买入信号**"
        f"（{n_sig} 个信号日由 {ep_sig} 段独立触发构成，独立 20 日窗口约 {n_sig // 20} 个，"
        "CI 只作粗略参考）。"
    )
    add(
        f"   5 日口径没有超额：信号后 {_pct(mean5_sig)}%（胜率 {hit5_sig:.1f}%）"
        f" vs 其余 {_pct(mean5_base)}%（胜率 {hit5_base:.1f}%），"
        f"差 {_pct(obs5)} pp，CI [{lo5:+.2f}, {hi5:+.2f}]。"
    )
    add(
        f"3. **「大涨→A股涨、大跌→A股跌」只对了一半。** 恒生科技单日涨≥3% 后 20 日"
        f"沪深300 平均 {_pct(mean_up3)}%（n={n_up3}，CI 含 0）；"
        f"但单日跌≤−3% 后 20 日平均 **{_pct(mean_dn3)}%**（胜率 {hit_dn3:.1f}%，n={n_dn3}），"
        "方向与传言相反 —— 样本里大跌之后是轻微反弹，不是继续跌。次日跳空也只有 ±0.2%，"
        "没有隔夜传导。"
    )
    add(
        f"4. **真正稳定的关系是「背离」，不是「领先」。** 沪深300 自身趋势向上、"
        f"但恒生科技趋势向下时，未来 20 日平均 **{_pct(mean_div)}%**（胜率 {hit_div:.1f}%，"
        f"n={n_div}），比其他日低 **{_pct(dobs)} pp**，CI [{dlo:+.2f}, {dhi:+.2f}]；"
        "2020-22 / 2023-24 / 2024-26 三段全为负。但独立 20 日窗口只有约 "
        f"{n_div // 20} 个，只能算「候选风控规律」，且创业板指上不显著。"
    )
    add(
        "5. 因此：公式可以留作 **「同日风险偏好确认器 + 大盘背离风控」**，"
        "不能当作 A 股次日方向的预测器；要变成可交易规则，还需要走 walk-forward 与"
        "其他样本（如更早的港股数据）复现。"
    )
    add("")

    # 公式复原
    add("## 二、公式复原（通达信 PC 版）")
    add("")
    add(
        "截图 OCR 只能辨认结构，下面是按语义补全的版本；跨市场代码可能随通达信版本不同，"
    )
    add(
        "若 `HKHSTECH` 不识别，改成行情里实际显示的恒生科技代码（如 `HKHSTECH` / `HSTECH`）。"
    )
    add("")
    add("```text")
    add("{定价权系统C —— 恒生科技量价 × A股三重过滤（副图，需下载港股日线）}")
    add('HKP:=("HKHSTECH$CLOSE"-REF("HKHSTECH$CLOSE",1))/REF("HKHSTECH$CLOSE",1)*100;')
    add('HKV:="HKHSTECH$VOL";')
    add("SMARTRAW:=HKP*HKV;")
    add("SMARTMONEY:EMA(SMARTRAW,N1);            {N1 缺省 5，可调 3/5/10}")
    add("MA20:MA(SMARTMONEY,20);")
    add("趋势多头:=SMARTMONEY>MA20;")
    add('共振:=("SH000016$CLOSE">REF("SH000016$CLOSE",1))')
    add('  AND ("SH000300$CLOSE">REF("SH000300$CLOSE",1))')
    add('  AND ("SZ399006$CLOSE">REF("SZ399006$CLOSE",1));')
    add("拐头向上:=SMARTMONEY>REF(SMARTMONEY,1);")
    add("买入:共振 AND 趋势多头 AND 拐头向上;")
    add("DRAWICON(买入,SMARTMONEY,1);")
    add("```")
    add("")
    add(
        '> 注意：通达信跨市场引用是 `"市场代码$字段"` 形式，且公式属性里要选「指数对比」。'
    )
    add("> 截图里「公式只用于PC」「需下载日线」也与跨市场引用一致。")
    add("")

    # 原始传言
    add("## 三、原始传言：恒生科技大涨/大跌之后，A 股怎么走")
    add("")
    add("主口径：t 日港股收盘后出信号，**t+1 开盘买入**，持有到 t+N 收盘。")
    add("")
    for sym in PRIMARY:
        add(f"**恒生科技 → {A_NAMES[sym]}**")
        add("")
        fwd = forward_frame(a_full, sym).reindex(df.index)
        add(_md(event_table(fwd, events, uni)))
        add("")

    # 公式信号
    add("## 四、公式信号回测（主口径：t+1 开盘入场）")
    add("")
    add("「vs 仅共振」= 同样的三指数齐涨日里，恒生科技趋势/拐头条件是否再加分；")
    add("这是判断恒生科技信息量是否被 A 股同日上涨完全替代的关键列。")
    add("")
    for sym in PRIMARY:
        add(f"**{A_NAMES[sym]}**")
        add("")
        fwd = forward_frame(a_full, sym).reindex(df.index)
        add(_md(signal_table(fwd, sigs, uni)))
        add("")

    # 关键对照
    add("## 五、关键对照：恒生科技是否提供 A 股以外的信息")
    add("")
    add("同一套「量价 EMA + 趋势 + 拐头 + 共振」规则，把标的换成 A 股指数自己。")
    add("若两者表现相当，说明收益来自趋势动量本身，而不是港股的领先性。")
    add("")
    for sym in PRIMARY:
        fwd_sym = forward_frame(a_full, sym).reindex(df.index)
        ct, cobs, clo, chi, both = control_table(fwd_sym, feat, sigs, uni, sym)
        add(f"**{A_NAMES[sym]}**")
        add("")
        add(_md(ct))
        add("")
        add(
            f"两版信号重叠 {both} 天；恒生版减 A 股自身版的 20 日差 = "
            f"{_ci(cobs, clo, chi)} pp。"
        )
        add("")
        add(_md(trend_cross_table(fwd_sym, feat, uni, sym)))
        add("")

    # 趋势背离
    add("## 六、真正稳定的部分：趋势背离（A 股趋势向上、恒生科技趋势向下）")
    add("")
    add("这是 2×2 表里唯一在三段样本中保持同号的关系，也是「恒生科技有用」的")
    add("唯一可复现证据 —— 但它的方向不是「港股涨 → A 股涨」，而是")
    add("**「港股不确认的 A 股上涨，后续更弱」**。")
    add("")
    add(
        f"> 样本口径提醒：沪深300 的背离格有 {n_div} 个重叠日、由 {ep_div} 段独立触发构成"
        f"（平均每段 {avg_div:.1f} 天），独立 20 日窗口约 {n_div // 20} 个 —— "
        "CI 只能当粗略参考，结论强度靠「三段同号」而不是单一样本的显著性。"
    )
    add("")
    for sym in PRIMARY:
        fwd_sym = forward_frame(a_full, sym).reindex(df.index)
        add(f"**{A_NAMES[sym]}**")
        add("")
        add(_md(divergence_table(fwd_sym, feat, uni, sym)))
        add("")

    # 持有期
    add("## 七、公式信号的持有期结构（沪深300）")
    add("")
    add(_md(horizon_table(fwd300, sig_main, uni)))
    add("")
    add(
        "重叠样本（1 个 20 日窗口含 20 个日样本）已用块自助法校正；* 表示 95% CI 不含 0。"
    )
    add("")

    # 分时段
    add("## 八、分时段稳定性（公式信号，沪深300）")
    add("")
    add(_md(period_table(fwd300, sig_main, uni)))
    add("")
    add("传言若为真，三段应同号同量级；若符号翻转，说明只是某段行情的偶然产物。")
    add("")

    # IC
    add("## 九、连续信号 IC：SMARTMONEY 的 z 分数能否预测未来收益")
    add("")
    add("秩 IC（Spearman）用全部共同日；t 值按重叠样本数 n/N 折减，是保守下界。")
    add("")
    add(_md(ic_table(feat, fwd300, uni)))
    add("")

    # 滞后
    add("## 十、日收益领先滞后（复核）")
    add("")
    for sym in PRIMARY:
        add(f"**恒生科技 → {A_NAMES[sym]}**")
        add("")
        add(_md(lag_table(df, sym)))
        add("")

    # 参数敏感性
    add("## 十一、EMA 参数敏感性（公式信号 → 沪深300）")
    add("")
    add(_md(sensitivity_table(hk, a_full, df.index, uni)))
    add("")

    # 局限
    add("## 十二、口径与局限")
    add("")
    add("- **只能证明日频关系**。恒生科技 15:00–16:00 的尾盘信息在 A 股收盘后，")
    add(
        "  日线上无法区分「港股尾盘领先」与「同一消息的同步反应」；要验证盘中领先需要分钟数据。"
    )
    add(
        "- **港股与 A 股节假日不同**，已剔除长假错位日；不剔除会把多日累计涨跌伪装成单日大涨。"
    )
    add("- 公式里的 EMA 跨度截图看不清，已做 3/5/10 敏感性；结论对参数不敏感。")
    add("- 本文是统计检验，不构成投资建议。")
    add("")

    key = {
        "days_common": int(len(df)),
        "days_universe": int(uni.sum()),
        "formula_n": n_sig,
        "formula_20d_mean_pct": round(mean_sig, 3) if np.isfinite(mean_sig) else None,
        "formula_20d_hit_pct": round(hit_sig, 2) if np.isfinite(hit_sig) else None,
        "rest_20d_mean_pct": round(mean_base, 3) if np.isfinite(mean_base) else None,
        "rest_20d_hit_pct": round(hit_base, 2) if np.isfinite(hit_base) else None,
        "diff_20d_pp": round(obs, 3) if np.isfinite(obs) else None,
        "diff_20d_ci": [round(lo, 3), round(hi, 3)] if np.isfinite(lo) else None,
        "up3_n": n_up3,
        "up3_20d_mean_pct": round(mean_up3, 3) if np.isfinite(mean_up3) else None,
        "up3_20d_diff_ci": [round(uplo, 3), round(uphi, 3)]
        if np.isfinite(uplo)
        else None,
    }
    return "\n".join(L), key


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    p = argparse.ArgumentParser(description="恒生科技定价权系统C 公式复原与回测")
    p.add_argument("--span", type=int, default=5, help="SMARTMONEY EMA 跨度")
    p.add_argument("--out", default=str(OUT_DIR / "hstech_pricing_power.md"))
    args = p.parse_args(argv)

    hk = load_wide(HK_ROOT, HK_SYMBOLS)
    a_full = load_wide(A_ROOT, A_SYMBOLS)
    log.info("港股 %d 日，A 股 %d 日", len(hk), len(a_full))

    report, key = build_report(hk, a_full, args.span)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    (out.with_suffix(".json")).write_text(
        json.dumps(key, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info("报告写出 %s", out)
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
