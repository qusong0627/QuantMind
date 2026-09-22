#!/usr/bin/env python3
"""暴露面板逐格对差：修复前排序 vs 修复后排序（暴露层，不是只比纯因子收益）。

``audit_pitfix_impact.py`` 量的是**财务面板**被改动的格数；本脚本量的是这些改动
穿到**暴露产物**后还剩多少 —— 标准化流水线（缩尾 → z → 对 size 正交 → 行业去均值）
会摊平单票的小扰动，摊平多少必须实测。

前置：先跑 ``rebuild_old_order.py`` 生成 /tmp/sf_old_order（修复前排序的产物）。

运行：docker exec -w /app quantmind python /app/backend/tests/manual/style_audit/audit_exposure_delta.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, "/app")

from backend.services.engine.factor_report.style_model import STYLE_NAMES  # noqa: E402
from backend.shared.quantdb_paths import resolve_quantdb_subdir  # noqa: E402

OLD = Path("/tmp/sf_old_order/exposures")
NEW = Path(resolve_quantdb_subdir("5_technical_derived", "style_factors")) / "exposures"
# 产物写到 data/（gitignore），脚本本身入库；可用 STYLE_AUDIT_OUT 覆盖
OUT_DIR = Path(os.environ.get("STYLE_AUDIT_OUT", "/data/style_audit_20260922"))
OUT = OUT_DIR / "audit_exposure_delta.txt"
SAMPLE_PER_DAY = 12       # 逐格全量比会读 2604×2 个文件；按日抽样，覆盖足够


def main() -> int:
    rep = ["═══ 暴露面板对差（修复前排序 vs 修复后排序）═══",
           f"  旧产物 {OLD}", f"  新产物 {NEW}"]
    parts = sorted(p.name for p in OLD.glob("dt=*") if p.is_dir())
    if not parts:
        print("旧产物不存在 —— 先跑 rebuild_old_order.py")
        return 1
    parts = parts[::SAMPLE_PER_DAY]
    rep.append(f"  抽样 {len(parts)} 天（每 {SAMPLE_PER_DAY} 天取 1）")

    per_style: dict[str, list[np.ndarray]] = {s: [] for s in STYLE_NAMES}
    n_cell = dict.fromkeys(STYLE_NAMES, 0)
    n_diff = dict.fromkeys(STYLE_NAMES, 0)
    max_abs = dict.fromkeys(STYLE_NAMES, 0.0)
    days_any = 0
    for part in parts:
        a_p, b_p = OLD / part / "data.parquet", NEW / part / "data.parquet"
        if not (a_p.exists() and b_p.exists()):
            continue
        a = pd.read_parquet(a_p).set_index("symbol")
        b = pd.read_parquet(b_p).set_index("symbol")
        common = a.index.intersection(b.index)
        if len(common) == 0:
            continue
        a, b = a.loc[common], b.loc[common]
        hit_day = False
        for s in STYLE_NAMES:
            if s not in a.columns or s not in b.columns:
                continue
            x, y = a[s].to_numpy(float), b[s].to_numpy(float)
            m = np.isfinite(x) & np.isfinite(y)
            n_cell[s] += int(m.sum())
            d = np.abs(x[m] - y[m])
            per_style[s].append(d)
            n_diff[s] += int((d > 1e-6).sum())
            if d.size:
                max_abs[s] = max(max_abs[s], float(d.max()))
            if (d > 1e-6).any():
                hit_day = True
        days_any += int(hit_day)

    rep.append(f"  有差异的天数：{days_any}/{len(parts)}")
    rep.append("\n  风格            比较格数    不同格数   占比    |Δz|中位(全) |Δz|中位(变动格) |Δz|p99(变动格)  最大|Δz|")
    tot_cell = tot_diff = 0
    worst = None
    for s in STYLE_NAMES:
        all_d = np.concatenate(per_style[s]) if per_style[s] else np.array([0.0])
        chg = all_d[all_d > 1e-6]
        cell = n_cell[s] or 1
        tot_cell += n_cell[s]
        tot_diff += n_diff[s]
        p99 = float(np.percentile(chg, 99)) if chg.size else 0.0
        rep.append(
            f"  {s:14s} {n_cell[s]:10d} {n_diff[s]:10d} {n_diff[s] / cell:7.2%} "
            f"{np.median(all_d):12.5f} {(np.median(chg) if chg.size else 0.0):14.5f} "
            f"{p99:13.4f} {max_abs[s]:9.4f}"
        )
        if worst is None or n_diff[s] / cell > worst[1]:
            worst = (s, n_diff[s] / cell, float(np.median(chg)) if chg.size else 0.0, max_abs[s])
    rep.append(f"\n  合计：{tot_diff}/{tot_cell} 格不同（{tot_diff / max(tot_cell, 1):.4%}）")
    if worst:
        rep.append(
            f"  变动面最广的风格：{worst[0]}（{worst[1]:.2%} 格不同，变动格 |Δz| 中位 {worst[2]:.5f}，最大 {worst[3]:.4f}）"
        )
    rep.append(
        "\n读法：暴露是 z 分数（截面单位），|Δz|=0.01 即 1% 个标准差。\n"
        "  · 变动格占比高、但**变动格的中位 |Δz| 很小** → 大部分是标准化流水线的传染\n"
        "    （受影响股票改了截面均值/标准差 → 当天所有股票跟着挪一点点）；\n"
        "  · 真正要盯的是 p99 / 最大列：那是被直接咬到的那批股票，个别到 1.5~2.6σ；\n"
        "  · 对照：纯因子收益实测 ρ=1.000、最大 |Δ|=0.0009%/日（audit_style_products.txt §3）——\n"
        "    即「单票读数会变、截面结论不变」。"
    )
    text = "\n".join(rep)
    OUT.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
