#!/usr/bin/env python3
"""Brinson 业绩归因（Fachler/BHB 单期 + Carino 多期几何链接）— 离线确定性引擎 + QuantDB 装配层。

来源：quantskills/skill-brinson-performance-attribution（GPL-3.0-only，draft）。
方法论与输出契约保留；数据层由外部数据服务/用户自备改为本地 QuantDB 直读（2026-10-07 标定）：

  US  行业 = quantus/2_base_sector/sector/{SYM}.parquet（yahoo；sector 11 类 / industry 更细）；
      基准（缺省）= 行业映射全集等权。
  HK  sector/ 目录 sector 列全空（2818/2818，不可用）；改用 quanthk/2_base_sector/akshare_profile/
      {SYM}.parquet 的「所属行业」（31 类，2784/2784 非空）。
  CN  行业 = quantdb/2_base_sector/instrument_detail/instrument_detail.parquet 的 rs_hyname
      （128 类，5536/5536 非空；静态快照，HqDate=20260720）。
  个股区间收益 = 窗口内首/末有效收盘之比 − 1；CN daily_forward=前复权（含分红再投），
  HK/US daily_forward=不复权（价格收益，不含分红）。daily_backward 损坏，禁止用于收益。

仅 --demo / --input 为纯标准库，可在宿主机或 dsh 直接跑；--quantdb 需要 pandas/pyarrow
（在 quantmind 容器内运行）。

用法：
  python3 brinson.py --demo
  python3 brinson.py --input attribution.csv --method fachler --out report.json
  python3 brinson.py --input multiperiod.csv --method bhb          # 含 period 列 → Carino 链接
  python3 brinson.py --quantdb --market US --portfolio pf.csv --start 2026-04-01 --end 2026-09-30
  python3 brinson.py --quantdb --market US --portfolio pf.csv --periods 2026-04-01:2026-06-30,2026-07-01:2026-09-30
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
from pathlib import Path

# ================================================================ 常量与契约

REQUIRED_COLUMNS = ("sector", "w_p", "w_b", "r_p", "r_b")
NUMERIC_COLUMNS = ("w_p", "w_b", "r_p", "r_b")
PERIOD_COLUMN = "period"
RESIDUAL_TOL = 1e-4          # 1bp：源契约门禁（主动收益 vs 三效应和）
DEFAULT_WEIGHT_TOL = 0.02    # 源契约：权重和 1±0.02
CARINO_EPS = 1e-12

MARKET_KLINE_DIR = {
    "CN": "quantdb/1_kline_data/daily_forward",    # 前复权（含分红再投）
    "HK": "quanthk/1_kline_data/daily_forward",    # 不复权（价格收益）
    "US": "quantus/1_kline_data/daily_forward",    # 不复权（价格收益）
}

INDUSTRY_SOURCES = {
    "US": {
        "kind": "dir",
        "path": "quantus/2_base_sector/sector",
        "columns": {"sector": "sector", "industry": "industry"},
        "note": "yahoo 行业分类；sector=11 类 GICS 风格，industry=更细粒度",
    },
    "HK": {
        "kind": "dir",
        "path": "quanthk/2_base_sector/akshare_profile",
        "columns": {"sector": "所属行业"},
        "note": "sector/ 目录 sector 列全空（2818/2818）不可用，回退 akshare_profile",
    },
    "CN": {
        "kind": "file",
        "path": "quantdb/2_base_sector/instrument_detail/instrument_detail.parquet",
        "columns": {"sector": "rs_hyname"},
        "note": "通达信行业（128 类）；instrument_detail 为静态快照，HqDate=20260720",
    },
}

NOTES = [
    "Fachler 配置效应 = (w_p−w_b)·(r_b−R_b)；BHB 配置效应 = (w_p−w_b)·r_b。",
    "选股效应 = w_b·(r_p−r_b)；交互效应 = (w_p−w_b)·(r_p−r_b)。",
    "HHI（赫芬达尔）= Σw²（行业级权重，非个股级）；越大越集中。",
    "残差 = 主动收益 − 三效应之和；权重和恰为 1 时恒等成立（数值精度内 ≈0）。",
]

LIMITATIONS_BASE = [
    "归因是事后解释而非预测；分类口径变化、现金、费用、衍生品与期内交易都可能产生残差。",
    "组合与基准必须使用一致的行业分类、计价口径与评估区间，跨口径读数无效。",
    "只做行业维度的效应分解；个股权重由调用方给出，脚本不推断持仓。",
]

# 校验问题的用户可读文案（_normalize_finding 按 reason 查表）
ISSUE_TEXT = {
    "missing_columns": ("输入 CSV 缺少必需列，无法做归因。", "按契约提供 sector,w_p,w_b,r_p,r_b（可选 period 列）后重跑。"),
    "invalid_numeric": ("存在非有限/非数值字段，该行不参与计算。", "修正为有效数值后重跑。"),
    "empty_sector": ("存在空行业名，该行被剔除。", "补齐行业名后重跑。"),
    "duplicate_sectors": ("同一期内行业重复：每行业一行的契约被破坏，分解不可定义。", "先按行业聚合为一行再重跑。"),
    "weights_not_sum_one": ("权重和偏离 1 超出容差，主动收益恒等式不成立。", "修正权重，或显式调整 --weight-tolerance（源契约默认 0.02）。"),
    "empty_input": ("输入为空。", "提供至少一行数据。"),
    "file_unreadable": ("文件不可读。", "确认路径与编码（UTF-8，可带 BOM）。"),
    "invalid_weight_tolerance": ("--weight-tolerance 非法。", "提供非负有限数值。"),
    "empty_industry_map": ("行业映射为空，无法把标的归入行业。", "确认对应市场的 2_base_sector 数据已同步。"),
    "portfolio_empty_after_filter": ("组合标的经行业/行情过滤后为空，无法计算组合侧。", "核对 symbol 格式与窗口内行情覆盖。"),
    "benchmark_empty_after_filter": ("基准标的经过滤后为空，无法计算基准侧。", "核对 symbol 格式与窗口内行情覆盖。"),
    "portfolio_weights_out_of_tolerance": ("组合权重和偏离 1 超出容差，拒绝自动归一化。", "修正权重文件，或显式调整 --weight-tolerance。"),
    "benchmark_weights_out_of_tolerance": ("基准权重和偏离 1 超出容差，拒绝自动归一化。", "修正权重文件，或显式调整 --weight-tolerance。"),
    "empty_weights_file": ("权重文件为空或没有有效行。", "提供 symbol,weight 两列且至少一行有效数据。"),
    "duplicate_symbol": ("权重文件内同一标的出现多次。", "合并为一行后重跑。"),
    "invalid_weight": ("权重非正数或非数值。", "权重必须为 >0 的有限数（脚本会按容差归一化）。"),
    "no_data_in_window": ("窗口内没有可用行情，任何结论都不可给出。", "确认日期区间与数据同步状态。"),
}

# ========================================================== 离线确定性引擎


def _number(value: object) -> float | None:
    """宽松数值解析；非有限值返回 None（由调用方登记问题）。"""
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _std(values: list[float]) -> float:
    n = len(values)
    if n == 0:
        return 0.0
    mean = sum(values) / n
    return math.sqrt(sum((v - mean) ** 2 for v in values) / n)


def demo_rows() -> list[dict]:
    """源 run_demo.py CASE B 三期数据（配置倾斜 / 纯选股 / 收益上移），逐字复刻。"""
    alloc = [
        {"sector": "Tech", "w_p": 0.40, "w_b": 0.20, "r_p": 0.08, "r_b": 0.08},
        {"sector": "Banks", "w_p": 0.20, "w_b": 0.30, "r_p": 0.02, "r_b": 0.02},
        {"sector": "Energy", "w_p": 0.20, "w_b": 0.25, "r_p": 0.01, "r_b": 0.01},
        {"sector": "Consumer", "w_p": 0.20, "w_b": 0.25, "r_p": 0.03, "r_b": 0.03},
    ]
    select = [
        {"sector": "Tech", "w_p": 0.25, "w_b": 0.25, "r_p": 0.10, "r_b": 0.05},
        {"sector": "Banks", "w_p": 0.25, "w_b": 0.25, "r_p": 0.04, "r_b": 0.03},
        {"sector": "Energy", "w_p": 0.25, "w_b": 0.25, "r_p": 0.02, "r_b": 0.02},
        {"sector": "Consumer", "w_p": 0.25, "w_b": 0.25, "r_p": 0.05, "r_b": 0.04},
    ]
    shifted = [
        dict(r, r_p=r["r_p"] + 0.01, r_b=r["r_b"] + 0.005) for r in alloc
    ]
    rows: list[dict] = []
    for label, items in (("P1-配置倾斜", alloc), ("P2-纯选股", select), ("P3-收益上移", shifted)):
        for item in items:
            rows.append({**item, PERIOD_COLUMN: label})
    return rows


def load_csv_rows(path: str) -> tuple[list[dict], list[dict]]:
    """读取 CSV；表头统一小写去空格。返回 (rows, issues)。"""
    issues: list[dict] = []
    try:
        with open(path, encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            try:
                header = next(reader)
            except StopIteration:
                return [], [{"reason": "empty_input", "path": path}]
            keys = [h.strip().lower() for h in header]
            rows = [
                dict(zip(keys, values, strict=False))
                for values in reader
                if values and any(v.strip() != "" for v in values)
            ]
    except OSError as exc:
        return [], [{"reason": "file_unreadable", "path": path, "error": str(exc)}]
    missing = [c for c in REQUIRED_COLUMNS if c not in keys]
    if missing:
        issues.append({
            "reason": "missing_columns",
            "columns": missing,
            "required": list(REQUIRED_COLUMNS),
        })
        return [], issues
    if not rows:
        issues.append({"reason": "empty_input", "path": path})
    return rows, issues


def validate_sector_rows(
    rows: list[dict], weight_tolerance: float = DEFAULT_WEIGHT_TOL
) -> tuple[list[dict], list[tuple[str, list[dict]]]]:
    """校验并分组行业行：sector 唯一、数值有限、权重和 1±tol。返回 (issues, [(period, rows)])。"""
    issues: list[dict] = []
    if not math.isfinite(weight_tolerance) or weight_tolerance < 0:
        issues.append({"reason": "invalid_weight_tolerance", "value": weight_tolerance})
        return issues, []
    if not rows:
        issues.append({"reason": "empty_input"})
        return issues, []

    grouped: dict[str, list[dict]] = {}
    order: list[str] = []
    for row_number, row in enumerate(rows, 2):
        label = str(row.get(PERIOD_COLUMN, "_single")).strip() or "_single"
        if label not in grouped:
            grouped[label] = []
            order.append(label)
        sector = str(row.get("sector", "") or "").strip()
        if not sector:
            issues.append({"reason": "empty_sector", "row": row_number, "period": label})
            continue
        values: dict[str, float] = {}
        for column in NUMERIC_COLUMNS:
            parsed = _number(row.get(column))
            if parsed is None:
                issues.append({
                    "reason": "invalid_numeric",
                    "row": row_number,
                    "period": label,
                    "column": column,
                    "value": row.get(column),
                })
            else:
                values[column] = parsed
        if len(values) == len(NUMERIC_COLUMNS):
            grouped[label].append({"sector": sector, **values})

    for label in order:
        items = grouped[label]
        seen: set[str] = set()
        duplicates: list[str] = []
        for item in items:
            if item["sector"] in seen:
                duplicates.append(item["sector"])
            seen.add(item["sector"])
        if duplicates:
            issues.append({
                "reason": "duplicate_sectors",
                "period": label,
                "sectors": sorted(set(duplicates)),
            })
        if items:
            sum_wp = sum(it["w_p"] for it in items)
            sum_wb = sum(it["w_b"] for it in items)
            if abs(sum_wp - 1.0) > weight_tolerance or abs(sum_wb - 1.0) > weight_tolerance:
                issues.append({
                    "reason": "weights_not_sum_one",
                    "period": label,
                    "sum_w_p": sum_wp,
                    "sum_w_b": sum_wb,
                    "tolerance": weight_tolerance,
                })
    return issues, [(label, grouped[label]) for label in order if grouped[label]]


def single_period(sector_rows: list[dict], method: str = "fachler") -> dict:
    """单期 Brinson 分解（Fachler 或 BHB），含残差核对 / HHI / 贡献排序 / 质量门禁。"""
    method = method.lower()
    if method not in {"fachler", "bhb"}:
        raise ValueError("method must be 'fachler' or 'bhb'")

    r_b_total = sum(row["w_b"] * row["r_b"] for row in sector_rows)
    r_p_total = sum(row["w_p"] * row["r_p"] for row in sector_rows)

    sectors: list[dict] = []
    alloc = sel = inter = 0.0
    for row in sector_rows:
        w_p, w_b, r_p, r_b = row["w_p"], row["w_b"], row["r_p"], row["r_b"]
        if method == "fachler":
            a = (w_p - w_b) * (r_b - r_b_total)
        else:
            a = (w_p - w_b) * r_b
        s = w_b * (r_p - r_b)
        i = (w_p - w_b) * (r_p - r_b)
        sectors.append({
            "sector": row["sector"],
            "w_p": float(w_p),
            "w_b": float(w_b),
            "r_p": float(r_p),
            "r_b": float(r_b),
            "allocation": float(a),
            "selection": float(s),
            "interaction": float(i),
            "total": float(a + s + i),
            "weight_active": float(w_p - w_b),
        })
        alloc += a
        sel += s
        inter += i

    active = r_p_total - r_b_total
    explained = alloc + sel + inter
    residual = active - explained
    hhi_p = sum(row["w_p"] ** 2 for row in sector_rows)
    hhi_b = sum(row["w_b"] ** 2 for row in sector_rows)
    top = sorted(sectors, key=lambda x: abs(x["total"]), reverse=True)[:3]
    top_names = [f"{s['sector']}:{s['total']:.4%}" for s in top]

    drivers = {"ALLOCATION": abs(alloc), "SELECTION": abs(sel), "INTERACTION": abs(inter)}
    verdict = max(drivers, key=drivers.get)
    sum_wp = sum(row["w_p"] for row in sector_rows)
    sum_wb = sum(row["w_b"] for row in sector_rows)
    gates = {
        "weights_sum_near_1": bool(abs(sum_wp - 1) < 0.02 and abs(sum_wb - 1) < 0.02),
        "abs_residual<1bp": bool(abs(residual) < RESIDUAL_TOL),
        "active_return_explained": bool(abs(explained - active) < RESIDUAL_TOL),
        "has_dispersion": bool(
            _std([row["r_b"] for row in sector_rows]) > 0
            or _std([row["r_p"] for row in sector_rows]) > 0
        ),
    }
    return {
        "method": method,
        "portfolio_return": float(r_p_total),
        "benchmark_return": float(r_b_total),
        "active_return": float(active),
        "allocation": float(alloc),
        "selection": float(sel),
        "interaction": float(inter),
        "residual": float(residual),
        "herfindahl_portfolio": float(hhi_p),
        "herfindahl_benchmark": float(hhi_b),
        "top_contributors": top_names,
        "verdict": verdict,
        "score": float(sum(gates.values()) / len(gates)),
        "gates": gates,
        "sectors": sectors,
        "linked": None,
        "periods": None,
        "notes": list(NOTES),
    }


def carino_link(period_reports: list[dict]) -> dict:
    """Carino (1999) 平滑：把逐期算术效应链接到几何主动收益。"""
    if not period_reports:
        raise ValueError("period_reports empty")
    rp = [r["portfolio_return"] for r in period_reports]
    rb = [r["benchmark_return"] for r in period_reports]
    if any(x <= -1 for x in rp + rb):
        raise ValueError("Carino linking requires all period returns > -100%")
    rp_g = math.prod(1 + x for x in rp) - 1
    rb_g = math.prod(1 + x for x in rb) - 1
    active_g = rp_g - rb_g

    factors: list[float] = []
    for x, y in zip(rp, rb, strict=False):
        if abs(x - y) < CARINO_EPS:
            factors.append(1.0 / (1.0 + y))  # r_p→r_b 极限：d ln(1+r)/dr = 1/(1+r_b)
        else:
            factors.append(math.log((1 + x) / (1 + y)) / (x - y))
    active_arith = [x - y for x, y in zip(rp, rb, strict=False)]
    denominator = sum(f * a for f, a in zip(factors, active_arith, strict=False))
    scale = active_g / denominator if abs(denominator) > CARINO_EPS else 1.0
    c_factors = [f * scale for f in factors]

    linked: dict = {
        "portfolio_return_geometric": float(rp_g),
        "benchmark_return_geometric": float(rb_g),
        "active_return_geometric": float(active_g),
        "active_return_arithmetic_sum": float(sum(active_arith)),
        "carino_factors": [float(c) for c in c_factors],
    }
    for key in ("allocation", "selection", "interaction"):
        values = [r[key] for r in period_reports]
        linked[f"{key}_linked"] = float(sum(c * v for c, v in zip(c_factors, values, strict=False)))
    linked["residual_linked"] = float(
        active_g
        - linked["allocation_linked"]
        - linked["selection_linked"]
        - linked["interaction_linked"]
    )
    return linked


def multiperiod(period_groups: list[tuple[str, list[dict]]], method: str = "fachler") -> dict:
    """多期：逐期单期分解 + Carino 链接；headline 为链接后几何值，行业明细取最后一期。"""
    reports = [single_period(rows, method=method) for _, rows in period_groups]
    base = reports[-1]
    linked = carino_link(reports)

    domain = dict(base)
    domain["method"] = f"{method}+carino"
    domain["portfolio_return"] = linked["portfolio_return_geometric"]
    domain["benchmark_return"] = linked["benchmark_return_geometric"]
    domain["active_return"] = linked["active_return_geometric"]
    domain["allocation"] = linked["allocation_linked"]
    domain["selection"] = linked["selection_linked"]
    domain["interaction"] = linked["interaction_linked"]
    domain["residual"] = linked["residual_linked"]
    domain["gates"] = {
        **base["gates"],
        "abs_residual_linked<1bp": bool(abs(linked["residual_linked"]) < RESIDUAL_TOL),
    }
    domain["score"] = float(sum(domain["gates"].values()) / len(domain["gates"]))
    domain["linked"] = linked
    domain["period_count"] = len(reports)
    domain["periods"] = [
        {
            "period": label,
            "portfolio_return": report["portfolio_return"],
            "benchmark_return": report["benchmark_return"],
            "active_return": report["active_return"],
            "allocation": report["allocation"],
            "selection": report["selection"],
            "interaction": report["interaction"],
            "residual": report["residual"],
            "carino_factor": linked["carino_factors"][index],
        }
        for index, (label, _) in enumerate(period_groups)
        for report in (reports[index],)
    ]
    drivers = {
        "ALLOCATION": abs(linked["allocation_linked"]),
        "SELECTION": abs(linked["selection_linked"]),
        "INTERACTION": abs(linked["interaction_linked"]),
    }
    domain["verdict"] = max(drivers, key=drivers.get)
    domain["notes"] = list(NOTES) + [
        f"多期：{len(reports)} 期，headline 收益与效应为 Carino 链接后的几何值。",
        "行业明细与 HHI 为最后一期快照（源契约）。",
    ]
    return domain


def render_text(domain: dict) -> str:
    lines = [
        "=== Brinson 业绩归因 ===",
        f"method={domain['method']}",
        f"R_p={domain['portfolio_return']:.4%}  R_b={domain['benchmark_return']:.4%}  "
        f"active={domain['active_return']:.4%}",
        f"allocation={domain['allocation']:.4%}  selection={domain['selection']:.4%}  "
        f"interaction={domain['interaction']:.4%}  residual={domain['residual']:.4%}",
        f"HHI_p={domain['herfindahl_portfolio']:.3f} HHI_b={domain['herfindahl_benchmark']:.3f}",
        f"top_contributors={', '.join(domain['top_contributors'])}",
        f"quality_score={domain['score']:.0%}  dominant_driver={domain['verdict']}",
        "",
        "gates:",
    ]
    for key, value in domain["gates"].items():
        lines.append(f"  [{'PASS' if value else 'FAIL'}] {key}")
    if domain.get("linked"):
        lines += ["", "Carino-linked multi-period:"]
        for key, value in domain["linked"].items():
            if isinstance(value, list):
                lines.append(f"  {key}={[round(v, 6) for v in value]}")
            elif "return" in key or key.endswith("_linked"):
                lines.append(f"  {key}={value:.4%}")
            else:
                lines.append(f"  {key}={value}")
    if domain.get("periods"):
        lines += ["", "period | R_p | R_b | active | alloc | select | interact | resid | carino_c"]
        for period in domain["periods"]:
            lines.append(
                f"{str(period['period'])[:12]:12s} | {period['portfolio_return']:7.4%} | "
                f"{period['benchmark_return']:7.4%} | {period['active_return']:7.4%} | "
                f"{period['allocation']:7.4%} | {period['selection']:7.4%} | "
                f"{period['interaction']:8.4%} | {period['residual']:6.4%} | "
                f"{period['carino_factor']:.4f}"
            )
    lines += ["", "sector | w_act | alloc | select | interact | total"]
    for s in sorted(domain["sectors"], key=lambda x: abs(x["total"]), reverse=True):
        lines.append(
            f"{s['sector'][:14]:14s} | {s['weight_active']:6.2%} | {s['allocation']:7.4%} | "
            f"{s['selection']:7.4%} | {s['interaction']:8.4%} | {s['total']:7.4%}"
        )
    lines += ["", "notes:"]
    lines += [f"- {n}" for n in domain["notes"]]
    return "\n".join(lines)


# ================================================================ 报告封装


def _normalize_finding(item: object, index: int, source: str) -> dict:
    if isinstance(item, dict):
        evidence = item.get("evidence", item)
        severity = item.get("severity", "medium")
        finding_id = item.get("id", f"{source}-{index}")
        reason = evidence.get("reason") if isinstance(evidence, dict) else None
        default_impact, default_fix = ISSUE_TEXT.get(
            reason or "", ("该条件可能影响归因结果的可靠性，请人工确认。", "检查引用记录，修正输入或假设后重跑。")
        )
        impact = item.get("impact", default_impact)
        recommended_fix = item.get("recommended_fix", default_fix)
    else:
        evidence = item
        severity = "medium"
        finding_id = f"{source}-{index}"
        impact = "该条件可能影响归因结果的可靠性。"
        recommended_fix = "复核该条件并记录处理决定，必要时修正后重跑。"
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
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def build_report(
    domain: dict | None,
    issues: list[dict],
    input_summary: dict,
    assumptions: dict,
    adapter_findings: list[dict] | None = None,
    limitations: list[str] | None = None,
) -> dict:
    limitations = list(LIMITATIONS_BASE) + list(limitations or [])
    if issues or domain is None:
        findings = [
            _normalize_finding(item, index, "insufficient-evidence")
            for index, item in enumerate(issues, 1)
        ]
        return _json_safe({
            "status": "insufficient-evidence",
            "input_summary": input_summary,
            "assumptions": assumptions,
            "metrics": {},
            "findings": findings,
            "limitations": limitations,
            "next_actions": ["补齐缺失字段/修正参数（见 findings）后重跑；本报告未做任何归因计算。"],
            "domain_result": {"analysis_skipped": True},
        })  # type: ignore[return-value]

    findings = [
        _normalize_finding(item, index, "adapter")
        for index, item in enumerate(adapter_findings or [], 1)
    ]
    gates = domain.get("gates", {})
    core_gate = "abs_residual_linked<1bp" if "abs_residual_linked<1bp" in gates else "abs_residual<1bp"
    residual_ok = bool(gates.get(core_gate, False))
    other_gate_fail = [key for key, value in gates.items() if not value and key != core_gate]
    severe = any(f.get("severity") in ("critical", "high") for f in findings)
    if not residual_ok:
        status = "fail"
    elif other_gate_fail or severe:
        status = "warning"
    else:
        status = "pass"

    metrics = {
        "method": domain["method"],
        "portfolio_return": domain["portfolio_return"],
        "benchmark_return": domain["benchmark_return"],
        "active_return": domain["active_return"],
        "allocation": domain["allocation"],
        "selection": domain["selection"],
        "interaction": domain["interaction"],
        "residual": domain["residual"],
        "herfindahl_portfolio": domain["herfindahl_portfolio"],
        "herfindahl_benchmark": domain["herfindahl_benchmark"],
        "quality_score": domain["score"],
        "periods": domain.get("period_count", 1),
    }
    next_actions: list[str] = []
    if not residual_ok:
        next_actions.append(
            f"残差超过 1bp（{domain['residual']:.2e}）：先核对输入权重和与数值精度；"
            "CSV 模式下确认为行业级聚合行而非个股行。"
        )
    if other_gate_fail:
        next_actions.append(f"门禁未过：{', '.join(other_gate_fail)}；确认行业维度存在离散度且权重和为 1。")
    if severe:
        next_actions.append("补齐被剔除标的的行业映射/行情数据后重跑，再解读效应分解。")
    return _json_safe({
        "status": status,
        "input_summary": input_summary,
        "assumptions": assumptions,
        "metrics": metrics,
        "findings": findings,
        "limitations": limitations,
        "next_actions": next_actions,
        "domain_result": domain,
    })  # type: ignore[return-value]


def emit(report: dict, out: str | None) -> None:
    payload = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    if out:
        Path(out).write_text(payload + "\n", encoding="utf-8")
        metrics = report.get("metrics") or {}
        residual = metrics.get("residual")
        residual_text = f"{residual:.2e}" if isinstance(residual, float) else "-"
        print(
            f"[brinson] status={report['status']} "
            f"method={metrics.get('method', '-')} residual={residual_text} "
            f"findings={len(report.get('findings', []))} -> {out}"
        )
    else:
        print(payload)


# =========================================================== QuantDB 装配层


def _pd():
    try:
        import pandas as pd  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "--quantdb 模式需要 pandas/pyarrow。请在 quantmind 容器内运行：\n"
            "  docker cp <本脚本> quantmind:/tmp/ && docker exec -w /app quantmind "
            "python3 /tmp/brinson.py --quantdb ..."
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
    raise SystemExit(
        "未找到 QuantDB 数据根（含 quantdb/1_kline_data 的目录）：设置 QM_DATA_ROOT / 确认 /data 挂载"
    )


def _norm_range(start: str, end: str) -> tuple[str, str]:
    s = re.sub(r"\D", "", start or "")
    e = re.sub(r"\D", "", end or "")
    if len(s) != 8 or len(e) != 8:
        raise SystemExit("--start/--end 需为 YYYY-MM-DD 或 YYYYMMDD")
    return s, e


def _label_range(start_s: str, end_s: str) -> str:
    return f"{start_s[:4]}-{start_s[4:6]}-{start_s[6:]}~{end_s[:4]}-{end_s[4:6]}-{end_s[6:]}"


def _norm_symbol(market: str, symbol: str) -> str:
    s = str(symbol).strip()
    if market == "US":
        return s.upper()
    if market == "HK":
        match = re.fullmatch(r"(\d{1,5})\.HK", s, re.IGNORECASE)
        if match:
            return f"{int(match.group(1)):04d}.HK"
        return s.upper()
    return s.upper()  # CN：后缀式 000001.SZ


def load_industry_map(pd, root: str, market: str, level: str) -> tuple[dict[str, str], dict]:
    """加载行业映射：{symbol: 行业名} + 元信息。"""
    spec = INDUSTRY_SOURCES[market]
    column = spec["columns"].get(level) or spec["columns"]["sector"]
    path = os.path.join(root, spec["path"])
    if not os.path.exists(path):
        raise SystemExit(f"行业映射数据缺失: {path}")
    if spec["kind"] == "file":
        frame = pd.read_parquet(path)
        symbol_col = "Symbol" if "Symbol" in frame.columns else "symbol"
        if market == "CN" and "IsQuitGP" in frame.columns:
            frame = frame[frame["IsQuitGP"].astype(str) != "1"]
        sub = frame[[symbol_col, column]].rename(columns={symbol_col: "symbol"})
    else:
        try:
            sub = pd.read_parquet(path, columns=["symbol", column])
        except Exception:
            frames = []
            for name in sorted(os.listdir(path)):
                if not name.endswith(".parquet"):
                    continue
                try:
                    frames.append(pd.read_parquet(os.path.join(path, name), columns=["symbol", column]))
                except Exception:
                    continue
            if not frames:
                raise SystemExit(f"行业映射目录读取失败（schema 漂移？）: {path}") from None
            sub = pd.concat(frames, ignore_index=True)
    mapping: dict[str, str] = {}
    for symbol, industry in zip(sub["symbol"], sub[column], strict=False):
        if symbol is None or (isinstance(symbol, float) and math.isnan(symbol)):
            continue
        key = _norm_symbol(market, str(symbol))
        if pd.isna(industry):
            continue
        value = str(industry).strip()
        if not key or not value or value.lower() == "nan":
            continue
        mapping[key] = value
    meta = {
        "source": path,
        "column": column,
        "level": level,
        "note": spec["note"],
        "symbols": len(mapping),
        "industries": len(set(mapping.values())),
    }
    return mapping, meta


def load_window_returns(
    pd, root: str, market: str, symbols: set[str], start_s: str, end_s: str
) -> tuple[dict[str, dict], dict]:
    """读取 [start,end] 内所有分区，按 (symbol,time) 去重，返回 {symbol: 区间收益+首末日}。"""
    base = os.path.join(root, MARKET_KLINE_DIR[market])
    if not os.path.isdir(base):
        raise SystemExit(f"K 线目录缺失: {base}")
    partitions = sorted(
        d[3:]
        for d in os.listdir(base)
        if d.startswith("dt=") and d[3:].isdigit() and len(d[3:]) == 8
        and start_s <= d[3:] <= end_s
    )
    frames = []
    first_cols: set | None = None
    for ds in partitions:
        path = os.path.join(base, f"dt={ds}", "data.parquet")
        if not os.path.exists(path):
            continue
        if first_cols is None:
            first_cols = set(pd.read_parquet(path).columns)
        cols = [c for c in ("symbol", "time", "close", "published_at") if c in first_cols]
        frame = pd.read_parquet(path, columns=cols)
        frame = frame[frame["symbol"].isin(symbols)]
        if len(frame):
            frames.append(frame)
    meta = {"partitions": len(frames), "dups_dropped": 0, "symbols_with_returns": 0, "window": None}
    if not frames:
        return {}, meta
    frame = pd.concat(frames, ignore_index=True)
    meta["window"] = [partitions[0], partitions[-1]]
    frame["time"] = pd.to_datetime(frame["time"])
    frame = frame.dropna(subset=["close"])
    dups = int(frame.duplicated(subset=["symbol", "time"], keep=False).sum())
    sort_cols = ["symbol", "time"] + (["published_at"] if "published_at" in frame.columns else [])
    frame = frame.sort_values(sort_cols, kind="stable").drop_duplicates(
        subset=["symbol", "time"], keep="last"
    )
    out: dict[str, dict] = {}
    for symbol, group in frame.groupby("symbol", sort=False):
        group = group.sort_values("time")
        first_close = float(group["close"].iloc[0])
        last_close = float(group["close"].iloc[-1])
        if len(group) < 2 or first_close <= 0 or last_close <= 0:
            continue
        out[str(symbol)] = {
            "return": last_close / first_close - 1.0,
            "first": str(group["time"].iloc[0].date()),
            "last": str(group["time"].iloc[-1].date()),
            "obs": int(len(group)),
        }
    meta["dups_dropped"] = dups
    meta["symbols_with_returns"] = len(out)
    return out, meta


def read_weights_csv(path: str, market: str) -> tuple[dict[str, float], list[dict]]:
    """读取组合/基准权重 CSV（symbol,weight）。返回 (weights, issues)。"""
    issues: list[dict] = []
    try:
        with open(path, encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            try:
                header = [h.strip().lower() for h in next(reader)]
            except StopIteration:
                return {}, [{"reason": "empty_weights_file", "path": path}]
            if not {"symbol", "weight"} <= set(header):
                return {}, [{
                    "reason": "missing_columns",
                    "path": path,
                    "required": ["symbol", "weight"],
                }]
            index_symbol = header.index("symbol")
            index_weight = header.index("weight")
            weights: dict[str, float] = {}
            for row_number, values in enumerate(reader, 2):
                if not values or all(v.strip() == "" for v in values):
                    continue
                raw_symbol = values[index_symbol].strip() if index_symbol < len(values) else ""
                raw_weight = values[index_weight].strip() if index_weight < len(values) else ""
                if not raw_symbol:
                    issues.append({"reason": "empty_symbol", "path": path, "row": row_number})
                    continue
                symbol = _norm_symbol(market, raw_symbol)
                weight = _number(raw_weight)
                if weight is None or weight <= 0:
                    issues.append({
                        "reason": "invalid_weight",
                        "path": path,
                        "row": row_number,
                        "symbol": raw_symbol,
                        "value": raw_weight,
                    })
                    continue
                if symbol in weights:
                    issues.append({"reason": "duplicate_symbol", "path": path, "symbol": symbol})
                    continue
                weights[symbol] = weight
    except OSError as exc:
        return {}, [{"reason": "file_unreadable", "path": path, "error": str(exc)}]
    if not weights:
        issues.append({"reason": "empty_weights_file", "path": path})
    return weights, issues


def assemble_sector_rows(
    portfolio: dict[str, float],
    benchmark: dict[str, float] | None,
    industry: dict[str, str],
    returns: dict[str, dict],
    weight_tolerance: float,
    period_label: str | None = None,
    shared_notice: bool = True,
) -> tuple[list[dict], dict, list[dict], list[dict]]:
    """个股级组合/基准 → 行业级 Brinson 输入行。返回 (rows, stats, findings, issues)。"""
    findings: list[dict] = []
    issues: list[dict] = []

    def _filter(weights: dict[str, float], kind: str) -> tuple[dict[str, float], dict[str, list[str]]]:
        kept: dict[str, float] = {}
        dropped: dict[str, list[str]] = {"no_industry": [], "no_window_return": [], "invalid_weight": []}
        for symbol, weight in weights.items():
            if symbol not in industry:
                dropped["no_industry"].append(symbol)
            elif symbol not in returns:
                dropped["no_window_return"].append(symbol)
            elif not (isinstance(weight, (int, float)) and math.isfinite(weight) and weight > 0):
                dropped["invalid_weight"].append(symbol)
            else:
                kept[symbol] = float(weight)
        return kept, dropped

    def _drop_finding(kind: str, reason: str, symbols: list[str]) -> dict:
        severity = {"portfolio": "high", "benchmark": "medium"}[kind]
        if reason == "no_window_return" and kind == "benchmark":
            severity = "info"
        return {
            "id": f"quantdb-{kind}-{reason}",
            "severity": severity,
            "evidence": {
                "period": period_label,
                "symbols": symbols[:20],
                "count": len(symbols),
            },
            "impact": (
                f"{kind} 有 {len(symbols)} 只标的因 {reason} 被剔除，"
                "该部分权重不进入归因（组合权重将在剩余标的内归一化）。"
            ),
            "recommended_fix": "补齐行业映射/行情数据后重跑；组合侧剔除以 findings 记录为准。",
        }

    def _norm_weights(kept: dict[str, float]) -> dict[str, float]:
        total = sum(kept.values())
        return {s: w / total for s, w in kept.items()} if total > 0 else {}

    portfolio_sum_raw = sum(
        w for w in portfolio.values() if isinstance(w, (int, float)) and math.isfinite(w) and w > 0
    )
    portfolio_kept, portfolio_dropped = _filter(portfolio, "portfolio")
    for reason, symbols in portfolio_dropped.items():
        if symbols:
            findings.append(_drop_finding("portfolio", reason, sorted(symbols)))
    if not portfolio_kept:
        issues.append({
            "reason": "portfolio_empty_after_filter",
            "period": period_label,
            "dropped": {k: len(v) for k, v in portfolio_dropped.items()},
        })
    elif abs(portfolio_sum_raw - 1.0) > weight_tolerance:
        issues.append({
            "reason": "portfolio_weights_out_of_tolerance",
            "period": period_label,
            "sum": portfolio_sum_raw,
            "tolerance": weight_tolerance,
        })
    elif abs(portfolio_sum_raw - 1.0) > 1e-9 and shared_notice:
        findings.append({
            "id": "quantdb-portfolio-weights-normalized",
            "severity": "info",
            "evidence": {"period": period_label, "raw_sum": portfolio_sum_raw},
            "impact": "组合权重和不为 1（在容差内），已按剩余标的归一化。",
            "recommended_fix": "如需精确复现组合收益，请直接提供和为 1 的权重。",
        })
    portfolio_norm = _norm_weights(portfolio_kept)

    if benchmark is None:
        benchmark_kept = {s: 1.0 for s in industry if s in returns}
        benchmark_kind = "equal_weight_universe"
        missing_universe = sorted(set(industry) - set(benchmark_kept))
        if missing_universe and shared_notice:
            findings.append({
                "id": "quantdb-benchmark-universe-dropped",
                "severity": "info",
                "evidence": {
                    "period": period_label,
                    "count": len(missing_universe),
                    "sample": missing_universe[:20],
                },
                "impact": (
                    f"行业映射内 {len(missing_universe)} 只标的窗口内无有效行情，"
                    "未进入等权基准（基准 = 行业映射 ∩ 窗口有行情）。"
                ),
                "recommended_fix": "如需完整基准，请核对该批标的的行情同步状态。",
            })
    else:
        benchmark_sum_raw = sum(
            w for w in benchmark.values() if isinstance(w, (int, float)) and math.isfinite(w) and w > 0
        )
        benchmark_kept, benchmark_dropped = _filter(benchmark, "benchmark")
        for reason, symbols in benchmark_dropped.items():
            if symbols:
                findings.append(_drop_finding("benchmark", reason, sorted(symbols)))
        if not benchmark_kept:
            issues.append({
                "reason": "benchmark_empty_after_filter",
                "period": period_label,
                "dropped": {k: len(v) for k, v in benchmark_dropped.items()},
            })
        elif abs(benchmark_sum_raw - 1.0) > weight_tolerance:
            issues.append({
                "reason": "benchmark_weights_out_of_tolerance",
                "period": period_label,
                "sum": benchmark_sum_raw,
                "tolerance": weight_tolerance,
            })
        benchmark_kind = "custom_csv"
    benchmark_norm = _norm_weights(benchmark_kept)

    if issues:
        return [], {}, findings, issues

    def _aggregate(weights: dict[str, float]) -> dict[str, dict]:
        buckets: dict[str, dict[str, float]] = {}
        for symbol, weight in weights.items():
            sector = industry[symbol]
            bucket = buckets.setdefault(sector, {"w": 0.0, "wr": 0.0})
            bucket["w"] += weight
            bucket["wr"] += weight * returns[symbol]["return"]
        return {
            sector: {"w": bucket["w"], "r": bucket["wr"] / bucket["w"]}
            for sector, bucket in buckets.items()
            if bucket["w"] > 0
        }

    portfolio_agg = _aggregate(portfolio_norm)
    benchmark_agg = _aggregate(benchmark_norm)
    r_b_total = sum(v["w"] * v["r"] for v in benchmark_agg.values())

    for sector in sorted(set(portfolio_agg) - set(benchmark_agg)):
        findings.append({
            "id": "quantdb-portfolio-sector-absent-in-benchmark",
            "severity": "medium",
            "evidence": {"period": period_label, "sector": sector},
            "impact": "组合持有的行业不在基准中：该行业 r_b 以基准总收益中性约定，配置效应记为 0。",
            "recommended_fix": "确认基准口径；如需完整分解请让基准覆盖组合全部行业。",
        })

    rows: list[dict] = []
    for sector in sorted(set(portfolio_agg) | set(benchmark_agg)):
        bench = benchmark_agg.get(sector)
        port = portfolio_agg.get(sector)
        w_b = bench["w"] if bench else 0.0
        r_b = bench["r"] if bench else r_b_total  # 基准缺该行业 → 中性约定
        w_p = port["w"] if port else 0.0
        r_p = port["r"] if port else r_b          # 组合缺该行业 → 选股/交互必为 0
        rows.append({"sector": sector, "w_p": w_p, "w_b": w_b, "r_p": r_p, "r_b": r_b})

    stats = {
        "portfolio_symbols": len(portfolio_kept),
        "benchmark_symbols": len(benchmark_kept),
        "benchmark_kind": benchmark_kind,
        "sectors": len(rows),
        "universe_symbols": len(benchmark_kept) if benchmark is None else None,
    }
    return rows, stats, findings, issues


def parse_periods(args) -> list[tuple[str, tuple[str, str]]]:
    if args.periods:
        items: list[tuple[str, tuple[str, str]]] = []
        for part in args.periods.split(","):
            part = part.strip()
            match = re.fullmatch(r"(\d{4}-\d{2}-\d{2}):(\d{4}-\d{2}-\d{2})", part)
            if not match:
                raise SystemExit(
                    "--periods 需形如 2026-04-01:2026-06-30,2026-07-01:2026-09-30（逗号分隔 start:end）"
                )
            start_s, end_s = _norm_range(match.group(1), match.group(2))
            if start_s > end_s:
                raise SystemExit(f"--periods 期间起点晚于终点: {part}")
            items.append((_label_range(start_s, end_s), (start_s, end_s)))
        if not items:
            raise SystemExit("--periods 为空")
        return items
    start_s, end_s = _norm_range(args.start, args.end)
    if start_s > end_s:
        raise SystemExit("--start 晚于 --end")
    return [(_label_range(start_s, end_s), (start_s, end_s))]


def run_quantdb(args) -> dict:
    pd = _pd()
    market = args.market.upper()
    if market not in INDUSTRY_SOURCES:
        raise SystemExit(f"不支持的市场: {market}（可选 US/HK/CN）")
    root = resolve_data_root()

    issues: list[dict] = []
    findings: list[dict] = []
    industry, industry_meta = load_industry_map(pd, root, market, args.level)
    if not industry:
        issues.append({"reason": "empty_industry_map", "path": industry_meta["source"]})

    portfolio, portfolio_issues = read_weights_csv(args.portfolio, market)
    issues.extend(portfolio_issues)
    benchmark = None
    benchmark_path = None
    if args.benchmark:
        benchmark, benchmark_issues = read_weights_csv(args.benchmark, market)
        issues.extend(benchmark_issues)
        benchmark_path = args.benchmark

    periods = parse_periods(args)
    level_ignored = args.level != "sector" and market != "US"
    if level_ignored:
        findings.append({
            "id": "quantdb-level-ignored",
            "severity": "info",
            "evidence": {"market": market, "requested_level": args.level,
                         "used_column": industry_meta["column"]},
            "impact": "该市场的行业映射只有单一粒度，--level 参数不适用，已按默认列读取。",
            "recommended_fix": "无需处理；如需更细粒度请先补行业分类数据。",
        })

    if issues:
        return build_report(
            None, issues,
            input_summary={
                "mode": "quantdb", "market": market, "method": args.method,
                "periods": [label for label, _ in periods],
            },
            assumptions=_assumptions(args, market, benchmark_path),
            limitations=_quantdb_limitations(market, industry_meta),
        )

    candidate = set(portfolio) | set(benchmark or industry)
    period_groups: list[tuple[str, list[dict]]] = []
    last_meta: dict = {}
    for label, (start_s, end_s) in periods:
        returns, returns_meta = load_window_returns(pd, root, market, candidate, start_s, end_s)
        if returns_meta.get("dups_dropped"):
            findings.append({
                "id": "quantdb-duplicate-kline-rows",
                "severity": "info",
                "evidence": {"period": label, "dups_dropped": returns_meta["dups_dropped"]},
                "impact": "同一(标的,日期)存在多来源重复行（HK 双来源期）；读取侧已保留最新写入行。",
                "recommended_fix": "确认消费方去重口径；必要时重写分区。",
            })
        rows, stats, row_findings, row_issues = assemble_sector_rows(
            portfolio, benchmark, industry, returns, args.weight_tolerance, label,
            shared_notice=(not period_groups),  # 归一化/基准全集提示只报一次
        )
        findings.extend(row_findings)
        issues.extend(row_issues)
        if not rows:
            continue
        period_groups.append((label, rows))
        last_meta = {**stats, **{
            "symbols_with_returns": returns_meta.get("symbols_with_returns"),
            "window": returns_meta.get("window"),
        }}
    if issues:
        return build_report(
            None, issues,
            input_summary={
                "mode": "quantdb", "market": market, "method": args.method,
                "periods": [label for label, _ in periods],
            },
            assumptions=_assumptions(args, market, benchmark_path),
            limitations=_quantdb_limitations(market, industry_meta),
        )
    if not period_groups:
        issues.append({"reason": "no_data_in_window", "periods": [label for label, _ in periods]})
        return build_report(
            None, issues,
            input_summary={"mode": "quantdb", "market": market, "method": args.method},
            assumptions=_assumptions(args, market, benchmark_path),
            limitations=_quantdb_limitations(market, industry_meta),
        )

    if len(period_groups) == 1:
        domain = single_period(period_groups[0][1], method=args.method)
    else:
        domain = multiperiod(period_groups, method=args.method)

    input_summary = {
        "mode": "quantdb",
        "market": market,
        "method": args.method,
        "periods": [label for label, _ in period_groups],
        "portfolio_symbols": last_meta.get("portfolio_symbols"),
        "benchmark_symbols": last_meta.get("benchmark_symbols"),
        "universe_symbols": last_meta.get("universe_symbols"),
        "sectors": last_meta.get("sectors"),
        "industry_source": industry_meta["source"],
        "industry_column": industry_meta["column"],
        "benchmark_kind": last_meta.get("benchmark_kind"),
    }
    return build_report(
        domain, [],
        input_summary=input_summary,
        assumptions=_assumptions(args, market, benchmark_path),
        adapter_findings=findings,
        limitations=_quantdb_limitations(market, industry_meta),
    )


def _assumptions(args, market: str | None = None, benchmark_path: str | None = None) -> dict:
    assumptions = {
        "method": args.method,
        "weight_tolerance": args.weight_tolerance,
        "allocation_definition": "fachler: (w_p−w_b)·(r_b−R_b)；bhb: (w_p−w_b)·r_b",
        "selection_definition": "w_b·(r_p−r_b)",
        "interaction_definition": "(w_p−w_b)·(r_p−r_b)",
        "residual_tolerance": RESIDUAL_TOL,
        "period_order": "CSV 首次出现顺序 / --periods 列出顺序（调用方保证期序与非重叠）",
    }
    if market:
        assumptions["benchmark_construction"] = (
            f"custom CSV: {benchmark_path}" if benchmark_path else "行业映射全集等权（缺省）"
        )
        assumptions["return_convention"] = {
            "CN": "quantdb/1_kline_data/daily_forward = 前复权（含分红再投）",
            "HK": "quanthk/1_kline_data/daily_forward = 不复权（价格收益，不含分红）",
            "US": "quantus/1_kline_data/daily_forward = 不复权（价格收益，不含分红）",
        }[market]
        assumptions["return_window"] = "窗口内首/末有效收盘之比 − 1（非日历首末日）"
        assumptions["portfolio_weights"] = f"按容差(±{args.weight_tolerance})校验后归一化到 1"
    return assumptions


def _quantdb_limitations(market: str, industry_meta: dict) -> list[str]:
    per_market = {
        "CN": [
            "行业为 instrument_detail 静态快照（rs_hyname，HqDate=20260720），期间行业调整不反映。",
            "CN daily_forward 为前复权价，收益含分红再投；与不复权口径不可混比。",
        ],
        "HK": [
            "行业为 akshare_profile 的「所属行业」（31 类，非 GICS）；sector/ 目录 sector 列全空不可用。",
            "HK daily_forward 为不复权价：收益为价格收益，不含分红；2024-09-02~2026-05-08 存在双来源重复行（读取侧已去重）。",
        ],
        "US": [
            "行业为 yahoo sector（11 类）/ industry（更细），非 GICS 官方口径。",
            "US daily_forward 为不复权价：收益为价格收益，不含分红。",
        ],
    }
    return per_market.get(market, []) + [
        f"行业映射源：{industry_meta['source']}（列 {industry_meta['column']}，"
        f"{industry_meta['symbols']} 标的 / {industry_meta['industries']} 行业）；"
        "基准仅在行业映射 ∩ 窗口内有行情的标的内等权。"
    ]


# ====================================================================== CLI


def main() -> None:
    parser = argparse.ArgumentParser(description="Brinson 业绩归因（Fachler/BHB + Carino 多期链接）")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", help="CSV：sector,w_p,w_b,r_p,r_b（可选 period 列 → Carino 多期）")
    source.add_argument("--demo", action="store_true", help="内置三期样例（源 run_demo CASE B 数据）")
    source.add_argument("--quantdb", action="store_true", help="从本地 QuantDB 构建行业级输入")
    parser.add_argument("--method", choices=["fachler", "bhb"], default="fachler")
    parser.add_argument("--out", help="JSON 报告落盘路径")
    parser.add_argument("--text", action="store_true", help="额外打印人读摘要")
    parser.add_argument("--weight-tolerance", type=float, default=DEFAULT_WEIGHT_TOL)
    parser.add_argument("--market", help="US / HK / CN（--quantdb 时必填）")
    parser.add_argument("--portfolio", help="组合 CSV：symbol,weight（--quantdb 时必填）")
    parser.add_argument("--benchmark", help="基准 CSV：symbol,weight（缺省=行业映射全集等权）")
    parser.add_argument("--start", help="窗口起点 YYYY-MM-DD")
    parser.add_argument("--end", help="窗口终点 YYYY-MM-DD")
    parser.add_argument("--periods", help="多期窗口：2026-04-01:2026-06-30,2026-07-01:2026-09-30")
    parser.add_argument(
        "--level", choices=["sector", "industry"], default="sector",
        help="US 行业粒度（sector=11 类 / industry=更细）；HK/CN 单粒度，参数忽略",
    )
    args = parser.parse_args()

    if args.quantdb:
        if not (args.market and args.portfolio):
            parser.error("--quantdb 需要 --market --portfolio，以及 --start/--end 或 --periods")
        if args.periods and (args.start or args.end):
            parser.error("--periods 与 --start/--end 互斥")
        if not args.periods and not (args.start and args.end):
            parser.error("--quantdb 需要 --periods 或 --start+--end")
        report = run_quantdb(args)
        if args.text and report["status"] != "insufficient-evidence":
            print(render_text(report["domain_result"]))
        emit(report, args.out)
        return

    rows = demo_rows() if args.demo else None
    issues: list[dict] = []
    if rows is None:
        rows, issues = load_csv_rows(args.input)
    groups: list[tuple[str, list[dict]]] = []
    if not issues:
        validation_issues, groups = validate_sector_rows(rows, args.weight_tolerance)
        issues.extend(validation_issues)
    mode = "demo" if args.demo else ("csv-multiperiod" if len(groups) > 1 else "csv")
    input_summary = {
        "mode": mode,
        "method": args.method,
        "sectors": len(groups[0][1]) if len(groups) == 1 else sum(len(g) for _, g in groups),
        "periods": [label for label, _ in groups],
    }
    domain = None
    if not issues and groups:
        if len(groups) == 1:
            domain = single_period(groups[0][1], method=args.method)
        else:
            domain = multiperiod(groups, method=args.method)
    report = build_report(
        domain, issues,
        input_summary=input_summary,
        assumptions=_assumptions(args),
    )
    if args.text and domain is not None:
        print(render_text(domain))
    emit(report, args.out)


if __name__ == "__main__":
    main()
