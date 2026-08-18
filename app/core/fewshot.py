"""Few-Shot 自动构建: 成功轨迹索引 + 相似任务检索 + 注入上下文

对标主流 Agent:
- SWE-Agent: 历史轨迹检索
- Astra: Few-shot 自动构建
- ToolBench: 成功经验复用

核心能力:
1. 存储成功执行轨迹（任务→工具序列→结果）
2. 基于任务相似度检索历史示例
3. 自动注入到 system prompt
4. 反馈驱动的示例选择（优先高评分轨迹）
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class ExecutionTrajectory:
    """执行轨迹"""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    task: str = ""  # 原始任务
    task_embedding: Optional[list[float]] = None  # 任务向量
    steps: list[dict] = field(default_factory=list)  # 执行步骤
    tools_used: list[str] = field(default_factory=list)  # 使用的工具
    success: bool = False  # 是否成功
    result: str = ""  # 最终结果
    user_feedback: float = 0.0  # 用户反馈评分
    tags: list[str] = field(default_factory=list)  # 标签
    duration: float = 0.0  # 执行时长
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "task": self.task,
            "steps": self.steps,
            "tools_used": self.tools_used,
            "success": self.success,
            "result": self.result[:200],  # 截断
            "user_feedback": self.user_feedback,
            "tags": self.tags,
            "duration": self.duration,
            "created_at": self.created_at,
        }


@dataclass
class FewShotExample:
    """Few-Shot 示例"""
    trajectory: ExecutionTrajectory
    relevance_score: float = 0.0  # 相似度
    feedback_score: float = 0.0  # 反馈评分
    combined_score: float = 0.0  # 综合评分


class FewShotBuilder:
    """Few-Shot 自动构建器"""

    def __init__(self, max_trajectories: int = 1000):
        self._trajectories: dict[str, ExecutionTrajectory] = {}
        self._max_trajectories = max_trajectories
        self._tag_index: dict[str, list[str]] = {}  # tag -> trajectory_ids

    def record_trajectory(
        self,
        task: str,
        steps: list[dict],
        tools_used: list[str],
        success: bool,
        result: str,
        tags: Optional[list[str]] = None,
        duration: float = 0.0,
    ) -> ExecutionTrajectory:
        """记录执行轨迹"""
        trajectory = ExecutionTrajectory(
            task=task,
            steps=steps,
            tools_used=tools_used,
            success=success,
            result=result,
            tags=tags or [],
            duration=duration,
        )

        # 计算任务向量
        try:
            from app.rag.embeddings import get_embedder, run_embed
            trajectory.task_embedding = run_embed(get_embedder(), [task])[0]
        except Exception:
            pass

        self._trajectories[trajectory.id] = trajectory

        # 更新标签索引
        for tag in trajectory.tags:
            if tag not in self._tag_index:
                self._tag_index[tag] = []
            self._tag_index[tag].append(trajectory.id)

        # 限制数量
        if len(self._trajectories) > self._max_trajectories:
            self._cleanup_old()

        logger.info(f"记录轨迹: task={task[:50]}..., success={success}")
        return trajectory

    def update_feedback(self, trajectory_id: str, feedback_score: float) -> bool:
        """更新轨迹的用户反馈"""
        if trajectory_id not in self._trajectories:
            return False
        self._trajectories[trajectory_id].user_feedback = feedback_score
        return True

    def retrieve_similar(
        self,
        query: str,
        top_k: int = 3,
        tags: Optional[list[str]] = None,
        success_only: bool = True,
    ) -> list[FewShotExample]:
        """检索相似的成功轨迹"""
        # 计算查询向量
        q_vec = None
        try:
            from app.rag.embeddings import get_embedder, run_embed
            q_vec = run_embed(get_embedder(), [query])[0]
        except Exception:
            pass

        # 候选轨迹
        candidates = list(self._trajectories.values())

        # 过滤
        if success_only:
            candidates = [t for t in candidates if t.success]

        if tags:
            # 标签匹配
            tag_ids = set()
            for tag in tags:
                tag_ids.update(self._tag_index.get(tag, []))
            candidates = [t for t in candidates if t.id in tag_ids]

        # 计算相似度
        scored = []
        for traj in candidates:
            score = 0.0

            # 向量相似度
            if q_vec is not None and traj.task_embedding is not None:
                from app.rag.store import cosine_similarity
                cos = cosine_similarity(q_vec, traj.task_embedding)
                score += cos * 0.6

            # 反馈评分
            score += traj.user_feedback * 0.2

            # 标签匹配
            if tags:
                tag_overlap = len(set(tags) & set(traj.tags))
                score += tag_overlap * 0.1

            # 工具匹配（如果查询中提到工具）
            query_lower = query.lower()
            tool_match = sum(1 for t in traj.tools_used if t.lower() in query_lower)
            score += tool_match * 0.1

            if score > 0:
                scored.append((score, traj))

        # 排序
        scored.sort(key=lambda x: x[0], reverse=True)

        # 构建示例
        examples = []
        for score, traj in scored[:top_k]:
            examples.append(FewShotExample(
                trajectory=traj,
                relevance_score=score,
                feedback_score=traj.user_feedback,
                combined_score=score,
            ))

        return examples

    def build_few_shot_prompt(
        self,
        query: str,
        max_examples: int = 3,
        max_tokens: int = 500,
        tags: Optional[list[str]] = None,
    ) -> str:
        """构建 Few-Shot 提示"""
        from app.memory.context import estimate_tokens

        examples = self.retrieve_similar(query, top_k=max_examples, tags=tags)
        if not examples:
            return ""

        lines = ["以下是类似任务的成功执行示例:"]
        total_tokens = estimate_tokens(lines[0])

        for i, ex in enumerate(examples):
            traj = ex.trajectory
            # 构建示例文本
            example_text = f"""
示例 {i + 1}:
任务: {traj.task[:100]}
工具: {', '.join(traj.tools_used[:5])}
结果: {traj.result[:100]}...
"""
            tokens = estimate_tokens(example_text)
            if total_tokens + tokens > max_tokens:
                break
            lines.append(example_text)
            total_tokens += tokens

        if len(lines) == 1:
            return ""

        return "\n".join(lines)

    def _cleanup_old(self) -> None:
        """清理旧轨迹"""
        # 按创建时间排序
        sorted_trajs = sorted(
            self._trajectories.values(),
            key=lambda t: t.created_at,
        )

        # 删除最旧的 20%
        to_delete = len(sorted_trajs) // 5
        for traj in sorted_trajs[:to_delete]:
            del self._trajectories[traj.id]
            for tag in traj.tags:
                if tag in self._tag_index:
                    self._tag_index[tag] = [
                        tid for tid in self._tag_index[tag] if tid != traj.id
                    ]

    def get_stats(self) -> dict:
        """获取统计信息"""
        total = len(self._trajectories)
        successful = sum(1 for t in self._trajectories.values() if t.success)
        return {
            "total": total,
            "successful": successful,
            "success_rate": successful / max(1, total),
            "avg_feedback": sum(t.user_feedback for t in self._trajectories.values()) / max(1, total),
        }


# 单例
_few_shot_builder: Optional[FewShotBuilder] = None


def get_few_shot_builder() -> FewShotBuilder:
    """获取 Few-Shot 构建器单例"""
    global _few_shot_builder
    if _few_shot_builder is None:
        _few_shot_builder = FewShotBuilder()
    return _few_shot_builder
