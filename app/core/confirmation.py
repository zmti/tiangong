"""人机协同确认系统。

支持：
1. 可视化确认面板
2. 批量确认模式
3. 确认历史追溯
4. 自动确认规则配置
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


class ConfirmationStatus(str, Enum):
    """确认状态。"""
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass
class ConfirmationRequest:
    """确认请求。"""
    request_id: str
    tool_name: str
    tool_params: dict[str, Any]
    risk_level: str  # low/medium/high/critical
    description: str = ""
    
    # 时间信息
    created_at: float = field(default_factory=time.time)
    expires_at: Optional[float] = None
    
    # 状态
    status: ConfirmationStatus = ConfirmationStatus.PENDING
    confirmed_by: Optional[str] = None
    confirmation_time: Optional[float] = None
    rejection_reason: Optional[str] = None
    
    # 扩展信息
    metadata: dict[str, Any] = field(default_factory=dict)
    
    @property
    def is_expired(self) -> bool:
        """是否已过期。"""
        if not self.expires_at:
            return False
        return time.time() > self.expires_at
    
    @property
    def is_pending(self) -> bool:
        """是否待确认。"""
        return self.status == ConfirmationStatus.PENDING and not self.is_expired


@dataclass
class ConfirmationHistory:
    """确认历史。"""
    request_id: str
    tool_name: str
    action: str  # approved/rejected
    user: str
    reason: Optional[str] = None
    timestamp: float = field(default_factory=time.time)


class ConfirmationManager:
    """确认管理器。"""
    
    def __init__(self):
        self._pending: dict[str, ConfirmationRequest] = {}
        self._history: list[ConfirmationHistory] = []
        self._auto_approve_rules: list[dict] = []
        self._auto_reject_rules: list[dict] = []
    
    def request_confirmation(
        self,
        tool_name: str,
        tool_params: dict[str, Any],
        risk_level: str = "medium",
        timeout: int = 300,
        description: str = ""
    ) -> ConfirmationRequest:
        """请求确认。"""
        request_id = f"confirm_{int(time.time()*1000)}"
        
        request = ConfirmationRequest(
            request_id=request_id,
            tool_name=tool_name,
            tool_params=tool_params,
            risk_level=risk_level,
            description=description,
            expires_at=time.time() + timeout
        )
        
        # 检查自动确认规则
        if self._should_auto_approve(request):
            request.status = ConfirmationStatus.APPROVED
            request.confirmed_by = "auto"
            request.confirmation_time = time.time()
            logger.info(f"工具 {tool_name} 自动批准")
        elif self._should_auto_reject(request):
            request.status = ConfirmationStatus.REJECTED
            request.confirmed_by = "auto"
            request.confirmation_time = time.time()
            request.rejection_reason = "匹配自动拒绝规则"
            logger.info(f"工具 {tool_name} 自动拒绝")
        else:
            self._pending[request_id] = request
            logger.info(f"工具 {tool_name} 等待确认: {request_id}")
        
        return request
    
    def approve(self, request_id: str, user: str = "user") -> bool:
        """批准确认。"""
        request = self._pending.get(request_id)
        if not request:
            return False
        
        request.status = ConfirmationStatus.APPROVED
        request.confirmed_by = user
        request.confirmation_time = time.time()
        
        # 记录历史
        self._history.append(ConfirmationHistory(
            request_id=request_id,
            tool_name=request.tool_name,
            action="approved",
            user=user
        ))
        
        del self._pending[request_id]
        return True
    
    def reject(self, request_id: str, user: str = "user", reason: str = "") -> bool:
        """拒绝确认。"""
        request = self._pending.get(request_id)
        if not request:
            return False
        
        request.status = ConfirmationStatus.REJECTED
        request.confirmed_by = user
        request.confirmation_time = time.time()
        request.rejection_reason = reason
        
        # 记录历史
        self._history.append(ConfirmationHistory(
            request_id=request_id,
            tool_name=request.tool_name,
            action="rejected",
            user=user,
            reason=reason
        ))
        
        del self._pending[request_id]
        return True
    
    def get_pending(self) -> list[ConfirmationRequest]:
        """获取待确认列表。"""
        return list(self._pending.values())
    
    def get_history(self, limit: int = 50) -> list[ConfirmationHistory]:
        """获取确认历史。"""
        return self._history[-limit:]
    
    def add_auto_approve_rule(self, rule: dict) -> None:
        """添加自动批准规则。"""
        self._auto_approve_rules.append(rule)
    
    def add_auto_reject_rule(self, rule: dict) -> None:
        """添加自动拒绝规则。"""
        self._auto_reject_rules.append(rule)
    
    def _should_auto_approve(self, request: ConfirmationRequest) -> bool:
        """检查是否应自动批准。"""
        for rule in self._auto_approve_rules:
            if rule.get("tool") == request.tool_name:
                if rule.get("risk_level") in [None, request.risk_level]:
                    return True
        return False
    
    def _should_auto_reject(self, request: ConfirmationRequest) -> bool:
        """检查是否应自动拒绝。"""
        for rule in self._auto_reject_rules:
            if rule.get("tool") == request.tool_name:
                return True
        return False
    
    def cleanup_expired(self) -> int:
        """清理过期确认。"""
        now = time.time()
        expired = []
        
        for req_id, req in self._pending.items():
            if req.expires_at and now > req.expires_at:
                req.status = ConfirmationStatus.EXPIRED
                expired.append(req_id)
        
        for req_id in expired:
            del self._pending[req_id]
        
        return len(expired)


# 全局实例
_confirmation_manager = ConfirmationManager()


def get_confirmation_manager() -> ConfirmationManager:
    """获取确认管理器实例。"""
    return _confirmation_manager
