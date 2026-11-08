"""路径数收敛实验：1/2/3/5/10/20 条采样路径对截面指标的影响。

做法：对同一个 (信号日, 股票)，独立采样 K 次（每次 sample_count=1），
保留每条路径的分数，再按前 N 条的均值算截面指标，N=1..K。
这样每个 N 用的是**同一批路径的前缀**，是配对比较，不是换一批样本。

用法（容器内，GPU）：
  python3 -m backend.scripts.kronos_ashare.path_convergence \
    --limit-symbols 50 --max-dates 12 --paths 20 \
    --lookback 100 --pred-len 10 --device cuda:0 \
    --output /data/kronos_ashare/convergence.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from backend.scripts.kronos_ashare.data import (
    init_qlib,
    load_panel,
    read_instruments,
    trading_calendar,
)
from backend.scripts.kronos_ashare.metrics import baseline_score, cross_section_metrics
from backend.scripts.kronos_ashare.windows import (
    build_window,
    score_prediction,
    select_signal_dates,
)
from backend.scripts.stock_classifier.panel import is_limit_up

log = logging.getLogger("kronos_ashare.path_convergence")

PATH_COUNTS = (1, 2, 3, 5, 10, 20)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Kronos 采样路径数收敛实验")
    p.add_argument("--qlib-dir", default="/data/qlib/cn_data")
    p.add_argument("--universe", default="csi300")
    p.add_argument("--start", default="2025-01-01")
    p.add_argument("--end", default="2026-06-30")
    p.add_argument("--signal-every", type=int, default=20)
    p.add_argument("--max-dates", type=int, default=12)
    p.add_argument("--lookback", type=int, default=100)
    p.add_argument("--pred-len", type=int, default=10)
    p.add_argument("--paths", type=int, default=20)
    p.add_argument("--topk", type=int, default=20)
    p.add_argument("--model", default="NeoQuasar/Kronos-base")
    p.add_argument("--tokenizer", default="NeoQuasar/Kronos-Tokenizer-base")
    p.add_argument("--device", default="cuda:0")
    p.add_argument("--max-context", type=int, default=512)
    p.add_argument("--temperature", type=float, default=0.6)
    p.add_argument("--top-p", type=float, default=0.9)
    p.add_argument("--batch-size", type=int, default=32)
    p.add_argument("--limit-symbols", type=int, default=50)
    p.add_argument("--kronos-home", default=None)
    p.add_argument("--output", required=True)
    return p.parse_args(argv)


def _summarize(values: list[float]) -> dict[str, float]:
    arr = np.array([v for v in values if np.isfinite(v)], dtype=float)
    if len(arr) == 0:
        return {"mean": float("nan"), "std": float("nan"), "icir": float("nan")}
    std = float(arr.std(ddof=1)) if len(arr) > 1 else float("nan")
    return {
        "mean": float(arr.mean()),
        "std": std,
        "icir": float(arr.mean() / std * np.sqrt(len(arr))) if std and np.isfinite(std) else float("nan"),
    }


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    init_qlib(args.qlib_dir)
    symbols = read_instruments(args.qlib_dir, args.universe)[: args.limit_symbols]
    cal_start = (pd.Timestamp(args.start) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
    cal_end = (pd.Timestamp(args.end) + pd.Timedelta(days=120)).strftime("%Y-%m-%d")
    calendar = trading_calendar(cal_start, cal_end)
    dates = select_signal_dates(
        calendar, args.start, args.end, every=args.signal_every, max_dates=args.max_dates
    )
    panels = load_panel(symbols, cal_start, cal_end)

    from backend.scripts.kronos_ashare.kronos_runtime import KronosRunner

    runner = KronosRunner(
        kronos_home=args.kronos_home,
        model_name=args.model,
        tokenizer_name=args.tokenizer,
        device=args.device,
        max_context=args.max_context,
    )

    # 每个目标路径数都要有的日期级指标
    targets = [n for n in PATH_COUNTS if n <= args.paths]
    per_date: list[dict] = []
    dispersion_pairs: list[tuple[float, float]] = []

    for signal_date in dates:
        windows = []
        for symbol in symbols:
            panel = panels.get(symbol)
            if panel is None or panel.empty:
                continue
            win = build_window(
                panel, calendar, symbol, signal_date,
                lookback=args.lookback, pred_len=args.pred_len, entry_lag=1,
            )
            if win is not None:
                windows.append(win)
        if len(windows) < 20:
            log.warning("%s: 有效窗口不足 (%d)", signal_date.date(), len(windows))
            continue

        # (窗口数 × 路径数)
        path_scores = np.full((len(windows), args.paths), np.nan)
        for k in range(args.paths):
            for start in range(0, len(windows), args.batch_size):
                chunk = windows[start : start + args.batch_size]
                preds = runner.predict_batch(
                    [w.x_df for w in chunk],
                    [pd.Series(w.hist_dates) for w in chunk],
                    [pd.Series(w.future_dates) for w in chunk],
                    pred_len=args.pred_len,
                    temperature=args.temperature,
                    top_p=args.top_p,
                    sample_count=1,
                )
                for j, (win, pred) in enumerate(zip(chunk, preds, strict=True)):
                    path_scores[start + j, k] = score_prediction(pred, win.last_close, mode="mean")

        fwd = np.array([w.fwd_ret_model for w in windows], dtype=float)
        entry_change = np.array([w.entry_change for w in windows], dtype=float)
        raw_symbols = pd.Series([w.symbol for w in windows])
        tradable = pd.Series(~is_limit_up(raw_symbols, pd.Series(entry_change)).astype(bool))
        panic_scores = pd.Series([
            baseline_score(panels[w.symbol], signal_date, 20, "momentum") for w in windows
        ])

        for n in targets:
            mean_score = pd.Series(np.nanmean(path_scores[:, :n], axis=1))
            m = cross_section_metrics(
                mean_score, pd.Series(fwd), topk=args.topk, tradable=tradable
            )
            base = cross_section_metrics(panic_scores, pd.Series(fwd), topk=args.topk, tradable=tradable)
            per_date.append(
                {
                    "date": str(signal_date.date()),
                    "paths": n,
                    "rank_ic": m["rank_ic"],
                    "topk_excess": m["topk_excess"],
                    "momentum_rank_ic": base["rank_ic"],
                }
            )

        # 路径离散度与 |预测收益| 的关系：不确定性是否含有信息
        if args.paths > 1:
            disp = np.nanstd(path_scores, axis=1)
            mean_abs = np.abs(np.nanmean(path_scores, axis=1))
            ok = np.isfinite(disp) & np.isfinite(mean_abs)
            if ok.sum() >= 20:
                dispersion_pairs.append((float(np.corrcoef(disp[ok], mean_abs[ok])[0, 1]),
                                         float(np.nanmean(disp))))
        log.info("%s: %d 只 × %d 条路径完成", signal_date.date(), len(windows), args.paths)

    df = pd.DataFrame(per_date)
    summary: dict[str, dict[str, float]] = {}
    for n in targets:
        sub = df[df["paths"] == n]
        summary[str(n)] = {
            "n_dates": float(len(sub)),
            "rank_ic": _summarize(sub["rank_ic"].tolist()),
            "topk_excess": _summarize(sub["topk_excess"].tolist()),
            "momentum_rank_ic": _summarize(sub["momentum_rank_ic"].tolist()),
        }

    payload = {
        "config": vars(args),
        "summary": summary,
        "per_date": per_date,
        "dispersion_vs_abs_score": {
            "mean_corr": float(np.mean([p[0] for p in dispersion_pairs])) if dispersion_pairs else float("nan"),
            "mean_dispersion": float(np.mean([p[1] for p in dispersion_pairs])) if dispersion_pairs else float("nan"),
        },
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"{'paths':>5s} {'RankIC':>9s} {'ICIR':>6s} {'TopK':>9s}")
    for n in targets:
        s = summary[str(n)]
        print(
            f"{n:5d} {s['rank_ic']['mean']:+9.4f} {s['rank_ic']['icir']:+6.2f} "
            f"{s['topk_excess']['mean']:+9.2%}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
