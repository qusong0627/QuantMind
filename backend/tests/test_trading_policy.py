"""阶段 1：`TradingPolicy` 单一配置源的契约（先红后绿）。

设计见 `docs/实盘智能体交易策略重构_设计与分阶段_20261028.md` §2。

四条纪律：
1. 缺文件 = 内置默认（开源/测试可跑），不是错误；
2. 文件存在但坏/未知字段/数值非法 = `TradingPolicyError`（失败关闭，不静默回退）；
3. per-agent 只写差异，其余继承 `default`；键按大小写/空白归一；
4. 缓存按 `(path, mtime_ns, size)`；文件改动下一轮生效，无需重启。
"""

from __future__ import annotations

import json
import os

import pytest

from backend.shared.trading_policy import (
    AgentPolicy,
    MirrorPolicy,
    TradingPolicy,
    TradingPolicyError,
    clear_cache,
    load_trading_policy,
    parse_trading_policy,
    policy_path,
)


def _write(tmp_path, doc) -> str:
    p = tmp_path / "trading_policy.json"
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return str(p)


@pytest.fixture(autouse=True)
def _clear():
    clear_cache()
    yield
    clear_cache()


def test_missing_file_returns_builtin_defaults(tmp_path):
    p = load_trading_policy(tmp_path / "absent.json")
    assert p.source == "builtin"
    assert p.default == AgentPolicy()
    assert p.agents == {}
    assert p.mirror == MirrorPolicy()
    assert p.default.quota == 100_000.0
    assert p.default.per_stock_pct == 0.20
    assert p.default.max_new_buys == 3
    assert p.default.allow_out_of_pool is False
    assert p.default.profit_first is False
    assert p.mirror.max_order_value == 10_000.0


def test_full_file_parses_and_inherits(tmp_path):
    path = _write(
        tmp_path,
        {
            "default": {
                "quota": 150_000,
                "per_stock_pct": 0.5,
                "max_new_buys": 4,
                "allow_out_of_pool": True,
                "profit_first": False,
                "score_leaders_limit": 10,
            },
            "agents": {
                "flash": {
                    "quota": 200_000,
                    "per_stock_pct": 1.0,
                    "max_new_buys": 9,
                    "allow_out_of_pool": True,
                    "profit_first": True,
                    "score_leaders_limit": 20,
                },
                "half": {"quota": 80_000},
            },
            "mirror": {
                "max_order_value": 200_000,
                "max_daily_value": 1_200_000,
                "max_daily_symbols": 40,
                "max_daily_orders": 80,
                "max_slippage_pct": 0.02,
            },
        },
    )
    p = load_trading_policy(path)
    assert p.source == path
    flash = p.for_agent("flash")
    assert flash.quota == 200_000.0
    assert flash.per_stock_pct == 1.0
    assert flash.max_new_buys == 9
    assert flash.profit_first is True
    assert flash.score_leaders_limit == 20

    half = p.for_agent("half")
    assert half.quota == 80_000.0
    assert half.per_stock_pct == 0.5  # 继承 default
    assert half.max_new_buys == 4
    assert half.allow_out_of_pool is True  # 继承 default

    assert p.for_agent("unknown") == p.default
    assert p.for_agent("FLASH") == flash  # 大小写归一
    assert p.mirror.max_order_value == 200_000.0
    assert p.mirror.max_daily_orders == 80


def test_agent_key_normalized_for_whitespace(tmp_path):
    path = _write(tmp_path, {"agents": {" flash ": {"quota": 200_000}}})
    assert load_trading_policy(path).for_agent("flash").quota == 200_000.0


def test_partial_agent_inherits_default(tmp_path):
    path = _write(
        tmp_path,
        {
            "default": {"quota": 120_000, "profit_first": True},
            "agents": {"pro": {"quota": 200_000}},
        },
    )
    pro = load_trading_policy(path).for_agent("pro")
    assert pro.quota == 200_000.0
    assert pro.profit_first is True


def test_cache_returns_same_object_until_cleared(tmp_path):
    path = _write(tmp_path, {"default": {"quota": 100_000}})
    first = load_trading_policy(path)
    assert load_trading_policy(path) is first
    _write(tmp_path, {"default": {"quota": 200_000}})
    # mtime 未变（同秒写入）时仍走缓存；显式清缓存后必须读到新值。
    clear_cache()
    assert load_trading_policy(path).default.quota == 200_000.0


def test_env_override_path(monkeypatch, tmp_path):
    path = _write(tmp_path, {"default": {"quota": 123_456}})
    monkeypatch.setenv("QM_TRADING_POLICY_FILE", path)
    assert policy_path() == __import__("pathlib").Path(path)
    assert load_trading_policy().default.quota == 123_456.0


def test_bad_json_file_fails_closed(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(TradingPolicyError):
        load_trading_policy(p)


@pytest.mark.parametrize(
    "raw",
    [
        [],
        {"unknown_top": {}},
        {"default": {"quota": 0}},
        {"default": {"quota": -1}},
        {"default": {"per_stock_pct": 0}},
        {"default": {"per_stock_pct": 1.5}},
        {"default": {"max_new_buys": -1}},
        {"default": {"max_new_buys": 1.5}},
        {"default": {"allow_out_of_pool": "true"}},
        {"default": {"unknown_field": 1}},
        {"agents": {"flash": {"quota": "x"}}},
        {"agents": {"": {"quota": 1}}},
        {"agents": ["flash"]},
        {"mirror": {"max_order_value": 0}},
        {"mirror": {"max_daily_symbols": -1}},
        {"mirror": {"max_slippage_pct": 1.1}},
        {"mirror": {"unknown": 1}},
    ],
)
def test_invalid_documents_fail_closed(raw):
    with pytest.raises(TradingPolicyError):
        parse_trading_policy(raw)


def test_duplicate_agent_after_normalization_is_rejected():
    with pytest.raises(TradingPolicyError):
        parse_trading_policy(
            {"agents": {"flash": {"quota": 1}, " FLASH ": {"quota": 2}}}
        )


def test_empty_agents_and_mirror_are_allowed(tmp_path):
    path = _write(tmp_path, {})
    p = load_trading_policy(path)
    assert p.default == AgentPolicy()
    assert p.mirror == MirrorPolicy()
