"""QMT 执行端链路延迟探测（容器内一条命令）。

分段测：RPC 往返（ping/查委托/查成交）→ 下单受理（submit ack）→ 成交检测
→ 撤单受理。``--order`` 会真下一笔小额激进限价单（默认 600036.SH 100 股，
参考价 +1%），15s 未成交自动撤单——不想动单就别加这个参数。

用法（容器内）::

    python backend/scripts/probe_qmt_latency.py            # 只测只读 RPC
    python backend/scripts/probe_qmt_latency.py --order    # 加测下单/成交/撤单

口径提醒：ping 的 RTT 里含 QMT 面板打印的 GIL 等待（上游 #104），抖动大；
``query_orders``/``submit`` 的数值更能代表桥的真实服务能力。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import time
from typing import Any, Callable, Coroutine

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from backend.services.live_trading.services.qmt_exec_client import (  # noqa: E402
    get_qmt_exec_client,
)


def _stats(name: str, samples: list[float]) -> None:
    ms = sorted(s * 1000.0 for s in samples)
    if not ms:
        print("%-14s （无样本）" % name)
        return
    print(
        "%-14s n=%d  min=%.0f  p50=%.0f  avg=%.0f  max=%.0f ms"
        % (name, len(ms), ms[0], statistics.median(ms), sum(ms) / len(ms), ms[-1])
    )


async def _timed(label: str, fn: Callable[[], Coroutine[Any, Any, Any]]) -> Any:
    started = time.perf_counter()
    result = await fn()
    print("  %s: %.0f ms" % (label, (time.perf_counter() - started) * 1000.0))
    return result


async def probe_readonly(client, rounds: int) -> dict[str, Any]:
    print("== 只读 RPC（%d 轮）==" % rounds)
    pings: list[float] = []
    for _ in range(rounds):
        started = time.perf_counter()
        await client.ping()
        pings.append(time.perf_counter() - started)
    _stats("ping", pings)

    queries: list[float] = []
    for _ in range(rounds):
        started = time.perf_counter()
        orders = await client.query_orders()
        queries.append(time.perf_counter() - started)
    _stats("query_orders", queries)

    trades_rtt: list[float] = []
    for _ in range(max(1, rounds // 3)):
        started = time.perf_counter()
        trades = await client.query_trades()
        trades_rtt.append(time.perf_counter() - started)
    _stats("query_trades", trades_rtt)
    return {"orders": orders, "trades": trades}


async def probe_order(client, symbol: str, quantity: int) -> None:
    from backend.services.live_trading.services.qmt_account_sync_task import (
        batch_quantdb_last_close,
    )

    closes = await asyncio.to_thread(batch_quantdb_last_close, [symbol])
    ref = float(closes.get(symbol) or 0)
    if ref <= 0:
        print("!! 参考价取不到（QuantDB 无 %s），跳过下单段" % symbol)
        return
    limit_price = round(ref * 1.01, 2)
    cid = "lat-%d" % int(time.time())
    print("== 下单段：BUY %s %d 股 限价 %.2f（参考价 %.2f）cid=%s ==" % (
        symbol, quantity, limit_price, ref, cid))

    result = await _timed(
        "submit ack",
        lambda: client.submit_order(
            symbol=symbol,
            side="BUY",
            quantity=quantity,
            order_type="LIMIT",
            price=limit_price,
            client_order_id=cid,
        ),
    )
    order_id = str(result.get("order_id") or "")
    print("  委托号 %s" % order_id)

    started = time.perf_counter()
    filled = False
    for _ in range(60):
        orders = await client.query_orders()
        for order in orders:
            if str(order.get("order_id")) == order_id and str(order.get("status")) in (
                "FILLED",
                "PARTIAL_FILLED",
            ):
                print("  成交检测：%.0f ms（status=%s 价=%s）" % (
                    (time.perf_counter() - started) * 1000.0,
                    order.get("status"),
                    order.get("traded_price"),
                ))
                filled = True
                break
        if filled:
            break
        await asyncio.sleep(0.25)
    if not filled:
        print("  15s 未成交 → 测撤单")
        await _timed("cancel ack", lambda: client.cancel_order(order_id=order_id, symbol=symbol))


async def main() -> int:
    parser = argparse.ArgumentParser(description="QMT 桥延迟探测")
    parser.add_argument("--rounds", type=int, default=10, help="只读 RPC 取样轮数")
    parser.add_argument("--order", action="store_true", help="加测下单/成交/撤单（真单小额）")
    parser.add_argument("--symbol", default="600036.SH", help="下单段标的")
    parser.add_argument("--quantity", type=int, default=100, help="下单段数量（股）")
    args = parser.parse_args()

    client = get_qmt_exec_client()
    await client.refresh_settings()  # 生产进程里由 qmt_exec_poller 每轮刷新
    cfg = client.effective_config()
    print("执行端 enabled=%s account=%s" % (cfg.get("enabled"), cfg.get("account_id")))

    await probe_readonly(client, max(1, args.rounds))
    if args.order:
        await probe_order(client, args.symbol, args.quantity)
    return 0


if __name__ == "__main__":
    code = asyncio.run(main())
    sys.stdout.flush()
    sys.stderr.flush()
    # RPC 超时后工作线程会卡在桥连接上，解释器收尾的 shutdown_default_executor
    # 可能挂死 >45s（与 qmt_bridge_selftest 同因），直接硬退。
    os._exit(code)
