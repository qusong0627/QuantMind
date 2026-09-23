"""P0.6 · 桥侧止损 daemon 隔离：桥自带的 ``StopLossDaemon`` 必须恒为未 arm。

被守护的缺陷
------------
``tools/bridge-windows/src/executor/stop_loss_daemon.py`` 是一个**独立于
QuantMind 的卖出者**：5s 轮询、触发即 ``order_stock(..., Side.SELL, PriceType.MARKET)``。
QuantMind 的 ``sltp_executor`` 已经占了守护单这个位、指向**同一账户同一持仓**。
两者同时触发 = 超卖。桥侧 ``main.py`` 的 ``sltp_daemon.enabled`` 默认是
``True``，且条目会从 ``stop_loss_state.json`` **跨重启自恢复**——所以「现在没配」
不等于「以后不会被 arm」，这条闸门守的是「被 arm 了要立刻发现」。

本文件同时钉死**arming 路径本身在 QuantMind 侧不存在**（源码守卫），
两侧合起来才是"结构性不可能"，而不是"眼下恰好为空"。
"""
from __future__ import annotations

import pathlib
from typing import Any

import pytest

from backend.services.live_trading.routers import real_trading_utils as rtu

_BRIDGE = "http://bridge.invalid:8550"


class _Resp:
    def __init__(self, status_code: int = 200, payload: Any = None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


def _configure(monkeypatch, url: str = _BRIDGE, token: str = "tok"):
    monkeypatch.setattr(rtu.settings, "TDX_BRIDGE_URL", url, raising=False)
    monkeypatch.setattr(rtu.settings, "TDX_BRIDGE_TOKEN", token, raising=False)


def test_armed_items_block_startup(monkeypatch):
    """桥侧有 enabled 条目 = 第二个卖出者 = 必须阻断。"""
    # Arrange
    _configure(monkeypatch)
    monkeypatch.setattr(
        rtu.httpx, "get",
        lambda *a, **k: _Resp(200, {"items": [
            {"stock_code": "600036.SH", "enabled": True, "entry_price": 38.0},
            {"stock_code": "000001.SZ", "enabled": True, "entry_price": 11.0},
        ]}),
    )
    # Act
    ok, detail, armed = rtu.check_bridge_sltp_disarmed()
    # Assert
    assert ok is False
    assert armed is True
    assert "600036.SH" in detail and "000001.SZ" in detail
    assert "重复卖出" in detail


def test_already_triggered_items_do_not_count_as_armed(monkeypatch):
    """**误报陷阱**：触发过的条目被置 ``enabled=False`` 但留在列表里。

    按列表长度判定会把一次历史触发变成永久红灯，最终被人忽略——
    按 ``enabled`` 计数才是「当前真的会不会卖」。
    """
    # Arrange
    _configure(monkeypatch)
    monkeypatch.setattr(
        rtu.httpx, "get",
        lambda *a, **k: _Resp(200, {"items": [
            {"stock_code": "600036.SH", "enabled": False},  # 已触发过、已停用
            {"stock_code": "000001.SZ", "enabled": False},
        ]}),
    )
    # Act
    ok, detail, armed = rtu.check_bridge_sltp_disarmed()
    # Assert：列表非空但无一 armed → 放行
    assert ok is True
    assert armed is False
    assert "2 条历史记录均已停用" in detail


def test_empty_state_is_ok(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(rtu.httpx, "get", lambda *a, **k: _Resp(200, {"items": []}))
    ok, _detail, armed = rtu.check_bridge_sltp_disarmed()
    assert ok is True
    assert armed is False


def test_unconfigured_bridge_is_ok(monkeypatch):
    """未配置桥 = 没有桥侧 daemon = 无此风险（不是「跳过检查」）。"""
    _configure(monkeypatch, url="", token="")
    ok, detail, armed = rtu.check_bridge_sltp_disarmed()
    assert ok is True
    assert armed is False
    assert "未配置" in detail


def test_non_200_is_not_ok_but_not_blocking(monkeypatch):
    """查不顺 ≠ 已 arm：可达性由 check_tdx_bridge_online 单独兜底，不重复阻断。"""
    _configure(monkeypatch)
    monkeypatch.setattr(rtu.httpx, "get", lambda *a, **k: _Resp(500, {}))
    ok, detail, armed = rtu.check_bridge_sltp_disarmed()
    assert ok is False
    assert armed is False
    assert "HTTP 500" in detail


def test_unreachable_is_not_ok_but_not_blocking(monkeypatch):
    _configure(monkeypatch)

    def _boom(*_a, **_k):
        raise OSError("network unreachable")

    monkeypatch.setattr(rtu.httpx, "get", _boom)
    ok, detail, armed = rtu.check_bridge_sltp_disarmed()
    assert ok is False
    assert armed is False
    assert "不可达" in detail


def test_sends_bearer_token_to_sltp_endpoint(monkeypatch):
    """桥的 /sltp/state 与其它端点一样要鉴权；漏 token 会 401 → 恒报不可达。"""
    # Arrange
    _configure(monkeypatch, token="secret-tok")
    seen: dict = {}

    def _capture(url, **kwargs):
        seen["url"] = url
        seen["headers"] = kwargs.get("headers") or {}
        return _Resp(200, {"items": []})

    monkeypatch.setattr(rtu.httpx, "get", _capture)
    # Act
    rtu.check_bridge_sltp_disarmed()
    # Assert
    assert seen["url"].endswith("/api/v1/sltp/state")
    assert seen["headers"].get("Authorization") == "Bearer secret-tok"


@pytest.mark.parametrize("bad_items", [None, "not-a-list", [None, 42]])
def test_malformed_items_payload_does_not_crash(monkeypatch, bad_items):
    """桥返回畸形 items 时不得抛异常把整个 preflight 带崩。"""
    _configure(monkeypatch)
    monkeypatch.setattr(rtu.httpx, "get", lambda *a, **k: _Resp(200, {"items": bad_items}))
    ok, _detail, armed = rtu.check_bridge_sltp_disarmed()
    assert ok is True
    assert armed is False


def test_no_quantmind_source_arms_bridge_sltp():
    """源码守卫：QuantMind **永不**调用桥的 arming 端点。

    桥侧 daemon 默认 enabled=True 且条目跨重启自恢复 —— 只要有人调一次
    ``/api/v1/sltp/configure``，就会有一个 QuantMind 不知情的卖出者长期存在。
    这条路径必须保持关闭。
    """
    root = pathlib.Path(__file__).resolve().parents[1]  # backend/
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        if "tests" in path.parts or path.name == "real_trading_utils.py":
            continue  # 测试自身与本文件的探测函数允许出现该字符串
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "sltp/configure" in text:
            offenders.append(str(path.relative_to(root)))
    assert not offenders, f"QuantMind 侧出现了桥侧止损 arming 调用点: {offenders}"


def test_bridge_ships_with_daemon_disabled_by_default():
    """桥源码的**两处独立默认值**都必须是「关」。

    ``Config.get(dotted, default)`` 在 config.yaml 存在但缺该段时返回调用方给的
    default —— 所以 ``DEFAULT_CONFIG`` 与 ``main.py`` 的 ``.get`` 字面量是两个
    各自独立生效的门，只关一个等于没关（部署包通常自带 config.yaml，
    此时真正生效的恰恰是 ``main.py`` 那一处）。
    """
    root = pathlib.Path(__file__).resolve().parents[2]  # 仓库根
    bridge = root / "tools/bridge-windows"
    if not bridge.exists():
        # 后端测试跑在 quantmind 容器内，其 /app 只挂了 backend/ docs/ scripts/ 等，
        # **不含 tools/**。此处显式 skip 而非静默通过：同一断言已在桥自己的
        # 测试里落了一份（tools/bridge-windows/tests/test_all.py「止损守护默认关闭」），
        # 那份与源码同处、在宿主/Windows 上跑，是真守卫；这份是容器内的补充。
        pytest.skip("容器 /app 未挂载 tools/，桥源码守卫见 "
                    "tools/bridge-windows/tests/test_all.py")

    cfg_src = (bridge / "src/utils/config.py").read_text(encoding="utf-8")
    main_src = (bridge / "main.py").read_text(encoding="utf-8")

    assert "enabled: false" in cfg_src, "DEFAULT_CONFIG 里 sltp_daemon.enabled 应为 false"
    assert 'cfg.get("sltp_daemon.enabled", False)' in main_src, (
        'main.py 的 cfg.get("sltp_daemon.enabled", ...) 默认值应为 False'
    )
    assert 'cfg.get("sltp_daemon.enabled", True)' not in main_src, "仍有 True 默认值"


def test_preflight_actually_registers_the_check():
    """接线守卫：探测函数写好了但没接进 preflight = 闸门不存在。

    与 ``test_broker_plan_id_uniqueness`` 的源码守卫同一形态——防的是
    「函数在、测试绿、没人调用」这类静默失效。
    """
    src = (
        pathlib.Path(__file__).resolve().parents[1]
        / "services/live_trading/routers/real_trading_preflight.py"
    ).read_text(encoding="utf-8")
    assert 'add_check(\n            "bridge_sltp_disarmed"' in src or \
        '"bridge_sltp_disarmed"' in src, "preflight 未接入桥侧止损检查"
    assert "check_bridge_sltp_disarmed" in src, "preflight 未调用 check_bridge_sltp_disarmed"
