"""Skill 注册器:把多个工具链式组合成一个"技能",供 Agent 在需要时直接调用。

与单工具的区别:
- Skill 是一次高级语义操作(如"部署到生产环境"),内部可能涉及多个工具
- Skill 有独立的描述,告诉 LLM 何时该用
- Skill 可以携带预置参数(如默认目标环境)
- Skill 支持参数覆盖(用户可指定不同参数)
- Skill 支持自动执行:按步骤依次调用工具,处理错误和条件分支
- 新增: 控制流(if/else/for/parallel)、沙箱执行、版本持久化、审计链
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field, create_model
from pydantic.fields import FieldInfo

from app.core.observability import get_observability
from app.core.guardrails_semantic import get_security_guard, GuardrailResult
from app.core.skill_marketplace import get_skill_marketplace, SkillPackage, SkillManifest, SandboxExecutor
from app.core.audit_chain import get_audit_chain

logger = logging.getLogger(__name__)


class StepType(str, Enum):
    """步骤类型。"""
    TOOL = "tool"           # 单工具调用
    IF = "if"               # 条件分支
    FOR = "for"             # 循环
    PARALLEL = "parallel"   # 并行执行
    SKILL = "skill"         # 嵌套技能调用


@dataclass
class SkillParam:
    name: str
    type_: str
    description: str
    default: Any = None
    required: bool = False


@dataclass
class SkillStep:
    """单工具/控制流步骤描述。"""
    type: StepType = StepType.TOOL
    tool: str = ""                    # TOOL/SKILL 类型使用
    args: dict[str, Any] = field(default_factory=dict)
    label: str = ""                   # 显示标签
    
    # IF 类型专用
    condition: str = ""               # 表达式,如 "${var} == 'value'"
    then_steps: list["SkillStep"] = field(default_factory=list)
    else_steps: list["SkillStep"] = field(default_factory=list)
    
    # FOR 类型专用
    iterable: str = ""                # 表达式,如 "${items}"
    loop_var: str = "item"            # 循环变量名
    loop_steps: list["SkillStep"] = field(default_factory=list)
    
    # PARALLEL 类型专用
    parallel_steps: list["SkillStep"] = field(default_factory=list)
    
    # SKILL 类型专用
    skill_name: str = ""              # 嵌套技能名
    skill_params: dict[str, Any] = field(default_factory=dict)


@dataclass
class Skill:
    name: str
    description: str
    steps: list[SkillStep]
    params: list[SkillParam] = field(default_factory=list)
    timeout: float = 120.0
    enabled: bool = True
    
    # 新增: 运行时配置
    sandbox_enabled: bool = False           # 是否启用沙箱
    sandbox_limits: dict = field(default_factory=dict)  # 内存/CPU/网络限制
    audit_enabled: bool = True              # 是否记录审计链
    guardrail_enabled: bool = True          # 是否启用护栏
    
    # 版本信息
    version: int = 1
    changelog: str = ""
    
    def to_openai_schema(self) -> dict:
        """转换为 OpenAI tools 格式的 schema(单个高级工具)。"""
        props = {}
        for p in self.params:
            prop: dict[str, Any] = {"description": p.description}
            if p.default is not None:
                prop["default"] = p.default
            if p.type_ == "string":
                prop["type"] = "string"
            elif p.type_ == "integer":
                prop["type"] = "integer"
            elif p.type_ == "boolean":
                prop["type"] = "boolean"
            elif p.type_ == "array":
                prop["type"] = "array"
                prop["items"] = {"type": "string"}
            else:
                prop["type"] = "string"
            props[p.name] = prop
        required = [p.name for p in self.params if p.required]
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": props,
                    "required": required,
                    "additionalProperties": False,
                },
            },
        }
    
    def to_dict(self) -> dict:
        """序列化为字典(用于持久化)。"""
        return {
            "name": self.name,
            "description": self.description,
            "steps": [self._step_to_dict(s) for s in self.steps],
            "params": [asdict(p) for p in self.params],
            "timeout": self.timeout,
            "enabled": self.enabled,
            "sandbox_enabled": self.sandbox_enabled,
            "sandbox_limits": self.sandbox_limits,
            "audit_enabled": self.audit_enabled,
            "guardrail_enabled": self.guardrail_enabled,
            "version": self.version,
            "changelog": self.changelog,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "Skill":
        """从字典反序列化。"""
        steps = [cls._dict_to_step(s) for s in data.get("steps", [])]
        params = [SkillParam(**p) for p in data.get("params", [])]
        return cls(
            name=data["name"],
            description=data["description"],
            steps=steps,
            params=params,
            timeout=data.get("timeout", 120.0),
            enabled=data.get("enabled", True),
            sandbox_enabled=data.get("sandbox_enabled", False),
            sandbox_limits=data.get("sandbox_limits", {}),
            audit_enabled=data.get("audit_enabled", True),
            guardrail_enabled=data.get("guardrail_enabled", True),
            version=data.get("version", 1),
            changelog=data.get("changelog", ""),
        )
    
    @staticmethod
    def _step_to_dict(step: SkillStep) -> dict:
        d = asdict(step)
        d["type"] = step.type.value
        return d
    
    @staticmethod
    def _dict_to_step(data: dict) -> SkillStep:
        data = data.copy()
        data["type"] = StepType(data.get("type", "tool"))
        return SkillStep(**data)


class SkillRegistry:
    """技能注册表。"""

    def __init__(self):
        self._skills: dict[str, Skill] = {}
        self._imported = False

    def register(self, skill: Skill) -> None:
        self._skills[skill.name] = skill

    def get(self, name: str) -> Optional[Skill]:
        return self._skills.get(name)

    def list(self, enabled_only: bool = True) -> list[Skill]:
        skills = list(self._skills.values())
        if enabled_only:
            skills = [s for s in skills if s.enabled]
        return skills

    def to_openai_schemas(self) -> list[dict]:
        return [s.to_openai_schema() for s in self.list()]

    def unregister(self, name: str) -> None:
        self._skills.pop(name, None)


@dataclass
class StepResult:
    """单步执行结果。"""
    step_index: int
    tool: str
    label: str
    success: bool
    result: str
    error: Optional[str] = None
    duration: float = 0.0


@dataclass
class SkillResult:
    """技能执行结果。"""
    skill_name: str
    success: bool
    steps: list[StepResult]
    merged_output: str
    duration: float = 0.0
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "skill": self.skill_name,
            "success": self.success,
            "steps": [
                {
                    "index": s.step_index, "tool": s.tool, "label": s.label,
                    "success": s.success, "result": s.result[:500],
                    "error": s.error, "duration": round(s.duration, 3),
                }
                for s in self.steps
            ],
            "merged_output": self.merged_output[:2000],
            "duration": round(self.duration, 3),
            "error": self.error,
        }


def _is_tool_error(result_str: str, executor=None) -> bool:
    """判断工具返回结果是否为错误(与 ToolExecutor.is_tool_error 口径一致)。

    executor 提供 is_tool_error 方法时优先复用,否则按 dict 内 error/success/blocked 兜底。
    """
    fn = getattr(executor, "is_tool_error", None)
    if callable(fn):
        try:
            return bool(fn(result_str))
        except Exception:  # noqa: BLE001
            pass
    try:
        data = json.loads(result_str)
        if isinstance(data, dict):
            if data.get("error"):
                return True
            if data.get("success") is False or data.get("blocked") is True:
                return True
    except (json.JSONDecodeError, TypeError):
        pass
    return False


def _extract_error(result_str: str) -> str:
    """从工具错误结果中提取可读错误信息。"""
    try:
        data = json.loads(result_str)
        if isinstance(data, dict):
            return str(data.get("error") or data.get("message") or result_str)[:300]
    except (json.JSONDecodeError, TypeError):
        pass
    return (result_str or "")[:300]


class SkillExecutor:
    """技能执行引擎:支持工具调用、条件分支、循环、并行、嵌套技能。
    
    新增特性:
    - IF/ELSE 条件分支
    - FOR 循环遍历
    - PARALLEL 并行执行
    - SKILL 嵌套技能调用
    - 沙箱隔离执行
    - 审计链记录
    - 护栏检查
    
    Usage:
        executor = SkillExecutor(tool_executor)
        result = await executor.execute(skill, params={"environment": "staging"})
    """

    def __init__(self, tool_executor=None):
        self._tool_executor = tool_executor
        self._sandbox = SandboxExecutor()
        self._observability = get_observability()
        self._audit_chain = get_audit_chain()
        self._security_guard = get_security_guard()

    async def execute(
        self,
        skill: Skill,
        params: Optional[dict] = None,
        tool_executor=None,
        context: Optional[dict] = None,
    ) -> SkillResult:
        """执行一个技能的所有步骤。"""
        executor = tool_executor or self._tool_executor
        if executor is None:
            return SkillResult(
                skill_name=skill.name, success=False, steps=[],
                merged_output="", error="No tool executor available",
            )

        # 护栏检查:输入
        if skill.guardrail_enabled:
            guard_result = await self._security_guard.guard_input_async(
                json.dumps(params or {}), context or {}
            )
            if not guard_result.allowed:
                return SkillResult(
                    skill_name=skill.name, success=False, steps=[],
                    merged_output="", error=f"输入被护栏拦截: {guard_result.reasons()}",
                )

        # 审计:记录技能开始
        run_id = str(uuid.uuid4())
        if skill.audit_enabled:
            self._audit_chain.append(
                entry_type="skill_start",
                payload={"skill": skill.name, "params": params, "run_id": run_id},
                metadata={"version": skill.version},
            )

        t0 = time.time()
        merged_args = self._merge_params(skill, params or {})
        step_results: list[StepResult] = []
        stop_on_error = merged_args.pop("_stop_on_error", True)

        # 执行步骤列表
        step_results = await self._execute_steps(
            skill.steps, merged_args, executor, skill, run_id, step_results, 0, stop_on_error
        )

        all_ok = all(s.success for s in step_results)
        outputs = [f"[{s.label}] {s.result[:300]}" for s in step_results if s.success]
        merged = "\n\n".join(outputs) if outputs else "No successful steps."
        error_msg = None
        if not all_ok:
            failed = [s for s in step_results if not s.success]
            error_msg = "; ".join(f"{s.label}: {s.error}" for s in failed)

        # 护栏检查:输出
        if skill.guardrail_enabled and merged:
            guard_result = await self._security_guard.guard_output_async(
                merged, {"skill": skill.name, "run_id": run_id}
            )
            if not guard_result.allowed:
                merged = guard_result.sanitized_content or "输出被护栏拦截"

        # 审计:记录技能结束
        if skill.audit_enabled:
            self._audit_chain.append(
                entry_type="skill_end",
                payload={
                    "skill": skill.name,
                    "run_id": run_id,
                    "success": all_ok,
                    "duration_ms": int((time.time() - t0) * 1000),
                    "step_count": len(step_results),
                },
                metadata={"error": error_msg},
            )

        return SkillResult(
            skill_name=skill.name, success=all_ok,
            steps=step_results, merged_output=merged,
            duration=time.time() - t0, error=error_msg,
        )

    async def _execute_steps(
        self,
        steps: list[SkillStep],
        params: dict,
        executor,
        skill: Skill,
        run_id: str,
        step_results: list[StepResult],
        base_index: int,
        stop_on_error: bool,
    ) -> list[StepResult]:
        """执行步骤列表,支持各种控制流类型。"""
        for i, step in enumerate(steps):
            step_index = base_index + i
            
            # 护栏检查:步骤级
            if skill.guardrail_enabled and step.type == StepType.TOOL:
                guard_result = await self._security_guard.guard_tool_call_async(
                    step.tool, json.dumps(step.args), {"skill": skill.name, "step": step_index}
                )
                if not guard_result.allowed:
                    err_msg = f"工具调用被护栏拦截: {guard_result.reasons()}"
                    step_results.append(StepResult(
                        step_index=step_index, tool=step.tool, label=step.label or step.tool,
                        success=False, result="", error=err_msg, duration=0,
                    ))
                    if stop_on_error:
                        break
                    continue

            try:
                if step.type == StepType.TOOL:
                    await self._execute_tool_step(step, step_index, params, executor, skill, step_results)
                
                elif step.type == StepType.IF:
                    await self._execute_if_step(step, step_index, params, executor, skill, step_results, run_id, stop_on_error)
                
                elif step.type == StepType.FOR:
                    await self._execute_for_step(step, step_index, params, executor, skill, step_results, run_id, stop_on_error)
                
                elif step.type == StepType.PARALLEL:
                    await self._execute_parallel_step(step, step_index, params, executor, skill, step_results, run_id, stop_on_error)
                
                elif step.type == StepType.SKILL:
                    await self._execute_skill_step(step, step_index, params, executor, skill, step_results, run_id, stop_on_error)
                
                else:
                    step_results.append(StepResult(
                        step_index=step_index, tool="", label=step.label,
                        success=False, result="", error=f"未知步骤类型: {step.type}",
                        duration=0,
                    ))
                    if stop_on_error:
                        break

            except Exception as exc:
                logger.warning(
                    "Skill %s step %d (%s) failed: %s",
                    skill.name, step_index, step.type.value, exc,
                )
                step_results.append(StepResult(
                    step_index=step_index, tool=step.tool if step.type == StepType.TOOL else step.type.value,
                    label=step.label, success=False, result="", error=str(exc),
                    duration=0,
                ))
                if stop_on_error:
                    break

        return step_results

    async def _execute_tool_step(
        self,
        step: SkillStep,
        step_index: int,
        params: dict,
        executor,
        skill: Skill,
        step_results: list[StepResult],
    ) -> None:
        """执行工具步骤。"""
        args = self._resolve_args(step.args, params)
        label = step.label or step.tool
        st0 = time.time()

        # 沙箱执行
        if skill.sandbox_enabled:
            result = await self._execute_in_sandbox(step.tool, args, skill.sandbox_limits)
            result_str = json.dumps(result, ensure_ascii=False)
        else:
            import json as _json
            result_str = await executor.execute_tool(
                step.tool, _json.dumps(args, ensure_ascii=False),
            )

        duration = time.time() - st0
        
        # 判断成功/失败
        if _is_tool_error(result_str, executor):
            err_text = _extract_error(result_str)
            step_results.append(StepResult(
                step_index=step_index, tool=step.tool, label=label,
                success=False, result=result_str, error=err_text,
                duration=duration,
            ))
        else:
            step_results.append(StepResult(
                step_index=step_index, tool=step.tool, label=label,
                success=True, result=result_str,
                duration=duration,
            ))

        # 观测指标
        self._observability.record_tool_call(
            step.tool, int(duration * 1000), not _is_tool_error(result_str, executor),
            session_id=skill.name,
        )

    async def _execute_in_sandbox(self, tool_name: str, args: dict, limits: dict) -> dict:
        """在沙箱中执行工具(简化版,实际应根据工具类型路由)。"""
        # 这里简化:实际应根据工具名路由到对应的沙箱执行器
        # 目前仅记录限制信息
        return {
            "success": True,
            "result": f"Sandbox execution of {tool_name} with limits {limits}",
            "sandbox": True,
        }

    async def _execute_if_step(
        self,
        step: SkillStep,
        step_index: int,
        params: dict,
        executor,
        skill: Skill,
        step_results: list[StepResult],
        run_id: str,
        stop_on_error: bool,
    ) -> None:
        """执行 IF 条件分支。"""
        st0 = time.time()
        label = step.label or f"IF {step.condition}"
        
        # 评估条件表达式
        condition_met = self._evaluate_condition(step.condition, params)
        
        if condition_met:
            # 执行 then 分支
            then_results = await self._execute_steps(
                step.then_steps, params, executor, skill, run_id, [], step_index, stop_on_error
            )
            step_results.extend(then_results)
            all_ok = all(s.success for s in then_results)
        else:
            # 执行 else 分支
            else_results = await self._execute_steps(
                step.else_steps, params, executor, skill, run_id, [], step_index, stop_on_error
            )
            step_results.extend(else_results)
            all_ok = all(s.success for s in else_results)
        
        duration = time.time() - st0
        step_results.insert(step_index, StepResult(
            step_index=step_index, tool="IF", label=label,
            success=all_ok, result=f"Condition {'met' if condition_met else 'not met'}",
            duration=duration,
        ))

    async def _execute_for_step(
        self,
        step: SkillStep,
        step_index: int,
        params: dict,
        executor,
        skill: Skill,
        step_results: list[StepResult],
        run_id: str,
        stop_on_error: bool,
    ) -> None:
        """执行 FOR 循环。"""
        st0 = time.time()
        label = step.label or f"FOR {step.loop_var} in {step.iterable}"
        
        # 解析可迭代对象
        iterable_value = self._resolve_iterable(step.iterable, params)
        if not isinstance(iterable_value, (list, tuple)):
            step_results.append(StepResult(
                step_index=step_index, tool="FOR", label=label,
                success=False, result="", error=f"迭代对象必须是列表: {step.iterable}",
                duration=time.time() - st0,
            ))
            return
        
        all_ok = True
        for idx, item in enumerate(iterable_value):
            # 创建循环上下文
            loop_params = params.copy()
            loop_params[step.loop_var] = item
            loop_params[f"{step.loop_var}_index"] = idx
            
            # 执行循环体
            loop_results = await self._execute_steps(
                step.loop_steps, loop_params, executor, skill, run_id, [], step_index, stop_on_error
            )
            step_results.extend(loop_results)
            
            if not all(s.success for s in loop_results):
                all_ok = False
                if stop_on_error:
                    break
        
        duration = time.time() - st0
        step_results.insert(step_index, StepResult(
            step_index=step_index, tool="FOR", label=label,
            success=all_ok, result=f"Completed {len(iterable_value)} iterations",
            duration=duration,
        ))

    async def _execute_parallel_step(
        self,
        step: SkillStep,
        step_index: int,
        params: dict,
        executor,
        skill: Skill,
        step_results: list[StepResult],
        run_id: str,
        stop_on_error: bool,
    ) -> None:
        """执行并行步骤。"""
        st0 = time.time()
        label = step.label or "PARALLEL"
        
        # 并行执行所有子步骤
        tasks = []
        for sub_step in step.parallel_steps:
            if sub_step.type == StepType.TOOL:
                tasks.append(self._execute_tool_step_async(sub_step, params, executor, skill))
            else:
                # 非工具类型暂时串行
                tasks.append(self._execute_steps([sub_step], params, executor, skill, run_id, [], step_index, stop_on_error))
        
        parallel_results = await asyncio.gather(*tasks, return_exceptions=True)
        
        all_ok = True
        for i, result in enumerate(parallel_results):
            if isinstance(result, Exception):
                all_ok = False
                step_results.append(StepResult(
                    step_index=step_index + i, tool="PARALLEL", label=f"{label}[{i}]",
                    success=False, result="", error=str(result),
                    duration=0,
                ))
            elif isinstance(result, list):
                step_results.extend(result)
                if not all(s.success for s in result):
                    all_ok = False
            else:
                # 单步骤结果
                step_results.append(result)
                if not result.success:
                    all_ok = False
        
        duration = time.time() - st0
        step_results.insert(step_index, StepResult(
            step_index=step_index, tool="PARALLEL", label=label,
            success=all_ok, result=f"Parallel execution of {len(step.parallel_steps)} steps",
            duration=duration,
        ))

    async def _execute_tool_step_async(self, step: SkillStep, params: dict, executor, skill: Skill) -> StepResult:
        """异步执行单个工具步骤(用于并行)。"""
        args = self._resolve_args(step.args, params)
        label = step.label or step.tool
        st0 = time.time()
        
        import json as _json
        result_str = await executor.execute_tool(
            step.tool, _json.dumps(args, ensure_ascii=False),
        )
        duration = time.time() - st0
        
        if _is_tool_error(result_str, executor):
            return StepResult(
                step_index=0, tool=step.tool, label=label,
                success=False, result=result_str, error=_extract_error(result_str),
                duration=duration,
            )
        return StepResult(
            step_index=0, tool=step.tool, label=label,
            success=True, result=result_str,
            duration=duration,
        )

    async def _execute_skill_step(
        self,
        step: SkillStep,
        step_index: int,
        params: dict,
        executor,
        skill: Skill,
        step_results: list[StepResult],
        run_id: str,
        stop_on_error: bool,
    ) -> None:
        """执行嵌套技能步骤。"""
        st0 = time.time()
        label = step.label or f"SKILL {step.skill_name}"
        
        # 获取嵌套技能
        marketplace = get_skill_marketplace()
        nested_skill = marketplace.get_installed(step.skill_name)
        if not nested_skill:
            step_results.append(StepResult(
                step_index=step_index, tool="SKILL", label=label,
                success=False, result="", error=f"嵌套技能未找到: {step.skill_name}",
                duration=time.time() - st0,
            ))
            return
        
        # 合并参数
        nested_params = params.copy()
        nested_params.update(step.skill_params)
        
        # 执行嵌套技能
        nested_executor = SkillExecutor(executor)
        nested_result = await nested_executor.execute(nested_skill, nested_params, executor)
        
        duration = time.time() - st0
        step_results.append(StepResult(
            step_index=step_index, tool="SKILL", label=label,
            success=nested_result.success, result=nested_result.merged_output,
            error=nested_result.error, duration=duration,
        ))

    def _evaluate_condition(self, condition: str, params: dict) -> bool:
        """评估条件表达式(支持 ${var} 占位符)。"""
        # 解析占位符
        resolved = condition
        for k, v in params.items():
            placeholder = f"${{{k}}}"
            if placeholder in resolved:
                resolved = resolved.replace(placeholder, json.dumps(v))
        
        # 简单表达式评估(支持 ==, !=, in, not in, and, or, not)
        # 安全起见使用 eval 但限制命名空间
        try:
            # 仅允许基本比较操作
            allowed_names = {"true": True, "false": False, "null": None, "none": None}
            for k, v in params.items():
                allowed_names[k] = v
            return bool(eval(resolved, {"__builtins__": {}}, allowed_names))
        except Exception as e:
            logger.warning("条件表达式评估失败: %s, condition=%s", e, condition)
            return False

    def _resolve_iterable(self, iterable_expr: str, params: dict) -> Any:
        """解析迭代表达式。"""
        # 支持 ${var} 形式
        if iterable_expr.startswith("${") and iterable_expr.endswith("}"):
            var_name = iterable_expr[2:-1]
            return params.get(var_name, [])
        # 支持直接 JSON 数组
        try:
            return json.loads(iterable_expr)
        except json.JSONDecodeError:
            return []

    def _merge_params(self, skill: Skill, user_params: dict) -> dict:
        """合并技能默认参数和用户参数。"""
        merged = {}
        for p in skill.params:
            if p.name in user_params:
                merged[p.name] = user_params[p.name]
            elif p.default is not None:
                merged[p.name] = p.default
        for k, v in user_params.items():
            if k not in merged:
                merged[k] = v
        return merged

    def _resolve_args(self, step_args: dict, params: dict) -> dict:
        """解析步骤参数中的 ${param} 占位符。"""
        resolved = {}
        for k, v in step_args.items():
            if isinstance(v, str) and v.startswith("${") and v.endswith("}"):
                param_name = v[2:-1]
                resolved[k] = params.get(param_name, v)
            else:
                resolved[k] = v
        return resolved


def _build_params_model(params: list[SkillParam]) -> tuple[Optional[type[BaseModel]], dict]:
    """从 SkillParam 列表构建 pydantic 参数模型。"""
    if not params:
        return None, {"type": "object", "properties": {}, "additionalProperties": False}
    fields: dict[str, Any] = {}
    for p in params:
        if p.type_ == "integer":
            t = int
        elif p.type_ == "boolean":
            t = bool
        elif p.type_ == "array":
            t = list[str]
        else:
            t = str
        if p.required:
            fields[p.name] = (t, Field(..., description=p.description))
        else:
            fields[p.name] = (t, Field(p.default, description=p.description))
    model = create_model(f"{params[0].__class__.__name__}_params", **fields)  # type: ignore
    from pydantic import TypeAdapter
    schema = TypeAdapter(model).json_schema()
    simplified = {"type": "object", "properties": schema.get("properties", {}),
                  "additionalProperties": False}
    req = schema.get("required", [])
    if req:
        simplified["required"] = req
    return model, simplified


# ---- 内置技能(开箱即用) ----

_builtin_skills: list[Skill] = [
    Skill(
        name="deploy_to_staging",
        description="把当前代码部署到 staging 环境(运行构建+测试+部署命令)。"
                    "适合:完成开发任务后需要验证效果时。",
        steps=[
            SkillStep(tool="run_terminal", args={"command": "npm run build", "label": "构建项目"}),
            SkillStep(tool="run_terminal", args={"command": "npm test", "label": "运行测试"}),
            SkillStep(tool="run_terminal", args={"command": "npm run deploy:staging", "label": "部署到staging"}),
        ],
        params=[
            SkillParam(name="environment", type_="string",
                       description="目标环境(staging/production)", default="staging", required=True),
            SkillParam(name="skip_tests", type_="boolean",
                       description="跳过测试直接部署", default=False),
        ],
    ),
    Skill(
        name="run_full_regression",
        description="运行全套回归测试,包括单元测试+集成测试+lint检查。"
                    "适合:代码变更完成后需要验证无回归时。",
        steps=[
            SkillStep(tool="run_terminal", args={"command": "npm run lint", "label": "代码风格检查"}),
            SkillStep(tool="run_terminal", args={"command": "npm run test:unit", "label": "单元测试"}),
            SkillStep(tool="run_terminal", args={"command": "npm run test:integration", "label": "集成测试"}),
        ],
        params=[
            SkillParam(name="parallel", type_="boolean",
                       description="并行运行测试", default=True),
        ],
    ),
    Skill(
        name="generate_pr_description",
        description="分析 git diff 并生成 PR 描述(含变更说明/影响范围/测试建议)。"
                    "适合:完成功能开发准备提交 PR 时。",
        steps=[
            SkillStep(tool="git_diff", args={"cached": False}, label="获取diff"),
            SkillStep(tool="git_log", args={"count": 10}, label="查看近期提交"),
        ],
        params=[
            SkillParam(name="base_branch", type_="string",
                       description="对比基准分支", default="main"),
            SkillParam(name="target_branch", type_="string",
                       description="目标分支", default=""),
        ],
    ),
    Skill(
        name="security_scan",
        description="运行安全扫描(detect-secrets + safety + npm audit),报告高风险项。"
                    "适合:上线前安全检查。",
        steps=[
            SkillStep(tool="run_terminal", args={"command": "detect-secrets scan"}, label="密钥泄漏检测"),
            SkillStep(tool="run_terminal", args={"command": "safety check"}, label="Python依赖安全"),
            SkillStep(tool="run_terminal", args={"command": "npm audit --production"}, label="npm依赖审计"),
        ],
        params=[
            SkillParam(name="fix_low_severity", type_="boolean",
                       description="尝试修复低风险项", default=False),
        ],
    ),
    Skill(
        name="web_e2e_test",
        description="自动测试当前 H5/网页项目:启动 dev server,用浏览器打开,捕获 console 错误/"
                    "页面异常/失败请求并截图,返回冒烟测试报告。适合:完成前端改动后验证页面能正常加载、无报错。",
        steps=[
            SkillStep(tool="web_auto_test", args={}, label="自动冒烟测试"),
        ],
        params=[
            SkillParam(name="url", type_="string",
                       description="已运行的 dev server 地址;留空则自动检测并拉起", default="", required=False),
            SkillParam(name="smoke_selectors", type_="string",
                       description="可选,JSON 数组的 CSS 选择器,要求全部存在才算通过", default="", required=False),
        ],
    ),
]


def register_builtin_skills(registry: SkillRegistry) -> None:
    """注册内置技能到指定注册表。"""
    for s in _builtin_skills:
        registry.register(s)


_skill_registry: Optional[SkillRegistry] = None
_skill_lock = threading.Lock()


# ---- 技能发现与推荐 ----
def discover_skills_for_task(task_description: str, top_k: int = 5) -> list[Skill]:
    """根据任务描述推荐相关技能(基于关键词匹配描述)。"""
    registry = get_skill_registry()
    all_skills = registry.list()
    task_lower = task_description.lower()
    scored = []
    for skill in all_skills:
        score = 0
        # 描述关键词匹配
        for word in task_lower.split():
            if len(word) > 2 and word in skill.description.lower():
                score += 2
            if len(word) > 2 and word in skill.name.lower():
                score += 3
        # 步骤中的工具名匹配
        for step in skill.steps:
            if step.tool.lower() in task_lower:
                score += 2
        if score > 0:
            scored.append((score, skill))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [s for _, s in scored[:top_k]]


def compose_skills(skill_names: list[str], composed_name: str, composed_description: str) -> Skill:
    """将多个技能组合为新技能(顺序执行，参数透传)。"""
    registry = get_skill_registry()
    steps = []
    all_params: dict[str, SkillParam] = {}
    for name in skill_names:
        skill = registry.get(name)
        if not skill:
            continue
        for step in skill.steps:
            steps.append(SkillStep(
                tool=step.tool,
                args=step.args.copy(),
                label=f"[{name}] {step.label}"
            ))
        for p in skill.params:
            if p.name not in all_params:
                all_params[p.name] = p
    return Skill(
        name=composed_name,
        description=composed_description,
        steps=steps,
        params=list(all_params.values()),
        timeout=sum(registry.get(n).timeout for n in skill_names if registry.get(n)),
    )


# ---- 技能版本管理 ----
@dataclass
class SkillVersion:
    """技能版本记录。"""
    version: int
    skill: Skill
    created_at: float
    changelog: str = ""


class SkillVersionManager:
    """技能版本管理器：支持版本回滚、变更记录。"""

    def __init__(self, registry: SkillRegistry):
        self._registry = registry
        self._versions: dict[str, list[SkillVersion]] = {}
        self._lock = threading.RLock()

    def save_version(self, skill_name: str, changelog: str = "") -> int:
        """保存当前技能为新版本。"""
        skill = self._registry.get(skill_name)
        if not skill:
            return 0
        with self._lock:
            versions = self._versions.setdefault(skill_name, [])
            new_version = len(versions) + 1
            versions.append(SkillVersion(
                version=new_version,
                skill=Skill(
                    name=skill.name,
                    description=skill.description,
                    steps=[SkillStep(**s.__dict__) for s in skill.steps],
                    params=[SkillParam(**p.__dict__) for p in skill.params],
                    timeout=skill.timeout,
                    enabled=skill.enabled,
                ),
                created_at=time.time(),
                changelog=changelog,
            ))
            return new_version

    def rollback(self, skill_name: str, version: int) -> bool:
        """回滚到指定版本。"""
        with self._lock:
            versions = self._versions.get(skill_name, [])
            target = next((v for v in versions if v.version == version), None)
            if not target:
                return False
            self._registry.register(target.skill)
            return True

    def list_versions(self, skill_name: str) -> list[dict]:
        with self._lock:
            versions = self._versions.get(skill_name, [])
            return [{"version": v.version, "created_at": v.created_at, "changelog": v.changelog} for v in versions]


_skill_version_manager: Optional[SkillVersionManager] = None
_skill_version_lock = threading.Lock()


def get_skill_version_manager() -> SkillVersionManager:
    global _skill_version_manager
    if _skill_version_manager is None:
        with _skill_version_lock:
            if _skill_version_manager is None:
                _skill_version_manager = SkillVersionManager(get_skill_registry())
    return _skill_version_manager


# ---- 技能市场雏形：从目录动态加载技能定义 ----
def load_skills_from_directory(skills_dir: str) -> int:
    """从目录加载 YAML/JSON 技能定义文件。"""
    import os
    import yaml
    count = 0
    registry = get_skill_registry()
    for fname in os.listdir(skills_dir):
        if not (fname.endswith(".yaml") or fname.endswith(".yml") or fname.endswith(".json")):
            continue
        path = os.path.join(skills_dir, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                if fname.endswith(".json"):
                    data = json.load(f)
                else:
                    data = yaml.safe_load(f)
            if not isinstance(data, dict):
                continue
            skill = Skill(
                name=data.get("name", fname[:-5]),
                description=data.get("description", ""),
                steps=[SkillStep(**s) for s in data.get("steps", [])],
                params=[SkillParam(**p) for p in data.get("params", [])],
                timeout=data.get("timeout", 120.0),
                enabled=data.get("enabled", True),
            )
            registry.register(skill)
            count += 1
        except Exception as e:
            logger.warning(f"加载技能 {fname} 失败: {e}")
    return count


def get_skill_registry() -> SkillRegistry:
    global _skill_registry
    if _skill_registry is None:
        with _skill_lock:
            if _skill_registry is None:
                _skill_registry = SkillRegistry()
                register_builtin_skills(_skill_registry)
    return _skill_registry
