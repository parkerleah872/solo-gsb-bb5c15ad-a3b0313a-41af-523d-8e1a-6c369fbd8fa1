"""FastAPI 应用入口：生命周期内持有单一 SQLite 连接与后台清理任务。"""
from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.crypto import CryptoBox
from app.db import connect, init_db
from app.policy_engine import get_runner
from app.routers import router
from app.services.document_service import DocumentService
from app.services.errors import ServiceError
from app.services.policy_service import PolicyService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("redaction")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if settings.env == "production" and not settings.master_key:
        raise RuntimeError("生产环境必须配置 REDACT_MASTER_KEY（可用 `python -m app.cli keygen` 生成）")

    if settings.db_path != ":memory:":
        # 幂等建表 / 迁移；容器首次启动时自动初始化 SQLite
        from pathlib import Path

        Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
        await init_db(settings.db_path)

    db = await connect(settings.db_path)
    crypto = CryptoBox(settings.master_key, ephemeral=settings.ephemeral_key)
    rule_runner = get_runner()
    policy_service = PolicyService(db=db)
    app.state.policy_service = policy_service
    app.state.service = DocumentService(
        db=db,
        crypto=crypto,
        default_ttl=settings.default_ttl,
        max_ttl=settings.max_ttl,
        token_ttl=settings.token_ttl,
        policies=policy_service,
        rule_timeout=settings.rule_timeout,
        rule_runner=rule_runner,
    )

    stop = asyncio.Event()

    async def cleanup_loop() -> None:
        while not stop.is_set():
            try:
                await app.state.service.purge_expired()
            except Exception:  # noqa: BLE001 - 清理失败不应终止进程
                logger.exception("过期数据清理失败")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=settings.cleanup_interval)

    task = asyncio.create_task(cleanup_loop())
    logger.info("脱敏服务已启动，数据库：%s", settings.db_path)
    try:
        yield
    finally:
        stop.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        rule_runner.close()
        await db.close()


app = FastAPI(
    title="合同文本脱敏 API",
    version="1.0.0",
    description="按 Unicode 位置解析重叠敏感片段，生成稳定占位符；映射加密存储且可过期，"
    "恢复接口使用一次性令牌。",
    lifespan=lifespan,
)
app.include_router(router)


@app.exception_handler(ServiceError)
async def service_error_handler(_: Request, exc: ServiceError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
