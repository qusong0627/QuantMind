"""基准取数与超额块的**叶子依赖**（§6 的下半部分）。

这里的三个函数（基准日收益 / 超额统计量 / 分年度）是纯叶子：只被 :mod:`blocks` 的
``excess_block`` 与 ``build_blocks`` 调用，自身不再往外派生。

⚠️ **``excess_block`` 本身留在 :mod:`blocks`，不要搬进来** —— 它是测试替换
``bench_daily_returns`` 的唯一接缝。若 ``excess_block`` 与 ``bench_daily_returns``
分居两个模块，``monkeypatch.setattr(blocks, "bench_daily_returns", ...)`` 只会改到
``build_blocks`` 那一路的查找，而 ``excess_block`` 内部仍解析到真函数 —— 一半
被替换、一半没被替换，测试随即变成「看着通过、其实没测到」的那种。二者必须
共享同一个模块命名空间。

本文件是全层唯一**有 IO** 的地方（QuantDB ``index_daily``），因此带进程内 TTL 缓存。
"""

from __future__ import annotations

import logging
import math
from typing import Any

import numpy as np

from . import metrics as M
from .blocks_common import _f

log = logging.getLogger(__name__)

_BENCH_CACHE: dict[str, tuple[float, Any]] = {}
_BENCH_TTL = 600.0


def bench_daily_returns(symbol: str, dates: list[str]) -> np.ndarray | None:
    """基准日收益，按报告日期对齐（缺的日期留 NaN，**不前向填充**）。

    ⚠️ 与 :mod:`blocks` 的 ``excess_block`` 同属一个替换接缝（见模块 docstring）：
    测试用 ``monkeypatch.setattr(blocks, "bench_daily_returns", ...)`` 时才拦得住。
    """
    import time

    import pandas as pd

    hit = _BENCH_CACHE.get(symbol)
    if hit and time.time() - hit[0] < _BENCH_TTL:
        series = hit[1]
    else:
        try:
            from backend.shared.benchmark import load_index_frame

            frame = load_index_frame(columns=("close",), symbols=[symbol])
            frame = frame.sort_values("time")
            series = pd.Series(
                frame["close"].astype(float).to_numpy(),
                index=pd.to_datetime(frame["time"]).dt.strftime("%Y-%m-%d"),
            )
        except Exception as e:  # noqa: BLE001 — 单个指数取不到只影响它自己
            log.warning("基准取数失败 %s：%s", symbol, e)
            _BENCH_CACHE[symbol] = (time.time(), None)
            return None
        _BENCH_CACHE[symbol] = (time.time(), series)
    if series is None:
        return None
    ret = series.pct_change()
    return ret.reindex(dates).to_numpy(dtype=np.float64)


def _excess_stats(long_daily: np.ndarray, bench: np.ndarray, exc: np.ndarray) -> dict[str, Any]:
    """跟踪误差 / 信息比率 / Beta / 相关性 / 年化超额。样本不足时全部为 None。"""
    ok = np.isfinite(long_daily) & np.isfinite(bench)
    te = ir = beta = corr = None
    if ok.sum() >= M.MIN_SAMPLES:
        a, b = long_daily[ok], bench[ok]
        sd = float(np.std(exc[np.isfinite(exc)], ddof=1)) if np.isfinite(exc).sum() > 1 else 0.0
        mu = float(np.nanmean(exc[np.isfinite(exc)]))
        te = sd * math.sqrt(M.TRADING_DAYS)
        ir = (mu / sd * math.sqrt(M.TRADING_DAYS)) if sd > 0 else None
        if float(np.std(b, ddof=1)) > 0:
            beta = float(np.cov(a, b, ddof=1)[0, 1] / np.var(b, ddof=1))
        corr = M.pearson(a, b)
    return {
        "tracking_error": _f(te),
        "information_ratio": _f(ir),
        "beta": _f(beta),
        "corr": _f(corr),
        "annual_excess": _f(np.nanmean(exc) * M.TRADING_DAYS),
        "n_days": int(ok.sum()),
    }


def _annual(dates: list[str], long_daily: np.ndarray, ls_daily: np.ndarray,
            bench_daily: np.ndarray | None) -> list[dict[str, Any]]:
    """分年度：多头 / 基准 / 超额 / 多空。两张表按年合并（``annual_breakdown`` 一条腿一次）。"""
    ls_rows = {r["year"]: r["ret"] for r in M.annual_breakdown(dates, ls_daily)}
    return [
        {
            "year": r["year"],
            "n_days": r["n_days"],
            "long_ret": r["ret"],
            "bench_ret": r["bench_ret"],
            "excess": r["excess"],
            "ls_ret": ls_rows.get(r["year"]),
        }
        for r in M.annual_breakdown(dates, long_daily, bench_daily)
    ]
