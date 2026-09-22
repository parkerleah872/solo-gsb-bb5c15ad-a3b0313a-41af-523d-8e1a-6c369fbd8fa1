"""对称加密封装：所有持久化到 SQLite 的原文 / 片段 / 令牌状态均先加密。

主密钥支持两种提供方式：
1. Fernet 格式密钥（``app.cli keygen`` 生成）；
2. 任意口令字符串：使用 SHA-256 派生为 32 字节并做 urlsafe-base64 编码。

生产环境必须显式配置 REDACT_MASTER_KEY；开发环境可留空使用进程内一次性密钥。
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)


class CryptoBox:
    def __init__(self, configured_key: str, ephemeral: bool = False) -> None:
        if ephemeral or not configured_key:
            self._fernet = Fernet(Fernet.generate_key())
            self.ephemeral = True
            logger.warning(
                "未配置 REDACT_MASTER_KEY，已生成一次性加密密钥；"
                "进程重启后历史映射无法解密。"
            )
            return

        self._fernet = Fernet(self._coerce(configured_key))
        self.ephemeral = False

    @staticmethod
    def _coerce(key: str) -> bytes:
        raw = key.strip().encode("utf-8")
        # 已是合法 Fernet 密钥则直接使用
        try:
            Fernet(raw)
            return raw
        except (ValueError, TypeError):
            pass
        digest = hashlib.sha256(raw).digest()
        return base64.urlsafe_b64encode(digest)

    def encrypt(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, token: bytes | str) -> str:
        return self._fernet.decrypt(token).decode("utf-8")

    def decrypt_or_raise(self, token: bytes | str) -> str:
        """解密失败时抛出 InvalidToken，由上层映射为 401/500。"""
        return self._fernet.decrypt(token).decode("utf-8")


__all__ = ["CryptoBox", "InvalidToken"]


def random_token() -> str:
    """生成一次性恢复令牌（仅在创建 / 轮换时向客户端展示一次明文）。"""
    return os.urandom(32).hex()
