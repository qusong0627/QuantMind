"""
Local Storage Service (OSS Edition)
Provides upload/download/delete utilities for local file storage.
"""

import json
import hashlib
import os
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from dotenv import load_dotenv
from loguru import logger

load_dotenv()


class LocalStorageService:
    """Local file storage service for OSS edition."""

    def __init__(self):
        # 优先使用环境变量 STORAGE_ROOT，否则使用 /data（Docker 挂载路径）
        storage_root = os.getenv("STORAGE_ROOT", "/data")
        if not os.path.isabs(storage_root):
            storage_root = os.path.abspath(os.path.join(os.getcwd(), storage_root))
        self.local_storage_root = storage_root
        if not os.path.exists(self.local_storage_root):
            os.makedirs(self.local_storage_root, exist_ok=True)
        logger.info(
            f"LocalStorageService initialized - Root: {self.local_storage_root}"
        )

    def is_connected(self) -> bool:
        return True

    def _build_key(self, file_name: str, folder: str) -> str:
        today = datetime.utcnow().strftime("%Y/%m/%d")
        unique_prefix = uuid.uuid4().hex[:8]
        from urllib.parse import quote

        safe_name = quote(file_name)
        return f"{folder}/{today}/{unique_prefix}-{safe_name}"

    def upload_file(
        self,
        file_data: bytes | str,
        file_name: str,
        folder: str = "uploads",
        content_type: str | None = None,
        use_exact_key: bool = False,
    ) -> dict[str, Any]:

        if use_exact_key:
            key = file_name
        else:
            key = self._build_key(file_name, folder)

        if isinstance(file_data, str) and os.path.exists(file_data):
            with open(file_data, "rb") as f:
                content = f.read()
        elif isinstance(file_data, str):
            content = file_data.encode("utf-8")
        else:
            content = file_data

        try:
            local_path = os.path.join(self.local_storage_root, key)
            os.makedirs(os.path.dirname(local_path), exist_ok=True)

            with open(local_path, "wb") as f:
                f.write(content)

            file_url = f"/{key}"
            file_md5 = hashlib.md5(content).hexdigest()

            logger.info(f"Local storage upload success: {local_path}")
            return {
                "success": True,
                "file_key": key,
                "file_name": os.path.basename(key),
                "file_url": file_url,
                "file_size": len(content),
                "upload_time": datetime.utcnow().isoformat(),
                "file_md5": file_md5,
            }
        except Exception as e:
            logger.error(f"Local storage upload failed: {e}")
            return {"success": False, "error": str(e)}

    def delete_file(self, key: str) -> dict[str, Any]:
        try:
            local_path = os.path.join(self.local_storage_root, key)
            if os.path.exists(local_path):
                os.remove(local_path)
                logger.info(f"Local storage delete success: {local_path}")
                return {"success": True, "delete_time": datetime.utcnow().isoformat()}
            else:
                return {"success": False, "error": "File not found"}
        except Exception as e:
            logger.error(f"Local storage delete failed: {e}")
            return {"success": False, "error": str(e)}

    def get_file_info(self, key: str) -> dict[str, Any]:
        try:
            local_path = os.path.join(self.local_storage_root, key)
            if os.path.exists(local_path):
                stat = os.stat(local_path)
                return {
                    "success": True,
                    "file_key": key,
                    "file_url": f"/{key}",
                    "raw_url": f"/{key}",
                    "file_size": stat.st_size,
                    "last_modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    "etag": "",
                    "content_type": "application/octet-stream",
                }
            else:
                return {"success": False, "error": "File not found"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_presigned_url(self, key: str, expired: int = 3600) -> str | None:
        return f"/{key}"

    def get_object_bytes(self, key: str) -> bytes | None:
        try:
            local_path = os.path.join(self.local_storage_root, key)
            if os.path.exists(local_path):
                with open(local_path, "rb") as f:
                    return f.read()
            return None
        except Exception:
            return None

    def get_object_text(self, key: str, encoding: str = "utf-8") -> str | None:
        content = self.get_object_bytes(key)
        if content is None:
            return None
        try:
            return content.decode(encoding)
        except Exception:
            return None

    def get_object_json(
        self, key: str, encoding: str = "utf-8"
    ) -> dict[str, Any] | None:
        text = self.get_object_text(key, encoding=encoding)
        if not text:
            return None
        try:
            data = json.loads(text)
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def list_files(
        self, folder: str = "uploads", max_keys: int = 100, prefix: str | None = None
    ) -> dict[str, Any]:
        try:
            target_folder = folder
            if prefix:
                if "/" in prefix:
                    target_folder = os.path.join(folder, os.path.dirname(prefix))

            search_root = os.path.join(self.local_storage_root, target_folder)
            if not os.path.exists(search_root):
                return {"success": True, "files": [], "total_count": 0}

            files = []
            for root, dirs, filenames in os.walk(search_root):
                for filename in filenames:
                    full_path = os.path.join(root, filename)
                    rel_key = os.path.relpath(full_path, self.local_storage_root)

                    if prefix and not rel_key.startswith(prefix):
                        continue

                    stat = os.stat(full_path)
                    files.append(
                        {
                            "file_key": rel_key,
                            "file_name": filename,
                            "file_url": f"/{rel_key}",
                            "file_size": stat.st_size,
                            "last_modified": datetime.fromtimestamp(
                                stat.st_mtime
                            ).isoformat(),
                        }
                    )
                    if len(files) >= max_keys:
                        break
                if len(files) >= max_keys:
                    break

            return {"success": True, "files": files, "total_count": len(files)}
        except Exception as e:
            logger.error(f"Local storage list_files failed: {e}")
            return {"success": False, "error": str(e)}


def get_cos_service() -> LocalStorageService:
    return LocalStorageService()


TencentCOSService = LocalStorageService
