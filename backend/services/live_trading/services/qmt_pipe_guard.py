"""QMT 管道健康监视 + 超时委托核对（2026-10-30）。

两块职责，都只读 Redis、不占用 QMT 单线程管道：

1. **管道健康告警**：读伴生桥写出的 ``qm:qmt:pipe:metrics``
   （``queue_len`` / ``pos_age_s`` / ``order_age_s``），边沿触发投递管理员通知。
   背景：2026-10-30 盘中管道冻结 8 分钟，全链零告警。
2. **超时委托核对**：客户端发单超时（委托可能已提交）时把 remark 记进
   ``qm:qmt:unverified:{account}``；本任务从 ``bigqmt:order_events:{account}``
   流里比对 remark——出现即销账，超过 ``stale_after_s`` 仍未出现则告警。

判定全部是纯函数（``evaluate_pipe_metrics`` / ``PipeHealthTracker`` /
``reconcile_unverified``），I/O 只在 ``run_qmt_pipe_guard_task`` 里。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

METRICS_KEY = "qm:qmt:pipe:metrics"
UNVERIFIED_KEY = "qm:qmt:unverified:{account}"
ORDER_EVENTS_KEY = "bigqmt:order_events:{account}"

DEFAULT_QUEUE_MAX = 3
DEFAULT_POS_AGE_MAX_S = 60.0
DEFAULT_ORDER_AGE_MAX_S = 60.0
DEFAULT_INTERVAL_S = 15.0
DEFAULT_REPEAT_S = 1800.0
DEFAULT_UNVERIFIED_STALE_S = 600.0
DEFAULT_HARD_THRESHOLD = 2
DEFAULT_UNVERIFIED_TTL_S = 7200

_CN_TZ = timezone(timedelta(hours=8))


# ---------------------------------------------------------------- 判定（纯函数）
def _f(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def evaluate_pipe_metrics(
    metrics: Mapping[str, object],
    *,
    queue_max: int = DEFAULT_QUEUE_MAX,
    pos_age_max_s: float = DEFAULT_POS_AGE_MAX_S,
    order_age_max_s: float = DEFAULT_ORDER_AGE_MAX_S,
) -> list[str]:
    """把伴生桥指标翻成人话问题列表；空列表 = 健康。

    读不到指标（空 dict / 缺字段）**不判故障**——桥刚重启时指标尚未写入，
    误报会把真实故障淹没。缺字段按 0 处理。
    """
    if not metrics:
        return []
    issues: list[str] = []
    queue_len = int(_f(metrics.get("queue_len")))
    if queue_max > 0 and queue_len >= queue_max:
        issues.append(f"请求队列积压 {queue_len}（阈值 {queue_max}）")
    pos_age = _f(metrics.get("pos_age_s"), -1.0)
    if pos_age_max_s > 0 and pos_age > pos_age_max_s:
        issues.append(f"持仓推送停滞 {pos_age:.0f}s（阈值 {pos_age_max_s:.0f}s）")
    order_age = _f(metrics.get("order_age_s"), -1.0)
    if order_age_max_s > 0 and order_age > order_age_max_s:
        issues.append(f"委托回报停滞 {order_age:.0f}s（阈值 {order_age_max_s:.0f}s）")
    return issues


EVENT_DOWN = "down"
EVENT_RECOVERED = "recovered"


@dataclass(frozen=True)
class PipeEvent:
    kind: str
    issues: tuple[str, ...]
    consecutive: int
    down_for_s: float
    repeat: bool = False


@dataclass
class PipeHealthTracker:
    """边沿触发状态机（纯逻辑，可单测）。"""

    hard_threshold: int = DEFAULT_HARD_THRESHOLD
    repeat_s: float = DEFAULT_REPEAT_S
    _run: int = field(default=0, init=False)
    _down: bool = field(default=False, init=False)
    _down_since: float = field(default=0.0, init=False)
    _last_alert_at: float = field(default=0.0, init=False)

    @property
    def is_down(self) -> bool:
        return self._down

    def feed(self, issues: list[str], *, now: float | None = None) -> PipeEvent | None:
        now = time.time() if now is None else float(now)
        if issues:
            self._run += 1
            if not self._down:
                if self._run < max(1, self.hard_threshold):
                    return None
                self._down = True
                self._down_since = now
                self._last_alert_at = now
                return PipeEvent(EVENT_DOWN, tuple(issues), self._run, 0.0)
            if self.repeat_s > 0 and now - self._last_alert_at >= self.repeat_s:
                self._last_alert_at = now
                return PipeEvent(
                    EVENT_DOWN,
                    tuple(issues),
                    self._run,
                    now - self._down_since,
                    repeat=True,
                )
            return None
        # 健康
        self._run = 0
        if self._down:
            self._down = False
            down_for = now - self._down_since
            self._down_since = 0.0
            self._last_alert_at = 0.0
            return PipeEvent(EVENT_RECOVERED, (), 0, down_for)
        return None


def reconcile_unverified(
    unverified: Mapping[str, Mapping[str, object] | str],
    seen_remarks: set[str],
    *,
    now: float | None = None,
    stale_after_s: float = DEFAULT_UNVERIFIED_STALE_S,
) -> tuple[list[str], list[dict]]:
    """超时委托核对（纯函数）。

    返回 ``(resolved_remarks, stale_entries)``：
    - resolved：订单事件流里已出现该 remark（委托确实存在）；
    - stale：超过 ``stale_after_s`` 仍未见，需要告警的条目。
    """
    now = time.time() if now is None else float(now)
    resolved: list[str] = []
    stale: list[dict] = []
    for remark, raw in unverified.items():
        entry: dict = {}
        if isinstance(raw, Mapping):
            entry = dict(raw)
        elif isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                entry = parsed if isinstance(parsed, dict) else {}
            except (TypeError, ValueError):
                entry = {}
        if remark in seen_remarks:
            resolved.append(remark)
            continue
        ts = _f(entry.get("ts"), 0.0)
        if stale_after_s > 0 and now - ts > stale_after_s:
            stale.append({"remark": remark, **entry})
    return resolved, stale


def read_order_event_remarks(rows: list[str | bytes], limit: int = 500) -> set[str]:
    """从 ``bigqmt:order_events`` 的 XREVRANGE 原始行里提取 remark 集合。"""
    seen: set[str] = set()
    for raw in rows[:limit]:
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        if not text.startswith("{"):
            continue
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            continue
        row = payload.get("row") or {}
        for key in ("user_order_id", "remark", "order_remark"):
            value = row.get(key)
            if value:
                seen.add(str(value))
    return seen


def build_alert(event: PipeEvent, *, account: str = "") -> tuple[str, str, str]:
    """事件 → (title, content, level)。"""
    stamp = datetime.now(_CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
    acct = f"\n账户: {account}" if account else ""
    detail = "\n".join(f"- {item}" for item in event.issues) or "- （已恢复）"
    if event.kind == EVENT_RECOVERED:
        return (
            "QMT 管道已恢复",
            f"持续 {event.down_for_s:.0f}s 后恢复{acct}\n时间: {stamp}",
            "success",
        )
    title = "QMT 管道仍异常" if event.repeat else "QMT 管道异常"
    return (
        title,
        f"连续 {event.consecutive} 次探测异常{acct}\n{detail}\n时间: {stamp}"
        "\n交易时段内下单/撤单可能超时：超时委托禁止重试，先查 order_events。",
        "error",
    )


# ---------------------------------------------------------------- I/O
async def _notify(event: PipeEvent, *, account: str = "") -> bool:
    title, content, level = build_alert(event, account=account)
    sent = False
    try:
        from backend.shared.notification_publisher import (
            publish_notification_to_admins_async,
        )

        delivered, audience = await publish_notification_to_admins_async(
            title=title, content=content, type="health", level=level
        )
        sent = delivered > 0
        if not audience:
            logger.warning("[QmtPipeGuard] 无管理员用户可通知: %s", title)
    except Exception as exc:  # noqa: BLE001 - 监视循环必须活着
        logger.warning("[QmtPipeGuard] 站内通知失败 %s: %s", title, exc)
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
            logger.info("[QmtPipeGuard] QQ 恢复通知跳过: %s", exc)
    return sent


async def _redis_url_db5() -> str:
    """桥 Redis 连接串：以页面配置 ``broker:config:qmt_exec`` 为准（db5）。

    只读 ``from_env`` 会拿到 db0 —— 伴生桥的 ``qm:qmt:pipe:metrics`` 在 db5，
    必须先 ``refresh_settings`` 合并页面配置，否则本任务永远读不到指标。
    """
    from backend.services.live_trading.services.qmt_exec_client import QmtExecClient

    client = QmtExecClient.from_env()
    await client.refresh_settings()
    cfg = client.effective_config()
    url = QmtExecClient._build_redis_url(cfg)
    if not url:
        raise RuntimeError("QMT 桥 Redis 未配置")
    return url


async def run_qmt_pipe_guard_task(interval_seconds: float | None = None) -> None:
    """常驻任务：每 ``interval_seconds`` 采一次指标 + 核对超时委托。"""
    import redis.asyncio as aioredis

    interval = float(
        interval_seconds or os.getenv("QM_QMT_PIPE_GUARD_INTERVAL", DEFAULT_INTERVAL_S)
    )
    queue_max = int(os.getenv("QM_QMT_PIPE_QUEUE_MAX", DEFAULT_QUEUE_MAX))
    pos_age_max = float(os.getenv("QM_QMT_PIPE_POS_AGE_MAX", DEFAULT_POS_AGE_MAX_S))
    order_age_max = float(
        os.getenv("QM_QMT_PIPE_ORDER_AGE_MAX", DEFAULT_ORDER_AGE_MAX_S)
    )
    stale_after = float(
        os.getenv("QM_QMT_UNVERIFIED_STALE", DEFAULT_UNVERIFIED_STALE_S)
    )
    account = str(os.getenv("QMT_EXEC_ACCOUNT_ID", "") or "").strip()
    if not account:
        # 生产环境账号来自页面配置 broker:config:qmt_exec（容器 env 通常不配）
        try:
            from backend.services.live_trading.services.qmt_exec_client import (
                QmtExecClient,
            )

            _client = QmtExecClient.from_env()
            await _client.refresh_settings()
            account = str(_client.effective_config().get("account_id") or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[QmtPipeGuard] 账号解析失败: %s", exc)
    tracker = PipeHealthTracker()
    redis_client = None
    logger.info(
        "[QmtPipeGuard] started interval=%.0fs queue_max=%s account=%s",
        interval,
        queue_max,
        account or "-",
    )
    while True:
        try:
            if redis_client is None:
                redis_client = aioredis.from_url(
                    await _redis_url_db5(), decode_responses=True
                )
            metrics = await redis_client.hgetall(METRICS_KEY)
            issues = evaluate_pipe_metrics(
                metrics or {},
                queue_max=queue_max,
                pos_age_max_s=pos_age_max,
                order_age_max_s=order_age_max,
            )
            event = tracker.feed(issues)
            if event is not None:
                await _notify(event, account=account)
            if account:
                await _reconcile_once(redis_client, account, stale_after)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("[QmtPipeGuard] loop error: %s", exc)
            if redis_client is not None:
                try:
                    await redis_client.aclose()
                except Exception:  # noqa: BLE001
                    pass
                redis_client = None
        await asyncio.sleep(max(2.0, interval))


async def _reconcile_once(redis_client, account: str, stale_after: float) -> None:
    key = UNVERIFIED_KEY.format(account=account)
    entries = await redis_client.hgetall(key)
    if not entries:
        return
    rows = await redis_client.xrevrange(
        ORDER_EVENTS_KEY.format(account=account), count=500
    )
    flat: list[str] = []
    for item in rows or []:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            flat.append(item[1])
    seen = read_order_event_remarks(flat)
    resolved, stale = reconcile_unverified(entries, seen, stale_after_s=stale_after)
    if resolved:
        await redis_client.hdel(key, *resolved)
        logger.info("[QmtPipeGuard] 超时委托已核对销账: %s", ",".join(resolved))
    for item in stale:
        logger.warning(
            "[QmtPipeGuard] 超时委托超过 %.0fs 仍未出现: %s",
            stale_after,
            item.get("remark"),
        )
        title = "QMT 超时委托未核对"
        content = (
            f"remark {item.get('remark')} 自 {item.get('ts')} 起超时，"
            f"订单事件流中仍未出现。\n"
            f"标的: {item.get('symbol', '')} {item.get('side', '')} "
            f"{item.get('quantity', '')} @ {item.get('price', '')}\n"
            "请人工核对 QMT 委托列表，确认是否已下单。"
        )
        try:
            from backend.shared.notification_publisher import (
                publish_notification_to_admins_async,
            )

            await publish_notification_to_admins_async(
                title=title, content=content, type="health", level="error"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[QmtPipeGuard] 未核对告警投递失败: %s", exc)
        # 标记已告警，避免每轮重复
        try:
            await redis_client.hdel(key, item["remark"])
        except Exception:  # noqa: BLE001
            pass
