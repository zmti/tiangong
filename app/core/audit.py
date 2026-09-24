"""审计日志模块:记录敏感操作、配置变更、数据访问等安全相关事件。

设计目标:
- 合规审计:满足安全合规要求的操作留痕
- 可查询:支持按租户/用户/事件类型/时间范围过滤
- 结构化:统一事件格式,便于 SIEM/日志平台接入
- 高性能:异步写入,批量刷盘,不阻塞主流程
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

from app.config import settings
from app.core.storage_backend import StorageBackend, get_storage_backend


@dataclass
class AuditEvent:
    """审计事件记录"""
    id: str = field(default_factory=lambda: f"audit_{int(time.time()*1000)}_{os.urandom(4).hex()}")
    timestamp: float = field(default_factory=time.time)
    event_type: str = ""  # config_change, data_access, sensitive_operation, auth, admin
    tenant_id: str = "default"
    user_id: str = ""
    session_id: str = ""
    resource_type: str = ""  # model, tool, memory, document, session, config, user
    resource_id: str = ""
    action: str = ""  # create, read, update, delete, execute, grant, revoke
    result: str = "success"  # success, failure, blocked
    details: dict = field(default_factory=dict)  # 扩展字段:旧值/新值/IP/UA等
    ip_address: str = ""
    user_agent: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


class AuditLogger:
    """审计日志记录器:异步批量写入,支持 SQLite/PostgreSQL/文件"""

    def __init__(
        self,
        backend: Optional[StorageBackend] = None,
        db_path: Optional[str] = None,
        batch_size: int = 100,
        flush_interval: float = 5.0,
    ):
        self._backend = backend or get_storage_backend(db_path=db_path)
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._lock = threading.RLock()
        self._buffer: list[AuditEvent] = []
        self._flush_task: Optional[asyncio.Task] = None
        self._running = False
        self._init_db()

    def _init_db(self) -> None:
        """初始化审计日志表"""
        schema = {
            "audit_logs": [
                {"name": "id", "type": "TEXT", "constraints": "PRIMARY KEY"},
                {"name": "timestamp", "type": "REAL", "constraints": "NOT NULL", "index": True},
                {"name": "event_type", "type": "TEXT", "constraints": "NOT NULL", "index": True},
                {"name": "tenant_id", "type": "TEXT", "constraints": "NOT NULL DEFAULT 'default'", "index": True},
                {"name": "user_id", "type": "TEXT", "constraints": "", "index": True},
                {"name": "session_id", "type": "TEXT", "constraints": "", "index": True},
                {"name": "resource_type", "type": "TEXT", "constraints": ""},
                {"name": "resource_id", "type": "TEXT", "constraints": ""},
                {"name": "action", "type": "TEXT", "constraints": "NOT NULL"},
                {"name": "result", "type": "TEXT", "constraints": "NOT NULL DEFAULT 'success'"},
                {"name": "details", "type": "TEXT", "constraints": "DEFAULT '{}'"},
                {"name": "ip_address", "type": "TEXT", "constraints": ""},
                {"name": "user_agent", "type": "TEXT", "constraints": ""},
            ],
        }
        self._backend.init_db(schema)

    def log(self, event: AuditEvent) -> None:
        """记录审计事件(同步接口,内部异步批量写入)"""
        with self._lock:
            self._buffer.append(event)
            if len(self._buffer) >= self._batch_size:
                self._flush_sync()

    async def log_async(self, event: AuditEvent) -> None:
        """记录审计事件(异步接口)"""
        self.log(event)

    def _flush_sync(self) -> int:
        """同步刷新缓冲区到数据库"""
        if not self._buffer:
            return 0
        with self._lock:
            events = self._buffer[:]
            self._buffer.clear()
        try:
            for event in events:
                self._backend.insert("audit_logs", event.to_dict())
            return len(events)
        except Exception as e:
            # 写入失败时放回缓冲区
            with self._lock:
                self._buffer = events + self._buffer
            import logging
            logging.getLogger(__name__).warning(f"审计日志写入失败: {e}")
            return 0

    async def _flush_loop(self) -> None:
        """后台定期刷新循环"""
        while self._running:
            try:
                await asyncio.sleep(self._flush_interval)
                if self._buffer:
                    self._flush_sync()
            except asyncio.CancelledError:
                break
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(f"审计日志刷新异常: {e}")

    def start(self) -> None:
        """启动后台刷新任务"""
        if not self._running:
            self._running = True
            try:
                loop = asyncio.get_running_loop()
                self._flush_task = loop.create_task(self._flush_loop())
            except RuntimeError:
                import logging
                logging.getLogger(__name__).debug("无运行中事件循环,跳过后台任务")

    async def stop(self) -> None:
        """停止并刷新剩余缓冲"""
        self._running = False
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        self._flush_sync()

    def query(
        self,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        event_type: Optional[str] = None,
        resource_type: Optional[str] = None,
        action: Optional[str] = None,
        result: Optional[str] = None,
        start_time: Optional[float] = None,
        end_time: Optional[float] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """查询审计日志"""
        conditions = []
        params = []
        if tenant_id:
            conditions.append("tenant_id = ?")
            params.append(tenant_id)
        if user_id:
            conditions.append("user_id = ?")
            params.append(user_id)
        if event_type:
            conditions.append("event_type = ?")
            params.append(event_type)
        if resource_type:
            conditions.append("resource_type = ?")
            params.append(resource_type)
        if action:
            conditions.append("action = ?")
            params.append(action)
        if result:
            conditions.append("result = ?")
            params.append(result)
        if start_time:
            conditions.append("timestamp >= ?")
            params.append(start_time)
        if end_time:
            conditions.append("timestamp <= ?")
            params.append(end_time)

        where = " AND ".join(conditions) if conditions else "1=1"
        rows = self._backend.select(
            "audit_logs",
            where,
            tuple(params),
            order_by="timestamp DESC",
            limit=limit,
            offset=offset,
        )
        return rows

    def count(self, **kwargs) -> int:
        """统计符合条件的审计日志数量"""
        conditions = []
        params = []
        for k, v in kwargs.items():
            if v is not None:
                conditions.append(f"{k} = ?")
                params.append(v)
        where = " AND ".join(conditions) if conditions else "1=1"
        row = self._backend.select_one("audit_logs", f"SELECT COUNT(*) as cnt FROM audit_logs WHERE {where}", tuple(params))
        return row["cnt"] if row else 0


# 全局单例
_audit_logger: Optional[AuditLogger] = None
_audit_lock = threading.Lock()


def get_audit_logger() -> AuditLogger:
    """获取审计日志记录器单例"""
    global _audit_logger
    if _audit_logger is None:
        with _audit_lock:
            if _audit_logger is None:
                _audit_logger = AuditLogger()
    return _audit_logger


# 便捷记录函数
def audit_log(
    event_type: str,
    action: str,
    resource_type: str = "",
    resource_id: str = "",
    result: str = "success",
    tenant_id: str = "default",
    user_id: str = "",
    session_id: str = "",
    details: Optional[dict] = None,
    ip_address: str = "",
    user_agent: str = "",
) -> None:
    """快速记录审计事件"""
    if not getattr(settings, "agent_audit_log_enabled", False):
        return
    logger = get_audit_logger()
    event = AuditEvent(
        event_type=event_type,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        result=result,
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
        details=details or {},
        ip_address=ip_address,
        user_agent=user_agent,
    )
    logger.log(event)


# 常用事件类型常量
class AuditEventType:
    CONFIG_CHANGE = "config_change"
    DATA_ACCESS = "data_access"
    SENSITIVE_OPERATION = "sensitive_operation"
    AUTH = "auth"
    ADMIN = "admin"
    TOOL_EXECUTION = "tool_execution"
    MODEL_CALL = "model_call"
    MEMORY_OPERATION = "memory_operation"
    DOCUMENT_OPERATION = "document_operation"
    SESSION_OPERATION = "session_operation"