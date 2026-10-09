#!/usr/bin/env python3
"""机构持股集中度面板（CCASS 本地化移植版，HK 主市场）。

来源：quantskills/skill-hk-us-institutional-concentration（源仓库许可为空/未声明）。
仅方法论改写与本地化：研究门禁 + 证据台账 + 结构三分（广度/头部主导/证据置信度）
+ 阈值敏感性 + 时点可用性纪律。数据层由 PandaAI/PandaData API 改为 QuantDB 本地直读。

本地数据（2026-10-07 实测标定，容器内 /data/quanthk）：
  HK  quanthk/2_base_sector/ccass_top50/dt=YYYYMMDD/data.parquet
      列：stock_code(四位+.HK) / participant_id / participant_name /
          holding_quantity(int64 股数) / holding_percentage(double 0-1 分数) / query_date
      标定证据（同日精确对账）：2025-12-15 中国结算两席位(A00003+A00004)
      holding_quantity 合计 = 1,006,948,064 股，与 hsgt_south/dt=20251215/data.parquet
      同日 holding_quantity 完全相同、holding_percentage 均 11.01%，证明
      ① holding_percentage 是「占公司总股本」的 0-1 分数；② holding_quantity 为原始股数。
      另 0388.HK 2026-09-11：579,122,694/0.4567=12.68 亿股，与港交所已发行股本
      1,267,836,895 相符（四席位互推总股本一致，离散 <0.1%）。
  南向对账源 hsgt_south：读 dt=YYYYMMDD 主布局（每晚同步，2024-11-27 起连续）；
      旧的 {sym}.HK.parquet 布局 2025-12-19 后冻结、勿读（会静默拿到过时数据），
      脚本仅在主布局分区缺失时才用旧布局兜底。

用法：
  python3 inst_concentration.py --demo                     # 离线确定性引擎（纯标准库）
  python3 inst_concentration.py --input rows.csv --out DIR # 任意 CSV 面板复核（纯标准库）
  python3 inst_concentration.py --quantdb --market HK --symbols 0700.HK,0005.HK \\
      --start 2026-06-12 --end 2026-09-11 --out DIR       # 本地 CCASS（容器内跑，需 pandas/pyarrow）

输出：<out>/quality_report.json + <out>/institutional_concentration_panel.csv
      （+ participant_detail.csv 明细，--no-detail 可关）。

US 13F：本地无数据源 → --market US 显式报「不可用」；45 天滞后知识点保留在 references。
CN holder_num：股东户数 ≠ 机构集中度，仅见 references 备注，本脚本不消费。
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
from pathlib import Path

# ------------------------------------------------------------------ 常量

SOURCE_REPO = "quantskills/skill-hk-us-institutional-concentration"

# 结构标签词表（本地化命名；源标签映射见 SKILL.md）
# fmt: off
STRUCTURE_LABELS = (
    "broad_participation",   # ≙ broad_institutional
    "dominant_seat",         # ≙ dominant_holder（CCASS 头部=托管席位，非实益持有人）
    "fragmented_or_mixed",
    "data_anomaly",
    "insufficient_data",
)
# fmt: on

# 主阈值：与源技能默认一致（源：largest_holder>=20 或 hhi>=0.10 判主导；breadth>=50 判广布）
PRIMARY_THRESHOLDS = {
    "dominance_top1_pct": 20.0,
    "dominance_hhi": 0.10,
    "broad_breadth_pct": 50.0,
}
# 敏感性备选阈值（紧/松各一档：top1 ∓5pp、hhi ∓0.02），门禁要求至少两套
ALT_THRESHOLD_STEPS = {"tight": (-5.0, -0.02), "loose": (+5.0, +0.02)}

# HK CCASS 披露节奏保守口径：分区日 T 的数据按 T+1 交易日可用（时点联结纪律）
AVAILABILITY_LAG_TRADING_DAYS = 1
# US 13F：期末 + 45 日历日 = 信息可得日（本地无源，仅知识保留）
THIRTEEN_F_LAG_DAYS = 45

# CSV 列名归一（源技能 api-map 归一化思路的本地简化版）
# fmt: off
COLUMN_ALIASES = {
    "symbol": ("symbol", "ticker", "stock_code", "code"),
    "date": ("date", "query_date", "dt", "trade_date"),
    "holder_id": ("holder_id", "participant_id", "investor_id"),
    "holder_name": ("holder_name", "participant_name", "investor_name", "shareholder_name", "name"),
    "holding_pct": ("holding_pct", "holdings_pct", "holding_percentage", "percentage", "percent", "pct"),
    "holding_shares": ("holding_shares", "holding_quantity", "shares", "quantity"),
}
# fmt: on
REQUIRED_KEYS = ("symbol", "date", "holding_pct")

# CCASS 参与者中属「中国结算」的南向通道席位（一致性抽查用）
CSDC_PARTICIPANT_IDS = ("A00003", "A00004")
# 南向对账按股数精确相等判定（同日或 T-1 双对齐）；不再用相对偏差阈值——
# 实测差异行 54/54 均为整日快照错位（official(T)==CCASS(T-1)），非数值偏差。
DENOM_SPREAD_TOL = 0.02  # 总股本互推离散容忍 2%
DENOM_MIN_PCT_FRAC = 0.01  # 参与互推的最小份额（低于此,舍入噪声大）

# ---------------------------------------------------------------- 离线演示


def _demo_rows() -> list[dict]:
    """确定性演示数据：覆盖全部五种结构标签 + 一例阈值标签翻转。"""
    # fmt: off
    spec = {  # symbol: 席位份额阶梯（%）→ 结构标签
        "BROAD.HK": [4.6] * 12,                                                    # 广度 55.2，无主导 → broad
        "DOM.HK":   [34.0, 8.0, 5.0, 4.0, 3.0, 2.5, 2.0],                          # → dominant
        "EDGE.HK":  [17.5, 9.5, 6.5, 4.5, 3.5, 3.0, 2.5, 2.0, 1.5, 1.0, 0.5],      # 广度 52、top1 17.5：紧阈值翻转
        "MIX.HK":   [12.0, 7.0, 6.0, 5.0, 4.5, 4.0, 3.5, 3.0, 2.0, 1.0],           # 广度 48 → mixed
        "ANOM.HK":  [150.0, 6.0, 4.0],                                             # 越界 → data_anomaly
    }
    # fmt: on
    dates = ["2026-01-05", "2026-01-06", "2026-01-07"]
    rows: list[dict] = []
    for symbol, weights in spec.items():
        for di, date in enumerate(dates):
            for hi, base in enumerate(weights):
                drift = round(0.01 * di, 4)  # 极小漂移：只动 Δ，不跨越结构阈值
                pct = round(base + drift, 4)
                rows.append(
                    {
                        "symbol": symbol,
                        "date": date,
                        "holder_id": f"H{hi + 1:02d}",
                        "holder_name": f"Seat {hi + 1}",
                        "holding_pct": str(pct),
                        "holding_shares": "",
                    }
                )
    rows.append(
        {
            "symbol": "MISS.HK",
            "date": dates[-1],
            "holder_id": "H01",
            "holder_name": "Seat 1",
            "holding_pct": "",
            "holding_shares": "",
        }
    )
    return rows


# ------------------------------------------------------------ 输入装载


def _json_safe(value: object) -> object:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _number(value: object) -> float:
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else math.nan
    except (TypeError, ValueError):
        return math.nan


def resolve_columns(header: list[str]) -> tuple[dict, list[str]]:
    """按别名表把任意列名表解析为标准键；返回（映射, 缺失键清单）。"""
    lower = {str(c).strip().lower(): c for c in header}
    mapping: dict[str, str] = {}
    for key, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in lower:
                mapping[key] = lower[alias]
                break
    missing = [k for k in REQUIRED_KEYS if k not in mapping]
    return mapping, missing


def load_input_rows(path: str) -> tuple[list[dict], dict]:
    """读任意 CSV（列名走别名归一；holding_pct 支持 0-1 与 0-100 两种标度）。"""
    with open(path, encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        header = list(reader.fieldnames or [])
        raw_rows = list(reader)
    mapping, missing = resolve_columns(header)
    if missing:
        raise SystemExit(
            f"输入缺少必需列：{missing}；现有列：{header}。"
            f"支持别名（holding_pct）：{COLUMN_ALIASES['holding_pct']}"
        )
    issues: list[dict] = []
    rows: list[dict] = []
    values: list[float] = []

    def _cell(raw: dict, key: str) -> str:
        col = mapping.get(key)
        return "" if col is None else str(raw.get(col, "") or "").strip()

    for line_no, raw in enumerate(raw_rows, 2):
        pct = _number(raw.get(mapping["holding_pct"]))
        if math.isnan(pct):
            issues.append(
                {
                    "reason": "invalid_numeric",
                    "row": line_no,
                    "column": "holding_pct",
                    "value": raw.get(mapping["holding_pct"]),
                }
            )
            continue
        values.append(pct)
        rows.append(
            {
                "symbol": _cell(raw, "symbol"),
                "date": _cell(raw, "date"),
                "holder_id": _cell(raw, "holder_id"),
                "holder_name": _cell(raw, "holder_name"),
                "holding_pct": pct,
                "holding_shares": _number(_cell(raw, "holding_shares"))
                if "holding_shares" in mapping
                else math.nan,
            }
        )
    scale = detect_scale(values)
    if scale["detected"] == "fraction_0_1":
        for row in rows:
            row["holding_pct"] *= 100.0
    return rows, {"scale": scale, "issues": issues, "input_rows": len(raw_rows)}


def detect_scale(values: list[float]) -> dict:
    """源技能规则：非空绝对值全部 ≤1 → 0-1 分数，转 0-100。"""
    non_null = [abs(v) for v in values if not math.isnan(v)]
    if not non_null:
        return {"detected": "unknown", "max_abs": None}
    max_abs = max(non_null)
    if max_abs <= 1.0:
        return {"detected": "fraction_0_1", "max_abs": max_abs}
    mixed = any(v <= 1.0 for v in non_null)
    return {
        "detected": "percent_0_100",
        "max_abs": max_abs,
        "mixed_scale_suspected": mixed,
    }


# ---------------------------------------------------------------- 核心引擎


def group_metrics(holders: list[dict], thresholds: dict) -> dict:
    """单（标的, 日期）分组 → 结构与证据指标。holders: 已归一的 {holding_pct(0-100), ...}。

    任何输入都返回同一键形（缺失项为 None），保证下游 CSV/JSON 列稳定。
    """
    valid = [h for h in holders if not math.isnan(h["holding_pct"])]
    if not valid:
        return {
            "breadth_pct": None,
            "top1_pct": None,
            "top5_pct": None,
            "top10_pct": None,
            "holder_hhi": None,
            "participant_count": 0,
            "seat_rows": 0,
            "breadth_gap_pct": None,
            "largest_participant_id": "",
            "largest_participant_name": "",
            "denominator_spread": None,
            "has_data_anomaly": False,
            "anomaly_flags": [],
            "ownership_structure": "insufficient_data",
            "evidence_count": 0,
            "data_confidence": "low",
        }
    pcts = sorted((h["holding_pct"] for h in valid), reverse=True)
    top = sorted(valid, key=lambda h: h["holding_pct"], reverse=True)
    breadth = sum(pcts)
    top1 = pcts[0] if pcts else math.nan
    top5 = sum(pcts[:5])
    top10 = sum(pcts[:10])
    hhi = sum((p / 100.0) ** 2 for p in pcts)
    nonzero = [p for p in pcts if p > 0]
    implied = [
        h["holding_shares"] / (h["holding_pct"] / 100.0)
        for h in valid
        if h["holding_pct"] / 100.0 >= DENOM_MIN_PCT_FRAC
        and not math.isnan(h.get("holding_shares", math.nan))
        and h["holding_shares"] > 0
    ]
    denom_spread = math.nan
    if len(implied) >= 2:
        mid = sorted(implied)[len(implied) // 2]
        if mid > 0:
            denom_spread = (max(implied) - min(implied)) / mid
    anomaly_flags = []
    if any(p < 0 or p > 100 for p in pcts):
        anomaly_flags.append("holding_pct_out_of_range")
    if breadth > 100.0001:
        anomaly_flags.append("breadth_over_100")
    if hhi > 1.0 + 1e-9:
        anomaly_flags.append("hhi_over_1")
    record = {
        "breadth_pct": round(breadth, 4),
        "top1_pct": round(top1, 4),
        "top5_pct": round(top5, 4),
        "top10_pct": round(top10, 4),
        "holder_hhi": round(hhi, 6),
        "participant_count": len(nonzero),
        "seat_rows": len(valid),
        "breadth_gap_pct": round(breadth - top10, 4),
        "largest_participant_id": top[0].get("holder_id", ""),
        "largest_participant_name": top[0].get("holder_name", ""),
        "denominator_spread": None
        if math.isnan(denom_spread)
        else round(denom_spread, 4),
        "has_data_anomaly": bool(anomaly_flags),
        "anomaly_flags": anomaly_flags,
    }
    record["ownership_structure"] = classify(record, thresholds)
    evidence = [
        _finite(record["breadth_pct"]) and -1e-9 <= record["breadth_pct"] <= 100.0001,
        _finite(record["top10_pct"]) and 0 <= record["top10_pct"] <= 100.0001,
        _finite(record["holder_hhi"]) and 0 <= record["holder_hhi"] <= 1.0001,
        _finite(record["top1_pct"]) and 0 <= record["top1_pct"] <= 100.0001,
    ]
    record["evidence_count"] = sum(1 for flag in evidence if flag)
    record["data_confidence"] = (
        "low"
        if record["evidence_count"] <= 1
        else "medium"
        if record["evidence_count"] <= 3
        else "high"
    )
    return record


def _finite(value: object) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _diff(new: object, old: object, digits: int) -> float | None:
    """None 安全的差值（结构指标缺失时返回 None，不得当成 0）。"""
    if not _finite(new) or not _finite(old):
        return None
    return round(float(new) - float(old), digits)


def classify(record: dict, thresholds: dict) -> str:
    """源 np.select 顺序：anomaly > insufficient > dominance > broad > default mixed。"""
    if record.get("has_data_anomaly"):
        return "data_anomaly"
    if not _finite(record.get("breadth_pct")):
        return "insufficient_data"
    dominance = (
        record["top1_pct"] >= thresholds["dominance_top1_pct"]
        or record["holder_hhi"] >= thresholds["dominance_hhi"]
    )
    broad = (
        record["breadth_pct"] >= thresholds["broad_breadth_pct"]
        and record["top1_pct"] < thresholds["dominance_top1_pct"]
        and record["holder_hhi"] < thresholds["dominance_hhi"]
    )
    if dominance:
        return "dominant_seat"
    if broad:
        return "broad_participation"
    return "fragmented_or_mixed"


def alt_threshold_sets(primary: dict) -> dict:
    sets = {"primary": dict(primary)}
    for name, (d_top1, d_hhi) in ALT_THRESHOLD_STEPS.items():
        sets[name] = {
            "dominance_top1_pct": round(primary["dominance_top1_pct"] + d_top1, 4),
            "dominance_hhi": round(primary["dominance_hhi"] + d_hhi, 4),
            "broad_breadth_pct": primary["broad_breadth_pct"],
        }
    return sets


def build_panel(holders: list[dict], thresholds: dict, window: int = 20) -> dict:
    """双模式共用：按 (symbol,date) 聚合 → 序列 + 摘要 + 敏感性 + 异常。"""
    groups: dict[tuple[str, str], list[dict]] = {}
    for holder in holders:
        if not holder["symbol"] or not holder["date"]:
            continue
        groups.setdefault((holder["symbol"], holder["date"]), []).append(holder)
    thr_sets = alt_threshold_sets(thresholds)
    series: dict[str, list[dict]] = {}
    anomalies: list[dict] = []
    for (symbol, date), members in sorted(groups.items()):
        primary_rec = group_metrics(members, thr_sets["primary"])
        rec = {"symbol": symbol, "date": date, **primary_rec}
        rec["labels"] = {
            name: classify(primary_rec, thr) for name, thr in thr_sets.items()
        }
        series.setdefault(symbol, []).append(rec)
        if primary_rec["has_data_anomaly"]:
            anomalies.append(
                {
                    "symbol": symbol,
                    "date": date,
                    "flags": primary_rec["anomaly_flags"],
                    "severity": "high",
                }
            )
    summary: list[dict] = []
    flips: list[dict] = []
    all_dates = sorted({rec["date"] for records in series.values() for rec in records})
    for symbol, records in sorted(series.items()):
        records.sort(key=lambda r: r["date"])
        for idx, rec in enumerate(records):
            if idx >= window:
                base = records[idx - window]
                rec["delta_window_top10_pct"] = _diff(
                    rec["top10_pct"], base["top10_pct"], 4
                )
                rec["delta_window_breadth_pct"] = _diff(
                    rec["breadth_pct"], base["breadth_pct"], 4
                )
                rec["delta_window_hhi"] = _diff(
                    rec["holder_hhi"], base["holder_hhi"], 6
                )
                rec["delta_window_participants"] = (
                    rec["participant_count"] - base["participant_count"]
                )
            else:
                rec["delta_window_top10_pct"] = None
                rec["delta_window_breadth_pct"] = None
                rec["delta_window_hhi"] = None
                rec["delta_window_participants"] = None
        latest, first = records[-1], records[0]
        labels_primary = latest["labels"]["primary"]
        alt_labels = {k: v for k, v in latest["labels"].items() if k != "primary"}
        if any(v != labels_primary for v in alt_labels.values()):
            flips.append(
                {
                    "symbol": symbol,
                    "date": latest["date"],
                    "primary_label": labels_primary,
                    "alternative_labels": alt_labels,
                }
            )
        symbol_dates = {rec["date"] for rec in records}
        missing_dates = sorted(set(all_dates) - symbol_dates)
        summary.append(
            {
                "symbol": symbol,
                "market": "HK" if symbol.endswith(".HK") else "custom",
                "dates": len(records),
                "first_date": first["date"],
                "latest_date": latest["date"],
                "dates_in_scope": len(all_dates),
                "missing_dates": missing_dates[:10],
                "missing_dates_count": len(missing_dates),
                "latest": {
                    k: latest[k]
                    for k in (
                        "breadth_pct",
                        "top1_pct",
                        "top5_pct",
                        "top10_pct",
                        "holder_hhi",
                        "participant_count",
                        "breadth_gap_pct",
                        "largest_participant_id",
                        "largest_participant_name",
                        "denominator_spread",
                        "has_data_anomaly",
                        "anomaly_flags",
                        "evidence_count",
                        "data_confidence",
                    )
                },
                "latest_labels": latest["labels"],
                "delta_latest_window": {
                    "top10_pct": latest["delta_window_top10_pct"],
                    "breadth_pct": latest["delta_window_breadth_pct"],
                    "hhi": latest["delta_window_hhi"],
                    "participants": latest["delta_window_participants"],
                },
                "delta_span_first_to_latest": {
                    "top10_pct": _diff(latest["top10_pct"], first["top10_pct"], 4),
                    "breadth_pct": _diff(
                        latest["breadth_pct"], first["breadth_pct"], 4
                    ),
                    "hhi": _diff(latest["holder_hhi"], first["holder_hhi"], 6),
                    "participants": (
                        latest["participant_count"] - first["participant_count"]
                    ),
                },
            }
        )
    return {
        "series": series,
        "summary": summary,
        "anomalies": anomalies,
        "label_flips": flips,
        "threshold_sets": thr_sets,
    }


# ------------------------------------------------------- QuantDB 装配层


def _pd():
    try:
        import pandas as pd  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "--quantdb 模式需要 pandas/pyarrow。请在 quantmind 容器内运行：\n"
            "  docker cp skills/institutional-concentration/scripts/inst_concentration.py quantmind:/tmp/\n"
            "  docker exec -w /app quantmind python3 /tmp/inst_concentration.py --quantdb ..."
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
        if os.path.isdir(os.path.join(cand, "quanthk", "2_base_sector", "ccass_top50")):
            return cand
    raise SystemExit(
        "未找到 quanthk/2_base_sector/ccass_top50：设置 QM_DATA_ROOT 或确认 /data 挂载"
    )


def _norm_range(start: str, end: str) -> tuple[str, str]:
    s, e = re.sub(r"\D", "", start), re.sub(r"\D", "", end)
    if len(s) != 8 or len(e) != 8:
        raise SystemExit("--start/--end 需为 YYYY-MM-DD 或 YYYYMMDD")
    return s, e


def read_ccass(base: str, symbols: list[str], start_s: str, end_s: str) -> dict:
    """读 CCASS 分区 → 归一 holder 行；同时采集分区/重复/未覆盖日期证据。"""
    pd = _pd()
    ccass_dir = os.path.join(base, "quanthk", "2_base_sector", "ccass_top50")
    dates = sorted(
        d[3:]
        for d in os.listdir(ccass_dir)
        if d.startswith("dt=") and d[3:].isdigit() and len(d[3:]) == 8
    )
    win = [d for d in dates if start_s <= d <= end_s]
    if not win:
        raise SystemExit(
            f"窗口 {start_s}~{end_s} 内无 CCASS 分区（现有 {dates[0]}~{dates[-1]}，共 {len(dates)} 天）。"
            "注意本机 2026-09-23~09-29 为回补缺口，取窗时避开或外推。"
        )
    want = set(symbols)
    holders: list[dict] = []
    seen_keys: set[tuple] = set()
    duplicates = 0
    found: set[str] = set()
    for ds in win:
        path = os.path.join(ccass_dir, f"dt={ds}", "data.parquet")
        if not os.path.exists(path):
            continue
        frame = pd.read_parquet(
            path,
            columns=[
                "stock_code",
                "participant_id",
                "participant_name",
                "holding_quantity",
                "holding_percentage",
            ],
        )
        frame = frame[frame["stock_code"].isin(want)]
        for row in frame.itertuples(index=False):
            key = (row.stock_code, ds, row.participant_id)
            if key in seen_keys:
                duplicates += 1
                continue
            seen_keys.add(key)
            found.add(row.stock_code)
            holders.append(
                {
                    "symbol": row.stock_code,
                    "date": f"{ds[:4]}-{ds[4:6]}-{ds[6:]}",
                    "holder_id": str(row.participant_id),
                    "holder_name": str(row.participant_name),
                    "holding_pct": float(row.holding_percentage)
                    * 100.0,  # 0-1 分数 → 0-100
                    "holding_shares": float(row.holding_quantity),
                }
            )
    missing_symbols = sorted(want - found)
    return {
        "holders": holders,
        "partitions": win,
        "duplicates": duplicates,
        "missing_symbols": missing_symbols,
        "availability_dates": _availability_map(dates),
    }  # 全量分区，窗口末日也能得到 T+1


def _availability_map(dates: list[str]) -> dict:
    """点-in-time 纪律：分区日 T 的可得日 = 下一可得分区日（末日落盘前记 None）。"""
    out = {}
    for i, ds in enumerate(dates):
        nxt = dates[i + 1] if i + 1 < len(dates) else None
        out[ds] = f"{nxt[:4]}-{nxt[4:6]}-{nxt[6:]}" if nxt else None
    return out


def southbound_check(base: str, holders: list[dict], partitions: list[str]) -> dict:
    """一致性抽查：CCASS 中国结算席位合计 vs hsgt_south 官方南向口径。

    读取主布局 `dt=YYYYMMDD/data.parquet`（每晚同步写入，2024-11-27 起的完整
    历史已由 quanthk_south_history_merge.py 回填）。旧「每股一文件」布局
    （`{symbol}.HK.parquet`）自 2025-12-19 冻结、不再更新，仅在主布局分区
    缺失且日期落在其覆盖期内时作兜底——直接读旧布局会静默拿到过时数据。

    对齐判定按**股数精确相等**，接受两种对位（实测 2026-08-25 起官方源存在
    更新时点竞态，多数分区是前一交易日的 T-1 快照；见 references/quantdb-ccass-map.md）：
    ① 同日：official(T) == CCASS(T)；② 滞后一日：official(T) == CCASS(T-1)。
    两态皆不吻合才计入 unaligned（真差异）。
    """
    pd = _pd()
    ccass_qty: dict[tuple[str, str], int] = {}
    for h in holders:
        if h["holder_id"] in CSDC_PARTICIPANT_IDS:
            key = (h["symbol"], h["date"].replace("-", ""))
            ccass_qty[key] = ccass_qty.get(key, 0) + int(h["holding_shares"])
    sb_path = os.path.join(base, "quanthk", "2_base_sector", "hsgt_south")
    official: dict[tuple[str, str], int] = {}
    missing_partitions: list[str] = []
    used_legacy = False
    for ds in sorted(set(partitions)):
        part = os.path.join(sb_path, f"dt={ds}", "data.parquet")
        if not os.path.exists(part):
            missing_partitions.append(ds)
            continue
        frame = pd.read_parquet(part, columns=["symbol", "holding_quantity"])
        for sym, qty in zip(frame["symbol"], frame["holding_quantity"], strict=False):
            official[(str(sym), ds)] = int(qty)
    legacy_dates = {ds for ds in missing_partitions if ds <= "20251219"}
    if legacy_dates:
        for symbol in sorted({h["symbol"] for h in holders}):
            path = os.path.join(sb_path, f"{symbol}.parquet")
            if not os.path.exists(path):
                continue
            frame = pd.read_parquet(path, columns=["query_date", "holding_quantity"])
            frame["ds"] = (
                frame["query_date"].astype(str).str.replace("-", "", regex=False)
            )
            for ds, qty in zip(frame["ds"], frame["holding_quantity"], strict=False):
                if ds in legacy_dates:
                    official.setdefault((symbol, ds), int(qty))
                    used_legacy = True
    # 全量 CCASS 分区序列（含窗口外），供窗口首日回看 T-1
    ccass_dir = os.path.join(base, "quanthk", "2_base_sector", "ccass_top50")
    all_dates = (
        sorted(d[3:] for d in os.listdir(ccass_dir) if d.startswith("dt="))
        if os.path.isdir(ccass_dir)
        else []
    )
    prev_all = {d: all_dates[i - 1] for i, d in enumerate(all_dates) if i > 0}
    same_day, lag1, unaligned = 0, 0, []
    for (symbol, ds), official_qty in sorted(official.items()):
        ccass_ds_qty = ccass_qty.get((symbol, ds))
        if ccass_ds_qty is None:
            continue
        if ccass_ds_qty == official_qty:
            same_day += 1
            continue
        prev_ds = prev_all.get(ds)
        prev_qty = ccass_qty.get((symbol, prev_ds)) if prev_ds else None
        if prev_qty is None and prev_ds:
            prev_part = os.path.join(ccass_dir, f"dt={prev_ds}", "data.parquet")
            if os.path.exists(prev_part):
                prev_frame = pd.read_parquet(
                    prev_part,
                    columns=["stock_code", "participant_id", "holding_quantity"],
                )
                prev_frame = prev_frame[
                    (prev_frame["stock_code"] == symbol)
                    & (
                        prev_frame["participant_id"]
                        .astype(str)
                        .isin(CSDC_PARTICIPANT_IDS)
                    )
                ]
                if len(prev_frame):
                    prev_qty = int(prev_frame["holding_quantity"].sum())
        if prev_qty is not None and prev_qty == official_qty:
            lag1 += 1
            continue
        unaligned.append(
            {
                "symbol": symbol,
                "date": ds,
                "ccass_csdc_qty": ccass_ds_qty,
                "hsgt_south_qty": official_qty,
                "rel_diff_pct": round(
                    (ccass_ds_qty - official_qty) / official_qty * 100, 4
                )
                if official_qty
                else None,
                "ccass_prev_qty": prev_qty,
            }
        )
    compared = same_day + lag1
    result = {
        "compared_stock_days": compared,
        "aligned_same_day": same_day,
        "aligned_official_lag_1d": lag1,
        "unaligned": unaligned[:20],
        "unaligned_count": len(unaligned),
        "partitions_missing_in_window": missing_partitions[:15],
        "source": "dt_partition" + ("+legacy_fallback" if used_legacy else ""),
    }
    if compared == 0:
        result["note"] = (
            "hsgt_south 与窗口无可对账日期（分区缺失或窗口内无中国结算席位行），"
            "对账未执行——按缺失证据处理，不视为通过"
        )
    return result


def denominator_check(panel: dict) -> dict:
    """总股本互推一致性：同（标的, 日）内 holding_quantity/holding_percentage 应互洽。"""
    offenders, checked = [], 0
    for symbol, records in panel["series"].items():
        for rec in records:
            spread = rec.get("denominator_spread")
            if spread is None:
                continue
            checked += 1
            if spread > DENOM_SPREAD_TOL:
                offenders.append(
                    {"symbol": symbol, "date": rec["date"], "spread": spread}
                )
    return {
        "stock_days_checked": checked,
        "inconsistent": offenders[:20],
        "inconsistent_count": len(offenders),
    }


# ------------------------------------------------------------------ 报告


def build_ledger(report: dict) -> list[dict]:
    scope, sem, cov = report["scope"], report["semantics"], report["coverage"]
    structure_ok = all(
        s["latest"].get("top10_pct") is not None for s in report["panel"]
    )
    anomalies = report["anomalies"]
    sensitivity = report["sensitivity"]
    ledger = [
        {
            "gate": "scope",
            "evidence": {
                k: scope[k]
                for k in ("market", "universe", "start", "end", "holder_definition")
            },
            "status": "ok" if scope["universe"] else "fail",
        },
        {
            "gate": "semantics",
            "evidence": {
                "source": sem["source"],
                "scale_detected": sem["scale_detected"],
                "dated_observations": sem["dated_observations"],
                "availability_lag_trading_days": sem["availability_lag_trading_days"],
                "denominator_check": sem.get("denominator_check"),
            },
            "status": "ok" if sem["scale_detected"] != "unknown" else "warn",
        },
        {
            "gate": "coverage",
            "evidence": {
                "stock_days": cov["stock_days"],
                "duplicates": cov["duplicates"],
                "missing_symbols": cov.get("missing_symbols", []),
                "date_gaps": cov.get("date_gaps", []),
                "depth_min_median_max": cov.get("depth_min_median_max"),
            },
            "status": (
                "fail"
                if cov["stock_days"] == 0
                else "warn"
                if (
                    cov.get("missing_symbols")
                    or cov["duplicates"]
                    or cov.get("date_gaps")
                )
                else "ok"
            ),
        },
        {
            "gate": "structure",
            "evidence": {
                "symbols_with_metrics": len(report["panel"]),
                "labels": sorted(
                    {s["latest_labels"]["primary"] for s in report["panel"]}
                ),
            },
            "status": "ok" if structure_ok else "warn",
        },
        {
            "gate": "anomalies",
            "evidence": {
                "count": len(anomalies),
                "sample": anomalies[:5],
                "non_rankable_labeled": True,
            },
            "status": "warn" if anomalies else "ok",
        },
        {
            "gate": "sensitivity",
            "evidence": {
                "threshold_sets": sensitivity["threshold_sets"],
                "label_flips": sensitivity["label_flips"],
            },
            "status": "ok" if len(sensitivity["threshold_sets"]) >= 2 else "fail",
        },
        {
            "gate": "interpretation",
            "evidence": {
                "rule": "结构标签是透明筛选规则，非经济规律；头部分位=托管席位而非实益持有人；"
                "不得由当前结构推收益结论（观察≠假设≠预测）"
            },
            "status": "ok",
        },
    ]
    return ledger


def assemble_report(
    mode: str,
    panel: dict,
    scope: dict,
    semantics: dict,
    coverage: dict,
    warnings: list[dict],
) -> dict:
    anomalies = panel["anomalies"]
    report = {
        "status": "FAIL" if coverage["stock_days"] == 0 else "PASS",
        "mode": mode,
        "source_skill": SOURCE_REPO,
        "scope": scope,
        "semantics": semantics,
        "coverage": coverage,
        "sensitivity": {
            "threshold_sets": panel["threshold_sets"],
            "label_flips": panel["label_flips"],
        },
        "panel": panel["summary"],
        "anomalies": anomalies,
        "warnings": warnings,
    }
    report["ledger"] = build_ledger(report)
    if any(item["status"] == "fail" for item in report["ledger"]):
        report["status"] = "FAIL"
    elif (
        any(item["status"] == "warn" for item in report["ledger"])
        and report["status"] == "PASS"
    ):
        report["status"] = "WARN"
    return report


def write_outputs(out_dir: Path, report: dict, panel: dict, detail: list[dict]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "quality_report.json").write_text(
        json.dumps(_json_safe(report), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    columns = [
        "symbol",
        "date",
        "breadth_pct",
        "top1_pct",
        "top5_pct",
        "top10_pct",
        "holder_hhi",
        "participant_count",
        "breadth_gap_pct",
        "largest_participant_id",
        "largest_participant_name",
        "has_data_anomaly",
        "ownership_structure",
        "evidence_count",
        "data_confidence",
        "delta_window_top10_pct",
        "delta_window_breadth_pct",
        "delta_window_hhi",
        "delta_window_participants",
        "availability_date",
    ]
    with open(
        out_dir / "institutional_concentration_panel.csv",
        "w",
        encoding="utf-8-sig",
        newline="",
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for symbol in sorted(panel["series"]):
            for rec in panel["series"][symbol]:
                writer.writerow(rec)
    if detail:
        with open(
            out_dir / "participant_detail.csv", "w", encoding="utf-8-sig", newline=""
        ) as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "symbol",
                    "date",
                    "holder_id",
                    "holder_name",
                    "holding_pct",
                    "holding_shares",
                ],
            )
            writer.writeheader()
            for row in detail:
                writer.writerow(
                    {
                        k: ("" if isinstance(v, float) and math.isnan(v) else v)
                        for k, v in row.items()
                    }
                )


def coverage_stats(
    holders: list[dict], series: dict, extra: dict | None = None
) -> dict:
    depths = [rec["seat_rows"] for sym in series for rec in series[sym]]
    stats = {
        "stock_days": sum(len(v) for v in series.values()),
        "symbols": sorted(series),
        "duplicates": (extra or {}).get("duplicates", 0),
        "missing_symbols": (extra or {}).get("missing_symbols", []),
        "depth_min_median_max": None
        if not depths
        else [min(depths), sorted(depths)[len(depths) // 2], max(depths)],
    }
    stats.update({k: v for k, v in (extra or {}).items() if k not in stats})
    return stats


# ------------------------------------------------------------------- main


def run_demo() -> None:
    rows = _demo_rows()
    holders = [
        {
            "symbol": r["symbol"],
            "date": r["date"],
            "holder_id": r["holder_id"],
            "holder_name": r["holder_name"],
            "holding_pct": _number(r["holding_pct"]),
            "holding_shares": math.nan,
        }
        for r in rows
    ]
    panel = build_panel(holders, PRIMARY_THRESHOLDS)
    scope = {
        "market": "DEMO",
        "universe": sorted(panel["series"]),
        "start": "2026-01-05",
        "end": "2026-01-07",
        "holder_definition": "演示席位（确定性 mock）",
    }
    semantics = {
        "source": "demo",
        "scale_detected": "percent_0_100",
        "dated_observations": True,
        "availability_lag_trading_days": AVAILABILITY_LAG_TRADING_DAYS,
        "thirteen_f_lag_days": THIRTEEN_F_LAG_DAYS,
    }
    report = assemble_report(
        "demo", panel, scope, semantics, coverage_stats(holders, panel["series"]), []
    )
    print(json.dumps(_json_safe(report), ensure_ascii=False, indent=2))


def run_input(args: argparse.Namespace) -> None:
    rows, meta = load_input_rows(args.input)
    holders, issues = rows, meta["issues"]
    panel = build_panel(holders, PRIMARY_THRESHOLDS, window=args.window)
    warnings = [{"reason": "input_issue", **issue} for issue in issues]
    if meta["scale"].get("mixed_scale_suspected"):
        warnings.append(
            {
                "reason": "mixed_scale_suspected",
                "note": "同时存在 ≤1 与 >1 的值；已按 0-100 解读，请复核源数据标度",
            }
        )
    scope = {
        "market": "custom",
        "universe": sorted({h["symbol"] for h in holders}),
        "start": min((h["date"] for h in holders), default=None),
        "end": max((h["date"] for h in holders), default=None),
        "holder_definition": args.holder_definition,
    }
    semantics = {
        "source": f"csv:{args.input}",
        "scale_detected": meta["scale"]["detected"],
        "dated_observations": True,
        "availability_lag_trading_days": AVAILABILITY_LAG_TRADING_DAYS,
        "thirteen_f_lag_days": THIRTEEN_F_LAG_DAYS,
        "normalization": "holding_pct = 持有人占公司总股本百分比（0-1 输入已自动 ×100）",
    }
    report = assemble_report(
        "input",
        panel,
        scope,
        semantics,
        coverage_stats(holders, panel["series"]),
        warnings,
    )
    emit(report, panel, holders if not args.no_detail else [], args)


def run_quantdb(args: argparse.Namespace) -> None:
    if args.market.upper() == "US":
        raise SystemExit(
            "US：本地无 13F/机构持仓数据源，不可用（方法论文档保留 45 天可得性滞后知识，"
            "见 references/methodology.md；接入 13F 后不得用报告期末日提前联结）"
        )
    if args.market.upper() == "CN":
        raise SystemExit(
            "CN：本地仅有 quantdb/3_financial_data/holder_num（股东户数），与机构集中度不是同一概念，"
            "本脚本不消费；仅 HK CCASS 支持 --quantdb（见 references/quantdb-ccass-map.md）"
        )
    base = resolve_data_root()
    start_s, end_s = _norm_range(args.start, args.end)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        raise SystemExit("--quantdb --market HK 需要 --symbols（如 0700.HK,0005.HK）")
    loaded = read_ccass(base, symbols, start_s, end_s)
    holders = loaded["holders"]
    panel = build_panel(holders, PRIMARY_THRESHOLDS, window=args.window)
    for symbol in panel["series"]:
        for rec in panel["series"][symbol]:
            ds = rec["date"].replace("-", "")
            rec["availability_date"] = loaded["availability_dates"].get(ds)
    denom = denominator_check(panel)
    south = southbound_check(base, holders, loaded["partitions"])
    warnings = []
    gapped = [
        {
            "symbol": s["symbol"],
            "missing_dates": s["missing_dates"],
            "count": s["missing_dates_count"],
        }
        for s in panel["summary"]
        if s["missing_dates_count"]
    ]
    if loaded["missing_symbols"]:
        warnings.append(
            {
                "reason": "symbol_not_in_window",
                "symbols": loaded["missing_symbols"],
                "note": "窗口内无该标的 CCASS 行（核对代码格式：四位+.HK）",
            }
        )
    if gapped:
        warnings.append(
            {
                "reason": "symbol_date_gaps",
                "symbols": gapped,
                "note": "某标的在部分分区日无 CCASS 行（真实覆盖缺口）；"
                "区间 Δ 只按该标的自身可用日期序列计",
            }
        )
    if loaded["duplicates"]:
        warnings.append({"reason": "duplicate_keys", "count": loaded["duplicates"]})
    if denom["inconsistent_count"]:
        warnings.append({"reason": "denominator_inconsistent", **denom})
    if south["unaligned_count"]:
        warnings.append({"reason": "southbound_unaligned", **south})
    scope = {
        "market": "HK",
        "universe": symbols,
        "start": args.start,
        "end": args.end,
        "holder_definition": "CCASS 前 50 托管参与者席位（按持有股数），holding_pct 占公司总股本",
    }
    semantics = {
        "source": "quanthk/2_base_sector/ccass_top50（HKEX CCASS 日度披露）",
        "scale_detected": "fraction_0_1（脚本已 ×100）",
        "dated_observations": True,
        "availability_lag_trading_days": AVAILABILITY_LAG_TRADING_DAYS,
        "availability_rule": "分区日 T 记 T+1 可得（availability_date 列）",
        "thirteen_f_lag_days": THIRTEEN_F_LAG_DAYS,
        "denominator_check": denom,
        "southbound_check": south,
    }
    report = assemble_report(
        "quantdb",
        panel,
        scope,
        semantics,
        coverage_stats(
            holders,
            panel["series"],
            {
                **{
                    k: v
                    for k, v in loaded.items()
                    if k not in ("holders", "availability_dates")
                },
                "date_gaps": gapped,
            },
        ),
        warnings,
    )
    emit(report, panel, holders if not args.no_detail else [], args)


def emit(
    report: dict, panel: dict, detail: list[dict], args: argparse.Namespace
) -> None:
    if args.out:
        write_outputs(Path(args.out), report, panel, detail)
    print(
        json.dumps(
            _json_safe(
                {
                    "status": report["status"],
                    "mode": report["mode"],
                    "coverage": report["coverage"],
                    "panel": report["panel"],
                    "anomalies": len(report["anomalies"]),
                    "label_flips": report["sensitivity"]["label_flips"],
                    "warnings": report["warnings"],
                    "ledger_status": {
                        item["gate"]: item["status"] for item in report["ledger"]
                    },
                    "out": args.out,
                }
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="机构持股集中度面板（CCASS 本地化移植）")
    p.add_argument("--demo", action="store_true", help="确定性离线引擎自检（纯标准库）")
    p.add_argument(
        "--input",
        help="任意 CSV：symbol,date,holder_id,holding_pct[,holder_name][,holding_shares]",
    )
    p.add_argument("--quantdb", action="store_true", help="读本地 HK CCASS（容器内跑）")
    p.add_argument("--market", default="HK", help="HK（US/CN 本地无源，显式拒绝）")
    p.add_argument(
        "--symbols", default="", help="逗号分隔，四位+.HK，如 0700.HK,0005.HK"
    )
    p.add_argument("--start", default="20260612")
    p.add_argument("--end", default="20260911")
    p.add_argument(
        "--window", type=int, default=20, help="区间变化窗口（可用交易日数，默认 20）"
    )
    p.add_argument("--holder-definition", default="用户提供的持股明细（占公司总股本%）")
    p.add_argument(
        "--no-detail", action="store_true", help="不写 participant_detail.csv"
    )
    p.add_argument(
        "--out", default="", help="输出目录（写 quality_report.json + 面板 CSV）"
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if args.demo:
        run_demo()
    elif args.input:
        run_input(args)
    elif args.quantdb:
        run_quantdb(args)
    else:
        raise SystemExit("需指定 --demo / --input / --quantdb 之一（--help 看用法）")


if __name__ == "__main__":
    main()
