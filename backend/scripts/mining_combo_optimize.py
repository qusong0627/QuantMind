#!/usr/bin/env python3
"""组合权重优化作业（P2 组合实验室）——单组合一进程，锁串行。

架构（与 ``mining_pool_rebuild`` 同款——pandas/scipy 重算绝不能跑在引擎
事件循环里）：

- 端点 ``POST /alpha-agent/combos/optimize`` 经 ``create_combo_row`` 先落一行
  ``rd_agent_factor_combos``（status=pending，``train_metrics.config`` 存请求
  参数回执），再 ``spawn_combo`` 起本脚本子进程；
- 本脚本 ``main`` 取全局 flock（同一时刻只允许一个组合作业：DE 是 CPU/内存
  大户）→ ``run_job``：行置 running → 读面板 → ``combo_optimizer.optimize_combo``
  （train/valid 时序拆分 + 差分进化 + 扣成本净值曲线）→ 结果回写 done；
  任何异常置 failed + error 并**上抛**（绝不静默留 pending/running）；
- 拿不到锁的子进程把自己的行标 failed 后退出 3（端点侧应已 409——
  子进程取锁是竞态的最终兜底）；
- ``run_job`` 对已 done 的行幂等跳过（不重算不覆盖）。

``combo_id`` 允许 ``[A-Za-z0-9_.:-]``（首字符非 ``-``）；argv 全走
``build_run_command`` 校验后固定拼接，无 shell。

典型用法::

    python backend/scripts/mining_combo_optimize.py --combo-id <hex>
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any
from uuid import uuid4

_project_root = Path(__file__).resolve().parents[2]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from backend.services.engine.mining_plugins import pool_panels  # noqa: E402
from backend.services.engine.mining_plugins.combo_optimizer import (  # noqa: E402
    DEFAULT_CONFIG,
    MAX_FACTORS,
    MIN_FACTORS,
    ComboConfig,
    optimize_combo,
)

logger = logging.getLogger("mining_combo_optimize")

DEFAULT_MARKET = "a_share"
_MARKETS = ("a_share", "hong_kong", "us_stock", "crypto", "futures")

#: 单段 token 白名单（与 mining_pool_rebuild 同款）：首字符不许是 ``-``，
#: 否则 ``--force`` 这类值会被 argparse 解析成 flag。
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:][A-Za-z0-9_.:-]{0,127}$")

#: 配置项类型（parse_config 的合法键；未知键忽略——向前兼容旧行）
_CONFIG_CASTERS: dict[str, Any] = {
    "max_days": int,
    "train_ratio": float,
    "popsize": int,
    "maxiter": int,
    "tol": float,
    "seed": int,
    "time_budget_s": float,
    "cost_rate": float,
}

_START_CONFIRM_TIMEOUT_S = 8.0
_START_CONFIRM_POLL_S = 0.2
_START_GUARD = threading.Lock()

#: scipy differential_evolution 的 seed 上限；越界必须建行前 400，
#: 不许拖到 DE 里才炸（那会白占一轮全局单飞槽）。
_MAX_SEED = 2**32 - 1


class ComboBusyError(RuntimeError):
    """已有一个组合优化进程持锁（端点转 409）。"""


class ComboStartError(RuntimeError):
    """子进程起不来/秒退（端点转 500，附日志路径）。"""


def _json_value(value: Any) -> Any:
    """asyncpg 直读 JSONB 可能给 str（text() 查询无类型信息），统一解析。"""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


# ── 运维面路径（env 可覆盖；测试指 tmp 隔离）───────────────────────────


def _lock_path() -> Path:
    override = os.getenv("QM_COMBO_LOCK")
    if override:
        return Path(override)
    return Path(tempfile.gettempdir()) / "qm-combo-optimize.lock"


def _log_dir() -> Path:
    override = os.getenv("QM_COMBO_LOG_DIR")
    if override:
        return Path(override)
    return Path("/data/combo_optimize_logs")


#: 锁等待窗口（秒）：父进程探活每 200ms 会短暂持 LOCK_EX，子进程一次性
#: LOCK_NB 会偶发撞进探针窗口被误判「忙」——带重试消除这个自伤竞态。
_LOCK_WAIT_S = 2.0
_LOCK_POLL_S = 0.05


def _open_lock_fd() -> int:
    """O_NOFOLLOW 打开锁文件：世界可写的 /tmp 里绝不跟随符号链接截断目标。

    打开失败（符号链接 ELOOP / 权限）由调用方按「拿不到锁」处理。
    """
    return os.open(_lock_path(), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644)


def _acquire_combo_lock(timeout_s: float = 0.0) -> Any | None:
    """独占锁（非阻塞取，可带重试窗）；拿不到返回 None（调用侧退出 3）。

    打开失败按「拿不到」处理并留警示——绝不 ``open("w")`` 跟随符号链接
    （安全 L-3）。``timeout_s`` 给子进程用：把父进程的 µs 级探针窗口
    熬过去再取，免费机器上不再偶发「起不来」。
    """
    import fcntl  # POSIX-only：容器/Ubuntu 运行

    deadline = time.monotonic() + max(0.0, timeout_s)
    while True:
        try:
            fd = _open_lock_fd()
        except OSError as exc:
            logger.warning("组合锁文件不可用（按拿不到锁处理）：%s", exc)
            return None
        try:
            handle = os.fdopen(fd, "r+", encoding="utf-8")
        except OSError:
            os.close(fd)
            return None
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except OSError:
            handle.close()
            if time.monotonic() >= deadline:
                return None
            time.sleep(_LOCK_POLL_S)


def probe_combo_lock() -> bool:
    """是否有组合作业在跑（flock 试探；锁文件不可用按未运行处理）。

    与真实运行共用同一把锁；探测与启动之间的竞态由子进程取锁（带重试窗）
    兜底。同样 O_NOFOLLOW，绝不因探测跟随符号链接。
    """
    import fcntl

    try:
        fd = _open_lock_fd()
    except OSError as exc:
        logger.warning("组合锁探测失败（按未运行处理）：%s", exc)
        return False
    handle = os.fdopen(fd, "r+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False
    finally:
        handle.close()


# ── 纯函数：配置 / 因子集 / argv ───────────────────────────────────────


def parse_config(payload: Mapping[str, Any] | None) -> ComboConfig:
    """请求配置（部分 dict）→ ComboConfig；未知键忽略、非法类型 ValueError。

    ``None`` 值按「未提供」处理（端点 seed 可选）。类型错误的信息必须
    带上键名——端点的 400 文案直接用它，排查时不必猜哪项错了。
    """
    data = dict(payload) if isinstance(payload, Mapping) else {}
    kwargs: dict[str, Any] = {}
    for key, caster in _CONFIG_CASTERS.items():
        if key not in data or data[key] is None:
            continue
        raw = data[key]
        try:
            kwargs[key] = caster(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"配置项 {key} 类型非法：{raw!r}") from exc
    if "seed" in kwargs and not 0 <= kwargs["seed"] <= _MAX_SEED:
        raise ValueError(f"配置项 seed 超出范围 0..{_MAX_SEED}：{kwargs['seed']!r}")
    return ComboConfig(**kwargs)


def validate_factor_ids(factor_ids: Sequence[str] | None) -> list[str]:
    """去重（保序）+ token 白名单 + 数量闸门（MIN_FACTORS..MAX_FACTORS）。

    数量闸门必须**先于**逐元素扫描：原始列表长度即计闸（重复也计入），
    否则 N 个元素的去重是 O(N²)，一个请求就能把引擎事件循环钉死
    （安全评审 H-1）。超限一次即拒，计数是去重前的原始数。
    """
    raw = list(factor_ids or [])
    if len(raw) > MAX_FACTORS:
        raise ValueError(f"因子数超上限：{len(raw)} > {MAX_FACTORS}（去重前计数）")
    order: list[str] = []
    seen: set[str] = set()
    for fid in raw:
        if not isinstance(fid, str) or not _SAFE_TOKEN_RE.fullmatch(fid):
            raise ValueError(f"因子 ID 含非法字符：{fid!r}")
        if fid not in seen:
            seen.add(fid)
            order.append(fid)
    if len(order) < MIN_FACTORS:
        raise ValueError(f"组合至少需要 {MIN_FACTORS} 个因子（去重后 {len(order)} 个）")
    return order


def build_run_command(combo_id: str) -> list[str]:
    """后台触发的作业命令（argv 白名单校验后固定拼接，无 shell）。"""
    if not combo_id or not _SAFE_TOKEN_RE.fullmatch(combo_id):
        raise ValueError("combo_id 含非法字符")
    return [
        sys.executable,
        "-m",
        "backend.scripts.mining_combo_optimize",
        "--combo-id",
        combo_id,
    ]


# ── DB 面：建行 / 作业执行 ─────────────────────────────────────────────


async def create_combo_row(
    *,
    user_id: str,
    market: str,
    universe: str = "",
    factor_ids: Sequence[str],
    name: str = "",
    seed: int | None = None,
) -> str:
    """校验因子集 → 落 pending 行 → 返回 combo_id（不启动进程）。

    校验两条（按序，错误信息喂端点 400）：
    1. 因子必须**存在且属于该用户**（跨用户引用 = 拿别人的挖掘成果，
       rd_agent_factors.user_id 收口）；
    2. 因子在**当前市场**必须有面板（判定读**池表** ``rd_agent_factor_pool``
       的 ``panel_ref`` + ``market``——面板按市场分目录，只查 panel_ref 会把
       跨市场提交放进来，到作业期才炸且报错指向错误的修复动作）；无面板的
       因子进组合会在作业期才炸，这里提前拦下并指路 ``mining_pool_rebuild
       --panels``。

    请求参数回执存 ``train_metrics.config``（combos 表没有 seed 列）；
    作业完成后该字段被优化器的完整回执覆盖（含 converged/n_evaluations）。
    """
    order = validate_factor_ids(factor_ids)
    if not user_id:
        raise ValueError("user_id 不能为空")
    if market not in _MARKETS:
        raise ValueError(f"不支持的 market：{market!r}")
    config = parse_config({"seed": seed})

    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=True) as session:
        rows = (
            (
                await session.execute(
                    text("""
                        SELECT f.factor_id AS factor_id,
                               (p.panel_ref IS NOT NULL
                                AND p.market = :market) AS has_panel
                        FROM rd_agent_factors f
                        LEFT JOIN rd_agent_factor_pool p
                               ON p.factor_id = f.factor_id
                        WHERE f.factor_id = ANY(:ids) AND f.user_id = :uid
                    """),
                    {"ids": order, "uid": user_id, "market": market},
                )
            )
            .mappings()
            .all()
        )
    owned = {str(r["factor_id"]) for r in rows}
    missing = [fid for fid in order if fid not in owned]
    if missing:
        raise ValueError(f"因子不存在或不属于当前用户：{', '.join(missing[:5])}")
    no_panel = [str(r["factor_id"]) for r in rows if not r["has_panel"]]
    if no_panel:
        raise ValueError(
            f"以下因子在当前市场（{market}）没有面板缓存，无法做组合优化"
            f"（先跑 mining_pool_rebuild --panels 回填）：{', '.join(no_panel[:5])}"
        )

    combo_id = uuid4().hex
    async with get_session() as session:
        await session.execute(
            text("""
                INSERT INTO rd_agent_factor_combos
                    (combo_id, user_id, market, universe, name, factor_ids,
                     weights, train_metrics, status)
                VALUES (:cid, :uid, :market, :universe, :name,
                        CAST(:fids AS JSONB), '{}'::jsonb, CAST(:tm AS JSONB),
                        'pending')
            """),
            {
                "cid": combo_id,
                "uid": user_id,
                "market": market,
                "universe": universe or "",
                "name": (name or "")[:200],
                "fids": json.dumps(order, ensure_ascii=False),
                "tm": json.dumps({"config": asdict(config)}, ensure_ascii=False),
            },
        )
    logger.info("[combo] 已建组合行 %s user=%s factors=%s", combo_id, user_id, order)
    return combo_id


async def _load_combo(combo_id: str) -> dict[str, Any]:
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=True) as session:
        row = (
            (
                await session.execute(
                    text("""
                        SELECT combo_id, user_id, market, universe, name,
                               factor_ids, weights, train_window, train_metrics,
                               valid_metrics, status, error
                        FROM rd_agent_factor_combos
                        WHERE combo_id = :cid
                    """),
                    {"cid": combo_id},
                )
            )
            .mappings()
            .first()
        )
    if row is None:
        raise ValueError(f"组合不存在：{combo_id}")
    return dict(row)


def _to_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


async def mark_failed_row(combo_id: str, error: str) -> None:
    """把组合行标 failed 并留因（公开：端点启动失败路径也用它收尾）。

    只翻**非终态**（pending/running）的行：done 的结果绝不许被「重放时锁被
    占」这类迟到失败覆盖（评审发现 6——终态被翻后前端连权重都看不回来）。
    """
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    async with get_session() as session:
        await session.execute(
            text("""
                UPDATE rd_agent_factor_combos
                SET status = 'failed', error = :err, updated_at = NOW()
                WHERE combo_id = :cid AND status IN ('pending', 'running')
            """),
            {"cid": combo_id, "err": str(error)[:2000]},
        )


#: 惰性收敛宽限（秒）：建行→子进程取锁之间有秒级窗口，刚建的行不许被秒判死。
_RECONCILE_GRACE_S = 90.0


async def reconcile_stale_row(
    combo_id: str, *, user_id: str, grace_s: float | None = None
) -> bool:
    """死亡作业的惰性收敛：pending/running + 锁空闲 + 超宽限 → failed。

    子进程被 OOM/SIGKILL 或容器重启卷走时无人收尸，行会永远停在 running，
    前端就会永久轮询一个不可能推进的状态（评审发现 2）。收敛挂在详情读取
    路径上（无独立清扫线程）：读时顺手体检，命中才写。返回是否发生了收敛。
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    grace = _RECONCILE_GRACE_S if grace_s is None else float(grace_s)
    async with get_session(read_only=True) as session:
        row = (
            (
                await session.execute(
                    text("""
                        SELECT status, updated_at
                        FROM rd_agent_factor_combos
                        WHERE combo_id = :cid AND user_id = :uid
                    """),
                    {"cid": combo_id, "uid": user_id},
                )
            )
            .mappings()
            .first()
        )
    if row is None:
        return False
    if str(row["status"] or "") not in ("pending", "running"):
        return False
    updated_at = row["updated_at"]
    if updated_at is not None:
        if updated_at.tzinfo is None:  # 防御：naive 时间一律按 UTC 读
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - updated_at < timedelta(seconds=grace):
            return False
    if probe_combo_lock():
        return False
    await mark_failed_row(
        combo_id, "作业进程已消失（容器重启或进程被杀），结果未产出；可重跑"
    )
    logger.warning("[combo] 收敛死亡作业 combo_id=%s（锁空闲且状态陈旧）", combo_id)
    return True


async def list_combos(
    *,
    user_id: str,
    market: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    """组合分页列表（user-scoped，新→旧）；market=None 不过滤市场。"""
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    clauses = ["user_id = :uid"]
    params: dict[str, Any] = {
        "uid": user_id,
        "limit": int(limit),
        "offset": int(offset),
    }
    if market:
        clauses.append("market = :market")
        params["market"] = market
    where = " AND ".join(clauses)
    async with get_session(read_only=True) as session:
        total = (
            await session.execute(
                text(f"SELECT COUNT(*) FROM rd_agent_factor_combos WHERE {where}"),
                params,
            )
        ).scalar() or 0
        rows = (
            (
                await session.execute(
                    text(f"""
                        SELECT combo_id, market, universe, name, factor_ids, status,
                               error, train_window,
                               train_metrics->>'mean_rank_ic' AS train_mean_rank_ic,
                               valid_metrics->>'mean_rank_ic' AS valid_mean_rank_ic,
                               created_at, updated_at
                        FROM rd_agent_factor_combos
                        WHERE {where}
                        ORDER BY created_at DESC, combo_id DESC
                        LIMIT :limit OFFSET :offset
                    """),
                    params,
                )
            )
            .mappings()
            .all()
        )
    items: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        fids = _json_value(item.get("factor_ids")) or []
        item["factor_ids"] = fids if isinstance(fids, list) else []
        item["n_factors"] = len(item["factor_ids"])
        item["train_mean_rank_ic"] = _to_float(item.get("train_mean_rank_ic"))
        item["valid_mean_rank_ic"] = _to_float(item.get("valid_mean_rank_ic"))
        for key in ("created_at", "updated_at"):
            value = item.get(key)
            item[key] = value.isoformat() if hasattr(value, "isoformat") else value
        items.append(item)
    return {"total": int(total), "items": items}


async def get_combo(combo_id: str, *, user_id: str) -> dict[str, Any] | None:
    """组合详情（含权重 / 两窗指标 / 曲线）；非属主返回 None（端点 404）。"""
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=True) as session:
        row = (
            (
                await session.execute(
                    text("""
                        SELECT combo_id, market, universe, name, factor_ids,
                               weights, train_window, train_metrics, valid_metrics,
                               status, error, created_at, updated_at
                        FROM rd_agent_factor_combos
                        WHERE combo_id = :cid AND user_id = :uid
                    """),
                    {"cid": combo_id, "uid": user_id},
                )
            )
            .mappings()
            .first()
        )
    if row is None:
        return None
    item = dict(row)
    for key in ("factor_ids", "weights", "train_metrics", "valid_metrics"):
        item[key] = _json_value(item.get(key))
    for key in ("created_at", "updated_at"):
        value = item.get(key)
        item[key] = value.isoformat() if hasattr(value, "isoformat") else value
    return item


async def _mark_failed_safe(combo_id: str, error: str) -> None:
    """尽力标失败 + 收尾数据库连接（main 的独立事件循环用）。"""
    try:
        await mark_failed_row(combo_id, error)
    except Exception as exc:  # noqa: BLE001 — 标记失败本身不许拖垮退出码
        logger.warning("[combo] 标记失败状态失败 combo_id=%s：%s", combo_id, exc)
    finally:
        await _close_db_safe()


async def _close_db_safe() -> None:
    try:
        from backend.shared.database_manager_v2 import close_database

        await close_database()
    except Exception as exc:  # noqa: BLE001
        logger.debug("[combo] 关闭数据库连接池：%s", exc)


def _read_frames(market: str, factor_ids: list[str]) -> dict[str, Any]:
    """逐因子读面板（同步 parquet IO，run_job 经 to_thread 调用）。"""
    return {fid: pool_panels.read_panel(market, fid) for fid in factor_ids}


async def run_job(combo_id: str) -> dict[str, Any]:
    """执行一个组合作业：pending/failed → running → done；异常置 failed 后上抛。

    已 ``done`` 的行幂等跳过（返回 ``{"skipped": "done"}``）——重放/重试
    不覆盖既有结果。
    """
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    row = await _load_combo(combo_id)
    if str(row.get("status") or "") == "done":
        logger.info("[combo] 已完成，跳过 combo_id=%s", combo_id)
        return {"skipped": "done", "combo_id": combo_id}

    raw_ids = _json_value(row.get("factor_ids")) or []
    order = [str(fid) for fid in raw_ids] if isinstance(raw_ids, list) else []
    if not order:
        raise ValueError(f"组合 {combo_id} 的 factor_ids 为空，无法执行")
    market = str(row.get("market") or DEFAULT_MARKET)
    config = parse_config(
        (_json_value(row.get("train_metrics")) or {}).get("config") or {}
    )

    try:
        async with get_session() as session:
            await session.execute(
                text("""
                    UPDATE rd_agent_factor_combos
                    SET status = 'running', error = NULL, updated_at = NOW()
                    WHERE combo_id = :cid
                """),
                {"cid": combo_id},
            )

        frames = await asyncio.to_thread(_read_frames, market, order)
        missing = [fid for fid, frame in frames.items() if frame is None]
        if missing:
            raise ValueError(
                f"以下因子面板缺失或损坏：{', '.join(missing[:5])}"
                "（先跑 mining_pool_rebuild --panels 回填）"
            )

        result = await asyncio.to_thread(optimize_combo, frames, order, config)

        async with get_session() as session:
            await session.execute(
                text("""
                    UPDATE rd_agent_factor_combos
                    SET status = 'done',
                        weights = CAST(:weights AS JSONB),
                        train_window = :tw,
                        train_metrics = CAST(:tm AS JSONB),
                        valid_metrics = CAST(:vm AS JSONB),
                        error = NULL,
                        updated_at = NOW()
                    WHERE combo_id = :cid
                """),
                {
                    "cid": combo_id,
                    "weights": json.dumps(result["weights"], ensure_ascii=False),
                    "tw": result["train_window"],
                    "tm": json.dumps(
                        result["train_metrics"], ensure_ascii=False, default=float
                    ),
                    "vm": json.dumps(
                        result["valid_metrics"], ensure_ascii=False, default=float
                    ),
                },
            )
    except Exception as exc:
        try:
            await mark_failed_row(combo_id, f"{type(exc).__name__}: {exc}")
        except Exception:  # noqa: BLE001 — 标失败不许掩盖原始异常
            logger.exception("[combo] 标记失败状态也失败了 combo_id=%s", combo_id)
        raise

    summary = {
        "status": "done",
        "combo_id": combo_id,
        "train_window": result["train_window"],
        "weights": result["weights"],
        "train_mean_rank_ic": result["train_metrics"].get("mean_rank_ic"),
        "valid_mean_rank_ic": result["valid_metrics"].get("mean_rank_ic"),
    }
    logger.info(
        "[combo] 完成 %s：valid mean rank-IC=%s",
        combo_id,
        summary["valid_mean_rank_ic"],
    )
    return summary


# ── 后台子进程启动（alpha-agent 路由用；与池刷新/物化同款硬化）───────────


async def spawn_combo(combo_id: str) -> dict[str, Any]:
    """起作业子进程 → 确认持锁 → 回收线程 → 回包（失败抛上面两个异常）。

    日志按组合一文件 ``<QM_COMBO_LOG_DIR>/<combo_id>.log``（先写 .tmp +
    ``O_NOFOLLOW``，启动成功才原子换名）。
    """
    if probe_combo_lock():
        raise ComboBusyError("已有组合优化作业在运行，请稍后重试")
    if not _START_GUARD.acquire(blocking=False):
        raise ComboBusyError("上一次启动确认尚未完成，请稍后重试")
    try:
        command = build_run_command(combo_id)
        log_path = _log_dir() / f"{combo_id}.log"
        if log_path.is_symlink():
            raise ComboStartError(f"作业日志路径 {log_path} 是符号链接，拒绝写入")
        tmp_path = log_path.with_name(log_path.name + ".tmp")
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(
                tmp_path,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                0o640,
            )
        except OSError as exc:
            raise ComboStartError(f"作业日志文件不可写：{tmp_path}（{exc}）") from exc

        try:
            log_handle = os.fdopen(fd, "w", encoding="utf-8")
        except OSError as exc:
            # fdopen 失败（EMFILE 等）时 fd 仍开着：手工收，别把行留成 pending
            with contextlib.suppress(OSError):
                os.close(fd)
            tmp_path.unlink(missing_ok=True)
            raise ComboStartError(f"作业日志文件打开失败：{tmp_path}（{exc}）") from exc
        try:
            process = subprocess.Popen(  # noqa: S603 - 固定 argv + 白名单校验
                command,
                cwd=str(_project_root),
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # 脱离引擎进程组：引擎重启不牵连作业
            )
        except OSError as exc:
            tmp_path.unlink(missing_ok=True)
            raise ComboStartError(
                f"作业子进程启动失败：{exc}（上一轮日志未动）"
            ) from exc
        finally:
            log_handle.close()
        try:
            os.replace(tmp_path, log_path)
        except OSError as exc:
            # 换名失败：刚起的子进程必须收回，否则它带着不可见日志跑到底、
            # 行却被标 failed——两端状态分叉（安全 L-2）。
            with contextlib.suppress(Exception):
                process.terminate()
            tmp_path.unlink(missing_ok=True)
            raise ComboStartError(f"作业日志文件换名失败：{log_path}（{exc}）") from exc

        confirmed = await _confirm_child_took_lock(process, log_path)
        if process.poll() is None:
            threading.Thread(
                target=_reap_combo_process,
                args=(process,),
                name="mining-combo-reaper",
                daemon=True,
            ).start()
        logger.info(
            "组合优化已启动：pid=%s combo=%s log=%s", process.pid, combo_id, log_path
        )
        return {
            "started": True,
            "pid": process.pid,
            "log_path": str(log_path),
            "confirmed": confirmed,
        }
    finally:
        _START_GUARD.release()


async def _confirm_child_took_lock(process: subprocess.Popen, log_path: Path) -> bool:
    """等子进程拿住独占锁（或退出）再回包；秒退按失败如实报。"""
    deadline = time.monotonic() + _START_CONFIRM_TIMEOUT_S
    while time.monotonic() < deadline:
        if probe_combo_lock():
            return True
        code = process.poll()
        if code is not None:
            process.wait()  # 回收已退出的子进程，避免僵尸
            if probe_combo_lock():
                raise ComboBusyError(
                    "已有组合优化作业在运行（本次子进程未取得独占锁，已退出，"
                    "其组合行已标失败）"
                )
            raise ComboStartError(
                f"作业启动失败（退出码 {code}），详见日志：{log_path}"
            )
        await asyncio.sleep(_START_CONFIRM_POLL_S)
    return False


def _reap_combo_process(process: subprocess.Popen) -> None:
    code = process.wait()
    if code == 0:
        logger.info("组合优化进程正常退出（pid=%s）", process.pid)
    else:
        logger.warning("组合优化进程退出码 %s（pid=%s），详见日志", code, process.pid)


# ── 入口 ───────────────────────────────────────────────────────────────


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="组合权重优化作业（单组合）")
    parser.add_argument("--combo-id", required=True, help="rd_agent_factor_combos 主键")
    return parser.parse_args(argv)


async def _run_and_close(combo_id: str) -> dict[str, Any]:
    try:
        return await run_job(combo_id)
    finally:
        await _close_db_safe()


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    args = _parse_args(argv)
    combo_id = str(args.combo_id or "")
    if not _SAFE_TOKEN_RE.fullmatch(combo_id):
        print("combo_id 含非法字符", file=sys.stderr)
        return 2

    lock = _acquire_combo_lock(timeout_s=_LOCK_WAIT_S)
    if lock is None:
        msg = "已有组合优化作业在运行，本次未执行"
        logger.warning("[combo] %s combo_id=%s", msg, combo_id)
        asyncio.run(_mark_failed_safe(combo_id, msg))
        return 3
    try:
        try:
            summary = asyncio.run(_run_and_close(combo_id))
        except Exception:
            logger.exception("[combo] 作业失败 combo_id=%s", combo_id)
            return 1
        logger.info(
            "[combo] 作业结束 %s", json.dumps(summary, ensure_ascii=False, default=str)
        )
        return 0
    finally:
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
