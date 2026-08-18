"""用户反馈闭环:like/dislike → 记忆权重调整 → 画像更新

核心能力:
1. 记录用户反馈（like/dislike/neutral）
2. 基于反馈调整记忆权重（高权重=更可能被检索注入）
3. 更新用户画像（偏好、风格、领域）
4. 反馈驱动的 Few-Shot 选择（优先注入高评分轨迹）
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Optional

from app.config import settings
from app.core.storage_backend import StorageBackend, get_storage_backend

logger = logging.getLogger(__name__)


class FeedbackType(str, Enum):
    """反馈类型"""
    LIKE = "like"
    DISLIKE = "dislike"
    NEUTRAL = "neutral"


@dataclass
class UserFeedback:
    """用户反馈记录"""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    session_id: str = ""
    user_id: str = ""
    message_id: str = ""  # 关联的消息ID
    feedback_type: FeedbackType = FeedbackType.NEUTRAL
    reason: str = ""  # 用户反馈原因
    tags: list = field(default_factory=list)  # 标签（如：准确、详细、简洁）
    score: float = 0.0  # 评分（-1 到 1）
    trajectory_id: str = ""  # 关联的轨迹ID
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "message_id": self.message_id,
            "feedback_type": self.feedback_type.value,
            "reason": self.reason,
            "tags": self.tags,
            "score": self.score,
            "trajectory_id": self.trajectory_id,
            "created_at": self.created_at,
        }


@dataclass
class UserProfile:
    """用户画像"""
    user_id: str = ""
    preferences: dict = field(default_factory=dict)  # 偏好（语言、风格、领域）
    feedback_history: list = field(default_factory=list)  # 反馈历史
    avg_score: float = 0.0  # 平均评分
    total_feedback: int = 0  # 总反馈数
    like_count: int = 0
    dislike_count: int = 0
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "preferences": self.preferences,
            "avg_score": round(self.avg_score, 3),
            "total_feedback": self.total_feedback,
            "like_count": self.like_count,
            "dislike_count": self.dislike_count,
            "updated_at": self.updated_at,
        }


class FeedbackManager:
    """用户反馈闭环管理器"""

    def __init__(self, db_path: Optional[str] = None, backend: Optional[StorageBackend] = None):
        """初始化 FeedbackManager
        
        Args:
            db_path: 数据库路径（SQLite 模式）
            backend: 存储后端实例，优先使用
        """
        if backend:
            self._backend = backend
            self._db_path = None
        else:
            self._db_path = db_path or os.path.join(settings.resolved_data_dir, "feedback.db")
            self._backend = get_storage_backend(db_path=self._db_path)
        
        self._lock = threading.RLock()
        self._feedbacks: dict[str, UserFeedback] = {}  # id -> feedback
        self._profiles: dict[str, UserProfile] = {}  # user_id -> profile
        self._memory_weights: dict[str, float] = {}  # memory_id -> weight
        self._init_db()
        self._load()

    def _init_db(self) -> None:
        """初始化数据库表"""
        schema = {
            "feedbacks": [
                {"name": "id", "type": "TEXT", "constraints": "PRIMARY KEY"},
                {"name": "session_id", "type": "TEXT", "constraints": "NOT NULL"},
                {"name": "user_id", "type": "TEXT", "constraints": "NOT NULL"},
                {"name": "message_id", "type": "TEXT", "constraints": ""},
                {"name": "feedback_type", "type": "TEXT", "constraints": "NOT NULL"},
                {"name": "reason", "type": "TEXT", "constraints": ""},
                {"name": "tags", "type": "TEXT", "constraints": "DEFAULT '[]'"},
                {"name": "score", "type": "REAL", "constraints": "DEFAULT 0.0"},
                {"name": "trajectory_id", "type": "TEXT", "constraints": ""},
                {"name": "created_at", "type": "REAL", "constraints": "NOT NULL"},
            ],
            "user_profiles": [
                {"name": "user_id", "type": "TEXT", "constraints": "PRIMARY KEY"},
                {"name": "preferences", "type": "TEXT", "constraints": "DEFAULT '{}'"},
                {"name": "feedback_history", "type": "TEXT", "constraints": "DEFAULT '[]'"},
                {"name": "avg_score", "type": "REAL", "constraints": "DEFAULT 0.0"},
                {"name": "total_feedback", "type": "INTEGER", "constraints": "DEFAULT 0"},
                {"name": "like_count", "type": "INTEGER", "constraints": "DEFAULT 0"},
                {"name": "dislike_count", "type": "INTEGER", "constraints": "DEFAULT 0"},
                {"name": "updated_at", "type": "REAL", "constraints": "NOT NULL"},
            ],
        }
        with self._lock:
            self._backend.init_db(schema)

    def _load(self) -> None:
        """从数据库加载数据"""
        with self._lock:
            # 加载反馈
            rows = self._backend.select("feedbacks", "1 = 1", order_by="created_at DESC")
            for row in rows:
                feedback = UserFeedback(
                    id=row["id"],
                    session_id=row["session_id"],
                    user_id=row["user_id"],
                    message_id=row["message_id"],
                    feedback_type=FeedbackType(row["feedback_type"]),
                    reason=row["reason"],
                    tags=json.loads(row["tags"] or "[]"),
                    score=row["score"],
                    trajectory_id=row.get("trajectory_id", ""),
                    created_at=row["created_at"],
                )
                self._feedbacks[feedback.id] = feedback

            # 加载用户画像
            rows = self._backend.select("user_profiles", "1 = 1")
            for row in rows:
                profile = UserProfile(
                    user_id=row["user_id"],
                    preferences=json.loads(row["preferences"] or "{}"),
                    feedback_history=json.loads(row["feedback_history"] or "[]"),
                    avg_score=row["avg_score"],
                    total_feedback=row["total_feedback"],
                    like_count=row["like_count"],
                    dislike_count=row["dislike_count"],
                    updated_at=row["updated_at"],
                )
                self._profiles[profile.user_id] = profile

    def record_feedback(
        self,
        session_id: str,
        user_id: str,
        message_id: str,
        feedback_type: FeedbackType,
        reason: str = "",
        tags: Optional[list[str]] = None,
        trajectory_id: str = "",
    ) -> UserFeedback:
        """记录用户反馈"""
        # 计算分数
        score_map = {
            FeedbackType.LIKE: 1.0,
            FeedbackType.DISLIKE: -1.0,
            FeedbackType.NEUTRAL: 0.0,
        }
        score = score_map.get(feedback_type, 0.0)

        feedback = UserFeedback(
            session_id=session_id,
            user_id=user_id,
            message_id=message_id,
            feedback_type=feedback_type,
            reason=reason,
            tags=tags or [],
            score=score,
            trajectory_id=trajectory_id,
        )

        # 保存到数据库
        with self._lock:
            self._backend.insert(
                "feedbacks",
                {
                    "id": feedback.id,
                    "session_id": feedback.session_id,
                    "user_id": feedback.user_id,
                    "message_id": feedback.message_id,
                    "feedback_type": feedback.feedback_type.value,
                    "reason": feedback.reason,
                    "tags": json.dumps(feedback.tags, ensure_ascii=False),
                    "score": feedback.score,
                    "trajectory_id": feedback.trajectory_id,
                    "created_at": feedback.created_at,
                }
            )

        self._feedbacks[feedback.id] = feedback

        # 更新用户画像
        self._update_profile(user_id, feedback)

        # 更新记忆权重
        self._update_memory_weights(session_id, feedback)

        # 更新轨迹评分
        if trajectory_id:
            try:
                from app.core.fewshot import get_few_shot_builder
                builder = get_few_shot_builder()
                builder.update_feedback(trajectory_id, score)
            except Exception:
                pass

        logger.info(f"记录反馈: user={user_id}, type={feedback_type.value}, score={score}")
        return feedback

    def _update_profile(self, user_id: str, feedback: UserFeedback) -> None:
        """更新用户画像"""
        if user_id not in self._profiles:
            self._profiles[user_id] = UserProfile(user_id=user_id)

        profile = self._profiles[user_id]
        profile.total_feedback += 1
        profile.feedback_history.append({
            "feedback_id": feedback.id,
            "score": feedback.score,
            "tags": feedback.tags,
            "created_at": feedback.created_at,
        })

        # 只保留最近 100 条反馈
        if len(profile.feedback_history) > 100:
            profile.feedback_history = profile.feedback_history[-100:]

        # 更新计数
        if feedback.feedback_type == FeedbackType.LIKE:
            profile.like_count += 1
        elif feedback.feedback_type == FeedbackType.DISLIKE:
            profile.dislike_count += 1

        # 更新平均分
        scores = [f["score"] for f in profile.feedback_history]
        profile.avg_score = sum(scores) / len(scores) if scores else 0.0

        # 从标签中提取偏好
        for tag in feedback.tags:
            tag_lower = tag.lower()
            if tag_lower not in profile.preferences:
                profile.preferences[tag_lower] = 0.0
            # 反馈驱动的偏好权重调整
            profile.preferences[tag_lower] += feedback.score * 0.1
            profile.preferences[tag_lower] = max(-1.0, min(1.0, profile.preferences[tag_lower]))

        profile.updated_at = time.time()

        # 保存到数据库
        with self._lock:
            self._backend.upsert(
                "user_profiles",
                {
                    "user_id": profile.user_id,
                    "preferences": json.dumps(profile.preferences, ensure_ascii=False),
                    "feedback_history": json.dumps(profile.feedback_history, ensure_ascii=False),
                    "avg_score": profile.avg_score,
                    "total_feedback": profile.total_feedback,
                    "like_count": profile.like_count,
                    "dislike_count": profile.dislike_count,
                    "updated_at": profile.updated_at,
                },
                conflict_columns=["user_id"]
            )

    def _update_memory_weights(self, session_id: str, feedback: UserFeedback) -> None:
        """基于反馈调整记忆权重"""
        try:
            from app.memory.ltm import get_memory_store
            store = get_memory_store()

            # 获取当前会话的记忆
            memories = store.list(limit=50, scope="session")
            for mem in memories:
                if mem["session_id"] == session_id:
                    # 调整权重
                    current_weight = self._memory_weights.get(mem["id"], 1.0)
                    # like 增加权重，dislike 降低权重
                    adjustment = feedback.score * 0.2
                    new_weight = max(0.1, min(2.0, current_weight + adjustment))
                    self._memory_weights[mem["id"]] = new_weight
        except Exception as e:
            logger.warning(f"更新记忆权重失败: {e}")

    def get_memory_weight(self, memory_id: str) -> float:
        """获取记忆权重"""
        return self._memory_weights.get(memory_id, 1.0)

    def get_profile(self, user_id: str) -> Optional[UserProfile]:
        """获取用户画像"""
        return self._profiles.get(user_id)

    def get_feedback_stats(self, user_id: str) -> dict:
        """获取反馈统计"""
        profile = self._profiles.get(user_id)
        if not profile:
            return {"total": 0, "like": 0, "dislike": 0, "avg_score": 0.0}
        return {
            "total": profile.total_feedback,
            "like": profile.like_count,
            "dislike": profile.dislike_count,
            "avg_score": round(profile.avg_score, 3),
        }

    def get_preferred_tags(self, user_id: str, top_k: int = 5) -> list[str]:
        """获取用户偏好的标签"""
        profile = self._profiles.get(user_id)
        if not profile:
            return []

        # 按偏好权重排序
        sorted_prefs = sorted(
            profile.preferences.items(),
            key=lambda x: x[1],
            reverse=True,
        )
        return [tag for tag, _ in sorted_prefs[:top_k]]

    def build_feedback_hint(self, user_id: str, max_tokens: int = 200) -> str:
        """构建反馈提示（注入到 system prompt）"""
        from app.memory.context import estimate_tokens

        profile = self._profiles.get(user_id)
        if not profile or profile.total_feedback < 3:
            return ""

        lines = []
        total_tokens = 0

        # 偏好提示
        preferred = self.get_preferred_tags(user_id, top_k=3)
        if preferred:
            hint = f"[用户偏好] {', '.join(preferred)}"
            tokens = estimate_tokens(hint)
            if total_tokens + tokens <= max_tokens:
                lines.append(hint)
                total_tokens += tokens

        # 评分提示
        if profile.avg_score > 0.5:
            hint = "[用户满意度] 高"
        elif profile.avg_score < -0.3:
            hint = "[用户满意度] 低，请更谨慎回答"
        else:
            hint = "[用户满意度] 中等"

        tokens = estimate_tokens(hint)
        if total_tokens + tokens <= max_tokens:
            lines.append(hint)
            total_tokens += tokens

        return "\n".join(lines) if lines else ""

    def get_recent_feedbacks(
        self,
        user_id: str,
        limit: int = 10,
    ) -> list[UserFeedback]:
        """获取用户最近的反馈"""
        feedbacks = [
            f for f in self._feedbacks.values()
            if f.user_id == user_id
        ]
        feedbacks.sort(key=lambda f: f.created_at, reverse=True)
        return feedbacks[:limit]

    def get_similar_feedbacks(
        self,
        user_id: str,
        tags: Optional[list[str]] = None,
        limit: int = 5,
    ) -> list[UserFeedback]:
        """获取相似反馈（用于 Few-Shot 选择）"""
        if not tags:
            return []

        # 按标签匹配度和分数排序
        scored = []
        for feedback in self._feedbacks.values():
            if feedback.user_id != user_id:
                continue
            # 计算标签匹配度
            overlap = len(set(tags) & set(feedback.tags))
            if overlap > 0:
                scored.append((overlap * feedback.score, feedback))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [f for _, f in scored[:limit]]


# 向后兼容别名
Feedback闭环 = FeedbackManager


# 单例
_feedback_manager: Optional[FeedbackManager] = None


def get_feedback_manager() -> FeedbackManager:
    """获取反馈管理器单例"""
    global _feedback_manager
    if _feedback_manager is None:
        _feedback_manager = FeedbackManager()
    return _feedback_manager


# 向后兼容别名
def get_feedback闭环() -> FeedbackManager:
    """获取反馈管理器单例（兼容旧代码）"""
    return get_feedback_manager()
