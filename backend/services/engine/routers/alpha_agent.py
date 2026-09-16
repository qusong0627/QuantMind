"""AlphaAgent / RD-Agent 因子挖掘 REST API

支持多市场因子挖掘: A股、加密货币、港股、美股
"""

import asyncio
import logging
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Query, Request

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
        return {
            "code": 200,
            "data": {
                "explanation": metadata["explanation"],
                "logic_score": metadata.get("logic_score"),
                "cached": True,
            },
        }

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

请直接输出解释，不要重复因子公式。
解释正文写完后，另起一行输出一行机器可读评分（不要解释这行）：
SCORE: 50 到 100 的整数（50-70 逻辑牵强/易过拟合，70-85 逻辑合理，85-100 经济学依据扎实）"""

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

    # 解析金融逻辑评分（LLM 可能不按格式输出 → 评分缺失但不影响解释文本）
    explanation, logic_score = _parse_logic_score(explanation)

    # Store explanation in metadata
    metadata["explanation"] = explanation
    if logic_score is not None:
        metadata["logic_score"] = logic_score
    await persistence.update_factor_metrics(factor_id, metadata=metadata)

    return {"code": 200, "data": {"explanation": explanation, "logic_score": logic_score, "cached": False}}


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

    # 质量闸门（软）：PFS 扰动保真度 / LLM 金融逻辑分低于阈值时给出显式警告。
    # 不阻断导出（研究流程需人工判断），但警告随响应与文件头一并交付。
    quality = meta.get("quality") or {}
    pfs_val = quality.get("pfs")
    logic_score = meta.get("logic_score")
    quality_warnings = _quality_warnings(pfs_val, logic_score)
    if quality_warnings:
        logger.warning("[alpha-export] %s quality warnings: %s", factor_id, "; ".join(quality_warnings))

    # 生成带头部注释的完整 Python 文件
    header_lines = [
        '"""',
        f'Factor: {factor_name}',
        'Source: RD-Agent Alpha Research',
        f'IC: {factor.get("ic_value", "N/A")}',
        f'RankIC: {meta.get("rank_ic", "N/A")}',
        f'Sharpe: {factor.get("sharpe_ratio", "N/A")}',
        f'Market: {meta.get("market", "a_share")}',
        f'PFS (perturbation fidelity): {f"{float(pfs_val):.3f}" if pfs_val is not None else "N/A"}',
        f'LogicScore: {logic_score if logic_score is not None else "N/A"}',
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
            "quality": {"pfs": pfs_val, "logic_score": logic_score},
            "quality_warnings": quality_warnings,
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


_MARKET_TO_QLIB: dict[str, str] = {
    "a_share": "CN",
    "hong_kong": "HK",
    "us_stock": "US",
    "crypto": "CRYPTO",
    "futures": "FUTURES",
}

_QLIB_NATIVE_UNIVERSES = ("csi300", "csi500", "csi1000", "csi800")


def _compute_pfs_quality(df: "pd.DataFrame") -> dict | None:
    """扰动保真度（PFS）：从 (trade_date, symbol, factor) 面板算因子的排名稳健性。

    实现来自 docker/training/data/factor_quality.py（经 backend.shared.factor_quality
    按路径加载，与训练侧筛选同一公式，口径不漂移）；模块不可用/样本不足返回 None。
    低于 0.9 = 截面 z 分加噪后排名明显塌陷（数据误差/离散化敏感），不宜实盘。
    """
    try:
        from backend.shared.factor_quality import load_factor_quality

        fq = load_factor_quality()
        if fq is None or df is None or df.empty:
            return None
        out = fq.compute_pfs(df, ["factor"]).get("factor") or {}
        if out.get("pfs") is None:
            return None
        return {
            "pfs": round(float(out["pfs"]), 4),
            "pfs_gauss": round(float(out["pfs_gauss"]), 4) if out.get("pfs_gauss") is not None else None,
            "pfs_t": round(float(out["pfs_t"]), 4) if out.get("pfs_t") is not None else None,
            "n_days": int(out.get("n_days") or 0),
        }
    except Exception as exc:  # noqa: BLE001 — 质量度量失败不影响回测结果
        logger.warning("[alpha-backtest] PFS computation failed: %s", exc)
        return None


def _quality_warnings(pfs: float | None, logic_score: int | None) -> list[str]:
    """导出前的质量闸门（软）：PFS / 金融逻辑分低于阈值时返回警告文案。"""
    warnings: list[str] = []
    if pfs is not None and float(pfs) < 0.9:
        warnings.append(
            f"扰动保真度偏低（PFS={float(pfs):.3f} < 0.9）：截面加噪后排名易塌，实盘换手不稳"
        )
    if logic_score is not None and int(logic_score) < 60:
        warnings.append(
            f"金融逻辑评分偏低（logic_score={int(logic_score)} < 60）：建议人工复核经济含义"
        )
    return warnings


def _parse_logic_score(text: str) -> tuple[str, int | None]:
    """从 LLM 解释文本中抽出 ``SCORE: <50-100>`` 评分行。

    返回 (去掉评分行的解释正文, 评分|None)。LLM 可能不按格式输出或给出越界值：
    没有匹配 → 原文 + None；越界一律钳制到 [0, 100]。取最后一个匹配（正文里
    若引用了评分说明，以最后的行为准）。
    """
    score: int | None = None
    match = None
    for match in re.finditer(r"SCORE\s*[:：]\s*(\d{1,3})", text):
        pass  # 取最后一个匹配
    if match is not None:
        score = max(0, min(100, int(match.group(1))))
        text = (text[: match.start()] + text[match.end():]).strip()
    return text, score


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


def _vectorized_daily_spearman_ic(f: "pd.Series", r: "pd.Series") -> tuple[float, float, float, int]:
    """向量化计算日度 Spearman IC（秩相关 = 秩的 Pearson）。

    不用每日 spearmanr() 调用，全表 groupby 一次算完。
    Returns: (ic_mean, rank_ic_median, icir, observations)
    """
    import numpy as np
    import pandas as pd

    if len(f) < 100 or len(r) < 100:
        return 0.0, 0.0, 0.0, 0

    df = pd.DataFrame({"f": f.values, "r": r.values})
    # Qlib MultiIndex: [instrument, datetime]
    df["date"] = f.index.get_level_values(1)
    df = df[np.isfinite(df["f"]) & np.isfinite(df["r"])]
    if len(df) < 100:
        return 0.0, 0.0, 0.0, 0

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
        return 0.0, 0.0, 0.0, 0

    ic_mean = float(ic_by_date.mean())
    rank_ic_median = float(ic_by_date.median())
    std = float(ic_by_date.std(ddof=1)) if len(ic_by_date) > 1 else 0.0
    icir = ic_mean / (std + 1e-8)
    return ic_mean, rank_ic_median, icir, int(len(df))


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

        if data_source == "qlib_bin":
            await _backtest_via_qlib(
                factor_id, factor_code, kind, market, market_upper, universe, start, end
            )
        else:
            await _backtest_via_h5(
                factor_id, factor_code, kind, universe, start, end
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
    ic_mean, rank_ic_median, icir, n_obs = _vectorized_daily_spearman_ic(f_clean, r_clean)
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

    # 质量闸门：扰动保真度（PFS）——同一批因子值上直接算，标注进 metadata，
    # 因子列表/详情原样返回（前端可据此过滤；低于阈值时导出会带质量警告）
    pfs_quality = _compute_pfs_quality(pd.DataFrame({
        "trade_date": f_clean.index.get_level_values(1),  # Qlib index=(instrument, datetime)
        "symbol": f_clean.index.get_level_values(0),
        "factor": np.asarray(f_clean, dtype=float),
    }))

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
            "data_source": "qlib_bin", "market": market, "icir": icir, "n_obs": n_obs,
            **({"quality": pfs_quality} if pfs_quality else {}),
        },
    )
    logger.info(
        "[alpha-backtest] %s done market=%s ic=%.4f rank_ic=%.4f icir=%.4f sharpe=%s ann_ret=%s max_dd=%s pfs=%s n=%d",
        factor_id, market, ic_mean, rank_ic_median, icir,
        f"{sharpe:.3f}" if sharpe is not None else "N/A",
        f"{ann_ret:.3f}" if ann_ret is not None else "N/A",
        f"{max_dd:.3f}" if max_dd is not None else "N/A",
        pfs_quality["pfs"] if pfs_quality else "N/A",
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

    h5_path = "/tmp/daily_pv.h5"
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
        raise RuntimeError(f"准备 daily_pv.h5 失败: {e}") from e

    tb_path = "/tmp/_bt_tb.txt"
    Path(tb_path).unlink(missing_ok=True)
    out_path = "/tmp/_bt_result.h5"
    Path(out_path).unlink(missing_ok=True)
    Path("/tmp/result.h5").unlink(missing_ok=True)

    script = f"""
import pandas as pd
import numpy as np
import sys, os, tempfile, traceback, shutil

os.chdir(tempfile.gettempdir())
TB = "/tmp/_bt_tb.txt"
OUT = "/tmp/_bt_result.h5"
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
    elif os.path.exists('result.h5'):
        shutil.move('result.h5', OUT)
    else:
        # 兜底: 找 cwd 下所有 .h5（排除 daily_pv）
        for f in os.listdir('.'):
            if f.endswith('.h5') and f != 'daily_pv.h5' and not f.startswith('_bt'):
                shutil.move(f, OUT)
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
    returncode, stdout, stderr = await _run_subprocess_tracked(
        factor_id, [_sys.executable, "-c", script], timeout=600
    )
    if returncode != 0:
        tb = Path(tb_path).read_text() if Path(tb_path).exists() else ""
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
) -> None:
    """H5 路径（仅 A 股 / 美股 / 港股支持；其他市场回退）。"""
    h5_path = _resolve_factor_h5_path(universe)
    if not Path(h5_path).exists():
        raise RuntimeError(f"H5 数据文件不存在: {h5_path}，请改用 data_source=qlib_bin")
    # 复制到 /tmp 让因子代码能相对路径读
    import shutil
    tmp_h5 = "/tmp/daily_pv.h5"
    if not Path(tmp_h5).exists() or Path(tmp_h5).stat().st_mtime < Path(h5_path).stat().st_mtime:
        shutil.copy2(h5_path, tmp_h5)
    await _backtest_functional_factor(factor_id, factor_code, start, end, universe)


def _resolve_factor_h5_path_for_market(market: str) -> str | None:
    """按市场找 H5 文件；不存在返回 None。"""
    candidates = {
        "a_share": [
            "/app/alphaagent/scenarios/qlib/experiment/factor_data_template/daily_pv_all.h5",
            "/app/db/cn_data/daily_pv.h5",
        ],
        "us_stock": ["/app/db/us_data/daily_pv.h5"],
        "hong_kong": ["/app/db/hk_data/daily_pv.h5"],
        "crypto": ["/app/db/crypto_data/5min_pv.h5"],
        "futures": [],  # H5 未生成
    }
    for p in candidates.get(market, []):
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
) -> None:
    """回测 RD-Agent 函数式因子（calculate_* 返回 DataFrame，读 daily_pv.h5）。

    与 run_rd_agent.py.compute_factor_ic 同一套逻辑：subprocess 执行因子代码
    写 result.h5，再与价格数据对齐算 IC/RankIC/ICIR。
    """
    try:
        import tempfile
        import sys as _sys
        from pathlib import Path

        _default_start, _default_end = _default_backtest_window("a_share")
        start = start_date or _default_start
        end = end_date or _default_end

        # 市场 → H5 数据文件（因子代码读 daily_pv.h5，subprocess chdir 到 /tmp）
        data_path = _resolve_factor_h5_path(universe)
        # 复制到 /tmp/daily_pv.h5，因子代码用相对路径 daily_pv.h5 能读到
        import shutil
        tmp_h5 = "/tmp/daily_pv.h5"
        try:
            if Path(data_path).exists() and (
                not Path(tmp_h5).exists()
                or Path(tmp_h5).stat().st_mtime < Path(data_path).stat().st_mtime
            ):
                shutil.copy2(data_path, tmp_h5)
        except Exception:
            pass

        script = f"""
import pandas as pd
import numpy as np
import sys, os, tempfile, traceback

os.chdir(tempfile.gettempdir())
try:
    # 用 exec 定义因子函数（__name__ != __main__，不触发 main 块），再调用 calculate_* 执行
    _factor_ns = {{}}
    exec({repr(factor_code)}, _factor_ns)
    _calc_fns = [v for k, v in _factor_ns.items() if k.startswith("calculate_") and callable(v)]
    if not _calc_fns:
        print("NO_CALC_FN"); sys.exit(1)
    _calc_fns[0]()
    result_files = [f for f in os.listdir('.') if f.endswith('.h5') and 'result' in f.lower()]
    if not result_files:
        result_files = [f for f in os.listdir('.') if f.endswith('.h5') and f != 'daily_pv.h5']
    if not result_files:
        print("NO_RESULT_FILE"); sys.exit(1)
    factor_df = pd.read_hdf(result_files[0])
    if factor_df.empty:
        print("EMPTY_FACTOR"); sys.exit(1)
    price_df = pd.read_hdf({repr(str(data_path))})
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
    common_idx = factor_values.index.intersection(returns.index)
    if len(common_idx) < 100:
        print("INSUFFICIENT_DATA"); sys.exit(1)
    f = factor_values.loc[common_idx]; r = returns.loc[common_idx]
    mask = np.isfinite(f) & np.isfinite(r)
    f = f[mask]; r = r[mask]
    if len(f) < 100:
        print("INSUFFICIENT_CLEAN_DATA"); sys.exit(1)
    from scipy import stats
    # 向量化逐日 IC（groupby 避免逐日 loc 全表扫描，显著提速）
    df_ic = pd.DataFrame({'f': f, 'r': r})
    df_ic['dt'] = df_ic.index.get_level_values(0)
    ic_values = []
    for dt, g in df_ic.groupby('dt'):
        if len(g) > 5:
            corr, _ = stats.spearmanr(g['f'], g['r'])
            if np.isfinite(corr):
                ic_values.append(corr)
    if not ic_values:
        print("NO_IC_VALUES"); sys.exit(1)
    ic = np.mean(ic_values)
    rank_ic = np.median(ic_values)
    icir = np.mean(ic_values) / (np.std(ic_values) + 1e-8)
    print(f"IC={{ic:.4f}}"); print(f"RANK_IC={{rank_ic:.4f}}")
    print(f"ICIR={{icir:.4f}}"); print(f"OBSERVATIONS={{len(f)}}")
    # 质量闸门：扰动保真度 PFS（与训练侧 data/factor_quality 同一实现；
    # 用 % 格式化避免与外层 f-string 的花括号冲突）
    try:
        from backend.shared.factor_quality import load_factor_quality
        _fq = load_factor_quality()
        if _fq is not None:
            _pfs_df = pd.DataFrame(
                list(zip(f.index.get_level_values(0), f.index.get_level_values(1),
                         np.asarray(f, dtype=float))),
                columns=["trade_date", "symbol", "factor"],
            )
            _q = _fq.compute_pfs(_pfs_df, ["factor"]).get("factor") or {{}}
            if _q.get("pfs") is not None:
                print("PFS=%.4f" % _q["pfs"])
                if _q.get("pfs_gauss") is not None:
                    print("PFS_GAUSS=%.4f" % _q["pfs_gauss"])
                if _q.get("pfs_t") is not None:
                    print("PFS_T=%.4f" % _q["pfs_t"])
                print("PFS_DAYS=%d" % int(_q.get("n_days") or 0))
    except Exception:
        pass
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
        ic_mean = rank_ic_mean = None
        pfs_quality: dict = {}
        for line in out.splitlines():
            if line.startswith("IC="):
                try:
                    ic_mean = float(line.split("=")[1])
                except Exception:
                    pass
            elif line.startswith("RANK_IC="):
                try:
                    rank_ic_mean = float(line.split("=")[1])
                except Exception:
                    pass
            elif line.startswith("PFS_GAUSS="):
                try:
                    pfs_quality["pfs_gauss"] = float(line.split("=")[1])
                except Exception:
                    pass
            elif line.startswith("PFS_T="):
                try:
                    pfs_quality["pfs_t"] = float(line.split("=")[1])
                except Exception:
                    pass
            elif line.startswith("PFS_DAYS="):
                try:
                    pfs_quality["n_days"] = int(line.split("=")[1])
                except Exception:
                    pass
            elif line.startswith("PFS="):
                try:
                    pfs_quality["pfs"] = float(line.split("=")[1])
                except Exception:
                    pass

        if ic_mean is None:
            raise RuntimeError(f"因子回测失败: {out[-500:]}")

        await persistence.update_factor_metrics(
            factor_id,
            status="completed",
            ic_value=ic_mean,
            rank_ic=rank_ic_mean,
            sharpe_ratio=None,
            annual_return=None,
            max_drawdown=None,
            universe=universe,
            date_range=f"{start}~{end}",
            metadata={"data_source": "h5", **({"quality": pfs_quality} if pfs_quality else {})},
        )
        logger.info("[alpha-backtest-fn] %s done ic=%.4f rank_ic=%s pfs=%s", factor_id, ic_mean,
                    f"{rank_ic_mean:.4f}" if rank_ic_mean is not None else "N/A",
                    f"{pfs_quality['pfs']:.4f}" if pfs_quality.get("pfs") is not None else "N/A")
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
        _running_backtests.discard(factor_id)
        _backtest_cancelled.discard(factor_id)


def _resolve_factor_h5_path(universe: str = "csi300") -> str:
    """解析因子回测用 H5 数据文件路径（RD-Agent daily_pv.h5）。"""
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
