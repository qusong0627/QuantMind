"""真单成交回执 QQ 推送（``tdx_push_service._notify_fill_qq``）：首次转 FILLED 才推、
排版一眼可读、绝不反噬落库。

背景（2026-10-09）：QM 原生栈的成交确认走 notifications ``level=success``——
``qq_notify.alert_async`` 只放行 warning/error，**成交回执从来到不了 QQ**
（只有拒单 error 会推）。TDX 真单的落库权威点是 ``TdxPushService.
_sync_orders_to_pg``（trade 任务 30s 轮询桥当日委托、刷新 orders 表），本模块把
成交回执补在这一层：**真单成交 → QQ**。

口径要点（钉在测试里）：
* 执行面用词保留「买入/卖出」（展示面才用「靠前/靠后」这类位置词）。
* 代码统一前缀式（与前端/委托台账一致）；桥的 stock_code 是后缀式，展示前转换。
* 只推**真单**：模拟成交会镜像成真单，模拟层再推一次 = 同一笔两条消息。
* 名称解析/推送失败都只降级，**绝不让通知反噬成交落库**。
"""

from __future__ import annotations

from backend.services.live_trading.services import tdx_push_service as tps


def _fill(**over) -> dict:
    base = {
        "side": "buy",
        "symbol": "600036.SH",
        "filled_volume": 300,
        "filled_price": 36.5,
        "exchange_order_id": "160356",
    }
    base.update(over)
    return base


def _capture(monkeypatch, name="招商银行"):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "backend.shared.qq_notify.notify_async",
        lambda title, content="": calls.append((title, content)),
    )
    if name is None:

        def _boom(_symbol):
            raise RuntimeError("mapper down")

        monkeypatch.setattr("backend.shared.stock_name_mapper.resolve_name", _boom)
    else:
        monkeypatch.setattr(
            "backend.shared.stock_name_mapper.resolve_name", lambda _symbol: name
        )
    return calls


def test_fill_push_message_is_glanceable(monkeypatch):
    # Arrange / Act
    calls = _capture(monkeypatch)
    tps._notify_fill_qq(_fill())

    # Assert：标题一眼可读（方向 + 名称(前缀码) + 量价），正文给金额与委托号
    assert len(calls) == 1
    title, content = calls[0]
    assert "买入" in title
    assert "招商银行(SH600036)" in title
    assert "300股" in title.replace(" ", "")
    assert "¥36.50" in title
    assert "金额 ¥10,950" in content
    assert "160356" in content


def test_sell_side_keeps_execution_wording(monkeypatch):
    # 执行面（成交回报）保留「卖出」，不做位置化含糊
    calls = _capture(monkeypatch)
    tps._notify_fill_qq(_fill(side="sell"))
    assert "卖出" in calls[0][0]


def test_falls_back_to_symbol_when_name_mapper_down(monkeypatch):
    # 名称解析失败 → 标题退化为符号，仍然要推（告警面不该被旁路组件拖死）
    calls = _capture(monkeypatch, name=None)
    tps._notify_fill_qq(_fill())
    assert len(calls) == 1
    assert "SH600036" in calls[0][0]


def test_never_raises_when_notify_explodes(monkeypatch):
    # 推送层炸了不许带走成交落库（同步循环里抛出去会中断整轮账户同步）

    def _boom(*_a, **_k):
        raise RuntimeError("qq down")

    monkeypatch.setattr("backend.shared.qq_notify.notify_async", _boom)
    monkeypatch.setattr(
        "backend.shared.stock_name_mapper.resolve_name", lambda _s: "招商银行"
    )
    tps._notify_fill_qq(_fill())  # 不抛即通过


def test_transition_predicate_only_fires_on_first_full_fill():
    """桥是成交权威源，30s 轮询反复刷同一行——只有首次转 FILLED 才是新事件。"""
    assert tps._fill_transitioned(None, "filled", 300) is True  # 首次见到即已成
    assert tps._fill_transitioned("submitted", "filled", 300) is True  # 状态推进
    assert tps._fill_transitioned("filled", "filled", 300) is False  # 刷新，不重推
    assert tps._fill_transitioned("filled", "filled", 0) is False
    assert tps._fill_transitioned("submitted", "submitted", 0) is False


def test_notify_call_site_sits_after_commit():
    """源码守卫：成交推送必须排在 ``db.commit()`` 之后。

    排在前面的话，事务后段一失败（回滚）就会推一笔**没落库**的成交；下轮重来
    再推一次 = 同一笔两条。事件先在 ``_sync_orders_to_pg`` 里收集，commit 后才推。
    """
    import inspect

    src = inspect.getsource(tps.TdxPushService.sync_account_to_pg)
    idx_sync = src.index("_sync_orders_to_pg")
    idx_commit = src.index("await db.commit()", idx_sync)
    idx_notify = src.index("_notify_fill_qq(", idx_commit)
    assert idx_commit < idx_notify
