"""P2-5 值班死手检查（celery beat，每交易日 16:00~23:30 每 30 分钟）。

回答的问题只有一个：**「该响没响」**。核对当日四类回执（键全走
``backend/shared/duty_receipts``，与生产者单源）：

    收盘报表（15:10）   done=已送达；registered:nodata=无数据（合法终态）；
                        registered:unsent=送了没成
    值班摘要（15:40）   done=已推送
    停滞检查（trade）   键在=「整天没跑」检查执行过（值=检查结论 JSON）
    实时信号            状态键 config.enabled 为真才判；判据 = PG 里当日
                        source='realtime' 的 max(signal_ts) ≥ 14:50

**为什么住在 celery**：它是跨进程树的裁判位——trade 整体死亡（进程没了、事件循环
卡死、容器 OOM）时，本检查照样跑、照样响。住在 trade 里的检查只能证明「trade 觉得
自己还行」。「该响没响」检查绝不能与被检查对象同生共死。

语义纪律：

- **缺失集只收缩**：16:00~23:30 每 30 分钟一轮（共 16 轮），把「已告警过的缺失集」
  落 Redis（``deadman_alerted_key``）——集合不变不重复响（否则一晚上刷 16 条）；
  集合**变化**（收缩/新增）即再告警一次（新缺口要让人知道）；补全后发**恢复**并
  当日**封账**（done 键）——封账后不再核对（回执 TTL 都 ≥3 天，隔日回看走全文键）。
- **读失败本身要报**：db2 读不到 ⇒ ``trade_receipts_unreadable``、状态/PG 读不到
  ⇒ ``realtime_signals_unreadable``。Redis 故障是「不知道」不是「没问题」。
- **连接池按 tick 清理**：celery worker 子进程跨 tick 复用，池里可能残留绑定
  **已关闭事件循环**的连接（前一任务留下的）——checkout 复用会抛「attached to a
  different loop」，``pre_ping`` 不把它识别为断连、救不了，会被误判成
  ``realtime_signals_unreadable``。自开 PG 会话前后各清一次池（``close_database``
  best-effort），两个 tick 之间不互相投毒。
- **实时信号的门控要克制**：状态键 ``config.enabled`` 为假 ⇒ 服务显式关闭，跳过
  （部署没开实时推理不是故障）；状态键**不在** ⇒ 从未运行过、无法证明「该响」，
  按 ``unknown`` 跳过（状态键 TTL 24h、运行中每周期刷新——真死掉的服务 24h 内
  状态键还在，届时按 enabled+PG 判据抓得住）。
- **非交易日秒退**：周末没有回执是对的。beat 条目不分交易日（简单、可读），闸门
  在任务体（``_is_trading_day``，与收盘报表/值班摘要同口径的降级策略）。
- 通知走 ``notification_publisher`` 管理员 fanout（level=error/success + qq_alert
  旁路到手机）；投递失败只记日志、**不落** alerted 台账——下轮集合未变即再试一次
  （投递失败 ≠ 已通知，与「已通知集合」分开记账；代价是极端情况下重试到投递成功
  为止，每次间隔半小时，不构成刷屏）。
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timezone
from typing import Any

from sqlalchemy import text

from backend.services.live_trading.services.trading_session import TZ
from backend.shared import duty_receipts as dr
from backend.shared.database_manager_v2 import close_database, get_session

logger = logging.getLogger(__name__)

#: 实时信号「写到收盘」判据（与 ``duty_summary.SIGNAL_CLOSE_THRESHOLD`` 同值；
#: 改则双改，``test_duty_deadman.py`` 有交叉断言钉死两侧）。
SIGNAL_CLOSE_THRESHOLD = time(14, 50)

#: 缺失项 → 人话（告警正文逐项渲染；键与 ``evaluate_receipts`` 返回值一一对应）。
_ITEM_TEXT = {
    "trade_receipts_unreadable": (
        "trade 域回执不可读（Redis 故障）——报表/摘要/停滞检查三项**无法核对**，"
        "先查 Redis 与 8002 服务"
    ),
    "pnl_report_missing": (
        "15:10 收盘收益报表：done 与登记键都不在（到点未跑，或报表任务已死）"
    ),
    "pnl_report_unsent": (
        "15:10 收盘收益报表：登记为「未送达」（QQ 推送一直没成功，任务仍在重试）"
    ),
    "duty_summary_missing": (
        "15:40 值班摘要：done 键不在（未推送成功——查 QQ 通道或摘要任务）"
    ),
    "stall_check_missing": (
        "决策轮停滞检查：今日无执行回执（trade 侧 _stall_watch 没跑过，"
        "决策轮 worker 可能整体没起来）"
    ),
    "realtime_signals_missing": (
        "实时推理：今日 0 条实时信号写入（服务在跑但从未发布——查模型/热集/行情源）"
    ),
    "realtime_signals_stale": (
        "实时推理：信号未覆盖收盘段（最后写入早于 14:50，盘尾停更）"
    ),
    "realtime_signals_unreadable": (
        "实时推理：状态键或 PG 读取失败——**无法核对**信号覆盖"
    ),
}


def _env_bool(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, "")).strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


async def _is_trading_day(day: date) -> bool:
    """交易日判定（与收盘报表/值班摘要同口径：日历不可用按工作日近似）。"""
    try:
        from backend.shared.trading_calendar import calendar_service

        return await calendar_service.is_trading_day(
            market="SSE", trade_date=day, tenant_id="default", user_id="0"
        )
    except Exception:  # noqa: BLE001
        return day.weekday() < 5


def _redis_client(db: int) -> Any:
    """原生客户端（decode_responses；与 realtime_service 状态写出侧同形）。"""
    import redis as redis_lib

    return redis_lib.Redis(
        host=os.getenv("REDIS_HOST") or "redis",
        port=int(os.getenv("REDIS_PORT", "6379")),
        db=db,
        password=os.getenv("REDIS_PASSWORD") or None,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )


# ---------- 纯函数 ----------


def evaluate_receipts(facts: Mapping[str, str]) -> list[str]:
    """事实 → 缺失项码表（**纯函数**；全齐返回 ``[]``）。

    ``facts``：``pnl`` ∈ sent/nodata/unsent/missing/unreadable；
    ``summary`` ∈ done/missing/unreadable；``stall`` ∈ present/missing/unreadable；
    ``realtime`` ∈ ok/stale/missing/disabled/unknown/unreadable。
    db2 三项任一 unreadable ⇒ 收敛为单个 ``trade_receipts_unreadable``（一次故障
    一条告警，不刷三条）。
    """
    missing: set[str] = set()
    pnl = facts.get("pnl")
    summary = facts.get("summary")
    stall = facts.get("stall")
    if "unreadable" in {pnl, summary, stall}:
        missing.add("trade_receipts_unreadable")
    else:
        if pnl == "unsent":
            missing.add("pnl_report_unsent")
        elif pnl == "missing":
            missing.add("pnl_report_missing")
        if summary == "missing":
            missing.add("duty_summary_missing")
        if stall == "missing":
            missing.add("stall_check_missing")
    realtime = facts.get("realtime")
    if realtime == "missing":
        missing.add("realtime_signals_missing")
    elif realtime == "stale":
        missing.add("realtime_signals_stale")
    elif realtime == "unreadable":
        missing.add("realtime_signals_unreadable")
    return sorted(missing)


def decide_alert(current: Sequence[str], previously: Sequence[str]) -> str:
    """告警决策（**纯函数**）：``alert`` / ``recover`` / ``silent``。

    缺失集只收缩：集不变不重复响；集变化（含收缩）即再响；空集+曾响 ⇒ 恢复。
    """
    cur, prev = set(current), set(previously)
    if not cur:
        return "recover" if prev else "silent"
    return "silent" if cur == prev else "alert"


def render_alert_content(
    day: date, missing: Sequence[str], facts: Mapping[str, str]
) -> str:
    lines = [f"当日回执核对：{len(missing)} 项该响没响（{day.isoformat()}）", ""]
    lines.extend(f"- {_ITEM_TEXT.get(code, code)}" for code in missing)
    lines.append("")
    lines.append(
        "核对窗口 16:00~23:30 每 30 分钟一轮；补齐后自动发恢复，全部到齐当日封账。"
    )
    lines.append(
        "事实: 报表={pnl} 摘要={summary} 停滞检查={stall} 实时信号={realtime}".format(
            pnl=facts.get("pnl", "?"),
            summary=facts.get("summary", "?"),
            stall=facts.get("stall", "?"),
            realtime=facts.get("realtime", "?"),
        )
    )
    return "\n".join(lines)


def render_recovery_content(day: date) -> str:
    return (
        f"当日回执已补齐（{day.isoformat()}）：收盘报表/值班摘要/停滞检查/实时信号"
        "四项均到齐，当日死手封账，今晚不再核对。"
    )


def _status_key() -> str:
    """实时推理状态键（单源 = realtime_service.STATUS_KEY）。"""
    from backend.services.engine.inference.realtime_service import STATUS_KEY

    return STATUS_KEY


# ---------- 取数 ----------


def _read_trade_facts(client: Any, day: date) -> dict[str, str]:
    """db2 三项回执；Redis 故障 ⇒ 三项统一 unreadable（一次故障一条告警）。"""
    if client is None:
        return {"pnl": "unreadable", "summary": "unreadable", "stall": "unreadable"}
    try:
        if client.exists(dr.pnl_report_done_key(day)):
            pnl = "sent"
        elif client.exists(dr.pnl_report_registered_key(day, "nodata")):
            pnl = "nodata"
        elif client.exists(dr.pnl_report_registered_key(day, "unsent")):
            pnl = "unsent"
        else:
            pnl = "missing"
        summary = "done" if client.exists(dr.duty_summary_done_key(day)) else "missing"
        stall = "present" if client.exists(dr.stall_check_key(day)) else "missing"
    except Exception as exc:  # noqa: BLE001 读不到本身要报
        logger.warning("[DutyDeadman] trade 域回执读取失败: %s", exc)
        return {"pnl": "unreadable", "summary": "unreadable", "stall": "unreadable"}
    return {"pnl": pnl, "summary": summary, "stall": stall}


def _read_realtime_status(client: Any) -> str:
    """状态键 → ``enabled`` / ``disabled`` / ``unknown`` / ``unreadable``（不触碰 PG）。"""
    if client is None:
        return "unreadable"
    try:
        raw = client.hgetall(_status_key())
    except Exception as exc:  # noqa: BLE001
        logger.warning("[DutyDeadman] 实时推理状态键读取失败: %s", exc)
        return "unreadable"
    if not raw:
        return "unknown"  # 从未运行过：无法证明「该响」
    cfg: Any = {}
    try:
        if raw.get("config"):
            cfg = json.loads(raw["config"])
    except (TypeError, ValueError):
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    return "enabled" if cfg.get("enabled") else "disabled"


async def _evaluate_signal(session: Any, day: date) -> str:
    """PG 里当日实时信号 → ``ok`` / ``stale`` / ``missing`` / ``unreadable``。"""
    if session is None:
        return "unreadable"
    sql = text(
        "SELECT COUNT(*) AS n, MAX(signal_ts) AS last_ts FROM engine_signal_scores "
        "WHERE trade_date = :day AND source = 'realtime'"
    )
    try:
        row = (await session.execute(sql, {"day": day})).fetchone()
    except Exception as exc:  # noqa: BLE001 读不到本身要报
        logger.warning("[DutyDeadman] 实时信号读取失败: %s", exc)
        return "unreadable"
    count = int((row[0] if row else 0) or 0)
    last = row[1] if row else None
    if isinstance(last, str):
        try:
            last = datetime.fromisoformat(last)
        except ValueError:
            last = None
    if isinstance(last, datetime) and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)  # TIMESTAMPTZ；naive 按 UTC 读
    if count == 0 or not isinstance(last, datetime):
        return "missing"
    return "ok" if last.astimezone(TZ).time() >= SIGNAL_CLOSE_THRESHOLD else "stale"


async def _notify(*, title: str, content: str, level: str, qq_alert: bool) -> int:
    """管理员 fanout（QQ 旁路到手机）；投递失败返回 0，绝不抛出。"""
    try:
        from backend.shared import notification_publisher as np

        delivered, _audience = await np.publish_notification_to_admins_async(
            title=title, content=content, type="trading", level=level, qq_alert=qq_alert
        )
        return int(delivered or 0)
    except Exception as exc:  # noqa: BLE001 投递失败不掀翻检查
        logger.warning("[DutyDeadman] 通知投递失败: %s", exc)
        return 0


async def _close_pool_quietly() -> None:
    """best-effort 关闭全局连接池（每 tick 一个 ``asyncio.run``，进出各清一次）。"""
    try:
        await close_database()
    except Exception as exc:  # noqa: BLE001 清理失败绝不影响核对结论
        logger.warning("[DutyDeadman] 连接池清理失败（忽略）: %s", exc)


# ---------- 编排 ----------


async def run_duty_deadman_check(
    *,
    day: date | None = None,
    force: bool = False,
    client_trade: Any = None,
    client_sched: Any = None,
    session: Any = None,
) -> dict[str, Any]:
    """核对一次（beat 每 30 分钟调；``schedule_ctl run duty_deadman`` 手动重跑）。

    ``force``：跳过非交易日闸门与当日封账键（操作员显式覆盖，如周末演练）。
    注入的客户端/会话不 close（调用方所有）；自建的必 close。
    """
    day = day or datetime.now(TZ).date()
    date_str = day.strftime("%Y%m%d")

    from backend.shared.scheduler_registry import heartbeat as _sched_heartbeat

    try:
        _sched_heartbeat("duty_deadman")
    except Exception:  # noqa: BLE001 心跳是旁路
        pass

    if not force and not await _is_trading_day(day):
        return {"date": date_str, "status": "skipped", "reason": "non_trading_day"}

    owned_trade: Any = None
    owned_sched: Any = None
    try:
        if client_trade is None:
            owned_trade = _redis_client(int(os.getenv("REDIS_DB_TRADE", "2")))
            client_trade = owned_trade
        if client_sched is None:
            owned_sched = _redis_client(int(os.getenv("REDIS_DB", "0")))
            client_sched = owned_sched

        if not force:
            try:
                if client_trade.exists(dr.deadman_done_key(day)):
                    return {"date": date_str, "status": "done"}
            except Exception as exc:  # noqa: BLE001 读不到 done ⇒ 继续核对
                logger.warning("[DutyDeadman] done 键读取失败（继续核对）: %s", exc)

        facts = _read_trade_facts(client_trade, day)
        status = _read_realtime_status(client_sched)
        if status in ("disabled", "unknown", "unreadable"):
            facts["realtime"] = status
        elif session is not None:
            facts["realtime"] = await _evaluate_signal(session, day)
        else:
            # celery worker 子进程会跨 tick 复用：池里可能残留绑定**已关闭循环**的
            # 连接（前一任务留下的），checkout 复用时抛「attached to a different
            # loop」——pre_ping 不把它识别为断连、救不了，会被误判成 unreadable。
            # 开跑前与收尾各清一次池（close_database 会置 _initialized=False，
            # 下一次 get_session 在当前循环重建引擎），两个 tick 之间不互相投毒。
            await _close_pool_quietly()
            try:
                async with get_session(read_only=True) as opened:
                    facts["realtime"] = await _evaluate_signal(opened, day)
            except Exception as exc:  # noqa: BLE001 PG 不可用 ⇒ 无法核对，要报
                logger.warning("[DutyDeadman] PG 会话不可用: %s", exc)
                facts["realtime"] = "unreadable"
            finally:
                await _close_pool_quietly()

        missing = evaluate_receipts(facts)

        prev: list[str] = []
        try:
            raw_prev = client_trade.get(dr.deadman_alerted_key(day))
            if raw_prev:
                parsed = json.loads(raw_prev)
                if isinstance(parsed, list):
                    prev = [str(x) for x in parsed]
        except Exception as exc:  # noqa: BLE001 读不到 ⇒ 按「未告警」处理（宁可再响）
            logger.warning("[DutyDeadman] alerted 台账读取失败: %s", exc)

        if not missing:
            recovered = bool(prev)
            if recovered:
                await _notify(
                    title=f"值班死手：回执已补齐 · {day.isoformat()}",
                    content=render_recovery_content(day),
                    level="success",
                    qq_alert=True,
                )
                try:
                    client_trade.delete(dr.deadman_alerted_key(day))
                except Exception:  # noqa: BLE001
                    pass
            try:
                client_trade.set(
                    dr.deadman_done_key(day), "1", ex=dr.DEADMAN_DONE_TTL_SECONDS
                )
            except Exception as exc:  # noqa: BLE001 封账失败下轮会再走一遍（幂等）
                logger.warning("[DutyDeadman] 封账键写入失败: %s", exc)
            logger.info(
                "[DutyDeadman] %s 回执全齐%s",
                date_str,
                "（已发恢复）" if recovered else "",
            )
            return {
                "date": date_str,
                "status": "ok",
                "missing": [],
                "recovered": recovered,
            }

        action = decide_alert(missing, prev)
        if action == "alert":
            delivered = await _notify(
                title=f"值班死手：{len(missing)} 项该响没响 · {day.isoformat()}",
                content=render_alert_content(day, missing, facts),
                level="error",
                qq_alert=True,
            )
            # 投递成功才记「已告警集合」：投递失败 ≠ 已通知，下轮（集合未变）再试
            if delivered > 0:
                try:
                    client_trade.set(
                        dr.deadman_alerted_key(day),
                        json.dumps(missing, ensure_ascii=False),
                        ex=dr.DEADMAN_DONE_TTL_SECONDS,
                    )
                except Exception as exc:  # noqa: BLE001 写不进去 ⇒ 下轮可能再响（宁可多响）
                    logger.warning("[DutyDeadman] alerted 台账写入失败: %s", exc)
        logger.warning(
            "[DutyDeadman] %s 缺失 %d 项（%s）: %s",
            date_str,
            len(missing),
            action,
            ",".join(missing),
        )
        return {
            "date": date_str,
            "status": "alerted" if action == "alert" else "pending",
            "missing": missing,
            "action": action,
        }
    finally:
        for owned in (owned_trade, owned_sched):
            if owned is not None:
                try:
                    owned.close()
                except Exception:  # noqa: BLE001
                    pass
