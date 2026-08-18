"""Prompt 自优化: 版本管理 + A/B 测试 + 失败案例驱动修改

对标主流 Agent:
- DSPy: 自动 prompt 优化
- OPRO: LLM 驱动的 prompt 优化

核心能力:
1. Prompt 版本管理（保存历史版本）
2. 基于失败案例的自动修改建议
3. A/B 测试框架（比较不同版本效果）
4. 性能指标追踪（成功率、用户满意度）
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from app.config import settings
from app.core.storage_backend import StorageBackend, get_storage_backend

logger = logging.getLogger(__name__)


class PromptVersion(str, Enum):
    """Prompt 版本状态"""
    DRAFT = "draft"
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    EXPERIMENTAL = "experimental"


@dataclass
class PromptRecord:
    """Prompt 记录"""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = ""  # Prompt 名称
    content: str = ""  # Prompt 内容
    version: str = "1.0"
    status: PromptVersion = PromptVersion.DRAFT
    metrics: dict = field(default_factory=dict)  # 性能指标
    failure_cases: list = field(default_factory=list)  # 失败案例
    improvement_suggestions: list = field(default_factory=list)  # 改进建议
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "content": self.content[:200] + "..." if len(self.content) > 200 else self.content,
            "version": self.version,
            "status": self.status.value,
            "metrics": self.metrics,
            "failure_count": len(self.failure_cases),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class ABTestResult:
    """A/B 测试结果"""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    prompt_a_id: str = ""
    prompt_b_id: str = ""
    sample_size: int = 0
    metric: str = "success_rate"
    score_a: float = 0.0
    score_b: float = 0.0
    winner: str = ""  # "a" / "b" / "tie"
    confidence: float = 0.0
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "prompt_a_id": self.prompt_a_id,
            "prompt_b_id": self.prompt_b_id,
            "sample_size": self.sample_size,
            "metric": self.metric,
            "score_a": round(self.score_a, 4),
            "score_b": round(self.score_b, 4),
            "winner": self.winner,
            "confidence": round(self.confidence, 4),
            "created_at": self.created_at,
        }


class PromptOptimizer:
    """Prompt 自优化器"""

    def __init__(self, db_path: Optional[str] = None, backend: Optional[StorageBackend] = None):
        """初始化 PromptOptimizer
        
        Args:
            db_path: 数据库路径（SQLite 模式）
            backend: 存储后端实例，优先使用
        """
        if backend:
            self._backend = backend
            self._db_path = None
        else:
            self._db_path = db_path or os.path.join(settings.resolved_data_dir, "prompt_optimizer.db")
            self._backend = get_storage_backend(db_path=self._db_path)
        
        self._lock = threading.RLock()
        self._prompts: dict[str, PromptRecord] = {}
        self._active_prompt: Optional[str] = None  # 当前激活的 prompt ID
        self._ab_tests: dict[str, ABTestResult] = {}
        self._metrics_history: list[dict] = []
        self._init_db()
        self._load()

    def _init_db(self) -> None:
        """初始化数据库表"""
        schema = {
            "prompt_records": [
                {"name": "id", "type": "TEXT", "constraints": "PRIMARY KEY"},
                {"name": "name", "type": "TEXT", "constraints": "NOT NULL"},
                {"name": "content", "type": "TEXT", "constraints": "NOT NULL"},
                {"name": "version", "type": "TEXT", "constraints": "DEFAULT '1.0'"},
                {"name": "status", "type": "TEXT", "constraints": "DEFAULT 'draft'"},
                {"name": "metrics", "type": "TEXT", "constraints": "DEFAULT '{}'"},
                {"name": "failure_cases", "type": "TEXT", "constraints": "DEFAULT '[]'"},
                {"name": "improvement_suggestions", "type": "TEXT", "constraints": "DEFAULT '[]'"},
                {"name": "created_at", "type": "REAL", "constraints": "NOT NULL"},
                {"name": "updated_at", "type": "REAL", "constraints": "NOT NULL"},
            ],
            "ab_test_results": [
                {"name": "id", "type": "TEXT", "constraints": "PRIMARY KEY"},
                {"name": "prompt_a_id", "type": "TEXT", "constraints": "NOT NULL"},
                {"name": "prompt_b_id", "type": "TEXT", "constraints": "NOT NULL"},
                {"name": "sample_size", "type": "INTEGER", "constraints": "DEFAULT 0"},
                {"name": "metric", "type": "TEXT", "constraints": "DEFAULT 'success_rate'"},
                {"name": "score_a", "type": "REAL", "constraints": "DEFAULT 0.0"},
                {"name": "score_b", "type": "REAL", "constraints": "DEFAULT 0.0"},
                {"name": "winner", "type": "TEXT", "constraints": "DEFAULT ''"},
                {"name": "confidence", "type": "REAL", "constraints": "DEFAULT 0.0"},
                {"name": "created_at", "type": "REAL", "constraints": "NOT NULL"},
            ],
        }
        with self._lock:
            self._backend.init_db(schema)

    def _load(self) -> None:
        """从数据库加载数据"""
        with self._lock:
            # 加载 Prompt
            rows = self._backend.select("prompt_records", "1 = 1", order_by="created_at DESC")
            for row in rows:
                prompt = PromptRecord(
                    id=row["id"],
                    name=row["name"],
                    content=row["content"],
                    version=row["version"],
                    status=PromptVersion(row["status"]),
                    metrics=json.loads(row["metrics"] or "{}"),
                    failure_cases=json.loads(row["failure_cases"] or "[]"),
                    improvement_suggestions=json.loads(row["improvement_suggestions"] or "[]"),
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
                self._prompts[prompt.id] = prompt
                if prompt.status == PromptVersion.ACTIVE:
                    self._active_prompt = prompt.id

            # 加载 A/B 测试结果
            rows = self._backend.select("ab_test_results", "1 = 1")
            for row in rows:
                test = ABTestResult(
                    id=row["id"],
                    prompt_a_id=row["prompt_a_id"],
                    prompt_b_id=row["prompt_b_id"],
                    sample_size=row["sample_size"],
                    metric=row["metric"],
                    score_a=row["score_a"],
                    score_b=row["score_b"],
                    winner=row["winner"],
                    confidence=row["confidence"],
                    created_at=row["created_at"],
                )
                self._ab_tests[test.id] = test

    def create_prompt(
        self,
        name: str,
        content: str,
        version: str = "1.0",
    ) -> PromptRecord:
        """创建新 Prompt"""
        prompt = PromptRecord(
            name=name,
            content=content,
            version=version,
            status=PromptVersion.DRAFT,
        )
        
        # 保存到数据库
        with self._lock:
            self._backend.insert(
                "prompt_records",
                {
                    "id": prompt.id,
                    "name": prompt.name,
                    "content": prompt.content,
                    "version": prompt.version,
                    "status": prompt.status.value,
                    "metrics": json.dumps(prompt.metrics, ensure_ascii=False),
                    "failure_cases": json.dumps(prompt.failure_cases, ensure_ascii=False),
                    "improvement_suggestions": json.dumps(prompt.improvement_suggestions, ensure_ascii=False),
                    "created_at": prompt.created_at,
                    "updated_at": prompt.updated_at,
                }
            )
        
        self._prompts[prompt.id] = prompt
        logger.info(f"创建 Prompt: {name} v{version}")
        return prompt

    def activate_prompt(self, prompt_id: str) -> bool:
        """激活 Prompt"""
        if prompt_id not in self._prompts:
            return False

        # 停用旧的
        if self._active_prompt and self._active_prompt in self._prompts:
            old_prompt = self._prompts[self._active_prompt]
            old_prompt.status = PromptVersion.DEPRECATED
            self._update_prompt_status(old_prompt)

        # 激活新的
        prompt = self._prompts[prompt_id]
        prompt.status = PromptVersion.ACTIVE
        self._active_prompt = prompt_id
        self._update_prompt_status(prompt)
        
        logger.info(f"激活 Prompt: {prompt_id}")
        return True

    def _update_prompt_status(self, prompt: PromptRecord) -> None:
        """更新 Prompt 状态到数据库"""
        with self._lock:
            self._backend.update(
                "prompt_records",
                {"status": prompt.status.value, "updated_at": time.time()},
                "id = ?",
                (prompt.id,)
            )

    def get_active_prompt(self) -> Optional[PromptRecord]:
        """获取当前激活的 Prompt"""
        if self._active_prompt:
            return self._prompts.get(self._active_prompt)
        return None

    def record_failure(
        self,
        prompt_id: str,
        task: str,
        failure_reason: str,
        suggested_fix: str = "",
    ) -> bool:
        """记录失败案例"""
        if prompt_id not in self._prompts:
            return False

        prompt = self._prompts[prompt_id]
        prompt.failure_cases.append({
            "task": task,
            "reason": failure_reason,
            "fix": suggested_fix,
            "timestamp": time.time(),
        })

        # 自动建议改进
        if suggested_fix:
            prompt.improvement_suggestions.append(suggested_fix)

        prompt.updated_at = time.time()
        
        # 保存到数据库
        with self._lock:
            self._backend.update(
                "prompt_records",
                {
                    "failure_cases": json.dumps(prompt.failure_cases, ensure_ascii=False),
                    "improvement_suggestions": json.dumps(prompt.improvement_suggestions, ensure_ascii=False),
                    "updated_at": prompt.updated_at,
                },
                "id = ?",
                (prompt_id,)
            )
        
        return True

    async def generate_improvement(
        self,
        prompt_id: str,
        llm: Any,
    ) -> Optional[str]:
        """基于失败案例生成改进建议"""
        if prompt_id not in self._prompts:
            return None

        prompt = self._prompts[prompt_id]
        if not prompt.failure_cases:
            return None

        # 构建改进提示
        failure_summary = "\n".join([
            f"- 任务: {f['task'][:50]}... 原因: {f['reason'][:100]}"
            for f in prompt.failure_cases[-5:]  # 最近 5 个失败
        ])

        improvement_prompt = f"""你是一个 Prompt 优化专家。请基于以下失败案例，给出 Prompt 的改进建议。

当前 Prompt:
{prompt.content[:500]}

失败案例:
{failure_summary}

请给出:
1. 问题分析
2. 具体的改进建议（直接修改 Prompt 内容）
3. 预期效果

只输出改进后的完整 Prompt，不要其他内容。"""

        try:
            response = await llm.chat([{"role": "user", "content": improvement_prompt}], temperature=0.5)
            return response
        except Exception as e:
            logger.error(f"生成改进失败: {e}")
            return None

    def apply_improvement(
        self,
        prompt_id: str,
        new_content: str,
        version: Optional[str] = None,
    ) -> Optional[PromptRecord]:
        """应用改进（创建新版本）"""
        if prompt_id not in self._prompts:
            return None

        old_prompt = self._prompts[prompt_id]

        # 版本号递增
        if version is None:
            try:
                major, minor = old_prompt.version.split(".")
                version = f"{major}.{int(minor) + 1}"
            except Exception:
                version = "1.1"

        # 创建新版本
        new_prompt = self.create_prompt(
            name=old_prompt.name,
            content=new_content,
            version=version,
        )
        new_prompt.status = PromptVersion.EXPERIMENTAL
        
        # 更新状态
        with self._lock:
            self._backend.update(
                "prompt_records",
                {"status": new_prompt.status.value, "updated_at": time.time()},
                "id = ?",
                (new_prompt.id,)
            )

        logger.info(f"应用改进: {prompt_id} -> {new_prompt.id} (v{version})")
        return new_prompt

    def start_ab_test(
        self,
        prompt_a_id: str,
        prompt_b_id: str,
        metric: str = "success_rate",
    ) -> ABTestResult:
        """开始 A/B 测试"""
        test = ABTestResult(
            prompt_a_id=prompt_a_id,
            prompt_b_id=prompt_b_id,
            metric=metric,
        )
        
        # 保存到数据库
        with self._lock:
            self._backend.insert(
                "ab_test_results",
                {
                    "id": test.id,
                    "prompt_a_id": test.prompt_a_id,
                    "prompt_b_id": test.prompt_b_id,
                    "sample_size": test.sample_size,
                    "metric": test.metric,
                    "score_a": test.score_a,
                    "score_b": test.score_b,
                    "winner": test.winner,
                    "confidence": test.confidence,
                    "created_at": test.created_at,
                }
            )
        
        self._ab_tests[test.id] = test
        logger.info(f"开始 A/B 测试: {prompt_a_id} vs {prompt_b_id}")
        return test

    def record_ab_test_result(
        self,
        test_id: str,
        prompt_id: str,
        success: bool,
        score: Optional[float] = None,
    ) -> bool:
        """记录 A/B 测试结果"""
        if score is None:
            score = 1.0 if success else 0.0
        
        if test_id not in self._ab_tests:
            return False

        test = self._ab_tests[test_id]
        test.sample_size += 1

        if prompt_id == test.prompt_a_id:
            # 更新 A 的分数
            total_a = test.score_a * (test.sample_size - 1)
            test.score_a = (total_a + score) / test.sample_size
        elif prompt_id == test.prompt_b_id:
            # 更新 B 的分数
            total_b = test.score_b * (test.sample_size - 1)
            test.score_b = (total_b + score) / test.sample_size

        # 判断胜者（需要足够样本）
        if test.sample_size >= 20:
            diff = abs(test.score_a - test.score_b)
            if diff < 0.05:
                test.winner = "tie"
            elif test.score_a > test.score_b:
                test.winner = "a"
            else:
                test.winner = "b"
            test.confidence = min(1.0, test.sample_size / 100)

        # 保存到数据库
        with self._lock:
            self._backend.update(
                "ab_test_results",
                {
                    "sample_size": test.sample_size,
                    "score_a": test.score_a,
                    "score_b": test.score_b,
                    "winner": test.winner,
                    "confidence": test.confidence,
                },
                "id = ?",
                (test_id,)
            )

        return True

    def get_ab_test_result(self, test_id: str) -> Optional[ABTestResult]:
        """获取 A/B 测试结果"""
        return self._ab_tests.get(test_id)

    def record_metrics(
        self,
        prompt_id: str,
        metrics: dict,
    ) -> None:
        """记录性能指标"""
        if prompt_id in self._prompts:
            prompt = self._prompts[prompt_id]
            prompt.metrics.update(metrics)
            prompt.updated_at = time.time()
            
            # 保存到数据库
            with self._lock:
                self._backend.update(
                    "prompt_records",
                    {
                        "metrics": json.dumps(prompt.metrics, ensure_ascii=False),
                        "updated_at": prompt.updated_at,
                    },
                    "id = ?",
                    (prompt_id,)
                )

        self._metrics_history.append({
            "prompt_id": prompt_id,
            "metrics": metrics,
            "timestamp": time.time(),
        })

    def get_best_prompt(self) -> Optional[PromptRecord]:
        """获取性能最好的 Prompt"""
        if not self._prompts:
            return None

        # 按成功率排序
        sorted_prompts = sorted(
            self._prompts.values(),
            key=lambda p: p.metrics.get("success_rate", 0),
            reverse=True,
        )
        return sorted_prompts[0] if sorted_prompts else None

    def list_prompts(self, status: Optional[PromptVersion] = None) -> list[dict]:
        """列出所有 Prompt"""
        prompts = list(self._prompts.values())
        if status:
            prompts = [p for p in prompts if p.status == status]
        return [p.to_dict() for p in prompts]

    def get_metrics_history(
        self,
        prompt_id: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        """获取指标历史"""
        history = self._metrics_history
        if prompt_id:
            history = [h for h in history if h["prompt_id"] == prompt_id]
        return history[-limit:]


# 单例
_optimizer: Optional[PromptOptimizer] = None


def get_prompt_optimizer() -> PromptOptimizer:
    """获取 Prompt 优化器单例"""
    global _optimizer
    if _optimizer is None:
        _optimizer = PromptOptimizer()
    return _optimizer
