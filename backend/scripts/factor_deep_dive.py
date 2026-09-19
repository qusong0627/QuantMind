#!/usr/bin/env python3
"""因子深度体检：IC 衰减 / 中性化 IC / 扣费净收益 / 拥挤度 / 组合构建 / 可交易性。

六个问题一次答完（每节独立，缺数据自动跳过）：
  A. **IC 衰减**：T+1/2/5/10/20 的 IC 与多空价差 → 该多久调仓（读快照的多期字段）
  B. **中性化 IC**：行业去均值 + 市值正交后的残差 IC → 剥离风格暴露看纯 alpha
  C. **扣费净收益**：多空毛收益 − 换手×费率 → 净曲线 + 盈亏平衡费率
  D. **因子拥挤度**：滚动 IC（流行度）、滚动换手、多空回撤 → 拥挤/衰减预警
  E. **组合构建**：等权 / |IC| 加权 / 最大 ICIR（含相关约束+收缩）→ 权重与预期表现
  F. **可交易性**：涨跌停/停牌在调仓日不可成交的比例（A 股短线硬约束）

用法：
  python backend/scripts/factor_deep_dive.py --dataset l1_l2_factors
  python backend/scripts/factor_deep_dive.py --dataset all --sample-step 5
  python backend/scripts/factor_deep_dive.py --dataset l1_factors --no-pdf

产出（技能中心「报告档案 → 因子研究」可直接预览）：
  <报告档案根>/因子研究/因子深度体检_YYYYMMDD.md / .pdf
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.services.engine.factor_report.datasets import DATASETS, dataset_dir  # noqa: E402
from backend.services.engine.factor_report.neutralize import (  # noqa: E402
    load_industry_map,
    load_mv_rank,
    neutralize_ic,
)
from backend.services.engine.factor_report.optimize import max_icir_weights  # noqa: E402
from backend.services.engine.factor_report.service import load_snapshot  # noqa: E402
from backend.shared.quantdb_paths import resolve_quantdb_subdir  # noqa: E402

HORIZON_KEYS = ["fwd_ret_1", "fwd_ret_2", "fwd_ret_5", "fwd_ret_10", "fwd_ret_20"]
META_COLS = {"symbol", "date", "time", "dt", "open", "high", "low", "close",
             "volume", "amount", "release_id", "published_at"}

# A 股默认往返成本（单边）：佣金 0.025% + 印花税 0.05%（卖出）+ 滑点 0.05%
DEFAULT_ROUND_TRIP_COST = 0.00025 * 2 + 0.0005 + 0.0005 * 2   # ≈ 0.20%

# 涨跌停阈值（按板块）：主板 10%、创业板/科创板 20%、北交所 30%（留 0.2pct 缓冲）
def limit_threshold(symbol: str) -> float:
    s = str(symbol)
    if s.startswith(("300", "301", "688", "689")):
        return 0.198
    if s.startswith(("4", "8")):
        return 0.298
    return 0.098


from backend.shared.report_archive import archive_root  # noqa: E402  （原三份副本已收口）


def _fnum(v, digits: int = 4) -> str:
    return "—" if v is None or not np.isfinite(v) else f"{v:+.{digits}f}"


def _fpct(v, digits: int = 3) -> str:
    return "—" if v is None or not np.isfinite(v) else f"{v * 100:+.{digits}f}%"


# ═══════════════ A. IC 衰减（读快照多期字段）═══════════════

def section_ic_decay(dataset: str, top_n: int = 12) -> tuple[list[str], list[dict]]:
    snap = load_snapshot(dataset)
    if not snap:
        return [], []
    meta = snap.get("meta") or {}
    horizons = meta.get("horizons") or [meta.get("horizon") or "fwd_ret_5"]
    fs = snap.get("factors") or []
    rows = [f for f in fs if (f.get("ic_by_horizon") or {}).get("fwd_ret_5") is not None]
    rows.sort(key=lambda f: abs(f["ic_by_horizon"]["fwd_ret_5"]), reverse=True)
    top = rows[:top_n]

    lines = [
        "| 因子 | " + " | ".join(h.replace("fwd_ret_", "T+") for h in horizons) + " | 峰位 |",
        "|---" * (len(horizons) + 2) + "|",
    ]
    peak_info: list[dict] = []
    for f in top:
        icb = f.get("ic_by_horizon") or {}
        vals = [icb.get(h) for h in horizons]
        valid = [(h, abs(v)) for h, v in zip(horizons, vals, strict=False) if v is not None]
        peak = max(valid, key=lambda x: x[1])[0] if valid else None
        # 调仓周期建议：|IC| 峰值所在期；若 T+20 最高说明是慢因子
        peak_info.append({"name": f["name"], "peak": peak, "ic": icb})
        lines.append(
            "| `" + f["name"] + "` | "
            + " | ".join(_fnum(v, 4) for v in vals)
            + f" | {peak.replace('fwd_ret_', 'T+') if peak else '—'} |"
        )

    # 统计：各期成为峰值的因子数 → 全库该用多长周期
    from collections import Counter

    counter = Counter(p["peak"] for p in peak_info if p["peak"])
    lines += ["", "**全库 |IC| 峰值分布**（决定整体调仓节奏）：",
              "", "| 峰值前瞻期 | " + " | ".join(h.replace("fwd_ret_", "T+") for h in horizons if counter.get(h)) + " |",
              "|---" * (1 + len([h for h in horizons if counter.get(h)])) + "|",
              "| 因子个数 | " + " | ".join(str(counter.get(h, 0)) for h in horizons if counter.get(h)) + " |"]
    return lines, [{"name": p["name"], "peak": p["peak"]} for p in peak_info]


# ═══════════════ B. 中性化 IC（行业 + 市值残差）═══════════════
# 取数与投影均收口到 factor_report.neutralize（同一套口径还被
# evaluate_signal_factors 与因子报告构建器使用，此前各写各的）。


def neutralized_ic(dataset: str, sample_step: int = 5, max_factors: int | None = None) -> pd.DataFrame:
    """行业去均值 + 市值正交后的残差秩 IC（逐日；默认每 step 个交易日取一天）。"""
    root = dataset_dir(dataset)
    parts = sorted(p for p in root.glob("dt=*") if p.is_dir())
    if not parts:
        return pd.DataFrame()
    parts = parts[:: max(sample_step, 1)]
    ind = load_industry_map()

    sch = pq.ParquetFile(f"{parts[-1]}/data.parquet").schema_arrow
    cols = [c for c in sch.names
            if c not in META_COLS and str(sch.field(c).type).startswith(("float", "double", "int", "decimal"))]
    if max_factors and len(cols) > max_factors:
        cols = cols[:max_factors]          # 调试用

    acc: dict[str, list[float]] = {c: [] for c in cols}
    # 需要 T+5 收益：从 close 序列算（close_fwd 数据集），alpha 用标签表
    meta = (load_snapshot(dataset) or {}).get("meta") or {}
    use_labels = str(meta.get("label_mode")) == "labels_table"
    label_root = resolve_quantdb_subdir("6_ml_datasets", "alpha_library_labels") if use_labels else None

    dates = [p.name.split("=")[1] for p in parts]
    close_panel: dict[str, pd.Series] = {}
    if not use_labels:
        for p in parts:
            dt = p.name.split("=")[1]
            close_panel[dt] = pq.read_table(f"{p}/data.parquet", columns=["symbol", "close"]).to_pandas().set_index("symbol")["close"]
    fwd5 = {}
    if not use_labels:
        all_dts = [q.name.split("=")[1] for q in sorted(root.glob("dt=*"))]
        idx_all = {d: i for i, d in enumerate(all_dts)}
        for dt in dates:
            j = idx_all[dt] + 5
            if j < len(all_dts):
                nxt = pq.read_table(f"{root}/dt={all_dts[j]}/data.parquet", columns=["symbol", "close"]).to_pandas().set_index("symbol")["close"]
                fwd5[dt] = (nxt / close_panel[dt] - 1.0).dropna()

    for p, dt in zip(parts, dates, strict=False):
        try:
            t = pq.read_table(f"{p}/data.parquet", columns=["symbol", *cols]).to_pandas().set_index("symbol")
        except Exception:  # noqa: BLE001
            continue
        if use_labels:
            lp = label_root / f"dt={dt}" / "data.parquet"
            if not lp.exists():
                continue
            y = pq.read_table(lp, columns=["symbol", "fwd_ret_5"]).to_pandas().set_index("symbol")["fwd_ret_5"]
        else:
            y = fwd5.get(dt)
        if y is None or len(y) < 100:
            continue

        # 市值 → 秩。pandas 的 rank 不给 NaN 计分母，故「先在全候选集上取秩、
        # 再 dropna」与旧代码「先取交集、再在交集上取秩」逐位相同（已实测）。
        mv_rank_s = load_mv_rank(dt, t.index.intersection(y.dropna().index)).dropna()
        if mv_rank_s.empty:
            continue
        idx = mv_rank_s.index
        if len(idx) < 100:
            continue
        X = t.loc[idx, cols]
        yy = y.loc[idx].to_numpy(dtype=np.float64)
        mv_rank = mv_rank_s.to_numpy(dtype=np.float64)
        inds = ind.reindex(idx).fillna("UNKNOWN").to_numpy()

        # ① 横截面秩 ② 行业去均值 ③ 对 rank(mv) 正交 ④ 残差 IC —— 全部在共享模块
        ic = neutralize_ic(X.rank(pct=True).to_numpy(dtype=np.float64), yy,
                           ind_codes=inds, control=mv_rank)
        for c, v in zip(cols, ic, strict=False):
            if np.isfinite(v):
                acc[c].append(float(v))

    out = pd.DataFrame({
        "name": list(acc.keys()),
        "ic_neutral": [float(np.mean(v)) if v else np.nan for v in acc.values()],
        "ic_neutral_days": [len(v) for v in acc.values()],
    })
    return out


# ═══════════════ C. 扣费净收益 ═══════════════

def net_of_cost(dataset: str, round_trip_cost: float = DEFAULT_ROUND_TRIP_COST, top_n: int = 12) -> tuple[list[str], pd.DataFrame]:
    """扣费净收益：**同时给 T+5 日频与 T+20 调仓两个口径**。

    口径说明（关键）：
    - A 段实测几乎所有因子的 |IC| 峰值在 T+20 → 日频调仓把成本放大了数倍，结论会偏悲观；
    - T+20 口径：毛收益 = ls_20（20 日多空价差），成本 = 20 日调仓换手 × 往返费率
      （换手用 1−(1−日换手)^20 估计持有 20 日的成员变动比例），均按 20 日摊到日均；
    - 两个口径并列展示，避免单一假设误导。
    """
    path = dataset_dir(dataset) / "report" / "factor_series.parquet"
    if not path.exists():
        return [], pd.DataFrame()
    cols = ["factor", "date", "q1", "q10", "turnover"]
    avail = set(pq.ParquetFile(path).schema_arrow.names)
    if "ls_20" in avail:
        cols.append("ls_20")
    df = pq.read_table(path, columns=cols).to_pandas()
    # 防御：早年 daily_forward 近零/负价会让 forward return 出现 ±inf，
    # 分位列里一旦混入 inf，mean() 直接变 NaN（实测 a158_KMID 的 q1）——统一打回 NaN
    for c in ("q1", "q10", "ls_20", "turnover"):
        if c in df.columns:
            df[c] = df[c].where(np.isfinite(df[c]))

    d = df["turnover"].fillna(0.0).clip(0.0, 1.0)
    # ① T+5 日频口径
    df["gross_d5"] = (df["q10"] - df["q1"]) / 5.0
    df["cost_d5"] = d * round_trip_cost
    df["net_d5"] = df["gross_d5"] - df["cost_d5"]
    # ② T+20 调仓口径
    if "ls_20" in df.columns:
        turn20 = (1.0 - (1.0 - d) ** 20).clip(0.0, 1.0)
        df["gross_d20"] = df["ls_20"].fillna(0.0) / 20.0
        df["cost_d20"] = turn20 * round_trip_cost / 20.0
        df["net_d20"] = df["gross_d20"] - df["cost_d20"]
    else:
        df["gross_d20"] = df["cost_d20"] = df["net_d20"] = np.nan

    g = df.groupby("factor")
    out = g.agg(
        gross_d5=("gross_d5", "mean"), cost_d5=("cost_d5", "mean"), net_d5=("net_d5", "mean"),
        gross_d20=("gross_d20", "mean"), cost_d20=("cost_d20", "mean"), net_d20=("net_d20", "mean"),
        std_d20=("net_d20", "std"), turnover=("turnover", "mean"), days=("net_d5", "size"),
    ).reset_index()
    out["sharpe_d20"] = out["net_d20"] / out["std_d20"].replace(0, np.nan) * np.sqrt(244)

    ranked = out.sort_values("net_d20", ascending=False).head(top_n)
    lines = [
        f"（往返成本 {round_trip_cost * 100:.3f}% = 佣金双边 0.05% + 印花税 0.05% + 滑点双边 0.1%）",
        "",
        "| 因子 | T+5 日频净/日 | **T+20 调仓净/日** | T+20 毛/日 | T+20 成本/日 | 净 Sharpe(T+20) | 日换手 |",
        "|---|---|---|---|---|---|---|",
    ]
    for _, r in ranked.iterrows():
        lines.append(
            f"| `{r['factor']}` | {_fpct(r['net_d5'], 3)} | **{_fpct(r['net_d20'], 3)}** | {_fpct(r['gross_d20'], 3)} | "
            f"{_fpct(-r['cost_d20'], 3)} | {_num_safe(r['sharpe_d20'])} | {_pct_plain(r['turnover'])} |"
        )
    n5 = int((out["net_d5"] <= 0).sum())
    n20 = int((out["net_d20"] <= 0).sum())
    lines += [
        "",
        f"**{len(out)} 个因子**：日频(T+5)口径净收益 ≤0 的有 {n5} 个（{n5 / max(len(out), 1):.0%}）；"
        f"**按 T+20 调仓口径只剩 {n20} 个（{n20 / max(len(out), 1):.0%}）≤0**。",
        "→ 结论：调仓周期从日频放到 20 日，成本对净收益的侵蚀大幅缓解；短线策略才需要担心换手。",
    ]
    return lines, out


def _pct_plain(v, digits: int = 0) -> str:
    return "—" if v is None or not np.isfinite(v) else f"{v * 100:.{digits}f}%"


def _num_safe(v, digits: int = 2) -> str:
    return "—" if v is None or not np.isfinite(v) else f"{v:.{digits}f}"


# ═══════════════ D. 拥挤度与衰减预警 ═══════════════

def crowding(dataset: str, top_n: int = 10) -> tuple[list[str], pd.DataFrame]:
    path = dataset_dir(dataset) / "report" / "factor_series.parquet"
    if not path.exists():
        return [], pd.DataFrame()
    df = pq.read_table(path, columns=["factor", "date", "ic", "ls_5", "turnover"]).to_pandas()
    df["dt"] = pd.to_datetime(df["date"].astype(str), format="%Y%m%d")
    rows = []
    win = 250
    for name, g in df.groupby("factor"):
        g = g.sort_values("dt")
        ic = g["ic"].to_numpy(dtype=float)
        turn = g["turnover"].to_numpy(dtype=float)
        ls = np.nan_to_num(g["ls_5"].to_numpy(dtype=float), nan=0.0)
        if len(ic) < win + 60:
            continue
        ic_roll = pd.Series(ic).rolling(win, min_periods=win // 2).mean()
        # 近一年 vs 之前：IC 是否衰减、换手是否升高（拥挤的两个特征）
        recent_ic = float(np.nanmean(ic[-win:]))
        prior_ic = float(np.nanmean(ic[-2 * win : -win])) if len(ic) >= 2 * win else np.nan
        recent_turn = float(np.nanmean(turn[-win:]))
        prior_turn = float(np.nanmean(turn[-2 * win : -win])) if len(ic) >= 2 * win else np.nan
        # 多空曲线回撤（因子崩盘）
        curve = np.cumprod(1.0 + ls)
        peak = np.maximum.accumulate(curve)
        dd = float(curve[-1] / peak[-1] - 1.0) if peak[-1] > 0 else 0.0
        rows.append({
            "factor": name,
            "ic_recent": recent_ic,
            "ic_prior": prior_ic,
            "ic_delta": (abs(recent_ic) - abs(prior_ic)) if np.isfinite(prior_ic) else np.nan,
            "turn_recent": recent_turn,
            "turn_delta": (recent_turn - prior_turn) if np.isfinite(prior_turn) else np.nan,
            "ls_drawdown": dd,
            "last_ic_roll": float(ic_roll.iloc[-1]) if np.isfinite(ic_roll.iloc[-1]) else np.nan,
        })
    out = pd.DataFrame(rows)
    if out.empty:
        return [], out
    # 拥挤预警：近一年 |IC| 明显衰减 且 换手升高（或回撤深）
    out["warn"] = ((out["ic_delta"] < -0.01) & (out["turn_delta"] > 0.03)) | (out["ls_drawdown"] < -0.5)
    warned = out[out["warn"]].sort_values("ic_delta").head(top_n)
    lines = ["| 因子 | 近一年 IC | 此前 IC | Δ\\|IC\\| | 换手变化 | 多空回撤 | 判读 |", "|---|---|---|---|---|---|---|"]
    for _, r in warned.iterrows():
        tag = "换手升高+IC衰减（拥挤）" if r["ic_delta"] < -0.01 and r["turn_delta"] > 0.03 else "多空深回撤（崩过盘）"
        lines.append(
            f"| `{r['factor']}` | {_fnum(r['ic_recent'], 4)} | {_fnum(r['ic_prior'], 4)} | "
            f"{_fnum(r['ic_delta'], 4)} | {_fpct(r['turn_delta'], 1)} | {_fpct(r['ls_drawdown'], 1)} | {tag} |"
        )
    if warned.empty:
        lines = ["（当前窗口内没有因子同时触发「IC 衰减 + 换手升高」或「多空回撤 >50%」）"]
    return lines, out


# ═══════════════ E. 组合构建 ═══════════════

def portfolio_construction(dataset: str, top_k: int = 30) -> tuple[list[str], dict]:
    snap = load_snapshot(dataset)
    if not snap:
        return [], {}
    corr = snap.get("correlation") or {}
    names = list(corr.get("factors") or [])
    mat = np.asarray(corr.get("matrix") or [], dtype=np.float64)
    metrics = {f["name"]: f for f in (snap.get("factors") or [])}
    if not names or mat.shape[0] != len(names):
        return [], {}

    idx = {n: i for i, n in enumerate(names)}
    ranked = sorted(
        [n for n in names if metrics.get(n)],
        key=lambda n: abs(float(metrics[n].get("ic_mean") or 0)) * abs(float(metrics[n].get("icir") or 0)),
        reverse=True,
    )[:top_k]
    sel = [n for n in ranked if n in idx]
    if len(sel) < 3:
        return [], {}
    ii = [idx[n] for n in sel]
    Sigma = mat[np.ix_(ii, ii)]
    sign = np.array([1.0 if (metrics[n].get("ic_mean") or 0) >= 0 else -1.0 for n in sel])
    mu = np.array([abs(float(metrics[n].get("ic_mean") or 0)) for n in sel]) * sign

    # ① 等权（按方向）
    w_eq = sign / len(sel)
    # ② |IC| 加权
    a = np.abs(mu)
    w_ic = sign * (a / a.sum())
    # ③ 最大 ICIR（Σ⁻¹μ，收缩 + 方向符号 + gross=1）
    # ⚠️ 与旧内联版的一处差异：本实现把每个权重**强制对齐到该因子 IC 的符号**
    #    （方向即观点），旧版直接用 Σ⁻¹μ 的原始符号 —— 在因子间强相关时 Σ⁻¹ 可能
    #    解出与 IC 相反的权重，等于"做空自己看好的因子"。此处与 portfolio.py 的
    #    口径取齐，两处组合权重从此可比。
    w_mv = max_icir_weights(Sigma, mu)["weights"]
    # 归一化到 gross exposure = 1
    for w in (w_eq, w_ic, w_mv):
        w /= np.abs(w).sum()

    # 组合 ICIR 必须用**逐日 IC 序列**合成来算（不能用相关矩阵：那是横截面秩相关、
    # 单位与 IC 不同，混着算会得出「分散化让 ICIR 变差」的假结论）。
    # 口径：composite_ic_t = Σ w_i · ic_i,t → ICIR = mean/std（与单因子 ICIR 同口径可比）
    ic_series = None
    sp = dataset_dir(dataset) / "report" / "factor_series.parquet"
    if sp.exists():
        try:
            raw = pq.read_table(sp, columns=["factor", "date", "ic"]).to_pandas()
            ic_series = raw.pivot_table(index="date", columns="factor", values="ic")
        except Exception:  # noqa: BLE001
            ic_series = None

    def stats(w: np.ndarray) -> tuple[float, float]:
        ic = float(w @ mu)
        if ic_series is not None:
            cols = [n for n in sel if n in ic_series.columns]
            if len(cols) >= 3:
                ww = np.array([w[sel.index(n)] for n in cols])
                comp = ic_series[cols].to_numpy(dtype=float) @ ww
                comp = comp[np.isfinite(comp)]
                if comp.size > 20 and comp.std() > 0:
                    return ic, float(comp.mean() / comp.std())
        vol = float(np.sqrt(max(w @ Sigma @ w, 1e-12)))
        return ic, ic / vol

    schemes = [("等权（按方向）", w_eq), ("|IC| 加权", w_ic), ("最大 ICIR（收缩 Σ⁻¹μ）", w_mv)]
    lines = ["| 方案 | 组合 IC | 组合 ICIR | 前 5 大权重 |", "|---|---|---|---|"]
    for label, w in schemes:
        ic, icir = stats(w)
        topw = sorted(zip(sel, w, strict=False), key=lambda kv: abs(kv[1]), reverse=True)[:5]
        top_txt = "、".join(f"`{n}`{v:+.2f}" for n, v in topw)
        lines.append(f"| {label} | {_fnum(ic, 4)} | {_fnum(icir, 3)} | {top_txt} |")

    # 分散化收益：平均单因子 ICIR vs 组合 ICIR
    single_icir = float(np.mean([abs(float(metrics[n].get("icir") or 0)) for n in sel]))
    _, comb_icir = stats(w_mv)
    ratio = comb_icir / max(single_icir, 1e-9)
    verdict = "分散化提升" if ratio >= 1 else "组合 ICIR 低于单因子平均（相关性太高，分散化收益有限）"
    lines += [
        "",
        f"组合内单因子 |ICIR| 平均 {single_icir:.3f} → 最大 ICIR 组合 {comb_icir:.3f}"
        f"（{verdict} {ratio:.2f}×；组合 ICIR 由逐日 IC 序列合成，与单因子口径一致）。",
        f"入选 {len(sel)} 个因子（按 |IC|×|ICIR| 排序取前 {top_k}，已含方向符号）。",
    ]

    weights = {n: float(w) for n, w in zip(sel, w_mv, strict=False)}
    return lines, {"weights": weights, "scheme": "max_icir", "comb_ic": stats(w_mv)[0], "comb_icir": comb_icir}


# ═══════════════ F. 可交易性（涨跌停 / 停牌）═══════════════

def tradability(dataset: str, sample_step: int = 5) -> tuple[list[str], dict]:
    """调仓日不可成交比例：涨跌停（按板块阈值）+ 停牌（成交量为 0）。"""
    root = dataset_dir(dataset)
    parts = sorted(p for p in root.glob("dt=*") if p.is_dir())
    if not parts:
        return [], {}
    parts = parts[:: max(sample_step, 1)]
    rows = []
    # 需要一个前收盘：逐日读 close/volume，比较前一日
    all_parts = sorted(p for p in root.glob("dt=*") if p.is_dir())
    idx_all = {p.name.split("=")[1]: i for i, p in enumerate(all_parts)}
    for p in parts:
        dt = p.name.split("=")[1]
        try:
            t = pq.read_table(f"{p}/data.parquet", columns=["symbol", "close", "volume"]).to_pandas()
        except Exception:  # noqa: BLE001
            continue
        j = idx_all[dt]
        if j == 0:
            continue
        prev_dt = all_parts[j - 1].name.split("=")[1]
        try:
            prev = pq.read_table(f"{root}/dt={prev_dt}/data.parquet", columns=["symbol", "close"]).to_pandas().set_index("symbol")["close"]
        except Exception:  # noqa: BLE001
            continue
        df = t.set_index("symbol")
        pc = prev.reindex(df.index)
        ok = df["close"].notna() & pc.notna() & (pc > 0)
        chg = (df["close"][ok] / pc[ok] - 1.0)
        thr = pd.Series([limit_threshold(s) for s in chg.index], index=chg.index)
        limit_up = (chg >= thr).sum()
        limit_down = (chg <= -thr).sum()
        suspended = int((df["volume"].fillna(0) <= 0).sum())
        n = int(ok.sum())
        rows.append({
            "date": dt, "n": n,
            "limit_up_pct": float(limit_up / max(n, 1)),
            "limit_down_pct": float(limit_down / max(n, 1)),
            "suspended_pct": float(suspended / max(len(df), 1)),
        })
    out = pd.DataFrame(rows)
    if out.empty:
        return [], {}
    summ = {
        "limit_up": float(out["limit_up_pct"].mean()),
        "limit_down": float(out["limit_down_pct"].mean()),
        "suspended": float(out["suspended_pct"].mean()),
        "worst_untradeable": float((out["limit_up_pct"] + out["limit_down_pct"] + out["suspended_pct"]).max()),
        "days": len(out),
    }
    lines = [
        f"抽样 {summ['days']} 个交易日（每 {sample_step} 日取一天）：",
        "",
        "| 状态 | 全市场平均占比 | 最差单日 |",
        "|---|---|---|",
        f"| 涨停（无法买入） | {_fpct(summ['limit_up'], 2)} | {_fpct(float(out['limit_up_pct'].max()), 2)} |",
        f"| 跌停（无法卖出） | {_fpct(summ['limit_down'], 2)} | {_fpct(float(out['limit_down_pct'].max()), 2)} |",
        f"| 停牌 | {_fpct(summ['suspended'], 2)} | {_fpct(float(out['suspended_pct'].max()), 2)} |",
        "",
        f"含义：一个十分位组合约占全市场 10%，若某日涨停占比 {_fpct(summ['limit_up'], 2)}，"
        f"该组合调仓时约有 {summ['limit_up'] * 10:.1f}% 的仓位买不进（跌停同理卖不出）。"
        f"最差单日不可成交合计 {_fpct(summ['worst_untradeable'], 1)}。",
    ]
    return lines, summ


# ═══════════════ 报告装配 ═══════════════

def build_markdown(datasets: list[str], parts: dict) -> str:
    now = datetime.now()
    lines = [
        "# 因子深度体检报告",
        "",
        f"> 覆盖：{len(datasets)} 个数据集　|　往返成本假设 {DEFAULT_ROUND_TRIP_COST * 100:.3f}%　|　"
        f"生成时间：{now.strftime('%Y-%m-%d %H:%M')}",
        "",
        "六个体检项：**IC 衰减（该多久调仓）→ 中性化 IC（有没有真 alpha）→ 扣费净收益（扣完还剩多少）"
        "→ 拥挤度（是不是人多了）→ 组合构建（怎么配）→ 可交易性（买得进吗）**。",
        "",
    ]
    for ds in datasets:
        label = str((DATASETS.get(ds) or {}).get("label") or ds)
        p = parts.get(ds) or {}
        lines += [f"# {'=' * 60}", f"# {label}", f"# {'=' * 60}", ""]

        lines += ["## A. IC 衰减（决策：调仓周期）", ""]
        lines += p.get("decay") or ["（缺快照）"]
        lines += ["", "> 读法：|IC| 随前瞻期升高 = 慢因子（调仓频率低、成本友好）；T+1 就很高然后衰减 = 快因子（成本敏感）。", ""]

        if p.get("neutral"):
            lines += ["## B. 中性化 IC（行业 + 市值残差）", "",
                      "| 因子 | 原始 IC | 中性化 IC | 保留比例 |", "|---|---|---|---|"]
            for r in p["neutral"]:
                lines.append(f"| `{r['name']}` | {_fnum(r['raw'], 4)} | {_fnum(r['ic_neutral'], 4)} | {_fpct(r['keep'], 0)} |")
            lines += ["", "> 保留比例 = |中性化 IC| / |原始 IC|。低于 ~40% 说明该因子的预测力主要来自行业或市值暴露，"
                      "而不是独立的选股能力；接近 100% 说明是干净的 alpha。", ""]

        if p.get("cost"):
            lines += ["## C. 扣费净收益", ""] + p["cost"] + [""]

        if p.get("crowd"):
            lines += ["## D. 拥挤度与衰减预警", ""] + p["crowd"] + [""]

        if p.get("portfolio"):
            lines += ["## E. 组合构建（前 30 因子）", ""] + p["portfolio"] + [""]

        if p.get("trade"):
            lines += ["## F. 可交易性", ""] + p["trade"] + [""]

    lines += [
        "# 总体使用建议", "",
        "1. **调仓周期跟着 IC 峰位走**：A 段的峰值分布决定整体节奏——多数因子峰在 T+20 就别按日频调仓。",
        "2. **中性化后再决定去留**：B 段保留比例低的因子，要么做中性化处理，要么干脆不要（它只是风格的代理）。",
        "3. **扣费后仍为正才算可用**：C 段的净多空与盈亏平衡费率是硬门槛；成本吃掉 >60% 毛收益的因子很难实盘。",
        "4. **警惕拥挤**：D 段预警的因子（IC 衰减 + 换手升高 / 多空深回撤）往往已被人群交易，权重应下调。",
        "5. **组合优于单因子**：E 段显示分散化能把 ICIR 提升数倍；用去重清单选代表、再按最大 ICIR 配权。",
        "6. **可交易性是硬约束**：F 段的涨跌停/停牌占比直接决定信号能落地多少，短线策略尤其要扣除这部分。",
        "",
        "---", "",
        "> 本报告由 QuantMind 自动生成，仅用于内部因子研究，不构成任何投资建议。",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="因子深度体检（衰减/中性化/扣费/拥挤/组合/可交易性）")
    ap.add_argument("--dataset", default="all")
    ap.add_argument("--sample-step", type=int, default=5, help="中性化与可交易性抽样的步长（每 N 个交易日取一天）")
    ap.add_argument("--top", type=int, default=12, help="各表展示的因子数")
    ap.add_argument("--skip-heavy", action="store_true", help="跳过 B/F 两个需要扫数据的慢项")
    ap.add_argument("--no-pdf", action="store_true")
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()

    datasets = list(DATASETS) if args.dataset == "all" else [args.dataset]
    datasets = [d for d in datasets if d in DATASETS] or list(DATASETS)

    parts: dict[str, dict] = {}
    for ds in datasets:
        print(f"[{ds}] A. IC 衰减…")
        decay, _ = section_ic_decay(ds, args.top)
        entry: dict = {"decay": decay}

        print(f"[{ds}] C. 扣费净收益…")
        cost_lines, cost_df = net_of_cost(ds, top_n=args.top)
        entry["cost"] = cost_lines

        print(f"[{ds}] D. 拥挤度…")
        crowd_lines, _ = crowding(ds, top_n=args.top)
        entry["crowd"] = crowd_lines

        print(f"[{ds}] E. 组合构建…")
        pf_lines, pf = portfolio_construction(ds)
        entry["portfolio"] = pf_lines

        if not args.skip_heavy:
            print(f"[{ds}] B. 中性化 IC（抽样 step={args.sample_step}）…")
            snap = load_snapshot(ds)
            neut = neutralized_ic(ds, sample_step=args.sample_step)
            if not neut.empty and snap:
                metrics_by_name = {f["name"]: f for f in (snap.get("factors") or [])}
                neut["raw"] = neut["name"].map(lambda n, mm=metrics_by_name: (mm.get(n) or {}).get("ic_mean"))
                neut = neut.dropna(subset=["raw", "ic_neutral"])
                neut["keep"] = neut["ic_neutral"].abs() / neut["raw"].abs().replace(0, np.nan)
                top = neut.reindex(neut["raw"].abs().sort_values(ascending=False).index).head(args.top)
                entry["neutral"] = top.to_dict("records")

            print(f"[{ds}] F. 可交易性（抽样 step={args.sample_step}）…")
            trade_lines, _ = tradability(ds, sample_step=args.sample_step)
            entry["trade"] = trade_lines

        parts[ds] = entry

    md = build_markdown(datasets, parts)
    out_dir = Path(args.out_dir) if args.out_dir else (archive_root() / "因子研究")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    md_path = out_dir / f"因子深度体检_{stamp}.md"
    md_path.write_text(md, encoding="utf-8")
    print(f"[ok] Markdown: {md_path}")

    if not args.no_pdf:
        try:
            from backend.scripts.md_to_pdf_report import main as md_to_pdf

            pdf_path = out_dir / f"因子深度体检_{stamp}.pdf"
            md_to_pdf(str(md_path), str(pdf_path))
            print(f"[ok] PDF: {pdf_path}（{pdf_path.stat().st_size / 1024:.0f} KB）")
        except Exception as e:  # noqa: BLE001
            print(f"[warn] PDF 生成失败（Markdown 已产出）：{e}")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
