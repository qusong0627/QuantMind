"""对外交易面 —— **只做模拟盘**。

为什么这一批只有模拟盘
----------------------
不是没写完，是**故意只开这一半**。

真实下单链路 `POST /api/v1/orders` 目前**没有任何幂等键**（实测：重复的
`client_order_id` 会撞唯一索引 → `IntegrityError` → 500）。一份对外 API 把
「重试一下就多下一单 / 或者直接 500」的端点暴露给一个跨公网、按自身节奏重试的
节点，等于交付一个已知的双下单与资金错账面。模拟盘有幂等（`sim_orders.client_order_id`
的部分唯一索引），实盘没有——所以这一批先开模拟盘，实盘等幂等补齐后**单独一批**。

另一层保险在上游：`simulation.models.order.TradingMode` 只定义了 `SIMULATION`
一个值，`/api/v1/simulation/orders` 收不到别的模式。所以这条路上**物理上**不可能
落到实盘撮合——本模块不依赖这一点（它有自己的 `Literal["simulation"]`），
但它是同一个方向的第二道。

## 路径为什么是 `/trading/sim/…` 而不是 `/trading/…`

为了**将来能把实盘侧按前缀关掉而不误伤模拟盘**。`live_trading_gate` 的
`_BLOCKED_EXT_PREFIXES` 是前缀语义：若模拟盘端点长在 `/trading/orders`，
将来把「实盘关闭的部署上拦住对外交易面」写成 `_BLOCKED_EXT_PREFIXES =
("/api/ext/v1/trading",)`，就会**连带打死模拟盘**——而 OSS 默认
`ENABLE_REAL_TRADING=false`，那是**每一个** OSS 部署。
`test_external_api_gate_coverage.py::test_blocked_prefix_never_covers_simulation`
把这条钉死。

## 幂等：判定「是不是重放」请比 `order_id`，不要比状态码

幂等**不是本层实现的**，是上游 `sim_orders.client_order_id` 的部分唯一索引。
同键重放，上游返回**已有的那张单**，状态码同样是 201。所以：

* 客户端要带幂等键（`Idempotency-Key` 头，或报文里的 `client_order_id`），**必需**。
* 超时/断线后**原样重试**是安全的——重试拿到的是同一个 `order_id`。
* **不要**用「201 还是 409」判断重放：两次都是 201，靠 `order_id` 相等判定。

对外契约比上游**更严**：`SimOrderCreate.client_order_id` 在上游是可空的
（历史前端调用不带），这里**必需**。对外来的机器请求没有「人到界面上看一眼」
的兜底，缺幂等键的重试就只能是重复下单。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_serializer

from backend.services.api.routers.external import permissions as perms
from backend.services.api.routers.external.auth import ExternalPrincipal
from backend.services.api.routers.external.upstream import (
    WRITE_TIMEOUT_SECONDS,
    fetch_json,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["External API · Trading"])

#: 上游交易的挂载前缀。与 `trade/main.py` 的 `include_router(..., prefix=...)` 一致。
_UPSTREAM = "/api/v1/simulation"

#: 模拟账户市场。**枚举出来**而不是放任自由字符串：上游只是 `market.upper()`，
#: 拼错的 `market=CHINA` 不会报错，只会造出一个空的、永远没数据的账户——
#: 外部节点会以为「账户是空的」而不是「我参数写错了」。
Market = Literal["CN", "HK", "US", "FUTURES", "CRYPTO"]

#: 幂等键最大长度，与上游 `client_order_id` 的 `max_length=64` 对齐。
_IDEMPOTENCY_KEY_MAX = 64


def _serialize_dt(value: datetime | None) -> str | None:
    """瞬时列一律带 `Z` 输出（见 CLAUDE.md「瞬时时间」）。

    不这么写，pydantic 会输出 `+00:00` 偏移——同一条链路上游发 `Z`、这里发
    `+00:00`，两处都是「UTC」，但客户端做字符串比对时会判成不同。
    """
    from backend.shared.utc_datetime import to_utc_iso

    return to_utc_iso(value)


# ---------------------------------------------------------------------------
# 报文模型
# ---------------------------------------------------------------------------


class SimPosition(BaseModel):
    """模拟持仓。字段与上游 Redis 账户哈希里的持仓块逐一对应。"""

    model_config = ConfigDict(extra="ignore")

    volume: float = Field(0.0, description="持仓数量")
    available_volume: float = Field(0.0, description="可卖数量（A 股 T+1 当日买入不计入）")
    cost: float = Field(0.0, description="摊薄成本价")
    market_value: float = Field(0.0, description="市值（无行情时按成本价估）")
    price: float = Field(0.0, description="最新价（同上）")


class SimAccountResponse(BaseModel):
    """模拟账户快照。**已展开上游的 `{success,data}` 信封**，这里就是 data。

    ⚠️ **三个必填字段是契约，其余是可空观测值。** 上游 `/simulation/account`
    返回的是一个**裸 dict**（没有 `response_model`），所以这里是有意设成
    `extra="ignore"`：上游 dict 里还有十几个我们没有承诺的键
    （`liabilities` / `maintenance_margin_ratio` / `baseline` / …），
    `forbid` 会把它们当错误。

    代价是**字段改名不会报错**，只会让那个字段恒为 null。所以分层是刻意的：
    `cash` / `total_asset` / `market_value` 是**必填**——上游一改名，这里立刻
    `ValidationError` → 502 `upstream_contract_changed`；其余可空字段改名则降级成
    null。**null 不等于 0**（本仓在市值零值上踩过：把「取不到」显示成 0，
    下游会当成「账户确实亏光了」）。
    """

    model_config = ConfigDict(extra="ignore")

    market: Market
    cash: float
    total_asset: float
    market_value: float
    positions: dict[str, SimPosition] = Field(
        default_factory=dict,
        description="键为标的（多空腿带 `::` 后缀），值为持仓",
    )
    position_count: int | None = Field(
        None, description="volume > 0 的持仓数（上游口径），0 是**真值**不是缺失"
    )
    initial_equity: float | None = Field(
        None, description="账户种子资金。上游未初始化路径不返回此项 → null。"
    )
    total_pnl: float | None = Field(None, description="总盈亏 = 总资产 − 种子资金")
    today_pnl: float | None = Field(None, description="今日盈亏（对日初权益）")
    monthly_pnl: float | None = Field(None, description="本月盈亏（对月初权益）")
    account_not_initialized: bool = Field(
        False,
        description=(
            "true = 该市场账户尚未创建。**这不是错误**：上游刻意不自动建账户"
            "（避免覆盖手动任务后的持仓），此时各金额为 0 且无持仓。"
        ),
    )


class SimOrderRequest(BaseModel):
    """对外下单报文。**比上游更严**：见模块 docstring 的幂等段落。"""

    # extra="forbid"：拼错字段名宁可 422，也不要静默忽略。
    # 上游 `SimOrderCreate` 是 `extra="ignore"`——那是为兼容老前端（透传字段
    # 一路加进来），对外面没有这个包袱，静默忽略一个拼错的 `quantitiy` 会变成
    # 「下单成功但数量是默认值」这类事故。
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(..., min_length=1, max_length=20, description="标的代码")
    side: Literal["buy", "sell"]
    order_type: Literal["market", "limit"]
    quantity: float = Field(..., gt=0)
    price: float | None = Field(
        None, gt=0, description="限价单必填；市价单必须为空或省略"
    )
    remarks: str | None = Field(None, max_length=500)
    strategy_id: int | None = Field(None, gt=0)
    #: 幂等键。可以不写在报文里而放在 `Idempotency-Key` 头里（两者都给时须一致）。
    client_order_id: str | None = Field(None, max_length=_IDEMPOTENCY_KEY_MAX)


class SimOrderResponse(BaseModel):
    """模拟委托。字段与上游 `SimOrderResponse` 对齐（那是**上游契约**，不是我们的）。"""

    model_config = ConfigDict(extra="ignore")

    order_id: UUID
    symbol: str
    symbol_name: str | None = None
    side: Literal["buy", "sell"]
    order_type: Literal["market", "limit"]
    quantity: float
    price: float | None
    status: Literal["pending", "submitted", "filled", "cancelled", "rejected"]
    filled_quantity: float
    average_price: float | None
    order_value: float
    filled_value: float
    commission: float
    client_order_id: str | None = Field(
        None,
        description=(
            "回显上游持久化的幂等键。⚠️ 历史单可能为 null（早期写入路径不落此列）"
            "——**null 不代表这张单没有幂等键**，只代表上游没存。"
        ),
    )
    remarks: str | None = None
    strategy_id: int | None = None
    portfolio_id: int = 0
    submitted_at: datetime | None = None
    filled_at: datetime | None = None
    cancelled_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @field_serializer(
        "submitted_at", "filled_at", "cancelled_at", "created_at", "updated_at",
        when_used="json",
    )
    def _dt(self, value: datetime | None) -> str | None:
        return _serialize_dt(value)


class SimTradeResponse(BaseModel):
    """模拟成交回报。"""

    model_config = ConfigDict(extra="ignore")

    trade_id: UUID
    order_id: UUID
    symbol: str
    symbol_name: str | None = None
    side: Literal["buy", "sell"]
    quantity: float
    price: float
    trade_value: float
    commission: float
    executed_at: datetime
    price_source: str | None = None

    @field_serializer("executed_at", when_used="json")
    def _dt(self, value: datetime | None) -> str | None:
        return _serialize_dt(value)


class CancelOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(None, max_length=200, description="撤单原因（留痕用）")


# ---------------------------------------------------------------------------
# 幂等键解析
# ---------------------------------------------------------------------------


def resolve_idempotency_key(
    header_value: str | None, body_value: str | None
) -> str | None:
    """把「头里的键」与「报文里的键」收敛成一个。冲突 → 400。

    两者都给且不等，是**调用方自己矛盾**：放行的话，它下次重试可能只带其中一个，
    于是同一笔意图拿到两个键、下出两张单。宁可 400 让它在写代码时就发现。

    都不给 → 返回 None，由端点决定要不要拒（本面上是拒）。
    """
    head = (header_value or "").strip()
    body = (body_value or "").strip()
    if head and body and head != body:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="idempotency_key_conflict",
        )
    return head or body or None


# ---------------------------------------------------------------------------
# 账户与持仓
# ---------------------------------------------------------------------------


@router.get("/sim/account", response_model=SimAccountResponse)
async def get_sim_account(
    market: Market = Query("CN", description="模拟账户市场"),
    principal: ExternalPrincipal = Depends(perms.require_permission(perms.PERMISSION_TRADE_READ)),
) -> SimAccountResponse:
    """模拟账户快照（含持仓）。

    `account_not_initialized=true` 时各金额为 0 且 `positions` 为空——那是
    「这个市场的账户还没建」，外部节点应当去建/重置，而不是当成「资产归零」。
    """
    raw: dict[str, Any] = await fetch_json(
        "trade",
        "GET",
        f"{_UPSTREAM}/account",
        principal=principal,
        params={"market": market},
    ) or {}
    return _unwrap_account(raw, market)


def _unwrap_account(raw: dict[str, Any], market: Market) -> SimAccountResponse:
    """展开上游的信封，并**盖回请求里的 market**。

    上游两条返回路径的信封形状**不一样**（实测）：

    * 已初始化：`{"success": true, "data": {..., "market": "CN"}}` —— market 在 data 里
    * 未初始化：`{"success": true, "data": {cash,total_asset,market_value,positions,
      account_not_initialized}, "market": "CN"}` —— market 在**顶层**，data 里没有

    所以 market 不看上游给哪一份，直接用请求里那个（它已是 `Market` 字面量，
    是规范形态）。这样上游哪天调整信封层级，本端点不会跟着错。

    `data` 缺失 = 上游形状变了，硬失败成 502。**不**退化成「空账户」——
    把一次契约漂移显示成「你账户里没钱」是最坏的一种错。
    """
    data = raw.get("data")
    if not isinstance(data, dict):
        logger.error("[ExtTrading] 账户响应缺少 data 信封：keys=%s", sorted(raw)[:10])
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="upstream_contract_changed"
        )
    payload = dict(data)
    payload["market"] = market
    try:
        return SimAccountResponse.model_validate(payload)
    except ValidationError as exc:
        logger.error("[ExtTrading] 账户响应与契约不符：%s", exc.errors()[:5])
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="upstream_contract_changed"
        ) from exc


# ---------------------------------------------------------------------------
# 委托
# ---------------------------------------------------------------------------


@router.get("/sim/orders", response_model=list[SimOrderResponse])
async def list_sim_orders(
    status_filter: str | None = Query(
        None,
        alias="status",
        description="pending/submitted/filled/cancelled/rejected",
    ),
    symbol: str | None = Query(None),
    start_date: datetime | None = Query(None, description="按下单时间过滤（闭区间）"),
    end_date: datetime | None = Query(None),
    limit: int = Query(50, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    principal: ExternalPrincipal = Depends(perms.require_permission(perms.PERMISSION_TRADE_READ)),
) -> list[SimOrderResponse]:
    """委托列表，按上游默认排序（`created_at` 倒序）。

    分页是 `limit`/`offset`——**不是**数据面那种游标。理由：委托表由 `user_id`
    收窄且量级小（单个模拟账户），而游标分页在「新单不断插入」时会漏行
    （offset 分页在两轮之间插入新行会重复，但委托是只追加且能被 `order_id` 去重）。
    """
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if status_filter:
        params["status"] = status_filter
    if symbol:
        params["symbol"] = symbol
    if start_date:
        params["start_date"] = start_date.isoformat()
    if end_date:
        params["end_date"] = end_date.isoformat()

    return await fetch_json(
        "trade",
        "GET",
        f"{_UPSTREAM}/orders",
        principal=principal,
        list_model=SimOrderResponse,
        params=params,
    )


@router.get("/sim/orders/{order_id}", response_model=SimOrderResponse)
async def get_sim_order(
    order_id: UUID = Path(..., description="委托的 order_id（UUID，不是自增 id）"),
    principal: ExternalPrincipal = Depends(perms.require_permission(perms.PERMISSION_TRADE_READ)),
) -> SimOrderResponse:
    """单张委托。

    ⚠️ 路径参数是 **UUID**（`sim_orders.order_id`），不是列表里那个自增 `id`。
    上游两者都有，用自增 id 拼 URL 会 422——本模块刻意**不**暴露自增 id，
    它是跨租户唯一的实现细节，不该出现在对外契约里。
    """
    return await fetch_json(
        "trade",
        "GET",
        f"{_UPSTREAM}/orders/{order_id}",
        principal=principal,
        model=SimOrderResponse,
    )


@router.post(
    "/sim/orders",
    response_model=SimOrderResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_sim_order(
    payload: SimOrderRequest,
    idempotency_key: str | None = Header(
        None,
        alias="Idempotency-Key",
        max_length=_IDEMPOTENCY_KEY_MAX,
        description="幂等键。也可写在报文 client_order_id 里；两者都给时须一致。",
    ),
    principal: ExternalPrincipal = Depends(perms.require_permission(perms.PERMISSION_TRADE_WRITE)),
) -> SimOrderResponse:
    """提交模拟委托。

    **幂等键必需**。缺了返回 400 `idempotency_key_required`——这不是格式挑剔：
    没有它，一次超时重试就是一张重复的成交（见模块 docstring）。

    响应 201 与「是否新单」无关：同键重放返回**同一张单**、同样是 201。
    判定请比 `order_id`。
    """
    key = resolve_idempotency_key(idempotency_key, payload.client_order_id)
    if not key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="idempotency_key_required",
        )

    body: dict[str, Any] = {
        "symbol": payload.symbol,
        "side": payload.side,
        "order_type": payload.order_type,
        "quantity": payload.quantity,
        "client_order_id": key,
        # 交易模式**由服务端钉死**，不接受调用方指定（报文模型里也没有这个字段）。
        # 这样「走对外接口下实盘单」不是「被检查后拒绝」，而是**表达不出来**。
        "trading_mode": "SIMULATION",
    }
    if payload.price is not None:
        body["price"] = payload.price
    if payload.remarks is not None:
        body["remarks"] = payload.remarks
    if payload.strategy_id is not None:
        body["strategy_id"] = payload.strategy_id

    return await fetch_json(
        "trade",
        "POST",
        f"{_UPSTREAM}/orders",
        principal=principal,
        model=SimOrderResponse,
        json_body=body,
        # 下单走更长的超时：上游要拿账户撮合锁，持锁排队时 10s 会假超时，
        # 而这里的「超时」对调用方意味着「不知道成没成」——要靠幂等重试兜底。
        timeout=WRITE_TIMEOUT_SECONDS,
    )


@router.post("/sim/orders/{order_id}/cancel", response_model=SimOrderResponse)
async def cancel_sim_order(
    payload: CancelOrderRequest,
    order_id: UUID = Path(...),
    principal: ExternalPrincipal = Depends(perms.require_permission(perms.PERMISSION_TRADE_WRITE)),
) -> SimOrderResponse:
    """撤单。

    可撤的是 `pending`（挂单）与 `submitted` 两种状态；已成/已撤的单上游回 400，
    detail 原样透传（它是给调用方看的原因，不是内部实现细节）。
    """
    return await fetch_json(
        "trade",
        "POST",
        f"{_UPSTREAM}/orders/{order_id}/cancel",
        principal=principal,
        model=SimOrderResponse,
        json_body={"reason": payload.reason},
        timeout=WRITE_TIMEOUT_SECONDS,
    )


# ---------------------------------------------------------------------------
# 成交回报
# ---------------------------------------------------------------------------


@router.get("/sim/trades", response_model=list[SimTradeResponse])
async def list_sim_trades(
    symbol: str | None = Query(None),
    start_date: datetime | None = Query(None),
    end_date: datetime | None = Query(None),
    limit: int = Query(50, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    principal: ExternalPrincipal = Depends(perms.require_permission(perms.PERMISSION_TRADE_READ)),
) -> list[SimTradeResponse]:
    """成交回报列表。

    **这是对账用的**：委托是意图，成交才是事实。持仓与现金的每一次变化都对应
    这里的至少一条记录（手续费也在 `commission` 里），外部节点重建本地台账时
    应当以本端点为准，而不是拿委托列表推算。
    """
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if symbol:
        params["symbol"] = symbol
    if start_date:
        params["start_date"] = start_date.isoformat()
    if end_date:
        params["end_date"] = end_date.isoformat()

    return await fetch_json(
        "trade",
        "GET",
        f"{_UPSTREAM}/trades",
        principal=principal,
        list_model=SimTradeResponse,
        params=params,
    )


__all__ = [
    "router",
    "CancelOrderRequest",
    "SimAccountResponse",
    "SimOrderRequest",
    "SimOrderResponse",
    "SimPosition",
    "SimTradeResponse",
    "resolve_idempotency_key",
]
