"""子智能体委派工具:把子任务交给一个独立上下文的子 Agent 执行,只回传结果摘要。

设计(对齐 Claude Code Subagents / Manus 子代理):
- 独立消息上下文:子 Agent 使用全新 messages(仅系统提示 + 指令),不与主会话共享历史
- 独立工具子集:可通过 tools 参数限制子 Agent 可用的工具
- 只返回摘要:完整执行轨迹不回流主上下文,避免上下文膨胀
- 递归深度限制:防止无限嵌套子智能体
"""
from __future__ import annotations

import asyncio
import contextvars
import datetime
import json
from dataclasses import dataclass
from typing import Optional

from pydantic import Field

from app.config import settings
from app.tools.registry import tool

MAX_DELEGATE_DEPTH = 2


@dataclass
class DelegateContext:
    """父 Agent 的上下文快照,供子智能体继承模型与会话。"""

    provider: str = ""
    model: str = ""
    session_id: str = ""
    active_tools: Optional[list[str]] = None


# 父 Agent 在执行工具前注入;子智能体创建时读取,用于继承模型/会话
_delegate_parent: contextvars.ContextVar[Optional[DelegateContext]] = contextvars.ContextVar(
    "delegate_parent", default=None
)
# 委派深度(0 = 主 Agent)
_delegate_depth: contextvars.ContextVar[int] = contextvars.ContextVar("delegate_depth", default=0)


def set_delegate_context(
    llm, session_id: str, active_tools: Optional[list[str]] = None
):
    """在执行工具前把父 Agent 的模型/会话信息注入 contextvar。返回恢复 token。"""
    info = getattr(llm, "info", None) or {}
    ctx = DelegateContext(
        provider=info.get("provider", ""),
        model=info.get("model", ""),
        session_id=session_id or "",
        active_tools=active_tools,
    )
    return _delegate_parent.set(ctx)


def get_delegate_context() -> Optional[DelegateContext]:
    """读取父 Agent 上下文(供工具内部使用)。"""
    return _delegate_parent.get()


def current_depth() -> int:
    """当前委派深度。"""
    return _delegate_depth.get()


async def enforce_output_contract(llm, answer: str, expect: str) -> tuple[bool, Optional[dict], Optional[str]]:
    """按输出契约(JSON Schema)校验并抽取子智能体结果。

    返回 (ok, data, error):ok=False 且 error 非空表示结果不满足契约,
    主智能体可据此重新委派或调整策略。
    """
    if expect is None or expect == "" or expect == {}:
        return True, None, None
    try:
        schema = json.loads(expect) if isinstance(expect, str) else expect
    except json.JSONDecodeError:
        return False, None, f"expect 不是合法 JSON Schema: {expect[:100]}"
    if not isinstance(schema, dict):
        return False, None, "expect 必须是 JSON Schema 对象"
    try:
        data = await llm.chat_json_schema(
            [
                {"role": "system", "content": "从子智能体的汇报中抽取结构化结果,严格按给定 JSON Schema 输出。"},
                {"role": "user", "content": f"子智能体汇报:\n{answer}\n\n请按 Schema 输出结构化结果。"},
            ],
            schema,
            temperature=0.2,
            max_retries=1,
        )
    except Exception as e:  # noqa: BLE001
        return False, None, f"结果不满足输出契约: {e}"
    return True, data, None


@tool(
    "delegate_task",
    "把子任务委派给一个使用独立上下文的子智能体执行。"
    "适合:需要多步推理/多次工具调用的子问题(如单独调研一个话题、单独重写一个模块、单独跑一遍测试排查问题)。"
    "子智能体看不到当前会话历史,只能根据你提供的 instruction/context 工作;完成后只返回结果摘要与工具使用统计,"
    "不会把内部过程塞进你的上下文。注意:不要委派一句话就能答完的简单问题;子智能体也可能失败或触达步数上限,失败时请换一种方式处理",
    timeout=600,
)
async def delegate_task(
    instruction: str = Field(
        description="给子智能体的完整任务指令。要具体:说明目标、约束、可用工具、期望输出格式。"
        "子智能体看不到当前对话,所有必要信息都必须写在这里"
    ),
    context: str = Field(
        "",
        description="子智能体需要的背景资料(文件路径、代码片段、数据等)。留空则不注入",
    ),
    tools: str = Field(
        "",
        description="允许子智能体使用的工具白名单,逗号分隔(如 'run_terminal,read_document,edit_file')。留空=全量工具",
    ),
    max_steps: int = Field(8, description="子智能体最大执行步数,默认 8,上限 15"),
    expect: str = Field(
        "",
        description="输出契约:期望子智能体返回的 JSON Schema(字符串)。非空时子智能体完成后用 LLM "
        "把其汇报抽取为符合该 Schema 的结构化结果;不满足则返回 status=contract_failed",
    ),
) -> dict:
    """委派子任务给独立上下文的子智能体。"""
    from app.core.agent import Agent
    from app.core.llm import LLMClient

    if _delegate_depth.get() >= MAX_DELEGATE_DEPTH:
        return {
            "status": "error",
            "error": f"委派深度已达上限({MAX_DELEGATE_DEPTH}),不允许继续嵌套子智能体",
        }

    instruction = (instruction or "").strip()
    if not instruction:
        return {"status": "error", "error": "instruction 不能为空"}

    parent = _delegate_parent.get()

    # 继承父 Agent 的模型(可能已路由),fallback 到默认配置
    llm = None
    if parent and parent.provider:
        try:
            llm = LLMClient(parent.provider, parent.model or None)
        except Exception:  # noqa: BLE001
            llm = None
    if llm is None:
        try:
            llm = LLMClient()
        except Exception as e:  # noqa: BLE001
            return {"status": "error", "error": f"初始化子智能体模型失败: {e}"}

    max_steps = max(1, min(max_steps or 8, 15))

    sub = Agent(
        llm=llm,
        max_steps=max_steps,
        system_prompt=(
            "你是一个子智能体(subagent),由主智能体委派完成一个明确的任务。"
            "你无法看到主会话的历史,只能依据任务指令与提供的背景工作。"
            "请独立完成任务,不要反问主智能体。完成或尽力完成后,用简洁的中文汇报结果与结论。"
        ),
        session_id=parent.session_id if parent else "",
        temperature=0.4,
    )
    # 工具子集裁剪:指定了 tools 就只用这些;未指定则用全量工具
    if tools:
        allowed = [t.strip() for t in tools.split(",") if t.strip()]
        sub._active_tools = allowed
        sub._schemas_cache = None
    # 子任务独立上下文强制化:收紧子 Agent 的上下文预算,防止回流父级历史
    sub.context_budget = settings.agent_subagent_context_budget
    sub.token_budget = settings.agent_subagent_token_budget

    prompt = instruction
    if context and context.strip():
        prompt += f"\n\n【背景资料】\n{context}"

    token = _delegate_depth.set(_delegate_depth.get() + 1)
    try:
        t0 = datetime.datetime.now()
        result = await sub.run(prompt, history=[])
        duration = round((datetime.datetime.now() - t0).total_seconds(), 2)
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": f"子智能体执行异常: {e}"}
    finally:
        _delegate_depth.reset(token)

    tool_steps = [s for s in result.steps if s.get("type") == "tool"]
    tool_names = [s.get("tool", "") for s in tool_steps]
    base = {
        "status": "ok",
        "answer": result.answer,
        "tool_calls": len(tool_steps),
        "tools_used": list(dict.fromkeys(tool_names)),
        "model": result.model,
        "duration_s": duration,
        "max_steps": max_steps,
        "note": "子智能体在独立上下文中执行,以上为结果摘要;完整过程不回流主会话",
    }
    # 输出契约:抽取符合 Schema 的结构化结果;不满足则标记失败由主智能体处理
    if expect:
        ok, data, err = await enforce_output_contract(llm, result.answer, expect)
        if not ok:
            return {
                "status": "contract_failed",
                "answer": result.answer,
                "error": err,
                "duration_s": duration,
                "note": "子智能体已执行但结果不满足输出契约,请重试或调整指令",
            }
        base["answer_structured"] = data
    return base


async def _run_subagent_task(
    instruction: str,
    context: str,
    tools: str,
    max_steps: int,
    parent: Optional[DelegateContext],
) -> dict:
    """并行委派的单个子任务执行体(与 delegate_task 内部逻辑一致,但自建 context)。"""
    from app.core.agent import Agent
    from app.core.llm import LLMClient

    llm = None
    if parent and parent.provider:
        try:
            llm = LLMClient(parent.provider, parent.model or None)
        except Exception:  # noqa: BLE001
            llm = None
    if llm is None:
        llm = LLMClient()

    sub = Agent(
        llm=llm,
        max_steps=max_steps,
        system_prompt=(
            "你是一个子智能体(subagent),由主智能体并行委派完成一个明确的任务。"
            "你无法看到主会话的历史,只能依据任务指令与提供的背景工作。"
            "请独立完成任务,不要反问主智能体。完成或尽力完成后,用简洁的中文汇报结果与结论。"
        ),
        session_id=parent.session_id if parent else "",
        temperature=0.4,
    )
    if tools:
        allowed = [t.strip() for t in tools.split(",") if t.strip()]
        sub._active_tools = allowed
        sub._schemas_cache = None
    sub.context_budget = settings.agent_subagent_context_budget
    sub.token_budget = settings.agent_subagent_token_budget

    prompt = instruction
    if context and context.strip():
        prompt += f"\n\n【背景资料】\n{context}"

    t0 = datetime.datetime.now()
    try:
        result = await sub.run(prompt, history=[])
        duration = round((datetime.datetime.now() - t0).total_seconds(), 2)
        tool_steps = [s for s in result.steps if s.get("type") == "tool"]
        return {
            "status": "ok",
            "instruction": instruction,
            "answer": result.answer,
            "tool_calls": len(tool_steps),
            "duration_s": duration,
            "note": "并行子智能体在独立上下文中执行,结果已合并",
        }
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "instruction": instruction, "error": f"子智能体执行异常: {e}"}


@tool(
    "delegate_parallel",
    "把多个相互独立的子任务并行委派给独立上下文的子智能体执行(map-reduce 式扇出)。"
    "所有任务同时运行,全部完成后返回合并结果。适合:需要同时调研多个话题、并行跑多组验证、"
    "把一个大任务拆成互不依赖的几块分头完成。注意:只用于互不依赖的任务,"
    "任务间有依赖请用 delegate_task 串行",
    timeout=900,
)
async def delegate_parallel(
    tasks: str = Field(
        description="子任务清单 JSON,格式 [{\"instruction\":\"...\",\"context\":\"...\","
        "\"tools\":\"...\",\"max_steps\":8}, ...]。instruction 必填;其余可省略。"
        "所有子任务必须互相独立"
    ),
    max_steps: int = Field(8, description="每个子智能体的最大执行步数,默认 8,上限 15"),
) -> dict:
    """并行扇出多个独立子任务,合并返回全部结果。"""
    if _delegate_depth.get() >= MAX_DELEGATE_DEPTH:
        return {"status": "error",
                "error": f"委派深度已达上限({MAX_DELEGATE_DEPTH}),不允许继续嵌套子智能体"}

    try:
        tasks_list = json.loads(tasks) if isinstance(tasks, str) else tasks
    except json.JSONDecodeError:
        return {"error": "tasks 参数必须是 JSON 数组"}
    if not isinstance(tasks_list, list) or not tasks_list:
        return {"error": "tasks 必须是至少一项的数组"}
    if len(tasks_list) > 10:
        return {"error": "tasks 一次最多 10 项(避免过度并发)"}

    parent = _delegate_parent.get()
    max_steps = max(1, min(max_steps or 8, 15))

    token = _delegate_depth.set(_delegate_depth.get() + 1)
    try:
        results = await asyncio.gather(*[
            _run_subagent_task(
                str(t.get("instruction", "")).strip(),
                str(t.get("context", "") or ""),
                str(t.get("tools", "") or ""),
                max(1, min(int(t.get("max_steps", max_steps) or max_steps), 15)),
                parent,
            )
            for t in tasks_list
        ])
    finally:
        _delegate_depth.reset(token)

    ok_count = sum(1 for r in results if r.get("status") == "ok")
    return {
        "status": "ok" if ok_count == len(results) else "partial",
        "total": len(results),
        "succeeded": ok_count,
        "failed": len(results) - ok_count,
        "results": results,
        "note": "全部子任务已并行执行完成,结果按提交顺序合并",
    }
