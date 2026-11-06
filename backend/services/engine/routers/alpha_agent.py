"""AlphaAgent / RD-Agent 因子挖掘 REST API

支持多市场因子挖掘: A股、加密货币、港股、美股
"""

import asyncio
import logging
import os
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse

from backend.services.engine.alpha_agent.hw_lock import HardwareLockError
from backend.services.engine.alpha_agent.launcher import get_launcher
from backend.services.engine.auth_context import (
    assert_identity_not_spoofed,
    get_authenticated_identity,
)
from backend.services.engine.qlib_app.services.rd_agent_persistence import (
    RDAgentFactorPersistence,
)
from backend.shared.stock_pool.builtins import cn_index_symbols

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/alpha-agent", tags=["AlphaAgent"])

# 因子挖掘扩展子路由（企业版下放）：任务/因子作业/因子池/文档/研究/方向批次/恢复/论文/清理/管线。
from backend.services.engine.alpha_agent.factor_job_api import router as factor_job_router
from backend.services.engine.alpha_agent.pool_research_api import router as pool_research_router
from backend.services.engine.alpha_agent.direction_batch_api import router as direction_batch_router
from backend.services.engine.alpha_agent.factor_pool_api import router as factor_pool_router
from backend.services.engine.alpha_agent.doc_api import router as doc_router
from backend.services.engine.alpha_agent.research_api import router as research_router
from backend.services.engine.alpha_agent.factor_recovery_api import router as recovery_router
from backend.services.engine.alpha_agent.paper_vault_api import router as paper_vault_router
from backend.services.engine.alpha_agent.purge_api import router as purge_router
from backend.services.engine.alpha_agent.research_pipeline import router as pipeline_router
from backend.services.engine.alpha_agent.statistical.variant_api import router as variant_router

router.include_router(factor_job_router)
router.include_router(pool_research_router)
router.include_router(direction_batch_router)
router.include_router(factor_pool_router)
router.include_router(doc_router)
router.include_router(research_router)
router.include_router(recovery_router)
router.include_router(paper_vault_router)
router.include_router(purge_router)
router.include_router(variant_router)
router.include_router(pipeline_router)

persistence = RDAgentFactorPersistence()

# P2：CN 可选股票池由 shared.stock_pool.builtins 派生（唯一事实源），
# 与 quantdb_hub.UNIVERSE_MAP / Strategy Lab 白名单同源，不再各写一份。
_VALID_CN_UNIVERSES: list[str] = list(cn_index_symbols().keys())


def _normalize_pool_ref(universe: str) -> str:
    u = (universe or "").strip()
    if not u:
        return "pool:csi300"
    if u.startswith(("pool:", "pool_id:", "list:", "file:")):
        return u
    return f"pool:{u}"


def _universe_is_valid(universe: str) -> bool:
    if universe in _VALID_CN_UNIVERSES:
        return True
    try:
        from backend.shared.stock_pool.resolver import resolve_pool_sync

        snap = resolve_pool_sync(_normalize_pool_ref(universe))
        return bool(snap.unfiltered or snap.symbols)
    except Exception:
        return False


def _resolve_custom_pool_instruments(universe: str) -> list[str] | None:
    """非内置 code 时尝试全局股票池解析；内置池返回 None 走原逻辑。"""
    if universe in _VALID_CN_UNIVERSES:
        return None
    try:
        from backend.shared.stock_pool.resolver import resolve_pool_sync
        from backend.shared.stock_utils import StockCodeUtil

        snap = resolve_pool_sync(_normalize_pool_ref(universe))
        if snap.unfiltered:
            return None
        if not snap.symbols:
            return []
        return sorted({StockCodeUtil.to_prefix(s) for s in snap.symbols})
    except Exception as e:
        logger.warning("custom pool %s resolve failed: %s", universe, e)
        return None


_running_backtests: set[str] = set()
# 回测子进程句柄 + 取消标记：cancel 接口据此真正 kill 子进程
_backtest_processes: dict[str, subprocess.Popen] = {}
_backtest_cancelled: set[str] = set()


async def _fetch_profile_llm_config(user_id: str, tenant_id: str):
    """读取用户个人中心「AI 服务配置」（Profile 的 api_key/llm_base_url/llm_model）。

    与 AI-IDE 共享同一份凭证。无有效 Key 返回 None。
    """
    from backend.services.engine.alpha_agent.llm_client import (
        LLMConfig,
        _is_placeholder,
        parse_extra_headers,
    )

    try:
        from backend.shared.auth import get_internal_call_secret

        gateway = os.getenv("INTERNAL_API_GATEWAY_URL") or "http://127.0.0.1:8000"
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{gateway}/api/v1/profiles/{user_id}",
                headers={
                    "X-Internal-Call": get_internal_call_secret(),
                    "X-User-Id": user_id,
                    "X-Tenant-Id": tenant_id,
                },
            )
        if resp.status_code != 200:
            logger.warning("[alpha-agent] fetch profile %s llm config: http %s", user_id, resp.status_code)
            return None
        data = resp.json().get("data", {})
        key = (data.get("api_key") or "").strip()
        if not key or _is_placeholder(key):
            return None
        base = (data.get("llm_base_url") or "").strip()
        model = (data.get("llm_model") or "").strip()
        # 以用户设置为准：base/model 缺失视为未配置，回退 env 兜底
        if not base or not model:
            return None
        base = base.rstrip("/")
        # DeepSeek 等 Anthropic 兼容端点（.../anthropic）走 Anthropic 协议
        protocol = "anthropic" if "/anthropic" in base or model.lower().startswith("astron") else "openai"
        # OpenAI 兼容端点统一保留 /v1（实际调用/RD-Agent 子进程都按 {base}/chat/completions 拼接）
        if protocol == "openai" and not base.endswith("/v1"):
            base += "/v1"
        headers = parse_extra_headers(data.get("llm_extra_headers"))
        return LLMConfig(
            api_key=key, base_url=base, model=model, protocol=protocol, headers=headers
        )
    except Exception:
        logger.exception("[alpha-agent] fetch profile llm config failed")
        return None


async def _resolve_effective_llm_config(user_id: str, tenant_id: str):
    """以用户设置为准：优先当前用户 Profile 的「AI 服务配置」，环境变量仅作兜底。"""
    from backend.services.engine.alpha_agent.llm_client import resolve_llm_config

    cfg = await _fetch_profile_llm_config(user_id, tenant_id)
    if cfg is not None:
        return cfg, "user_profile"
    cfg = resolve_llm_config()
    if cfg is not None:
        return cfg, "env"
    return None, "none"


class FactorBacktestCancelled(RuntimeError):
    """用户主动取消因子回测（子进程被 kill）。"""


async def _run_subprocess_tracked(
    factor_id: str,
    args: list[str],
    timeout: float = 600,
) -> tuple[int, str, str]:
    """运行回测子进程并登记句柄，供 cancel 接口 kill。

    Returns: (returncode, stdout, stderr)。用户取消时抛 FactorBacktestCancelled。
    """
    import subprocess

    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    _backtest_processes[factor_id] = proc
    try:
        try:
            stdout, stderr = await asyncio.to_thread(proc.communicate, timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = await asyncio.to_thread(proc.communicate)
            raise RuntimeError(f"因子计算超时（>{int(timeout)}s），子进程已终止") from None
        if factor_id in _backtest_cancelled:
            raise FactorBacktestCancelled("回测已被用户取消")
        return proc.returncode, stdout or "", stderr or ""
    finally:
        _backtest_processes.pop(factor_id, None)


async def _require_owned_task(task_id: str, request: Request) -> dict:
    """返回任务状态，若不属于当前认证用户则 404（不泄露任务是否存在）。"""
    auth_user_id, _ = get_authenticated_identity(request)
    launcher = get_launcher()
    status = await launcher.get_task_status(task_id)
    if not status or status.get("user_id") != auth_user_id:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    return status


async def _require_owned_factor(
    factor_id: str, request: Request, *, for_write: bool = False
) -> dict:
    """返回因子，若不属于当前认证用户则 404。

    历史因子的 user_id 可能为空（该列加入前写入），此类记录允许只读访问，
    但禁止写操作（回测/解释会回填 metrics），避免跨用户篡改。
    """
    auth_user_id, _ = get_authenticated_identity(request)
    factor = await persistence.get_factor(factor_id)
    if not factor:
        raise HTTPException(status_code=404, detail=f"Factor {factor_id} not found")
    owner = factor.get("user_id")
    if owner != auth_user_id and (owner or for_write):
        raise HTTPException(status_code=404, detail=f"Factor {factor_id} not found")
    return factor


@router.get("/markets")
async def list_markets():
    """列出所有可用的市场"""
    from backend.services.engine.rd_agent.market_adapters import list_markets as _list_markets
    markets = _list_markets()
    # Check data readiness for each market
    for m in markets:
        try:
            from backend.services.engine.rd_agent.market_adapters import get_adapter
            adapter = get_adapter(m["market_id"])
            m["data_ready"] = adapter.is_data_ready()
        except Exception:
            m["data_ready"] = False
    return {"code": 200, "data": {"markets": markets, "total": len(markets)}}


@router.post("/evolve")
async def start_evolution(
    request: Request,
    user_id: str | None = Query(None, description="已废弃：身份取自 JWT，仅用于防伪校验"),
    market: str = Query("a_share", description="市场: a_share, crypto, hong_kong, us_stock"),
    universe: str = Query("csi300", description="股票池: csi300, csi500, csi1000, sse50, gem, star, csi800, all_a"),
    loop_n: int = Query(5, ge=1, le=20, description="演化轮数"),
    direction: str = Query("", description="因子挖掘方向/假设"),
    directions: list[str] = Query(default=[], description="L1 因子类别方向列表（多选）"),
    direction_mode: str = Query("selected", description="类别选择模式: selected=取第一条, random=随机一条"),
    data_source: str = Query("", description="数据源: qlib_bin, parquet, pg (留空使用默认)"),
):
    """启动因子演化任务"""
    auth_user_id, auth_tenant_id = get_authenticated_identity(request)
    assert_identity_not_spoofed(
        auth_user_id=auth_user_id,
        auth_tenant_id=auth_tenant_id,
        provided_user_id=user_id,
    )

    # Validate market
    try:
        from backend.services.engine.rd_agent.market_adapters import get_adapter, list_markets
        adapter = get_adapter(market)
    except ValueError as e:
        available = [m["market_id"] for m in list_markets()]
        raise HTTPException(
            status_code=400,
            detail=f"Unknown market: {market}. Available: {available}",
        ) from e

    # Validate universe（内置指数 + 全局自定义股票池）
    if market == "a_share" and not _universe_is_valid(universe):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown universe: {universe}. "
                f"Available builtins: {_VALID_CN_UNIVERSES}, "
                "or any active global/custom pool code from /stock-pools/options"
            ),
        )

    llm_config, llm_source = await _resolve_effective_llm_config(auth_user_id, auth_tenant_id)
    if llm_config is None:
        raise HTTPException(
            status_code=412,
            detail="未配置 LLM API Key：可在个人中心「其他设置 → AI 服务配置」填写（与 AI-IDE 共用），"
            "或在服务器 .env 配置 DEEPSEEK_API_KEY / AI_IDE_LLM_API_KEY / OPENAI_API_KEY。",
        )
    logger.info("[alpha-agent] evolve llm source=%s model=%s", llm_source, llm_config.model)

    # 类别方向下发：前端传多选类别 + 模式，服务端解析成单条 direction
    clean_dirs = [d.strip() for d in directions if isinstance(d, str) and d.strip()]
    if clean_dirs:
        import random as _random

        direction = (
            _random.choice(clean_dirs) if direction_mode == "random" else clean_dirs[0]
        )
        logger.info(
            "[alpha-agent] evolve directions=%d mode=%s -> %s",
            len(clean_dirs), direction_mode, direction,
        )

    launcher = get_launcher()
    # 并发上限：每个任务是 RD-Agent 子进程（烧 LLM token + Qlib 回测），
    # 必须限流防止 fork 风暴。可用环境变量调整。
    counts = launcher.count_running()
    max_per_user = int(os.getenv("ALPHA_AGENT_MAX_RUNNING_PER_USER", "2"))
    max_global = int(os.getenv("ALPHA_AGENT_MAX_RUNNING_GLOBAL", "4"))
    user_running = counts["by_user"].get(auth_user_id, 0)
    if user_running >= max_per_user:
        raise HTTPException(
            status_code=429,
            detail=f"您已有 {user_running} 个挖掘任务在运行（上限 {max_per_user}），请等待完成或先取消任务。",
        )
    if counts["global"] >= max_global:
        raise HTTPException(
            status_code=429,
            detail=f"当前全平台挖掘任务数已达上限（{max_global}），请稍后再试。",
        )
    try:
        task_id = await launcher.start_evolution(
            auth_user_id,
            market=market,
            universe=universe,
            loop_n=loop_n,
            direction=direction or None,
            data_source=data_source or None,
            llm_overrides=llm_config.llm_env_overrides(),
        )
    except HardwareLockError as exc:
        raise HTTPException(status_code=412, detail=str(exc)) from exc
    return {
        "code": 200,
        "data": {
            "task_id": task_id,
            "market": market,
            "universe": universe,
            "market_name": adapter.market_name,
            "status": "pending",
            "message": f"{adapter.market_name} 因子挖掘任务已启动",
        },
    }


@router.get("/tasks/history")
async def mining_task_history(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=1000000),
    status: str | None = None,
    include_archived: bool = False,
):
    from backend.services.engine.qlib_app.services.rd_agent_persistence import (
        AlphaMiningTaskPersistence,
    )
    from backend.shared.secret_redaction import redact_data

    user_id, tenant_id = get_authenticated_identity(request)
    try:
        page = await AlphaMiningTaskPersistence().history_page(
            tenant_id=tenant_id,
            user_id=user_id,
            limit=limit,
            offset=offset,
            status=status,
            include_archived=include_archived,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(
        content=jsonable_encoder({"code": 200, "data": redact_data(page)}),
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/tasks/{task_id}")
async def get_task_status(task_id: str, request: Request):
    """查询演化任务状态（附带该任务已落库的结构化因子，供前端实时展示）"""
    status = await _require_owned_task(task_id, request)
    auth_user_id, _ = get_authenticated_identity(request)
    try:
        status["factors"] = await persistence.list_factors(
            user_id=auth_user_id, task_id=task_id, limit=20,
        )
    except Exception:
        logger.exception("[alpha-agent] list factors for task %s failed", task_id)
        status["factors"] = []
    return {"code": 200, "data": status}


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, request: Request):
    """取消演化任务"""
    await _require_owned_task(task_id, request)
    launcher = get_launcher()
    ok = await launcher.cancel_task(task_id)
    if not ok:
        raise HTTPException(status_code=400, detail="无法取消任务（可能已完成或不存在）")
    return {"code": 200, "data": {"task_id": task_id, "status": "cancelled"}}


@router.get("/tasks/{task_id}/log")
async def get_task_log(
    task_id: str,
    request: Request,
    tail: int = Query(500, ge=1, le=5000, description="返回行数"),
    offset: int = Query(0, ge=0, description="从第N行开始返回（0-based）"),
):
    """获取任务的详细子进程日志（分页读取）"""
    await _require_owned_task(task_id, request)
    launcher = get_launcher()
    log_content = await launcher.get_task_log(task_id, tail=0)
    if log_content is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} log not found")
    all_lines = log_content.splitlines()
    total = len(all_lines)
    # offset-based slicing: return lines[offset:offset+tail]
    end = min(offset + tail, total)
    lines = all_lines[offset:end]
    return {
        "code": 200,
        "data": {
            "task_id": task_id,
            "lines": lines,
            "total": total,
        },
    }


@router.get("/tasks")
async def list_tasks(
    request: Request,
    user_id: str | None = Query(None, description="已废弃：身份取自 JWT，仅用于防伪校验"),
    market: str | None = Query(None, description="按市场过滤"),
):
    """列出当前用户的演化任务"""
    auth_user_id, auth_tenant_id = get_authenticated_identity(request)
    assert_identity_not_spoofed(
        auth_user_id=auth_user_id,
        auth_tenant_id=auth_tenant_id,
        provided_user_id=user_id,
    )
    launcher = get_launcher()
    tasks = await launcher.list_tasks(user_id=auth_user_id)
    if market:
        tasks = [t for t in tasks if t.get("market") == market]
    return {"code": 200, "data": {"tasks": tasks, "total": len(tasks)}}


@router.get("/factors")
async def list_factors(
    request: Request,
    user_id: str | None = Query(None, description="已废弃：身份取自 JWT，仅用于防伪校验"),
    market: str | None = Query(None, description="按市场过滤"),
    universe: str | None = Query(None, description="按股票池过滤"),
    status: str | None = Query(None, description="按状态过滤: pending/backtesting/completed/failed"),
    limit: int = Query(50, ge=1, le=200),
):
    """列出当前用户已生成的因子"""
    auth_user_id, auth_tenant_id = get_authenticated_identity(request)
    assert_identity_not_spoofed(
        auth_user_id=auth_user_id,
        auth_tenant_id=auth_tenant_id,
        provided_user_id=user_id,
    )
    factors = await persistence.list_factors(
        user_id=auth_user_id, status=status, market=market, universe=universe, limit=limit,
    )
    return {"code": 200, "data": {"factors": factors, "total": len(factors)}}


@router.get("/factors/{factor_id}")
async def get_factor(factor_id: str, request: Request):
    """获取单个因子详情"""
    factor = await _require_owned_factor(factor_id, request)
    return {"code": 200, "data": factor}


def _resolve_factory_manifest() -> Path:
    """因子工厂 MANIFEST.csv 路径（quantcustom 用户自定义数据集）。"""
    root = os.getenv("QM_QUANTCUSTOM_DATA_DIR") or "/data/quantcustom"
    return Path(root) / "6_ml_datasets" / "l1_factors" / "MANIFEST.csv"


def _to_float(value: object) -> float | None:
    try:
        f = float(value)  # type: ignore[arg-type]
        return f if f == f else None  # 过滤 NaN
    except (TypeError, ValueError):
        return None


@router.get("/factory-factors")
async def list_factory_factors(request: Request):
    """列出因子工厂产出的表达式因子（只读，来自 quantcustom MANIFEST.csv）。

    工厂因子是共享的批量产出（不属某个用户），只展示、不提供回测/训练操作。
    """
    import csv

    get_authenticated_identity(request)  # 复用统一鉴权（与 /factors 一致）

    manifest = _resolve_factory_manifest()
    if not manifest.is_file():
        return {"code": 200, "data": {"factors": [], "total": 0, "generated_at": None}}

    factors: list[dict] = []
    with manifest.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            name = (row.get("factor_name") or "").strip()
            if not name:
                continue
            expr = (row.get("expression") or "").strip()
            factors.append({
                "factor_id": f"factory:{name}",
                "factor_name": name,
                "factor_expression": expr,
                "factor_formulation": expr,
                "factor_code": "",
                "ic_value": _to_float(row.get("ic")),
                "icir": _to_float(row.get("icir")),
                "coverage": _to_float(row.get("coverage")),
                "rank_ic": None,
                "status": "completed",
                "market": "a_share",
                "universe": "all_a",
                "source": "factor_factory",
                "read_only": True,
                "metadata": {
                    "source": "factor_factory",
                    "read_only": True,
                    "field": row.get("field") or "",
                    "icir": _to_float(row.get("icir")),
                    "coverage": _to_float(row.get("coverage")),
                },
            })
    factors.sort(key=lambda x: abs(x.get("ic_value") or 0.0), reverse=True)
    return {
        "code": 200,
        "data": {
            "factors": factors,
            "total": len(factors),
            "generated_at": datetime.fromtimestamp(manifest.stat().st_mtime).isoformat(),
        },
    }


@router.post("/factors/{factor_id}/explain")
async def explain_factor(factor_id: str, request: Request):
    """用 LLM 中文解释因子含义"""
    factor = await _require_owned_factor(factor_id, request, for_write=True)

    # Check if explanation already exists
    metadata = factor.get("metadata") or {}
    if metadata.get("explanation"):
        return {"code": 200, "data": {"explanation": metadata["explanation"], "cached": True}}

    factor_name = factor.get("factor_name", "unknown")
    factor_code = factor.get("factor_code", "")
    factor_formulation = metadata.get("factor_formulation", "")

    from backend.services.engine.alpha_agent.llm_client import chat as llm_chat

    auth_user_id, auth_tenant_id = get_authenticated_identity(request)
    llm_config, _ = await _resolve_effective_llm_config(auth_user_id, auth_tenant_id)
    if llm_config is None:
        raise HTTPException(
            status_code=412,
            detail="未配置 LLM API Key：可在个人中心「其他设置 → AI 服务配置」填写，或在服务器 .env 配置。",
        )

    prompt = f"""请用中文简洁地解释以下量化因子。输出格式：
1. **含义**：一句话概括
2. **金融直觉**：为什么这个因子可能有效
3. **适用场景**：在什么市场环境下表现较好
4. **预期方向**：因子值高/低时预示什么

因子名称：{factor_name}
因子公式：{factor_formulation or factor_code[:500]}

请直接输出解释，不要重复因子公式。"""

    try:
        explanation = await llm_chat(
            [{"role": "user", "content": prompt}],
            max_tokens=500,
            temperature=0.3,
            timeout=30,
            config=llm_config,
        )
    except httpx.HTTPStatusError as e:
        logger.error("LLM explain failed: status=%s body=%s", e.response.status_code, e.response.text[:300])
        raise HTTPException(status_code=502, detail=f"LLM 服务返回错误 ({e.response.status_code})，请检查 API Key 配置") from e
    except Exception as e:
        logger.error("LLM explain failed: %s", e)
        raise HTTPException(status_code=500, detail="LLM 解释失败，请稍后重试") from e

    # Store explanation in metadata
    metadata["explanation"] = explanation
    await persistence.update_factor_metrics(factor_id, metadata=metadata)

    return {"code": 200, "data": {"explanation": explanation, "cached": False}}


@router.post("/factors/{factor_id}/backtest")
async def backtest_factor(
    factor_id: str,
    request: Request,
    start_date: str | None = None,
    end_date: str | None = None,
    universe: str | None = Query("csi300", description="回测股票池: csi300, csi500, csi1000, sse50, gem, star, csi800, all_a"),
    data_source: str | None = Query("qlib_bin", description="回测数据源: qlib_bin(默认) | h5"),
):
    """对因子发起轻量验证（多市场 + 数据源可选）

    data_source=qlib_bin (默认): 用 Qlib 二进制 (5 个市场均支持)；
    data_source=h5: 走 RD-Agent daily_pv.h5（A股/港股/美股有预生成，期货从 parquet 自动生成，crypto 预生成）。
    """
    factor = await _require_owned_factor(factor_id, request, for_write=True)

    if not factor.get("factor_code"):
        raise HTTPException(status_code=400, detail="因子代码为空，无法回测")

    if factor_id in _running_backtests:
        return {
            "code": 200,
            "data": {
                "factor_id": factor_id,
                "status": "backtesting",
                "message": "回测已在进行中",
            },
        }

    market = factor.get("market") or "a_share"
    await persistence.update_factor_metrics(factor_id, status="backtesting")
    _running_backtests.add(factor_id)

    asyncio.create_task(
        _run_factor_backtest(
            factor_id,
            factor.get("factor_code") or "",
            market=market,
            data_source=data_source or "qlib_bin",
            start_date=start_date,
            end_date=end_date,
            universe=universe or "csi300",
        )
    )

    return {
        "code": 200,
        "data": {
            "factor_id": factor_id,
            "status": "backtesting",
            "message": f"快速验证已触发: {factor.get('factor_name')} (market={market}, data_source={data_source})",
        },
    }


@router.post("/factors/{factor_id}/cancel")
async def cancel_backtest(factor_id: str, request: Request):
    """取消一个正在进行的回测：kill 子进程（不再等 600s 超时）并标记 cancelled"""
    factor = await _require_owned_factor(factor_id, request)
    if factor_id not in _running_backtests:
        return {"code": 200, "data": {"factor_id": factor_id, "status": factor.get("status"), "message": "回测未在运行"}}
    _backtest_cancelled.add(factor_id)
    proc = _backtest_processes.get(factor_id)
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            for _ in range(10):
                if proc.poll() is not None:
                    break
                await asyncio.sleep(0.2)
            if proc.poll() is None:
                proc.kill()
                logger.warning("[alpha-backtest] force-killed subprocess for %s", factor_id)
        except ProcessLookupError:
            pass
        except Exception as e:
            logger.warning("[alpha-backtest] cancel kill %s failed: %s", factor_id, e)
    _running_backtests.discard(factor_id)
    try:
        await persistence.update_factor_metrics(
            factor_id,
            status="cancelled",
            metadata={"backtest_error": "cancelled_by_user"},
        )
    except Exception:
        pass
    return {"code": 200, "data": {"factor_id": factor_id, "status": "cancelled"}}


@router.post("/factors/{factor_id}/export")
async def export_factor_to_ide(
    factor_id: str,
    request: Request,
):
    """将因子代码导出到 AI-IDE 工作空间"""
    factor = await _require_owned_factor(factor_id, request)
    user_id, _ = get_authenticated_identity(request)

    factor_code = factor.get("factor_code") or ""
    if not factor_code.strip():
        raise HTTPException(status_code=400, detail="因子代码为空，无法导出")

    factor_name = factor.get("factor_name", "unnamed_factor")
    meta = factor.get("metadata") or {}

    # 生成带头部注释的完整 Python 文件
    header_lines = [
        '"""',
        f'Factor: {factor_name}',
        'Source: RD-Agent Alpha Research',
        f'IC: {factor.get("ic_value", "N/A")}',
        f'RankIC: {meta.get("rank_ic", "N/A")}',
        f'Sharpe: {factor.get("sharpe_ratio", "N/A")}',
        f'Market: {meta.get("market", "a_share")}',
        f'Description: {meta.get("description", "")[:200]}',
        '"""',
        '',
    ]
    full_code = "\n".join(header_lines) + factor_code

    # 保存到策略库
    from backend.shared.strategy_storage import get_strategy_storage_service
    svc = get_strategy_storage_service()
    file_name = f"factor_{factor_name}"
    res = await svc.save(
        user_id=user_id,
        name=file_name,
        code=full_code,
        metadata={
            "status": "DRAFT",
            "source": "alpha_research",
            "factor_id": factor_id,
            "description": f"Alpha Factor: {factor_name}",
            "tags": ["alpha", meta.get("market", "a_share")],
        },
    )

    return {
        "code": 200,
        "data": {
            "strategy_id": res["id"],
            "name": file_name,
            "message": f"因子 {factor_name} 已导出到 AI-IDE 工作空间",
        },
    }


@router.get("/stats")
async def get_stats(
    request: Request,
    market: str | None = Query(None, description="按市场过滤统计"),
):
    """当前用户的因子统计信息"""
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    auth_user_id, _ = get_authenticated_identity(request)

    conditions = ["user_id = :user_id"]
    params: dict = {"user_id": auth_user_id}
    if market:
        conditions.append("market = :market")
        params["market"] = market
    where_clause = "WHERE " + " AND ".join(conditions)

    async with get_session(read_only=True) as session:
        rows = await session.execute(text(f"""
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE status = 'completed') AS completed,
                COUNT(*) FILTER (WHERE status = 'pending') AS pending,
                COUNT(*) FILTER (WHERE status = 'backtesting') AS backtesting,
                COUNT(*) FILTER (WHERE status = 'failed') AS failed,
                AVG(ic_value) FILTER (WHERE ic_value IS NOT NULL) AS avg_ic,
                AVG(sharpe_ratio) FILTER (WHERE sharpe_ratio IS NOT NULL) AS avg_sharpe,
                MAX(ic_value) AS best_ic,
                MAX(sharpe_ratio) AS best_sharpe
            FROM rd_agent_factors
            {where_clause}
        """), params)
        row = rows.mappings().first()

    if not row:
        return {"code": 200, "data": {}}

    data = dict(row)
    for key in ("avg_ic", "best_ic", "avg_sharpe", "best_sharpe"):
        if data.get(key) is not None:
            data[key] = round(float(data[key]), 4)
    return {"code": 200, "data": data}


@router.get("/data-summary")
async def get_data_summary():
    """返回 QuantDB 数据可用性摘要（日期范围、股票池、数据集）"""
    try:
        from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub
        hub = QuantDBDataHub.get_instance()
        summary = hub.get_data_summary()
        return {"code": 200, "data": summary}
    except Exception as e:
        logger.warning("Failed to get data summary: %s", e)
        return {"code": 200, "data": {"available": False, "error": str(e)[:200]}}


@router.get("/factor-categories")
async def get_factor_categories():
    """返回 L1 因子类别（从 feature catalog 加载）"""
    try:
        from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub
        hub = QuantDBDataHub.get_instance()
        categories = hub.fetch_l1_factor_categories()
        return {"code": 200, "data": categories}
    except Exception as e:
        logger.warning("Failed to get factor categories: %s", e)
        return {"code": 200, "data": {"categories": []}}


@router.get("/universes")
async def get_universes(request: Request):
    """返回可用股票池及股票数（内置指数 + 用户可见的全局自定义池）"""
    universes: dict[str, dict] = {}
    try:
        from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

        hub = QuantDBDataHub.get_instance()
        summary = hub.get_data_summary()
        for code, meta in (summary.get("universes") or {}).items():
            universes[code] = {
                "count": meta.get("count", 0) if isinstance(meta, dict) else 0,
                "indexSymbol": meta.get("indexSymbol") if isinstance(meta, dict) else None,
                "is_system": True,
            }
    except Exception as e:
        logger.warning("Failed to get builtin universes: %s", e)

    try:
        from sqlalchemy import text

        from backend.shared.database_manager_v2 import get_session
        from backend.shared.stock_pool import repository as pool_repo

        user_id, tenant_id = get_authenticated_identity(request)
        async with get_session() as session:
            await pool_repo.ensure_tables(session)
            rows = (
                (
                    await session.execute(
                        text(
                            """
                    SELECT code, name, symbol_count, is_system
                      FROM qm_stock_pool
                     WHERE status <> 'archived'
                       AND market = 'CN'
                       AND (
                            scope = 'global'
                            OR (scope = 'tenant' AND tenant_id = :tenant_id)
                            OR (scope = 'user' AND owner_user_id = :user_id)
                       )
                     ORDER BY is_system DESC, code ASC
                    """
                        ),
                        {"tenant_id": tenant_id, "user_id": user_id},
                    )
                )
                .mappings()
                .all()
            )
        for row in rows:
            code = str(row["code"])
            if code in universes and row["is_system"]:
                continue
            universes[code] = {
                "count": int(row["symbol_count"] or 0),
                "indexSymbol": None,
                "is_system": bool(row["is_system"]),
                "name": str(row["name"] or code),
            }
    except Exception as e:
        logger.warning("Failed to merge custom stock pools: %s", e)

    return {"code": 200, "data": {"universes": universes}}


@router.get("/llm-config")
async def get_llm_config(request: Request):
    """返回当前生效的 LLM 配置状态（不回显完整 key）。

    优先级：服务器环境变量 > 当前用户个人中心的 AI 服务配置。
    """
    from backend.services.engine.alpha_agent.llm_client import resolve_llm_config

    auth_user_id, auth_tenant_id = get_authenticated_identity(request)
    cfg = await _fetch_profile_llm_config(auth_user_id, auth_tenant_id)
    source = "user_profile"
    if cfg is None:
        cfg = resolve_llm_config()
        source = "env"
    if cfg is None:
        return {
            "code": 200,
            "data": {
                "configured": False,
                "reason": "未配置可用的 API Key：可在个人中心「其他设置 → AI 服务配置」填写，或在服务器 .env 配置",
            },
        }
    # 仅回显 key 末 4 位，避免泄露
    key = cfg.api_key
    masked = f"****{key[-4:]}" if len(key) >= 4 else "****"
    return {
        "code": 200,
        "data": {
            "configured": True,
            "source": source,
            "provider": cfg.protocol,
            "model": cfg.model,
            "base_url": cfg.base_url,
            "api_key_masked": masked,
        },
    }


# ---- 企业版下放：算法/数据集/研究经验 ----


@router.get("/algorithms")
async def list_mining_algorithms():
    """List validated algorithm capabilities without exposing runner paths."""
    from backend.services.engine.alpha_agent.algorithm_registry import list_algorithms

    public_fields = (
        "key",
        "name",
        "version",
        "desc",
        "vendor",
        "enabled",
        "tags",
        "markets",
        "freqs",
        "uses_datasets",
        "uses_llm",
        "requires_gpu",
        "progress_kind",
        "stages",
    )
    from backend.services.engine.alpha_agent.statistical.configuration import (
        statistical_config_payload,
    )

    algorithms = []
    for item in list_algorithms():
        if item.get("valid") is True:
            statistical_options = {}
            if item.get("key") == "statistical" and item.get("enabled"):
                try:
                    statistical_options[
                        "statistical_defaults"
                    ] = await asyncio.to_thread(statistical_config_payload)
                except Exception:
                    logger.exception("Statistical mining configuration is unavailable")
                    algorithms.append(
                        {
                            "key": "statistical",
                            "valid": False,
                            "error": "统计挖掘配置暂不可用，请联系平台维护人员",
                        }
                    )
                    continue
            algorithms.append(
                {
                    **{field: item[field] for field in public_fields},
                    "valid": True,
                    "supports_direction_batches": item.get("key") == "rdagent",
                    "supports_sampled_direction_batches": item.get("key") == "rdagent",
                    "supports_direction_quota": item.get("key") == "rdagent",
                    "supports_experience_memory": True,
                    "supports_variant_ledger": item.get("key") == "statistical",
                    **statistical_options,
                }
            )
        else:
            algorithms.append(
                {
                    "key": str(item.get("key") or "")[:64],
                    "valid": False,
                    "error": "算法配置无效，请联系平台维护人员",
                }
            )
    return {
        "code": 200,
        "data": {"algorithms": algorithms, "total": len(algorithms)},
    }


@router.get("/datasets")
async def list_mining_datasets(request: Request = None):
    """List the public datasets available through the sandbox's QuantDB SDK."""
    # OSS-ADAPT：开源版无远端挖掘节点，固定走本地 mining_catalog。
    from backend.services.engine.alpha_agent.mining_catalog import dataset_catalog
    from backend.services.engine.rd_agent.rd_loop_wrapper import RDLoopWrapper

    root_value = RDLoopWrapper._resolve_quantdb_dir()
    return await asyncio.to_thread(
        dataset_catalog, Path(root_value) if root_value else None,
    )


@router.get("/research/experience")
async def get_research_experience(    request: Request,
    universe: str | None = Query(default=None, min_length=1, max_length=128),
):
    """Read bounded, derived experience from this authenticated owner's records."""
    user_id, tenant_id = get_authenticated_identity(request)
    if universe is not None and not _universe_is_valid(universe):
        raise HTTPException(status_code=404, detail="股票池不可用")
    from backend.services.engine.alpha_agent.experience_memory import load_memory

    try:
        memory = await load_memory(
            tenant_id=tenant_id, user_id=user_id, universe=universe
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Private research experience could not be loaded")
        raise HTTPException(status_code=503, detail="本人研究经验暂不可用") from exc
    return {"code": 200, "data": memory}


@router.post("/preview")
async def preview_mining(request: Request, payload: dict):
    """Validate and summarize a mining request without admission or execution."""
    auth_user_id, auth_tenant_id = get_authenticated_identity(request)
    from backend.services.engine.alpha_agent.algorithm_registry import (
        AlgorithmError,
        resolve_algorithm,
    )
    from backend.services.engine.alpha_agent.api_models import MiningSubmitRequest
    from backend.services.engine.alpha_agent.mining_windows import (
        build_mining_window_env,
    )

    try:
        parsed = MiningSubmitRequest.model_validate(payload)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    values = parsed.model_dump(exclude_none=True)
    algorithm_key = values.get("algorithm", "rdagent")
    from backend.services.engine.alpha_agent.direction_sampling import clean_directions

    try:
        preview_directions = clean_directions(values.get("directions", []))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    market = values.get("market", "a_share")
    freq = values.get("freq", "day")
    datasets = values.get("datasets")
    try:
        algorithm = resolve_algorithm(
            algorithm_key, market=market, freq=freq, datasets=datasets
        )
        # OSS-ADAPT：无远端挖掘节点，datasets 直接取请求值，不做远端解析。
        canonical_datasets: tuple[str, ...] = tuple(datasets or ())
        try:
            from backend.services.engine.rd_agent.market_adapters import get_adapter

            get_adapter(market)
        except ValueError as exc:
            raise ValueError(f"Unknown market: {market}") from exc
        universe = values.get("universe", "all_a")
        if market == "a_share" and not _universe_is_valid(universe):
            raise ValueError("所选股票池不可用或不属于当前用户")
        statistical_config = None
        if algorithm.key == "statistical":
            from backend.services.engine.alpha_agent.statistical.configuration import (
                STATISTICAL_PARAMETER_KEYS,
                statistical_config_payload,
            )

            if values.get("data_source") not in {None, "", "qlib_bin"}:
                raise ValueError("统计挖掘仅支持公共 Qlib 日线数据")
            statistical_config = await asyncio.to_thread(
                statistical_config_payload,
                {
                    key: values[key]
                    for key in STATISTICAL_PARAMETER_KEYS
                    if key in values
                },
            )
        window_env = build_mining_window_env(
            values.get("mining_start"),
            values.get("mining_end"),
            values.get("valid_start"),
            values.get("valid_end"),
        )
        # OSS-ADAPT：远端 admission 快照/窗口校验（mining_remote）跳过，单容器本地执行。
    except (AlgorithmError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if algorithm.key == "statistical" and not values.get("mining_start"):
        raise HTTPException(status_code=422, detail="统计挖掘必须提供挖掘日期范围")

    datasets_by_key = {}
    if canonical_datasets:
        from backend.services.engine.rd_agent.mining_datasets import DATASET_LABELS

        datasets_by_key = {key: DATASET_LABELS[key] for key in canonical_datasets}

    if window_env:
        window_preview = {
            "mode": "custom",
            "mining": [
                window_env["QM_MINING_WINDOW_START"],
                window_env["QM_MINING_WINDOW_END"],
            ],
            "train": [
                window_env["QM_QLIB_TRAIN_START"],
                window_env["QM_QLIB_TRAIN_END"],
            ],
            "valid": [
                window_env["QM_QLIB_VALID_START"],
                window_env["QM_QLIB_VALID_END"],
            ],
            "test": [
                window_env["QM_QLIB_TEST_START"],
                window_env["QM_QLIB_TEST_END"],
            ],
            "oos": (
                [
                    window_env["QM_VALIDATION_WINDOW_START"],
                    window_env["QM_VALIDATION_WINDOW_END"],
                ]
                if "QM_VALIDATION_WINDOW_START" in window_env
                else None
            ),
        }
    else:
        window_preview = {"mode": "server_default"}

    direction_weighting = (
        "blankness"
        if algorithm.key == "rdagent"
        and values.get("direction_mode") == "random"
        and preview_directions
        else None
    )
    direction_quota_preview = None
    if (
        algorithm.key == "rdagent"
        and values.get("direction_mode") == "quota"
        and preview_directions
    ):
        from backend.services.engine.alpha_agent import direction_quota
        from backend.shared.owner_scope import OwnerScope

        try:
            quota_owner = OwnerScope(auth_tenant_id, auth_user_id)
        except ValueError:
            quota_owner = None
        if quota_owner is not None:
            direction_quota_preview = await direction_quota.preview(
                owner=quota_owner,
                market=market,
                universe=universe,
                directions=preview_directions,
                n=1,
            )

    return {
        "code": 200,
        "data": {
            "algorithm": {
                "key": algorithm.key,
                "name": algorithm.name,
                "version": algorithm.version,
                "uses_llm": algorithm.uses_llm,
                "uses_datasets": algorithm.uses_datasets,
            },
            "market": market,
            "freq": freq,
            "universe": universe,
            "data_source": values.get("data_source") or "qlib_bin",
            "loop_n": values.get("loop_n", 3),
            "direction": values.get("direction", ""),
            "directions": preview_directions,
            "direction_mode": values.get("direction_mode", "selected"),
            "direction_weighting": direction_weighting,
            "direction_quota": direction_quota_preview,
            "datasets": datasets_by_key,
            "window": window_preview,
            "requires_llm": algorithm.uses_llm,
            "statistical_config": statistical_config,
        },
    }


# ---- 企业版下放：方向分解与批量派发 ----


@router.post("/directions/decompose")
async def decompose_mining_direction(payload: dict, request: Request):
    from backend.services.engine.alpha_agent.api_models import DirectionDecomposeRequest
    from backend.services.engine.alpha_agent.research_api import handle_decompose

    try:
        parsed = DirectionDecomposeRequest.model_validate(payload)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return await handle_decompose(parsed, request)


@router.post("/directions/dispatch")
async def dispatch_mining_directions(payload: dict, request: Request) -> dict:
    from backend.services.engine.alpha_agent.api_models import DirectionBatchDispatchRequest

    try:
        parsed = DirectionBatchDispatchRequest.model_validate(payload)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return await _dispatch_mining_directions(parsed, request)


async def _queue_position(task_id: str, tenant_id: str, user_id: str) -> int | None:
    from backend.services.engine.qlib_app.services.rd_agent_persistence import (
        AlphaMiningTaskPersistence,
    )

    return await AlphaMiningTaskPersistence().queue_position(
        task_id, tenant_id=tenant_id, user_id=user_id
    )


async def _dispatch_mining_directions(payload, request: Request, *, selection_batch=None) -> dict:
    """Enqueue reviewed hypotheses in the owner's shared durable mining queue."""
    user_id, tenant_id = get_authenticated_identity(request)
    if not tenant_id or not user_id:
        raise HTTPException(status_code=401, detail="Authenticated owner is required")
    if payload.market == "a_share" and not _universe_is_valid(payload.universe):
        raise HTTPException(status_code=400, detail="本人股票池不存在或不可用")

    from backend.services.engine.alpha_agent.algorithm_registry import (
        AlgorithmError,
        resolve_algorithm,
    )
    from backend.services.engine.rd_agent.mining_datasets import resolve_datasets

    try:
        resolve_algorithm(
            "rdagent",
            market=payload.market,
            freq=payload.freq,
            datasets=payload.datasets,
        )
        datasets = list(resolve_datasets(payload.datasets))
    except (AlgorithmError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if payload.doc_id is not None:
        from backend.services.engine.alpha_agent.doc_store import DocStore

        source_doc = await DocStore().get_doc(
            payload.doc_id, tenant_id=tenant_id, user_id=user_id
        )
        if (
            source_doc is None
            or source_doc.get("status") != "organized"
            or source_doc.get("organize_state") != "completed"
            or not isinstance(source_doc.get("organized_payload"), dict)
        ):
            raise HTTPException(
                status_code=409,
                detail="本人文档尚无已确认的整理结果，无法批量派发挖掘",
            )

    seed_factor_ids = payload.seed_factor_ids or [None] * len(payload.directions)
    seed_factors_by_id: dict[str, dict] = {}
    unique_seed_ids = list(dict.fromkeys(value for value in seed_factor_ids if value))
    if unique_seed_ids:
        from backend.services.engine.alpha_agent.factor_pool import (
            FactorPoolPersistence,
        )

        try:
            seed_rows = await FactorPoolPersistence().get_seed_factors(
                unique_seed_ids,
                tenant_id=tenant_id,
                user_id=user_id,
                market=payload.market,
                universe=payload.universe,
            )
        except LookupError as exc:
            raise HTTPException(
                status_code=404, detail="Parent factor not found"
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        seed_factors_by_id = {str(row["factor_id"]): row for row in seed_rows}

    llm_config, _llm_source = await _resolve_effective_llm_config(user_id, tenant_id)
    if llm_config is None:
        raise HTTPException(
            status_code=412,
            detail="未配置 LLM API Key：可在个人中心「其他设置 → AI 服务配置」填写（与 AI-IDE 共用），"
            "或在服务器 .env 配置 DEEPSEEK_API_KEY / AI_IDE_LLM_API_KEY / OPENAI_API_KEY。",
        )
    try:
        from backend.shared.storage_quota import check_user_quota
    except ImportError:
        check_user_quota = None
    if check_user_quota is not None:
        from backend.shared.storage_quota import StorageQuotaExceeded

        launcher = get_launcher()
        try:
            check_user_quota(
                launcher._log_dir,
                tenant_id,
                user_id,
                quota_gb=float(os.getenv("FACTOR_QUOTA_GB", "30")),
            )
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, StorageQuotaExceeded):
                raise HTTPException(
                    status_code=507,
                    detail={"code": "QUOTA_EXCEEDED", "retryable": False},
                ) from exc
            raise HTTPException(status_code=503, detail="存储配额暂时无法核验") from exc

    from backend.services.engine.alpha_agent.task_spec import (
        IdempotencyConflict,
        MiningTaskSpec,
    )

    batch_id = str(payload.batch_id)
    items: list[dict] = []
    started = queued = failed = 0
    for index, direction in enumerate(payload.directions):
        namespace = "doc-batch" if payload.doc_id is not None else "direction-batch"
        if selection_batch is not None:
            namespace = "sample-batch"
        request_key = f"{namespace}:{batch_id}:{index}"
        seed_factor_id = seed_factor_ids[index]
        item_seed_factors = (
            [seed_factors_by_id[seed_factor_id]] if seed_factor_id else []
        )
        item_seed_ids = [seed_factor_id] if seed_factor_id else []
        request_parameters = {
            "algorithm": "rdagent",
            "market": payload.market,
            "universe": payload.universe,
            "loop_n": payload.loop_n,
            "direction": direction,
            "batch_id": batch_id,
            "batch_size": len(payload.directions),
            "batch_index": index,
            "seed_factor_ids": item_seed_ids,
        }
        if payload.doc_id is not None:
            request_parameters["doc_id"] = payload.doc_id
        if selection_batch is not None:
            request_parameters["selection_batch_id"] = batch_id
        execution_options = {
            "data_source": payload.data_source,
            "freq": payload.freq,
            "datasets": datasets,
            "mining_start": payload.mining_start,
            "mining_end": payload.mining_end,
            "valid_start": payload.valid_start,
            "valid_end": payload.valid_end,
        }
        request_parameters.update(
            (key, value)
            for key, value in execution_options.items()
            if key in payload.model_fields_set
        )
        # OSS-ADAPT：单容器无 mining_remote admission 快照，data_snapshot 置空；
        # 落库后由 engine 内队列泵（factor-mining-queue）本地执行。
        parameters = dict(request_parameters)
        parameters["seed_factors"] = item_seed_factors
        try:
            from backend.services.engine.alpha_agent.resource_budget import (
                AlphaQueueFull,
                submit_budget,
            )
            from backend.services.engine.qlib_app.services.rd_agent_persistence import (
                AlphaMiningTaskPersistence,
            )

            specification = MiningTaskSpec.build(
                algorithm="rdagent",
                algorithm_version="rdagent",
                request=request_parameters,
                params=parameters,
                data_snapshot={},
                request_key=request_key,
            )
            task_id = uuid.uuid4().hex[:16]
            budget = submit_budget(task_id, tenant_id, user_id)
            store = AlphaMiningTaskPersistence()
            try:
                admitted_task_id = await store.create(
                    task_id,
                    tenant_id,
                    user_id,
                    specification=specification,
                    market=payload.market,
                    universe=payload.universe,
                    loop_n=payload.loop_n,
                    direction=direction,
                    data_source=payload.data_source or "",
                    source_doc_id=payload.doc_id,
                    status="queued",
                    phase="queued",
                    progress="等待前序任务完成",
                    executor_json="{}",
                )
            except Exception:
                try:
                    from backend.services.engine.alpha_agent.resource_budget import (
                        POOL as alpha_pool,
                    )

                    budget.store.cancel_queued(alpha_pool, task_id, tenant_id, user_id)
                except Exception:  # noqa: BLE001
                    pass
                raise
            task_id = admitted_task_id or task_id
            persisted = await store.get(
                task_id, tenant_id=tenant_id, user_id=user_id, consistent=True
            )
            if persisted is None:
                raise RuntimeError("任务状态暂时无法核验")
            status = persisted["status"]
            is_queued = status in {"pending", "queued"}
            item_error = None
            if status in {"failed", "cancelled"}:
                failed += 1
                item_error = "此幂等任务已进入终态；请核查任务详情后用新批次重试"
            elif is_queued:
                queued += 1
            else:
                started += 1
            items.append(
                {
                    "index": index,
                    "direction": direction,
                    "task_id": task_id,
                    "seed_factor_ids": item_seed_ids,
                    "status": status,
                    "queued": is_queued,
                    "queue_position": await _queue_position(
                        task_id, tenant_id, user_id
                    ),
                    "error": item_error,
                }
            )
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 -- preserve other item receipts
            if isinstance(exc, AlphaQueueFull):
                message = "因子挖掘队列暂不可用"
            elif isinstance(exc, ValueError):
                message = str(exc)
            else:
                message = "本方向派发失败，可用相同批次 ID 安全重试"
            logger.warning(
                "[alpha-agent] direction dispatch failed owner=%s/%s batch=%s index=%d error=%s",
                tenant_id,
                user_id,
                batch_id,
                index,
                type(exc).__name__,
            )
            failed += 1
            items.append(
                {
                    "index": index,
                    "direction": direction,
                    "task_id": None,
                    "status": "failed",
                    "queued": False,
                    "queue_position": None,
                    "error": message,
                }
            )

    return {
        "code": 200,
        "data": {
            "batch_id": batch_id,
            "items": items,
            "started": started,
            "queued": queued,
            "failed": failed,
        },
    }


# ---- 企业版下放：存储配额、提交回查、任务历史 ----


@router.get("/storage")
async def factor_mining_storage(request: Request):
    """Return only the authenticated user's factor-mining artifact quota."""
    auth_user_id, auth_tenant_id = get_authenticated_identity(request)
    try:
        from backend.shared.storage_quota import user_usage_bytes

        launcher = get_launcher()
        quota_gb = float(os.getenv("FACTOR_QUOTA_GB", "30"))
        used_bytes = await asyncio.to_thread(
            user_usage_bytes, launcher._log_dir, auth_tenant_id, auth_user_id
        )
        quota_bytes = None if quota_gb < 0 else int(quota_gb * (1024**3))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail="存储用量暂时无法核验") from exc
    return {
        "code": 200,
        "data": {
            "used_bytes": used_bytes,
            "quota_bytes": quota_bytes,
            "used_gb": round(used_bytes / (1024**3), 3),
            "quota_gb": None if quota_gb < 0 else quota_gb,
            "unlimited": quota_gb < 0,
            "remaining_bytes": (
                None if quota_bytes is None else max(0, quota_bytes - used_bytes)
            ),
        },
    }


@router.post("/storage/cleanup")
async def cleanup_factor_mining_artifacts(request: Request):
    """Preview or admit a durable cleanup; HTTP never deletes workspace files."""
    auth_user_id, auth_tenant_id = get_authenticated_identity(request)
    if not auth_tenant_id or not auth_user_id:
        raise HTTPException(status_code=401, detail="Authenticated owner is required")
    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail="请求体必须是 JSON") from exc
    task_ids = payload.get("task_ids") if isinstance(payload, dict) else None
    confirm = payload.get("confirm") if isinstance(payload, dict) else None
    if (
        not isinstance(task_ids, list)
        or not task_ids
        or len(task_ids) > 20
        or any(not isinstance(item, str) or not item.strip() for item in task_ids)
    ):
        raise HTTPException(status_code=422, detail="task_ids 必须为 1–20 个任务 ID")
    if confirm is not None and not isinstance(confirm, bool):
        raise HTTPException(status_code=422, detail="confirm 必须是布尔值")
    if len(set(task_ids)) != len(task_ids):
        raise HTTPException(status_code=422, detail="task_ids 不能重复")
    if set(payload) - {"task_ids", "confirm", "preview_token"}:
        raise HTTPException(status_code=422, detail="清理请求包含未知字段")
    token = payload.get("preview_token")
    if token is not None and (
        not isinstance(token, str)
        or len(token) != 64
        or any(char not in "0123456789abcdef" for char in token)
    ):
        raise HTTPException(status_code=422, detail="preview_token 格式无效")

    from backend.shared.owner_path import owner_segment
    from backend.shared.owner_scope import OwnerScope
    from backend.services.engine.alpha_agent.artifact_cleanup import (
        ArtifactCleanupConflict,
        attach_cleanup,
        preview,
    )
    from backend.services.engine.alpha_agent.factor_jobs import (
        AlphaFactorJobPersistence,
        FactorJobIdempotencyConflict,
        FactorJobQueueFull,
    )
    from backend.services.engine.alpha_agent.factor_job_api import _public

    owner = OwnerScope(auth_tenant_id, auth_user_id)
    try:
        for task_id in task_ids:
            if owner_segment(task_id) != task_id:
                raise ValueError("任务 ID 含非法字符")
        jobs = AlphaFactorJobPersistence()
        key = request.headers.get("Idempotency-Key", "").strip() or token or None

        def receipt(job):
            frozen = job["request_snapshot"]["request"]
            items = [
                {"task_id": item["task_id"], "size_bytes": item["size_bytes"]}
                for item in frozen["items"]
            ]
            return JSONResponse(
                status_code=202,
                content={
                    "code": 202,
                    "data": {
                        "confirmed": True,
                        "job": _public(job),
                        "items": items,
                        "total_bytes": sum(item["size_bytes"] for item in items),
                        "note": "清理作业已接收，可在任务中心查看进度；文件删除不可恢复。",
                    },
                    "dispatch": "durable_outbox",
                },
            )

        if confirm is True and key:
            existing = await jobs.get_by_request_key(
                key,
                tenant_id=owner.tenant_id,
                user_id=owner.user_id,
            )
            if existing:
                frozen = existing["request_snapshot"]["request"]
                if (
                    existing["kind"] != "cleanup"
                    or frozen.get("task_ids") != sorted(task_ids)
                    or (token is not None and frozen.get("preview_token") != token)
                ):
                    raise FactorJobIdempotencyConflict("幂等键已用于其他清理请求")
                return receipt(existing)
        root = Path(get_launcher()._log_dir)
        approved = await preview(root, owner, task_ids)
        if confirm is True and token is not None and token != approved["preview_token"]:
            raise ArtifactCleanupConflict("预览后产物已变化，请重新预览")
        if confirm is not True:
            return {
                "code": 200,
                "data": {
                    "confirmed": False,
                    "preview_token": approved["preview_token"],
                    "items": [
                        {"task_id": item["task_id"], "size_bytes": item["size_bytes"]}
                        for item in approved["items"]
                    ],
                    "total_bytes": approved["total_bytes"],
                    "note": "清理不可恢复的任务文件；保留任务、规格、结果、因子代码及训练引用。",
                },
            }

        def attach(session, job_id):
            attach_cleanup(session, owner=owner, job_id=job_id, items=approved["items"])

        job_id = await jobs.create(
            tenant_id=owner.tenant_id,
            user_id=owner.user_id,
            kind="cleanup",
            request={
                "task_ids": sorted(task_ids),
                "items": approved["items"],
                "preview_token": approved["preview_token"],
                "root_binding": approved["root_binding"],
            },
            request_key=key,
            on_created=attach,
        )
        job = await jobs.get(
            job_id, tenant_id=owner.tenant_id, user_id=owner.user_id, consistent=True
        )
        if job is None:
            raise RuntimeError("Cleanup receipt is missing")
        return receipt(job)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ArtifactCleanupConflict, FactorJobIdempotencyConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except FactorJobQueueFull as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="清理作业暂时无法核验或入队"
        ) from exc


@router.get("/submissions/by-request-key")
async def mining_submission_by_request_key(
    request: Request, request_key: str = Query(..., min_length=1, max_length=128)
):
    """Read back the caller's original submission, including terminal tasks."""
    from backend.services.engine.qlib_app.services.rd_agent_persistence import (
        AlphaMiningTaskPersistence,
    )
    from backend.services.engine.alpha_agent.public_task import public_task
    from backend.shared.secret_redaction import redact_data

    user_id, tenant_id = get_authenticated_identity(request)
    task = await AlphaMiningTaskPersistence().get_by_request_key(
        request_key, tenant_id=tenant_id, user_id=user_id
    )
    if task is None:
        raise HTTPException(status_code=404, detail="Mining submission not found")
    return JSONResponse(
        content=jsonable_encoder({"code": 200, "data": redact_data(public_task(task))}),
        headers={"Cache-Control": "private, no-store"},
    )


@router.post("/tasks/{task_id}/archive")
async def archive_mining_task(task_id: str, request: Request):
    """Archive an owned terminal mining task without deleting its evidence."""
    from backend.services.engine.qlib_app.services.rd_agent_persistence import (
        AlphaMiningTaskPersistence,
    )

    user_id, tenant_id = get_authenticated_identity(request)
    try:
        archived = await AlphaMiningTaskPersistence().archive_terminal(
            task_id, tenant_id=tenant_id, user_id=user_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not archived:
        raise HTTPException(status_code=404, detail="Mining task not found")
    return {"code": 200, "data": {"task_id": task_id, "archived": True}}


@router.post("/tasks/{task_id}/restore")
async def restore_mining_task(task_id: str, request: Request):
    """Restore an owned archived terminal task to the visible history."""
    from backend.services.engine.qlib_app.services.rd_agent_persistence import (
        AlphaMiningTaskPersistence,
    )

    user_id, tenant_id = get_authenticated_identity(request)
    try:
        restored = await AlphaMiningTaskPersistence().restore_terminal(
            task_id, tenant_id=tenant_id, user_id=user_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not restored:
        raise HTTPException(status_code=404, detail="Mining task not found")
    return {"code": 200, "data": {"task_id": task_id, "archived": False}}


@router.get("/tasks/{task_id}/result")
async def get_task_result(task_id: str, request: Request):
    """Return both algorithm output and persisted factors in the verified owner scope."""
    await _require_owned_task(task_id, request)
    user_id, tenant_id = get_authenticated_identity(request)
    from backend.services.engine.qlib_app.services.rd_agent_persistence import (
        AlphaMiningTaskPersistence,
    )
    from backend.shared.secret_redaction import redact_data

    store = AlphaMiningTaskPersistence()
    task = await store.get_for_owner_with_spec(
        task_id, tenant_id=tenant_id, user_id=user_id
    )
    if task is None:
        raise HTTPException(status_code=404, detail="Mining task not found")
    algorithm = task.get("algorithm") or "rdagent"
    result = await store.get_result(task_id, tenant_id=tenant_id, user_id=user_id)
    persisted_factors = await persistence.list_factors(
        user_id=user_id,
        task_id=task_id,
        limit=200,
    )
    factors = [
        {
            "factor_id": str(factor.get("factor_id") or ""),
            "factor_name": str(factor.get("factor_name") or ""),
            "factor_formulation": str(factor.get("factor_formulation") or "")[:2000],
            "status": str(factor.get("status") or "unknown"),
            "rank_ic": factor.get("rank_ic"),
            "factor_revision_no": factor.get("factor_revision_no"),
        }
        for factor in persisted_factors
    ]
    return {
        "code": 200,
        "data": redact_data(
            {
                "task_id": task_id,
                "algorithm": algorithm,
                "status": task.get("status"),
                "result": result,
                "factors": factors,
            }
        ),
    }


@router.get("/tasks/{task_id}/log/stream")
async def stream_task_log(
    task_id: str,
    request: Request,
    tail_lines: int = Query(200, ge=0, le=2000, description="首帧回看的日志行数"),
) -> StreamingResponse:
    """SSE 增量推送任务子进程日志（文件为事实源，按字节偏移只读新增）。"""
    import time as _time

    from backend.shared.sse_guard import guard_sse, identity_from_request

    await _require_owned_task(task_id, request)
    auth_user_id, auth_tenant_id = get_authenticated_identity(request)
    from backend.services.engine.alpha_agent.log_stream import stream_log
    from backend.services.engine.qlib_app.services.rd_agent_persistence import (
        AlphaMiningTaskPersistence,
    )

    launcher = get_launcher()
    log_path = launcher.task_log_path(
        task_id, tenant_id=auth_tenant_id, user_id=auth_user_id
    )
    store = AlphaMiningTaskPersistence()
    terminal = {"value": False, "checked_at": 0.0}

    async def is_done() -> bool:
        if terminal["value"]:
            return True
        now = _time.monotonic()
        if now - terminal["checked_at"] < 3.0:
            return False
        terminal["checked_at"] = now
        record = await store.get(
            task_id, tenant_id=auth_tenant_id, user_id=auth_user_id
        )
        if str((record or {}).get("status") or "") in {
            "completed",
            "failed",
            "cancelled",
        }:
            terminal["value"] = True
            return True
        return False

    return StreamingResponse(
        guard_sse(
            identity_from_request(request),
            stream_log(log_path, tail_lines=tail_lines, is_done=is_done),
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "private, no-store",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/tasks/{task_id}/trajectory")
async def get_task_trajectory(task_id: str, request: Request):
    """Read the current owner's RD-Agent loop and phase artifact summary."""
    from backend.shared.secret_redaction import redact_data

    task = await _require_owned_task(task_id, request)
    user_id, tenant_id = get_authenticated_identity(request)
    launcher = get_launcher()
    from backend.services.engine.alpha_agent.trajectory_store import load_trajectory

    try:
        trajectory = await load_trajectory(
            task=task,
            root=launcher._log_dir,
            tenant_id=tenant_id,
            user_id=user_id,
        )
    except (FileNotFoundError, ValueError):
        raise HTTPException(
            status_code=404, detail="Task trajectory not found"
        ) from None
    return {"code": 200, "data": redact_data(trajectory)}


# ---- 企业版下放：因子归档/生命周期/版本 ----


async def _set_factor_archived(
    factor_id: str, request: Request, *, archived: bool, reason: str
) -> dict:
    from backend.services.engine.alpha_agent.factor_visibility import (
        FactorVisibilityConflict,
        FactorVisibilityStore,
    )

    user_id, tenant_id = get_authenticated_identity(request)
    try:
        result = await FactorVisibilityStore().set_archived(
            factor_id,
            tenant_id=tenant_id,
            user_id=user_id,
            archived=archived,
            reason=reason,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Factor not found") from exc
    except FactorVisibilityConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"code": 200, "data": {**result, "history_preserved": True}}


@router.post("/factors/{factor_id}/archive")
async def archive_factor(factor_id: str, payload: dict, request: Request) -> dict:
    reason = payload.get("reason", "") if isinstance(payload, dict) else ""
    return await _set_factor_archived(
        factor_id, request, archived=True, reason=reason
    )


@router.post("/factors/{factor_id}/restore")
async def restore_factor(factor_id: str, payload: dict, request: Request) -> dict:
    reason = payload.get("reason", "") if isinstance(payload, dict) else ""
    return await _set_factor_archived(
        factor_id, request, archived=False, reason=reason
    )


@router.delete("/factors/{factor_id}")
async def archive_factor_legacy(factor_id: str, request: Request) -> dict:
    """Preserve frozen references when older clients request factor deletion."""
    return await _set_factor_archived(
        factor_id, request, archived=True, reason="兼容客户端归档"
    )


@router.post("/factors/{factor_id}/lifecycle/evaluate")
async def evaluate_factor_lifecycle(
    factor_id: str,
    request: Request,
    apply: bool = Query(False, description="显式保存本人因子的评估与降权状态"),
):
    """Evaluate one factor using only the authenticated owner's completed run."""
    from backend.services.engine.alpha_agent.factor_jobs import (
        AlphaFactorJobPersistence,
    )
    from backend.shared.factor_lifecycle import (
        STATE_DOWNWEIGHTED,
        STATE_OBSERVING,
        evaluate_lifecycle,
    )
    from backend.shared.utc_datetime import utc_now

    factor = await _require_owned_factor(factor_id, request, for_write=True)
    user_id, tenant_id = get_authenticated_identity(request)
    jobs = AlphaFactorJobPersistence()
    try:
        evidence = await jobs.latest_completed_backtest_evidence(
            [factor_id], tenant_id=tenant_id, user_id=user_id
        )
        first_completed = await jobs.first_completed_backtest_at(
            [factor_id], tenant_id=tenant_id, user_id=user_id
        )
    except Exception as exc:
        logger.warning("因子生命周期证据读取失败", exc_info=True)
        raise HTTPException(status_code=503, detail="因子评估证据暂不可用") from exc

    evidence_row = evidence.get(factor_id) or {}
    result_snapshot = evidence_row.get("result")
    if isinstance(result_snapshot, str):
        try:
            import json

            result_snapshot = json.loads(result_snapshot)
        except (TypeError, ValueError):
            result_snapshot = None
    chart = result_snapshot.get("series") if isinstance(result_snapshot, dict) else None
    raw_series = chart.get("lifecycle_ic_series") if isinstance(chart, dict) else None
    series = raw_series if isinstance(raw_series, dict) else None
    now = utc_now()
    result = evaluate_lifecycle(
        factor,
        series=series,
        first_completed_run_at=first_completed.get(factor_id),
        now=now,
    )
    lifecycle = result["state_before"]
    if apply:
        try:
            await persistence.record_factor_lifecycle_evaluation(
                factor_id,
                result,
                evaluated_at=now,
                factor_revision_no=(
                    result_snapshot.get("factor_revision_no")
                    if isinstance(result_snapshot, dict)
                    and isinstance(result_snapshot.get("factor_revision_no"), int)
                    else None
                ),
                evidence_job_id=evidence_row.get("job_id") or None,
                user_id=user_id,
                tenant_id=tenant_id,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="因子不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            logger.warning("因子生命周期结果写入失败", exc_info=True)
            raise HTTPException(status_code=503, detail="因子评估结果暂未保存") from exc

    if lifecycle == STATE_OBSERVING:
        return {
            "code": 200,
            "data": {
                "factor_id": factor_id,
                "state": STATE_OBSERVING,
                "blind_until": result["blind_until"],
                "eval_status": "blind_window",
                "action": "none",
                "applied": apply,
            },
        }

    verdict = result["verdict"]
    latest = verdict.get("latest") or {}
    return {
        "code": 200,
        "data": {
            "factor_id": factor_id,
            "state": result["state_after"] if apply else lifecycle,
            "blind_until": result["blind_until"],
            "eval_status": result["eval_status"],
            "decision": verdict.get("decision"),
            "streak": verdict.get("streak"),
            "latest": latest,
            "readings": result["readings"],
            "action": result["action"],
            "applied": apply,
            "downweighted": (
                result["state_after"] == STATE_DOWNWEIGHTED
                if apply
                else lifecycle == STATE_DOWNWEIGHTED
            ),
        },
    }


@router.get("/factors/{factor_id}/lifecycle/history")
async def factor_lifecycle_history(
    factor_id: str,
    request: Request,
    limit: int = Query(50, ge=1, le=100),
    before_event_id: int | None = Query(None, gt=0),
):
    """Return a bounded page of this owner's lifecycle evaluation history."""
    from backend.services.engine.alpha_agent.factor_jobs import (
        AlphaFactorJobPersistence,
    )
    from backend.shared.factor_lifecycle import blind_until_of
    from backend.shared.utc_datetime import utc_now

    factor = await _require_owned_factor(factor_id, request)
    user_id, tenant_id = get_authenticated_identity(request)
    jobs = AlphaFactorJobPersistence()
    try:
        first_completed = await jobs.first_completed_backtest_at(
            [factor_id], tenant_id=tenant_id, user_id=user_id
        )
        events = await persistence.list_factor_lifecycle_events(
            factor_id,
            tenant_id=tenant_id,
            user_id=user_id,
            limit=limit,
            before_event_id=before_event_id,
        )
    except Exception as exc:
        logger.warning("因子生命周期历史读取失败", exc_info=True)
        raise HTTPException(status_code=503, detail="因子生命周期历史暂不可用") from exc

    created_at = factor.get("created_at")
    blind_until = blind_until_of(
        created_at=created_at if isinstance(created_at, datetime) else None,
        first_completed_run_at=first_completed.get(factor_id),
    )
    redact = blind_until is not None and utc_now() < blind_until
    data = []
    for event in events:
        item = {
            "event_id": int(event["event_id"]),
            "event_type": event["event_type"],
            "evaluated_at": event["evaluated_at"],
        }
        if redact:
            item["redacted"] = True
            item["eval_status"] = "blind_window"
        else:
            item.update(
                {
                    "factor_revision_no": event.get("factor_revision_no"),
                    "evidence_job_id": event.get("evidence_job_id"),
                    "event_snapshot": event.get("event_snapshot"),
                }
            )
        data.append(item)
    return {
        "code": 200,
        "data": {
            "events": data,
            "next_before_event_id": (
                data[-1]["event_id"] if len(data) == limit else None
            ),
        },
    }


@router.post("/factors/{factor_id}/lifecycle/restore")
async def restore_factor_lifecycle(
    factor_id: str,
    request: Request,
    reason: str = Query(..., min_length=1, max_length=500),
):
    """Clear a downweight override with an owner-attributed restore record."""
    await _require_owned_factor(factor_id, request, for_write=True)
    user_id, tenant_id = get_authenticated_identity(request)
    try:
        lifecycle = await persistence.restore_factor_lifecycle(
            factor_id,
            reason,
            user_id=user_id,
            tenant_id=tenant_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Factor not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("因子生命周期恢复失败", exc_info=True)
        raise HTTPException(status_code=503, detail="因子状态暂未恢复") from exc

    return {
        "code": 200,
        "data": {
            "factor_id": factor_id,
            "lifecycle": lifecycle,
        },
    }


@router.get("/factors/{factor_id}/revisions")
async def list_factor_revisions(
    factor_id: str,
    request: Request,
    limit: int = Query(50, ge=1, le=100),
):
    """列出本人因子不可变代码版本及其内容哈希。"""
    auth_user_id, auth_tenant_id = get_authenticated_identity(request)
    await _require_owned_factor(factor_id, request)
    revisions = await persistence.list_factor_revisions(
        factor_id,
        tenant_id=auth_tenant_id,
        user_id=auth_user_id,
        limit=limit,
    )
    return {"code": 200, "data": {"revisions": revisions, "total": len(revisions)}}


_MARKET_TO_QLIB: dict[str, str] = {
    "a_share": "CN",
    "hong_kong": "HK",
    "us_stock": "US",
    "crypto": "CRYPTO",
    "futures": "FUTURES",
}

_QLIB_NATIVE_UNIVERSES = ("csi300", "csi500", "csi1000", "csi800")


def _detect_factor_kind(factor_code: str) -> str:
    """AST 预检：判断是 Qlib Factor 类还是 RD-Agent 函数式 (calculate_*)。

    不执行因子代码，只解析语法树。
    """
    import ast
    try:
        tree = ast.parse(factor_code)
    except SyntaxError as e:
        raise RuntimeError(f"因子代码语法错误: {e.msg} (line {e.lineno})") from e

    has_class = False
    has_calculate = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            name_lower = node.name.lower()
            if "factor" in name_lower or any(
                isinstance(n, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "name" for t in n.targets
                )
                for n in node.body
            ):
                has_class = True
        elif isinstance(node, ast.FunctionDef) and node.name.startswith("calculate_"):
            has_calculate = True

    if has_calculate:
        return "functional"
    if has_class:
        return "factor_class"
    return "unknown"


def _vectorized_daily_spearman_ic(
    f: "pd.Series", r: "pd.Series"
) -> tuple[float, float, float, float, int]:
    """向量化计算日度 Spearman IC（秩相关 = 秩的 Pearson）。

    不用每日 spearmanr() 调用，全表 groupby 一次算完。
    Returns: (ic_mean, rank_ic_median, icir, rank_icir, observations)

    口径与挖掘阶段 scripts/alpha_agent/run_rd_agent.py:compute_factor_ic 一致：
    ic = 日度秩相关均值，rank_ic = 其中位数，std = 该序列标准差(ddof=1)，
    icir = ic/std，rank_icir = rank_ic/std。
    """
    import numpy as np
    import pandas as pd

    if len(f) < 100 or len(r) < 100:
        return 0.0, 0.0, 0.0, 0.0, 0

    df = pd.DataFrame({"f": f.values, "r": r.values})
    # Qlib MultiIndex: [instrument, datetime]
    df["date"] = f.index.get_level_values(1)
    df = df[np.isfinite(df["f"]) & np.isfinite(df["r"])]
    if len(df) < 100:
        return 0.0, 0.0, 0.0, 0.0, 0

    # 每日 rank
    df["f_rank"] = df.groupby("date")["f"].rank(method="average")
    df["r_rank"] = df.groupby("date")["r"].rank(method="average")

    # 每日去均值（per group, transform 一次算完）
    g = df.groupby("date")[["f_rank", "r_rank"]]
    means = g.transform("mean")
    df["fc"] = df["f_rank"] - means["f_rank"]
    df["rc"] = df["r_rank"] - means["r_rank"]

    # 每日 sum / (n - 1) = 协方差 / 方差
    df["fcr"] = df["fc"] * df["rc"]
    df["fc2"] = df["fc"] ** 2
    df["rc2"] = df["rc"] ** 2
    sums = df.groupby("date")[["fcr", "fc2", "rc2"]].transform("sum")
    counts = df.groupby("date")["fcr"].transform("count")
    n = (counts - 1).clip(lower=1)
    cov = sums["fcr"] / n
    var_f = sums["fc2"] / n
    var_r = sums["rc2"] / n
    denom = np.sqrt(var_f * var_r)
    # 避免除零
    df["corr"] = np.where(denom > 1e-12, cov / np.where(denom > 1e-12, denom, 1.0), np.nan)

    # 每日的 IC
    ic_by_date = df.groupby("date")["corr"].first().dropna()
    ic_by_date = ic_by_date[np.isfinite(ic_by_date)]
    if len(ic_by_date) == 0:
        return 0.0, 0.0, 0.0, 0.0, 0

    ic_mean = float(ic_by_date.mean())
    rank_ic_median = float(ic_by_date.median())
    std = float(ic_by_date.std(ddof=1)) if len(ic_by_date) > 1 else 0.0
    icir = ic_mean / (std + 1e-8)
    rank_icir = rank_ic_median / (std + 1e-8)
    return ic_mean, rank_ic_median, icir, rank_icir, int(len(df))


def _resolve_instruments_for_universe(
    market_upper: str, universe: str
) -> list[str] | str:
    """返回 Qlib instruments 选择。

    A 股: Qlib 原生 (csi300/500/1000/800) 直接用 D.instruments(market=...)
         或 QuantDB 取 (sse50/gem/star/all_a)
    其他市场: Qlib 原生 (csi300/500/...) 不存在 → 用 D.instruments(market="all")
    """
    from qlib.data import D

    if market_upper == "CN":
        custom = _resolve_custom_pool_instruments(universe)
        if custom is not None:
            return custom
        if universe in _QLIB_NATIVE_UNIVERSES:
            return D.instruments(market=universe)
        try:
            from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub
            from backend.shared.stock_utils import StockCodeUtil
            hub = QuantDBDataHub.get_instance()
            universe_df = hub.fetch_universe_stocks(universe or "csi300")
            if universe_df is None or universe_df.empty:
                raise RuntimeError(f"QuantDB returned no constituents for {universe}")
            return sorted({StockCodeUtil.to_prefix(s) for s in universe_df["symbol"].tolist()[:500]})
        except Exception as e:
            logger.warning("QuantDB universe %s unavailable, falling back to csi300: %s", universe, e)
            return D.instruments(market="csi300")
    # 非 CN 市场：Qlib cache 的 instruments/all.txt 是全集，universe 仅作过滤
    # 这里简化: 直接 D.instruments(market="all")
    return D.instruments(market="all")


def _default_backtest_window(market: str = "a_share") -> tuple[str, str]:
    """默认回测窗口：近一年（end=数据最新交易日，start=end 往前一年）。"""
    import pandas as pd

    end_ts = None
    try:
        if market == "a_share":
            from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

            cal = QuantDBDataHub.get_instance().fetch_calendar()
            if cal is not None and not cal.empty:
                for col in ("trade_date", "date", "time", "cal_date", "TradingDate"):
                    if col in cal.columns:
                        end_ts = pd.to_datetime(cal[col]).max()
                        break
    except Exception as exc:
        logger.warning("[alpha-backtest] resolve default window failed: %s", exc)
    if end_ts is None or pd.isna(end_ts):
        end_ts = pd.Timestamp.today().normalize()
    return (end_ts - pd.DateOffset(years=1)).strftime("%Y-%m-%d"), end_ts.strftime("%Y-%m-%d")


async def _run_factor_backtest(
    factor_id: str,
    factor_code: str,
    market: str = "a_share",
    data_source: str = "qlib_bin",
    start_date: str | None = None,
    end_date: str | None = None,
    universe: str | None = "csi300",
) -> None:
    """统一回测入口（多市场 + 数据源可选）。

    Args:
        market: 'a_share' | 'hong_kong' | 'us_stock' | 'crypto' | 'futures'
        data_source: 'qlib_bin' (默认) | 'h5'
    """
    market_upper = _MARKET_TO_QLIB.get(market, "CN")
    _default_start, _default_end = _default_backtest_window(market)
    end = end_date or _default_end
    start = start_date or _default_start

    try:
        kind = _detect_factor_kind(factor_code)
        if kind == "unknown":
            raise RuntimeError("因子代码中未找到可调用的 Factor 类或 calculate_* 函数")

        # H5 路径: 多市场 H5 不全，自动回退
        if data_source == "h5":
            h5_path = _resolve_factor_h5_path_for_market(market)
            if not h5_path:
                logger.warning(
                    "[alpha-backtest] market=%s H5 不可用，自动回退到 Qlib 二进制",
                    market,
                )
                data_source = "qlib_bin"

        # 字段口径对齐：挖掘/IC 阶段用的是 39 字段的 daily_pv.h5，而 Qlib 二进制只有
        # OHLCV+amount+factor。因子若引用了扩展字段（rsi_14/turn_5/netflow_* 等），
        # 走 Qlib 必然 KeyError —— 这里自动切到 H5 数据源；没有 H5 就明确报错。
        if data_source == "qlib_bin":
            h5_only = _h5_only_fields_in(factor_code)
            if h5_only:
                h5_path = _resolve_factor_h5_path_for_market(market)
                if h5_path:
                    logger.info(
                        "[alpha-backtest] %s 引用了 Qlib 二进制没有的字段 %s，"
                        "自动切换到 H5 数据源（%s）",
                        factor_id, h5_only, h5_path,
                    )
                    data_source = "h5"
                else:
                    raise RuntimeError(
                        f"该因子引用了 Qlib 二进制数据源没有的字段 {h5_only}，"
                        f"且市场 {market} 无可用 H5 数据文件；请改用含这些字段的数据源。"
                    )

        if data_source == "qlib_bin":
            await _backtest_via_qlib(
                factor_id, factor_code, kind, market, market_upper, universe, start, end
            )
        else:
            await _backtest_via_h5(
                factor_id, factor_code, kind, universe, start, end, market=market
            )
    except FactorBacktestCancelled:
        logger.info("[alpha-backtest] %s cancelled by user", factor_id)
        try:
            await persistence.update_factor_metrics(
                factor_id,
                status="cancelled",
                metadata={"backtest_error": "cancelled_by_user"},
            )
        except Exception:
            pass
    except Exception as exc:
        logger.exception("[alpha-backtest] %s failed", factor_id)
        tb = getattr(exc, "__traceback__", None)
        tb_text = ""
        if tb:
            import traceback as _tb
            tb_text = "".join(_tb.format_tb(tb))[-1500:]
        err_msg = f"{type(exc).__name__}: {exc}" + (f"\n{tb_text}" if tb_text else "")
        try:
            await persistence.update_factor_metrics(
                factor_id,
                status="failed",
                metadata={"backtest_error": err_msg[-1500:]},
            )
        except Exception:
            pass
    finally:
        _running_backtests.discard(factor_id)
        _backtest_cancelled.discard(factor_id)


async def _backtest_via_qlib(
    factor_id: str,
    factor_code: str,
    kind: str,
    market: str,
    market_upper: str,
    universe: str,
    start: str,
    end: str,
) -> None:
    """Qlib 二进制回测（默认路径，所有 5 个市场支持）。"""
    import numpy as np
    import pandas as pd
    import qlib
    from qlib.data import D
    from backend.shared.qlib_paths import resolve_qlib_provider_uri

    provider_uri = resolve_qlib_provider_uri(market_upper)
    # 幂等 init：qlib.init 多次调用是安全的，第二次会快速返回
    try:
        qlib.init(provider_uri=provider_uri, region="cn" if market_upper in ("CN", "HK", "FUTURES", "CRYPTO") else "us")
    except Exception as e:
        logger.warning("qlib.init(%s) raised: %s", provider_uri, e)

    instruments = _resolve_instruments_for_universe(market_upper, universe)
    fields = ["$open", "$high", "$low", "$close", "$volume", "$factor"]
    df = D.features(instruments, fields, start_time=start, end_time=end, freq="day")
    if df.empty:
        raise RuntimeError(f"Qlib 数据为空: market={market}, instruments={instruments}, provider_uri={provider_uri}")

    logger.info(
        "[alpha-backtest] %s market=%s universe=%s rows=%d cols=%s",
        factor_id, market, universe, len(df), list(df.columns),
    )

    # 计算因子值（LLM 生成/用户提交的代码一律 subprocess 隔离执行，
    # 严禁在 engine 主进程 exec——主进程持有 DB 凭证与全部服务状态）
    if kind == "functional":
        # RD-Agent calculate_* 函数式：用 subprocess 跑（隔离 + 捕获 traceback）
        factor_series = await _run_functional_factor_subprocess(
            factor_id, factor_code, df
        )
    else:
        # Qlib Factor 类：subprocess 逐股计算，主进程读结果 H5
        factor_series = await _run_factor_class_subprocess(factor_id, factor_code, df)

    if factor_series is None or len(factor_series) == 0:
        raise RuntimeError("因子计算无输出，请检查 calculate_* 函数或 Factor 类")

    # 准备收益率（次日收益，Qlib index=(instrument, datetime)，level=0 是股码）
    close = df["$close"]
    fwd_ret = close.groupby(level=0).pct_change().shift(-1)

    # 对齐 (datetime, instrument) MultiIndex
    common_idx = factor_series.index.intersection(fwd_ret.index)
    if len(common_idx) < 100:
        raise RuntimeError(f"因子与价格对齐后数据不足 (共 {len(common_idx)} 行)")
    f = factor_series.loc[common_idx]
    r = fwd_ret.loc[common_idx]
    mask = np.isfinite(f.values) & np.isfinite(r.values)
    if mask.sum() < 100:
        raise RuntimeError("清洗后有效数据 < 100 行")

    f_clean = pd.Series(f.values[mask], index=f.index[mask])
    r_clean = pd.Series(r.values[mask], index=r.index[mask])

    # 向量化 IC
    ic_mean, rank_ic_median, icir, rank_icir, n_obs = _vectorized_daily_spearman_ic(
        f_clean, r_clean
    )
    if n_obs == 0:
        raise RuntimeError("日度 IC 全部为 NaN，因子可能与价格列不匹配")

    # Sharpe / Annual Return / Max Drawdown: 简单 long-top30% 组合
    try:
        df_pair = pd.DataFrame({"f": f_clean, "r": r_clean})
        df_pair["date"] = df_pair.index.get_level_values(1)  # level 1 = datetime
        df_pair["f_rank"] = df_pair.groupby("date")["f"].rank(pct=True)
        # long top 30% 每日收益均值
        longs = df_pair[df_pair["f_rank"] >= 0.7].groupby("date")["r"].mean().dropna()
        if len(longs) > 1:
            daily_ret = longs
            ann_ret = float(daily_ret.mean() * 252)
            sharpe = float(daily_ret.mean() / (daily_ret.std(ddof=1) + 1e-8) * np.sqrt(252))
            cum = (1 + daily_ret).cumprod()
            peak = cum.cummax()
            dd = (peak - cum) / peak
            max_dd = float(dd.max()) if len(dd) else None
        else:
            ann_ret = sharpe = max_dd = None
    except Exception:
        ann_ret = sharpe = max_dd = None

    await persistence.update_factor_metrics(
        factor_id,
        status="completed",
        ic_value=ic_mean,
        rank_ic=rank_ic_median,
        sharpe_ratio=sharpe,
        annual_return=ann_ret,
        max_drawdown=max_dd,
        universe=universe,
        date_range=f"{start}~{end}",
        metadata={
            "data_source": "qlib_bin",
            "market": market,
            "icir": icir,
            "rank_icir": rank_icir,
            "n_obs": n_obs,
        },
    )
    logger.info(
        "[alpha-backtest] %s done market=%s ic=%.4f rank_ic=%.4f icir=%.4f sharpe=%s ann_ret=%s max_dd=%s n=%d",
        factor_id, market, ic_mean, rank_ic_median, icir,
        f"{sharpe:.3f}" if sharpe is not None else "N/A",
        f"{ann_ret:.3f}" if ann_ret is not None else "N/A",
        f"{max_dd:.3f}" if max_dd is not None else "N/A",
        n_obs,
    )


async def _run_functional_factor_subprocess(
    factor_id: str, factor_code: str, df: "pd.DataFrame"
) -> "pd.Series | None":
    """对 RD-Agent calculate_* 函数式因子：用 subprocess 跑（隔离错误），主进程读 result.h5。

    准备 daily_pv.h5 在 /tmp（用现有 df 写入，比 337MB 模板小且数据新）。
    """
    import sys as _sys
    import shutil
    import tempfile
    from pathlib import Path

    # 每次回测独立工作目录：并发回测互不覆盖（此前固定 /tmp/*.h5 会互相踩）
    workdir = _new_backtest_workspace(factor_id)
    h5_path = str(workdir / "daily_pv.h5")
    try:
        # 把 Qlib 拉的 df 写成 H5 给 subprocess 读。
        # Qlib D.features() 的列名带 "$" 前缀 (如 $close, $volume)，
        # 但 RD-Agent 因子代码通常用不带前缀的列名 (如 close, volume)。
        # 同时写入两组列名，兼容两种命名约定。
        df_out = df.copy()
        for col in list(df_out.columns):
            if col.startswith("$"):
                plain = col[1:]
                if plain not in df_out.columns:
                    df_out[plain] = df_out[col]
        df_out.to_hdf(h5_path, key="data", mode="w")
    except Exception as e:
        shutil.rmtree(workdir, ignore_errors=True)
        raise RuntimeError(f"准备 daily_pv.h5 失败: {e}") from e

    tb_path = workdir / "_bt_tb.txt"
    out_path = workdir / "_bt_result.h5"

    script = f"""
import pandas as pd
import numpy as np
import sys, os, tempfile, traceback, shutil

WORKDIR = {str(workdir)!r}
os.chdir(WORKDIR)  # 因子代码用相对路径读 daily_pv.h5 / 写 result.h5
TB = os.path.join(WORKDIR, "_bt_tb.txt")
OUT = os.path.join(WORKDIR, "_bt_result.h5")
try:
    _factor_ns = {{}}
    exec({repr(factor_code)}, _factor_ns)
    _calc_fns = [v for k, v in _factor_ns.items() if k.startswith("calculate_") and callable(v)]
    if not _calc_fns:
        print("NO_CALC_FN"); sys.exit(1)
    _result = _calc_fns[0]()
    # 优先用返回值（DataFrame），否则看 result.h5
    if _result is not None and hasattr(_result, 'to_hdf'):
        _result.to_hdf(OUT, key='data', mode='w')
    elif os.path.exists(os.path.join(WORKDIR, 'result.h5')):
        shutil.move(os.path.join(WORKDIR, 'result.h5'), OUT)
    else:
        # 兜底: 只在本工作目录内找 .h5（排除输入 daily_pv.h5），不再扫描整个 /tmp
        for f in os.listdir(WORKDIR):
            if f.endswith('.h5') and f != 'daily_pv.h5' and not f.startswith('_bt'):
                shutil.move(os.path.join(WORKDIR, f), OUT)
                break
        else:
            print("NO_RESULT_FILE"); sys.exit(1)
    print("FACTOR_DONE")
except Exception as e:
    with open(TB, "w") as f:
        traceback.print_exc(file=f)
    print(f"ERROR: {{e}}")
    sys.exit(1)
"""
    try:
        returncode, stdout, stderr = await _run_subprocess_tracked(
            factor_id, [_sys.executable, "-c", script], timeout=600
        )
        if returncode != 0:
            tb = tb_path.read_text() if tb_path.exists() else ""
            out = stdout + "\n" + stderr
            raise RuntimeError(
                f"因子执行失败 (exit={returncode}): {out[-300:]}\n{tb[-1000:]}"
            )

        # 读 result.h5
        try:
            import pandas as _pd
            result_df = _pd.read_hdf(out_path)
        except Exception as e:
            raise RuntimeError(f"读取 result.h5 失败: {e}") from e

        # 转成 MultiIndex(datetime, instrument) 的 Series
        if isinstance(result_df.index, _pd.MultiIndex) and result_df.index.nlevels >= 2:
            s = result_df.iloc[:, 0]
        else:
            s = result_df.stack()
        s.index.names = ["instrument", "datetime"]
        return s
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def _run_factor_class_subprocess(
    factor_id: str, factor_code: str, df: "pd.DataFrame"
) -> "pd.Series | None":
    """Qlib Factor 类因子：subprocess 隔离执行（逐股调用），主进程只读结果 H5。

    因子代码由 LLM 生成或用户提交，绝不能在 engine 主进程 exec。
    每次运行用独立的临时文件，避免并发回测相互覆盖。
    """
    import uuid

    run_id = uuid.uuid4().hex[:8]
    input_path = f"/tmp/_factor_input_{run_id}.h5"
    out_path = f"/tmp/_factor_result_{run_id}.h5"
    tb_path = f"/tmp/_factor_tb_{run_id}.txt"
    Path(tb_path).unlink(missing_ok=True)
    Path(out_path).unlink(missing_ok=True)

    try:
        import pandas as _pd
        df.to_hdf(input_path, key="data", mode="w")
    except Exception as e:
        raise RuntimeError(f"准备因子输入数据失败: {e}") from e

    script = f"""
import pandas as pd
import numpy as np
import sys, os, traceback

try:
    _factor_ns = {{}}
    exec({repr(factor_code)}, _factor_ns)
    _factor_cls = None
    for _v in _factor_ns.values():
        if isinstance(_v, type) and _v.__module__ == "builtins":
            if getattr(_v, "name", None) or _v.__name__.lower().endswith("factor"):
                _factor_cls = _v
                break
    if _factor_cls is None:
        print("NO_FACTOR_CLASS"); sys.exit(1)
    _df = pd.read_hdf({input_path!r})
    _factor_inst = _factor_cls()
    _pieces = []
    for _code, _sub in _df.groupby(level=0):
        if len(_sub) < 30:
            continue
        try:
            _fv = _factor_inst(_sub.copy())
            _fv_col = _fv.iloc[:, 0] if hasattr(_fv, "iloc") else pd.Series(_fv)
            _pieces.append(pd.Series(_fv_col.values, index=_sub.index, name="f"))
        except Exception:
            continue
    if not _pieces:
        print("NO_PIECES"); sys.exit(1)
    pd.concat(_pieces).to_hdf({out_path!r}, key="data", mode="w")
    print("FACTOR_DONE")
except Exception:
    with open({tb_path!r}, "w") as _f:
        traceback.print_exc(file=_f)
    sys.exit(1)
"""
    returncode, stdout, stderr = await _run_subprocess_tracked(
        factor_id, [sys.executable, "-c", script], timeout=600
    )
    if returncode != 0:
        tb = Path(tb_path).read_text() if Path(tb_path).exists() else ""
        raise RuntimeError(
            f"因子执行失败 (exit={returncode}): {((stdout or '') + stderr)[-300:]}\n{tb[-1000:]}"
        )

    try:
        import pandas as _pd
        result_df = _pd.read_hdf(out_path)
    except Exception as e:
        raise RuntimeError(f"读取因子结果失败: {e}") from e

    if isinstance(result_df, _pd.Series):
        s = result_df
    elif isinstance(result_df.index, _pd.MultiIndex) and result_df.index.nlevels >= 2:
        s = result_df.iloc[:, 0]
    else:
        s = result_df.stack()
    s.index.names = ["instrument", "datetime"]
    return s


async def _backtest_via_h5(
    factor_id: str,
    factor_code: str,
    kind: str,
    universe: str,
    start: str,
    end: str,
    market: str = "a_share",
) -> None:
    """H5 路径（含挖掘同源的扩展字段；仅 A 股 / 美股 / 港股有预生成数据）。"""
    h5_path = _resolve_factor_h5_path(universe, market)
    if not Path(h5_path).exists():
        raise RuntimeError(f"H5 数据文件不存在: {h5_path}，请改用 data_source=qlib_bin")
    # 数据复制到隔离工作目录的动作在 _backtest_functional_factor 内完成
    await _backtest_functional_factor(
        factor_id, factor_code, start, end, universe, market=market
    )


# 各市场的 H5 数据文件候选（按优先级）。A 股首个候选必须与因子挖掘/IC 阶段同源，
# 否则回测会因字段缺失（rsi_14/turn_5/netflow_* 等）而失败。
_H5_PATH_CANDIDATES: dict[str, list[str]] = {
    "a_share": [
        "/data/quantdb/.h5_cache/daily_pv_all.h5",
        "/app/alphaagent/scenarios/qlib/experiment/factor_data_template/daily_pv_all.h5",
        "/app/db/cn_data/daily_pv.h5",
    ],
    "us_stock": ["/app/db/us_data/daily_pv.h5"],
    "hong_kong": ["/app/db/hk_data/daily_pv.h5"],
    "crypto": ["/app/db/crypto_data/5min_pv.h5"],
    "futures": [],  # H5 未生成
}


def _resolve_factor_h5_path_for_market(market: str) -> str | None:
    """按市场找 H5 文件；不存在返回 None。"""
    for p in _H5_PATH_CANDIDATES.get(market, []):
        if Path(p).exists():
            return p
    return None


async def _run_lightweight_backtest(
    factor_id: str,
    factor_code: str,
    start_date: str | None,
    end_date: str | None,
    universe: str | None = "csi300",
) -> None:
    """轻量回测（支持多股票池）"""
    try:
        import numpy as np
        import pandas as pd
        from qlib.data import D

        # 因子识别：优先 Qlib Factor 类；否则 RD-Agent 函数式（calculate_* 返回 DataFrame）。
        # AST 预检，不执行代码。
        kind = _detect_factor_kind(factor_code)
        if kind == "functional":
            await _backtest_functional_factor(factor_id, factor_code, start_date, end_date, universe)
            return
        if kind != "factor_class":
            raise RuntimeError("因子代码中未找到可调用的 Factor 类")

        _default_start, _default_end = _default_backtest_window("a_share")
        end = end_date or _default_end
        start = start_date or _default_start

        # Universes with a native Qlib instruments file can be passed straight through;
        # the rest (sse50, gem, star, all_a) are resolved from QuantDB index weights.
        QLIB_NATIVE_MARKETS = ("csi300", "csi500", "csi1000", "csi800")
        if universe in QLIB_NATIVE_MARKETS:
            instruments = D.instruments(market=universe)
        else:
            from backend.shared.stock_utils import StockCodeUtil

            try:
                from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub
                hub = QuantDBDataHub.get_instance()
                universe_df = hub.fetch_universe_stocks(universe or "csi300")
                if universe_df.empty:
                    raise RuntimeError(f"QuantDB returned no constituents for {universe}")
                # Qlib instrument files use prefix format (SZ000001), not suffix (000001.SZ)
                instruments = sorted(
                    {StockCodeUtil.to_prefix(s) for s in universe_df["symbol"].tolist()[:500]}
                )
            except Exception as e:
                logger.warning(
                    "QuantDB universe %s unavailable, falling back to csi300: %s", universe, e
                )
                instruments = D.instruments(market="csi300")

        fields = ["$open", "$high", "$low", "$close", "$volume", "$factor"]
        df = D.features(instruments, fields, start_time=start, end_time=end, freq="day")
        if df.empty:
            raise RuntimeError("Qlib 数据为空，请检查 QLIB_PROVIDER_URI")

        # 因子计算在 subprocess 内逐股执行，主进程只读结果序列
        factor_series = await _run_factor_class_subprocess(factor_id, factor_code, df)
        if factor_series is None or len(factor_series) == 0:
            raise RuntimeError("因子计算无输出，请检查 Factor 类实现")

        sample_codes = df.index.get_level_values(0).unique()[:50]
        ic_list: list[float] = []
        rank_ic_list: list[float] = []
        ret_list: list[float] = []
        equity_curve: list[float] = [1.0]

        for code in sample_codes:
            sub = df.xs(code, level=0).copy()
            if len(sub) < 30:
                continue
            try:
                fv_col = factor_series.xs(code, level=0)
            except KeyError:
                continue
            if not isinstance(fv_col, pd.Series) or fv_col.dropna().empty:
                continue
            fwd_ret = sub["$close"].pct_change(5).shift(-5)
            paired = pd.concat([fv_col, fwd_ret], axis=1).dropna()
            if len(paired) < 10:
                continue
            # Pearson IC
            ic = paired.iloc[:, 0].corr(paired.iloc[:, 1])
            if np.isfinite(ic):
                ic_list.append(float(ic))
            # Spearman Rank IC
            try:
                from scipy.stats import spearmanr
                rank_ic, _ = spearmanr(paired.iloc[:, 0], paired.iloc[:, 1])
                if np.isfinite(rank_ic):
                    rank_ic_list.append(float(rank_ic))
            except Exception:
                pass
            # Long portfolio return (top 30%)
            cutoff = fv_col.quantile(0.7)
            longs = fwd_ret[fv_col >= cutoff].dropna()
            if len(longs) > 0:
                ret_list.append(float(longs.mean()))
                equity_curve.append(equity_curve[-1] * (1 + longs.mean()))

        if not ic_list:
            raise RuntimeError("所有股票都无法计算 IC，因子可能与数据列不匹配")

        ic_mean = float(np.mean(ic_list))
        rank_ic_mean = float(np.mean(rank_ic_list)) if rank_ic_list else None
        sharpe = (
            float(np.mean(ret_list) / (np.std(ret_list) + 1e-8) * np.sqrt(252))
            if ret_list else None
        )
        annual_return = float(np.mean(ret_list) * 252) if ret_list else None

        # Max drawdown from equity curve
        max_drawdown = None
        if len(equity_curve) > 1:
            peak = equity_curve[0]
            max_dd = 0.0
            for val in equity_curve[1:]:
                if val > peak:
                    peak = val
                dd = (peak - val) / peak if peak > 0 else 0
                if dd > max_dd:
                    max_dd = dd
            max_drawdown = float(max_dd)

        await persistence.update_factor_metrics(
            factor_id,
            status="completed",
            ic_value=ic_mean,
            sharpe_ratio=sharpe,
            annual_return=annual_return,
            max_drawdown=max_drawdown,
            rank_ic=rank_ic_mean,
            universe=universe,
            date_range=f"{start}~{end}",
        )
        logger.info(
            "[alpha-backtest] %s done ic=%.4f rank_ic=%s sharpe=%s max_dd=%s universe=%s",
            factor_id, ic_mean,
            f"{rank_ic_mean:.4f}" if rank_ic_mean is not None else "N/A",
            f"{sharpe:.3f}" if sharpe is not None else "N/A",
            f"{max_drawdown:.3f}" if max_drawdown is not None else "N/A",
            universe,
        )

    except FactorBacktestCancelled:
        logger.info("[alpha-backtest] %s cancelled by user", factor_id)
        try:
            await persistence.update_factor_metrics(
                factor_id,
                status="cancelled",
                metadata={"backtest_error": "cancelled_by_user"},
            )
        except Exception:
            pass
    except Exception as exc:
        logger.exception("[alpha-backtest] %s failed", factor_id)
        try:
            await persistence.update_factor_metrics(
                factor_id,
                status="failed",
                metadata={"backtest_error": str(exc)[:500]},
            )
        except Exception:
            pass
    finally:
        _running_backtests.discard(factor_id)
        _backtest_cancelled.discard(factor_id)


async def _backtest_functional_factor(
    factor_id: str,
    factor_code: str,
    start_date: str | None,
    end_date: str | None,
    universe: str | None = "csi300",
    market: str = "a_share",
) -> None:
    """回测 RD-Agent 函数式因子（calculate_* 返回 DataFrame，读 daily_pv.h5）。

    与 run_rd_agent.py.compute_factor_ic 同一套逻辑：subprocess 执行因子代码
    写 result.h5，再与价格数据对齐算 IC/RankIC/ICIR。
    数据文件必须与挖掘/IC 阶段同源（含 rsi_14/turn_5 等扩展字段）。
    """
    workdir = None
    try:
        import sys as _sys
        from pathlib import Path

        _default_start, _default_end = _default_backtest_window("a_share")
        start = start_date or _default_start
        end = end_date or _default_end

        # 市场 → H5 数据文件（须与挖掘同源；见 _resolve_factor_h5_path_for_market）
        data_path = _resolve_factor_h5_path(universe, market)
        # 每次回测独立工作目录：因子代码用相对路径 daily_pv.h5 读、写 result.h5，
        # 并发回测互不覆盖（此前固定 /tmp 会互相踩，导致状态横跳）
        import shutil
        workdir = _new_backtest_workspace(factor_id)
        try:
            if Path(data_path).exists():
                shutil.copy2(data_path, workdir / "daily_pv.h5")
        except Exception as e:
            raise RuntimeError(f"准备回测数据 {data_path} 失败: {e}") from e

        script = f"""
import pandas as pd
import numpy as np
import sys, os, tempfile, traceback

WORKDIR = {str(workdir)!r}
os.chdir(WORKDIR)  # 因子代码用相对路径读 daily_pv.h5 / 写 result.h5
try:
    # 用 exec 定义因子函数（__name__ != __main__，不触发 main 块），再调用 calculate_* 执行
    _factor_ns = {{}}
    exec({repr(factor_code)}, _factor_ns)
    _calc_fns = [v for k, v in _factor_ns.items() if k.startswith("calculate_") and callable(v)]
    if not _calc_fns:
        print("NO_CALC_FN"); sys.exit(1)
    _calc_fns[0]()
    # 只在本工作目录内找结果文件（不再扫描 /tmp，避免并发时读到别人的结果）
    result_files = [f for f in os.listdir(WORKDIR) if f.endswith('.h5') and 'result' in f.lower()]
    if not result_files:
        result_files = [f for f in os.listdir(WORKDIR) if f.endswith('.h5') and f != 'daily_pv.h5']
    if not result_files:
        print("NO_RESULT_FILE"); sys.exit(1)
    factor_df = pd.read_hdf(os.path.join(WORKDIR, result_files[0]))
    if factor_df.empty:
        print("EMPTY_FACTOR"); sys.exit(1)
    price_df = pd.read_hdf(os.path.join(WORKDIR, 'daily_pv.h5'))
    # 切片到回测窗口（默认近一年，由调用方解析）
    _start = {start!r}
    _end = {end!r}
    if _start or _end:
        _di = price_df.index.get_level_values(0)
        if _start:
            price_df = price_df[_di >= pd.Timestamp(_start)]
            _di = price_df.index.get_level_values(0)
        if _end:
            price_df = price_df[_di <= pd.Timestamp(_end)]
        if price_df.empty:
            print("EMPTY_WINDOW"); sys.exit(1)
    if 'close' in price_df.columns.get_level_values(0):
        close = price_df['close']
    elif '$close' in price_df.columns.get_level_values(0):
        close = price_df['$close']
    else:
        close = price_df.iloc[:, 0]
    returns = close.groupby(level=1).pct_change().shift(-1)
    # 因子结果 → 对齐 (datetime, instrument)
    # factor_df 已是 MultiIndex(datetime,instrument) 单列 → 直接用；
    # 若单 index 则 stack 成 MultiIndex
    if isinstance(factor_df.index, pd.MultiIndex) and factor_df.index.nlevels >= 2:
        factor_values = factor_df.iloc[:, 0]
    else:
        factor_values = factor_df.stack()
    # 统一 index 名（若已有正确的 MultiIndex 则跳过，避免 "Length of names" 报错）
    try:
        factor_values.index.names = ['datetime', 'instrument']
    except Exception:
        pass
    returns.index.names = ['datetime', 'instrument']
    # 统一 instrument 大小写：因子代码可能输出大写（SH600036），而 daily_pv.h5 用小写
    # （sh600036）；不统一会导致交集为空。与挖掘阶段 compute_factor_ic 同口径。
    def _upper_instrument(_s):
        _names = list(_s.index.names)
        if 'instrument' in _names:
            _lvl = _names.index('instrument')
            _lvs = _s.index.levels[_lvl]
            if _lvs.dtype == object:
                _s.index = _s.index.set_levels(_lvs.str.upper(), level=_lvl)
        return _s

    factor_values = _upper_instrument(factor_values)
    returns = _upper_instrument(returns)
    common_idx = factor_values.index.intersection(returns.index)
    if len(common_idx) < 100:
        print("INSUFFICIENT_DATA"); sys.exit(1)
    f = factor_values.loc[common_idx]; r = returns.loc[common_idx]
    mask = np.isfinite(f) & np.isfinite(r)
    f = f[mask]; r = r[mask]
    if len(f) < 100:
        print("INSUFFICIENT_CLEAN_DATA"); sys.exit(1)
    # 日度 Spearman（秩的 Pearson，向量化；口径与挖掘阶段 compute_factor_ic 完全一致）
    df_ic = pd.DataFrame({{'f': f.values, 'r': r.values}})
    df_ic['dt'] = f.index.get_level_values(0)
    df_ic = df_ic[np.isfinite(df_ic['f']) & np.isfinite(df_ic['r'])]
    g = df_ic.groupby('dt')
    df_ic['fr'] = g['f'].rank(method='average')
    df_ic['rr'] = g['r'].rank(method='average')
    g = df_ic.groupby('dt')
    _means = g[['fr', 'rr']].transform('mean')
    df_ic['fc'] = df_ic['fr'] - _means['fr']
    df_ic['rc'] = df_ic['rr'] - _means['rr']
    df_ic['fcr'] = df_ic['fc'] * df_ic['rc']
    df_ic['fc2'] = df_ic['fc'] ** 2
    df_ic['rc2'] = df_ic['rc'] ** 2
    _sums = g[['fcr', 'fc2', 'rc2']].transform('sum')
    _counts = g['fcr'].transform('count')
    _n = (_counts - 1).clip(lower=1)
    _cov = _sums['fcr'] / _n
    _var_f = _sums['fc2'] / _n
    _var_r = _sums['rc2'] / _n
    _denom = np.sqrt(_var_f * _var_r)
    df_ic['corr'] = np.where(_denom > 1e-12, _cov / np.where(_denom > 1e-12, _denom, 1.0), np.nan)
    ic_by_day = g['corr'].first().dropna()
    ic_by_day = ic_by_day[np.isfinite(ic_by_day)]
    if len(ic_by_day) == 0:
        print("NO_IC_VALUES"); sys.exit(1)
    ic = float(ic_by_day.mean())
    rank_ic = float(ic_by_day.median())
    _std = float(ic_by_day.std(ddof=1)) if len(ic_by_day) > 1 else 0.0
    icir = ic / (_std + 1e-8)
    rank_icir = rank_ic / (_std + 1e-8)
    print("IC=%s" % ic); print("RANK_IC=%s" % rank_ic)
    print("ICIR=%s" % icir); print("RANK_ICIR=%s" % rank_icir)
    print("OBSERVATIONS=%s" % len(f))
    # 组合指标（与 Qlib 路径同口径：做多因子前 30% 的等权日收益）
    _pair = pd.DataFrame({{'f': f.values, 'r': r.values}})
    _pair['dt'] = f.index.get_level_values(0)
    _pair = _pair[np.isfinite(_pair['f']) & np.isfinite(_pair['r'])]
    _pair['fr'] = _pair.groupby('dt')['f'].rank(pct=True)
    _longs = _pair[_pair['fr'] >= 0.7].groupby('dt')['r'].mean().dropna()
    if len(_longs) > 1:
        _ann = float(_longs.mean() * 252)
        _sharpe = float(_longs.mean() / (_longs.std(ddof=1) + 1e-8) * np.sqrt(252))
        _cum = (1 + _longs).cumprod()
        _peak = _cum.cummax()
        _dd = (_peak - _cum) / _peak
        _mdd = float(_dd.max()) if len(_dd) else None
        print("ANN_RET=%s" % _ann); print("SHARPE=%s" % _sharpe); print("MAX_DD=%s" % _mdd)
except Exception as e:
    print(f"ERROR: {{e}}")
    traceback.print_exc()
    sys.exit(1)
"""
        # subprocess 执行（登记句柄，支持 cancel kill；to_thread 避免阻塞事件循环）
        returncode, stdout, stderr = await _run_subprocess_tracked(
            factor_id, [_sys.executable, "-c", script], timeout=600
        )
        # 合并 stdout + stderr（因子脚本异常用 stderr 输出 traceback）
        out = stdout + "\n" + stderr

        def _metric(name: str) -> float | None:
            """从子进程输出解析 NAME=<float> 指标（缺失或非法返回 None）。"""
            prefix = f"{name}="
            for line in out.splitlines():
                if line.startswith(prefix):
                    try:
                        value = float(line.split("=", 1)[1])
                    except Exception:
                        return None
                    return value if value == value else None  # 过滤 NaN
            return None

        ic_mean = _metric("IC")
        rank_ic_mean = _metric("RANK_IC")
        icir = _metric("ICIR")
        rank_icir = _metric("RANK_ICIR")
        ann_ret = _metric("ANN_RET")
        sharpe = _metric("SHARPE")
        max_dd = _metric("MAX_DD")

        if ic_mean is None:
            raise RuntimeError(f"因子回测失败: {out[-500:]}")

        await persistence.update_factor_metrics(
            factor_id,
            status="completed",
            ic_value=ic_mean,
            rank_ic=rank_ic_mean,
            icir=icir,
            rank_icir=rank_icir,
            sharpe_ratio=sharpe,
            annual_return=ann_ret,
            max_drawdown=max_dd,
            universe=universe,
            date_range=f"{start}~{end}",
            metadata={"data_source": "h5"},
        )
        logger.info(
            "[alpha-backtest-fn] %s done ic=%.4f rank_ic=%s icir=%s sharpe=%s ann_ret=%s max_dd=%s",
            factor_id, ic_mean,
            f"{rank_ic_mean:.4f}" if rank_ic_mean is not None else "N/A",
            f"{icir:.4f}" if icir is not None else "N/A",
            f"{sharpe:.4f}" if sharpe is not None else "N/A",
            f"{ann_ret:.4f}" if ann_ret is not None else "N/A",
            f"{max_dd:.4f}" if max_dd is not None else "N/A",
        )
    except FactorBacktestCancelled:
        logger.info("[alpha-backtest-fn] %s cancelled by user", factor_id)
        try:
            await persistence.update_factor_metrics(
                factor_id,
                status="cancelled",
                metadata={"backtest_error": "cancelled_by_user"},
            )
        except Exception:
            pass
    except Exception as exc:
        logger.exception("[alpha-backtest-fn] %s failed", factor_id)
        try:
            await persistence.update_factor_metrics(
                factor_id,
                status="failed",
                metadata={"backtest_error": str(exc)[:500]},
            )
        except Exception:
            pass
    finally:
        if workdir is not None:
            import shutil as _shutil

            _shutil.rmtree(workdir, ignore_errors=True)
        _running_backtests.discard(factor_id)
        _backtest_cancelled.discard(factor_id)


# Qlib 二进制数据集（db/qlib_data/features/<symbol>/*.day.bin）实际可提供的字段。
# 只有这 7 个，超出范围的字段在 Qlib 路径下取不到。
_QLIB_BINARY_FIELDS = frozenset(
    {"open", "high", "low", "close", "volume", "amount", "factor"}
)

# 仅存在于 daily_pv.h5（与因子挖掘/IC 阶段同源的富数据，共 39 字段）的扩展字段。
# 因子若引用其中任意字段，走 Qlib 二进制回测必然 KeyError，必须改用 H5 数据源。
_H5_ONLY_FIELDS = frozenset(
    {
        "rsi_14", "macd_hist", "atr_14", "beta_20", "pe_ttm", "pb", "ps_ttm",
        "div_yield", "total_mv", "float_mv", "np_ttm", "turn_5", "turn_20",
        "turn_z_20", "netflow_5", "netflow_20", "mfi_14", "obv_slope",
        "parkinson_20", "bb_width", "bb_pos", "adx_14", "maxdd_20", "bp", "ep",
        "roe", "peg", "np_growth", "chip_profit_20", "idio_vol_20",
        "ind_strength", "concept_hot",
    }
)


def _new_backtest_workspace(factor_id: str) -> Path:
    """为单次回测创建隔离工作目录。

    此前回测固定使用 /tmp/daily_pv.h5、/tmp/_bt_result.h5，且 subprocess chdir 到 /tmp、
    再用 os.listdir('.') 扫描 result 文件；并发回测会互相覆盖输入、并读到**别人**的结果文件，
    表现为同一因子状态在 completed/failed/backtesting 之间反复横跳。
    改为每次回测一个独立目录（参考 _run_factor_class_subprocess 的 run_id 做法）。
    """
    import tempfile

    safe = "".join(c for c in factor_id if c.isalnum() or c in "-_")[:32] or "factor"
    return Path(tempfile.mkdtemp(prefix=f"bt_{safe}_"))


def _h5_only_fields_in(factor_code: str) -> list[str]:
    """找出因子代码引用的、Qlib 二进制数据集没有的字段。

    用于两件事：① 回测时自动选择能提供这些字段的数据源；② 都不可用时给出明确报错，
    而不是抛裸 KeyError: None of [Index(['rsi_14'])] are in the [columns]。
    """
    import re

    return sorted(
        name
        for name in _H5_ONLY_FIELDS
        if re.search(rf"(?<![A-Za-z0-9_$])\$?{name}(?![A-Za-z0-9_])", factor_code)
    )


def _resolve_factor_h5_path(universe: str = "csi300", market: str = "a_share") -> str:
    """解析因子回测用的 H5 数据文件路径。

    必须与因子挖掘/IC 阶段同一份（含 rsi_14/turn_5/netflow_* 等扩展字段），
    否则因子代码读不到字段，回测必然失败。
    """
    resolved = _resolve_factor_h5_path_for_market(market)
    if resolved:
        return resolved
    base = "/app/alphaagent/scenarios/qlib/experiment/factor_data_template/daily_pv_all.h5"
    if Path(base).exists():
        return base
    return "/tmp/daily_pv.h5"


async def _startup_recover_stuck_factors() -> None:
    """模块加载时启动一次性恢复任务：把超时的 backtesting 状态清理为 failed。

    在事件循环里 schedule 一个后台协程，等 3s DB 就绪后执行一次。
    """
    try:
        await asyncio.sleep(3)
        count = await persistence.recover_stuck_factors(max_age_min=15)
        if count:
            logger.info("[alpha-agent startup] recovered %d stuck backtests", count)
    except Exception as e:
        logger.debug("[alpha-agent startup] recovery skipped: %s", e)


# 模块加载时自动注册启动恢复任务（如果事件循环已运行）
try:
    _loop = asyncio.get_running_loop()
    _loop.create_task(_startup_recover_stuck_factors())
except RuntimeError:
    pass  # 事件循环未运行（导入阶段），跳过；下次请求时会懒触发（如果有的话）
