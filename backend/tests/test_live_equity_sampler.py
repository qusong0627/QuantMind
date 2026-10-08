"""实盘净值分钟采样器（live_equity_sampler）单元测试。

覆盖：采样时段纯函数边界、总账户行（桥实时/降级估算/资产≤0 跳过）、每 agent
分账行（桥价估值/无持仓=现金/全无价跳过）、同分钟去重、快照行 schema。
"""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from backend.services.live_trading.services import live_equity_sampler as sampler

_TZ = ZoneInfo("Asia/Shanghai")
_TRADING_NOW = datetime(2026, 10, 8, 10, 0, 30, tzinfo=_TZ)  # 周四 10:00 北京


def _write_ledger(path, agents):
    path.write_text(
        json.dumps({"version": 1, "agents": agents}, ensure_ascii=False),
        encoding="utf-8",
    )


def _ledger_agents():
    return {
        "deepseek-v4-flash": {
            "positions": {"600036.SH": {"volume": 100, "cost_price": 10.0}},
            "virtual_cash": 99000.0,
        },
        "glm-5.3-flash": {"positions": {}, "virtual_cash": 100000.0},
    }


def _read_rows(path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


# ── 时段纯函数 ──────────────────────────────────────────────────────


@pytest.mark.unit
def test_sample_window_boundaries():
    def at(hm):
        h, m = hm
        return datetime(2026, 10, 8, h, m, tzinfo=_TZ)

    assert sampler.in_sample_window(at((9, 25)))
    assert not sampler.in_sample_window(at((9, 24)))
    assert sampler.in_sample_window(at((11, 30)))
    assert not sampler.in_sample_window(at((11, 31)))  # 午休不出行
    assert not sampler.in_sample_window(at((12, 59)))
    assert sampler.in_sample_window(at((13, 0)))
    assert sampler.in_sample_window(at((15, 10)))
    assert not sampler.in_sample_window(at((15, 11)))


@pytest.mark.unit
def test_trading_day_fail_open_on_unknown(monkeypatch):
    import backend.shared.trading_calendar as cal

    monkeypatch.setattr(cal, "is_trading_day_xcal", lambda market, day: None)
    assert sampler.is_trading_day(_TRADING_NOW.date()) is True

    monkeypatch.setattr(cal, "is_trading_day_xcal", lambda market, day: False)
    assert sampler.is_trading_day(_TRADING_NOW.date()) is False


# ── 采样主流程 ──────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sample_once_writes_account_and_agent_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(sampler, "is_trading_day", lambda day: True)
    ledger = tmp_path / "live_ledger.json"
    equity = tmp_path / "live_equity.jsonl"
    _write_ledger(ledger, _ledger_agents())

    async def account():
        return 200000.0, 150000.0, False

    async def quotes(codes):
        assert "600036.SH" in codes
        return {"600036.SH": 12.0}

    report = await sampler.sample_once(
        _TRADING_NOW,
        account_fetcher=account,
        quote_fetcher=quotes,
        ledger_path=ledger,
        equity_path=equity,
    )
    assert report["written"] == 3
    rows = _read_rows(equity)
    by_agent = {r["agent"]: r for r in rows}
    account_row = by_agent[None]
    assert account_row["value"] == 200000.0
    assert account_row["asset"] == 200000.0 and account_row["cash"] == 150000.0
    assert account_row["key"] == "2026-10-08 10:00"
    assert account_row["date"] == "2026-10-08"
    assert account_row["ts"].startswith("2026-10-08T10:00:30")
    assert "estimated" not in account_row
    # flash：99000 现金 + 100×12 市值
    assert by_agent["deepseek-v4-flash"]["value"] == 100200.0
    # glm 无持仓：现金平线（曲线不中断）
    assert by_agent["glm-5.3-flash"]["value"] == 100000.0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sample_once_dedupes_same_minute(tmp_path, monkeypatch):
    monkeypatch.setattr(sampler, "is_trading_day", lambda day: True)
    ledger = tmp_path / "live_ledger.json"
    equity = tmp_path / "live_equity.jsonl"
    _write_ledger(ledger, _ledger_agents())

    async def account():
        return 200000.0, 150000.0, False

    async def quotes(codes):
        return {"600036.SH": 12.0}

    first = await sampler.sample_once(
        _TRADING_NOW,
        account_fetcher=account,
        quote_fetcher=quotes,
        ledger_path=ledger,
        equity_path=equity,
    )
    second = await sampler.sample_once(
        _TRADING_NOW,
        account_fetcher=account,
        quote_fetcher=quotes,
        ledger_path=ledger,
        equity_path=equity,
    )
    assert first["written"] == 3 and second["written"] == 0
    assert len(_read_rows(equity)) == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sample_once_bridge_down_uses_estimated_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(sampler, "is_trading_day", lambda day: True)
    ledger = tmp_path / "live_ledger.json"
    equity = tmp_path / "live_equity.jsonl"
    _write_ledger(ledger, {})

    async def account():
        raise RuntimeError("桥查询失败")

    async def degraded():
        return 123456.0, 100000.0, True

    report = await sampler.sample_once(
        _TRADING_NOW,
        account_fetcher=account,
        degraded_fetcher=degraded,
        ledger_path=ledger,
        equity_path=equity,
    )
    assert report["written"] == 1
    row = _read_rows(equity)[0]
    assert row["estimated"] is True and row["value"] == 123456.0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sample_once_guards_skip_bad_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(sampler, "is_trading_day", lambda day: True)
    ledger = tmp_path / "live_ledger.json"
    equity = tmp_path / "live_equity.jsonl"
    _write_ledger(ledger, _ledger_agents())

    async def account_zero():
        return 0.0, 0.0, False

    async def quotes_empty(codes):
        return {}

    report = await sampler.sample_once(
        _TRADING_NOW,
        account_fetcher=account_zero,
        quote_fetcher=quotes_empty,
        ledger_path=ledger,
        equity_path=equity,
    )
    # 总账户资产≤0 跳过；flash 有持仓但全无价跳过；glm 无持仓 → 仍写现金行
    assert report["written"] == 1
    rows = _read_rows(equity)
    assert rows[0]["agent"] == "glm-5.3-flash"
    assert any(s.startswith("account:asset<=0") for s in report["skipped"])
    assert "agent:deepseek-v4-flash:no_quotes" in report["skipped"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sample_once_skips_outside_window_and_non_trading_day(
    tmp_path, monkeypatch
):
    ledger = tmp_path / "live_ledger.json"
    equity = tmp_path / "live_equity.jsonl"
    _write_ledger(ledger, _ledger_agents())

    noon = datetime(2026, 10, 8, 12, 0, tzinfo=_TZ)
    out = await sampler.sample_once(
        noon,
        ledger_path=ledger,
        equity_path=equity,
    )
    assert out["skipped"] == ["outside_window"]
    assert not equity.exists()

    monkeypatch.setattr(sampler, "is_trading_day", lambda day: False)
    out2 = await sampler.sample_once(
        _TRADING_NOW,
        ledger_path=ledger,
        equity_path=equity,
    )
    assert out2["skipped"] == ["non_trading_day"]
    assert not equity.exists()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sample_once_missing_ledger_still_writes_account_row(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(sampler, "is_trading_day", lambda day: True)
    equity = tmp_path / "live_equity.jsonl"

    async def account():
        return 200000.0, 150000.0, False

    report = await sampler.sample_once(
        _TRADING_NOW,
        account_fetcher=account,
        ledger_path=tmp_path / "missing.json",
        equity_path=equity,
    )
    assert report["written"] == 1
    assert _read_rows(equity)[0]["agent"] is None
