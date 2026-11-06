"""Owner-scoped immutable N-direction selection; no paid calls or auto dispatch."""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import text

from backend.shared.database_manager_v2 import get_session
from backend.shared.owner_scope import OwnerScope
from backend.shared.utc_datetime import to_utc_iso
from .api_models import DirectionBatchDispatchRequest, SampledDirectionBatchRequest
from .direction_sampling import sample_weighted_directions_n


class DirectionBatchConflict(ValueError):
    pass


def _identity(owner):
    if not isinstance(owner, OwnerScope):
        raise ValueError("Authenticated owner required")


def child_key(batch_id, index):
    return f"sample-batch:{batch_id}:{index}"


async def get_batch(owner: OwnerScope, batch_id):
    _identity(owner)
    async with get_session() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        await session.execute(text("SET LOCAL statement_timeout='5s'"))
        row = (
            (
                await session.execute(
                    text(
                        "SELECT batch_id,fingerprint,request_snapshot,selection_snapshot,created_at "
                        "FROM alpha_direction_batches WHERE tenant_id=:tenant_id AND user_id=:user_id "
                        "AND batch_id=:batch_id"
                    ),
                    {**owner.sql_params(), "batch_id": batch_id},
                )
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


async def freeze_batch(owner: OwnerScope, payload: SampledDirectionBatchRequest):
    _identity(owner)
    submitted = payload.model_dump(mode="json")
    encoded = json.dumps(
        submitted,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    if len(encoded.encode("utf-8")) > 256 * 1024:
        raise ValueError("批次输入超过256 KiB")
    fingerprint = hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def match(row):
        if row["fingerprint"] != fingerprint:
            raise DirectionBatchConflict("批次ID已绑定其他完整请求；未派发任何变更方向")
        return row

    previous = await get_batch(owner, payload.batch_id)
    if previous is not None:
        return match(previous)
    if payload.selection_mode == "random":
        pairs = await sample_weighted_directions_n(
            payload.directions,
            payload.num_directions,
            owner=owner,
            market=payload.market,
            universe=payload.universe,
        )
    elif payload.selection_mode == "quota":
        from .direction_quota import allocate as allocate_quota_directions

        pairs = await allocate_quota_directions(
            payload.directions,
            payload.num_directions,
            owner=owner,
            market=payload.market,
            universe=payload.universe,
        )
    else:
        pairs = [
            (value, None) for value in payload.directions[: payload.num_directions]
        ]
    positions = {value: index for index, value in enumerate(payload.directions)}
    selection = [
        {"direction": value, "source_index": positions[value], "evidence": meta}
        for value, meta in pairs
    ]
    selection_json = json.dumps(selection, ensure_ascii=False, allow_nan=False)
    if len(selection_json.encode("utf-8")) > 2 * 1024 * 1024:
        raise ValueError("批次抽样凭证超过2 MiB")
    async with get_session() as session:
        await session.execute(text("SET LOCAL statement_timeout='5s'"))
        await session.execute(
            text(
                "INSERT INTO alpha_direction_batches "
                "(tenant_id,user_id,batch_id,fingerprint,request_snapshot,selection_snapshot) "
                "VALUES (:tenant_id,:user_id,:batch_id,:fingerprint,CAST(:request AS jsonb),CAST(:selection AS jsonb)) "
                "ON CONFLICT (tenant_id,user_id,batch_id) DO NOTHING"
            ),
            {
                **owner.sql_params(),
                "batch_id": payload.batch_id,
                "fingerprint": fingerprint,
                "request": encoded,
                "selection": selection_json,
            },
        )
        row = (
            (
                await session.execute(
                    text(
                        "SELECT batch_id,fingerprint,request_snapshot,selection_snapshot,created_at "
                        "FROM alpha_direction_batches WHERE tenant_id=:tenant_id AND user_id=:user_id "
                        "AND batch_id=:batch_id"
                    ),
                    {**owner.sql_params(), "batch_id": payload.batch_id},
                )
            )
            .mappings()
            .one()
        )
        result = match(dict(row))
        await session.commit()
    return result


def dispatch_payload(batch):
    request = batch["request_snapshot"]
    selected = batch["selection_snapshot"]
    values = {
        key: value
        for key, value in request.items()
        if key not in {"selection_mode", "num_directions"}
    }
    values["directions"] = [row["direction"] for row in selected]
    if values.get("seed_factor_ids") is not None:
        values["seed_factor_ids"] = [
            request["seed_factor_ids"][row["source_index"]] for row in selected
        ]
    return DirectionBatchDispatchRequest.model_validate(values)


async def detail(owner: OwnerScope, batch):
    _identity(owner)
    selection = batch["selection_snapshot"]
    keys = [child_key(batch["batch_id"], index) for index in range(len(selection))]
    async with get_session() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        await session.execute(text("SET LOCAL statement_timeout='5s'"))
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT k.request_key,t.task_id,t.status FROM alpha_mining_request_keys k "
                        "JOIN alpha_mining_tasks t ON t.tenant_id=k.tenant_id AND t.user_id=k.user_id AND t.task_id=k.task_id "
                        "WHERE k.tenant_id=:tenant_id AND k.user_id=:user_id "
                        "AND t.tenant_id=:tenant_id AND t.user_id=:user_id "
                        "AND k.request_key=ANY(CAST(:keys AS TEXT[]))"
                    ),
                    {**owner.sql_params(), "keys": keys},
                )
            )
            .mappings()
            .all()
        )
    by_key = {row["request_key"]: row for row in rows}
    return {
        "batch_id": batch["batch_id"],
        "created_at": to_utc_iso(batch["created_at"]),
        "request": batch["request_snapshot"],
        "items": [
            {
                **item,
                "index": index,
                "task_id": by_key[keys[index]]["task_id"]
                if keys[index] in by_key
                else None,
                "status": by_key[keys[index]]["status"]
                if keys[index] in by_key
                else "not_submitted",
            }
            for index, item in enumerate(selection)
        ],
    }


async def history(owner: OwnerScope, offset):
    _identity(owner)
    async with get_session() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        await session.execute(text("SET LOCAL statement_timeout='5s'"))
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT batch_id,created_at,request_snapshot->>'universe' AS universe,"
                        "request_snapshot->>'selection_mode' AS selection_mode,jsonb_array_length(selection_snapshot) AS selected_count "
                        "FROM alpha_direction_batches WHERE tenant_id=:tenant_id AND user_id=:user_id "
                        "ORDER BY created_at DESC,batch_id DESC LIMIT 21 OFFSET :offset"
                    ),
                    {**owner.sql_params(), "offset": offset},
                )
            )
            .mappings()
            .all()
        )
    return {
        "items": [
            {**dict(row), "created_at": to_utc_iso(row["created_at"])}
            for row in rows[:20]
        ],
        "has_more": len(rows) > 20,
    }
