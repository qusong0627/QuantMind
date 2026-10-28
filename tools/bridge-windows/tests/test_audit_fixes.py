"""代码审计（2026-10-23）HIGH/MEDIUM 修复的回归测试。

    HIGH-1 单飞共享 Task：任一等待者取消不得毒死其他等待者
    HIGH-2 裸码歧义：000001.SH（指数）与 000001.SZ（平安银行）不得串数据
    HIGH-3 aidata 卡死：超时标记 + 冷却期快速失败 + 锁等待有上限
    MEDIUM read_token 兼容 CRLF ini

运行: cd bridge-windows && python3 tests/test_audit_fixes.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api.routes import _run_aidata  # noqa: E402
from src.tdx.aidata import AidataService, AidataUnavailable  # noqa: E402
from src.ths.client import ThsClient  # noqa: E402


class _SlowClient(ThsClient):
    def __init__(self, **kw):
        super().__init__(enabled=True, **kw)
        self.calls = 0
        self.seen: list[tuple[str, dict]] = []

    async def _do_call(self, http_method, path, query, timeout_s=None):
        self.calls += 1
        self.seen.append((path, dict(query)))
        await asyncio.sleep(0.15)
        return {"ok": True}


async def _test_singleflight_cancel():
    c = _SlowClient()
    t1 = asyncio.create_task(c.call("quote", {"codes": "600519"}))
    await asyncio.sleep(0.02)
    t2 = asyncio.create_task(c.call("quote", {"codes": "600519"}))
    await asyncio.sleep(0.02)
    t1.cancel()  # owner 取消
    try:
        await t1
    except asyncio.CancelledError:
        pass
    assert await t2 == {"ok": True}, "等待者被 owner 取消连坐（HIGH-1）"
    assert c.calls == 1, f"单飞失效，打了 {c.calls} 次上游"
    # 任务完成后 inflight 必须清空
    await asyncio.sleep(0.05)
    assert c._inflight == {}, c._inflight
    print("  ✅ 单飞：owner 取消不影响等待者")


async def _test_bare_code_ambiguity():
    c = _SlowClient(cache_enabled=False)
    await c.call("quote", {"codes": "000001.SH"})   # 上证指数
    await c.call("quote", {"codes": "000001.SZ"})   # 平安银行
    p1, p2 = c.seen[0], c.seen[1]
    assert p1 != p2, f"指数与个股串成同一上游请求: {p1} vs {p2}"
    assert ThsClient._bare("000001.SH") == "000001.SH"
    assert ThsClient._bare("000001.SZ") == "000001"
    assert ThsClient._bare("600519.SH") == "600519"
    assert ThsClient._bare("880081.SH") == "880081"  # 板块码仍去后缀
    print("  ✅ 裸码歧义：指数不去后缀、缓存键不串")


async def _test_aidata_timeout_marks_stuck():
    class _Svc:
        marked = 0
        label = ""

        def mark_timeout(self, label=""):
            type(self).marked += 1
            type(self).label = label

    t0 = time.monotonic()
    try:
        await _run_aidata(_Svc(), lambda: time.sleep(0.4), timeout=0.05)
    except AidataUnavailable as exc:
        assert "超时" in str(exc), exc
    else:
        raise AssertionError("超时未转成 AidataUnavailable")
    elapsed = time.monotonic() - t0
    assert elapsed < 0.35, f"wait_for 没有生效（{elapsed:.2f}s）"
    assert _Svc.marked == 1, "超时未标记上游卡死"

    # 冷却期内 call() 必须快速失败，不再碰 DLL
    with tempfile.TemporaryDirectory() as td:
        svc = AidataService(enabled=False,
                            ini_path=os.path.join(td, "TdxAiData.ini"))
        svc._tqs = object()
        svc.mark_timeout("unit")
        t0 = time.monotonic()
        try:
            svc.call("get_market_snapshot", {})
        except AidataUnavailable as exc:
            assert "冷却中" in str(exc), exc
        else:
            raise AssertionError("卡死冷却期内 call() 未快速失败")
        assert time.monotonic() - t0 < 0.2
    print("  ✅ aidata 超时：标记卡死 + 冷却期快速失败")


def _test_read_token_crlf():
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "TdxAiData.ini")
        with open(path, "wb") as fh:
            fh.write(b"[Token]\r\ntoken=ABCDEFGHIJKLMNOP\r\n[TcHqHost]\r\nx=1\r\n")
        svc = AidataService(enabled=False, ini_path=path)
        assert svc.read_token() == "ABCDEFGHIJKLMNOP", repr(svc.read_token())
    print("  ✅ CRLF ini 能读回 token")


async def _test_outbound_gate():
    c = _SlowClient(max_concurrency=2, gate_wait_s=0.05)
    tasks = [asyncio.create_task(c.call("quote", {"codes": f"60051{i}"}))
             for i in range(4)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    ok = [r for r in results if isinstance(r, dict)]
    rejected = [r for r in results
                if isinstance(r, Exception) and "并发已满" in str(r)]
    assert len(ok) == 2 and len(rejected) == 2, results
    assert c._stats["gate_rejects"] == 2, c._stats
    assert c.calls == 2, f"闸门外的请求仍打到了上游: {c.calls}"
    print("  ✅ 出站并发闸门：超限快速失败，不打上游")


def _test_canon_code():
    from src.api.routes import _canon_code
    assert _canon_code("600519") == "600519.SH"
    assert _canon_code("sh600519") == "600519.SH"
    assert _canon_code("600519.SH") == "600519.SH"
    assert _canon_code("002415") == "002415.SZ"
    assert _canon_code("300750") == "300750.SZ"
    assert _canon_code("688981") == "688981.SH"
    assert _canon_code("430047") == "430047.BJ"
    assert _canon_code("880081") == "880081.SH"  # 板块码
    assert ThsClient._check_code("SH600519") == "600519.SH"
    assert ThsClient._check_code("600519") == "600519"
    c = ThsClient(enabled=True)
    path, _q = c._build("board_quotes", {"code": "bc9e"})
    assert path == "/api/board/BC9E/quotes", path
    try:
        c._build("board_quotes", {"code": "../etc"})
    except Exception:  # noqa: BLE001
        pass
    else:
        raise AssertionError("非法板块代码未拒绝")
    print("  ✅ 代码归一：裸码/前缀式/后缀式 都能落到正确市场（板块码单列）")


def _test_timeout_circuit():
    c = ThsClient(enabled=True)
    for _ in range(9):
        c._fail("timeout", timeout=True)
    assert c._open_until == 0.0, "9 次超时不应熔断（阈值 10）"
    c._fail("timeout", timeout=True)
    assert c._open_until > 0.0, "第 10 次超时应熔断"
    c2 = ThsClient(enabled=True)
    for _ in range(5):
        c2._fail("transport")
    assert c2._open_until > 0.0, "5 次传输错误应熔断"
    print("  ✅ 熔断分权：传输错误 5 次 / 超时 10 次")


def _test_order_notify():
    from src.core.types import PriceType, Side
    from src.tdx.client import TdxClient
    from src.core.trade_plan import TradePlan

    class _Rec(TdxClient):
        def __init__(self):
            super().__init__()
            self.last = None

        def _call(self, method, params, **kw):
            self.last = (method, params)
            return {"Value": "1", "Msg": "待确认"}

    c = _Rec()
    c.order_stock(0, "600519.SH", Side.BUY, 100, PriceType.MANUAL, 1200.0, notify=1)
    assert c.last[0] == "order_stock" and c.last[1]["notify"] == 1, c.last
    plan = TradePlan.from_dict({"plan_id": "t", "orders": [
        {"stock_code": "600519.SH", "side": "buy", "volume": 100, "notify": 1}]})
    assert plan.orders[0].notify == 1
    print("  ✅ 下单确认参数：notify 透传到 TQ，计划可按单覆盖")


async def _run():
    await _test_singleflight_cancel()
    await _test_bare_code_ambiguity()
    await _test_aidata_timeout_marks_stuck()
    _test_read_token_crlf()
    await _test_outbound_gate()
    _test_canon_code()
    _test_timeout_circuit()
    _test_order_notify()
    print("✅ 审计修复回归：HIGH-1/2/3 / CRLF / 闸门 / 代码归一 / 熔断分权 全部通过")


if __name__ == "__main__":
    asyncio.run(_run())
