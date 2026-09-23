"""``plan_id`` 秒精度缺陷回归：同秒两笔单被桥按 ``plan_id`` 去重丢掉。

缺陷原文（``broker_client.py``，2026-09-23 核实）::

    plan_id = str(client_order_id or f"qm_{int(time.time())}_{os.getpid()}")

``time.time()`` 是**秒**精度：同一秒内下两笔单得到**同一个** ``plan_id``。
桥侧 ``tools/bridge-windows/src/executor/plan_executor.py::execute_plan`` 按
``plan_id`` 去重（命中即 ``status=duplicate`` → HTTP 409 ``DUPLICATE_PLAN``），
于是第二笔单被整单丢弃——没有 ``order_id``、没有成交、链路也不抛异常。
止损批量卖出多只标的最典型（同一事件循环里连发数笔）。

本文件钉五件事：
  1. 同一秒内两次下单得到**不同**的 ``plan_id``（旧实现此处红）；
  2. 调用方传入 ``client_order_id`` 时原样透传（幂等语义不变）；
  3. ``tdx_pusher.place_order`` 未传号时的缺省号同秒内也不撞；
  4. 纯函数在同一秒的不同纳秒上产出不同号；
  5. 两个 ``/api/v1/plans/execute`` 生成点都走唯一实现，不再自带秒精度写法。

时钟一律**注入**（``_FakeClock`` / ``now_ns`` 参数），不 sleep，结果确定。
异步用例统一 ``asyncio.run``（容器内无 pytest-asyncio）。
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path
from typing import Any

from backend.services.live_trading.services.broker_client import TdxBroker
from backend.shared.order_contract import build_bridge_plan_id

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BROKER_CLIENT = _REPO_ROOT / "backend/services/live_trading/services/broker_client.py"
_TDX_PUSH_SERVICE = (
    _REPO_ROOT / "backend/services/live_trading/services/tdx_push_service.py"
)

#: 秒精度的时间戳写法。覆盖两种历史形态：
#: ``int(time.time())``（broker_client）与 ``__import__("time").time()``（tdx_push_service）。
_SECONDS_CLOCK_RE = re.compile(
    r"int\(\s*time\.time\(\s*\)\s*\)|__import__\(\s*['\"]time['\"]"
)

#: 基准纳秒时间戳，落在某一秒正中（避免 1ns 推进跨秒把用例变成跨秒对比）。
_BASE_NS = 1_756_000_000_000_000_000


class _FakeClock:
    """可注入时钟：秒与纳秒同步推进（旧实现读 ``time()``，唯一实现读 ``time_ns()``）。"""

    def __init__(self, now_ns: int) -> None:
        self.now_ns = int(now_ns)

    def time(self) -> float:
        return self.now_ns / 1_000_000_000

    def time_ns(self) -> int:
        return self.now_ns

    def tick(self, delta_ns: int = 1) -> None:
        self.now_ns += delta_ns


class _FakeResponse:
    status_code = 200
    text = ""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class _RecordingBridge:
    """假桥：只记录下发请求体，不触网；回执形态与桥 ``plan_done`` 一致。"""

    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    async def post(
        self, url: str, json: dict[str, Any] | None = None, headers: Any = None
    ) -> _FakeResponse:
        body = dict(json or {})
        self.payloads.append(body)
        return _FakeResponse(
            {
                "plan_id": body.get("plan_id"),
                "status": "executed",
                "orders": [
                    {
                        "stock_code": "600036.SH",
                        "status": "submitted",
                        "order_id": f"Wtbh-{len(self.payloads)}",
                    }
                ],
            }
        )


def _broker_with_fake_bridge() -> tuple[TdxBroker, _RecordingBridge]:
    broker = TdxBroker(bridge_url="http://bridge:8550", bridge_token="token")
    fake = _RecordingBridge()
    # _get_client() 直接返回它 —— 不建真实 httpx 连接、不触网
    broker._client = fake  # type: ignore[assignment]
    return broker, fake


def _ns_of(plan_id: str) -> int:
    """取 ``qm_{ns}_{pid}`` 里的纳秒戳。格式变了就该红：这是被测契约本身。"""
    return int(plan_id.split("_")[1])


def test_同秒内两笔单得到不同_plan_id(monkeypatch: Any) -> None:
    """止损批量卖出多只票：同一秒内连发两笔，桥不得当成同一个计划。"""
    clock = _FakeClock(_BASE_NS)
    monkeypatch.setattr(time, "time", clock.time)
    monkeypatch.setattr(time, "time_ns", clock.time_ns)
    broker, bridge = _broker_with_fake_bridge()

    first = asyncio.run(
        broker.place_order(
            user_id=1, symbol="600036", side="SELL", quantity=100, order_type="MARKET"
        )
    )
    clock.tick()  # 同一秒，只晚 1 纳秒
    second = asyncio.run(
        broker.place_order(
            user_id=1, symbol="000001", side="SELL", quantity=100, order_type="MARKET"
        )
    )

    # 前提：两笔都真的走到了下发（防空转通过）
    assert first.success and second.success
    assert len(bridge.payloads) == 2
    plan_ids = [p["plan_id"] for p in bridge.payloads]
    # 用例自身的不空转校验：两个号确实落在同一秒内，才叫「同秒碰撞」
    assert _ns_of(plan_ids[0]) // 1_000_000_000 == _ns_of(plan_ids[1]) // 1_000_000_000
    assert plan_ids[0] != plan_ids[1], (
        f"同秒两笔单撞同一个 plan_id（{plan_ids[0]}）——"
        "桥按 plan_id 去重会把第二笔整单丢掉"
    )


def test_显式_client_order_id_原样透传() -> None:
    """调用方给了幂等键就用它（下游按此键对账，不得二次加工）。"""
    cid = "sltp-600036-20260923-r1"
    assert build_bridge_plan_id(cid) == cid

    broker, bridge = _broker_with_fake_bridge()
    result = asyncio.run(
        broker.place_order(
            user_id=1,
            symbol="600036",
            side="SELL",
            quantity=100,
            order_type="MARKET",
            client_order_id=cid,
        )
    )
    assert result.success
    assert [p["plan_id"] for p in bridge.payloads] == [cid]


def test_推送服务缺省号同秒内也不撞(monkeypatch: Any) -> None:
    """第二个生成点：``tdx_pusher.place_order`` 未传 ``plan_id`` 时的缺省号。"""
    from backend.services.live_trading.services.tdx_push_service import tdx_pusher

    clock = _FakeClock(_BASE_NS)
    monkeypatch.setattr(time, "time", clock.time)
    monkeypatch.setattr(time, "time_ns", clock.time_ns)
    sent: list[dict[str, Any]] = []

    async def _fake_post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
        sent.append(payload)
        return {"status": "executed", "orders": []}

    monkeypatch.setattr(tdx_pusher, "_post", _fake_post)

    asyncio.run(
        tdx_pusher.place_order(
            stock_code="600036.SH", side="sell", volume=100, price=12.5
        )
    )
    clock.tick()  # 同一秒，只晚 1 纳秒
    asyncio.run(
        tdx_pusher.place_order(
            stock_code="000001.SZ", side="sell", volume=100, price=12.6
        )
    )

    ids = [p["plan_id"] for p in sent]
    assert len(ids) == 2
    assert ids[0] != ids[1]


def test_纯函数在同一秒的不同纳秒产出不同号() -> None:
    """生成逻辑本身：纳秒是唯一精度来源，同秒不同纳秒即不同号。"""
    a = build_bridge_plan_id(now_ns=_BASE_NS)
    b = build_bridge_plan_id(now_ns=_BASE_NS + 1)  # 同秒，晚 1ns

    assert a != b
    assert a.startswith("qm_")
    assert a.endswith(f"_{os.getpid()}")
    assert _ns_of(a) == _BASE_NS  # 注入的时间戳原样进号，不被二次取整成秒


def test_两个生成点都不再自带秒精度写法() -> None:
    """``broker_client`` 与 ``tdx_push_service`` 共用唯一实现。"""
    for path in (_BROKER_CLIENT, _TDX_PUSH_SERVICE):
        src = path.read_text(encoding="utf-8")
        plan_lines = [ln for ln in src.splitlines() if "plan_id" in ln]
        assert plan_lines, f"{path.name} 里找不到 plan_id 生成点（扒错文件？）"
        assert "build_bridge_plan_id" in src, f"{path.name} 未走唯一实现"
        offenders = [ln.strip() for ln in plan_lines if _SECONDS_CLOCK_RE.search(ln)]
        assert not offenders, f"{path.name} 仍有秒精度 plan_id：{offenders}"
