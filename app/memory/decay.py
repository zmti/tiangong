"""记忆衰退与聚类:自动淘汰低价值记忆,并将相似记忆合并。

衰退策略:
- 时间衰减:超过 TTL 的记忆分数随时间递减
- 访问频率:很少被检索的记忆降低优先级
- 自动清理:分数低于阈值的记忆被标记为待删除

聚类策略:
- 基于关键词和向量相似度,将相关记忆合并
- 合并后保留原始记忆的引用关系
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from typing import Optional

from app.config import settings


class MemoryDecay:
    """记忆衰退管理器。"""

    def __init__(
        self,
        ttl_hours: float = 720.0,      # 30天默认TTL
        decay_rate: float = 0.95,      # 每过一个TTL周期衰减系数
        min_score: float = 0.1,        # 低于此分数视为过期
        check_interval_hours: float = 24.0,  # 定期清理间隔
    ):
        self.ttl_hours = ttl_hours
        self.decay_rate = decay_rate
        self.min_score = min_score
        self.check_interval = check_interval_hours * 3600
        self._last_cleanup = 0.0
        self._lock = threading.Lock()

    def calculate_score(
        self,
        created_at: float,
        updated_at: float,
        hit_count: int = 0,
    ) -> float:
        """计算记忆当前分数 [0, 1]。

        使用 max(created_at, updated_at) 作为基准时间,
        确保"近期更新过"的记忆不会因为创建时间久远而被错误地给予高分。
        """
        now = time.time()
        # 修复:使用 max(created, updated) 而非仅 created,避免旧记录因不再更新而得分虚高
        base_time = max(created_at, updated_at)
        age_hours = (now - base_time) / 3600
        # 时间衰减:每过一个 TTL 周期乘以 decay_rate
        time_score = max(0.0, self.decay_rate ** (age_hours / self.ttl_hours))
        # 访问频率加分:log1p(hit_count) 增长缓慢,上限 0.2
        access_bonus = min(0.2, math.log1p(hit_count) * 0.05)
        score = time_score + access_bonus
        return min(1.0, max(0.0, score))

    def should_evict(
        self,
        created_at: float,
        updated_at: float,
        hit_count: int = 0,
    ) -> bool:
        """判断记忆是否应该被清除。"""
        score = self.calculate_score(created_at, updated_at, hit_count)
        return score < self.min_score

    def cleanup(
        self,
        memory_store,
        scope: Optional[str] = None,
        dry_run: bool = False,
    ) -> dict:
        """执行记忆清理,返回统计信息。"""
        now = time.time()
        if now - self._last_cleanup < self.check_interval:
            return {"skipped": True, "reason": "距上次清理不足间隔"}
        self._last_cleanup = now

        memories = memory_store.list(limit=1000, scope=scope)
        total = len(memories)
        to_evict = []
        kept = []

        for m in memories:
            created_at = m.get("created_at", 0) or 0
            updated_at = m.get("updated_at", 0) or 0
            hit_count = m.get("hit_count", 0) or 0
            if self.should_evict(created_at, updated_at, hit_count):
                to_evict.append(m["id"])
            else:
                kept.append(m["id"])

        evicted = 0
        if to_evict and not dry_run:
            for mid in to_evict:
                memory_store.delete(mid)
                evicted += 1

        return {
            "total": total,
            "kept": len(kept),
            "evicted": evicted,
            "dry_run": dry_run,
        }


class MemoryCluster:
    """简单记忆聚类:按关键词+向量相似度分组。"""

    @staticmethod
    def find_similar(
        memory_store,
        content: str,
        keywords: list[str],
        threshold: float = 0.7,
        max_results: int = 3,
    ) -> list[dict]:
        """查找与给定内容相似的记忆,用于合并前的查重。"""
        if memory_store is None:
            return []
        # 先关键词匹配
        keyword_matches = []
        for m in memory_store.list(limit=100):
            m_kw = [kw.lower() for kw in (m.get("keywords") or [])]
            query_kw = set(k.lower() for k in keywords)
            if set(m_kw) & query_kw:  # 有交集
                keyword_matches.append(m)

        # 向量相似度排序
        from app.rag.embeddings import get_embedder, run_embed
        from app.rag.store import cosine_similarity

        embedder = get_embedder()
        query_vec = run_embed(embedder, [content])[0]

        scored = []
        for m in keyword_matches or memory_store.list(limit=100):
            vec_str = m.get("vector")
            if not vec_str:
                continue
            try:
                vec = json.loads(vec_str) if isinstance(vec_str, str) else vec_str
                sim = cosine_similarity(query_vec, vec)
                if sim >= threshold:
                    scored.append({"memory": m, "similarity": round(sim, 4)})
            except Exception:  # noqa: BLE001
                continue

        scored.sort(key=lambda x: x["similarity"], reverse=True)
        return scored[:max_results]

    @staticmethod
    def merge_similar(
        memory_store,
        base_id: str,
        similar_ids: list[str],
    ) -> dict:
        """将相似记忆合并为基础记忆的增强版本。"""
        base = memory_store.get(base_id)
        if not base:
            return {"error": "基础记忆不存在"}
        merged_content = base["content"]
        merged_keywords = list(set(base.get("keywords") or []))
        for sid in similar_ids:
            other = memory_store.get(sid)
            if other:
                merged_content += "\n\n---\n" + other["content"]
                merged_keywords.extend(other.get("keywords") or [])
                memory_store.delete(sid)

        memory_store.update(base_id, content=merged_content, keywords=merged_keywords)
        return {
            "merged_base": base_id,
            "merged_into": similar_ids,
            "total_keywords": len(merged_keywords),
        }


def run_decay_cleanup(memory_store, scope: Optional[str] = None, dry_run: bool = False) -> dict:
    """执行一次完整的记忆衰退清理。"""
    decay = MemoryDecay(
        ttl_hours=settings.ltm_ttl_hours,
        decay_rate=settings.ltm_decay_rate,
        min_score=settings.ltm_min_score,
    )
    return decay.cleanup(memory_store, scope=scope, dry_run=dry_run)


# ---- 增强:语义去重 + TTL 自动驱逐(保存时触发) ----

def _hash_content(content: str) -> str:
    """内容指纹。"""
    import hashlib
    return hashlib.sha256(content.strip().encode("utf-8")).hexdigest()[:16]


def auto_dedup_and_evict(
    memory_store,
    content: str,
    keywords: Optional[list[str]] = None,
    scope: str = "global",
    session_id: Optional[str] = None,
    similarity_threshold: float = 0.85,
) -> dict:
    """保存前自动去重:若已有高度相似记忆则合并,否则保存新记忆。

    返回: {"action": "merged|saved|evicted", "memory_id": str, "details": dict}
    """
    import json as _json
    import time as _time
    from app.rag.embeddings import get_embedder, run_embed
    from app.rag.store import cosine_similarity as _cosine

    result = {"action": "saved", "memory_id": "", "details": {}}

    # 1. TTL 驱逐:先清理即将过期的旧记忆
    try:
        decay = MemoryDecay(
            ttl_hours=settings.ltm_ttl_hours,
            decay_rate=settings.ltm_decay_rate,
            min_score=settings.ltm_min_score,
        )
        # 检查是否存在即将过期的记忆
        all_memories = memory_store.list(limit=500, scope=scope)
        to_evict = []
        for m in all_memories:
            if decay.should_evict(m.get("created_at", 0), m.get("updated_at", 0), m.get("hit_count", 0)):
                to_evict.append(m["id"])
        if to_evict:
            for mid in to_evict:
                memory_store.delete(mid)
            result["details"]["evicted"] = len(to_evict)
    except Exception:  # noqa: BLE001
        pass

    # 2. 语义去重:检查是否有高度相似的记忆
    try:
        embedder = get_embedder()
        new_vec = run_embed(embedder, [content])[0]
        new_hash = _hash_content(content)

        # 搜索相似记忆
        similar = []
        for m in memory_store.list(limit=200, scope=scope):
            vec_str = m.get("vector")
            if not vec_str:
                continue
            try:
                existing_vec = _json.loads(vec_str) if isinstance(vec_str, str) else vec_str
                sim = _cosine(new_vec, existing_vec)
                if sim >= similarity_threshold:
                    similar.append({"memory": m, "similarity": round(sim, 4)})
            except Exception:  # noqa: BLE001
                continue

        if similar:
            # 找到最相似的,合并
            best = max(similar, key=lambda x: x["similarity"])
            existing = best["memory"]
            merged_content = existing["content"]
            merged_keywords = list(set((existing.get("keywords") or []) + (keywords or [])))
            # 追加新内容
            if new_hash != _hash_content(existing["content"]):
                merged_content += "\n\n---\n" + content
            memory_store.update(existing["id"], content=merged_content, keywords=merged_keywords)
            result["action"] = "merged"
            result["memory_id"] = existing["id"]
            result["details"] = {
                "merged_with": existing["id"],
                "similarity": best["similarity"],
                "new_content_appended": new_hash != _hash_content(existing["content"]),
            }
            return result
    except Exception:  # noqa: BLE001
        pass

    # 3. 正常保存(由调用方负责调用 memory_store.save)
    result["action"] = "saved"
    return result
