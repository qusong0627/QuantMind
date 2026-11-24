"""FF5 因子模型自检脚本：溢价 t / 因子相关 / spanning / 5×5 组合回归。

用法（容器内）：
    docker exec -w /app quantmind python3 /app/backend/scripts/eval/ff_validate.py \
        --start 2025-02-24 --end 2026-11-20 --sort-mode academic --orth-inv

5×5 回归是学术 FF5 的标准稳健性检验（teal0range/FF5 同款）：Size×{BM,OP,Inv}
各 25 个市值加权组合，对 FF5 做时间序列回归；理想情况下绝大多数 α 不显著。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parents[3]
for _p in (str(_ROOT), str(_ROOT / "backend")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backend.scripts.eval.ff_factors import (  # noqa: E402
    build_or_load_factors,
    build_or_load_panel,
    factor_diagnostics,
    size_char_5x5_validation,
)
from backend.shared.quantdb_paths import resolve_quantdb_dir  # noqa: E402

log = logging.getLogger("ff_validate")


def _write_report(
    out_dir: Path, args: argparse.Namespace, diag: dict, v5: dict
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    md = out_dir / f"ff_validate_{stamp}.md"
    js = out_dir / f"ff_validate_{stamp}.json"
    lines = [
        f"# FF5 因子模型自检 — {args.start} ~ {args.end}",
        "",
        f"- 口径：sort={args.sort_mode}、Inv⟂OP={'是' if args.orth_inv else '否'}、rf={args.rf_annual:.0%}",
        f"- 生成：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## 因子溢价",
        "",
        "| 因子 | 月均 | 年化 | t |",
        "|---|---:|---:|---:|",
    ]
    labels = {
        "MKT": "市场",
        "SMB": "市值",
        "HML": "价值(B/M)",
        "RMW": "盈利",
        "CMA": "投资",
        "TURN": "换手",
    }
    for name, stat in (diag.get("premium") or {}).items():
        lines.append(
            f"| {labels.get(name, name)} {name} | {stat['mean_monthly'] * 100:+.2f}% | "
            f"{stat['ann'] * 100:+.1f}% | {stat['t']:.2f} |"
        )
    corr = diag.get("corr") or {}
    cma_rmw = (corr.get("CMA") or {}).get("RMW")
    if cma_rmw is not None:
        lines += ["", f"CMA/RMW 相关 **{cma_rmw:+.2f}**"]
    if diag.get("spanning"):
        lines += [
            "",
            "## 因子互相回归（spanning）",
            "",
            "| 因子 | α 月 | HAC t |",
            "|---|---:|---:|",
        ]
        for name, stat in diag["spanning"].items():
            lines.append(
                f"| {labels.get(name, name)} {name} | {stat['alpha_monthly'] * 100:+.2f}% | {stat['alpha_t']:.2f} |"
            )
    lines += [
        "",
        "## 5×5 Size × 特征 组合回归（α 显著性）",
        "",
        f"组合数 {v5.get('portfolios', 0)}；平均 R² {v5.get('mean_r2') or 0:.2f}；"
        f"|α t|>2 占比 {(v5.get('frac_abs_t_gt2') or 0):.0%}；max|α t| {v5.get('max_abs_t') or 0:.2f}",
        "",
        "| 特征 | 组合数 | 平均 R² | |α t|>2 | max|α t| |",
        "|---|---:|---:|---:|---:|",
    ]
    for char, stat in (v5.get("by_char") or {}).items():
        lines.append(
            f"| {char} | {stat['n']} | {stat['mean_r2']:.2f} | {stat['frac_abs_t_gt2']:.0%} | {stat['max_abs_t']:.2f} |"
        )
    lines += [
        "",
        "判读：因子模型成立时，5×5 组合的 α 应大多不显著（|t|<2）。"
        "若某特征下大面积显著，说明该风格的收益未被 FF5 解释，需加因子或改分组口径。",
    ]
    md.write_text("\n".join(lines), encoding="utf-8")
    js.write_text(
        json.dumps(
            {"args": vars(args), "diagnostics": diag, "validation_5x5": v5},
            ensure_ascii=False,
            indent=2,
            default=float,
        ),
        encoding="utf-8",
    )
    return md


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    ap = argparse.ArgumentParser(
        description="FF5 因子模型自检（溢价/相关/spanning/5×5）"
    )
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--sort-mode", choices=["academic", "monthly"], default="academic")
    ap.add_argument("--orth-inv", action="store_true")
    ap.add_argument("--rf-annual", type=float, default=0.0)
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args(argv)

    factors, _ = build_or_load_factors(
        args.start,
        args.end,
        rf_annual=args.rf_annual,
        sort_mode=args.sort_mode,
        orth_inv=args.orth_inv,
        rebuild=args.rebuild,
    )
    panel = build_or_load_panel(
        args.start,
        args.end,
        sort_mode=args.sort_mode,
        orth_inv=args.orth_inv,
    )
    diag = factor_diagnostics(factors)
    v5 = size_char_5x5_validation(panel, factors)
    out_dir = (
        Path(args.out_dir)
        if args.out_dir
        else resolve_quantdb_dir().parent / "factor_research" / "ff_validation"
    )
    md = _write_report(out_dir, args, diag, v5)
    print(f"报告已生成：{md}")
    print(
        f"5×5：组合 {v5.get('portfolios', 0)}，平均 R² {v5.get('mean_r2') or 0:.2f}，"
        f"|α t|>2 占比 {(v5.get('frac_abs_t_gt2') or 0):.0%}，max|α t| {v5.get('max_abs_t') or 0:.2f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
