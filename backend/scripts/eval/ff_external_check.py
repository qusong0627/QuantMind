"""外部 FF5 因子数据交叉核对（央财 / jh-factors 等）。

同一市场、同一月份区间，把「我们自建的 FF5」与「外部发布的 FF5」逐因子对齐：
相关系数、回归斜率/t、R²、均值差。用于发现口径漂移（分组阈值、市值加权、
正交化、样本池差异），而不是判断谁对谁错——外部数据只做旁证。

外部 CSV 约定（列名大小写不敏感，支持中英文别名）：
    month,MKT,SMB,HML,RMW,CMA[,RF]
    month 支持 2025-01 / 2025-01-31 / 202501
    MKT 为市场因子（若有 Mkt-RF / 市场 等别名也可识别）

用法（容器内）：
    docker exec -w /app quantmind python3 /app/backend/scripts/eval/ff_external_check.py \
        --external /data/factor_research/external/ff5_ccer.csv \
        --start 2025-02-24 --end 2026-11-20 --sort-mode academic --orth-inv
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[3]
for _p in (str(_ROOT), str(_ROOT / "backend")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backend.scripts.eval.ff_attribution import hac_ols  # noqa: E402
from backend.scripts.eval.ff_factors import build_or_load_factors  # noqa: E402
from backend.shared.quantdb_paths import resolve_quantdb_dir  # noqa: E402

log = logging.getLogger("ff_external_check")

FACTORS = ("MKT", "SMB", "HML", "RMW", "CMA")
LABELS = {
    "MKT": "市场",
    "SMB": "市值",
    "HML": "价值(B/M)",
    "RMW": "盈利",
    "CMA": "投资",
}
ALIASES = {
    "MKT": ("mkt", "mkt-rf", "mktrf", "rmrf", "rm-rf", "market", "市场", "市场因子"),
    "SMB": ("smb", "size", "市值", "规模", "规模因子"),
    "HML": ("hml", "bm", "b/m", "value", "价值", "账面市值比"),
    "RMW": ("rmw", "op", "profitability", "盈利", "盈利能力"),
    "CMA": ("cma", "inv", "investment", "投资", "投资因子"),
    "RF": ("rf", "riskfree", "risk-free", "无风险", "无风险利率"),
}


def _norm(col: str) -> str:
    return re.sub(r"[\s_]+", "", str(col).strip().lower())


def parse_month(value: object) -> str | None:
    """任意日期表示 → YYYY-MM；无法解析返回 None。"""
    s = str(value).strip()
    m = re.match(r"^(\d{4})[-/]?(\d{2})", s)
    if m and 1900 <= int(m.group(1)) <= 2100:
        return f"{m.group(1)}-{m.group(2)}"
    try:
        return pd.Timestamp(s).strftime("%Y-%m")
    except Exception:  # noqa: BLE001 — 非法日期行丢弃
        return None


def load_external_factors(path: Path) -> pd.DataFrame:
    """读外部月度因子 CSV → index=YYYY-MM, columns=MKT/SMB/HML/RMW[/RF]。"""
    df = pd.read_csv(path)
    if df.empty:
        raise ValueError(f"外部因子文件为空：{path}")
    lower = {c: _norm(c) for c in df.columns}
    month_col = next(
        (c for c, n in lower.items() if n in ("month", "date", "ym", "月份", "日期")),
        df.columns[0],
    )
    out = pd.DataFrame({"month": df[month_col].map(parse_month)})
    found = []
    for factor, names in ALIASES.items():
        hits = [c for c, n in lower.items() if n in {_norm(x) for x in names}]
        if hits:
            out[factor] = pd.to_numeric(df[hits[0]], errors="coerce")
            found.append(factor)
    missing = [f for f in FACTORS if f not in found]
    if missing:
        raise ValueError(f"外部文件缺少因子列：{missing}（识别到 {found}）")
    out = out.dropna(subset=["month"]).drop_duplicates("month", keep="last")
    return out.set_index("month").sort_index()


def compare(ours: pd.DataFrame, ext: pd.DataFrame) -> dict:
    """逐因子对齐：corr / 回归斜率 t / R² / 均值差 / 月份数。"""
    common = ours.index.intersection(ext.index)
    if len(common) < 6:
        raise ValueError(f"月份交集过少（{len(common)}），无法交叉核对")
    o = ours.loc[common]
    e = ext.loc[common]
    rows = {}
    for f in FACTORS:
        if f not in e.columns:
            continue
        y = o[f].to_numpy(dtype=float)
        x = e[f].to_numpy(dtype=float)
        ok = np.isfinite(y) & np.isfinite(x)
        if ok.sum() < 6:
            continue
        res = hac_ols(y[ok], x[ok][:, None], lags=2)
        rows[f] = {
            "n": int(ok.sum()),
            "corr": float(np.corrcoef(x[ok], y[ok])[0, 1]),
            "slope": float(res["beta"][1]),
            "slope_t": float(res["t"][1]),
            "r2": float(res["r2"]),
            "ours_mean": float(np.mean(y[ok])),
            "ext_mean": float(np.mean(x[ok])),
            "mean_diff": float(np.mean(y[ok]) - np.mean(x[ok])),
        }
    return {
        "months": [common.min(), common.max()],
        "n_months": int(len(common)),
        "factors": rows,
    }


def write_report(out_dir: Path, args: argparse.Namespace, res: dict) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    md_path = out_dir / f"ff_external_check_{stamp}.md"
    js_path = out_dir / f"ff_external_check_{stamp}.json"
    lines = [
        "# FF5 外部数据交叉核对",
        "",
        f"- 外部文件：`{args.external}`",
        f"- 月份：{res['months'][0]} ~ {res['months'][1]}（{res['n_months']} 个月）",
        f"- 我方口径：sort={args.sort_mode}、Inv⟂OP={'是' if args.orth_inv else '否'}",
        f"- 生成：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "| 因子 | 月份数 | 相关 | 回归斜率 | t | R² | 我方均值 | 外部均值 | 均值差 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for f, r in res["factors"].items():
        lines.append(
            f"| {LABELS[f]} {f} | {r['n']} | {r['corr']:+.3f} | {r['slope']:+.3f} | "
            f"{r['slope_t']:+.2f} | {r['r2']:.2f} | {r['ours_mean'] * 100:+.2f}% | "
            f"{r['ext_mean'] * 100:+.2f}% | {r['mean_diff'] * 100:+.2f}% |"
        )
    lines += [
        "",
        "判读：",
        "- corr ≥0.8 且斜率≈1：口径基本一致；corr 低或斜率显著偏离 1：分组/加权/样本池存在系统性差异。",
        "- 外部数据只作旁证；两者不一致时，以「模型归因结论对两种口径是否同向」为准，不挑选更有利的一份。",
    ]
    md_path.write_text("\n".join(lines), encoding="utf-8")
    js_path.write_text(
        json.dumps(
            {"args": vars(args), "result": res},
            ensure_ascii=False,
            indent=2,
            default=float,
        ),
        encoding="utf-8",
    )
    return md_path


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    ap = argparse.ArgumentParser(description="外部 FF5 因子数据交叉核对")
    ap.add_argument("--external", required=True, help="外部月度因子 CSV 路径")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--sort-mode", choices=["monthly", "academic"], default="academic")
    ap.add_argument("--orth-inv", action="store_true")
    ap.add_argument("--rf-annual", type=float, default=0.0)
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args(argv)

    ext = load_external_factors(Path(args.external))
    factors, _ = build_or_load_factors(
        args.start,
        args.end,
        rf_annual=args.rf_annual,
        sort_mode=args.sort_mode,
        orth_inv=args.orth_inv,
    )
    ours = factors.set_index("month")
    res = compare(ours, ext)
    out_dir = (
        Path(args.out_dir)
        if args.out_dir
        else resolve_quantdb_dir().parent / "factor_research" / "ff_external"
    )
    md = write_report(out_dir, args, res)
    print(f"报告已生成：{md}")
    print(
        "  ".join(
            f"{f}: corr {r['corr']:+.2f} slope {r['slope']:+.2f} (t {r['slope_t']:+.1f})"
            for f, r in res["factors"].items()
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
