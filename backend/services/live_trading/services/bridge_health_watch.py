"""TDX 桥账户通道健康监视 —— 「桥掉线」告警的生产端。

判据取**账户通道**（桥 ``/api/v1/account/query``，即既有 tdx 账户同步任务
本身），不是行情面：桥「假活」时行情照推、``/health`` 照样 200，但账户查询
内容是空的（资产/现金/市值全 0）——行情通 ≠ 账户通，账户才是下单依赖的那条链。

分级与阈值（30s 节拍下的实义）：

* 硬失败（桥不可达 / HTTP!=200 / 鉴权失败）连续 ``hard_threshold`` 次
  （默认 3 次 ≈ 90s）→ 「掉线」告警；
* 软失败（桥在线但持续返回空账户）连续 ``soft_threshold`` 次（默认 10 次
  ≈ 5min）→ 「账户通道异常」告警——开盘前后桥未就绪会短暂返回空账户
  （零资产守卫的既有口径），阈值放宽避免误报；
* 边沿触发：只在「转掉线 / 转恢复」时投递；持续掉线按 ``repeat_s`` 复提醒；
* 送达面：管理员站内通知（``publish_notification_to_admins``，QQ 旁路自动
  外发 warning/error）；恢复事件显式 ``force`` 推 QQ——掉线告警的闭环必须
  回到同一面（手机）。

本模块只做判定与投递，不新增轮询：由 ``tdx_account_sync_task`` 把每次同步
结果喂进来即可。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

# 判定结果（feed 进 tracker 的 verdict）
VERDICT_OK = "ok"
VERDICT_SOFT = "soft"
VERDICT_HARD = "hard"

# 边沿事件
EVENT_DOWN = "down"
EVENT_RECOVERED = "recovered"

DEFAULT_HARD_THRESHOLD = 3
DEFAULT_SOFT_THRESHOLD = 10
DEFAULT_REPEAT_S = 1800.0

_CN_TZ = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class HealthEvent:
    """一次需要投递的健康事件（边沿触发；repeat 表示持续掉线的复提醒）。"""

    kind: str  # EVENT_DOWN / EVENT_RECOVERED
    detail: str
    consecutive: int
    down_since: float
    down_for_s: float
    repeat: bool = False


@dataclass
class BridgeHealthTracker:
    """账户通道健康状态机（纯逻辑，无 I/O，可单测）。

    fail-closed：未知 verdict 一律按硬失败计（宁可多报，不可漏报）。
    """

    hard_threshold: int = DEFAULT_HARD_THRESHOLD
    soft_threshold: int = DEFAULT_SOFT_THRESHOLD
    repeat_s: float = DEFAULT_REPEAT_S
    _hard_run: int = field(default=0, init=False)
    _soft_run: int = field(default=0, init=False)
    _down: bool = field(default=False, init=False)
    _down_since: float = field(default=0.0, init=False)
    _run_started_at: float = field(default=0.0, init=False)
    _last_alert_at: float = field(default=0.0, init=False)
    _last_detail: str = field(default="", init=False)

    @property
    def is_down(self) -> bool:
        return self._down

    def state(self) -> dict[str, Any]:
        """当前状态快照（观测/测试用）。"""
        return {
            "down": self._down,
            "hard_run": self._hard_run,
            "soft_run": self._soft_run,
            "down_since": self._down_since or None,
            "last_detail": self._last_detail,
        }

    def record(
        self, verdict: str, *, detail: str = "", now: float | None = None
    ) -> HealthEvent | None:
        """喂入一次探测结论，返回需要投递的事件（没有则 ``None``）。"""
        ts = float(now) if now is not None else datetime.now(timezone.utc).timestamp()
        if verdict == VERDICT_OK:
            self._hard_run = 0
            self._soft_run = 0
            self._last_detail = ""
            if not self._down:
                return None
            down_for = max(0.0, ts - self._down_since)
            self._down = False
            self._down_since = 0.0
            return HealthEvent(
                kind=EVENT_RECOVERED,
                detail="",
                consecutive=0,
                down_since=0.0,
                down_for_s=down_for,
            )

        if verdict not in (VERDICT_SOFT, VERDICT_HARD):
            # fail-closed：未知结论按硬失败算，并留下原文供排查
            detail = detail or f"未知探测结论: {verdict!r}"
            verdict = VERDICT_HARD
        # 轮起点 = 当前**同类**连续失败的第一次（硬软交替会打断计数，
        # 起点也必须跟着重置，否则「已持续 X 分钟」把被打断的旧轮算进来）
        if verdict == VERDICT_SOFT:
            if self._soft_run == 0:
                self._run_started_at = ts
            self._soft_run += 1
            self._hard_run = 0
            run = self._soft_run
            reached = self._soft_run >= self.soft_threshold
        else:
            if self._hard_run == 0:
                self._run_started_at = ts
            self._hard_run += 1
            self._soft_run = 0
            run = self._hard_run
            reached = self._hard_run >= self.hard_threshold
        self._last_detail = detail or self._last_detail

        if not self._down:
            if not reached:
                return None
            self._down = True
            self._down_since = self._run_started_at or ts
            self._last_alert_at = ts
            return HealthEvent(
                kind=EVENT_DOWN,
                detail=self._last_detail,
                consecutive=run,
                down_since=self._down_since,
                down_for_s=max(0.0, ts - self._down_since),
            )
        if self.repeat_s > 0 and (ts - self._last_alert_at) >= self.repeat_s:
            self._last_alert_at = ts
            return HealthEvent(
                kind=EVENT_DOWN,
                detail=self._last_detail,
                consecutive=run,
                down_since=self._down_since,
                down_for_s=max(0.0, ts - self._down_since),
                repeat=True,
            )
        return None


def classify_sync_result(result: Any) -> tuple[str, str]:
    """账户同步返回值 → ``(verdict, detail)``（纯函数）。

    ``sync_account_to_pg`` 的失败不一定抛异常：空账户走 ``skipped`` 分支返回，
    桥侧错误则可能带 ``success=False``——两条都要识别，不能只看异常。
    """
    if not isinstance(result, Mapping):
        return VERDICT_HARD, f"账户同步返回类型异常: {type(result).__name__}"
    if result.get("skipped") and str(result.get("reason") or "") == "empty_account":
        return VERDICT_SOFT, "桥在线但账户为空（资产/现金/市值全 0）"
    if result.get("success") is False:
        return VERDICT_HARD, f"账户查询失败: {result.get('error') or '未知错误'}"
    return VERDICT_OK, ""


def _format_duration(seconds: float) -> str:
    total = int(max(0.0, seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分"
    if minutes:
        return f"{minutes} 分 {secs} 秒"
    return f"{secs} 秒"


def _cst_now_text(ts: float) -> str:
    return datetime.fromtimestamp(ts, _CN_TZ).strftime("%Y-%m-%d %H:%M:%S")


def build_alert(
    event: HealthEvent,
    *,
    bridge_url: str = "",
    interval_seconds: int = 30,
    now: float | None = None,
) -> tuple[str, str, str]:
    """事件 → ``(title, content, level)``（纯函数）。"""
    now_ts = float(now) if now is not None else datetime.now(timezone.utc).timestamp()
    bridge_line = f"\n桥: {bridge_url}" if bridge_url else ""
    time_line = f"\n时间: {_cst_now_text(now_ts)}"
    if event.kind == EVENT_RECOVERED:
        return (
            "通达信桥已恢复",
            f"账户通道恢复，掉线时长 {_format_duration(event.down_for_s)}"
            f"{bridge_line}{time_line}",
            "success",
        )
    if event.repeat:
        return (
            "通达信桥仍处于掉线",
            f"已持续 {_format_duration(event.down_for_s)}（每 {interval_seconds}s"
            f" 探测连续失败 {event.consecutive} 次）"
            f"\n原因: {event.detail}{bridge_line}{time_line}"
            "\n交易时段内请尽快处理：检查桥上通达信是否已登录、桥进程是否存活。",
            "error",
        )
    return (
        "通达信桥账户通道掉线",
        f"每 {interval_seconds}s 探测连续失败 {event.consecutive} 次"
        f"\n原因: {event.detail}{bridge_line}{time_line}"
        "\n交易时段内请尽快处理：检查桥上通达信是否已登录、桥进程是否存活。",
        "error",
    )


async def notify_bridge_event(
    event: HealthEvent,
    *,
    bridge_url: str = "",
    interval_seconds: int = 30,
) -> bool:
    """投递健康事件：站内通知（管理员 fanout，QQ 旁路自动外发）。

    恢复事件额外显式 force 推 QQ——QQ 旁路的等级过滤会挡掉 success，
    但「掉线→恢复」的闭环必须回到手机；普通 success 生产者不得照抄此法。
    任何投递失败只记日志，不影响监视循环。
    """
    title, content, level = build_alert(
        event, bridge_url=bridge_url, interval_seconds=interval_seconds
    )
    sent = False
    try:
        from backend.shared.notification_publisher import (
            publish_notification_to_admins_async,
        )

        delivered, audience = await publish_notification_to_admins_async(
            title=title, content=content, type="health", level=level
        )
        if not audience:
            logger.warning("[BridgeHealth] 无管理员用户可通知: %s", title)
        sent = delivered > 0
    except Exception as exc:  # noqa: BLE001 - 监视循环必须活着
        logger.warning("[BridgeHealth] 站内通知投递失败: %s → %s", title, exc)
    if event.kind == EVENT_RECOVERED:
        try:
            from backend.shared.qq_notify import alert_async

            sent = (
                alert_async(
                    level=level,
                    title=title,
                    content=content,
                    alert_type="health",
                    force=True,
                )
                or sent
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("[BridgeHealth] QQ 恢复通知跳过: %s", exc)
    return sent
