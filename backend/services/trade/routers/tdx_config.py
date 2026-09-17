"""
TDX 桥配置管理路由

读取/更新通达信桥配置 (环境变量), 查询桥健康状态。
供前端"模拟交易设置 → 通达信桥"卡片使用。
"""
import asyncio
import logging
import os
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.services.trade_shared.deps import (
    AuthContext,
    get_auth_context,
    require_admin,
)
from backend.services.trade_shared.trade_config import settings

logger = logging.getLogger(__name__)
router = APIRouter()


class TdxConfigResponse(BaseModel):
    enabled: bool = Field(..., description="是否启用通达信推送")
    bridge_url: str = Field(..., description="桥地址")
    bridge_token_configured: bool = Field(..., description="token 是否已配置")
    real_trading_enabled: bool = Field(..., description="实盘是否启用")
    broker_type: str = Field(..., description="实盘 broker 类型")
    health: dict | None = Field(None, description="桥健康状态")


class TdxConfigUpdate(BaseModel):
    bridge_url: str | None = Field(None, description="桥地址")
    bridge_token: str | None = Field(None, description="桥 token")


class TdxPushSignalsRequest(BaseModel):
    run_id: str | None = Field(None, description="推理 run_id，为空取最新推理")
    top_n: int = Field(20, ge=1, le=100, description="推送股票数量")
    block_name: str = Field("QuantMind今日选股", description="通达信板块名")
    push_warnings: bool = Field(True, description="是否推送预警信号")
    push_message: bool = Field(True, description="是否推送界面消息")


class TdxRollingSignalsRequest(BaseModel):
    run_id: str | None = Field(None, description="推理 run_id，为空取最新推理")
    trade_date: str | None = Field(None, description="预测日期 YYYY-MM-DD，推送该日分数（推历史）")
    fixed_buy_amount: float | None = Field(None, gt=0, description="每只固定买入金额（元），空则用已保存配置")
    push_message: bool = Field(True, description="是否推送界面消息")
    check_index: bool = Field(True, description="是否按当日大盘 MA20 过滤（推历史时自动跳过）")


class TdxRollingConfigUpdate(BaseModel):
    score_threshold: float = Field(..., gt=0, le=10, description="买入分数阈值（>此分数买入）")
    fixed_buy_amount: float = Field(..., gt=0, description="每只固定买入金额（元）")
    execute_mode: Literal["off", "tdx", "paper"] | None = Field(
        None,
        description="执行模式: off=仅预警 / tdx=通达信下单(TQ收费账号直接提交免确认) / paper=模拟盘直接下单(免确认)",
    )
    auto_place: bool | None = Field(
        None, description="旧字段兼容: true=通达信下单(tdx) / false=仅预警(off)"
    )


RUNTIME_CONFIG_KEY = "trade:tdx_config:runtime"


def load_runtime_config() -> dict:
    """读取运行时桥配置（Redis 持久化，跨子进程 respawn 生效）。"""
    try:
        from backend.services.trade_shared.redis_client import get_redis

        saved = get_redis().get(RUNTIME_CONFIG_KEY)
        return saved if isinstance(saved, dict) else {}
    except Exception as exc:
        logger.warning("[TdxConfig] 读取运行时配置失败: %s", exc)
        return {}


def apply_runtime_config() -> None:
    """把 Redis 持久化的运行时桥配置应用到 settings/env/tdx_pusher。

    在 trade lifespan 启动时调用，使 PUT /tdx/config 的修改跨 respawn 生效。
    """
    saved = load_runtime_config()
    bridge_url = str(saved.get("bridge_url") or "").strip()
    bridge_token = str(saved.get("bridge_token") or "").strip()
    if not bridge_url and not bridge_token:
        return
    if bridge_url:
        settings.TDX_BRIDGE_URL = bridge_url
        os.environ["TDX_BRIDGE_URL"] = bridge_url
    if bridge_token:
        settings.TDX_BRIDGE_TOKEN = bridge_token
        os.environ["TDX_BRIDGE_TOKEN"] = bridge_token
    from backend.services.live_trading.services.tdx_push_service import tdx_pusher

    tdx_pusher.bridge_url = str(getattr(settings, "TDX_BRIDGE_URL", "") or "").strip()
    tdx_pusher.bridge_token = str(getattr(settings, "TDX_BRIDGE_TOKEN", "") or "").strip()
    logger.info(
        "[TdxConfig] 已应用运行时桥配置: url=%s token=%s",
        tdx_pusher.bridge_url, "configured" if tdx_pusher.bridge_token else "missing",
    )


@router.get("/tdx/config", response_model=TdxConfigResponse)
async def get_tdx_config(auth: AuthContext = Depends(get_auth_context)):
    """读取通达信桥配置状态 (不返回 token 明文)."""
    bridge_url = str(getattr(settings, "TDX_BRIDGE_URL", "") or "").strip()
    bridge_token = str(getattr(settings, "TDX_BRIDGE_TOKEN", "") or "").strip()
    enable_push = os.getenv("ENABLE_TDX_PUSH", "").strip().lower() == "true"
    enable_real = str(getattr(settings, "ENABLE_REAL_TRADING", "false")).lower() == "true"

    health = None
    if bridge_url and bridge_token:
        try:
            import httpx
            resp = httpx.get(f"{bridge_url}/api/v1/health", timeout=3)
            if resp.status_code == 200:
                health = resp.json()
            else:
                health = {"error": f"HTTP {resp.status_code}"}
        except Exception as e:
            health = {"error": str(e)}

    return TdxConfigResponse(
        enabled=enable_push,
        bridge_url=bridge_url,
        bridge_token_configured=bool(bridge_token),
        real_trading_enabled=enable_real,
        broker_type=str(getattr(settings, "REAL_BROKER_TYPE", "tdx")),
        health=health,
    )


@router.post("/tdx/config")
async def update_tdx_config(
    data: TdxConfigUpdate,
    auth: AuthContext = Depends(require_admin),  # H4 加固：全局桥地址/token 属管理员面（SSRF/凭证外带防线）
):
    """更新通达信桥配置 (运行时, 进程内生效)."""
    # 运行时覆盖 pydantic-settings (进程内生效)
    if data.bridge_url is not None:
        settings.TDX_BRIDGE_URL = str(data.bridge_url).strip()
        os.environ["TDX_BRIDGE_URL"] = str(data.bridge_url).strip()
    if data.bridge_token is not None:
        settings.TDX_BRIDGE_TOKEN = str(data.bridge_token).strip()
        os.environ["TDX_BRIDGE_TOKEN"] = str(data.bridge_token).strip()
    if data.bridge_url is not None or data.bridge_token is not None:
        from backend.services.live_trading.services.tdx_push_service import tdx_pusher

        tdx_pusher.bridge_url = str(getattr(settings, "TDX_BRIDGE_URL", "")).strip()
        tdx_pusher.bridge_token = str(getattr(settings, "TDX_BRIDGE_TOKEN", "")).strip()
        # 持久化到 Redis，跨子进程 respawn 生效
        try:
            from backend.services.trade_shared.redis_client import get_redis

            get_redis().set(
                RUNTIME_CONFIG_KEY,
                {
                    "bridge_url": tdx_pusher.bridge_url,
                    "bridge_token": tdx_pusher.bridge_token,
                },
            )
        except Exception as exc:
            logger.warning("[TdxConfig] 运行时配置持久化失败: %s", exc)

    return {"success": True, "message": "通达信桥配置已更新"}


@router.get("/tdx/overview")
async def get_tdx_overview(auth: AuthContext = Depends(get_auth_context)):
    """聚合通达信桥的局域网信息，供前端设置页展示。

    返回: 桥基本信息(stats) + 账户资产 + 持仓 + 当日委托 + 缓存/安全状态。
    桥不可达时返回 available=false 与错误信息，不阻断前端渲染。
    """
    from backend.services.live_trading.services.tdx_push_service import tdx_pusher

    bridge_url = str(getattr(settings, "TDX_BRIDGE_URL", "") or "").strip()
    bridge_token = str(getattr(settings, "TDX_BRIDGE_TOKEN", "") or "").strip()
    if not bridge_url or not bridge_token:
        return {"available": False, "error": "桥地址或 token 未配置"}

    try:
        import httpx

        headers = {"Authorization": f"Bearer {bridge_token}"}
        async with httpx.AsyncClient(timeout=httpx.Timeout(2.0, connect=1.0)) as client:
            stats_task = client.get(f"{bridge_url}/api/v1/stats", headers=headers)
            account_task = client.post(
                f"{bridge_url}/api/v1/account/query", json={}, headers=headers
            )
            orders_task = client.post(
                f"{bridge_url}/api/v1/orders/query", json={}, headers=headers
            )
            stats_resp, account_resp, orders_resp = await asyncio.gather(
                stats_task, account_task, orders_task, return_exceptions=True
            )
    except Exception as exc:
        logger.warning("TDX overview 拉取失败: %s", exc)
        return {"available": False, "error": str(exc)}

    def _ok(resp, status: int = 200):
        return isinstance(resp, httpx.Response) and resp.status_code == status

    stats_data = {}
    if _ok(stats_resp):
        payload = stats_resp.json()
        stats_data = payload.get("data") or payload if isinstance(payload, dict) else {}

    account_data = {}
    if _ok(account_resp):
        payload = account_resp.json()
        account_data = payload if isinstance(payload, dict) else {}

    orders_data = {}
    if _ok(orders_resp):
        payload = orders_resp.json()
        orders_data = payload if isinstance(payload, dict) else {}

    account = account_data.get("asset") or {}
    positions = account_data.get("positions") or []
    orders = orders_data.get("orders") or []
    cache = stats_data.get("cache") or {}
    security = stats_data.get("security") or {}

    return {
        "available": True,
        "bridge": {
            "hostname": stats_data.get("hostname"),
            "local_ips": stats_data.get("local_ips") or [],
            "bridge_url": stats_data.get("bridge_url") or bridge_url,
            "port": stats_data.get("port"),
            "tdx_connected": bool(stats_data.get("tdx_connected")),
            "server_time": stats_data.get("server_time"),
            "token_configured": bool(stats_data.get("token_configured")),
            "shared_dir": stats_data.get("shared_dir"),
        },
        "account": {
            "currency": account.get("currency"),
            "balance": account.get("balance"),
            "cash": account.get("cash"),
            "asset": account.get("asset"),
            "market_value": account.get("market_value"),
            "position_count": len(positions),
        },
        "positions": positions,
        "orders": orders,
        "cache": {
            "stock_info": cache.get("stock_info", 0),
            "kline": cache.get("kline", 0),
            "sector_stocks": cache.get("sector_stocks", 0),
            "market_snapshot": cache.get("market_snapshot", 0),
            "tdx_log": cache.get("tdx_log", 0),
            "financial": cache.get("financial", 0),
            "trade_log": cache.get("trade_log", 0),
            "mem_hit_rate": cache.get("mem_hit_rate", 0.0),
            "mem_entries": cache.get("mem_entries", 0),
        },
        "security": {
            "banned_ips": security.get("banned_ips", 0),
            "active_ips": security.get("active_ips", 0),
            "write_active": security.get("write_active", 0),
        },
    }


@router.post("/tdx/push-signals")
async def push_signals_to_tdx(
    data: TdxPushSignalsRequest | None = None,
    auth: AuthContext = Depends(get_auth_context),
):
    """把模型推理 Top N 选股推送到通达信（板块 + 预警 + 消息）。

    手动重推入口，与推理完成后的自动推送共用 TdxSignalPushService。
    """
    from backend.services.live_trading.services.tdx_signal_push_service import (
        tdx_signal_pusher,
    )

    payload = data or TdxPushSignalsRequest()
    result = await tdx_signal_pusher.build_push_payload(
        tenant_id=(auth.tenant_id or "default").strip() or "default",
        user_id=str(auth.user_id or "00000001").strip() or "00000001",
        run_id=(payload.run_id or "").strip() or None,
        top_n=payload.top_n,
        block_name=payload.block_name or "QuantMind今日选股",
        push_warnings=payload.push_warnings,
        push_message=payload.push_message,
    )
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error") or "推送失败")
    return result


@router.post("/tdx/rolling-signals")
async def push_rolling_signals_to_tdx(
    data: TdxRollingSignalsRequest | None = None,
    auth: AuthContext = Depends(get_auth_context),
):
    """滚动买卖检查：分数>阈值 买 / 持仓掉下阈值 卖 / 大盘低于 MA20 只卖不买。

    拉取通达信持仓对比推理分数（可指定 trade_date 推历史分数），推送买卖预警
    （半自动，双击闪电下单确认）。
    """
    from backend.services.live_trading.services.tdx_rolling_trade_service import (
        tdx_rolling_trader,
    )

    payload = data or TdxRollingSignalsRequest()
    if payload.run_id and payload.trade_date:
        raise HTTPException(status_code=400, detail="run_id 与 trade_date 只能传一个")
    result = await tdx_rolling_trader.run_rolling_push(
        tenant_id=(auth.tenant_id or "default").strip() or "default",
        user_id=str(auth.user_id or "00000001").strip() or "00000001",
        run_id=(payload.run_id or "").strip() or None,
        trade_date=(payload.trade_date or "").strip() or None,
        fixed_buy_amount=payload.fixed_buy_amount,
        push_message=payload.push_message,
        check_index=payload.check_index,
    )
    if not result.get("success"):
        raise HTTPException(
            status_code=400, detail=result.get("error") or "滚动买卖推送失败"
        )
    return result


@router.get("/tdx/rolling-config")
async def get_rolling_config(auth: AuthContext = Depends(get_auth_context)):
    """读取滚动买卖配置（分数阈值 + 每只固定金额 + 执行模式）。"""
    from backend.services.live_trading.services.tdx_rolling_trade_service import (
        DEFAULT_EXECUTE_MODE,
        load_rolling_config,
    )

    threshold, amount, execute_mode = load_rolling_config(
        (auth.tenant_id or "default").strip() or "default",
        str(auth.user_id or "00000001").strip() or "00000001",
    )
    return {
        "score_threshold": threshold,
        "fixed_buy_amount": amount,
        "execute_mode": execute_mode,
        "auto_place": execute_mode != DEFAULT_EXECUTE_MODE,
    }


@router.put("/tdx/rolling-config")
async def update_rolling_config(
    data: TdxRollingConfigUpdate,
    auth: AuthContext = Depends(get_auth_context),
):
    """保存滚动买卖配置（阈值 + 金额 + 执行模式），推理自动推送即时生效。
    """
    from backend.services.live_trading.services.tdx_rolling_trade_service import (
        DEFAULT_EXECUTE_MODE,
        save_rolling_config,
    )

    execute_mode = data.execute_mode
    if execute_mode is None and data.auto_place is not None:
        execute_mode = "tdx" if data.auto_place else "off"
    if execute_mode is None:
        raise HTTPException(status_code=400, detail="execute_mode 或 auto_place 必填")

    save_rolling_config(
        (auth.tenant_id or "default").strip() or "default",
        str(auth.user_id or "00000001").strip() or "00000001",
        score_threshold=data.score_threshold,
        fixed_buy_amount=data.fixed_buy_amount,
        execute_mode=execute_mode,
    )
    return {
        "success": True,
        "score_threshold": data.score_threshold,
        "fixed_buy_amount": data.fixed_buy_amount,
        "execute_mode": execute_mode,
        "auto_place": execute_mode != DEFAULT_EXECUTE_MODE,
    }
