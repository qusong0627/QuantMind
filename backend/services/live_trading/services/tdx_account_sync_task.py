"""
TDX Account Sync Task - 定期把通达信桥账户/持仓落库到 real_account_snapshots

供前端 REAL 模式 /account 接口读取通达信实盘持仓。

本任务同时是「桥掉线」告警的探测端：每次同步结论喂给
:class:`~backend.services.live_trading.services.bridge_health_watch.BridgeHealthTracker`，
连续失败/持续空账户即投递告警（站内 + QQ 旁路），恢复再报一条。
"""

import asyncio
import logging

from backend.shared.simulation_account_keys import resolve_db_account_user
from backend.services.live_trading.services.bridge_health_watch import (
    VERDICT_HARD,
    BridgeHealthTracker,
    classify_sync_result,
    notify_bridge_event,
)
from backend.services.live_trading.services.tdx_push_service import tdx_pusher

logger = logging.getLogger(__name__)


async def run_tdx_account_sync_task(interval_seconds: int = 30):
    """定期拉取通达信桥账户并落库 PG。默认每 30 秒同步一次。"""
    if not tdx_pusher.enabled:
        logger.info("[TdxSync] TDX_BRIDGE_URL/TOKEN 未配置，通达信账户同步任务跳过")
        return

    logger.info(
        "[TdxSync] 通达信账户同步任务启动, interval=%ss, bridge=%s",
        interval_seconds,
        tdx_pusher.bridge_url,
    )
    tracker = BridgeHealthTracker()
    while True:
        verdict, detail = VERDICT_HARD, ""
        try:
            result = await tdx_pusher.sync_account_to_pg(
                tenant_id="default",
                # 账户名唯一口径：规范名 10000001（老口径 00000001 曾致委托落库
                # 唯一键冲突、账户快照写到前端看不见的账户下）
                user_id=resolve_db_account_user("TDX_ACCOUNT_USER_ID"),
            )
            verdict, detail = classify_sync_result(result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            detail = f"账户通道异常: {exc}"
            logger.warning("[TdxSync] 通达信账户同步失败: %s", exc)
        event = tracker.record(verdict, detail=detail)
        if event is not None:
            await notify_bridge_event(
                event,
                bridge_url=tdx_pusher.bridge_url,
                interval_seconds=int(interval_seconds),
            )
        await asyncio.sleep(max(10, float(interval_seconds)))
