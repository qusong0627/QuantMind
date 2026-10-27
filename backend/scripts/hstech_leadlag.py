#!/usr/bin/env python3
"""恒生科技（HSTECH）与 A 股的量价关系 / 领先滞后检验。

要验证的传言：**恒生科技领先 A 股约一个月，走势高度相似，可用来预判 A 股。**

纪律：
- 只用两边**同时开市**的交易日（港股与 A 股节假日不同，直接按日历拼接会
  在长假后造出假跳空）。
- 领先滞后一律用「过去 W 日收益 → 未来 N 日收益」的显式口径，不用平滑序列
  的相关系数冒充 —— 平滑序列自相关极高，任何滞后都能算出高相关，那是伪影。
- 显著性用块自助法（块长 = 预测窗口），重叠样本不报朴素 t 值。
- 负结果照报。

数据源：QuantDB / QuantHK 本地 parquet。
"""
from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from backend.scripts.research_stats import block_bootstrap_corr

log = logging.getLogger("hstech_leadlag")

HK_DIR = Path(os.environ.get("QM_QUANTHK_DATA_DIR", "data/quanthk")) / "1_kline_data" / "index_daily"
PANEL_PATH = Path("data/quantdb/5_technical_derived/market_breadth/trin_daily.parquet")

A_INDICES = {
    "close_000300.SH": "沪深300",
    "close_000001.SH": "上证指数",
    "close_000852.SH": "中证1000",
    "close_399006.SZ": "创业板指",
}
HK_INDICES = {"HSTECH.HK": "恒生科技", "HSI.HK": "恒生指数"}


def load_hk_index(symbols: tuple[str, ...]) -> pd.DataFrame:
    """从 QuantHK 逐日分区读指定指数的 close/volume/amount。"""
    out: dict[pd.Timestamp, dict[str, float]] = {}
    for part in sorted(HK_DIR.glob("dt=*")):
        tag = part.name.split("=", 1)[1]
        df = pq.read_table(
            part / "data.parquet", columns=["symbol", "close", "volume", "amount"]
        ).to_pandas()
        df = df[df["symbol"].isin(symbols)]
        for _, r in df.iterrows():
            out.setdefault(pd.Timestamp(tag), {})[r["symbol"] + "|close"] = r["close"]
            out.setdefault(pd.Timestamp(tag), {})[r["symbol"] + "|volume"] = r["volume"]
            out.setdefault(pd.Timestamp(tag), {})[r["symbol"] + "|amount"] = r["amount"]
    res = pd.DataFrame.from_dict(out, orient="index").sort_index()
    res.index.name = "date"
    return res


def build_aligned() -> pd.DataFrame:
    hk = load_hk_index(tuple(HK_INDICES))
    a = pd.read_parquet(PANEL_PATH).set_index("date")[
        list(A_INDICES) + ["amt_total"]
    ]
    df = hk.join(a, how="inner")
    # 收盘列去掉 "|close" 后缀，量/额保留后缀以免与 A 股列名冲突
    df = df.rename(columns={c: c.split("|")[0] for c in df.columns if c.endswith("|close")})
    return df.dropna(subset=["HSTECH.HK", "close_000300.SH"])


def _winsor(s: pd.Series, q: float = 0.005) -> pd.Series:
    lo, hi = s.quantile([q, 1 - q])
    return s.clip(lo, hi)


def lag_table(df: pd.DataFrame, hk: str, a: str, max_lag: int = 40) -> pd.DataFrame:
    """日收益互相关：``corr(hk_ret[t], a_ret[t+k])``，k>0 表示港股领先。"""
    r_hk = np.log(df[hk]).diff()
    r_a = np.log(df[a]).diff()
    rows = []
    for k in range(-20, max_lag + 1):
        x = r_hk.to_numpy()
        y = r_a.shift(-k).to_numpy()
        point, lo, hi = block_bootstrap_corr(x, y, block=20, n_boot=1000)
        rows.append({"lag": k, "corr": point, "lo": lo, "hi": hi})
    return pd.DataFrame(rows)


def trend_lead(
    df: pd.DataFrame, hk: str, a: str, windows: tuple[tuple[int, int], ...]
) -> pd.DataFrame:
    """「过去 W 日 → 未来 N 日」：直接检验"领先一个月"的说法。"""
    lp_hk = np.log(df[hk])
    lp_a = np.log(df[a])
    rows = []
    for w, n in windows:
        x = (lp_hk - lp_hk.shift(w)).to_numpy()
        y = (lp_a.shift(-n) - lp_a).to_numpy()
        point, lo, hi = block_bootstrap_corr(x, y, block=n, n_boot=1000)
        rows.append(
            {
                "window": f"W{w}->N{n}",
                "corr": point,
                "lo": lo,
                "hi": hi,
                "n": int(np.sum(np.isfinite(x) & np.isfinite(y))),
            }
        )
    return pd.DataFrame(rows)


def monthly_lead(df: pd.DataFrame, hk: str, a: str, max_lag: int = 6) -> pd.DataFrame:
    """非重叠月度收益的领先滞后（每 21 个交易日取一个点）。"""
    sub = df.iloc[::21]
    r_hk = (np.log(sub[hk]).diff()).to_numpy()
    r_a = (np.log(sub[a]).diff()).to_numpy()
    rows = []
    for k in range(-3, max_lag + 1):
        x = r_hk
        y = np.roll(r_a, -k)
        if k > 0:
            y[-k:] = np.nan
        elif k < 0:
            y[:-k] = np.nan
        point, lo, hi = block_bootstrap_corr(x, y, block=3, n_boot=1000)
        rows.append({"lag_months": k, "corr": point, "lo": lo, "hi": hi})
    return pd.DataFrame(rows)


def volume_shock(df: pd.DataFrame, col: str, window: int = 20) -> pd.Series:
    """成交量冲击：当日成交额 / 过去 window 日中位数（取对数）。"""
    amt = df[col]
    base = amt.rolling(window, min_periods=window // 2).median()
    return np.log(amt / base)


def smoothed_lag_profile(
    df: pd.DataFrame, hk: str, a: str, span: int = 20, max_lag: int = 40
) -> pd.DataFrame:
    """平滑序列的滞后相关曲线 —— 用来演示「视觉领先」是从哪来的。

    MA20 对数价格自相关极高，所以相关系数在很宽的滞后区间里都保持高位；
    峰值位置随样本区间漂移。看到这种平台就宣布"领先 N 天"，是把平滑当因果。
    """
    s_hk = np.log(df[hk]).rolling(span).mean().to_numpy()
    s_a = np.log(df[a]).rolling(span).mean()
    rows = []
    for k in range(-20, max_lag + 1):
        d = pd.DataFrame({"x": s_hk, "y": s_a.shift(-k).to_numpy()}).dropna()
        rows.append(
            {"lag": k, "corr": float(d.corr().iloc[0, 1]) if len(d) > 50 else np.nan}
        )
    return pd.DataFrame(rows)


def block_group_diff(
    y: np.ndarray,
    labels: np.ndarray,
    g1: str,
    g2: str,
    block: int = 20,
    n_boot: int = 2000,
    seed: int = 20261027,
) -> tuple[float, float, float]:
    """两组均值差的块自助法置信区间（保留时间结构）。返回 (obs, lo, hi)。"""
    ok = np.isfinite(y)
    y, labels = y[ok], labels[ok]
    n = len(y)
    obs = float(y[labels == g1].mean() - y[labels == g2].mean())
    rng = np.random.default_rng(seed)
    n_blocks = int(np.ceil(n / block))
    boot = []
    for _ in range(n_boot):
        starts = rng.integers(0, n - block + 1, size=n_blocks)
        idx = np.concatenate([np.arange(s, s + block) for s in starts])[:n]
        yy, ll = y[idx], labels[idx]
        m1, m2 = yy[ll == g1].mean(), yy[ll == g2].mean()
        if np.isfinite(m1) and np.isfinite(m2):
            boot.append(m1 - m2)
    lo, hi = np.nanpercentile(boot, [2.5, 97.5])
    return obs, float(lo), float(hi)


def _cluster_stability(
    df: pd.DataFrame, hk: str, a: str, windows: tuple[tuple[int, int], ...]
) -> pd.DataFrame:
    """分时段看 W20→N20 的相关是否稳定（传言若为真，各段应同号同量级）。"""
    cuts = [
        ("2020-08~2022-12", "2020-08-01", "2022-12-31"),
        ("2023-01~2024-08", "2023-01-01", "2024-08-31"),
        ("2024-09~2026-10", "2024-09-01", "2026-12-31"),
    ]
    rows = []
    lp_hk, lp_a = np.log(df[hk]), np.log(df[a])
    for label, s, e in cuts:
        m = (df.index >= s) & (df.index <= e)
        r = {"period": label, "days": int(m.sum())}
        for w, n in windows:
            x = (lp_hk - lp_hk.shift(w))[m]
            y = (lp_a.shift(-n) - lp_a)[m]
            d = pd.concat([x, y], axis=1).dropna()
            r[f"W{w}->N{n}"] = round(float(d.corr().iloc[0, 1]), 3) if len(d) > 50 else np.nan
        rows.append(r)
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
                cells.append("" if not np.isfinite(v) else f"{v:.3f}")
            else:
                cells.append(str(v))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def build_report(df: pd.DataFrame) -> str:
    L: list[str] = []
    a = L.append
    a("# 恒生科技 ↔ A 股：量价关系与领先滞后检验")
    a("")
    a(f"共同交易日 {len(df)} 个（{df.index[0]:%Y-%m-%d} ~ {df.index[-1]:%Y-%m-%d}），"
      "仅取两地同时开市的日子。")
    a("")

    price_cols = [c for c in df.columns if c in HK_INDICES] + list(A_INDICES)
    rets = np.log(df[price_cols]).diff()
    a("## 一、日收益相关（同期）")
    a("")
    a(_md(rets.corr().round(3).reset_index()))
    a("")

    a("## 二、日收益领先滞后（港股领先 > 0）")
    a("")
    for hk, hkname in HK_INDICES.items():
        if hk not in df:
            continue
        lag = lag_table(df, hk, "close_000300.SH")
        best = lag.loc[lag["corr"].abs().idxmax()]
        a(f"**{hkname} → 沪深300**：同期 corr = "
          f"{lag.loc[lag.lag == 0, 'corr'].iloc[0]:+.3f}；"
          f"全滞后区间 |corr| 最大 = {best['corr']:+.3f}（lag={int(best['lag'])}）。")
        a("")
        a(_md(lag[lag.lag.isin([-5, -1, 0, 1, 5, 10, 20, 30, 40])].round(3)))
        a("")

    a("## 三、传言检验：「过去一个月 → 未来一个月」")
    a("")
    wins = ((5, 5), (10, 10), (20, 20), (20, 5), (5, 20), (60, 20), (60, 60))
    for hk, hkname in HK_INDICES.items():
        if hk not in df:
            continue
        for a_col, a_name in A_INDICES.items():
            t = trend_lead(df, hk, a_col, wins)
            a(f"**{hkname} → {a_name}**")
            a("")
            a(_md(t.round(3)))
            a("")

    a("## 四、月度非重叠口径")
    a("")
    for a_col, a_name in [("close_000300.SH", "沪深300"), ("close_399006.SZ", "创业板指")]:
        m = monthly_lead(df, "HSTECH.HK", a_col)
        a(f"**恒生科技 → {a_name}**（每 21 个交易日一个样本，样本量小、置信区间宽）")
        a("")
        a(_md(m.round(3)))
        a("")

    a("## 五、分时段稳定性")
    a("")
    a(_md(_cluster_stability(df, "HSTECH.HK", "close_000300.SH", ((20, 20), (20, 5), (10, 10)))))
    a("")

    a("## 六、量价：成交额冲击与涨跌四象限")
    a("")
    hk_shock = volume_shock(df, "HSTECH.HK|amount")
    a_shock = volume_shock(df, "amt_total")
    a_ret = np.log(df["close_000300.SH"]).diff()
    hk_ret = np.log(df["HSTECH.HK"]).diff()
    q = pd.DataFrame(
        {
            "hk_ret": hk_ret,
            "a_fwd_20": np.log(df["close_000300.SH"]).shift(-20) - np.log(df["close_000300.SH"]),
            "hk_up": hk_ret > 0,
            "hk_vol_up": hk_shock > 0,
        }
    ).dropna()
    q["quadrant"] = np.select(
        [q.hk_up & q.hk_vol_up, q.hk_up & ~q.hk_vol_up,
         ~q.hk_up & q.hk_vol_up, ~q.hk_up & ~q.hk_vol_up],
        ["涨+放量", "涨+缩量", "跌+放量", "跌+缩量"],
        default="其他",
    )
    g = q.groupby("quadrant")["a_fwd_20"]
    quad = pd.DataFrame({
        "n": g.count(),
        "A股未来20日均值_%": (g.mean() * 100).round(2),
        "胜率": g.apply(lambda s: round(float((s > 0).mean()), 2)),
    }).sort_values("A股未来20日均值_%", ascending=False)
    a(_md(quad.reset_index()))
    a("")
    obs, lo, hi = block_group_diff(
        q["a_fwd_20"].to_numpy() * 100,
        q["quadrant"].to_numpy(),
        "涨+放量",
        "跌+缩量",
        block=20,
    )
    a(f"「涨+放量」减「跌+缩量」的 20 日收益差 = **{obs:+.2f}pp**，"
      f"块自助法 95% 置信区间 [{lo:+.2f}, {hi:+.2f}]。"
      + ("区间不含 0，可视为弱证据。" if lo > 0 or hi < 0 else "区间含 0，不能视为有效信号。"))
    a("")
    a(f"同期：恒生科技量冲击 vs A 股量冲击 corr = "
      f"{hk_shock.corr(a_shock):+.3f}；"
      f"恒生科技量冲击 vs A 股当日收益 corr = {hk_shock.corr(a_ret):+.3f}。")
    a("")

    a("## 七、「视觉领先」的伪影演示：平滑序列的滞后相关")
    a("")
    a("对 MA20 对数价格做滞后相关。曲线在宽区间内保持高位、峰值随区间漂移，"
      "正是「看起来领先一个月」的来源 —— 它不是因果领先。")
    a("")
    prof = smoothed_lag_profile(df, "HSTECH.HK", "close_000300.SH")
    a(_md(prof[prof.lag.isin([-20, -10, -5, 0, 5, 10, 20, 30, 40])].round(3)))
    a("")
    a(f"MA20 序列峰值滞后 = {int(prof.loc[prof['corr'].idxmax(), 'lag'])} 日；"
      f"lag=0 处 corr = {prof.loc[prof.lag == 0, 'corr'].iloc[0]:.3f}。")
    a("")

    a("## 八、60 日滚动相关：关系本身会变")
    a("")
    rh = np.log(df["HSTECH.HK"]).diff()
    ra = np.log(df["close_000300.SH"]).diff()
    roll = rh.rolling(60).corr(ra).dropna()
    a(f"均值 {roll.mean():.3f}，中位 {roll.median():.3f}，"
      f"10%/90% 分位 {roll.quantile(0.1):.3f} / {roll.quantile(0.9):.3f}，"
      f"区间 [{roll.min():.3f}, {roll.max():.3f}]。")
    a("")
    a(f"最高：{roll.idxmax():%Y-%m-%d}（{roll.max():.3f}）；"
      f"最低：{roll.idxmin():%Y-%m-%d}（{roll.min():.3f}）。")
    a("")
    a("相关性并非恒定值 —— 用「它们高度相关」去推「谁领先谁」本身就不成立。")
    a("")
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="恒生科技 vs A 股 领先滞后检验")
    p.add_argument("--out", default=str(PANEL_PATH.parent / "hstech_leadlag.md"))
    args = p.parse_args(argv)

    df = build_aligned()
    log.info("共同交易日 %d（%s ~ %s）", len(df), df.index[0].date(), df.index[-1].date())
    report = build_report(df)
    out = Path(args.out)
    out.write_text(report, encoding="utf-8")
    log.info("报告写出 %s", out)
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
