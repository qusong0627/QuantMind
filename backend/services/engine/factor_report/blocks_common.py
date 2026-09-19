"""读时派生层的**共享底座**：口径常量、跨块小工具、降级约定。

本文件**没有业务逻辑、没有 IO** —— 只放「换个块也必须一模一样」的东西。
四个模块的分工：

===============  ==========================================================
``blocks_common``  常量 + 小工具（本文件）
``blocks_ic``      显著性块 + IC 块（纯派生，无 IO）
``blocks_bench``   基准取数 + 超额块的叶子依赖（**有 IO**：QuantDB index_daily）
``blocks``         概览/分组/成本/超额/风格/稳健性 + 组装（对外唯一入口）
===============  ==========================================================

## 分工表（动手前先看这张表决定新指标写哪边）

==============================  ==========================================
只进构建期（要当日截面）        读时派生（本层，只要逐日序列）
==============================  ==========================================
半 IC / 中性化 IC / 分域 IC     累计 IC、多空收益·回撤·分布、Fitness/Margin
风格相关 / clip_frac / n_valid   NW t、Bootstrap、BHY、DSR、成本敏感性
逐组换手 gt1..gt10              持有期扫描、分年度、月度矩阵、稳健性分段
可交易轨 q*_trad_long/short     滚动 IR、风格归因回归、容量估算、多基准超额
==============================  ==========================================

## 三个必须知道的口径陷阱

1. **``ls_{h}`` 列是 Q10−Q1 极值价差，不是可配的 G3/G9**。构建期为省体积只存了
   极值组价差；可配多空对必须从 ``q1..q10`` 现算（:func:`blocks.group_block`）。故
   ``ls_{h}`` 只被持有期扫描消费，且该块**显式标注**用的是极值组口径。
2. **``turnover`` 列是全截面「换组比例」**（任意组挪到任意组都算一次），**不是组合
   换手**。7 指标环与成本模型要的是 G3/G9 两条腿的换手，来自 ``gt1..gt10``。
   两者量级不同，混用会系统性低估成本 —— 用 :func:`blocks.legs_turnover` 区分。
3. **累计型序列走全窗口、噪声型序列服从 ``lookback``**。日 IC / 日收益这类噪声序列
   给 2588 个点只会把 JSON 撑到几 MB 且图上看不出差别；累计 IC、净值、超额累计
   必须全窗口才有意义。两类在各块的 ``*_full`` / 无后缀命名上区分。

## 降级原则

任一数据源缺失（风格产物未建、某 horizon 缺列、基准取不到、掩码没有）→ 该块返回
``{"available": False, "reason": ...}``，**绝不写 0 或用空数组冒充**。前端据此显式提示。
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from . import metrics as M

N_QUANTILES = 10
BENCHMARKS: tuple[tuple[str, str], ...] = (
    ("000300.SH", "沪深300"),
    ("000905.SH", "中证500"),
    ("000852.SH", "中证1000"),
)
"""多基准对照表。取数走 ``shared/benchmark.load_index_frame``（任意指数可传）。"""

ROLL_SHORT = 21
ROLL_LONG = 252  # 滚动 IR / 滚动 IC 的长窗口（与年化交易日一致）
IC_AUTOCORR_LAGS = 20
INDEPENDENCE_TOP = 20
CAPACITY_NOTE_SCOPE = "全市场当日成交额中位数（非持仓级）"


# ─────────────────────────── 小工具 ───────────────────────────


def _f(v: Any) -> float | None:
    """非有限值一律转 None —— NaN/Inf 会让 FastAPI 序列化直接 500。"""
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _seq(a: Any) -> list[float | None]:
    arr = np.asarray(a, dtype=np.float64).ravel()
    return [_f(v) for v in arr]


def iso_dates(vals: Any) -> list[str]:
    """``20260918`` → ``2026-09-18``。

    ⚠️ 不是美化：``annual_breakdown`` / ``monthly_matrix`` 按 ``s[5:7]`` 取月，
    紧凑格式会解析出「91 月」这种不存在的月份而**静默产出垃圾矩阵**。
    """
    out: list[str] = []
    for v in vals:
        s = str(v).strip()
        out.append(f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 and s.isdigit() else s)
    return out


def _hist(values: Any, bins: int = 40) -> dict[str, Any]:
    """直方图（供前端画分布图）。空输入返回空箱而不是 0 计数。"""
    v = np.asarray(values, dtype=np.float64).ravel()
    v = v[np.isfinite(v)]
    if v.size == 0:
        return {"bin_edges": [], "counts": [], "n": 0}
    counts, edges = np.histogram(v, bins=max(1, int(bins)))
    return {
        "bin_edges": [float(x) for x in edges],
        "counts": [int(x) for x in counts],
        "n": int(v.size),
    }


def _mean_std(a: Any) -> tuple[float | None, float | None]:
    v = np.asarray(a, dtype=np.float64).ravel()
    v = v[np.isfinite(v)]
    if v.size == 0:
        return None, None
    return _f(v.mean()), _f(v.std(ddof=1)) if v.size > 1 else None


def _col(df: Any, name: str) -> np.ndarray | None:
    return df[name].to_numpy(dtype=np.float64) if name in df.columns else None


def _col_mean(df: Any, name: str) -> float | None:
    """整列的窗口均值（列不存在或全 NaN → None，不写 0）。"""
    a = _col(df, name)
    if a is None or not np.isfinite(a).any():
        return None
    return _f(np.nanmean(a))


def _missing_block(reason: str, **extra: Any) -> dict[str, Any]:
    return {"available": False, "reason": reason, **extra}


def _episodes(curve: np.ndarray, dates: list[str]) -> list[dict[str, Any]]:
    """回撤区间表：把 ``drawdown_episodes`` 的下标翻译成日期。

    区间端点按 ``dates``（累计型曲线的轴，通常是全窗口）取；下标越界时该端点为
    ``None`` 而不是抛错 —— 降级成「日期不知道」好过整张表消失。
    """
    out = []
    for e in M.drawdown_episodes(curve, top=5):
        i0, i1 = e.get("i0"), e.get("i1")
        out.append({
            **e,
            "start": dates[i0] if isinstance(i0, int) and 0 <= i0 < len(dates) else None,
            "end": dates[i1] if isinstance(i1, int) and 0 <= i1 < len(dates) else None,
        })
    return out
