"""人工卖出成交的周期自动吸收 + 分账/账户差异告警（trade 服务常驻）。

目标（2026-11-18 用户要求）：**下次手动卖出后，下一次 agent 分析就能看到真实持仓**。
手动卖单 ``orders.agent`` 为空，按账本纪律不写分账；本任务每 60 秒把
「账户已清空 + 有未归属卖出成交」的幻影仓自动吸收进分账（判据/写库与
``repair_agent_ledger_manual_sells.py`` 同一份实现），并在吸收后复核
账本 vs 账户的差异：

* ``ledger_excess``：账本 > 账户 —— 有卖单但未覆盖/买入时刻不明，吸收器不动；
* ``account_excess``：账户 > 账本 —— 典型是 TDX 跨日过期委托实际成交但未落分账。

两类差异都只告警（每日每标的去重一次），**不自动猜价格补记**。

环境变量：
  MANUAL_ABSORB_ENABLED          默认 "1"
  MANUAL_ABSORB_INTERVAL_SEC     默认 60（最小 15）
  QM_DECISION_ACCOUNT_USER_ID    默认 10000001（与决策轮同一身份口径）
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

from backend.services.live_trading.services.trading_session import TZ
from backend.shared.decision.manual_sell_auto_absorb import (
    AbsorbResult,
    absorb_manual_sells,
)

logger = logging.getLogger(__name__)

#: 手动卖出只可能发生在连续竞价时段；盘后留 15 分钟给回报落库
_WINDOW_START = dtime(9, 15)
_WINDOW_END = dtime(15, 15)

_ALERT_KEY = "manual_absorb:alert:{date}:{kind}:{code}"
_ALERT_TTL_SEC = 24 * 3600


def _enabled() -> bool:
    raw = str(os.getenv("MANUAL_ABSORB_ENABLED", "1")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _in_window(now: datetime | None = None) -> bool:
    """是否处于「可能产生手动卖出」的时段（工作日 09:15–15:15，上海时区）。"""
    now = now or datetime.now(TZ)
    if now.weekday() >= 5:
        return False
    return _WINDOW_START <= now.time() <= _WINDOW_END


def _identity() -> tuple[str, str]:
    from backend.shared.simulation_account_keys import normalize_runtime_user

    raw = os.getenv("QM_DECISION_ACCOUNT_USER_ID", "").strip()
    return "default", normalize_runtime_user(raw or "10000001")


def _alert_once(kind: str, code: str) -> bool:
    """同一标的同一类差异每日只告警一次；Redis 不可用则放行（宁多一条不漏）。"""
    try:
        from backend.services.trade_shared.deps import get_redis

        key = _ALERT_KEY.format(
            date=datetime.now(TZ).strftime("%Y%m%d"), kind=kind, code=code
        )
        client = get_redis()
        # trade_shared 的 RedisClient.set 不支持 nx/ex（2026-11-18 首跑踩过：
        # TypeError 被兜底成「每次都放行」，一天内重复推送）。去重走裸 redis。
        raw = getattr(client, "client", None) or client
        return bool(raw.set(key, "1", nx=True, ex=_ALERT_TTL_SEC))
    except Exception:  # noqa: BLE001 去重失败不阻断告警
        return True


async def _notify(title: str, content: str, level: str = "info") -> None:
    from backend.shared.notification_publisher import publish_notification_async

    _, user = _identity()
    try:
        await publish_notification_async(
            user_id=user,
            tenant_id="default",
            title=title,
            content=content,
            type="trading",
            level=level,
            action_url="/trading",
        )
    except Exception as exc:  # noqa: BLE001 通知失败不影响吸收
        logger.warning("[manual-absorb] 通知失败: %s", exc)


def _absorb_content(result: AbsorbResult) -> str:
    lines = [
        f"{f['agent']} {f['code']} {float(f['volume']):g} 股 @{float(f['price']):.3f}"
        for f in result.fills
    ]
    pnl = "；".join(
        f"{agent} 已实现 {pnl:+.2f}"
        for agent, pnl in sorted(result.pnl_by_agent.items())
    )
    tail = f"。{pnl}" if pnl else ""
    return "人工卖出成交已吸收进分账：" + "；".join(lines) + tail


def _write_parity_report(result: AbsorbResult) -> None:
    """日终账户 vs 分账报表（按日覆盖；差异存在才写，避免制造空报告）。"""
    if not result.mismatch:
        return
    try:
        directory = Path(os.getenv("QM_REPORT_DIR", "/data/reports")) / "ledger_parity"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{datetime.now(TZ).strftime('%Y%m%d')}.json"
        payload = {
            "as_of": datetime.now(TZ).isoformat(),
            "snapshot": result.snapshot_note,
            "ledger_excess": result.ledger_excess,
            "account_excess": result.account_excess,
            "cash_after": result.cash_after,
            "absorbed": result.fills,
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001 报表失败不阻断告警
        logger.warning("[manual-absorb] 写对账报表失败: %s", exc)


async def absorb_once() -> AbsorbResult:
    """执行一轮吸收；吸收成功/发现差异时发通知。"""
    tenant, user = _identity()
    result = await absorb_manual_sells(tenant, user)

    if result.error:
        logger.error("[manual-absorb] 本轮失败：%s", result.error)
        await _notify("分账吸收失败", result.error, "error")
        return result

    if result.absorbed:
        logger.info(
            "[manual-absorb] 吸收 %d 段：%s",
            len(result.fills),
            ", ".join(
                f"{f['agent']} {f['code']} {float(f['volume']):g}@{float(f['price']):.3f}"
                for f in result.fills
            ),
        )
        await _notify("人工卖出已自动对账", _absorb_content(result), "info")

    for code, diff in result.ledger_excess.items():
        if _alert_once("ledger_excess", code):
            await _notify(
                f"{code} 分账与账户不一致",
                f"分账 {diff['ledger']:g} 股 > 账户 {diff['account']:g} 股："
                f"有未归属卖出但未覆盖/买入时刻不明，自动吸收器未动。"
                f"请核对人工卖出与分账流水（{result.snapshot_note}）。",
                "warning",
            )
    for code, diff in result.account_excess.items():
        if _alert_once("account_excess", code):
            await _notify(
                f"{code} 账户持仓多于分账",
                f"账户 {diff['account']:g} 股 > 分账 {diff['ledger']:g} 股："
                f"典型原因是 TDX 跨日过期委托实际成交但未落分账，或人工买入。"
                f"需人工确认归属（{result.snapshot_note}）。",
                "warning",
            )
    _write_parity_report(result)
    return result


async def run_manual_sell_absorb_task() -> None:
    """常驻循环：启动先补跑一次（catch-up），之后仅在卖出时段内轮询。"""
    interval = max(15, int(os.getenv("MANUAL_ABSORB_INTERVAL_SEC", "60")))
    first = True
    while True:
        try:
            if _enabled() and (first or _in_window()):
                await absorb_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 周期任务不得因单轮异常退出
            logger.exception("[manual-absorb] 周期任务异常")
        finally:
            first = False
        await asyncio.sleep(interval)
