"""arena 数据面 · 族 2：行情实验室（K线/策略库/批量产物 + Pine 闭环，全量面）。

移植自隔壁 api_server.py L2091-2429 同族 handler，响应形状逐字段一致
（`arena/api/client.ts` 的类型定义即契约）。与上游的两处**有意偏离**：

1. 全部端点加 `Depends(get_current_user)`（上游 baymax 无鉴权；QM 统一口径）。
2. **写端点的执行面在宿主**（2026-10-08 B2 迁移落地后全量开挂）：`POST
   library/{id}/backtest`（转写+沙箱回测）与 `POST library/{id}/chat`（改策略
   候选）在容器里只往 `data/pine_*/queue/` 落请求文件——容器没有 bwrap、也不
   执行模型产物；真正的转写/沙箱回测/对话由宿主 cron 跑
   `scripts/pine_transpile_worker.py` / `scripts/pine_chat_worker.py` 消费队列，
   界面轮询 `library/{id}/job` 与 `chat/{job}` 读状态。宿主 worker 未接线时
   队列无人消费，按钮表现为「一直排队中」——不产生假活（前端 A6 已有文案）。
   `POST /market-lab/backtest`（模板回测）仍走容器内 pynecore：未装时如实报
   「运行时未安装」（上游同款降级语义）。

错误路径语义照抄上游：入参类（ValueError / LibraryError / FileNotFoundError）
→ 400 或 404（按上游逐端点定）；其余异常 → 200 + `{success: false, error}`。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from backend.services.api.user_app.middleware.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()


def _lab():
    from backend.services.agent_arena import market_lab as ml

    return ml


def _lab_batch():
    from backend.services.agent_arena import lab_batch as lb

    return lb


def _pine_lib():
    from backend.services.agent_arena import pine_library as pl

    return pl


# ---------- K线 / 标的 / 策略 ----------

@router.get("/market-lab/symbols")
def lab_symbols(
    q: str = "",
    limit: int = Query(30, ge=1, le=200),
    _current_user: dict = Depends(get_current_user),
):
    try:
        return {"success": True, "data": _lab().search_symbols(q, limit)}
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": f"标的搜索失败: {e}"}


@router.get("/market-lab/klines")
def lab_klines(
    symbol: str,
    adj: str = "unadjusted",
    limit: int = Query(600, ge=60, le=3000),
    start: str = "",
    end: str = "",
    _current_user: dict = Depends(get_current_user),
):
    """日线（adj=unadjusted|forward|backward）。看盘默认不复权，回测默认后复权。"""
    try:
        ml = _lab()
        bars = ml.load_klines(symbol, adj=adj, limit=limit, start=start, end=end)
        return {"success": True, "data": {"symbol": ml.normalize_code(symbol),
                                          "name": ml.stock_name(symbol), "adj": adj,
                                          "count": len(bars), "bars": bars}}
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": f"K线查询失败: {e}"}


@router.get("/market-lab/strategies")
def lab_strategies(_current_user: dict = Depends(get_current_user)):
    try:
        return {"success": True, "data": _lab().list_strategies()}
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": f"策略列表失败: {e}"}


@router.post("/market-lab/backtest")
def lab_backtest(
    payload: dict = Body(...),
    _current_user: dict = Depends(get_current_user),
):
    """{strategy, symbol, adj?, start?, end?, params?} → 统计/逐笔/净值。"""
    try:
        ml = _lab()
        data = ml.run_backtest(
            strategy_id=str(payload.get("strategy") or ""),
            symbol=str(payload.get("symbol") or ""),
            adj=str(payload.get("adj") or "backward"),
            start=str(payload.get("start") or ""),
            end=str(payload.get("end") or ""),
            params=payload.get("params") or {},
        )
        return {"success": True, "data": data}
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        logger.error("market-lab backtest failed: %s", e, exc_info=True)
        return {"success": False, "error": f"回测失败: {e}"}


# ---------- 批量回测产物（只读；产物由 scripts/lab_batch_backtest.py 生成） ----------
# 注意声明顺序：/batch/runs、/batch/pools 必须在 /batch/{run_id} 之前，
# 否则 "runs" 会被当成 run_id 吞掉。

@router.get("/market-lab/batch/runs")
def lab_batch_runs(
    limit: int = Query(20, ge=1, le=100),
    _current_user: dict = Depends(get_current_user),
):
    """批次列表（时间倒序，只含摘要）。"""
    try:
        return {"success": True, "data": _lab_batch().list_runs(limit=limit)}
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": f"批次列表失败: {e}"}


@router.get("/market-lab/batch/pools")
def lab_batch_pools(_current_user: dict = Depends(get_current_user)):
    """可选股票池（指数成分 + 全市场），附当前成员数。"""
    try:
        lb = _lab_batch()
        data = []
        for key, (_, label) in lb.INDEX_POOLS.items():
            try:
                n = len(lb.resolve_pool(key))
            except Exception:  # noqa: BLE001 缺文件不影响其它池
                n = 0
            data.append({"id": key, "label": label, "count": n})
        return {"success": True, "data": data}
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": f"股票池列表失败: {e}"}


@router.get("/market-lab/batch/{run_id}")
def lab_batch_run(
    run_id: str,
    strategy: str = "",
    sort: str = "net",
    limit: int = Query(200, ge=1, le=5000),
    _current_user: dict = Depends(get_current_user),
):
    """单批次结果：ranking=策略排行；strategy 非空时返回该策略的选股明细（倒序前 N）。"""
    try:
        lb = _lab_batch()
        d = lb.load_run(run_id)
        rows = d.get("rows", [])
        if strategy:
            rows = [r for r in rows if r["strategy"] == strategy]
            key = {"net": "net_pct", "dd": "dd_pct", "sharpe": "sharpe",
                   "profit_factor": "profit_factor", "trades": "trades"}.get(sort, "net_pct")
            rows = sorted(rows, key=lambda r: (r.get(key) is not None, r.get(key) or -1e9),
                          reverse=True)
        return {"success": True, "data": {
            "run_id": d.get("run_id"), "created": d.get("created"),
            "pool": d.get("pool"), "pool_label": d.get("pool_label"),
            "adj": d.get("adj"), "strategies": d.get("strategies"),
            "universe": d.get("universe"), "counts": d.get("counts"),
            "elapsed_sec": d.get("elapsed_sec"),
            "ranking": d.get("ranking", []),
            "rows": rows[:limit],
            "skipped": d.get("skipped", [])[:50],
            "errors": d.get("errors", [])[:50],
        }}
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        logger.error("lab batch load failed: %s", e, exc_info=True)
        return {"success": False, "error": f"批次读取失败: {e}"}


# ---------- Pine 策略库（只读面；编辑/对话/回滚见模块 docstring 的挂载说明） ----------

@router.get("/market-lab/library")
def pine_library_list(
    category: str = "",
    q: str = "",
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    _current_user: dict = Depends(get_current_user),
):
    """分类 + 关键词过滤的策略列表（不含源码）。"""
    try:
        return {"success": True, "data": _pine_lib().list_items(category, q, limit, offset)}
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": f"策略库读取失败: {e}"}


@router.get("/market-lab/notes")
def pine_notes_index(_current_user: dict = Depends(get_current_user)):
    """有备注的策略 id 集合（左栏角标用）。"""
    try:
        return {"success": True, "data": _pine_lib().list_notes()}
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": f"备注索引读取失败: {e}"}


@router.get("/market-lab/transpile")
def pine_transpile_list(_current_user: dict = Depends(get_current_user)):
    """AI 转写产物一览（宿主上跑 scripts/pine_to_pyne.py 生成，页面只读）。"""
    try:
        return {"success": True, "data": _pine_lib().list_transpile()}
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": f"转写产物读取失败: {e}"}


@router.post("/market-lab/library/{item_id}/backtest")
def pine_library_backtest(
    item_id: str,
    payload: dict = Body(default={}),
    _current_user: dict = Depends(get_current_user),
):
    """把「转写 + 沙箱回测」排进队列（宿主 worker 消费，见 scripts/pine_transpile_worker.py）。

    这里只写请求文件：容器里没有 bwrap，也不该执行模型产出的代码。
    """
    try:
        return {"success": True, "data": _pine_lib().enqueue_backtest(
            item_id, payload.get("symbol", ""), payload.get("adj", "backward"),
            payload.get("model", ""), bool(payload.get("force")))}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        logger.error("pine library backtest enqueue failed: %s", e, exc_info=True)
        return {"success": False, "error": f"入队失败: {e}"}


@router.get("/market-lab/library/{item_id}")
def pine_library_get(item_id: str, _current_user: dict = Depends(get_current_user)):
    """单条策略：元数据 + 源码全文。"""
    try:
        return {"success": True, "data": _pine_lib().get_source(item_id)}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        logger.error("pine library get failed: %s", e, exc_info=True)
        return {"success": False, "error": f"策略读取失败: {e}"}


@router.put("/market-lab/library/{item_id}")
def pine_library_save(
    item_id: str,
    payload: dict = Body(...),
    _current_user: dict = Depends(get_current_user),
):
    """保存编辑（写 data/pine_library/edited/，不动桌面原语料）。"""
    try:
        return {"success": True,
                "data": _pine_lib().save_source(item_id, payload.get("source", ""))}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        logger.error("pine library save failed: %s", e, exc_info=True)
        return {"success": False, "error": f"保存失败: {e}"}


@router.delete("/market-lab/library/{item_id}")
def pine_library_reset(item_id: str, _current_user: dict = Depends(get_current_user)):
    """丢弃编辑，回到索引构建时的版本。"""
    try:
        return {"success": True, "data": _pine_lib().reset_source(item_id)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": f"重置失败: {e}"}


@router.get("/market-lab/library/{item_id}/note")
def pine_library_note_get(item_id: str, _current_user: dict = Depends(get_current_user)):
    """单条策略的备注（没写过 → 空结构）。"""
    try:
        return {"success": True, "data": _pine_lib().get_note(item_id)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        logger.error("pine note get failed: %s", e, exc_info=True)
        return {"success": False, "error": f"备注读取失败: {e}"}


@router.put("/market-lab/library/{item_id}/note")
def pine_library_note_save(
    item_id: str,
    payload: dict = Body(...),
    _current_user: dict = Depends(get_current_user),
):
    """写备注（data/pine_library/notes/<id>.json；索引重建不会丢）。"""
    try:
        return {"success": True, "data": _pine_lib().save_note(item_id, payload)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        logger.error("pine note save failed: %s", e, exc_info=True)
        return {"success": False, "error": f"备注保存失败: {e}"}


@router.post("/market-lab/library/{item_id}/chat")
def pine_library_chat_send(
    item_id: str,
    payload: dict = Body(...),
    _current_user: dict = Depends(get_current_user),
):
    """投递一条对话消息（宿主 worker 消费，见 scripts/pine_chat_worker.py）。

    这里只写请求文件：模型调用与候选回测都在宿主上做，容器不执行模型产物。
    """
    try:
        return {"success": True, "data": _pine_lib().enqueue_chat(
            item_id, payload.get("message", ""), payload.get("mode", "ask"))}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        logger.error("pine chat enqueue failed: %s", e, exc_info=True)
        return {"success": False, "error": f"投递失败: {e}"}


@router.get("/market-lab/library/{item_id}/chat")
def pine_library_chat_thread(
    item_id: str,
    limit: int = Query(100, ge=1, le=500),
    _current_user: dict = Depends(get_current_user),
):
    """该策略的对话历史（给界面回放）。"""
    try:
        return {"success": True, "data": _pine_lib().chat_thread(item_id, limit)}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        logger.error("pine chat thread failed: %s", e, exc_info=True)
        return {"success": False, "error": f"对话历史读取失败: {e}"}


@router.get("/market-lab/library/{item_id}/chat/{job_id}")
def pine_library_chat_job(
    item_id: str,
    job_id: str,
    _current_user: dict = Depends(get_current_user),
):
    """轮询单条消息的处理状态 / 回复。"""
    try:
        return {"success": True, "data": _pine_lib().chat_job(item_id, job_id)}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        logger.error("pine chat job failed: %s", e, exc_info=True)
        return {"success": False, "error": f"对话任务读取失败: {e}"}


@router.post("/market-lab/library/{item_id}/chat/{job_id}/apply")
def pine_library_chat_apply(
    item_id: str,
    job_id: str,
    payload: dict = Body(default={}),
    _current_user: dict = Depends(get_current_user),
):
    """采用对话产出的候选：快照当前源码 → 写 edited/<id>.pine → 入队重跑。

    候选是模型产物，只有用户显式点「采用」才会进正式源码
    （`scripts/pine_chat_worker.py` 只写 chat/）。
    """
    try:
        return {"success": True, "data": _pine_lib().apply_candidate(
            item_id, job_id, payload.get("symbol", ""), payload.get("adj", "backward"))}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        logger.error("pine candidate apply failed: %s", e, exc_info=True)
        return {"success": False, "error": f"采用失败: {e}"}


@router.get("/market-lab/library/{item_id}/versions")
def pine_library_versions(item_id: str, _current_user: dict = Depends(get_current_user)):
    """可回滚的版本快照（每次「采用」前存一版）。"""
    try:
        return {"success": True, "data": _pine_lib().list_versions(item_id)}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        logger.error("pine versions failed: %s", e, exc_info=True)
        return {"success": False, "error": f"版本读取失败: {e}"}


@router.post("/market-lab/library/{item_id}/revert")
def pine_library_revert(
    item_id: str,
    payload: dict = Body(default={}),
    _current_user: dict = Depends(get_current_user),
):
    """回滚到上一版（默认最新快照）；回滚后同样自动重跑官方产物。"""
    try:
        return {"success": True, "data": _pine_lib().revert_source(
            item_id, payload.get("version", ""))}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        logger.error("pine revert failed: %s", e, exc_info=True)
        return {"success": False, "error": f"回滚失败: {e}"}


@router.get("/market-lab/library/{item_id}/job")
def pine_library_job(item_id: str, _current_user: dict = Depends(get_current_user)):
    """该策略的转写/回测任务状态 + 回测报告（净值、逐笔）。没跑过 → report 为 null。"""
    try:
        pl = _pine_lib()
        job = pl.job_status(item_id)
        # 报告文件不存在时服务层给的是 {}——空对象在 JS 里是 truthy，界面会当成
        # 「有报告」去读 stats 然后炸掉。这里统一收敛成 null（上游同款注释）。
        report = pl.read_report(item_id) or None
        return {"success": True, "data": {"job": job, "report": report}}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        logger.error("pine library job failed: %s", e, exc_info=True)
        return {"success": False, "error": f"任务状态读取失败: {e}"}
