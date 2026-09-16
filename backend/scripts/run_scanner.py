#!/usr/bin/env python3
"""机会扫描 CLI（T-P4-01）：注册表全路批扫描 → 合并机会池。

用法（容器内）:
    python backend/scripts/run_scanner.py                      # 最新交易日的模型信号扫描
    python backend/scripts/run_scanner.py --date 2026-09-15 --strategy aggressive
    python backend/scripts/run_scanner.py --json --top 20
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))


async def _run(args) -> dict:
    from backend.services.engine.scanners.runner import run_scan

    return await run_scan(trade_date=args.date, strategy=args.strategy, mode=args.mode)


def main() -> int:
    parser = argparse.ArgumentParser(description="机会扫描 CLI（T-P4-01）")
    parser.add_argument(
        "--date", default=None, help="交易日 YYYY-MM-DD（缺省=最新信号日）"
    )
    parser.add_argument(
        "--strategy",
        default="balanced",
        choices=("conservative", "balanced", "aggressive"),
    )
    parser.add_argument(
        "--mode",
        default="quantile",
        choices=("quantile", "absolute"),
        help="阈值口径：quantile=分位自适应（默认，T-P4-03）/ absolute=存量绝对口径",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--top", type=int, default=20)
    args = parser.parse_args()

    report = asyncio.run(_run(args))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print(f"扫描时刻: {report['as_of']} ｜ 策略预设: {report['strategy']}")
    for meta in report["meta"]:
        if meta.get("note"):
            print(f"  [{meta.get('scanner')}] {meta['note']}")
        else:
            print(
                f"  [{meta.get('scanner')}] {meta.get('trade_date')} "
                f"市场状态={meta.get('market_state')} avgTop1={meta.get('avg_top1')} "
                f"强行业={meta.get('strong_industry_count')} 选中={meta.get('picked')}"
            )
    print(f"\n机会池（{len(report['opportunities'])} 条，Top {args.top}）:")
    print(f"{'symbol':<12}{'score':>6}{'strength':>10}  行业 / 融合分 / 趋势")
    for o in report["opportunities"][: args.top]:
        ev = o.get("evidence") or {}
        print(
            f"{o['symbol']:<12}{o['score']:>6}{o['strength']:>10.4f}  "
            f"{str(ev.get('industry') or '-')[:10]:<12}{ev.get('fusion_score')} / {ev.get('trend')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
