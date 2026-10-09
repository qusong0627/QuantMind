"""QQ 告警摘要（``backend.shared.qq_digest``）单测：批量折叠、上限折叠、分组、永不抛。

背景（2026-10-09）：市场跳水 6 分钟推了 26 条 QQ（一条一只票）。摘要把同窗口
的批量告警合并成一条；下面的测试钉住合并口径与安全边界。
"""

from __future__ import annotations

import pytest

from backend.shared import qq_digest as qd

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """隔离模块全局缓冲与真实网络：不起定时器、不发 QQ。"""
    sent: list[tuple[str, str]] = []

    def _capture(title, content="", channel="default"):
        sent.append((title, content))

    monkeypatch.setattr("backend.shared.qq_notify.notify_async", _capture)
    monkeypatch.setattr(qd, "_start_timer", lambda *a, **k: None)
    qd.flush_all()  # 清掉其它测试可能残留的缓冲（此时 notify 已被捕获，不会外发）
    yield sent
    qd.flush_all()


def _enqueue(key="holding:default:1", line="▼ 麦格米特(SZ002851) 盘中异动"):
    qd.enqueue(digest_key=key, header="持仓预警", line=line)


def test_burst_folds_into_one_message(_isolate):
    # Arrange / Act：同窗口 6 条（2026-10-09 的真实形态）
    for i in range(6):
        _enqueue(line=f"▼ 票{i}(SZ00000{i}) 大幅下行 -5.0%")
    qd.flush_all()

    # Assert：一条消息，标题带条数，正文逐行 + 尾句
    assert len(_isolate) == 1
    title, body = _isolate[0]
    assert title == "持仓预警 · 6 条"
    assert len(body.splitlines()) == 8  # 6 行明细 + 空行 + 尾句
    assert "票3(SZ000003)" in body
    assert body.rstrip().endswith("详情见交易台。")


def test_lines_beyond_cap_are_folded(_isolate):
    # 超展示上限折叠为「另有 N 条」，明细不全展
    for i in range(20):
        _enqueue(line=f"行{i}")
    qd.flush_all()

    title, body = _isolate[0]
    assert title == "持仓预警 · 20 条"
    assert "行14" in body and "行15" not in body
    assert "另有 5 条" in body


def test_buffer_overflow_counts_but_keeps_memory_bounded(_isolate):
    # 极端风暴：缓冲上限外的行只计数不存文本（内存有界），总数仍然如实
    for i in range(qd.MAX_BUFFERED_LINES + 10):
        _enqueue(line=f"行{i}")
    qd.flush_all()

    title, body = _isolate[0]
    assert title == f"持仓预警 · {qd.MAX_BUFFERED_LINES + 10} 条"
    # 展示 15 行，其余 55 条（60-15 存下的 + 10 缓冲外）折叠
    hidden = qd.MAX_BUFFERED_LINES + 10 - qd.MAX_LINES
    assert f"另有 {hidden} 条" in body


def test_separate_keys_flush_separately(_isolate):
    _enqueue(key="holding:default:1", line="A")
    qd.enqueue(digest_key="sentinel:CN", header="实时情报 · A股", line="B")
    qd.flush_all()

    titles = sorted(t for t, _ in _isolate)
    assert titles == ["实时情报 · A股 · 1 条", "持仓预警 · 1 条"]


def test_empty_line_or_key_is_ignored(_isolate):
    qd.enqueue(digest_key="", header="h", line="x")
    qd.enqueue(digest_key="k", header="h", line="   ")
    assert qd.flush_all() == 0
    assert _isolate == []


def test_flush_is_idempotent_per_entry(_isolate):
    # 定时器与手动 flush 竞态：同一个条目只发一次（摘除后旧引用静默退出）
    _enqueue(line="A")
    with qd._lock:
        key, entry = next(iter(qd._buffers.items()))
    qd.flush_all()
    qd._flush_entry(key, entry)  # 迟到的定时器回调
    assert len(_isolate) == 1


def test_send_failure_never_raises(monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("qq down")

    monkeypatch.setattr("backend.shared.qq_notify.notify_async", _boom)
    monkeypatch.setattr(qd, "_start_timer", lambda *a, **k: None)
    _enqueue(line="A")
    assert qd.flush_all() == 1  # 冲刷动作本身不抛
