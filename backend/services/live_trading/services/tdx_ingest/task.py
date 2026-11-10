"""通达信桥采集档位主循环（trade 常驻任务；call_auction_watch 同款骨架）。

档位表见 :mod:`datasets`（15:31 / 15:45 / 16:45 / 17:20 / 17:30 / 23:50 /
次日 06:30）。纪律：

* **目标交易日制**：到点后对「目标交易日」采集一次；06:30 档目标 = 上一
  交易日（早间补采夜间才发布齐的数据）。done/attempts 按 ``slot@目标日``
  记账，进程内保存（重启即失——重跑幂等，天然自愈）。
* **宽限 4h**：档位到点后 4 小时内有效（进程重启/桥短暂重启不丢档）；
  超宽限不追（历史缺口走回填 CLI）。
* **单数据集失败不阻断同档**；**整档全败/单集 error 告警一次**（按
  ``slot@目标日`` 去重，48h TTL）；``blocked``（数据包家族）与 ``suspended``
  （挂起数据集）**不告警不重试**——那是环境前置，不是故障。
* **rate_limited 不告警**：令牌桶退避 + 降速是自愈路径；烧完 MAX 次后在
  下个档期自然重try。
* 心跳随 tick 写（≤10min 一次，TTL 1h）；状态 Hash ``qm:tdx_ingest:status``
  （db2）+ blocked 键供面板/路由读。
* 手动触发：Redis 键 ``qm:tdx_ingest:run_once:{dataset}``（认领即删除），
  值 = ``YYYYMMDD`` 或 ``{"target": ..., "force": true}``。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta

from backend.services.live_trading.services.tdx_ingest import sinks
from backend.services.live_trading.services.tdx_ingest.bridge_client import (
    BridgeError,
    TdxBridgeClient,
)
from backend.services.live_trading.services.tdx_ingest.datasets import (
    DATASETS,
    SLOT_TARGET_OFFSET,
    DatasetSpec,
    all_slots,
    datasets_for_slot,
)
from backend.services.live_trading.services.tdx_ingest.fetchers import (
    FetchContext,
    fetch_dataset,
)
from backend.services.live_trading.services.tdx_ingest.rate_budget import RateBudget
from backend.services.live_trading.services.trading_session import TZ
from backend.services.trade.services.daily_pnl_report_task import (
    _env_bool,
    _is_trading_day,
    _redis_client,
)

logger = logging.getLogger(__name__)

#: 调度注册表 job key（心跳 + schedule_ctl 重跑面）。
TASK_KEY = "tdx_ingest"

STATUS_KEY = "qm:tdx_ingest:status"
BLOCKED_KEY_FMT = "qm:tdx_ingest:blocked:{key}"
TRIGGER_KEY_FMT = "qm:tdx_ingest:run_once:{key}"
ALERT_KEY_FMT = "qm:tdx_ingest:alerted:{slot}:{target}"

#: 轮询间隔（秒）。
TICK_S = 60.0

#: 档位宽限（秒）：到点后多久内仍补跑（过期不追，走回填）。
SLOT_GRACE_S = 4 * 3600

#: 单档最大尝试次数（跨 tick 累计；到数放弃当日并告警）。
MAX_ATTEMPTS_PER_SLOT = 3

#: 心跳间隔（秒）与状态键 TTL。
HEARTBEAT_INTERVAL_S = 600
STATUS_TTL_S = 2 * 86400
BLOCKED_TTL_S = 7 * 86400
ALERT_TTL_S = 2 * 86400

#: 数据完整性无关的「终结态」：到这些状态即视为本档完成，不再重试。
_TERMINAL_STATUSES = frozenset({"ok", "partial", "suspended", "blocked"})

#: 进程内状态镜像（面板/路由经 Redis Hash 读；重启即重建）。
_STATUS: dict[str, dict] = {}


# ============ 纯函数（可单测） ============


def slot_due_at(now: datetime, slot: str) -> datetime:
    """今日该档的到点时刻（now 同 tz）。"""
    hh, mm = (int(x) for x in slot.split(":", 1))
    return now.replace(hour=hh, minute=mm, second=0, microsecond=0)


def due_slots(now: datetime) -> list[str]:
    """当前到点的档（宽限内）——时间纯函数。

    done/attempts 的键是 ``slot@目标日``，目标日解析是异步的（交易日历），
    所以到点筛选与「是否已做」判断分离：调用方对每个候选档解析目标日后
    再查 done/attempts。
    """
    out: list[str] = []
    for slot in all_slots():
        due = slot_due_at(now, slot)
        if now < due:
            continue
        if (now - due).total_seconds() > SLOT_GRACE_S:
            continue
        out.append(slot)
    return out


def next_slot_at(now: datetime) -> datetime:
    """下一档到点时刻（今日全过 → 明日首档）。仅用于状态展示。"""
    slots = sorted(all_slots())
    for slot in slots:
        due = slot_due_at(now, slot)
        if now < due:
            return due
    return slot_due_at(now + timedelta(days=1), slots[0])


def parse_trigger(raw: str | None) -> tuple[str, bool]:
    """触发键值 → ``(target, force)``；target 空串 = 用默认目标日。"""
    if not raw:
        return "", False
    text = str(raw).strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
            target = str(data.get("target") or "").strip()
            return target, bool(data.get("force"))
        except Exception:  # noqa: BLE001 - 坏 JSON 当纯日期试
            pass
    return text, False


# ============ 目标日与交易日 ============


async def resolve_target(slot: str, now: datetime) -> str | None:
    """档位 → 目标交易日 ``YYYYMMDD``（06:30 档 = 上一交易日）。"""
    offset = SLOT_TARGET_OFFSET.get(slot, 0)
    if offset >= 0:
        return (now.date() + timedelta(days=offset)).strftime("%Y%m%d")
    try:
        from backend.shared.trading_calendar import calendar_service

        prev = await calendar_service.prev_trading_day(
            market="SSE", trade_date=now.date(), tenant_id="default", user_id="0"
        )
        if isinstance(prev, str):
            return prev.replace("-", "")[:8]
        if isinstance(prev, datetime):
            prev = prev.date()
        return prev.strftime("%Y%m%d")
    except Exception as exc:  # noqa: BLE001 - 日历不可用按周末近似
        logger.warning("[TdxIngest] 上一交易日解析失败（按周末近似）: %s", exc)
        d = now.date() - timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return d.strftime("%Y%m%d")


# ============ Redis（db2；同步客户端统一 to_thread） ============


async def _redis_do(fn, *args, **kwargs):
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - 状态面失败不阻断采集
        logger.warning(
            "[TdxIngest] Redis 操作失败 %s: %s", getattr(fn, "__name__", fn), exc
        )
        return None


async def _hset_status(client, mapping: dict) -> None:
    if client is None:
        return
    await _redis_do(client.hset, STATUS_KEY, mapping=mapping)
    await _redis_do(client.expire, STATUS_KEY, STATUS_TTL_S)


async def _set_blocked(client, spec: DatasetSpec, message: str) -> None:
    if client is None:
        return
    await _redis_do(
        client.set,
        BLOCKED_KEY_FMT.format(key=spec.key),
        message[:500],
        ex=BLOCKED_TTL_S,
    )


async def _clear_blocked(client, spec: DatasetSpec) -> None:
    if client is None:
        return
    await _redis_do(client.delete, BLOCKED_KEY_FMT.format(key=spec.key))


async def _alert_once(client, slot: str, target: str, text: str) -> None:
    """按 ``slot@目标日`` 去重的告警（SETNX 成功才发；Redis 坏则静默跳过）。"""
    key = ALERT_KEY_FMT.format(slot=slot, target=target)
    claimed = None
    if client is not None:
        claimed = await _redis_do(client.set, key, "1", ex=ALERT_TTL_S, nx=True)
    if client is not None and not claimed:
        return
    try:
        from backend.shared.qq_notify import alert_async

        alert_async(
            level="warning",
            title=f"通达信桥采集异常（{slot} 档 / {target}）",
            content=text,
            alert_type="data_quality",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[TdxIngest] 告警发送失败: %s", exc)


async def _claim_triggers(client) -> list[tuple[DatasetSpec, str, bool]]:
    """认领手动触发键（GET+DEL；值见模块 docstring）。"""
    if client is None:
        return []
    claimed: list[tuple[DatasetSpec, str, bool]] = []
    for spec in DATASETS:
        key = TRIGGER_KEY_FMT.format(key=spec.key)
        raw = await _redis_do(client.get, key)
        if raw is None:
            continue
        await _redis_do(client.delete, key)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        target, force = parse_trigger(raw)
        claimed.append((spec, target, force))
    return claimed


# ============ 单数据集一轮 ============


async def run_dataset(
    spec: DatasetSpec,
    *,
    slot: str,
    target_date: str,
    client: TdxBridgeClient,
    budget: RateBudget,
    redis_client,
    force: bool = False,
) -> dict:
    """执行单数据集一轮并落台账/水位/状态；返回结果字典。"""
    started = time.monotonic()
    status = "ok"
    error = ""
    written = archived = 0
    archive_path = ""
    calls = 0

    if not spec.enabled and not force:
        status = "suspended"
        error = spec.suspended_reason
        logger.info("[TdxIngest] %s 挂起跳过（%s）", spec.key, error)
    else:
        ctx = FetchContext(client=client, budget=budget, target_date=target_date)
        try:
            rows = await fetch_dataset(spec, ctx)
            calls = ctx.calls
            if ctx.failures:
                logger.warning(
                    "[TdxIngest] %s 软失败 %d 笔（首条: %s）",
                    spec.key,
                    len(ctx.failures),
                    ctx.failures[0],
                )
            if spec.require_nonempty and not rows and not ctx.failures:
                # 市场级序列（SC 家族）空集不是合法结果：桥 ErrorId=0 + Value=null
                # 说明客户端没有这份专业数据。记 blocked（面板可见），不要写成 ok。
                status = "blocked"
                error = "桥返回空：市场级数据包/权限缺失（require_nonempty）"
                logger.warning("[TdxIngest] %s 返回空集 → blocked: %s", spec.key, error)
            else:
                written = await sinks.upsert_rows(spec, rows)
                try:
                    archive_path, archived = await sinks.write_archive(
                        spec, rows, target_date
                    )
                except Exception as exc:  # noqa: BLE001 - PG 已成，归档失败记 partial
                    status = "partial"
                    error = f"归档失败: {exc}"
                    logger.warning("[TdxIngest] %s 归档失败: %s", spec.key, exc)
                if ctx.failures and not error:
                    error = f"软失败 {len(ctx.failures)} 笔: {ctx.failures[0]}"
        except BridgeError as exc:
            calls = ctx.calls
            if exc.kind == "blocked":
                status = "blocked"
            elif exc.kind == "rate_limited":
                status = "rate_limited"
            else:
                status = "error"
            error = str(exc)
            logger.warning("[TdxIngest] %s 失败(%s): %s", spec.key, exc.kind, exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 单集异常不炸整档
            calls = ctx.calls
            status = "error"
            error = f"{type(exc).__name__}: {exc}"
            logger.warning("[TdxIngest] %s 异常: %s", spec.key, exc, exc_info=True)

    duration_ms = int((time.monotonic() - started) * 1000)

    if status == "blocked":
        await _set_blocked(redis_client, spec, error)
    elif status in ("ok", "partial"):
        await _clear_blocked(redis_client, spec)

    await sinks.record_run(
        dataset=spec.key,
        slot=slot,
        target_date=target_date,
        status=status,
        rows_written=written,
        rows_archived=archived,
        calls=calls,
        duration_ms=duration_ms,
        error=error,
    )
    await sinks.update_cursor(
        spec.key,
        "daily",
        status=status,
        last_target_date=target_date,
        rows_delta=written,
        error=error,
    )

    result = {
        "key": spec.key,
        "status": status,
        "rows_written": written,
        "rows_archived": archived,
        "calls": calls,
        "duration_ms": duration_ms,
        "error": error,
        "archive": archive_path,
        "done": status in _TERMINAL_STATUSES,
    }
    _STATUS[spec.key] = {
        "status": status,
        "at": datetime.now(TZ).isoformat(timespec="seconds"),
        "slot": slot,
        "target": target_date,
        "rows": written,
        "calls": calls,
        "duration_ms": duration_ms,
        "error": error[:300],
    }
    await _hset_status(
        redis_client,
        {
            "ts": datetime.now(TZ).isoformat(timespec="seconds"),
            "datasets": json.dumps(_STATUS, ensure_ascii=False),
            "budget": json.dumps(
                {"base": budget.base_rate, "effective": budget.effective_rate},
                ensure_ascii=False,
            ),
        },
    )
    return result


# ============ 档位 ============


async def run_slot(
    slot: str,
    target_date: str,
    *,
    client: TdxBridgeClient,
    budget: RateBudget,
    redis_client,
    force_keys: set[str] | None = None,
) -> bool:
    """跑一个档位的全部数据集；返回「整档是否已终结」。"""
    specs = datasets_for_slot(slot)
    results: dict[str, str] = {}
    for spec in specs:
        res = await run_dataset(
            spec,
            slot=slot,
            target_date=target_date,
            client=client,
            budget=budget,
            redis_client=redis_client,
            force=bool(force_keys and spec.key in force_keys),
        )
        results[spec.key] = res["status"]
    logger.info("[TdxIngest] %s 档（目标 %s）完成: %s", slot, target_date, results)

    errors = {k: v for k, v in results.items() if v == "error"}
    if errors:
        await _alert_once(
            redis_client,
            slot,
            target_date,
            "数据集失败: "
            + "、".join(f"{k}" for k in errors)
            + "（详见状态面板/日志）",
        )
    await _hset_status(
        redis_client,
        {
            "slot_last": json.dumps(
                {
                    "slot": slot,
                    "target": target_date,
                    "results": results,
                    "at": datetime.now(TZ).isoformat(timespec="seconds"),
                },
                ensure_ascii=False,
            )
        },
    )
    return all(v in _TERMINAL_STATUSES for v in results.values())


async def run_trigger(
    spec: DatasetSpec,
    target: str,
    force: bool,
    *,
    client: TdxBridgeClient,
    budget: RateBudget,
    redis_client,
) -> dict:
    """手动触发一轮（target 空 → 今日；非交易日回退上一交易日）。

    显式触发一律视同 ``force``（挂起数据集如 zdt 也可单跑复测）——``force``
    参数保留在签名里为将来「只读触发」留口，当前不改变行为。
    """
    now = datetime.now(TZ)
    if target and not (len(target) == 8 and target.isdigit()):
        logger.warning("[TdxIngest] 触发目标日非法（%r），改用默认目标日", target)
        target = ""
    if not target:
        if await _is_trading_day(now.date()):
            target = now.date().strftime("%Y%m%d")
        else:
            resolved = await resolve_target("06:30", now)
            target = resolved or now.date().strftime("%Y%m%d")
    return await run_dataset(
        spec,
        slot="manual",
        target_date=target,
        client=client,
        budget=budget,
        redis_client=redis_client,
        force=True,
    )


async def run_rerun_cycle(target_date: str, *, force: bool = False) -> dict[str, dict]:
    """手动重跑面（schedule_ctl / admin 触发共用）：全数据集各跑一轮。

    顺序执行、令牌桶照常生效；``force=True`` 时挂起数据集（zdt）也单跑
    （盘中复测用）。返回 ``{key: result}``（含 error 的由调用方判失败）。
    """
    client = TdxBridgeClient()
    redis_client = None
    try:
        from backend.services.trade_shared.deps import get_redis

        redis_client = _redis_client(get_redis())
    except Exception as exc:  # noqa: BLE001 - 状态面降级，采集本身照跑
        logger.warning("[TdxIngest] Redis 不可用（状态面降级）: %s", exc)
    budget = RateBudget(redis_client)
    out: dict[str, dict] = {}
    try:
        for spec in DATASETS:
            out[spec.key] = await run_dataset(
                spec,
                slot="manual",
                target_date=target_date,
                client=client,
                budget=budget,
                redis_client=redis_client,
                force=force,
            )
    finally:
        await client.aclose()
    return out


# ============ 主循环 ============


async def run_tdx_ingest_task() -> None:
    """常驻循环：档位到点采集；手动触发键随 tick 认领。"""
    if not _env_bool("TDX_INGEST_ENABLED", True):
        logger.info("[TdxIngest] 任务关闭（TDX_INGEST_ENABLED=0）")
        return

    from backend.shared.scheduler_registry import heartbeat as _sched_heartbeat

    client = TdxBridgeClient()
    redis_client = None
    try:
        from backend.services.trade_shared.deps import get_redis

        redis_client = _redis_client(get_redis())
    except Exception as exc:  # noqa: BLE001 - 状态面降级，采集本身照跑
        logger.warning("[TdxIngest] Redis 不可用（状态面降级）: %s", exc)

    budget = RateBudget(redis_client)
    done_day = ""
    done: set[str] = set()
    attempts: dict[str, int] = {}
    last_heartbeat = 0.0

    logger.info(
        "[TdxIngest] 采集任务启动：档位 %s（预算 %d/min）",
        "、".join(all_slots()),
        budget.effective_rate,
    )
    try:
        while True:
            try:
                now = datetime.now(TZ)
                day = now.strftime("%Y%m%d")
                if day != done_day:
                    done_day, done, attempts = day, set(), {}

                for slot in due_slots(now):
                    target = await resolve_target(slot, now)
                    if target is None:
                        continue
                    key = f"{slot}@{target}"
                    if key in done:
                        continue
                    if not await _is_trading_day(
                        datetime.strptime(target, "%Y%m%d").date()
                    ):
                        done.add(key)  # 目标日非交易日：本档作罢
                        continue
                    attempts[key] = attempts.get(key, 0) + 1
                    ok = await run_slot(
                        slot,
                        target,
                        client=client,
                        budget=budget,
                        redis_client=redis_client,
                    )
                    if ok:
                        done.add(key)
                    elif attempts[key] >= MAX_ATTEMPTS_PER_SLOT:
                        logger.warning(
                            "[TdxIngest] %s 尝试 %d 次仍未终结，本档放弃",
                            key,
                            attempts[key],
                        )
                        done.add(key)

                for spec, target, force in await _claim_triggers(redis_client):
                    try:
                        await run_trigger(
                            spec,
                            target,
                            force,
                            client=client,
                            budget=budget,
                            redis_client=redis_client,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "[TdxIngest] 手动触发 %s 失败: %s", spec.key, exc
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 常驻循环不因单圈异常退出
                logger.warning("[TdxIngest] 循环异常: %s", exc, exc_info=True)

            mono = time.monotonic()
            if mono - last_heartbeat >= HEARTBEAT_INTERVAL_S:
                try:
                    _sched_heartbeat("tdx_ingest")
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[TdxIngest] 心跳写入失败: %s", exc)
                last_heartbeat = mono
            await asyncio.sleep(TICK_S)
    finally:
        await client.aclose()


__all__ = [
    "ALERT_KEY_FMT",
    "BLOCKED_KEY_FMT",
    "STATUS_KEY",
    "TASK_KEY",
    "TRIGGER_KEY_FMT",
    "due_slots",
    "next_slot_at",
    "parse_trigger",
    "resolve_target",
    "run_dataset",
    "run_rerun_cycle",
    "run_slot",
    "run_tdx_ingest_task",
    "slot_due_at",
]
