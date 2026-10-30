"""手工下单工具的 PG 分账口径（2026-10-30 重做）。

盯四件事：
* 额度逐 agent 取 ``TradingPolicy``，且可动用额度 = ``min(授予−已用, 账上现金)``；
* 卖出归属从 PG 活账取（不再读 2026-09-30 冻结的文件账本）；
* 候选读取兼容夜间池 ``candidates`` 与决策池 ``picks`` 两种 schema，代码统一后缀式；
* 成交按桥的**累计成交量台阶**分段入账，部分成交不重复记、终态即停。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # scripts/
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # 仓库根

import live_ledger_pg as llp  # noqa: E402
import live_trade_picks as ltp  # noqa: E402
from backend.shared.trading_policy import AgentPolicy, TradingPolicy  # noqa: E402


def _policy() -> TradingPolicy:
    return TradingPolicy(
        default=AgentPolicy(quota=100_000.0, per_stock_pct=0.2),
        agents={
            "a": AgentPolicy(quota=200_000.0, per_stock_pct=1.0),
            "b": AgentPolicy(quota=150_000.0, per_stock_pct=1.0),
        },
    )


def _ledger() -> dict:
    return {
        "version": 1,
        "agents": {
            "a": {
                "virtual_cash": 165_614.0,
                "positions": {
                    "601825.SH": {"volume": 3000, "cost_price": 8.96},
                },
            },
            "b": {
                "virtual_cash": 120_000.0,
                "positions": {
                    "600036.SH": {"volume": 1000, "cost_price": 10.0},
                },
            },
            "c": {"virtual_cash": 50_000.0, "positions": {}},
        },
    }


def test_remaining_is_min_of_grant_and_cash() -> None:
    policy = _policy()
    ledger = _ledger()

    # a: min(200000−26880, 165614) = 165614（现金更紧）
    assert llp.agent_remaining(ledger, "a", policy) == 165_614.0
    # b: min(150000−10000, 120000) = 120000（现金更紧）
    assert llp.agent_remaining(ledger, "b", policy) == 120_000.0
    # c: min(100000−0, 50000) = 50000（默认额度）
    assert llp.agent_remaining(ledger, "c", policy) == 50_000.0


def test_holder_comes_from_ledger_positions() -> None:
    ledger = _ledger()

    assert llp.holders_of(ledger, "601825.SH") == ["a"]
    assert llp.holders_of(ledger, "600036.SH") == ["b"]
    assert llp.holders_of(ledger, "000001.SZ") == []


def test_load_picks_supports_both_schemas(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ltp, "_PICKS_DIR", tmp_path)
    night = tmp_path / "20261030_picks.json"
    night.write_text(
        json.dumps(
            {
                "candidates": [
                    {
                        "symbol": "688687",
                        "name": "凯因科技",
                        "side": "BUY",
                        "score": 0.65,
                    },
                    {
                        "symbol": "600519",
                        "name": "贵州茅台",
                        "side": "HOLD",
                        "score": 0.9,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    agent = tmp_path / "20261030_agent_picks.json"
    agent.write_text(
        json.dumps(
            {
                "picks": [
                    {
                        "code": "003816.SZ",
                        "name": "中国广核",
                        "side": "BUY",
                        "score": 30.5,
                    },
                    {
                        "code": "300979.SZ",
                        "name": "华利集团",
                        "side": "HOLD",
                        "score": 29.2,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    picks = ltp.load_picks("picks", 5, "BUY")
    assert picks == [
        {"code": "688687.SH", "name": "凯因科技", "side": "BUY", "score": 0.65}
    ]

    picks = ltp.load_picks("agent", 5, "HOLD")
    assert [p["code"] for p in picks] == ["003816.SZ", "300979.SZ"]
    assert picks[0]["score"] == 30.5


def test_load_picks_rejects_retired_md_source() -> None:
    with pytest.raises(SystemExit):
        ltp.load_picks("md", 5, "BUY")


def _fake_broker(steps: list[dict]):
    """每次 get_orders 返回下一帧（最后一帧重复）。"""
    frames = list(steps)

    class _Broker:
        def get_orders(self):
            frame = frames.pop(0) if len(frames) > 1 else frames[0]
            return [frame]

    return _Broker()


def test_settle_fill_records_each_cumulative_step(monkeypatch) -> None:
    monkeypatch.setattr(llp, "FILL_POLL_S", 0)
    calls: list[dict] = []

    def fake_apply(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(duplicate=False, note="")

    monkeypatch.setattr(llp, "apply_fill", fake_apply)
    broker = _fake_broker(
        [
            {
                "order_id": "o1",
                "filled_volume": 100,
                "filled_price": 10.0,
                "status": "partial",
            },
            {
                "order_id": "o1",
                "filled_volume": 300,
                "filled_price": 10.1,
                "status": "filled",
            },
        ]
    )

    out = llp.settle_fill(
        broker,
        allocations=[("a", 10_000)],
        code="600036.SH",
        side="buy",
        order_id="o1",
        limit_price=10.2,
        timeout_s=5,
    )

    assert [c["volume"] for c in calls] == [100, 200]
    assert [c["cumulative_volume"] for c in calls] == [100, 300]
    # 第 2 台阶的增量价 = (10.1×300 − 10.0×100) / 200 = 10.15，不是累计均价 10.1
    assert [c["price"] for c in calls] == [10.0, 10.15]
    assert out == {
        "filled": 300,
        "price": 10.1,
        "status": "filled",
        "terminal": True,
        "error": "",
        "by_agent": {"a": 300},
    }


def test_settle_fill_duplicate_advances_without_double_counting(monkeypatch) -> None:
    monkeypatch.setattr(llp, "FILL_POLL_S", 0)
    calls: list[dict] = []

    def fake_apply(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(duplicate=True, note="")

    monkeypatch.setattr(llp, "apply_fill", fake_apply)
    broker = _fake_broker(
        [
            {
                "order_id": "o1",
                "filled_volume": 100,
                "filled_price": 10.0,
                "status": "filled",
            }
        ]
    )

    out = llp.settle_fill(
        broker,
        allocations=[("a", 10_000)],
        code="600036.SH",
        side="sell",
        order_id="o1",
        limit_price=10.0,
        timeout_s=1,
    )

    assert len(calls) == 1
    assert out["filled"] == 100
    assert out["error"] == ""


def test_settle_fill_reports_ledger_error_and_keeps_prior_steps(monkeypatch) -> None:
    monkeypatch.setattr(llp, "FILL_POLL_S", 0)
    state = {"n": 0}

    def fake_apply(**kwargs):
        state["n"] += 1
        if state["n"] == 2:
            raise RuntimeError("db down")
        return SimpleNamespace(duplicate=False, note="")

    monkeypatch.setattr(llp, "apply_fill", fake_apply)
    broker = _fake_broker(
        [
            {
                "order_id": "o1",
                "filled_volume": 100,
                "filled_price": 10.0,
                "status": "partial",
            },
            {
                "order_id": "o1",
                "filled_volume": 300,
                "filled_price": 10.1,
                "status": "filled",
            },
        ]
    )

    out = llp.settle_fill(
        broker,
        allocations=[("a", 10_000)],
        code="600036.SH",
        side="buy",
        order_id="o1",
        limit_price=10.2,
        timeout_s=5,
    )

    assert out["filled"] == 100  # 已记上的部分不回滚
    assert out["by_agent"] == {"a": 100}
    assert "db down" in out["error"]


def test_settle_fill_splits_shared_account_sell_fifo(monkeypatch) -> None:
    """共享账户一笔卖单跨多家 agent：按 FIFO 逐家减仓，键带 agent 防幂等吞行。"""
    monkeypatch.setattr(llp, "FILL_POLL_S", 0)
    calls: list[dict] = []

    def fake_apply(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(duplicate=False, note="")

    monkeypatch.setattr(llp, "apply_fill", fake_apply)
    broker = _fake_broker(
        [
            {
                "order_id": "o1",
                "filled_volume": 6900,
                "filled_price": 8.98,
                "status": "filled",
            }
        ]
    )

    out = llp.settle_fill(
        broker,
        allocations=[("flash", 3000), ("pro", 2800), ("glm", 1100)],
        code="601825.SH",
        side="sell",
        order_id="o1",
        limit_price=8.9,
        timeout_s=1,
    )

    assert [(c["agent"], c["volume"]) for c in calls] == [
        ("flash", 3000),
        ("pro", 2800),
        ("glm", 1100),
    ]
    assert [c["key_suffix"] for c in calls] == ["flash", "pro", "glm"]
    assert out["by_agent"] == {"flash": 3000, "pro": 2800, "glm": 1100}
    assert out["filled"] == 6900
    assert out["error"] == ""


def test_settle_fill_reports_volume_without_ledger_cover(monkeypatch) -> None:
    """成交比账本持仓多 → 多出的部分明确报「无对应持仓」，不硬塞给某一家。"""
    monkeypatch.setattr(llp, "FILL_POLL_S", 0)
    monkeypatch.setattr(
        llp,
        "apply_fill",
        lambda **kwargs: SimpleNamespace(duplicate=False, note=""),
    )
    broker = _fake_broker(
        [
            {
                "order_id": "o1",
                "filled_volume": 300,
                "filled_price": 8.98,
                "status": "filled",
            }
        ]
    )

    out = llp.settle_fill(
        broker,
        allocations=[("flash", 100)],
        code="601825.SH",
        side="sell",
        order_id="o1",
        limit_price=8.9,
        timeout_s=1,
    )

    assert out["by_agent"] == {"flash": 100}
    assert out["filled"] == 300
    assert "200 股" in out["error"]
