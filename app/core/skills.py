"""Skill 注册器:把多个工具链式组合成一个"技能",供 Agent 在需要时直接调用。

与单工具的区别:
- Skill 是一次高级语义操作(如"部署到生产环境"),内部可能涉及多个工具
- Skill 有独立的描述,告诉 LLM 何时该用
- Skill 可以携带预置参数(如默认目标环境)
- Skill 支持参数覆盖(用户可指定不同参数)
- Skill 支持自动执行:按步骤依次调用工具,处理错误和条件分支
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field, create_model
from pydantic.fields import FieldInfo

logger = logging.getLogger(__name__)


@dataclass
class SkillParam:
    name: str
    type_: str
    description: str
    default: Any = None
    required: bool = False


@dataclass
class SkillStep:
    """单工具步骤描述。"""
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    label: str = ""  # 可选标签,用于结果汇总


@dataclass
class Skill:
    name: str
    description: str
    steps: list[SkillStep]
    params: list[SkillParam] = field(default_factory=list)
    timeout: float = 120.0
    enabled: bool = True

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
    """技能执行引擎:按步骤依次调用工具,支持错误处理和条件跳过。

    Usage:
        executor = SkillExecutor(tool_executor)
        result = await executor.execute(skill, params={"environment": "staging"})
    """

    def __init__(self, tool_executor=None):
        self._tool_executor = tool_executor

    async def execute(
        self,
        skill: Skill,
        params: Optional[dict] = None,
        tool_executor=None,
    ) -> SkillResult:
        """执行一个技能的所有步骤。"""
        executor = tool_executor or self._tool_executor
        if executor is None:
            return SkillResult(
                skill_name=skill.name, success=False, steps=[],
                merged_output="", error="No tool executor available",
            )

        t0 = time.time()
        merged_args = self._merge_params(skill, params or {})
        step_results: list[StepResult] = []
        stop_on_error = merged_args.pop("_stop_on_error", True)

        for i, step in enumerate(skill.steps):
            args = self._resolve_args(step.args, merged_args)
            label = step.label or step.tool
            st0 = time.time()

            try:
                import json as _json
                result_str = await executor.execute_tool(
                    step.tool, _json.dumps(args, ensure_ascii=False),
                )
                # 工具返回错误 JSON(未抛异常)也应判为失败,与 ToolExecutor.is_tool_error 口径一致
                if _is_tool_error(result_str, executor):
                    err_text = _extract_error(result_str)
                    step_results.append(StepResult(
                        step_index=i, tool=step.tool, label=label,
                        success=False, result=result_str, error=err_text,
                        duration=time.time() - st0,
                    ))
                    if stop_on_error:
                        break
                    continue
                step_results.append(StepResult(
                    step_index=i, tool=step.tool, label=label,
                    success=True, result=result_str,
                    duration=time.time() - st0,
                ))
            except Exception as exc:
                logger.warning(
                    "Skill %s step %d (%s) failed: %s",
                    skill.name, i, step.tool, exc,
                )
                step_results.append(StepResult(
                    step_index=i, tool=step.tool, label=label,
                    success=False, result="", error=str(exc),
                    duration=time.time() - st0,
                ))
                if stop_on_error:
                    break

        all_ok = all(s.success for s in step_results)
        outputs = [f"[{s.label}] {s.result[:300]}" for s in step_results if s.success]
        merged = "\n\n".join(outputs) if outputs else "No successful steps."
        error_msg = None
        if not all_ok:
            failed = [s for s in step_results if not s.success]
            error_msg = "; ".join(f"{s.label}: {s.error}" for s in failed)

        return SkillResult(
            skill_name=skill.name, success=all_ok,
            steps=step_results, merged_output=merged,
            duration=time.time() - t0, error=error_msg,
        )

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


def get_skill_registry() -> SkillRegistry:
    global _skill_registry
    if _skill_registry is None:
        with _skill_lock:
            if _skill_registry is None:
                _skill_registry = SkillRegistry()
                register_builtin_skills(_skill_registry)
    return _skill_registry
