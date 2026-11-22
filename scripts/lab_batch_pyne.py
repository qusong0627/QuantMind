#!/usr/bin/env python3
"""外部 Pyne 语料批量回测：策略库条目 × 股票池 → 现有「策略排行」JSON。

与 ``scripts/lab_batch_backtest.py`` 的分工：
- 后者跑**内置模板**（``market_lab.STRATEGIES``，``lab_batch.run_batch``）；
- 本脚本跑**导入的 Pine 语料**（``data/pine_transpile/9xxx/candidate.py``），
  复用 ``lab_batch._row / aggregate / save_run`` 的行 schema 与落盘契约，
  界面「行情回测 → 策略排行」直接读。

口径：
- 每个市场一套 ``market_lab.STRATEGY_PRESETS``（A股 0.05%/边、港股 0.15%/边）；
- 候选脚本只复制到临时目录再改 decorator，原文件不动；
- A 股只跑 ``market_hint=cn``（多头）；美股跑 ``market_hint=us``（多空）；
  港股默认跑全部（卖空受限，仅作第二展示市场）。

用法（宿主）::

    python3 scripts/lab_batch_pyne.py --market cn --pool hs300 --limit 50 --workers 4
    python3 scripts/lab_batch_pyne.py --market us --limit 100 --ids 9002,9004
    python3 scripts/lab_batch_pyne.py --market cn --ids all --resume <run_id>
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import signal
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Python 3.14 Linux 默认 forkserver；PyneCore 的 security 子进程会在子进程里
# 再 spawn，forkserver 下直接 RuntimeError。批量回测统一 fork。
if sys.platform.startswith("linux"):
    import multiprocessing as _mp

    try:
        _mp.set_start_method("fork", force=True)
    except RuntimeError:
        pass


def _pine_site() -> Path | None:
    env = os.environ.get("QM_PINE_SITE")
    if env:
        return Path(env).expanduser()
    lib = ROOT / "pine_lab/.venv/lib"
    if lib.is_dir():
        for cand in sorted(lib.glob("python3.*/site-packages"), reverse=True):
            return cand
    return None


_SITE = _pine_site()
if _SITE and _SITE.is_dir() and str(_SITE) not in sys.path:
    sys.path.append(str(_SITE))

from backend.services.agent_arena import lab_batch as lb  # noqa: E402
from backend.services.agent_arena import market_lab as ml  # noqa: E402
from backend.services.agent_arena import pine_library as pl  # noqa: E402
from backend.services.agent_arena import pyne_overrides as po  # noqa: E402

CHUNK = 8


def imported_items() -> list[dict[str, Any]]:
    return [
        it
        for it in pl._load_custom()  # noqa: SLF001
        if it.get("source_kind") == "imported" and it.get("id")
    ]


def select_strategies(
    items: list[dict[str, Any]], market: str, ids: str, scope: str
) -> list[dict[str, Any]]:
    want_ids = (
        {s.strip() for s in ids.split(",") if s.strip()}
        if ids and ids != "all"
        else None
    )
    out: list[dict[str, Any]] = []
    for it in items:
        sid = str(it["id"])
        if want_ids is not None and sid not in want_ids:
            continue
        if want_ids is None and it.get("import_status") not in (
            None,
            "ready",
            "ready_security",
        ):
            continue
        hint = str(it.get("market_hint") or "cn")
        if want_ids is None and scope == "hint":
            if market == "cn" and hint != "cn":
                continue
            if market == "us" and hint != "us":
                continue
            # hk：卖空受限，默认全量（只作展示市场）
        # security 周期是否能在该市场构造（1/5/15/30/60 需分钟数据；
        # 240+ 仅加密）；显式 --ids 仍放行，让失败原因明确落 errors。
        sec_markets = it.get("security_markets")
        if want_ids is None and sec_markets is not None and market not in sec_markets:
            continue
        cand = pl.transpile_dir() / sid / "candidate.py"
        if not cand.is_file():
            continue
        out.append(
            {
                "id": sid,
                "name": str(it.get("title") or sid),
                "hint": hint,
                "path": str(cand),
            }
        )
    out.sort(key=lambda x: x["id"])
    return out


def resolve_symbols(
    market: str, pool: str, adj: str, limit: int
) -> list[dict[str, str]]:
    if market == "cn":
        return [
            {"code": s["code"], "name": s.get("name", "")}
            for s in lb.resolve_pool(pool, limit)
        ]
    import duckdb

    root = ml._market_root(market) / "1_kline_data"  # noqa: SLF001
    sub = "daily_backward" if adj == "backward" else "daily_forward"
    glob = str(root / sub / "dt=*" / "*.parquet")
    rows = (
        duckdb.connect()
        .execute("SELECT DISTINCT symbol FROM read_parquet(?) ORDER BY symbol", [glob])
        .fetchall()
    )
    codes = [str(r[0]) for r in rows if r[0]][: limit or None]
    out: list[dict[str, str]] = []
    for code in codes:
        try:
            name = ml.stock_name(code, market)
        except Exception:  # noqa: BLE001 - 名称缺失不影响回测
            name = ""
        out.append({"code": code, "name": name or ""})
    return out


class _StrategyTimeout(RuntimeError):
    pass


def _on_alarm(signum, frame):  # noqa: ARG001
    raise _StrategyTimeout()


def _run_one_strategy(
    script: Path,
    bars: list[dict],
    code: str,
    market: str,
    adj: str,
    sid: str,
    name: str,
    timeout: int,
) -> dict:
    """跑单条策略；``timeout>0`` 时用 SIGALRM 硬超时（防死循环卡住整批）。"""
    kwargs = {
        "adj": adj,
        "market": market,
        "freq": "1d",
        "want_trades": False,
        "strategy_id": sid,
        "name": name,
    }
    if not timeout or timeout <= 0:
        return ml.run_script_on_bars(script, bars, code, **kwargs)
    old = signal.signal(signal.SIGALRM, _on_alarm)
    signal.alarm(int(timeout))
    try:
        return ml.run_script_on_bars(script, bars, code, **kwargs)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def _run_chunk(args: tuple) -> dict[str, Any]:
    symbols, strategies, market, adj, min_bars, timeout = args
    import contextlib
    import io

    rows: list[dict] = []
    skipped: list[dict] = []
    errors: list[dict] = []
    tmp = Path(tempfile.mkdtemp(prefix="pyne_batch_"))
    try:
        mats: dict[str, Path] = {}
        preset = ml.STRATEGY_PRESETS[market]
        for s in strategies:
            d = tmp / s["id"]
            d.mkdir(parents=True, exist_ok=True)
            mat = po.materialize_script(Path(s["path"]), preset, d)
            unique = d / f"{s['id']}.py"
            mat.rename(unique)
            mats[s["id"]] = unique

        for sym in symbols:
            code = sym["code"]
            name = sym.get("name") or code
            try:
                bars = ml.load_klines(code, adj=adj, limit=0, market=market, freq="1d")
            except Exception as exc:  # noqa: BLE001
                errors.append({"symbol": code, "error": f"读K线失败: {exc}"})
                continue
            if len(bars) < min_bars:
                skipped.append(
                    {
                        "symbol": code,
                        "name": name,
                        "bars": len(bars),
                        "reason": f"K线不足 {min_bars} 根",
                    }
                )
                continue
            for s in strategies:
                try:
                    with contextlib.redirect_stdout(io.StringIO()):
                        res = _run_one_strategy(
                            mats[s["id"]],
                            bars,
                            code,
                            market,
                            adj,
                            s["id"],
                            s["name"],
                            timeout,
                        )
                    rows.append(lb._row(code, name, s["id"], res, len(bars)))  # noqa: SLF001
                except _StrategyTimeout:
                    errors.append(
                        {
                            "symbol": code,
                            "strategy": s["id"],
                            "error": f"超时（>{timeout}s，已跳过）",
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append(
                        {
                            "symbol": code,
                            "strategy": s["id"],
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"rows": rows, "skipped": skipped, "errors": errors}


def _load_done(run_id: str, out_dir: Path) -> tuple[set[str], list[dict]]:
    try:
        prev = lb.load_run(run_id, out_dir)
    except FileNotFoundError:
        return set(), []
    done = {r["symbol"] for r in prev.get("rows", [])}
    done |= {s["symbol"] for s in prev.get("skipped", [])}
    return done, prev.get("rows", [])


def _save(
    run_id: str,
    pool: str,
    market: str,
    adj: str,
    strategies: list[dict],
    rows: list[dict],
    skipped: list[dict],
    errors: list[dict],
    universe: int,
    elapsed: float,
    out_dir: Path,
) -> Path:
    ranking = lb.aggregate(rows)
    name_by_id = {s["id"]: s["name"] for s in strategies}
    for rank in ranking:
        rank["name"] = name_by_id.get(rank["strategy"], rank["name"])
    payload = {
        "run_id": run_id,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "pool": pool,
        "pool_label": lb.pool_label(pool) if market == "cn" else market.upper(),
        "market": market,
        "adj": adj,
        "strategies": [{"id": s["id"], "name": s["name"]} for s in strategies],
        "universe": universe,
        "counts": {"rows": len(rows), "skipped": len(skipped), "errors": len(errors)},
        "elapsed_sec": round(elapsed, 1),
        "ranking": ranking,
        "rows": rows,
        "skipped": skipped,
        "errors": errors[:200],
    }
    return lb.save_run(payload, out_dir)


def _plan_jobs(
    todo: list[dict],
    strategies: list[dict],
    market: str,
    adj: str,
    min_bars: int,
    workers: int,
    timeout: int,
) -> list[tuple]:
    """标的数 ≥ workers 时按标的切块；否则按策略切片，避免单 worker 串完 300 条。"""
    if len(todo) >= workers:
        chunks = [todo[i : i + CHUNK] for i in range(0, len(todo), CHUNK)]
        return [(c, strategies, market, adj, min_bars, timeout) for c in chunks]
    per = max(1, math.ceil(len(strategies) / workers))
    jobs: list[tuple] = []
    for sym in todo:
        for i in range(0, len(strategies), per):
            jobs.append(
                ([sym], strategies[i : i + per], market, adj, min_bars, timeout)
            )
    return jobs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Pine 语料批量回测（股票池 × 导入策略）")
    ap.add_argument("--market", default="cn", choices=["cn", "hk", "us"])
    ap.add_argument(
        "--pool",
        default="hs300",
        help="A股池：hs300/zz500/zz1000/sz50/zz800/kc50/cyb/all/file:<path>",
    )
    ap.add_argument("--ids", default="all", help="all 或逗号分隔的库 ID（9001,...）")
    ap.add_argument(
        "--scope",
        default="hint",
        choices=["hint", "all"],
        help="hint=只跑该市场标签；all=全部导入条目",
    )
    ap.add_argument("--limit", type=int, default=0, help="限制标的数")
    ap.add_argument("--adj", default="", help="缺省：A股 backward；非A股 unadjusted")
    ap.add_argument("--workers", type=int, default=0, help="进程数（默认 CPU-1）")
    ap.add_argument("--min-bars", type=int, default=lb.MIN_BARS)
    ap.add_argument(
        "--strategy-timeout",
        type=int,
        default=180,
        help="单条策略硬超时秒数（0=不限；防死循环卡住整批）",
    )
    ap.add_argument("--run-id", default="")
    ap.add_argument("--resume", default="")
    ap.add_argument("--out-dir", default="")
    args = ap.parse_args(argv)

    market = ml.normalize_market(args.market)
    adj = args.adj or ("backward" if market == "cn" else "unadjusted")
    strategies = select_strategies(imported_items(), market, args.ids, args.scope)
    if not strategies:
        print(
            "没有可跑的导入策略（先跑 scripts/import_pynecore_corpus.py）",
            file=sys.stderr,
        )
        return 2

    pool = resolve_symbols(market, args.pool, adj, args.limit)
    if not pool:
        print(
            f"股票池为空：market={market} pool={args.pool} adj={adj}", file=sys.stderr
        )
        return 2

    out_dir = Path(args.out_dir) if args.out_dir else lb.batch_dir()
    run_id = args.resume or args.run_id or lb.new_run_id(f"{market}_{args.pool}")
    done, rows = _load_done(run_id, out_dir) if args.resume else (set(), [])
    skipped: list[dict] = []
    errors: list[dict] = []
    if args.resume:
        prev = lb.load_run(run_id, out_dir)
        skipped, errors = prev.get("skipped", []), prev.get("errors", [])

    todo = [s for s in pool if s["code"] not in done]
    pool_label = lb.pool_label(args.pool) if market == "cn" else market.upper()
    print(
        f"市场 {market} / 池 {pool_label}：{len(pool)} 标的，待跑 {len(todo)}，"
        f"策略 {len(strategies)} 个，预计 {len(todo) * len(strategies)} 次回测"
    )
    if not todo:
        print("没有待跑标的（续跑已完成）")
        return 0

    workers = args.workers or max(1, (os.cpu_count() or 2) - 1)
    jobs = _plan_jobs(
        todo, strategies, market, adj, args.min_bars, workers, args.strategy_timeout
    )
    t0 = time.time()
    done_n = 0
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(_run_chunk, j) for j in jobs]
        for fut in as_completed(futures):
            got = fut.result()
            rows.extend(got["rows"])
            skipped.extend(got["skipped"])
            errors.extend(got["errors"])
            done_n += 1
            print(
                f"  [{done_n}/{len(jobs)}] rows={len(rows)} skipped={len(skipped)} "
                f"errors={len(errors)} elapsed={time.time() - t0:.0f}s"
            )

    path = _save(
        run_id,
        args.pool,
        market,
        adj,
        strategies,
        rows,
        skipped,
        errors,
        len(pool),
        time.time() - t0,
        out_dir,
    )
    print(
        json.dumps(
            {
                "run_id": run_id,
                "path": str(path),
                "rows": len(rows),
                "skipped": len(skipped),
                "errors": len(errors),
                "elapsed_sec": round(time.time() - t0, 1),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
