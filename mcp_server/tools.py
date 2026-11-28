"""MCP 工具实现（v1 + v2 只读）。

每个工具都经 `_guarded`：
- 认证（SDK key → token）与配置（全局/key 开关、权限、配额）
- 计时、审计（成功/拒绝/失败）
- 上游错误统一映射成机器可读的 `{code, message, retryable}`
"""

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Annotated, Any
from urllib.parse import quote

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from backend.shared.mcp_tool_catalog import TOOLS_BY_NAME

from .api_client import ApiClient, ApiError
from .auth import AuthError, AuthManager, ExternalSession
from .quota import QuotaExceeded, QuotaManager

logger = logging.getLogger(__name__)


@dataclass
class Deps:
    auth: AuthManager
    api: ApiClient
    quota: QuotaManager


def _err(code: str, message: str, *, retryable: bool = False) -> ToolError:
    return ToolError(
        json.dumps(
            {"code": code, "message": message, "retryable": retryable},
            ensure_ascii=False,
        )
    )


def _tool_error_code(exc: ToolError) -> str:
    try:
        payload = json.loads(str(exc))
        if isinstance(payload, dict):
            return str(payload.get("code") or "tool_error")
    except ValueError:
        pass
    return "tool_error"


def _session_from_ctx(ctx: Context, deps: Deps) -> ExternalSession:
    request = None
    if ctx.request_context is not None:
        request = ctx.request_context.request
    if request is not None:
        session = getattr(request.state, "qm_session", None)
        if session is None:
            raise _err("auth_required", "缺少已认证的 MCP 会话")
        return session
    raise _err("auth_required", "stdio 模式请通过环境变量提供凭据")


async def _stdio_or_request_session(ctx: Context, deps: Deps) -> ExternalSession:
    request = None
    if ctx.request_context is not None:
        request = ctx.request_context.request
    if request is not None:
        return _session_from_ctx(ctx, deps)
    return await deps.auth.stdio_session()


def _args_digest(args: dict[str, Any]) -> str:
    raw = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


async def _audit(
    deps: Deps,
    session: ExternalSession | None,
    *,
    tool: str,
    args: dict[str, Any],
    status: str,
    code: str | None,
    started: float,
    request_id: str | None,
) -> None:
    if session is None:
        return
    await deps.api.post_audit(
        session,
        [
            {
                "tool": tool,
                "status": status,
                "error_code": code,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "args_digest": _args_digest(args),
                "request_id": request_id,
                "session_id": None,
                "client_info": "mcp",
            }
        ],
    )


async def _guarded(
    ctx: Context,
    deps: Deps,
    tool: str,
    args: dict[str, Any],
    handler,
):
    spec = TOOLS_BY_NAME.get(tool)
    if spec is None:
        raise _err("tool_not_registered", f"未知工具：{tool}")
    started = time.monotonic()
    session: ExternalSession | None = None
    status = "ok"
    code: str | None = None
    request_id = str(getattr(ctx, "request_id", "") or "") or None
    try:
        session = await _stdio_or_request_session(ctx, deps)
        config = await deps.api.config(session)
        if not config.get("enabled"):
            code = "mcp_disabled"
            status = "denied"
            raise _err(code, "MCP 服务已被管理员停用")
        if not config.get("tool_flags", {}).get(tool, spec.default_enabled):
            code = "tool_disabled"
            status = "denied"
            raise _err(code, f"工具 {tool} 已被管理员禁用")
        permissions = set(config.get("permissions") or [])
        if spec.permission not in permissions:
            code = "permission_denied"
            status = "denied"
            raise _err(code, f"当前凭据缺少权限：{spec.permission}")
        limits = config.get("limits") or {}
        await deps.quota.check_and_incr(
            session,
            "calls",
            limits.get("calls_per_minute", 60),
            window_seconds=60,
        )
        return await handler(session, config)
    except QuotaExceeded as exc:
        code = "quota_exceeded"
        status = "quota"
        raise _err(
            code,
            f"配额已用尽：{exc.metric}（上限 {exc.limit}）",
        ) from exc
    except AuthError as exc:
        code = exc.code
        status = "denied"
        raise _err(exc.code, exc.message, retryable=exc.retryable) from exc
    except ApiError as exc:
        code = exc.code
        status = "error"
        raise _err(exc.code, exc.message, retryable=exc.retryable) from exc
    except ToolError as exc:
        code = code or _tool_error_code(exc)
        if status == "ok":
            status = "denied" if code in {"permission_denied", "tool_disabled", "mcp_disabled"} else "error"
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("[mcp] tool %s failed", tool)
        code = "internal_error"
        status = "error"
        raise _err(code, f"工具执行失败：{type(exc).__name__}") from exc
    finally:
        await _audit(
            deps,
            session,
            tool=tool,
            args=args,
            status=status,
            code=code,
            started=started,
            request_id=request_id,
        )


def _annotations(tool: str) -> ToolAnnotations:
    spec = TOOLS_BY_NAME[tool]
    return ToolAnnotations(
        title=spec.description,
        readOnlyHint=not spec.write,
        destructiveHint=spec.destructive,
        idempotentHint=not spec.write,
        openWorldHint=False,
    )


def register_tools(mcp: FastMCP, deps: Deps) -> None:
    Symbol = Annotated[str, Field(description="标的代码，600519 / SH600519 / 600519.SH 均可")]

    @mcp.tool(name="qm_capabilities", annotations=_annotations("qm_capabilities"))
    async def qm_capabilities(ctx: Context) -> dict[str, Any]:
        """返回部署能力、实盘开关、当前凭据和可用工具。接入后先调它。"""

        async def run(session: ExternalSession, config: dict[str, Any]):
            data = await deps.api.get(session, "/capabilities")
            data["mcp"] = {
                "enabled": config.get("enabled"),
                "permissions": config.get("permissions"),
                "limits": config.get("limits"),
                "tools": [
                    name
                    for name, enabled in (config.get("tool_flags") or {}).items()
                    if enabled
                ],
            }
            return data

        return await _guarded(ctx, deps, "qm_capabilities", {}, run)

    @mcp.tool(name="qm_list_datasets", annotations=_annotations("qm_list_datasets"))
    async def qm_list_datasets(ctx: Context) -> dict[str, Any]:
        """数据集目录：有哪些数据、覆盖到哪天、是否新鲜。"""

        async def run(session: ExternalSession, _config: dict[str, Any]):
            return await deps.api.get(session, "/data/datasets")

        return await _guarded(ctx, deps, "qm_list_datasets", {}, run)

    @mcp.tool(name="qm_get_dataset_schema", annotations=_annotations("qm_get_dataset_schema"))
    async def qm_get_dataset_schema(
        ctx: Context,
        dataset: Annotated[str, Field(description="数据集名（见 qm_list_datasets）")],
    ) -> dict[str, Any]:
        """某数据集的列名与类型；有日期列时返回 time_column。"""

        async def run(session: ExternalSession, _config: dict[str, Any]):
            return await deps.api.get(
                session,
                f"/data/datasets/{quote(dataset, safe='')}/schema",
            )

        return await _guarded(ctx, deps, "qm_get_dataset_schema", {"dataset": dataset}, run)

    @mcp.tool(name="qm_list_partitions", annotations=_annotations("qm_list_partitions"))
    async def qm_list_partitions(
        ctx: Context,
        dataset: Annotated[str, Field(description="数据集名")],
        since: Annotated[str | None, Field(description="起始日期 YYYY-MM-DD")] = None,
        until: Annotated[str | None, Field(description="结束日期 YYYY-MM-DD")] = None,
        limit: Annotated[int, Field(ge=1, le=5000)] = 500,
    ) -> dict[str, Any]:
        """分区清单（按日期），带 etag。"""

        async def run(session: ExternalSession, _config: dict[str, Any]):
            params = {"limit": limit}
            if since:
                params["since"] = since
            if until:
                params["until"] = until
            return await deps.api.get(
                session,
                f"/data/datasets/{quote(dataset, safe='')}/partitions",
                params=params,
            )

        return await _guarded(
            ctx,
            deps,
            "qm_list_partitions",
            {"dataset": dataset, "since": since, "until": until, "limit": limit},
            run,
        )

    @mcp.tool(name="qm_query_dataset", annotations=_annotations("qm_query_dataset"))
    async def qm_query_dataset(
        ctx: Context,
        dataset: Annotated[str, Field(description="分区数据集名")],
        columns: Annotated[list[str], Field(min_length=1, max_length=50, description="要返回的列（先用 schema 查）")],
        start: Annotated[str | None, Field(description="起始日期 YYYY-MM-DD")] = None,
        end: Annotated[str | None, Field(description="结束日期 YYYY-MM-DD")] = None,
        symbols: Annotated[list[str] | None, Field(max_length=100, description="标的列表")] = None,
        limit: Annotated[int, Field(ge=1, le=5000)] = 500,
    ) -> dict[str, Any]:
        """有界查询：列/日期/标的/行数都封顶，返回 JSON 行；不返回整份 parquet。"""

        async def run(session: ExternalSession, config: dict[str, Any]):
            max_rows = int((config.get("limits") or {}).get("query_max_rows", 5000))
            params: dict[str, Any] = {
                "dataset": dataset,
                "columns": ",".join(columns),
                "limit": min(int(limit), max_rows),
            }
            if start:
                params["start"] = start
            if end:
                params["end"] = end
            if symbols:
                params["symbols"] = ",".join(symbols)
            return await deps.api.get(session, "/data/query", params=params)

        return await _guarded(
            ctx,
            deps,
            "qm_query_dataset",
            {
                "dataset": dataset,
                "columns": columns,
                "start": start,
                "end": end,
                "symbols": symbols,
                "limit": limit,
            },
            run,
        )

    @mcp.tool(name="qm_get_dataset_file", annotations=_annotations("qm_get_dataset_file"))
    async def qm_get_dataset_file(
        ctx: Context,
        dataset: Annotated[str, Field(description="分区数据集名")],
        partition: Annotated[str | None, Field(description="YYYY-MM-DD；缺省最新分区")] = None,
    ) -> dict[str, Any]:
        """返回分区文件的下载地址与 etag（不把文件体塞进工具结果）。"""

        async def run(session: ExternalSession, _config: dict[str, Any]):
            listed = await deps.api.get(
                session,
                f"/data/datasets/{quote(dataset, safe='')}/partitions",
                params={"limit": 1 if partition else 5000},
            )
            entries = (listed or {}).get("partitions") or []
            if partition:
                entry = next((e for e in entries if e.get("partition") == partition), None)
            else:
                entry = entries[-1] if entries else None
            if entry is None:
                raise _err("not_found", "没有可用的分区")
            part = str(entry.get("partition"))
            return {
                "dataset": dataset,
                "partition": part,
                "etag": entry.get("etag"),
                "bytes": entry.get("bytes"),
                "url": f"/api/ext/v1/data/datasets/{quote(dataset, safe='')}/partitions/{part}/file",
                "note": "用当前 MCP 会话的 Bearer token 请求该地址",
            }

        return await _guarded(
            ctx,
            deps,
            "qm_get_dataset_file",
            {"dataset": dataset, "partition": partition},
            run,
        )

    @mcp.tool(name="qm_list_strategies", annotations=_annotations("qm_list_strategies"))
    async def qm_list_strategies(
        ctx: Context,
        market: Annotated[str | None, Field(description="CN/HK/US/…")] = None,
        search: Annotated[str | None, Field(max_length=100)] = None,
        include_templates: Annotated[bool, Field(description="是否含平台内置模板")] = False,
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
    ) -> dict[str, Any]:
        """当前用户的策略清单（只读）。"""

        async def run(session: ExternalSession, _config: dict[str, Any]):
            params: dict[str, Any] = {"include_templates": include_templates, "limit": limit}
            if market:
                params["market"] = market
            if search:
                params["search"] = search
            return await deps.api.get(session, "/control/strategies", params=params)

        return await _guarded(
            ctx,
            deps,
            "qm_list_strategies",
            {"market": market, "search": search, "include_templates": include_templates, "limit": limit},
            run,
        )

    @mcp.tool(name="qm_list_models", annotations=_annotations("qm_list_models"))
    async def qm_list_models(
        ctx: Context,
        status: Annotated[str | None, Field(description="模型状态过滤（ready/active/…）")] = None,
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
    ) -> dict[str, Any]:
        """当前用户的模型清单（只读）。"""

        async def run(session: ExternalSession, _config: dict[str, Any]):
            params: dict[str, Any] = {"limit": limit}
            if status:
                params["status"] = status
            return await deps.api.get(session, "/control/models", params=params)

        return await _guarded(ctx, deps, "qm_list_models", {"status": status, "limit": limit}, run)

    @mcp.tool(name="qm_submit_task", annotations=_annotations("qm_submit_task"))
    async def qm_submit_task(
        ctx: Context,
        kind: Annotated[str, Field(description="training / backtest / alpha_evolve / data_sync")],
        payload: Annotated[dict[str, Any], Field(description="任务请求体（见 /task/kinds）")],
    ) -> dict[str, Any]:
        """提交训练/回测/因子演化/数据同步任务；返回 ref，用 qm_get_task 轮询。"""

        async def run(session: ExternalSession, config: dict[str, Any]):
            await deps.quota.check_and_incr(
                session,
                "task_submits",
                int((config.get("limits") or {}).get("task_submits_per_day", 20)),
                window_seconds=86400,
            )
            return await deps.api.post(session, f"/task/{quote(kind, safe='')}", json=payload)

        return await _guarded(
            ctx,
            deps,
            "qm_submit_task",
            {"kind": kind, "payload": payload},
            run,
        )

    @mcp.tool(name="qm_get_task", annotations=_annotations("qm_get_task"))
    async def qm_get_task(
        ctx: Context,
        kind: Annotated[str, Field(description="任务种类")],
        ref: Annotated[str, Field(description="提交返回的 ref")],
    ) -> dict[str, Any]:
        """轮询任务状态（queued/running/succeeded/failed/cancelled/unknown）。"""

        async def run(session: ExternalSession, _config: dict[str, Any]):
            return await deps.api.get(
                session, f"/task/{quote(kind, safe='')}/{quote(ref, safe='')}"
            )

        return await _guarded(ctx, deps, "qm_get_task", {"kind": kind, "ref": ref}, run)

    @mcp.tool(name="qm_sim_account", annotations=_annotations("qm_sim_account"))
    async def qm_sim_account(
        ctx: Context,
        market: Annotated[str, Field(description="CN/HK/US")] = "CN",
    ) -> dict[str, Any]:
        """模拟账户快照 + 持仓。"""

        async def run(session: ExternalSession, _config: dict[str, Any]):
            return await deps.api.get(
                session, "/trading/sim/account", params={"market": market}
            )

        return await _guarded(ctx, deps, "qm_sim_account", {"market": market}, run)

    @mcp.tool(name="qm_sim_orders", annotations=_annotations("qm_sim_orders"))
    async def qm_sim_orders(
        ctx: Context,
        status: Annotated[str | None, Field(description="pending/submitted/filled/cancelled/rejected")] = None,
        symbol: Annotated[str | None, Field(description="标的过滤")] = None,
        limit: Annotated[int, Field(ge=1, le=1000)] = 50,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> list[dict[str, Any]]:
        """模拟委托列表。"""

        async def run(session: ExternalSession, _config: dict[str, Any]):
            params: dict[str, Any] = {"limit": limit, "offset": offset}
            if status:
                params["status"] = status
            if symbol:
                params["symbol"] = symbol
            return await deps.api.get(session, "/trading/sim/orders", params=params)

        return await _guarded(
            ctx,
            deps,
            "qm_sim_orders",
            {"status": status, "symbol": symbol, "limit": limit, "offset": offset},
            run,
        )

    @mcp.tool(name="qm_sim_order", annotations=_annotations("qm_sim_order"))
    async def qm_sim_order(
        ctx: Context,
        order_id: Annotated[str, Field(description="委托 UUID")],
    ) -> dict[str, Any]:
        """单张模拟委托。"""

        async def run(session: ExternalSession, _config: dict[str, Any]):
            return await deps.api.get(
                session, f"/trading/sim/orders/{quote(order_id, safe='')}"
            )

        return await _guarded(ctx, deps, "qm_sim_order", {"order_id": order_id}, run)

    @mcp.tool(name="qm_sim_trades", annotations=_annotations("qm_sim_trades"))
    async def qm_sim_trades(
        ctx: Context,
        symbol: Annotated[str | None, Field(description="标的过滤")] = None,
        limit: Annotated[int, Field(ge=1, le=1000)] = 50,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> list[dict[str, Any]]:
        """模拟成交列表（对账以成交为准）。"""

        async def run(session: ExternalSession, _config: dict[str, Any]):
            params: dict[str, Any] = {"limit": limit, "offset": offset}
            if symbol:
                params["symbol"] = symbol
            return await deps.api.get(session, "/trading/sim/trades", params=params)

        return await _guarded(
            ctx, deps, "qm_sim_trades", {"symbol": symbol, "limit": limit, "offset": offset}, run
        )

    @mcp.tool(name="qm_sim_preview_order", annotations=_annotations("qm_sim_preview_order"))
    async def qm_sim_preview_order(
        ctx: Context,
        symbol: Symbol,
        side: Annotated[str, Field(description="buy / sell")],
        quantity: Annotated[float, Field(gt=0)],
        order_type: Annotated[str, Field(description="market / limit")] = "market",
        price: Annotated[float | None, Field(gt=0, description="限价单价格")] = None,
        market: Annotated[str, Field(description="CN/HK/US")] = "CN",
    ) -> dict[str, Any]:
        """模拟下单预估：账户现金、预估金额、单笔上限；不产生委托。"""

        async def run(session: ExternalSession, config: dict[str, Any]):
            account = await deps.api.get(
                session, "/trading/sim/account", params={"market": market}
            )
            max_value = float((config.get("limits") or {}).get("sim_order_max_value", 0) or 0)
            amount = float(price) * float(quantity) if price is not None else None
            return {
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "order_type": order_type,
                "price": price,
                "estimated_amount": amount,
                "max_order_value": max_value,
                "within_limit": True if amount is None else amount <= max_value,
                "account": {
                    "cash": (account or {}).get("cash"),
                    "total_asset": (account or {}).get("total_asset"),
                    "market": market,
                },
                "note": "仅为 MCP 预估，不替代上游风控；下单仍需 qm_sim_place_order + client_order_id",
            }

        return await _guarded(
            ctx,
            deps,
            "qm_sim_preview_order",
            {
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "order_type": order_type,
                "price": price,
                "market": market,
            },
            run,
        )

    @mcp.tool(name="qm_sim_place_order", annotations=_annotations("qm_sim_place_order"))
    async def qm_sim_place_order(
        ctx: Context,
        symbol: Symbol,
        side: Annotated[str, Field(description="buy / sell")],
        quantity: Annotated[float, Field(gt=0)],
        client_order_id: Annotated[str, Field(min_length=1, max_length=80, description="幂等键；重试必须复用")],
        order_type: Annotated[str, Field(description="market / limit")] = "market",
        price: Annotated[float | None, Field(gt=0, description="限价单必填")] = None,
        remarks: Annotated[str | None, Field(max_length=500)] = None,
        strategy_id: Annotated[int | None, Field(gt=0)] = None,
    ) -> dict[str, Any]:
        """提交模拟委托（仅模拟盘；必须带 client_order_id）。"""

        async def run(session: ExternalSession, config: dict[str, Any]):
            max_value = float((config.get("limits") or {}).get("sim_order_max_value", 0) or 0)
            if price is not None and max_value > 0 and float(price) * float(quantity) > max_value:
                raise _err(
                    "order_value_exceeded",
                    f"预估金额超过单笔上限 {max_value:.0f} 元",
                )
            await deps.quota.check_and_incr(
                session,
                "sim_orders",
                int((config.get("limits") or {}).get("sim_orders_per_day", 20)),
                window_seconds=86400,
            )
            body: dict[str, Any] = {
                "symbol": symbol,
                "side": side,
                "order_type": order_type,
                "quantity": quantity,
                "client_order_id": client_order_id,
            }
            if price is not None:
                body["price"] = price
            if remarks:
                body["remarks"] = remarks
            if strategy_id is not None:
                body["strategy_id"] = strategy_id
            return await deps.api.post(session, "/trading/sim/orders", json=body)

        return await _guarded(
            ctx,
            deps,
            "qm_sim_place_order",
            {
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "client_order_id": client_order_id,
                "order_type": order_type,
                "price": price,
                "remarks": remarks,
                "strategy_id": strategy_id,
            },
            run,
        )

    @mcp.tool(name="qm_sim_cancel_order", annotations=_annotations("qm_sim_cancel_order"))
    async def qm_sim_cancel_order(
        ctx: Context,
        order_id: Annotated[str, Field(description="委托 UUID")],
        reason: Annotated[str | None, Field(max_length=200)] = None,
    ) -> dict[str, Any]:
        """撤销模拟委托。"""

        async def run(session: ExternalSession, _config: dict[str, Any]):
            body = {"reason": reason} if reason else {}
            return await deps.api.post(
                session,
                f"/trading/sim/orders/{quote(order_id, safe='')}/cancel",
                json=body,
            )

        return await _guarded(
            ctx, deps, "qm_sim_cancel_order", {"order_id": order_id, "reason": reason}, run
        )

    # ── v2 只读：推理排名 ───────────────────────────────────────────────
    @mcp.tool(name="qm_get_inference_rankings", annotations=_annotations("qm_get_inference_rankings"))
    async def qm_get_inference_rankings(
        ctx: Context,
        model_id: Annotated[str, Field(description="模型 id")],
        date: Annotated[str | None, Field(description="交易日 YYYY-MM-DD；缺省最近一次推理")] = None,
        top: Annotated[int, Field(ge=1, le=500)] = 100,
        offset: Annotated[int, Field(ge=0, le=10000)] = 0,
    ) -> dict[str, Any]:
        """某模型某交易日的推理排名（分页），展示面只说位置，不构成交易建议。"""

        async def run(session: ExternalSession, _config: dict[str, Any]):
            params: dict[str, Any] = {"model_id": model_id, "top": top, "offset": offset}
            if date:
                params["date"] = date
            return await deps.api.get(session, "/inference/rankings", params=params)

        return await _guarded(
            ctx,
            deps,
            "qm_get_inference_rankings",
            {"model_id": model_id, "date": date, "top": top, "offset": offset},
            run,
        )

    @mcp.tool(name="qm_get_stock_score", annotations=_annotations("qm_get_stock_score"))
    async def qm_get_stock_score(
        ctx: Context,
        model_id: Annotated[str, Field(description="模型 id")],
        symbol: Symbol,
        date: Annotated[str | None, Field(description="交易日 YYYY-MM-DD")] = None,
    ) -> dict[str, Any]:
        """单票在某模型/交易日的评分与截面位置。"""

        async def run(session: ExternalSession, _config: dict[str, Any]):
            params: dict[str, Any] = {"model_id": model_id, "symbol": symbol}
            if date:
                params["date"] = date
            return await deps.api.get(session, "/inference/stock-score", params=params)

        return await _guarded(
            ctx,
            deps,
            "qm_get_stock_score",
            {"model_id": model_id, "symbol": symbol, "date": date},
            run,
        )
