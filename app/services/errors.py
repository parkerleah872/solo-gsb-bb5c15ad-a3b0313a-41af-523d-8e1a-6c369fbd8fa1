"""服务层统一错误，由 main 中的异常处理器映射为 HTTP 状态码。"""
from __future__ import annotations


class ServiceError(Exception):
    status_code = 400

    def __init__(self, detail: str, status_code: int | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        if status_code is not None:
            self.status_code = status_code


class NotFoundError(ServiceError):
    status_code = 404


class UnauthorizedError(ServiceError):
    status_code = 401


class ExpiredError(ServiceError):
    status_code = 410
