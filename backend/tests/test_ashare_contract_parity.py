"""A 股交易规则契约层测试（数量口径 + 卖出定价 + 时段）。

**与 ``test_rule_parity.py`` 的分工**：那个是仓内三处（matcher / market_rules /
回测引擎）的费用与数量平价；本文件测的是**执行侧**（QMT 止损执行器 / 真单链路）
与交易所规则的一致性，含两条来自真账户实录的事故回归：

* **2026-09-08 数量**：600 股 × 33% 意图 199 股被地板取整成 100（一半），
  模型下一轮对不上账 → 改「最近整手取整」。
* **2026-09-21 定价**：止损触发时报跌停价属越界申报 → 柜台废单
  （当日 002074 按跌停价报的 42 笔真单全废、0 成交，并形成
  「触发→废单→重布防→再触发」每 2 分钟一笔的死循环）。

两条都不是理论推演，是花真单换来的口径，故在测试里固化。
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from backend.services.live_trading.services.lot_rules import (
    aggressive_sell_price,
    align_sell_quantity,
    in_continuous_auction,
    protect_sell_price,
    resolve_board,
)

_SH = ZoneInfo("Asia/Shanghai")
_TRADE_DATE = date(2026, 9, 23)


def _at(hhmm: str) -> datetime:
    hh, mm = (int(x) for x in hhmm.split(":"))
    return datetime(2026, 9, 23, hh, mm, tzinfo=_SH)


# ---------------------------------------------------------------------------
# 1. 卖出定价 —— 2026-09-21 事故回归（核心）
# ---------------------------------------------------------------------------
class TestAggressiveSellPrice:
    """``aggressive_sell_price`` = max(跌停价, 现价 × 0.99)：可成交且合法的最激进报价。"""

    def test_incident_20260921_replay(self) -> None:
        """002074 实录重放：昨收 26.14、市价 26.26。

        当时的实现报跌停价 23.53（比市价低 10.4%）→ 42 笔真单全 rejected。
        正确报价是 26.26 × 0.99 = 25.9974 → HALF_UP 到分 = 26.00。
        """
        floor = protect_sell_price("002074.SZ", 26.14, trade_date=_TRADE_DATE)
        assert floor == 23.53, "跌停价金样（与本仓 compute_limits 同源）"

        quote = aggressive_sell_price("002074.SZ", 26.14, 26.26, trade_date=_TRADE_DATE)
        assert quote == 26.00, f"应报现价 −1%，实际 {quote}"
        assert quote > floor, "报价绝不可等于/低于跌停价（越界申报 → 废单）"

    def test_near_limit_down_clamps_to_floor(self) -> None:
        """贴近跌停时，现价 × 0.99 会跌破跌停价 → 夹取到跌停价（此时基准价贴近跌停价，合法）。"""
        pre_close = 26.14
        floor = protect_sell_price("002074.SZ", pre_close, trade_date=_TRADE_DATE)
        assert floor is not None
        # 市价 23.60（贴着跌停价 23.53）→ 23.60×0.99 = 23.364 → 低于跌停价 → 夹到 23.53
        quote = aggressive_sell_price(
            "002074.SZ", pre_close, 23.60, trade_date=_TRADE_DATE
        )
        assert quote == floor == 23.53

    def test_never_falls_back_to_floor_when_price_missing(self) -> None:
        """现价取不到 → **绝不**退回跌停价（那正是那 42 笔废单的报价）。"""
        for bad in (None, 0, -1.0, float("nan"), float("inf"), float("-inf")):
            assert (
                aggressive_sell_price("002074.SZ", 26.14, bad, trade_date=_TRADE_DATE)
                is None
            ), f"现价 {bad!r} 应 fail-closed（None），而非退回跌停价"

    def test_bridge_floor_overrides_recompute(self) -> None:
        """桥给的 ``DownStopPrice`` 是权威值（含 ST/板别/日期口径），优先于本地重算。"""
        # 故意给一个与本地重算不同的 floor，验证注入生效
        quote = aggressive_sell_price(
            "600036.SH", 26.14, 26.26, trade_date=_TRADE_DATE, floor=25.00
        )
        assert quote == 26.00, "26.00 > 25.00，取报价"

        quote = aggressive_sell_price(
            "600036.SH", 26.14, 26.26, trade_date=_TRADE_DATE, floor=26.50
        )
        assert quote == 26.50, "报价低于注入下限时夹取到下限"

    def test_non_finite_pre_close_yields_none(self) -> None:
        """昨收非法（NaN/Inf）不得进报价链 —— Inf 会让 quantize 抛，NaN 会报出 NaN 单。"""
        for bad in (None, 0, float("nan"), float("inf")):
            assert (
                aggressive_sell_price("600036.SH", bad, 10.0, trade_date=_TRADE_DATE)
                is None
            )

    @pytest.mark.parametrize(
        "symbol,pre_close,ref,expected",
        [
            ("600036.SH", 10.00, 10.00, 9.90),  # 主板 10%
            ("300750.SZ", 10.00, 10.00, 9.90),  # 创业板 20%，报价仍按现价 −1%
            ("688596.SH", 10.00, 10.00, 9.90),  # 科创板
            ("920950.BJ", 10.00, 10.00, 9.90),  # 北交所 30%
        ],
    )
    def test_quote_is_price_minus_one_pct_all_boards(
        self, symbol: str, pre_close: float, ref: float, expected: float
    ) -> None:
        """报价口径与板别无关：一律「现价 −1%」（板别只影响跌停价的夹取下限）。"""
        assert (
            aggressive_sell_price(symbol, pre_close, ref, trade_date=_TRADE_DATE)
            == expected
        )


class TestProtectSellPrice:
    """``protect_sell_price`` = 硬下限（跌停价）。**不是**可报价，只用于夹取。"""

    @pytest.mark.parametrize(
        "symbol,pre_close,expected_floor",
        [
            ("600036.SH", 10.00, 9.00),  # 主板 −10%
            ("300750.SZ", 10.00, 8.00),  # 创业板 −20%
            ("688596.SH", 10.00, 8.00),  # 科创板 −20%
            ("920950.BJ", 10.00, 7.00),  # 北交所 −30%
        ],
    )
    def test_floor_per_board(
        self, symbol: str, pre_close: float, expected_floor: float
    ) -> None:
        assert protect_sell_price(symbol, pre_close, trade_date=_TRADE_DATE) == (
            expected_floor
        )

    def test_st_main_board_5pct_before_relax(self) -> None:
        """ST 主板 2026-07-06 前为 −5%；该日起放宽到 −10%（与板别同幅）。"""
        before = protect_sell_price(
            "600036.SH", 10.00, is_st=True, trade_date=date(2026, 7, 3)
        )
        after = protect_sell_price(
            "600036.SH", 10.00, is_st=True, trade_date=date(2026, 7, 6)
        )
        assert (before, after) == (9.50, 9.00)

    def test_missing_pre_close_yields_none(self) -> None:
        assert protect_sell_price("600036.SH", None, trade_date=_TRADE_DATE) is None


# ---------------------------------------------------------------------------
# 2. 卖出数量 —— 2026-09-08 事故回归
# ---------------------------------------------------------------------------
class TestSellQuantityRounding:
    """主板/创业板按**最近整手**取整（恰半手向下），碎股一次性全清。

    2026-09-08 实录：600 股 × 33% 意图 199 股被地板取整成 100（一半），
    模型下一轮对不上账。
    """

    @pytest.mark.parametrize(
        "want,expected",
        [
            (199, 200),  # ← 事故值：地板取整给 100（一半），最近整手给 200
            (151, 200),  # 过半手 → 进位
            (150, 100),  # 恰半手 → 向下（不放大意图）
            (149, 100),  # 未过半手 → 舍去
            (250, 200),  # 恰半手 → 向下
            (251, 300),  # 过半手 → 进位
            (246, 200),
            (100, 100),
            (1000, 1000),
        ],
    )
    def test_main_board_nearest_lot(self, want: int, expected: int) -> None:
        qty, _note = align_sell_quantity("600036.SH", want, 100000)
        assert qty == expected, f"意图 {want} 股应报 {expected} 股"

    def test_odd_lot_swept_when_remainder_unsellable(self) -> None:
        """剩余会是碎股（< 1 手）→ 一次性全清，防之后卖不掉。"""
        qty, note = align_sell_quantity("600036.SH", 700, 750)
        assert qty == 750, "卖 700 剩 50 碎股卖不掉，应一次清 750"
        assert "碎股" in note or "全" in note

    def test_full_position_sell_keeps_odd_lot(self) -> None:
        """全量卖出（意图 == 可用）允许碎股 —— 柜台只在部分卖出非整手时拒单。"""
        qty, note = align_sell_quantity("600036.SH", 0, 246)
        assert (qty, note) == (246, "")

    def test_star_lifts_to_min_not_liquidates(self) -> None:
        """科创板部分卖出不足 200 股 → **抬到 200**，而非清仓。

        旧实现降级为全量卖出：持有 1000 股、意图 180 股时会卖 1000 股（5.5 倍超卖）。
        """
        qty, _note = align_sell_quantity("688596.SH", 180, 1000)
        assert qty == 200, "应抬到科创板最小申报量 200，而不是卖掉全部 1000"

    def test_star_partial_at_or_above_min_kept(self) -> None:
        assert align_sell_quantity("688596.SH", 250, 1000)[0] == 250
        assert align_sell_quantity("688596.SH", 200, 1000)[0] == 200

    def test_bj_min_lot(self) -> None:
        """北交所最低 100 股、1 股递增。"""
        assert align_sell_quantity("920950.BJ", 30, 1000)[0] == 100
        assert align_sell_quantity("920950.BJ", 137, 1000)[0] == 137

    def test_avail_below_min_lot_liquidates(self) -> None:
        """可用量本身不足最小申报量 → 只能一次性全卖。"""
        assert align_sell_quantity("600036.SH", 50, 50)[0] == 50
        assert align_sell_quantity("688596.SH", 150, 150)[0] == 150

    def test_can_use_zero_returns_zero(self) -> None:
        qty, note = align_sell_quantity("600036.SH", 100, 0)
        assert qty == 0.0
        assert note, "可用量为 0 必须给出原因（T+1 锁定/挂单占用）"

    def test_never_exceeds_available(self) -> None:
        """任何路径都不得报出超过柜台可用量的数量。"""
        for sym in ("600036.SH", "300750.SZ", "688596.SH", "920950.BJ"):
            for want in (0, 1, 99, 150, 199, 5000):
                qty, _ = align_sell_quantity(sym, want, 300)
                assert 0 <= qty <= 300, f"{sym} want={want} → {qty} 越界"


# ---------------------------------------------------------------------------
# 3. 交易时段
# ---------------------------------------------------------------------------
class TestTradingSessions:
    """连续竞价窗口 —— **明确排除 14:57–15:00 收盘集合竞价**。"""

    @pytest.mark.parametrize(
        "hhmm,expected",
        [
            ("09:29", False),
            ("09:30", True),
            ("11:29", True),
            ("11:30", False),  # 上午收盘（上界开区间）
            ("12:59", False),
            ("13:00", True),
            ("14:56", True),
            ("14:57", False),  # ← 收盘集合竞价开始，不可撤单，语义不同
            ("14:59", False),
            ("15:00", False),
        ],
    )
    def test_boundaries(self, hhmm: str, expected: bool) -> None:
        assert in_continuous_auction(_at(hhmm)) is expected

    def test_weekend_is_closed(self) -> None:
        sat = datetime(2026, 9, 26, 10, 0, tzinfo=_SH)  # 周六
        assert in_continuous_auction(sat) is False

    def test_naive_datetime_treated_as_shanghai(self) -> None:
        """naive 时间按上海墙钟解释（宿主可能跑在 JST，不得用宿主本地钟）。"""
        assert in_continuous_auction(datetime(2026, 9, 23, 10, 0)) is True  # noqa: DTZ001 — naive 正是被测对象


# ---------------------------------------------------------------------------
# 4. 板别判定
# ---------------------------------------------------------------------------
class TestBoardResolution:
    @pytest.mark.parametrize(
        "symbol,expected",
        [
            ("600036.SH", "MAIN"),
            ("601398.SH", "MAIN"),
            ("603213.SH", "MAIN"),
            ("605499.SH", "MAIN"),
            ("000001.SZ", "MAIN"),
            ("001979.SZ", "MAIN"),
            ("002074.SZ", "MAIN"),
            ("003816.SZ", "MAIN"),
            ("300750.SZ", "GEM"),
            ("301001.SZ", "GEM"),
            ("302132.SZ", "GEM"),
            ("688596.SH", "STAR"),
            ("689009.SH", "STAR"),
            ("920950.BJ", "BJ"),
            ("430047.BJ", "BJ"),
            ("835185.BJ", "BJ"),
        ],
    )
    def test_boards(self, symbol: str, expected: str) -> None:
        assert resolve_board(symbol) == expected, symbol

    def test_prefix_and_bare_forms(self) -> None:
        assert resolve_board("SH600036") == "MAIN"
        assert resolve_board("600036") == "MAIN"
