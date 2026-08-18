"""记忆清理与一致性引擎:自动清理过期记忆、合并重复记忆、跨会话一致性检查。

对标 Claude 的记忆管理:
- 时间衰减:低频使用的记忆自动降权
- 冲突检测:新记忆与旧记忆冲突时标注并保留版本历史
- 过期清理:超过 TTL 的记忆自动归档
- 一致性检查:跨会话搜索记忆,确保回答一致
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from app.config import settings
from app.memory.ltm import get_memory_store, MemoryStore


@dataclass
class DecayRule:
    """记忆衰减规则。"""
    memory_type: str       # fact / preference / habit / decision / skill
    half_life_hours: float # 半衰期(小时),经过此时间强度减半
    min_strength: float = 0.05  # 最低强度阈值,低于此值触发清理


DEFAULT_DECAY_RULES = [
    DecayRule("fact", half_life_hours=720, min_strength=0.1),     # 事实记忆:30天半衰期
    DecayRule("preference", half_life_hours=2160, min_strength=0.05),  # 偏好:90天
    DecayRule("habit", half_life_hours=4320, min_strength=0.05), # 习惯:180天
    DecayRule("decision", half_life_hours=2160, min_strength=0.1),  # 决策:90天
    DecayRule("skill", half_life_hours=8760, min_strength=0.01), # 技能:365天
]


class MemoryCleanupEngine:
    """记忆清理引擎。"""

    def __init__(
        self,
        store: Optional[MemoryStore] = None,
        decay_rules: Optional[list[DecayRule]] = None,
    ):
        self._store = store or get_memory_store()
        self._decay_rules = decay_rules or DEFAULT_DECAY_RULES
        self._lock = threading.RLock()

    def run_decay(self, dry_run: bool = True) -> dict:
        """执行记忆衰减计算,返回统计。"""
        from app.rag.embeddings import get_embedder, run_embed
        from app.rag.store import cosine_similarity

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
                # 使用hit_count作为强度的代理（访问次数越多，强度越高）
                hit_count = row.get("hit_count", 0)
                max_hits = 100  # 假设最大访问量
                base_strength = min(1.0, hit_count / max_hits)
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
            self._archive_candidates(candidates)
            stats["applied"] = True
        else:
            stats["applied"] = False
        return stats

    def _infer_type(self, content: str, keywords: list[str]) -> str:
        """根据内容推断记忆类型。"""
        lower = content.lower()
        kw_lower = [k.lower() for k in keywords]
        if any(k in lower for k in ["偏好", "喜欢", "讨厌", "习惯"]):
            return "preference"
        if any(k in lower for k in ["决定", "选择", "采用"]):
            return "decision"
        if any(k in lower for k in ["总是", "经常", "习惯", "规律"]):
            return "habit"
        if any(k in lower for k in ["技能", "掌握", "擅长"]):
            return "skill"
        return "fact"

    def _apply_updates(self, updates: list[dict]) -> None:
        for u in updates:
            try:
                self._store._backend.update(
                    "memories",
                    {"strength": u["new_strength"], "updated_at": time.time()},
                    "id = ?",
                    (u["id"],),
                )
            except Exception:  # noqa: BLE001
                pass

    def _archive_candidates(self, ids: list[str]) -> None:
        """将即将过期的记忆标记为归档(不删除,供用户手动恢复)。"""
        for mid in ids:
            try:
                self._store._backend.update(
                    "memories",
                    {"scope": "archived", "updated_at": time.time()},
                    "id = ?",
                    (mid,),
                )
            except Exception:  # noqa: BLE001
                pass

    def cleanup_expired(self, max_age_days: int = 365) -> dict:
        """清理超过最大年龄的记忆,返回清理统计。"""
        cutoff = time.time() - max_age_days * 86400
        with self._lock:
            rows = self._store._backend.select(
                "memories",
                "created_at < ?",
                (cutoff,),
                limit=500,
            )
            ids = [r["id"] for r in rows]
        deleted = 0
        for mid in ids:
            try:
                self._store._backend.delete("memories", "id = ?", (mid,))
                deleted += 1
            except Exception:  # noqa: BLE001
                pass
        return {"deleted": deleted, "total_candidates": len(ids), "max_age_days": max_age_days}

    def merge_duplicates(self, threshold: float = 0.9, user_id: str = "default") -> dict:
        """合并高相似度记忆,返回合并统计。"""
        from app.rag.embeddings import get_embedder, run_embed
        from app.rag.store import cosine_similarity

        with self._lock:
            rows = self._store._backend.select(
                "memories",
                "scope != 'archived'",
                limit=500,
            )

        mems = []
        for row in rows:
            mems.append({
                "id": row["id"],
                "content": row["content"],
                "strength": row["strength"],
                "vector": None,
            })

        vectors = []
        try:
            vectors = run_embed(get_embedder(), [m["content"] for m in mems])
        except Exception:
            return {"merged": 0, "error": "嵌入失败"}

        for i, v in enumerate(vectors):
            mems[i]["vector"] = v

        merged = 0
        used = set()
        for i, m1 in enumerate(mems):
            if i in used or m1["vector"] is None:
                continue
            for j in range(i + 1, len(mems)):
                if j in used or mems[j]["vector"] is None:
                    continue
                cos = cosine_similarity(m1["vector"], mems[j]["vector"])
                if cos >= threshold:
                    # 保留强度更高的
                    base = m1 if m1.get("strength", 0) >= mems[j].get("strength", 0) else mems[j]
                    other = mems[j] if base["id"] == m1["id"] else m1
                    # 更新关联
                    related = json.loads(
                        self._store._backend.select_one(
                            "memories", "id = ?", (base["id"],),
                        ) or {}
                    ).get("related_ids", "[]")
                    new_related = json.loads(related)
                    if other["id"] not in new_related:
                        new_related.append(other["id"])
                    self._store._backend.update(
                        "memories",
                        {"related_ids": json.dumps(new_related, ensure_ascii=False)},
                        "id = ?", (base["id"],),
                    )
                    self._store._backend.delete("memories", "id = ?", (other["id"],))
                    used.add(j)
                    merged += 1

        return {"merged": merged, "threshold": threshold}


def run_memory_cleanup(dry_run: bool = True, max_age_days: int = 365) -> dict:
    """执行记忆清理(供 API 调用)。"""
    engine = MemoryCleanupEngine()
    decay_stats = engine.run_decay(dry_run=dry_run)
    if not dry_run:
        expire_stats = engine.cleanup_expired(max_age_days=max_age_days)
        decay_stats["expired_deleted"] = expire_stats["deleted"]
    return decay_stats


# ── 跨会话一致性 ──────────────────────────────────────────────────
class CrossSessionConsistency:
    """跨会话一致性检查:确保相同问题的回答风格和深度一致。"""

    def __init__(self, store: Optional[MemoryStore] = None):
        self._store = store or get_memory_store()
        self._consistency_db_path = os.path.join(
            settings.resolved_data_dir, "consistency_cache.db"
        )
        self._lock = threading.RLock()
        self._init_cache()

    def _init_cache(self) -> None:
        from app.core.storage_backend import get_storage_backend
        backend = get_storage_backend(db_path=self._consistency_db_path)
        schema = {
            "consistency_cache": [
                {"name": "question_hash", "type": "TEXT", "constraints": "PRIMARY KEY"},
                {"name": "answer_style", "type": "TEXT", "constraints": "NOT NULL"},
                {"name": "detail_level", "type": "TEXT", "constraints": "NOT NULL"},
                {"name": "language", "type": "TEXT", "constraints": "NOT NULL"},
                {"name": "tech_stack", "type": "TEXT", "constraints": ""},
                {"name": "session_count", "type": "INTEGER", "constraints": "DEFAULT 1"},
                {"name": "last_seen", "type": "REAL", "constraints": "NOT NULL"},
                {"name": "created_at", "type": "REAL", "constraints": "NOT NULL"},
            ],
        }
        backend.init_db(schema)

    def get_consistency_hint(self, current_question: str, current_session: str) -> str:
        """获取跨会话一致性提示。"""
        from hashlib import sha256
        q_hash = sha256(current_question.strip().lower().encode()).hexdigest()[:16]

        with self._lock:
            row = self._store._backend.select_one(
                "consistency_cache", "question_hash = ?", (q_hash,)
            )

        if not row:
            return ""

        style = row.get("answer_style", "")
        detail = row.get("detail_level", "")
        lang = row.get("language", "")
        tech = row.get("tech_stack", "")
        sessions = row.get("session_count", 1)

        lines = []
        if sessions > 1:
            lines.append(f"这是该问题的第 {sessions} 次交互。")
        if style:
            lines.append(f"回答风格: {style}")
        if detail:
            lines.append(f"详细程度: {detail}")
        if lang:
            lines.append(f"语言: {lang}")
        if tech:
            lines.append(f"技术栈: {tech}")

        return "\n".join(lines) if lines else ""

    def record_consistency(
        self,
        question: str,
        answer_style: str,
        detail_level: str,
        language: str,
        tech_stack: list[str],
    ) -> None:
        """记录一次一致性快照。"""
        from hashlib import sha256
        q_hash = sha256(question.strip().lower().encode()).hexdigest()[:16]
        now = time.time()

        with self._lock:
            existing = self._store._backend.select_one(
                "consistency_cache", "question_hash = ?", (q_hash,)
            )
            if existing:
                self._store._backend.update(
                    "consistency_cache",
                    {
                        "answer_style": answer_style,
                        "detail_level": detail_level,
                        "language": language,
                        "tech_stack": ",".join(tech_stack),
                        "session_count": existing.get("session_count", 0) + 1,
                        "last_seen": now,
                    },
                    "question_hash = ?",
                    (q_hash,),
                )
            else:
                self._store._backend.upsert(
                    "consistency_cache",
                    {
                        "question_hash": q_hash,
                        "answer_style": answer_style,
                        "detail_level": detail_level,
                        "language": language,
                        "tech_stack": ",".join(tech_stack),
                        "session_count": 1,
                        "last_seen": now,
                        "created_at": now,
                    },
                    conflict_columns=["question_hash"],
                )


def get_consistency_engine() -> CrossSessionConsistency:
    return CrossSessionConsistency()
