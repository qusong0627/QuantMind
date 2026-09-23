"""影子代价账·市场侧取数（P1.6）——把留痕行接上真实行情，产出 `PriceInput`。

分工：`risk/ghost_pricing.py` 是纯口径（怎么算、什么状态），本模块是**取数**
（去哪儿拿、拿哪一天、拿不到算什么）。分开的好处是定价口径能在没有行情的地方
单测，而这里的每条数据来源都能单独解释。

为什么两个数据源（**不能只用一套**）
------------------------------------
* **算收益用前复权**（`1_kline_data/daily_forward`）。若用不复权，除权日会在价格上
  留下一个假跌 —— 那天的收益被凭空扣掉几个点，且**只在恰好跨除权日的样本上发生**，
  报告里表现为"某条规则偶尔花大钱"，根本查不出来。
* **判可成交性用不复权**（`LocalMarketData`，见其模块头）。涨跌停必须与当日真实
  盘口同口径：前复权价是被回溯改写过的历史价，拿它去比 `compute_limits` 算出来的
  涨停价，一字板会判错。

故这两个口径**各司其职**，不是冗余。**绝不要**用 `daily_backward`（后复权）：它的
复权比率逐日平滑漂移且会下降，累计因子不可能如此（见 memory
`daily-backward-defective-use-qfq`）。

取数口径
--------
* 日期一律 **YYYYMMDD 整数**做键（与 parquet 分区名、`_read_partitioned` 同口径）；
  对外暴露 `YYYY-MM-DD` 字符串（与 `ghost_pricing` 同口径）。
* 截面读法走 `hub._read_partitioned`（按 `dt=` 精确拼路径），**不要**用
  `hub.query("... FROM qdb_daily_forward WHERE dt IN ...")`：parquet 文件内自带
  `dt` 列，会遮蔽 hive 分区列让谓词失效，每个批次退化成全扫 2596 个文件
  （见 memory `quantdb-duckdb-hive-dt-shadow`）。
* 基准是**全市场等权、同窗口形状**：与标的用同一个 `entry_open → exit_close`。
  形状不同（比如基准用收盘→收盘）会把隔夜跳空算进超额，那部分是市场收益、
  不是这条规则的代价。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from typing import Any

import pandas as pd

from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub
from backend.services.simulation.services.local_market_data import (
    LocalMarketData,
    get_local_market_data,
)
from backend.services.simulation.services.market_rules import Market, normalize_market
from backend.shared.risk.ghost import GhostRow, ghost_id
from backend.shared.risk.ghost_pricing import (
    HORIZONS,
    PriceInput,
    cross_section_mean,
    entry_unfillable_reason,
    horizon_days,
    next_trading_day,
    window_return,
)
from backend.shared.stock_utils import StockCodeUtil

logger = logging.getLogger(__name__)

#: 前复权日线（CN）在 hub 数据目录下的相对路径
_QFQ_REL = "1_kline_data/daily_forward"

#: 一次读进内存的日期上限（防手滑传全历史把内存打满）
MAX_PANEL_DATES = 400


def to_dt(day: str) -> int | None:
    """`YYYY-MM-DD` → `YYYYMMDD` 整数；脏值 None（不猜）。"""
    s = str(day or "").strip().replace("-", "")
    return int(s) if len(s) == 8 and s.isdigit() else None


def from_dt(dt: int) -> str:
    """`YYYYMMDD` → `YYYY-MM-DD`。"""
    s = str(int(dt))
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


def _as_date(day: str) -> date | None:
    dt = to_dt(day)
    if dt is None:
        return None
    return date(dt // 10000, dt // 100 % 100, dt % 100)


@dataclass(frozen=True)
class PanelBar:
    """前复权截面里的一格（开/收/量；量只为剔除停牌样本）。"""

    open: float | None
    close: float | None
    volume: float | None

    @property
    def traded(self) -> bool:
        """当日有没有真的成交过（停牌样本不进基准，否则会给基准灌 0 收益）。"""
        return bool(self.volume and self.volume > 0)


@dataclass(frozen=True)
class Panel:
    """一批交易日的前复权截面（键 = (后缀式 symbol, YYYYMMDD)）。"""

    bars: Mapping[tuple[str, int], PanelBar]

    def get(self, symbol: str, dt: int | None) -> PanelBar | None:
        if dt is None:
            return None
        return self.bars.get((str(symbol or ""), int(dt)))

    @property
    def symbols(self) -> set[str]:
        return {s for s, _ in self.bars}


class GhostMarket:
    """影子账的市场侧取数器（日历 / 前复权截面 / 可成交性）。"""

    def __init__(
        self,
        *,
        market: Market | str = Market.CN,
        hub: QuantDBDataHub | None = None,
        lmd: LocalMarketData | None = None,
    ) -> None:
        self.market = normalize_market(market)
        if self.market is not Market.CN:
            raise ValueError(
                "影子代价账当前只支持 A 股：可成交性判定（涨跌停/ST/停牌）是 CN 口径，"
                "其它市场会得到看似有数、其实口径错的结果"
            )
        self._hub = hub
        self._lmd = lmd
        self._calendar: tuple[str, ...] | None = None
        self._panel_cache: dict[tuple[int, ...], Panel] = {}

    # ── 依赖（惰性：单测可注入假实现，不必连数据平台）────────────────
    @property
    def hub(self) -> QuantDBDataHub:
        if self._hub is None:
            self._hub = QuantDBDataHub.get_instance()
        return self._hub

    @property
    def lmd(self) -> LocalMarketData:
        if self._lmd is None:
            self._lmd = get_local_market_data(self.market)
        return self._lmd

    # ── 交易日历 ────────────────────────────────────────────────────
    def trading_days(self) -> tuple[str, ...]:
        """升序交易日（YYYY-MM-DD）。只含 IsTradingDay=1 的行。"""
        if self._calendar is not None:
            return self._calendar
        df = self.hub.fetch_calendar()
        if df is None or df.empty:
            raise RuntimeError(
                "交易日历读不到（2_base_sector/trading_calendar 缺失）——"
                "没有日历就推不出入场日，代价一律算不出来"
            )
        col = next(
            (
                c
                for c in ("TradingDate", "trade_date", "date", "time", "cal_date")
                if c in df.columns
            ),
            None,
        )
        if col is None:
            raise RuntimeError(f"交易日历没有可识别的日期列：{list(df.columns)}")
        if "IsTradingDay" in df.columns:
            df = df[
                pd.to_numeric(df["IsTradingDay"], errors="coerce").fillna(0).astype(int) == 1
            ]
        days = sorted(
            {
                s
                for s in (str(v).strip().replace("-", "") for v in df[col])
                if len(s) == 8 and s.isdigit()
            }
        )
        self._calendar = tuple(from_dt(int(s)) for s in days)
        return self._calendar

    def entry_day(self, block_date: str) -> str | None:
        """拦下那天的**下一个交易日**（次开入场；None = 日历还没走到）。"""
        return next_trading_day(block_date, self.trading_days())

    def horizon_exits(
        self, entry_day: str, horizons: Iterable[int] = HORIZONS
    ) -> dict[int, str | None]:
        return horizon_days(entry_day, self.trading_days(), horizons)

    # ── 前复权截面 ──────────────────────────────────────────────────
    def panel(self, days: Iterable[str]) -> Panel:
        """读一批交易日的前复权开/收/量（按日期集合缓存；同一批只读一次盘）。"""
        dts = sorted({d for d in (to_dt(x) for x in days) if d is not None})
        if not dts:
            return Panel({})
        if len(dts) > MAX_PANEL_DATES:
            raise ValueError(
                f"一次要读 {len(dts)} 个交易日（>{MAX_PANEL_DATES}）——"
                "影子账按窗口取数，不该出现全历史；检查日历推窗口的逻辑"
            )
        key = tuple(dts)
        cached = self._panel_cache.get(key)
        if cached is not None:
            return cached
        panel = self._read_panel(dts)
        self._panel_cache[key] = panel
        return panel

    def _read_panel(self, dts: Sequence[int]) -> Panel:
        df = self.hub._read_partitioned(  # noqa: SLF001 - 精确分区读，见模块头
            _QFQ_REL, [str(d) for d in dts], cols="symbol, time, open, close, volume"
        )
        if df is None or df.empty:
            logger.warning("前复权截面为空（读过日期 %s）", ",".join(str(d) for d in dts))
            return Panel({})
        # 日期键取 `time` 而非返回的 `dt` 列：hive 分区列与文件内 dt 同名，
        # 谁优先由 DuckDB 决定；`time` 没有这个歧义（两者取值本应一致）。
        ts = pd.to_datetime(df["time"], errors="coerce")
        df, ts = df[ts.notna()], ts[ts.notna()]
        bars: dict[tuple[str, int], PanelBar] = {}
        for sym, t, o, c, v in zip(
            df["symbol"].astype(str),
            ts.dt.strftime("%Y%m%d").astype(str),
            pd.to_numeric(df["open"], errors="coerce"),
            pd.to_numeric(df["close"], errors="coerce"),
            pd.to_numeric(df["volume"], errors="coerce"),
            strict=True,
        ):
            bars[(sym, int(t))] = PanelBar(
                open=float(o) if pd.notna(o) else None,
                close=float(c) if pd.notna(c) else None,
                volume=float(v) if pd.notna(v) else None,
            )
        return Panel(bars)

    # ── 基准（全市场等权，同窗口形状）───────────────────────────────
    def bench_returns(
        self, panel: Panel, pairs: Iterable[tuple[str, str]]
    ) -> dict[tuple[str, str], float | None]:
        """每 (入场日, 出场日) 的等权基准收益。

        同窗口形状：每只成分股都按 `入场日开盘 → 出场日收盘` 算，与标的走同一个
        `window_return`。停牌（量=0）成分剔除——qfq 价常是前值结转，留进来会把
        基准往 0 拉，虚高每条规则的"超额"。
        """
        out: dict[tuple[str, str], float | None] = {}
        syms = sorted(panel.symbols)
        for entry_day, exit_day in pairs:
            if (entry_day, exit_day) in out:
                continue
            e_dt, x_dt = to_dt(entry_day), to_dt(exit_day)
            if e_dt is None or x_dt is None:
                out[(entry_day, exit_day)] = None
                continue
            rets = []
            for sym in syms:
                eb, xb = panel.get(sym, e_dt), panel.get(sym, x_dt)
                if eb is None or xb is None or not eb.traded or not xb.traded:
                    continue
                rets.append(window_return(eb.open, xb.close))
            out[(entry_day, exit_day)] = cross_section_mean(rets)
        return out

    # ── 主入口：行 → PriceInput ────────────────────────────────────
    def price_inputs(
        self,
        rows: Sequence[GhostRow],
        *,
        horizons: Iterable[int] = HORIZONS,
    ) -> dict[str, PriceInput]:
        """给每行算出一个 `PriceInput`（键 = `ghost_id`）。

        输入里都是**已取好**的数：入场日/入场价/可成交性/各期出场价/各期基准。
        取不到的一律留 None 并给出原因——**不补 0、不沿用上一日**。
        """
        hs = tuple(int(h) for h in horizons)
        # ① 每行推窗口（纯日历运算，不碰盘）
        windows: dict[str, tuple[str | None, dict[int, str | None]]] = {}
        for r in rows:
            entry = self.entry_day(r.date)
            windows[ghost_id(r)] = (entry, self.horizon_exits(entry, hs) if entry else {})

        # ② 一次把要用的日期全读了（含基准所需的全市场截面）
        wanted: set[str] = set()
        for entry, exits in windows.values():
            if entry:
                wanted.add(entry)
            wanted.update(d for d in exits.values() if d)
        panel = self.panel(sorted(wanted))

        # ③ 基准按 (入场日, 出场日) 只算一次（同窗口全行共用）
        pairs = {
            (entry, exits[h])
            for entry, exits in windows.values()
            if entry
            for h in hs
            if exits.get(h)
        }
        bench = self.bench_returns(panel, sorted(pairs))

        # ④ 逐行装配
        out: dict[str, PriceInput] = {}
        for r in rows:
            gid = ghost_id(r)
            entry, exits = windows[gid]
            sym = StockCodeUtil.to_suffix(r.symbol)
            entry_bar = panel.get(sym, to_dt(entry)) if entry else None
            exit_px: dict[int, float | None] = {}
            for h in hs:
                ex_bar = panel.get(sym, to_dt(exits.get(h))) if exits.get(h) else None
                exit_px[h] = ex_bar.close if ex_bar else None
            inp = PriceInput(
                entry_day=entry,
                entry_px=entry_bar.open if entry_bar else None,
                exit_px=exit_px,
                bench={h: bench.get((entry, exits[h])) if entry and exits.get(h) else None for h in hs},
                entry_pending=entry is None,
            )
            out[gid] = self._with_tradability(inp, r, sym, entry)
        return out

    def _with_tradability(
        self, inp: PriceInput, row: GhostRow, sym: str, entry: str | None
    ) -> PriceInput:
        """入场日可否成交（一字板/停牌）——走**不复权**的 `LocalMarketData`。"""
        if entry is None:
            return inp
        day = _as_date(entry)
        bar = self.lmd.get_bar(sym, day) if day else None
        why = entry_unfillable_reason(
            row.side,
            open_px=bar.open if bar else None,
            limit_up=bar.limit_up if bar else None,
            limit_down=bar.limit_down if bar else None,
            volume=bar.volume if bar else None,
            has_bar=bar is not None,
        )
        if why is None:
            return inp
        return replace(inp, tradable=False, reason=why)

    # ── 体检 ────────────────────────────────────────────────────────
    def coverage(self, rows: Sequence[GhostRow]) -> dict[str, Any]:
        """取数体检（CLI 摘要用）：一眼看出是日历短了、盘没落，还是标的真没行情。"""
        inputs = self.price_inputs(rows)
        days = self.trading_days()
        dts = sorted({d for key in self._panel_cache for d in key})
        return {
            "calendar_days": len(days),
            "calendar_last": days[-1] if days else None,
            "rows": len(rows),
            "rows_with_entry_day": sum(1 for i in inputs.values() if i.entry_day),
            "rows_entry_pending": sum(1 for i in inputs.values() if i.entry_pending),
            "rows_with_entry_price": sum(1 for i in inputs.values() if i.entry_px is not None),
            "rows_untradable": sum(1 for i in inputs.values() if i.tradable is False),
            "panel_dates": [from_dt(d) for d in dts],
        }


__all__ = [
    "MAX_PANEL_DATES",
    "GhostMarket",
    "Panel",
    "PanelBar",
    "from_dt",
    "to_dt",
]
