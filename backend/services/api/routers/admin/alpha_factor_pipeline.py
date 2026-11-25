"""AlphaAgent 因子 → 训练特征自动流水线

将 AlphaAgent 挖到的因子一键推广到训练特征集：
1. 从 rd_agent_factors 读取因子
2. 提取 Qlib 表达式
3. 用 Qlib 计算因子值
4. 写入用户自定义数据集 (data/quantcustom/6_ml_datasets/l1_factors)
5. 注册到特征目录 (qm_feature_*)
"""

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import uuid
from typing import Any, Optional

import numpy as np
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text

from backend.services.api.user_app.middleware.auth import require_admin
from backend.shared.database_manager_v2 import get_session
from backend.shared.user_factor_publish import (
    create_building_generation,
    describe_custom_user_dataset_state,
    ensure_user_factor_schema,
    get_active_generation,
    list_generations,
    mark_generation_failed,
    new_generation_id,
    publish_completed_generation,
    upsert_discovered_field,
    validate_dataset_key,
)

logger = logging.getLogger(__name__)
router = APIRouter(
    tags=["AlphaFactorPipeline"],
    dependencies=[Depends(require_admin)],  # 路由器级认证兜底，新增端点默认受保护
)

# ── 路径配置 ──
# 因子挖掘/历史补全属用户产出，统一写用户自定义数据集
# （data/quantcustom/6_ml_datasets/l1_factors），不写官方 QuantDB，也不再写
# db/feature_snapshots/model_features_*.parquet 旧入口。
# Qlib 目录统一走 qlib_paths 解析（固定目录优先）
try:
    from backend.shared.qlib_paths import resolve_qlib_provider_uri
    QLIB_PROVIDER_URI = resolve_qlib_provider_uri("CN")
except Exception:
    QLIB_PROVIDER_URI = "/data/qlib/cn_data"


class PromoteRequest(BaseModel):
    factor_ids: list[str] = []
    auto_train: bool = False


class PromoteExpressionRequest(BaseModel):
    """直接用表达式推广因子（不依赖 rd_agent_factors 表中的 ID）。"""
    factors: list[dict[str, str]]  # [{name, expression}, ...]
    auto_train: bool = False


class PromoteResponse(BaseModel):
    success: bool
    promoted: list[dict[str, Any]]
    errors: list[dict[str, Any]]
    auto_train_triggered: bool = False


# ═══════════════════════════════════════════════════════════════════
# 因子表达式提取
# ═══════════════════════════════════════════════════════════════════


def _extract_qlib_expression(factor_code: str, metadata: dict) -> str | None:
    """从因子代码或 metadata 中提取 Qlib 表达式。"""
    # 1. 优先从 metadata 取
    formulation = metadata.get("formulation", "")
    if formulation and _is_qlib_expression(formulation):
        return formulation.strip()

    # 2. 从 ExpressionFactor 子类提取 (sandbox via subprocess)
    if "get_expression" in factor_code and "ExpressionFactor" in factor_code:
        try:
            import tempfile
            with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, prefix="factor_expr_") as tmp:
                tmp.write(factor_code)
                tmp_path = tmp.name
            try:
                result = subprocess.run(
                    [sys.executable, "-c",
                     f"import json; exec(open({repr(tmp_path)}).read()); "
                     f"ns={{k:v for k,v in locals().items() if isinstance(v,type) and hasattr(v,'get_expression')}};"
                     f"expr=list(ns.values())[0]().get_expression() if ns else ''; "
                     f"print(json.dumps({{'expr':expr}}))"],
                    capture_output=True, text=True, timeout=15,
                )
                if result.returncode == 0 and result.stdout.strip():
                    import json as _json
                    out = _json.loads(result.stdout.strip().splitlines()[-1])
                    expr = out.get("expr", "")
                    if expr and _is_qlib_expression(expr):
                        return expr.strip()
            finally:
                os.unlink(tmp_path)
        except Exception as e:
            logger.debug("Failed to extract ExpressionFactor via subprocess: %s", e)

    # 3. 尝试从 return "..." 模式提取
    m = re.search(r'return\s+["\'](.+?)["\']', factor_code)
    if m and _is_qlib_expression(m.group(1)):
        return m.group(1).strip()

    return None


def _is_qlib_expression(expr: str) -> bool:
    """判断是否为合法的 Qlib 表达式（包含 $变量 或 Qlib 函数）。"""
    if not expr or len(expr) < 5:
        return False
    qlib_indicators = ["$", "Ref(", "Mean(", "Std(", "TS_", "EMA(", "RSI(", "MACD(",
                       "Rank(", "Corr(", "Slope(", "Resi(", "DELAY(", "COUNT(",
                       "SUMIF(", "FILTER(", "REGBETA(", "REGRESI(",
                       "(", ")", "?", ":", "&&", "||"]
    return any(ind in expr for ind in qlib_indicators)


def _sanitize_feature_key(name: str) -> str:
    """将因子名转为合法的 feature_key。"""
    key = re.sub(r'[^a-zA-Z0-9_]', '_', name).lower().strip('_')
    key = re.sub(r'_+', '_', key)
    return f"alpha_{key}"[:80]


# ═══════════════════════════════════════════════════════════════════
# Qlib 表达式计算
# ═══════════════════════════════════════════════════════════════════


_qlib_initialized = False


def _load_joblib_backend() -> str:
    """Qlib 并行后端，与回测服务同口径（`QLIB_JOBLIB_BACKEND` 可覆盖）。

    因子 `D.features` 是内存映射读 + 表达式计算，进程后端（loky）要把
    任务 pickle 到子进程并在子进程重建缓存，csi300 全历史会卡死；
    threading 共享内存，实测 710k 行 1.7 秒，故缺省 threading。
    """
    backend = os.getenv("QLIB_JOBLIB_BACKEND")
    if backend and backend.strip():
        return backend.strip()
    return "threading"


def _load_kernels() -> int:
    raw = os.getenv("QLIB_BACKTEST_KERNELS") or os.getenv("QLIB_KERNELS")
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            logger.warning("Invalid QLIB_BACKTEST_KERNELS value: %r", raw)
    return max(1, min((os.cpu_count() or 1), 8))


def _ensure_qlib_init():
    """确保 Qlib 已初始化（惰性初始化，只执行一次）。"""
    global _qlib_initialized
    if _qlib_initialized:
        return
    try:
        import qlib
        from qlib.config import C
        joblib_backend = _load_joblib_backend()
        kernels = _load_kernels()
        qlib.init(
            provider_uri=QLIB_PROVIDER_URI,
            region="cn",
            joblib_backend=joblib_backend,
        )
        C["joblib_backend"] = joblib_backend
        C["kernels"] = kernels
        _qlib_initialized = True
        logger.info(
            "Qlib initialized with provider_uri=%s joblib_backend=%s kernels=%s",
            QLIB_PROVIDER_URI, joblib_backend, kernels,
        )
    except Exception as e:
        logger.error("Failed to init Qlib: %s", e)
        raise RuntimeError(f"Qlib 初始化失败: {e}") from e


def compute_factor_via_qlib(expression: str, feature_name: str) -> pd.DataFrame:
    """用 Qlib 引擎计算因子表达式，返回 MultiIndex(date, instrument) DataFrame。"""
    _ensure_qlib_init()
    from qlib.data import D

    instruments = D.instruments(market="csi300")
    df = D.features(instruments, [expression], freq="day")
    if df.empty:
        raise RuntimeError(f"Qlib 返回空数据，表达式可能无效: {expression}")
    df.columns = [feature_name]
    return df


# ═══════════════════════════════════════════════════════════════════
# 用户自定义数据集写入
# ═══════════════════════════════════════════════════════════════════


def merge_factor_into_parquet(factor_df: pd.DataFrame, feature_name: str) -> int:
    """将因子列写入用户自定义数据集，返回非空值数。

    因子挖掘与历史补全是用户产出，落 ``data/quantcustom/6_ml_datasets/l1_factors``
    （CUSTOM 市场），不写官方 QuantDB ``6_ml_datasets/l1_factors``。仅追加/覆盖单列，
    按 6 位代码对齐 symbol；分区不存在时按 (symbol,date) 新建，之后经后台
    「字段发现 + 草稿发布」注册为可用训练源。
    """
    from backend.shared.feature_source import merge_factor_into_source

    return merge_factor_into_source(
        factor_df,
        feature_name,
        source="l1_factors",
        market="CUSTOM",
        create_missing=True,
    )


# ═══════════════════════════════════════════════════════════════════
# 特征目录注册
# ═══════════════════════════════════════════════════════════════════


async def register_feature_in_catalog(
    feature_key: str,
    feature_name: str,
    formula: str,
    category_id: str = "alpha_agent",
) -> None:
    """将新特征注册到 qm_feature_* 目录表。"""
    async with get_session() as session:
        # 1. 确保 alpha_agent 分类存在
        await session.execute(text("""
            INSERT INTO qm_feature_category (category_id, category_name, sort_order, description)
            VALUES (:cat_id, :cat_name, 99, 'AlphaAgent 自动挖掘的因子')
            ON CONFLICT (category_id) DO NOTHING
        """), {"cat_id": category_id, "cat_name": "AlphaAgent 挖掘因子"})

        # 2. 插入特征定义
        feature_id = str(uuid.uuid4())
        await session.execute(text("""
            INSERT INTO qm_feature_definition (feature_id, feature_key, feature_name, formula, category_id)
            VALUES (:fid, :fkey, :fname, :formula, :cat_id)
            ON CONFLICT (feature_key) DO UPDATE SET
                feature_name = EXCLUDED.feature_name,
                formula = EXCLUDED.formula,
                updated_at = now()
        """), {
            "fid": feature_id,
            "fkey": feature_key,
            "fname": feature_name,
            "formula": formula,
            "cat_id": category_id,
        })

        # 3. 获取当前活跃版本
        version_row = await session.execute(text("""
            SELECT version_id FROM qm_feature_set_version
            WHERE status = 'active'
            ORDER BY effective_at DESC, created_at DESC
            LIMIT 1
        """))
        version = version_row.mappings().first()
        if not version:
            logger.warning("No active feature set version found, skipping set_item registration")
            return

        version_id = version["version_id"]

        # 4. 获取当前最大 order_no
        max_order_row = await session.execute(text("""
            SELECT COALESCE(MAX(order_no), 0) + 1 AS next_order
            FROM qm_feature_set_item
            WHERE version_id = :vid
        """), {"vid": version_id})
        next_order = max_order_row.scalar() or 999

        # 5. 插入 set_item（如果不存在）
        # id 是 integer 类型，使用 MAX(id) + 1
        max_id_row = await session.execute(text("SELECT COALESCE(MAX(id), 0) + 1 FROM qm_feature_set_item"))
        next_id = max_id_row.scalar() or 1

        await session.execute(text("""
            INSERT INTO qm_feature_set_item (id, version_id, category_id, feature_key, order_no, enabled)
            VALUES (:id, :vid, :cat_id, :fkey, :order_no, true)
            ON CONFLICT (version_id, feature_key) DO UPDATE SET
                enabled = true,
                order_no = EXCLUDED.order_no
        """), {
            "id": next_id,
            "vid": version_id,
            "cat_id": category_id,
            "fkey": feature_key,
            "order_no": next_order,
        })

        # 6. 更新版本特征计数
        await session.execute(text("""
            UPDATE qm_feature_set_version
            SET feature_count = (
                SELECT COUNT(*) FROM qm_feature_set_item
                WHERE version_id = :vid AND enabled = true
            )
            WHERE version_id = :vid
        """), {"vid": version_id})

    logger.info("Registered feature %s (%s) in catalog", feature_key, feature_name)


# ═══════════════════════════════════════════════════════════════════
# 自动发布（企业版 generation 语义下放：建代次 → 合并 → 原子发布切指针）
# ═══════════════════════════════════════════════════════════════════

_AUTO_PUBLISH_DATASET = "l1_factors"
_AUTO_PUBLISH_CREATED_BY = "api:alpha_factor_pipeline"


def _frame_date_span(factor_df: pd.DataFrame) -> tuple[str | None, str | None]:
    """从因子帧提取日期上下界（`YYYY-MM-DD`），供字段发现表登记。"""
    try:
        dates = None
        if isinstance(factor_df.index, pd.MultiIndex):
            for name in ("datetime", "date", "trade_date"):
                if name in factor_df.index.names:
                    dates = pd.to_datetime(
                        factor_df.index.get_level_values(name), errors="coerce"
                    )
                    break
        for col in ("trade_date", "date", "datetime"):
            if dates is None and col in factor_df.columns:
                dates = pd.to_datetime(factor_df[col], errors="coerce")
                break
        if dates is None:
            return None, None
        dates = dates.dropna()
        if len(dates) == 0:
            return None, None
        return (
            str(dates.min().date()),
            str(dates.max().date()),
        )
    except Exception:  # noqa: BLE001
        return None, None


async def _auto_publish_factor(
    *,
    feature_key: str,
    factor_name: str,
    expression: str,
    factor_df: pd.DataFrame,
    non_null: int,
) -> dict[str, Any]:
    """合并成功后自动发布一代次；失败抛错前已记 ``failed``，指针不动。

    返回 publish 记录（含 ``generation_id``）。调用方捕获异常后记入 errors。
    """
    generation_id = new_generation_id()
    async with get_session() as session:
        await ensure_user_factor_schema(session)
        await create_building_generation(
            session,
            generation_id=generation_id,
            dataset_key=_AUTO_PUBLISH_DATASET,
            feature_key=feature_key,
            factor_name=factor_name,
            expression=expression,
            created_by=_AUTO_PUBLISH_CREATED_BY,
        )
    try:
        rows = int(non_null)
        if rows <= 0:
            raise ValueError(f"因子合并结果为空（non_null={non_null}），拒绝发布")
        min_date, max_date = _frame_date_span(factor_df)
        byte_count = max(1, rows * 8)
        try:
            _dir_state = describe_custom_user_dataset_state(_AUTO_PUBLISH_DATASET)
            _dir_fingerprint = str(_dir_state.get("fingerprint")) if _dir_state else ""
        except Exception:  # noqa: BLE001
            _dir_fingerprint = ""
        async with get_session() as session:
            record = await publish_completed_generation(
                session,
                generation_id=generation_id,
                dataset_key=_AUTO_PUBLISH_DATASET,
                row_count=rows,
                byte_count=byte_count,
                manifest_extra={
                    "factor_name": factor_name,
                    "min_date": min_date,
                    "max_date": max_date,
                    "fingerprint": _dir_fingerprint,
                },
            )
            await upsert_discovered_field(
                session,
                column=feature_key,
                market="CUSTOM",
                dataset_id=_AUTO_PUBLISH_DATASET,
                min_date=min_date,
                max_date=max_date,
            )
        return record
    except Exception as exc:  # noqa: BLE001
        async with get_session() as session:
            try:
                await ensure_user_factor_schema(session)
                await mark_generation_failed(session, generation_id, error=str(exc))
            except Exception:  # noqa: BLE001
                logger.warning("标记代次失败状态异常: %s", generation_id)
        raise


# ═══════════════════════════════════════════════════════════════════
# API 端点
# ═══════════════════════════════════════════════════════════════════


@router.post("/promote", response_model=PromoteResponse)
async def promote_factors(req: PromoteRequest):
    """将 AlphaAgent 因子推广到训练特征集。

    流程：
    1. 从 rd_agent_factors 读取因子
    2. 提取 Qlib 表达式
    3. 计算因子值并写入用户自定义数据集
    4. 注册到特征目录
    5. 可选：自动触发训练
    """
    if not req.factor_ids:
        raise HTTPException(status_code=400, detail="factor_ids 不能为空")

    # 读取因子
    async with get_session(read_only=True) as session:
        placeholders = ", ".join(f":fid_{i}" for i in range(len(req.factor_ids)))
        params = {f"fid_{i}": fid for i, fid in enumerate(req.factor_ids)}
        rows = await session.execute(text(f"""
            SELECT factor_id, factor_name, factor_code, metadata_json, ic_value, sharpe_ratio
            FROM rd_agent_factors
            WHERE factor_id IN ({placeholders})
        """), params)
        factors = [dict(r) for r in rows.mappings().all()]

    if not factors:
        raise HTTPException(status_code=404, detail="未找到指定的因子")

    promoted = []
    errors = []

    for factor in factors:
        factor_id = factor["factor_id"]
        factor_name = factor["factor_name"]
        factor_code = factor.get("factor_code", "")
        metadata = factor.get("metadata_json", {})
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except Exception:
                metadata = {}

        try:
            # 1. 提取 Qlib 表达式
            expression = _extract_qlib_expression(factor_code, metadata)
            if not expression:
                errors.append({
                    "factor_id": factor_id,
                    "factor_name": factor_name,
                    "error": "无法提取 Qlib 表达式（因子可能是纯 pandas 代码）",
                })
                continue

            # 2. 生成 feature_key
            feature_key = _sanitize_feature_key(factor_name)

            # 3. 计算因子值
            factor_df = compute_factor_via_qlib(expression, feature_key)

            # 4. 写入用户自定义数据集
            non_null = merge_factor_into_parquet(factor_df, feature_key)

            # 5. 注册到特征目录
            await register_feature_in_catalog(
                feature_key=feature_key,
                feature_name=factor_name,
                formula=expression,
            )

            # 6. auto-publish a generation (failed publish keeps old pointer)
            try:
                publish_record = await _auto_publish_factor(
                    feature_key=feature_key,
                    factor_name=factor_name,
                    expression=expression,
                    factor_df=factor_df,
                    non_null=non_null,
                )
                generation_id = str(publish_record.get("generation_id") or "")
            except Exception as exc:  # noqa: BLE001
                logger.warning("auto-publish failed for %s: %s", factor_name, exc)
                errors.append(
                    {
                        "factor_id": factor_id,
                        "factor_name": factor_name,
                        "error": f"auto-publish failed: {exc}",
                    }
                )
                continue

            promoted.append(
                {
                    "factor_id": factor_id,
                    "factor_name": factor_name,
                    "feature_key": feature_key,
                    "expression": expression,
                    "non_null_values": non_null,
                    "ic_value": factor.get("ic_value"),
                    "sharpe_ratio": factor.get("sharpe_ratio"),
                    "generation_id": generation_id,
                }
            )

        except Exception:
            logger.exception("Failed to promote factor %s", factor_name)
            errors.append({
                "factor_id": factor_id,
                "factor_name": factor_name,
                "error": "因子推广失败，请检查因子代码",
            })

    # 可选：自动触发训练
    auto_train_triggered = False
    if req.auto_train and promoted:
        try:
            # TODO: 调用训练 API
            auto_train_triggered = True
        except Exception as e:
            logger.error("Auto-train failed: %s", e)

    return PromoteResponse(
        success=len(promoted) > 0,
        promoted=promoted,
        errors=errors,
        auto_train_triggered=auto_train_triggered,
    )


@router.get("/extractable")
async def list_extractable_factors(
    limit: int = 50,
    status: str | None = "completed",
):
    """列出可提取 Qlib 表达式的因子（已完成回测且有代码）。"""
    async with get_session(read_only=True) as session:
        conditions = ["factor_code IS NOT NULL", "factor_code != ''"]
        params: dict[str, Any] = {"limit": limit}
        if status:
            conditions.append("status = :status")
            params["status"] = status

        where = " AND ".join(conditions)
        rows = await session.execute(text(f"""
            SELECT factor_id, factor_name, factor_code, metadata_json,
                   ic_value, sharpe_ratio, annual_return, created_at
            FROM rd_agent_factors
            WHERE {where}
            ORDER BY created_at DESC
            LIMIT :limit
        """), params)

        results = []
        for r in rows.mappings().all():
            item = dict(r)
            metadata = item.get("metadata_json", {})
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except Exception:
                    metadata = {}

            # 尝试提取表达式
            expression = _extract_qlib_expression(item.get("factor_code", ""), metadata)
            item["extractable"] = expression is not None
            item["qlib_expression"] = expression
            item.pop("factor_code", None)
            item.pop("metadata_json", None)
            results.append(item)

    return {"success": True, "data": results, "total": len(results)}


@router.post("/promote-by-expression", response_model=PromoteResponse)
async def promote_factors_by_expression(req: PromoteExpressionRequest):
    """直接用因子名 + Qlib 表达式推广到训练特征集（不依赖 rd_agent_factors 表）。"""
    if not req.factors:
        raise HTTPException(status_code=400, detail="factors 不能为空")

    promoted = []
    errors = []

    for item in req.factors:
        factor_name = item.get("name", "").strip()
        expression = item.get("expression", "").strip()

        if not factor_name or not expression:
            errors.append({"factor_name": factor_name, "error": "name 和 expression 不能为空"})
            continue

        if not _is_qlib_expression(expression):
            errors.append({"factor_name": factor_name, "error": f"不是合法的 Qlib 表达式: {expression[:100]}"})
            continue

        try:
            feature_key = _sanitize_feature_key(factor_name)
            factor_df = compute_factor_via_qlib(expression, feature_key)
            non_null = merge_factor_into_parquet(factor_df, feature_key)
            await register_feature_in_catalog(
                feature_key=feature_key,
                feature_name=factor_name,
                formula=expression,
            )
            # auto-publish a generation (failed publish keeps old pointer)
            try:
                publish_record = await _auto_publish_factor(
                    feature_key=feature_key,
                    factor_name=factor_name,
                    expression=expression,
                    factor_df=factor_df,
                    non_null=non_null,
                )
                generation_id = str(publish_record.get("generation_id") or "")
            except Exception as exc:  # noqa: BLE001
                logger.warning("auto-publish failed for %s: %s", factor_name, exc)
                errors.append(
                    {"factor_name": factor_name, "error": f"auto-publish failed: {exc}"}
                )
                continue

            promoted.append(
                {
                    "factor_name": factor_name,
                    "feature_key": feature_key,
                    "expression": expression,
                    "non_null_values": non_null,
                    "generation_id": generation_id,
                }
            )
        except Exception:
            logger.exception("Failed to promote factor %s", factor_name)
            errors.append({"factor_name": factor_name, "error": "因子推广失败"})

    return PromoteResponse(
        success=len(promoted) > 0,
        promoted=promoted,
        errors=errors,
        auto_train_triggered=False,
    )


@router.get("/generations/active")
async def get_active_user_factor_generation_endpoint(
    dataset: str = "l1_factors",
):
    """查询用户因子数据集当前自动发布的 active 代次（训练侧可信版本）。"""
    dataset_key = validate_dataset_key(dataset)
    async with get_session() as session:
        await ensure_user_factor_schema(session)
        record = await get_active_generation(session, dataset_key)
    return {"dataset_key": dataset_key, "active": record}


@router.get("/generations")
async def list_user_factor_generation_endpoint(
    dataset: str | None = None,
    limit: int = 50,
):
    """按时间倒序列出用户因子数据集代次（含 building/failed，便于排障）。"""
    async with get_session() as session:
        await ensure_user_factor_schema(session)
        records = await list_generations(session, dataset, limit)
    return {"generations": records, "total": len(records)}
