"""晋升流程台账（P2 · 设计 §3.1 状态机 / §5.4）：``qm_model_rollouts`` 唯一存储层。

阶段（stage）与合法迁移——**每一跳都由服务层带 ``from_stages`` 条件迁移**，
台账拒绝跳级与并发覆盖（条件更新返回 None 即「没改到行」，由调用方裁决）：

    replay_eval ──▶ observing ──▶ gate_passed ──▶ promoted
         │             │             │
         ├─────────────┴─────────────┴──▶ rejected（关 settings 行）
         └──（仅 promoted/rolled_back 之后）──▶ rolled_back（理由必填）

- ``replay_eval``：rollout 已创建，vintage 回放配对评估可跑；
- ``observing``：挑战者已进推理名单（``qm_model_inference_settings``），等前向 IC；
- ``gate_passed``：G0-G7 结论齐、等人工批准（观察模式下 flagged 也可人工放行，
  证据卡如实展示）；
- ``promoted``：事务内 ``set_default(challenger)`` 成功，``prior_default_model_id``
  记录备任链；``decided_at`` 落批准时刻；
- ``rejected``：关 settings 行（服务层动作）；
- ``rolled_back``：``set_default(prior_default)`` + **理由必填**（审计）。

同一 (租户, 用户, 市场, 挑战者) 同时最多一条活跃 rollout（部分唯一索引），
终态后可再建（复评）。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import text

from backend.shared.database_manager_v2 import get_session
from backend.shared.utc_datetime import utc_now

STAGE_REPLAY_EVAL = "replay_eval"
STAGE_OBSERVING = "observing"
STAGE_GATE_PASSED = "gate_passed"
STAGE_PROMOTED = "promoted"
STAGE_REJECTED = "rejected"
STAGE_ROLLED_BACK = "rolled_back"

#: 活跃阶段（部分唯一索引的谓词 + 「同时只能有一条」的判定集）
ACTIVE_STAGES = frozenset({STAGE_REPLAY_EVAL, STAGE_OBSERVING, STAGE_GATE_PASSED})

VALID_STAGES = frozenset(
    {
        STAGE_REPLAY_EVAL,
        STAGE_OBSERVING,
        STAGE_GATE_PASSED,
        STAGE_PROMOTED,
        STAGE_REJECTED,
        STAGE_ROLLED_BACK,
    }
)

#: 列表默认条数（治理页按更新时间倒序）
DEFAULT_LIST_LIMIT = 50

_DDL_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS qm_model_rollouts (
        rollout_id             TEXT PRIMARY KEY,
        tenant_id              VARCHAR(64)  NOT NULL,
        user_id                VARCHAR(64)  NOT NULL,
        market                 VARCHAR(16)  NOT NULL,
        campaign_id            VARCHAR(128),
        champion_model_id      VARCHAR(128) NOT NULL,
        challenger_model_id    VARCHAR(128) NOT NULL,
        stage                  VARCHAR(16)  NOT NULL,
        gate_result            JSONB,
        evidence               JSONB,
        prior_default_model_id VARCHAR(128),
        decided_by             VARCHAR(128),
        decided_at             TIMESTAMPTZ,
        created_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        updated_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        notes                  TEXT
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_qm_model_rollouts_active
    ON qm_model_rollouts (tenant_id, user_id, market, challenger_model_id)
    WHERE stage IN ('replay_eval', 'observing', 'gate_passed')
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_qm_model_rollouts_roster
    ON qm_model_rollouts (tenant_id, user_id, market, updated_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_qm_model_rollouts_challenger
    ON qm_model_rollouts (challenger_model_id, stage)
    """,
)


async def ensure_tables() -> None:
    """启动期自愈（老库补表）；与 db_init.sql 的镜像由漂移测试守着。"""
    async with get_session() as session:
        for statement in _DDL_STATEMENTS:
            await session.execute(text(statement))


def build_rollout_id(market: str) -> str:
    """``ro_{市场}_{UTC 时刻}_{随机 6}``——可读、可排序、并发安全。"""
    stamp = utc_now().strftime("%Y%m%d%H%M%S")
    return f"ro_{str(market or 'xx').lower()}_{stamp}_{uuid.uuid4().hex[:6]}"


def _loads_maybe(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return value
    return value


def _jsonb(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=str)


def _row_to_dict(row: Any) -> dict[str, Any]:
    data = dict(row._mapping)
    for key in ("gate_result", "evidence"):
        if key in data:
            data[key] = _loads_maybe(data.get(key))
    for key in ("decided_at", "created_at", "updated_at"):
        value = data.get(key)
        if isinstance(value, datetime):
            data[key] = value.isoformat()
    return data


async def insert_rollout(
    *,
    rollout_id: str | None = None,
    tenant_id: str,
    user_id: str,
    market: str,
    champion_model_id: str,
    challenger_model_id: str,
    campaign_id: str | None = None,
    stage: str = STAGE_REPLAY_EVAL,
    notes: str | None = None,
) -> dict[str, Any] | None:
    """落初始行；活跃冲突（部分唯一索引/主键）→ None，由调用方取既有活跃行。"""
    if stage not in VALID_STAGES:
        raise ValueError(f"未知 rollout 阶段: {stage}")
    rid = rollout_id or build_rollout_id(market)
    async with get_session() as session:
        result = await session.execute(
            text(
                """
                INSERT INTO qm_model_rollouts (
                    rollout_id, tenant_id, user_id, market, campaign_id,
                    champion_model_id, challenger_model_id, stage, notes
                ) VALUES (
                    :rollout_id, :tenant_id, :user_id, :market, :campaign_id,
                    :champion_model_id, :challenger_model_id, :stage, :notes
                )
                ON CONFLICT DO NOTHING
                RETURNING *
                """
            ),
            {
                "rollout_id": rid,
                "tenant_id": tenant_id,
                "user_id": user_id,
                "market": market,
                "campaign_id": campaign_id,
                "champion_model_id": champion_model_id,
                "challenger_model_id": challenger_model_id,
                "stage": stage,
                "notes": notes,
            },
        )
        row = result.first()
    return _row_to_dict(row) if row else None


async def get_rollout(rollout_id: str) -> dict[str, Any] | None:
    async with get_session(read_only=True) as session:
        result = await session.execute(
            text("SELECT * FROM qm_model_rollouts WHERE rollout_id = :rid"),
            {"rid": rollout_id},
        )
        row = result.first()
    return _row_to_dict(row) if row else None


async def get_active_rollout(
    *, tenant_id: str, user_id: str, market: str, challenger_model_id: str
) -> dict[str, Any] | None:
    """当前活跃 rollout（同一挑战者重评时返回在途的那条）。"""
    async with get_session(read_only=True) as session:
        result = await session.execute(
            text(
                """
                SELECT * FROM qm_model_rollouts
                WHERE tenant_id = :tenant_id AND user_id = :user_id
                  AND market = :market AND challenger_model_id = :challenger
                  AND stage = ANY(:stages)
                ORDER BY updated_at DESC LIMIT 1
                """
            ),
            {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "market": market,
                "challenger": challenger_model_id,
                "stages": sorted(ACTIVE_STAGES),
            },
        )
        row = result.first()
    return _row_to_dict(row) if row else None


async def list_rollouts(
    *,
    tenant_id: str,
    user_id: str | None = None,
    market: str | None = None,
    stage: str | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
) -> list[dict[str, Any]]:
    clauses = ["tenant_id = :tenant_id"]
    params: dict[str, Any] = {
        "tenant_id": tenant_id,
        "limit": max(1, min(int(limit), 500)),
    }
    if user_id:
        clauses.append("user_id = :user_id")
        params["user_id"] = user_id
    if market:
        clauses.append("market = :market")
        params["market"] = market
    if stage:
        clauses.append("stage = :stage")
        params["stage"] = stage
    async with get_session(read_only=True) as session:
        result = await session.execute(
            text(
                f"""
                SELECT * FROM qm_model_rollouts
                WHERE {' AND '.join(clauses)}
                ORDER BY updated_at DESC
                LIMIT :limit
                """
            ),
            params,
        )
        rows = result.fetchall()
    return [_row_to_dict(row) for row in rows]


async def transition(
    rollout_id: str,
    *,
    to_stage: str,
    from_stages: list[str] | frozenset[str],
    gate_result: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
    prior_default_model_id: str | None = None,
    decided_by: str | None = None,
    notes: str | None = None,
    decided: bool = False,
    require_notes: bool = False,
    session: Any | None = None,
) -> dict[str, Any] | None:
    """条件迁移（唯一写口）：``stage ∈ from_stages`` 才改，改不到返回 None。

    - ``decided=True`` 时落 ``decided_at = NOW()``（promoted/rejected/rolled_back）；
    - ``require_notes=True``（回滚）时理由必填——空理由在进 SQL 前就报错；
    - JSONB/text 字段一律 COALESCE 语义：不传 = 保留旧值；
    - 传 ``session``（AsyncSession）时复用调用方事务——晋升/回滚要与
      「市场级默认切换」同生共死（要么都成，要么整体回滚）；不传则自管事务。
    """
    if to_stage not in VALID_STAGES:
        raise ValueError(f"未知 rollout 阶段: {to_stage}")
    if require_notes and not str(notes or "").strip():
        raise ValueError("理由必填")

    async def _execute(active_session: Any) -> dict[str, Any] | None:
        result = await active_session.execute(
            text(
                """
                UPDATE qm_model_rollouts
                SET stage = :to_stage,
                    gate_result = COALESCE(CAST(:gate_result AS JSONB), gate_result),
                    evidence = COALESCE(CAST(:evidence AS JSONB), evidence),
                    prior_default_model_id = COALESCE(
                        :prior_default_model_id, prior_default_model_id
                    ),
                    decided_by = COALESCE(:decided_by, decided_by),
                    decided_at = CASE WHEN :decided THEN NOW() ELSE decided_at END,
                    notes = COALESCE(:notes, notes),
                    updated_at = NOW()
                WHERE rollout_id = :rid AND stage = ANY(:from_stages)
                RETURNING *
                """
            ),
            {
                "to_stage": to_stage,
                "gate_result": _jsonb(gate_result),
                "evidence": _jsonb(evidence),
                "prior_default_model_id": prior_default_model_id,
                "decided_by": decided_by,
                "notes": notes,
                "decided": bool(decided),
                "from_stages": sorted(from_stages),
                "rid": rollout_id,
            },
        )
        row = result.first()
        return _row_to_dict(row) if row else None

    if session is not None:
        return await _execute(session)
    async with get_session() as owned_session:
        return await _execute(owned_session)


async def latest_promotion_of(
    model_id: str,
    *,
    tenant_id: str,
    user_id: str,
    market: str,
    session: Any | None = None,
) -> dict[str, Any] | None:
    """该模型最近一次被晋升的 rollout（archive 回退查备任链用，§5.4）。

    传 ``session`` 时复用调用方事务（archive 回退要在同一事务里查备任链）。
    """
    params = {
        "model_id": model_id,
        "promoted": STAGE_PROMOTED,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "market": market,
    }
    stmt = text(
        """
        SELECT * FROM qm_model_rollouts
        WHERE challenger_model_id = :model_id AND stage = :promoted
          AND tenant_id = :tenant_id AND user_id = :user_id
          AND market = :market
        ORDER BY decided_at DESC NULLS LAST, updated_at DESC
        LIMIT 1
        """
    )
    if session is not None:
        row = (await session.execute(stmt, params)).first()
    else:
        async with get_session(read_only=True) as owned_session:
            row = (await owned_session.execute(stmt, params)).first()
    return _row_to_dict(row) if row else None
