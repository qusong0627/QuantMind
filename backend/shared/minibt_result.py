# -*- coding: utf-8 -*-
"""minibt 回测结果提取与输出(运行于 ai-ide minibt 运行时容器)。

职责:
1. 从 Bt 实例提取逐 bar 权益/交易统计,修正 minibt 自带报告的口径
   (其"最终收益"不含未平仓浮盈;max_drawdown 为负值等);
2. 以 ``[RESULT] key: value`` 行打印标量指标 —— ai-ide 前端
   (AIIDEPage.tsx ingestExecuteResultLine 正则)直接解析,零前端改动;
3. 将完整结果(对齐 electron/src/features/strategy-lab/types/index.ts 的
   StrategyLabRunResult 结构)写 result.json,供 /execute/result/{job_id} 读取。

只依赖 numpy/pandas,在 minibt 运行时镜像内随 backend/ 卷可导入。
"""
from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

import numpy as np
import pandas as pd

_RESULT_DIR_ENV = "QM_MINIBT_RESULT_DIR"
_DEFAULT_RESULT_DIR = "/app/result"

# 写进 result.json warnings 的口径提示(与 minibt/PATCHES.md 保持一致)
_CALIBRATION_WARNINGS = [
    "minibt 撮合口径: 信号当根K线收盘价成交, 无 T+1/涨跌停/整手约束, 结果与实盘存在口径差",
    "默认手续费为 0, 如未显式配置 percent_commission, 收益指标偏乐观",
]


def _safe_float(value: Any) -> float | None:
    try:
        out = float(value)
    except Exception:
        return None
    if not np.isfinite(out):
        return None
    return out


def _get_stats(bt: Any) -> Any:
    """qs_stats 必须在 run() 之后调用(内部有 __is_finish 校验)。"""
    try:
        return bt.qs_stats(0)
    except Exception:
        return None


def _extract_metrics(bt: Any, res: pd.DataFrame) -> dict[str, float | None]:
    total_profit = res["total_profit"].astype(float)
    base_equity = float(total_profit.iloc[0]) or 1.0

    stats = _get_stats(bt)
    win_rate = _safe_float(getattr(stats, "win_rate", lambda: None)()) if stats is not None else None

    # 盈亏比: quantstats 的 profit_ratio() 不是盈亏比,用 avg_win/|avg_loss|
    profit_factor: float | None = None
    if stats is not None:
        avg_win = _safe_float(getattr(stats, "avg_win", lambda: None)())
        avg_loss = _safe_float(getattr(stats, "avg_loss", lambda: None)())
        if avg_win is not None and avg_loss not in (None, 0.0):
            profit_factor = avg_win / abs(avg_loss)

    positions = res["positions"].astype(float)
    # 开仓次数(0→±1,含开多与开空)= 完整回合数;minibt 持平时 sell 会直接开空
    n_trades = int(((positions != 0) & (positions.shift(fill_value=0) == 0)).sum())
    max_drawdown = _safe_float(getattr(stats, "max_drawdown", lambda: None)()) if stats is not None else None
    if max_drawdown is not None:
        max_drawdown = abs(max_drawdown)

    return {
        "cum_return": _safe_float(total_profit.iloc[-1] / base_equity - 1.0),
        "annual_return": _safe_float(getattr(stats, "cagr", lambda: None)()) if stats is not None else None,
        "sharpe": _safe_float(getattr(stats, "sharpe", lambda: None)()) if stats is not None else None,
        "max_drawdown": max_drawdown,
        "win_rate": win_rate,
        "n_trades": float(n_trades),
        "avg_position": _safe_float(positions.mean()),
        "profit_factor": profit_factor,
        "total_fee": _safe_float(res["total_fee"].astype(float).iloc[-1]) if len(res) else None,
        "final_equity": _safe_float(total_profit.iloc[-1]) if len(res) else None,
    }


def _print_result_lines(metrics: dict[str, float | None], elapsed: float) -> None:
    """按前端正则 `^\\[RESULT\\]\\s+([a-z_]+):\\s+(-?\\d+(?:\\.\\d+)?)$` 输出裸数字。"""
    print(f"[RESULT] backtest_elapsed_sec: {elapsed:.2f}")
    for key, value in metrics.items():
        if value is None:
            continue
        if key == "n_trades":
            print(f"[RESULT] {key}: {int(value)}")
        else:
            print(f"[RESULT] {key}: {value:.6f}")


def _build_run_result(
    bt: Any,
    source_df: pd.DataFrame,
    res: pd.DataFrame,
    metrics: dict[str, float | None],
    elapsed: float,
    started_at: float,
) -> dict[str, Any]:
    dates = source_df["datetime"].tolist() if "datetime" in source_df.columns else []
    total_profit = res["total_profit"].astype(float).tolist()
    n = min(len(dates), len(total_profit))
    equity = [
        {
            "date": pd.Timestamp(dates[i]).isoformat(),
            "value": round(float(total_profit[i]), 4),
            "benchmark": None,
        }
        for i in range(n)
    ]

    metrics_payload = {
        "cum_return": metrics.get("cum_return") or 0.0,
        "annual_return": metrics.get("annual_return") or 0.0,
        "sharpe": metrics.get("sharpe") or 0.0,
        "max_drawdown": metrics.get("max_drawdown") or 0.0,
        "win_rate": metrics.get("win_rate") or 0.0,
        "n_trades": int(metrics.get("n_trades") or 0),
        "avg_position": metrics.get("avg_position") or 0.0,
    }

    return {
        "run_id": os.getenv("AI_IDE_BACKTEST_RUN_ID") or str(uuid.uuid4()),
        "status": "success",
        "metrics": metrics_payload,
        "equity": equity,
        "trades": [],
        "positions": [],
        "overlays": {},
        "logs": [],
        "warnings": list(_CALIBRATION_WARNINGS),
        "error": None,
        "error_traceback": None,
        "config": {"engine": "minibt", "market": os.getenv("AI_IDE_BACKTEST_MARKET", "CN")},
        "script_sha": "",
        "data_snapshot_at": (
            pd.Timestamp(dates[-1]).isoformat() if dates else None
        ),
        "elapsed_sec": round(elapsed, 2),
        "started_at": started_at,
        "finished_at": time.time(),
        "extra": {
            "profit_factor": metrics.get("profit_factor"),
            "total_fee": metrics.get("total_fee"),
            "final_equity": metrics.get("final_equity"),
        },
    }


def run_and_report(bt: Any, source_df: pd.DataFrame, *, result_dir: str | None = None) -> dict[str, Any]:
    """执行回测并输出结果。策略脚本尾部调用一次即可。

    Args:
        bt: 已 addstrategy 的 minibt Bt 实例(内部执行 run(isplot=False, isreport=False))
        source_df: 喂给 get_kline 的同一份 DataFrame(用于权益曲线日期对齐)
        result_dir: result.json 输出目录;默认环境变量 QM_MINIBT_RESULT_DIR(/app/result)

    Returns:
        组装好的 StrategyLabRunResult 形状 dict(写盘失败不影响返回与 [RESULT] 输出)。
    """
    started_at = time.time()
    bt.run(isplot=False, isreport=False)
    elapsed = time.time() - started_at

    results = bt.get_results()
    res = results[0][0] if results and results[0] else pd.DataFrame()
    if res.empty:
        print("[ERROR] minibt 回测无结果(get_results 为空)")
        return {}

    metrics = _extract_metrics(bt, res)
    _print_result_lines(metrics, elapsed)

    payload = _build_run_result(bt, source_df, res, metrics, elapsed, started_at)
    out_dir = result_dir or os.getenv(_RESULT_DIR_ENV, _DEFAULT_RESULT_DIR)
    try:
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "result.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, allow_nan=False)
    except Exception as exc:  # 写盘失败不影响已流式输出的指标
        print(f"[WARNING] result.json 写盘失败: {exc}")
    return payload
