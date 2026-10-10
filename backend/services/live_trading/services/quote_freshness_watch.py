"""market:snapshot 数据心跳监视（审计 M8）——行情断流的告警生产端。

背景：热集轮询席（tdx_hot_set_feed）、持仓馈送、QMT 备源席共用 ``market:snapshot``
标准键，但此前只有**调度心跳**（``qm:sched:hb:tdx_hot_set_feed``，证明循环在跑）：
循环活着却一只也写不进去（桥面半死 / 契约漂移全映射失败）时，体检全绿、页面照转，
直到下游（实时推理 / regime / 撮合取价）吃到空数据才暴露。本模块按
``freshness.quote_policy()`` 唯一谓词给**数据本身**判活，断流即告警。

判据（判活 = **最新写入年龄**：任何写席写进来都算活，热集全量轮转一圈 ≈95s）：
- 每 ``QM_QUOTE_WATCH_INTERVAL_S``（默认 60s）交易时段内抽样热集标的的
  ``market:snapshot``（复用 ``quote_source_audit`` 读侧聚合，与供数源审计同口径）；
- fresh → 健康；stale → 软失败（降级）；unavailable / 读错 / 无样本 → 硬失败；
- 连续 ``WATCH_HARD_RUN``（默认 3，≈3min）次硬失败 → 「行情快照断流」告警；
  软失败连续 ``WATCH_SOFT_RUN``（默认 6，≈6min）次同样转告警；
- 边沿触发 + ``WATCH_REPEAT_S``（默认 1800s）复提醒；恢复回手机（force QQ），
  与桥健康监视同一闭环纪律（``bridge_health_watch``）。

跨场次不复用断流计时：交易日切换时状态机清零（隔夜没有「断流 17 小时」语义）。
心跳入调度注册表（``qm:sched:hb:quote_freshness_watch``，体检 C07 判活）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from backend.shared.freshness import FRESH, STALE
from backend.services.live_trading.services.bridge_health_watch import (
    EVENT_RECOVERED,
    VERDICT_HARD,
    VERDICT_OK,
    VERDICT_SOFT,
    BridgeHealthTracker,
    HealthEvent,
    _format_duration,
)
from backend.services.live_trading.services.tdx_quote_feed import (
    TZ,
    _now_sh,
    is_trading_time,
)

logger = logging.getLogger(__name__)

WATCH_INTERVAL_S = max(15, int(os.getenv("QM_QUOTE_WATCH_INTERVAL_S", "60")))
WATCH_SAMPLE_LIMIT = max(20, int(os.getenv("QM_QUOTE_WATCH_SAMPLE", "120")))
WATCH_HARD_RUN = 3  # ≈3 分钟无新鲜数据 → 断流告警
WATCH_SOFT_RUN = 6  # ≈6 分钟持续 stale → 降级告警

# 运行状态（供 GET /tdx/quote-feed/status 的 quote_freshness 段读取）
quote_freshness_status: dict = {
    "enabled": False,
    "last_tick_at": None,
    "verdict": None,
    "level": None,
    "newest_age_sec": None,
    "dominant": None,
    "requested": 0,
    "missing": 0,
    "tracker": None,
    "last_error": None,
}


def _env_bool(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, "")).strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def classify_sample(sample: dict[str, Any]) -> tuple[str, str]:
    """采样聚合 → ``(verdict, detail)``（纯函数）。

    判活 = **最新写入年龄**（dominant 写席的最新样本；断流时它单调变老）：
    读错 / 无样本 → 硬失败；unavailable（>stale 线或无数据）→ 硬失败；
    stale → 软失败（降级）；fresh → 健康。
    """
    err = str(sample.get("error") or "").strip()
    if err:
        return VERDICT_HARD, f"采样读取失败: {err[:160]}"
    requested = int(sample.get("requested") or 0)
    if requested <= 0:
        return VERDICT_HARD, "热集为空，无可采样标的"
    age = sample.get("newest_age_sec")
    age_text = f"{age:.0f}s" if isinstance(age, (int, float)) else "无"
    level = str(sample.get("level") or "")
    if level == FRESH:
        return VERDICT_OK, ""
    if level == STALE:
        return VERDICT_SOFT, f"最新快照年龄 {age_text}（stale：降级可用）"
    return VERDICT_HARD, f"最新快照年龄 {age_text}（unavailable：无新鲜数据）"


def build_quote_alert(
    event: HealthEvent,
    *,
    sample: dict[str, Any] | None = None,
    interval_seconds: int = WATCH_INTERVAL_S,
    now: float | None = None,
) -> tuple[str, str, str]:
    """事件 → ``(title, content, level)``（纯函数）。"""
    now_ts = float(now) if now is not None else time.time()
    time_line = (
        f"\n时间: {datetime.fromtimestamp(now_ts, TZ).strftime('%Y-%m-%d %H:%M:%S')}"
    )
    sample_line = ""
    if sample:
        requested = int(sample.get("requested") or 0)
        missing = int(sample.get("missing") or 0)
        dominant = str(sample.get("dominant") or "-")
        sample_line = (
            f"\n采样: {requested - missing}/{requested} 只有快照，主写席 {dominant}"
        )
    hint = (
        "\n排查: GET /tdx/quote-feed/status 看 hot_set 段（bridge_ok / 退避 / 失败分账）"
        "与桥登录。"
    )
    if event.kind == EVENT_RECOVERED:
        return (
            "行情快照已恢复",
            f"market:snapshot 恢复写入，断流时长 {_format_duration(event.down_for_s)}"
            f"{sample_line}{time_line}",
            "success",
        )
    if event.repeat:
        return (
            "行情快照仍处于断流",
            f"已持续 {_format_duration(event.down_for_s)}（每 {interval_seconds}s 探测）"
            f"\n原因: {event.detail}{sample_line}{time_line}{hint}",
            "error",
        )
    return (
        "行情快照断流（market:snapshot）",
        f"每 {interval_seconds}s 探测连续失败 {event.consecutive} 次"
        f"\n原因: {event.detail}{sample_line}{time_line}{hint}",
        "error",
    )


async def notify_quote_event(
    event: HealthEvent,
    *,
    sample: dict[str, Any] | None = None,
    interval_seconds: int = WATCH_INTERVAL_S,
) -> bool:
    """投递断流事件：站内通知（管理员 fanout，QQ 旁路自动外发 warning/error）。

    恢复事件显式 ``force`` 推 QQ——断流告警的闭环必须回到同一面（手机），
    与 ``bridge_health_watch`` 同纪律。任何投递失败只记日志，不影响监视循环。
    """
    title, content, level = build_quote_alert(
        event, sample=sample, interval_seconds=interval_seconds
    )
    sent = False
    try:
        from backend.shared.notification_publisher import (
            publish_notification_to_admins_async,
        )

        delivered, audience = await publish_notification_to_admins_async(
            title=title, content=content, type="data_quality", level=level
        )
        if not audience:
            logger.warning("[QuoteWatch] 无管理员用户可通知: %s", title)
        sent = delivered > 0
    except Exception as exc:  # noqa: BLE001 - 监视循环必须活着
        logger.warning("[QuoteWatch] 站内通知投递失败: %s → %s", title, exc)
    if event.kind == EVENT_RECOVERED:
        try:
            from backend.shared.qq_notify import alert_async

            sent = (
                alert_async(
                    level=level,
                    title=title,
                    content=content,
                    alert_type="data_quality",
                    force=True,
                )
                or sent
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("[QuoteWatch] QQ 恢复通知跳过: %s", exc)
    return sent


def sample_hot_set() -> dict[str, Any]:
    """热集抽样 → ``collect_quote_sources`` 聚合（读侧同口径；同步，放线程跑）。

    取排序后的前 ``WATCH_SAMPLE_LIMIT`` 只：样本稳定，判活只看「最新写入年龄」，
    全量轮转体内任意子集都能代表写侧活性。
    """
    from backend.services.live_trading.services.quote_source_audit import (
        collect_quote_sources,
    )
    from backend.services.live_trading.services.tdx_hot_set_feed import (
        load_hot_set_symbols,
    )

    symbols = load_hot_set_symbols()[:WATCH_SAMPLE_LIMIT]
    return collect_quote_sources(symbols, limit=WATCH_SAMPLE_LIMIT)


def _update_status(sample: dict[str, Any], verdict: str) -> None:
    quote_freshness_status["last_tick_at"] = _now_sh().isoformat(timespec="seconds")
    quote_freshness_status["verdict"] = verdict
    quote_freshness_status["level"] = sample.get("level")
    quote_freshness_status["newest_age_sec"] = sample.get("newest_age_sec")
    quote_freshness_status["dominant"] = sample.get("dominant")
    quote_freshness_status["requested"] = int(sample.get("requested") or 0)
    quote_freshness_status["missing"] = int(sample.get("missing") or 0)
    quote_freshness_status["last_error"] = sample.get("error")


async def _watch_tick(
    tracker: BridgeHealthTracker,
    *,
    sampler: Callable[[], dict[str, Any]] | None = None,
) -> tuple[HealthEvent | None, dict[str, Any]]:
    """一次探测：采样 → 判定 → 喂状态机；返回 ``(需投递的事件, 采样)``。"""
    sample_fn = sampler or sample_hot_set
    try:
        # 同步 Redis pipeline（含键读）放线程，不占交易事件循环
        sample = await asyncio.to_thread(sample_fn)
        if not isinstance(sample, dict):
            sample = {"error": f"采样返回类型异常: {type(sample).__name__}"}
    except Exception as exc:  # noqa: BLE001 - 采样失败按硬失败计入
        sample = {"error": str(exc)}
    verdict, detail = classify_sample(sample)
    event = tracker.record(verdict, detail=detail)
    _update_status(sample, verdict)
    quote_freshness_status["tracker"] = tracker.state()
    return event, sample


def _new_tracker() -> BridgeHealthTracker:
    return BridgeHealthTracker(
        hard_threshold=WATCH_HARD_RUN,
        soft_threshold=WATCH_SOFT_RUN,
        repeat_s=float(os.getenv("QM_QUOTE_WATCH_REPEAT_S", "1800")),
    )


async def run_quote_freshness_watch_task() -> None:
    """常驻循环：交易时段内每 ``WATCH_INTERVAL_S`` 判活一次，断流/恢复边沿投递。"""
    if not _env_bool("QM_QUOTE_WATCH_ENABLED", True):
        logger.info("[QuoteWatch] 行情快照心跳监视关闭（QM_QUOTE_WATCH_ENABLED=0）")
        return

    logger.info(
        "[QuoteWatch] 行情快照心跳监视启动：每 %ds 采样热集前 %d 只（硬失败 %d 次告警）",
        WATCH_INTERVAL_S,
        WATCH_SAMPLE_LIMIT,
        WATCH_HARD_RUN,
    )
    quote_freshness_status["enabled"] = True
    tracker = _new_tracker()
    session_day: Any = None
    while True:
        try:
            from backend.shared.scheduler_registry import heartbeat as _sched_heartbeat

            _sched_heartbeat("quote_freshness_watch")
        except Exception:  # noqa: BLE001 - best-effort
            pass
        try:
            now = _now_sh()
            if is_trading_time(now):
                # 跨场次清零：断流计时不跨收盘/隔夜（隔夜时长无意义）
                if session_day != now.date():
                    session_day = now.date()
                    tracker = _new_tracker()
                event, sample = await _watch_tick(tracker)
                if event is not None:
                    await notify_quote_event(
                        event, sample=sample, interval_seconds=WATCH_INTERVAL_S
                    )
            else:
                quote_freshness_status["tracker"] = tracker.state()
        except asyncio.CancelledError:
            quote_freshness_status["tracker"] = tracker.state()
            raise
        except Exception as exc:  # noqa: BLE001 - 循环永续
            quote_freshness_status["last_error"] = str(exc)[:200]
            logger.warning("[QuoteWatch] 循环异常: %s", exc)
        await asyncio.sleep(WATCH_INTERVAL_S)
