#!/usr/bin/env python3
"""rmw/cma 取数层的 PIT 审计：**独立朴素实现** vs 生产实现 逐格对拍。

被审的是前视偏差这一类**不会报错的错**：PIT 逻辑若差一天、排序键用错、或
「公告前可见」——产物照常出数，只是偷看了未来，报告上完全看不出来。

方法（刻意用另一条代码路径，不复用生产任何函数）：
- 生产：``build_style_factors.load_flow_pit / load_balance_pit``（向量化 + run-length TTM）
- 朴素：本文件内 pandas 版（sort → 显式差分 → rolling(4) + 季度连续性判定 →
  过滤 ``ann ≤ T`` 取最后一条）
两者在同一 (票, 日) 网格上必须**逐格相等**（含 NaN 的位置）。

网格刻意压着重边界：每票采样若干公告日，取其 ±1 天 —— 公告日当天必须已可见、
前一天必须还看不到（对拍要能在差一天时红）。

运行：docker exec -w /app quantmind python /app/backend/tests/manual/style_audit/audit_pit_naive.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, "/app")

from backend.scripts.build_style_factors import (  # noqa: E402
    FLOW_SOURCES,
    load_balance_pit,
    load_flow_pit,
)
from backend.shared.quantdb_paths import resolve_quantdb_subdir  # noqa: E402

N_SYMBOLS = 30
N_ANN_PER_SYMBOL = 8
N_RANDOM_DATES = 6
SEED = 20260922
# 产物写到 data/（gitignore），脚本本身入库；可用 STYLE_AUDIT_OUT 覆盖
OUT_DIR = Path(os.environ.get("STYLE_AUDIT_OUT", "/data/style_audit_20260922"))
OUT = OUT_DIR / "audit_pit_naive.txt"


# ─────────────────────── 朴素实现（独立代码路径） ───────────────────────


def _naive_single(tt: np.ndarray, v: np.ndarray, cumulative: bool) -> np.ndarray:
    """单季值序列：累计序列先差分还原（Q1 为年初重置点，原样取用）。"""
    df = pd.DataFrame({"tt": tt, "v": v})
    df["q"] = (df["tt"] // 100 % 100 - 1) // 3
    df["ord"] = df["tt"] // 10000 * 4 + df["q"]
    df = df.sort_values("tt", kind="stable").reset_index(drop=True)
    if not cumulative:
        return df["v"].to_numpy(dtype=float)
    prev = df["v"].shift(1)
    adjacent = (df["ord"] - df["ord"].shift(1)) == 1
    return np.where(
        df["q"].to_numpy() == 0,
        df["v"].to_numpy(dtype=float),
        np.where(adjacent.to_numpy(), df["v"].to_numpy(dtype=float) - prev.to_numpy(dtype=float), np.nan),
    )


def naive_flow_pit(sym: str, col: str, cumulative: bool, date_ints: np.ndarray) -> np.ndarray:
    src = resolve_quantdb_subdir("3_financial_data", FLOW_SOURCES_INV[(col, cumulative)])
    path = src / f"{sym}.parquet"
    out = np.full(date_ints.shape, np.nan)
    if not path.exists():
        return out
    raw = pd.read_parquet(path, columns=["m_anntime", "m_timetag", col])
    ann = pd.to_numeric(raw["m_anntime"], errors="coerce").to_numpy()
    tt = pd.to_numeric(raw["m_timetag"], errors="coerce").to_numpy()
    v = pd.to_numeric(raw[col], errors="coerce").to_numpy(dtype=float)
    ok = np.isfinite(ann) & np.isfinite(tt)
    if not ok.any():
        return out
    single = _naive_single(tt[ok].astype(np.int64), v[ok], cumulative)
    s = pd.Series(single)
    roll = s.rolling(4, min_periods=4).sum()
    ords = pd.Series(((tt[ok].astype(np.int64) // 100 % 100 - 1) // 3) + (tt[ok].astype(np.int64) // 10000 * 4))
    ords = ords.iloc[np.argsort(tt[ok], kind="stable")].reset_index(drop=True)
    consecutive = (ords - ords.shift(3)) == 3
    complete = s.notna() & s.shift(1).notna() & s.shift(2).notna() & s.shift(3).notna()
    ttm = pd.Series(np.where(consecutive.to_numpy() & complete.to_numpy(), roll.to_numpy(), np.nan))
    rep = (
        pd.DataFrame({"ann": ann[ok], "tt": tt[ok], "val": ttm.to_numpy()})
        .dropna(subset=["ann"])
        .sort_values(["ann", "tt"], kind="stable")
    )
    if rep.empty:
        return out
    ann_s = rep["ann"].to_numpy(dtype=float)
    val_s = rep["val"].to_numpy(dtype=float)
    pos = np.searchsorted(ann_s, date_ints.astype(float), side="right") - 1
    hit = pos >= 0
    out[hit] = val_s[pos[hit]]
    return out


def naive_balance_pit(sym: str, col: str, date_ints: np.ndarray) -> np.ndarray:
    src = resolve_quantdb_subdir("3_financial_data", "balance")
    path = src / f"{sym}.parquet"
    out = np.full(date_ints.shape, np.nan)
    if not path.exists():
        return out
    raw = pd.read_parquet(path, columns=["m_anntime", "m_timetag", col])
    ann = pd.to_numeric(raw["m_anntime"], errors="coerce").to_numpy()
    tt = pd.to_numeric(raw["m_timetag"], errors="coerce").to_numpy()
    v = pd.to_numeric(raw[col], errors="coerce").to_numpy(dtype=float)
    ok = np.isfinite(ann)
    if not ok.any():
        return out
    rep = (
        pd.DataFrame({"ann": ann[ok], "tt": tt[ok], "val": v[ok]})
        .sort_values(["ann", "tt"], kind="stable")
    )
    ann_s = rep["ann"].to_numpy(dtype=float)
    val_s = rep["val"].to_numpy(dtype=float)
    pos = np.searchsorted(ann_s, date_ints.astype(float), side="right") - 1
    hit = pos >= 0
    out[hit] = val_s[pos[hit]]
    return out


FLOW_SOURCES_INV = {("deducted_net_profit", False): "income", ("cash_pay_acq_const_fiolta", True): "cashflow"}


# ─────────────────────── 对拍 ───────────────────────


def compare(name: str, prod: np.ndarray, naive: np.ndarray, cells: list[tuple[str, int]], report: list[str]) -> int:
    """逐格对拍：NaN 位置必须一致，数值 rtol=1e-9。返回不一致格数。"""
    bad = 0
    nan_mask_mismatch = 0
    for (sym, dt), p, n in zip(cells, prod, naive, strict=True):
        if not np.isfinite(p) and not np.isfinite(n):
            continue
        if np.isfinite(p) != np.isfinite(n):
            nan_mask_mismatch += 1
            bad += 1
            if bad <= 6:
                report.append(f"  [NaN 掩码不一致] {name} {sym} {dt}: 生产={p!r} 朴素={n!r}")
            continue
        if not np.isclose(p, n, rtol=1e-9, atol=1e-6):
            bad += 1
            if bad <= 6:
                report.append(f"  [数值不一致] {name} {sym} {dt}: 生产={p!r} 朴素={n!r} 相对差={abs(p - n) / max(abs(p), 1e-12):.2e}")
    report.append(f"  {name:26s} 格数 {len(cells):5d}  不一致 {bad:3d}（其中 NaN 掩码 {nan_mask_mismatch}）")
    return bad


def main() -> int:
    rng = np.random.default_rng(SEED)
    report: list[str] = []

    # ── 采样：随机票 + 每票压公告日边界 ──
    inc_dir = resolve_quantdb_subdir("3_financial_data", "income")
    files = sorted(p.stem for p in inc_dir.glob("*.parquet"))
    symbols = sorted(rng.choice(files, size=min(N_SYMBOLS, len(files)), replace=False).tolist())
    report.append(f"采样 {len(symbols)} 只（种子 {SEED}）")

    cells: list[tuple[str, int]] = []
    for sym in symbols:
        raw = pd.read_parquet(inc_dir / f"{sym}.parquet", columns=["m_anntime"])
        ann = pd.to_numeric(raw["m_anntime"], errors="coerce").dropna().astype(np.int64)
        ann = ann[(ann >= 20160101) & (ann <= 20260921)]
        if ann.empty:
            continue
        pick = ann.iloc[np.linspace(0, len(ann) - 1, min(N_ANN_PER_SYMBOL, len(ann))).astype(int)]
        dates = set()
        for a in pick:                                       # 边界三连：公告日当天 + 前后一天
            ts = pd.Timestamp(str(int(a)))
            for delta in (-1, 0, 1):
                dates.add(int((ts + pd.Timedelta(days=delta)).strftime("%Y%m%d")))
        for _ in range(N_RANDOM_DATES):
            dates.add(int(rng.integers(20170101, 20260921)))
        for d in sorted(dates):
            if 20160101 <= d <= 20260921:
                cells.append((sym, d))
    report.append(f"对拍网格：{len(cells)} 个 (票, 日) 格（公告日 ±1 天占多数）")

    # ── 生产实现：一次跑全部采样票 ──
    sym_arr = np.array(sorted({s for s, _ in cells}))
    date_list = sorted({str(d) for _, d in cells})
    prod_flow = {k: load_flow_pit(date_list, sym_arr, k) for k in FLOW_SOURCES}
    prod_bal = load_balance_pit(date_list, sym_arr)
    idx_sym = {s: i for i, s in enumerate(sym_arr)}
    idx_dt = {d: i for i, d in enumerate(date_list)}
    # (票序号, 日序号)；生产面板是 (T, N) = (日, 票)，索引时反过来
    jobs = [(idx_sym[s], idx_dt[str(d)]) for s, d in cells]

    # 朴素实现按票成批算（每票只读一次盘），再摊回 (票, 日) 网格
    by_sym: dict[str, list[tuple[int, int]]] = {}
    for n, (s, d) in enumerate(cells):
        by_sym.setdefault(s, []).append((int(d), n))
    naive_flow: dict[str, np.ndarray] = {}
    naive_bal: dict[str, np.ndarray] = {}
    for key, (_subdir, col, cumulative) in FLOW_SOURCES.items():
        arr = np.full(len(cells), np.nan)
        for s, pairs in by_sym.items():
            vals = naive_flow_pit(s, col, cumulative, np.array([d for d, _ in pairs], dtype=np.int64))
            for (_d, n), v in zip(pairs, vals, strict=True):
                arr[n] = v
        naive_flow[key] = arr
    for key, col in (("book_equity", "total_equity"), ("total_assets", "tot_assets")):
        arr = np.full(len(cells), np.nan)
        for s, pairs in by_sym.items():
            vals = naive_balance_pit(s, col, np.array([d for d, _ in pairs], dtype=np.int64))
            for (_d, n), v in zip(pairs, vals, strict=True):
                arr[n] = v
        naive_bal[key] = arr

    report.append("\n── 逐格对拍（生产 vs 朴素独立实现）──")
    total_bad = 0
    for key, (subdir, col, _cum) in FLOW_SOURCES.items():
        prod = np.array([prod_flow[key][j, i] for i, j in jobs])
        total_bad += compare(f"{subdir}.{col}", prod, naive_flow[key], cells, report)
    for key, col in (("book_equity", "total_equity"), ("total_assets", "tot_assets")):
        prod = np.array([prod_bal[key][j, i] for i, j in jobs])
        total_bad += compare(f"balance.{col}", prod, naive_bal[key], cells, report)

    # ── 手工金样（报告里留可人工复核的算例）──
    report.append("\n── 手工金样（前 3 格，人工可复核）──")
    for s, d in cells[:3]:
        i, j = idx_sym[s], idx_dt[str(d)]
        rep = (f"  {s} @ {d}: 扣非TTM={prod_flow['deducted_net_profit_ttm'][j, i]:,.0f} "
               f"capexTTM={prod_flow['capex_ttm'][j, i]:,.0f} 净资产={prod_bal['book_equity'][j, i]:,.0f} "
               f"总资产={prod_bal['total_assets'][j, i]:,.0f}")
        if np.isfinite(prod_bal["book_equity"][j, i]) and prod_bal["book_equity"][j, i] > 0:
            rep += f" → rmw={prod_flow['deducted_net_profit_ttm'][j, i] / prod_bal['book_equity'][j, i]:.6f}"
        if np.isfinite(prod_bal["total_assets"][j, i]) and prod_bal["total_assets"][j, i] > 0:
            rep += f" cma={-prod_flow['capex_ttm'][j, i] / prod_bal['total_assets'][j, i]:.6f}"
        report.append(rep)

    verdict = "PASS —— 两条独立实现逐格一致，未见前视泄漏迹象" if total_bad == 0 else f"FAIL —— {total_bad} 格不一致，须查"
    report.append(f"\n结论：{verdict}")
    text = "\n".join(report)
    OUT.write_text(text, encoding="utf-8")
    print(text)
    return 1 if total_bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
