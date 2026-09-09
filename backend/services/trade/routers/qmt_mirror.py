"""大 QMT 真单镜像控制与对账路由。

面向「模拟交易设置 → 大 QMT 镜像」卡片与运维：

* ``GET  /qmt-mirror/status``    —— 开关/急停/白黑名单/限额/当日用量/队列/阻塞原因
* ``PUT  /qmt-mirror/enabled``   —— Redis 热开关（env 关时也能开，但通道未就绪仍不下单）
* ``PUT  /qmt-mirror/kill``      —— 急停（优先级最高，置位后立即停单）
* ``PUT  /qmt-mirror/config``    —— 限额参数（覆盖 env 基线）
* ``PUT  /qmt-mirror/lists``     —— 白名单 / 黑名单整体替换
* ``POST /qmt-mirror/drain``     —— 手动排空非交易时段队列
* ``GET  /qmt-mirror/reconcile`` —— 虚拟账本 vs 真单对账（滑点/费用/部分成交/拒单）

每次写操作都记审计日志（用户/租户/前后值）。所有 Redis 键的读写都封装在
``real_mirror_service`` 内，本路由只做参数校验、鉴权与审计。

**鉴权口径**：本路由是真金白银的控制面（开关/急停/限额/名单/补交），全部端点
要求管理员（``require_admin``），不是「登录即可」。读接口同样收口：状态与对账
含白名单、当日用量与真单明细，普通用户无权查看。
"""

from __future__ import annotations

import logging
from datetime import date as date_type
from datetime import datetime, time, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from backend.services.live_trading.services import real_mirror_service as mirror
from backend.services.live_trading.services.trading_session import TZ
from backend.services.trade_shared.deps import (
    AuthContext,
    get_db,
    get_redis,
    require_admin,
)

logger = logging.getLogger(__name__)
router = APIRouter()

SIM_REMARK_PREFIX = "client_order_id="
RECONCILE_LIMIT_MAX = 2000

# 限额上限：防手滑把「单笔 1 万」改成天文数字后一路下单（下限由 gt=0 保证）
MAX_ORDER_VALUE_LIMIT = 10_000_000.0  # 单笔上限 ≤1000 万
MAX_DAILY_VALUE_LIMIT = 100_000_000.0  # 单日累计上限 ≤1 亿
MAX_LIST_ITEMS = 500  # 白/黑名单最多条数
MAX_LIST_ITEM_LEN = 64  # 单条最长字符（tenant:user:strategy / SH600519）


# --------------------------------------------------------------------------
# 请求体
# --------------------------------------------------------------------------
class MirrorEnabledUpdate(BaseModel):
    enabled: bool = Field(..., description="是否开启镜像（Redis 热开关）")


class MirrorKillUpdate(BaseModel):
    on: bool = Field(..., description="true=急停，false=解除急停")


class MirrorConfigUpdate(BaseModel):
    max_order_value: float | None = Field(
        None, gt=0, le=MAX_ORDER_VALUE_LIMIT, description="单笔上限（元）"
    )
    max_daily_value: float | None = Field(
        None, gt=0, le=MAX_DAILY_VALUE_LIMIT, description="单日累计上限（元）"
    )
    max_daily_symbols: int | None = Field(
        None, gt=0, le=200, description="单日最多标的数"
    )
    max_daily_orders: int | None = Field(
        None, gt=0, le=1000, description="单日最多笔数"
    )
    max_slippage_pct: float | None = Field(
        None, gt=0, le=0.2, description="限价滑点（0.02=±2%）"
    )
    max_consecutive_rejects: int | None = Field(
        None, ge=0, le=100, description="连续拒单熔断阈值，0=关闭熔断"
    )
    queue_outside_hours: bool | None = Field(None, description="非交易时段是否入队")
    markets: list[str] | None = Field(
        None, max_length=20, description="允许镜像的市场，如 ['CN']"
    )


class MirrorListsUpdate(BaseModel):
    whitelist: list[str] | None = Field(
        None,
        max_length=MAX_LIST_ITEMS,
        description="白名单：* / tenant / tenant:user / tenant:user:strategy；空数组=全部关闭",
    )
    blacklist: list[str] | None = Field(
        None,
        max_length=MAX_LIST_ITEMS,
        description="黑名单标的（前缀式，如 SH600519）",
    )

    @field_validator("whitelist", "blacklist")
    @classmethod
    def _clean_items(cls, value: list[str] | None) -> list[str] | None:
        """去空白 + 限长：名单项会被拼进 Redis 键与日志，超长/空白项只会帮倒忙。"""
        if value is None:
            return None
        cleaned: list[str] = []
        for item in value:
            text = str(item or "").strip()
            if not text:
                continue
            if len(text) > MAX_LIST_ITEM_LEN:
                raise ValueError(f"名单项过长（≤{MAX_LIST_ITEM_LEN} 字符）: {text[:24]}…")
            cleaned.append(text)
        return cleaned


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def _audit(action: str, auth: AuthContext, **detail: Any) -> None:
    logger.info(
        "[MirrorAPI] %s tenant=%s user=%s %s",
        action,
        auth.tenant_id,
        auth.user_id,
        " ".join(f"{k}={v!r}" for k, v in detail.items()),
    )


def _snapshot(redis: Any) -> dict[str, Any]:
    try:
        return mirror.status_snapshot(redis)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Redis 读取失败: {exc}") from exc


def _write(action: str, fn: Any, auth: AuthContext, **detail: Any) -> Any:
    try:
        result = fn()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Redis 写入失败: {exc}") from exc
    _audit(action, auth, **detail)
    return result


# --------------------------------------------------------------------------
# 状态 / 开关
# --------------------------------------------------------------------------
@router.get("/qmt-mirror/status")
async def get_mirror_status(
    redis: Any = Depends(get_redis),
    auth: AuthContext = Depends(require_admin),
):
    """镜像总览：开关、急停、白黑名单、限额、当日用量、队列与阻塞原因。"""
    return _snapshot(redis)


@router.put("/qmt-mirror/enabled")
async def set_mirror_enabled(
    payload: MirrorEnabledUpdate,
    redis: Any = Depends(get_redis),
    auth: AuthContext = Depends(require_admin),
):
    """Redis 热开关（env 基线之外的运行时开关）。"""
    _write(
        "enabled",
        lambda: mirror.set_enabled(redis, payload.enabled),
        auth,
        enabled=payload.enabled,
    )
    return _snapshot(redis)


@router.put("/qmt-mirror/kill")
async def set_mirror_kill(
    payload: MirrorKillUpdate,
    redis: Any = Depends(get_redis),
    auth: AuthContext = Depends(require_admin),
):
    """急停开关。置位后所有镜像单立即停发（虚拟账本不受影响）。"""
    _write(
        "kill",
        lambda: mirror.set_kill_switch(redis, payload.on),
        auth,
        on=payload.on,
    )
    return _snapshot(redis)


@router.put("/qmt-mirror/config")
async def update_mirror_config(
    payload: MirrorConfigUpdate,
    redis: Any = Depends(get_redis),
    auth: AuthContext = Depends(require_admin),
):
    """限额参数覆盖（写入 ``mirror:config``，只覆盖显式给出的字段）。"""
    updates = payload.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="未提供任何配置项")
    if "markets" in updates:
        updates["markets"] = [str(m).upper() for m in updates["markets"]]
    before = mirror.read_config_overrides(redis)
    merged = _write(
        "config",
        lambda: mirror.write_config_overrides(redis, updates),
        auth,
        before=before,
        updates=updates,
    )
    payload_out = _snapshot(redis)
    payload_out["config_overrides"] = merged
    return payload_out


@router.put("/qmt-mirror/lists")
async def update_mirror_lists(
    payload: MirrorListsUpdate,
    redis: Any = Depends(get_redis),
    auth: AuthContext = Depends(require_admin),
):
    """白名单/黑名单整体替换（``None`` 表示不动，``[]`` 表示清空）。"""
    if payload.whitelist is None and payload.blacklist is None:
        raise HTTPException(status_code=400, detail="未提供任何名单")
    _write(
        "lists",
        lambda: mirror.set_lists(
            redis, whitelist=payload.whitelist, blacklist=payload.blacklist
        ),
        auth,
        whitelist=payload.whitelist,
        blacklist=payload.blacklist,
    )
    return _snapshot(redis)


@router.post("/qmt-mirror/drain")
async def drain_mirror_queue_endpoint(
    limit: int = Query(20, ge=1, le=200, description="本次最多补交笔数"),
    redis: Any = Depends(get_redis),
    auth: AuthContext = Depends(require_admin),
):
    """手动排空镜像队列（交易时段内有效）。"""
    result = await mirror.drain_mirror_queue(redis, limit=limit)
    _audit("drain", auth, result=result)
    return result


# --------------------------------------------------------------------------
# 对账
# --------------------------------------------------------------------------
def _side_text(value: Any) -> str:
    return str(getattr(value, "value", value) or "").upper()


def _order_price(row: Any) -> float:
    if row is None:
        return 0.0
    for name in ("average_price", "filled_price", "price"):
        value = getattr(row, name, None)
        if value:
            return float(value)
    return 0.0


def _sim_created_window(target: date_type) -> tuple[datetime, datetime]:
    """虚拟单落库时间的「上海日」UTC 窗口。

    ``sim_orders.created_at`` 是 ``timestamptz``，但写入方给的是 naive 的
    ``datetime.utcnow()``（``simulation.models.TimestampMixin``）——asyncpg 按会话
    时区（Asia/Shanghai）解释 naive 值，库内瞬时比真实时刻**早 UTC+8 小时**
    （实测：同一笔单 ``sim_orders.created_at`` 比 ``sim_trades.executed_at`` 早 8h）。
    因此按上海交易日过滤时，窗口必须整体回移同样的偏移，否则永远查不到当天的单。
    """
    offset = datetime.now(TZ).utcoffset() or timedelta(0)
    start = datetime.combine(target, time.min, tzinfo=TZ).astimezone(timezone.utc)
    start -= offset
    return start, start + timedelta(days=1)


@router.get("/qmt-mirror/reconcile")
async def reconcile_mirror_orders(
    date: str | None = Query(
        None, description="交易日 YYYY-MM-DD（按虚拟成交落库时间过滤），缺省=今天"
    ),
    limit: int = Query(200, ge=1, le=RECONCILE_LIMIT_MAX),
    db: Any = Depends(get_db),
    auth: AuthContext = Depends(require_admin),
):
    """虚拟成交 vs 真单对账：价格滑点、手续费差、部分成交、拒单原因。"""
    try:
        target = (
            datetime.strptime(date, "%Y-%m-%d").date() if date else date_type.today()
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="date 需为 YYYY-MM-DD") from exc
    # sim_orders.created_at 的库内瞬时比真实时刻早 8h（见 _sim_created_window）
    start, end = _sim_created_window(target)

    from backend.services.simulation.models.order import SimOrder
    from backend.services.trade_shared.models.order import Order

    tenant = (auth.tenant_id or "default").strip() or "default"
    sim_stmt = (
        select(SimOrder)
        .where(
            SimOrder.tenant_id == tenant,
            SimOrder.remarks.like(f"{SIM_REMARK_PREFIX}%"),
            SimOrder.created_at >= start,
            SimOrder.created_at < end,
        )
        .order_by(SimOrder.created_at.desc())
        .limit(limit)
    )
    sim_rows = list((await db.execute(sim_stmt)).scalars().all())
    cids = {
        str(row.remarks or "")[len(SIM_REMARK_PREFIX) :].strip()
        for row in sim_rows
        if str(row.remarks or "").startswith(SIM_REMARK_PREFIX)
    }
    cids.discard("")
    real_by_cid: dict[str, Any] = {}
    if cids:
        real_stmt = select(Order).where(
            Order.tenant_id == tenant,
            # 用与下单同一条构造函数：镜像单号会截断到 orders.client_order_id 列宽，
            # 这里拼 f"mir-{cid}" 在超长 cid 时会对不上真单。
            Order.client_order_id.in_(
                [mirror.build_mirror_client_order_id(client_order_id=c) for c in cids]
            ),
        )
        for row in (await db.execute(real_stmt)).scalars().all():
            real_by_cid[str(row.client_order_id)] = row

    items: list[dict[str, Any]] = []
    matched = filled = rejected = 0
    slippage_sum = 0.0
    slippage_n = 0
    fee_diff_sum = 0.0
    for sim in sim_rows:
        remark = str(sim.remarks or "")
        cid = (
            remark[len(SIM_REMARK_PREFIX) :].strip()
            if remark.startswith(SIM_REMARK_PREFIX)
            else ""
        )
        real = real_by_cid.get(mirror.build_mirror_client_order_id(client_order_id=cid))
        sim_price = _order_price(sim)
        sim_fee = float(getattr(sim, "total_fee", 0) or 0)
        item: dict[str, Any] = {
            "client_order_id": cid,
            "symbol": str(sim.symbol or ""),
            "side": _side_text(sim.side),
            "quantity": float(sim.quantity or 0),
            "virtual_price": sim_price,
            "virtual_fee": sim_fee,
            "virtual_status": _side_text(sim.status),
            "mirrored": real is not None,
        }
        if real is None:
            item["note"] = "未找到真单（被风控跳过 / 镜像未开启 / 跨日）"
            items.append(item)
            continue
        matched += 1
        status = _side_text(real.status)
        real_price = _order_price(real)
        real_fee = float(getattr(real, "commission", 0) or 0)
        item.update(
            {
                "real_order_id": str(real.order_id),
                "real_exchange_order_id": str(real.exchange_order_id or ""),
                "real_price": real_price,
                "real_fee": real_fee,
                "real_status": status,
                "real_filled_quantity": float(real.filled_quantity or 0),
                "real_limit_price": float(real.price or 0),
            }
        )
        if real_price > 0 and sim_price > 0:
            diff = real_price - sim_price
            item["slippage"] = round(diff, 4)
            item["slippage_pct"] = round(diff / sim_price, 6)
            slippage_sum += abs(diff)
            slippage_n += 1
        item["fee_diff"] = round(real_fee - sim_fee, 4)
        fee_diff_sum += real_fee - sim_fee
        if status in {"FILLED", "PARTIALLY_FILLED"}:
            filled += 1
        if status in {"REJECTED", "CANCELLED", "EXPIRED"}:
            rejected += 1
            item["real_message"] = str(real.remarks or "")
        items.append(item)

    return {
        "date": target.isoformat(),
        "summary": {
            "virtual_orders": len(sim_rows),
            "mirrored": matched,
            "filled": filled,
            "rejected_or_cancelled": rejected,
            "avg_abs_slippage": round(slippage_sum / slippage_n, 4)
            if slippage_n
            else 0.0,
            "fee_diff_total": round(fee_diff_sum, 4),
        },
        "items": items,
    }
