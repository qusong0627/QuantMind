"""桥「假活」探针：**行情通 ≠ 账户通**（P0.3 核出的两个 ⚠️ 之一）。

被守护的缺陷（**已发生过的真实故障**，不是理论风险）
--------------------------------------------------
隔壁 `logs/preflight.json` 09:12:17 实录：

```json
{"ok": false, "exec_enabled": true,
 "account": {"ok": false, "error": "TDX 桥账户查询失败: Read timed out"},
 "problems": ["账户通道不可用……盘中分析/调仓/哨兵条件位会静默停摆，需 RDP 重登通达信交易端"]}
```

即 **桥进程活着、行情照常走，而交易端掉线**：`/api/v1/health` 返回 200，
但 `query_stock_asset` 超时。QuantMind 的兜底探测
（`check_tdx_bridge_online`）只读 health 里的 `tdx_connected`，而那个字段来自
`health_check_fast` —— 它发的是 **`get_match_stkinfo`（贵州茅台）**，一个**行情类**
查询（`tools/bridge-windows/src/tdx/client.py:54`）。于是「行情通」被判成了
「交易通道在线」，两个 REAL 闸门（`/preflight` 的可启动、`/trading-precheck` 的
`passed`）都会放行——正是隔壁那条故障要拦的东西。

本模块补的是**账户通道**这一半：真走一次账户查询，而不是看心跳。

为什么超时必须大于 8 秒
-----------------------
桥自己的 TDX 调用预算是 `TDX_CALL_TIMEOUT = 8.0`
（`tools/bridge-windows/src/api/routes.py:20`）。客户端超时若小于它，先断的是**我们
这边**，失败退化成说不清的 "Read timed out"——分不出「桥在忙」与「交易端掉线」。
大于它则是桥先返回 502 `TDX_UNAVAILABLE` + 桥侧原始报错，可据此直接指到
「RDP 重登交易端」。

测试纪律（本仓既有教训）
------------------------
**本机真桥是通的**（实测 `/api/v1/health` 200、账户查询 200/0.05s）——所以每条用例
都必须把 `httpx.post` 打桩，否则断言会跟着本机环境漂。同理，每条「不通过」的用例
都配了**正向对照**：同一个探针在同一份夹具下喂健康响应必须返回 True，否则
`ok is False` 可能只是管道压根不通（见 `verification-vacuous-pass-guard`）。
"""

from __future__ import annotations

from typing import Any

import pytest

from backend.services.live_trading.routers import real_trading_utils as rtu

_BRIDGE = "http://bridge.invalid:8550"

#: 健康响应里 `asset` 的真实形状（2026-09-24 实测本机桥：5 个键、8 条持仓）。
_HEALTHY_ASSET = {
    "asset": 919044.38,
    "balance": 500000.0,
    "cash": 419044.38,
    "currency": "人民币",
    "market_value": 500000.0,
}


class _Resp:
    """``httpx.Response`` 的最小替身（探针读到的四个属性全在）。

    ``content`` 与 ``headers`` 不是装饰：探针按 ``resp.content`` 判「有没有 body」、
    ``check_tdx_bridge_online`` 按 ``content-type`` 决定要不要解析 JSON——少了它们，
    真件能跑而替身抛 AttributeError（那种红是替身的错，不是实现的）。
    """

    def __init__(self, status_code: int = 200, payload: Any = None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.content = b"{}" if payload is not None else b""
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._payload


def _configure(monkeypatch, url: str = _BRIDGE, token: str = "tok") -> None:
    monkeypatch.setattr(rtu.settings, "TDX_BRIDGE_URL", url, raising=False)
    monkeypatch.setattr(rtu.settings, "TDX_BRIDGE_TOKEN", token, raising=False)


def _healthy(**over: Any) -> _Resp:
    payload: dict = {
        "account_id": 0,
        "asset": dict(_HEALTHY_ASSET),
        "positions": [{}] * 8,
    }
    payload.update(over)
    return _Resp(200, payload)


# ── 探针：账户通道通不通 ──────────────────────────────────────────────
def test_healthy_account_channel_is_ok(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(rtu.httpx, "post", lambda *a, **k: _healthy())

    ok, detail, details = rtu.check_bridge_account_channel()

    assert ok is True
    assert "账户通道" in detail
    assert details["position_count"] == 8


def test_the_documented_failure_is_caught(monkeypatch):
    """**本模块存在的理由**：行情/health 通、而账户查询失败 —— 必须判不通过。

    正向对照在同一用例里：同一份 health 夹具下 `check_tdx_bridge_online()` 返回
    True（那正是缺陷：它判不出这种情形），而账户通道探针返回 False。
    """
    # Arrange：桥活着、行情通（health 200 + tdx_connected），但账户查询 502
    _configure(monkeypatch)
    monkeypatch.setattr(
        rtu.httpx,
        "get",
        lambda *a, **k: _Resp(200, {"status": "ok", "tdx_connected": True}),
    )
    monkeypatch.setattr(
        rtu.httpx,
        "post",
        lambda *a, **k: _Resp(
            502,
            {
                "success": False,
                "error": {"code": "TDX_UNAVAILABLE", "message": "通达信响应超时"},
            },
        ),
    )

    # Act
    quote_online, _quote_detail = rtu.check_tdx_bridge_online()
    account_ok, account_detail, _details = rtu.check_bridge_account_channel()

    # Assert：正向对照先立住 —— health 这一路确实判「在线」（否则下面的 False
    # 可能只是夹具全烂）
    assert quote_online is True, (
        "正向对照：health 通时旧探针必须判在线（它判不出的正是账户）"
    )
    assert account_ok is False
    assert "通达信响应超时" in account_detail, "桥侧原始报错要原样带出，别吞成一句话"
    assert "RDP" in account_detail, "要给出隔壁那条故障实录里验证过的修法"


def test_unreachable_bridge_is_not_ok_and_never_raises(monkeypatch):
    _configure(monkeypatch)

    def _boom(*_a, **_k):
        raise rtu.httpx.ConnectError("connection refused")

    monkeypatch.setattr(rtu.httpx, "post", _boom)

    ok, detail, _details = rtu.check_bridge_account_channel()

    assert ok is False
    assert "不可达" in detail


def test_unconfigured_bridge_is_not_ok(monkeypatch):
    """没配桥 = 这条通道无从谈起（不是「没有风险所以算过」）。

    与 `check_bridge_sltp_disarmed` 的方向**故意相反**：那条判的是「有没有第二个
    卖出者」，没桥就是没有；本条判的是「交易通道能不能用」，没桥就是不能用。
    调用方只在 REAL 且 QMT 也没就绪时才走到这里，那时不通过才是对的。
    """
    _configure(monkeypatch, url="", token="")

    ok, detail, _details = rtu.check_bridge_account_channel()

    assert ok is False
    assert "未配置" in detail


def test_missing_token_is_not_ok(monkeypatch):
    """有 URL 没 token = 打不开鉴权端点（不可当成「桥在线」）。"""
    _configure(monkeypatch, token="")

    ok, detail, _details = rtu.check_bridge_account_channel()

    assert ok is False
    assert "未配置" in detail


@pytest.mark.parametrize(
    "payload",
    [
        {},  # 空对象
        {"account_id": 0},  # 没有 asset
        {"asset": None, "positions": []},  # asset 是 null
        {"asset": "not-a-dict", "positions": []},  # asset 是字符串
        {"asset": {}, "positions": []},  # asset 是空 dict = 没查到东西
    ],
)
def test_a_200_without_a_real_asset_is_not_ok(monkeypatch, payload):
    """200 不等于查到了：**判据的失败方向必须是「报出来」**。

    桥在账户查询失败时返回 502，但「200 + 空壳」也要按失败办——否则桥一旦改成
    吞异常回 200，这条闸门就静默失效（同 `stall_alert` 对读不懂的日志条目的纪律）。
    """
    _configure(monkeypatch)
    monkeypatch.setattr(rtu.httpx, "post", lambda *a, **k: _Resp(200, payload))

    ok, detail, _details = rtu.check_bridge_account_channel()

    assert ok is False
    assert "未返回有效" in detail


def test_zero_positions_is_still_ok(monkeypatch):
    """空仓是合法状态 —— 不许把「今天没持仓」判成「通道坏了」。"""
    _configure(monkeypatch)
    monkeypatch.setattr(rtu.httpx, "post", lambda *a, **k: _healthy(positions=[]))

    ok, _detail, details = rtu.check_bridge_account_channel()

    assert ok is True
    assert details["position_count"] == 0


def test_malformed_positions_payload_does_not_crash(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(rtu.httpx, "post", lambda *a, **k: _healthy(positions="oops"))

    ok, _detail, details = rtu.check_bridge_account_channel()

    assert ok is True
    assert details["position_count"] == 0


def test_sends_bearer_token_to_the_account_endpoint(monkeypatch):
    """鉴权与端点形状：POST `/api/v1/account/query` + Bearer。

    与桥的 `auth_middleware`（`routes.py:191` 一带）对得上——health 是免鉴权的
    read-only 端点，账户查询不是。
    """
    _configure(monkeypatch, token="secret-tok")
    seen: dict = {}

    def _capture(url, **kwargs):
        seen["url"] = url
        seen["kwargs"] = kwargs
        return _healthy()

    monkeypatch.setattr(rtu.httpx, "post", _capture)

    rtu.check_bridge_account_channel()

    assert seen["url"].endswith("/api/v1/account/query")
    assert seen["kwargs"]["headers"]["Authorization"] == "Bearer secret-tok"
    assert seen["kwargs"]["json"]["account_type"] == "stock"


def test_client_timeout_exceeds_the_bridge_own_tdx_budget(monkeypatch):
    """超时必须大于桥自己的 8 秒预算（`TDX_CALL_TIMEOUT`，routes.py:20）。

    小于它 ⇒ 先断的是客户端 ⇒ 失败退化成 "Read timed out"，分不出「桥忙」与
    「交易端掉线」，桥侧的真实报错永远读不到。
    """
    assert rtu.BRIDGE_ACCOUNT_TIMEOUT_S > 8.0, (
        "客户端超时必须大于桥的 TDX_CALL_TIMEOUT(8.0)，否则先超时的是我们这边"
    )


def test_probe_passes_against_the_real_endpoint_shape(monkeypatch):
    """夹具形状守卫：本文件用的 `asset` 键必须覆盖探针真正读的那个。

    探针只要求 `asset` 是非空 dict；这条把「实测到的真实键」与「夹具」对齐，
    免得夹具悄悄漂成生产不会出现的形状（同 `verification-vacuous-pass-guard`
    第七形态）。
    """
    assert set(_HEALTHY_ASSET) == {
        "asset",
        "balance",
        "cash",
        "currency",
        "market_value",
    }

    _configure(monkeypatch)
    monkeypatch.setattr(rtu.httpx, "post", lambda *a, **k: _healthy())

    ok, _detail, details = rtu.check_bridge_account_channel()

    assert ok is True
    assert details["total_asset"] == _HEALTHY_ASSET["asset"]


# ── 接线：两个 REAL 闸门都要真的问这一句 ────────────────────────────────
# 为什么不用「源码里含某字符串」那种守卫：那条只证明字出现过，不证明代码跑得起来
# （见 `verification-vacuous-pass-guard` 第四形态）。这里直接调处理函数，断言
# **行出现在 checks 里、且 ready/passed 真的被它翻掉**。
from datetime import datetime, timezone  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

from backend.services.trade_shared.deps import AuthContext  # noqa: E402
from backend.services.live_trading.routers import (  # noqa: E402
    real_trading_preflight as preflight,
)
from backend.services.trade.services.trading_precheck_service import (  # noqa: E402
    run_trading_readiness_precheck,
)
import backend.services.trade.services.trading_precheck_service as precheck_service  # noqa: E402

_ACCOUNT_DOWN = (
    False,
    "账户通道不可用：TDX_UNAVAILABLE 通达信响应超时（需 RDP 重登）",
    {},
)
_ACCOUNT_UP = (True, "账户通道已连接（持仓 8 条）", {"position_count": 8})
_QUOTE_UP = (True, "TDX 桥在线，行情通道已连接")


class _Result:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return self

    def scalar_one_or_none(self):
        return (
            self._row if not isinstance(self._row, list) else (self._row or [None])[0]
        )

    def scalars(self):
        return self

    def first(self):
        return (
            self._row if not isinstance(self._row, list) else (self._row or [None])[0]
        )

    def all(self):
        return self._row if isinstance(self._row, list) else []


class _Db:
    def __init__(self, rows):
        self._rows = list(rows)

    async def execute(self, *_a, **_k):
        return _Result(self._rows.pop(0) if self._rows else [])

    async def rollback(self):
        return None


class _Redis:
    def __init__(self, payload=None):
        self.payload = payload or {}
        self.client = self

    def ping(self):
        return True

    def get(self, key):
        return self.payload.get(key)

    def zrevrange(self, *_a, **_k):
        return [("tick", 9999999999.0)]


def _model_dir(tmp_path):
    d = tmp_path / "model_qlib"
    d.mkdir(parents=True, exist_ok=True)
    (d / "model.lgb").write_bytes(b"fake_model")
    return d


class _FakeSignalReadiness:
    async def evaluate(self, *_a, **_k):
        return {
            "available": True,
            "blocking": False,
            "message": "信号就绪",
            "trading_permission": "live",
        }


def _stub_readiness_pipeline(monkeypatch, tmp_path, account_probe):
    """把 `run_trading_readiness_precheck` 除账户通道外的每一环都置成健康。

    这样「`passed` 翻假」就只可能来自账户通道那一条——**正向对照**正是在这里：
    同一份桩、只换账户通道的返回值。
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MODELS_PRODUCTION", str(_model_dir(tmp_path)))
    monkeypatch.setenv("STRATEGY_RUNNER_IMAGE", "quantmind-ml-runtime:latest")
    monkeypatch.setenv("INTERNAL_CALL_SECRET", "secret")
    monkeypatch.setattr(
        precheck_service, "signal_readiness_service", _FakeSignalReadiness()
    )
    monkeypatch.setattr(
        "backend.services.live_trading.routers.real_trading_utils"
        ".check_stream_series_freshness",
        lambda **_k: {"ok": True, "message": "stream_ready"},
    )
    monkeypatch.setattr(precheck_service.k8s_manager, "api", object(), raising=False)
    monkeypatch.setattr(
        precheck_service.k8s_manager, "core_api", object(), raising=False
    )
    monkeypatch.setattr(
        "backend.services.live_trading.routers.real_trading_utils"
        "._fetch_latest_real_account_snapshot",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "backend.services.live_trading.routers.real_trading_utils"
        ".check_tdx_bridge_online",
        lambda: _QUOTE_UP,
    )
    monkeypatch.setattr(
        "backend.services.live_trading.routers.real_trading_utils"
        ".check_bridge_account_channel",
        lambda: account_probe,
    )
    return _Db([{"ok": 1}])


def _run_precheck(db):
    return run_trading_readiness_precheck(
        db,
        mode="REAL",
        redis_client=_Redis(
            {"trade:agent:heartbeat:default:00001001": '{"timestamp": 9999999999}'}
        ),
        user_id="1001",
        tenant_id="default",
    )


@pytest.mark.asyncio
async def test_trading_precheck_fails_when_the_account_channel_is_down(
    monkeypatch, tmp_path
):
    """**产品级断言**：行情通、账户不通 ⇒ 预检不通过（这正是隔壁那条故障）。"""
    db = _stub_readiness_pipeline(monkeypatch, tmp_path, _ACCOUNT_DOWN)

    result = await _run_precheck(db)

    row = next(i for i in result["items"] if i["key"] == "bridge_account_channel")
    assert row["passed"] is False
    assert "RDP" in row["detail"]
    # 正向对照：行情那一行必须是**通过**的——否则「不通过」可能只是桥整个没探到，
    # 与账户通道无关（那正是改造前唯一能被发现的情形）。
    qmt = next(i for i in result["items"] if i["key"] == "qmt_agent_online")
    assert qmt["passed"] is True, "行情/进程这一半是通的（旧探针判得出它、判不出账户）"
    assert result["passed"] is False


@pytest.mark.asyncio
async def test_trading_precheck_passes_the_account_row_when_the_channel_is_up(
    monkeypatch, tmp_path
):
    """反向对照：同一份桩、只把账户通道换成健康 ⇒ 该行通过。

    缺这条时，上一条的 `passed is False` 可能来自任何别的行，与账户通道无关。
    """
    db = _stub_readiness_pipeline(monkeypatch, tmp_path, _ACCOUNT_UP)

    result = await _run_precheck(db)

    row = next(i for i in result["items"] if i["key"] == "bridge_account_channel")
    assert row["passed"] is True
    assert "8" in row["detail"]


def _stub_preflight_route(monkeypatch, account_probe, ready_rows):
    """把 `/preflight` 除账户通道外的每一环打桩（含免容器节点）。"""
    monkeypatch.setattr(
        preflight, "ensure_real_trading_allowed", lambda *_a, **_k: None
    )
    monkeypatch.setattr(preflight, "signal_readiness_service", _FakeSignalReadiness())
    monkeypatch.setattr(preflight, "orchestration_disabled", lambda: True)
    monkeypatch.setattr(
        preflight,
        "_resolve_runner_image_for_mode",
        lambda: ("img:latest", "configured"),
    )
    monkeypatch.setattr(
        preflight, "_fetch_latest_real_account_snapshot", AsyncMock(return_value=None)
    )
    # 两个桥探针都是**函数体内 import**（`from ... import check_bridge_account_channel`），
    # 所以只有打**源模块**才生效。打在 `preflight` 命名空间上是静默无效的——本机真桥
    # 恰好是通的，于是被探针问到的会是那台真桥，测试跟着环境漂（这条已实测踩到）。
    monkeypatch.setattr(rtu, "check_tdx_bridge_online", lambda: _QUOTE_UP)
    monkeypatch.setattr(rtu, "check_bridge_account_channel", lambda: account_probe)
    monkeypatch.setattr(
        preflight,
        "check_stream_series_freshness",
        lambda **_k: {"ok": True, "message": "stream_ready", "details": {}},
    )
    monkeypatch.setattr(
        preflight,
        "check_stream_quote_persist_rate",
        lambda **_k: {"ok": True, "message": "persist_ready", "details": {}},
    )
    monkeypatch.setattr(
        preflight, "_upsert_preflight_snapshot", AsyncMock(return_value=None)
    )
    # 桥侧止损 daemon：函数体内实时 import，打源模块
    monkeypatch.setattr(
        rtu, "check_bridge_sltp_disarmed", lambda: (True, "未 arm", False)
    )
    monkeypatch.setenv("INTERNAL_CALL_SECRET", "secret")
    return _Db(ready_rows)


def _auth():
    return AuthContext(
        user_id="10000001", tenant_id="default", raw_sub="10000001", roles=[]
    )


@pytest.mark.asyncio
async def test_preflight_ready_is_false_when_the_account_channel_is_down(monkeypatch):
    """`/preflight` 的 `ready` 就是前端「可启动」那个徽标——必须真被账户通道翻掉。"""
    db = _stub_preflight_route(monkeypatch, _ACCOUNT_DOWN, [{"ok": 1}])

    result = await preflight.preflight_check(
        trading_mode="REAL",
        user_id=None,
        tenant_id=None,
        auth=_auth(),
        redis=_Redis(),
        db=db,
    )

    row = next(c for c in result["checks"] if c["key"] == "bridge_account_channel")
    assert row["ok"] is False
    assert row["required"] is True, "REAL 下账户通道坏了必须阻断启动，不能只是提醒"
    qmt = next(c for c in result["checks"] if c["key"] == "qmt_agent_online")
    assert qmt["ok"] is True, "正向对照：桥进程/行情这一半是通的"
    assert result["ready"] is False


@pytest.mark.asyncio
async def test_preflight_account_row_absent_when_the_qmt_agent_carries_it(monkeypatch):
    """走 QMT 通道时**不**该出现桥的账户通道行——别把不存在的通道报成红的。

    只让**账户通道探针**炸：`check_tdx_bridge_online` 另有一个正当消费点
    （融资融券段的「通达信账户无信用数据 ⇒ 放行多空灰度」，`real_trading_preflight`
    第 438 行一带，那里问的就是行情/进程这一半），那条不该被本用例波及。
    """
    # Arrange：QMT Agent 快照与心跳都在且新鲜 ⇒ 不进桥兜底分支
    db = _stub_preflight_route(monkeypatch, _ACCOUNT_DOWN, [{"ok": 1}])
    snapshot = {
        "snapshot_at": datetime.now(timezone.utc),
        "payload_json": {},
    }
    monkeypatch.setattr(
        preflight,
        "_fetch_latest_real_account_snapshot",
        AsyncMock(return_value=snapshot),
    )
    monkeypatch.setattr(rtu, "check_tdx_bridge_online", lambda: _QUOTE_UP)
    monkeypatch.setattr(
        rtu,
        "check_bridge_account_channel",
        lambda: (_ for _ in ()).throw(
            AssertionError("QMT 通道就绪时不该探桥的账户通道")
        ),
    )
    monkeypatch.setattr(preflight, "_parse_bridge_report_ts", lambda _p: 9999999999.0)

    # Act
    result = await preflight.preflight_check(
        trading_mode="REAL",
        user_id=None,
        tenant_id=None,
        auth=_auth(),
        redis=_Redis(
            {"trade:agent:heartbeat:default:10000001": '{"timestamp": 9999999999}'}
        ),
        db=db,
    )

    # Assert：正向对照先立住 —— QMT 那一行确实是过的（否则「没有该行」可能只是
    # 整个 REAL 段提前炸了，与本条无关）
    qmt = next(c for c in result["checks"] if c["key"] == "qmt_agent_online")
    assert qmt["ok"] is True
    assert not [c for c in result["checks"] if c["key"] == "bridge_account_channel"]
