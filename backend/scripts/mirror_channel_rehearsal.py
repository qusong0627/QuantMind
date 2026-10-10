#!/usr/bin/env python3
"""P2-2③ 真单镜像通道切换预演探针（**零真单红线**）。

配合决策轮就绪面（强制轮审计行 ``context_meta->'mirror'``）完成「qmt_exec 通道
切换预演」的另一半：以一条 100 股探针单在同一实现（``real_mirror_service.
mirror_virtual_fill``）里逐道走通镜像闸门，**到「排队」为止**——时段外队列语义
（``_enqueue``）在 ``_submit_payload``（唯一触柜点）之前返回，所以本探针在交易
时段外运行**物理上无法**把任何委托送到柜台。

为什么需要直探而不是只跑决策轮：决策轮**只在交易时段发腿**（非交易时段
``refusing_submitter``，腿留纸上），盘后强制轮拿到的逐腿段是 ``unknown``（拒发），
走不到镜像链；而时段内**全开镜像 = 真提交**（那是上线动作，不是演练）。两件互补：
决策轮就绪面证明「开关切了审计行如实翻转」，本探针证明「闸门链本身放行/拦截
的分岔正确、排队语义不触柜」。

探针序列（``--probe all``）：

  A 关态   ``mirror:enabled=0`` → 期望 ``skipped:mirror_disabled``
  B 开态   ``mirror:enabled=1`` → 期望 ``queued:outside_trading_hours`` +
           队列 +1（cid ``mir-rehearsal-…``）——启用/市场/白名单/黑名单/通道就绪/
           时段门在同一实现里逐道走通
  C 急停   ``mirror:kill=1`` → 期望 ``skipped:mirror_disabled``
  D 排空器 时段外 ``drain_mirror_queue`` → 期望 ``{"status":"outside_trading_hours",
           "drained":0}``（排空器时段外不触柜）
  收尾    purge 本探针 cid（``mir-rehearsal-`` 前缀）+ 还原 enabled/kill 原值

**时段内一律拒跑**（``status`` / ``purge`` 除外）：B 展开门后若在交易时段，直接
调用就是真提交。这是本脚本最重要的安全不变量，不允许放宽。

用法（容器内）::

    docker exec -w /app/backend -e PYTHONPATH=/app quantmind \\
        python scripts/mirror_channel_rehearsal.py --probe status
    docker exec ... python scripts/mirror_channel_rehearsal.py --probe all --price 38.50
    docker exec ... python scripts/mirror_channel_rehearsal.py --probe purge

退出码：0=全 PASS；1=有 FAIL；2=前置不满足（时段内/未开闸等，fail-closed 拒跑）。

留痕如实：A/C 会在 ``mirror:skipped:{当日}`` 记账 2 笔 ``mirror_disabled``——那是
**真实发生过的跳过**，日报镜像段可见；本探针不清洗这两笔（清洗审计面 = 造假）。

配套 runbook：``docs/实盘全天链整改方案_20261010.md`` P2-2③。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:  # 让 `python scripts/…` 直接可跑（不依赖 PYTHONPATH）
    sys.path.insert(0, _ROOT)

_REHEARSAL_CID_PREFIX = "mir-rehearsal-"
_ENABLED_KEY = "mirror:enabled"
_KILL_KEY = "mirror:kill"
_QUEUE_KEY = "mirror:queue"
_QUEUED_SET_KEY = "mirror:queued"
_SELECTED_KEY = "broker:selected:CN"

_RESULTS: list[dict[str, Any]] = []


def _record(name: str, ok: bool, detail: str, hint: str = "") -> None:
    _RESULTS.append({"probe": name, "ok": ok, "detail": detail, "hint": hint})
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}: {detail}")
    if hint:
        print(f"       → {hint}")


def _redis() -> Any:
    from backend.services.trade_shared.redis_client import RedisClient

    rc = RedisClient()
    rc.connect()
    if rc.client is None:
        raise RuntimeError("交易 Redis（db2）连不上，预演中止")
    return rc


def _raw(rc: Any, key: str) -> str | None:
    v = rc.client.get(key)
    if isinstance(v, (bytes, bytearray)):
        return v.decode("utf-8", errors="ignore")
    return v


def _restore_key(rc: Any, key: str, prior: str | None) -> None:
    if prior is None:
        rc.client.delete(key)
    else:
        rc.client.set(key, prior)


async def _probe_fill(
    rc: Any, *, symbol: str, side: str, quantity: float, price: float
):
    """直接调用镜像链唯一实现（db=None：时段外永远到不了需要会话的 _submit_payload）。"""
    from backend.services.live_trading.services.real_mirror_service import (
        mirror_virtual_fill,
    )

    run_id = f"rehearsal-{os.getpid()}"
    return await mirror_virtual_fill(
        db=None,
        redis=rc,
        tenant_id="default",
        user_id="10000001",
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=price,
        sim_order_id="",
        run_id=run_id,
        strategy_id="",
        market="CN",
        source="rehearsal:p2-2",
        trigger="P2-2③ 通道切换预演（演练单，非策略信号——若在真实渠道见到此单应立即上报）",
    )


def _assert_skipped(receipt: Any, expect_reason: str) -> tuple[bool, str]:
    if not isinstance(receipt, dict):
        return False, f"回执不是 dict: {receipt!r}"
    status = str(receipt.get("status") or "")
    reason = str(receipt.get("reason") or "")
    if status == "skipped" and reason == expect_reason:
        return True, f"skipped/{reason}"
    return (
        False,
        f"期望 skipped/{expect_reason}，实得 {status}/{reason}（回执={receipt!r}）",
    )


async def probe_closed(rc: Any, *, symbol: str, quantity: float, price: float) -> None:
    prior = _raw(rc, _ENABLED_KEY)
    try:
        rc.client.set(_ENABLED_KEY, "0")
        receipt = await _probe_fill(
            rc, symbol=symbol, side="BUY", quantity=quantity, price=price
        )
        ok, detail = _assert_skipped(receipt, "mirror_disabled")
        _record(
            "A 关态（mirror:enabled=0）",
            ok,
            detail,
            "" if ok else "热开关读取异常？检查 Redis db2 与本脚本是否同一实例",
        )
    finally:
        _restore_key(rc, _ENABLED_KEY, prior)


async def probe_kill(rc: Any, *, symbol: str, quantity: float, price: float) -> None:
    prior = _raw(rc, _KILL_KEY)
    try:
        rc.client.set(_KILL_KEY, "1")
        receipt = await _probe_fill(
            rc, symbol=symbol, side="BUY", quantity=quantity, price=price
        )
        ok, detail = _assert_skipped(receipt, "mirror_disabled")
        _record(
            "C 急停（mirror:kill=1）",
            ok,
            detail,
            "" if ok else "急停未生效——先停一切实盘动作再排查",
        )
    finally:
        _restore_key(rc, _KILL_KEY, prior)


async def probe_open(rc: Any, *, symbol: str, quantity: float, price: float) -> None:
    """开态探针：全闸门放开，仅剩时段门（时段外）拦在 RPC 之前。"""
    from backend.services.live_trading.services.trading_session import is_trading_time

    if is_trading_time():
        _record(
            "B 开态（queued）",
            False,
            "拒绝在交易时段运行 B 探针（时段内全开 = 真提交，越过演练红线）",
            "交易时段外重跑（runbook 规定收市后执行）",
        )
        return

    from backend.shared.live_trading_gate import is_real_trading_enabled
    from backend.services.live_trading.services.real_mirror_service import (
        kill_switch_on,
        whitelist_allows,
    )

    if not is_real_trading_enabled():
        _record(
            "B 开态（queued）",
            False,
            "ENABLE_REAL_TRADING 未开（词表只认字面 true）",
            ".env 置 ENABLE_REAL_TRADING=true 并重建容器（docker-compose.yml:239 转发白名单）",
        )
        return
    selected = str(_raw(rc, _SELECTED_KEY) or "").strip().lower()
    if selected != "qmt_exec":
        _record(
            "B 开态（queued）",
            False,
            f"broker:selected:CN={selected or 'unset'}，非 qmt_exec",
            "先切券商（界面「券商接入」或 PUT /api/v1/broker-config/selected/CN）",
        )
        return
    if kill_switch_on(rc):
        _record("B 开态（queued）", False, "mirror:kill 处于置位状态", "先解除急停")
        return
    if not whitelist_allows(
        rc, tenant_id="default", user_id="10000001", strategy_id=""
    ):
        _record(
            "B 开态（queued）",
            False,
            "白名单不放行 default:10000001（空集合=全否）",
            "PUT /api/v1/qmt-mirror/lists 写入 default:10000001 后重试",
        )
        return

    prior = _raw(rc, _ENABLED_KEY)
    try:
        rc.client.set(_ENABLED_KEY, "1")
        receipt = await _probe_fill(
            rc, symbol=symbol, side="BUY", quantity=quantity, price=price
        )
        if not isinstance(receipt, dict):
            _record("B 开态（queued）", False, f"回执不是 dict: {receipt!r}")
            return
        status = str(receipt.get("status") or "")
        reason = str(receipt.get("reason") or "")
        cid = str(receipt.get("client_order_id") or "")
        if status != "queued" or reason != "outside_trading_hours":
            hint = (
                "mirror:config 覆盖了 queue_outside_hours=false？"
                if status == "skipped" and reason == "outside_trading_hours"
                else "逐字段核对回执，按 reason 排查"
            )
            _record(
                "B 开态（queued）",
                False,
                f"期望 queued/outside_trading_hours，实得 {status}/{reason}",
                hint,
            )
            return
        if not cid.startswith(_REHEARSAL_CID_PREFIX):
            _record("B 开态（queued）", False, f"cid 前缀异常: {cid!r}")
            return
        in_set = bool(rc.client.sismember(_QUEUED_SET_KEY, cid))
        in_list = any(
            cid in (r if isinstance(r, str) else r.decode("utf-8", "ignore"))
            for r in rc.client.lrange(_QUEUE_KEY, 0, -1)
        )
        ok = in_set and in_list
        _record(
            "B 开态（queued）",
            ok,
            f"queued/outside_trading_hours cid={cid} set={in_set} list={in_list}",
            ""
            if ok
            else "回执说已排队但队列里找不到——_enqueue 半失败？立即人工核对 mirror:queue",
        )
    finally:
        _restore_key(rc, _ENABLED_KEY, prior)


async def probe_drain(rc: Any) -> None:
    from backend.services.live_trading.services.real_mirror_service import (
        drain_mirror_queue,
    )

    try:
        result = await drain_mirror_queue(rc, limit=20)
    except Exception as exc:  # noqa: BLE001 - 探针如实报
        _record("D 排空器（时段外）", False, f"drain 抛异常: {exc}")
        return
    ok = (
        isinstance(result, dict)
        and result.get("status") == "outside_trading_hours"
        and int(result.get("drained") or 0) == 0
    )
    _record(
        "D 排空器（时段外）",
        ok,
        f"{result!r}",
        ""
        if ok
        else "时段外排空器未按预期短路——若 status=disabled 说明 mirror_enabled 已关，仍安全；其余形态需人工核对",
    )


def probe_purge(rc: Any) -> None:
    """清掉本探针（mir-rehearsal- 前缀）的所有队列残留；绝不碰其他条目。"""
    removed: list[str] = []
    for raw in rc.client.lrange(_QUEUE_KEY, 0, -1):
        text = raw if isinstance(raw, str) else raw.decode("utf-8", "ignore")
        if _REHEARSAL_CID_PREFIX not in text:
            continue
        rc.client.lrem(_QUEUE_KEY, 1, raw)
        removed.append(text[:160])
    set_removed = 0
    for cid in rc.client.smembers(_QUEUED_SET_KEY):
        cid_s = cid if isinstance(cid, str) else cid.decode("utf-8", "ignore")
        if cid_s.startswith(_REHEARSAL_CID_PREFIX):
            rc.client.srem(_QUEUED_SET_KEY, cid_s)
            set_removed += 1
    left = int(rc.client.llen(_QUEUE_KEY))
    ok = set_removed == len(removed) and not any(
        _REHEARSAL_CID_PREFIX
        in (r if isinstance(r, str) else r.decode("utf-8", "ignore"))
        for r in rc.client.lrange(_QUEUE_KEY, 0, -1)
    )
    _record(
        "PURGE 演练残留",
        ok,
        f"list 移除={len(removed)} set 移除={set_removed} 剩余队列长度={left}",
        ""
        if ok
        else "残留 cid 未清净——**绝不离场**：人工 LREM mirror:queue / SREM mirror:queued 后复核",
    )


def probe_status(rc: Any) -> None:
    """只读快照：预演前/后的对照面（任何时刻可跑）。"""
    from backend.services.live_trading.services.trading_session import is_trading_time
    from backend.shared.live_trading_gate import is_real_trading_enabled

    q = int(rc.client.llen(_QUEUE_KEY))
    print("── 镜像通道快照（只读）─────────────────────────────")
    print(f"ENABLE_REAL_TRADING : {is_real_trading_enabled()}")
    print(f"is_trading_time()   : {is_trading_time()}")
    print(f"{_SELECTED_KEY} : {_raw(rc, _SELECTED_KEY)!r}")
    print(f"{_ENABLED_KEY}      : {_raw(rc, _ENABLED_KEY)!r}")
    print(f"{_KILL_KEY}         : {_raw(rc, _KILL_KEY)!r}")
    print(f"whitelist           : {sorted(rc.client.smembers('mirror:whitelist'))}")
    print(f"queue len / set     : {q} / {int(rc.client.scard(_QUEUED_SET_KEY))}")
    if q:
        print("队列条目 cid：")
        for raw in rc.client.lrange(_QUEUE_KEY, 0, -1):
            text = raw if isinstance(raw, str) else raw.decode("utf-8", "ignore")
            print(f"  - {text[:160]}")
    print("────────────────────────────────────────────────────")
    _record("STATUS 快照", True, "见上方（预演前后各取一次，应逐项一致）")


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P2-2③ 镜像通道预演探针（零真单）")
    parser.add_argument(
        "--probe",
        default="status",
        choices=["status", "closed", "kill", "open", "drain", "purge", "all"],
        help="要跑的探针；all = closed→open→kill→drain→purge",
    )
    parser.add_argument(
        "--symbol", default="600036.SH", help="探针标的（默认 600036.SH）"
    )
    parser.add_argument(
        "--quantity", type=float, default=100.0, help="探针股数（默认 100）"
    )
    parser.add_argument(
        "--price", type=float, default=0.0, help="探针价（须>0；队列载荷用，永不触柜）"
    )
    args = parser.parse_args(argv)

    from backend.services.live_trading.services.trading_session import is_trading_time

    if args.probe not in ("status", "purge") and is_trading_time():
        print("拒绝在交易时段运行（B 探针时段内 = 真提交）。收市后重跑。")
        return 2
    if args.probe in ("closed", "kill", "open", "all") and not (args.price > 0):
        print("--price 须 > 0（探针载荷价；段外排队语义永远不触柜，但价闸要真实通过）")
        return 2

    rc = _redis()
    try:
        if args.probe == "status":
            probe_status(rc)
        elif args.probe == "purge":
            probe_purge(rc)
        elif args.probe == "closed":
            await probe_closed(
                rc, symbol=args.symbol, quantity=args.quantity, price=args.price
            )
        elif args.probe == "kill":
            await probe_kill(
                rc, symbol=args.symbol, quantity=args.quantity, price=args.price
            )
        elif args.probe == "open":
            await probe_open(
                rc, symbol=args.symbol, quantity=args.quantity, price=args.price
            )
        elif args.probe == "drain":
            await probe_drain(rc)
        else:  # all
            probe_status(rc)
            try:
                await probe_closed(
                    rc, symbol=args.symbol, quantity=args.quantity, price=args.price
                )
                await probe_open(
                    rc, symbol=args.symbol, quantity=args.quantity, price=args.price
                )
                await probe_kill(
                    rc, symbol=args.symbol, quantity=args.quantity, price=args.price
                )
                await probe_drain(rc)
            finally:
                probe_purge(rc)
            probe_status(rc)
    finally:
        try:
            rc.close()
        except Exception:  # noqa: BLE001
            pass

    failed = [r for r in _RESULTS if not r["ok"]]
    print(f"\n结果：{len(_RESULTS) - len(failed)}/{len(_RESULTS)} PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
