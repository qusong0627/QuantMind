"""统计派 · 数据面板与元数据装配（移植 P2 / Tier 2）。

两个来源，各司其职：

- **行情**（OHLCV+amount）走 qlib provider —— 与入库表达式的回测**同源**，
  避免复权口径（qfq/hfq/不复权）在「算 IC 的数据」和「回测的数据」之间漂移；
- **元数据**（行业 / 上市日 / ST / 流通股本）走 QuantDB ``instrument_detail``
  （经 ``quantdb_hub.fetch_stock_list``）。

面板约定：``MultiIndex (trade_date, symbol)``，日期升序；symbol 为 qlib 式
（``sh600000``）；列 = open/high/low/close/volume/amount（float64）。

**暖机纪律**：按 qlib 日历向前多取 ``warmup_bars`` 个交易日——保证挖掘区间
首日各候选窗口已满，与「表达式在 qlib 里带扩展历史求值」逐点一致（等价性
契约见 ``test_statistical_candidates.py``）。多取的暖机行由引擎在算完候选后裁掉。

符号口径：跨表比较一律先归一到**代码数字**（``symbol_digits``）——qlib 用小写
后缀、QuantDB 用大写后缀（``600000.SH``），直接字符串比较会静默对不上。

行情加载器（``loader``）可注入：测试传假加载器即可全离线跑。
"""

from __future__ import annotations

import os
from collections.abc import Callable

import numpy as np
import pandas as pd

from . import candidates as _candidates
from .tradability import map_unique, symbol_digits  # 单一实现：代码数字归一

#: 行情字段（qlib 表达式字段名）
PRICE_FIELDS = ("$open", "$high", "$low", "$close", "$volume", "$amount")
PANEL_COLUMNS = ("open", "high", "low", "close", "volume", "amount")

DEFAULT_QLIB_DIR = os.environ.get("QM_QLIB_DATA_DIR", "/data/qlib/cn_data")


def warmup_start(
    start: pd.Timestamp | str, calendar: pd.DatetimeIndex, warmup_bars: int
) -> pd.Timestamp:
    """挖掘起点向前回看 warmup_bars 个交易日（不足则退到日历首日）。"""
    if warmup_bars < 0:
        raise ValueError(f"warmup_bars 不得为负（收到 {warmup_bars!r}）")
    position = calendar.searchsorted(pd.Timestamp(start), side="left")
    return pd.Timestamp(calendar[max(0, position - warmup_bars)])


def _qlib_features(
    instruments: list[str], fields: list[str], start: str, end: str, qlib_dir: str
) -> pd.DataFrame:
    """qlib 取数（真实加载器；测试注入替身）。"""
    import qlib  # noqa: PLC0415
    import qlib.data as qd  # noqa: PLC0415
    from qlib.constant import REG_CN  # noqa: PLC0415

    qlib.init(provider_uri=qlib_dir, region=REG_CN)
    return qd.D.features(instruments, fields, start_time=start, end_time=end)


def qlib_calendar(
    start: str, end: str, *, qlib_dir: str = DEFAULT_QLIB_DIR
) -> pd.DatetimeIndex:
    import qlib  # noqa: PLC0415
    import qlib.data as qd  # noqa: PLC0415
    from qlib.constant import REG_CN  # noqa: PLC0415

    qlib.init(provider_uri=qlib_dir, region=REG_CN)
    return pd.DatetimeIndex(qd.D.calendar(start_time=start, end_time=end))


def qlib_instruments(
    market: str = "all", *, qlib_dir: str = DEFAULT_QLIB_DIR
) -> list[str]:
    """标的池（qlib 式小写代码）。

    解析必须走两步：``D.instruments`` 只回**配置字典**（如
    ``{"market": "all", "filter_pipe": []}``），直接 ``list()`` 拿到的是它的
    两个键名——数量恒为 2，任何市场都一样（实测踩过：整池静默取空）。
    真列表在 ``D.list_instruments(cfg, as_list=True)``。
    """
    import qlib  # noqa: PLC0415
    import qlib.data as qd  # noqa: PLC0415
    from qlib.constant import REG_CN  # noqa: PLC0415

    qlib.init(provider_uri=qlib_dir, region=REG_CN)
    return list(qd.D.list_instruments(qd.D.instruments(market=market), as_list=True))


def normalize_panel(raw: pd.DataFrame) -> pd.DataFrame:
    """qlib 取数结果（index=(instrument, datetime)）→ 标准面板。"""
    if raw.empty:
        empty_index = pd.MultiIndex.from_arrays(
            [pd.DatetimeIndex([]), pd.Index([], dtype=object)],
            names=["trade_date", "symbol"],
        )
        return pd.DataFrame(columns=list(PANEL_COLUMNS), index=empty_index)
    frame = raw.rename(columns={f"${name}": name for name in PANEL_COLUMNS})
    missing = [c for c in PANEL_COLUMNS if c not in frame.columns]
    if missing:
        raise ValueError(f"行情缺列: {missing}（期望 {list(PRICE_FIELDS)}）")
    frame = frame.reset_index()
    keys = list(frame.columns[:2])
    frame = frame.rename(columns={keys[0]: "symbol", keys[1]: "trade_date"})
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    frame["symbol"] = frame["symbol"].astype(str)
    panel = frame.set_index(["trade_date", "symbol"])[list(PANEL_COLUMNS)].sort_index()
    return panel.astype(float)


def load_price_panel(
    start: str,
    end: str,
    *,
    market: str = "all",
    symbols: list[str] | None = None,
    warmup_bars: int = _candidates.MAX_MIN_HISTORY,
    qlib_dir: str = DEFAULT_QLIB_DIR,
    loader: Callable[[list[str], list[str], str, str], pd.DataFrame] | None = None,
    calendar: pd.DatetimeIndex | None = None,
) -> pd.DataFrame:
    """取行情面板（含暖机段；暖机行由引擎计算完候选后裁掉）。"""
    cal = (
        calendar
        if calendar is not None
        # Warm-up needs bars before ``start``. Restricting the Qlib calendar to
        # the requested window silently returned no warm-up rows at all.
        else qlib_calendar("1900-01-01", end, qlib_dir=qlib_dir)
    )
    fetch_start = warmup_start(start, cal, warmup_bars)
    universe = (
        symbols if symbols is not None else qlib_instruments(market, qlib_dir=qlib_dir)
    )
    if not universe:
        raise ValueError(f"标的池为空（market={market!r}）")
    if loader is None:
        raw = _qlib_features(
            universe,
            list(PRICE_FIELDS),
            fetch_start.strftime("%Y-%m-%d"),
            end,
            qlib_dir,
        )
    else:
        raw = loader(
            universe, list(PRICE_FIELDS), fetch_start.strftime("%Y-%m-%d"), end
        )
    return normalize_panel(raw)


# ── 元数据（QuantDB instrument_detail） ──────────────────────────────


def _to_timestamp(value) -> pd.Timestamp | None:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    text = str(int(value)) if isinstance(value, (int, float)) else str(value)
    if len(text) != 8 or not text.isdigit():
        return None
    return pd.Timestamp(f"{text[:4]}-{text[4:6]}-{text[6:]}")


def build_symbol_metadata(raw: pd.DataFrame) -> pd.DataFrame:
    """instrument_detail 原始表 → 按代码数字索引的元数据表。

    列：symbol_raw / name / industry / listing_date / is_st / is_quit /
    float_shares（万股）。缺列容错（对应字段置 NaN/False），不因单列缺失整体炸。
    """
    if raw is None or raw.empty:
        return pd.DataFrame(
            columns=[
                "symbol_raw",
                "name",
                "industry",
                "listing_date",
                "is_st",
                "is_quit",
                "float_shares",
            ]
        ).rename_axis("symbol_digits")
    symbol_col = next(
        (c for c in ("symbol", "Symbol", "wind_code") if c in raw.columns), None
    )
    if symbol_col is None:
        raise ValueError(
            "instrument_detail 无 symbol 列（symbol/Symbol/wind_code 均缺）"
        )
    out = pd.DataFrame(
        index=pd.Index(
            [symbol_digits(v) for v in raw[symbol_col]], name="symbol_digits"
        )
    )
    out["symbol_raw"] = raw[symbol_col].astype(str).to_numpy()
    out["name"] = raw["Name"].astype(str).to_numpy() if "Name" in raw else ""
    out["industry"] = (
        raw["rs_hyname"].astype(str).to_numpy() if "rs_hyname" in raw else np.nan
    )
    out["listing_date"] = [
        _to_timestamp(v)
        for v in (raw["J_start"] if "J_start" in raw else [None] * len(raw))
    ]
    out["is_st"] = (
        raw["IsSTGP"].fillna(0).astype(bool).to_numpy() if "IsSTGP" in raw else False
    )
    out["is_quit"] = (
        raw["IsQuitGP"].fillna(0).astype(bool).to_numpy()
        if "IsQuitGP" in raw
        else False
    )
    out["float_shares"] = pd.to_numeric(
        raw["ActiveCapital"]
        if "ActiveCapital" in raw
        else pd.Series(np.nan, index=raw.index),
        errors="coerce",
    ).to_numpy()
    return out[~out.index.duplicated(keep="first")]


def load_symbol_metadata(*, hub=None) -> pd.DataFrame:
    """读取 QuantDB 元数据（hub 可注入；默认 quantdb_hub.fetch_stock_list）。"""
    if hub is None:
        from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub  # noqa: PLC0415

        hub = QuantDBDataHub.get_instance()
    return build_symbol_metadata(hub.fetch_stock_list())


def map_to_panel(panel: pd.DataFrame, symbol_series: pd.Series) -> pd.Series:
    """按代码数字把「symbol → 值」映射到面板逐行（前缀/后缀式混用皆可）。"""
    digits = pd.Index(map_unique(panel.index.get_level_values(1), symbol_digits))
    mapped = symbol_series.reindex(digits)
    return pd.Series(mapped.to_numpy(), index=panel.index, name=symbol_series.name)
