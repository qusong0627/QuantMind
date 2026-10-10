"""P2-5 值班摘要（trade 常驻任务，默认 15:40）——「今天这条链上发生过什么」的一条记录。

盘点全天七段（规格见 ``docs/实盘全天链整改方案_20261010.md`` → P2-5）：

    收盘报表：送达状态（done/登记键，与 ``daily_pnl_report_task`` 同源）
    池：      rows / 方向 / 文件名 / 缺列（``load_pool_doc``，缺列如实登记）
    轮次：    当日轮数 / Σ决策 / Σ腿 / Σ提交 / Σ失败 / Σ审计行 / 末轮注记（决策轮 log）
    精确否决：``qm_decision_ledger`` 按 ``reject_reason`` 当日分组（pool_not_member 等）
    镜像：    真单镜像跳过/失败两本账（``collect_mirror_stats``，与收盘报表同口径）
    跳发：    调度域跳发台账（``scheduler_skip_ledger.read_skips``，db0）
    信号：    实时推理最后一笔 ``signal_ts``（PG，覆盖到收盘 14:50 的判据）

纪律（与全仓「缺失一律如实」同款，逐段独立）：

- **读不到 ≠ 没有**：任何一段的读取失败渲染成「不可读（×× 故障）」，绝不静默降级
  成 0 或「无」——把 Redis 故障渲染成「今天没跳发」是这份摘要最坏的失败模式。
- **旧格式条目不判**：决策轮 log 里 T2-1 之前的条目没有 ``pool`` 段，缺键就不判、
  不编 0（``legacy`` 计数如实带出来）。
- **旁路不许掀翻主链**：摘要全文落键/通知面登记/QQ 推送，任何一步失败只记日志；
  摘要本身跑不出来的唯一后果是下个周期重试。
- 摘要**不是同步闸门**：它只记录，不阻塞、不重试别人的任务；「该响没响」的裁判
  是隔壁死手检查（celery 侧，跨进程树），不是这份摘要。

键空间全部走 ``backend/shared/duty_receipts``（死手检查核对同一批键，两侧单源）。
任务由 ``trade/main.py`` 的 lifespan 起停；开关 ``QM_DUTY_SUMMARY_ENABLED`` 在两处
各判一次（main 决定建不建任务，循环体决定跑不跑）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any

from backend.services.live_trading.services.trading_session import TZ
from backend.services.trade.services.daily_pnl_report_task import (
    _env_bool,
    _is_trading_day,
    _redis_client,
    collect_mirror_stats,
    parse_report_time,
)
from backend.shared import duty_receipts as dr
from backend.shared import qq_notify

logger = logging.getLogger(__name__)

#: 实时信号「写到收盘」判据（14:50 后仍有写入 = 覆盖了收盘段）。
SIGNAL_CLOSE_THRESHOLD = time(14, 50)

#: 精确否决渲染上限（超出打「…」，全量在 PG 里）。
VETO_ROWS_MAX = 8

#: 跳发原因单行渲染宽度（原因本体可能带换行/长解释，摘要只要一行）。
_SKIP_REASON_WIDTH = 50

#: 轮 log 的数字字段（T2-1 前的旧条目全缺 ⇒ 计 legacy，不判不编 0）。
_ROUND_NUM_FIELDS = (
    "decisions",
    "legs",
    "submitted",
    "failed",
    "watch_armed",
    "audit_rows",
)

_REGISTER_LEVELS = {"sent": "success", "unsent": "error"}


def _config() -> dict:
    return {
        "enabled": _env_bool("QM_DUTY_SUMMARY_ENABLED", True),
        "time": str(os.getenv("QM_DUTY_SUMMARY_TIME", "15:40")),
        "interval": max(10, int(os.getenv("QM_DUTY_SUMMARY_INTERVAL_SEC", "60"))),
    }


# ---------- 纯函数：轮次聚合 ----------


def _int_or_none(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def aggregate_rounds(raw_entries: Sequence[object], day: date) -> dict[str, Any]:
    """聚合当日决策轮 log 条目（**纯函数**；LPUSH 写入 ⇒ 下标 0 最新）。

    缺键不判：旧格式条目（无计数字段）只计轮数、计 ``legacy``，数字不参与求和；
    非 JSON / 非 dict / 非当日条目一律跳过。
    """
    today = day.isoformat()
    sums: dict[str, int] = dict.fromkeys(_ROUND_NUM_FIELDS, 0)
    rounds = 0
    legacy = 0
    slots: list[str] = []
    statuses: dict[str, int] = {}
    last: dict[str, Any] | None = None

    for entry in raw_entries:
        if not isinstance(entry, (str, bytes)):
            continue
        try:
            parsed = json.loads(entry)
        except (TypeError, ValueError):
            continue
        if not isinstance(parsed, dict) or str(parsed.get("day") or "") != today:
            continue
        rounds += 1
        values = {k: _int_or_none(parsed.get(k)) for k in _ROUND_NUM_FIELDS}
        if all(v is None for v in values.values()):
            legacy += 1
        else:
            for k, v in values.items():
                sums[k] += v or 0
        slot = str(parsed.get("slot") or "")
        if slot:
            slots.append(slot)
        status = str(parsed.get("status") or "")
        if status:
            statuses[status] = statuses.get(status, 0) + 1
        if last is None:  # LPUSH：第一条即最新
            pool = parsed.get("pool")
            last = {
                "last_ts": str(parsed.get("ts") or ""),
                "last_slot": slot,
                "last_status": status,
                "last_note": str(parsed.get("note") or ""),
                "last_pool": (
                    {
                        "rows": _int_or_none(pool.get("rows")),
                        "shown": _int_or_none(pool.get("shown")),
                        "dropped": _int_or_none(pool.get("dropped")),
                        "direction": str(pool.get("direction") or ""),
                    }
                    if isinstance(pool, dict) and "rows" in pool
                    else None
                ),
            }
    return {
        "rounds": rounds,
        "legacy": legacy,
        "slots": slots,
        "statuses": statuses,
        "last_ts": (last or {}).get("last_ts", ""),
        "last_slot": (last or {}).get("last_slot", ""),
        "last_status": (last or {}).get("last_status", ""),
        "last_note": (last or {}).get("last_note", ""),
        "last_pool": (last or {}).get("last_pool"),
        **sums,
    }


# ---------- 纯函数：渲染 ----------


def _oneline(text: object, width: int = _SKIP_REASON_WIDTH) -> str:
    first = (
        str(text or "").strip().splitlines()[0].strip()
        if str(text or "").strip()
        else ""
    )
    return first if len(first) <= width else first[: width - 1] + "…"


_REPORT_LINES = {
    "sent": "收盘报表：已送达",
    "nodata": "收盘报表：无数据（当日无台账行；数据迟到会补发）",
    "unsent": "收盘报表：未送达（QQ 推送未成功，任务仍在重试）",
    "none": "收盘报表：无记录（done/登记键都不在——到点未跑或回执已过期）",
    "unreadable": "收盘报表：回执不可读（Redis 故障）",
}


def render_summary(day: date, materials: Mapping[str, Any]) -> str:
    """把 ``collect_all`` 的材料渲染成摘要全文（**纯函数**；缺失一律如实）。"""
    lines: list[str] = [
        _REPORT_LINES.get(str(materials.get("report")), "收盘报表：状态未知")
    ]

    pool = materials.get("pool")
    if pool is None:
        lines.append("池：未生成（文件不在）")
    else:
        line = (
            f"池：{pool.get('rows', 0)} 只 · 方向：{pool.get('direction') or '—'}"
            f" · {Path(str(pool.get('source') or '')).name or '—'}"
        )
        missing = tuple(pool.get("missing_columns") or ())
        if missing:
            line += f"（缺列：{', '.join(str(m) for m in missing)}）"
        lines.append(line)

    rounds = materials.get("rounds") or {"available": False}
    if not rounds.get("available"):
        lines.append("轮次：日志不可读（Redis 故障）")
    elif not rounds.get("rounds"):
        lines.append("轮次：0 轮（今日无轮次记录）")
    else:
        lines.append(
            f"轮次：{rounds['rounds']} 轮 · 决策 {rounds.get('decisions', 0)}"
            f" · 腿 {rounds.get('legs', 0)} · 提交 {rounds.get('submitted', 0)}"
            f" · 失败 {rounds.get('failed', 0)} · 审计行 {rounds.get('audit_rows', 0)}"
        )
        last = f"  末轮 {rounds.get('last_slot') or '—'}（{rounds.get('last_status') or '—'}）"
        if rounds.get("last_note"):
            last += f"：{_oneline(rounds['last_note'], 80)}"
        lines.append(last)
        if rounds.get("statuses"):
            pairs = sorted(rounds["statuses"].items(), key=lambda kv: (-kv[1], kv[0]))
            lines.append("  状态：" + " · ".join(f"{k} {v}" for k, v in pairs))
        if rounds.get("legacy"):
            lines.append(f"  旧格式条目 {rounds['legacy']} 条（无计数字段，未计入）")
        lp = rounds.get("last_pool")
        if lp is not None:
            lines.append(
                f"  末轮取池：{lp.get('rows')} 行 · 展示 {lp.get('shown')}"
                f" · 剔除 {lp.get('dropped')} · 方向：{lp.get('direction') or '—'}"
            )

    vetoes = materials.get("vetoes") or {"available": False}
    if not vetoes.get("available"):
        lines.append("精确否决：台账不可读（PG 故障）")
    elif not vetoes.get("counts"):
        lines.append("精确否决：无")
    else:
        pairs = sorted(vetoes["counts"].items(), key=lambda kv: (-kv[1], kv[0]))
        shown = pairs[:VETO_ROWS_MAX]
        line = "精确否决：" + " · ".join(f"{reason} {n}" for reason, n in shown)
        if len(pairs) > len(shown):
            line += " …"
        lines.append(line)

    mirror = materials.get("mirror") or {"available": False}
    if not mirror.get("available"):
        lines.append("镜像：台账不可读（Redis 故障）")
    elif not mirror.get("stats"):
        lines.append("镜像：无跳过/失败记录")
    else:
        stats = mirror["stats"]
        lines.append(
            f"镜像：跳过 {stats.get('skipped_total', 0)}"
            f" · 失败 {stats.get('failed_total', 0)}"
        )

    skips = materials.get("skips") or {"available": False}
    if not skips.get("available"):
        lines.append("跳发：台账不可读（Redis 故障）")
    elif not skips.get("entries"):
        lines.append("跳发：无")
    else:
        labels = {"market_sync": "市场同步", "factor_fill": "因子填充"}
        parts = []
        for field, reason in sorted(skips["entries"].items()):
            job, _, market = str(field).partition(":")
            parts.append(
                f"{labels.get(job, job)} {market or '—'}（{_oneline(reason)}）"
            )
        lines.append("跳发：" + " · ".join(parts))

    signal = materials.get("signal") or {"available": False}
    if not signal.get("available"):
        lines.append("信号：不可读（PG 故障）")
    elif not signal.get("rows"):
        lines.append("信号：今日无实时信号写入")
    else:
        last_dt = signal.get("last")
        if not isinstance(last_dt, datetime):
            lines.append(f"信号：实时信号 {signal['rows']} 条，最后写入时刻不可读")
        else:
            at = last_dt.astimezone(TZ).strftime("%H:%M:%S")
            if signal.get("wrote_to_close"):
                lines.append(
                    f"信号：实时信号 {signal['rows']} 条，写到 {at}（覆盖收盘段）"
                )
            else:
                lines.append(
                    f"信号：实时信号 {signal['rows']} 条，只写到 {at}"
                    f"（未覆盖 {SIGNAL_CLOSE_THRESHOLD.strftime('%H:%M')} 后）"
                )
    return "\n".join(lines)


# ---------- 取数（逐段独立；复用的是既有唯一实现） ----------


def collect_report_status(native: Any, day: date) -> str:
    """收盘报表送达状态：sent > nodata > unsent > none；读失败 = unreadable。"""
    if native is None:
        return "unreadable"
    try:
        if native.exists(dr.pnl_report_done_key(day)):
            return "sent"
        if native.exists(dr.pnl_report_registered_key(day, "nodata")):
            return "nodata"
        if native.exists(dr.pnl_report_registered_key(day, "unsent")):
            return "unsent"
        return "none"
    except Exception as exc:  # noqa: BLE001 读不到 ≠ 没跑
        logger.warning("[DutySummary] 收盘报表回执读取失败: %s", exc)
        return "unreadable"


def collect_pool(day: date) -> dict[str, Any] | None:
    """当日池文件摘要（``load_pool_doc`` 唯一入口）；文件不在返回 ``None``。"""
    try:
        from backend.shared.decision_context_source import load_pool_doc

        doc = load_pool_doc(day.strftime("%Y%m%d"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[DutySummary] 池文件读取失败: %s", exc)
        return None
    if doc is None:
        return None
    return {
        "rows": len(doc.rows),
        "direction": str(getattr(doc.direction, "direction", "") or ""),
        "source": str(doc.source or ""),
        "missing_columns": tuple(doc.missing_columns or ()),
    }


def collect_rounds(native: Any, day: date) -> dict[str, Any]:
    """决策轮 log 当日聚合；native 客户端不可用/读失败 → ``available=False``。"""
    from backend.services.trade.services.decision_round_core import LOG_KEY

    if native is None:
        return {"available": False}
    try:
        raw = native.lrange(LOG_KEY, 0, -1)
    except Exception as exc:  # noqa: BLE001 读不到 ≠ 没跑
        logger.warning("[DutySummary] 决策轮 log 读取失败: %s", exc)
        return {"available": False}
    return {"available": True, **aggregate_rounds(list(raw or []), day)}


async def collect_vetoes(session: Any, day: date) -> dict[str, Any]:
    """当日精确否决（``qm_decision_ledger`` 按 ``reject_reason`` 分组）。"""
    if session is None:
        return {"available": False}
    from sqlalchemy import text

    sql = text(
        "SELECT reject_reason, COUNT(*) AS n FROM qm_decision_ledger "
        "WHERE trade_date = :day AND reject_reason <> '' "
        "GROUP BY reject_reason ORDER BY n DESC, reject_reason"
    )
    try:
        rows = (await session.execute(sql, {"day": day})).fetchall()
    except Exception as exc:  # noqa: BLE001 读不到 ≠ 没有
        logger.warning("[DutySummary] 决策台账读取失败: %s", exc)
        return {"available": False}
    return {
        "available": True,
        "counts": {str(r[0]): int(r[1] or 0) for r in rows},
    }


async def collect_signal(session: Any, day: date) -> dict[str, Any]:
    """当日实时信号（``source='realtime'``）条数与最后一笔时刻 + 收盘覆盖判据。"""
    if session is None:
        return {"available": False}
    from sqlalchemy import text

    sql = text(
        "SELECT COUNT(*) AS n, MAX(signal_ts) AS last_ts FROM engine_signal_scores "
        "WHERE trade_date = :day AND source = 'realtime'"
    )
    try:
        row = (await session.execute(sql, {"day": day})).fetchone()
    except Exception as exc:  # noqa: BLE001 读不到 ≠ 没有
        logger.warning("[DutySummary] 实时信号读取失败: %s", exc)
        return {"available": False}
    count = int((row[0] if row else 0) or 0)
    last = row[1] if row else None
    if isinstance(last, str):
        try:
            last = datetime.fromisoformat(last)
        except ValueError:
            last = None
    if isinstance(last, datetime) and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)  # 列是 TIMESTAMPTZ；naive 按 UTC 读
    if not isinstance(last, datetime):
        last = None
    wrote_to_close = bool(
        last is not None and last.astimezone(TZ).time() >= SIGNAL_CLOSE_THRESHOLD
    )
    return {
        "available": True,
        "rows": count,
        "last": last,
        "wrote_to_close": wrote_to_close,
    }


def collect_mirror(redis: Any, day: date) -> dict[str, Any]:
    """真单镜像两本账（跳过/失败）；读失败只标不可读，不掀翻摘要。"""
    try:
        stats = collect_mirror_stats(redis, day)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[DutySummary] 镜像台账读取失败: %s", exc)
        return {"available": False}
    return {"available": True, "stats": stats}


def collect_skips(day: date, *, client: Any = None) -> dict[str, Any]:
    """调度域跳发台账（db0）；读失败标不可读（见 ``scheduler_skip_ledger`` 纪律）。"""
    from backend.shared.scheduler_skip_ledger import read_skips

    owned = None
    try:
        if client is None:
            import redis as _redis_lib

            owned = _redis_lib.from_url(
                os.getenv("REDIS_URL", "redis://redis:6379/0"),
                socket_timeout=2,
                decode_responses=True,
            )
            client = owned
        return {"available": True, "entries": read_skips(client, day)}
    except Exception as exc:  # noqa: BLE001 读不到 ≠ 没有
        logger.warning("[DutySummary] 跳发台账读取失败: %s", exc)
        return {"available": False}
    finally:
        if owned is not None:
            try:
                owned.close()
            except Exception:  # noqa: BLE001
                pass


async def collect_all(redis: Any, day: date, *, session: Any = None) -> dict[str, Any]:
    """七段材料一次取齐（每段内部已兜底；本函数不抛）。"""
    native = _redis_client(redis)
    return {
        "report": collect_report_status(native, day),
        "pool": collect_pool(day),
        "rounds": collect_rounds(native, day),
        "vetoes": await collect_vetoes(session, day),
        "mirror": collect_mirror(redis, day),
        "skips": collect_skips(day),
        "signal": await collect_signal(session, day),
    }


# ---------- 发送与登记（与 daily_pnl_report_task 同构；键不同） ----------


def _save_summary(redis: Any, day: date, title: str, content: str) -> None:
    client = _redis_client(redis)
    if client is None:
        return
    try:
        client.set(
            dr.duty_summary_key(day),
            json.dumps(
                {
                    "date": day.strftime("%Y%m%d"),
                    "title": title,
                    "content": content,
                    "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
                },
                ensure_ascii=False,
            ),
            ex=dr.DUTY_SUMMARY_TTL_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 全文落键是旁路
        logger.warning("[DutySummary] 摘要全文落键失败: %s", exc)


def _claim_registration(redis: Any, day: date, outcome: str) -> bool:
    """抢占当日该结果的登记权（SET NX）。Redis 不可用不拦截——痕迹优先于去重。"""
    client = _redis_client(redis)
    if client is None:
        return True
    try:
        return bool(
            client.set(
                dr.duty_summary_registered_key(day, outcome),
                "1",
                ex=dr.DUTY_SUMMARY_TTL_SECONDS,
                nx=True,
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[DutySummary] 登记去重键写入失败（放行）: %s", exc)
        return True


def _release_registration(redis: Any, day: date, outcome: str) -> None:
    client = _redis_client(redis)
    if client is None:
        return
    try:
        client.delete(dr.duty_summary_registered_key(day, outcome))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[DutySummary] 登记去重键释放失败: %s", exc)


async def _publish_admin(
    title: str, content: str, *, level: str, qq_alert: bool
) -> int:
    """管理员 fanout（通知中心可查）；任何失败返回 0（调用方释放占位）。"""
    try:
        from backend.shared import notification_publisher as np

        delivered, audience = await np.publish_notification_to_admins_async(
            title=title,
            content=content,
            type="trading",
            level=level,
            qq_alert=qq_alert,
        )
        if audience == 0:
            logger.warning("[DutySummary] 无管理员用户可登记: %s", title)
        return int(delivered or 0)
    except Exception as exc:  # noqa: BLE001 - 登记失败不回冲主链
        logger.warning("[DutySummary] 通知面登记失败: %s", exc)
        return 0


async def _register_delivery(
    redis: Any, day: date, *, outcome: str, title: str, content: str
) -> bool:
    """登记送达成败进通知面；每（日, 结果）至多一条（未送达周期重试不刷屏）。"""
    if not _claim_registration(redis, day, outcome):
        return False
    delivered = await _publish_admin(
        title, content, level=_REGISTER_LEVELS[outcome], qq_alert=outcome == "unsent"
    )
    if delivered <= 0:
        _release_registration(redis, day, outcome)
        return False
    logger.info("[DutySummary] 已登记进通知面（%s）: %s", outcome, title)
    return True


async def run_duty_summary(
    redis: Any, *, today: date | None = None, session: Any = None
) -> dict:
    """生成并推送当日值班摘要（``sent`` 只有 QQ 明确送达才为 True）。

    ``session`` 缺省时自建只读会话；建不起来/读中途异常 → PG 两段按「不可读」
    照常出摘要（可见性优先于完整性），绝不因此不发。
    """
    day = today or datetime.now(TZ).date()
    date_str = day.strftime("%Y%m%d")

    materials: dict[str, Any] | None = None
    if session is not None:
        try:
            materials = await collect_all(redis, day, session=session)
        except Exception as exc:  # noqa: BLE001
            logger.error("[DutySummary] 取数失败: %s", exc, exc_info=True)
            return {"date": date_str, "sent": False, "error": f"collect_failed: {exc}"}
    else:
        try:
            from backend.shared.database_manager_v2 import get_session

            async with get_session(read_only=True) as opened:
                materials = await collect_all(redis, day, session=opened)
        except Exception as exc:  # noqa: BLE001 PG 段降级为不可读，摘要照发
            logger.warning("[DutySummary] PG 会话不可用（PG 段将标不可读）: %s", exc)
        if materials is None:
            try:
                materials = await collect_all(redis, day, session=None)
            except Exception as exc:  # noqa: BLE001
                logger.error("[DutySummary] 取数失败: %s", exc, exc_info=True)
                return {
                    "date": date_str,
                    "sent": False,
                    "error": f"collect_failed: {exc}",
                }

    title = f"值班摘要 · {day.isoformat()}"
    content = render_summary(day, materials)
    _save_summary(redis, day, title, content)

    sent = False
    try:
        # notify 为同步 HTTP，放线程里跑，不占交易事件循环
        sent = bool(await asyncio.to_thread(qq_notify.notify, title, content))
    except Exception as exc:  # noqa: BLE001 - notify 自带全兜底，这里只防意外
        logger.warning("[DutySummary] QQ 推送异常: %s", exc)
    logger.info(
        "[DutySummary] %s 摘要%s", date_str, "已推送" if sent else "未送达，稍后重试"
    )

    unsent_note = (
        "" if sent else "\n\nQQ 推送未成功（每周期重试，送达后补登「已送达」）。"
    )
    await _register_delivery(
        redis,
        day,
        outcome="sent" if sent else "unsent",
        title=title,
        content=content + unsent_note,
    )
    return {"date": date_str, "sent": sent}


# ---------- 常驻任务 ----------


async def run_duty_summary_task() -> None:
    """常驻循环：每交易日到点后跑一次，QQ 送达才落 Redis done 标记。"""
    cfg = _config()
    if not cfg["enabled"]:
        logger.info("[DutySummary] 值班摘要任务关闭（QM_DUTY_SUMMARY_ENABLED=0）")
        return

    from backend.services.trade_shared.deps import get_redis
    from backend.shared.scheduler_registry import heartbeat as _sched_heartbeat

    target_h, target_m = parse_report_time(cfg["time"])
    logger.info(
        "[DutySummary] 值班摘要任务启动：每交易日 %02d:%02d 推送", target_h, target_m
    )
    last_error = ""
    trading_day_memo: tuple[date, bool] | None = None
    while True:
        try:
            _sched_heartbeat("duty_summary")
        except Exception:  # noqa: BLE001 心跳是旁路
            pass
        try:
            now = datetime.now(TZ)
            today = now.date()
            if (now.hour, now.minute) >= (target_h, target_m):
                if trading_day_memo is None or trading_day_memo[0] != today:
                    trading_day_memo = (today, await _is_trading_day(today))
                if trading_day_memo[1]:
                    redis = get_redis()
                    client = _redis_client(redis)
                    done_key = dr.duty_summary_done_key(today)
                    already = False
                    if client is not None:
                        try:
                            already = bool(client.exists(done_key))
                        except Exception:  # noqa: BLE001
                            already = False
                    if not already:
                        result = await run_duty_summary(redis, today=today)
                        # 只有 QQ 送达才落 done；未送达下一周期继续试
                        if result.get("sent") and client is not None:
                            try:
                                client.set(
                                    done_key,
                                    "1",
                                    ex=dr.DUTY_SUMMARY_DONE_TTL_SECONDS,
                                )
                            except Exception:  # noqa: BLE001
                                pass
            last_error = ""
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            if message != last_error:
                logger.error("[DutySummary] 任务异常: %s", exc, exc_info=True)
                last_error = message
        await asyncio.sleep(cfg["interval"])
