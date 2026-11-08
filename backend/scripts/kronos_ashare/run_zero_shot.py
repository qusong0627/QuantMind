"""Kronos A股零样本截面测试 CLI。

用法（容器内或远端训练节点）：
  python3 -m backend.scripts.kronos_ashare.run_zero_shot \
    --qlib-dir /data/qlib/cn_data \
    --universe csi300 \
    --start 2025-01-01 --end 2026-11-06 \
    --signal-every 20 --max-dates 12 \
    --model NeoQuasar/Kronos-small --device cpu \
    --sample-count 2 --kronos-home /data/kronos/Kronos

先跑 ``--dry-run`` 验证数据与窗口，不加载模型。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from backend.scripts.kronos_ashare.data import (
    init_qlib,
    load_panel,
    read_instruments,
    trading_calendar,
)
from backend.scripts.kronos_ashare.metrics import (
    baseline_score,
    cross_section_metrics,
    summarize,
)
from backend.scripts.kronos_ashare.windows import (
    build_window,
    score_prediction,
    select_signal_dates,
)
from backend.scripts.stock_classifier.panel import is_limit_up

log = logging.getLogger("kronos_ashare.zero_shot")

BASE_COLUMNS = [
    "date",
    "symbol",
    "score",
    "fwd_ret_model",
    "fwd_ret_exec",
    "entry_change",
    "limit_up_entry",
    "momentum_20",
    "reversal_20",
]


def _default_output(universe: str, model: str) -> Path:
    root = Path("/data/kronos_ashare") if Path("/data").is_dir() else Path("data/kronos_ashare")
    tag = model.rsplit("/", 1)[-1].lower()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return root / f"zero_shot_{universe}_{tag}_{stamp}"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Kronos A股零样本截面测试")
    p.add_argument("--qlib-dir", default="/data/qlib/cn_data")
    p.add_argument("--universe", default="csi300")
    p.add_argument("--start", default="2025-01-01")
    p.add_argument("--end", default="2026-11-06")
    p.add_argument("--signal-every", type=int, default=20)
    p.add_argument("--max-dates", type=int, default=12)
    p.add_argument("--lookback", type=int, default=90)
    p.add_argument("--pred-len", type=int, default=20)
    p.add_argument("--entry-lag", type=int, default=1)
    p.add_argument("--topk", type=int, default=20)
    p.add_argument("--model", default="NeoQuasar/Kronos-small")
    p.add_argument("--tokenizer", default="NeoQuasar/Kronos-Tokenizer-base")
    p.add_argument("--device", default="cpu")
    p.add_argument("--max-context", type=int, default=512)
    p.add_argument("--sample-count", type=int, default=1)
    p.add_argument("--temperature", type=float, default=0.6)
    p.add_argument("--top-p", type=float, default=0.9)
    p.add_argument("--score-mode", choices=["mean", "end", "max", "min"], default="mean")
    p.add_argument("--batch-size", type=int, default=32)
    p.add_argument("--limit-symbols", type=int, default=0)
    p.add_argument("--kronos-home", default=None)
    p.add_argument("--output", default=None)
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args(argv)


def _collect_windows(panels, calendar, symbol_list, signal_date, args):
    windows = []
    for symbol in symbol_list:
        panel = panels.get(symbol)
        if panel is None or panel.empty:
            continue
        win = build_window(
            panel,
            calendar,
            symbol,
            signal_date,
            lookback=args.lookback,
            pred_len=args.pred_len,
            entry_lag=args.entry_lag,
        )
        if win is not None:
            windows.append(win)
    return windows


def _rows_for_date(windows, preds, panels, signal_date, args) -> pd.DataFrame:
    rows = []
    for win, pred in zip(windows, preds, strict=True):
        score = score_prediction(pred, win.last_close, mode=args.score_mode)
        panel = panels[win.symbol]
        rows.append(
            {
                "date": signal_date,
                "symbol": win.symbol,
                "score": score,
                "fwd_ret_model": win.fwd_ret_model,
                "fwd_ret_exec": win.fwd_ret_exec,
                "entry_change": win.entry_change,
                "momentum_20": baseline_score(panel, signal_date, 20, "momentum"),
                "reversal_20": baseline_score(panel, signal_date, 20, "reversal"),
            }
        )
    df = pd.DataFrame(rows, columns=BASE_COLUMNS)
    if df.empty:
        return df
    df["limit_up_entry"] = is_limit_up(df["symbol"], df["entry_change"])
    return df


def _date_metrics(df: pd.DataFrame, args) -> dict[str, dict[str, float]]:
    tradable = (~df["limit_up_entry"].fillna(False)) & np.isfinite(df["fwd_ret_exec"])
    out: dict[str, dict[str, float]] = {}
    for name, col in (
        ("kronos", "score"),
        ("momentum_20", "momentum_20"),
        ("reversal_20", "reversal_20"),
    ):
        out[name] = cross_section_metrics(
            df[col], df["fwd_ret_model"], topk=args.topk, tradable=tradable
        )
    return out


def _write_report(out_dir: Path, summary: dict, per_date: pd.DataFrame) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    per_date.to_csv(out_dir / "per_date.csv", index=False)

    lines = [
        "# Kronos A股零样本测试",
        "",
        f"- 模型：`{summary['config']['model']}`",
        f"- 股票池：`{summary['config']['universe']}`",
        f"- 窗口：{summary['config']['start']} ~ {summary['config']['end']}，"
        f"每 {summary['config']['signal_every']} 个交易日一个截面",
        f"- lookback={summary['config']['lookback']}，pred_len={summary['config']['pred_len']}，"
        f"entry_lag={summary['config']['entry_lag']}，sample_count={summary['config']['sample_count']}",
        "",
        "## 汇总",
        "",
        "| 信号 | 截面数 | IC 均值 | RankIC 均值 | RankIC ICIR | TopK 超额均值 | TopK 胜率 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name, s in summary["scores"].items():
        lines.append(
            f"| {name} | {int(s['n_dates'])} | {s['ic_mean']:.4f} | "
            f"{s['rank_ic_mean']:.4f} | {s['rank_ic_icir']:.2f} | "
            f"{s['topk_excess_mean']:.4%} | {s['topk_hit_mean']:.1%} |"
        )
    lines += [
        "",
        "## 口径",
        "",
        "- 信号日 T 收盘出分，T+entry_lag 收盘成交；",
        "- IC/RankIC 对 `close[T+pred_len]/close[T]-1`；",
        "- TopK 超额的收益用 `close[T+pred_len]/close[T+entry_lag]-1`，并剔除入场日涨停；",
        "- 涨停判定为板块近似（ST 按主板处理），正式结论需用 `compute_limits` 复核。",
        "",
        "> 研究脚本输出，不构成投资建议；未过生产准入。",
    ]
    (out_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    out_dir = Path(args.output) if args.output else _default_output(args.universe, args.model)

    init_qlib(args.qlib_dir)
    symbols = read_instruments(args.qlib_dir, args.universe)
    if args.limit_symbols and args.limit_symbols > 0:
        symbols = symbols[: args.limit_symbols]
    if not symbols:
        log.error("empty universe: %s", args.universe)
        return 2

    cal_start = (pd.Timestamp(args.start) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
    cal_end = (pd.Timestamp(args.end) + pd.Timedelta(days=120)).strftime("%Y-%m-%d")
    calendar = trading_calendar(cal_start, cal_end)
    signal_dates = select_signal_dates(
        calendar, args.start, args.end, every=args.signal_every, max_dates=args.max_dates
    )
    if len(signal_dates) == 0:
        log.error("no signal dates in [%s, %s]", args.start, args.end)
        return 2

    log.info("loading %d symbols ...", len(symbols))
    panels = load_panel(symbols, cal_start, cal_end)
    log.info("loaded %d panels; signal dates=%d", len(panels), len(signal_dates))

    all_rows: list[pd.DataFrame] = []
    per_date_summary: list[dict] = []
    dry_counts: list[dict] = []

    runner = None
    if not args.dry_run:
        from backend.scripts.kronos_ashare.kronos_runtime import KronosRunner

        runner = KronosRunner(
            kronos_home=args.kronos_home,
            model_name=args.model,
            tokenizer_name=args.tokenizer,
            device=args.device,
            max_context=args.max_context,
        )

    for signal_date in signal_dates:
        windows = _collect_windows(panels, calendar, symbols, signal_date, args)
        if not windows:
            log.warning("%s: no valid windows", signal_date.date())
            continue
        if args.dry_run:
            dry_counts.append({"date": str(signal_date.date()), "windows": len(windows)})
            log.info("%s: %d windows (dry-run)", signal_date.date(), len(windows))
            continue

        preds: list[pd.DataFrame] = []
        for start in range(0, len(windows), args.batch_size):
            chunk = windows[start : start + args.batch_size]
            preds.extend(
                runner.predict_batch(
                    [w.x_df for w in chunk],
                    [pd.Series(w.hist_dates) for w in chunk],
                    [pd.Series(w.future_dates) for w in chunk],
                    pred_len=args.pred_len,
                    temperature=args.temperature,
                    top_p=args.top_p,
                    sample_count=args.sample_count,
                )
            )
        df = _rows_for_date(windows, preds, panels, signal_date, args)
        if df.empty:
            continue
        all_rows.append(df)
        m = _date_metrics(df, args)
        per_date_summary.append({"date": str(signal_date.date()), **m})
        log.info(
            "%s: n=%d kronos rank_ic=%.4f topk=%.4f",
            signal_date.date(),
            len(df),
            m["kronos"]["rank_ic"],
            m["kronos"]["topk_excess"],
        )

    if args.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "dry_run.json").write_text(
            json.dumps({"config": vars(args), "dates": dry_counts}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log.info("dry-run done: %s", out_dir)
        return 0

    if not all_rows:
        log.error("no predictions produced")
        return 2

    per_date = pd.concat(all_rows, ignore_index=True)
    summary = {
        "config": {
            "model": args.model,
            "tokenizer": args.tokenizer,
            "universe": args.universe,
            "start": args.start,
            "end": args.end,
            "signal_every": args.signal_every,
            "lookback": args.lookback,
            "pred_len": args.pred_len,
            "entry_lag": args.entry_lag,
            "topk": args.topk,
            "sample_count": args.sample_count,
            "temperature": args.temperature,
            "top_p": args.top_p,
            "score_mode": args.score_mode,
            "device": args.device,
            "n_symbols": len(symbols),
        },
        "scores": {
            name: summarize(entry[name] for entry in per_date_summary)
            for name in ("kronos", "momentum_20", "reversal_20")
        },
        "per_date": per_date_summary,
    }
    _write_report(out_dir, summary, per_date)
    print(json.dumps(summary["scores"], ensure_ascii=False, indent=2))
    log.info("report written: %s", out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
