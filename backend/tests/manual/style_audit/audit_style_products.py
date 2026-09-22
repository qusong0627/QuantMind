#!/usr/bin/env python3
"""风格产物机构级体检：正交性 / 口径断点量化 / 归因稳健性（bootstrap）。

回答三个问题：
1. 12 个纯因子收益彼此是否近似正交（共线会让归因 β 变得不可解释）；
2. 产物每次重建对**旧风格**的读数改了多大 —— 换了产物前后，同一份因子报告的
   归因数字不可比，这个「口径断点」有多大必须量化，不能靠感觉；
3. 归因结论对统计口径有多敏感：重叠样本的朴素 t、NW(H) 修正、非重叠独立样本、
   块 bootstrap 置信区间，四个口径一起摆出来。

运行：docker exec -w /app quantmind python /app/backend/tests/manual/style_audit/audit_style_products.py
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, "/app")

SF_DIR = Path("/data/quantdb/5_technical_derived/style_factors")
FF5 = Path("/data/train_batch_20260922_purged/ff5_attribution.py")
# 产物写到 data/（gitignore），脚本本身入库；可用 STYLE_AUDIT_OUT 覆盖
OUT_DIR = Path(os.environ.get("STYLE_AUDIT_OUT", "/data/style_audit_20260922"))
OUT = OUT_DIR / "audit_style_products.txt"
BOOT_N = 2000
# 种子可换（AUDIT_SEED）：bootstrap 区间是**随机**估计，只报一次等于没报
# —— 换种子重跑看区间端点漂多少，才知道这个区间是不是种子运气。
SEED = int(os.environ.get("AUDIT_SEED", "20260922"))


def load_ff5():
    spec = importlib.util.spec_from_file_location("ff5_attr", FF5)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def corr_matrix(returns: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    return returns[cols].corr()


def main() -> int:
    rep: list[str] = []
    ff5 = load_ff5()
    from backend.services.engine.factor_report.style_model import STYLE_LABELS, STYLE_NAMES

    # returns.parquet 是**宽表**：dt, horizon, n_used, n_universe + 每风格一列
    def load(path: Path, horizon: int = 1) -> pd.DataFrame:
        df = pd.read_parquet(path)
        df = df[df["horizon"] == horizon].copy()
        df["date"] = pd.to_datetime(df["dt"].astype(str), format="%Y%m%d")
        return df.set_index("date").drop(columns=["dt", "horizon"])

    cur = load(SF_DIR / "returns.parquet")
    prev = load(SF_DIR / "returns.parquet.bak_pitfix_prev")
    base10 = load(SF_DIR / "returns.parquet.bak10")
    wide = cur[[c for c in STYLE_NAMES if c in cur.columns]]

    # ── 1. 覆盖（rmw/cma 逐年） ──
    rep.append("═══ 1. 覆盖 ═══")
    for s in ("rmw", "cma"):
        col = cur[s]
        by_year = col[col.notna()].groupby(col[col.notna()].index.year).size()
        rep.append(f"  {s}({STYLE_LABELS.get(s, s)})：{int(col.notna().sum())}/{len(col)} 天"
                   f"，首个出数 {col.first_valid_index().date()}"
                   f"，逐年 {dict(by_year.head(3))}…{dict(by_year.tail(2))}")

    # ── 2. 正交性：12 风格纯因子收益两两相关 ──
    rep.append("\n═══ 2. 纯因子收益两两相关（应近似正交）═══")
    C = corr_matrix(wide, list(STYLE_NAMES))
    off = C.where(~np.eye(len(C), dtype=bool)).abs().stack().sort_values(ascending=False)
    top = off.drop_duplicates().head(8)
    rep.append("  最大 |相关| 的 8 对：")
    for (a, b), v in top.items():
        rep.append(f"    {a:14s} × {b:14s} |ρ|={v:.3f}")
    rep.append(f"  全矩阵 |ρ| 中位 {off.median():.3f}，均值 {off.mean():.3f}，最大 {off.max():.3f}")

    # ── 3. 口径断点量化 ──
    rep.append("\n═══ 3. 口径断点（重建前后同一风格读数变化）═══")
    for tag, old_wide in (("同日双披露修复（12→12）", prev), ("扩到 12 风格（10→12）", base10)):
        rows = []
        for s in STYLE_NAMES:
            if s not in old_wide.columns:
                continue
            a, b = wide[s].align(old_wide[s], join="inner")
            m = a.notna() & b.notna()
            if m.sum() < 30:
                continue
            rho = float(np.corrcoef(a[m], b[m])[0, 1])
            d = float(np.mean(np.abs(a[m] - b[m])))
            scale = float(np.std(b[m]))
            rows.append((s, rho, d, d / scale if scale else float("nan"), int(m.sum())))
        rep.append(f"  ── {tag}")
        rep.append("     " + "  ".join(f"{s}(ρ={r:.3f},|Δ|={d / max(sc, 1e-12):.2f}σ)" for s, r, d, sc, _ in rows))
        worst = max(rows, key=lambda r: r[2])
        rep.append(f"     最大绝对变化：{worst[0]} 平均 |Δ|={worst[2]:.4%}/日（对照该风格日波动 {worst[3]:.2f}σ）")

    # ── 4. 归因稳健性：四个统计口径 ──
    rep.append("\n═══ 4. 排除腿 α 的四个口径 ═══")
    style_daily = wide
    cols = [s for s in STYLE_NAMES if s in style_daily.columns]
    rng = np.random.default_rng(SEED)
    for tag, (rid, H) in ff5.ARMS.items():
        for sp in ff5.SPLITS:
            legs = ff5.load_legs(rid, sp)
            if legs.empty:
                continue
            dates = pd.DatetimeIndex(sorted(set(legs.index) & set(style_daily.index)))
            y = legs.reindex(dates)["excl"].to_numpy(dtype=float)
            fw = ff5.factor_window_sum(style_daily[cols], H).reindex(dates).to_numpy(dtype=float)
            ok = np.isfinite(y) & np.isfinite(fw).all(axis=1)
            yv, Xv = y[ok], fw[ok]
            ann = ff5.TRADING_DAYS / H
            naive = ff5.regress(yv, Xv, cols, ann)                 # 重叠、不修正
            nw = ff5.regress(yv, Xv, cols, ann, nw_lag=H)          # 重叠 + NW(H)
            step = np.arange(0, len(yv), H)
            nonov = ff5.regress(yv[step], Xv[step], cols, ann)     # 非重叠独立样本
            # 块 bootstrap（环形，块长 H）：α 的 95% 区间
            n = len(yv)
            n_blk = int(np.ceil(n / H))
            alphas = np.empty(BOOT_N)
            for b in range(BOOT_N):
                starts = rng.integers(0, n, n_blk)
                idx = np.concatenate([(np.arange(s, s + H) % n) for s in starts])[:n]
                alphas[b] = ff5.regress(yv[idx], Xv[idx], cols, ann)["alpha_annual"]
            lo, hi = np.percentile(alphas, [2.5, 97.5])
            rep.append(f"  {tag} {sp} 毛超额 {yv.mean() * ann:+.1%}/年")
            rep.append(f"    朴素 OLS(重叠)  α={naive['alpha_annual']:+.1%} t={naive['t_alpha']:+.2f}   ← 会把 t 吹大")
            rep.append(f"    NW({H}) HAC       α={nw['alpha_annual']:+.1%} t={nw['coefficients']['alpha']['t_nw']:+.2f}")
            rep.append(f"    非重叠(n={len(step)})  α={nonov['alpha_annual']:+.1%} t={nonov['t_alpha']:+.2f}")
            rep.append(f"    块 bootstrap 95%CI  [{lo:+.1%}, {hi:+.1%}]（{BOOT_N} 次，块长 {H}）")
            # 只用 10 风格（去掉 rmw/cma）的 α：回答「加这两个风格改变了结论吗」
            c10 = [c for c in cols if c not in ("rmw", "cma")]
            j10 = [cols.index(c) for c in c10]
            nw10 = ff5.regress(yv, Xv[:, j10], c10, ann, nw_lag=H)
            rep.append(f"    仅 10 风格(NW)     α={nw10['alpha_annual']:+.1%} t={nw10['coefficients']['alpha']['t_nw']:+.2f}")

    text = "\n".join(rep)
    OUT.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
