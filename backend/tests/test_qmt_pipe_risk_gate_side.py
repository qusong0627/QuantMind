"""QMT 管道桥 risk_gate：买入受控、卖出放行（2026-10-30 用户拍板）。

背景：桥侧 `risk_gate` 原先对买卖一视同仁——ST/退名称黑名单、单笔 20 万、
±5% 偏离、30 笔/分钟 四条闸把**清仓卖出**也一并拦死（实测 2026-10-30 盘中：
8 只 ST 一只都卖不出去）。卖出是减风险动作，拦截它等于给退出加风险；
风控只该管住买入。

本文件钉住：
- 方向推断：xtquant `order_stock`/`passorder` 的 23=买 / 24=卖；
- 默认 `risk.apply_to_sells=false` → 卖出跳过黑名单/金额/偏离/限频；
- 买入四条闸全部保留；
- 把 `apply_to_sells` 打开时，卖出重新受控（可回滚）。
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "deploy"
    / "qmt-pipe-bridge"
    / "pipe_redis_bridge.py"
)

if not _MODULE_PATH.exists():  # pragma: no cover - 容器镜像不含 deploy/
    pytest.skip("deploy/qmt-pipe-bridge 不在当前运行环境", allow_module_level=True)


def _load_bridge_module(monkeypatch):
    """以桩模块加载桥脚本（生产依赖 redis / bigqmt 不在测试环境里）。"""

    class _FakeRedis:
        def __init__(self, *args, **kwargs):
            pass

        def pipeline(self):
            return self

    redis_mod = types.ModuleType("redis")
    redis_mod.Redis = _FakeRedis
    monkeypatch.setitem(sys.modules, "redis", redis_mod)

    pkg = types.ModuleType("bigqmt_signal_trader")
    pkg.__path__ = []
    transports = types.ModuleType("bigqmt_signal_trader.transports")
    transports.__path__ = []
    pipe_mod = types.ModuleType("bigqmt_signal_trader.transports.pipe_transport")

    class _FakeTransport:
        def __init__(self, *args, **kwargs):
            pass

    pipe_mod.NamedPipeTransport = _FakeTransport
    rpc_mod = types.ModuleType("bigqmt_signal_trader.redis_rpc")
    rpc_mod.decode_rpc_request_payload = lambda payload: payload
    monkeypatch.setitem(sys.modules, "bigqmt_signal_trader", pkg)
    monkeypatch.setitem(sys.modules, "bigqmt_signal_trader.transports", transports)
    monkeypatch.setitem(
        sys.modules, "bigqmt_signal_trader.transports.pipe_transport", pipe_mod
    )
    monkeypatch.setitem(sys.modules, "bigqmt_signal_trader.redis_rpc", rpc_mod)

    spec = importlib.util.spec_from_file_location("_qmt_pipe_bridge_test", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def bridge(monkeypatch):
    module = _load_bridge_module(monkeypatch)
    module.CFG._data["risk"] = {
        "enabled": True,
        "max_order_amount": 200000,
        "max_orders_per_min": 30,
        "max_deviation_pct": 5.0,
        "blacklist_codes": [],
        "blacklist_name_keywords": ["ST", "退"],
        "apply_to_sells": False,
        "sell_price_band_pct": 9.5,
    }
    module.STATE.name_map = {"600289.SH": "ST信通", "600036.SH": "招商银行"}
    module.STATE.quote_pre_close = {"600289.SH": 5.0, "600036.SH": 40.0}
    module._order_hits.clear()
    return module


def _order(symbol: str, volume: float, price: float, side: int) -> dict:
    return {
        "stock_code": symbol,
        "order_volume": volume,
        "price": price,
        "order_type": side,
    }


def test_side_detection_uses_xtquant_order_type(bridge):
    assert bridge._order_side({"order_type": 23}) == "BUY"
    assert bridge._order_side({"order_type": 24}) == "SELL"
    assert bridge._order_side({"op_type": 23}) == "BUY"
    assert bridge._order_side({"side": "sell"}) == "SELL"
    assert bridge._order_side({}) == ""


def test_sell_bypasses_st_name_blacklist(bridge):
    # 2026-10-30 实测：ST 名称黑名单把卖出也拦死 → 清仓失败。
    assert bridge.risk_gate("order_stock", _order("600289.SH", 100, 5.0, 24)) is None


def test_buy_still_blocked_by_st_name_blacklist(bridge):
    reason = bridge.risk_gate("order_stock", _order("600289.SH", 100, 5.0, 23))
    assert reason and "blacklisted name" in reason


def test_sell_bypasses_amount_cap(bridge):
    # 31,200 股 × 37.0 ≈ 115 万 > 20 万：买入拒，卖出放行（价格在卖出带宽内）。
    assert bridge.risk_gate("order_stock", _order("600036.SH", 31200, 37.0, 24)) is None
    reason = bridge.risk_gate("order_stock", _order("600036.SH", 31200, 37.0, 23))
    assert reason and "amount" in reason


def test_sell_bypasses_price_deviation(bridge):
    # 昨收 40.0，卖价 37.0（-7.5%）：买入的 ±5% 闸会拒，卖出放行（带宽 9.5% 内）。
    assert bridge.risk_gate("order_stock", _order("600036.SH", 100, 37.0, 24)) is None
    reason = bridge.risk_gate("order_stock", _order("600036.SH", 100, 37.0, 23))
    assert reason and "deviation" in reason


def test_sell_bypasses_rate_limit(bridge):
    # 卖出不消耗限频额度：连发 40 笔卖单全部放行
    for _ in range(40):
        assert (
            bridge.risk_gate("order_stock", _order("600036.SH", 100, 40.0, 24)) is None
        )
    # 买入的 30 笔/分钟额度仍然独立生效
    for _ in range(30):
        assert (
            bridge.risk_gate("order_stock", _order("600036.SH", 100, 40.0, 23)) is None
        )
    reason = bridge.risk_gate("order_stock", _order("600036.SH", 100, 40.0, 23))
    assert reason and "rate limit" in reason


def test_apply_to_sells_restores_sell_controls(bridge):
    """可回滚：把 apply_to_sells 打开，卖出重新受四条闸约束。"""
    bridge.CFG._data["risk"]["apply_to_sells"] = True
    reason = bridge.risk_gate("order_stock", _order("600289.SH", 100, 5.0, 24))
    assert reason and "blacklisted name" in reason


def test_empty_symbol_still_rejected_for_both_sides(bridge):
    assert (
        bridge.risk_gate("order_stock", _order("", 100, 5.0, 24))
        == "risk: empty symbol"
    )
    assert (
        bridge.risk_gate("order_stock", _order("", 100, 5.0, 23))
        == "risk: empty symbol"
    )


def test_idem_key_reads_order_remark(bridge):
    """xtquant order_stock 的幂等键字段是 order_remark（不是 remark）。

    2026-10-30 实测：只认 remark 时 order_stock 永远拿不到键，
    Redis 里 0 条 `qm:qmt:pipe:idem:*` → client_order_id 去重实际未生效。
    """
    bridge.CFG._data["account_id"] = "40327478"
    req = {
        "method": "order_stock",
        "params": {"stock_code": "600036.SH", "order_remark": "qm-abc"},
    }
    assert bridge._idem_key(req) == "qm:qmt:pipe:idem:40327478:qm-abc"


def test_idem_key_still_reads_http_remark(bridge):
    bridge.CFG._data["account_id"] = "40327478"
    req = {"method": "submit_order", "params": {"remark": "qm-http"}}
    assert bridge._idem_key(req) == "qm:qmt:pipe:idem:40327478:qm-http"


def test_idem_key_none_without_key_field(bridge):
    bridge.CFG._data["account_id"] = "40327478"
    assert bridge._idem_key({"method": "order_stock", "params": {}}) is None


def test_sell_price_band_rejects_far_limit(bridge):
    """卖出保留错价保护：限价对昨收偏离 > 带宽（9.5%）即拒。"""
    reason = bridge.risk_gate("order_stock", _order("600036.SH", 100, 30.0, 24))
    assert reason and "sell price band" in reason


def test_sell_price_band_allows_normal(bridge):
    assert bridge.risk_gate("order_stock", _order("600036.SH", 100, 39.0, 24)) is None


def test_sell_market_order_bypasses_band(bridge):
    assert bridge.risk_gate("order_stock", _order("600036.SH", 100, 0.0, 24)) is None


def test_sell_band_can_be_disabled(bridge):
    bridge.CFG._data["risk"]["sell_price_band_pct"] = 0
    assert bridge.risk_gate("order_stock", _order("600036.SH", 100, 30.0, 24)) is None


def test_order_method_detection(bridge):
    assert bridge.is_order_method("order_stock")
    assert bridge.is_order_method("passorder")
    assert bridge.is_order_method("submit_order")
    assert not bridge.is_order_method("query_stock_orders")
    assert not bridge.is_order_method("")


def test_order_backlog_gate(bridge):
    assert bridge.order_backlog_gate(2, 3) is None
    busy = bridge.order_backlog_gate(3, 3)
    assert busy and busy.startswith("BRIDGE_BUSY")
    # limit=0 关闭快速失败
    assert bridge.order_backlog_gate(99, 0) is None
