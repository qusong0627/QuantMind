"""推送候选筛选（`TdxSignalPushService._screen_candidates`）的行为钉子。

这个函数是把模型选股推给通达信前的**最后一道人工可见的过滤**。它错一格，
用户看到的就是一份可以照着下单的清单——所以这里钉的不是"能跑"，而是三件
具体的事：

1. **退市股必须被剔除**。改动前这里是 ``"ST" in name.upper() or
   name.startswith("*")``——`退市海润` 两个条件都不命中，会被推出去；
2. **子串不算命中**。``华STAR科技`` 不含 ST 标记，改动前会被误杀（漏推一只
   正常票，用户看到的是"今天没信号"）；
3. **不丢行**。`picked + skipped` 必须覆盖全部输入：静默丢行比误判更坏，
   因为没人知道少了哪只。

判据本身（前缀表、退市标记、空名 fail-open）的单测在 `test_symbol_policy.py`；
这里只钉**本调用点确实在用同一个判据**。
"""

from __future__ import annotations

import pytest

from backend.services.live_trading.services.tdx_signal_push_service import (
    TdxSignalPushService,
)

_screen = TdxSignalPushService._screen_candidates


def _row(symbol: str, score: float = 1.0, **extra: object) -> dict:
    return {"symbol": symbol, "fusion_score": score, **extra}


class TestRiskyNamesAreDropped:
    @pytest.mark.parametrize("name", ["退市海润", "海润退"])
    def test_delisting_names_are_skipped(self, name: str) -> None:
        """**改动前会放行**：旧判据只看 ST 前缀/星号，退市整理期的票会被推给用户。"""
        picked, skipped = _screen(
            [_row("SH600401")],
            {"600401.SH": name},
            {"SH600401": 3.5},
        )
        assert picked == []
        assert skipped[0]["reason"] == "ST/退市"
        assert skipped[0]["name"] == name

    @pytest.mark.parametrize(
        "name", ["ST海航", "*ST三圣", "SST前锋", "S*ST生化", "ST 三圣"]
    )
    def test_st_forms_are_skipped(self, name: str) -> None:
        picked, skipped = _screen(
            [_row("SH600221")], {"600221.SH": name}, {"SH600221": 2.0}
        )
        assert picked == []
        assert skipped[0]["reason"] == "ST/退市"

    def test_substring_st_is_not_a_match(self) -> None:
        """``华STAR科技`` 不含 ST 标记——旧子串判据会误杀它，用户看到"今天没信号"。"""
        picked, skipped = _screen(
            [_row("SZ300001")], {"300001.SZ": "华STAR科技"}, {"SZ300001": 12.0}
        )
        assert skipped == []
        assert picked[0]["name"] == "华STAR科技"

    def test_missing_name_fails_open(self) -> None:
        """名称表拉不到（空串）→ 放行：不能因为名称表故障停掉整份推送。"""
        picked, _ = _screen([_row("SH600036")], {}, {"SH600036": 35.0})
        assert len(picked) == 1


class TestNoClosePriceIsSkippedFirst:
    def test_missing_close_is_skipped(self) -> None:
        picked, skipped = _screen([_row("SH600036")], {"600036.SH": "招商银行"}, {})
        assert picked == []
        assert skipped == [{"symbol": "600036.SH", "reason": "无收盘价"}]

    def test_price_check_wins_over_name_check(self) -> None:
        """两个判据都命中时的**优先序**：先记「无收盘价」。

        钉住它是因为两条 reason 指向完全不同的排查方向：无价是数据链路
        （同步/停牌），禁买是标的本身。谁在前决定了运维先查哪一头。
        """
        _, skipped = _screen([_row("SH600221")], {"600221.SH": "ST海航"}, {})
        assert skipped[0]["reason"] == "无收盘价"

    @pytest.mark.parametrize("close", [0.0, -1.0])
    def test_non_positive_close_is_skipped(self, close: float) -> None:
        _, skipped = _screen([_row("SH600036")], {}, {"SH600036": close})
        assert skipped[0]["reason"] == "无收盘价"


class TestOutputShape:
    def test_symbol_is_normalised_to_suffix(self) -> None:
        picked, _ = _screen([_row("SH600036")], {}, {"SH600036": 35.678})
        assert picked[0]["symbol"] == "600036.SH"

    def test_score_is_rounded_and_close_is_two_decimals(self) -> None:
        picked, _ = _screen([_row("SH600036", 1.23456789)], {}, {"SH600036": 35.678})
        assert picked[0]["score"] == 1.2346
        assert picked[0]["close"] == 35.68

    def test_side_defaults_to_buy_and_is_uppercased(self) -> None:
        picked, _ = _screen(
            [_row("SH600036"), _row("SZ000001", 2.0, signal_side="sell")],
            {},
            {"SH600036": 35.0, "SZ000001": 12.0},
        )
        assert [p["side"] for p in picked] == ["BUY", "SELL"]

    def test_position_and_order_are_preserved(self) -> None:
        """筛选不改排序、不改条数（排序是 `_pick_stocks` 的职责，不在这里重复）。"""
        rows = [_row("SH600036"), _row("SZ000001"), _row("SH600221")]
        picked, _ = _screen(
            rows, {"600221.SH": "ST海航"}, {"SH600036": 1.0, "SZ000001": 2.0}
        )
        assert [p["symbol"] for p in picked] == ["600036.SH", "000001.SZ"]


def test_every_input_row_is_accounted_for() -> None:
    """不丢行：`picked + skipped` 必须等于输入条数。

    静默丢行是本函数最坏的失效方式——清单少一只，没人会发现。
    """
    rows = [
        _row("SH600036"),
        _row("SH600221"),  # ST
        _row("SH600401"),  # 退市
        _row("SZ000002"),  # 无价
    ]
    picked, skipped = _screen(
        rows,
        {"600221.SH": "ST海航", "600401.SH": "退市海润"},
        {"SH600036": 35.0},
    )
    assert len(picked) + len(skipped) == len(rows)
    assert {p["symbol"] for p in picked} == {"600036.SH"}
