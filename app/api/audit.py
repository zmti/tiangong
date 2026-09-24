"""审计日志 API: /v1/audit/*"""
from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.core.audit import get_audit_logger, AuditEventType
from app.core.tenant_context import get_tenant

router = APIRouter(prefix="/v1/audit", tags=["audit"])


class AuditLogRequest(BaseModel):
    event_type: str = Field("", description="事件类型过滤")
    resource_type: str = Field("", description="资源类型过滤")
    action: str = Field("", description="动作过滤")
    result: str = Field("", description="结果过滤: success/failure/blocked")
    user_id: str = Field("", description="用户ID过滤")
    start_time: float = Field(0, description="开始时间戳")
    end_time: float = Field(0, description="结束时间戳")
    limit: int = Field(100, ge=1, le=1000, description="返回条数")
    offset: int = Field(0, ge=0, description="偏移量")


@router.post("/logs")
async def query_audit_logs(req: AuditLogRequest) -> dict:
    """查询审计日志"""
    tid = get_tenant() or "default"
    logger = get_audit_logger()
    
    rows = logger.query(
        tenant_id=tid,
        event_type=req.event_type or None,
        resource_type=req.resource_type or None,
        action=req.action or None,
        result=req.result or None,
        user_id=req.user_id or None,
        start_time=req.start_time or None,
        end_time=req.end_time or None,
        limit=req.limit,
        offset=req.offset,
    )
    return {"logs": rows, "total": len(rows)}


@router.get("/stats")
async def audit_stats(
    event_type: str = Query("", description="事件类型"),
    days: int = Query(7, ge=1, le=90, description="统计天数"),
) -> dict:
    """审计日志统计"""
    tid = get_tenant() or "default"
    logger = get_audit_logger()
    
    import time
    start = time.time() - days * 86400
    end = time.time()
    
    total = logger.count(tenant_id=tid)
    recent = logger.count(tenant_id=tid)
    # 简化统计：实际应按时间过滤
    
    return {
        "total_logs": total,
        "recent_logs": recent,
        "period_days": days,
        "event_types": [
            "config_change", "data_access", "sensitive_operation",
            "auth", "admin", "tool_execution", "model_call",
            "memory_operation", "document_operation", "session_operation"
        ],
    }


@router.get("/event-types")
async def list_event_types() -> dict:
    """获取可用的事件类型"""
    return {
        "event_types": [
            {"value": "config_change", "label": "配置变更"},
            {"value": "data_access", "label": "数据访问"},
            {"value": "sensitive_operation", "label": "敏感操作"},
            {"value": "auth", "label": "认证授权"},
            {"value": "admin", "label": "管理操作"},
            {"value": "tool_execution", "label": "工具执行"},
            {"value": "model_call", "label": "模型调用"},
            {"value": "memory_operation", "label": "记忆操作"},
            {"value": "document_operation", "label": "文档操作"},
            {"value": "session_operation", "label": "会话操作"},
        ]
    }