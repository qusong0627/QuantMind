#!/usr/bin/env python3
"""P6 实时轨验收报告（生产机构测试仪器，只读）。

开盘后任一时间可跑；汇总六项验收证据并给出判定（闭市项如实标 N/A 不假绿）：

  A 订阅分片健康   —— 分片数/就绪/SDK/订阅数/静默点名（T-P6-02 分片修复验收）
  B 热集落地率     —— 抽样热集标的 market:snapshot 新鲜命中率（T-P6-02 验收）
  C 端到端时延     —— intel:latency P95<2s 判定（T-P6-05 验收）
  D 数据帧节拍     —— 抽样标的 market:series 相邻帧间隔 P95（T-P6-02 验收）
  E L0.5 留存      —— 最新分区日行数/文件/字节 vs 节拍推算（T-P6-04 验收）
  F 推理预算       —— 增量快车道实测 µs/只 → 500 只轮次估算（T-P6-07/T-P6-08 输入）

用法:
    python backend/scripts/p6_acceptance_report.py            # 人类可读
    python backend/scripts/p6_acceptance_report.py --json     # 机器可读（归档/看板）
    python backend/scripts/p6_acceptance_report.py --sample 80 --series-len 120

退出码：0=无 FAIL；1=存在 FAIL（可直接进 cron/CI）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

CST = timezone(timedelta(hours=8))
LATENCY_STAGE = "market_snapshot"
LATENCY_STAGE_FRESH = "market_snapshot_fresh"  # 新鲜档（≤5min）：验收口径
LATENCY_P95_BUDGET_MS = 2000.0
LANDING_RATE_MIN = 0.80  # 盘中抽样命中率下限（P6 验收口径）
FRESH_AGE_S = 300.0  # 快照新鲜阈值（与 freshness 默认 stale 窗口一致）


# ── 纯函数（可单测）───────────────────────────────────────────────────


def is_trading_now(now: datetime | None = None) -> bool:
    """A 股连续竞价时段（CST，周一至周五；不含节假日日历——节假日表现为抽样无数据 N/A）。"""
    now = now or datetime.now(tz=CST)
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return (9 * 60 + 30) <= minutes <= (11 * 60 + 30) or (13 * 60) <= minutes <= (15 * 60)


def interval_p95(scores: list[float]) -> float | None:
    """相邻时间戳间隔的 P95（秒，最近秩口径）；样本 <3 返回 None。"""
    if len(scores) < 3:
        return None
    ordered = sorted(scores)
    gaps = [b - a for a, b in zip(ordered, ordered[1:], strict=False)]
    gaps = [g for g in gaps if g > 0]
    if len(gaps) < 2:
        return None
    gaps.sort()
    rank = max(1, min(len(gaps), int(0.95 * len(gaps) + 0.999)))
    return float(gaps[rank - 1])


def grade_landing_rate(rate: float | None, *, trading: bool) -> tuple[str, str]:
    """落地率判定 → (level, message)。闭市（无对照意义）→ N/A。"""
    if not trading:
        return "N/A", "闭市观察（盘中才有落地语义）"
    if rate is None:
        return "WARN", "抽样无结果（远端键不可读?）"
    if rate >= LANDING_RATE_MIN:
        return "OK", f"落地率 {rate:.0%} ≥ {LANDING_RATE_MIN:.0%}"
    return "FAIL", f"落地率 {rate:.0%} < {LANDING_RATE_MIN:.0%}"


def grade_latency(stats: dict[str, Any] | None, *, trading: bool) -> tuple[str, str]:
    """时延判定：有样本即判 P95（T-P6-05）；无样本且盘中 → WARN。"""
    if not stats or not stats.get("samples"):
        return ("WARN", "无时延样本（采集打点未运行?）") if trading else ("N/A", "闭市无数据帧")
    p95 = float(stats.get("p95_ms") or 0.0)
    if p95 <= LATENCY_P95_BUDGET_MS:
        return "OK", f"P95={p95:.0f}ms ≤ {LATENCY_P95_BUDGET_MS:.0f}ms（n={stats.get('samples')}）"
    return "FAIL", f"P95={p95:.0f}ms > {LATENCY_P95_BUDGET_MS:.0f}ms"


def grade_cadence(p95_s: float | None, *, trading: bool, silence_s: float = 120.0) -> tuple[str, str]:
    """节拍判定：盘中 P95 间隔应小于静默阈值（默认 120s）；闭市 → N/A。"""
    if not trading:
        return "N/A", "闭市观察"
    if p95_s is None:
        return "WARN", "抽样序列不足（标的未在推?）"
    if p95_s <= silence_s:
        return "OK", f"帧间隔 P95={p95_s:.1f}s ≤ {silence_s:.0f}s"
    return "FAIL", f"帧间隔 P95={p95_s:.1f}s > {silence_s:.0f}s（疑似掉订阅/限流）"


def grade_shards(status: dict[str, Any] | None) -> tuple[str, str]:
    if not status:
        return "FAIL", "分片状态不可读"
    up, total = str(status.get("shards_up") or "0/0").split("/")
    if status.get("worker") == "up":
        return "OK", f"分片 {up}/{total} 全部就绪"
    if status.get("worker") == "degraded":
        return "WARN", f"分片 {up}/{total} 部分就绪（degraded）"
    return "FAIL", f"分片全部不可用（{status.get('shards_up')}）"


P6_RSS_BUDGET_MB = 4096.0  # 单容器 P6 相关进程 RSS 预算（T-P6-10 先算账；压测后校准）


def grade_resources(res: dict[str, Any] | None) -> tuple[str, str]:
    """P6 进程 RSS 汇总 vs 预算（>80% 预警、>100% 失败）；不可测 → N/A。"""
    if not res or res.get("p6_rss_mb") is None:
        return "N/A", "资源不可测（psutil 缺失?）"
    rss = float(res["p6_rss_mb"])
    detail = (
        f"P6 进程 RSS 合计 {rss:.0f}MB / 预算 {P6_RSS_BUDGET_MB:.0f}MB"
        f"（tdx worker×{res.get('tdx_workers')} + engine {res.get('engine_rss_mb')}MB）"
    )
    if rss > P6_RSS_BUDGET_MB:
        return "FAIL", detail + " 超预算"
    if rss > P6_RSS_BUDGET_MB * 0.8:
        return "WARN", detail + "（>80%）"
    return "OK", detail


# ── 采集（IO）────────────────────────────────────────────────────────


def _remote_redis():
    from backend.shared.remote_quote_config import resolve_remote_quote_redis

    resolved = resolve_remote_quote_redis()
    if resolved is None:
        return None
    import redis as redis_lib

    host, port, password, db = resolved
    return redis_lib.Redis(
        host=host, port=port, password=password, db=db,
        decode_responses=True, socket_connect_timeout=5, socket_timeout=10,
    )


def _main_redis():
    import os

    import redis as redis_lib

    return redis_lib.Redis(
        host=os.getenv("REDIS_HOST") or "redis",
        port=int(os.getenv("REDIS_PORT", "6379")),
        db=int(os.getenv("REDIS_DB", "0")),
        password=os.getenv("REDIS_PASSWORD") or None,
        decode_responses=True, socket_connect_timeout=3, socket_timeout=5,
    )


def collect_shards() -> dict[str, Any] | None:
    import asyncio

    from backend.shared.tdx_aidata.client import TdxAiDataCluster

    async def _run():
        cluster = TdxAiDataCluster()
        try:
            status = await cluster.status()
            sub = await cluster.subscription_status()
            return {"status": status, "subscription": sub}
        finally:
            await cluster.close()

    try:
        return asyncio.run(_run())
    except Exception:  # noqa: BLE001
        return None


def collect_landing(sample: int) -> dict[str, Any]:
    r = _remote_redis()
    if r is None:
        return {"readable": False}
    try:
        hot = sorted(r.smembers("qm:hot_set:symbols") or [])
        if not hot:
            return {"readable": True, "hot": 0, "sampled": 0, "fresh": 0, "rate": None}
        step = max(1, len(hot) // sample)
        picked = hot[::step][:sample]
        now = time.time()
        fresh = stale = missing = 0
        stale_list: list[str] = []
        for sym in picked:
            code, market = sym.split(".")
            key = f"market:snapshot:{market.lower()}{code}"
            data = r.hgetall(key)
            if not data:
                missing += 1
                continue
            ts = float(data.get("timestamp") or 0)
            age = now - ts if ts > 0 else 1e9
            if age <= FRESH_AGE_S:
                fresh += 1
            else:
                stale += 1
                if len(stale_list) < 10:
                    stale_list.append(f"{sym}({age:.0f}s)")
        total = len(picked)
        return {
            "readable": True, "hot": len(hot), "sampled": total,
            "fresh": fresh, "stale": stale, "missing": missing,
            "rate": (fresh / total) if total else None,
            "stale_sample": stale_list,
        }
    except Exception as exc:  # noqa: BLE001
        return {"readable": False, "error": str(exc)}
    finally:
        try:
            r.close()
        except Exception:  # noqa: BLE001
            pass


def collect_latency() -> dict[str, Any] | None:
    """验收口径 = 新鲜档（夜盘/停牌陈旧重放帧不计传输时延）；附全量档供对照。"""
    from backend.shared.latency_metrics import read_latency

    try:
        fresh = read_latency(LATENCY_STAGE_FRESH)
        all_frames = read_latency(LATENCY_STAGE)
        if fresh is None and all_frames is None:
            return None
        out = dict(fresh or {})
        if all_frames is not None:
            out["_all_frames"] = {
                k: all_frames.get(k) for k in ("samples", "p50_ms", "p95_ms", "max_ms", "stale_count")
            }
        return out
    except Exception:  # noqa: BLE001
        return None


def collect_cadence(sample: int, series_len: int) -> dict[str, Any]:
    r = _remote_redis()
    if r is None:
        return {"p95_s": None, "n_symbols": 0}
    try:
        hot = sorted(r.smembers("qm:hot_set:symbols") or [])
        if not hot:
            return {"p95_s": None, "n_symbols": 0}
        step = max(1, len(hot) // sample)
        picked = hot[::step][:sample]
        interval_p95s: list[float] = []
        for sym in picked:
            code, market = sym.split(".")
            rows = r.zrange(f"market:series:{market}{code}", -series_len, -1, withscores=True)
            p95 = interval_p95([float(s) for _, s in rows])
            if p95 is not None:
                interval_p95s.append(p95)
        overall = None
        if interval_p95s:
            ordered = sorted(interval_p95s)
            rank = max(1, min(len(ordered), int(0.95 * len(ordered) + 0.999)))
            overall = ordered[rank - 1]
        return {"p95_s": overall, "n_symbols": len(interval_p95s)}
    except Exception as exc:  # noqa: BLE001
        return {"p95_s": None, "n_symbols": 0, "error": str(exc)}
    finally:
        try:
            r.close()
        except Exception:  # noqa: BLE001
            pass


def collect_l05() -> dict[str, Any] | None:
    from backend.shared.l05_store import capacity_report, list_days

    try:
        days = list_days()
        if not days:
            return {"days": 0}
        report = capacity_report()
        return {"days": len(days), "latest": days[-1], "stats": report["days"][days[-1]]}
    except Exception:  # noqa: BLE001
        return None


def collect_engine_budget() -> dict[str, Any] | None:
    """增量快车道微基准（合成窗口，纯计算）。"""
    try:
        import numpy as np

        from backend.shared.feature_incremental import Window, compute_tier

        rng = np.random.default_rng(1)
        close = np.round(10 * np.cumprod(1 + rng.normal(0, 0.01, 46)), 3)
        win = Window(close, close * 1.01, close * 0.99, np.full(46, 1e6), np.full(46, 1e7))
        compute_tier(win)
        t0 = time.monotonic()
        for _ in range(500):
            compute_tier(win)
        per = (time.monotonic() - t0) / 500
        return {"per_symbol_ms": round(per * 1000, 3), "round_500_ms": round(per * 500 * 1000, 1)}
    except Exception:  # noqa: BLE001
        return None


def collect_resources() -> dict[str, Any] | None:
    """P6 进程资源汇总（T-P6-10 预算面）：tdx worker 群 + 主引擎进程 + cgroup 容器内存。"""
    try:
        import psutil

        tdx_rss = 0.0
        tdx_count = 0
        engine_rss = None
        main_pid = None
        for proc in psutil.process_iter(["pid", "name", "cmdline", "memory_info"]):
            try:
                cmdline = " ".join(proc.info.get("cmdline") or [])
                if "tdx_aidata.worker" in cmdline:
                    tdx_count += 1
                    tdx_rss += float(proc.info["memory_info"].rss) / 1e6
                elif (
                    "main_oss.py" in cmdline
                    and str(proc.info.get("name") or "").startswith("python")
                    and main_pid is None
                ):
                    # 只认 python 进程本体（cmdline 里 sh -c 包装壳 RSS≈0，误抓过一次）
                    main_pid = proc.info["pid"]
                    engine_rss = round(float(proc.info["memory_info"].rss) / 1e6, 1)
            except Exception:  # noqa: BLE001
                continue
        container_mb = None
        try:
            with open("/sys/fs/cgroup/memory.current") as f:
                container_mb = round(int(f.read().strip()) / 1e6, 1)
        except Exception:  # noqa: BLE001
            pass
        return {
            "tdx_workers": tdx_count,
            "tdx_rss_mb": round(tdx_rss, 1),
            "engine_rss_mb": engine_rss,
            "p6_rss_mb": round(tdx_rss + (engine_rss or 0.0), 1),
            "container_mem_mb": container_mb,
        }
    except Exception:  # noqa: BLE001
        return None


# ── 报告 ─────────────────────────────────────────────────────────────


def build_report(sample: int, series_len: int) -> dict[str, Any]:
    now = datetime.now(tz=CST)
    trading = is_trading_now(now)
    shards = collect_shards()
    landing = collect_landing(sample)
    latency = collect_latency()
    cadence = collect_cadence(sample, series_len)
    l05 = collect_l05()
    budget = collect_engine_budget()
    resources = collect_resources()

    verdicts: dict[str, dict[str, str]] = {}

    def _v(grade: tuple[str, str]) -> dict[str, str]:
        level, message = grade
        return {"level": level, "message": message}

    verdicts["A_shards"] = _v(grade_shards((shards or {}).get("status")))
    verdicts["B_landing"] = _v(grade_landing_rate(landing.get("rate"), trading=trading))
    verdicts["C_latency"] = _v(grade_latency(latency, trading=trading))
    verdicts["D_cadence"] = _v(grade_cadence(cadence.get("p95_s"), trading=trading))
    if l05 is None:
        verdicts["E_l05"] = {"level": "WARN", "message": "留存信息不可读"}
    elif l05.get("days", 0) == 0:
        verdicts["E_l05"] = {"level": "N/A", "message": "尚无分区（开盘首日 EOD 后出现）"}
    else:
        verdicts["E_l05"] = {
            "level": "OK",
            "message": f"最新分区 {l05['latest']} rows={l05['stats']['rows']:,} "
                       f"size={l05['stats']['bytes'] / 1e6:.1f}MB",
        }
    if budget is None:
        verdicts["F_engine"] = {"level": "WARN", "message": "快车道微基准不可运行"}
    else:
        verdicts["F_engine"] = {
            "level": "OK" if budget["round_500_ms"] < 5000 else "WARN",
            "message": f"快车道 {budget['per_symbol_ms']}ms/只 → 500 只 ≈{budget['round_500_ms']}ms",
        }
    verdicts["G_resources"] = _v(grade_resources(resources))
    has_fail = any(v["level"] == "FAIL" for v in verdicts.values())
    return {
        "generated_at": now.isoformat(),
        "trading": trading,
        "verdicts": verdicts,
        "has_fail": has_fail,
        "details": {
            "shards": shards,
            "landing": landing,
            "latency": latency,
            "cadence": cadence,
            "l05": l05,
            "engine_budget": budget,
            "resources": resources,
        },
    }


def _print_human(report: dict[str, Any]) -> None:
    icon = {"OK": "✓", "WARN": "!", "FAIL": "✗", "N/A": "—"}
    print(f"P6 实时轨验收报告  {report['generated_at']}  "
          f"({'盘中' if report['trading'] else '闭市'}观察窗)")
    print("-" * 72)
    for key in (
        "A_shards", "B_landing", "C_latency", "D_cadence", "E_l05", "F_engine", "G_resources",
    ):
        v = report["verdicts"][key]
        print(f" {icon.get(v['level'], '?')} {key:<12} {v['message']}")
    details = report["details"]
    sub = (details.get("shards") or {}).get("subscription") or {}
    if sub:
        per = ", ".join(
            f"s{s['shard']['id']}={s.get('subscribed')}"
            for s in sub.get("shards", [])
            if isinstance(s, dict) and s.get("shard")
        )
        print(f"\n 订阅：热集 {sub.get('counters', {}).get('hot_set_size', '-')} / "
              f"订阅合计 {sub.get('subscribed')}（{per}） 静默片 {sub.get('silent_shards') or '无'}")
    landing = details.get("landing") or {}
    if landing.get("readable"):
        print(f" 落地：抽样 {landing.get('sampled')} 命中 {landing.get('fresh')} "
              f"陈旧 {landing.get('stale')} 缺失 {landing.get('missing')}"
              + (f" 陈旧样例 {landing.get('stale_sample')}" if landing.get("stale_sample") else ""))
    latency = details.get("latency") or {}
    if latency.get("samples"):
        print(f" 时延(新鲜档)：n={latency.get('samples')} p50={latency.get('p50_ms')}ms "
              f"p95={latency.get('p95_ms')}ms max={latency.get('max_ms')}ms")
    allf = latency.get("_all_frames") or {}
    if allf.get("samples"):
        print(f" 时延(全量档)：n={allf.get('samples')} p95={allf.get('p95_ms')}ms "
              f"陈旧帧={allf.get('stale_count')}（夜盘重放，不计验收）")
    elif latency.get("_all_frames") is None and not latency.get("samples"):
        print(" 时延：无样本")
    print("-" * 72)
    print("结果：FAIL" if report["has_fail"] else "结果：通过（无 FAIL）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P6 实时轨验收报告（只读）")
    parser.add_argument("--sample", type=int, default=50, help="落地率/节拍抽样标的数")
    parser.add_argument("--series-len", type=int, default=120, help="节拍抽样每标的时序点数")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args(argv)

    report = build_report(args.sample, args.series_len)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        _print_human(report)
    return 1 if report["has_fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
