"""QMT 止盈/止损执行器控制面。

面向运维与「模拟交易设置」页：

* ``GET  /qmt-sltp/config``  —— 执行器配置 + 各规则当日状态
* ``PUT  /qmt-sltp/config``  —— 整体替换配置（规则表、开关、保护价模式、告警阈值）
* ``PUT  /qmt-sltp/enabled`` —— 快速开关（不动规则表）
* ``GET  /qmt-sltp/status``  —— 仅状态（触发/委托/成交进度）
* ``POST /qmt-sltp/reset``   —— 重新武装（全部或指定标的）

**鉴权口径**：止盈止损执行器触发即下真单（保护价报单），是真金白银的控制面，
全部端点要求管理员（``require_admin``）。所有 Redis 键读写封装在
``sltp_executor`` 内，本路由只做参数校验、鉴权与审计。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from backend.services.live_trading.services import sltp_executor as executor
from backend.services.trade_shared.deps import AuthContext, get_redis, require_admin

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_RULES = 50
VALID_MODES = ("limit_floor", "market")


class SltpRule(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=32, description="标的（600036.SH）")
    enabled: bool = Field(True, description="该规则是否启用")
    side: str = Field("SELL", description="方向（当前仅支持 SELL）")
    entry_price: float | None = Field(None, gt=0, description="成本价；缺省取柜台持仓成本")
    quantity: float | None = Field(None, gt=0, description="数量；缺省取柜台可用全量")
    stop_loss_pct: float | None = Field(None, gt=0, lt=1, description="止损比例 0.05=5%")
    take_profit_pct: float | None = Field(None, gt=0, lt=10, description="止盈比例 0.10=10%")
    trailing_stop_pct: float | None = Field(None, gt=0, lt=1, description="移动止损回撤比例")

    @field_validator("symbol")
    @classmethod
    def _clean_symbol(cls, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("symbol 不能为空")
        return text

    @field_validator("side")
    @classmethod
    def _clean_side(cls, value: str) -> str:
        text = str(value or "SELL").strip().upper()
        if text != "SELL":
            # 执行器是清仓语义（触发即卖出）；买入会让「越止越买」，建仓走策略链路
            raise ValueError("side 仅支持 SELL（止盈止损执行器只卖不买）")
        return text


class SltpConfigUpdate(BaseModel):
    enabled: bool = Field(False, description="执行器总开关（生产默认关闭）")
    user_id: str = Field("1", description="真账户用户 id（与镜像白名单口径一致）")
    tenant_id: str = Field("default", min_length=1, max_length=32)
    poll_interval_sec: float = Field(3, ge=1, le=60, description="轮询间隔（秒）")
    protect_price_mode: str = Field("limit_floor", description="limit_floor=跌停价保护 / market=市价")
    pending_alert_sec: float = Field(
        300, ge=0, le=3600, description="未成交告警阈值（秒，0=立即；同时驱动余量策略）"
    )
    remainder_policy: str = Field(
        "alert_only",
        description="未成交余量策略：alert_only=仅提醒 / cancel=撤单 / requote_at_protect_price=偏离保护价才重挂",
    )
    close_reminder_sec: float = Field(300, ge=0, le=3600, description="收盘前提醒窗口（秒，0=关闭）")
    rules: list[SltpRule] = Field(default_factory=list, max_length=MAX_RULES)

    @field_validator("protect_price_mode")
    @classmethod
    def _clean_mode(cls, value: str) -> str:
        text = str(value or "").strip().lower()
        if text not in VALID_MODES:
            raise ValueError(f"protect_price_mode 仅支持 {VALID_MODES}")
        return text

    @field_validator("remainder_policy")
    @classmethod
    def _clean_policy(cls, value: str) -> str:
        text = str(value or "alert_only").strip().lower()
        if text not in executor.VALID_REMAINDER_POLICIES:
            raise ValueError(f"remainder_policy 仅支持 {executor.VALID_REMAINDER_POLICIES}")
        return text


class SltpEnabledUpdate(BaseModel):
    enabled: bool


class SltpResetRequest(BaseModel):
    symbols: list[str] | None = Field(None, description="留空=全部重新武装")


def _read(redis: Any) -> dict[str, Any]:
    try:
        return {
            "config": executor.load_config(redis),
            "state": executor.load_state(redis),
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Redis 读取失败: {exc}") from exc


@router.get("/qmt-sltp/config")
async def get_sltp_config(redis: Any = Depends(get_redis), auth: AuthContext = Depends(require_admin)):
    """执行器配置 + 规则当日状态。"""
    return _read(redis)


@router.get("/qmt-sltp/status")
async def get_sltp_status(redis: Any = Depends(get_redis), auth: AuthContext = Depends(require_admin)):
    """仅状态（配置里的 rules 不带回，减少敏感面）。"""
    snapshot = _read(redis)
    state = snapshot.get("state") or {}
    config = snapshot.get("config") or {}
    return {
        "enabled": config.get("enabled"),
        "protect_price_mode": config.get("protect_price_mode"),
        "date": state.get("date"),
        "rules": state.get("rules") or {},
    }


@router.put("/qmt-sltp/config")
async def put_sltp_config(
    payload: SltpConfigUpdate,
    redis: Any = Depends(get_redis),
    auth: AuthContext = Depends(require_admin),
):
    """整体替换配置。改规则后需 ``POST /reset`` 才会重新武装当日已触发的标的。"""
    data = payload.model_dump()
    try:
        saved = executor.save_config(redis, data)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Redis 写入失败: {exc}") from exc
    logger.info(
        "[SltpAPI] config tenant=%s user=%s enabled=%s rules=%d mode=%s",
        auth.tenant_id,
        auth.user_id,
        saved.get("enabled"),
        len(saved.get("rules") or []),
        saved.get("protect_price_mode"),
    )
    return saved


@router.put("/qmt-sltp/enabled")
async def put_sltp_enabled(
    payload: SltpEnabledUpdate,
    redis: Any = Depends(get_redis),
    auth: AuthContext = Depends(require_admin),
):
    """快速开关（保留规则表）。

    走 ``executor.set_enabled``：读失败直接 503，绝不把「读不到配置」当空配置
    写回去（否则 Redis 抖动时这一按会把规则表抹掉）。
    """
    try:
        saved = executor.set_enabled(redis, payload.enabled)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Redis 读取/写入失败: {exc}") from exc
    logger.info("[SltpAPI] enabled tenant=%s user=%s → %s", auth.tenant_id, auth.user_id, saved["enabled"])
    return {"enabled": saved["enabled"]}


@router.post("/qmt-sltp/reset")
async def post_sltp_reset(
    payload: SltpResetRequest | None = None,
    redis: Any = Depends(get_redis),
    auth: AuthContext = Depends(require_admin),
):
    """重新武装（当日已触发/已成交的规则恢复 armed，可再次触发）。"""
    symbols = (payload.symbols if payload else None) or None
    try:
        state = executor.reset_rules(redis, symbols)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Redis 写入失败: {exc}") from exc
    logger.info(
        "[SltpAPI] reset tenant=%s user=%s symbols=%s", auth.tenant_id, auth.user_id, symbols or "ALL"
    )
    return {"date": state.get("date"), "rules": state.get("rules") or {}}
