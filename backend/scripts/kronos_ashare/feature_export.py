"""把 Kronos 预测导出成可训练的日频特征面板。

与 ``run_zero_shot`` 的区别：
- ``run_zero_shot`` 算的是截面指标（IC/TopK），只取少量信号日；
- 本模块为**训练**服务：每个交易日、每只股票算一行特征，落成 parquet，
  供 ``ablation`` 做「有/无 Kronos 特征」的对照训练。

产物列（前缀 ``k_``）：
- ``k_score_mean``：预测期均价 / T 收盘 - 1（与零样本测试同口径）
- ``k_score_end``：预测期末值 / T 收盘 - 1
- ``k_path_vol``：预测路径逐日收益的标准差（模型认为的预期波动）
- ``k_path_range``：(预测期最高 - 最低) / 均价（预期振幅）
- ``k_path_slope``：预测收盘对时间回归的斜率 / T 收盘（预期趋势强度）

用法（容器内，GPU）：
  python3 -m backend.scripts.kronos_ashare.feature_export \
    --start 2024-06-03 --end 2026-06-30 --step-days 1 \
    --limit-symbols 300 --device cuda:0 --sample-count 1 \
    --output /data/kronos_ashare/features/base_csi300_daily.parquet
"""

from __future__ import annotations

import argparse
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
from backend.scripts.kronos_ashare.windows import build_window

log = logging.getLogger("kronos_ashare.feature_export")

FEATURE_COLUMNS = (
    "k_score_mean",
    "k_score_end",
    "k_path_vol",
    "k_path_range",
    "k_path_slope",
)


def path_features(pred_df: pd.DataFrame, last_close: float) -> dict[str, float]:
    """把一条预测路径压成若干标量特征；价格非法时返回全 NaN。"""
    nan_row = dict.fromkeys(FEATURE_COLUMNS, float("nan"))
    if last_close <= 0 or "close" not in pred_df.columns:
        return nan_row
    close = pd.to_numeric(pred_df["close"], errors="coerce").to_numpy(dtype=float)
    if len(close) == 0 or not np.isfinite(close).all():
        return nan_row

    mean_price = float(close.mean())
    rets = close[1:] / close[:-1] - 1.0 if len(close) > 1 else np.array([], dtype=float)
    slope = float(np.polyfit(np.arange(len(close)), close, 1)[0]) if len(close) > 1 else 0.0
    return {
        "k_score_mean": mean_price / last_close - 1.0,
        "k_score_end": float(close[-1]) / last_close - 1.0,
        "k_path_vol": float(rets.std(ddof=1)) if len(rets) > 1 else 0.0,
        "k_path_range": (
            float(close.max() - close.min()) / mean_price if mean_price > 0 else float("nan")
        ),
        "k_path_slope": slope / last_close,
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Kronos 日频特征导出")
    p.add_argument("--qlib-dir", default="/data/qlib/cn_data")
    p.add_argument("--universe", default="csi300")
    p.add_argument("--start", default="2024-06-03")
    p.add_argument("--end", default="2026-06-30")
    p.add_argument("--step-days", type=int, default=1, help="每隔多少个交易日导出一个截面")
    p.add_argument("--lookback", type=int, default=90)
    p.add_argument("--pred-len", type=int, default=10)
    p.add_argument("--model", default="NeoQuasar/Kronos-base")
    p.add_argument("--tokenizer", default="NeoQuasar/Kronos-Tokenizer-base")
    p.add_argument("--device", default="cuda:0")
    p.add_argument("--max-context", type=int, default=512)
    p.add_argument("--sample-count", type=int, default=1)
    p.add_argument("--temperature", type=float, default=0.6)
    p.add_argument("--top-p", type=float, default=0.9)
    p.add_argument("--batch-size", type=int, default=64)
    p.add_argument("--limit-symbols", type=int, default=0)
    p.add_argument("--kronos-home", default=None)
    p.add_argument("--output", required=True)
    p.add_argument("--resume", action="store_true", help="已存在输出时续跑，跳过已有日期")
    p.add_argument("--checkpoint-every", type=int, default=40, help="每多少个截面落盘一次")
    return p.parse_args(argv)


def _flush(frames: list[pd.DataFrame], out_path: Path, done: set[str]) -> set[str]:
    """把内存中的截面合并进输出文件，返回新的已完成日期集合。"""
    if not frames:
        return done
    new = pd.concat(frames, ignore_index=True)
    if out_path.is_file():
        new = pd.concat([pd.read_parquet(out_path), new], ignore_index=True)
    new = new.drop_duplicates(subset=["date", "symbol"], keep="last")
    new.to_parquet(out_path, index=False)
    log.info("checkpoint: %s 累计 %d 行 / %d 个截面", out_path, len(new), new["date"].nunique())
    return set(new["date"].astype(str).unique())


def _signal_dates(calendar, start: str, end: str, step: int) -> pd.DatetimeIndex:
    mask = (calendar >= pd.Timestamp(start)) & (calendar <= pd.Timestamp(end))
    positions = np.flatnonzero(mask)
    if step > 1:
        positions = positions[::step]
    return pd.DatetimeIndex(calendar[positions])


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    init_qlib(args.qlib_dir)
    symbols = read_instruments(args.qlib_dir, args.universe)
    if args.limit_symbols > 0:
        symbols = symbols[: args.limit_symbols]
    if not symbols:
        log.error("empty universe: %s", args.universe)
        return 2

    cal_start = (pd.Timestamp(args.start) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
    cal_end = (pd.Timestamp(args.end) + pd.Timedelta(days=120)).strftime("%Y-%m-%d")
    calendar = trading_calendar(cal_start, cal_end)
    dates = _signal_dates(calendar, args.start, args.end, args.step_days)
    if len(dates) == 0:
        log.error("no signal dates in [%s, %s]", args.start, args.end)
        return 2

    done_dates: set[str] = set()
    if args.resume and out_path.is_file():
        done_dates = set(pd.read_parquet(out_path, columns=["date"])["date"].astype(str).unique())
        log.info("resume: %d dates already done", len(done_dates))

    log.info("loading %d symbols ...", len(symbols))
    panels = load_panel(symbols, cal_start, cal_end)
    log.info("loaded %d panels; signal dates=%d", len(panels), len(dates))

    from backend.scripts.kronos_ashare.kronos_runtime import KronosRunner

    runner = KronosRunner(
        kronos_home=args.kronos_home,
        model_name=args.model,
        tokenizer_name=args.tokenizer,
        device=args.device,
        max_context=args.max_context,
    )

    frames: list[pd.DataFrame] = []
    for i, signal_date in enumerate(dates, start=1):
        if str(signal_date.date()) in done_dates:
            continue
        windows = []
        for symbol in symbols:
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
                entry_lag=1,
            )
            if win is not None:
                windows.append(win)
        if not windows:
            log.warning("%s: no valid windows", signal_date.date())
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

        rows = []
        for win, pred in zip(windows, preds, strict=True):
            row = {"date": signal_date, "symbol": win.symbol}
            row.update(path_features(pred, win.last_close))
            rows.append(row)
        frames.append(pd.DataFrame(rows))
        log.info(
            "[%d/%d] %s: %d rows", i, len(dates), signal_date.date(), len(rows)
        )
        if len(frames) >= args.checkpoint_every:
            done_dates = _flush(frames, out_path, done_dates)
            frames.clear()

    if not frames and not out_path.is_file():
        log.error("no features produced")
        return 2

    done_dates = _flush(frames, out_path, done_dates)
    total = pd.read_parquet(out_path, columns=["date", "symbol"])
    log.info("written %s: %d rows, %d dates", out_path, len(total), total["date"].nunique())
    return 0


if __name__ == "__main__":
    sys.exit(main())
