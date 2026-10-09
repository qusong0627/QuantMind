"""富途通道测试（2026-10-08 自旧栈恢复）——子进程纯函数 + arena `/futu/*` 契约 + 写面闸门。

三处金样必须钉死，任何一处松了都是生产事故：

1. **`closed` 返回 `{closed:[...]}`**——旧栈把服务层解包成裸数组再发，
   前端按 `d.closed` 取永远是空（「已完成」页静默空白半年）。这里防回归。
2. **code 归一 `00700.HK` 单向出口**——前端 reshape 与港股分析循环都吃这一种
   形态；富途原始形态（`HK.00700`）不得从任何端点漏出。
3. **REAL 写面 fail-closed 且在两处都拦在起子进程之前**：
   闸门关 → 403 `real_trading_disabled` 逐字；闸门开但无 `trade_pwd_md5`
   → 409 `futu_unlock_required`。测试用假 `place_order` 证明「压根没调到桥」。
   解锁凭据只允许在 `futu_live` 注入子进程 payload——审计 jsonl 里不得出现。

子进程模块（futu_subprocess）的设计承诺可脱离 futu SDK 单测：SDK 相关导入
全部函数内懒加载，这里用 `sys.modules["futu"]` 假模块顶替（宿主测试环境
无 futu-api）。
"""

from __future__ import annotations

import json
import signal
import sys
import time
import types
from pathlib import Path

import pytest

from backend.services.trade.services import futu_subprocess as fp

BASE = "/api/v1/agent-arena"


# ---------- 假 futu SDK（懒加载导入顶替） ----------


class _Tok:
    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:  # 报错信息可读
        return self.name


def _fake_futu_module() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        TrdEnv=types.SimpleNamespace(REAL=_Tok("REAL"), SIMULATE=_Tok("SIMULATE")),
        OrderType=types.SimpleNamespace(MARKET="MARKET", NORMAL="NORMAL"),
        TrdSide=types.SimpleNamespace(BUY="BUY", SELL="SELL"),
        ModifyOrderOp=types.SimpleNamespace(CANCEL="CANCEL"),
    )


@pytest.fixture()
def fake_futu(monkeypatch):
    module = _fake_futu_module()
    monkeypatch.setitem(sys.modules, "futu", module)
    return module


# ---------- 假 DataFrame（dict 行 + iloc/iterrows 最小实现） ----------


class _Frame:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    def __len__(self) -> int:
        return len(self._rows)

    @property
    def iloc(self):
        rows = self._rows

        class _Iloc:
            def __getitem__(self, i):
                return rows[i]

        return _Iloc()

    def iterrows(self):
        return iter(list(enumerate(self._rows)))


class FakeTradeCtx:
    """OpenSecTradeContext 最小替身：记录调用序，返回值可编排。"""

    def __init__(self, frames: dict | None = None, unlock_ret: int = 0):
        self.calls: list[tuple] = []
        self._frames = frames or {}
        self._unlock_ret = unlock_ret

    def accinfo_query(self, trd_env=None):
        self.calls.append(("accinfo", trd_env))
        return 0, _Frame([{"total_assets": 100.0, "cash": 40.0, "market_val": 60.0}])

    def position_list_query(self, trd_env=None):
        self.calls.append(("positions", trd_env))
        rows = self._frames.get(
            repr(trd_env),
            [
                {
                    "code": "HK.700",
                    "qty": 100,
                    "can_sell_qty": 100,
                    "market_val": 40000.0,
                    "cost_price": 380.0,
                    "nominal_price": 400.0,
                    "stock_name": "腾讯控股",
                    "currency": "HKD",
                }
            ],
        )
        return 0, _Frame(rows)

    def unlock_trade(self, password_md5=None):
        self.calls.append(("unlock", password_md5))
        return self._unlock_ret, "unlock failed" if self._unlock_ret else "ok"

    def place_order(self, **kwargs):
        self.calls.append(("place", kwargs))
        return 0, _Frame(
            [
                {
                    "order_id": "OID-1",
                    "order_status": "SUBMITTED",
                    "dealt_qty": 0,
                    "dealt_avg_price": 0,
                    "last_err_msg": "",
                }
            ]
        )

    def modify_order(self, *args, **kwargs):
        self.calls.append(("modify", kwargs))
        return 0, "ok"

    def order_list_query(self, trd_env=None):
        self.calls.append(("orders", trd_env))
        return 0, _Frame(
            [
                {
                    "order_id": "OID-1",
                    "code": "HK.00700",
                    "stock_name": "腾讯控股",
                    "trd_side": "BUY",
                    "order_type": "NORMAL",
                    "order_status": "FILLED_ALL",
                    "qty": 100,
                    "price": 400.0,
                    "dealt_qty": 100,
                    "dealt_avg_price": 399.5,
                    "create_time": "2026-10-08 10:00:00",
                    "last_err_msg": "",
                }
            ]
        )

    def get_market_snapshot(self, code_list=None):
        self.calls.append(("snapshot", code_list))
        return 0, _Frame(
            [
                {
                    "code": "HK.00700",
                    "stock_name": "腾讯控股",
                    "last_price": 404.0,
                    "prev_close_price": 400.0,
                    "volume": 1000,
                    "turnover": 404000.0,
                }
            ]
        )


# ---------- 代码归一（金样：00700.HK 单向出口） ----------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HK.700", "00700.HK"),
        ("HK.0700", "00700.HK"),
        ("HK.00700", "00700.HK"),
        ("00700.HK", "00700.HK"),
        ("0001.HK", "00001.HK"),
        ("hk00700", "00700.HK"),
        ("00700.hk", "00700.HK"),
        ("US.AAPL", "US.AAPL"),
        ("SH.600036", "SH.600036"),
        ("CL.FUT", "CL.FUT"),
        ("", ""),
        (None, ""),
    ],
)
def test_norm_hk_code_forms(raw, expected):
    assert fp._norm_hk_code(raw) == expected


@pytest.mark.parametrize("raw", ["HK.700", "00700.HK", "HK00700", "0001.HK"])
def test_norm_hk_code_idempotent(raw):
    once = fp._norm_hk_code(raw)
    assert fp._norm_hk_code(once) == once


def test_to_futu_code_maps_hk_and_passes_others():
    # Arrange / Act / Assert
    assert fp._to_futu_code("00700.HK") == "HK.00700"
    assert fp._to_futu_code("US.AAPL") == "US.AAPL"
    assert fp._to_futu_code("SH.600036") == "SH.600036"


# ---------- 解析纯函数 ----------


def test_aggregate_positions_merges_same_code_after_normalize():
    # Arrange：同一标的两种写法（HK.700 / HK.00700）必须归并到一条
    rows = [
        {"code": "HK.700", "qty": 100, "can_sell_qty": 100, "market_val": 40000.0,
         "cost_price": 380.0, "nominal_price": 400.0, "stock_name": "腾讯控股",
         "currency": "HKD"},
        {"code": "HK.00700", "qty": 100, "can_sell_qty": 50, "market_val": 41000.0,
         "cost_price": 390.0, "nominal_price": 410.0, "stock_name": "腾讯控股",
         "currency": "HKD"},
        {"code": "HK.00700", "qty": 0, "market_val": 0, "realized_pl": 88.0},
    ]
    # Act
    positions = fp._aggregate_positions(rows)
    # Assert：已平仓行被跳过；两条持仓归并
    assert list(positions.keys()) == ["00700.HK"]
    merged = positions["00700.HK"]
    assert merged["volume"] == 200
    assert merged["available_volume"] == 150
    assert merged["cost"] == pytest.approx(385.0)


@pytest.mark.parametrize(
    ("row", "is_closed"),
    [
        ({"code": "HK.700", "qty": 0, "realized_pl": 120.5, "cost_price": 380.0,
          "nominal_price": 400.0, "stock_name": "腾讯控股", "currency": "HKD"}, True),
        ({"code": "HK.700", "qty": 0, "realized_pl": "N/A"}, False),  # 'N/A' → 0
        ({"code": "HK.700", "qty": 0, "realized_pl": 0}, False),
        ({"code": "HK.700", "qty": 100, "realized_pl": 500.0}, False),  # 持仓行
    ],
)
def test_parse_closed_row_judgement(row, is_closed):
    parsed = fp._parse_closed_row(row)
    assert (parsed is not None) is is_closed
    if is_closed:
        assert parsed["code"] == "00700.HK"
        assert parsed["realized_pl"] == 120.5


def test_parse_order_row_normalizes_and_defaults():
    parsed = fp._parse_order_row(
        {"order_id": "O1", "code": "HK.0700", "stock_name": "腾讯控股",
         "trd_side": "BUY", "price": "N/A", "dealt_qty": 0}
    )
    assert parsed["code"] == "00700.HK"
    assert parsed["price"] == 0.0
    assert parsed["order_id"] == "O1"


def test_parse_snapshot_row_day_change():
    code, snap = fp._parse_snapshot_row(
        {"code": "HK.00700", "stock_name": "腾讯控股", "last_price": 404.0,
         "prev_close_price": 400.0, "volume": 10, "turnover": 4040.0}
    )
    assert code == "00700.HK"
    assert snap["day_chg"] == pytest.approx(1.0)
    # 昨收缺失（0）时不得除零，涨跌给 0
    _, snap0 = fp._parse_snapshot_row({"code": "HK.00700", "last_price": 404.0})
    assert snap0["day_chg"] == 0.0


# ---------- account_both（单腿失败不拖垮另一腿） ----------


class _LegCtx(FakeTradeCtx):
    def __init__(self, real_fails: bool = False):
        super().__init__()
        self._real_fails = real_fails

    def accinfo_query(self, trd_env=None):
        if self._real_fails and repr(trd_env) == "REAL":
            raise RuntimeError("REAL leg down")
        return super().accinfo_query(trd_env)


def test_account_both_two_legs(fake_futu):
    # Arrange
    ctx = _LegCtx()
    # Act
    out = fp._op_account_both(ctx, None, {})
    # Assert
    assert out["errors"] == {}
    assert out["real"]["env"] == "REAL"
    assert out["simulate"]["env"] == "SIMULATE"
    assert out["real"]["positions"]["00700.HK"]["volume"] == 100


def test_account_both_single_leg_failure_is_null(fake_futu):
    # Arrange
    ctx = _LegCtx(real_fails=True)
    # Act
    out = fp._op_account_both(ctx, None, {})
    # Assert：单腿 null + errors，另一腿完好
    assert out["real"] is None
    assert "REAL" in out["errors"]
    assert out["simulate"]["total_asset"] == 100.0


# ---------- place：解锁先行 + 失败短路 + adjust_limit 分市场 ----------


def _place_payload(**over):
    payload = {
        "env": "SIMULATE",
        "order": {"code": "00700.HK", "price": 400.0, "quantity": 100,
                  "order_type": "NORMAL", "trd_side": "BUY"},
    }
    payload.update(over)
    return payload


def test_place_sim_no_unlock(fake_futu):
    ctx = FakeTradeCtx()
    out = fp._op_place(ctx, _Tok("SIMULATE"), _place_payload(unlock_pwd_md5="md5"))
    assert out["success"] is True
    assert [c[0] for c in ctx.calls] == ["place"]
    # 代码以富途形态交给 SDK；港股单带市价保护价
    place_kwargs = ctx.calls[0][1]
    assert place_kwargs["code"] == "HK.00700"
    assert place_kwargs["adjust_limit"] == 0.0


def test_place_real_unlocks_then_places(fake_futu):
    ctx = FakeTradeCtx()
    out = fp._op_place(
        ctx, _Tok("REAL"), _place_payload(env="REAL", unlock_pwd_md5="abc123")
    )
    assert out["success"] is True
    assert [c[0] for c in ctx.calls] == ["unlock", "place"]
    assert ctx.calls[0][1] == "abc123"


def test_place_unlock_failure_short_circuits(fake_futu):
    # Arrange：解锁被拒 → 绝不下单
    ctx = FakeTradeCtx(unlock_ret=-1)
    out = fp._op_place(
        ctx, _Tok("REAL"), _place_payload(env="REAL", unlock_pwd_md5="bad")
    )
    # Assert
    assert out["success"] is False
    assert out["message"].startswith("unlock_failed")
    assert [c[0] for c in ctx.calls] == ["unlock"]


def test_place_us_code_passthrough_without_adjust_limit(fake_futu):
    ctx = FakeTradeCtx()
    payload = _place_payload()
    payload["order"]["code"] = "US.AAPL"
    fp._op_place(ctx, _Tok("SIMULATE"), payload)
    place_kwargs = ctx.calls[0][1]
    assert place_kwargs["code"] == "US.AAPL"
    assert place_kwargs["adjust_limit"] is None


# ---------- 路由层：契约 + 闸门 ----------


@pytest.fixture()
def live_mod(monkeypatch):
    from backend.services.trade.services import futu_live

    return futu_live


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.services.api.routers.agent_arena import futu_family
    from backend.services.api.user_app.middleware.auth import get_current_user

    # 审计落点指到沙箱（logs_dir 走 QM_ARENA_DATA_ROOT）
    monkeypatch.setenv("QM_ARENA_DATA_ROOT", str(tmp_path))
    app = FastAPI()
    app.include_router(futu_family.router, prefix=BASE)
    app.dependency_overrides[get_current_user] = lambda: {"user_id": 1}
    with TestClient(app) as c:
        yield c


def test_futu_routes_registered_in_arena_package(monkeypatch):
    """futu 族 7 端点已挂进 arena 包且可达。

    注意断言姿势：FastAPI 0.141 起 ``include_router`` 是惰性的——父路由的
    ``routes`` 里是 ``_IncludedRouter`` 对象（无 ``.path``），即便挂了 app、
    发过请求也不会摊平成路径集合，所以旧的「静态路径 ∈ app.routes」断言
    已不成立。改为：子路由自身路由表钉满 7 条（子路由无嵌套 include，routes
    是实体），再用一条真实请求穿过 arena 包证明聚合链路可路由。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.services.api.routers.agent_arena import futu_family
    from backend.services.api.routers.agent_arena import router as arena_router
    from backend.services.api.user_app.middleware.auth import get_current_user
    from backend.services.trade.services import futu_live

    own = {getattr(r, "path", "") for r in futu_family.router.routes}
    for expected in (
        "/futu/account",
        "/futu/account-both",
        "/futu/orders",
        "/futu/closed",
        "/futu/snapshot",
        "/futu/place",
        "/futu/cancel",
    ):
        assert expected in own, f"{expected} 不在 futu_family 路由表"

    async def fake_both():
        return {"real": {"total_asset": 1.0}, "simulate": None, "errors": {}}

    monkeypatch.setattr(futu_live, "query_account_both", fake_both)
    app = FastAPI()
    app.include_router(arena_router)
    app.dependency_overrides[get_current_user] = lambda: {"user_id": 1}
    with TestClient(app) as c:
        res = c.get(f"{BASE}/futu/account-both")
    assert res.status_code == 200
    assert res.json()["data"]["real"]["total_asset"] == 1.0


def test_account_envelope_carries_env(client, live_mod, monkeypatch):
    async def fake_account(env="SIMULATE"):
        return {"total_asset": 1.0, "cash": 1.0, "market_value": 0.0,
                "positions": {"00700.HK": {"volume": 1}}}

    monkeypatch.setattr(live_mod, "query_account", fake_account)
    res = client.get(f"{BASE}/futu/account", params={"env": "real"})
    assert res.status_code == 200
    body = res.json()
    assert body["success"] is True
    assert body["data"]["env"] == "REAL"
    assert "00700.HK" in body["data"]["positions"]


def test_account_failure_degrades_to_200_envelope(client, live_mod, monkeypatch):
    async def boom(env="SIMULATE"):
        raise RuntimeError("opend down")

    monkeypatch.setattr(live_mod, "query_account", boom)
    res = client.get(f"{BASE}/futu/account")
    assert res.status_code == 200
    body = res.json()
    assert body["success"] is False
    assert "富途查询失败" in body["error"]


def test_account_both_partial_leg_null(client, live_mod, monkeypatch):
    async def fake_both():
        return {"real": None, "simulate": {"total_asset": 1.0}, "errors": {"REAL": "x"}}

    monkeypatch.setattr(live_mod, "query_account_both", fake_both)
    res = client.get(f"{BASE}/futu/account-both")
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["real"] is None
    assert data["simulate"]["total_asset"] == 1.0
    assert data["errors"] == {"REAL": "x"}


def test_account_both_all_down_503(client, live_mod, monkeypatch):
    async def fake_both():
        return {"real": None, "simulate": None, "errors": {"REAL": "a", "SIMULATE": "b"}}

    monkeypatch.setattr(live_mod, "query_account_both", fake_both)
    res = client.get(f"{BASE}/futu/account-both")
    assert res.status_code == 503


def test_closed_is_wrapped_object(client, live_mod, monkeypatch):
    """旧栈解包成裸数组是 bug（前端 d.closed 永远空）——这里钉回对象形状。"""

    async def fake_closed(env="SIMULATE"):
        return {"closed": [{"code": "00700.HK", "realized_pl": 1.0}]}

    monkeypatch.setattr(live_mod, "query_closed", fake_closed)
    res = client.get(f"{BASE}/futu/closed")
    assert res.status_code == 200
    data = res.json()["data"]
    assert isinstance(data, dict) and isinstance(data["closed"], list)
    assert data["closed"][0]["code"] == "00700.HK"


def test_closed_missing_key_yields_empty_list(client, live_mod, monkeypatch):
    async def fake_closed(env="SIMULATE"):
        return {}

    monkeypatch.setattr(live_mod, "query_closed", fake_closed)
    res = client.get(f"{BASE}/futu/closed")
    assert res.json()["data"] == {"closed": []}


def test_snapshot_empty_codes_400(client):
    res = client.get(f"{BASE}/futu/snapshot", params={"codes": " , "})
    assert res.status_code == 400


def test_snapshot_shape(client, live_mod, monkeypatch):
    async def fake_snapshot(codes):
        return {"snapshot": {"00700.HK": {"last_price": 1.0}}}

    monkeypatch.setattr(live_mod, "query_snapshot", fake_snapshot)
    res = client.get(f"{BASE}/futu/snapshot", params={"codes": "00700.HK"})
    assert res.status_code == 200
    assert res.json()["data"]["snapshot"]["00700.HK"]["last_price"] == 1.0


# ---- 写面闸门（两个 fail-closed 都在起子进程之前） ----


@pytest.fixture()
def place_spy(monkeypatch, live_mod):
    calls: list = []

    async def fake_place(order, env="SIMULATE", market="HK"):
        calls.append((order, env, market))
        return {"success": True, "order_id": "OID-9", "message": "SUBMITTED"}

    monkeypatch.setattr(live_mod, "place_order", fake_place)
    monkeypatch.setattr(live_mod, "trade_pwd_md5_configured", lambda: False)
    return calls


def _place_body(**over):
    body = {"env": "SIMULATE", "market": "HK",
            "order": {"code": "00700.HK", "price": 400.0, "quantity": 100,
                      "order_type": "NORMAL", "trd_side": "BUY"}}
    body.update(over)
    return body


def test_place_real_gate_disabled_403(client, monkeypatch, place_spy):
    # Arrange：闸门关（默认）+ 凭据齐备——仍必须 403
    monkeypatch.delenv("ENABLE_REAL_TRADING", raising=False)
    monkeypatch.setattr(
        "backend.services.trade.services.futu_live.trade_pwd_md5_configured", lambda: True
    )
    # Act
    res = client.post(f"{BASE}/futu/place", json=_place_body(env="REAL"))
    # Assert：detail 逐字 + 桥未被调用
    assert res.status_code == 403
    assert res.json()["detail"] == "real_trading_disabled"
    assert place_spy == []


def test_place_real_without_unlock_409(client, monkeypatch, place_spy):
    # Arrange：闸门开、无解锁凭据
    monkeypatch.setenv("ENABLE_REAL_TRADING", "true")
    # Act
    res = client.post(f"{BASE}/futu/place", json=_place_body(env="REAL"))
    # Assert
    assert res.status_code == 409
    assert res.json()["detail"] == "futu_unlock_required"
    assert place_spy == []


def test_place_simulate_passes_without_gate(client, monkeypatch, place_spy):
    monkeypatch.delenv("ENABLE_REAL_TRADING", raising=False)
    res = client.post(f"{BASE}/futu/place", json=_place_body())
    assert res.status_code == 200
    body = res.json()
    assert body["success"] is True
    assert body["data"]["order_id"] == "OID-9"
    assert len(place_spy) == 1


@pytest.mark.parametrize(
    ("patch", "detail_part"),
    [
        ({"order": {"price": 1.0, "quantity": 1, "trd_side": "BUY"}}, "order.code"),
        ({"order": {"code": "00700.HK", "price": 1.0, "quantity": 0,
                    "trd_side": "BUY"}}, "quantity"),
        ({"order": {"code": "00700.HK", "price": 0, "quantity": 1,
                    "trd_side": "BUY"}}, "price"),
        ({"order": {"code": "00700.HK", "price": 1.0, "quantity": 1,
                    "trd_side": "HOLD"}}, "trd_side"),
    ],
)
def test_place_validation_400(client, place_spy, patch, detail_part):
    body = _place_body(**patch)
    res = client.post(f"{BASE}/futu/place", json=body)
    assert res.status_code == 400
    assert detail_part in res.json()["detail"]
    assert place_spy == []


def test_place_bridge_error_502(client, monkeypatch, live_mod, tmp_path):
    async def boom(order, env="SIMULATE", market="HK"):
        raise RuntimeError("opend down")

    monkeypatch.setattr(live_mod, "place_order", boom)
    monkeypatch.setattr(live_mod, "trade_pwd_md5_configured", lambda: False)
    res = client.post(f"{BASE}/futu/place", json=_place_body())
    assert res.status_code == 502
    assert res.json()["detail"].startswith("futu_bridge_error")


def test_cancel_real_fail_closed(client, monkeypatch, live_mod):
    async def fake_cancel(order_id, env="SIMULATE", market="HK"):
        return {"success": True, "message": "CANCELLED"}

    monkeypatch.setattr(live_mod, "cancel_order", fake_cancel)
    monkeypatch.setattr(live_mod, "trade_pwd_md5_configured", lambda: False)
    # SIM 放行
    res = client.post(f"{BASE}/futu/cancel", json={"env": "SIMULATE", "order_id": "O1"})
    assert res.status_code == 200 and res.json()["success"] is True
    # REAL 无凭据 409（闸门开的前提下）
    monkeypatch.setenv("ENABLE_REAL_TRADING", "true")
    res = client.post(f"{BASE}/futu/cancel", json={"env": "REAL", "order_id": "O1"})
    assert res.status_code == 409
    assert res.json()["detail"] == "futu_unlock_required"
    # order_id 必填
    res = client.post(f"{BASE}/futu/cancel", json={"env": "SIMULATE"})
    assert res.status_code == 400


def test_place_audit_written_without_secrets(client, monkeypatch, place_spy, tmp_path):
    res = client.post(f"{BASE}/futu/place", json=_place_body())
    assert res.status_code == 200
    files = list((tmp_path / "logs").glob("futu_orders_*.jsonl"))
    assert len(files) == 1
    line = files[0].read_text(encoding="utf-8").strip()
    assert '"code": "00700.HK"' in line
    # 审计记录绝不得出现解锁凭据字段
    assert "unlock_pwd_md5" not in line
    assert "md5" not in line


# ---- 委托回执 → QQ 分市场通道（写面同时是通知面，2026-10-08 用户裁决） ----


@pytest.fixture(autouse=True)
def qq_spy(monkeypatch):
    """把 QQ 外发钉进记录器：容器内 runtime.env 有真实凭据，测试绝不允许真发。"""
    from backend.shared import qq_notify

    spy: dict[str, list] = {"notify": [], "alert": []}
    monkeypatch.setattr(
        qq_notify,
        "notify_async",
        lambda title, content="", channel="default": spy["notify"].append(
            (title, content, channel)
        ),
    )
    monkeypatch.setattr(
        qq_notify, "alert_async", lambda **kw: spy["alert"].append(kw) or True
    )
    return spy


def test_place_receipt_pushed_to_hk_channel(client, place_spy, qq_spy):
    res = client.post(f"{BASE}/futu/place", json=_place_body())
    assert res.status_code == 200
    assert qq_spy["notify"], "下单回执必须外发（用户裁决：成交/委托结果是通知事件）"
    title, _, channel = qq_spy["notify"][0]
    assert channel == "hk"
    assert "00700.HK" in title and "100股" in title


def test_place_us_market_routes_to_us_channel(client, place_spy, qq_spy):
    res = client.post(f"{BASE}/futu/place", json=_place_body(market="US"))
    assert res.status_code == 200
    assert qq_spy["notify"][0][2] == "us", "美股标的不得串进港股通知通道"


def test_place_rejected_outcome_still_pushed_with_reason(
    client, live_mod, monkeypatch, qq_spy
):
    """SDK 拒单（200 + success:false）也是委托结果——必须回执，且带拒因。"""

    async def rejected(order, env="SIMULATE", market="HK"):
        return {"success": False, "message": "现金不足", "order_id": "OID-X"}

    monkeypatch.setattr(live_mod, "place_order", rejected)
    res = client.post(f"{BASE}/futu/place", json=_place_body())
    assert res.status_code == 200 and res.json()["success"] is False
    title, content, channel = qq_spy["notify"][0]
    assert "❌" in title and channel == "hk"
    assert "现金不足" in content


def test_place_bridge_error_alerts_hk_channel(client, live_mod, monkeypatch, qq_spy):
    async def boom(order, env="SIMULATE", market="HK"):
        raise RuntimeError("opend down")

    monkeypatch.setattr(live_mod, "place_order", boom)
    res = client.post(f"{BASE}/futu/place", json=_place_body())
    assert res.status_code == 502
    assert qq_spy["alert"], "桥故障必须告警外发"
    alert = qq_spy["alert"][0]
    assert alert["level"] == "warning"
    assert alert["alert_type"] == "futu-bridge"
    assert alert["channel"] == "hk"
    assert "opend down" in alert["content"]


def test_cancel_receipt_pushed(client, live_mod, monkeypatch, qq_spy):
    async def fake_cancel(order_id, env="SIMULATE", market="HK"):
        return {"success": True, "message": "CANCELLED"}

    monkeypatch.setattr(live_mod, "cancel_order", fake_cancel)
    res = client.post(f"{BASE}/futu/cancel", json={"env": "SIMULATE", "order_id": "O1"})
    assert res.status_code == 200
    title, _, channel = qq_spy["notify"][0]
    assert "撤单" in title and "O1" in title and channel == "hk"


def test_notification_failure_never_breaks_trade(client, place_spy, monkeypatch):
    """QQ 挂了交易照走——通知只是旁路（模块 docstring 的承诺）。"""

    def _boom(*a, **kw):
        raise RuntimeError("qq down")

    monkeypatch.setattr("backend.shared.qq_notify.notify_async", _boom)
    res = client.post(f"{BASE}/futu/place", json=_place_body())
    assert res.status_code == 200 and res.json()["success"] is True


# ---- 子进程硬超时（OpenD 未登录时 SDK 挂在握手上永不返回；2026-10-08 实测） ----


class TestHardTimeout:
    def test_hung_call_is_aborted_at_the_deadline(self):
        """看门狗必须在超时点**打断**阻塞调用，而不是等它自己结束。

        现场：容器里 5 个 futu_subprocess 挂 20~70 分钟，各驻留 ~296MB
        （OpenD 停在「请输入账号」）；调用方 kill docker exec 客户端杀不到容器内进程，
        所以要由子进程自己保证退出。
        """
        t0 = time.monotonic()
        try:
            fp._install_hard_timeout(1)
            with pytest.raises(TimeoutError) as ei:
                time.sleep(10)
        finally:
            signal.alarm(0)
        assert time.monotonic() - t0 < 3, "超时点就该炸，不该陪跑到 sleep 结束"
        assert "1s" in str(ei.value)
        assert "OpenD" in str(ei.value), "错误信息要指向根因（未登录），不是泛泛的 timeout"

    def test_default_stays_below_every_caller_timeout(self):
        """默认值必须明显小于调用方超时（futu_live 45s / 循环降级 30s），否则看门狗白装。"""
        assert fp.HARD_TIMEOUT_S == 20
        for caller_timeout in (30, 45):
            assert fp.HARD_TIMEOUT_S < caller_timeout

    def test_timeout_from_env_guards_bad_values(self, monkeypatch):
        monkeypatch.delenv("FUTU_SUBPROCESS_TIMEOUT_S", raising=False)
        assert fp._timeout_from_env() == fp.HARD_TIMEOUT_S
        monkeypatch.setenv("FUTU_SUBPROCESS_TIMEOUT_S", "5")
        assert fp._timeout_from_env() == 5
        for bad in ("abc", "0", "-3", "", " "):
            monkeypatch.setenv("FUTU_SUBPROCESS_TIMEOUT_S", bad)
            assert fp._timeout_from_env() == fp.HARD_TIMEOUT_S, repr(bad)

    def test_connect_timeout_is_caught_and_written(self, monkeypatch, tmp_path):
        """挂死点在 ``_open_ctx`` 里（SDK 构造即连 OpenD）——它必须在 try 内。

        实况回归：ctx 放 try 外时，看门狗抛的 TimeoutError 直接冒到顶层，
        失败 JSON 没落盘、进程卡在非 daemon 的 SDK 线程上不退
        （容器里那些驻留 1 小时、各占 296MB 的僵尸进程就是这么来的）。
        """
        out = tmp_path / "probe.json"
        monkeypatch.setattr(
            sys, "argv",
            ["futu_subprocess.py", "futu-opend", "11111", "/tmp/k",
             "account_both", "{}", str(out)],
        )
        # 只求 import 通过：测的是失败路径，不是真 SDK（真 SDK 在容器里）
        fake = types.ModuleType("futu")
        fake.TrdEnv = types.SimpleNamespace(REAL="REAL", SIMULATE="SIMULATE")
        fake_cfg = types.ModuleType("futu.common.sys_config")
        fake_cfg.SysConfig = types.SimpleNamespace(set_init_rsa_file=lambda *_: None)
        monkeypatch.setitem(sys.modules, "futu", fake)
        monkeypatch.setitem(sys.modules, "futu.common", types.ModuleType("futu.common"))
        monkeypatch.setitem(sys.modules, "futu.common.sys_config", fake_cfg)

        msg = "futu 子进程硬超时 20s——OpenD 未登录时 SDK 会挂在握手上"

        def _boom(*a, **kw):
            raise TimeoutError(msg)

        monkeypatch.setattr(fp, "_open_ctx", _boom)
        monkeypatch.setattr(fp.os, "_exit", lambda code=0: (_ for _ in ()).throw(SystemExit(code)))

        try:
            with pytest.raises(SystemExit) as ei:
                fp.main()
        finally:
            signal.alarm(0)  # main 装的看门狗别漏给后续测试
        assert ei.value.code == 2, "超时路径要以非 0 退出（且 os._exit 绕开 SDK 线程）"
        assert json.loads(out.read_text(encoding="utf-8")) == {
            "success": False, "message": msg,
        }, "失败必须落盘：调用方按文件内容判失败，不依赖退出码"
