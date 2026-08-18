"""记忆冲突仲裁与合并。

解决以下问题:
1. 重复记忆合并（相似度 > 0.85）
2. 矛盾记忆仲裁（保留新版本，标记旧版本）
3. 记忆时效衰减（TTL + 自动过期）
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from app.rag.embeddings import get_embedding_client, cosine_similarity
from app.core.storage_backend import StorageBackend, get_storage_backend
from app.config import settings


@dataclass
class MemoryConflict:
    """记忆冲突记录。"""
    old_memory_id: str
    new_memory_id: str
    conflict_type: str  # "duplicate" / "contradiction"
    resolution: str = "new_wins"  # "new_wins" / "merged" / "manual"
    resolved_at: float = field(default_factory=time.time)


class MemoryConflictResolver:
    """记忆冲突仲裁器。"""
    
    # 重复判定阈值
    DUPLICATE_THRESHOLD = 0.85
    # 记忆有效期（天）
    MEMORY_TTL_DAYS = 180
    
    def __init__(self, backend: Optional[StorageBackend] = None):
        self._backend = backend or get_storage_backend()
        self._lock = threading.RLock()
    
    def check_and_resolve(
        self,
        new_content: str,
        new_keywords: list[str],
        session_id: str = "",
        tenant_id: str = "default",
    ) -> dict:
        """检查新记忆是否与现有记忆冲突，并返回仲裁结果。
        
        Returns:
            {
                "action": "insert" / "merge" / "update",
                "memory_id": str,
                "conflict": Optional[MemoryConflict],
                "reason": str,
            }
        """
        with self._lock:
            # 1. 查找相似记忆
            similar = self._find_similar_memories(new_content, top_k=3)
            
            if not similar:
                return {
                    "action": "insert",
                    "memory_id": None,
                    "conflict": None,
                    "reason": "无相似记忆，直接插入",
                }
            
            best_match = similar[0]
            similarity = best_match.get("score", 0)
            
            if similarity >= self.DUPLICATE_THRESHOLD:
                # 高度相似:更新旧记忆
                return self._handle_duplicate(
                    old_id=best_match["id"],
                    old_content=best_match["content"],
                    new_content=new_content,
                    new_keywords=new_keywords,
                    session_id=session_id,
                    tenant_id=tenant_id,
                )
            else:
                # 低相似度:检查是否矛盾
                conflict = self._check_contradiction(
                    old_content=best_match["content"],
                    new_content=new_content,
                )
                if conflict:
                    return {
                        "action": "insert_with_note",
                        "memory_id": None,
                        "conflict": conflict,
                        "reason": f"与现有记忆可能存在矛盾(相似度={similarity:.2f})",
                    }
                else:
                    return {
                        "action": "insert",
                        "memory_id": None,
                        "conflict": None,
                        "reason": f"相似度={similarity:.2f}，无冲突",
                    }
    
    def _find_similar_memories(
        self,
        content: str,
        top_k: int = 3,
    ) -> list[dict]:
        """查找相似记忆。"""
        from app.memory.ltm import get_memory_store
        store = get_memory_store()
        
        # 使用 LTM 检索
        results = store.search(content, top_k=top_k)
        
        # 计算相似度
        emb_client = get_embedding_client()
        if emb_client is None:
            return []
        
        new_vec = emb_client.embed(content)
        if new_vec is None:
            return []
        
        scored = []
        for r in results:
            old_vec = r.get("vector")
            if old_vec:
                sim = cosine_similarity(new_vec, old_vec)
                scored.append({**r, "score": sim})
        
        return sorted(scored, key=lambda x: x["score"], reverse=True)
    
    def _handle_duplicate(
        self,
        old_id: str,
        old_content: str,
        new_content: str,
        new_keywords: list[str],
        session_id: str,
        tenant_id: str,
    ) -> dict:
        """处理重复记忆:更新旧记录。"""
        # 更新旧记忆内容（保留创建时间，更新更新时间）
        self._backend.execute(
            """UPDATE memories 
               SET content = ?, keywords = ?, updated_at = ?, hit_count = hit_count + 1
               WHERE id = ?""",
            (new_content, ",".join(new_keywords), time.time(), old_id),
        )
        
        return {
            "action": "update",
            "memory_id": old_id,
            "conflict": MemoryConflict(
                old_memory_id=old_id,
                new_memory_id=old_id,  # 复用旧ID
                conflict_type="duplicate",
                resolution="merged",
            ),
            "reason": f"与现有记忆高度相似，已合并更新",
        }
    
    def _check_contradiction(
        self,
        old_content: str,
        new_content: str,
    ) -> Optional[MemoryConflict]:
        """检查是否存在矛盾（简化版:关键词冲突检测）。"""
        old_kw = set(old_content.lower().split())
        new_kw = set(new_content.lower().split())
        
        # 简单启发式:如果新旧内容完全没有共同词，可能是矛盾
        overlap = old_kw & new_kw
        if len(overlap) < 2:
            return MemoryConflict(
                old_memory_id="",  # 未知
                new_memory_id="",
                conflict_type="contradiction",
            )
        return None
    
    def cleanup_expired(self, max_age_days: Optional[int] = None) -> int:
        """清理过期记忆。
        
        Returns:
            清理的记忆数量
        """
        ttl = (max_age_days or self.MEMORY_TTL_DAYS) * 86400
        cutoff = time.time() - ttl
        
        result = self._backend.execute(
            "DELETE FROM memories WHERE created_at < ?",
            (cutoff,),
        )
        
        return result.rowcount if result else 0
    
    def decay_score(self, memory_id: str, decay_factor: float = 0.99):
        """对记忆进行衰减评分（降低检索优先级）。"""
        self._backend.execute(
            "UPDATE memories SET hit_count = hit_count * ? WHERE id = ?",
            (decay_factor, memory_id),
        )


# 全局单例
_resolver: Optional[MemoryConflictResolver] = None


def get_conflict_resolver() -> MemoryConflictResolver:
    """获取记忆冲突仲裁器单例。"""
    global _resolver
    if _resolver is None:
        _resolver = MemoryConflictResolver()
    return _resolver


def resolve_memory_conflict(
    content: str,
    keywords: list[str] = None,
    session_id: str = "",
    tenant_id: str = "default",
) -> dict:
    """便捷函数:检查并解决记忆冲突。"""
    return get_conflict_resolver().check_and_resolve(
        content,
        keywords or [],
        session_id,
        tenant_id,
    )
