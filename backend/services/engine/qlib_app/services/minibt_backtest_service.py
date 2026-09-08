# -*- coding: utf-8 -*-
"""回测中心 minibt 策略派发：runner 容器执行 + 结果映射为 qlib 回测结果。

背景
----
minibt 脚本框架只装在 ``quantmind-minibt-runner`` 镜像里（qlib 引擎主镜像没有
该库），而 **celery worker 没有挂 docker socket** —— 常规 ``run_backtest_async``
链路既跑不了 minibt，也起不了容器。所以这类策略在 **持有 docker.sock 的引擎
进程** 里直接派发 runner 容器，再把 ``result.json`` 映射成
``QlibBacktestResult``，让回测中心 / 回测历史与 qlib 策略同构。

执行模型（与 AI-IDE minibt 运行时一致，见 routers/ai_ide/executor.py）：
- 镜像 ``quantmind-minibt-runner:latest``，命令 ``python -u /app/strategy.py``
  （minibt 模板尾部自带 ``if __name__ == "__main__"`` 块）
- 挂载 backend(ro) / data(ro) / strategy.py(ro) / result-dir(rw)
- 环境 ``PYTHONPATH=/app``、``QM_QUANTDB_DATA_DIR=/data/quantdb``、
  ``QM_MINIBT_RESULT_DIR=/app/result``、``QM_MINIBT_INCLUDE_TRADES=1``

口径换算（对齐 risk_analyzer 的 qlib 契约）
- ``max_drawdown``：minibt 给正值，qlib 口径为负值 → 取负
- ``drawdown_curve``：qlib 键名 ``drawdown``（负值），同时补 ``value`` 兼容旧图表
- ``equity_curve``：``{date: YYYY-MM-DD, value: 总资产(元)}``
- ``trades[].action``：必须是两态 ``buy``/``sell``（前端按 ``action === 'buy'`` 判方向）
- ``execution_time``：必须是数字（前端直接 ``.toFixed(2)``）
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import shutil
from datetime import datetime
from typing import Any
from uuid import uuid4

from backend.services.engine.qlib_app.schemas.backtest import (
    QlibBacktestRequest,
    QlibBacktestResult,
    QlibPortfolioMetrics,
    infer_market_from_request,
)
from backend.services.engine.qlib_app.utils.structured_logger import StructuredTaskLogger
from backend.shared.minibt_detect import detect_minibt

logger = logging.getLogger(__name__)
task_logger = StructuredTaskLogger(logger, "MinibtBacktest")

MINIBT_RUNNER_IMAGE = os.getenv(
    "AI_IDE_MINIBT_RUNNER_IMAGE", "quantmind-minibt-runner:latest"
)
DOCKER_NETWORK = os.getenv("AI_IDE_DOCKER_NETWORK", "quantmind_quantmind-net")
HOST_PROJECT_PATH = os.getenv("HOST_PROJECT_PATH", "/home/quantmind")
# 与 AI-IDE 临时目录同级（都在 /app/db 下，才能换算成宿主机挂载路径）
_WORK_ROOT = os.getenv("MINIBT_BACKTEST_WORK_DIR", "/app/db/minibt_backtest")
_DOCKER_TIMEOUT = int(os.getenv("AI_IDE_DOCKER_TIMEOUT", "180"))
_RUN_TIMEOUT_SEC = int(os.getenv("MINIBT_BACKTEST_TIMEOUT", "1800"))
_RESULT_FILENAME = "result.json"
_LOG_TAIL_LINES = 40

# 回测中心日期区间 → minibt 模板顶层常量。只认行首、单/双引号包裹的 8 位日期：
# 缩进的类属性、START_DATE 这类同前缀变量都不动。
_TOP_LEVEL_DATE_ASSIGN = {
    "start": re.compile(r"^(START)(\s*=\s*)(['\"])(\d{8})\3", re.MULTILINE),
    "end": re.compile(r"^(END)(\s*=\s*)(['\"])(\d{8})\3", re.MULTILINE),
}
_DATE_PATTERN = re.compile(r"^(\d{4})-?(\d{2})-?(\d{2})$")


# ---------------------------------------------------------------------------
# 纯逻辑：日期覆盖 + 结果映射（可单测，不依赖 docker）
# ---------------------------------------------------------------------------
def _to_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _normalize_compact_date(value: Any) -> str | None:
    """``YYYY-MM-DD`` / ``YYYYMMDD`` → ``YYYYMMDD``；非法日期返回 None。"""
    match = _DATE_PATTERN.match(str(value or "").strip())
    if not match:
        return None
    compact = "".join(match.groups())
    try:
        datetime.strptime(compact, "%Y%m%d")
    except ValueError:
        return None
    return compact


def apply_minibt_date_overrides(
    code: str, start_date: Any, end_date: Any
) -> tuple[str, dict[str, bool]]:
    """把回测中心的日期区间写进 minibt 脚本的顶层 ``START``/``END`` 常量。

    minibt 模板用模块级常量控制数据区间，回测中心传的 start_date/end_date
    默认不会生效；这里就地替换，返回 (新代码, 各字段是否命中)。

    只替换 **行首**、引号成对、值为 8 位日期的顶层赋值。缩进赋值（类属性）、
    ``START_DATE`` 这类同前缀变量、非日期字面量都不动。
    """
    if not code:
        return code, {"start": False, "end": False}

    requested = {
        "start": _normalize_compact_date(start_date),
        "end": _normalize_compact_date(end_date),
    }
    applied = {"start": False, "end": False}
    updated = code
    for key, pattern in _TOP_LEVEL_DATE_ASSIGN.items():
        compact = requested[key]
        if not compact:
            continue

        def _replace(match: re.Match[str], _compact: str = compact) -> str:
            applied[key] = True
            return (
                f"{match.group(1)}{match.group(2)}"
                f"{match.group(3)}{_compact}{match.group(3)}"
            )

        updated = pattern.sub(_replace, updated, count=1)
    return updated, applied


def _build_drawdown_curve(
    equity_curve: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """按 qlib 口径生成回撤曲线：键名 ``drawdown``（负值），另补 ``value`` 兼容旧图表。"""
    peak: float | None = None
    curve: list[dict[str, Any]] = []
    for point in equity_curve:
        value = _to_float(point.get("value")) or 0.0
        peak = value if peak is None else max(peak, value)
        drawdown = round((value / peak - 1.0) if peak else 0.0, 6)
        curve.append(
            {"date": point.get("date"), "drawdown": drawdown, "value": drawdown}
        )
    return curve


def _map_trades(raw_trades: Any) -> list[dict[str, Any]]:
    """minibt 订单流水 → qlib 交易明细形状。

    minibt 侧（``QM_MINIBT_INCLUDE_TRADES=1`` 时由 backend/shared/minibt_result.py
    输出）字段为 ``date/symbol/direction/price/qty/detail.fee``；这里归一成前端
    交易表消费的 ``action('buy'|'sell')/quantity/totalAmount/commission``。

    ``factor`` 固定 1.0：minibt 用的是前复权行情，没有独立复权因子；这样也能
    避免持久化层再去 qlib 数据里查因子（``normalize_trades_for_display``）。
    """
    trades: list[dict[str, Any]] = []
    for item in raw_trades or []:
        if not isinstance(item, dict):
            continue
        raw_action = str(item.get("action") or item.get("direction") or "").lower()
        if raw_action.startswith("buy"):
            action = "buy"
        elif raw_action.startswith("sell"):
            action = "sell"
        else:
            continue
        price = _to_float(item.get("price")) or 0.0
        quantity = _to_float(item.get("quantity", item.get("qty"))) or 0.0
        detail = item.get("detail") if isinstance(item.get("detail"), dict) else {}
        total_amount = _to_float(
            item.get("totalAmount", item.get("total_amount", detail.get("value")))
        )
        if total_amount is None:
            total_amount = price * quantity
        trades.append(
            {
                "date": str(item.get("date") or "")[:10],
                "symbol": str(item.get("symbol") or ""),
                "action": action,
                "price": round(price, 4),
                "quantity": int(quantity) if float(quantity).is_integer() else quantity,
                "totalAmount": round(total_amount, 2),
                "commission": round(_to_float(detail.get("fee")) or 0.0, 4),
                "adj_price": round(price, 4),
                "adj_quantity": quantity,
                "factor": 1.0,
            }
        )
    return trades


def _build_config_payload(
    request: QlibBacktestRequest,
    payload: dict[str, Any],
    date_overrides: dict[str, bool] | None,
) -> dict[str, Any]:
    """复用 qlib 的配置快照结构，再补 minibt 专有字段（前端按同一套字段读）。"""
    try:
        from backend.services.engine.qlib_app.services.risk_analyzer import RiskAnalyzer

        config = dict(RiskAnalyzer._build_config_payload(request))
    except Exception as exc:  # noqa: BLE001 - 快照失败不能拖垮回测结果
        task_logger.warning("config_payload_failed", "构建配置快照失败", error=str(exc))
        config = {
            "start_date": request.start_date,
            "end_date": request.end_date,
            "initial_capital": request.initial_capital,
            "qlib_strategy_type": request.strategy_type,
        }
    engine_config = payload.get("config") or {}
    config.update(
        {
            "engine": "minibt",
            "market": engine_config.get("market")
            or infer_market_from_request(request.qlib_provider_uri, request.qlib_region),
            "date_overrides": date_overrides or {},
            "warnings": list(payload.get("warnings") or []),
        }
    )
    return config


def map_minibt_payload_to_result(
    payload: dict[str, Any] | None,
    *,
    request: QlibBacktestRequest,
    backtest_id: str,
    error_message: str | None = None,
    date_overrides: dict[str, bool] | None = None,
) -> QlibBacktestResult:
    """minibt ``result.json`` → ``QlibBacktestResult``（回测中心/回测历史同构）。"""
    payload = payload or {}
    config = _build_config_payload(request, payload, date_overrides)
    now = datetime.now()

    runner_status = str(payload.get("status") or "").lower()
    if error_message or runner_status != "success":
        return QlibBacktestResult(
            backtest_id=backtest_id,
            user_id=request.user_id,
            tenant_id=request.tenant_id,
            status="failed",
            created_at=now,
            completed_at=now,
            config=config,
            annual_return=0.0,
            sharpe_ratio=0.0,
            max_drawdown=0.0,
            alpha=0.0,
            error_message=error_message
            or str(payload.get("error") or "minibt 回测未产出结果"),
            full_error=payload.get("error_traceback") or None,
            execution_time=_to_float(payload.get("elapsed_sec")) or 0.0,
        )

    metrics = payload.get("metrics") or {}
    extra = payload.get("extra") or {}

    equity_curve: list[dict[str, Any]] = []
    for point in payload.get("equity") or []:
        if not isinstance(point, dict):
            continue
        value = _to_float(point.get("value"))
        if value is None:
            continue
        equity_curve.append({"date": str(point.get("date") or "")[:10], "value": value})

    trades = _map_trades(payload.get("trades"))
    max_drawdown = _to_float(metrics.get("max_drawdown"))
    final_equity = _to_float(extra.get("final_equity"))
    round_trips = int(metrics.get("n_trades") or 0)

    return QlibBacktestResult(
        backtest_id=backtest_id,
        user_id=request.user_id,
        tenant_id=request.tenant_id,
        status="completed",
        created_at=now,
        completed_at=now,
        config=config,
        annual_return=_to_float(metrics.get("annual_return")) or 0.0,
        sharpe_ratio=_to_float(metrics.get("sharpe")) or 0.0,
        # minibt 给正值，qlib 口径为负值（-0.10 好于 -0.15）
        max_drawdown=-abs(max_drawdown) if max_drawdown is not None else 0.0,
        total_return=_to_float(metrics.get("cum_return")),
        # 与 minibt [RESULT] 行的 n_trades 同口径（开仓回合数），跨界面一致
        total_trades=round_trips,
        win_rate=_to_float(metrics.get("win_rate")),
        profit_factor=_to_float(extra.get("profit_factor")),
        portfolio_metrics=(
            QlibPortfolioMetrics(final_value=final_equity, account=final_equity)
            if final_equity is not None
            else None
        ),
        equity_curve=equity_curve,
        drawdown_curve=_build_drawdown_curve(equity_curve),
        trades=trades,
        positions=list(payload.get("positions") or []),
        # minibt 是"当根K线收盘价成交"的撮合口径（无 T+1/涨跌停/整手约束），
        # 这里如实标注成交价类型，但不设 signal_lag_days=0——那是未来信息泄漏，
        # minibt 用的是当根收盘价决策+成交，属于口径乐观而非偷看未来。
        deal_price="close",
        advanced_stats={
            "engine": "minibt",
            # mtm=逐日盯市重建 / minibt_balance=引擎原生持仓成本口径
            "equity_caliber": extra.get("equity_caliber"),
            "avg_position": _to_float(metrics.get("avg_position")),
            "total_fee": _to_float(extra.get("total_fee")),
            "initial_capital": _to_float(request.initial_capital),
            "round_trips": round_trips,
            "trade_records": len(trades),
            "warnings": list(payload.get("warnings") or []),
        },
        execution_time=_to_float(payload.get("elapsed_sec")) or 0.0,
    )


# ---------------------------------------------------------------------------
# 派发入口
# ---------------------------------------------------------------------------
def is_minibt_request(request: QlibBacktestRequest) -> bool:
    """回测中心请求是否属于 minibt 策略（代码 import 或 minibt_* 模板类型）。"""
    if detect_minibt(request.strategy_content):
        return True
    return str(request.strategy_type or "").lower().startswith("minibt_")


def _resolve_strategy_code(request: QlibBacktestRequest) -> str:
    """取可执行代码：优先请求体，其次按 ``minibt_*`` 模板 ID 回查模板库。"""
    code = (request.strategy_content or "").strip()
    if code:
        return request.strategy_content or ""
    strategy_type = str(request.strategy_type or "").strip()
    if not strategy_type:
        return ""
    try:
        from backend.services.engine.qlib_app.services.strategy_templates import (
            get_template_by_id,
        )

        template = get_template_by_id(strategy_type)
    except Exception as exc:  # noqa: BLE001
        task_logger.warning("template_lookup_failed", "模板回查失败", error=str(exc))
        return ""
    return (getattr(template, "code", "") or "") if template else ""


async def _persist_failure(
    persistence: Any,
    request: QlibBacktestRequest,
    *,
    backtest_id: str,
    request_dict: dict[str, Any],
    message: str,
) -> QlibBacktestResult:
    """未进容器就失败（缺代码 / 市场不支持）时，直接落一条 failed 记录。"""
    result = map_minibt_payload_to_result(
        None, request=request, backtest_id=backtest_id, error_message=message
    )
    now = datetime.now()
    try:
        await persistence.save_run(
            backtest_id=backtest_id,
            user_id=request.user_id,
            tenant_id=request.tenant_id,
            status=result.status,
            created_at=now,
            completed_at=now,
            config=request_dict,
            result=result,
        )
    except Exception as exc:  # noqa: BLE001
        task_logger.error(
            "persist_failed",
            "minibt 失败记录落库失败",
            backtest_id=backtest_id,
            error=str(exc),
        )
    return result


async def _run_runner_container(
    code: str, backtest_id: str, initial_capital: float | None = None
) -> tuple[dict[str, Any] | None, str | None]:
    """在 ``quantmind-minibt-runner`` 里跑策略脚本，返回 (result.json, 错误信息)。"""
    import docker

    work_dir = os.path.join(_WORK_ROOT, backtest_id)
    result_dir = os.path.join(work_dir, "result")
    script_path = os.path.join(work_dir, "strategy.py")
    os.makedirs(result_dir, exist_ok=True)
    with open(script_path, "w", encoding="utf-8") as handle:
        handle.write(code)

    host_work_dir = os.path.join(
        HOST_PROJECT_PATH, os.path.relpath(work_dir, "/app")
    )
    volumes = {
        os.path.join(host_work_dir, "strategy.py"): {
            "bind": "/app/strategy.py",
            "mode": "ro",
        },
        os.path.join(host_work_dir, "result"): {
            "bind": os.getenv("QM_MINIBT_RESULT_DIR", "/app/result"),
            "mode": "rw",
        },
        os.path.join(HOST_PROJECT_PATH, "backend"): {
            "bind": "/app/backend",
            "mode": "ro",
        },
        os.path.join(HOST_PROJECT_PATH, "data"): {"bind": "/data", "mode": "ro"},
    }
    environment = {
        "PYTHONPATH": "/app",
        "PYTHONUNBUFFERED": "1",
        "QM_QUANTDB_DATA_DIR": os.getenv("QM_QUANTDB_DATA_DIR", "/data/quantdb"),
        "QM_MINIBT_RESULT_DIR": os.getenv("QM_MINIBT_RESULT_DIR", "/app/result"),
        # 回测中心要逐笔明细；AI-IDE 默认口径不变（不设该变量即为空列表）
        "QM_MINIBT_INCLUDE_TRADES": "1",
    }
    if initial_capital and initial_capital > 0:
        # minibt 默认初始资金 100 万；回测中心的"初始资金"字段以请求为准
        environment["QM_MINIBT_INITIAL_CAPITAL"] = str(float(initial_capital))

    client = await asyncio.to_thread(docker.from_env, timeout=_DOCKER_TIMEOUT)
    container = None
    try:
        container = await asyncio.to_thread(
            client.containers.run,
            MINIBT_RUNNER_IMAGE,
            command=["python", "-u", "/app/strategy.py"],
            name=f"qm-minibt-bt-{backtest_id[:24]}",
            detach=True,
            volumes=volumes,
            network=DOCKER_NETWORK,
            environment=environment,
            mem_limit="16g",
            cpu_quota=100000,
        )
        try:
            wait_result = await asyncio.wait_for(
                asyncio.to_thread(container.wait), timeout=_RUN_TIMEOUT_SEC
            )
            exit_code = int(wait_result.get("StatusCode", -1))
        except asyncio.TimeoutError:
            return None, f"minibt 回测超时（>{_RUN_TIMEOUT_SEC}s），已终止容器"
        logs = (await asyncio.to_thread(container.logs, tail=_LOG_TAIL_LINES)).decode(
            "utf-8", errors="replace"
        )
    finally:
        if container is not None:
            try:
                await asyncio.to_thread(container.remove, force=True)
            except Exception:  # noqa: BLE001 - 清理失败不影响结果
                pass
        try:
            await asyncio.to_thread(client.close)
        except Exception:  # noqa: BLE001
            pass

    result_path = os.path.join(result_dir, _RESULT_FILENAME)
    if os.path.isfile(result_path):
        try:
            with open(result_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload, dict) and payload:
                return payload, None
        except (OSError, ValueError) as exc:
            return None, f"minibt result.json 解析失败: {exc}"

    tail = "\n".join(logs.strip().splitlines()[-_LOG_TAIL_LINES:])
    return None, f"minibt runner 无结果输出（ExitCode={exit_code}）\n{tail}"


async def _execute_and_persist(
    request: QlibBacktestRequest,
    *,
    backtest_id: str,
    request_dict: dict[str, Any],
    code: str,
    date_overrides: dict[str, bool],
    persistence: Any,
) -> QlibBacktestResult:
    """跑容器 → 映射 → 落库；任何异常都收敛成 failed 结果，不留 pending 悬挂。"""
    created_at = datetime.now()

    async def _save(status: str, result: QlibBacktestResult | None) -> None:
        try:
            await persistence.save_run(
                backtest_id=backtest_id,
                user_id=request.user_id,
                tenant_id=request.tenant_id,
                status=status,
                created_at=created_at,
                completed_at=(
                    datetime.now() if status in ("completed", "failed") else None
                ),
                config=request_dict,
                result=result,
            )
        except Exception as exc:  # noqa: BLE001
            task_logger.error(
                "persist_failed",
                "minibt 回测结果落库失败",
                backtest_id=backtest_id,
                error=str(exc),
            )

    await _save("running", None)
    try:
        payload, run_error = await _run_runner_container(
            code, backtest_id, request.initial_capital
        )
        result = map_minibt_payload_to_result(
            payload,
            request=request,
            backtest_id=backtest_id,
            error_message=run_error,
            date_overrides=date_overrides,
        )
    except Exception as exc:  # noqa: BLE001 - 派发链路自身异常也要落 failed
        task_logger.exception(
            "dispatch_failed",
            "minibt 回测派发失败",
            backtest_id=backtest_id,
            error=str(exc),
        )
        result = map_minibt_payload_to_result(
            None,
            request=request,
            backtest_id=backtest_id,
            error_message=f"minibt 回测派发失败: {exc}",
        )

    await _save(result.status, result)

    if result.status == "completed":
        try:
            from backend.shared.notification_publisher import publish_notification_async

            await publish_notification_async(
                user_id=str(request.user_id),
                tenant_id=str(request.tenant_id or "default"),
                title="回测已完成",
                content=(
                    f"{request.strategy_type} 回测完成，年化 {result.annual_return:.2%}，"
                    f"最大回撤 {result.max_drawdown:.2%}"
                ),
                type="strategy",
                level="success",
                action_url="/backtest",
            )
        except Exception as exc:  # noqa: BLE001 - 通知失败不影响回测
            task_logger.warning("notify_failed", "回测完成通知发送失败", error=str(exc))
    return result


async def dispatch_minibt_backtest(
    request: QlibBacktestRequest,
    *,
    backtest_id: str,
    async_mode: bool,
) -> QlibBacktestResult:
    """回测中心 minibt 入口：先落 pending 行，再（后台）跑容器并回填结果。

    先落库是为了让前端轮询 ``/backtest/{id}/status`` 立刻可见，避免 not_found。
    """
    from backend.services.engine.qlib_app.services.backtest_persistence import (
        BacktestPersistence,
    )

    request_dict = request.dict()
    request_dict["backtest_id"] = backtest_id
    persistence = BacktestPersistence()

    market = infer_market_from_request(request.qlib_provider_uri, request.qlib_region)
    if market != "CN":
        return await _persist_failure(
            persistence,
            request,
            backtest_id=backtest_id,
            request_dict=request_dict,
            message=(
                f"minibt 运行时只支持 A 股数据（当前市场: {market}），"
                "请在 A 股市场下回测"
            ),
        )

    code = _resolve_strategy_code(request)
    if not code.strip():
        return await _persist_failure(
            persistence,
            request,
            backtest_id=backtest_id,
            request_dict=request_dict,
            message="minibt 策略缺少可执行代码（strategy_content 为空）",
        )

    code, date_overrides = apply_minibt_date_overrides(
        code, request.start_date, request.end_date
    )

    await persistence.save_run(
        backtest_id=backtest_id,
        user_id=request.user_id,
        tenant_id=request.tenant_id,
        status="pending",
        created_at=datetime.now(),
        config=request_dict,
        result=None,
    )

    coro = _execute_and_persist(
        request,
        backtest_id=backtest_id,
        request_dict=request_dict,
        code=code,
        date_overrides=date_overrides,
        persistence=persistence,
    )
    if not async_mode:
        return await coro

    asyncio.create_task(coro)
    return QlibBacktestResult(
        backtest_id=backtest_id,
        user_id=request.user_id,
        tenant_id=request.tenant_id,
        status="pending",
        config=request_dict,
        task_id=f"minibt-{uuid4().hex[:12]}",
        annual_return=0.0,
        sharpe_ratio=0.0,
        max_drawdown=0.0,
        alpha=0.0,
    )


def cleanup_work_dir(backtest_id: str) -> None:
    """删除该回测的临时工作目录（容器内路径）。"""
    shutil.rmtree(os.path.join(_WORK_ROOT, backtest_id), ignore_errors=True)
