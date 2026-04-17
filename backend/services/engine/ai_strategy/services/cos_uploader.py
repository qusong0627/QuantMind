"""
本地策略保存服务（OSS Edition）
Local Strategy Storage Service

功能：
- 上传策略代码到本地存储
- 保存策略元数据到数据库
- 生成访问URL
"""

import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

import httpx
from sqlalchemy import text

logger = logging.getLogger(__name__)


class InvalidUserIdError(ValueError):
    """用户ID格式错误（应为整数语义字符串）。"""


try:
    from backend.shared.database_pool import get_db
except Exception:
    try:
        from shared.database_pool import get_db  # type: ignore
    except Exception:
        get_db = None  # type: ignore


class COSUploader:
    """
    本地存储上传服务 (OSS Edition)
    """

    def __init__(self):
        try:
            from pathlib import Path
            from dotenv import load_dotenv

            env_path = Path(__file__).resolve().parents[3] / ".env"
            if env_path.exists():
                load_dotenv(dotenv_path=env_path)
            else:
                load_dotenv()
        except Exception:
            pass

        self.storage_mode = "local"
        self.is_local_mode = True
        logger.info(f"COSUploader: storage_mode=local (OSS Edition)")

        default_root = "/app/data/strategies"
        storage_root = os.getenv("STORAGE_ROOT", default_root)

        self.local_storage_path = Path(storage_root)
        try:
            self.local_storage_path.mkdir(parents=True, exist_ok=True)
            logger.info(f"本地存储路径已就绪: {self.local_storage_path}")
        except Exception as e:
            logger.error(f"无法创建本地存储路径 {self.local_storage_path}: {e}")
            if "/tmp" not in str(self.local_storage_path):
                self.local_storage_path = Path("/tmp/quantmind_strategies")
                self.local_storage_path.mkdir(parents=True, exist_ok=True)
                logger.warning(f"降级到临时路径: {self.local_storage_path}")

    def _get_user_int_id(self, user_uuid: str) -> int | None:
        """从users表解析用户Int ID (Resolution from UUID/String to Int)"""
        if not get_db:
            logger.warning("DB Pool not available, skipping DB user resolution")
            return None

        try:
            with get_db() as session:
                # 1. 优先按业务用户ID查询（例如 00000001）
                result = session.execute(
                    text("SELECT id FROM users WHERE user_id = :uid"),
                    {"uid": user_uuid},
                ).scalar()

                if result:
                    return int(result)

                # 2. 再尝试按数据库主键 ID 查询（兼容传入纯数字）
                if user_uuid.isdigit():
                    result_by_pk = session.execute(
                        text("SELECT id FROM users WHERE id = :id"),
                        {"id": int(user_uuid)},
                    ).scalar()
                    if result_by_pk:
                        return int(result_by_pk)

                logger.warning(f"User UUID/ID {user_uuid} not found in users table")
                return None
        except Exception as e:
            logger.error(f"Failed to resolve user ID: {e}")
            return None

    def _save_to_db_strategies(
        self,
        user_int_id: int,
        user_uuid: str,
        strategy_name: str,
        code: str,
        metadata: dict = None,
    ) -> bool:
        """
        [Direct DB Storage]
        将策略直接存入 Strategies 表，以便回测服务直接读取
        """
        if not get_db:
            return False

        try:
            metadata = metadata or {}

            # 构造 config 包含代码
            config = metadata.get("config", {})
            if not isinstance(config, dict):
                config = {}
            config["code"] = code  # <--- 关键点：将代码存入JSON

            # 默认值
            now = datetime.now()

            with get_db() as session:
                # 检查是否已存在 (同名策略更新?) - 这里简单起见，如果同名则更新，或者直接插入新记录
                # 策略服务通常允许同名不同ID，但为了同步方便，我们先查一下
                # 简化逻辑：每次上传视为新版本或新策略？
                # Wizard生成通常是新的。这里我们执行 INSERT

                # 注意：Strategy表需要 name, strategy_type, status, user_id 等
                # 我们尽量填充默认值

                stmt = text("""
                    INSERT INTO strategies (
                        user_id, name, description, strategy_type, status, 
                        config, parameters, code, cos_url, code_hash, file_size,
                        tags, is_public, shared_users,
                        backtest_count, view_count, like_count, 
                        created_at, updated_at
                    ) VALUES (
                        :uid, :name, :desc, :stype, :status,
                        :config, :params, :code, :cos_url, :code_hash, :file_size,
                        '[]', false, '[]',
                        0, 0, 0, :now, :now
                    ) RETURNING id
                """)

                # 导出 config 和 params 为 JSON 字符串 (SQLAlchemy若使用JSON类型，传dict即可，但Text需dumps)
                # 这里假设是 PG JSONB，传 dict 即可 (Shared Pool 会处理 Adapter?)
                # 我们的 Shared Pool 是 text() SQL，可能需要 json.dumps
                import hashlib
                import json

                # 计算代码哈希和大小
                code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
                file_size = len(code.encode("utf-8"))

                result = session.execute(
                    stmt,
                    {
                        "uid": user_int_id,
                        "name": strategy_name,
                        "desc": f"Generated by AI Wizard ({now.strftime('%Y-%m-%d %H:%M')})",
                        "stype": "quantitative",  # 小写，匹配枚举值
                        "status": "draft",  # 明确设置为 draft 状态
                        "config": json.dumps(config),
                        "params": json.dumps(metadata.get("parameters", {})),
                        "code": code,  # 直接存储代码
                        "cos_url": None,  # 稍后在 upload_strategy 中更新
                        "code_hash": code_hash,
                        "file_size": file_size,
                        "now": now,
                    },
                )
                # Commit handled by context manager if not explicit (Wait, get_session does commit)
                # But get_db() is contextmanager yielding session?

                new_id = result.scalar()
                logger.info(f"[DirectDB] Strategy saved to DB. ID={new_id}")
                return True

        except Exception as e:
            logger.error(
                f"[DirectDB] Failed to save strategy to DB: {e}", exc_info=True
            )
            return False

    async def upload_strategy(
        self, user_id: str, strategy_id: str, code: str, metadata: dict | None = None
    ) -> dict[str, str]:
        """
        上传策略文件到本地存储 (OSS Edition)
        """
        return await self._upload_to_local(user_id, strategy_id, code, metadata)

    async def upload_pool_file(
        self,
        user_id: str,
        pool_id: str,
        content: str,
        fmt: str = "json",
        timestamp: str | None = None,
    ) -> dict[str, str]:
        """
        上传股票池文件到本地存储 (OSS Edition)
        """
        return await self._upload_pool_to_local(
            user_id, pool_id, content, fmt, timestamp
        )

    async def _upload_to_local(
        self, user_id: str, strategy_id: str, code: str, metadata: dict | None = None
    ) -> dict[str, str]:
        """本地保存：保存到本地文件系统"""
        try:
            # 创建用户目录
            user_dir = self.local_storage_path / f"user_{user_id}"
            user_dir.mkdir(parents=True, exist_ok=True)

            # 策略文件路径
            strategy_file = user_dir / f"{strategy_id}.py"

            # 保存代码
            with open(strategy_file, "w", encoding="utf-8") as f:
                f.write(code)

            # 保存元数据（如果有）
            if metadata:
                metadata_file = user_dir / f"{strategy_id}.json"
                import json

                with open(metadata_file, "w", encoding="utf-8") as f:
                    json.dump(metadata, f, ensure_ascii=False, indent=2)

            # 计算文件信息
            file_size = len(code.encode("utf-8"))
            code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()

            # 生成 file URL（仅用于本地模式下的可访问路径；生产建议使用对象存储）
            object_key = f"user_strategies/{user_id}/{strategy_id}/strategy.py"
            file_url = f"file://{strategy_file.absolute()}"

            logger.info(
                f"策略已保存到本地: {strategy_file} "
                f"(用户: {user_id}, 策略ID: {strategy_id}, 大小: {file_size}字节)"
            )

            return {
                "url": file_url,
                "object_key": object_key,
                "file_size": file_size,
                "code_hash": code_hash,
            }

        except Exception as e:
            logger.error(f"本地保存失败: {e}", exc_info=True)
            raise RuntimeError(f"策略保存失败: {e}")

    async def _upload_pool_to_local(
        self,
        user_id: str,
        pool_id: str,
        content: str,
        fmt: str,
        timestamp: str | None = None,
    ) -> dict[str, str]:
        """本地保存股票池文件,支持时间戳文件夹"""
        try:
            # 如果没有提供timestamp,使用当前时间
            if not timestamp:
                from datetime import datetime

                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

            # 创建用户目录/时间戳文件夹
            user_dir = self.local_storage_path / f"user_{user_id}" / timestamp
            user_dir.mkdir(parents=True, exist_ok=True)

            file_name = f"stock_pool.{fmt}"
            pool_file = user_dir / file_name

            with open(pool_file, "w", encoding="utf-8") as f:
                f.write(content)

            file_size = len(content.encode("utf-8"))
            code_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

            # 对象键包含时间戳文件夹
            object_key = f"user_pools/{user_id}/{timestamp}/{file_name}"
            # 相对路径供大模型使用
            relative_path = f"{timestamp}/{file_name}"
            file_url = f"file://{pool_file.absolute()}"

            logger.info(
                f"股票池已保存: {pool_file} (用户: {user_id}, 时间戳: {timestamp}, 格式: {fmt})"
            )

            return {
                "url": file_url,
                "object_key": object_key,
                "relative_path": relative_path,
                "file_size": file_size,
                "code_hash": code_hash,
            }
        except Exception as e:
            logger.error(f"本地保存股票池失败: {e}", exc_info=True)
            raise RuntimeError(f"股票池保存失败: {e}")

    async def delete_object(self, url: str, object_key: str | None = None) -> bool:
        try:
            if url.startswith("file://"):
                file_path = url.replace("file://", "")
                Path(file_path).unlink(missing_ok=True)
                return True
            return False
        except Exception as e:
            logger.error(f"删除对象失败: {e}", exc_info=True)
            return False

    async def read_object(
        self, url: str | None = None, object_key: str | None = None
    ) -> str:
        try:
            # 优先使用 url 参数
            if url and url.startswith("file://"):
                file_path = url.replace("file://", "")
                with open(file_path, encoding="utf-8") as f:
                    return f.read()

            # 如果提供了 object_key，从本地存储路径读取
            if object_key:
                # object_key 格式如: user_pools/user_id/timestamp/stock_pool.txt
                # 需要映射到实际路径: {local_storage_path}/user_{user_id}/timestamp/stock_pool.txt
                if object_key.startswith("user_pools/"):
                    # 转换: user_pools/{user_id}/{timestamp}/{file} -> user_{user_id}/{timestamp}/{file}
                    parts = object_key.split("/")
                    if len(parts) >= 4:
                        user_id = parts[1]
                        timestamp = parts[2]
                        file_name = parts[3]
                        relative_path = f"user_{user_id}/{timestamp}/{file_name}"
                        file_path = self.local_storage_path / relative_path
                        if file_path.exists():
                            with open(file_path, encoding="utf-8") as f:
                                return f.read()
                        else:
                            logger.warning(f"股票池文件不存在: {file_path}")
                            return ""
                else:
                    # 其他 object_key 格式，直接拼接
                    file_path = self.local_storage_path / object_key
                    if file_path.exists():
                        with open(file_path, encoding="utf-8") as f:
                            return f.read()
                    else:
                        logger.warning(f"文件不存在: {file_path}")
                        return ""

            return ""
        except Exception as e:
            logger.error(f"读取对象失败: {e}", exc_info=True)
            raise RuntimeError(f"读取对象失败: {e}")

    async def download_strategy(self, url: str) -> str:
        """
        下载策略代码

        Args:
            url: 策略URL

        Returns:
            策略代码字符串
        """
        if url.startswith("file://"):
            # local 模式：从本地读取
            file_path = url.replace("file://", "")
            with open(file_path, encoding="utf-8") as f:
                return f.read()
        else:
            # 真实模式：从COS下载
            # TODO: 实现COS下载
            raise NotImplementedError("COS下载功能待实现")

    async def list_objects(self, prefix: str) -> list[dict[str, Any]]:
        """
        列出指定前缀下的所有对象

        Args:
            prefix: 对象前缀 (例如: user_strategies/user_123/)

        Returns:
            [
                {
                    "key": str,
                    "size": int,
                    "last_modified": datetime
                }
            ]
        """
        results = []
        try:
            if self.is_local_mode:
                # local 模式：扫描本地目录
                # 本地存储结构: {STORAGE_ROOT}/user_{user_id}/{strategy_id}.py
                # Cloud Key结构: user_strategies/{user_id}/{strategy_id}/strategy.py

                # 尝试解析 prefix 中的 user_id
                # prefix 格式: user_strategies/{user_id}/
                parts = prefix.strip("/").split("/")
                if len(parts) >= 2 and parts[0] == "user_strategies":
                    user_id = parts[1]
                    target_dir = self.local_storage_path / f"user_{user_id}"
                else:
                    # Fallback to direct mapping if pattern doesn't match
                    target_dir = self.local_storage_path / prefix.strip("/")

                if not target_dir.exists():
                    return []

                # 扫描所有 .py 文件 (支持 flat结构 {strategy_id}.py 和 nested结构 {strategy_id}/strategy.py)
                for path in target_dir.rglob("*.py"):
                    stat = path.stat()

                    # 确定 strategy_id
                    rel_path = path.relative_to(target_dir)
                    parts = rel_path.parts

                    if len(parts) == 1:
                        # Flat: {strategy_id}.py
                        strategy_id = path.stem
                    elif len(parts) == 2 and parts[1] == "strategy.py":
                        # Nested: {strategy_id}/strategy.py
                        strategy_id = parts[0]
                    else:
                        # 其他结构，简单取文件名或通过路径推断，这里暂取 stem
                        strategy_id = path.stem

                    # 构造符合 Cloud Key 格式的 key
                    key = f"user_strategies/{user_id}/{strategy_id}/strategy.py"

                    results.append(
                        {
                            "key": key,
                            "size": stat.st_size,
                            "last_modified": datetime.fromtimestamp(stat.st_mtime),
                        }
                    )

        except Exception as e:
            logger.error(f"List objects failed: {e}", exc_info=True)
            # 扫描失败不抛出异常，返回空列表以免阻断
            return []

        return results


# 单例
_uploader_instance: COSUploader | None = None


def get_cos_uploader() -> COSUploader:
    """获取COS上传器单例"""
    global _uploader_instance
    if _uploader_instance is None:
        _uploader_instance = COSUploader()
    return _uploader_instance


# 策略保存服务（结合COS和数据库）
class StrategyStorageService:
    """策略存储服务（完整流程）"""

    def __init__(self):
        # 使用 STORAGE_MODE 决定 local 或 cos
        self.cos_uploader = get_cos_uploader()

    async def save_strategy(
        self, user_id: str, strategy_name: str, code: str, metadata: dict
    ) -> dict:
        """
        保存策略到云端并记录数据库

        Args:
            user_id: 用户ID
            strategy_name: 策略名称
            code: 策略代码
            metadata: 元数据（条件、风险配置等）

        Returns:
            {
                "strategy_id": str,
                "cloud_url": str,
                "access_path": str,
                "file_size": int,
                "code_hash": str
            }
        """
        try:
            # 生产统一要求：user_id 以整数语义传递，避免跨服务类型歧义。
            normalized_user_id = str(int(str(user_id).strip()))
        except Exception as exc:
            raise InvalidUserIdError(
                f"user_id 必须为整数类型字符串，当前值: {user_id}"
            ) from exc

        # 生成策略ID
        strategy_id = str(uuid4())

        # 确保 name 进入 metadata 以便 upload_strategy 使用
        metadata = metadata or {}
        if "name" not in metadata:
            metadata["name"] = strategy_name

        # 上传到COS
        upload_result = await self.cos_uploader.upload_strategy(
            user_id=normalized_user_id,
            strategy_id=strategy_id,
            code=code,
            metadata=metadata,
        )

        # 2026-02-14 统一架构：通过 strategy-service API 创建策略，不再直接写库
        sync_enabled = os.getenv("STRATEGY_SYNC_ENABLED", "true").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        strategy_service_url = os.getenv(
            "STRATEGY_SERVICE_URL", "http://strategy-service:8003"
        )

        # 准备创建策略的 Payload (符合 StrategyCreate schema)
        tags = metadata.get("tags") or []
        if isinstance(tags, str):
            tags = [tags]

        # 提取关键回测参数，确保回测一致性
        # 这些参数由 AI 向导的 Step 3, 4 提供
        strategy_config = metadata.get("config") or {}
        benchmark = (
            metadata.get("risk_config", {}).get("marketIndexSymbol") or "SH000300"
        )
        # 兼容性处理：如果 benchmark 只有代码（如 000300），补全后缀
        if benchmark == "000300":
            benchmark = "SH000300"
        elif benchmark == "000905":
            benchmark = "SH000905"
        elif benchmark == "000852":
            benchmark = "SH000852"

        payload = {
            "name": strategy_name,
            "description": metadata.get("description")
            or f"Generated by AI Wizard ({datetime.now().strftime('%Y-%m-%d %H:%M')})",
            "strategy_type": "quantitative",
            "config": {
                "code": code,
                "cos_url": upload_result["url"],
                "code_hash": upload_result["code_hash"],
                "file_size": upload_result["file_size"],
                "source": "ai_wizard",
                "benchmark": benchmark,
                "universe": metadata.get("stock_pool", {})
                .get("summary", {})
                .get("universeTotal")
                or "csi300",
                "strategy_type": "TopkDropout",  # 默认 Qlib 类型
                **strategy_config,
            },
            "parameters": metadata.get("parameters")
            or metadata.get("risk_config")
            or {},
            "tags": tags,
            "is_public": bool(metadata.get("is_public", False)),
        }

        # 获取用户 Int ID (strategy-service 需要整数类型的 user_id)
        user_int_id = self.cos_uploader._get_user_int_id(normalized_user_id)
        if user_int_id is None:
            # 如果无法从数据库解析，尝试直接转换 (兼容性处理)
            try:
                user_int_id = int(normalized_user_id)
            except ValueError:
                raise RuntimeError(f"无法解析用户ID为整数类型: {normalized_user_id}")

        if not sync_enabled:
            logger.info("STRATEGY_SYNC_ENABLED=false, skip strategy-service sync")
            return {
                "strategy_id": strategy_id,
                "cloud_url": upload_result["url"],
                "access_path": f"/user-center/strategies/{strategy_id}",
                "file_size": upload_result["file_size"],
                "code_hash": upload_result["code_hash"],
            }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                # 注意：由于 strategy-service 有身份验证中间件，这里需要模拟或传递 token
                # 在内部服务间调用中，通常使用内部密钥或信任机制。
                # 暂时通过 Header 传递当前 user_id，由 nginx-gateway 或服务中间件识别
                response = await client.post(
                    f"{strategy_service_url}/api/v1/strategies",
                    json=payload,
                    headers={"X-User-Id": str(user_int_id)},
                )

                if response.status_code != 200:
                    logger.error(f"调用 strategy-service 失败: {response.text}")
                    # 触发补偿：删除已上传的 COS 文件
                    try:
                        await self.cos_uploader.delete_object(
                            url=upload_result["url"],
                            object_key=upload_result["object_key"],
                        )
                        logger.info(
                            f"已补偿删除孤儿文件: {upload_result['object_key']}"
                        )
                    except Exception as delete_err:
                        logger.error(f"补偿删除失败: {delete_err}")

                    response.raise_for_status()

                data = response.json()
                # 假设返回结构是 { "data": { "id": ... } } 或直接是对象
                new_strategy = data.get("data") or data
                strategy_db_id = new_strategy.get("id")

                logger.info(
                    f"策略已通过 strategy-service 创建: id={strategy_db_id}, user={user_int_id}"
                )

                return {
                    "strategy_id": str(strategy_db_id),
                    "cloud_url": upload_result["url"],
                    "access_path": f"/user-center/strategies/{strategy_db_id}",
                    "file_size": upload_result["file_size"],
                    "code_hash": upload_result["code_hash"],
                }
        except Exception as e:
            logger.error(f"集成保存策略失败: {e}", exc_info=True)
            # 如果是请求抛出的异常（如超时），也尝试清理
            try:
                await self.cos_uploader.delete_object(
                    url=upload_result["url"], object_key=upload_result["object_key"]
                )
                logger.info(
                    f"已因请求异常补偿删除孤儿文件: {upload_result['object_key']}"
                )
            except:
                pass
            raise RuntimeError(f"策略保存同步到服务失败: {e}")


def _json(payload: object | None) -> str:
    return json.dumps(payload or {}, ensure_ascii=False)


# 单例
_storage_service_instance: StrategyStorageService | None = None


def get_strategy_storage_service() -> StrategyStorageService:
    """获取存储服务单例"""
    global _storage_service_instance
    if _storage_service_instance is None:
        _storage_service_instance = StrategyStorageService()
    return _storage_service_instance
