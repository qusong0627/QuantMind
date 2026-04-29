"""AI 策略向导 - 路由分发层 (Phase 5 精简版)

5步策略生成流程：
1. 股票池选择 (parse_conditions)
2. 股票池确认 (query_pool)
3. 策略参数设置 (validate_position)
4. 风格配置 (apply_style_config)
5. 策略生成 (generate_strategy)

其余路由（pool 文件管理、远程策略、Qlib 生成/验证/修复、save-to-cloud 等）
保留原始实现。内联 Pydantic 模型已迁移至 api/schemas/。
"""

import asyncio
import hashlib
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from textwrap import dedent
from typing import Any, Dict, List, Optional
from urllib.parse import unquote_plus
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import text

# ---------------------------------------------------------------------------
# 数据库连接
# ---------------------------------------------------------------------------
try:
    from backend.shared.database_pool import get_database_pool, get_db
except ImportError:
    try:
        from shared.database_pool import get_database_pool, get_db
    except ImportError:
        try:
            from backend.shared.strategy_storage import get_db

            def get_database_pool():
                raise ImportError("database_pool not available")

        except ImportError:
            try:
                from shared.strategy_storage import get_db

                def get_database_pool():
                    raise ImportError("database_pool not available")

            except ImportError:

                def get_database_pool():
                    raise ImportError("Cannot find database_pool module")

                def get_db():
                    raise ImportError("Cannot find database_pool module")


from backend.shared.redis_sentinel_client import get_redis_sentinel_client

from ...models.stock_pool_file import StockPoolFile
from ...services.cos_uploader import (
    get_cos_uploader,
)
from ...services.llm_resilience import LLMRateLimitError, get_resilient_llm_router
from ...services.qlib_validator import get_qlib_validator
from ...services.selection.generator import get_sql_generator

# ---------------------------------------------------------------------------
# 服务层依赖
# ---------------------------------------------------------------------------
from ...services.selection.parser import get_intent_parser
from ...services.selection.rule_parser import get_trade_rule_parser

# ---------------------------------------------------------------------------
# Steps 层（Phase 1-4 抽取的业务逻辑）
# ---------------------------------------------------------------------------
from ...steps.step1_stock_selection import (
    LATEST_TABLE,
    _condition_to_dsl,
)
from ...steps.step1_stock_selection import parse_conditions as _step1_parse_conditions
from ...steps.step2_pool_confirmation import (
    _build_full_market_sql,
    _build_pool_summary,
    _get_universe_total,
    _is_full_market_query,
    _is_full_market_sql,
    _require_user_id,
)
from ...steps.step2_pool_confirmation import query_pool as _step2_query_pool
from .validation import router as validation_router
from .storage import router as storage_router
from .generation import router as generation_router

# ---------------------------------------------------------------------------
# Schemas（Phase 1 抽取的 Pydantic 模型）
# ---------------------------------------------------------------------------
from ..schemas import (  # Stock Pool; Backtest; Text Parse; Remote
    BacktestRequest,
    BacktestResponse,
    DeletePoolFileRequest,
    DeletePoolFileResponse,
    GenerateQlibRequest,
    GenerateQlibResponse,
    GenerateQlibTaskStatusResponse,
    GenerateQlibTaskSubmitResponse,
    GetActivePoolFileRequest,
    GetActivePoolFileResponse,
    ImportRemoteRequest,
    ListPoolFilesRequest,
    ListPoolFilesResponse,
    ParseRequest,
    ParseResponse,
    ParseTextRequest,
    ParseTradeRulesRequest,
    PoolFileSummary,
    PoolItem,
    PreviewPoolFileRequest,
    PreviewPoolFileResponse,
    QueryPoolRequest,
    QueryPoolResponse,
    RepairQlibRequest,
    RepairQlibResponse,
    SavePoolFileRequest,
    SavePoolFileResponse,
    SaveToCloudRequest,
    SaveToCloudResponse,
    ScanRemoteRequest,
    ValidateQlibRequest,
    ValidateQlibResponse,
    ValidationCheckResponse,
)

logger = logging.getLogger(__name__)
TOTAL_MV_PER_YI = float(os.getenv("AI_STRATEGY_TOTAL_MV_PER_YI", "10000"))
TOTAL_MV_TO_YUAN = 100000000.0 / TOTAL_MV_PER_YI

router = APIRouter(prefix="/strategy", tags=["strategy-wizard"])
router.include_router(validation_router)
router.include_router(storage_router)
router.include_router(generation_router)

_QLIB_TASK_TTL_SECONDS = int(os.getenv("QLIB_GENERATE_TASK_TTL_SECONDS", "3600"))
_QLIB_TASK_REDIS_PREFIX = os.getenv("QLIB_GENERATE_TASK_REDIS_PREFIX", "quantmind:strategy:generate_qlib:task:").strip()
_qlib_task_lock = asyncio.Lock()
_qlib_tasks: dict[str, dict[str, Any]] = {}


def _strip_markdown_fences(code: str) -> str:
    """将 LLM 返回的 markdown 代码围栏剥离为纯 Python。"""
    if not code:
        return code
    s = code.strip()
    if "```" not in s:
        return s + "\n"
    try:
        m = re.search(r"```(?:python)?\s*(.*?)\s*```", s, flags=re.IGNORECASE | re.DOTALL)
        if m:
            return (m.group(1) or "").strip() + "\n"
    except Exception:
        pass
    lines = [ln for ln in s.splitlines() if not ln.strip().startswith("```")]
    return "\n".join(lines).strip() + "\n"


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


# ============================================================================
#  Step 1: 股票池选择 — 条件解析
# ============================================================================


@router.post("/parse-conditions", response_model=ParseResponse)
async def parse_conditions(body: ParseRequest, request: Request):
    """解析筛选条件为 DSL"""
    try:
        logger.info("parse_conditions started", extra={"trace_id": _trace_id(request)})
        return _step1_parse_conditions(body.conditions)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("parse_conditions failed: %s", e)
        raise HTTPException(status_code=400, detail=f"解析失败: {e}")


# ============================================================================
#  Step 2: 股票池确认 — 执行查询
# ============================================================================


@router.post("/query-pool", response_model=QueryPoolResponse)
async def query_pool(body: QueryPoolRequest, request: Request):
    """执行查询确认股票池"""
    try:
        trace_id = _trace_id(request)
        user_id = _require_user_id(request)
        result = await _step2_query_pool(body.dsl, user_id)

        logger.info(
            f"query_pool executed for user {user_id}",
            extra={"dsl": body.dsl, "count": len(result.items), "trace_id": trace_id},
        )
        return result
    except HTTPException:
        # 透传鉴权/参数错误，避免误报为 500。
        raise
    except Exception as e:
        import traceback

        traceback.print_exc()
        err_text = str(e)
        logger.error(f"query_pool failed: {err_text}")
        if "UndefinedColumn" in err_text or ("column" in err_text.lower() and "does not exist" in err_text.lower()):
            raise HTTPException(
                status_code=422,
                detail="股票池筛选字段与当前数据库结构不匹配，请联系管理员同步字段映射。",
            )
        raise HTTPException(status_code=500, detail=f"查询失败 (AI Strategy): {err_text}")


# ============================================================================
#  文本解析（自然语言 → DSL/SQL）
#  _simple_parse_text / _parse_trade_text 尚未迁移到 steps/，暂保留于路由层
# ============================================================================


def _simple_parse_text(text_input: str):
    """基于正则的本地文本解析（无需 LLM）"""
    s = text_input.replace("：", ":").replace(" ", "")
    s = s.replace("总市值", "市值")
    s = s.replace("<=", "≤").replace(">=", "≥").replace("—", "-")

    factors = []
    suggestions = []
    pe_range = None
    pb_range = None
    cap_range = None
    industry_list: list[str] = []
    local_hit = False
    loose_mode_hits: list[str] = []

    def _sql_quote(val: str) -> str:
        return (val or "").replace("'", "''")

    # total_mv 单位可配置，默认“万元”：1亿=10000。
    # 如数据库存“千元”，可设置 AI_STRATEGY_TOTAL_MV_PER_YI=100000。
    cap_unit_multiplier = 1.0
    if "亿" in s:
        cap_unit_multiplier = TOTAL_MV_PER_YI

    def _cap_to_wanyuan(num_text: str, unit_text: str | None) -> float:
        n = float(num_text)
        u = (unit_text or "").strip()
        if u == "亿":
            return n * TOTAL_MV_PER_YI
        if u == "万":
            return n * (TOTAL_MV_PER_YI / 10000.0)
        return n * cap_unit_multiplier

    m_cap_range = re.search(
        r"(?:总?市值)(?:区间|范围)?(?:在)?(\d+(?:\.\d+)?)(亿|万)?(?:到|至|~|-|—|和)(\d+(?:\.\d+)?)(亿|万)?(?:之间|区间|范围内|以内)?",
        s,
    )
    if m_cap_range:
        low_val = _cap_to_wanyuan(m_cap_range.group(1), m_cap_range.group(2))
        high_val = _cap_to_wanyuan(m_cap_range.group(3), m_cap_range.group(4))
        if low_val > high_val:
            low_val, high_val = high_val, low_val
            loose_mode_hits.append("市值区间已自动纠正为从小到大")
        cap_range = (low_val, high_val)
        factors.append("market_cap")
        local_hit = True
    else:
        m_cap = re.search(
            r"(?:总?市值)(?:[≥>=]|大于等于|不少于|不低于|不小于|大于|高于|超过|以上|至少)(\d+(?:\.\d+)?)(亿|万)?",
            s,
        )
        if m_cap:
            cap_range = (_cap_to_wanyuan(m_cap.group(1), m_cap.group(2)), None)
            factors.append("market_cap")
            local_hit = True
        else:
            m_cap_rev = re.search(
                r"(?:总?市值)(?:[≤<=]|小于等于|不高于|不超过|不大于|小于|低于|以下|以内|至多)(\d+(?:\.\d+)?)(亿|万)?",
                s,
            )
            if m_cap_rev:
                cap_range = (None, _cap_to_wanyuan(m_cap_rev.group(1), m_cap_rev.group(2)))
                factors.append("market_cap")
                local_hit = True
            else:
                m_cap_approx = re.search(
                    r"(?:总?市值)(?:约|大约|大概|大致)?(\d+(?:\.\d+)?)(亿|万)?(?:左右|附近)?",
                    s,
                )
                if m_cap_approx:
                    center = _cap_to_wanyuan(m_cap_approx.group(1), m_cap_approx.group(2))
                    # 宽松查询：近似值按 ±20% 区间处理
                    cap_range = (center * 0.8, center * 1.2)
                    factors.append("market_cap")
                    local_hit = True
                    loose_mode_hits.append("市值“约/左右”已按±20%宽松区间处理")

    if cap_range is None and (("小市值" in s) or ("小盘" in s) or ("微盘" in s)):
        # 默认小市值阈值：500亿（按 total_mv 口径）
        cap_range = (None, 500.0 * TOTAL_MV_PER_YI)
        factors.append("market_cap")
        local_hit = True
        suggestions.append("已按“小市值≤500亿”默认阈值处理，可手动指定市值范围提升精度")

    m_pe_range = re.search(
        r"(?:PE|市盈率)(?:区间|范围)?(?:在|是)?(\d+(?:\.\d+)?)(?:到|至|~|-|—|和)(\d+(?:\.\d+)?)(?:之间|区间|范围内|以内)?",
        s,
        re.IGNORECASE,
    )
    if m_pe_range:
        pe_low = float(m_pe_range.group(1))
        pe_high = float(m_pe_range.group(2))
        if pe_low > pe_high:
            pe_low, pe_high = pe_high, pe_low
            loose_mode_hits.append("PE区间已自动纠正为从小到大")
        pe_range = (pe_low, pe_high)
        factors.append("pe")
        local_hit = True
    else:
        m_pe = (
            re.search(r"PE[≤<=](\d+(?:\.\d+)?)", s, re.IGNORECASE)
            or re.search(r"市盈率[≤<=](\d+(?:\.\d+)?)", s)
            or re.search(
                r"(?:PE|市盈率)(?:小于等于|不高于|不超过|不大于|小于|低于|以下|以内|至多)(\d+(?:\.\d+)?)",
                s,
                re.IGNORECASE,
            )
        )
        if m_pe:
            pe_range = (None, float(m_pe.group(1)))
            factors.append("pe")
            local_hit = True
        else:
            m_pe_ge = re.search(
                r"(?:PE|市盈率)(?:大于等于|不少于|不低于|不小于|大于|高于|超过|以上|至少)(\d+(?:\.\d+)?)",
                s,
                re.IGNORECASE,
            )
            if m_pe_ge:
                pe_range = (float(m_pe_ge.group(1)), None)
                factors.append("pe")
                local_hit = True
            else:
                m_pe_approx = re.search(
                    r"(?:PE|市盈率)(?:约|大约|大概|大致)?(\d+(?:\.\d+)?)(?:左右|附近)?",
                    s,
                    re.IGNORECASE,
                )
                if m_pe_approx:
                    center = float(m_pe_approx.group(1))
                    pe_range = (center * 0.8, center * 1.2)
                    factors.append("pe")
                    local_hit = True
                    loose_mode_hits.append("PE“约/左右”已按±20%宽松区间处理")

    m_pb_range = re.search(
        r"(?:PB|市净率)(?:区间|范围)?(?:在)?(\d+(?:\.\d+)?)(?:到|至|~|-|—|和)(\d+(?:\.\d+)?)(?:之间|区间|范围内|以内)?",
        s,
        re.IGNORECASE,
    )
    if m_pb_range:
        pb_low = float(m_pb_range.group(1))
        pb_high = float(m_pb_range.group(2))
        if pb_low > pb_high:
            pb_low, pb_high = pb_high, pb_low
            loose_mode_hits.append("PB区间已自动纠正为从小到大")
        pb_range = (pb_low, pb_high)
        factors.append("pb")
        local_hit = True
    else:
        m_pb = (
            re.search(r"PB[≤<=](\d+(?:\.\d+)?)", s, re.IGNORECASE)
            or re.search(r"市净率[≤<=](\d+(?:\.\d+)?)", s)
            or re.search(
                r"(?:PB|市净率)(?:小于等于|不高于|不超过|不大于|小于|低于|以下|以内|至多)(\d+(?:\.\d+)?)",
                s,
                re.IGNORECASE,
            )
        )
        if m_pb:
            pb_range = (None, float(m_pb.group(1)))
            factors.append("pb")
            local_hit = True
        else:
            m_pb_ge = re.search(
                r"(?:PB|市净率)(?:大于等于|不少于|不低于|不小于|大于|高于|超过|以上|至少)(\d+(?:\.\d+)?)",
                s,
                re.IGNORECASE,
            )
            if m_pb_ge:
                pb_range = (float(m_pb_ge.group(1)), None)
                factors.append("pb")
                local_hit = True
            else:
                m_pb_approx = re.search(
                    r"(?:PB|市净率)(?:约|大约|大概|大致)?(\d+(?:\.\d+)?)(?:左右|附近)?",
                    s,
                    re.IGNORECASE,
                )
                if m_pb_approx:
                    center = float(m_pb_approx.group(1))
                    pb_range = (center * 0.8, center * 1.2)
                    factors.append("pb")
                    local_hit = True
                    loose_mode_hits.append("PB“约/左右”已按±20%宽松区间处理")

    if any(k in s for k in ("金融股", "金融板块", "金融行业", "券商股", "证券股", "银行股", "保险股")):
        # 兼容当前库内行业值（仅有 industry 一列，常见为细分行业名）
        industry_list = ["金融", "银行", "保险", "证券"]
        factors.append("industry")
        local_hit = True
        suggestions.append("已按金融股解析为金融相关行业（含金融信息服务）")
    else:
        inds = re.findall(r"行业[:：]\s*([\u4e00-\u9fa5,，\s]+)", s)
        if inds:
            industry_list = [x.strip() for x in re.split(r"[,，\s]+", inds[0]) if x.strip()]
            if industry_list:
                factors.append("industry")
                local_hit = True
        if not industry_list:
            # 宽松行业识别：支持“XX股/XX板块/XX行业/XX概念”
            coarse_terms = re.findall(r"([\u4e00-\u9fa5]{2,8})(?:股|板块|行业|概念)", s)
            industry_stopwords = {
                "小市值",
                "大市值",
                "中小市值",
                "沪深300",
                "中证1000",
                "全市场",
                "A股",
                "股票",
                "非ST",
                "ST",
                "总市值",
                "市值",
                "市盈率",
                "市净率",
                "成分",
                "成分股",
            }
            for term in coarse_terms:
                t = term.strip()
                if not t or t in industry_stopwords:
                    continue
                if t not in industry_list:
                    industry_list.append(t)
            if industry_list:
                factors.append("industry")
                local_hit = True
                loose_mode_hits.append("已启用“XX股/XX板块”宽松行业匹配")

    is_st_flag = None
    if re.search(r"非ST|去除ST|排除ST|不含ST", s, re.IGNORECASE):
        is_st_flag = 0
    elif re.search(r"\bST\b|^ST|\\*ST", s, re.IGNORECASE):
        is_st_flag = 1
    if is_st_flag is not None:
        factors.append("is_st")
        local_hit = True

    hs300_flag = None
    csi1000_flag = None
    if "沪深300" in s or "HS300" in s.upper():
        hs300_flag = 1
        factors.append("is_csi300")
        local_hit = True
    if "中证1000" in s or "CSI1000" in s.upper():
        csi1000_flag = 1
        factors.append("is_csi1000")
        local_hit = True

    parts = []
    if cap_range:
        low, high = cap_range
        if low is not None and high is not None:
            parts.append(f"market_cap >= {low} AND market_cap <= {high}")
        elif low is not None:
            parts.append(f"market_cap >= {low}")
        elif high is not None:
            parts.append(f"market_cap <= {high}")

    if pe_range:
        low, high = pe_range
        if low is not None and high is not None:
            parts.append(f"pe >= {low} AND pe <= {high}")
        elif low is not None:
            parts.append(f"pe >= {low}")
        elif high is not None:
            parts.append(f"pe <= {high}")

    if pb_range:
        low, high = pb_range
        if low is not None and high is not None:
            parts.append(f"pb >= {low} AND pb <= {high}")
        elif low is not None:
            parts.append(f"pb >= {low}")
        elif high is not None:
            parts.append(f"pb <= {high}")

    if is_st_flag is not None:
        parts.append(f"is_st == {is_st_flag}")
    if hs300_flag is not None:
        parts.append("is_csi300 == 1")
    if csi1000_flag is not None:
        parts.append("is_csi1000 == 1")
    if industry_list:
        sql_parts: list[str] = []
        if cap_range:
            low, high = cap_range
            if low is not None:
                sql_parts.append(f"total_mv >= {low}")
            if high is not None:
                sql_parts.append(f"total_mv <= {high}")
        if pe_range:
            low, high = pe_range
            if low is not None:
                sql_parts.append(f"pe_ttm >= {low}")
            if high is not None:
                sql_parts.append(f"pe_ttm <= {high}")
        if pb_range:
            low, high = pb_range
            if low is not None:
                sql_parts.append(f"pb >= {low}")
            if high is not None:
                sql_parts.append(f"pb <= {high}")
        if is_st_flag is not None:
            sql_parts.append(f"is_st = {is_st_flag}")
        if hs300_flag is not None:
            sql_parts.append("is_csi300 = 1")
        if csi1000_flag is not None:
            sql_parts.append("is_csi1000 = 1")
        # 将业务行业词映射到库内 industry 字段常见取值，仍然基于 industry 字段匹配。
        industry_alias_map: dict[str, list[str]] = {
            # 金融
            # 先保留业务词本身（如“金融”），再扩展到可能的细分别名，避免只匹配别名导致 0 结果。
            "金融": ["金融", "金融信息服务", "货币金融服务", "资本市场服务", "保险业", "其他金融业"],
            "银行": ["银行", "银行业", "货币金融服务"],
            "保险": ["保险", "保险业"],
            "证券": ["证券", "证券、期货业", "资本市场服务", "其他金融业"],
            "券商": ["资本市场服务"],
            # 科技
            "科技": [
                "计算机、通信和其他电子设备制造业",
                "软件和信息技术服务业",
                "互联网和相关服务",
                "电信、广播电视和卫星传输服务",
                "研究和试验发展",
                "科技推广和应用服务业",
            ],
            "半导体": ["计算机、通信和其他电子设备制造业"],
            "电子": ["计算机、通信和其他电子设备制造业"],
            "通信": ["计算机、通信和其他电子设备制造业"],
            "计算机": ["软件和信息技术服务业", "计算机、通信和其他电子设备制造业"],
            "软件": ["软件和信息技术服务业"],
            # 医药消费
            "医药": ["医药制造业", "卫生"],
            "医疗": ["医药制造业", "卫生"],
            "生物医药": ["医药制造业"],
            "白酒": ["酒、饮料和精制茶制造业"],
            "消费": [
                "食品制造业",
                "酒、饮料和精制茶制造业",
                "农副食品加工业",
                "纺织服装、服饰业",
                "零售业",
            ],
            # 制造与周期
            "军工": ["铁路、船舶、航空航天和其它运输设备制造业"],
            "新能源": ["电气机械及器材制造业", "电力、热力生产和供应业", "汽车制造业"],
            "光伏": ["电气机械及器材制造业"],
            "锂电": ["电气机械及器材制造业"],
            "汽车": ["汽车制造业"],
            "化工": ["化学原料及化学制品制造业", "化学纤维制造业"],
            "有色": ["有色金属冶炼及压延加工业", "有色金属矿采选业"],
            "钢铁": ["黑色金属冶炼及压延加工业", "黑色金属矿采选业"],
            "煤炭": ["煤炭开采和洗选业"],
            "石油": ["石油和天然气开采业", "石油加工、炼焦及核燃料加工业"],
            # 地产基建
            "地产": ["房地产业", "房屋建筑业"],
            "房地产": ["房地产业", "房屋建筑业"],
            "基建": ["土木工程建筑业", "建筑装饰和其他建筑业", "建筑安装业", "房屋建筑业"],
            "建筑": ["土木工程建筑业", "建筑装饰和其他建筑业", "建筑安装业", "房屋建筑业"],
            # 交通运输与公用事业
            "交通运输": ["道路运输业", "铁路运输业", "航空运输业", "水上运输业", "仓储业", "邮政业"],
            "公用事业": ["电力、热力生产和供应业", "燃气生产和供应业", "水的生产和供应业", "公共设施管理业"],
            "环保": ["生态保护和环境治理业"],
            # 传媒文娱
            "传媒": ["新闻和出版业", "广播、电视、电影和影视录音制作业", "文化艺术业"],
            "文娱": ["广播、电视、电影和影视录音制作业", "文化艺术业", "体育"],
        }
        expanded_industry_terms: list[str] = []
        for ind in industry_list:
            aliases = industry_alias_map.get(ind, [ind])
            for alias in aliases:
                if alias and alias not in expanded_industry_terms:
                    expanded_industry_terms.append(alias)

        # 行业匹配
        ind_clause = " OR ".join(
            [
                f"industry ILIKE '%{_sql_quote(ind)}%'"
                for ind in expanded_industry_terms
            ]
        )
        if ind_clause:
            sql_parts.append(f"({ind_clause})")
        where_clause = " AND ".join(sql_parts) if sql_parts else "true"
        dsl = (
            "SQL: SELECT code as symbol, stock_name as name, close, "
            "total_mv as market_cap, pe_ttm as pe_ratio, pb as pb_ratio "
            f"FROM {LATEST_TABLE} WHERE {where_clause}"
        )
    else:
        dsl = "SELECT symbol WHERE " + (" AND ".join(parts) if parts else "true")
    mapping = {"factors": factors, "industry": industry_list, "local_hit": local_hit}
    if not parts and not industry_list:
        suggestions.append("未识别具体条件, 可尝试使用: 市值100-300, PE 15-20, 行业: 计算机")
    suggestions.extend(loose_mode_hits)
    return dsl, mapping, suggestions


@router.post("/parse-text", response_model=ParseResponse)
async def parse_text(body: ParseTextRequest, request: Request):
    # TODO: 后续迁移到 steps/
    try:
        logger.info("parse_text started", extra={"trace_id": _trace_id(request)})
        if _is_full_market_query(body.text):
            sql = _build_full_market_sql()
            return ParseResponse(
                dsl=f"SQL: {sql}",
                mapping={
                    "semantic_category": "full_market",
                    "query": body.text,
                    "target_table": "stock_daily",
                    "sql": sql,
                },
                warnings=[],
                confidence=0.95,
                suggestions=["已识别为全市场查询，使用 stock_daily 最新交易日全量数据"],
                version="2.0.0",
            )

        local_dsl, local_mapping, local_suggestions = _simple_parse_text(body.text)
        if local_mapping.get("local_hit"):
            return ParseResponse(
                dsl=local_dsl,
                mapping=local_mapping,
                warnings=[],
                confidence=0.85,
                suggestions=local_suggestions,
                version="local-1.0.0",
            )

        parser = get_intent_parser()
        intent = await parser.parse(body.text)

        generator = get_sql_generator()
        sql = await generator.generate_sql(intent)
        if sql:
            sql = re.sub(
                r"from\s+stock_selection",
                f"from {LATEST_TABLE}",
                sql,
                flags=re.IGNORECASE,
            )
            sql = re.sub(r"from\s+stock_daily", f"from {LATEST_TABLE}", sql, flags=re.IGNORECASE)

        if _is_full_market_sql(sql):
            sql = _build_full_market_sql()
            intent = {
                **intent,
                "semantic_category": "full_market",
                "target_table": "stock_daily_latest",
            }

        if not sql:
            dsl, mapping, suggestions = _simple_parse_text(body.text)
        else:
            dsl = f"SQL: {sql}"
            mapping = {**intent, "sql": sql}
            suggestions = [
                f"已识别为 {intent.get('semantic_category', '通用')} 策略原型",
                f"选用数据表: {intent.get('target_table', 'stock_selection')}",
            ]

        return ParseResponse(
            dsl=dsl,
            mapping=mapping,
            warnings=[],
            confidence=0.9,
            suggestions=suggestions,
            version="2.0.0",
        )
    except Exception as e:
        logger.error("parse_text failed: %s", e)
        try:
            dsl, mapping, suggestions = _simple_parse_text(body.text)
            return ParseResponse(dsl=dsl, mapping=mapping, suggestions=suggestions)
        except:
            raise HTTPException(status_code=400, detail=f"解析失败: {e}")


# ============================================================================
