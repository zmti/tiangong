"""记忆生命周期管理:TTL/衰减/冲突解决/版本演化/重要性分层。

核心能力:
1. TTL 过期清理: 基于时间/访问频次的自动过期
2. 重要性衰减: 遗忘曲线模型,长期未访问自动降权
3. 冲突检测与解决: 语义相似度检测冲突记忆,自动合并/标记/版本化
4. 版本演化: 记忆修改保留历史版本,支持回滚/对比
5. 分层存储: Working(热)/Semantic(温)/Episodic(冷)三层,自动分层迁移
6. 重要性分级: Critical/High/Medium/Low 四级,差异化保留策略
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from app.config import settings
from app.core.storage_backend import StorageBackend, get_storage_backend

logger = logging.getLogger(__name__)


# ============================================================
# 枚举与数据模型
# ============================================================

class MemoryTier(str, Enum):
    """记忆存储层级。"""
    WORKING = "working"      # 热数据:高频访问,毫秒级读取,全内存/SSD
    SEMANTIC = "semantic"    # 温数据:中频访问,秒级读取,SQLite+向量索引
    EPISODIC = "episodic"    # 冷数据:低频访问,分钟级读取,归档存储/对象存储


class MemoryImportance(str, Enum):
    """重要性等级。"""
    CRITICAL = "critical"    # 核心架构决策、安全策略、用户核心偏好 - 永久保留
    HIGH = "high"            # 重要代码模式、关键错误修复、最佳实践 - 长期保留
    MEDIUM = "medium"        # 一般事实、偏好、上下文片段 - 中期保留
    LOW = "low"              # 临时上下文、闲聊片段 - 短期保留


class ConflictResolution(str, Enum):
    """冲突解决策略。"""
    MERGE = "merge"              # 语义合并,保留共同点
    KEEP_LATEST = "keep_latest"  # 保留最新版本
    KEEP_HIGHEST = "keep_highest" # 保留重要性最高版本
    VERSION = "version"          # 保留所有版本,标记冲突
    MANUAL = "manual"            # 标记需人工处理


@dataclass
class MemoryVersion:
    """记忆版本。"""
    version: int
    content: str
    summary: str
    keywords: list[str]
    importance: MemoryImportance
    tier: MemoryTier
    created_at: float
    created_by: str  # user / agent / system / merge
    changelog: str = ""
    parent_version: int = 0


@dataclass
class MemoryEntry:
    """记忆条目(增强版)。"""
    id: str
    user_id: str = "default"
    session_id: str = ""
    
    # 核心内容
    content: str = ""
    summary: str = ""
    keywords: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    
    # 分类
    memory_type: str = "fact"  # fact / preference / decision / pattern / fix / context
    importance: MemoryImportance = MemoryImportance.MEDIUM
    tier: MemoryTier = MemoryTier.SEMANTIC
    
    # 生命周期
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    expires_at: float = 0  # 0 = 永不过期
    last_accessed: float = 0
    access_count: int = 0
    
    # 版本控制
    current_version: int = 1
    versions: list[MemoryVersion] = field(default_factory=list)
    
    # 关联与冲突
    related_ids: list[str] = field(default_factory=list)
    conflict_ids: list[str] = field(default_factory=list)
    conflict_resolution: ConflictResolution = ConflictResolution.VERSION
    
    # 向量(用于语义检索)
    vector: Optional[list[float]] = field(default=None, repr=False)
    
    # 元数据
    metadata: dict = field(default_factory=dict)
    
    def to_dict(self) -> dict:
        d = asdict(self)
        d["importance"] = self.importance.value
        d["tier"] = self.tier.value
        d["conflict_resolution"] = self.conflict_resolution.value
        d["versions"] = [asdict(v) for v in self.versions]
        d["vector"] = None  # 向量单独存储
        return d
    
    @classmethod
    def from_dict(cls, data: dict) -> "MemoryEntry":
        # 处理枚举
        if "importance" in data and isinstance(data["importance"], str):
            data["importance"] = MemoryImportance(data["importance"])
        if "tier" in data and isinstance(data["tier"], str):
            data["tier"] = MemoryTier(data["tier"])
        if "conflict_resolution" in data and isinstance(data["conflict_resolution"], str):
            data["conflict_resolution"] = ConflictResolution(data["conflict_resolution"])
        # 处理版本
        versions = []
        for v in data.get("versions", []):
            v["importance"] = MemoryImportance(v["importance"])
            v["tier"] = MemoryTier(v["tier"])
            versions.append(MemoryVersion(**v))
        data["versions"] = versions
        # 移除向量(单独加载)
        data.pop("vector", None)
        return cls(**data)
    
    def add_version(self, content: str, summary: str, keywords: list[str], importance: MemoryImportance, tier: MemoryTier, created_by: str, changelog: str = "") -> MemoryVersion:
        """添加新版本。"""
        version = MemoryVersion(
            version=self.current_version + 1,
            content=content,
            summary=summary,
            keywords=keywords,
            importance=importance,
            tier=tier,
            created_at=time.time(),
            created_by=created_by,
            changelog=changelog,
            parent_version=self.current_version,
        )
        self.versions.append(version)
        self.current_version = version.version
        self.content = content
        self.summary = summary
        self.keywords = keywords
        self.importance = importance
        self.tier = tier
        self.updated_at = time.time()
        return version
    
    def calculate_decay_score(self, now: float = None) -> float:
        """计算衰减后的重要性分数(0-1)。
        
        使用遗忘曲线模型: score = importance_base * exp(-lambda * days_since_access)
        """
        now = now or time.time()
        if self.last_accessed == 0:
            days_since_access = (now - self.created_at) / 86400
        else:
            days_since_access = (now - self.last_accessed) / 86400
        
        # 基础重要性权重
        base_weights = {
            MemoryImportance.CRITICAL: 1.0,
            MemoryImportance.HIGH: 0.8,
            MemoryImportance.MEDIUM: 0.5,
            MemoryImportance.LOW: 0.2,
        }
        base = base_weights.get(self.importance, 0.5)
        
        # 访问频次加成
        access_bonus = min(math.log1p(self.access_count) * 0.1, 0.3)
        
        # 时间衰减(lambda 根据重要性调整)
        lambda_decay = {
            MemoryImportance.CRITICAL: 0.001,  # 极慢衰减
            MemoryImportance.HIGH: 0.01,
            MemoryImportance.MEDIUM: 0.05,
            MemoryImportance.LOW: 0.1,
        }.get(self.importance, 0.05)
        
        decay = math.exp(-lambda_decay * days_since_access)
        score = (base + access_bonus) * decay
        return max(0.0, min(1.0, score))


# ============================================================
# 生命周期管理器
# ============================================================

class MemoryLifecycleManager:
    """记忆生命周期管理器。"""
    
    # 分层 TTL 配置(天)
    TIER_TTL_DAYS = {
        MemoryTier.WORKING: 7,      # 工作记忆:7天未访问降级
        MemoryTier.SEMANTIC: 90,    # 语义记忆:90天未访问降级
        MemoryTier.EPISODIC: 365,   # 情节记忆:1年未访问归档
    }
    
    # 重要性最低保留天数
    IMPORTANCE_MIN_RETENTION_DAYS = {
        MemoryImportance.CRITICAL: 3650,  # 10年
        MemoryImportance.HIGH: 1095,      # 3年
        MemoryImportance.MEDIUM: 180,     # 6个月
        MemoryImportance.LOW: 30,         # 1个月
    }
    
    # 衰减阈值:低于此分数自动降级/归档
    DECAY_THRESHOLDS = {
        MemoryTier.WORKING: 0.3,    # 低于0.3降级到 SEMANTIC
        MemoryTier.SEMANTIC: 0.15,  # 低于0.15降级到 EPISODIC
        MemoryTier.EPISODIC: 0.05,  # 低于0.05标记归档
    }
    
    # 冲突检测相似度阈值
    CONFLICT_SIMILARITY_THRESHOLD = 0.85
    
    def __init__(
        self,
        db_path: Optional[str] = None,
        backend: Optional[StorageBackend] = None,
        consolidation_manager: Optional[Any] = None,
    ):
        if backend:
            self._backend = backend
            self._db_path = None
        else:
            self._db_path = db_path or os.path.join(settings.resolved_data_dir, "memory_lifecycle.db")
            self._backend = get_storage_backend(db_path=self._db_path)
        self._lock = threading.RLock()
        self._consolidation = consolidation_manager
        self._init_db()
        self._embedder = None
        self._embedder_loaded = False
        self._running = False
        self._cleanup_task: Optional[threading.Thread] = None
    
    def _init_db(self) -> None:
        schema = {
            "memory_entries": [
                {"name": "id", "type": "TEXT", "constraints": "PRIMARY KEY"},
                {"name": "user_id", "type": "TEXT", "constraints": "NOT NULL DEFAULT 'default'"},
                {"name": "session_id", "type": "TEXT", "constraints": ""},
                {"name": "content", "type": "TEXT", "constraints": "NOT NULL"},
                {"name": "summary", "type": "TEXT", "constraints": ""},
                {"name": "keywords", "type": "TEXT", "constraints": ""},
                {"name": "tags", "type": "TEXT", "constraints": ""},
                {"name": "memory_type", "type": "TEXT", "constraints": "NOT NULL DEFAULT 'fact'"},
                {"name": "importance", "type": "TEXT", "constraints": "NOT NULL DEFAULT 'medium'"},
                {"name": "tier", "type": "TEXT", "constraints": "NOT NULL DEFAULT 'semantic'"},
                {"name": "created_at", "type": "REAL", "constraints": "NOT NULL"},
                {"name": "updated_at", "type": "REAL", "constraints": "NOT NULL"},
                {"name": "expires_at", "type": "REAL", "constraints": "DEFAULT 0"},
                {"name": "last_accessed", "type": "REAL", "constraints": "DEFAULT 0"},
                {"name": "access_count", "type": "INTEGER", "constraints": "DEFAULT 0"},
                {"name": "current_version", "type": "INTEGER", "constraints": "DEFAULT 1"},
                {"name": "versions_json", "type": "TEXT", "constraints": "DEFAULT '[]'"},
                {"name": "related_ids", "type": "TEXT", "constraints": "DEFAULT '[]'"},
                {"name": "conflict_ids", "type": "TEXT", "constraints": "DEFAULT '[]'"},
                {"name": "conflict_resolution", "type": "TEXT", "constraints": "DEFAULT 'version'"},
                {"name": "vector", "type": "BLOB", "constraints": ""},
                {"name": "metadata", "type": "TEXT", "constraints": "DEFAULT '{}'"},
            ],
            "memory_conflicts": [
                {"name": "id", "type": "TEXT", "constraints": "PRIMARY KEY"},
                {"name": "memory_id_1", "type": "TEXT", "constraints": "NOT NULL"},
                {"name": "memory_id_2", "type": "TEXT", "constraints": "NOT NULL"},
                {"name": "similarity", "type": "REAL", "constraints": "NOT NULL"},
                {"name": "status", "type": "TEXT", "constraints": "DEFAULT 'detected'"},  # detected / resolving / resolved
                {"name": "resolution", "type": "TEXT", "constraints": "DEFAULT ''"},
                {"name": "created_at", "type": "REAL", "constraints": "NOT NULL"},
                {"name": "resolved_at", "type": "REAL", "constraints": "DEFAULT 0"},
            ],
        }
        self._backend.init_db(schema)
        self._backend.execute("CREATE INDEX IF NOT EXISTS idx_mem_user ON memory_entries(user_id)")
        self._backend.execute("CREATE INDEX IF NOT EXISTS idx_mem_tier ON memory_entries(tier)")
        self._backend.execute("CREATE INDEX IF NOT EXISTS idx_mem_importance ON memory_entries(importance)")
        self._backend.execute("CREATE INDEX IF NOT EXISTS idx_mem_expires ON memory_entries(expires_at)")
        self._backend.execute("CREATE INDEX IF NOT EXISTS idx_mem_accessed ON memory_entries(last_accessed)")
        self._backend.execute("CREATE INDEX IF NOT EXISTS idx_conflict_mem1 ON memory_conflicts(memory_id_1)")
        self._backend.execute("CREATE INDEX IF NOT EXISTS idx_conflict_mem2 ON memory_conflicts(memory_id_2)")
    
    def _get_embedder(self):
        if self._embedder_loaded:
            return self._embedder
        try:
            from app.rag.embeddings import get_embedder
            self._embedder = get_embedder()
            self._embedder_loaded = True
        except Exception as e:
            logger.warning("嵌入模型加载失败,冲突检测降级: %s", e)
            self._embedder = None
            self._embedder_loaded = True
        return self._embedder
    
    def _embed(self, text: str) -> Optional[list[float]]:
        embedder = self._get_embedder()
        if embedder is None:
            return None
        try:
            from app.rag.embeddings import run_embed
            vec = run_embed(embedder, [text])
            return vec[0] if vec else None
        except Exception:
            return None
    
    def _vector_to_blob(self, vec: list[float]) -> bytes:
        import struct
        return struct.pack(f"{len(vec)}f", *vec)
    
    def _blob_to_vector(self, blob: bytes) -> list[float]:
        import struct
        return list(struct.unpack(f"{len(blob)//4}f", blob))
    
    # ---- CRUD ----
    
    def create(self, entry: MemoryEntry) -> MemoryEntry:
        """创建记忆条目。"""
        # 计算过期时间
        if entry.expires_at == 0:
            min_days = self.IMPORTANCE_MIN_RETENTION_DAYS.get(entry.importance, 180)
            entry.expires_at = time.time() + min_days * 86400
        
        # 生成向量
        entry.vector = self._embed(entry.content)
        
        with self._lock:
            vector_blob = self._vector_to_blob(entry.vector) if entry.vector else b""
            self._backend.insert(
                "memory_entries",
                {
                    "id": entry.id,
                    "user_id": entry.user_id,
                    "session_id": entry.session_id,
                    "content": entry.content,
                    "summary": entry.summary,
                    "keywords": json.dumps(entry.keywords, ensure_ascii=False),
                    "tags": json.dumps(entry.tags, ensure_ascii=False),
                    "memory_type": entry.memory_type,
                    "importance": entry.importance.value,
                    "tier": entry.tier.value,
                    "created_at": entry.created_at,
                    "updated_at": entry.updated_at,
                    "expires_at": entry.expires_at,
                    "last_accessed": entry.last_accessed,
                    "access_count": entry.access_count,
                    "current_version": entry.current_version,
                    "versions_json": json.dumps([asdict(v) for v in entry.versions], ensure_ascii=False),
                    "related_ids": json.dumps(entry.related_ids, ensure_ascii=False),
                    "conflict_ids": json.dumps(entry.conflict_ids, ensure_ascii=False),
                    "conflict_resolution": entry.conflict_resolution.value,
                    "vector": vector_blob,
                    "metadata": json.dumps(entry.metadata, ensure_ascii=False),
                },
            )
        return entry
    
    def get(self, memory_id: str) -> Optional[MemoryEntry]:
        """获取记忆条目。"""
        with self._lock:
            row = self._backend.select_one("memory_entries", "id = ?", (memory_id,))
        if not row:
            return None
        return self._row_to_entry(row)
    
    def update(self, entry: MemoryEntry) -> bool:
        """更新记忆条目。"""
        entry.updated_at = time.time()
        vector_blob = self._vector_to_blob(entry.vector) if entry.vector else b""
        
        with self._lock:
            result = self._backend.update(
                "memory_entries",
                {
                    "content": entry.content,
                    "summary": entry.summary,
                    "keywords": json.dumps(entry.keywords, ensure_ascii=False),
                    "tags": json.dumps(entry.tags, ensure_ascii=False),
                    "memory_type": entry.memory_type,
                    "importance": entry.importance.value,
                    "tier": entry.tier.value,
                    "updated_at": entry.updated_at,
                    "expires_at": entry.expires_at,
                    "last_accessed": entry.last_accessed,
                    "access_count": entry.access_count,
                    "current_version": entry.current_version,
                    "versions_json": json.dumps([asdict(v) for v in entry.versions], ensure_ascii=False),
                    "related_ids": json.dumps(entry.related_ids, ensure_ascii=False),
                    "conflict_ids": json.dumps(entry.conflict_ids, ensure_ascii=False),
                    "conflict_resolution": entry.conflict_resolution.value,
                    "vector": vector_blob,
                    "metadata": json.dumps(entry.metadata, ensure_ascii=False),
                },
                "id = ?",
                (entry.id,),
            )
        return result > 0
    
    def delete(self, memory_id: str) -> bool:
        with self._lock:
            result = self._backend.delete("memory_entries", "id = ?", (memory_id,))
        return result > 0
    
    def _row_to_entry(self, row: dict) -> MemoryEntry:
        vector = None
        if row["vector"]:
            vector = self._blob_to_vector(row["vector"])
        
        versions = []
        for v in json.loads(row["versions_json"] or "[]"):
            v["importance"] = MemoryImportance(v["importance"])
            v["tier"] = MemoryTier(v["tier"])
            versions.append(MemoryVersion(**v))
        
        return MemoryEntry(
            id=row["id"],
            user_id=row["user_id"],
            session_id=row["session_id"],
            content=row["content"],
            summary=row["summary"],
            keywords=json.loads(row["keywords"] or "[]"),
            tags=json.loads(row["tags"] or "[]"),
            memory_type=row["memory_type"],
            importance=MemoryImportance(row["importance"]),
            tier=MemoryTier(row["tier"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            expires_at=row["expires_at"],
            last_accessed=row["last_accessed"],
            access_count=row["access_count"],
            current_version=row["current_version"],
            versions=versions,
            related_ids=json.loads(row["related_ids"] or "[]"),
            conflict_ids=json.loads(row["conflict_ids"] or "[]"),
            conflict_resolution=ConflictResolution(row["conflict_resolution"]),
            vector=vector,
            metadata=json.loads(row["metadata"] or "{}"),
        )
    
    # ---- 生命周期操作 ----
    
    def access(self, memory_id: str) -> Optional[MemoryEntry]:
        """访问记忆:更新访问时间/计数,检查是否需要分层迁移。"""
        entry = self.get(memory_id)
        if not entry:
            return None
        
        now = time.time()
        entry.last_accessed = now
        entry.access_count += 1
        
        # 检查分层迁移
        self._check_tier_migration(entry)
        
        self.update(entry)
        return entry
    
    def _check_tier_migration(self, entry: MemoryEntry) -> None:
        """检查并执行分层迁移。"""
        decay_score = entry.calculate_decay_score()
        threshold = self.DECAY_THRESHOLDS.get(entry.tier, 0.1)
        
        if decay_score < threshold:
            # 降级
            if entry.tier == MemoryTier.WORKING:
                entry.tier = MemoryTier.SEMANTIC
                logger.info("记忆 %s 降级: WORKING -> SEMANTIC (decay=%.3f)", entry.id[:8], decay_score)
            elif entry.tier == MemoryTier.SEMANTIC:
                entry.tier = MemoryTier.EPISODIC
                logger.info("记忆 %s 降级: SEMANTIC -> EPISODIC (decay=%.3f)", entry.id[:8], decay_score)
            elif entry.tier == MemoryTier.EPISODIC:
                # 标记归档
                entry.metadata["archived"] = True
                logger.info("记忆 %s 标记归档: EPISODIC (decay=%.3f)", entry.id[:8], decay_score)
    
    def check_expirations(self) -> list[str]:
        """检查过期记忆,返回过期ID列表。"""
        now = time.time()
        with self._lock:
            rows = self._backend.select(
                "memory_entries",
                "expires_at > 0 AND expires_at < ?",
                (now,),
            )
        expired_ids = [row["id"] for row in rows]
        for eid in expired_ids:
            logger.info("记忆过期: %s", eid[:8])
        return expired_ids
    
    def cleanup_expired(self, hard_delete: bool = False) -> int:
        """清理过期记忆。"""
        expired_ids = self.check_expirations()
        if not expired_ids:
            return 0
        
        deleted = 0
        for eid in expired_ids:
            entry = self.get(eid)
            if entry and entry.importance == MemoryImportance.CRITICAL:
                # CRITICAL 不删除,仅延长过期时间
                entry.expires_at = time.time() + 365 * 86400
                self.update(entry)
                continue
            
            if hard_delete:
                self.delete(eid)
            else:
                # 软删除:标记归档
                entry.metadata["archived"] = True
                entry.metadata["archived_at"] = time.time()
                self.update(entry)
            deleted += 1
        
        return deleted
    
    # ---- 冲突检测与解决 ----
    
    def detect_conflicts(self, user_id: str = "default", threshold: float = None) -> list[tuple[str, str, float]]:
        """检测冲突记忆对。
        
        Returns:
            List of (memory_id_1, memory_id_2, similarity)
        """
        threshold = threshold or self.CONFLICT_SIMILARITY_THRESHOLD
        
        with self._lock:
            rows = self._backend.select(
                "memory_entries",
                "user_id = ?",
                (user_id,),
                limit=500,
            )
        
        entries = []
        for row in rows:
            if row["vector"]:
                vec = self._blob_to_vector(row["vector"])
                entries.append({"id": row["id"], "vector": vec, "content": row["content"]})
        
        conflicts = []
        from app.rag.store import cosine_similarity
        
        for i, e1 in enumerate(entries):
            if not e1["vector"]:
                continue
            for e2 in entries[i+1:]:
                if not e2["vector"]:
                    continue
                sim = cosine_similarity(e1["vector"], e2["vector"])
                if sim >= threshold:
                    conflicts.append((e1["id"], e2["id"], sim))
        
        # 记录冲突
        for mem1, mem2, sim in conflicts:
            self._record_conflict(mem1, mem2, sim)
        
        return conflicts
    
    def _record_conflict(self, mem1: str, mem2: str, similarity: float) -> None:
        """记录冲突。"""
        conflict_id = uuid.uuid4().hex
        with self._lock:
            # 检查是否已存在
            existing = self._backend.select_one(
                "memory_conflicts",
                "(memory_id_1 = ? AND memory_id_2 = ?) OR (memory_id_1 = ? AND memory_id_2 = ?)",
                (mem1, mem2, mem2, mem1),
            )
            if existing:
                return
            
            self._backend.insert(
                "memory_conflicts",
                {
                    "id": conflict_id,
                    "memory_id_1": mem1,
                    "memory_id_2": mem2,
                    "similarity": similarity,
                    "status": "detected",
                    "created_at": time.time(),
                },
            )
        
        # 更新记忆的冲突列表
        for mem_id in [mem1, mem2]:
            entry = self.get(mem_id)
            if entry and mem2 not in entry.conflict_ids:
                entry.conflict_ids.append(mem2 if mem_id == mem1 else mem1)
                self.update(entry)
    
    def resolve_conflict(
        self,
        memory_id_1: str,
        memory_id_2: str,
        strategy: ConflictResolution = ConflictResolution.VERSION,
        merged_content: str = "",
    ) -> bool:
        """解决冲突。"""
        with self._lock:
            # 更新冲突状态
            self._backend.update(
                "memory_conflicts",
                {"status": "resolved", "resolution": strategy.value, "resolved_at": time.time()},
                "(memory_id_1 = ? AND memory_id_2 = ?) OR (memory_id_1 = ? AND memory_id_2 = ?)",
                (memory_id_1, memory_id_2, memory_id_2, memory_id_1),
            )
        
        entry1 = self.get(memory_id_1)
        entry2 = self.get(memory_id_2)
        if not entry1 or not entry2:
            return False
        
        if strategy == ConflictResolution.MERGE:
            # 语义合并:取并集
            merged = merged_content or f"{entry1.content}\n---\n{entry2.content}"
            merged_summary = f"合并自 {entry1.id[:8]} 和 {entry2.id[:8]}"
            merged_keywords = list(set(entry1.keywords + entry2.keywords))
            merged_importance = max(entry1.importance, entry2.importance, key=lambda x: ["critical", "high", "medium", "low"].index(x.value))
            
            # 更新 entry1 为合并版本,entry2 标记为已合并
            entry1.add_version(
                content=merged,
                summary=merged_summary,
                keywords=merged_keywords,
                importance=merged_importance,
                tier=entry1.tier,
                created_by="merge",
                changelog=f"与 {memory_id_2[:8]} 语义合并",
            )
            self.update(entry1)
            
            entry2.metadata["merged_into"] = memory_id_1
            entry2.metadata["merged_at"] = time.time()
            self.update(entry2)
            
        elif strategy == ConflictResolution.KEEP_LATEST:
            # 保留更新时间最新的
            keep = entry1 if entry1.updated_at > entry2.updated_at else entry2
            discard = entry2 if keep == entry1 else entry1
            discard.metadata["superseded_by"] = keep.id
            discard.metadata["superseded_at"] = time.time()
            self.update(discard)
            
        elif strategy == ConflictResolution.KEEP_HIGHEST:
            # 保留重要性最高的
            importance_order = {MemoryImportance.CRITICAL: 4, MemoryImportance.HIGH: 3, MemoryImportance.MEDIUM: 2, MemoryImportance.LOW: 1}
            keep = entry1 if importance_order[entry1.importance] >= importance_order[entry2.importance] else entry2
            discard = entry2 if keep == entry1 else entry1
            discard.metadata["superseded_by"] = keep.id
            self.update(discard)
            
        elif strategy == ConflictResolution.VERSION:
            # 双方都保留,仅标记冲突已解决
            entry1.metadata["conflict_resolved"] = True
            entry1.metadata["conflict_resolved_at"] = time.time()
            entry2.metadata["conflict_resolved"] = True
            entry2.metadata["conflict_resolved_at"] = time.time()
            self.update(entry1)
            self.update(entry2)
        
        # 从冲突列表移除
        for e in [entry1, entry2]:
            other_id = entry2.id if e == entry1 else entry1.id
            if other_id in e.conflict_ids:
                e.conflict_ids.remove(other_id)
            self.update(e)
        
        return True
    
    # ---- 版本管理 ----
    
    def get_version_history(self, memory_id: str) -> list[MemoryVersion]:
        """获取版本历史。"""
        entry = self.get(memory_id)
        return entry.versions if entry else []
    
    def rollback_version(self, memory_id: str, target_version: int) -> bool:
        """回滚到指定版本。"""
        entry = self.get(memory_id)
        if not entry:
            return False
        
        target = None
        for v in entry.versions:
            if v.version == target_version:
                target = v
                break
        
        if not target:
            return False
        
        # 创建新版本作为回滚
        entry.add_version(
            content=target.content,
            summary=target.summary,
            keywords=target.keywords,
            importance=target.importance,
            tier=target.tier,
            created_by="rollback",
            changelog=f"回滚到版本 {target_version}",
        )
        self.update(entry)
        return True
    
    # ---- 后台清理任务 ----
    
    def start_background_cleanup(self, interval_seconds: int = 3600) -> None:
        """启动后台清理任务。"""
        if self._running:
            return
        
        self._running = True
        
        def cleanup_loop():
            while self._running:
                try:
                    # 1. 清理过期
                    self.cleanup_expired()
                    
                    # 2. 检测冲突
                    self.detect_conflicts()
                    
                    # 3. 分层迁移检查(批量)
                    self._batch_tier_migration()
                    
                except Exception as e:
                    logger.error("记忆生命周期清理异常: %s", e)
                
                time.sleep(interval_seconds)
        
        self._cleanup_task = threading.Thread(target=cleanup_loop, daemon=True)
        self._cleanup_task.start()
        logger.info("记忆生命周期后台清理任务已启动,间隔 %d 秒", interval_seconds)
    
    def stop_background_cleanup(self) -> None:
        """停止后台清理任务。"""
        self._running = False
        if self._cleanup_task:
            self._cleanup_task.join(timeout=5)
        logger.info("记忆生命周期后台清理任务已停止")
    
    def _batch_tier_migration(self) -> None:
        """批量检查分层迁移。"""
        with self._lock:
            rows = self._backend.select("memory_entries", "1=1", limit=2000)
        
        for row in rows:
            entry = self._row_to_entry(row)
            self._check_tier_migration(entry)
            if entry.tier != row["tier"]:
                self.update(entry)
    
    # ---- 统计 ----
    
    def get_stats(self, user_id: str = "default") -> dict:
        """获取记忆统计。"""
        with self._lock:
            rows = self._backend.select("memory_entries", "user_id = ?", (user_id,))
        
        total = len(rows)
        by_tier = defaultdict(int)
        by_importance = defaultdict(int)
        by_type = defaultdict(int)
        expired = 0
        now = time.time()
        
        for row in rows:
            by_tier[row["tier"]] += 1
            by_importance[row["importance"]] += 1
            by_type[row["memory_type"]] += 1
            if row["expires_at"] > 0 and row["expires_at"] < now:
                expired += 1
        
        return {
            "total": total,
            "by_tier": dict(by_tier),
            "by_importance": dict(by_importance),
            "by_type": dict(by_type),
            "expired": expired,
        }


# 全局单例
_lifecycle_manager: Optional[MemoryLifecycleManager] = None


def get_lifecycle_manager(
    db_path: Optional[str] = None,
    consolidation_manager: Optional[Any] = None,
) -> MemoryLifecycleManager:
    global _lifecycle_manager
    if _lifecycle_manager is None:
        _lifecycle_manager = MemoryLifecycleManager(db_path, consolidation_manager=consolidation_manager)
    return _lifecycle_manager