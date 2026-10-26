# -*- coding: utf-8 -*-
"""minibt 模拟盘桥：键与纯逻辑单测（不依赖 minibt / Redis / DB）。"""

from __future__ import annotations

import time

from backend.shared.minibt_sim_keys import (
    attempts_key,
    build_request_payload,
    container_name,
    heartbeat_is_fresh,
    normalize_target_shares,
    parse_symbol_constant,
    request_key,
    target_key,
    target_state,
    trade_date_str,
)


def test_键含租户用户策略且非法字符被替换() -> None:
    key = target_key("default", "10000001", "sys/minibt dual_ma")
    assert key == "qm:sim:minibt:target:default:10000001:sys_minibt_dual_ma"
    assert request_key("t", "u", "s").startswith("qm:sim:minibt:req:")
    assert attempts_key("t", "u", "s", "2026-10-26").endswith(":2026-10-26")


def test_容器名长度受限() -> None:
    name = container_name("t" * 80, "u" * 80, "s" * 80)
    assert len(name) <= 63
    assert name.startswith("qm-minibt-sim-")


def test_解析策略顶部SYMBOL常量() -> None:
    code = "# comment\nSYMBOL = '600036'      # 标的\nSTRATEGY = 'x'\n"
    assert parse_symbol_constant(code) == "600036"
    assert parse_symbol_constant('SYMBOL = "SH600036"') == "SH600036"
    assert parse_symbol_constant("SYMBOL: str = '600036'") == "600036"  # 带类型注解
    assert parse_symbol_constant("    SYMBOL = '600036'") is None  # 缩进=类属性，不认
    assert parse_symbol_constant("symbol = '600036'") is None
    assert parse_symbol_constant("SYMBOL = 600036") is None


def test_目标状态_正常() -> None:
    now = time.time()
    payload = {
        "status": "ok",
        "trade_date": "2026-10-26",
        "computed_at": now - 5,
        "symbol": "600036.SH",
        "target_shares": 1000,
    }
    assert target_state(payload, trade_date="2026-10-26", now_ts=now) == ("ok", "ok")


def test_目标状态_缺失_过期_错误_日期不符() -> None:
    now = time.time()
    assert target_state(None, trade_date="2026-10-26", now_ts=now) == (
        "missing",
        "missing",
    )
    stale = {
        "status": "ok",
        "trade_date": "2026-10-26",
        "computed_at": now - 999,
        "symbol": "600036.SH",
        "target_shares": 100,
    }
    assert target_state(stale, trade_date="2026-10-26", now_ts=now)[0] == "stale"
    error = {
        "status": "error",
        "error": "snapshot_stale:400s",
        "trade_date": "2026-10-26",
        "computed_at": now,
    }
    state, reason = target_state(error, trade_date="2026-10-26", now_ts=now)
    assert state == "error" and "snapshot_stale" in reason
    wrong = {
        "status": "ok",
        "trade_date": "2026-10-25",
        "computed_at": now,
        "symbol": "600036.SH",
        "target_shares": 100,
    }
    assert target_state(wrong, trade_date="2026-10-26", now_ts=now)[0] == "wrong_date"


def test_目标状态_负股数与坏时间戳一律无效() -> None:
    now = time.time()
    negative = {
        "status": "ok",
        "trade_date": "2026-10-26",
        "computed_at": now,
        "symbol": "600036.SH",
        "target_shares": -100,
    }
    assert target_state(negative, trade_date="2026-10-26", now_ts=now)[0] == "invalid"
    bad_time = {
        "status": "ok",
        "trade_date": "2026-10-26",
        "computed_at": "not-a-number",
        "symbol": "600036.SH",
        "target_shares": 100,
    }
    assert target_state(bad_time, trade_date="2026-10-26", now_ts=now)[0] == "invalid"


def test_目标股数归一_负数按零() -> None:
    assert normalize_target_shares(1000) == 1000
    assert normalize_target_shares("1000.0") == 1000
    assert normalize_target_shares(-500) == 0


def test_心跳新鲜度() -> None:
    now = time.time()
    assert heartbeat_is_fresh(str(now - 10), now_ts=now) is True
    assert heartbeat_is_fresh(str(now - 200), now_ts=now) is False
    assert heartbeat_is_fresh(None, now_ts=now) is False
    assert heartbeat_is_fresh("garbage", now_ts=now) is False
    assert heartbeat_is_fresh(f'{{"ts": {now - 5}}}', now_ts=now) is True


def test_请求载荷与交易日期() -> None:
    payload = build_request_payload(
        tenant_id="t",
        user_id="u",
        strategy_id="s",
        run_id="run_1",
        trade_date="2026-10-26",
        phase="BUY",
        request_id="req_1",
    )
    assert payload["trade_date"] == "2026-10-26"
    assert payload["phase"] == "BUY"
    assert payload["request_id"] == "req_1"
    assert trade_date_str()  # 非空
