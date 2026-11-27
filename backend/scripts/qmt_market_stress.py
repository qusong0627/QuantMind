"""QMT 行情压力与稳定性压测（盘中使用；只读，不下单）。

用法（容器内）：

    # 全市场单次快照持续 60s + 分片模式 30s + 数据覆盖检查
    python /app/backend/scripts/qmt_market_stress.py --mode all --duration 60

    # 并发压力（默认 2 个进程；桥的市场队列上限=2，超过会 BRIDGE_BUSY）
    python /app/backend/scripts/qmt_market_stress.py --mode concurrent --workers 2 --duration 30

    # 账户查询压力（默认不做；盘中慎用，会占用 adjust 主线程）
    python /app/backend/scripts/qmt_market_stress.py --mode trade --duration 30

    # 行情压力 + 交易查询并发：验证行情压测是否拖慢 get_asset
    # （先确认 account_ok=1；账户本来就挂时只会测出超时）
    python /app/backend/scripts/qmt_market_stress.py --mode mixed --duration 30

输出：终端汇总 + 可选 ``--json-out /data/exports/qmt_stress.json``。

判据（2026-11-27 基线，5,225 只沪深A股）：
* 单次全市场 ``get_full_tick`` ≈ 0.45–0.50s；60s 持续 ≈ 2.0 次/秒、0 失败。
* 500 只/批 ≈ 0.45–0.85s/轮，p95 ≈ 120ms。
* 并发：2 进程 × 单次全市场 ≈ 0.86s/次、0 背压；**3 进程会触发桥的
  ``BRIDGE_BUSY: market queue backlog=2 >= 2``**（设计内背压，不是故障）。
  脚本把 ``BRIDGE_BUSY`` 单独计数，不计入硬失败。
* 下单保护：订单方法不走限速/缓存；``budget.market_preempt_queue=2`` 让行情请求
  在队列积压 ≥2 时先被 ``BRIDGE_BUSY`` 拒掉，订单积压闸是 ≥3，所以**行情压测
  不会把订单饿死**；但行情压力下订单的端到端延迟未实测，``--mode mixed`` 用
  ``get_asset`` 做同主线程代理测量（真正下单仍需人工在盘中另验）。
* ``get_market_data_ex(period='tick')`` **只覆盖本地数据包/订阅过的代码**：
  实测 000001.SZ 当天有 4,809 行 tick，600036/000300 为 0 行；实时 tick 线
  请用 ``get_full_tick`` 轮询落库。
* 日线/分钟线只覆盖 QMT 本地数据包里的代码（实测当天只有 493/5,225 只
  ``volume>0``），**当天全市场日线不要以 QMT 为源**。
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import statistics
import sys
import time
from typing import Any

from backend.scripts.qmt_api.client import client

REDIS_DB = 5
METRICS_KEY = "qm:qmt:pipe:metrics"
WATCHDOG_KEY = "qm:qmt:strategy:watchdog"


def _pct(ordered: list[float], q: float) -> float:
    if not ordered:
        return 0.0
    return ordered[min(len(ordered) - 1, int(len(ordered) * q))]


def _lat_stats(lat: list[float]) -> dict[str, float]:
    ordered = sorted(lat)
    if not ordered:
        return {"calls": 0}
    return {
        "calls": len(ordered),
        "avg_s": round(statistics.mean(ordered), 4),
        "min_s": round(ordered[0], 4),
        "p50_s": round(_pct(ordered, 0.50), 4),
        "p95_s": round(_pct(ordered, 0.95), 4),
        "p99_s": round(_pct(ordered, 0.99), 4),
        "max_s": round(ordered[-1], 4),
    }


def _is_backpressure(exc: Exception) -> bool:
    return "BRIDGE_BUSY" in str(exc)


def fetch_universe(rpc) -> list[str]:
    codes = rpc.call("get_stock_list_in_sector", {"sector_name": "沪深A股"})
    return list(codes or [])


def read_metrics() -> dict[str, Any]:
    """桥指标 + 策略看门狗（读不到返回空）。"""
    try:
        import redis

        conn = redis.Redis(
            host=os.getenv("REDIS_HOST") or "redis",
            port=int(os.getenv("REDIS_PORT") or 6379),
            db=REDIS_DB,
            password=os.getenv("REDIS_PASSWORD") or None,
            socket_timeout=3,
        )
        metrics = conn.hgetall(METRICS_KEY) or {}
        watchdog = conn.hgetall(WATCHDOG_KEY) or {}
        return {
            "metrics": {_text(k): _text(v) for k, v in metrics.items()},
            "watchdog": {_text(k): _text(v) for k, v in watchdog.items()},
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def run_quote_single(rpc, codes: list[str], duration: float) -> dict[str, Any]:
    lat: list[float] = []
    ticks = 0
    backpressure = 0
    failures: list[str] = []
    last_timetag = ""
    end = time.perf_counter() + duration
    while time.perf_counter() < end:
        t0 = time.perf_counter()
        try:
            data = rpc.call("get_full_tick", {"codes": codes})
            lat.append(time.perf_counter() - t0)
            ticks += len(data or {})
            first = next(iter((data or {}).values()), None) or {}
            last_timetag = str(first.get("timetag") or last_timetag)
        except Exception as exc:  # noqa: BLE001
            if _is_backpressure(exc):
                backpressure += 1
            else:
                failures.append(f"{type(exc).__name__}: {str(exc)[:160]}")
    return {
        "mode": "single",
        "codes": len(codes),
        "ticks": ticks,
        "backpressure": backpressure,
        "failures": failures,
        "last_timetag": last_timetag,
        **_lat_stats(lat),
    }


def run_quote_chunk(rpc, codes: list[str], duration: float, chunk: int) -> dict[str, Any]:
    lat: list[float] = []
    ticks = 0
    backpressure = 0
    failures: list[str] = []
    rounds = 0
    end = time.perf_counter() + duration
    while time.perf_counter() < end:
        rounds += 1
        for i in range(0, len(codes), chunk):
            part = codes[i : i + chunk]
            t0 = time.perf_counter()
            try:
                data = rpc.call("get_full_tick", {"codes": part})
                lat.append(time.perf_counter() - t0)
                ticks += len(data or {})
            except Exception as exc:  # noqa: BLE001
                if _is_backpressure(exc):
                    backpressure += 1
                else:
                    failures.append(f"{type(exc).__name__}: {str(exc)[:160]}")
    return {
        "mode": "chunk",
        "chunk": chunk,
        "codes": len(codes),
        "rounds": rounds,
        "ticks": ticks,
        "backpressure": backpressure,
        "failures": failures,
        **_lat_stats(lat),
    }


def _concurrent_worker(codes: list[str], duration: float, out) -> None:
    from backend.scripts.qmt_api.client import client as make_client

    rpc = make_client("rpc", timeout=30)
    lat: list[float] = []
    ticks = 0
    backpressure = 0
    failures: list[str] = []
    end = time.perf_counter() + duration
    while time.perf_counter() < end:
        t0 = time.perf_counter()
        try:
            data = rpc.call("get_full_tick", {"codes": codes})
            lat.append(time.perf_counter() - t0)
            ticks += len(data or {})
        except Exception as exc:  # noqa: BLE001
            if _is_backpressure(exc):
                backpressure += 1
            else:
                failures.append(f"{type(exc).__name__}: {str(exc)[:160]}")
    out.put(
        {
            "ticks": ticks,
            "backpressure": backpressure,
            "failures": failures,
            **_lat_stats(lat),
        }
    )


def run_concurrent(codes: list[str], duration: float, workers: int) -> dict[str, Any]:
    ctx = mp.get_context("fork")
    out = ctx.Queue()
    procs = [
        ctx.Process(target=_concurrent_worker, args=(codes, duration, out))
        for _ in range(workers)
    ]
    t0 = time.perf_counter()
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join()
    elapsed = time.perf_counter() - t0
    worker_stats = [out.get() for _ in range(workers)]
    ticks = backpressure = 0
    failures: list[str] = []
    for stat in worker_stats:
        ticks += int(stat.get("ticks") or 0)
        backpressure += int(stat.get("backpressure") or 0)
        failures.extend(stat.get("failures") or [])
    return {
        "mode": "concurrent",
        "workers": workers,
        "duration_s": round(elapsed, 2),
        "ticks": ticks,
        "backpressure": backpressure,
        "failures": failures[:20],
        "worker_stats": worker_stats,
    }


def run_data_probe(rpc, codes: list[str], chunk: int) -> dict[str, Any]:
    """今日 1d 覆盖、1m 样本、tick 序列可用性（只读）。"""
    report: dict[str, Any] = {}

    t0 = time.perf_counter()
    nonempty = rows = traded = failures = 0
    for i in range(0, len(codes), chunk):
        part = codes[i : i + chunk]
        try:
            data = rpc.call(
                "get_market_data_ex",
                {
                    "stock_list": part,
                    "period": "1d",
                    "start_time": time.strftime("%Y%m%d"),
                    "end_time": time.strftime("%Y%m%d"),
                    "count": -1,
                },
            )
            for frame in (data or {}).values():
                shape = getattr(frame, "shape", None)
                if shape and shape[0]:
                    nonempty += 1
                    rows += int(shape[0])
                    try:
                        traded += int((frame["volume"] > 0).sum())
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001
            failures += 1
    report["daily_1d_today"] = {
        "elapsed_s": round(time.perf_counter() - t0, 2),
        "nonempty": nonempty,
        "rows": rows,
        "traded_volume_gt0": traded,
        "fails": failures,
    }

    sample = codes[:20]
    t0 = time.perf_counter()
    minute_rows = 0
    try:
        data = rpc.call(
            "get_market_data_ex",
            {
                "stock_list": sample,
                "period": "1m",
                "start_time": time.strftime("%Y%m%d"),
                "end_time": time.strftime("%Y%m%d"),
                "count": -1,
            },
        )
        for frame in (data or {}).values():
            shape = getattr(frame, "shape", None)
            if shape:
                minute_rows += int(shape[0])
    except Exception:  # noqa: BLE001
        pass
    report["minute_1m_sample"] = {
        "symbols": len(sample),
        "rows": minute_rows,
        "elapsed_s": round(time.perf_counter() - t0, 2),
    }

    tick_shapes: dict[str, Any] = {}
    for code in sample[:3]:
        try:
            data = rpc.call(
                "get_market_data_ex",
                {
                    "stock_list": [code],
                    "period": "tick",
                    "start_time": time.strftime("%Y%m%d"),
                    "end_time": time.strftime("%Y%m%d"),
                    "count": -1,
                },
            )
            first = next(iter((data or {}).values()), None)
            tick_shapes[code] = getattr(first, "shape", None)
        except Exception as exc:  # noqa: BLE001
            tick_shapes[code] = f"{type(exc).__name__}: {str(exc)[:80]}"
    report["tick_series"] = tick_shapes
    return report


def run_mixed(codes: list[str], duration: float) -> dict[str, Any]:
    """行情压力 + 交易查询并发：验证行情压测是否拖慢 get_asset。

    行情压测在子进程跑（单进程全市场连续拉），父进程每 ~1s 调一次
    ``get_asset``（adjust 主线程路径）。**跑之前先确认 QMT 策略账户健康**；
    账户本来就不响应时，这里只会得到超时，不能说明行情压测的影响。
    """
    ctx = mp.get_context("fork")
    out = ctx.Queue()
    market_proc = ctx.Process(
        target=_concurrent_worker, args=(codes, duration, out)
    )
    market_proc.start()

    rpc = client("rpc", timeout=30)
    lat: list[float] = []
    failures: list[str] = []
    end = time.perf_counter() + duration
    while time.perf_counter() < end:
        t0 = time.perf_counter()
        try:
            rpc.call("get_asset", {})
            lat.append(time.perf_counter() - t0)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{type(exc).__name__}: {str(exc)[:160]}")
        time.sleep(max(0.0, 1.0 - (time.perf_counter() - t0)))

    market_proc.join()
    market = out.get() if not out.empty() else {}
    return {
        "mode": "mixed",
        "market_stress": market,
        "trade_get_asset": {
            "failures": failures,
            **_lat_stats(lat),
        },
    }


def run_trade_probe(rpc, duration: float) -> dict[str, Any]:
    """交易查询压力（get_asset 串行；盘中慎用）。"""
    lat: list[float] = []
    failures: list[str] = []
    end = time.perf_counter() + duration
    while time.perf_counter() < end:
        t0 = time.perf_counter()
        try:
            rpc.call("get_asset", {})
            lat.append(time.perf_counter() - t0)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{type(exc).__name__}: {str(exc)[:160]}")
        time.sleep(max(0.0, 1.0 - (time.perf_counter() - t0)))
    return {
        "mode": "trade",
        "duration_s": duration,
        "failures": failures,
        **_lat_stats(lat),
    }


def _print_block(title: str, block: dict[str, Any]) -> None:
    print(f"\n== {title} ==")
    print(json.dumps(block, ensure_ascii=False, indent=1, default=str)[:4000])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="QMT 行情压力与稳定性压测")
    parser.add_argument(
        "--mode",
        default="all",
        choices=("all", "quote", "chunk", "concurrent", "mixed", "data", "trade"),
    )
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--chunk", type=int, default=500)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--json-out", default="")
    parser.add_argument(
        "--include-trade",
        action="store_true",
        help="all 模式下也做 get_asset 压力（盘中会占 adjust 主线程，慎用）",
    )
    args = parser.parse_args(argv)

    rpc = client("rpc", timeout=30)
    before = read_metrics()
    report: dict[str, Any] = {
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "metrics_before": before,
    }
    codes = fetch_universe(rpc)
    print(f"universe={len(codes)}")
    report["universe"] = len(codes)

    if args.mode in ("all", "quote"):
        report["quote_single"] = run_quote_single(rpc, codes, args.duration)
        _print_block("quote single", report["quote_single"])
    if args.mode in ("all", "chunk"):
        report["quote_chunk"] = run_quote_chunk(
            rpc, codes, min(30.0, args.duration), args.chunk
        )
        _print_block("quote chunk", report["quote_chunk"])
    if args.mode in ("all", "concurrent"):
        report["concurrent"] = run_concurrent(codes, args.duration, args.workers)
        _print_block("concurrent", report["concurrent"])
    if args.mode == "mixed" or (args.mode == "all" and args.include_trade):
        report["mixed"] = run_mixed(codes, args.duration)
        _print_block("mixed", report["mixed"])
    if args.mode in ("all", "data"):
        report["data"] = run_data_probe(rpc, codes, args.chunk)
        _print_block("data coverage", report["data"])
    if args.mode == "trade" or (args.mode == "all" and args.include_trade):
        report["trade"] = run_trade_probe(rpc, min(30.0, args.duration))
        _print_block("trade", report["trade"])

    report["metrics_after"] = read_metrics()
    report["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _print_block("metrics after", report["metrics_after"])

    hard_failures = 0
    for key in ("quote_single", "quote_chunk", "concurrent", "trade"):
        block = report.get(key) or {}
        hard_failures += len(block.get("failures") or [])
    mixed = report.get("mixed") or {}
    hard_failures += len((mixed.get("trade_get_asset") or {}).get("failures") or [])
    print(f"\nhard_failures={hard_failures}")

    if args.json_out:
        os.makedirs(os.path.dirname(args.json_out) or ".", exist_ok=True)
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=1, default=str)
        print(f"report={args.json_out}")
    return 1 if hard_failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
