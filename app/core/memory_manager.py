"""统一记忆管理器：合并 decay/ttl/resolver/cleanup 功能

设计原则:
1. 单一职责: 一个类处理所有记忆生命周期管理
2. 统一接口: retrieve/store/cleanup/compress 四个核心方法
3. 向后兼容: 保留原有类名作为别名，避免破坏导入
"""
from __future__ import annotations

import json
import math
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from app.config import settings
from app.memory.ltm import get_memory_store, MemoryStore
from app.rag.store import cosine_similarity

logger = logging.getLogger(__name__)


# ── 数据模型 ────────────────────────────────────────────────────────

@dataclass
class DecayRule:
    """记忆衰减规则。"""
    memory_type: str
    half_life_hours: float
    min_strength: float = 0.05


DEFAULT_DECAY_RULES = [
    DecayRule("fact", half_life_hours=720, min_strength=0.1),
    DecayRule("preference", half_life_hours=2160, min_strength=0.05),
    DecayRule("habit", half_life_hours=4320, min_strength=0.05),
    DecayRule("decision", half_life_hours=2160, min_strength=0.1),
    DecayRule("skill", half_life_hours=8760, min_strength=0.01),
]


@dataclass
class MemoryConflict:
    """记忆冲突记录。"""
    old_memory_id: str
    new_memory_id: str
    conflict_type: str  # "duplicate" / "contradiction"
    resolution: str = "new_wins"
    resolved_at: float = field(default_factory=time.time)


# ── 统一记忆管理器 ───────────────────────────────────────────────────

class UnifiedMemoryManager:
    """统一记忆管理器：整合 TTL、衰减、冲突检测、清理功能。
    
    合并原: decay.py + memory_ttl.py + memory_resolver.py + memory_cleanup.py
    """
    
    # 默认配置
    DEFAULT_TTL_DAYS = 180
    DEFAULT_DECAY_RATE = 0.95
    DEFAULT_MIN_SCORE = 0.1
    DUPLICATE_THRESHOLD = 0.85
    
    def __init__(
        self,
        store: Optional[MemoryStore] = None,
        decay_rules: Optional[list[DecayRule]] = None,
        ttl_days: int = None,
        decay_rate: float = None,
        min_score: float = None,
    ):
        self._store = store or get_memory_store()
        self._decay_rules = decay_rules or DEFAULT_DECAY_RULES
        self._ttl_days = ttl_days or int(getattr(settings, "memory_ttl_days", self.DEFAULT_TTL_DAYS))
        self._decay_rate = decay_rate or float(getattr(settings, "memory_decay_factor", self.DEFAULT_DECAY_RATE))
        self._min_score = min_score or float(getattr(settings, "memory_min_score", self.DEFAULT_MIN_SCORE))
        self._lock = threading.RLock()
        self._last_cleanup = 0.0
        self._cleanup_interval = int(getattr(settings, "memory_cleanup_interval", 3600))
    
    # ── TTL 计算 ─────────────────────────────────────────────────────
    
    def calculate_score(
        self,
        created_at: float,
        updated_at: float,
        hit_count: int = 0,
    ) -> float:
        """计算记忆当前分数 [0, 1]。

        使用线性衰减 + 访问频率加成，确保 TTL 阈值生效。
        """
        now = time.time()
        base_time = max(created_at, updated_at)
        age_days = (now - base_time) / 86400
        ttl_days = max(1, self._ttl_days)
        # 线性衰减：每天减少 1/ttl_days，时间分最多占 0.7
        time_score = max(0.0, (1.0 - age_days / ttl_days)) * 0.7
        # 访问频率加分（最多 0.3），确保低频和高频有明显差异
        access_bonus = min(0.3, math.log1p(hit_count) * 0.1)
        return min(1.0, max(0.0, time_score + access_bonus))
    
    def should_evict(self, created_at: float, updated_at: float, hit_count: int = 0) -> bool:
        """判断记忆是否应该被清除。"""
        return self.calculate_score(created_at, updated_at, hit_count) < self._min_score
    
    # ── 记忆检索 ─────────────────────────────────────────────────────
    
    def retrieve(self, query: str, top_k: int = 4, session_id: str = "") -> list[dict]:
        """统一检索接口：向量 + 关键词混合检索。"""
        return self._store.search(query, top_k=top_k, session_id=session_id)
    
    def store(self, content: str, metadata: dict = None) -> str:
        """统一存储接口。"""
        return self._store.add(content, metadata or {})
    
    # ── 冲突检测 ─────────────────────────────────────────────────────
    
    def check_and_resolve(
        self,
        new_content: str,
        new_keywords: list[str],
        session_id: str = "",
        tenant_id: str = "default",
    ) -> dict:
        """检查新记忆是否与现有记忆冲突，并返回仲裁结果。
        
        合并原 memory_resolver.py 的 check_and_resolve 逻辑。
        """
        with self._lock:
            similar = self._find_similar_memories(new_content, top_k=3)
            
            if not similar:
                return {"action": "insert", "memory_id": None, "conflict": None, "reason": "无相似记忆"}
            
            best_match = similar[0]
            similarity = best_match.get("score", 0)
            
            if similarity >= self.DUPLICATE_THRESHOLD:
                return self._handle_duplicate(best_match["id"], best_match["content"], new_content)
            else:
                return {"action": "insert", "memory_id": None, "conflict": None, "reason": f"相似度={similarity:.2f}"}
    
    def _find_similar_memories(self, content: str, top_k: int = 3) -> list[dict]:
        """查找相似记忆。"""
        try:
            from app.rag.embeddings import get_embedding_client
            embed_client = get_embedding_client()
            if embed_client:
                query_vec = embed_client.embed([content])[0]
                all_memories = self._store.list(limit=100)
                similar = []
                for mem in all_memories:
                    if mem.get("vector"):
                        sim = cosine_similarity(query_vec, json.loads(mem["vector"]))
                        similar.append({**mem, "score": sim})
                similar.sort(key=lambda x: x["score"], reverse=True)
                return similar[:top_k]
        except Exception as e:
            logger.warning("Embedding search failed: %s", e)
        return []
    
    def _handle_duplicate(self, old_id: str, old_content: str, new_content: str) -> dict:
        """处理重复记忆。"""
        # 更新旧记忆，保留新版本
        self._store.update(old_id, {"content": new_content, "updated_at": time.time()})
        return {
            "action": "merge",
            "memory_id": old_id,
            "conflict": MemoryConflict(old_id, "", "duplicate", "merged"),
            "reason": "检测到重复记忆，已合并",
        }
    
    # ── 清理与衰减 ───────────────────────────────────────────────────
    
    def cleanup_expired(self, max_age_days: int = None) -> dict:
        """清理过期记忆。
        
        合并原 memory_ttl.py 和 memory_cleanup.py 的清理逻辑。
        """
        max_age_days = max_age_days or self._ttl_days
        cutoff = time.time() - (max_age_days * 86400)
        
        with self._lock:
            # 获取所有记忆
            memories = self._store.list(limit=1000)
            total = len(memories)
            to_evict = []
            
            for m in memories:
                created_at = m.get("created_at", 0) or 0
                updated_at = m.get("updated_at", 0) or 0
                hit_count = m.get("hit_count", 0) or 0
                
                if self.should_evict(created_at, updated_at, hit_count):
                    to_evict.append(m["id"])
            
            # 执行删除
            evicted = 0
            if to_evict:
                evicted = self._store.delete_batch(to_evict)
            
            return {
                "total_checked": total,
                "evicted": evicted,
                "kept": total - evicted,
            }
    
    def run_decay(self, dry_run: bool = True) -> dict:
        """执行记忆衰减计算，返回统计。
        
        合并原 memory_cleanup.py 的 run_decay 逻辑。
        """
        now = time.time()
        rules_map = {r.memory_type: r for r in self._decay_rules}
        updates = []
        candidates = []
        
        with self._lock:
            rows = self._store._backend.select("memories", "1=1", limit=1000)
            for row in rows:
                content = row.get("content", "")
                keywords = json.loads(row.get("keywords") or "[]")
                mtype = self._infer_type(content, keywords)
                rule = rules_map.get(mtype)
                if rule is None:
                    continue
                
                age_hours = (now - row["created_at"]) / 3600
                hit_count = row.get("hit_count", 0)
                base_strength = min(1.0, hit_count / 100)
                decayed_strength = rule.min_strength + (base_strength - rule.min_strength) * (
                    0.5 ** (age_hours / rule.half_life_hours)
                )
                
                if decayed_strength < base_strength - 0.01:
                    updates.append({
                        "id": row["id"],
                        "old_strength": round(base_strength, 3),
                        "new_strength": round(decayed_strength, 3),
                        "age_hours": round(age_hours, 1),
                        "type": mtype,
                    })
                if decayed_strength < rule.min_strength:
                    candidates.append(row["id"])
        
        stats = {
            "total_checked": len(rows),
            "updated_strength": len(updates),
            "cleanup_candidates": len(candidates),
            "updates": updates[:20],
        }
        
        if not dry_run:
            self._apply_updates(updates)
            stats["applied"] = True
        else:
            stats["applied"] = False
        
        return stats
    
    def _infer_type(self, content: str, keywords: list[str]) -> str:
        """推断记忆类型。"""
        content_lower = content.lower()
        if any(k in content_lower for k in ["偏好", "喜欢", "讨厌", "偏好"]):
            return "preference"
        if any(k in content_lower for k in ["习惯", "通常", "经常"]):
            return "habit"
        if any(k in content_lower for k in ["决定", "选择", "决策"]):
            return "decision"
        if any(k in content_lower for k in ["技能", "知识", "能力"]):
            return "skill"
        return "fact"
    
    def _apply_updates(self, updates: list[dict]) -> None:
        """应用衰减更新。"""
        for update in updates:
            self._store.update(update["id"], {"strength": update["new_strength"]})
    
    # ── 记忆压缩 ─────────────────────────────────────────────────────
    
    async def compress(self, session_id: str, max_tokens: int = 200000) -> str:
        """压缩会话历史为摘要。"""
        from app.memory.summarize import summarize_messages_async
        from app.core.llm import LLMClient
        
        history = self._store.get_session_history(session_id)
        llm = LLMClient.from_settings()
        return await summarize_messages_async(llm, history, max_tokens)
    
    # ── 统计接口 ─────────────────────────────────────────────────────
    
    def stats(self) -> dict:
        """获取记忆系统统计。"""
        total = self._store.count()
        expired = len([m for m in self._store.list(limit=1000) 
                      if self.should_evict(m.get("created_at", 0), m.get("updated_at", 0), m.get("hit_count", 0))])
        return {
            "total_memories": total,
            "expired_count": expired,
            "ttl_days": self._ttl_days,
            "decay_rate": self._decay_rate,
            "min_score": self._min_score,
        }


# ── 单例实例 ─────────────────────────────────────────────────────────

_instance = None
_instance_lock = threading.Lock()


def get_memory_manager() -> UnifiedMemoryManager:
    """获取统一记忆管理器单例。"""
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = UnifiedMemoryManager()
        return _instance


# ── 向后兼容别名 ─────────────────────────────────────────────────────
# 保留原有类名作为别名，避免破坏现有导入

class MemoryDecay(UnifiedMemoryManager):
    """向后兼容别名。"""
    pass


class MemoryTTLManager(UnifiedMemoryManager):
    """向后兼容别名。"""
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._running = False
        self._task = None
    
    def get_memory_score(self, memory: dict) -> float:
        """计算记忆分数（向后兼容）。"""
        now = time.time()
        created_at = memory.get('created_at', now)
        hit_count = memory.get('hit_count', 0)
        return self.calculate_score(created_at, created_at, hit_count)
    
    def should_prune(self, memory: dict, current_count: int = 0) -> bool:
        """判断记忆是否应该被剪枝（向后兼容）。"""
        now = time.time()
        created_at = memory.get('created_at', now)
        hit_count = memory.get('hit_count', 0)
        return self.should_evict(created_at, created_at, hit_count)
    
    def stop_background_cleanup(self):
        """停止后台清理（向后兼容，实际无需操作）。"""
        self._running = False


class MemoryConflictResolver(UnifiedMemoryManager):
    """向后兼容别名。"""
    pass


class MemoryCleanupEngine(UnifiedMemoryManager):
    """向后兼容别名。"""
    pass
