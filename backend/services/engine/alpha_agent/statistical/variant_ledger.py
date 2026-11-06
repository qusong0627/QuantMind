"""AutoAlpha variant memory, adapted to explicit PG ownership and frozen inputs.

The compute child never reads PG or shared files. Cooling is captured by its
controller after data hashes are known, then becomes part of the task snapshot.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from datetime import timedelta
from pathlib import Path

from sqlalchemy import text

from backend.shared.owner_scope import OwnerScope
from backend.shared.utc_datetime import to_utc_iso, utc_now

COOLDOWN_DAYS = 90
HIGH_CONF_DAYS = 40
COOL_ICIR_FLOOR = 0.3
MAX_OBSERVATIONS = 24
MEMORY_VERSION = 1
EDIT_TYPE_BY_KEY = {
    "reversal_10d": "调窗",
    "reversal_20d": "调窗",
    "reversal_60d": "调窗",
    "momentum_60d": "调窗",
    "momentum_120d": "调窗",
    "high_prox_250d": "估计量变换",
    "skew_20d": "矩变换",
    "vol_60d": "调窗",
    "downside_vol_20d": "估计量变换",
    "parkinson_20d": "估计量变换",
    "atr_14": "估计量变换",
    "amihud_20d": "调窗",
    "volume_spike_20d": "估计量变换",
    "amount_burst_20d": "换算子",
    "volume_cv_20d": "估计量变换",
    "pv_corr_60d": "调窗",
    "range_20d": "调窗",
    "overnight_ret_20d": "调窗",
    "close_position_20d": "估计量变换",
    "volreg_reversal_5d": "加门",
    "trend_r2_gate_20d": "加门",
    "skew_gate_momentum_20d": "加门",
}


def _json(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _catalog():
    from .candidates import BASELINE_BY_KEY, CANDIDATES

    return BASELINE_BY_KEY, CANDIDATES


def coolable_keys() -> frozenset[str]:
    baselines, _ = _catalog()
    return frozenset(baselines) - frozenset(baselines.values())


def variant_skeleton() -> list[dict]:
    baselines, candidates = _catalog()
    allowed = coolable_keys()
    return [
        {
            "key": item.key,
            "name": item.name,
            "family": item.family,
            "baseline_key": baselines.get(item.key),
            "edit_type": EDIT_TYPE_BY_KEY.get(item.key),
            "coolable": item.key in allowed,
        }
        for item in candidates
    ]


def valid_context_id(value: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )


def kernel_digest() -> str:
    """Version the trusted numerical pipeline, including its candidate genealogy."""
    root = Path(__file__).parent
    digest = hashlib.sha256()
    from importlib.metadata import PackageNotFoundError, version

    for package in ("numpy", "pandas", "scipy", "pyarrow"):
        try:
            installed = version(package)
        except PackageNotFoundError:
            installed = "unavailable"
        digest.update(f"{package}={installed}\n".encode())
    for name in (
        "candidates.py",
        "engine.py",
        "ic_stats.py",
        "tradability.py",
        "neutralize.py",
        "incremental.py",
        "dedup.py",
        "stability.py",
        "composite.py",
        "complexity.py",
        "costs.py",
        "panel.py",
        "worker.py",
        "configuration.py",
        "variant_ledger.py",
    ):
        digest.update(name.encode())
        digest.update((root / name).read_bytes())
    digest.update((root.parent.parent / "inference" / "trading_cost.py").read_bytes())
    return digest.hexdigest()


def _scope(algorithm_version: str, params: dict, snapshot: dict) -> dict:
    from .configuration import STATISTICAL_PARAMETER_KEYS, statistical_config_payload

    if params.get("market") != "a_share" or not params.get("universe"):
        raise ValueError("Invalid statistical variant scope")
    return {
        "memory_version": MEMORY_VERSION,
        "algorithm_version": algorithm_version,
        "kernel_digest": kernel_digest(),
        "catalog": variant_skeleton(),
        "policy": {
            "cooldown_days": COOLDOWN_DAYS,
            "high_conf_days": HIGH_CONF_DAYS,
            "cool_icir_floor": COOL_ICIR_FLOOR,
            "max_observations": MAX_OBSERVATIONS,
        },
        "market": "a_share",
        "universe": params["universe"],
        "parameters": statistical_config_payload(
            {k: v for k, v in params.items() if k in STATISTICAL_PARAMETER_KEYS}
        ),
        "window": {
            k: params.get(k)
            for k in ("mining_start", "mining_end", "valid_start", "valid_end")
        },
        "data": {
            k: snapshot[k]
            for k in ("panel_sha256", "metadata_sha256", "symbols_sha256")
        },
    }


async def freeze_memory(
    session, *, owner: OwnerScope, algorithm_version: str, params: dict, snapshot: dict
) -> dict:
    scope = _scope(algorithm_version, params, snapshot)
    context_id = hashlib.sha256(_json(scope).encode("utf-8")).hexdigest()
    sql_params = {
        **owner.sql_params(),
        "context": context_id,
        "universe": scope["universe"],
        "scope": _json(scope),
    }
    await session.execute(
        text("""
        INSERT INTO alpha_variant_contexts (tenant_id,user_id,context_id,universe,scope_json)
        VALUES (:tenant_id,:user_id,:context,:universe,CAST(:scope AS jsonb))
        ON CONFLICT (tenant_id,user_id,context_id) DO NOTHING
    """),
        sql_params,
    )
    row = (
        (
            await session.execute(
                text("""
        SELECT scope_json FROM alpha_variant_contexts
        WHERE tenant_id=:tenant_id AND user_id=:user_id AND context_id=:context FOR UPDATE
    """),
                sql_params,
            )
        )
        .mappings()
        .one()
    )
    if row["scope_json"] != scope:
        raise ValueError("Statistical variant context does not match its digest")
    rows = (
        (
            await session.execute(
                text("""
        SELECT variant_key,evidence_task_id FROM alpha_variant_states
        WHERE tenant_id=:tenant_id AND user_id=:user_id AND context_id=:context
          AND cooled_until > now() ORDER BY variant_key
    """),
                sql_params,
            )
        )
        .mappings()
        .all()
    )
    allowed = coolable_keys()
    return {
        "version": MEMORY_VERSION,
        "context_id": context_id,
        "kernel_digest": scope["kernel_digest"],
        "cooled_keys": [
            row["variant_key"] for row in rows if row["variant_key"] in allowed
        ],
        "evidence_tasks": {
            row["variant_key"]: row["evidence_task_id"]
            for row in rows
            if row["variant_key"] in allowed
        },
    }


def frozen_cooled_keys(snapshot: dict) -> frozenset[str]:
    """Validate the controller's frozen memory before the numerical pipeline runs."""
    memory = snapshot.get("variant_memory")
    if memory is None:  # Older tasks retain the original no-cooling behavior.
        return frozenset()
    if (
        not isinstance(memory, dict)
        or memory.get("version") != MEMORY_VERSION
        or not valid_context_id(memory.get("context_id"))
        or memory.get("kernel_digest") != kernel_digest()
    ):
        raise ValueError("Frozen statistical variant memory version does not match")
    keys = memory.get("cooled_keys")
    if (
        not isinstance(keys, list)
        or len(keys) > 32
        or any(not isinstance(key, str) for key in keys)
        or len(set(keys)) != len(keys)
        or set(keys) - coolable_keys()
    ):
        raise ValueError("Invalid frozen variant cooling list")
    return frozenset(keys)


def _observations(result: dict) -> list[dict]:
    baselines, _ = _catalog()
    candidates = result.get("candidates")
    if not isinstance(candidates, list) or len(candidates) > 32:
        raise ValueError("Invalid variant result candidates")
    out, seen = [], set()
    for candidate in candidates:
        key = candidate.get("key") if isinstance(candidate, dict) else None
        if key not in baselines:
            continue
        if key in seen or candidate.get("variant_of") != baselines[key]:
            raise ValueError("Statistical variant genealogy does not match")
        seen.add(key)
        horizons = candidate.get("variant_residual")
        if not isinstance(horizons, list) or len(horizons) > 8:
            raise ValueError("Invalid statistical residual observations")
        normalized = []
        for item in horizons:
            if not isinstance(item, dict) or type(item.get("horizon")) is not int:
                raise ValueError("Invalid variant residual horizon")
            days = item.get("n_days", 0)
            if type(days) is not int or days < 0:
                raise ValueError("Invalid variant residual sample size")
            values = {}
            for field in ("ic_ir", "ic_mean"):
                value = item.get(field)
                if value is not None and (
                    type(value) not in (int, float) or not math.isfinite(value)
                ):
                    raise ValueError("Invalid variant residual metric")
                values[field] = (
                    abs(value) if field == "ic_ir" and value is not None else value
                )
            normalized.append(
                {
                    "horizon": item["horizon"],
                    "n_days": days,
                    "status": str(item.get("status", "unavailable"))[:64],
                    **values,
                }
            )
        best = candidate.get("best_horizon")
        if best is not None and type(best) is not int:
            raise ValueError("Invalid statistical best horizon")
        out.append(
            {
                "variant": key,
                "baseline": baselines[key],
                "best": best,
                "horizons": normalized,
            }
        )
    return out


async def record_result(
    session, *, owner: OwnerScope, task_id: str, result: dict
) -> None:
    """Called inside the result/terminal transaction, after its result row exists."""
    row = (
        (
            await session.execute(
                text("""
        SELECT data_snapshot FROM alpha_mining_task_specs
        WHERE tenant_id=:tenant_id AND user_id=:user_id AND task_id=:task AND algorithm='statistical'
    """),
                {**owner.sql_params(), "task": task_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None or not row["data_snapshot"].get("variant_memory"):
        return
    memory = row["data_snapshot"]["variant_memory"]
    context_id = memory["context_id"]
    if result.get("variant_context_id") != context_id or sorted(
        result.get("cooled_keys") or []
    ) != sorted(memory["cooled_keys"]):
        raise ValueError("Statistical result does not match frozen variant memory")
    params = {**owner.sql_params(), "context": context_id, "task": task_id}
    context = (
        await session.execute(
            text("""
        SELECT context_id FROM alpha_variant_contexts WHERE tenant_id=:tenant_id
          AND user_id=:user_id AND context_id=:context FOR UPDATE
    """),
            params,
        )
    ).first()
    if context is None:
        raise ValueError("Statistical variant context is missing")
    moment = utc_now()
    allowed = coolable_keys()
    for observation in _observations(result):
        values = {
            **params,
            **observation,
            "horizons": _json(observation["horizons"]),
            "at": moment,
        }
        inserted = await session.execute(
            text("""
            INSERT INTO alpha_variant_observations
              (tenant_id,user_id,context_id,variant_key,task_id,baseline_key,best_horizon,horizons_json,observed_at)
            VALUES (:tenant_id,:user_id,:context,:variant,:task,:baseline,:best,CAST(:horizons AS jsonb),:at)
            ON CONFLICT (tenant_id,user_id,context_id,variant_key,task_id) DO NOTHING
        """),
            values,
        )
        if inserted.rowcount != 1 or observation["variant"] not in allowed:
            continue
        stat = next(
            (
                item
                for item in observation["horizons"]
                if item["horizon"] == observation["best"]
            ),
            None,
        )
        if (
            not stat
            or stat["status"] != "ok"
            or stat["n_days"] < HIGH_CONF_DAYS
            or stat["ic_ir"] is None
        ):
            continue  # Missing/weak evidence never cools or lifts a prior decision.
        failed = abs(stat["ic_ir"]) < COOL_ICIR_FLOOR
        await session.execute(
            text("""
            INSERT INTO alpha_variant_states
              (tenant_id,user_id,context_id,variant_key,evidence_task_id,cooled_since,cooled_until,updated_at)
            VALUES (:tenant_id,:user_id,:context,:variant,:task,:since,:until,:at)
            ON CONFLICT (tenant_id,user_id,context_id,variant_key) DO UPDATE SET
              evidence_task_id=EXCLUDED.evidence_task_id,cooled_since=EXCLUDED.cooled_since,
              cooled_until=EXCLUDED.cooled_until,reset_at=NULL,updated_at=EXCLUDED.updated_at
            WHERE alpha_variant_states.tenant_id=:tenant_id AND alpha_variant_states.user_id=:user_id
              AND alpha_variant_states.context_id=:context AND alpha_variant_states.updated_at<=:at
        """),
            {
                **values,
                "since": moment if failed else None,
                "until": moment + timedelta(days=COOLDOWN_DAYS) if failed else None,
            },
        )
    await session.execute(
        text("""
        UPDATE alpha_variant_contexts SET updated_at=:at
        WHERE tenant_id=:tenant_id AND user_id=:user_id AND context_id=:context
    """),
        {**params, "at": moment},
    )


async def list_contexts(
    *, tenant_id: str, user_id: str, universe: str, limit: int = 20, offset: int = 0
) -> dict:
    from backend.shared.database_manager_v2 import get_session

    owner = OwnerScope(tenant_id, user_id)
    if not 1 <= limit <= 50 or not 0 <= offset <= 1000000:
        raise ValueError("Invalid variant context page")
    async with get_session(read_only=True) as session:
        row = (
            (
                await session.execute(
                    text("""
            WITH matching AS MATERIALIZED (
                SELECT context_id,scope_json->'window' AS "window",created_at,updated_at
                FROM alpha_variant_contexts WHERE tenant_id=:tenant_id AND user_id=:user_id
                  AND universe=:universe
            ), page AS (
                SELECT * FROM matching ORDER BY updated_at DESC,context_id LIMIT :limit OFFSET :offset
            )
            SELECT (SELECT count(*) FROM matching) AS total,
              COALESCE((SELECT jsonb_agg(to_jsonb(page) ORDER BY updated_at DESC,context_id) FROM page),'[]'::jsonb) AS items
        """),
                    {
                        **owner.sql_params(),
                        "universe": universe,
                        "limit": limit,
                        "offset": offset,
                    },
                )
            )
            .mappings()
            .one()
        )
    items = row["items"]
    if isinstance(items, str):
        items = json.loads(items)
    from datetime import datetime

    for item in items:
        for key in ("created_at", "updated_at"):
            item[key] = to_utc_iso(
                datetime.fromisoformat(item[key].replace("Z", "+00:00"))
            )
    return {
        "items": items,
        "total": int(row["total"]),
        "limit": limit,
        "offset": offset,
    }


async def context_view(*, tenant_id: str, user_id: str, context_id: str) -> dict | None:
    from backend.shared.database_manager_v2 import get_session

    owner = OwnerScope(tenant_id, user_id)
    if not valid_context_id(context_id):
        return None
    params = {**owner.sql_params(), "context": context_id, "limit": MAX_OBSERVATIONS}
    async with get_session(read_only=True) as session:
        context = (
            (
                await session.execute(
                    text("""
            SELECT scope_json,updated_at FROM alpha_variant_contexts
            WHERE tenant_id=:tenant_id AND user_id=:user_id AND context_id=:context
        """),
                    params,
                )
            )
            .mappings()
            .first()
        )
        if context is None:
            return None
        rows = (
            (
                await session.execute(
                    text("""
            WITH ranked AS (
              SELECT variant_key,task_id,best_horizon,horizons_json,observed_at,
                row_number() OVER (PARTITION BY variant_key ORDER BY observed_at DESC,task_id DESC) AS position,
                count(*) OVER (PARTITION BY variant_key) AS total
              FROM alpha_variant_observations WHERE tenant_id=:tenant_id AND user_id=:user_id AND context_id=:context
            ) SELECT * FROM ranked WHERE position<=:limit ORDER BY variant_key,position
        """),
                    params,
                )
            )
            .mappings()
            .all()
        )
        states = (
            (
                await session.execute(
                    text("""
            SELECT s.variant_key,s.evidence_task_id,s.cooled_since,s.cooled_until,s.reset_at,
                o.best_horizon,o.horizons_json,o.observed_at
            FROM alpha_variant_states s JOIN alpha_variant_observations o
              ON o.tenant_id=s.tenant_id AND o.user_id=s.user_id AND o.context_id=s.context_id
              AND o.variant_key=s.variant_key AND o.task_id=s.evidence_task_id
            WHERE s.tenant_id=:tenant_id AND s.user_id=:user_id AND s.context_id=:context
              AND o.tenant_id=:tenant_id AND o.user_id=:user_id
        """),
                    params,
                )
            )
            .mappings()
            .all()
        )
        resets = (
            (
                await session.execute(
                    text("""
            SELECT variant_key,reason,created_at FROM alpha_variant_reset_events
            WHERE tenant_id=:tenant_id AND user_id=:user_id AND context_id=:context
            ORDER BY created_at DESC,event_id DESC LIMIT 50
        """),
                    params,
                )
            )
            .mappings()
            .all()
        )
    by_key, state_by_key = {}, {row["variant_key"]: row for row in states}
    for row in rows:
        by_key.setdefault(row["variant_key"], []).append(row)
    catalog = context["scope_json"].get("catalog") or variant_skeleton()
    moment = utc_now()
    variants = []
    for candidate in catalog:
        observations = by_key.get(candidate["key"], [])
        state = state_by_key.get(candidate["key"])
        recent = observations[0] if observations else None
        latest_stat = (
            next(
                (
                    item
                    for item in recent["horizons_json"]
                    if item["horizon"] == recent["best_horizon"]
                ),
                None,
            )
            if recent
            else None
        )
        variants.append(
            {
                **candidate,
                "observations_count": int(recent["total"]) if recent else 0,
                "latest_stat": latest_stat,
                "latest_at": to_utc_iso(recent["observed_at"]) if recent else None,
                "decision_stat": next(
                    (
                        item
                        for item in state["horizons_json"]
                        if item["horizon"] == state["best_horizon"]
                    ),
                    None,
                )
                if state
                else None,
                "cooling_active": bool(
                    state
                    and state["cooled_until"]
                    and state["cooled_until"] > moment
                    and candidate["coolable"]
                ),
                "cooled_until": to_utc_iso(state["cooled_until"]) if state else None,
                "reset_at": to_utc_iso(state["reset_at"]) if state else None,
                "observations": [
                    {
                        "task_id": item["task_id"],
                        "at": to_utc_iso(item["observed_at"]),
                        "best_horizon": item["best_horizon"],
                        "horizons": item["horizons_json"],
                    }
                    for item in observations
                ],
            }
        )
    return {
        "context_id": context_id,
        "scope": context["scope_json"],
        "updated_at": to_utc_iso(context["updated_at"]),
        "reset_events": [
            {
                "variant_key": item["variant_key"],
                "reason": item["reason"],
                "at": to_utc_iso(item["created_at"]),
            }
            for item in resets
        ],
        "constants": context["scope_json"].get("policy")
        or {
            "cooldown_days": COOLDOWN_DAYS,
            "high_conf_days": HIGH_CONF_DAYS,
            "cool_icir_floor": COOL_ICIR_FLOOR,
            "max_observations": MAX_OBSERVATIONS,
        },
        "variants": variants,
    }


async def reset_cooling(
    *, tenant_id: str, user_id: str, context_id: str, variant_key: str, reason: str
) -> dict | None:
    from backend.shared.database_manager_v2 import get_session

    owner = OwnerScope(tenant_id, user_id)
    if not valid_context_id(context_id) or not variant_key or len(variant_key) > 64:
        raise ValueError("Invalid variant cooling reset")
    reason = reason.strip()
    if not reason or len(reason) > 500:
        raise ValueError("请填写 1–500 字解除原因")
    params = {**owner.sql_params(), "context": context_id, "variant": variant_key}
    async with get_session() as session:
        context = (
            (
                await session.execute(
                    text("""
            SELECT scope_json FROM alpha_variant_contexts WHERE tenant_id=:tenant_id
              AND user_id=:user_id AND context_id=:context FOR UPDATE
        """),
                    params,
                )
            )
            .mappings()
            .first()
        )
        if context is None:
            return None
        catalog = context["scope_json"].get("catalog") or variant_skeleton()
        if not any(item["key"] == variant_key and item["coolable"] for item in catalog):
            raise ValueError("Protected or unknown variant cannot be reset")
        state = (
            (
                await session.execute(
                    text("""
            SELECT evidence_task_id,cooled_until FROM alpha_variant_states
            WHERE tenant_id=:tenant_id AND user_id=:user_id AND context_id=:context AND variant_key=:variant FOR UPDATE
        """),
                    params,
                )
            )
            .mappings()
            .first()
        )
        moment = utc_now()
        if not state or not state["cooled_until"] or state["cooled_until"] <= moment:
            return {
                "context_id": context_id,
                "variant_key": variant_key,
                "changed": False,
            }
        await session.execute(
            text("""
            UPDATE alpha_variant_states SET cooled_since=NULL,cooled_until=NULL,reset_at=:at,updated_at=:at
            WHERE tenant_id=:tenant_id AND user_id=:user_id AND context_id=:context AND variant_key=:variant
        """),
            {**params, "at": moment},
        )
        await session.execute(
            text("""
            INSERT INTO alpha_variant_reset_events
              (tenant_id,user_id,context_id,variant_key,event_id,evidence_task_id,reason,created_at)
            VALUES (:tenant_id,:user_id,:context,:variant,:event,:task,:reason,:at)
        """),
            {
                **params,
                "at": moment,
                "event": uuid.uuid4().hex,
                "task": state["evidence_task_id"],
                "reason": reason,
            },
        )
        await session.execute(
            text("""
            UPDATE alpha_variant_contexts SET updated_at=:at
            WHERE tenant_id=:tenant_id AND user_id=:user_id AND context_id=:context
        """),
            {**params, "at": moment},
        )
    return {"context_id": context_id, "variant_key": variant_key, "changed": True}
