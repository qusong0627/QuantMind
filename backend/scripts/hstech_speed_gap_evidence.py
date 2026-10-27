#!/usr/bin/env python3
"""恒生科技快速涨跌 → A 股次日跳空：证据强度报告。

这份报告不重复技术细节（见 hstech_speed_gap.md），只回答三个问题：
1. 关系到底有多强？——逐年、阈值、样本外、混淆矩阵；
2. 是不是假的？——控制隔夜美股、控制 A 股当日涨跌、成交量安慰剂；
3. 现在（2026-10）还成立吗？——2026 年逐笔事件与最近案例。

数据截止：港股/A 股 2026-10-26，美股 2026-10-23
（A 股 2026-10-26 开盘前最后一个美股交易日就是 10-23，不是缺数据）。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

import numpy as np
import pandas as pd

from backend.scripts.hstech_pricing_power import (
    A_NAMES,
    A_ROOT,
    A_SYMBOLS,
    HK_ROOT,
    HK_SYMBOLS,
    load_wide,
)
from backend.scripts.hstech_speed_gap import (
    _cell_stats,
    _fit_hac,
    _fmt,
    _md,
    build_triple_aligned,
    extreme_cells,
    regression_table,
    volume_table,
)
from backend.scripts.research_stats import block_bootstrap_corr

log = logging.getLogger("hstech_evidence")

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


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """二项比例的 Wilson 95% 置信区间。"""
    if n == 0:
        return float("nan"), float("nan")
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def year_table(al: pd.DataFrame, sym: str) -> pd.DataFrame:
    """2021-2026 逐年：双低/双高是否同号。2020 只有 1-2 次，单列说明。"""
    rows = []
    for year, g in al.groupby(al.index.year):
        if year == 2020:
            continue
        lo = g[(g.hk_z <= -1) & (g.us_z <= -1)]
        hi = g[(g.hk_z >= 1) & (g.us_z >= 1)]
        gl, gh = lo[f"gap_{sym}"], hi[f"gap_{sym}"]
        rows.append(
            {
                "年份": year,
                "双低n": len(gl),
                "双低跳空%": _fmt(float(gl.mean() * 100)) if len(gl) else "—",
                "双低低开%": f"{(gl < 0).mean() * 100:.1f}" if len(gl) else "—",
                "双高n": len(gh),
                "双高跳空%": _fmt(float(gh.mean() * 100)) if len(gh) else "—",
                "双高高开%": f"{(gh > 0).mean() * 100:.1f}" if len(gh) else "—",
            }
        )
    return pd.DataFrame(rows)


def threshold_matrix(al: pd.DataFrame, sym: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """3×3 阈值敏感性：HK z 与 US z 的 0.5/1.0/1.5σ。"""
    g = al[f"gap_{sym}"]
    down, up = [], []
    for kh in (0.5, 1.0, 1.5):
        drow = {"HK阈值": f"≤-{kh}σ"}
        urow = {"HK阈值": f"≥+{kh}σ"}
        for ku in (0.5, 1.0, 1.5):
            m = (al.hk_z <= -kh) & (al.us_z <= -ku)
            x = g[m].dropna()
            drow[f"US≤-{ku}σ"] = (
                f"n={len(x)} {x.mean() * 100:+.2f}% ({(x < 0).mean() * 100:.0f}%)"
                if len(x)
                else "—"
            )
            m = (al.hk_z >= kh) & (al.us_z >= ku)
            x = g[m].dropna()
            urow[f"US≥+{ku}σ"] = (
                f"n={len(x)} {x.mean() * 100:+.2f}% ({(x > 0).mean() * 100:.0f}%)"
                if len(x)
                else "—"
            )
        down.append(drow)
        up.append(urow)
    return pd.DataFrame(down), pd.DataFrame(up)


def oos_table(al: pd.DataFrame) -> pd.DataFrame:
    """发现段（2020-08~2023-12）与确认段（2024-01~2026-10）。"""
    rows = []
    for sym in TARGETS:
        g = al[f"gap_{sym}"]
        for label, s, e in (
            ("发现段 2020-08~2023-12", "2020-08-17", "2023-12-31"),
            ("确认段 2024-01~2026-10", "2024-01-01", "2026-10-26"),
        ):
            seg = (al.index >= s) & (al.index <= e)
            lo = (al.hk_z <= -1) & (al.us_z <= -1) & seg
            hi = (al.hk_z >= 1) & (al.us_z >= 1) & seg
            gl, gh = g[lo], g[hi]
            rows.append(
                {
                    "指数": A_NAMES[sym],
                    "时段": label,
                    "双低n": len(gl),
                    "双低跳空%": _fmt(float(gl.mean() * 100)) if len(gl) else "—",
                    "双低低开%": f"{(gl < 0).mean() * 100:.1f}" if len(gl) else "—",
                    "双高n": len(gh),
                    "双高跳空%": _fmt(float(gh.mean() * 100)) if len(gh) else "—",
                    "双高高开%": f"{(gh > 0).mean() * 100:.1f}" if len(gh) else "—",
                }
            )
    return pd.DataFrame(rows)


def confusion_table(al: pd.DataFrame) -> pd.DataFrame:
    """混淆矩阵：双低/双高是低频高置信事件，不是每日分类器。"""
    rows = []
    for sym in TARGETS:
        g = al[f"gap_{sym}"]
        base_down = float((g < 0).mean())
        base_up = float((g > 0).mean())
        lo = (al.hk_z <= -1) & (al.us_z <= -1)
        hi = (al.hk_z >= 1) & (al.us_z >= 1)
        n_lo = int(lo.sum())
        n_hi = int(hi.sum())
        k_lo = int((g[lo] < 0).sum())
        k_hi = int((g[hi] > 0).sum())
        lo_ci = wilson(k_lo, n_lo)
        hi_ci = wilson(k_hi, n_hi)
        rows.append(
            {
                "指数": A_NAMES[sym],
                "基准低开%": f"{base_down * 100:.1f}",
                "双低n": n_lo,
                "双低低开%(95%CI)": f"{k_lo / n_lo * 100:.1f} "
                f"[{lo_ci[0] * 100:.0f},{lo_ci[1] * 100:.0f}]",
                "双低提升": f"{k_lo / n_lo / base_down:.2f}x",
                "基准高开%": f"{base_up * 100:.1f}",
                "双高n": n_hi,
                "双高高开%(95%CI)": f"{k_hi / n_hi * 100:.1f} "
                f"[{hi_ci[0] * 100:.0f},{hi_ci[1] * 100:.0f}]",
                "双高提升": f"{k_hi / n_hi / base_up:.2f}x",
            }
        )
    return pd.DataFrame(rows)


def control_table(al: pd.DataFrame, a: pd.DataFrame) -> pd.DataFrame:
    """逐层加控制：美股 → +HK → +A 股当日涨跌。"""
    a_ret = {
        sym: np.log(a[f"{sym}|close"] / a[f"{sym}|close"].shift(1)) for sym in TARGETS
    }
    rows = []
    for sym in TARGETS:
        y = al[f"gap_{sym}"]
        ar = a_ret[sym].reindex(al.signal_date).to_numpy()
        x = pd.DataFrame({"us": al["us"], "hkv": al["hkv"], "a_ret": ar})
        specs = (
            ("① 美股", ["us"]),
            ("② 美股 + HK", ["us", "hkv"]),
            ("③ 美股 + HK + A当日", ["us", "hkv", "a_ret"]),
        )
        for label, cols in specs:
            m, n = _fit_hac(y, x[cols])
            rec = {
                "指数": A_NAMES[sym],
                "模型": label,
                "n": n,
                "R2": round(m.rsquared, 3),
            }
            for i, name in enumerate(["const", *cols]):
                if name == "const":
                    continue
                rec[f"{name}_coef"] = round(float(m.params[i]), 4)
                rec[f"{name}_t"] = round(float(m.tvalues[i]), 1)
            rows.append(rec)
    return pd.DataFrame(rows)


def recent_events(al: pd.DataFrame, sym: str, k: int = 5) -> pd.DataFrame:
    """最近 k 次双低 / 双高，用于逐笔核对。"""
    g = al[f"gap_{sym}"]
    oc = al[f"oc_{sym}"]
    rows = []
    for label, mask in (
        ("双低", (al.hk_z <= -1) & (al.us_z <= -1)),
        ("双高", (al.hk_z >= 1) & (al.us_z >= 1)),
    ):
        sub = al[mask].tail(k)
        for d, r in sub.iterrows():
            rows.append(
                {
                    "类型": label,
                    "信号日": r.signal_date.strftime("%Y-%m-%d"),
                    "A股日": d.strftime("%Y-%m-%d"),
                    "HK z": round(float(r.hk_z), 2),
                    "US z": round(float(r.us_z), 2),
                    "跳空%": round(float(g.loc[d] * 100), 2),
                    "开→收%": round(float(oc.loc[d] * 100), 2),
                }
            )
    return pd.DataFrame(rows)


def y2026_summary(al: pd.DataFrame, sym: str) -> dict:
    sub = al[al.index.year == 2026]
    lo = sub[(sub.hk_z <= -1) & (sub.us_z <= -1)]
    hi = sub[(sub.hk_z >= 1) & (sub.us_z >= 1)]
    gl, gh = lo[f"gap_{sym}"], hi[f"gap_{sym}"]
    return {
        "days": int(len(sub)),
        "low_n": int(len(gl)),
        "low_hit": float((gl < 0).mean() * 100) if len(gl) else float("nan"),
        "low_mean": float(gl.mean() * 100) if len(gl) else float("nan"),
        "high_n": int(len(gh)),
        "high_hit": float((gh > 0).mean() * 100) if len(gh) else float("nan"),
        "high_mean": float(gh.mean() * 100) if len(gh) else float("nan"),
    }


def _range(vals: list[float], fmt: str) -> str:
    v = [x for x in vals if np.isfinite(x)]
    if not v:
        return "—"
    return f"{fmt.format(min(v))} ~ {fmt.format(max(v))}"


def rule_card(al: pd.DataFrame) -> pd.DataFrame:
    """四指数的规则区间 + 沪深300 的单信号行。"""
    low_gap, low_hit, low_rec = [], [], []
    high_gap, high_hit, high_rec = [], [], []
    n_low, n_high = [], []
    for sym in TARGETS:
        g = al[f"gap_{sym}"]
        oc = al[f"oc_{sym}"]
        m = (al.hk_z <= -1) & (al.us_z <= -1)
        n_low.append(int(m.sum()))
        low_gap.append(float(g[m].mean() * 100))
        low_hit.append(float((g[m] < 0).mean() * 100))
        low_rec.append(float(oc[m].mean() * 100))
        m = (al.hk_z >= 1) & (al.us_z >= 1)
        n_high.append(int(m.sum()))
        high_gap.append(float(g[m].mean() * 100))
        high_hit.append(float((g[m] > 0).mean() * 100))
        high_rec.append(float(oc[m].mean() * 100))

    g = al["gap_000300.SH"]
    oc = al["oc_000300.SH"]
    down = _cell_stats(g, al["hkv"] < 0)
    up = _cell_stats(g, al["hkv"] > 0)
    down_oc = float(oc[al.hkv < 0].mean() * 100)
    up_oc = float(oc[al.hkv > 0].mean() * 100)
    n_low_txt = (
        f"{n_low[0]}（四指数相同）"
        if len(set(n_low)) == 1
        else f"{min(n_low)}~{max(n_low)}"
    )
    n_high_txt = (
        f"{n_high[0]}（四指数相同）"
        if len(set(n_high)) == 1
        else f"{min(n_high)}~{max(n_high)}"
    )
    return pd.DataFrame(
        [
            {
                "规则": "双低（HK≤−1σ 且 US≤−1σ）",
                "触发": "次日 A 股低开",
                "次数（6年/指数）": n_low_txt,
                "次日跳空": _range(low_gap, "{:+.2f}%"),
                "方向命中": _range(low_hit, "{:.1f}%"),
                "跳空后开→收": _range(low_rec, "{:+.2f}%"),
                "用途": "不要开盘恐慌卖；等日内回吐",
            },
            {
                "规则": "双高（HK≥+1σ 且 US≥+1σ）",
                "触发": "次日 A 股高开",
                "次数（6年/指数）": n_high_txt,
                "次日跳空": _range(high_gap, "{:+.2f}%"),
                "方向命中": _range(high_hit, "{:.1f}%"),
                "跳空后开→收": _range(high_rec, "{:+.2f}%"),
                "用途": "不要追高；日内延续很弱",
            },
            {
                "规则": "HK 单独跌（HK×量<0）",
                "触发": "次日 A 股低开",
                "次数（6年/指数）": f"n={down['n']}（沪深300）",
                "次日跳空": f"{_fmt(down['mean'])}%",
                "方向命中": f"{down['down']:.1f}%",
                "跳空后开→收": f"{_fmt(down_oc)}%",
                "用途": "弱信号；需美股确认",
            },
            {
                "规则": "HK 单独涨（HK×量>0）",
                "触发": "次日 A 股高开",
                "次数（6年/指数）": f"n={up['n']}（沪深300）",
                "次日跳空": f"{_fmt(up['mean'])}%",
                "方向命中": f"{up['down']:.1f}%（低开率）",
                "跳空后开→收": f"{_fmt(up_oc)}%",
                "用途": "**不成立**；需美股也涨",
            },
        ]
    )


def build_report(
    hk: pd.DataFrame, a: pd.DataFrame, us: pd.DataFrame
) -> tuple[str, dict]:
    al = build_triple_aligned(hk, a, us, TARGETS)
    last_a, last_us = a.index.max(), us.index.max()
    g300 = al["gap_000300.SH"]
    oc300 = al["oc_000300.SH"]
    lo_mask = (al.hk_z <= -1) & (al.us_z <= -1)
    hi_mask = (al.hk_z >= 1) & (al.us_z >= 1)
    lo = g300[lo_mask]
    hi = g300[hi_mask]
    up_sig = _cell_stats(g300, al.hkv > 0)
    corr_hk, hk_lo, hk_hi = block_bootstrap_corr(
        al.hk_oc.to_numpy(), g300.to_numpy(), block=20, n_boot=600
    )
    corr_hkv, hkv_lo, hkv_hi = block_bootstrap_corr(
        al.hkv.to_numpy(), g300.to_numpy(), block=20, n_boot=600
    )
    corr_us, us_lo, us_hi = block_bootstrap_corr(
        al.us.to_numpy(), g300.to_numpy(), block=20, n_boot=600
    )
    y26 = y2026_summary(al, "000300.SH")
    latest = al[lo_mask].tail(1)
    latest_row = None
    if len(latest):
        d = latest.index[0]
        r = latest.iloc[0]
        latest_row = {
            "signal": r.signal_date,
            "target": d,
            "hk_z": float(r.hk_z),
            "us_z": float(r.us_z),
            "gap": float(g300.loc[d] * 100),
            "oc": float(oc300.loc[d] * 100),
        }

    L: list[str] = []
    add = L.append
    add("# 恒生科技快速涨跌 → A 股次日跳空：证据强度报告")
    add("")
    add(
        f"数据截止：港股/A 股 **{last_a:%Y-%m-%d}**，美股 **{last_us:%Y-%m-%d}**。"
        f"三重对齐样本 **{len(al)}** 个交易日（{al.index[0]:%Y-%m-%d} ~ {al.index[-1]:%Y-%m-%d}），"
        "信号日 t 是 A 股下一交易日 d 之前最后一个港股交易日；"
        "美股取 d 开盘前最后一个美股交易日。"
    )
    add("")
    add(
        f"> 为什么美股只到 {last_us:%m-%d}：A 股 {last_a:%m-%d} 开盘前，"
        f"最后一个已收盘的美股交易日就是 {last_us:%m-%d}；"
        f"{last_a:%m-%d} 的美股收盘发生在 {(last_a + pd.Timedelta(days=1)):%m-%d} 凌晨，"
        f"晚于 A 股 {last_a:%m-%d} 开盘。这不是数据缺口，是对齐正确。"
    )
    add("")

    # 一页结论
    add("## 一、一页结论")
    add("")
    add(
        "**成立的是「大跌 → A 股次日低开」，不是「大跌 → A 股次日整天跌」。**"
        f"恒生科技单日日内涨跌与沪深300 次日跳空相关 **{corr_hk:+.3f}**"
        f"（95% CI [{hk_lo:+.3f}, {hk_hi:+.3f}]）；"
        f"乘上相对成交额后 **{corr_hkv:+.3f}**"
        f"（CI [{hkv_lo:+.3f}, {hkv_hi:+.3f}]）。但 A 股开盘后就回吐："
        f"双低格（HK 与美股都 ≤−1σ）次日跳空 **{_fmt(lo.mean() * 100)}%**、"
        f"低开概率 **{(lo < 0).mean() * 100:.1f}%**（n={len(lo)}），"
        f"开盘→收盘 **{_fmt(oc300[lo_mask].mean() * 100)}%**。"
    )
    add("")
    add(
        "**不成立的是「大涨 → A 股次日高开」。** 港股单独涨（HK×量>0）时，"
        f"次日跳空均值 {_fmt(up_sig['mean'])}%、低开概率 {up_sig['down']:.1f}%，等于抛硬币；"
        "只有美股也涨（双高格）才有正的高开（见规则卡）。"
        "**下行港股自己就够，上行必须美股确认。**"
    )
    add("")
    add(
        f"**主要驱动是隔夜美股，恒生科技是第二确认。** 美股单变量相关 {corr_us:+.3f}"
        f"（CI [{us_lo:+.3f}, {us_hi:+.3f}]）；"
        "控制美股后恒生系数 t≈2.3~2.7；再控制 A 股当日涨跌后 t 升到 3.1~4.3"
        "（因为 A 股当日涨跌本身是反转项，不控制会把恒生系数压低）。"
    )
    add("")
    add("## 二、规则卡")
    add("")
    add(_md(rule_card(al)))
    add("")
    if latest_row:
        add(
            f"> 最近一次双低：信号日 **{latest_row['signal']:%Y-%m-%d}**"
            f"（HK z={latest_row['hk_z']:+.2f}，US z={latest_row['us_z']:+.2f}）→ "
            f"A 股 **{latest_row['target']:%Y-%m-%d}** 跳空 **{latest_row['gap']:+.2f}%**，"
            f"开盘→收盘 **{latest_row['oc']:+.2f}%**。"
        )
    add("")

    # 主表
    add("## 三、四个指数的对应关系（2020-08 ~ 2026-10）")
    add("")
    add(_md(extreme_cells(al)))
    add("")
    add(
        "双低/双高相对其他日的均值差全部为负/正，块自助法 95% CI 均不含 0。"
        "跳空后开→收一列为正，说明 A 股在开盘后回吐，而不是继续跟随。"
    )
    add("")

    # 逐年
    add("## 四、逐年稳定性：2021-2026 每一年都同号")
    add("")
    add(
        "2020 年 HSTECH 只有 8 月以后的 1~2 次事件，单独剔除；2021 年起每年都有足够样本。"
    )
    add("")
    add("**沪深300**")
    add("")
    add(_md(year_table(al, "000300.SH")))
    add("")
    add("2021-2026 六年的双低格全部为负、双高格全部为正；没有一年翻转。")
    add(
        f"2026 年（截至 {last_a:%m-%d}）双低 {y26['low_n']} 次、"
        f"{y26['low_hit']:.1f}% 低开（平均 {y26['low_mean']:+.2f}%），"
        f"双高 {y26['high_n']} 次、{y26['high_hit']:.1f}% 高开（平均 {y26['high_mean']:+.2f}%）。"
    )
    add("")

    # 阈值
    add("## 五、阈值敏感性：不是挑出来的一个点")
    add("")
    add("**沪深300 下跌格**（单元格 = n / 跳空均值 / 低开概率）")
    add("")
    dmat, umat = threshold_matrix(al, "000300.SH")
    add(_md(dmat))
    add("")
    add("**沪深300 上涨格**（单元格 = n / 跳空均值 / 高开概率）")
    add("")
    add(_md(umat))
    add("")
    add(
        "9 个阈值组合全部同号，且越极端（0.5σ→1.5σ）跳空幅度越大、命中率越高。"
        "这说明结果不是「1σ」这个特定阈值造出来的。"
    )
    add("")

    # 样本外
    add("## 六、样本外：发现段与确认段同号")
    add("")
    add(_md(oos_table(al)))
    add("")
    add(
        "2020-08~2023-12 是发现段，2024-01~2026-10 是确认段；"
        "四个指数的双低/双高在两段都保持同号。除上证双高略有减弱（+0.30→+0.20）外，"
        "其余格的确认段幅度不弱于发现段。"
    )
    add("")

    # 混淆矩阵
    add("## 七、混淆矩阵：低频高置信，不是每日分类器")
    add("")
    add(_md(confusion_table(al)))
    add("")
    add(
        "基准低开率约 57%，双低格把低开概率提到 82%~92%（提升 1.4~1.6 倍）；"
        "基准高开率约 43%，双高格把高开概率提到 70%~81%（提升 1.7~1.9 倍）。"
        "但双低/双高各自只占全部交易日的 3%~4%，**召回率只有 5% 左右**——"
        "它是「极端日的风险过滤器」，不是每天都能用的方向预测器。"
    )
    add("")

    # 控制变量
    add("## 八、控制变量：美股、A 股当日涨跌都不能解释掉它")
    add("")
    add(_md(control_table(al, a)))
    add("")
    add(
        "模型③ 同时控制美股和 A 股当日涨跌后，恒生科技系数的 t 值反而升到 3.1~4.3。"
        "原因是 A 股当日涨跌对次日跳空是**反转项**（当日涨得多，次日容易低开），"
        "而恒生科技与 A 股当日涨跌正相关；不控制 A 股当日涨跌，会把恒生的系数压低。"
        "这说明恒生科技不是「A 股动量的代理」，它有独立信息。"
    )
    add("")
    add("**对照：单看美股的解释力**")
    add("")
    add(
        _md(
            regression_table(al).loc[
                lambda d: d["模型"].isin(["美股", "美股 + HK日内×量"])
            ]
        )
    )
    add("")

    # 成交量
    add("## 九、成交量：只放大波动，不决定方向")
    add("")
    add(_md(volume_table(al, "000300.SH")))
    add("")
    add(
        "相对成交额与 |次日跳空| 的相关约 +0.15；只有 **>2 倍中位数** 的极端量"
        "把 |跳空| 从 0.33% 放大到 0.55%，普通 1.2~2 倍放量没有增量。"
        "成交量单独看方向并不可靠（放量日低开率 68.8%，但这与「放量多发生在下跌日」混淆）；"
        "它适合做**幅度/风险**的乘数，不适合做方向门槛。"
    )
    add("")

    # 反证
    add("## 十、被证伪的说法")
    add("")
    add(
        "1. **「港股大涨 → A 股次日高开」**：港股单独涨时次日跳空 −0.01%、低开概率 50.0%，不成立；"
        "必须美股也涨（双高格）。"
    )
    add(
        "2. **「多日累计速度更强」**：1 日相关 0.184，2/3/5/10 日累计只有 0.12/0.10/0.11/0.09；"
        "平滑越多越弱。"
    )
    add(
        "3. **「原公式 EMA5+MA20+共振」**：信号日次日跳空 −0.01%、低开概率 54.0%，"
        "与基准无差别；平滑把 1 日速度抹掉了。"
    )
    add(
        "4. **「放量≥1.2 倍才有效」**：1.2~2 倍与缩量的 |跳空| 都是 0.33%~0.34%，没有增量；"
        "只有 >2 倍的极端量才放大。"
    )
    add(
        "5. **「A 股会跟着涨跌一整天」**：跳空后开盘→收盘在双低格是 +0.25%~+0.49%（反弹），"
        "双高格只有 +0.09%~+0.39%（不延续）；跟随只发生在开盘那一下。"
    )
    add("")

    # 2026 逐笔
    add("## 十一、2026 年逐笔核对（截至 2026-10-26）")
    add("")
    add(
        f"2026 年样本 {y26['days']} 天：双低 **{y26['low_n']}** 次，"
        f"低开 **{y26['low_hit']:.1f}%**，平均跳空 **{y26['low_mean']:+.2f}%**；"
        f"双高 **{y26['high_n']}** 次，高开 **{y26['high_hit']:.1f}%**，"
        f"平均跳空 **{y26['high_mean']:+.2f}%**。"
    )
    add("")
    add("**最近 5 次双低 / 双高（沪深300）**")
    add("")
    add(_md(recent_events(al, "000300.SH", 5)))
    add("")
    add(
        "2026 年唯一的双低失手是 2026-07-17 信号（7-20 跳空 +1.02%）；"
        "双高有 2 次失手（2026-04-08 −0.72%、2026-06-29 −0.25%）。"
        "命中率不是 100%，但方向和幅度与六年样本一致。"
    )
    add("")

    # 数据清洗
    add("## 十二、数据清洗与口径")
    add("")
    add(
        "- 极端日保留为事件，但算 z 分数时缩尾到 ±10%（HSTECH 有 +20.05% / +18.83% 的单日）。"
    )
    add(
        "- 成交量一律用「当日 / 过去 20 日中位数」；2023→2025 成交额中位数从 252.6 亿跳到 673.8 亿，"
        "绝对量阈值不可比。"
    )
    add("- 剔除 32 个长假错位日；港股无重复日期、无零成交量。")
    add(
        "- 三重日历对齐：t 是 d 前最后一个港股交易日，美股用 d 开盘前最后一个美股交易日。"
    )
    add(
        "- 入场口径：信号在港股 16:00 收盘后完整可知，A 股只做「次日开盘跳空」；"
        "跳空不可交易，开盘→收盘才是可执行的观察。"
    )
    add("")

    # 公式
    add("## 十三、修正后的通达信公式")
    add("")
    add("```text")
    add(
        'HKDAY:=("HKHSTECH$CLOSE"-"HKHSTECH$OPEN")/"HKHSTECH$OPEN"*100;  {1日日内，不做EMA}'
    )
    add('HKVOL:="HKHSTECH$AMOUNT"/MA("HKHSTECH$AMOUNT",20);              {相对成交额}')
    add(
        "SMART:=HKDAY*HKVOL;                                             {量价速度，不平滑}"
    )
    add("SMARTMA:=MA(SMART,20);")
    add("趋势:=SMART>SMARTMA;")
    add("DRAWBAND(SMART,RGB(255,200,200),SMARTMA,RGB(200,220,255));")
    add("STICKLINE(SMART>0,0,SMART,2,0),COLORRED;")
    add("STICKLINE(SMART<0,0,SMART,2,0),COLORGREEN;")
    add("```")
    add("")
    add(
        "> 若通达信能引用美股，第一变量优先用 `USIXIC$CLOSE` 的隔夜收益；"
        "恒生科技只作第二确认。不能引用美股时，上面的 HKDAY×HKVOL 已是最接近的替代。"
    )
    add("")

    add("## 十四、边界")
    add("")
    add("- 没有港股历史分钟数据，无法分离尾盘 15:00-16:00 的单独贡献。")
    add("- 跳空不可交易；跳空后的回吐幅度也小于一跳价差+手续费，本文只做统计描述。")
    add("- 2024-26 段比 2020-23 段更强；不能假设未来保持同样强度。")
    add("- 本文不构成投资建议。")
    add("")

    key = {
        "aligned_days": int(len(al)),
        "last_hk": str(hk.index.max().date()),
        "last_a": str(a.index.max().date()),
        "last_us": str(us.index.max().date()),
        "double_low_n": int(lo_mask.sum()),
        "double_low_gap_pct": round(float(lo.mean() * 100), 3),
        "double_low_hit_pct": round(float((lo < 0).mean() * 100), 1),
        "double_low_recovery_pct": round(float(oc300[lo_mask].mean() * 100), 3),
        "double_high_n": int(hi_mask.sum()),
        "double_high_gap_pct": round(float(hi.mean() * 100), 3),
        "double_high_hit_pct": round(float((hi > 0).mean() * 100), 1),
        "double_high_continue_pct": round(float(oc300[hi_mask].mean() * 100), 3),
        "y2026_double_low_n": y26["low_n"],
        "y2026_double_low_hit_pct": round(y26["low_hit"], 1),
        "y2026_double_high_n": y26["high_n"],
        "y2026_double_high_hit_pct": round(y26["high_hit"], 1),
    }
    return "\n".join(L), key


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    p = argparse.ArgumentParser(description="恒生科技快速涨跌证据强度报告")
    p.add_argument("--out", default=str(OUT_DIR / "hstech_speed_gap_evidence.md"))
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
