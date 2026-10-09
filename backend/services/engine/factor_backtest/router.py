"""T-FB-07/08 ``/api/v1/factor-backtest`` 端点组。

职责：把「因子挖掘 → 回测中心」的用例翻成对 engine/store 的编排：
- ``GET  /markets``            市场档案 + 数据面实况（矩阵列头）
- ``POST /single``             单因子单市场发起（后台任务，台账 instantly 见）
- ``POST /single/{id}/cancel`` 取消（kill 子进程；**不动因子行**）
- ``POST /batch``              批量派发（factors × markets，去重/背压，见 batch.py）
- ``GET  /batch/status``       批次进度（行终态回读 + 熔断/失败清单）
- ``GET  /batches``            批次列表（UI 重开页面后找回在跑的批次）
- ``POST /batch/cancel``       取消批次（杀当前单元；未启动单元不落行）
- ``POST /matrix``             因子 × 市场适配矩阵（每格=最近一次运行 + 静态兼容）
- ``GET  /runs``               台账列表（可筛可排的数据面）
- ``GET  /runs/{id}/series``   曲线下钻（IC/净值/分位/换手）

纪律（与旧回测链共存的边界）：
- 去重与取消**共享** ``alpha_agent`` 的 ``_running_backtests``/
  ``_backtest_processes``/``_backtest_cancelled``——子进程登记按 factor_id 键，
  新旧两条链必须全局串行；旧 UI 的取消也能 kill 新链的子进程（反向亦然）。
- 新链**绝不更新 rd_agent_factors 行**：因子库指标是 CN 语义，跨市场结果只进
  台账 + 序列表。
- 后台任务用 ``asyncio.create_task``（engine 常驻进程内）＋测试挂桩点
  ``_spawn_backtest_run``（TestClient 的请求级 loop 会取消后台任务，路由级
  测试 monkeypatch 本函数）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from backend.services.engine.auth_context import get_authenticated_identity
from backend.services.engine.factor_backtest import batch, store
from backend.services.engine.factor_backtest.compat import classify_factor
from backend.services.engine.factor_backtest.engine import evaluate_factor_market
from backend.services.engine.factor_backtest.profiles import (
    columns_for_market,
    get_market_profile,
    list_market_profiles,
    profile_status,
)
from backend.services.engine.routers.alpha_agent import (
    FactorBacktestCancelled,
    _backtest_cancelled,
    _backtest_processes,
    _detect_factor_kind,
    _format_backtest_error,
    _require_owned_factor,
    _running_backtest_runs,
    _running_backtests,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/factor-backtest", tags=["factor-backtest"])

#: 矩阵单元格展示的指标键（metrics_json 里拣出来的排序维度）。
_CELL_METRIC_KEYS = (
    "ic",
    "rank_ic",
    "icir",
    "rank_icir",
    "ic_nw_t",
    "ic_positive_rate",
    "sharpe",
    "sharpe_net",
    "ann_return",
    "ann_return_net",
    "ann_vol",
    "max_drawdown",
    "ann_turnover",
    "ls_sharpe",
    "ls_ann_return",
    "n_days",
)


class SingleBacktestPayload(BaseModel):
    factor_id: str = Field(..., min_length=1, max_length=128)
    market: str = Field(..., min_length=1, max_length=32)
    universe: str | None = Field(None, max_length=64)
    start: str | None = Field(None, max_length=16)
    end: str | None = Field(None, max_length=16)
    cost_bps: int | None = Field(None, ge=0, le=1000)


class MatrixPayload(BaseModel):
    factor_ids: list[str] = Field(..., min_length=1, max_length=500)
    markets: list[str] | None = Field(None, max_length=10)


class BatchPayload(BaseModel):
    factor_ids: list[str] = Field(..., min_length=1, max_length=200)
    markets: list[str] | None = Field(None, max_length=10)  # None = 全部市场
    start: str | None = Field(None, max_length=16)
    end: str | None = Field(None, max_length=16)
    cost_bps: int | None = Field(None, ge=0, le=1000)


class BatchCancelPayload(BaseModel):
    batch_id: str = Field(..., min_length=1, max_length=64)


def _spawn_backtest_run(coro) -> None:
    """后台启动（测试挂桩点，其语义见模块 docstring）。"""
    asyncio.create_task(coro)


def _kill_backtest_process(factor_id: str) -> None:
    """kill 该因子在跑的子进程（terminate → 等 2s → kill）。"""
    proc = _backtest_processes.get(factor_id)
    if not proc or proc.poll() is not None:
        return
    try:
        proc.terminate()
        for _ in range(10):
            if proc.poll() is not None:
                break
            import time

            time.sleep(0.2)  # noqa: ASYNC251 — 由取消端点在线程侧短等，最长 2s
        if proc.poll() is None:
            proc.kill()
            logger.warning(
                "[factor-backtest] force-killed subprocess for %s", factor_id
            )
    except ProcessLookupError:
        pass
    except Exception as e:  # noqa: BLE001
        logger.warning("[factor-backtest] cancel kill %s failed: %s", factor_id, e)


# ── 市场档案 ─────────────────────────────────────────────────────────


@router.get("/markets")
async def list_markets():
    """五市场档案 + 数据面实况（CN 列 = 样本内基准，其余样本外）。"""
    rows = []
    for profile in list_market_profiles():
        status = profile_status(profile)
        status.pop("provider", None)  # 路径是内部实现细节，不出接口
        rows.append(status)
    return {"code": 200, "data": {"markets": rows, "total": len(rows)}}


# ── 单因子发起 / 取消 ────────────────────────────────────────────────


@router.post("/single")
async def start_single_backtest(request: Request, payload: SingleBacktestPayload):
    """发起单因子单市场回测（异步；台账行秒级可见，前端轮询 /runs）。"""
    factor = await _require_owned_factor(payload.factor_id, request)
    if not factor.get("factor_code"):
        raise HTTPException(status_code=400, detail="因子代码为空，无法回测")
    try:
        profile = get_market_profile(payload.market)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    factor_id = payload.factor_id
    if factor_id in _running_backtests:
        return {
            "code": 200,
            "data": {
                "factor_id": factor_id,
                "status": "running",
                "message": "回测已在进行中",
            },
        }

    # 静态预检只为台账行先记上 kind；终态裁决在 engine（data_unsupported 等
    # 也要落一行——矩阵需要看到「为什么没有数字」）。
    try:
        kind = _detect_factor_kind(factor.get("factor_code") or "")
    except RuntimeError:
        kind = "unknown"

    # 占位：check→add 之间无 await，双击不会并发双跑（照旧链同纪律）。
    _running_backtests.add(factor_id)
    try:
        run_id = await store.start_run(
            factor_id,
            kind=kind,
            market=payload.market,
            universe=payload.universe or profile.default_universe,
            params={
                "universe": payload.universe,
                "start": payload.start,
                "end": payload.end,
                "cost_bps": payload.cost_bps,
            },
            factor_name=factor.get("factor_name"),
            user_id=factor.get("user_id"),
        )
    except Exception as exc:
        _running_backtests.discard(factor_id)
        logger.error("[factor-backtest] 台账登记失败 %s: %s", factor_id, exc)
        raise HTTPException(status_code=500, detail="台账登记失败") from exc
    _running_backtest_runs[factor_id] = run_id

    _spawn_backtest_run(
        _run_single(
            factor_id,
            factor,
            run_id,
            market=payload.market,
            universe=payload.universe,
            start=payload.start,
            end=payload.end,
            cost_bps=payload.cost_bps,
        )
    )
    return {
        "code": 200,
        "data": {
            "factor_id": factor_id,
            "run_id": run_id,
            "market": payload.market,
            "status": "running",
            "message": f"跨市场回测已触发: {factor.get('factor_name')} @ {profile.label}",
        },
    }


@router.post("/single/{factor_id}/cancel")
async def cancel_single_backtest(factor_id: str, request: Request):
    """取消进行中的跨市场回测（kill 子进程 + 台账收口 cancelled）。

    与旧取消端点的差异：**不写 rd_agent_factors 行**（跨市场 run 的纪律）。
    去重标记不在本端点拆——归后台任务 finally 以身份守卫清理（旧实现同注释：
    提前拆会让旧任务的延迟收尾误拆新任务的标记）。
    """
    await _require_owned_factor(factor_id, request)
    if factor_id not in _running_backtests:
        return {
            "code": 200,
            "data": {
                "factor_id": factor_id,
                "status": None,
                "message": "回测未在运行",
            },
        }
    _backtest_cancelled.add(factor_id)
    _kill_backtest_process(factor_id)
    run_id = _running_backtest_runs.get(factor_id)
    if run_id:
        try:
            await store.finish_run(run_id, "cancelled", error="cancelled_by_user")
        except Exception as exc:  # noqa: BLE001 — 收口失败由任务侧兜底
            logger.warning("[factor-backtest] cancel 收口失败 %s: %s", run_id, exc)
    return {"code": 200, "data": {"factor_id": factor_id, "status": "cancelled"}}


async def _run_single(
    factor_id: str,
    factor: dict,
    run_id: str,
    *,
    market: str,
    universe: str | None,
    start: str | None,
    end: str | None,
    cost_bps: int | None,
) -> None:
    """单因子回测后台任务：engine 求值 → 台账收口（+ 序列落盘）→ 去重清理。"""
    try:
        res = await evaluate_factor_market(
            factor,
            market=market,
            universe=universe,
            start=start,
            end=end,
            cost_bps=cost_bps,
        )
        win = res.get("window") or {}
        date_range = (
            f"{win.get('start')}~{win.get('end')}" if win.get("start") else None
        )
        if res["status"] == "ok":
            m = res["metrics"] or {}
            await store.finish_run(
                run_id,
                "completed",
                ic_value=m.get("ic"),
                rank_ic=m.get("rank_ic"),
                icir=m.get("icir"),
                rank_icir=m.get("rank_icir"),
                sharpe_ratio=m.get("sharpe"),
                annual_return=m.get("ann_return"),
                max_drawdown=m.get("max_drawdown"),
                universe=res.get("universe"),
                data_source="qlib_bin",
                date_range=date_range,
                metrics=m,
            )
            if res.get("series") is not None:
                try:
                    await store.save_series(
                        run_id,
                        factor_id=factor_id,
                        market=market,
                        payload=res["series"],
                    )
                except Exception as exc:  # noqa: BLE001 — 序列是下钻层：缺失可复检
                    logger.warning(
                        "[factor-backtest] 序列落盘失败（不拦收口）%s: %s", run_id, exc
                    )
        else:
            await store.finish_run(
                run_id,
                res["status"],
                error=(res.get("message") or res.get("reason")),
                universe=res.get("universe"),
                data_source="qlib_bin",
                date_range=date_range,
            )
    except FactorBacktestCancelled:
        try:
            await store.finish_run(run_id, "cancelled", error="cancelled_by_user")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[factor-backtest] cancelled 收口失败 %s: %s", run_id, exc)
    except Exception as exc:  # noqa: BLE001 — 兜底：任何异常都得有终态
        logger.exception("[factor-backtest] %s 后台任务异常", factor_id)
        try:
            await store.finish_run(
                run_id, "failed", error=_format_backtest_error(exc)[-1500:]
            )
        except Exception as exc2:  # noqa: BLE001
            logger.error("[factor-backtest] failed 收口失败 %s: %s", run_id, exc2)
    finally:
        # 身份守卫清理（照旧链 finally 注释）：取消→立即重跑后，旧任务延迟收尾
        # 不得拆新任务的去重键/取消标记。
        if (
            factor_id not in _running_backtest_runs
            or _running_backtest_runs[factor_id] == run_id
        ):
            _running_backtest_runs.pop(factor_id, None)
            _running_backtests.discard(factor_id)
            _backtest_cancelled.discard(factor_id)


# ── 批量派发 / 进度 / 取消 ───────────────────────────────────────────


def _require_owned_batch(row: dict, request: Request) -> None:
    """批次归属：他人批次一律 404（不泄露存在性）；空属主历史行只读可见。"""
    auth_user_id, _ = get_authenticated_identity(request)
    owner = row.get("user_id")
    if owner and owner != auth_user_id:
        raise HTTPException(
            status_code=404, detail=f"Batch {row['batch_id']} not found"
        )


@router.post("/batch")
async def start_batch_backtest(request: Request, payload: BatchPayload):
    """批量派发：factors × markets 单元集合（服务端串行排水，进度走 /batch/status）。

    派发前把关（都进响应的 skipped 清单，绝不静默丢）：
    - 因子归属/代码空 → 跳过该因子（归属不符按 404 语义统一成「不可见或不存在」）；
    - 背压：因子名下已有活跃 running 行（单发或他批）→ 跳过其全部单元。
    """
    auth_user_id, _ = get_authenticated_identity(request)
    factor_ids = list(dict.fromkeys(payload.factor_ids))
    markets = list(
        dict.fromkeys(payload.markets or [p.market for p in list_market_profiles()])
    )
    for m in markets:
        try:
            get_market_profile(m)
        except KeyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    skipped: list[dict[str, Any]] = []
    factors: dict[str, dict] = {}
    for fid in factor_ids:
        try:
            factor = await _require_owned_factor(fid, request)
        except HTTPException:
            skipped.append(
                {"factor_id": fid, "market": None, "reason": "因子不可见或不存在"}
            )
            continue
        if not factor.get("factor_code"):
            skipped.append({"factor_id": fid, "market": None, "reason": "因子代码为空"})
            continue
        factors[fid] = factor

    if factors:
        busy = {r["factor_id"] for r in await store.running_factor_pairs(list(factors))}
        for fid in [f for f in factors if f in busy]:
            del factors[fid]
            skipped.append(
                {"factor_id": fid, "market": None, "reason": "已有进行中的回测"}
            )

    if not factors:
        return {
            "code": 200,
            "data": {
                "batch_id": None,
                "total": 0,
                "queued": 0,
                "skipped": skipped,
                "status": "empty",
                "message": "没有可派发的因子（全部跳过）",
            },
        }

    kinds: dict[str, str] = {}
    for fid, factor in factors.items():
        try:
            kinds[fid] = _detect_factor_kind(factor.get("factor_code") or "")
        except RuntimeError:
            kinds[fid] = "unknown"

    try:
        result = await batch.launch(
            factors=factors,
            kinds=kinds,
            markets=markets,
            start=payload.start,
            end=payload.end,
            cost_bps=payload.cost_bps,
            user_id=auth_user_id,
            skipped=skipped,
        )
    except Exception as exc:
        logger.error("[factor-backtest] 批量派发失败: %s", exc)
        raise HTTPException(status_code=500, detail="批量派发失败") from exc
    result["status"] = "running"
    result["message"] = (
        f"批量回测已派发: {len(factors)} 因子 × {len(markets)} 市场"
        f" = {result['total']} 单元"
    )
    return {"code": 200, "data": result}


@router.get("/batches")
async def list_backtest_batches(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
):
    """批次列表（新→旧；UI 重开页面后找回在跑/近期批次）。"""
    auth_user_id, _ = get_authenticated_identity(request)
    rows = await store.list_batches(user_id=auth_user_id, limit=limit)
    return {"code": 200, "data": {"batches": rows, "total": len(rows)}}


@router.get("/batch/status")
async def get_batch_status(batch_id: str, request: Request):
    """批次进度：单元计数/明细全部来自台账行终态回读；失败单元单列清单。

    重启可重入的**兜底入口**：DB 仍 running 但进程内无状态（重启过/创建方
    未入队成功）→ 就地重建续跑（engine 启动时的 resume_interrupted 是主入口）。
    """
    row = await store.get_batch(batch_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Batch {batch_id} not found")
    _require_owned_batch(row, request)
    if batch_id not in batch._STATES and row.get("status") == "running":
        try:
            await batch.resume_batch(batch_id)
        except Exception as exc:  # noqa: BLE001 — 恢复失败不拦查询
            logger.warning("[factor-backtest] 批次恢复失败 %s: %s", batch_id, exc)
    data = await batch.get_batch_status(batch_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"Batch {batch_id} not found")
    return {"code": 200, "data": data}


@router.post("/batch/cancel")
async def cancel_batch_backtest(request: Request, payload: BatchCancelPayload):
    """取消批次：杀当前单元子进程 + 批次行收口；未启动单元不落行。"""
    row = await store.get_batch(payload.batch_id)
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"Batch {payload.batch_id} not found"
        )
    _require_owned_batch(row, request)
    if row.get("status") != "running":
        return {
            "code": 200,
            "data": {
                "batch_id": payload.batch_id,
                "status": row["status"],
                "killed": [],
                "message": "批次已收口，无需取消",
            },
        }
    result = await batch.cancel_batch(payload.batch_id)
    fresh = await store.get_batch(payload.batch_id)
    return {
        "code": 200,
        "data": {
            "batch_id": payload.batch_id,
            "status": (fresh or row).get("status"),
            "killed": result["killed"],
            "message": f"已取消：中止 {len(result['killed'])} 个在跑单元",
        },
    }


# ── 矩阵 ─────────────────────────────────────────────────────────────


@router.post("/matrix")
async def factor_market_matrix(request: Request, payload: MatrixPayload):
    """因子 × 市场适配矩阵：每格 = 最近一次运行（任意终态）+ 静态兼容档。

    CN 列为样本内基准并排列出；未跑过的格子给出静态兼容判定（portable /
    data_unsupported / unknown），「能不能跑」与「跑出来怎样」分开呈现。
    """
    auth_user_id, _ = get_authenticated_identity(request)
    for m in payload.markets or []:
        try:
            get_market_profile(m)
        except KeyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    metas = await store.get_factor_meta(payload.factor_ids)
    by_id = {m["factor_id"]: m for m in metas}
    # 归属过滤（与 _require_owned_factor 同语义：owner 为空的历史行只读可见）
    visible = [
        fid
        for fid in payload.factor_ids
        if fid in by_id
        and (not by_id[fid].get("user_id") or by_id[fid]["user_id"] == auth_user_id)
    ]

    all_markets = [p.market for p in list_market_profiles()]
    markets = payload.markets or all_markets
    cells = await store.latest_cells(visible, markets)
    cell_map = {(c["factor_id"], c["market"]): c for c in cells}

    profiles = {p.market: p for p in list_market_profiles()}
    factors_out: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for fid in payload.factor_ids:
        meta = by_id.get(fid)
        code = (meta or {}).get("factor_code") or ""
        row: dict[str, Any] = {
            "factor_id": fid,
            "factor_name": (meta or {}).get("factor_name"),
            "found": meta is not None,
            "owned": fid in visible,
            "cn_ic": (meta or {}).get("ic_value"),
            "cells": {},
        }
        for m in markets:
            compat = classify_factor(code, columns_for_market(m))
            run = cell_map.get((fid, m)) if fid in visible else None
            row["cells"][m] = _matrix_cell(run, compat, profiles[m])
            counts[row["cells"][m]["status"]] = (
                counts.get(row["cells"][m]["status"], 0) + 1
            )
        factors_out.append(row)

    return {
        "code": 200,
        "data": {
            "markets": [
                {
                    "market": p.market,
                    "label": p.label,
                    "in_sample": p.in_sample,
                    "experimental": p.experimental,
                    "benchmark": p.benchmark,
                    "cost_bps": p.cost_bps,
                }
                for p in list_market_profiles()
                if p.market in markets
            ],
            "factors": factors_out,
            "counts": counts,
        },
    }


def _matrix_cell(run: dict[str, Any] | None, compat: dict, profile) -> dict[str, Any]:
    """单元格：未跑过 → not_run（带静态兼容档）；跑过 → 最近一次运行摘要。"""
    cell: dict[str, Any] = {
        "status": "not_run",
        "run_id": None,
        "compat": compat["status"],
        "missing": compat["missing"],
        "dynamic": compat["dynamic"],
        "error": None,
        "universe": None,
        "date_range": None,
        "finished_at": None,
        "in_sample": profile.in_sample,
        "metrics": {},
    }
    if run is None:
        return cell
    metrics = run.get("metrics") or {}
    cell.update(
        {
            "status": run["status"],
            "run_id": run["run_id"],
            "error": run.get("error"),
            "universe": run.get("universe"),
            "date_range": run.get("date_range"),
            "finished_at": (
                run["finished_at"].isoformat() if run.get("finished_at") else None
            ),
            "metrics": {k: metrics.get(k) for k in _CELL_METRIC_KEYS if k in metrics},
        }
    )
    # 排序便利键（前端排名视图直接排；缺失一律 None 显示「—」）。metrics_json
    # 是主源（新引擎全键齐备）；缺键回落台账列（旧 CN 回测只写了 icir 等专列，
    # metrics 里没有 ic/sharpe——矩阵要把新旧两代 CN 行都装进同一张表）。
    for key, col in (
        ("ic", "ic_value"),
        ("rank_ic", "rank_ic"),
        ("icir", "icir"),
        ("sharpe", "sharpe_ratio"),
        ("ann_return_net", "annual_return"),
        ("max_drawdown", "max_drawdown"),
    ):
        val = cell["metrics"].get(key)
        cell[key] = val if val is not None else run.get(col)
    return cell


# ── 台账 / 曲线 ──────────────────────────────────────────────────────


@router.get("/runs")
async def list_backtest_runs(
    factor_id: str,
    request: Request,
    market: str | None = Query(None, description="按市场筛选"),
    status: str | None = Query(None, description="按终态筛选"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """回测台账列表（新→旧，可筛可排的数据面）。"""
    await _require_owned_factor(factor_id, request)
    rows = await store.list_runs(
        factor_id=factor_id,
        market=market,
        status=status,
        limit=limit,
        offset=offset,
    )
    return {"code": 200, "data": {"runs": rows, "total": len(rows)}}


@router.get("/runs/{run_id}/series")
async def get_backtest_series(run_id: str, request: Request):
    """曲线下钻：IC 序列 / 净值 / 分位桶 / 换手（JSON 安全载荷）。"""
    run = await store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    await _require_owned_factor(run["factor_id"], request)
    series = await store.get_series(run_id)
    if series is None:
        raise HTTPException(
            status_code=404,
            detail="该运行没有曲线数据（未完成或为降级终态）",
        )
    return {"code": 200, "data": {"run": run, "series": series["series"]}}
