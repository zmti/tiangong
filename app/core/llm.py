"""多 Provider LLM 接入层,统一封装 OpenAI 兼容接口。

支持: deepseek / qwen / ollama / openai / 任意 OpenAI 兼容服务
所有 Provider 均走 /chat/completions OpenAI 兼容协议。
额外能力:原生 function calling、并行工具调用、结构化 JSON 输出、
严格 JSON Schema 输出(chat_json_schema + 校验回灌重试)、瞬态错误指数
退避重试、provider 故障转移、prompt cache 断点、reasoning 模型思维链分离。
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Optional

from openai import AsyncOpenAI

from app.config import settings
from app.core.telemetry import estimate_cost, tracer

# ChatCompletion message 类型定义
Message = dict  # {"role": "user"/"assistant"/"system", "content": str}

# 瞬态错误特征:命中即判定为可重试(网络抖动/限流/5xx)
_RETRYABLE_MARKERS = (
    "timeout", "timed out", "rate limit", "429", "overloaded",
    "connection", "network", "500", "502", "503", "504",
    "temporarily unavailable", "try again",
)

# 推理模型:thinking 内容在 reasoning_content 字段,需分离透出
_REASONING_MODELS = ("deepseek-reasoner", "deepseek-r1", "o1", "o1-", "o3", "o3-", "reasoner")

PROVIDER_ALIASES = {
    "deepseek": "deepseek",
    "ds": "deepseek",
    "qwen": "qwen",
    "dashscope": "qwen",
    "tongyi": "qwen",
    "ollama": "ollama",
    "local": "ollama",
    "openai": "openai",
    "compat": "compat",
    "compatible": "compat",
}


class LLMError(Exception):
    pass


@dataclass
class ToolCall:
    """一次原生工具调用。"""

    id: str
    name: str
    arguments: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "arguments": self.arguments,
        }

    def to_openai(self) -> dict:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


@dataclass
class ChatResult:
    """一次对话的完整结果:文本内容 + 原生工具调用列表。"""

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = ""
    usage: Optional[dict] = None
    reasoning: str = ""  # reasoning_content 思维链(仅推理模型返回)

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)

    @property
    def tool_calls_plain(self) -> list[dict]:
        return [tc.to_dict() for tc in self.tool_calls]


class LLMClient:
    """统一 LLM 客户端,按 provider 从配置中组装 OpenAI 兼容客户端。"""

    # ---- Provider 熔断状态(实例级,避免多会话互相影响) ----

    def __init__(self, provider: Optional[str] = None, model: Optional[str] = None):
        explicit = (provider or "").strip()
        raw = (explicit or settings.agent_llm_provider).lower()
        # 兼容历史整串格式 "provider:model"(旧版前端拼接)
        if explicit and ":" in raw and raw not in PROVIDER_ALIASES and not raw.startswith("custom-"):
            head, tail = raw.split(":", 1)
            if head.lower() in PROVIDER_ALIASES:
                raw = head.lower()
                if not model:
                    model = tail
        self._custom: Optional[dict] = None
        if raw.startswith("custom-"):
            from app.core.provider_registry import get_custom

            self._custom = get_custom(raw)
        if self._custom:
            self.provider = raw
        elif explicit and raw not in PROVIDER_ALIASES:
            # 显式指定的 provider 无法解析:明确报错,绝不静默换成别的模型
            raise LLMError(
                f"所选模型提供商不可用: {explicit}(不存在或已被删除)。"
                "请重新在左下角选择模型,或在设置-模型服务中检查该提供商。"
            )
        else:
            self.provider = PROVIDER_ALIASES.get(raw, "compat")
        self._model_override = model
        self.client, self.model = self._build()
        self._api_key = self._get_key()
        self.last_usage: Optional[dict] = None  # 最近一次调用返回的 token 用量
        self._reasoning: str = ""  # 最近一次推理模型的思维链(reasoning_content)
        # 实例级熔断状态
        self._circuit_fails: dict[str, int] = {}
        self._circuit_open_until: dict[str, float] = {}

    # ---- 可观测性 ----

    def _trace(self, kind: str, t0: float, usage: Optional[dict], ok: bool = True,
               error: str = "", tools_n: int = 0, content: str = "") -> None:
        """记录一次 LLM 调用 trace(含 token 与成本估算)。"""
        usage = usage or {}
        cache_hit = int(usage.get("cache_hit_tokens", 0) or 0)
        cache_miss = int(usage.get("cache_miss_tokens", 0) or 0)
        prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        hit_rate = (cache_hit / prompt_tokens) if prompt_tokens else 0.0
        tracer.record({
            "type": "llm_call",
            "kind": kind,
            "provider": self.provider,
            "model": self.model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": usage.get("completion_tokens", 0),
            "tokens": usage.get("total_tokens", 0) or (prompt_tokens + usage.get("completion_tokens", 0)),
            "cache_hit_tokens": cache_hit,
            "cache_miss_tokens": cache_miss,
            "cache_hit_rate": round(hit_rate, 4),
            "cost": estimate_cost(self.model, prompt_tokens, usage.get("completion_tokens", 0)),
            "duration": round(time.time() - t0, 3),
            "ok": ok,
            "error": (error or "")[:300],
            "tools_n": tools_n,
            "reply_preview": (content or "")[:200],
        })

    def _get_key(self) -> str:
        if self._custom:
            return self._custom.get("api_key", "")
        p = self.provider
        if p == "deepseek":
            return settings.deepseek_api_key
        if p == "qwen":
            return settings.qwen_api_key
        if p == "ollama":
            return "ollama"
        if p == "openai":
            import os

            return os.environ.get("OPENAI_API_KEY", "")
        return settings.openai_compat_api_key

    def _build(self) -> tuple[AsyncOpenAI, str]:
        if self._custom:
            return AsyncOpenAI(
                api_key=self._custom.get("api_key") or "sk-placeholder",
                base_url=self._custom["base_url"],
            ), self._model_override or self._custom.get("model") or ""
        p = self.provider
        if p == "deepseek":
            return AsyncOpenAI(
                api_key=settings.deepseek_api_key or "sk-placeholder",
                base_url=settings.deepseek_base_url,
            ), self._model_override or settings.deepseek_model
        if p == "qwen":
            return AsyncOpenAI(
                api_key=settings.qwen_api_key or "sk-placeholder",
                base_url=settings.qwen_base_url,
            ), self._model_override or settings.qwen_model
        if p == "ollama":
            return AsyncOpenAI(
                api_key="ollama",
                base_url=settings.ollama_base_url,
            ), self._model_override or settings.ollama_model
        if p == "openai":
            return AsyncOpenAI(), self._model_override or "gpt-4o-mini"
        # compat: 任意 OpenAI 兼容
        return AsyncOpenAI(
            api_key=settings.openai_compat_api_key or "sk-placeholder",
            base_url=settings.openai_compat_base_url,
        ), self._model_override or settings.openai_compat_model

    def _require_key(self) -> None:
        if self._custom and not self._api_key:
            raise LLMError(f"自定义提供商「{self._custom.get('name', self.provider)}」未配置 API Key,请在设置-模型服务中填写")
        if self.provider in ("deepseek", "qwen", "openai", "compat") and not self._api_key:
            raise LLMError(f"Provider {self.provider} 未配置 API Key,请在 .env 中设置")

    # ---- 可靠性:瞬态错误重试 + 故障转移 ----

    @classmethod
    def _is_retryable(cls, error: str) -> bool:
        err = error.lower()
        return any(m in err for m in _RETRYABLE_MARKERS)

    def _failover_specs(self) -> list[tuple[str, str]]:
        """返回 [(provider, model), ...] 故障转移候选,主 provider 在前。"""
        specs: list[tuple[str, str]] = [(self.provider, self.model)]
        fb = settings.agent_llm_failover_provider
        if fb:
            fb_p = PROVIDER_ALIASES.get(fb.lower(), "compat")
            fb_m = settings.agent_llm_failover_model
            specs.append((fb_p, fb_m))
        return specs

    def _make_client(self, provider: str, model: str) -> tuple[AsyncOpenAI, str]:
        """按 provider 构造 OpenAI 兼容客户端(供故障转移时重建)。"""
        if provider.startswith("custom-"):
            from app.core.provider_registry import get_custom

            entry = get_custom(provider)
            if entry:
                return AsyncOpenAI(
                    api_key=entry.get("api_key") or "sk-placeholder",
                    base_url=entry["base_url"],
                ), model or entry.get("model") or ""
        p = provider
        if p == "deepseek":
            return AsyncOpenAI(
                api_key=settings.deepseek_api_key or "sk-placeholder",
                base_url=settings.deepseek_base_url,
            ), model or settings.deepseek_model
        if p == "qwen":
            return AsyncOpenAI(
                api_key=settings.qwen_api_key or "sk-placeholder",
                base_url=settings.qwen_base_url,
            ), model or settings.qwen_model
        if p == "ollama":
            return AsyncOpenAI(
                api_key="ollama",
                base_url=settings.ollama_base_url,
            ), model or settings.ollama_model
        if p == "openai":
            return AsyncOpenAI(), model or "gpt-4o-mini"
        return AsyncOpenAI(
            api_key=settings.openai_compat_api_key or "sk-placeholder",
            base_url=settings.openai_compat_base_url,
        ), model or settings.openai_compat_model

    async def _invoke(self, fn, kind: str, tools_n: int = 0, **trace_extra):
        """带重试+故障转移+熔断的 LLM 调用。

        fn(client, model) -> OpenAI 响应对象;成功返回响应,耗尽后抛 LLMError。
        语义:每个候选 provider 内对瞬态错误指数退避重试;该 provider 仍失败
        则切换到下一个候选(故障转移);连续失败触发熔断,冷却窗口内直接跳过。
        """
        specs = self._failover_specs()
        retries = max(0, int(settings.agent_llm_retries))
        last_err: Optional[Exception] = None
        for idx, (provider, model) in enumerate(specs):
            if self._is_circuit_open(provider):
                # 熔断冷却中:跳过该 provider
                last_err = LLMError(f"provider {provider} 熔断冷却中,跳过")
                continue
            if idx == 0:
                # 主 provider:用 __init__ 预构建的客户端(子类可覆盖 self.client 便于测试)
                client, resolved_model = self.client, self.model
            else:
                client, resolved_model = self._make_client(provider, model)
            for attempt in range(retries + 1):
                t0 = time.time()
                try:
                    resp = await fn(client, resolved_model)
                    self._record_success(provider)
                    self.last_usage = self._usage_of(resp)
                    if idx > 0:
                        # 故障转移生效:把实例切到实际使用的 provider/model,
                        # 保证 done.model / trace / info 反映真实模型(粘性切换)
                        self.provider = provider
                        self.model = resolved_model
                        self.client = client
                        self._api_key = self._get_key()
                    self._trace(kind, t0, self.last_usage, tools_n=tools_n,
                                **trace_extra)
                    return resp
                except Exception as e:  # noqa: BLE001
                    self._trace(kind, t0, None, ok=False, error=str(e), tools_n=tools_n)
                    last_err = e
                    if attempt < retries and self._is_retryable(str(e)):
                        await asyncio.sleep(settings.agent_llm_retry_delay * (2 ** attempt))
                        continue
                    break  # 不可重试或重试耗尽,本 spec 失败
            self._record_failure(provider)
        raise LLMError(f"LLM 调用失败(已重试{retries}次/切换{len(specs)}个provider): {last_err}") from last_err

    # ---- Provider 熔断状态(进程级共享,同类 provider 复用) ----
    # 注:实例级 _circuit_fails / _circuit_open_until 已在 __init__ 中初始化
    # 保留类级别作为兜底(旧代码直接访问 LLMClient._circuit_fails 时使用)
    _circuit_fails: dict[str, int] = {}
    _circuit_open_until: dict[str, float] = {}

    def _is_circuit_open(self, provider: str) -> bool:
        if settings.agent_llm_circuit_threshold <= 0:
            return False
        until = self._circuit_open_until.get(provider, 0.0)
        if until > time.time():
            return True
        if until > 0:
            # 冷却结束:清零计数,允许再次尝试
            self._circuit_fails[provider] = 0
            self._circuit_open_until[provider] = 0.0
        return False

    def _record_success(self, provider: str) -> None:
        self._circuit_fails[provider] = 0
        self._circuit_open_until[provider] = 0.0

    def _record_failure(self, provider: str) -> None:
        if settings.agent_llm_circuit_threshold <= 0:
            return
        fails = self._circuit_fails.get(provider, 0) + 1
        self._circuit_fails[provider] = fails
        if fails >= settings.agent_llm_circuit_threshold:
            self._circuit_open_until[provider] = time.time() + settings.agent_llm_circuit_cooldown

    # ---- 缓存控制 / reasoning ----

    def _cache_control_messages(self, messages: list[Message]) -> list[Message]:
        """给 system 消息注入 cache_control 断点,保持稳定前缀命中缓存。"""
        if not settings.agent_prompt_cache:
            return messages
        out: list[Message] = []
        for i, m in enumerate(messages):
            mm = dict(m)
            if m.get("role") == "system":
                mm["cache_control"] = {"type": "ephemeral"}
            out.append(mm)
        return out

    def _is_reasoning_model(self) -> bool:
        return any(t in (self.model or "").lower() for t in _REASONING_MODELS)

    def _maybe_wrap_with_thinking_prompt(self, messages: list[Message]) -> list[Message]:
        """为非推理模型添加 CoT 提示,使非推理模型也能产出结构化思考。

        仅在以下条件下注入:
        - agent_reasoning_enabled=True
        - 当前模型不是推理模型(_is_reasoning_model=False)
        - 消息中还没有 thinking 类系统提示

        通过修改最后一条 user 消息的尾部追加思考指令,
        让模型在最终回答前先进行逐步推理。
        """
        if not settings.agent_reasoning_enabled:
            return messages
        if self._is_reasoning_model():
            return messages
        # 检查是否已有 thinking 提示
        for m in messages:
            if m.get("role") == "system" and "think" in str(m.get("content", "")).lower():
                return messages
        if not messages:
            return messages
        # 在最后一条 user 消息后追加思考指令
        last = messages[-1]
        if last.get("role") != "user":
            return messages
        content = last.get("content", "")
        # 多模态内容(content 是 list 时)不注入 CoT 提示
        if isinstance(content, list):
            return messages
        if not content or not content.strip():
            return messages
        thinking_suffix = (
            "\n\n---\n\n请分步骤思考:先列出解题思路或关键信息,再给出最终答案。"
            "用 <thinking>...</thinking> 标签包裹你的逐步推理过程。"
        )
        if thinking_suffix not in content:
            last = dict(last)
            last["content"] = content.rstrip() + thinking_suffix
            messages = list(messages)
            messages[-1] = last
        return messages

    def _extract_reasoning(self, msg) -> str:
        rc = getattr(msg, "reasoning_content", None) or ""
        if rc:
            return rc
        if not self._is_reasoning_model() or not settings.agent_reasoning_enabled:
            return ""
        return getattr(msg, "reasoning", "") or ""

    @staticmethod
    def _usage_of(resp) -> Optional[dict]:
        """从 OpenAI 响应中提取 token 用量(含 prompt cache 命中量)。"""
        u = getattr(resp, "usage", None)
        if u is None:
            return None
        prompt_tokens = int(getattr(u, "prompt_tokens", 0) or 0)
        # 缓存命中:DeepSeek 用 prompt_cache_hit_tokens;OpenAI 风格用 prompt_tokens_details.cached_tokens
        cache_hit = int(getattr(u, "prompt_cache_hit_tokens", 0) or 0)
        details = getattr(u, "prompt_tokens_details", None)
        if details is not None:
            cache_hit = max(cache_hit, int(getattr(details, "cached_tokens", 0) or 0))
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": int(getattr(u, "completion_tokens", 0) or 0),
            "total_tokens": int(getattr(u, "total_tokens", 0) or 0),
            "cache_hit_tokens": cache_hit,
            "cache_miss_tokens": max(0, prompt_tokens - cache_hit),
        }

    async def chat(self, messages: list[Message], temperature: float = 0.7) -> str:
        """非流式对话,返回完整回复文本。"""
        self._require_key()
        messages = self._cache_control_messages(messages)
        messages = self._maybe_wrap_with_thinking_prompt(messages)
        kwargs: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }

        async def _fn(client, model):
            k = dict(kwargs)
            k["model"] = model
            return await client.chat.completions.create(**k)

        resp = await self._invoke(_fn, "chat")
        msg = resp.choices[0].message
        self._reasoning = self._extract_reasoning(msg)
        return msg.content or ""

    async def chat_stream(self, messages: list[Message], temperature: float = 0.7):
        """流式对话,逐个产出文本增量(str);推理模型先产出 thinking 增量。"""
        self._require_key()
        messages = self._cache_control_messages(messages)
        kwargs: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }

        async def _fn(client, model):
            k = dict(kwargs)
            k["model"] = model
            return await client.chat.completions.create(**k)

        resp = await self._invoke(_fn, "chat_stream")
        reasoning = ""
        try:
            async for chunk in resp:
                if chunk.usage:
                    self.last_usage = self._usage_of(chunk)
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                rc = getattr(delta, "reasoning_content", None)
                if rc and self._is_reasoning_model():
                    reasoning += rc
                    yield {"type": "thinking", "content": rc}
                    continue
                if delta and delta.content:
                    yield delta.content
        except Exception as e:  # noqa: BLE001
            self._trace("chat_stream", time.time(), None, ok=False, error=str(e))
            raise LLMError(f"LLM 流式调用失败: {e}") from e
        self._reasoning = reasoning
        self._trace("chat_stream", time.time(), self.last_usage)

    @property
    def supports_native_tools(self) -> bool:
        """是否支持原生 function calling。

        ollama 的 OpenAI 兼容端点对 tools 的支持随版本而异;
        统一交给上层:先尝试原生,失败自动降级(见 agent.py)。
        """
        return settings.agent_native_tools

    async def chat_with_tools(
        self,
        messages: list[Message],
        tools: Optional[list[dict]] = None,
        temperature: float = 0.7,
    ) -> ChatResult:
        """带工具的函数调用:返回文本内容 + 原生 tool_calls(可为多个,支持并行)。

        Args:
            messages: 消息列表(需含 role/tool_call_id 以支持多轮工具对话)
            tools: OpenAI function schemas;为空则等价于 chat()
        """
        self._require_key()
        messages = self._cache_control_messages(messages)
        messages = self._maybe_wrap_with_thinking_prompt(messages)
        kwargs: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = tools

        async def _fn(client, model):
            k = dict(kwargs)
            k["model"] = model
            return await client.chat.completions.create(**k)

        resp = await self._invoke(_fn, "chat_with_tools", tools_n=len(tools or []))
        choice = resp.choices[0]
        msg = choice.message
        tool_calls: list[ToolCall] = []
        for tc in getattr(msg, "tool_calls", None) or []:
            tool_calls.append(
                ToolCall(
                    id=tc.id or "",
                    name=getattr(tc.function, "name", ""),
                    arguments=getattr(tc.function, "arguments", "") or "{}",
                )
            )
        return ChatResult(
            content=msg.content or "",
            tool_calls=tool_calls,
            finish_reason=getattr(choice, "finish_reason", "") or "",
            usage=self.last_usage,
            reasoning=self._extract_reasoning(msg),
        )

    async def chat_stream_with_tools(
        self,
        messages: list[Message],
        tools: Optional[list[dict]] = None,
        temperature: float = 0.7,
        result_holder: Optional[dict] = None,
    ):
        """流式对话+工具调用:边流式产出 token/thinking 事件,边收集 tool_calls。

        参数:
            result_holder: 可选的 dict,流结束后将 ChatResult 写入 result_holder["result"]。
                          调用方在迭代完成后读取此对象获取完整结果。

        yield:
            str: 文本增量(delta content)
            dict: {"type": "thinking", "content": "..."} 推理内容
            dict: {"type": "tool_call_delta", "index": int, "id": str, "name": str, "arguments": str}
                  工具调用增量(可用于前端实时展示工具名)
        """
        self._require_key()
        messages = self._cache_control_messages(messages)
        messages = self._maybe_wrap_with_thinking_prompt(messages)
        kwargs: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        if tools:
            kwargs["tools"] = tools

        async def _fn(client, model):
            k = dict(kwargs)
            k["model"] = model
            return await client.chat.completions.create(**k)

        resp = await self._invoke(_fn, "chat_stream", tools_n=len(tools or []))
        reasoning = ""
        content_buf = ""
        tool_call_bufs: list[dict] = []  # 每个 tool_call 的累积数据
        try:
            async for chunk in resp:
                if chunk.usage:
                    self.last_usage = self._usage_of(chunk)
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta
                rc = getattr(delta, "reasoning_content", None)
                if rc and self._is_reasoning_model():
                    reasoning += rc
                    yield {"type": "thinking", "content": rc}
                    continue
                # 文本内容
                if delta and delta.content:
                    content_buf += delta.content
                    yield delta.content
                # 工具调用增量
                if delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        while len(tool_call_bufs) <= idx:
                            tool_call_bufs.append({"id": "", "name": "", "arguments": ""})
                        if tc_delta.id:
                            tool_call_bufs[idx]["id"] = tc_delta.id
                        if tc_delta.function:
                            fn = tc_delta.function
                            if fn.name:
                                tool_call_bufs[idx]["name"] = fn.name
                            if fn.arguments:
                                tool_call_bufs[idx]["arguments"] += fn.arguments
                        # 实时发射工具调用增量事件
                        yield {
                            "type": "tool_call_delta",
                            "index": idx,
                            "id": tool_call_bufs[idx]["id"],
                            "name": tool_call_bufs[idx]["name"],
                            "arguments": tool_call_bufs[idx]["arguments"],
                        }
        except Exception as e:  # noqa: BLE001
            self._trace("chat_stream", time.time(), None, ok=False, error=str(e))
            raise LLMError(f"LLM 流式调用失败: {e}") from e
        self._reasoning = reasoning

        # 构建最终结果
        tool_calls = []
        for buf in tool_call_bufs:
            tool_calls.append(ToolCall(
                id=buf.get("id", ""),
                name=buf.get("name", ""),
                arguments=buf.get("arguments", "{}"),
            ))
        result = ChatResult(
            content=content_buf,
            tool_calls=tool_calls,
            reasoning=reasoning,
            usage=self.last_usage,
        )
        if result_holder is not None:
            result_holder["result"] = result

    async def chat_json(
        self,
        messages: list[Message],
        temperature: float = 0.2,
    ) -> dict:
        """结构化 JSON 输出:response_format=json_object,返回解析后的 dict。

        某些 provider 不支持 response_format,会回退为普通 chat 再解析 JSON。
        """
        self._require_key()
        messages = self._cache_control_messages(messages)
        messages = self._maybe_wrap_with_thinking_prompt(messages)
        kwargs: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        }

        async def _fn(client, model):
            k = dict(kwargs)
            k["model"] = model
            return await client.chat.completions.create(**k)

        try:
            resp = await self._invoke(_fn, "chat_json")
            content = resp.choices[0].message.content or ""
        except Exception:  # noqa: BLE001
            content = await self.chat(messages, temperature=temperature)

        data = _extract_json(content)
        if isinstance(data, dict):
            return data
        raise LLMError(f"结构化输出解析失败: 模型未返回合法 JSON ({content[:200]})")

    async def chat_json_schema(
        self,
        messages: list[Message],
        schema: dict,
        temperature: float = 0.2,
        max_retries: int = 2,
        validator=None,
        strict_mode: Optional[bool] = None,
    ) -> dict:
        """严格结构化输出:response_format 绑 JSON Schema + 校验重试。

        Args:
            schema: JSON Schema(dict),传入 response_format 与提示词
            max_retries: 校验失败时的重试次数(把校验错误回灌给模型)
            validator: 可选 callable(data) -> (ok, error_msg);不传则仅做 JSON 解析
            strict_mode: 是否启用严格校验(检查 required/类型);None=跟随 settings
        返回符合 schema 的 dict;耗尽仍失败抛 LLMError。
        增强:校验失败时返回字段级详细错误,便于 LLM 精准修正。
        """
        from app.config import settings as _cfg
        self._require_key()
        messages = self._cache_control_messages(messages)
        messages = self._maybe_wrap_with_thinking_prompt(messages)
        schema_help = (
            f"你必须严格输出符合以下 JSON Schema 的合法 JSON(不要包裹 markdown):\n"
            f"{json.dumps(schema, ensure_ascii=False)}\n"
        )
        prompt = list(messages)
        if prompt and prompt[0].get("role") == "system":
            prompt[0] = {**prompt[0], "content": prompt[0].get("content", "") + "\n\n" + schema_help}
        else:
            prompt = [{"role": "system", "content": schema_help}] + prompt

        # 严格模式:优先使用传入值,否则跟随 settings
        strict_mode = strict_mode if strict_mode is not None else bool(
            getattr(_cfg, "agent_structured_output_strict", True)
        )
        max_retries = int(getattr(_cfg, "agent_structured_output_max_retries", 3) or max_retries)

        def _validate_field_level(data: dict, schema: dict, prefix: str = "") -> list[str]:
            """字段级校验:返回详细错误列表(含字段路径)。"""
            errors: list[str] = []
            props = schema.get("properties", {})
            required = schema.get("required", [])

            # 检查必填字段
            for field in required:
                if field not in data:
                    path = f"{prefix}.{field}" if prefix else field
                    errors.append(f"缺少必填字段: {path}")

            # 检查类型
            for field, value in data.items():
                path = f"{prefix}.{field}" if prefix else field
                if field in props:
                    field_schema = props[field]
                    expected_type = field_schema.get("type", "")
                    if expected_type == "string" and not isinstance(value, str):
                        errors.append(f"字段 {path} 期望字符串类型,实际为 {type(value).__name__}")
                    elif expected_type == "number" and not isinstance(value, (int, float)):
                        errors.append(f"字段 {path} 期望数字类型,实际为 {type(value).__name__}")
                    elif expected_type == "integer" and not isinstance(value, int):
                        errors.append(f"字段 {path} 期望整数类型,实际为 {type(value).__name__}")
                    elif expected_type == "boolean" and not isinstance(value, bool):
                        errors.append(f"字段 {path} 期望布尔类型,实际为 {type(value).__name__}")
                    elif expected_type == "array" and not isinstance(value, list):
                        errors.append(f"字段 {path} 期望数组类型,实际为 {type(value).__name__}")
                    elif expected_type == "object" and not isinstance(value, dict):
                        errors.append(f"字段 {path} 期望对象类型,实际为 {type(value).__name__}")
                    # 递归检查嵌套对象
                    if expected_type == "object" and isinstance(value, dict) and props[field].get("properties"):
                        errors.extend(_validate_field_level(value, props[field], path))
            return errors

        # 尝试 json_schema(最严格) → json_object(降级) → 普通 chat+手动校验(最宽)
        async def _try_with_format(format_type: str, kwargs_extra: dict) -> tuple:
            """尝试用指定 response_format 调用,返回 (content, error)"""
            kwargs = {
                "model": self.model,
                "messages": prompt,
                "temperature": temperature,
            }
            kwargs.update(kwargs_extra)
            kwargs["response_format"] = {"type": format_type}
            if format_type == "json_schema" and strict_mode:
                kwargs["response_format"]["json_schema"] = {
                    "name": "output", "strict": True, "schema": schema
                }
            try:
                resp = await self._invoke(
                    lambda client, model: client.chat.completions.create(**{**kwargs, "model": model}),
                    f"chat_json_schema_{format_type}",
                )
                return resp.choices[0].message.content or "", None
            except Exception as e:  # noqa: BLE001
                return None, str(e)

        last_err: Optional[str] = None
        last_content = ""
        for attempt in range(max_retries + 1):
            if attempt > 0:
                # 把上次校验错误回灌给模型
                prompt = list(messages) + [
                    {"role": "assistant", "content": last_content},
                    {"role": "user", "content": f"上次输出未通过校验: {last_err}\n请重新输出符合 schema 的 JSON。"},
                ]
            content, err = await _try_with_format("json_schema", {})
            if err:
                # 降级到 json_object
                content, err2 = await _try_with_format("json_object", {})
                if err2:
                    last_err = f"JSON 提取失败: {err2}"
                    last_content = ""
                    continue
            else:
                last_err = None

            last_content = content
            data = _extract_json(content)
            if not isinstance(data, dict):
                last_err = "输出不是合法 JSON 对象"
                continue
            # 字段级校验(优先级高于简单 required 检查)
            if strict_mode:
                field_errors = _validate_field_level(data, schema)
                if field_errors:
                    last_err = "字段校验失败:\n" + "\n".join(f"  - {e}" for e in field_errors)
                    continue
            # 额外校验:检查 required 字段(兼容旧逻辑)
            required_fields = schema.get("required", [])
            missing = [f for f in required_fields if f not in data]
            if missing and strict_mode:
                last_err = f"缺少必填字段: {', '.join(missing)}"
                continue
            # 调用自定义 validator
            if validator is not None:
                ok, err = validator(data)
                if not ok:
                    last_err = err or "未通过校验"
                    continue
            return data
        raise LLMError(f"结构化输出校验失败(重试 {max_retries} 次): {last_err}")

    @property
    def info(self) -> dict:
        return {"provider": self.provider, "model": self.model}


def _extract_json(text: str):
    """从文本中提取 JSON 对象;失败返回 None。"""
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 找到第一个 {,然后用括号匹配找到对应的 },避免多余内容被纳入
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    end = start
    for i in range(start, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end == start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def build_llm(provider: Optional[str] = None) -> LLMClient:
    return LLMClient(provider)
