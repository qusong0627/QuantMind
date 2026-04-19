"""
文件上传服务模块
集成腾讯云COS存储，支持多种文件类型上传和管理
"""

import os
import uuid
from datetime import datetime
from typing import Any, Dict, List

from fastapi import UploadFile

from .cos_service import get_cos_service
from .errors import ErrorCode
from .response import error, success


class FileUploadService:
    """文件上传服务类"""

    # 支持的文件类型
    ALLOWED_IMAGE_TYPES = ["jpg", "jpeg", "png", "gi", "bmp", "webp"]
    ALLOWED_VIDEO_TYPES = ["mp4", "webm", "ogg", "mov", "m4v", "avi"]
    ALLOWED_DOCUMENT_TYPES = ["pd", "doc", "docx", "txt", "md", "csv", "xlsx", "xls"]
    ALLOWED_ARCHIVE_TYPES = ["zip", "rar", "7z", "tar", "gz"]

    # 文件大小限制 (MB)
    MAX_IMAGE_SIZE = 10
    MAX_VIDEO_SIZE = 500
    MAX_DOCUMENT_SIZE = 50
    MAX_ARCHIVE_SIZE = 100

    def __init__(self):
        """初始化文件上传服务"""
        self.cos_service = get_cos_service()

    def _validate_file(self, file: UploadFile, file_category: str = "auto") -> dict[str, Any]:
        """
        验证上传的文件

        Args:
            file: 上传的文件对象
            file_category: 文件类别 (auto/image/document/archive)

        Returns:
            Dict: 验证结果
        """
        try:
            # 检查文件名
            if not file.filename:
                return {"valid": False, "error": "文件名不能为空"}

            # 获取文件扩展名
            file_ext = os.path.splitext(file.filename)[1].lower().lstrip(".")

            # 自动检测文件类别
            if file_category == "auto":
                if file_ext in self.ALLOWED_IMAGE_TYPES:
                    file_category = "image"
                elif file_ext in self.ALLOWED_VIDEO_TYPES:
                    file_category = "video"
                elif file_ext in self.ALLOWED_DOCUMENT_TYPES:
                    file_category = "document"
                elif file_ext in self.ALLOWED_ARCHIVE_TYPES:
                    file_category = "archive"
                else:
                    return {"valid": False, "error": f"不支持的文件类型: {file_ext}"}

            # 检查文件类型
            if file_category == "image" and file_ext not in self.ALLOWED_IMAGE_TYPES:
                return {"valid": False, "error": f"不支持的图片类型: {file_ext}"}
            elif file_category == "video" and file_ext not in self.ALLOWED_VIDEO_TYPES:
                return {"valid": False, "error": f"不支持的视频类型: {file_ext}"}
            elif file_category == "document" and file_ext not in self.ALLOWED_DOCUMENT_TYPES:
                return {"valid": False, "error": f"不支持的文档类型: {file_ext}"}
            elif file_category == "archive" and file_ext not in self.ALLOWED_ARCHIVE_TYPES:
                return {"valid": False, "error": f"不支持的压缩包类型: {file_ext}"}

            # 获取文件大小
            file.file.seek(0, 2)  # 移动到文件末尾
            file_size = file.file.tell()
            file.file.seek(0)  # 重置到文件开头

            # 检查文件大小限制
            max_size = 0
            if file_category == "image":
                max_size = self.MAX_IMAGE_SIZE * 1024 * 1024
            elif file_category == "video":
                max_size = self.MAX_VIDEO_SIZE * 1024 * 1024
            elif file_category == "document":
                max_size = self.MAX_DOCUMENT_SIZE * 1024 * 1024
            elif file_category == "archive":
                max_size = self.MAX_ARCHIVE_SIZE * 1024 * 1024

            if file_size > max_size:
                return {
                    "valid": False,
                    "error": f"文件大小超过限制 ({max_size // (1024 * 1024)}MB)",
                }

            return {
                "valid": True,
                "file_category": file_category,
                "file_ext": file_ext,
                "file_size": file_size,
            }

        except Exception as e:
            return {"valid": False, "error": f"文件验证失败: {str(e)}"}

    def _extract_image_metadata(self, file_data: bytes, file_ext: str) -> dict[str, Any]:
        """提取图片元数据"""
        try:
            import io

            from PIL import Image
            from PIL.ExifTags import TAGS

            image = Image.open(io.BytesIO(file_data))

            metadata = {
                "width": image.width,
                "height": image.height,
                "format": image.format,
                "mode": image.mode,
                "has_transparency": image.mode in ("RGBA", "LA") or "transparency" in image.info,
            }

            # 提取EXIF数据
            exif_data = {}
            if hasattr(image, "_getexif") and image._getexif() is not None:
                exif = image._getexif()
                for tag_id, value in exif.items():
                    tag = TAGS.get(tag_id, tag_id)
                    if isinstance(value, (str, int, float)):
                        exif_data[tag] = value

            if exif_data:
                metadata["exif"] = exif_data

            return metadata

        except Exception as e:
            return {"error": f"图片元数据提取失败: {str(e)}"}

    def _extract_document_metadata(self, file_data: bytes, file_ext: str) -> dict[str, Any]:
        """提取文档元数据"""
        try:
            metadata = {}

            if file_ext in ("pdf", "pd"):
                import io

                import fitz

                pdf_document = fitz.open(stream=file_data, filetype="pdf")
                metadata = {
                    "page_count": pdf_document.page_count,
                    "title": pdf_document.metadata.get("title", ""),
                    "author": pdf_document.metadata.get("author", ""),
                    "subject": pdf_document.metadata.get("subject", ""),
                    "creator": pdf_document.metadata.get("creator", ""),
                    "producer": pdf_document.metadata.get("producer", ""),
                    "creation_date": pdf_document.metadata.get("creationDate", ""),
                    "modification_date": pdf_document.metadata.get("modDate", ""),
                }
                pdf_document.close()

            elif file_ext in ["xlsx", "xls"]:
                import io

                import pandas as pd

                df = pd.read_excel(io.BytesIO(file_data))
                metadata = {
                    "sheet_count": 1,  # 简化处理
                    "row_count": len(df),
                    "column_count": len(df.columns),
                    "columns": list(df.columns),
                }

            elif file_ext == "csv":
                import io

                import pandas as pd

                df = pd.read_csv(io.BytesIO(file_data))
                metadata = {
                    "row_count": len(df),
                    "column_count": len(df.columns),
                    "columns": list(df.columns),
                }

            elif file_ext in ["txt", "md"]:
                content = file_data.decode("utf-8", errors="ignore")
                metadata = {
                    "char_count": len(content),
                    "line_count": content.count("\n") + 1,
                    "word_count": len(content.split()),
                }

            return metadata

        except Exception as e:
            return {"error": f"文档元数据提取失败: {str(e)}"}

    async def upload_file(
        self,
        file: UploadFile,
        user_id: str,
        category: str = "auto",
        description: str = "",
        tags: list[str] = None,
    ) -> dict[str, Any]:
        """
        上传文件

        Args:
            file: FastAPI上传的文件对象
            user_id: 用户ID
            category: 文件类别
            description: 文件描述
            tags: 标签列表

        Returns:
            Dict: 上传结果
        """
        try:
            # 验证文件
            validation = self._validate_file(file, category)
            if not validation["valid"]:
                return error(ErrorCode.PARAM_INVALID, validation["error"])

            file_category = validation["file_category"]
            file_ext = validation["file_ext"]

            # 读取文件内容
            file_data = await file.read()
            # 重置文件指针，以便后续可能的其他操作
            await file.seek(0)

            # 生成唯一文件名
            unique_filename = f"{uuid.uuid4().hex}.{file_ext}"

            # 确定存储文件夹（不包含日期，日期由 cos_service._build_key 添加）
            folder = f"uploads/{file_category}"

            # 提取文件元数据
            metadata = {}
            if file_category == "image":
                metadata = self._extract_image_metadata(file_data, file_ext)
            elif file_category == "document":
                metadata = self._extract_document_metadata(file_data, file_ext)

            # 确定内容类型
            content_type = file.content_type or "application/octet-stream"
            if file_category == "image":
                content_type = f"image/{file_ext}"
            elif file_category == "video":
                content_type = file.content_type or f"video/{file_ext}"
            elif file_ext == "pd":
                content_type = "application/pd"
            elif file_ext in ["doc", "docx"]:
                content_type = "application/msword"
            elif file_ext in ["xlsx", "xls"]:
                content_type = "application/vnd.ms-excel"

            # 上传到COS
            upload_result = self.cos_service.upload_file(
                file_data=file_data,
                file_name=unique_filename,
                folder=folder,
                content_type=content_type,
            )

            if not upload_result.get("success", False):
                return error(ErrorCode.UPLOAD_FAILED, upload_result.get("error", "上传失败"))

            file_key = upload_result.get("file_key") or upload_result.get("key") or ""
            file_url = upload_result.get("file_url") or upload_result.get("url") or ""
            file_name_saved = upload_result.get("file_name") or (
                os.path.basename(file_key) if file_key else unique_filename
            )
            file_size_saved = int(upload_result.get("file_size") or len(file_data))
            upload_time_saved = upload_result.get("upload_time") or datetime.utcnow().isoformat()
            file_md5_saved = upload_result.get("file_md5") or ""

            # 构建返回结果
            result = {
                "file_id": file_key,
                "file_key": file_key,
                "original_name": file.filename,
                "file_name": file_name_saved,
                "file_url": file_url,
                "file_size": file_size_saved,
                "file_category": file_category,
                "file_type": file_ext,
                "content_type": content_type,
                "user_id": user_id,
                "description": description,
                "tags": tags or [],
                "metadata": metadata,
                "upload_time": upload_time_saved,
                "file_md5": file_md5_saved,
            }

            return success(result)

        except Exception as e:
            return error(ErrorCode.UPLOAD_FAILED, f"文件上传失败: {str(e)}")

    def delete_file(self, file_key: str, user_id: str) -> dict[str, Any]:
        """
        删除文件

        Args:
            file_key: 文件在COS中的key
            user_id: 用户ID (用于权限验证)

        Returns:
            Dict: 删除结果
        """
        try:
            # 这里可以添加权限验证逻辑
            # 例如检查文件是否属于该用户

            delete_result = self.cos_service.delete_file(file_key)

            if isinstance(delete_result, bool):
                delete_ok = delete_result
                delete_time = datetime.utcnow().isoformat()
                delete_err = "删除失败"
            else:
                delete_ok = bool(delete_result.get("success"))
                delete_time = delete_result.get("delete_time", datetime.utcnow().isoformat())
                delete_err = delete_result.get("error", "删除失败")

            if delete_ok:
                return success({"file_key": file_key, "deleted_at": delete_time})
            else:
                return error(ErrorCode.DELETE_FAILED, delete_err)

        except Exception as e:
            return error(ErrorCode.DELETE_FAILED, f"文件删除失败: {str(e)}")

    def get_file_info(self, file_key: str) -> dict[str, Any]:
        """
        获取文件信息

        Args:
            file_key: 文件在COS中的key

        Returns:
            Dict: 文件信息
        """
        try:
            file_info = self.cos_service.get_file_info(file_key)

            if file_info["success"]:
                return success(file_info)
            else:
                return error(ErrorCode.FILE_NOT_FOUND, file_info["error"])

        except Exception as e:
            return error(ErrorCode.FILE_NOT_FOUND, f"文件信息获取失败: {str(e)}")

    def list_user_files(self, user_id: str, category: str = None, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        """
        列出用户的文件

        Args:
            user_id: 用户ID
            category: 文件类别过滤
            limit: 返回数量限制
            offset: 偏移量

        Returns:
            Dict: 文件列表
        """
        try:
            # 构建文件夹前缀
            if category:
                folder = f"uploads/{category}"
            else:
                folder = "uploads"

            list_result = self.cos_service.list_files(folder=folder, max_keys=limit + offset, prefix=f"{folder}/")

            if not list_result["success"]:
                return error(ErrorCode.LIST_FAILED, list_result["error"])

            # 过滤和处理文件列表
            files = []
            for file_info in list_result["files"][offset : offset + limit]:
                # 这里可以添加用户权限过滤
                # 例如从数据库中查询文件归属信息

                file_info["user_id"] = user_id  # 临时处理，实际应该从数据库查询
                files.append(file_info)

            return success(
                {
                    "files": files,
                    "total": list_result["total_count"],
                    "limit": limit,
                    "offset": offset,
                    "category": category,
                }
            )

        except Exception as e:
            return error(ErrorCode.LIST_FAILED, f"文件列表获取失败: {str(e)}")


# 创建全局文件上传服务实例
file_upload_service = FileUploadService()


def get_file_upload_service() -> FileUploadService:
    """获取文件上传服务实例"""
    return file_upload_service
