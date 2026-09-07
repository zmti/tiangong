"""统一错误处理模块。

提供标准化的异常类和错误响应格式，确保 API 返回一致的错误信息。
"""
from __future__ import annotations

import logging
import traceback
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ── 标准错误码 ────────────────────────────────────────────────────
class ErrorCode:
    """标准错误码定义。"""
    # 客户端错误 (4xx)
    BAD_REQUEST = "BAD_REQUEST"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    UNSUPPORTED_MEDIA_TYPE = "UNSUPPORTED_MEDIA_TYPE"
    TOO_MANY_REQUESTS = "TOO_MANY_REQUESTS"
    
    # 服务端错误 (5xx)
    INTERNAL_ERROR = "INTERNAL_ERROR"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    GATEWAY_TIMEOUT = "GATEWAY_TIMEOUT"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    
    # 业务错误
    LLM_ERROR = "LLM_ERROR"
    TOOL_ERROR = "TOOL_ERROR"
    RATE_LIMIT = "RATE_LIMIT"
    COST_EXCEEDED = "COST_EXCEEDED"
    SECURITY_BLOCKED = "SECURITY_BLOCKED"


# ── 标准化错误响应 ────────────────────────────────────────────────
@dataclass
class ErrorDetail:
    """单个错误详情。"""
    code: str
    message: str
    field: Optional[str] = None
    details: Optional[dict] = field(default_factory=dict)
    
    def to_dict(self) -> dict:
        result = {
            "code": self.code,
            "message": self.message,
        }
        if self.field:
            result["field"] = self.field
        if self.details:
            result["details"] = self.details
        return result


@dataclass
class ErrorResponse:
    """标准化错误响应。"""
    success: bool = False
    error_code: str = ""
    message: str = ""
    details: list[dict] = field(default_factory=list)
    request_id: str = ""
    timestamp: float = 0.0
    
    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "error_code": self.error_code,
            "message": self.message,
            "details": self.details,
            "request_id": self.request_id,
            "timestamp": self.timestamp,
        }


# ── 基础异常类 ────────────────────────────────────────────────────
class AgentServiceError(Exception):
    """Agent Service 基础异常类。"""
    
    def __init__(
        self,
        message: str,
        error_code: str = ErrorCode.INTERNAL_ERROR,
        details: Optional[dict] = None,
        status_code: int = 500,
    ):
        self.message = message
        self.error_code = error_code
        self.details = details or {}
        self.status_code = status_code
        super().__init__(self.message)
    
    def to_response(self, request_id: str = "") -> ErrorResponse:
        return ErrorResponse(
            success=False,
            error_code=self.error_code,
            message=self.message,
            details=[{"code": self.error_code, "message": self.message, **self.details}],
            request_id=request_id,
            timestamp=__import__("time").time(),
        )


class BadRequestError(AgentServiceError):
    """请求参数错误。"""
    
    def __init__(self, message: str, field: Optional[str] = None, details: Optional[dict] = None):
        error_details = {"field": field} if field else {}
        if details:
            error_details.update(details)
        super().__init__(
            message=message,
            error_code=ErrorCode.BAD_REQUEST,
            details=error_details,
            status_code=400,
        )


class UnauthorizedError(AgentServiceError):
    """未授权访问。"""
    
    def __init__(self, message: str = "未授权访问"):
        super().__init__(
            message=message,
            error_code=ErrorCode.UNAUTHORIZED,
            status_code=401,
        )


class ForbiddenError(AgentServiceError):
    """禁止访问。"""
    
    def __init__(self, message: str = "权限不足"):
        super().__init__(
            message=message,
            error_code=ErrorCode.FORBIDDEN,
            status_code=403,
        )


class NotFoundError(AgentServiceError):
    """资源不存在。"""
    
    def __init__(self, message: str = "资源不存在"):
        super().__init__(
            message=message,
            error_code=ErrorCode.NOT_FOUND,
            status_code=404,
        )


class ConflictError(AgentServiceError):
    """资源冲突。"""
    
    def __init__(self, message: str, field: Optional[str] = None):
        error_details = {"field": field} if field else {}
        super().__init__(
            message=message,
            error_code=ErrorCode.CONFLICT,
            details=error_details,
            status_code=409,
        )


class ValidationError(AgentServiceError):
    """数据验证失败。"""
    
    def __init__(self, message: str, field: Optional[str] = None, details: Optional[list[dict]] = None):
        error_details = {"field": field, "validation_errors": details} if field or details else {}
        super().__init__(
            message=message,
            error_code=ErrorCode.VALIDATION_ERROR,
            details=error_details,
            status_code=422,
        )


class LLMError(AgentServiceError):
    """LLM 调用失败。"""
    
    def __init__(self, message: str, provider: Optional[str] = None, model: Optional[str] = None):
        error_details = {}
        if provider:
            error_details["provider"] = provider
        if model:
            error_details["model"] = model
        super().__init__(
            message=message,
            error_code=ErrorCode.LLM_ERROR,
            details=error_details,
            status_code=502,
        )


class ToolError(AgentServiceError):
    """工具执行失败。"""
    
    def __init__(self, message: str, tool_name: Optional[str] = None):
        error_details = {"tool": tool_name} if tool_name else {}
        super().__init__(
            message=message,
            error_code=ErrorCode.TOOL_ERROR,
            details=error_details,
            status_code=500,
        )


class SecurityError(AgentServiceError):
    """安全拦截。"""
    
    def __init__(self, message: str, reason: Optional[str] = None):
        error_details = {"reason": reason} if reason else {}
        super().__init__(
            message=message,
            error_code=ErrorCode.SECURITY_BLOCKED,
            details=error_details,
            status_code=403,
        )


class RateLimitError(AgentServiceError):
    """请求限流。"""
    
    def __init__(self, message: str = "请求过于频繁，请稍后重试"):
        super().__init__(
            message=message,
            error_code=ErrorCode.RATE_LIMIT,
            status_code=429,
        )


class CostExceededError(AgentServiceError):
    """成本超预算。"""
    
    def __init__(self, message: str, budget_usd: float = 0.0, actual_cost: float = 0.0):
        super().__init__(
            message=message,
            error_code=ErrorCode.COST_EXCEEDED,
            details={"budget_usd": budget_usd, "actual_cost": actual_cost},
            status_code=403,
        )


# ── 统一错误处理器 ────────────────────────────────────────────────
def format_error_response(
    exc: Exception,
    request_id: str = "",
    include_traceback: bool = False,
) -> ErrorResponse:
    """将异常转换为标准化的错误响应。
    
    Args:
        exc: 异常对象
        request_id: 请求 ID（用于追踪）
        include_traceback: 是否包含堆栈跟踪（仅开发环境）
    
    Returns:
        ErrorResponse 对象
    """
    # 如果是 AgentServiceError，直接转换
    if isinstance(exc, AgentServiceError):
        response = exc.to_response(request_id)
        response.timestamp = __import__("time").time()
        return response
    
    # 其他异常，根据类型转换
    import httpx
    from fastapi import HTTPException
    
    if isinstance(exc, HTTPException):
        return ErrorResponse(
            success=False,
            error_code=ErrorCode.INTERNAL_ERROR,
            message=exc.detail if isinstance(exc.detail, str) else str(exc.detail),
            request_id=request_id,
            timestamp=__import__("time").time(),
        )
    
    if isinstance(exc, httpx.HTTPStatusError):
        return ErrorResponse(
            success=False,
            error_code=ErrorCode.SERVICE_UNAVAILABLE,
            message=f"上游服务错误: HTTP {exc.response.status_code}",
            request_id=request_id,
            timestamp=__import__("time").time(),
        )
    
    if isinstance(exc, TimeoutError):
        return ErrorResponse(
            success=False,
            error_code=ErrorCode.GATEWAY_TIMEOUT,
            message="请求超时",
            request_id=request_id,
            timestamp=__import__("time").time(),
        )
    
    # 通用错误
    message = str(exc)
    if include_traceback:
        message += f"\n\n详细错误:\n{traceback.format_exc()}"
    
    return ErrorResponse(
        success=False,
        error_code=ErrorCode.INTERNAL_ERROR,
        message=message or "未知错误",
        request_id=request_id,
        timestamp=__import__("time").time(),
    )


def log_error(
    exc: Exception,
    context: str = "",
    level: int = logging.ERROR,
    include_traceback: bool = True,
) -> None:
    """记录错误日志。
    
    Args:
        exc: 异常对象
        context: 错误上下文描述
        level: 日志级别
        include_traceback: 是否记录堆栈跟踪
    """
    logger.log(
        level,
        f"[{context}] 错误: {exc}" + (f"\n{traceback.format_exc()}" if include_traceback and level >= logging.DEBUG else ""),
        exc_info=level >= logging.DEBUG,
    )
