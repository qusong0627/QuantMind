"""训练目标注册表（Postgres）：版本不可变、状态机、审计、内置种子。

边界：
- 本模块负责 DB 读写；纯字段契约/hash 在 `target_spec.py`。
- 训练容器不连 DB：API 在提交时把冻结 spec 写进 config.yaml，容器只校验 hash。
- 状态规则：新训练只允许 active；draft 仅管理员显式放行且强制 research_only；
  deprecated 只允许“同 hash 的历史重训”显式放行。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping

from sqlalchemy import text

from backend.shared.database_manager_v2 import get_session
from backend.shared.training.target_spec import (
    LabelTargetSpec,
    apply_overrides,
    spec_from_dict,
    spec_from_label_fields,
    validate_spec_semantics,
)

logger = logging.getLogger(__name__)


class TargetRegistryError(RuntimeError):
    """注册表错误。code 稳定；http_status 供 API 映射。"""

    def __init__(self, code: str, message: str, *, http_status: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


# ── 内置目标（CN / HK / US / FUTURES）────────────────────────────────────────


def _market_rank_targets(
    market: str, prefix: str, label: str
) -> tuple[dict[str, Any], ...]:
    """港股/美股/期货的标准目标族：T+1/T+3/T+5/T+10 rank + raw/top20/updown。

    P1 只给 CN 预置分位目标；其它市场若需分位，注册自定义 target（family=
    quantile_return，仅 CN 能力闸允许 LightGBM）。
    """
    specs: list[dict[str, Any]] = []
    for horizon in (1, 3, 5, 10):
        specs.append(
            {
                "target_id": f"{prefix}_ret_{horizon}d_open_rank",
                "version": 1,
                "market": market,
                "family": "forward_return",
                "mode": "regression",
                "horizon_days": horizon,
                "entry": {"lag_days": 1, "price": "open"},
                "exit": {"price": "close"},
                "transform": {
                    "type": "rank",
                    "top_pct": 0.2,
                    "drop_extreme_pct": 0.0,
                },
                "description": (
                    f"{label} T+1 开盘买、T+{horizon + 1} 收盘卖，"
                    f"{horizon} 日收益截面 rank"
                ),
            }
        )
    specs.extend(
        [
            {
                "target_id": f"{prefix}_ret_5d_open_raw",
                "version": 1,
                "market": market,
                "family": "forward_return",
                "mode": "regression",
                "horizon_days": 5,
                "entry": {"lag_days": 1, "price": "open"},
                "exit": {"price": "close"},
                "transform": {
                    "type": "raw",
                    "top_pct": 0.2,
                    "drop_extreme_pct": 0.0,
                },
                "description": f"{label} T+1 开盘买、T+6 收盘卖，5 日原始收益",
            },
            {
                "target_id": f"{prefix}_ret_5d_open_top20",
                "version": 1,
                "market": market,
                "family": "forward_return",
                "mode": "regression",
                "horizon_days": 5,
                "entry": {"lag_days": 1, "price": "open"},
                "exit": {"price": "close"},
                "transform": {
                    "type": "top_tail",
                    "top_pct": 0.2,
                    "drop_extreme_pct": 0.0,
                },
                "description": f"{label} 5 日收益只学当日最强 20% 尾部，其余置 0",
            },
            {
                "target_id": f"{prefix}_updown_5d_open",
                "version": 1,
                "market": market,
                "family": "classification_binary",
                "mode": "classification",
                "horizon_days": 5,
                "entry": {"lag_days": 1, "price": "open"},
                "exit": {"price": "close"},
                "transform": {
                    "type": "rank",
                    "top_pct": 0.2,
                    "drop_extreme_pct": 0.0,
                },
                "description": f"{label} 5 日收益方向二分类（>0 为 1）",
            },
        ]
    )
    return tuple(specs)


BUILTIN_TARGETS_V1: tuple[dict[str, Any], ...] = (
    {
        "target_id": "cn_ret_1d_open_rank",
        "version": 1,
        "market": "CN",
        "family": "forward_return",
        "mode": "regression",
        "horizon_days": 1,
        "entry": {"lag_days": 1, "price": "open"},
        "exit": {"price": "close"},
        "transform": {"type": "rank", "top_pct": 0.2, "drop_extreme_pct": 0.0},
        "description": "A股 T+1 开盘买、T+2 收盘卖，1 日收益截面 rank",
    },
    {
        "target_id": "cn_ret_5d_open_rank",
        "version": 1,
        "market": "CN",
        "family": "forward_return",
        "mode": "regression",
        "horizon_days": 5,
        "entry": {"lag_days": 1, "price": "open"},
        "exit": {"price": "close"},
        "transform": {"type": "rank", "top_pct": 0.2, "drop_extreme_pct": 0.0},
        "description": "A股 T+1 开盘买、T+6 收盘卖，5 日收益截面 rank",
    },
    {
        "target_id": "cn_ret_5d_open_raw",
        "version": 1,
        "market": "CN",
        "family": "forward_return",
        "mode": "regression",
        "horizon_days": 5,
        "entry": {"lag_days": 1, "price": "open"},
        "exit": {"price": "close"},
        "transform": {"type": "raw", "top_pct": 0.2, "drop_extreme_pct": 0.0},
        "description": "A股 T+1 开盘买、T+6 收盘卖，5 日原始收益",
    },
    {
        "target_id": "cn_ret_5d_open_top20",
        "version": 1,
        "market": "CN",
        "family": "forward_return",
        "mode": "regression",
        "horizon_days": 5,
        "entry": {"lag_days": 1, "price": "open"},
        "exit": {"price": "close"},
        "transform": {"type": "top_tail", "top_pct": 0.2, "drop_extreme_pct": 0.0},
        "description": "A股 5 日收益只学当日最强 20% 尾部，其余置 0",
    },
    {
        "target_id": "cn_updown_5d_open",
        "version": 1,
        "market": "CN",
        "family": "classification_binary",
        "mode": "classification",
        "horizon_days": 5,
        "entry": {"lag_days": 1, "price": "open"},
        "exit": {"price": "close"},
        "transform": {"type": "rank", "top_pct": 0.2, "drop_extreme_pct": 0.0},
        "description": "A股 5 日收益方向二分类（>0 为 1）",
    },
    {
        "target_id": "cn_ret_5d_open_quantile",
        "version": 1,
        "market": "CN",
        "family": "quantile_return",
        "mode": "quantile",
        "horizon_days": 5,
        "entry": {"lag_days": 1, "price": "open"},
        "exit": {"price": "close"},
        "transform": {"type": "raw", "top_pct": 0.2, "drop_extreme_pct": 0.0},
        "description": "A股 5 日收益 p10/p50/p90 分位回归（仅 CN 单 LightGBM）",
    },
    {
        "target_id": "cn_ret_3d_open_rank",
        "version": 1,
        "market": "CN",
        "family": "forward_return",
        "mode": "regression",
        "horizon_days": 3,
        "entry": {"lag_days": 1, "price": "open"},
        "exit": {"price": "close"},
        "transform": {"type": "rank", "top_pct": 0.2, "drop_extreme_pct": 0.0},
        "description": "A股 T+1 开盘买、T+4 收盘卖，3 日收益截面 rank",
    },
    {
        "target_id": "cn_ret_10d_open_rank",
        "version": 1,
        "market": "CN",
        "family": "forward_return",
        "mode": "regression",
        "horizon_days": 10,
        "entry": {"lag_days": 1, "price": "open"},
        "exit": {"price": "close"},
        "transform": {"type": "rank", "top_pct": 0.2, "drop_extreme_pct": 0.0},
        "description": "A股 T+1 开盘买、T+11 收盘卖，10 日收益截面 rank",
    },
    *_market_rank_targets("HK", "hk", "港股"),
    *_market_rank_targets("US", "us", "美股"),
    *_market_rank_targets("FUTURES", "fut", "期货"),
)


def builtin_specs() -> tuple[LabelTargetSpec, ...]:
    """校验并返回内置 spec；任何内置定义非法都在首次调用时炸。"""
    out: list[LabelTargetSpec] = []
    for raw in BUILTIN_TARGETS_V1:
        spec = spec_from_dict(raw)
        if spec.version < 1:
            raise TargetRegistryError(
                "target_version_invalid", f"内置目标 {spec.target_id} version 必须 >=1"
            )
        out.append(spec)
    return tuple(out)


async def ensure_builtin_targets(session, *, force: bool = False) -> None:
    """幂等种子：同 id/version 已存在时校验 hash，不允许静默覆盖。

    不做进程内缓存：调用点只有目标读取/解析（每次训练一次），29 条幂等 upsert
    的成本远低于“缓存已置位但事务回滚”导致的静默缺种子风险。
    `force` 保留给测试/运维显式重放（当前实现总是重放）。
    """
    _ = force
    for spec in builtin_specs():
        payload = spec.canonical_dict()
        digest = spec.spec_hash()
        await session.execute(
            text(
                """
                INSERT INTO qm_label_targets
                    (id, version, market, family, mode, spec, spec_hash,
                     status, created_by, activated_by, activated_at)
                VALUES
                    (:id, :version, :market, :family, :mode,
                     CAST(:spec AS JSONB), :spec_hash,
                     'active', 'system', 'system', now())
                ON CONFLICT (id, version) DO NOTHING
                """
            ),
            {
                "id": spec.target_id,
                "version": spec.version,
                "market": spec.market,
                "family": spec.family,
                "mode": spec.mode,
                "spec": json.dumps(payload, ensure_ascii=False),
                "spec_hash": digest,
            },
        )
        row = (
            await session.execute(
                text(
                    "SELECT spec_hash FROM qm_label_targets "
                    "WHERE id=:id AND version=:version"
                ),
                {"id": spec.target_id, "version": spec.version},
            )
        ).mappings().one()
        if str(row["spec_hash"]) != digest:
            raise TargetRegistryError(
                "target_seed_hash_mismatch",
                f"内置目标 {spec.target_id}@{spec.version} 已存在但 hash 不一致；"
                "改口径必须升 version，不能原地改内置定义",
                http_status=409,
            )


def _row_to_spec(row: Mapping[str, Any]) -> LabelTargetSpec:
    raw = row["spec"]
    data = json.loads(raw) if isinstance(raw, str) else dict(raw)
    spec = spec_from_dict(data)
    stored_hash = str(row["spec_hash"])
    actual = spec.spec_hash()
    if stored_hash != actual:
        raise TargetRegistryError(
            "target_spec_hash_mismatch",
            f"注册表 {spec.target_id}@{spec.version} 的 spec_hash 与内容不一致",
            http_status=500,
        )
    # 行上的冗余列必须与 spec 一致（防止手工改库绕过触发器）
    for col, expected in (
        ("id", spec.target_id),
        ("version", spec.version),
        ("market", spec.market),
        ("family", spec.family),
        ("mode", spec.mode),
    ):
        if str(row[col]) != str(expected):
            raise TargetRegistryError(
                "target_spec_field_mismatch",
                f"注册表行 {col}={row[col]!r} 与 spec={expected!r} 不一致",
                http_status=500,
            )
    return spec


def parse_target_ref(ref: Mapping[str, Any] | None) -> tuple[str, int]:
    if not isinstance(ref, Mapping):
        raise TargetRegistryError(
            "target_ref_invalid", "target_ref 必须是 {id, version} 对象"
        )
    target_id = str(ref.get("id") or ref.get("target_id") or "").strip()
    if not target_id:
        raise TargetRegistryError("target_ref_invalid", "target_ref.id 不能为空")
    version_raw = ref.get("version")
    if version_raw is None:
        raise TargetRegistryError(
            "target_version_required", "target_ref.version 必填（不接受“最新版”猜测）"
        )
    try:
        version = int(version_raw)
    except (TypeError, ValueError):
        raise TargetRegistryError(
            "target_ref_invalid", f"target_ref.version 非法: {version_raw!r}"
        ) from None
    if version < 1:
        raise TargetRegistryError(
            "target_ref_invalid", "target_ref.version 必须 >=1"
        )
    return target_id, version


async def list_targets(
    *,
    market: str | None = None,
    status: str | None = None,
    family: str | None = None,
) -> list[dict[str, Any]]:
    async with get_session() as session:
        await ensure_builtin_targets(session)
        clauses: list[str] = []
        params: dict[str, Any] = {}
        if market:
            clauses.append("market = :market")
            params["market"] = str(market).strip().upper()
        if status:
            clauses.append("status = :status")
            params["status"] = str(status).strip().lower()
        if family:
            clauses.append("family = :family")
            params["family"] = str(family).strip().lower()
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = (
            await session.execute(
                text(
                    "SELECT id, version, market, family, mode, spec, spec_hash, "
                    "status, created_by, created_at, activated_by, activated_at, "
                    "deprecated_by, deprecated_at, note "
                    f"FROM qm_label_targets {where} "
                    "ORDER BY market, family, id, version"
                ),
                params,
            )
        ).mappings().all()
    return [_serialize_row(row) for row in rows]


def _serialize_row(row: Mapping[str, Any]) -> dict[str, Any]:
    spec = _row_to_spec(row)
    return {
        "id": spec.target_id,
        "version": spec.version,
        "market": spec.market,
        "family": spec.family,
        "mode": spec.mode,
        "status": row["status"],
        "spec_hash": row["spec_hash"],
        "spec": spec.canonical_dict(),
        "created_by": row.get("created_by"),
        "created_at": _iso(row.get("created_at")),
        "activated_by": row.get("activated_by"),
        "activated_at": _iso(row.get("activated_at")),
        "deprecated_by": row.get("deprecated_by"),
        "deprecated_at": _iso(row.get("deprecated_at")),
        "note": row.get("note"),
    }


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return value.isoformat()
    except AttributeError:
        return str(value)


async def get_target(target_id: str, version: int) -> dict[str, Any]:
    async with get_session() as session:
        await ensure_builtin_targets(session)
        row = (
            await session.execute(
                text(
                    "SELECT id, version, market, family, mode, spec, spec_hash, "
                    "status, created_by, created_at, activated_by, activated_at, "
                    "deprecated_by, deprecated_at, note "
                    "FROM qm_label_targets WHERE id=:id AND version=:version"
                ),
                {"id": target_id, "version": int(version)},
            )
        ).mappings().first()
    if row is None:
        raise TargetRegistryError(
            "target_not_found",
            f"目标 {target_id}@{version} 不存在",
            http_status=404,
        )
    return _serialize_row(row)


async def create_draft(
    spec: LabelTargetSpec, *, actor: str, note: str | None = None
) -> dict[str, Any]:
    """创建 draft version。幂等：同内容重复创建返回既有行；不同内容 409。"""
    validate_spec_semantics(spec)
    if spec.version < 1:
        raise TargetRegistryError(
            "target_version_invalid", "落库目标 version 必须 >=1"
        )
    payload = spec.canonical_dict()
    digest = spec.spec_hash()
    async with get_session() as session:
        result = await session.execute(
            text(
                """
                INSERT INTO qm_label_targets
                    (id, version, market, family, mode, spec, spec_hash,
                     status, created_by, note)
                VALUES
                    (:id, :version, :market, :family, :mode,
                     CAST(:spec AS JSONB), :spec_hash,
                     'draft', :actor, :note)
                ON CONFLICT (id, version) DO NOTHING
                RETURNING id
                """
            ),
            {
                "id": spec.target_id,
                "version": spec.version,
                "market": spec.market,
                "family": spec.family,
                "mode": spec.mode,
                "spec": json.dumps(payload, ensure_ascii=False),
                "spec_hash": digest,
                "actor": actor,
                "note": note,
            },
        )
        inserted = result.first() is not None
        if not inserted:
            existing = (
                await session.execute(
                    text(
                        "SELECT spec_hash, status FROM qm_label_targets "
                        "WHERE id=:id AND version=:version"
                    ),
                    {"id": spec.target_id, "version": spec.version},
                )
            ).mappings().first()
            if existing is None:  # pragma: no cover - 并发窗口兜底
                raise TargetRegistryError(
                    "target_registry_conflict", "目标创建冲突，请重试", http_status=409
                )
            if str(existing["spec_hash"]) != digest:
                raise TargetRegistryError(
                    "target_version_immutable",
                    f"目标 {spec.target_id}@{spec.version} 已存在且内容不同；"
                    "改口径必须升 version",
                    http_status=409,
                )
        else:
            await _audit(
                session,
                spec.target_id,
                spec.version,
                "create",
                actor,
                detail={"note": note, "spec_hash": digest},
            )
        await session.commit()
    return await get_target(spec.target_id, spec.version)


async def activate(target_id: str, version: int, *, actor: str) -> dict[str, Any]:
    return await _transition(
        target_id, version, from_status="draft", to_status="active", actor=actor
    )


async def deprecate(target_id: str, version: int, *, actor: str) -> dict[str, Any]:
    async with get_session() as session:
        row = (
            await session.execute(
                text(
                    "SELECT status FROM qm_label_targets "
                    "WHERE id=:id AND version=:version"
                ),
                {"id": target_id, "version": int(version)},
            )
        ).mappings().first()
        if row is None:
            raise TargetRegistryError(
                "target_not_found", f"目标 {target_id}@{version} 不存在", http_status=404
            )
    if row["status"] == "deprecated":
        return await get_target(target_id, version)
    from_status = "active" if row["status"] == "active" else "draft"
    return await _transition(
        target_id, version, from_status=from_status, to_status="deprecated", actor=actor
    )


async def _transition(
    target_id: str,
    version: int,
    *,
    from_status: str,
    to_status: str,
    actor: str,
) -> dict[str, Any]:
    async with get_session() as session:
        if to_status == "active":
            result = await session.execute(
                text(
                    "UPDATE qm_label_targets "
                    "SET status='active', activated_by=:actor, activated_at=now() "
                    "WHERE id=:id AND version=:version AND status=:from "
                    "RETURNING id"
                ),
                {
                    "id": target_id,
                    "version": int(version),
                    "from": from_status,
                    "actor": actor,
                },
            )
        elif to_status == "deprecated":
            result = await session.execute(
                text(
                    "UPDATE qm_label_targets "
                    "SET status='deprecated', deprecated_by=:actor, deprecated_at=now() "
                    "WHERE id=:id AND version=:version AND status=:from "
                    "RETURNING id"
                ),
                {
                    "id": target_id,
                    "version": int(version),
                    "from": from_status,
                    "actor": actor,
                },
            )
        else:  # pragma: no cover - 调用点只有两个合法目标状态
            raise TargetRegistryError(
                "target_status_invalid", f"非法目标状态: {to_status}"
            )
        if result.first() is None:
            raise TargetRegistryError(
                "target_status_invalid",
                f"目标 {target_id}@{version} 当前不是 {from_status}，不能转 {to_status}",
                http_status=409,
            )
        await _audit(
            session,
            target_id,
            int(version),
            "activate" if to_status == "active" else "deprecate",
            actor,
        )
        await session.commit()
    return await get_target(target_id, version)


async def _audit(
    session,
    target_id: str,
    version: int,
    action: str,
    actor: str,
    *,
    job_id: str | None = None,
    detail: Mapping[str, Any] | None = None,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO qm_label_target_audit
                (target_id, target_version, action, actor, job_id, detail)
            VALUES (:id, :version, :action, :actor, :job_id,
                    CAST(:detail AS JSONB))
            """
        ),
        {
            "id": target_id,
            "version": int(version),
            "action": action,
            "actor": actor,
            "job_id": job_id,
            "detail": json.dumps(dict(detail or {}), ensure_ascii=False),
        },
    )


async def record_blocked_resolve(
    *,
    target_id: str,
    version: int,
    actor: str,
    job_id: str | None,
    code: str,
    message: str,
) -> None:
    """提交被能力/状态闸拒绝时写审计；失败只告警，不阻断 422 返回。"""
    try:
        async with get_session() as session:
            await _audit(
                session,
                target_id,
                int(version),
                "blocked_resolve",
                actor,
                job_id=job_id,
                detail={"code": code, "message": message[:500]},
            )
            await session.commit()
    except Exception:  # noqa: BLE001 - 审计失败不得吞掉原始 422
        logger.warning("target blocked_resolve audit failed", exc_info=True)


async def resolve_for_training(
    ref: Mapping[str, Any],
    *,
    market: str,
    allow_draft: bool = False,
    allow_deprecated: bool = False,
) -> tuple[LabelTargetSpec, str]:
    """解析 target_ref；返回 (spec, status)。状态/市场不符即抛错。"""
    target_id, version = parse_target_ref(ref)
    row = await get_target(target_id, version)
    status = str(row["status"])
    if str(row["market"]).upper() != str(market or "").upper():
        raise TargetRegistryError(
            "target_market_mismatch",
            f"目标 {target_id}@{version} market={row['market']}，"
            f"训练 market={str(market).upper()}",
        )
    if status == "draft" and not allow_draft:
        raise TargetRegistryError(
            "target_not_active",
            f"目标 {target_id}@{version} 仍是 draft；生产训练只允许 active",
        )
    if status == "deprecated" and not allow_deprecated:
        raise TargetRegistryError(
            "target_deprecated",
            f"目标 {target_id}@{version} 已 deprecated；新训练请用新版本",
        )
    if status not in {"active", "draft", "deprecated"}:  # pragma: no cover
        raise TargetRegistryError(
            "target_status_invalid", f"目标状态非法: {status}", http_status=500
        )
    return spec_from_dict(row["spec"]), status


async def resolve_target_for_payload(
    payload: Mapping[str, Any],
    *,
    market: str,
    allow_draft: bool = False,
    allow_deprecated: bool = False,
) -> dict[str, Any]:
    """提交链路入口：registry 或 legacy → 冻结 target_meta。

    返回字段：
      id/version/status/source/base_hash/base_spec
    `label_overrides` 由调用方在 base_spec 上应用后计算 effective_hash；
    本函数不处理训练选项（prediction_mode/sample_weight 等）。
    """
    ref = payload.get("target_ref")
    if ref:
        spec, status = await resolve_for_training(
            ref,
            market=market,
            allow_draft=allow_draft,
            allow_deprecated=allow_deprecated,
        )
        return {
            "id": spec.target_id,
            "version": spec.version,
            "status": status,
            "source": "registry",
            "base_hash": spec.spec_hash(),
            "base_spec": spec.canonical_dict(),
        }
    spec = spec_from_label_fields(payload, market=market)
    return {
        "id": "legacy",
        "version": 0,
        "status": "legacy",
        "source": "legacy",
        "base_hash": spec.spec_hash(),
        "base_spec": spec.canonical_dict(),
    }


def apply_payload_overrides(
    base_spec: Mapping[str, Any], overrides: Mapping[str, Any] | None
) -> LabelTargetSpec:
    """把 label_overrides 应用到冻结 base spec，返回重新校验后的 effective spec。"""
    return apply_overrides(spec_from_dict(base_spec), overrides)


__all__ = [
    "BUILTIN_TARGETS_V1",
    "TargetRegistryError",
    "activate",
    "apply_payload_overrides",
    "builtin_specs",
    "create_draft",
    "deprecate",
    "ensure_builtin_targets",
    "get_target",
    "list_targets",
    "parse_target_ref",
    "record_blocked_resolve",
    "resolve_for_training",
    "resolve_target_for_payload",
]
