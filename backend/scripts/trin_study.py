#!/usr/bin/env python3
"""TRIN / 市场广度 → 大盘前瞻研究。

用法::

    python -m backend.scripts.trin_study            # 全量报告
    python -m backend.scripts.trin_study --current  # 只看当前读数

纪律（与项目军规一致）：
- 信号一律用**扩张窗口分位**，不使用全样本分位（会泄漏未来分布）。
- 走查用扩张窗口 + 年度重拟合，训练样本截止到「预测起点 − 预测窗口」，
  避免用还没走完的收益去训练。
- 重叠样本的显著性用 ``overlap_deflated_t``（保守下界），不报朴素 t 值。
- 负结果照报：分项 IC 弱就是弱，不拿个别分桶挑好的说。
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
    overlap_deflated_t,
    spearman_ic,
)

log = logging.getLogger("trin_study")

PANEL_PATH = Path(
    "data/quantdb/5_technical_derived/market_breadth/trin_daily.parquet"
)
TARGETS = {"000300.SH": "沪深300", "000852.SH": "中证1000", "000001.SH": "上证指数"}
HORIZONS = (1, 5, 10, 20, 40)


@dataclass(frozen=True)
class Feature:
    name: str
    column: str
    note: str


#: 预注册信号集 —— 先定后测，避免「试到显著为止」
FEATURES: tuple[Feature, ...] = (
    Feature("breadth", "pct_above_ma20", "站上 MA20 的个股占比"),
    Feature("trin", "trin_ema10", "TRIN 10 日 EMA（成交额版）"),
    Feature("trin_raw", "trin_amt", "TRIN 当日原始值"),
    Feature("udr", "udr_amt", "上涨/下跌成交额比"),
    Feature("adr", "adr", "上涨/下跌家数比"),
    Feature("mcc", "mcclellan", "McClellan 摆动"),
    Feature("limit_net", "_limit_net", "涨跌停净家数占比"),
    Feature("amt_shock", "_amt_shock", "全市场成交额 20 日 z 值"),
)


def load_panel(path: Path = PANEL_PATH) -> pd.DataFrame:
    df = pd.read_parquet(path).set_index("date").sort_index()
    df["_limit_net"] = (df["n_limit_up"] - df["n_limit_down"]) / df["n_total"]
    amt = df["amt_total"]
    df["_amt_shock"] = (amt - amt.rolling(20).mean()) / amt.rolling(20).std()
    return df


def build_features(df: pd.DataFrame, min_periods: int = 250) -> pd.DataFrame:
    """把原始列转成扩张分位（0~1）。绝对值受市场扩容影响，分位才可比。"""
    out = pd.DataFrame(index=df.index)
    for f in FEATURES:
        out[f.name] = expanding_pct_rank(df[f.column], min_periods=min_periods)
    return out


# --------------------------------------------------------------------------
# 报告排版
# --------------------------------------------------------------------------
def _md_table(df: pd.DataFrame, index: bool = True) -> str:
    """极简 markdown 表格（不依赖 tabulate）。"""
    d = df.reset_index() if index else df.reset_index(drop=True)
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


# --------------------------------------------------------------------------
# 描述性：IC 与条件分布
# --------------------------------------------------------------------------
def ic_table(
    feats: pd.DataFrame, df: pd.DataFrame, target: str
) -> pd.DataFrame:
    rows = []
    for f in FEATURES:
        row = {"signal": f.name}
        for n in HORIZONS:
            y = forward_return(df[f"close_{target}"], n)
            ic, cnt = spearman_ic(feats[f.name], y)
            row[f"IC{n}"] = ic
            row[f"t{n}"] = overlap_deflated_t(ic, cnt, n)
        rows.append(row)
    return pd.DataFrame(rows).set_index("signal")


def conditional_table(
    feats: pd.DataFrame, df: pd.DataFrame, target: str, n: int, signal: str
) -> pd.DataFrame:
    y = forward_return(df[f"close_{target}"], n)
    d = pd.concat([feats[signal].rename("s"), y.rename("y")], axis=1).dropna()
    d["bucket"] = pd.cut(
        d["s"], [-0.001, 0.1, 0.25, 0.5, 0.75, 0.9, 1.001],
        labels=["0-10", "10-25", "25-50", "50-75", "75-90", "90-100"],
    )
    g = d.groupby("bucket", observed=True)["y"]
    out = pd.DataFrame(
        {
            "n": g.count(),
            "mean_%": g.mean().round(2),
            "median_%": g.median().round(2),
            "win_rate": g.apply(lambda s: round(float((s > 0).mean()), 2)),
            "std_%": g.std().round(2),
        }
    )
    return out


# --------------------------------------------------------------------------
# 走查（OOS）
# --------------------------------------------------------------------------
def _design(feat_block: np.ndarray) -> np.ndarray:
    """每个特征加平方项 —— 广度信号是 U 形，纯线性会漏掉两端。"""
    cols = [np.ones(len(feat_block))]
    for j in range(feat_block.shape[1]):
        c = feat_block[:, j]
        cols.extend([c, (c - 0.5) ** 2])
    return np.column_stack(cols)


def _ridge_fit(X: np.ndarray, y: np.ndarray, alpha: float = 1e-3) -> np.ndarray:
    """岭回归（截距不惩罚）。用增广最小二乘而不是正规方程求逆 —— 后者在
    特征高度相关时会直接抛 ``LinAlgError: Singular matrix``。"""
    n_feat = X.shape[1]
    reg = np.sqrt(alpha) * np.eye(n_feat)
    reg[0, 0] = 0.0
    A = np.vstack([X, reg])
    b = np.concatenate([y, np.zeros(n_feat)])
    return np.linalg.lstsq(A, b, rcond=None)[0]


def walk_forward(
    feats: pd.DataFrame,
    df: pd.DataFrame,
    target: str,
    horizon: int,
    signal_names: tuple[str, ...],
    min_train: int = 756,
    refit_every: int = 250,
) -> pd.Series:
    """扩张窗口走查：每 ``refit_every`` 个交易日重拟合一次，只用已知收益训练。"""
    y = forward_return(df[f"close_{target}"], horizon).to_numpy()
    X_raw = feats[list(signal_names)].to_numpy()
    n = len(df)
    pred = np.full(n, np.nan)
    start = min_train
    while start < n:
        end = min(start + refit_every, n)
        train_ok = np.isfinite(y[: max(start - horizon, 0)]) & np.isfinite(
            X_raw[: max(start - horizon, 0)]
        ).all(axis=1)
        if train_ok.sum() < 200:
            start = end
            continue
        X_tr = _design(X_raw[: max(start - horizon, 0)][train_ok])
        beta = _ridge_fit(X_tr, y[: max(start - horizon, 0)][train_ok])
        block = X_raw[start:end]
        ok = np.isfinite(block).all(axis=1)
        pred[start:end][ok] = _design(block[ok]) @ beta
        start = end
    return pd.Series(pred, index=df.index)


def oos_metrics(
    pred: pd.Series, df: pd.DataFrame, target: str, horizon: int
) -> dict:
    y = forward_return(df[f"close_{target}"], horizon)
    d = pd.concat([pred.rename("p"), y.rename("y")], axis=1).dropna()
    ic, cnt = spearman_ic(d["p"], d["y"])
    # 非重叠方向胜率：每隔 horizon 取一个，消除重叠样本
    every = d.iloc[::horizon]
    hit = float((np.sign(every["p"]) == np.sign(every["y"])).mean())
    return {
        "n": len(d),
        "IC": round(ic, 4),
        "t_deflated": round(overlap_deflated_t(ic, cnt, horizon), 2),
        "hit_nonoverlap": round(hit, 3),
        "n_nonoverlap": len(every),
    }


def long_flat_metrics(
    pred: pd.Series,
    df: pd.DataFrame,
    target: str,
    horizon: int,
    cost_per_switch: float = 0.0005,
) -> dict:
    """预测为正持有多头，否则空仓。

    成交假设：T 日收盘算出信号、按 T 日收盘价成交（略偏乐观，故切换成本取
    5bp 的保守值）。只统计走查起点之后的表现 —— 前面那段模型还没开始预测，
    拿现金段跟买入持有比没有意义。
    """
    first = pred.first_valid_index()
    if first is None:
        return {}
    sub = df.loc[first:]
    p = pred.loc[first:]
    ret_next = sub[f"close_{target}"].pct_change().shift(-1).fillna(0.0)
    pos = (p > 0).astype(float)
    switch = pos.diff().abs().fillna(pos.abs())
    strat = pos * ret_next - switch * cost_per_switch
    bench = ret_next
    years = len(sub) / 244.0
    ann = (1 + strat).prod() ** (1 / years) - 1
    ann_b = (1 + bench).prod() ** (1 / years) - 1
    vol = strat.std() * np.sqrt(244)
    sharpe = ann / vol if vol > 0 else np.nan
    bench_vol = bench.std() * np.sqrt(244)
    bench_sharpe = ann_b / bench_vol if bench_vol > 0 else np.nan
    dd = (1 + strat).cumprod() / (1 + strat).cumprod().cummax() - 1
    expo = pos.mean()
    return {
        "ann_%": round(ann * 100, 2),
        "bench_ann_%": round(ann_b * 100, 2),
        "vol_%": round(vol * 100, 2),
        "sharpe": round(sharpe, 2),
        "bench_sharpe": round(bench_sharpe, 2),
        "maxdd_%": round(dd.min() * 100, 2),
        "exposure": round(expo, 2),
        "ann_per_exposure_%": round(ann * 100 / expo, 2) if expo > 0 else np.nan,
        "switch/yr": round(float(switch.sum()) / years, 1),
    }


def forecast_table(
    df: pd.DataFrame, feats: pd.DataFrame, target: str, signal: str = "breadth"
) -> pd.DataFrame:
    """当前分位落在历史上哪个桶、该桶此后的收益与波动分布。

    这是「历史条件基准」，不是模型预测：样本重叠、且极端桶高度集中于少数
    危机窗口，读者必须自己打折。
    """
    rows = []
    cur_pct = feats[signal].iloc[-1]
    for n in (5, 20, 40):
        y = forward_return(df[f"close_{target}"], n)
        d = pd.concat([feats[signal].rename("s"), y.rename("y")], axis=1).dropna()
        d = d[d["s"] <= cur_pct + 0.10]
        d = d[d["s"] >= cur_pct - 0.10]
        if len(d) < 20:
            continue
        rows.append(
            {
                "horizon": n,
                "n": len(d),
                "mean_%": round(float(d["y"].mean()), 2),
                "median_%": round(float(d["y"].median()), 2),
                "win_rate": round(float((d["y"] > 0).mean()), 2),
                "p10_%": round(float(d["y"].quantile(0.10)), 2),
                "p90_%": round(float(d["y"].quantile(0.90)), 2),
                "std_%": round(float(d["y"].std()), 2),
            }
        )
    out = pd.DataFrame(rows)
    out.attrs["signal_pct"] = float(cur_pct)
    return out


# --------------------------------------------------------------------------
# 当前读数
# --------------------------------------------------------------------------
def current_reading(df: pd.DataFrame, feats: pd.DataFrame) -> pd.DataFrame:
    latest = df.index[-1]
    rows = []
    for f in FEATURES:
        v = df[f.column].iloc[-1]
        p = feats[f.name].iloc[-1]
        rows.append(
            {
                "signal": f.name,
                "value": round(float(v), 4) if np.isfinite(v) else np.nan,
                "pct_since_2016": round(float(p), 3) if np.isfinite(p) else np.nan,
                "note": f.note,
            }
        )
    out = pd.DataFrame(rows).set_index("signal")
    out.attrs["date"] = latest
    return out


def build_report(df: pd.DataFrame, feats: pd.DataFrame) -> str:
    lines: list[str] = []
    a = lines.append
    a("# TRIN / 市场广度 → 大盘前瞻研究")
    a("")
    a(f"数据区间：{df.index[0]:%Y-%m-%d} ~ {df.index[-1]:%Y-%m-%d}，"
      f"{len(df)} 个交易日。数据源：QuantDB 本地 parquet。")
    a("")

    a("## 一、当前读数")
    a("")
    cur = current_reading(df, feats)
    a(_md_table(cur))
    a("")

    for target, tname in TARGETS.items():
        if f"close_{target}" not in df:
            continue
        a(f"## 二、{tname}（{target}）分项 IC")
        a("")
        ic = ic_table(feats, df, target)
        a(_md_table(ic.round(3)))
        a("")
        a("括号内为重叠样本折算后的保守 t 值；|t| < 2 一律视为不显著。")
        a("")
        for sig in ("breadth", "trin"):
            for n in (5, 20):
                a(f"### {tname} · {sig} · 未来 {n} 日")
                a("")
                a(_md_table(conditional_table(feats, df, target, n, sig)))
                a("")

    a("## 三、走查（OOS）")
    a("")
    rows = []
    for target, tname in TARGETS.items():
        if f"close_{target}" not in df:
            continue
        for sig in ("breadth", "trin", "udr", "mcc"):
            for n in (5, 20):
                pred = walk_forward(feats, df, target, n, (sig,))
                m = oos_metrics(pred, df, target, n)
                m.update({"target": tname, "signal": sig, "horizon": n})
                m.update(long_flat_metrics(pred, df, target, n))
                rows.append(m)
    oos = pd.DataFrame(rows)[
        ["target", "signal", "horizon", "n", "IC", "t_deflated",
         "hit_nonoverlap", "ann_%", "bench_ann_%", "sharpe", "bench_sharpe",
         "maxdd_%", "exposure", "ann_per_exposure_%", "switch/yr"]
    ]
    a(_md_table(oos, index=False))
    a("")
    a("`ann_per_exposure` = 年化 / 平均仓位，用来消除「长期空仓」造成的低收益假象；"
      "与 `bench_sharpe` 对比才是有意义的胜负判据。")
    a("")

    a("## 四、当前状态的历史条件基准（不是模型预测）")
    a("")
    for target, tname in TARGETS.items():
        if f"close_{target}" not in df:
            continue
        for sig in ("breadth", "trin"):
            ft = forecast_table(df, feats, target, sig)
            if ft.empty:
                continue
            a(f"**{tname} · {sig}**：当前分位 {ft.attrs['signal_pct']:.2f}，"
              f"取历史上分位落在 ±0.10 内的样本：")
            a("")
            a(_md_table(ft, index=False))
            a("")
    return "\n".join(lines)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="TRIN/广度前瞻研究")
    p.add_argument("--panel", default=str(PANEL_PATH))
    p.add_argument("--out", default=None, help="报告输出路径（默认与面板同目录）")
    p.add_argument("--current", action="store_true", help="只打印当前读数")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)
    df = load_panel(Path(args.panel))
    feats = build_features(df)

    if args.current:
        cur = current_reading(df, feats)
        print(f"最新交易日：{df.index[-1]:%Y-%m-%d}")
        print(cur.to_string())
        return 0

    report = build_report(df, feats)
    out = Path(args.out) if args.out else Path(args.panel).parent / "trin_study.md"
    out.write_text(report, encoding="utf-8")
    log.info("报告写出 %s", out)
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
