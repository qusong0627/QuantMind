"""账户概览盘中实时口径覆盖（account_live_overlay）单元测试。

覆盖：全量取价重算、部分覆盖（混合口径 + uncovered 标注）、零覆盖/异常/非
tdx_bridge/开关关闭 → 原样返回（绝不抛）、入参不被修改（immutability）。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _contract() -> dict:
    positions = [
        {
            "name": "",
            "price": 29.5,
            "symbol": "002074.SZ",
            "volume": 100,
            "cost_price": 21.07,
            "market_value": 2950.0,
            "available_volume": 100,
        },
        {
            "name": "",
            "price": 4.62,
            "symbol": "601000.SH",
            "volume": 1700,
            "cost_price": 4.541,
            "market_value": 7854.0,
            "available_volume": 1700,
        },
        {
            "name": "",
            "price": 11.21,
            "symbol": "601857.SH",
            "volume": 700,
            "cost_price": 11.043,
            "market_value": 7847.0,
            "available_volume": 700,
        },
        {
            "name": "",
            "price": 13.87,
            "symbol": "603213.SH",
            "volume": 700,
            "cost_price": 13.594,
            "market_value": 9709.0,
            "available_volume": 700,
        },
    ]
    return {
        "account_source": "tdx_bridge",
        "positions": positions,
        "position_count": 4,
        "market_value": 28360.0,
        "total_asset": 917952.0,
        "cash": 889592.0,
        "available_cash": 889592.0,
        "floating_pnl": 1287.40,
        "floating_pnl_raw": 1287.40,
        "realized_pnl": -1954.62,
        "cumulative_pnl": -667.22,
        "total_pnl": -667.22,
        "today_pnl": 316.0,
        "daily_pnl": 316.0,
        "monthly_pnl": 0.0,
        "initial_equity": 918619.22,
        "day_open_equity": 917636.0,
        "month_open_equity": 0.0,
        "baseline": {
            "initial_equity": 918619.22,
            "day_open_equity": 917636.0,
            "month_open_equity": 0.0,
        },
    }


_LIVE = {"002074.SZ": 29.9, "601000.SH": 4.66, "601857.SH": 11.3, "603213.SH": 14.0}


def _fetcher(prices):
    async def _f(codes):
        return {k: v for k, v in prices.items() if k in codes}

    return _f


@pytest.mark.asyncio
async def test_full_coverage_recomputes_all_bases():
    from backend.services.live_trading.services.account_live_overlay import (
        overlay_account_live_prices,
    )

    original = _contract()
    out = await overlay_account_live_prices(original, quote_fetcher=_fetcher(_LIVE))

    # 逐仓：实时价与市值替换
    assert out["positions"][0]["price"] == 29.9
    assert out["positions"][0]["market_value"] == 2990.0
    # 市值 = 2990 + 7922 + 7910 + 9800
    assert out["market_value"] == pytest.approx(28622.0)
    # 总资产 = 旧总资产 + 市值差（262）
    assert out["total_asset"] == pytest.approx(918214.0)
    # 浮盈 = Σ(现价−成本)×量
    assert out["floating_pnl"] == pytest.approx(1549.4)
    assert out["floating_pnl_raw"] == pytest.approx(1549.4)
    # 今日 = 总资产 − 日开；累计 = 总资产 − 初始；已实现 = 累计 − 浮盈
    assert out["today_pnl"] == pytest.approx(578.0)
    assert out["cumulative_pnl"] == pytest.approx(-405.22)
    assert out["realized_pnl"] == pytest.approx(-1954.62)
    assert out["total_pnl"] == pytest.approx(-405.22)
    assert out["total_return_pct"] == pytest.approx(-405.22 / 918619.22 * 100.0)
    assert out["daily_return_pct"] == pytest.approx(578.0 / 917636.0 * 100.0)
    # 覆盖度标注
    ov = out["quote_overlay"]
    assert ov["source"] == "tdx_bridge_live" and ov["covered"] == 4 and ov["total"] == 4
    assert "uncovered" not in ov
    # 入参不可变：原字典仍是昨收口径
    assert original["floating_pnl"] == 1287.40
    assert original["positions"][0]["price"] == 29.5
    assert "quote_overlay" not in original


@pytest.mark.asyncio
async def test_partial_coverage_mixed_basis_with_annotation():
    from backend.services.live_trading.services.account_live_overlay import (
        overlay_account_live_prices,
    )

    out = await overlay_account_live_prices(
        _contract(), quote_fetcher=_fetcher({"002074.SZ": 29.9})
    )
    ov = out["quote_overlay"]
    assert ov["covered"] == 1 and ov["total"] == 4
    assert set(ov["uncovered"]) == {"601000.SH", "601857.SH", "603213.SH"}
    # 市值 = 2990 + 7854 + 7847 + 9709（未覆盖仓维持快照价）
    assert out["market_value"] == pytest.approx(28400.0)
    # 浮盈：covered 仓用实时价，未覆盖仓用快照价
    assert out["floating_pnl"] == pytest.approx(1327.4)
    assert out["total_asset"] == pytest.approx(917992.0)


@pytest.mark.asyncio
async def test_no_coverage_or_failure_returns_original():
    from backend.services.live_trading.services.account_live_overlay import (
        overlay_account_live_prices,
    )

    original = _contract()
    out = await overlay_account_live_prices(original, quote_fetcher=_fetcher({}))
    assert out is original

    async def _boom(codes):
        raise RuntimeError("bridge down")

    out2 = await overlay_account_live_prices(original, quote_fetcher=_boom)
    assert out2 is original


@pytest.mark.asyncio
async def test_non_tdx_source_and_disabled_env_untouched(monkeypatch):
    from backend.services.live_trading.services.account_live_overlay import (
        overlay_account_live_prices,
    )

    qmt = _contract()
    qmt["account_source"] = "qmt_exec"
    out = await overlay_account_live_prices(qmt, quote_fetcher=_fetcher(_LIVE))
    assert out is qmt

    monkeypatch.setenv("QM_ACCOUNT_LIVE_OVERLAY_ENABLED", "false")
    tdx = _contract()
    out2 = await overlay_account_live_prices(tdx, quote_fetcher=_fetcher(_LIVE))
    assert out2 is tdx
