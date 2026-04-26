"""AI Strategy V1 Qlib 生成、任务与远程策略路由"""

import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from textwrap import dedent
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import text

try:
    from backend.shared.database_pool import get_db
except ImportError:
    from shared.database_pool import get_db

from backend.shared.redis_sentinel_client import get_redis_sentinel_client
from ...services.cos_uploader import get_cos_uploader
from ...services.llm_resilience import LLMRateLimitError, get_resilient_llm_router
from ..schemas import (
    GenerateQlibRequest,
    GenerateQlibResponse,
    GenerateQlibTaskStatusResponse,
    GenerateQlibTaskSubmitResponse,
    ImportRemoteRequest,
    ScanRemoteRequest,
)
from .validation import _strip_markdown_fences

logger = logging.getLogger(__name__)
router = APIRouter()

_QLIB_TASK_TTL_SECONDS = int(os.getenv("QLIB_GENERATE_TASK_TTL_SECONDS", "3600"))
_QLIB_TASK_REDIS_PREFIX = os.getenv("QLIB_GENERATE_TASK_REDIS_PREFIX", "quantmind:strategy:generate_qlib:task:").strip()
_qlib_task_lock = asyncio.Lock()
_qlib_tasks: dict[str, dict[str, Any]] = {}


def _normalize_qlib_symbol(symbol: str) -> str:
    code = str(symbol or "").strip()
    if not code:
        return ""
    upper = code.upper()
    if len(upper) == 8 and upper[:2] in {"SH", "SZ", "BJ"}:
        return upper.lower()
    if len(upper) == 9 and "." in upper:
        left, right = upper.split(".", 1)
        if len(left) == 6 and left.isdigit() and right in {"SH", "SZ", "BJ"}:
            return f"{right}{left}".lower()
    if len(upper) == 6 and upper.isdigit():
        if upper.startswith(("6", "9")):
            return f"sh{upper}"
        if upper.startswith(("0", "2", "3")):
            return f"sz{upper}"
        if upper.startswith(("4", "8")):
            return f"bj{upper}"
    return code.lower()


def _normalize_pool_content_for_qlib(pool_text: str) -> str:
    lines: list[str] = []
    for raw_line in str(pool_text or "").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            lines.append(raw_line)
            continue
        if "\t" in stripped:
            code, rest = stripped.split("\t", 1)
            lines.append(f"{_normalize_qlib_symbol(code)}\t{rest}")
        else:
            parts = stripped.split(maxsplit=1)
            if len(parts) == 2:
                lines.append(f"{_normalize_qlib_symbol(parts[0])} {parts[1]}")
            else:
                lines.append(_normalize_qlib_symbol(stripped))
    return "\n".join(lines) + ("\n" if str(pool_text or "").endswith("\n") else "")


def _trace_id(request: Request | None) -> str | None:
    if not request:
        return None
    return (
        getattr(request.state, "trace_id", None)
        or request.headers.get("X-Trace-Id")
        or request.headers.get("X-Request-Id")
    )


async def _cleanup_expired_qlib_tasks() -> None:
    now = datetime.now().timestamp()
    expired_ids = [
        task_id
        for task_id, task in _qlib_tasks.items()
        if (now - float(task.get("updated_at_ts", now))) > _QLIB_TASK_TTL_SECONDS
    ]
    for task_id in expired_ids:
        _qlib_tasks.pop(task_id, None)


def _qlib_task_cache_key(task_id: str) -> str:
    return f"{_QLIB_TASK_REDIS_PREFIX}{task_id}"


async def _save_qlib_task_to_redis(task_id: str, task: dict[str, Any]) -> None:
    def _write() -> None:
        client = get_redis_sentinel_client()
        client.setex(
            _qlib_task_cache_key(task_id),
            _QLIB_TASK_TTL_SECONDS,
            json.dumps(task, ensure_ascii=False).encode("utf-8"),
        )

    try:
        await asyncio.to_thread(_write)
    except Exception as exc:
        logger.warning("save qlib task to redis failed: task_id=%s err=%s", task_id, exc)


async def _load_qlib_task_from_redis(task_id: str) -> dict[str, Any] | None:
    def _read() -> bytes | None:
        client = get_redis_sentinel_client()
        return client.get(_qlib_task_cache_key(task_id), use_slave=False)

    try:
        raw = await asyncio.to_thread(_read)
    except Exception as exc:
        logger.warning("load qlib task from redis failed: task_id=%s err=%s", task_id, exc)
        return None

    if not raw:
        return None
    try:
        if isinstance(raw, bytes):
            return json.loads(raw.decode("utf-8"))
        if isinstance(raw, str):
            return json.loads(raw)
        return None
    except Exception as exc:
        logger.warning("decode qlib task from redis failed: task_id=%s err=%s", task_id, exc)
        return None


async def _save_qlib_task(task_id: str, updates: dict[str, Any]) -> None:
    async with _qlib_task_lock:
        await _cleanup_expired_qlib_tasks()
        task = _qlib_tasks.get(task_id) or await _load_qlib_task_from_redis(task_id) or {}
        task.update(updates)
        task["updated_at_ts"] = datetime.now().timestamp()
        _qlib_tasks[task_id] = task
        await _save_qlib_task_to_redis(task_id, task)


async def _get_qlib_task(task_id: str) -> dict[str, Any] | None:
    async with _qlib_task_lock:
        await _cleanup_expired_qlib_tasks()
        task = _qlib_tasks.get(task_id)
        if not task:
            task = await _load_qlib_task_from_redis(task_id)
            if task:
                _qlib_tasks[task_id] = task
        if not task:
            return None
        return dict(task)


async def _generate_qlib_impl(body: GenerateQlibRequest, trace_id: str | None) -> GenerateQlibResponse:
    try:
        logger.info("generate_qlib started", extra={"trace_id": trace_id})

        def _camel_to_snake(key: str) -> str:
            out: list[str] = []
            for ch in key or "":
                if ch.isupper():
                    out.append("_")
                    out.append(ch.lower())
                else:
                    out.append(ch)
            return "".join(out).lstrip("_")

        def _normalize_keys_to_snake(d: dict[str, Any]) -> dict[str, Any]:
            out: dict[str, Any] = {}
            for k, v in (d or {}).items():
                if k is None:
                    continue
                nk = _camel_to_snake(str(k))
                out[nk] = v
            return out

        def _live_only_risk_config(risk_config: dict[str, Any]) -> dict[str, Any]:
            src = _normalize_keys_to_snake(risk_config or {})
            allow = {
                "max_drawdown",
                "max_position_size",
                "max_positions",
                "rebalance_frequency",
                "stop_loss",
                "take_profit",
                "risk_free_rate",
                "commission",
                "stamp_duty",
                "transfer_fee",
                "slippage",
                "market_detector_enabled",
                "bear_style",
                "bull_style",
                "normal_style",
            }
            return {k: v for k, v in src.items() if k in allow and v is not None}

        def _strip_backtest_keys(d: dict[str, Any]) -> dict[str, Any]:
            drop = {
                "start_date",
                "end_date",
                "benchmark",
                "account",
                "trade_cost",
                "exchange_kwargs",
                "backtest",
                "backtest_config",
            }
            return {k: v for k, v in (d or {}).items() if k not in drop and v is not None}

        def _persist_local_pool_file(pool_text: str, user_id: str) -> str:
            """保存股票池到本地文件"""
            content = _normalize_pool_content_for_qlib(str(pool_text or ""))
            if not content.strip():
                return ""

            local_root = Path(os.getenv("AI_STRATEGY_LOCAL_POOL_ROOT", "/app/user_pools_local"))
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            relative = Path(str(user_id or "default")) / ts / "stock_pool.txt"

            local_path = (local_root / relative).resolve()
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_text(content, encoding="utf-8")
            return str(local_path)

        qp = body.qlib_params or {}
        strategy_type = qp.get("strategy_type", "TopkDropout")
        topk = int(qp.get("topk", 10))
        n_drop = int(qp.get("n_drop", 2))
        long_exposure = float(qp.get("long_exposure", 1.0))
        short_exposure = float(qp.get("short_exposure", 1.0))
        max_weight = float(qp.get("max_weight", 0.05))
        short_topk = int(qp.get("short_topk", topk))

        rebalance_days_raw = qp.get("rebalance_days")
        rebalance_period = qp.get("rebalance_period", "weekly")
        if rebalance_days_raw in (1, 3, 5):
            holding_period = int(rebalance_days_raw)
            rebalance_period = {1: "daily", 3: "every_3_days", 5: "weekly"}.get(holding_period, rebalance_period)
        else:
            holding_period = {"daily": 1, "weekly": 5, "monthly": 21}.get(rebalance_period, 5)

        logger.info(
            "generate_qlib params",
            extra={
                "trace_id": trace_id,
                "strategy_type": strategy_type,
                "topk": topk,
                "n_drop": n_drop,
                "long_exposure": long_exposure,
                "short_exposure": short_exposure,
                "rebalance_period": rebalance_period,
                "rebalance_days": holding_period,
            },
        )

        def _syntax_check(code: str) -> tuple[bool, str]:
            try:
                import ast

                ast.parse(code)
                return True, ""
            except SyntaxError as e:
                return False, f"{e.msg} (line {e.lineno})"
            except Exception as e:  # pragma: no cover
                return False, str(e)

        def _local_repair_prompt(code: str, err: str) -> str:
            return dedent(
                f"""
                下面是一段 Python 代码，存在语法错误：{err}

                请你修复它，要求：
                1. 输出必须是完整的 Python 代码文件
                2. 保持原有功能和结构，尽量少改动
                3. 不要输出 markdown 代码块，不要解释，只输出代码

                待修复代码：
                {code}
                """
            ).strip()

        llm_provider = (os.getenv("LLM_PROVIDER_FORCE") or os.getenv("LLM_PROVIDER") or "qwen").strip().lower()
        llm_router = get_resilient_llm_router()

        # 从用户 Profile 读取 API Key
        user_api_key: str | None = None
        if body.user_id:
            try:
                with get_db() as session:
                    row = session.execute(
                        text("SELECT ai_ide_api_key FROM user_profiles WHERE user_id = :uid"),
                        {"uid": body.user_id},
                    ).fetchone()
                    if row and row[0]:
                        user_api_key = row[0].strip()
            except Exception as e:
                logger.warning("Failed to fetch user API key: %s", e)

        # 获取股票池内容：优先 pool_content，其次从 COS 读取 pool_file_key
        pool_content = body.pool_content or ""
        if not pool_content.strip() and body.pool_file_key:
            try:
                uploader = get_cos_uploader()
                pool_content = await uploader.read_object(object_key=body.pool_file_key) or ""
            except Exception as e:
                logger.warning("Failed to read pool from COS: %s", e)

        # 保存股票池到本地
        pool_file_local = _persist_local_pool_file(pool_content, body.user_id) if pool_content.strip() else ""

        if strategy_type == "long_short_topk":
            strategy_class = "RedisLongShortTopkStrategy"
            module_path = "backend.services.engine.qlib_app.utils.extended_strategies"
        elif strategy_type == "TopkDropout":
            strategy_class = "RedisRecordingStrategy"
            module_path = "backend.services.engine.qlib_app.utils.recording_strategy"
        else:
            strategy_class = "RedisWeightStrategy"
            module_path = "backend.services.engine.qlib_app.utils.recording_strategy"

        n_drop_field = f'"n_drop": {n_drop},' if strategy_type == "TopkDropout" else ""
        ls_fields = ""
        if strategy_type == "long_short_topk":
            ls_fields = dedent(
                f"""
                "long_exposure": {long_exposure},
                "short_exposure": {short_exposure},
                "max_weight": {max_weight},
                "short_topk": {short_topk},
                """
            ).strip()

        prompt = dedent(
            f"""
            请生成一个 QuantMind Qlib 策略 Python 文件，只需包含 STRATEGY_CONFIG 字典和必要的常量，禁止写策略类。

            【平台约定】
            - 策略通过 STRATEGY_CONFIG 字典描述，由平台内部负责实例化
            - STRATEGY_CONFIG 必须包含：class、module_path、kwargs 三个字段
            - class 固定为 "{strategy_class}"
            - module_path 固定为 "{module_path}"
            - kwargs 包含策略参数

            【策略参数】
            - 选股数量 topk: {topk}
            - 每期剔除数 n_drop: {n_drop}（仅 TopkDropout 时有效）
            - 调仓周期 holding_period: {holding_period} 个交易日（对应 {rebalance_period}）
            - 股票池本地文件: {pool_file_local}
            - 选股条件: {body.conditions}
            - 附加说明: {body.custom_notes or "无"}
            - 股票池摘要: {pool_content[:300] if pool_content else "未提供"}

            【禁止事项】
            - 禁止 import 任何模块（包括 qlib、qlib_app 等）
            - 禁止定义任何类
            - 禁止包含回测参数（start_date/end_date/benchmark/slippage）
            - 禁止输出 markdown 代码块，只输出纯 Python 代码

            【标准输出格式（严格按此格式，只改参数值）】
            POOL_FILE = "..."

            STRATEGY_CONFIG = {{
                "class": "{strategy_class}",
                "module_path": "{module_path}",
                "kwargs": {{
                    "signal": "<PRED>",
                    "topk": {topk},
                    {n_drop_field}
                    {ls_fields}
                    "rebalance_days": {holding_period},
                    "pool_file": "...",
                }},
            }}
            """
        ).strip()
        try:
            code, _meta = await asyncio.to_thread(
                llm_router.generate_code, prompt, llm_provider, "simple", user_api_key
            )
            code = _strip_markdown_fences(code)
            if "POOL_FILE" not in code:
                code = f"POOL_FILE = {json.dumps(pool_file_local, ensure_ascii=False)}\n\n" + code
            if "STRATEGY_CONFIG" not in code:
                n_drop_entry = f'"n_drop": {n_drop},' if strategy_type == "TopkDropout" else ""
                code += dedent(
                    f"""

                    STRATEGY_CONFIG = {{
                        "class": "{strategy_class}",
                        "module_path": "{module_path}",
                        "kwargs": {{
                            "signal": "<PRED>",
                            "topk": {topk},
                            {n_drop_entry}
                            {ls_fields}
                            "rebalance_days": {holding_period},
                            "pool_file": {json.dumps(pool_file_local, ensure_ascii=False)},
                        }},
                    }}
                    """
                ).strip()
            ok, err = _syntax_check(code)
            if not ok:
                for _ in range(2):
                    fixed, _meta2 = await asyncio.to_thread(
                        llm_router.generate_code,
                        _local_repair_prompt(code, err),
                        llm_provider,
                        "simple",
                        user_api_key,
                    )
                    code = _strip_markdown_fences(fixed)
                    ok, err = _syntax_check(code)
                    if ok:
                        break
            return GenerateQlibResponse(success=True, code=code)
        except LLMRateLimitError as exc:
            logger.warning("LLM rate limit hit(%s): %s", llm_provider, exc)
            return GenerateQlibResponse(success=False, error=f"LLM限流: {exc}")
        except Exception as exc:
            logger.warning("LLM generate failed(%s): %s", llm_provider, exc)
            raise
    except Exception as e:
        logger.error("Generate qlib failed: %s", e, exc_info=True)
        return GenerateQlibResponse(success=False, error=f"生成失败: {e}")


@router.post("/generate-qlib", response_model=GenerateQlibResponse)
async def generate_qlib(body: GenerateQlibRequest, request: Request):
    return await _generate_qlib_impl(body, _trace_id(request))


@router.post("/generate-qlib/async", response_model=GenerateQlibTaskSubmitResponse)
async def submit_generate_qlib_task(body: GenerateQlibRequest, request: Request):
    auth_user_id = str(getattr(request.state, "user", {}).get("user_id") or "").strip()
    if auth_user_id and auth_user_id != body.user_id:
        raise HTTPException(status_code=403, detail="未授权：user_id 与认证身份不匹配")

    task_id = uuid4().hex
    trace_id = _trace_id(request)
    tenant_id = str(getattr(request.state, "user", {}).get("tenant_id") or "default").strip()

    await _save_qlib_task(
        task_id,
        {
            "task_id": task_id,
            "status": "pending",
            "user_id": body.user_id,
            "tenant_id": tenant_id,
            "trace_id": trace_id,
            "result": None,
            "error": None,
            "created_at": datetime.now().isoformat() + "Z",
        },
    )

    async def _runner() -> None:
        await _save_qlib_task(task_id, {"status": "running"})
        result = await _generate_qlib_impl(body.model_copy(deep=True), trace_id)
        status = "completed" if result.success else "failed"
        await _save_qlib_task(
            task_id,
            {
                "status": status,
                "result": result.model_dump(),
                "error": result.error if not result.success else None,
            },
        )

    asyncio.create_task(_runner())
    return GenerateQlibTaskSubmitResponse(success=True, task_id=task_id, status="pending")


@router.get("/generate-qlib/tasks/{task_id}", response_model=GenerateQlibTaskStatusResponse)
async def get_generate_qlib_task(task_id: str, request: Request):
    task = await _get_qlib_task(task_id)
    if not task:
        return GenerateQlibTaskStatusResponse(
            success=False,
            task_id=task_id,
            status="not_found",
            error="任务不存在或已过期",
        )

    auth_user_id = str(getattr(request.state, "user", {}).get("user_id") or "").strip()
    task_user_id = str(task.get("user_id") or "").strip()
    if auth_user_id and task_user_id and auth_user_id != task_user_id:
        raise HTTPException(status_code=403, detail="未授权：不可访问其他用户任务")

    result_payload = task.get("result")
    parsed_result: GenerateQlibResponse | None = None
    if isinstance(result_payload, dict):
        parsed_result = GenerateQlibResponse(**result_payload)

    return GenerateQlibTaskStatusResponse(
        success=task.get("status") != "failed",
        task_id=task_id,
        status=str(task.get("status") or "pending"),
        result=parsed_result,
        error=task.get("error"),
    )


@router.post("/remote/scan")
async def scan_remote_strategies(body: ScanRemoteRequest):
    """扫描云端未导入的策略文件"""
    try:
        user_id = body.user_id
        uploader = get_cos_uploader(use_mock=False)
        prefix = f"user_strategies/{user_id}/"
        cos_objects = await uploader.list_objects(prefix)

        if not cos_objects:
            return {"success": True, "items": []}

        with get_db() as session:
            rows = session.execute(text("SELECT cos_url FROM user_strategies WHERE user_id = :uid"), {"uid": user_id}).fetchall()
            existing_urls = {r[0] for r in rows if r[0]}

        candidates = []
        for obj in cos_objects:
            key = obj["key"]
            is_imported = any(key in url for url in existing_urls)
            if not is_imported:
                parts = key.split("/")
                strategy_id = parts[2] if len(parts) >= 4 else "unknown"
                candidates.append(
                    {
                        "key": key,
                        "strategy_id": strategy_id,
                        "size": obj["size"],
                        "last_modified": (obj["last_modified"].isoformat() if obj["last_modified"] else None),
                    }
                )

        return {"success": True, "items": candidates}
    except Exception as e:
        logger.error("Scan remote strategies failed: %s", e, exc_info=True)
        return {"success": False, "error": f"扫描失败: {e}"}


@router.post("/remote/import")
async def import_remote_strategies(body: ImportRemoteRequest):
    """批量导入云端策略"""
    try:
        user_id = body.user_id
        uploader = get_cos_uploader(use_mock=False)

        success_count = 0
        failed_count = 0

        for key in body.files:
            try:
                code = await uploader.read_object(object_key=key)
                parts = key.split("/")
                strategy_id = str(uuid4())
                if len(parts) >= 4:
                    maybe_uuid = parts[2]
                    if len(maybe_uuid) == 36:
                        strategy_id = maybe_uuid

                metadata = {
                    "description": f"Imported from cloud scan at {datetime.now().isoformat()}",
                    "tags": ["imported", "remote-scan"],
                    "conditions": {},
                    "stock_pool": {},
                    "style": "custom",
                    "risk_config": {},
                    "is_public": False,
                }

                with get_db() as session:
                    base_url = uploader.base_url
                    if not base_url:
                        base_url = f"https://{uploader.bucket}.cos.{uploader.region}.myqcloud.com"

                    full_url = f"{base_url}/{key}"
                    if uploader.use_mock:
                        full_url = f"file://{uploader.local_storage_path}/{key}"

                    file_size = len(code.encode("utf-8"))
                    code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()

                    exists = session.execute(text("SELECT 1 FROM user_strategies WHERE id = :id"), {"id": strategy_id}).scalar()
                    final_id = strategy_id if not exists else str(uuid4())

                    session.execute(
                        text(
                            """
                            INSERT INTO user_strategies (
                                id, user_id, strategy_name, description,
                                conditions, stock_pool, position_config, style, risk_config,
                                cos_url, file_size, code_hash,
                                qlib_validated, validation_result,
                                tags, is_public, downloads, created_at, updated_at
                            ) VALUES (
                                :id, :user_id, :strategy_name, :description,
                                '{}', '{}', '{}', 'custom', '{}',
                                :cos_url, :file_size, :code_hash,
                                false, '{}',
                                :tags, false, 0, NOW(), NOW()
                            )
                            """
                        ),
                        {
                            "id": final_id,
                            "user_id": user_id,
                            "strategy_name": f"Imported Strategy {final_id[:8]}",
                            "description": metadata["description"],
                            "cos_url": full_url,
                            "file_size": file_size,
                            "code_hash": code_hash,
                            "tags": metadata["tags"],
                        },
                    )
                    session.commit()

                success_count += 1
            except Exception as e:
                logger.error("Import file %s failed: %s", key, e)
                failed_count += 1

        return {
            "success": True,
            "imported": success_count,
            "failed": failed_count,
            "message": f"成功导入 {success_count} 个策略，失败 {failed_count} 个",
        }
    except Exception as e:
        logger.error("Batch import failed: %s", e, exc_info=True)
        return {"success": False, "error": f"导入失败: {e}"}


@router.get("/remote/list")
async def list_remote_strategies(user_id: str):
    try:
        with get_db() as session:
            items = []
            seen_ids = set()

            user_int_id = session.execute(text("SELECT id FROM users WHERE user_id = :uid"), {"uid": user_id}).scalar()
            if user_int_id:
                try:
                    rows = session.execute(
                        text(
                            """
                            SELECT id, name, description, tags, config, created_at, updated_at
                            FROM strategies
                            WHERE user_id = :uid
                            AND status IN ('ACTIVE', 'DRAFT')
                            ORDER BY created_at DESC
                            """
                        ),
                        {"uid": user_int_id},
                    ).fetchall()
                    for r in rows:
                        conf = r[4]
                        if isinstance(conf, str):
                            try:
                                conf = json.loads(conf)
                            except Exception:
                                conf = {}
                        elif conf is None:
                            conf = {}
                        sid = str(r[0])
                        seen_ids.add(sid)
                        code = conf.get("code", "")
                        items.append(
                            {
                                "id": sid,
                                "strategy_name": r[1],
                                "description": r[2],
                                "tags": r[3] or [],
                                "file_size": len(code),
                                "created_at": r[5].isoformat() if r[5] else None,
                                "updated_at": r[6].isoformat() if r[6] else None,
                                "source": "strategy_service",
                            }
                        )
                except Exception as read_new_err:
                    logger.warning("read strategies table failed, fallback to user_strategies: %s", read_new_err)
                    try:
                        session.rollback()
                    except Exception:
                        pass

            old_rows = session.execute(
                text(
                    """
                    SELECT id, strategy_name, description, tags, file_size, created_at, updated_at
                    FROM user_strategies
                    WHERE user_id = :uid
                    ORDER BY created_at DESC
                    """
                ),
                {"uid": user_id},
            ).fetchall()
            for r in old_rows:
                sid = str(r[0])
                if sid in seen_ids:
                    continue
                tags = r[3] or []
                if isinstance(tags, str):
                    try:
                        tags = json.loads(tags)
                    except Exception:
                        tags = []
                items.append(
                    {
                        "id": sid,
                        "strategy_name": r[1],
                        "description": r[2],
                        "tags": tags,
                        "file_size": int(r[4] or 0),
                        "created_at": r[5].isoformat() if r[5] else None,
                        "updated_at": r[6].isoformat() if r[6] else None,
                        "source": "ai_strategy_cloud",
                    }
                )
        return {"success": True, "items": items}
    except Exception as e:
        logger.error("List remote strategies failed: %s", e, exc_info=True)
        return {"success": False, "error": f"列表获取失败: {e}"}


@router.get("/remote/{strategy_id}")
async def get_remote_strategy(strategy_id: str, user_id: str | None = None):
    try:
        with get_db() as session:
            row = None
            try:
                sid = int(strategy_id)
                row = session.execute(
                    text(
                        """
                        SELECT s.id, s.user_id, s.name, s.description, s.tags, s.config, u.user_id as user_uuid
                        FROM strategies s
                        JOIN users u ON s.user_id = u.id
                        WHERE s.id = :id
                        """
                    ),
                    {"id": sid},
                ).fetchone()
            except ValueError:
                row = None

            if row:
                if user_id and row[6] != user_id:
                    return {"success": False, "error": "无权限访问该策略"}
                conf = row[5]
                if isinstance(conf, str):
                    try:
                        conf = json.loads(conf)
                    except Exception:
                        conf = {}
                elif conf is None:
                    conf = {}
                code = conf.get("code", "")
                return {
                    "success": True,
                    "item": {
                        "id": str(row[0]),
                        "user_id": str(row[6]),
                        "strategy_name": row[2],
                        "description": row[3],
                        "tags": row[4] or [],
                        "code": code,
                        "source": "strategy_service",
                    },
                }

            old_row = session.execute(
                text(
                    """
                    SELECT id, user_id, strategy_name, description, tags, cos_url
                    FROM user_strategies
                    WHERE id = :id
                    """
                ),
                {"id": strategy_id},
            ).fetchone()
            if not old_row:
                return {"success": False, "error": "策略不存在"}
            if user_id and str(old_row[1]) != str(user_id):
                return {"success": False, "error": "无权限访问该策略"}
            code = ""
            if old_row[5]:
                try:
                    uploader = get_cos_uploader()
                    code = await uploader.read_object(object_key=old_row[5].split("/")[-1])
                except Exception:
                    code = ""
            tags = old_row[4] or []
            if isinstance(tags, str):
                try:
                    tags = json.loads(tags)
                except Exception:
                    tags = []
            return {
                "success": True,
                "item": {
                    "id": str(old_row[0]),
                    "user_id": str(old_row[1]),
                    "strategy_name": old_row[2],
                    "description": old_row[3],
                    "tags": tags,
                    "code": code,
                    "source": "ai_strategy_cloud",
                },
            }
    except Exception as e:
        logger.error("Get remote strategy failed: %s", e, exc_info=True)
        return {"success": False, "error": f"获取失败: {e}"}
