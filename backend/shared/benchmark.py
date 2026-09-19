#!/usr/bin/env python3
"""基准指数（benchmark）口径唯一事实源。

平台里「指数」承担三个**互不相同**的角色，混用会静默出错，故在此写明台账：

======  ============  ================================  ==========================================
角色    符号          用途                              代表调用点
======  ============  ================================  ==========================================
基准    000300.SH     超额收益、相对强弱、因子中性化的  本模块 ``BENCHMARK_SYMBOL``
                      分母
择时    000001.SH     「大盘多/空」判断                  ``tdx_rolling_trade_service.INDEX_SYMBOL``
                      （上证 MA20 只卖不买）             ``selection.py`` 大盘状态
择时    000300.SH     regime 分段 / 回测健康度           ``backtest_health.DEFAULT_REGIME_INDEX``
                                                         ``market_regime.DEFAULT_REGIME_INDEX``
市场腿  000905.SH     因子库的 ``market_close``          ``factor_research/data.py``
======  ============  ================================  ==========================================

**只统一第一个（基准）角色。** 其余两条**不要**为了「统一」改掉：

- 择时口径本身就有两个阵营（回测/regime 用沪深300，实盘 TDX 用上证 MA20），
  这是**策略设计**不是基准，改了会动到实盘下单规则；
- 因子库的市场腿是中证500，属既有产物口径，改了要重跑因子库。

本模块只回答「算超额时拿哪条指数」，不回答「择时看哪条」。

2026-09-19 之前全平台有 5-6 处各自写字面量 ``000300.SH``，
唯一真正的分歧点是 ``alpha_library_factors.py::load_benchmark()`` 取 ``000001.SH``
（只被 ``compute_gtja`` 消费，且 191 条 GTJA 因子中仅 ``gtja_075`` / ``gtja_182``
两条用到基准，且只用「涨/跌」布尔、不涉幅度）。现已收回本模块。

取数解析顺序：``000300.SH`` → ``000001.SH``，**回退时打 warning，不静默**。

依赖约定：**常量路径零第三方依赖**（pandas/duckdb 延迟到取数函数内），
故纯注册表模块（如 ``risk_rule_types``）也能安全 import 本模块取常量。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from backend.shared.quantdb_paths import resolve_quantdb_subdir

if TYPE_CHECKING:  # 仅供类型标注；运行时不导入
    import pandas as pd

logger = logging.getLogger(__name__)

# ── 基准口径（本模块是唯一事实源）────────────────────────────────
BENCHMARK_SYMBOL = "000300.SH"
BENCHMARK_NAME = "沪深300"
#: 回退链：主基准缺失/不足时按序尝试（同样属于「基准」角色，非择时指数）
BENCHMARK_FALLBACKS: tuple[str, ...] = ("000001.SH",)
BENCHMARK_CHAIN: tuple[str, ...] = (BENCHMARK_SYMBOL,) + BENCHMARK_FALLBACKS

#: 判「该指数在本地 parquet 里覆盖充分」的行数下限（全量约 2600 行）
MIN_ROWS = 500

_PART_GLOB = "dt=*/data.parquet"
_DATE_COL = "time"
_SYMBOL_COL = "symbol"

_DF_COLS: tuple[str, ...] = ("open", "high", "low", "close", "volume", "amount")


def index_daily_dir() -> Path:
    """QuantDB 指数日线目录（不检查存在性，由调用方决定如何降级）。"""
    return resolve_quantdb_subdir("1_kline_data", "index_daily")


def _as_iso(value: object) -> str | None:
    """归一为 ``YYYY-MM-DD``；``None`` 原样透传。"""
    if value is None:
        return None
    import pandas as pd

    if isinstance(value, (date, pd.Timestamp)):
        return pd.Timestamp(value).strftime("%Y-%m-%d")
    return str(value)


def load_index_frame(
    columns: Sequence[str] = _DF_COLS,
    symbols: Sequence[str] | None = None,
    start: object = None,
    end: object = None,
) -> pd.DataFrame:
    """读 QuantDB ``index_daily``（后缀式符号，如 ``000300.SH``）。

    Args:
        columns: 需要的列（``time`` / ``symbol`` 自动带上）。
        symbols: 只要这些指数；``None`` 表示全部。
        start / end: 闭区间日期过滤，支持 ``date`` / ``datetime`` / ISO 字符串。

    Returns:
        列含 ``time``(datetime64) 与 ``symbol`` 的 DataFrame；无数据时为空表。

    Raises:
        FileNotFoundError: 指数日线目录下没有分区文件（数据未同步）。
    """
    import duckdb  # 延迟导入：与 l05_store / minibt_qdb 同约定
    import pandas as pd

    part_dir = index_daily_dir()
    files = sorted(part_dir.glob(_PART_GLOB))
    if not files:
        raise FileNotFoundError(
            f"指数日线数据缺失：{part_dir} 下没有 {_PART_GLOB}，"
            f"请先跑 QuantDB 同步（见 backend/scripts/quantdb_daily_sync.py）"
        )

    cols = ", ".join(dict.fromkeys([_DATE_COL, _SYMBOL_COL, *columns]))
    glob = str(part_dir / _PART_GLOB)
    con = duckdb.connect()
    try:
        df = con.execute(f"SELECT {cols} FROM read_parquet('{glob}')").fetchdf()
    finally:
        con.close()

    if df.empty:
        return df

    df[_DATE_COL] = pd.to_datetime(df[_DATE_COL])
    if symbols is not None:
        df = df[df[_SYMBOL_COL].isin(list(symbols))]

    lo, hi = _as_iso(start), _as_iso(end)
    if lo is not None:
        df = df[df[_DATE_COL] >= lo]
    if hi is not None:
        df = df[df[_DATE_COL] <= hi]

    return df.reset_index(drop=True)


def resolve_benchmark(
    columns: Sequence[str] = ("open", "close"),
    start: object = None,
    end: object = None,
    chain: Sequence[str] = BENCHMARK_CHAIN,
) -> tuple[str, pd.DataFrame]:
    """按 ``chain`` 顺序取第一个覆盖充分的基准指数。

    Returns:
        ``(实际使用的符号, 以 time 为索引的 DataFrame)`` —— **返回符号是刻意的**，
        调用方应记录/展示它，避免回退发生在暗处。

    Raises:
        ValueError: 链上所有指数都取不到或行数不足 :data:`MIN_ROWS`。
    """
    chain = tuple(chain)
    df = load_index_frame(columns=columns, symbols=chain, start=start, end=end)

    tried: list[str] = []
    for sym in chain:
        sub = df[df[_SYMBOL_COL] == sym].sort_values(_DATE_COL)
        if len(sub) > MIN_ROWS:
            if sym != chain[0]:
                logger.warning(
                    "基准口径回退：%s 仅有 %d 行（阈值 %d），改用 %s",
                    chain[0],
                    len(df[df[_SYMBOL_COL] == chain[0]]),
                    MIN_ROWS,
                    sym,
                )
            return sym, sub.set_index(_DATE_COL)
        tried.append(f"{sym}={len(sub)}行")

    raise ValueError(
        f"基准指数链 {list(chain)} 均不可用（阈值 >{MIN_ROWS} 行）：{', '.join(tried)}"
    )


def load_benchmark_closes(
    column: str = "close",
    start: object = None,
    end: object = None,
) -> tuple[str, pd.Series]:
    """基准指数单列序列；返回 ``(符号, Series)``。"""
    sym, df = resolve_benchmark(columns=(column,), start=start, end=end)
    return sym, df[column].astype(float)


def load_benchmark_ohlc(
    start: object = None,
    end: object = None,
) -> tuple[pd.Series, pd.Series]:
    """基准指数 ``(open, close)``，对齐到同一索引（未与个股日历对齐，由调用方 reindex）。"""
    sym, df = resolve_benchmark(columns=("open", "close"), start=start, end=end)
    logger.info(
        "基准指数 %s：%d 行，%s ~ %s",
        sym,
        len(df),
        df.index[0].date(),
        df.index[-1].date(),
    )
    return df["open"].astype(float), df["close"].astype(float)
