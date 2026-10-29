"""``qmt_flatten_positions.py`` 安全闸与终态核对单测（fake 客户端，不下真单）。

2026-10-29 实测发现：脚本读的是 **QMT** 持仓，但 ``--via dispatcher``（默认）按
``broker:selected:CN`` 路由——选定券商是 tdx 时会把 QMT 的平仓意图卖到 TDX 账户。
本文件的判据：**券商不匹配时 live 必须直接拒跑**，dry-run 只提醒不拦。
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

from backend.scripts import qmt_flatten_positions as flat

POSITIONS = {
    "000002.SZ": {"volume": 100.0, "can_use": 100.0, "name": "万 科Ａ"},
}


class FakeClient:
    def __init__(
        self, *, enabled: bool = True, orders: list[dict[str, Any]] | None = None
    ):
        self.enabled = enabled
        self.orders = orders or []

    async def refresh_settings(self) -> dict[str, Any]:
        return {"enabled": self.enabled}

    async def query_orders(self) -> list[dict[str, Any]]:
        return list(self.orders)


def _args(*extra: str) -> Any:
    return flat._build_parser().parse_args(list(extra))


def _run(args: Any) -> int:
    return asyncio.run(flat._run(args))


def _patches(
    client: FakeClient,
    submit_dispatcher: AsyncMock,
    submit_direct: AsyncMock | None = None,
):
    return (
        patch(
            "backend.services.live_trading.services.qmt_exec_client.get_qmt_exec_client",
            return_value=client,
        ),
        patch.object(flat, "_positions", AsyncMock(return_value=POSITIONS)),
        patch.object(
            flat, "_reference_prices", AsyncMock(return_value={"000002.SZ": 3.29})
        ),
        patch.object(flat, "_submit_via_dispatcher", submit_dispatcher),
        patch.object(
            flat,
            "_submit_via_direct",
            submit_direct
            or AsyncMock(
                return_value={"order_id": "5048", "symbol": "000002.SZ", "side": "SELL"}
            ),
        ),
    )


class TestDispatcherBrokerGuard:
    def test_dispatcher_live_refuses_when_selected_broker_is_tdx(self) -> None:
        submit = AsyncMock()
        client = FakeClient()
        p = _patches(client, submit)
        with (
            p[0],
            p[1],
            p[2],
            p[3],
            p[4],
            patch(
                "backend.shared.real_positions.active_broker_type", return_value="tdx"
            ),
        ):
            code = _run(_args("--symbols", "000002.SZ", "--live"))
        assert code == 1
        submit.assert_not_awaited()

    def test_dispatcher_live_refuses_when_broker_read_fails(self, capsys: Any) -> None:
        """读不到选定券商时 live 必须 fail-closed（未知 ≠ 放行）。"""
        submit = AsyncMock()
        p = _patches(FakeClient(), submit)
        with (
            p[0],
            p[1],
            p[2],
            p[3],
            p[4],
            patch(
                "backend.shared.real_positions.active_broker_type",
                side_effect=RuntimeError("redis down"),
            ),
        ):
            code = _run(_args("--symbols", "000002.SZ", "--live"))
        assert code == 1
        submit.assert_not_awaited()
        assert "读取当前实盘券商失败" in capsys.readouterr().out

    def test_dispatcher_live_proceeds_when_selected_broker_is_qmt_exec(self) -> None:
        submit = AsyncMock(return_value={"status": "SUBMITTED"})
        client = FakeClient()
        p = _patches(client, submit)
        with (
            p[0],
            p[1],
            p[2],
            p[3],
            p[4],
            patch(
                "backend.shared.real_positions.active_broker_type",
                return_value="qmt_exec",
            ),
        ):
            code = _run(_args("--symbols", "000002.SZ", "--live", "--sleep", "0"))
        assert code == 0
        submit.assert_awaited_once()

    def test_dry_run_only_warns_on_mismatch(self, capsys: Any) -> None:
        submit = AsyncMock()
        p = _patches(FakeClient(), submit)
        with (
            p[0],
            p[1],
            p[2],
            p[3],
            p[4],
            patch(
                "backend.shared.real_positions.active_broker_type", return_value="tdx"
            ),
        ):
            code = _run(_args("--symbols", "000002.SZ"))
        assert code == 0
        submit.assert_not_awaited()
        assert "会卖到该券商账户" in capsys.readouterr().out

    def test_guard_reason_pure(self) -> None:
        assert flat._dispatcher_guard_reason("qmt_exec") == ""
        assert "tdx" in flat._dispatcher_guard_reason("tdx")
        assert flat._dispatcher_guard_reason("") != ""


class TestDirectVerify:
    def test_direct_mode_prints_order_id_and_final_status(self, capsys: Any) -> None:
        client = FakeClient(
            orders=[
                {
                    "order_id": "5048",
                    "order_remark": "qmwhatever",
                    "status": "FILLED",
                    "traded_volume": 100.0,
                    "traded_price": 3.24,
                }
            ]
        )
        p = _patches(client, AsyncMock())
        with p[0], p[1], p[2], p[3], p[4]:
            code = _run(
                _args(
                    "--symbols",
                    "000002.SZ",
                    "--live",
                    "--via",
                    "direct",
                    "--verify-sec",
                    "1",
                    "--sleep",
                    "0",
                )
            )
        out = capsys.readouterr().out
        assert code == 0
        assert "5048" in out
        assert "FILLED" in out
        assert "None" not in out

    def test_verify_failure_is_not_reported_as_submit_failure(
        self, capsys: Any
    ) -> None:
        """核对查询失败 ≠ 下单失败：委托可能已受理，调用方不能就此重下。"""
        client = FakeClient()
        p = _patches(client, AsyncMock())
        with (
            p[0],
            p[1],
            p[2],
            p[3],
            p[4],
            patch.object(
                flat,
                "_verify_order",
                AsyncMock(side_effect=RuntimeError("pipe timeout")),
            ),
        ):
            code = _run(
                _args(
                    "--symbols",
                    "000002.SZ",
                    "--live",
                    "--via",
                    "direct",
                    "--verify-sec",
                    "1",
                    "--sleep",
                    "0",
                )
            )
        out = capsys.readouterr().out
        assert code == 0
        assert "5048" in out
        assert "委托已受理，勿重复提交" in out
        assert "异常" not in out.split("5048", 1)[1]

    def test_non_terminal_status_is_not_labelled_final(self, capsys: Any) -> None:
        """核对窗口到期仍是 SUBMITTED 时，不能说成「终态」。"""
        client = FakeClient(
            orders=[{"order_id": "5048", "status": "SUBMITTED", "traded_volume": 0.0}]
        )
        p = _patches(client, AsyncMock())
        with p[0], p[1], p[2], p[3], p[4]:
            code = _run(
                _args(
                    "--symbols",
                    "000002.SZ",
                    "--live",
                    "--via",
                    "direct",
                    "--verify-sec",
                    "0.1",
                    "--sleep",
                    "0",
                )
            )
        out = capsys.readouterr().out
        assert code == 0
        assert "核对超时（未到终态） SUBMITTED" in out
        assert "终态 SUBMITTED" not in out

    def test_not_found_prints_unknown(self, capsys: Any) -> None:
        client = FakeClient()
        p = _patches(client, AsyncMock())
        with p[0], p[1], p[2], p[3], p[4]:
            code = _run(
                _args(
                    "--symbols",
                    "000002.SZ",
                    "--live",
                    "--via",
                    "direct",
                    "--verify-sec",
                    "0.1",
                    "--sleep",
                    "0",
                )
            )
        out = capsys.readouterr().out
        assert code == 0
        assert "终态未知：核对窗口内未查到该委托" in out


class TestVerifyOrder:
    def test_matches_by_remark_when_order_id_missing(self) -> None:
        client = FakeClient(
            orders=[
                {
                    "order_id": "9999",
                    "order_remark": "qmknown",
                    "status": "FILLED",
                    "traded_volume": 100.0,
                    "traded_price": 3.24,
                }
            ]
        )
        with patch(
            "backend.services.live_trading.services.qmt_exec_client.build_remark",
            return_value="qmknown",
        ):
            out = asyncio.run(
                flat._verify_order(client, order_id="", cid="any", seconds=0.2)
            )
        assert out is not None and out["order_id"] == "9999"

    def test_returns_none_when_nothing_matches(self) -> None:
        out = asyncio.run(
            flat._verify_order(FakeClient(), order_id="5048", cid="any", seconds=0.2)
        )
        assert out is None

    def test_non_terminal_returns_last_state(self) -> None:
        client = FakeClient(
            orders=[{"order_id": "5048", "status": "SUBMITTED", "traded_volume": 0.0}]
        )
        out = asyncio.run(
            flat._verify_order(client, order_id="5048", cid="any", seconds=0.2)
        )
        assert out is not None and out["status"] == "SUBMITTED"
