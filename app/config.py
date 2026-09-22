"""运行期配置（可通过 REDACT_ 前缀的环境变量覆盖）。"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="REDACT_", env_file=".env", extra="ignore"
    )

    # SQLite 数据库文件路径；:memory: 仅用于测试
    database_url: str = "/data/redaction.db"

    # 对称加密主密钥（base64 urlsafe 编码的 32 字节，Fernet 格式）。
    # 留空时：生产环境(REDACT_ENV=production)启动失败，其他环境生成一次性密钥，
    # 进程重启后历史映射将无法解密。
    master_key: str = ""

    env: str = "development"

    # 文档 / 片段映射默认与最大有效期（秒）
    default_ttl: int = 86400
    max_ttl: int = 7 * 86400

    # 一次性恢复令牌有效期（秒）
    token_ttl: int = 600

    # 单条自动脱敏规则执行的超时时间（秒）；任一规则超时则整次提交 422
    rule_timeout: float = 1.0

    # 后台清理周期（秒）
    cleanup_interval: int = 300

    @property
    def ephemeral_key(self) -> bool:
        return not self.master_key and self.env != "production"

    @property
    def db_path(self) -> str:
        prefix = "sqlite:///"
        return self.database_url[len(prefix) :] if self.database_url.startswith(prefix) else self.database_url


@lru_cache
def get_settings() -> Settings:
    return Settings()
