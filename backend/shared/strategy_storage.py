"""
统一策略存储服务 (PG + COS)
Unified Strategy Storage Service

设计原则：
- PG strategies 表：存储全量元数据（含 cos_key、code_hash、file_size、code 冗余列）
- COS：存储策略代码文件（私读，通过预签名 URL 访问）
- 此模块作为 *唯一* 策略读写入口，供 api/engine/ai_strategy 服务共用

COS Key 命名规则:
  user_strategies/{user_id}/{yyyy}/{mm}/{strategy_id}.py

预签名 URL 有效期: 3600s（可通过 COS_STRATEGY_URL_TTL 覆盖）
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker as _sessionmaker
import builtins

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 数据库连接（独立的同步 psycopg2 引擎，不依赖 database_pool 的 asyncpg 驱动）
# ---------------------------------------------------------------------------


def _build_sync_db_url() -> str:
    """从环境变量构造同步 psycopg2 数据库 URL。"""
    url = os.getenv("DATABASE_URL", "").strip()
    # 替换 asyncpg → psycopg2
    if "asyncpg" in url:
        url = url.replace("asyncpg", "psycopg2")
    # 如果 DATABASE_URL 只是主机名（如 "localhost"），或者为空，使用分解的环境变量重组
    if not url.startswith("postgresql"):
        host = os.getenv("DB_MASTER_HOST", "localhost")
        port = os.getenv("DB_MASTER_PORT", "5432")
        user = os.getenv("DB_USER", "quantmind")
        password = os.getenv("DB_PASSWORD", "")
        dbname = os.getenv("DB_NAME", "quantmind")
        from urllib.parse import quote_plus

        url = f"postgresql+psycopg2://{user}:{quote_plus(password)}@{host}:{port}/{dbname}"
    # host.docker.internal → 在容器内与宿主机通信
    return url


_sync_engine = None
_sync_session_factory = None


def _get_sync_session_factory():
    global _sync_engine, _sync_session_factory
    if _sync_session_factory is None:
        db_url = _build_sync_db_url()
        _sync_engine = create_engine(db_url, pool_size=5, max_overflow=2, pool_pre_ping=True)
        _sync_session_factory = _sessionmaker(bind=_sync_engine, autocommit=False, autoflush=False)
    return _sync_session_factory


@contextmanager
def get_db():  # type: ignore[override]
    """同步数据库 session 上下文管理器（兼容 with get_db() as session:）"""
    Session = _get_sync_session_factory()
    session = Session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ---------------------------------------------------------------------------
# COS 服务（共享层，已有 TencentCOSService）
# ---------------------------------------------------------------------------
try:
    from backend.shared.cos_service import TencentCOSService
except ImportError:
    try:
        from shared.cos_service import TencentCOSService  # type: ignore
    except ImportError:
        TencentCOSService = None  # type: ignore


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
_URL_TTL = int(os.getenv("COS_STRATEGY_URL_TTL", "3600"))
_STRATEGY_FOLDER = "user_strategies"
_STATUS_ACTIVE = "ACTIVE"
_STATUS_DRAFT = "DRAFT"
_STATUS_LIVE_TRADING = "LIVE_TRADING"
_STATUS_ARCHIVED = "ARCHIVED"


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _make_cos_key(user_id: str, strategy_id: str) -> str:
    """生成 COS 对象键。格式: user_strategies/{user_id}/{yyyy}/{mm}/{strategy_id}.py"""
    now = datetime.now(timezone.utc)
    return f"{_STRATEGY_FOLDER}/{user_id}/{now.strftime('%Y/%m')}/{strategy_id}.py"


def _code_hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _ensure_int_user_id(user_id: str) -> int:
    """将 user_id 解析为整数。先按 users.user_id 业务字段查，再兼容纯数字。"""
    if get_db is None:
        try:
            return int(user_id)
        except ValueError:
            raise ValueError(f"无法解析 user_id={user_id!r} 为整数，且数据库不可用")

    try:
        with get_db() as session:
            # 1. 按业务用户ID查询
            row = session.execute(
                text("SELECT id FROM users WHERE user_id = :uid"),
                {"uid": user_id},
            ).scalar()
            if row is not None:
                return int(row)
            # 2. 按数字主键兼容
            if user_id.isdigit():
                row2 = session.execute(
                    text("SELECT id FROM users WHERE id = :id"),
                    {"id": int(user_id)},
                ).scalar()
                if row2 is not None:
                    return int(row2)
    except Exception as e:
        logger.warning(f"_ensure_int_user_id DB lookup failed: {e}")

    # 3. 最后尝试直接转换
    try:
        return int(user_id)
    except ValueError:
        raise ValueError(f"user_id={user_id!r} 无法解析为整数且在数据库中不存在")


def _parse_tags(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        if s.startswith("{") and s.endswith("}"):
            body = s[1:-1].strip()
            return [item.strip().strip('"') for item in body.split(",") if item.strip()]
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
        except Exception:
            pass
        return [s]
    return []


def _json_safe(obj: Any) -> str:
    return json.dumps(obj or {}, ensure_ascii=False)


def _normalize_lifecycle_status(status: str) -> str:
    text = str(status or "").strip().lower()
    if text in {"draft", "d"}:
        return _STATUS_DRAFT
    if text in {"active", "repository", "repo"}:
        return _STATUS_ACTIVE
    if text in {"live_trading", "live", "trading"}:
        return _STATUS_LIVE_TRADING
    if text in {"archived", "archive"}:
        return _STATUS_ARCHIVED
    # 保持兼容：未知状态按传入值大写写入
    return str(status or _STATUS_DRAFT).strip().upper() or _STATUS_DRAFT


# ---------------------------------------------------------------------------
# 主服务类
# ---------------------------------------------------------------------------


class StrategyStorageService:
    """
    策略统一存储服务：PG 元数据 + COS 代码文件
    """

    def __init__(self) -> None:
        self._cos: TencentCOSService | None = None
        self._has_cos_key_col: bool | None = None
        self._init_cos()

    def _has_cos_key_column(self, session) -> bool:
        """兼容旧库：运行时探测 strategies.cos_key 是否存在并缓存。"""
        if self._has_cos_key_col is not None:
            return self._has_cos_key_col
        try:
            exists = session.execute(text("""
                    SELECT EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_name = 'strategies' AND column_name = 'cos_key'
                    )
                    """)).scalar()
            self._has_cos_key_col = bool(exists)
        except Exception as e:
            logger.warning(f"探测 strategies.cos_key 失败，按不存在处理: {e}")
            self._has_cos_key_col = False
        return bool(self._has_cos_key_col)

    def _init_cos(self) -> None:
        if TencentCOSService is None:
            logger.warning("TencentCOSService not available, COS operations will fail")
            return
        try:
            self._cos = TencentCOSService()
            if not self._cos.client:
                logger.warning("COS client not initialized (missing credentials), falling back to local mode")
                self._cos = None
        except Exception as e:
            logger.warning(f"COS service init failed: {e}")
            self._cos = None

    @property
    def _local_mode(self) -> bool:
        return self._cos is None

    # ------------------------------------------------------------------
    # 内部 COS 操作
    # ------------------------------------------------------------------

    def _upload_code_to_cos(self, cos_key: str, code: str) -> str:
        """上传代码到 COS，返回预签名 URL。"""
        if self._local_mode:
            raise RuntimeError("COS 未初始化，无法上传策略")
        result = self._cos.upload_file(  # type: ignore[union-attr]
            file_data=code.encode("utf-8"),
            file_name=cos_key,
            use_exact_key=True,
            content_type="text/x-python",
        )
        if not result.get("success"):
            raise RuntimeError(f"COS 上传失败: {result.get('error')}")
        presigned = self._cos.get_presigned_url(cos_key, expired=_URL_TTL)  # type: ignore[union-attr]
        return presigned or f"{self._cos.base_url}/{cos_key}"

    def _get_presigned_url(self, cos_key: str | None) -> str | None:
        """按需生成预签名 URL。"""
        if not cos_key or self._local_mode:
            return None
        try:
            return self._cos.get_presigned_url(cos_key, expired=_URL_TTL)
        except Exception as e:
            logger.warning(f"生成预签名 URL 失败 cos_key={cos_key}: {e}")
            return None

    def _download_code_from_cos(self, cos_key: str) -> str:
        """从 COS 下载代码。"""
        if self._local_mode:
            raise RuntimeError("COS 未初始化，无法下载策略")
        try:
            resp = self._cos.client.get_object(  # type: ignore[union-attr]
                Bucket=self._cos.bucket_name,
                Key=cos_key,
            )
            return resp["Body"].read().decode("utf-8")
        except Exception as e:
            raise RuntimeError(f"从 COS 下载代码失败 cos_key={cos_key}: {e}") from e

    # ------------------------------------------------------------------
    # 内部 DB 操作
    # ------------------------------------------------------------------

    def _db_upsert(
        self,
        user_id: str,
        strategy_id: str | None,
        name: str,
        code: str,
        cos_key: str,
        cos_url: str | None,
        file_size: int,
        hash_val: str,
        metadata: dict[str, Any],
    ) -> str:
        """INSERT or UPDATE strategies 表。"""
        if get_db is None:
            raise RuntimeError("数据库不可用")

        now = datetime.now(timezone.utc)
        tags = _parse_tags(metadata.get("tags", []))
        description = metadata.get("description") or f"Updated ({now.strftime('%Y-%m-%d %H:%M')})"
        strategy_type = metadata.get("strategy_type") or "QUANTITATIVE"
        status = metadata.get("status") or _STATUS_DRAFT
        config = metadata.get("config") or {}
        parameters = metadata.get("parameters") or {}
        execution_config = metadata.get("execution_config") or {"max_buy_drop": -0.03}
        is_public = bool(metadata.get("is_public", False))

        with get_db() as session:
            has_cos_key = self._has_cos_key_column(session)

            params = {
                "uid": user_id,
                "name": name,
                "desc": description,
                "stype": strategy_type,
                "status": status,
                "config": _json_safe(config),
                "params": _json_safe(parameters),
                "exec_config": _json_safe(execution_config),
                "code": code,
                "cos_url": cos_url,
                "code_hash": hash_val,
                "file_size": file_size,
                "tags": _json_safe(tags),
                "is_public": is_public,
                "now": now,
                "backtest_count": 0,
                "view_count": 0,
                "like_count": 0,
                "version": 1,
                "is_verified": bool(metadata.get("is_verified", False)),
            }
            if has_cos_key:
                params["cos_key"] = cos_key

            if strategy_id and strategy_id.isdigit():
                # UPDATE
                params["sid"] = int(strategy_id)
                sql = f"""
                    UPDATE strategies SET
                        name = :name, description = :desc,
                        code = :code, cos_url = :cos_url, 
                        { "cos_key = :cos_key," if has_cos_key else "" }
                        code_hash = :code_hash, file_size = :file_size,
                        config = CAST(:config AS jsonb), 
                        parameters = CAST(:params AS jsonb),
                        execution_config = CAST(:exec_config AS jsonb),
                        tags = CAST(:tags AS jsonb),
                        updated_at = :now
                    WHERE id = :sid AND user_id = :uid
                """
                session.execute(text(sql), params)
                return strategy_id
            else:
                # INSERT
                sql = f"""
                    INSERT INTO strategies (
                        user_id, name, description, strategy_type, status,
                        config, parameters, execution_config, code, cos_url, 
                        { "cos_key," if has_cos_key else "" }
                        code_hash, file_size,
                        tags, is_public, shared_users, 
                        backtest_count, view_count, like_count, version, is_verified,
                        created_at, updated_at
                    ) VALUES (
                        :uid, :name, :desc, :stype, :status,
                        CAST(:config AS jsonb), CAST(:params AS jsonb), CAST(:exec_config AS jsonb),
                        :code, :cos_url, 
                        { ":cos_key," if has_cos_key else "" }
                        :code_hash, :file_size,
                        CAST(:tags AS jsonb), :is_public, CAST('[]' AS jsonb), 
                        :backtest_count, :view_count, :like_count, :version, :is_verified,
                        :now, :now
                    ) RETURNING id
                """
                row = session.execute(text(sql), params).scalar()
                return str(row)

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    async def save(
        self,
        user_id: str,
        name: str,
        code: str,
        metadata: dict[str, Any] | None = None,
        strategy_id: str | None = None,
    ) -> dict[str, Any]:
        metadata = metadata or {}
        new_id = str(uuid4())
        cos_key = _make_cos_key(user_id, new_id)
        file_size = len(code.encode("utf-8"))
        hash_val = _code_hash(code)

        cos_url = None
        if not self._local_mode:
            try:
                cos_url = self._upload_code_to_cos(cos_key, code)
            except Exception as e:
                logger.error(f"COS 上传失败: {e}")

        db_id = self._db_upsert(user_id, strategy_id, name, code, cos_key, cos_url, file_size, hash_val, metadata)
        return {"id": db_id, "cos_key": cos_key, "cos_url": cos_url, "code_hash": hash_val, "file_size": file_size}

    async def get(
        self, strategy_id: Any, user_id: str | None = None, resolve_code: bool = False
    ) -> dict[str, Any] | None:
        # 1. 检查是否为系统内置策略 (sys_ 开头)
        if isinstance(strategy_id, str) and strategy_id.startswith("sys_"):
            try:
                from backend.services.engine.qlib_app.services.strategy_templates import get_template_by_id

                # 关键修复：移除 sys_ 前缀后再去模板库查找
                real_template_id = strategy_id.replace("sys_", "")
                template = get_template_by_id(real_template_id)

                if template:
                    return {
                        "id": strategy_id,
                        "user_id": "system",
                        "name": template.name,
                        "description": template.description,
                        "code": template.code,
                        "is_verified": True,
                        "parameters": {"strategy_type": real_template_id, "topk": 50, "signal": "<PRED>"},
                        "tags": ["system", "template"],
                    }
            except Exception as e:
                logger.warning(f"加载系统模板 {strategy_id} 失败: {e}")

        # 2. 常规数据库查询 (仅限整数 ID)
        if not str(strategy_id).isdigit():
            return None

        with get_db() as session:
            has_cos_key = self._has_cos_key_column(session)
            cos_key_expr = "cos_key" if has_cos_key else "NULL::text as cos_key"
            sql = f"""
                SELECT id, user_id, name, description, strategy_type, status,
                       config, parameters, code, cos_url, {cos_key_expr}, 
                       code_hash, file_size, tags, is_public, created_at, updated_at,
                       is_verified, execution_config
                FROM strategies
                WHERE id = :sid AND status != '{_STATUS_ARCHIVED}'
            """
            params = {"sid": strategy_id}
            if user_id:
                params["uid"] = user_id
                sql += " AND user_id = :uid"

            row = session.execute(text(sql), params).fetchone()
            if not row:
                return None

            return {
                "id": str(row[0]),
                "user_id": str(row[1]),
                "name": row[2],
                "description": row[3],
                "code": row[8],
                "cos_url": row[9],
                "cos_key": row[10],
                "is_verified": bool(row[17]),
                "execution_config": row[18] or {},
                "tags": _parse_tags(row[13]),
                "parameters": row[7] or {},
            }

    async def mark_as_verified(self, strategy_id: str, user_id: str) -> bool:
        with get_db() as session:
            session.execute(
                text("UPDATE strategies SET is_verified = TRUE, updated_at = :now WHERE id = :sid AND user_id = :uid"),
                {"sid": int(strategy_id), "uid": user_id, "now": datetime.now(timezone.utc)},
            )
            return True

    def update_lifecycle_status(self, strategy_id: Any, user_id: str, status: str) -> bool:
        """
        更新策略生命周期状态（draft/repository/live_trading -> DB status）。
        返回是否命中并更新到记录。
        """
        sid_text = str(strategy_id or "").strip()
        if not sid_text.isdigit():
            logger.warning("update_lifecycle_status skip non-numeric strategy_id=%s", sid_text)
            return False
        normalized = _normalize_lifecycle_status(status)
        with get_db() as session:
            result = session.execute(
                text("""
                    UPDATE strategies
                    SET status = :status, updated_at = :now
                    WHERE id = :sid AND user_id = :uid AND status != :archived
                    """),
                {
                    "status": normalized,
                    "now": datetime.now(timezone.utc),
                    "sid": int(sid_text),
                    "uid": user_id,
                    "archived": _STATUS_ARCHIVED,
                },
            )
            return bool((result.rowcount or 0) > 0)

    async def delete(self, strategy_id: Any, user_id: str) -> bool:
        """
        删除策略（数据库和COS）
        """
        if isinstance(strategy_id, str) and strategy_id.startswith("sys_"):
            raise ValueError("无法删除系统内置策略")

        if not str(strategy_id).isdigit():
            return False

        # 1. 查出 cos_key
        strategy = await self.get(strategy_id, user_id=user_id)
        if not strategy:
            return False

        # 2. 从 COS 删除
        cos_key = strategy.get("cos_key")
        if not self._local_mode and cos_key:
            try:
                self._cos.delete_file(cos_key)
            except Exception as e:
                logger.warning(f"删除COS文件失败 {cos_key}: {e}")

        # 3. 从 DB 删除
        with get_db() as session:
            session.execute(
                text("DELETE FROM strategies WHERE id = :sid AND user_id = :uid"),
                {"sid": int(strategy_id), "uid": user_id}
            )
        return True

    def list(
        self,
        user_id: str,
        category: str | None = None,
        search: str | None = None,
        tags: builtins.list[str] | None = None,
    ) -> builtins.list[dict[str, Any]]:
        # user_id is already a string (e.g., 'admin'), use directly
        with get_db() as session:
            has_cos_key = self._has_cos_key_column(session)
            cos_key_expr = "cos_key" if has_cos_key else "NULL::text as cos_key"
            sql = f"""
                SELECT id, name, description, status, cos_url, {cos_key_expr},
                       code_hash, tags, is_verified, execution_config, created_at, updated_at
                FROM strategies WHERE user_id = :uid AND status != '{_STATUS_ARCHIVED}'
            """
            rows = session.execute(text(sql), {"uid": user_id}).fetchall()
            return [
                {
                    "id": str(r[0]),
                    "name": r[1],
                    "description": r[2],
                    "status": r[3],
                    "cos_url": r[4],
                    "is_verified": bool(r[8]),
                    "execution_config": r[9] or {},
                    "tags": _parse_tags(r[7]),
                    "created_at": r[10].isoformat() if r[10] else None,
                    "updated_at": r[11].isoformat() if r[11] else None,
                }
                for r in rows
            ]


# ---------------------------------------------------------------------------
# 单例工厂
# ---------------------------------------------------------------------------
_instance: StrategyStorageService | None = None


def get_strategy_storage_service() -> StrategyStorageService:
    global _instance
    if _instance is None:
        _instance = StrategyStorageService()
    return _instance
