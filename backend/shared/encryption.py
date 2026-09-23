"""
配置加密模块
提供密码等敏感信息的加密存储功能
"""

import base64
import logging
import os

from cryptography.fernet import Fernet
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logger = logging.getLogger(__name__)


class ConfigEncryption:
    """配置加密类。

    主密钥**只能来自环境变量** `QUANTMIND_MASTER_KEY` 或显式入参。

    2026-09-23 移除内置默认主密钥：本模块曾用 `os.getenv(..., "<字面量>")` 兜底，而
    盐是常量、密文（`backend/config/users/*.json`）又在同一个仓库里——**钥匙和锁
    一起发出去**，而这是个公开仓。缺密钥时现在直接抛错，不再静默用一个人人都知道的
    密钥「加密」券商口令。

    历史密文迁移：旧部署若用过那个默认值，先在**本机**用当值环境变量跑一次
    `decrypt_password()` 读出明文，再用新的 `QUANTMIND_MASTER_KEY` 重新 `encrypt_password()`
    写回（密钥换掉即作废旧的公开密钥）。
    """

    def __init__(self, master_key: str = None):
        """
        初始化加密器

        Args:
            master_key: 主密钥；不提供则从环境变量 `QUANTMIND_MASTER_KEY` 读取

        Raises:
            RuntimeError: 未提供主密钥（不再有内置默认值）
        """
        if master_key is None:
            master_key = os.getenv("QUANTMIND_MASTER_KEY")
        if not master_key:
            raise RuntimeError(
                "缺少 QUANTMIND_MASTER_KEY：配置加密不再提供内置默认主密钥"
                "（默认值与密文同在一个公开仓里 = 公开）。请在部署环境（.env 或密钥"
                "管理器）里设置后重启；历史密文的迁移办法见 ConfigEncryption 类文档。"
            )

        self.master_key = master_key
        self.cipher = self._create_cipher()

    def _create_cipher(self) -> Fernet:
        """创建加密器"""
        # 使用PBKDF2从主密钥派生加密密钥
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"quantmind_salt_v1",  # 固定salt用于相同主密钥生成相同加密密钥
            iterations=100000,
            backend=default_backend(),
        )

        key = base64.urlsafe_b64encode(kdf.derive(self.master_key.encode()))
        return Fernet(key)

    def encrypt(self, plain_text: str) -> str:
        """
        加密文本

        Args:
            plain_text: 明文

        Returns:
            str: 加密后的文本（Base64编码）
        """
        try:
            if not plain_text:
                return ""

            encrypted = self.cipher.encrypt(plain_text.encode())
            return base64.urlsafe_b64encode(encrypted).decode()

        except Exception as e:
            logger.error(f"加密失败: {e}")
            raise

    def decrypt(self, encrypted_text: str) -> str:
        """
        解密文本

        Args:
            encrypted_text: 加密文本（Base64编码）

        Returns:
            str: 解密后的明文
        """
        try:
            if not encrypted_text:
                return ""

            encrypted = base64.urlsafe_b64decode(encrypted_text.encode())
            decrypted = self.cipher.decrypt(encrypted)
            return decrypted.decode()

        except Exception as e:
            logger.error(f"解密失败: {e}")
            raise

    def encrypt_dict(self, data: dict, fields: list) -> dict:
        """
        加密字典中的指定字段

        Args:
            data: 数据字典
            fields: 需要加密的字段列表

        Returns:
            dict: 加密后的字典（原字典的副本）
        """
        result = data.copy()

        for field in fields:
            if field in result and result[field]:
                result[field] = self.encrypt(str(result[field]))

        return result

    def decrypt_dict(self, data: dict, fields: list) -> dict:
        """
        解密字典中的指定字段

        Args:
            data: 数据字典
            fields: 需要解密的字段列表

        Returns:
            dict: 解密后的字典（原字典的副本）
        """
        result = data.copy()

        for field in fields:
            if field in result and result[field]:
                try:
                    result[field] = self.decrypt(str(result[field]))
                except Exception as e:
                    logger.warning(f"解密字段 {field} 失败: {e}")
                    result[field] = None

        return result


# 全局加密器实例
_global_encryptor = None


def get_encryptor() -> ConfigEncryption:
    """获取全局加密器实例"""
    global _global_encryptor
    if _global_encryptor is None:
        _global_encryptor = ConfigEncryption()
    return _global_encryptor


def encrypt_password(password: str) -> str:
    """便捷函数：加密密码"""
    return get_encryptor().encrypt(password)


def decrypt_password(encrypted_password: str) -> str:
    """便捷函数：解密密码"""
    return get_encryptor().decrypt(encrypted_password)
