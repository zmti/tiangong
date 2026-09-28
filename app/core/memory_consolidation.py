"""持久化记忆固化:每轮对话后提取关键决策/代码片段存入 SQLite + 向量索引。

实现跨会话的知识积累:架构决策、代码模式、错误修复、最佳实践。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Optional

from app.config import settings
from app.core.storage_backend import StorageBackend, get_storage_backend

logger = logging.getLogger(__name__)


@dataclass
class MemoryEntry:
    """记忆条目。"""
    id: str = ""
    session_id: str = ""
    user_id: str = "default"
    content: str = ""
    memory_type: str = "fact"  # fact / decision / code_pattern / error_fix / best_practice / preference
    summary: str = ""
    keywords: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)  # 场景标签: architecture / debugging / refactoring / testing
    importance: float = 1.0  # 0-1, 越高越重要
    source_turn: int = 0  # 对话轮次
    created_at: float = 0.0
    updated_at: float = 0.0
    access_count: int = 0
    last_accessed: float = 0.0
    vector: Optional[list[float]] = field(default=None, repr=False)


class MemoryConsolidator:
    """记忆固化器:从对话历史提取结构化记忆并持久化。"""

    # 记忆类型关键词映射
    TYPE_KEYWORDS = {
        "decision": ["决定", "选择", "采用", "方案", "架构", "选型", "decision", "choose", "adopt", "architecture"],
        "code_pattern": ["模式", "范式", "写法", "封装", "抽象", "pattern", "idiom", "wrapper", "abstraction"],
        "error_fix": ["修复", "解决", "报错", "错误", "异常", "bug", "fix", "solve", "error", "exception", "bug"],
        "best_practice": ["最佳", "推荐", "规范", "约定", "标准", "best practice", "recommend", "convention", "standard"],
        "preference": ["偏好", "习惯", "喜欢", "倾向", "prefer", "like", "habit"],
        "fact": ["是", "等于", "定义", "概念", "原理", "机制", "fact", "define", "concept", "principle"],
    }

    def __init__(self, db_path: Optional[str] = None, backend: Optional[StorageBackend] = None):
        if backend:
            self._backend = backend
            self._db_path = None
        else:
            self._db_path = db_path or os.path.join(settings.resolved_data_dir, "memory_consolidation.db")
            self._backend = get_storage_backend(db_path=self._db_path)
        self._lock = threading.RLock()
        self._init_db()
        self._embedder = None
        self._embedder_loaded = False

    def _init_db(self) -> None:
        schema = {
            "memory_entries": [
                {"name": "id", "type": "TEXT", "constraints": "PRIMARY KEY"},
                {"name": "session_id", "type": "TEXT", "constraints": "NOT NULL"},
                {"name": "user_id", "type": "TEXT", "constraints": "NOT NULL DEFAULT 'default'"},
                {"name": "content", "type": "TEXT", "constraints": "NOT NULL"},
                {"name": "summary", "type": "TEXT", "constraints": ""},
                {"name": "memory_type", "type": "TEXT", "constraints": "NOT NULL DEFAULT 'fact'"},
                {"name": "keywords", "type": "TEXT", "constraints": ""},
                {"name": "tags", "type": "TEXT", "constraints": ""},
                {"name": "importance", "type": "REAL", "constraints": "NOT NULL DEFAULT 1.0"},
                {"name": "source_turn", "type": "INTEGER", "constraints": "DEFAULT 0"},
                {"name": "created_at", "type": "REAL", "constraints": "NOT NULL"},
                {"name": "updated_at", "type": "REAL", "constraints": "NOT NULL"},
                {"name": "access_count", "type": "INTEGER", "constraints": "DEFAULT 0"},
                {"name": "last_accessed", "type": "REAL", "constraints": "DEFAULT 0"},
                {"name": "vector", "type": "BLOB", "constraints": ""},  # 向量二进制存储
            ],
        }
        self._backend.init_db(schema)
        self._backend.execute("CREATE INDEX IF NOT EXISTS idx_mem_session ON memory_entries(session_id)")
        self._backend.execute("CREATE INDEX IF NOT EXISTS idx_mem_user ON memory_entries(user_id)")
        self._backend.execute("CREATE INDEX IF NOT EXISTS idx_mem_type ON memory_entries(memory_type)")
        self._backend.execute("CREATE INDEX IF NOT EXISTS idx_mem_importance ON memory_entries(importance DESC)")

    def _get_embedder(self):
        """惰性加载嵌入模型。"""
        if self._embedder_loaded:
            return self._embedder
        try:
            from app.rag.embeddings import get_embedder
            self._embedder = get_embedder()
            self._embedder_loaded = True
        except Exception as e:
            logger.warning("嵌入模型加载失败,向量搜索不可用: %s", e)
            self._embedder = None
            self._embedder_loaded = True
        return self._embedder

    def _embed(self, text: str) -> Optional[list[float]]:
        """生成文本向量。"""
        embedder = self._get_embedder()
        if embedder is None:
            return None
        try:
            from app.rag.embeddings import run_embed
            vec = run_embed(embedder, [text])
            return vec[0] if vec else None
        except Exception as e:
            logger.debug("向量生成失败: %s", e)
            return None

    def _classify_memory_type(self, content: str) -> str:
        """基于关键词分类记忆类型。"""
        content_lower = content.lower()
        scores = {}
        for mtype, keywords in self.TYPE_KEYWORDS.items():
            score = sum(1 for kw in keywords if kw in content_lower)
            if score > 0:
                scores[mtype] = score
        if scores:
            return max(scores, key=scores.get)
        return "fact"

    def _extract_keywords(self, content: str, max_k: int = 10) -> list[str]:
        """简单关键词提取(基于词频+技术词典)。"""
        # 技术词典
        tech_terms = {
            "python", "fastapi", "django", "flask", "asyncio", "pytest", "ruff", "mypy",
            "sql", "postgres", "redis", "docker", "kubernetes", "aws", "gcp",
            "react", "vue", "typescript", "javascript", "html", "css",
            "api", "rest", "graphql", "grpc", "websocket",
            "design pattern", "singleton", "factory", "observer", "strategy",
            "clean code", "solid", "dry", "kiss", "yagni",
            "ci/cd", "github actions", "gitlab ci", "jenkins",
        }
        words = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*|[\u4e00-\u9fff]+", content.lower())
        freq = {}
        for w in words:
            if len(w) >= 2:
                freq[w] = freq.get(w, 0) + 1
        # 优先技术术语
        scored = [(freq[w] + (10 if w in tech_terms else 0), w) for w in freq]
        scored.sort(reverse=True)
        return [w for _, w in scored[:max_k]]

    def _extract_tags(self, content: str) -> list[str]:
        """提取场景标签。"""
        tags = []
        content_lower = content.lower()
        tag_map = {
            "architecture": ["架构", "设计", "模块", "分层", "微服务", "architecture", "design", "module"],
            "debugging": ["调试", "排查", "定位", "日志", "断点", "debug", "troubleshoot", "log"],
            "refactoring": ["重构", "优化", "简化", "提取", "重命名", "refactor", "optimize", "extract"],
            "testing": ["测试", "单测", "集成测", "覆盖率", "mock", "test", "pytest", "coverage"],
            "security": ["安全", "权限", "认证", "授权", "加密", "security", "auth", "encrypt"],
            "performance": ["性能", "优化", "慢查询", "缓存", "并发", "performance", "cache", "concurrent"],
        }
        for tag, keywords in tag_map.items():
            if any(kw in content_lower for kw in keywords):
                tags.append(tag)
        return tags

    def _calculate_importance(self, content: str, memory_type: str, turn: int) -> float:
        """计算重要性分数。"""
        base = 0.5
        # 类型权重
        type_weights = {
            "decision": 0.9,
            "code_pattern": 0.8,
            "error_fix": 0.85,
            "best_practice": 0.75,
            "preference": 0.7,
            "fact": 0.5,
        }
        base = type_weights.get(memory_type, 0.5)
        # 长度加成(有一定内容的更有价值)
        length_bonus = min(len(content) / 2000, 0.2)
        # 近期加成(越新越相关)
        recency_bonus = max(0.1 - turn * 0.01, 0)
        return min(base + length_bonus + recency_bonus, 1.0)

    async def consolidate(self, session_id: str, history: list[dict], user_id: str = "default") -> list[MemoryEntry]:
        """从对话历史固化记忆。"""
        if not history:
            return []

        # 只处理最近 N 轮(避免重复处理旧历史)
        recent_turns = getattr(settings, "agent_memory_consolidation_turns", 6)
        # history 格式: [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}, ...]
        # 一轮 = user + assistant
        pairs = []
        for i in range(0, len(history) - 1, 2):
            if i + 1 < len(history):
                pairs.append((history[i], history[i + 1]))
        pairs = pairs[-recent_turns:]

        entries = []
        for turn_idx, (user_msg, assistant_msg) in enumerate(pairs):
            user_content = user_msg.get("content", "") or ""
            assistant_content = assistant_msg.get("content", "") or ""
            combined = f"用户: {user_content}\n助手: {assistant_content}"

            # 跳过太短的内容
            if len(combined.strip()) < 50:
                continue

            # 简单规则:如果助手回复包含代码块或关键决策词,提取
            if self._worth_remembering(assistant_content):
                entry = self._create_entry(
                    session_id=session_id,
                    user_id=user_id,
                    content=combined,
                    turn=turn_idx,
                )
                entries.append(entry)

        if entries:
            await self._save_entries(entries)
            logger.info("固化 %d 条记忆 (session=%s)", len(entries), session_id[:8])

        return entries

    def _worth_remembering(self, content: str) -> bool:
        """判断内容是否值得记忆。"""
        if not content or len(content.strip()) < 30:
            return False
        # 包含代码块
        if "```" in content:
            return True
        # 包含决策/修复/最佳实践关键词
        content_lower = content.lower()
        important_keywords = [
            "决定", "选择", "采用", "方案", "架构", "选型",
            "修复", "解决", "报错", "错误", "异常", "bug",
            "最佳", "推荐", "规范", "约定", "标准",
            "模式", "范式", "写法", "封装", "抽象",
            "decision", "choose", "adopt", "architecture",
            "fix", "solve", "error", "bug", "exception",
            "best practice", "recommend", "convention",
            "pattern", "idiom", "wrapper", "abstraction",
        ]
        return any(kw in content_lower for kw in important_keywords)

    def _create_entry(self, session_id: str, user_id: str, content: str, turn: int) -> MemoryEntry:
        memory_type = self._classify_memory_type(content)
        keywords = self._extract_keywords(content)
        tags = self._extract_tags(content)
        importance = self._calculate_importance(content, memory_type, turn)
        # 生成摘要(取前 200 字)
        summary = content[:200].replace("\n", " ") + ("..." if len(content) > 200 else "")
        vector = self._embed(content)

        now = time.time()
        return MemoryEntry(
            id=uuid.uuid4().hex,
            session_id=session_id,
            user_id=user_id,
            content=content,
            memory_type=memory_type,
            summary=summary,
            keywords=keywords,
            tags=tags,
            importance=importance,
            source_turn=turn,
            created_at=now,
            updated_at=now,
            vector=vector,
        )

    async def _save_entries(self, entries: list[MemoryEntry]) -> None:
        """批量保存记忆条目。"""

        def _save_sync():
            with self._lock:
                for entry in entries:
                    vector_blob = None
                    if entry.vector:
                        import struct
                        vector_blob = struct.pack(f"{len(entry.vector)}f", *entry.vector)
                    self._backend.upsert(
                        "memory_entries",
                        {
                            "id": entry.id,
                            "session_id": entry.session_id,
                            "user_id": entry.user_id,
                            "content": entry.content,
                            "summary": entry.summary,
                            "memory_type": entry.memory_type,
                            "keywords": json.dumps(entry.keywords, ensure_ascii=False),
                            "tags": json.dumps(entry.tags, ensure_ascii=False),
                            "importance": entry.importance,
                            "source_turn": entry.source_turn,
                            "created_at": entry.created_at,
                            "updated_at": entry.updated_at,
                            "access_count": entry.access_count,
                            "last_accessed": entry.last_accessed,
                            "vector": vector_blob,
                        },
                        conflict_columns=["id"],
                    )

        await asyncio.to_thread(_save_sync)

    def recall(
        self,
        query: str,
        user_id: str = "default",
        memory_types: Optional[list[str]] = None,
        tags: Optional[list[str]] = None,
        top_k: int = 5,
        min_importance: float = 0.3,
    ) -> list[MemoryEntry]:
        """检索相关记忆(向量+关键词混合)。"""
        conditions = ["user_id = ?"]
        params: list = [user_id]
        if memory_types:
            placeholders = ",".join("?" * len(memory_types))
            conditions.append(f"memory_type IN ({placeholders})")
            params.extend(memory_types)
        if tags:
            # tags 是 JSON 数组,用 LIKE 模糊匹配
            for tag in tags:
                conditions.append("tags LIKE ?")
                params.append(f"%{tag}%")

        with self._lock:
            rows = self._backend.select(
                "memory_entries",
                " AND ".join(conditions),
                tuple(params),
                order_by="importance DESC, access_count DESC",
                limit=200,
            )

        mems = []
        for row in rows:
            vector = None
            if row["vector"]:
                import struct
                vec_len = len(row["vector"]) // 4
                vector = list(struct.unpack(f"{vec_len}f", row["vector"]))
            mems.append(MemoryEntry(
                id=row["id"],
                session_id=row["session_id"],
                user_id=row["user_id"],
                content=row["content"],
                memory_type=row["memory_type"],
                summary=row["summary"],
                keywords=json.loads(row["keywords"] or "[]"),
                tags=json.loads(row["tags"] or "[]"),
                importance=row["importance"],
                source_turn=row["source_turn"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                access_count=row["access_count"],
                last_accessed=row["last_accessed"],
                vector=vector,
            ))

        if not mems:
            return []

        # 向量相似度重排
        q_vec = self._embed(query)
        if q_vec:
            from app.rag.store import cosine_similarity
            scored = []
            for mem in mems:
                score = mem.importance * 0.3
                if mem.vector:
                    cos = cosine_similarity(q_vec, mem.vector)
                    score += cos * 0.7
                scored.append((score, mem))
            scored.sort(key=lambda x: x[0], reverse=True)
            mems = [mem for score, mem in scored if score > min_importance]

        # 更新访问统计
        for mem in mems[:top_k]:
            self._update_access(mem.id)

        return mems[:top_k]

    def _update_access(self, mem_id: str) -> None:
        """更新访问计数。"""
        with self._lock:
            self._backend.execute(
                "UPDATE memory_entries SET access_count = access_count + 1, last_accessed = ? WHERE id = ?",
                (time.time(), mem_id),
            )

    def build_memory_note(self, entries: list[MemoryEntry], max_chars: int = 2000) -> str:
        """构建注入到系统提示的记忆块。"""
        if not entries:
            return ""
        lines = ["## 跨会话记忆(自动固化)"]
        for mem in entries:
            tag_str = f" [{', '.join(mem.tags)}]" if mem.tags else ""
            lines.append(f"- [{mem.memory_type}]{tag_str} {mem.summary}")
        note = "\n".join(lines)
        return note[:max_chars]


# 全局单例
_consolidator: Optional[MemoryConsolidator] = None


def get_consolidator(db_path: Optional[str] = None) -> MemoryConsolidator:
    global _consolidator
    if _consolidator is None:
        _consolidator = MemoryConsolidator(db_path)
    return _consolidator


async def consolidate_memory(session_id: str, history: list[dict], user_id: str = "default") -> list[MemoryEntry]:
    """便捷函数:固化对话记忆。"""
    consolidator = get_consolidator()
    return await consolidator.consolidate(session_id, history, user_id)


def recall_memory(
    query: str,
    user_id: str = "default",
    memory_types: Optional[list[str]] = None,
    tags: Optional[list[str]] = None,
    top_k: int = 5,
) -> list[MemoryEntry]:
    """便捷函数:检索记忆。"""
    consolidator = get_consolidator()
    return consolidator.recall(query, user_id, memory_types, tags, top_k)