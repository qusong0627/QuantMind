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
import re
import time
import uuid
from typing import Any

import numpy as np
import pandas as pd

_RESULT_DIR_ENV = "QM_MINIBT_RESULT_DIR"
_DEFAULT_RESULT_DIR = "/app/result"
# 逐笔明细开关:回测中心需要交易明细,AI-IDE 默认口径保持不变(不设=空列表)
_INCLUDE_TRADES_ENV = "QM_MINIBT_INCLUDE_TRADES"
# 初始资金:回测中心按请求里的 initial_capital 跑,AI-IDE 不设=用 minibt 默认
_INITIAL_CAPITAL_ENV = "QM_MINIBT_INITIAL_CAPITAL"

# 写进 result.json warnings 的口径提示(与 minibt/PATCHES.md 保持一致)
_CALIBRATION_WARNINGS = [
    "minibt 撮合口径: 信号当根K线收盘价成交, 无 T+1/涨跌停/整手约束, 结果与实盘存在口径差",
    "默认手续费为 0, 如未显式配置 percent_commission, 收益指标偏乐观",
]
_TRADE_CALIBRATION_WARNING = (
    "逐笔明细来自 minibt 订单流水(只有买卖方向, 无开/平标记与逐笔盈亏)"
)
# minibt 的 broker 按序号命名(symbol0/symbol1...), 真实代码要用数据源 attrs 回填
_PLACEHOLDER_SYMBOL_RE = re.compile(r"^symbol\d*$", re.IGNORECASE)
# 权益口径提示:minibt 原生 total_profit = 现金 + 持仓成本, 不逐日盯市
_MTM_WARNING = (
    "权益曲线已按成交流水+收盘价逐日盯市重建"
    "(minibt 原生记账为持仓成本口径, 回撤/夏普偏乐观)"
)


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


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _order_side_value(order: Any) -> int | None:
    """minibt OrderSide: Buy=0 / Sell=1（枚举或裸 int 都兼容）。"""
    side = getattr(order, "side", None)
    value = getattr(side, "value", side)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _completed_orders(broker: Any) -> list[Any]:
    getter = getattr(broker, "get_completed_orders", None)
    if callable(getter):
        try:
            return list(getter() or [])
        except Exception:
            return []
    return list(getattr(broker, "_completed_orders", None) or [])


def _symbol_hint_from_df(source_df: Any) -> str:
    """``load_daily`` 会把来源代码写进 ``df.attrs['symbol']``(minibt 自身不带)。"""
    attrs = getattr(source_df, "attrs", None)
    if not isinstance(attrs, dict):
        return ""
    return str(attrs.get("symbol") or "")


def _extract_trades(bt: Any, symbol_hint: str = "") -> list[dict[str, Any]]:
    """从各 broker 的已完成订单提取逐笔成交(对齐 StrategyLabTradeRecord)。

    只取真正成交的订单(``executed_size`` 非空)。minibt 的 Order 只有买卖方向,
    没有开/平标记与逐笔盈亏,故 ``pnl`` 留 None —— 口径提示会写进 warnings。

    minibt broker 按序号命名(symbol0/symbol1...), 单标的脚本里用它当代码会显示成
    ``symbol0``;有 ``symbol_hint``(来自数据源)时用它替换占位名。
    """
    trades: list[dict[str, Any]] = []
    for strategy in getattr(bt, "strategies", None) or []:
        account = getattr(strategy, "_account", None)
        for broker in getattr(account, "brokers", None) or []:
            broker_symbol = str(getattr(broker, "symbol", "") or "")
            symbol = (
                symbol_hint
                if symbol_hint and _PLACEHOLDER_SYMBOL_RE.match(broker_symbol)
                else broker_symbol
            )
            for order in _completed_orders(broker):
                size = _safe_float(getattr(order, "executed_size", None))
                side_value = _order_side_value(order)
                if not size or side_value is None:
                    continue
                trades.append(
                    {
                        "date": str(
                            getattr(order, "executed_datetime", None)
                            or getattr(order, "create_time", "")
                            or ""
                        ),
                        "symbol": symbol,
                        "direction": "BUY" if side_value == 0 else "SELL",
                        "price": _safe_float(getattr(order, "executed_price", None)) or 0.0,
                        "qty": size,
                        "reason": "",
                        "detail": {
                            "fee": _safe_float(getattr(order, "executed_commission", None)) or 0.0,
                            "value": _safe_float(getattr(order, "executed_value", None)) or 0.0,
                            "ref": getattr(order, "ref", None),
                        },
                        "pnl": None,
                    }
                )
    trades.sort(key=lambda item: item["date"])
    return trades


def _brokers(bt: Any) -> list[Any]:
    brokers: list[Any] = []
    for strategy in getattr(bt, "strategies", None) or []:
        account = getattr(strategy, "_account", None)
        brokers.extend(list(getattr(account, "brokers", None) or []))
    return brokers


def _initial_cash(bt: Any) -> float | None:
    for strategy in getattr(bt, "strategies", None) or []:
        account = getattr(strategy, "_account", None)
        for attr in ("_balance", "cash"):
            value = _safe_float(getattr(account, attr, None))
            if value:
                return value
    return None


def _reconstruct_mtm_equity(
    bt: Any, source_df: pd.DataFrame, res: pd.DataFrame
) -> tuple[list[float] | None, str]:
    """按成交流水 + 收盘价重建逐日盯市权益曲线。

    minibt 的 ``total_profit`` 是 ``balance = _available + margin``(现金 + 持仓
    成本),持仓期间不随价格变动 —— 用它算回撤/夏普会严重偏乐观(实测 0.001% vs
    真实 0.71%)。这里用已完成订单和收盘价重建 MTM 曲线,并与 minibt 逐 bar 交叉
    校验(持仓方向/数量、空仓 bar 的现金);校验不过(多标的/数据缺口)就返回
    None,由调用方回退原口径并给出警告。

    Returns:
        (权益序列, 跳过原因)。成功时原因串为空。
    """
    brokers = _brokers(bt)
    if len(brokers) != 1:
        return None, f"标的数={len(brokers)}(仅支持单标的)"
    if "datetime" not in source_df.columns or "close" not in source_df.columns:
        return None, "缺少 datetime/close 列"
    if "total_profit" not in res.columns or "positions" not in res.columns:
        return None, "minibt 结果缺少 total_profit/positions 列"

    dates = pd.to_datetime(source_df["datetime"]).reset_index(drop=True)
    closes = source_df["close"].astype(float).reset_index(drop=True)
    balance = res["total_profit"].astype(float).reset_index(drop=True)
    positions = res["positions"].astype(float).reset_index(drop=True)
    sizes = (
        res["sizes"].astype(float).reset_index(drop=True)
        if "sizes" in res.columns
        else None
    )
    n = min(len(dates), len(closes), len(balance), len(positions))
    if n < 2:
        return None, "样本过短"

    cash = _initial_cash(bt)
    if cash is None:
        return None, "取不到初始资金"

    orders: list[tuple[pd.Timestamp, float, int, float, float]] = []
    for order in _completed_orders(brokers[0]):
        size = _safe_float(getattr(order, "executed_size", None))
        side = _order_side_value(order)
        price = _safe_float(getattr(order, "executed_price", None))
        if not size or side is None or price is None:
            continue
        when = getattr(order, "executed_datetime", None) or getattr(
            order, "create_time", None
        )
        try:
            when_ts = pd.Timestamp(when)
        except (TypeError, ValueError):
            continue
        fee = _safe_float(getattr(order, "executed_commission", None)) or 0.0
        orders.append((when_ts, float(size), side, float(price), fee))
    orders.sort(key=lambda item: item[0])

    equity: list[float] = []
    rebuilt_positions: list[float] = []
    position = 0.0
    idx = 0
    for i in range(n):
        bar_date = dates.iloc[i]
        while idx < len(orders) and orders[idx][0] <= bar_date:
            _, size, side, price, fee = orders[idx]
            if side == 0:  # Buy
                cash -= size * price + fee
                position += size
            else:  # Sell
                cash += size * price - fee
                position -= size
            idx += 1
        rebuilt_positions.append(position)
        equity.append(cash + position * float(closes.iloc[i]))

    # 校验一(逐 bar):重建持仓方向/数量必须与 minibt 记账一致
    if sizes is not None:
        for i in range(n):
            if abs(rebuilt_positions[i] - float(sizes.iloc[i])) > 1e-6:
                return None, f"第 {i} 根 bar 持仓数量与 minibt 记账不一致"
            expected_dir = 0.0 if sizes.iloc[i] == 0 else float(positions.iloc[i])
            actual_dir = 0.0 if rebuilt_positions[i] == 0 else (
                1.0 if rebuilt_positions[i] > 0 else -1.0
            )
            if actual_dir != expected_dir:
                return None, f"第 {i} 根 bar 持仓方向与 minibt 记账不一致"

    # 校验二(更强):空仓 bar 上"重建权益"必须等于 minibt 记账值(两者都=现金)
    flat_bars = [i for i in range(n) if positions.iloc[i] == 0]
    if not flat_bars and sizes is None:
        return None, "全程持仓且无 sizes 列,无法校验"
    for i in flat_bars:
        expected = float(balance.iloc[i])
        if abs(equity[i] - expected) > 1e-6 * max(1.0, abs(expected)):
            return None, f"第 {i} 根空仓 bar 与 minibt 记账不一致"
    return equity, ""


def _apply_mtm_metrics(
    metrics: dict[str, float | None], mtm_equity: list[float]
) -> dict[str, float | None]:
    """用 MTM 权益序列重算收益/回撤/夏普/胜率(其余指标保持 minibt 口径)。"""
    series = pd.Series(mtm_equity, dtype=float)
    returns = series.pct_change().dropna()
    out = dict(metrics)
    out["cum_return"] = _safe_float(series.iloc[-1] / series.iloc[0] - 1.0)
    peak = series.cummax()
    out["max_drawdown"] = _safe_float(abs(((series - peak) / peak).min()))
    std = _safe_float(returns.std())
    if std:
        out["sharpe"] = _safe_float(returns.mean() / std * np.sqrt(252))
    nonzero = returns[returns != 0]
    if len(nonzero):
        out["win_rate"] = _safe_float((nonzero > 0).mean())
    return out


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
    *,
    mtm_equity: list[float] | None = None,
    mtm_skip_reason: str = "",
) -> dict[str, Any]:
    dates = source_df["datetime"].tolist() if "datetime" in source_df.columns else []
    total_profit = res["total_profit"].astype(float).tolist()
    n = min(len(dates), len(total_profit))
    series = (
        mtm_equity[:n]
        if mtm_equity is not None and len(mtm_equity) >= n
        else total_profit[:n]
    )
    equity = [
        {
            "date": pd.Timestamp(dates[i]).isoformat(),
            "value": round(float(series[i]), 4),
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

    trades: list[dict[str, Any]] = []
    warnings = list(_CALIBRATION_WARNINGS)
    if mtm_equity is not None:
        warnings.append(_MTM_WARNING)
    else:
        warnings.append(
            f"未能重建逐日盯市权益({mtm_skip_reason or '原因未知'}),"
            "回撤/夏普按 minibt 原生口径, 偏乐观"
        )
    if _truthy(os.getenv(_INCLUDE_TRADES_ENV)):
        trades = _extract_trades(bt, _symbol_hint_from_df(source_df))
        if trades:
            warnings.append(_TRADE_CALIBRATION_WARNING)

    return {
        "run_id": os.getenv("AI_IDE_BACKTEST_RUN_ID") or str(uuid.uuid4()),
        "status": "success",
        "metrics": metrics_payload,
        "equity": equity,
        "trades": trades,
        "positions": [],
        "overlays": {},
        "logs": [],
        "warnings": warnings,
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
            # 权益口径: mtm=逐日盯市重建 / minibt_balance=引擎原生持仓成本口径
            "equity_caliber": "mtm" if mtm_equity is not None else "minibt_balance",
        },
    }


def _apply_initial_capital(bt: Any, initial_capital: float | None) -> float | None:
    """把请求里的初始资金写进各策略 config.value(minibt 默认 1,000,000)。

    账户在 ``bt.run()`` 里按 ``config.value`` 创建,所以必须在 run 之前设置。
    策略脚本自己写的 ``self.config.value`` 会被这里的显式值覆盖(回测中心的
    "初始资金"字段是 UI 契约)。
    """
    capital = _safe_float(initial_capital)
    if capital is None:
        capital = _safe_float(os.getenv(_INITIAL_CAPITAL_ENV))
    if not capital or capital <= 0:
        return None
    for strategy in getattr(bt, "strategies", None) or []:
        config = getattr(strategy, "config", None)
        if config is not None and hasattr(config, "value"):
            config.value = float(capital)
    return float(capital)


def run_and_report(
    bt: Any,
    source_df: pd.DataFrame,
    *,
    result_dir: str | None = None,
    initial_capital: float | None = None,
) -> dict[str, Any]:
    """执行回测并输出结果。策略脚本尾部调用一次即可。

    Args:
        bt: 已 addstrategy 的 minibt Bt 实例(内部执行 run(isplot=False, isreport=False))
        source_df: 喂给 get_kline 的同一份 DataFrame(用于权益曲线日期对齐)
        result_dir: result.json 输出目录;默认环境变量 QM_MINIBT_RESULT_DIR(/app/result)
        initial_capital: 初始资金;缺省读环境变量 QM_MINIBT_INITIAL_CAPITAL,
            两者都没有则用 minibt 默认(1,000,000)

    Returns:
        组装好的 StrategyLabRunResult 形状 dict(写盘失败不影响返回与 [RESULT] 输出)。
    """
    started_at = time.time()
    capital = _apply_initial_capital(bt, initial_capital)
    if capital:
        print(f"[INFO] 初始资金: {capital:,.2f}")
    bt.run(isplot=False, isreport=False)
    elapsed = time.time() - started_at

    results = bt.get_results()
    res = results[0][0] if results and results[0] else pd.DataFrame()
    if res.empty:
        print("[ERROR] minibt 回测无结果(get_results 为空)")
        return {}

    metrics = _extract_metrics(bt, res)
    mtm_equity, mtm_skip_reason = _reconstruct_mtm_equity(bt, source_df, res)
    if mtm_equity is not None:
        metrics = _apply_mtm_metrics(metrics, mtm_equity)
    else:
        print(f"[WARNING] 未能重建逐日盯市权益: {mtm_skip_reason}")
    _print_result_lines(metrics, elapsed)

    payload = _build_run_result(
        bt,
        source_df,
        res,
        metrics,
        elapsed,
        started_at,
        mtm_equity=mtm_equity,
        mtm_skip_reason=mtm_skip_reason,
    )
    out_dir = result_dir or os.getenv(_RESULT_DIR_ENV, _DEFAULT_RESULT_DIR)
    try:
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "result.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, allow_nan=False)
    except Exception as exc:  # 写盘失败不影响已流式输出的指标
        print(f"[WARNING] result.json 写盘失败: {exc}")
    return payload
