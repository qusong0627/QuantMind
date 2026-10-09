"""T-FB-19 基准序列——真实指数日收益读数（QuantDB 系 parquet 的 index_daily）。

设计契约：
- 请求基准 = ``MarketProfile.benchmark``（csi300 / hsi / spx / equal_weight）；
  只有 ``BENCHMARK_SOURCES`` 登记的市场可能拿到真指数；其余（crypto / futures）
  声明即等权兜底；
- 读数失败（目录缺失 / 标的缺行 / 日历覆盖不足 / 任何数据面异常）一律返回
  ``None``——调用方回落等权并在载荷 ``bench`` 里如实标注，**绝不**用近似
  序列冒充指数；
- 输出为「与调用方日期轴对齐」的**次日收益** Series——与评估侧标签
  ``_forward_return`` 同口径：date t 记 ``close(t+1)/close(t) − 1``
  （逐期可比，错一天整条超额曲线就错位）；轴末日若索引已无次日收盘则为
  NaN，属定义而非缺失。

IO 纪律：parquet 读取是同步重活（可达秒级），engine 侧调用一律走
``asyncio.to_thread``，不阻塞事件循环（防看门狗强杀）。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import timedelta

import pandas as pd

logger = logging.getLogger(__name__)

#: 请求基准 id → 指数代码（各 hub 的 index_daily 标的命名）。
#: 与 ``profiles.MarketProfile.benchmark``、report 的展示标签三处同词表。
BENCHMARK_SOURCES: dict[str, str] = {
    "csi300": "000300.SH",
    "hsi": "HSI.HK",
    "spx": "SPX.US",
}

#: 对齐后最低覆盖（有效收益日 / 轴长——前向口径下每一天都需要一个次日收益）；
#: 低于即判不可用 → 回落等权。宁可等权兜底，不给断续的指数序列当超额基准。
BENCH_MIN_COVERAGE = 0.90


def _load_index_close(bench: str, start, end) -> pd.DataFrame:
    """按基准 id 取指数日线（惰性导入 hub，避免模块级拉重依赖）。"""
    symbol = BENCHMARK_SOURCES.get(bench)
    if not symbol:
        return pd.DataFrame()
    if bench == "csi300":
        from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

        hub = QuantDBDataHub()
    elif bench == "hsi":
        from backend.services.engine.data_platform.quanthk_hub import QuantHKDataHub

        hub = QuantHKDataHub.get_instance()
    else:  # spx
        from backend.services.engine.data_platform.quantus_hub import QuantUSDataHub

        hub = QuantUSDataHub.get_instance()
    return hub.fetch_index_kline(symbol, start, end)


def load_benchmark_returns(bench: str, dates: Sequence) -> pd.Series | None:
    """指数**次日收益**对齐到 ``dates``；不可用返回 ``None``（调用方回落等权）。

    只认 ``BENCHMARK_SOURCES`` 登记的 id——``equal_weight`` 与任何未登记 id
    直接 None，不触数据面（等权兜底由调用方在载荷层完成）。
    """
    if bench not in BENCHMARK_SOURCES:
        return None
    axis = pd.to_datetime(pd.Index(dates))
    if len(axis) < 2:
        return None
    # 轴外多取 10 个自然日两端：末日方向要拿到「轴末日的次日收盘」才能算
    # 最后一天的次日收益；首端纯保险（前向口径其实不需要轴前收盘）。
    start = axis.min().date() - timedelta(days=10)
    end = axis.max().date() + timedelta(days=10)
    try:
        df = _load_index_close(bench, start, end)
    except Exception as exc:  # 数据面任何异常都只配回落等权，绝不外抛
        logger.warning("[factor-backtest] 基准 %s 读取失败: %s", bench, exc)
        return None
    if (
        df is None
        or df.empty
        or "close" not in df.columns
        or "trade_date" not in df.columns
    ):
        return None
    close = pd.Series(
        pd.to_numeric(df["close"], errors="coerce").to_numpy(),
        index=pd.to_datetime(df["trade_date"]),
    )
    close = close.dropna()
    close = close[~close.index.duplicated(keep="last")].sort_index()
    if len(close) < 2:
        return None
    # 前向口径（与评估侧 _forward_return 一致）：date t ← close(t+1)/close(t)−1
    ret = close.pct_change().shift(-1).reindex(axis)
    coverage = float(ret.notna().sum()) / len(axis)
    if coverage < BENCH_MIN_COVERAGE:
        logger.warning(
            "[factor-backtest] 基准 %s 对齐覆盖 %.1f%% < %.0f%%，回落等权",
            bench,
            coverage * 100.0,
            BENCH_MIN_COVERAGE * 100.0,
        )
        return None
    return ret
