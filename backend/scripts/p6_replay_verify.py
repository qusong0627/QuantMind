#!/usr/bin/env python3
"""P6 可回放复现验收（T-P6-09 硬验收）：账本 + L0.5 归档 → 逐周期 diff=0 判定。

用法:
    # 校验某交易日（账本取 Redis qm:realtime:infer:ledger:{YYYYMMDD}）
    python backend/scripts/p6_replay_verify.py --date 20260917 --model-dir /app/models/users/.../mdl_xxx

    # 离线账本（JSON 行文件）与 JSON 输出
    python backend/scripts/p6_replay_verify.py --date 20260917 --model-dir ... \
        --ledger-file /tmp/ledger.jsonl --json

退出码：0=diff 全 0（通过）；1=存在不一致或数据缺失（阻断）。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))


def _parse_day(raw: str) -> date:
    text = str(raw).replace("-", "")
    return date(int(text[:4]), int(text[4:6]), int(text[6:8]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P6 回放复现验收（diff=0 为通过）")
    parser.add_argument("--date", required=True, help="交易日 YYYYMMDD")
    parser.add_argument("--model-dir", required=True, help="模型目录（metadata.json + ONNX/pkl）")
    parser.add_argument("--ledger-file", help="离线账本（JSONL；缺省从 Redis 读）")
    parser.add_argument("--l05-dir", help="L0.5 数据目录（缺省 /data/l05_snapshots）")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    parser.add_argument("--limit", type=int, default=20, help="不一致明细上限")
    args = parser.parse_args(argv)

    day = _parse_day(args.date)
    day_key = day.strftime("%Y%m%d")

    from backend.services.engine.inference.replay_verifier import load_ledger, verify_day
    from backend.shared.l05_store import DEFAULT_BASE_DIR, read_day

    if args.ledger_file:
        ledger = []
        for line in Path(args.ledger_file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                ledger.append(json.loads(line))
    else:
        ledger = load_ledger(day_key)

    frames = read_day(day, base_dir=args.l05_dir or DEFAULT_BASE_DIR)
    report = verify_day(
        day=day, model_dir=args.model_dir, ledger=ledger, frames=frames,
        detail_limit=args.limit,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        if report.get("reason"):
            print(f"[replay] {day_key}: 无法验收 —— {report['reason']}")
            return 1
        status = "PASS（diff=0）" if report["diff_zero"] else "FAIL"
        print(
            f"[replay] {day_key} {status}：{report['matched']}/{report['entries']} 周期一致"
            f"（模型 {report['model_dir']}）"
        )
        for item in report.get("mismatch_details", [])[: args.limit]:
            diag = item.get("diagnostics") or {}
            print(
                f"    ✗ ts={item.get('ts')} n={item['n']} snap={item['snapshots_used']} "
                f"x_mean {diag.get('x_mean_live')}→{diag.get('x_mean_replay')} "
                f"scores_mean {diag.get('scores_mean_live')}→{diag.get('scores_mean_replay')}"
            )
    return 0 if report.get("diff_zero") else 1


if __name__ == "__main__":
    sys.exit(main())
