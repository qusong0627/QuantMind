#!/usr/bin/env python
"""热集行情覆盖探针（T-RT-03）：回答「597 只热集里，谁没有快照、为什么」。

只读，不写任何键。输出分档：

- ``fresh``   ≤ ``QM_QUOTE_FRESH_WITHIN_S``（默认 60s）
- ``stale``   ≤ ``QM_QUOTE_STALE_WITHIN_S``（默认 300s，仍是可用档）
- ``expired`` 键存在但时间戳超 300s（写过、停更了）
- ``missing`` 连键都没有（从未写入，或写入后键已过期——与 ``never_seen`` 合看可分因）

按代码段分组（沪市 6xx / 深市 0xx、3xx / 北交所 4xx、8xx / 指数），因为「指数不写、
个股写」和「整段都不写」是两种故障。

用法：
    python backend/scripts/realtime_coverage_probe.py            # 文本表
    python backend/scripts/realtime_coverage_probe.py --json     # 机读（定时采集）
    python backend/scripts/realtime_coverage_probe.py --list-missing 30
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from typing import Any

_PREFIX_RE = re.compile(r"^(SH|SZ|BJ)(\d{6})$", re.IGNORECASE)
_SUFFIX_RE = re.compile(r"^(\d{6})\.(SH|SZ|BJ)$", re.IGNORECASE)
_BARE_RE = re.compile(r"^\d{6}$")

FRESH_WITHIN_S = float(os.getenv("QM_QUOTE_FRESH_WITHIN_S", "60") or 60)
STALE_WITHIN_S = float(os.getenv("QM_QUOTE_STALE_WITHIN_S", "300") or 300)


def normalize_symbol(symbol: str) -> tuple[str, str] | None:
    """任意写法 → ``(6 位代码, 交易所)``；无法识别返回 None。

    ``000001.SH``（指数）与 ``000001.SZ``（平安银行）**不可折叠**——这正是
    hot_set 与快照键容易对不上的地方（2026-11-06 实测两只指数长期无快照）。
    """
    raw = str(symbol or "").strip()
    match = _SUFFIX_RE.match(raw) or _PREFIX_RE.match(raw)
    if match:
        code, exchange = (
            (match.group(1), match.group(2))
            if _SUFFIX_RE.match(raw)
            else (match.group(2), match.group(1))
        )
        return code, exchange.upper()
    if _BARE_RE.match(raw):
        head = raw[0]
        exchange = (
            "SH" if head in {"5", "6", "9"} else "BJ" if head in {"4", "8"} else "SZ"
        )
        return raw, exchange
    return None


def bucket_of(code: str, exchange: str) -> str:
    """代码段（与交易/行情侧的品类划分同口径）。"""
    if (exchange == "SH" and code.startswith("000")) or (
        exchange == "SZ" and code.startswith("399")
    ):
        return "index"
    if exchange == "BJ":
        return "bj"
    if code.startswith("6"):
        return "sh_main"
    if code.startswith(("0", "3")):
        return "sz_main"
    return "other"


def classify(
    hot: list[str],
    snapshots: dict[tuple[str, str], dict[str, Any]],
    *,
    now: float,
) -> dict[str, Any]:
    """纯函数：热集 + 快照键 → 分档报告（便于单测与定时采集）。"""
    buckets: dict[str, dict[str, int]] = {}
    symbols: list[dict[str, Any]] = []
    counts = {"fresh": 0, "stale": 0, "expired": 0, "missing": 0}
    for raw in sorted(set(hot)):
        ident = normalize_symbol(raw)
        if ident is None:
            counts["missing"] += 1
            symbols.append({"symbol": raw, "bucket": "other", "state": "unparsed"})
            continue
        bucket = bucket_of(*ident)
        stats = buckets.setdefault(
            bucket, {"total": 0, "fresh": 0, "stale": 0, "expired": 0, "missing": 0}
        )
        stats["total"] += 1
        snap = snapshots.get(ident)
        age: float | None = None
        if snap is None:
            state = "missing"
        else:
            ts = _snapshot_ts(snap)
            if ts is None or ts <= 0:
                state = "expired"
            else:
                age = now - ts
                if age <= FRESH_WITHIN_S:
                    state = "fresh"
                elif age <= STALE_WITHIN_S:
                    state = "stale"
                else:
                    state = "expired"
        counts[state] += 1
        stats[state] += 1
        symbols.append(
            {
                "symbol": raw,
                "bucket": bucket,
                "state": state,
                "age_s": round(age, 1) if age is not None else None,
            }
        )
    total = sum(counts.values())
    usable = counts["fresh"] + counts["stale"]
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "now_epoch": int(now),
        "hot_total": total,
        "snapshot_keys": len(snapshots),
        "counts": counts,
        "usable_ratio": round(usable / total, 4) if total else None,
        "fresh_within_s": FRESH_WITHIN_S,
        "stale_within_s": STALE_WITHIN_S,
        "buckets": buckets,
        "symbols": symbols,
    }


def _snapshot_ts(snap: dict[str, Any]) -> float | None:
    raw = snap.get("timestamp") or snap.get("ts")
    try:
        ts = float(raw)
    except (TypeError, ValueError):
        return None
    if ts > 1e12:  # 毫秒
        ts /= 1000.0
    return ts


def _redis_client(db: int):
    import redis as redis_lib

    return redis_lib.Redis(
        host=os.getenv("REDIS_HOST") or "redis",
        port=int(os.getenv("REDIS_PORT", "6379")),
        password=os.getenv("REDIS_PASSWORD") or None,
        db=db,
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=3,
    )


def collect() -> dict[str, Any]:
    """读热集（db0）+ 快照（远端行情库，默认 db3）→ 报告。"""
    hot_db = int(os.getenv("REDIS_DB", "0") or 0)
    quote_db = int(os.getenv("REMOTE_QUOTE_REDIS_DB", "3") or 3)
    hot_client = _redis_client(hot_db)
    quote_client = _redis_client(quote_db)
    try:
        from backend.shared.hot_set_store import hot_set_key

        hot = list(hot_client.smembers(hot_set_key()) or [])
        snapshots: dict[tuple[str, str], dict[str, Any]] = {}
        for key in quote_client.scan_iter("market:snapshot:*", count=500):
            ident = normalize_symbol(str(key).rsplit(":", 1)[-1])
            if ident is None:
                continue
            snapshots[ident] = quote_client.hgetall(key) or {}
        report = classify(hot, snapshots, now=time.time())
        status = hot_client.hgetall("qm:realtime:infer:status") or {}
        try:
            report["engine_counters"] = json.loads(status.get("counters") or "{}")
        except (TypeError, ValueError):
            report["engine_counters"] = {}
        return report
    finally:
        hot_client.close()
        quote_client.close()


def render(report: dict[str, Any], *, list_missing: int = 0) -> str:
    counts = report["counts"]
    lines = [
        f"热集 {report['hot_total']} 只 · 快照键 {report['snapshot_keys']} · "
        f"可用 {counts['fresh'] + counts['stale']} "
        f"({(report['usable_ratio'] or 0) * 100:.1f}%)",
        f"  fresh≤{report['fresh_within_s']:.0f}s {counts['fresh']} · "
        f"stale≤{report['stale_within_s']:.0f}s {counts['stale']} · "
        f"expired {counts['expired']} · missing {counts['missing']}",
    ]
    engine = report.get("engine_counters") or {}
    if engine:
        lines.append(
            "  引擎计数: never_seen={never} · 前进={adv} / 冻结={frozen} · "
            "覆盖率={cov}".format(
                never=engine.get("never_seen"),
                adv=engine.get("quote_advanced"),
                frozen=engine.get("quote_frozen"),
                cov=engine.get("last_live_coverage"),
            )
        )
    lines.append("  按代码段：")
    for bucket, stats in sorted(report["buckets"].items()):
        lines.append(
            f"    {bucket:8s} total={stats['total']:4d} fresh={stats['fresh']:4d} "
            f"stale={stats['stale']:4d} expired={stats['expired']:3d} "
            f"missing={stats['missing']:4d}"
        )
    if list_missing:
        missing = [s for s in report["symbols"] if s["state"] == "missing"]
        missing.sort(key=lambda s: s["bucket"])
        lines.append(f"  缺失样本（前 {list_missing}）：")
        for item in missing[:list_missing]:
            lines.append(f"    {item['symbol']}  {item['bucket']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="热集行情覆盖探针（只读）")
    parser.add_argument("--json", action="store_true", help="输出 JSON（供定时采集）")
    parser.add_argument(
        "--list-missing", type=int, default=0, help="列出前 N 个缺失标的"
    )
    args = parser.parse_args(argv)
    try:
        report = collect()
    except Exception as exc:  # noqa: BLE001 - 探针失败要如实退非零
        print(f"探针失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if args.json:
        payload = {k: v for k, v in report.items() if k != "symbols"}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render(report, list_missing=args.list_missing))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
