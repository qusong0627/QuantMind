"""QMT 执行端账户快照同步。

定期把大 QMT 的资金/持仓落库到 ``real_account_snapshots``（与 TDX 桥同表同口径），
供前端 REAL 模式 ``/account`` 与对账视图读取。

与 TDX 版本的差异：
  * 数据源是 ``qmt_exec_client``（big-convert RPC），不是 HTTP 桥；
  * 持仓缺现价时用 QuantDB 最近收盘价补全（与模拟撮合同源）；
  * 零资产守卫：桥未就绪/非交易时段返回空账户时不落库，避免 0 资产快照
    污染日终账本与收益计算。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

from backend.services.live_trading.services.qmt_exec_client import (
    QmtExecClient,
    QmtExecError,
    get_qmt_exec_client,
    mask_account_id,
)
from backend.services.live_trading.services.trading_session import is_trading_time

logger = logging.getLogger(__name__)

SOURCE = "qmt_exec"

# 快照新鲜度告警：交易时段内超过阈值未成功落库则通知，冷却期内只报一次
STALE_ALERT_SECONDS_DEFAULT = 300
STALE_ALERT_COOLDOWN_SECONDS_DEFAULT = 3600

SETTINGS_REFRESH_SECONDS = 30.0  # 页面配置重读间隔（开启后无需重启服务）
DISABLED_SLEEP_SECONDS = 60.0  # 未启用/未配置时空转间隔


def _env_seconds(name: str, default: int) -> int:
    """读秒级 env，非法值回退默认（下限 60s，避免误配成高频告警）。"""
    try:
        return max(60, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def batch_quantdb_last_close(symbols: list[str]) -> dict[str, float]:
    """批量取 QuantDB 最近交易日收盘价（持仓现价缺失时补全）。"""
    if not symbols:
        return {}
    result: dict[str, float] = {}
    try:
        from backend.services.simulation.services.local_market_data import (
            get_local_market_data,
        )
        from backend.shared.stock_utils import StockCodeUtil

        market_data = get_local_market_data()
        latest_date = market_data.latest_trade_date()
        if latest_date is None:
            return result
        for symbol in symbols:
            suffix = StockCodeUtil.to_suffix(symbol)
            if not suffix:
                continue
            bar = market_data.get_bar(suffix, latest_date)
            if bar is not None and bar.close > 0:
                result[symbol] = float(bar.close)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[QmtSync] QuantDB 收盘价补全失败: %s", exc)
    return result


class _SyncHealth:
    """账户快照新鲜度：最近成功写入时间、连续失败、告警节流。"""

    def __init__(self) -> None:
        self.started_at = time.monotonic()
        self.last_write_at: float | None = None
        self.consecutive_failures = 0
        self.last_error = ""
        self.last_alert_at = 0.0

    def record(self, result: dict[str, Any]) -> None:
        if not result.get("success"):
            self.consecutive_failures += 1
            self.last_error = str(result.get("error") or result.get("code") or "")
            return
        self.consecutive_failures = 0
        self.last_error = ""
        if not result.get("skipped"):
            self.last_write_at = time.monotonic()

    @property
    def stale_seconds(self) -> float:
        """距最近一次成功落库的秒数（从未落库则从任务启动算起）。"""
        return time.monotonic() - (self.last_write_at or self.started_at)


class QmtAccountSyncService:
    """大 QMT 账户 → ``real_account_snapshots``。"""

    def __init__(self, client: QmtExecClient | None = None):
        self._client = client

    @property
    def client(self) -> QmtExecClient:
        return self._client or get_qmt_exec_client()

    async def fetch_account(self) -> dict[str, Any]:
        """拉取并归一化账户全景（资金 + 持仓）。"""
        client = self.client
        asset = await client.get_asset()
        positions_raw = await client.get_positions()
        price_map = await asyncio.to_thread(
            batch_quantdb_last_close,
            [str(p.get("symbol") or "") for p in positions_raw],
        )
        positions: list[dict[str, Any]] = []
        for item in positions_raw:
            symbol = str(item.get("symbol") or "").strip()
            if not symbol:
                continue
            volume = float(item.get("volume") or 0)
            price = float(item.get("market_value") or 0) / volume if volume > 0 else 0.0
            if price <= 0:
                price = price_map.get(symbol, 0.0)
            positions.append(
                {
                    "symbol": symbol,
                    "name": str(item.get("instrument_name") or "").strip(),
                    "volume": volume,
                    "available_volume": float(item.get("can_use_volume") or 0),
                    "cost_price": float(
                        item.get("avg_price") or item.get("open_price") or 0
                    ),
                    "price": price,
                    "market_value": round(volume * price, 2),
                }
            )
        total_asset = float(asset.get("total_asset") or 0)
        cash = float(asset.get("cash") or 0)
        market_value = float(asset.get("market_value") or 0)
        if market_value <= 0 and positions:
            market_value = round(
                sum(float(x.get("market_value") or 0) for x in positions), 2
            )
        if total_asset <= 0 and (cash > 0 or market_value > 0):
            total_asset = round(cash + market_value, 2)
        return {
            "account_id": str(asset.get("account_id") or self.client.account_id),
            "total_asset": total_asset,
            "cash": cash,
            "frozen_cash": float(asset.get("frozen_cash") or 0),
            "market_value": market_value,
            "positions": positions,
        }

    async def sync_account_to_pg(
        self,
        *,
        tenant_id: str = "default",
        user_id: str = "",
    ) -> dict[str, Any]:
        """拉取账户并落库；返回 ``{success, skipped?, ...}``。"""
        client = self.client
        if not client.configured:
            return {"success": False, "error": "QMT 执行端未启用或未配置"}
        try:
            snapshot = await self.fetch_account()
        except QmtExecError as exc:
            logger.warning("[QmtSync] 拉取账户失败 code=%s: %s", exc.code, exc)
            return {"success": False, "error": str(exc), "code": exc.code}

        total_asset = float(snapshot.get("total_asset") or 0)
        cash = float(snapshot.get("cash") or 0)
        market_value = float(snapshot.get("market_value") or 0)
        positions = snapshot.get("positions") or []

        # 零资产守卫：桥未就绪/未登录时 QMT 可能返回全 0，落库会污染账本
        if total_asset <= 0 and cash <= 0 and market_value <= 0:
            logger.info("[QmtSync] 账户为空（total/cash/mv 全 0），跳过落库")
            return {
                "success": True,
                "skipped": True,
                "reason": "empty_account",
                "position_count": len(positions),
            }

        from sqlalchemy import insert

        from backend.services.trade_shared.models.real_account_snapshot import (
            RealAccountSnapshot,
        )
        from backend.shared.database_manager_v2 import get_session

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        account_id = f"qmt-{tenant_id}-{user_id or '0'}"
        async with get_session() as db:
            await db.execute(
                insert(RealAccountSnapshot).values(
                    tenant_id=tenant_id,
                    user_id=user_id or "0",
                    account_id=account_id,
                    snapshot_at=now,
                    snapshot_date=now.date(),
                    snapshot_month=now.strftime("%Y-%m"),
                    total_asset=total_asset,
                    cash=cash,
                    market_value=market_value,
                    today_pnl_raw=0.0,
                    total_pnl_raw=0.0,
                    floating_pnl_raw=0.0,
                    source=SOURCE,
                    payload_json={
                        "positions": positions,
                        "source": SOURCE,
                        "broker_type": SOURCE,
                        "qmt_account_id": snapshot.get("account_id"),
                        "frozen_cash": snapshot.get("frozen_cash"),
                    },
                )
            )
            await db.commit()

        logger.info(
            "[QmtSync] 账户快照已落库 account=%s total=%.2f cash=%.2f mv=%.2f positions=%d",
            account_id,
            total_asset,
            cash,
            market_value,
            len(positions),
        )
        return {
            "success": True,
            "account_id": account_id,
            "total_asset": total_asset,
            "cash": cash,
            "market_value": market_value,
            "position_count": len(positions),
        }


qmt_account_sync = QmtAccountSyncService()


def _maybe_alert_stale(
    health: _SyncHealth,
    *,
    threshold_seconds: int,
    cooldown_seconds: int,
    account_id: str,
) -> None:
    """快照超时未更新 → 站内通知（仅交易时段，冷却期内只报一次）。"""
    if not is_trading_time():
        return
    stale = health.stale_seconds
    if stale < threshold_seconds:
        return
    now = time.monotonic()
    if now - health.last_alert_at < cooldown_seconds:
        return
    health.last_alert_at = now
    minutes = int(stale // 60)
    if health.consecutive_failures:
        detail = (
            f"连续 {health.consecutive_failures} 次拉取失败"
            f"（最近原因：{health.last_error or '未知'}）"
        )
    else:
        detail = "通道可连但账户为空（未落库）"
    logger.error(
        "[QmtSync] 账户快照已 %d 分钟未更新：%s account=%s",
        minutes,
        detail,
        mask_account_id(account_id),
    )
    try:
        # 延迟导入：real_mirror_service 反向依赖本模块（batch_quantdb_last_close）
        from backend.services.live_trading.services.real_mirror_service import notify

        notify(
            title="大 QMT 账户快照超时未更新",
            content=(
                f"账户 {account_id} 的账户快照已 {minutes} 分钟未成功落库。\n"
                f"{detail}\n"
                "请检查：QMT 是否已登录、big-convert 服务端是否在跑、"
                "Redis 通道与防火墙、「券商实盘接入」配置是否有效。"
            ),
            level="error",
        )
    except Exception as exc:  # noqa: BLE001 - 告警失败不能影响同步任务
        logger.warning("[QmtSync] 新鲜度告警发送失败: %s", exc)


async def run_qmt_account_sync_task(interval_seconds: int = 30) -> None:
    """常驻任务：定期同步大 QMT 账户快照。

    启动时未配置**不退出**：页面「券商实盘接入」开启 QMT 后，任务每轮重读页面
    配置即可接管（直接 return 的话就再也没机会醒来，页面开了也永远没有快照）。
    未启用/未配置时低频空转，不给桥与数据库添压力。
    """
    client = get_qmt_exec_client()
    interval = max(10, int(interval_seconds))
    stale_threshold = _env_seconds(
        "QMT_SYNC_STALE_ALERT_SECONDS", STALE_ALERT_SECONDS_DEFAULT
    )
    stale_cooldown = _env_seconds(
        "QMT_SYNC_STALE_ALERT_COOLDOWN_SECONDS", STALE_ALERT_COOLDOWN_SECONDS_DEFAULT
    )
    health = _SyncHealth()
    logger.info(
        "[QmtSync] 大 QMT 账户同步任务启动, interval=%ss, account=%s, enabled=%s, "
        "stale_alert=%ss, cooldown=%ss",
        interval,
        mask_account_id(client.account_id) or "(未配置)",
        client.configured,
        stale_threshold,
        stale_cooldown,
    )
    last_refresh = 0.0
    while True:
        try:
            now = asyncio.get_running_loop().time()
            if now - last_refresh >= SETTINGS_REFRESH_SECONDS:
                last_refresh = now
                await client.refresh_settings()
            if not client.configured:
                await asyncio.sleep(DISABLED_SLEEP_SECONDS)
                continue
            result = await qmt_account_sync.sync_account_to_pg(
                tenant_id="default",
                user_id=os.getenv("QMT_EXEC_ACCOUNT_USER_ID", "00000001"),
            )
            health.record(result)
            _maybe_alert_stale(
                health,
                threshold_seconds=stale_threshold,
                cooldown_seconds=stale_cooldown,
                account_id=client.account_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 常驻任务不能因单次失败退出
            health.record({"success": False, "error": str(exc)})
            logger.warning("[QmtSync] 账户同步失败: %s", exc)
        await asyncio.sleep(interval)
