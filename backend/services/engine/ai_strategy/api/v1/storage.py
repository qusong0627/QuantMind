"""AI Strategy V1 云端保存与股票池文件路由"""

import logging
import os
import hashlib
import math
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import text

try:
    from backend.shared.database_pool import get_db
except ImportError:
    from shared.database_pool import get_db

from ...models.stock_pool_file import StockPoolFile
from ...services.cos_uploader import get_cos_uploader
from ...steps.step1_stock_selection import LATEST_TABLE
from ...steps.step2_pool_confirmation import (
    TOTAL_MV_TO_YI,
    _build_compat_table_sql,
    _build_pool_summary,
    _get_table_columns,
    _get_universe_total,
)
from ..schemas import (
    DeletePoolFileRequest,
    DeletePoolFileResponse,
    GetActivePoolFileRequest,
    GetActivePoolFileResponse,
    ListPoolFilesRequest,
    ListPoolFilesResponse,
    PoolFileSummary,
    PoolItem,
    PreviewPoolFileRequest,
    PreviewPoolFileResponse,
    SavePoolFileRequest,
    SavePoolFileResponse,
    SaveToCloudRequest,
    SaveToCloudResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _trace_id(request: Request | None) -> str | None:
    if not request:
        return None
    return (
        getattr(request.state, "trace_id", None)
        or request.headers.get("X-Trace-Id")
        or request.headers.get("X-Request-Id")
    )


def _to_qlib_instrument(symbol: str) -> str:
    s = (symbol or "").strip()
    if not s:
        return ""
    s_upper = s.upper()
    if len(s_upper) == 8 and (s_upper[:2] in ("SZ", "SH")) and s_upper[2:].isdigit():
        return s_upper
    if "." in s_upper:
        base, suffix = s_upper.split(".", 1)
        base = base.strip()
        suffix = suffix.strip()
        if suffix in ("SZ", "SH") and base.isdigit():
            return f"{suffix}{base.zfill(6)}"
    if len(s_upper) >= 8 and (s_upper[:2] in ("SZ", "SH")):
        tail = "".join(ch for ch in s_upper[2:] if ch.isdigit())
        if tail:
            return f"{s_upper[:2]}{tail.zfill(6)[:6]}"
    return s_upper


def _qlib_to_db_code(symbol: str) -> str:
    s = (symbol or "").strip().upper()
    if not s:
        return ""
    if len(s) == 8 and (s[:2] in ("SZ", "SH")) and s[2:].isdigit():
        return f"{s[2:]}.{s[:2]}"
    if "." in s:
        base, suffix = s.split(".", 1)
        base = base.strip()
        suffix = suffix.strip()
        if suffix in ("SZ", "SH") and base.isdigit():
            return f"{base.zfill(6)}.{suffix}"
    return s


def _user_id_variants(user_id: str) -> list:
    uid = (user_id or "").strip()
    variants = {uid}
    if uid.isdigit():
        variants.add(uid.zfill(8))
        variants.add(str(int(uid)))
    return list(variants)


def _canonical_user_id(user_id: str) -> str:
    uid = (user_id or "").strip()
    return uid.zfill(8) if uid.isdigit() else uid


def _safe_number(value: Any, default: float = 0.0) -> float:
    """将数据库数值安全转换为 JSON 兼容浮点数（过滤 NaN/Inf）。"""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(n):
        return default
    return n


@router.post("/save-to-cloud", response_model=SaveToCloudResponse)
async def save_strategy_to_cloud(body: SaveToCloudRequest, request: Request):
    """保存策略到云端（PG + COS 统一存储）"""
    try:
        trace_id = _trace_id(request)
        authenticated_user_id = str(getattr(request.state, "user", {}).get("user_id") or "").strip()
        if body.user_id and str(body.user_id) != str(authenticated_user_id):
            raise HTTPException(status_code=403, detail="未授权：user_id 与认证身份不匹配")

        try:
            from backend.shared.strategy_storage import get_strategy_storage_service as _get_shared_svc
        except ImportError:
            from shared.strategy_storage import get_strategy_storage_service as _get_shared_svc  # type: ignore

        metadata = body.metadata or {}
        metadata.setdefault("description", f"AI 向导生成策略: {body.strategy_name}")

        result = await _get_shared_svc().save(
            user_id=authenticated_user_id,
            name=body.strategy_name,
            code=body.code,
            metadata=metadata,
        )

        logger.info(
            "Strategy saved to PG+COS: id=%s cos_key=%s user=%s",
            result["id"],
            result.get("cos_key"),
            authenticated_user_id,
            extra={"trace_id": trace_id},
        )

        return SaveToCloudResponse(
            success=True,
            strategy_id=result["id"],
            cos_url=result.get("cos_url"),
            cos_key=result.get("cos_key"),
            cloud_url=result.get("cos_url"),
            access_path=f"/user-center/strategies/{result['id']}",
            file_size=result.get("file_size"),
            code_hash=result.get("code_hash"),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Save to cloud failed: %s", e, exc_info=True)
        return SaveToCloudResponse(success=False, error=f"保存失败: {str(e)}")


@router.post("/list-pool-files", response_model=ListPoolFilesResponse)
async def list_pool_files(body: ListPoolFilesRequest):
    """列出用户历史保存的股票池文件"""
    try:
        uid_variants = _user_id_variants(body.user_id)
        with get_db() as db:
            q = db.query(StockPoolFile).filter(StockPoolFile.user_id.in_(uid_variants))
            if body.tenant_id:
                q = q.filter(StockPoolFile.tenant_id == body.tenant_id)
            rows = q.order_by(StockPoolFile.created_at.desc()).limit(int(body.limit)).all()
            pools = [PoolFileSummary(**r.to_dict()) for r in rows]
            return ListPoolFilesResponse(success=True, pools=pools)
    except Exception as e:
        logger.error("List pool files failed: %s", e, exc_info=True)
        return ListPoolFilesResponse(success=False, pools=[], error=f"获取失败: {e}")


@router.post("/preview-pool-file", response_model=PreviewPoolFileResponse)
async def preview_pool_file(body: PreviewPoolFileRequest):
    """加载并预览某个已保存的股票池文件"""
    try:
        uid_variants = _user_id_variants(body.user_id)
        with get_db() as db:
            q = db.query(StockPoolFile).filter(
                StockPoolFile.user_id.in_(uid_variants),
                StockPoolFile.file_key == body.file_key,
            )
            if body.tenant_id:
                q = q.filter(StockPoolFile.tenant_id == body.tenant_id)
            rec = q.order_by(StockPoolFile.created_at.desc()).first()
            if not rec:
                return PreviewPoolFileResponse(success=False, error="股票池不存在或无权限访问")

            uploader = get_cos_uploader()
            # 兼容 local/cos 双模式：local 优先使用 file_url，cos 使用 file_key
            content = await uploader.read_object(url=rec.file_url, object_key=rec.file_key)

            instruments: list[str] = []
            seen = set()
            for ln in (content or "").splitlines():
                x = _to_qlib_instrument(ln.strip())
                if not x or x in seen:
                    continue
                seen.add(x)
                instruments.append(x)

            if not instruments:
                return PreviewPoolFileResponse(
                    success=True,
                    items=[],
                    summary={"matchRate": 0.0, "totalCandidates": 0},
                    charts={},
                    pool_file=rec.to_dict(),
                )

            latest_columns = _get_table_columns(db, LATEST_TABLE)
            compat_table_sql = _build_compat_table_sql(LATEST_TABLE, latest_columns)
            sql = text(
                f"""
                select
                  symbol,
                  name,
                  total_mv as market_cap,
                  pe_ttm as pe_ratio,
                  pb as pb_ratio,
                  roe,
                  close,
                  amount,
                  volume
                from {compat_table_sql} stock_daily_latest
                where symbol = any(:codes)
                """
            )
            rows = db.execute(sql, {"codes": instruments}).fetchall()
            metrics_map: dict[str, dict[str, Any]] = {}
            for row in rows:
                metrics_map[str(row[0])] = {
                    "symbol": str(row[0]),
                    "name": row[1],
                    "metrics": {
                        "market_cap": _safe_number(row[2]) * TOTAL_MV_TO_YI,
                        "pe": _safe_number(row[3]),
                        "pb": _safe_number(row[4]),
                        "roe": _safe_number(row[5]) * 100,  # 转换为百分比
                        "close": _safe_number(row[6]),
                        "amount": _safe_number(row[7]),
                        "volume": _safe_number(row[8]),
                    },
                }

            items: list[PoolItem] = []
            for c in instruments:
                r = metrics_map.get(c)
                if r:
                    items.append(PoolItem(symbol=r["symbol"], name=r.get("name"), metrics=r.get("metrics") or {}))

            universe_total = _get_universe_total(body.user_id)
            summary, charts = _build_pool_summary(items, as_of_date=None, universe_total=universe_total)
            return PreviewPoolFileResponse(
                success=True,
                items=items,
                summary=summary,
                charts=charts,
                pool_file=rec.to_dict(),
            )
    except Exception as e:
        logger.error("Preview pool file failed: %s", e, exc_info=True)
        return PreviewPoolFileResponse(success=False, error=f"预览失败: {e}")


@router.post("/save-pool-file", response_model=SavePoolFileResponse)
async def save_pool_file(body: SavePoolFileRequest, request: Request):
    try:
        logger.info("save_pool_file started", extra={"trace_id": _trace_id(request)})
        if not body.pool_name.strip():
            return SavePoolFileResponse(success=False, error="股票池名称不能为空")
        uploader = get_cos_uploader()
        canonical_user_id = _canonical_user_id(body.user_id)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        stock_count = len(body.pool)

        if body.format == "csv":
            lines = ["symbol,name"]
            for item in body.pool:
                lines.append(f"{item.get('symbol', '')},{item.get('name', '')}")
            content = "\n".join(lines)
        elif body.format == "txt":
            seen = set()
            lines = []
            for item in body.pool:
                q = _to_qlib_instrument(str(item.get("symbol", "")))
                if not q or q in seen:
                    continue
                seen.add(q)
                lines.append(q)
            stock_count = len(lines)
            content = "\n".join(lines) + ("\n" if lines else "")
        else:
            import json as _json

            content = _json.dumps(
                {"generated_at": datetime.now().isoformat(), "count": len(body.pool), "symbols": body.pool},
                ensure_ascii=False,
                indent=2,
            )

        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

        try:
            with get_db() as db:
                uid_variants = _user_id_variants(body.user_id)
                existing_query = db.query(StockPoolFile).filter(
                    StockPoolFile.user_id.in_(uid_variants),
                    StockPoolFile.code_hash == content_hash,
                )
                if body.tenant_id:
                    existing_query = existing_query.filter(StockPoolFile.tenant_id == body.tenant_id)
                existing_record = existing_query.order_by(StockPoolFile.created_at.desc()).first()

                if existing_record:
                    deactivate_query = db.query(StockPoolFile).filter(
                        StockPoolFile.user_id.in_(uid_variants),
                        StockPoolFile.is_active == True,
                    )
                    if body.tenant_id:
                        deactivate_query = deactivate_query.filter(StockPoolFile.tenant_id == body.tenant_id)
                    deactivate_query.update({"is_active": False}, synchronize_session=False)

                    existing_record.pool_name = body.pool_name
                    existing_record.format = body.format
                    existing_record.file_size = len(content.encode("utf-8"))
                    existing_record.stock_count = stock_count
                    existing_record.is_active = True
                    existing_record.updated_at = datetime.utcnow()
                    db.commit()
                    db.refresh(existing_record)

                    logger.info(
                        "Stock pool deduplicated and reactivated: id=%s user_id=%s code_hash=%s",
                        existing_record.id,
                        body.user_id,
                        content_hash,
                    )

                    return SavePoolFileResponse(
                        success=True,
                        pool_name=existing_record.pool_name,
                        file_url=existing_record.file_url,
                        file_key=existing_record.file_key,
                        relative_path=existing_record.relative_path,
                        file_size=existing_record.file_size,
                        code_hash=existing_record.code_hash,
                    )
        except Exception as db_lookup_error:
            logger.warning("Pool dedupe lookup failed, fallback to new save: %s", db_lookup_error, exc_info=True)

        pool_id = str(uuid4())
        result = await uploader.upload_pool_file(
            user_id=canonical_user_id,
            pool_id=pool_id,
            content=content,
            fmt=body.format,
            timestamp=timestamp,
        )

        try:
            with get_db() as db:
                uid_variants = _user_id_variants(body.user_id)
                query = db.query(StockPoolFile).filter(
                    StockPoolFile.user_id.in_(uid_variants),
                    StockPoolFile.is_active == True,
                )
                if body.tenant_id:
                    query = query.filter(StockPoolFile.tenant_id == body.tenant_id)
                query.update({"is_active": False}, synchronize_session=False)

                pool_file_record = StockPoolFile(
                    tenant_id=body.tenant_id,
                    user_id=canonical_user_id,
                    pool_name=body.pool_name,
                    file_key=result["object_key"],
                    file_url=result["url"],
                    relative_path=result.get("relative_path"),
                    format=body.format,
                    file_size=result["file_size"],
                    code_hash=result["code_hash"],
                    stock_count=stock_count,
                    is_active=True,
                )
                db.add(pool_file_record)
                db.commit()
                db.refresh(pool_file_record)
                logger.info(
                    "Stock pool file saved to database: id=%s, user_id=%s, file_key=%s",
                    pool_file_record.id,
                    body.user_id,
                    result["object_key"],
                )
        except Exception as db_error:
            logger.error("Failed to save to database: %s", db_error, exc_info=True)
            try:
                await uploader.delete_object(result["url"], result["object_key"])
            except Exception:
                logger.warning("Rollback pool object delete failed: key=%s", result.get("object_key"))
            return SavePoolFileResponse(success=False, error=f"保存股票池失败: 数据库写入失败 ({db_error})")

        return SavePoolFileResponse(
            success=True,
            pool_name=body.pool_name,
            file_url=result["url"],
            file_key=result["object_key"],
            relative_path=result.get("relative_path"),
            file_size=result["file_size"],
            code_hash=result["code_hash"],
        )
    except Exception as e:
        logger.error("Save pool file failed: %s", e, exc_info=True)
        return SavePoolFileResponse(success=False, error=f"保存股票池失败: {e}")


@router.post("/get-active-pool-file", response_model=GetActivePoolFileResponse)
async def get_active_pool_file(body: GetActivePoolFileRequest, request: Request):
    """获取用户当前活跃的股票池文件"""
    try:
        logger.info("get_active_pool_file started", extra={"trace_id": _trace_id(request)})
        uid_variants = _user_id_variants(body.user_id)
        with get_db() as db:
            query = db.query(StockPoolFile).filter(
                StockPoolFile.user_id.in_(uid_variants), StockPoolFile.is_active == True
            )
            if body.tenant_id:
                query = query.filter(StockPoolFile.tenant_id == body.tenant_id)
            pool_file = query.order_by(StockPoolFile.created_at.desc()).first()
            if pool_file:
                return GetActivePoolFileResponse(success=True, pool_file=pool_file.to_dict())
            return GetActivePoolFileResponse(success=True, pool_file=None)
    except Exception as e:
        logger.error("Get active pool file failed: %s", e, exc_info=True)
        return GetActivePoolFileResponse(success=False, error=f"获取失败: {e}")


@router.post("/delete-pool-file", response_model=DeletePoolFileResponse)
async def delete_pool_file(body: DeletePoolFileRequest):
    try:
        if not body.file_key and not body.file_url:
            return DeletePoolFileResponse(success=False, error="缺少 file_key/file_url")

        uploader = get_cos_uploader()
        cos_deleted = await uploader.delete_object(body.file_url or "", body.file_key)

        db_deleted = 0
        with get_db() as db:
            q = db.query(StockPoolFile)
            if body.user_id:
                uid_variants = _user_id_variants(body.user_id)
                q = q.filter(StockPoolFile.user_id.in_(uid_variants))
            if body.file_key:
                q = q.filter(StockPoolFile.file_key == body.file_key)
            elif body.file_url:
                q = q.filter(StockPoolFile.file_url == body.file_url)
            db_deleted = q.delete(synchronize_session=False)
            db.commit()

        if not cos_deleted and db_deleted == 0:
            return DeletePoolFileResponse(success=False, error="删除失败或对象不存在")

        return DeletePoolFileResponse(success=True)
    except Exception as e:
        logger.error("Delete pool file failed: %s", e, exc_info=True)
        return DeletePoolFileResponse(success=False, error=f"删除失败: {e}")
