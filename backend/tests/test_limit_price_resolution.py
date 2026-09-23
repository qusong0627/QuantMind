"""逐笔限价（P1.2）的**单口径**解析测试。

背景：隔壁 LLM 的决策输出逐笔带 ``limit_px``（今日实录：卖报 38.95 贴 ref 39.34 下方、
买报 45.99 贴 45.53 上方，都是"贴着打保成交"）。QuantMind 的执行面原本把限价**算死**在
``ref × (1 ± max_slippage_pct)``，调用方无从表达自己的限价。

本文件钉住 :func:`lot_rules.resolve_limit_price` —— 真单边界（``real_mirror_service``）
与预检（``push_orders``）**共用同一个实现**，两侧不许各写一套带。

单边带语义（与既有派生公式逐字一致）：

* 买单：不得**高于** ``base × (1 + slip)``（更紧 = 报更低，允许；只是可能不成交）；
* 卖单：不得**低于** ``base × (1 - slip)``（更紧 = 报更高，允许）；
* 反方向（买更低/卖更高）在 ``sanity`` 以内放行 —— 那只是挂了远价，不是价格错了；
  超出 ``sanity`` 视为脏价格，fail-closed。
"""

from __future__ import annotations

import pytest

from backend.services.live_trading.services import lot_rules as lr


class TestDerivationWhenNoRequested:
    """无调用方限价 → 派生带值（与既有实现逐字一致，防行为漂移）。"""

    def test_buy_derives_base_plus_slip(self):
        # Arrange / Act
        price, problem = lr.resolve_limit_price("buy", 45.53, max_slip=0.02)

        # Assert
        assert problem == ""
        assert price == 46.44  # 45.53 × 1.02 = 46.4406 → HALF_UP 到分

    def test_sell_derives_base_minus_slip(self):
        # Arrange / Act
        price, problem = lr.resolve_limit_price("sell", 39.34, max_slip=0.02)

        # Assert
        assert problem == ""
        assert price == 38.55  # 39.34 × 0.98 = 38.5532

    def test_missing_base_is_fail_closed_for_both_sides(self):
        # Arrange / Act / Assert
        assert lr.resolve_limit_price("buy", None, max_slip=0.02) == (
            None,
            "no_reference_price",
        )
        assert lr.resolve_limit_price("sell", 0.0, max_slip=0.02) == (
            None,
            "no_reference_price",
        )


class TestRequestedWithinBand:
    """调用方限价在带内 → 采用（并按分 HALF_UP 取整）。"""

    def test_buy_inside_band_is_adopted(self):
        # Arrange：45.99 贴 ref 45.53 上方（+1.01% < 2%）
        # Act
        price, problem = lr.resolve_limit_price(
            "buy", 45.53, requested=45.99, max_slip=0.02
        )

        # Assert
        assert problem == ""
        assert price == 45.99

    def test_sell_inside_band_is_adopted(self):
        # Arrange / Act：38.95 贴 ref 39.34 下方（-0.99%）
        price, problem = lr.resolve_limit_price(
            "sell", 39.34, requested=38.95, max_slip=0.02
        )

        # Assert
        assert problem == ""
        assert price == 38.95

    def test_buy_tighter_than_band_is_allowed(self):
        """更紧 = 报得更低：只是可能不成交，不该被拒（拒了会扭转调用方意图）。"""
        # Arrange / Act
        price, problem = lr.resolve_limit_price(
            "buy", 100.0, requested=95.0, max_slip=0.02
        )

        # Assert
        assert problem == ""
        assert price == 95.0

    def test_sell_tighter_than_band_is_allowed(self):
        # Arrange / Act
        price, problem = lr.resolve_limit_price(
            "sell", 100.0, requested=105.0, max_slip=0.02
        )

        # Assert
        assert problem == ""
        assert price == 105.0

    def test_exact_band_edge_is_adopted(self):
        # Arrange：恰好等于派生带值（边界取闭区间，不因浮点差一档被拒）
        # Act
        buy, p1 = lr.resolve_limit_price("buy", 100.0, requested=102.0, max_slip=0.02)
        sell, p2 = lr.resolve_limit_price("sell", 100.0, requested=98.0, max_slip=0.02)

        # Assert
        assert (buy, p1) == (102.0, "")
        assert (sell, p2) == (98.0, "")

    def test_requested_is_rounded_half_up_to_cent(self):
        # Arrange / Act：45.999 → 46.00（不是银行家舍入的 46.0 截断）
        price, problem = lr.resolve_limit_price(
            "buy", 45.53, requested=45.999, max_slip=0.02
        )

        # Assert
        assert problem == ""
        assert price == 46.0


class TestRequestedOutsideBand:
    """越界方向一律拒（fail-closed），不回落到派生值 —— 静默改价即伪造回执。"""

    def test_buy_above_band_is_rejected(self):
        # Arrange / Act
        price, problem = lr.resolve_limit_price(
            "buy", 100.0, requested=102.5, max_slip=0.02
        )

        # Assert
        assert price is None
        assert problem == "limit_price_too_aggressive"

    def test_sell_below_band_is_rejected(self):
        # Arrange / Act
        price, problem = lr.resolve_limit_price(
            "sell", 100.0, requested=97.5, max_slip=0.02
        )

        # Assert
        assert price is None
        assert problem == "limit_price_too_aggressive"

    def test_sanity_floor_catches_far_prices_even_in_the_tight_direction(self):
        """买 79 = ref −21%：方向是"更紧"，但那是脏价格不是意愿 —— 拒。"""
        # Arrange / Act
        price, problem = lr.resolve_limit_price(
            "buy", 100.0, requested=79.0, max_slip=0.02, sanity=0.20
        )

        # Assert
        assert price is None
        assert problem == "limit_price_sanity"

    def test_sanity_allows_just_inside(self):
        # Arrange / Act：81 = ref −19%，未超 sanity
        price, problem = lr.resolve_limit_price(
            "buy", 100.0, requested=81.0, max_slip=0.02, sanity=0.20
        )

        # Assert
        assert problem == ""
        assert price == 81.0

    @pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
    def test_non_finite_or_non_positive_requested_is_rejected(self, bad):
        # Arrange / Act
        price, problem = lr.resolve_limit_price(
            "buy", 100.0, requested=bad, max_slip=0.02
        )

        # Assert
        assert price is None
        assert problem  # 具体词不钉死：NaN/0/负数各有各的写法，但都必须拒

    def test_missing_base_rejects_even_with_requested(self):
        """没有参考价就没有带 —— 无从判断 45.99 是不是"太高"，fail-closed。"""
        # Arrange / Act
        price, problem = lr.resolve_limit_price(
            "buy", None, requested=45.99, max_slip=0.02
        )

        # Assert
        assert price is None
        assert problem == "no_reference_price"


class TestSharedByBothSides:
    """真单边界与预检用同一函数 —— 源码守卫（换行不敏感）。"""

    def _flat(self, src: str) -> str:
        import re

        return re.sub(r"\s+", "", src)

    def test_mirror_submit_uses_the_shared_resolver(self):
        # Arrange
        from pathlib import Path

        src = Path(
            "backend/services/live_trading/services/real_mirror_service.py"
        ).read_text(encoding="utf-8")

        # Act / Assert：读 payload 的 limit_price 并过共享解析（而不是自己再写一套）
        assert "resolve_limit_price(" in self._flat(src)
        assert 'payload.get("limit_price")' in self._flat(src).replace("'", '"')

    def test_router_limit_check_uses_the_same_resolver(self):
        # Arrange
        from pathlib import Path

        src = Path("backend/services/api/routers/push_orders.py").read_text(
            encoding="utf-8"
        )

        # Act / Assert
        assert "resolve_limit_price(" in self._flat(src)

    def test_mirror_callers_can_carry_a_limit_into_the_payload(self):
        """``mirror_virtual_fill`` 必须能收下逐笔限价并带进队列载荷。"""
        # Arrange
        import inspect

        from backend.services.live_trading.services import real_mirror_service as m

        sig = inspect.signature(m.mirror_virtual_fill)

        # Act / Assert
        assert "limit_price" in sig.parameters
