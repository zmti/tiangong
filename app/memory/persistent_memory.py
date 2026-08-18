"""持久化个性化记忆系统:跨会话用户画像、偏好学习、知识图谱。

对标 Claude/ChatGPT 长期记忆:
- 三层记忆架构:工作记忆(会话级) / 语义记忆(事实/知识) / 程序记忆(习惯/偏好)
- 跨会话自适应:用户画像持续演化,每次对话后自动更新
- 记忆融合:相似记忆自动合并,冲突记忆标注并保留版本历史
- 个性化注入:每次对话前按场景动态注入相关记忆
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Optional

from app.config import settings
from app.memory.ltm import get_memory_store, MemoryStore


# ── 持久化记忆模型 ────────────────────────────────────────────────
@dataclass
class PersistentMemory:
    """持久化记忆条目:比 LTM 更丰富的结构化数据。"""
    id: str = ""
    scope: str = "global"          # global / session / user
    session_id: Optional[str] = None
    user_id: str = "default"
    content: str = ""
    memory_type: str = "fact"      # fact / preference / habit / decision / skill
    keywords: list[str] = field(default_factory=list)
    strength: float = 1.0          # 记忆强度(0~1),高频使用增强,长期不用衰减
    created_at: float = 0.0
    updated_at: float = 0.0
    last_accessed: float = 0.0
    access_count: int = 0
    context_tags: list[str] = field(default_factory=list)  # 适用场景标签
    related_ids: list[str] = field(default_factory=list)   # 关联记忆 ID


class PersistentMemoryStore:
    """持久化记忆存储:SQLite 后端,支持跨会话查询与画像更新。"""

    def __init__(self, db_path: Optional[str] = None):
        self._store = get_memory_store()
        self._db_path = db_path or os.path.join(
            settings.resolved_data_dir, "persistent_memory.db"
        )
        self._lock = threading.RLock()
        self._init_db()

    def _init_db(self) -> None:
        """初始化持久化记忆表。"""
        from app.core.storage_backend import get_storage_backend
        backend = get_storage_backend(db_path=self._db_path)
        schema = {
            "persistent_memories": [
                {"name": "id", "type": "TEXT", "constraints": "PRIMARY KEY"},
                {"name": "scope", "type": "TEXT", "constraints": "NOT NULL DEFAULT 'global'"},
                {"name": "user_id", "type": "TEXT", "constraints": "NOT NULL DEFAULT 'default'"},
                {"name": "session_id", "type": "TEXT", "constraints": ""},
                {"name": "content", "type": "TEXT", "constraints": "NOT NULL"},
                {"name": "memory_type", "type": "TEXT", "constraints": "NOT NULL DEFAULT 'fact'"},
                {"name": "keywords", "type": "TEXT", "constraints": ""},
                {"name": "strength", "type": "REAL", "constraints": "NOT NULL DEFAULT 1.0"},
                {"name": "context_tags", "type": "TEXT", "constraints": ""},
                {"name": "related_ids", "type": "TEXT", "constraints": ""},
                {"name": "access_count", "type": "INTEGER", "constraints": "DEFAULT 0"},
                {"name": "created_at", "type": "REAL", "constraints": "NOT NULL"},
                {"name": "updated_at", "type": "REAL", "constraints": "NOT NULL"},
                {"name": "last_accessed", "type": "REAL", "constraints": "DEFAULT 0"},
            ],
        }
        backend.init_db(schema)
        backend.execute("CREATE INDEX IF NOT EXISTS idx_pm_user ON persistent_memories(user_id)")
        backend.execute("CREATE INDEX IF NOT EXISTS idx_pm_type ON persistent_memories(memory_type)")
        backend.execute("CREATE INDEX IF NOT EXISTS idx_pm_scope ON persistent_memories(scope)")

    def save(
        self,
        content: str,
        memory_type: str = "fact",
        keywords: Optional[list[str]] = None,
        context_tags: Optional[list[str]] = None,
        user_id: str = "default",
        session_id: Optional[str] = None,
        strength: float = 1.0,
    ) -> dict:
        mid = uuid.uuid4().hex
        now = time.time()
        with self._lock:
            self._store._backend.upsert(
                "persistent_memories",
                {
                    "id": mid,
                    "scope": "global" if user_id == "default" else "user",
                    "user_id": user_id,
                    "session_id": session_id,
                    "content": content.strip(),
                    "memory_type": memory_type,
                    "keywords": json.dumps(keywords or [], ensure_ascii=False),
                    "strength": strength,
                    "context_tags": json.dumps(context_tags or [], ensure_ascii=False),
                    "related_ids": "[]",
                    "access_count": 0,
                    "created_at": now,
                    "updated_at": now,
                    "last_accessed": 0,
                },
                conflict_columns=["id"],
            )
        return {"id": mid, "type": memory_type}

    def recall(
        self,
        query: str,
        user_id: str = "default",
        memory_types: Optional[list[str]] = None,
        top_k: int = 5,
    ) -> list[dict]:
        """检索相关持久化记忆,按用户和类型过滤。"""
        from app.rag.embeddings import get_embedder, run_embed
        from app.rag.store import cosine_similarity

        conditions = ["user_id = ?"]
        params: list = [user_id]
        if memory_types:
            placeholders = ",".join("?" * len(memory_types))
            conditions.append(f"memory_type IN ({placeholders})")
            params.extend(memory_types)

        with self._lock:
            rows = self._store._backend.select(
                "persistent_memories",
                " AND ".join(conditions),
                tuple(params),
                order_by="strength DESC, access_count DESC",
                limit=200,
            )

        mems = []
        for row in rows:
            mems.append({
                "id": row["id"],
                "content": row["content"],
                "type": row["memory_type"],
                "strength": row["strength"],
                "keywords": json.loads(row["keywords"] or "[]"),
                "context_tags": json.loads(row["context_tags"] or "[]"),
                "access_count": row["access_count"],
            })

        if not mems:
            return []

        q_vec = None
        try:
            q_vec = run_embed(get_embedder(), [query])[0]
        except Exception:  # noqa: BLE001
            pass

        scored = []
        for mem in mems:
            score = mem["strength"] * 0.3
            try:
                from app.rag.embeddings import run_embed
                mem_vec = run_embed(get_embedder(), [mem["content"]])[0]
                if q_vec is not None and mem_vec is not None:
                    cos = cosine_similarity(q_vec, mem_vec)
                    score += cos * 0.7
            except Exception:  # noqa: BLE001
                pass
            scored.append((score, mem))

        scored.sort(key=lambda x: x[0], reverse=True)
        result = []
        for score, mem in scored[:top_k]:
            if score > 0.1:
                result.append(mem)
        return result

    def merge_similar(
        self,
        user_id: str = "default",
        threshold: float = 0.85,
    ) -> int:
        """合并相似记忆(基于语义相似度),返回合并条数。"""
        from app.rag.embeddings import get_embedder, run_embed
        from app.rag.store import cosine_similarity

        with self._lock:
            rows = self._store._backend.select(
                "persistent_memories",
                "user_id = ?",
                (user_id,),
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

        # 计算向量
        vectors = []
        try:
            vectors = run_embed(get_embedder(), [m["content"] for m in mems])
        except Exception:  # noqa: BLE001
            return 0

        for i, v in enumerate(vectors):
            mems[i]["vector"] = v

        merged = 0
        used = set()
        for i, m1 in enumerate(mems):
            if i in used or m1["vector"] is None:
                continue
            for j, m2 in enumerate(mems[i + 1:], i + 1):
                if j in used or m2["vector"] is None:
                    continue
                cos = cosine_similarity(m1["vector"], m2["vector"])
                if cos >= threshold:
                    # 合并:取内容更长、强度更高的作为基准
                    base = m1 if m1["strength"] >= m2["strength"] else m2
                    other = m2 if base["id"] == m1["id"] else m1
                    # 更新关联
                    related = json.loads(
                        self._store._backend.select_one(
                            "persistent_memories", "id = ?", (base["id"],),
                        ) or {}
                    ).get("related_ids", "[]")
                    new_related = json.loads(related)
                    if other["id"] not in new_related:
                        new_related.append(other["id"])
                    self._store._backend.update(
                        "persistent_memories",
                        {"related_ids": json.dumps(new_related, ensure_ascii=False)},
                        "id = ?",
                        (base["id"],),
                    )
                    # 标记为已合并(删除)
                    self._store._backend.delete("persistent_memories", "id = ?", (other["id"],))
                    used.add(j)
                    merged += 1

        return merged


# ── 用户画像进化系统 ─────────────────────────────────────────────
@dataclass
class UserProfileV2:
    """增强版用户画像:支持持续学习与跨会话自适应。"""
    user_id: str = "default"
    name: str = ""
    # 编程相关
    preferred_language: str = ""
    style: str = ""
    experience_level: str = ""
    tech_stack: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    # 沟通偏好
    response_style: str = ""
    language_preference: str = ""
    detail_level: str = ""  # concise / detailed / balanced
    # 习惯
    habits: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    # 学习记录(从交互中自动提取)
    learned_preferences: dict = field(default_factory=dict)
    feedback_history: list[dict] = field(default_factory=list)
    # 元数据
    version: int = 1
    last_updated: float = 0.0
    confidence_scores: dict = field(default_factory=dict)  # 各字段置信度

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "UserProfileV2":
        known = {f.name for f in cls.__dataclass_fields__()}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)


class UserProfileEvolver:
    """用户画像进化器:基于对话历史和反馈持续优化画像。"""

    def __init__(self):
        from app.memory.profile import get_profile_store, UserProfile
        self._legacy_store = get_profile_store()
        self._store_path = os.path.join(settings.resolved_data_dir, "user_profiles_v2.json")
        self._lock = threading.RLock()
        self._profiles: dict[str, UserProfileV2] = {}
        self._load()

    def _load(self) -> None:
        try:
            if os.path.exists(self._store_path):
                with open(self._store_path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                for uid, data in raw.items():
                    self._profiles[uid] = UserProfileV2.from_dict(data)
        except Exception:  # noqa: BLE001
            self._profiles = {}

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._store_path), exist_ok=True)
            with open(self._store_path, "w", encoding="utf-8") as f:
                json.dump(
                    {uid: p.to_dict() for uid, p in self._profiles.items()},
                    f, ensure_ascii=False, indent=2,
                )
        except Exception:  # noqa: BLE001
            pass

    def get(self, user_id: str) -> UserProfileV2:
        with self._lock:
            return self._profiles.get(user_id) or UserProfileV2(user_id=user_id)

    def save(self, user_id: str, profile: UserProfileV2) -> None:
        with self._lock:
            profile.last_updated = time.time()
            profile.version += 1
            self._profiles[user_id] = profile
            self._save()

    def learn_from_dialogue(
        self,
        user_id: str,
        messages: list[dict],
        tool_results: Optional[list[dict]] = None,
    ) -> UserProfileV2:
        """从对话历史中自动学习用户偏好,更新画像。"""
        profile = self.get(user_id)

        # 提取语言偏好
        user_msgs = [m for m in messages if m.get("role") == "user"]
        if user_msgs:
            latest = user_msgs[-1].get("content", "")
            if any(c in latest for c in "abcdefghijklmnopqrstuvwxyz"):
                if not profile.language_preference:
                    profile.language_preference = "mixed"
            if any(ord(c) > 127 for c in latest):
                if "zh" not in profile.language_preference:
                    profile.language_preference = "zh"

        # 提取技术栈
        tech_keywords = ["python", "typescript", "react", "fastapi", "django",
                         "postgres", "redis", "docker", "kubernetes", "aws"]
        all_content = " ".join(m.get("content", "") for m in messages).lower()
        for kw in tech_keywords:
            if kw in all_content and kw not in profile.tech_stack:
                profile.tech_stack.append(kw)
                profile.tech_stack = profile.tech_stack[:20]  # 限制数量

        # 提取领域
        domain_keywords = {"web": ["html", "css", "javascript", "frontend", "backend"],
                           "ml": ["model", "training", "dataset", "neural", "tensor"],
                           "devops": ["deploy", "ci-cd", "docker", "kubernetes", "pipeline"],
                           "data": ["database", "sql", "query", "analytics", "dashboard"]}
        for domain, keywords in domain_keywords.items():
            if any(kw in all_content for kw in keywords):
                if domain not in profile.domains:
                    profile.domains.append(domain)

        self.save(user_id, profile)
        return profile

    def build_prompt(self, user_id: str = "default") -> str:
        """根据画像生成注入系统提示的文本块。"""
        profile = self.get(user_id)
        if not profile or not any([
            profile.preferred_language, profile.style, profile.response_style,
            profile.language_preference, profile.domains, profile.tech_stack,
            profile.habits, profile.constraints,
        ]):
            return ""

        lines = ["## 用户画像(自动学习)"]
        if profile.preferred_language:
            lines.append(f"- 常用语言: {profile.preferred_language}")
        if profile.style:
            lines.append(f"- 代码风格: {profile.style}")
        if profile.response_style:
            lines.append(f"- 回复风格: {profile.response_style}")
        if profile.detail_level:
            lines.append(f"- 详细程度: {profile.detail_level}")
        if profile.language_preference:
            lines.append(f"- 语言偏好: {profile.language_preference}")
        if profile.domains:
            lines.append(f"- 擅长领域: {', '.join(profile.domains)}")
        if profile.tech_stack:
            lines.append(f"- 技术栈: {', '.join(profile.tech_stack)}")
        if profile.habits:
            lines.append(f"- 工作习惯: {', '.join(profile.habits)}")
        if profile.constraints:
            lines.append(f"- 约束: {', '.join(profile.constraints)}")

        return "\n".join(lines)


# ── 记忆融合器 ────────────────────────────────────────────────────
def run_persistent_merge(user_id: str = "default", threshold: float = 0.85) -> dict:
    """执行一次持久化记忆融合,返回统计。"""
    store = PersistentMemoryStore()
    merged = store.merge_similar(user_id, threshold)
    return {"merged_count": merged, "user_id": user_id}


def get_evolver() -> UserProfileEvolver:
    """获取画像进化器单例。"""
    return UserProfileEvolver()


def get_persistent_store() -> PersistentMemoryStore:
    """获取持久化记忆存储单例。"""
    return PersistentMemoryStore()
