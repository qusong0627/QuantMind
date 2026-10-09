"""滚动训练 campaign 台账（P1 · 设计文档《滚动训练与模型生命周期》§4.2/§4.6）。

campaign = 「某配方在某锚定日的一次滚动重训」的完整生命周期记录：
``planned`` →（提交训练）``dispatched`` →（run 终态回流）``registered`` / ``failed``，
另有 ``skipped``（数据未就绪等可重试原因）。表 ``qm_rolling_campaigns``，
``UNIQUE(market, recipe_id, anchor_date, trigger)`` 保证同一窗口同一触发只训一次。

幂等纪律（与验收口径 ③「kill -9 后不得永久 running」配套）：
- 派发前先落 ``planned`` 行，提交成功才 ``mark_dispatched(run_id)``；
- 自动路径发现既有行时走 :func:`decide_redispatch` **纯函数**裁决，绝不盲目重训；
- 崩溃残留（planned 且最后状态迁移超时）由 ``planned_is_stale`` 识别 → reopen
  重开补发——否则「提交前进程死亡」的窗口会永久卡死整月派发；
- run 终态经 :func:`mark_outcome_by_run` 回流（``complete_training_run`` 与
  ``job_reaper._mark_orphaned`` 各一处），均 best-effort——台账写失败不能拖垮
  训练回调本身，但要吼日志。

时间列一律 DB 端 ``NOW()``（TIMESTAMPTZ）：训练链路的多个进程共用 PG 一个时钟，
避免容器间时钟漂移（本仓实测容器时钟可慢 1h）造成 dispatched_at 与 run 时间倒挂。
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Any

from sqlalchemy import text

from backend.shared.database_manager_v2 import get_session

logger = logging.getLogger(__name__)

STATUS_PLANNED = "planned"
STATUS_DISPATCHED = "dispatched"
STATUS_REGISTERED = "registered"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

VALID_STATUSES = frozenset(
    {STATUS_PLANNED, STATUS_DISPATCHED, STATUS_REGISTERED, STATUS_FAILED, STATUS_SKIPPED}
)

#: 默认每窗口自动路径最多真实提交几次训练（busy-409 等「未消耗计算」的失败
#: attempts 不增长，天然可无限重试；只有真提交过才算一次）。
DEFAULT_MAX_ATTEMPTS = 1

_TRIGGER_SCHEDULE = "schedule"

_DDL_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS qm_rolling_campaigns (
        campaign_id   TEXT PRIMARY KEY,
        market        VARCHAR(16)  NOT NULL,
        recipe_id     VARCHAR(128) NOT NULL,
        recipe_hash   VARCHAR(64)  NOT NULL,
        trigger       VARCHAR(16)  NOT NULL,
        status        VARCHAR(16)  NOT NULL,
        window_index  INTEGER      NOT NULL,
        anchor_date   DATE         NOT NULL,
        purge_days    INTEGER      NOT NULL,
        window_policy JSONB,
        window_plan   JSONB,
        run_id        VARCHAR(64),
        model_id      VARCHAR(128),
        attempts      INTEGER      NOT NULL DEFAULT 0,
        detail        JSONB,
        created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        updated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        dispatched_at TIMESTAMPTZ,
        finished_at   TIMESTAMPTZ
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_qm_rolling_campaigns_window
    ON qm_rolling_campaigns (market, recipe_id, anchor_date, trigger)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_qm_rolling_campaigns_status
    ON qm_rolling_campaigns (status, updated_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_qm_rolling_campaigns_run
    ON qm_rolling_campaigns (run_id)
    WHERE run_id IS NOT NULL
    """,
)


async def ensure_tables() -> None:
    async with get_session() as session:
        for statement in _DDL_STATEMENTS:
            await session.execute(text(statement))


def build_campaign_id(
    market: str, recipe_id: str, anchor_date: date, trigger: str = _TRIGGER_SCHEDULE
) -> str:
    """campaign_id 由窗口三要素导出（可复算、可对照）；手动触发加后缀区分台账。"""
    base = f"rc_{str(market).lower()}_{recipe_id}_{anchor_date.strftime('%Y%m%d')}"
    trigger_token = str(trigger or _TRIGGER_SCHEDULE).strip().lower()
    if trigger_token and trigger_token != _TRIGGER_SCHEDULE:
        base = f"{base}_{trigger_token}"
    return base


def decide_redispatch(
    existing: dict[str, Any] | None,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> dict[str, str]:
    """既有 campaign 的裁决（纯函数，可单测）。

    返回 ``{"action": create|reuse|redispatch, "reason": ...}``：
    - 无记录 → create；
    - planned / dispatched（在途）→ reuse；
    - registered（已训完）→ reuse；
    - skipped（数据未就绪等）→ redispatch；
    - failed：``attempts < max_attempts`` 才 redispatch（busy-409 计入 failed 但
      attempts=0，天然可重试；真提交过且失败的需人工介入，不自动重训）。
    """
    if existing is None:
        return {"action": "create", "reason": "no_campaign"}
    status = str(existing.get("status") or "")
    if status == STATUS_PLANNED:
        return {"action": "reuse", "reason": "planned_in_flight"}
    if status == STATUS_DISPATCHED:
        return {"action": "reuse", "reason": "in_flight"}
    if status == STATUS_REGISTERED:
        return {"action": "reuse", "reason": "already_registered"}
    if status == STATUS_SKIPPED:
        return {"action": "redispatch", "reason": "retry_after_skip"}
    if status == STATUS_FAILED:
        attempts = int(existing.get("attempts") or 0)
        if attempts < max_attempts:
            return {"action": "redispatch", "reason": "retry_after_failure"}
        return {"action": "reuse", "reason": "max_attempts_reached"}
    return {"action": "reuse", "reason": f"unknown_status:{status}"}


def _loads_maybe(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return value
    return value


def _row_to_dict(row: Any) -> dict[str, Any]:
    data = dict(row._mapping)
    for key in ("window_policy", "window_plan", "detail"):
        data[key] = _loads_maybe(data.get(key))
    anchor = data.get("anchor_date")
    if isinstance(anchor, date) and not isinstance(anchor, datetime):
        data["anchor_date"] = anchor.isoformat()
    for key in ("created_at", "updated_at", "dispatched_at", "finished_at"):
        value = data.get(key)
        if isinstance(value, datetime):
            data[key] = value.isoformat()
    return data


def _jsonb(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=str)


async def get_campaign(campaign_id: str) -> dict[str, Any] | None:
    async with get_session(read_only=True) as session:
        result = await session.execute(
            text("SELECT * FROM qm_rolling_campaigns WHERE campaign_id = :cid"),
            {"cid": campaign_id},
        )
        row = result.first()
    return _row_to_dict(row) if row else None


async def get_campaign_by_window(
    market: str, recipe_id: str, anchor_date: date | str, trigger: str
) -> dict[str, Any] | None:
    async with get_session(read_only=True) as session:
        result = await session.execute(
            text(
                """
                SELECT * FROM qm_rolling_campaigns
                WHERE market = :market AND recipe_id = :recipe_id
                  AND anchor_date = :anchor_date AND trigger = :trigger
                """
            ),
            {
                "market": market,
                "recipe_id": recipe_id,
                "anchor_date": anchor_date,
                "trigger": trigger,
            },
        )
        row = result.first()
    return _row_to_dict(row) if row else None


async def insert_campaign(
    *,
    campaign_id: str,
    market: str,
    recipe_id: str,
    recipe_hash: str,
    trigger: str,
    anchor_date: date,
    window_index: int,
    purge_days: int,
    window_policy: dict[str, Any] | None,
    window_plan: dict[str, Any] | None,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """落 planned 行；窗口已被占用（唯一键冲突）时返回 None，由调用方取既有行裁决。"""
    async with get_session() as session:
        result = await session.execute(
            text(
                """
                INSERT INTO qm_rolling_campaigns (
                    campaign_id, market, recipe_id, recipe_hash, trigger, status,
                    window_index, anchor_date, purge_days,
                    window_policy, window_plan, detail
                ) VALUES (
                    :campaign_id, :market, :recipe_id, :recipe_hash, :trigger,
                    :status, :window_index, :anchor_date, :purge_days,
                    CAST(:window_policy AS JSONB), CAST(:window_plan AS JSONB),
                    CAST(:detail AS JSONB)
                )
                ON CONFLICT (market, recipe_id, anchor_date, trigger) DO NOTHING
                RETURNING *
                """
            ),
            {
                "campaign_id": campaign_id,
                "market": market,
                "recipe_id": recipe_id,
                "recipe_hash": recipe_hash,
                "trigger": trigger,
                "status": STATUS_PLANNED,
                "window_index": window_index,
                "anchor_date": anchor_date,
                "purge_days": purge_days,
                "window_policy": _jsonb(window_policy),
                "window_plan": _jsonb(window_plan),
                "detail": _jsonb(detail),
            },
        )
        row = result.first()
    return _row_to_dict(row) if row else None


async def reopen_campaign(campaign_id: str) -> dict[str, Any] | None:
    """failed/skipped/崩溃残留 planned → planned（重试同一窗口；campaign_id 不变）。

    run_id/model_id 保留为上一次尝试的痕迹（最后一次派发会由 mark_dispatched
    覆盖）；attempts 不动，作为「真提交过几次」的累计账。

    planned 同样可重开：提交前进程死亡会留下永不推进的 planned 行，不重开就
    永久卡死。新鲜度闸门在唯一调用方（execute_dispatch 只在 ``planned_is_stale``
    裁决为 redispatch 后才走到这里）——本函数只认状态、不看时间；重开会刷新
    ``updated_at``，等于向并发 tick 声明「已受理，新窗口在途」。
    """
    async with get_session() as session:
        result = await session.execute(
            text(
                """
                UPDATE qm_rolling_campaigns
                SET status = :planned, finished_at = NULL, updated_at = NOW()
                WHERE campaign_id = :cid AND status IN (:failed, :skipped, :planned)
                RETURNING *
                """
            ),
            {
                "planned": STATUS_PLANNED,
                "failed": STATUS_FAILED,
                "skipped": STATUS_SKIPPED,
                "cid": campaign_id,
            },
        )
        row = result.first()
    return _row_to_dict(row) if row else None


async def mark_dispatched(campaign_id: str, run_id: str) -> dict[str, Any] | None:
    """planned → dispatched，记录 run_id 并记一次真实尝试（attempts+1）。"""
    async with get_session() as session:
        result = await session.execute(
            text(
                """
                UPDATE qm_rolling_campaigns
                SET status = :dispatched, run_id = :run_id, attempts = attempts + 1,
                    dispatched_at = NOW(), updated_at = NOW()
                WHERE campaign_id = :cid AND status = :planned
                RETURNING *
                """
            ),
            {
                "dispatched": STATUS_DISPATCHED,
                "planned": STATUS_PLANNED,
                "run_id": run_id,
                "cid": campaign_id,
            },
        )
        row = result.first()
    return _row_to_dict(row) if row else None


async def mark_failed(
    campaign_id: str, reason: str, detail: dict[str, Any] | None = None
) -> bool:
    """planned/dispatched → failed。registered 不允许被覆盖。"""
    payload = {"reason": reason}
    if detail:
        payload.update(detail)
    async with get_session() as session:
        result = await session.execute(
            text(
                """
                UPDATE qm_rolling_campaigns
                SET status = :failed, detail = CAST(:detail AS JSONB),
                    finished_at = NOW(), updated_at = NOW()
                WHERE campaign_id = :cid AND status IN (:planned, :dispatched)
                """
            ),
            {
                "failed": STATUS_FAILED,
                "planned": STATUS_PLANNED,
                "dispatched": STATUS_DISPATCHED,
                "detail": _jsonb(payload),
                "cid": campaign_id,
            },
        )
        return bool(result.rowcount)


async def mark_skipped(
    campaign_id: str, reason: str, detail: dict[str, Any] | None = None
) -> bool:
    """planned → skipped（数据未就绪等；decide_redispatch 视其为可重试）。"""
    payload = {"reason": reason}
    if detail:
        payload.update(detail)
    async with get_session() as session:
        result = await session.execute(
            text(
                """
                UPDATE qm_rolling_campaigns
                SET status = :skipped, detail = CAST(:detail AS JSONB),
                    finished_at = NOW(), updated_at = NOW()
                WHERE campaign_id = :cid AND status = :planned
                """
            ),
            {
                "skipped": STATUS_SKIPPED,
                "planned": STATUS_PLANNED,
                "detail": _jsonb(payload),
                "cid": campaign_id,
            },
        )
        return bool(result.rowcount)


async def mark_outcome_by_run(
    run_id: str,
    *,
    status: str,
    model_id: str | None = None,
    reason: str = "",
) -> bool:
    """run 终态回流：dispatched 行 → registered（status=completed）/ failed。

    只认 dispatched 行（planned 行还没提交成功，不该被 run 终态污染）。
    返回是否真的改到了行——False 不代表错误（该 run 本就不是滚动派发的）。
    """
    normalized = str(status or "").strip().lower()
    if normalized == STATUS_REGISTERED:
        target, finished = STATUS_REGISTERED, True
    elif normalized == STATUS_FAILED:
        target, finished = STATUS_FAILED, True
    else:
        raise ValueError(f"mark_outcome_by_run 不支持的状态: {status}")
    detail = _jsonb({"reason": reason}) if reason else None
    async with get_session() as session:
        result = await session.execute(
            text(
                """
                UPDATE qm_rolling_campaigns
                SET status = :target,
                    model_id = COALESCE(:model_id, model_id),
                    detail = COALESCE(CAST(:detail AS JSONB), detail),
                    finished_at = CASE WHEN :finished THEN NOW() ELSE finished_at END,
                    updated_at = NOW()
                WHERE run_id = :run_id AND status = :dispatched
                """
            ),
            {
                "target": target,
                "model_id": model_id,
                "detail": detail,
                "finished": finished,
                "run_id": run_id,
                "dispatched": STATUS_DISPATCHED,
            },
        )
        return bool(result.rowcount)


async def mark_outcome_by_run_safe(
    run_id: str,
    *,
    status: str,
    model_id: str | None = None,
    reason: str = "",
) -> bool:
    """callable-from-callback 版本：任何失败只记日志（训练回调不能被台账拖垮）。"""
    try:
        return await mark_outcome_by_run(
            run_id, status=status, model_id=model_id, reason=reason
        )
    except Exception:  # noqa: BLE001
        logger.exception("[RollingCampaign] run=%s 终态回流台账失败", run_id)
        return False


async def list_campaigns(
    *,
    market: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: dict[str, Any] = {"limit": max(1, min(int(limit), 500))}
    if market:
        clauses.append("market = :market")
        params["market"] = market
    if status:
        clauses.append("status = :status")
        params["status"] = status
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    async with get_session(read_only=True) as session:
        result = await session.execute(
            text(
                f"""
                SELECT * FROM qm_rolling_campaigns
                {where}
                ORDER BY updated_at DESC
                LIMIT :limit
                """
            ),
            params,
        )
        rows = result.fetchall()
    return [_row_to_dict(row) for row in rows]
