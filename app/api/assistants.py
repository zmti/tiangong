"""OpenAI Assistants API 兼容层:threads / messages / runs / tool_outputs。

对齐 OpenAI Assistants API v2:
- POST /v1/assistants       — 创建助手
- GET  /v1/assistants       — 列表
- GET  /v1/assistants/{id}  — 查看
- POST /v1/threads          — 创建对话线程
- POST /v1/threads/{id}/messages  — 发送消息
- POST /v1/threads/{id}/runs      — 执行 run
- GET  /v1/threads/{id}/runs      — 列表 runs
- GET  /v1/threads/{id}/runs/{run_id} — 查看 run
- POST /v1/threads/{id}/runs/{run_id}/tool_outputs — 提交工具结果
- GET  /v1/threads/{id}/messages  — 获取消息列表

所有操作底层复用现有的 /v1/chat 和 /v1/tasks 接口。
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.config import settings
from app.core.storage_backend import get_storage_backend
from app.core.agent import Agent
from app.core.llm import LLMClient

router = APIRouter(prefix="/v1", tags=["assistants"])

# ---- 存储 ----
_db = get_storage_backend()
_db.init_db({
    "assistants": [
        {"name": "id", "type": "TEXT", "constraints": "PRIMARY KEY"},
        {"name": "name", "type": "TEXT", "constraints": "NOT NULL"},
        {"name": "description", "type": "TEXT", "constraints": ""},
        {"name": "model", "type": "TEXT", "constraints": "NOT NULL DEFAULT 'deepseek/deepseek-chat'"},
        {"name": "system_prompt", "type": "TEXT", "constraints": ""},
        {"name": "tools", "type": "TEXT", "constraints": "DEFAULT '[]'"},
        {"name": "metadata", "type": "TEXT", "constraints": "DEFAULT '{}'"},
        {"name": "created_at", "type": "REAL", "constraints": "NOT NULL"},
    ],
    "threads": [
        {"name": "id", "type": "TEXT", "constraints": "PRIMARY KEY"},
        {"name": "assistant_id", "type": "TEXT", "constraints": "NOT NULL"},
        {"name": "metadata", "type": "TEXT", "constraints": "DEFAULT '{}'"},
        {"name": "created_at", "type": "REAL", "constraints": "NOT NULL"},
    ],
    "thread_messages": [
        {"name": "id", "type": "TEXT", "constraints": "PRIMARY KEY"},
        {"name": "thread_id", "type": "TEXT", "constraints": "NOT NULL"},
        {"name": "role", "type": "TEXT", "constraints": "NOT NULL"},
        {"name": "content", "type": "TEXT", "constraints": "NOT NULL"},
        {"name": "attachments", "type": "TEXT", "constraints": "DEFAULT '[]'"},
        {"name": "metadata", "type": "TEXT", "constraints": "DEFAULT '{}'"},
        {"name": "created_at", "type": "REAL", "constraints": "NOT NULL"},
    ],
    "runs": [
        {"name": "id", "type": "TEXT", "constraints": "PRIMARY KEY"},
        {"name": "thread_id", "type": "TEXT", "constraints": "NOT NULL"},
        {"name": "assistant_id", "type": "TEXT", "constraints": "NOT NULL"},
        {"name": "status", "type": "TEXT", "constraints": "NOT NULL DEFAULT 'pending'"},
        {"name": "mode", "type": "TEXT", "constraints": "DEFAULT 'auto'"},
        {"name": "max_completion_tokens", "type": "INTEGER", "constraints": "DEFAULT 0"},
        {"name": "max_prompt_tokens", "type": "INTEGER", "constraints": "DEFAULT 0"},
        {"name": "truncation_strategy", "type": "TEXT", "constraints": "DEFAULT 'auto'"},
        {"name": "tool_choice", "type": "TEXT", "constraints": "DEFAULT 'auto'"},
        {"name": "parallel_tool_calls", "type": "INTEGER", "constraints": "DEFAULT 1"},
        {"name": "required_action", "type": "TEXT", "constraints": ""},
        {"name": "last_error", "type": "TEXT", "constraints": ""},
        {"name": "started_at", "type": "REAL", "constraints": ""},
        {"name": "completed_at", "type": "REAL", "constraints": ""},
        {"name": "created_at", "type": "REAL", "constraints": "NOT NULL"},
        {"name": "metadata", "type": "TEXT", "constraints": "DEFAULT '{}'"},
    ],
})


# ---- Request/Response Models ----

class CreateAssistantRequest(BaseModel):
    model: str = "deepseek/deepseek-chat"
    name: Optional[str] = Field(None, description="助手名称")
    description: Optional[str] = Field(None)
    system_prompt: Optional[str] = Field(None, description="自定义 system prompt")
    tools: list[dict] = Field([], description="工具列表(JSON Schema)")
    metadata: dict = Field(default_factory=dict)


class CreateThreadRequest(BaseModel):
    messages: list[dict] = Field([], description="初始消息列表")
    metadata: dict = Field(default_factory=dict)


class ThreadMessageRequest(BaseModel):
    role: str = Field(..., description="user 或 assistant")
    content: str = Field(...)
    attachments: list[dict] = Field([], description="附件列表")
    metadata: dict = Field(default_factory=dict)


class RunRequest(BaseModel):
    model: Optional[str] = Field(None, description="覆盖助手默认模型")
    instructions: Optional[str] = Field(None, description="本次 run 的额外指令")
    tools: Optional[list[dict]] = Field(None, description="覆盖助手默认工具")
    max_completion_tokens: int = Field(0, description="最大输出 token")
    max_prompt_tokens: int = Field(0, description="最大输入 token")
    truncation_strategy: str = Field("auto", description="auto / last_messages(N)")
    tool_choice: str = Field("auto", description="auto / none / required")
    parallel_tool_calls: bool = Field(True, description="是否允许并行工具调用")
    metadata: dict = Field(default_factory=dict)


class ToolOutputRequest(BaseModel):
    tool_calls: list[dict] = Field(
        ..., description='[{"tool_call_id": "...", "output": "..."}]'
    )


# ---- Assistant CRUD ----

@router.post("/assistants")
async def create_assistant(req: CreateAssistantRequest) -> dict:
    aid = f"asst_{uuid.uuid4().hex[:16]}"
    tools_json = json.dumps(req.tools, ensure_ascii=False) if req.tools else "[]"
    now = time.time()
    _db.insert("assistants", {
        "id": aid,
        "name": req.name or "Assistant",
        "description": req.description or "",
        "model": req.model,
        "system_prompt": req.system_prompt or "",
        "tools": tools_json,
        "metadata": json.dumps(req.metadata, ensure_ascii=False),
        "created_at": now,
    })
    return {"id": aid, "object": "assistant", "created_at": now, **req.model_dump()}


@router.get("/assistants")
async def list_assistants(limit: int = 20, order: str = "desc") -> dict:
    rows = _db.select("assistants", "1=1", order_by="created_at DESC", limit=limit)
    items = []
    for r in rows:
        tools = json.loads(r.get("tools", "[]") or "[]")
        items.append({
            "id": r["id"],
            "object": "assistant",
            "name": r["name"],
            "description": r.get("description", ""),
            "model": r["model"],
            "tools": tools,
            "created_at": r["created_at"],
        })
    return {"object": "list", "data": items, "has_more": len(items) == limit}


@router.get("/assistants/{assistant_id}")
async def get_assistant(assistant_id: str) -> dict:
    row = _db.select("assistants", "id = ?", (assistant_id,), limit=1)
    if not row:
        raise HTTPException(status_code=404, detail=f"Asssistant not found: {assistant_id}")
    r = row[0]
    tools = json.loads(r.get("tools", "[]") or "[]")
    return {
        "id": r["id"],
        "object": "assistant",
        "name": r["name"],
        "description": r.get("description", ""),
        "model": r["model"],
        "tools": tools,
        "created_at": r["created_at"],
    }


# ---- Thread CRUD ----

@router.post("/threads")
async def create_thread(req: CreateThreadRequest) -> dict:
    tid = f"thread_{uuid.uuid4().hex[:16]}"
    now = time.time()
    _db.insert("threads", {"id": tid, "assistant_id": "", "metadata": json.dumps(req.metadata, ensure_ascii=False), "created_at": now})
    # 初始化消息
    for msg in req.messages:
        await _create_thread_message(tid, msg)
    return {"id": tid, "object": "thread", "created_at": now}


async def _create_thread_message(thread_id: str, msg_data: dict) -> None:
    mid = f"msg_{uuid.uuid4().hex[:12]}"
    now = time.time()
    _db.insert("thread_messages", {
        "id": mid,
        "thread_id": thread_id,
        "role": msg_data.get("role", "user"),
        "content": msg_data.get("content", ""),
        "attachments": json.dumps(msg_data.get("attachments", []), ensure_ascii=False),
        "metadata": json.dumps(msg_data.get("metadata", {}), ensure_ascii=False),
        "created_at": now,
    })


@router.post("/threads/{thread_id}/messages")
async def create_message(thread_id: str, req: ThreadMessageRequest) -> dict:
    # 验证 thread 存在
    row = _db.select("threads", "id = ?", (thread_id,), limit=1)
    if not row:
        raise HTTPException(status_code=404, detail=f"Thread not found: {thread_id}")
    mid = f"msg_{uuid.uuid4().hex[:12]}"
    now = time.time()
    _db.insert("thread_messages", {
        "id": mid,
        "thread_id": thread_id,
        "role": req.role,
        "content": req.content,
        "attachments": json.dumps(req.attachments, ensure_ascii=False),
        "metadata": json.dumps(req.metadata, ensure_ascii=False),
        "created_at": now,
    })
    return {
        "id": mid, "object": "message", "role": req.role,
        "content": [{"type": "text", "text": req.content}],
        "thread_id": thread_id, "created_at": now,
    }


@router.get("/threads/{thread_id}/messages")
async def list_messages(thread_id: str, limit: int = 20, order: str = "desc") -> dict:
    rows = _db.select("thread_messages", "thread_id = ?", (thread_id,),
                      order_by="created_at DESC" if order == "desc" else "created_at ASC",
                      limit=limit)
    items = []
    for r in rows:
        content = r.get("content", "")
        attachments = json.loads(r.get("attachments", "[]") or "[]")
        parts = [{"type": "text", "text": content}]
        if attachments:
            parts.append({"type": "attachments", "attachments": attachments})
        items.append({
            "id": r["id"], "object": "message", "role": r["role"],
            "content": parts, "thread_id": r["thread_id"],
            "created_at": r["created_at"],
        })
    return {"object": "list", "data": items, "has_more": len(items) == limit}


# ---- Run Execution ----

@router.post("/threads/{thread_id}/runs")
async def create_run(thread_id: str, req: RunRequest) -> dict:
    """创建 run:从 thread 历史构造 messages,触发 Agent 执行。"""
    # 验证 thread 存在
    row = _db.select("threads", "id = ?", (thread_id,), limit=1)
    if not row:
        raise HTTPException(status_code=404, detail=f"Thread not found: {thread_id}")
    thread = row[0]
    assistant_id = thread.get("assistant_id", "")

    # 获取助手配置
    asst_row = _db.select("assistants", "id = ?", (assistant_id,), limit=1) if assistant_id else []
    asst = asst_row[0] if asst_row else {}

    # 获取 thread 消息历史
    msgs = _db.select("thread_messages", "thread_id = ?", (thread_id,),
                      order_by="created_at ASC")
    history = []
    for m in msgs:
        history.append({"role": m["role"], "content": m["content"]})

    # 取最后一条用户消息作为当前输入
    user_msgs = [m for m in history if m["role"] == "user"]
    if not user_msgs:
        raise HTTPException(status_code=400, detail="Thread 中没有用户消息")
    user_input = user_msgs[-1]["content"]
    # 去掉最后一条用户消息(会被 run 重新提交)
    history = [m for m in history if not (m["role"] == "user" and m["content"] == user_input)] or history[:-1] if history else []

    # 构造 run 记录
    run_id = f"run_{uuid.uuid4().hex[:16]}"
    now = time.time()
    _db.insert("runs", {
        "id": run_id,
        "thread_id": thread_id,
        "assistant_id": assistant_id,
        "status": "pending",
        "mode": "auto",
        "max_completion_tokens": req.max_completion_tokens,
        "max_prompt_tokens": req.max_prompt_tokens,
        "truncation_strategy": req.truncation_strategy,
        "tool_choice": req.tool_choice,
        "parallel_tool_calls": 1 if req.parallel_tool_calls else 0,
        "required_action": "",
        "last_error": "",
        "started_at": 0.0,
        "completed_at": 0.0,
        "created_at": now,
        "metadata": json.dumps(req.metadata, ensure_ascii=False),
    })

    # 异步执行
    import asyncio
    asyncio.create_task(_execute_run(run_id, thread_id, assistant_id, user_input, history, req, asst))

    return {
        "id": run_id,
        "object": "run",
        "thread_id": thread_id,
        "assistant_id": assistant_id,
        "status": "pending",
        "created_at": now,
        "required_action": None,
        "last_error": None,
        "started_at": None,
        "completed_at": None,
    }


async def _execute_run(
    run_id: str, thread_id: str, assistant_id: str,
    user_input: str, history: list[dict],
    req: RunRequest, asst: dict,
) -> None:
    """后台执行 run。"""
    from app.core.agent import Agent
    from app.core.llm import LLMClient

    # 解析模型
    model_str = req.model or asst.get("model", "deepseek/deepseek-chat")
    provider, _, model = model_str.partition(":")
    try:
        llm = LLMClient(provider or None, model or None)
    except Exception as e:
        _db.update("runs", {"status": "failed", "last_error": str(e)}, "id = ?", (run_id,))
        return

    # 构造 system prompt
    sys_prompt = asst.get("system_prompt", "") or ""
    if req.instructions:
        sys_prompt = (sys_prompt + "\n\n" + req.instructions).strip()

    agent = Agent(
        llm=llm,
        system_prompt=sys_prompt,
        session_id=thread_id,
        mode="auto",
    )
    # 覆盖 max_output_tokens
    if req.max_completion_tokens > 0:
        agent.max_output_tokens = req.max_completion_tokens

    try:
        # 更新 run 状态
        _db.update("runs", {"status": "in_progress", "started_at": time.time()}, "id = ?", (run_id,))

        result = await agent.run(user_input, history=history)

        # 保存 assistant 回复为消息
        mid = f"msg_{uuid.uuid4().hex[:12]}"
        now = time.time()
        _db.insert("thread_messages", {
            "id": mid, "thread_id": thread_id,
            "role": "assistant", "content": result.answer,
            "attachments": "[]", "metadata": json.dumps({"run_id": run_id}),
            "created_at": now,
        })

        _db.update("runs", {
            "status": "completed",
            "completed_at": time.time(),
        }, "id = ?", (run_id,))

    except Exception as e:  # noqa: BLE001
        _db.update("runs", {"status": "failed", "last_error": str(e)[:500]}, "id = ?", (run_id,))


@router.get("/threads/{thread_id}/runs")
async def list_runs(thread_id: str, limit: int = 20) -> dict:
    rows = _db.select("runs", "thread_id = ?", (thread_id,),
                      order_by="created_at DESC", limit=limit)
    items = []
    for r in rows:
        items.append({
            "id": r["id"], "object": "run", "thread_id": r["thread_id"],
            "assistant_id": r.get("assistant_id", ""),
            "status": r["status"],
            "created_at": r["created_at"],
            "started_at": r.get("started_at") or None,
            "completed_at": r.get("completed_at") or None,
            "last_error": r.get("last_error") or None,
            "required_action": None,
        })
    return {"object": "list", "data": items, "has_more": len(items) == limit}


@router.get("/threads/{thread_id}/runs/{run_id}")
async def get_run(thread_id: str, run_id: str) -> dict:
    row = _db.select("runs", "id = ? AND thread_id = ?", (run_id, thread_id), limit=1)
    if not row:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
    r = row[0]
    return {
        "id": r["id"], "object": "run", "thread_id": r["thread_id"],
        "assistant_id": r.get("assistant_id", ""),
        "status": r["status"],
        "created_at": r["created_at"],
        "started_at": r.get("started_at") or None,
        "completed_at": r.get("completed_at") or None,
        "last_error": r.get("last_error") or None,
        "required_action": None,
        "model": r.get("model", ""),
    }


@router.post("/threads/{thread_id}/runs/{run_id}/tool_outputs")
async def submit_tool_outputs(thread_id: str, run_id: str, req: ToolOutputRequest) -> dict:
    """提交工具结果(当前为占位实现,未来支持 run 级工具中断恢复)。"""
    # 验证 run 存在且处于 required_action 状态
    row = _db.select("runs", "id = ? AND thread_id = ?", (run_id, thread_id), limit=1)
    if not row:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
    run = row[0]
    if run["status"] != "completed":
        raise HTTPException(status_code=400, detail=f"Run 状态为 {run['status']},无法提交工具结果")

    # 当前实现:记录 tool_outputs 到 thread 消息历史
    for tc in req.tool_calls:
        tc_id = tc.get("tool_call_id", "")
        output = tc.get("output", "")
        mid = f"tool_{uuid.uuid4().hex[:8]}"
        now = time.time()
        _db.insert("thread_messages", {
            "id": mid, "thread_id": thread_id,
            "role": "tool", "content": output,
            "attachments": json.dumps({"tool_call_id": tc_id}),
            "metadata": json.dumps({"run_id": run_id}),
            "created_at": now,
        })

    return {"id": run_id, "object": "run", "status": "completed"}
