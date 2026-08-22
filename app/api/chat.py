"""对话接口:非流式 /v1/chat 与流式 SSE /v1/chat/stream。"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Body, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.agent import Agent
from app.core.llm import LLMClient, LLMError
from app.config import settings
from app.memory.session import get_session_store
from app.tools.registry import get_registry

router = APIRouter(prefix="/v1", tags=["chat"])


def _auth_required():
    """占位：认证依赖，当前桌面端不使用 API Key 验证。"""
    pass


def _store():
    """会话存储统一入口:按配置解析 Redis/SQLite,避免散落直连。"""
    try:  # noqa: PLW0129
        return get_session_store()
    except Exception:  # noqa: BLE001
        from app.memory.session import session_store

        return session_store


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, description="用户消息")
    session_id: str = Field("default", description="会话 ID")
    stream: bool = Field(False, description="是否流式返回")
    use_history: bool = Field(True, description="是否携带会话历史")
    provider: str | None = Field(None, description="指定模型 provider,默认用配置")
    model: str | None = Field(None, description="指定模型(留空=该 provider 默认模型)")
    approve_tools: list[str] = Field(default_factory=list, description="本次运行允许执行的危险工具(需开启 AGENT_TOOL_CONFIRM)")
    attachments: list[str] = Field(default_factory=list, description="本次消息携带的附件 ID(已上传到 /v1/attachments)")
    mode: str = Field("auto", description="协作模式: auto / single / review / ensemble / sequential / hierarchical / consensual")
    critic_provider: str = Field("", description="review/ensemble 模式评审模型 provider(留空=全局配置)")
    critic_model: str = Field("", description="review/ensemble 模式评审模型(留空=该 provider 默认)")
    models: list[str] = Field(default_factory=list, description="ensemble 模式参与模型列表(如 [\"deepseek:deepseek-chat\",\"qwen:qwen-plus\"],留空=全局配置)")
    max_output_tokens: int = Field(0, ge=0, description="输出 token 配额硬控制,0=不限制(超过即截断)")
    orch_subtasks: int = Field(0, ge=0, description="编排模式(层级/共识/顺序)的子任务数或候选执行体数提示,0=自动")
    orch_candidates: list[str] = Field(default_factory=list, description="编排模式候选执行体清单(执行模式),如 ['auto','review','ensemble'];空=默认")


def _with_attachments(req: ChatRequest, message: str) -> str:
    """把文本类附件注入到用户消息之前(纯文本注入由 _prepare_attachments 负责)。

    图片/音频已由 _prepare_attachments 转成多模态 parts,不在此注入文本。
    """
    return message


async def _prepare_attachments(req: ChatRequest, message: str) -> tuple[str, list[dict]]:
    """解析请求附件:文本注入、图片转 data URL part、音频走 ASR。

    返回 (注入后的用户文本, 多模态内容 parts)。
    """
    if not req.attachments:
        return message, []
    from app.memory.attachments import attachment_store
    from app.core.multimodal import build_image_url_part, image_to_data_url, is_image_mime

    injected: list[str] = []
    parts: list[dict] = []
    for att_id in req.attachments:
        got = attachment_store.get_bytes(req.session_id, att_id)
        if got is None:
            continue
        name, mime, content = got
        if is_image_mime(mime):
            data_url = image_to_data_url(content, mime)
            parts.append(build_image_url_part(data_url))
            continue
        if (mime or "").startswith("audio/"):
            from app.core.multimodal import transcribe_audio

            text = await transcribe_audio(content, name)
            if text and not text.startswith(("无法识别", "语音识别失败")):
                injected.append(f"[音频已转文字: {name}]\n{text}")
            else:
                injected.append(text)
            continue
        # 其余按文本注入
        text_got = attachment_store.read_content(req.session_id, att_id)
        if text_got:
            injected.append(f"[附件: {text_got[0]}]\n```\n{text_got[1]}\n```")
    prefix = ("\n\n".join(injected) + "\n\n") if injected else ""
    return prefix + message, parts


def _friendly_llm_error(msg: str) -> str:
    """把常见上游 LLM 报错翻译成用户可操作的中文提示,其余原样返回。"""
    lowered = (msg or "").lower()
    if (
        "does not support image input" in lowered
        or "not support image" in lowered
        or "doesn't support image" in lowered
        or ("cannot read" in lowered and "image" in lowered)
    ):
        return "当前使用的模型不支持图片识别。请移除图片附件,或在左下角切换支持视觉的模型(如 qwen-vl)后再试。"
    if "cannot identify image file" in lowered or "error while loading image" in lowered:
        return "图片无法被模型读取,请换用清晰的 PNG/JPG 图片后再试。"
    return msg


_VISION_REFUSAL_MARKERS = (
    "does not support image input",
    "does not support image",
    "doesn't support image",
    "not support image input",
    "unable to process images",
    "cannot process images",
)


def _looks_like_vision_refusal(text: str) -> bool:
    """识别'上游以 200 正常响应返回的不支持视觉说明'。

    部分网关/中转不抛 HTTP 错误,而是把上游错误包装成 assistant 文本
    (常见残留指令如 'Inform the user.'),导致英文原文被当作答案渲染。
    """
    t = (text or "").lower()
    if not t or "image" not in t:
        return False
    if any(m in t for m in _VISION_REFUSAL_MARKERS):
        return True
    # 更宽的兜底:'cannot read' 与图片语境同时出现(如 Cannot read "image.png")
    return "cannot read" in t and ('image.png' in t or 'image_url' in t or 'attachment' in t)


async def _stream_with_heartbeat(agent_iter, interval: int = 15):
    """在 Agent 静默期(长耗时工具调用 / 外部图片生成 API 等)周期性注入 heartbeat 事件。

    前端以"收到任意事件即续期空闲计时器"来判定连接是否存活;若后端在工具调用期间
    长时间不产出任何 SSE 事件,前端会误判为"响应超时"。heartbeat 让连接保持活跃,
    且不进入 full_answer / 历史存储。

    实现要点:用一个常驻 producer 任务把 Agent 流完整驱动(整条流处于同一 asyncio
    Context,保证 run_stream 内的 contextvars token set/reset 在同一上下文完成),
    事件经队列交给消费协程;消费协程同时等待"队列有事件"或"心跳间隔",二者先到先发。
    """
    queue: asyncio.Queue = asyncio.Queue()
    agen = agent_iter.__aiter__()

    async def _producer():
        try:
            async for evt in agen:
                await queue.put(("evt", evt))
        except Exception as exc:  # noqa: BLE001
            await queue.put(("err", exc))
        finally:
            await queue.put(("stop", None))

    prod_task = asyncio.ensure_future(_producer())
    try:
        while True:
            get_q = asyncio.ensure_future(queue.get())
            hb = asyncio.ensure_future(asyncio.sleep(interval))
            done, _ = await asyncio.wait({get_q, hb}, return_when=asyncio.FIRST_COMPLETED)
            if get_q in done:
                hb.cancel()
                kind, payload = get_q.result()
                if kind == "stop":
                    break
                if kind == "err":
                    raise payload
                yield payload
            else:
                # 静默达到一个间隔:发心跳(不取消 get_q,事件稍后照常到达)
                yield {"type": "heartbeat"}
    finally:
        prod_task.cancel()


def _build_agent(req: ChatRequest) -> Agent:
    registry = get_registry()
    try:
        if req.provider:
            llm = LLMClient(req.provider, req.model or None)
        elif req.model:
            llm = LLMClient(model=req.model)
        else:
            llm = LLMClient()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"模型配置错误: {e}") from e
    return Agent(
        llm=llm,
        registry=registry,
        route=settings.agent_routing_enabled,
        session_id=req.session_id,
        approve_tools=req.approve_tools,
        mode=req.mode,
        critic_provider=req.critic_provider,
        critic_model=req.critic_model,
        ensemble_models=req.models,
        max_output_tokens=req.max_output_tokens,
        orch_subtasks=req.orch_subtasks,
        orch_candidates=req.orch_candidates,
    )


@router.post("/chat")
async def chat(req: ChatRequest) -> StreamingResponse:
    """非流式接口已废弃，统一使用流式输出。"""
    return await chat_stream(req)


class ChatResumeRequest(BaseModel):
    run_id: str = Field(..., description="checkpoint 运行 ID(interrupt 时返回)")
    session_id: str = Field("default", description="会话 ID")
    resume_value: object | None = Field(None, description="用户决定:工具名列表 或 {confirmation_id: {approved: bool}}")
    use_history: bool = Field(True, description="是否携带会话历史")
    stream: bool = Field(False, description="是否流式返回(SSE)")


@router.post("/chat/resume")
async def chat_resume(req: ChatResumeRequest):
    """从中断处恢复一次运行(interrupt 式确认流批准后继续执行)。

    支持非流式和流式两种返回模式。
    """
    agent = _build_agent(ChatRequest(message=" ", session_id=req.session_id))
    try:
        gen = agent.resume(req.run_id, resume_value=req.resume_value, stream=req.stream)
        import types
        if isinstance(gen, types.AsyncGeneratorType):
            async def event_gen():
                async for item in gen:
                    if isinstance(item, dict) and item.get("type") in (
                        "token", "thinking", "tool", "plan", "confirmation", "done", "tool_call_start"
                    ):
                        yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                    elif hasattr(item, "answer"):
                        r = item
                        if r.interrupted:
                            yield f"data: {json.dumps({'type': 'done', 'interrupted': True, 'interrupt': r.interrupt}, ensure_ascii=False)}\n\n"
                        else:
                            for ch in r.answer:
                                yield f"data: {json.dumps({'type': 'token', 'content': ch}, ensure_ascii=False)}\n\n"
                            yield f"data: {json.dumps({'type': 'done', 'model': r.model, 'steps': r.steps, 'run_id': req.run_id}, ensure_ascii=False)}\n\n"
            return StreamingResponse(
                event_gen(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        result = gen
    except LLMError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    if not result.interrupted:
        for step in result.steps:
            if step.get("type") == "tool":
                await _store().add_message(
                    req.session_id,
                    "tool",
                    json.dumps(
                        {"name": step.get("tool", ""), "arguments": step.get("arguments", ""), "result": step.get("result", "")},
                        ensure_ascii=False,
                    ),
                )
            elif step.get("type") == "plan":
                await _store().add_message(
                    req.session_id,
                    "plan",
                    json.dumps(step.get("plan"), ensure_ascii=False),
                )
        await _store().add_message(req.session_id, "assistant", result.answer)

    resp = {
        "answer": result.answer,
        "session_id": req.session_id,
        "model": result.model,
        "steps": result.steps,
    }
    if result.interrupted:
        resp["interrupted"] = True
        resp["interrupt"] = result.interrupt
    return resp


@router.post("/chat/stream")
async def chat_stream(req: ChatRequest) -> StreamingResponse:
    agent = _build_agent(req)
    history = []
    if req.use_history:
        history = await _store().get_history(req.session_id)

    async def event_gen():
        full_answer: list[str] = []
        tool_events: list[dict] = []
        plan_events: list[dict] = []
        injected_message, mm_parts = await _prepare_attachments(req, req.message)
        agent.with_multimodal(mm_parts)
        try:
            async for event in _stream_with_heartbeat(agent.run_stream(injected_message, history=history)):
                if event["type"] == "token":
                    full_answer.append(event["content"])
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                elif event["type"] == "plan":
                    plan_events.append(event)
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                elif event["type"] in ("tool", "thinking", "confirmation", "tool_start", "tool_done", "tool_chunk", "tool_call_start"):
                    if event["type"] == "tool":
                        tool_events.append(event)
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                else:  # done
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except LLMError as e:
            yield f"data: {json.dumps({'type': 'error', 'message': _friendly_llm_error(str(e))}, ensure_ascii=False)}\n\n"
            return
        finally:
            await _store().add_message(req.session_id, "user", req.message)
            for evt in tool_events:
                await _store().add_message(
                    req.session_id,
                    "tool",
                    json.dumps(
                        {"name": evt.get("name", ""), "arguments": evt.get("arguments", ""), "result": evt.get("result", "")},
                        ensure_ascii=False,
                    ),
                )
            for evt in plan_events:
                await _store().add_message(
                    req.session_id,
                    "plan",
                    json.dumps(evt.get("plan"), ensure_ascii=False),
                )
            if full_answer:
                await _store().add_message(req.session_id, "assistant", "".join(full_answer))

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/stream")
async def stream_alias(
    req: ChatRequest = Body(...),
) -> StreamingResponse:
    """SSE 流式接口(仅 POST)。

    安全说明:旧版 GET 接口会将用户消息放入 URL 查询参数,导致消息内容
    泄露到服务器访问日志/CDN 日志/浏览器历史。改为 POST 后消息走请求体,
    不再出现在 URL 日志中。EventSource 客户端需改用 fetch+ReadabilityStream。
    """
    return await chat_stream(req)

@router.get("/chat/history")
async def get_history(session_id: str = Query("default")) -> dict:
    history = await _store().get_history(session_id)
    return {"session_id": session_id, "messages": history}


@router.get("/chat/context")
async def chat_context(session_id: str = Query("default")) -> dict:
    """当前会话上下文用量:消息数 / 字符数 / 估算 token / 预算与占用百分比。"""
    from app.memory.context import estimate_tokens

    history = await _store().get_history(session_id)
    chars = sum(len(str(m.get("content", ""))) for m in history)
    tokens = sum(estimate_tokens(str(m.get("content", ""))) for m in history)

    # 预算:优先 token 级;否则按字符预算折算(约 4 字符/token,与 estimate_tokens 一致)
    token_budget = settings.agent_token_budget
    if token_budget and token_budget > 0:
        budget = token_budget
    else:
        budget = max(1, settings.context_char_budget // 4)

    return {
        "session_id": session_id,
        "messages": len(history),
        "chars": chars,
        "tokens": tokens,
        "token_budget": budget,
        "usage_pct": round(min(100.0, tokens / budget * 100), 1) if budget else 0.0,
        "over_budget": tokens > budget,
    }


@router.get("/chat/sessions")
async def list_sessions() -> dict:
    """列出所有会话(标题/消息数/更新时间),供桌面端会话列表使用。"""
    sessions = await _store().list_sessions()
    return {"sessions": sessions, "total": len(sessions)}


@router.get("/memory")
async def list_memories(limit: int = Query(50, ge=1, le=500)) -> dict:
    """列出长期记忆。"""
    from app.memory.ltm import get_memory_store

    store = get_memory_store()
    return {"memories": store.list(limit=limit), "stats": store.stats()}


@router.post("/memory/clear")
async def clear_memories() -> dict:
    """清空长期记忆。"""
    from app.memory.ltm import get_memory_store

    deleted = get_memory_store().clear()
    return {"deleted": deleted, "cleared": True}


class MemoryCreate(BaseModel):
    content: str = Field(..., min_length=1, description="记忆内容")
    keywords: list[str] = Field(default_factory=list, description="关键词(可选)")
    scope: str = Field("global", description="作用域: global / session")
    session_id: str | None = Field(None, description="scope=session 时的会话 ID")


@router.post("/memory")
async def create_memory(req: MemoryCreate) -> dict:
    """手动新增一条长期记忆。"""
    from app.memory.ltm import get_memory_store

    result = get_memory_store().save(
        req.content,
        keywords=req.keywords,
        scope=req.scope,
        session_id=req.session_id,
        source="manual",
    )
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.delete("/memory/{memory_id}")
async def delete_memory(memory_id: str) -> dict:
    """删除一条长期记忆。"""
    from app.memory.ltm import get_memory_store

    ok = get_memory_store().delete(memory_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"未找到记忆: {memory_id}")
    return {"memory_id": memory_id, "deleted": True}


@router.get("/chat/search")
async def search_history(q: str = Query(..., description="搜索关键词")) -> dict:
    """在全部会话的消息内容中做全文检索,返回命中会话与片段。"""
    q = q.strip()
    if not q:
        return {"query": q, "results": []}
    try:
        store = get_session_store()
        results = await store.search(q)
    except Exception:  # noqa: BLE001
        results = []
    return {"query": q, "results": results}


@router.get("/chat/export")
async def export_history(session_id: str = Query("default"), format: str = Query("md")) -> dict:
    """导出会话历史为 Markdown 文本。"""
    history = await _store().get_history(session_id)
    lines = [f"# 会话 {session_id}", ""]
    for m in history:
        role = m.get("role", "")
        content = str(m.get("content", ""))
        if role == "user":
            lines.append(f"## 用户\n\n{content}\n")
        elif role == "assistant":
            lines.append(f"## 助手\n\n{content}\n")
        elif role == "tool":
            try:
                obj = json.loads(content)
                lines.append(f"## 工具调用: {obj.get('name', '')}\n\n```json\n{content}\n```\n")
            except Exception:  # noqa: BLE001
                lines.append(f"## 工具调用\n\n{content}\n")
        elif role == "error":
            lines.append(f"## 错误\n\n{content}\n")
    text = "\n".join(lines)
    if format == "txt":
        return {"session_id": session_id, "format": format, "content": text}
    return {"session_id": session_id, "format": "md", "content": text}


@router.post("/chat/clear")
async def clear_history(session_id: str = Query("default")) -> dict:
    await _store().clear(session_id)
    return {"session_id": session_id, "cleared": True}


class RenameRequest(BaseModel):
    session_id: str = Field(..., description="会话 ID")
    title: str = Field("", description="自定义会话名(空串清除,回到自动标题)")


@router.post("/chat/rename")
async def rename_session(req: RenameRequest) -> dict:
    await _store().rename(req.session_id, req.title[:100])
    return {"session_id": req.session_id, "title": req.title[:100].strip(), "ok": True}


@router.post("/vision")
async def vision(
    file: UploadFile = File(...),
    prompt: str = Form("请描述这张图片的内容"),
    session_id: str = Form("default"),
    provider: str = Form(""),
    model: str = Form(""),
) -> dict:
    """图片识别接口:将图片与文字提示发送给支持视觉的模型。

    provider/model 由前端传入(左下角模型选择器的当前选择);
    留空时回退到后端全局配置。
    """
    content = await file.read()
    if len(content) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="图片超过 20MB 限制")
    ext = (file.filename or "png").rsplit(".", 1)[-1].lower()
    if ext not in ("png", "jpg", "jpeg", "gif", "webp", "bmp"):
        raise HTTPException(status_code=400, detail=f"不支持的图片格式: {ext}")

    from app.core.multimodal import image_to_data_url, model_supports_vision

    mime = f"image/{ext}" if ext != "jpg" else "image/jpeg"
    data_url = image_to_data_url(content, mime)

    llm = None
    try:
        from app.core.llm import LLMClient, LLMError

        # 使用前端选中的模型(与聊天一致);留空回退全局配置
        llm = LLMClient(provider or None, model or None)
        if not model_supports_vision(llm.info.get("model", "")):
            return {
                "answer": "",
                "error": f"当前模型 {llm.info.get('model')} 不支持图片识别。"
                         "请切换支持视觉的模型(如 qwen-vl)后再试。",
                "model": llm.info.get("model", ""),
            }
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }]
        answer = await llm.chat(messages)
        if _looks_like_vision_refusal(answer):
            # 上游以 200 把"不支持视觉"包装成文本返回,拦截并转成可操作的中文提示
            return {
                "answer": "",
                "error": f"上游模型 {llm.info.get('model', '')} 不支持图片输入。"
                         "请切换支持视觉的模型(如 qwen-vl、gpt-4o)后再试。",
                "model": llm.info.get("model", ""),
            }
        return {"answer": answer, "model": llm.info["model"], "session_id": session_id}
    except LLMError as e:
        msg = str(e)
        lowered = msg.lower()
        if (
            "does not support image input" in lowered
            or "cannot read" in lowered
            or "not support image" in lowered
            or "doesn't support image" in lowered
        ):
            hint = "当前使用的模型不支持图片识别。请切换支持视觉的模型(如 qwen-vl)后再试。"
        elif "cannot identify image file" in lowered or "error while loading image" in lowered:
            hint = "图片无法被模型读取,请换用清晰的 PNG/JPG 图片后再试。"
        else:
            hint = f"图片识别失败: {e}"
        return {
            "answer": "",
            "error": hint,
            "model": llm.info.get("model") if llm is not None else "",
        }
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        return {"answer": "", "error": f"图片识别异常: {e}"}



@router.post("/audio/transcribe")
async def transcribe(
    file: UploadFile = File(...),
    session_id: str = Form("default"),
) -> dict:
    """语音转录接口:把音频字节发给 OpenAI 兼容 ASR 端点,返回文字。

    未配置 ASR 服务或转录失败时返回明确提示(不抛异常),供前端直接展示。
    配置项: ASR_BASE_URL / ASR_API_KEY / ASR_MODEL(.env)。
    """
    content = await file.read()
    if len(content) > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="音频超过 50MB 限制")
    from app.core.multimodal import transcribe_audio

    name = file.filename or "audio.wav"
    text = await transcribe_audio(content, name)
    ok = bool(text) and not text.startswith(("无法识别", "语音识别失败"))
    return {
        "session_id": session_id,
        "transcribed": ok,
        "text": text,
        "error": "" if ok else text,
    }


# ---- Provider / Model 配置接口 ----

# 各 provider 的默认模型
_PROVIDER_DEFAULT_MODELS: dict[str, list[dict]] = {
    "deepseek": [
        {"id": "deepseek-chat", "name": "DeepSeek Chat", "speed": "fast", "quality": "good"},
        {"id": "deepseek-coder", "name": "DeepSeek Coder", "speed": "fast", "quality": "excellent"},
        {"id": "deepseek-reasoner", "name": "DeepSeek R1", "speed": "slow", "quality": "excellent"},
    ],
    "qwen": [
        {"id": "qwen-plus", "name": "Qwen Plus", "speed": "fast", "quality": "good"},
        {"id": "qwen-turbo", "name": "Qwen Turbo", "speed": "very_fast", "quality": "fair"},
        {"id": "qwen-max", "name": "Qwen Max", "speed": "medium", "quality": "excellent"},
        {"id": "qwen3.7-flash", "name": "Qwen 3.7 Flash", "speed": "very_fast", "quality": "good"},
    ],
    "openai": [
        {"id": "gpt-4o-mini", "name": "GPT-4o Mini", "speed": "very_fast", "quality": "good"},
        {"id": "gpt-4o", "name": "GPT-4o", "speed": "fast", "quality": "excellent"},
        {"id": "gpt-4-turbo", "name": "GPT-4 Turbo", "speed": "medium", "quality": "excellent"},
        {"id": "o1-mini", "name": "O1 Mini", "speed": "slow", "quality": "excellent"},
        {"id": "o1", "name": "O1", "speed": "very_slow", "quality": "outstanding"},
    ],
    "compat": [
        {"id": "agnes-2.0-flash", "name": "Agnes 2.0 Flash", "speed": "fast", "quality": "good"},
        {"id": "agnes-2.5-flash", "name": "Agnes 2.5 Flash", "speed": "very_fast", "quality": "good"},
        {"id": "agnes-2.5-pro", "name": "Agnes 2.5 Pro", "speed": "medium", "quality": "excellent"},
        {"id": "agnes-2.5-pro-alpha", "name": "Agnes 2.5 Pro Alpha", "speed": "medium", "quality": "outstanding"},
    ],
    "ollama": [],
}

# Provider 分组信息
_PROVIDER_GROUPS = [
    {"id": "agnes", "name": "Agnes AI", "providers": ["compat"]},
    {"id": "deepseek", "name": "DeepSeek", "providers": ["deepseek"]},
    {"id": "qwen", "name": "阿里云 Qwen", "providers": ["qwen"]},
    {"id": "openai", "name": "OpenAI", "providers": ["openai"]},
    {"id": "local", "name": "本地模型", "providers": ["ollama"]},
]


@router.get("/models")
async def list_models(provider: str | None = None) -> dict:
    """获取可用模型列表。

    Args:
        provider: 指定 provider ID (deepseek/qwen/openai/compat/ollama)，留空返回当前配置的模型
    """
    # 如果指定了 provider，返回该 provider 的模型列表
    if provider:
        provider = provider.lower()
        if provider.startswith("custom-"):
            from app.core.provider_registry import get_custom

            entry = get_custom(provider)
            if entry is None:
                raise HTTPException(status_code=404, detail=f"自定义提供商不存在: {provider}")
            models = [
                {"id": m, "name": m, "speed": "", "quality": ""}
                for m in entry.get("models", [])
            ]
            default = entry.get("model", "")
            if default and default not in [m["id"] for m in models]:
                models.insert(0, {"id": default, "name": default, "speed": "", "quality": ""})
            return {"provider": provider, "configured": True, "models": models}
        if provider in _PROVIDER_DEFAULT_MODELS:
            models = _PROVIDER_DEFAULT_MODELS[provider]
            # 检查是否配置了 API key
            configured = False
            if provider == "deepseek":
                configured = bool(settings.deepseek_api_key)
            elif provider == "qwen":
                configured = bool(settings.qwen_api_key)
            elif provider == "openai":
                import os
                configured = bool(os.environ.get("OPENAI_API_KEY"))
            elif provider == "compat":
                configured = bool(settings.openai_compat_api_key)
            elif provider == "ollama":
                configured = True  # 本地模型，不需要 API key

            return {
                "provider": provider,
                "configured": configured,
                "models": models,
            }
        raise HTTPException(status_code=400, detail=f"未知的 provider: {provider}")

    # 返回当前配置的 provider 和默认模型
    current_provider = settings.agent_llm_provider.lower()
    default_model = None
    if current_provider == "deepseek":
        default_model = settings.deepseek_model
    elif current_provider == "qwen":
        default_model = settings.qwen_model
    elif current_provider == "openai":
        default_model = "gpt-4o-mini"
    elif current_provider == "compat":
        default_model = settings.openai_compat_model
    elif current_provider == "ollama":
        default_model = settings.ollama_model

    return {
        "provider": current_provider,
        "model": default_model,
        "available_providers": [
            {"id": p["id"], "name": p["name"], "providers": p["providers"]}
            for p in _PROVIDER_GROUPS
        ],
    }


@router.get("/providers")
async def list_providers() -> dict:
    """获取已配置的 provider 列表。"""
    providers = []
    for group in _PROVIDER_GROUPS:
        for pid in group["providers"]:
            configured = False
            if pid == "deepseek":
                configured = bool(settings.deepseek_api_key)
            elif pid == "qwen":
                configured = bool(settings.qwen_api_key)
            elif pid == "openai":
                import os
                configured = bool(os.environ.get("OPENAI_API_KEY"))
            elif pid == "compat":
                configured = bool(settings.openai_compat_api_key)
            elif pid == "ollama":
                configured = True

            providers.append({
                "id": pid,
                "name": next((g["name"] for g in _PROVIDER_GROUPS if pid in g["providers"]), pid),
                "group": group["id"],
                "configured": configured,
                "default_model": (
                    settings.deepseek_model if pid == "deepseek"
                    else settings.qwen_model if pid == "qwen"
                    else settings.ollama_model if pid == "ollama"
                    else settings.openai_compat_model if pid == "compat"
                    else "gpt-4o-mini"
                ),
            })

    # 自定义 OpenAI 兼容提供商(设置-模型服务中配置)
    from app.core.provider_registry import list_custom

    for c in list_custom():
        providers.append({
            "id": c["id"],
            "name": c["name"],
            "group": "custom",
            "configured": True,
            "default_model": c.get("model", ""),
        })

    # 获取当前激活的 provider
    current = settings.agent_llm_provider.lower()
    return {
        "providers": providers,
        "default": current,
        "active": current,
        "groups": [
            {"id": g["id"], "name": g["name"], "count": len(g["providers"])}
            for g in _PROVIDER_GROUPS
        ],
    }
