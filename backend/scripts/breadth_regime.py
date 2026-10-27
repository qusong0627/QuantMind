#!/usr/bin/env python3
"""广度指标的风险预警阈值研究 —— 「达到某个数值后，后续行情变好还是变差」。

与 ``trin_study.py`` 的区别：
- 那边测的是**连续值**的秩相关（弱）；
- 这边测的是**极端阈值触发**后的条件分布（用户真正要的用法）。

纪律：
1. 阈值不用全样本挑。每个交易日的「是否极端」只用**当天及以前**的分位判断
   （扩张窗口，起点 500 天），因此阈值本身随时间漂移、可实盘复现。
2. 同时报告 ``n_days``（重叠样本）与 ``n_episodes``（连续触发段数）——
   后者才是独立样本量。2016 年以来极端事件只有几十段，别拿 2000 个日样本
   冒充 2000 次独立观测。
3. 显著性用 Newey-West HAC t 值（滞后 = 预测窗口），再做 Benjamini-Hochberg
   FDR 校正 —— 13 个指标 × 2 侧 × 3 窗口 = 78 个检验，不校正等于自欺。
4. 风险面同时给出：后续窗口内最大回撤、跌超 5% 的概率。

用法::

    python -m backend.scripts.breadth_regime            # 全量报告
    python -m backend.scripts.breadth_regime --status   # 只看当前触发了什么
"""
from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from backend.scripts.research_stats import (
    expanding_pct_rank,
    forward_return,
    newyork_west_tstat,
)

log = logging.getLogger("breadth_regime")

PANEL_PATH = Path("data/quantdb/5_technical_derived/market_breadth/trin_daily.parquet")
TARGETS = {"000300.SH": "沪深300", "000852.SH": "中证1000", "000001.SH": "上证指数"}
HORIZONS = (5, 20, 40)
TAIL = 0.10
MIN_PERIODS = 500
MIN_EPISODE_DAYS = 1


@dataclass(frozen=True)
class Indicator:
    name: str
    column: str
    note: str


INDICATORS: tuple[Indicator, ...] = (
    Indicator("trin", "trin_amt", "TRIN 当日值（成交额版）"),
    Indicator("trin_ema10", "trin_ema10", "TRIN 10 日 EMA"),
    Indicator("trin_sum10", "trin_sum10", "TRIN 10 日成分和"),
    Indicator("adr", "adr", "上涨/下跌家数比"),
    Indicator("udr", "udr_amt", "上涨/下跌成交额比"),
    Indicator("breadth20", "pct_above_ma20", "站上 MA20 个股占比"),
    Indicator("breadth60", "pct_above_ma60", "站上 MA60 个股占比"),
    Indicator("mcclellan", "mcclellan", "McClellan 摆动"),
    Indicator("limit_net", "limit_net", "涨跌停净家数占比"),
    Indicator("amt_shock", "amt_shock", "全市场成交额 20 日 z 值"),
    Indicator("nh_nl", "nh_nl_pct", "(52 周新高 − 新低) 占比"),
    Indicator("adl_slope", "adl_slope", "ADL 20 日斜率（归一）"),
    Indicator("eqw_ret", "ret_equal_weight", "全市场等权收益"),
)


def load_panel(path: Path = PANEL_PATH) -> pd.DataFrame:
    df = pd.read_parquet(path).set_index("date").sort_index()
    n = df["n_total"].replace(0, np.nan)
    df["limit_net"] = (df["n_limit_up"] - df["n_limit_down"]) / n
    amt = df["amt_total"]
    df["amt_shock"] = (amt - amt.rolling(20).mean()) / amt.rolling(20).std()
    df["nh_nl_pct"] = (df["n_high_52w"] - df["n_low_52w"]) / n
    df["adl_slope"] = df["adl"].diff(20) / n.rolling(20).mean()
    return df


def build_features(df: pd.DataFrame, min_periods: int = MIN_PERIODS) -> pd.DataFrame:
    out = pd.DataFrame(index=df.index)
    for f in INDICATORS:
        out[f.name] = expanding_pct_rank(df[f.column], min_periods=min_periods)
    return out


def forward_path(close: pd.Series, horizon: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """t 收盘 → t+N 收盘：最终收益、窗口内最低点、窗口内最高点（均为相对 t 的比例）。"""
    vals = close.to_numpy(dtype=float)
    n = len(vals)
    ret = np.full(n, np.nan)
    low = np.full(n, np.nan)
    high = np.full(n, np.nan)
    for i in range(n - horizon):
        path = vals[i + 1 : i + 1 + horizon] / vals[i] - 1.0
        ret[i] = path[-1]
        low[i] = path.min()
        high[i] = path.max()
    return ret, low, high


def episode_count(mask: np.ndarray) -> int:
    """连续触发段数。mask 为 bool 序列。"""
    if len(mask) == 0:
        return 0
    edges = np.diff(np.r_[False, mask.astype(bool), False].astype(int))
    return int((edges == 1).sum())


def cluster_count(mask: np.ndarray, gap: int) -> int:
    """独立触点数：相邻触发至少间隔 ``gap`` 天，才算是两次独立观测。

    比 ``episode_count`` 严格 —— 同一个月里三次单日触发共用大部分重叠的
    20 日预测窗口，只能算一次。
    """
    idx = np.flatnonzero(mask)
    if len(idx) == 0:
        return 0
    count, last = 1, idx[0]
    for i in idx[1:]:
        if i - last >= gap:
            count += 1
            last = i
    return count


def bh_fdr(pvals: np.ndarray, alpha: float = 0.05) -> np.ndarray:
    """Benjamini-Hochberg 校正后的 q 值。"""
    p = np.asarray(pvals, dtype=float)
    q = np.full_like(p, np.nan)
    ok = np.isfinite(p)
    idx = np.where(ok)[0]
    if len(idx) == 0:
        return q
    order = idx[np.argsort(p[idx])]
    m = len(order)
    prev = 1.0
    for rank, i in enumerate(order[::-1], start=1):
        val = p[i] * m / (m - rank + 1)
        prev = min(prev, val)
        q[i] = prev
    return q


def _p_from_t(t: float) -> float:
    from scipy import stats as st

    return float(2 * (1 - st.norm.cdf(abs(t)))) if np.isfinite(t) else np.nan


def trigger_table(
    df: pd.DataFrame,
    feats: pd.DataFrame,
    target: str,
    tail: float = TAIL,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """每个指标两侧极端的条件统计 + 未来窗口风险面。"""
    rows = []
    risk = []
    base_risk: dict[int, dict[str, float]] = {}
    for n in HORIZONS:
        y, low, high = forward_path(df[f"close_{target}"], n)
        ok = np.isfinite(y)
        base_risk[n] = {
            "mdd_%": round(float(np.nanmin(low[ok])) * 100, 1),
            "mae_%": round(float(np.nanmean(low[ok])) * 100, 1),
            "mfe_%": round(float(np.nanmean(high[ok])) * 100, 1),
            "p_drop5": round(float((low[ok] <= -0.05).mean()), 3),
            "p_drop10": round(float((low[ok] <= -0.10).mean()), 3),
        }
    for f in INDICATORS:
        pct = feats[f.name].to_numpy()
        for side in ("low", "high"):
            mask = (pct <= tail) if side == "low" else (pct >= 1 - tail)
            mask = mask & np.isfinite(pct)
            if mask.sum() < 20:
                continue
            for n in HORIZONS:
                y, low, high = forward_path(df[f"close_{target}"], n)
                ok = np.isfinite(y)
                yv, mv = y[ok], mask[ok]
                if mv.sum() < 20:
                    continue
                base_mean = float(yv.mean()) * 100
                cond_mean = float(yv[mv].mean()) * 100
                t = newyork_west_tstat(yv, mv.astype(float), lags=n)
                rows.append(
                    {
                        "indicator": f.name,
                        "side": side,
                        "h": n,
                        "n_days": int(mv.sum()),
                        "n_episodes": episode_count(mv),
                        "n_clusters": cluster_count(mv, n),
                        "mean_%": round(cond_mean, 2),
                        "median_%": round(float(np.median(yv[mv])) * 100, 2),
                        "win": round(float((yv[mv] > 0).mean()), 3),
                        "base_%": round(base_mean, 2),
                        "delta_%": round(cond_mean - base_mean, 2),
                        "t_nw": round(t, 2),
                        "p": _p_from_t(t),
                    }
                )
                lo_v = low[ok][mv]
                hi_v = high[ok][mv]
                risk.append(
                    {
                        "indicator": f.name,
                        "side": side,
                        "h": n,
                        "n_days": int(mv.sum()),
                        "n_clusters": cluster_count(mv, n),
                        "mdd_%": round(float(np.nanmin(lo_v)) * 100, 1),
                        "avg_mae_%": round(float(np.nanmean(lo_v)) * 100, 1),
                        "p_drop5": round(float((lo_v <= -0.05).mean()), 3),
                        "p_drop10": round(float((lo_v <= -0.10).mean()), 3),
                        "avg_mfe_%": round(float(np.nanmean(hi_v)) * 100, 1),
                        "base_mdd_%": base_risk[n]["mdd_%"],
                        "base_mae_%": base_risk[n]["mae_%"],
                        "base_p_drop5": base_risk[n]["p_drop5"],
                        "base_p_drop10": base_risk[n]["p_drop10"],
                        "base_mfe_%": base_risk[n]["mfe_%"],
                        "lift_drop5": round(
                            float((lo_v <= -0.05).mean()) / base_risk[n]["p_drop5"] - 1, 2
                        ) if base_risk[n]["p_drop5"] > 0 else np.nan,
                    }
                )
    tdf = pd.DataFrame(rows)
    rdf = pd.DataFrame(risk)
    if not tdf.empty:
        tdf["q_fdr"] = bh_fdr(tdf["p"].to_numpy())
    return tdf, rdf


def threshold_sensitivity(
    df: pd.DataFrame, feats: pd.DataFrame, target: str, names: tuple[str, ...]
) -> pd.DataFrame:
    """阈值敏感性：5/10/20/30% 尾部门槛下的 20 日条件均值。

    如果只有某一个门槛亮、相邻门槛全灭，那是噪声不是信号。
    """
    rows = []
    for name in names:
        pct = feats[name].to_numpy()
        y, _, _ = forward_path(df[f"close_{target}"], 20)
        ok = np.isfinite(y)
        yv, pv = y[ok], pct[ok]
        row = {"indicator": name, "base_%": round(float(yv.mean()) * 100, 2)}
        for tail in (0.05, 0.10, 0.20, 0.30):
            low_m = pv <= tail
            high_m = pv >= 1 - tail
            row[f"low{int(tail*100)}_%"] = (
                round(float(yv[low_m].mean()) * 100, 2) if low_m.sum() >= 15 else np.nan
            )
            row[f"high{int(tail*100)}_%"] = (
                round(float(yv[high_m].mean()) * 100, 2) if high_m.sum() >= 15 else np.nan
            )
        rows.append(row)
    return pd.DataFrame(rows)


#: 综合压力分数：+1 表示分位越高越"紧张"，-1 表示分位越低越"紧张"
STRESS_COMPONENTS: tuple[tuple[str, int], ...] = (
    ("breadth20", -1),
    ("breadth60", -1),
    ("adl_slope", -1),
    ("nh_nl", -1),
    ("mcclellan", -1),
    ("trin_ema10", +1),
)


def composite_scores(feats: pd.DataFrame) -> pd.DataFrame:
    """把 6 个方向一致的指标压成 0~100 的「广度压力分」。

    分位本身已经是扩张窗口算出来的，所以分数跨年份可比、且不含未来信息。
    高分 = 市场处于广度压力（下跌/普跌）状态；低分 = 普遍走强的过热状态。
    """
    parts = []
    for name, sign in STRESS_COMPONENTS:
        p = feats[name]
        parts.append(p if sign > 0 else 1 - p)
    stress = sum(parts) / len(parts) * 100.0
    return pd.DataFrame({"stress": stress}, index=feats.index)


def score_threshold_table(
    df: pd.DataFrame, scores: pd.DataFrame, target: str
) -> pd.DataFrame:
    """综合分数的绝对阈值表 —— 直接回答「到这个数值后面会怎样」。"""
    rows = []
    y20, low20, high20 = forward_path(df[f"close_{target}"], 20)
    base = float(np.nanmean(y20)) * 100
    base_p5 = float(np.nanmean(low20 <= -0.05))
    base_p10 = float(np.nanmean(low20 <= -0.10))
    for thr in (90, 80, 70, 60):
        m = (scores["stress"] >= thr).to_numpy() & np.isfinite(y20)
        if m.sum() >= 20:
            rows.append(
                _score_row(m, y20, low20, high20, f"压力>={thr}", base, base_p5, base_p10)
            )
    for thr in (40, 30, 20, 10):
        m = (scores["stress"] <= thr).to_numpy() & np.isfinite(y20)
        if m.sum() >= 20:
            rows.append(
                _score_row(m, y20, low20, high20, f"压力<={thr}", base, base_p5, base_p10)
            )
    return pd.DataFrame(rows)


def _score_row(m, y20, low20, high20, label, base, base_p5, base_p10) -> dict:
    return {
        "rule": label,
        "n_days": int(m.sum()),
        "n_clusters": cluster_count(m, 20),
        "mean_%": round(float(np.nanmean(y20[m])) * 100, 2),
        "median_%": round(float(np.nanmedian(y20[m])) * 100, 2),
        "win": round(float(np.nanmean(y20[m] > 0)), 3),
        "base_%": round(base, 2),
        "p_drop5": round(float(np.nanmean(low20[m] <= -0.05)), 3),
        "base_p_drop5": round(base_p5, 3),
        "p_drop10": round(float(np.nanmean(low20[m] <= -0.10)), 3),
        "base_p_drop10": round(base_p10, 3),
        "avg_mfe_%": round(float(np.nanmean(high20[m])) * 100, 2),
    }


TREND_RULES = (
    ("低广度(<=20%) & 指数>MA20", "low", "gt20"),
    ("低广度(<=20%) & 指数<MA20", "low", "lt20"),
    ("高广度(>=80%) & 指数>MA60", "high", "gt60"),
    ("高广度(>=80%) & 指数<MA60", "high", "lt60"),
)


def trend_interaction_table(df: pd.DataFrame, feats: pd.DataFrame, target: str) -> pd.DataFrame:
    """同样的广度极端，在均线上方还是下方，后续是否不同。"""
    c = df[f"close_{target}"]
    ma20 = c.rolling(20).mean()
    ma60 = c.rolling(60).mean()
    y = forward_return(c, 20)
    br = feats["breadth20"]
    rows = []
    for label, side, trend in TREND_RULES:
        breadth_mask = (br <= 0.20) if side == "low" else (br >= 0.80)
        trend_mask = {
            "gt20": c > ma20,
            "lt20": c < ma20,
            "gt60": c > ma60,
            "lt60": c < ma60,
        }[trend]
        m = (breadth_mask & trend_mask).to_numpy() & np.isfinite(y.to_numpy())
        if m.sum() < 20:
            continue
        yy = y.to_numpy()[m]
        rows.append(
            {
                "rule": label,
                "n_days": int(m.sum()),
                "n_clusters": cluster_count(m, 20),
                "mean_%": round(float(yy.mean()), 2),
                "median_%": round(float(np.median(yy)), 2),
                "win": round(float((yy > 0).mean()), 3),
            }
        )
    base = y.dropna()
    rows.append(
        {
            "rule": "基准（全样本）",
            "n_days": len(base),
            "n_clusters": 1,
            "mean_%": round(float(base.mean()), 2),
            "median_%": round(float(base.median()), 2),
            "win": round(float((base > 0).mean()), 3),
        }
    )
    return pd.DataFrame(rows)


def current_status(df: pd.DataFrame, feats: pd.DataFrame) -> pd.DataFrame:
    latest = -1
    rows = []
    for f in INDICATORS:
        p = feats[f.name].iloc[latest]
        rows.append(
            {
                "indicator": f.name,
                "value": round(float(df[f.column].iloc[latest]), 4),
                "pct": round(float(p), 3),
                "zone": (
                    f"低 {int(TAIL*100)}%"
                    if p <= TAIL
                    else (f"高 {int(TAIL*100)}%" if p >= 1 - TAIL else "正常")
                ),
                "note": f.note,
            }
        )
    return pd.DataFrame(rows)


def _md(df: pd.DataFrame) -> str:
    d = df.reset_index(drop=True)
    cols = [str(c) for c in d.columns]
    lines = ["| " + " | ".join(cols) + " |",
             "|" + "|".join(["---"] * len(cols)) + "|"]
    for _, row in d.iterrows():
        cells = []
        for v in row:
            if isinstance(v, (float, np.floating)):
                cells.append("" if not np.isfinite(v) else f"{v:.4g}")
            elif pd.isna(v):
                cells.append("")
            else:
                cells.append(str(v))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def build_report(df: pd.DataFrame, feats: pd.DataFrame) -> str:
    L: list[str] = []
    a = L.append
    a("# 广度指标风险预警阈值研究")
    a("")
    a(f"数据区间 {df.index[0]:%Y-%m-%d} ~ {df.index[-1]:%Y-%m-%d}，{len(df)} 个交易日。"
      f"极端判定 = 扩张窗口（起点 {MIN_PERIODS} 天）分位，尾部门槛 {int(TAIL*100)}%。")
    a("")
    a("`n_days` 是重叠的日样本；`n_episodes` 是连续触发段数，**后者才是独立样本量**。")
    a("")

    scores = composite_scores(feats)
    a("## 一、当前触发了什么")
    a("")
    a(f"**综合广度压力分 = {scores['stress'].iloc[-1]:.1f} / 100**"
      f"（越高越紧张；60 以上为偏高压、80 以上为极端压力）。")
    a("")
    a(_md(current_status(df, feats)))
    a("")

    all_tables = []
    all_risk = []
    for target, tname in TARGETS.items():
        tdf, rdf = trigger_table(df, feats, target)
        if tdf.empty:
            continue
        tdf["target"] = tname
        rdf["target"] = tname
        all_tables.append(tdf)
        all_risk.append(rdf)
        a(f"## 二、{tname}：极端触发后的条件收益")
        a("")
        show = tdf[tdf.h == 20][
            ["indicator", "side", "n_days", "n_episodes", "n_clusters", "mean_%",
             "median_%", "win", "base_%", "delta_%", "t_nw", "q_fdr"]
        ].sort_values("q_fdr")
        a(_md(show))
        a("")
        a(f"### {tname}：同条件下的风险面（未来 20 日内）")
        a("")
        rshow = rdf[rdf.h == 20][
            ["indicator", "side", "n_clusters", "avg_mae_%", "base_mae_%",
             "p_drop5", "base_p_drop5", "lift_drop5", "p_drop10",
             "base_p_drop10", "avg_mfe_%", "base_mfe_%"]
        ].sort_values(["lift_drop5"], ascending=False)
        a(_md(rshow))
        a("")

    merged = pd.concat(all_tables, ignore_index=True) if all_tables else pd.DataFrame()
    merged_risk = pd.concat(all_risk, ignore_index=True) if all_risk else pd.DataFrame()

    a("## 三、综合分数阈值表（「到这个数值之后会怎样」）")
    a("")
    for target, tname in TARGETS.items():
        st = score_threshold_table(df, scores, target)
        if st.empty:
            continue
        a(f"**{tname} · 未来 20 日**")
        a("")
        a(_md(st))
        a("")
    a("压力分高 = 普跌/超卖状态；分低 = 普涨/过热状态。")
    a("")

    a("## 四、阈值敏感性（20 日条件均值 %）")
    a("")
    sig = (
        merged[(merged.h == 20) & (merged.q_fdr < 0.10)]["indicator"].unique()
        if not merged.empty
        else np.array([])
    )
    names = tuple(dict.fromkeys(list(sig) + ["trin_ema10", "breadth20", "adr", "udr"]))
    for target, tname in TARGETS.items():
        a(f"**{tname}**")
        a("")
        a(_md(threshold_sensitivity(df, feats, target, names)))
        a("")
    a("相邻门槛应当同向、单调；只有单点亮说明是噪声。")
    a("")

    a("## 五、广度 × 趋势：同样超卖，趋势里还是趋势外")
    a("")
    a("低广度只是「跌得多」；指数还在 MA20 上方时是上升趋势中的回踩，"
      "跌到 MA60 下方则是趋势走坏。两者后续完全不同。")
    a("")
    for target, tname in TARGETS.items():
        a(f"**{tname} · 未来 20 日**")
        a("")
        a(_md(trend_interaction_table(df, feats, target)))
        a("")

    a("## 六、全部窗口扫描（按 FDR q 排序，取前 60 行）")
    a("")
    if not merged.empty:
        full = merged[
            ["target", "indicator", "side", "h", "n_days", "n_episodes",
             "n_clusters", "mean_%", "win", "delta_%", "t_nw", "q_fdr"]
        ].sort_values(["q_fdr", "target", "indicator", "h"]).head(60)
        a(_md(full))
    a("")
    if not merged_risk.empty:
        a("### 风险面全表")
        a("")
        a(_md(merged_risk.sort_values(["target", "indicator", "side", "h"])))
        a("")
    return "\n".join(L)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="广度指标风险预警阈值研究")
    p.add_argument("--panel", default=str(PANEL_PATH))
    p.add_argument("--out", default=None)
    p.add_argument("--status", action="store_true", help="只打印当前状态")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)
    df = load_panel(Path(args.panel))
    feats = build_features(df)

    if args.status:
        scores = composite_scores(feats)
        print(f"最新交易日 {df.index[-1]:%Y-%m-%d}")
        print(f"综合广度压力分：{scores['stress'].iloc[-1]:.1f} / 100")
        print(current_status(df, feats).to_string(index=False))
        return 0

    report = build_report(df, feats)
    out = Path(args.out) if args.out else Path(args.panel).parent / "breadth_regime.md"
    out.write_text(report, encoding="utf-8")
    log.info("报告写出 %s", out)
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
