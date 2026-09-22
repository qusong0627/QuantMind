#!/usr/bin/env python3
"""同日双披露修复的**影响面**：逐单元格复现「旧排序 vs 新排序」并直接对差。

背景：``build_style_factors.load_balance_pit/load_flow_pit`` 原用 ``np.argsort(ann)``
（默认快速排序、**非稳定**）排 PIT 序列，同日多披露谁在最后是实现定义的。修复后
``pit_order`` 按「主序公告日、次序报告期」取报告期最新的一条。

本脚本不是估上界，而是**精确复现**：对每只股票，用同一份原始行分别按
``np.argsort(ann)``（旧）与 ``pit_order(ann, tt)``（新）对齐到交易日，逐格对差。
所以输出的单元格数 = 历史上真实被改动过的 (股票, 交易日) 格数。

三个源各自的面板口径与 build_style_factors 一致：balance 直接取存量列
（长期负债 = 长期借款 + 应付债券），income/cashflow 先按报告期做 TTM 再 PIT 对齐。

运行：docker exec -w /app quantmind python /app/backend/tests/manual/style_audit/audit_pitfix_impact.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, "/app")

from backend.scripts.build_style_factors import pit_align, pit_order  # noqa: E402
from backend.shared.quantdb_paths import resolve_quantdb_subdir  # noqa: E402

# 产物写到 data/（gitignore），脚本本身入库；可用 STYLE_AUDIT_OUT 覆盖
OUT_DIR = Path(os.environ.get("STYLE_AUDIT_OUT", "/data/style_audit_20260922"))
OUT = OUT_DIR / "audit_pitfix_impact.txt"
START = "2016-01-01"   # 与产物构建同起点
END = "2026-09-18"


def trading_dates() -> np.ndarray:
    val_dir = resolve_quantdb_subdir("5_technical_derived", "valuation")
    ds = sorted(p.name.split("=")[1] for p in val_dir.glob("dt=*") if p.is_dir())
    ds = [d for d in ds if START.replace("-", "") <= d <= END.replace("-", "") ]
    return np.array([int(d) for d in ds], dtype=np.int64)


def align_pair(
    ann: np.ndarray, tt: np.ndarray, val: np.ndarray, date_int: np.ndarray, *, legacy_kind,
) -> tuple[np.ndarray, np.ndarray]:
    """同一份输入分别走旧/新排序 → (旧面板, 新面板)。

    ``legacy_kind`` 必须**逐路还原**各路径修复前的真实排序：balance 原用默认
    （非稳定）``np.argsort``，flow 原用 ``kind="stable"``。统一用非稳定版会把 flow
    的修复效果算大 —— 那是自证。
    """
    o_old = np.argsort(ann, kind=legacy_kind)
    o_new = pit_order(ann, np.where(np.isfinite(tt), tt, 0.0))
    return (pit_align(ann[o_old], val[o_old], date_int),
            pit_align(ann[o_new], val[o_new], date_int))


def scan_source(subdir: str, cols: dict[str, str | list[str]], *, ttm: bool, legacy_kind) -> dict:
    """扫一个源。

    Args:
        cols: 输出键 → 原始列名（列表 = 多列相加，如长期负债 = 长期借款 + 应付债券）。
        ttm: True = 列是单季/累计流量，先转 TTM 再对齐。
        legacy_kind: 该路径修复前的 ``np.argsort`` kind（balance=None、flow="stable"）。
    """
    d = resolve_quantdb_subdir("3_financial_data", subdir)
    files = sorted(d.glob("*.parquet"))
    n_sym_any = 0
    n_cell = 0
    n_cell_finite_pair = 0
    deltas: list[float] = []
    rels: list[float] = []
    per_year: dict[int, int] = {}
    raw_cols = [c for v in cols.values() for c in ([v] if isinstance(v, str) else v)]
    for p in files:
        try:
            df = pd.read_parquet(p, columns=["m_anntime", "m_timetag", *raw_cols])
        except Exception:  # noqa: BLE001 — 单票损坏与本次修复无关
            continue
        ann_s = pd.to_numeric(df["m_anntime"], errors="coerce")
        tt_s = pd.to_numeric(df["m_timetag"], errors="coerce")
        ok = (ann_s.notna() & tt_s.notna()).to_numpy()
        if not ok.any():
            continue
        ann = ann_s.to_numpy(dtype=float)[ok]
        tt_a = tt_s.to_numpy(dtype=float)[ok]
        hit = False
        for src_col in cols.values():
            if isinstance(src_col, list):
                # 多列相加（与生产一致：fillna(0) 再相加 —— 缺一列不等于整行缺失）
                raw = np.zeros(len(df))
                for c in src_col:
                    raw = raw + pd.to_numeric(df[c], errors="coerce").fillna(0.0).to_numpy(dtype=float)
                raw = raw[ok]
            else:
                raw = pd.to_numeric(df[src_col], errors="coerce").to_numpy(dtype=float)[ok]
            if ttm:
                by_period = np.argsort(tt_a, kind="stable")
                val_sorted = ttm_series(tt_a[by_period], raw[by_period], cumulative=src_col == "cash_pay_acq_const_fiolta")
                order = np.argsort(by_period)          # 还原回原行序
                series = val_sorted[order]
            else:
                series = raw
            old, new = align_pair(ann, tt_a, series, DATES, legacy_kind=legacy_kind)
            m = np.isfinite(old) & np.isfinite(new)
            diff = m & (old != new)
            # 一格「从无到有/从有到无」也算改动（读数不同就是不同）
            flip = (np.isfinite(old) ^ np.isfinite(new)) & (np.isfinite(old) | np.isfinite(new))
            n = int(diff.sum() + flip.sum())
            if n:
                hit = True
                n_cell += n
                for i in np.flatnonzero(diff):
                    deltas.append(abs(float(old[i] - new[i])))
                    den = max(abs(float(old[i])), abs(float(new[i])), 1e-12)
                    rels.append(abs(float(old[i] - new[i])) / den)
                    y = int(str(DATES[i])[:4])
                    per_year[y] = per_year.get(y, 0) + 1
                n_cell_finite_pair += int(flip.sum())
        if hit:
            n_sym_any += 1
    return {
        "subdir": subdir, "n_files": len(files), "n_sym_any": n_sym_any,
        "n_cell": n_cell, "n_span_flip": n_cell_finite_pair,
        "median_abs": float(np.median(deltas)) if deltas else 0.0,
        "median_rel": float(np.median(rels)) if rels else 0.0,
        "max_rel": float(np.max(rels)) if rels else 0.0,
        "per_year": per_year,
    }


def ttm_series(tt: np.ndarray, val: np.ndarray, *, cumulative: bool) -> np.ndarray:
    from backend.scripts.build_style_factors import ttm_from_reports
    return ttm_from_reports(tt, val, cumulative=cumulative)


DATES = trading_dates()


def main() -> int:
    rep = ["═══ 同日双披露修复的影响面（逐格精确复现，不是上界）═══",
           f"  交易日网格：{DATES[0]} ~ {DATES[-1]}，共 {len(DATES)} 天"]
    rows = []
    # balance：存量值直接对齐；修复前该路用的是**默认（非稳定）** argsort
    rep.append("\n── balance（存量值，直接对齐；旧 = np.argsort 默认非稳定）")
    bal = scan_source(
        "balance",
        {
            "book_equity": "total_equity",
            "total_assets": "tot_assets",
            "long_term_debt": ["long_term_loans", "bonds_payable"],   # leverage 的分子
        },
        ttm=False, legacy_kind=None,
    )
    rows.append(("balance", bal))
    rep.append("\n── income（单季 → TTM；旧 = kind=\"stable\"）")
    inc = scan_source("income", {"deducted_net_profit_ttm": "deducted_net_profit"},
                      ttm=True, legacy_kind="stable")
    rows.append(("income", inc))
    rep.append("\n── cashflow（累计 → TTM；旧 = kind=\"stable\"）")
    cf = scan_source("cashflow", {"capex_ttm": "cash_pay_acq_const_fiolta"},
                     ttm=True, legacy_kind="stable")
    rows.append(("cashflow", cf))

    rep.append("\n── 汇总")
    tot_cell = 0
    for name, r in rows:
        tot_cell += r["n_cell"]
        rep.append(
            f"   {name:9s} 股票 {r['n_files']} 只，被改动的股票 {r['n_sym_any']} 只，"
            f"改动单元格 {r['n_cell']} 个（(股票,日,面板键) 三元组；其中「有↔无」{r['n_span_flip']} 个）"
        )
        if r["n_cell"]:
            rep.append(f"             绝对差中位 {r['median_abs']:.4g}，相对差中位 {r['median_rel']:.2%}，最大 {r['max_rel']:.1%}")
            ys = sorted(r["per_year"].items())
            rep.append(f"             分年单元格数：{dict(ys)}")
    rep.append(f"\n   三个源合计改动单元格 {tot_cell} 个")

    rep.append(
        "\n读法：\n"
        "  · 「改动单元格」= 旧排序与新排序在**同一交易日**给出不同读数的 (股票, 日) 格数；\n"
        "    旧序按各路径修复前的真实实现还原（balance 非稳定、flow stable），不是统一拿一种近似。\n"
        "  · 同日双披露在 A 股极常见（年报 + 一季报同日）→ **机会面**很大；\n"
        "    但 flow 路径的 stable 排序恰好继承了文件里的报告期序 → 多数格子没被咬；\n"
        "    balance 路径的非稳定排序才是真正不可复现的那一块。\n"
        "  · 产物级影响另见 audit_style_products.txt §3：纯因子收益 ρ=1.000、最大 |Δ|=0.0009%/日。"
    )
    text = "\n".join(rep)
    OUT.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
