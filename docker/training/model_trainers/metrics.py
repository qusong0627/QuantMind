"""训练指标：IC/RankIC/全集 metrics（B4 由 train.py 拆出，无状态纯函数）。"""
from __future__ import annotations
import numpy as np
import pandas as pd

def _ic(pred: np.ndarray, label: np.ndarray) -> float:
    mask = np.isfinite(pred) & np.isfinite(label)
    if mask.sum() < 10:
        return float("nan")
    return float(np.corrcoef(pred[mask], label[mask])[0, 1])

def _rank_ic_series(df: pd.DataFrame, pred_col: str, label_col: str) -> list[float]:
    daily = []
    for _, g in df.groupby("trade_date", sort=False):
        g = g[[pred_col, label_col]].dropna()
        if len(g) < 10:
            continue
        rp = g[pred_col].rank(method="average").to_numpy()
        rl = g[label_col].rank(method="average").to_numpy()
        v = _ic(rp, rl)
        if np.isfinite(v):
            daily.append(v)
    return daily

def _compute_metrics(df: pd.DataFrame, y_true: np.ndarray, y_pred: np.ndarray) -> dict:
    # 剔除 NaN/Inf 对，避免 rmse/auc 传播为 NaN
    y_true = np.asarray(y_true, dtype=np.float64)
    y_pred = np.asarray(y_pred, dtype=np.float64)
    valid = np.isfinite(y_true) & np.isfinite(y_pred)
    if valid.sum() < 10:
        return {"ic": float("nan"), "rank_ic": float("nan"), "rank_icir": float("nan"),
                "rmse": 0.0, "auc": 0.0, "score_direction": "normal"}
    y_true = y_true[valid]
    y_pred = y_pred[valid]
    # df 与 y 长度一致时同步过滤，避免 assign 长度不匹配
    if len(df) == len(valid):
        df = df.iloc[valid].copy()
    ic     = _ic(y_pred, y_true)
    series = _rank_ic_series(df.assign(_pred=y_pred, _label=y_true), "_pred", "_label")
    rank_ic   = float(np.nanmean(series)) if series else float("nan")
    rank_icir = float(np.mean(series) / (np.std(series) + 1e-9)) if series else float("nan")
    rmse = float(np.sqrt(np.mean(np.square(y_pred - y_true)))) if len(y_true) else float("nan")
    labels = (y_true > 0).astype(int)
    pos = int(labels.sum())
    neg = int(len(labels) - pos)
    auc = float("nan")
    if pos > 0 and neg > 0:
        ranks = pd.Series(y_pred).rank(method="average").to_numpy()
        auc = float((ranks[labels == 1].sum() - pos * (pos + 1) / 2.0) / (pos * neg))
    # 方向检测：IC < 0 说明模型预测与标签方向相反
    score_direction = "normal" if np.isnan(ic) or ic >= 0 else "reversed"
    return {"ic": ic, "rank_ic": rank_ic, "rank_icir": rank_icir, "rmse": rmse, "auc": auc, "score_direction": score_direction}


def _daily_rank_ic(df: pd.DataFrame, pred_col: str, ret_col: str) -> pd.Series:
    """每日 RankIC 序列（index=trade_date），供评估报告做时序/年度汇总。"""
    out: dict = {}
    for d, g in df.groupby("trade_date", sort=True):
        s = g[[pred_col, ret_col]].dropna()
        if len(s) < 10:
            continue
        rp = s[pred_col].rank(method="average").to_numpy()
        rl = s[ret_col].rank(method="average").to_numpy()
        v = _ic(rp, rl)
        if np.isfinite(v):
            out[d] = v
    return pd.Series(out, dtype="float64").sort_index()


def _series_stats(series: pd.Series) -> dict:
    """一条日频序列的汇总：均值/标准差/ICIR/胜率/t 值。"""
    s = series.dropna()
    if s.empty:
        return {"mean": None, "std": None, "icir": None, "win_rate": None, "t_stat": None}
    mean = float(s.mean())
    std = float(s.std(ddof=0))
    n = int(len(s))
    return {
        "mean": round(mean, 6),
        "std": round(std, 6),
        "icir": round(mean / std, 4) if std > 1e-12 else None,
        "win_rate": round(float((s > 0).mean()), 4),
        "t_stat": round(mean / (std / np.sqrt(n)), 2) if std > 1e-12 else None,
    }


def _downsample(series: pd.Series, max_points: int) -> list:
    """日频序列抽样为 ≤max_points 个 [date, value] 点（前后端点保留）。"""
    s = series.dropna()
    if s.empty:
        return []
    if len(s) > max_points:
        step = int(np.ceil(len(s) / max_points))
        s = s.iloc[::step]
    return [[str(idx)[:10], round(float(v), 6)] for idx, v in s.items()]


def compute_eval_report(
    pred_df: pd.DataFrame,
    *,
    n_groups: int = 10,
    curve_points: int = 200,
    ann_days: int = 244,
    horizon_days: int = 1,
) -> dict:
    """模型评估报告：从全窗口预测构造「预测强弱」结构化诊断。

    覆盖：全量/分年 RankIC 汇总、每日 RankIC 累计曲线、十分位分层收益与
    单调性、Top−Bottom 多空组合（年化/夏普/最大回撤/净值曲线）、按
    train/valid/test 分段汇总。曲线抽样到 curve_points 个点，JSON 友好。

    收益口径优先用 label_return（真实未来收益）；只有 rank 标签时退化为
    rank 空间分层（结构一致但非真实收益，报告内会标注 return_basis）。
    """
    report: dict = {"version": 1}
    need = {"trade_date", "pred"}
    if not need <= set(pred_df.columns):
        return {**report, "error": f"missing columns: {sorted(need - set(pred_df.columns))}"}
    ret_col = (
        "label_return"
        if "label_return" in pred_df.columns
        else ("label" if "label" in pred_df.columns else None)
    )
    if ret_col is None:
        return {**report, "error": "no label column for evaluation"}
    cols = ["trade_date", "pred", ret_col] + (["split"] if "split" in pred_df.columns else [])
    df = pred_df[cols].dropna(subset=["pred", ret_col]).copy()
    if ret_col == "label_return":
        # 早年前复权损坏（负价/趋零）会产生 ±1000% 级的假收益（实测 4474/768 万行
        # |ret|>50%）：多空净值 cumprod 会炸成 1e34、回撤变乱码。报告口径剔除
        # |ret|>200% 行（0.06%），IC 统计不受影响。
        df = df[df[ret_col].abs() <= 2.0]
    if df.empty:
        return {**report, "error": "empty predictions"}
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    report["return_basis"] = "label_return" if ret_col == "label_return" else "label_rank"
    report["n_rows"] = int(len(df))
    report["n_days"] = int(df["trade_date"].nunique())

    # ① 每日 RankIC 时序 + 全量/分年/分段汇总
    ic_daily = _daily_rank_ic(df, "pred", ret_col)
    report["rank_ic"] = _series_stats(ic_daily)
    report["ic_curve"] = _downsample(ic_daily.cumsum(), curve_points)
    yearly = []
    for year, s in ic_daily.groupby(ic_daily.index.year, sort=True):
        yearly.append({"year": int(year), **_series_stats(s)})
    report["yearly"] = yearly

    # ② 十分位分层收益（按日截面 pred 分位分组）与单调性
    pct = df.groupby("trade_date")["pred"].rank(pct=True)
    grp = np.ceil(pct.to_numpy() * n_groups).clip(1, n_groups).astype(int)
    gm = (
        df.assign(_grp=grp)
        .groupby(["trade_date", "_grp"])[ret_col]
        .mean()
        .unstack()
    )
    group_mean = gm.mean(axis=0)
    report["groups"] = {
        "n_groups": n_groups,
        "mean_returns": [None if pd.isna(v) else round(float(v), 6) for v in group_mean],
        "monotonicity": None,
    }
    try:
        from scipy.stats import spearmanr

        rho, _ = spearmanr(group_mean.index.to_numpy(), group_mean.to_numpy())
        if np.isfinite(rho):
            report["groups"]["monotonicity"] = round(float(rho), 4)
    except Exception:  # noqa: BLE001
        pass

    # ③ Top−Bottom 多空组合
    if n_groups in gm.columns and 1 in gm.columns:
        spread = (gm[n_groups] - gm[1]).dropna()
        # 标签是未来 N 日收益：逐日值相互重叠，直接按日复利 = 把 N 日收益按
        # 244 次/年复利（实测年化 650%、净值 e28）。按持仓周期抽样为非重叠
        # 序列后再统计/复利（年化基准仍按 244 个交易日）。
        h = max(1, int(horizon_days or 1))
        if h > 1:
            spread = spread.iloc[::h]
        # 单日价差缩尾 ±5%（因子报告通行口径）：防个别极端日主导复利。
        spread = spread.clip(-0.05, 0.05)
        stats = _series_stats(spread)
        cum = (1.0 + spread).cumprod() - 1.0
        max_dd = float((cum - cum.cummax()).min()) if not cum.empty else None
        ann_ret = None
        ann_vol = None
        sharpe = None
        if not spread.empty:
            ann_ret = float(spread.mean() * ann_days)
            ann_vol = float(spread.std(ddof=0) * np.sqrt(ann_days))
            sharpe = round(ann_ret / ann_vol, 4) if ann_vol > 1e-12 else None
        report["long_short"] = {
            **stats,
            "ann_return": None if ann_ret is None else round(ann_ret, 6),
            "ann_vol": None if ann_vol is None else round(ann_vol, 6),
            "sharpe": sharpe,
            "max_drawdown": None if max_dd is None else round(max_dd, 6),
            "curve": _downsample(cum, curve_points),
        }

    # ④ 按 train/valid/test 分段汇总（体现样本外衰减）
    if "split" in df.columns:
        by_split = {}
        for name, sub in df.groupby("split", sort=False):
            s = _daily_rank_ic(sub, "pred", ret_col)
            by_split[str(name)] = {**_series_stats(s), "n_days": int(len(s)), "n_rows": int(len(sub))}
        report["by_split"] = by_split

    return report
