"""滚动重训调度器（P1 · 设计文档《滚动训练与模型生命周期》§4.2）。

beat 每分钟 tick：读 Redis 配置（``quantmind:retrain_schedule:{market}``）→ 到点判定
（日规则 ∩ 交易日历 ∩ 本期未派发）→ 内存守卫 → busy 探测 → HTTP POST 内部端点
``/api/v1/internal/rolling/dispatch``。**真正的训练提交发生在 API 进程**——celery
worker 无 docker.sock，训练容器只能由 API 进程的编排器启动；本模块绝不 import
``execute_dispatch`` 直接提交（那是第二套提交路径）。

mark-after-dispatch：只有端点回 2xx 且 status ∈ {dispatched, duplicate} 才写
last_run 标记（例外：duplicate 且 campaign_status=planned——那是崩溃残留、窗口
并未被真实受理，照「不记账、下一 tick 重试」处理）；busy / 数据未就绪 / HTTP
失败一律不写 → 下一 tick 重试（验收 ②）。
due 判定用 ``now >= due_moment`` 而非时刻等号（对比 market_sync 的 ``==``）：首交易日
15:30 那分钟被训练占用时，同一 due 期内的后续任意 tick 仍然判 due，天然补发。
标记键按 due_date 记账（``quantmind:retrain_schedule_last_run:{market}:{due_date}``），
TTL 35 天——整个 due 期（当月）盖住，且跨月换键，上月的标记绝不会吞掉下月的派发。

与文档 §4.2 的一处刻意偏差：**心跳每 tick 都写**（不是只在 2xx 时写）。C07 体检按
心跳新鲜度判活；月度任务若只在派发成功那一刻写一次 1800s 心跳，一个月里有 29 天
会被误报 stale——「tick 进程还活着」才是这条心跳的语义（与市场同步派发同款）。
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from datetime import date, datetime
from typing import Any

from backend.services.engine.training.rolling_dispatch import (
    TRIGGER_SCHEDULE,
    alert_once,
    mem_guard,
    probe_busy,
)

logger = logging.getLogger(__name__)

_SCHEDULE_KEY = "quantmind:retrain_schedule:{market}"
_LAST_RUN_KEY = "quantmind:retrain_schedule_last_run:{market}:{due_date}"
#: 35 天：due 期 = 当月，标记必须活到月末；跨月 due_date 换键，不会吞下月的派发。
_LAST_RUN_TTL_SECONDS = 35 * 24 * 3600
_DISPATCH_PATH = "/api/v1/internal/rolling/dispatch"

#: 每市场调度配置（设计文档 §4.2 样例）；未配置 = enabled:false（与 market_sync
#: 同一哲学：不内置默认调度，是否自动重训一律以用户保存的配置为准）。
DEFAULT_SCHEDULE: dict[str, Any] = {
    "enabled": False,
    "day_rule": "first_trading_day",
    "time": "15:30",
    "recipe_id": "cn_nativetft_base",
    "window_policy": None,
    "purge_days": None,
    "observation_days": 20,
    "max_time_minutes": 240,
    "executor": "local",
    "last_run": None,
}

VALID_DAY_RULES = ("first_trading_day",)
#: executor 词表。remote 的节点路由尚未接线：PUT 400 拒绝 + 本模块按 local 兜底
#: （直写 Redis 的存量配置保留内存守卫并告警）。
VALID_EXECUTORS = ("local", "remote")


def _redis():
    import redis

    return redis.from_url(
        os.getenv("REDIS_URL", "redis://redis:6379/0"), socket_timeout=3
    )


def recipe_markets() -> list[str]:
    """有有效配方的市场集合（调度逐市场检查的名单来源，不硬编码市场表）。"""
    from backend.shared.training.recipe_registry import list_recipes

    markets = {
        str(recipe.get("market") or "").strip().upper()
        for recipe in list_recipes()
        if recipe.get("valid")
    }
    return sorted(m for m in markets if m)


def _normalize(cfg: dict[str, Any] | None) -> dict[str, Any]:
    out = dict(DEFAULT_SCHEDULE)
    for key in out:
        if key in (cfg or {}):
            out[key] = cfg[key]
    # time 非法回退默认档（与 market_sync 同款）：时刻配置坏掉不该静默取消月度重训
    t = str(out["time"] or "").strip()
    try:
        datetime.strptime(t, "%H:%M")
        out["time"] = t
    except ValueError:
        out["time"] = DEFAULT_SCHEDULE["time"]
    # day_rule **不做回退**：未知规则必须让 judge_due 拒绝并告警——把乱码静默
    # 当成「每月首交易日自动重训」是配置事故，不是容错。
    out["day_rule"] = str(out["day_rule"] or "").strip().lower()
    out["enabled"] = bool(out["enabled"])
    out["recipe_id"] = str(out["recipe_id"] or "").strip()
    out["executor"] = str(out["executor"] or "local").strip().lower()
    for key, fallback in (("observation_days", 20), ("max_time_minutes", 240)):
        try:
            out[key] = int(out[key])
        except (TypeError, ValueError):
            out[key] = fallback
    return out


def get_schedule(market: str, *, redis_client: Any = None) -> dict[str, Any]:
    client = redis_client if redis_client is not None else _redis()
    raw = client.get(_SCHEDULE_KEY.format(market=market))
    return _normalize(json.loads(raw) if raw else None)


def save_schedule(market: str, cfg: dict[str, Any], *, redis_client: Any = None) -> dict[str, Any]:
    normalized = _normalize(cfg)
    client = redis_client if redis_client is not None else _redis()
    client.set(
        _SCHEDULE_KEY.format(market=market),
        json.dumps(normalized, ensure_ascii=False),
    )
    return normalized


def get_all_schedules(*, redis_client: Any = None) -> dict[str, dict[str, Any]]:
    client = redis_client if redis_client is not None else _redis()
    return {m: get_schedule(m, redis_client=client) for m in recipe_markets()}


def last_run_key(market: str, due_date: date) -> str:
    return _LAST_RUN_KEY.format(market=market, due_date=due_date.isoformat())


def _last_run_marked(client: Any, market: str, due_date: date) -> bool:
    return bool(client.exists(last_run_key(market, due_date)))


def _mark_last_run(client: Any, market: str, due_date: date) -> None:
    client.set(last_run_key(market, due_date), "1", ex=_LAST_RUN_TTL_SECONDS)


# ─────────────────────────────────────────────────────────────────────────────
# 到点判定（纯函数）
# ─────────────────────────────────────────────────────────────────────────────


def judge_due(
    *,
    day_rule: str,
    month_sessions: list[date] | None,
    today: date,
    now_hm: str,
    cfg_time: str,
) -> dict[str, Any]:
    """到点判定：``{"due": bool, "due_date": date | None, "reason": str}``。

    due 期 = 本月首个交易日（``first_trading_day``）。``now >= due_moment`` 语义：
    today > due_date 即视为已过时刻（catch-up），today == due_date 时比 HH:MM。
    """
    rule = str(day_rule or "").strip().lower()
    if rule not in VALID_DAY_RULES:
        return {"due": False, "due_date": None, "reason": "unknown_day_rule"}
    if month_sessions is None:
        return {"due": False, "due_date": None, "reason": "calendar_unavailable"}
    if not month_sessions:
        return {"due": False, "due_date": None, "reason": "no_session_in_month"}
    due_date = month_sessions[0]
    if today < due_date:
        return {"due": False, "due_date": due_date, "reason": "not_due_yet"}
    if today == due_date and str(now_hm) < str(cfg_time):
        return {"due": False, "due_date": due_date, "reason": "before_time"}
    return {"due": True, "due_date": due_date, "reason": "due"}


# ─────────────────────────────────────────────────────────────────────────────
# IO 边界（薄封装，测试可整层 monkeypatch）
# ─────────────────────────────────────────────────────────────────────────────


def _month_sessions(calendar_market: str, today: date) -> list[date] | None:
    from backend.shared.trading_calendar import trading_days_xcal

    return trading_days_xcal(calendar_market, today.replace(day=1), today)


def _busy_probe(redis_client: Any) -> dict[str, Any]:
    """同步桥：probe_busy 走 asyncpg。事件循环内被误调 → 保守按 busy。"""
    import asyncio

    try:
        return asyncio.run(probe_busy(redis_client))
    except RuntimeError:
        return {"busy": True, "reason": "busy_probe_failed", "detail": "running event loop"}


def _post_dispatch(body: dict[str, Any], *, timeout: int = 30) -> dict[str, Any]:
    """同步 POST 内部端点（urllib，与 backend/scripts/rolling_train.py 同款）。

    返回 ``{"ok": True, "status_code", "body"}`` 或 ``{"ok": False, ...}``——
    一律不抛：调度 tick 的失败记账（不写 last_run）在调用方。
    """
    from backend.shared.auth import get_internal_call_secret
    from backend.shared.training_runtime import default_api_base_url

    secret = get_internal_call_secret()
    if not secret:
        return {"ok": False, "error": "internal_call_secret_missing"}
    url = default_api_base_url().rstrip("/") + _DISPATCH_PATH
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Internal-Call-Secret": secret,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return {
                "ok": True,
                "status_code": response.status,
                "body": json.loads(response.read().decode("utf-8")),
            }
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            detail: Any = json.loads(raw)
        except ValueError:
            detail = raw
        return {"ok": False, "status_code": exc.code, "error": detail}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "error": f"无法连接 {url}: {exc}"}


# ─────────────────────────────────────────────────────────────────────────────
# tick 主流程
# ─────────────────────────────────────────────────────────────────────────────


def _dispatch_one(
    market: str,
    *,
    today: date,
    now_hm: str,
    force: bool,
    client: Any,
) -> dict[str, Any]:
    cfg = get_schedule(market, redis_client=client)
    if not cfg["enabled"] and not force:
        return {"market": market, "status": "disabled"}

    recipe_id = cfg["recipe_id"]
    try:
        from backend.shared.training.recipe_registry import load_recipe

        recipe = load_recipe(recipe_id)
    except Exception as exc:  # noqa: BLE001 - 坏配方不拖垮其他市场
        alert_once(
            f"sched:config:{market}",
            "滚动重训：配方不可用",
            f"market={market} recipe={recipe_id}: {exc}",
            client,
        )
        return {"market": market, "status": "recipe_invalid", "error": str(exc)}
    if recipe.market.upper() != market.upper():
        alert_once(
            f"sched:config:{market}",
            "滚动重训：配方市场不匹配",
            f"调度 market={market} 指向配方 {recipe_id}（属于 {recipe.market}），已跳过。",
            client,
        )
        return {"market": market, "status": "market_mismatch"}

    sessions = _month_sessions(recipe.calendar_market, today)
    verdict = judge_due(
        day_rule=cfg["day_rule"],
        month_sessions=sessions,
        today=today,
        now_hm=now_hm,
        cfg_time=cfg["time"],
    )
    due_date = verdict.get("due_date") or today
    if not force:
        if not verdict["due"]:
            reason = verdict["reason"]
            if reason in ("unknown_day_rule", "calendar_unavailable"):
                alert_once(
                    f"sched:{reason}:{market}",
                    "滚动重训：调度配置/日历不可用",
                    f"market={market} reason={reason} day_rule={cfg['day_rule']}；本轮不派发。",
                    client,
                )
            return {"market": market, "status": f"not_due:{reason}"}
        if _last_run_marked(client, market, due_date):
            return {
                "market": market,
                "status": "already_run",
                "due_date": due_date.isoformat(),
            }

    if str(cfg.get("executor") or "local") != "local":
        # executor=remote 尚未接线：派发 payload 不带 node_id，实际仍本地执行
        # （PUT 已 400 拒绝；这里兜底直写 Redis 的存量配置）。不解除内存守卫——
        # 「以为远程、实际本地」再跳过守卫，就是在 44G 宿主上裸奔 OOM。
        alert_once(
            f"sched:remote:{market}",
            "滚动重训：executor=remote 尚未接线",
            f"market={market} executor={cfg.get('executor')}；按 local 执行并保留内存守卫。",
            client,
        )
    mem = mem_guard()
    if not mem.get("ok"):
        alert_once(
            f"sched:mem:{market}",
            "滚动重训：宿主内存不足",
            f"market={market} MemAvailable={mem.get('available_gb')}G "
            f"< {mem.get('min_gb')}G；本轮跳过，下一 tick 重试。",
            client,
        )
        return {"market": market, "status": "low_memory", "detail": mem}

    busy = _busy_probe(client)
    if busy.get("busy"):
        alert_once(
            f"sched:busy:{market}",
            "滚动重训：训练占位，本轮跳过",
            f"market={market} detail={busy}；不写标记，下一 tick 自动补发。",
            client,
        )
        return {"market": market, "status": "busy", "detail": busy}

    posted = _post_dispatch(
        {
            "market": market,
            "recipe_id": recipe.recipe_id,
            "trigger": TRIGGER_SCHEDULE,
            "dry_run": False,
        }
    )
    if not posted.get("ok"):
        alert_once(
            f"sched:http:{market}",
            "滚动重训：内部端点调用失败",
            f"market={market} detail={posted}；不写标记，下一 tick 重试。",
            client,
        )
        return {"market": market, "status": "http_error", "detail": posted}

    body = posted.get("body") or {}
    status = str(body.get("status") or "")
    if status == "duplicate" and str(body.get("campaign_status") or "") == "planned":
        # 「duplicate + planned」= 台账里是未推进的 planned 行（上次派发在提交前
        # 死亡），不是已受理：不写 last_run、下一 tick 继续问，直到该行转
        # dispatched（正常秒级）或超 30 分钟被判陈旧、reopen 补发。若按普通
        # duplicate 记账，整个 due 期会再无声息（审查 F1 的放大器）。
        logger.info(
            "[RetrainSchedule] %s due=%s duplicate(planned) 未真实受理，暂不记账",
            market,
            due_date.isoformat(),
        )
        return {
            "market": market,
            "status": "planned_pending",
            "campaign_id": body.get("campaign_id"),
            "anchor_date": body.get("anchor_date"),
        }
    if status in ("dispatched", "duplicate"):
        _mark_last_run(client, market, due_date)
        logger.info(
            "[RetrainSchedule] %s due=%s 端点回 %s campaign=%s run=%s",
            market,
            due_date.isoformat(),
            status,
            body.get("campaign_id"),
            body.get("run_id"),
        )
        return {
            "market": market,
            "status": status,
            "due_date": due_date.isoformat(),
            "campaign_id": body.get("campaign_id"),
            "run_id": body.get("run_id"),
            "anchor_date": body.get("anchor_date"),
        }
    if status == "skipped":
        reason = str(body.get("reason") or "")
        # 数据未就绪等由端点按日告警；busy 只有调度侧知道要喊（端点的 busy 分支不告警）
        if reason == "busy":
            # 独立去重键：与探针型 busy（sched:busy）分开，同日互不吞并
            alert_once(
                f"sched:busy_submit:{market}",
                "滚动重训：提交时训练占位",
                f"market={market}（端点 409/占用）；不写标记，下一 tick 自动补发。",
                client,
            )
        return {
            "market": market,
            "status": f"skipped:{reason}",
            "detail": body.get("detail"),
        }
    alert_once(
        f"sched:response:{market}",
        "滚动重训：端点返回未知状态",
        f"market={market} body={str(body)[:300]}；不写标记，下一 tick 重试。",
        client,
    )
    return {"market": market, "status": f"unexpected:{status}"}


def dispatch_due_retrains(
    *,
    now: datetime | None = None,
    markets: list[str] | None = None,
    force: bool = False,
    redis_client: Any = None,
) -> dict[str, Any]:
    """逐市场检查滚动重训调度，到点且未派发则 POST 内部端点。

    ``force=True``（CLI 重跑）：不看 enabled/到点/本期标记，仍走内存/busy/数据就绪
    守卫——「重跑」是立刻试一轮，不是绕过安全闸。单个市场失败不拖垮其他市场
    （与 dispatch_due_syncs 同纪律）。
    """
    now_dt = now if now is not None else datetime.now()
    today = now_dt.date()
    now_hm = now_dt.strftime("%H:%M")
    client = redis_client if redis_client is not None else _redis()

    results: list[dict[str, Any]] = []
    for market in markets if markets is not None else recipe_markets():
        try:
            results.append(
                _dispatch_one(
                    market, today=today, now_hm=now_hm, force=force, client=client
                )
            )
        except Exception as exc:  # noqa: BLE001 - 单市场异常不拖垮同 tick 其他市场
            logger.exception("[RetrainSchedule] %s 检查失败: %s", market, exc)
            results.append({"market": market, "status": "error", "error": str(exc)})

    dispatched = [
        r for r in results if r.get("status") in ("dispatched", "duplicate")
    ]
    return {
        "now": now_dt.isoformat(timespec="seconds"),
        "dispatched": dispatched,
        "results": results,
    }
