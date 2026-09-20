"""候选信号「多选 → 一键推送」的两个端点（T-FE-09）。

    POST /api/v1/stock-terminal/push-orders/preflight   推送前预检（不落任何留痕）
    POST /api/v1/stock-terminal/push-orders             逐笔下单 + 镜像真单

四条不变量（每条都对应一处会静默出错的地方）：

1. **价格与仓位信号一律服务端解析**。前端只能指定 ``side``、``quantities`` 与目标通道；
   价格取全市场快照、``position_score`` 取 ``engine_signal_scores``。让前端传「现价 1 元、
   仓位 99%」就能把仓位放大两个数量级 —— 这条链最终会发真单。
2. **预检绝不写留痕**。判定照跑（同一份 18 条规则代码），但 ``qm:risk:decisions`` /
   ``qm:risk:metrics`` 一个字节都不写（``preflight_order`` 的 ``record=False``）；
   否则用户每开一次确认面板就把影子报告灌爆，翻闸前的校准数据直接失真。
   实盘配额同理：只读 ``status_snapshot``，绝不调 ``_reserve_quota``。
3. **绝不在外层包 ``locked_execution``**。``OrderRouter.submit_order`` 内部已持
   「同用户撮合临界区」锁（唯一入口自带串行化），重入即死锁（``copilot.py`` 有实测注释）。
4. **回执逐笔如实**。``RouterOutcome.message`` 里的 ``风控拒单[rule_id]：reason``
   与镜像的 ``status/reason`` 原样透出；``skipped`` / ``failed`` / ``error`` /
   ``duplicate`` 在响应里各占一个词，前端不得把它们渲染成成功。

**实盘通道 = 在模拟盘建单 + 镜像真单**，不是第二条下单链路：本仓真单只有
``OrderRouter._mirror_fill`` → ``real_mirror_service`` 这一条（``mirror=True``）。
所以 ``channels`` 里 ``sim`` 恒为真，``real`` 是**叠加**语义而非替代 ——
响应里的 ``channels_effective`` 会如实说明这一笔实际走了哪几条路。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.services.api.user_app.middleware.auth import get_current_user
from backend.shared.logging_config import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/stock-terminal", tags=["CandidatePush"])

#: 单批上限。选了 50 只以上的多半是误操作（「全选」），而下单是逐笔串行 + 每笔一次风控，
#: 50 笔已经要跑好几秒；超过就要求分两批，宁可多一次确认也不要一次误发 500 笔。
MAX_BATCH_SYMBOLS = 50

#: 卖出的整仓语义下逐笔都要查可用持仓，但单批仍沿用同一上限
MAX_QUANTITY = 1_000_000


class PushIn(BaseModel):
    """预检与推送共用载荷（预检时只有 ``batch_id`` 会被回显，不产生副作用）。"""

    symbols: list[str] = Field(..., min_length=1, max_length=MAX_BATCH_SYMBOLS)
    side: Literal["buy", "sell"]
    channels: list[Literal["sim", "real"]] = Field(..., min_length=1)
    batch_id: str = Field(
        ...,
        min_length=8,
        max_length=64,
        description="前端打开确认面板时生成一次的稳定 ID，幂等键的一部分；必填以保证重复点击不重复下单",
    )
    quantities: dict[str, float] | None = Field(
        None, description="{symbol: 股数} 手填覆盖；缺省由服务端按可用资金×仓位信号算"
    )
    ack_risk: bool = Field(
        False,
        description="已确认并接受名单/新闻命中（不改风控裁定，只解名单/新闻阻断）",
    )
    dry_run: bool = Field(
        False, description="只跑预检不落单（与 /preflight 等价，便于前端只发一个请求）"
    )


# ---------------------------------------------------------------------------
# 服务端解析：价格 / 仓位信号 / 风险载荷
# ---------------------------------------------------------------------------


def _resolve_prices(symbols: list[str]) -> dict[str, float]:
    """从全市场快照取最新收盘价（与候选列表同一份 ``_load_universe`` 缓存）。

    ``_load_universe`` 有 300s 进程内缓存 + 双检锁，逐笔调用不会放大成 N 次全量扫描。
    查不到就**不编造**：缺价的腿在下面会被 ``plan_quantity`` 判 blocked。
    """
    from backend.services.api.routers.stock_terminal import _load_universe

    try:
        df, _trade_date = _load_universe()
    except Exception as exc:  # noqa: BLE001 - 快照不可用 → 全部缺价，由 plan_quantity 如实阻断
        logger.warning("[push] 全市场快照不可用，所有腿将按「无有效价格」阻断: %s", exc)
        return {}

    wanted = set(symbols)
    out: dict[str, float] = {}
    try:
        sub = df[df["Symbol"].isin(wanted)]
        for _, row in sub.iterrows():
            sym = str(row.get("Symbol") or "")
            px = row.get("close")
            try:
                px_f = float(px)
            except (TypeError, ValueError):
                continue
            if sym and px_f > 0:
                out[sym] = px_f
    except Exception as exc:  # noqa: BLE001
        logger.warning("[push] 快照取价失败: %s", exc)
    return out


async def _resolve_position_scores(symbols: list[str]) -> tuple[dict[str, float], str]:
    """取最近一个信号日的 ``quality->position->position_score``。

    读法与候选列表 (``stock_terminal.py`` 的 overlay) 同源：只为被选中的标的取数，
    不做全表扫描。返回 ``({裸码: score}, signal_date)``；查不到就是查不到，
    空 dict 表示这批腿在该信号日都没有仓位信号（``plan_quantity`` 会如实阻断）。
    """
    from backend.shared.database_manager_v2 import get_session
    from sqlalchemy import text as _text

    bare = {s.split(".")[0]: s for s in symbols}
    out: dict[str, float] = {}
    signal_date = ""
    try:
        async with get_session() as session:
            row = (
                await session.execute(
                    _text("SELECT max(trade_date) FROM engine_signal_scores")
                )
            ).fetchone()
            latest = row[0] if row else None
            if latest is None:
                return {}, ""
            signal_date = str(latest)[:10]
            rows = (
                await session.execute(
                    _text(
                        "SELECT symbol, quality FROM engine_signal_scores "
                        "WHERE trade_date = :d AND symbol = ANY(:syms)"
                    ),
                    {"d": latest, "syms": sorted(bare.keys())},
                )
            ).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[push] 仓位信号读取失败: %s", exc)
        return {}, ""

    for r in rows:
        sym = str(r[0])
        quality = r[1] if isinstance(r[1], dict) else None
        pos = (quality or {}).get("position") if quality else None
        score = (pos or {}).get("position_score") if isinstance(pos, dict) else None
        if score is None:
            continue
        try:
            out[sym] = float(score)
        except (TypeError, ValueError):
            continue
    return out, signal_date


async def _resolve_risk(symbols: list[str]) -> dict[str, Any]:
    """名单命中 + 近 20 天新闻标签（与候选列表同一套通道，不二创口径）。"""
    from backend.services.api import stock_terminal_exclusions as excl

    result: dict[str, Any] = {"by_symbol": {}, "meta": {}, "imported": True}
    try:
        lst, blocked, meta = excl.list_channel()
        result["meta"] = dict(meta or {})
        result["imported"] = bool((meta or {}).get("imported", True))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[push] 排除名单读取失败（按未导入处理，不静默当空名单）: %s", exc
        )
        result["imported"] = False
        return result

    try:
        news_map = await excl.news_annotations(symbols)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[push] 新闻标签读取失败: %s", exc)
        news_map = {}

    for sym in symbols:
        try:
            hit = excl.row_risk(sym, lst=lst, blocked=blocked, news=news_map.get(sym))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[push] 逐笔风险载荷失败 %s: %s", sym, exc)
            hit = None
        if hit:
            result["by_symbol"][sym] = hit
    return result


def _list_blocking(risk: dict[str, Any] | None) -> tuple[bool, str]:
    """名单/新闻是否阻断这一笔（``ack_risk`` 只能解这一层，解不了风控裁定）。"""
    if not risk:
        return False, ""
    if risk.get("excluded"):
        hits = risk.get("hits") or []
        why = "；".join(str(h.get("reason") or h.get("source") or "") for h in hits[:3])
        return True, f"在排除名单内（{why or '名单命中'}）"
    news = risk.get("news") or {}
    if news.get("risk"):
        tags = news["risk"][:3]
        why = "、".join(str(t.get("tag") or "") for t in tags)
        return True, f"近 20 天新闻利空（{why}）"
    return False, ""


# ---------------------------------------------------------------------------
# 预检
# ---------------------------------------------------------------------------


def _mirror_plan(
    redis: Any, *, real: bool, legs: list[dict[str, Any]]
) -> dict[str, Any]:
    """实盘侧闸门快照 + 逐笔「这一笔会不会被跳」。

    **只读**：``status_snapshot`` 不产生副作用。配额是按剩余量**推算**的，不是预留 ——
    真正的预留发生在提交那一刻（``_reserve_quota``），这里有偏差但方向安全：
    预检说「够」，提交时若被别人用掉会如实返回 ``skipped(max_daily_*)``。
    """
    if not real:
        return {"requested": False}
    from backend.services.live_trading.services import real_mirror_service as mirror

    try:
        status = mirror.status_snapshot(redis)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[push] 镜像控制面不可读: %s", exc)
        return {"requested": True, "available": False, "reason": str(exc)[:200]}

    cfg = status.get("config") or {}
    quota = status.get("quota") or {}
    used_orders = int(quota.get("daily_orders") or 0)
    used_symbols = int(quota.get("daily_symbols") or 0)
    used_value = float(quota.get("daily_value") or 0.0)
    max_orders = int(cfg.get("max_daily_orders") or 0)
    max_symbols = int(cfg.get("max_daily_symbols") or 0)
    max_value = float(cfg.get("max_daily_value") or 0.0)

    # 逐笔试算：只累计**可执行**的腿，模拟镜像侧「先到先得」的排队顺序，
    # 让用户在点确认之前就看到「第 6 只起会被配额跳过」。
    running_orders = used_orders
    running_value = used_value
    seen_symbols: set[str] = set()
    running_symbols = used_symbols
    for leg in legs:
        # 先落一个「不跳过」的显式结果：``None`` 与 ``{"will_skip": false}`` 在界面上
        # 是两件事（没查 / 查了没事），而这两者在 sim-only 与 real 之间恰好都要区分。
        leg["mirror_precheck"] = {"will_skip": False, "reason": ""}
        if not leg.get("executable"):
            continue
        sym = str(leg.get("symbol") or "")
        value = float(leg.get("amount") or 0)
        if sym not in seen_symbols:
            seen_symbols.add(sym)
            running_symbols += 1
        running_orders += 1
        running_value += value
        reason = ""
        if max_orders and running_orders > max_orders:
            reason = "max_daily_orders"
        elif max_symbols and running_symbols > max_symbols:
            reason = "max_daily_symbols"
        elif max_value and running_value > max_value:
            reason = "max_daily_value"
        elif cfg.get("max_order_value") and value > float(cfg["max_order_value"]):
            reason = "max_order_value"
        else:
            continue
        leg["mirror_precheck"] = {"will_skip": True, "reason": reason}
        leg["executable"] = False
        leg["problem"] = (
            leg.get("problem") or f"实盘配额不足（{reason}），该笔不会下发真单"
        )

    return {
        "requested": True,
        "available": True,
        "enabled": status.get("enabled"),
        "kill_switch": status.get("kill_switch"),
        "trading_time": status.get("trading_time"),
        "real_trading_ready": status.get("real_trading_ready"),
        "blocked_reason": status.get("blocked_reason") or "",
        # 通道不就绪的原因（与 blocked_reason 是两个问题：镜像开着也可能不就绪）
        "not_ready_reason": status.get("not_ready_reason") or "",
        # 非交易时段下单不会丢，而是入队等开盘 —— 对真钱来说「排队」与「已成交」
        # 必须分清，否则用户以为买到了，实际排在队列里。
        "will_queue": bool(
            not status.get("trading_time") and cfg.get("queue_outside_hours")
        ),
        "broker_selected": status.get("broker_selected") or "",
        "whitelist": status.get("whitelist") or [],
        "blacklist": status.get("blacklist") or [],
        "queue_length": status.get("queue_length"),
        "config": cfg,
        "quota": {
            **quota,
            "remaining_orders": max(0, max_orders - used_orders)
            if max_orders
            else None,
            "remaining_symbols": max(0, max_symbols - used_symbols)
            if max_symbols
            else None,
            "remaining_value": round(max(0.0, max_value - used_value), 2)
            if max_value
            else None,
        },
    }


async def _build_legs(
    body: PushIn, current_user: dict, *, redis: Any, db_available: bool
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """逐笔组装确认面板的行（预检与推送共用，保证「看到的」与「发出去的」同源）。"""
    from backend.services.trade_shared.simulation_manager import (
        SimulationAccountManager,
        require_sim_user_id,
    )
    from backend.shared.stock_utils import StockCodeUtil

    tenant_id = str(current_user.get("tenant_id") or "default")
    uid = int(
        require_sim_user_id(str(current_user.get("user_id") or ""), tenant_id=tenant_id)
    )

    # 三形归一为后缀式（parquet/风控/镜像同口径），去重后保持用户勾选顺序
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in body.symbols:
        sym = StockCodeUtil.to_suffix(str(raw or "").strip().upper())
        if not sym or sym in seen:
            continue
        seen.add(sym)
        normalized.append(sym)
    if not normalized:
        raise HTTPException(status_code=400, detail="没有可识别的股票代码")

    prices = _resolve_prices(normalized)
    scores, signal_date = await _resolve_position_scores(normalized)
    risk = await _resolve_risk(normalized)

    account: dict[str, Any] | None = None
    try:
        account = await SimulationAccountManager(redis).get_account(uid, tenant_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[push] 模拟账户读取失败（按零资金处理，逐笔如实阻断）: %s", exc)

    available_cash = float((account or {}).get("cash") or 0.0)
    positions = (account or {}).get("positions") or {}

    from backend.shared.push_plan import apply_batch_scale, plan_quantity

    legs: list[dict[str, Any]] = []
    for sym in normalized:
        bare = sym.split(".")[0]
        price = prices.get(sym)
        risk_payload = risk["by_symbol"].get(sym)
        pos = _find_position(positions, sym)
        available_position = float(pos.get("available_volume") or 0) if pos else 0.0

        override = None
        if body.quantities:
            for key in (sym, bare, StockCodeUtil.to_prefix(sym)):
                if key in body.quantities:
                    override = body.quantities[key]
                    break

        plan = plan_quantity(
            symbol=sym,
            side=body.side,
            price=price,
            position_score=scores.get(bare),
            available_cash=available_cash,
            available_position=available_position,
            override=override,
        )

        leg: dict[str, Any] = {
            "symbol": sym,
            "name": _name_of(sym),
            "price": price,
            "position_score": scores.get(bare),
            "signal_date": signal_date or None,
            "available_position": available_position,
            "amount": round((price or 0.0) * plan.quantity, 2),
            "risk": risk_payload,
            "blocked_by": "",
            # 镜像预检结果由 _mirror_plan 回填
            "mirror_precheck": None,
        }
        leg.update(plan.as_dict())

        if not plan.executable:
            leg["blocked_by"] = "quantity"
        else:
            hit, why = _list_blocking(risk_payload)
            if hit and not body.ack_risk:
                leg["executable"] = False
                leg["blocked_by"] = "list"
                leg["problem"] = why
        legs.append(leg)

    # 整批资金约束（仅买入）：逐笔各自按全量可用资金算量会系统性超配，
    # 多出来的单子在账户层逐笔被拒，表现为「推 3 只成交 1 只、另两只莫名失败」。
    legs, budget = apply_batch_scale(legs, available_cash, body.side)
    if budget.get("applied"):
        logger.info(
            "[push] 批次资金约束缩量 batch=%s factor=%.4f planned=%.2f cash=%.2f",
            body.batch_id,
            budget["factor"],
            budget["planned_amount"],
            budget["available_cash"],
        )

    # 风控判定：模拟与实盘各跑一遍（同一份规则，账户上下文不同）
    if db_available:
        await _apply_risk_verdicts(
            body, legs, tenant_id=tenant_id, uid=uid, redis=redis
        )

    mirror_info = _mirror_plan(redis, real="real" in body.channels, legs=legs)

    from backend.shared.push_plan import summarize_legs

    summary = summarize_legs(legs)
    meta = {
        "signal_date": signal_date or None,
        "available_cash": round(available_cash, 2),
        "account_found": account is not None,
        "exclusion": risk["meta"],
        "exclusion_imported": risk["imported"],
        "budget": budget,
    }
    return legs, {"mirror": mirror_info, "summary": summary, "meta": meta}


def _name_of(symbol: str) -> str:
    """中文名来自证券主表缓存（取不到就留空，前端显示代码，不编造名字）。"""
    try:
        from backend.services.api.routers.stock_terminal import _load_universe

        df, _ = _load_universe()
        hit = df[df["Symbol"] == symbol]
        if not hit.empty:
            return str(hit.iloc[0].get("Name") or "")
    except Exception:  # noqa: BLE001
        pass
    return ""


def _find_position(positions: dict[str, Any], symbol: str) -> dict[str, Any] | None:
    """持仓 lookup：后缀式（``600036.SH``）/ 前缀式（``SH600036``）/ 裸码，大小写不限。

    键形随写入路径而变（桥返回的前缀式、台账的后缀式、旧数据的裸码），认不出会让卖出
    恒判「无可用持仓」而整批静默跳过 —— 所以先按候选键直查（快路径），再退化为
    「两侧都归一到前缀大写」的比对，覆盖任何别的写法。
    """
    from backend.shared.stock_utils import StockCodeUtil

    def _pick(hit: Any) -> dict[str, Any] | None:
        if isinstance(hit, dict):
            return hit
        if isinstance(hit, list):  # 多空双边持仓
            for item in hit:
                if isinstance(item, dict):
                    return item
        return None

    bare = symbol.split(".")[0]
    upper = symbol.upper()
    for key in (symbol, upper, symbol.lower(), bare, StockCodeUtil.to_prefix(upper)):
        hit = _pick(positions.get(key))
        if hit is not None:
            return hit

    want = StockCodeUtil.to_prefix(upper).upper()
    for key, value in positions.items():
        try:
            if StockCodeUtil.to_prefix(str(key).upper()).upper() == want:
                hit = _pick(value)
                if hit is not None:
                    return hit
        except Exception:  # noqa: BLE001 - 单个畸形键不应中断整批查询
            continue
    return None


async def _apply_risk_verdicts(
    body: PushIn, legs: list[dict[str, Any]], *, tenant_id: str, uid: int, redis: Any
) -> None:
    """逐笔跑风控**判定**（不落留痕），把环境闸与标的闸分开挂到腿上。

    同一份 18 条规则、同一份代码；实盘腿走 ``DirectOrderReq(trading_mode=REAL)``
    使账户上下文取真账户快照 —— 若实盘与模拟各写一套判据，两边就会给出不同结论，
    而用户看到的是同一个确认面板。
    """
    from backend.services.trade.services.risk_gate_service import (
        DirectOrderReq,
        preflight_order,
    )
    from backend.shared.database_manager_v2 import get_session
    from backend.shared.push_plan import classify_decisions

    real = "real" in body.channels
    try:
        async with get_session(read_only=False) as session:
            for leg in legs:
                qty = float(leg.get("quantity") or 0)
                if qty <= 0:
                    continue
                symbol = str(leg.get("symbol") or "")
                req: Any
                if real:
                    req = DirectOrderReq(
                        tenant_id=tenant_id,
                        user_id=uid,
                        symbol=symbol,
                        side=body.side,
                        quantity=qty,
                        price=leg.get("price"),
                        remarks=f"candidate_push:{body.batch_id}",
                    )
                else:
                    from backend.services.simulation.services.order_router import (
                        OrderRequest,
                    )

                    req = OrderRequest(
                        tenant_id=tenant_id,
                        user_id=uid,
                        symbol=symbol,
                        side=body.side,
                        quantity=qty,
                        price=leg.get("price"),
                        remarks=f"candidate_push:{body.batch_id}",
                    )
                try:
                    verdict = await preflight_order(req, db=session, redis=redis)
                except Exception as exc:  # noqa: BLE001 - 单腿判定失败不阻断其余腿
                    logger.warning("[push] 预检判定失败 %s: %s", symbol, exc)
                    leg["risk_verdict"] = "error"
                    leg["environment"] = []
                    leg["subject"] = []
                    continue

                split = classify_decisions(verdict.decisions)
                leg["risk_verdict"] = verdict.verdict
                leg["risk_enforced"] = verdict.enforced
                leg["risk_rule_id"] = verdict.rule_id
                leg["risk_reason"] = verdict.reason
                leg["environment"] = split["environment"]
                leg["subject"] = split["subject"]

                # 只有「已生效且未通过」才阻断 —— 影子期（shadow=true）判定照跑但不拦单，
                # 此时预检说「拦」而下单放行，比反过来更让用户困惑。
                if verdict.enforced and not verdict.passed and leg.get("executable"):
                    leg["executable"] = False
                    leg["blocked_by"] = "risk"
                    leg["problem"] = (
                        f"风控拒单[{verdict.rule_id or 'unknown'}]：{verdict.reason or '未说明'}"
                    )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[push] 预检风控链路不可用，逐笔按未判定处理: %s", exc)
        for leg in legs:
            leg.setdefault("risk_verdict", "unavailable")


@router.post("/push-orders/preflight")
async def push_orders_preflight(
    body: PushIn, current_user: dict = Depends(get_current_user)
):
    """推送前预检：逐笔给数量、风险、风控裁定、实盘配额，**不产生任何副作用**。

    用户明确要求「我推送前，都需要系统帮我排除风险的」—— 这个端点就是那句话的实现：
    返回的 ``legs`` 不是「建议」，而是**下单那一刻会被同样算出来的东西**，
    因为推送端点复用同一个 :func:`_build_legs`。
    """
    from backend.services.trade_shared.redis_client import get_redis as get_trade_redis

    redis = get_trade_redis()
    if getattr(redis, "client", None) is None:
        redis.connect()

    legs, extra = await _build_legs(body, current_user, redis=redis, db_available=True)
    return {
        "success": True,
        "data": {
            "batch_id": body.batch_id,
            "side": body.side,
            "channels": list(body.channels),
            "channels_effective": _channels_effective(body.channels),
            "ack_risk": body.ack_risk,
            "legs": legs,
            **extra,
        },
    }


def _channels_effective(channels: list[str]) -> list[str]:
    """实盘是**叠加**在模拟单上的镜像，不是替代 —— 如实说明这一笔实际走了哪几条路。"""
    out = ["sim"]
    if "real" in channels:
        out.append("real")
    return out


@router.post("/push-orders")
async def push_orders(body: PushIn, current_user: dict = Depends(get_current_user)):
    """逐笔下单（模拟）+ 可选镜像真单。

    逐笔循环、**单笔失败不阻断其余**（照抄 ``copilot.execute_advice`` 的批量范式）；
    幂等键 ``cand-{batch_id}-{symbol}-{side}`` 保证重复点击不重复下单。
    """
    from backend.services.simulation.services.order_router import (
        OrderRequest,
        submit_order,
    )
    from backend.services.trade_shared.redis_client import get_redis as get_trade_redis
    from backend.services.trade_shared.simulation_manager import require_sim_user_id
    from backend.shared.database_manager_v2 import get_session
    from backend.shared.order_contract import (
        SOURCE_CANDIDATE_PUSH,
        build_candidate_client_order_id,
    )

    tenant_id = str(current_user.get("tenant_id") or "default")
    uid = int(
        require_sim_user_id(str(current_user.get("user_id") or ""), tenant_id=tenant_id)
    )

    redis = get_trade_redis()
    if getattr(redis, "client", None) is None:
        redis.connect()

    legs, extra = await _build_legs(body, current_user, redis=redis, db_available=True)

    if body.dry_run:
        return {
            "success": True,
            "data": {
                "batch_id": body.batch_id,
                "dry_run": True,
                "status": "preview",
                "channels": list(body.channels),
                "channels_effective": _channels_effective(body.channels),
                "legs": legs,
                **extra,
            },
        }

    real = "real" in body.channels
    blocked = [leg for leg in legs if not leg.get("executable")]
    if blocked and not any(leg.get("executable") for leg in legs):
        raise HTTPException(
            status_code=400,
            detail={
                "message": "没有可执行的腿，请先修正确认面板上的阻断项",
                "blocked": [
                    {
                        "symbol": x.get("symbol"),
                        "why": x.get("problem") or x.get("blocked_by"),
                    }
                    for x in blocked
                ],
            },
        )

    # 注：submit_order 内部已持「同用户撮合临界区」锁（唯一入口自带串行化），
    # 调用方**不得**再包 locked_execution——否则同用户重入死锁（见 copilot.py 实测注释）。
    results: list[dict[str, Any]] = []
    async with get_session(read_only=False) as session:
        for leg in legs:
            symbol = str(leg.get("symbol") or "")
            if not leg.get("executable"):
                results.append(
                    {
                        "symbol": symbol,
                        "success": False,
                        "executed": False,
                        "skipped_reason": leg.get("problem")
                        or leg.get("blocked_by")
                        or "blocked",
                        "message": leg.get("problem") or "预检阻断，未下单",
                        "duplicate": False,
                        "mirror": None,
                    }
                )
                continue
            try:
                outcome = await submit_order(
                    session,
                    redis,
                    OrderRequest(
                        tenant_id=tenant_id,
                        user_id=uid,
                        symbol=symbol,
                        side=body.side,
                        quantity=float(leg.get("quantity") or 0),
                        order_type="market",
                        price=leg.get("price"),
                        source=SOURCE_CANDIDATE_PUSH,
                        client_order_id=build_candidate_client_order_id(
                            body.batch_id, symbol, body.side
                        ),
                        remarks=f"候选信号一键推送 batch={body.batch_id}",
                        mirror=real,
                        mirror_source=SOURCE_CANDIDATE_PUSH if real else "",
                    ),
                )
                results.append(
                    {
                        "symbol": symbol,
                        "success": bool(outcome.success),
                        "executed": True,
                        "order_id": outcome.order_id,
                        "trade_id": outcome.trade_id,
                        "fill_price": outcome.fill_price,
                        "filled_quantity": outcome.filled_quantity,
                        "commission": outcome.commission,
                        "message": outcome.message,
                        "duplicate": bool(outcome.duplicate),
                        "mirror": _mirror_receipt(outcome, real=real),
                    }
                )
            except Exception as exc:  # noqa: BLE001 - 单腿失败不阻断其余
                logger.warning("[push] 下单失败 %s %s: %s", symbol, body.side, exc)
                results.append(
                    {
                        "symbol": symbol,
                        "success": False,
                        "executed": True,
                        "message": str(exc)[:200],
                        "duplicate": False,
                        "mirror": None,
                    }
                )

    attempted = [r for r in results if r.get("executed")]
    ok = sum(1 for r in attempted if r.get("success"))
    if not attempted:
        status = "blocked"
    elif ok == len(attempted):
        status = "executed"
    elif ok == 0:
        status = "failed"
    else:
        status = "partial"

    from backend.shared.push_plan import summarize_legs

    return {
        "success": True,
        "data": {
            "batch_id": body.batch_id,
            "dry_run": False,
            "status": status,
            "side": body.side,
            "channels": list(body.channels),
            "channels_effective": _channels_effective(body.channels),
            "results": results,
            "summary": {
                **summarize_legs(legs),
                "attempted": len(attempted),
                "succeeded": ok,
                "failed": len(attempted) - ok,
                "skipped": len(results) - len(attempted),
            },
            "mirror": extra.get("mirror"),
        },
    }


def _mirror_receipt(outcome: Any, *, real: bool) -> dict[str, Any] | None:
    """把 ``RouterOutcome.mirror`` 翻成「成功/排队/重复/未执行/失败」五选一。

    ``skipped`` / ``failed`` / ``error`` / ``duplicate`` 各有各的词，**绝不合并成成功**：
    用户靠这个字段判断真钱有没有出去。取不到载荷时按 ``unknown``（fail-closed）报。
    """
    if not real:
        return None
    from backend.shared.push_plan import mirror_outcome_class

    payload = getattr(outcome, "mirror", None)
    if not isinstance(payload, dict):
        return {"status": "unknown", "class": "failed", "reason": "镜像回执缺失"}
    status = str(payload.get("status") or "")
    return {
        "status": status or "unknown",
        "class": mirror_outcome_class(status),
        "reason": str(payload.get("reason") or ""),
        "order_id": payload.get("order_id"),
        "client_order_id": payload.get("client_order_id"),
        "detail": {
            k: v
            for k, v in payload.items()
            if k in ("submitted", "failed", "requeued", "dropped")
        },
    }
