"""审计链:基于 Merkle 树的不可篡改操作审计日志。

复用 guardrails_semantic 中的 MerkleAuditChain 实现。
"""
from __future__ import annotations

from typing import Any, Optional

from app.core.guardrails_semantic import MerkleAuditChain, AuditEntry


def get_audit_chain(chain_id: str = "default") -> MerkleAuditChain:
    """获取审计链实例(单例模式)。"""
    # 使用模块级缓存实现单例
    if not hasattr(get_audit_chain, "_instances"):
        get_audit_chain._instances = {}
    
    if chain_id not in get_audit_chain._instances:
        get_audit_chain._instances[chain_id] = MerkleAuditChain(chain_id=chain_id)
    
    return get_audit_chain._instances[chain_id]