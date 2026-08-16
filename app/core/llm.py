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

    def __init__(self, provider: Optional[str] = None, model: Optional[str] = None):
        self.provider = PROVIDER_ALIASES.get((provider or settings.agent_llm_provider).lower(), "compat")
        self._model_override = model
        self.client, self.model = self._build()
        self._api_key = self._get_key()
        self.last_usage: Optional[dict] = None  # 最近一次调用返回的 token 用量
        self._reasoning: str = ""  # 最近一次推理模型的思维链(reasoning_content)

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
    ) -> dict:
        """严格结构化输出:response_format 绑 JSON Schema + 校验重试。

        Args:
            schema: JSON Schema(dict),传入 response_format 与提示词
            max_retries: 校验失败时的重试次数(把校验错误回灌给模型)
            validator: 可选 callable(data) -> (ok, error_msg);不传则仅做 JSON 解析
        返回符合 schema 的 dict;耗尽仍失败抛 LLMError。
        """
        self._require_key()
        messages = self._cache_control_messages(messages)
        schema_help = (
            f"你必须严格输出符合以下 JSON Schema 的合法 JSON(不要包裹 markdown):\n"
            f"{json.dumps(schema, ensure_ascii=False)}\n"
        )
        prompt = list(messages)
        if prompt and prompt[0].get("role") == "system":
            prompt[0] = {**prompt[0], "content": prompt[0].get("content", "") + "\n\n" + schema_help}
        else:
            prompt = [{"role": "system", "content": schema_help}] + prompt

        kwargs: dict = {
            "model": self.model,
            "messages": prompt,
            "temperature": temperature,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "output", "strict": True, "schema": schema},
            },
        }

        async def _fn(client, model):
            k = dict(kwargs)
            k["model"] = model
            try:
                return await client.chat.completions.create(**k)
            except Exception:
                # 部分 provider 不支持 json_schema,降级为 json_object
                k2 = dict(k)
                k2["response_format"] = {"type": "json_object"}
                return await client.chat.completions.create(**k2)

        last_err: Optional[str] = None
        for attempt in range(max_retries + 1):
            if attempt > 0:
                # 把上次校验错误回灌给模型
                prompt = list(messages) + [
                    {"role": "assistant", "content": last_content},
                    {"role": "user", "content": f"上次输出未通过校验: {last_err}\n请重新输出符合 schema 的 JSON。"},
                ]
                k = dict(kwargs)
                k["messages"] = prompt
            try:
                resp = await self._invoke(_fn, "chat_json_schema", **{"content": ""})
                content = resp.choices[0].message.content or ""
            except Exception as e:  # noqa: BLE001
                raise LLMError(f"结构化输出调用失败: {e}") from e
            last_content = content
            data = _extract_json(content)
            if not isinstance(data, dict):
                last_err = "输出不是合法 JSON 对象"
                continue
            if validator is not None:
                ok, err = validator(data)
                if not ok:
                    last_err = err or "未通过校验"
                    continue
            return data
        raise LLMError(f"结构化输出校验失败: {last_err}")

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
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


def build_llm(provider: Optional[str] = None) -> LLMClient:
    return LLMClient(provider)
