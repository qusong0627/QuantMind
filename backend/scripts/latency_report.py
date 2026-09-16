#!/usr/bin/env python3
"""端到端时延报表（T-P6-05）：读 ``intel:latency:*`` 滚动统计（主 Redis db0）。

口径：latency_ms = 可消费时刻 - 源时间戳；P95 目标 < 2000ms（P6 完成定义）。
写入面在 ``backend/shared/latency_metrics.py``（订阅采集 worker 内嵌打点）。

用法:
    # 全部 stage 一览（当前窗口统计）
    python backend/scripts/latency_report.py

    # 单 stage 深看：最近 12 个 flush 的 P95 趋势
    python backend/scripts/latency_report.py --stage market_snapshot --series 12

    # JSON 输出（供监控/看板采集）
    python backend/scripts/latency_report.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.shared.latency_metrics import read_all, read_latency, read_series  # noqa: E402


def _fmt_age(updated_at) -> str:
    try:
        age = max(0, int(time.time() - float(updated_at)))
    except (TypeError, ValueError):
        return "-"
    if age < 60:
        return f"{age}s前"
    if age < 3600:
        return f"{age // 60}m前"
    return f"{age // 3600}h前"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="intel:latency 时延报表（T-P6-05）")
    parser.add_argument("--stage", help="只看指定 stage")
    parser.add_argument("--series", type=int, default=0, help="同时打印最近 N 个 flush 的 P95 趋势")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args(argv)

    try:
        if args.stage:
            stats = read_latency(args.stage)
            rows = {args.stage: stats} if stats else {}
        else:
            rows = read_all()
    except Exception as exc:  # noqa: BLE001
        print(f"[latency] Redis 读取失败: {exc}", file=sys.stderr)
        return 1

    if not rows:
        print("[latency] 无任何 intel:latency:* 记录（采集打点未运行或尚未有数据帧）")
        return 0

    if args.json:
        out = {"stages": rows}
        if args.stage and args.series:
            out["series"] = read_series(args.stage, limit=args.series)
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return 0

    print(f"{'stage':<22} {'samples':>7} {'p50':>8} {'p95':>8} {'max':>9} {'avg':>8} "
          f"{'future':>6} {'total':>9}  updated")
    for stage, stats in sorted(rows.items()):
        print(
            f"{stage:<22} {stats.get('samples', 0):>7} "
            f"{stats.get('p50_ms', 0):>8.1f} {stats.get('p95_ms', 0):>8.1f} "
            f"{stats.get('max_ms', 0):>9.1f} {stats.get('avg_ms', 0):>8.1f} "
            f"{stats.get('future_count', 0):>6} {stats.get('total_count', 0):>9} "
            f" {_fmt_age(stats.get('updated_at'))}"
        )

    if args.stage and args.series:
        series = read_series(args.stage, limit=args.series)
        if series:
            print(f"\n{args.stage} 最近 {len(series)} 个 flush（P95 ms）:")
            for point in series:
                stamp = time.strftime("%H:%M:%S", time.localtime(point.get("at") or 0))
                print(
                    f"    {stamp}  p50={point.get('p50_ms')} p95={point.get('p95_ms')} "
                    f"max={point.get('max_ms')} n={point.get('samples')}"
                )
        else:
            print(f"\n{args.stage} 无趋势序列")
    return 0


if __name__ == "__main__":
    sys.exit(main())
