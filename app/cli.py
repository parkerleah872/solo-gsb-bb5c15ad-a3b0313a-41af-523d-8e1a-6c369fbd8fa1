"""命令行工具：

* ``python -m app.cli keygen``            生成 Fernet 主密钥
* ``python -m app.cli initdb [--db PATH]`` 幂等初始化 SQLite 数据库
"""
from __future__ import annotations

import argparse

from cryptography.fernet import Fernet

from app.config import get_settings
from app.db import init_db_sync


def main() -> None:
    parser = argparse.ArgumentParser(prog="app.cli", description="脱敏服务运维工具")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("keygen", help="生成 Fernet 格式主密钥并打印到标准输出")

    p_init = sub.add_parser("initdb", help="幂等创建数据库表")
    p_init.add_argument("--db", default=None, help="数据库路径（默认取 REDACT_DATABASE_URL）")

    args = parser.parse_args()

    if args.command == "keygen":
        print(Fernet.generate_key().decode())
    elif args.command == "initdb":
        settings = get_settings()
        db_path = args.db or settings.db_path
        init_db_sync(db_path)
        print(f"数据库已初始化：{db_path}")


if __name__ == "__main__":
    main()
