"""融合模型权重周期刷新（P2-4；设计《机构级模型融合_设计方案》§7）。

beat 每日 22:30 扫描全部融合模型 → 拉成员证据（``gather_fusion_evidence``，
与创建期预览同一实现）→ 权重引擎（``compute_fusion_weights``，与预览/创建
同一实现）→ 与现快照比对：最大权重变动 < ``MIN_WEIGHT_DELTA``（默认 0.02）
不落盘（防抖——权重是连续量，逐日写盘只会制造无意义的抖动流水）→ 原子写
``weight_snapshot.json`` + append ``weight_history.jsonl``（event=refreshed）。
防抖阈值下「实际写盘」自然呈周更节律。

历史：``recent_ic`` 的每日刷新任务随 6c469eb7 删除，此后权重是**创建时快照**；
本模块为 P2-4 重建——复用同一引擎，但把刷新语义收敛在下列纪律里。

与创建期预览的**刻意不对称**（写入面 vs 只读面）：
- 预览是只读面：证据拉不到可降级等权预览（用户看着数字做决定，不落盘）；
- 刷新是写入面：证据不可得**绝不**降级重算——降级写盘 = 把等权当「重算结果」
  静默改掉生产权重。拉不到就跳过该模型留原因，下个 tick 重试。

其余纪律：
- manual 是用户意图、equal 无参可变，两者跳过（刷新无语义）；
- 成员解析失败（源模型被删/未就绪/跨市场）→ 跳过，绝不缺员重算
  （缺员重算 = 静默改变融合构成）；
- 单模型失败不拖垮其余；心跳与告警在任务层
  （``celery_tasks.refresh_fusion_weights_task``）。
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.services.engine.inference.fusion_orchestrator import (
    resolve_fusion_members,
)
from backend.services.engine.inference.fusion_quality import (
    MemberSpec,
    gather_fusion_evidence,
)
from backend.shared.fusion_weights import (
    STRATEGY_EQUAL,
    STRATEGY_ICIR_SHRUNK,
    STRATEGY_MANUAL,
    FusionWeightConfig,
    compute_fusion_weights,
)

logger = logging.getLogger(__name__)

#: 防抖阈值：最大权重变动小于该值不落盘（设计 §7 的 0.02）。
MIN_WEIGHT_DELTA = 0.02

#: 可自动刷新的策略（引擎可无参重算）。别名 recent_ic 在引擎内归一到 icir_shrunk；
#: 其余（manual/equal/历史 icir 初值策略/未知值）一律跳过并留原因——宁可不动，
#: 不猜。
_REFRESHABLE_STRATEGIES = frozenset({STRATEGY_ICIR_SHRUNK, "recent_ic"})


def _skip(model_id: str, status: str, reason: str) -> dict[str, Any]:
    return {"model_id": model_id, "status": status, "reason": reason}


def _max_abs_delta(old: dict[str, float], new: dict[str, float]) -> float:
    """最大绝对权重变动；无旧快照视为无穷（必落盘）。

    成员增删（键只在一边）按整段变动计——成员构成变了必须写盘留痕，
    绝不能因「交集键都没怎么动」被防抖吞掉。
    """
    if not old:
        return math.inf
    keys = set(old) | set(new)
    return max(abs(float(new.get(k, 0.0)) - float(old.get(k, 0.0))) for k in keys)


def _read_snapshot_weights(path: Path) -> dict[str, float]:
    """读现快照权重：v2 取 ``weights`` 段，v1 平铺 {model_id: weight} 兼容；
    读不了返回 {}（→ 视为全变动，照常落盘）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    raw = data.get("weights")
    if not isinstance(raw, dict):
        raw = data
    out: dict[str, float] = {}
    for k, v in raw.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        fv = float(v)
        if math.isfinite(fv):
            out[str(k)] = fv
    return out


def _write_snapshot(
    model_dir: Path,
    *,
    strategy: str,
    weights: dict[str, float],
    diagnostics: list[dict[str, Any]],
    max_delta: float,
) -> None:
    """原子写快照 + 追加历史行（形态与 register_ensemble_model 完全一致）。"""
    now = datetime.now(timezone.utc)
    snapshot = {
        "version": 2,
        "as_of": now.date().isoformat(),
        "strategy": str(strategy),
        "weights": {str(k): round(float(v), 6) for k, v in weights.items()},
        "diagnostics": diagnostics or [],
        "updated_at": now.isoformat(),
    }
    snap_path = model_dir / "weight_snapshot.json"
    snap_tmp = snap_path.with_suffix(".json.tmp")
    snap_tmp.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    snap_tmp.replace(snap_path)
    with (model_dir / "weight_history.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "as_of": snapshot["as_of"],
                    "strategy": snapshot["strategy"],
                    "weights": snapshot["weights"],
                    "event": "refreshed",
                    "max_delta": None
                    if math.isinf(max_delta)
                    else round(float(max_delta), 6),
                },
                ensure_ascii=False,
            )
            + "\n"
        )


async def _scan_ensembles() -> list[dict[str, Any]]:
    from backend.shared.model_registry import model_registry_service

    return await model_registry_service.scan_ensemble_models()


async def _resolve_members(model_ids: list[str], tenant_id: str, user_id: str):
    return await resolve_fusion_members(
        model_ids=model_ids, tenant_id=tenant_id, user_id=user_id
    )


async def _gather_evidence(specs: list[MemberSpec], tenant_id: str, user_id: str):
    return await gather_fusion_evidence(specs, tenant_id=tenant_id, user_id=user_id)


async def _refresh_one(
    row: dict[str, Any], *, min_delta: float, dry_run: bool
) -> dict[str, Any]:
    model_id = str(row.get("model_id") or "")
    tenant_id = str(row.get("tenant_id") or "default")
    user_id = str(row.get("user_id") or "")
    raw_meta = row.get("metadata_json")
    meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}

    raw_strategy = meta.get("weight_strategy")
    strategy = str(raw_strategy).strip().lower() if raw_strategy else ""
    if not strategy:
        # 不把缺失默认成 equal 再按 equal 报原因——缺字段与「显式等权」是两回事
        return _skip(
            model_id,
            "skipped_no_strategy",
            "元数据缺 weight_strategy，无法判定刷新语义",
        )
    if strategy == STRATEGY_MANUAL:
        return _skip(model_id, "skipped_manual", "manual 权重是用户意图，不自动改写")
    if strategy == STRATEGY_EQUAL:
        return _skip(model_id, "skipped_equal", "equal 策略无参可变，无需刷新")
    if strategy not in _REFRESHABLE_STRATEGIES:
        return _skip(
            model_id,
            "skipped_strategy",
            f"策略 {strategy!r} 不在可刷新集 {sorted(_REFRESHABLE_STRATEGIES)}",
        )

    source_ids = [
        str(x).strip() for x in (meta.get("source_model_ids") or []) if str(x).strip()
    ]
    if len(set(source_ids)) < 2:
        return _skip(
            model_id,
            "skipped_legacy",
            "缺 source_model_ids（旧版融合记录），无法定位成员；重建融合模型后纳入刷新",
        )

    storage_path = str(row.get("storage_path") or "").strip()
    model_dir = Path(storage_path)
    if not storage_path or not model_dir.is_dir():
        return _skip(
            model_id, "skipped_dir", f"模型目录不存在: {storage_path or '(空)'}"
        )

    try:
        members = await _resolve_members(source_ids, tenant_id, user_id)
    except ValueError as exc:
        return _skip(model_id, "skipped_members", f"成员解析失败（不缺席重算）: {exc}")

    specs = [
        MemberSpec(
            model_id=m.model_id,
            horizon_days=m.horizon_days,
            market=m.market,
            model_dir=m.model_dir or None,
        )
        for m in members
    ]
    try:
        evidence = await _gather_evidence(specs, tenant_id, user_id)
    except Exception as exc:  # noqa: BLE001 —— 宁可跳过（下 tick 重试），绝不降级写盘
        logger.warning(
            "[FusionRefresh] %s 证据组装失败（跳过，不降级）: %s", model_id, exc
        )
        return _skip(model_id, "skipped_evidence", f"证据不可得: {exc}")

    fw = compute_fusion_weights(
        evidence.member_ic_series(),
        evidence.corr,
        FusionWeightConfig(strategy=strategy),
    )
    if fw.warning:
        logger.info("[FusionRefresh] %s 引擎提示: %s", model_id, fw.warning)

    new_weights = {str(k): round(float(v), 6) for k, v in fw.weights.items()}
    old_weights = _read_snapshot_weights(model_dir / "weight_snapshot.json")
    delta = _max_abs_delta(old_weights, new_weights)
    delta_out = None if math.isinf(delta) else round(delta, 6)

    if delta < min_delta:
        return {
            "model_id": model_id,
            "status": "debounced",
            "max_delta": delta_out,
            "weights": new_weights,
        }
    if dry_run:
        return {
            "model_id": model_id,
            "status": "would_update",
            "max_delta": delta_out,
            "weights": new_weights,
        }

    _write_snapshot(
        model_dir,
        strategy=fw.strategy,
        weights=new_weights,
        diagnostics=[asdict(d) for d in fw.diagnostics],
        max_delta=delta,
    )
    logger.info(
        "[FusionRefresh] %s 权重已刷新 max_delta=%s weights=%s",
        model_id,
        delta_out,
        new_weights,
    )
    return {
        "model_id": model_id,
        "status": "updated",
        "max_delta": delta_out,
        "weights": new_weights,
    }


async def refresh_fusion_weights(
    *, min_delta: float | None = None, dry_run: bool = False
) -> dict[str, Any]:
    """扫描并刷新全部融合模型权重；返回分桶摘要。

    桶：``updated``（含 dry_run 的 would_update）、``debounced``、
    ``skipped``（带 status/reason）、``errors``（单模型异常，隔离不拖垮其余）。
    """
    threshold = MIN_WEIGHT_DELTA if min_delta is None else float(min_delta)
    rows = await _scan_ensembles()
    summary: dict[str, Any] = {
        "scanned": len(rows),
        "min_delta": threshold,
        "dry_run": bool(dry_run),
        "updated": [],
        "debounced": [],
        "skipped": [],
        "errors": [],
    }
    for row in rows:
        model_id = str(row.get("model_id") or "")
        try:
            entry = await _refresh_one(row, min_delta=threshold, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001 —— 单模型失败隔离，逐模型上报
            logger.warning(
                "[FusionRefresh] %s 刷新失败: %s", model_id, exc, exc_info=True
            )
            summary["errors"].append({"model_id": model_id, "error": str(exc)})
            continue
        if entry["status"] in ("updated", "would_update"):
            summary["updated"].append(entry)
        elif entry["status"] == "debounced":
            summary["debounced"].append(entry)
        else:
            summary["skipped"].append(entry)
    return summary
