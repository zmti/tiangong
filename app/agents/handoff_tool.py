"""角色委派/交接工具:主 Agent 把任务交给某个角色的子智能体,只拿回结果摘要。

与 delegation_tool 的关系:
- delegate_task:通用子智能体(自拟指令/白名单)
- handoff_to_role:按团队角色(researcher/coder/...)委派,系统提示与工具子集由角色决定
- list_roles:查看团队里有哪些可用角色
"""
from __future__ import annotations

import logging
from typing import Optional

from pydantic import Field

from app.agents.roles import ensure_default_roles, get_role_registry
from app.tools.registry import tool

logger = logging.getLogger(__name__)


def get_team():
    ensure_default_roles()
    return get_role_registry().team


@tool(
    "list_roles",
    "列出团队中可委派的角色(每个角色是具备独立系统提示与工具集的专业子智能体)",
    timeout=10,
)
def list_roles() -> dict:
    """列出可用角色。"""
    team = get_team()
    return {"roles": team.summary()}


@tool(
    "handoff_to_role",
    "把子任务委派给团队中某个角色(researcher/coder/code_reviewer/analyst/writer/security_reviewer)执行。"
    "适合需要专业分工的任务:调研、写代码、审查代码、数据分析、写作等。"
    "角色子智能体使用独立上下文,只回传结果摘要。失败时请换一种方式处理。",
    timeout=600,
)
async def handoff_to_role(
    role: str = Field(description="角色名,用 list_roles 查看可用角色"),
    instruction: str = Field(description="给角色的完整任务指令:目标、约束、期望输出格式"),
    context: str = Field("", description="角色需要的背景资料(文件路径、代码片段、数据等)"),
    expect: str = Field(
        "",
        description="输出契约:期望角色返回的 JSON Schema(字符串)。非空时完成后用 LLM 抽取为"
        "符合该 Schema 的结构化结果;不满足则返回 status=contract_failed",
    ),
) -> dict:
    """把任务交接给指定角色执行。"""
    import datetime

    from app.config import settings as _cfg
    from app.core.agent import Agent
    from app.core.llm import LLMClient
    from app.core.telemetry import _current_run_id
    from app.tools.delegation_tool import (
        _delegate_depth as _depth_var,
        enforce_output_contract,
        get_delegate_context,
    )

    team = get_team()
    role_def = team.get(role)
    if role_def is None:
        names = "、".join(team.names())
        return {"status": "error", "error": f"未知角色: {role}。可用角色: {names}"}

    if _delegate_depth.get() >= 2:
        return {"status": "error", "error": "委派深度已达上限,不允许继续嵌套"}

    instruction = (instruction or "").strip()
    if not instruction:
        return {"status": "error", "error": "instruction 不能为空"}

    # 继承父 Agent 的模型(可能已路由),角色可指定覆盖
    parent = get_delegate_context()
    try:
        if role_def.model:
            provider, _, model = role_def.model.partition(":")
            llm = LLMClient(provider or None, model or None)
        elif parent and parent.provider:
            llm = LLMClient(parent.provider, parent.model or None)
        else:
            llm = LLMClient()
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": f"初始化角色模型失败: {e}"}

    # 角色子智能体与主 Agent 共用 checkpoint(同一 run_id 命名空间隔离于 session)
    sub = Agent(
        llm=llm,
        max_steps=role_def.max_steps,
        system_prompt=role_def.system_prompt,
        session_id=parent.session_id if parent else "",
        temperature=0.4,
        project_path=getattr(parent, "project_path", "") or "",
    )
    if role_def.tools:
        sub._active_tools = list(role_def.tools)
        sub._schemas_cache = None
    # 角色子智能体同样收紧上下文预算,强制独立聚焦
    sub.context_budget = _cfg.agent_subagent_context_budget
    sub.token_budget = _cfg.agent_subagent_token_budget

    prompt = instruction
    if context and context.strip():
        prompt += f"\n\n【背景资料】\n{context}"

    token = _depth_var.set(_depth_var.get() + 1)
    try:
        t0 = datetime.datetime.now()
        result = await sub.run(prompt, history=[])
        duration = round((datetime.datetime.now() - t0).total_seconds(), 2)
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": f"角色执行异常: {e}"}
    finally:
        _depth_var.reset(token)

    # 持久化子代理状态(断点续跑支持)
    tool_steps = [s for s in result.steps if s.get("type") == "tool"]
    try:
        from app.core.persistent_agent import get_agent_store
        store = get_agent_store()
        agent_state = store.create(
            parent_run_id=_current_run_id() or "",
            session_id=parent.session_id if parent else "",
            role=role,
            task=instruction,
            max_steps=role_def.max_steps,
            provider=llm.info.get("provider", ""),
            model=llm.info.get("model", ""),
        )
        agent_state.messages = [
            {"role": "user", "content": instruction},
            {"role": "assistant", "content": result.answer or ""},
        ]
        agent_state.tool_calls = [{"name": s.get("name"), "args": s.get("arguments", {})} for s in tool_steps]
        agent_state.intermediate_results = []
        for ts in tool_steps:
            result_str = ts.get("result", "") or ts.get("arguments", {})
            agent_state.intermediate_results.append(str(result_str)[:500])
        agent_state.current_step = len(tool_steps)
        if result.answer:
            agent_state.status = "done"
            agent_state.result = result.answer
        else:
            agent_state.status = "failed"
            agent_state.error = "无回答结果"
        store._persist(agent_state)
    except Exception as e:
        logger.warning("角色子智能体状态持久化失败: %s", e)

    base = {
        "status": "ok",
        "role": role,
        "answer": result.answer,
        "tool_calls": len(tool_steps),
        "model": result.model,
        "duration_s": duration,
        "note": "角色子智能体在独立上下文中执行,以上为结果摘要",
    }
    # 输出契约:抽取符合 Schema 的结构化结果;不满足则标记失败由主智能体处理
    if expect:
        ok, data, err = await enforce_output_contract(llm, result.answer, expect)
        if not ok:
            return {
                "status": "contract_failed",
                "role": role,
                "answer": result.answer,
                "error": err,
                "duration_s": duration,
                "note": "角色已执行但结果不满足输出契约,请重试或调整指令",
            }
        base["answer_structured"] = data
    return base
