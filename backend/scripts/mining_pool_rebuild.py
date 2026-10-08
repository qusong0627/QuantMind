#!/usr/bin/env python3
"""因子池回填/刷新器：``rd_agent_factors`` → 池行 / 谱系边 / 面板缓存 / 缺失指标。

三个阶段（按序，各自可独立开关；单因子/单 scope 失败不中断其余）：

1. ``--panels``：对「已完成但没有面板缓存」的因子重跑因子代码（复用
   ``rd_mined_materialize`` 的子进程执行链：daily_pv.h5 + 隔离执行），产出
   逐日截面 rank_pct/zscore 面板 —— 值级相关性 / 新颖度 / 多样性 / 组合
   优化的价值级底座。仅 a_share（daily_pv.h5 为 CN 数据）。
2. ``--metrics``：面板值 × Qlib 次日收益 → 评估器链（RRE/换手/扣费），把
   **缺失**的指标键补进 metadata（不覆盖既有值；``--force-metrics`` 重算）。
   对齐/收益口径直接复用回测路由的 ``_align_factor_returns`` 等纯函数 ——
   同一套公式，修一处两边一起对。
3. 池刷新（默认执行，``--no-refresh`` 关闭）：逐 scope 调
   ``pool_service.refresh_pool`` —— 池行 / novelty / 公式边 / task 边 /
   值级相关边 / pool_score / 多样性。

**默认 dry-run**（只统计不落库），``--apply`` 才写。flock 独占：同一时刻
只允许一个刷新进程（与物化器同款，拿不到锁安静退 0）。

后台面板（alpha-agent「因子池」页）经 ``build_run_command`` 起子进程、
``probe_run_lock`` 探活、``read_status``/``tail_web_log`` 看进度——argv
全走 ``build_run_command`` 的校验，绝不拼接任何用户输入。

典型用法::

    python backend/scripts/mining_pool_rebuild.py --user <uid>            # 预演
    python backend/scripts/mining_pool_rebuild.py --user <uid> --apply    # 落库
    python backend/scripts/mining_pool_rebuild.py --apply --panels        # 连面板回填
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

_project_root = Path(__file__).resolve().parents[2]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

logger = logging.getLogger("mining_pool_rebuild")

DEFAULT_MARKET = "a_share"
_MARKETS = ("a_share", "hong_kong", "us_stock", "crypto", "futures")
_EXEC_TIMEOUT_S = 900
_METRICS_WINDOW_DAYS = 750
_MAX_SCOPE_REPORTS = 200

#: 单段 token（user_id / universe）白名单：仅作 argv 值进入子进程，
#: 拒绝空白/控制字符与「像 flag 的值」。**首字符不许是 `-`**：字符类里
#: 必须含 `-`（user-1 这类 ID），若只靠字符类，``--force`` 这类值能整段
#: 通过，argparse 侧就是 flag 注入面。
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:][A-Za-z0-9_.:-]{0,127}$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── 运维面路径（env 可覆盖；测试指 tmp 隔离）───────────────────────────


def _lock_path() -> Path:
    override = os.getenv("QM_POOL_REBUILD_LOCK")
    if override:
        return Path(override)
    return Path(tempfile.gettempdir()) / "_mining_pool_rebuild.lock"


def _status_path() -> Path:
    override = os.getenv("QM_POOL_REBUILD_STATUS")
    if override:
        return Path(override)
    return Path("/data/mining_pool_rebuild_status.json")


def _web_log_path() -> Path:
    override = os.getenv("QM_POOL_REBUILD_WEB_LOG")
    if override:
        return Path(override)
    return Path("/data/mining_pool_rebuild_ui.log")


def _write_status(payload: dict[str, Any]) -> None:
    """状态文件原子写（tmp + os.replace）；失败只告警——状态是展示面。"""
    path = _status_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("状态文件写入失败：%s", exc)


def read_status() -> dict[str, Any]:
    """最近一次刷新的落盘状态；文件缺失/损坏 → status=unknown（不抛）。"""
    path = _status_path()
    if not path.is_file():
        return {"status": "unknown", "path": str(path)}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "unknown", "path": str(path), "note": "状态文件损坏"}
    if not isinstance(data, dict):
        return {"status": "unknown", "path": str(path)}
    data["path"] = str(path)
    return data


def tail_web_log(max_lines: int = 120, max_bytes: int = 256 * 1024) -> dict[str, Any]:
    """后台刷新日志尾部（符号链接拒读等约束见 shared/log_tail）。"""
    from backend.shared.log_tail import tail_log_file

    return tail_log_file(_web_log_path(), max_lines=max_lines, max_bytes=max_bytes)


def _acquire_run_lock() -> Any | None:
    """非阻塞独占锁（与物化器同款）；拿不到返回 None，调用侧退出 0。"""
    import fcntl  # POSIX-only：容器/Ubuntu 运行

    handle = open(_lock_path(), "w", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def probe_run_lock() -> bool:
    """是否有刷新进程在跑（flock 试探；锁文件不可写按未运行处理）。

    与真实运行共用同一把锁；探测与启动之间的竞态由子进程取锁兜底。
    """
    import fcntl

    try:
        handle = open(_lock_path(), "a", encoding="utf-8")
    except OSError as exc:
        logger.warning("刷新锁探测失败（按未运行处理）：%s", exc)
        return False
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False
    finally:
        handle.close()


def build_run_command(
    *,
    user_id: str,
    market: str = DEFAULT_MARKET,
    universe: str | None = None,
    dry_run: bool = True,
) -> list[str]:
    """后台触发的刷新命令（argv 白名单校验后固定拼接，无 shell）。

    user_id/universe 允许 ``[A-Za-z0-9_.:-]``（拒绝空白与 ``--`` 前缀，
    防 argv 被解析成 flag）；market 必须在内置市场表内。校验失败抛
    ValueError，由端点转 400。
    """
    if market not in _MARKETS:
        raise ValueError(f"不支持的 market：{market!r}")
    if not user_id or not _SAFE_TOKEN_RE.fullmatch(user_id):
        raise ValueError("user_id 含非法字符")
    if (
        universe is not None
        and universe != ""
        and not _SAFE_TOKEN_RE.fullmatch(universe)
    ):
        raise ValueError("universe 含非法字符")
    cmd = [
        sys.executable,
        "-m",
        "backend.scripts.mining_pool_rebuild",
        "--user",
        user_id,
        "--market",
        market,
    ]
    if universe:
        cmd += ["--universe", universe]
    cmd.append("--dry-run" if dry_run else "--apply")
    return cmd


def refresh_status(max_lines: int = 80) -> dict[str, Any]:
    """端点用组合快照：锁探活 + 落盘状态 + 日志尾。"""
    status = read_status()
    status["running"] = probe_run_lock()
    status["log"] = tail_web_log(max_lines=max_lines)
    return status


# ── 后台子进程启动（alpha-agent 路由用；与管理员面物化启动同款硬化）─────


class RefreshBusyError(RuntimeError):
    """已有一个刷新进程持锁（端点转 409）。"""


class RefreshStartError(RuntimeError):
    """子进程起不来/秒退（端点转 500，附日志路径）。"""


_START_CONFIRM_TIMEOUT_S = 8.0
_START_CONFIRM_POLL_S = 0.2
_START_GUARD = threading.Lock()


async def spawn_refresh(
    *,
    user_id: str,
    market: str = DEFAULT_MARKET,
    universe: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """起刷新子进程 → 确认持锁 → 回收线程 → 回包（失败抛上面两个异常）。

    硬化点（与 ``admin.rd_mined_materialize._spawn_and_confirm`` 同款）：
    - argv 全走 ``build_run_command`` 白名单（无 shell、无拼接）；
    - 日志先写 ``.tmp`` + ``O_NOFOLLOW``，子进程起成功后才原子换名——
      启动失败不毁上一轮日志，符号链接摆不住；
    - 持锁确认后 ``started=true`` 才算数：flock 探测只是快速拒绝，
      子进程要几秒导入完才拿锁；期间进程内启动闩挡住第二个 POST。
    """
    if probe_run_lock():
        raise RefreshBusyError("已有因子池刷新进程在运行，本次未启动")
    if not _START_GUARD.acquire(blocking=False):
        raise RefreshBusyError("上一次启动确认尚未完成，请稍后重试")
    try:
        command = build_run_command(
            user_id=user_id, market=market, universe=universe, dry_run=dry_run
        )
        log_path = _web_log_path()
        if log_path.is_symlink():
            raise RefreshStartError(f"刷新日志路径 {log_path} 是符号链接，拒绝写入")
        tmp_path = log_path.with_name(log_path.name + ".tmp")
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(
                tmp_path,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                0o640,
            )
        except OSError as exc:
            raise RefreshStartError(f"刷新日志文件不可写：{tmp_path}（{exc}）") from exc

        log_handle = os.fdopen(fd, "w", encoding="utf-8")
        try:
            env = os.environ.copy()
            env["QM_POOL_REBUILD_TRIGGER"] = "api"
            process = subprocess.Popen(  # noqa: S603 - 固定 argv + 白名单校验
                command,
                cwd=str(_project_root),
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # 脱离引擎进程组：引擎重启不牵连刷新
                env=env,
            )
        except OSError as exc:
            tmp_path.unlink(missing_ok=True)
            raise RefreshStartError(
                f"刷新子进程启动失败：{exc}（上一轮日志未动）"
            ) from exc
        finally:
            log_handle.close()
        os.replace(tmp_path, log_path)

        confirmed = await _confirm_child_took_lock(process, log_path)
        if process.poll() is None:
            threading.Thread(
                target=_reap_refresh_process,
                args=(process,),
                name="mining-pool-refresh-reaper",
                daemon=True,
            ).start()
        logger.info(
            "因子池刷新已启动：pid=%s log=%s argv=%s", process.pid, log_path, command
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
        if probe_run_lock():
            return True
        code = process.poll()
        if code is not None:
            process.wait()  # 回收已退出的子进程，避免僵尸
            if probe_run_lock():
                raise RefreshBusyError(
                    "已有刷新进程在运行（本次子进程未取得独占锁，已退出，未写任何数据）"
                )
            raise RefreshStartError(
                f"刷新启动失败（退出码 {code}），详见日志：{log_path}"
            )
        await asyncio.sleep(_START_CONFIRM_POLL_S)
    return False


def _reap_refresh_process(process: subprocess.Popen) -> None:
    code = process.wait()
    if code == 0:
        logger.info("因子池刷新进程正常退出（pid=%s）", process.pid)
    else:
        logger.warning("因子池刷新进程退出码 %s（pid=%s），详见日志", code, process.pid)


# ── scope 发现 ─────────────────────────────────────────────────────────


async def _discover_scopes(args: argparse.Namespace) -> list[dict[str, str]]:
    """已完成因子的 (user, market, universe) 去重面；无 user 的因子跳过。"""
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    clauses = ["status = 'completed'", "user_id IS NOT NULL", "user_id <> ''"]
    params: dict[str, Any] = {"market": args.market}
    if args.user:
        clauses.append("user_id = :user_id")
        params["user_id"] = args.user
    if args.market:
        clauses.append("COALESCE(market, :market) = :market")
    if args.universe is not None:
        clauses.append("COALESCE(universe, '') = :universe")
        params["universe"] = args.universe
    where = " AND ".join(clauses)
    async with get_session(read_only=True) as session:
        rows = (
            (
                await session.execute(
                    text(f"""
                    SELECT DISTINCT user_id, COALESCE(market, :market) AS market,
                           COALESCE(universe, '') AS universe
                    FROM rd_agent_factors
                    WHERE {where}
                    ORDER BY user_id, market, universe
                """),
                    params,
                )
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


# ── 阶段 1/2：面板与缺失指标（仅 a_share，复用物化器执行链）────────────


async def _load_panel_metric_candidates(
    args: argparse.Namespace,
) -> list[dict[str, Any]]:
    """panels/metrics 阶段的候选：已完成、有代码、a_share 的因子。"""
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    clauses = [
        "status = 'completed'",
        "factor_code IS NOT NULL",
        "factor_code <> ''",
        "COALESCE(market, :market) = :market",
    ]
    params: dict[str, Any] = {"market": DEFAULT_MARKET}
    if args.user:
        clauses.append("user_id = :user_id")
        params["user_id"] = args.user
    if args.universe is not None:
        clauses.append("COALESCE(universe, '') = :universe")
        params["universe"] = args.universe
    where = " AND ".join(clauses)
    sql = (
        "SELECT factor_id, factor_name, factor_code, "
        "COALESCE(universe, '') AS universe, user_id, "
        "metadata_json->>'rre' AS rre, "
        "metadata_json->>'ann_turnover' AS ann_turnover, "
        "metadata_json->>'ann_return_net' AS ann_return_net "
        "FROM rd_agent_factors "
        f"WHERE {where} ORDER BY created_at"
    )
    if args.limit and args.limit > 0:
        sql += " LIMIT :limit"
        params["limit"] = int(args.limit)
    async with get_session(read_only=True) as session:
        rows = (await session.execute(text(sql), params)).mappings().all()
    return [dict(r) for r in rows]


def _metrics_missing(row: dict[str, Any]) -> bool:
    return any(
        not str(row.get(key) or "").strip()
        for key in ("rre", "ann_turnover", "ann_return_net")
    )


def _to_datetime_lenient(values: pd.Index) -> pd.DatetimeIndex:
    """to_datetime，容忍日期串书写形式不一（pandas ≥2 对混合格式默认直接抛）。"""
    try:
        return pd.to_datetime(values)
    except ValueError:
        return pd.to_datetime(values, format="mixed")


def _normalize_value_index(index: pd.Index) -> pd.Index:
    """值索引 0 层规整为 datetime64（行序/层名不动）。

    陷阱：``set_levels`` 要的是「该层的唯一值」（``index.levels[0]``），不是
    逐行数组（``get_level_values(0)``）——传逐行数组时跨标的重复日期直接撞
    MultiIndex 唯一性校验（2026-10-08 实测：真实面板崩在 ``Level values must
    be unique``，两万行堆栈 64MB；单标的测试数据碰不到）。日期串书写形式不
    一时 to_datetime 可能把两个唯一值折叠成一个，levels 路径不成立，退回
    按逐行值重建。
    """
    if not isinstance(index, pd.MultiIndex):
        return pd.Index(_to_datetime_lenient(index), name=index.name)
    converted = _to_datetime_lenient(index.levels[0])
    if converted.is_unique:
        return index.set_levels(converted, level=0)
    return pd.MultiIndex.from_arrays(
        [
            _to_datetime_lenient(index.get_level_values(0)),
            *[index.get_level_values(level) for level in range(1, index.nlevels)],
        ],
        names=index.names,
    )


def _align_values_returns(
    values: pd.Series, *, market_upper: str, universe: str, window_days: int
) -> tuple[pd.Series, pd.Series] | None:
    """面板值 × Qlib 次日收益对齐（口径与 qlib 回测同源，复用其纯函数）。

    值索引先归一到 (datetime, instrument)（物化器产出的是字符串日期 +
    前缀式代码，两层都要规整，否则「数据不足」假象——2026-10-07 教训）。
    样本不足返回 None；成功返回已剔非有限的 (factor, ret) 两序列——面板
    的 ``fret`` 列与指标评估**共用**这一份对齐（口径不会分叉）。
    """
    import numpy as np
    import qlib
    from qlib.data import D

    from backend.services.engine.routers.alpha_agent import (
        _align_factor_returns,
        _resolve_instruments_for_universe,
    )
    from backend.shared.qlib_paths import resolve_qlib_provider_uri

    normalized = values.copy()
    normalized.index = _normalize_value_index(normalized.index)
    days = sorted(
        {
            d.strftime("%Y-%m-%d")
            for d in normalized.index.get_level_values(0)
            if pd.notna(d)
        }
    )
    if len(days) < 2:
        return None
    start, end = days[-window_days], days[-1]
    provider_uri = resolve_qlib_provider_uri(market_upper)
    qlib.init(
        provider_uri=provider_uri,
        region="cn" if market_upper in ("CN", "HK", "FUTURES", "CRYPTO") else "us",
    )
    instruments = _resolve_instruments_for_universe(market_upper, universe or "csi300")
    price = D.features(
        instruments, ["$close"], start_time=start, end_time=end, freq="day"
    )
    if price.empty:
        return None
    f, r = _align_factor_returns(normalized, price["$close"])
    if len(f) < 100:
        return None
    mask = np.isfinite(f.to_numpy(dtype=float)) & np.isfinite(r.to_numpy(dtype=float))
    if int(mask.sum()) < 100:
        return None
    return f[mask], r[mask]


def _metrics_from_aligned(
    aligned: tuple[pd.Series, pd.Series] | None, *, universe: str
) -> dict[str, float]:
    """已对齐 (factor, ret) → 评估器链；None/空返回 {}。"""
    if aligned is None:
        return {}
    from backend.services.engine.mining_plugins import evaluate_paired

    f, r = aligned
    paired = pd.DataFrame(
        {
            "datetime": f.index.get_level_values(0),
            "symbol": f.index.get_level_values(1),
            "factor": f.to_numpy(dtype=float),
            "ret": r.to_numpy(dtype=float),
        }
    )
    metrics = evaluate_paired(paired, market="a_share", universe=universe, factor_id="")
    return {k: float(v) for k, v in metrics.items() if v is not None}


def _compute_metrics_for_values(
    values: pd.Series, *, market_upper: str, universe: str, window_days: int
) -> dict[str, float]:
    """对齐 → 评估器链；样本不足返回 {}（调用方保持既有告警行为）。"""
    return _metrics_from_aligned(
        _align_values_returns(
            values,
            market_upper=market_upper,
            universe=universe,
            window_days=window_days,
        ),
        universe=universe,
    )


def _panel_needs_rebuild(market: str, factor_id: str, *, force: bool) -> bool:
    """面板重建判据：强制 / 文件缺失 / 坏读 / 缺收益列（旧格式随 --panels 自动升级）。"""
    if force:
        return True
    from backend.services.engine.mining_plugins import pool_panels

    if not pool_panels.panel_path(market, factor_id).is_file():
        return True
    frame = pool_panels.read_panel(market, factor_id)
    return frame is None or "fret" not in frame.columns


async def _run_panel_metric_stage(args: argparse.Namespace) -> dict[str, Any]:
    """--panels / --metrics 合并一趟：因子代码每因子只执行一次，两个产物共享。"""
    from backend.scripts.rd_mined_materialize import (
        _compute_factor_values,
        _resolve_h5,
    )
    from backend.services.engine.mining_plugins import pool_panels

    stats: dict[str, Any] = {
        "candidates": 0,
        "panels_written": 0,
        "panels_planned": 0,
        "metrics_updated": 0,
        "metrics_planned": 0,
        "errors": 0,
    }
    if not (args.panels or args.metrics):
        return stats
    candidates = await _load_panel_metric_candidates(args)
    stats["candidates"] = len(candidates)
    h5_path: Path | None = None
    for row in candidates:
        factor_id = str(row.get("factor_id") or "")
        market = DEFAULT_MARKET
        need_panel = args.panels and _panel_needs_rebuild(
            market, factor_id, force=bool(args.force_panels)
        )
        need_metrics = args.metrics and (args.force_metrics or _metrics_missing(row))
        if not (need_panel or need_metrics):
            continue
        if need_panel:
            stats["panels_planned"] += 1
        if need_metrics:
            stats["metrics_planned"] += 1
        if not args.apply:
            continue
        try:
            if h5_path is None:
                h5_path = _resolve_h5(args.h5_path)
                logger.info("daily_pv.h5 = %s", h5_path)
            values = _compute_factor_values(row, h5_path, timeout=args.timeout)
            if values.empty:
                raise RuntimeError("因子值为全空")
            universe = str(row.get("universe") or "")
            # 一次对齐、两处共用：面板的 fret 列与指标评估绝不各算各的
            aligned = _align_values_returns(
                values,
                market_upper="CN",
                universe=universe,
                window_days=int(args.window_days),
            )
            if need_panel:
                ref = pool_panels.write_panel(
                    market,
                    factor_id,
                    values,
                    forward_return=aligned[1] if aligned is not None else None,
                )
                if ref is None:
                    raise RuntimeError("面板清洗后无有效值")
                stats["panels_written"] += 1
                logger.info("[panel] %s → %s", factor_id[:8], ref)
            if need_metrics:
                metrics = _metrics_from_aligned(aligned, universe=universe)
                if metrics:
                    from backend.services.engine.qlib_app.services.rd_agent_persistence import (
                        RDAgentFactorPersistence,
                    )

                    await RDAgentFactorPersistence().update_factor_metrics(
                        factor_id=factor_id, metadata=metrics
                    )
                    stats["metrics_updated"] += 1
                    logger.info("[metrics] %s ← %s", factor_id[:8], sorted(metrics))
                else:
                    logger.warning(
                        "[metrics] %s 对齐样本不足，未产出指标", factor_id[:8]
                    )
        except Exception:  # noqa: BLE001 - 单因子失败继续
            logger.exception("[panel/metrics] %s 失败", factor_id[:8])
            stats["errors"] += 1
    return stats


# ── 阶段 3：池刷新 ─────────────────────────────────────────────────────


async def _run_refresh_stage(args: argparse.Namespace) -> dict[str, Any]:
    from backend.services.engine.mining_plugins import pool_service

    scopes = await _discover_scopes(args)
    stats: dict[str, Any] = {
        "scopes": len(scopes),
        "factors": 0,
        "panels": 0,
        "corr_edges": 0,
        "formula_edges": 0,
        "task_edges": 0,
        "scored": 0,
        "reports": [],
    }
    for scope in scopes:
        report = await pool_service.refresh_pool(
            user_id=scope["user_id"],
            market=scope["market"],
            universe=str(scope["universe"] or ""),
            dry_run=not args.apply,
        )
        for key in (
            "factors",
            "panels",
            "corr_edges",
            "formula_edges",
            "task_edges",
            "scored",
        ):
            stats[key] += int(report.get(key) or 0)
        logger.info(
            "[refresh] user=%s market=%s universe=%r：因子 %s / 面板 %s / 边 %s+%s+%s",
            scope["user_id"],
            scope["market"],
            scope["universe"],
            report.get("factors"),
            report.get("panels"),
            report.get("corr_edges"),
            report.get("formula_edges"),
            report.get("task_edges"),
        )
        if len(stats["reports"]) < _MAX_SCOPE_REPORTS:
            stats["reports"].append({"scope": scope, "report": report})
    return stats


# ── 主流程 ─────────────────────────────────────────────────────────────


async def _run(args: argparse.Namespace) -> int:
    lock = _acquire_run_lock()
    if lock is None:
        logger.warning("另一个因子池刷新进程正在运行，本次跳过（避免并发写同池）")
        return 0
    trigger = os.environ.get("QM_POOL_REBUILD_TRIGGER", "cli")
    started_at = _now_iso()
    _write_status(
        {
            "status": "running",
            "started_at": started_at,
            "trigger": trigger,
            "args": {
                "user": args.user,
                "market": args.market,
                "universe": args.universe,
                "apply": bool(args.apply),
                "panels": bool(args.panels),
                "metrics": bool(args.metrics),
            },
        }
    )
    try:
        summary: dict[str, Any] = {"apply": bool(args.apply)}
        if args.panels or args.metrics:
            summary["panel_metrics"] = await _run_panel_metric_stage(args)
        if not args.no_refresh:
            summary["refresh"] = await _run_refresh_stage(args)
        _write_status(
            {
                "status": "done",
                "started_at": started_at,
                "finished_at": _now_iso(),
                "trigger": trigger,
                "summary": summary,
            }
        )
        logger.info("汇总：%s", json.dumps(summary, ensure_ascii=False, default=str))
        errors = int((summary.get("panel_metrics") or {}).get("errors") or 0)
        return 0 if errors == 0 else 1
    except Exception as exc:  # noqa: BLE001 - 状态落盘后原样上抛给 main 记日志
        _write_status(
            {
                "status": "failed",
                "started_at": started_at,
                "finished_at": _now_iso(),
                "trigger": trigger,
                "error": str(exc)[:500],
            }
        )
        raise
    finally:
        lock.close()


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="因子池回填/刷新器（默认 dry-run）")
    parser.add_argument("--user", help="只处理该用户的因子（缺省=全部用户）")
    parser.add_argument(
        "--market",
        default=DEFAULT_MARKET,
        choices=_MARKETS,
        help="市场（默认 a_share）",
    )
    parser.add_argument(
        "--universe", default=None, help="只处理该 universe（缺省=全部）"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="只统计不落库（默认）")
    mode.add_argument("--apply", action="store_true", help="真正写库")
    parser.add_argument("--panels", action="store_true", help="回填缺失面板缓存")
    parser.add_argument("--force-panels", action="store_true", help="面板存在也重算")
    parser.add_argument("--metrics", action="store_true", help="回填缺失的评估指标")
    parser.add_argument("--force-metrics", action="store_true", help="指标存在也重算")
    parser.add_argument("--no-refresh", action="store_true", help="跳过池刷新阶段")
    parser.add_argument(
        "--window-days",
        type=int,
        default=_METRICS_WINDOW_DAYS,
        help="指标回填的收益窗口（交易日，默认 750）",
    )
    parser.add_argument("--h5-path", help="daily_pv.h5 路径（缺省用共享缓存/现场生成）")
    parser.add_argument(
        "--timeout", type=int, default=_EXEC_TIMEOUT_S, help="单因子执行超时秒数"
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="panels/metrics 最多处理条数"
    )
    parser.add_argument("--verbose", action="store_true", help="调试日志")
    args = parser.parse_args(argv)
    args.apply = bool(args.apply)
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
