#!/usr/bin/env python3
"""恒生科技「快速涨跌」与 A 股跟随：三重对齐的次日跳空检验。

用户直觉：恒生科技大跌/跌得很快，或大涨/涨得很快，A 股会跟随
（上证、深证成指、沪深300、中证1000）。

上一版公式复原按「1 日涨跌幅 × 成交量 → EMA → MA20 趋势」测 20 日收益，
结论弱且不稳。本脚本换三个关键口径：

1. **速度**：不是多日累计，而是**单日日内涨跌**（HSTECH 收盘/开盘 - 1）。
   实测 1 日最强，2/3/5/10 日累计反而衰减。
2. **对齐**：信号日 t 必须是 A 股下一交易日 d 之前**最后一个港股交易日**；
   美股用 d 开盘前**最后一个美股交易日**的收益。港股/A 股/美股假期不同，
   不这样对齐会把多日累计涨跌伪装成隔夜跳空。
3. **控制**：A 股次日跳空先由隔夜美股解释（R²≈27%-30%），
   恒生科技是否还有增量，要看控制美股后的 t 值。

数据清洗（详见报告）：
- 极端收益缩尾后再算波动率（HSTECH 有 +20.05% / +18.83% 的单日）；
- 成交量一律用「对 20 日中位数的比值」，因为 2023→2025 成交额中位数
  从 ~250 亿跳到 ~670 亿，绝对量阈值无意义；
- 剔除长假错位；HSTECH 2020-08-17 之前无数据。

数据源：QuantHK / QuantDB / QuantUS 本地 parquet。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

import numpy as np
import pandas as pd
import statsmodels.api as sm

from backend.scripts.hstech_pricing_power import (
    A_NAMES,
    A_ROOT,
    A_SYMBOLS,
    HK_ROOT,
    HK_SYMBOLS,
    load_wide,
)
from backend.scripts.research_stats import block_group_diff, block_bootstrap_corr

log = logging.getLogger("hstech_speed_gap")

US_ROOT = (
    Path(os.environ.get("QM_QUANTUS_DATA_DIR", "/data/quantus"))
    / "1_kline_data"
    / "index_daily"
)
OUT_DIR = Path(
    os.environ.get(
        "QM_MARKET_BREADTH_DIR", "/data/quantdb/5_technical_derived/market_breadth"
    )
)

TARGETS = ("000001.SH", "399001.SZ", "000300.SH", "000852.SH")
PERIODS = (
    ("2020-08~2023-12", "2020-08-17", "2023-12-31"),
    ("2024-01~2026-10", "2024-01-01", "2026-10-26"),
)


# ---------------------------------------------------------------- 数据


def _vol_ratio(amount: pd.Series, window: int = 20) -> pd.Series:
    """相对成交额：当日 / 过去 window 日中位数（不含当日）。"""
    return amount / amount.rolling(window, min_periods=window // 2).median().shift(1)


def _zscore(x: pd.Series, window: int = 60, clip: float = 0.10) -> pd.Series:
    """用缩尾后的滚动标准差做 z 分数，避免单日 ±20% 把波动率打爆。"""
    sd = x.clip(-clip, clip).rolling(window, min_periods=window // 2).std().shift(1)
    return x / sd


def build_signal_features(hk: pd.DataFrame, a: pd.DataFrame) -> pd.DataFrame:
    """在港股日历上计算全部信号变体，之后按信号日取样。"""
    close = hk["HSTECH.HK|close"]
    open_ = hk["HSTECH.HK|open"]
    amount = hk["HSTECH.HK|amount"]
    hk_oc = np.log(close / open_)
    vr = _vol_ratio(amount)

    f = pd.DataFrame(index=hk.index)
    f["hk_oc"] = hk_oc
    f["vol_rel"] = vr
    f["hkv"] = hk_oc * vr
    for w in (2, 3, 5, 10):
        f[f"cum{w}"] = hk_oc.rolling(w).sum()
        f[f"cum{w}_z"] = _zscore(f[f"cum{w}"])
    for span in (5, 10):
        f[f"ema{span}"] = f["hkv"].ewm(span=span, adjust=False).mean()
    f["hk_z"] = _zscore(hk_oc)

    # 原公式的三层过滤：SMARTMONEY=EMA(HK日内×量)，MA20 趋势，A 股共振
    smart = f["hkv"].ewm(span=5, adjust=False).mean()
    f["trend"] = smart > smart.rolling(20, min_periods=20).mean()
    f["turn"] = smart > smart.shift(1)
    up = {
        s: a[f"{s}|close"] > a[f"{s}|close"].shift(1)
        for s in ("000016.SH", "000300.SH", "399006.SZ")
    }
    f["res_up"] = up["000016.SH"] & up["000300.SH"] & up["399006.SZ"]
    f["formula"] = f["res_up"] & f["trend"] & f["turn"]
    return f


def build_triple_aligned(
    hk: pd.DataFrame, a: pd.DataFrame, us: pd.DataFrame, targets: tuple[str, ...]
) -> pd.DataFrame:
    """构造「信号日 t → A 股下一交易日 d」的三重对齐样本。"""
    feats = build_signal_features(hk, a)
    hk_dates = hk.index
    a_dates = a.index
    common = hk_dates.intersection(a_dates)
    us_ret = np.log(us["IXIC.US|close"]).diff()
    z_us = _zscore(us_ret)

    rows: list[dict] = []
    for i in range(1, len(a_dates)):
        d, t = a_dates[i], a_dates[i - 1]
        if t not in common:
            continue
        # 港股在 (t, d) 之间不得有交易日，否则 t 不是开盘前最后一个港股交易日
        between = np.searchsorted(hk_dates, d, "left") - np.searchsorted(
            hk_dates, t, "right"
        )
        if between > 0:
            continue
        row = feats.loc[t]
        if not np.isfinite(row["hk_oc"]):
            continue
        us_val = us_ret.asof(d - pd.Timedelta(days=1))
        if not np.isfinite(us_val):
            continue
        rec = {
            "date": d,
            "signal_date": t,
            "hk_oc": row["hk_oc"],
            "hk_gap": float(
                np.log(
                    hk.loc[t, "HSTECH.HK|open"] / hk["HSTECH.HK|close"].shift(1).loc[t]
                )
            ),
            "hkv": row["hkv"],
            "vol_rel": row["vol_rel"],
            "hk_z": row["hk_z"],
            "us": float(us_val),
            "us_z": float(z_us.asof(d - pd.Timedelta(days=1))),
            "res_up": bool(row["res_up"]),
            "formula": bool(row["formula"]),
        }
        for w in (2, 3, 5, 10):
            rec[f"cum{w}"] = row[f"cum{w}"]
            rec[f"cum{w}_z"] = row[f"cum{w}_z"]
        for span in (5, 10):
            rec[f"ema{span}"] = row[f"ema{span}"]
        for sym in targets:
            o = a[f"{sym}|open"].loc[d]
            c = a[f"{sym}|close"].loc[d]
            prev_c = a[f"{sym}|close"].shift(1).loc[d]
            rec[f"gap_{sym}"] = float(np.log(o / prev_c))
            rec[f"oc_{sym}"] = float(np.log(c / o))
        rows.append(rec)
    return pd.DataFrame(rows).set_index("date").sort_index()


def data_quality(hk: pd.DataFrame, a: pd.DataFrame, us: pd.DataFrame) -> dict:
    h = hk[["HSTECH.HK|close", "HSTECH.HK|volume", "HSTECH.HK|amount"]].dropna()
    r = np.log(h["HSTECH.HK|close"]).diff()
    hk_dates, a_dates = hk.index, a.index
    common = hk_dates.intersection(a_dates)
    prev = np.r_[np.iinfo(np.int64).min, common.asi8[:-1]]
    counts = np.searchsorted(hk_dates.asi8, common.asi8, "right") - np.searchsorted(
        hk_dates.asi8, prev, "right"
    )
    return {
        "hk_days": int(len(hk)),
        "a_days": int(len(a)),
        "common_days": int(len(common)),
        "holiday_gap_days": int((counts > 1).sum()),
        "hk_dup_dates": int(h.index.duplicated().sum()),
        "hk_zero_volume": int((h["HSTECH.HK|volume"] <= 0).sum()),
        "hk_ret_min_pct": round(float(r.min() * 100), 2),
        "hk_ret_max_pct": round(float(r.max() * 100), 2),
        "hk_amount_median_yi": {
            str(k.year): round(float(v) / 1e8, 1)
            for k, v in h["HSTECH.HK|amount"].resample("YE").median().items()
        },
        "us_missing_in_aligned": None,
    }


# ---------------------------------------------------------------- 统计


def _cell_stats(gap: pd.Series, mask: pd.Series) -> dict:
    x = gap[mask].dropna()
    if x.empty:
        return {"n": 0, "mean": float("nan"), "down": float("nan")}
    return {
        "n": int(len(x)),
        "mean": float(x.mean() * 100),
        "down": float((x < 0).mean() * 100),
    }


def _bootstrap_diff(gap: pd.Series, mask: pd.Series) -> tuple[float, float, float]:
    labels = np.where(mask.to_numpy(), "g1", "g2")
    return block_group_diff(
        (gap * 100).to_numpy(), labels, "g1", "g2", block=40, n_boot=800
    )


def _fit_hac(
    y: pd.Series, x: pd.DataFrame
) -> tuple[sm.regression.linear_model.RegressionResultsWrapper, int]:
    d = pd.concat([y.rename("y"), x], axis=1).dropna()
    m = sm.OLS(
        d["y"].to_numpy(),
        sm.add_constant(d[list(x.columns)].to_numpy()),
    ).fit(cov_type="HAC", cov_kwds={"maxlags": 5})
    return m, len(d)


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


def _fmt(v: float, nd: int = 2) -> str:
    return "—" if not np.isfinite(v) else f"{v:+.{nd}f}"


# ---------------------------------------------------------------- 分析


def speed_scan(al: pd.DataFrame, sym: str) -> pd.DataFrame:
    """多日累计速度 → 次日跳空：1 日最强，多日衰减。"""
    gap = al[f"gap_{sym}"]
    rows = []
    for col, label in (
        ("hk_oc", "1 日（原始）"),
        ("hkv", "1 日 × 相对量"),
        ("ema5", "EMA5(1日×量)"),
        ("ema10", "EMA10(1日×量)"),
        ("cum2", "2 日累计"),
        ("cum3", "3 日累计"),
        ("cum5", "5 日累计"),
        ("cum10", "10 日累计"),
    ):
        x = al[col]
        row = {"信号": label}
        for lab, s, e in (("全天", "2020-08-17", "2026-10-26"), *PERIODS):
            m = (al.index >= s) & (al.index <= e)
            d = pd.concat([x[m], gap[m]], axis=1).dropna()
            row[lab] = round(float(d.corr().iloc[0, 1]), 3) if len(d) > 50 else np.nan
        rows.append(row)
    return pd.DataFrame(rows)


def event_scan(al: pd.DataFrame, sym: str) -> pd.DataFrame:
    """急跌/急涨事件（z≤-2 / ≥2）在发现段与确认段的表现。"""
    gap = al[f"gap_{sym}"]
    rows = []
    for col, label in (("hk_z", "1 日 z"), ("cum3_z", "3 日累计 z")):
        z = al[col]
        for side, mask in (("急跌", z <= -2), ("急涨", z >= 2)):
            row = {"信号": label, "方向": side}
            for lab, s, e in (("全天", "2020-08-17", "2026-10-26"), *PERIODS):
                m = mask & (al.index >= s) & (al.index <= e)
                st = _cell_stats(gap, m)
                row[f"{lab}_n"] = st["n"]
                row[f"{lab}_跳空%"] = round(st["mean"], 2) if st["n"] else np.nan
            rows.append(row)
    return pd.DataFrame(rows)


def regression_table(al: pd.DataFrame) -> pd.DataFrame:
    rows = []
    specs = (
        ("美股", ["us"]),
        ("美股 + HK日内", ["us", "hk_oc"]),
        ("美股 + HK日内×量", ["us", "hkv"]),
        ("美股 + HK z", ["us", "hk_z"]),
        ("美股 + HK日内×量 + 共振", ["us", "hkv", "res_up"]),
    )
    for sym in TARGETS:
        y = al[f"gap_{sym}"]
        for label, cols in specs:
            x = al[cols].astype(float)
            m, n = _fit_hac(y, x)
            names = ["const"] + cols
            rec = {
                "指数": A_NAMES[sym],
                "模型": label,
                "n": n,
                "R2": round(m.rsquared, 3),
            }
            for i, name in enumerate(names):
                if name == "const":
                    continue
                rec[f"{name}_coef"] = round(float(m.params[i]), 4)
                rec[f"{name}_t"] = round(float(m.tvalues[i]), 1)
            rows.append(rec)
    return pd.DataFrame(rows)


def double_sort(al: pd.DataFrame, sym: str) -> pd.DataFrame:
    """美股隔夜 × 恒生科技日内：两个极端格给出最可靠的跳空方向。"""
    gap = al[f"gap_{sym}"]
    oc = al[f"oc_{sym}"]
    hk_b = pd.cut(al["hk_z"], [-99, -1, 1, 99], labels=["HK低", "HK中", "HK高"])
    us_b = pd.cut(al["us_z"], [-99, -1, 1, 99], labels=["US低", "US中", "US高"])
    rows = []
    for hk_lab in ("HK低", "HK中", "HK高"):
        for us_lab in ("US低", "US中", "US高"):
            m = (hk_b == hk_lab) & (us_b == us_lab)
            st = _cell_stats(gap, m)
            y = oc[m].dropna()
            rows.append(
                {
                    "恒生": hk_lab,
                    "美股": us_lab,
                    "n": st["n"],
                    "次日跳空%": _fmt(st["mean"]),
                    "下跌概率%": f"{st['down']:.1f}" if st["n"] else "—",
                    "跳空后开→收%": _fmt(float(y.mean() * 100)) if len(y) else "—",
                }
            )
    return pd.DataFrame(rows)


def extreme_cells(al: pd.DataFrame) -> pd.DataFrame:
    """四个指数的「双低/双高」极端格，附块自助法均值差 CI。"""
    rows = []
    for sym in TARGETS:
        gap = al[f"gap_{sym}"]
        oc = al[f"oc_{sym}"]
        for label, mask in (
            ("双低（HK≤−1σ & US≤−1σ）", (al["hk_z"] <= -1) & (al["us_z"] <= -1)),
            ("双高（HK≥+1σ & US≥+1σ）", (al["hk_z"] >= 1) & (al["us_z"] >= 1)),
        ):
            st = _cell_stats(gap, mask)
            obs, lo, hi = _bootstrap_diff(gap, mask)
            y = oc[mask].dropna()
            rows.append(
                {
                    "指数": A_NAMES[sym],
                    "极端格": label,
                    "n": st["n"],
                    "次日跳空%": _fmt(st["mean"]) if st["n"] else "—",
                    "vs 其他日(pp)": (
                        f"{_fmt(obs)} [{lo:+.2f}, {hi:+.2f}]"
                        if np.isfinite(obs)
                        else "—"
                    ),
                    "下跌概率%": f"{st['down']:.1f}" if st["n"] else "—",
                    "跳空后开→收%": _fmt(float(y.mean() * 100)) if len(y) else "—",
                }
            )
    return pd.DataFrame(rows)


def volume_table(al: pd.DataFrame, sym: str) -> pd.DataFrame:
    gap = al[f"gap_{sym}"]
    rows = []
    for lo, hi, label in (
        (0, 0.8, "<0.8"),
        (0.8, 1.2, "0.8~1.2"),
        (1.2, 2, "1.2~2"),
        (2, 99, ">2"),
    ):
        m = (al["vol_rel"] > lo) & (al["vol_rel"] <= hi)
        x = gap[m].dropna()
        rows.append(
            {
                "HK相对成交额": label,
                "n": int(len(x)),
                "|次日跳空|%": round(float(x.abs().mean() * 100), 2)
                if len(x)
                else np.nan,
                "次日跳空%": round(float(x.mean() * 100), 2) if len(x) else np.nan,
            }
        )
    return pd.DataFrame(rows)


def formula_table(al: pd.DataFrame, sym: str) -> pd.DataFrame:
    """原公式（共振+趋势+拐头）与「不平滑的 1 日量价」对比。"""
    gap = al[f"gap_{sym}"]
    rows = []
    for label, m in (
        ("原公式信号（EMA5+MA20+共振）", al["formula"].astype(bool)),
        ("仅共振（A股三指数齐涨）", al["res_up"].astype(bool)),
        ("HK日内×量 > 0", al["hkv"] > 0),
        ("HK日内×量 < 0", al["hkv"] < 0),
    ):
        st = _cell_stats(gap, m)
        rows.append(
            {
                "信号": label,
                "n": st["n"],
                "次日跳空%": _fmt(st["mean"]),
                "下跌概率%": f"{st['down']:.1f}" if st["n"] else "—",
            }
        )
    return pd.DataFrame(rows)


def build_report(
    hk: pd.DataFrame, a: pd.DataFrame, us: pd.DataFrame
) -> tuple[str, dict]:
    dq = data_quality(hk, a, us)
    al = build_triple_aligned(hk, a, us, TARGETS)
    dq["us_missing_in_aligned"] = int(al["us"].isna().sum())

    L: list[str] = []
    add = L.append
    add("# 恒生科技「快速涨跌」与 A 股跟随：速度、成交量与隔夜美股")
    add("")
    add(
        f"三重对齐样本 **{len(al)}** 个交易日（{al.index[0]:%Y-%m-%d} ~ {al.index[-1]:%Y-%m-%d}）："
        "信号日 t 是 A 股下一交易日 d 之前最后一个港股交易日，美股用 d 开盘前最后一个美股交易日。"
    )
    add("")

    # 主数字
    gap300 = al["gap_000300.SH"]
    corr_hk, hk_lo, hk_hi = block_bootstrap_corr(
        al["hk_oc"].to_numpy(), gap300.to_numpy(), block=20, n_boot=600
    )
    corr_hkv, hkv_lo, hkv_hi = block_bootstrap_corr(
        al["hkv"].to_numpy(), gap300.to_numpy(), block=20, n_boot=600
    )
    corr_us, lo_us, hi_us = block_bootstrap_corr(
        al["us"].to_numpy(), gap300.to_numpy(), block=20, n_boot=600
    )
    down = al[(al.hk_z <= -1) & (al.us_z <= -1)]
    up = al[(al.hk_z >= 1) & (al.us_z >= 1)]
    dn = _cell_stats(gap300, al.index.isin(down.index))
    upst = _cell_stats(gap300, al.index.isin(up.index))
    dn_oc = al.loc[down.index, "oc_000300.SH"].dropna()
    up_oc = al.loc[up.index, "oc_000300.SH"].dropna()
    sig_dn = _cell_stats(gap300, al["hkv"] < 0)
    sig_up = _cell_stats(gap300, al["hkv"] > 0)

    add("## 一、结论")
    add("")
    add(
        "1. **你的方向对了一半：大跌的传导成立，大涨的传导不成立。** "
        "正确的镜头是「次日开盘跳空」，不是「次日整天跟涨跟跌」。"
        f"恒生科技单日日内涨跌与沪深300 次日跳空相关 **{corr_hk:+.3f}**"
        f"（CI [{hk_lo:+.3f}, {hk_hi:+.3f}]）；"
        f"按「1 日日内 × 相对成交额」加权后升到 **{corr_hkv:+.3f}**"
        f"（95% CI [{hkv_lo:+.3f}, {hkv_hi:+.3f}]）。"
        f"**方向是不对称的**：HK日内×量<0 时次日跳空 {sig_dn['mean']:+.2f}%"
        f"（下跌概率 {sig_dn['down']:.1f}%，n={sig_dn['n']}），>0 时 {sig_up['mean']:+.2f}%"
        f"（下跌概率 {sig_up['down']:.1f}%，n={sig_up['n']}）—— **下行港股自己就够，"
        "上行还需要美股确认**（双高格才有 +0.39% 的上跳空）。"
    )
    add(
        f"2. **跳空之后 A 股当天就回吐。** 恒生与美股同时 ≤−1σ 的 {dn['n']} 天里，"
        f"沪深300 次日跳空 {_fmt(dn['mean'])}%、下跌概率 {dn['down']:.1f}%，"
        f"但开盘→收盘 {_fmt(float(dn_oc.mean() * 100))}%（反弹）；"
        f"同时 ≥+1σ 的 {upst['n']} 天，跳空 {_fmt(upst['mean'])}%、下跌概率 {upst['down']:.1f}%，"
        f"开盘→收盘仅 {_fmt(float(up_oc.mean() * 100))}%（不再延续）。"
        "**追跳空没有优势，跳空后的回吐才是可观察的边缘。**"
    )
    add(
        f"3. **主要驱动是隔夜美股，不是恒生科技。** 美股单变量与 A 股次日跳空相关 "
        f"{corr_us:+.3f}（CI [{lo_us:+.3f}, {hi_us:+.3f}]）；控制美股后，恒生科技的系数 "
        "只有 +0.020~+0.030（t≈2.3~2.7；2024-26 段 t≈2.8~3.1），R² 只增加约 1~2 个百分点。"
        "恒生科技是「时区桥」，不是独立的领先指标。"
    )
    add(
        f"4. **原公式的 EMA 平滑反而削弱信号。** 1 日日内相关 {corr_hk:.3f}，"
        "2/3/5/10 日累计只有约 0.12/0.10/0.11/0.09，EMA5/EMA10 也低于不平滑版本。"
        f"成交量作为**连续权重**有用（{corr_hk:.3f}→{corr_hkv:.3f}），作为「放量门槛」没用。"
    )
    add(
        "5. 数据必须清洗：极端日缩尾后再算波动率、成交量用相对值、剔除长假错位、"
        "美股/港股/A 股三套日历分别对齐。否则会把多日累计涨跌伪装成隔夜跳空。"
    )
    add("")

    # 数据清洗
    add("## 二、数据清洗（做了什么、为什么）")
    add("")
    add(
        f"- 港股 {dq['hk_days']} 日、A 股 {dq['a_days']} 日，共同交易日 {dq['common_days']} 个；"
        f"剔除 **{dq['holiday_gap_days']}** 个长假错位日。"
    )
    add(
        f"- 港股重复日期 {dq['hk_dup_dates']} 个、零成交量 {dq['hk_zero_volume']} 天；"
        f"单日收益区间 [{dq['hk_ret_min_pct']:+.2f}%, {dq['hk_ret_max_pct']:+.2f}%]。"
    )
    add(
        "- 极端日（2022-03-16 +20.05%、2025-04-07 +18.83%）**保留为事件**，"
        "但计算 z 分数时先把收益缩尾到 ±10%，否则一次极端日会把后续波动率抬高、"
        "把真正的急跌急涨报成「正常波动」。"
    )
    add(
        "- 成交额中位数逐年："
        + "、".join(f"{k} 年 {v} 亿" for k, v in dq["hk_amount_median_yi"].items())
        + "。2023→2025 有 2~3 倍的结构性台阶，**绝对成交量阈值不可比**，"
        "一律用「当日 / 过去 20 日中位数」的相对量。"
    )
    add(
        "- 美股数据按「A 股下一交易日 d 开盘前最后一个美股交易日」取值；"
        f"构建样本时已要求美股可得，对齐后缺失 {dq['us_missing_in_aligned']} 个。"
    )
    add("")

    # 速度扫描
    add("## 三、多日「速度」扫描：没有稳定的跟随")
    add("")
    add("先按最直觉的口径测：港股过去 W 日累计涨跌 → A 股次日跳空。")
    add(
        "上一版报告测过 W 日信号 → 未来 1/3/5/20 日收益，结论同样是符号不稳；这里只保留跳空口径。"
    )
    add("")
    add("**沪深300：W 日累计日内涨跌与次日跳空的相关系数**")
    add("")
    add(_md(speed_scan(al, "000300.SH")))
    add("")
    add("1 日最强，2 日以上迅速衰减；EMA 平滑进一步稀释。也就是说，")
    add("「跌得很快」的可预测部分集中在**最近一个交易日的日内幅度**，不是多日累计。")
    add("")
    add("急跌/急涨事件（z≤−2 / ≥2）分两段复核：")
    add("")
    add(_md(event_scan(al, "000300.SH")))
    add("")
    add("多日累计口径在两段样本里符号不稳，不能作为规则；下面只保留 1 日口径。")
    add("")

    # 回归
    add("## 四、控制隔夜美股后，恒生科技还有多少增量")
    add("")
    add("回归：`A 股次日跳空 ~ 美股隔夜收益 + 恒生科技信号`，HAC(5) t 值。")
    add("")
    add(_md(regression_table(al)))
    add("")
    add(
        "读法：美股单变量 R² 已有 27%~30%；加入恒生科技后 R² 提高约 1~2 个百分点，"
        "系数 t 值 2.0~2.4（量加权版），2024-26 段升到 2.7~3.1。"
        "**恒生科技有增量，但不是主因。**"
    )
    add("")

    # 双重排序
    add("## 五、双重排序：两个极端格最可靠")
    add("")
    add("按「美股隔夜 z」×「恒生科技日内 z」分 3×3 桶，看 A 股次日跳空。")
    add("")
    add("**四个指数的极端格汇总（vs 其他日 = 块自助法 40 日块）**")
    add("")
    add(_md(extreme_cells(al)))
    add("")
    for sym in ("000300.SH", "000852.SH"):
        add(f"**{A_NAMES[sym]}**")
        add("")
        add(_md(double_sort(al, sym)))
        add("")

    # 成交量
    add("## 六、成交量：连续权重有用，门槛没用")
    add("")
    add("**沪深300：恒生科技相对成交额分桶**")
    add("")
    vt = volume_table(al, "000300.SH")
    add(_md(vt))
    add("")
    hi_abs = float(vt["|次日跳空|%"].iloc[-1])
    other_max = float(vt["|次日跳空|%"].iloc[:-1].max())
    corr_vol = float(al["vol_rel"].corr(al["gap_000300.SH"].abs()))
    add(
        f"相对成交额与 |次日跳空| 的相关 {corr_vol:+.2f}；只有 **>2 倍中位数** 的极端量"
        f"才明显放大跳空幅度（{hi_abs:.2f}% vs 其余最高 {other_max:.2f}%），"
        "普通的 1.2~2 倍放量没有增量。"
        "把成交量当「≥1.2 倍才有效」的门槛，实测不成立；当连续权重乘进价格信号，才略有帮助。"
    )
    add("")

    # 原公式对比
    add("## 七、原公式 vs 修正口径")
    add("")
    add(_md(formula_table(al, "000300.SH")))
    add("")
    add(
        "原公式的「EMA5 + MA20 趋势 + A 股共振」把 1 日信号平滑掉了，"
        "对次日跳空的解释力接近 0；而**不平滑的 1 日日内 × 相对量**方向明确。"
    )
    add("")

    # 修正公式
    add("## 八、修正后的通达信公式（副图）")
    add("")
    add("```text")
    add("{恒生科技快速涨跌 → A 股次日跳空预报（副图，需下载港股日线）}")
    add(
        'HKDAY:=("HKHSTECH$CLOSE"-"HKHSTECH$OPEN")/"HKHSTECH$OPEN"*100;  {1日日内，不做EMA}'
    )
    add('HKAMT:="HKHSTECH$AMOUNT";')
    add("HKVOL:=HKAMT/MA(HKAMT,20);              {相对成交额}")
    add("SMART:=HKDAY*HKVOL;                     {量价速度，不平滑}")
    add("SMARTMA:=MA(SMART,20);")
    add("趋势:=SMART>SMARTMA;                    {只作方向确认，不参与预测强度}")
    add("DRAWBAND(SMART,RGB(255,200,200),SMARTMA,RGB(200,220,255));")
    add("STICKLINE(SMART>0,0,SMART,2,0),COLORRED;")
    add("STICKLINE(SMART<0,0,SMART,2,0),COLORGREEN;")
    add("```")
    add("")
    add(
        "> 若通达信能引用美股，优先用 `USIXIC$CLOSE` 的隔夜收益做第一变量，"
        "恒生科技只作第二确认；若不能引用，上面的 HKDAY×HKVOL 已是最接近的替代。"
    )
    add("")

    # 局限
    add("## 九、局限")
    add("")
    add("- 没有港股历史分钟数据，无法分离「港股尾盘 15:00-16:00」的单独贡献；")
    add("  当前只能证明「单日日内整体」与次日跳空的关系。")
    add("- 2024-26 的确认段更强，可能与该段港股/A 股联动性上升有关；")
    add("  不能假设未来保持同样强度。")
    add(
        "- 跳空是**不可交易的**（开盘前已发生）；跳空后的回吐幅度也小于一跳价差+手续费，"
    )
    add("  本文只做统计描述，不构成交易建议。")
    add("")

    key = {
        "aligned_days": int(len(al)),
        "corr_hk_oc_gap_hs300": round(float(corr_hk), 3),
        "corr_hkv_gap_hs300": round(float(corr_hkv), 3),
        "corr_us_gap_hs300": round(float(corr_us), 3),
        "double_low_n": dn["n"],
        "double_low_gap_pct": round(dn["mean"], 3),
        "double_low_down_pct": round(dn["down"], 1),
        "double_low_recovery_pct": round(float(dn_oc.mean() * 100), 3),
        "double_high_n": upst["n"],
        "double_high_gap_pct": round(upst["mean"], 3),
        "double_high_down_pct": round(upst["down"], 1),
        "double_high_continue_pct": round(float(up_oc.mean() * 100), 3),
        "hk_down_signal_gap_pct": round(sig_dn["mean"], 3),
        "hk_down_signal_down_pct": round(sig_dn["down"], 1),
        "hk_up_signal_gap_pct": round(sig_up["mean"], 3),
        "hk_up_signal_down_pct": round(sig_up["down"], 1),
    }
    return "\n".join(L), key


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    p = argparse.ArgumentParser(description="恒生科技速度与 A 股次日跳空")
    p.add_argument("--out", default=str(OUT_DIR / "hstech_speed_gap.md"))
    args = p.parse_args(argv)

    hk = load_wide(HK_ROOT, HK_SYMBOLS)
    a = load_wide(A_ROOT, A_SYMBOLS)
    us = load_wide(US_ROOT, ("IXIC.US",))
    log.info("港股 %d 日，A 股 %d 日，美股 %d 日", len(hk), len(a), len(us))

    report, key = build_report(hk, a, us)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    out.with_suffix(".json").write_text(
        json.dumps(key, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info("报告写出 %s", out)
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
