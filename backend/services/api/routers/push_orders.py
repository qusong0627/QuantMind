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

from dataclasses import replace
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

from backend.services.api.user_app.middleware.auth import get_current_user
from backend.shared.live_trading_gate import ensure_real_trading_allowed
from backend.shared.logging_config import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/stock-terminal", tags=["CandidatePush"])

#: 单批上限。选了 50 只以上的多半是误操作（「全选」），而下单是逐笔串行 + 每笔一次风控，
#: 50 笔已经要跑好几秒；超过就要求分两批，宁可多一次确认也不要一次误发 500 笔。
MAX_BATCH_SYMBOLS = 50

#: 卖出的整仓语义下逐笔都要查可用持仓，但单批仍沿用同一上限
MAX_QUANTITY = 1_000_000

#: 逐笔限价的 schema 上界（A 股最高价约 2,600 元；留两个数量级只是防手滑多打几个 0，
#: 真正的合理性判断在 ``lot_rules.resolve_limit_price`` 的 ±20% 带上）
MAX_LIMIT_PRICE = 100_000.0


class PushLegIn(BaseModel):
    """逐笔形态的一条腿：方向（与可选限价）都写在腿上。

    来自 2026-09-23 实盘实录：一次调仓决策常常同时有买有卖（卖 002518 买 600276），
    而整批一个 ``side`` 的旧契约表达不出来 —— 只能拆成两批，两批之间还有先卖后买的
    资金顺序问题。限价同理：调用方的 ``limit_px`` 是"贴着打保成交"的价，服务端算死
    的 ``ref ± 2%`` 表达不了。
    """

    symbol: str = Field(..., min_length=1, max_length=32)
    side: Literal["buy", "sell"]
    quantity: float | None = Field(
        None,
        gt=0,
        le=MAX_QUANTITY,
        description="本腿股数；缺省由服务端按可用资金×仓位信号算",
    )
    limit_price: float | None = Field(
        None,
        gt=0,
        le=MAX_LIMIT_PRICE,
        description=(
            "本腿限价；仅约束实盘真单（模拟腿按服务端快照价撮合）。"
            "须落在参考价 ±max_slippage_pct 的带内（买不得更高、卖不得更低），越界在预检即阻断"
        ),
    )


class PushIn(BaseModel):
    """预检与推送共用载荷（预检时只有 ``batch_id`` 会被回显，不产生副作用）。

    两种形态**互斥**，必须二选一：

    * 整批同向（旧）：``symbols`` + ``side``；
    * 逐笔（新）：``orders``，方向与限价写在每条腿上。

    混用直接 422 而不是"取其一"：两种形态的 side 语义不同，猜错了代价是真单。
    """

    symbols: list[str] | None = Field(None, min_length=1, max_length=MAX_BATCH_SYMBOLS)
    side: Literal["buy", "sell"] | None = None
    orders: list[PushLegIn] | None = Field(
        None, min_length=1, max_length=MAX_BATCH_SYMBOLS
    )
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

    @model_validator(mode="after")
    def _check_shape(self) -> PushIn:
        has_legacy = self.symbols is not None or self.side is not None
        if has_legacy and self.orders is not None:
            raise ValueError(
                "symbols/side 与 orders 不能同时给：前者整批同向、后者逐笔"
            )
        if not has_legacy and self.orders is None:
            raise ValueError("必须给 symbols+side（整批同向）或 orders（逐笔）")
        if has_legacy and (self.symbols is None or self.side is None):
            raise ValueError("整批形态必须同时给 symbols 与 side")
        if self.orders is not None:
            if self.quantities:
                raise ValueError(
                    "orders 形态的数量写在每条腿的 quantity 上，不能再给 quantities"
                )
            seen: set[str] = set()
            for leg in self.orders:
                key = leg.symbol.strip().upper()
                if key in seen:
                    raise ValueError(
                        f"orders 里 {leg.symbol} 重复：同一批里同一只票只能出现一次"
                    )
                seen.add(key)
        return self

    def leg_plan(self) -> list[PushLegIn]:
        """归一成逐笔形态（整批形态按 ``side`` 展开）—— 下游只认这一种。"""
        if self.orders is not None:
            return list(self.orders)
        return [
            PushLegIn(symbol=s, side=self.side)  # type: ignore[arg-type]
            for s in (self.symbols or [])
        ]

    def batch_side_label(self) -> str:
        """回执里的方向标签：同向时报该方向，混合时报 ``mixed``。

        绝不能随便挑一个方向报出去 —— 前端会把卖单渲染成买单。
        """
        sides = {leg.side for leg in self.leg_plan()}
        if len(sides) == 1:
            return next(iter(sides))
        return "mixed"


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


def _list_gate(
    side: str, risk: dict[str, Any] | None, ack_risk: bool
) -> tuple[bool, str, str]:
    """名单/新闻对**这一笔**的处置 → ``(是否阻断, problem, note)``。

    **卖出不阻断，只提示。** 名单是买入纪律（用户原话「我不买入的」）：持有期里标的
    进了名单（利空、连续亏损、逼近退市），用户恰恰要卖出来减风险 —— 把卖出一起挡住
    等于把人锁在仓位里，而且「在排除名单内」对卖出根本不是理由。实测持有 27 只的
    账户里两只全在名单上，若不分开，任何一次卖出推送都是 0 成交。
    """
    hit, why = _list_blocking(risk)
    if not hit:
        return False, "", ""
    if str(side or "").strip().lower() == "sell":
        return False, "", f"{why}（卖出不受名单限制，仅提示）"
    if ack_risk:
        return False, "", ""
    return True, why, ""


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

    # 三形归一为后缀式（parquet/风控/镜像同口径），去重后保持用户勾选顺序。
    # 整批形态下同一只票只出现一次（旧行为）；逐笔形态下重复已在契约层 422。
    plan_lines: list[tuple[PushLegIn, str]] = []
    seen: set[str] = set()
    for line in body.leg_plan():
        sym = StockCodeUtil.to_suffix(str(line.symbol or "").strip().upper())
        if not sym or sym in seen:
            continue
        seen.add(sym)
        plan_lines.append((line, sym))
    if not plan_lines:
        raise HTTPException(status_code=400, detail="没有可识别的股票代码")
    normalized = [sym for _line, sym in plan_lines]

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

    # 卖出的可卖量还要看实盘账户：模拟台账没有的票，可能只持有在实盘（直发，Phase 4）。
    # 买入不读 —— 与实盘持仓无关，白读一次 PG 只会让预检变慢。
    # 整批只读一次：混向批次里只要有**任一**卖腿就得读（逐腿判断会读 N 次同一份快照）。
    has_sell = any(line.side == "sell" for line, _sym in plan_lines)
    real_positions: dict[str, Any] = {}
    real_meta: dict[str, Any] = {}
    real_known = False
    if has_sell:
        try:
            from backend.shared.real_positions import load_real_positions

            real_positions, real_meta = await load_real_positions(
                tenant_id, str(current_user.get("user_id") or "")
            )
            # 「收到过快照」与「持有 0 股」是两件事：只有前者才敢对用户说未持有。
            real_known = bool(real_meta.get("sources"))
        except Exception as exc:  # noqa: BLE001 - 读不到不等于空仓，交给来源裁定如实标注
            logger.warning("[push] 实盘持仓读取失败（按读不到处理）: %s", exc)

    from backend.shared.push_plan import (
        apply_batch_scale,
        choose_sell_source,
        plan_quantity,
    )

    real_wanted = "real" in body.channels
    # 限价的带（镜像配置）只在真要下真单时才读 —— 纯模拟批次不碰 Redis。
    # 派生与校验都要用它（没人给限价时也要按它派生），不能只在"有人给了限价"时才读。
    max_slip = _mirror_max_slip(redis) if real_wanted else 0.0

    legs: list[dict[str, Any]] = []
    for line, sym in plan_lines:
        leg_side = line.side
        is_sell = leg_side == "sell"
        bare = sym.split(".")[0]
        price = prices.get(sym)
        risk_payload = risk["by_symbol"].get(sym)
        pos = _find_position(positions, sym)
        available_position = float(pos.get("available_volume") or 0) if pos else 0.0
        source_plan = None
        if is_sell:
            real_pos = _find_position(real_positions, sym)
            real_available = (
                (float(real_pos.get("available_volume") or 0) if real_pos else 0.0)
                if real_known
                else None
            )
            source_plan = choose_sell_source(
                sim_available=available_position,
                real_available=real_available,
                real_requested=real_wanted,
            )
            available_position = source_plan.available

        # 手填量：逐笔形态写在腿上（``quantity``），整批形态走 ``quantities`` 映射。
        override = line.quantity
        if override is None and body.quantities:
            for key in (sym, bare, StockCodeUtil.to_prefix(sym)):
                if key in body.quantities:
                    override = body.quantities[key]
                    break

        plan = plan_quantity(
            symbol=sym,
            side=leg_side,
            price=price,
            position_score=scores.get(bare),
            available_cash=available_cash,
            available_position=available_position,
            override=override,
        )

        # 来源裁定的阻断理由优先于 plan_quantity 的通用句：后者只会说「无可用持仓
        # （T+1 锁定或未持有）」，而用户屏幕上明明看得见这只持仓 —— 那是句假话。
        if source_plan is not None and source_plan.problem:
            plan = replace(
                plan,
                quantity=0.0,
                source="blocked",
                note="",
                problem=source_plan.problem,
            )

        leg: dict[str, Any] = {
            "symbol": sym,
            # 逐笔方向：回执、风控判定、幂等键、下单请求全按它走，不再读 batch 级 side
            "side": leg_side,
            "name": _name_of(sym),
            "price": price,
            "position_score": scores.get(bare),
            "signal_date": signal_date or None,
            "available_position": available_position,
            "amount": round((price or 0.0) * plan.quantity, 2),
            "risk": risk_payload,
            "blocked_by": "",
            # 可卖量的取数来源与下单路径（`sim` 走模拟台账；`real_direct` 实盘独有持仓直发）
            "position_source": source_plan.source if source_plan else "sim",
            "exec_path": source_plan.exec_path if source_plan else "sim",
            # 限价（仅实盘真单）：由下面的 _resolve_leg_limit 回填
            "limit_price": None,
            "limit_source": "",
            "limit_problem": "",
            # 镜像预检结果由 _mirror_plan 回填
            "mirror_precheck": None,
        }
        leg.update(plan.as_dict())
        if source_plan is not None and source_plan.note and leg.get("executable"):
            leg["note"] = (
                f"{leg['note']}；{source_plan.note}"
                if leg.get("note")
                else source_plan.note
            )

        if not plan.executable:
            leg["blocked_by"] = "quantity"
        else:
            blocked, problem, note = _list_gate(leg_side, risk_payload, body.ack_risk)
            if blocked:
                leg["executable"] = False
                leg["blocked_by"] = "list"
                leg["problem"] = problem
            elif note:
                leg["note"] = f"{leg.get('note')}；{note}" if leg.get("note") else note

        _apply_leg_limit(
            leg,
            side=leg_side,
            requested=line.limit_price,
            real_wanted=real_wanted,
            max_slip=max_slip,
        )
        legs.append(leg)

    # 整批资金约束（仅买入）：逐笔各自按全量可用资金算量会系统性超配，
    # 多出来的单子在账户层逐笔被拒，表现为「推 3 只成交 1 只、另两只莫名失败」。
    # 混向批次里只缩买腿 —— 卖腿是回收资金，缩它等于凭空少卖。
    legs, budget = apply_batch_scale(legs, available_cash, body.side or "")
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
        # 卖出取数的第二个来源（没有卖腿的批次不查，如实报 null 而不是编一个空快照）
        "real_positions": (
            {
                "known": real_known,
                "held": len(real_positions),
                "snapshot_at": real_meta.get("snapshot_at"),
                "sources": real_meta.get("sources") or {},
                "active_broker": real_meta.get("active_broker"),
            }
            if has_sell
            else None
        ),
    }
    return legs, {"mirror": mirror_info, "summary": summary, "meta": meta}


def _mirror_max_slip(redis: Any) -> float:
    """镜像的滑点带（``ref ± max_slippage_pct``）—— 预检与真单**必须同源**。

    预检若用另一个数（哪怕只差一点），就会出现「确认面板放行、点下去被镜像拒」，
    而用户已经按下去了。
    """
    from backend.services.live_trading.services import real_mirror_service as mirror

    # load_config 自身吞掉 Redis 读取异常（读不到就用 env 基线），不会抛。
    return float(mirror.load_config(redis).max_slippage_pct)


def _apply_leg_limit(
    leg: dict[str, Any],
    *,
    side: str,
    requested: float | None,
    real_wanted: bool,
    max_slip: float,
) -> None:
    """把这一腿的限价定下来并写回 ``leg``（就地，回填字段已在 leg 里预置）。

    * ``real_wanted=False``：限价**不参与**模拟腿撮合（模拟按服务端快照价成交）。
      给了合法限价就如实说明它约束的是谁；给了越界限价只加提示、**不阻断** ——
      那条价根本不参与这次撮合，为它挡掉一笔模拟单是误伤。
    * ``real_wanted=True``：买不得高于 / 卖不得低于 ``ref ± max_slip``，越界即阻断。
      在预检就拦下，用户看得见；等真单提交时才被镜像拒，用户只收到一条失败回执。
    """
    from backend.services.live_trading.services import lot_rules

    if not real_wanted:
        if requested is not None:
            leg["note"] = _append_note(
                leg.get("note"),
                f"限价 {requested:g} 仅约束实盘真单；模拟腿按快照价撮合",
            )
        return

    price, source, problem = _resolve_leg_limit(
        side=side,
        reference=leg.get("price"),
        requested=requested,
        max_slip=max_slip,
    )
    leg["limit_price"] = price
    leg["limit_source"] = source
    if not problem:
        return
    leg["limit_problem"] = problem
    if leg.get("executable"):
        leg["executable"] = False
        leg["blocked_by"] = "limit"
        leg["problem"] = f"限价越界（{lot_rules.describe_limit_problem(problem)}）"


def _resolve_leg_limit(
    *,
    side: str,
    reference: float | None,
    requested: float | None,
    max_slip: float,
) -> tuple[float | None, str, str]:
    """→ ``(生效限价, 来源, 问题)``；来源 ``requested``（采纳调用方）/ ``derived``（服务端派生）。"""
    from backend.services.live_trading.services import lot_rules

    price, problem = lot_rules.resolve_limit_price(
        side, reference, requested=requested, max_slip=max_slip
    )
    if problem:
        return None, "", problem
    return price, "requested" if requested is not None else "derived", ""


def _append_note(note: Any, extra: str) -> str:
    """note 是「；」串起来的多句；追加一句（空 note 时不留下前导分隔符）。"""
    head = str(note or "").strip()
    return f"{head}；{extra}" if head else extra


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
                # 方向取**这一笔**的：混向批次里用 batch 级 side 会把卖单判成买单
                leg_side = str(leg.get("side") or "")
                req: Any
                if real:
                    req = DirectOrderReq(
                        tenant_id=tenant_id,
                        user_id=uid,
                        symbol=symbol,
                        side=leg_side,
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
                        side=leg_side,
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


def _ensure_channels_allowed(body: PushIn) -> None:
    """实盘闸门关闭时拒绝 ``real`` 通道。

    **显式 403，不静默降级成纯模拟**：调用方勾了「实盘镜像」却只拿到一笔虚拟撮合，
    比报错糟得多——用户会以为下的是真单。前端 ``pushModel.pushGate`` 已有一份
    同样的判定（开关关闭时连勾选项都不渲染），这里是兜底，防的是绕过 UI 的直接
    调用与旧客户端。

    走共享闸门（而不是自己读 env），403 的机器可读 detail 与中间件一致，
    前端一处识别即可。
    """
    ensure_real_trading_allowed("REAL" if "real" in body.channels else "SIMULATION")


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

    _ensure_channels_allowed(body)
    redis = get_trade_redis()
    if getattr(redis, "client", None) is None:
        redis.connect()

    legs, extra = await _build_legs(body, current_user, redis=redis, db_available=True)
    return {
        "success": True,
        "data": {
            "batch_id": body.batch_id,
            "side": body.batch_side_label(),
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
    _ensure_channels_allowed(body)

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
                        "side": str(leg.get("side") or ""),
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
                if str(leg.get("exec_path") or "") == "real_direct":
                    results.append(
                        await _execute_real_direct(
                            leg,
                            body=body,
                            tenant_id=tenant_id,
                            uid=uid,
                            redis=redis,
                            db=session,
                        )
                    )
                    continue
                leg_side = str(leg.get("side") or "")
                outcome = await submit_order(
                    session,
                    redis,
                    OrderRequest(
                        tenant_id=tenant_id,
                        user_id=uid,
                        symbol=symbol,
                        side=leg_side,
                        quantity=float(leg.get("quantity") or 0),
                        order_type="market",
                        price=leg.get("price"),
                        source=SOURCE_CANDIDATE_PUSH,
                        client_order_id=build_candidate_client_order_id(
                            body.batch_id, symbol, leg_side
                        ),
                        remarks=f"候选信号一键推送 batch={body.batch_id}",
                        mirror=real,
                        mirror_source=SOURCE_CANDIDATE_PUSH if real else "",
                        # 限价只对真单有意义（模拟腿按快照价撮合），没开实盘就不传
                        real_limit_price=leg.get("limit_price") if real else None,
                    ),
                )
                results.append(
                    {
                        "symbol": symbol,
                        "side": leg_side,
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
                logger.warning(
                    "[push] 下单失败 %s %s: %s", symbol, leg.get("side"), exc
                )
                results.append(
                    {
                        "symbol": symbol,
                        "side": str(leg.get("side") or ""),
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
            "side": body.batch_side_label(),
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


async def _execute_real_direct(
    leg: dict[str, Any],
    *,
    body: PushIn,
    tenant_id: str,
    uid: int,
    redis: Any,
    db: Any,
) -> dict[str, Any]:
    """实盘独有持仓直卖：**不经模拟台账**，直接向真账户下卖单。

    刻意复用 ``mirror_virtual_fill`` 而不是另开一条下单路径：用户已有的急停开关、
    白/黑名单、价格偏离闸门、持仓与当日限额全在那条链上 —— 「一键卖出」若绕开它，
    就会出现「点了急停，持仓页的卖出按钮照样把真单打出去」。与镜像路径的唯一区别是
    这只票没有模拟腿：``trigger`` 改写通知首句（原句「模拟盘成交触发真单镜像」在这里
    是假话），``source`` 标 ``real_direct``（真钱留痕要分得清是谁发起的）。
    """
    from backend.services.live_trading.services.real_mirror_service import (
        mirror_virtual_fill,
    )
    from backend.shared.order_contract import (
        SOURCE_REAL_DIRECT,
        build_candidate_client_order_id,
    )
    from backend.shared.push_plan import mirror_outcome_class

    symbol = str(leg.get("symbol") or "")
    quantity = float(leg.get("quantity") or 0)
    try:
        payload = await mirror_virtual_fill(
            db=db,
            redis=redis,
            tenant_id=tenant_id,
            user_id=str(uid),
            symbol=symbol,
            side="SELL",
            quantity=quantity,
            price=float(leg.get("price") or 0),
            client_order_id=build_candidate_client_order_id(
                body.batch_id, symbol, "sell"
            ),
            source=SOURCE_REAL_DIRECT,
            trigger="用户从持仓页一键卖出实盘持仓（不经模拟台账）",
            limit_price=leg.get("limit_price"),
        )
    except Exception as exc:  # noqa: BLE001 - 单腿失败不阻断其余（mirror 本就不该抛）
        logger.warning("[push] 实盘直卖异常 %s: %s", symbol, exc)
        payload = {"status": "error", "reason": str(exc), "symbol": symbol}

    status = str(payload.get("status") or "error")
    cls = mirror_outcome_class(status)
    reason = str(payload.get("reason") or "")
    limit_price = payload.get("limit_price")
    # 「已提交」「排队中」「重复跳过」都是**受理**，但都不等于已成交：措辞分开，
    # 真钱路径上把排队/重复说成「已卖出」是最贵的一种错。
    message = {
        "success": f"实盘真单已提交（限价 {limit_price}）",
        "queued": "非交易时段：真单已入队，开盘后自动下发（尚未成交）",
        "duplicate": "这一笔此前已提交过（幂等跳过，未重复下单）",
        "skipped": f"实盘闸门未放行：{reason or '未说明'}",
    }.get(cls, f"实盘真单失败：{reason or status or '未说明'}")
    attempted = cls != "skipped"
    return {
        "symbol": symbol,
        "success": cls == "success",
        "executed": attempted,
        **({"skipped_reason": reason or "skipped"} if not attempted else {}),
        "order_id": payload.get("order_id"),
        "message": message,
        "duplicate": cls == "duplicate",
        # 无模拟腿，因此没有镜像回执；真单结论在下方的 real_direct 里
        "mirror": None,
        "exec_path": "real_direct",
        "real_direct": {
            "status": status,
            "class": cls,
            "reason": reason,
            "order_id": payload.get("order_id"),
            "client_order_id": payload.get("client_order_id"),
            "limit_price": limit_price,
            "order_value": payload.get("order_value"),
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
