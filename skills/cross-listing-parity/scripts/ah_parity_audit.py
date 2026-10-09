#!/usr/bin/env python3
"""A/H 交叉上市平价与溢价审计 — 离线确定性引擎 + QuantDB 本地装配。

来源：quantskills/skill-cross-listing-parity（GPL-3.0-only）。方法论与报告契约保留，
数据层由 PandaData 改为本地 QuantDB 直读（2026-10-07 标定）：

  溢价 = A收盘(CNY) / (H收盘(HKD) × fx_hkd_cny) − 1        （ratio=1，A/H 同股同权）

  A 收盘  quantdb/1_kline_data/daily_unadjusted（原始价；前复权价会引入历史分红偏差，
          见 references/data-map.md「A 侧前复权偏差」）
  H 收盘  quanthk/1_kline_data/daily_forward（不复权；同 (symbol,date) 双来源重复行
          须去重，优先 akshare release——paid_hk 对部分标的施加过复权/缩放）
  汇率    ah_premium 数据集自带列 fx_hkd_cny（中行折算价，akshare currency_boc_sina /100），
          一日一值；数据集覆盖日之外必须 --fx 由用户提供，脚本不联网抓汇率
  配对    quanthk/2_base_sector/ah_membership.parquet（akshare 名称匹配，159 行/149 对，
          含 10 对内重；名称口径差异的配对标的不在表内）

标定（2026-10-07，见 references/data-map.md）：
  - 数据集最新分区 dt=20260827 上，数据集 premium_pct == 本脚本重算值（149/149 逐分精确）；
  - 数据集历史日的 a_close 是构建时点前复权价，与当日真实成交价的偏差随分红累积
    （2025-01-15 抽样：101 对中 88 对偏差 >0.05pp，平均 |Δ|≈7.5pp，最大 25.5pp）——
    因此历史分位一律用本脚本重算的「原始价」序列，不以数据集自带 premium_pct 为准；
  - 250 日窗口内数据集重复行共 12523 组、组内取值完全一致（去重安全）。

仅 demo/--input 模式为纯标准库；--quantdb 模式需要 pandas/duckdb（在 quantmind 容器内运行）。

用法：
  python3 ah_parity_audit.py --demo
  python3 ah_parity_audit.py --input pairs.csv --out report.json [--md report.md]
  python3 ah_parity_audit.py --quantdb [--date 2026-08-27] [--window 250] [--fx 0.8654]
      [--top 10] [--out /data/reports/cross-listing-parity/ah_20260827.json] [--md ...]
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

SKILL = "cross-listing-parity"
DEFAULT_WINDOW = 250  # 历史分位窗口（交易日数）
DEFAULT_TOP = 10
FORMULA_TOL_PP = 1e-6  # 数据集内部公式核对容忍（百分点）
PREMIUM_TOL_PP = 0.05  # 重算 vs 数据集允许偏差（百分点）
H_SOURCE_TOL = 0.005  # 同一 H 标的同日两来源相对差容忍（0.5%）
EXTREME_PREMIUM_PCT = 100.0  # 仅作统计标记，不判对错

DISCLAIMER = (
    "本报告由本地数据自动生成，仅用于研究学习与技术演示，不构成任何投资建议。"
    "跨市场价差受汇率、交易时段、股息税与资金管制影响，不能直接视为可执行的套利机会。"
)

REQUIRED_COLUMNS = {"date", "a_symbol", "h_symbol", "a_close", "h_close", "fx_hkd_cny"}
OPTIONAL_COLUMNS = {"premium_pct", "ratio"}


# ---------------------------------------------------------------- 通用工具


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def norm_dt(value: str) -> str:
    digits = re.sub(r"\D", "", str(value))
    if len(digits) != 8:
        raise ValueError(f"日期格式应为 YYYY-MM-DD 或 YYYYMMDD: {value!r}")
    return digits


def fmt_dt(dt: str) -> str:
    return f"{dt[:4]}-{dt[4:6]}-{dt[6:8]}"


def premium_pct(a_close, h_close, fx, ratio: float = 1.0):
    """溢价（百分点）：A收盘(CNY) / (H收盘(HKD) × fx_hkd_cny × ratio) − 1。"""
    try:
        a, h, f, r = float(a_close), float(h_close), float(fx), float(ratio)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (a, h, f, r)):
        return None
    if a <= 0 or h <= 0 or f <= 0 or r <= 0:
        return None
    return (a / (h * f * r) - 1.0) * 100.0


def percentile_rank(values: list[float], value: float):
    """value 在 values 中的分位（0-100，含自身；值相等按 ≤ 计）。"""
    if not values or value is None:
        return None
    n = sum(1 for v in values if v <= value)
    return 100.0 * n / len(values)


def series_stats(values: list[float]):
    if not values:
        return None
    return {
        "n": len(values),
        "mean": round(statistics.fmean(values), 4),
        "std": round(statistics.pstdev(values), 4) if len(values) > 1 else 0.0,
        "min": round(min(values), 4),
        "max": round(max(values), 4),
    }


def finding(
    fid: str, severity: str, kind: str, detail: str, evidence=None, fix=None
) -> dict:
    out = {"id": fid, "severity": severity, "kind": kind, "detail": detail}
    if evidence is not None:
        out["evidence"] = evidence
    if fix is not None:
        out["recommended_fix"] = fix
    return out


def summarize(pairs: list[dict]) -> dict:
    vals = [p["premium_pct"] for p in pairs if p.get("premium_pct") is not None]
    if not vals:
        return {"computed_pairs": 0}
    ordered = sorted(vals)
    return {
        "computed_pairs": len(vals),
        "median_premium_pct": round(statistics.median(ordered), 4),
        "mean_premium_pct": round(statistics.fmean(ordered), 4),
        "p5_premium_pct": round(ordered[max(0, int(0.05 * (len(ordered) - 1)))], 4),
        "p95_premium_pct": round(
            ordered[min(len(ordered) - 1, int(0.95 * (len(ordered) - 1)))], 4
        ),
        "a_above_h_count": sum(1 for v in vals if v > 0),
        "a_below_h_count": sum(1 for v in vals if v < 0),
        "extreme_above_100pct_count": sum(1 for v in vals if v >= EXTREME_PREMIUM_PCT),
    }


# ---------------------------------------------------------------- 离线引擎（demo / --input）

DEMO_ROWS = [
    # 同一对 3 天：ratio=1；再给一对 ratio=2 的样例覆盖股数比路径
    {
        "date": "2026-08-25",
        "a_symbol": "601398.SH",
        "h_symbol": "1398.HK",
        "a_close": "7.70",
        "h_close": "7.30",
        "fx_hkd_cny": "0.8650",
        "premium_pct": "21.90",
    },
    {
        "date": "2026-08-26",
        "a_symbol": "601398.SH",
        "h_symbol": "1398.HK",
        "a_close": "7.76",
        "h_close": "7.40",
        "fx_hkd_cny": "0.8652",
        "premium_pct": "21.20",
    },
    {
        "date": "2026-08-27",
        "a_symbol": "601398.SH",
        "h_symbol": "1398.HK",
        "a_close": "7.82",
        "h_close": "7.455",
        "fx_hkd_cny": "0.8654",
        "premium_pct": "21.21",
    },
    {
        "date": "2026-08-25",
        "a_symbol": "000333.SZ",
        "h_symbol": "0300.HK",
        "a_close": "72.50",
        "h_close": "73.10",
        "fx_hkd_cny": "0.8650",
        "premium_pct": "14.65",
    },
    {
        "date": "2026-08-26",
        "a_symbol": "000333.SZ",
        "h_symbol": "0300.HK",
        "a_close": "73.00",
        "h_close": "74.00",
        "fx_hkd_cny": "0.8652",
        "premium_pct": "14.02",
    },
    {
        "date": "2026-08-27",
        "a_symbol": "000333.SZ",
        "h_symbol": "0300.HK",
        "a_close": "73.82",
        "h_close": "74.80",
        "fx_hkd_cny": "0.8654",
        "premium_pct": "14.05",
    },
    {
        "date": "2026-08-27",
        "a_symbol": "603259.SH",
        "h_symbol": "2359.HK",
        "a_close": "159.94",
        "h_close": "199.00",
        "fx_hkd_cny": "0.8654",
        "premium_pct": "-7.13",
    },
    {
        "date": "2026-08-27",
        "a_symbol": "300750.SZ",
        "h_symbol": "3750.HK",
        "a_close": "373.00",
        "h_close": "613.50",
        "fx_hkd_cny": "0.8654",
        "premium_pct": "-29.74",
    },
]


def load_rows(path: str | None, demo_rows: list[dict]) -> list[dict[str, str]]:
    if path is None:
        return [{k: str(v) for k, v in row.items()} for row in demo_rows]
    with open(path, encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def run_offline(rows: list[dict], top_n: int) -> dict:
    findings: list[dict] = []
    if not rows:
        findings.append(
            finding("input_empty", "high", "input", "输入为空，无法计算溢价。")
        )
        return {
            "skill": SKILL,
            "mode": "offline",
            "status": "fail",
            "asof_date": None,
            "generated_at": now_iso(),
            "pairs": [],
            "top": [],
            "bottom": [],
            "crosscheck": {},
            "quality_findings": findings,
            "limitations": [],
            "disclaimer": DISCLAIMER,
        }
    actual = set(rows[0])
    missing = sorted(REQUIRED_COLUMNS - actual)
    if missing:
        findings.append(
            finding(
                "missing_columns",
                "high",
                "input",
                f"缺少必需列：{missing}",
                fix="列：date,a_symbol,h_symbol,a_close,h_close,fx_hkd_cny[,premium_pct,ratio]",
            )
        )

    recs = []
    bad_rows = []
    for i, row in enumerate(rows, 2):
        try:
            dt = norm_dt(row["date"])
        except (KeyError, ValueError) as exc:
            bad_rows.append({"row": i, "reason": str(exc)})
            continue
        ratio = 1.0
        if row.get("ratio") not in (None, ""):
            try:
                ratio = float(row["ratio"])
            except ValueError:
                bad_rows.append({"row": i, "reason": f"ratio 非数值: {row['ratio']!r}"})
                continue
        value = premium_pct(
            row.get("a_close"), row.get("h_close"), row.get("fx_hkd_cny"), ratio
        )
        if value is None:
            bad_rows.append({"row": i, "reason": "价格/汇率缺失或非正数"})
            continue
        rec = {
            "date": fmt_dt(dt),
            "dt": dt,
            "a_symbol": row["a_symbol"],
            "h_symbol": row["h_symbol"],
            "a_close": float(row["a_close"]),
            "h_close": float(row["h_close"]),
            "fx_hkd_cny": float(row["fx_hkd_cny"]),
            "ratio": ratio,
            "premium_pct": round(value, 4),
        }
        if row.get("premium_pct") not in (None, ""):
            stored = float(row["premium_pct"])
            rec["premium_pct_input"] = stored
            rec["diff_input_pp"] = round(value - stored, 6)
        if row.get("name"):
            rec["name"] = row["name"]
        recs.append(rec)

    if bad_rows:
        findings.append(
            finding(
                "input_rows_skipped",
                "medium",
                "input",
                f"{len(bad_rows)} 行因字段无效被跳过。",
                evidence=bad_rows[:10],
            )
        )
    if not recs:
        findings.append(finding("no_valid_rows", "high", "input", "没有可计算的行。"))

    asof = max((r["dt"] for r in recs), default=None)
    series: dict[tuple, list[float]] = {}
    for r in recs:
        series.setdefault((r["h_symbol"], r["a_symbol"]), []).append(r["premium_pct"])

    pairs = [r for r in recs if r["dt"] == asof]
    for r in pairs:
        vals = series[(r["h_symbol"], r["a_symbol"])]
        r["pct_rank_window"] = (
            round(percentile_rank(vals, r["premium_pct"]), 2) if len(vals) > 1 else None
        )
        r["window_days"] = len(vals)
        r["series_stats"] = series_stats(vals)

    diff_vals = [abs(r["diff_input_pp"]) for r in recs if "diff_input_pp" in r]
    crosscheck = {
        "rows_used": len(recs),
        "asof_rows": len(pairs),
        "input_premium_max_abs_diff_pp": round(max(diff_vals), 6)
        if diff_vals
        else None,
    }
    if diff_vals and max(diff_vals) > PREMIUM_TOL_PP:
        findings.append(
            finding(
                "input_premium_mismatch",
                "high",
                "formula",
                f"输入文件自带 premium_pct 与重算值最大差 {max(diff_vals):.4f}pp（容忍 {PREMIUM_TOL_PP}pp），口径可能不一致。",
                evidence=[
                    r for r in recs if abs(r.get("diff_input_pp", 0.0)) > PREMIUM_TOL_PP
                ][:10],
            )
        )

    ranked = sorted(pairs, key=lambda r: r["premium_pct"], reverse=True)
    status = (
        "warning"
        if any(f["severity"] in ("high", "medium") for f in findings)
        else "pass"
    )
    return {
        "skill": SKILL,
        "mode": "offline",
        "status": status,
        "asof_date": fmt_dt(asof) if asof else None,
        "generated_at": now_iso(),
        "fx": {"hkd_cny": pairs[0]["fx_hkd_cny"] if pairs else None, "source": "input"},
        "summary": summarize(pairs),
        "pairs": pairs,
        "top": ranked[:top_n],
        "bottom": ranked[-top_n:][::-1],
        "crosscheck": crosscheck,
        "quality_findings": findings,
        "formula": "premium_pct = (a_close / (h_close * fx_hkd_cny * ratio) - 1) * 100",
        "limitations": [
            "离线模式只校验输入/公式一致性；数据来源与汇率日期由调用方保证。"
        ],
        "disclaimer": DISCLAIMER,
    }


# ---------------------------------------------------------------- QuantDB 装配


def resolve_data_root(cli_root: str | None) -> Path:
    candidates = []
    if cli_root:
        candidates.append(Path(cli_root))
    if os.environ.get("QM_DATA_ROOT"):
        candidates.append(Path(os.environ["QM_DATA_ROOT"]))
    candidates.append(Path("/data"))  # quantmind 容器
    script_path = Path(__file__).resolve()
    if len(script_path.parents) >= 4:  # 宿主仓库 skills/<skill>/scripts/
        candidates.append(script_path.parents[3] / "data")
    for cand in candidates:
        if (cand / "quanthk" / "2_base_sector" / "ah_membership.parquet").exists():
            return cand
    tried = ", ".join(str(c) for c in candidates)
    raise SystemExit(
        f"未找到 QuantDB 数据目录（试过 {tried}）；可用 --data-root 或 QM_DATA_ROOT 指定"
    )


def _read_parquet_window(
    con, files: list[str], symbols: list[str], cols: list[str], extra_where: str = ""
):
    col_sql = ", ".join(cols + ["filename"])
    sql = (
        f"SELECT {col_sql} FROM read_parquet(?, hive_partitioning=false, filename=true) "
        f"WHERE symbol IN (SELECT unnest(?::VARCHAR[])) {extra_where}"
    )
    return con.execute(sql, [files, symbols]).fetchdf()


def run_quantdb(args) -> dict:
    try:
        import duckdb
        import pandas as pd
    except ImportError as exc:  # pragma: no cover - 环境提示
        raise SystemExit(
            f"--quantdb 模式需要 pandas/duckdb（请在 quantmind 容器内运行）：{exc}"
        ) from None

    root = resolve_data_root(getattr(args, "data_root", None))
    hk_base = root / "quanthk"
    ds_dir = hk_base / "2_base_sector" / "ah_premium"
    membership_path = hk_base / "2_base_sector" / "ah_membership.parquet"
    cn_dir = root / "quantdb" / "1_kline_data" / "daily_unadjusted"
    hk_dir = hk_base / "1_kline_data" / "daily_forward"

    findings: list[dict] = []

    membership = pd.read_parquet(membership_path)
    membership_rows = len(membership)
    member_cols = [
        c for c in ("h_symbol", "a_symbol", "名称") if c in membership.columns
    ]
    membership = (
        membership[member_cols]
        .drop_duplicates(["h_symbol", "a_symbol"])
        .reset_index(drop=True)
    )
    dup_rows = membership_rows - len(membership)
    if dup_rows > 0:
        findings.append(
            finding(
                "membership_duplicates",
                "low",
                "mapping",
                f"配对表 {membership_rows} 行 / {len(membership)} 对，含 {dup_rows} 行内重（akshare 清单重复），已按 (h_symbol,a_symbol) 去重。",
                fix="需要完整配对面时人工核对 ah_membership.parquet 的 名称 匹配口径。",
            )
        )
    name_map = (
        dict(
            zip(
                zip(membership["h_symbol"], membership["a_symbol"], strict=True),
                membership.get("名称", pd.Series([None] * len(membership))),
                strict=True,
            )
        )
        if "名称" in membership.columns
        else {}
    )

    parts = sorted(
        (p.name.split("=")[1], p / "data.parquet") for p in ds_dir.glob("dt=*")
    )
    if not parts:
        raise SystemExit(f"未找到 ah_premium 分区：{ds_dir}")
    ds_last = parts[-1][0]

    target = norm_dt(args.date) if args.date else ds_last
    window_parts = [p for p in parts if p[0] <= target][-args.window :]
    if not window_parts:
        raise SystemExit(
            f"目标日 {fmt_dt(target)} 之前没有数据集分区（最早 {fmt_dt(parts[0][0])}）。"
        )

    con = duckdb.connect()
    con.execute("SET threads=2")

    # ---- 数据集窗口（fx / 自带 premium / 配对覆盖）
    files = [str(p) for _, p in window_parts]
    prem = con.execute(
        "SELECT h_symbol, a_symbol, a_close, h_close, fx_hkd_cny, premium_pct, filename "
        "FROM read_parquet(?, hive_partitioning=false, filename=true)",
        [files],
    ).fetchdf()
    prem["dt"] = prem["filename"].str.extract(r"dt=(\d{8})")[0]
    dup_groups = prem.groupby(["dt", "h_symbol", "a_symbol"])["premium_pct"].agg(
        ["size", "nunique"]
    )
    n_dup_groups = int((dup_groups["size"] > 1).sum())
    n_dup_distinct = int((dup_groups["nunique"] > 1).sum())
    if n_dup_groups:
        findings.append(
            finding(
                "premium_row_duplicates",
                "low",
                "dataset",
                f"数据集窗口内 {n_dup_groups} 组 (日期,配对) 存在重复行（组内取值不同的是 {n_dup_distinct} 组），已按值去重。",
                fix="重复来源未定；读侧统一按 (dt,h_symbol,a_symbol) 去重并对组内分歧报警。",
            )
        )
    prem = prem.sort_values(["dt", "h_symbol", "a_symbol"]).drop_duplicates(
        ["dt", "h_symbol", "a_symbol"]
    )

    # 数据集内部公式核对：premium_pct 是否等于自家 a_close/h_close/fx 的折算
    calc = prem.apply(
        lambda r: premium_pct(r["a_close"], r["h_close"], r["fx_hkd_cny"]), axis=1
    )
    formula_diff = (calc - prem["premium_pct"]).abs()
    formula_max = float(formula_diff.max()) if len(formula_diff) else 0.0
    if formula_max > FORMULA_TOL_PP:
        bad = prem.loc[
            formula_diff > FORMULA_TOL_PP, ["dt", "h_symbol", "a_symbol", "premium_pct"]
        ]
        findings.append(
            finding(
                "dataset_formula_mismatch",
                "high",
                "dataset",
                f"数据集自带 premium_pct 与自家价格列折算最大差 {formula_max:.4f}pp，存在构建口径错误。",
                evidence=bad.head(10).to_dict("records"),
            )
        )
    fx_by_dt = prem.groupby("dt")["fx_hkd_cny"].first().to_dict()
    ds_dates = set(fx_by_dt)

    # ---- 目标日汇率
    if target in fx_by_dt:
        fx_target = float(fx_by_dt[target])
        fx_source = (
            f"dataset:ah_premium/dt={target}（中行折算价，akshare currency_boc_sina）"
        )
    elif getattr(args, "fx", None) is not None:
        fx_target = float(args.fx)
        fx_source = "user:--fx"
        findings.append(
            finding(
                "fx_user_supplied",
                "info",
                "fx",
                f"目标日 {fmt_dt(target)} 无数据集汇率，采用用户提供的 fx_hkd_cny={fx_target}；报告以用户汇率为准。",
            )
        )
    else:
        fx_target, fx_source = None, None
        findings.append(
            finding(
                "fx_missing",
                "info",
                "fx",
                f"目标日 {fmt_dt(target)} 无可用汇率（本地无独立汇率源，数据集止于 {fmt_dt(ds_last)}），"
                "只输出原币价格与配对缺口，不计算溢价。",
                fix="提供 --fx <HKD兑CNY>（并记录来源与日期），或把目标日退回数据集覆盖范围。",
            )
        )

    # ---- 本地行情（目标日 + 窗口）
    a_syms = sorted(membership["a_symbol"].unique())
    h_syms = sorted(membership["h_symbol"].unique())

    def _load_market(dts: list[str], base: Path):
        f = [
            str(base / f"dt={d}" / "data.parquet")
            for d in dts
            if (base / f"dt={d}" / "data.parquet").exists()
        ]
        if not f:
            return None
        return f

    window_dts = [d for d, _ in window_parts]
    cn_files = _load_market(
        window_dts + ([target] if target not in window_dts else []), cn_dir
    )
    hk_files = _load_market(
        window_dts + ([target] if target not in window_dts else []), hk_dir
    )

    cn_cols = ["symbol", "close"]
    hk_cols = ["symbol", "close", "release_id"]
    cn = _read_parquet_window(con, cn_files, a_syms, cn_cols) if cn_files else None
    hk = _read_parquet_window(con, hk_files, h_syms, hk_cols) if hk_files else None
    if cn is None or hk is None:
        raise SystemExit("缺少 CN/HK 行情分区，无法重算。")
    for frame in (cn, hk):
        frame["dt"] = frame["filename"].str.extract(r"dt=(\d{8})")

    cn = cn.rename(columns={"symbol": "a_symbol", "close": "a_close"}).drop_duplicates(
        ["dt", "a_symbol"]
    )

    # H 侧去重：优先 akshare release（原始成交价）；paid_hk 对部分标的做过复权/缩放
    hk["_pref"] = (hk["release_id"] != "akshare").astype(int)
    hk_sorted = hk.sort_values(["dt", "symbol", "_pref"])
    hk_primary = hk_sorted.drop_duplicates(["dt", "symbol"]).rename(
        columns={"symbol": "h_symbol", "close": "h_close", "release_id": "h_release"}
    )[["dt", "h_symbol", "h_close", "h_release"]]

    # 两来源分歧（仅统计窗口内、配对表内）
    pivot = hk.pivot_table(
        index=["dt", "symbol"], columns="release_id", values="close", aggfunc="first"
    )
    if {"akshare", "paid_hk"} <= set(pivot.columns):
        both = pivot.dropna(subset=["akshare", "paid_hk"]).reset_index()
        both = both[both["symbol"].isin(h_syms)]
        both["rel_diff"] = (both["akshare"] - both["paid_hk"]).abs() / both[
            "akshare"
        ].abs().clip(lower=1e-12)
        disagree = both[both["rel_diff"] > H_SOURCE_TOL]
    else:
        disagree = None
    if disagree is not None and len(disagree):
        disagree = disagree.copy()
        disagree["rel_diff"] = disagree["rel_diff"].round(4)
        findings.append(
            finding(
                "h_source_disagreement",
                "medium",
                "market",
                f"窗口内 {len(disagree)} 个 (日期,H标的) 的 akshare 与 paid_hk 收盘相对差 >{H_SOURCE_TOL:.1%}；"
                "脚本统一取 akshare（标定显示 paid_hk 对部分标的施加过复权/缩放，非原始成交价）。",
                evidence=disagree.nlargest(8, "rel_diff")[
                    ["dt", "symbol", "akshare", "paid_hk", "rel_diff"]
                ].to_dict("records"),
                fix="对分歧标的单独核对交易所行情后再引用。",
            )
        )

    # ---- 窗口重算序列（原始价口径）
    m = (
        prem[["dt", "h_symbol", "a_symbol", "premium_pct"]]
        .merge(cn, on=["dt", "a_symbol"], how="left")
        .merge(hk_primary, on=["dt", "h_symbol"], how="left")
    )
    m["fx"] = m["dt"].map(fx_by_dt)
    m["rebased_pct"] = m.apply(
        lambda r: premium_pct(r["a_close"], r["h_close"], r["fx"]), axis=1
    )

    # ---- 目标日全配对表（独立于数据集，直接用当日行情）
    cn_t = cn[cn["dt"] == target].drop(columns=["dt"])
    hk_t = hk_primary[hk_primary["dt"] == target].drop(columns=["dt"])
    tbl = membership.merge(cn_t, on="a_symbol", how="left").merge(
        hk_t, on="h_symbol", how="left"
    )
    if target in ds_dates:
        ds_today = prem[prem["dt"] == target][["h_symbol", "a_symbol", "premium_pct"]]
        tbl = tbl.merge(
            ds_today.rename(columns={"premium_pct": "premium_pct_dataset"}),
            on=["h_symbol", "a_symbol"],
            how="left",
        )
    if fx_target is not None:
        tbl["premium_pct"] = tbl.apply(
            lambda r: premium_pct(r["a_close"], r["h_close"], fx_target), axis=1
        )
    else:
        tbl["premium_pct"] = None

    series = m.groupby(["h_symbol", "a_symbol"])["rebased_pct"].apply(
        lambda s: s.dropna().tolist()
    )

    pairs = []
    missing_a, missing_h = [], []
    for _, r in tbl.iterrows():
        rec = {
            "a_symbol": r["a_symbol"],
            "h_symbol": r["h_symbol"],
            "name": name_map.get((r["h_symbol"], r["a_symbol"])),
            "a_close": None
            if pd.isna(r.get("a_close"))
            else round(float(r["a_close"]), 4),
            "h_close": None
            if pd.isna(r.get("h_close"))
            else round(float(r["h_close"]), 4),
            "h_release": None if pd.isna(r.get("h_release")) else str(r["h_release"]),
            "fx_hkd_cny": fx_target,
            "ratio": 1.0,
            "premium_pct": None
            if r.get("premium_pct") is None or pd.isna(r.get("premium_pct"))
            else round(float(r["premium_pct"]), 4),
        }
        ds_val = r.get("premium_pct_dataset")
        if ds_val is not None and not pd.isna(ds_val):
            rec["premium_pct_dataset"] = round(float(ds_val), 4)
            if rec["premium_pct"] is not None:
                rec["diff_pp"] = round(rec["premium_pct"] - float(ds_val), 4)
        if pd.isna(r.get("a_close")):
            missing_a.append(rec["a_symbol"])
        if pd.isna(r.get("h_close")):
            missing_h.append(rec["h_symbol"])
        vals = series.get((r["h_symbol"], r["a_symbol"]), [])
        rec["window_days"] = len(vals)
        rec["pct_rank_window"] = (
            round(percentile_rank(vals, rec["premium_pct"]), 2)
            if (rec["premium_pct"] is not None and vals)
            else None
        )
        rec["series_stats"] = series_stats(vals)
        pairs.append(rec)

    if missing_a or missing_h:
        findings.append(
            finding(
                "price_missing",
                "medium",
                "market",
                f"目标日 {fmt_dt(target)}：{len(missing_a)} 只 A 股、{len(missing_h)} 只 H 股缺收盘价，相关配对不计溢价。"
                "（H 侧缺失多为 H 尚未上市/停牌或行情源缺口。）",
                evidence={
                    "missing_a_symbols": missing_a[:20],
                    "missing_h_symbols": missing_h[:20],
                },
            )
        )

    short_cov = [
        p
        for p in pairs
        if p["premium_pct"] is not None and p["window_days"] < args.window
    ]
    if short_cov:
        findings.append(
            finding(
                "short_history_coverage",
                "low",
                "coverage",
                f"{len(short_cov)}/{sum(1 for p in pairs if p['premium_pct'] is not None)} 对在窗口内历史不足 {args.window} 天"
                "（H 新上市或行情缺口），其分位按实际天数计算。",
                evidence=[
                    {
                        "h_symbol": p["h_symbol"],
                        "a_symbol": p["a_symbol"],
                        "days": p["window_days"],
                    }
                    for p in short_cov[:10]
                ],
            )
        )

    # ---- 目标日交叉校验
    computed = [p for p in pairs if p["premium_pct"] is not None]
    ds_join = [p for p in computed if "premium_pct_dataset" in p]
    rebased_delta = [abs(p["diff_pp"]) for p in ds_join if "diff_pp" in p]
    over_tol = [p for p in ds_join if abs(p.get("diff_pp", 0.0)) > PREMIUM_TOL_PP]
    if over_tol:
        findings.append(
            finding(
                "rebased_vs_dataset_divergence",
                "medium",
                "crosscheck",
                f"目标日 {len(over_tol)}/{len(ds_join)} 对「原始价重算 vs 数据集」偏差 >{PREMIUM_TOL_PP}pp"
                "（数据集历史日 A 侧为构建时点前复权价，见 references/data-map.md）。",
                evidence=over_tol[:10],
            )
        )

    # 窗口层面：数据集 vs 重算的平均偏差（衡量 A 侧前复权累积效应）
    win = m.dropna(subset=["rebased_pct", "premium_pct"]).copy()
    win["delta_pp"] = (win["rebased_pct"] - win["premium_pct"]).abs()
    win_base_excluded = win[win["dt"] != ds_last]
    bias_mean = (
        float(win_base_excluded["delta_pp"].mean()) if len(win_base_excluded) else 0.0
    )
    if bias_mean > PREMIUM_TOL_PP:
        top_off = win_base_excluded.nlargest(5, "delta_pp")[
            ["dt", "h_symbol", "a_symbol", "premium_pct", "rebased_pct", "delta_pp"]
        ]
        findings.append(
            finding(
                "a_side_adjustment_bias",
                "medium",
                "dataset",
                f"数据集历史日 a_close 为构建时点前复权价：窗口内（剔除基期 {fmt_dt(ds_last)}）重算与数据集平均 |Δ|={bias_mean:.2f}pp，"
                "分红越多的标的偏差越大。历史分位一律以重算（原始价）序列为准。",
                evidence=top_off.round(4).to_dict("records"),
            )
        )

    # 本地行情最新共同日（用于 stale 提示）
    local_dts = sorted(
        {os.path.basename(p).split("=")[1] for p in cn_dir.glob("dt=*")}
        & {os.path.basename(p).split("=")[1] for p in hk_dir.glob("dt=*")}
    )
    local_last = local_dts[-1] if local_dts else None
    if local_last and target < local_last:
        findings.append(
            finding(
                "dataset_stale",
                "info",
                "dataset",
                f"数据集最新分区为 {fmt_dt(ds_last)}，本地行情已到 {fmt_dt(local_last)}；"
                "重算更晚日期需 --date 与用户提供汇率（--fx）。",
                fix=f"python3 ah_parity_audit.py --quantdb --date {fmt_dt(local_last)} --fx <HKD兑CNY>",
            )
        )

    ranked = sorted(computed, key=lambda r: r["premium_pct"], reverse=True)
    crosscheck = {
        "asof_pairs_total": len(pairs),
        "asof_pairs_computed": len(computed),
        "dataset_pairs_on_target": int(len(prem[prem["dt"] == target]))
        if target in ds_dates
        else 0,
        "rebased_vs_dataset_n": len(ds_join),
        "rebased_vs_dataset_max_abs_diff_pp": round(max(rebased_delta), 6)
        if rebased_delta
        else None,
        "rebased_vs_dataset_over_tol": len(over_tol),
        "dataset_formula_max_abs_diff_pp": round(formula_max, 6),
        "window_rebased_vs_dataset_mean_abs_diff_pp": round(bias_mean, 4),
        "window": {
            "days": len(window_parts),
            "start": fmt_dt(window_parts[0][0]),
            "end": fmt_dt(window_parts[-1][0]),
        },
    }

    status = "pass"
    if any(f["severity"] == "high" for f in findings):
        status = "fail"
    elif fx_target is None:
        status = "insufficient-evidence"
    elif any(f["severity"] == "medium" for f in findings):
        status = "warning"

    limitations = [
        "溢价只做同一公司 A/H 收盘价的折算观察，未建模股息税、资金管制、融券成本、结算与交易时段差异；价差不是可执行套利信号。",
        "历史分位基于当前配对表回看，H 新上市/停牌日缺数据；窗口天数不足者以实际天数计。",
        "汇率来自数据集内置列（覆盖至最后一期）；更晚日期由用户提供 --fx，脚本不联网抓取汇率。",
    ]

    return {
        "skill": SKILL,
        "mode": "quantdb",
        "status": status,
        "asof_date": fmt_dt(target),
        "generated_at": now_iso(),
        "fx": {
            "hkd_cny": fx_target,
            "source": fx_source,
            "direction": "1 HKD = fx_hkd_cny CNY",
        },
        "universe": {
            "membership_rows": membership_rows,
            "pairs": len(membership),
            "membership_duplicate_rows": dup_rows,
            "dataset_last_date": fmt_dt(ds_last),
            "local_kline_last_common_date": fmt_dt(local_last) if local_last else None,
        },
        "data_sources": {
            "membership": "quanthk/2_base_sector/ah_membership.parquet（akshare stock_zh_ah_name 名称匹配）",
            "premium_dataset": "quanthk/2_base_sector/ah_premium/dt=YYYYMMDD/（A 侧为构建时点前复权价）",
            "a_close": "quantdb/1_kline_data/daily_unadjusted（原始价）",
            "h_close": "quanthk/1_kline_data/daily_forward（不复权；去重优先 akshare release）",
            "fx": "数据集列 fx_hkd_cny（中行折算价）",
        },
        "formula": "premium_pct = (a_close / (h_close * fx_hkd_cny * ratio) - 1) * 100，ratio=1（A/H 同股同权）",
        "summary": summarize(pairs),
        "pairs": pairs,
        "top": ranked[: args.top],
        "bottom": ranked[-args.top :][::-1],
        "crosscheck": crosscheck,
        "quality_findings": findings,
        "limitations": limitations,
        "disclaimer": DISCLAIMER,
    }


# ---------------------------------------------------------------- Markdown 报告


def _pct(v):
    return "—" if v is None else f"{v:.2f}%"


def _rank(v):
    return "—" if v is None else f"{v:.0f}%"


def _row(i, p):
    return (
        f"| {i} | {p['h_symbol']} | {p['a_symbol']} | {p.get('name') or '—'} | "
        f"{p['a_close']} | {p['h_close']} | {_pct(p['premium_pct'])} | "
        f"{_rank(p.get('pct_rank_window'))} |"
    )


def render_markdown(report: dict) -> str:
    asof = report.get("asof_date") or "（未知）"
    fx = report.get("fx", {})
    if not fx.get("hkd_cny"):
        fx = {"hkd_cny": None, "source": fx.get("source") or "缺失"}
    s = report.get("summary", {})
    lines = [
        f"# A/H 跨市场平价（溢价）日报 — {asof}",
        "",
        "## 摘要",
        "",
        f"- 数据日：{asof}（A 股、港股收盘快照 snapshot，非实时 T+1 盘中数据）。",
        f"- 配对表：ah_membership 映射表版本 {report.get('universe', {}).get('dataset_last_date', '—')}；"
        f"覆盖 {s.get('computed_pairs', 0)} 对可计算配对。",
        f"- 溢价中位数 {_pct(s.get('median_premium_pct'))}，均值 {_pct(s.get('mean_premium_pct'))}；"
        f"A 高于 H 折算价 {s.get('a_above_h_count', 0)} 对，A 低于 H {s.get('a_below_h_count', 0)} 对。",
        f"- 汇率：1 HKD = {fx['hkd_cny']} CNY（来源：{fx['source']}）。",
        "",
        "## A/H 溢价排行",
        "",
        "### 溢价最高（A 相对 H 更贵）",
        "",
        "| # | H 代码 | A 代码 | 名称 | A收盘(CNY) | H收盘(HKD) | 溢价 | 窗口分位 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for i, p in enumerate(report.get("top", []), 1):
        lines.append(_row(i, p))
    lines += [
        "",
        "### 折价最深（A 相对 H 更便宜）",
        "",
        "| # | H 代码 | A 代码 | 名称 | A收盘(CNY) | H收盘(HKD) | 溢价 | 窗口分位 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for i, p in enumerate(report.get("bottom", []), 1):
        lines.append(_row(i, p))
    cc = report.get("crosscheck", {})
    w = cc.get("window", {})
    lines += [
        "",
        "## 极值与历史分位",
        "",
        f"- 窗口：{w.get('start', '—')} ~ {w.get('end', '—')}，共 {w.get('days', '—')} 个交易日（重算原始价口径）。",
        f"- 目标日重算与数据集自带溢价的最大偏差 {cc.get('rebased_vs_dataset_max_abs_diff_pp')} 百分点"
        f"（{cc.get('rebased_vs_dataset_over_tol', 0)} 对超容忍）；窗口内平均 |Δ| "
        f"{cc.get('window_rebased_vs_dataset_mean_abs_diff_pp')} 百分点（数据集历史日含 A 侧前复权偏差）。",
        "- 分位含义：该配对在窗口内 ≤ 当前溢价的交易日占比；分位数据不足者在表中记 —。",
        "",
        "## 数据说明",
        "",
        "- 数据来源：本地 QuantDB（A 收盘 quantdb/1_kline_data/daily_unadjusted 原始价；"
        "H 收盘 quanthk/1_kline_data/daily_forward 不复权；配对 quanthk/2_base_sector/ah_membership.parquet；"
        "历史溢价参考 quanthk/2_base_sector/ah_premium）。",
        f"- 数据日：A 股与港股均为 {asof} 收盘；快照 snapshot，不构成 T+1 实时行情。",
        f"- 汇率来源：{fx['source']}，方向 1 HKD = {fx['hkd_cny']} CNY；HKD/CNY 波动会同时放大或收窄全部配对溢价。",
        "- 股数比 ratio=1：A 股与 H 股为同一公司同股同权股份（映射表版本见上文）；"
        "若标的含特殊股本结构需人工核对映射表版本后再引用。",
        "- 缺失与降级：缺失配对或价格的标的列入质量问题，不以估算值填补；汇率缺失时只报原币价格。",
        "- 免责声明：本报告仅用于研究学习与技术演示，不构成任何投资建议。",
    ]
    for f in report.get("quality_findings", []):
        lines.append(f"- 质量问题[{f['severity']}] {f['kind']}：{f['detail']}")
    lines += ["", "## 免责声明", "", DISCLAIMER, ""]
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--demo", action="store_true", help="内置样本烟雾测试（纯标准库）"
    )
    mode.add_argument(
        "--input",
        help="任意配对 CSV（列：date,a_symbol,h_symbol,a_close,h_close,fx_hkd_cny[,premium_pct,ratio]）",
    )
    mode.add_argument(
        "--quantdb",
        action="store_true",
        help="QuantDB 本地直读（需 pandas/duckdb，容器内跑）",
    )
    parser.add_argument("--date", help="目标日 YYYY-MM-DD；默认=数据集最新分区日")
    parser.add_argument(
        "--window",
        type=int,
        default=DEFAULT_WINDOW,
        help=f"历史分位窗口（交易日，默认 {DEFAULT_WINDOW}）",
    )
    parser.add_argument(
        "--fx", type=float, help="目标日无数据集汇率时由用户提供的 HKD→CNY 汇率"
    )
    parser.add_argument(
        "--top", type=int, default=DEFAULT_TOP, help=f"榜单条数（默认 {DEFAULT_TOP}）"
    )
    parser.add_argument(
        "--data-root", help="QuantDB 数据根（默认探测 /data 或仓库 data/）"
    )
    parser.add_argument("--out", help="JSON 报告落盘路径（缺省打印到 stdout）")
    parser.add_argument("--md", help="同时输出 Markdown 报告路径")
    args = parser.parse_args(argv)

    if args.demo:
        report = run_offline(load_rows(None, DEMO_ROWS), args.top)
    elif args.input:
        report = run_offline(load_rows(args.input, DEMO_ROWS), args.top)
    else:
        report = run_quantdb(args)

    text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n", encoding="utf-8")
        print(f"JSON 报告已写入 {out_path}")
    else:
        print(text)
    if args.md:
        md_path = Path(args.md)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(render_markdown(report), encoding="utf-8")
        print(f"Markdown 报告已写入 {md_path}")
    return 0 if report["status"] in ("pass", "warning") else 1


if __name__ == "__main__":
    sys.exit(main())
