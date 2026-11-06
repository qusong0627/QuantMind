"""统计派 · A 股可交易性过滤（移植 P2 / Tier 1）。

IC 统计的样本必须**真能成交**，否则「涨停买不进、跌停卖不出、停牌干瞪眼」的
样本会系统性偏乐观。过滤按固定顺序逐级剔除（每个样本归因到第一个命中的原因，
报告可加总核对）：

1. **停牌/无成交**（``volume ≤ 0``）——面板缺 bar 的样本天然不存在；
2. **ST**（可关）——用 QuantDB ``IsSTGP`` **当前快照**：注意这是「现在是不是
   ST」而非「当时是不是 ST」，只能防当下风险、不能修历史偏差（历史 ST 需要
   状态表，QuantDB 无此数据；默认开，可关并在报告里如实标注口径）；
3. **次新**——上市不足 ``min_list_days`` 个交易日（上市日 ``J_start`` 真实值，
   按面板日历计交易日）；
4. **涨跌停**——当日 **收盘价** 触及涨跌停（收盘价不可成交）：幅度按板块给
   （主板 10%、创业板 2020-08-24 起 20% 之前 10%、科创板 20%、北交所 30%、
   ST 5%），留 ``limit_tolerance`` 容差吸收四舍五入；
5. **低流动性**（可关）——``min_amount_window`` 日均成交额处于当日截面
   ``min_amount_quantile`` 分位以下。

报告（:class:`FilterReport`）逐级计数，随任务产物落盘——IC 数字变了先查它。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np
import pandas as pd

#: 板块涨跌停幅度
BOARD_LIMIT = {"main": 0.10, "gem": 0.20, "star": 0.20, "bse": 0.30}
ST_LIMIT = 0.05
#: 创业板注册制（20%）生效日
GEM_20PCT_START = pd.Timestamp("2020-08-24")
#: 涨跌停判定容差（四舍五入后的 9.98% 也算涨停）
DEFAULT_LIMIT_TOLERANCE = 0.002


@dataclass(frozen=True)
class TradabilityConfig:
    exclude_suspended: bool = True
    st_snapshot_symbols: tuple[str, ...] = ()  # 当前 ST 池（空 = 不过滤 ST）
    min_list_days: int = 120
    exclude_limit_locked: bool = True
    limit_tolerance: float = DEFAULT_LIMIT_TOLERANCE
    min_amount_quantile: float = 0.0  # 0 = 关；0.2 = 踢当日成交额末 20%
    min_amount_window: int = 20


@dataclass(frozen=True)
class FilterReport:
    """逐级剔除计数（顺序归因，各级之和 = 总剔除）。"""

    n_input: int
    n_suspended: int
    n_st: int
    n_new_listing: int
    n_limit_locked: int
    n_low_liquidity: int

    @property
    def n_kept(self) -> int:
        return (
            self.n_input
            - self.n_suspended
            - self.n_st
            - self.n_new_listing
            - self.n_limit_locked
            - self.n_low_liquidity
        )


def symbol_digits(symbol: str) -> str:
    return re.sub(r"\D", "", str(symbol))


def map_unique(symbols: pd.Index, func) -> np.ndarray:
    """对**唯一** symbol 求值再回填到逐行（百万行面板上避免逐行调用）。"""
    unique = pd.Index(symbols.unique())
    table = pd.Series(unique.map(func).to_numpy(), index=unique)
    return table.reindex(symbols).to_numpy()


def board_of(symbol: str) -> str:
    """symbol（前缀/后缀式均可）→ 板块键（main/gem/star/bse）。"""
    code = symbol_digits(symbol)
    suffix = str(symbol).upper()
    if (
        suffix.endswith(".BJ")
        or code[:3] in {"920"}
        or code[:2] in {"43", "83", "87", "88"}
    ):
        return "bse"
    if code.startswith(("688", "689")):
        return "star"
    if code.startswith(("300", "301")):
        return "gem"
    return "main"


def limit_width(symbol: str, date: pd.Timestamp, *, is_st: bool = False) -> float:
    """逐样本涨跌停幅度（创业板注册制前按 10%）。"""
    if is_st:
        return ST_LIMIT
    board = board_of(symbol)
    if board == "gem" and date < GEM_20PCT_START:
        return BOARD_LIMIT["main"]
    return BOARD_LIMIT[board]


def limit_locked(
    panel: pd.DataFrame, *, tolerance: float = DEFAULT_LIMIT_TOLERANCE
) -> pd.Series:
    """当日收盘是否触及涨跌停（True = 收盘价不可成交）。

    幅度逐样本判定（板块 × 日期 × ST，全向量化）；首根 bar（无前收）→ False
    （另有暖机裁）。
    """
    close = panel["close"].astype(float)
    ret = (close / close.groupby(level=1).shift(1) - 1).to_numpy()
    symbols = panel.index.get_level_values(1)
    dates = panel.index.get_level_values(0)

    board = map_unique(symbols, board_of)
    # copy=True：pandas 3 CoW 下 to_numpy() 可能给只读视图，下面要就地改宽（ST/注册制前）
    width = pd.Series(BOARD_LIMIT).reindex(board).to_numpy(dtype=float, copy=True)
    if (board == "gem").any():
        early = (board == "gem") & (dates < GEM_20PCT_START)
        width[early] = BOARD_LIMIT["main"]
    if "is_st" in panel.columns:
        st = panel["is_st"].astype(bool).to_numpy()
        width[st] = ST_LIMIT

    return pd.Series(
        (ret >= width - tolerance) | (ret <= -(width - tolerance)), index=panel.index
    )


def _new_listing_mask(
    panel: pd.DataFrame, listing_dates: pd.Series, min_list_days: int
) -> pd.Series:
    """上市不足 min_list_days 个交易日（按面板日历计交易日）。

    ``listing_dates`` 索引与面板 symbol 均先归一到代码数字再对表（前缀/后缀式
    混用皆可）；上市日缺失的样本按「未知 → 保守剔除」处理（元数据没有这只票，
    不进 IC）。

    上市日在面板日历之外**不等于缺失**：早于面板起点 = 老票（searchsorted 得
    位次 0），晚于终点 = 全段次新。早期实现用 ``reindex`` 落位，老票的上市日
    不在日历里一律变 NaN 被当未知剔除——生产上面板起点必然晚于上市日，等于
    全市场误杀（回归测试：``test_listed_before_panel_start_is_not_new``）。
    """
    dates = pd.Index(sorted(panel.index.get_level_values(0).unique()))
    row_pos = (
        pd.Series(np.arange(len(dates)), index=dates)
        .reindex(panel.index.get_level_values(0))
        .to_numpy()
    )

    normalized = pd.Series(
        pd.to_datetime(listing_dates).to_numpy(),
        index=pd.Index([symbol_digits(s) for s in listing_dates.index]),
    )
    normalized = normalized[~normalized.index.duplicated(keep="first")]
    panel_digits = pd.Index(map_unique(panel.index.get_level_values(1), symbol_digits))
    mapped = normalized.reindex(panel_digits).to_numpy()

    known = ~pd.isna(mapped)
    too_new = ~known  # 上市日缺失（元数据没这只票）→ 保守剔除
    if known.any():
        listing = pd.DatetimeIndex(mapped[known])
        # 面板日历里的插入位 = 该股在面板内的第一个「满上市日」行；晚于面板终点
        # → len(dates)（全段次新）；早于起点 → 0，但位次 0 不能直接当「上市当天」
        # （见上），老票单独用 listed_before_start 放行
        listing_pos = dates.searchsorted(listing, side="left")
        listed_before_start = listing < dates[0]
        bars_since_listing = row_pos[known] - listing_pos
        too_new[known] = (bars_since_listing < min_list_days) & ~listed_before_start
    return pd.Series(too_new, index=panel.index)


def _low_liquidity_mask(panel: pd.DataFrame, cfg: TradabilityConfig) -> pd.Series:
    """当日截面成交额（N 日均值）**严格低于**分位阈值 → 剔除。

    用严格低于而非 rank ≤ 分位：全截面并列（如全停牌日）不该凭空剔人；
    并列时 ``quantile`` 阈值等于取值 → 无人被剔。
    """
    amount_ma = (
        panel["amount"]
        .astype(float)
        .groupby(level=1)
        .transform(lambda s: s.rolling(cfg.min_amount_window, min_periods=1).mean())
    )
    by_day = amount_ma.groupby(level=0)
    threshold = by_day.transform(lambda s: s.quantile(cfg.min_amount_quantile))
    return amount_ma < threshold


def build_mask(
    panel: pd.DataFrame,
    *,
    cfg: TradabilityConfig,
    listing_dates: pd.Series | None = None,
) -> tuple[pd.Series, FilterReport]:
    """逐级过滤 → (可交易掩码, 报告)。掩码与面板同索引（True = 保留）。"""
    mask = pd.Series(True, index=panel.index)
    counts = {"suspended": 0, "st": 0, "new": 0, "limit": 0, "liquidity": 0}

    def drop(reason: str, condition: pd.Series) -> None:
        nonlocal mask
        hit = condition & mask
        counts[reason] += int(hit.sum())
        mask &= ~condition

    if cfg.exclude_suspended:
        drop("suspended", panel["volume"].astype(float).fillna(0) <= 0)
    if cfg.st_snapshot_symbols:
        st_set = {symbol_digits(s) for s in cfg.st_snapshot_symbols}
        st_hit = pd.Index(
            map_unique(panel.index.get_level_values(1), symbol_digits)
        ).isin(st_set)
        drop("st", pd.Series(st_hit, index=panel.index))
    if cfg.min_list_days > 0 and listing_dates is not None:
        drop("new", _new_listing_mask(panel, listing_dates, cfg.min_list_days))
    if cfg.exclude_limit_locked:
        drop("limit", limit_locked(panel, tolerance=cfg.limit_tolerance))
    if cfg.min_amount_quantile > 0:
        drop("liquidity", _low_liquidity_mask(panel, cfg))

    return mask, FilterReport(
        n_input=len(panel),
        n_suspended=counts["suspended"],
        n_st=counts["st"],
        n_new_listing=counts["new"],
        n_limit_locked=counts["limit"],
        n_low_liquidity=counts["liquidity"],
    )
