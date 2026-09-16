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
        _sync_engine = create_engine(
            db_url, pool_size=5, max_overflow=2, pool_pre_ping=True
        )
        _sync_session_factory = _sessionmaker(
            bind=_sync_engine, autocommit=False, autoflush=False
        )
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
# 常量（状态词表唯一实现 = shared/strategy_lifecycle.py，禁止本地副本）
# ---------------------------------------------------------------------------
_URL_TTL = int(os.getenv("COS_STRATEGY_URL_TTL", "3600"))
_STRATEGY_FOLDER = "user_strategies"

from backend.shared.strategy_lifecycle import (  # noqa: E402
    STATUS_ARCHIVED as _STATUS_ARCHIVED,
)
from backend.shared.strategy_lifecycle import (  # noqa: E402
    STATUS_DRAFT as _STATUS_DRAFT,
)
from backend.shared.strategy_lifecycle import (  # noqa: E402
    STATUS_VERIFIED as _STATUS_VERIFIED,
)
from backend.shared.strategy_lifecycle import (  # noqa: E402
    IllegalTransitionError,
    StrategyLockedError,
    VersionConflictError,
    assert_transition,
    is_running,
    normalize_status,
    requires_version_bump,
)


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
    """（兼容保留）状态归一委托唯一实现 strategy_lifecycle.normalize_status。"""
    return normalize_status(status)


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
        cached = getattr(self, "_has_cos_key_col", None)
        if cached is not None:
            return cached
        try:
            exists = session.execute(
                text("""
                    SELECT EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_name = 'strategies' AND column_name = 'cos_key'
                    )
                    """)
            ).scalar()
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
                logger.warning(
                    "COS client not initialized (missing credentials), falling back to local mode"
                )
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
        expected_version: int | None = None,
    ) -> str:
        """INSERT or UPDATE strategies 表（T-P3-01：补丁语义 + 版本递增 + 参数锁）。"""
        if get_db is None:
            raise RuntimeError("数据库不可用")

        now = datetime.now(timezone.utc)
        tags = _parse_tags(metadata.get("tags", []))
        description = (
            metadata.get("description") or f"Updated ({now.strftime('%Y-%m-%d %H:%M')})"
        )
        strategy_type = metadata.get("strategy_type") or "CUSTOM"
        status = normalize_status(metadata.get("status"))
        config = metadata.get("config") or {}
        parameters = metadata.get("parameters") or {}
        execution_config = metadata.get("execution_config") or {"max_buy_drop": -0.03}
        is_public = bool(metadata.get("is_public", False))

        with get_db() as session:
            has_cos_key = self._has_cos_key_column(session)

            uid_int = _ensure_int_user_id(user_id)
            params = {
                "uid": uid_int,
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
                "tags": list(tags) if isinstance(tags, (list, tuple)) else [],
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
                # UPDATE（T-P3-01）：
                # ① 补丁语义——description/config/parameters/execution_config 仅在显式提供时覆盖
                #   （修复"局部更新把 execution_config/config 抹成默认值"的族缺陷）；
                # ② 版本递增——代码/参数/执行配置实质变化才 version+1；
                # ③ 参数锁——运行中（SIM/LIVE）策略改内容必须携带 expected_version 显式升版。
                sid = int(strategy_id)
                params["sid"] = sid
                current = session.execute(
                    text(
                        "SELECT status, version, code_hash, parameters, execution_config "
                        "FROM strategies WHERE id = :sid AND user_id = :uid FOR UPDATE"
                    ),
                    {"sid": sid, "uid": uid_int},
                ).fetchone()
                if current is None:
                    raise StrategyLockedError(f"策略不存在或无权更新: id={strategy_id}")

                cur_status = normalize_status(current[0])
                cur_version = int(current[1] or 1)
                provided_params = metadata.get("parameters") is not None
                provided_exec = metadata.get("execution_config") is not None
                provided_config = metadata.get("config") is not None
                provided_desc = metadata.get("description") is not None

                content_changed = bool(
                    hash_val != str(current[2] or "")
                    or (provided_params and parameters != (current[3] or {}))
                    or (provided_exec and execution_config != (current[4] or {}))
                )
                new_version = cur_version
                if content_changed:
                    if requires_version_bump(cur_status):
                        if expected_version is None:
                            raise StrategyLockedError(
                                f"策略处于运行中（{cur_status}），修改内容必须显式升版本："
                                "请携带 expected_version=当前版本（参数锁，T-P3-01）"
                            )
                        if int(expected_version) != cur_version:
                            raise VersionConflictError(
                                f"版本冲突：期望 {expected_version}，当前 {cur_version}"
                                "（策略已被其他操作修改，请刷新后重试）"
                            )
                    elif expected_version is not None and int(expected_version) != cur_version:
                        raise VersionConflictError(
                            f"版本冲突：期望 {expected_version}，当前 {cur_version}"
                        )
                    new_version = cur_version + 1

                set_parts = [
                    "name = :name",
                    "code = :code",
                    "cos_url = :cos_url",
                    "code_hash = :code_hash",
                    "file_size = :file_size",
                    "tags = :tags",
                    "version = :version",
                    "updated_at = :now",
                ]
                if has_cos_key:
                    set_parts.insert(2, "cos_key = :cos_key")
                if provided_desc:
                    set_parts.append("description = :desc")
                if provided_config:
                    set_parts.append("config = CAST(:config AS jsonb)")
                if provided_params:
                    set_parts.append("parameters = CAST(:params AS jsonb)")
                if provided_exec:
                    set_parts.append("execution_config = CAST(:exec_config AS jsonb)")
                params["version"] = new_version
                sql = (
                    f"UPDATE strategies SET {', '.join(set_parts)} "
                    "WHERE id = :sid AND user_id = :uid"
                )
                result = session.execute(text(sql), params)
                if (result.rowcount or 0) == 0:
                    raise StrategyLockedError(f"策略更新未命中记录: id={strategy_id}")
                if new_version != cur_version:
                    logger.info(
                        "[Strategy] 版本递增 id=%s %s→%s status=%s",
                        strategy_id,
                        cur_version,
                        new_version,
                        cur_status,
                    )
                return strategy_id
            else:
                # INSERT
                sql = f"""
                    INSERT INTO strategies (
                        user_id, name, description, strategy_type, status,
                        config, parameters, execution_config, code, cos_url, 
                        {"cos_key," if has_cos_key else ""}
                        code_hash, file_size,
                        tags, is_public, shared_users,
                        backtest_count, view_count, like_count, version, is_verified,
                        created_at, updated_at
                    ) VALUES (
                        :uid, :name, :desc, :stype, :status,
                        CAST(:config AS jsonb), CAST(:params AS jsonb), CAST(:exec_config AS jsonb),
                        :code, :cos_url,
                        {":cos_key," if has_cos_key else ""}
                        :code_hash, :file_size,
                        :tags, :is_public, CAST('[]' AS jsonb),
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
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        """保存/更新策略。

        ``expected_version``（T-P3-01 参数锁）：运行中（SIM/LIVE）策略修改内容时必填，
        且须等于当前版本（读 get()["version"] 获得）；非运行态可选（乐观并发）。
        """
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

        db_id = self._db_upsert(
            user_id,
            strategy_id,
            name,
            code,
            cos_key,
            cos_url,
            file_size,
            hash_val,
            metadata,
            expected_version=expected_version,
        )
        return {
            "id": db_id,
            "cos_key": cos_key,
            "cos_url": cos_url,
            "code_hash": hash_val,
            "file_size": file_size,
        }

    async def get(
        self, strategy_id: Any, user_id: str | None = None, resolve_code: bool = False
    ) -> dict[str, Any] | None:
        # 1. 检查是否为系统内置策略 (sys_ 开头)
        if isinstance(strategy_id, str) and strategy_id.startswith("sys_"):
            try:
                from backend.services.engine.qlib_app.services.strategy_templates import (
                    get_template_by_id,
                )

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
                        "parameters": {
                            "strategy_type": real_template_id,
                            "topk": 50,
                            "signal": "<PRED>",
                        },
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
                       is_verified, execution_config, version
                FROM strategies
                WHERE id = :sid AND status != '{_STATUS_ARCHIVED}'
            """
            params = {"sid": strategy_id}
            if user_id:
                params["uid"] = _ensure_int_user_id(user_id)
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
                # T-P3-01：状态机与参数锁需要状态/版本（此前 SELECT 取了 status 却不返回）
                "status": normalize_status(row[5]),
                "version": int(row[19] or 1),
            }

    async def mark_as_verified(self, strategy_id: str, user_id: str) -> bool:
        """回测验证通过标记（T-P3-04 状态一致化）。

        - ``is_verified = TRUE``（回测证据位，activate/手动执行/前端按钮沿用）；
        - **状态机联动**：DRAFT → VERIFIED（否则 is_verified=True 而 status=DRAFT 的
          分裂会让 T-P3-01 的 SIM 启动门禁误拒已回测策略）；已在 VERIFIED/SIM/LIVE
          不降级，ARCHIVED 保持归档；
        - 行数诚实：目标不存在 → False（此前无条件 True）。
        """
        sid_text = str(strategy_id or "").strip()
        if not sid_text.isdigit():
            return False
        uid_int = _ensure_int_user_id(user_id)
        with get_db() as session:
            row = session.execute(
                text(
                    "SELECT status FROM strategies "
                    "WHERE id = :sid AND user_id = :uid FOR UPDATE"
                ),
                {"sid": int(sid_text), "uid": uid_int},
            ).fetchone()
            if row is None:
                return False
            current = normalize_status(row[0])
            new_status = _STATUS_VERIFIED if current == _STATUS_DRAFT else row[0]
            result = session.execute(
                text(
                    "UPDATE strategies SET is_verified = TRUE, status = :status, "
                    "updated_at = :now WHERE id = :sid AND user_id = :uid"
                ),
                {
                    "status": new_status,
                    "now": datetime.now(timezone.utc),
                    "sid": int(sid_text),
                    "uid": uid_int,
                },
            )
            if (result.rowcount or 0) == 0:
                return False
            if current == _STATUS_DRAFT:
                logger.info(
                    "[Strategy] 回测验证通过：id=%s DRAFT→VERIFIED（T-P3-04）", sid_text
                )
            return True

    def update_lifecycle_status(
        self, strategy_id: Any, user_id: str, status: str
    ) -> bool:
        """更新策略生命周期状态（T-P3-01：经状态机校验的合法迁移）。

        - 同状态回写 = 幂等成功（no-op）；
        - 非法迁移（跨级/回退）→ 告警并返回 False（不静默改写）；
        - 命中且合法 → True，行内 status 写为规范新词表（VERIFIED/SIM/LIVE/…）。
        """
        sid_text = str(strategy_id or "").strip()
        if not sid_text.isdigit():
            logger.warning(
                "update_lifecycle_status skip non-numeric strategy_id=%s", sid_text
            )
            return False
        target = normalize_status(status)
        uid_int = _ensure_int_user_id(user_id)
        with get_db() as session:
            row = session.execute(
                text(
                    "SELECT status FROM strategies "
                    "WHERE id = :sid AND user_id = :uid FOR UPDATE"
                ),
                {"sid": int(sid_text), "uid": uid_int},
            ).fetchone()
            if row is None:
                return False
            current = normalize_status(row[0])
            if current == target:
                return True  # 幂等 no-op（如重复启动/重复停止）
            try:
                assert_transition(current, target)
            except IllegalTransitionError as exc:
                logger.warning(
                    "[Strategy] 拒绝非法状态迁移 id=%s: %s", sid_text, exc
                )
                return False
            result = session.execute(
                text(
                    "UPDATE strategies SET status = :status, updated_at = :now "
                    "WHERE id = :sid AND user_id = :uid AND status = :current"
                ),
                {
                    "status": target,
                    "now": datetime.now(timezone.utc),
                    "sid": int(sid_text),
                    "uid": uid_int,
                    "current": row[0],
                },
            )
            return bool((result.rowcount or 0) > 0)

    async def delete(self, strategy_id: Any, user_id: str) -> bool:
        """删除策略（数据库 + COS）。T-P3-02：行数诚实 + 运行中守卫 + 可清归档。

        - 运行中（SIM/LIVE）：拒绝（ValueError，上层 400 直出话术）——防悬空引用；
        - 归档（ARCHIVED）可清理（此前经 get() 预检查、归档行永远删不掉）；
        - DELETE 未命中行 → False（不再无条件返回 True）。
        """
        if isinstance(strategy_id, str) and strategy_id.startswith("sys_"):
            raise ValueError("无法删除系统内置策略")

        if not str(strategy_id).isdigit():
            return False

        uid_int = _ensure_int_user_id(user_id)
        with get_db() as session:
            has_cos_key = self._has_cos_key_column(session)
            key_expr = "cos_key" if has_cos_key else "NULL::text AS cos_key"
            row = session.execute(
                text(
                    f"SELECT status, {key_expr} FROM strategies "
                    "WHERE id = :sid AND user_id = :uid FOR UPDATE"
                ),
                {"sid": int(strategy_id), "uid": uid_int},
            ).fetchone()
            if row is None:
                return False

            cur_status = normalize_status(row[0])
            if is_running(cur_status):
                raise ValueError(
                    f"策略正在运行（{cur_status}），请先停止后再删除"
                )

            # COS 删除（文件删失败仅告警；随后删行）
            cos_key = row[1]
            if not self._local_mode and cos_key:
                try:
                    self._cos.delete_file(cos_key)
                except Exception as e:
                    logger.warning(f"删除COS文件失败 {cos_key}: {e}")

            result = session.execute(
                text("DELETE FROM strategies WHERE id = :sid AND user_id = :uid"),
                {"sid": int(strategy_id), "uid": uid_int},
            )
            if (result.rowcount or 0) == 0:
                return False
        return True

    def list(
        self,
        user_id: str,
        category: str | None = None,
        search: str | None = None,
        tags: builtins.list[str] | None = None,
        market: str | None = None,
        include_templates: bool = False,
    ) -> builtins.list[dict[str, Any]]:
        # strategies.user_id 为整数（users.id），需先解析业务 user_id（如 'admin'）
        # 统一管理：category/search/tags 在此层生效，避免上层各自为政
        try:
            uid_int = _ensure_int_user_id(user_id)
        except ValueError as exc:
            logger.warning("list: user_id=%r 无法解析（%s），返回空列表", user_id, exc)
            return []
        with get_db() as session:
            has_cos_key = self._has_cos_key_column(session)
            cos_key_expr = "cos_key" if has_cos_key else "NULL::text as cos_key"
            where = ["user_id = :uid", f"status != '{_STATUS_ARCHIVED}'"]
            params: dict[str, Any] = {"uid": uid_int}
            if category:
                where.append(
                    "(tags::text ILIKE :cat_like OR parameters::text ILIKE :cat_like OR config::text ILIKE :cat_like)"
                )
                params["cat_like"] = f"%{category}%"
            if search:
                where.append(
                    "(name ILIKE :search OR description ILIKE :search OR code ILIKE :search)"
                )
                params["search"] = f"%{search}%"
            if tags:
                for idx, t in enumerate(tags):
                    key = f"tag_{idx}"
                    where.append(f"tags::text ILIKE :{key}")
                    params[key] = f"%{t}%"
            # 市场过滤(市场存 parameters.jsonb.market;历史无 market 行一律视为 A 股)
            # 必须并入 where 列表:此前在 ORDER BY 之后追加 AND 导致 SQL 语法错误
            if market:
                mkt = str(market).upper()
                if mkt in ("A", "CN"):
                    where.append(
                        "(parameters->>'market' IS NULL"
                        " OR UPPER(parameters->>'market') IN ('A','CN'))"
                    )
                else:
                    where.append("UPPER(parameters->>'market') = :mkt")
                    params["mkt"] = mkt
            where_sql = " AND ".join(where)
            sql = f"""
                SELECT id, name, description, status, cos_url, {cos_key_expr},
                       code_hash, tags, is_verified, execution_config, created_at, updated_at,
                       parameters, config
                FROM strategies WHERE {where_sql} ORDER BY updated_at DESC
            """
            rows = session.execute(text(sql), params).fetchall()
            items = [
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
                    "parameters": r[12] or {},
                    "config": r[13] or {},
                }
                for r in rows
            ]
            if include_templates:
                try:
                    from backend.services.engine.qlib_app.services.strategy_templates import (
                        get_all_templates,
                    )

                    for t in get_all_templates():
                        items.append(
                            {
                                "id": f"sys_{t.id}",
                                "name": t.name,
                                "description": t.description,
                                "status": "ACTIVE",
                                "cos_url": None,
                                "is_verified": True,
                                "execution_config": {},
                                "tags": ["system", "template", t.category],
                                "created_at": None,
                                "updated_at": None,
                                "parameters": {"strategy_type": t.id},
                                "config": {},
                                "is_system": True,
                            }
                        )
                except Exception as e:
                    logger.warning(f"include_templates failed: {e}")
            return items


# ---------------------------------------------------------------------------
# 单例工厂
# ---------------------------------------------------------------------------
_instance: StrategyStorageService | None = None


def get_strategy_storage_service() -> StrategyStorageService:
    global _instance
    if _instance is None:
        _instance = StrategyStorageService()
    return _instance
