"""强平族前缀的五处登记表必须同步（P2.6 防漂移守卫）。

为什么要一张测试把五处捏在一起
--------------------------------
一条「一定会成交」的真单（止损 ``sltp:`` / 平仓清单 ``flat-`` / 减仓 ``trim:``）
在系统里要同时穿过五张登记表，谁漏了谁就出事，而且**每处的事故形态都不一样**：

============================  ==================================================
登记表                         漏登记的后果
============================  ==================================================
``order_timeout_scanner``     本地 30 分钟超时启发式把柜台仍挂着的单标成 EXPIRED，
  备注前缀 + cid 前缀          委托列表与柜台长期错位、成交回报被终态守卫吞掉
``close_cleanup_audit_task``  收盘核对不认识这类单的 QMT 通道身份，报假「本地残留」
``risk_gate_service``         自己的风控闸（``l3.price_deviation`` ±2%）拒掉自己的
  强平腿，减仓/止损在快市里全部变成废单（002074 的 42 笔实录）
``internal_strategy_dispatcher``  镜像时不豁免 2% 偏离闸门，真单被镜像层拦下
``broker_client``             涨跌停带校验把保护价当「疑似价格错误」拒掉
============================  ==================================================

这类漂移**不会自己暴露**：五处各自都有单测，各自都是绿的；只有「新加一个前缀家族」
这种横切改动才会同时踩到五处。故此处用同一份「强平族前缀表」逐处断言。
"""

from __future__ import annotations

import pytest

#: 强平族的统一口径：备注前缀（写进 order_data.remarks）与幂等号前缀（CID）。
FORCED_EXIT_REMARK_PREFIX = "trim:"
FORCED_EXIT_CID_PREFIX = "trim-"


def test_order_timeout_scanner_knows_the_family() -> None:
    from backend.services.trade.services.order_timeout_scanner import (
        _BROKER_MANAGED_CID_PREFIXES,
        _BROKER_MANAGED_REMARK_PREFIXES,
        is_broker_managed,
    )

    assert FORCED_EXIT_REMARK_PREFIX in _BROKER_MANAGED_REMARK_PREFIXES
    assert FORCED_EXIT_CID_PREFIX in _BROKER_MANAGED_CID_PREFIXES
    # 备注会被成交回报覆盖，cid 不会 —— 两条识别路径都要能用
    assert is_broker_managed(f"{FORCED_EXIT_REMARK_PREFIX}减仓执行器", None) is True
    assert (
        is_broker_managed(None, f"{FORCED_EXIT_CID_PREFIX}600036.SH-20260924-g1")
        is True
    )


def test_close_cleanup_audit_knows_the_family() -> None:
    from backend.services.trade.services.close_cleanup_audit_task import (
        _QMT_CHANNEL_CID_PREFIXES,
    )

    assert FORCED_EXIT_CID_PREFIX in _QMT_CHANNEL_CID_PREFIXES


def test_risk_gate_treats_the_family_as_forced_exit() -> None:
    from backend.services.trade.services.risk_gate_service import _is_forced_exit

    assert _is_forced_exit(f"{FORCED_EXIT_REMARK_PREFIX}减仓执行器 激进保护价") is True
    assert _is_forced_exit(f"{FORCED_EXIT_CID_PREFIX}600036.SH-20260924-g1") is True
    assert _is_forced_exit("普通策略单") is False


def test_dispatcher_mirror_exempts_the_family() -> None:
    from backend.services.live_trading.services.internal_strategy_dispatcher import (
        _FORCED_EXIT_REMARK_PREFIXES,
    )

    assert FORCED_EXIT_REMARK_PREFIX in _FORCED_EXIT_REMARK_PREFIXES


def test_broker_client_price_band_exempts_the_family() -> None:
    from backend.services.live_trading.services.broker_client import (
        _PRICE_PROTECTION_EXEMPT_CID_PREFIXES,
    )

    assert FORCED_EXIT_CID_PREFIX in _PRICE_PROTECTION_EXEMPT_CID_PREFIXES


@pytest.mark.parametrize(
    "module_path", ["order_timeout_scanner", "close_cleanup_audit_task"]
)
def test_cid_prefix_registries_share_the_same_family(module_path: str) -> None:
    """两张 cid 表历史上逐字相同（都是「QMT 通道托管」的同一批前缀）：别再分叉。"""
    import importlib

    if module_path == "order_timeout_scanner":
        mod = importlib.import_module(
            "backend.services.trade.services.order_timeout_scanner"
        )
        table = mod._BROKER_MANAGED_CID_PREFIXES
    else:
        mod = importlib.import_module(
            "backend.services.trade.services.close_cleanup_audit_task"
        )
        table = mod._QMT_CHANNEL_CID_PREFIXES
    for prefix in ("mir-", "sltp-", "flat-", "flatten-", FORCED_EXIT_CID_PREFIX):
        assert prefix in table, f"{module_path} 缺少 {prefix}"
