#!/usr/bin/env python3
"""风控影子报告（T-RC-02 翻闸评估工具）：聚合 ``qm:risk:decisions`` 命中分布。

用途：影子期（shadow=true，判定留痕不拦单）积累数据后，翻闸前审阅——
样本量（尤其**盘中申报时段**样本）、verdict 分布、各规则会拦/告警条数、逐日趋势。

翻闸建议阈值（机构口径，仅作初筛，最终人工审阅）：
- 盘中样本 ≥ 50 单且 ≥ 1 个完整交易日 → "可进入人工审阅"；
- 盘中样本不足 → "继续累积"（盘后入队单（queued_intent）不计入盘中判定样本）。

用法（容器内）:
    python backend/scripts/risk_shadow_report.py [--days 7] [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_CST = ZoneInfo("Asia/Shanghai")
MIN_INTRADAY_SAMPLES = 50


def _redis():
    import redis as _redis_lib

    return _redis_lib.Redis(
        host=os.getenv("REDIS_HOST") or "redis",
        port=int(os.getenv("REDIS_PORT", "6379")),
        password=os.getenv("REDIS_PASSWORD") or None,
        db=int(os.getenv("REDIS_DB_TRADE", "2")),
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=5,
    )


def _in_session(ts: float) -> bool:
    from backend.shared.risk.builtin_rules import CN_SESSION_DEFAULT, _hm_ok

    return _hm_ok(datetime.fromtimestamp(ts, tz=_CST).strftime("%H:%M"), CN_SESSION_DEFAULT)


def collect(days: int = 7, *, today: datetime | None = None) -> dict[str, Any]:
    """聚合近 days 天决策流 → 报告 dict（纯读）。"""
    today = today or datetime.now(tz=_CST)
    client = _redis()
    entries: list[dict[str, Any]] = []
    per_day: dict[str, int] = {}
    try:
        for i in range(int(days)):
            day = (today - timedelta(days=i)).strftime("%Y%m%d")
            key = f"qm:risk:decisions:{day}"
            try:
                rows = client.xrange(key)
            except Exception:
                continue
            per_day[day] = len(rows)
            for _eid, fields in rows:
                doc: dict[str, Any] = {}
                for k, v in fields.items():
                    try:
                        doc[k] = json.loads(v)
                    except (TypeError, ValueError):
                        doc[k] = v
                entries.append(doc)
    finally:
        try:
            client.close()
        except Exception:
            pass

    verdicts = Counter(str(e.get("verdict") or "?") for e in entries)
    rule_reject = Counter()
    rule_warn = Counter()
    sources = Counter(str(e.get("source") or "?") for e in entries)
    intr_samples = 0
    intraday_verdicts = Counter()
    for e in entries:
        try:
            ts = float(e.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts and _in_session(ts):
            intr_samples += 1
            intraday_verdicts[str(e.get("verdict") or "?")] += 1
        for d in e.get("decisions") or []:
            action = str(d.get("action") or "")
            rule = str(d.get("rule_id") or "?")
            if action == "REJECT":
                rule_reject[rule] += 1
            elif action == "WARN":
                rule_warn[rule] += 1

    would_block = verdicts.get("reject", 0) + verdicts.get("halt", 0)
    if not entries:
        advice = "无样本：影子尚未评估任何订单（检查订单是否过 OrderRouter / 直连闸接线）"
    elif intr_samples < MIN_INTRADAY_SAMPLES:
        advice = (
            f"盘中样本不足（{intr_samples} < {MIN_INTRADAY_SAMPLES}）：继续累积，"
            "盘后再审（盘后入队单不构成申报时段判定样本）"
        )
    else:
        advice = "盘中样本达标：人工审阅规则命中与误伤后，可考虑 shadow=false 翻闸（带版本号）"

    return {
        "days": int(days),
        "total": len(entries),
        "intraday_samples": intr_samples,
        "verdicts": dict(verdicts),
        "intraday_verdicts": dict(intraday_verdicts),
        "would_block": would_block,
        "rule_reject": dict(rule_reject.most_common()),
        "rule_warn": dict(rule_warn.most_common()),
        "sources": dict(sources.most_common()),
        "per_day": per_day,
        "advice": advice,
    }


def _print_human(rep: dict[str, Any]) -> None:
    print(f"=== 风控影子报告（近 {rep['days']} 天）===")
    print(f"样本总量: {rep['total']}（盘中 {rep['intraday_samples']}）")
    print(f"verdict: {rep['verdicts']}  （会拦 {rep['would_block']}）")
    if rep["intraday_verdicts"]:
        print(f"盘中 verdict: {rep['intraday_verdicts']}")
    print(f"来源分布: {rep['sources']}")
    print("规则命中（会拦 REJECT）:")
    for rule, n in (rep["rule_reject"] or {}).items():
        print(f"  {rule}: {n}")
    print("规则告警（WARN）:")
    for rule, n in (rep["rule_warn"] or {}).items():
        print(f"  {rule}: {n}")
    print(f"逐日: {rep['per_day']}")
    print(f"翻闸建议: {rep['advice']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="风控影子报告（T-RC-02 翻闸评估）")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    rep = collect(days=args.days)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        _print_human(rep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
