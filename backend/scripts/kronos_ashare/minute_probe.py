"""分钟级探针：Kronos 在 A 股 1 分钟 K 线上的基本预测力。

为什么单独一条链：A 股 T+1，当日买入不能当日卖出，分钟级预测**不能用于当日选股买入**，
只能用于 (a) 持仓日内卖出择时、(b) T+0 品种（ETF/可转债/期货）、(c) 尾盘/次日开盘择时。
所以这里同时算两个指标：

- 截面 RankIC：同一时刻跨股票排序（对应"选股"口径，A 股当日不可执行，仅作参考）
- 时序 IC：同一股票跨时点排序（对应"持仓卖出择时"口径，真正可执行）

口径：
- 输入：过去 ``lookback`` 根 1 分钟 K 线（base 的上下文上限 512，历史+预测 ≤ 512）
- 预测：未来 ``pred_len`` 根
- 分数：预测期均价 / 当前收盘 - 1
- 真实收益：``close[t+pred_len] / close[t] - 1``

用法（容器内，GPU）：
  python3 -m backend.scripts.kronos_ashare.minute_probe \
    --limit-symbols 50 --days 20 --lookback 480 --pred-len 30 \
    --times 10:30,14:00 --device cuda:0 \
    --output /data/kronos_ashare/minute_probe.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from backend.scripts.stock_classifier.panel import rankdata_nan

log = logging.getLogger("kronos_ashare.minute_probe")

MINUTE_DIR = Path("/data/quantdb/1_kline_data/min1_kline")
OHLCVA = ("open", "high", "low", "close", "volume", "amount")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Kronos 分钟级探针")
    p.add_argument("--data-dir", default=str(MINUTE_DIR))
    p.add_argument("--limit-symbols", type=int, default=50)
    p.add_argument("--days", type=int, default=20, help="取最近多少个交易日做探针")
    p.add_argument("--times", default="10:30,14:00", help="每日探针时点（逗号分隔）")
    p.add_argument("--lookback", type=int, default=480)
    p.add_argument("--pred-len", type=int, default=30)
    p.add_argument("--baseline-bars", type=int, default=30, help="朴素基线的回看 bar 数")
    p.add_argument("--topk", type=int, default=5)
    p.add_argument("--model", default="NeoQuasar/Kronos-base")
    p.add_argument("--tokenizer", default="NeoQuasar/Kronos-Tokenizer-base")
    p.add_argument("--device", default="cuda:0")
    p.add_argument("--max-context", type=int, default=512)
    p.add_argument("--sample-count", type=int, default=1)
    p.add_argument("--batch-size", type=int, default=64)
    p.add_argument("--kronos-home", default=None)
    p.add_argument("--output", required=True)
    return p.parse_args(argv)


def _load_symbol(path: Path) -> pd.DataFrame | None:
    """读取单只 1 分钟 K 线，返回按时间排序的 OHLCVA。"""
    try:
        frame = pd.read_parquet(path)
    except Exception as exc:  # noqa: BLE001
        log.warning("读取失败 %s: %s", path.name, exc)
        return None
    need = set(OHLCVA) | {"time"}
    if not need.issubset(frame.columns):
        return None
    frame = frame.sort_values("time").reset_index(drop=True)
    frame["time"] = pd.to_datetime(frame["time"])
    frame = frame[np.isfinite(frame[list(OHLCVA)].to_numpy(float)).all(axis=1)]
    # 过滤后必须重建索引，后面用 index 当作 bar 位置
    return frame.reset_index(drop=True) if len(frame) > 0 else None


def _probe_positions(frame: pd.DataFrame, days: int, times: list[str]) -> list[int]:
    """返回探针所在的 bar 位置：最近 days 天里每个指定时点的最后一根 bar。"""
    frame = frame.copy()
    frame["date"] = frame["time"].dt.date
    frame["hhmm"] = frame["time"].dt.strftime("%H:%M")
    dates = sorted(frame["date"].unique())[-days:]
    positions: list[int] = []
    for date in dates:
        day = frame[frame["date"] == date]
        for hhmm in times:
            hit = day[day["hhmm"] <= hhmm]
            if hit.empty:
                continue
            positions.append(int(hit.index[-1]))
    return positions


def _ic(x: np.ndarray, y: np.ndarray) -> float:
    rx = rankdata_nan(x)
    ry = rankdata_nan(y)
    ok = np.isfinite(rx) & np.isfinite(ry)
    if ok.sum() < 10:
        return float("nan")
    xd = rx[ok] - rx[ok].mean()
    yd = ry[ok] - ry[ok].mean()
    denom = float(np.sqrt((xd * xd).sum() * (yd * yd).sum()))
    return float((xd * yd).sum() / denom) if denom > 0 else float("nan")


def _summary(values: list[float]) -> dict[str, float]:
    arr = np.array([v for v in values if np.isfinite(v)], dtype=float)
    if len(arr) == 0:
        return {"n": 0.0, "mean": float("nan"), "std": float("nan"), "icir": float("nan"), "hit": float("nan")}
    std = float(arr.std(ddof=1)) if len(arr) > 1 else float("nan")
    return {
        "n": float(len(arr)),
        "mean": float(arr.mean()),
        "std": std,
        "icir": float(arr.mean() / std * np.sqrt(len(arr))) if std and np.isfinite(std) else float("nan"),
        "hit": float((arr > 0).mean()),
    }


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    times = [t.strip() for t in args.times.split(",") if t.strip()]

    from backend.scripts.kronos_ashare.kronos_runtime import KronosRunner

    files = sorted(Path(args.data_dir).glob("*.parquet"))[: args.limit_symbols]
    if not files:
        log.error("没有分钟数据：%s", args.data_dir)
        return 2

    frames: dict[str, pd.DataFrame] = {}
    for path in files:
        frame = _load_symbol(path)
        if frame is not None:
            frames[path.stem] = frame
    log.info("载入 %d 只分钟数据", len(frames))

    runner = KronosRunner(
        kronos_home=args.kronos_home,
        model_name=args.model,
        tokenizer_name=args.tokenizer,
        device=args.device,
        max_context=args.max_context,
    )

    probes: list[tuple[str, pd.Timestamp, int, pd.DataFrame]] = []
    for symbol, frame in frames.items():
        for pos in _probe_positions(frame, args.days, times):
            if pos < args.lookback or pos + args.pred_len >= len(frame):
                continue
            probes.append((symbol, frame.loc[pos, "time"], pos, frame))
    if not probes:
        log.error("没有可用探针窗口（历史不足 %d 根？）", args.lookback)
        return 2
    log.info("探针窗口 %d 个（%d 只 × %d 天 × %d 时点）", len(probes), len(frames), args.days, len(times))

    rows: list[dict] = []
    for start in range(0, len(probes), args.batch_size):
        chunk = probes[start : start + args.batch_size]
        dfs, x_stamps, y_stamps = [], [], []
        for _, _, pos, frame in chunk:
            hist = frame.iloc[pos - args.lookback + 1 : pos + 1]
            future = frame.iloc[pos + 1 : pos + args.pred_len + 1]
            dfs.append(hist[list(OHLCVA)].reset_index(drop=True))
            x_stamps.append(pd.Series(hist["time"].to_numpy()))
            y_stamps.append(pd.Series(future["time"].to_numpy()))
        preds = runner.predict_batch(
            dfs, x_stamps, y_stamps,
            pred_len=args.pred_len, sample_count=args.sample_count,
        )
        for (symbol, ts, pos, frame), pred in zip(chunk, preds, strict=True):
            last_close = float(frame.loc[pos, "close"])
            exit_close = float(frame.loc[pos + args.pred_len, "close"])
            pred_close = pd.to_numeric(pred["close"], errors="coerce").to_numpy(float)
            past_close = (
                float(frame.loc[pos - args.baseline_bars, "close"])
                if pos >= args.baseline_bars
                else float("nan")
            )
            mom = (
                last_close / past_close - 1.0
                if np.isfinite(past_close) and past_close > 0
                else float("nan")
            )
            rows.append(
                {
                    "symbol": symbol,
                    "time": str(pd.Timestamp(ts)),
                    "date": str(pd.Timestamp(ts).date()),
                    "hhmm": pd.Timestamp(ts).strftime("%H:%M"),
                    "score": float(np.nanmean(pred_close)) / last_close - 1.0,
                    "realized": exit_close / last_close - 1.0,
                    "mom": mom,
                    "rev": -mom,
                }
            )
        log.info("已完成 %d/%d", min(start + args.batch_size, len(probes)), len(probes))

    df = pd.DataFrame(rows)

    def _ics(col: str) -> tuple[list[float], list[float]]:
        cross, ts = [], []
        for _, day in df.groupby(["date", "hhmm"]):
            cross.append(_ic(day[col].to_numpy(float), day["realized"].to_numpy(float)))
        for _, sub in df.groupby("symbol"):
            if len(sub) >= 10:
                ts.append(_ic(sub[col].to_numpy(float), sub["realized"].to_numpy(float)))
        return cross, ts

    summary: dict = {
        "config": vars(args),
        "n_samples": int(len(df)),
        "mean_realized_abs": float(df["realized"].abs().mean()),
    }
    for col in ("score", "mom", "rev"):
        cross, ts = _ics(col)
        summary[f"cross_section_ic_{col}"] = _summary(cross)
        summary[f"time_series_ic_{col}"] = _summary(ts)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    df.to_csv(str(Path(args.output).with_suffix(".csv")), index=False)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
