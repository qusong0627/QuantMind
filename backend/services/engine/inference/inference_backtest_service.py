"""
推理回测服务 — 基于推理信号 + 选股策略的事件驱动回测。

数据流:
1. 逐日跑模型推理（复用 BacktestService 的推理能力）得到每日全市场 fusion_score，
   或直接读 engine_signal_scores 已有信号（数据源二选一，见 mode）。
2. 按策略三层过滤选股:
   - 第1层 行业信号: 申万行业 Top1 分数 → avgTop1 决定入场/空仓
   - 第2层 个股分数区间: 0.10-0.12 黄金区间 + 主板优先
   - 第3层 板块/趋势过滤: 排除涨停/ST/北交所/科创板高分
3. 事件驱动模拟: T+1 开盘买入 → 持有 N 天 / 止盈止损 / 行业消失清仓 / 大盘跌破 MA20 强制空仓
4. 输出净值序列、交易流水、持仓快照、行业统计。

设计约束:
- 与实盘推理同口径: 信号来源 = 模型对 T 日特征数据的推理输出。
- T+1 结算: 信号日 T 选股，T+1 开盘执行，避免前视偏差。
- 涨跌停不可成交: 涨停买不进 / 跌停卖不出。
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .shenwan_industry import load_shenwan_industry_map

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 策略参数
# ---------------------------------------------------------------------------


@dataclass
class StrategyConfig:
    """选股策略参数。默认值 = 平衡型（用户策略文档推荐组合）。"""

    # 入场/空仓（行业 avg Top1）
    entry_threshold: float = 0.09  # 行业avgTop1 ≥ 此值才入场
    exit_threshold: float = 0.06  # 行业avgTop1 < 此值强制空仓
    strong_industry_min: int = 2  # 强行业数（Top1≥0.10）≥ 此值才入场

    # 个股分数区间
    score_min: float = 0.10
    score_max: float = 0.12

    # 交易
    initial_capital: float = 100_000.0
    max_hold_days: int = 5  # 最长持有交易日
    take_profit: float = 0.08  # 止盈 +8%
    stop_loss: float = 0.05  # 止损 -5%
    max_positions: int = 5  # 每日最多持有股票数
    daily_select_max: int = 5  # 每日新选股上限

    # 过滤开关
    exclude_limit_moves: bool = True  # 涨停买不进/跌停卖不出
    exclude_st: bool = True  # 剔除 ST
    main_board_only: bool = True  # 仅主板（600/000 开头）
    use_index_ma20_filter: bool = True  # 大盘跌破 MA20 强制空仓
    index_symbol: str = "sh000001"  # 上证指数

    # 数据源
    signal_mode: str = "realtime"  # realtime=逐日推理 | stored=读已有信号

    @classmethod
    def preset(cls, name: str) -> StrategyConfig:
        """策略风格预设。"""
        base = cls()
        if name == "conservative":
            base.entry_threshold = 0.10
            base.exit_threshold = 0.10
            base.strong_industry_min = 5
        elif name == "aggressive":
            base.entry_threshold = 0.07
            base.exit_threshold = 0.06
            base.strong_industry_min = 1
        return base


@dataclass
class Position:
    """持仓记录。"""

    symbol: str
    name: str
    industry: str
    score: float
    buy_date: str
    buy_price: float
    shares: int
    hold_days: int = 0
    sell_date: str | None = None
    sell_price: float | None = None
    sell_reason: str | None = None
    profit_pct: float = 0.0
    open_pnl: float = 0.0
    realized_pnl: float = 0.0


@dataclass
class Trade:
    """成交记录。"""

    date: str
    symbol: str
    name: str
    side: str  # BUY / SELL
    price: float
    shares: int
    amount: float
    industry: str
    score: float
    reason: str = ""  # 买入理由 / 卖出理由
    profit_pct: float = 0.0
    hold_days: int = 0


@dataclass
class DailySelection:
    """单日选股结果。"""

    trade_date: str
    market_state: str
    industry_avg_top1: float
    strong_industry_count: int
    index_above_ma20: bool
    selections: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class BacktestResult:
    """回测结果汇总。"""

    status: str
    metrics: dict[str, Any]
    daily_selections: list[DailySelection]
    trades: list[Trade]
    nav_curve: list[dict[str, Any]]  # [{date, nav, cash, holdings_value, drawdown}]
    holdings_snapshot: list[dict[str, Any]]  # 每日持仓快照
    industry_rotation: list[dict[str, Any]]  # 每月强行业统计
    monthly_returns: dict[str, float]
    errors: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 行业信号计算
# ---------------------------------------------------------------------------


def _compute_industry_signals(
    day_scores: pd.DataFrame,
    industry_map: dict[str, str],
    strong_threshold: float = 0.10,
) -> tuple[dict[str, float], dict[str, int], float, int]:
    """计算单日行业信号。

    day_scores: DataFrame[symbol, score]
    industry_map: {suffix_symbol: 申万行业名}

    返回 (行业Top1分数dict, 行业股票数dict, avgTop1, 强行业数)
    - 行业Top1: 该行业内分数最高那只股票的分数
    - avgTop1: Top20股票所在行业的Top1分数均值
    - 强行业数: Top1 ≥ 0.10 的行业个数
    """
    if day_scores.empty:
        return {}, {}, 0.0, 0

    joined = day_scores.copy()
    joined["industry"] = joined["symbol"].map(industry_map)
    joined = joined[joined["industry"].notna() & (joined["industry"] != "")]

    if joined.empty:
        return {}, {}, 0.0, 0

    # 行业 Top1 分数（该行业分数最高股票的分数）
    ind_top1 = (
        joined.sort_values("score", ascending=False)
        .groupby("industry")
        .first()["score"]
        .to_dict()
    )

    # 行业股票数
    ind_count = joined.groupby("industry")["score"].count().to_dict()

    # avgTop1 = Top20 股票所在行业的 Top1 分数均值
    top20 = joined.nlargest(20, "score")
    top20_industries = top20["industry"].unique()
    if len(top20_industries) > 0:
        avg_top1 = float(
            np.mean([ind_top1[i] for i in top20_industries if i in ind_top1])
        )
    else:
        avg_top1 = 0.0

    # 强行业数: Top1 ≥ strong_threshold 的行业个数
    # （T-P4-03：阈值可注入——分位模式传分布分位；默认 0.10 保持存量等价）
    strong = sum(1 for v in ind_top1.values() if v >= strong_threshold)

    return ind_top1, ind_count, avg_top1, strong


def _market_state(avg_top1: float, strong_count: int) -> str:
    """按行业信号判断市场状态。"""
    if avg_top1 >= 0.12:
        return "牛市"
    if avg_top1 >= 0.10:
        return "震荡偏强"
    if avg_top1 >= 0.09:
        return "震荡"
    if avg_top1 >= 0.06:
        return "震荡偏弱"
    return "熊市"


# ---------------------------------------------------------------------------
# 价格数据加载
# ---------------------------------------------------------------------------


def _load_price_panel(
    data_dir: Path,
    trade_dates: list[str],
) -> pd.DataFrame:
    """加载回测区间的每日价格面板（open/high/low/close/pct_change 等）。

    价格数据源用 DB 月表 stock_daily_new_YYYY_MM（**已复权**）而非 feature parquet
    （不复权）。parquet 的 close 在除权日会跳变（如 43→138），用未复权价算
    持仓收益会产生虚假的翻倍收益；月表 close 已做前复权处理，价格连续。

    月表 symbol 是 prefix 格式（SH600459），统一转 suffix（600459.SH）。
    """
    if not trade_dates:
        return pd.DataFrame()

    # 按月分组查询
    month_dates: dict[str, list[str]] = {}
    for d in trade_dates:
        month_dates.setdefault(d[:7], []).append(d)

    # 用 psycopg2 同步连接读取（独立于异步连接池，避免跨事件循环复用冲突）
    import os

    import psycopg2

    conn_params = {
        "host": os.getenv("DB_HOST", "db"),
        "port": int(os.getenv("DB_PORT", "5432")),
        "dbname": os.getenv("DB_NAME", "quantmind"),
        "user": os.getenv("DB_USER", "quantmind"),
        "password": os.getenv("DB_PASSWORD", ""),
    }

    frames: list[pd.DataFrame] = []
    try:
        conn = psycopg2.connect(**conn_params)
        try:
            cur = conn.cursor()
            for month, dates in month_dates.items():
                table = f"stock_daily_new_{month.replace('-', '_')}"
                try:
                    cur.execute(
                        f"""
                        SELECT symbol, trade_date::text, open, high, low, close,
                               volume, amount, pct_change, ma20, is_st
                        FROM {table}
                        WHERE trade_date = ANY(%s::date[])
                        """,
                        (dates,),
                    )
                    rows = cur.fetchall()
                    if rows:
                        frames.append(
                            pd.DataFrame(
                                rows,
                                columns=[
                                    "symbol",
                                    "trade_date",
                                    "open",
                                    "high",
                                    "low",
                                    "close",
                                    "volume",
                                    "amount",
                                    "pct_change",
                                    "ma20",
                                    "is_st",
                                ],
                            )
                        )
                except Exception as exc:
                    logger.warning("读取价格表 %s 失败: %s", table, exc)
            cur.close()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("连接 DB 加载价格失败: %s", exc)

    if not frames:
        return pd.DataFrame()

    panel = pd.concat(frames, ignore_index=True)
    panel["trade_date"] = pd.to_datetime(panel["trade_date"]).dt.strftime("%Y-%m-%d")
    panel = panel[panel["trade_date"].isin(trade_dates)]
    panel["symbol"] = panel["symbol"].astype(str).str.strip()
    # 统一 symbol 为规范 suffix 格式（SH600459 → 600459.SH）
    from backend.shared.stock_utils import StockCodeUtil

    panel["symbol"] = panel["symbol"].map(StockCodeUtil.to_suffix)
    panel = panel.drop_duplicates(subset=["symbol", "trade_date"], keep="last")
    return panel


def _load_index_close(
    data_dir: Path,
    index_symbol: str,
    trade_dates: list[str],
) -> pd.Series:
    """加载上证指数收盘价序列（用于 MA20 过滤）。

    指数数据来自 QuantDB index_daily（000001.SH 上证综指），而非个股 parquet。
    index_symbol 支持 '000001.SH' 或 'sh000001' 格式。
    """
    if not trade_dates:
        return pd.Series(dtype=float)

    # 归一化指数代码 → 000001.SH
    sym = str(index_symbol).lower().strip().replace("sh", "").replace("sz", "")
    if "." in sym:
        num, mkt = sym.split(".")
        sym = f"{num}.{mkt.upper()}"
    else:
        sym = f"{sym}.SH"

    try:
        from datetime import date as _date

        from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

        hub = QuantDBDataHub()
        df = hub.fetch_index_kline(
            sym,
            _date.fromisoformat(trade_dates[0]),
            _date.fromisoformat(trade_dates[-1]),
        )
        if df is None or df.empty:
            logger.warning("QuantDB 无指数 %s 数据", sym)
            return pd.Series(dtype=float)

        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
        df = df.drop_duplicates(subset=["trade_date"], keep="last")
        return df.set_index("trade_date")["close"].astype(float).sort_index()
    except Exception as exc:
        logger.warning("加载指数 %s 失败: %s", sym, exc)
        return pd.Series(dtype=float)


# ---------------------------------------------------------------------------
# 选股策略
# ---------------------------------------------------------------------------


def _is_main_board(symbol: str) -> bool:
    """主板判断: 600/601/603/605/000/001/002 开头（含后缀）。"""
    s = symbol.split(".")[0] if "." in symbol else symbol
    return s.startswith(("600", "601", "603", "605", "000", "001", "002"))


def _is_star_market(symbol: str) -> bool:
    """科创板(688)/创业板(300) 判断。"""
    s = symbol.split(".")[0] if "." in symbol else symbol
    return s.startswith(("688", "300", "301"))


def _check_three_day_trend(
    score_t_minus_1: float | None,
    score_t: float,
    score_t_plus_1: float | None,
) -> tuple[bool, str]:
    """3 天分数趋势判断（策略文档第三节）。

    返回 (可买入, 趋势标签):
      - 先升后降: T-1 低 → T 高 → T+1 回落 = 最佳买点（78% 胜率）
      - 连续上升: T-1 低 → T 高 → T+1 更高 = 过热，不追（62% 胜率）
      - 连续下降: T-1 高 → T 降 → T+1 更低 = 信号衰退，不买
    缺失数据时按「能判断的判断、不能判断的不排除」处理：
      - 只有 T-1: 今日高于昨日 → 上升中（可买）；否则 → 回落中（不排除）
      - 只有 T+1: 明日低于今日 → 可买；明日不低于今日 → 视为连续上升排除
    """
    if score_t is None:
        return False, "无今日分数"

    # 完整三天数据
    if score_t_minus_1 is not None and score_t_plus_1 is not None:
        if score_t_minus_1 < score_t and score_t_plus_1 < score_t:
            return True, "先升后降"
        if score_t_minus_1 < score_t and score_t_plus_1 >= score_t:
            return False, "连续上升"
        if score_t_minus_1 >= score_t and score_t_plus_1 < score_t:
            return False, "连续下降"
        return True, "震荡"

    # 只有 T-1
    if score_t_minus_1 is not None:
        if score_t_minus_1 < score_t:
            return True, "上升中"
        return True, "回落中"

    # 只有 T+1（今日分 vs 预测分）
    if score_t_plus_1 is not None:
        if score_t_plus_1 < score_t:
            return True, "明日回落"
        return False, "连续上升"

    return True, "趋势未知"


def _limit_threshold_pct(
    symbol: str, trade_date: object, is_st: object = False
) -> float:
    """该标的当日的涨跌停幅度（**百分数**，已扣取整余量）。

    板规/ST/制度日期全部委托 `limit_pct`（唯一权威实现），本模块不再自持阈值。
    `limit_pct` 返回比例（0.20），而本文件的 `pct_change` 是百分数，故 ×100。

    余量同源：唯一事实源 = `local_market_data.limit_threshold`（= 板别幅度 − 该板别
    的取整容差，北交所容差翻倍；本文件的 `pct_change` 是百分数，故 ×100）。本文件
    曾自带 `_LIMIT_SLACK_PCT = 0.2`，并在注释里承认「与 cn_exchange 不同源：两处各自
    沿用了自己的历史余量」—— 同值或异值都不该由各文件自己决定。0.2pp 已由实测
    证伪：在股价 < ¥2.50 时漏判真涨停（41 个交易日 × 2864 个封板事件中漏 4 条）。
    """
    from backend.services.simulation.services.local_market_data import (
        limit_threshold,
    )

    td = date.today() if pd.isna(trade_date) else pd.Timestamp(trade_date).date()
    return limit_threshold(str(symbol), is_st=bool(is_st), trade_date=td) * 100.0


def _select_stocks_daily(
    day_scores: pd.DataFrame,
    industry_map: dict[str, str],
    config: StrategyConfig,
    price_day: pd.DataFrame | None,
    history_scores: dict[str, dict[str, float] | None] | None = None,
) -> list[dict[str, Any]]:
    """按策略单日选股。day_scores: DataFrame[symbol, score]（已去重）。

    history_scores: {suffix_symbol: {score_t_minus_1, score_t_plus_1}} 可选，
    传入时对候选股做 3 天趋势过滤（先升后降=最佳买点）。
    """
    if day_scores.empty:
        return []

    from backend.shared.stock_utils import StockCodeUtil

    df = day_scores.copy()
    df["symbol"] = df["symbol"].map(StockCodeUtil.to_suffix)
    df["industry"] = df["symbol"].map(industry_map)

    # 第2层: 个股分数区间
    df = df[(df["score"] >= config.score_min) & (df["score"] <= config.score_max)]
    if df.empty:
        # 空集提前返回（T-P4-01 等价性工具实测抓到）：空 DataFrame 被空布尔 Series
        # 索引会丢列（pandas 行为），后续 df["symbol"] 直接 KeyError 崩溃——
        # 真库空带日（如量纲错位期）复现过。
        return []

    # 主板优先
    if config.main_board_only:
        df = df[df["symbol"].apply(_is_main_board)]

    # 排除科创板高分
    df = df[~df["symbol"].apply(_is_star_market)]

    # 排除 ST / 涨停（若价格数据可用）
    if price_day is not None and not price_day.empty:
        price_map = price_day.set_index("symbol")
        has_st_col = "is_st" in price_day.columns
        keep = []
        for _, row in df.iterrows():
            p = (
                price_map.loc[row["symbol"]]
                if row["symbol"] in price_map.index
                else None
            )
            if p is None:
                keep.append(True)
                continue
            if (
                config.exclude_st
                and has_st_col
                and pd.notna(p.get("is_st"))
                and float(p["is_st"]) == 1
            ):
                keep.append(False)
                continue
            if config.exclude_limit_moves and pd.notna(p.get("pct_change")):
                pct = float(p["pct_change"])
                is_st = (
                    bool(p.get("is_st"))
                    if has_st_col and pd.notna(p.get("is_st"))
                    else False
                )
                thresh = _limit_threshold_pct(row["symbol"], p.get("trade_date"), is_st)
                if abs(pct) >= thresh:  # 触及涨停或跌停（按该板块当日的真实幅度）
                    keep.append(False)
                    continue
            keep.append(True)
        df = df[keep]

    # 行业必须有信号（非空）。用 reindex 确保列存在（空结果时 pandas 会 drop 全 NaN 列）
    if "industry" not in df.columns:
        df["industry"] = ""
    df = df[df["industry"].notna() & (df["industry"] != "")]

    # 3 天趋势过滤（可选）: 保留「先升后降/上升中/明日回落」等可买信号，
    # 排除「连续上升/连续下降」等过热或衰退信号。
    trend_map: dict[str, str] = {}
    if history_scores:
        keep = []
        for row in df.itertuples(index=False):
            hist = history_scores.get(row.symbol)
            if hist is None:
                keep.append(True)
                continue
            ok, trend = _check_three_day_trend(
                hist.get("score_t_minus_1"),
                float(row.score),
                hist.get("score_t_plus_1"),
            )
            trend_map[row.symbol] = trend
            keep.append(ok)
        df = df[keep]

    # 按分数降序，取前 daily_select_max
    if df.empty:
        return []
    df = df.sort_values("score", ascending=False).head(config.daily_select_max)

    return [
        {
            "symbol": r.symbol,
            "score": float(r.score),
            "industry": str(r.industry),
            "trend": trend_map.get(r.symbol, "趋势未知"),
        }
        for r in df.itertuples(index=False)
    ]


# ---------------------------------------------------------------------------
# 事件驱动模拟引擎
# ---------------------------------------------------------------------------


class _SimulationEngine:
    """事件驱动交易模拟。"""

    def __init__(self, config: StrategyConfig, industry_map: dict[str, str]):
        self.config = config
        self.industry_map = industry_map
        self.cash = config.initial_capital
        self.positions: dict[str, Position] = {}
        self.trades: list[Trade] = []
        self.daily_selections: list[DailySelection] = []
        self.nav_history: list[dict[str, Any]] = []
        self.holdings_snapshot: list[dict[str, Any]] = []
        self.index_close: pd.Series = pd.Series(dtype=float)
        self.index_ma20: pd.Series = pd.Series(dtype=float)
        self.price_panel: pd.DataFrame = pd.DataFrame()
        self.trade_dates_sorted: list[str] = []
        self.date_pos: dict[str, int] = {}

    def setup_prices(self, panel: pd.DataFrame, index_series: pd.Series) -> None:
        self.price_panel = panel
        self.index_close = index_series
        if not index_series.empty:
            self.index_ma20 = index_series.rolling(20).mean()
        self.trade_dates_sorted = (
            sorted(set(panel["trade_date"])) if not panel.empty else []
        )
        self.date_pos = {d: i for i, d in enumerate(self.trade_dates_sorted)}

    # -- 工具 --

    def _next_date(self, trade_date: str) -> str | None:
        idx = self.date_pos.get(trade_date)
        if idx is None or idx + 1 >= len(self.trade_dates_sorted):
            return None
        return self.trade_dates_sorted[idx + 1]

    def _get_prices(self, symbol: str, trade_date: str) -> dict[str, float] | None:
        row = self.price_panel[
            (self.price_panel["symbol"] == symbol)
            & (self.price_panel["trade_date"] == trade_date)
        ]
        if row.empty:
            return None
        r = row.iloc[0]
        return {
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
        }

    def _index_above_ma20(self, trade_date: str) -> bool:
        if self.index_close.empty or self.index_ma20.empty:
            return True  # 无指数数据时不启用过滤
        if trade_date not in self.index_close.index:
            return True
        close = self.index_close[trade_date]
        ma = self.index_ma20[trade_date]
        if pd.isna(ma):
            return True
        return float(close) >= float(ma)

    # -- 每日推进 --

    def run_day(
        self,
        trade_date: str,
        day_scores: pd.DataFrame,
    ) -> None:
        """处理一个交易日: 卖出到期/止盈止损 → 大盘检查 → 行业信号 → 选股买入。"""
        # 统一信号 symbol 为 suffix 格式（engine_signal_scores 里格式混杂）
        if not day_scores.empty and "symbol" in day_scores.columns:
            from backend.shared.stock_utils import StockCodeUtil

            day_scores = day_scores.copy()
            day_scores["symbol"] = day_scores["symbol"].map(StockCodeUtil.to_suffix)

        price_day = (
            self.price_panel[self.price_panel["trade_date"] == trade_date]
            if not self.price_panel.empty
            else pd.DataFrame()
        )

        # 1. 大盘 MA20 过滤
        index_ok = self._index_above_ma20(trade_date)

        # 2. 行业信号（用信号日分数）
        ind_top1, ind_count, avg_top1, strong_count = _compute_industry_signals(
            day_scores, self.industry_map
        )
        state = _market_state(avg_top1, strong_count)

        # 3. 卖出逻辑
        self._process_sells(trade_date, avg_top1, ind_top1, index_ok)

        # 4. 入场判断
        should_enter = (
            index_ok
            and avg_top1 >= self.config.entry_threshold
            and strong_count >= self.config.strong_industry_min
        )
        if not should_enter:
            self.daily_selections.append(
                DailySelection(
                    trade_date=trade_date,
                    market_state=state,
                    industry_avg_top1=avg_top1,
                    strong_industry_count=strong_count,
                    index_above_ma20=index_ok,
                    selections=[],
                )
            )
            self._record_nav(trade_date, price_day)
            return

        # 5. 选股（分数区间 + 过滤）
        picks = _select_stocks_daily(
            day_scores, self.industry_map, self.config, price_day
        )

        # 6. 买入（T+1 开盘执行: 今天信号 → 下个交易日开盘买）
        exec_date = self._next_date(trade_date)
        if exec_date:
            self._process_buys(picks, exec_date, price_day)

        self.daily_selections.append(
            DailySelection(
                trade_date=trade_date,
                market_state=state,
                industry_avg_top1=avg_top1,
                strong_industry_count=strong_count,
                index_above_ma20=index_ok,
                selections=picks,
            )
        )
        self._record_nav(trade_date, price_day)

    # -- 卖出 --

    def _process_sells(
        self,
        trade_date: str,
        avg_top1: float,
        ind_top1: dict[str, float],
        index_ok: bool,
    ) -> None:
        to_sell: list[tuple[str, str]] = []  # (symbol, reason)

        for symbol, pos in list(self.positions.items()):
            price = self._get_prices(symbol, trade_date)
            if price is None:
                continue
            close = price["close"]
            pos.hold_days += 1
            pos.open_pnl = close / pos.buy_price - 1.0

            # 持有到期
            if pos.hold_days >= self.config.max_hold_days:
                to_sell.append((symbol, "持有到期"))
                continue
            # 止盈
            if pos.open_pnl >= self.config.take_profit:
                to_sell.append((symbol, "止盈"))
                continue
            # 止损（跌停无法卖出时忽略）
            if pos.open_pnl <= -self.config.stop_loss:
                limit_down = self._is_limit_down(symbol, trade_date)
                if not limit_down:
                    to_sell.append((symbol, "止损"))
                continue
            # 行业消失（该行业 Top1 跌破入场线）
            ind = pos.industry
            if ind in ind_top1 and ind_top1[ind] < self.config.entry_threshold:
                to_sell.append((symbol, "行业转弱"))
                continue
            # 大盘跌破 MA20 → 全部清仓
            if not index_ok and self.config.use_index_ma20_filter:
                to_sell.append((symbol, "大盘MA20"))

        for symbol, reason in to_sell:
            pos = self.positions[symbol]
            self._execute_sell(pos, trade_date, reason)

    def _is_limit_down(self, symbol: str, trade_date: str) -> bool:
        row = self.price_panel[
            (self.price_panel["symbol"] == symbol)
            & (self.price_panel["trade_date"] == trade_date)
        ]
        if row.empty or "pct_change" not in row.columns:
            return False
        rec = row.iloc[0]
        pct = rec.get("pct_change")
        if pct is None or pd.isna(pct):
            return False
        is_st = (
            bool(rec.get("is_st"))
            if "is_st" in row.columns and pd.notna(rec.get("is_st"))
            else False
        )
        return float(pct) <= -_limit_threshold_pct(symbol, trade_date, is_st)

    def _execute_sell(self, pos: Position, trade_date: str, reason: str) -> None:
        price = self._get_prices(pos.symbol, trade_date)
        if price is None:
            return
        # 收盘价卖出（策略: 收盘卖优于开盘卖）
        sell_price = price["close"]
        amount = sell_price * pos.shares
        # 卖出印花税 + 佣金
        cost = amount * (0.001 + 0.00025)
        self.cash += amount - cost
        pos.sell_date = trade_date
        pos.sell_price = sell_price
        pos.sell_reason = reason
        pos.profit_pct = sell_price / pos.buy_price - 1.0
        pos.realized_pnl = (sell_price - pos.buy_price) * pos.shares - cost

        self.trades.append(
            Trade(
                date=trade_date,
                symbol=pos.symbol,
                name=pos.name,
                side="SELL",
                price=sell_price,
                shares=pos.shares,
                amount=amount,
                industry=pos.industry,
                score=pos.score,
                reason=reason,
                profit_pct=pos.profit_pct,
                hold_days=pos.hold_days,
            )
        )
        del self.positions[pos.symbol]

    # -- 买入 --

    def _process_buys(
        self,
        picks: list[dict[str, Any]],
        exec_date: str,
        signal_price_day: pd.DataFrame,
    ) -> None:
        slots = self.config.max_positions - len(self.positions)
        if slots <= 0:
            return
        for pick in picks[:slots]:
            symbol = pick["symbol"]
            price = self._get_prices(symbol, exec_date)
            if price is None:
                continue
            buy_price = price["open"]
            if buy_price <= 0:
                continue

            # 每笔资金: 可用现金按份数均分
            alloc = self.cash / max(1, slots)
            shares = int(alloc / buy_price / 100) * 100
            if shares <= 0:
                continue
            amount = shares * buy_price
            cost = amount * 0.00025  # 佣金
            if amount + cost > self.cash:
                shares = int((self.cash - cost) / buy_price / 100) * 100
                if shares <= 0:
                    continue
                amount = shares * buy_price
                cost = amount * 0.00025

            self.cash -= amount + cost
            pos = Position(
                symbol=symbol,
                name="",
                industry=pick["industry"],
                score=pick["score"],
                buy_date=exec_date,
                buy_price=buy_price,
                shares=shares,
            )
            self.positions[symbol] = pos
            self.trades.append(
                Trade(
                    date=exec_date,
                    symbol=symbol,
                    name="",
                    side="BUY",
                    price=buy_price,
                    shares=shares,
                    amount=amount,
                    industry=pick["industry"],
                    score=pick["score"],
                    reason="行业信号+分数区间",
                )
            )

    # -- 净值 --

    def _record_nav(self, trade_date: str, price_day: pd.DataFrame) -> None:
        holdings_value = 0.0
        for pos in self.positions.values():
            price = self._get_prices(pos.symbol, trade_date)
            if price:
                holdings_value += price["close"] * pos.shares
        nav = self.cash + holdings_value
        self.nav_history.append(
            {
                "date": trade_date,
                "nav": round(nav, 2),
                "cash": round(self.cash, 2),
                "holdings": round(holdings_value, 2),
                "position_count": len(self.positions),
            }
        )

    def finalize(self) -> None:
        # 回测结束，按最后一日收盘价强制平仓（不产生交易流水，仅计净值）
        if self.nav_history:
            last = self.nav_history[-1]
            # 未实现盈亏已计入净值


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def run_inference_backtest(
    *,
    model_id: str,
    start_date: str,
    end_date: str,
    data_dir: Path | None = None,
    model_meta: dict[str, Any] | None = None,
    config: StrategyConfig | None = None,
    signal_provider: Any = None,
) -> BacktestResult:
    """运行推理回测。

    signal_provider: 可调用对象，输入 trade_date 返回该日全市场 DataFrame[symbol, score]。
                     不传时用 realtime 模式逐日推理（需配合推理引擎）。
    """
    config = config or StrategyConfig()
    industry_map = load_shenwan_industry_map()
    data_dir = data_dir or Path("/app/db/feature_snapshots")

    # 1. 获取交易日序列
    from .data_loader import get_available_dates

    trade_dates = get_available_dates(
        data_dir=data_dir,
        start_date=start_date,
        end_date=end_date,
        meta=model_meta,
    )
    if not trade_dates:
        return BacktestResult(
            status="error",
            metrics={},
            daily_selections=[],
            trades=[],
            nav_curve=[],
            holdings_snapshot=[],
            industry_rotation=[],
            monthly_returns={},
            errors=[{"error": f"日期范围 {start_date} ~ {end_date} 内无可用数据"}],
        )

    # 2. 加载价格面板 + 指数
    panel = _load_price_panel(data_dir, trade_dates)
    index_series = _load_index_close(data_dir, config.index_symbol, trade_dates)

    if panel.empty:
        return BacktestResult(
            status="error",
            metrics={},
            daily_selections=[],
            trades=[],
            nav_curve=[],
            holdings_snapshot=[],
            industry_rotation=[],
            monthly_returns={},
            errors=[{"error": "价格面板为空"}],
        )

    # 3. 初始化模拟引擎
    engine = _SimulationEngine(config, industry_map)
    engine.setup_prices(panel, index_series)

    # 4. 逐日执行
    errors: list[dict[str, str]] = []
    for trade_date in trade_dates:
        try:
            # 获取当日信号
            if signal_provider is not None:
                day_scores = signal_provider(trade_date)
            else:
                day_scores = _realtime_signals(trade_date, data_dir)
            if day_scores is None or day_scores.empty:
                # 无信号日：仍推进卖出逻辑（用空信号，行业 avg=0 → 空仓）
                day_scores = pd.DataFrame(columns=["symbol", "score"])
            engine.run_day(trade_date, day_scores)
        except Exception as exc:
            logger.warning("回测日期 %s 处理失败: %s", trade_date, exc)
            errors.append({"date": trade_date, "error": str(exc)})

    engine.finalize()

    # 5. 汇总指标
    metrics = _compute_metrics(engine.nav_history, engine.trades, config)
    monthly = _compute_monthly_returns(engine.nav_history)
    rotation = _compute_industry_rotation(engine.daily_selections)

    return BacktestResult(
        status="success",
        metrics=metrics,
        daily_selections=engine.daily_selections,
        trades=engine.trades,
        nav_curve=engine.nav_history,
        holdings_snapshot=engine.holdings_snapshot,
        industry_rotation=rotation,
        monthly_returns=monthly,
        errors=errors,
    )


def _realtime_signals(trade_date: str, data_dir: Path) -> pd.DataFrame | None:
    """实时推理信号提供者（占位，实际由 API 层注入）。"""
    raise NotImplementedError(
        "realtime 信号需要注入推理引擎；stored 模式请传 signal_provider"
    )


# ---------------------------------------------------------------------------
# 指标计算
# ---------------------------------------------------------------------------


def _compute_metrics(
    nav_history: list[dict[str, Any]],
    trades: list[Trade],
    config: StrategyConfig,
) -> dict[str, Any]:
    if not nav_history:
        return {"total_return": 0.0, "max_drawdown": 0.0, "trade_count": 0}

    navs = [n["nav"] for n in nav_history]
    start_nav = config.initial_capital
    end_nav = navs[-1]
    total_return = end_nav / start_nav - 1.0

    # 年化（按自然日粗算）
    days = max(1, len(nav_history))
    years = days / 252.0
    annualized = (1 + total_return) ** (1 / years) - 1 if total_return > -1 else -1.0

    # 最大回撤
    peak = navs[0]
    max_dd = 0.0
    for n in navs:
        peak = max(peak, n)
        dd = (peak - n) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)

    # 胜率
    sells = [t for t in trades if t.side == "SELL"]
    wins = [t for t in sells if t.profit_pct > 0]
    win_rate = len(wins) / len(sells) if sells else 0.0

    # 平均盈利/亏损
    profits = [t.profit_pct for t in sells if t.profit_pct > 0]
    losses = [t.profit_pct for t in sells if t.profit_pct <= 0]
    avg_profit = float(np.mean(profits)) if profits else 0.0
    avg_loss = float(np.mean(losses)) if losses else 0.0

    # 收益/回撤比
    ret_dd_ratio = total_return / max_dd if max_dd > 0 else 0.0

    return {
        "initial_capital": config.initial_capital,
        "final_nav": round(end_nav, 2),
        "total_return": round(float(total_return), 4),
        "annualized_return": round(float(annualized), 4),
        "max_drawdown": round(float(max_dd), 4),
        "win_rate": round(float(win_rate), 4),
        "trade_count": len(trades),
        "buy_count": len([t for t in trades if t.side == "BUY"]),
        "sell_count": len(sells),
        "avg_profit": round(float(avg_profit), 4),
        "avg_loss": round(float(avg_loss), 4),
        "ret_dd_ratio": round(float(ret_dd_ratio), 4),
        "position_days": len(nav_history),
        "empty_days": sum(1 for n in nav_history if n["position_count"] == 0),
    }


def _compute_monthly_returns(nav_history: list[dict[str, Any]]) -> dict[str, float]:
    """按月计算收益率。"""
    monthly_nav: dict[str, float] = {}
    for n in nav_history:
        month = n["date"][:7]
        monthly_nav[month] = n["nav"]

    monthly_returns: dict[str, float] = {}
    months = sorted(monthly_nav)
    for i, month in enumerate(months):
        if i == 0:
            continue
        prev_nav = monthly_nav[months[i - 1]]
        cur_nav = monthly_nav[month]
        if prev_nav > 0:
            monthly_returns[month] = round(cur_nav / prev_nav - 1.0, 4)
    return monthly_returns


def _compute_industry_rotation(
    daily_selections: list[DailySelection],
) -> list[dict[str, Any]]:
    """按月统计强行业出现天数，识别主线轮动。"""
    month_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for sel in daily_selections:
        if sel.market_state in ("熊市", "震荡偏弱"):
            continue
        month = sel.trade_date[:7]
        for pick in sel.selections:
            month_counts[month][pick["industry"]] += 1

    result: list[dict[str, Any]] = []
    for month in sorted(month_counts):
        top = sorted(month_counts[month].items(), key=lambda x: -x[1])[:5]
        result.append(
            {
                "month": month,
                "top_industries": [{"industry": k, "days": v} for k, v in top],
            }
        )
    return result
