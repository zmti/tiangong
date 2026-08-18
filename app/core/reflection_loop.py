"""反思-改进循环:执行→评估→反思→策略调整→再执行→验证改进

这是智能体"自我进化"的核心能力:
1. 执行任务并评估结果
2. 反思失败原因(为什么成功/失败)
3. 生成改进策略(调整工具选择、参数、流程)
4. 再执行并验证改进效果
5. 将成功经验存储到经验库

对标主流 Agent:
- SWE-Agent: 反思-重试循环
- Devin: fix_hint 自动提取
- Claude Agent: 用户反馈闭环
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


class ReflectionPhase(str, Enum):
    """反思阶段"""
    EXECUTE = "execute"           # 执行任务
    EVALUATE = "evaluate"         # 评估结果
    REFLECT = "reflect"           # 反思原因
    STRATEGY = "strategy"         # 调整策略
    RE_EXECUTE = "re_execute"     # 再执行
    VERIFY = "verify"             # 验证改进
    LEARN = "learn"               # 学习经验


@dataclass
class ReflectionRecord:
    """反思记录"""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    task_id: str = ""
    phase: ReflectionPhase = ReflectionPhase.EXECUTE
    success: bool = False
    original_action: dict = field(default_factory=dict)
    result: dict = field(default_factory=dict)
    failure_analysis: str = ""
    improvement_strategy: str = ""
    re_execute_result: dict = field(default_factory=dict)
    improvement_verified: bool = False
    learned_experience: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "phase": self.phase.value,
            "success": self.success,
            "original_action": self.original_action,
            "result": self.result,
            "failure_analysis": self.failure_analysis,
            "improvement_strategy": self.improvement_strategy,
            "re_execute_result": self.re_execute_result,
            "improvement_verified": self.improvement_verified,
            "learned_experience": self.learned_experience,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class ImprovementStrategy:
    """改进策略"""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    strategy_type: str = ""  # tool_retry, parameter_adjust, alternative_tool, workflow_change
    description: str = ""
    tool_name: str = ""
    original_params: dict = field(default_factory=dict)
    adjusted_params: dict = field(default_factory=dict)
    alternative_tool: str = ""
    confidence: float = 0.5
    success_rate: float = 0.0
    created_at: float = field(default_factory=time.time)


class ReflectionLoop:
    """反思-改进循环管理器"""

    def __init__(self):
        self._records: dict[str, ReflectionRecord] = {}
        self._strategies: dict[str, ImprovementStrategy] = {}
        self._max_reflections = 3  # 最大反思次数
        self._min_confidence = 0.3  # 最小置信度

    def execute_and_reflect(
        self,
        task_id: str,
        action: dict,
        executor_fn: Any,
        evaluator_fn: Any,
        max_attempts: int = 3,
    ) -> ReflectionRecord:
        """执行任务并反思改进
        
        Args:
            task_id: 任务ID
            action: 要执行的动作 {"tool": "xxx", "params": {...}}
            executor_fn: 执行函数 fn(action) -> result
            evaluator_fn: 评估函数 fn(result) -> (success, score, feedback)
            max_attempts: 最大尝试次数
        
        Returns:
            ReflectionRecord: 反思记录
        """
        record = ReflectionRecord(task_id=task_id, original_action=action)
        
        for attempt in range(max_attempts):
            # 1. 执行
            record.phase = ReflectionPhase.EXECUTE
            try:
                result = executor_fn(action)
                record.result = result
            except Exception as e:
                result = {"error": str(e), "success": False}
                record.result = result
            
            # 2. 评估
            record.phase = ReflectionPhase.EVALUATE
            success, score, feedback = evaluator_fn(result)
            record.success = success
            
            if success:
                # 成功，记录经验
                record.phase = ReflectionPhase.LEARN
                record.learned_experience = self._extract_success_experience(action, result)
                self._store_experience(record)
                return record
            
            # 3. 反思
            record.phase = ReflectionPhase.REFLECT
            record.failure_analysis = self._analyze_failure(action, result, feedback)
            
            # 4. 生成改进策略
            record.phase = ReflectionPhase.STRATEGY
            strategy = self._generate_improvement_strategy(action, result, record.failure_analysis)
            record.improvement_strategy = strategy.description
            
            # 5. 调整动作
            action = self._apply_strategy(action, strategy)
            
            # 6. 再执行
            record.phase = ReflectionPhase.RE_EXECUTE
            try:
                re_result = executor_fn(action)
                record.re_execute_result = re_result
            except Exception as e:
                re_result = {"error": str(e), "success": False}
                record.re_execute_result = re_result
            
            # 7. 验证改进
            record.phase = ReflectionPhase.VERIFY
            re_success, re_score, re_feedback = evaluator_fn(re_result)
            record.improvement_verified = re_success
            
            if re_success:
                # 改进成功，记录经验
                record.phase = ReflectionPhase.LEARN
                record.learned_experience = self._extract_improvement_experience(
                    record.original_action, action, record.failure_analysis
                )
                self._store_experience(record)
                # 更新策略成功率
                strategy.success_rate = min(1.0, strategy.success_rate + 0.1)
                return record
            
            # 继续反思循环
            record.result = re_result
            record.failure_analysis += f"\n改进后仍然失败: {re_feedback}"
        
        # 达到最大尝试次数
        record.phase = ReflectionPhase.LEARN
        record.learned_experience = {
            "type": "failure_pattern",
            "pattern": record.failure_analysis,
            "strategies_tried": [record.improvement_strategy],
        }
        self._store_experience(record)
        return record

    def _analyze_failure(self, action: dict, result: dict, feedback: str) -> str:
        """分析失败原因"""
        analysis_parts = []
        
        # 工具执行错误
        if result.get("error"):
            error = result["error"]
            if "timeout" in error.lower():
                analysis_parts.append("工具执行超时")
            elif "permission" in error.lower():
                analysis_parts.append("权限不足")
            elif "not found" in error.lower():
                analysis_parts.append("资源不存在")
            else:
                analysis_parts.append(f"执行错误: {error[:100]}")
        
        # 参数问题
        if result.get("missing_params"):
            analysis_parts.append(f"缺少参数: {result['missing_params']}")
        
        # 逻辑错误
        if result.get("logical_error"):
            analysis_parts.append(f"逻辑错误: {result['logical_error']}")
        
        # LLM 反馈
        if feedback:
            analysis_parts.append(f"LLM 反馈: {feedback[:200]}")
        
        return "；".join(analysis_parts) if analysis_parts else "未知失败原因"

    def _generate_improvement_strategy(
        self,
        action: dict,
        result: dict,
        failure_analysis: str,
    ) -> ImprovementStrategy:
        """生成改进策略"""
        tool_name = action.get("tool", "")
        
        # 基于失败分析选择策略
        if "超时" in failure_analysis:
            return ImprovementStrategy(
                strategy_type="parameter_adjust",
                description="增加超时时间或减少批量大小",
                tool_name=tool_name,
                original_params=action.get("params", {}),
                adjusted_params={**action.get("params", {}), "timeout": 60},
                confidence=0.7,
            )
        elif "权限" in failure_analysis:
            return ImprovementStrategy(
                strategy_type="alternative_tool",
                description="使用不需要特殊权限的替代工具",
                tool_name=tool_name,
                alternative_tool=self._find_alternative_tool(tool_name),
                confidence=0.6,
            )
        elif "不存在" in failure_analysis:
            return ImprovementStrategy(
                strategy_type="parameter_adjust",
                description="检查路径或资源是否存在",
                tool_name=tool_name,
                original_params=action.get("params", {}),
                adjusted_params=self._verify_params(action.get("params", {})),
                confidence=0.8,
            )
        else:
            # 通用策略：重试并调整参数
            return ImprovementStrategy(
                strategy_type="tool_retry",
                description="重试并调整参数",
                tool_name=tool_name,
                original_params=action.get("params", {}),
                adjusted_params=self._adjust_params(action.get("params", {})),
                confidence=0.5,
            )

    def _apply_strategy(self, action: dict, strategy: ImprovementStrategy) -> dict:
        """应用改进策略到动作"""
        new_action = action.copy()
        
        if strategy.strategy_type == "parameter_adjust":
            new_action["params"] = strategy.adjusted_params
        elif strategy.strategy_type == "alternative_tool":
            new_action["tool"] = strategy.alternative_tool
        elif strategy.strategy_type == "tool_retry":
            new_action["params"] = strategy.adjusted_params
        
        return new_action

    def _find_alternative_tool(self, tool_name: str) -> str:
        """查找替代工具"""
        # 简单映射，实际应该基于工具能力图
        alternatives = {
            "bash": "python",
            "python": "bash",
            "http_get": "curl",
            "curl": "http_get",
        }
        return alternatives.get(tool_name, tool_name)

    def _verify_params(self, params: dict) -> dict:
        """验证并修正参数"""
        verified = params.copy()
        # 确保路径存在
        if "path" in verified:
            path = verified["path"]
            if not path.startswith("/") and not path.startswith("\\"):
                verified["path"] = f"./{path}"
        return verified

    def _adjust_params(self, params: dict) -> dict:
        """调整参数以提高成功率"""
        adjusted = params.copy()
        # 增加重试次数
        if "retries" in adjusted:
            adjusted["retries"] = min(3, adjusted["retries"] + 1)
        else:
            adjusted["retries"] = 1
        return adjusted

    def _extract_success_experience(self, action: dict, result: dict) -> dict:
        """提取成功经验"""
        return {
            "type": "success",
            "tool": action.get("tool"),
            "params": action.get("params"),
            "result_summary": str(result)[:200],
            "learned_at": time.time(),
        }

    def _extract_improvement_experience(
        self,
        original_action: dict,
        improved_action: dict,
        failure_analysis: str,
    ) -> dict:
        """提取改进经验"""
        return {
            "type": "improvement",
            "original_tool": original_action.get("tool"),
            "improved_tool": improved_action.get("tool"),
            "original_params": original_action.get("params"),
            "improved_params": improved_action.get("params"),
            "failure_reason": failure_analysis,
            "learned_at": time.time(),
        }

    def _store_experience(self, record: ReflectionRecord) -> None:
        """存储经验到失败记忆"""
        try:
            from app.memory.failure_memory import get_failure_memory
            
            if not record.success and record.failure_analysis:
                failure_memory = get_failure_memory()
                failure_memory.record_failure(
                    tool_name=record.original_action.get("tool", "unknown"),
                    failure_pattern=record.failure_analysis,
                    run_id=record.task_id,
                )
                
                # 如果有改进策略，添加修复建议
                if record.improvement_strategy:
                    failure_memory.add_fix_hint(
                        record.id,
                        record.improvement_strategy,
                    )
        except Exception as e:
            logger.warning(f"存储经验失败: {e}")

    def get_similar_strategies(self, tool_name: str, failure_pattern: str) -> list[ImprovementStrategy]:
        """获取相似的改进策略"""
        similar = []
        for strategy in self._strategies.values():
            if strategy.tool_name == tool_name:
                # 简单的相似度匹配
                if failure_pattern[:50] in strategy.description or strategy.description[:50] in failure_pattern:
                    similar.append(strategy)
        similar.sort(key=lambda s: s.confidence, reverse=True)
        return similar[:5]


# 单例
_reflection_loop: Optional[ReflectionLoop] = None


def get_reflection_loop() -> ReflectionLoop:
    """获取反思-改进循环单例"""
    global _reflection_loop
    if _reflection_loop is None:
        _reflection_loop = ReflectionLoop()
    return _reflection_loop
