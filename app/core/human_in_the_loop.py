"""人工介入断点系统:计划/工具/决策级暂停+审批+回滚。

核心能力:
1. 多级断点:计划步骤/工具调用/关键决策/策略变更
2. 审批流:单人/多级/会签/或签,支持超时自动决策
3. 上下文快照:暂停时完整保存执行上下文,恢复无缝衔接
4. 修改与回滚:人工可修改计划/参数/代码,支持版本回滚
5. 审计追踪:所有介入操作记录到 Merkle 审计链
6. 通知集成:钉钉/Slack/邮件/飞书 Webhook 通知
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Optional

from app.config import settings
from app.core.storage_backend import StorageBackend, get_storage_backend

logger = logging.getLogger(__name__)


# ============================================================
# 枚举与数据模型
# ============================================================

class BreakpointType(str, Enum):
    """断点类型。"""
    PLAN_STEP = "plan_step"           # 计划步骤执行前
    TOOL_CALL = "tool_call"           # 工具调用前
    DECISION = "decision"             # 关键决策点
    POLICY_CHANGE = "policy_change"   # 策略/配置变更
    CODE_REVIEW = "code_review"       # 代码变更审查
    DEPLOYMENT = "deployment"         # 部署前确认


class ApprovalStatus(str, Enum):
    """审批状态。"""
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    MODIFIED = "modified"      # 批准但修改了内容
    TIMEOUT = "timeout"        # 超时自动决策
    ESCALATED = "escalated"    # 升级处理


class ApprovalMode(str, Enum):
    """审批模式。"""
    SINGLE = "single"           # 单人审批
    SEQUENTIAL = "sequential"   # 顺序会签(按顺序逐个审批)
    PARALLEL = "parallel"       # 并行会签(同时通知所有人)
    QUORUM = "quorum"           # 达成人数/比例即通过
    ANY = "any"                 # 任意一人批准即通过


@dataclass
class Approver:
    """审批人。"""
    id: str
    name: str
    role: str = ""
    contact: str = ""  # 邮件/钉钉ID/Slack ID/飞书ID
    weight: float = 1.0  # 权重(用于加权表决)


@dataclass
class Breakpoint:
    """断点定义。"""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = ""
    type: BreakpointType = BreakpointType.PLAN_STEP
    description: str = ""
    
    # 触发条件
    condition: dict = field(default_factory=dict)  # 如 {"step_index": 2, "tool_name": "write_file"}
    
    # 审批配置
    approvers: list[Approver] = field(default_factory=list)
    approval_mode: ApprovalMode = ApprovalMode.SINGLE
    quorum_count: int = 0  # QUORUM 模式下所需票数
    quorum_ratio: float = 0.0  # QUORUM 模式下所需比例
    
    # 超时处理
    timeout_seconds: int = 300  # 5分钟默认
    timeout_action: ApprovalStatus = ApprovalStatus.REJECTED  # 超时后自动拒绝/批准/升级
    
    # 允许操作
    allow_modify: bool = True      # 允许修改内容
    allow_rollback: bool = True    # 允许回滚
    allow_skip: bool = False       # 允许跳过
    
    # 元数据
    enabled: bool = True
    tags: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    
    def to_dict(self) -> dict:
        d = asdict(self)
        d["type"] = self.type.value
        d["approval_mode"] = self.approval_mode.value
        d["timeout_action"] = self.timeout_action.value
        d["approvers"] = [asdict(a) for a in self.approvers]
        return d
    
    @classmethod
    def from_dict(cls, data: dict) -> "Breakpoint":
        if "type" in data and isinstance(data["type"], str):
            data["type"] = BreakpointType(data["type"])
        if "approval_mode" in data and isinstance(data["approval_mode"], str):
            data["approval_mode"] = ApprovalMode(data["approval_mode"])
        if "timeout_action" in data and isinstance(data["timeout_action"], str):
            data["timeout_action"] = ApprovalStatus(data["timeout_action"])
        approvers = []
        for a in data.get("approvers", []):
            approvers.append(Approver(**a))
        data["approvers"] = approvers
        return cls(**data)


@dataclass
class ApprovalRequest:
    """审批请求(运行时实例)。"""
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    breakpoint_id: str = ""
    run_id: str = ""
    session_id: str = ""
    task_id: str = ""
    
    # 待审批内容
    title: str = ""
    description: str = ""
    payload: dict = field(default_factory=dict)  # 原始数据:计划步骤/工具参数/决策上下文
    context_snapshot: dict = field(default_factory=dict)  # 完整上下文快照
    
    # 审批状态
    status: ApprovalStatus = ApprovalStatus.PENDING
    approvers: list[Approver] = field(default_factory=list)
    approval_mode: ApprovalMode = ApprovalMode.SINGLE
    quorum_count: int = 0
    quorum_ratio: float = 0.0
    
    # 决策记录
    decisions: dict[str, dict] = field(default_factory=dict)  # approver_id -> {status, comment, modified_payload, timestamp}
    final_decision: ApprovalStatus = ApprovalStatus.PENDING
    final_payload: dict = field(default_factory=dict)  # 最终执行载荷(可能被修改)
    
    # 时间
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    expires_at: float = 0
    decided_at: float = 0
    
    # 通知
    notified: bool = False
    notification_channels: list[str] = field(default_factory=list)
    
    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        d["approval_mode"] = self.approval_mode.value
        d["final_decision"] = self.final_decision.value
        d["approvers"] = [asdict(a) for a in self.approvers]
        return d
    
    @classmethod
    def from_dict(cls, data: dict) -> "ApprovalRequest":
        if "status" in data and isinstance(data["status"], str):
            data["status"] = ApprovalStatus(data["status"])
        if "approval_mode" in data and isinstance(data["approval_mode"], str):
            data["approval_mode"] = ApprovalMode(data["approval_mode"])
        if "final_decision" in data and isinstance(data["final_decision"], str):
            data["final_decision"] = ApprovalStatus(data["final_decision"])
        approvers = []
        for a in data.get("approvers", []):
            approvers.append(Approver(**a))
        data["approvers"] = approvers
        return cls(**data)
    
    def is_expired(self) -> bool:
        return self.expires_at > 0 and time.time() > self.expires_at
    
    def evaluate_decisions(self) -> ApprovalStatus:
        """根据审批模式评估当前决策结果。"""
        if not self.approvers:
            return ApprovalStatus.APPROVED  # 无审批人自动通过
        
        approved = 0
        rejected = 0
        total_weight = 0
        approved_weight = 0
        
        for approver in self.approvers:
            decision = self.decisions.get(approver.id)
            if not decision:
                continue
            
            total_weight += approver.weight
            if decision.get("status") == ApprovalStatus.APPROVED.value:
                approved += 1
                approved_weight += approver.weight
            elif decision.get("status") == ApprovalStatus.REJECTED.value:
                rejected += 1
        
        if self.approval_mode == ApprovalMode.SINGLE:
            # 单人模式:第一个审批人决定
            for approver in self.approvers:
                decision = self.decisions.get(approver.id)
                if decision:
                    return ApprovalStatus(decision.get("status", ApprovalStatus.PENDING.value))
            return ApprovalStatus.PENDING
        
        elif self.approval_mode == ApprovalMode.SEQUENTIAL:
            # 顺序会签:按顺序,任一拒绝即拒绝
            for approver in self.approvers:
                decision = self.decisions.get(approver.id)
                if not decision:
                    return ApprovalStatus.PENDING
                if decision.get("status") == ApprovalStatus.REJECTED.value:
                    return ApprovalStatus.REJECTED
            return ApprovalStatus.APPROVED
        
        elif self.approval_mode == ApprovalMode.PARALLEL:
            # 并行会签:所有人都批准才通过
            if approved == len(self.approvers):
                return ApprovalStatus.APPROVED
            if rejected > 0:
                return ApprovalStatus.REJECTED
            return ApprovalStatus.PENDING
        
        elif self.approval_mode == ApprovalMode.QUORUM:
            # 达成人数/比例
            if self.quorum_count > 0 and approved >= self.quorum_count:
                return ApprovalStatus.APPROVED
            if self.quorum_ratio > 0 and total_weight > 0 and (approved_weight / total_weight) >= self.quorum_ratio:
                return ApprovalStatus.APPROVED
            if rejected > 0 and (len(self.approvers) - approved - rejected) == 0:
                return ApprovalStatus.REJECTED
            return ApprovalStatus.PENDING
        
        elif self.approval_mode == ApprovalMode.ANY:
            # 任意一人批准即通过
            if approved > 0:
                return ApprovalStatus.APPROVED
            if rejected == len(self.approvers):
                return ApprovalStatus.REJECTED
            return ApprovalStatus.PENDING
        
        return ApprovalStatus.PENDING
    
    def get_pending_approvers(self) -> list[Approver]:
        """获取尚未决策的审批人。"""
        pending = []
        for approver in self.approvers:
            if approver.id not in self.decisions:
                pending.append(approver)
        return pending


# ============================================================
# 断点管理器
# ============================================================

class BreakpointManager:
    """断点定义管理器。"""
    
    DEFAULT_BREAKPOINTS = [
        Breakpoint(
            id="bp_plan_step",
            name="计划步骤执行确认",
            type=BreakpointType.PLAN_STEP,
            description="重要计划步骤执行前需要人工确认",
            condition={"importance": "high"},
            approval_mode=ApprovalMode.SINGLE,
            timeout_seconds=300,
            timeout_action=ApprovalStatus.REJECTED,
            tags=["plan", "human_in_the_loop"],
        ),
        Breakpoint(
            id="bp_dangerous_tool",
            name="危险工具调用确认",
            type=BreakpointType.TOOL_CALL,
            description="高风险工具(run_terminal/run_code/write_file等)调用前确认",
            condition={"tool_category": "dangerous"},
            approval_mode=ApprovalMode.SINGLE,
            timeout_seconds=120,
            timeout_action=ApprovalStatus.REJECTED,
            tags=["tool", "security"],
        ),
        Breakpoint(
            id="bp_code_review",
            name="代码变更审查",
            type=BreakpointType.CODE_REVIEW,
            description="生产环境代码变更前需要代码审查",
            condition={"environment": "production", "file_types": [".py", ".js", ".ts", ".go", ".java"]},
            approval_mode=ApprovalMode.SEQUENTIAL,
            timeout_seconds=3600,
            timeout_action=ApprovalStatus.REJECTED,
            allow_modify=True,
            tags=["code", "review", "production"],
        ),
        Breakpoint(
            id="bp_deployment",
            name="部署确认",
            type=BreakpointType.DEPLOYMENT,
            description="生产环境部署前最终确认",
            condition={"action": "deploy", "environment": "production"},
            approval_mode=ApprovalMode.QUORUM,
            quorum_count=2,
            timeout_seconds=1800,
            timeout_action=ApprovalStatus.REJECTED,
            tags=["deployment", "production"],
        ),
    ]
    
    def __init__(self, db_path: Optional[str] = None, backend: Optional[StorageBackend] = None):
        if backend:
            self._backend = backend
            self._db_path = None
        else:
            self._db_path = db_path or os.path.join(settings.resolved_data_dir, "breakpoints.db")
            self._backend = get_storage_backend(db_path=self._db_path)
        self._lock = threading.RLock()
        self._init_db()
        self._load_defaults()
    
    def _init_db(self) -> None:
        schema = {
            "breakpoints": [
                {"name": "id", "type": "TEXT", "constraints": "PRIMARY KEY"},
                {"name": "definition", "type": "TEXT", "constraints": "NOT NULL"},
                {"name": "enabled", "type": "INTEGER", "constraints": "DEFAULT 1"},
                {"name": "created_at", "type": "REAL", "constraints": "NOT NULL"},
                {"name": "updated_at", "type": "REAL", "constraints": "NOT NULL"},
            ],
        }
        self._backend.init_db(schema)
    
    def _load_defaults(self) -> None:
        with self._lock:
            for bp in self.DEFAULT_BREAKPOINTS:
                existing = self._backend.select_one("breakpoints", "id = ?", (bp.id,))
                if not existing:
                    self._backend.insert(
                        "breakpoints",
                        {
                            "id": bp.id,
                            "definition": json.dumps(bp.to_dict(), ensure_ascii=False),
                            "enabled": 1,
                            "created_at": time.time(),
                            "updated_at": time.time(),
                        },
                    )
    
    def create(self, bp: Breakpoint) -> Breakpoint:
        with self._lock:
            bp.updated_at = time.time()
            self._backend.insert(
                "breakpoints",
                {
                    "id": bp.id,
                    "definition": json.dumps(bp.to_dict(), ensure_ascii=False),
                    "enabled": 1,
                    "created_at": bp.created_at,
                    "updated_at": bp.updated_at,
                },
            )
        return bp
    
    def get(self, bp_id: str) -> Optional[Breakpoint]:
        with self._lock:
            row = self._backend.select_one("breakpoints", "id = ?", (bp_id,))
        if not row:
            return None
        return Breakpoint.from_dict(json.loads(row["definition"]))
    
    def list(self, enabled_only: bool = True) -> list[Breakpoint]:
        with self._lock:
            if enabled_only:
                rows = self._backend.select("breakpoints", "enabled = 1")
            else:
                rows = self._backend.select("breakpoints", "1=1")
        return [Breakpoint.from_dict(json.loads(r["definition"])) for r in rows]
    
    def update(self, bp: Breakpoint) -> bool:
        with self._lock:
            bp.updated_at = time.time()
            result = self._backend.update(
                "breakpoints",
                {"definition": json.dumps(bp.to_dict(), ensure_ascii=False), "updated_at": bp.updated_at},
                "id = ?",
                (bp.id,),
            )
        return result > 0
    
    def delete(self, bp_id: str) -> bool:
        with self._lock:
            result = self._backend.delete("breakpoints", "id = ?", (bp_id,))
        return result > 0
    
    def match(self, event_type: BreakpointType, context: dict) -> list[Breakpoint]:
        """匹配触发的断点。"""
        breakpoints = self.list(enabled_only=True)
        matched = []
        for bp in breakpoints:
            if bp.type != event_type:
                continue
            # 简单条件匹配(可扩展为表达式引擎)
            match = True
            for key, value in bp.condition.items():
                if context.get(key) != value:
                    match = False
                    break
            if match:
                matched.append(bp)
        return matched


# ============================================================
# 审批请求管理器
# ============================================================

class ApprovalManager:
    """审批请求管理器:创建/查询/决策/通知。"""
    
    def __init__(
        self,
        db_path: Optional[str] = None,
        backend: Optional[StorageBackend] = None,
        breakpoint_manager: Optional[BreakpointManager] = None,
        audit_chain: Optional[Any] = None,
        notifier: Optional[Callable] = None,
    ):
        if backend:
            self._backend = backend
            self._db_path = None
        else:
            self._db_path = db_path or os.path.join(settings.resolved_data_dir, "approvals.db")
            self._backend = get_storage_backend(db_path=self._db_path)
        self._lock = threading.RLock()
        self._breakpoint_manager = breakpoint_manager or BreakpointManager()
        self._audit_chain = audit_chain
        self._notifier = notifier
        self._init_db()
        self._pending_requests: dict[str, asyncio.Event] = {}  # request_id -> event(用于等待)
        self._running = False
        self._timeout_checker: Optional[threading.Thread] = None
    
    def _init_db(self) -> None:
        schema = {
            "approval_requests": [
                {"name": "request_id", "type": "TEXT", "constraints": "PRIMARY KEY"},
                {"name": "breakpoint_id", "type": "TEXT", "constraints": "NOT NULL"},
                {"name": "run_id", "type": "TEXT", "constraints": "NOT NULL"},
                {"name": "session_id", "type": "TEXT", "constraints": "NOT NULL DEFAULT ''"},
                {"name": "task_id", "type": "TEXT", "constraints": ""},
                {"name": "request_data", "type": "TEXT", "constraints": "NOT NULL"},
                {"name": "status", "type": "TEXT", "constraints": "NOT NULL DEFAULT 'pending'"},
                {"name": "created_at", "type": "REAL", "constraints": "NOT NULL"},
                {"name": "updated_at", "type": "REAL", "constraints": "NOT NULL"},
                {"name": "expires_at", "type": "REAL", "constraints": "DEFAULT 0"},
                {"name": "decided_at", "type": "REAL", "constraints": "DEFAULT 0"},
            ],
        }
        self._backend.init_db(schema)
        self._backend.execute("CREATE INDEX IF NOT EXISTS idx_approval_run ON approval_requests(run_id)")
        self._backend.execute("CREATE INDEX IF NOT EXISTS idx_approval_status ON approval_requests(status)")
        self._backend.execute("CREATE INDEX IF NOT EXISTS idx_approval_expires ON approval_requests(expires_at)")
    
    def create_request(
        self,
        breakpoint: Breakpoint,
        run_id: str,
        session_id: str,
        task_id: str,
        title: str,
        description: str,
        payload: dict,
        context_snapshot: dict,
    ) -> ApprovalRequest:
        """创建审批请求。"""
        request = ApprovalRequest(
            breakpoint_id=breakpoint.id,
            run_id=run_id,
            session_id=session_id,
            task_id=task_id,
            title=title,
            description=description,
            payload=payload,
            context_snapshot=context_snapshot,
            approvers=breakpoint.approvers,
            approval_mode=breakpoint.approval_mode,
            quorum_count=breakpoint.quorum_count,
            quorum_ratio=breakpoint.quorum_ratio,
            expires_at=time.time() + breakpoint.timeout_seconds if breakpoint.timeout_seconds > 0 else 0,
        )
        
        with self._lock:
            self._backend.insert(
                "approval_requests",
                {
                    "request_id": request.request_id,
                    "breakpoint_id": request.breakpoint_id,
                    "run_id": request.run_id,
                    "session_id": request.session_id,
                    "task_id": request.task_id,
                    "request_data": json.dumps(request.to_dict(), ensure_ascii=False),
                    "status": request.status.value,
                    "created_at": request.created_at,
                    "updated_at": request.updated_at,
                    "expires_at": request.expires_at,
                    "decided_at": request.decided_at,
                },
            )
        
        # 记录审计
        if self._audit_chain:
            self._audit_chain.append(
                entry_type="approval_request_created",
                payload={"request_id": request.request_id, "breakpoint_id": breakpoint.id, "run_id": run_id},
                metadata={"title": title, "approvers": [a.id for a in breakpoint.approvers]},
            )
        
        # 异步发送通知
        if self._notifier and not request.notified:
            asyncio.create_task(self._send_notification(request))
            request.notified = True
            self._update_request(request)
        
        return request
    
    async def _send_notification(self, request: ApprovalRequest) -> None:
        """发送通知(异步)。"""
        if not self._notifier:
            return
        try:
            await self._notifier(request)
            request.notification_channels.append("webhook")
            self._update_request(request)
        except Exception as e:
            logger.warning("发送审批通知失败: %s", e)
    
    def _update_request(self, request: ApprovalRequest) -> None:
        request.updated_at = time.time()
        with self._lock:
            self._backend.update(
                "approval_requests",
                {
                    "request_data": json.dumps(request.to_dict(), ensure_ascii=False),
                    "status": request.status.value,
                    "updated_at": request.updated_at,
                    "expires_at": request.expires_at,
                    "decided_at": request.decided_at,
                },
                "request_id = ?",
                (request.request_id,),
            )
    
    def get_request(self, request_id: str) -> Optional[ApprovalRequest]:
        with self._lock:
            row = self._backend.select_one("approval_requests", "request_id = ?", (request_id,))
        if not row:
            return None
        return ApprovalRequest.from_dict(json.loads(row["request_data"]))
    
    def get_requests_by_run(self, run_id: str) -> list[ApprovalRequest]:
        with self._lock:
            rows = self._backend.select("approval_requests", "run_id = ?", (run_id,))
        return [ApprovalRequest.from_dict(json.loads(r["request_data"])) for r in rows]
    
    def get_pending_requests(self, session_id: str = None) -> list[ApprovalRequest]:
        with self._lock:
            if session_id:
                rows = self._backend.select(
                    "approval_requests",
                    "status = 'pending' AND session_id = ?",
                    (session_id,),
                )
            else:
                rows = self._backend.select("approval_requests", "status = 'pending'")
        return [ApprovalRequest.from_dict(json.loads(r["request_data"])) for r in rows]
    
    def decide(
        self,
        request_id: str,
        approver_id: str,
        decision: ApprovalStatus,
        comment: str = "",
        modified_payload: dict = None,
    ) -> ApprovalRequest:
        """提交审批决策。"""
        request = self.get_request(request_id)
        if not request:
            raise ValueError(f"审批请求不存在: {request_id}")
        
        if request.status != ApprovalStatus.PENDING:
            raise ValueError(f"请求已决策: {request.status.value}")
        
        if request.is_expired():
            request.status = ApprovalStatus.TIMEOUT
            request.final_decision = request.timeout_action
            request.decided_at = time.time()
            self._update_request(request)
            return request
        
        # 记录决策
        request.decisions[approver_id] = {
            "status": decision.value,
            "comment": comment,
            "modified_payload": modified_payload or {},
            "timestamp": time.time(),
        }
        
        # 评估整体结果
        result = request.evaluate_decisions()
        request.final_decision = result
        
        if result != ApprovalStatus.PENDING:
            request.status = result
            request.decided_at = time.time()
            if modified_payload:
                request.final_payload = modified_payload
            else:
                request.final_payload = request.payload
            
            # 记录审计
            if self._audit_chain:
                self._audit_chain.append(
                    entry_type="approval_decided",
                    payload={
                        "request_id": request.request_id,
                        "decision": result.value,
                        "approver": approver_id,
                    },
                    metadata={"run_id": request.run_id, "breakpoint_id": request.breakpoint_id},
                )
        
        self._update_request(request)
        
        # 唤醒等待者
        if request_id in self._pending_requests:
            self._pending_requests[request_id].set()
        
        return request
    
    async def wait_for_decision(self, request_id: str, timeout: float = None) -> ApprovalRequest:
        """等待审批决策(异步)。"""
        request = self.get_request(request_id)
        if not request:
            raise ValueError(f"审批请求不存在: {request_id}")
        
        if request.status != ApprovalStatus.PENDING:
            return request
        
        # 创建等待事件
        event = asyncio.Event()
        self._pending_requests[request_id] = event
        
        try:
            wait_timeout = timeout or (request.expires_at - time.time() if request.expires_at > 0 else 300)
            await asyncio.wait_for(event.wait(), timeout=max(1, wait_timeout))
        except asyncio.TimeoutError:
            # 超时处理
            request = self.get_request(request_id)
            if request and request.status == ApprovalStatus.PENDING:
                request.status = ApprovalStatus.TIMEOUT
                request.final_decision = request.timeout_action
                request.decided_at = time.time()
                self._update_request(request)
        finally:
            self._pending_requests.pop(request_id, None)
        
        return self.get_request(request_id)
    
    def start_timeout_checker(self, interval_seconds: int = 30) -> None:
        """启动超时检查器。"""
        if self._running:
            return
        self._running = True
        
        def check_loop():
            while self._running:
                try:
                    now = time.time()
                    with self._lock:
                        rows = self._backend.select(
                            "approval_requests",
                            "status = 'pending' AND expires_at > 0 AND expires_at < ?",
                            (now,),
                        )
                    for row in rows:
                        request = ApprovalRequest.from_dict(json.loads(row["request_data"]))
                        request.status = ApprovalStatus.TIMEOUT
                        request.final_decision = request.timeout_action
                        request.decided_at = time.time()
                        self._update_request(request)
                        
                        # 唤醒等待者
                        if request.request_id in self._pending_requests:
                            self._pending_requests[request.request_id].set()
                        
                        # 记录审计
                        if self._audit_chain:
                            self._audit_chain.append(
                                entry_type="approval_timeout",
                                payload={"request_id": request.request_id, "timeout_action": request.timeout_action.value},
                                metadata={"run_id": request.run_id},
                            )
                except Exception as e:
                    logger.error("审批超时检查异常: %s", e)
                
                time.sleep(interval_seconds)
        
        self._timeout_checker = threading.Thread(target=check_loop, daemon=True)
        self._timeout_checker.start()
        logger.info("审批超时检查器已启动")
    
    def stop_timeout_checker(self) -> None:
        self._running = False
        if self._timeout_checker:
            self._timeout_checker.join(timeout=5)
        logger.info("审批超时检查器已停止")


# ============================================================
# 人工介入协调器
# ============================================================

class HumanInTheLoopCoordinator:
    """人工介入协调器:整合断点匹配、审批流程、上下文管理。"""
    
    def __init__(
        self,
        breakpoint_manager: Optional[BreakpointManager] = None,
        approval_manager: Optional[ApprovalManager] = None,
        audit_chain: Optional[Any] = None,
        notifier: Optional[Callable] = None,
    ):
        self.breakpoint_manager = breakpoint_manager or BreakpointManager()
        self.approval_manager = approval_manager or ApprovalManager(
            breakpoint_manager=self.breakpoint_manager,
            audit_chain=audit_chain,
            notifier=notifier,
        )
        self._context_snapshots: dict[str, dict] = {}  # run_id -> context
    
    def capture_context(self, run_id: str, context: dict) -> None:
        """捕获运行时上下文快照。"""
        self._context_snapshots[run_id] = context
    
    def check_breakpoint(
        self,
        run_id: str,
        session_id: str,
        task_id: str,
        event_type: BreakpointType,
        event_data: dict,
    ) -> Optional[ApprovalRequest]:
        """检查是否触发断点,如果触发则创建审批请求。"""
        context = self._context_snapshots.get(run_id, {})
        context.update(event_data)
        
        matched = self.breakpoint_manager.match(event_type, context)
        if not matched:
            return None
        
        # 取优先级最高的断点(按创建顺序)
        bp = matched[0]
        
        # 创建审批请求
        request = self.approval_manager.create_request(
            breakpoint=bp,
            run_id=run_id,
            session_id=session_id,
            task_id=task_id,
            title=f"{bp.name}: {event_data.get('summary', '')}",
            description=bp.description,
            payload=event_data,
            context_snapshot=context,
        )
        
        logger.info("触发人工断点: %s (request_id=%s)", bp.name, request.request_id[:8])
        return request
    
    async def wait_for_approval(self, request_id: str, timeout: float = None) -> ApprovalRequest:
        """等待审批结果。"""
        return await self.approval_manager.wait_for_decision(request_id, timeout)
    
    def apply_decision(self, request: ApprovalRequest, executor: Callable) -> Any:
        """应用审批决策并继续执行。"""
        if request.final_decision == ApprovalStatus.APPROVED:
            # 直接执行
            return executor(request.final_payload or request.payload)
        
        elif request.final_decision == ApprovalStatus.MODIFIED:
            # 执行修改后的内容
            return executor(request.final_payload)
        
        elif request.final_decision == ApprovalStatus.REJECTED:
            raise HumanInterventionRejected(f"人工拒绝: {request.title}")
        
        elif request.final_decision == ApprovalStatus.TIMEOUT:
            raise HumanInterventionTimeout(f"审批超时自动{request.timeout_action.value}: {request.title}")
        
        else:
            raise HumanInterventionError(f"未知决策状态: {request.final_decision.value}")
    
    def rollback_to_checkpoint(self, run_id: str, checkpoint_id: str) -> bool:
        """回滚到指定检查点。"""
        # 这里需要配合 checkpoint 系统实现
        # 简化返回
        logger.info("请求回滚: run_id=%s, checkpoint=%s", run_id[:8], checkpoint_id)
        return True
    
    def get_pending_for_session(self, session_id: str) -> list[ApprovalRequest]:
        """获取会话的待审批请求。"""
        return self.approval_manager.get_pending_requests(session_id)
    
    def start(self) -> None:
        """启动协调器(后台任务)。"""
        self.approval_manager.start_timeout_checker()
    
    def stop(self) -> None:
        """停止协调器。"""
        self.approval_manager.stop_timeout_checker()


# 异常类
class HumanInterventionError(Exception):
    pass

class HumanInterventionRejected(HumanInterventionError):
    pass

class HumanInterventionTimeout(HumanInterventionError):
    pass


# ============================================================
# 默认通知器
# ============================================================

async def default_notifier(request: ApprovalRequest) -> None:
    """默认通知器:打印日志 + 可扩展 Webhook。"""
    webhook_urls = getattr(settings, "webhook_urls", "")
    if not webhook_urls:
        logger.info("审批通知: %s (request_id=%s)", request.title, request.request_id[:8])
        return
    
    urls = [u.strip() for u in webhook_urls.split(",") if u.strip()]
    import httpx
    
    payload = {
        "type": "approval_request",
        "request_id": request.request_id,
        "title": request.title,
        "description": request.description,
        "payload": request.payload,
        "approvers": [{"id": a.id, "name": a.name, "contact": a.contact} for a in request.approvers],
        "expires_at": request.expires_at,
        "approval_url": f"{getattr(settings, 'agent_host', 'http://localhost:8080')}/approvals/{request.request_id}",
    }
    
    async with httpx.AsyncClient() as client:
        for url in urls:
            try:
                await client.post(url, json=payload, timeout=10)
            except Exception as e:
                logger.warning("Webhook 通知失败 %s: %s", url, e)


# 全局单例
_hitl_coordinator: Optional[HumanInTheLoopCoordinator] = None


def get_hitl_coordinator(
    audit_chain: Optional[Any] = None,
    notifier: Optional[Callable] = None,
) -> HumanInTheLoopCoordinator:
    global _hitl_coordinator
    if _hitl_coordinator is None:
        _hitl_coordinator = HumanInTheLoopCoordinator(
            audit_chain=audit_chain,
            notifier=notifier or default_notifier,
        )
    return _hitl_coordinator