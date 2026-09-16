#!/usr/bin/env python3
"""模型滚动 IC 监控：pred.parquet + QuantDB 前复权收盘 → 日频 rank IC / IR 滚动窗口。

背景：模型卡片给的是整段静态指标（train/val/test），看不出「最近这一个月还行不行」。
本脚本按最新数据现算前向收益标签，输出每个模型的近期滚动表现，供
「小仓观察 / 加仓 / 停用」的日常判断使用。

用法（容器内执行）:
    docker exec -w /app quantmind python backend/scripts/model_ic_monitor.py \
        --model-id mdl_cust_train_20260914130341_887a7a0d_c2e90650 --days 90
    # 多模型对比 / 指定窗口：
    ... --model-id A --model-id B --days 120 --windows 5,10,20,60

口径（与训练指标同源，可对照 metadata.metrics）:
  - 标签：QuantDB daily_forward 前复权收盘，T+lag 执行、持有 H 日
    （lag/H 读模型 metadata.execution_lag_days / target_horizon_days）；
  - 日频 IC = 当日截面 Spearman(pred, 前向收益)（≥50 只样本才计）；
  - 每日 IC 序列的 mean/IR，以及 t = IR × √n；月度池化 IC（跨日排序能力）。
预测分来源：pred.parquet（训练产物 + 推理回写的行；回写行 label 为空，用收盘现算）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

if Path("/app/models/users").is_dir():
    MODELS_USERS_ROOT = Path("/app/models/users")
else:
    MODELS_USERS_ROOT = PROJECT_ROOT / "models" / "users"

DEFAULT_TENANT = "default"
DEFAULT_USER = "00000001"
MIN_SAMPLES_PER_DAY = 50


def _find_model_dir(model_id: str) -> Path:
    hits = [p for p in MODELS_USERS_ROOT.glob(f"*/*/{model_id}") if p.is_dir()]
    hits += [p for p in MODELS_USERS_ROOT.glob(f"*/*/*/{model_id}") if p.is_dir()]
    if not hits:
        raise SystemExit(f"模型目录未找到: {model_id}")
    return hits[0]


def _load_metadata(model_dir: Path) -> dict:
    f = model_dir / "metadata.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}


def _resolve_kline_dir(quantdb_dir: Path, meta: dict) -> Path:
    """前复权行情树：pin 目录优先，CUSTOM 等派生数据集没有行情树时回退市场默认目录。

    自定义市场数据集（/data/quantcustom）只存合并后的因子分区，行情/标签同源于
    A 股 QuantDB（daily_forward），因此回退读 CN 默认目录是对的。
    """
    cand = Path(quantdb_dir) / "1_kline_data" / "daily_forward"
    if cand.is_dir():
        return cand
    try:
        from backend.services.engine.data_platform.quantdb_factor_reader import (
            market_data_dir,
            normalize_market,
        )

        market = normalize_market(
            str((meta.get("context") or {}).get("market") or "CN")
        )
        alt = market_data_dir(market) / "1_kline_data" / "daily_forward"
        if alt.is_dir():
            return alt
    except Exception:  # noqa: BLE001
        pass
    return cand


def _forward_returns(
    kline: Path, start: pd.Timestamp, end: pd.Timestamp, lag: int, horizon: int
) -> pd.DataFrame:
    """按日分区读取前复权收盘，构造 (symbol, trade_date, fwd_ret)。"""
    if not Path(kline).is_dir():
        raise SystemExit(f"daily_forward 目录不存在: {kline}")
    lo = (start - pd.Timedelta(days=40)).strftime("%Y%m%d")
    frames = []
    for part in sorted(Path(kline).glob("dt=*")):
        dt = part.name[3:]
        if not (dt.isdigit() and len(dt) == 8):
            continue
        # 前后各留足 lag+horizon+缓冲 的交易日（分区跨度用自然日粗筛）
        if dt < lo or dt > (end + pd.Timedelta(days=40)).strftime("%Y%m%d"):
            continue
        f = part / "data.parquet"
        if f.exists():
            d = pd.read_parquet(f, columns=["symbol", "close"])
            d["trade_date"] = pd.Timestamp(dt)
            frames.append(d)
    if not frames:
        raise SystemExit("未读到任何行情分区")
    cl = pd.concat(frames, ignore_index=True).sort_values(["symbol", "trade_date"])
    g = cl.groupby("symbol")["close"]
    cl["fwd_ret"] = g.shift(-(lag + horizon)) / g.shift(-lag) - 1.0
    return cl[["symbol", "trade_date", "fwd_ret"]]


def _daily_ic(df: pd.DataFrame) -> pd.Series:
    def f(g: pd.DataFrame) -> float:
        g = g[["pred", "fwd_ret"]].dropna()
        if len(g) < MIN_SAMPLES_PER_DAY or g["pred"].nunique() < 10:
            return np.nan
        return g["pred"].rank().corr(g["fwd_ret"].rank())

    return df.groupby("trade_date", sort=True).apply(f).dropna()


def _window_stats(s: pd.Series, n: int) -> dict:
    sub = s.iloc[-n:]
    if not len(sub):
        return {}
    mean = float(sub.mean())
    std = float(sub.std())
    ir = mean / std if std > 0 else float("nan")
    return {
        "days": int(len(sub)),
        "mean_ic": round(mean, 4),
        "ir": round(ir, 3),
        "t_stat": round(ir * np.sqrt(len(sub)), 2),
        "win_rate": round(float((sub > 0).mean()), 2),
    }


def monitor(model_id: str, days: int, windows: list[int]) -> dict:
    from backend.shared.stock_utils import StockCodeUtil

    model_dir = _find_model_dir(model_id)
    meta = _load_metadata(model_dir)
    lag = max(1, int(meta.get("execution_lag_days") or 1))
    horizon = max(1, int(meta.get("target_horizon_days") or 5))
    quantdb_dir = Path(str(meta.get("quantdb_dir") or "/data/quantdb"))

    pred_file = model_dir / "pred.parquet"
    if not pred_file.is_file():
        raise SystemExit(f"无 pred.parquet: {model_dir}")
    pred = pd.read_parquet(pred_file, columns=["symbol", "trade_date", "pred"])
    pred["trade_date"] = pd.to_datetime(pred["trade_date"])
    end = pred["trade_date"].max()
    start = end - pd.Timedelta(days=days * 2)  # 自然日粗筛，窗口由交易日数决定
    pred = pred[pred["trade_date"] >= start]

    fwd = _forward_returns(
        _resolve_kline_dir(quantdb_dir, meta), start, end, lag, horizon
    )
    fwd["symbol"] = fwd["symbol"].map(StockCodeUtil.to_prefix)
    df = pred.merge(fwd, on=["symbol", "trade_date"], how="inner")
    ic = _daily_ic(df)
    if not len(ic):
        raise SystemExit("无法计算日 IC（样本不足）")
    ic = ic.iloc[-days:]

    monthly = ic.groupby(ic.index.to_period("M")).mean().dropna()
    result = {
        "model_id": model_id,
        "model_dir": str(model_dir),
        "horizon": f"T+{lag} 执行 / 持有 {horizon} 日",
        "factor_dir": str(quantdb_dir),
        "kline_dir": str(_resolve_kline_dir(quantdb_dir, meta)),
        "latest_ic_date": str(ic.index.max())[:10],
        "windows": {f"last_{n}": _window_stats(ic, n) for n in windows},
        "monthly_mean_daily_ic": {
            str(k): round(float(v), 4) for k, v in monthly.tail(8).items()
        },
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="模型滚动 IC 监控")
    ap.add_argument("--model-id", action="append", required=True, help="可多次传入")
    ap.add_argument("--days", type=int, default=90, help="日 IC 序列保留的最大交易日数")
    ap.add_argument(
        "--windows", default="5,10,20,60", help="滚动窗口（交易日），逗号分隔"
    )
    args = ap.parse_args()
    windows = [int(x) for x in str(args.windows).split(",") if x.strip()]
    for mid in args.model_id:
        print(f"\n===== {mid} =====")
        monitor(mid, args.days, windows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
