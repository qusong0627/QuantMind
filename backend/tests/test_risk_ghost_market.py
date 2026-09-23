"""影子账市场侧取数（`GhostMarket`）——共用入口与标签窗口的口径守卫。

为什么值得单测
--------------
`price_inputs`（影子账：拦下的单要是放行会怎样）与 `price_batch`（决策记分卡：
模型说的话事后怎样）回答的是**同一个问题**，只是行的来源不同。两条路径一旦分叉，
影子账与记分卡会各出一套结论，而两边看起来都很正常（数都在、都能排序）——只有把
结果摆在一起才看得出。故第一条测试就是「两者逐字一致」。

第二条是**绝不前视**：形态标签吃的是入场日**之前**的收盘，入场日自己的收盘必须
不在里面（入场发生在开盘，当天收盘是决策之后的数）。把 `_days_before` 的右界从
`i` 写成 `i + 1` 就会让「接近60日高」因当天大涨而恒真——这类错在报表上只表现为
「标签区分度莫名很好」。

数据源全部注入假实现（`hub` / `lmd`），不连数据平台。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd
import pytest

from backend.scripts.risk_ghost_market import GhostMarket, PriceQuery
from backend.shared.risk.ghost import GhostRow

#: 假日历的起点（周一）
_START = date(2026, 8, 3)

SYMBOL_PREFIX = "SH600519"
SYMBOL_SUFFIX = "600519.SH"


def _calendar(n: int) -> list[str]:
    """n 个「工作日」（假日历：不排节假日，够用即可）。"""
    out: list[str] = []
    d = _START
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


@dataclass(frozen=True)
class _Bar:
    open: float
    close: float
    volume: float
    limit_up: float
    limit_down: float


class _FakeHub:
    """只实现 GhostMarket 用到的那两个方法（`fetch_calendar` / `_read_partitioned`）。"""

    def __init__(self, days: list[str], bars: dict[tuple[str, int], _Bar]) -> None:
        self.days = list(days)
        #: 前复权面板（假实现里直接改它来造缺数/一字板场景）
        self.bars = dict(bars)
        #: 每次读盘请求的日期（断言「lookback 真的进了取数窗口」）
        self.reads: list[list[str]] = []

    def fetch_calendar(self) -> pd.DataFrame:
        return pd.DataFrame({"TradingDate": self.days, "IsTradingDay": 1})

    def _read_partitioned(
        self, rel: str, dts: list[str], cols: str | None = None
    ) -> pd.DataFrame:
        self.reads.append(list(dts))
        wanted = {str(d) for d in dts}
        rows = [
            {
                "symbol": sym,
                "time": f"{dt // 10000}-{dt // 100 % 100:02d}-{dt % 100:02d}",
                "open": b.open,
                "close": b.close,
                "volume": b.volume,
            }
            for (sym, dt), b in sorted(self.bars.items())
            if str(dt) in wanted
        ]
        return pd.DataFrame(rows, columns=["symbol", "time", "open", "close", "volume"])


class _FakeLmd:
    def __init__(self, bars: dict[tuple[str, date], _Bar]) -> None:
        self._bars = dict(bars)

    def get_bar(self, symbol: str, trade_date: date) -> _Bar | None:
        return self._bars.get((symbol, trade_date))


def _mk(
    *,
    n_days: int = 90,
    lookback_from: int = 20,
    entry_close: float = 9999.0,
) -> tuple[GhostMarket, list[str], _FakeHub]:
    """造一个「日历 + 一只票的前复权序列」的取数器。

    入场日（`_START` 后第 21 个交易日）的收盘特意设成 `9999.0` 哨兵值：
    前视与否一眼可判（见模块头第二条）。
    """
    days = _calendar(n_days)
    bars: dict[tuple[str, int], _Bar] = {}
    entry_idx = lookback_from + 1  # 决策日 = 第 20 个，入场日 = 第 21 个
    for i, day in enumerate(days):
        dt = int(day.replace("-", ""))
        base = 100.0 + i
        bars[(SYMBOL_SUFFIX, dt)] = _Bar(
            open=base,
            close=entry_close if i == entry_idx else base + 1.0,
            volume=1e6,
            limit_up=base * 1.1,
            limit_down=base * 0.9,
        )
    hub = _FakeHub(days, bars)
    entry_day = days[entry_idx]
    # 可成交性走**不复权**那条链（lmd）；假实现只给入场日一根可成交的 bar
    lmd = _FakeLmd(
        {
            (SYMBOL_SUFFIX, date.fromisoformat(entry_day)): bars[
                (SYMBOL_SUFFIX, _dt(entry_day))
            ]
        }
    )
    market = GhostMarket(hub=hub, lmd=lmd)  # type: ignore[arg-type]
    return market, days, hub


def _dt(day: str) -> int:
    return int(day.replace("-", ""))


def _row(day: str, *, side: str = "buy") -> GhostRow:
    return GhostRow(
        date=day,
        rule_id="l1.position",
        kind="position",
        tenant="default",
        uid="10000001",
        symbol=SYMBOL_PREFIX,
        side=side,
        quantity=100.0,
        source="risk_gate",
        reason="test",
    )


def _query(day: str, *, key: str = "k1", side: str = "buy") -> PriceQuery:
    return PriceQuery(key=key, day=day, symbol=SYMBOL_PREFIX, side=side)


# ── 两条路径同口径 ─────────────────────────────────────────────────
def test_price_inputs_is_the_same_batch_as_price_batch():
    """影子账适配器与记分卡主入口必须**逐字同结果**（取数逻辑只此一份）。"""
    market, days, _hub = _mk()
    rows = [_row(days[20]), _row(days[19], side="sell")]
    queries = [
        PriceQuery(key=f"k{i}", day=r.date, symbol=r.symbol, side=r.side)
        for i, r in enumerate(rows)
    ]
    from_rows = market.price_inputs(rows, horizons=(1, 5))
    from_queries = market.price_batch(queries, horizons=(1, 5)).inputs
    assert list(from_queries) == ["k0", "k1"]
    # 影子账那侧的键是 ghost_id，逐值比内容（两侧按键序一致）
    assert list(from_rows.values()) == list(from_queries.values())
    assert from_rows, "影子账适配器一行都没出"


def test_tradability_follows_the_side_of_the_query():
    """同一根一字板，买入不可成交、卖出可成交——side 必须逐条取自问句本身。

    影子账的 side 藏在 `GhostRow` 里、记分卡的藏在行里，若取数时写死成 `buy`
    或漏传，跌停卖不掉的那些样本会被整批漏判。
    """
    market, days, hub = _mk()
    entry_day = days[21]
    sealed = _Bar(open=110.0, close=110.0, volume=1e6, limit_up=110.0, limit_down=99.0)
    entry_dt = _dt(entry_day)
    hub.bars[(SYMBOL_SUFFIX, entry_dt)] = sealed  # 前复权面板里也是一字板
    # lmd 用**不复权**那根：开盘即封涨停
    market._lmd = _FakeLmd(  # noqa: SLF001 - 测试要换掉注入的假实现
        {(SYMBOL_SUFFIX, date.fromisoformat(entry_day)): sealed}
    )

    buy = market.price_batch([_query(days[20], side="buy")], horizons=(1,))
    sell = market.price_batch([_query(days[20], side="sell")], horizons=(1,))
    assert buy.inputs["k1"].tradable is False
    assert "涨停" in buy.inputs["k1"].reason
    assert sell.inputs["k1"].tradable is None  # 未判过 = None，不是 True


# ── 标签窗口：绝不前视 ─────────────────────────────────────────────
def test_prior_closes_excludes_the_entry_day_itself():
    """入场日**自己**的收盘不得出现在窗口里（哨兵值 9999.0 必须缺席）。"""
    market, days, _hub = _mk()
    entry_day = days[21]
    batch = market.price_batch([_query(days[20])], horizons=(1,), lookback_days=60)
    closes = market.prior_closes(batch.panel, SYMBOL_PREFIX, entry_day, 60)
    assert closes, "窗口是空的 —— 取数没把 lookback 的日子读进来"
    assert 9999.0 not in closes, "入场日收盘进了标签窗口（前视）"
    assert len(closes) == 21, "应恰为入场日之前的全部交易日"


def test_lookback_days_widens_the_read_window():
    """`lookback_days=0` 时**不该**多读日期，且标签拿不到历史（静默空 = 明示空）。"""
    market, days, hub = _mk()
    entry_day = days[21]
    without = market.price_batch([_query(days[20])], horizons=(1,))
    assert all(len(r) <= 2 for r in hub.reads), "没要 lookback 却多读了日期"
    assert market.prior_closes(without.panel, SYMBOL_PREFIX, entry_day, 60) == ()

    with_lb = market.price_batch([_query(days[20])], horizons=(1,), lookback_days=60)
    assert market.prior_closes(with_lb.panel, SYMBOL_PREFIX, entry_day, 60)
    assert any(len(r) > 2 for r in hub.reads), "开了 lookback 却没进取数窗口"


def test_prior_closes_is_empty_when_the_days_are_unknown():
    """入场日不在日历上（挂历/脏值）→ 空元组，**不猜**。"""
    market, days, _hub = _mk()
    batch = market.price_batch([_query(days[20])], horizons=(1,), lookback_days=60)
    assert market.prior_closes(batch.panel, SYMBOL_PREFIX, None, 60) == ()
    assert market.prior_closes(batch.panel, SYMBOL_PREFIX, "1990-01-01", 60) == ()
    assert market.prior_closes(batch.panel, SYMBOL_PREFIX, days[21], 0) == ()


def test_prior_closes_keeps_order_and_skips_missing_bars():
    """升序、缺 bar 跳过（不沿用上一日）——下单标签前序列的顺序错了，涨跌幅会反号。"""
    market, days, hub = _mk()
    entry_day = days[21]
    # 抠掉入场日前第 3 天的 bar（模拟缺数）
    missing = days[18]
    hub.bars.pop((SYMBOL_SUFFIX, _dt(missing)))
    batch = market.price_batch([_query(days[20])], horizons=(1,), lookback_days=60)
    closes = market.prior_closes(batch.panel, SYMBOL_PREFIX, entry_day, 60)
    assert closes == tuple(sorted(closes)), "不是升序"
    assert len(closes) == 20, "缺 bar 的那天没有被跳过"


def test_first_calendar_day_has_no_history():
    """日历开头没有「之前」——空窗口而不是把最后一天绕回来（负索引会绕回）。"""
    market, days, _hub = _mk()
    batch = market.price_batch([_query(days[0])], horizons=(1,), lookback_days=60)
    # days[0] 的入场日是 days[1]，其之前恰有 1 天
    assert market.prior_closes(batch.panel, SYMBOL_PREFIX, days[1], 60) == (
        pytest.approx(101.0),
    )
