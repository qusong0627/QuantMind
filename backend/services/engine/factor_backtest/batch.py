"""T-FB-08/09 批量回测引擎（服务端，样板 = 补码评估批）。

一个「批次」= factor_ids × markets 的单元集合。单元 = (factor_id, market)，
批次级常量（窗口/费率）+ 单元的键即规划稿去重键 ``(factor, market, window,
cost)`` 的展开——同批次内 (factor, market) 先例去重，跨批次按**活跃 running 行**
背压（因子粒度：子进程登记以 factor_id 为键，同一因子不得跨市场并发）。

纪律（继承补码评估批与台账契约）：
- **进度/成败一律行终态回读**：每个单元先在台账落 running 行（带 batch_id），
  跑完由 ``_run_single`` 收口，worker 再 ``get_run`` 回读终态——内存从不自造
  终态结论；进程重启后状态端点/启动扫描可从台账完整重建。
- **运行中接管旧链成果**：单元执行直接复用 ``router._run_single``（取消、
  身份守卫、序列落盘语义与单发链路逐字一致），不复制第二份生命周期。
- **重启可重入**：重建时遗留 running 行收口为 failed
  (``engine_restarted_mid_run``)，该单元**重试一次**（第二次再中断不再重试，
  防重启-重试死循环）；未启动单元照常续跑。无批次归属的**孤儿单跑**同值
  收口但**不重试**（单跑没有 spec/单元表可续跑，UI 如实显示 failed，
  用户重派）。
- **熔断**：连续 N 个单元 failed（``QM_BACKTEST_MAX_CONSEC_FAILS``，默认 5）
  → aborted；completed/data_unsupported/insufficient/unavailable 是适配结论，
  重置连败计数——降级终态不是故障。
- **取消走标记不拆身份守卫**：用户取消 → 杀当前子进程（与单发取消同一路径）
  → worker 在循环顶自退；未启动单元不落行（诚实：它们从未运行）。
- 排水进程内串行（``QM_BACKTEST_MAX_CONCURRENCY`` 默认 1，语义=worker 数；
  同一因子的单元天然互斥——eligibility 检查即 alpha_agent 的
  ``_running_backtests`` 占位表）。
- 后台任务只经 ``_spawn`` 创建（测试挂桩点，仿 router 的
  ``_spawn_backtest_run``：TestClient 请求级 loop 会取消后台任务）。
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from backend.services.engine.factor_backtest import store
from backend.services.engine.factor_backtest.profiles import get_market_profile
from backend.services.engine.routers.alpha_agent import (
    _backtest_cancelled,
    _running_backtest_runs,
    _running_backtests,
)

logger = logging.getLogger(__name__)

#: 重启中断的收口错误标记（续跑重试判据：恰以此值识别「可重试的中断」）。
_RESTART_ERROR = "engine_restarted_mid_run"

#: 单元状态聚合进响应的指标键（缺键回落台账专列，缺失一律 None → 前端「—」）。
_UNIT_METRIC_KEYS = (
    "ic",
    "rank_ic",
    "icir",
    "rank_icir",
    "sharpe",
    "ann_return_net",
    "max_drawdown",
    "n_days",
)

#: 因子被单发/他批占用的等待重试间隔（秒）；等待期间循环重扫 pending。
_ELIGIBILITY_POLL_S = 5.0


def max_concurrency() -> int:
    """worker 数（默认 1 = 进程内串行；env 可升，同因子仍互斥）。"""
    raw = os.getenv("QM_BACKTEST_MAX_CONCURRENCY", "1")
    try:
        return max(1, int(raw))
    except ValueError:
        return 1


def max_consec_fails() -> int:
    """连败熔断阈值。"""
    raw = os.getenv("QM_BACKTEST_MAX_CONSEC_FAILS", "5")
    try:
        return max(1, int(raw))
    except ValueError:
        return 5


@dataclass
class BatchState:
    """进程内批次状态（可丢失：台账行是唯一事实源，重建见 resume_batch）。"""

    batch_id: str
    spec: dict[str, Any]
    pending: list[dict[str, str]]
    cancelled: bool = False
    stop_reason: str | None = None  # cancelled / circuit_breaker / drain_crashed
    consec_fails: int = 0
    running_units: dict[tuple[str, str], str] = field(default_factory=dict)
    wakeup: asyncio.Event = field(default_factory=asyncio.Event)


#: batch_id → 状态（仅内存；终态批次在 drain 收口后移出）。
_STATES: dict[str, BatchState] = {}
#: 待排水批次 FIFO（惰性创建：避免模块导入期绑死事件循环）。
_QUEUE: asyncio.Queue[str] | None = None
#: 单排水调度任务（串行消费 _QUEUE；批次逐个跑完）。
_SCHEDULER: asyncio.Task | None = None


def _spawn(coro) -> asyncio.Task:
    """后台启动（测试挂桩点，语义见模块 docstring）。"""
    return asyncio.create_task(coro)


def _router():
    """延迟导入 router：router 在模块级 import 本模块（端点），此处反向引用
    只能发生在运行期（drain/取消），避免导入环。"""
    from backend.services.engine.factor_backtest import router

    return router


def _ensure_queue() -> asyncio.Queue[str]:
    global _QUEUE
    if _QUEUE is None:
        _QUEUE = asyncio.Queue()
    return _QUEUE


def _ensure_scheduler() -> None:
    global _SCHEDULER
    if _SCHEDULER is None or _SCHEDULER.done():
        _SCHEDULER = _spawn(_scheduler_loop())


# ── 派发 ─────────────────────────────────────────────────────────────


async def launch(
    *,
    factors: dict[str, dict],
    kinds: dict[str, str],
    markets: list[str],
    start: str | None,
    end: str | None,
    cost_bps: int | None,
    user_id: str | None,
    skipped: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """登记批次并入队。``factors`` 已过归属/代码校验；调用方负责背压过滤。"""
    batch_id = f"fbb-{uuid4().hex}"
    units = [
        {"factor_id": fid, "market": market} for fid in factors for market in markets
    ]
    spec: dict[str, Any] = {
        "factor_ids": list(factors),
        "markets": list(markets),
        "start": start,
        "end": end,
        "cost_bps": cost_bps,
        "kinds": dict(kinds),
        "units": units,
        "skipped": list(skipped or []),
    }
    state = BatchState(batch_id=batch_id, spec=spec, pending=list(units))
    # 先占位再落库：关死「create_batch 落库 → 入队」窗口内状态轮询触发
    # resume_batch 的重复重建（resume 对已在 _STATES 的批次直接拒绝）。
    _STATES[batch_id] = state
    try:
        await store.create_batch(batch_id, user_id=user_id, spec=spec)
    except Exception:
        _STATES.pop(batch_id, None)
        raise
    _ensure_queue().put_nowait(batch_id)
    _ensure_scheduler()
    return {
        "batch_id": batch_id,
        "total": len(units),
        "queued": len(units),
        "skipped": spec["skipped"],
    }


# ── 取消 ─────────────────────────────────────────────────────────────


async def cancel_batch(batch_id: str) -> dict[str, Any]:
    """取消批次：杀当前单元（单发取消同一路径）+ 批次行收口 cancelled。

    未启动单元不落行；已在排水的批次的 drain 收口与这里幂等（先到先赢）。
    """
    state = _STATES.get(batch_id)
    killed: list[dict[str, str]] = []
    if state is not None:
        state.cancelled = True
        router = _router()
        for (fid, market), run_id in list(state.running_units.items()):
            _backtest_cancelled.add(fid)
            try:
                router._kill_backtest_process(fid)
            except Exception as exc:  # noqa: BLE001 — 杀失败由任务侧自退兜底
                logger.warning("[factor-backtest-batch] kill %s 失败: %s", fid, exc)
            try:
                await store.finish_run(run_id, "cancelled", error="cancelled_by_user")
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[factor-backtest-batch] 取消收口失败 %s: %s", run_id, exc
                )
            killed.append({"factor_id": fid, "market": market, "run_id": run_id})
        state.wakeup.set()
    closed = False
    try:
        closed = await store.finish_batch(
            batch_id, "cancelled", error="cancelled_by_user"
        )
    except Exception as exc:  # noqa: BLE001 — 端点会重读批次行报告实况
        logger.warning("[factor-backtest-batch] 批次收口失败 %s: %s", batch_id, exc)
    return {"killed": killed, "closed": closed}


# ── 重启可重入 ───────────────────────────────────────────────────────


def _pending_units(
    spec: dict[str, Any], rows: list[dict[str, Any]]
) -> list[dict[str, str]]:
    """未定案单元 = 没有任何行 + 「重启中断」恰一次的重试单元（只重试一次）。"""
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rows:
        by_key.setdefault((r["factor_id"], r.get("market")), []).append(r)
    pending: list[dict[str, str]] = []
    for u in spec.get("units") or []:
        rs = by_key.get((u["factor_id"], u["market"]))
        if not rs:
            pending.append(u)
            continue
        if len(rs) < 2 and all(
            r["status"] == "failed" and r.get("error") == _RESTART_ERROR for r in rs
        ):
            pending.append(u)
    return pending


async def resume_batch(batch_id: str) -> bool:
    """重建并续跑一个 DB 仍为 running 的批次（内存态已丢失：重启/端点竞态）。"""
    if batch_id in _STATES:
        return False
    row = await store.get_batch(batch_id)
    if row is None or row.get("status") != "running":
        return False
    spec = row.get("spec") or {}
    state = BatchState(batch_id=batch_id, spec=spec, pending=[])
    _STATES[batch_id] = state  # 先占位：并发状态轮询不得重复重建
    try:
        if spec.get("units"):
            settled = await store.settle_orphan_running(batch_id, error=_RESTART_ERROR)
            if settled:
                logger.warning(
                    "[factor-backtest-batch] %s 重启恢复：收口 %d 个孤儿 running 行",
                    batch_id,
                    settled,
                )
            rows = await store.batch_runs(batch_id)
            state.pending = _pending_units(spec, rows)
        _ensure_queue().put_nowait(batch_id)
        _ensure_scheduler()
    except Exception:
        _STATES.pop(batch_id, None)
        raise
    return True


async def resume_interrupted() -> int:
    """engine 启动扫描：收口孤儿单跑 + 把中断批次全部重新入队。返回恢复批次数。

    单跑（无 batch_id）不在批次重建面内——没有 spec/单元表可续跑，唯一正确
    的处置是收口 failed（``engine_restarted_mid_run``）让台账/矩阵如实显示；
    收口失败只告警，绝不拦批次恢复。
    """
    try:
        settled_singles = await store.settle_orphan_single_runs(error=_RESTART_ERROR)
        if settled_singles:
            logger.warning(
                "[factor-backtest] 重启恢复：收口 %d 个孤儿单跑 running 行",
                settled_singles,
            )
    except Exception as exc:  # noqa: BLE001 — 单跑收口失败不拦批次恢复
        logger.warning("[factor-backtest] 单跑孤儿收口失败: %s", exc)
    try:
        rows = await store.list_running_batches()
    except Exception as exc:  # noqa: BLE001 — 启动路径永不因此失败
        logger.warning("[factor-backtest-batch] 重启扫描失败: %s", exc)
        return 0
    count = 0
    for r in rows:
        try:
            if await resume_batch(r["batch_id"]):
                count += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[factor-backtest-batch] 恢复 %s 失败: %s", r["batch_id"], exc
            )
    return count


# ── 排水 ─────────────────────────────────────────────────────────────


async def _scheduler_loop() -> None:
    queue = _ensure_queue()
    while True:
        batch_id = await queue.get()
        state = _STATES.get(batch_id)
        if state is None:
            continue
        if state.cancelled:
            _STATES.pop(batch_id, None)
            continue
        try:
            await _drain_batch(state)
        except Exception:  # noqa: BLE001 — drain 自收口；此处只兜崩溃善后
            logger.exception("[factor-backtest-batch] drain 崩溃 %s", batch_id)
            _STATES.pop(batch_id, None)


async def _drain_batch(state: BatchState) -> None:
    """跑完（或熔断/取消打断）一个批次，然后**收口批次行**。"""
    tasks = [_spawn(_worker(state)) for _ in range(max_concurrency())]
    await asyncio.gather(*tasks, return_exceptions=True)

    if state.cancelled or state.stop_reason == "cancelled":
        await _safe_finish_batch(state, "cancelled", "cancelled_by_user")
    elif state.stop_reason == "circuit_breaker":
        await _safe_finish_batch(
            state,
            "aborted",
            f"circuit_breaker: {state.consec_fails} consecutive failures",
        )
    elif state.stop_reason:
        await _safe_finish_batch(state, "aborted", state.stop_reason)
    else:
        await _safe_finish_batch(state, "completed", None)
    _STATES.pop(state.batch_id, None)


async def _safe_finish_batch(state: BatchState, status: str, error: str | None) -> None:
    try:
        await store.finish_batch(state.batch_id, status, error=error)
    except Exception as exc:  # noqa: BLE001 — 行仍 running，下次续跑重建后收口
        logger.warning(
            "[factor-backtest-batch] 批次收口失败 %s: %s", state.batch_id, exc
        )


def _reserve_eligible(state: BatchState) -> dict[str, str] | None:
    """摘出一个可跑的单元并**原子占位**（照 single 端点 check→add 无 await 纪律：
    同一因子不得跨市场/跨批次并发——子进程登记表以 factor_id 为键）。"""
    for i, unit in enumerate(state.pending):
        fid = unit["factor_id"]
        if fid in _running_backtests or fid in _backtest_cancelled:
            continue
        state.pending.pop(i)
        _running_backtests.add(fid)
        return unit
    return None


async def _worker(state: BatchState) -> None:
    while True:
        if state.cancelled or state.stop_reason:
            return
        unit = _reserve_eligible(state)
        if unit is None:
            if not state.pending:
                return
            state.wakeup.clear()
            try:  # 被占因子（单发/他批）释放由本批完成事件唤醒；超时兜底重扫
                await asyncio.wait_for(state.wakeup.wait(), timeout=_ELIGIBILITY_POLL_S)
            except asyncio.TimeoutError:
                pass
            continue
        try:
            status = await _execute_unit(state, unit)
        except Exception:  # noqa: BLE001 — _execute_unit 内部已兜底；纯保险
            logger.exception(
                "[factor-backtest-batch] 单元异常 %s@%s",
                unit["factor_id"],
                unit["market"],
            )
            status = "failed"
            fid = unit["factor_id"]
            if _running_backtest_runs.get(fid) is None:
                _running_backtests.discard(fid)
        state.wakeup.set()
        if status == "failed":
            state.consec_fails += 1
            if state.consec_fails >= max_consec_fails():
                state.stop_reason = "circuit_breaker"
                logger.error(
                    "[factor-backtest-batch] %s 连续 %d 个单元失败，熔断中止",
                    state.batch_id,
                    state.consec_fails,
                )
                return
        else:
            state.consec_fails = 0


async def _execute_unit(state: BatchState, unit: dict[str, str]) -> str:
    """跑一个单元：落 running 行 → ``router._run_single``（完整生命周期）→
    行终态回读。调用前提：``_reserve_eligible`` 已占位（fid ∈ _running_backtests）。"""
    fid = unit["factor_id"]
    market = unit["market"]
    router = _router()

    metas = await store.get_factor_meta([fid])
    meta = metas[0] if metas else None
    kind = (state.spec.get("kinds") or {}).get(fid) or "unknown"
    try:
        run_id = await store.start_run(
            fid,
            kind=kind,
            market=market,
            universe=_default_universe(market),
            params={
                "start": state.spec.get("start"),
                "end": state.spec.get("end"),
                "cost_bps": state.spec.get("cost_bps"),
                "batch_id": state.batch_id,
            },
            factor_name=(meta or {}).get("factor_name"),
            user_id=(meta or {}).get("user_id"),
            batch_id=state.batch_id,
        )
    except Exception:  # noqa: BLE001 — 台账不可用：放回队首按失败计数（熔断兜底）
        logger.exception("[factor-backtest-batch] 台账登记失败 %s@%s", fid, market)
        _running_backtests.discard(fid)
        state.pending.insert(0, unit)
        return "failed"

    if meta is None or not meta.get("factor_code"):
        _running_backtests.discard(fid)
        await store.finish_run(run_id, "failed", error="因子不存在或代码为空")
        return "failed"

    _running_backtest_runs[fid] = run_id
    state.running_units[(fid, market)] = run_id
    try:
        # _run_single 的 finally 做身份守卫清理（含 _running_backtests 占位）。
        await router._run_single(
            fid,
            meta,
            run_id,
            market=market,
            universe=None,
            start=state.spec.get("start"),
            end=state.spec.get("end"),
            cost_bps=state.spec.get("cost_bps"),
        )
    finally:
        state.running_units.pop((fid, market), None)

    row = await store.get_run(run_id)
    return (row or {}).get("status") or "failed"


def _default_universe(market: str) -> str | None:
    """台账行记档案默认池名（与单发端点同口径）；未知市场回落 None。"""
    try:
        return get_market_profile(market).default_universe
    except KeyError:
        return None


# ── 状态 ─────────────────────────────────────────────────────────────


async def get_batch_status(batch_id: str) -> dict[str, Any] | None:
    """批次进度：计数与单元明细**全部来自台账行终态回读**，内存只补充
    ``draining/current/consec_fails``（进度活口）。批次不存在返回 None。"""
    row = await store.get_batch(batch_id)
    if row is None:
        return None
    spec = row.get("spec") or {}
    units = spec.get("units") or []
    rows = await store.batch_runs(batch_id)

    latest: dict[tuple[str, str], dict[str, Any]] = {}
    attempts: dict[tuple[str, str], int] = {}
    for r in rows:
        key = (r["factor_id"], r.get("market"))
        attempts[key] = attempts.get(key, 0) + 1
        cur = latest.get(key)
        if cur is None or (r["created_at"], r["run_id"]) > (
            cur["created_at"],
            cur["run_id"],
        ):
            latest[key] = r

    counts = dict.fromkeys(("pending", "running") + store.TERMINAL_STATUSES, 0)
    units_out: list[dict[str, Any]] = []
    for u in units:
        key = (u["factor_id"], u["market"])
        r = latest.get(key)
        entry: dict[str, Any] = {
            "factor_id": u["factor_id"],
            "market": u["market"],
            "status": "pending",
            "run_id": None,
            "attempts": attempts.get(key, 0),
            "error": None,
            "finished_at": None,
            "ic": None,
            "rank_ic": None,
            "icir": None,
            "sharpe": None,
            "max_drawdown": None,
            "n_days": None,
        }
        if r is not None:
            metrics = r.get("metrics") or {}
            status = r["status"] if r["status"] in counts else "failed"
            counts[status] += 1
            entry.update(
                {
                    "status": status,
                    "run_id": r["run_id"],
                    "error": r.get("error"),
                    "finished_at": (
                        r["finished_at"].isoformat() if r.get("finished_at") else None
                    ),
                }
            )
            for key_metric, col in (
                ("ic", "ic_value"),
                ("rank_ic", "rank_ic"),
                ("icir", "icir"),
                ("sharpe", "sharpe_ratio"),
                ("max_drawdown", "max_drawdown"),
            ):
                val = metrics.get(key_metric)
                entry[key_metric] = val if val is not None else r.get(col)
            entry["n_days"] = metrics.get("n_days")
        else:
            counts["pending"] += 1
        units_out.append(entry)

    state = _STATES.get(batch_id)
    draining = bool(state is not None and not state.cancelled)
    current = [
        {"factor_id": fid, "market": market, "run_id": run_id}
        for (fid, market), run_id in (state.running_units if state else {}).items()
    ]
    done = sum(counts[s] for s in store.TERMINAL_STATUSES)
    return {
        "batch": {
            "batch_id": row["batch_id"],
            "user_id": row.get("user_id"),
            "status": row["status"],
            "error": row.get("error"),
            "created_at": (
                row["created_at"].isoformat() if row.get("created_at") else None
            ),
            "finished_at": (
                row["finished_at"].isoformat() if row.get("finished_at") else None
            ),
        },
        "spec": {
            "factor_ids": spec.get("factor_ids") or [],
            "markets": spec.get("markets") or [],
            "start": spec.get("start"),
            "end": spec.get("end"),
            "cost_bps": spec.get("cost_bps"),
            "skipped": spec.get("skipped") or [],
        },
        "progress": {
            **counts,
            "total": len(units),
            "done": done,
            "consec_fails": state.consec_fails if state else 0,
            "max_consec_fails": max_consec_fails(),
            "draining": draining,
            "current": current,
        },
        "units": units_out,
        "failures": [u for u in units_out if u["status"] == "failed"],
    }
