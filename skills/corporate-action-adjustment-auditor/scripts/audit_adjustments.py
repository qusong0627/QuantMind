#!/usr/bin/env python3
"""公司行动复权审计 — 离线确定性引擎 + QuantDB(CN/HK/US) 数据装配。

来源：quantskills/skill-corporate-action-adjustment-auditor（GPL-3.0-only）。
方法论与输出契约保留；数据层由 PandaData 改为本地 QuantDB 直读（2026-10 标定）：

  CN  raw=daily_unadjusted, adj=daily_forward(前复权), events=3_financial_data/dividend_factors
      interest=每10股派息(税前) → div=interest/10；stockBonus=每10股送转 → split=1+bonus/10
      标定 000001.SZ 2026：事件日 |diff|<0.001，非事件日 <1e-12。配股(allotment>0)未建模，单列告警。
  HK  raw=daily_forward(不复权), events=3_financial_data/{dividend,splits}(yahoo，trade_date=除净日)
      无可用全量复权序列（daily_backward 为稀疏派生产物）→ 不做收益等式核对，报 insufficient-evidence；
      改为：跳点检查 + 拆股/送股对齐检查 + 来源重复行检查 + adjust_factors 内部一致性（若文件存在）。
  US  raw=daily_forward(不复权), events=3_financial_data/{dividend,splits}(yahoo)，无复权序列，同上。

仅 demo/--input 模式为纯标准库；--quantdb 模式需要 pandas/pyarrow（在 quantmind 容器内运行）。

用法：
  python3 audit_adjustments.py --demo
  python3 audit_adjustments.py --input rows.csv --out report.json
  python3 audit_adjustments.py --quantdb --market CN --symbols 000001.SZ --start 2026-01-01 --end 2026-09-30
  python3 audit_adjustments.py --quantdb --market HK --symbols 0001.HK,0700.HK --start 2024-09-01 --end 2026-05-08
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------- 离线引擎

_INPUT_ISSUES: list[dict] = []

DEMO = [
    {"symbol": "AAA", "date": "2024-01-01", "close": "100", "adj_close": "50", "split_factor": "1", "cash_dividend": "0"},
    {"symbol": "AAA", "date": "2024-01-02", "close": "50", "adj_close": "50", "split_factor": "2", "cash_dividend": "0"},
    {"symbol": "AAA", "date": "2024-01-03", "close": "80", "adj_close": "80", "split_factor": "1", "cash_dividend": "0"},
]

REQUIRED_COLUMNS = {"adj_close", "cash_dividend", "close", "date", "split_factor", "symbol"}
NUMERIC_COLUMNS = {"adj_close", "cash_dividend", "close", "split_factor"}
OPTIONAL_NUMERIC_COLUMNS: set[str] = set()

# 拆股/送股对齐检查的容忍度：拆股日真实波动 + 对齐误差
SPLIT_ALIGN_TOL = 0.20


def _demo_rows(demo_rows: list[dict]) -> list[dict[str, str]]:
    return [{k: str(v) for k, v in row.items()} for row in demo_rows]


def load_rows(path: str | None, demo_rows: list[dict]) -> list[dict[str, str]]:
    _INPUT_ISSUES.clear()
    if path is None:
        return _demo_rows(demo_rows)
    with open(path, encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    required = set(globals().get("REQUIRED_COLUMNS", set(demo_rows[0]) if demo_rows else set()))
    numeric_columns = set(globals().get("NUMERIC_COLUMNS", set()))
    optional_numeric = set(globals().get("OPTIONAL_NUMERIC_COLUMNS", set()))
    actual = set(rows[0]) if rows else set()
    missing = sorted(required - actual)
    if not rows:
        _INPUT_ISSUES.append({"reason": "empty_input", "required_columns": sorted(required)})
    if missing:
        _INPUT_ISSUES.append({"reason": "missing_columns", "columns": missing})
    for row_number, row in enumerate(rows, 2):
        for key in numeric_columns:
            value = row.get(key)
            if value in (None, "") and key in optional_numeric:
                continue
            try:
                parsed = float(value) if value not in (None, "") else math.nan
            except (TypeError, ValueError):
                parsed = math.nan
            if not math.isfinite(parsed):
                _INPUT_ISSUES.append({"reason": "invalid_numeric", "row": row_number, "column": key, "value": value})
    if _INPUT_ISSUES:
        return _demo_rows(demo_rows)
    return rows


def number(value: object, default: float = 0.0) -> float:
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else default
    except (TypeError, ValueError):
        return default


def text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _normalize_finding(item: object, index: int, source: str) -> dict:
    if isinstance(item, dict):
        evidence = item.get("evidence", item)
        severity = item.get("severity", "medium")
        finding_id = item.get("id", f"{source}-{index}")
        impact = item.get("impact", "Review the domain result and confirm whether the issue changes the research conclusion.")
        recommended_fix = item.get("recommended_fix", "Inspect the cited record, correct the input or assumptions, and rerun the check.")
    else:
        evidence = item
        severity = "medium"
        finding_id = f"{source}-{index}"
        impact = "The detected condition may affect the reliability of the quantitative result."
        recommended_fix = "Review the condition, document the decision, and rerun after correction when applicable."
    return {
        "id": finding_id,
        "severity": severity,
        "evidence": evidence,
        "impact": impact,
        "recommended_fix": recommended_fix,
    }


def _json_safe(value: object) -> object:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    return value


def analyze(
    rows: list[dict[str, str]],
    return_tolerance: float = 0.02,
    jump_threshold: float = 0.40,
    *,
    check_return_mismatch: bool = True,
    extra_findings: list[dict] | None = None,
) -> dict:
    errors: list = []
    if not rows:
        errors.append("at least two rows for one symbol are required")
    if not math.isfinite(return_tolerance) or return_tolerance < 0:
        errors.append("return-tolerance must be finite and non-negative")
    if not math.isfinite(jump_threshold) or jump_threshold <= 0:
        errors.append("jump-threshold must be finite and positive")
    seen: set[tuple[str, str]] = set()
    symbol_counts: dict[str, int] = {}
    for row_number, row in enumerate(rows, 2):
        raw_symbol = text(row.get("symbol"))
        symbol = raw_symbol.strip()
        date_value = text(row.get("date"))
        key = (symbol, date_value)
        symbol_counts[symbol] = symbol_counts.get(symbol, 0) + 1
        if not symbol:
            errors.append({"reason": "empty_symbol", "row": row_number})
        elif raw_symbol != symbol:
            errors.append({"reason": "symbol_surrounding_whitespace", "row": row_number})
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_value):
                raise ValueError("date must use YYYY-MM-DD")
            datetime.strptime(date_value, "%Y-%m-%d")
        except ValueError:
            errors.append({"reason": "invalid_date", "row": row_number})
        if key in seen:
            errors.append({"reason": "duplicate_symbol_date", "row": row_number})
        seen.add(key)
        if number(row.get("split_factor")) <= 0:
            errors.append({"reason": "non_positive_split_factor", "row": row_number})
        if number(row.get("cash_dividend")) < 0:
            errors.append({"reason": "negative_cash_dividend", "row": row_number})
        if number(row.get("close")) <= 0 or number(row.get("adj_close")) <= 0:
            errors.append({"reason": "non_positive_price", "row": row_number})
    for symbol, count in symbol_counts.items():
        if symbol and count < 2:
            errors.append({"reason": "insufficient_symbol_history", "symbol": symbol, "rows": count})
    if errors:
        return {"_parameter_errors": errors}

    findings: list[dict] = []
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        grouped.setdefault(text(row.get("symbol")).strip(), []).append(row)
    mismatch_hit = False
    for symbol, items in grouped.items():
        items.sort(key=lambda x: x.get("date", ""))
        for prev, cur in zip(items, items[1:], strict=False):
            p0, p1 = number(prev.get("close")), number(cur.get("close"))
            a0, a1 = number(prev.get("adj_close")), number(cur.get("adj_close"))
            split = number(cur.get("split_factor"), 1.0)
            dividend = number(cur.get("cash_dividend"))
            reasons: list[str] = []
            if p0 and abs(p1 / p0 - 1) > jump_threshold and split == 1 and dividend == 0:
                reasons.append("large_unexplained_raw_price_jump")
            raw_ret = (p1 * split + dividend) / p0 - 1 if p0 else 0
            adj_ret = a1 / a0 - 1 if a0 else 0
            if check_return_mismatch and abs(raw_ret - adj_ret) > return_tolerance:
                reasons.append("adjusted_return_mismatch")
                mismatch_hit = True
            if reasons:
                findings.append({
                    "symbol": symbol,
                    "date": cur.get("date"),
                    "reasons": reasons,
                    "raw_total_return": raw_ret,
                    "adjusted_return": adj_ret,
                })
    for i, extra in enumerate(extra_findings or [], 1):
        findings.append(_normalize_finding(extra, i, "adapter"))
    return {
        "rows": len(rows),
        "symbols": len(grouped),
        "findings": findings,
        "passed": not mismatch_hit and not any(
            f.get("severity") in ("critical", "high") for f in (extra_findings or [])
        ),
        "_assumptions": {
            "return_tolerance": return_tolerance,
            "jump_threshold": jump_threshold,
            "split_factor_convention": "new shares per old share on the current row",
            "cash_dividend_timing": "cash dividend belongs to the current row ex-date",
            "check_return_mismatch": check_return_mismatch,
        },
        "_limitations": [
            "The executable check covers cash dividends, splits/reverse splits, raw close and adjusted close.",
            "Rights issues, spin-offs, mergers, symbol changes and share-count adjustments require additional event fields and remain unverified.",
            "Vendor adjustment-factor direction must be mapped to the stated split convention before use.",
        ],
        "_next_actions": [
            "Reconcile each mismatch against an authoritative event ledger and vendor adjustment-factor history.",
            "Treat complex events outside the input schema as insufficient evidence, not as a pass.",
        ],
    }


def build_report(result: dict) -> dict:
    evidence_issues = list(_INPUT_ISSUES)
    parameter_errors = result.get("_parameter_errors", [])
    if parameter_errors:
        evidence_issues.extend(parameter_errors if isinstance(parameter_errors, list) else [parameter_errors])

    issue_keys = ("findings", "violations", "warnings", "flags", "timing_findings")
    findings: list[dict] = []
    if evidence_issues:
        findings.extend(_normalize_finding(item, index, "insufficient-evidence") for index, item in enumerate(evidence_issues, 1))
    else:
        for key in issue_keys:
            value = result.get(key)
            if value in (None, "", [], {}):
                continue
            values = value if isinstance(value, list) else [value]
            findings.extend(_normalize_finding(item, index, key) for index, item in enumerate(values, 1))
    passed = result.get("passed")
    if evidence_issues:
        status = "insufficient-evidence"
    elif passed is False:
        status = "fail"
    elif findings:
        status = "warning"
    else:
        status = "pass"

    count_keys = ("rows", "records", "orders", "events", "quotes", "symbols", "simulations", "baseline_count", "current_count")
    input_summary = {key: result[key] for key in count_keys if key in result}
    metrics = {
        key: value for key, value in result.items()
        if not key.startswith("_") and not isinstance(value, (list, dict)) and key != "passed"
    }
    domain_result: dict = {"analysis_skipped": True} if evidence_issues else result
    report = {
        "status": status,
        "input_summary": input_summary,
        "assumptions": result.get("_assumptions", {"event_scope": "not supplied"}),
        "metrics": metrics,
        "findings": findings,
        "limitations": result.get("_limitations", [
            "This script covers split and cash-dividend consistency only."
        ]),
        "next_actions": ["Supply valid required fields or parameters and rerun."] if evidence_issues else (
            result.get("_next_actions", ["Reconcile flagged dates against an authoritative corporate-action ledger."])
            if findings else []
        ),
        "domain_result": domain_result,
    }
    return _json_safe(report)  # type: ignore[return-value]


def emit(result: dict, out: str | None) -> None:
    report = build_report(result)
    payload = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    if out:
        Path(out).write_text(payload + "\n", encoding="utf-8")
        n = len(report.get("findings", []))
        print(f"[audit_adjustments] status={report['status']} findings={n} -> {out}")
    else:
        print(payload)


# ------------------------------------------------------- QuantDB 数据装配层

def _pd():
    try:
        import pandas as pd  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "--quantdb 模式需要 pandas/pyarrow。请在 quantmind 容器内运行：\n"
            "  docker cp <本脚本> quantmind:/tmp/ && docker exec -w /app quantmind python3 /tmp/audit_adjustments.py --quantdb ..."
        ) from exc
    return pd


def resolve_data_root() -> str:
    env = os.environ.get("QM_DATA_ROOT", "").strip()
    candidates = ([env] if env else []) + [
        "/data",
        "/quantmind/data",
        str(Path.home() / "projects" / "quantmind" / "data"),
    ]
    for cand in candidates:
        if os.path.isdir(os.path.join(cand, "quantdb", "1_kline_data")):
            return cand
    raise SystemExit("未找到 QuantDB 数据根（含 quantdb/1_kline_data 的目录）：设置 QM_DATA_ROOT / 确认 /data 挂载")


def _norm_range(start: str, end: str) -> tuple[str, str]:
    s = re.sub(r"\D", "", start)
    e = re.sub(r"\D", "", end)
    if len(s) != 8 or len(e) != 8:
        raise SystemExit("--start/--end 需为 YYYY-MM-DD 或 YYYYMMDD")
    return s, e


def _read_kline(base: str, symbol: str, start_s: str, end_s: str) -> tuple[object, dict]:
    """读取 [start,end] 窗口内单标的收盘序列；按 (time) 去重（多来源重复行保留最新写入）。"""
    pd = _pd()
    dts = sorted(d[3:] for d in os.listdir(base) if d.startswith("dt=") and d[3:].isdigit() and len(d[3:]) == 8)
    frames: list = []
    sources: set = set()
    first_cols: set | None = None
    for ds in dts:
        if ds < start_s or ds > end_s:
            continue
        path = os.path.join(base, f"dt={ds}", "data.parquet")
        if not os.path.exists(path):
            continue
        if first_cols is None:
            first_cols = set(pd.read_parquet(path).columns)
        cols = [c for c in ("symbol", "time", "close", "release_id", "published_at") if c in first_cols]
        df = pd.read_parquet(path, columns=cols)
        df = df[df["symbol"] == symbol]
        if not len(df):
            continue
        if "release_id" in df.columns:
            sources |= set(df["release_id"].dropna().astype(str).unique())
        frames.append(df)
    if not frames:
        return None, {"rows": 0, "dups_dropped": 0, "sources": []}
    df = pd.concat(frames, ignore_index=True)
    df["time"] = pd.to_datetime(df["time"])
    dup = int(df.duplicated(subset=["time"], keep=False).sum())
    sort_cols = ["time", "published_at"] if "published_at" in df.columns else ["time"]
    df = df.sort_values(sort_cols, kind="stable").drop_duplicates(subset=["time"], keep="last")
    return df.reset_index(drop=True), {"rows": len(df), "dups_dropped": dup, "sources": sorted(sources)}


def _load_events_pairs(path: str, date_col: str, value_cols: dict[str, str], start_ts, end_ts) -> dict:
    """通用事件表读取：返回 {Timestamp: {别名: 值}}。value_cols: {别名: 列名}。"""
    pd = _pd()
    if not os.path.exists(path):
        return {}
    df = pd.read_parquet(path)
    if date_col not in df.columns:
        return {}
    df[date_col] = pd.to_datetime(df[date_col])
    df = df[(df[date_col] >= start_ts) & (df[date_col] <= end_ts)]
    out: dict = {}
    for _, row in df.iterrows():
        out[pd.Timestamp(row[date_col])] = {
            alias: (float(row[col]) if row.get(col) is not None and str(row.get(col)) != "nan" else 0.0)
            for alias, col in value_cols.items() if col in df.columns
        }
    return out


def _rows_from_frame(pd, frame, symbol: str, split_map: dict, div_map: dict) -> list[dict]:
    rows = []
    for _, r in frame.iterrows():
        ts = r["time"]
        split = float(split_map.get(ts, {}).get("split", 1.0)) or 1.0
        div = float(div_map.get(ts, {}).get("div", 0.0)) or 0.0
        close = float(r["close"])
        adj = float(r["adj_close"]) if "adj_close" in frame.columns else close
        rows.append({
            "symbol": symbol,
            "date": ts.strftime("%Y-%m-%d"),
            "close": close,
            "adj_close": adj,
            "split_factor": split,
            "cash_dividend": div,
        })
    return rows


def _split_align_findings(pd, frame, symbol: str, split_map: dict, tol: float) -> list[dict]:
    """拆股/送股日对齐检查：c_t × ratio / c_{t-1} 应 ≈ 1（容忍当日真实波动）。"""
    findings = []
    times = list(frame["time"])
    index = {t: i for i, t in enumerate(times)}
    for ts, meta in split_map.items():
        ratio = float(meta.get("split", 1.0))
        if ratio == 1.0:
            continue
        i = index.get(ts)
        if i is None or i == 0:
            findings.append({
                "id": "split-date-not-in-kline", "severity": "info",
                "evidence": {"symbol": symbol, "date": str(ts)[:10], "ratio": ratio},
                "impact": "拆股日不在 K 线窗内，无法核对价格对齐。",
                "recommended_fix": "确认 K 线覆盖范围或拆股日口径。",
            })
            continue
        c0 = float(frame.iloc[i - 1]["close"])
        c1 = float(frame.iloc[i]["close"])
        if c0 <= 0:
            continue
        observed = c1 * ratio / c0
        if abs(observed - 1) > tol:
            findings.append({
                "id": "split-price-misaligned", "severity": "high",
                "evidence": {"symbol": symbol, "date": str(ts)[:10], "ratio": ratio,
                             "prev_close": c0, "close": c1, "observed_factor": round(observed, 6)},
                "impact": "拆股/送股事件与价格序列不一致：价格可能已提前复权（双重调整）或漏调整。",
                "recommended_fix": "对照事件表与行情源，确认该日应为原始价还是已调整价，修复后重跑。",
            })
    return findings


def load_quantdb(market: str, symbols: list[str], start: str, end: str) -> tuple[list[dict], list[dict]]:
    pd = _pd()
    root = resolve_data_root()
    start_s, end_s = _norm_range(start, end)
    start_ts, end_ts = pd.Timestamp(start_s), pd.Timestamp(end_s)
    rows: list[dict] = []
    extras: list[dict] = []
    market = market.upper()

    for sym in symbols:
        if market == "CN":
            raw, info_r = _read_kline(f"{root}/quantdb/1_kline_data/daily_unadjusted", sym, start_s, end_s)
            adj, info_a = _read_kline(f"{root}/quantdb/1_kline_data/daily_forward", sym, start_s, end_s)
            if raw is None or adj is None or raw.empty or adj.empty:
                extras.append({"id": "no-kline", "severity": "high",
                               "evidence": {"symbol": sym, "market": market},
                               "impact": "该窗口内无原始/前复权 K 线，任何结论都不可给出。",
                               "recommended_fix": "确认代码格式（后缀式 000001.SZ）与同步状态。"})
                continue
            frame = raw[["time", "close"]].merge(adj[["time", "close"]], on="time", suffixes=("", "_adj"))
            frame = frame.rename(columns={"close_adj": "adj_close"})
            ev_path = f"{root}/quantdb/3_financial_data/dividend_factors/{sym}.parquet"
            split_map: dict = {}
            div_map: dict = {}
            if os.path.exists(ev_path):
                ev = pd.read_parquet(ev_path)
                ev["time"] = pd.to_datetime(ev["time"])
                ev = ev[(ev["time"] >= start_ts) & (ev["time"] <= end_ts)]
                kline_times = set(frame["time"])
                for _, e in ev.iterrows():
                    ts = pd.Timestamp(e["time"])
                    if ts not in kline_times:
                        extras.append({"id": "event-date-not-in-kline", "severity": "info",
                                       "evidence": {"symbol": sym, "date": str(ts)[:10]},
                                       "impact": "分红送转事件日不在 K 线中，该事件未参与核对。",
                                       "recommended_fix": "核对事件日口径（除权除息日 vs 公告日）。"})
                        continue
                    bonus = float(e.get("stockBonus") or 0.0)
                    split_map[ts] = {"split": 1.0 + bonus / 10.0}
                    div_map[ts] = {"div": float(e.get("interest") or 0.0) / 10.0}
                    if float(e.get("allotment") or 0.0) > 0:
                        extras.append({"id": "rights-issue-not-modeled", "severity": "medium",
                                       "evidence": {"symbol": sym, "date": str(ts)[:10],
                                                    "allotment_per10": float(e.get("allotment") or 0.0),
                                                    "allot_price": float(e.get("allotPrice") or 0.0)},
                                       "impact": "配股事件未建模，该日原始总收益等式与复权收益可能不符（属已知缺口而非数据错误）。",
                                       "recommended_fix": "按 (close×10 + 配股价×配股数)/(10+送转+配股) 单独测算理论除权价。"})
            else:
                extras.append({"id": "no-dividend-factors", "severity": "high",
                               "evidence": {"symbol": sym, "path": ev_path},
                               "impact": "缺少分红送转事件表，原始总收益无法重建，等式核对对该标的失效。",
                               "recommended_fix": "先同步 3_financial_data/dividend_factors，再重跑。"})
            rows.extend(_rows_from_frame(pd, frame, sym, split_map, div_map))

        elif market in ("HK", "US"):
            prefix = "quanthk" if market == "HK" else "quantus"
            raw, info = _read_kline(f"{root}/{prefix}/1_kline_data/daily_forward", sym, start_s, end_s)
            if raw is None or raw.empty:
                extras.append({"id": "no-kline", "severity": "high",
                               "evidence": {"symbol": sym, "market": market},
                               "impact": "该窗口内无 K 线，任何结论都不可给出。",
                               "recommended_fix": "确认代码格式（HK=0001.HK，US=NVDA）与同步状态。"})
                continue
            if info["dups_dropped"] > 0:
                extras.append({"id": "duplicate-source-rows", "severity": "medium",
                               "evidence": {"symbol": sym, "dups_dropped": info["dups_dropped"],
                                            "rows_kept": info["rows"], "sources": info["sources"]},
                               "impact": "同一(标的,日期)存在多来源重复行（读侧未去重时会重复计数）；本审计已保留最新写入行。",
                               "recommended_fix": "确认消费方去重口径；必要时对分区做去重重写。"})
            split_map = _load_events_pairs(
                f"{root}/{prefix}/3_financial_data/splits/{sym}.parquet",
                "trade_date", {"split": "split_ratio"}, start_ts, end_ts)
            div_map = _load_events_pairs(
                f"{root}/{prefix}/3_financial_data/dividend/{sym}.parquet",
                "trade_date", {"div": "dividend"}, start_ts, end_ts)
            if not split_map and not div_map:
                extras.append({"id": "no-event-tables", "severity": "info",
                               "evidence": {"symbol": sym, "market": market},
                               "impact": "无拆股/分红事件表（或窗口内无事件），事件核对对该标的不可用。",
                               "recommended_fix": "核对 3_financial_data/{splits,dividend} 同步情况。"})
            frame = raw.rename(columns={"close": "close"})
            rows.extend(_rows_from_frame(pd, frame, sym, split_map, div_map))
            extras.extend(_split_align_findings(pd, frame, sym, split_map, SPLIT_ALIGN_TOL))

            if market == "HK":
                af_path = f"{root}/quanthk/2_base_sector/adjust_factors/{sym}.parquet"
                if os.path.exists(af_path):
                    af = pd.read_parquet(af_path)
                    af["time"] = pd.to_datetime(af["time"])
                    win = af[(af["time"] >= start_ts) & (af["time"] <= end_ts)].reset_index(drop=True)
                    if len(win) > 1:
                        f_ratio = win["adj_factor"] / win["adj_factor"].shift(1)
                        mism = int(((f_ratio - win["adj_step"]).abs() > 1e-6).sum())
                        if mism:
                            extras.append({"id": "adjust-factor-step-break", "severity": "medium",
                                           "evidence": {"symbol": sym, "mismatch_rows": mism},
                                           "impact": "复权因子与逐日步进不一致（因子断点类缺陷）。",
                                           "recommended_fix": "定位断点日期，用事件表重建该段因子。"})
                    overlap = frame.merge(win[["time", "close_raw"]], on="time")
                    if len(overlap):
                        max_diff = float((overlap["close"] - overlap["close_raw"]).abs().max())
                        if max_diff > 0.005 * max(float(overlap["close"].abs().max()), 1.0):
                            extras.append({"id": "quote-vs-factor-close-mismatch", "severity": "low",
                                           "evidence": {"symbol": sym, "max_abs_diff": max_diff},
                                           "impact": "行情收盘与复权因子文件原始收盘不一致。",
                                           "recommended_fix": "核对两数据集的拉取时间与源。"})
                        if win["time"].max() < end_ts:
                            extras.append({"id": "adjust-factor-coverage", "severity": "info",
                                           "evidence": {"symbol": sym, "factor_last_date": str(win["time"].max())[:10],
                                                        "window_end": str(end_ts)[:10]},
                                           "impact": "复权因子覆盖止于窗口中途（本数据集 2026 年以来未随分红更新），其后事件的复权口径不可核。",
                                           "recommended_fix": "需要时先修复/重建 adjust_factors 管线。"})
        else:
            raise SystemExit(f"不支持的市场: {market}（可选 CN/HK/US）")

    return rows, extras


# -------------------------------------------------------------------- CLI

def main() -> None:
    parser = argparse.ArgumentParser(description="Audit corporate-action price adjustments.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input")
    source.add_argument("--demo", action="store_true")
    source.add_argument("--quantdb", action="store_true")
    parser.add_argument("--market", help="CN / HK / US（--quantdb 时必填）")
    parser.add_argument("--symbols", help="逗号分隔，后缀式：000001.SZ / 0001.HK / NVDA（--quantdb 时必填）")
    parser.add_argument("--start", help="窗口起点 YYYY-MM-DD（--quantdb 时必填）")
    parser.add_argument("--end", help="窗口终点 YYYY-MM-DD（--quantdb 时必填）")
    parser.add_argument("--out")
    parser.add_argument("--return-tolerance", type=float, default=0.02)
    parser.add_argument("--jump-threshold", type=float, default=0.40)
    args = parser.parse_args()

    if args.quantdb:
        if not (args.market and args.symbols and args.start and args.end):
            parser.error("--quantdb 需要 --market --symbols --start --end")
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
        rows, extras = load_quantdb(args.market, symbols, args.start, args.end)
        mismatch_enabled = args.market.upper() == "CN"
        emit(analyze(rows, args.return_tolerance, args.jump_threshold,
                     check_return_mismatch=mismatch_enabled, extra_findings=extras), args.out)
        return

    emit(analyze(
        load_rows(args.input, DEMO),
        args.return_tolerance,
        args.jump_threshold,
    ), args.out)


if __name__ == "__main__":
    main()
