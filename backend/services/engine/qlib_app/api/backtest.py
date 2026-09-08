"""Qlib 回测 API 路由"""

import logging
from datetime import datetime
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request

from backend.services.engine.qlib_app.api.identity import _identity_from_request
from backend.services.engine.qlib_app.api.ai_fix import router as ai_fix_router
from backend.services.engine.qlib_app.api.export import router as export_router
from backend.services.engine.qlib_app.api.history import router as history_router
from backend.services.engine.qlib_app.api.ops import router as ops_router
from backend.services.engine.qlib_app.api.optimization import router as optimization_router
from backend.services.engine.qlib_app.api.risk import router as risk_router
from backend.services.engine.qlib_app.schemas.backtest import QlibBacktestRequest, QlibBacktestResult
from backend.shared.utils import normalize_user_id
from backend.services.engine.qlib_app.utils.structured_logger import StructuredTaskLogger

# 前端 backtestService 的 baseUrl 是 /api/v1/qlib
# 因此这里必须显式包含 prefix="/qlib"，以便匹配 /api/v1/qlib/backtest
router = APIRouter(prefix="/qlib", tags=["qlib"])

logger = logging.getLogger(__name__)
task_logger = StructuredTaskLogger(logger, "QlibBacktestAPI")

# 子路由直接挂载，路径将自动变为 /api/v1/qlib/results, /api/v1/qlib/history 等
router.include_router(ops_router)
router.include_router(history_router)
router.include_router(export_router)
router.include_router(risk_router)
router.include_router(optimization_router)
router.include_router(ai_fix_router)


def get_qlib_service() -> Any:
    """依赖注入：获取 Qlib 服务实例"""
    return get_qlib_service_cached()


_qlib_service_cache = None


def get_qlib_service_cached():
    global _qlib_service_cache
    if _qlib_service_cache is None:
        from backend.services.engine.qlib_app.services.backtest_service import QlibBacktestService

        _qlib_service_cache = QlibBacktestService()
    return _qlib_service_cache


@router.post("/backtest", response_model=QlibBacktestResult)
async def run_backtest(
    request_ctx: Request,
    request: QlibBacktestRequest,
    service: Any = Depends(get_qlib_service),
    async_mode: bool = False,
) -> QlibBacktestResult:
    """
    运行 Qlib 回测。

    async_mode=False: 同步执行并直接返回结果。
    async_mode=True: 提交 Celery 任务并返回任务信息。
    """
    try:
        auth_user_id, auth_tenant_id = _identity_from_request(
            request_ctx,
            provided_user_id=request.user_id,
            provided_tenant_id=request.tenant_id,
        )
        request.user_id = normalize_user_id(auth_user_id)
        request.tenant_id = auth_tenant_id

        backtest_id = getattr(request, "backtest_id", None) or uuid4().hex

        # minibt 脚本策略不走 qlib 引擎（主镜像没有该库），也不走 celery
        # （worker 没有 docker socket）——在持有 docker.sock 的引擎进程里
        # 直接派发 quantmind-minibt-runner 容器，结果映射回 qlib 结果形状。
        from backend.services.engine.qlib_app.services.minibt_backtest_service import (
            dispatch_minibt_backtest,
            is_minibt_request,
        )

        if is_minibt_request(request):
            return await dispatch_minibt_backtest(
                request, backtest_id=backtest_id, async_mode=async_mode
            )

        if async_mode:
            request_dict = request.dict()
            request_dict["backtest_id"] = backtest_id

            try:
                from backend.services.engine.qlib_app.services.backtest_persistence import BacktestPersistence
                from backend.services.engine.qlib_app.tasks import run_backtest_async

                task = run_backtest_async.apply_async(args=[request_dict])

                persistence = BacktestPersistence()
                await persistence.save_run(
                    backtest_id=backtest_id,
                    user_id=request.user_id,
                    tenant_id=request.tenant_id,
                    status="pending",
                    created_at=datetime.now(),
                    config=request_dict,
                    result=None,
                    task_id=task.id,
                )

                return QlibBacktestResult(
                    backtest_id=backtest_id,
                    status="pending",
                    config=request_dict,
                    task_id=task.id,
                    annual_return=0.0,
                    sharpe_ratio=0.0,
                    max_drawdown=0.0,
                    alpha=0.0,
                )
            except Exception as celery_err:
                task_logger.error(
                    "async_enqueue_failed",
                    "异步回测入队失败（仅支持 Celery）",
                    backtest_id=backtest_id,
                    error=str(celery_err),
                )
                raise HTTPException(
                    status_code=503,
                    detail="异步回测服务不可用，请检查 Celery Worker",
                )

        return await service.run_backtest(request)
    except HTTPException:
        raise
    except Exception as e:
        task_logger.exception("backtest_failed", "回测执行失败", error=str(e))
        raise HTTPException(status_code=500, detail=f"回测执行失败: {str(e)}")
