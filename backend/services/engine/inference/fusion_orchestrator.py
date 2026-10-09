"""机构级融合模型编排层 —— preview / create 的共享证据与权重组装。

设计见 docs/机构级模型融合_设计方案.md §5/§9（P1 第 3 步）。

职责：
1. 成员解析与硬校验：≥2、存在、ready/active、不重复、同一市场。
2. 证据（fusion_quality）→ 权重引擎（fusion_weights）→ OOS 回放（compute_replay）
   的完整链路；产出 API 响应与 register_ensemble_model 落盘所需的全部工件
   （weights_override / weight_diagnostics / fusion_eval / horizon）。

失败姿态：
- 证据组装失败 fail-open：成员分数尚未积累（新模型）不阻断创建，权重要么由
  收缩先验退化等权、要么为用户 manual 指定，warning 随响应显式返回；
- 成员/权重类硬错误一律 ValueError 上抛，由端点转 4xx。

「创建即日更」（qm_model_inference_settings(enabled=TRUE)）由
register_ensemble_model 落库时单点写入，见该函数。
"""

from __future__ import annotations

import json
import logging
import math
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from backend.services.engine.inference.fusion_quality import (
    MemberSpec,
    compute_replay,
    gather_fusion_evidence,
    read_model_horizon,
)
from backend.shared.fusion_weights import (
    STRATEGY_EQUAL,
    STRATEGY_ICIR_SHRUNK,
    STRATEGY_MANUAL,
    FusionWeightConfig,
    compute_fusion_weights,
)

logger = logging.getLogger(__name__)

__all__ = [
    "FusionMember",
    "build_fusion_preview",
    "create_fusion_model",
    "resolve_fusion_members",
]

_ALLOWED_STRATEGIES = (STRATEGY_ICIR_SHRUNK, STRATEGY_EQUAL, STRATEGY_MANUAL, "recent_ic")
_READY_STATUSES = {"ready", "active"}
_CORR_WARN_THRESHOLD = 0.95
_DEFAULT_HORIZON = 5


@dataclass(frozen=True)
class FusionMember:
    """已解析并校验过的融合成员（registry 行为在此收敛为不可变快照）。"""

    model_id: str
    display_name: str
    model_type: str
    market: str
    horizon_days: int
    model_dir: str


def _parse_meta(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


async def resolve_fusion_members(
    *, model_ids: list[str], tenant_id: str, user_id: str
) -> list[FusionMember]:
    """解析成员模型并做硬校验；任何不满足项直接 ValueError（端点转 4xx）。"""
    from backend.shared.model_registry import model_registry_service

    cleaned = [str(m).strip() for m in (model_ids or []) if str(m).strip()]
    if len(cleaned) < 2:
        raise ValueError("融合至少需要 2 个成员模型")
    if len(set(cleaned)) != len(cleaned):
        raise ValueError("融合成员不可重复")

    members: list[FusionMember] = []
    for mid in cleaned:
        row = await model_registry_service.get_model(
            tenant_id=tenant_id, user_id=user_id, model_id=mid
        )
        if not row:
            raise ValueError(f"成员模型不存在: {mid}")
        status = str(row.get("status") or "")
        if status not in _READY_STATUSES:
            raise ValueError(f"成员模型 {mid} 状态为 {status or '未知'}，需为 ready/active")
        meta = _parse_meta(row.get("metadata_json"))
        ctx = meta.get("context") if isinstance(meta.get("context"), dict) else {}
        market = str(meta.get("market") or ctx.get("market") or "CN").upper()
        model_dir = str(row.get("storage_path") or "")
        horizon = read_model_horizon(model_dir) if model_dir else _DEFAULT_HORIZON
        members.append(
            FusionMember(
                model_id=mid,
                display_name=str(
                    meta.get("display_name") or meta.get("model_name") or mid
                ),
                model_type=str(meta.get("model_type") or ""),
                market=market,
                horizon_days=int(horizon),
                model_dir=model_dir,
            )
        )

    markets = {m.market for m in members}
    if len(markets) > 1:
        raise ValueError(
            "融合成员必须属于同一市场（截面口径不同不可混融）: "
            + "/".join(sorted(markets))
        )
    return members


def _fused_horizon(members: list[FusionMember]) -> int:
    """融合周期 = 成员周期众数（平局取先出现的成员序，确定性）。"""
    return int(statistics.mode([m.horizon_days for m in members]))


def _finite(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _replay_verdict(summary: dict[str, dict[str, float]]) -> dict[str, Any] | None:
    """融合 vs 成员的 OOS 结论（前端红字警告的数据面）。证据不足 → None。"""
    fused = summary.get("fused") or {}
    fused_icir = _finite(fused.get("icir"))
    fused_days = int(fused.get("n_days") or 0)
    if fused_icir is None or fused_days <= 0:
        return None
    member_icirs = [
        v
        for mid, s in summary.items()
        if mid != "fused" and int(s.get("n_days") or 0) > 0
        for v in (_finite(s.get("icir")),)
        if v is not None
    ]
    if not member_icirs:
        return None
    best = max(member_icirs)
    median = statistics.median(member_icirs)
    return {
        "fused_icir": fused_icir,
        "fused_ic_days": fused_days,
        "best_member_icir": round(best, 6),
        "median_member_icir": round(median, 6),
        "beats_best": fused_icir > best,
        "beats_median": fused_icir > median,
    }


def _corr_warnings(corr: dict[str, dict[str, float]] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for a, row in (corr or {}).items():
        for b, c in (row or {}).items():
            key = tuple(sorted((str(a), str(b))))
            if a == b or key in seen:
                continue
            seen.add(key)
            val = _finite(c)
            if val is not None and val > _CORR_WARN_THRESHOLD:
                out.append({"a": key[0], "b": key[1], "corr": round(val, 4)})
    return out


async def build_fusion_preview(
    *,
    tenant_id: str,
    user_id: str,
    model_ids: list[str],
    weight_strategy: str = STRATEGY_ICIR_SHRUNK,
    manual_weights: dict[str, float] | None = None,
    config: FusionWeightConfig | None = None,
) -> dict[str, Any]:
    """无副作用预览：成员统计 + 相关性 + 权重与诊断 + OOS 回放结论。"""
    members = await resolve_fusion_members(
        model_ids=model_ids, tenant_id=tenant_id, user_id=user_id
    )
    ws = str(weight_strategy or STRATEGY_ICIR_SHRUNK).strip().lower()
    if ws not in _ALLOWED_STRATEGIES:
        raise ValueError(f"weight_strategy 应为 {'/'.join(_ALLOWED_STRATEGIES)}")
    if ws == STRATEGY_MANUAL and not manual_weights:
        raise ValueError("manual 策略必须提供 manual_weights")

    warnings: list[str] = []
    horizons = [m.horizon_days for m in members]
    if len(set(horizons)) > 1:
        warnings.append(f"horizon_mismatch: {sorted(set(horizons))}")
    fused_horizon = _fused_horizon(members)

    specs = [
        MemberSpec(
            model_id=m.model_id,
            horizon_days=m.horizon_days,
            market=m.market,
            model_dir=m.model_dir or None,
        )
        for m in members
    ]
    evidence = None
    try:
        evidence = await gather_fusion_evidence(
            specs, tenant_id=tenant_id, user_id=user_id
        )
        warnings.extend(str(w) for w in evidence.warnings)
    except Exception as exc:  # noqa: BLE001 —— 证据不可得时降级预览而非阻断
        logger.warning("融合证据组装失败（降级等权预览）: %s", exc, exc_info=True)
        warnings.append(f"evidence_unavailable: {exc}")

    ic_map = (
        evidence.member_ic_series()
        if evidence is not None
        else {m.model_id: [] for m in members}
    )
    corr = evidence.corr if evidence is not None else None
    if ws == STRATEGY_MANUAL:
        cfg = FusionWeightConfig(strategy=STRATEGY_MANUAL, manual_weights=manual_weights)
    elif ws == STRATEGY_EQUAL:
        cfg = FusionWeightConfig(strategy=STRATEGY_EQUAL)
    else:
        cfg = config if config is not None else FusionWeightConfig(strategy=ws)
    fw = compute_fusion_weights(ic_map, corr, cfg)
    if fw.warning:
        warnings.append(fw.warning)

    replay_payload: dict[str, Any] | None = None
    if evidence is not None:
        try:
            rep = compute_replay(evidence, fw.weights, target_horizon=fused_horizon)
            summary_json = {
                key: {
                    "ic_mean": _finite(s.get("ic_mean")),
                    "icir": _finite(s.get("icir")),
                    "n_days": int(s.get("n_days") or 0),
                }
                for key, s in rep.summary.items()
            }
            replay_payload = {
                "dates": len(rep.dates),
                "summary": summary_json,
                "verdict": _replay_verdict(rep.summary),
            }
        except Exception as exc:  # noqa: BLE001 —— 回放失败不阻断创建
            logger.warning("融合 OOS 回放失败: %s", exc, exc_info=True)
            warnings.append(f"replay_failed: {exc}")

    diag_by_id = {d.member_id: d for d in fw.diagnostics}
    replay_summary = (replay_payload or {}).get("summary") or {}
    member_rows: list[dict[str, Any]] = []
    for m in members:
        d = diag_by_id.get(m.model_id)
        rep_row = replay_summary.get(m.model_id) or {}
        member_rows.append(
            {
                "model_id": m.model_id,
                "display_name": m.display_name,
                "model_type": m.model_type,
                "market": m.market,
                "horizon_days": m.horizon_days,
                "ic_days": d.n_days if d else 0,
                "ic_mean": _finite(d.ic_mean) if d else None,
                "icir": _finite(d.icir) if d else None,
                "replay_icir": rep_row.get("icir"),
                "weight": fw.weights.get(m.model_id),
            }
        )

    return {
        "status": "success",
        "as_of": datetime.now(timezone.utc).date().isoformat(),
        "market": members[0].market,
        "horizon_days": fused_horizon,
        "weight_strategy": fw.strategy,
        "weights": {k: _finite(v) for k, v in fw.weights.items()},
        "diagnostics": [asdict(d) for d in fw.diagnostics],
        "members": member_rows,
        "corr": corr or {},
        "corr_warnings": _corr_warnings(corr),
        "replay": replay_payload,
        "warnings": warnings,
    }


async def create_fusion_model(
    *,
    tenant_id: str,
    user_id: str,
    model_ids: list[str],
    display_name: str = "",
    weight_strategy: str = STRATEGY_ICIR_SHRUNK,
    manual_weights: dict[str, float] | None = None,
    fusion_strategy: str = "linear",
    strategy_config: dict[str, float] | None = None,
    config: FusionWeightConfig | None = None,
) -> dict[str, Any]:
    """预览（服务器权威口径）→ 注册落库 + 落盘工件 + 写日更名单。"""
    preview = await build_fusion_preview(
        tenant_id=tenant_id,
        user_id=user_id,
        model_ids=model_ids,
        weight_strategy=weight_strategy,
        manual_weights=manual_weights,
        config=config,
    )
    from backend.shared.model_registry import model_registry_service

    created = await model_registry_service.register_ensemble_model(
        tenant_id=tenant_id,
        user_id=user_id,
        source_model_ids=[m["model_id"] for m in preview["members"]],
        display_name=display_name,
        weight_strategy=preview["weight_strategy"],
        manual_weights=manual_weights,
        fusion_strategy=fusion_strategy,
        strategy_config=strategy_config,
        weights_override=preview["weights"],
        weight_diagnostics=preview["diagnostics"],
        fusion_eval=preview["replay"],
        target_horizon_days=preview["horizon_days"],
    )
    return {**created, "preview": preview}
