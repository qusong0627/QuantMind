"""可交易性掩码：理想口径 vs 可交易口径的**唯一**判定实现。

## 口径（报告页逐字展示同一段文字，改这里必须改前端）

建仓在 T 日，故看 **T 日盘面**：

| 盘面 | 含义 | 剔除哪条腿 |
|---|---|---|
| 涨停（``close ≥ limit_up``） | 买不进 | **多头腿** |
| 跌停（``close ≤ limit_down``） | 卖不出 | **空头腿** |
| 停牌（``volume ≤ 0`` 或当日**无该股行**） | 不可交易 | 两腿 |

「剔除」= 当日该股**不进组**、收益记为不参与，**不是**按收盘价照常成交。
两轨差额（理想 − 可交易）读作「**理想口径高估了多少**」，**不是**「策略会亏这么多」。

两种停牌形态都认：``volume ≤ 0``（有行无量，实测 2026-09-18 有 13 只）与**缺行**
（该股当日根本没落盘）—— 后者在向量化引擎里表现为 ``price_wide.isna()``。
两者分别计数，因为它们的成因不同（前者是全天停牌，后者多是数据缺口）。

## 为什么不做 ST 过滤（2026-09-19 实测，不是偷懒）

1. ``instrument_detail`` 的 ST 名单是**单一快照**，``local_market_data._st_symbol_set``
   的 docstring 自述「历史回放沿用该快照」—— 用今天的 ST 名单过滤 2018 年的股票
   就是前视偏差。
2. 本以为 ``6_ml_datasets/features_daily.is_st`` 能逐日取用，实测**该列只存在于
   2026-09-16 起的 3 个分区**（抽样 25 个跨 2016–2026 的分区，只有最后 3 个有该列），
   即此路没有历史。
3. 更麻烦的是：快照口径下 ST 标的的历史涨跌停价会被 ``limit_pct`` 的 ``st_reduces``
   折成 ±5%，一条 6% 的普通阳线会被判成涨停 —— **判定本身**被污染，不只是过滤名单错。

⇒ 对当时被标为 ST 的标的**整票不入掩码**：既不把它们当阻挡（那会引入偏差），
也不假装它们可交易。占比在 meta 里报出，报告页注明。

## 与被复用实现的关系

涨跌停价、停牌判定**一律复用** ``services/simulation/services/local_market_data``
（``DailyBar`` / ``compute_limits``）—— 那是平台唯一权威实现，本模块不重算一条规则。
⚠️ 不是 ``services/trade/simulation/services/local_market_data.py``（并行旧树）。

## 掩码产物

单个 parquet（**稀疏**：只存被挡的行）``trade_mask.parquet``：
``dt(int32) / symbol(str) / blocked_long(bool) / blocked_short(bool)``。
全量约 2600 天 × 数百只 ≈ 数十万行、几 MB，构建器一次读入即可。
为而不做按日分区：报告构建器本来就要顺序遍历全部日期，单文件更省事也更好对齐。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

log = logging.getLogger("factor_report.tradability")

MASK_COLUMNS = ("dt", "symbol", "blocked_long", "blocked_short")
N_QUANTILES = 10
"""分组数与构建器一致（G1 最小 … G10 最大）。"""

_SPARSE_DTYPE = {"blocked_long": bool, "blocked_short": bool}


# ─────────────────────────── 由盘面生成掩码 ───────────────────────────

def blocked_rows_for_date(bars: dict[str, Any], dt_int: int) -> list[tuple[int, str, bool, bool]]:
    """当日全市场 ``DailyBar`` → 被挡行（**稀疏**：可交易的股票不产生行）。

    ST 标的整票跳过（见模块 docstring 第 2 节）；``bars`` 为空返回空列表
    （调用方按「无数据」处理，不在这里抛 —— 某个市场/某天没数据是常态）。
    """
    rows: list[tuple[int, str, bool, bool]] = []
    for sym, bar in bars.items():
        if getattr(bar, "is_st", False):
            continue
        suspended = bool(getattr(bar, "suspended", False))
        close = float(getattr(bar, "close", 0.0) or 0.0)
        limit_up = float(getattr(bar, "limit_up", np.inf))
        limit_down = float(getattr(bar, "limit_down", 0.0))
        # 无昨收（新股首日）时 limit_up = inf / limit_down = 0 → 两条都不会命中，
        # 这是厂里既有约定（compute_limits 的返回值），此处不额外判断。
        at_up = np.isfinite(limit_up) and close >= limit_up - 1e-9 and close > 0
        at_down = limit_down > 0 and close <= limit_down + 1e-9
        blk_long = suspended or at_up
        blk_short = suspended or at_down
        if blk_long or blk_short:
            rows.append((dt_int, sym, blk_long, blk_short))
    return rows


def build_mask(
    dates: Sequence[int],
    *,
    market: str = "CN",
    market_data: Any = None,
) -> pd.DataFrame:
    """逐日扫盘面 → 稀疏掩码表（``MASK_COLUMNS``）。

    Args:
        dates: ``YYYYMMDD`` 整数日期（升序）。
        market: 传给 ``get_local_market_data``。
        market_data: 可注入的 ``LocalMarketData``（单测用假实现），None 则取进程单例。
    """
    from datetime import date as _date

    if market_data is None:
        from backend.services.simulation.services.local_market_data import get_local_market_data

        market_data = get_local_market_data(market)

    rows: list[tuple[int, str, bool, bool]] = []
    empty_days = 0
    for dt_int in dates:
        d = _date(dt_int // 10000, (dt_int // 100) % 100, dt_int % 100)
        bars = market_data.load_date(d)
        if not bars:
            empty_days += 1
            log.warning("可交易掩码：%s 无行情数据（计为整日不可判定）", dt_int)
            continue
        rows.extend(blocked_rows_for_date(bars, dt_int))
    if empty_days:
        log.warning("可交易掩码：%d/%d 天无行情数据", empty_days, len(dates))
    return pd.DataFrame(rows, columns=list(MASK_COLUMNS)).astype(
        {"dt": "int32", "symbol": "string", **_SPARSE_DTYPE}, errors="ignore"
    )


def write_mask(df: pd.DataFrame, path: str | Path) -> dict[str, Any]:
    """原子写（临时文件 + ``os.replace``），中途失败不留半截产物。"""
    import os

    import pyarrow as pa
    import pyarrow.parquet as pq

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".parquet.tmp")
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), tmp)
    os.replace(tmp, p)
    n_long = int(df["blocked_long"].sum()) if len(df) else 0
    n_short = int(df["blocked_short"].sum()) if len(df) else 0
    return {"path": str(p), "rows": len(df), "blocked_long": n_long, "blocked_short": n_short}


def load_mask(path: str | Path) -> dict[int, tuple[frozenset[str], frozenset[str]]]:
    """读掩码 → ``{dt_int: (多头被挡集合, 空头被挡集合)}``。

    读不到返回**空 dict**（调用方据此判定「掩码缺失」并降级），不抛。
    """
    import pyarrow.parquet as pq

    p = Path(path)
    if not p.exists():
        return {}
    try:
        df = pq.read_table(p, columns=list(MASK_COLUMNS)).to_pandas()
    except Exception as e:  # noqa: BLE001 — 掩码坏了不该拖垮报告构建
        log.warning("可交易掩码读取失败（%s）：%s", p, e)
        return {}
    out: dict[int, tuple[frozenset[str], frozenset[str]]] = {}
    for dt_int, grp in df.groupby("dt", sort=False):
        syms = grp["symbol"].astype(str).to_numpy()
        out[int(dt_int)] = (
            frozenset(syms[grp["blocked_long"].to_numpy(dtype=bool)]),
            frozenset(syms[grp["blocked_short"].to_numpy(dtype=bool)]),
        )
    return out


def align_blocked(
    symbols: Sequence[str],
    entry: tuple[frozenset[str], frozenset[str]] | None,
) -> tuple[np.ndarray, np.ndarray] | None:
    """把掩码集合对齐到**当日截面行序** → 两个布尔数组。

    必须按 ``symbols`` 的行序逐位对齐：错位会静默把 A 股票的涨跌停状态扣到 B 股票头上，
    且结果「看起来也像有拦截」。掩码为 None/空 → 返回 None（调用方跳过可交易轨）。
    """
    if not entry:
        return None
    long_set, short_set = entry
    if not long_set and not short_set:
        return None
    syms = np.asarray([str(s) for s in symbols], dtype=object)
    blk_long = np.fromiter((s in long_set for s in syms), dtype=bool, count=len(syms))
    blk_short = np.fromiter((s in short_set for s in syms), dtype=bool, count=len(syms))
    return blk_long, blk_short


# ─────────────────────────── 可交易轨的组收益 ───────────────────────────

def exclude_blocked(
    idx: np.ndarray,
    yv: np.ndarray,
    blk_sel: np.ndarray,
    cnt_all: np.ndarray,
    sum_all: np.ndarray,
    n_quantiles: int = N_QUANTILES,
) -> tuple[np.ndarray, np.ndarray]:
    """**可交易轨的核心原语**：从理想轨已算好的 (计数, 求和) 里**减去**被挡成员的贡献。

    为什么是这个形态：一个 1336 因子 × 4200 只的替身实现（自己重算两遍 bincount）
    实测 **239 ms/天 → 全量 10.4 分钟**；而这里只对**被挡的那几百行**做一次 bincount，
    其余全部复用调用方已经算过的量。两条轨因此也**必然共享同一套分组** ——
    自己重算分位会让「双轨差额」里混进分组差异，读的人无从分辨。

    Args:
        idx: ``(m,)`` 组编号 0..nq-1（仅**有效成员**，与 ``yv``/``blk_sel`` 同行序）。
        yv: ``(m,)`` 前瞻收益。
        blk_sel: ``(m,)`` 布尔 —— 该侧被挡（多头腿看涨停/停牌，空头腿看跌停/停牌）。
        cnt_all / sum_all: ``(nq,)`` 理想轨的组计数与组收益和。
        n_quantiles: 分组数。

    Returns:
        ``(q_trade, cnt_blocked)``，均 ``(nq,)``；某组全员被挡 → 该组 NaN
        （不是 0 —— 0 会被读成「当天没赔没赚」，而真相是「这天没法建仓」）。
    """
    nq = int(n_quantiles)
    idx = np.asarray(idx)
    yv = np.asarray(yv, dtype=np.float64)
    blk_sel = np.asarray(blk_sel, dtype=bool)
    if not (idx.shape == yv.shape == blk_sel.shape):
        raise ValueError(f"idx/yv/blk_sel 形状必须一致，收到 {idx.shape}/{yv.shape}/{blk_sel.shape}")
    if len(idx) == 0:
        # 零项不是「通过」：无成员参与时组收益无定义
        return np.full(nq, np.nan), np.zeros(nq, dtype=np.float64)

    n_blk = np.zeros(nq, dtype=np.float64)
    cnt_blk = np.zeros(nq, dtype=np.float64)
    sum_blk = np.zeros(nq, dtype=np.float64)
    if blk_sel.any():                       # 稀疏：绝大多数日子只有几百只被挡
        bi = idx[blk_sel]
        cnt_blk = np.bincount(bi, minlength=nq)[:nq].astype(np.float64)
        sum_blk = np.bincount(bi, weights=yv[blk_sel], minlength=nq)[:nq]
        n_blk = cnt_blk
    n_keep = np.asarray(cnt_all, dtype=np.float64)[:nq] - cnt_blk
    with np.errstate(invalid="ignore", divide="ignore"):
        q = np.where(n_keep > 0, (np.asarray(sum_all, dtype=np.float64)[:nq] - sum_blk) / np.where(n_keep > 0, n_keep, 1.0), np.nan)
    return q, n_blk


def blocked_summary(
    group: np.ndarray,
    y: np.ndarray,
    blk: np.ndarray,
    n_quantiles: int = N_QUANTILES,
) -> tuple[np.ndarray, np.ndarray]:
    """逐因子报出「当日参与截面的只数」与「其中被挡只数」→ ``(n_valid, n_blocked)``，均 ``(K,)``。

    ⚠️ 分母必须是**该因子当日有效样本数**，不是全市场、也不是「行数 × 因子数」——
    后者是个量纲错误的数（本函数第一版就是把二维掩码整个 sum，20 只 × 2 因子得出 40）。
    比例由调用方按 ``n_blocked / n_valid`` 算，除零处自己给 NaN。
    """
    g = np.asarray(group)
    if g.ndim != 2:
        raise ValueError(f"group 必须是二维矩阵，收到 shape={g.shape}")
    yv = np.asarray(y, dtype=np.float64)
    if yv.shape[0] != g.shape[0]:
        raise ValueError("group / y 的行数必须一致（同一截面）")
    ok = np.isfinite(yv)[:, None] & (g >= 0) & (g < n_quantiles)
    n_valid = ok.sum(axis=0).astype(np.float64)
    b = np.asarray(blk, dtype=bool)
    n_blocked = (ok & b[:, None]).sum(axis=0).astype(np.float64)
    return n_valid, n_blocked


# 容量估算**不在这里**再写一份：``metrics.capacity_estimate`` 已是唯一实现（P0 落地），
# 本模块只负责掩码与分组剔除。两处实现迟早会在参与率默认值上漂移。
