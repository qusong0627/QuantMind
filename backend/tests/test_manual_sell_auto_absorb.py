"""人工卖出自动吸收（运行期）的纯逻辑与接线测试。

回归点（2026-11-18 用户要求「下次手动卖出要能实时同步到下一次 agent 分析」）：
* 吸收判据复用 ``manual_sell_absorption``：账户已清空 + 未归属卖出覆盖 + 买入早于卖出；
* 吸收成功后账本行消失、现金按真实成交价回补；
* 账户不可读（桥掉线）时整批不动；
* 两类差异（账本多 / 账户多）都要如实报告，不猜价格补记；
* 周期任务的时段闸门（工作日 09:15–15:15，上海时区）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from backend.services.trade.services import manual_sell_absorb_task as task
from backend.shared.decision import manual_sell_auto_absorb as auto
from backend.shared.decision.manual_sell_auto_absorb import (
    compute_mismatch,
    absorb_manual_sells,
)

CST = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 18, 14, 30, tzinfo=CST)


class _FakeSessionCtx:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *exc) -> bool:
        return False


def test_compute_mismatch_reports_both_directions() -> None:
    ledger_rows = [
        {"agent": "a", "code": "600011.SH", "volume": 3300},
        {"agent": "a", "code": "601825.SH", "volume": 1400},
    ]
    real = {"601658.SH": 3300.0}

    ledger_excess, account_excess = compute_mismatch(ledger_rows, real)

    assert ledger_excess["601825.SH"] == {"ledger": 1400.0, "account": 0.0}
    assert account_excess["601658.SH"] == {"ledger": 0.0, "account": 3300.0}
    assert ledger_excess["600011.SH"] == {"ledger": 3300.0, "account": 0.0}


def test_compute_mismatch_ignores_equal_totals() -> None:
    assert compute_mismatch(
        [{"agent": "a", "code": "600674.SH", "volume": 400}], {"600674.SH": 400.0}
    ) == ({}, {})


def _patch_repair(monkeypatch, *, ledger_seq, real, sells, applied):
    import backend.scripts.repair_agent_ledger_manual_sells as repair

    async def load_ledger_rows(session, tenant, user):
        return ledger_seq.pop(0) if ledger_seq else []

    async def load_accounts(session, tenant, user):
        return {"deepseek-v4-flash": 1000.0}

    async def load_unattributed_sells(session, tenant, user, since):
        return sells

    async def load_tdx_account_volumes(tenant, user, *, max_age_s):
        return (real, "读取失败：模拟" if real is None else "tdx_bridge 快照，2 只持仓")

    async def fake_apply(plan, *, tenant, user):
        applied.extend(plan.fills)
        return [
            {
                "fill_key": f.fill_key,
                "agent": f.agent,
                "code": f.code,
                "volume": f.volume,
                "price": f.price,
                "virtual_cash": 1000.0 + f.volume * f.price,
            }
            for f in plan.fills
        ]

    async def fake_verify(plan, outcomes, *, tenant, user, accounts_before):
        return []

    monkeypatch.setattr(repair, "load_ledger_rows", load_ledger_rows)
    monkeypatch.setattr(repair, "load_accounts", load_accounts)
    monkeypatch.setattr(repair, "load_unattributed_sells", load_unattributed_sells)
    monkeypatch.setattr(auto, "load_tdx_account_volumes", load_tdx_account_volumes)
    monkeypatch.setattr(repair, "apply_plan", fake_apply)
    monkeypatch.setattr(repair, "verify_plan", fake_verify)
    monkeypatch.setattr(auto, "get_session", lambda *a, **k: _FakeSessionCtx())


def test_absorb_applies_and_clears_phantom_position(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("QM_LOG_DIR", str(tmp_path))
    applied = []
    buy_ts = datetime(2026, 11, 9, 2, 51, tzinfo=timezone.utc)
    sells = [
        {
            "order_id": "ord-1",
            "code": "600011.SH",
            "volume": 4100.0,
            "price": 7.97,
            "executed_at": datetime(2026, 11, 18, 6, 31, tzinfo=timezone.utc),
            "trade_date": datetime(2026, 11, 18).date(),
            "agent": "",
        }
    ]
    ledger_seq = [
        [
            {
                "agent": "deepseek-v4-flash",
                "code": "600011.SH",
                "volume": 3300.0,
                "cost_price": 7.63,
                "buy_ts": buy_ts,
            }
        ],
        [],  # 吸收后复读
    ]
    _patch_repair(
        monkeypatch, ledger_seq=ledger_seq, real={}, sells=sells, applied=applied
    )

    result = asyncio.run(absorb_manual_sells("default", "10000001"))

    assert result.absorbed
    assert result.error is None
    assert len(result.fills) == 1
    assert result.fills[0]["code"] == "600011.SH"
    assert result.fills[0]["volume"] == pytest.approx(3300.0)
    assert applied and applied[0].price == pytest.approx(7.97)
    assert result.ledger_excess == {}
    assert result.backup_path and list(tmp_path.iterdir())


def test_absorb_skips_when_account_snapshot_unreadable(monkeypatch) -> None:
    applied = []
    _patch_repair(
        monkeypatch,
        ledger_seq=[[{"agent": "a", "code": "600011.SH", "volume": 3300}]],
        real=None,
        sells=[],
        applied=applied,
    )

    result = asyncio.run(absorb_manual_sells("default", "10000001"))

    assert result.fills == []
    assert result.error is None
    assert applied == []
    assert "读取失败" in result.snapshot_note or "快照" in result.snapshot_note


def test_window_gate_boundaries() -> None:
    assert task._in_window(datetime(2026, 11, 18, 9, 15, tzinfo=CST))
    assert task._in_window(datetime(2026, 11, 18, 15, 15, tzinfo=CST))
    assert not task._in_window(datetime(2026, 11, 18, 9, 14, tzinfo=CST))
    assert not task._in_window(datetime(2026, 11, 18, 15, 16, tzinfo=CST))
    assert not task._in_window(datetime(2026, 11, 21, 10, 0, tzinfo=CST))  # 周六


class _RowSession:
    def __init__(self, row):
        self._row = row

    async def execute(self, sql, params=None):
        return self

    def fetchone(self):
        return self._row


class _RowCtx:
    def __init__(self, row):
        self._row = row

    async def __aenter__(self):
        return _RowSession(self._row)

    async def __aexit__(self, *exc) -> bool:
        return False


def test_tdx_volumes_only_reads_tdx_snapshot(monkeypatch) -> None:
    """对账必须只看 TDX 账户：QMT 独有持仓不能并入（首跑误报的根因）。"""
    from datetime import timezone as _tz

    now = datetime.now(_tz.utc).replace(tzinfo=None)
    payload = {
        "positions": [
            {"symbol": "SH600011", "volume": 4100},
            {"symbol": "SZ002958", "volume": 2400},
            {"symbol": "SH600000", "volume": 0},  # 清仓残留不计
        ]
    }
    monkeypatch.setattr(auto, "get_session", lambda *a, **k: _RowCtx((now, payload)))

    volumes, note = asyncio.run(auto.load_tdx_account_volumes("default", "10000001"))

    assert volumes == {"600011.SH": 4100.0, "002958.SZ": 2400.0}
    assert "tdx_bridge" in note


def test_tdx_volumes_reject_stale_snapshot(monkeypatch) -> None:
    from datetime import timezone as _tz

    old = (datetime.now(_tz.utc) - timedelta(hours=2)).replace(tzinfo=None)
    monkeypatch.setattr(
        auto, "get_session", lambda *a, **k: _RowCtx((old, {"positions": []}))
    )

    volumes, note = asyncio.run(auto.load_tdx_account_volumes("default", "10000001"))

    assert volumes is None
    assert "掉线" in note


def test_alert_once_uses_raw_redis_for_nx(monkeypatch) -> None:
    """trade_shared 包装器不支持 nx/ex；去重必须落到裸 redis，否则每天重复推送。"""
    calls: dict = {}

    class _Raw:
        def set(self, key, value, nx=False, ex=None):
            calls["args"] = (key, nx, ex)
            return True

    class _Wrapper:
        client = _Raw()

        def set(self, *a, **k):  # 模拟真实包装器：不接受 nx
            raise TypeError("unexpected keyword argument 'nx'")

    import backend.services.trade_shared.deps as deps

    monkeypatch.setattr(deps, "get_redis", lambda: _Wrapper())

    assert task._alert_once("ledger_excess", "601825.SH") is True
    assert calls["args"][1] is True
    assert calls["args"][2] == task._ALERT_TTL_SEC
