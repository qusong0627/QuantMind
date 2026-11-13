#!/usr/bin/env python3
"""批量回测 CLI：股票池 × 策略模板 → 策略排行榜 + 单策略选股结果。

用法（pynecore 在 pine_lab 运行时里；首次先 ``bash scripts/setup_pine_lab.sh``）：
  cd <repo>
  pine_lab/.venv/bin/python scripts/lab_batch_backtest.py --pool hs300
  pine_lab/.venv/bin/python scripts/lab_batch_backtest.py --pool all --limit 800 --workers 8
  pine_lab/.venv/bin/python scripts/lab_batch_backtest.py --pool file:data/watch.json --strategies donchian,sma_cross

产物：``<数据根>/lab_batch/<run_id>.json``（界面「行情回测 → 策略排行/选股」读这个）
中断重跑：``--resume <run_id>`` 只补没跑完的标的。

移植说明（2026-11-13 自 quant-Trader 入仓）：导入路径改为本仓
``backend.services.agent_arena``；产物目录走 ``lb.batch_dir()``（env
``LAB_BATCH_DIR`` / 数据根），不再写死模块级常量。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.services.agent_arena import lab_batch as lb  # noqa: E402
from backend.services.agent_arena import market_lab as ml  # noqa: E402

# 每个子任务的标的数：太小则进程启动开销占比高，太大则进度/断点粒度粗
CHUNK = 12


def _chunk_job(args: tuple) -> dict:
    """子进程任务：跑一小批标的（策略列表随任务传，避免全局状态）。"""
    symbols, strategies, adj, min_bars = args
    return lb.run_batch(symbols, strategies, adj=adj, min_bars=min_bars)


def _load_done(run_id: str, out_dir: Path) -> tuple[set[str], list[dict]]:
    """断点续跑：已完成的标的集合 + 已有明细行。"""
    try:
        prev = lb.load_run(run_id, out_dir)
    except FileNotFoundError:
        return set(), []
    done = {r["symbol"] for r in prev.get("rows", [])}
    done |= {s["symbol"] for s in prev.get("skipped", [])}
    return done, prev.get("rows", [])


def _save(run_id: str, pool: str, adj: str, strategies: list[str],
          rows: list[dict], skipped: list[dict], errors: list[dict],
          universe: int, elapsed: float, out_dir: Path) -> Path:
    payload = {
        "run_id": run_id,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "pool": pool, "pool_label": lb.pool_label(pool), "adj": adj,
        "strategies": [{"id": s, "name": ml.STRATEGIES[s]["name"]} for s in strategies],
        "universe": universe,
        "counts": {"rows": len(rows), "skipped": len(skipped), "errors": len(errors)},
        "elapsed_sec": round(elapsed, 1),
        "ranking": lb.aggregate(rows),
        "rows": rows,
        "skipped": skipped,
        "errors": errors[:200],
    }
    return lb.save_run(payload, out_dir)


def main() -> int:
    ap = argparse.ArgumentParser(description="批量回测（股票池 × 策略）")
    ap.add_argument("--pool", default="hs300",
                    help="hs300/zz500/zz1000/sz50/zz800/kc50/cyb/all/file:<path>")
    ap.add_argument("--limit", type=int, default=0, help="限制标的数（all 按流通市值降序取前 N）")
    ap.add_argument("--strategies", default=",".join(ml.STRATEGIES),
                    help="逗号分隔的策略 id，默认全部")
    ap.add_argument("--adj", default="backward", choices=sorted(ml.ADJ_DIRS),
                    help="复权口径，默认后复权")
    ap.add_argument("--workers", type=int, default=0, help="进程数，默认 CPU 核数-1")
    ap.add_argument("--min-bars", type=int, default=lb.MIN_BARS)
    ap.add_argument("--run-id", default="", help="指定批次名（默认时间戳-池名）")
    ap.add_argument("--resume", default="", help="续跑已有批次：run_id")
    ap.add_argument("--out-dir", default="", help="产物目录（默认数据根/lab_batch）")
    args = ap.parse_args()

    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    bad = [s for s in strategies if s not in ml.STRATEGIES]
    if bad:
        print(f"未知策略: {bad}（可用: {list(ml.STRATEGIES)}）", file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir) if args.out_dir else lb.batch_dir()
    run_id = args.resume or args.run_id or lb.new_run_id(args.pool)
    done, rows = _load_done(run_id, out_dir) if args.resume else (set(), [])
    skipped: list[dict] = []
    errors: list[dict] = []
    if args.resume:
        prev = lb.load_run(run_id, out_dir)
        skipped, errors = prev.get("skipped", []), prev.get("errors", [])
        # 续跑常用更窄的 --strategies 补缺；元数据保留原批次的全集，否则界面显示不全
        strategies = list(dict.fromkeys(
            [s["id"] for s in prev.get("strategies", [])] + strategies))
        print(f"续跑 {run_id}：已完成 {len(done)} 个标的")

    t0 = time.time()
    pool = lb.resolve_pool(args.pool, args.limit)
    todo = [s for s in pool if s["code"] not in done]
    print(f"股票池 {lb.pool_label(args.pool)}：{len(pool)} 个标的"
          f"，待跑 {len(todo)}，策略 {len(strategies)} 个"
          f"，预计 {len(todo) * len(strategies)} 次回测")

    workers = args.workers or max(1, (os.cpu_count() or 2) - 1)
    chunks = [todo[i:i + CHUNK] for i in range(0, len(todo), CHUNK)]
    jobs = [(c, strategies, args.adj, args.min_bars) for c in chunks]

    n_done = len(done)
    if jobs:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_chunk_job, j): j for j in jobs}
            for i, fut in enumerate(as_completed(futs), 1):
                try:
                    r = fut.result()
                except Exception as exc:  # noqa: BLE001 单个分片崩溃只记错，不丢整批
                    syms = [s["code"] for s in futs[fut][0]]
                    errors.append({"symbols": syms, "error": f"分片失败: {exc}"})
                    continue
                rows.extend(r["rows"])
                skipped.extend(r["skipped"])
                errors.extend(r["errors"])
                n_done += len(futs[fut][0])
                el = time.time() - t0
                eta = el / i * (len(jobs) - i)
                print(f"  [{i}/{len(jobs)}] {n_done}/{len(pool)} 标的"
                      f" · 行 {len(rows)} · 用时 {el:.0f}s · 剩余 ~{eta:.0f}s",
                      flush=True)
                if i % 5 == 0 or i == len(jobs):     # 周期落盘，崩溃可续
                    _save(run_id, args.pool, args.adj, strategies, rows, skipped,
                          errors, len(pool), time.time() - t0, out_dir)

    elapsed = time.time() - t0
    path = _save(run_id, args.pool, args.adj, strategies, rows, skipped, errors,
                 len(pool), elapsed, out_dir)
    print(f"\n完成：{len(rows)} 行 / {len(skipped)} 跳过 / {len(errors)} 错误"
          f"，用时 {elapsed:.0f}s\n产物：{path}")
    print("\n策略排行（按均值净收益）：")
    print(f"  {'策略':<14}{'标的数':>6}{'均值%':>10}{'中位%':>10}"
          f"{'均回撤%':>9}{'夏普':>7}{'盈亏比':>7}{'胜率%':>7}{'跑赢持有%':>10}")
    for a in lb.aggregate(rows):
        f = lambda v, d=1: "—" if v is None else f"{v:.{d}f}"  # noqa: E731
        print(f"  {a['name']:<14}{a['symbols']:>6}{f(a['mean_net_pct']):>10}"
              f"{f(a['median_net_pct']):>10}{f(a['mean_dd_pct']):>9}"
              f"{f(a['mean_sharpe'], 2):>7}{f(a['mean_profit_factor'], 2):>7}"
              f"{f(a['mean_win_rate_pct']):>7}{f(a['beat_bh_pct']):>10}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
