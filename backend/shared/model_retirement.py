"""§7 退役保留策略（P4）：archive 保留 N=3 版本 + 30 天冷却 + 全部审计。

**口径（设计 §7「退役」行的工程化）**：

- **保留 N=3 版本**：同 ``(tenant, user, market)`` 组内按归档时间保留**最新 3 个归档版本**，
  更老的进入清退候选（分组按市场——§5.5 纪律：CN 的清退不许动 HK/US 的存档）；
- **30 天冷却**：归档满 ``cooldown_days`` 天（含边界）才可清退；不足 = 跳过并给出可清退日；
- **清退动作 = 只删产物目录**（``storage_path`` 指向的模型目录），**DB 行保留**并写元数据
  墓碑（``artifacts_purged``）——审计/回退链/台账不受影响，撤销 = 重训或从备份恢复；
- **诚实拒绝**：缺归档时间戳不清退（不猜）；产物路径越出用户模型根目录 / 是符号链接 /
  被活跃 rollout（replay_eval/observing/gate_passed）引用 → 一律跳过并记原因；
- **全部审计**：清退写 ``user_audit_logs``（action=model.retire_purge）；归档动作的审计在
  ``model_registry.archive_model``（action=model.archive，与归档同事务）。

**归档时间戳**：``qm_user_models`` 无 archived_at 列，采用 ``updated_at`` 作代理——
``archive_model`` 归档即刷新 updated_at，归档行此后无其它写路径；清退时刻意不碰
updated_at（保住该代理语义）。

**默认关/预演**：清退是破坏性动作，唯一入口 CLI ``backend/scripts/model_retirement_sweep.py``
默认 dry-run，``--apply`` 才执行；不内置调度（与市场同步同一纪律：何时跑由用户定）。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: 保留版本数（§7：archive 保留 N=3 版本）
KEEP_N = 3
#: 清退冷却（§7：30 天）
COOLDOWN_DAYS = 30

#: 审计动作名（user_audit_logs.action）——归档在 model_registry 侧，清退在本模块
AUDIT_ACTION_ARCHIVE = "model.archive"
AUDIT_ACTION_PURGE = "model.retire_purge"


def default_user_models_root() -> Path:
    """用户模型根目录（与 ``model_registry`` 同口径：USER_MODELS_ROOT，缺省 /app/models/users）。"""
    raw = Path(os.getenv("USER_MODELS_ROOT", "models/users"))
    return raw if raw.is_absolute() else Path("/app") / raw


def _as_utc(value: Any) -> datetime | None:
    """宽松解析归档时间（datetime / ISO 字符串）→ aware UTC；不可解析 → None。

    库列是 TIMESTAMPTZ（asyncpg 回 aware）；naive 只可能来自夹具，**按 UTC 假设**并留注释，
    不按上海墙钟解释（同「快照表 naive UTC」教训）。
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def plan_retirement(
    rows: Sequence[Mapping[str, Any]],
    *,
    keep_n: int = KEEP_N,
    cooldown_days: int = COOLDOWN_DAYS,
    now: datetime | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """纯函数：归档行 → 清退计划 ``{purge, kept, skipped}``。

    行契约：``{model_id, tenant_id, user_id, market, archived_at, blocked?, blocked_reason?}``
    （``blocked`` 由 IO 层填充：already_purged / active_rollout_reference）。

    规则（按 ``(tenant,user,market)`` 分组，组内 archive 时间倒序——平手按 model_id 倒序，确定性）：

    1. 组内最新 ``keep_n`` 个 → ``kept``（不因冷却/阻塞改变）；
    2. 其余：缺 ``model_id`` → skip(missing_model_id，库里有历史哨兵空行，见下)；
       缺时间戳 → skip(missing_archived_at)；``blocked`` → skip(原因)；归档未满
       ``cooldown_days`` → skip(cooldown, 给 eligible_at)；否则 → ``purge``（retention_excess）；
    3. ``purge`` 输出按归档时间升序（最老先清，便于分批）。

    空 ``model_id`` 行是真实现象（生产库存在 ``{readonly, system_default}`` 的归档哨兵
    空行）——不清退也不崩：清退目标按 id 推导，空 id 无从下手，如实跳过。
    """
    if keep_n < 0 or cooldown_days < 0:
        raise ValueError("keep_n / cooldown_days 不得为负")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)

    kept: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    purge: list[dict[str, Any]] = []

    groups: dict[tuple[str, str, str], list[tuple[datetime, Mapping[str, Any]]]] = {}
    for row in rows or []:
        mid = str(row.get("model_id") or "").strip()
        if not mid:
            skipped.append(
                {
                    "model_id": mid,
                    "tenant_id": str(row.get("tenant_id") or ""),
                    "user_id": str(row.get("user_id") or ""),
                    "market": str(row.get("market") or ""),
                    "reason": "missing_model_id",
                }
            )
            continue
        archived = _as_utc(row.get("archived_at"))
        if archived is None:
            skipped.append(
                {
                    "model_id": mid,
                    "tenant_id": str(row.get("tenant_id") or ""),
                    "user_id": str(row.get("user_id") or ""),
                    "market": str(row.get("market") or ""),
                    "reason": "missing_archived_at",
                }
            )
            continue
        key = (
            str(row.get("tenant_id") or ""),
            str(row.get("user_id") or ""),
            str(row.get("market") or ""),
        )
        groups.setdefault(key, []).append((archived, row))

    for key in sorted(groups):
        entries = sorted(
            groups[key],
            key=lambda t: (t[0], str(t[1].get("model_id") or "")),
            reverse=True,
        )  # 最新在前；平手 model_id 倒序（确定性）
        for rank, (archived, row) in enumerate(entries):
            entry: dict[str, Any] = {
                "model_id": str(row.get("model_id")),
                "tenant_id": key[0],
                "user_id": key[1],
                "market": key[2],
                "archived_at": archived.isoformat(),
                "age_days": round((current - archived).total_seconds() / 86400, 1),
                "storage_path": row.get("storage_path"),
            }
            if rank < keep_n:
                entry["reason"] = "within_keep_n"
                kept.append(entry)
                continue
            if row.get("blocked"):
                entry["reason"] = str(row.get("blocked_reason") or "blocked")
                skipped.append(entry)
                continue
            eligible_at = archived + timedelta(days=cooldown_days)
            if current < eligible_at:
                entry["reason"] = "cooldown"
                entry["eligible_at"] = eligible_at.isoformat()
                skipped.append(entry)
                continue
            entry["reason"] = "retention_excess"
            purge.append(entry)

    purge.sort(key=lambda e: (e["archived_at"], e["model_id"]))
    return {"purge": purge, "kept": kept, "skipped": skipped}


def purge_model_artifacts(
    models_root: Path | str,
    model_id: str,
    storage_path: str | None = None,
) -> dict[str, Any]:
    """删除单个模型的产物目录（§7 清退动作；不动 DB 行）。返回结果字典，不抛业务异常。

    安全约束（任一不满足 → ``removed=False`` + ``reason``）：

    - 目标必须是**符号链接之外**的普通目录（链接一律拒绝——指向别处时 rmtree 语义是灾难）；
    - 目标 resolve 后必须**深在用户模型根目录之内**（越界=配置错误/注入，拒绝）；
    - 缺 ``storage_path`` 时按 ``root/<model_id>`` 推；``model_id`` 必须是单一目录名。
    """
    root = Path(models_root)
    mid = str(model_id or "").strip()
    if not mid:
        raise ValueError("model_id is required")
    out: dict[str, Any] = {
        "model_id": mid,
        "path": None,
        "existed": False,
        "removed": False,
        "files_removed": 0,
        "bytes_freed": 0,
        "reason": None,
    }

    if storage_path:
        raw = Path(str(storage_path))
        if not raw.is_absolute():
            raw = root / raw
    else:
        if Path(mid).name != mid or mid in (".", ".."):
            raise ValueError(f"model_id 必须是单一目录名: {mid!r}")
        raw = root / mid

    out["path"] = str(raw)
    if raw.is_symlink():
        out["reason"] = "symlink_refused"
        return out
    resolved = raw.resolve()
    root_resolved = root.resolve()
    out["path"] = str(resolved)
    if resolved == root_resolved or not resolved.is_relative_to(root_resolved):
        out["reason"] = "outside_models_root"
        return out
    if not resolved.exists():
        out["existed"] = False
        out["reason"] = "not_found"
        return out
    out["existed"] = True

    files = 0
    size = 0
    try:
        for dirpath, _dirnames, filenames in os.walk(resolved):
            for name in filenames:
                p = Path(dirpath) / name
                try:
                    if not p.is_symlink():
                        size += p.stat().st_size
                        files += 1
                except OSError:
                    continue
        shutil.rmtree(resolved)
    except OSError as exc:
        out["reason"] = f"rmtree_failed: {exc}"
        return out
    out["removed"] = True
    out["files_removed"] = files
    out["bytes_freed"] = size
    return out


async def sweep_retirement(
    *,
    dry_run: bool = True,
    keep_n: int = KEEP_N,
    cooldown_days: int = COOLDOWN_DAYS,
    tenant_id: str | None = None,
    user_id: str | None = None,
    models_root: Path | str | None = None,
) -> dict[str, Any]:
    """退役清退巡检（唯一 IO 处）：读归档行 + 活跃 rollout 引用 → 计划 →（可选）执行。

    ``dry_run=True``（默认）只出计划；``apply`` 时逐模型：清产物 → 写墓碑 + 审计
    （每模型独立事务——单模型失败不回滚已清退者）。
    """
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session
    from backend.shared.model_rollout_store import ACTIVE_STAGES

    root = Path(models_root) if models_root else default_user_models_root()

    where = ["status = 'archived'"]
    params: dict[str, Any] = {}
    if tenant_id:
        where.append("tenant_id = :tenant_id")
        params["tenant_id"] = tenant_id
    if user_id:
        where.append("user_id = :user_id")
        params["user_id"] = user_id

    stages = sorted(ACTIVE_STAGES)
    placeholders = ", ".join(f":s{i}" for i in range(len(stages)))
    stage_params = {f"s{i}": s for i, s in enumerate(stages)}

    async with get_session(read_only=True) as session:
        db_rows = (
            (
                await session.execute(
                    text(
                        "SELECT model_id, tenant_id, user_id, storage_path, updated_at, "
                        "qm_market_of(metadata_json) AS market, "
                        "(metadata_json->>'artifacts_purged' = 'true') AS purged "
                        f"FROM qm_user_models WHERE {' AND '.join(where)} "
                        "ORDER BY updated_at DESC"
                    ),
                    params,
                )
            )
            .mappings()
            .all()
        )
        refs = (
            await session.execute(
                text(
                    "SELECT challenger_model_id AS mid FROM qm_model_rollouts "
                    f"WHERE stage IN ({placeholders}) "
                    "UNION SELECT champion_model_id FROM qm_model_rollouts "
                    f"WHERE stage IN ({placeholders})"
                ),
                stage_params,
            )
        ).all()
    blocked_ids = {str(r[0]) for r in refs}

    plan_rows: list[dict[str, Any]] = []
    for r in db_rows:
        mid = str(r["model_id"])
        blocked_reason = None
        if bool(r["purged"]):
            blocked_reason = "already_purged"
        elif mid in blocked_ids:
            blocked_reason = "active_rollout_reference"
        plan_rows.append(
            {
                "model_id": mid,
                "tenant_id": r["tenant_id"],
                "user_id": r["user_id"],
                "market": r["market"],
                "archived_at": r["updated_at"],
                "storage_path": r["storage_path"],
                "blocked": blocked_reason is not None,
                "blocked_reason": blocked_reason,
            }
        )

    plan = plan_retirement(plan_rows, keep_n=keep_n, cooldown_days=cooldown_days)
    summary: dict[str, Any] = {
        "dry_run": dry_run,
        "keep_n": keep_n,
        "cooldown_days": cooldown_days,
        "models_root": str(root),
        "purge": plan["purge"],
        "kept": plan["kept"],
        "skipped": plan["skipped"],
        "errors": [],
    }
    if dry_run:
        return summary

    for item in plan["purge"]:
        result = purge_model_artifacts(root, item["model_id"], item.get("storage_path"))
        if result["removed"] or result["reason"] == "not_found":
            await _mark_purged(item, result, keep_n=keep_n, cooldown_days=cooldown_days)
            item["purged"] = True
            item["files_removed"] = result["files_removed"]
            item["bytes_freed"] = result["bytes_freed"]
        else:
            summary["errors"].append({**result, "tenant_id": item["tenant_id"],
                                      "user_id": item["user_id"]})
    return summary


async def _mark_purged(
    item: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    keep_n: int,
    cooldown_days: int,
) -> None:
    """墓碑 + 审计（同事务）。**不动 updated_at**——它是归档时间戳代理（见模块 docstring）。"""
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session
    from backend.shared.utc_datetime import utc_now

    now = utc_now()
    patch = {
        "artifacts_purged": True,
        "artifacts_purged_at": now.isoformat(),
        "artifacts_purged_bytes": int(result.get("bytes_freed") or 0),
        "artifacts_purge_path": result.get("path"),
        "artifacts_purge_policy": f"keep_n={keep_n},cooldown_days={cooldown_days}",
    }
    description = (
        f"退役清退（§7）：模型 {item['model_id']}（market={item['market']}，"
        f"归档 {item['age_days']} 天 ≥ 冷却 {cooldown_days} 天，超出保留 {keep_n} 版）；"
        f"产物已清 {'not_found（已不在盘）' if result.get('reason') == 'not_found' else str(result.get('files_removed')) + ' 文件'}，"
        f"释放 {int(result.get('bytes_freed') or 0)} 字节；DB 行保留（墓碑 artifacts_purged）"
    )
    async with get_session() as session:
        await session.execute(
            text(
                "UPDATE qm_user_models "
                "SET metadata_json = COALESCE(metadata_json, '{}'::jsonb) || CAST(:patch AS JSONB) "
                "WHERE tenant_id = :tenant_id AND user_id = :user_id AND model_id = :model_id"
            ),
            {
                "patch": json.dumps(patch, ensure_ascii=False),
                "tenant_id": item["tenant_id"],
                "user_id": item["user_id"],
                "model_id": item["model_id"],
            },
        )
        await session.execute(
            text(
                "INSERT INTO user_audit_logs "
                "(user_id, tenant_id, action, resource, resource_id, description, success, created_at) "
                "VALUES (:user_id, :tenant_id, :action, :resource, :resource_id, :description, TRUE, :created_at)"
            ),
            {
                "user_id": item["user_id"],
                "tenant_id": item["tenant_id"],
                "action": AUDIT_ACTION_PURGE,
                "resource": "qm_user_models",
                "resource_id": item["model_id"],
                "description": description[:2000],
                "created_at": now,
            },
        )
