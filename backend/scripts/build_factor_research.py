#!/usr/bin/env python3
"""因子研究快照构建（QuantDB → factor_research 数据集）

用法（仓库根执行）:
    python3 backend/scripts/build_factor_research.py                 # CN 默认 2016-07 至今
    python3 backend/scripts/build_factor_research.py --start 2025-01-01 --end 2025-12-31
    python3 backend/scripts/build_factor_research.py --smoke 200     # 冒烟：200 只 + 2023 起
    python3 backend/scripts/build_factor_research.py --skip-financial

产物（<quantdb>/factor_research/）:
    meta.json           构建元信息（时间窗/股票池/基准/列数）
    metrics.json        每因子 KPI + 排行榜（综合分）
    ic.parquet          月频 IC 序列（date × factor）
    nav.parquet         Top-N 回测净值（date × factor；top_n 见 meta）
    corr.parquet        因子两两相关（全窗口均值）
    monthly_scores.parquet  月末截面打分（合成/对比用）
    monthly_scores/year=YYYY.parquet  同上，按年分区
    fwd_returns.parquet 月末→次月末持有期收益
    holdings_latest.json    最新一期各因子 Top-N 持仓
    benchmarks.parquet  基准净值（沪深300/中证800/中证500，index_code 区分）
    factor_panel.parquet    月末名次面板：每因子每期前 K 名的 symbol/score/raw/fwd_ret
                            （任意 N 与任意区间回测的 O(1) 底料，见 scorecard.py）
    factor_panel/year=YYYY.parquet  同上，按年分区（运行时只读相交年份）
    stock_snapshot.parquet  最新截面个股元数据
    stock_snapshot/year=YYYY.parquet  按年末截面（历史持仓画像不回看今天）
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.services.engine.factor_research import analysis  # noqa: E402
from backend.services.engine.factor_research import data as frdata  # noqa: E402
from backend.services.engine.factor_research import engine, financials  # noqa: E402
from backend.services.engine.factor_research import windows as frwindows  # noqa: E402
from backend.services.engine.factor_research.catalog import FACTORS, BY_CODE  # noqa: E402

LOOKBACK_START = frwindows.CN_LOOKBACK_START  # 252 日窗口预热（2016-01-04 起）
DEFAULT_START = frwindows.CN_START
TOP_N = 30

# 基准指数（净值供超额收益口径；首个为主基准，写入 meta["benchmark"]）
BENCHMARKS = (
    ("000300.SH", "沪深300"),
    ("000906.SH", "中证800"),
    ("000905.SH", "中证500"),
)
# 月末面板每期保存的名次数：覆盖 N≤100 的持仓数扫描 + 合成权重搜索的候选池
PANEL_K = 150


def _out_dir() -> Path:
    import os

    env = os.environ.get("FACTOR_RESEARCH_OUT")  # 冒烟测试用临时目录，避免覆盖线上快照
    if env:
        d = Path(env)
        d.mkdir(parents=True, exist_ok=True)
        return d
    from backend.shared.quantdb_paths import resolve_quantdb_dir

    d = resolve_quantdb_dir() / "factor_research"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _build_panel(
    scores: dict[str, pd.DataFrame],
    raw: dict[str, pd.DataFrame],
    fwd: pd.DataFrame,
    k: int,
) -> pd.DataFrame:
    """月末名次面板：每因子每期前 K 名（rank/symbol/score/raw/fwd_ret）。

    末月（最新快照）fwd_ret 为 NaN —— 仍保留，个股表要读最新截面。
    """
    dates = next(iter(scores.values())).index
    # symbol 轴 = 各因子列名的并集（先到先得、稳定序），不能只取首个因子：
    # 估值/行情面板列集实测不同（多 1 只），只取首个会静默丢掉别的因子算得出的票。
    syms = pd.Index(dict.fromkeys(c for sc in scores.values() for c in sc.columns))
    fwd = fwd.reindex(index=dates, columns=syms)
    fwd_np = fwd.to_numpy(dtype=np.float32)
    fcode, tds, ranks, sym_out, sc_out, raw_out, fr_out = [], [], [], [], [], [], []
    for code, sc in scores.items():
        # 必须 reindex 到统一 symbol 轴：各因子列集/列序可不同（实测估值面板比行情
        # 面板多 1 只）——直接 to_numpy() 按位置取会越界，或更糟：列序不同时把分数
        # 静默记到别的票头上（2026-10-22 截断窗构建在 [6/8] 炸出）。
        s = sc.reindex(index=dates, columns=syms).to_numpy(dtype=np.float32)
        r = raw[code].reindex(index=dates, columns=syms).to_numpy(dtype=np.float32)
        r = np.where(np.isfinite(r), r, np.nan)
        for i, d in enumerate(dates):
            row = s[i]
            valid = np.isfinite(row)
            kk = min(k, int(valid.sum()))
            if kk == 0:
                continue
            filled = np.where(valid, row, -np.inf)
            top = np.argpartition(-filled, kk - 1)[:kk]
            top = top[np.argsort(-filled[top])]
            fcode.append(np.full(kk, code, dtype=object))
            tds.append(np.full(kk, np.datetime64(d), dtype="datetime64[ns]"))
            ranks.append(np.arange(1, kk + 1, dtype=np.int16))
            sym_out.append(np.asarray(syms)[top])
            sc_out.append(row[top])
            raw_out.append(r[i][top])
            fr_out.append(fwd_np[i][top])
    panel = pd.DataFrame(
        {
            "factor_code": pd.Categorical(np.concatenate(fcode)),
            "trade_date": np.concatenate(tds),
            "rank": np.concatenate(ranks),
            "symbol": pd.Categorical(np.concatenate(sym_out)),
            "score": np.concatenate(sc_out).astype(np.float32),
            "raw": np.concatenate(raw_out).astype(np.float32),
            "fwd_ret": np.concatenate(fr_out).astype(np.float32),
        }
    )
    panel["trade_date"] = pd.to_datetime(panel["trade_date"])
    return panel


def _build_stocks_snapshot(
    daily: dict[str, pd.DataFrame],
    val: dict[str, pd.DataFrame],
    instr: pd.DataFrame,
    panel_date: pd.Timestamp,
) -> pd.DataFrame:
    """最新截面个股元数据（个股表用）：名称/申万行业/总市值(亿)/PE(TTM)/PB/近一年日均成交额(亿)。"""
    close = daily["close"]
    idx = close.index[close.index <= panel_date]
    win = idx[-252:]
    amt_yi = daily["amount"].reindex(win).mean() / 1e4  # 万元 → 亿元
    meta = instr.set_index("symbol")
    snap = pd.DataFrame({"symbol": close.columns})
    snap["name"] = meta["name"].reindex(snap["symbol"]).to_numpy()
    snap["industry"] = meta["industry"].reindex(snap["symbol"]).to_numpy()
    snap["total_mv_yi"] = (
        (val["total_mv"].loc[panel_date] / 1e8).reindex(snap["symbol"]).to_numpy()
    )
    for col, src in (("pe_ttm", "pe_ttm"), ("pb", "pb")):
        if src in val:
            snap[col] = val[src].loc[panel_date].reindex(snap["symbol"]).to_numpy()
    snap["avg_amount_yi"] = amt_yi.reindex(snap["symbol"]).to_numpy()
    return snap


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=DEFAULT_START, help="首个采样月末（含）")
    ap.add_argument("--end", default=None, help="截止日（默认最新）")
    ap.add_argument(
        "--lookback-start", default=LOOKBACK_START, help="预热起点（默认 2016-01-04）"
    )
    ap.add_argument(
        "--smoke", type=int, default=0, help="冒烟模式：只取 N 只股票"
    )
    ap.add_argument(
        "--skip-financial", action="store_true", help="跳过财务/行为类（仅行情+估值）"
    )
    ap.add_argument("--top-n", type=int, default=TOP_N)
    ap.add_argument("--out", default=None, help="输出目录（默认按市场解析）")
    ap.add_argument(
        "--no-partition", action="store_true", help="只写整份产物（冒烟/兼容用）"
    )
    args = ap.parse_args()

    t0 = time.time()
    out = Path(args.out) if args.out else _out_dir()
    out.mkdir(parents=True, exist_ok=True)
    start_ts = pd.Timestamp(args.start)
    end_ts = pd.Timestamp(args.end) if args.end else None
    end_s = args.end or "20991231"
    load_end = (
        (end_ts + pd.Timedelta(days=frwindows.FWD_BUFFER_DAYS)).strftime("%Y%m%d")
        if end_ts is not None
        else end_s
    )
    print(
        f"[1/8] 载入 daily_forward / valuation / index ... ({args.lookback_start} ~ {load_end})"
    )
    daily = frdata.load_daily_panel(args.lookback_start, load_end)
    val = frdata.load_valuation_panel(args.lookback_start, load_end)
    bench_series = {
        sym: frdata.load_index_close(args.lookback_start, load_end, sym)
        for sym, _ in BENCHMARKS
    }
    instr = frdata.load_instrument()

    close = daily["close"]
    if args.smoke:
        syms = list(close.columns)[: args.smoke]
        for d_ in (daily, val):
            for k in d_:
                d_[k] = d_[k][syms]
        close = daily["close"]

    all_dates = frdata.month_end_dates(close.index)
    dates = [
        d
        for d in all_dates
        if d >= start_ts and (end_ts is None or d <= end_ts)
    ]
    fwd_dates = list(dates)
    if end_ts is not None:
        nxt = [d for d in all_dates if d > dates[-1]]
        if nxt:
            fwd_dates.append(nxt[0])
    universe = frdata.build_universe(instr, close)
    print(
        f"      交易日 {len(close)}，采样日 {len(dates)}（{dates[0].date()} ~ {dates[-1].date()}），"
        f"股票 {close.shape[1]}"
    )

    print("[2/8] 行情类 + 估值类因子 ...")
    raw: dict[str, pd.DataFrame] = {}
    raw.update(engine.compute_trade_factors(daily, val, instr, dates))
    raw.update(engine.compute_valuation_factors(val, instr, dates, pb_daily=val["pb"]))
    print(f"      {len(raw)} 个")

    if not args.skip_financial:
        print("[3/8] 财务类 + 行为类因子（PIT）...")
        syms = list(close.columns)
        fin_dirs = {
            "income": frdata.market_path(frdata.INCOME_PARTS),
            "balance": frdata.market_path(frdata.BALANCE_PARTS),
            "cashflow": frdata.market_path(frdata.CASHFLOW_PARTS),
            "pershare": frdata.market_path(frdata.PERSHARE_PARTS),
        }
        fin = financials.compute_financial_factors(
            syms, fin_dirs, dates, total_mv=val["total_mv"]
        )
        beh = financials.compute_behavior_factors(
            syms,
            frdata.market_path(frdata.HOLDER_PARTS),
            dates,
            circ_cap=val["circulating_capital"],
        )
        raw.update(fin)
        raw.update(beh)
        print(f"      累计 {len(raw)} 个")
    else:
        print("[3/8] 跳过财务/行为类")

    print("[4/8] 截面打分（rank→正态分位，按方向）...")
    scores: dict[str, pd.DataFrame] = {}
    for code, df in raw.items():
        meta = BY_CODE.get(code)
        if meta is None:
            continue
        scores[code] = engine.rank_to_score(
            df.reindex(dates), int(meta["direction"]), universe.reindex(dates)
        )

    print("[5/8] 持有期收益 / IC / 回测 ...")
    # fwd 多算一个采样日：`--end 2019-12-31` 时 2019-12 的持有期收益要 2020-01 的价格，
    # 否则块末月 fwd=NaN，窗口边界直接断链。
    fwd = analysis.forward_returns(
        close, fwd_dates, universe.reindex(fwd_dates).fillna(False)
    ).reindex(index=dates)
    metrics: dict[str, dict] = {}
    ic_all, nav_all, holdings_latest = [], [], {}
    for code, sc in scores.items():
        ic = analysis.ic_series(sc, fwd)
        bt = analysis.backtest_topn(sc, fwd, top_n=args.top_n)
        k = analysis.kpi(bt["ret"], bt["nav"])
        k["ic_mean"] = round(float(ic.mean()), 4) if ic.notna().any() else None
        k["ic_std"] = round(float(ic.std()), 4) if ic.notna().any() else None
        k["ic_ir"] = (
            round(float(ic.mean() / ic.std()), 3)
            if ic.notna().any() and ic.std()
            else None
        )
        k["ic_win_rate"] = (
            round(float((ic > 0).mean()), 4) if ic.notna().any() else None
        )
        metrics[code] = k
        ic_all.append(ic.rename(code))
        nav_all.append(bt["nav"].rename(code))
        if bt["holdings"]:
            holdings_latest[code] = bt["holdings"][max(bt["holdings"])]
    ic_df = pd.concat(ic_all, axis=1)
    nav_df = pd.concat(nav_all, axis=1)

    print("[6/8] 月末名次面板 + 最新截面个股快照 ...")
    panel = _build_panel(scores, raw, fwd, PANEL_K)
    # 每年一份截面快照：历史窗口的持仓画像（市值/行业）必须用当年末的数，
    # 拿今天的最新截面回看 2016 就是把未来信息贴到过去。
    year_last: dict[int, pd.Timestamp] = {}
    for d in dates:
        year_last[int(d.year)] = d
    stock_snaps = {
        y: _build_stocks_snapshot(daily, val, instr, d) for y, d in year_last.items()
    }
    stock_snap = stock_snaps[int(dates[-1].year)]
    print(f"      面板 {len(panel):,} 行（K={PANEL_K}）；个股快照 {len(stock_snap)} 只")

    print("[7/8] 因子相关矩阵 ...")
    corr = analysis.correlation_pairs(scores)

    print("[8/8] 落盘 ...")
    long_rows = []
    for code, sc in scores.items():
        s = sc.stack()
        long_rows.append(
            pd.DataFrame(
                {
                    "trade_date": s.index.get_level_values(0),
                    "symbol": s.index.get_level_values(1),
                    "factor_code": code,
                    "score": s.values.astype("float32"),
                }
            )
        )
    long = pd.concat(long_rows, ignore_index=True)
    long.to_parquet(out / "monthly_scores.parquet", index=False)
    if not args.no_partition:
        frwindows.write_year_partitions(long, out, frwindows.SCORES_DIR)

    fwd_long = fwd.stack()
    pd.DataFrame(
        {
            "trade_date": fwd_long.index.get_level_values(0),
            "symbol": fwd_long.index.get_level_values(1),
            "fwd_ret": fwd_long.values.astype("float32"),
        }
    ).to_parquet(out / "fwd_returns.parquet", index=False)

    ic_out = ic_df.stack()
    pd.DataFrame(
        {
            "trade_date": ic_out.index.get_level_values(0),
            "factor_code": ic_out.index.get_level_values(1),
            "ic": ic_out.values.astype("float32"),
        }
    ).to_parquet(out / "ic.parquet", index=False)

    nav_rows = []
    for code in nav_df.columns:
        s = nav_df[code].dropna()
        for d, v in s.items():
            nav_rows.append({"trade_date": d, "factor_code": code, "nav": float(v)})
    pd.DataFrame(nav_rows).to_parquet(out / "nav.parquet", index=False)
    corr.to_parquet(out / "corr.parquet", index=False)

    bench_frames = []
    for sym, _name in BENCHMARKS:
        s = bench_series[sym].reindex(dates).dropna()
        if s.empty:
            print(f"      基准 {sym} 无数据，跳过")
            continue
        b = (s / s.iloc[0]).rename("nav").reset_index()
        b.columns = ["trade_date", "nav"]
        b["index_code"] = sym
        bench_frames.append(b)
    pd.concat(bench_frames, ignore_index=True).to_parquet(
        out / "benchmarks.parquet", index=False
    )
    panel.to_parquet(out / "factor_panel.parquet", index=False)
    stock_snap.to_parquet(out / "stock_snapshot.parquet", index=False)
    if not args.no_partition:
        frwindows.write_year_partitions(panel, out, frwindows.PANEL_DIR)
        for y, snap in stock_snaps.items():
            p = out / frwindows.SNAPSHOT_DIR / f"year={y}.parquet"
            frwindows.atomic_write_parquet(snap, p)

    lb = analysis.leaderboard(metrics)
    available = {f["code"]: f for f in FACTORS if f["available"]}
    meta = {
        "built_at": pd.Timestamp.now().isoformat(timespec="seconds"),
        "window": [str(dates[0].date()), str(dates[-1].date())],
        "lookback_start": args.lookback_start,
        "direction_calibration": {
            "start": frwindows.DIRECTION_START,
            "end": frwindows.DIRECTION_END,
            "source": "catalog",  # 经典因子的方向是目录静态定义，不按 IC 翻
        },
        "partitioned": not args.no_partition,
        "n_dates": len(dates),
        "n_symbols": int(close.shape[1]),
        "top_n": args.top_n,
        "cost_rate": analysis.COST_RATE,
        "benchmark": frdata.BENCHMARK_SYMBOL,
        "benchmarks": [{"symbol": sym, "name": name} for sym, name in BENCHMARKS],
        "panel_k": PANEL_K,
        "n_factors_total": len(FACTORS),
        "n_factors_available": len(available),
        "n_factors_computed": len(scores),
        "elapsed_sec": round(time.time() - t0, 1),
        "skip_financial": bool(args.skip_financial),
        "smoke": args.smoke,
    }

    # NaN/Inf → null（Python json 默认放行 NaN，但 FastAPI 序列化会拒绝，故在源头消毒）
    def _clean(o):
        if isinstance(o, float):
            return o if np.isfinite(o) else None
        if isinstance(o, dict):
            return {k: _clean(v) for k, v in o.items()}
        if isinstance(o, list):
            return [_clean(v) for v in o]
        return o

    (out / "metrics.json").write_text(
        json.dumps(
            _clean({"leaderboard": lb, "metrics": metrics, "meta": meta}),
            ensure_ascii=False,
            indent=1,
            allow_nan=False,
        ),
        encoding="utf-8",
    )
    (out / "holdings_latest.json").write_text(
        json.dumps(
            {"date": str(dates[-1].date()), "holdings": holdings_latest},
            ensure_ascii=False,
            indent=1,
            allow_nan=False,
        ),
        encoding="utf-8",
    )
    print(
        f"完成 → {out}（{meta['elapsed_sec']}s，因子 {len(scores)} 个，样本 {len(dates)} 期）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
