"""人机协作工具:危险操作确认、审批流程、人工介入。"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime
from typing import Optional

from app.config import settings
from app.tools.registry import tool


class ConfirmationStore:
    """确认请求存储。"""

    def __init__(self, db_path: str = ""):
        # 默认存到实际数据目录(开发=项目根/data,打包=exe 旁/data),避免部署错位
        if not db_path:
            db_path = os.path.join(settings.resolved_data_dir, "confirmations.db")
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()
    
    def _get_conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
    
    def _init_db(self):
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS confirmations (
                id TEXT PRIMARY KEY,
                action TEXT NOT NULL,
                description TEXT NOT NULL,
                params TEXT DEFAULT '{}',
                status TEXT DEFAULT 'pending',
                result TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                responded_at TEXT
            )
        """)
        conn.commit()
        conn.close()
    
    def create(self, action: str, description: str, params: dict = None) -> dict:
        conf_id = str(uuid.uuid4())[:8]
        now = datetime.now().isoformat()
        
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO confirmations (id, action, description, params, status, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?)
        """, (conf_id, action, description, json.dumps(params or {}), now))
        conn.commit()
        conn.close()
        
        return {
            "id": conf_id,
            "action": action,
            "description": description,
            "status": "pending",
            "created_at": now,
        }
    
    def respond(self, conf_id: str, approved: bool, result: str = "") -> Optional[dict]:
        conn = self._get_conn()
        cursor = conn.cursor()
        
        now = datetime.now().isoformat()
        status = "approved" if approved else "rejected"
        
        cursor.execute("""
            UPDATE confirmations SET status = ?, result = ?, responded_at = ?
            WHERE id = ? AND status = 'pending'
        """, (status, result, now, conf_id))
        
        if cursor.rowcount > 0:
            conn.commit()
            conn.close()
            return {"id": conf_id, "status": status, "result": result}

        conn.close()
        # 已处理:重复响应(前端双卡片/重复点击/轮询重入)不再报错,以首次决定为准。
        # 否则用户点第二次会看到"确认请求不存在或已处理"的错误横幅(实测 2026-09-20)。
        existing = self.get(conf_id)
        if existing:
            return {
                "id": conf_id,
                "status": existing["status"],
                "result": existing.get("result", ""),
                "already": True,
            }
        return None
    
    def get(self, conf_id: str) -> Optional[dict]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM confirmations WHERE id = ?", (conf_id,))
        row = cursor.fetchone()
        conn.close()
        
        if row:
            return {
                "id": row["id"],
                "action": row["action"],
                "description": row["description"],
                "params": json.loads(row["params"]),
                "status": row["status"],
                "result": row["result"],
                "created_at": row["created_at"],
                "responded_at": row["responded_at"],
            }
        return None
    
    def list_pending(self) -> list[dict]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM confirmations WHERE status = 'pending' ORDER BY created_at DESC")
        rows = cursor.fetchall()
        conn.close()
        
        return [
            {
                "id": row["id"],
                "action": row["action"],
                "description": row["description"],
                "status": row["status"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]


_store: Optional[ConfirmationStore] = None


def _get_store() -> ConfirmationStore:
    global _store
    if _store is None:
        _store = ConfirmationStore()
    return _store


@tool("request_confirmation", "请求用户确认危险操作", timeout=300)
def request_confirmation(
    action: str,
    description: str,
    params: str = "",
) -> dict:
    """
    请求用户确认操作。
    
    当智能体需要执行危险操作(如删除文件、发送邮件、执行 SQL 等)时,
    先调用此工具请求确认,等用户确认后再执行。
    
    Args:
        action: 操作名称(如 "delete_file", "send_email", "execute_sql")
        description: 操作描述,告诉用户将要做什么
        params: 操作参数(JSON 格式)
    
    Returns:
        确认请求结果
    """
    store = _get_store()
    params_dict = json.loads(params) if params else {}
    
    result = store.create(action=action, description=description, params=params_dict)
    
    return {
        "status": "pending",
        "message": f"请确认操作: {description}",
        "confirmation_id": result["id"],
        "hint": "等待用户确认,请调用 approve_confirmation 或 reject_confirmation"
    }


@tool("approve_confirmation", "批准确认请求", timeout=10)
def approve_confirmation(
    confirmation_id: str,
    result: str = "",
) -> dict:
    """
    批准一个待确认的操作。
    
    Args:
        confirmation_id: 确认请求 ID
        result: 批准时的备注(可选)
    
    Returns:
        操作结果
    """
    store = _get_store()
    conf = store.respond(confirmation_id, approved=True, result=result)
    
    if conf:
        return {"status": "ok", "message": "操作已批准", "confirmation": conf}
    else:
        return {"status": "error", "error": f"确认请求不存在或已处理: {confirmation_id}"}


@tool("reject_confirmation", "拒绝确认请求", timeout=10)
def reject_confirmation(
    confirmation_id: str,
    reason: str = "",
) -> dict:
    """
    拒绝一个待确认的操作。
    
    Args:
        confirmation_id: 确认请求 ID
        reason: 拒绝原因
    
    Returns:
        操作结果
    """
    store = _get_store()
    conf = store.respond(confirmation_id, approved=False, result=reason)
    
    if conf:
        return {"status": "ok", "message": "操作已拒绝", "confirmation": conf}
    else:
        return {"status": "error", "error": f"确认请求不存在或已处理: {confirmation_id}"}


@tool("list_pending_confirmations", "列出待确认的操作", timeout=10)
def list_pending_confirmations() -> dict:
    """
    列出所有待确认的操作。
    
    Returns:
        待确认列表
    """
    store = _get_store()
    pending = store.list_pending()
    
    return {
        "status": "ok",
        "total": len(pending),
        "pending": pending,
    }


@tool("get_confirmation", "获取确认请求详情", timeout=10)
def get_confirmation(
    confirmation_id: str,
) -> dict:
    """
    获取指定确认请求的详情。
    
    Args:
        confirmation_id: 确认请求 ID
    
    Returns:
        请求详情
    """
    store = _get_store()
    conf = store.get(confirmation_id)
    
    if conf:
        return {"status": "ok", "confirmation": conf}
    else:
        return {"status": "error", "error": f"确认请求不存在: {confirmation_id}"}


@tool("auto_confirm_check", "检查操作是否需要确认", timeout=10)
def auto_confirm_check(
    action: str,
) -> dict:
    """
    检查指定操作是否需要用户确认。
    
    基于预定义的规则判断操作风险等级。
    
    Args:
        action: 操作名称或描述
    
    Returns:
        风险评估结果
    """
    action_lower = action.lower()
    
    # 高风险操作 - 必须确认
    high_risk = [
        "delete", "drop", "remove", "destroy", "execute",
        "send_email", "publish", "deploy", "transfer", "payment",
        "删除", "移除", "执行", "发送", "发布", "部署", "转账", "支付",
    ]
    
    # 中风险操作 - 建议确认
    medium_risk = [
        "update", "modify", "edit", "change", "replace",
        "rename", "move", "copy", "export",
        "更新", "修改", "编辑", "替换", "重命名", "移动", "导出",
    ]
    
    for keyword in high_risk:
        if keyword in action_lower:
            return {
                "risk_level": "high",
                "needs_confirmation": True,
                "message": f"检测到高风险操作: {keyword}",
            }
    
    for keyword in medium_risk:
        if keyword in action_lower:
            return {
                "risk_level": "medium",
                "needs_confirmation": True,
                "message": f"检测到中风险操作: {keyword}",
            }
    
    return {
        "risk_level": "low",
        "needs_confirmation": False,
        "message": "操作风险较低,无需确认",
    }
