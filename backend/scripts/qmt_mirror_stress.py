"""QMT 真单镜像压力与边界测试（容器内运行）。

场景
----
``sweep``    多标的并发镜像下单（混买卖、随机数量、预算内），采集逐笔延迟
``cancel``   对未成交挂单撤单（走生产撤单接口 ``POST /api/v1/orders/{id}/cancel``）
``rest``     确定性造在途委托（远价挂单）→ 逐笔走生产接口撤单 → 本地/柜台终态复核
``partial``  大单部分成交 → 撤余量 → 重挂剩余（撤单重挂链路）
``stale``    远价挂单不成交，观察是否被重复提交（随后撤掉）
``quota``    限额/急停闸门验证（临时收紧后恢复原配置）
``universe`` 只选股预览，不下单

用法（容器内）::

    docker exec -w /app/backend -e PYTHONPATH=/app quantmind \\
        python scripts/qmt_mirror_stress.py sweep --symbols 50 --budget 1000000

安全：所有场景默认只读+小额；``quota`` 会临时改写 Redis 控制面配置与白/黑名单，
跑完 ``finally`` 恢复原值（含急停原状态——**绝不无条件清掉运维的急停**）；
``--dry-run`` 只打印计划不发单。

Redis 口径：控制面键（``mirror:config`` / ``mirror:kill`` / ``mirror:enabled`` /
名单）**只能用原始客户端或 real_mirror_service 的控制面函数**——``RedisClient``
包装器的 ``get/set`` 会走 ``json.loads/json.dumps``，写入会被二次编码、
读取会解出 str，导致「配置写了但服务端读不到」，限额/急停形同虚设。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import time
from typing import Any

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

LATEST_FILE = "/tmp/qmt_stress_latest.json"


# --------------------------------------------------------------------------
# 基础设施
# --------------------------------------------------------------------------
def _redis():
    from backend.services.trade_shared.deps import get_redis

    return get_redis()


async def _db_session():
    from backend.shared.database_manager_v2 import get_session

    return get_session()


async def _login(base: str = "http://127.0.0.1:8000") -> dict[str, str]:
    """登录拿 JWT（撤单接口要用）。凭据可用环境变量覆盖。

    返回 ``{"token", "sub"}``：``sub`` 是 JWT 主体，也是撤单接口查订单用的
    user_id —— 下单 user_id 必须与它同口径，否则订单归属对不上、撤单全 404。
    """
    import httpx

    payload = {
        "username": os.getenv("STRESS_USERNAME", "admin"),
        "password": os.getenv("STRESS_PASSWORD", "admin123"),
        "tenant_id": os.getenv("STRESS_TENANT", "default"),
    }
    async with httpx.AsyncClient(timeout=15) as http:
        resp = await http.post(f"{base}/api/v1/auth/login", json=payload)
        resp.raise_for_status()
        token = str(resp.json().get("access_token") or "")
    return {"token": token, "sub": _jwt_sub(token)}


def _jwt_sub(token: str) -> str:
    """解 JWT payload 取 ``sub``（不验签，只为本机自检口径一致性）。"""
    import base64

    try:
        part = str(token).split(".")[1]
        part += "=" * (-len(part) % 4)
        data = json.loads(base64.urlsafe_b64decode(part))
        return str(data.get("sub") or "")
    except Exception:  # noqa: BLE001 - 解不开就当拿不到，交由调用方判断
        return ""


def _normalize_user(user: Any) -> str:
    """DB 口径 user_id（数字串补零 8 位），与下单落库同口径。"""
    try:
        from backend.services.live_trading.routers.real_trading_utils import (
            normalize_db_user_id,
        )

        return normalize_db_user_id(user)
    except Exception:  # noqa: BLE001
        return str(user or "")


async def _resolve_identity(args: argparse.Namespace) -> tuple[str, str]:
    """统一「下单用户」与「登录账号」：返回 ``(token, user_id)`` 并回写 args。

    撤单接口按 JWT ``sub`` 过滤订单，压测下单必须用同一个 user_id，
    否则撤单必 404（挂单留在柜台无人撤）。默认账号 admin 的 sub 就是
    ``00000001``；显式传了不一致的 ``--user-id`` 时以登录账号为准并告警。
    """
    login = await _login()
    token = login["token"]
    sub = _normalize_user(login["sub"])
    want = _normalize_user(args.user_id)
    if sub and want != sub:
        print(
            "!! --user-id=%s 与登录账号 sub=%s 不一致：撤单接口按 JWT 过滤，"
            "已统一改用 %s（否则撤单全 404）" % (want, sub, sub)
        )
        args.user_id = sub
    elif not sub:
        print("!! 登录响应里没有可用 sub，沿用 --user-id=%s" % want)
    return token, str(args.user_id)


# --------------------------------------------------------------------------
# 选股
# --------------------------------------------------------------------------
UNIVERSE_SQL = """
SELECT symbol, stock_name, close, amount, turnover_rate, is_st
FROM stock_daily_latest
WHERE trade_date = (SELECT max(trade_date) FROM stock_daily_latest)
  AND close >= :min_price
  AND close <= :max_price
  AND COALESCE(amount, 0) >= :min_amount
  AND (:max_amount = 0 OR COALESCE(amount, 0) <= :max_amount)
  AND COALESCE(is_st, 0) = 0
  AND COALESCE(listed_days, 9999) > :min_listed
ORDER BY {order}
LIMIT :pool
"""


async def load_universe(
    *,
    pool: int = 300,
    min_price: float = 2.0,
    max_price: float = 200.0,
    min_amount: float = 0.0,
    max_amount: float = 0.0,
    min_listed: int = 120,
    liquid: bool = True,
    symbols: list[str] | None = None,
) -> list[dict[str, Any]]:
    """按成交额排序取流动性最好/最差的一池（默认剔除 ST 与超低价股）。

    ``amount`` 单位为万元（QuantDB 口径），``min_amount``/``max_amount`` 是
    日成交额区间（万元，``max_amount=0`` 表示不限）。返回的 ``symbol`` 已归一到
    QuantDB/QMT 口径的后缀式（``600036.SH``）。

    传 ``symbols`` 时（后缀式/前缀式均可）只查这几只，价格/上市天数等过滤放宽
    （用于「直接指定标的」的场景，如 BJ 薄标的的部分成交测试）。
    """
    from sqlalchemy import text

    from backend.shared.stock_utils import StockCodeUtil

    order = "amount DESC" if liquid else "amount ASC"
    if symbols:
        # 视图里 symbol 是前缀式（SH600036），入参后缀/前缀都接受，统一转前缀再比
        wanted = [StockCodeUtil.to_prefix(str(s)) for s in symbols if str(s).strip()]
        sql = UNIVERSE_SQL.format(order=order).replace(
            "WHERE trade_date", "WHERE symbol = ANY(:symbols) AND trade_date"
        )
        min_price, min_listed, max_amount = 0.0, 0, 0.0
        params: dict[str, Any] = {"symbols": wanted}
    else:
        sql = UNIVERSE_SQL.format(order=order)
        params = {}
    async with await _db_session() as session:
        rows = (
            await session.execute(
                text(sql),
                {
                    "pool": pool,
                    "min_price": min_price,
                    "max_price": max_price,
                    "min_amount": min_amount,
                    "max_amount": max_amount,
                    "min_listed": min_listed,
                    **params,
                },
            )
        ).mappings().all()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["symbol"] = StockCodeUtil.to_suffix(str(item["symbol"]))
        out.append(item)
    return out


def pick_symbols(pool: list[dict[str, Any]], count: int, rng: random.Random) -> list[dict[str, Any]]:
    count = min(count, len(pool))
    return rng.sample(pool, count)


# --------------------------------------------------------------------------
# 场景：universe
# --------------------------------------------------------------------------
async def scenario_universe(args: argparse.Namespace) -> dict[str, Any]:
    rng = random.Random(args.seed)
    liquid = await load_universe(pool=300, liquid=True)
    illiquid = await load_universe(pool=args.pool, min_amount=1.0, liquid=False)
    picked = pick_symbols(liquid, args.symbols, rng)
    print("== 流动性池 top300（按成交额）前 5 ==")
    for row in liquid[:5]:
        print("   %s %s close=%.2f amount=%.0f万" % (
            row["symbol"], row["stock_name"], row["close"], row["amount"] or 0))
    print("== 低流动性池（成交额最低 %d 只）前 5 ==" % args.pool)
    for row in illiquid[:5]:
        print("   %s %s close=%.2f amount=%.0f万" % (
            row["symbol"], row["stock_name"], row["close"], row["amount"] or 0))
    print("== 本次抽样 %d 只 ==" % len(picked))
    print("   " + " ".join(str(r["symbol"]) for r in picked))
    return {"liquid": liquid[:5], "illiquid": illiquid[:5], "picked": picked}


# --------------------------------------------------------------------------
# 场景：sweep
# --------------------------------------------------------------------------
async def scenario_sweep(args: argparse.Namespace) -> dict[str, Any]:
    from backend.services.live_trading.services.qmt_exec_client import (
        get_qmt_exec_client,
    )
    from backend.services.live_trading.services.real_mirror_service import (
        is_trading_time,
        mirror_virtual_fill,
    )

    client = get_qmt_exec_client()
    await client.refresh_settings()
    cfg = client.effective_config()
    print("执行端 enabled=%s account=%s" % (cfg.get("enabled"), cfg.get("account_id")))
    if not is_trading_time():
        print("!! 当前非交易时段，镜像单只会入队（不会真下单），建议在交易时段重跑")

    rng = random.Random(args.seed)
    liquid = await load_universe(pool=300, liquid=True, max_price=args.max_price)
    picked = pick_symbols(liquid, args.symbols, rng)
    if not picked:
        print("!! 选股池为空（检查 stock_daily_latest 是否已同步 / --max-price 是否过严），退出")
        return {"planned": [], "error": "empty_universe"}

    # 掺入一部分低流动性标的（用户口径：流动性差的股票怎么整）。
    # 北交所（BJ）先排除：券商侧多为「无权限」拒绝，属账户权限问题而非链路问题。
    illiquid_n = int(round(len(picked) * max(0.0, min(1.0, args.illiquid_ratio))))
    if illiquid_n:
        illiquid = [
            r
            for r in await load_universe(pool=200, liquid=False, max_price=args.max_price)
            if not str(r["symbol"]).upper().endswith(".BJ")
        ]
        extra = pick_symbols(illiquid, illiquid_n, rng) if illiquid else []
        if extra:
            picked = picked[: len(picked) - len(extra)] + extra
            print("掺入低流动性标的 %d 只：%s" % (
                len(extra), [str(r["symbol"]) for r in extra]))

    # 卖出侧优先用账户真实持仓，避免整批「卖空」废单
    from backend.services.live_trading.services.qmt_account_sync_task import (
        batch_quantdb_last_close,
    )
    from backend.shared.stock_utils import StockCodeUtil

    holdings: list[dict[str, Any]] = []
    try:
        for pos in await client.get_positions():
            avail = int(float(pos.get("can_use_volume") or 0))
            if avail >= 200:
                holdings.append({
                    # get_positions 给的是前缀式（SH600036），镜像/QuantDB 要后缀式
                    "symbol": StockCodeUtil.to_suffix(str(pos.get("symbol") or "")),
                    "close": 0.0,  # 现价下面用 QuantDB 收盘价补（持仓的 avg_price 是成本价）
                    "stock_name": "持仓",
                    "available": avail,
                })
        print("可卖持仓：%s" % [(h["symbol"], h["available"]) for h in holdings])
    except Exception as exc:  # noqa: BLE001
        print("!! 读取持仓失败（卖出将用普通标的，可能被券商拒单）: %s" % exc)

    # 参考价统一用 QuantDB 最近收盘（镜像限价基准就是它，虚拟成交价贴着走才不会被
    # price_drift 闸门跳过；用持仓成本价会偏出 ±2% 直接被拦）
    ref_close = await asyncio.to_thread(
        batch_quantdb_last_close,
        [h["symbol"] for h in holdings] + [str(r["symbol"]) for r in picked],
    )

    redis = _redis()
    planned: list[dict[str, Any]] = []
    buy_notional = 0.0
    sell_remaining = {h["symbol"]: h["available"] for h in holdings}
    sell_count = int(round(len(picked) * (1.0 - args.buy_ratio)))
    if not holdings and sell_count:
        # 无可用持仓时一笔都不卖：卖未持有标的会被券商连续拒单，
        # 累计 3 次触发 _record_reject 自动急停（mirror:kill=1）会把链路停掉。
        print("!! 无可用持仓，本次全部按买入执行（卖空废单会触发连续拒单熔断）")
        sell_count = 0
    flipped = 0
    for idx, row in enumerate(picked):
        side = "SELL" if idx < sell_count else "BUY"
        close = float(row["close"] or 0)
        if close <= 0:
            continue
        symbol = str(row["symbol"])
        if side == "SELL":
            candidates = [
                h for h in holdings if sell_remaining.get(h["symbol"], 0) >= 100
            ]
            if not candidates:
                side = "BUY"  # 持仓额度用完 → 降级买入，绝不卖未持有标的
                flipped += 1
            else:
                symbol = rng.choice(candidates)["symbol"]
        base = float(ref_close.get(symbol) or 0)
        if base <= 0:
            if side == "SELL":
                continue  # 持仓标的取不到参考价（ETF/退市等），换下一只
            base = close
        if base <= 0:
            continue
        price = round(base * rng.uniform(0.99, 1.01), 2)  # 虚拟成交价（模拟盘撮合价）
        max_lots = max(1, int(args.per_order_max // max(price * 100, 1)))
        if side == "SELL":
            max_lots = min(max_lots, max(1, sell_remaining.get(symbol, 0) // 100))
        lots = rng.choice([n for n in (1, 2, 3, 5, 8, 10, 15, 20, 30) if n <= max_lots] or [1])
        quantity = lots * 100
        # 真实占用按镜像限价 ref×(1+2%) 估（虚拟价贴着 ref 走，口径差 ~2%）：
        # 用虚拟价做预算会低估，实际单日金额可能超 --budget
        notional = round(price * 1.02, 2) * quantity
        if side == "BUY" and buy_notional + notional > args.budget:
            continue  # 预算内；买够即停
        if side == "BUY":
            buy_notional += notional
        else:
            sell_remaining[symbol] = sell_remaining.get(symbol, 0) - quantity
        planned.append({
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "price": price,
            "notional": notional,
        })

    print("== 计划 %d 笔（买 %d / 卖 %d）买入名义金额=%.0f 元%s ==" % (
        len(planned),
        sum(1 for p in planned if p["side"] == "BUY"),
        sum(1 for p in planned if p["side"] == "SELL"),
        buy_notional,
        "（%d 笔卖单降级为买入）" % flipped if flipped else "",
    ))
    if args.dry_run:
        for item in planned:
            print("   %s %s %s @ %.2f" % (
                item["side"], item["symbol"], item["quantity"], item["price"]))
        return {"planned": planned, "dry_run": True}

    run_id = time.strftime("%Y%m%d-%H%M%S")
    sem = asyncio.Semaphore(max(1, args.concurrency))
    results: list[dict[str, Any]] = []
    lock = asyncio.Lock()
    pending: list[tuple[str, asyncio.Task]] = []  # 超时但仍在跑的镜像调用

    async def _mirror(item: dict[str, Any], cid: str):
        async with await _db_session() as session:
            return await mirror_virtual_fill(
                db=session,
                redis=redis,
                tenant_id="default",
                user_id=args.user_id,
                symbol=item["symbol"],
                side=item["side"],
                quantity=item["quantity"],
                price=item["price"],
                client_order_id=cid,
                strategy_id=args.strategy_id,
                source="stress",
            )

    def _record(item: dict[str, Any], cid: str, seq: int, res: dict, elapsed: float) -> dict:
        detail = res.get("detail") if isinstance(res.get("detail"), dict) else {}
        violations = detail.get("violations") or []
        first_rule = ""
        if violations and isinstance(violations[0], dict):
            first_rule = str(violations[0].get("rule") or "")
        reason = str(res.get("reason") or "") or first_rule
        return {
            **item,
            "cid": cid,
            "seq": seq,
            "status": res.get("status"),
            "reason": reason,
            "violations": violations,
            "order_id": res.get("order_id"),
            "mirror_cid": res.get("client_order_id"),
            "limit_price": res.get("limit_price"),
            "latency_ms": round(elapsed, 1),
        }

    async def _one(item: dict[str, Any], seq: int) -> None:
        cid = "stress-%s-%03d" % (run_id, seq)
        async with sem:
            started = time.perf_counter()
            task = asyncio.create_task(_mirror(item, cid))
            try:
                # shield：超时**不取消**内层任务。取消会让已预留的额度泄漏
                # （_release_quota 只在 except Exception 分支跑，CancelledError
                # 是 BaseException 不走），且 RPC 可能已被券商受理、本地却无记录。
                res = await asyncio.wait_for(asyncio.shield(task), timeout=args.timeout)
            except asyncio.TimeoutError:
                res = {"status": "timeout", "reason": "client_timeout"}
                pending.append((cid, task))
            except Exception as exc:  # noqa: BLE001
                res = {"status": "error", "reason": "%s: %s" % (type(exc).__name__, exc)}
            elapsed = (time.perf_counter() - started) * 1000.0
            entry = _record(item, cid, seq, res, elapsed)
            async with lock:
                results.append(entry)
                print("   [%02d] %s %s %.2f×%s → %s %s %.0fms" % (
                    seq, item["side"], item["symbol"], item["price"], item["quantity"],
                    res.get("status"), entry["reason"], elapsed))

    await asyncio.gather(*[_one(item, i) for i, item in enumerate(planned)])

    if pending:
        print("\n   %d 笔超时未返回，继续等待（不取消，避免额度泄漏/孤儿单）..." % len(pending))
        tasks = [t for _, t in pending]
        await asyncio.wait(tasks, timeout=args.timeout)
        for cid, task in pending:
            entry = next((r for r in results if r["cid"] == cid), None)
            if entry is None:
                continue
            if task.done() and not task.cancelled() and task.exception() is None:
                res = task.result() or {}
                late = _record({}, cid, entry.get("seq", -1), res, entry["latency_ms"])
                for key in ("status", "reason", "violations", "order_id", "mirror_cid", "limit_price"):
                    entry[key] = late[key]
                entry["late_result"] = True
                print("   [晚到] %s → %s %s" % (cid, entry["status"], entry["reason"]))
            elif task.done():
                entry["status"] = "error"
                entry["reason"] = "late_task_error: %s" % (task.exception(),)
            else:
                entry["status"] = "pending"
                entry["reason"] = "still_running"

    ok = [r for r in results if r["status"] == "submitted"]
    lat = sorted(r["latency_ms"] for r in ok)
    def _pct(p: float) -> float:
        return lat[min(len(lat) - 1, int(len(lat) * p))] if lat else 0.0
    print("\n== sweep 汇总 ==")
    print("   提交 %d / 计划 %d，成功率 %.0f%%" % (
        len(ok), len(planned), 100.0 * len(ok) / max(1, len(planned))))
    if lat:
        print("   延迟(ms)：min=%.0f p50=%.0f p90=%.0f max=%.0f" % (
            lat[0], _pct(0.5), _pct(0.9), lat[-1]))
    by_status: dict[str, int] = {}
    for r in results:
        by_status[str(r["status"])] = by_status.get(str(r["status"]), 0) + 1
    print("   状态分布：%s" % by_status)
    reasons: dict[str, int] = {}
    for r in results:
        if r["status"] != "submitted":
            key = str(r["reason"])[:60]
            reasons[key] = reasons.get(key, 0) + 1
    if reasons:
        print("   未成功原因：%s" % reasons)

    out = {"run_id": run_id, "results": results, "buy_notional": buy_notional}
    with open("/tmp/qmt_stress_%s.json" % run_id, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    with open(LATEST_FILE, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    print("   明细：/tmp/qmt_stress_%s.json" % run_id)
    return out


# --------------------------------------------------------------------------
# 场景：cancel / watch
# --------------------------------------------------------------------------
def _load_latest() -> dict[str, Any]:
    with open(LATEST_FILE, encoding="utf-8") as fh:
        return json.load(fh)


async def _order_rows(cids: list[str]) -> list[dict[str, Any]]:
    from sqlalchemy import text

    if not cids:
        return []
    async with await _db_session() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT order_id::text, client_order_id, symbol, side, quantity,
                           filled_quantity, status, price, exchange_order_id,
                           trading_mode, created_at, submitted_at, updated_at
                    FROM orders
                    WHERE client_order_id = ANY(:cids)
                    ORDER BY created_at
                    """
                ),
                {"cids": cids},
            )
        ).mappings().all()
    return [dict(r) for r in rows]


async def scenario_watch(args: argparse.Namespace) -> dict[str, Any]:
    data = _load_latest()
    cids = [str(r["mirror_cid"] or r["cid"]) for r in data["results"]]
    rows = await _order_rows(cids)
    by_cid = {str(r["client_order_id"]): r for r in rows}
    print("== 挂单/终态快照（%d 笔）==" % len(rows))
    counts: dict[str, int] = {}
    for r in rows:
        counts[str(r["status"])] = counts.get(str(r["status"]), 0) + 1
    print("   状态分布：%s" % counts)
    for cid in cids:
        row = by_cid.get(cid)
        if row and str(row["status"]) not in {"filled", "cancelled", "rejected", "expired"}:
            print("   未终结：%s %s %s 已成交=%s exchange_id=%s" % (
                row["client_order_id"], row["symbol"], row["status"],
                row["filled_quantity"], row["exchange_order_id"]))
    # 提交→终态延迟：orders 表 submitted_at/updated_at 差值（含轮询器 2s 回写粒度）
    lat = sorted(
        (r["updated_at"] - r["submitted_at"]).total_seconds() * 1000.0
        for r in rows
        if r["submitted_at"] and r["updated_at"]
        and str(r["status"]) in {"filled", "cancelled", "rejected"}
    )
    if lat:
        def _pct(p: float) -> float:
            return lat[min(len(lat) - 1, int(len(lat) * p))]
        print("   提交→终态延迟(ms)：n=%d min=%.0f p50=%.0f p90=%.0f max=%.0f" % (
            len(lat), lat[0], _pct(0.5), _pct(0.9), lat[-1]))
    return {"rows": rows, "counts": counts}


async def _cancel_via_api(order_id: str, token: str, reason: str) -> dict[str, Any]:
    import httpx

    base = os.getenv("STRESS_TRADE_BASE", "http://127.0.0.1:8002")
    async with httpx.AsyncClient(timeout=30) as http:
        resp = await http.post(
            f"{base}/api/v1/orders/{order_id}/cancel",
            headers={"Authorization": f"Bearer {token}"},
            json={"order_id": order_id, "reason": reason},
        )
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = {"raw": resp.text[:300]}
        return {"http": resp.status_code, "body": body}


async def scenario_cancel(args: argparse.Namespace) -> dict[str, Any]:
    from backend.services.live_trading.services.qmt_exec_client import (
        get_qmt_exec_client,
    )

    data = _load_latest()
    token = str(getattr(args, "token", "") or "")
    if not token:
        print("!! 无可用登录 token（main 里登录失败），撤单链路无法验证，退出")
        return {"error": "login_failed"}
    cids = [str(r["mirror_cid"] or r["cid"]) for r in data["results"]]
    rows = await _order_rows(cids)
    targets = [
        r for r in rows
        if str(r["status"]) not in {"filled", "cancelled", "rejected", "expired"}
    ][: args.limit]
    print("== 撤单 %d 笔（走 POST /api/v1/orders/{id}/cancel）==" % len(targets))
    client = get_qmt_exec_client()
    await client.refresh_settings()
    out: list[dict[str, Any]] = []
    for row in targets:
        started = time.perf_counter()
        resp = await _cancel_via_api(str(row["order_id"]), token, "stress cancel")
        cost = (time.perf_counter() - started) * 1000.0
        print("   %s %s %s → HTTP %s %.0fms body=%s" % (
            row["client_order_id"], row["symbol"], row["status"],
            resp["http"], cost, str(resp["body"])[:120]))
        out.append({"client_order_id": row["client_order_id"], "http": resp["http"],
                    "ms": round(cost, 1), "body": resp["body"]})
        await asyncio.sleep(args.gap)
    # 复核：本地状态 + 桥侧委托状态
    await asyncio.sleep(3)
    after = await _order_rows([str(r["client_order_id"]) for r in rows])
    print("   复核状态：%s" % {str(r["client_order_id"]): str(r["status"]) for r in after})
    bridge = await client.query_orders()
    resting = [o for o in bridge if str(o.get("status")) not in {"FILLED", "CANCELED", "CANCELLED", "REJECTED"}]
    print("   桥侧未终结委托 %d 笔" % len(resting))
    return {"cancels": out, "after": after, "bridge_resting": len(resting)}


# --------------------------------------------------------------------------
# 场景：quota / kill
# --------------------------------------------------------------------------
# 控制面键。注意：**只能用原始客户端或 real_mirror_service 的控制面函数读写**。
# trade_shared 的 RedisClient 包装器 get/set 会 json.loads/json.dumps：写单层 JSON
# 变双层（服务端解出 str → 覆盖项整体失效），写 "1" 变 '"1"'（_one() 判为假 →
# 急停形同没开）。踩过一次，别再直接用包装器碰这些键。
CONFIG_KEY = "mirror:config"
KILL_KEY = "mirror:kill"
ENABLED_KEY = "mirror:enabled"
WHITELIST_KEY = "mirror:whitelist"
BLACKLIST_KEY = "mirror:blacklist"
DAILY_KEY = "mirror:daily:{date}:{field}"


def _raw(redis):
    """原始 redis-py 客户端（decode_responses=True）。"""
    return getattr(redis, "client", None)


def _raw_get(redis, key: str) -> str:
    raw = _raw(redis).get(key) if _raw(redis) is not None else None
    if raw is None:
        return ""
    return raw.decode("utf-8", "ignore") if isinstance(raw, (bytes, bytearray)) else str(raw)


def _restore_key(redis, key: str, value: Any) -> None:
    """按字节原样恢复键：空值 = 删除（不做任何 JSON 编解码）。"""
    client = _raw(redis)
    if client is None:
        return
    if value is None or value == "" or value == b"":
        client.delete(key)
    else:
        client.set(key, value)


def _set_enabled_raw(redis, value: str) -> None:
    """写热开关（原始客户端：包装器会把 "0" 写成 '"0"'，_one() 判假失败）。"""
    client = _raw(redis)
    if client is not None:
        client.set(ENABLED_KEY, value)


def _decode_members(raw_members) -> list[str]:
    return sorted(
        e.decode("utf-8", "ignore") if isinstance(e, (bytes, bytearray)) else str(e)
        for e in (raw_members or set())
    )


def _snapshot_control(redis) -> dict[str, Any]:
    """控制面快照（配置/急停/热开关/白名单/黑名单原值）。"""
    client = _raw(redis)
    return {
        "config": client.get(CONFIG_KEY),
        "kill": client.get(KILL_KEY),
        "enabled": client.get(ENABLED_KEY),
        "whitelist": _decode_members(client.smembers(WHITELIST_KEY)),
        "blacklist": _decode_members(client.smembers(BLACKLIST_KEY)),
    }


def _restore_control(redis, snap: dict[str, Any]) -> None:
    from backend.services.live_trading.services import real_mirror_service as rms

    _restore_key(redis, CONFIG_KEY, snap.get("config"))
    _restore_key(redis, KILL_KEY, snap.get("kill"))
    _restore_key(redis, ENABLED_KEY, snap.get("enabled"))
    rms.set_lists(
        redis,
        whitelist=list(snap.get("whitelist") or []),
        blacklist=list(snap.get("blacklist") or []),
    )


async def _mirror_call(
    redis,
    symbol: str,
    side: str,
    quantity: float,
    price: float,
    cid: str,
    user_id: str,
):
    """镜像下单（user_id 必传：必须与 main 里登录解析出的身份一致，否则撤单 404）。"""
    from backend.services.live_trading.services.real_mirror_service import (
        mirror_virtual_fill,
    )

    async with await _db_session() as session:
        return await mirror_virtual_fill(
            db=session,
            redis=redis,
            tenant_id="default",
            user_id=user_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            client_order_id=cid,
            source="stress-quota",
        )


async def scenario_quota(args: argparse.Namespace) -> dict[str, Any]:
    """限额/急停闸门：临时收紧 → 观察是否被拦 → finally 按字节恢复全部控制面。

    安全设计（每条都对应踩过的坑）：
    * 配置/急停/名单一律走原始客户端或 ``real_mirror_service`` 控制面函数
      （包装器会二次编码，导致「写了配置但服务端读不到」）；
    * 急停原状态快照后恢复——**绝不无条件 delete**（会清掉运维的急停）；
    * 临时抬升的上限只抬到够 100 股的程度，不用 ``1e9`` 这类 fail-open 值；
    * 白名单只在原名单不含压测账号时临时加入，跑完恢复；原名单是 ``*`` 时
      跳过「白名单外用户」用例（该用例失去意义，且会真的下发）。
    """
    from backend.services.live_trading.services import real_mirror_service as rms

    redis = _redis()
    if _raw(redis) is None:
        print("!! Redis 不可用")
        return {"error": "redis_unavailable"}
    snap = _snapshot_control(redis)
    kill_was_on = str(snap.get("kill") or "").strip() not in {"", "0", "false", "False"}
    print("== 限额闸门 ==  原配置=%s 急停=%s 热开关=%s 白名单=%s 黑名单=%s" % (
        snap["config"], "on" if kill_was_on else "off", snap["enabled"],
        snap["whitelist"], snap["blacklist"]))
    universe = await load_universe(pool=50, liquid=True)
    if not universe:
        print("!! 选股池为空，退出")
        return {"error": "empty_universe"}
    row = min(universe, key=lambda r: float(r["close"] or 0))  # 用池里最便宜的，压小额
    symbol, price = str(row["symbol"]), float(row["close"])
    user = str(args.user_id)
    day = rms.trade_date_str()
    probe_value = round(price * 1.02, 2) * 100  # 100 股的真实占用（镜像限价口径）
    cap = round(probe_value * 1.2, 2)  # 临时上限：够这一笔 + 20% 缓冲
    findings: dict[str, Any] = {}

    def _w(updates: dict[str, Any]) -> None:
        rms.write_config_overrides(redis, updates)

    async def _call(tag: str, *, user_id: str | None = None, side: str = "BUY",
                    qty: float = 100, px: float | None = None, sym: str | None = None):
        return await _mirror_call(
            redis, sym or symbol, side, qty, price if px is None else px,
            "quota-%s-%d" % (tag, int(time.time() * 1000)), user_id or user,
        )

    def _case(name: str, res: dict[str, Any], expect: str | tuple[str, ...],
              expect_reason: str | None = None) -> None:
        status = str(res.get("status") or "")
        reason = str(res.get("reason") or "")
        ok = status in ((expect,) if isinstance(expect, str) else expect) and (
            expect_reason is None or reason == expect_reason
        )
        findings[name] = {
            "status": status, "reason": reason,
            "expect": "%s/%s" % (
                expect if isinstance(expect, str) else "|".join(expect),
                expect_reason or "*"),
            "ok": ok,
        }
        print("   %s %-22s → %s / %s（期望 %s / %s）" % (
            "✅" if ok else "❌", name, status, reason,
            expect if isinstance(expect, str) else "|".join(expect), expect_reason or "*"))

    try:
        # 预置：确保压测账号在镜像白名单里。否则每个用例都会在 whitelist 闸门
        # 短路返回，所有限额用例「看起来都拦住了」，实际一个闸门都没测到。
        wl = set(snap["whitelist"])
        if "*" not in wl and ("default:%s" % user) not in wl:
            wl.add("default:%s" % user)
            rms.set_lists(redis, whitelist=sorted(wl))
            print("   白名单临时加入 default:%s（跑完恢复原名单 %s）" % (user, snap["whitelist"]))

        # ① 单笔上限：压到 1 元 → 必拦（镜像限价 ~price×1.02 远大于 1）
        _w({"enabled": 1, "max_order_value": 1.0, "max_daily_value": cap * 10,
            "max_daily_orders": 50, "max_daily_symbols": 50})
        _case("max_order_value", await _call("single"), "skipped", "max_order_value")

        # ② 急停：mirror:kill=1 → 首闸 mirror_enabled 即短路
        _w({"max_order_value": cap})
        rms.set_kill_switch(redis, True)
        _case("kill_switch", await _call("kill"), "skipped", "mirror_disabled")
        rms.set_kill_switch(redis, kill_was_on)  # 立刻恢复原状态
        cfg_now = rms.load_config(redis)
        print("   控制项（纯读）：急停恢复后 mirror_enabled=%s（急停开启时应为 False）" % (
            rms.mirror_enabled(redis, cfg_now)))
        findings["kill_control"] = {
            "kill_restored": kill_was_on,
            "enabled_after_restore": rms.mirror_enabled(redis, cfg_now),
        }

        # ③ 黑名单（SET 元素为标的代码，镜像侧按 .upper() 比对）
        rms.set_lists(redis, blacklist=sorted(set(snap["blacklist"]) | {symbol.upper()}))
        _case("blacklist", await _call("black"), "skipped", "blacklist")
        rms.set_lists(redis, blacklist=snap["blacklist"])

        # ④ 当日笔数上限（「买够了就停」的机制）：名额压到「已用+1」→ 第 2 笔必被拦。
        #    第 1 笔是**真实下单**（100 股、池内最便宜标的），这是预期行为。
        used_orders = int(float(_raw_get(redis, DAILY_KEY.format(date=day, field="orders")) or 0))
        used_value = float(_raw_get(redis, DAILY_KEY.format(date=day, field="value")) or 0)
        # 金额上限必须抬到「当日已用 + 若干笔」之上：只压笔数这一个闸门。
        # （若沿用 cap 这类小值，金额闸门会先于笔数闸门触发，用例测不到笔数逻辑）
        _w({"max_order_value": cap, "max_daily_orders": used_orders + 1,
            "max_daily_value": used_value + probe_value * 10, "max_daily_symbols": 50})
        _case("max_daily_orders.ok", await _call("orders-ok"), ("submitted", "queued", "duplicate"))
        _case("max_daily_orders.block", await _call("orders-block"), "skipped", "max_daily_orders")
        print("   （当日已用笔数 %d → 上限 %d；第 1 笔放行属预期）" % (
            used_orders, used_orders + 1))

        # ⑤ 当日金额上限：额度收到「已用 + 本笔的一半」→ 必拦；
        #    同时把笔数/单笔上限放开，只让金额闸门承压。
        used_orders = int(float(_raw_get(redis, DAILY_KEY.format(date=day, field="orders")) or 0))
        used_value = float(_raw_get(redis, DAILY_KEY.format(date=day, field="value")) or 0)
        _w({"max_order_value": cap, "max_daily_orders": used_orders + 10,
            "max_daily_value": used_value + probe_value * 0.5})
        _case("max_daily_value", await _call("value"), "skipped", "max_daily_value")

        # 以下闸门都在额度校验之前判定，先放开额度以免相互干扰
        _w({"max_order_value": cap, "max_daily_orders": 50,
            "max_daily_value": cap * 10, "max_daily_symbols": 50})

        # ⑥ 价格偏离闸门（「波动大」：虚拟价离最近收盘 >2% → 不追价，跳过）
        drift_price = round(price * 1.05, 2)
        res = await _call("drift", px=drift_price)
        _case("price_drift", res, "skipped", "price_drift")

        # ⑦ 无参考价（退市/停牌/QuantDB 无数据）：fail-closed，不下单
        _case("no_reference_price", await _call("noref", sym="999999.SZ"),
              "skipped", "no_reference_price")

        # ⑧ 市场不在支持列表（港股）：镜像只服务 cfg.markets
        _case("market_not_supported", await _call("market", sym="00700.HK"),
              "skipped", "market_not_supported:HK")

        # ⑨ 白名单外用户：只镜像被点名的账户（原名单为 * 时该用例无意义，跳过）
        if "*" in set(snap["whitelist"]):
            print("   ⏭️  whitelist_other_user 跳过：原白名单为 *，任何用户都放行")
            findings["whitelist_other_user"] = {"skipped": "original_whitelist_is_star"}
        else:
            _case("whitelist_other_user", await _call("user", user_id="99999999"),
                  "skipped", "whitelist")

        # ⑩ 热开关关闭（mirror:enabled=0，原始客户端写）
        _set_enabled_raw(redis, "0")
        _case("disabled", await _call("off"), "skipped", "mirror_disabled")
    finally:
        _restore_control(redis, snap)
        print("   已恢复控制面：配置=%s 急停=%s 热开关=%s 白名单=%s 黑名单=%s" % (
            _raw_get(redis, CONFIG_KEY), _raw_get(redis, KILL_KEY) or "(无)",
            _raw_get(redis, ENABLED_KEY) or "(无)",
            _decode_members(_raw(redis).smembers(WHITELIST_KEY)),
            _decode_members(_raw(redis).smembers(BLACKLIST_KEY))))
    failed = [k for k, v in findings.items() if isinstance(v, dict) and v.get("ok") is False]
    print("== 限额闸门结论：%d 项通过，%d 项不符预期 %s ==" % (
        sum(1 for v in findings.values() if isinstance(v, dict) and v.get("ok") is True),
        len(failed), failed or ""))
    findings["_failed"] = failed
    return findings


# --------------------------------------------------------------------------
# 场景：stale / partial（直接经执行端下单，绕开镜像限价）
# --------------------------------------------------------------------------
# 桥侧委托终态（QMT 状态映射用 CANCELLED；部分上游下行用 CANCELED，两种都认）
_TERMINAL_STATUSES = {"FILLED", "CANCELLED", "CANCELED", "REJECTED"}


async def scenario_stale(args: argparse.Namespace) -> dict[str, Any]:
    """远价挂单：观察是否有重复提交；随后撤掉。

    重复提交按「桥侧同一备注（client_order_id 的 sha1 摘要）出现几笔委托」判定——
    备注是 cid 的确定映射（``build_remark``），同一 cid 被重复下单必然表现为
    同备注多笔委托；只看 order_id 是查不出重复的。
    """
    from backend.services.live_trading.services.qmt_exec_client import (
        build_remark,
        get_qmt_exec_client,
    )

    client = get_qmt_exec_client()
    await client.refresh_settings()
    if getattr(args, "symbol", ""):
        symbol = str(args.symbol).strip().upper()
        rows = await load_universe(pool=6000, min_amount=0.0, max_amount=0.0,
                                   liquid=False, symbols=[symbol])
        if not rows or not rows[0].get("close"):
            print("!! %s 无昨收（停牌/退市/不在 QuantDB），退出" % symbol)
            return {"error": "no_close", "symbol": symbol}
        row = rows[0]
    else:
        universe = await load_universe(pool=100, liquid=True)
        row = universe[args.index % len(universe)]
    symbol, close = str(row["symbol"]), float(row["close"])
    limit = round(close * args.discount, 2)
    cid = "stale-%d" % int(time.time())
    print("== 远价挂单 %s BUY 100 @ %.2f（现价基准 %.2f，跌 %.0f%%）==" % (
        symbol, limit, close, (1 - args.discount) * 100))
    started = time.perf_counter()
    res = await client.submit_order(
        symbol=symbol, side="BUY", quantity=100, order_type="LIMIT",
        price=limit, client_order_id=cid,
    )
    order_id = str(res.get("order_id") or "")
    print("   受理 %.0fms order_id=%s" % ((time.perf_counter() - started) * 1000.0, order_id))
    remark = build_remark(cid)
    duplicates = 0
    same_remark_count = 1
    print("   观察 %ds（每 5s 查一次，看是否被重复提交/状态异常）..." % args.watch)
    deadline = time.time() + args.watch
    while time.time() < deadline:
        await asyncio.sleep(5)
        orders = await client.query_orders()
        mine = [o for o in orders if str(o.get("order_id")) == order_id]
        same_remark = [o for o in orders if str(o.get("order_remark") or "") == remark]
        same_remark_count = max(same_remark_count, len(same_remark))
        if len(same_remark) > 1:
            duplicates = max(duplicates, len(same_remark) - 1)
            print("     !! 同一备注 %s 出现 %d 笔委托（重复提交）" % (remark, len(same_remark)))
        if mine:
            print("     状态=%s 已成交=%s 价=%s" % (
                mine[0].get("status"), mine[0].get("traded_volume"), mine[0].get("traded_price")))
            if str(mine[0].get("status")) in (_TERMINAL_STATUSES | {"PARTIALLY_FILLED"}):
                break
    trades = await client.query_trades()
    dup_trades = [t for t in trades if str(t.get("order_id")) == order_id]
    print("   成交回报 %d 条；同备注委托数=%d（应恒为 1），重复提交次数=%d" % (
        len(dup_trades), same_remark_count, duplicates))
    if args.cancel_after:
        try:
            print("   撤单：%s" % await client.cancel_order(order_id=order_id, symbol=symbol))
        except Exception as exc:  # noqa: BLE001 - 终态单不可撤（柜台 -1），非链路故障
            print("   撤单被拒：%s: %s（订单多半已是终态，属预期）" % (type(exc).__name__, exc))
    return {"symbol": symbol, "order_id": order_id, "limit": limit,
            "trades": len(dup_trades), "duplicates": duplicates}


async def scenario_partial(args: argparse.Namespace) -> dict[str, Any]:
    """大单部分成交 → 撤余量 → 重挂：验证 3/4 个关键状态。"""
    from backend.services.live_trading.services.qmt_exec_client import (
        get_qmt_exec_client,
    )

    client = get_qmt_exec_client()
    await client.refresh_settings()
    if args.symbol:
        # 直指标的：从池里取它的昨收（池外标的走 QuantDB 兜底查询）
        symbol = str(args.symbol).strip().upper()
        rows = await load_universe(pool=6000, min_amount=0.0, max_amount=0.0,
                                   liquid=False, symbols=[symbol])
        row = rows[0] if rows else {"symbol": symbol, "close": 0.0,
                                    "amount": 0.0, "stock_name": None}
        if not row.get("close"):
            print("!! %s 无昨收（停牌/退市/不在 QuantDB），退出" % symbol)
            return {"error": "no_close", "symbol": symbol}
    else:
        pool = await load_universe(
            pool=args.pool,
            min_amount=args.min_amount,
            max_amount=args.max_amount,
            liquid=False,
        )
        pool = [r for r in pool if not str(r["symbol"]).upper().endswith(".BJ")]  # 券商权限差异
        if not pool:
            print("!! 成交额区间内没有标的（放宽 --min-amount/--max-amount 重试）")
            return {"error": "empty_pool"}
        row = pool[args.index % len(pool)]
    symbol, close = str(row["symbol"]), float(row["close"])
    quantity = args.lots * 100
    limit = round(close * args.premium, 2)
    cid = "part-%d" % int(time.time())
    print("== 部分成交测试 %s BUY %d 股 @ %.2f（%s / 日成交额 %.0f 万 / 本单约 %.0f 万）==" % (
        symbol, quantity, limit, row["stock_name"], (row["amount"] or 0),
        close * quantity / 1e4))
    if args.dry_run:
        return {"dry_run": True, "symbol": symbol, "close": close,
                "quantity": quantity, "limit": limit,
                "notional": round(limit * quantity, 2)}
    res = await client.submit_order(
        symbol=symbol, side="BUY", quantity=quantity, order_type="LIMIT",
        price=limit, client_order_id=cid,
    )
    order_id = str(res.get("order_id") or "")
    print("   受理 order_id=%s" % order_id)
    filled = 0
    status = ""
    deadline = time.time() + args.wait
    while time.time() < deadline:
        await asyncio.sleep(3)
        mine = [o for o in await client.query_orders() if str(o.get("order_id")) == order_id]
        if not mine:
            continue
        status = str(mine[0].get("status") or "")
        filled = int(float(mine[0].get("traded_volume") or 0))
        print("     status=%s 已成交=%d/%d" % (status, filled, quantity))
        if status in _TERMINAL_STATUSES or (filled and args.stop_on_partial):
            break
    result: dict[str, Any] = {"symbol": symbol, "order_id": order_id,
                              "quantity": quantity, "filled": filled, "status": status}
    if filled and status not in _TERMINAL_STATUSES:
        # 撤单可能**被柜台拒绝**（最常见：轮询间隔内余量刚好全部成交，撤单请求
        # 打到已是终态的单上，柜台返回 -1）。这不是链路故障，先复核柜台终态再决定。
        cancel_error = ""
        try:
            cancel_resp = await client.cancel_order(order_id=order_id, symbol=symbol)
            print("   部分成交 → 撤余量：%s" % cancel_resp)
        except Exception as exc:  # noqa: BLE001
            cancel_error = "%s: %s" % (type(exc).__name__, exc)
            result["cancel_error"] = cancel_error
            print("   部分成交 → 撤余量被拒：%s（复核柜台终态）" % cancel_error)
        await asyncio.sleep(3)
        after_status, after_filled = status, filled
        mine = [o for o in await client.query_orders() if str(o.get("order_id")) == order_id]
        if mine:
            after_status = str(mine[0].get("status") or "")
            after_filled = int(float(mine[0].get("traded_volume") or 0))
            result["after_cancel_status"] = after_status
            result["after_cancel_filled"] = after_filled
            print("   撤单后 status=%s 已成交=%s" % (after_status, after_filled))
        # 重挂前提：撤单**已确认**，且余量按撤单后的成交量重算。
        # 用撤单前的 filled 算余量会把撤单期间新成交的部分再买一遍（双倍买入）；
        # 撤单未确认（51/52「撤单请求已发未确认」）时重挂更危险——原单随时可能成交。
        remain = quantity - after_filled
        if after_status not in {"CANCELLED", "CANCELED"}:
            print("   !! 撤单未确认（status=%s），跳过重挂以免双倍买入" % after_status)
            result["replace_skipped"] = "cancel_not_confirmed:%s" % after_status
        elif remain >= 100:
            cid2 = cid + "-r1"
            print("   重挂剩余 %d 股 @ %.2f ..." % (remain, limit))
            res2 = await client.submit_order(
                symbol=symbol, side="BUY", quantity=remain, order_type="LIMIT",
                price=limit, client_order_id=cid2,
            )
            result["replace_order_id"] = str(res2.get("order_id") or "")
            print("   重挂受理 order_id=%s" % result["replace_order_id"])
        else:
            result["replace_skipped"] = "remain_below_lot:%d" % remain
    return result


# --------------------------------------------------------------------------
# 场景：rest（远价挂单 → 生产撤单接口 → 终态复核）
# --------------------------------------------------------------------------
async def scenario_rest(args: argparse.Namespace) -> dict[str, Any]:
    """远价挂 N 笔（真实进 orders 表且在柜台在途）→ 逐笔走生产撤单接口 → 复核终态。

    与 ``cancel`` 的区别：``cancel`` 撤的是 sweep 残留的挂单（有没有残留看行情），
    本场景**确定性**地造出在途委托，专门覆盖撤单链路与终态一致性：
    OrderService → QmtExecBroker.cancel_order → QMT RPC，以及撤单后轮询器
    会不会把已撤委托又改回在途/成交（本地终态与柜台终态对齐）。
    """
    from backend.services.live_trading.services.internal_strategy_dispatcher import (
        dispatch_internal_strategy_order,
    )
    from backend.services.live_trading.services.qmt_account_sync_task import (
        batch_quantdb_last_close,
    )
    from backend.services.live_trading.services.qmt_exec_client import (
        get_qmt_exec_client,
    )
    from backend.shared.stock_utils import StockCodeUtil

    client = get_qmt_exec_client()
    await client.refresh_settings()
    token = str(getattr(args, "token", "") or "")
    if not token:
        print("!! 无可用登录 token（main 里登录失败），撤单链路无法验证，退出")
        return {"error": "login_failed"}
    redis = _redis()

    universe = await load_universe(pool=200, liquid=True, max_price=150.0)
    if not universe:
        print("!! 选股池为空，退出")
        return {"error": "empty_universe"}
    holdings: list[dict[str, Any]] = []
    try:
        for pos in await client.get_positions():
            avail = int(float(pos.get("can_use_volume") or 0))
            if avail >= args.lots * 100:
                holdings.append({
                    "symbol": StockCodeUtil.to_suffix(str(pos.get("symbol") or "")),
                    "available": avail,
                })
        print("可卖持仓：%s" % [(h["symbol"], h["available"]) for h in holdings])
    except Exception as exc:  # noqa: BLE001
        print("!! 读取持仓失败（卖出侧退化为买入）: %s" % exc)

    ref = await asyncio.to_thread(
        batch_quantdb_last_close,
        [str(r["symbol"]) for r in universe[: args.count * 2]]
        + [h["symbol"] for h in holdings],
    )

    run_id = time.strftime("%Y%m%d-%H%M%S")
    placed: list[dict[str, Any]] = []
    quantity = args.lots * 100
    # 卖出侧额度台账：同一持仓的可用量被子单逐笔扣减，避免重复卖同一只
    # （持仓不足时券商连续拒单会触发连续拒单熔断）
    sell_remaining = {h["symbol"]: h["available"] for h in holdings}
    for i in range(args.count):
        row = universe[(args.index + i) % len(universe)]
        symbol = str(row["symbol"])
        close = float(ref.get(symbol) or row["close"] or 0)
        side = "BUY"
        if i % 2 == 1 and holdings:
            candidates = [
                h for h in holdings if sell_remaining.get(h["symbol"], 0) >= quantity
            ]
            if candidates:
                held = candidates[(i // 2) % len(candidates)]
                side = "SELL"
                symbol = held["symbol"]
                close = float(ref.get(symbol) or 0) or close
        if close <= 0:
            print("   [%02d] 跳过（无参考价）：%s" % (i, symbol))
            continue
        # 买单压到市价下方、卖单抬到市价上方 → 挂着不成交（仍在涨跌停 ×10% 之内）
        limit = round(
            close * (1 - args.discount) if side == "BUY" else close * (1 + args.premium), 2
        )
        cid = "rest-%s-%02d" % (run_id, i)
        try:
            async with await _db_session() as session:
                res = await dispatch_internal_strategy_order(
                    order_data={
                        "symbol": symbol,
                        "side": side,
                        "quantity": quantity,
                        "price": limit,
                        "order_type": "LIMIT",
                        "trading_mode": "REAL",
                        "strategy_id": args.strategy_id or None,
                        "client_order_id": cid,
                        "remarks": "mirror:stress-rest",
                    },
                    user_id=args.user_id,
                    tenant_id="default",
                    redis=redis,
                    db=session,
                )
        except Exception as exc:  # noqa: BLE001
            print("   [%02d] %s %s 挂单失败: %s: %s" % (
                i, side, symbol, type(exc).__name__, exc))
            continue
        order_id = str(res.get("order_id") or "")
        print("   [%02d] %s %s %.2f×%s → %s order_id=%s" % (
            i, side, symbol, limit, quantity, res.get("status"), order_id))
        if side == "SELL":
            sell_remaining[symbol] = sell_remaining.get(symbol, 0) - quantity
        placed.append({
            "i": i, "symbol": symbol, "side": side, "limit": limit,
            "quantity": quantity, "cid": cid, "order_id": order_id,
            "submit_status": res.get("status"),
        })

    if not placed:
        print("!! 没有挂出的委托，退出")
        return {"placed": [], "cancels": []}
    await asyncio.sleep(args.settle)

    cids = [p["cid"] for p in placed]
    rows = await _order_rows(cids)
    by_cid = {str(r["client_order_id"]): r for r in rows}
    print("== 挂单复核（本地）==")
    for p in placed:
        r = by_cid.get(p["cid"]) or {}
        p["local_before"] = str(r.get("status") or "missing")
        p["exchange_order_id"] = str(r.get("exchange_order_id") or "")
        print("   %s %s 本地=%s 柜台委托号=%s" % (
            p["cid"], p["symbol"], p["local_before"], p["exchange_order_id"] or "-"))

    print("== 逐笔撤单（生产接口 POST /api/v1/orders/{id}/cancel，间隔 %.1fs）==" % args.gap)
    for p in placed:
        if not p["order_id"]:
            continue
        started = time.perf_counter()
        resp = await _cancel_via_api(p["order_id"], token, "stress rest cancel")
        cost = (time.perf_counter() - started) * 1000.0
        p["cancel_http"] = resp["http"]
        p["cancel_ms"] = round(cost, 1)
        p["cancel_body"] = str(resp["body"])[:300]
        print("   %s %s → HTTP %s %.0fms body=%s" % (
            p["cid"], p["symbol"], resp["http"], cost, str(resp["body"])[:140]))
        await asyncio.sleep(args.gap)

    await asyncio.sleep(args.settle)
    rows = await _order_rows(cids)
    by_cid = {str(r["client_order_id"]): r for r in rows}
    bridge = await client.query_orders()
    by_bridge = {str(o.get("order_id")): o for o in bridge}
    print("== 终态复核（本地 orders 表 vs 柜台 query_orders）==")
    mismatches: list[str] = []
    for p in placed:
        r = by_cid.get(p["cid"]) or {}
        b = by_bridge.get(p["exchange_order_id"]) or {}
        p["local_after"] = str(r.get("status") or "missing")
        p["local_filled"] = float(r.get("filled_quantity") or 0)
        p["bridge_status"] = str(b.get("status") or "missing")
        p["bridge_filled"] = float(b.get("traded_volume") or 0)
        consistent = (
            p["local_after"] == "cancelled"
            and p["bridge_status"] in {"CANCELED", "CANCELLED"}
        )
        p["consistent"] = consistent
        if not consistent:
            mismatches.append(p["cid"])
        print("   %s %s 本地=%s(成交%s) 柜台=%s(成交%s) %s" % (
            p["cid"], p["symbol"], p["local_after"], p["local_filled"],
            p["bridge_status"], p["bridge_filled"],
            "" if consistent else "  ← 不一致"))
    out = {"run_id": run_id, "placed": placed, "mismatches": mismatches}
    with open("/tmp/qmt_stress_rest_%s.json" % run_id, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    print("   撤单一致 %d/%d，明细：/tmp/qmt_stress_rest_%s.json" % (
        len(placed) - len(mismatches), len(placed), run_id))
    return out


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="QMT 镜像压力/边界测试")
    sub = parser.add_subparsers(dest="scenario", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--seed", type=int, default=20260910)
    common.add_argument("--user-id", default=os.getenv("STRESS_USER_ID", "00000001"))
    common.add_argument("--strategy-id", default="0")
    common.add_argument("--timeout", type=float, default=60.0, help="单笔镜像调用超时(秒)")
    common.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("universe", parents=[common])
    p.add_argument("--symbols", type=int, default=50)
    p.add_argument("--pool", type=int, default=50)

    p = sub.add_parser("sweep", parents=[common])
    p.add_argument("--symbols", type=int, default=50)
    p.add_argument("--budget", type=float, default=1_000_000.0)
    p.add_argument("--buy-ratio", type=float, default=0.6)
    p.add_argument("--concurrency", type=int, default=50)
    p.add_argument("--per-order-max", type=float, default=20_000.0, help="单笔名义金额上限")
    p.add_argument("--max-price", type=float, default=150.0, help="选股价格上限（避免高价股单笔超标）")
    p.add_argument("--illiquid-ratio", type=float, default=0.2, help="掺入低流动性标的的比例")

    p = sub.add_parser("cancel", parents=[common])
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--gap", type=float, default=0.2)

    sub.add_parser("watch", parents=[common])

    p = sub.add_parser("quota", parents=[common])

    p = sub.add_parser("stale", parents=[common])
    p.add_argument("--symbol", default="", help="直接指定标的（空=按流动性池取 --index）")
    p.add_argument("--index", type=int, default=0)
    p.add_argument("--discount", type=float, default=0.95)
    p.add_argument("--watch", type=int, default=60)
    p.add_argument("--cancel-after", action=argparse.BooleanOptionalAction, default=True,
                   help="观察结束后撤掉挂单（--no-cancel-after 关闭）")

    p = sub.add_parser("partial", parents=[common])
    p.add_argument("--symbol", default="", help="直接指定标的（空=按成交额区间从池里挑）")
    p.add_argument("--index", type=int, default=0)
    p.add_argument("--pool", type=int, default=200)
    p.add_argument("--lots", type=int, default=2000)
    p.add_argument("--premium", type=float, default=1.03)
    p.add_argument("--wait", type=int, default=60)
    p.add_argument("--min-amount", type=float, default=1.0, help="日成交额下限（万元）")
    p.add_argument("--max-amount", type=float, default=0.0, help="日成交额上限（万元，0=不限）")
    p.add_argument("--stop-on-partial", action=argparse.BooleanOptionalAction, default=True,
                   help="出现部分成交即结束等待（--no-stop-on-partial 可继续等到超时/全成）")

    p = sub.add_parser("rest", parents=[common])
    p.add_argument("--count", type=int, default=6, help="挂单笔数（奇偶交替买/卖）")
    p.add_argument("--index", type=int, default=0)
    p.add_argument("--lots", type=int, default=1)
    p.add_argument("--discount", type=float, default=0.93, help="买单低于市价比例（挂着不成交）")
    p.add_argument("--premium", type=float, default=1.07, help="卖单高于市价比例（挂着不成交）")
    p.add_argument("--gap", type=float, default=0.5, help="逐笔撤单间隔秒")
    p.add_argument("--settle", type=float, default=3.0, help="状态复核前等待秒数")
    return parser


SCENARIOS = {
    "universe": scenario_universe,
    "sweep": scenario_sweep,
    "cancel": scenario_cancel,
    "watch": scenario_watch,
    "quota": scenario_quota,
    "stale": scenario_stale,
    "partial": scenario_partial,
    "rest": scenario_rest,
}


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    """先统一登录身份（token + user_id 回写 args）+ 刷新 QMT 页面配置，再跑场景。

    登录失败不致命：universe/sweep 走的是镜像内部通道不依赖 HTTP token，
    只有 cancel/rest 依赖；那两者拿到空 token 会自行早退并说明原因。

    页面配置刷新必须做：``QmtExecClient`` 的 ``enabled`` 来自
    ``broker:config:qmt_exec``（trade Redis），但该覆盖由常驻任务每轮
    ``refresh_settings()`` 拉取；一次性进程（本脚本）不刷新就会退化成
    读环境变量（容器里没设 ``QMT_EXEC_ENABLED``）→ 所有下单恒被判
    ``DISABLED``，用例全部空转。
    """
    from backend.services.live_trading.services.qmt_exec_client import (
        get_qmt_exec_client,
    )

    try:
        client = get_qmt_exec_client()
        eff = await client.refresh_settings()
        print(
            "QMT 执行端：enabled=%s account=%s redis=%s:%s/%s"
            % (
                eff.get("enabled"), eff.get("account_id"),
                eff.get("redis_host"), eff.get("redis_port"), eff.get("redis_db"),
            )
        )
        if not eff.get("enabled"):
            print("!! QMT 执行端未启用（页面配置 broker:config:qmt_exec 或环境变量），下单将全部被拦")
    except Exception as exc:  # noqa: BLE001
        print("!! 刷新 QMT 页面配置失败: %s: %s" % (type(exc).__name__, exc))

    try:
        token, user = await _resolve_identity(args)
        args.token = token
        args.user_id = user
        print("登录成功：user_id=%s token=%s..." % (user, token[:16]))
    except Exception as exc:  # noqa: BLE001
        print(
            "!! 登录失败（cancel/rest 等依赖登录的场景将不可用）: %s: %s"
            % (type(exc).__name__, exc)
        )
        args.token = ""
    return await SCENARIOS[args.scenario](args)


def main() -> int:
    args = _build_parser().parse_args()
    result = asyncio.run(_run(args))
    print("\n== 结果 JSON ==")
    print(json.dumps(result, ensure_ascii=False, default=str)[:4000])
    return 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    # RPC 超时后工作线程可能卡在桥连接上，解释器收尾会挂死（同 qmt_bridge_selftest）
    os._exit(code)
