#!/usr/bin/env python3
"""机构集中度报告验收闸门（源技能 harness.py 的本地化改写，纯标准库）。

来源：quantskills/skill-hk-us-institutional-concentration（源仓库许可为空/未声明）。
用途：inst_concentration.py 落盘后独立复检 —— 交付前必须 PASS。

用法：
  python3 verify_report.py --out <inst_concentration.py 的输出目录>

检查项（任一失败 → 退出码 1，不得交付）：
  1  required_files            质量报告 + 面板 CSV 存在
  2  nonempty_panel            面板非空
  3  required_columns          面板契约列齐全
  4  unique_keys               (symbol,date) 唯一
  5  valid_structure            结构标签在词表内
  6  valid_confidence           置信度在 {low,medium,high}
  7  anomalies_explicit         越界 breadth 行必须 has_data_anomaly=True（异常不得静默）
  8  breadth_coverage           有效广度覆盖率 >= 90%
  9  sensitivity_documented     报告含 >=2 套阈值 + 翻转清单
 10  ledger_complete            证据台账 7 门齐全且状态合法
 11  availability_documented    时点可用性口径已记录（HK CCASS T+1；13F 45 天知识保留）
 12  denominator_consistent     总股本互推抽查无未列明的不一致
 13  quality_report_ok          质量报告 status ∈ {PASS,WARN} 且台账无 fail 门
                                 （WARN=有已显式标注的异常，不阻断交付；FAIL/台账 fail 阻断）
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

STRUCTURE_LABELS = {
    "broad_participation",
    "dominant_seat",
    "fragmented_or_mixed",
    "data_anomaly",
    "insufficient_data",
}
CONFIDENCE_LABELS = {"low", "medium", "high"}
REQUIRED_COLUMNS = {
    "symbol",
    "date",
    "breadth_pct",
    "top1_pct",
    "top5_pct",
    "top10_pct",
    "holder_hhi",
    "participant_count",
    "ownership_structure",
    "evidence_count",
    "data_confidence",
    "has_data_anomaly",
}
LEDGER_GATES = {
    "scope",
    "semantics",
    "coverage",
    "structure",
    "anomalies",
    "sensitivity",
    "interpretation",
}
TRUTHY = {"true", "1", "yes"}


def _bool(value: str) -> bool:
    return str(value).strip().lower() in TRUTHY


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, help="inst_concentration.py 的输出目录")
    args = parser.parse_args()
    root = Path(args.out)
    panel_path = root / "institutional_concentration_panel.csv"
    quality_path = root / "quality_report.json"

    checks: dict[str, bool] = {
        "required_files": panel_path.is_file() and quality_path.is_file(),
    }
    detail: dict[str, object] = {}
    if checks["required_files"]:
        quality = json.loads(quality_path.read_text(encoding="utf-8"))
        with open(panel_path, encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        columns = set(rows[0]) if rows else set()
        checks["nonempty_panel"] = bool(rows)
        checks["required_columns"] = REQUIRED_COLUMNS.issubset(columns)
        keys = [(r.get("symbol"), r.get("date")) for r in rows]
        checks["unique_keys"] = len(keys) == len(set(keys))
        checks["valid_structure"] = bool(rows) and all(
            r.get("ownership_structure") in STRUCTURE_LABELS for r in rows
        )
        checks["valid_confidence"] = bool(rows) and all(
            (r.get("data_confidence") or "") in CONFIDENCE_LABELS for r in rows
        )
        over = [
            r
            for r in rows
            if r.get("breadth_pct") not in (None, "")
            and not (-1e-9 <= float(r["breadth_pct"]) <= 100.0001)
        ]
        checks["anomalies_explicit"] = all(
            _bool(r.get("has_data_anomaly", "")) for r in over
        )
        detail["out_of_range_rows"] = len(over)
        valid = [r for r in rows if r.get("breadth_pct") not in (None, "")]
        checks["breadth_coverage"] = bool(rows) and len(valid) / len(rows) >= 0.90

        sensitivity = quality.get("sensitivity", {})
        threshold_sets = sensitivity.get("threshold_sets", {})
        checks["sensitivity_documented"] = (
            len(threshold_sets) >= 2 and "label_flips" in sensitivity
        )

        ledger = quality.get("ledger", [])
        checks["ledger_complete"] = {
            item.get("gate") for item in ledger
        } == LEDGER_GATES and all(
            item.get("status") in {"ok", "warn", "fail"} for item in ledger
        )

        semantics = quality.get("semantics", {})
        checks["availability_documented"] = (
            semantics.get("availability_lag_trading_days") == 1
            and semantics.get("thirteen_f_lag_days") == 45
            and "source" in semantics
        )

        denom = semantics.get("denominator_check")
        if denom is None:  # --input 模式无总股本互推（本地 CSV 可能只有百分比）
            checks["denominator_consistent"] = True
            detail["denominator_check"] = "not_applicable(input mode)"
        else:
            checks["denominator_consistent"] = denom.get("inconsistent_count", 0) == 0
            detail["denominator_check"] = denom

        checks["quality_report_ok"] = quality.get("status") in {"PASS", "WARN"} and all(
            item.get("status") != "fail" for item in ledger
        )
        detail["status"] = quality.get("status")
        detail["panel_rows"] = len(rows)

    status = "PASS" if checks and all(checks.values()) else "FAIL"
    result = {"status": status, "checks": checks, "detail": detail}
    report_path = root / "harness_report.json"
    if root.is_dir():
        report_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if status == "PASS" else 1)


if __name__ == "__main__":
    main()
