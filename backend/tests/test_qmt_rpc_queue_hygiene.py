"""大 QMT 桥 RPC 队列卫生工具单测（纯桩，无网络）。

锁定两条机构级护栏：
1. **订单类方法永不删除**——即使显式传入 --drop-methods 也拒绝（main 层）且 trim 层跳过；
2. 清理只按显式方法白名单 / 保留最近 N 条执行，无差别清空被拒绝。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "qmt_rpc_queue_hygiene.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("qmt_rpc_queue_hygiene", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakeRedis:
    def __init__(self, items):
        self.items = list(items)

    def ping(self):
        return True

    def close(self):
        pass

    def lrange(self, key, start, end):
        return list(self.items)

    def lrem(self, key, count, value):
        for i, it in enumerate(self.items):
            if it == value:
                del self.items[i]
                return 1
        return 0


def _entry(method: str, request_id: str = "r1") -> bytes:
    from bigqmt_signal_trader.redis_rpc import encode_rpc_request_payload

    return encode_rpc_request_payload(
        {"schema_version": 1, "request_id": request_id, "account_id": "1", "method": method,
         "params": {}, "ttl_seconds": 60}
    ).encode("utf-8")


@pytest.mark.unit
def test_survey_classifies_and_flags_order_methods():
    mod = _load_module()
    fake = _FakeRedis([_entry("query_stock_orders", "a"), _entry("query_stock_asset", "b"),
                       _entry("order_stock", "c")])
    hist, order_rows = mod.survey(fake, "q")
    assert hist["query_stock_orders"] == 1 and hist["query_stock_asset"] == 1
    # 订单类方法被识别（无论其是否在 kit 常量里，兜底按名字含 order 判定）
    assert any(row["method"] == "order_stock" for row in order_rows)


@pytest.mark.unit
def test_trim_never_deletes_order_methods_even_if_requested():
    mod = _load_module()
    fake = _FakeRedis([_entry("query_stock_orders", "a"), _entry("order_stock", "c"),
                       _entry("subscribe_whole_quote", "d")])
    stats = mod.trim(fake, "q", drop_methods={"query_stock_orders", "order_stock"}, keep_last=None)
    methods = [mod._decode_entry(x)["method"] for x in fake.items]
    # query_stock_orders 命中白名单被删；order_stock 属订单类硬保护；subscribe 不在白名单
    assert methods == ["order_stock", "subscribe_whole_quote"]
    assert stats["removed"] == 1 and stats["kept_order_methods"] == 1


@pytest.mark.unit
def test_cli_rejects_order_methods_in_drop_list(monkeypatch, capsys):
    """--drop-methods 含订单类（kit 常量或名字启发）→ 拒绝执行（硬护栏）。"""
    mod = _load_module()
    fake = _FakeRedis([_entry("passorder")])
    monkeypatch.setattr(mod, "_bridge_redis_params", lambda: {"host": "h", "port": 1, "db": 0, "password": None})
    monkeypatch.setattr(mod, "_client", lambda p: fake)
    monkeypatch.setattr(sys, "argv", ["hygiene", "--account", "1", "--trim", "--drop-methods", "passorder,order_stock"])
    rc = mod.main()
    assert rc == 2
    assert "订单类" in capsys.readouterr().out


@pytest.mark.unit
def test_trim_keep_last_preserves_newest():
    mod = _load_module()
    items = [_entry("query_stock_asset", f"r{i}") for i in range(5)]
    fake = _FakeRedis(items)
    stats = mod.trim(fake, "q", drop_methods=set(), keep_last=2)
    assert stats["removed"] == 3
    assert len(fake.items) == 2
    assert [mod._decode_entry(x)["request_id"] for x in fake.items] == ["r3", "r4"]


@pytest.mark.unit
def test_cli_requires_explicit_scope(monkeypatch, capsys):
    mod = _load_module()
    fake = _FakeRedis([_entry("query_stock_asset")])
    monkeypatch.setattr(mod, "_bridge_redis_params", lambda: {"host": "h", "port": 1, "db": 0, "password": None})
    monkeypatch.setattr(mod, "_client", lambda p: fake)
    monkeypatch.setattr(sys, "argv", ["hygiene", "--account", "1", "--trim"])
    rc = mod.main()
    assert rc == 2
    out = capsys.readouterr().out
    assert "拒绝无差别清空" in out
